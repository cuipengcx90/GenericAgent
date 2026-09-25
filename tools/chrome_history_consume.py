#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chrome History 快照「消费面」工具（R423）——只读、离线、可复跑。

背景
----
`tools/rolling_source_snapshot.py` 每天把 Chrome `Default/History` 冷拷贝进
`temp/chrome_snapshots/<日期>/`，但长期**零消费者**（R415/R418 已记）。R392 已消费过
"小时分布形状 × knowledgeC 交叉"，本工具只做**未被 R392 覆盖的增量维度**：

  * `trends`  站点趋势：近 W 天 vs 其前 (90-W) 天的**日均**访问量/活跃天数 → 升降榜（消费方：备份/保留策略）
  * `drift`   长期时段漂移：按**月**分组的「每日首访小时」（作息起点代理指标）中位数 + 晚间占比（消费方：择时/排程）
  * `volume`  体量：每周访问量、字节/访问、域名长尾度（消费方：清理/裁剪）

设计约束（验收口径）
--------------------
1. **只读**：一律以 `file:<path>?mode=ro` 打开**快照副本**，绝不触碰源库；每次运行打印
   副本 sha256 前后指纹（`--prove-readonly` 默认开），不一致即 rc≠0。
2. **不重出全量小时分布**：drift 只输出"首访小时"的月度统计量，不输出 0–23 时直方图。
3. **隐私**：只聚合到**域名**级，绝不打印完整 URL / 路径 / 查询串 / 标题；`--json` 同样只含域名。
4. **自造访问声明**：自驾任务（日报/TL3 监控/网盘监控/项目监控）走同一 Chrome，
   `SELF_HOSTS` 为 R362 已证的自造域名表；所有指标同时给 `all` 与 `excl_self` 两口径。
   注意 `linux.do` / `github.com` 属**人机混用**，剥离后是**下界**（真值介于两口径之间）。

用法
----
    python3 tools/chrome_history_consume.py                       # 取最新快照，跑三个维度
    python3 tools/chrome_history_consume.py --snapshot <dir>
    python3 tools/chrome_history_consume.py --window 30 --json out.json
    python3 tools/chrome_history_consume.py --selftest             # 合成库自测（不读真实数据）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

CHROME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "temp" / "chrome_snapshots"
# R362 已证的自造访问域名（自驾任务的日报/监控走同一 Chrome）
SELF_HOSTS = {
    "linux.do",
    "github.com",
    "birdiesearch.com",
    "birdiesearch.top",
    "haisou.cc",
    "trytrellis.com",
}

_HOST_SQL = """
WITH base AS (
  SELECT v.id AS vid, v.visit_time AS vt,
         substr(u.url, instr(u.url, '//') + 2) AS rest,
         u.url AS url
  FROM visits v JOIN urls u ON u.id = v.url
  WHERE u.url LIKE 'http://%' OR u.url LIKE 'https://%'
),
cut AS (
  SELECT vid, vt,
         CASE WHEN instr(rest, '/') > 0 THEN substr(rest, 1, instr(rest, '/') - 1) ELSE rest END AS hostport
  FROM base
)
SELECT vid, vt,
       CASE WHEN instr(hostport, ':') > 0 THEN substr(hostport, 1, instr(hostport, ':') - 1) ELSE hostport END AS host
FROM cut
"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def chrome_to_dt(us: int) -> dt.datetime:
    return CHROME_EPOCH + dt.timedelta(microseconds=int(us))


def dt_to_chrome(d: dt.datetime) -> int:
    return int((d - CHROME_EPOCH).total_seconds() * 1_000_000)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def latest_snapshot(root: Path) -> Path:
    cands = sorted([p for p in root.iterdir() if p.is_dir() and (p / "History").exists()])
    if not cands:
        raise SystemExit(f"[ERR] 未找到含 History 的快照目录：{root}")
    return cands[-1]


def load_visits(history: Path):
    """返回 [(datetime_local, host, vid)]，只读打开副本。"""
    con = sqlite3.connect(f"file:{history}?mode=ro", uri=True)
    try:
        rows = con.execute(_HOST_SQL).fetchall()
    finally:
        con.close()
    out = []
    for vid, vt, host in rows:
        try:
            t = chrome_to_dt(vt).astimezone()
        except Exception:
            continue
        out.append((t, norm_host(host), vid))
    return out


# --------------------------------------------------------------------------- #
# 维度 1：站点趋势
# --------------------------------------------------------------------------- #
def dim_trends(visits, window_days: int, top: int = 12):
    if not visits:
        return {}
    ref = max(v[0] for v in visits)
    recent_start = ref - dt.timedelta(days=window_days)
    prior_start = ref - dt.timedelta(days=window_days * 3)  # 前 2W 天

    buckets = {"recent": {}, "prior": {}}
    days = {"recent": set(), "prior": set()}
    counts = {"recent": 0, "prior": 0}
    for t, host, _ in visits:
        if t >= recent_start:
            b, key = "recent", t.date()
        elif prior_start <= t < recent_start:
            b, key = "prior", t.date()
        else:
            continue
        counts[b] += 1
        days[b].add(key)
        d = buckets[b].setdefault(host, {"visits": 0, "days": set()})
        d["visits"] += 1
        d["days"].add(key)

    nd_recent = max(1, window_days)
    nd_prior = max(1, window_days * 2)

    def per_day(host, b, nd):
        e = buckets[b].get(host)
        return (e["visits"] / nd) if e else 0.0

    hosts = set(buckets["recent"]) | set(buckets["prior"])

    def pack(host):
        return {
            "host": host,
            "recent_visits": buckets["recent"].get(host, {}).get("visits", 0),
            "prior_visits_60d": buckets["prior"].get(host, {}).get("visits", 0),
            "recent_active_days": len(buckets["recent"].get(host, {}).get("days", ())),
            "prior_active_days": len(buckets["prior"].get(host, {}).get("days", ())),
            "recent_per_day": round(per_day(host, "recent", nd_recent), 3),
            "prior_per_day": round(per_day(host, "prior", nd_prior), 3),
            "delta_per_day": round(per_day(host, "recent", nd_recent) - per_day(host, "prior", nd_prior), 3),
        }

    all_rows = [pack(h) for h in hosts]
    rising = sorted([r for r in all_rows if r["recent_per_day"] > r["prior_per_day"]],
                    key=lambda r: -r["delta_per_day"])[:top]
    falling = sorted([r for r in all_rows if r["recent_per_day"] < r["prior_per_day"]],
                     key=lambda r: r["delta_per_day"])[:top]
    return {
        "ref_time": ref.strftime("%Y-%m-%d %H:%M"),
        "window_days": window_days,
        "recent_start": recent_start.strftime("%Y-%m-%d"),
        "prior_start": prior_start.strftime("%Y-%m-%d"),
        "counts": {
            "recent_visits": counts["recent"], "prior_visits": counts["prior"],
            "recent_days": len(days["recent"]), "prior_days": len(days["prior"]),
            "recent_per_day": round(counts["recent"] / nd_recent, 1),
            "prior_per_day": round(counts["prior"] / nd_prior, 1),
            "distinct_hosts_recent": len(buckets["recent"]),
            "distinct_hosts_prior": len(buckets["prior"]),
        },
        "rising": rising,
        "falling": falling,
    }


# --------------------------------------------------------------------------- #
# 维度 2：长期时段漂移（只出「首访小时」统计量）
# --------------------------------------------------------------------------- #
def dim_drift(visits):
    if not visits:
        return {}
    by_day = {}
    for t, _h, _ in visits:
        d = t.date()
        e = by_day.setdefault(d, {"first": t, "last": t, "n": 0, "evening": 0})
        if t < e["first"]:
            e["first"] = t
        if t > e["last"]:
            e["last"] = t
        e["n"] += 1
        if t.hour >= 20:
            e["evening"] += 1
    months = {}
    for d, e in sorted(by_day.items()):
        key = f"{d.year}-{d.month:02d}"
        m = months.setdefault(key, {"days": 0, "first_hours": [], "last_hours": [], "n": 0, "evening": 0})
        m["days"] += 1
        m["first_hours"].append(e["first"].hour + e["first"].minute / 60)
        m["last_hours"].append(e["last"].hour + e["last"].minute / 60)
        m["n"] += e["n"]
        m["evening"] += e["evening"]
        # 「每日首访早于 09:30」的天数占比：用于判断哪些时段与用户上网起点重叠
        if e["first"].hour < 9 or (e["first"].hour == 9 and e["first"].minute < 30):
            m["early_days"] = m.get("early_days", 0) + 1

    def median(xs):
        xs = sorted(xs)
        n = len(xs)
        if not n:
            return None
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    out = {}
    for k, m in sorted(months.items()):
        out[k] = {
            "days": m["days"],
            "median_first_visit_hour": round(median(m["first_hours"]), 2),
            "median_last_visit_hour": round(median(m["last_hours"]), 2),
            "visits": m["n"],
            "visits_per_day": round(m["n"] / max(1, m["days"]), 1),
            "evening_share_pct": round(100.0 * m["evening"] / max(1, m["n"]), 1),
            "first_before_0930_pct": round(100.0 * m.get("early_days", 0) / max(1, m["days"]), 1),
        }
    return out


# --------------------------------------------------------------------------- #
# 维度 3：体量 / 长尾（清理与保留策略输入）
# --------------------------------------------------------------------------- #
def dim_volume(visits, db_bytes: int):
    if not visits:
        return {}
    per_week = {}
    per_host = {}
    per_host_days = {}
    for t, h, _ in visits:
        wk = f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}"
        e = per_week.setdefault(wk, {"n": 0, "days": set()})
        e["n"] += 1
        e["days"].add(t.date())
        per_host[h] = per_host.get(h, 0) + 1
        per_host_days.setdefault(h, set()).add(t.date())
    weeks = {k: {"visits": v["n"], "active_days": len(v["days"]),
                 "visits_per_active_day": round(v["n"] / max(1, len(v["days"])), 1)}
             for k, v in sorted(per_week.items())}
    total = sum(per_host.values())
    top = sorted(per_host.items(), key=lambda kv: -kv[1])
    top10 = sum(n for _, n in top[:10])
    singles = [h for h, n in per_host.items() if n == 1]
    return {
        "total_visits": total,
        "distinct_hosts": len(per_host),
        "top10_share_pct": round(100.0 * top10 / total, 1),
        "single_visit_hosts": len(singles),
        "single_visit_share_pct": round(100.0 * len(singles) / max(1, len(per_host)), 1),
        "db_bytes": db_bytes,
        "bytes_per_visit": round(db_bytes / total, 1),
        "per_week": weeks,
        "top_hosts": [{"host": h, "visits": n, "active_days": len(per_host_days[h])} for h, n in top[:10]],
    }


def dim_uniform(visits, min_visits: int = 50, top: int = 15):
    """「疑似自驾」指纹：R362 的 SELF_HOSTS 表是当时的快照，后续新增的定时任务
    （dpdomain_renew / 日报 / 归档站等）不在表内。自驾/监控型访问的统计指纹 =
    几乎每天都访问 + 每日次数稳定（变异系数 CV 低）⇒ 用 (活跃天占比, CV) 双条件标记，
    仅作**提示**，不作剥离依据（人机混用域名无法从 History 区分）。"""
    if not visits:
        return {}
    days = sorted({t.date() for t, _, _ in visits})
    span = max(1, (days[-1] - days[0]).days + 1)
    per = {}
    for t, h, _ in visits:
        e = per.setdefault(h, {})
        e[t.date()] = e.get(t.date(), 0) + 1
    out = []
    for h, d in per.items():
        n = sum(d.values())
        if n < min_visits:
            continue
        vals = [d.get(x, 0) for x in days]
        mean = n / len(days)
        var = sum((x - mean) ** 2 for x in vals) / len(vals)
        cv = (var ** 0.5) / mean if mean else 0
        active_ratio = len(d) / span
        out.append({
            "host": h, "visits": n, "active_days": len(d), "span_days": span,
            "active_ratio": round(active_ratio, 2), "cv": round(cv, 2),
            "visits_per_active_day": round(n / max(1, len(d)), 1),
            "suspected_automation": bool(active_ratio >= 0.8 and cv <= 0.6),
        })
    out.sort(key=lambda r: (-int(r["suspected_automation"]), -r["visits"]))
    return {"day_span": span, "min_visits": min_visits, "hosts": out[:top]}


def split_self(visits):
    return ([v for v in visits if v[1] not in SELF_HOSTS],
            [v for v in visits if v[1] in SELF_HOSTS])


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(snapshot: Path, window: int, out_json: Path | None, prove_readonly: bool = True):
    history = snapshot / "History"
    if not history.exists():
        raise SystemExit(f"[ERR] 快照缺少 History：{history}")
    hashes = {}
    if prove_readonly:
        hashes["before"] = sha256_file(history)
    visits = load_visits(history)
    clean, self_v = split_self(visits)
    db_bytes = history.stat().st_size

    result = {
        "snapshot": str(snapshot),
        "history_bytes": db_bytes,
        "visits_total": len(visits),
        "visits_excl_self": len(clean),
        "self_host_visits": len(self_v),
        "self_host_share_pct": round(100.0 * len(self_v) / max(1, len(visits)), 1),
        "self_hosts": sorted(SELF_HOSTS),
        "trends_all": dim_trends(visits, window),
        "trends_excl_self": dim_trends(clean, window),
        "drift_all": dim_drift(visits),
        "drift_excl_self": dim_drift(clean),
        "volume_all": dim_volume(visits, db_bytes),
        "volume_excl_self": dim_volume(clean, db_bytes),
        "uniform_all": dim_uniform(visits),
        "uniform_excl_self": dim_uniform(clean),
    }
    if prove_readonly:
        hashes["after"] = sha256_file(history)
        result["readonly_proof"] = {
            "sha256_before": hashes["before"], "sha256_after": hashes["after"],
            "pass": hashes["before"] == hashes["after"],
        }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def render(res: dict, window: int):
    P = print
    P(f"# Chrome History 快照消费（{res['snapshot']}）")
    P(f"副本 {res['history_bytes']} B / visits {res['visits_total']}（剥离自造后 {res['visits_excl_self']}，"
      f"自造 {res['self_host_visits']} = {res['self_host_share_pct']}%）")
    if "readonly_proof" in res:
        rp = res["readonly_proof"]
        P(f"只读自证 sha256: {'PASS' if rp['pass'] else 'FAIL'} {rp['sha256_before'][:16]}…")
        if not rp["pass"]:
            raise SystemExit("[ERR] 副本指纹变化（只读自证失败）")
    P("")
    P(f"## 1. 站点趋势（近 {window} 天 vs 前 {window*2} 天，按日均访问）")
    for label, key in (("全量", "trends_all"), ("剥离自造", "trends_excl_self")):
        t = res[key]
        if not t:
            continue
        P(f"### {label}: 近期 {t['counts']['recent_visits']} 次/{t['counts']['recent_days']} 天 "
          f"({t['counts']['recent_per_day']}/天) vs 前段 {t['counts']['prior_visits']} 次/"
          f"{t['counts']['prior_days']} 天 ({t['counts']['prior_per_day']}/天); "
          f"域名数 {t['counts']['distinct_hosts_recent']} vs {t['counts']['distinct_hosts_prior']}")
        P("  上升: " + ", ".join(f"{r['host']}({r['prior_per_day']}→{r['recent_per_day']})" for r in t["rising"][:8]))
        P("  下降: " + ", ".join(f"{r['host']}({r['prior_per_day']}→{r['recent_per_day']})" for r in t["falling"][:8]))
    P("")
    P("## 2. 长期时段漂移（只出每日首/末访小时，非 0–23 直方图）")
    def _drift_table(rows, title):
        P(title)
        P("| 月 | 天数 | 首访小时中位 | 末访小时中位 | 访问/天 | 20 时后占比 | 首访<09:30 天数占比 |")
        P("|---|---|---|---|---|---|---|")
        for k, v in rows.items():
            P(f"| {k} | {v['days']} | {v['median_first_visit_hour']} | {v['median_last_visit_hour']} | "
              f"{v['visits_per_day']} | {v['evening_share_pct']}% | {v.get('first_before_0930_pct')}% |")

    _drift_table(res["drift_all"], "")
    _drift_table(res.get("drift_excl_self", {}), "（同表·剥离自造口径）")
    P("")
    P(f"## 2b. 疑似自驾/常驻监控指纹（活跃天占比 ≥0.8 且 日次数 CV ≤0.6；全库 {res.get('uniform_all',{}).get('day_span')} 天）")
    P("| 域名 | 访问 | 活跃天 | 活跃占比 | CV | 次/活跃天 | 疑似自驾 |")
    P("|---|---|---|---|---|---|---|")
    for r in res.get("uniform_all", {}).get("hosts", []):
        P(f"| {r['host']} | {r['visits']} | {r['active_days']} | {r['active_ratio']} | {r['cv']} | "
          f"{r['visits_per_active_day']} | {'是' if r['suspected_automation'] else '否'} |")
    P("")
    P("## 3. 体量 / 长尾")
    v = res["volume_all"]
    P(f"总访问 {v['total_visits']}，域名 {v['distinct_hosts']}，Top10 占 {v['top10_share_pct']}%，"
      f"仅访 1 次域名 {v['single_visit_hosts']}（{v['single_visit_share_pct']}%），"
      f"{res['history_bytes']} B ⇒ {v['bytes_per_visit']} B/访问")
    P("| 周 | 访问 | 活跃天 | 访问/活跃天 |")
    P("|---|---|---|---|")
    for k, w in v["per_week"].items():
        P(f"| {k} | {w['visits']} | {w['active_days']} | {w['visits_per_active_day']} |")
    P("  剥离自造后 Top10 占 " + f"{res['volume_excl_self']['top10_share_pct']}%")


# --------------------------------------------------------------------------- #
# 自测（合成库，不读真实数据）
# --------------------------------------------------------------------------- #
def _mkdb(path: Path, rows):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR);"
        "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER,"
        " from_visit INTEGER, transition INTEGER, visit_duration INTEGER);"
    )
    for i, (url, ts) in enumerate(rows, 1):
        con.execute("INSERT INTO urls (id, url) VALUES (?, ?)", (i, url))
        con.execute("INSERT INTO visits (id, url, visit_time) VALUES (?, ?, ?)", (i, i, ts))
    con.commit()
    con.close()


def selftest() -> int:
    import tempfile
    fails = 0

    def check(name, cond, extra=""):
        nonlocal fails
        ok = bool(cond)
        if not ok:
            fails += 1
        print(f"  {'PASS' if ok else 'FAIL'} {name} {extra}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ref = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
        rows = []
        # 近 3 天：example.com 每天 2 次；old.com 仅前段
        for i in range(3):
            for _ in range(2):
                rows.append(("https://www.example.com/p", dt_to_chrome(ref - dt.timedelta(days=i, hours=i))))
        # 前段（第 5–10 天）：old.com 每天 4 次
        for i in range(5, 11):
            for _ in range(4):
                rows.append(("https://old.com/x", dt_to_chrome(ref - dt.timedelta(days=i, hours=3))))
        # 自造域名
        rows.append(("https://linux.do/t/1", dt_to_chrome(ref - dt.timedelta(hours=1))))
        rows.append(("https://example.com:8443/y", dt_to_chrome(ref - dt.timedelta(hours=2))))
        # 跨月样本（前一个月）
        rows.append(("https://example.com/z", dt_to_chrome(dt.datetime(2026, 8, 3, 22, 30, tzinfo=dt.timezone.utc))))
        # 非 http 协议不计入；once.com 仅访一次（长尾样本）
        rows.append(("chrome://settings", dt_to_chrome(ref)))
        rows.append(("https://once.com/a", dt_to_chrome(ref - dt.timedelta(days=1, hours=5))))
        db = td / "History"
        _mkdb(db, rows)

        visits = load_visits(db)
        check("只读加载 = 全部行 − 1 条 chrome://", len(visits) == len(rows) - 1 == 34,
              f"rows={len(rows)} got={len(visits)}")
        check("www. 归一 + 端口剥离", any(h == "example.com" for _, h, _ in visits)
              and all(":" not in h for _, h, _ in visits))

        clean, selfv = split_self(visits)
        check("自造域名剥离 1 条", len(selfv) == 1 and len(clean) == len(visits) - 1, f"selfv={len(selfv)}")

        t = dim_trends(clean, 3)
        hosts = {r["host"]: r for r in t["rising"] + t["falling"]}
        # 近窗口 3 天：example.com 每天 2 次 + 端口变体 1 次（归一后同域）⇒ 7/3
        # 注意维度值经 round(...,3) ⇒ 容差须 ≥5e-4，不能用 1e-9
        check("trends 近窗口日均 example.com=7/3", abs(hosts["example.com"]["recent_per_day"] - 7 / 3) <= 1e-3,
              f"got={hosts['example.com']['recent_per_day']}")
        # 前段窗口 = 2×window = 6 天，命中 4 天 × 4 次 ⇒ 16/6
        check("trends 前段 old.com 日均=16/6", abs(hosts["old.com"]["prior_per_day"] - 16 / 6) <= 1e-3,
              f"got={hosts['old.com']['prior_per_day']}")
        check("trends 下降榜首=old.com", t["falling"][0]["host"] == "old.com", f"got={t['falling'][0]['host']}")

        d = dim_drift(visits)
        check("drift 含两个月分组", len(d) == 2 and "2026-08" in d and "2026-09" in d, f"keys={list(d)}")
        check("drift 9 月 evening_share 有值(0-100)", 0 <= d["2026-09"]["evening_share_pct"] <= 100)

        v = dim_volume(clean, db.stat().st_size)
        check("volume 总访问=clean 条数", v["total_visits"] == len(clean), f"got={v['total_visits']}")
        check("volume 单次访问域名=once.com 1 个", v["single_visit_hosts"] == 1, f"got={v['single_visit_hosts']}")
        u = dim_uniform(visits, min_visits=10)
        check("uniform 结构含 hosts 且 CV 可算", bool(u.get("hosts")) and all("cv" in r for r in u["hosts"]),
              f"n={len(u.get('hosts', []))}")

        # 只读自证：改动文件后指纹须变化
        h1 = sha256_file(db)
        with open(db, "ab") as fh:
            fh.write(b"\x00")
        check("只读自证可检出篡改", sha256_file(db) != h1)

        # 负控：SELF_HOSTS 剥离数不得把普通域名误判
        clean2, self2 = split_self([(ref, "example.com", 1), (ref, "linux.do", 2)])
        check("负控 普通域名不入自造桶", len(self2) == 1 and len(clean2) == 1)

    print(f"selftest {'OK' if fails == 0 else 'FAILED'} ({'all passed' if fails == 0 else str(fails) + ' failures'})")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chrome History 快照消费（只读/离线）")
    ap.add_argument("--snapshot", help="快照目录（默认取 temp/chrome_snapshots 下最新）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="快照根目录")
    ap.add_argument("--window", type=int, default=30, help="近窗口天数（前段 = 2×）")
    ap.add_argument("--json", help="结果 JSON 落盘路径")
    ap.add_argument("--no-prove", action="store_true", help="跳过 sha256 只读自证")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    snap = Path(args.snapshot) if args.snapshot else latest_snapshot(Path(args.root))
    res = run(snap, args.window, Path(args.json) if args.json else None, prove_readonly=not args.no_prove)
    render(res, args.window)
    return 0


if __name__ == "__main__":
    sys.exit(main())
