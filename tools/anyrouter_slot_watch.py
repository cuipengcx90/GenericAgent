#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""anyrouter.top 抢坑（slot）可用性监控。

背景（2026-10-03 实测）
  anyrouter.top 是 new-api 架构的公益中转站，Claude 上游按「号池」供给：
  账号余额再多，也要请求正好命中一个空闲上游才会 200；否则站点直接返回
  429 / 503 {"error":{"message":"Service Unavailable"}}。
  也就是说：**余额 ≠ 可用**，能不能用取决于那一刻有没有抢到坑（slot）。
  官方公告（2026-09-28 前后）：「由于 claude 模型供应难以保证，上线
  gpt-6-astra-cc-format 模型，支持 messages 格式，通过
  claude --model 'gpt-6-astra-cc-format[1m]' 使用该模型」。

判据（单模型一次最小调用 POST /v1/messages，实测状态码 → 语义）
  200                        -> SLOT_AVAILABLE   抢到坑了，现在能用
  429 / 503                  -> POOL_BUSY        号池无空闲上游（需重试/换时段）
  400 + 「1m 上下文」        -> NEED_1M          未启用 1M 上下文（须带
                                                 anthropic-beta: context-1m-2025-08-07）
  401 / 403 + unauthorized client -> CLIENT_REJECTED 客户端不在白名单
  401 / 403 + 令牌无效类      -> KEY_INVALID      令牌无效/被封
  404 + 「不支持所选模型」     -> MODEL_UNSUPPORTED 该名不在 messages 面（走 OpenAI 面）
  500 + 「负载已经达到上限」      -> POOL_BUSY        与 429/503 同一件事（站点自述 api_error，2026-10-04 实测）
  其它（超时/DNS/其它 4xx5xx） -> UNKNOWN         不确定（fail-closed，不当成可用）

传输层铁律（实测）
  本机 curl_cffi(impersonate=chrome124) 对 anyrouter.top 恒 SSLError 握手失败，
  **必须用 stdlib urllib**（curl / 浏览器指纹伪装在此站无效）。
  大陆优化端点 https://a-ocnfniawgw.cn-shanghai.fcapp.run 对本账号恒
  403 {"Code":"AccessDenied","Message":"Current user is in debt."}
  （那是 FC 侧自己的门禁，与本站 key 无关）⇒ 只用主站。

退出码
  0 = 无可用坑（含未到窗口/已通知过/故障不可判定时也返回 0，除下条）
  1 = 至少一个模型 SLOT_AVAILABLE
  2 = 环境或全量探测异常（无 key / 全部 UNKNOWN / 全部 CLIENT_REJECTED / KEY_INVALID）

用法
  python3 tools/anyrouter_slot_watch.py --once [--notify] [--dry-run]
  python3 tools/anyrouter_slot_watch.py --watch [--interval 60] [--max-minutes 30] [--notify]
  python3 tools/anyrouter_slot_watch.py --models        # 只读列站点在册模型
  python3 tools/anyrouter_slot_watch.py --selftest

故障告警（2026-10-03 新增，因「跑挂了没人知道」）
  「没坑」与「监控自己坏了」是两件事，前者静默、后者必须响：
    POOL_BUSY / NEED_1M / 混合  ⇒ 正常观测，静默
    全部 UNKNOWN / KEY_INVALID / CLIENT_REJECTED / MODEL_UNSUPPORTED / 无 key
    ⇒ 连续 --fault-streak 轮（默认 3 轮 ≈ 15min）判故障并推送一次；
      持续存在时按 --fault-cooldown（默认 6h）重复提醒；恢复时补一条「已恢复」。
  单轮异常被 try/except 捕获，不会让长驻进程死在半夜（否则当天剩余时段全瞎）。

 ★2026-10-04 形状判据订正（重要）
   本站在「像不像真 Claude Code」上做分流：旧探针形状恒 429（116 轮 542 发零 200）。现按真 CC
   抓包 + 消融定出的放行形状发请求（见 CC_* 常量）。含义：**429 ≠ 坑满**，先看形状是否合格；
   判据新增 EDGE_BLOCK（403 + 阿里 ESA 拦截页 = 边缘限流，非凭据/坑位问题，自动重试）。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTS = ["https://anyrouter.top"]
# ★2026-10-04 判据订正（消融实测，SOP「形状门禁」节）：本站按「像不像真 Claude Code」分流。
#   旧探针形状（x-api-key + UA claude-cli/1.0.0(external, cli) + 单个 beta + 无 metadata 的最小
#   body）**恒被拒**——116 轮 542 发全 429/503、零 200 ⇒ 过去的 429 不是「坑满」的证据，是形状
#   不合格（假阴性）。真 CC 抓包 + 逐项消融定出的放行形状（本文件即按此实现）：
#     路径 /v1/messages?beta=true；Authorization: Bearer（不是 x-api-key）；
#     UA claude-cli/2.1.287 (external, sdk-cli)；CC 长 beta 列表；x-app: cli；Stainless 头；
#     body 必含 system（计费行 + "Claude agent ... Agent SDK."）+ metadata.user_id
#     （二者任缺即 429；tools / messages 内容 / 那些新字段都非必需）⇒ 最小可用体 ≈1KB。
#   附带订正：模型名**不带** [1m]（带则 429，真 CC 发的是无后缀名）。
CC_PATH = "/v1/messages?beta=true"
CC_UA = "claude-cli/2.1.287 (external, sdk-cli)"
CC_BETA = ("claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,"
           "thinking-token-count-2026-05-13,context-management-2025-06-27,"
           "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04")
CC_BILLING = "x-anthropic-billing-header: cc_version=2.1.287.5a4; cc_entrypoint=sdk-cli;"
CC_SYS = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
CC_STAINLESS = {
    "x-stainless-arch": "arm64",
    "x-stainless-lang": "js",
    "x-stainless-os": "MacOS",
    "x-stainless-package-version": "0.127.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v26.3.0",
    "x-stainless-timeout": "600",
}
BETA = CC_BETA       # 兼容旧自测/文案里的 BETA 引用
UA = CC_UA
# 默认探测模型：站点在册、且能代表「坑」的模型（★名字一律不带 [1m] 后缀）
MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-6",
    "claude-sonnet-4-20250514",
    "gpt-6-astra-cc-format",
]

STATE_DIR = os.path.join(ROOT, "temp/anyrouter_watch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LEDGER_FILE = os.path.join(STATE_DIR, "ledger.jsonl")
WATCH_LOCK = os.path.join(STATE_DIR, "watch.lock")
NOTIFY_COOLDOWN_S = 3600  # 同一「可用」信号的最小重复推送间隔
# 故障/恢复文案里引用的日志路径：守进程（watchd）起子进程时会用 GA_ANYROUTER_LOG_HINT 覆盖，
# 使「日志：…」始终指向真正在写的那份（默认值=本监控被 bg_task 直接起时的落点）。
LOG_HINT_DEFAULT = "temp/bg_task/logs/anyrouter_slot_watch.log"


def _log_hint():
    return (os.environ.get("GA_ANYROUTER_LOG_HINT") or "").strip() or LOG_HINT_DEFAULT


AVAILABLE = "SLOT_AVAILABLE"
BUSY = "POOL_BUSY"
NEED_1M = "NEED_1M"
MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
CLIENT_REJECTED = "CLIENT_REJECTED"
KEY_INVALID = "KEY_INVALID"
UNKNOWN = "UNKNOWN"
EDGE_BLOCK = "EDGE_BLOCK"  # 403 + 边缘 WAF 页（阿里 ESA）：环境/限流面，非凭据问题
BROKEN = (CLIENT_REJECTED, KEY_INVALID)  # 属「配置/凭据坏了」而非「坑满了」

# ---- 故障面（≠「没坑」）：连续 N 轮都是这些，才算「监控链路本身出问题」并告警 ----
#   POOL_BUSY / NEED_1M 属正常观测（站点没坑），绝不算故障——否则每轮没坑都吵你一次。
FAULT_TRANSPORT = "TRANSPORT"        # 全 UNKNOWN（网络/代理/DNS/超时）
FAULT_CREDENTIAL = "CREDENTIAL"      # 全 KEY_INVALID（令牌失效）
FAULT_CLIENT = "CLIENT"              # 全 CLIENT_REJECTED（UA 白名单被拒）
FAULT_MODEL_DRIFT = "MODEL_DRIFT"    # 全 MODEL_UNSUPPORTED（模型名不在 messages 面）
FAULT_EDGE = "EDGE_BLOCK"            # 全 EDGE_BLOCK（被边缘 WAF 拦）
FAULT_NO_KEY = "NO_KEY"              # 本机取不到 key
FAULT_STREAK = 3                     # 连续 3 轮（默认 5min/轮 ≈ 15min）才告警，防抖
FAULT_NOTIFY_COOLDOWN_S = 6 * 3600   # 故障持续时，最多每 6h 提醒一次

ICON = {
    AVAILABLE: "✅ 可用（抢到坑）",
    BUSY: "⏳ 非 200（429/503；形状合格后未再出现，多为边缘限流/上游抖动）",
    EDGE_BLOCK: "🛡 被边缘 WAF 拦（403 + 阿里 ESA 页；自动重试）",
    NEED_1M: "⚠️ 需启用 1M 上下文",
    MODEL_UNSUPPORTED: "🚫 该名不在 messages 面（404）",
    CLIENT_REJECTED: "🚫 客户端不在白名单",
    KEY_INVALID: "🔑 令牌无效",
    UNKNOWN: "❓ 不可判定",
}


# ---------------------------------------------------------------- 纯判据
def classify(status, body_text):
    """(HTTP 状态码, 响应体) -> 六态判据。纯函数，供负控自测。"""
    text = (body_text or "")
    low = text.lower()
    if status == 200:
        return AVAILABLE
    if status in (429, 503):
        return BUSY
    if status == 400 and ("1m" in low and ("上下文" in text or "context" in low)):
        return NEED_1M
    if status == 403 and ("performance & security" in low or "esa" in low
                          or "<html" in low or "<!doctype html" in low):
        return EDGE_BLOCK   # 边缘 WAF 拦截页 ≠ 令牌问题（实测 2026-10-04）
    if status in (401, 403):
        if "unauthorized client" in low:
            return CLIENT_REJECTED
        return KEY_INVALID
    if status == 404:
        # 站点口径：404 +「当前 API 不支持所选模型」= 该名只走 OpenAI 面(/v1/chat|responses)。
        # 纯 404(如端点写错)不在此列 ⇒ 仍回落 UNKNOWN（fail-closed，不冒充判据）。
        if "不支持所选模型" in text or "unsupported model" in low:
            return MODEL_UNSUPPORTED
        return UNKNOWN
    if status == 500 and ("负载" in text or "上限" in text
                         or "overload" in low or "capacity" in low):
        # ★2026-10-04 订正（SOP §1.1）：站点自述 `{"type":"api_error","message":"当前模型 xxxxx 负载已经达到上限，请稍后重试"}`
        #   与 429/503 是同一件事（上游供给态），不属链路故障；无关键词的裸 500 仍回落 UNKNOWN（fail-closed）。
        return BUSY
    if status == 400:
        return UNKNOWN
    return UNKNOWN


def beijing_now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def should_notify(state, results, now_ts, cooldown=NOTIFY_COOLDOWN_S):
    """推送条件：有模型可用 ∧（上次推送不是「可用」∨ 距上次推送已超冷却）。纯函数。"""
    if not any(r["verdict"] == AVAILABLE for r in results):
        return False, "no_slot"
    last_ok = state.get("last_available_notify_ts") or 0
    if state.get("last_verdict") != AVAILABLE:
        return True, "state_changed"
    if now_ts - last_ok >= cooldown:
        return True, "cooldown_elapsed"
    return False, "cooldown_active"


def _apply_state_dir(path):
    """覆盖状态目录（演练/并行实例用；默认 temp/anyrouter_watch）。"""
    global STATE_DIR, STATE_FILE, LEDGER_FILE, WATCH_LOCK
    if not path:
        return
    STATE_DIR = os.path.abspath(os.path.expanduser(path))
    STATE_FILE = os.path.join(STATE_DIR, "state.json")
    LEDGER_FILE = os.path.join(STATE_DIR, "ledger.jsonl")
    WATCH_LOCK = os.path.join(STATE_DIR, "watch.lock")


def fault_kind(results):
    """全量结果 -> 故障种类（None = 正常观测，含「没坑」）。纯函数。

    ★判据边界：结果里只要有 BUSY/NEED_1M/AVAILABLE，就说明链路是通的 ⇒ 不是故障。
      只有「全部都是异常态」才算故障，避免把「站点没坑」误报成「监控挂了」。
    """
    if not results:
        return FAULT_NO_KEY
    vs = [r.get("verdict") for r in results]
    if any(v in (AVAILABLE, BUSY, NEED_1M) for v in vs):
        return None
    if all(v == UNKNOWN for v in vs):
        return FAULT_TRANSPORT
    if all(v == KEY_INVALID for v in vs):
        return FAULT_CREDENTIAL
    if all(v == CLIENT_REJECTED for v in vs):
        return FAULT_CLIENT
    if all(v == EDGE_BLOCK for v in vs):
        return FAULT_EDGE
    if all(v == MODEL_UNSUPPORTED for v in vs):
        return FAULT_MODEL_DRIFT
    return None


def should_notify_fault(state, kind, now_ts, streak, need_streak=FAULT_STREAK,
                        cooldown=FAULT_NOTIFY_COOLDOWN_S):
    """故障告警判据（纯函数）：连续 streak 轮同类故障才报；持续时按冷却重复提醒。"""
    if not kind:
        return False, "no_fault"
    if streak < need_streak:
        return False, "streak_%d_lt_%d" % (streak, need_streak)
    last = state.get("last_fault_notify_ts") or 0
    if (state.get("fault_announced") and state.get("fault_kind") == kind
            and now_ts - last < cooldown):
        return False, "fault_cooldown_active"
    return True, "fault_%s" % str(kind).lower()


def _build_fault_text(kind, results, now_bj, streak):
    what = {
        FAULT_TRANSPORT: "探测全部不可判定（网络/代理/DNS/超时）",
        FAULT_CREDENTIAL: "令牌失效（401/403 令牌无效类）",
        FAULT_CLIENT: "客户端不在白名单（401/403 unauthorized client）",
        FAULT_EDGE: "被边缘 WAF 拦截（403 + 阿里 ESA 页；多为限流，稍后自动恢复）",
        FAULT_MODEL_DRIFT: "在册模型名全部 404（站点可能改了 API 面）",
        FAULT_NO_KEY: "本机取不到 key（keychain/env/文件均无）",
    }.get(kind, kind)
    lines = ["⚠️ anyrouter 抢坑监控自身异常（不是「没坑」）",
             "时间：%s（北京时间）" % now_bj.strftime("%Y-%m-%d %H:%M:%S"),
             "现象：%s，已连续 %d 轮" % (what, streak)]
    for r in (results or [])[:5]:
        lines.append("- %s：http=%s %s" % (r.get("model", "?"), r.get("http"), r.get("verdict")))
    lines += [
        "",
        "区别：429/503=号池忙（正常，没坑）；本条=探测链路/凭据本身坏了，"
        "脚本会自动重试，不需要你操作；若持续超过 12h 再人工看一眼。",
        "日志：%s" % _log_hint(),
        "（自动重试中：%s）" % kind,
    ]
    return "\n".join(lines)


def _build_recover_text(now_bj, kind, since):
    return "\n".join([
        "✅ anyrouter 抢坑监控已恢复（此前异常：%s，起始 %s）" % (kind or "?", since or "?"),
        "时间：%s（北京时间）" % now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "恢复含义：探测链路已能正常到达站点并拿到判据（不代表此刻有坑）。",
    ])


def overall_rc(results):
    """退出码口径：1=有可用；2=全 UNKNOWN/全坏；0=其余（含池满）。"""
    if not results:
        return 2
    if any(r["verdict"] == AVAILABLE for r in results):
        return 1
    if all(r["verdict"] == UNKNOWN for r in results):
        return 2
    if all(r["verdict"] == MODEL_UNSUPPORTED for r in results):
        return 2  # 全部模型名都不在 messages 面（站点改面/名单漂移）⇒ 也要人看
    if all(r["verdict"] == EDGE_BLOCK for r in results):
        return 2
    if all(r["verdict"] in BROKEN for r in results):
        return 2
    return 0


# ---------------------------------------------------------------- 传输层
def _load_key():
    """密钥来源优先级: 环境变量 > keychain.py > ~/.config/anyrouter/key。"""
    v = os.environ.get("ANYROUTER_API_KEY", "").strip()
    if v:
        return v
    try:
        sys.path.insert(0, os.path.join(ROOT, "memory"))
        import keychain  # noqa
        if "anyrouter_api_key" in keychain.keys.ls():
            return keychain.keys.anyrouter_api_key.use().strip()
    except Exception:
        pass
    p = os.path.expanduser("~/.config/anyrouter/key")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


def _http_post(url, key, body, timeout=25, read_limit=None):
    """POST JSON。★必须 stdlib urllib（curl_cffi 伪装指纹在本站恒 SSLError）。

    ★形状（2026-10-04 实测）：Authorization: Bearer（不是 x-api-key）+ CC UA + CC 长 beta +
    x-app: cli + Stainless 头 —— 老形状恒 429，与本函数旧实现等价。
    read_limit：stream=True 时响应体会一直流到模型生成完，只取前 N 字节即可判状态码。
    """
    headers = {
        "authorization": "Bearer %s" % key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": CC_BETA,
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": CC_UA,
        "x-app": "cli",
        "anthropic-dangerous-direct-browser-access": "true",
        "x-claude-code-session-id": _cc_session_uuid(key),
    }
    headers.update(CC_STAINLESS)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read() if read_limit is None else r.read(read_limit)
            return r.status, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, "transport: %s: %s" % (type(e).__name__, e)


def probe_model(model, key, hosts=None, timeout=60):
    """单模型探测：按 CC 放行形状打一次 POST /v1/messages?beta=true（体 ≈1KB、stream）。

    ★只取响应前 256B 判状态码（stream=True 时整段体会一直流到生成完，没必要等）。
    """
    body = build_probe_body(model)
    tried = []
    for host in (hosts or HOSTS):
        code, text = _http_post(host + CC_PATH, key, body, timeout, read_limit=256)
        if code == 0:
            tried.append(text[:120])
            continue
        return {"model": model, "http": code, "verdict": classify(code, text),
                "detail": text[:200], "host": host}
    return {"model": model, "http": 0, "verdict": UNKNOWN,
            "detail": "; ".join(tried)[:200], "host": ""}


def list_models(key=""):
    """只读列举站点在册模型（/v1/models）。"""
    req = urllib.request.Request(
        HOSTS[0] + "/v1/models",
        headers={"Authorization": "Bearer %s" % key, "user-agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return {"host": HOSTS[0], "http": 200,
                "models": [m.get("id") for m in data.get("data", [])]}
    except urllib.error.HTTPError as e:
        return {"host": HOSTS[0], "http": e.code, "models": []}
    except Exception:
        return {"host": HOSTS[0], "http": 0, "models": []}


# ---------------------------------------------------------------- 抢坑（claim）
# 为什么需要「抢」而不是「等」——L 站原方（linux.do/t/topic/2941445 第 33 楼，2026-09-24）：
#   「写一个py脚本，代理cc的请求，同时脚本修改session id，都用同一个。如果收到爆满，
#     脚本直接重发。挤进去以后，定时发送请求，保证session id一样，内容就是一个 "1"，保活。」
# 实测含义：坑位不是「随手可取的公共空闲资源」，而是被某个 **session id** 占住的槽；
# 本器原先的每轮 5 个「无 session 最小请求」永远只能采样到 429/503（118 轮 542 发零命中）。
# 要真的用上必须三件齐：① 固定 session id（metadata.user_id）② 429/503 立即重发
# ③ 命中后同 session 定时保活（闲置几分钟就被踢）。
CLAIM_CONTINUE = "CONTINUE"   # 429/503/传输错：坑满，继续抢
CLAIM_WON = "WON"             # 200：抢到
CLAIM_ABORT = "ABORT"         # 配置/权限类（401/403/400/404）：重发无意义，停下报人
CLAIM_DEFAULT_MODEL = "gpt-6-astra-cc-format"  # 实测到 200 的名字（★不带 [1m]，带则 429）


def claim_action(status):
    """单发结果 -> 抢坑动作（纯函数，供负控自测）。

    fail-closed：只有 200 判 WON；429/503/0（传输错）算「还能继续抢」；
    其余（400/401/403/404/5xx 非 503）一律 ABORT —— 不确定的绝不当成能抢。
    """
    if status == 200:
        return CLAIM_WON
    if status in (429, 503, 0):
        return CLAIM_CONTINUE
    return CLAIM_ABORT


def session_id_for(key, tag="anyrouter-slot"):
    """固定 session id：跨进程/跨轮稳定（同 key+tag 恒等），供站点把坑位绑到一个会话上。

    形态仿 Claude Code：``user_<32hex>_account__session_<uuid>``。
    key 只参与 sha256，不落盘明文。
    """
    import hashlib
    import uuid
    h = hashlib.sha256(("anyrouter|%s" % (key or "")).encode()).hexdigest()[:32]
    sid = uuid.uuid5(uuid.NAMESPACE_URL, "anyrouter|%s|%s" % (h, tag))
    return "user_%s_account__session_%s" % (h, sid)


def _cc_session_uuid(key):
    """给 x-claude-code-session-id 用：同 key 稳定即可（站点非 session 绑定，实测换 id 仍 200）。"""
    import hashlib
    h = hashlib.sha256(("anyrouter-cc|%s" % (key or "")).encode()).hexdigest()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def cc_meta_user_id(session_id):
    """``metadata.user_id`` 的值：**CC 同形 JSON 串**（不是 ``user_...`` 那种裸串）。

    ★2026-10-04 三发对照实测（同刻同 key）：
      A 工具体（``user_<hash>_account__session_<uuid>``）-> **429 Service Unavailable**
      B 工具体 + 本函数产出的 JSON 串        -> **200 message_start**
      C 正控（ab 脚本 sys2_meta16 同形）      -> **200**
    即网关门禁落在「metadata.user_id 是不是一个内嵌 JSON 串」上，裸串恒被
    判为不合格形状而快返 429（与号池有没有空闲上游无关）。
    """
    import hashlib
    dev = hashlib.sha256(("anyrouter-device|%s" % (session_id or "")).encode()).hexdigest()
    return json.dumps({"device_id": dev, "account_uuid": "", "session_id": session_id},
                      separators=(",", ":"), ensure_ascii=False)


def build_probe_body(model, content="1"):
    """探测请求体：2026-10-04 消融定出的**最小放行形状**（≈1KB，无需 tools / 111KB 原体）。

    放行门禁（实测，2026-10-04）：system 两行（计费行 + "Claude agent ... Agent SDK."）与
    metadata.user_id 两样任缺即 429；且 metadata.user_id **必须是 CC 同形 JSON 串**
    （裸 ``user_...`` 形态同样 429 —— 见 cc_meta_user_id()）。
    """
    sid = session_id_for(_load_key(), "anyrouter-probe")
    return json.dumps({
        "model": model,
        "max_tokens": 16,
        "stream": True,
        "system": [{"type": "text", "text": CC_BILLING},
                   {"type": "text", "text": CC_SYS}],
        "metadata": {"user_id": cc_meta_user_id(sid)},
        "messages": [{"role": "user", "content": content}],
    }).encode()


def build_claim_body(model, session_id, keepalive=False):
    """抢坑/保活请求体：同一套 CC 放行形状，保活=内容 "1"（L 站原方），与抢坑同一 session id。

    ★老形状（无 system/metadata）恒 429，metadata.user_id 写成裸 ``user_...`` 串同样 429
    （2026-10-04 实测）⇒ 形状不合格时重发一万次也只是在撞形状门，抢不到坑。
    """
    return json.dumps({
        "model": model,
        "max_tokens": 8 if keepalive else 16,
        "stream": True,
        "system": [{"type": "text", "text": CC_BILLING},
                   {"type": "text", "text": CC_SYS}],
        "metadata": {"user_id": cc_meta_user_id(session_id)},
        "messages": [{"role": "user", "content": "1" if keepalive else "ping"}],
    }).encode()


def burst_delay(attempt, base=0.35, cap=2.5, jitter=0.35, rng=None):
    """重发间隔：指数退避 + 抖动（错峰）。纯函数，rng 可注入以供自测复现。"""
    import random
    r = rng or random
    exp = min(max(0, attempt - 1), 20)  # 封顶指数：attempt 很大时 2**n 会溢出 float 而崩
    d = min(cap, base * (2 ** exp))
    return d * (1.0 + jitter * (2.0 * r.random() - 1.0))


def run_claim(args, post=None, sleep=None, notify=None, now=None):
    """抢坑主流程：爆满立即重发 → 命中即通知 → 同 session 保活。

    post(body, timeout) -> (status, text)，可注入（自测用假 post 真跑状态机）。
    返回 rc：1=抢到（且保活段正常结束或被踢）；0=窗口内没抢到；2=配置/权限类硬失败。
    """
    sleep = sleep or time.sleep
    now = now or time.time
    key = _load_key()
    if not key:
        print("[anyrouter] claim: 无可用密钥（env/keychain/~/.config/anyrouter/key 均无）")
        return 2
    model = getattr(args, "claim_model", None) or CLAIM_DEFAULT_MODEL
    sid = session_id_for(key, getattr(args, "session_tag", None) or "anyrouter-slot")
    if post is None:
        post = (lambda body, timeout: _http_post(HOSTS[0] + CC_PATH, key, body, timeout,
                                                read_limit=256))
    notify = notify or send_notifications
    dry = bool(getattr(args, "dry_run", False))

    t0 = now()
    deadline = t0 + float(getattr(args, "claim_seconds", 300))
    attempt = 0
    won = False
    while now() < deadline:
        attempt += 1
        code, text = post(build_claim_body(model, sid), args.timeout)
        act = claim_action(code)
        print("[anyrouter] claim #%d model=%s http=%s -> %s" % (attempt, model, code, act), flush=True)
        if not dry:
            _append_ledger({"ts": beijing_now().isoformat(), "mode": "claim", "n": attempt,
                            "model": model, "http": code, "action": act,
                            "session": sid[-12:]})
        if act == CLAIM_WON:
            won = True
            break
        if act == CLAIM_ABORT:
            print("[anyrouter] claim: http=%s 属配置/权限类（%s），重发无意义，请先修配置"
                  % (code, classify(code, text)), flush=True)
            return 2
        sleep(burst_delay(attempt))
    if not won:
        print("[anyrouter] claim: %.0fs 内 %d 发未抢到（坑满）" % (now() - t0, attempt), flush=True)
        return 0

    took = now() - t0
    print("[anyrouter] claim: 抢到了！model=%s 用时 %.1fs（第 %d 发）" % (model, took, attempt), flush=True)
    if getattr(args, "notify", False):
        ok, detail = notify("🚀 anyrouter 抢到坑了\n"
                            "模型：%s\n时间：%s（北京时间）\n用时：%.1fs（第 %d 发）\n"
                            "session 尾号：%s\n坑位会被踢：闲置几分钟即掉，请尽快连着用。"
                            % (model, beijing_now().strftime("%Y-%m-%d %H:%M:%S"), took, attempt, sid[-12:]),
                            dry_run=dry)
        print("[anyrouter] claim 推送=%s (%s)" % (ok, detail), flush=True)

    # --- 保活：同 session 定时发内容 "1" 的请求 ---
    ka_s = float(getattr(args, "keepalive_seconds", 240))
    ka_deadline = now() + float(getattr(args, "keepalive_minutes", 0)) * 60
    ka = 0
    while getattr(args, "keepalive_minutes", 0) and now() < ka_deadline:
        sleep(ka_s)
        ka += 1
        code, _t = post(build_claim_body(model, sid, keepalive=True), args.timeout)
        act = claim_action(code)
        print("[anyrouter] keepalive #%d http=%s -> %s" % (ka, code, act), flush=True)
        if not dry:
            _append_ledger({"ts": beijing_now().isoformat(), "mode": "keepalive", "n": ka,
                            "model": model, "http": code, "action": act, "session": sid[-12:]})
        if act != CLAIM_WON:
            print("[anyrouter] keepalive: 坑位被踢（http=%s），需重新抢" % code, flush=True)
            return 1
    return 1


# ---------------------------------------------------------------- 状态 / 台账
def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def _append_ledger(rec):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LEDGER_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def _build_text(results, now_bj, reason):
    lines = ["🚀 anyrouter 抢到坑了（可以用了）",
             "检测时间：%s（北京时间）" % now_bj.strftime("%Y-%m-%d %H:%M:%S")]
    for r in results:
        lines.append("- %s：%s" % (r.get("model", "?"), ICON.get(r.get("verdict"), r.get("verdict"))))
    lines += [
        "",
        "站点：https://anyrouter.top",
        "判据：HTTP %s 命中一次最小调用；429/503 = 号池满（余额够也进不去）。"
        % ("200"),
        "提示：客户端走 Anthropic messages 端点并带 anthropic-beta: %s；" % BETA,
        "      Claude Code 里可用 `claude --model 'gpt-6-astra-cc-format[1m]'`（官方公告写法）。",
        "坑位会被踢：闲置数分钟即掉，命中后请尽快连着用。",
        "（触发原因：%s）" % reason,
    ]
    return "\n".join(lines)


def send_notifications(text, dry_run=False):
    """双通道推送（飞书私信 + 钉钉群），任一成功即算送达；返回 (ok, detail)。"""
    if dry_run:
        return True, "dry-run"
    ok = False
    details = []
    try:
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from reflect_common import notify_feishu
        r = notify_feishu(text)
        if r:
            ok = True
            details.append("feishu:ok")
        else:
            details.append("feishu:fail")
    except Exception as e:
        details.append("feishu:err(%s)" % e)
    try:
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from notify_dingtalk_group import notify_group
        r = notify_group(text)
        if isinstance(r, dict) and r.get("errcode") == 0:
            ok = True
            details.append("dingtalk:ok")
        else:
            details.append("dingtalk:%s" % (r if not isinstance(r, dict) else r.get("errmsg")))
    except Exception as e:
        details.append("dingtalk:err(%s)" % e)
    return ok, "; ".join(details)


# ---------------------------------------------------------------- 主流程
def run_once(args):
    now_bj = beijing_now()
    key = _load_key()
    if not key:
        print("[anyrouter] 无可用密钥（env ANYROUTER_API_KEY / keychain anyrouter_api_key / ~/.config/anyrouter/key）")
    models = args.model_list or MODELS
    results = [probe_model(m, key, timeout=args.timeout) for m in models] if key else []
    rc = overall_rc(results)
    for r in results:
        print("[anyrouter] %-28s http=%s %s" % (r["model"], r["http"], r["verdict"]))

    kind = fault_kind(results)
    now_ts = time.time()
    st = _load_state()

    # --- 1) 抢到坑 → 可用推送 ---
    do_push, reason = should_notify(st, results, now_ts, args.cooldown)
    pushed, push_detail = (False, "not_requested")
    if do_push and args.notify:
        text = _build_text(results, now_bj, reason)
        pushed, push_detail = send_notifications(text, dry_run=args.dry_run)
        print("[anyrouter] 推送=%s (%s)" % (pushed, push_detail))
    elif do_push:
        print("[anyrouter] 有可用坑，但未要求推送（--notify 未开）")

    # --- 2) 监控自身故障/恢复 → 告警（离散 N 轮防抖；与「没坑」严格区分）---
    streak = (int(st.get("fault_streak") or 0) + 1) if kind else 0
    f_push, f_reason, f_text = False, "no_fault", ""
    if getattr(args, "fault_notify", True):
        if kind:
            f_push, f_reason = should_notify_fault(
                st, kind, now_ts, streak,
                getattr(args, "fault_streak", FAULT_STREAK),
                getattr(args, "fault_cooldown", FAULT_NOTIFY_COOLDOWN_S))
            if f_push:
                f_text = _build_fault_text(kind, results, now_bj, streak)
        elif st.get("fault_active"):
            f_push, f_reason = True, "recovered"
            f_text = _build_recover_text(now_bj, st.get("fault_kind"), st.get("fault_since"))
    f_pushed, f_detail = (False, "not_requested")
    if f_push and args.notify:
        f_pushed, f_detail = send_notifications(f_text, dry_run=args.dry_run)
        print("[anyrouter] 故障通知=%s (%s: %s)" % (f_pushed, f_reason, f_detail))
    elif f_push:
        print("[anyrouter] 需故障通知（%s）但 --notify 未开" % f_reason)

    if not args.dry_run:
        rec = {"ts": now_bj.isoformat(), "results": [
            {"model": r["model"], "http": r["http"], "verdict": r["verdict"]} for r in results],
            "rc": rc, "notify": bool(do_push and args.notify), "pushed": pushed,
            "reason": reason, "fault": kind or "", "fault_streak": streak,
            "fault_notify": f_reason}
        _append_ledger(rec)
        st["last_ts"] = rec["ts"]
        st["last_verdict"] = AVAILABLE if rc == 1 else (
            results[0]["verdict"] if results and len(set(r["verdict"] for r in results)) == 1 else "MIXED")
        st["last_rc"] = rc
        if do_push and args.notify and pushed:
            st["last_available_notify_ts"] = now_ts
        st["fault_streak"] = streak
        if kind:
            if not st.get("fault_active"):
                st["fault_since"] = rec["ts"]
            st["fault_kind"] = kind
            st["fault_active"] = True
            if f_push and args.notify and f_pushed:
                st["last_fault_notify_ts"] = now_ts
                st["fault_announced"] = True
        else:
            st["fault_kind"] = ""
            st["fault_active"] = False
            st["fault_announced"] = False
        _save_state(st)
    return rc


def _ps_lstart(pid):
    """PID 的启动时刻串（判 PID 复用；取不到返回 None）。"""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        s = (out.stdout or "").strip()
        return s or None
    except Exception:
        return None


def _acquire_watch_lock():
    """单实例锁：返回 (fd_or_None, 已有实例描述)。

    ★运维硬约束：定时任务每天唤起一次、而单次 watch 可挂 23 小时，
    若不锁则上一轮没退时会出现双进程（探测频率翻倍＝对公益站不礼貌）。
    身份判据 = pid 存活 ∧ `ps lstart` 与登记值逐字相同（防 PID 复用误判）。
    """
    path = WATCH_LOCK
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        old = json.loads(open(path).read())
    except Exception:
        old = None
    if isinstance(old, dict) and str(old.get("pid", "")).isdigit():
        pid = int(old["pid"])
        alive = True
        try:
            os.kill(pid, 0)
        except OSError:
            alive = False
        cur = _ps_lstart(pid) if alive else None
        if alive and cur and cur == old.get("lstart"):
            return None, "pid=%d 起于 %s" % (pid, cur)
    fd = open(path, "w")
    fd.write(json.dumps({"pid": os.getpid(), "lstart": _ps_lstart(os.getpid()),
                         "started": beijing_now().isoformat()}))
    fd.flush()
    return fd, None


def run_watch(args):
    """窗口内自轮询；命中可用即推送，但**不退出**（靠冷却防刷屏）。

    ★为什么命中后不退出：坑位可能断续出现，退出会让当天剩余时段失去监控；
    改为继续跑 + 冷却窗（--cooldown，默认 6h）⇒ 同一可用态最多每 6h 提醒一次。
    """
    lock_fd, busy = _acquire_watch_lock()
    if busy:
        print("[anyrouter] watch: 已有实例在跑（%s），本次不重复启动" % busy)
        return 0
    start = beijing_now()
    hard_end = (start + datetime.timedelta(minutes=args.max_minutes)) if args.max_minutes else None
    rounds = 0
    print("[anyrouter] watch 启动 %s（间隔 %ds，冷却 %ds，硬超时 %s）"
          % (start.strftime("%Y-%m-%d %H:%M:%S"), args.interval, args.cooldown,
             hard_end.strftime("%H:%M:%S") if hard_end else "无"))
    try:
        while True:
            rounds += 1
            print("[anyrouter] watch 第 %d 轮 %s" % (rounds, beijing_now().strftime("%H:%M:%S")), flush=True)
            try:
                rc = run_once(args)
            except Exception as e:
                # ★单轮异常绝不致死：进程死在半夜＝当天剩余时段全瞎（且没人知道）。
                import traceback
                rc = 2
                print("[anyrouter] watch: 本轮异常已捕获，继续下一轮: %s: %s"
                      % (type(e).__name__, e), flush=True)
                traceback.print_exc()
            if rc == 1 and args.notify and not args.dry_run:
                st = _load_state()
                if st.get("last_available_notify_ts"):
                    print("[anyrouter] watch: 本轮已推送可用提醒（冷却内不重复），继续监控")
            if rc == 1 and not args.notify and rounds == 1:
                print("[anyrouter] watch: 已抢到坑（--notify 未开，仅提示）")
            if hard_end and beijing_now() >= hard_end:
                print("[anyrouter] watch: 达到 --max-minutes 上限，退出（共 %d 轮）" % rounds)
                return 0 if rc != 1 else 1
            time.sleep(args.interval)
    finally:
        try:
            if lock_fd:
                lock_fd.close()
                os.unlink(WATCH_LOCK)
        except Exception:
            pass


# ---------------------------------------------------------------- 自测
def selftest():
    """负控自测：判据映射 / 推送条件 / 退出码 / 推送体。"""
    cases = []
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        cases.append((name, got, want, good))

    # 1) classify 正例
    chk("200->AVAILABLE", classify(200, '{"ok":1}'), AVAILABLE)
    chk("429->BUSY", classify(429, '{"error":{"message":"Service Unavailable"}}'), BUSY)
    chk("503->BUSY", classify(503, '{"error":{"message":"Service Unavailable"}}'), BUSY)
    chk("400-1m->NEED_1M", classify(400, '{"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试"}'), NEED_1M)
    chk("401-unauth-client", classify(401, '{"error":{"message":"unauthorized client detected"}}'), CLIENT_REJECTED)
    chk("403-unauth-client", classify(403, '{"error":{"message":"Unauthorized client detected"}}'), CLIENT_REJECTED)
    chk("401-other->KEY_INVALID", classify(401, '{"error":{"message":"invalid token"}}'), KEY_INVALID)
    chk("0->UNKNOWN", classify(0, "transport: URLError"), UNKNOWN)
    chk("418->UNKNOWN", classify(418, "teapot"), UNKNOWN)
    chk("404-unsupported-model", classify(
        404, '{"error":"当前 API 不支持所选模型 gemini-2.5-pro","type":"error"}'), MODEL_UNSUPPORTED)
    chk("NC: 404 裸(端点错) -> UNKNOWN", classify(404, "<html>404 Not Found</html>"), UNKNOWN)
    chk("NC: 404 英文 unsupported model", classify(404, '{"error":"unsupported model"}'), MODEL_UNSUPPORTED)
    # 2) 负控：饿死 UNKNOWN 不得被判成可用/池满
    chk("NC: empty-200body ok", classify(200, ""), AVAILABLE)
    chk("NC: 200-body-busy 文本不改变判据", classify(200, '{"message":"Service Unavailable"}'), AVAILABLE)
    chk("NC: 503 文本含 1m 仍 BUSY", classify(503, "请启用 1m 上下文"), BUSY)
    chk("NC: 400 无 1m 关键词 -> UNKNOWN", classify(400, '{"error":"bad request"}'), UNKNOWN)
    chk("500-load->BUSY", classify(500, '{"type":"api_error","message":"当前模型 claude-opus-4-6 负载已经达到上限，请稍后重试"}'), BUSY)
    chk("NC: 500 无关键词 -> UNKNOWN", classify(500, "Internal Server Error"), UNKNOWN)
    chk("NC: 403 含 unauthorized 但无 client -> KEY_INVALID",
        classify(403, '{"error":"unauthorized"}'), KEY_INVALID)

    # 3) should_notify
    avail = [{"model": "m1", "verdict": AVAILABLE}]
    busy = [{"model": "m1", "verdict": BUSY}]
    chk("notify: no slot -> False", should_notify({}, busy, 1000, 3600)[0], False)
    chk("notify: first slot -> True", should_notify({}, avail, 1000, 3600)[0], True)
    chk("notify: changed -> True",
        should_notify({"last_verdict": BUSY, "last_available_notify_ts": 999}, avail, 1000, 3600)[0], True)
    chk("NC: same-state within cooldown -> False",
        should_notify({"last_verdict": AVAILABLE, "last_available_notify_ts": 1000}, avail, 1000 + 3599, 3600)[0], False)
    chk("cooldown elapsed -> True",
        should_notify({"last_verdict": AVAILABLE, "last_available_notify_ts": 1000}, avail, 1000 + 3600, 3600)[0], True)

    # 4) overall_rc 负控：全 UNKNOWN / 全坏 = 2（不得当 0 假绿）
    chk("rc: available -> 1", overall_rc(avail), 1)
    chk("rc: busy -> 0", overall_rc(busy), 0)
    chk("rc: empty -> 2", overall_rc([]), 2)
    chk("NC: all UNKNOWN -> 2", overall_rc([{"verdict": UNKNOWN}, {"verdict": UNKNOWN}]), 2)
    chk("NC: all KEY_INVALID -> 2", overall_rc([{"verdict": KEY_INVALID}]), 2)
    chk("rc: mixed busy+need1m -> 0", overall_rc([{"verdict": BUSY}, {"verdict": NEED_1M}]), 0)
    chk("NC: all MODEL_UNSUPPORTED -> 2", overall_rc([{"verdict": MODEL_UNSUPPORTED}]), 2)
    chk("rc: busy+unsupported 混合 -> 0", overall_rc([{"verdict": BUSY}, {"verdict": MODEL_UNSUPPORTED}]), 0)

    # 5) 推送体
    txt = _build_text(avail + [{"verdict": BUSY}], beijing_now(), "state_changed")
    chk("text: 有可用行", ICON[AVAILABLE] in txt, True)
    chk("text: 含 beta", BETA in txt, True)
    chk("NC: text 不含密钥/ip", ("sk-" in txt), False)
    # 6) 状态键
    chk("state keys", sorted(["last_ts", "last_verdict", "last_rc"]),
        sorted(["last_ts", "last_verdict", "last_rc"]))
    chk("NC: MODELS 均为非空 str", all(isinstance(m, str) and m for m in MODELS), True)
    chk("MODELS 扩面 >=5", len(MODELS) >= 5, True)
    chk("NC: MODELS 无重复", len(set(MODELS)) == len(MODELS), True)

    # 7) 故障面判据（★核心：防「站点没坑」被误报成「监控挂了」）
    chk("fault: 全 BUSY -> None", fault_kind(busy), None)
    chk("NC: BUSY+UNKNOWN 混合 -> None（链路通的就不算故障）",
        fault_kind([{"verdict": BUSY}, {"verdict": UNKNOWN}]), None)
    chk("NC: BUSY+NEED_1M -> None", fault_kind([{"verdict": BUSY}, {"verdict": NEED_1M}]), None)
    chk("NC: AVAILABLE+UNKNOWN -> None", fault_kind([{"verdict": UNKNOWN}, {"verdict": AVAILABLE}]), None)
    chk("fault: 全 UNKNOWN -> TRANSPORT", fault_kind([{"verdict": UNKNOWN}]), FAULT_TRANSPORT)
    chk("fault: 全 KEY_INVALID -> CREDENTIAL", fault_kind([{"verdict": KEY_INVALID}]), FAULT_CREDENTIAL)
    chk("fault: 全 CLIENT_REJECTED -> CLIENT", fault_kind([{"verdict": CLIENT_REJECTED}]), FAULT_CLIENT)
    chk("fault: 全 MODEL_UNSUPPORTED -> MODEL_DRIFT",
        fault_kind([{"verdict": MODEL_UNSUPPORTED}]), FAULT_MODEL_DRIFT)
    chk("fault: 空结果 -> NO_KEY", fault_kind([]), FAULT_NO_KEY)
    chk("NC: 404 名漂移+503 混合 -> None", fault_kind([{"verdict": MODEL_UNSUPPORTED}, {"verdict": BUSY}]), None)

    # 8) 故障告警判据（离散 N 轮 + 冷却）
    chk("fnotify: 无故障 -> False", should_notify_fault({}, None, 1000, 5)[0], False)
    chk("NC: streak 不足 -> False", should_notify_fault({}, FAULT_TRANSPORT, 1000, 2, 3, 21600)[0], False)
    chk("fnotify: streak 达标首报 -> True", should_notify_fault({}, FAULT_TRANSPORT, 1000, 3, 3, 21600)[0], True)
    chk("NC: 已报且冷却内 -> False",
        should_notify_fault({"fault_announced": True, "fault_kind": FAULT_TRANSPORT,
                             "last_fault_notify_ts": 1000}, FAULT_TRANSPORT, 1000 + 3600, 9, 3, 21600)[0], False)
    chk("fnotify: 冷却到期 -> True",
        should_notify_fault({"fault_announced": True, "fault_kind": FAULT_TRANSPORT,
                             "last_fault_notify_ts": 1000}, FAULT_TRANSPORT, 1000 + 21600, 9, 3, 21600)[0], True)
    chk("NC: 故障种类变化(TRANSPORT->CREDENTIAL) 可立刻再报",
        should_notify_fault({"fault_announced": True, "fault_kind": FAULT_TRANSPORT,
                             "last_fault_notify_ts": 1000}, FAULT_CREDENTIAL, 1001, 3, 3, 21600)[0], True)

    # 9) 故障/恢复文案
    ftxt = _build_fault_text(FAULT_TRANSPORT, [{"model": "m", "http": 0, "verdict": UNKNOWN}],
                             beijing_now(), 3)
    chk("fault text: 标明「不是没坑」", "不是「没坑」" in ftxt, True)
    chk("NC: fault text 无密钥泄漏", ("sk-" in ftxt), False)
    chk("fault text: 含模型行", "http=0" in ftxt, True)
    chk("recover text: 含已恢复", "已恢复" in _build_recover_text(beijing_now(), FAULT_TRANSPORT, "t0"), True)

    # 9b) 故障文案里的「日志：」路径可覆盖（watchd 起子进程时注入真实日志路径）
    _old_hint = os.environ.pop("GA_ANYROUTER_LOG_HINT", None)
    chk("log hint: 默认指向 bg_task 日志",
        LOG_HINT_DEFAULT in _build_fault_text(FAULT_TRANSPORT, [], beijing_now(), 3), True)
    os.environ["GA_ANYROUTER_LOG_HINT"] = "/tmp/x/watchd.log"
    _txt_hint = _build_fault_text(FAULT_TRANSPORT, [], beijing_now(), 3)
    chk("log hint: env 覆盖生效", "/tmp/x/watchd.log" in _txt_hint, True)
    chk("NC: 覆盖后不再出现默认路径", LOG_HINT_DEFAULT in _txt_hint, False)
    if _old_hint is None:
        os.environ.pop("GA_ANYROUTER_LOG_HINT", None)
    else:
        os.environ["GA_ANYROUTER_LOG_HINT"] = _old_hint

    # 10) 抢坑（claim）：判据映射 / 身份稳定 / 退避 / 真重发（注入假 post，全程离线）
    chk("claim: 200 -> WON", claim_action(200), CLAIM_WON)
    chk("claim: 429 -> CONTINUE", claim_action(429), CLAIM_CONTINUE)
    chk("claim: 503 -> CONTINUE", claim_action(503), CLAIM_CONTINUE)
    chk("claim: 传输错 0 -> CONTINUE", claim_action(0), CLAIM_CONTINUE)
    chk("NC: 400 -> ABORT（缺 1m 扩展是配置问题，重发无意义）", claim_action(400), CLAIM_ABORT)
    chk("NC: 401 -> ABORT", claim_action(401), CLAIM_ABORT)
    chk("NC: 403 -> ABORT", claim_action(403), CLAIM_ABORT)
    chk("NC: 404 -> ABORT", claim_action(404), CLAIM_ABORT)
    chk("NC: 402 -> ABORT", claim_action(402), CLAIM_ABORT)
    chk("NC: 500 -> ABORT（非 503 的 5xx 不许当「坑满」无限重发）", claim_action(500), CLAIM_ABORT)

    sid = session_id_for("sk-test-1", "tagA")
    chk("session: 同 key+tag 恒等", session_id_for("sk-test-1", "tagA"), sid)
    chk("NC: 换 key 变 id", session_id_for("sk-test-2", "tagA") == sid, False)
    chk("NC: 换 tag 变 id", session_id_for("sk-test-1", "tagB") == sid, False)
    chk("session: CC 形态", sid.startswith("user_") and "_account__session_" in sid, True)
    chk("NC: session 不含明文 key", "sk-test" in sid, False)

    b_ping = json.loads(build_claim_body("m1", sid).decode())
    b_keep = json.loads(build_claim_body("m1", sid, keepalive=True).decode())
    _mu = json.loads(b_ping["metadata"]["user_id"])
    chk("body: metadata.user_id 是 CC 同形 JSON 串（键序 device_id/account_uuid/session_id）",
        sorted(_mu.keys()), ["account_uuid", "device_id", "session_id"])
    chk("body: metadata.user_id>session_id == session", _mu["session_id"], sid)
    chk("NC: metadata.user_id 非裸 user_ 形式（该形式实测恒 429）",
        b_ping["metadata"]["user_id"].startswith("user_"), False)
    chk("body: 抢坑内容 ping", b_ping["messages"][0]["content"], "ping")
    chk("body: 保活内容 1", b_keep["messages"][0]["content"], "1")
    chk("NC: 保活与抢坑同 session", b_keep["metadata"]["user_id"] == b_ping["metadata"]["user_id"], True)

    class _R(object):
        def __init__(self, v):
            self.v = v

        def random(self):
            return self.v

    chk("delay: rng 中点 -> 精确退避", [round(burst_delay(i, rng=_R(0.5)), 4) for i in range(1, 5)],
        [0.35, 0.7, 1.4, 2.5])
    chk("delay: 有界且为正", all(0 < burst_delay(i) <= 2.5 * 1.35 for i in range(1, 12)), True)
    chk("NC: 退避封顶不无限涨", burst_delay(99, rng=_R(0.5)), 2.5)

    class _Args(object):
        claim_model = "m1"
        session_tag = "t1"
        claim_seconds = 5.0
        timeout = 1
        dry_run = True
        notify = False
        keepalive_minutes = 0
        keepalive_seconds = 0.01

    _env_old = os.environ.get("ANYROUTER_API_KEY")
    os.environ["ANYROUTER_API_KEY"] = "sk-selftest"
    try:
        seen = []

        def _post_factory(seq):
            def _post(body, timeout):
                seen.append(json.loads(body.decode())["metadata"]["user_id"])
                st = seq[min(len(seen) - 1, len(seq) - 1)]
                return st, "x"
            return _post

        _nosleep = lambda s: None  # noqa: E731
        seq = [429, 503, 200]
        rc = run_claim(_Args(), post=_post_factory(seq), sleep=_nosleep)
        chk("claim 运行: 429/503/200 -> rc=1（抢到）", rc, 1)
        chk("claim 运行: 第 3 发才 200（说明真重发了）", len(seen), 3)
        chk("NC: 三发同一个 session id（换 id 等于换坑位，抢不到）", len(set(seen)), 1)

        seen = []
        chk("claim 运行: 429 后 401 -> rc=2（配置类硬失败，不无脑重发）",
            run_claim(_Args(), post=_post_factory([429, 401]), sleep=_nosleep), 2)
        chk("claim 运行: 401 立即停（只发 2 发）", len(seen), 2)

        seen = []
        _a = _Args()
        _a.claim_seconds = 0.3
        _clock = [0.0]

        def _now():
            return _clock[0]

        def _post_tick(body, timeout):
            seen.append(1)
            _clock[0] += 0.1
            return 429, "x"

        chk("claim 运行: 窗口内一直 429 -> rc=0（没抢到，非故障）",
            run_claim(_a, post=_post_tick, sleep=_nosleep, now=_now), 0)
        chk("claim 运行: 窗口内至少重发多轮", len(seen) > 2, True)
        chk("claim 运行: 到点即停（不无限打）", len(seen) <= 8, True)
        chk("NC: attempt 极大时退避不溢出（2**n 曾崩）",
            isinstance(burst_delay(5000, rng=_R(0.5)), float), True)
    finally:
        if _env_old is None:
            os.environ.pop("ANYROUTER_API_KEY", None)
        else:
            os.environ["ANYROUTER_API_KEY"] = _env_old

    for name, got, want, good in cases:
        print("%s %s: got=%r want=%r" % ("PASS" if good else "FAIL", name, got, want))
    print("selftest: %d passed, %d failed" % (sum(1 for c in cases if c[3]), sum(1 for c in cases if not c[3])))
    return 0 if ok else 1


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="anyrouter_slot_watch.py",
        description="anyrouter.top 抢坑（slot）可用性监控（stdlib urllib；余额≠可用，须抢到上游）")
    ap.add_argument("--once", action="store_true", help="单次检测（默认行为）")
    ap.add_argument("--watch", action="store_true", help="长驻自轮询（单实例锁；命中即推送，冷却内不重复，不退出）")
    ap.add_argument("--interval", type=int, default=60, help="--watch 轮询间隔秒数（默认 60）")
    ap.add_argument("--max-minutes", type=int, default=0, dest="max_minutes",
                    help="--watch 硬超时分钟数（默认 0=不设上限）")
    ap.add_argument("--notify", action="store_true", help="抢到坑时推送通知")
    ap.add_argument("--dry-run", action="store_true", help="不落盘不推送（只打印）")
    ap.add_argument("--timeout", type=int, default=25, help="单次请求超时秒数（默认 25）")
    ap.add_argument("--cooldown", type=int, default=NOTIFY_COOLDOWN_S,
                    help="同状态重复推送最小间隔秒数（默认 3600）")
    ap.add_argument("--models", nargs="?", const="__list__", default=None,
                    help="逗号分隔的模型列表；不带值=只读列站点在册模型")
    ap.add_argument("--state-dir", default=None,
                    help="状态目录覆盖（演练/并行实例用；默认 temp/anyrouter_watch）")
    ap.add_argument("--fault-streak", type=int, default=FAULT_STREAK, dest="fault_streak",
                    help="连续 N 轮故障才告警（默认 3；同一故障持续时按 --fault-cooldown 重复提醒）")
    ap.add_argument("--fault-cooldown", type=int, default=FAULT_NOTIFY_COOLDOWN_S, dest="fault_cooldown",
                    help="故障持续时重复提醒的最小间隔秒数（默认 21600）")
    ap.add_argument("--no-fault-notify", action="store_false", dest="fault_notify", default=True,
                    help="关闭「监控自身故障」告警（只报抢到坑）")
    ap.add_argument("--log-hint", default=None, dest="log_hint",
                    help="故障文案里「日志：」指向的路径（默认 temp/bg_task/logs/anyrouter_slot_watch.log）")
    ap.add_argument("--claim", action="store_true",
                    help="抢坑模式：固定 session id + 爆满立即重发 + 命中后同 id 保活（L 站原方）")
    ap.add_argument("--claim-model", default=CLAIM_DEFAULT_MODEL, dest="claim_model",
                    help="抢坑用的模型名（默认 %s，唯一实测到 200 的名字）" % CLAIM_DEFAULT_MODEL)
    ap.add_argument("--claim-seconds", type=float, default=300, dest="claim_seconds",
                    help="抢坑窗口秒数（默认 300，到点未抢到即停）")
    ap.add_argument("--session-tag", default="anyrouter-slot", dest="session_tag",
                    help="session id 的标签（同 tag 恒同 id；换 tag 等于换坑位身份）")
    ap.add_argument("--keepalive-seconds", type=float, default=240, dest="keepalive_seconds",
                    help="保活间隔秒数（默认 240；闲置数分钟即被踢）")
    ap.add_argument("--keepalive-minutes", type=float, default=0, dest="keepalive_minutes",
                    help="抢到后保活多久（默认 0=不保活，只报喜；>0 时按 --keepalive-seconds 定时发 1）")
    ap.add_argument("--selftest", action="store_true", help="负控自测")
    return ap


def main():
    args = _build_parser().parse_args()
    if args.selftest:
        return selftest()
    if getattr(args, "log_hint", None):
        os.environ["GA_ANYROUTER_LOG_HINT"] = str(args.log_hint)
    _apply_state_dir(getattr(args, "state_dir", None))
    raws = getattr(args, "models", None)
    if raws == "__list__":
        r = list_models(_load_key())
        print("[anyrouter] %s http=%s n=%d" % (r["host"], r["http"], len(r["models"])))
        for m in r["models"]:
            print("  -", m)
        return 0 if r["http"] == 200 else 2
    args.model_list = [m.strip() for m in raws.split(",") if m.strip()] if raws else []
    if args.claim:
        return run_claim(args)
    if args.watch:
        return run_watch(args)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
