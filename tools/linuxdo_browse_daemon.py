#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linuxdo_browse_daemon — L站 TL3 维护型只读浏览后台守护
========================================================
定位（2026-10-05 用户授权，整合 linuxdo_browse_sop × linuxdo_trust_level_sop）:
  TL3 等级检测**不满足**时 → 后台执行只读浏览（SOP 链路），**直到满足**即停。
  零 LLM token：纯 HTTP 打 TMWebDriver 桥（http://127.0.0.1:18766/link），
  复用已登录 linux.do 的浏览器 tab（与 tools/dpdomain_renew.py 同款零依赖桥）。

架构（2026-10-05 用户定稿，五个 SOP 同一入口路由）:
  等级检测（本守护内，每天 1 次、时刻随机）＝**路由入口**；
  缺什么补什么、缺几项补几项（多命中并行，非 if-else 单分支）：
    浏览类  → 本守护自己按随机节奏只读浏览（本文件即浏览 SOP 的执行面）
    回复类  → 排队 pending_acts.reply，由 tools/linuxdo_acts.py 按 reply 档随机节奏执行
    送赞类  → 排队 pending_acts.like，同上（like 档）
    获赞不足 → 用户口径：优先自己发帖（获赞概率高），其次在别人帖下回帖 ⇒ earn 桶：
              有已批准草稿则发帖（post 档：日≤1、间隔≥24h、周≤3），否则并入回帖排队
    其余（访问天数/活跃程度/登录/举报/禁言/封禁）→ 只能人工，仅私信提示
  五个 SOP 均可由「提示词」单独唤醒（不设定点任务）；守护本身由保活任务零站点维持。

只读边界（本守护自身）:
  零发帖 / 零回复 / 零点赞 / 零关注 / 零领奖；不模拟登录（封号风险）。
  写操作（回复/点赞/发帖）只经 tools/linuxdo_acts.py 执行，且必经等级路由或提示词显式触发。

能推进的 TL3 项（浏览是只读行为，2026-10-03 实测缺口=浏览帖子 12571/20000）:
  - 浏览话题 / 浏览帖子：进帖 + 深翻楼层即涨
  - 访问天数：只能用户本人真实登录日积月累，本守护不代刷

判停（按页面标记，禁数值机械比较 —— SOP 红线，上限类指标天然规避）:
  connect 页: 解析 class token（unmet 优先，剔 .tl3-bar-fill 进度条）→ 待完成=unmet 数
  首页等级卡: ✓/○ 四元组 → 待完成=○M（须与头部「○M 待完成」对账）
  先试 connect（title 非「请稍候…」且正文含「信任级别」才算读到），失败回落首页卡；
  两路都判不了 → rc=3（不瞎判、不假绿）。

防检测参数（后台版节奏比单任务版更稀；用户强调「悠着点别封号」）:
  - 帖间随机 90–240s（25% 概率 240–420s）
  - 深翻随机 14–26 步 × 0.8–1.5s，步长 380–900px（随机）
  - 每 batch 随机混入 2–4 次「只逛列表页」不进帖（拟人翻列表）
  - 候选页序随机（/latest.json?page=<rand>），已浏览 id 去重
  - --batch 默认 20 帖/批；--daily-cap 每日话题上限（默认取内核 browse 档 daily_cap=200，
    每日 ±20 内随机定一次、跨重启稳定）
  - 静默窗结束后随机晨抖 0–60min 再开工（防每天准点开工的机械特征）
  - 段预算（2026-10-06 起）由内核 day_segments 决定：browse 档每天 6–8 段 × 85–160min，
    段位按日期种子派生（守护重启不重置段位），段外一律等下一段；
    --max-session-min（默认 110min）仅在**内核缺席时兜底**用旧行为（跑满休 25–53min）。
    ★数值以 tools/linuxdo_antidetect.py 的 PROFILES 为准，改档须同批改本注释。
  - 静默窗默认 01:00–07:30 不浏览、不查等级
  - 等级前置检查：**每天 1 次**、**上午 08:00–10:00 内随机**（2026-10-06 用户要求：原
    对数正态会抽到傍晚/夜间，判级后活动窗只剩几小时，缺项（浏览/回帖/点赞）补不完；
    节奏仍由内核 linuxdo_antidetect.level_check_ts 单一源决定，窗内抽点且守 90min 最小间隔。
    LEVEL_CHECK_GAP 仅作内核缺席时的兜底）

用法:
  python3 tools/linuxdo_browse_daemon.py status            # 只读：state+最近等级
  python3 tools/linuxdo_browse_daemon.py check             # 只读：读一次等级卡
  python3 tools/linuxdo_browse_daemon.py browse --batch 10 # 浏览一批（不判停）
  python3 tools/linuxdo_browse_daemon.py watch             # 长驻：判停循环（bg_task 起）
  python3 tools/linuxdo_browse_daemon.py --selftest        # 离线自测

退出码: 0=正常停(含判停「已满足」) 1=缺口仍在但达上限/批结束 2=桥不可达/无linux.do tab
        3=两路都判不了等级(登录失效/CF双卡，需人工) 4=用法错误 5=自测失败
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

# ---- 拟人层单一源（2026-10-05）：浏览路径必须与 acts（回复/点赞/发帖）共用同一内核，
#      否则「浏览」是独立实现、参数漂移，用户要求的通用抽象就不成立。
try:
    import linuxdo_humanize as hu
except Exception:                                       # 非 tools/ 目录被 import 时的兜底
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import linuxdo_humanize as hu
    except Exception:
        hu = None

GA_ROOT = os.environ.get("GA_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BASE = os.path.join(GA_ROOT, "temp", "linuxdo_browse_daemon")

# ---- 心跳自声明（R558 -------------------------------------------------------
# 事故：静默窗(01:00–07:30)长睡 6h 期间，state.json/browse_log 都不刷新 ⇒ 看门狗
# (tools/linuxdo_daemon_watchd.py) 只按 mtime 判「停滞 26645s」⇒ 误重启，而
# bg_task 见同名任务已在 RUNNING 直接 rc=4 ⇒ 又被告警成「重启失败」。
# 修法：守护每轮/每次睡前把自己的相位与**下次预计醒来时刻**写进 heartbeat.json，
# 让「长睡」可被外部自证（只写自己的运行态文件，零站点请求，不影响随机节奏）。
def heartbeat_path():
    return os.path.join(BASE, "heartbeat.json")


def write_heartbeat(phase, next_wake_ts=None):
    hb = {"ts": now_str(), "epoch": time.time(), "pid": os.getpid(),
          "phase": phase, "next_wake_ts": next_wake_ts}
    try:
        os.makedirs(BASE, exist_ok=True)
        tmp = heartbeat_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hb, f, ensure_ascii=False)
        os.replace(tmp, heartbeat_path())
    except Exception as e:
        print("[%s] heartbeat write failed: %r" % (now_str(), e), flush=True)
    return hb


# ---- 主站 host 判据单一源（2026-10-06 停摆事故）-------------------------------
# 事故：唯一 linux.do 标签被导航到 connect.linux.do 后，pick_session 判「无候选」而
# try_ensure_tab 用子串 "linux.do" 判「已有」⇒ 自愈不触发、空转 64min 零产出。
# 两处共用下面这一份判据，禁止再写 `in "linux.do"` 之类子串判据。
MAIN_HOSTS = ("linux.do", "www.linux.do")


def _url_host(u):
    """从 URL 串取 host（小写，带 userinfo/端口也能取对）；解析失败回空串。"""
    try:
        return (urlparse(str(u)).hostname or "").lower()
    except Exception:
        return ""


def _is_main_host(host):
    """是否 linux.do 主站（connect./sharedchat 等含 "linux.do" 子串但非主站）。"""
    return isinstance(host, str) and host.strip().lower() in MAIN_HOSTS
BRIDGE = os.environ.get("LJQ_BRIDGE", "http://127.0.0.1:18766")
HINT = "linux.do"

RC_OK, RC_GAPS, RC_NO_SESSION, RC_UNJUDGEABLE, RC_USAGE, RC_SELFTEST = 0, 1, 2, 3, 4, 5

# 防检测参数（后台版）——2026-10-06 三轮：日上限 160→200(每日±20 抖动；段预算 6–8 段×85–160min)
# 注:以下是**内核缺席时的回退档**，正常路径一律走 tools/linuxdo_antidetect.py 单一源(见 _profiles)。
GAP_RANGE = (110, 260)
GAP_LONG_RANGE = (300, 600)
SCROLL_STEPS = (22, 30)      # 慢滚多步：步长<半楼 ⇒ 每楼≥2个 tick 覆盖
SCROLL_DT = (1.4, 2.0)
SCROLL_AMP = (300, 440)
BOTTOM_STAY = (90, 125)      # 底部驻留秒——跨 Discourse ~60s timings flush 周期（2026-10-05 实测根因）；二轮拉长更像细看
ROLLBACK_STEPS = 10          # 回滚给每楼二次 tick 机会
LOITER_P_LIST = (2, 5)
LIGHT_P = 0.25              # 25% 浅读（刷到不感兴趣划走型）：每帖都读到底=机器味（2026-10-05 反检测审查）
LIGHT_STEPS = (4, 6)
LIGHT_DWELL = (15, 30)      # 浅读驻留秒
BATCH_DEFAULT = 20
DAILY_CAP_DEFAULT = 200
DAILY_CAP_JITTER = 20       # 每日 cap 在 ±JITTER 内随机——每天固定同一数字=机械特征
SESSION_MIN_DEFAULT = 110   # 单段连续浏览分钟数（拉长=更像刷入迷的真人，提高占空比）
SESSION_REST = (1500, 3200) # 段间休息秒（25–53min）
MORNING_JITTER = (0, 3600)  # 静默窗结束后随机延迟 0–60min 再开工——真人不会每天准点开始
QUIET_WINDOW = (1, 0, 7, 30)          # 01:00–07:30 静默
# 等级前置检查节流（2026-10-05 用户要求）：不再每轮都读等级卡 —— 每天 1–3 次、时刻随机。
# 「不满足 ⇒ 浏览补足」的路由语义不变；两次检查之间沿用上次判定继续补足。
LEVEL_CHECK_GAP = (12 * 3600, 20 * 3600)   # 兜底/展示用；真实节奏见内核 level_check_ts（每日 1 次错峰）
try:                                        # 统一随机化内核（单一源）
    import linuxdo_antidetect as _ad
except Exception:                           # 内核缺席 ⇒ 回退本地兜底，绝不静默假绿
    _ad = None
# 缺项路由（2026-10-05 用户定义）：未达标项分四路 ——
#   浏览类 → 浏览 SOP（daemon 自身随机节奏）；
#   回复类 → 回帖执行器 tools/linuxdo_acts.py（排队后由 acts 自己的随机段执行，与等级检查时刻解耦）；
#   送赞类 → 点赞执行器（同上，独立 cap/节奏）；
#   其余（获赞/访问天数/活跃程度/登录/举报/禁言/封禁）只能人工，只通知不代操作。
MANUAL_NAME_HINTS = ("访问天数", "活跃程度", "登录", "举报", "禁言", "封禁")
EARN_KINDS = ("获赞",)          # 获赞不足：用户口径=优先自己发帖，其次到别人帖下回帖
BROWSE_KINDS = ("浏览话题", "浏览帖子", "浏览", "阅读")
REPLY_KINDS = ("回复话题", "回复帖子", "回复")
LIKE_KINDS = ("点赞", "送赞")
# 排队上限：一次等级检查最多排多少（真正的日 cap/节奏由 acts 内核管，这里只是欠账量）
QUEUE_REPLY_MAX = 2
QUEUE_LIKE_MAX = 3
# 等级检查与动作执行的解耦：等级检查即时排队，动作由 acts 随机段内执行；
# 每轮最多各做 1 条，且两轮之间至少歇 20–45min（防「检查完立刻动手」的固定时刻特征）。
ACTS_ROUND_GAP = (1200, 2700)
FORGE_MIN_GAP_S = 3 * 86400       # 无草稿时后台锻造的最小间隔（3 天）
FORGE_JITTER_S = 4 * 86400        # 再叠 0~4 天随机抖动 ⇒ 实际 3~7 天，禁固定节奏
FORGE_STATE = os.environ.get("LJQ_FORGE_STATE") or os.path.join(BASE, "post_forge.json")
# connect 页两条路径结构不同：connect=({met:bool,name:'当前/门槛'}，无指标名)；首页卡=({mark, name, cur, req})。
# connect 实测固定 12 项顺序（2026-10-05 level_log.jsonl）⇒ 仅当这些项的**门槛值全部吻合**才启用顺序映射，
# 否则一律按「未识别」处理（fail-safe：只通知人，绝不误判为可自动补）。
CONNECT_ITEM_MAP = (
    (0, 50, "访问天数"),
    (1, 500, "浏览话题"),
    (2, 20000, "浏览帖子"),
    (3, 10, "回复话题"),
    (4, 30, "点赞"),
    (5, 20, "获赞"),
)
BATCH_GAP_MULT = 1.5                    # 批间休息 = 内核 gap_long 档 ×1.5（同一来源的批级放大）
SESS_RETRY_MAX = 8                      # pick_session/判级连续失败达上限才退出
SESS_RETRY_SHORT = (20, 40)             # 首段短退避（秒）：覆盖 Chrome 冷启动/扩展重连成
SESS_RETRY_SHORT_MAX = 6                # 前 N 次用短退避，之后转长退避
SESS_RETRY_SLEEP = (600, 1200)          # 断链长退避间隔 10–20min
FRONT_NONE_TOLERANCE = 3                # 前台探测连续失败≥3次按后台继续（fail-open 防永停）
LATEST_PAGE_MAX = 9

# ---- 风控保险丝（2026-10-05 用户要求：借 anyrouter 被封教训）-----------------
# anyrouter 教训三条：①高频自动请求累积 → 封号；②把异常当常态（429/503 当"供给态"
# 一路重试，直到 403「用户已被封禁」才发现）；③无自动告警，靠人偶然看见。
# 这里对应：任何风控信号一律 记 state + 飞书私信 + 长暂停，绝不静默硬撞。
RISK_PAUSE = (3600, 7200)        # 命中单次风控信号 → 暂停 60–120min
RISK_ESCALATE_N = 3              # 同类信号当日累计 ≥3 ⇒ 当日停止浏览 + 强制告警
RISK_ALERT_COOLDOWN_H = 6        # 同类告警最小间隔（小时），避免刷屏
RISK_TEXT_HINTS = (
    # 顺序=优先级：封禁 > CF 挑战 > 限流 > 登录失效
    ("BANNED", r"已被?封禁|账号被封|封号|account suspended|user has been banned|you are banned"),
    ("CF_CHALLENGE", r"Just a moment|cf-chl|Attention Required|Cloudflare|安全验证|人机验证|请稍候"),
    ("RATE_LIMIT", r"Too Many Requests|请求过于频繁|操作过于频繁|rate limit exceeded"),
    ("LOGIN_REQUIRED", r"not_logged_in|请先登录|需要登录|你要登录|Sign Up|Log In"),
)
# 强信号=必须长暂停 + 告警；CF_CHALLENGE 常见且多半自愈 ⇒ 只做轻量计数（避免误伤成小时级停机）
RISK_STRONG = ("BANNED", "RATE_LIMIT", "FORBIDDEN", "LOGIN_REQUIRED")
REST_DAY_P = 1.0 / 7.0           # 随机休息日：平均每周 1 天完全不浏览（降总量+拟人）
REST_MIN_GAP_DAYS = 3            # 两次休息日至少间隔 3 天（不连休）

# ---- 内核单一源（2026-10-06 R546，修 R545 缺陷③）--------------------------------
# 节奏参数一律以 linuxdo_antidetect.profile("browse") 为**唯一来源**：daemon 不再自带
# 第二份常量（旧实现里 GAP_LONG_RANGE/GAP_LONG_P 只被自测断言引用＝死常量，实际节奏
# 与内核漂移不自知）。内核缺席时保留上面的本地默认值（fail-safe，绝不静默改行为）。
if _ad is not None:
    try:
        _P = _ad.profile("browse")
        GAP_RANGE = tuple(_P["gap"])
        GAP_LONG_RANGE = tuple(_P["gap_long"])
        DAILY_CAP_DEFAULT = int(_P["daily_cap"])
        DAILY_CAP_JITTER = int(_P["cap_jitter"])
        MORNING_JITTER = tuple(_P["morning_jitter"])
        QUIET_WINDOW = tuple(_P["quiet"])
        REST_DAY_P = float(_P["rest_p"])
        REST_MIN_GAP_DAYS = int(_P["rest_min_gap"])
        SEG_N = tuple(_P["seg_n"])
        SEG_LEN = tuple(_P["seg_len"])
    except Exception as _e:                 # 内核结构变了 ⇒ 明示，不静默
        print("[warn] antidetect profile 读取失败，沿用本地默认节奏: %s" % _e, flush=True)


def _batch_gap():
    """批间休息（秒）：内核长休档（gap_long）×BATCH_GAP_MULT，保持「同一随机内核」单一源。

    R545 缺陷③：旧实现自持 INTER_BATCH_GAP=(420,900) 第二份常量，与内核漂移不自知。
    内核缺席时回落到本地 GAP_LONG_RANGE（同为内核实测值的镜像，非另立口径）。
    """
    rng_lo_hi = None
    if _ad is not None:
        try:
            lo, hi = _ad.profile("browse")["gap_long"]
            if hi > lo:
                rng_lo_hi = (lo, hi)
        except Exception:
            rng_lo_hi = None
    lo, hi = rng_lo_hi or tuple(GAP_LONG_RANGE)
    return round(random.uniform(lo, hi) * BATCH_GAP_MULT, 1)


def _bridge(payload, timeout=90):
    req = urllib.request.Request(
        BRIDGE.rstrip("/") + "/link",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data.get("r", data)


def list_sessions(timeout=20):
    try:
        r = _bridge({"cmd": "get_all_sessions"}, timeout=timeout)
    except Exception as e:
        raise BridgeError("bridge 不可达: %s" % e)
    if isinstance(r, dict):
        r = r.get("data", r.get("sessions", []))
    out = []
    for s in r or []:
        if isinstance(s, dict) and s.get("id") is not None:
            out.append({"id": str(s["id"]), "url": s.get("url") or "", "title": s.get("title") or ""})
    return out


class BridgeError(Exception):
    pass


def run_js(session_id, code, timeout=60):
    r = _bridge({"cmd": "execute_js", "sessionId": session_id,
                 "code": code, "timeout": str(int(timeout))}, timeout=timeout + 20)
    if isinstance(r, dict) and "data" in r and isinstance(r.get("data"), str):
        raw = r["data"]
    elif isinstance(r, dict) and r.get("error"):
        return {"ok": False, "error": str(r["error"])}
    else:
        raw = r
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": True, "raw": raw}
    return raw


FRONT_JS = ("(function(){var v=(document.visibilityState==='visible');"
            "var f=!!(document.hasFocus&&document.hasFocus());"
            "return JSON.stringify({visible:v,focused:f,front:v})})()")


def tab_is_front(session):
    """linux.do tab 是否可见（用户可能正在看）。

    front 判据 = visibilityState==='visible'：分屏/焦点在终端但屏幕可见也算前台，
    宁可错跳绝不抢用户正在看的 tab。探测失败返回 None（fail-closed 由调用方定）。
    """
    try:
        res = run_js(session["id"], FRONT_JS, timeout=15)
    except Exception:
        return None
    if isinstance(res, dict) and "front" in res:
        return bool(res["front"])
    return None


def front_decision(front, none_cnt):
    """返回 (skip: bool, why: str)。None 且连续失败未达容忍 ⇒ fail-closed 跳过。"""
    if front is True:
        return True, "用户在前台看 linux.do，跳过本轮浏览"
    if front is None:
        if none_cnt < FRONT_NONE_TOLERANCE:
            return True, "前台探测失败(fail-closed %d/%d)，跳过本轮浏览" % (none_cnt + 1, FRONT_NONE_TOLERANCE)
        return False, "前台探测连续失败 %d 次，按后台继续(fail-open)" % none_cnt
    return False, "tab 后台，正常浏览"


def pick_session(prefer=None, timeout=20):
    """挑一个真正位于 linux.do 的会话；找不到返回 (None, 诊断串)。"""
    sessions = list_sessions(timeout=timeout)
    if not sessions:
        return None, "浏览器桥无任何会话（Chrome 未运行 / 扩展未连接 / 驱动未启动？）"
    cands = [s for s in sessions if prefer and s["id"] == str(prefer)]
    cands += [s for s in sessions if HINT in (s["url"] or "") and "//connect." not in (s["url"] or "")]
    seen, ordered = set(), []
    for s in cands:
        if s["id"] not in seen:
            seen.add(s["id"])
            ordered.append(s)
    if not ordered:
        return None, "无 %s 标签页；当前会话: %s" % (
            HINT, "; ".join("%s=%s" % (s["id"], s["url"][:60]) for s in sessions[:8]))
    for s in ordered:
        probe = run_js(s["id"], "(()=>location.host)()", timeout=15)
        host = probe.get("raw") if isinstance(probe, dict) else probe
        # 精确 host 判定（单一源 _is_main_host）：connect.linux.do 也含 "linux.do" 子串但
        # 无 Discourse 环境（2026-10-05 实测误选 connect Error 页 tab → routeTo 永远 need_loc）
        if _is_main_host(host):
            return s, ""
    return None, "候选标签页 %s 均不在 %s（可能已被导航走）" % ([s["id"] for s in ordered], HINT)


# ---------------------------------------------------------------- state
def load_state():
    os.makedirs(BASE, exist_ok=True)
    p = os.path.join(BASE, "state.json")
    st = None
    if os.path.exists(p):
        try:
            st = json.load(open(p, encoding="utf-8"))
        except Exception:
            st = None
    if not isinstance(st, dict):
        st = {}
    # 健壮化：任何缺键都补默认值 —— 防「state 被局部 dict 覆盖」类事故导致 KeyError 崩守护
    st.setdefault("seen_ids", [])
    st.setdefault("daily", {})
    st.setdefault("level", {})
    st.setdefault("browsed_total", 0)
    st.setdefault("rest_days", {})
    st.setdefault("pending_acts", {})       # 等级缺项排队的回帖/点赞（由 acts 随机节奏消费）
    st.setdefault("level_skip", 0)          # 等级检查「节流轮次」计数（≠真实检查次数，见 R545 缺陷④）
    return st


def save_state(st):
    p = os.path.join(BASE, "state.json")
    tmp = p + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def today():
    return dt.datetime.now().strftime("%Y-%m-%d")


def log_jsonl(name, obj):
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 风控保险丝
# 2026-10-05：借 anyrouter 被封教训——异常绝不静默吞掉，一律落账 + 告警 + 暂停。
def notify(text):
    """飞书私信告警（复用 GA 既有通道；失败不致命，但必须留痕）。"""
    try:
        rs = os.path.join(GA_ROOT, "reflect_scripts")
        if rs not in sys.path:
            sys.path.insert(0, rs)
        from reflect_common import notify_feishu  # type: ignore
        notify_feishu(text)
        return True
    except Exception as e:
        print("[warn] 风控告警推送失败: %s" % str(e)[:160], flush=True)
        return False


def risk_kind_from_http(status):
    """HTTP 状态码 → 风控类别（None=非风控信号）。"""
    if status == 429:
        return "RATE_LIMIT"
    if status == 403:
        return "FORBIDDEN"
    if status == 401:
        return "LOGIN_REQUIRED"
    return None


def risk_kind_from_text(txt):
    """页面文本 → 风控类别。只认强特征，宁漏不误（避免把技术帖里的数字当信号）。"""
    t = (txt or "")[:4000]
    if not t:
        return None
    for kind, pat in RISK_TEXT_HINTS:
        if re.search(pat, t, re.I):
            return kind
    return None


class RiskAbort(Exception):
    """批内风控熔断（R563）：候选抓取阶段确认强风控信号 ⇒ 立即中止本批。

    旧行为：风控判定在 browse_batch **返回之后**才做，403 发生时本批照跑
    （2026-10-08 实测：19:56 抓到 403，20:00–22:02 仍浏览 20 帖，22:06 才暂停）
    ⇒ 违背「命中即停、绝不硬撞」。新行为：抓取阶段判定，批内零帖即熔断。
    """

    def __init__(self, kind, detail=""):
        Exception.__init__(self, "%s: %s" % (kind, str(detail)[:200]))
        self.kind = kind
        self.detail = detail


def note_risk(st, kind, detail=""):
    """记风控信号 + 去重告警 + 当日计数。返回 (count_today, alert_sent)。"""
    t = today()
    day = st.setdefault("risk", {}).setdefault("days", {}).setdefault(t, {})
    day[kind] = int(day.get(kind, 0)) + 1
    count = day[kind]
    st["risk"]["last"] = {"ts": now_str(), "kind": kind,
                          "detail": str(detail)[:300], "count_today": count}
    log_jsonl("risk_log.jsonl", {"ts": now_str(), "st": "RISK", "kind": kind,
                                 "detail": str(detail)[:300], "count_today": count})
    esc = count >= RISK_ESCALATE_N
    alerts = st.setdefault("risk_alert", {})
    key = "%s:%s" % (kind, t)
    due = True
    try:
        if alerts.get(key):
            due = (time.time() - float(alerts[key])) >= RISK_ALERT_COOLDOWN_H * 3600
    except Exception:
        due = True
    if esc or due:
        alerts[key] = time.time()
        notify("⚠️ linux.do 浏览守护·风控信号\n"
               "类型：%s\n"
               "今日同类第 %d 次%s\n"
               "详情：%s\n"
               "动作：暂停浏览 %d–%d 分钟（绝不硬撞）\n"
               "时间：%s" % (kind, count, "（已升级：当日停止）" if esc else "",
                           str(detail)[:200],
                           RISK_PAUSE[0] // 60, RISK_PAUSE[1] // 60, now_str()))
        save_state(st)
        return count, True
    save_state(st)
    return count, False


def handle_risk(st, kind, detail=""):
    """记信号 →（达阈值则当日停，否则长暂停）→ 返回 True 表示该 continue。"""
    cnt, _ = note_risk(st, kind, detail)
    if cnt >= RISK_ESCALATE_N:
        st["risk_stop_day"] = today()
        save_state(st)
        print("[%s] 风控升级 %s x%d → 当日停止浏览" % (now_str(), kind, cnt), flush=True)
        return True
    pause = random.uniform(*RISK_PAUSE)
    print("[%s] 风控信号 %s → 暂停 %.0f 分钟" % (now_str(), kind, pause / 60), flush=True)
    time.sleep(pause)
    return True


_SEG_CACHE = {"day": None, "segs": None}


def _day_segments_cached(day=None):
    """当日活动段表（内核确定性派生，按天缓存）——重启安全。

    返回 None 表示内核缺席（调用方回落旧行为）。同一天内重复调用结果逐字一致；
    跨天自动重算。缓存只在内存，不落盘（段表本身由 day seed 决定，无需持久化）。
    """
    if _ad is None:
        return None
    d = day or today()
    if _SEG_CACHE.get("day") == d and _SEG_CACHE.get("segs") is not None:
        return _SEG_CACHE["segs"]
    try:
        segs = _ad.day_segments(d, "browse")
    except Exception as e:
        print("[warn] day_segments 失败，回落旧段计时: %s" % e, flush=True)
        return None
    _SEG_CACHE["day"], _SEG_CACHE["segs"] = d, segs
    return segs


MAX_SEG_WAIT_S = 1800.0   # 段间单次等待上限：分段睡，每 ≤30min 回主循环（保心跳/保响应）


def segment_gate(now_ts=None, segs=None):
    """段闸门（单一源）：→ (可干活, 需等秒数, 原因)。

    ★ 2026-10-06 修（R548）：内核 in_segment() 返回 (bool, 段末ts)，旧写法
    `if not _ad.in_segment(...)` 里元组恒真 ⇒ not 恒假 ⇒ 段闸门从未生效
    （实测 daemon 在段外 12:40 仍落 DONE，「段间空档」行日志从未出现）；
    同一处的 `float(_ad.wait_hint(...))` 对 (秒, 原因) 元组取 float 必 TypeError，
    被上一坑掩盖成死代码。现两处均按内核签名解包，并由自测 T15g/T15h 负控锁死。
    内核缺席（_ad 为 None 或段表为空）⇒ (True, 0.0, "no_kernel")，保持「回退旧行为」。
    """
    if _ad is None:
        return True, 0.0, "no_kernel"
    if segs is None:
        segs = _day_segments_cached()
    if not segs:
        return True, 0.0, "no_kernel"
    t = time.time() if now_ts is None else now_ts
    ok, _end = _ad.in_segment(t, segs)          # 契约：元组
    if ok:
        return True, 0.0, "in_segment"
    w, why = _ad.wait_hint(t, segs)             # 契约：元组
    return False, max(60.0, min(float(w), MAX_SEG_WAIT_S)), why


def rest_day_today(st):
    """随机休息日：平均每 7 天 1 天完全不浏览（降总量 + 拟人）。判一次即落盘。"""
    t = today()
    rd = st.setdefault("rest_days", {})
    if t in rd:
        return bool(rd[t])
    days = sorted(rd.keys())
    if days:
        try:
            last = dt.datetime.strptime(days[-1], "%Y-%m-%d").date()
            if (dt.datetime.now().date() - last).days < REST_MIN_GAP_DAYS:
                rd[t] = False
                save_state(st)
                return False
        except Exception:
            pass
    v = random.random() < REST_DAY_P
    rd[t] = v
    for k in sorted(rd.keys())[:-60]:
        rd.pop(k, None)
    save_state(st)
    if v:
        log_jsonl("rest_log.jsonl", {"ts": now_str(), "st": "REST_DAY",
                                     "note": "今日随机休息，零浏览"})
    return v


def quiet_window_check(now=None):
    """在静默窗(01:00–07:30)内返回 True。"""
    now = now or dt.datetime.now()
    qh, qm, eh, em = QUIET_WINDOW
    t = now.hour * 60 + now.minute
    return (qh * 60 + qm) <= t < (eh * 60 + em)


def _ts_str(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _ts_of_today(hour, minute=0):
    """今日 hh:mm 的本地 epoch 秒（自测取样用，避开静默窗）。"""
    n = dt.datetime.now()
    return dt.datetime(n.year, n.month, n.day, int(hour), int(minute)).timestamp()


def level_check_due(st, now_ts=None):
    """等级前置检查是否到期。

    v2（2026-10-05）：节奏委托内核（单一源）——每天 1 次、错峰时刻、静默窗内不跑。
    两条保守例外（绝不永久沿用旧判定）：① 从未查过 ② 内核缺席（回退旧的 next_ts 口径）。
    """
    now_ts = time.time() if now_ts is None else now_ts
    if not st.get("level"):
        return True
    if _ad is not None:
        try:
            return bool(_ad.level_check_due(st, now_ts)[0])
        except Exception:
            pass
    return (st.get("level_next_ts") or 0) <= now_ts


def level_check_wait(st, now_ts=None):
    """守护用：距下次等级检查还有多少秒 + 人类可读时刻（内核缺席回退 next_ts）。"""
    now_ts = time.time() if now_ts is None else now_ts
    if _ad is not None:
        try:
            w, why = _ad.level_check_next_wait(st, now_ts)
            return float(w), why
        except Exception:
            pass
    left = max(0.0, float(st.get("level_next_ts") or 0) - now_ts)
    return left, "fallback"


def level_check_mark(st, now_ts=None):
    """查完记账：内核记 level_check；同时维护旧字段 level_next_ts（兼容展示与旧状态）。"""
    now_ts = time.time() if now_ts is None else now_ts
    st["level_next_ts"] = now_ts + random.uniform(*LEVEL_CHECK_GAP)
    if _ad is not None:
        try:
            _ad.level_check_mark(st, now_ts)
            w, _why = _ad.level_check_next_wait(st, now_ts)
            st["level_next_ts"] = now_ts + max(60.0, float(w))
        except Exception:
            pass
    return st["level_next_ts"]


def _item_unmet(it):
    """未达标判定：兼容两路径（connect=met 布尔；首页卡=mark ✓/○）。"""
    if "met" in it:
        return it.get("met") is False
    return it.get("mark") == "○"


def _req_of(it):
    """门槛值：首页卡=int req；connect=从 '13171/ 20000' 解析。"""
    if isinstance(it.get("req"), int):
        return it["req"]
    m = re.search(r"/\s*(\d+)", str(it.get("name") or ""))
    return int(m.group(1)) if m else None


def _map_connect_names(items):
    """connect 项按实测顺序且门槛值全部吻合 ⇒ {idx: 指标名}；否则 None（不可信，不映射）。"""
    if len(items) < len(CONNECT_ITEM_MAP):
        return None
    names = {}
    for idx, req, name in CONNECT_ITEM_MAP:
        if _req_of(items[idx]) != req:
            return None
        names[idx] = name
    return names


def classify_pending(lvl):
    """未达标项分五路：browse/reply/like/earn 可自动补；manual 须人工；unknown fail-safe 也通知。

    多命中语义（用户口径）：一轮等级检查里**每一项**独立入桶 ⇒ 同时缺 N 项就同时走 N 条路，
    不是 if-else 单分支。
    """
    items = lvl.get("items") or []
    names = _map_connect_names(items)
    manual, auto, unknown, reply, like, earn = [], [], [], [], [], []
    for i, it in enumerate(items):
        if not _item_unmet(it):
            continue
        nm = (names or {}).get(i) or str(it.get("name") or "")
        rec = {"name": nm, "cur": it.get("cur"), "req": _req_of(it)}
        if any(k in nm for k in BROWSE_KINDS):
            auto.append(rec)
        elif any(k in nm for k in REPLY_KINDS):
            reply.append(rec)
        elif any(k in nm for k in LIKE_KINDS):
            like.append(rec)
        elif any(k in nm for k in EARN_KINDS):
            earn.append(rec)
        elif any(k in nm for k in MANUAL_NAME_HINTS):
            manual.append(rec)
        else:
            unknown.append(rec)
    return {"manual": manual, "auto": auto, "unknown": unknown,
            "reply": reply, "like": like, "earn": earn}


def _deficit(rec, cap):
    """欠账量：cur/req 可解析就算差值，否则保守取 1；上限 cap。"""
    cur, req = rec.get("cur"), rec.get("req")
    try:
        n = int(req) - int(cur)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(int(cap), n if n > 0 else 1))


def _load_post_draft():
    """取一份待发草稿（temp/linuxdo_post_drafts/*.json，最早的优先）。

    草稿格式：{"title":..., "raw":..., "category":int, "tags":"AI,求助"}
    没有草稿 ⇒ 返回 None（获赞类缺项此时改走回帖路径，绝不自己编内容发帖）。
    """
    try:
        d = os.path.join(BASE, "linuxdo_post_drafts")
        if not os.path.isdir(d):
            return None
        files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        for fn in files:
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                    j = json.load(fh)
                if j.get("title") and j.get("raw"):
                    j["_file"] = fn
                    return j
            except Exception:
                continue
    except Exception:
        return None
    return None


def queue_pending_from_route(st, route):
    """把「回复类/送赞类/获赞类」欠项排进 pending_acts（不立即执行）。

    关键设计：排队与执行解耦 —— 等级检查即时排队，实际动作由 linuxdo_acts 的
    随机段/随机间隔决定，避免「检查完就动手」形成固定时刻特征。

    获赞类（用户口径）：优先自己发帖（获赞概率高）⇒ 有草稿排 post；
    无草稿则改走回帖（回帖同样可能被赞），并留痕说明，绝不自己编内容发帖。
    """
    pa = st.setdefault("pending_acts", {})
    added = {}
    for kind, key, cap in (("reply", "reply", QUEUE_REPLY_MAX),
                           ("like", "like", QUEUE_LIKE_MAX)):
        for rec in route.get(key) or []:
            n = _deficit(rec, cap)
            pa[kind] = max(int(pa.get(kind) or 0), n)
            added[kind] = pa[kind]
    if route.get("earn"):
        names = [r.get("name") for r in (route.get("earn") or [])]
        draft = _load_post_draft()
        if draft:
            pa["post"] = 1
            pa["post_src"] = "level_route"
            added["post"] = 1
            log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "QUEUED_POST",
                                         "reason": "获赞不足(有草稿)",
                                         "items": names, "draft": draft.get("_file")})
        else:
            need = max(int(pa.get("reply") or 0), 1)
            pa["reply"] = min(QUEUE_REPLY_MAX, need)
            added["reply"] = pa["reply"]
            log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "QUEUED_REPLY_FOR_EARN",
                                         "reason": "获赞不足(无草稿⇒改回帖)",
                                         "items": names})
    if added:
        pa["ts"] = time.time()
        pa["src"] = pa.get("src") or "level_route"
        save_state(st)
        log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "QUEUED",
                                     "queued": added,
                                     "items": [r.get("name") for kk in ("reply", "like", "earn")
                                               for r in (route.get(kk) or [])]})
    return added


def _forge_gate(now=None):
    """低频闸门：距上次锻造尝试 >= 3 天 + 0~4 天随机抖动 才放行。

    返回 (allow, 还需等待的天数)。锻造只读本地素材 + 本地 codex，零站点流量；
    真正发帖仍走 act_post 自己的日/周/间隔上限，这里只控制「稿子多久备一份」。
    """
    now = now or time.time()
    st = {}
    try:
        if os.path.exists(FORGE_STATE):
            with open(FORGE_STATE, encoding="utf-8") as f:
                st = json.load(f) or {}
    except Exception:
        st = {}
    last = float(st.get("last_ts") or 0)
    need = FORGE_MIN_GAP_S + random.uniform(0, FORGE_JITTER_S)
    if last and (now - last) < need:
        return False, round((need - (now - last)) / 86400.0, 2)
    return True, 0.0


def _maybe_forge_draft(out, apply_=True):
    """无草稿时在后台备一份（本地 codex 生成 + 去 AI 味闸门），不阻塞浏览循环。

    dry-run（apply_=False）绝不落盘、绝不写状态——自测/金丝雀禁写生产状态。
    """
    if not apply_:
        out["forge"] = "SKIP:DRY_RUN"
        return
    try:
        ok, wait_d = _forge_gate()
    except Exception as e:
        out["forge"] = "ERROR:%s" % repr(e)[:80]
        return
    if not ok:
        out["forge"] = "SKIP:COOLDOWN(%.2fd)" % wait_d
        return
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "linuxdo_post_forge.py")
        os.makedirs(BASE, exist_ok=True)
        logf = open(os.path.join(BASE, "forge.log"), "a")
        proc = subprocess.Popen([sys.executable, script, "forge", "--notify"],
                                stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
        logf.close()
        tmp = FORGE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:      # 先落 last_ts 再回到守护循环
            json.dump({"last_ts": time.time(), "pid": proc.pid,
                       "at": now_str()}, f)
        os.replace(tmp, FORGE_STATE)
        out["forge"] = "SPAWNED:%d" % proc.pid
        log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "FORGE_SPAWNED",
                                     "pid": proc.pid})
    except Exception as e:                              # 锻造异常绝不死守护
        out["forge"] = "ERROR:%s" % repr(e)[:80]
        log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "FORGE_ERROR",
                                     "note": repr(e)[:200]})


def maybe_run_pending_acts(st, session, apply_=True):
    """按 acts 内核的随机节奏执行排队中的回帖/点赞/发帖（每轮每类最多 1 条）。

    返回 dict（有多少条执行成功/被跳过）。失败一律留痕，不改 pending，也不抛。
    发帖（post）仅由「获赞不足 + 存在草稿」路由而来，其日/周/间隔上限由 acts 的
    act_post 自行裁决（日≤1、间隔≥24h、周≤3），这里不做二次判断。
    """
    pa = st.get("pending_acts") or {}
    if not (int(pa.get("reply") or 0) or int(pa.get("like") or 0)
            or int(pa.get("post") or 0)):
        return {}
    last = float(pa.get("last_run_ts") or 0)
    gap = random.uniform(*ACTS_ROUND_GAP)
    if last and (time.time() - last) < gap:
        return {}
    out = {}
    tok = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import linuxdo_acts as A           # 延迟 import（避免循环 import）
        tok = A.acquire_lock("daemon-acts")   # 与提示词触发的 acts 互斥：同一 linux.do 标签
        if not tok:
            log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "LOCK_BUSY"})
            return {"locked": True}
        ast_ = A.load_state()              # acts 自己的 state（独立 cap/日账）
        for kind, fn in (("reply", A.act_reply), ("like", A.act_like)):
            n_pending = int(pa.get(kind) or 0)
            if n_pending <= 0:
                continue
            try:
                res = fn(ast_, session, max_n=1, apply_=apply_,
                         log=lambda *a, **k: None)
            except Exception as e:                     # 执行器异常绝不死守护
                log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "ERROR",
                                             "kind": kind, "note": repr(e)[:200]})
                out[kind] = "ERROR"
                continue
            if not res or res.get("skip"):
                out[kind] = "SKIP:%s" % (res or {}).get("why")
                continue
            items = res.get("items") or []
            ok = [x for x in items if x.get("status") in ("REPLIED", "LIKED")]
            if ok:
                pa[kind] = max(0, n_pending - len(ok))
                out[kind] = "DONE:%d" % len(ok)
            else:
                out[kind] = "NOOP"
        if int(pa.get("post") or 0) > 0:           # 获赞不足路由来的发帖（须有草稿）
            draft = _load_post_draft()
            if not draft:
                pa["post"] = 0
                out["post"] = "SKIP:NO_DRAFT"
                _maybe_forge_draft(out, apply_)     # 稿子空了 ⇒ 低频后台补一份
            else:
                try:
                    res_p = A.act_post(ast_, session, draft.get("title"), draft.get("raw"),
                                       int(draft.get("category") or 4),
                                       str(draft.get("tags") or ""),
                                       apply_=apply_, log=lambda *a, **k: None)
                except Exception as e:
                    log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "ERROR",
                                                 "kind": "post", "note": repr(e)[:200]})
                    res_p = None
                if not res_p or res_p.get("skip"):
                    out["post"] = "SKIP:%s" % (res_p or {}).get("why")
                elif (res_p.get("items") or [{}])[0].get("status") in ("POSTED",):
                    pa["post"] = 0
                    pa.pop("post_src", None)
                    out["post"] = "DONE:1"
                    try:                            # 草稿用后归档，避免重复发
                        src = os.path.join(BASE, "linuxdo_post_drafts", draft["_file"])
                        dst = os.path.join(BASE, "linuxdo_post_drafts", "used_" + draft["_file"])
                        os.rename(src, dst)
                    except Exception:
                        pass
                else:
                    out["post"] = "NOOP"
        st["pending_acts"] = pa
        pa["last_run_ts"] = time.time()
        if not (int(pa.get("reply") or 0) or int(pa.get("like") or 0)
                or int(pa.get("post") or 0)):
            pa["done_ts"] = time.time()
        save_state(st)
        log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "RUN", "result": out,
                                     "left": {k: int(pa.get(k) or 0) for k in ("reply", "like", "post")}})
    except Exception as e:
        log_jsonl("acts_log.jsonl", {"ts": now_str(), "st": "IMPORT_ERROR",
                                     "note": repr(e)[:200]})
        return {"error": repr(e)[:200]}
    finally:
        if tok:
            try:
                A.release_lock(tok)        # 只删自己的锁；异常绝不死守护
            except Exception:
                pass
    return out


def maybe_notify_manual(st, route):
    """须人工项：每天最多提醒一次（幂等，避免刷屏）。"""
    if not (route.get("manual") or route.get("unknown")):
        return False
    if st.get("route_notified") == today():
        return False
    lines = ["【L站 TL3 前置检查】以下未达标项需人工处理（脚本不代操作）："]
    for it in route.get("manual") or []:
        lines.append("· %s %s/%s" % (it.get("name"), it.get("cur"), it.get("req")))
    for it in route.get("unknown") or []:
        lines.append("· [未能识别指标名] %s" % (it.get("name"),))
    if route.get("auto"):
        lines.append("（浏览类缺项已由后台随机节奏自动补足：%s）" % "、".join(
            str(it.get("name")) for it in route["auto"]))
    for key, label in (("reply", "回复"), ("like", "点赞")):
        if route.get(key):
            lines.append("（%s类缺项已排队，由独立随机节奏自动补：%s）" % (
                label, "、".join(str(it.get("name")) for it in route[key])))
    lines.append("提示：获赞/访问天数只能靠本人持续活跃；登录/举报/封禁类无需处理。")
    notify("\n".join(lines))
    st["route_notified"] = today()
    save_state(st)
    return True


def daily_count(st):
    return int(st.get("daily", {}).get(today(), 0))


# ---------------------------------------------------------------- 段级审计视图
# R546 遗留建议 #5（2026-10-06）：把「今天按段走了没有」变成一条只读命令能看见的事。
# 动机：段级节律是整套防检测的核心（段内刷帖 + 段间长休 ≥25min），但此前它**不可观测**——
# 10-05 全天 102 帖 / 21.05h 几乎无间歇（段计时因重启被反复重置），却只能事后翻 browse_log
# 时间戳反推才发现。本视图只做「派生 + 展示」：不写盘、不改节奏、不触站点。
REST_GAP_MIN_S = 900            # 视图中「休息」的最小间隔（15min）；≥SESSION_REST[0] 另标 long


def _hhmm(ts):
    """epoch → HH:MM（仅供展示口径）。"""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return None


def _parse_log_ts(s):
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _day_done_ts(day, log_name="browse_log.jsonl"):
    """当日 st=DONE 事件的 epoch 列表（只读；文件缺失/坏行一律跳过，绝不抛）。"""
    out = []
    p = os.path.join(BASE, log_name)
    try:
        if not os.path.exists(p):
            return out
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("st") != "DONE":
                    continue
                t = _parse_log_ts(rec.get("ts"))
                if t is None:
                    continue
                if time.strftime("%Y-%m-%d", time.localtime(t)) != day:
                    continue
                out.append(t)
    except Exception:
        return out
    out.sort()
    return out


def segment_audit(day=None, now=None, log_name="browse_log.jsonl"):
    """段级审计视图（R546 #5，只读派生）。

    返回字段：
      theoretical      今天理论几段（内核 day_segments 派生，同日稳定）
      current_index    现在处于第几段（1-based；段间空档为 null）
      current_window   [本段起, 本段止, 段长分钟]
      next_segment     段间空档时下一段起点 HH:MM
      browsed_in_segment 本段内已浏览帖数（browse_log 的 DONE 归段）
      segments_seen/segments_seen_idx  今天实际进过的段数/段号（与 theoretical 对照即知节律是否真按段走）
      rests_n/rests_long_n/rests  相邻 DONE 间隔 ≥15min 的休息（long=≥SESSION_REST[0]，即真长休）
    内核缺席或无日志时字段置 null 并带 note（不伪造）。
    """
    day = day or today()
    nowt = time.time() if now is None else float(now)
    out = {"day": day, "theoretical": None, "current_index": None, "current_window": None,
           "next_segment": None, "browsed_in_segment": None, "segments_seen": None,
           "segments_seen_idx": [], "rests_n": 0, "rests_long_n": 0, "rests": [], "note": None}
    segs = _day_segments_cached(day)
    if not segs:
        out["note"] = "kernel_absent_or_day_segments_failed"
        return out
    out["theoretical"] = len(segs)
    evs = _day_done_ts(day, log_name)
    seen = set()
    for t in evs:
        for i, s in enumerate(segs):
            if s["start"] <= t < s["end"]:
                seen.add(i)
                break
    out["segments_seen"] = len(seen)
    out["segments_seen_idx"] = [i + 1 for i in sorted(seen)]
    ci = None
    for i, s in enumerate(segs):
        if s["start"] <= nowt < s["end"]:
            ci = i
            break
    if ci is not None:
        s = segs[ci]
        out["current_index"] = ci + 1
        out["current_window"] = [_hhmm(s["start"]), _hhmm(s["end"]), s["minutes"]]
        out["browsed_in_segment"] = len([t for t in evs if s["start"] <= t < s["end"]])
    elif _ad is not None:
        try:
            nxt = _ad.next_seg_start(nowt, segs)
        except Exception:
            nxt = None
        out["next_segment"] = _hhmm(nxt) if nxt else None
    for a, b in zip(evs, evs[1:]):
        if b - a >= REST_GAP_MIN_S:
            out["rests"].append({"from": _hhmm(a), "to": _hhmm(b),
                                 "minutes": round((b - a) / 60.0, 1),
                                 "long": (b - a) >= SESSION_REST[0]})
    out["rests_n"] = len(out["rests"])
    out["rests_long_n"] = len([r for r in out["rests"] if r["long"]])
    if out["rests_n"] > 6:
        out["rests"] = out["rests"][-6:]
    return out


def daily_cap_today(st, base):
    """当日 cap：base±JITTER 内每日随机定一次（先存内存，判级落盘后跨重启稳定）。"""
    caps = st.setdefault("cap", {})
    v = caps.get(today())
    if not v:
        v = max(1, int(base) + random.randint(-DAILY_CAP_JITTER, DAILY_CAP_JITTER))
        caps[today()] = v
    return int(v)


def morning_jitter_sec(st):
    """今日晨间抖动秒：静默窗结束后再随机等这么久才开工（每日定一次，仅内存）。"""
    jt = st.setdefault("jitter", {})
    v = jt.get(today())
    if v is None:
        v = int(random.uniform(*MORNING_JITTER))
        jt[today()] = v
    return int(v)


# ---------------------------------------------------------------- TL3 判停（按页面标记，禁数值比较）
# 首页等级卡两种实测形态：A=mark 独立行(SOP 09-25 记录) / B=连写「✓ 名称 81/50」
TL_RING_RX_A = re.compile(r"([✓○])\s*\n([^\n✓○]+?)\s*\n(\d+)\s*\n\s*/\s*\n(\d+)\s*\n")
TL_RING_RX_B = re.compile(r"([✓○])\s*([^\n✓○/]+?)\s*(\d+)\s*/\s*(\d+)")


def parse_home_card(text):
    """linux.do 首页等级卡 ✓/○ 四元组 → {pending, met, items, head_pending}"""
    text = text or ""
    raw = TL_RING_RX_A.findall(text) or TL_RING_RX_B.findall(text)
    items = [{"mark": m, "name": n.strip(), "cur": int(c), "req": int(r)}
             for m, n, c, r in raw]
    if not items:
        return None
    head = None
    m = re.search(r"(\d+)\s*待完成", text)
    if m:
        head = int(m.group(1))
    pending = sum(1 for it in items if it["mark"] == "○")
    return {"items": items, "met": len(items) - pending, "pending": pending,
            "head_pending": head, "source": "home_card"}


def parse_connect_dom(res):
    """connect 页 DOM 解析（class token 判定，unmet 优先，剔进度条）。res=run_js 结果"""
    if not isinstance(res, dict) or res.get("ok") is False:
        return None
    met = res.get("met")
    unmet = res.get("unmet")
    if unmet is None or met is None:
        return None
    return {"met": int(met), "unmet": int(unmet), "pending": int(unmet),
            "items": res.get("items") or [], "source": "connect"}


CONNECT_JS = r"""
(async () => {
  const title = document.title || '';
  const txt = document.body ? document.body.innerText : '';
  if (/请稍候/.test(title) || (txt || '').replace(/\s/g, '').length < 200) {
    return JSON.stringify({bad: true, title: title, len: (txt||'').length});
  }
  if (!/信任级别|Trust Level/i.test(txt)) return JSON.stringify({bad: true, title, len: (txt||'').length});
  const els = document.querySelectorAll('.met, .unmet');
  const items = [];
  let met = 0, unmet = 0;
  els.forEach(el => {
    const cls = el.classList;
    if (cls.contains('tl3-bar-fill')) return;          // 进度条填充，非独立指标
    const isMet = cls.contains('met') && !cls.contains('unmet');
    const name = (el.textContent || '').trim().slice(0, 40);
    if (isMet) met++; else unmet++;
    items.push({met: isMet, name});
  });
  return JSON.stringify({met, unmet, items});
})()
"""


def check_level(session, timeout=40):
    """读一次等级。返回 (level|None, note)。先 connect，失败回落首页卡。"""
    # 1) connect 顶层导航（分区 cookie 使其可能 CF 挑战——SOP 处置顺序）
    # ★(2026-10-06) 还原主站改由 finally 兜底：原实现只在「读到等级」那条路径还原，
    #   一旦 CONNECT_JS/解析/进程中断就把唯一的 linux.do 标签永久留在 connect 页
    #   （当次事故：标签停在 connect ⇒ 浏览停摆 64min）。任何退出路径都必须还原。
    _nav_connect = False
    try:
        run_js(session["id"], "location.href='https://connect.linux.do/'", timeout=20)
        _nav_connect = True
        time.sleep(8)
        res = run_js(session["id"], CONNECT_JS, timeout=timeout)
        lvl = parse_connect_dom(res)
        if lvl:
            return lvl, "connect"
    except Exception as e:
        print("[warn] check_level connect 阶段异常: %s" % str(e)[:120], flush=True)
    finally:
        # ★(2026-10-05) 读完必须还原主站：否则守护把唯一的 linux.do 标签页变成 connect 页,
        #   下一轮 pick_session 再也找不到可浏览的标签 ⇒ 自毁供应、永久 batch done=0
        if _nav_connect:
            try:
                run_js(session["id"], "location.href='https://linux.do/'", timeout=20)
                time.sleep(6)
            except Exception as e:
                print("[warn] check_level 还原主站失败: %s" % str(e)[:120], flush=True)
    # 2) 回落首页等级卡
    run_js(session["id"], "location.href='https://linux.do/'", timeout=20)
    time.sleep(9)
    res = run_js(session["id"],
                 "JSON.stringify({t: document.body ? document.body.innerText.slice(0,3000) : ''})",
                 timeout=25)
    txt = res.get("t", "") if isinstance(res, dict) else ""
    lvl = parse_home_card(txt)
    if lvl is None:
        k = risk_kind_from_text(txt)
        return None, ("connect 与首页卡都判不了（CF 双卡或登录失效）" + (" [risk=%s]" % k if k else ""))
    # 头部对账：head_pending 与解析 pending 若同时存在且矛盾 → 以解析为准但记 note
    note = "home_card"
    if lvl["head_pending"] is not None and lvl["head_pending"] != lvl["pending"]:
        note += "(head=%d,parse=%d 口径以页面标记为准)" % (lvl["head_pending"], lvl["pending"])
    return lvl, note


# ---------------------------------------------------------------- 候选帖
LATEST_JS = r"""
(async (page) => {
  try {
    const r = await fetch('/latest.json?no_definitions=true&page=' + page + '&order=created',
                          {credentials: 'include', cache: 'no-store'});
    if (!r.ok) { let _b=''; try { _b=(await r.text()).slice(0,200); } catch(e){}
                 return JSON.stringify({ok: false, status: r.status, body: _b}); }
    const j = await r.json();
    const topics = (j && j.topic_list && j.topic_list.topics) || [];
    return JSON.stringify({ok: true, topics: topics.map(t => ({
      id: t.id, title: t.title, cat: t.category_id, posts: t.posts_count}))});
  } catch (e) { return JSON.stringify({ok: false, err: String(e).slice(0, 120)}); }
})(__PAGE__)
"""


LAST_FETCH_DIAG = {}
LAST_FETCH_STATS = {"pages": 0, "ok": 0}   # R563：本批候选抓取的页数/成功页数（供 fetch_verdict 判定）


def fetch_candidates(session, page):
    code = LATEST_JS.replace("__PAGE__", str(int(page)))
    res = run_js(session["id"], code, timeout=30)
    LAST_FETCH_STATS["pages"] = int(LAST_FETCH_STATS.get("pages", 0)) + 1
    if isinstance(res, dict) and res.get("ok"):
        LAST_FETCH_STATS["ok"] = int(LAST_FETCH_STATS.get("ok", 0)) + 1
        return res.get("topics") or []
    # 失败：留下诊断（供风控保险丝判 HTTP 429/403 等）
    # R563：非 2xx 时连响应正文前 200 字一起留（区分 CF 挑战 / 封禁文案 / 限流），
    #       旧实现只有 status 码 ⇒ 「是站点抖动还是账号级拦截」不可分。
    if isinstance(res, dict):
        _body = str(res.get("body") or "")[:200]
        LAST_FETCH_DIAG.update({"ts": now_str(), "page": page, "status": res.get("status"),
                                "body": _body,
                                "detail": "page=%s status=%s body=%s err=%s" % (
                                    page, res.get("status"), _body.replace("\n", " "),
                                    str(res.get("err"))[:120]),
                                "diag": "page=%s status=%s err=%s" % (page, res.get("status"), str(res.get("err"))[:120])})
    else:
        LAST_FETCH_DIAG.update({"ts": now_str(), "page": page, "status": None,
                                "body": "",
                                "detail": "page=%s raw=%s" % (page, str(res)[:120]),
                                "diag": "page=%s raw=%s" % (page, str(res)[:120])})
    return []


def fetch_verdict(pages_total, pages_ok, diag):
    """批内候选抓取后的风控判定（纯函数，便于离线自测；R563）。

    返回 (action, kind)：action ∈ {"ABORT", "NOTE", None}
      ABORT：**所有**采样页都失败 ∧ 诊断为强风控信号（403/429/401/封禁文案）
             ⇒ 本批立即熔断（零浏览），交由主循环长暂停
      NOTE ：部分页失败但仍有候选 ⇒ 只留痕（不打断本批：单页抖动不该整批空转）
      None ：无风控信号（含「全成功」与「非风控码失败」）
    """
    d = diag or {}
    rk = risk_kind_from_http(d.get("status")) or risk_kind_from_text(d.get("body") or d.get("detail") or "")
    if rk in RISK_STRONG and pages_ok == 0:
        return ("ABORT", rk)
    if rk:
        return ("NOTE", rk)
    return (None, None)


# ---------------------------------------------------------------- 浏览动作
ROUTE_JS = r"""
(async () => {
  const tid='__TID__'; let how=null;
  // ── 记账解锁三件套（2026-10-05 R558 实锤：后台 tab IntersectionObserver/rAF 停→
  //    screen-track 零 timings POST→服务端 last_read 不动→浏览帖子计数不涨）。
  //    修法=①按 DOM 真实楼层喂 _onscreen ②dispatch scroll 解锁 tick 的 180s 无滚动早退闸
  //    ③1.5s interval 驱动 tick（后台节流 ≈1Hz 仍有效，实测已验证）。
  //    幂等：interval 挂 window 跨帖存活；整页导航兜底后重新注入。
  try {
    const c = window.Discourse && window.Discourse.__container__;
    const t = c && c.lookup && c.lookup('service:screen-track');
    if (t && !window.__tmUnlockOn) {
      window.__tmUnlockOn = true;
      window.__tmUnlock = () => {
        try {
          if (!t._topicId) return;
          const nums = [];
          document.querySelectorAll('.topic-post[data-post-number]').forEach(e => {
            const n = parseInt(e.getAttribute('data-post-number'));
            if (n > 0) nums.push(n);
          });
          if (nums.length) {
            t.set('_onscreen', [...new Set(nums)]);
            window.dispatchEvent(new Event('scroll'));
          }
        } catch(e){}
      };
      window.__tmInterval = setInterval(() => {
        try { window.__tmUnlock(); if (t.tick) t.tick(); } catch(e){}
      }, 1500);
    }
  } catch(e){}
  try { const m=window.require('discourse/lib/url');
    if (m && m.default && m.default.routeTo){ m.default.routeTo('/t/topic/'+tid); how='REQ'; } } catch(e){}
  if (!how) return JSON.stringify({need_loc:true});
  for (let i=0;i<25;i++){ await new Promise(r=>setTimeout(r,500));
    const h1=document.querySelector('.fancy-title');
    if (h1){
      // 分屏滚动计划（humanize.scroll_plan 生成）：[[dy,pause_s],...]，
      // 步长/停顿逐步不同、含回滚项⇒不再匀速等高。
      const plan = __PLAN__;
      for (let k=0;k<plan.length;k++){
        await new Promise(r=>setTimeout(r, plan[k][1]*1000 + Math.random()*400));
        window.scrollBy(0, plan[k][0] + Math.random()*120);
      }
      await new Promise(r=>setTimeout(r, __STAYMS__ + Math.random()*2500));
      for(let s=0;s<__RB__;s++){ await new Promise(r=>setTimeout(r, 900 + Math.random()*500));
        window.scrollBy(0, -380); }
      return JSON.stringify({how,title:h1.innerText.trim().slice(0,60),posts:document.querySelectorAll('.topic-post').length}); } }
  return JSON.stringify({fail:true,now:location.pathname});
})()
"""

LOITER_JS = r"""
(async () => {
  try { const m=window.require('discourse/lib/url');
    if (m && m.default && m.default.routeTo){ m.default.routeTo('/latest'); }
    else { location.href='/latest'; }
  } catch(e) { location.href='/latest'; }
  await new Promise(r=>setTimeout(r,1800));
  const steps = __STEPS__;
  for (let s=0;s<steps;s++){ await new Promise(r=>setTimeout(r, 900 + Math.random()*600));
    window.scrollBy(0, 500 + Math.random()*400); }
  return JSON.stringify({loiter:true, title:(document.title||'').slice(0,40)});
})()
"""


# 帖文本量先验：候选只给 id/title/cat，无法预探正文⇒按楼层数×每楼约 420 字估算。
POST_CHARS_PER_FLOOR = 420
POST_CHARS_FALLBACK = (2200, 9000)
# 单帖时长的上界（沿用接线前的预算，避免接上拟人层后慢到日 cap 跑不完）。
DEEP_DWELL_CAP_S = 150.0
LIGHT_DWELL_CAP_S = 25.0


def _estimate_chars(c):
    try:
        posts = int((c or {}).get("posts") or 0)
    except Exception:
        posts = 0
    if posts >= 2:
        return max(1500, min(posts * POST_CHARS_PER_FLOOR, 60000))
    return random.randint(*POST_CHARS_FALLBACK)


def _browse_plan(n_chars, light=False):
    """一次浏览的姿势（humanize 单一源）：分屏滚动计划 + 底部停留 + 回滚。

    light=浅读：约三成帖子是「刷到划走」——等效文本量砍到 35%、屏内停顿减半、不回滚。
    humanize 缺席时回退常量均匀计划（保证守护仍能跑）。返回 (plan, stay_ms, rb, pose)。
    """
    if hu is None:
        steps = random.randint(*(LIGHT_STEPS if light else SCROLL_STEPS))
        dt = random.uniform(0.25, 0.5) if light else random.uniform(*SCROLL_DT)
        amp = random.randint(520, 760) if light else random.randint(*SCROLL_AMP)
        plan = [{"dy": amp, "pause": dt} for _ in range(steps)]
        stay_ms = int(random.uniform(*(LIGHT_DWELL if light else BOTTOM_STAY)) * 1000)
        return plan, stay_ms, (0 if light else ROLLBACK_STEPS), None
    n_eff = max(600, int(n_chars * (0.35 if light else 1.0)))
    dwell = hu.read_dwell_seconds(n_eff, kind="browse")
    dwell = min(dwell, LIGHT_DWELL_CAP_S if light else DEEP_DWELL_CAP_S)
    plan = hu.scroll_plan(n_eff, dwell, kind="browse")
    if light:                                     # 浅读：屏幅更大、停顿更短
        for p in plan:
            p["dy"] = round(p["dy"] * random.uniform(1.3, 1.8), 1)
            p["pause"] = round(max(0.25, p["pause"] * 0.5), 2)
    stay_ms = int(hu.wrap_up_seconds("browse") * 1000)
    pose = hu.pose("browse", n_eff)
    pose.update({"light": light, "n_chars": n_eff})
    return plan, stay_ms, (0 if light else ROLLBACK_STEPS), pose


def browse_one(session, tid, state, cand=None):
    # 深浅分层：真人不是每帖都读到底——约三成是「刷到划走」型浅读（快滚大屏幅、不回滚、短驻留）
    light = random.random() < LIGHT_P
    plan, stay, rb, pose = _browse_plan(_estimate_chars(cand), light)
    plan_js = "[" + ",".join("[%s,%s]" % (p["dy"], p["pause"]) for p in plan) + "]"
    code = ROUTE_JS.replace("__TID__", str(tid)).replace("__PLAN__", plan_js).replace("__STAYMS__", str(stay)).replace("__RB__", str(rb))
    js_budget = int(sum(float(p["pause"]) for p in plan) + stay / 1000.0 + rb * 2)
    res = run_js(session["id"], code, timeout=max(150, js_budget + 45))
    ok = isinstance(res, dict) and not res.get("need_loc") and not res.get("fail")
    if isinstance(res, dict) and res.get("need_loc"):
        # 兜底整页导航仅允许在主站 tab 上做：connect.linux.do 等子域无 Discourse，
        # 导航只会造出 Error 页并顶掉等级检查 tab（2026-10-05 实测踩坑）。
        h = run_js(session["id"], "(()=>location.host)()", timeout=15)
        host = h.get("raw") if isinstance(h, dict) else h
        if not (isinstance(host, str) and host.strip().lower() in ("linux.do", "www.linux.do")):
            return False, {"fail": "non-linuxdo-tab:%s" % host}
        run_js(session["id"], "location.href='/t/topic/%s'" % tid, timeout=20)  # 整页直达兜底
        time.sleep(6)
        res2 = run_js(session["id"], code, timeout=max(150, js_budget + 45))
        ok = isinstance(res2, dict) and not res2.get("fail")
        res = res2
    if isinstance(res, dict):
        res["mode"] = "LIGHT" if light else "DEEP"
        res["pose"] = pose
    return ok, res


def try_ensure_tab(now=None, max_per_day=6, min_gap_s=600):
    """无 linux.do 标签时补一个**后台**标签（有界自愈；2026-10-06 放宽至 6 次/日、间隔 10min）。"""
    """无 linux.do 标签时补一个**后台**标签（有界自愈）。

    2026-10-06 实测事故：浏览器里 linux.do 标签消失后，守护 pick_session 恒失败
    ⇒ 日志只刷 RC_NO_SESSION_RETRY，10.5h 零产出却「看着健康」。这里做有界自愈：
    仅当*完全没有* linux.do 标签时尝试、每天最多 max_per_day 次、两次间隔 ≥ min_gap_s。
    返回 (ok, note)。全程只读+建标签，不导航既有标签（避免顶掉等级检查页）。
    """
    led = os.path.join(BASE, "ensure_tab.jsonl")
    now_ts = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now_ts))
    tries = []
    if os.path.exists(led):
        try:
            for ln in io.open(led, encoding="utf-8"):
                ln = ln.strip()
                if ln:
                    tries.append(json.loads(ln))
        except Exception:
            tries = []
    todays = [t for t in tries if str(t.get("day")) == day]
    if len(todays) >= max_per_day:
        return False, "已达本日自愈上限(%d)" % max_per_day
    if todays and now_ts - float(todays[-1].get("ts") or 0) < min_gap_s:
        return False, "距上次自愈不足 %dmin" % (min_gap_s // 60)
    # 2026-10-06 二次实测：新建标签会**顶到前台**（Chrome 新建即激活）⇒ linux.do 标签
    # 的 document.visibilityState 变 visible，用户前台保护随即每 5min 判「用户在看」而永远跳过。
    # 故建完立刻把原先的活动标签换回来（新标签留在后台），既补上供给也不打扰用户。
    script = ("tell application \"Google Chrome\"\n"
              "  set _u to \"\"\n"
              "  repeat with w in windows\n"
              "    repeat with tb in tabs of w\n"
              "      if (URL of tb) contains \"linux.do\" then set _u to _u & (URL of tb) & linefeed\n"
              "    end repeat\n"
              "  end repeat\n"
              "  if _u is not \"\" then return \"URLS:\" & _u\n"
              "  if (count of windows) = 0 then make new window\n"
              "  set _prev to (active tab index of window 1)\n"
              "  tell window 1 to make new tab with properties {URL:\"https://linux.do/\"}\n"
              "  try\n"
              "    set active tab index of window 1 to _prev\n"
              "  end try\n"
              "  return (_prev as text)\n"
              "end tell")
    prev_idx = None
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=25)
        ok = (r.returncode == 0)
        note = (r.stdout or r.stderr or "").strip()
        if ok and note.upper().startswith("URLS:"):
            # ★精确 host 判据（2026-10-06）：只认主站；connect.linux.do 不算「已有」；
            #   否则标签被导航走时自愈恒不触发（当次空转 64min、零产出）。
            _urls = [u.strip() for u in note[5:].splitlines() if u.strip()]
            if any(_is_main_host(_url_host(u)) for u in _urls):
                return False, "已有 linux.do 主站标签(仅等驱动重连)"
        if ok:
            try:
                prev_idx = int(str(note).strip() or 0) or None
            except Exception:
                prev_idx = None
    except Exception as e:
        ok, note = False, repr(e)[:120]
    try:
        with io.open(led, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_ts, "day": day, "ok": ok, "note": note,
                                "restored_active": prev_idx}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return ok, note


def browse_batch(session, st, batch=25):
    """浏览一批。返回 (done, skipped)"""
    LAST_FETCH_DIAG.clear()
    LAST_FETCH_STATS.update({"pages": 0, "ok": 0})
    done = skipped = 0
    loiter_n = random.randint(*LOITER_P_LIST)
    pages = random.sample(range(0, LATEST_PAGE_MAX + 1), min(LATEST_PAGE_MAX + 1, 4))
    cand = []
    for p in pages:
        cand += fetch_candidates(session, p)
        time.sleep(random.uniform(1.5, 4.0))
    # ★R563 批内风控熔断：抓取阶段已确认强风控（403/429/401/封禁文案）⇒ 零帖中止。
    #   旧行为把风控判定放在本函数返回之后，403 时仍照跑整批（2026-10-08 实测多浏览 20 帖）。
    _act, _rk = fetch_verdict(LAST_FETCH_STATS["pages"], LAST_FETCH_STATS["ok"], LAST_FETCH_DIAG)
    if _act == "ABORT":
        log_jsonl("browse_log.jsonl", {"ts": now_str(), "st": "ABORT",
                                       "why": "risk_abort:" + str(_rk),
                                       "detail": str(LAST_FETCH_DIAG.get("detail"))[:200],
                                       "pages": LAST_FETCH_STATS["pages"],
                                       "pages_ok": LAST_FETCH_STATS["ok"]})
        raise RiskAbort(_rk, LAST_FETCH_DIAG.get("detail") or "")
    if _act == "NOTE":
        print("[%s] 部分页风控信号 %s（仍有候选）⇒ 本批继续，仅留痕" % (now_str(), _rk), flush=True)
    seen = set(st.get("seen_ids", []))
    cand = [c for c in cand if c.get("id") and int(c["id"]) not in seen]
    random.shuffle(cand)
    # 分类偏好加权：真人会「进一个板块连看」，均匀跳遍全站分类=机器味
    by_cat = {}
    for c in cand:
        by_cat.setdefault(c.get("cat"), []).append(c)
    if len(by_cat) >= 3 and batch >= 8:
        pref_cats = set(random.sample(sorted(by_cat, key=str), min(3, len(by_cat) - 1)))
        pref_pool = [c for c in cand if c.get("cat") in pref_cats]
        other_pool = [c for c in cand if c.get("cat") not in pref_cats]
        n_pref = min(len(pref_pool), max(2, int(batch * 0.6)))
        cand = pref_pool[:n_pref] + other_pool
    for i, c in enumerate(cand):
        if i >= batch:
            break
        tid = str(c["id"])
        if i == loiter_n:
            # 拟人：逛一次列表页不进帖
            run_js(session["id"], LOITER_JS.replace("__STEPS__", str(random.randint(4, 8))), timeout=45)
            log_jsonl("browse_log.jsonl", {"ts": now_str(), "st": "LOITER", "note": "latest"})
        ok, res = browse_one(session, tid, st)
        title = (res.get("title", "") if isinstance(res, dict) else "")[:36]
        _why = ""
        if not ok and isinstance(res, dict):
            _why = str(res.get("fail") or res.get("err") or res.get("need_loc") or "unknown")[:100]
        elif not ok:
            _why = "no-result"
        log_jsonl("browse_log.jsonl", {"ts": now_str(), "st": "DONE" if ok else "FAIL",
                                       "why": _why,
                                       "id": tid, "cat": c.get("cat"), "title": title,
                                       "posts": res.get("posts") if isinstance(res, dict) else None,
                                       "mode": res.get("mode") if isinstance(res, dict) else None,
                                       "pose": res.get("pose") if isinstance(res, dict) else None})
        # ★会话级故障即中止本批（2026-10-06）：标签被导航走/驱动回落错标签时，逐帖重试
        #   会把本批 19 次机会全烧在同一个故障上（当次 21:52–22:56 零产出）。交回主循环自愈。
        if (not ok) and str(_why).startswith("non-linuxdo-tab"):
            log_jsonl("browse_log.jsonl", {"ts": now_str(), "st": "ABORT",
                                           "why": "session_drift:" + str(_why)[:80],
                                           "n_left": len(cand) - i - 1})
            break
        if ok:
            done += 1
            seen.add(int(tid))
            st["seen_ids"] = (st.get("seen_ids", []) + [int(tid)])[-4000:]
            st["browsed_total"] = st.get("browsed_total", 0) + 1
            st.setdefault("daily", {})[today()] = daily_count(st) + 1
            save_state(st)
        else:
            skipped += 1
        # 帖间随机间隔（防检测核心）：统一走 humanize，含 25-35% 长休
        _base_gap = _ad.gap_seconds("browse") if _ad is not None else random.uniform(*GAP_RANGE)
        time.sleep(hu.action_gap("browse", base=_base_gap))
    return done, skipped


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- watch
def watch(args):
    st = load_state()
    write_heartbeat("start")
    start = time.time()     # 仅作 _ad 缺席时的兜底计时（有内核时按当日段表驱动，见下）
    sess_fails = 0      # pick_session 连续失败（断链重试）
    lvl_fails = 0       # check_level 连续失败
    front_none = 0      # 前台探测连续失败
    front_true_cnt = 0  # 前台 True 连续次数（探测失真自检日志用）
    def _nap(sec, phase="sleep"):
        """睡 sec 秒；**睡前**把「下次预计醒来时刻」写进心跳 ⇒ 长睡可被看门狗自证。"""
        try:
            write_heartbeat(phase, next_wake_ts=time.time() + float(sec))
        except Exception:
            pass
        time.sleep(sec)

    while True:
        write_heartbeat("loop")
        # 风控升级后当日停止（只长睡，不再发请求）
        if st.get("risk_stop_day") == today():
            print("[%s] 风控升级后当日停止浏览，睡 1h 复查" % now_str(), flush=True)
            _nap(3600)
            continue
        # 随机休息日：平均每周 1 天完全不浏览（降总量 + 拟人）
        if rest_day_today(st):
            print("[%s] 今日为随机休息日，全天不浏览" % now_str(), flush=True)
            _nap(3600)
            continue
        if quiet_window_check():
            print("[%s] 静默窗内，睡 30min" % now_str(), flush=True)
            _nap(1800)
            continue
        mj = morning_jitter_sec(st)
        if mj:
            now = dt.datetime.now()
            gate = dt.datetime(now.year, now.month, now.day, QUIET_WINDOW[2], QUIET_WINDOW[3]) + dt.timedelta(seconds=mj)
            if now < gate:
                print("[%s] 晨间抖动：静默窗后延 %dmin 再开工（先睡 %dmin）" % (now_str(), mj / 60, (gate - now).total_seconds() / 60), flush=True)
                _nap(min((gate - now).total_seconds(), 1800))
                continue
        session, note = pick_session()
        if not session:
            _ok, _note = try_ensure_tab()
            if _ok:
                print("[%s] 无 linux.do 标签 ⇒ 已自愈补一个后台标签（%s），12s 后重试" % (now_str(), _note), flush=True)
                _nap(12)
                session, note = pick_session()
            elif _note:
                print("[%s] 无 linux.do 标签，自愈跳过：%s" % (now_str(), _note), flush=True)
        if not session:
            sess_fails += 1
            if sess_fails >= SESS_RETRY_MAX:
                print("[%s] RC_NO_SESSION: %s（连续 %d 次断链，退出）" % (now_str(), note, sess_fails), flush=True)
                return RC_NO_SESSION
            if sess_fails <= SESS_RETRY_SHORT_MAX:
                wait = random.uniform(*SESS_RETRY_SHORT)
                _wu = "%.0fs" % wait
            else:
                wait = random.uniform(*SESS_RETRY_SLEEP)
                _wu = "%.0fmin" % (wait / 60)
            print("[%s] RC_NO_SESSION_RETRY: %s（%d/%d，睡 %s 后重试）" % (now_str(), note, sess_fails, SESS_RETRY_MAX, _wu), flush=True)
            _nap(wait)
            continue
        sess_fails = 0
        if level_check_due(st):
            lvl, src = check_level(session)
        else:
            _prev = st.get("level") or {}
            lvl = {"pending": int(_prev.get("pending") or 1), "met": _prev.get("met")}
            src = "throttled"
            print("[%s] 等级前置检查节流中（下次 %s），沿用上次判定 pending=%s 继续补足（本日节流 %d 次）" % (
                now_str(), _ts_str(st.get("level_next_ts")), lvl["pending"],
                int(st.get("level_skip") or 0)), flush=True)
            lvl_fails = 0
        if lvl is None:
            m = re.search(r"\[risk=([A-Z_]+)\]", src or "")
            rk = m.group(1) if m else None
            if rk in RISK_STRONG:
                handle_risk(st, rk, "check_level: %s" % src)
                continue
            if rk:
                note_risk(st, rk, "check_level: %s" % src)   # CF 挑战等：留痕+计数，不改原重试口径
            lvl_fails += 1
            if lvl_fails >= SESS_RETRY_MAX:
                print("[%s] RC_UNJUDGEABLE: %s（连续 %d 次判级失败，退出）" % (now_str(), src, lvl_fails), flush=True)
                log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "UNJUDGEABLE", "note": src})
                return RC_UNJUDGEABLE
            print("[%s] RC_UNJUDGEABLE_RETRY: %s（%d/%d，睡后重试）" % (now_str(), src, lvl_fails, SESS_RETRY_MAX), flush=True)
            _nap(random.uniform(*SESS_RETRY_SLEEP))
            continue
        lvl_fails = 0
        if src == "throttled":
            # 节流轮次只是「沿用上次判定继续补足」，**不是一次真实等级检查**：
            # 旧实现照样写 level_log / 刷 st["level"].ts / 推进 level_check_mark ⇒ 台账被每 301s 的
            # 节流行污染、检查频次不可取证（R545 缺陷④）。改为只计 level_skip。
            st["level_skip"] = int(st.get("level_skip") or 0) + 1
            save_state(st)
        else:
            log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "OK", "source": src,
                                          "met": lvl.get("met"), "pending": lvl.get("pending"), "items": lvl.get("items")})
            st["level"] = {"ts": now_str(), "met": lvl.get("met"), "pending": lvl.get("pending")}
            level_check_mark(st)
            save_state(st)
        if lvl.get("pending", 99) == 0:
            print("[%s] ✓ TL3 已满足（pending=0），守护自然退出" % now_str(), flush=True)
            return RC_OK
        # 缺项路由：浏览类走浏览 SOP 自补；回复/送赞/获赞类排队给 acts 随机节奏执行；其余只通知
        route = classify_pending(lvl)
        if (route["manual"] or route["unknown"] or route.get("reply")
                or route.get("like") or route.get("earn")):
            print("[%s] 缺项路由：须人工 %s / 未识别 %s / 自动补 %s / 排队回帖 %s / 排队点赞 %s / 排队发帖 %s" % (
                now_str(), [it.get("name") for it in route["manual"]],
                [it.get("name") for it in route["unknown"]],
                [it.get("name") for it in route["auto"]],
                [it.get("name") for it in route.get("reply") or []],
                [it.get("name") for it in route.get("like") or []],
                [it.get("name") for it in route.get("earn") or []]), flush=True)
        if route.get("reply") or route.get("like") or route.get("earn"):
            queued = queue_pending_from_route(st, route)
            print("[%s] 已排队自动补足（等 acts 随机段执行，不即刻动手）：%s" % (now_str(), queued), flush=True)
        if route["manual"] or route["unknown"]:
            if maybe_notify_manual(st, route):
                print("[%s] 已私信提醒须人工项（今日首次）" % now_str(), flush=True)
        cap_today = daily_cap_today(st, args.daily_cap)
        if daily_count(st) >= cap_today:
            print("[%s] 今日已达 daily-cap=%d（base=%d，每日随机定一次），睡到明天" % (now_str(), cap_today, args.daily_cap), flush=True)
            _r = maybe_run_pending_acts(st, session)
            if _r:
                print("[%s] 浏览已到量，仍按随机节奏补做排队动作：%s（剩余 %s）" % (
                    now_str(), _r, {k: int((st.get("pending_acts") or {}).get(k) or 0)
                                    for k in ("reply", "like", "post")}), flush=True)
            _nap(3600)
            continue
        # ---- 段级节奏（2026-10-06 R546，修 R545 缺陷①）--------------------------------
        # 当日活动段由内核按「日期种子」确定性派生（day_segments）：守护重启/被杀不重置段位，
        # 段外一律等（不再有「段满才休息」的进程内计时器——旧实现在 9h20m 内被守护重启 35 次，
        # 段满事件几乎永不发生，长休档形同虚设）。内核缺席时回落旧行为。
        _segs = _day_segments_cached()
        if _segs:
            _go, _w, _why = segment_gate(segs=_segs)
            if not _go:
                print("[%s] 段间空档（当日 %d 段，%s），等 %.0fmin 进下一段" % (
                    now_str(), len(_segs), _why, _w / 60), flush=True)
                _nap(_w)
                continue
        elif (time.time() - start) / 60 >= args.max_session_min:
            rest = random.uniform(*SESSION_REST)
            print("[%s] 单段运行满 %.0fmin，休息 %.0fmin" % (now_str(), args.max_session_min, rest / 60), flush=True)
            _nap(rest)
            start = time.time()
        # 用户前台保护：tab 可见即视为用户在看，绝不抢（宁停勿扰；fail-closed 起步）
        front = tab_is_front(session)
        skip, why = front_decision(front, front_none)
        front_none = front_none + 1 if front is None else 0
        front_true_cnt = front_true_cnt + 1 if front is True else 0
        if front_true_cnt == 12:
            print("[%s] ⚠ 前台连续 %d 次为 True——若持续请人工核查探测是否失真" % (now_str(), front_true_cnt), flush=True)
        if skip:
            print("[%s] %s，睡 5min" % (now_str(), why), flush=True)
            _nap(300)
            continue
        # 排队中的回帖/点赞：按 acts 内核的随机段与随机间隔执行（与等级检查时刻解耦）
        _ra = maybe_run_pending_acts(st, session)
        if _ra:
            print("[%s] 排队动作执行：%s（剩余 %s）" % (
                now_str(), _ra, {k: int((st.get("pending_acts") or {}).get(k) or 0)
                                 for k in ("reply", "like", "post")}), flush=True)
        # ★R563：抓取阶段命中强风控 ⇒ browse_batch 内部抛 RiskAbort（零帖熔断）。
        #   旧行为把判定放在本块之后 ⇒ 403 仍照跑整批（2026-10-08 实测多浏览 20 帖）。
        try:
            done, skipped = browse_batch(session, st, batch=args.batch)
        except RiskAbort as e:
            print("[%s] ⛔ 批内风控熔断 %s（零浏览）：%s ⇒ 转风控处置" % (
                now_str(), e.kind, str(e.detail)[:200]), flush=True)
            handle_risk(st, e.kind, "batch_abort: %s" % e.detail)
            continue
        print("[%s] batch done=%d skipped=%d total_browsed=%d" % (now_str(), done, skipped, st.get("browsed_total", 0)), flush=True)
        # 候选抓取失败时留了诊断 ⇒ 先判是不是风控（HTTP 429/403 / 封禁文案）
        diag = LAST_FETCH_DIAG.get("diag")
        if diag:
            rk = risk_kind_from_http(LAST_FETCH_DIAG.get("status")) or risk_kind_from_text(diag)
            if rk in RISK_STRONG:
                handle_risk(st, rk, diag)
                continue
            if rk:
                note_risk(st, rk, diag)
            else:
                log_jsonl("risk_log.jsonl", {"ts": now_str(), "st": "INFO", "kind": "FETCH_FAIL",
                                             "detail": str(diag)[:300]})
        if done == 0 and skipped == 0:
            print("[%s] 无可用候选（候选全重/接口异常），睡 10min 重试" % now_str(), flush=True)
            _nap(600)
        gap = _batch_gap()
        print("[%s] 批间休 %.0fs" % (now_str(), gap), flush=True)
        _nap(gap)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    argv = argv or sys.argv[1:]
    if argv and argv[0] in ("--selftest", "-t"):
        return selftest()
    ap = argparse.ArgumentParser(prog="linuxdo_browse_daemon")
    ap.add_argument("cmd", choices=["status", "check", "browse", "watch"])
    ap.add_argument("--batch", type=int, default=BATCH_DEFAULT)
    ap.add_argument("--daily-cap", type=int, default=DAILY_CAP_DEFAULT)
    ap.add_argument("--max-session-min", type=float, default=SESSION_MIN_DEFAULT)
    ap.add_argument("--tab-id", type=str, default=None, help="显式指定任务 tab id（默认自动发现唯一 linux.do tab）")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        st = load_state()
        print(json.dumps({"today": today(), "daily_browsed": daily_count(st),
                          "total": st.get("browsed_total", 0), "level": st.get("level"),
                          "quiet_now": quiet_window_check(),
                          # 只读派生（R546 #5）：段级节律是否真在走 —— 不写盘、不碰站点
                          "segments": segment_audit()}, ensure_ascii=False, indent=1))
        return RC_OK
    session, note = pick_session(prefer=args.tab_id)
    if not session and args.cmd != "watch":
        print("RC_NO_SESSION: %s" % note)
        return RC_NO_SESSION
    st = load_state()
    if args.cmd == "check":
        lvl, src = check_level(session)
        if lvl is None:
            print("RC_UNJUDGEABLE: %s" % src)
            return RC_UNJUDGEABLE
        log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "OK", "source": src, "met": lvl.get("met"), "pending": lvl.get("pending")})
        print(json.dumps({"source": src, "pending": lvl.get("pending"), "met": lvl.get("met")}, ensure_ascii=False))
        return RC_OK
    if args.cmd == "browse":
        try:
            done, skipped = browse_batch(session, st, batch=args.batch)
        except RiskAbort as e:
            print("RISK_ABORT %s: %s" % (e.kind, str(e.detail)[:200]))
            handle_risk(st, e.kind, "browse_abort: %s" % e.detail)
            return RC_GAPS
        print("browse done=%d skipped=%d" % (done, skipped))
        return RC_GAPS if done else RC_NO_SESSION
    if args.cmd == "watch":
        return watch(args)
    return RC_USAGE


# ---------------------------------------------------------------- selftest
_SANDBOX_ACTIVE = False


def _selftest_impl():
    # 2026-10-06：自测卫生 - 直调本函数（不经 selftest() 包装）会写到**生产** BASE，
    # 且 T6c6/T6c9 会在 BASE/linuxdo_post_drafts 留下 d1.json/d2.json ⇒ 下一次跑
    # T6c7「无草稿仍排 post」必假红（草稿是上次自测自己留下的）。修法=兜底沙箱，
    # 幂等可重入：已在沙箱内（selftest() 或本兜底）则直接跑本体。
    if not globals().get("_SANDBOX_ACTIVE"):
        return _run_selftest_sandboxed()
    fails = []
    cases = 0
    cases += 1
    # T1 首页卡解析：混合 ✓/○，含上限类「被举报 0/5」标 ✓ —— 不得算未达标
    sample = "✓ 访问天数 81\n/\n50\n✓ 浏览话题 582\n/\n500\n○ 已读帖子 12571\n/\n20000\n✓ 被举报帖子 0\n/\n5\n✓ 被禁言 0\n/\n0\n○ 回复 8\n/\n10\n○ 2 待完成"
    lvl = parse_home_card(sample)
    if lvl is None or lvl["pending"] != 2 or lvl["met"] != 4:
        fails.append("T1 home_card 解析错: %s" % (lvl,))
    if lvl and lvl["head_pending"] != 2:
        fails.append("T1 head_pending 错: %s" % lvl["head_pending"])
    cases += 1
    # T1n 负控：上限类(当前值 0 < 门槛 5)标 ✓ —— 数值机械比较会误报，页面标记法不误报
    if lvl and any(it["name"].find("被举报") >= 0 and it["mark"] == "○" for it in lvl["items"]):
        fails.append("T1n 上限类被误判未达标(子串陷阱): %s" % [it for it in lvl["items"] if "被举报" in it["name"]])
    cases += 1
    # T2 connect DOM：class token 判定 + tl3-bar-fill 剔除 + 'unmet' 子串陷阱
    fake = {"met": 10, "unmet": 2, "items": []}
    lvl2 = parse_connect_dom(fake)
    if lvl2 is None or lvl2["pending"] != 2:
        fails.append("T2 connect 解析错: %s" % lvl2)
    bad = {"ok": False, "error": "x"}
    if parse_connect_dom(bad) is not None:
        fails.append("T2n 坏响应未拒")
    cases += 1
    # T2n2 'unmet' 含 'met' 子串 —— 判据必须 isMet = has('met') && !has('unmet')
    cls_test = "tl3-ring-circle unmet"
    isMet = ("met" in cls_test.split()) and ("unmet" not in cls_test.split())
    if isMet:
        fails.append("T2n2 unmet 子串陷阱未挡")
    cases += 1
    # T3 静默窗
    if not quiet_window_check(dt.datetime(2026, 10, 5, 3, 0)) or quiet_window_check(dt.datetime(2026, 10, 5, 9, 0)):
        fails.append("T3 静默窗判断错")
    cases += 1
    # T4 daily cap / 间隔参数边界
    st = {"daily": {today(): DAILY_CAP_DEFAULT}}
    if daily_count(st) < DAILY_CAP_DEFAULT:
        fails.append("T4 daily cap 错")
    if not (0 < GAP_RANGE[0] < GAP_RANGE[1] <= GAP_LONG_RANGE[0] < GAP_LONG_RANGE[1]):
        fails.append("T4 间隔区间边界错")
    cases += 1
    # T5 桥缺失 fail-closed：指向不可达端口
    global BRIDGE
    old = BRIDGE
    BRIDGE = "http://127.0.0.1:1"
    try:
        list_sessions(timeout=2)
        fails.append("T5 桥缺失未 fail-closed")
    except BridgeError:
        pass
    finally:
        BRIDGE = old
    cases += 1
    # T6c 缺项路由分类（两路径兼容）——connect 形态（met+值文本，门槛吻合⇒顺序映射生效）
    _ci = [{"met": True, "name": "81/ 50"}, {"met": True, "name": "598/ 500"},
           {"met": False, "name": "13171/ 20000"}, {"met": True, "name": "32/ 10"},
           {"met": True, "name": "75/ 30"}, {"met": True, "name": "49/ 20"},
           {"met": True, "name": "11/ 7"}, {"met": True, "name": "48/ 5"},
           {"met": True, "name": "被举报帖子\n 0 / 5"}, {"met": True, "name": "举报用户\n 0 / 5"},
           {"met": True, "name": "被禁言 0"}, {"met": True, "name": "被封禁 0"}]
    _r = classify_pending({"items": _ci})
    if [it["name"] for it in _r["auto"]] != ["浏览帖子"] or _r["manual"] or _r["unknown"]:
        fails.append("T6c connect 形态：浏览帖子应归自动补且无人工项: %s" % _r)
    cases += 1
    # T6c2 回复类未达标（idx3 门槛吻合⇒映射出「回复话题」）⇒ 归 reply 路由（排队给 acts，不须人工）
    _ci2 = [dict(x) for x in _ci]; _ci2[3] = {"met": False, "name": "8/ 10"}
    _r2 = classify_pending({"items": _ci2})
    if [it["name"] for it in _r2["reply"]] != ["回复话题"] or _r2["manual"]:
        fails.append("T6c2 回复类应归 reply 路由且不进人工: %s" % _r2)
    cases += 1
    # T6c2b 送赞类未达标（idx4=点赞门槛）⇒ 归 like 路由
    _ci2b = [dict(x) for x in _ci]; _ci2b[4] = {"met": False, "name": "22/ 30"}
    _r2b = classify_pending({"items": _ci2b})
    if [it["name"] for it in _r2b["like"]] != ["点赞"] or _r2b["manual"]:
        fails.append("T6c2b 送赞类应归 like 路由且不进人工: %s" % _r2b)
    cases += 1
    # T6c2c 排队：欠账按 cur/req 差值算，且受上限封顶（dry：不执行动作，仅排队）
    _stq = {"pending_acts": {}}
    _add = queue_pending_from_route(_stq, {"reply": [{"name": "回复话题", "cur": 8, "req": 10}],
                                           "like": [{"name": "点赞", "cur": 0, "req": 30}]})
    if _add != {"reply": 2, "like": QUEUE_LIKE_MAX} or \
       _stq["pending_acts"].get("reply") != 2 or _stq["pending_acts"].get("src") != "level_route":
        fails.append("T6c2c 排队欠账量/封顶错: %s / %s" % (_add, _stq.get("pending_acts")))
    cases += 1
    # T6c2d 负控：无 reply/like 缺项 ⇒ 不得产生任何排队（不许无中生有）
    _stq2 = {"pending_acts": {}}
    if queue_pending_from_route(_stq2, {"reply": [], "like": [], "auto": [{"name": "浏览帖子"}]}) or \
       _stq2["pending_acts"]:
        fails.append("T6c2d 无缺项却排队: %s" % _stq2["pending_acts"])
    cases += 1
    # T6c2e 执行节流：距上轮不足随机间隔 ⇒ 本轮不执行（且不触发 import 执行器）
    _stt = {"pending_acts": {"reply": 1, "like": 0, "last_run_ts": time.time()}}
    if maybe_run_pending_acts(_stt, None) != {}:
        fails.append("T6c2e 节流窗内不应执行排队动作")
    cases += 1
    # T6c3 负控：门槛值被改（idx1 req 600）⇒ 顺序不可信 ⇒ 未达标项必须落 unknown（不得误判 auto）
    _ci3 = [dict(x) for x in _ci]; _ci3[1] = {"met": False, "name": "598/ 600"}
    _r3 = classify_pending({"items": _ci3})
    if _r3["auto"] or [it["name"] for it in _r3["unknown"]] != ["598/ 600", "13171/ 20000"]:
        fails.append("T6c3 门槛失配必须全部转 unknown: %s" % _r3)
    cases += 1
    # T6c4 首页卡形态（mark/cur/req，name=指标名）
    _hc = {"items": [{"mark": "○", "name": "浏览话题", "cur": 100, "req": 500},
                     {"mark": "○", "name": "回复话题", "cur": 8, "req": 10},
                     {"mark": "✓", "name": "点赞", "cur": 30, "req": 30},
                     {"mark": "○", "name": "访问天数", "cur": 40, "req": 50}]}
    _r4 = classify_pending(_hc)
    if [it["name"] for it in _r4["auto"]] != ["浏览话题"] or \
       [it["name"] for it in _r4["reply"]] != ["回复话题"] or \
       [it["name"] for it in _r4["manual"]] != ["访问天数"]:
        fails.append("T6c4 首页卡形态分类错: %s" % _r4)
    if any(it["name"] == "点赞" for it in _r4["manual"] + _r4["auto"] + _r4["unknown"] +
           _r4["reply"] + _r4["like"]):
        fails.append("T6c4 已达标项(✓)不应进入路由")
    cases += 1
    # T6c5 负控：unknown 不得静默 —— 必须进通知面
    _ci5 = [{"met": False, "name": "13171/ 20000"}]
    _r5 = classify_pending({"items": _ci5})
    if len(_r5["unknown"]) != 1:
        fails.append("T6c5 短表(不可映射)应落 unknown: %s" % _r5)
    cases += 1
    # T6c6 获赞类缺项 + 存在草稿 ⇒ 排 post（发帖由 acts.act_post 自裁日/周上限）
    _stp = {"pending_acts": {}}
    _ddir = os.path.join(BASE, "linuxdo_post_drafts")
    os.makedirs(_ddir, exist_ok=True)
    with open(os.path.join(_ddir, "d1.json"), "w", encoding="utf-8") as fh:
        json.dump({"title": "t", "raw": "r", "category": 4, "tags": "AI"}, fh)
    _addp = queue_pending_from_route(_stp, {"earn": [{"name": "获赞"}]})
    if _addp.get("post") != 1 or _stp["pending_acts"].get("post") != 1 or \
       _stp["pending_acts"].get("post_src") != "level_route":
        fails.append("T6c6 获赞缺项+有草稿应排 post: %s / %s" % (_addp, _stp.get("pending_acts")))
    cases += 1
    # T6c7 负控：获赞缺项但**无草稿** ⇒ 绝不排 post（不许自己编内容），改走回帖并留痕
    os.remove(os.path.join(_ddir, "d1.json"))
    _stp2 = {"pending_acts": {}}
    _addp2 = queue_pending_from_route(_stp2, {"earn": [{"name": "获赞"}]})
    if _addp2.get("post") or _stp2["pending_acts"].get("post") or \
       _stp2["pending_acts"].get("reply", 0) < 1:
        fails.append("T6c7 无草稿仍排 post（会编造内容）: %s / %s" % (_addp2, _stp2.get("pending_acts")))
    cases += 1
    # T6c8 负控：无获赞缺项 ⇒ 不得排队 post（不许无中生有）
    _stp3 = {"pending_acts": {}}
    if queue_pending_from_route(_stp3, {"reply": [], "like": [], "earn": []}) or \
       _stp3["pending_acts"].get("post"):
        fails.append("T6c8 无获赞缺项却排 post: %s" % _stp3["pending_acts"])
    cases += 1
    # T6c9 多缺项并行（用户口径：一个不够走一个，多个不够走多个）
    with open(os.path.join(_ddir, "d2.json"), "w", encoding="utf-8") as fh:
        json.dump({"title": "t2", "raw": "r2"}, fh)
    _stm = {"pending_acts": {}}
    _addm = queue_pending_from_route(_stm, {"reply": [{"name": "回复话题", "cur": 8, "req": 10}],
                                            "like": [{"name": "点赞", "cur": 22, "req": 30}],
                                            "earn": [{"name": "获赞"}],
                                            "auto": [{"name": "浏览话题"}]})
    if set(["reply", "like", "post"]) - set(_addm.keys()):
        fails.append("T6c9 多缺项未并行排队: %s" % _addm)
    cases += 1
    # T6b 等级前置检查节奏（2026-10-05 用户要求：每天 1 次、错峰、不要固定时刻打站点）
    if not (0 < LEVEL_CHECK_GAP[0] < LEVEL_CHECK_GAP[1] <= 24 * 3600):
        fails.append("T6b LEVEL_CHECK_GAP 兜底区间不合理: %s" % (LEVEL_CHECK_GAP,))
    if not level_check_due({}):
        fails.append("T6b 首次（无 level 记录）应到期必查")
    # 白昼固定时刻取样（避开静默窗 01:00–07:30 造成的假红）
    _t0 = _ts_of_today(12, 0)
    _st0 = {"level": {"pending": 1}}
    if _ad is not None:                              # 委托证明：判定必须与内核逐字一致
        _kd = bool(_ad.level_check_due(_st0, _t0)[0])
        if level_check_due(_st0, _t0) != _kd:
            fails.append("T6b 等级检查未委托内核: %s vs %s" % (level_check_due(_st0, _t0), _kd))
        if not _kd and not _ad.quiet_now(dt.datetime.fromtimestamp(_t0)):
            # 未到期 ⇒ 只能是错峰时刻未到（不允许出现别的沉默理由）
            _why = _ad.level_check_due(_st0, _t0)[1]
            if not _why.startswith("before_slot_"):
                fails.append("T6b 未到期的理由不可解释: %s" % _why)
    else:
        fails.append("T6b 内核缺席（统一随机化内核必须可加载）")
    _st = {"level": {"pending": 1}}
    level_check_mark(_st, _t0)                       # 模拟今天已在 t0 查过
    if level_check_due(_st, _t0 + 60):
        fails.append("T6b 同一天内不应重复检查")
    _w, _why = level_check_wait(_st, _t0 + 60)
    if not (_w > 0 and _why.startswith("next_day")):
        fails.append("T6b 下次检查应落到次日错峰时刻: %s/%s" % (_w, _why))
    if not level_check_due(_st, _t0 + 60 + _w + 5):
        fails.append("T6b 到了次日错峰时刻应到期")
    if _ad is not None:                              # 内核在场 ⇒ 每日次数上限必须为 1
        if getattr(_ad, "LEVEL_CHECK_PER_DAY", 0) != 1:
            fails.append("T6b 内核 LEVEL_CHECK_PER_DAY 应为 1: %s" % getattr(_ad, "LEVEL_CHECK_PER_DAY", None))
        _ts1 = _ad.level_check_ts(_ad._day_of(_t0))
        if not (0 < _ad.LEVEL_MIN_GAP_S <= 24 * 3600):
            fails.append("T6b 内核最小间隔越界: %s" % _ad.LEVEL_MIN_GAP_S)
        if not (_ts1 > 0):
            fails.append("T6b 内核错峰时刻异常: %s" % _ts1)
    # 内核缺席 ⇒ 回退旧 next_ts 口径（负控：证明兜底仍可用而非静默假绿）
    _keep = _ad
    try:
        globals()["_ad"] = None
        if level_check_due({"level": {"pending": 1}, "level_next_ts": _t0 + 3600}, _t0):
            fails.append("T6b2 兜底口径：节流窗内不应检查")
        if not level_check_due({"level": {"pending": 1}, "level_next_ts": _t0 - 1}, _t0):
            fails.append("T6b2 兜底口径：已到期应再查")
    finally:
        globals()["_ad"] = _keep
    cases += 1
    # T6 防检测参数齐全
    for name in ("GAP_RANGE", "SCROLL_STEPS", "LOITER_P_LIST", "QUIET_WINDOW", "BATCH_GAP_MULT", "LIGHT_P", "LIGHT_STEPS", "LIGHT_DWELL", "SESS_RETRY_MAX", "SESS_RETRY_SLEEP", "FRONT_NONE_TOLERANCE", "FRONT_JS"):
        v = globals().get(name)
        if not v:
            fails.append("T6 %s 缺失" % name)
    cases += 1
    # T7 前台保护三态 + 断链重试常量边界
    s, w = front_decision(True, 0)
    if not s or "前台" not in w:
        fails.append("T7 front True 未跳过: %s" % w)
    s2, _ = front_decision(False, 0)
    if s2:
        fails.append("T7 front False 不应跳过")
    s3, _ = front_decision(None, 0)
    if not s3:
        fails.append("T7 None 容忍内应 fail-closed 跳过")
    s4, _ = front_decision(None, FRONT_NONE_TOLERANCE)
    if s4:
        fails.append("T7 None 达容忍上限应放行(fail-open)")
    if not (SESS_RETRY_MAX >= 2 and 0 < SESS_RETRY_SLEEP[0] < SESS_RETRY_SLEEP[1]):
        fails.append("T7n SESS_RETRY 常量边界错")
    if not ("hasFocus" in FRONT_JS and "visibilityState" in FRONT_JS and "JSON.stringify" in FRONT_JS):
        fails.append("T7n FRONT_JS 形状缺关键 token")
    cases += 1
    # T7o 短退避常量边界 + 必须显著短于长退避（2026-10-06：Chrome 冷启动 1-2min，旧码首轮就睡 10-20min）
    if not (SESS_RETRY_SHORT_MAX >= 1 and 0 < SESS_RETRY_SHORT[0] < SESS_RETRY_SHORT[1]):
        fails.append("T7o SESS_RETRY_SHORT 常量边界错")
    if not (SESS_RETRY_SHORT[1] < SESS_RETRY_SLEEP[0]):
        fails.append("T7o 短退避未显著短于长退避")
    if not ("io" in globals()):
        fails.append("T7o io 未导入(try_ensure_tab 台账会静默失败)")
    cases += 1
    # T7p/T7q try_ensure_tab：EXISTS 短路不记账；超日上限拒绝（沙箱 BASE + 桩 subprocess）
    _pb, _psub = globals()["BASE"], globals()["subprocess"]
    _sd = tempfile.mkdtemp(prefix="ljq_selftest_tab_")
    class _ShimSub(object):
        out = "URLS:https://linux.do/"      # 默认=主站标签在（短路分支）
        @classmethod
        def run(cls, *a, **k):
            class _R(object):
                returncode = 0
                stdout = _ShimSub.out
                stderr = ""
            return _R()
    try:
        globals()["BASE"] = _sd
        globals()["subprocess"] = _ShimSub
        _okx, _notex = try_ensure_tab(now=time.time())
        if _okx or "已有" not in str(_notex):
            fails.append("T7p 主站标签应短路不新建: %r/%r" % (_okx, _notex))
        if os.path.exists(os.path.join(_sd, "ensure_tab.jsonl")):
            fails.append("T7p 短路分支不应写 ensure_tab 台账")
        # T7r 负控（2026-10-06 停摆根因）：只有 connect./sharedchat 这类「含 linux.do 子串但非主站」
        #     的标签时，必须判「无主站标签」并新建；被子串骗过则自愈恒不触发、浏览空转。
        _ShimSub.out = "URLS:https://connect.linux.do/\nhttps://sharedchat.example/room/1"
        _ok_r, _note_r = try_ensure_tab(now=time.time())
        if not _ok_r:
            fails.append("T7r 仅 connect/非主站标签应判无主站并新建: %r/%r" % (_ok_r, _note_r))
        if os.path.exists(os.path.join(_sd, "ensure_tab.jsonl")) is False:
            fails.append("T7r 新建分支应写 ensure_tab 台账")
        if _is_main_host("connect.linux.do") or _is_main_host("sharedchat.linux.do.evil.example"):
            fails.append("T7r _is_main_host 子串误判(应精确匹配)")
        if not (_is_main_host("linux.do") and _is_main_host("www.linux.do")):
            fails.append("T7r _is_main_host 主站识别错")
        if _url_host("https://connect.linux.do/x?y=1") != "connect.linux.do":
            fails.append("T7r _url_host 解析错: %r" % _url_host("https://connect.linux.do/x?y=1"))
        _day = time.strftime("%Y-%m-%d")
        with io.open(os.path.join(_sd, "ensure_tab.jsonl"), "w", encoding="utf-8") as _f:
            for _i in range(6):
                _f.write(json.dumps({"ts": time.time() - 1, "day": _day, "ok": True, "note": "x"}) + "\n")
        _ok2, _note2 = try_ensure_tab(now=time.time())
        if _ok2 or "上限" not in str(_note2):
            fails.append("T7q 超日上限应拒绝: %r/%r" % (_ok2, _note2))
        _ok3, _note3 = try_ensure_tab(now=time.time())
        if _ok3 or "上限" not in str(_note3):
            fails.append("T7q 台账读取应可复现(io 已导入): %r/%r" % (_ok3, _note3))
    finally:
        globals()["BASE"], globals()["subprocess"] = _pb, _psub
        shutil.rmtree(_sd, ignore_errors=True)
    # T7s 结构闸门（2026-10-06 三处停摆修复的回归锚点；读源码不绑现场内容）
    import inspect as _insp
    _src_cl = _insp.getsource(check_level)
    if "finally:" not in _src_cl or "location.href='https://linux.do/'" not in _src_cl:
        fails.append("T7s check_level 缺 finally 还原主站")
    if "non-linuxdo-tab" not in _insp.getsource(browse_batch):
        fails.append("T7s browse_batch 缺会话漂移即中止")
    if "_is_main_host" not in _insp.getsource(try_ensure_tab):
        fails.append("T7s try_ensure_tab 未用精确 host 判据")
    cases += 1
    # T15 批内风控熔断（R563，2026-10-08 回归）：抓取阶段即判，强风控 ∧ 零成功页 ⇒ ABORT
    if fetch_verdict(4, 0, {"status": 403}) != ("ABORT", "FORBIDDEN"):
        fails.append("T15 全页 403 应判 ABORT")
    if fetch_verdict(4, 0, {"status": 429}) != ("ABORT", "RATE_LIMIT"):
        fails.append("T15 全页 429 应判 ABORT")
    if fetch_verdict(4, 0, {"body": "您的账号已被封禁"}) != ("ABORT", "BANNED"):
        fails.append("T15 封禁文案应判 ABORT")
    if fetch_verdict(4, 3, {"status": 403}) != ("NOTE", "FORBIDDEN"):
        fails.append("T15n 部分页成功不得整批熔断（应仅 NOTE）")
    if fetch_verdict(4, 0, {"status": 500, "body": "internal"}) != (None, None):
        fails.append("T15n 非风控码全失败不得判 ABORT")
    if fetch_verdict(4, 4, {"status": 200}) != (None, None):
        fails.append("T15n 全成功不得判风控")
    if fetch_verdict(4, 0, {"body": "Just a moment..."}) != ("NOTE", "CF_CHALLENGE"):
        fails.append("T15n CF 挑战非强信号（只 NOTE 不熔断）")
    _ra = RiskAbort("FORBIDDEN", "page=5 status=403")
    if _ra.kind != "FORBIDDEN" or "403" not in _ra.detail:
        fails.append("T15 RiskAbort 未携带 kind/detail")
    # 结构闸门：熔断必须发生在候选抓取之后（否则等于没做），且两处调用点都接住
    _src_bb = _insp.getsource(browse_batch)
    if "raise RiskAbort" not in _src_bb:
        fails.append("T15s browse_batch 缺批内 raise RiskAbort")
    elif _src_bb.index("raise RiskAbort") < _src_bb.index("fetch_candidates(session, p)"):
        fails.append("T15s raise 位置早于抓取循环（形同未熔断）")
    if "except RiskAbort" not in _insp.getsource(watch):
        fails.append("T15s watch 未接住 RiskAbort")
    if "except RiskAbort" not in _insp.getsource(main):
        fails.append("T15s main/browse 未接住 RiskAbort")
    cases += 1
    cst = {"cap": {}}
    c1 = daily_cap_today(cst, 250)
    c2 = daily_cap_today(cst, 250)
    if abs(c1 - 250) > DAILY_CAP_JITTER or abs(c2 - 250) > DAILY_CAP_JITTER:
        fails.append("T8 cap 抖动越界: %s/%s" % (c1, c2))
    if c1 != c2:
        fails.append("T8 同 st 二次调用不稳定: %s vs %s" % (c1, c2))
    if not (1 <= DAILY_CAP_JITTER <= 60):
        fails.append("T8 抖动常量边界错")
    if daily_cap_today({"cap": {today(): 999}}, 250) != 999:
        fails.append("T8 已存当日 cap 未直接返回")
    jst = {"jitter": {}}
    j1 = morning_jitter_sec(jst)
    j2 = morning_jitter_sec(jst)
    if not (0 <= j1 <= MORNING_JITTER[1] and j1 == j2):
        fails.append("T9 晨抖越界或不稳定: %s vs %s" % (j1, j2))
    if morning_jitter_sec({"jitter": {today(): 123}}) != 123:
        fails.append("T9 已存晨抖未直接返回")
    if not (isinstance(MORNING_JITTER, tuple) and MORNING_JITTER[0] == 0 and MORNING_JITTER[1] > 0):
        fails.append("T9 晨抖常量形状错")
    cases += 1
    # T10 风控判据：HTTP 码 / 页面文本（正例 + 负控）
    if (risk_kind_from_http(429), risk_kind_from_http(403), risk_kind_from_http(401)) != ("RATE_LIMIT", "FORBIDDEN", "LOGIN_REQUIRED"):
        fails.append("T10 HTTP 风控码映射错")
    if risk_kind_from_http(200) is not None or risk_kind_from_http(404) is not None:
        fails.append("T10n 非风控码被误判为风控")
    if risk_kind_from_text("Just a moment...") != "CF_CHALLENGE":
        fails.append("T10 CF 挑战未识别")
    if risk_kind_from_text("您的账号已被封禁") != "BANNED":
        fails.append("T10 封禁文案未识别")
    if risk_kind_from_text("今天聊了 429 次，很有趣") is not None:
        fails.append("T10n 普通文本被误判为风控（数字/口语不算信号）")
    if not (set(RISK_STRONG) <= (set(k for k, _ in RISK_TEXT_HINTS) | {"FORBIDDEN"})):
        fails.append("T10n RISK_STRONG 含未定义类别")
    if "CF_CHALLENGE" in RISK_STRONG:
        fails.append("T10n CF 挑战列入强信号会误伤成长暂停")
    cases += 1
    # T11/T12 风控链路：stub 通知/落盘（绝不真发飞书）+ 零暂停
    # 验证 计数 → 首次告警 → 冷却窗口内去重 → 达阈值升级当日停
    global notify, save_state, log_jsonl, RISK_PAUSE
    sent = []
    _old = (notify, save_state, log_jsonl, RISK_PAUSE)
    notify = lambda text: (sent.append(text), True)[1]
    save_state = lambda st: None
    log_jsonl = lambda name, obj: None
    RISK_PAUSE = (0, 0)
    try:
        rst = {}
        c1, a1 = note_risk(rst, "RATE_LIMIT", "http 429")
        c2, a2 = note_risk(rst, "RATE_LIMIT", "http 429")
        if (c1, c2) != (1, 2) or not a1 or a2:
            fails.append("T11 计数/告警去重错: %s/%s/%s/%s" % (c1, c2, a1, a2))
        if len(sent) != 1 or "RATE_LIMIT" not in sent[0]:
            fails.append("T11 首次告警缺失或冷却窗口失效: n=%d" % len(sent))
        c3, _ = note_risk(rst, "RATE_LIMIT", "http 429")
        if c3 != RISK_ESCALATE_N:
            fails.append("T11 升级计数错: %s" % c3)
        if rst.get("risk", {}).get("last", {}).get("count_today") != RISK_ESCALATE_N:
            fails.append("T11 risk.last 未留痕")
        hst = {}
        for _ in range(RISK_ESCALATE_N):
            if handle_risk(hst, "BANNED", "账号被封") is not True:
                fails.append("T12 handle_risk 未返回 True")
        if hst.get("risk_stop_day") != today():
            fails.append("T12 达阈值未写当日停止标记（会继续硬撞）")
        if not any("BANNED" in s for s in sent):
            fails.append("T12 风控告警未发出")
        if not any("暂停" in s for s in sent):
            fails.append("T12 告警正文缺动作说明")
    finally:
        notify, save_state, log_jsonl, RISK_PAUSE = _old
    cases += 1
    # T13 休息日：幂等 + 最小间隔（不连休）+ 常量边界
    st13 = {"rest_days": {}}
    if rest_day_today(st13) != rest_day_today(st13):
        fails.append("T13 休息日判定不幂等（同 st 二次结果不同）")
    yd = (dt.datetime.strptime(today(), "%Y-%m-%d").date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    if rest_day_today({"rest_days": {yd: True}}):
        fails.append("T13 最小间隔未生效（可连休）")
    if not (0 < REST_DAY_P <= 0.5 and REST_MIN_GAP_DAYS >= 2):
        fails.append("T13 休息日常量边界错")
    # T15 段计时持久化：当日段表由内核按日期确定性派生（守护重启/被杀不重置段位，R545 缺陷①）
    if _ad is None:
        fails.append("T15 内核缺失：无法保证段计时持久化（回退旧行为）")
    else:
        segs = _day_segments_cached("2026-10-06")
        _SEG_CACHE["day"], _SEG_CACHE["segs"] = None, None      # 模拟「重启」：缓存全丢
        segs2 = _day_segments_cached("2026-10-06")
        if not segs or segs != segs2:
            fails.append("T15 段表不确定（重启后段位会变）: %s vs %s" % (segs, segs2))
        # 结构判据（内核口径：段是 dict{start,end,minutes}）：
        def _bad_seg(ss):
            out = []
            for i, s in enumerate(ss):
                if not (s.get("start") and s.get("end") and s["start"] < s["end"]):
                    out.append(i)
                elif i and not (ss[i - 1]["end"] <= s["start"]):
                    out.append(i)
            return out
        bad = _bad_seg(segs or [])
        if bad:
            fails.append("T15b 段表结构异常（乱序/重叠/零长）: %s" % ([segs[i] for i in bad][:2],))
        # 段内 ⇒ in_segment True；段外 ⇒ False 且 wait_hint>0（段间空档靠等待，等价于「重启不重置段位」）
        if segs:
            mid = (segs[0]["start"] + segs[0]["end"]) / 2.0
            ok_in, end_in = _ad.in_segment(mid, segs)
            if not ok_in:
                fails.append("T15c 段中点应判在段内")
            outside = segs[-1]["end"] + 60
            ok_out, _ = _ad.in_segment(outside, segs)
            if ok_out:
                fails.append("T15d 段外应判不在段内")
            else:
                w, why = _ad.wait_hint(outside, segs)
                if not (w > 0):
                    fails.append("T15e 段外 wait_hint 应为正: %s" % why)
            # T15g/T15h：段闸门必须真按内核返回值决策（2026-10-06 修：元组真值坑）
            _g_out = segment_gate(outside, segs)
            if _g_out[0] is not False or not (_g_out[1] > 0) or not _g_out[2]:
                fails.append("T15g 段闸门未拦段外: %s" % (_g_out,))
            _g_in = segment_gate(mid, segs)
            if _g_in[0] is not True or _g_in[1] != 0.0:
                fails.append("T15h 段内应放行且零等待: %s" % (_g_in,))
            if not isinstance(_ad.in_segment(mid, segs), tuple):
                fails.append("T15h2 内核 in_segment 契约变更（须返回元组）")
            _keep_is = _ad.in_segment
            _ad.in_segment = lambda ts, sg: True          # 退化内核：裸 bool（旧坑形态）
            try:
                _loud = False
                try:
                    segment_gate(outside, segs)
                except Exception:
                    _loud = True
                if not _loud:
                    fails.append("T15h3 内核契约退化时须响亮失败，不得静默放行")
            finally:
                _ad.in_segment = _keep_is
            if segment_gate(outside, segs)[0] is not False:
                fails.append("T15h4 负控后未恢复")
        # 跨天不重排（同日稳定 + 不同日一般不同），且每个段都在静默窗外 [07:30, 23:59)
        other = _day_segments_cached("2026-10-07")
        if (not other) or [_bad_seg(segs), _bad_seg(other)] != [[], []]:
            fails.append("T15f 相邻日段表不可用/结构异常")
    cases += 1
    # T16 节奏参数单一源：daemon 不得自带第二份常量（R545 缺陷③：死常量+漂移不自知）
    if _ad is not None:
        try:
            _pf = _ad.profile("browse")
            pairs = [("gap", GAP_RANGE), ("gap_long", GAP_LONG_RANGE),
                     ("daily_cap", DAILY_CAP_DEFAULT), ("cap_jitter", DAILY_CAP_JITTER),
                     ("morning_jitter", MORNING_JITTER), ("quiet", QUIET_WINDOW),
                     ("rest_p", REST_DAY_P), ("rest_min_gap", REST_MIN_GAP_DAYS)]
            drift = [(k, v, _pf.get(k)) for k, v in pairs if _pf.get(k) != v]
            if drift:
                fails.append("T16 节奏常量与内核漂移: %s" % drift)
        except Exception as e:
            fails.append("T16 内核 profile 不可读: %s" % e)
    cases += 1
    # T16b 批间休=内核单一源（R545 缺陷③续）：取值必须落在 内核 gap_long×MULT 内，且
    # 内核缺席时回落本地镜像区间；两态都不许出现「第二份常量」形态的固定区间。
    try:
        gs = [_batch_gap() for _ in range(60)]
        if _ad is not None:
            _lo, _hi = _ad.profile("browse")["gap_long"]
            _exp = (_lo * BATCH_GAP_MULT, _hi * BATCH_GAP_MULT)
        else:
            _exp = (GAP_LONG_RANGE[0] * BATCH_GAP_MULT, GAP_LONG_RANGE[1] * BATCH_GAP_MULT)
        if any((g < _exp[0] - 0.05) or (g > _exp[1] + 0.05) for g in gs):
            fails.append("T16b 批间休越出内核 gap_long×MULT: %s" % (sorted(set(gs))[:3],))
        if len(set(gs)) < 3:
            fails.append("T16b 批间休取值退化为常量: %s" % (sorted(set(gs))[:3],))
    except Exception as e:
        fails.append("T16b 批间休计算异常: %s" % e)
    cases += 1
    # T17 节流行不得污染等级台账（R545 缺陷④）：throttled 分支只计 level_skip，不写 level_log/不刷 ts
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    # 自指坑：本用例自身若含完整字面量会被算作「第二处」，故分片拼接（NC 自证）
    _mark = "if src == " + chr(34) + "throttled" + chr(34) + ":"
    if _src.count(_mark) != 1:
        fails.append("T17 节流分支定位失败（命中 %d 次）" % _src.count(_mark))
    else:
        _lines = _src.split("\n")
        _i = [k for k, l in enumerate(_lines) if l.strip() == _mark][0]
        _base = len(_lines[_i]) - len(_lines[_i].lstrip())
        _body = []
        for _l in _lines[_i + 1:]:
            if _l.strip() and (len(_l) - len(_l.lstrip())) <= _base:
                break
            _body.append(_l)
        # 只判代码：注释里复述旧行为（"旧实现照样写 level_log"）不算写入点
        _code = "\n".join(l for l in _body if not l.strip().startswith("#"))
        if "log_jsonl(" in _code or "level_check_mark(" in _code or ('st["level"] =' in _code):
            fails.append("T17 节流分支仍在写等级台账/刷 ts（污染检查频次口径）")
        if ("level" + "_skip") not in _code:
            fails.append("T17 节流分支未计 level_skip")
        if ("level" + "_log.jsonl") not in _src:
            fails.append("T17 台账写入点整体消失（过度删除）")
    cases += 1

    # ---- T18 段级审计视图（R546 #5，只读派生）：结构判据，不绑现场内容（RULE 25）
    _d = today()
    _segs = _day_segments_cached(_d)
    if not _segs:
        fails.append("T18 day_segments 内核缺席（段级审计会退化成 note，节律不可观测）")
    else:
        # 用真实段表造合成台账（只落沙箱 BASE）：段内各一条 DONE + 一条真长休
        _t0 = _segs[0]["start"] + 60
        _t1 = _segs[1]["start"] + 60 if len(_segs) >= 2 else None
        _rows = [{"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_t0)), "st": "DONE"}]
        if _t1:
            _rows.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_t1)), "st": "DONE"})
        # 负控行：非 DONE / 坏时间戳 / 昨天 —— 一律不得计入
        _rows.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_t0)), "st": "FAIL"})
        _rows.append({"ts": "not-a-ts", "st": "DONE"})
        _rows.append({"ts": "1999-01-01 00:00:00", "st": "DONE"})
        with open(os.path.join(BASE, "browse_log.jsonl"), "w", encoding="utf-8") as f:
            for r in _rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        _sa = segment_audit(day=_d, now=_t0 + 120)
        if _sa["theoretical"] != len(_segs):
            fails.append("T18 理论段数应=%d: %s" % (len(_segs), _sa["theoretical"]))
        _exp_seen = 2 if len(_segs) >= 2 else 1
        if _sa["segments_seen"] != _exp_seen or _sa["segments_seen_idx"] != list(range(1, _exp_seen + 1)):
            fails.append("T18b 实际进段数/段号错（负控行须被剔除）: %s" % _sa)
        if _sa["browsed_in_segment"] != 1:
            fails.append("T18c 段内已浏览应只数本段 DONE: %s" % _sa["browsed_in_segment"])
        if _sa["current_index"] != 1 or not _sa["current_window"]:
            fails.append("T18d now 落段内须给 current_index=1+窗口: %s" % _sa)
        elif _sa["current_window"][2] != _segs[0]["minutes"]:
            fails.append("T18e 窗口段长须与内核逐字一致: %s" % (_sa["current_window"], _segs[0]["minutes"]))
        if len(_segs) >= 2:
            _gap = _t1 - _t0
            if _sa["rests_n"] != 1 or _sa["rests_long_n"] != (1 if _gap >= SESSION_REST[0] else 0):
                fails.append("T18f 休息/长休判定错(gap=%.0fs): %s" % (_gap, _sa["rests"]))
        else:
            if _sa["rests_n"] != 0:
                fails.append("T18g 单段日不得有休息: %s" % _sa["rests"])
        # T18h 负控：空台账 ⇒ 零覆盖、零休息（不得伪造）
        with open(os.path.join(BASE, "browse_log_empty.jsonl"), "w"):
            pass
        _ea = segment_audit(day=_d, now=_t0 + 120, log_name="browse_log_empty.jsonl")
        if _ea["segments_seen"] != 0 or _ea["rests_n"] != 0 or _ea["browsed_in_segment"] != 0:
            fails.append("T18h 空台账须零覆盖: %s" % _ea)
        # T18i 负控：台账缺失 ⇒ 同样零覆盖且不抛
        _ma = segment_audit(day=_d, now=_t0 + 120, log_name="no_such_log.jsonl")
        if _ma["segments_seen"] != 0:
            fails.append("T18i 缺台账须零覆盖: %s" % _ma)
        # T18j 负控：内核缺席（注入空段表）⇒ note + 字段 null，绝不伪造
        _g = globals()
        _keep = _g["_day_segments_cached"]
        _g["_day_segments_cached"] = lambda day=None: None
        try:
            _na = segment_audit(day=_d, now=_t0 + 120)
        finally:
            _g["_day_segments_cached"] = _keep
        if not _na["note"] or _na["theoretical"] is not None or _na["current_index"] is not None:
            fails.append("T18j 内核缺席须 note+null: %s" % _na)
    cases += 1

    return fails, cases


def _state_fp():
    """生产 state.json 指纹（sha256 前16 + 字节数）；不存在→None。"""
    p = os.path.join(BASE, "state.json")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        data = f.read()
    return hashlib.sha256(data).hexdigest()[:16], len(data)


def _run_selftest_sandboxed():
    """直调 _selftest_impl 的兜底沙箱（与 selftest() 同口径，幂等可重入）。"""
    global BASE, _SANDBOX_ACTIVE
    orig = BASE
    sb = tempfile.mkdtemp(prefix="ldo_selftest_")
    _SANDBOX_ACTIVE = True
    try:
        BASE = sb
        return _selftest_impl()
    finally:
        BASE = orig
        _SANDBOX_ACTIVE = False
        shutil.rmtree(sb, ignore_errors=True)


def selftest():
    """沙箱化自测 + 生产面零污染自证（T14）。

    2026-10-05 事故：T13 走 rest_day_today(st) → save_state(st)，把只含 rest_days 的
    局部 dict 直接写成生产 state.json，抹掉 daily/seen_ids/browsed_total ⇒ watch 崩。
    根修：整个 self-test 在临时 BASE 内跑；并比对前后生产 state.json 指纹（不一致即 FAIL）。
    """
    global BASE, _SANDBOX_ACTIVE
    orig = BASE
    sb = tempfile.mkdtemp(prefix="ldo_selftest_")
    before = _state_fp()
    prev_sb = _SANDBOX_ACTIVE
    try:
        BASE = sb
        _SANDBOX_ACTIVE = True
        fails, cases = _selftest_impl()
    finally:
        BASE = orig
        _SANDBOX_ACTIVE = prev_sb
        shutil.rmtree(sb, ignore_errors=True)
    cases += 1
    after = _state_fp()
    if before != after:
        fails.append("T14 selftest 污染生产 state.json：%r → %r" % (before, after))
    if fails:
        for f in fails:
            print("FAIL " + f)
        return RC_SELFTEST
    print("selftest %d/%d PASS" % (cases, cases))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
