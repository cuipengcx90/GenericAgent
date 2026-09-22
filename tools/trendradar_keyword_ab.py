#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TrendRadar「拆词修复」效果 A/B 离线复算（R352 / TODO#13）。

只读复算：不改任何生产配置，不联网，不调用 MCP。数据全部来自本机：
  · 每日新闻库  ~/projects/TrendRadar/output/news/<date>.db  (news_items.title / platform_id)
  · 生产关键词  sche_tasks/run_trendradar_digest.py 的 KEYWORDS 字面量（ast 解析，不 import）
  · 当日供给    output/trendradar_{morning,evening}_<date>.material.json 的 rss.data 标题

三个臂（对同一批历史日期重放检索）：
  A  现状(baseline)：整个 query 作为子串出现在标题里（= TrendRadar keyword 模式，
     即 R343 确证的「132 期 total=0」根因）
  B  拆词(修法A)：query 按空白拆词，任一 token 命中即算命中；按标题去重
      · B_query  单 query 逐词并集（R343 impact 口径，可与之对齐）
      · B_union  当日两个 query 合并后再去重（真实落地后的每日增量）
  F  模糊(修法B)：复刻 TrendRadar search_mode='fuzzy' 的 _fuzzy_match（含/相似度/关键词重合）

附带量化：limit=30 截断风险、与当日 RSS 供给的重合度（净新增）、平台分布、提示词字符成本。

用法：
  python3 tools/trendradar_keyword_ab.py [--days 30] [--end 2026-09-21] [--start ...]
        [--thresholds 0.3,0.6] [--out temp/trendradar_keyword_ab] [--no-fuzzy] [--json]
退出码：0 正常；2 参数/数据缺失。
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

GA = Path(__file__).resolve().parent.parent
TR = Path(os.path.expanduser("~/projects/TrendRadar"))
DBDIR = TR / "output" / "news"
PROD = GA / "sche_tasks" / "run_trendradar_digest.py"
OUTD = GA / "output"


# ────────────────────────── 数据取用 ──────────────────────────

def prod_keywords() -> list[str]:
    """ast 解析生产脚本里的 KEYWORDS 字面量（不执行模块，避免副作用）。"""
    tree = ast.parse(PROD.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "KEYWORDS":
                    return [str(x.value) for x in node.value.elts]
    raise SystemExit("[FATAL] 未能在生产脚本里解析出 KEYWORDS")


def titles_of(day: str, dedup: bool = True) -> list[tuple[str, str]]:
    """返回 [(title, platform_name)]，只读打开当日 DB。无库/TCC 不可读时返回 []。

    dedup=True  → 同名标题跨平台只算一次（GA 侧拆词修复的落地口径）
    dedup=False → 同名标题按 (title, platform) 每条各算一次（=TrendRadar
                  `_search_titles` 的真实行为，也与 R343 impact 口径一致）
    """
    db = DBDIR / f"{day}.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(news_items)")}
        pid = "platform_id" if "platform_id" in cols else "platform"
        rows = con.execute(f"SELECT title, {pid} FROM news_items").fetchall()
        pmap: dict = {}
        try:
            for i, n in con.execute("SELECT id, name FROM platforms"):
                pmap[i] = n or str(i)
        except sqlite3.Error:
            pass
        out, seen = [], set()
        for t, p in rows:
            if not t:
                continue
            key = t.strip() if dedup else (t.strip(), p)
            if key in seen:          # 同题跨平台去重，保留首见
                continue
            seen.add(key)
            out.append((t.strip(), pmap.get(p, str(p))))
        return out
    finally:
        con.close()


def rss_titles_of(day: str) -> set[str]:
    """当日 material.json 里 rss.data 的标题集合（morning+evening 合并）。"""
    got: set[str] = set()
    for f in sorted(OUTD.glob(f"trendradar_*_{day}.material.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in ((d.get("rss") or {}).get("data") or []):
            t = (it or {}).get("title")
            if t:
                got.add(t.strip())
    return got


# ─────────────────────── 匹配语义（复刻生产） ───────────────────────

def tokens_of(query: str) -> list[str]:
    """拆词：按空白切分并去重保序（query 形如 'AI 画图 Stable Diffusion ...'）。"""
    out, seen = [], set()
    for w in query.split():
        w = w.strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


_ASCII_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#-]*$")


def token_hit(tok: str, title: str, boundary: bool = False) -> bool:
    """token 是否命中 title。

    boundary=True 时，纯 ASCII 词按词边界匹配（`\bAI\b`），避免 AIM-424 / AIGC 这类
    子串误召回；CJK 词仍用子串（中文无词边界）。
    """
    if not boundary or not _ASCII_TOKEN.match(tok):
        return tok.lower() in title.lower()
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])",
                     title, re.IGNORECASE) is not None


def match_whole(query: str, title: str) -> bool:
    """A 臂 = TrendRadar keyword 模式：query_lower in title_lower。"""
    return query.lower() in title.lower()


def _extract_keywords(text: str, min_length: int = 2) -> list[str]:
    """等价于 TrendRadar SearchTools._extract_keywords。"""
    text = re.sub(r"http[s]?://\S+", "", text)
    text = re.sub(r"\[.*?\]", "", text)
    return [w for w in re.findall(r"[\w]+", text) if w and len(w) >= min_length]


def match_fuzzy(query: str, title: str, threshold: float) -> bool:
    """等价于 TrendRadar SearchTools._fuzzy_match(query, title, threshold)。"""
    if query.lower() in title.lower():
        return True
    if SequenceMatcher(None, query.lower(), title.lower()).ratio() >= threshold:
        return True
    qw = set(_extract_keywords(query))
    tw = set(_extract_keywords(title))
    if not qw or not tw:
        return False
    return (len(qw & tw) / len(qw)) >= 0.5


# ────────────────────────── 主复算 ──────────────────────────

def run(days: int, end: str | None, start: str | None, thresholds: list[float],
        skip_fuzzy: bool, dedup: bool = True) -> dict:
    queries = prod_keywords()
    end_d = date.fromisoformat(end) if end else date.today() - timedelta(days=1)
    if start:
        start_d = date.fromisoformat(start)
    else:
        start_d = end_d - timedelta(days=days - 1)

    rows = []
    d = start_d
    while d <= end_d:
        day = d.isoformat()
        titles = titles_of(day, dedup=dedup)
        d += timedelta(days=1)
        if not titles:
            rows.append({"date": day, "titles": 0, "missing_db": True})
            continue

        r = {"date": day, "titles": len(titles)}
        # A 臂：整串
        r["A_query"] = [sum(1 for t, _ in titles if match_whole(q, t)) for q in queries]
        r["A_total"] = sum(r["A_query"])

        # B 臂：逐词
        key = (lambda t, p: t) if dedup else (lambda t, p: (t, p))
        bq, bunion, hit_tokens = [], set(), {}
        for q in queries:
            toks = tokens_of(q)
            hits = set()
            for tk in toks:
                tl = tk.lower()
                n = 0
                for t, p in titles:
                    if tl in t.lower():
                        hits.add(key(t, p))
                        n += 1
                if n:
                    hit_tokens[tk] = hit_tokens.get(tk, 0) + n
            bq.append(len(hits))
            bunion |= hits
        r["B_query"] = bq
        r["B_sum_queries"] = sum(bq)           # R343 impact 口径（跨 query 不去重）
        r["B_union"] = len(bunion)             # 真实落地后每日增量（去重）
        r["B_union_ge30"] = len(bunion) >= 30  # limit=30 触顶标记
        r["B_query_ge30"] = [n >= 30 for n in bq]
        r["B_top_tokens"] = dict(sorted(hit_tokens.items(), key=lambda kv: -kv[1])[:8])
        # 平台分布（B_union）
        plat: dict[str, int] = {}
        for t, p in titles:
            if key(t, p) in bunion:
                plat[p] = plat.get(p, 0) + 1
        r["B_platforms"] = dict(sorted(plat.items(), key=lambda kv: -kv[1]))

        # B_bd：ASCII 词加词边界的逐词并集（精度对照臂）
        bd = set()
        for q in queries:
            for tk in tokens_of(q):
                for t in [t for t, _ in titles]:
                    if token_hit(tk, t, boundary=True):
                        bd.add(t)
        r["B_bd_union"] = len(bd)
        r["B_bd_lost"] = len(bunion) - len(bd)

        # 与当日 RSS 供给的重合
        rss = rss_titles_of(day)
        r["rss_titles"] = len(rss)
        btitles = {k if isinstance(k, str) else k[0] for k in bunion}
        r["B_union_in_rss"] = len(btitles & rss) if rss else None
        r["B_union_net_new"] = (len(btitles - rss) if rss else None)
        r["B_union_chars"] = sum(len(t) for t in btitles)

        # F 臂：模糊
        if not skip_fuzzy:
            for th in thresholds:
                fq = [sum(1 for t, _ in titles if match_fuzzy(q, t, th)) for q in queries]
                r[f"F{th}_query"] = fq
                r[f"F{th}_total"] = sum(fq)
        rows.append(r)

    return {"queries": queries, "range": [start_d.isoformat(), end_d.isoformat()],
            "days": len(rows), "thresholds": thresholds, "title_dedup": dedup, "rows": rows}


def summarize(res: dict) -> dict:
    rows = [r for r in res["rows"] if not r.get("missing_db")]
    if not rows:
        return {"error": "no data"}
    s = {
        "days": len(rows),
        "A_total": sum(r["A_total"] for r in rows),
        "B_sum_queries": sum(r["B_sum_queries"] for r in rows),
        "B_union": sum(r["B_union"] for r in rows),
        "B_union_max_day": max(r["B_union"] for r in rows),
        "B_union_zero_days": sum(1 for r in rows if r["B_union"] == 0),
        "limit30_hit_days": sum(1 for r in rows if r["B_union_ge30"]),
        "titles_total": sum(r["titles"] for r in rows),
    }
    for th in res["thresholds"]:
        k = f"F{th}_total"
        if any(k in r for r in rows):
            s[k] = sum(r[k] for r in rows)
    s["limit_days_query_ge30"] = sum(1 for r in rows for n in r["B_query"] if n >= 30)
    s["limit_lost_if_30"] = sum(max(0, n - 30) for r in rows for n in r["B_query"])
    s["B_bd_union"] = sum(r["B_bd_union"] for r in rows)
    s["B_bd_lost"] = sum(r["B_bd_lost"] for r in rows)
    s["title_chars_avg"] = round(sum(r["B_union_chars"] for r in rows) / max(1, sum(r["B_union"] for r in rows)), 1)
    rss_known = [r for r in rows if r.get("B_union_in_rss") is not None]
    if rss_known:
        s["rss_days_matched"] = len(rss_known)
        s["B_union_in_rss"] = sum(r["B_union_in_rss"] for r in rss_known)
        s["B_union_net_new"] = sum(r["B_union_net_new"] for r in rss_known)
        s["B_union_chars"] = sum(r["B_union_chars"] for r in rss_known)
    plat: dict[str, int] = {}
    for r in rows:
        for p, n in (r.get("B_platforms") or {}).items():
            plat[p] = plat.get(p, 0) + n
    s["B_platforms"] = dict(sorted(plat.items(), key=lambda kv: -kv[1]))
    tok: dict[str, int] = {}
    for r in rows:
        for t, n in (r.get("B_top_tokens") or {}).items():
            tok[t] = tok.get(t, 0) + n
    s["B_top_tokens"] = dict(sorted(tok.items(), key=lambda kv: -kv[1])[:15])
    return s


def write_outputs(res: dict, outd: Path) -> None:
    outd.mkdir(parents=True, exist_ok=True)
    (outd / "ab_replay.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    cols = ["date", "titles", "A_total", "B_sum_queries", "B_union", "B_bd_union", "B_union_ge30",
            "B_union_in_rss", "B_union_net_new", "B_union_chars"]
    for th in res["thresholds"]:
        cols.append(f"F{th}_total")
    with (outd / "ab_replay.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in res["rows"]:
            if r.get("missing_db"):
                w.writerow([r["date"], 0] + [""] * (len(cols) - 2))
                continue
            w.writerow([r.get(c, "") for c in cols])

    lines = ["| 日期 | 库内标题 | A 整串 | B 逐词(2 query 合计) | B 逐词(去重) | 命中≥30 | 与RSS重合 | 净新增 | 字符 |"]
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in res["rows"]:
        if r.get("missing_db"):
            lines.append(f"| {r['date']} | (无库) | | | | | | | |")
            continue
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["date"], r["titles"], r["A_total"], r["B_sum_queries"], r["B_union"],
            "Y" if r["B_union_ge30"] else "", r.get("B_union_in_rss", "n/a"),
            r.get("B_union_net_new", "n/a"), r.get("B_union_chars", "")))
    (outd / "ab_replay.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", default=None, help="YYYY-MM-DD（默认昨天）")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD（覆盖 --days）")
    ap.add_argument("--thresholds", default="0.3,0.6")
    ap.add_argument("--out", default=str(GA / "temp" / "trendradar_keyword_ab"))
    ap.add_argument("--no-fuzzy", action="store_true")
    ap.add_argument("--pair-mode", action="store_true",
                    help="同名标题跨平台各算一次（=TrendRadar _search_titles/R343 impact 口径）")
    ap.add_argument("--json", action="store_true", help="只在 stdout 打印汇总 JSON")
    a = ap.parse_args()

    if not DBDIR.exists():
        print(f"[FATAL] 新闻库目录不存在: {DBDIR}")
        return 2
    ths = [float(x) for x in a.thresholds.split(",") if x.strip()]
    res = run(a.days, a.end, a.start, ths, a.no_fuzzy, dedup=not a.pair_mode)
    s = summarize(res)
    write_outputs(res, Path(a.out))

    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        print(f"口径: 关键词={res['queries']}")
        print(f"区间: {res['range'][0]} ~ {res['range'][1]}  ({s['days']} 天有数据)")
        print(f"A 整串命中合计        : {s['A_total']}")
        print(f"B 逐词(query 合计)   : {s['B_sum_queries']}   ← R343 impact 口径")
        print(f"B 逐词(跨 query 去重): {s['B_union']}   单日最高 {s['B_union_max_day']}"
              f"   零命中天 {s['B_union_zero_days']}   触顶(≥30)天 {s['limit30_hit_days']}")
        for th in ths:
            if f"F{th}_total" in s:
                print(f"F 模糊(th={th}) 合计  : {s[f'F{th}_total']}")
        if "B_union_in_rss" in s:
            print(f"与当日 RSS 重合       : {s['B_union_in_rss']} / 净新增 {s['B_union_net_new']}"
                  f"  (覆盖 {s['rss_days_matched']} 天)")
        print(f"B 词边界(ASCII)去重  : {s['B_bd_union']}   比子串少 {s['B_bd_lost']} 条(子串误召回)")
        print(f"limit=30 截断风险     : 触顶 {s['limit_days_query_ge30']} 次(query·天)"
              f"，按现状 limit=30 会丢 {s['limit_lost_if_30']} 条")
        print(f"命中标题平均字数      : {s['title_chars_avg']}")
        print(f"平台分布              : {s['B_platforms']}")
        print(f"B 高频词              : {s['B_top_tokens']}")
        print(f"产物: {a.out}/ab_replay.{{json,csv,md}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
