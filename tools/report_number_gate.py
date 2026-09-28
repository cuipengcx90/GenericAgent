#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_number_gate.py — 报告编号双侧一致性门禁（只读）

背景（第22批 TODO L195 / R472 收尾实测）:
  helper._next_report_number() 只扫 temp/autonomous_reports/history.txt 取 max+1，
  而 temp 侧是**滚动近窗**（会被压缩/截断），memory 侧才是全量归档面。
  两侧一旦分叉，照 temp 侧取号就会与既有报告重号并**静默覆盖**（helper 用 move 落盘）。

本工具在取号前机械核三件事（全程只读，绝不修改 helper / 索引 / 任何文件）:
  Q1 双侧最大值 : temp history.txt max vs memory history.txt max（不等即报，口径以 memory 为准）
  Q2 重号判定   : 拟用编号是否已出现于「盘面文件名」或四份索引
                  （memory history.txt / memory history.md / memory INDEX.md / temp history.txt）
  Q3 存量偏差   : INDEX 编号集合 vs history 编号集合的双向差集量化

额外能力:
  --suggest 会从 memory_max+1 起向上找**第一个未被任何来源占用**的编号
  （因为 INDEX 常含「仅索引」编号，直接取 max+1 仍可能撞号）

判定: rc=0 无异常；rc=1 有告警（分叉 / 重号 / 偏差 / 缺文件）；rc=2 用法错误或根目录不存在
用法:
  python3 tools/report_number_gate.py                  # 真树体检
  python3 tools/report_number_gate.py --propose 486    # 取号前核对
  python3 tools/report_number_gate.py --json           # 机读
  python3 tools/report_number_gate.py --proof          # 加只读自证（被读文件 sha256 前后一致）
  python3 tools/report_number_gate.py --selftest       # 自测（含负控）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

GA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RE_REPORT = re.compile(r"^R(\d{1,4})[_\-].*\.md$")
RE_HIST = re.compile(r"^R(\d{1,4})\s*\|")
RE_INDEX_ROW = re.compile(r"^\|\s*R(\d{1,4})\s*\|")


# ────────────────────────── 只读原语 ──────────────────────────
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_nums(path, kind, record=None):
    """kind: 'history' | 'index'。返回 (nums:set, exists:bool)。只读。"""
    if record is not None and os.path.isfile(path):
        record.append(path)
    if not os.path.isfile(path):
        return set(), False
    rx = RE_HIST if kind == "history" else RE_INDEX_ROW
    nums = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = rx.match(ln)
            if m:
                nums.add(int(m.group(1)))
    return nums, True


def dir_report_nums(d, record=None):
    """盘面文件名里的编号（只 列目录 + stat，不读内容）。返回 (nums:set, exists:bool)。"""
    if not os.path.isdir(d):
        return set(), False
    nums = set()
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        m = RE_REPORT.match(name)
        if m:
            nums.add(int(m.group(1)))
    return nums, True


def _paths(root):
    mem = os.path.join(root, "memory", "autonomous_reports")
    tmp = os.path.join(root, "temp", "autonomous_reports")
    return {
        "memory_dir": mem,
        "temp_dir": tmp,
        "memory_history": os.path.join(mem, "history.txt"),
        "memory_history_md": os.path.join(mem, "history.md"),
        "memory_index": os.path.join(mem, "INDEX.md"),
        "temp_history": os.path.join(tmp, "history.txt"),
    }


# ────────────────────────── 审计内核 ──────────────────────────
def audit(root, propose=None, record=None):
    """只读审计。root = 仓库根（其下有 memory/ 与 temp/）。返回结果 dict（含 rc）。"""
    rec = record if record is not None else []
    P = _paths(root)
    res = {"root": root, "checks": {}, "alerts": [], "sources": {}}
    if not os.path.isdir(P["memory_dir"]):
        res["rc"] = 2
        res["error"] = "memory/autonomous_reports 不存在: %s" % P["memory_dir"]
        return res

    th, th_ok = read_nums(P["temp_history"], "history", rec)
    mh, mh_ok = read_nums(P["memory_history"], "history", rec)
    mhmd, mhmd_ok = read_nums(P["memory_history_md"], "history", rec)
    mi, mi_ok = read_nums(P["memory_index"], "index", rec)
    mfiles, mdir_ok = dir_report_nums(P["memory_dir"], rec)
    tfiles, tdir_ok = dir_report_nums(P["temp_dir"], rec)

    res["sources"] = {
        "temp_history": sorted(th), "memory_history": sorted(mh),
        "memory_history_md": sorted(mhmd), "memory_index": sorted(mi),
        "memory_files": sorted(mfiles), "temp_files": sorted(tfiles),
    }
    res["exists"] = {
        "temp_history": th_ok, "memory_history": mh_ok,
        "memory_history_md": mhmd_ok, "memory_index": mi_ok,
        "memory_dir": mdir_ok, "temp_dir": tdir_ok,
    }

    tmax = max(th) if th else None
    mmax = max(mh) if mh else None
    mmdmax = max(mhmd) if mhmd else None
    mimax = max(mi) if mi else None

    res["checks"]["temp_history_max"] = tmax
    res["checks"]["memory_history_max"] = mmax
    res["checks"]["memory_history_md_max"] = mmdmax
    res["checks"]["memory_index_max"] = mimax

    # Q1 双侧最大值
    if not th_ok:
        res["alerts"].append({"kind": "MISSING_TEMP_HISTORY", "severity": "ALERT", "detail": P["temp_history"]})
        res["checks"]["diverged"] = None
    elif not mh_ok:
        res["alerts"].append({"kind": "MISSING_MEMORY_HISTORY", "severity": "ALERT", "detail": P["memory_history"]})
        res["checks"]["diverged"] = None
    else:
        diverged = (tmax != mmax)
        res["checks"]["diverged"] = diverged
        if diverged and tmax > mmax:
            # temp 侧比归档面更新 ⇒ 有新报告未归档（严重：归档链断了一环）
            res["alerts"].append({
                "kind": "TEMP_AHEAD_OF_MEMORY", "severity": "ALERT",
                "detail": "temp max=R%s > memory max=R%s ⇒ 有报告未归档到 memory 侧，先补归档再取号"
                          % (tmax, mmax),
            })
        elif diverged:
            # temp 侧是滚动近窗（会被截断）⇒ 属已知形态，取号一律以 memory 侧为准
            res["alerts"].append({
                "kind": "TEMP_MEMORY_DIVERGED", "severity": "INFO",
                "detail": "temp max=R%s < memory max=R%s（temp 侧为滚动近窗，属已知截断形态；"
                          "取号口径以 memory 侧为准，勿用 temp 侧）" % (tmax, mmax),
            })

    # 占用面（用于 Q2）：四份索引 + 双侧盘面文件名
    used = {}
    for src, nums in (("memory_history", mh), ("memory_history_md", mhmd),
                      ("memory_index", mi), ("temp_history", th),
                      ("memory_files", mfiles), ("temp_files", tfiles)):
        for n in nums:
            used.setdefault(n, []).append(src)
    res["used_count"] = len(used)
    res["used"] = {str(k): sorted(v) for k, v in sorted(used.items())}

    # Q3 存量偏差
    idx_minus_hist = sorted(mi - mh)
    hist_minus_idx = sorted(mh - mi)
    res["checks"]["index_minus_history"] = idx_minus_hist
    res["checks"]["history_minus_index"] = hist_minus_idx
    if idx_minus_hist:
        res["alerts"].append({"kind": "INDEX_ONLY_NUMBERS", "severity": "INFO",
                              "detail": "%d 个编号有 INDEX 行但无 history 行: %s"
                                        % (len(idx_minus_hist), idx_minus_hist[:20])})
    if hist_minus_idx:
        res["alerts"].append({"kind": "HISTORY_ONLY_NUMBERS", "severity": "INFO",
                              "detail": "%d 个编号有 history 行但无 INDEX 行: %s"
                                        % (len(hist_minus_idx), hist_minus_idx[:20])})
    if not mhmd_ok:
        res["alerts"].append({"kind": "MISSING_MEMORY_HISTORY_MD", "severity": "INFO", "detail": P["memory_history_md"]})

    # --suggest: 从 memory_max+1 起找第一个未被占用者
    base = mmax if mmax is not None else 0
    sug = base + 1
    while sug in used:
        sug += 1
    res["checks"]["suggested_next"] = sug

    # Q2 拟用编号
    if propose is not None:
        res["checks"]["proposed"] = propose
        conflict = sorted(used.get(propose, []))
        res["checks"]["proposed_conflict"] = conflict
        res["checks"]["proposed_safe"] = (not conflict)
        if conflict:
            res["alerts"].append({
                "kind": "NUMBER_ALREADY_USED", "severity": "ALERT",
                "detail": "拟用编号 R%d 已被占用（来源: %s）⇒ 若落盘会静默覆盖既有报告"
                          % (propose, ", ".join(conflict)),
            })

    # rc 只看 ALERT：INFO 属已知存量/良性形态（temp 侧滚动截断、存量 INDEX-only 等），不影响取号安全
    res["rc"] = 1 if any(a.get("severity") == "ALERT" for a in res["alerts"]) else 0
    return res


def render(res):
    out = []
    if res.get("rc") == 2:
        return "[FATAL] %s" % res.get("error")
    c = res["checks"]
    out.append("root        : %s" % res["root"])
    out.append("temp   max  : R%s   (history.txt)" % c.get("temp_history_max"))
    out.append("memory max  : R%s   (history.txt) | history.md R%s | INDEX.md R%s"
               % (c.get("memory_history_max"), c.get("memory_history_md_max"), c.get("memory_index_max")))
    out.append("diverged    : %s   (temp 侧是否与 memory 侧分叉)" % c.get("diverged"))
    out.append("占用编号数  : %d 个（四索引 + 双侧盘面文件名并集）" % res.get("used_count", 0))
    out.append("建议取号    : R%d（memory_max+1 起第一个未占用编号）" % c.get("suggested_next"))
    out.append("INDEX-only  : %d 个 %s" % (len(c.get("index_minus_history", [])),
                                           c.get("index_minus_history", [])[:12]))
    out.append("history-only: %d 个 %s" % (len(c.get("history_minus_index", [])),
                                           c.get("history_minus_index", [])[:12]))
    if "proposed" in c:
        out.append("拟用 R%s    : %s" % (c["proposed"],
                                        "冲突 -> " + ", ".join(c["proposed_conflict"])
                                        if c.get("proposed_conflict") else "未占用 (safe)"))
    hard = [a for a in res["alerts"] if a.get("severity") == "ALERT"]
    soft = [a for a in res["alerts"] if a.get("severity") != "ALERT"]
    if hard:
        out.append("alerts:")
        for a in hard:
            out.append("  - [%s] %s" % (a["kind"], a["detail"]))
    else:
        out.append("alerts      : (none)")
    if soft:
        out.append("notes (INFO, 不影响取号/不改 rc):")
        for a in soft:
            out.append("  - [%s] %s" % (a["kind"], a["detail"]))
    out.append("VERDICT: %s (rc=%d)" % ("OK" if res["rc"] == 0 else "ALERT", res["rc"]))
    return "\n".join(out)


# ────────────────────────── 自测（含负控） ──────────────────────────
def _build_fixture(root, kind):
    mem = os.path.join(root, "memory", "autonomous_reports")
    tmp = os.path.join(root, "temp", "autonomous_reports")
    os.makedirs(mem, exist_ok=True)
    os.makedirs(tmp, exist_ok=True)
    if kind == "consistent":
        open(os.path.join(mem, "history.txt"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        open(os.path.join(mem, "history.md"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        open(os.path.join(mem, "INDEX.md"), "w").write(
            "| R10 | ✅正文 | t10 | 100B R10_a.md | s |\n| R09 | ✅正文 | t09 | 100B R09_a.md | s |\n")
        open(os.path.join(tmp, "history.txt"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        open(os.path.join(mem, "R09_a.md"), "w").write("x")
        open(os.path.join(mem, "R10_a.md"), "w").write("x")
    elif kind == "diverged":
        open(os.path.join(mem, "history.txt"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        open(os.path.join(mem, "history.md"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        # INDEX 多一个「仅索引」编号 11；盘面另有未入索引的 R13
        open(os.path.join(mem, "INDEX.md"), "w").write(
            "| R10 | ✅正文 | t10 | 100B R10_a.md | s |\n| R11 | 仅索引 | t11 | 0B  | s |\n")
        open(os.path.join(tmp, "history.txt"), "w").write("R08 | d | 工程 | t08\n")
        open(os.path.join(mem, "R09_a.md"), "w").write("x")
        open(os.path.join(mem, "R10_a.md"), "w").write("x")
        open(os.path.join(mem, "R13_z.md"), "w").write("x")
        open(os.path.join(tmp, "R08_c.md"), "w").write("x")
    elif kind == "temp_ahead":
        # 负控：temp 侧比归档面更新（有新报告未归档）
        open(os.path.join(mem, "history.txt"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        open(os.path.join(mem, "history.md"), "w").write(
            "R10 | d | 工程 | t10\nR09 | d | 工程 | t09\n")
        open(os.path.join(mem, "INDEX.md"), "w").write(
            "| R10 | ✅正文 | t10 | 100B R10_a.md | s |\n| R09 | ✅正文 | t09 | 100B R09_a.md | s |\n")
        open(os.path.join(tmp, "history.txt"), "w").write(
            "R12 | d | 工程 | t12\nR10 | d | 工程 | t10\n")
        open(os.path.join(mem, "R09_a.md"), "w").write("x")
        open(os.path.join(mem, "R10_a.md"), "w").write("x")
    elif kind == "no_temp_history":
        open(os.path.join(mem, "history.txt"), "w").write("R10 | d | 工程 | t10\n")
        open(os.path.join(mem, "history.md"), "w").write("R10 | d | 工程 | t10\n")
        open(os.path.join(mem, "INDEX.md"), "w").write(
            "| R10 | ✅正文 | t10 | 100B R10_a.md | s |\n")
        open(os.path.join(mem, "R10_a.md"), "w").write("x")
    return root


def _snapshot(root):
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            out[p] = sha256(p)
    return out


def selftest():
    ok = fail = 0
    base = tempfile.mkdtemp(prefix="rng_selftest_")

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS %s %s" % (name, extra))
        else:
            fail += 1
            print("  FAIL %s %s" % (name, extra))

    def kinds(res):
        return {a["kind"] for a in res["alerts"]}

    # ── A 真值负控：分叉 + 仅索引 + 盘面未入索引 ──
    ra = _build_fixture(os.path.join(base, "a"), "diverged")
    snap_before = _snapshot(ra)
    res = audit(ra)
    snap_after = _snapshot(ra)
    chk("A1 分叉必报(INFO 档)", "TEMP_MEMORY_DIVERGED" in kinds(res))
    chk("A9 INFO 档不抬 rc（只报不改判）", res["rc"] == 0 and
        [a["severity"] for a in res["alerts"] if a["kind"] == "TEMP_MEMORY_DIVERGED"] == ["INFO"],
        "(rc=%s)" % res["rc"])
    chk("A2 INDEX-only 必报", "INDEX_ONLY_NUMBERS" in kinds(res) and
        res["checks"]["index_minus_history"] == [11])
    chk("A3 盘面文件名计入占用", res["used"].get("13") is not None and "memory_files" in res["used"]["13"])
    chk("A4 建议号跳过 INDEX-only 与盘面占用", res["checks"]["suggested_next"] == 12
        and str(res["checks"]["suggested_next"]) not in res["used"],
        "(got R%s)" % res["checks"]["suggested_next"])
    chk("A5 只读自证（fixture sha256 前后一致）", snap_before == snap_after)
    res2 = audit(ra, propose=10)
    chk("A6 拟用已占用号必报", "NUMBER_ALREADY_USED" in kinds(res2) and res2["rc"] == 1 and
        "memory_files" in res2["checks"]["proposed_conflict"])
    res3 = audit(ra, propose=11)
    chk("A7 仅索引号也判冲突（NC：只查盘面会漏）", bool(res3["checks"]["proposed_conflict"]) and
        "memory_index" in res3["checks"]["proposed_conflict"])
    res4 = audit(ra, propose=14)
    chk("A8 未占用号不报冲突", res4["checks"]["proposed_conflict"] == [] and
        res4["checks"]["proposed_safe"] is True)

    # ── B 正控：两侧一致且编号未占用 ⇒ rc=0 ──
    rb = _build_fixture(os.path.join(base, "b"), "consistent")
    resb = audit(rb, propose=11)
    chk("B1 一致树 rc=0", resb["rc"] == 0 and resb["checks"]["diverged"] is False,
        "alerts=%s" % kinds(resb))
    chk("B2 一致树建议号=R11", resb["checks"]["suggested_next"] == 11)
    chk("B3 一致树拟用未占用", resb["checks"]["proposed_safe"] is True)

    # ── C 缺文件负控 ──
    rc_dir = _build_fixture(os.path.join(base, "c"), "no_temp_history")
    resc = audit(rc_dir)
    chk("C1 缺 temp history 必报且不崩", "MISSING_TEMP_HISTORY" in kinds(resc) and resc["rc"] == 1)

    # ── D 用法错误 ──
    resd = audit(os.path.join(base, "does_not_exist"))
    chk("D1 根目录不存在 rc=2", resd["rc"] == 2 and "error" in resd)

    # ── E 同源一致性：history.md 与 history.txt 编号集合应一致 ──
    rese = audit(ra)
    chk("E1 history.md 与 history.txt 编号集合一致",
        set(rese["sources"]["memory_history"]) == set(rese["sources"]["memory_history_md"]))

    # ── F 负控：temp 侧超前（有新报告未归档）⇒ 与「滚动截断」区分开 ──
    rf = _build_fixture(os.path.join(base, "f"), "temp_ahead")
    resf = audit(rf)
    chk("F1 temp 超前必报且不与截断态混同",
        "TEMP_AHEAD_OF_MEMORY" in kinds(resf) and
        "TEMP_MEMORY_DIVERGED" not in kinds(resf) and resf["rc"] == 1)
    chk("F2 temp 超前时建议号仍取 memory_max+1", resf["checks"]["suggested_next"] == 11,
        "(got R%s)" % resf["checks"]["suggested_next"])

    shutil.rmtree(base, ignore_errors=True)
    print("selftest: %d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


# ────────────────────────── 入口 ──────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="报告编号双侧一致性门禁（只读）")
    ap.add_argument("--root", default=GA, help="仓库根（默认本工具所在仓）")
    ap.add_argument("--propose", type=int, help="拟用编号，核对是否已被占用")
    ap.add_argument("--suggest", action="store_true", help="只打印建议取号")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--proof", action="store_true", help="只读自证（被读文件 sha256 前后一致）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    rec = []
    res = audit(args.root, propose=args.propose, record=rec)
    proof = None
    if args.proof and res.get("rc") != 2:
        before = {p: sha256(p) for p in sorted(set(rec)) if os.path.isfile(p)}
        after = {p: sha256(p) for p in sorted(set(rec)) if os.path.isfile(p)}
        proof = {"files": len(before), "changed": [p for p in before if before[p] != after.get(p)]}
        res["proof"] = proof

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.suggest:
        print(res["checks"]["suggested_next"] if res.get("rc") != 2 else "")
    else:
        print(render(res))
        if proof is not None:
            print("proof: files=%d changed=%s" % (proof["files"], proof["changed"] or "[]"))
    return res["rc"]


if __name__ == "__main__":
    sys.exit(main())
