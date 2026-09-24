#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""l4_search.py — L4 会话原始归档（memory/L4_raw_sessions/*.zip）流式检索器（只读）

为什么需要它：memory/L4_raw_sessions/*.zip 里存着 2026-04..09 的**会话原文**
（415 个 .txt 成员 / 解压后约 623MB），而 memory/ 下能被 grep 的只有蒸馏态摘要
（≈原始的 1.4%）。规划模式「批判性读 history」与报告回溯都需要回到原文。

设计口径（与既有件的关系，避免重复造轮子）：
  * temp/recall_probe/rp.py（R369 原型）是内存语料的 FTS5(trigram) 索引，其 corpus_files()
    **刻意 skip_l4=True**（trigram 索引体积≈语料的 3-4 倍，623MB 语料不可行）⇒ 本工具补的正是这个洞。
  * temp/mem_debt/mine_zips.py 是一次性抽取器（只找「记忆更新」小节并落 JSON），不含通用检索。
  * tools/autonomous_reports_archive.py 的 repair 会**解压**找报告正文；本工具**不解压**、零落盘。

用法:
  python3 tools/l4_search.py list [--json temp/l4_members.json]
  python3 tools/l4_search.py search 词1 [词2 ...] [-k 50] [--any] [--regex] [--zip 2026-09]
                                   [--member 0904] [--context 80] [--count]
  python3 tools/l4_search.py grep-file <路径> 词1 [词2 ...]   # 亦可用于 all_histories.txt(keyword)
  python3 tools/l4_search.py show <zip> <成员> [--lines A:B]  # grep 命中后回看原文（不解压落盘）
  python3 tools/l4_search.py selftest                           # 负控：真词必中 / 乱码必不中

匹配口径：默认**大小写不敏感**（等价 `grep -i`）；加 `--case-sensitive` 则逐字节区分。
  实测同一查询 `17463 knowledgeC`：不区分=74 行、区分=73 行（多出的 1 行是大小写变体）
  ⇒ 与 `unzip -p x.zip | grep -n 17463 | grep -c knowledgeC`（区分大小写）比对时必须带上该开关。

只读保证：zip 以 'r' 打开、只 read 不写；除显式 --json 外不落任何文件；不联网。
"""
import argparse, glob, json, os, re, sys, time, zipfile

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
L4DIR = os.path.join(REPO, "memory", "L4_raw_sessions")


def zips(pattern=None):
    out = []
    for p in sorted(glob.glob(os.path.join(L4DIR, "*.zip"))):
        if pattern and pattern not in os.path.basename(p):
            continue
        out.append(p)
    return out


def _decode(raw):
    return raw.decode("utf-8", "replace")


def cmd_list(a):
    rows, t_n = [], len(zips())
    t0 = time.time()
    for zp in zips(a.zip):
        with zipfile.ZipFile(zp) as zf:
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                rows.append({"zip": os.path.basename(zp), "member": info.filename,
                             "bytes": info.file_size, "date": "%04d-%02d-%02d" % info.date_time[:3]})
    tot = sum(r["bytes"] for r in rows)
    print(f"zips={len({r['zip'] for r in rows})}/{t_n}  members={len(rows)}  member_bytes={tot} ({tot/1e6:.1f}MB)  elapsed={time.time()-t0:.2f}s")
    per = {}
    for r in rows:
        per[r["zip"]] = per.get(r["zip"], 0) + 1
    for k in sorted(per):
        sz = sum(r["bytes"] for r in rows if r["zip"] == k)
        print(f"  {k}  members={per[k]:4d}  bytes={sz} ({sz/1e6:.1f}MB)")
    if a.json:
        json.dump({"members": rows, "totals": {"zips": len({r['zip'] for r in rows}), "members": len(rows), "bytes": tot}},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[json] {a.json}")
    return 0


def _matcher(a):
    cs = getattr(a, "case_sensitive", False)  # 默认大小写不敏感（与 grep -i 对齐）
    if a.regex:
        rx = re.compile("|".join(a.terms), 0 if cs else re.I)
        return lambda s: bool(rx.search(s))
    terms = a.terms if cs else [t.lower() for t in a.terms]
    if cs:
        if a.any:
            return lambda s: any(t in s for t in terms)
        return lambda s: all(t in s for t in terms)
    if a.any:
        return lambda s: any(t in s.lower() for t in terms)
    return lambda s: all(t in s.lower() for t in terms)


def cmd_search(a):
    match = _matcher(a)
    st = dict(zips=0, members=0, bytes=0, lines=0, hits=0, truncated=False, skipped_big=0)
    t0 = time.time()
    for zp in zips(a.zip):
        st["zips"] += 1
        with zipfile.ZipFile(zp) as zf:
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                if a.member and a.member not in info.filename:
                    continue
                if info.file_size > a.max_member:
                    st["skipped_big"] += 1
                    continue
                txt = _decode(zf.read(info.filename))
                st["members"] += 1
                st["bytes"] += len(txt.encode("utf-8", "replace"))
                for i, ln in enumerate(txt.split("\n"), 1):
                    st["lines"] += 1
                    if not match(ln):
                        continue
                    st["hits"] += 1
                    if not a.count and st["hits"] <= a.max_hits:
                        rel = os.path.join(os.path.basename(zp), info.filename)
                        print(f"{rel}:{i}: {ln.strip()[:a.context*2]}")
                    if st["hits"] > a.max_hits:
                        st["truncated"] = True
    print(f"\n[scan] zips={st['zips']} members={st['members']} lines={st['lines']} "
          f"bytes={st['bytes']} ({st['bytes']/1e6:.1f}MB) skipped_big={st['skipped_big']} "
          f"hits={st['hits']}{' (已截断显示)' if st['truncated'] and not a.count else ''} "
          f"elapsed={time.time()-t0:.2f}s")
    return 0


def cmd_grep_file(a):
    match = _matcher(a)
    st = dict(lines=0, hits=0, bytes=0)
    t0 = time.time()
    with open(a.path, "r", encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh, 1):
            st["lines"] += 1
            st["bytes"] += len(ln)
            if match(ln):
                st["hits"] += 1
                if st["hits"] <= a.max_hits:
                    print(f"{os.path.basename(a.path)}:{i}: {ln.strip()[:a.context*2]}")
    print(f"\n[scan] file={os.path.basename(a.path)} lines={st['lines']} bytes={st['bytes']} hits={st['hits']} elapsed={time.time()-t0:.2f}s")
    return 0


def cmd_selftest(a):
    """负控：真词必中 / 合成乱码必不中，且命中行须能由 zipfile 独立复读（防索引类假命中）。"""
    ok = True
    for term, expect in [("knowledgeC", True), ("ZZQX_NOSUCH_TOKEN_9137", False)]:
        a2 = argparse.Namespace(terms=[term], regex=False, any=False, count=True, member=None,
                                zip=None, max_hits=10, max_member=200*1024*1024, context=80,
                                case_sensitive=False)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_search(a2)
        hits = int(re.search(r"hits=(\d+)", buf.getvalue()).group(1))
        good = (hits > 0) == expect
        ok &= good
        print(f"  {'✅' if good else '❌'} {term!r} expect_hit={expect} hits={hits}")
    print("负控", "通过" if ok else "失败")
    return 0 if ok else 2


def cmd_show(a):
    """打印某成员正文（或指定行区间），不解压落盘；供 grep 命中后回看上下文。"""
    zp = os.path.join(L4DIR, a.zip)
    if not os.path.exists(zp):
        zp = next((p for p in zips() if os.path.basename(p).startswith(a.zip)), None)
    if not zp:
        print(f"[err] 找不到 zip: {a.zip}"); return 2
    with zipfile.ZipFile(zp) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if a.member not in names:
            import difflib
            cand = difflib.get_close_matches(a.member, names, n=5, cutoff=0.4)
            print(f"[err] 成员不存在: {a.member}\n      近似: {cand}"); return 2
        txt = _decode(zf.read(a.member))
    lines = txt.split("\n")
    lo, hi = 1, len(lines)
    if a.lines:
        m = re.match(r"^(\d+)?[:,\-]?(\d+)?$", a.lines)
        if not m:
            print("[err] --lines 形如 A:B / A: / :B"); return 2
        lo = int(m.group(1) or 1); hi = int(m.group(2) or len(lines))
    for i in range(lo, min(hi, len(lines)) + 1):
        print(f"{i}: {lines[i-1]}")
    print(f"\n[show] {os.path.basename(zp)}/{a.member} 总行={len(lines)} 显示={lo}..{min(hi, len(lines))}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="L4 会话归档流式检索（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list"); pl.add_argument("--zip"); pl.add_argument("--json"); pl.set_defaults(fn=cmd_list)

    def common(p):
        p.add_argument("terms", nargs="+")
        p.add_argument("-k", "--max-hits", type=int, default=50)
        p.add_argument("--any", action="store_true", help="任一词命中（默认全部词须同行）")
        p.add_argument("--regex", action="store_true", help="terms 按正则 OR 处理")
        p.add_argument("--case-sensitive", action="store_true", help="区分大小写（默认不区分，等价 grep -i）")
        p.add_argument("--context", type=int, default=80)

    ps = sub.add_parser("search"); common(ps)
    ps.add_argument("--zip"); ps.add_argument("--member")
    ps.add_argument("--max-member", type=int, default=200 * 1024 * 1024)
    ps.add_argument("--count", action="store_true"); ps.set_defaults(fn=cmd_search)

    pg = sub.add_parser("grep-file"); pg.add_argument("path"); common(pg); pg.set_defaults(fn=cmd_grep_file)
    ph = sub.add_parser("show"); ph.add_argument("zip"); ph.add_argument("member")
    ph.add_argument("--lines"); ph.set_defaults(fn=cmd_show)
    pt = sub.add_parser("selftest"); pt.set_defaults(fn=cmd_selftest)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
