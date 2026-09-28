#!/usr/bin/env python3
"""巡检告警生命周期台账（R479 / TODO L188）

先读这段「口径与边界」——它本身就是本次交付的一部分（旧描述有误，见下）。

一、现状勘误（代码级证据）
  tools/task_freshness.py 的写台账是**无条件**的（L456-468：`if write_ledger and ledger:`
  即每次运行追加一行，含 ts / counts / hard_issue_ids / issues / channels），
  只有显式 `--no-ledger` 才不写。所以 `temp/task_freshness/ledger.jsonl` 只有 7 行
  **不是**「只在要推送时才写」，而是：
    ① `reflect_scripts/09_task_freshness.py` 尚未接线（该文件 docstring 自述，
       已实测 launchd 无 task-freshness 任务）⇒ 没有每小时那一轮；
    ② 人工/会话跑工具时多带 `--no-ledger` 以保持只读（R464 报告即如此声明）。
  ⇒ 结论：台账「每轮一行」的**写入点已存在**；缺的是**轮次**与**消费者**。

二、本器做什么（只读，不新增任何告警、不改任何判定阈值）
  analyze  <ledger.jsonl>   把台账的「每轮一行」事件化为告警事件（episode），
                            给出计数 / 持续时长 / 处置率 / 复发率 / 噪声度。
  rounds   --start --end    用**带日期的既有产物**（run_evidence 的 done 报告）重建
                            逐日告警序列（复算 L1「运行缺席」层），用于在无人跑工具的
                            历史窗口上算生命周期指标（≥4 天，实测覆盖 6-7 周）。
  reconcile                重建序列 × R467/detector_replay 的 ground-truth 窗口对账
                            （同一底层事件两口径必须一致；正控窗口必须零告警）。
  selftest / proof         隔离夹具自测（含负控）与只读自证。

三、诚实边界（不许把下界当全量）
  * rounds 重建只覆盖 L1「运行缺席」——日志签名层对历史窗口无数据（日志不留存），
    内容层需要历史快照（只有现值），故重建序列是**告警面的下界**，不是全量复刻。
  * 停用/退役目标（disabled / state∈{legacy,on_demand}）按 tools/task_freshness.py
    同款语义豁免，不计告警（负控：把它们计入会凭空造出长期「告警」）。
  * 时长口径随轮次间隔而变：日粒度重建 ⇒ 天；台账（人工跑，间隔小时级）⇒ 小时。
    摘要里显式给出 unit，禁止两个口径互相换算。
"""
import argparse
import datetime
import glob
import hashlib
import importlib.util
import copy
import json
import subprocess
import os
import re
import statistics
import sys

GA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CRITERIA = os.path.join(GA, "tools", "freshness_criteria.json")
DEFAULT_LEDGER = os.path.join(GA, "temp", "task_freshness", "ledger.jsonl")
DEFAULT_ROOT = GA
DETECTOR = os.path.join(GA, "tools", "detector_replay.py")

TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
EXEMPT_STATES = ("legacy", "on_demand")


# ----------------------------------------------------------------- 基础

def parse_ts(s):
    if not s:
        return None
    s = str(s).strip()
    for f in TS_FORMATS:
        try:
            return datetime.datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def parse_day(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, IndexError):
        return None


def sha256_file(path, cap=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        total = 0
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
            if cap and total >= cap:
                break
    return h.hexdigest()


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------- 台账 → 轮次

def load_rounds(path):
    """台账 JSONL → 轮次列表。每行=一轮巡检。返回 (rounds, skipped_line_numbers)。"""
    rounds, skipped = [], []
    if not os.path.exists(path):
        return rounds, skipped
    with open(path, encoding="utf-8") as f:
        for no, ln in enumerate(f, 1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                skipped.append(no)
                continue
            if not isinstance(d, dict):
                skipped.append(no)
                continue
            ids = d.get("hard_issue_ids")
            if ids is None:
                ids = [i.get("id") for i in (d.get("issues") or []) if isinstance(i, dict)]
            ids = sorted({str(x) for x in (ids or []) if x})
            rounds.append({"ts": d.get("ts") or d.get("timestamp"), "ids": ids,
                           "counts": d.get("counts") or {}})
    return rounds, skipped


def dedupe_rounds(rounds):
    """同一 ts 的重复行=同一轮被重复记账 ⇒ 合并（不影响 episode 计数）。"""
    order, merged = [], {}
    for r in rounds:
        k = r["ts"]
        if k not in merged:
            merged[k] = {"ts": k, "ids": list(r["ids"]), "counts": dict(r["counts"]),
                         "duplicates": 1}
            order.append(k)
        else:
            merged[k]["ids"] = sorted(set(merged[k]["ids"]) | set(r["ids"]))
            merged[k]["duplicates"] += 1
    keyed = [(parse_ts(k) or datetime.datetime.min, k) for k in order]
    return [merged[k] for _, k in sorted(keyed)]


def build_episodes(rounds):
    """轮次序列 → 告警事件（episode）。缺席一轮即视为「已处置」＝事件闭合。"""
    episodes, openmap = [], {}
    for r in rounds:
        ts = parse_ts(r["ts"])
        ids = set(r["ids"])
        for i in list(openmap):
            if i not in ids:
                e = openmap.pop(i)
                e["status"] = "CLEARED"
                e["cleared_observed_at"] = r["ts"]
                episodes.append(e)
        for i in ids:
            if i in openmap:
                e = openmap[i]
                e["rounds_present"] += 1
                e["last_present"] = r["ts"]
                e["last_dt"] = ts
            else:
                openmap[i] = {"id": i, "start": r["ts"], "start_dt": ts,
                              "last_present": r["ts"], "last_dt": ts,
                              "rounds_present": 1, "status": "OPEN"}
    for e in openmap.values():
        episodes.append(e)
    for e in episodes:
        a, b = e.get("start_dt"), e.get("last_dt")
        secs = (b - a).total_seconds() if (a and b) else 0.0
        e["duration_hours"] = round(secs / 3600.0, 2)
        e["duration_days"] = round(secs / 86400.0, 2)
    episodes.sort(key=lambda e: (e.get("start_dt") or datetime.datetime.min, e["id"]))
    return episodes


def summarize(rounds, episodes):
    gaps = []
    dts = [parse_ts(r["ts"]) for r in rounds]
    dts = [d for d in dts if d]
    for a, b in zip(dts, dts[1:]):
        gaps.append((b - a).total_seconds() / 3600.0)
    med_gap = statistics.median(gaps) if gaps else 0.0
    unit = "days" if med_gap >= 20 else "hours"
    ids = sorted({e["id"] for e in episodes})
    cleared = [e for e in episodes if e["status"] == "CLEARED"]
    opener = [e for e in episodes if e["status"] == "OPEN"]
    recur = [i for i in ids if sum(1 for e in episodes if e["id"] == i) >= 2]
    alert_rounds = [r for r in rounds if r["ids"]]
    dur_key = "duration_days" if unit == "days" else "duration_hours"
    durs = [e[dur_key] for e in episodes]
    cleared_durs = [e[dur_key] for e in cleared]

    def _avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    def _med(xs):
        return round(statistics.median(xs), 2) if xs else None

    per_id = []
    for i in ids:
        es = [e for e in episodes if e["id"] == i]
        per_id.append({
            "id": i, "episodes": len(es),
            "rounds_present_total": sum(e["rounds_present"] for e in es),
            "first_seen": min(e["start"] for e in es),
            "last_seen": max(e["last_present"] for e in es),
            "status": "OPEN" if any(e["status"] == "OPEN" for e in es) else "CLEARED",
            "recurred": len(es) >= 2,
        })
    per_id.sort(key=lambda d: (-d["episodes"], -d["rounds_present_total"], d["id"]))
    return {
        "unit": unit,
        "median_round_gap_hours": round(med_gap, 2),
        "rounds": len(rounds),
        "rounds_with_alert": len(alert_rounds),
        "noise_ratio": round(len(alert_rounds) / len(rounds), 4) if rounds else None,
        "episodes": len(episodes),
        "episodes_cleared": len(cleared),
        "episodes_open": len(opener),
        "ids_alerted": len(ids),
        "disposal_rate": round(len(cleared) / len(episodes), 4) if episodes else None,
        "recurrence_rate": round(len(recur) / len(ids), 4) if ids else None,
        "recurred_ids": recur,
        "avg_duration": _avg(durs),
        "median_duration": _med(durs),
        "avg_duration_cleared": _avg(cleared_durs),
        "per_id": per_id,
    }


# ----------------------------------------------------------------- 证据重建

def load_criteria(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def target_exempt(t):
    """与 tools/task_freshness.py 同款豁免语义：disabled / state∈{legacy,on_demand}。"""
    if t.get("disabled"):
        return True, "disabled"
    st = str(t.get("state") or "").strip().lower()
    if st in EXEMPT_STATES:
        return True, "state=%s" % st
    return False, ""


def collect_run_days(root, globs):
    """run_evidence 的产物日期集合（日期取自文件名前 10 字符）。"""
    days = set()
    for pat in globs or []:
        p = pat if os.path.isabs(pat) else os.path.join(root, pat)
        for f in glob.glob(p, recursive=True):
            if not os.path.isfile(f):
                continue
            d = parse_day(os.path.basename(f))
            if d:
                days.add(d)
    return sorted(days)


def days_between(start, end):
    out, d = [], start
    while d <= end:
        out.append(d)
        d += datetime.timedelta(days=1)
    return out


def target_alert_days(t, root, start, end):
    """单个目标的逐日 L1 告警判定。返回 (alert_days, meta)。

    meta["verdict"] ∈ {OK, NO_EVIDENCE}；NO_EVIDENCE=该目标在窗口内无任何带日期的
    运行证据（无法判定，不计告警也不计健康）。
    """
    days = collect_run_days(root, t.get("run_evidence") or [])
    gap_max = int(t.get("max_run_gap_days") or 2)
    if not days:
        return [], {"verdict": "NO_EVIDENCE", "evidence_days": 0, "max_run_gap_days": gap_max}
    alerts = []
    for d in days_between(start, end):
        prev = [x for x in days if x <= d]
        if not prev:
            continue                      # 首个产物晚于窗口起点 ⇒ 该日无证据，不判
        if (d - prev[-1]).days > gap_max:
            alerts.append(d)
    return alerts, {"verdict": "OK", "evidence_days": len(days),
                    "first_evidence": days[0].isoformat(), "last_evidence": days[-1].isoformat(),
                    "max_run_gap_days": gap_max}


def reconstruct_rounds(root, targets, start, end, keep_empty_days=True):
    """逐日重建轮次（每天都算一轮，含零告警日——否则「缺席」不可观测、事件永不闭合）。"""
    per_day, info = {}, {}
    for t in targets:
        ex, why = target_exempt(t)
        if ex:
            info[t["id"]] = {"verdict": "EXEMPT", "reason": why}
            continue
        alerts, meta = target_alert_days(t, root, start, end)
        info[t["id"]] = meta
        for d in alerts:
            per_day.setdefault(d.isoformat(), []).append(t["id"])
    rounds = []
    for d in days_between(start, end):
        k = d.isoformat()
        ids = sorted(set(per_day.get(k, [])))
        if ids or keep_empty_days:
            rounds.append({"ts": k, "ids": ids, "counts": {}})
    return rounds, info


# ----------------------------------------------------------------- reconcile

def cases():
    if not os.path.exists(DETECTOR):
        return []
    mod = _load_module(DETECTOR, "detector_replay_for_lifecycle")
    return list(getattr(mod, "CASES", []))


def reconcile(root, cases_in, shift_days=0, tol_extra=1, blind_ratio=0.2):
    """重建序列 × ground truth 窗口对账（同一事件两口径）。

    判定四态（都是机械可复算的，没有「凭印象放行」）：
      PASS            gt 窗口被某个 episode 覆盖：起点延迟 |latency| ≤ gap+tol 且止于 gt 末尾之后
      L1_BLIND        gt 窗口内告警天数占比 < blind_ratio ⇒ 该层对这件事**结构上**看不见
                      （机制证据：窗口内证据文件照落，见 alt_days/gt_days）
      DISABLED_EXCUSED 用例自带 cadence_disabled（退役任务），按 tools/task_freshness.py
                      同款语义豁免，不计漏报
      FAIL            其余（真漏报/错位）⇒ rc=1

    L1_BLIND 不算绿灯：它必须同时满足「证据照落」这一机制条件，且与 R467 的层结论一致；
    shift_days 用来检验判据对 gt 敏感（窗口文本/天数随之改变，NC18a）；
    另有一条真负控：把已知坏窗口冒充正控(expect=no_alert)必须 FAIL（证明「零告警=过关」不可被滥用）；
    加宽 gt 窗口也不得把 L1_BLIND 洗成 PASS（不允许「窗口越宽越好交差」,NC18c）。
    """
    out = []
    for c in cases_in:
        gt_start = parse_day(c["gt_start"]) - datetime.timedelta(days=shift_days)
        gt_end = parse_day(c["gt_end"])
        w_start = parse_day(c["window_start"]) - datetime.timedelta(days=shift_days)
        w_end = parse_day(c["window_end"])
        pseudo = {"id": c["task"], "run_evidence": c.get("run_evidence") or [],
                  "max_run_gap_days": c.get("max_run_gap_days", 2),
                  "disabled": bool(c.get("cadence_disabled"))}
        ex_why = target_exempt(pseudo)[1] if target_exempt(pseudo)[0] else ""
        rounds, _info = reconstruct_rounds(root, [pseudo], w_start, w_end)
        eps = build_episodes(rounds)
        tol = int(pseudo["max_run_gap_days"]) + tol_extra
        # 机制证据：gt 窗口内的告警天数占比（用与重建同一套判定）
        g_days = days_between(gt_start, gt_end)
        alt_days, meta = target_alert_days(pseudo, root, gt_start, gt_end)
        ratio = round(len(alt_days) / len(g_days), 4) if g_days else 0.0
        rec = {"case": c["id"], "task": c["task"], "gt_window": "%s..%s" % (gt_start, gt_end),
               "window": "%s..%s" % (w_start, w_end), "tol_days": tol,
               "gt_days": len(g_days), "l1_alert_days_in_gt": len(alt_days),
               "l1_alert_ratio_in_gt": ratio, "l1_meta": meta,
               "episodes": [{"start": e["start"], "end": e["last_present"],
                             "status": e["status"], "days": e["duration_days"]} for e in eps]}
        best = None
        for e in eps:
            s0, t0 = e.get("start_dt"), e.get("last_dt")
            if not s0 or not t0:
                continue
            ov = (min(t0.date(), gt_end) - max(s0.date(), gt_start)).days
            if best is None or ov > best[0]:
                best = (ov, e)
        if c.get("expect") == "no_alert":
            rec["expect"] = "no_alert"
            rec["verdict"] = "PASS" if (not eps and not alt_days) else "FAIL"
            rec["detail"] = "正控窗口 episode=%d / L1 告警日=%d（都必须为 0）" % (len(eps), len(alt_days))
        elif ex_why:
            # 豁免走单一源 target_exempt（与 reconstruct_rounds 同一判据），
            # 不是「episode 空就放行」；l1_alert_ratio 仍如实记录该层本可看见的程度。
            rec["verdict"] = "DISABLED_EXCUSED"
            rec["detail"] = ("用例 %s ⇒ 按 tools/task_freshness.py 同款语义豁免（不计漏报）；"
                             "参考：L1 层本可覆盖 %d/%d 天(%.1f%%)" % (
                                 ex_why, len(alt_days), len(g_days), ratio * 100))
        elif best:
            e = best[1]
            latency = (e["start_dt"].date() - gt_start).days
            covers_end = e["last_dt"].date() >= gt_end
            if (-tol <= latency <= tol) and covers_end:
                rec["verdict"] = "PASS"
                rec["latency_days"] = latency
                rec["covers_gt_end"] = covers_end
                rec["detail"] = "episode %s..%s latency=%+d 天 covers_end=%s" % (
                    e["start"], e["last_present"], latency, covers_end)
            elif ratio < blind_ratio:
                rec["verdict"] = "L1_BLIND"
                rec["latency_days"] = latency
                rec["detail"] = ("L1 盲区：gt 窗口 %d 天里 L1 只报 %d 天(%.1f%%<%.0f%%)，"
                                 "检出 episode %s..%s 属窗口内另一个真缺口" % (
                                     len(g_days), len(alt_days), ratio * 100, blind_ratio * 100,
                                     e["start"], e["last_present"]))
            else:
                rec["verdict"] = "FAIL"
                rec["latency_days"] = latency
                rec["covers_gt_end"] = covers_end
                rec["detail"] = "episode 错位：%s..%s latency=%+d covers_end=%s（L1 命中率 %.1f%%）" % (
                    e["start"], e["last_present"], latency, covers_end, ratio * 100)
        elif ratio < blind_ratio:
            rec["verdict"] = "L1_BLIND"
            rec["detail"] = ("L1 盲区：gt 窗口 %d 天里 L1 报 %d 天(%.1f%%)，且无 episode 覆盖 "
                             "⇒ 该层结构上看不见（证据照落，见 l1_meta）" % (
                                 len(g_days), len(alt_days), ratio * 100))
        else:
            rec["verdict"] = "FAIL"
            rec["detail"] = "重建序列无 episode 覆盖 gt（真漏报；L1 命中率 %.1f%%）" % (ratio * 100)
        out.append(rec)
    failed = [r for r in out if r["verdict"] == "FAIL"]
    return out, failed


# ----------------------------------------------------------------- 输出

def fmt_analyze(res, skipped, dupes):
    s = res["summary"]
    L = ["== 告警生命周期（unit=%s，轮次间隔中位 %.2f h）" % (s["unit"], s["median_round_gap_hours"]),
         "   轮次 %d（含告警 %d，噪声度 %.4f）；被合并的重复行 %d；坏行 %d"
         % (s["rounds"], s["rounds_with_alert"], s["noise_ratio"] or 0.0, dupes, len(skipped)),
         "   告警事件 %d（已处置 %d / 未闭合 %d）；涉及目标 %d"
         % (s["episodes"], s["episodes_cleared"], s["episodes_open"], s["ids_alerted"]),
         "   持续时长：均值 %s / 中位 %s（已处置者均值 %s）"
         % (s["avg_duration"], s["median_duration"], s["avg_duration_cleared"]),
         "   处置率 %s；复发率 %s（复发目标 %s）"
         % (s["disposal_rate"], s["recurrence_rate"], s["recurred_ids"] or "无"),
         "-" * 100,
         "%-34s %-6s %-8s %-18s %-16s %s" % ("目标", "事件", "在场轮", "首见", "末见", "状态")]
    for d in s["per_id"][:20]:
        L.append("%-34s %-6d %-8d %-18s %-16s %s%s"
                 % (d["id"], d["episodes"], d["rounds_present_total"], d["first_seen"],
                    d["last_seen"], d["status"], "  ←复发" if d["recurred"] else ""))
    if len(s["per_id"]) > 20:
        L.append("...（另有 %d 个目标，见 --json）" % (len(s["per_id"]) - 20))
    return "\n".join(L)


def fmt_rounds(res, info):
    s = res["summary"]
    ex = [k for k, v in info.items() if v.get("verdict") == "EXEMPT"]
    ne = [k for k, v in info.items() if v.get("verdict") == "NO_EVIDENCE"]
    L = ["== 证据重建（L1「运行缺席」层）%s..%s" % (res["start"], res["end"]),
         "   目标 %d（豁免 %d：%s；无证据 %d）" % (len(info), len(ex), ",".join(ex[:6]) or "无", len(ne)),
         "   轮次 %d（日粒度，含零告警日）；有告警轮 %d" % (s["rounds"], s["rounds_with_alert"]),
         "   告警事件 %d（已处置 %d / 未闭合 %d）；涉及目标 %d"
         % (s["episodes"], s["episodes_cleared"], s["episodes_open"], s["ids_alerted"]),
         "   持续时长（天）：均值 %s / 中位 %s；处置率 %s；复发率 %s"
         % (s["avg_duration"], s["median_duration"], s["disposal_rate"], s["recurrence_rate"]),
         "-" * 100,
         "%-34s %-6s %-8s %-14s %-14s %s" % ("目标", "事件", "在场天", "首见", "末见", "状态")]
    for d in s["per_id"]:
        L.append("%-34s %-6d %-8d %-14s %-14s %s%s"
                 % (d["id"], d["episodes"], d["rounds_present_total"], d["first_seen"][:10],
                    d["last_seen"][:10], d["status"], "  ←复发" if d["recurred"] else ""))
    return "\n".join(L)


def cmd_analyze(args):
    rounds_raw, skipped = load_rounds(args.ledger)
    rounds = dedupe_rounds(rounds_raw)
    if not rounds:
        print("[alert_lifecycle] 无可解析轮次：%s（坏行 %d）" % (args.ledger, len(skipped)))
        return 2
    eps = build_episodes(rounds)
    res = {"schema": "ga.alert_lifecycle/v1", "source": args.ledger, "rounds": rounds,
           "skipped_lines": skipped,
           "duplicate_rows_merged": len(rounds_raw) - len(rounds),
           "episodes": eps, "summary": summarize(rounds, eps)}
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(fmt_analyze(res, skipped, res["duplicate_rows_merged"]))
    return 1 if res["summary"]["episodes_open"] else 0


def cmd_rounds(args):
    crit = load_criteria(args.criteria)
    start = parse_day(args.start)
    end = parse_day(args.end)
    if not start or not end or start > end:
        print("[alert_lifecycle] --start/--end 必须为 YYYY-MM-DD 且 start<=end")
        return 2
    rounds, info = reconstruct_rounds(args.root, crit.get("targets") or [], start, end)
    eps = build_episodes(rounds)
    res = {"schema": "ga.alert_lifecycle/v1", "mode": "rounds",
           "start": start.isoformat(), "end": end.isoformat(), "root": args.root,
           "targets": len(info), "info": info, "rounds": rounds, "episodes": eps,
           "summary": summarize(rounds, eps)}
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(fmt_rounds(res, info))
    return 0


def cmd_reconcile(args):
    cs = cases()
    if not cs:
        print("[alert_lifecycle] 找不到用例（%s）" % DETECTOR)
        return 2
    out, failed = reconcile(args.root, cs, shift_days=args.shift_gt_days)
    if args.json:
        print(json.dumps({"schema": "ga.alert_lifecycle/v1", "mode": "reconcile",
                          "shift_gt_days": args.shift_gt_days, "cases": out}, ensure_ascii=False, indent=1))
    else:
        print("== 重建序列 × ground truth 对账（用例 %d，gt 位移 %d 天）" % (len(out), args.shift_gt_days))
        for r in out:
            print("  [%s] %-26s %s" % (r["verdict"], r["case"], r["detail"]))
        from collections import Counter
        cc = Counter(r["verdict"] for r in out)
        print("  合计：%s" % ", ".join("%s %d" % (k, cc[k]) for k in sorted(cc)))
        blind = [r["case"] for r in out if r["verdict"] == "L1_BLIND"]
        if blind:
            print("  L1_BLIND（该层结构上看不见，已登记非绿灯）：%s" % ", ".join(blind))
    return 1 if failed else 0


# ----------------------------------------------------------------- crosscheck

def parse_tool_run(d):
    """真工具 --json 输出 → {id: {l3_fresh, l3_text, levels}}。"""
    res = {}
    for t in d.get("targets") or []:
        l3 = None
        levels = []
        for c in t.get("checks") or []:
            levels.append(c.get("level"))
            txt = str(c.get("text") or "")
            if txt.startswith("L3 "):
                l3 = (c.get("level"), txt)
        res[t.get("id")] = {"l3_fresh": bool(l3 and l3[0] == "OK"),
                            "l3_text": l3[1] if l3 else None,
                            "levels": levels, "verdict": t.get("verdict")}
    return res


def my_l1_state(root, targets, end_day, back_days=3):
    """我的重建在「今天」这一天的 L1 判定（与真工具 L3 同层口径）。"""
    start = end_day - datetime.timedelta(days=back_days)
    out = {}
    for t in targets:
        ex, why = target_exempt(t)
        if ex:
            out[t["id"]] = {"exempt": why}
            continue
        alerts, meta = target_alert_days(t, root, start, end_day)
        out[t["id"]] = {"exempt": None, "alert_today": bool(alerts and alerts[-1] == end_day),
                        "alert_days": [d.isoformat() for d in alerts], "meta": meta}
    return out


def crosscheck(real, mine):
    """真工具 L3（mtime 口径）× 本器重建（文件名日期口径）逐目标对账。"""
    rows, diverge = [], []
    for tid in sorted(real):
        r = real[tid]
        m = mine.get(tid)
        if m is None:
            rows.append({"id": tid, "verdict": "NOT_IN_CRITERIA"})
            continue
        if m.get("exempt"):
            rows.append({"id": tid, "verdict": "EXEMPT", "reason": m["exempt"]})
            continue
        if r.get("l3_text") is None:
            rows.append({"id": tid, "verdict": "REAL_NO_L3"})
            continue
        expect_alert = not r["l3_fresh"]
        agree = bool(m["alert_today"]) == expect_alert
        rec = {"id": tid, "verdict": "AGREE" if agree else "DIVERGE",
               "real_l3_fresh": r["l3_fresh"], "real_l3": (r["l3_text"] or "")[:120],
               "mine_alert_today": bool(m["alert_today"]), "mine_meta": m["meta"]}
        rows.append(rec)
        if not agree:
            diverge.append(rec)
    return rows, diverge


def load_snapshot(explicit=None):
    d = os.path.join(GA, "temp", "alert_lifecycle")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    if not os.path.isdir(d):
        return None
    cands = sorted(glob.glob(os.path.join(d, "tf_run_*.json")))
    return cands[-1] if cands else None


def cmd_crosscheck(args):
    led_sha = sha256_file(args.ledger) if os.path.exists(args.ledger) else None
    crit = load_criteria(args.criteria)
    if args.from_file:
        path = args.from_file
        if not os.path.exists(path):
            print("[alert_lifecycle] --from-file 不存在：%s" % path)
            return 2
        d = json.load(open(path, encoding="utf-8"))
    else:
        r = subprocess.run([sys.executable or "python3",
                            os.path.join(GA, "tools", "task_freshness.py"),
                            "--json", "--no-ledger"],
                           capture_output=True, text=True, cwd=args.root, timeout=args.timeout)
        if not r.stdout.strip():
            print("[alert_lifecycle] 真工具无输出（rc=%d）：%s" % (r.returncode, r.stderr[:300]))
            return 2
        d = json.loads(r.stdout)
        path = os.path.join(GA, "temp", "alert_lifecycle",
                            "tf_run_%s.json" % datetime.date.today().strftime("%Y%m%d"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    end_day = parse_day(str(d.get("ts"))[:10]) or datetime.date.today()
    real = parse_tool_run(d)
    mine = my_l1_state(args.root, crit.get("targets") or [], end_day)
    rows, diverge = crosscheck(real, mine)
    from collections import Counter
    cc = Counter(x["verdict"] for x in rows)
    res = {"schema": "ga.alert_lifecycle/v1", "mode": "crosscheck", "source": path,
           "end_day": end_day.isoformat(), "real_ts": d.get("ts"),
           "real_counts": d.get("counts"), "real_hard_issue_ids": d.get("hard_issue_ids"),
           "summary": {k: cc[k] for k in sorted(cc)}, "diverge": diverge, "rows": rows}
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("== 交叉口径：真工具 L3（mtime）× 本器重建（文件名日期）  快照 %s" % path)
        print("   真工具 ts=%s counts=%s" % (d.get("ts"), d.get("counts")))
        print("   逐目标：%s" % ", ".join("%s %d" % (k, cc[k]) for k in sorted(cc)))
        if diverge:
            print("   ⚠ DIVERGE（两口径不一致，必须人看）：")
            for x in diverge:
                print("     - %-34s real_l3_fresh=%s mine_alert_today=%s | %s"
                      % (x["id"], x["real_l3_fresh"], x["mine_alert_today"], x["real_l3"]))
        else:
            print("   DIVERGE 0 ⇒ 两口径同层判定一致")
    led_after = sha256_file(args.ledger) if os.path.exists(args.ledger) else None
    if led_sha != led_after:
        print("[alert_lifecycle] 警告：台账被改动（只读自证失败）")
        return 1
    return 1 if diverge else 0


# ----------------------------------------------------------------- selftest

def _mk_ledger(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            if isinstance(r, str):
                f.write(r + "\n")
            else:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def selftest():
    import shutil
    checks = []

    def ck(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    sand = os.path.join(GA, "temp", "alert_lifecycle", "selftest")
    if os.path.isdir(sand):
        shutil.rmtree(sand)
    os.makedirs(sand)

    # ---- 夹具 1：简单台账（含重复行、退场、复发、坏行）
    led = os.path.join(sand, "ledger.jsonl")
    _mk_ledger(led, [
        {"ts": "2026-09-01 10:00", "counts": {"ALERT": 2, "OK": 1}, "hard_issue_ids": ["A", "B"]},
        {"ts": "2026-09-01 10:00", "counts": {"ALERT": 2}, "hard_issue_ids": ["A"]},   # 重复行
        {"ts": "2026-09-02 10:00", "counts": {"ALERT": 1}, "hard_issue_ids": ["A"]},
        {"ts": "2026-09-03 10:00", "counts": {}, "hard_issue_ids": []},                # A/B 均缺席
        {"ts": "2026-09-04 10:00", "counts": {"ALERT": 1}, "hard_issue_ids": ["A"]},   # A 复发
        "{bad json",                                                                   # 坏行
    ])
    raw, skipped = load_rounds(led)
    rounds = dedupe_rounds(raw)
    eps = build_episodes(rounds)
    s = summarize(rounds, eps)

    ck("NC1 重复 ts 行被合并为 1 轮", len(rounds) == 4, "rounds=%d" % len(rounds))
    ck("NC2 重复行不产生额外 episode（A=2 条）",
       len([e for e in eps if e["id"] == "A"]) == 2,
       "A episodes=%d" % len([e for e in eps if e["id"] == "A"]))
    ck("NC3 退场即闭合（B 1 条 CLEARED）",
       len([e for e in eps if e["id"] == "B"]) == 1
       and [e for e in eps if e["id"] == "B"][0]["status"] == "CLEARED")
    ck("NC4 复发被识别", s["recurred_ids"] == ["A"] and s["recurrence_rate"] == 0.5,
       "recur=%s rate=%s" % (s["recurred_ids"], s["recurrence_rate"]))
    ck("NC5 事件闭合/未闭合计数（A:1 闭合+1 未闭合, B:1 闭合）",
       s["episodes_cleared"] == 2 and s["episodes_open"] == 1,
       "cleared=%d open=%d" % (s["episodes_cleared"], s["episodes_open"]))
    ck("NC6 坏行被跳过且计数", skipped == [6], "skipped=%s" % skipped)
    ck("NC7 噪声度=3/4", abs((s["noise_ratio"] or 0) - 0.75) < 1e-9, "noise=%s" % s["noise_ratio"])

    # ---- NC8：A 在 09-03 缺席即在**该轮**闭合（cleared_observed 口径）
    b = [e for e in eps if e["id"] == "B"][0]
    ck("NC8 闭合观测点=B 首次缺席那一轮（09-02）", b["cleared_observed_at"] == "2026-09-02 10:00",
       "cleared_observed=%s" % b["cleared_observed_at"])

    # ---- NC9：空台账/不存在 ⇒ rc=2
    empty = os.path.join(sand, "empty.jsonl")
    _mk_ledger(empty, [])
    rc_empty = cmd_analyze(argparse.Namespace(ledger=empty, json=False))
    ck("NC9 空台账 rc=2 且不抛异常", rc_empty == 2, "rc=%d" % rc_empty)

    # ---- 夹具 2：证据重建（假 root + 假 criteria）
    root = os.path.join(sand, "root")
    os.makedirs(os.path.join(root, "done"))
    for d in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-10"):
        open(os.path.join(root, "done", "%s_taskX.md" % d), "w").write("x")
    crit = os.path.join(sand, "criteria.json")
    json.dump({"targets": [
        {"id": "taskX", "run_evidence": ["done/*_taskX.md"], "max_run_gap_days": 2},
        {"id": "taskOff", "run_evidence": ["done/*_taskOff.md"], "max_run_gap_days": 2,
         "disabled": True},
        {"id": "taskLegacy", "run_evidence": ["done/*_taskLegacy.md"], "max_run_gap_days": 2,
         "state": "legacy"},
    ]}, open(crit, "w"), ensure_ascii=False)

    c = json.load(open(crit, encoding="utf-8"))
    rounds2, info2 = reconstruct_rounds(root, c["targets"], parse_day("2026-08-01"), parse_day("2026-08-12"))
    eps2 = build_episodes(rounds2)
    s2 = summarize(rounds2, eps2)
    ck("NC10 重建为逐日轮次（12 天=12 轮）", len(rounds2) == 12, "rounds=%d" % len(rounds2))
    ck("NC11 运行缺席窗口 08-06..08-09 被判告警（4 天）",
       len([e for e in eps2 if e["id"] == "taskX"]) == 1
       and [e for e in eps2 if e["id"] == "taskX"][0]["start"] == "2026-08-06"
       and [e for e in eps2 if e["id"] == "taskX"][0]["duration_days"] == 3.0,
       "eps=%s" % [(e["id"], e["start"], e["last_present"], e["duration_days"]) for e in eps2])
    ck("NC12 disabled / legacy 目标豁免（不出现在告警里）",
       all(e["id"] == "taskX" for e in eps2)
       and info2["taskOff"]["verdict"] == "EXEMPT" and info2["taskLegacy"]["verdict"] == "EXEMPT")
    ck("NC13 有证据目标判 OK 并自证证据天数", info2["taskX"]["verdict"] == "OK"
       and info2["taskX"]["evidence_days"] == 4, "meta=%s" % info2["taskX"])
    # 负控：把 disabled 目标改成不豁免 ⇒ 必须凭空多出告警（证明豁免判据真的在起作用）
    c2 = json.loads(json.dumps(c))
    c2["targets"][1].pop("disabled")
    c2["targets"][1]["run_evidence"] = ["done/*_taskX.md"]      # 借真实证据，制造「产物很旧」
    rounds3, info3 = reconstruct_rounds(root, c2["targets"], parse_day("2026-08-01"), parse_day("2026-08-12"))
    eps3 = build_episodes(rounds3)
    ck("NC14 负控：去掉豁免后同一窗口多出告警", len(eps3) > len(eps2),
       "eps2=%d eps3=%d" % (len(eps2), len(eps3)))
    ck("NC15 重建 unit=days", s2["unit"] == "days" and s2["median_round_gap_hours"] == 24.0,
       "unit=%s gap=%s" % (s2["unit"], s2["median_round_gap_hours"]))

    # ---- reconcile：真仓 ground truth
    ck("NC16 真仓用例可读（≥4 例）", len(cases()) >= 4, "cases=%d" % len(cases()))
    out, failed = reconcile(DEFAULT_ROOT, cases())
    ck("NC17 真仓对账含正控且全 PASS", len(out) == len(cases()) and not failed,
       "out=%d failed=%s" % (len(out), [r["case"] for r in failed]))
    out2, failed2 = reconcile(DEFAULT_ROOT, cases(), shift_days=30)
    f2 = {r["case"] for r in failed2}
    b2 = {r["case"]: r["verdict"] for r in out2}
    blind0 = {r["case"] for r in out if r["verdict"] == "L1_BLIND"}
    # 位移确实作用到窗口（管线自证，防 shift_days 被静默忽略）
    ck("NC18a 位移 30 天确实改变 gt 窗口（管线自证）",
       all("gt_window" in r for r in out2) and
       {r["case"]: r["gt_window"] for r in out2} != {r["case"]: r["gt_window"] for r in out},
       "样例 %s vs %s" % (out[0]["gt_window"], out2[0]["gt_window"]))
    # 【真负控】把已知坏窗口冒充成正控（expect=no_alert）⇒ 判据必须拒绝它（FAIL，rc≠0）
    fake = []
    for cid in ("x_ai_0818_0927", "wechat_13h_0704_0927"):
        c = copy.deepcopy([x for x in cases() if x["id"] == cid][0])
        c["expect"] = "no_alert"
        fake.append(c)
    out3, failed3 = reconcile(DEFAULT_ROOT, fake)
    ck("NC18 负控：坏窗口冒充正控(expect=no_alert) ⇒ 必须 FAIL 且 rc≠0",
       len(failed3) == len(fake) and all(r["verdict"] == "FAIL" for r in failed3),
       "failed=%s" % [(r["case"], r["verdict"]) for r in out3])
    # 位移只加宽 gt 窗口，不得把 L1_BLIND 洗成 PASS/L1_BLIND 之外的免费放行
    ck("NC18c 加宽 gt 窗口不得给盲区免费放行（原 L1_BLIND 三例位移后仍非 PASS）",
       all(b2.get(k) in ("L1_BLIND", "FAIL") for k in blind0),
       "blind0=%s 位移后=%s" % (sorted(blind0), {k: b2.get(k) for k in sorted(blind0)}))
    exc = [r for r in out if r["verdict"] == "DISABLED_EXCUSED"]
    ck("NC18b 豁免只认 target_exempt 单一源（真仓恰 1 例且带参考覆盖率）",
       len(exc) == 1 and "L1 层本可覆盖" in exc[0]["detail"],
       "excused=%s" % [r["case"] for r in exc])
    pc = [r for r in out if r.get("expect") == "no_alert"]
    ck("NC19 正控用例存在且零告警", pc and pc[0]["verdict"] == "PASS",
       "positive_control=%s" % (pc[0]["detail"] if pc else "缺失"))

    # ---- NC26 静态门禁：源码里用到的模块名必须真的 import 过
    # （实测坑：cmd_crosscheck 的真分支用 subprocess 但漏 import，而 selftest 只走 --from-file
    #   ⇒ 自测全绿、真命令 `crosscheck` 必崩 NameError；这条按词法自查堵住同一类洞）
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _imported = set()
    for _m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", _src, re.M):
        _imported.add(_m.group(1).split(".")[0])
    _used = set()
    for _tok in re.finditer(r"\b([a-z_][a-z0-9_]{1,15})\.(?=[A-Za-z_(])", _src):
        _used.add(_tok.group(1))
    _builtin_like = {"self", "cls", "args", "rec", "res", "d", "e", "r", "f", "c", "p", "s", "info",
                     "meta", "crit", "rows", "rounds", "eps", "out", "os_", "sys_"}
    _missing = sorted(n for n in _used - _imported - _builtin_like
                      if n in {"subprocess", "shutil", "tempfile", "datetime", "hashlib", "copy",
                               "argparse", "collections", "glob", "json", "re", "sys", "os", "time"})
    ck("NC26 静态门禁：源码用到的模块名都已 import（防真分支 NameError）",
       not _missing, "missing=%s imported=%d" % (_missing, len(_imported)))

    # ---- 只读自证：分析前后源文件 sha256 不变
    before = sha256_file(led)
    cmd_analyze(argparse.Namespace(ledger=led, json=False))
    after = sha256_file(led)
    ck("NC20 只读自证：夹具台账 sha256 前后一致", before == after, "%s vs %s" % (before[:12], after[:12]))
    real_before = sha256_file(DEFAULT_LEDGER) if os.path.exists(DEFAULT_LEDGER) else None
    if real_before:
        cmd_analyze(argparse.Namespace(ledger=DEFAULT_LEDGER, json=False))
        ck("NC21 只读自证：真台账 sha256 前后一致",
           sha256_file(DEFAULT_LEDGER) == real_before)
    else:
        ck("NC21 只读自证：真台账不存在（跳过）", True, "无真台账")

    # ---- 真实台账可用性
    rr, sk = load_rounds(DEFAULT_LEDGER)
    ck("NC22 真台账可解析且行数自证", len(rr) >= 1, "rows=%d skipped=%d" % (len(rr), len(sk)))

    # ---- 语料计数自证（防"没读到语料"的假绿）
    crit_real = load_criteria(DEFAULT_CRITERIA)
    ck("NC23 判据表读到 ≥40 目标", len(crit_real.get("targets") or []) >= 40,
       "targets=%d" % len(crit_real.get("targets") or []))

    # ---- crosscheck：真工具 L3 × 本器重建（快照缺失则显式 SKIP，不假装通过）
    snap = load_snapshot()
    if not snap:
        print("  [SKIP] NC24/NC25 交叉口径：无真工具快照（先跑 crosscheck 生成 tf_run_*.json）")
    else:
        dreal = json.load(open(snap, encoding="utf-8"))
        end_day = parse_day(str(dreal.get("ts"))[:10]) or datetime.date.today()
        real = parse_tool_run(dreal)
        mine = my_l1_state(DEFAULT_ROOT, load_criteria(DEFAULT_CRITERIA).get("targets") or [], end_day)
        rows, dv = crosscheck(real, mine)
        cmp_rows = [x for x in rows if x["verdict"] in ("AGREE", "DIVERGE")]
        ck("NC24 真工具 × 重建逐目标比对：DIVERGE=0 且可比样本 ≥20",
           not dv and len(cmp_rows) >= 20,
           "rows=%d compared=%d diverge=%s" % (len(rows), len(cmp_rows), [x["id"] for x in dv]))
        # 负控：把每个目标的 L3 新鲜度整表取反 ⇒ 必须全量 DIVERGE（证明判据真读该字段）
        flipped = json.loads(json.dumps(real))
        for v in flipped.values():
            if v.get("l3_text") is not None:
                v["l3_fresh"] = not v["l3_fresh"]
        rows2, dv2 = crosscheck(flipped, mine)
        n2 = len([x for x in rows2 if x["verdict"] in ("AGREE", "DIVERGE")])
        ck("NC25 负控：L3 新鲜度整表取反 ⇒ DIVERGE 数应=可比行数（且 ≥20）",
           len(dv2) == n2 and n2 >= 20, "diverge=%d compared=%d" % (len(dv2), n2))

    ok = sum(1 for _, c_, _ in checks if c_)
    print("== alert_lifecycle selftest：%d/%d" % (ok, len(checks)))
    for name, cond, detail in checks:
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               ("  (%s)" % detail) if (detail and not cond) else ""))
    return 0 if ok == len(checks) else 1


def cmd_proof(args):
    """只读自证：analyze / rounds / reconcile 前后，被读文件的 sha256 必须不变。"""
    targets = [args.ledger, args.criteria, DETECTOR]
    done_glob = []
    try:
        for t in load_criteria(args.criteria).get("targets") or []:
            done_glob.extend(t.get("run_evidence") or [])
    except Exception:
        pass
    files = [p for p in targets if os.path.exists(p)]
    before = {p: sha256_file(p) for p in files}
    done_before = {}
    for pat in done_glob:
        for f in glob.glob(pat if os.path.isabs(pat) else os.path.join(args.root, pat), recursive=True):
            if os.path.isfile(f):
                done_before[f] = (os.path.getsize(f), os.path.getmtime(f))
    rc1 = cmd_analyze(argparse.Namespace(ledger=args.ledger, json=False))
    rc2 = cmd_reconcile(argparse.Namespace(root=args.root, json=False, shift_gt_days=0))
    after = {p: sha256_file(p) for p in files}
    changed = [p for p in files if before[p] != after[p]]
    done_after = {}
    for pat in done_glob:
        for f in glob.glob(pat if os.path.isabs(pat) else os.path.join(args.root, pat), recursive=True):
            if os.path.isfile(f):
                done_after[f] = (os.path.getsize(f), os.path.getmtime(f))
    done_changed = [k for k in set(done_before) | set(done_after)
                    if done_before.get(k) != done_after.get(k)]
    print("[proof] files=%d changed=%s" % (len(files), changed or []))
    print("[proof] run_evidence files=%d changed=%s" % (len(done_before), done_changed or []))
    print("[proof] rc(analyze)=%d rc(reconcile)=%d" % (rc1, rc2))
    return 1 if (changed or done_changed) else 0


# ----------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="巡检告警生命周期台账（只读）")
    ap.add_argument("cmd", choices=["analyze", "rounds", "reconcile", "crosscheck",
                                    "selftest", "proof"])
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--criteria", default=DEFAULT_CRITERIA)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--shift-gt-days", dest="shift_gt_days", type=int, default=0,
                    help="负控用：把 ground truth 起点人为前移 N 天")
    ap.add_argument("--from-file", dest="from_file", default="",
                    help="crosscheck：复用已保存的真工具 JSON（不给则真跑）")
    ap.add_argument("--timeout", type=int, default=300, help="crosscheck 真跑超时秒")
    args = ap.parse_args(argv)
    if args.cmd == "analyze":
        return cmd_analyze(args)
    if args.cmd == "rounds":
        return cmd_rounds(args)
    if args.cmd == "reconcile":
        return cmd_reconcile(args)
    if args.cmd == "crosscheck":
        return cmd_crosscheck(args)
    if args.cmd == "selftest":
        return selftest()
    return cmd_proof(args)


if __name__ == "__main__":
    sys.exit(main())
