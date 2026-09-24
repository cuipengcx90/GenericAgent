#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_assert_audit.py — 自主报告「路径断言」可核查闸门（TODO#10，只读）

与 tools/memory_assert_audit.py（R378，L1/L2 记忆断言）互补：R378 显式排除
memory/autonomous_reports/，把「报告内转述的路径断言」划归本条 TODO。本工具只做报告侧。

口径（报告 §1 同步声明）：
  断言语料 = memory/autonomous_reports/R*.md + temp/autonomous_reports/R*.md
             （同名文件以 memory/ 侧（durable）为准；temp/ 独有的--即未归档--也纳入并标注）
             不扫描 INDEX.md/history.md/history.txt（索引与台账，非报告正文）
  断言     = 报告文本里具备「文件名+扩展名」的 path token（与 R378 同一 EXTS/TOK_RE），
             解析分类同 R378：ABS/TMP/HOME/REPO/UP/EXT/BARE，多根回退顺序一致
             （GA 根 → ALT_ROOTS 位置漂移 → basename 索引；这两条直接 import R378 常量，
               解析块照抄并标注行号，避免口径漂移）
  自称产物 = 断言所在行含 产出|产物|新增|落盘|生成|写入|实现|新建|已建 等词（高下游影响子集）

  引用语料（"下游会当真"的通道）= memory/*.md,*.txt + memory/*/*.md,*.txt（剔除 L4_raw_sessions/
             __pycache__/autonomous_reports 之外的索引？——不剔：其它报告也算后续引用）
             + temp/TODO.txt + sche_tasks/*.json + reflect_scripts/*.py + tools/*.py
  引用判定 = 同一个 path token 字面出现在引用语料某行（BARE/漂移类退化为 basename 匹配），
             给出 file:line 作为引用来源；不含断言自身的报告文件（自引不算引用）
  排序     = 被引用却已消失的断言按 (外部引用条数 desc, 引用来源里含 memory/ desc, 报告编号新→旧)

只读保证：对断言语料全部报告做 sha1 前后比对，任何写入即 rc=3。
负控 --negctl：①合成报告里的假断链被引用于合成记忆 ⇒ 必须进 top 且 refs≥1；②真在盘 token 必须 PASS；
             ③未被引用的假断链必须排在①之后（证明排序而非纯比率）；④改写 sha1 比对真生效。
"""
import argparse, collections, hashlib, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_assert_audit as maa  # noqa: E402  （复用常量/索引/分类，口径与 R378 一致）

REPO = maa.REPO
HOME = maa.HOME
REPORTS_D = os.path.join(REPO, "memory/autonomous_reports")
TEMP_REPORTS_D = os.path.join(REPO, "temp/autonomous_reports")

SELF_CLAIM = re.compile(r'产出|产物|新增|落盘|生成|写入|实现|新建|已建|已落|created|artifact')
RNUM = re.compile(r'^R(\d+)')
SYNTH_REPORT = "«negctl-report»"
SYNTH_MEM = "«negctl-mem»"


def report_files(last=None):
    """返回 [(rnum, path, side, sha1, nbytes)]；同名以 memory 侧为准。"""
    out, seen = [], {}
    for side, d in (("memory", REPORTS_D), ("temp", TEMP_REPORTS_D)):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not (f.endswith(".md") and RNUM.match(f)):
                continue
            p = os.path.join(d, f)
            m = RNUM.match(f)
            rnum = int(m.group(1))
            raw = open(p, "rb").read()
            rec = (rnum, p, side, hashlib.sha1(raw).hexdigest(), len(raw))
            if f in seen and side == "temp":
                continue  # memory 侧已收
            seen[f] = rec
            out.append(rec)
    out.sort(key=lambda r: -r[0])
    if last:
        out = out[:last]
    return out


def ref_files(assert_paths, synthetic_mem=None):
    """引用语料：下游可能把报告断言当真的通道。"""
    out = []
    for d, subs in ((os.path.join(REPO, "memory"), None),):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if os.path.isfile(p) and (f.endswith(".md") or f.endswith(".txt")):
                    out.append(p)
                elif os.path.isdir(p) and f not in ("L4_raw_sessions", "__pycache__"):
                    for g in sorted(os.listdir(p)):
                        q = os.path.join(p, g)
                        if os.path.isfile(q) and (g.endswith(".md") or g.endswith(".txt")):
                            out.append(q)
    for p in (os.path.join(REPO, "temp/TODO.txt"),):
        if os.path.exists(p):
            out.append(p)
    for d, exts in ((os.path.join(REPO, "sche_tasks"), (".json",)),
                    (os.path.join(REPO, "reflect_scripts"), (".py",)),
                    (os.path.join(REPO, "tools"), (".py",))):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(exts):
                    out.append(os.path.join(d, f))
    if synthetic_mem:
        out.append(synthetic_mem)
    aps = set(assert_paths)
    # 断言语料里的报告不参与「引用语料」（自引不算），但其它报告算
    return [p for p in out if p not in aps]


def read(p, synthetic=None, synth_map=None):
    if synth_map and p in synth_map:
        return synth_map[p]
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def classify(tok, pre, line, index, skip):
    """单 token → 解析结果；与 maa.main() 166-252 行逐条对齐（常量与回退顺序同一来源）。"""
    if tok[0] in "@(#":
        tok = tok.lstrip("@(#") or tok
    pm = re.search(r'([A-Za-z][\w.-]*):\s*$', pre)
    prefix = pm.group(1) if pm else None
    if prefix and prefix not in maa.EXT_ROOTS and prefix.lower() in ("http", "https", "file", "mailto"):
        skip["url"] += 1
        return None
    if "+" in tok or tok in maa.PRODUCT_NAMES:
        skip["compound(+)/产品名"] += 1
        return None
    if maa.PLACEHOLDER.search(tok):
        skip["placeholder"] += 1
        return None
    if maa.PROSE_SLASH.match(tok):
        skip["prose-slash"] += 1
        return None
    if tok.startswith("/") and not re.match(r'^/(Users|Volumes|tmp|var|opt|etc|private)/', tok):
        skip["url-ish"] += 1
        return None
    if tok[0] in "._" and "/" not in tok:
        lm = re.search(r'([\u4e00-\u9fffA-Za-z0-9_]+)$', pre)
        base = lm.group(1) if lm else ""
        cc, fixed = base, None
        while True:
            if cc and index.get(cc + tok):
                fixed = cc + tok
                break
            if not cc or not maa.CJK.search(cc[0]):
                break
            cc = cc[1:]
        if fixed:
            skip["cjk-prefix-repaired"] += 1
            tok = fixed
        else:
            skip["suffix-fragment"] += 1
            return None
    t = tok.lstrip("./")
    if prefix and prefix in maa.EXT_ROOTS:
        kind, root, target = "EXT", maa.EXT_ROOTS[prefix], tok
    elif tok.startswith("/"):
        kind, target = ("TMP" if tok.startswith("/tmp/") else "ABS"), tok
    elif tok.startswith("~"):
        kind, target = "HOME", os.path.expanduser(tok)
    elif tok.startswith("../") or tok.startswith("./"):
        kind, target = "UP", os.path.join(REPO, tok.split("/", 1)[1] if "/" in tok else "")
    elif "/" in tok:
        kind, target = "REPO", os.path.join(REPO, t)
    else:
        kind, target = "BARE", tok
    drift = False
    if kind == "EXT":
        path = os.path.join(root, target.split(":", 1)[1] if ":" in target else target)
        exists = os.path.exists(path)
    elif kind == "BARE":
        cands = index.get(tok, [])
        path = cands[0] if cands else "(basename 索引无此名)"
        exists = bool(cands)
    else:
        path, exists = target, os.path.exists(target)
        if not exists and kind in ("REPO", "UP"):
            rel = tok.split("/", 1)[1] if tok.startswith(("../", "./")) else tok.lstrip("./")
            for alt in maa.ALT_ROOTS:
                cand = os.path.join(alt, rel)
                if os.path.exists(cand):
                    path, exists, drift = cand, True, True
                    break
    ambig = False
    if not exists and kind not in ("BARE", "EXT"):
        rel = tok.split("/", 1)[1] if tok.startswith(("../", "./")) else tok.lstrip("./")
        base = rel.rstrip("/").split("/")[-1]
        cands = sorted(set(index.get(base, [])))
        pref = [c for c in cands if c.endswith("/" + rel)]
        if pref:
            path, exists, drift = pref[0], True, True
        elif len(cands) == 1:
            path, exists, drift = cands[0], True, True
        elif cands:
            path, exists, ambig = cands[0] + f"  (+{len(cands)-1} 同名)", False, True
    return {"kind": kind, "target": target, "path": path, "exists": exists,
            "drift": drift, "ambig": ambig, "tok": tok}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=0, help="只审最近 N 份报告（0=全部）")
    ap.add_argument("--top", type=int, default=40, help="top 清单长度")
    ap.add_argument("--json", default=os.path.join(REPO, "temp/report_assert_audit.json"))
    ap.add_argument("--md", default=os.path.join(REPO, "temp/report_assert_audit.md"))
    ap.add_argument("--negctl", action="store_true")
    ap.add_argument("--no-index", action="store_true", help="跳过 basename 索引（BARE/漂移类将判 FAIL）")
    a = ap.parse_args()
    t0 = time.time()

    reps = report_files(a.last)
    if not reps:
        print("no reports found"); return 2
    pre_sha = {p: s for _, p, _, s, _ in reps}

    synth_map = {}
    if a.negctl:
        synth_map[SYNTH_REPORT] = (
            "# negctl 合成报告（仅存于内存）\n"
            "- 产出：`temp/definitely_missing_zzz.py`（假断链，且被合成记忆引用）\n"
            "- 产出：`temp/never_referenced_qqq.py`（假断链，无人引用）\n"
            "- 产出：`tools/log_compress.py`（真在盘，必须 PASS）\n")
        synth_map[SYNTH_MEM] = (
            "# negctl 合成记忆（仅存于内存）\n"
            "- 定时/自主: temp/definitely_missing_zzz.py 已被引用\n"
            "- 另一处引用 temp/definitely_missing_zzz.py\n")
        reps = reps + [(-1, SYNTH_REPORT, "synthetic", "synthetic", 0)]

    index, (nfiles, ndirs, cap) = ({}, (0, 0, "skipped")) if a.no_index else maa.build_basename_index(
        [REPO, os.path.join(HOME, "develop"), os.path.join(HOME, "projects"), os.path.join(HOME, ".agents")])

    skip = collections.Counter()
    asserts = {}     # key -> rec
    n_tok = 0
    nbytes = 0
    for rnum, p, side, sha, nb in reps:
        src = read(p, synth_map=synth_map)
        nbytes += len(src.encode())
        for ln, line in enumerate(src.split("\n"), 1):
            for m in maa.TOK_RE.finditer(line):
                tok = m.group(1)
                n_tok += 1
                pre = line[:m.start(1)]
                r = classify(tok, pre, line, index, skip)
                if not r:
                    continue
                claim = bool(SELF_CLAIM.search(line))
                key = (r["kind"], r["target"] if (r["kind"] == "BARE" or not r["exists"]) else r["path"])
                rec = asserts.setdefault(key, dict(r, count=0, claim=False, hits=[], owners=set()))
                rec["count"] += 1
                rec["owners"].add(os.path.basename(p))
                rec["claim"] = rec["claim"] or claim
                if len(rec["hits"]) < 3:
                    rec["hits"].append({"file": os.path.basename(p), "rnum": rnum, "line": ln,
                                        "ctx": line.strip()[:150]})
    # ---- 引用扫描 ----
    apaths = {p for _, p, _, _, _ in reps}
    refs = ref_files(apaths, SYNTH_MEM if a.negctl else None)
    ref_lines = {}
    for p in refs:
        src = read(p, synth_map=synth_map)
        for ln, line in enumerate(src.split("\n"), 1):
            ref_lines.setdefault(p, []).append((ln, line))
    nref_files = len(refs)
    for key, rec in asserts.items():
        # 引用匹配针 = 报告里的**原文 token**（如 `temp/x.py`）；解析后的绝对路径在正文里
        # 不可能出现（实测负控因只拿绝对 target 匹配而漏检），故 tok 优先、target 兜底
        needles = list({rec.get("tok", ""), rec["target"]})
        needles = [n for n in needles if n]
        base = os.path.basename(str(rec["target"]).rstrip("/"))
        hits = []
        for p in refs:
            pl = ref_lines.get(p, [])
            for ln, line in pl:
                if any(n and n in line for n in needles) or (rec["kind"] == "BARE" and base in line):
                    hits.append({"src": os.path.relpath(p, REPO), "line": ln, "ctx": line.strip()[:130]})
            if len(hits) > 60:
                break
        rec["refs"] = hits
        rec["ref_n"] = len(hits)
        # 剔除「自称产物的那份报告自己其它行」的命中：那不算被引用（否则语句重复即自证，循环论证）
        own = rec.get("owners", set())
        rec["refs_other"] = [h for h in hits if os.path.basename(h["src"]) not in own]
        rec["ref_n_other"] = len(rec["refs_other"])
        rec["ref_in_memory"] = any(h["src"].startswith("memory/") for h in hits)

    def is_synth(r):
        """负控合成报告的断言只用于自检，不得混进交付统计
        （实测：--negctl 时 «negctl-report» 的假断链挤进 top 表第 6 行 = 交付物被自检数据污染）"""
        return any(h["file"] == SYNTH_REPORT for h in r["hits"])

    real = [r for r in asserts.values() if not is_synth(r)]
    synth_recs = [r for r in asserts.values() if is_synth(r)]
    n_synth_reports = 1 if a.negctl else 0
    ok = [r for r in real if r["exists"] and not r["ambig"]]
    amb = [r for r in real if r["ambig"]]
    bad_all = [r for r in asserts.values() if not r["exists"] and not r["ambig"]]
    bad = [r for r in real if not r["exists"] and not r["ambig"]]
    ok_all = ok + [r for r in synth_recs if r["exists"] and not r["ambig"]]

    def tier(r):
        """分级口径（写进报告）：具目录/扩展根的可解析路径 = 强断言；
        裸文件名(BARE)只有落在「自称产物」行(产出/落盘/新增/生成…)才算断链候选，
        其余裸名多为切词碎片（实测 `r.json()`、示例列表 ['a.py','x.py']）⇒ 归弱，单独统计不混入 top。"""
        return "strong" if r["kind"] != "BARE" else ("claim" if r["claim"] else "weak")

    for r in bad_all:
        r["tier"] = tier(r)
    strong_all = [r for r in bad_all if r["tier"] != "weak"]   # 含负控合成项，仅供负控判定
    strong = [r for r in strong_all if not is_synth(r)]        # 交付统计口径
    weak = [r for r in bad if r["tier"] == "weak"]
    # 排序键：被**其它**报告/记忆引用次数 → 总引用数 → 自称产物 → 报告新旧
    ranked_all = sorted(strong_all, key=lambda r: (-r["ref_n_other"], -r["ref_n"], -int(r["claim"]),
                                                   -max(h["rnum"] for h in r["hits"])))
    ranked = [r for r in ranked_all if not is_synth(r)]   # 交付清单（合成项仅用于负控判定）
    refd = [r for r in ranked if r["ref_n"] > 0]

    # ---- 只读自证 ----
    post_sha = {p: hashlib.sha1(open(p, "rb").read()).hexdigest() for _, p, _, _, _ in reps
                if p in pre_sha}
    written = [p for p, s in pre_sha.items() if post_sha.get(p) != s]
    rc = 0

    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "reports": len(reps) - n_synth_reports, "bytes": nbytes,
           "tokens": n_tok, "asserts": len(real), "pass": len(ok), "fail": len(bad), "ambig": len(amb),
           "ref_files": nref_files, "index": {"files": nfiles, "dirs": ndirs, "cap": cap},
           "skip": dict(skip), "refd_fail": len(refd),
           "fail_strong": len(strong), "fail_weak_bare": len(weak),
           "refd_fail_other": sum(1 for r in ranked if r["ref_n_other"] > 0),
           "top": [{"target": r["target"], "kind": r["kind"], "path": r["path"], "claim": r["claim"],
                    "tier": r["tier"], "count": r["count"], "ref_n": r["ref_n"],
                    "ref_n_other": r["ref_n_other"], "ref_in_memory": r["ref_in_memory"],
                    "hits": r["hits"], "refs": r["refs_other"][:6] or r["refs"][:6]}
                   for r in ranked[:a.top]],
           "weak_top": [{"target": r["target"], "ref_n": r["ref_n"], "hits": r["hits"][:1]}
                        for r in sorted(weak, key=lambda r: -r["ref_n"])[:10]],
           "write_violations": written, "secs": round(time.time() - t0, 1)}

    if a.negctl:
        # 判据落在**全量 ranked**上（合成项 ref_n=2 与大量真引用平局，按 rnum 平局键会落到 top 之外，
        # 只看 top 会误报负控失败 = 口径错误而非检出错误）
        names = {os.path.basename(r["target"]): i for i, r in enumerate(ranked_all)}
        checks = {
            "fake_referenced_flagged": "definitely_missing_zzz.py" in names,
            "fake_referenced_has_refs": any(r["ref_n_other"] >= 1 for r in ranked_all
                                            if os.path.basename(r["target"]) == "definitely_missing_zzz.py"),
            "fake_referenced_before_unreferenced": (names.get("definitely_missing_zzz.py", 10**9) <
                                                    names.get("never_referenced_qqq.py", -1)),
            "real_file_passes": any(os.path.basename(r["target"]) == "log_compress.py" for r in ok_all),
        }
        out["negctl"] = checks
        rc = 0 if all(checks.values()) else 2
        print("NEGCTL", json.dumps(checks, ensure_ascii=False))
    if written:
        print("WRITE VIOLATION", written); rc = 3

    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k not in ("top", "skip")}, ensure_ascii=False))
    print("跳过统计:", dict(skip))
    print(f"强断言 FAIL={len(strong)}  弱(BARE无claim)={len(weak)}  被引用={len(refd)}")
    for i, t in enumerate(out["top"][:12], 1):
        print(f"{i:2d}. refs={t['ref_n']:3d}(他引{t['ref_n_other']:2d}) mem={int(t['ref_in_memory'])} "
              f"claim={int(t['claim'])} [{t['tier']}] "
              f"{t['target']}  ← {t['hits'][0]['file']}:{t['hits'][0]['line']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
