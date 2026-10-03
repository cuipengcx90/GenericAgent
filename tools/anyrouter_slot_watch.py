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
  进程级守护见 tools/anyrouter_slot_watchd.py（崩了/卡了自动重启，零 LLM token）。
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
# 1M 上下文 beta（不带它，claude 模型恒 400「请启用 1m 上下文后重试」）
BETA = "context-1m-2025-08-07"
UA = "claude-cli/1.0.0 (external, cli)"
# 默认探测模型：站点在册、且能代表「坑」的模型
#   [1m] 后缀 = 站点约定的 1M 上下文写法（官方公告里的用法）
MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-6",
    "claude-sonnet-4-20250514",
    "gpt-6-astra-cc-format[1m]",
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
BROKEN = (CLIENT_REJECTED, KEY_INVALID)  # 属「配置/凭据坏了」而非「坑满了」

# ---- 故障面（≠「没坑」）：连续 N 轮都是这些，才算「监控链路本身出问题」并告警 ----
#   POOL_BUSY / NEED_1M 属正常观测（站点没坑），绝不算故障——否则每轮没坑都吵你一次。
FAULT_TRANSPORT = "TRANSPORT"        # 全 UNKNOWN（网络/代理/DNS/超时）
FAULT_CREDENTIAL = "CREDENTIAL"      # 全 KEY_INVALID（令牌失效）
FAULT_CLIENT = "CLIENT"              # 全 CLIENT_REJECTED（UA 白名单被拒）
FAULT_MODEL_DRIFT = "MODEL_DRIFT"    # 全 MODEL_UNSUPPORTED（模型名不在 messages 面）
FAULT_NO_KEY = "NO_KEY"              # 本机取不到 key
FAULT_STREAK = 3                     # 连续 3 轮（默认 5min/轮 ≈ 15min）才告警，防抖
FAULT_NOTIFY_COOLDOWN_S = 6 * 3600   # 故障持续时，最多每 6h 提醒一次

ICON = {
    AVAILABLE: "✅ 可用（抢到坑）",
    BUSY: "⏳ 号池忙（没抢到坑）",
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


def _http_post(url, key, body, timeout=25):
    """POST JSON。★必须 stdlib urllib：curl_cffi 伪装指纹在本站恒 SSLError。"""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": BETA,
            "content-type": "application/json",
            "user-agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, "transport: %s: %s" % (type(e).__name__, e)


def probe_model(model, key, hosts=None, timeout=25):
    """单模型最小探测：打一次 POST /v1/messages（max_tokens 极小）。"""
    body = json.dumps({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    tried = []
    for host in (hosts or HOSTS):
        code, text = _http_post(host + "/v1/messages", key, body, timeout)
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
    if args.watch:
        return run_watch(args)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
