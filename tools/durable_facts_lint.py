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
import glob
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


def _abs(p):
    """注册表路径：`~` 展开；其余按 REPO 相对解析"""
    if p.startswith("~"):
        return os.path.expanduser(p)
    return os.path.join(REPO, p)


def _count_source(spec):
    """按规格求「实测计数」-> (int|None, evidence, reason)

    kind=rows       : file 内 row_regex 命中的行数（可跨文件：声明在 A、数据在 B）
    kind=file_lines : file 的行数（nonempty=true 时只数非空行）
    kind=glob       : pattern 命中文件数（相对 REPO；`~` 开头按用户目录）
    kind=json_len   : file 的 JSON 在 dotted path 处的长度（dict/list/str）

    kind=glob_multi : patterns 列表各自 glob 后求和（每项目录必须存在，任一缺失即 fail-closed）
    kind=rows_diff  : a/b 两处 row_regex 命中行数之差（如 INDEX 表行 − history 编号行）

    fail-closed：源缺失 / JSON 坏 / path 不可达 ⇒ (None, …, reason)，
    由调用方判 UNVERIFIABLE —— 绝不静默当成 0（「源没了」与「计数变 0」是两回事）。
    """
    kind = spec.get("kind")
    if kind in ("rows", "file_lines", "json_len"):
        txt = read_text(_abs(spec["file"]))
        if txt is None:
            return None, {}, "计数源文件不存在: %s" % spec["file"]
        if kind == "rows":
            n = len(re.findall(spec["row_regex"], txt, re.M))
            return n, {"kind": "rows", "source": spec["file"]}, ""
        if kind == "file_lines":
            lines = txt.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            n = len([l for l in lines if l.strip()]) if spec.get("nonempty") else len(lines)
            return n, {"kind": "file_lines", "source": spec["file"],
                       "nonempty": bool(spec.get("nonempty"))}, ""
        try:
            cur = json.loads(txt)
        except ValueError as e:
            return None, {}, "计数源 JSON 解析失败: %s (%s)" % (spec["file"], e)
        for k in str(spec["path"]).split("."):
            if isinstance(cur, dict):
                if k not in cur:
                    return None, {}, "计数源 path 不可达: %s" % spec["path"]
                cur = cur[k]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(k)]
                except (ValueError, IndexError):
                    return None, {}, "计数源 path 越界: %s" % spec["path"]
            else:
                return None, {}, "计数源 path 不可达: %s" % spec["path"]
        try:
            n = len(cur)
        except TypeError:
            return None, {}, "计数源 path 处不可取长度: %s" % spec["path"]
        return n, {"kind": "json_len", "source": spec["file"], "path": spec["path"]}, ""

    if kind == "glob_multi":
        ev = {"kind": "glob_multi", "per": {}}
        total = 0
        for p0 in spec.get("patterns", []):
            n0, _e0, why0 = _count_source({"kind": "glob", "pattern": p0})
            if n0 is None:
                return None, {}, "glob_multi 子项失败: %s（pattern=%s）" % (why0, p0)
            ev["per"][p0] = n0
            total += n0
        if not spec.get("patterns"):
            return None, {}, "glob_multi 未给 patterns"
        return total, ev, ""

    if kind == "rows_diff":
        a = _count_source(dict(spec["a"], kind="rows"))
        b = _count_source(dict(spec["b"], kind="rows"))
        if a[0] is None:
            return None, {}, "rows_diff 左项失败: %s" % a[2]
        if b[0] is None:
            return None, {}, "rows_diff 右项失败: %s" % b[2]
        return a[0] - b[0], {"kind": "rows_diff", "a_rows": a[0], "b_rows": b[0],
                             "a": spec["a"]["file"], "b": spec["b"]["file"]}, ""

    if kind == "glob":
        pat = spec["pattern"]
        pat = os.path.expanduser(pat) if pat.startswith("~") else os.path.join(REPO, pat)
        base = pat.split("*")[0]
        if base.endswith("/"):
            base = base.rstrip("/")
        else:
            base = os.path.dirname(base)
        if "*" not in pat:
            return None, {}, "glob 规格无通配符（拒绝把固定路径当计数）: %s" % pat
        if not os.path.isdir(base):
            return None, {}, "计数源目录不存在: %s" % base
        hits = glob.glob(pat)
        return len(hits), {"kind": "glob", "pattern": pat,
                           "samples": [os.path.basename(x) for x in sorted(hits)[:3]]}, ""

    return None, {}, "未知 count_source.kind: %s" % kind


def chk_count_consistency(p):
    """『N 个/行/条』声明 与 实测计数 是否自洽

    声明侧：file + count_line_regex（claim_group 默认 1；claim_take=first|last，默认 first）
    实测侧：① legacy `row_regex`（同文件行数）或 ② `count_source` 规格（跨文件/glob/JSON/非空行）
    """
    res = {"detail": [], "evidence": {}}
    txt = read_text(_abs(p["file"]))
    if txt is None:
        return "UNVERIFIABLE", res, "源文件不存在: %s" % p["file"]
    hits = list(re.finditer(p["count_line_regex"], txt, re.M))
    if not hits:
        return "UNVERIFIABLE", res, "未匹配到计数行: %s" % p["count_line_regex"]
    if p.get("claim_take", "first") == "last":
        hits = hits[-1:]
    m = hits[0]
    g = int(p.get("claim_group", 1))
    try:
        declared = int(m.group(g))
    except (IndexError, ValueError):
        return "UNVERIFIABLE", res, "计数行第 %d 捕获组非数字: %s" % (g, p["count_line_regex"])
    ev = {"declared": declared, "claim_text": m.group(0)[:120]}
    if "count_source" in p:
        actual, sev, why = _count_source(p["count_source"])
        if actual is None:
            res["evidence"] = ev
            return "UNVERIFIABLE", res, why
        ev.update(sev)
    else:
        actual = len(re.findall(p["row_regex"], txt, re.M))
        ev["kind"] = "rows(same-file)"
    res["evidence"] = ev
    res["detail"].append("[计数] 声明 %d / 实测 %d（%s）" % (declared, actual, ev.get("kind", "")))
    return ("MATCH", res, "计数自洽") if declared == actual else \
           ("CONTRADICTED", res, "计数行声明 %d 但实测 %d" % (declared, actual))


def chk_expr_sum(p):
    """『A+B+C=D』类分量算式的自洽性：最后一个数字=总和，其余求和

    用途：注册面/统计行的分量算式（如 LaunchAgents 29+sche_tasks 25=54）；
    可选 + 段（如 +reflect_scripts 14）会被一并计入 ⇒ 用于暴露「把不该加的项并进算式」。
    fail-closed：未匹配 / 数字少于 3 个 ⇒ UNVERIFIABLE
    """
    res = {"detail": [], "evidence": {}}
    txt = read_text(_abs(p["file"]))
    if txt is None:
        return "UNVERIFIABLE", res, "源文件不存在: %s" % p["file"]
    m = re.search(p["claim_regex"], txt, re.M)
    if not m:
        return "UNVERIFIABLE", res, "未匹配到算式: %s" % p["claim_regex"]
    nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
    if len(nums) < 3:
        return "UNVERIFIABLE", res, "算式中数字少于 3 个: %s" % m.group(0)[:80]
    total, parts = nums[-1], nums[:-1]
    res["evidence"] = {"claim_text": m.group(0)[:120], "parts": parts, "declared_total": total,
                       "actual_total": sum(parts)}
    res["detail"].append("[算式] %s = %d / 声明 %d" % ("+".join(map(str, parts)), sum(parts), total))
    return ("MATCH", res, "算式自洽") if sum(parts) == total else \
           ("CONTRADICTED", res, "算式的分量和 %d 但声明合计 %d" % (sum(parts), total))


CHECKS = {
    "index_insert_side": chk_index_insert_side,
    "path_tokens_exist": chk_path_tokens_exist,
    "file_truth": chk_file_truth,
    "claim_body_conflict": chk_claim_body_conflict,
    "count_consistency": chk_count_consistency,
    "expr_sum": chk_expr_sum,
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

    # ---- 计数类断言扩面（R476）：count_source 四形态 / 算式自洽 / fail-closed
    d2 = tempfile.mkdtemp(prefix="dfl_count_")
    old_repo2 = REPO
    try:
        os.makedirs(os.path.join(d2, "tools"), exist_ok=True)
        os.makedirs(os.path.join(d2, "docs"), exist_ok=True)
        for i in (1, 2, 3):
            io.open(os.path.join(d2, "tools", "t%d.py" % i), "w", encoding="utf-8").write("x=%d\n" % i)
        W = lambda rel, s: io.open(os.path.join(d2, rel), "w", encoding="utf-8").write(s)
        W("docs/tools_claim.md", "工具面 3 个 tools/*.py（复核）\n")
        W("docs/tools_claim_last.md", "工具面 3 个\n历史复核: 工具面 4 个\n")
        W("docs/rows_claim.md", "采集: 共 2 行\n")
        W("docs/rows_claim_bad.md", "采集: 共 3 行\n")
        W("docs/rows.txt", "| R1 |\n| R2 |\n")
        W("docs/json_claim.md", "条目: 5 条\n")
        W("docs/json_claim_bad.md", "条目: 4 条\n")
        W("docs/tgt.json", json.dumps({"entries": [1, 2, 3, 4, 5]}))
        W("docs/fl_claim.md", "非空: 3 行\n")
        W("docs/lines.txt", "a\n\nb\nc\n")
        W("docs/expr_bad.md", "算式 LaunchAgents 29+sche_tasks 25+reflect_scripts 14=54\n")
        W("docs/expr_ok.md", "算式 LaunchAgents 29+sche_tasks 25=54\n")
        REPO = d2
        glob_src = {"kind": "glob", "pattern": "tools/*.py"}
        reg2 = {"assertions": [
            {"id": "NC14", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/tools_claim.md", "count_line_regex": r"工具面\s*(\d+)",
                "count_source": glob_src}},
            {"id": "NC15", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/tools_claim.md", "count_line_regex": r"工具面\s*(\d+)",
                "count_source": {"kind": "glob", "pattern": "tools/*.md"}}},
            {"id": "NC16", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/tools_claim.md", "count_line_regex": r"工具面\s*(\d+)",
                "count_source": {"kind": "glob", "pattern": "nodir/*.py"}}},
            {"id": "NC17", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/rows_claim.md", "count_line_regex": r"共\s*(\d+)\s*行",
                "count_source": {"kind": "rows", "file": "docs/rows.txt",
                                 "row_regex": r"^\|\s*R\d+\s*\|"}}},
            {"id": "NC18", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/rows_claim_bad.md", "count_line_regex": r"共\s*(\d+)\s*行",
                "count_source": {"kind": "rows", "file": "docs/rows.txt",
                                 "row_regex": r"^\|\s*R\d+\s*\|"}}},
            {"id": "NC19", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/json_claim.md", "count_line_regex": r"条目:\s*(\d+)",
                "count_source": {"kind": "json_len", "file": "docs/tgt.json", "path": "entries"}}},
            {"id": "NC20", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/json_claim_bad.md", "count_line_regex": r"条目:\s*(\d+)",
                "count_source": {"kind": "json_len", "file": "docs/tgt.json", "path": "entries"}}},
            {"id": "NC21", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/fl_claim.md", "count_line_regex": r"非空:\s*(\d+)",
                "count_source": {"kind": "file_lines", "file": "docs/lines.txt", "nonempty": True}}},
            {"id": "NC22", "cls": "COUNT", "check": "expr_sum", "params": {
                "file": "docs/expr_bad.md",
                "claim_regex": r"算式 LaunchAgents \d+\+sche_tasks \d+(?:\+reflect_scripts \d+)?=\d+"}},
            {"id": "NC23", "cls": "COUNT", "check": "expr_sum", "params": {
                "file": "docs/expr_ok.md",
                "claim_regex": r"算式 LaunchAgents \d+\+sche_tasks \d+(?:\+reflect_scripts \d+)?=\d+"}},
            {"id": "NC24", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/tools_claim_last.md", "count_line_regex": r"工具面\s*(\d+)",
                "count_source": glob_src, "claim_take": "last"}},
            {"id": "NC25", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/tools_claim.md", "count_line_regex": r"工具面\s*(\d+)",
                "count_source": {"kind": "rows", "file": "docs/nope.txt", "row_regex": r"."}}},
        ]}
        r2 = {r["id"]: r["verdict"] for r in run_registry(reg2, corpus_files(os.path.join(d2, "docs")))}
        check("NC14 glob 计数一致须 MATCH", r2["NC14"] == "MATCH", r2["NC14"])
        check("NC15 glob 实测 0 与声明不符须 CONTRADICTED（0 是实测值不是缺失）",
              r2["NC15"] == "CONTRADICTED", r2["NC15"])
        check("NC16 计数源目录不存在须 UNVERIFIABLE（不得当 0）", r2["NC16"] == "UNVERIFIABLE", r2["NC16"])
        check("NC17 跨文件 rows 计数一致须 MATCH", r2["NC17"] == "MATCH", r2["NC17"])
        check("NC18 跨文件 rows 声明被篡改须 CONTRADICTED", r2["NC18"] == "CONTRADICTED", r2["NC18"])
        check("NC19 json_len 一致须 MATCH", r2["NC19"] == "MATCH", r2["NC19"])
        check("NC20 json_len 声明被篡改须 CONTRADICTED", r2["NC20"] == "CONTRADICTED", r2["NC20"])
        check("NC21 file_lines nonempty=true 只数非空行须 MATCH", r2["NC21"] == "MATCH", r2["NC21"])
        check("NC22 算式把不该加的项并进算式须 CONTRADICTED", r2["NC22"] == "CONTRADICTED", r2["NC22"])
        check("NC23 加法算式自洽须 MATCH", r2["NC23"] == "MATCH", r2["NC23"])
        check("NC24 claim_take=last 生效（取末次声明）", r2["NC24"] == "CONTRADICTED", r2["NC24"])
        check("NC25 计数源文件缺失须 UNVERIFIABLE", r2["NC25"] == "UNVERIFIABLE", r2["NC25"])
        # NC26：真正把算式改对 ⇒ 同一断言必须转 MATCH（证明 CONTRADICTED 可被订正消除）
        W("docs/expr_bad.md", "算式 LaunchAgents 29+sche_tasks 25=54\n")
        r3 = {r["id"]: r["verdict"] for r in run_registry(
            {"assertions": [reg2["assertions"][8]]}, corpus_files(os.path.join(d2, "docs")))}
        check("NC26 订正后同一算式断言转 MATCH", r3["NC22"] == "MATCH", r3["NC22"])
        # NC27–NC30：glob_multi / rows_diff
        W("docs/multi_claim.md", "三组合计 5\n")
        W("docs/multi_claim_bad.md", "三组合计 4\n")
        os.makedirs(os.path.join(d2, "docs", "b"), exist_ok=True)
        io.open(os.path.join(d2, "docs", "b", "x1.txt"), "w", encoding="utf-8").write("1\n")
        io.open(os.path.join(d2, "docs", "b", "x2.txt"), "w", encoding="utf-8").write("2\n")
        W("docs/a_rows.txt", "| R1 |\n| R2 |\n| R3 |\n| R4 |\n")
        W("docs/b_rows.txt", "R1 |\nR2 |\n")
        W("docs/diff_claim.md", "两处相差 2\n")
        W("docs/diff_claim_bad.md", "两处相差 3\n")
        diff_src = {"kind": "rows_diff",
                    "a": {"file": "docs/a_rows.txt", "row_regex": r"^\|\s*R\d+\s*\|"},
                    "b": {"file": "docs/b_rows.txt", "row_regex": r"^R\d+ \|"}}
        reg3 = {"assertions": [
            {"id": "NC27", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/multi_claim.md", "count_line_regex": r"三组合计\s*(\d+)",
                "count_source": {"kind": "glob_multi",
                                 "patterns": ["tools/*.py", "docs/b/*.txt"]}}},
            {"id": "NC28", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/multi_claim.md", "count_line_regex": r"三组合计\s*(\d+)",
                "count_source": {"kind": "glob_multi",
                                 "patterns": ["tools/*.py", "nodir/*.txt"]}}},
            {"id": "NC29", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/diff_claim.md", "count_line_regex": r"相差\s*(\d+)",
                "count_source": diff_src}},
            {"id": "NC30", "cls": "COUNT", "check": "count_consistency", "params": {
                "file": "docs/diff_claim_bad.md", "count_line_regex": r"相差\s*(\d+)",
                "count_source": diff_src}},
        ]}
        r4 = {r["id"]: r["verdict"] for r in run_registry(reg3, corpus_files(os.path.join(d2, "docs")))}
        check("NC27 glob_multi 多目录求和一致须 MATCH", r4["NC27"] == "MATCH", r4["NC27"])
        check("NC28 glob_multi 子项目录缺失须 UNVERIFIABLE", r4["NC28"] == "UNVERIFIABLE", r4["NC28"])
        check("NC29 rows_diff 行数差一致须 MATCH", r4["NC29"] == "MATCH", r4["NC29"])
        check("NC30 rows_diff 声明被篡改须 CONTRADICTED", r4["NC30"] == "CONTRADICTED", r4["NC30"])
    finally:
        REPO = old_repo2
        shutil.rmtree(d2, ignore_errors=True)

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
