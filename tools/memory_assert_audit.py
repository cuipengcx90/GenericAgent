#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_assert_audit.py — L1/L2 记忆「路径与文件断言」机械核验器（只读）

设计口径（报告 §2 同步声明）：
  语料 = memory/*.md + memory/*.txt + memory/*/*.md + memory/*/*.txt
         排除 memory/autonomous_reports/（报告内转述的路径断言 → 归 TODO#10）
         排除 memory/L4_raw_sessions/、__pycache__
  断言 = 具备「文件名+扩展名」的 token（`.py/.md/.txt/.json/.jsonl/.sh/.csv/.zip/.toml/.yaml/.log/.db/.plist/.pa` 等），
         且不在跳过规则内。纯叙述句（无路径 token）自动跳过——跳过理由逐类计数打印。

  解析类别（kind）：
    ABS   /Users/... /Volumes/... 等绝对路径
    TMP   /tmp/... 等易失绝对路径
    HOME  ~/... 家目录
    REPO  仓库相对（tools/x.py、temp/x.py、memory/x.md、sche_tasks/...）
    UP    ../x.py（记忆内约定：`../` 相对 memory/ 目录 ⇒ GA 根）
    EXT   repo前缀:路径（如 herdr-agents:docs/...）
    BARE  裸文件名（无目录）⇒ 弱断言，需 basename 索引判定

  结果：PASS / FAIL（FAIL 再分 F1..F5，见 --md 输出「判据」节）；LINEOFF 单列（path:NN 行号断言）。
  负控：--negctl 要求 3 个已知真断链必须 FAIL，否则 rc=2；同时注入 1 真 PASS + 1 假路径做双向自检。
  只读：本脚本不写任何 memory 文件。
"""
import argparse, json, os, re, sys, collections

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HOME = os.path.expanduser("~")
EXTS = ("py md txt json jsonl sh csv zip toml yaml yml log db sqlite sqlite3 plist pa png html js mjs mp3 wav".split())
TOK_RE = re.compile(r'(?<![A-Za-z0-9./~-])((?:~|\.{0,2})/?(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+\.(?:' + "|".join(EXTS) + r'))(?![A-Za-z0-9])')
PLACEHOLDER = re.compile(r'(X{2,}|x{2,}|Y{2,}|N{2,}|R\d*X|\*|\{|\}|<|>|\.\.\.|…|\$\{|yy|xx|(^|/)0x\.)')
# 散文里的「文件名/文件名」（如 AGENTS.md/CLAUDE.md 表示"或"）不是路径，须跳过
PROSE_SLASH = re.compile(r'^[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]{1,8}/[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]{1,8}$')
# 形如 xxx.js 的产品/框架名，不是本机文件断言
PRODUCT_NAMES = {"Next.js", "Node.js", "Vue.js", "React.js", "Nuxt.js", "Three.js", "D3.js",
                 "Express.js", "Svelte.js", "Solid.js", "Deno.js", "Bun.js", "Chart.js"}
CJK = re.compile(r'[\u4e00-\u9fff]')
LINE_RE = re.compile(r'((?:~|\.{0,2})/?(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+\.(?:py|md|json|sh|txt|yaml|yml|toml))[：:](\d{1,5})\b')

EXT_ROOTS = {
    "herdr-agents": os.path.join(HOME, "develop", "herdr-agents"),
    "GA": REPO, "genericagent": REPO,
}
# GA 根之外，记忆里常见的相对路径真实归属根（命中即判「位置漂移」而非断链）
ALT_ROOTS = [os.path.join(HOME, "develop", "herdr-agents"), os.path.join(HOME, ".agents", "skills"),
             os.path.join(HOME, ".agents"), os.path.join(HOME, ".codex"), os.path.join(HOME, ".claude"),
             os.path.join(HOME, ".pi"), os.path.join(HOME, "develop")]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build", ".idea"}

# ---------- 负控（已知真断链 / 已知真在盘） ----------
NEG_FILES = ["planning_gate.py", "deploy_git_hooks.py", "bridge_autonomous.py"]
POS_FILE = "tools/log_compress.py"
FAKE = "definitely_missing_zzz.py"

# 负控夹具：把 3 个已知断链按记忆里的真实写法（含 temp/ 前缀与反引号）注入扫描，
# 证明「抽取→解析→判 FAIL」这条链路本身有效（不依赖自然语料是否恰好提到某条）。
SYNTH_TEXT = """# negctl fixture (虚拟语料，仅存于内存)
- 定时/自主: bridge_autonomous 死任务已归档disabled; 实现见 `temp/bridge_autonomous.py`
- 部署门: planning_gate.py 与 deploy_git_hooks.py 曾由 sitecustomize 注入
- 正控: tools/log_compress.py 存在
- 假阳控: temp/definitely_missing_zzz.py 不存在
"""


def build_basename_index(roots):
    """basename -> [paths]，用于 BARE 类断言；返回 (index, 统计)"""
    idx = collections.defaultdict(list)
    seen_dirs, nfiles, ndirs = set(), 0, 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for r, ds, fs in os.walk(root):
            if r in seen_dirs:
                ds[:] = []
                continue
            seen_dirs.add(r)
            ndirs += 1
            ds[:] = [d for d in ds if d not in SKIP_DIRS]
            depth = r.count(os.sep) - base_depth
            if depth >= 7:
                ds[:] = []
            for f in fs:
                nfiles += 1
                if nfiles > 300000:
                    return idx, (nfiles, ndirs, "CAP@300k")
                if os.path.splitext(f)[1].lstrip(".") in EXTS or "." in f:
                    idx[f].append(os.path.join(r, f))
    return idx, (nfiles, ndirs, "ok")


def corpus_files(include_reports=False, synthetic=None):
    out = []
    for sub in ("", "autonomous_operation_sop", "review_sop"):
        d = os.path.join("memory", sub) if sub else "memory"
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if not os.path.isfile(p):
                continue
            if f.endswith(".md") or (f.endswith(".txt")):
                if sub == "autonomous_operation_sop" and f == "TODO.txt":
                    continue  # TODO 属任务台账，不是记忆事实
                out.append(p)
    if include_reports:  # 扩展语料：报告正文（默认排除，属 TODO#10 领域）
        for f in sorted(os.listdir("memory/autonomous_reports")):
            if f.endswith(".md"):
                out.append(os.path.join("memory/autonomous_reports", f))
    if synthetic:
        out.append(synthetic)   # 虚拟文件，仅存在于内存；由 scan_texts 注入
    return out


def context_class(line):
    if re.search(r'举例|示例|例如|样本|形如|demo|sample|比如|如[:：]|文档用语|占位', line):
        return "F4"
    if re.search(r'归档|已删|下线|disabled|废弃|不再|shim|已清|曾存在|历史快照|灭失|已失|已废|临时', line):
        return "F1"
    if re.search(r'待批|待建|计划|提案|建议|将(新增|由|建|改|落)|下一步|未来|拟', line):
        return "F2"
    return "F5"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(REPO, "temp/mem_assert_audit.json"),
                    help="默认锚定 GA 根（与 cwd 无关）")
    ap.add_argument("--md", default=os.path.join(REPO, "temp/mem_assert_audit.md"))
    ap.add_argument("--negctl", action="store_true", help="负面控制：3 断链必须 FAIL（含合成夹具注入）")
    ap.add_argument("--include-reports", action="store_true", help="语料扩大到 memory/autonomous_reports/*.md")
    a = ap.parse_args()

    syn_path = "«negctl-fixture»" if a.negctl else None
    files = corpus_files(a.include_reports, syn_path)
    idx_roots = [REPO, os.path.join(HOME, "develop"), os.path.join(HOME, "projects"), os.path.join(HOME, ".agents")]
    index, (nfiles, ndirs, cap) = build_basename_index(idx_roots)

    skip = collections.Counter()
    ext_root_hits = collections.Counter()
    drift_hits = collections.Counter()
    entries = {}          # (kind,target,fail_class) -> rec
    lines_asserts = []
    n_tok_total = 0
    nbytes = 0

    for f in files:
        if f == syn_path:
            src = SYNTH_TEXT
        else:
            src = open(f, encoding="utf-8", errors="replace").read()
        nbytes += len(src.encode())
        for ln, line in enumerate(src.split("\n"), 1):
            # ---- path:NN 行号断言 ----
            for m in LINE_RE.finditer(line):
                lines_asserts.append({"file": f, "line": ln, "tok": m.group(1), "n": int(m.group(2)), "ctx": line.strip()[:160]})
            for m in TOK_RE.finditer(line):
                tok = m.group(1)
                if tok[0] in "@(#":
                    tok2 = tok.lstrip("@(#")
                    if tok2:
                        skip["mention-prefix(@/#)"] += 1
                        tok = tok2
                n_tok_total += 1
                # 前缀 repo 限定词识别（... herdr-agents:docs/x.md）
                pre = line[:m.start(1)]
                pm = re.search(r'([A-Za-z][\w.-]*):\s*$', pre)
                prefix = pm.group(1) if pm else None
                if prefix and prefix not in EXT_ROOTS and prefix.lower() in ("http", "https", "file", "mailto"):
                    skip["url"] += 1
                    continue
                if "+" in tok or tok in PRODUCT_NAMES:
                    skip["compound(+)/产品名"] += 1
                    continue
                if PLACEHOLDER.search(tok):
                    skip["placeholder"] += 1
                    continue
                if PROSE_SLASH.match(tok):
                    skip["prose-slash(散文中的 文件名/文件名)"] += 1
                    continue
                if tok.startswith("/") and not re.match(r'^/(Users|Volumes|tmp|var|opt|etc|private)/', tok):
                    skip["url-ish(/ 开头非本机根)"] += 1
                    continue
                # CJK 前缀被切（实测「最终口径_FINAL.md」被抽成 `_FINAL.md`）：左向扩展回查索引，
                # 命中则用完整名继续核验；否则记为 suffix 碎片（不再当成断链）
                if tok[0] in "._" and "/" not in tok:
                    lm = re.search(r'([\u4e00-\u9fffA-Za-z0-9_]+)$', pre)
                    base = lm.group(1) if lm else ""
                    cc, fixed = base, None
                    while True:
                        if cc and index.get(cc + tok):
                            fixed = cc + tok
                            break
                        if not cc or not CJK.search(cc[0]):
                            break
                        cc = cc[1:]
                    if fixed:
                        skip["cjk-prefix-repaired(CJK扩展命中)"] += 1
                        tok = fixed
                    else:
                        skip["suffix-fragment(CJK前缀未命中)"] += 1
                        continue
                glued = bool(pre and CJK.search(pre[-1:]))
                kind, target, root = None, None, None
                t = tok.lstrip("./")
                if prefix and prefix in EXT_ROOTS:
                    kind, root, target = "EXT", EXT_ROOTS[prefix], tok
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
                    path = target
                    exists = os.path.exists(path)
                    # 多根解析：GA 根没有 ⇒ 试外部已知根（herdr-agents / skills / 家目录）
                    if not exists and kind in ("REPO", "UP"):
                        rel = tok.split("/", 1)[1] if tok.startswith(("../", "./")) else tok.lstrip("./")
                        for alt in ALT_ROOTS:
                            cand = os.path.join(alt, rel)
                            if os.path.exists(cand):
                                path, exists, drift = cand, True, True
                                ext_root_hits[kind] += 1
                                break
                # basename 兜底：①后缀吻合=根未标注/搬迁（强证据）②全盘唯一同名（中等）③同名多份=AMBIG 单列
                ambig = False
                if not exists and kind != "BARE" and kind != "EXT":
                    rel = tok.split("/", 1)[1] if tok.startswith(("../", "./")) else tok.lstrip("./")
                    base = rel.rstrip("/").split("/")[-1]
                    cands = sorted(set(index.get(base, [])))
                    pref = [c for c in cands if c.endswith("/" + rel)]
                    if pref:
                        path, exists, drift = pref[0], True, True
                        drift_hits[kind] += 1
                    elif len(cands) == 1:
                        path, exists, drift = cands[0], True, True
                        drift_hits[kind + "-uniq"] += 1
                    elif cands:
                        path, exists, ambig = cands[0] + f"  (+{len(cands)-1} 同名)", False, True
                fcls = "" if exists else context_class(line)
                # key 必须含 token 本身：否则不同断链会塌成一条（实测 BARE 287 条合一的 bug）
                key = (kind, target if (kind == "BARE" or not exists) else path, fcls)
                rec = entries.setdefault(key, {"kind": kind, "target": target, "path": path, "exists": exists,
                                               "fail_class": fcls, "drift": drift, "ambig": ambig,
                                               "count": 0, "hits": []})
                rec["count"] += 1
                if len(rec["hits"]) < 4:
                    rec["hits"].append({"file": f, "line": ln, "glued_cjk": glued, "ctx": line.strip()[:150]})

    # 双向自检注入
    inj = {"fake": not os.path.exists(os.path.join(REPO, FAKE)), "pos": os.path.exists(os.path.join(REPO, POS_FILE))}

    amb = [r for r in entries.values() if r.get("ambig")]
    ok = [r for r in entries.values() if r["exists"] and not r.get("ambig")]
    bad = [r for r in entries.values() if not r["exists"] and not r.get("ambig")]
    bad.sort(key=lambda r: (-r["count"], r["kind"]))
    fails_by_name = {os.path.basename(r["target"]): r for r in bad}
    oks_by_name = {os.path.basename(r["target"]): r for r in ok}
    neg_miss = [n for n in NEG_FILES if n not in fails_by_name]
    pos_miss = [POS_FILE] if os.path.basename(POS_FILE) not in oks_by_name else []
    fake_hit = FAKE in fails_by_name
    base_absent = {n: (n not in index) for n in NEG_FILES + [FAKE]}
    neg_ok = ((not neg_miss) and (not pos_miss) and inj["pos"] and inj["fake"] and fake_hit
              and all(base_absent.values()) and a.negctl)

    # LINEOFF（多根解析：先 REPO，再 ALT_ROOTS；解析不到=NOFILE，不算行号造假）
    lo = []
    for la in lines_asserts:
        p = la["tok"]
        if p.startswith("~"):
            p = os.path.expanduser(p)
        elif p.startswith("/"):
            pass
        else:
            rel = p.split("/", 1)[1] if p.startswith(("../", "./")) else p.lstrip("./")
            p = os.path.join(REPO, rel)
            if not os.path.exists(p):
                for alt in ALT_ROOTS:
                    cand = os.path.join(alt, rel)
                    if os.path.exists(cand):
                        p = cand
                        break
        if not os.path.exists(p):
            # basename 兜底：仅后缀吻合 或 全盘唯一同名 才接受（避免贪心误配）
            base = p.rstrip("/").split("/")[-1]
            cands = sorted(set(index.get(base, [])))
            pref = [c for c in cands if c.endswith("/" + la["tok"].lstrip("./"))]
            if pref:
                p = pref[0]; la["resolved_root"] = "basename-fallback(suffix)"
            elif len(cands) == 1:
                p = cands[0]; la["resolved_root"] = "basename-fallback(uniq)"
            elif cands:
                la["status"] = "AMBIG"
                la["file_lines"] = None
                lo.append(la)
                continue
        if not os.path.exists(p):
            la["status"] = "NOFILE"
        else:
            n = sum(1 for _ in open(p, "rb"))
            la["status"] = "OK" if la["n"] <= n else "LINEOFF"
            la["file_lines"] = n
            if not p.startswith(REPO):
                la["resolved_root"] = p
        lo.append(la)
    lo_bad = [x for x in lo if x["status"] != "OK"]
    lo_off = [x for x in lo if x["status"] == "LINEOFF"]
    lo_nof = [x for x in lo if x["status"] == "NOFILE"]

    summary = {
        "corpus_files": len(files), "corpus_bytes": nbytes,
        "tokens_total": n_tok_total, "assertions": len(entries),
        "assertions_occurrences": sum(r["count"] for r in entries.values()) + sum(skip.values()),
        "skipped": dict(skip), "pass": len(ok), "fail": len(bad), "ambiguous": len(amb),
        "fail_by_class": dict(collections.Counter(r["fail_class"] for r in bad)),
        "fail_by_kind": dict(collections.Counter(r["kind"] for r in bad)),
        "line_asserts": len(lo), "line_asserts_bad": len(lo_bad),
        "line_asserts_lineoff": len(lo_off), "line_asserts_nofile": len(lo_nof),
        "negctl_natural_hits": {n: (n in fails_by_name) for n in NEG_FILES},
        "negctl_fixture_proves_chain": a.negctl,
        "basename_index_files": nfiles, "basename_index_dirs": ndirs, "basename_index_note": cap,
        "ext_root_hits": dict(ext_root_hits), "drift_hits": dict(drift_hits),
        "advisory_drift": [{"target": r["target"], "resolved": r["path"], "count": r["count"]}
                           for r in ok if r["drift"]],
        "negctl_basename_absent": {n: (n not in index) for n in NEG_FILES + [FAKE]},
        "negctl_ok": neg_ok, "negctl_missing": neg_miss, "injected": inj,
    }
    summary["advisory_drift_n"] = len(summary["advisory_drift"])
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("\n[FAIL 清单·全量] （kind x次数 class token <- 证据行）")
    for r in bad:
        h = r["hits"][0] if r["hits"] else {}
        print(f"  {r['kind']:4s} x{r['count']:<3d} {r['fail_class']} {r['target'][:70]:70s} <- {os.path.basename(h.get('file',''))}:{h.get('line','')}")
    print(f"\n[AMBIG 同名多份] {len(amb)} 条（同名文件多处存在、路径又不吻合 ⇒ 无法判定，需人工看上下文）")
    for r in sorted(amb, key=lambda r: -r["count"])[:15]:
        h = r["hits"][0] if r["hits"] else {}
        print(f"  x{r['count']:<2d} {r['target'][:60]:60s} -> {r['path'][:80]} <- {os.path.basename(h.get('file',''))}:{h.get('line','')}")
    print(f"\n[ADVISORY 位置漂移] GA 根没有、外部根找到：{summary['advisory_drift_n']} 条")
    for d in summary["advisory_drift"][:20]:
        print(f"  x{d['count']:<2d} {d['target'][:50]:50s} -> {d['resolved']}")
    print(f"\n[LINEOFF/NOFILE] {len(lo_bad)}/{len(lo)}")
    for x in lo_bad[:15]:
        print(f"  {x['status']:8s} {x['tok']}:{x['n']} @ {x['file']}:{x['line']}")

    json.dump({"summary": summary, "pass": ok, "fail": bad, "line_asserts": lo},
              open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[json] {a.json}")
    if a.negctl and not neg_ok:
        print("❌ 负控失败:", neg_miss, inj)
        return 2
    if a.negctl:
        print("✅ 负控通过：3 断链全 FAIL、注入假路径 FAIL、注入真文件 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
