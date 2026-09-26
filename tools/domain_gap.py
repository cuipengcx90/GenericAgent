#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""domain_gap.py — 「高频访问但零提及」域名对账（只读，纯 stdlib）

回答的问题（TODO 第 16 批 #12）：
    本机 Chrome 历史上被高频访问的域名里，哪些 **从未在本仓报告/记忆中被提到过**？
    并给出 K 敏感性、时段偏好与「为什么对这台机器有用」的本机证据。

数据侧（全部只读，只碰副本，不碰 ~/Library/Application Support/Google/Chrome）：
    --copies 接受目录（递归找 History）或单文件；默认用 R415/R430 的两处冻结副本
        temp/chrome_snapshots/2026-09-25/History            (Default)
        temp/chrome_profiles/2026-09-26/<Profile N>/History (9 个非 Default)

语料侧（mention 判定）：
    --corpus 默认 memory/ 与 temp/autonomous_reports/；只统计 .md/.txt 文本文件。
    ★语义是「本仓是否提到过」（下界口径）：只做域名子串判定，不做词形还原。

用法：
    python3 tools/domain_gap.py scan [--k 20,50] [--out temp/domain_gap/<date>.json] [--top 40]
    python3 tools/domain_gap.py --selftest

判据铁律（踩过的坑，已固化成自测）：
    ①语料为空 ⇒ 必须 rc=2 报 corpus_empty，**绝不**把全部域名判成「零提及」
      （历史上多次「校验器没读到语料却输出全 MISS」的假绿）。
    ②域名归属：www.foo.com 也算 foo.com 被提及（base 域回退），避免假 gap。
    ③自造域（localhost/127.0.0.1/*.local/扩展 id 等）单列，不入 gap 结论。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

CST = timezone(timedelta(hours=8))
CHROME_EPOCH_OFFSET = 11644473600  # 1601-01-01 → 1970-01-01 秒
DEFAULT_COPIES = [
    "temp/chrome_snapshots/2026-09-25/History",
    "temp/chrome_profiles/2026-09-26",
]
DEFAULT_CORPUS = ["memory", "temp/autonomous_reports"]
TEXT_EXT = {".md", ".txt"}
SELF_HOST_RE = re.compile(
    r"^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0|::1|.*\.local|.*\.localhost)$"
)
EXT_ID_RE = re.compile(r"^[a-p]{32}$")  # Chrome 扩展 id（chrome-extension://<id>/…）

# 时段（本地时区 Asia/Shanghai）——口径写死，报告里必须原样引用
SLOTS = [("night", 0, 6), ("morning", 6, 12), ("afternoon", 12, 18), ("evening", 18, 24)]


# ---------------------------------------------------------------- 基础工具
def chrome_time_to_epoch(t: int) -> float:
    return t / 1_000_000.0 - CHROME_EPOCH_OFFSET


def domain_of(url: str):
    """返回 (host, kind)：kind ∈ {web, self_constructed, extension, non_web}"""
    try:
        sp = urlsplit(url)
    except Exception:
        return None, "non_web"
    scheme = (sp.scheme or "").lower()
    if scheme not in ("http", "https"):
        if scheme == "chrome-extension":
            return (sp.netloc or "").lower() or None, "extension"
        return None, "non_web"
    host = (sp.hostname or "").lower().strip(".")
    if not host:
        return None, "non_web"
    if SELF_HOST_RE.match(host):
        return host, "self_constructed"
    if EXT_ID_RE.match(host):
        return host, "extension"
    return host, "web"


def base_domain(host: str) -> str:
    """粗口径 registrable 近似：最后两段；对 .co.uk/.com.cn 等做有限修正。"""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two = ".".join(parts[-2:])
    if two in ("co.uk", "com.cn", "co.jp", "com.au", "org.uk", "net.cn", "gov.cn"):
        return ".".join(parts[-3:])
    return two


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_history_files(paths):
    out = []
    for p in paths:
        path = Path(p)
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            for root, _dirs, files in os.walk(path):
                for f in files:
                    if f == "History":  # Chrome History 库无扩展名
                        out.append(Path(root) / f)
    return sorted(set(out))


def collect_corpus(paths):
    """返回 (files, total_bytes, blobs:list[(path, text_lower)])；文本类扩展名白名单。"""
    files, blobs, total = [], [], 0
    for p in paths:
        path = Path(p)
        if path.is_file():
            cand = [path]
        elif path.is_dir():
            cand = [Path(r) / f for r, _d, fs in os.walk(path) for f in fs]
        else:
            cand = []
        for f in cand:
            if f.suffix.lower() not in TEXT_EXT:
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            files.append(str(f))
            total += len(txt.encode("utf-8"))
            blobs.append((str(f), txt.lower()))
    return files, total, blobs


def _verdict(set_lo, set_hi) -> str:
    """K 敏感性判据：嵌套单调=结构性稳定；交集过半=stable；否则 k_sensitive。"""
    if not set_hi:
        return "threshold_above_all_gaps"
    if set_hi <= set_lo:
        return "nested_monotone"
    if set_lo and len(set_lo & set_hi) / len(set_lo) >= 0.5:
        return "stable"
    return "k_sensitive"


def slot_of(ts_epoch: float) -> str:
    hour = datetime.fromtimestamp(ts_epoch, CST).hour
    for name, lo, hi in SLOTS:
        if lo <= hour < hi:
            return name
    return "evening"


# ---------------------------------------------------------------- 采样
def sample_copies(copy_paths):
    """读副本 → {host: {visits:[epoch...], by_profile:{name:count}, titles:Counter}}"""
    data = defaultdict(lambda: {"visits": [], "by_profile": Counter(),
                                "titles": Counter(), "kind": "web"})
    meta = {"files": [], "visits_total": 0, "http_total": 0, "non_web": 0,
            "self_constructed": 0, "extension": 0, "web": 0, "errors": []}
    for db in iter_history_files(copy_paths):
        profile = db.parent.name if db.name == "History" else db.name
        src = {"path": str(db), "bytes": db.stat().st_size, "sha256": sha256_file(str(db)),
               "profile": profile, "http": 0}
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT urls.url, visits.visit_time, urls.title FROM visits "
                "JOIN urls ON urls.id = visits.url"
            ).fetchall()
            con.close()
        except Exception as exc:  # 副本被移动/损坏 ⇒ 显式记录，不静默
            meta["errors"].append({"path": str(db), "error": f"{type(exc).__name__}: {exc}"})
            continue
        for url, vt, title in rows:
            meta["visits_total"] += 1
            host, kind = domain_of(url)
            if kind == "non_web":
                meta["non_web"] += 1
                continue
            if kind == "self_constructed":
                meta["self_constructed"] += 1
            elif kind == "extension":
                meta["extension"] += 1
            else:
                meta["web"] += 1
            src["http"] += 1
            meta["http_total"] += 1
            rec = data[host]
            rec["kind"] = kind
            rec["visits"].append(chrome_time_to_epoch(vt))
            rec["by_profile"][profile] += 1
            if title:
                rec["titles"][title.strip()] += 1
        meta["files"].append(src)
    return data, meta


# ---------------------------------------------------------------- 提及判定
def make_matcher(corpus_paths):
    files, total_bytes, blobs = collect_corpus(corpus_paths)
    if not files or not blobs:
        raise SystemExit("[ERROR] corpus_empty：语料 0 文件 ⇒ 拒绝输出「零提及」结论 "
                         f"(扫描路径={corpus_paths})")

    def match(host: str):
        needle = host.lower()
        hits = [p for p, t in blobs if needle in t]
        base = base_domain(host)
        base_hits = [] if base == needle else [p for p, t in blobs if base in t]
        return {"mentioned_full": bool(hits), "mentioned_base": bool(base_hits),
                "evidence": (hits + base_hits)[:3]}

    return match, {"files": len(files), "bytes": total_bytes, "paths": corpus_paths}


def analyse(copies, corpus, k_values, top):
    data, meta = sample_copies(copies)
    match, corpus_meta = make_matcher(corpus)

    web = {h: r for h, r in data.items() if r["kind"] == "web"}
    report = {"generated_at": datetime.now(CST).isoformat(timespec="seconds"),
              "sources": meta, "corpus": corpus_meta,
              "k_values": sorted(k_values), "tables": {}, "gap": {}, "stability": {}}

    for k in sorted(k_values):
        rows = []
        for host, rec in web.items():
            n = len(rec["visits"])
            if n < k:
                continue
            m = match(host)
            hours = Counter(datetime.fromtimestamp(t, CST).hour for t in rec["visits"])
            slots = Counter(slot_of(t) for t in rec["visits"])
            slot, slot_n = slots.most_common(1)[0]
            rows.append({
                "domain": host, "visits": n,
                "base": base_domain(host),
                "mentioned_full": m["mentioned_full"], "mentioned_base": m["mentioned_base"],
                "gap": not (m["mentioned_full"] or m["mentioned_base"]),
                "mention_evidence": m["evidence"],
                "profiles": dict(rec["by_profile"]),
                "hour_hist": {str(h): hours[h] for h in range(24) if hours[h]},
                "top_slot": slot, "top_slot_share": round(slot_n / n, 3),
                "top_title": (rec["titles"].most_common(1)[0][0] if rec["titles"] else ""),
            })
        rows.sort(key=lambda r: (-r["visits"], r["domain"]))
        gaps = [r for r in rows if r["gap"]]
        report["tables"][str(k)] = {
            "domains_ge_k": len(rows), "gap_domains": len(gaps),
            "gap_visits": sum(r["visits"] for r in gaps),
            "gap_share_of_web_visits": round(
                sum(r["visits"] for r in gaps) / max(1, sum(len(v["visits"]) for v in web.values())), 4),
            "top_gap": gaps[:top], "top_all": [{kk: r[kk] for kk in
                ("domain", "visits", "mentioned_full", "mentioned_base", "gap")} for r in rows[:top]],
        }
        report["gap"][str(k)] = gaps

    ks = sorted(k_values)
    if len(ks) >= 2:
        pairs = []
        for a, b in zip(ks, ks[1:]):
            sa = {r["domain"] for r in report["gap"][str(a)]}
            sb = {r["domain"] for r in report["gap"][str(b)]}
            pairs.append({
                "k_lo": a, "k_hi": b, "gap_lo": len(sa), "gap_hi": len(sb),
                "gap_both": len(sa & sb), "kept_at_hi_of_gap_lo": round(len(sa & sb) / max(1, len(sa)), 3),
                "kept_at_lo_of_gap_hi": round(len(sa & sb) / max(1, len(sb)), 3),
                "verdict": _verdict(sa, sb),
            })
        report["stability"] = pairs[0]
        report["stability_pairs"] = pairs
    return report


# ---------------------------------------------------------------- 命令
def cmd_scan(args):
    copies = args.copies or DEFAULT_COPIES
    corpus = args.corpus or DEFAULT_CORPUS
    k_values = [int(x) for x in args.k.split(",") if x.strip()]
    if any(k <= 0 for k in k_values):
        print("[ERROR] --k 必须为正整数", file=sys.stderr)
        return 2
    rep = analyse(copies, corpus, k_values, args.top)
    src = rep["sources"]
    print(f"[sources] 历史库 {len(src['files'])} 个 / visits {src['visits_total']} / http(s) {src['http_total']} "
          f"= web {src['web']} + 自造域 {src['self_constructed']} + 扩展 {src['extension']} / 非 http {src['non_web']}")
    if rep["sources"]["errors"]:
        print(f"[sources][WARN] 读取失败 {len(rep['sources']['errors'])}: {rep['sources']['errors'][:2]}")
    print(f"[corpus] 报告/记忆语料 {rep['corpus']['files']} 文件 {rep['corpus']['bytes']}B "
          f"(路径={rep['corpus']['paths']})")
    for k in sorted(k_values):
        t = rep["tables"][str(k)]
        print(f"[K={k}] ≥K 域名 {t['domains_ge_k']} / 其中零提及 {t['gap_domains']} "
              f"({t['gap_visits']} 次访问, 占全 web 访问 {t['gap_share_of_web_visits']:.2%})")
        for r in t["top_gap"][:args.show]:
            print(f"   - {r['domain']:<38} {r['visits']:>5} 次  主时段={r['top_slot']}"
                  f"({r['top_slot_share']:.0%})  profiles={len(r['profiles'])}")
    if rep["stability"]:
        s = rep["stability"]
        print(f"[stability] K={s['k_lo']}→{s['k_hi']}: gap {s['gap_lo']}→{s['gap_hi']}, "
              f"两档都在 {s['gap_both']} (低档保留率 {s['kept_at_hi_of_gap_lo']:.0%}) ⇒ {s['verdict']}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[out] {out} ({out.stat().st_size}B)")
    return 0


# ---------------------------------------------------------------- 自测
def _mk_history(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
    for i, (url, vt) in enumerate(rows, 1):
        con.execute("INSERT INTO urls VALUES(?,?,?)", (i, url, f"t{i}"))
        con.execute("INSERT INTO visits VALUES(?,?,?)", (i, i, vt))
    con.commit()
    con.close()


def cmd_selftest(_args):
    import tempfile
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name} {extra}")

    # T1 domain_of 归类
    check("T1 domain_of 分类", domain_of("https://a.b.com/x") == ("a.b.com", "web")
          and domain_of("http://localhost:8899/")[1] == "self_constructed"
          and domain_of("file:///tmp/x")[1] == "non_web"
          and domain_of("chrome-extension://abcdefghijklmnop/x")[1] == "extension")
    # T2 base_domain
    check("T2 base_domain", base_domain("a.b.co.uk") == "b.co.uk"
          and base_domain("www.foo.com") == "foo.com" and base_domain("foo.com") == "foo.com")
    # T3 chrome 时间换算（1601 纪元）
    check("T3 chrome_time_to_epoch", abs(chrome_time_to_epoch(133_000_000_000_000_00)
                                         - (133_000_000_000_000_00 / 1e6 - CHROME_EPOCH_OFFSET)) < 1e-6)
    # T4 时段划分
    h = datetime(2026, 9, 27, 3, 0, tzinfo=CST).timestamp()
    check("T4 slot 归属", slot_of(h) == "night" and slot_of(h + 6 * 3600) == "morning")

    tmp = Path(tempfile.mkdtemp(prefix="domain_gap_"))
    now = datetime.now(CST)
    ct = lambda dt: int((dt.timestamp() + CHROME_EPOCH_OFFSET) * 1_000_000)
    rows = []
    for i in range(30):  # 已知被提及的域名（语料里一定有 github.com）
        rows.append(("https://github.com/x", ct(now - timedelta(hours=i))))
    for i in range(25):  # 造一个必定零提及的域名
        rows.append(("https://zzz-never-mentioned-4f2a.example/page", ct(now - timedelta(hours=i))))
    for i in range(7):  # 低频，不该进 K=20 表
        rows.append(("https://rare-1f3b.example/", ct(now)))
    dbp = tmp / "Default" / "History"
    dbp.parent.mkdir(parents=True)
    _mk_history(str(dbp), rows)

    corpus = tmp / "corpus"
    corpus.mkdir()
    (corpus / "r.md").write_text("我们常用 github.com 与 api.github.com 作参考。", encoding="utf-8")

    # T5 正常路径：github.com ≥K 但被提及 ⇒ 不是 gap
    rep = analyse([str(dbp)], [str(corpus)], [20], top=10)
    t20 = rep["tables"]["20"]
    doms = {r["domain"]: r for r in t20["top_all"]}
    check("T5 已提及域名不入 gap", doms["github.com"]["gap"] is False
          and doms["github.com"]["mentioned_full"] is True, str(doms.get("github.com")))
    # NC1（负控）：确定零提及的域名必须被抓成 gap
    check("NC1 真·零提及域名被抓出", "zzz-never-mentioned-4f2a.example" in {r["domain"] for r in t20["top_gap"]})
    # NC2（负控）：低频域名不得进 K=20
    check("NC2 低频域不进表", "rare-1f3b.example" not in doms)
    # NC3（负控，核心）：语料为空必须 rc≠0，绝不能输出全量「零提及」
    empty = tmp / "empty_corpus"
    empty.mkdir()
    raised = False
    try:
        analyse([str(dbp)], [str(empty)], [20], top=5)
    except SystemExit as exc:
        raised = "corpus_empty" in str(exc)
    check("NC3 空语料 fail-closed", raised)
    # NC4（负控）：www./base 回退——子域未出现但基域出现 ⇒ 不算 gap
    (corpus / "r2.md").write_text("业务系统见 example-corp.com 后台。", encoding="utf-8")
    m2, _ = make_matcher([str(corpus)])
    check("NC4 base 域回退", m2("www.example-corp.com")["mentioned_base"] is True)
    # NC5：副本损坏/缺失不得静默（errors 记账）
    rep2 = analyse([str(tmp / "nope_missing")], [str(corpus)], [20], top=5)
    check("NC5 缺库不静默", rep2["sources"]["visits_total"] == 0
          and rep2["tables"]["20"]["domains_ge_k"] == 0)
    # T6 K 敏感性输出
    rep3 = analyse([str(dbp)], [str(corpus)], [20, 25], top=5)
    check("T6 K 敏感性字段", "verdict" in rep3["stability"] and rep3["stability"]["k_hi"] == 25)

    print(f"\nselftest: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="高频访问但零提及域名对账（只读）")
    sub = ap.add_subparsers(dest="cmd")
    sc = sub.add_parser("scan", help="扫描并出对账")
    sc.add_argument("--copies", nargs="*", default=None, help="History 文件或目录（默认两处冻结副本）")
    sc.add_argument("--corpus", nargs="*", default=None, help="语料目录/文件（默认 memory + temp/autonomous_reports）")
    sc.add_argument("--k", default="20,50", help="访问次数阈值，逗号分隔（默认 20,50）")
    sc.add_argument("--top", type=int, default=40, help="JSON 中每档保留条数")
    sc.add_argument("--show", type=int, default=10, help="终端打印条数")
    sc.add_argument("--out", default=None, help="JSON 落盘路径")
    sc.set_defaults(func=cmd_scan)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return cmd_selftest(args)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
