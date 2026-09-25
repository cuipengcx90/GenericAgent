#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan_landing_backtest.py — 规划批次「落地率」回测（只读，纯 stdlib）。

背景（TODO 第14批 / 规划模式 C5，R422）：量化「规划产出多、落地靠人」的系统性瓶颈。
- 逐条判两件事：① **是否需要外部（用户）批准** —— 复用 `tools/todo_quality_gate.py`
  的 `approval_flag()` 同一份判据（单一来源，禁在此重写第二套规则）；
  ② **是否已落地** —— 该条声明的产物路径在盘上是否存在（机械判定，非人工印象）。
- 批量口径：manifest（JSON）描述一个批次的候选；第 12/13 批按 TODO 验收「不重算」，
  只把 R417 已记录的结果原样放 `reference` 块作对照。

用法：
    python3 tools/plan_landing_backtest.py --manifest temp/plan_landing/batch11.json
    python3 tools/plan_landing_backtest.py --manifest ... --json out.json
    python3 tools/plan_landing_backtest.py --selftest

manifest schema = `ga.plan_landing/v1`：

    {"schema": "ga.plan_landing/v1",
     "batch": "第11批",
     "candidates": [
        {"id": "C1", "score": 8.33, "review": "保留（本批最高）",
         "text_ref": {"file": "temp/plan11/prompt0.txt", "id": "C1"},   # 判据输入（可省）
         "artifacts": ["tools/x.py", "memory/autonomous_reports/R403_x.md"],
         "delivered_report": "R403", "note": "…"},
        …],
     "reference": {"batch": "第13批", "source": "…R417….md",
                   "recorded": "…逐字摘录…", "recomputed": false}}

口径边界（重要，与本工具的诚实性有关）：
* 「需批准」是**提示级**判据：它认的是候选正文里的批准依赖语言，无法区分
  「本项落地需批准」与「正文在描述待批台账里别人的批准依赖」⇒ 只作 FLAG 提示，
  不改变 todo_quality_gate 的 A/B/C 判定，也不改退出码语义（本工具 rc：0 有数据、
  1 存在「需批准且未全落地」的条目、2 输入无效/空批次）。
* 「已落地」= 声明产物全部存在，**不等于**「质量达标」；部分存在记 `部分落地 n/m`。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import todo_quality_gate as gate  # noqa: E402  判据单一来源

SCHEMA = "ga.plan_landing/v1"


def repo_root(path=None):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, cwd=path or HERE).stdout.strip()
        return out or os.getcwd()
    except Exception:
        return os.getcwd()


def resolve_text(ref, root):
    """按 {file,id} 从候选源文件里取该候选正文（复用 gate 的解析器，禁第二套解析）。"""
    if not ref:
        return None, ""
    fp = os.path.join(root, ref["file"])
    if not os.path.exists(fp):
        return None, f"text_ref 文件不存在: {ref['file']}"
    cands = gate.parse_candidates(open(fp, encoding="utf-8").read())
    for cid, body in cands:
        if cid == ref.get("id"):
            return body, ""
    return None, f"text_ref 命不中候选 {ref.get('id')}（该文件解析出 {len(cands)} 条）"


def judge_candidate(c, root):
    """→ dict（需批准 / 落地 / 证据原样保留，禁黑箱）"""
    rid = c["id"]
    hits, err = [], ""
    if "approval_override" in c and c["approval_override"] is not None:
        need = bool(c["approval_override"])
        hits = [{"label": "override", "text": c.get("approval_evidence", "")}]
        src = "override"
    else:
        body, err = resolve_text(c.get("text_ref"), root)
        if body is None:
            need, src = None, "未判定"
        else:
            hits = gate.approval_flag(body)
            need, src = bool(hits), "criterion"
    arts = c.get("artifacts") or []
    exists = [a for a in arts if os.path.exists(os.path.join(root, a))]
    missing = [a for a in arts if a not in exists]
    if not arts:
        landed, frac = "未判定（无声明产物）", None
    elif not missing:
        landed, frac = f"全落地({len(exists)}/{len(arts)})", (len(exists), len(arts))
    elif exists:
        landed, frac = f"部分落地({len(exists)}/{len(arts)})", (len(exists), len(arts))
    else:
        landed, frac = f"未落地(0/{len(arts)})", (0, len(arts))
    return {"id": rid, "score": c.get("score"), "review": c.get("review", ""),
            "report": c.get("delivered_report", ""), "note": c.get("note", ""),
            "need_approval": need, "approval_source": src,
            "approval_hits": [(h["label"], h["text"]) for h in hits],
            "landed": landed, "frac": frac, "missing": missing,
            "text_ref": (c.get("text_ref") or {}).get("file", ""), "text_err": err}


def run_manifest(path, root):
    m = json.load(open(path, encoding="utf-8"))
    if m.get("schema") != SCHEMA:
        raise SystemExit(f"manifest schema 必须为 {SCHEMA}，实为 {m.get('schema')!r}")
    cands = m.get("candidates") or []
    if not cands:
        raise SystemExit(f"# 批次为空（{path}）：不作「全部通过」的结论，exit 2")
    rows = [judge_candidate(c, root) for c in cands]
    n = len(rows)
    need = [r for r in rows if r["need_approval"] is True]
    full = [r for r in rows if r["frac"] and r["frac"][0] == r["frac"][1]]
    determined = [r for r in rows if r["frac"]]
    blocked = [r for r in need if not (r["frac"] and r["frac"][0] == r["frac"][1])]
    summary = {
        "batch": m.get("batch", ""), "candidates": n,
        "need_approval": len(need), "need_approval_ids": [r["id"] for r in need],
        "pct_need_approval": round(100.0 * len(need) / n, 1),
        "landed_full": len(full), "landed_full_ids": [r["id"] for r in full],
        "pct_landed": round(100.0 * len(full) / len(determined), 1) if determined else None,
        "blocked_by_approval": len(blocked), "blocked_ids": [r["id"] for r in blocked],
        "unlanded_or_partial": [r["id"] for r in rows if r["frac"] and r["frac"][0] < r["frac"][1]],
        "undetermined": [r["id"] for r in rows if not r["frac"]],
    }
    return {"manifest": os.path.basename(path), "batch": m.get("batch", ""), "rows": rows,
            "summary": summary, "reference": m.get("reference")}


def fmt_manifest(res):
    out = [f"# plan_landing_backtest  批次={res['batch']}  候选={len(res['rows'])}  (manifest {res['manifest']})"]
    for r in res["rows"]:
        need = {True: "YES", False: "no", None: "未判定"}[r["need_approval"]]
        hits = ",".join(f"{l}:'{t}'" for l, t in r["approval_hits"]) or "无"
        out.append(f"[{r['id']}] 需批准={need}({r['approval_source']}: {hits}) | 落地={r['landed']}"
                   f" | 报告={r['report'] or '-'}" + (f" | 缺={','.join(r['missing'])}" if r["missing"] else ""))
        if r["text_err"]:
            out.append(f"      注: {r['text_err']}")
    s = res["summary"]
    out.append("\n# 汇总 " + json.dumps(s, ensure_ascii=False))
    if res.get("reference"):
        ref = res["reference"]
        out.append(f"\n# 对照批次 {ref.get('batch', '?')}（来源 {ref.get('source', '?')}；"
                   f"recomputed={ref.get('recomputed')}）")
        if isinstance(ref.get("recorded"), dict):
            for k, v in ref["recorded"].items():
                out.append(f"    {k}: {v}")
        else:
            out.append(f"    {ref.get('recorded', '')}")
    return "\n".join(out)


def selftest():
    fails = []

    def chk(name, cond, extra=""):
        print(("  PASS " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    root = tempfile.mkdtemp(prefix="plan_landing_st_")
    open(os.path.join(root, "exists.py"), "w").write("x")
    open(os.path.join(root, "cands.txt"), "w", encoding="utf-8").write(
        "C1 【工程】纯自动候选\n验收：①rc=0 ②selftest 3/3\n"
        "C2 【产出】本项 apply 须用户批准后才能执行\n验收：①rc=0 ②selftest 3/3\n"
        "C3 【分析】无关文本，不含批准依赖语言\n验收：①rc=0 ②selftest 3/3\n")
    mf = os.path.join(root, "m.json")
    json.dump({"schema": SCHEMA, "batch": "合成批", "candidates": [
        {"id": "C1", "text_ref": {"file": "cands.txt", "id": "C1"}, "artifacts": ["exists.py"]},
        {"id": "C2", "text_ref": {"file": "cands.txt", "id": "C2"}, "artifacts": ["gone.py"]},
        {"id": "C3", "text_ref": {"file": "cands.txt", "id": "C3"}, "artifacts": ["exists.py"]},
    ]}, open(mf, "w", encoding="utf-8"), ensure_ascii=False)
    res = run_manifest(mf, root)
    rows = {r["id"]: r for r in res["rows"]}
    s = res["summary"]

    chk("P1 产物存在 ⇒ 全落地", rows["C1"]["frac"] == (1, 1) and rows["C1"]["need_approval"] is False,
        f"landed={rows['C1']['landed']} need={rows['C1']['need_approval']}")
    chk("NC1 合成「需用户批准」+ 产物缺失 ⇒ 标 FLAG 且计入瓶颈",
        rows["C2"]["need_approval"] is True and rows["C2"]["frac"] == (0, 1)
        and s["blocked_by_approval"] == 1 and rows["C2"]["missing"] == ["gone.py"],
        f"need={rows['C2']['need_approval']} landed={rows['C2']['landed']} blocked={s['blocked_by_approval']}")
    chk("NC2 纯自动候选不得误标 FLAG", rows["C3"]["need_approval"] is False,
        f"hits={rows['C3']['approval_hits']}")
    chk("NC3 计数与百分比自证", s["candidates"] == 3 and s["need_approval"] == 1
        and s["pct_need_approval"] == 33.3 and s["landed_full"] == 2 and s["pct_landed"] == 66.7,
        json.dumps(s, ensure_ascii=False))
    # 语料计数自证：text_ref 命不中时不得静默当「自动」
    mf2 = os.path.join(root, "m2.json")
    json.dump({"schema": SCHEMA, "batch": "合成批2", "candidates": [
        {"id": "C9", "text_ref": {"file": "cands.txt", "id": "C99"}, "artifacts": ["exists.py"]}]},
        open(mf2, "w", encoding="utf-8"), ensure_ascii=False)
    r2 = run_manifest(mf2, root)["rows"][0]
    chk("NC4 text_ref 命不中 ⇒ 未判定（不得静默算自动）", r2["need_approval"] is None and r2["text_err"],
        f"need={r2['need_approval']} err={r2['text_err']}")
    mf3 = os.path.join(root, "m3.json")
    json.dump({"schema": SCHEMA, "batch": "空批", "candidates": []}, open(mf3, "w", encoding="utf-8"))
    try:
        run_manifest(mf3, root)
        empty_ok = False
    except SystemExit:
        empty_ok = True
    chk("NC5 空批次不得静默通过（SystemExit/rc2）", empty_ok)
    print(f"selftest {6 - len(fails)}/6")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest")
    ap.add_argument("--json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    root = repo_root()
    before = {p: os.path.getmtime(p) for p in
              [os.path.join(root, "tools", f) for f in os.listdir(os.path.join(root, "tools"))
               if f.endswith(".py")]}
    res = run_manifest(a.manifest, root)
    print(fmt_manifest(res))
    after = {p: os.path.getmtime(p) for p in
             [os.path.join(root, "tools", f) for f in os.listdir(os.path.join(root, "tools"))
              if f.endswith(".py")]}
    print(f"# 只读自证 tools/*.py mtime {'UNCHANGED' if before == after else 'CHANGED!!'}")
    if a.json:
        json.dump(res, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"# json -> {a.json}")
    sys.exit(1 if res["summary"]["blocked_by_approval"] else 0)


if __name__ == "__main__":
    main()
