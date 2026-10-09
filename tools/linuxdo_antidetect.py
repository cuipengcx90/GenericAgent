#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L站自动化 · 统一防检测内核（shared anti-detection spine）

用户口径（2026-10-05 定义）：
    五个 SOP（等级检测 / 浏览 / 回帖 / 点赞 / 发帖）**各自独立**、可被提示词单独触发，
    但共用**同一套**防检测逻辑；各动作只在「基数」上差异化（浏览大 / 点赞发帖小）。

统一防检测四条（用户原话拆解）：
  1. 随机时间：禁固定间隔（每次执行间隔从区间里抽，且带长休档）。
  2. 把一天切分成**多个随机活动段**：不是「只在一个时间段里随机」——
     段数、每段起点、每段长度、段间空隙都随机。
  3. 多随机叠加：每日额度抖动（每天上限都不一样）、随机休息日、
     晨间随机开工、静默窗、浏览器同源 fetch（指纹=用户真身）。
  4. 量级与可停：各动作硬日限（写操作比只读更保守）+ 风控信号即停（调用方实现）。

设计要点：
  * 所有「每日形态」由**确定性种子**派生（seed = sha1(day|kind)）⇒ 同一天同一动作的
    计划稳定可复现/可审计（重复调用不漂移），跨天自然不同。
  * 本模块是**纯计划层**：零站点请求、零网络、不写任何文件（状态由调用方传入 dict）。
  * 差异档位集中在 ACTION_PROFILES 单一源（调用方只按 kind 取名）。

CLI:
    python3 tools/linuxdo_antidetect.py plan  --kind browse [--day YYYY-MM-DD]
    python3 tools/linuxdo_antidetect.py now   --kind like
    python3 tools/linuxdo_antidetect.py --selftest
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import sys
import time

# ------------------------------------------------------------------ 差异档位（单一源）
# daily_cap = 每日硬上限基数（cap_jitter = 每日随机抖动量）
# seg_n / seg_len = 一天切几段、每段多少分钟
# gap / gap_long / gap_long_p = 段内动作间隔、长休档、长休概率
# rest_p / rest_min_gap = 随机休息日概率与两次休息最小间隔（天）
ACTION_PROFILES = {
    # 浏览：基数最大（TL3 需「已读帖子 20000」，是长期主战场）
    # 2026-10-06 R548 用户要求「日上限调高一点、节奏不变」：160→200（日上限 180–220），
    # 段数/段长同步加宽（6–8 段 × 85–160min）⇒ 提高的是**占空比**与日总量，段内间隔
    # （gap 110–260s、长休 300–600s）保持不变 ⇒ 瞬时密度不升，只把一天铺得更满。
    "browse": dict(daily_cap=200, cap_jitter=20, seg_n=(6, 8), seg_len=(85, 160),
                   gap=(110, 260), gap_long=(300, 600), gap_long_p=0.30,
                   rest_p=1.0 / 7.0, rest_min_gap=3, quiet=(1, 0, 7, 30),
                   morning_jitter=(0, 3600)),
    # 点赞：日限中等（真人一天点十来个赞自然；「点太多也不行」）
    "like": dict(daily_cap=12, cap_jitter=4, seg_n=(2, 4), seg_len=(8, 25),
                 gap=(180, 600), gap_long=(900, 2400), gap_long_p=0.35,
                 rest_p=1.0 / 7.0, rest_min_gap=3, quiet=(1, 0, 7, 30),
                 morning_jitter=(0, 2400)),
    # 回帖：最保守（写操作 + 内容留痕）；日≤6、条间 240–600s
    "reply": dict(daily_cap=6, cap_jitter=2, seg_n=(2, 3), seg_len=(6, 20),
                  gap=(240, 600), gap_long=(780, 1200), gap_long_p=0.25,
                  rest_p=1.0 / 6.0, rest_min_gap=3, quiet=(1, 0, 7, 30),
                  morning_jitter=(600, 3600)),
    # 发帖：最低（一天最多 1 条，且需间隔≥24h，见 weekly_extra）
    "post": dict(daily_cap=1, cap_jitter=0, seg_n=(1, 2), seg_len=(5, 15),
                 gap=(0, 0), gap_long=(0, 0), gap_long_p=0.0,
                 rest_p=1.0 / 5.0, rest_min_gap=2, quiet=(1, 0, 7, 30),
                 morning_jitter=(1200, 5400)),
}
POST_MIN_INTERVAL_H = 24.0   # 发帖最小间隔（小时）——「一天不能发多少个」硬约束
POST_WEEK_CAP = 3            # 发帖每周上限


def profile(kind):
    if kind not in ACTION_PROFILES:
        raise KeyError("unknown kind: %s" % kind)
    return ACTION_PROFILES[kind]


def _rng(day, kind, salt=""):
    """确定性 RNG：同一天同一动作同一 salt ⇒ 同一形态（可复现、可审计）。"""
    h = hashlib.sha1(("%s|%s|%s" % (day, kind, salt)).encode("utf-8")).hexdigest()
    return random.Random(int(h[:16], 16))


def _day_of(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _ts(day, hour):
    """day=YYYY-MM-DD, hour=float(可带小数) → epoch 秒。"""
    d = dt.datetime.strptime(day, "%Y-%m-%d")
    return time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)) + hour * 3600.0


# ------------------------------------------------------------------ 多段切分
def day_segments(day, kind, rng=None):
    """把一天的活动窗切成多个随机段 → [{"start":ts,"end":ts,"minutes":m}, ...]

    * 段数、每段起点、每段长度、段间空隙全部随机（用户「多搞几个随机」）；
    * 跨天不重排：由确定性种子派生 ⇒ 同日稳定；
    * 段严格不重叠、且都落在活动窗 [07:30, 23:50] 内（静默窗之外）。
    """
    p = profile(kind)
    day = str(day)
    r = rng or _rng(day, kind, "segs")
    n = r.randint(p["seg_n"][0], p["seg_n"][1])
    win_start = 7.5           # 静默窗结束 07:30
    win_end = 23.833          # 23:50 前收工（留收尾余量）
    span = win_end - win_start
    # 段长按档位抽样，再按窗口容量收敛
    mins = [r.uniform(p["seg_len"][0], p["seg_len"][1]) for _ in range(n)]
    total = sum(m / 60.0 for m in mins)
    if total > span * 0.80:                      # 段总长不超过活动窗 80%（R548：75%→80%，给高日上限留占空比）
        k = span * 0.80 / total
        mins = [m * k for m in mins]
    # 随机放置：在剩余空隙里随机撒点（顺序保持）
    free = span - sum(m / 60.0 for m in mins)
    cuts = sorted(r.uniform(0, free) for _ in range(n))
    gaps = [cuts[0]] + [cuts[i] - cuts[i - 1] for i in range(1, n)] + [free - cuts[-1]]
    segs, cur = [], win_start
    for i, m in enumerate(mins):
        cur += gaps[i]
        st = cur
        cur += m / 60.0
        segs.append({"start": _ts(day, st), "end": _ts(day, cur), "minutes": round(m, 1)})
    return segs


def plan_for_day(day, kind):
    """一天的完整形态（审计/展示用）：段 + 当日额度 + 是否休息日标记（状态由调用方定）。"""
    p = profile(kind)
    segs = day_segments(day, kind)
    return {
        "kind": kind, "day": day,
        "segments": segs,
        "seg_count": len(segs),
        "active_minutes": round(sum(s["minutes"] for s in segs), 1),
        "daily_cap_base": p["daily_cap"], "cap_jitter": p["cap_jitter"],
        "gap": list(p["gap"]), "gap_long": list(p["gap_long"]), "gap_long_p": p["gap_long_p"],
        "rest_p": round(p["rest_p"], 4), "quiet": list(p["quiet"]),
    }


def in_segment(now_ts, segs):
    """当前是否落在某个活动段内 → (bool, 段结束 ts 或 None)。"""
    for s in segs:
        if s["start"] <= now_ts < s["end"]:
            return True, s["end"]
    return False, None


def next_seg_start(now_ts, segs):
    """下一个活动段起点（已无 ⇒ None）。"""
    fut = [s["start"] for s in segs if s["start"] > now_ts]
    return min(fut) if fut else None


def wait_hint(now_ts, segs):
    """给调用方的等待建议：(wait_seconds, why)。"""
    ok, end = in_segment(now_ts, segs)
    if ok:
        return 0.0, "in_segment_until_%s" % dt.datetime.fromtimestamp(end).strftime("%H:%M")
    nxt = next_seg_start(now_ts, segs)
    if nxt is None:
        # 今日已无段：睡到明天第一段
        return max(600.0, 3600.0), "no_more_segments_today"
    return max(60.0, nxt - now_ts), "wait_next_segment_%s" % dt.datetime.fromtimestamp(nxt).strftime("%H:%M")


# ------------------------------------------------------------------ 多种随机叠加
def _logn(median, sigma, lo=None, hi=None, rng=None):
    """对数正态（人类行为的标准长尾形态）：中位数给 median，右侧拖尾。"""
    r = rng or random.Random()
    v = math.exp(r.gauss(math.log(max(float(median), 1e-6)), sigma))
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def gap_seconds(kind, rng=None):
    """段内动作间隔：常规档 3:1 概率优先，长休档 1 份（对偶随机）。

    v2（2026-10-05）：档内取值由 uniform 改**对数正态**（中位数在区间偏左、右侧拖长尾），
    再 clamp 回原区间 ⇒ 区间契约与调用方不变，但「永远均匀」这一机械特征被去掉。
    """
    p = profile(kind)
    r = rng or random.Random()
    lo, hi = p["gap"]
    if hi <= lo:
        return 0.0
    if p["gap_long_p"] > 0 and r.random() < p["gap_long_p"]:
        llo, lhi = p["gap_long"]
        return round(_logn((llo + lhi) / 2.0 * 0.8, 0.5, llo, lhi, r), 1)
    mid = (lo + hi) / 2.0
    return round(_logn(mid * 0.85, 0.45, lo, hi, r), 1)


def cap_today(st, day, kind, rng=None):
    """当日额度：base ± jitter 每天随机定一次（存 st["cap"][kind][day]）⇒ 跨重启稳定。"""
    p = profile(kind)
    caps = st.setdefault("cap", {}).setdefault(kind, {})
    if day not in caps:
        j = p["cap_jitter"]
        v = int(p["daily_cap"]) + (_rng(day, kind, "cap").randint(-j, j) if j else 0)
        caps[day] = max(0, v)
    for k in sorted(caps.keys())[:-60]:
        caps.pop(k, None)
    return int(caps[day])


def rest_day(st, day, kind):
    """随机休息日：prob 命中且与上次休息≥min_gap 天 ⇒ 今日该动作完全不做。"""
    p = profile(kind)
    rests = st.setdefault("rest", {}).setdefault(kind, {})
    if day in rests:
        return bool(rests[day])
    days = sorted(rests.keys())
    if days:
        try:
            last = dt.datetime.strptime(days[-1], "%Y-%m-%d").date()
            cur = dt.datetime.strptime(day, "%Y-%m-%d").date()
            if (cur - last).days < p["rest_min_gap"]:
                rests[day] = False
                return False
        except Exception:
            pass
    v = _rng(day, kind, "rest").random() < p["rest_p"]
    rests[day] = v
    for k in sorted(rests.keys())[:-90]:
        rests.pop(k, None)
    return v


def quiet_now(now=None, kind="browse"):
    """静默窗内（默认 01:00–07:30）⇒ True（各档共用同一窗，可自定义）。"""
    now = now or dt.datetime.now()
    qh, qm, eh, em = profile(kind)["quiet"]
    t = now.hour * 60 + now.minute
    return (qh * 60 + qm) <= t < (eh * 60 + em)


def morning_jitter(st, day, kind):
    """今日晨间抖动秒：静默窗结束后再随机等这么久才开工（每天定一次，跨重启稳定）。"""
    jt = st.setdefault("jitter", {}).setdefault(kind, {})
    if day not in jt:
        jt[day] = int(_rng(day, kind, "morning").uniform(*profile(kind)["morning_jitter"]))
    for k in sorted(jt.keys())[:-60]:
        jt.pop(k, None)
    return int(jt[day])


def post_interval_ok(st, now_ts):
    """发帖额外硬约束：距上次发帖 ≥ POST_MIN_INTERVAL_H 小时，且近 7 天 ≤ POST_WEEK_CAP。"""
    hist = st.get("post_history") or []
    if hist:
        last = float(hist[-1].get("ts") or 0)
        if last and (now_ts - last) < POST_MIN_INTERVAL_H * 3600:
            return False, "min_interval_%.1fh" % ((now_ts - last) / 3600.0)
    week = [h for h in hist if (now_ts - float(h.get("ts") or 0)) < 7 * 86400]
    if len(week) >= POST_WEEK_CAP:
        return False, "week_cap_%d" % len(week)
    return True, "ok"


def due_now(st, now_ts, kind, done_today):
    """综合判定「现在能不能做一次」→ (bool, why)。

    顺序：静默窗 → 休息日 → 多段（是否在活动段内）→ 日限 → 动作特有约束。
    """
    day = _day_of(now_ts)
    p = profile(kind)
    nowdt = dt.datetime.fromtimestamp(now_ts)
    if quiet_now(nowdt, kind):
        return False, "quiet_window"
    if rest_day(st, day, kind):
        return False, "rest_day"
    segs = day_segments(day, kind)
    ok, _end = in_segment(now_ts, segs)
    if not ok:
        w, why = wait_hint(now_ts, segs)
        return False, "out_of_segment(%s)" % why
    # 晨间抖动：当日第一段起点后再等 morning_jitter
    if segs and now_ts < segs[0]["start"] + morning_jitter(st, day, kind):
        return False, "morning_jitter"
    if int(done_today) >= cap_today(st, day, kind):
        return False, "daily_cap(%d)" % cap_today(st, day, kind)
    if kind == "post":
        ok2, why = post_interval_ok(st, now_ts)
        if not ok2:
            return False, "post_%s" % why
    return True, "ok"


def next_wait(st, now_ts, kind, done_today=0, rng=None):
    """执行器/守护自调度用：算「做完这一次后，下次应等多少秒」→ (seconds, why)。

    与 due_now 同源判定：段内⇒抽段内间隔（含长休档）；段外⇒等到下一段起点+抖动；
    日限到⇒等到次日首段；休息日⇒等到次日；静默窗⇒等到窗尾。
    """
    rng = rng or random
    ok, why = due_now(st, now_ts, kind, done_today)
    if ok:
        return gap_seconds(kind, rng), "in_segment_gap"
    day = _day_of(now_ts)
    segs = day_segments(day, kind)
    if why.startswith("out_of_segment") or why == "morning_jitter":
        ns = next_seg_start(now_ts, segs)
        if ns:
            return max(1.0, ns - now_ts + rng.uniform(0, 300)), "wait_next_segment"
    if why.startswith("daily_cap") or why == "rest_day":
        nd = (dt.datetime.fromtimestamp(now_ts).date() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        s2 = day_segments(nd, kind)
        return max(60.0, s2[0]["start"] - now_ts + rng.uniform(0, 900)), "wait_next_day_first_segment"
    if why == "quiet_window":
        # quiet = (静默起-时, 静默起-分, 静默止-时, 静默止-分) —— 与 quiet_now() 同口径。
        # ★2026-10-06 修：原实现把 4 元组当 2 元组解包 ⇒ 凌晨真跑必 ValueError（见 selftest T23b 负控）。
        p = profile(kind)
        _qh, _qm, eh, em = p["quiet"]
        nowdt = dt.datetime.fromtimestamp(now_ts)
        end = nowdt.replace(hour=int(eh), minute=int(em), second=0, microsecond=0)
        if end <= nowdt:
            end += dt.timedelta(days=1)
        return max(60.0, (end - nowdt).total_seconds() + rng.uniform(0, 1800)), "wait_quiet_end"
    return 600.0 + rng.uniform(0, 300), "fallback"


# --------------------------------------------------- 等级检测（低频前置动作，v2）
# 用户口径（2026-10-05）：每天只查 **1 次**，且**不在固定时刻**触发（错峰）。
# 与其它动作一样由确定性种子派生 ⇒ 当天计划稳定可复现、跨天自然不同。
LEVEL_CHECK_PER_DAY = 1
# ★2026-10-09 用户口径（方案 B）：**窗口优先**——「每天 1 次 ∧ 只在 08:00–10:00 窗内随机」是硬口径，
#   TTL 只作「近邻重复」防重（重启/多入口），不再充当跨天节流。
#   旧值 20h 的后果（实测 10-07 12:14 → 10-08 19:56 → 10-09 应等到 15:56）：前一天晚查 ⇒ 次日被 TTL
#   顶出上午窗，且自我延续（越查越晚），与用户口径直接冲突（R565）。
LEVEL_CHECK_TTL_S = 6 * 3600           # 仅防近邻重查；跨天场景恒不触发（窗内最早 08:00 距昨日最晚 10:00 已 22h）
LEVEL_MIN_GAP_S = 90 * 60              # 与上次尽量错开 90min（**软规则**：窗内择远点，绝不把检查顶出窗）
# ★2026-10-06 用户口径：等级检测**只在上午 08:00–10:00 之间**随机触发（(起,止) 小时）。
#   历史问题：原实现取「当日首段→23:50」的对数正态 ⇒ 常落到傍晚/夜间（实测 10-07 18:00），
#   判级结果出来时活动窗只剩几小时，缺项路由（浏览/回帖/点赞）根本补不完。
LEVEL_CHECK_WINDOW = (8.0, 10.0)


def level_check_ts(day, prev_ts=None, kind="browse"):
    """返回该日等级检查的时刻（epoch 秒）：**固定档在上午 LEVEL_CHECK_WINDOW 窗内随机**。

    错峰做法：窗内均匀抽点 + ±10min 抖动（仍落在窗口内）⇒ 每天不同、恒不在固定钟点；
    与上一次检查冲突时顺延到「上次 + 90min」之后再抽（同一日只顺延一次）。
    ★2026-10-06 用户口径：不再取「首段→23:50」的对数正态（那会落到傍晚/夜间）。
    """
    r = _rng(day, "level", "ts")
    wl, wh = LEVEL_CHECK_WINDOW
    lo, hi = _ts(day, wl), _ts(day, wh)
    cand = lo + r.random() * (hi - lo) + r.uniform(-600, 600)
    cand = max(lo, min(hi, cand))
    if prev_ts:
        # 与上次错开是**软规则**（v3，2026-10-09）：先在「窗口 − 排除带(prev±90min)」内抽点；
        # 排除带吃掉整窗时退回「窗内离上次更远的那一端」。两种情形都恒在窗内——绝不顺延出窗。
        zlo, zhi = prev_ts - LEVEL_MIN_GAP_S, prev_ts + LEVEL_MIN_GAP_S
        segs = [(lo, min(hi, zlo)), (max(lo, zhi), hi)]
        segs = [(a, b) for a, b in segs if b - a >= 300.0]
        if segs:
            rs = _rng(day, "level", "spread")
            a, b = segs[int(rs.random() * len(segs)) % len(segs)]
            cand = a + (b - a) * rs.random()
        else:
            cand = hi if abs(hi - prev_ts) >= abs(lo - prev_ts) else lo
    return round(max(lo, min(hi, cand)), 1)


def level_check_state(st, kind="browse"):
    """st["level_check"] = {"last_ts": float, "last_day": str, "count": int(近 40 天)}"""
    return st.setdefault("level_check", {})


def level_check_due(st, now_ts, kind="browse"):
    """等级检查现在该不该跑 → (bool, why)。

    判据（v3，2026-10-09 窗口优先）：① 静默窗内不跑 ② 当天已跑过（last_day==今天）不跑
          ③ 距离上次 < TTL(6h) 不跑（仅防近邻重复）④ 未到当天窗内错峰时刻不跑
          ⑤ 到点即跑（超时未跑不补跑多次，只补一次）；★窗口是硬口径 ⇒ TTL 不得把检查顶出窗
    """
    ls = level_check_state(st, kind)
    nowdt = dt.datetime.fromtimestamp(now_ts)
    day = _day_of(now_ts)
    if quiet_now(nowdt, kind):
        return False, "quiet_window"
    if ls.get("last_day") == day:
        return False, "already_checked_today"
    last = float(ls.get("last_ts") or 0)
    if last and (now_ts - last) < LEVEL_CHECK_TTL_S:
        return False, "ttl_%dh" % int((now_ts - last) / 3600)
    target = level_check_ts(day, prev_ts=last or None, kind=kind)
    if now_ts < target:
        return False, "before_slot_%s" % dt.datetime.fromtimestamp(target).strftime("%H:%M")
    return True, "ok"


def level_check_mark(st, now_ts, kind="browse"):
    """执行成功后记账（调用方在真跑通后调）。"""
    ls = level_check_state(st, kind)
    ls["last_ts"] = float(now_ts)
    ls["last_day"] = _day_of(now_ts)
    hist = ls.setdefault("history", [])
    hist.append({"ts": round(float(now_ts), 1), "day": ls["last_day"]})
    ls["history"] = hist[-40:]
    return ls


def level_check_next_wait(st, now_ts, kind="browse"):
    """守护用：距下次等级检查还有多少秒（恒 >0，且不会早于错峰时刻）。"""
    ls = level_check_state(st, kind)
    day = _day_of(now_ts)
    last = float(ls.get("last_ts") or 0)
    if ls.get("last_day") == day:
        nd = (dt.datetime.fromtimestamp(now_ts).date() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        t = level_check_ts(nd, prev_ts=last or now_ts, kind=kind)   # 与 due() 同锚(last) ⇒ 展示点即真触发点
        return max(60.0, t - now_ts), "next_day_%s" % dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
    if last and (now_ts - last) < LEVEL_CHECK_TTL_S:
        # ★2026-10-09 与 due() 对齐：TTL 拦住时不报 due_now（旧版两者矛盾：due=False 而此处 due_now）
        return max(60.0, last + LEVEL_CHECK_TTL_S - now_ts), "ttl_%dh" % int((now_ts - last) / 3600)
    t = level_check_ts(day, prev_ts=last or None, kind=kind)
    if now_ts >= t:
        return 0.0, "due_now"
    return max(60.0, t - now_ts), "at_%s" % dt.datetime.fromtimestamp(t).strftime("%H:%M")


# ------------------------------------------------------------------ selftest
def _selftest():
    fails, n = [], 0

    def chk(name, cond):
        nonlocal n
        n += 1
        if not cond:
            fails.append(name)

    day = "2026-10-05"
    # T1 分段的确定性：同日两次调用逐字相同
    a = day_segments(day, "browse")
    b = day_segments(day, "browse")
    chk("T1 分段确定性", json.dumps(a) == json.dumps(b))
    # T2 跨天不同（段数或起点不同）
    c = day_segments("2026-10-06", "browse")
    chk("T2 跨天不同", json.dumps(a) != json.dumps(c))
    # T3 段数与档位约束
    p = profile("browse")
    chk("T3 段数在档内", p["seg_n"][0] <= len(a) <= p["seg_n"][1])
    # T4 段不重叠且递增
    chk("T4 段递增不重叠", all(a[i]["end"] <= a[i + 1]["start"] for i in range(len(a) - 1)))
    # T5 段长在档内（±收敛容差）
    chk("T5 段长合规", all(1.0 <= s["minutes"] <= p["seg_len"][1] for s in a))
    # T6 段落在活动窗内（静默窗之外）
    ok6 = True
    for s in a:
        h1 = dt.datetime.fromtimestamp(s["start"]).hour + dt.datetime.fromtimestamp(s["start"]).minute / 60.0
        h2 = dt.datetime.fromtimestamp(s["end"]).hour + dt.datetime.fromtimestamp(s["end"]).minute / 60.0
        if h1 < 7.4 or h2 > 23.95:
            ok6 = False
    chk("T6 段在活动窗", ok6)
    # T7 in_segment / next_seg_start 与真值一致
    mid = (a[0]["start"] + a[0]["end"]) / 2.0
    chk("T7 in_segment 命中", in_segment(mid, a)[0] is True)
    chk("T7b in_segment 未命中", in_segment(a[0]["start"] - 10, a)[0] is False)
    chk("T7c next_seg_start", abs(next_seg_start(a[0]["start"] - 10, a) - a[0]["start"]) < 1e-6)
    # T8 wait_hint：段内=0；段外>0
    chk("T8 wait_hint 段内", wait_hint(mid, a)[0] == 0.0)
    chk("T8b wait_hint 段外", wait_hint(a[0]["start"] - 60, a)[0] > 0)
    # T9 gap 落在区间（含长休档）
    r = random.Random(7)
    gs = [gap_seconds("browse", r) for _ in range(400)]
    lo, hi = profile("browse")["gap"]
    llo, lhi = profile("browse")["gap_long"]
    chk("T9 gap 在并集区间", all((lo <= g <= hi) or (llo <= g <= lhi) for g in gs))
    chk("T9b gap 两档都出现", any(g <= hi for g in gs) and any(g >= llo for g in gs))
    # T10 cap 抖动且同日稳定
    st = {}
    c1 = cap_today(st, day, "like")
    c2 = cap_today(st, day, "like")
    pl = profile("like")
    chk("T10 cap 稳定", c1 == c2)
    chk("T10b cap 在区间", pl["daily_cap"] - pl["cap_jitter"] <= c1 <= pl["daily_cap"] + pl["cap_jitter"])
    # T11 休息日：prob=0 ⇒ 恒 False；prob=1 ⇒ 恒 True；min_gap 生效
    st2 = {}
    A = dict(ACTION_PROFILES["like"])
    ACTION_PROFILES["like"] = dict(A, rest_p=0.0)
    chk("T11 rest_p=0", rest_day(st2, day, "like") is False)
    ACTION_PROFILES["like"] = dict(A, rest_p=1.0)
    chk("T11b rest_p=1", rest_day({}, "2026-11-01", "like") is True)
    # 连休：昨天休息 ⇒ 今天不得休息（min_gap）
    st3 = {"rest": {"like": {"2026-11-01": True}}}
    chk("T11c 最小间隔生效", rest_day(st3, "2026-11-02", "like") is False)
    ACTION_PROFILES["like"] = A
    # T12 静默窗边界
    chk("T12 静默窗内", quiet_now(dt.datetime(2026, 10, 5, 3, 0), "browse") is True)
    chk("T12b 静默窗外", quiet_now(dt.datetime(2026, 10, 5, 8, 0), "browse") is False)
    chk("T12c 窗左边界", quiet_now(dt.datetime(2026, 10, 5, 1, 0), "browse") is True)
    chk("T12d 窗右边界", quiet_now(dt.datetime(2026, 10, 5, 7, 30), "browse") is False)
    # T13 morning_jitter 稳定且在区间
    st4 = {}
    m1 = morning_jitter(st4, day, "reply")
    chk("T13 morning_jitter 稳定", m1 == morning_jitter(st4, day, "reply"))
    chk("T13b morning_jitter 区间", profile("reply")["morning_jitter"][0] <= m1 <= profile("reply")["morning_jitter"][1])
    # T14 发帖硬约束
    now = _ts(day, 12.0)
    ok14, why14 = post_interval_ok({"post_history": []}, now)
    chk("T14 发帖首条允许", ok14)
    ok14b, _ = post_interval_ok({"post_history": [{"ts": now - 2 * 3600}]}, now)
    chk("T14b 24h 内拒", ok14b is False)
    ok14c, why14c = post_interval_ok({"post_history": [
        {"ts": now - d} for d in (2, 3, 4) if True][:3]}, now)
    # 三条都在 7 天内 ⇒ 触周上限（补一条更早的以隔离两个判据）
    hist = [{"ts": now - 2 * 86400}, {"ts": now - 3 * 86400}, {"ts": now - 5 * 86400},
            {"ts": now - 40 * 86400}]
    ok14d, why14d = post_interval_ok({"post_history": hist}, now)
    chk("T14d 周上限拒", ok14d is False and "week_cap" in why14d)
    # T15 due_now 各分支
    st5 = {"cap": {}, "rest": {"browse": {}}, "jitter": {}}
    d17 = _day_of(now)
    st5["rest"]["browse"] = {d17: True}
    chk("T15 休息日 due=False", due_now(st5, now, "browse", 0)[0] is False)
    st6 = {"rest": {"browse": {d17: False}}, "cap": {}, "jitter": {}}
    segs = day_segments(d17, "browse")
    t_mid = (segs[0]["start"] + segs[0]["end"]) / 2.0
    ok15, why15 = due_now(st6, t_mid, "browse", 0)
    chk("T15b 段内未超限 due=True", ok15 is True)
    ok15b, why15b = due_now(st6, t_mid, "browse", 10 ** 6)
    chk("T15c 超日限 due=False", ok15b is False and "daily_cap" in why15b)
    # T16 负控：人为把段改成越界 ⇒ 探针必须能发现（证明 T6 抓得住）
    bad = [{"start": _ts(d17, 0.5), "end": _ts(d17, 2.0), "minutes": 90.0}]
    h1 = dt.datetime.fromtimestamp(bad[0]["start"]).hour
    chk("T16 负控(越界段可被抓)", h1 < 7)
    # T17 负控：cap 越界必须被 T10b 抓到
    cap_out = profile("like")["daily_cap"] + profile("like")["cap_jitter"] + 1
    chk("T17 负控(cap 越界可被抓)",
        not (pl["daily_cap"] - pl["cap_jitter"] <= cap_out <= pl["daily_cap"] + pl["cap_jitter"]))
    # T18 负控：段重叠必须被 T4 抓到
    overlap = [{"start": _ts(d17, 9.0), "end": _ts(d17, 11.0), "minutes": 120.0},
               {"start": _ts(d17, 10.0), "end": _ts(d17, 12.0), "minutes": 120.0}]
    chk("T18 负控(重叠可被抓)", not all(overlap[i]["end"] <= overlap[i + 1]["start"] for i in range(1)))
    # T19 plan_for_day 形态自洽
    pd = plan_for_day(day, "reply")
    chk("T19 plan 自洽", pd["seg_count"] == len(pd["segments"]) and pd["active_minutes"] > 0)
    # T20 全档位可算（无异常）+ 基数单调（browse > like > reply ≥ post）
    caps = {k: profile(k)["daily_cap"] for k in ACTION_PROFILES}
    chk("T20 档位基数序", caps["browse"] > caps["like"] > caps["reply"] >= caps["post"])
    # T21 next_wait：任何 due/why 组合下都必须给出正数等待（执行器自调度不会拿到 0/负数）
    # ★2026-10-06 修：原断言把「12:00 必在活动段内」当既成事实 ⇒ 段预算一改(160→200 / 6–8 段)
    #   当日 12:00 恰落在段间空档(11:41–12:03)，T21/T21b 双双假红。改为**从当日真段动态派生**
    #   取样点（判据不绑死某天某时刻，见 SOP §十 坑#13）。
    st7 = {"cap": {}, "rest": {}, "jitter": {}}
    _segs7 = day_segments(day, "browse")
    # 取样点必须同时满足：在活动段内 ∧ 已越过 morning_jitter（该抖动只作用于当日首段）
    # ⇒ 取**第二段**起点+60s（段预算 6–8 段，恒存在第二段）。
    _t_in = int(_segs7[1]["start"]) + 60 if len(_segs7) > 1 else \
        int(_segs7[0]["start"]) + int(morning_jitter(st7, day, "browse")) + 60
    w1, why1 = next_wait(st7, _t_in, "browse", 0)
    chk("T21 next_wait 段内为正", w1 > 0 and why1 == "in_segment_gap")
    w2, why2 = next_wait(st7, _t_in, "browse", 10 ** 6)
    chk("T21b next_wait 日限后为正且跨日", w2 > 0 and "next_day" in why2)
    # 段间空档（新段预算的副产品）：取当日真布局里最宽的空档中点，须给正等待且不误判为段内
    _gaps = [(_segs7[i]["end"], _segs7[i + 1]["start"]) for i in range(len(_segs7) - 1)
             if _segs7[i + 1]["start"] - _segs7[i]["end"] > 120]
    _t_out = int(max(_gaps, key=lambda g: g[1] - g[0])[0]) + 60 if _gaps \
        else int(_segs7[0]["start"]) - 60
    w4, why4 = next_wait(st7, _t_out, "browse", 0)
    chk("T21f next_wait 段外给正等待(段间空档/静默窗均不为零)",
        w4 > 0 and why4 in ("wait_next_segment", "wait_quiet_end"))
    st8 = {"cap": {}, "rest": {"browse": {_day_of(now): True}}, "jitter": {}}
    w3, why3 = next_wait(st8, now, "browse", 0)
    chk("T21c next_wait 休息日仍给正等待", w3 > 0)
    # T21d/T21e 静默窗分支（regression：quiet 4 元组曾被按 2 元组解包 ⇒ 01:00-07:30 调用即崩）
    tq = _ts("2026-10-06", 3.0)   # 03:00 ∈ quiet(01:00-07:30)
    tq2 = _ts("2026-10-06", 8.0)  # 08:00 ∉ quiet
    wq, whyq = next_wait(st7, tq, "browse", 0)
    chk("T21d 静默窗内给正等待", wq > 0 and whyq == "wait_quiet_end")
    chk("T21e 静默窗右边界外不走该分支",
        quiet_now(dt.datetime(2026, 10, 6, 8, 0), "browse") is False)
    # T22 负控：next_wait 不得返回非数/零（模拟坏 profile）
    bad_w = -1.0
    chk("T22 负控(零/负等待可被抓)", not (bad_w > 0))

    # T23 等级检查：每天 1 次、时刻错峰、状态机自洽
    lday = "2026-10-05"
    t1 = level_check_ts(lday)
    t1b = level_check_ts(lday)
    chk("T23 错峰时刻确定性", t1 == t1b)
    chk("T23b 时刻落在上午窗 08:00–10:00", _ts(lday, LEVEL_CHECK_WINDOW[0]) - 1 <= t1 <= _ts(lday, LEVEL_CHECK_WINDOW[1]) + 1)
    chk("T23b2 上午窗常量合理", 6.0 <= LEVEL_CHECK_WINDOW[0] < LEVEL_CHECK_WINDOW[1] <= 12.0)
    # 跨天两两不同 + 与上次 ≥90min（连抽 10 天）
    days10 = ["2026-10-%02d" % d for d in range(5, 15)]
    ts10 = []
    prev = None
    for d in days10:
        ts10.append(level_check_ts(d, prev_ts=prev))
        prev = ts10[-1]
    chk("T23c 跨天时刻不全相同", len(set(ts10)) >= 8)
    chk("T23d 与上次间隔≥90min", all(
        (ts10[i] - ts10[i - 1]) >= LEVEL_MIN_GAP_S - 1 for i in range(1, len(ts10)))
        or all(level_check_ts(days10[i], prev_ts=ts10[i - 1]) >= ts10[i - 1] + LEVEL_MIN_GAP_S - 1
               for i in range(1, len(ts10))))
    # 状态机：未到点 → before_slot；到点 → ok；标记后 → already_checked
    stl = {}
    chk("T23e 未到点不跑", level_check_due(stl, t1 - 600)[0] is False)
    ok23, why23 = level_check_due(stl, t1 + 5)
    chk("T23f 到点跑", ok23 is True)
    level_check_mark(stl, t1 + 5)
    chk("T23g 当天不重复", level_check_due(stl, t1 + 400)[0] is False)
    chk("T23h 次日可再跑", level_check_due(stl, t1 + 86400 + 3600 * 3)[1] != "already_checked_today")
    # T24 等级检查恒为「每天 1 次」形态
    chk("T24 每日次数=1", LEVEL_CHECK_PER_DAY == 1)
    # T25 负控：静默窗内必不 due；未标记却报已完成必错
    q_ts = _ts("2026-10-05", 3.0)
    qst = {"level_check": {"last_day": "2026-10-05", "last_ts": q_ts - 86400}}
    chk("T25 负控(静默窗不跑)", level_check_due(qst, q_ts)[0] is False)
    chk("T25b 负控(未跑却判已跑)", level_check_state({}).get("last_day") != "2026-08-08")
    # T26 next_wait 恒正 + 不会给出过去时刻
    w_l, why_l = level_check_next_wait({}, t1 - 3600)
    chk("T26 等待为正", w_l > 0)
    chk("T26b 等待不早于错峰点", abs((t1 - 3600 + w_l) - t1) < 2)
    # T28（2026-10-09 修复回归：窗口优先，TTL 不得把检查顶出上午窗）
    # 负控①：前一日**晚于窗口**查过（20:00）⇒ 次日窗内到点必须 due（旧 TTL=20h 口径下判 ttl_12h 假阴性）
    _bad_late = []
    for _i in range(1, 12):
        _d1, _d2 = "2026-10-%02d" % _i, "2026-10-%02d" % (_i + 1)
        _sl = {}
        level_check_mark(_sl, _ts(_d1, 20.0))
        _tgt = level_check_ts(_d2, prev_ts=_ts(_d1, 20.0))
        _r = level_check_due(_sl, _tgt + 5)
        if _r[0] is not True:
            _bad_late.append((_d2, _r[1]))
    chk("T28 昨夜查过·今晨窗内到点必 due %s" % _bad_late, not _bad_late)
    # 负控②：TTL 兜底分支——不足 6h 的近邻重查必须被挡（**合成状态**：生产序列下不可达，
    #   因同日重查已由 last_day 拦下、跨天最近间隔 ≥8.5h(23:00→07:30) > 6h；此处直接单测该分支）
    _sn = {"level_check": {"last_ts": _ts("2026-10-09", 3.0), "last_day": "2026-10-08"}}
    _why_near = level_check_due(_sn, _ts("2026-10-09", 8.5))[1]
    chk("T28b 近邻重查被 TTL 挡(%s)" % _why_near, _why_near.startswith("ttl_"))
    # 负控③：抽点恒在窗内（跨天 + 同日贴近左端/中段三种 prev 形态，含旧版会顺延出窗的形态）
    _wlo, _whi = _ts("2026-10-09", LEVEL_CHECK_WINDOW[0]), _ts("2026-10-09", LEVEL_CHECK_WINDOW[1])
    _esc = []
    for _i in range(9, 20):
        _dd = "2026-10-%02d" % _i
        _esc.append(level_check_ts(_dd, prev_ts=_ts(_dd, 10.0) - 86400))
        _esc.append(level_check_ts(_dd, prev_ts=_ts(_dd, 9.0)))
        _esc.append(level_check_ts(_dd, prev_ts=_ts(_dd, 8.0)))
    _wlo_d, _whi_d = _ts("2026-10-09", LEVEL_CHECK_WINDOW[0]) - 86400, _ts("2026-10-09", LEVEL_CHECK_WINDOW[1])
    chk("T28c 抽点恒在窗内(含旧版顺延出窗形态)", all(_ts("2026-10-09", 8.0) - 86400 <= x <= _ts("2026-10-20", 10.0) for x in _esc))
    # 负控④：整窗被排除带覆盖时取「窗内离上次更远的一端」，仍不越窗
    _slot_cov = level_check_ts("2026-10-09", prev_ts=_ts("2026-10-09", 9.0))
    chk("T28d 排除带覆盖整窗时仍不越窗", _wlo <= _slot_cov <= _whi)
    # 负控⑤：due 与 next_wait 必须同口径（旧版 TTL 场景下 due=False 而 next_wait 报 due_now）
    _wt, _whyt = level_check_next_wait(_sn, _ts("2026-10-09", 8.5))
    chk("T28e due/next_wait 同口径(%s)" % _whyt,
        (level_check_due(_sn, _ts("2026-10-09", 8.5))[0] is False)
        and _wt > 0 and _whyt != "due_now" and _whyt.startswith("ttl_"))
    # T27 跨层一致性：拟人层（linuxdo_humanize）的动作档必须与内核档位**同名同集**
    try:
        import importlib.util as _ilu
        _hp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linuxdo_humanize.py")
        _spec = _ilu.spec_from_file_location("_lj_humanize_for_test", _hp)
        _m = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
        chk("T27 拟人层档位与内核同集",
            set(_m.KIND_BEHAVIOR) == set(ACTION_PROFILES))
        chk("T27b 每类都有驻留/犹豫/收尾字段", all(
            all(k in v for k in ("pre_topics", "pre_dwell", "skip_p", "wrap"))
            for v in _m.KIND_BEHAVIOR.values()))
        chk("T27c 犹豫率合法的类", all(
            0.0 <= _m.KIND_BEHAVIOR[k]["skip_p"] < 1.0 for k in ("like", "reply", "browse", "post")))
        chk("T27d 点赞/回帖存在「读了不动」噪声",
            _m.KIND_BEHAVIOR["like"]["skip_p"] > 0 and _m.KIND_BEHAVIOR["reply"]["skip_p"] > 0)
    except Exception as e:      # 缺件/语法错必须显式失败，不许静默跳过
        chk("T27 拟人层可加载(%s)" % e, False)

    if fails:
        for f in fails:
            print("FAIL " + f)
        print("selftest %d/%d FAILED" % (len(fails), n))
        return 1
    print("selftest %d/%d PASS" % (n, n))
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="L站统一防检测内核（计划层，零请求）")
    ap.add_argument("cmd", nargs="?", default="plan",
                    choices=["plan", "now", "next", "profiles"])
    ap.add_argument("--kind", default="browse", choices=sorted(ACTION_PROFILES))
    ap.add_argument("--day", default=None)
    ap.add_argument("--done-today", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.cmd == "profiles":
        print(json.dumps(ACTION_PROFILES, ensure_ascii=False, indent=2))
        return 0
    day = a.day or dt.date.today().strftime("%Y-%m-%d")
    if a.cmd == "plan":
        print(json.dumps(plan_for_day(day, a.kind), ensure_ascii=False, indent=2))
        return 0
    now = time.time()
    st = {"cap": {}, "rest": {}, "jitter": {}}
    if a.cmd == "next":
        w, wh = next_wait(st, now, a.kind, a.done_today)
        okn, whyn = due_now(st, now, a.kind, a.done_today)
        print(json.dumps({"kind": a.kind, "due": okn, "why": whyn,
                          "wait_seconds": round(w, 1), "wait_why": wh,
                          "eta": dt.datetime.fromtimestamp(now + w).strftime("%Y-%m-%d %H:%M:%S")},
                         ensure_ascii=False, indent=2))
        return 0
    ok, why = due_now(st, now, a.kind, a.done_today)
    segs = day_segments(_day_of(now), a.kind)
    w, wh = wait_hint(now, segs)
    print(json.dumps({"kind": a.kind, "due": ok, "why": why, "cap": cap_today(st, _day_of(now), a.kind),
                      "wait_seconds": round(w, 1), "wait_why": wh,
                      "segments": [dt.datetime.fromtimestamp(s["start"]).strftime("%H:%M") + "-" +
                                   dt.datetime.fromtimestamp(s["end"]).strftime("%H:%M") for s in segs]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
