#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dedup_guard.py — 规划候选「近重复」闸门（只读，纯 stdlib）。

背景（第13批 C4）：历史报告三次记「已交付」的规划闸门工具
（planning_gate.py / dedup_guard.py / todo_quality_gate.py）在本机全盘
零命中 = 真 ghost（见 `--prove-ghost` 的双证据），而历史低价值模式量化里
P4_近重复 13 条仍在增长。本器复活其中「去重」那一半，只做一件事：
把**待规划候选**与「批内其它候选」及「历史条目（TODO 行 + 历史报告标题/首段）」
做机械比对，报出疑似近重复并给可审计证据。

判据（两个信号同时看，证据一律打印，禁黑箱）：
  - jaccard  = |A∩B| / |A∪B|        （A/B = 词元集合）
  - contain  = |A∩B| / min(|A|,|B|) （一方几乎是另一方的子集时很高）
  - strong   = 双方命中同一个「硬 token」（tools/ 或 memory/ 下的文件路径、
               R\\d{2,3} 报告编号、或专有工具名）——不看比例直接判 STRONG_DUP
  dup 判定：strong 或 (jaccard ≥ J) 或 (contain ≥ C)，且两侧词元数都 ≥ MIN_TOKENS
  （太短的条目一律 SKIPPED_SHORT，避免「短文本两两相似」刷屏）

只读：不写任何仓库文件（--json 需显式给路径，且只写该路径）。
用法：
  python3 tools/dedup_guard.py                       # 默认：干跑本仓规划语料（不传候选=只体检语料）
  python3 tools/dedup_guard.py --candidates FILE     # 候选文件（JSON 或规划 prompt 文本）
  python3 tools/dedup_guard.py --candidates temp/plan13/prompt.txt --diag
  python3 tools/dedup_guard.py --prove-ghost         # 打印三个 ghost 工具的双证据
  python3 tools/dedup_guard.py --selftest            # 含负控
"""
import argparse, hashlib, json, os, re, sys, unicodedata

# ---------------------------------------------------------------- 词元化
# 通用虚词/结构词（不携带主题信息）。刻意保守：只放明显不区分主题的词。
STOP = set("""的 了 是 在 与 和 及 等 一条 一个 我们 你 我 他 它 这 那 有 无 不 要 会
可以 需要 必须 应当 应该 而且 但是 因为 所以 如果 那么 把 被 从 到 对 为 以 于 之
并 或 也 都 还 就 只 再 更 最 里 中 上 下 前 后 内 外 时 时候 现在 本轮 本批 本器
任务 候选 报告 结论 背景 验收 目标 内容 方式 方法 部分 情况 问题 数据 工具 文件
结果 输出 输入 使用 采用 通过 进行 给出 提供 实现 完成 落地 相关 相应 后续 目前
已经 仍然 依然 可能 是否 以及 或者 not and the for with that this from are was
""".split())
PATH_RE = re.compile(r"(?:tools|memory|temp|scripts|reflect_scripts|sche_tasks|logs|docs)/[\w./\-\u4e00-\u9fff]+")
RID_RE = re.compile(r"\bR\d{2,3}\b")
LATIN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.\-]{2,}")


def norm_text(s):
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("`", " ").replace("*", " ").replace("｜", "|")
    return s


def tokens(text):
    """词元集合：路径整块 + R编号 + 拉丁标识符 + CJK bigram。"""
    t = norm_text(text)
    out = set()
    out |= set(PATH_RE.findall(t))
    out |= set(RID_RE.findall(t))
    for w in LATIN_RE.findall(t):
        w = w.lower()
        if len(w) >= 3 and w not in STOP:
            out.add(w)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", t)
    for run in cjk_runs:
        if len(run) <= 4 and run not in STOP:
            out.add(run)
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in STOP:
                out.add(bg)
    return {x for x in out if x not in STOP}


def hard_tokens(text):
    """硬 token：路径 / R编号 / 反引号内标识符（.py, .md, _guard 等）。"""
    t = norm_text(text)
    hs = set(PATH_RE.findall(t)) | set(RID_RE.findall(t))
    for w in re.findall(r"[`\"']?([a-z_][a-z0-9_]{3,}(?:\.py|\.md|\.json|\.sh|\.plist))[`\"']?", t):
        hs.add(w)
    return {h for h in hs if not h.endswith("/")}


def pair_metrics(a, b):
    ta, tb = tokens(a), tokens(b)
    inter = ta & tb
    union = ta | tb
    jac = len(inter) / len(union) if union else 0.0
    contain = len(inter) / min(len(ta), len(tb)) if min(len(ta), len(tb)) else 0.0
    shared_hard = hard_tokens(a) & hard_tokens(b)
    return ta, tb, inter, jac, contain, shared_hard


def evidence(inter, n=8):
    """挑证据词：长词元/路径优先，短 bigram 次之。"""
    items = sorted(inter, key=lambda x: (-len(x), x))
    return items[:n]


# ---------------------------------------------------------------- 输入解析
def parse_candidates(path):
    """支持两种输入：JSON list（[{id|name, text|desc}]）或规划 prompt 文本
    （`Cn 【类型】标题：正文` 分块，到下一个 Cn / `## ` 为止）。"""
    raw = open(path, encoding="utf-8", errors="replace").read()
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            obj = obj.get("candidates") or obj.get("items") or []
        out = []
        for i, it in enumerate(obj, 1):
            cid = str(it.get("id") or it.get("name") or f"C{i}")
            body = it.get("text") or it.get("desc") or it.get("body") or ""
            if it.get("title"):
                body = str(it["title"]) + "：" + str(body)
            out.append((cid, body))
        return out, "json"
    # 文本：按行扫，遇 ^C\d+ 起新块
    out, cur_id, buf = [], None, []
    pat = re.compile(r"^\s*(C\d+)\s*[【\[]")
    for line in raw.split("\n"):
        m = pat.match(line)
        if m:
            if cur_id:
                out.append((cur_id, "\n".join(buf).strip()))
            cur_id, buf = m.group(1), [line]
        elif cur_id is not None:
            if line.startswith("## ") or line.startswith("# "):
                out.append((cur_id, "\n".join(buf).strip()))
                cur_id, buf = None, []
            else:
                buf.append(line)
    if cur_id:
        out.append((cur_id, "\n".join(buf).strip()))
    return out, "prompt"


def corpus_auto(root, since_batch=None):
    """历史语料：temp/TODO.txt 的 [ ]/[x] 行（按 | 切成条目）+ 历史报告
    文件名与首 4 行。返回 [(id, text)]，id 里带来源路径便于审计。"""
    items = []
    todo = os.path.join(root, "temp", "TODO.txt")
    if os.path.exists(todo):
        for i, line in enumerate(open(todo, encoding="utf-8", errors="replace"), 1):
            s = line.strip()
            if s.startswith("[ ]") or s.startswith("[x]"):
                items.append((f"temp/TODO.txt:{i}", s))
    for d in ("memory/autonomous_reports", "temp/autonomous_reports"):
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if not fn.endswith(".md"):
                continue
            fp = os.path.join(p, fn)
            try:
                head = "".join(open(fp, encoding="utf-8", errors="replace").readlines()[:4])
            except Exception:
                continue
            items.append((f"{d}/{fn}", fn + "\n" + head))
    return items


def corpus_from_file(path):
    items = []
    for i, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
        s = line.strip()
        if s:
            items.append((f"{path}:{i}", s))
    return items


# ---------------------------------------------------------------- 主判定
def check(cands, corpus, j_th=0.20, c_th=0.45, min_tokens=40, diag=False,
          intra=True, corpus_label="corpus"):
    """返回 (results, stats)。results: [{id, verdict, hits:[{ref,why,jac,contain,shared_hard,evidence}]}]"""
    results, stats = [], {"candidates": len(cands), "corpus": len(corpus), "pairs": 0,
                          "skipped_short": 0, "dup_history": 0, "dup_intra": 0, "unique": 0}
    ctx = {cid: tokens(txt) for cid, txt in cands}
    ctx_hard = {cid: hard_tokens(txt) for cid, txt in cands}
    # 批内两两比对（预计算；双方都要被标记，避免「只有 id 大的一方报警」的不对称）
    intra_hits = {}
    if intra:
        for i in range(len(cands)):
            for k in range(i + 1, len(cands)):
                a_id, a_txt = cands[i]
                b_id, b_txt = cands[k]
                stats["pairs"] += 1
                ta, tb, inter, jac, contain, sh = pair_metrics(a_txt, b_txt)
                if len(ta) < min_tokens or len(tb) < min_tokens:
                    if diag:
                        print(f"    [diag-intra] {a_id}~{b_id} 短条目跳过 (tk={len(ta)}/{len(tb)})")
                    continue
                why = []
                if sh:
                    why.append("shared_hard=" + ",".join(sorted(sh)))
                if jac >= j_th:
                    why.append(f"jaccard={jac:.3f}")
                if contain >= c_th:
                    why.append(f"contain={contain:.3f}")
                if why:
                    h = {"why": ";".join(why), "jac": round(jac, 3), "contain": round(contain, 3),
                         "shared_hard": sorted(sh), "evidence": evidence(inter)}
                    intra_hits.setdefault(a_id, []).append(dict(h, ref=f"批内:{b_id}"))
                    intra_hits.setdefault(b_id, []).append(dict(h, ref=f"批内:{a_id}"))
                elif diag:
                    print(f"    [diag-intra] {a_id}~{b_id} jac={jac:.3f} contain={contain:.3f} hard={sorted(sh)}")
    for cid, txt in cands:
        hits = []
        if len(ctx[cid]) < min_tokens:
            stats["skipped_short"] += 1
            results.append({"id": cid, "verdict": "SKIPPED_SHORT", "hits": [],
                            "tokens": len(ctx[cid])})
            continue
        for h in intra_hits.get(cid, []):
            hits.append(dict(h))
        # 与历史语料比：硬 token 命中 或 比例超阈
        for ref, rtxt in corpus:
            stats["pairs"] += 1
            ta, tb, inter, jac, contain, sh = pair_metrics(txt, rtxt)
            if not sh and (len(inter) < 8 or len(tb) < min_tokens):
                if diag and jac > 0.10:
                    print(f"    [diag-hist] {cid}~{ref} jac={jac:.3f} contain={contain:.3f} tk={len(tb)}")
                continue
            why = []
            if sh:
                why.append("shared_hard=" + ",".join(sorted(sh)))
            if jac >= j_th:
                why.append(f"jaccard={jac:.3f}")
            if contain >= c_th:
                why.append(f"contain={contain:.3f}")
            if why:
                hits.append({"ref": ref, "why": ";".join(why), "jac": round(jac, 3),
                             "contain": round(contain, 3), "shared_hard": sorted(sh),
                             "evidence": evidence(inter)})
            elif diag:
                print(f"    [diag-hist] {cid}~{ref} jac={jac:.3f} contain={contain:.3f}")
        verdict = "UNIQUE"
        if hits:
            verdict = "DUP"
            if any(h["ref"].startswith("批内:") for h in hits):
                stats["dup_intra"] += 1
            if any(not h["ref"].startswith("批内:") for h in hits):
                stats["dup_history"] += 1
        else:
            stats["unique"] += 1
        # 证据最多 5 条，按 jac 降序
        hits.sort(key=lambda h: (-h["jac"], -h["contain"]))
        results.append({"id": cid, "verdict": verdict, "hits": hits[:5],
                        "tokens": len(ctx[cid])})
    return results, stats


# ---------------------------------------------------------------- 只读自证
def tree_sha(root, subdirs=("tools",)):
    h = hashlib.sha256()
    for sub in subdirs:
        base = os.path.join(root, sub)
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for fn in sorted(fns):
                fp = os.path.join(dp, fn)
                h.update(os.path.relpath(fp, root).encode())
                h.update(open(fp, "rb").read())
    return h.hexdigest()


GHOSTS = ["planning_gate.py", "dedup_guard.py", "todo_quality_gate.py"]


def prove_ghost(root):
    """双证据：①磁盘 find 式遍历零命中 ②git log --all --diff-filter=A 零新增。"""
    import subprocess
    print("== 双证据：三个规划闸门工具是否真 ghost ==")
    disk = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            if fn in GHOSTS:
                disk.setdefault(fn, []).append(os.path.join(dp, fn))
    for g in GHOSTS:
        print(f"  [磁盘] {g}: 命中 {len(disk.get(g, []))} 处 {disk.get(g, [])}")
    for g in GHOSTS:
        r = subprocess.run(["git", "log", "--all", "--diff-filter=A", "--name-only",
                            "--pretty=format:%h", "--", f"*{g}", g],
                           cwd=root, capture_output=True, text=True)
        added = [l for l in r.stdout.split("\n") if l and not l.startswith("  ")]
        print(f"  [git]  {g}: 历史新增该路径的提交 = {len(added)} 条 {added[:3]}")


# ---------------------------------------------------------------- selftest
def selftest(root):
    ok = fail = 0
    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1; print(f"  PASS {name} {detail}")
        else:
            fail += 1; print(f"  FAIL {name} {detail}")

    hist = [("hist:A", "【第12批】引用完整性闸门 tools/ref_integrity.py：抽报告中 path 引用判 EXISTS/GONE/AMBIG，"
                      "只读，接进 helper.complete_task 收尾，交付报告 R410_引用完整性闸门ref_integrity.md，"
                      "验收：①真跑给出计数 ②负控 3 条 ③只读自证 sha1 全同"),
            ("hist:B", "【第11批】日志回收 dry-run 计划器 temp/log_reclaim_plan.py 给 logs/ 可回收清单，"
                       "含孤儿 launch_gui.log，须显式 --apply --approve 才动，待批")]
    dup = [("C1", "做一个引用完整性对账器 tools/ref_integrity_v2.py，抽报告里的 path 引用并判 EXISTS/GONE，"
                  "只读，验收：①计数自证 ②负控必报断链 ③只读自证 ④证据打印语料条数"),
           ("C2", "分析本机 Chrome 书签里的技术偏好并给个性化推荐清单，读正文提炼，验收：①清单≥20 条")]
    res, st = check(dup, hist, min_tokens=20)
    w = {r["id"]: r for r in res}
    chk("NC1 同主题候选必判重复", w["C1"]["verdict"] == "DUP",
        f"{w['C1']['verdict']} hits={[h['ref'] for h in w['C1']['hits']]}")
    chk("NC2 无关主题不得误报", w["C2"]["verdict"] == "UNIQUE", w["C2"]["verdict"])
    res2, _ = check([("S", "很短的一条")], hist, min_tokens=40)
    chk("NC3 短条目 SKIPPED_SHORT", res2[0]["verdict"] == "SKIPPED_SHORT", res2[0]["verdict"])
    res3, st3 = check([("X", dup[0][1])], [], min_tokens=20)
    chk("NC4 空语料须自证 corpus=0（不得静默全 OK 当结论）",
        st3["corpus"] == 0 and any("corpus" in str(k) for k in st3), f"stats={st3['corpus']}")
    pair = [("P1", "给 knowledgeC 做每日快照器 tools/kc_snap.py 并真采一份，验收：①路径/字节/sha1 ②窗口 SQL ③只读自证"),
            ("P2", "为 knowledgeC 写每日快照脚本 tools/kc_snap2.py 并真跑采集，验收：①打印路径字节 sha1 ②窗口长度命令 ③sha1 前后一致")]
    res4, st4 = check(pair, [], min_tokens=15)
    chk("NC5 批内近重复必报（互换措辞）",
        all(r["verdict"] == "DUP" for r in res4), [r["verdict"] for r in res4])
    # NC6：语料自证条数>0（防「校验器没读到语料」）
    chk("NC6 语料计数自证", st["corpus"] == 2 and st["candidates"] == 2, f"{st}")
    print(f"selftest {ok}/{ok+fail}")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--candidates")
    ap.add_argument("--corpus", default="auto")
    ap.add_argument("--threshold-jaccard", type=float, default=0.20)
    ap.add_argument("--threshold-containment", type=float, default=0.45)
    ap.add_argument("--min-tokens", type=int, default=40)
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--no-intra", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--prove-ghost", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest(a.root))
    if a.prove_ghost:
        prove_ghost(a.root); sys.exit(0)

    before = tree_sha(a.root)
    if a.corpus == "auto":
        corpus = corpus_auto(a.root)
    else:
        corpus = corpus_from_file(a.corpus)
    if a.candidates:
        cands, fmt = parse_candidates(a.candidates)
    else:
        cands, fmt = [], "-"
    print(f"# dedup_guard  candidates={len(cands)} ({fmt})  corpus={len(corpus)} 条"
          f"  thresholds: jac>={a.threshold_jaccard} contain>={a.threshold_containment}"
          f" min_tokens={a.min_tokens}")
    if not corpus:
        print("!! 语料 0 条 —— 结论不可用（先证读到了语料）")
    results, stats = check(cands, corpus, a.threshold_jaccard,
                           a.threshold_containment, a.min_tokens, a.diag,
                           intra=not a.no_intra)
    for r in results:
        print(f"\n[{r['id']}] {r['verdict']}  (tokens={r['tokens']})")
        for h in r["hits"]:
            print(f"    ← {h['ref']}  {h['why']}")
            print(f"      evidence: {' / '.join(h['evidence'])}")
    print(f"\n# 统计 {json.dumps(stats, ensure_ascii=False)}")
    after = tree_sha(a.root)
    print(f"# 只读自证 tools/ sha256 {'UNCHANGED' if before == after else 'CHANGED!!'}")
    if a.json:
        json.dump({"stats": stats, "results": results}, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"# json -> {a.json}")
    sys.exit(1 if stats["dup_history"] or stats["dup_intra"] else 0)


if __name__ == "__main__":
    main()
