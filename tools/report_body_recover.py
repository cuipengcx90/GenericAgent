#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自主报告正文补全（从 L4 会话 zip 静态还原历史报告正文）

背景（R382 任务 / TODO L12）：
  history.txt 有 349 个编号，但两树（memory/ + temp/autonomous_reports）在盘正文并集仅 ~122 份。
  早期（2026-04 ~ 05）报告的写盘方式与后期不同：
    - 后期：file_write 工具调用（正文在 tool_use.input.content）→ 既有 harvest() 能抓
    - 早期：**code_run 脚本内联**（`report = \"\"\"...\"\"\"` + `Path('temp/autonomous_reports/Rxxx_*.md').write_text(report)`）
            → harvest() 抓不到（正文不是 tool_use 的 content，而是在脚本源码里的字符串字面量）
  本工具用 **ast 静态解析**（绝不执行脚本：这些脚本内含 complete_task，执行会真落账并推送）还原该字面量。

用法：
  python3 tools/report_body_recover.py scan  --zip memory/L4_raw_sessions/2026-04.zip [--json OUT]
  python3 tools/report_body_recover.py apply --zip memory/L4_raw_sessions/2026-04.zip [--out DIR] [--manifest PATH]

判据（验收②）：
  正文 = len >= MIN_LEN(300) 且含 Markdown 结构（`##`）+ 以 `#` 开头；否则记入不可恢复清单并附原因。

安全：默认 dry-run（`apply` 才写盘）；**不覆盖已有正文**（存在即跳过；同号不同内容记 conflict）；
      manifest 记录每个恢复文件的来源（zip:member、时间戳、字节数、sha256）以便回溯。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import warnings
import zipfile
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)

GA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GA / "tools"))
import autonomous_reports_archive as ara  # noqa: E402  (复用 _split_sections/_items/history_lines)

MEM = GA / "memory" / "autonomous_reports"
TMP = GA / "temp" / "autonomous_reports"
ZIPDIR = GA / "memory" / "L4_raw_sessions"
MIN_LEN = 300
REP_RE = re.compile(r"autonomous_reports/(R(\d{1,3})_[^\s'\"]{1,90}?\.md)")
# 报告名（可带路径前缀也可裸名；早期脚本用裸名写进 cwd 再被 complete_task 搬运）
REPORT_RE = re.compile(r"R(\d{1,3})_[^\s'\"\\/:*?<>|]{1,90}?\.md")
TS_RE = re.compile(r"^=== (?:Response|Prompt) === (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


# ---------------------------------------------------------------- 状态盘点
def disk_numbers():
    """-> {rnum: [Path, ...]} 两树在盘正文"""
    out: dict[int, list[Path]] = {}
    for d in (MEM, TMP):
        if not d.is_dir():
            continue
        for f in d.glob("R*.md"):
            m = re.match(r"R(\d{1,3})[_-]", f.name)
            if m:
                out.setdefault(int(m.group(1)), []).append(f)
    return out


def history_numbers():
    src = TMP / "history.txt"
    if not src.exists():
        src = MEM / "history.txt"
    return ara.history_lines()


# ---------------------------------------------------------------- 抽取
def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def extract_from_script(script: str):
    """从 code_run 脚本静态抽取报告正文 → [(rnum, filename, body|None, reason)]"""
    try:
        tree = ast.parse(script)
    except SyntaxError as e:
        return [(None, "", None, f"syntax-error:{e.msg}")]
    return _write_calls(tree)


def _resolve_name(expr, var2name):
    """从表达式解析出报告文件名：字面量 → 变量（var2name 多跳）→ ''"""
    if expr is None:
        return ""
    m = REPORT_RE.search(ast.dump(expr))
    if m:
        return m.group(0)
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name) and sub.id in var2name:
            return var2name[sub.id]
    return ""


def _write_calls(tree):
    """遍历写调用 → [(rnum, filename|'', body|None, reason)]。

    文件名四路识别：① 目标字面量（含全路径或裸名）② 目标变量（含 `p = Path(...)`
    绑定与 `with open(...) as f` 绑定）③ 正文首行标题 `# R123 ...` ④ 无。
    只认形如 `R<数字>_*.md` 的报告名，其它写盘调用一律忽略。
    """
    lits, var2name, handlers = {}, {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                if (s := _const_str(node.value)) is not None:
                    lits[t.id] = s
                m = REPORT_RE.search(ast.dump(node.value))  # p = Path('R280_x.md')
                if m:
                    var2name[t.id] = m.group(0)
    # 文件名变量多跳解析：report_path → Path(report_path) → f 等（迭代至不动点）
    for _ in range(4):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if not isinstance(t, ast.Name) or t.id in var2name:
                        continue
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Name) and sub.id in var2name:
                            var2name[t.id] = var2name[sub.id]
                            break
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for wi in node.items:
                if not isinstance(wi.optional_vars, ast.Name):
                    continue
                nm = _resolve_name(wi.context_expr, var2name)
                if nm:
                    handlers[wi.optional_vars.id] = nm
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in ("write_text", "write") and node.args):
            continue
        name = _resolve_name(f.value, var2name)
        if not name and isinstance(f.value, ast.Name):
            name = handlers.get(f.value.id, "")
        a = node.args[0]
        if (s := _const_str(a)) is not None:
            body, reason = s, "literal-write"
        elif isinstance(a, ast.Name) and a.id in lits:
            body, reason = lits[a.id], "literal-var-write"
        elif isinstance(a, ast.JoinedStr):
            body, reason = None, "fstring-dynamic"
        elif isinstance(a, ast.Name):
            body, reason = None, f"var-dynamic:{a.id}"
        else:
            body, reason = None, "expr-dynamic"
        if not name:
            m = REPORT_RE.search(body or "")
            if m:
                name = m.group(0)
        if not name:
            continue  # 与报告无关的写盘
        rn = int(REPORT_RE.match(name).group(1))
        out.append((rn, name, body, reason))
    return out


def _unescape_layers(s, max_layers=4):
    """逐层还原 JSON 转义（\\n → 换行等），无变化即停。"""
    for _ in range(max_layers):
        s2 = (s.replace("\\\\", "\\").replace("\\n", "\n")
              .replace('\\"', '"').replace("\\t", "\t").replace("\\'", "'"))
        if s2 == s:
            return s
        s = s2
    return s


DUMP_RE = re.compile(r"={3,}\s*(R\d{1,3}_[^\n=]{1,90}?\.md)\s*={3,}")


def dump_blocks(text):
    """会话 stdout 里 `=== R<n>_xxx.md ===` 后跟全文的 dump 块 → [(name, body)]

    同名多次 dump 取最长块；解码后必须以 Markdown 标题开头才收。
    """
    ms = list(DUMP_RE.finditer(text))
    if not ms:
        return []
    best = {}
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        nm = m.group(1)
        if end - m.end() > best.get(nm, (0, 0, 0))[0]:
            best[nm] = (end - m.end(), m.end(), end)
    out = []
    for nm, (_, a, b) in best.items():
        body = _unescape_layers(text[a:b]).strip()
        if body.startswith("#"):
            out.append((nm, body))
    return out


def scan_zip(zp: Path, log=print):
    """扫描一个 zip → (cands, parse_stats)  cands: list[dict]"""
    cands, stats = [], {"members": 0, "code_run_with_rep_path": 0, "syntax_err": 0, "file_write": 0, "dump_blocks": 0}
    zf = zipfile.ZipFile(zp)
    for name in [n for n in zf.namelist() if n.endswith(".txt")]:
        stats["members"] += 1
        text = zf.read(name).decode("utf-8", errors="replace")
        ts_map, cur = {}, None
        for line in text.splitlines():
            m = TS_RE.match(line)
            if m:
                cur = m.group(1)
            ts_map.setdefault(line[:40], cur)
        for kind, blk in ara._split_sections(text):
            if kind != "Response":
                continue
            for it in ara._items(blk):
                if not isinstance(it, dict) or it.get("type") != "tool_use":
                    continue
                nm, inp = it.get("name"), (it.get("input") or {})
                if nm == "code_run":
                    script = inp.get("script") or ""
                    if not REPORT_RE.search(script):
                        continue
                    stats["code_run_with_rep_path"] += 1
                    for rn, fn, body, reason in extract_from_script(script):
                        if reason.startswith("syntax-error"):
                            stats["syntax_err"] += 1
                        cands.append({"rnum": rn, "name": fn, "body": body, "reason": reason,
                                      "kind": "code_run", "src": f"{zp.name}:{name}"})
                elif nm in ("file_write", "file_patch"):
                    p = str(inp.get("path") or "")
                    m = REPORT_RE.search(p)
                    if not m:
                        continue
                    stats["file_write"] += 1
                    body = inp.get("content") or inp.get("new_content")
                    cands.append({"rnum": int(m.group(1)),
                                  "name": p.split("autonomous_reports/")[-1],
                                  "body": body if isinstance(body, str) else None,
                                  "reason": "file_write-content" if isinstance(body, str) else f"file_{nm}-no-content",
                                  "kind": nm, "src": f"{zp.name}:{name}"})
        for nm_, body in dump_blocks(text):
            stats["dump_blocks"] += 1
            rr = REPORT_RE.match(nm_)
            cands.append({"rnum": int(rr.group(1)) if rr else None, "name": nm_, "body": body,
                          "reason": "dump-block", "kind": "dump", "src": f"{zp.name}:{name}"})
    log(f"[scan] {zp.name}: members={stats['members']} code_run(含报告路径)={stats['code_run_with_rep_path']} "
        f"file_write={stats['file_write']} syntax_err={stats['syntax_err']} → 候选 {len(cands)}")
    return cands, stats


# ---------------------------------------------------------------- 判据
def judge(body):
    """-> (is_body, note)"""
    if body is None:
        return False, "无静态正文"
    b = body.strip()
    if len(b) < MIN_LEN:
        return False, f"过短({len(b)}B<{MIN_LEN})"
    if not b.startswith("#"):
        return False, "非Markdown标题开头"
    if "##" not in b:
        return False, "无二级标题结构"
    return True, f"正文({len(b)}B,{b.count(chr(10)) + 1}行)"


# ---------------------------------------------------------------- 编号对账
def _hist_titles():
    """{rnum: 标题}（history.txt 第 4 列）。早期文件名前缀=规范号，正文 H1 常为自编号 ⇒ 需对账。"""
    out = {}
    for n, line in (ara.history_lines() or {}).items():
        parts = [p.strip() for p in str(line).split("|")]
        out[int(n)] = parts[3] if len(parts) > 3 else ""
    return out


def h1_title(body):
    for ln in (body or "").splitlines():
        s = ln.strip()
        if s.startswith("#"):
            return s.lstrip("# ").strip()
    return ""


def _toks(s):
    """中英混合分词：ASCII 词 + CJK 二元组"""
    s = re.sub(r"[\s`*_（）()\[\]【】:：,，.。/|]+", " ", s)
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]+", s) if len(w) > 2}
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", s))
    for ch in "的地了和与及对为在是有":
        cjk = cjk.replace(ch, "")
    words |= {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return words


def title_score(body, hist_title):
    """正文 H1 与 history 标题的重叠度（0~1，Jaccard 变体）"""
    if not hist_title:
        return 0.0
    a, b = _toks(h1_title(body)), _toks(hist_title)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


MATCH_MIN = 0.34
HIST = {}   # {rnum: history 标题}，main() 里注入


def _hist_tok_cache(hist_titles):
    return {n: _toks(t) for n, t in hist_titles.items()}


def number_hypotheses(name, body):
    """候选规范号假设：文件名首号、双编号第二号、正文 H1 的 `R<n>`"""
    hyps = []
    for m in re.finditer(r"R(\d{1,3})", name):
        hyps.append(int(m.group(1)))
    h1 = h1_title(body)
    for m in re.finditer(r"R?(\d{1,3})", h1):
        hyps.append(int(m.group(1)))
    return [h for h in hyps if 1 <= h <= 999]


def best_hist_match(body, hist_titles, cache, hyps=()):
    """正文 ↔ history 标题对账，返回 (规范号, 分数, 次高分)。

    早期文件名/H1 编号都不可信（同一会话内会跨会话全局重编号），故以**标题对账**为准；
    但编号假设（文件名号/H1 号）有先验价值：若其实例分与最高分相差 ≤0.15 则优先采用它
    （防「真号已在盘 ⇒ 匹配器把正文错配到别的缺失号」，实测 R68 正文被误配到 R284）。
    """
    a = _toks(h1_title(body))
    if not a:
        return None, 0.0, 0.0
    scored = []
    for n, bt in cache.items():
        if not bt:
            continue
        scored.append((len(a & bt) / min(len(a), len(bt)), n))
    if not scored:
        return None, 0.0, 0.0
    scored.sort(reverse=True)
    top_sc, top_n = scored[0]
    for h in hyps:
        if h not in cache or h == top_n:
            continue
        sc_h = len(a & cache[h]) / min(len(a), len(cache[h])) if cache[h] else 0.0
        if sc_h >= top_sc - 0.15 and sc_h > 0:
            return h, round(sc_h, 3), round(top_sc, 3)
    return top_n, round(top_sc, 3), round(scored[1][0], 3)


def canon_name(n, name):
    """输出文件名：R{规范号}_<slug>.md（剥掉原文件名里的自编号段）"""
    m = re.match(r"^R\d+_R\d+[ _](.+)$", name)
    slug = m.group(1) if m else re.sub(r"^R\d+[_-]", "", name)
    return f"R{n:03d}_{slug.lstrip('_ ')}"


def classify(cands, targets, hist_titles):
    """按「正文标题 ↔ history 标题」对账定规范号。

    -> (ok: {n: cand}, bad: {n: reason}, low: [(cand, n, score)], present: {n: cand})
    low = 匹配低分（编号待人工确认）；present = 对账到的号已有在盘正文（不写盘）。
    """
    cache = _hist_tok_cache(hist_titles)
    good = []
    for c in cands:
        is_ok, _ = judge(c["body"])
        if not is_ok:
            continue
        n, sc, sc2 = best_hist_match(c["body"], hist_titles, cache, number_hypotheses(c["name"], c["body"]))
        c = dict(c, canon=n, score=sc, score2=sc2, h1=h1_title(c["body"]))
        good.append(c)
    ok, bad, low, present = {}, {}, [], {}
    for n in sorted(targets):
        cs = [c for c in good if c["canon"] == n]
        if not cs:
            byname = [c for c in cands if c["rnum"] == n]
            if not byname:
                bad[n] = "该 zip 内无写盘调用（正文可能不在本 zip / 非报告写盘方式）"
            else:
                reasons = sorted({c["reason"] for c in byname})
                bad[n] = "候选正文未过判据: " + ",".join(reasons[:3])
            continue
        best = max(cs, key=lambda c: (c["score"], len(c["body"])))
        if best["score"] < MATCH_MIN:
            low.append((best, n, best["score"]))
        else:
            ok[n] = best
    for c in good:
        if c["canon"] not in targets:
            old = present.get(c["canon"])
            if not old or c["score"] > old["score"]:
                present[c["canon"]] = c
    return ok, bad, low, present


# ---------------------------------------------------------------- 写盘
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


HIST = {}


def safe_name(name: str) -> str:
    return name.replace("/", "_").replace(chr(92), "_").strip()


def apply_recovery(ok, manifest_path: Path, out_dir: Path = MEM, low=None, present=None, log=print):
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, written, skipped = [], 0, 0
    for r, c in sorted(ok.items()):
        name = safe_name(canon_name(r, c["name"]))
        dst = out_dir / name
        body = c["body"]
        h = sha256(body)
        status = "written"
        if dst.exists():
            old = dst.read_text(encoding="utf-8", errors="replace")
            status = "skip-dup" if sha256(old) == h else "conflict-existing-differs"
        else:
            dst.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        if status == "written":
            written += 1
        else:
            skipped += 1
        ht = HIST.get(r, "")
        rows.append({"r": r, "file": name, "status": status, "bytes": len(body), "sha256": h,
                     "src": c["src"], "kind": c["kind"], "reason": c["reason"],
                     "h1": h1_title(body), "hist_title": ht, "title_score": round(title_score(body, ht), 3)})
    manifest_path.write_text(json.dumps(
        {"generated": date.today().isoformat(), "tool": "tools/report_body_recover.py",
         "min_len": MIN_LEN, "written": written, "skipped": skipped, "entries": rows,
         "low_score": [{"r": r, "file": canon_name(r, c["name"]), "score": sc, "h1": c["h1"],
                        "hist_title": HIST.get(r, ""), "src": c["src"]} for c, r, sc in (low or [])],
         "already_on_disk": [{"r": n, "file": c["name"], "score": c["score"], "h1": c["h1"],
                              "hist_title": HIST.get(n, ""), "src": c["src"]}
                             for n, c in sorted((present or {}).items())]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[apply] written={written} skipped={skipped} manifest={manifest_path}")
    for x in rows:
        log(f"  R{x['r']:<4} {x['status']:<26} {x['bytes']:>6}B  {x['file']}")
    return rows


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="自主报告正文补全（从 L4 会话 zip 静态还原）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("scan", "apply"):
        p = sub.add_parser(cmd)
        p.add_argument("--zip", required=True)
        p.add_argument("--json", default="")
        p.add_argument("--out", default=str(GA / "temp" / "recovered"))
        p.add_argument("--manifest", default=str(GA / "temp" / "RECOVERY_MANIFEST.json"))
    a = ap.parse_args()

    zp = Path(a.zip)
    if not zp.is_absolute():
        zp = (GA / zp) if (GA / zp).exists() else (ZIPDIR / zp.name)
    global HIST
    HIST = _hist_titles()
    disk, hist = disk_numbers(), history_numbers()
    missing = sorted(set(hist) - set(disk))
    print(f"[state] history={len(hist)} 在盘正文编号={len(disk)} 缺失={len(missing)} "
          f"(范围 R{min(missing) if missing else '-'}~R{max(missing) if missing else '-'})")

    cands, stats = scan_zip(zp)
    months = [n for n in (ZIPDIR.glob("*.zip"))]
    ok, bad, low, present = classify(cands, set(missing), HIST)
    print(f"[result] 可恢复={len(ok)} 低分待确认={len(low)} 不可恢复={len(bad)} "
          f"(对账到已有在盘正文={len(present)}，不写盘)")
    if a.json:
        Path(a.json).write_text(json.dumps(
            {"zip": zp.name, "missing_total": len(missing),
             "recoverable": {r: {"name": c["name"], "bytes": len(c["body"]), "src": c["src"]} for r, c in sorted(ok.items())},
             "low_score": [{"r": r, "file": canon_name(r, c["name"]), "score": sc, "h1": c["h1"],
                            "hist_title": HIST.get(r, ""), "src": c["src"]} for c, r, sc in low],
             "already_on_disk": [{"r": n, "file": c["name"], "score": c["score"], "h1": c["h1"],
                                  "hist_title": HIST.get(n, ""), "src": c["src"]}
                                 for n, c in sorted(present.items())],
             "unrecoverable": bad, "stats": stats}, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.cmd == "scan":
        for r, c in sorted(ok.items()):
            print(f"  ✓ R{r:<4} {len(c['body']):>6}B  对账{c['score']:.2f}/{c['score2']:.2f}  "
                  f"{canon_name(r, c['name'])}   [{c['src']}]")
        for c, r, sc in low:
            print(f"  ? R{r:<4} {len(c['body']):>6}B  对账低分{sc:.2f}  H1={c['h1']!r}  "
                  f"hist={HIST.get(r, '')[:40]!r}   [{c['src']}]")
        for r in sorted(bad):
            print(f"  ✗ R{r:<4} {bad[r]}")
        print(f"[zip 覆盖情况] L4 现有 {len(months)} 个月度 zip；本 zip 之外可能仍有可恢复正文 "
              f"(剩余 {sum(1 for x in ZIPDIR.glob('*.zip') if x != zp)} 个 zip 未扫)")
        return
    apply_recovery(ok, Path(a.manifest), Path(a.out))


if __name__ == "__main__":
    main()
