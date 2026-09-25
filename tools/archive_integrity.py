#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档完整性只读检测器（report archive integrity guard）

背景：`memory/autonomous_reports/` 是受 git 跟踪的 durable 归档，历史上出现过
①同一编号双文件（纯改名对 / 附录拆分对 / 真内容冲突）②INDEX 指向错文件
③history 与磁盘文件编号不一致 ④`_next_report_number()` 无锁导致的抢号。

本工具**只读**：只以 rb 打开归档文件，绝不写入、改名、删除归档目录内任何文件；
也**不修改**归档工具 `tools/autonomous_reports_archive.py`。

用法：
  python3 tools/archive_integrity.py                      # 检测 memory/autonomous_reports
  python3 tools/archive_integrity.py --dir <目录>          # 指定归档目录（只读）
  python3 tools/archive_integrity.py --json <路径>         # 另存 JSON 结论（默认写到 temp/）
  python3 tools/archive_integrity.py --selftest           # 夹具负控自测（在 temp/ 内，不触真实归档）

退出码：0 = PASS（无硬性 finding）；1 = 有硬性 finding；2 = 输入缺失/不可读。
硬性 finding（会 rc=1）：真内容冲突 / INDEX 指向不存在的文件 / INDEX 与 history 编号不一致
                        （两个方向）/ 存在有正文但无 history 行的编号 / 编号空洞。
软性提示（不改变 rc）：同号同源组、同号拆分对、history 有编号但归档无正文、INDEX 指向非主文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

GA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(GA, "memory", "autonomous_reports")

RE_REPORT = re.compile(r"^R(\d{1,4})[_\-].*\.md$")
RE_HIST = re.compile(r"^R(\d{1,4})\s*\|")
RE_INDEX_ROW = re.compile(r"^\|\s*R(\d{1,4})\s*\|")
RE_INDEX_FILE = re.compile(r"\|\s*(\d+)\s*B\s+(\S+\.md)")

# 同号组的处置建议（本工具只给判据与建议，不执行任何改名/删除）
ADVICE = {
    "SAME": "同源副本（sha1 逐一相同）→ 建议留一份、另一份 git rm（属待批动作）",
    "CROSSREF": "拆分/附录对（一方正文点名另一方文件名）→ 非内容冲突；INDEX 应指向正文更全者，附件保留",
    "SPLIT": "段落高度重叠但互不包含 → 疑为拆分对，需人工判读主从",
    "CONFLICT": "真内容冲突（同号、内容不同、互不包含、行集合重叠<50%）→ 必须人工裁定保留哪份，另一份改名或移出归档",
}
# 文件名里出现 `数字+B` 才认定为 INDEX 的文件字段，避免把摘要里的 .md 误抓


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:            # 只读
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_lines(path: str) -> set:
    """行集合（去首尾空白、丢空行）——用于判断两个文件是否同源。"""
    out = set()
    with open(path, "rb") as f:            # 只读
        for raw in f.read().decode("utf-8", "replace").splitlines():
            s = raw.strip()
            if s:
                out.add(s)
    return out


def scan_dir(d: str):
    """返回 (files_by_num, all_files, matched, excluded)。只读 os.listdir + stat。"""
    if not os.path.isdir(d):
        raise FileNotFoundError(d)
    allf = sorted(os.listdir(d))
    by_num, matched, excluded = defaultdict(list), [], []
    for name in allf:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            excluded.append(name + "  (非普通文件/子目录)")
            continue
        m = RE_REPORT.match(name)
        if m:
            by_num[int(m.group(1))].append(name)
            matched.append(name)
        else:
            excluded.append(name)
    return by_num, allf, matched, excluded


def read_history(path: str):
    rows = {}
    if not os.path.isfile(path):
        return rows, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = RE_HIST.match(ln)
            if m:
                rows.setdefault(int(m.group(1)), ln.strip())
    return rows, True


def read_index(path: str):
    rows = {}
    if not os.path.isfile(path):
        return rows, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = RE_INDEX_ROW.match(ln)
            if m:
                fm = RE_INDEX_FILE.search(ln)
                rows.setdefault(int(m.group(1)), []).append(
                    {"raw": ln.strip(),
                     "file": fm.group(2) if fm else None,
                     "bytes": int(fm.group(1)) if fm else None,
                     "index_only": "仅索引" in ln}
                )
    return rows, True


def classify_group(d: str, names: list):
    """同号组判定：SAME / CONTAINED / CROSSREF / SPLIT / CONFLICT。"""
    info = []
    for n in names:
        p = os.path.join(d, n)
        info.append({"name": n, "sha": sha256(p), "bytes": os.path.getsize(p)})
    shas = {i["sha"] for i in info}
    if len(shas) == 1:
        kind = "SAME"
    else:
        nameset = {i["name"] for i in info}
        kinds = []
        for i in info:                                   # 正文里提到同号另一文件名 ⇒ 拆分对
            try:
                with open(os.path.join(d, i["name"]), encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                body = ""
            if any(o != i["name"] and o in body for o in nameset):
                kinds.append("CROSSREF")
        if kinds:
            kind = "CROSSREF"
        else:
            sets = {i["name"]: norm_lines(os.path.join(d, i["name"])) for i in info}
            ns = list(sets)
            for a in range(len(ns)):
                for b in range(len(ns)):
                    if a != b and sets[ns[a]] and sets[ns[a]] <= sets[ns[b]]:
                        kinds.append("CONTAINED")
            if kinds:
                kind = "CONTAINED"
            else:
                inter = set.intersection(*sets.values())
                union = set.union(*sets.values())
                jac = len(inter) / max(1, len(union))
                kind = "SPLIT" if jac >= 0.5 else "CONFLICT"
    for i in info:
        i["lines"] = len(norm_lines(os.path.join(d, i["name"])))
    pairs = {}
    if kind != "SAME":
        ns = [i["name"] for i in info]
        for a in range(len(ns)):
            for b in range(a + 1, len(ns)):
                A = norm_lines(os.path.join(d, ns[a]))
                B = norm_lines(os.path.join(d, ns[b]))
                pairs[f"{ns[a]} vs {ns[b]}"] = {
                    "only_a": len(A - B), "only_b": len(B - A), "common": len(A & B)}
    return {"kind": kind, "files": info, "pairwise": pairs}


def detect(d: str):
    by_num, allf, matched, excluded = scan_dir(d)
    hist, hist_ok = read_history(os.path.join(d, "history.txt"))
    index, index_ok = read_index(os.path.join(d, "INDEX.md"))

    dups, findings, soft = {}, [], []
    for n, names in sorted(by_num.items()):
        if len(names) > 1:
            g = classify_group(d, names)
            dups[n] = g
            if g["kind"] == "CONFLICT":
                findings.append(f"同号真内容冲突 R{n}: {names}")
            else:
                soft.append(f"同号组 R{n}: {g['kind']} ({len(names)} 个文件)")

    disk_nums = set(by_num)
    hist_nums, index_nums = set(hist), set(index)
    maxn = max(sorted(disk_nums | hist_nums | index_nums) or [0])

    idx_bad_file, idx_declared_offsite = [], []
    for n, rows in sorted(index.items()):
        for r in rows:
            if not r["file"] or r["file"] in allf:
                continue
            if "未落盘" in r["raw"]:          # 行内已自声明正文不在归档（如临时路径）⇒ 非错误
                idx_declared_offsite.append(f"R{n} -> {r['file']}")
            else:
                idx_bad_file.append(f"R{n} -> {r['file']}")
    if idx_declared_offsite:
        soft.append(f"INDEX 自声明「未落盘」（正文在归档外路径，属预期状态）{len(idx_declared_offsite)} 条: "
                    f"{idx_declared_offsite}")
    if idx_bad_file:
        findings.append(f"INDEX 指向不存在的文件 {len(idx_bad_file)} 条: {idx_bad_file[:5]}")

    no_body = sorted(hist_nums - disk_nums)          # 有索引无正文（归档抢救现状，软）
    no_row = sorted(disk_nums - hist_nums)           # 有正文无 history 行（硬）
    if no_row:
        findings.append(f"有正文但 history 无编号行 {len(no_row)} 个: {no_row}")
    i_minus_h = sorted(index_nums - hist_nums)
    h_minus_i = sorted(hist_nums - index_nums)
    if i_minus_h:
        findings.append(f"INDEX 有而 history 无 {len(i_minus_h)} 个: {i_minus_h[:10]}")
    if h_minus_i:
        findings.append(f"history 有而 INDEX 无 {len(h_minus_i)} 个: {h_minus_i[:10]}")

    holes = [n for n in range(1, maxn + 1)
             if n not in disk_nums and n not in hist_nums and n not in index_nums]
    if holes:
        findings.append(f"编号空洞（1..{maxn} 三处皆无）{len(holes)} 个: {holes[:20]}")

    # INDEX 指向非主文件（同号组内行数最多者视为主报告）
    idx_wrong_pick = []
    for n, g in dups.items():
        main = max(g["files"], key=lambda i: i["lines"])["name"]
        for r in index.get(n, []):
            if r["file"] and r["file"] != main:
                idx_wrong_pick.append(f"R{n}: INDEX->{r['file']}  主文件应为 {main}")
    if idx_wrong_pick:
        soft.append(f"INDEX 指向同号组内非主文件 {len(idx_wrong_pick)} 条: {idx_wrong_pick}")

    if not hist_ok:
        findings.append(f"history.txt 缺失: {os.path.join(d, 'history.txt')}")
    if not index_ok:
        findings.append(f"INDEX.md 缺失: {os.path.join(d, 'INDEX.md')}")

    return {
        "dir": d,
        "scan": {"total_entries": len(allf), "matched_reports": len(matched),
                 "by_number": len(by_num), "excluded": excluded},
        "counts": {"history": len(hist_nums), "disk_numbers": len(disk_nums),
                   "disk_files": len(matched), "index_rows": sum(len(v) for v in index.values()),
                   "index_numbers": len(index_nums), "max_number": maxn},
        "dup_groups": {str(k): v for k, v in dups.items()},
        "coverage": {"no_body_for_history": len(no_body), "no_history_for_file": no_row,
                     "index_minus_history": i_minus_h, "history_minus_index": h_minus_i,
                     "number_holes": holes},
        "index_file_mismatch": idx_bad_file,
        "index_declared_offsite": idx_declared_offsite,
        "index_wrong_pick": idx_wrong_pick,
        "soft": soft,
        "findings": findings,
        "rc": 1 if findings else 0,
    }


def print_report(res):
    c, s = res["counts"], res["scan"]
    print(f"== 归档完整性检测（只读）== {res['dir']}")
    print(f"[语料自证] 目录条目 {s['total_entries']} / 命中报告 {s['matched_reports']} "
          f"/ 覆盖编号 {s['by_number']} / 排除 {len(s['excluded'])} 项 {s['excluded']}")
    print(f"[计数] history {c['history']} 行 · INDEX {c['index_rows']} 行（{c['index_numbers']} 编号）"
          f" · 磁盘 {c['disk_files']} 文件（{c['disk_numbers']} 编号）· 最大编号 R{c['max_number']}")
    print(f"[同号组] {len(res['dup_groups'])} 组")
    for n, g in res["dup_groups"].items():
        print(f"  R{n}: {g['kind']}  → {ADVICE.get(g['kind'], '')}")
        for i in g["files"]:
            print(f"     {i['sha'][:12]} {i['bytes']:>7}B {i['lines']:>4}行 {i['name']}")
        for pair, pc in g["pairwise"].items():
            print(f"     差集 {pair}: only_a={pc['only_a']} only_b={pc['only_b']} common={pc['common']}")
    cov = res["coverage"]
    print(f"[覆盖] 有 history 编号但归档无正文 {cov['no_body_for_history']} 个 · 有正文但 history 无编号行 "
          f"{len(cov['no_history_for_file'])} {cov['no_history_for_file']}")
    print(f"       INDEX-history 差 {len(cov['index_minus_history'])} {cov['index_minus_history'][:10]}")
    print(f"       history-INDEX 差 {len(cov['history_minus_index'])} {cov['history_minus_index'][:10]}")
    print(f"       编号空洞 {len(cov['number_holes'])} {cov['number_holes'][:20]}")
    if res["soft"]:
        print(f"[提示 {len(res['soft'])}]")
        for x in res["soft"]:
            print(f"  - {x}")
    if res["findings"]:
        print(f"[发现 {len(res['findings'])}]")
        for x in res["findings"]:
            print(f"  ! {x}")
    else:
        print("[发现 0] 所有硬性检查通过")
    print(f"[结论] rc={res['rc']}  ({'PASS' if res['rc'] == 0 else 'FINDINGS'})")


# ---------------------------------------------------------------- selftest
FIXTURE = os.path.join(GA, "temp", "archive_integrity_fixture")


def _mk(d, name, text):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


def build_fixture(root: str, kind: str) -> str:
    d = os.path.join(root, kind)
    if os.path.isdir(d):
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
    else:
        os.makedirs(d)
    hist = ["R1 | 2026-01-01 | 产出 | 甲 | 结论甲", "R2 | 2026-01-02 | 产出 | 乙 | 结论乙"]
    idx = ["# 自主报告归档清单", "",
           "| R1 | ✅正文 | 甲 | 10B R1_a.md | 摘要 |",
           "| R2 | ✅正文 | 乙 | 10B R2_b.md | 摘要 |"]
    files = {"R1_a.md": "# 甲\n内容甲\n", "R2_b.md": "# 乙\n内容乙\n"}
    if kind == "dup_same":                       # 同号同内容（纯改名对）⇒ 不属硬 finding
        files["R2_b_copy.md"] = files["R2_b.md"]
    elif kind == "dup_conflict":                 # 同号不同内容且互不包含 ⇒ 硬 finding
        files["R2_zzz.md"] = "# 完全不同的另一篇\n" + ("别的正文\n" * 40)
    elif kind == "del_history_row":              # history 少一行 ⇒ 硬 finding
        hist = hist[:1]
    elif kind == "del_file":                     # INDEX 指向的文件被删 ⇒ 硬 finding
        files.pop("R2_b.md")
    elif kind == "declared_offsite":             # 自声明未落盘的归档外路径 ⇒ 不应报硬发现
        hist = hist + ["R3 | 2026-01-03 | 产出 | 丙 | 结论丙（正文未落盘）"]
        idx = idx + ["| R3 | ✅正文(未落盘) | 丙 | 100B /tmp/R3_off.md | 摘要 |"]
    elif kind == "clean":
        pass
    else:
        raise ValueError(kind)
    for n, t in files.items():
        _mk(d, n, t)
    _mk(d, "history.txt", "\n".join(hist) + "\n")
    _mk(d, "INDEX.md", "\n".join(idx) + "\n")
    return d


def selftest():
    os.makedirs(FIXTURE, exist_ok=True)
    cases = [
        ("clean",            0, None,                2, 0),   # rc, 必含 finding 关键词, 期望命中报告数, 期望同号组数
        ("dup_same",         0, None,                3, 1),
        ("dup_conflict",     1, "真内容冲突",         3, 1),
        ("del_history_row",  1, "history 无编号行",   2, 0),
        ("del_file",         1, "指向不存在",         1, 0),
        ("declared_offsite", 0, None,                2, 0),
    ]
    ok = 0
    for kind, exp_rc, kw, exp_files, exp_dups in cases:
        d = build_fixture(FIXTURE, kind)
        res = detect(d)
        got_files = res["scan"]["matched_reports"]
        got_dups = len(res["dup_groups"])
        hit_kw = (kw is None) or any(kw in f for f in res["findings"])
        passed = (res["rc"] == exp_rc and hit_kw and got_files == exp_files and got_dups == exp_dups)
        ok += passed
        print(f"[selftest] {kind:<16} rc={res['rc']} (期望 {exp_rc})  命中报告={got_files}"
              f" (期望 {exp_files})  同号组={got_dups} (期望 {exp_dups})"
              f"  关键词{'命中' if hit_kw else '未命中'}  => {'PASS' if passed else 'FAIL'}")
        if not passed:
            for f in res["findings"]:
                print(f"      finding: {f[:120]}")
    print(f"[selftest] {ok}/{len(cases)} 用例通过（夹具在 {FIXTURE}，真实归档未被触碰）")
    return 0 if ok == len(cases) else 1


def main():
    ap = argparse.ArgumentParser(description="归档完整性只读检测器")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="归档目录（只读）")
    ap.add_argument("--json", default=None, help="另存 JSON 结论（建议放 temp/）")
    ap.add_argument("--selftest", action="store_true", help="跑夹具负控自测")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        res = detect(a.dir)
    except FileNotFoundError as e:
        print(f"[error] 目录不存在: {e}", file=sys.stderr)
        return 2
    print_report(res)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"[json] 已写 {a.json}")
    return res["rc"]


if __name__ == "__main__":
    sys.exit(main())
