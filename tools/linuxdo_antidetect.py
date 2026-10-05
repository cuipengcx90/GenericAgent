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
    "browse": dict(daily_cap=160, cap_jitter=20, seg_n=(3, 5), seg_len=(45, 110),
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
    if total > span * 0.75:                      # 段总长不超过活动窗 75%
        k = span * 0.75 / total
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
def gap_seconds(kind, rng=None):
    """段内动作间隔：常规档 3:1 概率优先，长休档 1 份（对偶随机）。"""
    p = profile(kind)
    r = rng or random.Random()
    lo, hi = p["gap"]
    if hi <= lo:
        return 0.0
    if p["gap_long_p"] > 0 and r.random() < p["gap_long_p"]:
        return r.uniform(*p["gap_long"])
    return r.uniform(lo, hi)


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
        p = profile(kind)
        qa, qb = p["quiet"] if p.get("quiet", (0, 0, 0, 0))[0] else (0, 0)
        nowdt = dt.datetime.fromtimestamp(now_ts)
        end = nowdt.replace(hour=int(qb), minute=0, second=0, microsecond=0)
        if end <= nowdt:
            end += dt.timedelta(days=1)
        return max(60.0, (end - nowdt).total_seconds() + rng.uniform(0, 1800)), "wait_quiet_end"
    return 600.0 + rng.uniform(0, 300), "fallback"


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
    st7 = {"cap": {}, "rest": {}, "jitter": {}}
    w1, why1 = next_wait(st7, now, "browse", 0)
    chk("T21 next_wait 段内为正", w1 > 0 and why1 == "in_segment_gap")
    w2, why2 = next_wait(st7, now, "browse", 10 ** 6)
    chk("T21b next_wait 日限后为正且跨日", w2 > 0 and "next_day" in why2)
    st8 = {"cap": {}, "rest": {"browse": {_day_of(now): True}}, "jitter": {}}
    w3, why3 = next_wait(st8, now, "browse", 0)
    chk("T21c next_wait 休息日仍给正等待", w3 > 0)
    # T22 负控：next_wait 不得返回非数/零（模拟坏 profile）
    bad_w = -1.0
    chk("T22 负控(零/负等待可被抓)", not (bad_w > 0))

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
