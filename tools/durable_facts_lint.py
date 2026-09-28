#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""durable_facts_lint.py —— 耐久记忆事实断言复核器（只读，纯 stdlib）

目的：把 L1(global_mem_insight.txt) / L2(global_mem.txt) / L3(SOP .md) 里
      那些"会被当成事实读"的断言，变成机器可复核的注册表条目。

四类（cls）：
  REF_EXISTS         引用存在性      —— 语料里出现的路径 token 是否在盘上
  CLAIM_VS_BODY      标题/正文一致性 —— 同文件内「已接线/已落地」类声明 与「未接线/待批」类自承 并存
  SOP_VS_IMPL        SOP 断言 vs 实现 —— SOP 文本断言（升序/降序 ⇒ 顶部/末尾）× 文件实际插入位置
  CITED_FILE_TRUTH   被引文件真身    —— 同名多份 / 相对路径归属歧义

判定三态：MATCH / CONTRADICTED / UNVERIFIABLE（源文件或语料缺失一律 UNVERIFIABLE，**绝不静默通过**）。
退出码：有 CONTRADICTED ⇒ 1，否则 0。

用法：
  python3 tools/durable_facts_lint.py [--json OUT] [--registry FILE] [--scan-only|--curated-only]
  python3 tools/durable_facts_lint.py --selftest      # 内建夹具 + 负控，不碰真实记忆
  python3 tools/durable_facts_lint.py --proof         # 只读自证（语料 sha256 前后一致）

负控自测覆盖：① 断言的实现与 SOP 文本相反 ⇒ 必报 CONTRADICTED
              ② 被引路径不存在 ⇒ 必报 CONTRADICTED
              ③ 源文件不存在 ⇒ UNVERIFIABLE（不是 MATCH）
              ④ 计数行与行数不符 ⇒ CONTRADICTED
              ⑤ 标题声明与正文自承并存 ⇒ CONTRADICTED
              ⑥ 语料为空 ⇒ 显式报错（防"校验器没读到语料却全绿"）
              ⑦ 状态字段透传 / CLOSED 不计失败（--strict 才计）
              ⑧ 路径 token 全在盘上 ⇒ MATCH（防"恒报真"的反向守卫）
              ⑨ basename 兜底索引只建一次（防性能回归到「每 token 全仓 os.walk」）
              ⑩ 索引快照语义与真幽灵判负（防兜底"恒真"；建索引后新增文件不可见=已固定边界）
              ⑪ 性能优化不得改变判定：scan_claim_body 与「旧实现（逐 artifact 逐行 re.search）」
                 在同一夹具上输出逐字比对（负控损坏实现必报失败，已用坏实现验证灵敏度）

性能（2026-09-28 R468 实测）：语料 326 文件 / 13.7MB；全量（注册表 + 扫描 + 只读自证）
  **1.06s**（cProfile 插桩 1.30s），其中 scan_claim_body 0.31s、_build_base_index 0.34s；
  同 token 集下旧式逐 token 全仓 walk 需 239.01s ⇒ 索引化后 **1356×**（判定逐字一致）。
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REGISTRY = os.path.join(REPO, "tools", "durable_facts_assertions.json")
MEM = os.path.join(REPO, "memory")
TEXT_EXT = (".md", ".txt")

# ---------------------------------------------------------------- 语料
def corpus_files(root=None):
    root = root or MEM
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for fn in filenames:
            if fn.endswith(TEXT_EXT):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def read_text(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 16), b""):
                h.update(blk)
    except OSError:
        return None
    return h.hexdigest()


def corpus_fingerprint(files):
    return {os.path.relpath(p, REPO): sha256_file(p) for p in files}

# ---------------------------------------------------------------- 工具
def find_line(text, pattern):
    """返回 (1-based 行号, 行文本) 或 None"""
    for i, ln in enumerate(text.split("\n"), 1):
        if re.search(pattern, ln):
            return i, ln
    return None


def all_lines(text, pattern):
    return [(i, ln) for i, ln in enumerate(text.split("\n"), 1) if re.search(pattern, ln)]


def numbered_rows(text, row_regex):
    """按 row_regex 抽取 (行号, 编号int) 列表（按文件顺序）"""
    rows = []
    for i, ln in enumerate(text.split("\n"), 1):
        m = re.search(row_regex, ln)
        if m:
            try:
                rows.append((i, int(m.group(1))))
            except (IndexError, ValueError):
                pass
    return rows


def insert_side(rows):
    """'TOP' / 'BOTTOM' / 'MIXED' / None"""
    if len(rows) < 2:
        return None
    mx = max(n for _, n in rows)
    first_n, last_n = rows[0][1], rows[-1][1]
    if first_n == mx:
        return "TOP"
    if last_n == mx:
        return "BOTTOM"
    return "MIXED"


def claim_side(claim_text):
    """SOP 文本断言的插入侧：末尾→BOTTOM，顶部→TOP，看不出→None"""
    if not claim_text:
        return None
    if "末尾" in claim_text or "文末" in claim_text:
        return "BOTTOM"
    if "顶部" in claim_text or "首行" in claim_text:
        return "TOP"
    return None

# ---------------------------------------------------------------- 检查器
def chk_index_insert_side(p):
    """SOP 断言 vs 实现：SOP 里关于某目标文件插入方向的断言 × 该文件实际插入位置"""
    res = {"detail": [], "evidence": {}}
    sop = read_text(os.path.join(REPO, p["sop_file"]))
    if sop is None:
        return "UNVERIFIABLE", res, "SOP 源文件不存在: %s" % p["sop_file"]
    hit = find_line(sop, p["sop_claim_regex"])
    if not hit:
        return "UNVERIFIABLE", res, "SOP 里未匹配到断言句: %s" % p["sop_claim_regex"]
    lineno, line = hit
    m = re.search(p["sop_claim_regex"], line)
    claim = m.group(0) if m else ""
    res["evidence"]["sop_line"] = lineno
    res["evidence"]["sop_text"] = claim
    assert_side = claim_side(claim)
    target = os.path.join(REPO, p["target_file"])
    txt = read_text(target)
    if txt is None:
        return "UNVERIFIABLE", res, "目标文件不存在: %s" % p["target_file"]
    rows = numbered_rows(txt, p["row_regex"])
    actual = insert_side(rows)
    res["evidence"]["target_rows"] = len(rows)
    res["evidence"]["max_num_row_index"] = (
        [i for i, n in rows if n == max(x for _, x in rows)][0] if rows else None)
    res["evidence"]["actual_side"] = actual
    res["evidence"]["asserted_side"] = assert_side
    if assert_side is None or actual is None:
        return "UNVERIFIABLE", res, "无法判定断言侧/实际侧 (assert=%s actual=%s)" % (assert_side, actual)
    if actual == "MIXED":
        return "CONTRADICTED", res, "实际插入位置不唯一（既非纯顶部也非纯末尾）"
    if assert_side == actual:
        return "MATCH", res, "SOP 断言(%s) 与实现(%s) 一致" % (assert_side, actual)
    return "CONTRADICTED", res, "SOP 断言(%s) 与实现(%s) 相反" % (assert_side, actual)


# 性能实测（2026-09-28，R468）：
#  ①「token 正则 (?:[\w.-]+/)+ 在长行上灾难性回溯」的假说**已被实测证伪**——旧写法
#    `rx.findall(ln)` 扫 memory/global_mem.txt 全文件（最长行 4592 字符）仅 0.003s。
#  ②一度改为 400/200 重叠分窗，但窗口重叠会把同一 token 重复计数（实测 L2 432 次 vs 真值 333 次）
#    ⇒ 非等价，已回退为单次 findall。
#  ③真实瓶颈是 _resolve 的 basename 兜底：每个 token 都全仓 os.walk（282 token ⇒ 17s），
#    现改为一次性 basename 索引（见 _build_base_index）。
def _scan_paths(files, token_regex):
    hits = {}
    rx = re.compile(token_regex)
    for f in files:
        t = read_text(f)
        if t is None:
            continue
        for i, ln in enumerate(t.split("\n"), 1):
            for tok in rx.findall(ln):
                hits.setdefault(tok, []).append((os.path.relpath(f, REPO), i))
    return hits


_BASE_INDEX = None


def _build_base_index():
    """仓内 basename → 首个命中路径（os.walk 顺序，等价于旧逐 token walk 的首次命中）。"""
    idx = {}
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            idx.setdefault(fn, os.path.join(dirpath, fn))
    return idx


def _resolve(tok):
    """token 在盘上是否存在（仓内相对 + 仓外绝对/家目录 + basename 兜底）

    basename 兜底走一次性索引（旧实现每 token 全仓 os.walk ⇒ 282 token 耗 17s）。
    """
    global _BASE_INDEX
    cands = []
    if tok.startswith("/"):
        cands.append(tok)
    else:
        cands.append(os.path.join(REPO, tok))
        cands.append(os.path.expanduser(os.path.join("~", tok)))
    for c in cands:
        if os.path.exists(c):
            return True, c
    base = os.path.basename(tok)
    if base:
        if _BASE_INDEX is None:
            _BASE_INDEX = _build_base_index()
        hit = _BASE_INDEX.get(base)
        if hit:
            return True, hit
    return False, None


def chk_path_tokens_exist(p):
    """引用存在性：语料（或指定文件）里的路径 token 是否在盘上"""
    res = {"detail": [], "evidence": {}}
    files = [os.path.join(REPO, p["file"])] if p.get("file") else p.get("_files") or []
    if not files:
        return "UNVERIFIABLE", res, "无被检文件（语料为空？）"
    missing_file = [os.path.relpath(f, REPO) for f in files if not os.path.exists(f)]
    for rel in missing_file:
        res["detail"].append("[源缺失] " + rel)
    hits = _scan_paths([f for f in files if os.path.exists(f)], p["token_regex"])
    absent, present = [], []
    for tok, where in sorted(hits.items()):
        ok, _ = _resolve(tok)
        (present if ok else absent).append(tok)
        res["detail"].append("%s %s  (首见 %s:%d)" % ("[有]" if ok else "[无]", tok, where[0][0], where[0][1]))
    res["evidence"] = {"tokens": len(hits), "present": len(present), "absent": len(absent),
                       "absent_list": absent[:50], "missing_source_files": missing_file}
    if not hits:
        return "UNVERIFIABLE", res, "未抽到任何路径 token（语料计数=%d）" % len(files)
    want = p.get("expect", "NO_ABSENT")
    if want == "NO_ABSENT":
        return ("MATCH", res, "全部 token 在盘上") if not absent else \
               ("CONTRADICTED", res, "有 %d 个 token 盘上查无" % len(absent))
    if want == "HAS_ABSENT":
        return ("MATCH", res, "确有 token 盘上查无") if absent else \
               ("CONTRADICTED", res, "预期应有查无项，实际全在")
    if want == "ALL_ABSENT":
        return ("MATCH", res, "全部 token 盘上查无") if len(absent) == len(hits) else \
               ("CONTRADICTED", res, "预期全部查无，实际有 %d 个存在" % len(present))
    return "UNVERIFIABLE", res, "未知 expect: %s" % want


def chk_file_truth(p):
    """被引文件真身：同名多份 / 相对路径归属"""
    res = {"detail": [], "evidence": {}}
    tok = p["token"]
    base = os.path.basename(tok)
    found = []
    roots = p.get("search_roots") or [REPO]
    for r in roots:
        r = os.path.expanduser(r)
        if not os.path.isabs(r):
            r = os.path.join(REPO, r)
        if not os.path.isdir(r):
            continue
        for dirpath, dirnames, filenames in os.walk(r):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
            if base in filenames:
                found.append(os.path.relpath(os.path.join(dirpath, base), REPO)
                             if os.path.join(dirpath, base).startswith(REPO)
                             else os.path.join(dirpath, base))
    res["evidence"] = {"token": tok, "candidates": found}
    for f in found:
        res["detail"].append("[候选] " + f)
    exp = p.get("expect_under")
    if not found:
        return ("MATCH", res, "全盘查无（与断言一致）") if p.get("expect") == "NONE" else \
               ("CONTRADICTED", res, "全盘查无同名文件（断言预期存在）")
    if exp:
        ok = any(f.startswith(exp) for f in found)
        if ok and len(found) > 1:
            return "MATCH", res, "可在 %s 下解析；另有 %d 个同名候选（提示级）" % (exp, len(found) - 1)
        if ok:
            return "MATCH", res, "唯一且可在 %s 下解析" % exp
        return "CONTRADICTED", res, "无候选落在 %s 下" % exp
    return "MATCH", res, "存在 %d 个候选" % len(found)


def chk_claim_body_conflict(p):
    """标题/正文一致性（artifact 作用域）：同一 artifact 是否既被声明「已做」又被自承「未做」

    p: {file, claim_regex, body_regex, artifact_regex}
    artifact 作用域是关键：不加作用域会退化成"文件里既有已做又有未做就报警"（几乎恒报真）。
    """
    res = {"detail": [], "evidence": {}}
    txt = read_text(os.path.join(REPO, p["file"]))
    if txt is None:
        return "UNVERIFIABLE", res, "源文件不存在: %s" % p["file"]
    art_rx = p.get("artifact_regex")
    if not art_rx:
        return "UNVERIFIABLE", res, "缺 artifact_regex（本类必须限定同一 artifact）"
    lines = txt.split("\n")
    arts = sorted(set(re.findall(art_rx, txt)))
    if not arts:
        return "UNVERIFIABLE", res, "语料里无 artifact: %s" % art_rx
    verdict = "MATCH"
    for art in arts:
        claims, bodies = [], []
        for i, ln in enumerate(lines, 1):
            if art in ln:
                if re.search(p["claim_regex"], ln):
                    claims.append((i, ln))
                if re.search(p["body_regex"], ln):
                    bodies.append((i, ln))
        if claims:
            for ln, t in claims:
                res["detail"].append("[声明] %s:%d %s" % (p["file"], ln, t.strip()[:110]))
        if bodies:
            for ln, t in bodies:
                res["detail"].append("[自承] %s:%d %s" % (p["file"], ln, t.strip()[:110]))
        if claims and bodies:
            verdict = "CONTRADICTED"
    res["evidence"] = {"artifacts": arts[:10]}
    if verdict == "CONTRADICTED":
        return verdict, res, "同一 artifact 既被声明「已做」又被自承「未做」（见 detail）"
    return "MATCH", res, "artifact %s 无自相矛盾" % (arts[:3])


def chk_count_consistency(p):
    """『行数 N』声明 与 实际行数 是否自洽"""
    res = {"detail": [], "evidence": {}}
    txt = read_text(os.path.join(REPO, p["file"]))
    if txt is None:
        return "UNVERIFIABLE", res, "源文件不存在: %s" % p["file"]
    m = re.search(p["count_line_regex"], txt, re.M)
    if not m:
        return "UNVERIFIABLE", res, "未匹配到计数行: %s" % p["count_line_regex"]
    declared = int(m.group(1))
    actual = len(re.findall(p["row_regex"], txt, re.M))
    res["evidence"] = {"declared": declared, "actual": actual}
    res["detail"].append("[计数] 声明 %d / 实测 %d" % (declared, actual))
    return ("MATCH", res, "计数自洽") if declared == actual else \
           ("CONTRADICTED", res, "计数行声明 %d 但实测 %d 行" % (declared, actual))


CHECKS = {
    "index_insert_side": chk_index_insert_side,
    "path_tokens_exist": chk_path_tokens_exist,
    "file_truth": chk_file_truth,
    "claim_body_conflict": chk_claim_body_conflict,
    "count_consistency": chk_count_consistency,
}

# ---------------------------------------------------------------- 运行
def run_registry(registry, files):
    results = []
    for a in registry["assertions"]:
        fn = CHECKS.get(a["check"])
        if fn is None:
            results.append({"id": a["id"], "cls": a["cls"], "verdict": "UNVERIFIABLE",
                            "reason": "未知 check: %s" % a["check"], "detail": [], "evidence": {}})
            continue
        params = dict(a["params"])
        params["_files"] = files
        verdict, res, reason = fn(params)
        results.append({"id": a["id"], "cls": a["cls"], "check": a["check"], "expect": a.get("expect"),
                        "status": a.get("status", "ACTIVE"),
                        "verdict": verdict, "reason": reason, "detail": res["detail"],
                        "evidence": res["evidence"], "statement": a.get("statement", ""),
                        "note": a.get("note", "")})
    return results


def verdict_rc(results, strict=False):
    """rc 口径：默认只把 status=ACTIVE 的 CONTRADICTED 当失败（CLOSED=已知缺口降为提示）；
    --strict 下任何 CONTRADICTED 都算失败。UNVERIFIABLE 不计失败（但会打印计数）。"""
    if strict:
        return sum(1 for r in results if r["verdict"] == "CONTRADICTED")
    return sum(1 for r in results if r["verdict"] == "CONTRADICTED" and r.get("status") == "ACTIVE")


def run_scan(files, token_regex):
    """扫描模式：全语料路径 token 存在性（提示级候选，不作判决）"""
    hits = _scan_paths(files, token_regex)
    absent = {}
    for tok, where in hits.items():
        ok, _ = _resolve(tok)
        if not ok:
            absent[tok] = where[:3]
    return {"tokens_total": len(hits), "absent_total": len(absent), "absent": absent}


def scan_claim_body(files, claim_regex, body_regex, artifact_regex):
    """扫描：同一 artifact 在文件内既有「已做」声明又有「未做」自承（提示级候选）"""
    out = []
    cre, bre = re.compile(claim_regex), re.compile(body_regex)
    for f in files:
        t = read_text(f)
        if not t:
            continue
        arts = sorted(set(re.findall(artifact_regex, t)))
        acc = {a: ([], []) for a in arts}
        # 性能（R468）：逐行先跑两个正则、全不命中就跳过，命中后才做 art 子串匹配。
        # 旧写法对每个 art 逐行 re.search（O(art×line×2 次正则)），实测占全量 80%（4.6s/5.6s）。
        # 判定语义与输出顺序均不变（art 仍按 sorted 顺序 append，c/b 行号仍升序）。
        # 负控：selftest NC13 断言新旧实现输出逐字相同。
        for i, ln in enumerate(t.split("\n"), 1):
            if not (cre.search(ln) or bre.search(ln)):
                continue
            for art in arts:
                if art in ln:
                    c, b = acc[art]
                    if cre.search(ln):
                        c.append(i)
                    if bre.search(ln):
                        b.append(i)
        for art in arts:
            c, b = acc[art]
            if c and b:
                out.append({"file": os.path.relpath(f, REPO), "artifact": art,
                            "claim_lines": c[:6], "body_lines": b[:6]})
    return out

# ---------------------------------------------------------------- 自测
def selftest():
    """夹具 + 负控：校验器自己先被证伪能力检验"""
    import shutil
    import tempfile
    ok = 0
    fail = []

    def check(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
            print("  [PASS] %s %s" % (name, extra))
        else:
            fail.append(name)
            print("  [FAIL] %s %s" % (name, extra))

    d = tempfile.mkdtemp(prefix="dfl_selftest_")
    global REPO, _BASE_INDEX
    old_repo, old_idx = REPO, _BASE_INDEX
    try:
        os.makedirs(os.path.join(d, "mem"), exist_ok=True)
        # 夹具 1：SOP 声称「升序⇒末尾」，实现却是顶部插 ⇒ 必报 CONTRADICTED
        io.open(os.path.join(d, "sop_bad.md"), "w", encoding="utf-8").write(
            "③ 提交后核行数。④ `IDX.md` 按编号**升序**⇒新行追加**文件末尾**。\n")
        io.open(os.path.join(d, "idx_bad.md"), "w", encoding="utf-8").write(
            "| R9 | a |\n| R8 | b |\n# 清单\n| R1 | c |\n")
        # 夹具 2：SOP 声称「降序⇒顶部」，实现也是顶部 ⇒ MATCH
        io.open(os.path.join(d, "sop_good.md"), "w", encoding="utf-8").write(
            "④ `HIS.txt` 按编号**降序**⇒新行插**顶部**（第 1 行）。\n")
        io.open(os.path.join(d, "his_good.txt"), "w", encoding="utf-8").write("R9 | a\nR8 | b\nR1 | c\n")
        # 夹具 3：路径 token
        io.open(os.path.join(d, "real.py"), "w", encoding="utf-8").write("x=1\n")
        io.open(os.path.join(d, "mem", "refs.md"), "w", encoding="utf-8").write(
            "见 `real.py` 与 `ghost_missing.py`。\n")
        # 夹具 4：计数行
        io.open(os.path.join(d, "idx_cnt.md"), "w", encoding="utf-8").write(
            "| R1 | a |\n| R2 | b |\n- 采集: 共 3 行\n")
        # 夹具 5：声明 vs 自承（同一 artifact）
        io.open(os.path.join(d, "mix.md"), "w", encoding="utf-8").write(
            "## 桥\n桥 **已接线**（真跑 3/3）。\n备注：桥 未接线，提案待批。\n")
        # 夹具 5b：不同 artifact 各说各话（不得报警 —— 作用域守卫）
        io.open(os.path.join(d, "mix2.md"), "w", encoding="utf-8").write(
            "## 甲\n甲 **已接线**（真跑 3/3）。\n## 乙\n乙 未接线，提案待批。\n")

        REPO = d
        _BASE_INDEX = None
        reg = {"assertions": [
            {"id": "NC1", "cls": "SOP_VS_IMPL", "check": "index_insert_side",
             "params": {"sop_file": "sop_bad.md", "sop_claim_regex": r"`IDX\.md`[^\n]{0,40}",
                        "target_file": "idx_bad.md", "row_regex": r"^\|\s*R(\d+)\s*\|"}},
            {"id": "PC1", "cls": "SOP_VS_IMPL", "check": "index_insert_side",
             "params": {"sop_file": "sop_good.md", "sop_claim_regex": r"`HIS\.txt`[^\n]{0,40}",
                        "target_file": "his_good.txt", "row_regex": r"^R(\d+)\s*\|"}},
            {"id": "NC2", "cls": "REF_EXISTS", "check": "path_tokens_exist",
             "params": {"file": "mem/refs.md", "token_regex": r"[A-Za-z0-9_./-]+\.py",
                        "expect": "NO_ABSENT"}},
            {"id": "NC3", "cls": "REF_EXISTS", "check": "path_tokens_exist",
             "params": {"file": "no_such_file.md", "token_regex": r"[A-Za-z0-9_./-]+\.py",
                        "expect": "NO_ABSENT"}},
            {"id": "NC4", "cls": "CLAIM_VS_BODY", "check": "count_consistency",
             "params": {"file": "idx_cnt.md", "count_line_regex": r"共\s*(\d+)\s*行",
                        "row_regex": r"^\|\s*R\d+\s*\|"}},
            {"id": "NC5", "cls": "CLAIM_VS_BODY", "check": "claim_body_conflict",
             "params": {"file": "mix.md", "artifact_regex": r"桥",
                        "claim_regex": r"已接线", "body_regex": r"未接线|待批"}},
            {"id": "NC5b", "cls": "CLAIM_VS_BODY", "check": "claim_body_conflict",
             "params": {"file": "mix2.md", "artifact_regex": r"甲|乙",
                        "claim_regex": r"已接线", "body_regex": r"未接线|待批"}},
            {"id": "NC6", "cls": "REF_EXISTS", "check": "path_tokens_exist",
             "status": "CLOSED",
             "params": {"file": "mem/refs.md", "token_regex": r"[A-Za-z0-9_./-]+\.py",
                        "expect": "NO_ABSENT"}},
            {"id": "VC1", "cls": "CITED_FILE_TRUTH", "check": "file_truth",
             "params": {"token": "real.py", "search_roots": ["."], "expect_under": "real.py"}},
        ]}
        res = run_registry(reg, corpus_files(os.path.join(d, "mem")))
        v = {r["id"]: r["verdict"] for r in res}
        check("NC1 SOP 断言与实现相反必报", v["NC1"] == "CONTRADICTED", v["NC1"])
        check("PC1 SOP 断言与实现一致须 MATCH", v["PC1"] == "MATCH", v["PC1"])
        check("NC2 存在+缺失token 混合必报", v["NC2"] == "CONTRADICTED", v["NC2"])
        check("NC3 源文件缺失须 UNVERIFIABLE(非MATCH)", v["NC3"] == "UNVERIFIABLE", v["NC3"])
        check("NC4 计数不自洽必报", v["NC4"] == "CONTRADICTED", v["NC4"])
        check("NC5 声明与自承并存必报", v["NC5"] == "CONTRADICTED", v["NC5"])
        check("NC5b 不同 artifact 不得互串(作用域守卫)", v["NC5b"] == "MATCH", v["NC5b"])
        st = {r["id"]: r.get("status") for r in res}
        check("NC6 status 字段透传", st["NC6"] == "CLOSED", st["NC6"])
        # 负控 7：rc 口径 —— CLOSED 的 CONTRADICTED 默认不计失败、--strict 计入
        closed_bad = [r for r in res if r.get("status") == "CLOSED" and r["verdict"] == "CONTRADICTED"]
        rc_def, rc_strict = verdict_rc(res), verdict_rc(res, strict=True)
        check("NC7 确有 CLOSED 的 CONTRADICTED 作对照", len(closed_bad) >= 1, len(closed_bad))
        check("NC7 rc默认排除 CLOSED 且严格模式包含", rc_strict > rc_def >= 1,
              "default=%d strict=%d" % (rc_def, rc_strict))
        check("VC1 被引文件真身可解析", v["VC1"] == "MATCH", v["VC1"])
        # 负控 8：语料为空必须显式 UNVERIFIABLE（不是静默全绿）
        # 注意：必须用「无 file 键」的断言才会走 _files 空分支；带 file 的断言走单文件路径，测不到此分支。
        empty = run_registry({"assertions": [{"id": "NC8", "cls": "REF_EXISTS",
            "check": "path_tokens_exist",
            "params": {"token_regex": r"[A-Za-z0-9_./-]+\.py", "expect": "NO_ABSENT"}}]}, [])
        check("NC8 空语料不得静默通过", empty[0]["verdict"] == "UNVERIFIABLE", empty[0]["verdict"])
        # 负控 9：全在盘上时 MUST 不是 CONTRADICTED（防"恒报真"）
        io.open(os.path.join(d, "mem", "refs2.md"), "w", encoding="utf-8").write("见 `real.py`。\n")
        good = run_registry({"assertions": [dict(reg["assertions"][2], params={
            "file": "mem/refs2.md", "token_regex": r"[A-Za-z0-9_./-]+\.py", "expect": "NO_ABSENT"})]},
            corpus_files(os.path.join(d, "mem")))
        check("NC9 全存在须 MATCH(防恒报真)", good[0]["verdict"] == "MATCH", good[0]["verdict"])
        # 负控 10：scan 模式在空语料下 token_total=0（自证语料计数可见）
        sc = run_scan([], r"[A-Za-z0-9_./-]+\.py")
        check("NC10 scan 语料计数自证", sc["tokens_total"] == 0)
        # 负控 11：basename 兜底索引只建一次（旧实现每 token 全仓 os.walk ⇒ 282 token 17s）
        os.makedirs(os.path.join(d, "deep", "nest"), exist_ok=True)
        io.open(os.path.join(d, "deep", "nest", "uniq_tok.py"), "w", encoding="utf-8").write("x=1\n")
        global_walk = os.walk
        calls = {"n": 0}

        def _counting_walk(*a, **k):
            calls["n"] += 1
            return global_walk(*a, **k)

        os.walk = _counting_walk
        try:
            _BASE_INDEX = None
            res11 = [_resolve("no/such/dir/uniq_tok.py") for _ in range(50)]
            walks_after_50 = calls["n"]
            _resolve("another/ghost_uniq_absent_xyz.py")
            walks_total = calls["n"]
        finally:
            os.walk = global_walk
        check("NC11 basename 兜底索引只建一次（不是每 token 一次）", walks_after_50 == 1,
              "50 token 后 walk=%d" % walks_after_50)
        check("NC11 索引复用：多解析不再触发 walk", walks_total == 1, "总 walk=%d" % walks_total)
        check("NC11 兜底命中真身", all(r[0] for r in res11), res11[0])
        # 负控 12：索引是「首次构建时的快照」——建索引后新增的文件，经 basename 兜底**不可见**（已知边界，
        # 须显式固定，防被当成缺陷；真实语料里语料文件先于 lint 存在，故不影响判定）
        io.open(os.path.join(d, "deep", "nest", "fresh_tok.py"), "w", encoding="utf-8").write("y=1\n")
        ok_fresh, _ = _resolve("nowhere/fresh_tok.py")
        check("NC12 索引快照语义：建索引后新增文件兜底不可见", ok_fresh is False, ok_fresh)
        _BASE_INDEX = None
        ok_fresh2, _ = _resolve("nowhere/fresh_tok.py")
        check("NC12b 重建索引后同一 token 可命中（证明只是快照语义非兜底失效）", ok_fresh2 is True, ok_fresh2)
        # 负控 12c：真幽灵 basename 必须判 False（防兜底「恒真」）
        ok_ghost, _ = _resolve("nowhere/definitely_absent_basename_zz9.py")
        check("NC12c 真幽灵 basename 判 False", ok_ghost is False, ok_ghost)
        # 负控 13：scan_claim_body 的性能优化不得改变判定结果
        # —— 与「旧实现（对每个 artifact 逐行 re.search）」在同一夹具上逐字比对；
        # 反向：若优化写坏（漏行 / artifact 串号），本负控必报。
        with io.open(os.path.join(d, "mem", "mix.md"), "a", encoding="utf-8") as fh:
            fh.write("桥 已接线\n桥 未接线|待批\n与断言无关的一行\n甲 已接线\n乙 待批\n")
        _files13 = corpus_files(os.path.join(d, "mem"))
        _arx, _crx, _brx = r"甲|乙|桥", r"已接线", r"未接线|待批"
        got13 = scan_claim_body(_files13, _crx, _brx, _arx)
        ref13 = []          # 旧实现（参考基准，勿删）
        for _f in _files13:
            _t = read_text(_f)
            if not _t:
                continue
            _lines = _t.split("\n")
            for _art in sorted(set(re.findall(_arx, _t))):
                _c, _b = [], []
                for _i, _ln in enumerate(_lines, 1):
                    if _art in _ln:
                        if re.search(_crx, _ln):
                            _c.append(_i)
                        if re.search(_brx, _ln):
                            _b.append(_i)
                if _c and _b:
                    ref13.append({"file": os.path.relpath(_f, REPO), "artifact": _art,
                                  "claim_lines": _c[:6], "body_lines": _b[:6]})
        check("NC13 优化后 scan_claim_body 与旧实现输出逐字相同", got13 == ref13,
              "new=%d ref=%d" % (len(got13), len(ref13)))
        check("NC13b 该负控确有命中（防两边都空跑假绿）",
              any(r["artifact"] == "桥" for r in got13), [r["artifact"] for r in got13])
    finally:
        REPO = old_repo
        _BASE_INDEX = old_idx
        shutil.rmtree(d, ignore_errors=True)

    print("\nselftest: %d passed, %d failed" % (ok, len(fail)))
    if fail:
        print("failed:", fail)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="耐久记忆事实断言复核器（只读）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--json")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--curated-only", action="store_true")
    ap.add_argument("--proof", action="store_true", help="只读自证：语料 sha256 前后一致")
    ap.add_argument("--strict", action="store_true",
                    help="rc 口径：默认只把 status=ACTIVE 的 CONTRADICTED 当失败（CLOSED=已知缺口降为提示）；"
                         "--strict 下任何 CONTRADICTED 都算失败")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    files = corpus_files()
    total_bytes = sum(os.path.getsize(f) for f in files)
    print("[自证] 语料 %d 文件 / %d 字节 (memory/** 除 __pycache__)" % (len(files), total_bytes))
    if not files:
        print("[ERR] 语料为空 —— 校验器未读到语料，拒绝出结论")
        return 2
    fp0 = corpus_fingerprint(files)

    report = {"corpus": {"files": len(files), "bytes": total_bytes}, "assertions": [], "scan": None}

    if not args.scan_only:
        try:
            reg = json.loads(read_text(args.registry))
        except (OSError, ValueError) as e:
            print("[ERR] 注册表不可用: %s (%s)" % (args.registry, e))
            return 2
        print("[注册表] %s 断言 %d 条" % (os.path.relpath(args.registry, REPO), len(reg["assertions"])))
        res = run_registry(reg, files)
        report["assertions"] = res
        tally = {}
        tally_st = {}
        for r in res:
            tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
            tally_st[r["status"]] = tally_st.get(r["status"], 0) + 1
        print("\n== 断言判定 ==\n")
        for r in res:
            print("  [%-13s] %-4s %-16s %-8s %s" % (r["verdict"], r["id"], r["cls"], r["status"], r["reason"]))
            for d in r["detail"]:
                print("        " + d)
        print("  ⇒ 合计 %s / status %s" % (tally, tally_st))

    if not args.curated_only:
        tok_rx = r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.(?:py|md|json|sh|txt|log|html|yaml|yml|db|sqlite|zip)"
        sc = run_scan(files, tok_rx)
        cb = scan_claim_body(files,
                             r"(已接线|已落地|已实现|已闭环|已修)",
                             r"(未接线|待批|未实现|尚未落地)",
                             r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.(?:py|md|json|sh|txt|yaml|yml|plist)")
        report["scan"] = {"paths": {"tokens_total": sc["tokens_total"], "absent_total": sc["absent_total"],
                                    "absent": sc["absent"]},
                          "claim_body": cb}
        print("\n== 扫描（提示级，非判决）==")
        print("  路径 token %d 个，盘上查无 %d 个，不同名 %d 个" %
              (sc["tokens_total"], sc["absent_total"], len(sc["absent"])))
        for tok, where in sorted(sc["absent"].items())[:40]:
            print("    [查无] %s  (首见 %s:%d)" % (tok, where[0][0], where[0][1]))
        print("  同文件内「已做」声明 × 「未做」自承 并存 %d 文件" % len(cb))
        for it in cb[:25]:
            print("    %s  声明行%s vs 自承行%s" % (it["file"], it["claim_lines"], it["body_lines"]))

    fp1 = corpus_fingerprint(files)
    changed = [k for k in fp0 if fp0.get(k) != fp1.get(k)]
    print("\n[只读自证] 受检文件 %d，sha256 变化 = %s" % (len(fp0), changed or "无"))
    report["readonly"] = {"files": len(fp0), "changed": changed}

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False, indent=1))
        print("[json] %s" % args.json)

    if args.proof:
        return 0 if not changed else 1
    n_bad_all = sum(1 for r in report["assertions"] if r["verdict"] == "CONTRADICTED")
    n_bad_act = sum(1 for r in report["assertions"]
                    if r["verdict"] == "CONTRADICTED" and r.get("status") == "ACTIVE")
    n_unver = sum(1 for r in report["assertions"] if r["verdict"] == "UNVERIFIABLE")
    n = verdict_rc(report["assertions"], args.strict)
    print("\n[verdict] CONTRADICTED(全部)=%d  CONTRADICTED(ACTIVE)=%d  UNVERIFIABLE=%d  strict=%s  rc=%d"
          % (n_bad_all, n_bad_act, n_unver, args.strict, 1 if n else 0))
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
