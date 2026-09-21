#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autonomous_reports_archive.py —— 自主报告(R1..R334)灾备抢救 / 入库同步 / 逐字验证

【事故背景】temp/ 在 .gitignore 内 => temp/autonomous_reports/(自主报告唯一副本)一直游离于版本控制，
随 temp 清理而整体灭失；reports 目录无任何 git 历史。

【灾备源】memory/L4_raw_sessions/YYYY-MM.zip —— GA 每 12h 的 L4 会话归档(reflect/scheduler.py)，
内含历史会话全文，其中夹带报告正文。两种历史格式：
  * era A (2026-04/05, repr 风格)：正文写在 assistant 文本的 <file_content>...</file_content> 标签内，
    tool_use.file_write 的 input 只有 {'mode','path'}(正文被剥离) => 必须按“文本标签 + 同段路径”配对。
  * era B (2026-06 起, JSON 风格)：正文在 tool_use.input.content / new_content 内。
另有第三类**只含标题不含正文**的线索：日志里 `ls autonomous_reports` 的回显 ==> 用于重建清单(inventory)。

子命令
  repair    [--apply] [--zips ...]  抽取正文 -> ARCHIVE 目录(逐字，不加任何装饰)
  inventory [--apply]               合成 INDEX.md(正文已恢复 / 仅标题)
  verify    [--n N] [--ids ...]     独立复抽 + 逐字比对(等价性证明)
  sync      [--apply]               temp/autonomous_reports/ -> ARCHIVE 增量同步(未来的落盘即入库)
"""
import argparse, ast, json, re, sys, zipfile
from datetime import date as _date, datetime as _dt
from pathlib import Path

GA = Path(__file__).resolve().parent.parent
ZIPDIR = GA / "memory" / "L4_raw_sessions"
HIST = LIVE_HIST = GA / "temp" / "autonomous_reports" / "history.txt"   # 真实索引(在 temp 内，同样需要抢救)
HIST_ARCHIVE = GA / "memory" / "autonomous_reports" / "history.txt"
LIVE = GA / "temp" / "autonomous_reports"
ARCHIVE = GA / "memory" / "autonomous_reports"

FC_RE = re.compile(r"<file_content>(.*?)</file_content>", re.S)
R_PATH = re.compile(r"R(\d{1,3})(?:-\d+)?_")
R_NAME = re.compile(r"R(\d{1,3})[_-]([^\s/\\'\"]*?\.md)")
SIGS = ("## 记忆更新建议", "## 执行时间", "## 1.", "# R")


def _split_sections(text):
    parts = re.split(r"\n=== (Prompt|Response) === ", text)
    for i in range(1, len(parts) - 1, 2):
        yield parts[i], parts[i + 1]


def _balanced(s, i):
    """从 s[i] 处取第一个平衡的 [ ... ] 字面量"""
    depth, quote, esc = 0, None, False
    for j in range(i, len(s)):
        c = s[j]
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return s[i:j + 1]
    return None


def _items(block):
    """尽力解析一段响应正文为 items 列表(char 级平衡 + literal_eval)"""
    j = block.find("[")
    if j < 0:
        return None
    lit = _balanced(block, j)
    if not lit:
        return None
    try:
        v = ast.literal_eval(lit)
    except Exception:
        return None
    return v if isinstance(v, list) else None


def _path_writes(items):
    """严格只取 file_write/file_patch(排除 file_read，否则 ls/read 会污染配对)"""
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "tool_use" \
                and str(it.get("name")) in ("file_write", "file_patch"):
            inp = it.get("input") or {}
            out.append((str(inp.get("path") or ""),
                        str(inp.get("content") or inp.get("new_content") or "")))
    return out


def harvest(zips=None):
    """-> (bodies: {rnum: (text, relpath, src, ts)}, seen: {rnum: set(names)})"""
    bodies, seen = {}, {}
    for zp in sorted(zips or ZIPDIR.glob("*.zip")):
        zf = zipfile.ZipFile(zp)
        for name in zf.namelist():
            if not name.endswith(".txt"):
                continue
            t = zf.read(name).decode("utf-8", errors="replace")
            if "<file_content>" not in t and "'tool_use'" not in t and '"tool_use"' not in t:
                continue
            for kind, blk in _split_sections(t):
                if kind != "Response":
                    continue
                items = _items(blk)
                tags = [x for it in (items or []) if isinstance(it, dict) and it.get("type") == "text"
                        for x in FC_RE.findall(it.get("text") or "")]
                if not tags:
                    tags = FC_RE.findall(blk)
                writes = _path_writes(items) if items else []
                # era B: 正文直接在 input 内
                cands = []
                for p, body in writes:
                    if body:
                        cands.append((p, body))
                # era A: 标签与同段唯一写路径配对(同段多写时按顺序对位)
                if not cands and tags:
                    if len(writes) == 1:
                        cands = [(writes[0][0], tags[-1])]
                    elif writes and len(writes) == len(tags):
                        cands = list(zip([p for p, _ in writes], tags))
                for p, body in cands:
                    if len(body) < 200:
                        continue
                    m = R_PATH.search(p)
                    if not m:
                        # 路径不含 R 号时，仅接受“正文首行标题即 R 号”的自证报告
                        m = re.match(r"\s*#\s*R(\d{1,3})\b", body)
                    if not m:
                        continue
                    r = int(m.group(1))
                    if r not in bodies or len(body) > len(bodies[r][0]):
                        bodies[r] = (body, p, f"{zp.name}:{name}", "")
                # 清单线索：ls 回显 / 报告名提及
                for mm in R_NAME.finditer(blk):
                    seen.setdefault(int(mm.group(1)), set()).add(mm.group(2))
    return bodies, seen


def history_lines():
    """history.txt 的 R 条目（编号 -> 单行文本）"""
    out = {}
    if not HIST.exists():
        return out
    txt = HIST.read_text(encoding="utf-8", errors="replace")
    for line in txt.splitlines():
        m = re.match(r"\s*R(\d{1,3})[^\w]", line) or re.match(r"\s*\|?\s*R(\d{1,3})\b", line)
        if m:
            out.setdefault(int(m.group(1)), line.strip())
    return out


def _safe_title(body, rnum, fallback):
    m = re.match(r"#\s*R?\d*\s*[:_\-]?\s*(.+)", body.strip())
    title = (m.group(1).strip() if m else "") or fallback
    title = re.sub(r"[\s/\\:*?\"<>|]+", "_", title)[:60].strip("_")
    return title or f"R{rnum}"


def _outname(r, p, body):
    """归档文件名：R###_<原始名(单一扩展名)>；路径无线索时退回正文标题"""
    ext = ".md"
    base = ""
    if p and R_PATH.search(p):
        b = Path(p).name
        ext = Path(b).suffix if Path(b).suffix else ".md"
        base = Path(b).stem
        base = re.sub(r"^R\d{1,3}[_-]", "", base)
    if not base:
        base = _safe_title(body, r, f"R{r}")
    base = re.sub(r"[\s/\\:*?\"<>|]+", "_", base).strip("_")[:80] or f"R{r}"
    if base.endswith(ext):
        base = base[: -len(ext)]
    return f"R{r:03d}_{base}{ext}"


def cmd_repair(a):
    bodies, _ = harvest([Path(z) for z in a.zips] if a.zips else None)
    print(f"[repair] 抽取到正文的报告数 = {len(bodies)}")
    for r in sorted(bodies):
        body, p, src, _ = bodies[r]
        print(f"   R{r:<3} {len(body):>6}B  {p:<50} {src}")
    if not a.apply:
        print("[repair] dry-run（加 --apply 落盘）")
        return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in sorted(bodies):
        body, p, src, _ = bodies[r]
        out = ARCHIVE / _outname(r, p, body)
        out.write_text(body, encoding="utf-8")
        n += 1
    print(f"[repair] 已写入 {n} 个文件 -> {ARCHIVE}")
    return 0


def _rename_map():
    """R号 -> 已落盘文件（用于重命名/清单）"""
    out = {}
    for f in ARCHIVE.glob("R*.md"):
        m = re.match(r"R(\d{1,3})[_-]", f.name)
        if m:
            out[int(m.group(1))] = f
    return out


def cmd_inventory(a):
    bodies, seen = harvest()
    hist = history_lines()
    live_hist = {}
    if LIVE_HIST.exists():
        for line in LIVE_HIST.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*R(\d{1,3})\b", line)
            if m:
                live_hist[int(m.group(1))] = line.strip()
    disk = _rename_map()
    rows = []
    for r in sorted(set(bodies) | set(seen) | set(hist) | set(live_hist) | set(disk)):
        title = ""
        line = live_hist.get(r) or hist.get(r) or ""
        if line:
            parts = [p.strip() for p in line.split("|")]
            title = parts[3] if len(parts) > 3 else (parts[1] if len(parts) > 1 else "")
        if r in disk:
            st, extra = "✅正文", f"{disk[r].stat().st_size}B {disk[r].name}"
        elif r in bodies:
            st, extra = "✅正文(未落盘)", f"{len(bodies[r][0])}B {bodies[r][1]}"
        else:
            st, extra = "⚠️仅索引", (sorted(seen[r])[0][:60] if r in seen else "见 history/日志")
        rows.append((r, st, title or "-", extra, (parts[-1][:90] if line else "")))
    lines = ["# 自主报告归档清单（抢救重建）", "",
             f"- 生成: {__import__('datetime').date.today()}",
             f"- 正文已恢复: {sum(1 for x in rows if x[1].startswith('✅'))} / 索引可见 {len(rows)} 个编号",
             "- 索引来源: temp/autonomous_reports/history.txt(已随本目录一同抢救)",
             "- 正文来源: memory/L4_raw_sessions/*.zip (GA L4 会话归档)；抽取= tools/autonomous_reports_archive.py",
             "- 说明: 报告正文仅在会话日志中被引用过才可能恢复；⚠️仅索引 = 编号/主题可考、正文已随 temp 灭失", "",
             "| R | 状态 | 主题 | 证据 | 结论摘要 |", "|---|---|---|---|---|"]
    for r, st, title, extra, concl in rows:
        lines.append(f"| R{r} | {st} | {title} | {extra} | {concl} |")
    txt = "\n".join(lines) + "\n"
    if a.apply:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        (ARCHIVE / "INDEX.md").write_text(txt, encoding="utf-8")
        if LIVE_HIST.exists():
            raw = LIVE_HIST.read_text(encoding="utf-8", errors="replace")
            (ARCHIVE / "history.txt").write_text(raw, encoding="utf-8")
            (ARCHIVE / "history.md").write_text(
                "# 自主报告 history 索引（抢救自 temp/autonomous_reports/history.txt）\n\n"
                f"- 采集: {_date.today().isoformat()}；行数 {len(raw.splitlines())}\n"
                "- 格式: `R### | 日期 | 类型 | 主题 | 结论`\n\n```text\n" + raw.rstrip() + "\n```\n",
                encoding="utf-8")
        # 重命名：R###_<basename> 优先级高于正文标题
        renamed = 0
        for r, f in disk.items():
            if r not in bodies:
                continue
            p = bodies[r][1]
            if not p or not R_PATH.search(p):
                continue
            want = ARCHIVE / _outname(r, p, bodies[r][0])
            if want != f and not want.exists():
                f.rename(want)
                renamed += 1
        print(f"[inventory] INDEX.md({len(rows)} 行) + history.txt 已归档；重命名 {renamed} 项 -> {ARCHIVE}")
    else:
        print(txt[:1500])
        print(f"[inventory] dry-run（加 --apply 落盘）")
    return 0


def cmd_verify(a):
    """独立复抽（重新解压重解析）并与归档文件逐字比对"""
    bodies, _ = harvest()
    ids = [int(x) for x in a.ids.split(",") if x.strip()] or sorted(bodies)[:a.n]
    ok = bad = miss = 0
    for r in ids:
        cand = sorted(ARCHIVE.glob(f"R{r:03d}_*.md"))
        if not cand:
            print(f"  R{r}: 归档缺失"); miss += 1; continue
        disk = cand[0].read_text(encoding="utf-8")
        src = bodies.get(r, (None,))[0]
        same = (src is not None and disk == src)
        print(f"  R{r}: {'逐字一致' if same else '不一致'}  {len(disk)}B vs "
              f"{len(src) if src else 0}B  {cand[0].name}")
        ok += same; bad += (not same)
    print(f"[verify] 一致 {ok} / 不一致 {bad} / 缺失 {miss}")
    return 0 if bad == 0 and miss == 0 else 1


def cmd_sync(a):
    if not LIVE.exists():
        print(f"[sync] 无源目录 {LIVE}"); return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(LIVE.glob("*.md")):
        m = re.match(r"R(\d{1,3})[_-]", f.name)
        if not m:
            continue
        dst = ARCHIVE / f"R{int(m.group(1)):03d}_{re.sub(r'^R\d{1,3}[_-]', '', f.name)}"
        if dst.exists() and dst.read_bytes() == f.read_bytes():
            continue
        print(f"  {'更新' if dst.exists() else '新增'} {dst.name}")
        if a.apply:
            dst.write_bytes(f.read_bytes())
        n += 1
    print(f"[sync] {n} 项待同步（{'已落盘' if a.apply else 'dry-run'}）")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("repair"); p.add_argument("--apply", action="store_true")
    p.add_argument("--zips", nargs="*", default=[]); p.set_defaults(fn=cmd_repair)
    p = sub.add_parser("inventory"); p.add_argument("--apply", action="store_true"); p.set_defaults(fn=cmd_inventory)
    p = sub.add_parser("verify"); p.add_argument("--n", type=int, default=5)
    p.add_argument("--ids", default=""); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("sync"); p.add_argument("--apply", action="store_true"); p.set_defaults(fn=cmd_sync)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
