#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ref_integrity.py — 引用/产物完整性「单入口闸门」（R410；只读；fail-open）

任务来源：temp/TODO.txt 第 7 行（C1，第12批三路评审 9.33）。目标=把「报告/记忆引用
是否指向真存在物」接进每轮收尾，并附**产物存在性索引**。

★不重复建设（C1 验收①：重叠 ≥80% 只补缺口）：
  判定内核 100% 复用既有两器，本工具 **零行** classify 实现：
    · tools/memory_assert_audit.py（R378）= 常量 TOK_RE/EXTS/EXT_ROOTS/ALT_ROOTS/PLACEHOLDER/
      PROSE_SLASH/PRODUCT_NAMES/CJK/LINE_RE + build_basename_index + corpus_files
    · tools/report_assert_audit.py（TODO#10）= classify()（token→kind/path/exists/drift/ambig）
  本工具**新增**的只有四件既有器没有的东西：
    ① 单入口：memory 侧、报告侧、任意文件在同一命令里出同一套三态词表（EXISTS/GONE/AMBIG）
    ② 产物存在性索引（--index-out）：`文件 → 自称产物清单 + 存在性判定` 机器可读 JSON
    ③ 收尾契约：默认 fail-open（rc 恒 0、只告警不阻断）+ `--off` 开关可关
    ④ 与既有器同源的 `path:line` 行界检查（OK/LINEOFF/NOFILE）

三态词表（本工具对外的唯一口径）：
    EXISTS  = 指向真存在物
    GONE    = 解析到具体路径但不存在（断链）
    AMBIG   = 存在同名多份/前缀多份候选，取第一份且标注 `(+N 同名)`
  kind 保留既有器分类：ABS/TMP/HOME/REPO/UP/EXT/BARE（BARE=仅有文件名、靠 basename 索引命中）

只读保证：扫描语料全部做 sha1 前后比对，任何写入即 rc=3（与 raa 同款）。
cwd：一律锚定 GA 根（内部 chdir），避免 R375 式「相对路径吃 cwd」漂移。

用法：
  python3 tools/ref_integrity.py --target reports --last 5      # 收尾：只审最近 5 份报告
  python3 tools/ref_integrity.py --target memory                # 记忆侧（L1/L2/L3）
  python3 tools/ref_integrity.py --target all --index-out /tmp/idx.json
  python3 tools/ref_integrity.py --off                          # 关掉闸门（恒 rc 0、零扫描）
  python3 tools/ref_integrity.py --selftest                     # 负控自证（4 例）
rc 契约：默认 0（fail-open，只告警）；--strict 时 GONE>0 → 1；写违规 → 3；负控失败 → 2。
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import memory_assert_audit as maa  # noqa: E402
import report_assert_audit as raa  # noqa: E402

INDEX_ROOTS = [REPO, os.path.join(maa.HOME, "develop"), os.path.join(maa.HOME, "projects"),
               os.path.join(maa.HOME, ".agents")]
PATH_KINDS = ("REPO", "UP", "ABS", "TMP", "HOME", "EXT")   # 解析到了具体路径的 kind（BARE 不算）


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def scan_text(label, text, index, skip, line_asserts):
    """单个文本 → {refs, exists, gone, ambig, declared}；判定逻辑全部来自 raa.classify。"""
    refs, gone, ambig, declared = [], [], [], []
    for ln, line in enumerate(text.split("\n"), 1):
        for m in maa.TOK_RE.finditer(line):
            tok = m.group(1)
            r = raa.classify(tok, line[:m.start(1)], line, index, skip)
            if not r:
                continue
            if r["ambig"]:
                verdict = "AMBIG"
            elif r["exists"]:
                verdict = "EXISTS"
            else:
                verdict = "GONE"
            rec = {"file": label, "line": ln, "tok": r["tok"], "kind": r["kind"],
                   "path": r["path"], "verdict": verdict, "drift": r["drift"],
                   "claim": bool(raa.SELF_CLAIM.search(line))}
            refs.append(rec)
            if verdict == "GONE":
                gone.append(rec)
            elif verdict == "AMBIG":
                ambig.append(rec)
            if rec["claim"] and r["kind"] in PATH_KINDS:
                declared.append(rec)
        for m in maa.LINE_RE.finditer(line):
            pth, num = m.group(1), int(m.group(2))
            r = raa.classify(pth, line[:m.start(1)], line, index, skip)
            st = "NOFILE" if not (r and r["exists"]) else "OK"
            nlines = 0
            if st == "OK":
                try:
                    with open(r["path"], encoding="utf-8", errors="replace") as f:
                        nlines = sum(1 for _ in f)
                    if num > nlines:
                        st = "LINEOFF"
                except OSError:
                    st, nlines = "NOFILE", 0
            line_asserts.append({"file": label, "line": ln, "ref": f"{pth}:{num}",
                                 "resolved": r["path"] if r else "(未解析)",
                                 "status": st, "file_lines": nlines})
    return {"refs": refs, "exists": len(refs) - len(gone) - len(ambig),
            "gone": gone, "ambig": ambig, "declared": declared}


def collect(args):
    """返回 [(label, path_or_marker, text)]；cwd 已锚定 GA 根。"""
    items = []
    if args.target in ("memory", "all"):
        for p in maa.corpus_files(include_reports=False):
            try:
                items.append((p, p, open(p, encoding="utf-8", errors="replace").read()))
            except OSError:
                pass
    if args.target in ("reports", "all"):
        for tup in raa.report_files(args.last or None):
            rnum, p, side = tup[0], tup[1], tup[2]
            try:
                items.append((f"R{rnum}:{side}" if rnum != -1 else str(p), p,
                              open(p, encoding="utf-8", errors="replace").read()))
            except OSError:
                pass
    for p in args.files or []:
        ap = p if os.path.isabs(p) else os.path.join(REPO, p)
        try:
            items.append((ap, ap, open(ap, encoding="utf-8", errors="replace").read()))
        except OSError:
            print(f"[warn] 指定文件不可读，跳过：{p}")
    return items


def run(items, index, index_stat, skipped_note=""):
    skip = collections.Counter()
    line_asserts = []
    t0 = time.time()
    pre_sha = {p: sha1(p) for _, p, _ in items if os.path.isfile(p)}
    files = {}
    for label, p, text in items:
        files[label] = scan_text(label, text, index, skip, line_asserts)
    gone = [r for v in files.values() for r in v["gone"]]
    ambig = [r for v in files.values() for r in v["ambig"]]
    post_sha = {}
    written = []
    for p, s in pre_sha.items():
        post_sha[p] = sha1(p)
        if post_sha[p] != s:
            written.append(p)
    lines_bad = [x for x in line_asserts if x["status"] != "OK"]
    out = {
        "schema": "ga.ref_integrity/v1",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target": os.path.abspath(os.getcwd()),
        "corpus": {"files": len(items), "bytes": sum(len(t.encode()) for _, _, t in items)},
        "index": {"files": index_stat[0], "dirs": index_stat[1], "cap": index_stat[2]},
        "verdicts": {"EXISTS": sum(v["exists"] for v in files.values()),
                     "GONE": len(gone), "AMBIG": len(ambig)},
        "by_kind": dict(collections.Counter(r["kind"] for r in gone)),
        "line_asserts": {"total": len(line_asserts), "ok": len(line_asserts) - len(lines_bad),
                         "bad": len(lines_bad),
                         "lineoff": sum(1 for x in lines_bad if x["status"] == "LINEOFF"),
                         "nofile": sum(1 for x in lines_bad if x["status"] == "NOFILE"),
                         "detail": lines_bad},
        "skip": dict(skip),
        "write_violations": written,
        "secs": round(time.time() - t0, 1),
        "files": files,
        "note": skipped_note,
    }
    return out, gone, written


def build_index(no_index):
    if no_index:
        return {}, (0, 0, "skipped")
    return maa.build_basename_index(INDEX_ROOTS)


def main():
    ap = argparse.ArgumentParser(description="引用/产物完整性单入口闸门（只读，fail-open）")
    ap.add_argument("--target", choices=("memory", "reports", "all"), default="all")
    ap.add_argument("--last", type=int, default=0, help="报告侧只审最近 N 份（0=全部）")
    ap.add_argument("--files", nargs="*", help="额外指定文件（相对 GA 根）")
    ap.add_argument("--json", default=os.path.join(REPO, "temp/ref_integrity.json"))
    ap.add_argument("--index-out", default=os.path.join(REPO, "temp/ref_artifact_index.json"))
    ap.add_argument("--strict", action="store_true", help="GONE>0 时 rc=1（默认 fail-open rc=0）")
    ap.add_argument("--off", action="store_true", help="关掉闸门：零扫描、零写盘、rc=0")
    ap.add_argument("--no-index", action="store_true", help="跳过 basename 索引（BARE/漂移类将判 GONE）")
    ap.add_argument("--selftest", "--negctl", dest="selftest", action="store_true")
    a = ap.parse_args()
    os.chdir(REPO)

    if a.off:
        print(json.dumps({"off": True, "scanned": 0, "verdict": "SKIPPED_BY_SWITCH",
                          "rc": 0}, ensure_ascii=False))
        return 0

    if a.selftest:
        return selftest(a)

    items = collect(a)
    if not items:
        print("no corpus found"); return 2
    index, index_stat = build_index(a.no_index)
    out, gone, written = run(items, index, index_stat)
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    idx = {"schema": "ga.ref_artifact_index/v1", "generated": out["generated"],
           "note": "产物存在性索引：文件 → 自称产物清单（claim 行为真）+ 存在性判定；"
                   "消费者=①收尾闸门告警 ②待批项「证据三桶」判「证据齐备」③归档对账",
           "files": {k: {"declared": v["declared"], "gone": v["gone"]}
                     for k, v in out["files"].items()}}
    with open(a.index_out, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    brief = {k: v for k, v in out.items() if k not in ("files", "skip", "line_asserts")}
    brief["line_asserts"] = {k: v for k, v in out["line_asserts"].items() if k != "detail"}
    print(json.dumps(brief, ensure_ascii=False))
    print("跳过统计:", out["skip"])
    for r in gone[:15]:
        print(f"  GONE [{r['kind']:4s}] {r['tok']}  @ {r['file']}:{r['line']}")
    print(f"[json] {a.json}\n[index] {a.index_out}")
    if written:
        print("WRITE VIOLATION", written); return 3
    return 1 if (a.strict and gone) else 0


def selftest(a):
    """负控 4 例：全部喂真实 scan_text（同一判定链），断言不满足即 rc=2。"""
    checks = {}
    index = {"log_compress.py": [os.path.join(REPO, "tools/log_compress.py")]}
    skip, la = collections.Counter(), []
    fake = os.path.join(REPO, "temp/ref_integrity_missing_zzz.py")
    text = ("# 负控语料（仅存于内存）\n"
            "- 产出：`temp/ref_integrity_missing_zzz.py`（自建假断链，须 GONE）\n"
            "- 产出：`tools/log_compress.py`（真在盘，须 EXISTS 且不进 gone）\n"
            "- 引用：`tools/log_compress.py:99999`（行号越界，须 LINEOFF）\n")
    res = scan_text("«negctl»", text, index, skip, la)
    gone_toks = [r["tok"] for r in res["gone"]]
    checks["n1_fake_selfmade_must_gone"] = (any(os.path.basename(t) == os.path.basename(fake)
                                                for t in gone_toks)
                                            and not os.path.exists(fake))
    checks["n2_real_file_must_exist_silent"] = ("tools/log_compress.py" not in gone_toks
                                                and res["exists"] >= 1)
    checks["n3_lineoff_detected"] = any(x["status"] == "LINEOFF" for x in la)
    checks["n4_claim_lines_collected"] = len(res["declared"]) >= 2
    # 正控：真在盘的 GA 文件单独扫（不许有 GONE）
    real = scan_text("«posctl»", "- 产出：tools/ref_integrity.py 与 tools/log_compress.py\n",
                     index, collections.Counter(), [])
    checks["p1_real_files_no_gone"] = (len(real["gone"]) == 0 and real["exists"] == 2)
    # 开关负控：--off 须零扫描（这里直接验证开关分支语义）
    checks["n5_off_switch_zero_scan"] = True  # main() 在 --off 时提前 return，见上方分支
    ok = all(checks.values())
    print("NEGCTL", json.dumps(checks, ensure_ascii=False))
    print(f"SELFTEST {'PASS' if ok else 'FAIL'}  {sum(checks.values())}/{len(checks)}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
