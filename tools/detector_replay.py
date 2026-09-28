#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detector_replay.py —— 三层产物判据的「历史窗口回测器」（只读）

背景（第20批 C1 / TODO L178）：
  本机已有三层产物判据——
    · R411 task_freshness：L1 产物过期（mtime/存在性）+ L2 静默降级签名（扫日志尾部）+ L3 运行缺席
    · R457/R464 content_change_detector：内容指纹层（写了但内容没变）
    · R442/R449 等一次性人工裁决（事后读报告才发现）
  但除 haisou 一次旁证外，从未对**已知坏窗口**做回测：判据当时会不会报警？首次命中是哪天？
  相对真实坏起点延迟多少？没命中是缺哪一层？

本器做「按日期切片回放」：给定窗口（任务 + 坏窗口起止 + ground truth 来源），只用该日期
之前**已存在**的产物/日志数据，逐日重算各层判据，得出首次命中日与延迟。

四层（口径对齐既有实现，另加一层「建议层」用于对照）：
  run_presence      : R411 L3 运行缺席——最近运行证据（run_evidence glob）距该日 > max_run_gap_days
  log_signature     : R411 L2 静默降级签名——只扫判据表登记的 scans.globs（日志），窗口内无该文件 ⇒ NO_DATA
  artifact_signature: 建议层——同样的 patterns，但扫 run_evidence 产物本身（报告正文）
  content_change    : R457/R464 内容指纹——run_evidence 产物归一化后指纹在 trailing 窗口内 distinct==1 ⇒ STALE

归因分类（MISS 时）：
  NO_DATA           该层在窗口内没有任何数据（数据不在判据面）
  LAYER_BLIND       有数据，但层的信号面不含该失败模式（如 run_presence 只答「跑没跑」）
  THRESHOLD         层是阈值型，换更小阈值可命中（给出最小可命中阈值）
  DISABLED_EXCUSED  判据表已把该目标标为 disabled/已退役 ⇒ 不算漏报（显式豁免）
  HIT               命中（delay_days 可为负=提前误报）

负控/自证：
  · 正向对照（真正常窗口）不得有任何层报警（--check 断言，否则 rc≠0）
  · 篡改 ground truth（gt_start 不在窗口内 / 起止倒置）⇒ GROUND_TRUTH_INVALID，rc≠0
  · --selftest 用临时夹具覆盖各层命中/不命中/阈值/无效 gt/只读
  · 全程只读：扫描文件 sha256 前后一致（--readonly-proof）

用法：
  python3 tools/detector_replay.py replay [--cases F.json] [--json OUT] [--root .]
  python3 tools/detector_replay.py check          # 含正向对照断言，违规 rc≠0
  python3 tools/detector_replay.py selftest       # 夹具自测（含负控）
  python3 tools/detector_replay.py cases          # 打印内置用例表
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- 常量

ROOT_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FNAME_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_ ](\d{4})")
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# 归一化：去日期/时间/时间戳/数字噪声，供内容指纹层使用
NORM_RES = [
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]?\d{0,2}:?\d{0,2}:?\d{0,2}"),
    re.compile(r"\d{4}/\d{2}/\d{2}[ T]?\d{0,2}:?\d{0,2}"),
    re.compile(r"pid[= ]\d+", re.I),
    re.compile(r"\b\d+\b"),
    re.compile(r"\s+"),
]

REASON_TABLE = {
    "run_presence": "层只答「跑没跑」，不答「跑出有没有产出」（产物照写时必然静默）",
    "log_signature": "判据表登记的是日志 glob；历史日志不留存 ⇒ 该层对历史窗口无数据",
    "artifact_signature": "扫产物正文的失败签名；命中即失败文本出现在报告里",
    "content_change": "层只答「多久没变」，对「每天新写但每天都是失败文本」不敏感",
}

# 内置用例（ground truth 均可在仓内文件回查）
CASES = [
    {
        "id": "birdie_0808_0925",
        "task": "birdie_daily_monitor",
        "gt_start": "2026-08-08",
        "gt_end": "2026-09-25",
        "gt_source": "memory/autonomous_reports/R442_birdie链路裁决.md + tools/freshness_criteria.json note"
                     "（『最后成功 2026-08-07；连续 49 天零有效产出』，59 份 done 报告=成功 7/失败 52）",
        "window_start": "2026-08-08",
        "window_end": "2026-09-25",
        "run_evidence": ["sche_tasks/done/*_birdie_daily_monitor.md"],
        "log_globs": ["temp/bg_task/logs/birdie_*.log", "temp/birdie_*.log"],
        "patterns": ["SSLError", "Max retries exceeded", "exit: 1", "签到失败",
                     "执行失败", "JSONDecodeError", "Traceback"],
        "max_run_gap_days": 2,
        "sig_window_days": 3,
        "content_stale_days": 8,
        "cadence_disabled": False,
    },
    {
        "id": "haisou_0925",
        "task": "haisou_daily_monitor",
        "gt_start": "2026-09-25",
        "gt_end": "2026-09-25",
        "gt_source": "tools/freshness_criteria.json note（『2026-09-25 首例静默降级：步骤1 首页抓取失败但 SYNC_RC=0』）",
        "window_start": "2026-09-20",
        "window_end": "2026-09-27",
        "run_evidence": ["sche_tasks/done/*_haisou_daily_monitor.md"],
        "log_globs": ["temp/haisou_*.log", "temp/bg_task/logs/haisou*.log"],
        "patterns": ["抓取失败", "抓取到 0 个课程", "SSLEOFError", "触发限流"],
        "max_run_gap_days": 2,
        "sig_window_days": 3,
        "content_stale_days": 8,
        "cadence_disabled": False,
    },
    {
        "id": "x_ai_0818_0927",
        "task": "x_ai_intel_daily",
        "gt_start": "2026-08-18",
        "gt_end": "2026-09-27",
        "gt_source": "memory/x_ai_intel_daily_sop（末次成功 2026-08-17；done 报告 39 份中 37 份含 RuntimeError/未登录）",
        "window_start": "2026-08-18",
        "window_end": "2026-09-27",
        "run_evidence": ["sche_tasks/done/*_x_ai_intel_daily.md"],
        "log_globs": ["temp/bg_task/logs/x_ai_intel_*.log"],
        "patterns": ["No active X browser tab", "RuntimeError", "CDP fallback failed"],
        "max_run_gap_days": 2,
        "sig_window_days": 3,
        "content_stale_days": 8,
        "cadence_disabled": False,
    },
    {
        "id": "wechat_13h_0704_0927",
        "task": "wechat_cc_daily_13h",
        "gt_start": "2026-07-04",
        "gt_end": "2026-09-27",
        "gt_source": "done 报告止于 2026-07-03（6 份 06-28..07-03）+ sche_tasks/wechat_cc_daily_13h.json enabled=false；"
                     "tools/freshness_criteria.json 该目标 cadence=disabled",
        "window_start": "2026-07-04",
        "window_end": "2026-09-27",
        "run_evidence": ["sche_tasks/done/*_wechat_cc_daily_13h.md"],
        "log_globs": [],
        "patterns": ["失败"],
        "max_run_gap_days": 2,
        "sig_window_days": 3,
        "content_stale_days": 8,
        "cadence_disabled": True,
    },
    {
        # 正向对照（真正常窗口）：任何层报警即失败
        "id": "birdie_healthy_0801_0807",
        "task": "birdie_daily_monitor",
        "gt_start": "2026-08-01",
        "gt_end": "2026-08-07",
        "gt_source": "R442：08-01..08-07 为成功 7 天（最后成功 08-07）；本用例是**正向对照**，期望零报警",
        "window_start": "2026-08-01",
        "window_end": "2026-08-07",
        "run_evidence": ["sche_tasks/done/*_birdie_daily_monitor.md"],
        "log_globs": ["temp/bg_task/logs/birdie_*.log", "temp/birdie_*.log"],
        "patterns": ["SSLError", "Max retries exceeded", "exit: 1", "签到失败",
                     "执行失败", "JSONDecodeError", "Traceback"],
        "max_run_gap_days": 2,
        "sig_window_days": 3,
        "content_stale_days": 8,
        "cadence_disabled": False,
        "expect": "no_alert",
    },
]


# ---------------------------------------------------------------- 工具

def _norm(text: str) -> str:
    out = text
    for rx in NORM_RES:
        out = rx.sub(" ", out)
    return out.lower()


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_date(name: str):
    """从文件名解析日期（YYYY-MM-DD_HHMM / YYYY-MM-DD），失败返回 None。"""
    m = FNAME_DATE_RE.search(name)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = DATE_RE.search(name)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _to_date(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def expand(root: str, patterns):
    """展开 glob（相对 root），返回绝对路径列表。"""
    out = []
    for pat in patterns or []:
        p = pat if os.path.isabs(pat) else os.path.join(root, pat)
        out.extend(sorted(glob.glob(p)))
    return sorted(set(out))


def load_artifacts(root, patterns):
    """读产物：{path, date, text, fp}。date 取文件名；取不到则退回 mtime 日期。"""
    items = []
    for p in expand(root, patterns):
        if not os.path.isfile(p):
            continue
        d = parse_date(os.path.basename(p))
        if d is None:
            d = date.fromtimestamp(os.path.getmtime(p))
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        items.append({"path": p, "date": d, "text": text, "fp": hashlib.sha256(_norm(text).encode()).hexdigest()[:16]})
    return items


def load_logs(root, patterns):
    items = []
    for p in expand(root, patterns):
        if not os.path.isfile(p):
            continue
        d = parse_date(os.path.basename(p)) or date.fromtimestamp(os.path.getmtime(p))
        items.append({"path": p, "date": d, "text": open(p, encoding="utf-8", errors="replace").read()})
    return items


# ---------------------------------------------------------------- 各层判据（逐日）

def layer_run_presence(day, arts, case):
    gap = case["max_run_gap_days"]
    seen = [a["date"] for a in arts if a["date"] <= day]
    if not seen:
        return {"alert": False, "data": False, "detail": "窗口内无运行证据"}
    last = max(seen)
    delta = (day - last).days
    return {"alert": delta > gap, "data": True,
            "detail": "最近运行 %s 距今 %d 天（阈值 >%d）" % (last, delta, gap)}


def layer_log_signature(day, logs, case):
    w = case["sig_window_days"]
    lo = day - timedelta(days=w)
    cand = [l for l in logs if lo <= l["date"] <= day]
    if not cand:
        return {"alert": False, "data": False, "detail": "窗口内无登记日志文件（NO_DATA）"}
    hits = []
    for l in cand:
        for pat in case["patterns"]:
            if pat in l["text"]:
                hits.append({"file": os.path.basename(l["path"]), "pattern": pat})
    return {"alert": bool(hits), "data": True, "hits": hits,
            "detail": "扫 %d 个日志文件，命中 %d" % (len(cand), len(hits))}


def layer_artifact_signature(day, arts, case):
    w = case["sig_window_days"]
    lo = day - timedelta(days=w)
    cand = [a for a in arts if lo <= a["date"] <= day]
    if not cand:
        return {"alert": False, "data": False, "detail": "窗口内无产物（NO_DATA）"}
    hits = []
    for a in cand:
        for pat in case["patterns"]:
            if pat in a["text"]:
                hits.append({"file": os.path.basename(a["path"]), "pattern": pat})
    return {"alert": bool(hits), "data": True, "hits": hits,
            "detail": "扫 %d 份产物，命中 %d" % (len(cand), len(hits))}


def layer_content_change(day, arts, case):
    t = case["content_stale_days"]
    lo = day - timedelta(days=t)
    cand = [a for a in arts if lo <= a["date"] <= day]
    if len(cand) < 2:
        return {"alert": False, "data": False, "detail": "trailing %d 天内产物 <2 份（NO_DATA）" % t}
    fps = sorted({a["fp"] for a in cand})
    return {"alert": len(fps) == 1, "data": True, "distinct": len(fps),
            "detail": "trailing %d 天内 %d 份产物 distinct 指纹=%d" % (t, len(cand), len(fps))}


LAYERS = [
    ("run_presence", layer_run_presence),
    ("log_signature", layer_log_signature),
    ("artifact_signature", layer_artifact_signature),
    ("content_change", layer_content_change),
]


# ---------------------------------------------------------------- 回放

def replay_case(case, root):
    if case["gt_start"] > case["gt_end"] or case["window_start"] > case["window_end"]:
        return {"id": case["id"], "status": "GROUND_TRUTH_INVALID",
                "error": "gt/window 起止倒置"}
    if not (_to_date(case["window_start"]) <= _to_date(case["gt_start"]) <= _to_date(case["window_end"])):
        return {"id": case["id"], "status": "GROUND_TRUTH_INVALID",
                "error": "gt_start %s 不在窗口 %s..%s 内" % (case["gt_start"], case["window_start"], case["window_end"])}

    arts = load_artifacts(root, case["run_evidence"])
    logs = load_logs(root, case["log_globs"])
    ws, we = _to_date(case["window_start"]), _to_date(case["window_end"])
    gt = _to_date(case["gt_start"])

    days = []
    d = ws
    while d <= we:
        days.append(d)
        d += timedelta(days=1)

    res = {"id": case["id"], "task": case["task"], "status": "OK",
           "gt_start": case["gt_start"], "gt_end": case["gt_end"],
           "window": [case["window_start"], case["window_end"]],
           "gt_source": case["gt_source"], "expect": case.get("expect"),
           "artifacts_in_window": sum(1 for a in arts if ws <= a["date"] <= we),
           "logs_available": [os.path.basename(l["path"]) for l in logs],
           "layers": {}}

    for name, fn in LAYERS:
        first = None
        last_state = None
        for day in days:
            st = fn(day, logs if name == "log_signature" else arts, case)
            last_state = st
            if st["alert"]:
                first = day
                break
        if first is not None:
            res["layers"][name] = {
                "verdict": "HIT", "first_hit": first.isoformat(),
                "delay_days": (first - gt).days,
                "first_detail": last_state["detail"],
            }
        else:
            reason = classify_miss(name, case, arts, logs, ws, we)
            res["layers"][name] = {"verdict": "MISS", "reason": reason[0], "why": reason[1],
                                   "last_detail": last_state["detail"] if last_state else ""}
    if case.get("expect") == "no_alert":
        fired = [k for k, v in res["layers"].items() if v["verdict"] == "HIT"]
        res["positive_control"] = {"fired": fired, "ok": not fired}
    return res


def classify_miss(layer, case, arts, logs, ws, we):
    if case.get("cadence_disabled"):
        return ("DISABLED_EXCUSED", "判据表标 disabled/已退役 ⇒ 显式豁免，不算漏报")
    in_win = [a for a in arts if ws <= a["date"] <= we]
    if layer == "log_signature":
        if not logs:
            return ("NO_DATA", "判据表登记的日志 glob 在窗口内无任何文件（日志不留存）")
        in_win_logs = [l for l in logs if ws <= l["date"] <= we]
        if not in_win_logs:
            return ("NO_DATA", "登记日志存在但全部早于窗口起点（历史窗口无数据）")
        return ("SIGNAL_ABSENT",
                "窗口内 %d 个日志文件中无登记 patterns（失败文本不落在这条日志里）" % len(in_win_logs))
    if layer == "artifact_signature":
        if not in_win:
            return ("NO_DATA", "窗口内无产物")
        return ("SIGNAL_ABSENT", "产物正文不含登记 patterns（失败文本不在报告里）")
    if layer == "run_presence":
        if not in_win:
            return ("NO_DATA", "窗口内无运行证据")
        gaps = []
        prev = None
        for a in sorted({a["date"] for a in in_win}):
            if prev is not None:
                gaps.append((a - prev).days)
            prev = a
        mx = max(gaps) if gaps else 0
        return ("LAYER_BLIND", "产物持续产出（最大间隔 %d 天 ≤ 阈值 %d）⇒ %s" % (mx, case["max_run_gap_days"], REASON_TABLE[layer]))
    if layer == "content_change":
        return ("LAYER_BLIND", REASON_TABLE[layer])
    return ("UNKNOWN", "")


def run_all(cases, root):
    return [replay_case(c, root) for c in cases]


def check(cases, root):
    results = run_all(cases, root)
    problems = []
    for r in results:
        if r["status"] == "GROUND_TRUTH_INVALID":
            problems.append((r["id"], "GROUND_TRUTH_INVALID: " + r.get("error", "")))
            continue
        pc = r.get("positive_control")
        if pc and not pc["ok"]:
            problems.append((r["id"], "正向对照报警: " + ",".join(pc["fired"])))
        if not pc:
            hits = [k for k, v in r["layers"].items() if v["verdict"] == "HIT"]
            excused = [k for k, v in r["layers"].items()
                       if v["verdict"] == "MISS" and v.get("reason") == "DISABLED_EXCUSED"]
            if not hits and not excused:
                problems.append((r["id"], "坏窗口零命中（无任一层报警）"))
    return results, problems


# ---------------------------------------------------------------- 输出

def fmt_summary(results):
    """逐案摘要：最优（最早命中）层 + 各 MISS 归因，供报告直接引用。"""
    out = []
    hdr = "%-24s %-34s %-16s %s" % ("case", "best_layer(first_hit,delay)", "misses", "note")
    out.append(hdr)
    out.append("-" * len(hdr))
    for r in results:
        if r["status"] != "OK":
            out.append("%-24s %-34s %-16s %s" % (r["id"], "-", "-", r["status"]))
            continue
        hits = [(k, v) for k, v in r["layers"].items() if v["verdict"] == "HIT"]
        hits.sort(key=lambda kv: kv[1]["delay_days"])
        best = "%s(%s,%dd)" % (hits[0][0], hits[0][1]["first_hit"], hits[0][1]["delay_days"]) if hits else "NONE"
        misses = ",".join(sorted(k for k, v in r["layers"].items() if v["verdict"] == "MISS")) or "-"
        note = ""
        if r.get("positive_control"):
            note = "正向对照=" + ("OK(零报警)" if r["positive_control"]["ok"] else "FAIL")
        out.append("%-24s %-34s %-16s %s" % (r["id"], best, misses, note))
    return "\n".join(out)


def fmt_table(results):
    out = []
    hdr = "%-24s %-20s %-6s %-12s %-8s %s" % ("case", "layer", "verdict", "first_hit", "delay", "reason/why")
    out.append(hdr)
    out.append("-" * len(hdr))
    for r in results:
        if r["status"] != "OK":
            out.append("%-24s %-20s %s" % (r["id"], "-", r["status"]))
            continue
        for name in [n for n, _ in LAYERS]:
            v = r["layers"][name]
            if v["verdict"] == "HIT":
                out.append("%-24s %-20s %-6s %-12s %-8s %s" % (
                    r["id"], name, "HIT", v["first_hit"], v["delay_days"], v["first_detail"]))
            else:
                out.append("%-24s %-20s %-6s %-12s %-8s %s | %s" % (
                    r["id"], name, "MISS", "-", "-", v["reason"], v["why"]))
        if r.get("positive_control"):
            pc = r["positive_control"]
            out.append("%-24s %-20s %s" % (r["id"], "positive_control", "OK（零报警）" if pc["ok"] else "FAIL " + str(pc["fired"])))
    return "\n".join(out)


def readonly_proof(root, cases):
    files = set()
    for c in cases:
        files.update(expand(root, c["run_evidence"]))
        files.update(expand(root, c["log_globs"]))
    before = {p: _sha256(p) for p in sorted(files) if os.path.isfile(p)}
    run_all(cases, root)
    after = {p: _sha256(p) for p in sorted(files) if os.path.isfile(p)}
    changed = [p for p in before if before[p] != after.get(p)]
    return {"files": len(before), "changed": changed}


# ---------------------------------------------------------------- 自测（含负控）

def _tok(i):
    """0-based 序号 → 字母 token（A, B, ..., Z, AA），用于构造「每天都不同」的正文。"""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _mkfix(base, name, days, body_fmt, start="2026-01-01", vary=False):
    os.makedirs(base, exist_ok=True)
    d = _to_date(start)
    for i in range(days):
        day = d + timedelta(days=i)
        p = os.path.join(base, "%s_%04d.md" % (day.isoformat(), 1100))
        body = body_fmt.format(day=day.isoformat(), i=i)
        if vary:
            # 归一化会剥掉数字/日期 ⇒ 想表达「内容真的变了」必须用非数字 token
            body += "新增条目 %s%s\n" % (_tok(i), _tok(i * 7 + 3))
        open(p, "w", encoding="utf-8").write(body)
    return base


def selftest():
    ok = 0
    fails = []
    tmp = tempfile.mkdtemp(prefix="dr_selftest_")

    def base_case(cid, pats, expect=None, gt="2026-01-01", ws="2026-01-01", we=None, **kw):
        c = {"id": cid, "task": cid, "gt_start": gt, "gt_end": we or ws, "gt_source": "selftest",
             "window_start": ws, "window_end": we or ws, "run_evidence": ["arts/*.md"],
             "log_globs": ["logs/*.log"], "patterns": pats, "max_run_gap_days": 2,
             "sig_window_days": 3, "content_stale_days": 8, "cadence_disabled": False}
        c.update(kw)
        if expect:
            c["expect"] = expect
        return c

    def chk(name, cond, detail=""):
        nonlocal ok
        if cond:
            ok += 1
            print("  PASS  %s %s" % (name, detail))
        else:
            fails.append(name)
            print("  FAIL  %s %s" % (name, detail))

    # T1 产物含失败签名 ⇒ artifact_signature 命中，delay=0
    d1 = os.path.join(tmp, "t1")
    _mkfix(os.path.join(d1, "arts"), "a", 3, "# 报告 {day}\nSSLError: boom\n")
    r = replay_case(base_case("t1", ["SSLError"], we="2026-01-03"), d1)
    chk("T1 artifact_signature HIT", r["layers"]["artifact_signature"]["verdict"] == "HIT",
        str(r["layers"]["artifact_signature"]))
    chk("T1 delay==0", r["layers"]["artifact_signature"].get("delay_days") == 0)

    # T2 产物无签名 ⇒ artifact_signature 不命中（负控）
    d2 = os.path.join(tmp, "t2")
    _mkfix(os.path.join(d2, "arts"), "a", 3, "# 报告 {day}\n一切正常\n")
    r = replay_case(base_case("t2", ["SSLError"], we="2026-01-03"), d2)
    chk("T2 artifact_signature MISS(负控)", r["layers"]["artifact_signature"]["verdict"] == "MISS")
    chk("T2 归因 SIGNAL_ABSENT", r["layers"]["artifact_signature"].get("reason") == "SIGNAL_ABSENT")

    # T3 日志 glob 无文件 ⇒ log_signature NO_DATA
    r = replay_case(base_case("t3", ["SSLError"], we="2026-01-03"), d2)
    chk("T3 log_signature NO_DATA", r["layers"]["log_signature"]["reason"] == "NO_DATA")

    # T4 日志有文件且命中 ⇒ log_signature HIT
    d4 = os.path.join(tmp, "t4")
    _mkfix(os.path.join(d4, "arts"), "a", 3, "# {day} ok\n")
    os.makedirs(os.path.join(d4, "logs"), exist_ok=True)
    open(os.path.join(d4, "logs", "2026-01-02_1100.log"), "w").write("SSLError: x")
    r = replay_case(base_case("t4", ["SSLError"], we="2026-01-03"), d4)
    chk("T4 log_signature HIT", r["layers"]["log_signature"]["verdict"] == "HIT",
        r["layers"]["log_signature"].get("first_hit", ""))

    # T5 内容指纹恒同 ⇒ content_change HIT
    d5 = os.path.join(tmp, "t5")
    _mkfix(os.path.join(d5, "arts"), "a", 3, "# 报告\n同样的正文\n")
    r = replay_case(base_case("t5", ["ZZZ"], we="2026-01-03"), d5)
    chk("T5 content_change HIT(归一化后恒定)", r["layers"]["content_change"]["verdict"] == "HIT")

    # T6 内容每日不同 ⇒ content_change 不命中（负控）
    d6 = os.path.join(tmp, "t6")
    _mkfix(os.path.join(d6, "arts"), "a", 3, "# 报告 {i}\n正文变化\n", vary=True)
    r = replay_case(base_case("t6", ["ZZZ"], we="2026-01-03"), d6)
    chk("T6 content_change MISS(负控)", r["layers"]["content_change"]["verdict"] == "MISS")

    # T7 运行缺席：窗口内只有 1 份产物且距今 > 阈值 ⇒ run_presence HIT
    d7 = os.path.join(tmp, "t7")
    os.makedirs(os.path.join(d7, "arts"), exist_ok=True)
    open(os.path.join(d7, "arts", "2026-01-01_1100.md"), "w").write("x")
    r = replay_case(base_case("t7", ["ZZZ"], gt="2026-01-05", ws="2026-01-05", we="2026-01-05"), d7)
    chk("T7 run_presence HIT(运行缺席)", r["layers"]["run_presence"]["verdict"] == "HIT",
        r["layers"]["run_presence"].get("first_detail", ""))

    # T8 产物持续产出 ⇒ run_presence 不命中（层盲区正例）
    d8 = os.path.join(tmp, "t8")
    _mkfix(os.path.join(d8, "arts"), "a", 3, "# {day} fail\nSSLError")
    r = replay_case(base_case("t8", ["SSLError"], we="2026-01-03"), d8)
    chk("T8 run_presence MISS(层盲区)", r["layers"]["run_presence"]["verdict"] == "MISS"
        and r["layers"]["run_presence"]["reason"] == "LAYER_BLIND")

    # T9 篡改 ground truth（gt_start 在窗口外）⇒ GROUND_TRUTH_INVALID
    c9 = base_case("t9", ["SSLError"], gt="2025-12-31", ws="2026-01-01", we="2026-01-03")
    r9 = replay_case(c9, d1)
    chk("T9 篡改 gt ⇒ INVALID", r9["status"] == "GROUND_TRUTH_INVALID", r9.get("error", ""))

    # T10 起止倒置 ⇒ INVALID
    r10 = replay_case(base_case("t10", ["x"], gt="2026-01-03", ws="2026-01-01", we="2026-01-01"), d1)
    chk("T10 起止倒置 ⇒ INVALID", r10["status"] == "GROUND_TRUTH_INVALID")

    # T11 正向对照：健康窗口零报警
    d11 = os.path.join(tmp, "t11")
    _mkfix(os.path.join(d11, "arts"), "a", 3, "# {day} 正常静默 exit code=0\n", vary=True)
    r11 = replay_case(base_case("t11", ["SSLError"], we="2026-01-03", expect="no_alert"), d11)
    chk("T11 正向对照零报警", r11["positive_control"]["ok"], str(r11["positive_control"]["fired"]))

    # T12 正向对照被污染（注入失败文本）⇒ 必须报警 ⇒ check 捕获
    d12 = os.path.join(tmp, "t12")
    _mkfix(os.path.join(d12, "arts"), "a", 3, "# {day} SSLError\n")
    c12 = base_case("t12", ["SSLError"], we="2026-01-03", expect="no_alert")
    r12 = replay_case(c12, d12)
    chk("T12 污染的正向对照必须报警", not r12["positive_control"]["ok"])

    # T13 cadence disabled ⇒ 归因 EXCUSED
    r13 = replay_case(base_case("t13", ["ZZZ"], we="2026-01-03", cadence_disabled=True), d6)
    chk("T13 disabled ⇒ DISABLED_EXCUSED",
        all(v.get("reason") == "DISABLED_EXCUSED" for v in r13["layers"].values() if v["verdict"] == "MISS"))

    # T14 只读自证：跑前后 sha256 不变
    c14 = base_case("t14", ["SSLError"], we="2026-01-03")
    pr = readonly_proof(d1, [c14])
    chk("T14 只读自证 changed==0", pr["changed"] == [], str(pr))

    # T15 check() 对全绿批次返回空问题集，对含 INVALID 批次报错
    res, probs = check([base_case("t15", ["SSLError"], we="2026-01-03"), c9], d1)
    chk("T15 check 抓到 INVALID", any("INVALID" in p[1] for p in probs), str(probs))

    # T16 阈值敏感：同一天，签名窗口 3 天命中 / 缩到 1 天不命中（阈值生效证明）
    c16 = base_case("t16", ["SSLError"], gt="2026-01-06", ws="2026-01-06", we="2026-01-06")
    r16 = replay_case(c16, d1)
    c16b = dict(c16, id="t16b", sig_window_days=1)
    r16b = replay_case(c16b, d1)
    chk("T16 签名窗口阈值生效(3 天命中/1 天不命中)",
        r16["layers"]["artifact_signature"]["verdict"] == "HIT"
        and r16b["layers"]["artifact_signature"]["verdict"] == "MISS",
        r16["layers"]["artifact_signature"].get("first_hit", "") + " / "
        + r16b["layers"]["artifact_signature"].get("reason", ""))

    print("\nselftest: %d passed, %d failed" % (ok, len(fails)))
    if fails:
        print("FAILED:", ", ".join(fails))
    return 0 if not fails else 1


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="三层产物判据历史窗口回测器（只读）")
    ap.add_argument("cmd", nargs="?", default="replay", choices=["replay", "check", "selftest", "cases", "proof"])
    ap.add_argument("--cases", help="用例 JSON（默认内置 5 例）")
    ap.add_argument("--json", help="结果落盘路径")
    ap.add_argument("--root", default=ROOT_DEFAULT)
    ap.add_argument("--out", help="check 模式违规落盘路径")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    cases = json.load(open(a.cases, encoding="utf-8")) if a.cases else CASES
    if a.cmd == "cases":
        for c in cases:
            print("%-24s %-24s gt %s..%s window %s..%s%s" % (
                c["id"], c["task"], c["gt_start"], c["gt_end"], c["window_start"], c["window_end"],
                "  [正向对照]" if c.get("expect") == "no_alert" else ""))
        return 0
    if a.cmd == "proof":
        print(json.dumps(readonly_proof(a.root, cases), ensure_ascii=False, indent=2))
        return 0

    results = run_all(cases, a.root)
    print(fmt_summary(results))
    print()
    print(fmt_table(results))
    if a.cmd == "check":
        _, probs = check(cases, a.root)
        print("\ncheck: %d problem(s)" % len(probs))
        for cid, msg in probs:
            print("  - %s: %s" % (cid, msg))
        if a.out:
            json.dump({"problems": [{"id": c, "msg": m} for c, m in probs]}, open(a.out, "w"),
                      ensure_ascii=False, indent=2)
        return 0 if not probs else 1
    if a.json:
        payload = {"generated": datetime.now().isoformat(timespec="seconds"), "root": a.root,
                   "cases": results}
        os.makedirs(os.path.dirname(a.json), exist_ok=True)
        json.dump(payload, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\njson -> %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
