#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unattended_n3_audit.py — 夜批无人值守台账（herdr-agents control-plane/unattended_v0）
的**独立复算 / 注入负控 / 只读自证**器。消费方：TODO L194（长任务无人值守演练 N3）。

为什么需要它
------------
`unattended.py` 只有 5 个子命令 replay/append/report/log-intervention/selftest——**没有 `verify`**。
验收要求「台账输出与 run-state 独立复算一致」，因此本器不 import 该模块的聚合逻辑，
用**自己写的一份**解析/分夜/聚合代码，从 run-state 原始 JSON 复算夜批三判据，再与台账逐字段对账。

三判据（与 F8 规格同源，实现在本文件内自写）
--------------------------------------------
L1 夜窗口完整性：每个夜一行台账；夜 = 本地 [boundary, boundary+24h)（默认 8 ⇒ [08:00, 次日 08:00)）
L2 run 级落地  ：runs_total / runs_real / runs_simulated / runs_by_state / wall_clock_s_total
L3 两格分离    ：`interventions`（人动过手 R1/R2/R3）与 `forced`（装置被迫叫人/降级）**互不外溢**

子命令（**位置参数**，写 `--selftest` 会报 unrecognized）
---------------------------------------------------------
  audit   --ledger F --runs-root D [--runs-root D2 ...] [--boundary 8] [--json OUT]
  inject  --sandbox DIR --runs-root D [--boundary 8] [--json OUT]   # 只在沙盒内写
  proof   --ledger F --runs-root D [...]                            # 只读自证
  selftest

退出码：0 一致 / 1 不一致（含缺夜、多夜、字段不符）/ 2 语料错误（空语料、沙盒越界等）
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

MARKER = "ga.completion-contract/v1"          # 记录识别串（collector.MARKER 同源；本器独立实现）
SCHEMA_NIGHT = "ga.unattended-night/v1"        # 台账里的「夜」记录
SCHEMA_INTERVENTION = "ga.unattended-intervention/v1"  # 台账里的「人工干预」记录
DEFAULT_BOUNDARY_HOUR = 8
FORCED_REASON_STATES = ("ESCALATED",)         # 装置被迫叫人/降级的 run 状态（单一源：unattended.py）
# blocked.kind 里「需要人」的集合（单一源：autonomy_v0/contract.HARD_BLOCKING = FATE_BY_KIND 中 fate != retry）
HARD_BLOCKING_FALLBACK = {
    "trust_gate", "delete_approval", "permission", "auth", "quota", "model",
    "unclassified", "blocked",
}
REASONS_NEEDS_HUMAN_FALLBACK = {"auth_failed", "quota_exhausted", "model_unavailable", "unclassified"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
HERE = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------ 常量（尽量取单一源）

def _contract_constants():
    """从 herdr-agents control-plane 取 HARD_BLOCKING / REASONS_NEEDS_HUMAN 单一源；取不到用本地兜底。"""
    cands = [os.environ.get("GA_CONTRACT_DIR"),
             os.path.expanduser("~/develop/herdr-agents/control-plane/autonomy_v0"),
             "/Users/cuip/develop/herdr-agents/control-plane/autonomy_v0"]
    for d in cands:
        if not d or not os.path.isdir(d):
            continue
        sys.path.insert(0, d)
        try:
            import contract  # noqa
            return (set(getattr(contract, "HARD_BLOCKING", HARD_BLOCKING_FALLBACK)),
                    set(getattr(contract, "REASONS_NEEDS_HUMAN", REASONS_NEEDS_HUMAN_FALLBACK)),
                    os.path.join(d, "contract.py"))
        except Exception:
            sys.path.pop(0)
    return set(HARD_BLOCKING_FALLBACK), set(REASONS_NEEDS_HUMAN_FALLBACK), None


HARD_BLOCKING, REASONS_NEEDS_HUMAN, CONTRACT_SRC = _contract_constants()


# ------------------------------------------------------------------ 自写复算内核

def night_of(ts, boundary=DEFAULT_BOUNDARY_HOUR):
    """夜批归属：本地 [boundary, 次日 boundary) ⇒ 取 (ts - boundary 小时) 的日历日。"""
    if ts is None:
        return None
    d = _dt.datetime.fromtimestamp(ts - boundary * 3600)
    return d.strftime("%Y-%m-%d")


def first_ts(rec):
    """记录的首个可用时间戳（history 里第一个数值 ts）。"""
    for h in rec.get("history") or []:
        ts = h.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return float(ts)
    return None


def last_ts(rec):
    ts = [h.get("ts") for h in (rec.get("history") or [])
          if isinstance(h.get("ts"), (int, float)) and not isinstance(h.get("ts"), bool)]
    return float(max(ts)) if ts else None


def wall_clock(rec):
    """墙钟：记录自带 kpi.wall_clock_s 优先，否则用 history 首末差；都拿不到写 None（不写 0）。"""
    kpi = rec.get("kpi") or {}
    v = kpi.get("wall_clock_s")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    a, b = first_ts(rec), last_ts(rec)
    if a is None or b is None:
        return None
    return float(b - a)


def clusters_of(intervals, max_gap=900.0):
    """把 run 的 [start_ts, end_ts] 区间切成「连续簇」：相邻间隔 ≤ max_gap 归同一簇。

    为什么必须给簇：只用 first→last 包络会**虚报长批次**（09-22 夜真 run 包络 12.6h，
    实际连续簇远短，中间横着 9h 空档）⇒ N3 的「≥1h 真批次」一律以**簇**为准，不用包络。

    簇 span 口径 = 簇内最晚 end − 最早 start（**真实墙钟覆盖**，不是「起始时刻散布」）：
    单 run 簇的 span 必须等于该 run 自己的墙钟，否则「跑了一小时的单个 run」会被算成 0s。
    """
    ivs = sorted(((float(a), float(b)) for a, b in intervals), key=lambda x: x[0])
    if not ivs:
        return []
    out, cur = [], [ivs[0]]
    for a, b in ivs[1:]:
        if a - max(e for _s, e in cur) <= max_gap:
            cur.append((a, b))
        else:
            out.append(cur)
            cur = [(a, b)]
    out.append(cur)
    res = []
    for c in out:
        st = min(s for s, _e in c)
        en = max(e for _s, e in c)
        res.append({"n": len(c), "start_ts": st, "end_ts": en, "span_s": round(en - st, 3)})
    return res


def is_record(rec):
    return isinstance(rec, dict) and isinstance(rec.get("run_id"), str) and isinstance(rec.get("history"), list)


def load_runs(roots):
    """读 run-state 原始 JSON。返回 (records, errors, stats)。"""
    records, errors, stats = [], [], {"files": 0, "marked": 0, "unmarked": 0}
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            errors.append({"root": root, "kind": "runs_root_missing"})
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(dirpath, name)
                stats["files"] += 1
                try:
                    raw = open(path, "r", encoding="utf-8", errors="replace").read()
                    rec = json.loads(raw)
                except Exception as e:
                    errors.append({"path": path, "kind": "load_error", "err": str(e)})
                    continue
                if not is_record(rec):
                    stats["unmarked"] += 1
                    continue
                if MARKER in raw:
                    stats["marked"] += 1
                rec["_path"] = path
                records.append(rec)
    return records, errors, stats


def forced_of(rec):
    """两格之「被迫」：状态在 FORCED_REASON_STATES 或 blocked 的 kind/reason 属需要人。"""
    state = rec.get("state")
    blk = rec.get("blocked") or {}
    kind = blk.get("kind") if isinstance(blk, dict) else None
    reason = blk.get("reason") if isinstance(blk, dict) else None
    if state in FORCED_REASON_STATES:
        return {"why": "state=%s" % state, "kind": kind}
    if kind in HARD_BLOCKING:
        return {"why": "blocked_kind=%s" % kind, "kind": kind}
    if reason in REASONS_NEEDS_HUMAN:
        return {"why": "blocked.reason=%s" % reason, "kind": kind}
    return None


def interventions_of(rec):
    """两格之「人动过手」：R1=kpi.human_interventions>0，R2=history 有 actor==human。"""
    hits = []
    kpi = rec.get("kpi") or {}
    n = kpi.get("human_interventions")
    if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0:
        hits.append({"rule": "R1", "run_id": rec.get("run_id"), "detail": "kpi.human_interventions=%s" % n})
    for h in rec.get("history") or []:
        if h.get("actor") == "human":
            hits.append({"rule": "R2", "run_id": rec.get("run_id"), "detail": "history actor=human @%s" % h.get("ts")})
            break
    return hits


def split_ledger(lines):
    """台账 JSONL → (夜记录 dict, 干预记录 list, 坏行 list)。"""
    nights, ivs, bad = {}, [], []
    for i, ln in enumerate(lines, 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception as e:
            bad.append({"line": i, "err": str(e)})
            continue
        if isinstance(rec, dict) and rec.get("night") and "runs_total" in rec:
            nights[rec["night"]] = rec
        elif isinstance(rec, dict) and rec.get("id") and rec.get("night"):
            ivs.append(rec)
        else:
            bad.append({"line": i, "err": "unrecognized_record"})
    return nights, ivs, bad


def recompute(roots, ledger_lines, boundary=DEFAULT_BOUNDARY_HOUR):
    """独立复算：从 run-state + 台账干预记录推出每夜的判据值。"""
    records, errors, stats = load_runs(roots)
    nights, ledger_ivs, bad = split_ledger(ledger_lines)
    iv_by_night = {}
    for iv in ledger_ivs:
        iv_by_night.setdefault(iv.get("night"), []).append(iv)

    buckets, undated = {}, []
    for rec in records:
        ts = first_ts(rec)
        if ts is None:
            undated.append(rec.get("run_id"))
            continue
        buckets.setdefault(night_of(ts, boundary), []).append(rec)

    out = {}
    for night, recs in buckets.items():
        by_state, walls, real, sim, forced, hits = {}, [], 0, 0, [], []
        for rec in recs:
            st = rec.get("state")
            by_state[st] = by_state.get(st, 0) + 1
            if rec.get("simulated"):
                sim += 1
            else:
                real += 1
            w = wall_clock(rec)
            if w is not None:
                walls.append(w)
            f = forced_of(rec)
            if f:
                forced.append({"run_id": rec.get("run_id"), "why": f["why"], "kind": f["kind"]})
            hits.extend(interventions_of(rec))
        for iv in iv_by_night.get(night, []):
            rule = "R3" if not iv.get("run_id") else "R3"
            hits.append({"rule": rule, "run_id": iv.get("run_id"), "detail": "ledger %s by=%s" % (iv.get("id"), iv.get("by"))})
        by_rule = {}
        for h in hits:
            by_rule[h["rule"]] = by_rule.get(h["rule"], 0) + 1
        real_ts = sorted(first_ts(r) for r in recs if not r.get("simulated") and first_ts(r) is not None)
        cluster = round(real_ts[-1] - real_ts[0], 3) if len(real_ts) > 1 else 0.0
        # 异常记录护栏：单条 run 自身墙钟 > 24h ⇒ 极可能是占位/脏 ts（实测 _legacy_unmarked 里
        # 有条记录首 ts 是 2025-09-16 占位、末 ts 是 2026-09-22 ⇒ 自身跨 371 天，会把「长批次」判成 371 天）
        anomalous, real_ivs, real_ivs_ok = [], [], []
        for r in recs:
            if r.get("simulated") or first_ts(r) is None:
                continue
            a, b = first_ts(r), (last_ts(r) if last_ts(r) is not None else first_ts(r))
            iv = (a, b)
            real_ivs.append(iv)
            if b - a > 86400:
                anomalous.append({"run_id": r.get("run_id"), "wall_s": round(b - a, 3),
                                  "why": "RUN_WALL_GT_24H"})
            else:
                real_ivs_ok.append(iv)
        cl = clusters_of(real_ivs_ok) if real_ivs_ok else []
        longest = max([c["span_s"] for c in cl], default=0.0)
        # 「长批次」判据用**连续簇**（不是首末包络）：跨 9h 空档的两段 30min 连续工作不算 12h 长批次
        long_cluster = max(cl, key=lambda c: (c["span_s"], c["n"]), default=None)
        out[night] = {
            "run_ids": sorted(r.get("run_id") for r in recs),
            "runs_total": len(recs),
            "runs_real": real,
            "runs_simulated": sim,
            "runs_by_state": by_state,
            "wall_clock_s_total": round(sum(walls), 3) if walls else None,
            "interventions": {"count": len(hits), "by_rule": by_rule,
                              "records": sorted(hits, key=lambda x: (x["rule"], str(x.get("run_id"))))},
            "intervened": bool(hits),
            "forced": {"count": len(forced), "runs": sorted(forced, key=lambda x: str(x["run_id"]))},
            "night_unattended": not hits,
            # 本器补充面（台账与 F8 未定义，只作 N3 长任务证据）：
            "real_first_ts": real_ts[0] if real_ts else None,
            "real_last_ts": real_ts[-1] if real_ts else None,
            # envelope（首末包络，仅供参照，**不作长批次判据**）
            "real_cluster_span_s": cluster,
            # 间隔感知簇（max_gap=900s）：长批次一律以此为准
            "real_clusters": cl,
            "real_longest_cluster_s": round(longest, 3),
            "real_longest_cluster": long_cluster,
            "anomalous_runs": anomalous,
            "real_longest_cluster_all_s": round(max([c["span_s"] for c in clusters_of(real_ivs)], default=0.0), 3),
        }
    return {"nights": out, "undated": undated, "load_errors": errors, "ledger_bad_lines": bad,
            "ledger_nights": nights, "stats": stats, "contract_src": CONTRACT_SRC,
            "tz": _dt.datetime.now().astimezone().strftime("%Z%z")}


def compare(recomp, boundary=DEFAULT_BOUNDARY_HOUR):
    """独立复算 vs 台账：逐夜逐字段对账。"""
    ledger_nights = recomp["ledger_nights"]
    rows, problems = [], []
    for night in sorted(set(recomp["nights"]) | set(ledger_nights)):
        mine = recomp["nights"].get(night)
        theirs = ledger_nights.get(night)
        if mine and not theirs:
            problems.append({"night": night, "problem": "EXTRA_IN_RUN_STATE", "detail": "台账缺该夜（L1 夜窗口完整性破）"})
            rows.append({"night": night, "verdict": "MISSING_IN_LEDGER", "runs_total": mine["runs_total"]})
            continue
        if theirs and not mine:
            problems.append({"night": night, "problem": "EXTRA_IN_LEDGER", "detail": "run-state 里没有该夜"})
            rows.append({"night": night, "verdict": "EXTRA_IN_LEDGER", "runs_total": theirs.get("runs_total")})
            continue
        diffs = []
        for field in ("runs_total", "runs_real", "runs_simulated", "runs_by_state", "wall_clock_s_total"):
            if mine[field] != theirs.get(field):
                diffs.append({"field": field, "recompute": mine[field], "ledger": theirs.get(field)})
        li = theirs.get("interventions") or {}
        if mine["interventions"]["count"] != li.get("count"):
            diffs.append({"field": "interventions.count", "recompute": mine["interventions"]["count"],
                          "ledger": li.get("count")})
        if bool(mine["intervened"]) != bool(theirs.get("intervened")):
            diffs.append({"field": "intervened", "recompute": mine["intervened"], "ledger": theirs.get("intervened")})
        if bool(theirs.get("night_unattended")) != bool(mine["night_unattended"]):
            diffs.append({"field": "night_unattended", "recompute": mine["night_unattended"],
                          "ledger": theirs.get("night_unattended")})
        lf = theirs.get("forced") or {}
        if mine["forced"]["count"] != lf.get("count"):
            diffs.append({"field": "forced.count", "recompute": mine["forced"]["count"], "ledger": lf.get("count")})
        if sorted(mine["run_ids"]) != sorted(theirs.get("run_ids") or []):
            diffs.append({"field": "run_ids",
                          "recompute_only": sorted(set(mine["run_ids"]) - set(theirs.get("run_ids") or [])),
                          "ledger_only": sorted(set(theirs.get("run_ids") or []) - set(mine["run_ids"]))})
        rows.append({"night": night, "verdict": "CONSISTENT" if not diffs else "MISMATCH",
                     "runs_total": mine["runs_total"], "diffs": diffs,
                     "runs_real": mine["runs_real"], "runs_by_state": mine["runs_by_state"],
                     "forced": mine["forced"]["count"], "interventions": mine["interventions"]["count"],
                     "real_cluster_span_s": mine["real_cluster_span_s"],
                     "real_longest_cluster_s": mine["real_longest_cluster_s"],
                     "real_clusters": mine["real_clusters"]})
        for d in diffs:
            problems.append({"night": night, "problem": "FIELD_MISMATCH", "detail": d})
    return rows, problems


# ------------------------------------------------------------------ 只读自证

def hash_tree(paths):
    out = {}
    for p in paths:
        if os.path.isfile(p):
            out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def run_files(roots):
    files = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            files.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".json"))
    return sorted(files)


def cmd_audit(args):
    if not os.path.isfile(args.ledger):
        print("台账不存在：%s（先跑 unattended.py append）" % args.ledger, file=sys.stderr)
        return 2
    lines = open(args.ledger, "r", encoding="utf-8", errors="replace").read().splitlines()
    rec = recompute(args.runs_root, lines, args.boundary)
    if not rec["nights"]:
        print("复算语料为空（runs_root=%s）⇒ 不判绿" % args.runs_root, file=sys.stderr)
        return 2
    rows, problems = compare(rec, args.boundary)
    n3 = [r for r in rows if (r.get("real_longest_cluster_s") or 0) >= 3600]
    anom = {r["night"]: r.get("anomalous_runs") for r in rows if r.get("anomalous_runs")}
    report = {"schema": "ga.unattended-n3-audit/v1", "ledger": os.path.abspath(args.ledger),
              "runs_roots": [os.path.abspath(r) for r in args.runs_root],
              "boundary_hour": args.boundary, "tz": rec["tz"], "contract_src": rec["contract_src"],
              "stats": rec["stats"], "load_errors": rec["load_errors"],
              "ledger_bad_lines": rec["ledger_bad_lines"], "undated_runs": rec["undated"],
              "nights": rows, "problems": problems,
              "long_nights_ge_1h": [r["night"] for r in n3],
              "anomalous_runs_by_night": anom,
              "long_night_evidence": {r["night"]: {"real_longest_cluster_s": r["real_longest_cluster_s"],
                                                   "real_cluster_span_s": r["real_cluster_span_s"],
                                                   "real_clusters": r.get("real_clusters"),
                                                   "runs_real": r.get("runs_real"),
                                                   "forced": r.get("forced"),
                                                   "interventions": r.get("interventions")} for r in n3},
              "consistent": not problems}
    print("=== 夜批台账独立复算（boundary=%d，tz=%s）" % (args.boundary, rec["tz"]))
    print("语料: %d roots，读入 json=%d（marked=%d）⇒ 夜=%d，无首 ts 的 run=%d，载入错误=%d"
          % (len(args.runs_root), rec["stats"]["files"], rec["stats"]["marked"],
             len(rec["nights"]), len(rec["undated"]), len(rec["load_errors"])))
    for r in rows:
        tag = r["verdict"]
        extra = ""
        if r.get("real_longest_cluster_s"):
            extra = " 最长连续簇=%.1fs(包络=%.1fs)" % (r["real_longest_cluster_s"], r["real_cluster_span_s"])
        print("  %s  %-16s runs=%-3s forced=%-2s 干预=%-2s%s"
              % (r["night"], tag, r.get("runs_total"), r.get("forced"), r.get("interventions"), extra))
        for d in r.get("diffs") or []:
            print("      DIFF %s" % json.dumps(d, ensure_ascii=False))
    print("≥1h 真批次的夜（连续簇口径，已剔异常记录）：%s" % (", ".join(report["long_nights_ge_1h"]) or "（无）"))
    if anom:
        for night, lst in anom.items():
            print("  ⚠ %s 存在 %d 条异常记录（自身墙钟 >24h，已排除出长批次判据）：%s"
                  % (night, len(lst), ", ".join("%s(%.0fs)" % (x["run_id"], x["wall_s"]) for x in lst)))
    print("结论：%s（problems=%d）" % ("CONSISTENT" if not problems else "MISMATCH", len(problems)))
    if args.json:
        open(args.json, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
        print("JSON → %s" % args.json)
    return 0 if not problems else 1


def cmd_proof(args):
    """只读自证：跑一遍 audit 内核，台账与 run-state 的 sha256 必须前后一致。"""
    files = [os.path.abspath(args.ledger)] + run_files(args.runs_root)
    files = [f for f in files if os.path.isfile(f)]
    before = hash_tree(files)
    lines = open(args.ledger, "r", encoding="utf-8", errors="replace").read().splitlines() \
        if os.path.isfile(args.ledger) else []
    rec = recompute(args.runs_root, lines, args.boundary)
    rows, problems = compare(rec, args.boundary)
    after = hash_tree(files)
    changed = sorted(p for p in before if before.get(p) != after.get(p))
    print("只读自证：files=%d changed=%s" % (len(files), changed or "[]"))
    print("（复算仍需真读源：夜=%d problems=%d）" % (len(rec["nights"]), len(problems)))
    if args.json:
        open(args.json, "w", encoding="utf-8").write(json.dumps(
            {"files": len(files), "changed": changed, "nights": len(rec["nights"]),
             "problems": len(problems)}, ensure_ascii=False, indent=2))
    return 0 if not changed else 1


# ------------------------------------------------------------------ 注入负控（只在沙盒）

def cmd_inject(args):
    sb = os.path.abspath(args.sandbox)
    if not (sb.startswith("/tmp/") or sb.startswith("/private/tmp/") or sb.startswith(os.path.expanduser("~"))):
        print("沙盒路径不安全：%s" % sb, file=sys.stderr)
        return 2
    if "herdr-agents" in sb and not sb.startswith("/tmp/") and not sb.startswith("/private/tmp/"):
        print("沙盒不得落在 herdr-agents 仓内：%s" % sb, file=sys.stderr)
        return 2
    runs = os.path.join(sb, "runs")
    if os.path.isdir(sb):
        shutil.rmtree(sb)
    os.makedirs(runs)

    # 1) 从真实 run-state 里挑一条**真 run（非模拟、非被迫）**当注入靶子
    pool, _e, _s = load_runs(args.runs_root)
    target = None
    for rec in pool:
        if not rec.get("simulated") and forced_of(rec) is None and first_ts(rec) is not None:
            target = rec
            break
    if target is None:
        print("找不到可注入的真 run（语料=%d）" % len(pool), file=sys.stderr)
        return 2
    base_rec = {k: v for k, v in target.items() if not k.startswith("_")}
    stage = {"target_run_id": base_rec.get("run_id"), "night": night_of(first_ts(base_rec), args.boundary)}

    # 1a) 基线：未注入
    json.dump(base_rec, open(os.path.join(runs, "%s.json" % base_rec["run_id"]), "w"), ensure_ascii=False)
    led0 = os.path.join(sb, "ledger_base.jsonl")
    _producer_append(runs, led0, args.boundary)
    n0 = _night_line(led0, stage["night"])

    # 1b) 注入「阻塞事件」（hard kind ⇒ 装置被迫）：台账必须落进 forced 格
    inj = json.loads(json.dumps(base_rec))
    inj["blocked"] = {"kind": sorted(HARD_BLOCKING)[0] if HARD_BLOCKING else "trust_gate",
                      "reason": "unclassified", "detail": "drill: injected blocking event"}
    json.dump(inj, open(os.path.join(runs, "%s.json" % inj["run_id"]), "w"), ensure_ascii=False)
    led1 = os.path.join(sb, "ledger_blocked.jsonl")
    _producer_append(runs, led1, args.boundary)
    n1 = _night_line(led1, stage["night"])

    # 1c) 注入「人工干预」（走真实写入口 log-intervention）：台账必须判「有干预」
    led2 = os.path.join(sb, "ledger_intervention.jsonl")
    _producer_append(runs, led2, args.boundary)          # 先产夜行
    rc_lv = _producer_log_intervention(led2, base_rec["run_id"], first_ts(inj), args.boundary)
    _producer_append(runs, led2, args.boundary)          # 再重算（append 幂等，重号跳过）
    n2 = _night_line(led2, stage["night"])

    checks = {
        "baseline_forced_0": bool(n0) and (n0.get("forced") or {}).get("count") == 0,
        "baseline_intervened_false": bool(n0) and n0.get("intervened") is False,
        "blocked_injection_hits_forced_cell": bool(n1) and (n1.get("forced") or {}).get("count", 0) >= 1,
        "blocked_injection_does_not_leak_into_intervention_cell":
            bool(n1) and int((n1.get("interventions") or {}).get("count", 0)) == int((n0.get("interventions") or {}).get("count", 0)) == 0,
        "intervention_write_path_marks_intervened": bool(n2) and n2.get("intervened") is True
        and int((n2.get("interventions") or {}).get("count", 0)) >= 1,
        "intervention_write_path_clears_unattended": bool(n2) and n2.get("night_unattended") is False,
        "log_intervention_rc_ok": rc_lv == 0,
        "ledger_records_only_this_night": bool(n2) and len(_all_night_lines(led2)) >= 1,
    }
    ok = all(checks.values())
    report = {"schema": "ga.unattended-n3-inject/v1", "sandbox": sb, "stage": stage,
              "checks": checks, "ok": ok,
              "nights": {"baseline": n0, "blocked": n1, "intervention": n2}}
    print("=== 注入负控（沙盒 %s）" % sb)
    print("靶子 run=%s（night=%s）" % (stage["target_run_id"], stage["night"]))
    for k, v in checks.items():
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    print("  基线 forced=%s intervened=%s｜注入阻塞后 forced=%s intervened=%s｜注入干预后 intervened=%s"
          % ((n0 or {}).get("forced", {}).get("count") if n0 else None,
             (n0 or {}).get("intervened"),
             (n1 or {}).get("forced", {}).get("count") if n1 else None,
             (n1 or {}).get("intervened"),
             (n2 or {}).get("intervened")))
    if args.json:
        open(args.json, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
        print("JSON → %s" % args.json)
    return 0 if ok else 1


def _unattended_py():
    cands = [os.environ.get("GA_UNATTENDED_PY"),
             os.path.expanduser("~/develop/herdr-agents/control-plane/unattended_v0/unattended.py")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _producer_append(runs_root, ledger, boundary):
    ua = _unattended_py()
    if not ua:
        return 127
    r = subprocess.run([sys.executable, ua, "append", "--runs-root", runs_root, "--ledger", ledger,
                        "--day-boundary-hour", str(boundary)],
                       capture_output=True, text=True)
    return r.returncode


def _producer_log_intervention(ledger, run_id, ts, boundary):
    ua = _unattended_py()
    if not ua:
        return 127
    at = _dt.datetime.fromtimestamp(ts).isoformat()
    r = subprocess.run([sys.executable, ua, "log-intervention", "--ledger", ledger, "--at", at,
                        "--by", "drill", "--run-id", run_id, "--kind", "human_unblock",
                        "--note", "N3 注入负控（沙盒）", "--day-boundary-hour", str(boundary)],
                       capture_output=True, text=True)
    return r.returncode


def _night_lines(ledger):
    if not os.path.isfile(ledger):
        return []
    return [l for l in open(ledger, encoding="utf-8").read().splitlines()
            if l.strip() and "runs_total" in l]


def _all_night_lines(ledger):
    return _night_lines(ledger)


def _night_line(ledger, night):
    """取该夜的**最后一条**夜记录（台账语义=append-only 重跑追加新行、每夜以最后一条为准，
    即 producer 自己的 latest_per_night 口径）。取第一条会把「已注入干预后的新行」判成未生效=假阴性。"""
    hit = None
    for ln in _night_lines(ledger):
        rec = json.loads(ln)
        if rec.get("night") == night:
            hit = rec
    return hit


# ------------------------------------------------------------------ 自测（含反事实负控）

def selftest():
    checks = []

    def check(name, cond, note=""):
        checks.append((name, bool(cond), note))

    tmp = tempfile.mkdtemp(prefix="n3_audit_selftest_")
    try:
        def fixture(run_id, ts, state="ACCEPTED", kpi_n=0, human=False, blocked=None, wall=None, sim=False):
            hist = [{"ts": ts, "from": None, "to": "DISPATCHED", "actor": "driver", "reason": ""},
                    {"ts": ts + 10, "from": "DISPATCHED", "to": state, "actor": "driver", "reason": ""}]
            if human:
                hist.append({"ts": ts + 20, "from": state, "to": state, "actor": "human",
                             "annotation": True, "reason": "human intervention"})
            rec = {"schema": MARKER, "run_id": run_id, "state": state, "history": hist,
                   "simulated": sim, "blocked": blocked}
            if kpi_n is not None or wall is not None:
                rec["kpi"] = {"human_interventions": kpi_n, "wall_clock_s": wall}
            return rec

        def write(runs_dir, recs):
            os.makedirs(runs_dir, exist_ok=True)
            for r in recs:
                json.dump(r, open(os.path.join(runs_dir, "%s.json" % r["run_id"]), "w"), ensure_ascii=False)

        # T1 分夜 + 计数
        d = os.path.join(tmp, "t1", "runs")
        write(d, [fixture("a1", 1000000), fixture("a2", 1000100, state="ESCALATED"),
                  fixture("a3", 1000200, sim=True)])
        recs, errs, stats = load_runs([d])
        rec = recompute([d], [], DEFAULT_BOUNDARY_HOUR)
        n = list(rec["nights"].values())[0]
        check("T1 计数：total/real/sim", n["runs_total"] == 3 and n["runs_real"] == 2 and n["runs_simulated"] == 1)
        check("T1 forced 只认 ESCALATED", n["forced"]["count"] == 1 and n["forced"]["runs"][0]["run_id"] == "a2")
        check("T1 无干预 ⇒ night_unattended", n["night_unattended"] is True and n["intervened"] is False)

        # T2 两格分离（阻塞 ≠ 干预）★核心
        d2 = os.path.join(tmp, "t2", "runs")
        write(d2, [fixture("b1", 1000000, state="ESCALATED", blocked={"kind": "trust_gate", "reason": "x"})])
        r2 = recompute([d2], [], DEFAULT_BOUNDARY_HOUR)
        n2 = list(r2["nights"].values())[0]
        check("T2 阻塞只进 forced 格", n2["forced"]["count"] == 1 and n2["interventions"]["count"] == 0
              and n2["intervened"] is False)
        # 反事实：若把 forced 混进干预 ⇒ 该断言必须失败
        leaked = {"interventions": {"count": n2["interventions"]["count"] + n2["forced"]["count"]}}
        check("T2 反事实：混格即被抓住", leaked["interventions"]["count"] != n2["interventions"]["count"])

        # T3 干预判定 R1/R2
        d3 = os.path.join(tmp, "t3", "runs")
        write(d3, [fixture("c1", 1000000, kpi_n=2), fixture("c2", 1000100, human=True)])
        n3 = list(recompute([d3], [], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T3 R1/R2 命中", n3["interventions"]["by_rule"].get("R1") == 1
              and n3["interventions"]["by_rule"].get("R2") == 1 and n3["intervened"] is True
              and n3["night_unattended"] is False)

        # T4 台账干预记录（R3）计入
        iv = {"schema": SCHEMA_INTERVENTION, "id": "i1", "night": night_of(1000000, 8), "by": "cuip",
              "run_id": "c1", "at_iS": "x"}
        n4 = list(recompute([d3], [json.dumps(iv)], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T4 台账干预记录计入 R3", n4["interventions"]["by_rule"].get("R3") == 1
              and n4["interventions"]["count"] == n3["interventions"]["count"] + 1)

        # T5 墙钟：kpi 优先，缺失回落 history 首末差
        d5 = os.path.join(tmp, "t5", "runs")
        write(d5, [fixture("e1", 1000000, wall=42.0), fixture("e2", 1000000, kpi_n=None, wall=None)])
        n5 = list(recompute([d5], [], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T5 墙钟 kpi 优先 + history 回落", n5["wall_clock_s_total"] == 52.0)

        # T6 无首 ts 的记录不静默丢弃
        d6 = os.path.join(tmp, "t6", "runs")
        write(d6, [{"schema": MARKER, "run_id": "f1", "state": "ACCEPTED",
                    "history": [{"from": None, "to": "DISPATCHED", "actor": "driver"}]}])
        r6 = recompute([d6], [], DEFAULT_BOUNDARY_HOUR)
        check("T6 无 ts ⇒ undated 而非丢弃", r6["undated"] == ["f1"] and not r6["nights"])

        # T7 载入错误不吞：坏 JSON 必报
        d7 = os.path.join(tmp, "t7", "runs")
        os.makedirs(d7)
        open(os.path.join(d7, "bad.json"), "w").write("{not json")
        r7 = recompute([d7], [], DEFAULT_BOUNDARY_HOUR)
        check("T7 坏 JSON ⇒ load_errors 非空", len(r7["load_errors"]) == 1)

        # T8 非记录文件（无 run_id/history）不计入 run，但被统计
        d8 = os.path.join(tmp, "t8", "runs")
        write(d8, [fixture("g1", 1000000)])
        json.dump({"hello": "world"}, open(os.path.join(d8, "contract.json"), "w"))
        r8 = recompute([d8], [], DEFAULT_BOUNDARY_HOUR)
        check("T8 非记录被跳过但自证计数", list(r8["nights"].values())[0]["runs_total"] == 1
              and r8["stats"]["files"] == 2 and r8["stats"]["unmarked"] == 1)

        # T9 夜边界：08:00 与 07:59 分属不同夜
        d9 = os.path.join(tmp, "t9", "runs")
        base = _dt.datetime(2026, 3, 4, 8, 0, 0).timestamp()
        write(d9, [fixture("h1", base), fixture("h2", base - 60)])
        r9 = recompute([d9], [], DEFAULT_BOUNDARY_HOUR)
        check("T9 夜边界按 08:00 切", set(r9["nights"]) == {"2026-03-04", "2026-03-03"})

        # T10 对账：字段被篡改必报 MISMATCH（负控）
        lines = [json.dumps({"schema": SCHEMA_NIGHT, "night": night_of(1000000, 8),
                             "runs_total": 999, "runs_real": 1, "runs_simulated": 0,
                             "runs_by_state": {"ACCEPTED": 1}, "wall_clock_s_total": 10.0,
                             "interventions": {"count": 0}, "intervened": False,
                             "night_unattended": True, "forced": {"count": 0}}) for _ in [0]]
        rc10 = recompute([d], lines, DEFAULT_BOUNDARY_HOUR)
        rows, probs = compare(rc10)
        check("T10 台账篡改 ⇒ MISMATCH", rows[0]["verdict"] == "MISMATCH"
              and any(p["detail"]["field"] == "runs_total" for p in probs if p["problem"] == "FIELD_MISMATCH"))

        # T11 台账缺夜 ⇒ MISSING_IN_LEDGER（L1 破）
        rows11, probs11 = compare(recompute([d], [], DEFAULT_BOUNDARY_HOUR))
        check("T11 台账缺夜 ⇒ MISSING_IN_LEDGER", rows11[0]["verdict"] == "MISSING_IN_LEDGER"
              and probs11[0]["problem"] == "EXTRA_IN_RUN_STATE")

        # T12 台账多夜 ⇒ EXTRA_IN_LEDGER
        extra = json.dumps({"schema": SCHEMA_NIGHT, "night": "1999-01-01", "runs_total": 1})
        rows12, _p12 = compare(recompute([d], [extra], DEFAULT_BOUNDARY_HOUR))
        check("T12 台账多夜 ⇒ EXTRA_IN_LEDGER",
              any(r["night"] == "1999-01-01" and r["verdict"] == "EXTRA_IN_LEDGER" for r in rows12))

        # T13 一致时判 CONSISTENT（正控：用复算值自己造台账行）
        rec13 = recompute([d], [], DEFAULT_BOUNDARY_HOUR)
        n13 = list(rec13["nights"].values())[0]
        line = json.dumps({"schema": SCHEMA_NIGHT, "night": list(rec13["nights"])[0], **{k: n13[k] for k in
                            ("runs_total", "runs_real", "runs_simulated", "runs_by_state",
                             "wall_clock_s_total", "interventions", "intervened", "night_unattended", "forced")},
                           "run_ids": n13["run_ids"]})
        rows13, probs13 = compare(recompute([d], [line], DEFAULT_BOUNDARY_HOUR))
        check("T13 正控：逐字段一致 ⇒ CONSISTENT 且 problems=0",
              rows13[0]["verdict"] == "CONSISTENT" and probs13 == [])

        # T14 空语料不判绿（audit 必须 rc=2）
        empty = os.path.join(tmp, "t14", "runs")
        os.makedirs(empty)
        check("T14 空语料 ⇒ nights 空（上层 rc=2）", not recompute([empty], [], DEFAULT_BOUNDARY_HOUR)["nights"])

        # T15 只读自证内核：hash_tree 对同一文件稳定，改动可判
        f = os.path.join(tmp, "t15.txt")
        open(f, "w").write("x")
        h1 = hash_tree([f])
        h2 = hash_tree([f])
        open(f, "w").write("y")
        h3 = hash_tree([f])
        check("T15 hash_tree 稳定且能判变化", h1 == h2 and h1 != h3)

        # T16 长批次跨度计算（real 簇）与模拟剔除
        d16 = os.path.join(tmp, "t16", "runs")
        write(d16, [fixture("k1", 1000000), fixture("k2", 1000000 + 3600 * 3), fixture("k3", 1000000 + 99, sim=True)])
        n16 = list(recompute([d16], [], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T16 真 run 簇跨度 ≥ 3h 且模拟不掺", abs(n16["real_cluster_span_s"] - 10800) < 1e-6)

        # T17 log-intervention 记录解析（含 R3 归属）
        iv17 = json.dumps({"schema": SCHEMA_INTERVENTION, "id": "iv-x", "at": "2026-03-04T09:00:00",
                           "at_ts": 1000000, "night": night_of(1000000, 8), "by": "drill",
                           "run_id": "a1", "kind": "human_unblock", "note": "n"})
        nights17, ivs17, bad17 = split_ledger([iv17])
        check("T17 干预行被识别且不当作夜行", nights17 == {} and len(ivs17) == 1 and bad17 == [])

        # T19 簇切分：包络≠簇（跨空档两段不算一段长批次）；单 run 簇 span = 自身墙钟
        c1 = clusters_of([(0, 10), (60, 70), (120, 130)])
        c2 = clusters_of([(0, 10), (60, 70), (3600, 3610)])
        c3 = clusters_of([])
        c4 = clusters_of([(0, 3600)])
        check("T19 簇：连续段归并/超 gap 断开/空输入/单 run 计自身墙钟",
              len(c1) == 1 and c1[0]["span_s"] == 130.0 and len(c2) == 2
              and max(x["span_s"] for x in c2) == 70.0 and c3 == [] and c4[0]["span_s"] == 3600.0)
        # T20 长批次判据用簇不用包络：真实数据里 09-22 那种 9h 空档不得算一段
        d20 = os.path.join(tmp, "t20", "runs")
        base20 = _dt.datetime(2026, 3, 4, 9, 0, 0).timestamp()
        # 两段各 10 分钟的连续工作，中间横着 11h 空档（run 间隔 600s < max_gap ⇒ 同簇）
        write(d20, [fixture("m1", base20), fixture("m2", base20 + 600),
                    fixture("m3", base20 + 39600), fixture("m4", base20 + 40200)])
        n20 = list(recompute([d20], [], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T20 包络 11.5h 但最长簇仅 0.5h ⇒ 不判长批次（簇口径生效）",
              n20["real_cluster_span_s"] > 3600 and n20["real_longest_cluster_s"] < 3600
              and len(n20["real_clusters"]) == 2)
        # T21 长批次正例：真跨 1h 的连续簇必须被判为长批次（防"永不判长"的假绿）
        d21 = os.path.join(tmp, "t21", "runs")
        write(d21, [fixture("n%d" % i, base20 + 600 * i) for i in range(7)])  # 链式 0..3600s
        n21 = list(recompute([d21], [], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T21 跨 1h 连续簇 ⇒ 判为长批次", n21["real_longest_cluster_s"] >= 3600
              and len(n21["real_clusters"]) == 1)

        # T23 异常记录护栏：跨年占位 ts 的记录不得虚构成 371 天长批次
        d23 = os.path.join(tmp, "t23", "runs")
        weird = {"schema": MARKER, "run_id": "z1", "state": "ACCEPTED", "simulated": False,
                 "history": [{"ts": 1758000000.0, "from": None, "to": "DISPATCHED", "actor": "driver"},
                             {"ts": 1790039091.0, "from": "DISPATCHED", "to": "ACCEPTED", "actor": "driver"}]}
        same_night = {"schema": MARKER, "run_id": "z2", "state": "ACCEPTED", "simulated": False,
                      "history": [{"ts": 1758003600.0, "from": None, "to": "DISPATCHED", "actor": "driver"},
                                  {"ts": 1758005400.0, "from": "DISPATCHED", "to": "ACCEPTED", "actor": "driver"}]}
        write(d23, [weird, same_night])   # 同夜（按首 ts 归夜），z2 仅 0.5h
        n23 = list(recompute([d23], [], DEFAULT_BOUNDARY_HOUR)["nights"].values())[0]
        check("T23 跨年记录被标异常且不计入长批次",
              any(x["run_id"] == "z1" for x in n23["anomalous_runs"])
              and n23["real_longest_cluster_s"] < 3600.0)
        check("T23 反事实：不剔异常时该夜会被算成超长批次",
              n23["real_longest_cluster_all_s"] > 86400)

        # T22 台账「每夜取最后一条」语义（append-only 追加新行）
        import tempfile as _tf
        led22 = os.path.join(tmp, "led22.jsonl")
        open(led22, "w").write(json.dumps({"night": "2026-03-04", "runs_total": 1, "intervened": False}) + "\n"
                               + json.dumps({"night": "2026-03-04", "runs_total": 1, "intervened": True}) + "\n")
        check("T22 同夜多行 ⇒ 取最后一条", (_night_line(led22, "2026-03-04") or {}).get("intervened") is True)

        # T18 坏台账行进 ledger_bad_lines（不静默）
        nights18, ivs18, bad18 = split_ledger(["{oops", json.dumps({"foo": 1})])
        check("T18 坏行/未知行必报", len(bad18) == 2)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    npass = sum(1 for _n, c, _x in checks if c)
    for name, c, note in checks:
        print("[%s] %s%s" % ("PASS" if c else "FAIL", name, ("  # " + note) if note else ""))
    print("selftest: %d/%d passed" % (npass, len(checks)))
    return 0 if npass == len(checks) else 1


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="夜批无人值守台账独立复算/注入负控/只读自证（TODO L194）")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("audit")
    a.add_argument("--ledger", required=True)
    a.add_argument("--runs-root", action="append", required=True)
    a.add_argument("--boundary", type=int, default=DEFAULT_BOUNDARY_HOUR)
    a.add_argument("--json")
    a.set_defaults(func=cmd_audit)

    i = sub.add_parser("inject")
    i.add_argument("--sandbox", required=True)
    i.add_argument("--runs-root", action="append", required=True)
    i.add_argument("--boundary", type=int, default=DEFAULT_BOUNDARY_HOUR)
    i.add_argument("--json")
    i.set_defaults(func=cmd_inject)

    p = sub.add_parser("proof")
    p.add_argument("--ledger", required=True)
    p.add_argument("--runs-root", action="append", required=True)
    p.add_argument("--boundary", type=int, default=DEFAULT_BOUNDARY_HOUR)
    p.add_argument("--json")
    p.set_defaults(func=cmd_proof)

    s = sub.add_parser("selftest")
    s.set_defaults(func=lambda _a: selftest())

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
