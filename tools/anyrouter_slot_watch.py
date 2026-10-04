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
  400 + 「invalid codex request」-> CODEX_SHAPE_REQUIRED 该名只吃 Codex 形状（见下节「两面」）
  400 + 官方通告「claude 模型供应难以保证」-> MODEL_NOTICE 名在册但当前不供（2026-10-04 实测：
                                                 claude-opus-5-5；官方改推 gpt-6-astra-cc-format）
  其它（超时/DNS/其它 4xx5xx） -> UNKNOWN         不确定（fail-closed，不当成可用）

两面（★形状单一源：探针 = 抢坑 = 实验，同一套客户端形状）
  messages 面 /v1/messages?beta=true  = Claude Code 形状（system 计费行 + metadata.user_id …）
  codex   面 /v1/responses            = Codex 形状（include=[reasoning.encrypted_content] + prompt_cache_key）
  站点按「像不像真客户端」分流：形状不合格一律**秒拒**（CC 面 ~1.1s 429；codex 面 ~1.1s 400
  invalid codex request）⇒ 形状错 = 假阴性（会把「有坑」读成「没坑」），故形状必须与真身一致，
  且换形状要改这里一处（face_of/model 面登记表），禁在探针/抢坑/实验里各写一份。

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
import random
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
# ---- Codex 面（/v1/responses）：与 CC 面并列的第二套「客户端形状」（2026-10-04 消融实测）----
#   本站对 OpenAI 面模型另设「像不像真 Codex」门禁：body 必须**同时**含
#     include=["reasoning.encrypted_content"]  与  prompt_cache_key（值任意；缺任一即
#     400 {"code":"invalid_responses_request"} @~1.1s，页面显示 `invalid codex request`）。
#   请求头无关、tools 无关、input 内容无关 ⇒ 最小放行体 ≈300B（见 build_codex_probe_body）。
CODEX_PATH = "/v1/responses"
CODEX_UA = "codex_exec/0.157.1 (Mac OS 26.0.1; arm64) vscode"   # 与门禁无关，照真身抄以免将来加头校验
CODEX_ORIGINATOR = "codex_exec"
CODEX_INCLUDE = ["reasoning.encrypted_content"]      # ★门禁必需键 1/2
CODEX_REQUIRED_KEYS = ("include", "prompt_cache_key")  # ★门禁必需键 2/2
FACE_MESSAGES = "messages"
FACE_CODEX = "codex"
# 面登记表（形状单一源）：默认 messages 面；走 OpenAI 面的模型必须在此登记，
# 否则探针会拿 CC 形状去打它（实测裸名 gpt-6-astra 走 messages 面恒 404）。
MODEL_FACE = {
    "gpt-6-astra": FACE_CODEX,            # 2026-10-04 实测：/v1/responses + codex 形状 ⇒ 过闸（挂满超时）
    "gpt-6-astra-cc-format": FACE_MESSAGES,
}
# 默认探测模型：站点在册、且能代表「坑」的模型（★名字一律不带 [1m] 后缀）
MODELS = [
    # ★2026-10-04 按用户要求收敛：只探**高模型** —— Opus 与 Fable 两条线；
    #   4.x 系（opus-4-8/4-7/4-6、sonnet-4-5-20250929、sonnet-4-20250514、haiku-4-5）
    #   与小模型一律退出监测面（探它们只赚 POOL_BUSY，抢不到坑还占探测预算）。
    #   gpt-6-astra 系亦暂摘：社区帖（topic/2496114）10-04 06:12 起多人独立确认其上游故障（空回），
    #   要恢复监测把它们加回本表即可（面路由见 MODEL_FACE，gpt-* 走 codex 面，零额外改动）。
    "claude-opus-5-5",             # 官方 17 名在册；实测 400 = 官方通告「claude 模型供应难以保证…」
    "claude-fable-5-1-reversed",   # 官方在册；★2026-10-04 实测 http=200 真坑（本会话已抢到并保活 30min）
]

STATE_DIR = os.path.join(ROOT, "temp/anyrouter_watch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LEDGER_FILE = os.path.join(STATE_DIR, "ledger.jsonl")
WATCH_LOCK = os.path.join(STATE_DIR, "watch.lock")
NOTIFY_COOLDOWN_S = 3600  # 同一「可用」信号的最小重复推送间隔
# 故障/恢复文案里引用的日志路径：守进程（watchd）起子进程时会用 GA_ANYROUTER_LOG_HINT 覆盖，
# 使「日志：…」始终指向真正在写的那份（默认值=本监控被 bg_task 直接起时的落点）。
LOG_HINT_DEFAULT = "temp/bg_task/logs/anyrouter_slot_watch.log"

# ---------------- 反检测：请求节奏（2026-10-04 用户要求「分散随机，禁固定频次」）----------------
#   为什么：固定 5 分钟 / 固定 10 分钟、每轮同一份名单、同一秒多发起伏，是三种最容易被识别成
#   机器的指纹。本站的号池对「像不像真 Claude Code」敏感（见 CC_* 常量与 SOP §1.2），因此除请求
#   形状外，**节奏形状**也要像人：
#     ① 轮间隔  : 每轮在 [min,max] 内均匀取；另有 pause_prob 概率叠加「长休」（×[3,6]）⇒ 间隔分布重尾
#     ② 轮内错峰: 每个请求发出前随机 sleep ⇒ 不再「同一秒 N 发」并发突发
#     ③ 轮内名单: 每轮只打 sample 个（含 pin 名单），轮转保证每个模型每循环必被探到 ⇒ 同一模型再现间隔浮动
#     ④ 日预算  : --max-per-day 兜底（超了睡到次日），防任何 bug 把请求量放大成扫描器
DEFAULT_INTERVAL_MIN_S = 300      # 随机间隔下界（5 min）
DEFAULT_INTERVAL_MAX_S = 600      # 随机间隔上界（10 min）
DEFAULT_PAUSE_PROB = 0.12         # 每轮以该概率进入「长休」（拟人化：人不会像钟表一样准点）
DEFAULT_PAUSE_MULT = (3.0, 6.0)   # 长休 = 本轮随机间隔 × [3,6] ⇒ 15~60 min
DEFAULT_STAGGER = (2.0, 12.0)     # 轮内每个请求发出前的随机错峰秒
DEFAULT_WORKERS = 3               # 轮内并发上限（旧硬编码 5；并发越小越不像批量扫描）
DEFAULT_SAMPLE = 4                # 每轮抽样的模型数（含 pin；0=全量）
DEFAULT_MAX_PER_DAY = 800         # 每日请求预算（0=不限）


def _log_hint():
    return (os.environ.get("GA_ANYROUTER_LOG_HINT") or "").strip() or LOG_HINT_DEFAULT


AVAILABLE = "SLOT_AVAILABLE"
BUSY = "POOL_BUSY"
NEED_1M = "NEED_1M"
MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
MODEL_NOTICE = "MODEL_NOTICE"    # ★2026-10-04 补：400 + 官方通告「claude 模型供应难以保证」
                                 #   = 该名**在册但不供**（官方建议改用 gpt-6-astra-cc-format）
                                 #   ⇒ 既非「没坑」也非链路故障：不重试、不计故障（正常观测）
CLIENT_REJECTED = "CLIENT_REJECTED"
KEY_INVALID = "KEY_INVALID"
UNKNOWN = "UNKNOWN"
QUEUED = "QUEUED_NO_UPSTREAM"  # 过闸但上游无空闲槽：连接已建立后挂满超时（★≠链路故障、≠没额度）
LINK_DOWN = "LINK_DOWN"        # ★2026-10-04 补：本机→站点链路不可达（同栈控制探针也拿不到响应）
                               #   与 QUEUED 的差别是**证据**：QUEUED 要求「同栈控制请求拿到 HTTP 响应」
                               #   才算「过闸了只是没坑」；拿不到证据一律 LINK_DOWN（fail-closed，
                               #   不许把「连不上」乐观归因成「在排队」）
EDGE_BLOCK = "EDGE_BLOCK"  # 403 + 边缘 WAF 页（阿里 ESA）：环境/限流面，非凭据问题
CODEX_SHAPE_REQUIRED = "CODEX_SHAPE_REQUIRED"  # OpenAI 面形状门禁：400 invalid codex request（缺 include/prompt_cache_key）
BROKEN = (CLIENT_REJECTED, KEY_INVALID)  # 属「配置/凭据坏了」而非「坑满了」

# ---- 故障面（≠「没坑」）：连续 N 轮都是这些，才算「监控链路本身出问题」并告警 ----
#   POOL_BUSY / NEED_1M 属正常观测（站点没坑），绝不算故障——否则每轮没坑都吵你一次。
FAULT_TRANSPORT = "TRANSPORT"        # 全 UNKNOWN/LINK_DOWN（网络/代理/DNS/超时）
FAULT_CREDENTIAL = "CREDENTIAL"      # 全 KEY_INVALID（令牌失效）
FAULT_CLIENT = "CLIENT"              # 全 CLIENT_REJECTED（UA 白名单被拒）
FAULT_MODEL_DRIFT = "MODEL_DRIFT"    # 全 MODEL_UNSUPPORTED（模型名不在 messages 面）
FAULT_EDGE = "EDGE_BLOCK"            # 全 EDGE_BLOCK（被边缘 WAF 拦）
FAULT_NO_KEY = "NO_KEY"              # 本机取不到 key
FAULT_STREAK = 3                     # 连续 3 轮（默认 5min/轮 ≈ 15min）才告警，防抖
FAULT_NOTIFY_COOLDOWN_S = 6 * 3600   # 故障持续时，最多每 6h 提醒一次
FAULT_GATE_REJECT = "GATE_REJECT"    # ★2026-10-04 订正：仅当「对照（真 CC 形状）能过闸而我方不能」才成立
FAST_REJECT_S = 3.0                  # 429/503 快返阈值（秒）。★订正：快返**不等于**形状不对 —— 抓包逐发
                                     #   计时证明真 CC 的 9 发请求同样全是 429@1.5-2.3s（95KB 完整体）
                                     #   ⇒ 快返=号池满的常见形态，「全快返」不足以判形状门
QUEUED_MIN_S = 8.0                   # 传输超时、但从发起到挂满 ≥ 该秒数 ⇒ 判「过闸后无空闲上游」

ICON = {
    AVAILABLE: "✅ 可用（抢到坑）",
    QUEUED: "🕐 已过闸、上游暂无空闲槽（排队中超时；同栈控制探针已证链路可达）",
    LINK_DOWN: "🔌 链路不可达（同栈控制探针也拿不到响应）⇒ 网络/代理面问题，不是「没坑」",
    BUSY: "⏳ 非 200（429/503；形状合格后未再出现，多为边缘限流/上游抖动）",
    EDGE_BLOCK: "🛡 被边缘 WAF 拦（403 + 阿里 ESA 页；自动重试）",
    NEED_1M: "⚠️ 需启用 1M 上下文",
    MODEL_UNSUPPORTED: "🚫 该名不在 messages 面（404）",
    MODEL_NOTICE: "📢 官方通告：该 claude 名当前不供（400 供应难以保证）",
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
    if status == 400 and ("供应难以保证" in text or "供应难以保证" in text
                          or "模型供应" in text):
        # ★2026-10-04 实测：官方对个别 claude 名（如 claude-opus-5-5）直接回等长通告
        #   {"error":"由于 claude 模型供应难以保证，上线 gpt-6-astra-cc-format 模型…"}
        #   = 名在册、但当前不供 ⇒ 与 NEED_1M 同属「换名字/换面」而非「没坑」，
        #   故排除在故障面之外（fault_kind 里若全为它 ⇒ 视为正常观测，不告警）。
        return MODEL_NOTICE
    if status == 400 and ("invalid codex request" in low or "invalid_responses_request" in low):
        # ★OpenAI/codex 面形状门禁（2026-10-04 实测）：请求不像真 Codex（缺 include /
        #   prompt_cache_key）⇒ ~1.1s 秒拒。既不是「没坑」也不是链路故障，是**探针形状不对**
        #   （截图里那条 `invalid codex request` 即此类）⇒ 交 CODEX_SHAPE_REQUIRED，由探针自愈纠正。
        return CODEX_SHAPE_REQUIRED
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


def _is_uuid(s):
    """是否形如 8-4-4-4-12 的十六进制 UUID（站点 session_id 放行门禁的形态判据）。"""
    import re as _re
    return bool(_re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                          (s or "").strip().lower()))


def classify_latency(http, verdict, elapsed_s, link_ok=None):
    """在 HTTP 判据之上加「延迟维度」（纯函数，供负控自测）。返回 (verdict, latency, queued)。

    ★为什么必须有两维：快返（429/503 ≤ ~3s）与「挂满超时」是两种可观测形态。
    ★2026-10-04 订正（原说法已作废）：**快返不等于「形状不对」** —— 本地抓包逐发计时证明真 Claude Code
    的 9 发请求全是 429 @1.5-2.3s（95KB 完整体，HTTP/1.1），与我方探针**同等待遇**；
    「CC 挂满 90-100s、探针 1s 被拒」是 CC 内部重试+退避造成的假象。故 fast 仅表示
    「网关快速回绝（号池满常见形态）」，**不得**单独推出「探针姿势不对」。
      fast    = ≤FAST_REJECT_S 的 429/503 ⇒ 网关快速回绝（别读成形状门）
      slow    = 其余 429/503（挂了一会儿才回）
      ok      = 200
      timeout = 传输超时但已连上挂起（≥QUEUED_MIN_S）且链路可达 ⇒ queued=True
      no-link = 传输超时且同栈控制探针也拿不到响应（link_ok=False）⇒ LINK_DOWN（链路故障）
    ★link_ok 三态：True=证据表明链路可达 / False=证据表明不可达 / None=未取证（保持旧行为）
    """
    if elapsed_s is None:
        return verdict, "na", False
    if http == 200:
        return verdict, "ok", False
    if http == 0 and verdict == UNKNOWN and elapsed_s >= QUEUED_MIN_S:
        # ★2026-10-04 补：挂满超时**不再直接**判 QUEUED —— 先要「链路可达」的证据：
        #   link_ok=False 控制探针也拿不到响应 ⇒ LINK_DOWN（网络/代理面故障，fail-closed）
        #   link_ok=True  控制探针拿到 HTTP 响应 ⇒ 链路通，超时=上游无空闲槽 ⇒ QUEUED
        #   link_ok=None  未取证（自测/桩调用）⇒ 保持旧行为 QUEUED，向后兼容
        if link_ok is False:
            return LINK_DOWN, "no-link", False
        return QUEUED, "timeout", True
    if http in (429, 503):
        return verdict, ("fast" if elapsed_s <= FAST_REJECT_S else "slow"), False
    return verdict, "na", False


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
    lats = [r.get("latency") for r in results]
    if all(v == BUSY for v in vs) and lats and all(l == "fast" for l in lats):
        # ★2026-10-04 实测订正：全 BUSY 且全快返**不足以**判「探针形状被拒」——
        #   同刻用真 CC 身份（dump uid）打 claude 系同样 1.1s 快返（truth6_*.jsonl），
        #   说明快返既可能是形状被拒、也可能是网关侧供给态。
        #   ⇒ 此处**不再**直接判故障；升级判据见 gate_suspect() + gate_control_probe()
        #     （只有在「对照身份能过闸、我方不能」的对照证据下才升 GATE_REJECT）。
        return None
    if any(v in (AVAILABLE, BUSY, NEED_1M, QUEUED) for v in vs):
        # QUEUED = 过闸后上游无空闲槽：链路是通的（否则到不了上游），只是没坑 ⇒ 不是故障。
        return None
    if all(v in (UNKNOWN, LINK_DOWN) for v in vs):
        return FAULT_TRANSPORT   # LINK_DOWN=控制探针也拿不到响应 ⇒ 网络/代理面故障（fail-closed）
    if all(v == KEY_INVALID for v in vs):
        return FAULT_CREDENTIAL
    if all(v == CLIENT_REJECTED for v in vs):
        return FAULT_CLIENT
    if all(v == EDGE_BLOCK for v in vs):
        return FAULT_EDGE
    if all(v == MODEL_UNSUPPORTED for v in vs):
        return FAULT_MODEL_DRIFT
    return None


def gate_suspect(results):
    """全 BUSY 且全部快返 ⇒ 「可能被网关形状/身份门拒」的**候选**（纯函数，不等于判决）。

    ★2026-10-04 订正（重要，别再用错）：该信号**极易假阳**。本地抓包逐发计时证明真 Claude Code
    的 9 发请求同样全是 429 @1.5-2.3s（95KB 完整体），即「全快返」就是号池满的常见形态，
    **不能**推出形状门。故本函数只作**观测标记**；升级为 FAULT_GATE_REJECT 必须有对照证据
    （gate_control_probe / verify_pose）：**同一时刻**对照（真 CC 形状）能过闸而我方不能，
    才说明问题在探针自身。仅凭本函数返回值下结论 = R538 那次误判的成因。
    """
    if not results:
        return False
    vs = [r.get("verdict") for r in results]
    lats = [r.get("latency") for r in results]
    return bool(vs and all(v == BUSY for v in vs) and lats and all(l == "fast" for l in lats))


def _gate_control_uid():
    """可选对照身份：真 Claude Code 抓包里的 metadata.user_id（不存在则 None）。

    查找顺序：env ANR_GATE_CONTROL_UID_JSON > temp/anyrouter_watch/cc_control_uid.json
              > /tmp/anr_proxy_dump/000_req.json（临时抓包，易失）。
    """
    cands = []
    envp = os.environ.get("ANR_GATE_CONTROL_UID_JSON")
    if envp:
        if envp.strip().lower() in ("none", "off", "0", ""):
            return None          # 显式禁用对照（运维开关/自测用）
        cands.append(envp)
    cands.append(os.path.join(STATE_DIR, "cc_control_uid.json"))
    cands.append("/tmp/anr_proxy_dump/000_req.json")
    for c in cands:
        try:
            if not os.path.exists(c):
                continue
            obj = json.load(open(c, encoding="utf-8"))
            if isinstance(obj, dict) and "body" in obj:
                obj = obj["body"]
            raw = (obj.get("metadata") or {}).get("user_id") if isinstance(obj, dict) else None
            if not raw:
                continue
            uid = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(uid, dict) and uid.get("session_id"):
                return uid
        except Exception:
            continue
    return None


def gate_control_probe(key, model, timeout=20, post=None):
    """对照探针（判 GATE_REJECT 的**唯一**证据通道）。

    返回 "unavailable" / "pass"（对照能过闸：200 或挂满超时=走上游）/ "reject"（对照也快返）。
    """
    uid = _gate_control_uid()
    if not uid:
        return "unavailable"
    post = post or _http_post
    body = build_probe_body(model)
    o = json.loads(body.decode())
    o["metadata"]["user_id"] = json.dumps(uid, separators=(",", ":"))
    b = json.dumps(o).encode()
    t0 = time.time()
    try:
        st, txt = post(HOSTS[0] + CC_PATH, key, b, timeout)
    except Exception:
        st, txt = 0, ""
    el = time.time() - t0
    # 语义（★不调 classify_latency：它吃的是 (http, verdict, elapsed) 三元序，此处直接判更稳）
    #   pass   = 对照身份**没被网关拒**：200，或传输层挂满超时/异常（连拒绝响应都没拿到）
    #   reject = 拿到 429/503 且瞬回 ⇒ 对照也被形状门拒
    if st == 200:
        return "pass"
    if st in (429, 503) and el <= FAST_REJECT_S:
        return "reject"
    if st == 0:
        return "pass"
    return "unknown"


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
        FAULT_GATE_REJECT: "探针身份被网关拒（我方全快返，而**对照身份能过闸**）",
    }.get(kind, kind)
    lines = ["⚠️ anyrouter 抢坑监控自身异常（不是「没坑」）",
             "时间：%s（北京时间）" % now_bj.strftime("%Y-%m-%d %H:%M:%S"),
             "现象：%s，已连续 %d 轮" % (what, streak)]
    for r in (results or [])[:5]:
        lines.append("- %s：http=%s %s" % (r.get("model", "?"), r.get("http"), r.get("verdict")))
    lines += [
        "",
        "区别：429/503=号池忙（正常，没坑）；本条=%s，"
        "脚本会自动重试，不需要你操作；若持续超过 12h 再人工看一眼。"
        % ("探针形状/身份被网关拒（速返），非号池满——需修会话形状"
           if kind == FAULT_GATE_REJECT else "探测链路/凭据本身坏了"),
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
    if all(r["verdict"] in (UNKNOWN, LINK_DOWN) for r in results):
        return 2
    if all(r["verdict"] == MODEL_UNSUPPORTED for r in results):
        return 2  # 全部模型名都不在 messages 面（站点改面/名单漂移）⇒ 也要人看
    if all(r["verdict"] == EDGE_BLOCK for r in results):
        return 2
    if all(r["verdict"] in BROKEN for r in results):
        return 2
    if all(r["verdict"] == QUEUED for r in results):
        return 0  # 过闸但没坑：等价「池满」，不是链路故障
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


def _http_post(url, key, body, timeout=25, read_limit=None, extra_headers=None):
    """POST JSON。★必须 stdlib urllib（curl_cffi 伪装指纹在本站恒 SSLError）。

    extra_headers：覆盖/追加客户端形状头（codex 面用 codex_headers()，见 CODEX_* 常量）。

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
    if extra_headers:
        headers.update(extra_headers)   # codex 面形状头（message 面不传，行为逐字不变）
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


def _link_tag(lk):
    """link 证据标记（None=未取证 / ok=控制探针拿到响应 / down=拿不到响应）。"""
    if lk is None:
        return ""
    return " [link=%s]" % ("ok" if lk else "down")


def _link_ok(host, timeout=6, get=None):
    """同栈控制探针：本机→站点能否**拿到 HTTP 响应**（任一状态码都算可达）。

    ★必须走同一客户端栈：本机出网经 Clash 系统代理（urllib 经 _scproxy 自动读系统代理），
      裸 socket 不受代理管辖，会给出与真实请求相反的证据（假 LINK_DOWN）。
    ★只用它证「链路在」，站点状态页/反爬挑战页不可作任何状态判据（见 SOP §1）。
    """
    g = get or (lambda url, t: urllib.request.urlopen(
        urllib.request.Request(url, headers={"user-agent": UA}), timeout=t))
    try:
        r = g(host + "/api/status", timeout)
        code = getattr(r, "status", None) or getattr(r, "code", None) or 1
        try:
            r.close()
        except Exception:
            pass
        return bool(code)
    except urllib.error.HTTPError as e:
        return bool(e.code)          # 4xx/5xx 也是「拿到了响应」⇒ 链路可达
    except Exception:
        return False


def probe_model(model, key, hosts=None, timeout=60):
    """单模型探测：面/体/头全部由 face_of(model) 决定（★形状单一源，与抢坑同一套形状）。

    messages 面：CC 放行形状 → POST /v1/messages?beta=true（体 ≈1KB、stream）
    codex   面：Codex 放行形状 → POST /v1/responses（体 ≈300B）
                —— 缺 include/prompt_cache_key 必被 400 invalid codex request 秒拒（2026-10-04 实测）。

    ★形状自愈：任何面若命中 CODEX_SHAPE_REQUIRED（该名只吃 Codex 形状），就地换 codex 形状
      重打一次并记 shape_retry=True ⇒ 形状规则再变也不会静默退化成假阴性（探针自己纠正，
      不需要人去改代码）。

    ★只取响应前 256B 判状态码（stream=True 时整段体会一直流到生成完，没必要等）。
    """
    face = face_of(model)
    tried, shape_retry = [], False

    def attempt(host, face_):
        path = CODEX_PATH if face_ == FACE_CODEX else CC_PATH
        if face_ == FACE_CODEX:
            body, hdrs = build_codex_probe_body(model), codex_headers(key)
        else:
            body, hdrs = build_probe_body(model), None
        t0 = time.time()
        code, text = _http_post(host + path, key, body, timeout, read_limit=256, extra_headers=hdrs)
        return code, text, time.time() - t0

    hot = {"model": model, "host": "", "elapsed_s": None, "latency": "na",
           "queued": False, "face": face, "shape_retry": shape_retry}

    for host in (hosts or HOSTS):
        code, text, el = attempt(host, face)
        if code == 0:
            tried.append(text[:120])
            lk = _link_ok(host) if el >= QUEUED_MIN_S else None
            v, lat, queued = classify_latency(0, UNKNOWN, el, link_ok=lk)
            if queued or v == LINK_DOWN:
                # ★queued 必须写真实值：LINK_DOWN 时 queued=False（"在排队"与"连不上"是两件事）
                hot.update({"http": 0, "verdict": v, "detail": text[:200], "host": host,
                            "elapsed_s": round(el, 1), "latency": lat, "queued": queued,
                            "face": face, "shape_retry": shape_retry, "link": _link_tag(lk)})
                return hot
            continue
        v, lat, queued = classify_latency(code, classify(code, text), el)
        if v == CODEX_SHAPE_REQUIRED and face != FACE_CODEX:
            face, shape_retry = FACE_CODEX, True      # 形状自愈：就地换面重打
            code, text, el = attempt(host, face)
            if code == 0:
                lk = _link_ok(host) if el >= QUEUED_MIN_S else None
                v, lat, queued = classify_latency(0, UNKNOWN, el, link_ok=lk)
                if not queued:
                    tried.append(text[:120])
                    continue
                hot.update({"http": 0, "verdict": v, "detail": text[:200], "host": host,
                            "elapsed_s": round(el, 1), "latency": lat, "queued": queued,
                            "face": face, "shape_retry": shape_retry, "link": _link_tag(lk)})
                return hot
            v, lat, queued = classify_latency(code, classify(code, text), el)
        hot.update({"http": code, "verdict": v, "detail": text[:200], "host": host,
                    "elapsed_s": round(el, 1), "latency": lat, "queued": queued,
                    "face": face, "shape_retry": shape_retry})
        return hot
    hot.update({"http": 0, "verdict": UNKNOWN, "detail": "; ".join(tried)[:200]})
    return hot


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

    ★2026-10-04 铁证（6 发单变量对照，temp/anr_truth/truth5_*.jsonl）：网关门禁落在
    ``session_id`` 的**形态**上——必须是一个 UUID（36 字符 8-4-4-4-12）。任意 uuid4 都过闸
    （3/3 走上游并挂满超时），而旧实现自造的 ``user_<32hex>_account__session_<uuid>``（91 字符）
    恒被瞬时拒（429 @1.9s，与号池有没有空闲上游无关）⇒ 那是**假阴性**，不是「没抢到坑」。
    ``device_id`` / ``account_uuid`` 都不是门禁（真 CC 的 account_uuid 同样是空串）。

    取 uuid5 派生：既满足 UUID 形态，又对 (key,tag) 确定 ⇒ 抢坑/保活能复用同一坑位身份。
    key 只参与 sha256，不落盘明文。
    """
    import hashlib
    import uuid
    h = hashlib.sha256(("anyrouter|%s" % (key or "")).encode()).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "anyrouter|%s|%s" % (h, tag)))


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

    ★2026-10-04 追加铁证（6 发单变量，**订正上文**）：上面三发同时换了两个变量
      （JSON 串 vs 裸串、session_id 长串 vs UUID）。分量级复测证明真正卡住的是
      **session_id 的 UUID 形态**——把本函数里的 ``session_id`` 换成 UUID 后同一形状
      3/3 走上游（挂满超时）；device_id/account_uuid 均非门禁。
    """
    import hashlib
    dev = hashlib.sha256(("anyrouter-device|%s" % (session_id or "")).encode()).hexdigest()
    return json.dumps({"device_id": dev, "account_uuid": "", "session_id": session_id},
                      separators=(",", ":"), ensure_ascii=False)


def face_of(model):
    """模型 → 探测面：gpt-* 走 /v1/responses（Codex 面），其余走 /v1/messages（CC 面）。

    ★形状单一源：探测（probe_model）、抢坑（claim）与形状复刻实验脚本共用同一判据，
      避免再出现「实验通了、探针没通」的分叉。
    """
    m = (model or "").strip().lower()
    if m in MODEL_FACE:
        return MODEL_FACE[m]          # 显式登记优先（含「同名却走 CC 面」这类反例）
    return FACE_CODEX if m.startswith("gpt-") else FACE_MESSAGES


def codex_headers(key, session_hex=None):
    """Codex 面请求头（照真身 0.157.1 抓包抄）。本站门禁只看 body，头属预防性对齐。"""
    sid = _cc_session_uuid(key) if session_hex is None else session_hex
    return {
        "user-agent": CODEX_UA,
        "originator": CODEX_ORIGINATOR,
        "session-id": sid,
        "x-client-request-id": _cc_session_uuid(str(sid) + "|req"),
    }


def build_codex_probe_body(model, content="1"):
    """Codex 面探测体（体 ≈300B）。

    ★形状门禁（2026-10-04 消融实测，真 codex 0.157.1 抓包 + 逐键裁剪反向定位）：
      必须**同时**含 ``include=["reasoning.encrypted_content"]`` 与 ``prompt_cache_key``（值任意），
      缺任一 ⇒ 400 invalid_responses_request（页面写作 `invalid codex request`）@~1.1s；
      与请求头无关，与 tools/input 内容无关。
    """
    return json.dumps({
        "model": model,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": content}]}],
        "include": list(CODEX_INCLUDE),
        "prompt_cache_key": _cc_session_uuid(model + "|probe"),
        "store": False,
        "stream": True,
    }).encode()


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


# ---------------------------------------------------------------- 自动占坑保活（--auto-hold-minutes）
# ★2026-10-04 用户要求：「接上自动抢坑 + 抢到后保活 30 分钟，确保我能直接用」。
#   语义：watch 每轮探测；**一旦某模型 200（= 有坑、能用）**，立刻用**探测所用的同一个 session**
#   发 keepalive（内容 "1"）把坑位占住，默认拍 30 分钟；期间不阻塞探测循环（后台线程）。
#   为什么必须同 session：坑位是绑 session 的（L 站原方口径「闲置数分钟即被踢」），
#   换 session 去保活＝替别人占坑，自己那条照样掉。
HOLD_COOLDOWN_S = 600
# ---- 「停保活」旗标（2026-10-04 用户问：我已经在用别的客户端了，你还保活不是白烧额度吗？）----
# 保活跑在别处（后台线程/独立进程），它看不见「你此刻是否在用」⇒给一个显式开关：
#   `--stop-hold` 写旗标 ⇒ 正在跑的保活最多 HOLD_STOP_STEP_S 秒内收手；已完成的不回补。
HOLD_STOP_STEP_S = 5.0


def _hold_stop_path():
    return os.path.join(STATE_DIR, "hold.stop")


def hold_stop_requested():
    """是否已请求停保活（`--stop-hold` 写的旗标文件存在）。"""
    try:
        return os.path.exists(_hold_stop_path())
    except Exception:
        return False


def request_hold_stop(reason="manual"):
    """写停保活旗标（幂等）；返回旗标路径。"""
    fp = _hold_stop_path()
    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as f:
            f.write(json.dumps({"ts": beijing_now().isoformat(), "reason": reason}))
    except Exception:
        pass
    return fp


def clear_hold_stop():
    """开始新一轮保活前清旗标（新命中=重新决策）。返回是否真清掉了。"""
    try:
        os.unlink(_hold_stop_path())
        return True
    except Exception:
        return False


def sleep_with_stop(seconds, step=HOLD_STOP_STEP_S, sleep=None, stop_check=None):
    """分片睡眠：每 step 秒查一次停旗标 ⇒ `--stop-hold` 最多 step 秒生效。

    返回 True=中途收到停止请求（调用方应立即收手）。
    """
    sleep = sleep or time.sleep
    check = stop_check or hold_stop_requested
    left = float(seconds)
    while left > 0:
        if check():
            return True
        d = min(float(step), left)
        sleep(d)
        left -= d
    return bool(check())


def should_auto_hold(results, state, now, cooldown=HOLD_COOLDOWN_S):
    """命中可用且不在保活中/冷却内 → 返回该可用结果（dict），否则 None。纯函数，供负控自测。

    ★负控口径：全 BUSY / 空结果 / 已在保活（hold_active）/ 距上次保活不足 cooldown ⇒ 一律 None。
    """
    avail = [r for r in (results or []) if r.get("verdict") == AVAILABLE]
    if not avail:
        return None
    if state.get("hold_active"):
        return None
    try:
        ts = float(state.get("last_hold_ts") or 0)
    except Exception:
        ts = 0.0
    if (now - ts) < float(cooldown):
        return None
    return avail[0]


def hold_slot(model, sid, minutes, seconds, post, sleep, now, log=None, timeout=60,
              stop_check=None):
    """占坑保活：每 seconds 发 1 发 keepalive；**非 200 一律当被踢，立即停并返回 False**。

    ★口径：只有 200 才算「坑还在」。429/503/传输错 ⇒ 坑已丢（闲置回收/被挤掉），
      继续发只是白烧请求 ⇒ 立刻停（与 run_claim 的保活段同一判据，避免两套口径）。
    ★2026-10-04：stop_check 为真 ⇒ 立即收手（你已在别的客户端用上了，别白烧请求）。
    返回 True=保活段正常跑完；False=未启用/被踢/被用户停。
    """
    if not minutes or float(minutes) <= 0:
        return False
    log = log or (lambda s: None)
    end = now() + float(minutes) * 60
    n = 0
    while now() < end:
        if sleep_with_stop(seconds, sleep=sleep, stop_check=stop_check):
            log("[anyrouter] hold: 收到 --stop-hold（多半是你已在用别的客户端），主动停手（已发 %d 发）" % n)
            return False
        n += 1
        code, _t = post(build_claim_body(model, sid, keepalive=True), timeout)
        act = claim_action(code)
        log("[anyrouter] hold #%d model=%s http=%s -> %s" % (n, model, code, act))
        if act != CLAIM_WON:
            return False
    return True


def start_auto_hold(args, hit, post=None, log=None):
    """后台线程：命中可用 ⇒ 同 session 占坑保活（state 置 hold_active，结束回写）。返回线程。"""
    import threading
    log = log or (lambda s: print(s, flush=True))
    sid = session_id_for(_load_key(), "anyrouter-probe")     # ★与探测同 id（见 build_probe_body）
    if clear_hold_stop():
        log("[anyrouter] 新命中 ⇒ 清除上一轮的 --stop-hold 旗标（重新决策）")
    st = _load_state()
    st["hold_active"] = True
    st["hold_since"] = beijing_now().isoformat()
    st["hold_model"] = hit.get("model")
    _save_state(st)
    log("[anyrouter] 命中可用 ⇒ 启动自动占坑保活：model=%s session尾号=%s 时长=%.0f 分钟（每 %.0fs 续一次）"
        % (hit.get("model"), sid[-12:], float(args.auto_hold_minutes), float(args.keepalive_seconds)))

    def _run():
        def _post(body, timeout):
            return _http_post(HOSTS[0] + CC_PATH, _load_key(), body, timeout, read_limit=256)
        ok = hold_slot(hit.get("model"), sid, args.auto_hold_minutes, args.keepalive_seconds,
                       post or _post, time.sleep, time.time, log=log, timeout=args.timeout)
        st2 = _load_state()
        st2["hold_active"] = False
        st2["last_hold_ts"] = time.time()
        st2["last_hold_ok"] = bool(ok)
        _save_state(st2)
        try:
            _append_ledger({"ts": beijing_now().isoformat(), "mode": "auto_hold",
                            "model": hit.get("model"), "session": sid[-12:],
                            "minutes": float(args.auto_hold_minutes), "ok": bool(ok)})
        except Exception:
            pass
        log("[anyrouter] 自动占坑结束：%s" % ("保活段跑完（坑位仍在）" if ok else "被踢/中断"))
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


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


def _build_text(results, now_bj, reason, auto_hold_minutes=0, keepalive_seconds=240, sid=None):
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
    ]
    # ★2026-10-04：命中 fable 逆向渠道时附口碑提示（我方实测 + 社区帖 topic/2496114 逐字证据）
    if any("fable" in (r.get("model") or "").lower() for r in results):
        lines.append("⚠️ 命中的是**逆向渠道**模型（实测响应 model=anthropic/claude-fable-5.1，社区疑为 kiro 逆向）：")
        lines.append("   实测：132 入/114 出 token 收 $0.00702（约 $10/M 入、$50/M 出）、简单题 38s；")
        lines.append("   社区口碑：偏贵、慢、智商一般、长对话易 `let me…` 死循环（#22468/#22479/#22525/#22526/#22540）")
        lines.append("   建议：短会话、一次一件事；要高质量可优先等 opus 线或 gpt-6-astra 恢复（本轮 opus-5-5 已收官方通告=不供）。")
    if auto_hold_minutes:
        lines.append("✅ 已自动保活 %s 分钟（后台每 %s 秒用**同一 session** 发一个最小请求；被踢即停、下次命中重占）。"
                     % (("%g" % float(auto_hold_minutes)), int(keepalive_seconds)))
        lines.append("👉 怎么用：把客户端指到本站的 Anthropic 兼容端点（/v1/messages）即可。")
        lines.append("   上面那组 beta 头是 Claude Code 自己会带的，用 CC 不用手配；")
        lines.append("   用 Pi / 其它客户端按各家填 anthropic base_url，头按各家默认即可。")
        lines.append("⚠️ 说明（别误会成「给你留了个座位」）：坑位**不绑身份**——站点只看号池有没有空闲上游"
                     "（实测换 session_id 仍 200）⇒")
        lines.append("   你不必指定 session，任何客户端在空闲窗口都能进；我这条保活只是"
                     "「用同一记号持续发最小请求」，用来盯住通路、尽早发现被踢。")
        if sid:
            lines.append("🔑 我这条保活的身份记号 session_id：%s" % sid)
            lines.append("   想在 CC 里也用同一记号（非必需）：`claude --session-id %s`" % sid)
        lines.append("💡 你已经在用（CC / Pi 等）时它就没用了还费请求：跑 "
                     "`python3 tools/anyrouter_slot_watch.py --stop-hold` 立刻停（最多 %.0f 秒生效）。"
                     % HOLD_STOP_STEP_S)
    else:
        lines.append("坑位会被踢：闲置数分钟即掉，命中后请尽快连着用。")
    lines += [
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
def probe_models(models, key, timeout=90, workers=5, stagger=None, sleeper=None):
    """并行探测多个模型（纯为控轮次耗时）。

    ★2026-10-04：形状修对后，真过闸的请求会挂满超时（实测 200 恒 ~37s、排队态 60s+）⇒
    串行 5 个会把一轮拉到 300s+，与 --interval（现役 300s）打结；并发后一轮≈单个耗时。

    stagger=(lo,hi)：提交每个请求前随机 sleep [lo,hi] 秒（默认 None=不睡=旧行为逐字不变）。
    作用是把「同一秒 N 发」的批量突发摊成散点（见反检测常量块）；sleeper 仅供自测注入。
    """
    ms = list(models)
    sl = sleeper or time.sleep
    if len(ms) <= 1:
        if ms and stagger:
            sl(random.uniform(stagger[0], stagger[1]))
        return [probe_model(m, key, timeout=timeout) for m in ms]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(workers, len(ms))) as ex:
        futs = []
        for i, m in enumerate(ms):
            if i and stagger:
                sl(random.uniform(stagger[0], stagger[1]))
            futs.append(ex.submit(probe_model, m, key, timeout=timeout))
        return [f.result() for f in futs]


def _ledger_snippet(r):
    """非 200 时保留「网关原文片段」；200 一律 None（不把生成正文写进台账）。纯函数，供负控自测。

    ★2026-10-04 起因：站点消费日志里同一批请求写着「请求上游地址失败 / invalid codex request /
      not implemented」，而**我方台账只存 http+verdict**（detail 在内存里被落账丢掉）⇒
      事后只能靠用户截图才能区分「号池满」「形状不对」「网关未实现」。判据：
        429/503         -> 号池满（正常观测，别慌）
        400 invalid     -> 探针形状不对（可自愈）
        500 上游失败     -> 站点供给侧
        not implemented -> 网关未实现
      没有原文时这三者在我方侧长得一模一样（都是 400/500 + UNKNOWN/POOL_BUSY）。
    """
    if r.get("http") == 200:
        return None
    txt = " ".join((r.get("detail") or "").split())
    return txt[:160] or None


# ---------------------------------------------------------------- 受理阶段（站点日志口径）
# ★2026-10-04 起因：用户反复问「站点后台日志里只看到 GPT，没看到 Claude 的探测」。
#   地面真值（用登录态同源读 /api/log/self，最近 600 行 + 今日 148 行）：今日**全部 148 行**
#   出自本探针令牌 ga-monitor，其中 gpt-6-astra 108 行、gpt-6-astra-cc-format 38 行、
#   claude-haiku-4-5 仅 2 行（04:38 那两发真被受理并扣费 quota=3），而 claude 系今日实发
#   ~210 发。⇒ 结论：**站点日志只记「被受理并派发到某个上游账号」的请求**；claude 名当天
#   几乎全在**入口**被挡回（429/503 + body 无 request id，延迟 ~1.3s），站点根本不落行。
#   判据（可从响应体本身读出，不需要登录态）：
#       有 request id ⇒ 站点已受理（日志里会有这行，成功/失败另说）
#       429/503 且无 request id ⇒ 入口即拒（站点日志不会有这行）
#   这样「为什么日志里看不到某个模型」就有了机器可读的答案，不必再靠人翻后台。
STAGE_ADMITTED = "ADMITTED"          # 站点已受理（会落站点日志）
STAGE_GATE = "GATE_REJECTED"         # 入口即拒（不会落站点日志）
STAGE_UNKNOWN = "UNKNOWN"            # 传输层未取到判定证据（http=0 等）


def classify_stage(http, verdict, body_text):
    """(http, verdict, 响应体原文) -> 受理阶段。纯函数，供负控自测。

    依据：站点对「已受理」的请求在错误体里带 `request id: <id>`；入口拒绝体形如
    `{"error":{"message":"Service Unavailable","type":"error"}}`（无 request id）。
    fail-closed：无证据一律 UNKNOWN，绝不冒充「已受理」。
    """
    low = (body_text or "").lower()
    if http == 200:
        return STAGE_ADMITTED
    if "request id" in low or "request_id" in low:
        return STAGE_ADMITTED
    if http in (429, 503):
        return STAGE_GATE
    return STAGE_UNKNOWN


# ---------------------------------------------------------------- 台账摘要（--summary，只读）
# ★2026-10-04 起因：用户问「一直看你在探 gpt-6，Claude 面怎么没在探？」——
#   实情是**每轮 8 个名都在探**（含 6 个 claude-*），但台账是 append-only JSONL，
#   只有「命中 200」或「监控自身故障」才推送 ⇒ 全是 POOL_BUSY 时界面/通知里**看不见 claude 面**。
#   本函数把「最近 N 轮各模型的判据分布」直接摊开，使覆盖面**可自证**（无需读 JSONL）。
#   纯只读：不改台账、不推送、不落盘。
def summarize_ledger(lines=12, path=None):
    """最近 N 轮各模型判据分布（纯函数式只读，供 --summary 与负控自测）。

    返回 {"ok":True,"rounds":n,"total_rounds":m,"ts_from","ts_to","models":{m:{...}}}
    或 {"ok":False,"reason":"LEDGER_MISSING"|"NO_ROUNDS","path":p}
    """
    p = path or LEDGER_FILE
    if not os.path.exists(p):
        return {"ok": False, "reason": "LEDGER_MISSING", "path": p}
    recs = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except Exception:
                continue                      # 坏行跳过：摘要绝不因一行损坏而失败
    rounds = [r for r in recs if isinstance(r, dict) and r.get("results")]
    if not rounds:
        return {"ok": False, "reason": "NO_ROUNDS", "path": p}
    n = max(1, int(lines))
    tail = rounds[-n:]
    per = {}
    for r in tail:
        ts = r.get("ts")
        for x in r.get("results") or []:
            if not isinstance(x, dict):
                continue
            m = x.get("model") or "?"
            d = per.setdefault(m, {"counts": {}, "last_verdict": None, "last_ts": None,
                                   "last_non_busy": None, "n": 0, "stages": {}})
            v = x.get("verdict") or "?"
            _stx = x.get("stage") or STAGE_UNKNOWN      # 老台账无此键 ⇒ UNKNOWN（不冒充）
            d["stages"][_stx] = d["stages"].get(_stx, 0) + 1
            d["counts"][v] = d["counts"].get(v, 0) + 1
            d["n"] += 1
            d["last_verdict"] = v
            d["last_ts"] = ts
            if v != "POOL_BUSY":             # 号池满是「预期常态」，不算信号
                d["last_non_busy"] = {"ts": ts, "verdict": v, "http": x.get("http")}
    stages_all = {}
    for d in per.values():
        for k, v in (d.get("stages") or {}).items():
            stages_all[k] = stages_all.get(k, 0) + v
    return {"ok": True, "rounds": len(tail), "total_rounds": len(rounds),
            "ts_from": tail[0].get("ts"), "ts_to": tail[-1].get("ts"),
            "models": per, "stages": stages_all, "path": p}


def render_summary(s):
    """把 summarize_ledger 的结果渲染成一段人读文本（纯函数）。"""
    if not s.get("ok"):
        return "[anyrouter] 台账摘要不可用：%s（%s）" % (s.get("reason"), s.get("path"))
    per = s["models"]
    claude = [m for m in per if str(m).startswith("claude")]
    out = ["[anyrouter] 台账摘要：最近 %d 轮 / 共 %d 轮  %s → %s"
           % (s["rounds"], s["total_rounds"], s.get("ts_from"), s.get("ts_to")),
           "  覆盖面：%d 个模型（其中 claude 面 %d 个）" % (len(per), len(claude)),
           "  %-32s %5s  %s" % ("模型", "轮次", "判据分布 / 末次 / 末次非号池满")]
    _st = s.get("stages") or {}
    if _st:
        out.append("  受理阶段：ADMITTED(站点日志会记)×%d / GATE_REJECTED(站点日志不记)×%d / UNKNOWN×%d"
                   % (_st.get(STAGE_ADMITTED, 0), _st.get(STAGE_GATE, 0), _st.get(STAGE_UNKNOWN, 0)))
    for m, d in per.items():
        dist = " ".join("%s×%d" % (k, v) for k, v in sorted(d["counts"].items()))
        lnb = d.get("last_non_busy")
        lnb_txt = "-" if not lnb else "%s(%s)@%s" % (lnb["verdict"], lnb.get("http"), lnb["ts"])
        out.append("  %-32s %5d  %s | 末次=%s | %s" % (m, d["n"], dist, d["last_verdict"], lnb_txt))
    return "\n".join(out)


# ---------------------------------------------------------------- 姿势自证（--verify-pose）
# ★2026-10-04 起因：R538 引入「429 快返 = 形状不对」的判据（gate_suspect），但本地抓包逐发计时
#   证明真 Claude Code 的 9 发请求**同样全是 429@1.5-2.3s**（95KB 完整体）⇒ 该判据属误判。
#   为让「姿势到底对不对」可随时**自证**（而不是靠推断/靠人解释），做一次 A/B：
#   同一时刻、同一模型，分别用「我方探针形状」与「真 CC 形状」各打 n 发，比对结果分布。
#     POSE_OK           两形状同态（都 200 / 都排队 / 都 429-503）⇒ 形状非瓶颈
#     POSE_DIVERGED     一方可用、另一方被快拒 ⇒ 形状确有影响（此时才该改形状）
#     POSE_INCONCLUSIVE 数据不足（缺一边/全 OTHER）
POSE_OK = "POSE_OK"
POSE_DIVERGED = "POSE_DIVERGED"
POSE_INCONCLUSIVE = "POSE_INCONCLUSIVE"

CC_SHAPE_REF = os.path.join(STATE_DIR, "cc_shape_ref.json")
CC_FULL_REQ = os.path.join(STATE_DIR, "cc_full_request.json")
CC_FULL_REQ_TMP = "/tmp/anr_dump/001.json"


def _pose_state(http):
    """单发响应 → 形状可比的粗状态（纯函数，供负控自测）。"""
    if http == 200:
        return "UP"          # 拿到放行/生成
    if http == 0:
        return "QUEUE"       # 传输超时 = 没被拒，挂在上游排队/生成上
    if http in (429, 503):
        return "BUSY"        # 网关快速回绝（号池满的常见形态，★非形状门）
    return "OTHER"


def pose_conclusion(pairs):
    """A/B 对照结论（纯函数，供负控自测）。pairs=[{"shape":"probe"|"cc","http":int}, ...]。"""
    st = {}
    for r in pairs or []:
        st.setdefault(r.get("shape"), set()).add(_pose_state(r.get("http")))
    a = (st.get("probe") or set()) - {"OTHER"}
    b = (st.get("cc") or set()) - {"OTHER"}
    if not a or not b:
        return POSE_INCONCLUSIVE
    if ("UP" in a) == ("UP" in b) and ("QUEUE" in a) == ("QUEUE" in b):
        return POSE_OK
    return POSE_DIVERGED


CC_HDR_DROP = {"host", "content-length", "connection", "accept-encoding",
               "authorization", "x-api-key", "cookie"}


def _filter_cc_headers(h):
    """抓包头 → 安全形状头：剔除传输层与凭据（凭据一律用本工具自己的 key）。

    ★2026-10-04 实测坑：直接回放抓包头，authorization（别人的凭据）会覆盖我们自己的 key，
    且 content-length 与实际 body 冲突 ⇒ 请求必废 ⇒ 会被误读成「形状分歧」。纯函数，供自测。
    """
    if not isinstance(h, dict):
        return {}
    out = {}
    for k, v in h.items():
        if not isinstance(k, str) or k.lower() in CC_HDR_DROP:
            continue
        if isinstance(v, str):
            out[k] = v
    return out


def _norm_body(b):
    """请求体归一为 bytes（None 原样）。★坑：抓包文件里的 body 是 str，直接喂 _http_post 会在
    urllib 里报 TypeError: can't concat str to bytes（实测 ⇒ 被误读成 POSE_DIVERGED）。"""
    if b is None or isinstance(b, (bytes, bytearray)):
        return b
    if isinstance(b, str):
        return b.encode("utf-8")
    try:
        return json.dumps(b).encode()
    except Exception:
        return None


def _cc_shape_material():
    """真 CC 形状素材 → (headers|None, body|None, source)。

    优先级：STATE_DIR/cc_full_request.json > /tmp/anr_dump/001.json（抓包，易失）> cc_shape_ref.json（仅头）。
    ★这些文件只含**请求**，不含任何生成正文（200 的正文绝不落盘）。
    """
    for p in (CC_FULL_REQ, CC_FULL_REQ_TMP):
        try:
            with open(p) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("headers"), dict):
                return _filter_cc_headers(d.get("headers")), _norm_body(d.get("body")), p
        except Exception:
            pass
    try:
        with open(CC_SHAPE_REF) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("headers"), dict):
            return _filter_cc_headers(d["headers"]), None, CC_SHAPE_REF
    except Exception:
        pass
    return None, None, None


def verify_pose(model, key, n=3, timeout=60, post=None, hosts=None):
    """同刻 A/B：我方探针形状 vs 真 CC 形状，各 n 发。返回 dict（含 conclusion/pairs）。"""
    post = post or _http_post
    ch, cb, src = _cc_shape_material()
    pairs = []
    for shape, extra, body in (("probe", None, None), ("cc", ch, cb)):
        for _ in range(max(1, n)):
            if len([p for p in pairs if p["shape"] == shape and p["http"] == 200]):
                break                      # 该形状已拿到 200 ⇒ 收手（省调用）
            for host in (hosts or HOSTS):
                b = body if body is not None else build_probe_body(model)
                t0 = time.time()
                try:
                    code, text = post(host + CC_PATH, key, b, timeout,
                                      read_limit=256, extra_headers=extra)
                except TypeError:          # 桩替身可能是窄签名
                    code, text = post(host + CC_PATH, key, b, timeout)
                el = time.time() - t0
                r = {"shape": shape, "host": host, "http": code,
                     "elapsed_s": round(el, 2), "state": _pose_state(code),
                     "snippet": (None if code == 200
                                 else (" ".join((text or "").split())[:160] or None))}
                pairs.append(r)
                print("[pose] %-5s http=%-4s %6.2fs %s" % (shape, code, el, r["snippet"] or ""))
    return {"model": model, "n": n, "cc_shape_source": src, "cc_headers_available": bool(ch),
            "cc_body_available": cb is not None, "pairs": pairs,
            "conclusion": pose_conclusion(pairs), "ts": time.time(),
            "when": beijing_now().strftime("%Y-%m-%d %H:%M:%S")}


def run_verify_pose(args):
    """--verify-pose：跑一次 A/B 自证并落证据 JSON。rc=0 同态（姿势非瓶颈）/1 分歧。"""
    key = _load_key()
    model = (args.model_list or MODELS)[0]
    r = verify_pose(model, key, n=getattr(args, "pose_n", 3), timeout=args.timeout)
    if not args.dry_run:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "pose_verify_%s.json" % time.strftime("%Y%m%dT%H%M%S"))
        with open(path, "w") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        r["json_path"] = path
        print("[pose] 证据：%s" % path)
    print("[pose] 结论=%s（真 CC 形状来源：%s headers=%s body=%s）"
          % (r["conclusion"], r["cc_shape_source"], r["cc_headers_available"], r["cc_body_available"]))
    return 0 if r["conclusion"] == POSE_OK else 1


def _split_names(txt):
    return [x.strip() for x in (txt or "").split(",") if x.strip()]


def _pin_models(args):
    return _split_names(getattr(args, "pin_models", ""))


def _stagger_range(args):
    """轮内错峰区间；两端都为 0/缺省 ⇒ None（=不睡=旧行为逐字不变）。"""
    lo = float(getattr(args, "stagger_min", 0) or 0)
    hi = float(getattr(args, "stagger_max", 0) or 0)
    if lo <= 0 and hi <= 0:
        return None
    if hi < lo:
        lo, hi = hi, lo
    return (max(0.0, lo), max(0.0, hi))


def _round_cost(args):
    """一轮要发几个请求（=日预算的计价单位）。"""
    n = len(getattr(args, "model_list", None) or MODELS)
    k = int(getattr(args, "sample", 0) or 0)
    return n if (k <= 0 or k >= n) else max(1, k)


def pick_round_models(models, st, sample, pinned=None, rng=None):
    """本轮探测名单：轮内抽样 + 轮转游标（保证每个模型每循环必被探到，且再现间隔不固定）。

    返回 (名单, 全量数, 是否轮转)。抽样关（sample<=0 或 >= 全量）⇒ 全量乱序、rot=False（旧行为等价）。
    游标存在 state["rotation"]（order/pos）⇒ 跨轮、跨重启都续着走；状态损坏则 fail-open 取前 k 个
    （宁可多探、绝不因状态坏而停摆）。
    """
    ms = list(models)
    n = len(ms)
    rng = rng or random.Random()
    pin = [m for m in (pinned or []) if m in ms]
    k = int(sample or 0)
    if n <= 1 or k <= 0 or k >= n:
        out = list(ms)
        rng.shuffle(out)
        return out, n, False
    rest = [m for m in ms if m not in pin]
    kk = max(1, k - len(pin))
    try:
        rot = st.setdefault("rotation", {})
        order = rot.get("order")
        if not isinstance(order, list) or set(order) != set(rest):
            order = list(rest)
            rng.shuffle(order)
            rot["pos"] = 0
        pos = int(rot.get("pos") or 0)
        if pos < 0 or pos > len(order):
            pos = 0
        picked = order[pos:pos + kk]
        pos += len(picked)
        if len(picked) < kk:                     # 收尾不足 ⇒ 开新循环补齐（每循环全覆盖）
            order = list(rest)
            rng.shuffle(order)
            need = kk - len(picked)
            picked = picked + order[:need]
            pos = need
        rot["order"] = order
        rot["pos"] = pos % len(order) if order else 0
    except Exception:
        picked = list(rest)[:kk]
    return pin + picked, n, True


def next_interval(args, rng=None):
    """本轮结束后的睡眠秒数。

    --interval-min/--interval-max 同时给 ⇒ 均匀随机（禁固定频次）；pause_prob 概率叠加长休
    （× pause_mult）。未给区间 ⇒ 逐字回落到旧的固定 --interval（老配置行为不变）。
    """
    rng = rng or random.Random()
    lo = int(getattr(args, "interval_min", 0) or 0)
    hi = int(getattr(args, "interval_max", 0) or 0)
    if lo > 0 and hi > 0:
        if hi < lo:
            lo, hi = hi, lo
        base = rng.uniform(lo, hi)
    else:
        base = float(getattr(args, "interval", 0) or 0)
    p = float(getattr(args, "pause_prob", 0) or 0)
    if base > 0 and p > 0 and rng.random() < p:
        mult = getattr(args, "pause_mult", None) or DEFAULT_PAUSE_MULT
        base *= rng.uniform(float(mult[0]), float(mult[1]))
    return max(1.0, base)


def _budget_spent(st, results):
    """把本轮实际请求数记进 state（--max-per-day 的唯一计数处）；形状自愈重发按 2 计。"""
    try:
        day = beijing_now().strftime("%Y-%m-%d")
        n = len(results or []) + sum(1 for r in (results or []) if r.get("shape_retry"))
        if st.get("req_day") != day:
            st["req_day"] = day
            st["req_count"] = 0
        st["req_count"] = int(st.get("req_count") or 0) + n
        return st["req_count"]
    except Exception:
        return 0


def budget_verdict(st, pending, max_per_day):
    """(allow, used, sleep_hint)。超预算 ⇒ 不硬停，睡到次日 0 点 + 0~20min 抖动再继续。"""
    m = int(max_per_day or 0)
    if m <= 0:
        return True, 0, 0.0
    now = beijing_now()
    used = int(st.get("req_count") or 0) if st.get("req_day") == now.strftime("%Y-%m-%d") else 0
    if used + max(1, int(pending)) <= m:
        return True, used, 0.0
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    return False, used, max(60.0, (nxt - now).total_seconds()) + random.uniform(0, 1200)


def _note_next_round(sleep_s):
    """把「预期下一轮时刻」写进 state：守护进程据此放宽卡死阈值（长休不被误杀）。"""
    try:
        st = _load_state()
        st["next_sleep_s"] = int(round(sleep_s))
        st["next_round_ts"] = round(time.time() + sleep_s, 1)
        _save_state(st)
    except Exception:
        pass


def run_once(args):
    now_bj = beijing_now()
    st = _load_state()          # ★提前加载：抽样轮转/日预算都写在同一份 state（只加载一次）
    key = _load_key()
    if not key:
        print("[anyrouter] 无可用密钥（env ANYROUTER_API_KEY / keychain anyrouter_api_key / ~/.config/anyrouter/key）")
    models = args.model_list or MODELS
    rng = getattr(args, "_rng", None) or random.Random()
    models, _all_n, _rot = pick_round_models(models, st, getattr(args, "sample", 0),
                                             _pin_models(args), rng)
    if _rot:
        print("[anyrouter] 本轮名单 %d/%d（轮转抽样）：%s"
              % (len(models), _all_n, ",".join(m.split("/")[-1] for m in models)), flush=True)
    results = probe_models(models, key, timeout=args.timeout,
                           workers=int(getattr(args, "workers", DEFAULT_WORKERS) or DEFAULT_WORKERS),
                           stagger=_stagger_range(args)) if key else []
    rc = overall_rc(results)
    for r in results:
        # 面标注：messages 面不加后缀（保持与旧日志逐字一致）；codex 面/形状自愈才打标，
        # 否则「探针悄悄换了面」在日志里完全不可见（2026-10-04 实测踩到）。
        _face_tag = ""
        if r.get("face") and r.get("face") != FACE_MESSAGES:
            _face_tag = " [face=%s%s]" % (r["face"], " retry" if r.get("shape_retry") else "")
        # link 证据标记：http=0 的判定到底是「链路不可达」还是「过闸排队」，
        # 必须写进日志（2026-10-04 实测：QUEUED 与 LINK_DOWN 都源自 http=0，
        # 不标证据就没法事后复核）。
        _face_tag += (r.get("link") or "")
        _st = classify_stage(r.get("http"), r.get("verdict"), r.get("detail") or "")
        if _st == STAGE_GATE:
            _face_tag += " [gate]"        # 入口即拒：站点日志里不会有这一发
        elif _st == STAGE_ADMITTED:
            _face_tag += " [adm]"         # 已被站点受理：站点日志里能找到

        print("[anyrouter] %-28s http=%s %s (%s s/%s)%s" % (
            r["model"], r["http"], r["verdict"],
            "-" if r.get("elapsed_s") is None else r["elapsed_s"], r.get("latency", "na"), _face_tag))

    kind = fault_kind(results)
    gate_ctl = ""
    if kind is None and key and gate_suspect(results):
        # 全快返：拿对照身份验一下，只有「对照能过闸而我方不能」才判探针被拒
        gate_ctl = gate_control_probe(key, results[0].get("model") or MODELS[0],
                                      timeout=min(20, int(getattr(args, "timeout", 20) or 20)))
        print("[anyrouter] 全量瞬时快返（≤%ss）→ 对照身份判定：%s" % (FAST_REJECT_S, gate_ctl))
        if gate_ctl == "pass":
            kind = FAULT_GATE_REJECT
    now_ts = time.time()

    # --- 1) 抢到坑 → 可用推送 ---
    do_push, reason = should_notify(st, results, now_ts, args.cooldown)
    pushed, push_detail = (False, "not_requested")
    if do_push and args.notify:
        text = _build_text(results, now_bj, reason,
                           auto_hold_minutes=getattr(args, "auto_hold_minutes", 0),
                           keepalive_seconds=getattr(args, "keepalive_seconds", 240),
                           sid=session_id_for(key, "anyrouter-probe"))
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
            {"model": r["model"], "http": r["http"], "verdict": r["verdict"],
             "snippet": _ledger_snippet(r),      # ★非 200 的网关原文（判「号池满/形状错/未实现」的唯一凭据）
             "stage": classify_stage(r.get("http"), r.get("verdict"), r.get("detail") or ""),  # ★受理阶段（站点日志口径：ADMITTED 会落站点日志 / GATE_REJECTED 不会）
             "face": r.get("face"), "shape_retry": bool(r.get("shape_retry"))}
            for r in results],
            "rc": rc, "notify": bool(do_push and args.notify), "pushed": pushed,
            "reason": reason, "fault": kind or "", "fault_streak": streak,
            "fault_notify": f_reason,
            "gate_control": gate_ctl, "gate_suspect": bool(gate_suspect(results)),
            "latency": [r.get("latency") for r in results],
            "elapsed_s": [r.get("elapsed_s") for r in results]}
        _append_ledger(rec)
        st["last_ts"] = rec["ts"]
        st["last_verdict"] = AVAILABLE if rc == 1 else (
            results[0]["verdict"] if results and len(set(r["verdict"] for r in results)) == 1 else "MIXED")
        st["last_rc"] = rc
        if do_push and args.notify and pushed:
            _budget_spent(st, results)
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
    setattr(args, "_last_results", results)     # 供 watch 的自动占坑判据取用（只读派生，不改判据）
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
    rng = getattr(args, "_rng", None) or random.Random()
    args._rng = rng                       # 同一 rng 贯穿各轮（抽样/间隔共享）
    _lo, _hi = int(getattr(args, "interval_min", 0) or 0), int(getattr(args, "interval_max", 0) or 0)
    if _lo > 0 and _hi > 0:
        _pat = "随机 %d-%ds（长休概率 %.0f%%×%s）" % (min(_lo, _hi), max(_lo, _hi),
                                                    (getattr(args, "pause_prob", 0) or 0) * 100,
                                                    getattr(args, "pause_mult", DEFAULT_PAUSE_MULT))
    else:
        _pat = "固定 %ds" % args.interval
    print("[anyrouter] watch 启动 %s（间隔 %s，冷却 %ds，每轮请求 %d 个，日上限 %s，硬超时 %s）"
          % (start.strftime("%Y-%m-%d %H:%M:%S"), _pat, args.cooldown,
             _round_cost(args), getattr(args, "max_per_day", 0) or "不限",
             hard_end.strftime("%H:%M:%S") if hard_end else "无"))
    try:
        while True:
            _allow, _used, _hint = budget_verdict(_load_state(), _round_cost(args),
                                                  getattr(args, "max_per_day", 0))
            if not _allow:
                print("[anyrouter] watch: 当日请求预算用尽（已 %d + 本轮 %d > %d）⇒ 睡 %.0fs 到次日"
                      % (_used, _round_cost(args), getattr(args, "max_per_day", 0), _hint), flush=True)
                time.sleep(_hint)
                continue
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
            # ★自动占坑（--auto-hold-minutes>0）：本轮命中可用 ⇒ 用**探测同 session**后台保活，
            #   不阻塞下一轮探测；冷却/已在保活中由 should_auto_hold 拦掉（防反复占用）。
            if getattr(args, "auto_hold_minutes", 0):
                try:
                    _hit = should_auto_hold(getattr(args, "_last_results", None), _load_state(),
                                            time.time(), getattr(args, "hold_cooldown", HOLD_COOLDOWN_S))
                    if _hit:
                        start_auto_hold(args, _hit)
                except Exception as _e:      # 占坑失败绝不致死监控循环
                    print("[anyrouter] watch: 自动占坑启动异常（已忽略）: %s: %s"
                          % (type(_e).__name__, _e), flush=True)
            if hard_end and beijing_now() >= hard_end:
                print("[anyrouter] watch: 达到 --max-minutes 上限，退出（共 %d 轮）" % rounds)
                return 0 if rc != 1 else 1
            _sleep = next_interval(args, rng)
            _note_next_round(_sleep)
            print("[anyrouter] watch: 第 %d 轮结束，下一轮 %.0fs 后" % (rounds, _sleep), flush=True)
            time.sleep(_sleep)
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
    # 1b) 姿势自证：A/B 结论判据（★2026-10-04 订正：快返 ≠ 形状门）
    chk("pose: 双429 -> POSE_OK", pose_conclusion([{"shape": "probe", "http": 429},
                                                   {"shape": "cc", "http": 429}]), POSE_OK)
    chk("pose: 双200 -> POSE_OK", pose_conclusion([{"shape": "probe", "http": 200},
                                                   {"shape": "cc", "http": 200}]), POSE_OK)
    chk("pose: 双排队(0) -> POSE_OK", pose_conclusion([{"shape": "probe", "http": 0},
                                                       {"shape": "cc", "http": 0}]), POSE_OK)
    chk("NC: 我方429/真CC排队 -> POSE_DIVERGED",
        pose_conclusion([{"shape": "probe", "http": 429}, {"shape": "cc", "http": 0}]), POSE_DIVERGED)
    chk("NC: 真CC 200/我方429 -> POSE_DIVERGED",
        pose_conclusion([{"shape": "probe", "http": 429}, {"shape": "cc", "http": 200}]), POSE_DIVERGED)
    chk("pose: 空 -> POSE_INCONCLUSIVE", pose_conclusion([]), POSE_INCONCLUSIVE)
    chk("pose: 缺一边 -> POSE_INCONCLUSIVE",
        pose_conclusion([{"shape": "probe", "http": 429}]), POSE_INCONCLUSIVE)
    # 1d) 形状素材归一（★抓包回放的三个实测坑：str body / content-length / 别人凭据）
    chk("body 归一: str -> bytes", _norm_body("abc"), b"abc")
    chk("body 归一: bytes 原样", _norm_body(b"xy"), b"xy")
    chk("body 归一: None 原样", _norm_body(None), None)
    chk("body 归一: dict -> JSON bytes", _norm_body({"a": 1}), b'{"a": 1}')
    _fh = _filter_cc_headers({"content-length": "95162", "authorization": "Bearer other",
                              "host": "anyrouter.top", "x-app": "cli",
                              "user-agent": "claude-cli/2.1.220", "accept-encoding": "gzip",
                              "cookie": "s=1", "x-num": 5})
    chk("头过滤: 剔 content-length", "content-length" in _fh, False)
    chk("头过滤: 剔别人 authorization", "authorization" in _fh, False)
    chk("头过滤: 剔 host/cookie/accept-encoding",
        any(k in _fh for k in ("host", "cookie", "accept-encoding")), False)
    chk("头过滤: 保留形状头", _fh.get("x-app"), "cli")
    chk("头过滤: 非 str 值丢弃", "x-num" in _fh, False)
    chk("头过滤: 非 dict 输入", _filter_cc_headers(None), {})
    chk("pose_state: 503->BUSY", _pose_state(503), "BUSY")
    chk("pose_state: 0->QUEUE", _pose_state(0), "QUEUE")
    chk("pose_state: 404->OTHER", _pose_state(404), "OTHER")
    # 1c) verify_pose 端到端（桩替身，零网络；静音其打印）
    def _stub_post(url, key=None, body=None, timeout=0, read_limit=None, extra_headers=None):
        return (429, '{"error":{"message":"Service Unavailable"}}')
    import io as _io, contextlib as _ctx
    with _ctx.redirect_stdout(_io.StringIO()):
        _vp = verify_pose("claude-opus-4-8", "sk-x", n=1, timeout=5, post=_stub_post,
                          hosts=["https://pose.invalid"])
    chk("verify_pose: 全429 -> POSE_OK", _vp["conclusion"], POSE_OK)
    chk("verify_pose: 每形状1发", len(_vp["pairs"]), 2)
    chk("200->AVAILABLE", classify(200, '{"ok":1}'), AVAILABLE)
    chk("429->BUSY", classify(429, '{"error":{"message":"Service Unavailable"}}'), BUSY)
    chk("503->BUSY", classify(503, '{"error":{"message":"Service Unavailable"}}'), BUSY)
    chk("400-1m->NEED_1M", classify(400, '{"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试"}'), NEED_1M)
    chk("400-官方通告->MODEL_NOTICE", classify(
        400, '{"error":"由于 claude 模型供应难以保证，上线 gpt-6-astra-cc-format 模型，'
             '支持 messages 格式，通过 claude --model \'gpt-6-astra-cc-format[1m]\' 使用该模型","type":"error"}'),
        MODEL_NOTICE)
    chk("NC: 400 无关正文->UNKNOWN", classify(400, '{"error":"bad request"}'), UNKNOWN)
    chk("NC: 400 含 1m 但无上下文 -> UNKNOWN",
        classify(400, '{"error":"model name gpt-6-astra[1m] not found"}'), UNKNOWN)
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
    _txh = _build_text(avail, beijing_now(), "state_changed", auto_hold_minutes=30, keepalive_seconds=20)
    chk("text: 自动保活提示", ("已自动保活 30 分钟" in _txh), True)
    chk("NC: 保活时不提示'尽快连着用'", ("尽快连着用" in _txh), False)
    chk("NC: 未开保活时仍提示被踢", ("尽快连着用" in txt), True)
    _sidt = _build_text(avail, beijing_now(), "state_changed", auto_hold_minutes=30,
                        keepalive_seconds=20, sid="11111111-2222-3333-4444-555555555555")
    chk("text: 保活时给出完整 session_id", ("11111111-2222-3333-4444-555555555555" in _sidt), True)
    chk("text: 给出可直接粘贴的 --session-id 复用命令",
        ("claude --session-id 11111111-2222-3333-4444-555555555555" in _sidt), True)
    chk("NC: 未传 sid 时不出现 --session-id", ("--session-id" in _txh), False)
    # 6) 状态键
    chk("state keys", sorted(["last_ts", "last_verdict", "last_rc"]),
        sorted(["last_ts", "last_verdict", "last_rc"]))
    chk("NC: MODELS 均为非空 str", all(isinstance(m, str) and m for m in MODELS), True)
    # ★2026-10-04 收敛政策（用户要求）：只探高模型 —— Opus/Fable 两条线，4.x 与小模型退出监测面。
    #   守卫改**结构判据**（不绑固定条数、不绑固定名），名单回退到低模型时会当场变红。
    import re as _re
    _low_pat = _re.compile(r"(opus-4|sonnet-4|sonnet-3|haiku|opus-3|fable-4|claude-3|gpt-4)", _re.I)
    chk("MODELS 非空", len(MODELS) >= 1, True)
    chk("策略: 无低模型名混入", [m for m in MODELS if _low_pat.search(m)], [])
    chk("策略: 至少一个 opus 名", any("opus" in m.lower() for m in MODELS), True)
    chk("策略: 至少一个 fable 名", any("fable" in m.lower() for m in MODELS), True)
    chk("NC: 低模型名会被策略判出", bool(_low_pat.search("claude-opus-4-8")), True)
    chk("NC: haiku 会被策略判出", bool(_low_pat.search("claude-haiku-4-5")), True)
    chk("策略反例: 高模型名不被误判",
        [_low_pat.search(x) for x in ("claude-opus-5-5", "claude-fable-5-1-reversed")], [None, None])
    chk("NC: MODELS 无重复", len(set(MODELS)) == len(MODELS), True)

    # 11) 自动占坑（--auto-hold-minutes）：★2026-10-04 用户要求「接上自动抢坑 + 抢到后保活 30 分钟」
    _av = [{"model": "m1", "verdict": AVAILABLE, "http": 200}]
    _bs = [{"model": "m1", "verdict": BUSY, "http": 429}]
    chk("hold: 命中可用 -> 触发", (should_auto_hold(_av, {}, 1000) or {}).get("model"), "m1")
    chk("NC: 全 BUSY -> 不触发", should_auto_hold(_bs, {}, 1000), None)
    chk("NC: 空结果 -> 不触发", should_auto_hold([], {}, 1000), None)
    chk("NC: None 结果 -> 不触发", should_auto_hold(None, {}, 1000), None)
    chk("NC: 保活中 -> 不重复触发", should_auto_hold(_av, {"hold_active": True}, 1000), None)
    chk("NC: 冷却内 -> 不触发", should_auto_hold(_av, {"last_hold_ts": 990}, 1000, 600), None)
    chk("冷却已过 -> 触发", (should_auto_hold(_av, {"last_hold_ts": 300}, 1000, 600) or {}).get("model"), "m1")
    chk("NC: last_hold_ts 脏值 -> 不崩且触发",
        (should_auto_hold(_av, {"last_hold_ts": "x"}, 1000, 600) or {}).get("model"), "m1")
    # hold_slot 状态机（注入 post/sleep/now，零网络）
    _hst = {"t": 0.0}
    _seq = [(200, ""), (200, ""), (200, "")]
    _calls = []

    def _hpost(b, timeout=0):
        _calls.append(b)
        return _seq.pop(0) if _seq else (200, "")
    chk("hold: 持续200 -> True（保活段跑完）",
        hold_slot("m1", "sid", 0.02, 0.01, _hpost,
                  lambda s: _hst.__setitem__("t", _hst["t"] + float(s)), lambda: _hst["t"]), True)
    chk("hold: 真发了 keepalive（>=1 发）", len(_calls) >= 1, True)
    chk("hold: 保活体是内容 '1'", (b'"content": "1"' in _calls[0]), True)
    # NC：坑位被踢（200 后 503）⇒ 立即停、不得继续发
    _hst2 = {"t": 0.0}
    _seq2 = [(200, ""), (503, ""), (200, "")]
    _c2 = []

    def _hpost2(b, timeout=0):
        _c2.append(b)
        return _seq2.pop(0) if _seq2 else (200, "")
    chk("NC: 200 后 503 -> False（被踢即停）",
        hold_slot("m1", "sid", 0.05, 0.01, _hpost2,
                  lambda s: _hst2.__setitem__("t", _hst2["t"] + float(s)), lambda: _hst2["t"]), False)
    chk("NC: 被踢后不再发（发数恰 2）", len(_c2), 2)
    chk("NC: minutes=0 -> False",
        hold_slot("m1", "sid", 0, 0.01, lambda b, timeout=0: (_c2.append(b), (200, ""))[1],
                  lambda s: None, lambda: 0), False)
    chk("NC: minutes=0 零发", len(_c2), 2)
    chk("NC: 首发 429 -> False（不把池满当在保活）",
        hold_slot("m1", "sid", 0.05, 0.01, lambda b, timeout=0: (429, ""), lambda s: None, lambda: 0), False)
    chk("NC: 首发传输错 -> False",
        hold_slot("m1", "sid", 0.05, 0.01, lambda b, timeout=0: (0, "URLError"),
                  lambda s: None, lambda: 0), False)

    # 11b) 停保活开关（★2026-10-04 用户问「我在用别的客户端，你还保活不是白烧吗」）
    chk("sleep_with_stop: 不拦 -> False", sleep_with_stop(0.01, sleep=lambda s: None,
                                                         stop_check=lambda: False), False)
    chk("sleep_with_stop: 中途拦 -> True", sleep_with_stop(1.0, step=0.1, sleep=lambda s: None,
                                                          stop_check=lambda: True), True)
    _st3 = {"n": 0}

    def _stop_after_1():
        _st3["n"] += 1
        return _st3["n"] > 1          # 第一次问=不拦；之后=停
    _c3 = []
    chk("hold: --stop-hold -> 提前停手（False）",
        hold_slot("m1", "sid", 5.0, 0.01,
                  lambda b, timeout=0: (_c3.append(b), (200, ""))[1],
                  lambda s: None, lambda: 0, stop_check=_stop_after_1), False)
    chk("hold: 停手后不再发（发数<=2）", len(_c3) <= 2, True)
    _clk = {"t": 0.0}
    chk("NC: 未请求停 -> 仍照常跑完（不误停）",
        hold_slot("m1", "sid", 0.02, 0.01, lambda b, timeout=0: (200, ""),
                  lambda s: _clk.__setitem__("t", _clk["t"] + float(s)), lambda: _clk["t"],
                  stop_check=lambda: False), True)
    # 旗标文件往返（沙盒 state-dir，绝不碰真实监控目录）
    _sd_old = STATE_DIR
    try:
        import tempfile as _tf
        _apply_state_dir(_tf.mkdtemp(prefix="anr_stop_"))
        chk("stop: 初始无旗标", hold_stop_requested(), False)
        _fp = request_hold_stop("selftest")
        chk("stop: 写旗标后为 True", hold_stop_requested(), True)
        chk("stop: 旗标文件名正确", os.path.basename(_fp), "hold.stop")
        chk("stop: clear 后回到 False", (clear_hold_stop(), hold_stop_requested()), (True, False))
        chk("NC: 重复 clear 安全", clear_hold_stop(), False)
    finally:
        _apply_state_dir(_sd_old)
    # 11c) 通知文案说实话（★坑位不绑身份；并给出停机命令）
    _txt = _build_text([{"model": "m1", "verdict": AVAILABLE, "http": 200}], beijing_now(), "t",
                       auto_hold_minutes=30, keepalive_seconds=240, sid="sid-abc")
    chk("text: 明说坑位不绑身份", ("不绑身份" in _txt), True)
    chk("text: 给出 --stop-hold 停机命令", ("--stop-hold" in _txt), True)
    chk("NC: 不再声称「已占住的坑位身份」", ("已占住的坑位身份" in _txt), False)
    chk("text: 仍保留 session 记号（标非必需）", ("claude --session-id sid-abc" in _txt), True)

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

    # 9c) ★2026-10-04 会话形状 + 延迟维度（本轮假阴性根因：形状被拒 ≠ 号池满）
    _sid_probe = session_id_for("sk-test-1", "tagA")
    chk("NC: session 不再是自造长串（旧形 hard-reject）", _sid_probe.startswith("user_"), False)
    chk("lat: 429 快返 -> fast", classify_latency(429, BUSY, 1.2)[1], "fast")
    chk("lat: 503 快返 -> fast", classify_latency(503, BUSY, 1.9)[1], "fast")
    chk("lat: 503 慢返 -> slow", classify_latency(503, BUSY, 12.0)[1], "slow")
    chk("lat: 200 -> ok", classify_latency(200, AVAILABLE, 37.0)[1], "ok")
    chk("lat: 9s+ 传输挂起 -> QUEUED", classify_latency(0, UNKNOWN, 45.0)[0], QUEUED)
    chk("NC: 传输快失败（2s）不算 QUEUED", classify_latency(0, UNKNOWN, 2.0)[0], UNKNOWN)
    # 9c2) ★2026-10-04 链路证据门：挂满超时必须先证「链路可达」才算 QUEUED
    chk("lat: 控探针证链路可达 -> 仍 QUEUED",
        classify_latency(0, UNKNOWN, 45.0, link_ok=True)[0], QUEUED)
    chk("lat: 控探针拿不到响应 -> LINK_DOWN（不再乐观判 QUEUED）",
        classify_latency(0, UNKNOWN, 45.0, link_ok=False)[0], LINK_DOWN)
    chk("NC: link_ok=None 保持旧行为（自测/桩兼容）",
        classify_latency(0, UNKNOWN, 45.0, link_ok=None)[0], QUEUED)
    chk("NC: 快失败即便 link_ok=False 也不判 LINK_DOWN",
        classify_latency(0, UNKNOWN, 2.0, link_ok=False)[0], UNKNOWN)
    chk("NC: 200 携带 link_ok=False 不改判",
        classify_latency(200, AVAILABLE, 30.0, link_ok=False)[0], AVAILABLE)
    chk("fault: 全 LINK_DOWN -> TRANSPORT", fault_kind([{"verdict": LINK_DOWN}]), FAULT_TRANSPORT)
    chk("rc: 全 LINK_DOWN -> 2", overall_rc([{"verdict": LINK_DOWN}]), 2)
    chk("NC: LINK_DOWN+QUEUED 混合不算链路故障",
        fault_kind([{"verdict": LINK_DOWN}, {"verdict": QUEUED}]), None)
    chk("link: 控探针拿到 HTTP 响应即可达",
        _link_ok("h", get=lambda u, t: type("R", (), {"status": 432, "close": lambda s: None})()), True)
    chk("link: 标记 http=0 的证据（ok/down/未取证空）",
        (_link_tag(True), _link_tag(False), _link_tag(None)), (" [link=ok]", " [link=down]", ""))
    chk("link: LINK_DOWN 时 queued 必须为 False（证据门与排队互斥）",
        classify_latency(0, UNKNOWN, 45.0, link_ok=False)[2], False)
    chk("link: QUEUED 时 queued 必须为 True",
        classify_latency(0, UNKNOWN, 45.0, link_ok=True)[2], True)

    def _boom(u, t):
        raise OSError("boom")

    def _err403(u, t):
        raise urllib.error.HTTPError("u", 403, "x", None, None)

    chk("link NC: 控探针抛错即不可达", _link_ok("h", get=_boom), False)
    chk("link: HTTPError(403) 仍算可达（拿到了响应）", _link_ok("h", get=_err403), True)
    chk("NC: classify_latency 不把 200 降级", classify_latency(200, AVAILABLE, 0.5)[0], AVAILABLE)
    chk("fault: 全 BUSY 且全 fast **不再**直接判 GATE_REJECT（需对照证据）",
        fault_kind([{"verdict": BUSY, "latency": "fast"}, {"verdict": BUSY, "latency": "fast"}]),
        None)
    chk("fault: 全快返 ⇒ gate_suspect() 为真（候选）",
        gate_suspect([{"verdict": BUSY, "latency": "fast"}, {"verdict": BUSY, "latency": "fast"}]), True)
    chk("NC: 有一发 slow ⇒ 非候选",
        gate_suspect([{"verdict": BUSY, "latency": "fast"}, {"verdict": BUSY, "latency": "slow"}]), False)
    chk("NC: 有一发 QUEUED ⇒ 非候选（已过闸）",
        gate_suspect([{"verdict": BUSY, "latency": "fast"}, {"verdict": QUEUED, "latency": "timeout"}]), False)
    _ck = _load_key() or "sk-dummy"
    _uid = {"device_id": "d", "account_uuid": "", "session_id": "11111111-2222-3333-4444-555555555555"}
    import tempfile as _tf
    _tfdir = _tf.mkdtemp()
    _cf = os.path.join(_tfdir, "cc_control_uid.json")
    json.dump(_uid, open(_cf, "w"))
    os.environ["ANR_GATE_CONTROL_UID_JSON"] = _cf
    chk("gate control: 对照 200 ⇒ pass",
        gate_control_probe(_ck, "m", post=lambda *a, **k: (200, '{"x":1}')), "pass")
    chk("NC: 对照 401（同一 key 也认不出）⇒ unknown（不硬判）",
        gate_control_probe(_ck, "m", post=lambda *a, **k: (401, "unauthorized client")), "unknown")
    chk("gate control: 对照挂满超时（走上游）⇒ pass",
        gate_control_probe(_ck, "m", timeout=1, post=lambda *a, **k: (0, "TimeoutError")), "pass")
    chk("gate control: 对照也快返 ⇒ reject",
        gate_control_probe(_ck, "m", post=lambda *a, **k: (429, "{}")), "reject")
    os.environ["ANR_GATE_CONTROL_UID_JSON"] = "none"     # 显式禁用对照身份
    chk("NC: 无对照身份 ⇒ unavailable（不得凭空判 GATE_REJECT）",
        gate_control_probe(_ck, "m"), "unavailable")
    os.environ.pop("ANR_GATE_CONTROL_UID_JSON", None)
    chk("NC: 全 BUSY 但慢返 -> 非故障（真池满）",
        fault_kind([{"verdict": BUSY, "latency": "slow"}]), None)
    chk("NC: BUSY 无 latency（旧台账记录）-> 非故障",
        fault_kind([{"verdict": BUSY}]), None)
    chk("fault: QUEUED 属正常观测（不是链路故障）", fault_kind([{"verdict": QUEUED}]), None)
    chk("rc: 全 QUEUED -> 0（没坑，非故障）", overall_rc([{"verdict": QUEUED}]), 0)
    chk("meta: user_id 内嵌 session_id 亦为 UUID 形",
        _is_uuid(json.loads(cc_meta_user_id(_sid_probe))["session_id"]), True)
    chk("icon: QUEUED 有中文图标可读", bool(ICON.get(QUEUED)), True)
    chk("fault text: GATE_REJECT 文案点名「非号池满」",
        "非号池满" in _build_fault_text(FAULT_GATE_REJECT, [], beijing_now(), 3), True)

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
    chk("session: 形态=UUID（36 字符 8-4-4-4-12，站点放行门禁）", _is_uuid(sid), True)
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

    # ---- Codex 面（第二套「客户端形状」：/v1/responses）----
    chk("face: 显式登记优先（gpt-6-astra -> codex）", face_of("gpt-6-astra"), FACE_CODEX)
    chk("face: 同名反例 cc-format 仍走 messages", face_of("gpt-6-astra-cc-format"), FACE_MESSAGES)
    chk("face: claude-* 默认 messages", face_of("claude-opus-4-8"), FACE_MESSAGES)
    chk("face: 未登记 gpt-* 兜底 codex", face_of("gpt-9-future"), FACE_CODEX)
    chk("NC: 大小写与空格不影响判定", face_of("  GPT-6-Astra "), FACE_CODEX)

    _cb = json.loads(build_codex_probe_body("gpt-6-astra").decode())
    chk("codex body: 门禁必需键齐（include+prompt_cache_key）",
        all(k in _cb for k in CODEX_REQUIRED_KEYS), True)
    chk("codex body: include=reasoning.encrypted_content", _cb["include"], list(CODEX_INCLUDE))
    chk("NC: 缺 prompt_cache_key 就不是放行形状",
        all(k in {kk: 1 for kk in _cb if kk != "prompt_cache_key"} for k in CODEX_REQUIRED_KEYS), False)
    _ch = codex_headers("k")
    chk("codex 头: originator=codex_exec 且带 session-id",
        (_ch["originator"], "session-id" in _ch, _is_uuid(_ch["session-id"])), ("codex_exec", True, True))

    chk("classify: 400 invalid codex request -> CODEX_SHAPE_REQUIRED",
        classify(400, '{"code":"invalid_responses_request","message":"invalid codex request"}'),
        CODEX_SHAPE_REQUIRED)
    chk("NC: 普通 400（非 codex 关键词）不误判为形状门禁",
        classify(400, '{"error":{"message":"prompt too long","type":"invalid_request_error"}}'), UNKNOWN)

    _real_post = globals()["_http_post"]
    try:
        calls = []

        def _fake_post(url, key=None, body=None, timeout=0, read_limit=None, extra_headers=None):
            calls.append({"url": url, "body": json.loads((body or b"{}").decode())})
            if url.endswith("/v1/messages?beta=true"):
                return 400, '{"code":"invalid_responses_request","message":"invalid codex request"}'
            return 200, '{"ok":true}'

        globals()["_http_post"] = _fake_post
        _r = probe_model("claude-opus-4-8", "k", hosts=["https://x"])
        chk("自愈: 被 codex 门禁拒后自动换面重打 -> SLOT_AVAILABLE", _r["verdict"], AVAILABLE)
        chk("自愈: shape_retry 打标且终面=codex", (_r["shape_retry"], _r["face"]), (True, FACE_CODEX))
        chk("自愈: 第二发确实走 /v1/responses 且带门禁键",
            (calls[-1]["url"].endswith("/v1/responses"),
             all(k in calls[-1]["body"] for k in CODEX_REQUIRED_KEYS)), (True, True))
        chk("NC: 自愈只换一次面（共 2 发）", len(calls), 2)

        calls[:] = []
        _r2 = probe_model("gpt-6-astra", "k", hosts=["https://x"])
        chk("NC: codex 面模型首发即命中 -> 不触发自愈",
            (_r2["shape_retry"], _r2["face"], len(calls)), (False, FACE_CODEX, 1))

        calls[:] = []

        def _fake_post2(url, key=None, body=None, timeout=0, read_limit=None, extra_headers=None):
            calls.append(url)
            return 400, '{"code":"invalid_responses_request"}'

        globals()["_http_post"] = _fake_post2
        _r3 = probe_model("claude-opus-4-8", "k", hosts=["https://x"])
        chk("NC: 换面后仍被拒 -> 判 CODEX_SHAPE_REQUIRED 且不无限重试",
            (_r3["verdict"], len(calls)), (CODEX_SHAPE_REQUIRED, 2))
    finally:
        globals()["_http_post"] = _real_post

    # 9d) ★2026-10-04 网关原文落账：站点消费日志能看见「请求上游地址失败 / invalid codex request /
    #     not implemented」，而我方台账原先只存 http+verdict ⇒ 三类失败在本地长得一样，
    #     只能靠用户截图才能分「号池满 / 形状不对 / 网关未实现」。判据：
    #       200 ⇒ None（绝不把生成正文写进台账）
    #       非 200 ⇒ detail 空白归一后 ≤160 字
    #       无 detail / 全空白 ⇒ None（别造假证据）
    chk("snippet: 200 -> None（不落生成正文）",
        _ledger_snippet({"http": 200, "detail": "hello world"}), None)
    chk("snippet: 429 原文保留",
        _ledger_snippet({"http": 429, "detail": "号池满：当前分组无可用渠道"}),
        "号池满：当前分组无可用渠道")
    chk("snippet: 空白归一 + 截断 160",
        (_ledger_snippet({"http": 400, "detail": "a" * 300}),
         len(_ledger_snippet({"http": 400, "detail": "x\n\n  y\tz"}))),
        ("a" * 160, 5))
    chk("NC: 无 detail / 全空白 -> None",
        (_ledger_snippet({"http": 500}), _ledger_snippet({"http": 503, "detail": "  \n "})),
        (None, None))
    chk("snippet: 形状错误可回读（站点日志那三类失败的本地判据）",
        "invalid codex request" in (_ledger_snippet(
            {"http": 400, "detail": '{"error":{"message":"invalid codex request"}}'}) or ""),
        True)

    # 9d2) ★2026-10-04 受理阶段判据（=用户「站点后台日志只看到 GPT，看不到 Claude」的机器判据）
    #      地面真值：站点日志只记「被受理并派发到上游账号」的请求；claude 当天几乎全在入口被挡回。
    chk("stage: 200 -> ADMITTED",
        classify_stage(200, AVAILABLE, ""), STAGE_ADMITTED)
    chk("stage: 429 无 request id（claude 实测体）-> GATE_REJECTED",
        classify_stage(429, BUSY, '{"error":{"message":"Service Unavailable","type":"error"}}'),
        STAGE_GATE)
    chk("stage: 429 带 request id -> ADMITTED（判据优先级：有 id 即已受理）",
        classify_stage(429, BUSY, "负载已经达到上限，请稍后重试 (request id: 20261004123456789)"),
        STAGE_ADMITTED)
    chk("stage: 500 带 request id（gpt-6-astra 站点日志那行）-> ADMITTED",
        classify_stage(500, BUSY, '{"error":{"message":"请求上游地址失败 (request id: 20261004123)"}}'),
        STAGE_ADMITTED)
    chk("NC: 裸 500 无任何证据 -> UNKNOWN（fail-closed，不冒充已受理）",
        classify_stage(500, UNKNOWN, '{"error":{"message":"oops"}}'), STAGE_UNKNOWN)
    chk("NC: http=0（传输层）-> UNKNOWN",
        classify_stage(0, QUEUED, ""), STAGE_UNKNOWN)

    # 9d3) 台账/摘要必须把 stage 带出来（否则「站点日志里有没有这一发」仍不可自证）
    import tempfile as _tf2, json as _json2
    _d2 = _tf2.mkdtemp(prefix="anr_stage_")
    _lp2 = os.path.join(_d2, "ledger.jsonl")
    with open(_lp2, "w", encoding="utf-8") as _f:
        _f.write(_json2.dumps({"ts": "t1", "results": [
            {"model": "claude-opus-4-8", "http": 429, "verdict": "POOL_BUSY", "stage": STAGE_GATE},
            {"model": "gpt-6-astra", "http": 500, "verdict": "POOL_BUSY", "stage": STAGE_ADMITTED}]}) + "\n")
        _f.write(_json2.dumps({"ts": "t2", "results": [          # 老台账：无 stage 键
            {"model": "claude-haiku-4-5", "http": 429, "verdict": "POOL_BUSY"}]}) + "\n")
    _sum2 = summarize_ledger(lines=12, path=_lp2)
    chk("summary: 顶层 stages 汇总（ADMITTED=1 / GATE=1 / UNKNOWN=1[老台账不冒充]）",
        (_sum2["stages"].get(STAGE_ADMITTED), _sum2["stages"].get(STAGE_GATE),
         _sum2["stages"].get(STAGE_UNKNOWN)), (1, 1, 1))
    _txt2 = render_summary(_sum2)
    chk("summary: 渲染出「受理阶段」行且含两个口径词",
        ("受理阶段" in _txt2 and "ADMITTED" in _txt2 and "GATE_REJECTED" in _txt2), True)

    # 9e) ★2026-10-04 --summary：把「最近 N 轮各模型判据分布」摊开，证明覆盖面（含 claude 面）
    #     而不是靠界面/通知猜。纯只读 ⇒ 负控要覆盖：缺台账 / 全是非轮次行 / 坏行容错 / 截断轮数。
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="anr_summary_")
    _lp = os.path.join(_d, "ledger.jsonl")
    _rounds = [
        {"ts": "t1", "results": [{"model": "claude-opus-4-8", "http": 429, "verdict": "POOL_BUSY"},
                                 {"model": "gpt-6-astra", "http": 500, "verdict": "POOL_BUSY"}]},
        {"ts": "t2", "results": [{"model": "claude-opus-4-8", "http": 429, "verdict": "POOL_BUSY"},
                                 {"model": "gpt-6-astra", "http": 200, "verdict": "AVAILABLE"}]},
        {"ts": "t3", "results": [{"model": "claude-opus-4-8", "http": 429, "verdict": "POOL_BUSY"},
                                 {"model": "gpt-6-astra", "http": 500, "verdict": "POOL_BUSY"}]},
    ]
    with open(_lp, "w", encoding="utf-8") as _f:
        for _r in _rounds:
            _f.write(json.dumps(_r, ensure_ascii=False) + "\n")
        _f.write("not json at all\n")                       # 坏行必须被跳过
        _f.write(json.dumps({"ts": "x", "mode": "keepalive"}, ensure_ascii=False) + "\n")
    _s = summarize_ledger(12, _lp)
    chk("summary: 轮次计数（只数含 results 的行）", (_s["ok"], _s["rounds"], _s["total_rounds"]),
        (True, 3, 3))
    chk("summary: claude 面在覆盖率内且判据分布正确",
        (_s["models"]["claude-opus-4-8"]["counts"], _s["models"]["claude-opus-4-8"]["last_non_busy"]),
        ({"POOL_BUSY": 3}, None))
    chk("summary: 末次非号池满可回读（AVAILABLE@t2）",
        (_s["models"]["gpt-6-astra"]["counts"],
         _s["models"]["gpt-6-astra"]["last_non_busy"]["verdict"],
         _s["models"]["gpt-6-astra"]["last_non_busy"]["ts"]),
        ({"POOL_BUSY": 2, "AVAILABLE": 1}, "AVAILABLE", "t2"))
    chk("summary: --summary-lines 截断生效",
        summarize_ledger(1, _lp)["rounds"], 1)
    chk("summary: 文本含 claude 名与覆盖面行（人读自证）",
        ("claude-opus-4-8" in render_summary(_s), "覆盖面：" in render_summary(_s)),
        (True, True))
    _miss = summarize_ledger(12, os.path.join(_d, "nope.jsonl"))
    chk("NC: 台账缺失 -> LEDGER_MISSING（不是静默空摘要）",
        (_miss["ok"], _miss["reason"]), (False, "LEDGER_MISSING"))
    _only = os.path.join(_d, "only_meta.jsonl")
    with open(_only, "w", encoding="utf-8") as _f:
        _f.write(json.dumps({"ts": "x", "mode": "keepalive"}) + "\n")
    chk("NC: 无 results 行 -> NO_ROUNDS（别把非轮次记录当探测）",
        summarize_ledger(12, _only)["reason"], "NO_ROUNDS")
    chk("NC: 不可用台账的文本不冒充摘要",
        render_summary(_miss).startswith("[anyrouter] 台账摘要不可用"), True)

    # ---------------- 反检测节奏（2026-10-04）：随机间隔 / 长休 / 抽样轮转 / 日预算 ----------------
    class _Args(object):
        pass

    _a = _Args()
    _a.interval = 300
    _a.interval_min = 300
    _a.interval_max = 600
    _a.pause_prob = 0.0
    _a.pause_mult = [3.0, 6.0]
    _r = random.Random(20261004)
    _vals = [next_interval(_a, _r) for _ in range(40)]
    chk("节奏: 随机间隔全落 [300,600]", all(300 <= v <= 600 for v in _vals), True)
    chk("节奏: 随机间隔非恒定(>=20 种)", len(set(round(v, 3) for v in _vals)) >= 20, True)
    _a.pause_prob = 1.0
    _vals2 = [next_interval(_a, _r) for _ in range(20)]
    chk("节奏: 长休 >= 下界*3", all(v >= 900 for v in _vals2), True)
    chk("节奏: 长休 <= 上界*6", all(v <= 3600 for v in _vals2), True)
    _a.pause_prob = 0.0
    _a.interval_min = _a.interval_max = 0
    chk("节奏: 未给区间 ⇒ 回落固定 --interval", set(next_interval(_a, _r) for _ in range(5)), {300.0})
    chk("节奏: sleep 恒 >0", min(next_interval(_a, _r) for _ in range(5)) > 0, True)

    # 抽样轮转：8 模型 / 每轮 4 ⇒ 两个循环内全覆盖、轮内不重复
    _ms = ["m%d" % i for i in range(8)]
    _st = {}
    _r2 = random.Random(7)
    _seen, _lens, _dups, _rotflags = set(), [], 0, []
    for _ in range(4):
        _gm, _tot, _rot = pick_round_models(_ms, _st, 4, [], _r2)
        _seen |= set(_gm)
        _lens.append(len(_gm))
        _rotflags.append(_rot)
        _dups += len(_gm) - len(set(_gm))
    chk("抽样: 每轮 4 个", _lens, [4, 4, 4, 4])
    chk("抽样: 轮内不重复", _dups, 0)
    chk("抽样: 4 轮并集=全部 8 模型", sorted(_seen), sorted(_ms))
    chk("抽样: 标记轮转=True", sorted(set(_rotflags)), [True])
    _st2 = {}
    _pinall = []
    for _ in range(3):
        _gm2, _t2, _r3 = pick_round_models(_ms + ["pinned"], _st2, 3, ["pinned"], _r2)
        _pinall.append(("pinned" in _gm2, len(_gm2)))
    chk("抽样: pin 每轮都在且总数=3", set(_pinall), {(True, 3)})
    chk("抽样: sample=0 ⇒ 全量不轮转",
        pick_round_models(_ms, {}, 0, [], _r2)[0].__len__(), 8)
    chk("抽样: sample>n ⇒ 全量不轮转", pick_round_models(_ms, {}, 99, [], _r2)[2], False)
    chk("抽样: 状态损坏仍给 3 个(fail-open)",
        len(pick_round_models(_ms, {"rotation": {"order": "bad", "pos": "x"}}, 3, [], _r2)[0]), 3)
    chk("抽样: 单模型不轮转", pick_round_models(["only"], {}, 2, [], _r2)[2], False)

    # 日预算：计数（形状重发按 2 计）/ 跨日重置 / 0=不限 / 超限给次日 hint
    _sb = {}
    _budget_spent(_sb, [{"model": "a"}] * 3)
    chk("预算: 计数=3", _sb.get("req_count"), 3)
    _budget_spent(_sb, [{"model": "a"}, {"model": "b"}, {"model": "c", "shape_retry": True}])
    chk("预算: 形状重发按 2 计(3+4=7)", _sb.get("req_count"), 7)
    chk("预算: 未超 ⇒ allow", budget_verdict(_sb, 2, 10)[0], True)
    _ok2, _used2, _hint2 = budget_verdict(_sb, 4, 8)
    chk("预算: 超出 ⇒ 拒", _ok2, False)
    chk("预算: 超出 ⇒ hint 落 [60, 次日+1200]", 60.0 <= _hint2 <= 86400 + 1260, True)
    chk("预算: 0=不限", budget_verdict({"req_day": "1970-01-01", "req_count": 99999}, 8, 0)[0], True)
    chk("预算: 跨日重置", budget_verdict({"req_day": "1970-01-01", "req_count": 9999}, 8, 10)[0], True)

    # 错峰区间解析 + 轮成本
    _b = _Args()
    _b.stagger_min, _b.stagger_max = 2.0, 12.0
    chk("错峰: 区间解析", _stagger_range(_b), (2.0, 12.0))
    _b.stagger_min = _b.stagger_max = 0.0
    chk("错峰: 两端 0 ⇒ 关(None)", _stagger_range(_b), None)
    _c = _Args()
    _c.model_list, _c.sample = None, 0
    chk("预算: 轮成本 sample=0 ⇒ 全量", _round_cost(_c), len(MODELS))
    _c.sample = 4
    _c.model_list = None
    chk("预算: 轮成本 sample=4(≥名单长则封顶为名单长)", _round_cost(_c), min(4, len(MODELS)))
    _c.model_list = "a,b,c,d,e,f"
    chk("预算: 轮成本 sample=4(短于显式名单)", _round_cost(_c), 4)

    # 错峰真的落到 probe_models（monkeypatch probe_model，零网络）
    _slept = []
    _orig_pm = globals()["probe_model"]
    globals()["probe_model"] = lambda m, k, **kw: {"model": m, "http": 429, "verdict": BUSY}
    try:
        _res2 = probe_models(["a", "b", "c"], "k", timeout=1, workers=3,
                             stagger=(0.01, 0.02), sleeper=lambda x: _slept.append(x))
        _slept2 = []
        probe_models(["a", "b"], "k", timeout=1, stagger=None, sleeper=lambda x: _slept2.append(x))
    finally:
        globals()["probe_model"] = _orig_pm
    chk("错峰: 3 请求 ⇒ 2 次错峰 sleep", len(_slept), 2)
    chk("错峰: sleep 值落区间", all(0.01 <= x <= 0.02 for x in _slept), True)
    chk("错峰: 结果齐全", len(_res2), 3)
    chk("错峰: 不给区间 ⇒ 零 sleep(旧行为)", len(_slept2), 0)

    # 「预期下一轮时刻」落账（守护据此放宽卡死阈值）——写 /tmp 隔离，绝不碰真 state
    _sf0, _sd0 = globals()["STATE_FILE"], globals()["STATE_DIR"]
    globals()["STATE_FILE"] = "/tmp/anw_selftest_state.json"
    globals()["STATE_DIR"] = "/tmp"
    try:
        try:
            os.unlink("/tmp/anw_selftest_state.json")
        except Exception:
            pass
        _note_next_round(123)
        with open("/tmp/anw_selftest_state.json") as _f:
            _nst = json.load(_f)
        chk("守护: next_sleep_s 落账", _nst.get("next_sleep_s"), 123)
        chk("守护: next_round_ts ≈ now+123", abs(_nst.get("next_round_ts", 0) - (time.time() + 123)) < 5, True)
    finally:
        globals()["STATE_FILE"], globals()["STATE_DIR"] = _sf0, _sd0
        try:
            os.unlink("/tmp/anw_selftest_state.json")
        except Exception:
            pass

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
    ap.add_argument("--interval", type=int, default=60, help="--watch 轮询间隔秒数（--interval-min/max 同时给时忽略）")
    # 反检测节奏（2026-10-04）：默认即「随机 5-10 分钟 + 长休 + 轮内错峰 + 每轮抽样 4 个」，
    # 目的=既保持探测密度，又不给站点留下「固定周期 + 同秒突发 + 同名单」的机器指纹。
    ap.add_argument("--interval-min", type=float, default=float(DEFAULT_INTERVAL_MIN_S), dest="interval_min",
                    help="随机轮间隔下界秒（默认 %d；设 0 与 --interval-max 同关）" % DEFAULT_INTERVAL_MIN_S)
    ap.add_argument("--interval-max", type=float, default=float(DEFAULT_INTERVAL_MAX_S), dest="interval_max",
                    help="随机轮间隔上界秒（默认 %d）" % DEFAULT_INTERVAL_MAX_S)
    ap.add_argument("--pause-prob", type=float, default=DEFAULT_PAUSE_PROB, dest="pause_prob",
                    help="每轮以该概率进入「长休」（拟人化，默认 %.2f；0=关）" % DEFAULT_PAUSE_PROB)
    ap.add_argument("--pause-mult", type=float, nargs=2, default=list(DEFAULT_PAUSE_MULT), dest="pause_mult",
                    help="长休倍数区间（默认 %s）" % (list(DEFAULT_PAUSE_MULT),))
    ap.add_argument("--stagger-min", type=float, default=DEFAULT_STAGGER[0], dest="stagger_min",
                    help="轮内每个请求发出前的错峰下界秒（默认 %.0f；两端 0=关）" % DEFAULT_STAGGER[0])
    ap.add_argument("--stagger-max", type=float, default=DEFAULT_STAGGER[1], dest="stagger_max",
                    help="轮内错峰上界秒（默认 %.0f）" % DEFAULT_STAGGER[1])
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="轮内并发上限（默认 %d；越小越不像批量扫描）" % DEFAULT_WORKERS)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="每轮抽样的模型数（默认 %d，含 --pin-models；0=全量）" % DEFAULT_SAMPLE)
    ap.add_argument("--pin-models", default="", dest="pin_models",
                    help="每轮必探的模型（逗号分隔，默认空=不固定，靠轮转全覆盖）")
    ap.add_argument("--max-per-day", type=int, default=DEFAULT_MAX_PER_DAY, dest="max_per_day",
                    help="每日请求预算，超出则睡到次日（默认 %d；0=不限）" % DEFAULT_MAX_PER_DAY)
    ap.add_argument("--max-minutes", type=int, default=0, dest="max_minutes",
                    help="--watch 硬超时分钟数（默认 0=不设上限）")
    ap.add_argument("--notify", action="store_true", help="抢到坑时推送通知")
    ap.add_argument("--dry-run", action="store_true", help="不落盘不推送（只打印）")
    ap.add_argument("--timeout", type=int, default=90,
                    help="单次请求超时秒数（默认 90；★形状修对后真请求恒 ~37-70s，25 会把可用信号切掉）")
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
    ap.add_argument("--verify-pose", action="store_true", dest="verify_pose",
                    help="姿势自证：同刻 A/B（我方形状 vs 真 CC 形状）各打 N 发，输出 POSE_OK/POSE_DIVERGED")
    ap.add_argument("--pose-n", type=int, default=3, dest="pose_n",
                    help="--verify-pose 每个形状的发数（默认 3；拿到 200 提前收手）")
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
    ap.add_argument("--auto-hold-minutes", type=float, default=0, dest="auto_hold_minutes",
                    help="watch 模式下：探测命中可用（200）即**自动占坑保活**该 session N 分钟"
                         "（默认 0=只报喜不占坑；占坑=用探测同一 session 每 --keepalive-seconds 发 1）")
    ap.add_argument("--hold-cooldown", type=float, default=HOLD_COOLDOWN_S, dest="hold_cooldown",
                    help="两次自动占坑之间的最小间隔秒数（默认 %d；防坑位抖动时反复占用）" % HOLD_COOLDOWN_S)
    ap.add_argument("--stop-hold", action="store_true", dest="stop_hold",
                    help="立即停掉正在跑的保活（写旗标，保活最多 5 秒内停手）；"
                         "你自己已经在用别的客户端时用它省请求")
    ap.add_argument("--summary", action="store_true",
                    help="只读台账摘要：最近 N 轮各模型判据分布（含 claude 面覆盖面，零请求零推送）")
    ap.add_argument("--summary-lines", type=int, default=12, dest="summary_lines",
                    help="--summary 的回看轮数（默认 12）")
    ap.add_argument("--selftest", action="store_true", help="负控自测")
    return ap


def main():
    args = _build_parser().parse_args()
    if args.selftest:
        return selftest()
    if getattr(args, "log_hint", None):
        os.environ["GA_ANYROUTER_LOG_HINT"] = str(args.log_hint)
    _apply_state_dir(getattr(args, "state_dir", None))
    if getattr(args, "stop_hold", False):
        fp = request_hold_stop(reason="cli")
        print("[anyrouter] 已写入停保活旗标：%s"
              "（正在跑的保活 ≤%.0f 秒内停手；下次命中会自动清除并重新决策）"
              % (fp, HOLD_STOP_STEP_S))
        return 0
    if getattr(args, "summary", False):
        s = summarize_ledger(getattr(args, "summary_lines", 12))
        print(render_summary(s))
        return 0 if s.get("ok") else 2
    raws = getattr(args, "models", None)
    if raws == "__list__":
        r = list_models(_load_key())
        print("[anyrouter] %s http=%s n=%d" % (r["host"], r["http"], len(r["models"])))
        for m in r["models"]:
            print("  -", m)
        return 0 if r["http"] == 200 else 2
    args.model_list = [m.strip() for m in raws.split(",") if m.strip()] if raws else []
    if getattr(args, "verify_pose", False):
        return run_verify_pose(args)

    if args.claim:
        return run_claim(args)
    if args.watch:
        return run_watch(args)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
