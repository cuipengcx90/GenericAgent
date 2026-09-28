#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""roi_calibration —— ROI 口径 v3 重标定与「阈值可触发性」闸门（只读）。

背景（2026-09-29 R497 / TODO L199）：
  roi_monitor.py 的告警链路两次「结构性死亡」：
    ① v1: `avg_lines_per_task` 只被读不被写 ⇒ <100 行告警自 2026-06-26 起永不触发；
    ② v2 原型（temp/roi_monitor_v2.patch，未接线）：阈值取自**历史极值**(drop>25 / 4.0 次/日)
       而极值本身会漂移 ⇒ 实测 127 个评估日中 drop>25 仅 1 次、bpd<4 仅 2 次 = 近乎死告警。
  本器把阈值改为「历史分位数 + 可触发性闸门」：任何标定出来的阈值，必须在评估窗内
  **至少触发 1 次**且**不超过 FIRE_CAP 比例**，否则判 CALIBRATION_FAIL 且不落盘。

子命令（位置参数）：
  scan [--out P]            扫 sche_tasks/done/ 建逐日序列（执行数/去重种类数/行数）
  metrics [--series P]      打印逐日指标表（尾部 N 天）
  calibrate [--series P]    按分位标定阈值 + 跑可触发性闸门；合格则写 tools/roi_thresholds_v3.json
  backtest [--rules all|v1|v2|v3] [--series P]  回放规则，给触发点清单/首次末次触发/触发率
  proof                     只读自证（跑前后输入 sha256 一致）
  selftest                  自测（含负控）
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
GA_ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import roi_task_class as rtc  # noqa: E402

DONE_DIR = os.path.join(GA_ROOT, "sche_tasks", "done")
HISTORY = os.path.join(GA_ROOT, "roi_history.json")
THRESHOLDS_OUT = os.path.join(HERE, "roi_thresholds_v3.json")
OUT_DIR = os.path.join(GA_ROOT, "temp", "roi_calibration")

WARMUP_DAYS = 30          # 前 N 天只做基线，不参与评估
FIRE_CAP = 0.33           # 可触发性上限：触发率不得超过 1/3
MIN_FIRE_RATE = 0.01      # 可触发性下限：触发率至少 1%（且至少 1 天）
# ★关键设计（R497 实测教训）：固定分位（如一律 p05）在**离散/退化分布**上会自毁——
#   实测样例：40 天里 per-day 值只有 {1..5} 时 p05 恰好落在 min=1.0 ⇒ 阈值 1.0、触发 0 次
#   （= v1/v2 式死阈值换了张脸又回来）。故改为**分位网格搜索**：从最敏感的小分位起，
#   取第一个触发率落在 [MIN_FIRE_RATE, FIRE_CAP] 的分位作为阈值；全网格都不合格才判死。
QUANTILE_GRID = (0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
DONE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+)\.md$")

# 规则表：(id, 指标 key, 方向, 分位来源, 类别, 中文说明)
# 类别 calibrated=分位标定+可触发性闸门; fail_closed=未登记任务硬告警(不参与分位)
RULES_V3 = [
    ("R1", "bpd",       "lt", "lo", "calibrated", "业务型任务执行量稀疏(7日均)"),
    ("R2", "lines7",    "lt", "lo", "calibrated", "业务型单任务平均产出过低(7日均行数)"),
    ("R3", "biz_drop",  "gt", "hi", "calibrated", "业务层 7日均 vs 30日均 下滑幅度"),
    ("R4", "ratio",     "gt", "hi", "calibrated", "业务型灌水比(执行次数/去重种类数)"),
    ("R5", "unclass7",  "gt", "none", "fail_closed", "出现未登记任务名(fail-closed)"),
]
# 历史规则（用于回放对照，证明「阈值死亡」）
RULES_V2 = [
    ("V2a", "biz_drop", "gt", 25.0, "历史 v2 阈值(取自当时极值)"),
    ("V2b", "bpd",      "lt", 4.0,  "历史 v2 阈值(取自当时极值)"),
    ("V2c", "hbshare",  "gt", 85.0, "历史 v2 阈值(心跳占比)"),
]
V1_LINES_THRESHOLD = 100.0


# --------------------------------------------------------------------- scan ----
def scan(done_dir=None):
    d = done_dir or DONE_DIR
    reg = rtc.load_registry()
    day = {}
    for f in sorted(os.listdir(d)):
        m = DONE_RE.match(f)
        if not m:
            continue
        ds, _hm, name = m.groups()
        e = day.setdefault(ds, {"all": 0, "hb": 0, "biz": 0, "allK": [], "bizK": [],
                                "bizL": [], "unclass": []})
        e["all"] += 1
        if name not in e["allK"]:
            e["allK"].append(name)
        try:
            with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                lines = fh.read().count("\n") + 1
        except OSError:
            lines = 0
        layer = rtc.classify(name, reg)
        if layer == "heartbeat":
            e["hb"] += 1
        elif layer == "business":
            e["biz"] += 1
            if name not in e["bizK"]:
                e["bizK"].append(name)
            e["bizL"].append(lines)
        else:
            e["unclass"].append(name)
    days = []
    for ds in sorted(day):
        e = day[ds]
        days.append({"d": ds, "all": e["all"], "hb": e["hb"], "biz": e["biz"],
                     "allK": sorted(e["allK"]), "bizK": sorted(e["bizK"]),
                     "bizL": e["bizL"], "unclass": sorted(set(e["unclass"]))})
    hb = rtc.heartbeat_names(reg)
    return {"schema": "roi_series/v1", "generated_at": datetime.now().isoformat(timespec="seconds"),
            "done_dir": os.path.abspath(d), "registry_sha16": _sha(rtc.DEFAULT_REGISTRY)[:16],
            "heartbeat_names": hb, "n_days": len(days), "days": days}


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


# ------------------------------------------------------------------ metrics ----
def _win(idx, ds, n):
    e = date.fromisoformat(ds)
    return [(e - timedelta(days=i)).isoformat() for i in range(n)]


def metrics(series):
    """逐日指标表（含全量/业务型双口径 + 去重种类数 + 灌水比）。"""
    dm = {x["d"]: x for x in series["days"]}
    out = []
    for x in series["days"]:
        ds = x["d"]
        w7, w30 = _win(0, ds, 7), _win(0, ds, 30)
        w7p = _win(0, (date.fromisoformat(ds) - timedelta(days=7)).isoformat(), 7)
        g = lambda ks, key: sum(dm[k][key] for k in ks if k in dm)
        all7, allp = g(w7, "all") / 7.0, g(w7p, "all") / 7.0
        biz7, biz30 = g(w7, "biz") / 7.0, g(w30, "biz") / 30.0
        L7 = [l for k in w7 if k in dm for l in dm[k]["bizL"]]
        L30 = [l for k in w30 if k in dm for l in dm[k]["bizL"]]
        k7 = len(set().union(*[set(dm[k]["bizK"]) for k in w7 if k in dm])) / 7.0
        hb7 = g(w7, "hb")
        unc7 = len(set().union(*[set(dm[k]["unclass"]) for k in w7 if k in dm]))
        out.append({
            "d": ds,
            "all7": round(all7, 2), "wow_all": round((all7 - allp) / allp * 100, 1) if allp else None,
            "bpd": round(biz7, 2), "biz30": round(biz30, 2),
            "wow_biz": round((biz7 - biz30) / biz30 * 100, 1) if biz30 else None,
            "biz_drop": round((1 - biz7 / biz30) * 100, 1) if biz30 else None,
            "kpd": round(k7, 2),
            "ratio": round(biz7 / k7, 2) if k7 else None,
            "lines7": round(sum(L7) / len(L7), 1) if L7 else None,
            "lines30": round(sum(L30) / len(L30), 1) if L30 else None,
            "hbshare": round(hb7 / (hb7 + g(w7, "biz")) * 100, 1) if (hb7 + g(w7, "biz")) else None,
            "exec7": g(w7, "biz"), "kinds7": len(set().union(*[set(dm[k]["bizK"]) for k in w7 if k in dm])),
            "unclass7": unc7,
        })
    return out


def quantile(xs, q):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def _round_thr(v, op):
    return round(v, 2) if op == "gt" else round(v, 1)


def _fires(ev, key, op, thr):
    """在评估窗上算触发日（None 值不参与）。"""
    return [r["d"] for r in ev if r[key] is not None and
            ((r[key] < thr) if op == "lt" else (r[key] > thr))]


def pick_threshold(vals, ev, key, op):
    """分位网格搜索：返回 (thr, q, fires)；无可行阈值返回 (None, None, [])。

    自最敏感的小分位起，取首个「触发率 ∈ [MIN_FIRE_RATE, FIRE_CAP]」的分位。
    下尾取 q、上尾取 1-q（语义一致：均为「少数异常日」尾巴）。
    """
    need = max(1, int(MIN_FIRE_RATE * len(ev)))
    for q in QUANTILE_GRID:
        tv = quantile(vals, q if op == "lt" else 1.0 - q)
        if tv is None:
            return None, None, []
        thr = _round_thr(tv, op)
        fired = _fires(ev, key, op, thr)
        if need <= len(fired) and len(fired) / len(ev) <= FIRE_CAP:
            return thr, q, fired
    return None, None, []


def derive_rules(rows):
    """按**分位网格搜索**标定阈值 + 计算触发率 + 可触发性闸门。

    返回 (rules, gate_ok, problems)。fail-closed 类不参与分位（恒 fires>0 语义）。
    """
    ev = rows[WARMUP_DAYS:]
    rules, problems = [], []
    for rid, key, op, tail, kind, desc in RULES_V3:
        if kind == "fail_closed":
            fired = [r["d"] for r in ev if (r[key] or 0) > 0]
            rules.append({"id": rid, "metric": key, "op": op, "threshold": 0,
                          "kind": kind, "rule": "%s > 0" % key, "desc": desc,
                          "fires": len(fired), "fire_rate": round(len(fired) / len(ev), 4),
                          "fire_dates": fired, "gate": "n/a(fail-closed)"})
            continue
        vals = [r[key] for r in ev if r[key] is not None]
        if not vals:
            problems.append("NO_DATA %s 指标 %s 无数据" % (rid, key))
            continue
        thr, q, fired = pick_threshold(vals, ev, key, op)
        if thr is None:
            # 全网格皆不合格：区分「从不触发」与「过于嘈杂」以给可读诊断
            any_fire = _fires(ev, key, op, _round_thr(quantile(vals, 0.5), op))
            gate = "TOO_NOISY" if len(any_fire) / len(ev) > FIRE_CAP else "DEAD_THRESHOLD"
            problems.append("%s %s %s%s 无可行阈值（网格 %d 档全不合格）"
                            % (gate, rid, key, op, len(QUANTILE_GRID)))
            rules.append({"id": rid, "metric": key, "op": op, "threshold": None, "kind": kind,
                          "rule": "%s %s N/A" % (key, "<" if op == "lt" else ">"),
                          "desc": desc, "quantile": None, "fires": len(any_fire),
                          "fire_rate": round(len(any_fire) / len(ev), 4),
                          "fire_dates": any_fire, "gate": gate})
            continue
        rate = len(fired) / len(ev)
        rules.append({"id": rid, "metric": key, "op": op, "threshold": thr, "kind": kind,
                      "rule": "%s %s %.1f" % (key, "<" if op == "lt" else ">", thr),
                      "desc": desc, "quantile": q, "fires": len(fired),
                      "fire_rate": round(rate, 4), "fire_dates": fired, "gate": "PASS"})
    return rules, (not problems), problems


def calibrate(series, write=True):
    rows = metrics(series)
    ev = rows[WARMUP_DAYS:]
    rules, ok, problems = derive_rules(rows)
    doc = {"schema": "roi_thresholds_v3", "generated_at": datetime.now().isoformat(timespec="seconds"),
           "calibrated_on": {"done_dir": series["done_dir"], "n_days_total": len(rows),
                             "warmup_days": WARMUP_DAYS, "eval_days": len(ev),
                             "window": [ev[0]["d"], ev[-1]["d"]] if ev else None,
                             "registry_sha16": series.get("registry_sha16")},
           "constants": {"quantile_grid": list(QUANTILE_GRID), "min_fire_rate": MIN_FIRE_RATE,
                         "fire_cap": FIRE_CAP},
           "rules": rules, "gate_ok": ok, "problems": problems}
    for r in rules:
        print("%-4s %-26s thr=%-8s fires=%3d rate=%5.1f%% gate=%s  # %s"
              % (r["id"], r["rule"], r["threshold"], r["fires"], r["fire_rate"] * 100, r["gate"], r["desc"]))
    if ok and write:
        os.makedirs(os.path.dirname(THRESHOLDS_OUT), exist_ok=True)
        with open(THRESHOLDS_OUT, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        print("written %s" % os.path.relpath(THRESHOLDS_OUT, GA_ROOT))
    elif not ok:
        print("CALIBRATION_FAIL problems=%s（不落盘）" % problems)
    return doc


# -------------------------------------------------------------- gate (pure) ----
def triggerability_gate(rules, eval_days, fire_cap=FIRE_CAP):
    """纯函数版闸门（自测用）：返回 problems 列表。"""
    problems = []
    for r in rules:
        if r.get("kind") == "fail_closed":
            continue
        n, tot = r.get("fires", 0), max(1, eval_days)
        if n < 1:
            problems.append("DEAD_THRESHOLD %s 触发 0 次" % r.get("id"))
        elif n / tot > fire_cap:
            problems.append("TOO_NOISY %s 触发率 %.1f%%" % (r.get("id"), n / tot * 100))
    return problems


# ----------------------------------------------------------------- backtest ----
def backtest(series, ruleset="all", show_dates=8):
    rows = metrics(series)
    ev = rows[WARMUP_DAYS:]
    res = {}

    def replay(name, key, op, thr, note, universe=None):
        uni = universe if universe is not None else ev
        fired = [r["d"] for r in uni if r[key] is not None and
                 ((r[key] < thr) if op == "lt" else (r[key] > thr))]
        rate = len(fired) / len(uni) if uni else 0.0
        res[name] = {"rule": "%s %s %s" % (key, "<" if op == "lt" else ">", thr), "note": note,
                     "fires": len(fired), "universe": len(uni), "fire_rate": round(rate, 4),
                     "first": fired[0] if fired else None, "last": fired[-1] if fired else None,
                     "fire_dates": fired}
        print("%-6s %-28s fires=%3d/%-3d (%5.1f%%) first=%s last=%s  # %s"
              % (name, res[name]["rule"], len(fired), len(uni), rate * 100,
                 res[name]["first"], res[name]["last"], note))
        return fired

    if ruleset in ("all", "v1"):
        # v1：avg_lines_per_task < 100 —— 证据来自 roi_history.json 的字段存在性
        snaps = json.load(open(HISTORY, encoding="utf-8"))
        if isinstance(snaps, dict):
            snaps = [snaps]
        have = [s for s in snaps if "avg_lines_per_task" in s and s.get("avg_lines_per_task") is not None]
        fire = [s for s in have if s["avg_lines_per_task"] < V1_LINES_THRESHOLD]
        res["V1"] = {"rule": "avg_lines_per_task < 100", "note": "v1 结构性死亡(字段从不被写)",
                     "snapshots": len(snaps), "with_field": len(have), "fires": len(fire),
                     "fire_rate": round(len(fire) / len(snaps), 4),
                     "with_field_dates": [s.get("date") or s.get("timestamp") for s in have]}
        print("%-6s %-28s 快照=%d 含字段=%d 可触发=%d  # %s"
              % ("V1", res["V1"]["rule"], len(snaps), len(have), len(fire), res["V1"]["note"]))
    if ruleset in ("all", "v2"):
        for rid, key, op, thr, note in RULES_V2:
            replay(rid, key, op, thr, note)
    if ruleset in ("all", "v3"):
        try:
            doc = json.load(open(THRESHOLDS_OUT, encoding="utf-8"))
            rules = doc["rules"]
        except (OSError, ValueError, KeyError):
            rules, _ok, _p = derive_rules(rows)
        for r in rules:
            if r.get("kind") == "fail_closed":
                replay(r["id"], r["metric"], r["op"], r["threshold"], r["desc"] + "(fail-closed)")
            else:
                replay(r["id"], r["metric"], r["op"], r["threshold"], r["desc"])
    # 对照：全量口径的结构假象
    replay("FULL", "wow_all", "lt", -30.0, "全量口径 WoW<-30%（心跳归档后连续触发=假象）")
    replay("FULLbiz", "wow_biz", "lt", -25.0, "业务型口径 WoW<-25%（同期真实值）")
    for k, v in res.items():
        if v.get("fire_dates") and len(v["fire_dates"]) > show_dates:
            v["fire_dates_tail"] = v["fire_dates"][-show_dates:]
            v["fire_dates"] = v["fire_dates"][:show_dates]
    return res


# -------------------------------------------------------------------- proof ----
def proof():
    inputs = [rtc.DEFAULT_REGISTRY, os.path.abspath(__file__), HISTORY]
    inputs += [os.path.join(DONE_DIR, f) for f in sorted(os.listdir(DONE_DIR))[:200]]
    before = {p: _sha(p) for p in inputs if os.path.exists(p)}
    s = scan()
    metrics(s)
    backtest(s, "all", show_dates=3)
    after = {p: _sha(p) for p in inputs if os.path.exists(p)}
    changed = [p for p in before if before[p] != after.get(p)]
    print("proof files=%d changed=%s" % (len(before), changed))
    return 0 if not changed else 1


# ----------------------------------------------------------------- selftest ----
def _synth_done(tmp, spec):
    """spec: list of (date_str, task_name, n_files, lines)。返回 done 目录。"""
    d = os.path.join(tmp, "done")
    os.makedirs(d, exist_ok=True)
    for ds, nm, n, lines in spec:
        for i in range(n):
            with open(os.path.join(d, "%s_%04d_%s.md" % (ds, 1100 + i, nm)), "w") as fh:
                fh.write("x\n" * max(1, lines - 1))
    return d


def _synth_registry(tmp, extra_business=(), extra_hb=()):
    reg = {"schema": "roi_task_class/v1",
           "tasks": {"hb_alpha": {"layer": "heartbeat", "per_active_day": 5.0, "median_lines": 10,
                                  "note": "测试用无事件也照跑", "status": "active"}}}
    for n in ["biz_one", "biz_two", "biz_three", *extra_business]:
        reg["tasks"][n] = {"layer": "business", "status": "active"}
    for n in extra_hb:
        reg["tasks"][n] = {"layer": "heartbeat", "per_active_day": 2.0, "median_lines": 9,
                           "note": "测试", "status": "active"}
    p = os.path.join(tmp, "reg.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False)
    return p


def _mk_series(cases, hb_name="hb_alpha"):
    """内存造序列（纯函数测试用，免文件系统）。

    cases: [{"d": ..., "biz": n, "hb": n, "lines": n, "kinds": n?, "unclass": []?}]
    kinds 省略时 = biz（每任务各跑一次）；显式给 kinds<biz 表示同日重复跑（灌水）。
    """
    days = []
    for c in cases:
        biz, hb = c.get("biz", 0), c.get("hb", 0)
        kinds, lines = c.get("kinds", biz), c.get("lines", 40)
        days.append({"d": c["d"], "all": biz + hb, "hb": hb, "biz": biz,
                     "allK": sorted([hb_name] * hb + ["b%d" % i for i in range(kinds)]),
                     "bizK": sorted(["b%d" % i for i in range(kinds)]),
                     "bizL": [lines] * biz, "unclass": list(c.get("unclass", []))})
    return {"schema": "roi_series/v1", "generated_at": "selftest", "done_dir": "(selftest)",
            "registry_sha16": "0" * 16, "heartbeat_names": [hb_name],
            "n_days": len(days), "days": days}


def _normal_cases(n=70, start=date(2026, 1, 1), scale=1, spike=True):
    """有真实离散度的「正常」窗：biz 1..6*scale 循环、lines 30/40/50/60、ratio 周期尖峰。

    ★刻意不造常量序列——常量序列下任何分位都落在同一个值上，会（正确地）判 DEAD_THRESHOLD。
    """
    out = []
    # 周期 11：7 日滚动窗才会产生真实离散度（周期 ≤7 的循环会被滚动窗抹平，
    # 使分位网格全落在同一值上＝假 DEAD_THRESHOLD / 假 TOO_NOISY）。
    seq = [3, 4, 5, 6, 7, 8, 9, 4, 5, 6, 7]
    for i in range(n):
        biz = scale * seq[i % len(seq)]
        kinds = max(1, biz - 2) if (spike and i % 7 == 6) else biz
        out.append({"d": (start + timedelta(days=i)).isoformat(), "biz": biz, "kinds": kinds,
                    "hb": 2, "lines": 30 + 10 * (i % 4)})
    return out


def selftest():
    ok = fail = 0
    res = []

    def check(label, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        res.append("%s %s" % ("PASS" if cond else "FAIL", label))

    tmp = tempfile.mkdtemp(prefix="roi_cal_")
    old_reg = rtc.DEFAULT_REGISTRY
    old_out = THRESHOLDS_OUT
    try:
        regp = _synth_registry(tmp)
        rtc.DEFAULT_REGISTRY = regp
        globals()["THRESHOLDS_OUT"] = os.path.join(tmp, "thr_v3.json")

        # ---- A. 文件系统 scan 通路（类分层 / 未登记 / 去重种类）--------------
        spec = []
        for i in range(40):
            ds = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            spec += [(ds, "hb_alpha", 3, 10), (ds, "biz_one", 1, 40), (ds, "biz_two", 1, 40)]
        # 未登记名必须落在 40 天窗内（放到窗外的日期会多出第 41 天 ⇒ A1 假失败）
        spec += [("2026-01-20", "mystery_task", 1, 30)]
        s1 = scan(_synth_done(tmp, spec))
        day = [x for x in s1["days"] if x["d"] == "2026-01-20"][0]
        check("A1 scan 覆盖 40 天", s1["n_days"] == 40)
        check("A2 心跳名只进 hb 不进 biz", day["hb"] == 3 and day["biz"] == 2)
        check("A3 未登记名计入 unclass 且不污染业务计数", day["unclass"] == ["mystery_task"])
        check("A4 执行次数与去重种类分离", day["biz"] == 2 and len(day["bizK"]) == 2)
        m1 = metrics(s1)
        check("A5 unclass7 指标在同日=1",
              [r for r in m1 if r["d"] == "2026-01-20"][0]["unclass7"] == 1)

        # ---- B. 双口径结构假象（心跳可复现、业务口径免疫）--------------------
        # 结构假象：历史窗心跳型高频照跑（拉高全量口径），末尾 3 天心跳归档停跑（业务不变）
        c2 = _normal_cases(45)   # 2026-01-01..2026-02-14
        for c in c2[:-3]:
            c["hb"] = 25
        for c in c2[-3:]:
            c["hb"] = 0
        m2 = metrics(_mk_series(c2))
        check("B1 纯心跳日使全量口径 WoW 崩塌", m2[-1]["wow_all"] < -20)
        check("B2 同日业务型口径未崩塌", abs(m2[-1]["wow_biz"]) < 25)

        # ---- C. 阈值派生：分位网格搜索 + 可触发性闸门 -----------------------
        check("C1 quantile 纯函数正确",
              quantile([1, 2, 3, 4], 0.5) == 2.5 and quantile([], 0.5) is None
              and quantile([5], 0.05) == 5)
        mN = metrics(_mk_series(_normal_cases()))
        rulesN, gokN, probsN = derive_rules(mN)
        calN = [r for r in rulesN if r["kind"] == "calibrated"]
        check("C2 正常窗四条业务规则齐备且阈值非空",
              len(calN) == 4 and all(r["threshold"] is not None for r in calN))
        check("C3 正常窗全部规则通过可触发性闸门",
              gokN and all(r["gate"] == "PASS" for r in calN)
              and all(r["fires"] >= 1 and r["fire_rate"] <= FIRE_CAP for r in calN))
        check("C4 正常窗各规则触发率不低于下界",
              all(r["fire_rate"] >= MIN_FIRE_RATE for r in calN))
        mN2 = metrics(_mk_series(_normal_cases(scale=3)))
        thN = [r for r in rulesN if r["id"] == "R1"][0]["threshold"]
        thN2 = [r for r in derive_rules(mN2)[0] if r["id"] == "R1"][0]["threshold"]
        check("C5 R1 阈值随分布变化（非硬编码）", thN != thN2)

        # ★NC1：常量序列（零离散度）=> 全网格分位同值 => 必须判 DEAD_THRESHOLD 且不放行
        const = [{"d": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "biz": 3, "kinds": 3,
                  "hb": 2, "lines": 40} for i in range(45)]
        rc = derive_rules(metrics(_mk_series(const)))
        r1c = [r for r in rc[0] if r["id"] == "R1"][0]
        check("NC1 常量序列 R1 判 DEAD_THRESHOLD 且闸门不放行",
              r1c["gate"] == "DEAD_THRESHOLD" and rc[1] is False
              and any("DEAD_THRESHOLD R1" in p for p in rc[2]))
        check("NC2 退化语料 calibrate gate_ok=False 且不写盘",
              calibrate(_mk_series(const), write=True)["gate_ok"] is False
              and not os.path.exists(THRESHOLDS_OUT))

        # ---- D. 灵敏度：注入塌陷 / 灌水 / 未登记，规则必须触发 ---------------
        c3 = _normal_cases(45)
        for i in range(6):
            c3.append({"d": (date(2026, 2, 16) + timedelta(days=i)).isoformat(),
                       "biz": 0, "kinds": 0, "hb": 2, "lines": 10})
        rules3 = derive_rules(metrics(_mk_series(c3)))[0]
        r1 = [r for r in rules3 if r["id"] == "R1"][0]
        r3 = [r for r in rules3 if r["id"] == "R3"][0]
        tail = set((date(2026, 2, 10) + timedelta(days=i)).isoformat() for i in range(16))
        check("D1 业务塌陷触发 R1(bpd<) 且命中塌陷窗",
              r1["fires"] >= 1 and bool(set(r1["fire_dates"]) & tail))
        check("D2 业务下滑触发 R3(biz_drop>)", r3["fires"] >= 1)

        c4 = _normal_cases(50)
        c4.append({"d": "2026-02-20", "biz": 18, "kinds": 1, "hb": 0, "lines": 40})
        m4 = metrics(_mk_series(c4))
        d4 = [r for r in m4 if r["d"] == "2026-02-20"][0]
        dup = m4[-2]  # 注入日的前一天（正常日）
        check("D3 灌水日 ratio 上升（执行次数/去重种类可分离）",
              d4["exec7"] > dup["exec7"] and d4["ratio"] > dup["ratio"])

        c5 = _normal_cases(45)
        c5.append({"d": "2026-02-20", "biz": 3, "kinds": 3, "hb": 2, "lines": 40,
                   "unclass": ["mystery_task"]})
        r5 = [r for r in derive_rules(metrics(_mk_series(c5)))[0] if r["id"] == "R5"][0]
        check("D4 未登记名触发 R5(fail-closed) 且标 n/a",
              r5["fires"] >= 1 and r5["gate"] == "n/a(fail-closed)")

        # ---- E. 闸门纯函数语义 ---------------------------------------------
        check("E1 从不触发判 DEAD_THRESHOLD",
              any(x.startswith("DEAD_THRESHOLD") for x in triggerability_gate(
                  [{"id": "X1", "kind": "calibrated", "fires": 0}], 127)))
        check("E2 触发率超上限判 TOO_NOISY",
              any(x.startswith("TOO_NOISY") for x in triggerability_gate(
                  [{"id": "X2", "kind": "calibrated", "fires": 100}], 127)))
        check("E3 正常触发率通过闸门",
              triggerability_gate([{"id": "X3", "kind": "calibrated", "fires": 6}], 127) == [])

        # ---- F. 合格语料落盘 -------------------------------------------------
        doc = calibrate(_mk_series(_normal_cases()), write=True)
        check("F1 合格语料 gate_ok=True 且写盘",
              doc["gate_ok"] is True and os.path.exists(THRESHOLDS_OUT))
        saved = json.load(open(THRESHOLDS_OUT, encoding="utf-8"))
        check("F2 落盘 schema/规则数与内存一致",
              saved["schema"] == "roi_thresholds_v3" and len(saved["rules"]) == len(doc["rules"]))
    finally:
        rtc.DEFAULT_REGISTRY = old_reg
        globals()["THRESHOLDS_OUT"] = old_out

    print("\n".join(res))
    print("selftest: %d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


# -------------------------------------------------------------------- main ----
def _load_series(path):
    if path:
        return json.load(open(path, encoding="utf-8"))
    return scan()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        argv[0] = "selftest"
    ap = argparse.ArgumentParser(prog="roi_calibration.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--series", default=None)
    sp = sub.add_parser("scan"); sp.add_argument("--out", default=None)
    sub.add_parser("metrics")
    sub.add_parser("calibrate")
    sb = sub.add_parser("backtest"); sb.add_argument("--rules", default="all",
                                                     choices=["all", "v1", "v2", "v3", "none"])
    sub.add_parser("proof"); sub.add_parser("selftest")
    if not argv:
        ap.print_help(); return 2
    args = ap.parse_args(argv)

    if args.cmd == "scan":
        s = scan()
        out = args.out or os.path.join(OUT_DIR, "scan_%s.json" % datetime.now().strftime("%Y%m%d"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False)
        print("scan days=%d window=%s..%s heartbeat=%s" % (s["n_days"], s["days"][0]["d"],
                                                           s["days"][-1]["d"], ",".join(s["heartbeat_names"])))
        print("written %s (%dB)" % (os.path.relpath(out, GA_ROOT), os.path.getsize(out)))
        return 0
    if args.cmd == "metrics":
        rows = metrics(_load_series(args.series))
        for r in rows[-10:]:
            print(r["d"], json.dumps({k: v for k, v in r.items() if k != "d"}, ensure_ascii=False))
        return 0
    if args.cmd == "calibrate":
        doc = calibrate(_load_series(args.series))
        return 0 if doc["gate_ok"] else 1
    if args.cmd == "backtest":
        res = backtest(_load_series(args.series), args.rules)
        out = os.path.join(OUT_DIR, "backtest_%s.json" % datetime.now().strftime("%Y%m%d"))
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print("written %s" % os.path.relpath(out, GA_ROOT))
        return 0
    if args.cmd == "proof":
        return proof()
    if args.cmd == "selftest":
        return selftest()
    ap.print_help(); return 2


if __name__ == "__main__":
    sys.exit(main())
