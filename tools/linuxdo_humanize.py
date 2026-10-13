#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L站拟人层（humanization layer）——「动手前后真人会干的事」。

分层职责（单一源，勿混）：
  内核 linuxdo_antidetect.py = 何时动 / 一天动几次 / 间隔多大（时间与配额档案）
  本层 linuxdo_humanize.py  = 动手前后干什么（阅读驻留 / 滚动 / 犹豫 / 打字 / 收尾）

四类动作的特异面（用户要求「每一个另类的」）：
  browse : 慢滚 + 逐屏停留 + 偶尔回滚看上文（量大、无产出动作）
  like   : 先读后赞（读够了才决定）+ 犹豫（读了不赞）+ 赞后回看一屏
  reply  : 读**最新楼**（非主楼）+ 打字含停顿改稿 + 发送前回读自己的草稿
  post   : 前置闲逛多帖 + 长写作停顿（分段成文，标题最后写）+ 发完回收看

业界做法（2026-10-05 调研，落地为参数）：
  1. 长尾分布：动作间隔/阅读时长用对数正态，禁 uniform（uniform 的方差形态本身可判）
  2. 阅读模型：驻留时间随文本长度增长而非固定 sleep（固定 sleep 是最大破绽）
  3. 打字动力学：逐字 keystroke + 标点后停顿 + 思考停顿 + 小概率打错回删
  4. 决策噪声：真人不是每个帖子都点赞/回复，「读了不动」是常态
  5. 会话形态：先热身（首页/列表）再动手，动作后收尾停留，禁「打开→动作→关闭」直筒
纯 stdlib，零网络。所有函数可传入 rng（测试用固定种子）。
"""
import datetime as dt
import json
import math
import os
import random
import sys
import time

VERSION = "v2.0.0"

# ---------------------------------------------------------------- 参数（单一源）
# 每类动作的真人行为档案
KIND_BEHAVIOR = {
    "browse": dict(pre_topics=(0, 1), pre_dwell=(0.0, 0.0), skip_p=0.0,
                   wrap=(2.0, 10.0), label="浏览"),
    "like":   dict(pre_topics=(1, 2), pre_dwell=(4.0, 20.0), skip_p=0.22,
                   wrap=(3.0, 12.0), label="点赞"),
    "reply":  dict(pre_topics=(2, 4), pre_dwell=(8.0, 35.0), skip_p=0.35,
                   wrap=(5.0, 25.0), label="回帖"),
    "post":   dict(pre_topics=(3, 6), pre_dwell=(15.0, 60.0), skip_p=0.0,
                   wrap=(10.0, 40.0), label="发帖"),
}

# 阅读速度（字/秒 中位数）。真人先扫读、部分段落精读 ⇒ 用混合档
READ_SPEED = dict(fast=11.0, normal=7.0, careful=3.6)
PRECISE_P = 0.50          # 进入精读档的概率(★R581 0.45→0.50:更常精读,只增不减)
DWELL_MIN, DWELL_MAX = 2.5, 300.0
FIXATION = (0.8, 2.5)     # 开屏定位（找到要读的位置）耗时

# 打字：CJK 逐字中位延迟（秒）与对数正态 sigma
TYPE_MEDIAN = dict(reply=0.16, post=0.14, browse=0.16)
TYPE_SIGMA = 0.55
TYPE_MIN, TYPE_MAX = 0.05, 1.6
PUNCT_PAUSE = {"，": (0.25, 0.9), "。": (0.4, 1.4), "！": (0.4, 1.4), "？": (0.5, 1.6),
               ",": (0.25, 0.9), ".": (0.4, 1.4), "!": (0.4, 1.4), "?": (0.5, 1.6),
               "\n": (0.5, 2.0), "：": (0.3, 1.0), ":": (0.3, 1.0)}
THINK_EVERY = (18, 55)    # 每 N 字一次「思考停顿」
THINK_PAUSE = (1.2, 7.0)
TYPO_P = 0.012            # 每字打错概率
TYPO_FIX = (0.6, 2.6)     # 发现并回删的耗时

# 长文写作：分段与段间停顿
WRITE_SEGMENTS = (2, 4)
WRITE_PAUSE = (18.0, 120.0)

# 动作后收尾：回看自己刚发的/赞过的
WRAP_SCROLLS = (0, 2)


def _rng(rng=None):
    if rng is None:
        return random.Random()
    if isinstance(rng, random.Random):
        return rng
    return random.Random(str(rng))


def _lognormal(rng, median, sigma=0.75, lo=None, hi=None):
    """对数正态：median 为中位数，长尾在右侧（真人行为的标准形态）。"""
    m = rng.gauss(math.log(max(median, 1e-6)), sigma)
    v = math.exp(m)
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def _trunc_norm(rng, lo, hi, sigma_frac=0.28):
    mu, sd = (lo + hi) / 2.0, (hi - lo) * sigma_frac
    return min(max(rng.gauss(mu, sd), lo), hi)


def profile(kind):
    if kind not in KIND_BEHAVIOR:
        raise KeyError("unknown kind: %s" % kind)
    return KIND_BEHAVIOR[kind]


# ---------------------------------------------------------------- 阅读
def read_dwell_seconds(n_chars, rng=None, kind="browse", speed=None):
    """按文本字数给出驻留秒数（真人读得快慢混合 + 长尾）。"""
    r = _rng(rng)
    n = max(int(n_chars or 0), 0)
    if speed is None:
        speed = READ_SPEED["careful"] if r.random() < PRECISE_P else READ_SPEED["normal"]
    sec = n / max(speed, 0.5)
    sec *= _lognormal(r, 1.0, 0.30, 0.6, 1.9)      # 个体内波动（长尾）
    sec += r.uniform(*FIXATION)
    if n == 0:                                     # 空文本：仍要「看一眼」
        sec = r.uniform(1.5, 4.0)
    return round(min(max(sec, DWELL_MIN), DWELL_MAX), 1)


def scroll_plan(n_chars, dwell_s, rng=None, kind="browse"):
    """把驻留时间摊成滚轮动作：[{dy, pause}]（dy>0 向下，<0 回滚）。

    真人不是匀速到底：屏高分段、偶尔回滚看上文、末尾停留更久。
    """
    r = _rng(rng)
    step_lo, step_hi = {"browse": (180, 520), "like": (150, 420),
                        "reply": (200, 620), "post": (200, 620)}[kind]
    screens = max(1, int(round(n_chars / 420.0)))          # 约 420 字/屏
    screens = min(screens, 32)                          # ★R581 24→32:大帖读得更深(真人继续往下翻)
    total = max(float(dwell_s), 1.5)
    plan, used = [], 0.0
    for i in range(screens):
        dy = r.uniform(step_lo, step_hi)
        if r.random() < 0.12 and i > 0:                    # 回滚看上文
            dy = -r.uniform(60, 220)
        pause = total / screens * _lognormal(r, 1.0, 0.45, 0.35, 2.6)
        plan.append({"dy": round(dy, 1), "pause": round(pause, 2)})
        used += pause
    if used > 0 and abs(used - total) / total > 0.02:      # 归一化到目标时长
        k = total / used
        for p in plan:
            p["pause"] = round(p["pause"] * k, 2)
    if plan and r.random() < 0.75:                         # 底部多停一会(★R581 0.7→0.75)
        plan[-1]["pause"] = round(plan[-1]["pause"] * r.uniform(1.2, 2.4), 2)
    return plan


# ---------------------------------------------------------------- 打字
def typing_plan(text, rng=None, kind="reply", chars_per_chunk=1):
    """逐字打字计划：[{chunk, delay, typo}]；typo=被回删的错字（不影响最终文本）。"""
    r = _rng(rng)
    text = text or ""
    med = TYPE_MEDIAN.get(kind, 0.16)
    out, buf = [], ""
    think_at = r.randint(*THINK_EVERY)
    typed = 0
    for ch in text:
        d = _lognormal(r, med, TYPE_SIGMA, TYPE_MIN, TYPE_MAX)
        typo = False
        if ch not in PUNCT_PAUSE and r.random() < TYPO_P:
            typo = True
            d += r.uniform(*TYPO_FIX)
        if ch in PUNCT_PAUSE:
            lo, hi = PUNCT_PAUSE[ch]
            d += r.uniform(lo, hi)
        buf += ch
        typed += 1
        if typed >= think_at:                              # 思考停顿（攒一句话）
            out.append({"chunk": buf, "delay": round(d, 3), "typo": typo})
            buf = ""
            out.append({"chunk": "", "delay": round(r.uniform(*THINK_PAUSE), 2), "typo": False})
            think_at = typed + r.randint(*THINK_EVERY)
            continue
        if len(buf) >= chars_per_chunk:
            out.append({"chunk": buf, "delay": round(d, 3), "typo": typo})
            buf = ""
    if buf:
        out.append({"chunk": buf, "delay": round(r.uniform(0.2, 0.8), 3), "typo": False})
    return out


def typing_total(plan):
    return round(sum(float(p.get("delay") or 0.0) for p in plan), 2)


def write_plan(text, rng=None):
    """长文写作：按段落切分并插入段间停顿（发帖/长回复用）。"""
    r = _rng(rng)
    text = text or ""
    segs = [s for s in text.split("\n") if s.strip()] or [text]
    if len(segs) == 1 and len(text) > 220:                 # 单段长文：按句切开
        parts, cur = [], ""
        for ch in text:
            cur += ch
            if ch in "。！？" and len(cur) >= 60:
                parts.append(cur)
                cur = ""
        if cur:
            parts.append(cur)
        segs = parts
    lo, hi = WRITE_SEGMENTS
    if len(segs) > hi:
        k = int(math.ceil(len(segs) / float(hi)))
        segs = ["".join(segs[i:i + k]) for i in range(0, len(segs), k)]
    out = []
    for i, s in enumerate(segs):
        out.append({"chunk": s, "pause": 0.0 if i == 0 else round(r.uniform(*WRITE_PAUSE), 1)})
    return out


# ---------------------------------------------------------------- 决策 / 会话形态
def decision_skip(kind, rng=None):
    """读了却不动手（点了赞就假、每个帖子都回更假）。"""
    r = _rng(rng)
    return r.random() < float(profile(kind)["skip_p"])


def preface_topics(kind, rng=None):
    """动手之前先随便刷几个帖子（真人不会直奔目标）。"""
    r = _rng(rng)
    lo, hi = profile(kind)["pre_topics"]
    if hi <= lo:
        return lo
    return r.randint(lo, hi)


def preface_dwell(kind, rng=None):
    r = _rng(rng)
    lo, hi = profile(kind)["pre_dwell"]
    if hi <= lo:
        return lo
    return round(r.uniform(lo, hi), 1)


def wrap_up_seconds(kind, rng=None):
    """动作完成后继续停留（回看自己刚发的/赞过的/再滚一屏）。"""
    r = _rng(rng)
    lo, hi = profile(kind)["wrap"]
    return round(_lognormal(r, (lo + hi) / 2.0, 0.45, lo, hi * 1.4), 1)


def scrolls_after(kind, rng=None):
    r = _rng(rng)
    lo, hi = WRAP_SCROLLS
    return r.randint(lo, hi) if kind != "browse" else r.randint(1, 3)


def action_gap(kind, rng=None, base=None, cap=None):
    """两次同类动作之间的人类化间隔:内核 base × 抖动,另有起身长休。

    cap:单次间隔上限(秒),默认 None=不设限。R574:段内帖间间隔必须传 cap —
    旧实现无上限,当 base 取内核长休档(300–600s)时 6% 起身分支可单次睡到
    base×12=2h,与段预算 6–8 段 × 85–160min 的段级节律直接冲突(段内出现 2h
    静默,守护按 age 判停滞→误重启)。段级长休归 SESSION_REST,不在帖间叠加。
    """
    r = _rng(rng)
    if base is None:
        base = 300.0
    if r.random() < 0.06:                                  # 起身/走开
        v = round(_lognormal(r, base * 3.5, 0.5, base * 1.5, base * 12.0), 1)
    else:
        v = round(base * _lognormal(r, 1.0, 0.22, 0.82, 1.6), 1)
    if cap is not None and v > cap:
        v = float(cap)
    return v


def pose(kind, n_chars=0, rng=None):
    """一次动作的「姿势」摘要（落台账用，便于事后回看人类化是否生效）。"""
    r = _rng(rng)
    dwell = read_dwell_seconds(n_chars, r, kind=kind)
    return {
        "kind": kind, "version": VERSION,
        "pre_topics": preface_topics(kind, r),
        "pre_dwell": preface_dwell(kind, r),
        "read_dwell": dwell,
        "scroll_screens": len(scroll_plan(n_chars, dwell, r, kind=kind)),
        "skip": decision_skip(kind, r),
        "wrap_up": wrap_up_seconds(kind, r),
        "ts": round(time.time(), 1),
    }


def session_warmup(kind, rng=None):
    """会话开头热身：先看首页/列表，再去目标（禁直筒）。"""
    r = _rng(rng)
    if kind == "post":
        return {"home": round(r.uniform(3.0, 12.0), 1), "list": round(r.uniform(5.0, 20.0), 1)}
    return {"home": round(r.uniform(1.5, 6.0), 1), "list": round(r.uniform(2.0, 10.0), 1)}


# ---------------------------------------------------------------- 自测
def _selftest_impl():
    ok = [0, 0]
    fails = []

    def chk(name, cond, extra=""):
        ok[1] += 1
        if cond:
            ok[0] += 1
        else:
            fails.append("%s %s" % (name, extra))

    # T1 驻留时长随字数单调
    a = read_dwell_seconds(100, 7, kind="browse")
    b = read_dwell_seconds(900, 7, kind="browse")
    chk("T1 字数↑驻留↑", b > a, "%s/%s" % (a, b))
    # T2 上下界
    chk("T2a 空文本仍有驻留", read_dwell_seconds(0, 7) >= DWELL_MIN)
    chk("T2b 超长文本被截到 <=300", read_dwell_seconds(100000, 7) <= DWELL_MAX)
    # T3 精读档更慢（同字数，比较中位数）
    fast = sorted(read_dwell_seconds(600, random.Random(1000 + i), speed=READ_SPEED["fast"]) for i in range(60))
    slow = sorted(read_dwell_seconds(600, random.Random(1000 + i), speed=READ_SPEED["careful"]) for i in range(60))
    chk("T3 精读>扫读", slow[30] > fast[30] * 1.5, "%s/%s" % (fast[30], slow[30]))
    # T4 打字总时长=各段之和
    tp = typing_plan("这是一段测试文字，用来验证打字节奏。", 11)
    chk("T4 打字总时长=Σ段", abs(typing_total(tp) - sum(p["delay"] for p in tp)) < 0.05)
    # T5 单字延迟量级
    per = [p["delay"] for p in typing_plan("测试文字" * 30, 12) if p["chunk"]]
    per.sort()
    chk("T5 单字中位延迟∈[0.05,0.6]", 0.05 <= per[len(per) // 2] <= 0.6, str(per[len(per) // 2]))
    # T6 标点后停顿更久（取 120 次均值；[0] 是首字，标点延迟在末段）
    a1 = [typing_plan("好。", 500 + i)[-1]["delay"] for i in range(120)]
    a2 = [typing_plan("好x", 500 + i)[-1]["delay"] for i in range(120)]
    chk("T6 句号后更慢", sum(a1) / 120 > sum(a2) / 120 * 1.15,
        "%s/%s" % (round(sum(a1) / 120, 3), round(sum(a2) / 120, 3)))
    # T7 打错回删存在且是少数（率应在 0.3%~4%）
    typos = nchar = 0
    for i in range(200):
        pl = typing_plan("测试" * 60, i, kind="reply")
        typos += sum(1 for p in pl if p.get("typo"))
        nchar += sum(len(p["chunk"]) for p in pl)
    rate = typos / float(max(nchar, 1))
    chk("T7 打错少量存在", 1 <= typos and 0.003 <= rate <= 0.04, "%d/%.4f" % (typos, rate))
    # T8 长文分段与停顿
    wp = write_plan("第一段内容。\n第二段内容。\n第三段内容。", 21)
    chk("T8a 分段数≥2", len(wp) >= 2)
    chk("T8b 首段无停顿/后续有停顿", wp[0]["pause"] == 0.0 and all(p["pause"] >= 0 for p in wp))
    # T9 决策噪声频率贴近档案
    r = random.Random(99)
    sk = sum(1 for _ in range(4000) if decision_skip("like", r))
    chk("T9 点赞犹豫率≈0.22", 0.17 <= sk / 4000.0 <= 0.28, str(sk / 4000.0))
    # T10 前置浏览档位：post >= reply >= like >= browse
    med = {}
    for k in ("browse", "like", "reply", "post"):
        rr = random.Random(5)
        med[k] = sorted(preface_topics(k, rr) for _ in range(50))[25]
    chk("T10 前置浏览档位序", med["post"] >= med["reply"] >= med["like"] >= med["browse"], str(med))
    # T11 收尾停留档位
    chk("T11 收尾档位序", wrap_up_seconds("post", 3) >= wrap_up_seconds("like", 3))
    # T12 动作间隔抖动形态
    rr = random.Random(31)
    gaps = [action_gap("like", rr, base=300.0) for _ in range(800)]
    normal = [g for g in gaps if g <= 480]
    chk("T12a 间隔不恒定", len(set(round(g) for g in gaps)) > 100)
    chk("T12b 长休档存在", any(g > 600 for g in gaps))
    chk("T12c 常规档落在 300±20% 附近", 240 <= sorted(normal)[len(normal) // 2] <= 390,
        str(sorted(normal)[len(normal) // 2]))
    # T13 长尾（对数正态 p90/median 比值）
    rr = random.Random(77)
    v = sorted(_lognormal(rr, 1.0, 0.75) for _ in range(2000))
    chk("T13 长尾 p90 > 1.5×median", v[1800] > 1.5 * v[1000], "%s/%s" % (v[1000], v[1800]))
    # T14 滚动计划与驻留时长对齐
    dwell = read_dwell_seconds(1200, 41)
    sp = scroll_plan(1200, dwell, 41, kind="like")
    tot = sum(p["pause"] for p in sp)
    chk("T14a 滚动段数>0", len(sp) > 0)
    chk("T14b 滚动总时长≈驻留", abs(tot - dwell) / dwell <= 0.35, "%s/%s" % (round(tot, 1), dwell))
    chk("T14c 存在回滚可能(200 次内出现)", any(
        p["dy"] < 0 for i in range(200) for p in scroll_plan(2400, 120, random.Random(i), kind="browse")))
    # T15 确定性（同种子同结果）
    c1 = json.dumps(typing_plan("abcdefg", 55), ensure_ascii=False)
    c2 = json.dumps(typing_plan("abcdefg", 55), ensure_ascii=False)
    chk("T15a 同种子逐字一致", c1 == c2)
    c3 = json.dumps(typing_plan("abcdefg", 56), ensure_ascii=False)
    chk("T15b 异种子有差异", c1 != c3)
    # T16 负控：未知档位必须报错（不静默回落）
    try:
        profile("nope")
        chk("T16 未知 kind 必须 KeyError", False)
    except KeyError:
        chk("T16 未知 kind 必须 KeyError", True)
    # T17 pose 字段齐备
    p = pose("reply", 800, 61)
    need = {"kind", "version", "pre_topics", "pre_dwell", "read_dwell", "scroll_screens",
            "skip", "wrap_up", "ts"}
    chk("T17 pose 字段齐备", need.issubset(set(p.keys())), str(sorted(set(p.keys()))))
    # T18 热身时长非零
    w = session_warmup("reply", 71)
    chk("T18 热身非零", w["home"] > 0 and w["list"] > 0)

    for f in fails:
        print("  FAIL %s" % f)
    print("selftest: %d/%d passed" % (ok[0], ok[1]))
    return 0 if not fails else 1


def selftest():
    return _selftest_impl()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("selftest", "--selftest"):
        return selftest()
    if argv[0] == "pose":
        kind = argv[1] if len(argv) > 1 else "reply"
        n = int(argv[2]) if len(argv) > 2 else 600
        print(json.dumps(pose(kind, n), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "demo":
        r = random.Random(7)
        kind = argv[1] if len(argv) > 1 else "reply"
        text = "这个思路我觉得可行，不过我担心积分门槛还是太高。"
        tp = typing_plan(text, r, kind=kind)
        print(json.dumps({"kind": kind, "profile": profile(kind),
                          "warmup": session_warmup(kind, r),
                          "read_dwell": read_dwell_seconds(700, r, kind=kind),
                          "typing": tp, "typing_total": typing_total(tp),
                          "wrap_up": wrap_up_seconds(kind, r)}, ensure_ascii=False, indent=2))
        return 0
    print("usage: linuxdo_humanize.py [selftest|pose <kind> [chars]|demo <kind>]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
