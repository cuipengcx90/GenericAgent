#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kc_app_usage.py —— knowledgeC「注意力投入画像」：两条万级零消费者流的首份消费（只读）

背景（R434，第16批 C1，三路评审 9/8/9=8.67）：
  R415 把 knowledgeC 采成本地快照，R426 消费了 `/display/isBacklit`（熄屏）。
  仍有两条万级流**零消费者**：
    * `/app/usage`          （本快照 10427 行 / 44 个 app / 2026-09-01→09-25）
    * `/notification/usage` （本快照 5706 行 / 8 个来源 bundle / 2026-08-28→09-25）
  本器是它们的首个消费者：按「本地日 × app」聚合前台时长（区间先并后切日）、按「本地日 × 来源 app」
  统计通知并给密度，并把 app 前台区间与 `/display/isBacklit` 的 v=0（熄屏）区间做一致性检查。

口径（写死，防后续误用；与 memory/kc_profile/profile.py、tools/sleep_source_crosscheck.py 同源）：
  1. 时长 = `ZENDDATE - ZSTARTDATE`（仅用时间列；`ZVALUEDOUBLE`/`ZVALUEINTEGER` 是位型垃圾，R371 已证）。
  2. 同一 app 的区间**先做并集**再计秒：重叠/相邻区间不得重复计时。
  3. 跨本地午夜的区间**切分到两日**。
  4. 熄屏只认 `/display/isBacklit` 且 `ZVALUEINTEGER == 0`；「无行」（gap）是混合区，**不得**当亮屏也不得当熄屏。
  5. **空日 vs 零**：窗口内「该流一行都没有」的日期一律输出 `UNKNOWN`，绝不写成 0；
     「有行但合并后 0 秒」才输出 0.0。
  6. **快照内容时点** `cutoff = max(ZCREATIONDATE)`（全库）；`ZSTARTDATE > cutoff` 的行一律不计入
     （kc 有迟写/异常回填，晚于采集时点的段不是已发生事实）。
  7. **前台 ≠ 用户在操作**：`/app/usage` 只说明某 app 处于前台，长时间驻留（终端/浏览器）不代表人在用；
     本器输出的是**上界**，与熄屏重叠的部分即该上界的反证（见 §anomaly）。
  8. 通知来源 app 取结构化元数据 `Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID`（`ZVALUESTRING` 恒为 'Receive'，
     不是来源）；同一 `(秒, bundle, identifier)` 视为重复投递，raw 与 dedup 两个口径都给。

用法：
  python3 tools/kc_app_usage.py                                  # 默认：最新 kc 快照，全窗口 + 焦点日
  python3 tools/kc_app_usage.py --kc <db> --json out.json --top 10 --day 2026-09-24
  python3 tools/kc_app_usage.py --emit-profile temp/kc_profile    # 供 memory/kc_profile 消费的字段+溯源 JSON
  python3 tools/kc_app_usage.py --selftest                        # 18 组断言（含 NC1/NC3/NC7/NC13 + T2/T8/T10 共 7 条负控）
  python3 tools/kc_app_usage.py --readonly-proof                  # 前后 sha256 自证（副本硬判 + 源库软判）

只读：`file:...?mode=ro`；不写源库、不写快照、不改 profile.py、不接线定时。
"""

import argparse
import glob
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta

EPOCH_OFF = 978307200  # 2001-01-01 → unix（kc 的 Cocoa epoch）
APP = "/app/usage"
NOTIF = "/notification/usage"
BACKLIT = "/display/isBacklit"

SQL_APP = ("select ZSTARTDATE t, ZENDDATE e, ZVALUESTRING app from ZOBJECT"
           " where ZSTREAMNAME='/app/usage' order by ZSTARTDATE;")
SQL_NOTIF = ("select o.ZSTARTDATE t, s.Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID b,"
             " s.Z_DKNOTIFICATIONUSAGEMETADATAKEY__IDENTIFIER i"
             " from ZOBJECT o left join ZSTRUCTUREDMETADATA s on s.Z_PK=o.ZSTRUCTUREDMETADATA"
             " where o.ZSTREAMNAME='/notification/usage' order by o.ZSTARTDATE;")
SQL_BL = ("select ZSTARTDATE t, ZENDDATE e, ZVALUEINTEGER v from ZOBJECT"
          " where ZSTREAMNAME='/display/isBacklit' order by ZSTARTDATE;")
SQL_CUTOFF = "select max(ZCREATIONDATE) from ZOBJECT;"

DEFAULT_KC_GLOB = "temp/kc_snapshots/*/knowledgeC.db"


# ------------------------------------------------------------------ 基础工具
def dt(ts):
    return datetime.fromtimestamp(ts + EPOCH_OFF)


def merge(iv):
    """区间并集：返回互不重叠且已排序的 [(s,e)]；空输入 → []。"""
    out = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def split_by_day(iv):
    """把合并后的区间按本地日边界切分 → {date: 秒数}"""
    out = defaultdict(float)
    for s, e in iv:
        cur = dt(s)
        end = dt(e)
        while cur < end:
            nxt = min(end, datetime.combine(cur.date() + timedelta(days=1), datetime.min.time()))
            out[cur.date()] += (nxt - cur).total_seconds()
            cur = nxt
    return out


def overlap_secs(iv_a, iv_b):
    """两组区间集合（各已 merge）的总重叠秒数"""
    i = j = 0
    total = 0.0
    a, b = list(iv_a), list(iv_b)
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e > s:
            total += e - s
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(buf)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path), uri=True)


def latest_kc(root="."):
    cands = sorted(glob.glob(os.path.join(root, DEFAULT_KC_GLOB)))
    return cands[-1] if cands else None


# ------------------------------------------------------------------ 分析内核
def analyze(db_path, top=10, focus_day=None):
    """只读分析 kc 副本 → dict（全部数字可复算）"""
    con = _ro(db_path)
    cur = con.cursor()
    cutoff_raw = cur.execute(SQL_CUTOFF).fetchone()[0]
    cutoff = cutoff_raw if cutoff_raw is not None else float("inf")

    rep = {"db": os.path.abspath(db_path), "cutoff_unix": cutoff,
           "cutoff_local": dt(cutoff).strftime("%Y-%m-%d %H:%M:%S") if cutoff != float("inf") else None,
           "skipped": Counter()}

    # ---- /app/usage：per app 合并 → 切日 ----
    rows = cur.execute(SQL_APP).fetchall()
    per_app_iv = defaultdict(list)
    app_days = defaultdict(lambda: defaultdict(float))   # app -> day -> secs
    day_secs = defaultdict(float)
    day_appsecs = defaultdict(lambda: defaultdict(float))
    day_rows = Counter()
    for t, e, app in rows:
        if t is None or e is None:
            rep["skipped"]["app_null_dates"] += 1
            continue
        if t > cutoff:
            rep["skipped"]["app_after_cutoff"] += 1
            continue
        if e <= t:
            rep["skipped"]["app_nonpositive"] += 1
            continue
        per_app_iv[app].append((t, min(e, cutoff)))
    for app, iv in per_app_iv.items():
        for d, secs in split_by_day(merge(iv)).items():
            app_days[app][d] += secs
            day_secs[d] += secs
            day_appsecs[d][app] += secs
    app_rows = Counter()
    for t, e, app in rows:
        if t is None or e is None or t > cutoff:
            continue
        app_rows[(app, dt(t).date())] += 1
    for (app, d), n in app_rows.items():
        day_rows[d] += n
    rep["skipped"]["app_rows_after_cutoff_kept_out"] = rep["skipped"]["app_after_cutoff"]

    # ---- 通知 ----
    nrows = cur.execute(SQL_NOTIF).fetchall()
    notif_day = Counter()
    notif_day_dedup = Counter()
    notif_app = Counter()
    notif_app_dedup = Counter()
    seen = set()
    n_skipped = 0
    notif_hour = Counter()
    for t, b, ident in nrows:
        if t is None or t > cutoff:
            n_skipped += 1
            continue
        b = b or "(unknown)"
        d = dt(t).date()
        notif_day[d] += 1
        notif_app[b] += 1
        notif_hour[dt(t).hour] += 1
        key = (t, b, ident)
        if key not in seen:
            seen.add(key)
            notif_day_dedup[d] += 1
            notif_app_dedup[b] += 1
    rep["skipped"]["notif_after_cutoff_or_null"] = n_skipped

    # ---- 熄屏区间（v=0）----
    off_iv = []
    for t, e, v in cur.execute(SQL_BL).fetchall():
        if t is None or e is None or e <= t or t > cutoff:
            continue
        if int(v if v is not None else 0) == 0:
            off_iv.append((t, min(e, cutoff)))
    off_iv = merge(off_iv)

    # ---- 一致性检查：前台区间 ∩ 熄屏 ----
    anomaly = []          # (app, start, end, ovl_secs)  —— 用合并后的区间
    per_app_anom = defaultdict(float)
    for app, iv in per_app_iv.items():
        m = merge(iv)
        for s, e in m:
            o = overlap_secs([(s, e)], off_iv)
            if o > 0:
                anomaly.append({"app": app, "start": dt(s).strftime("%Y-%m-%d %H:%M:%S"),
                                "end": dt(e).strftime("%Y-%m-%d %H:%M:%S"),
                                "secs": round(e - s, 3), "off_overlap_s": round(o, 3)})
                per_app_anom[app] += o
    anomaly.sort(key=lambda x: -x["off_overlap_s"])

    # ---- 日序列：UNKNOWN vs 0 ----
    adays = sorted(set(day_secs) | set(notif_day))
    if adays:                       # ★补齐中间「一行都没有」的日期 → UNKNOWN（不得从序列里消失）
        cursor_d = adays[0]
        full = []
        while cursor_d <= adays[-1]:
            full.append(cursor_d)
            cursor_d += timedelta(days=1)
        adays = full
    days = []
    for d in adays:
        has_app = d in day_secs
        has_notif = d in notif_day
        days.append({
            "day": str(d),
            "fg_hours": round(day_secs[d] / 3600.0, 3) if has_app else "UNKNOWN",
            "app_rows": day_rows.get(d, 0),
            "notif_raw": notif_day.get(d, "UNKNOWN") if has_notif else "UNKNOWN",
            "notif_dedup": notif_day_dedup.get(d, "UNKNOWN") if has_notif else "UNKNOWN",
        })
    if not focus_day and days:
        # 默认焦点日 = 最后一个「有 app 行且不是 cutoff 当日（当日不完整）」的日期
        cand = [x["day"] for x in days if x["fg_hours"] != "UNKNOWN" and x["day"] != dt(cutoff).date().isoformat()]
        focus_day = cand[-1] if cand else (days[-1]["day"] if days else None)

    window = {
        "first_day": days[0]["day"] if days else None,
        "last_day": days[-1]["day"] if days else None,
        "days_total": len(days),
        "days_with_app": sum(1 for d in days if d["fg_hours"] != "UNKNOWN"),
        "days_without_app": sum(1 for d in days if d["fg_hours"] == "UNKNOWN"),
        "days_without_notif": sum(1 for d in days if d["notif_raw"] == "UNKNOWN"),
        "fg_hours_total": round(sum(day_secs.values()) / 3600.0, 3),
        "notif_raw_total": sum(notif_day.values()),
        "notif_dedup_total": sum(notif_day_dedup.values()),
        "apps": len(per_app_iv),
        "notif_bundles": len(notif_app),
        "off_hours": round(sum(e - s for s, e in off_iv) / 3600.0, 3),
        "off_intervals": len(off_iv),
        "anomaly_intervals": len(anomaly),
        "anomaly_off_overlap_h": round(sum(per_app_anom.values()) / 3600.0, 3),
    }

    # ---- 排行 ----
    def rank(dd, n):
        tot = sum(dd.values()) or 1.0
        return [{"app": k, "value": round(v, 3), "share": round(100.0 * v / tot, 2)}
                for k, v in sorted(dd.items(), key=lambda x: -x[1])[:n]]

    app_total_h = {a: sum(dd.values()) / 3600.0 for a, dd in app_days.items()}
    app_day_h = {a: v / 3600.0 for a, v in day_appsecs.get(datetime.strptime(focus_day, "%Y-%m-%d").date(), {}).items()} \
        if focus_day else {}

    out = {
        "report": "kc_app_usage/v1",
        "db": rep["db"],
        "cutoff_local": rep["cutoff_local"],
        "skipped": dict(rep["skipped"]),
        "window": window,
        "focus_day": focus_day,
        "days": days,
        "app_total_hours_rank": rank(app_total_h, top),
        "app_focus_day_hours_rank": rank(app_day_h, top),
        "notif_rank_raw": [{"bundle": k, "count": v} for k, v in notif_app.most_common(top)],
        "notif_rank_dedup": [{"bundle": k, "count": v} for k, v in notif_app_dedup.most_common(top)],
        "notif_hour_hist": dict(sorted(notif_hour.items())),
        "notif_per_fg_hour": round(window["notif_raw_total"] / (window["fg_hours_total"] or 1), 2),
        "anomaly_top": anomaly[:top],
        "anomaly_by_app": [{"app": k, "off_overlap_s": round(v, 1)}
                           for k, v in sorted(per_app_anom.items(), key=lambda x: -x[1])[:top]],
    }
    con.close()
    return out


# ------------------------------------------------------------------ 输出
def text_report(r, top=10):
    w = r["window"]
    L = []
    L.append("=== kc 注意力投入画像（只读消费 /app/usage + /notification/usage） ===")
    L.append("库: %s    快照内容时点(cutoff): %s" % (r["db"], r["cutoff_local"]))
    L.append("窗口: %s → %s（%d 天；有 app 行 %d 天 / 无行 %d 天；无通知行 %d 天）"
             % (w["first_day"], w["last_day"], w["days_total"], w["days_with_app"],
                w["days_without_app"], w["days_without_notif"]))
    L.append("跳过: %s" % (r["skipped"] or "{}"))
    L.append("")
    L.append("-- 前台时长（合并后；app 总数 %d）窗口合计 %.2f h；熄屏(v=0) %.2f h / %d 段"
             % (w["apps"], w["fg_hours_total"], w["off_hours"], w["off_intervals"]))
    L.append("   Top%d（窗口）:" % top)
    for i, x in enumerate(r["app_total_hours_rank"], 1):
        L.append("   %2d. %-46s %8.2f h  %5.2f%%" % (i, x["app"], x["value"], x["share"]))
    L.append("   焦点日 %s Top%d:" % (r["focus_day"], top))
    for i, x in enumerate(r["app_focus_day_hours_rank"], 1):
        L.append("   %2d. %-46s %8.2f h  %5.2f%%" % (i, x["app"], x["value"], x["share"]))
    L.append("")
    L.append("-- 通知（来源 bundle %d 个）raw %d / dedup %d；%.2f 次/前台小时"
             % (w["notif_bundles"], w["notif_raw_total"], w["notif_dedup_total"], r["notif_per_fg_hour"]))
    for i, x in enumerate(r["notif_rank_raw"], 1):
        L.append("   %2d. %-46s raw %6d" % (i, x["bundle"], x["count"]))
    L.append("   逐时分布: %s" % r["notif_hour_hist"])
    L.append("")
    L.append("-- 一致性检查：前台区间 ∩ 熄屏(v=0)")
    L.append("   命中 %d 段，重叠合计 %.2f h（= 「声称前台但当时已熄屏」）"
             % (w["anomaly_intervals"], w["anomaly_off_overlap_h"]))
    for x in r["anomaly_by_app"]:
        L.append("   %-46s %8.1f s" % (x["app"], x["off_overlap_s"]))
    L.append("")
    L.append("-- 口径边界：/app/usage 只说明「某 app 处于前台」，不说明有人在操作；")
    L.append("   驻留型 app（终端/浏览器）会系统性高估，上表是**上界**；∩熄屏部分即该上界的反证。")
    return "\n".join(L)


# ------------------------------------------------------------------ selftest
def _fixture_db(path, app_rows=(), notif_rows=(), bl_rows=(), cutoff=None):
    con = sqlite3.connect(path)
    con.execute("create table ZOBJECT (Z_PK integer primary key, ZSTARTDATE real, ZENDDATE real,"
                " ZSTREAMNAME text, ZVALUESTRING text, ZVALUEINTEGER real, ZSTRUCTUREDMETADATA integer,"
                " ZCREATIONDATE real)")
    con.execute("create table ZSTRUCTUREDMETADATA (Z_PK integer primary key,"
                " Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID text,"
                " Z_DKNOTIFICATIONUSAGEMETADATAKEY__IDENTIFIER text)")
    pk = 0
    _ends = [r[1] for r in app_rows] + [r[1] for r in bl_rows] + [r[0] for r in notif_rows]
    _ends = [x for x in _ends if x is not None]
    if cutoff is None:
        cutoff = max(_ends) + 1 if _ends else 0.0

    def _crt(t, e):
        """事件行的 ZCREATIONDATE：真实库里 creation≈end（实测 max(creation)=末条 end）。
        ★显式 cutoff 时，把「晚于 cutoff 的行」的 creation 压到 cutoff——否则该行自身会成为
        max(ZCREATIONDATE) 从而把 cutoff 顶大（初版 5 处 selftest 失败即此因，勿回退）。"""
        c = e if e is not None else t
        if c is None:
            return None
        return cutoff if c > cutoff else c

    for t, e, app in app_rows:
        pk += 1
        con.execute("insert into ZOBJECT values (?,?,?,?,?,?,?,?)",
                    (pk, t, e, APP, app, None, None, _crt(t, e)))
    for t, b, i in notif_rows:
        pk += 1
        con.execute("insert into ZSTRUCTUREDMETADATA values (?,?,?)", (pk, b, i))
        con.execute("insert into ZOBJECT values (?,?,?,?,?,?,?,?)",
                    (pk, t, t, NOTIF, "Receive", None, pk, _crt(t, t)))
    for t, e, v in bl_rows:
        pk += 1
        con.execute("insert into ZOBJECT values (?,?,?,?,?,?,?,?)",
                    (pk, t, e, BACKLIT, None, v, None, _crt(t, e)))
    pk += 1
    con.execute("insert into ZOBJECT values (?,?,?,?,?,?,?,?)",
                (pk, cutoff, cutoff, "/test/cutoff_marker", None, None, None, cutoff))
    con.commit()
    con.close()


def _u(y, m, d, H=0, M=0, S=0):
    return datetime(y, m, d, H, M, S).timestamp() - EPOCH_OFF


def selftest():
    ok = fail = 0
    msgs = []

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            msgs.append("PASS %s %s" % (name, detail))
        else:
            fail += 1
            msgs.append("FAIL %s %s" % (name, detail))

    td = tempfile.mkdtemp(prefix="kcau_")

    # --- T1 重叠区间不重复计时（含负控：朴素求和必须不等于合并后）---
    p = os.path.join(td, "t1.db")
    rows = [(_u(2026, 3, 1, 10, 0), _u(2026, 3, 1, 11, 0), "a"),
            (_u(2026, 3, 1, 10, 30), _u(2026, 3, 1, 11, 30), "a")]
    _fixture_db(p, app_rows=rows)
    r = analyze(p)
    naive = 3600 + 3600
    merged = 5400
    got = r["window"]["fg_hours_total"] * 3600
    check("T1.merge_dedup", abs(got - merged) < 1, "merged=%.0fs got=%.0fs" % (merged, got))
    check("NC1.naive_would_differ", abs(naive - merged) > 0, "naive=%ds vs merged=%ds" % (naive, merged))

    # --- T2 空日 → UNKNOWN 而非 0 ---
    p = os.path.join(td, "t2.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 10, 0), "a"),
                             (_u(2026, 3, 3, 9, 0), _u(2026, 3, 3, 10, 0), "a")])
    r = analyze(p)
    d2 = [x for x in r["days"] if x["day"] == "2026-03-02"][0]
    check("T2.empty_day_unknown", d2["fg_hours"] == "UNKNOWN", "got=%r" % d2["fg_hours"])

    # --- T3 晚于 cutoff 的区间不得计入 ---
    p = os.path.join(td, "t3.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 10, 0), "a"),
                             (_u(2026, 3, 1, 20, 0), _u(2026, 3, 1, 21, 0), "a")],
                cutoff=_u(2026, 3, 1, 12, 0))
    r = analyze(p)
    check("T3.after_cutoff_excluded", abs(r["window"]["fg_hours_total"] - 1.0) < 0.01,
          "hours=%.3f skipped=%s" % (r["window"]["fg_hours_total"], r["skipped"]))
    check("NC3.cutoff_counted", r["skipped"].get("app_after_cutoff") == 1,
          "skipped=%s" % r["skipped"])

    # --- T4 非正/空日期行跳过 ---
    p = os.path.join(td, "t4.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 9, 0), "a"),
                             (None, _u(2026, 3, 1, 9, 0), "b"),
                             (_u(2026, 3, 1, 10, 0), _u(2026, 3, 1, 10, 30), "c")])
    r = analyze(p)
    check("T4.bad_rows_skipped", r["skipped"].get("app_nonpositive") == 1 and
          r["skipped"].get("app_null_dates") == 1 and abs(r["window"]["fg_hours_total"] - 0.5) < 0.01,
          "skipped=%s hours=%.3f" % (r["skipped"], r["window"]["fg_hours_total"]))

    # --- T5 跨午夜切分 ---
    p = os.path.join(td, "t5.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 23, 30), _u(2026, 3, 2, 0, 30), "a")])
    r = analyze(p)
    m = {x["day"]: x["fg_hours"] for x in r["days"]}
    check("T5.midnight_split", m.get("2026-03-01") == 0.5 and m.get("2026-03-02") == 0.5, "got=%s" % m)

    # --- T6 通知来源取结构元数据 BUNDLEID + 去重 ---
    p = os.path.join(td, "t6.db")
    _fixture_db(p, notif_rows=[(_u(2026, 3, 1, 8, 0), "com.x", "ID1"),
                               (_u(2026, 3, 1, 8, 0), "com.x", "ID1"),
                               (_u(2026, 3, 1, 9, 0), "com.y", "ID2")])
    r = analyze(p)
    check("T6.notif_attr", r["notif_rank_raw"][0]["bundle"] == "com.x" and r["window"]["notif_raw_total"] == 3,
          "raw=%s" % r["notif_rank_raw"])
    check("T6.notif_dedup", r["window"]["notif_dedup_total"] == 2, "dedup=%d" % r["window"]["notif_dedup_total"])

    # --- T7 一致性检查：前台 ∩ 熄屏 ---
    p = os.path.join(td, "t7.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 10, 0), _u(2026, 3, 1, 11, 0), "a")],
                bl_rows=[(_u(2026, 3, 1, 10, 30), _u(2026, 3, 1, 10, 45), 0),
                         (_u(2026, 3, 1, 11, 0), _u(2026, 3, 1, 11, 30), 1)])
    r = analyze(p)
    check("T7.off_overlap", r["window"]["anomaly_intervals"] == 1 and
          abs(r["window"]["anomaly_off_overlap_h"] * 3600 - 900) < 1,
          "anom=%s" % r["window"]["anomaly_off_overlap_h"])
    # NC7: v=1 不得算熄屏
    p = os.path.join(td, "t7b.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 10, 0), _u(2026, 3, 1, 11, 0), "a")],
                bl_rows=[(_u(2026, 3, 1, 10, 30), _u(2026, 3, 1, 10, 45), 1)])
    r = analyze(p)
    check("NC7.v1_not_offscreen", r["window"]["anomaly_intervals"] == 0,
          "anom=%s" % r["window"]["anomaly_intervals"])

    # --- T8 空流 → 全 UNKNOWN，不得写 0 ---
    p = os.path.join(td, "t8.db")
    _fixture_db(p, notif_rows=[(_u(2026, 3, 1, 8, 0), "com.x", "ID1")])
    r = analyze(p)
    check("T8.no_app_all_unknown", r["window"]["days_with_app"] == 0 and r["window"]["fg_hours_total"] == 0
          and all(x["fg_hours"] == "UNKNOWN" for x in r["days"]),
          "days=%s" % [(x["day"], x["fg_hours"]) for x in r["days"]])

    # --- T9 确定性（两跑一致）---
    p = os.path.join(td, "t9.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 10, 0), "a"),
                             (_u(2026, 3, 2, 9, 0), _u(2026, 3, 2, 12, 0), "b")],
                notif_rows=[(_u(2026, 3, 1, 8, 0), "com.x", "ID1")])
    check("T9.deterministic", analyze(p) == analyze(p))

    # --- T10 只读自证：夹具 sha256 前后一致 ---
    p = os.path.join(td, "t10.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 10, 0), "a")])
    h0 = sha256(p)
    analyze(p)
    check("T10.readonly_fixture", sha256(p) == h0)

    # --- T11 合并的相邻（end==next start）不双计 ---
    p = os.path.join(td, "t11.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 10, 0), "a"),
                             (_u(2026, 3, 1, 10, 0), _u(2026, 3, 1, 11, 0), "a")])
    r = analyze(p)
    check("T11.adjacent_merge", abs(r["window"]["fg_hours_total"] - 2.0) < 0.01,
          "hours=%.3f" % r["window"]["fg_hours_total"])

    # --- T13 跨 cutoff 的区间：截到 cutoff，且不得误判成「晚于 cutoff 跳过」 ---
    p = os.path.join(td, "t13.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 11, 0), _u(2026, 3, 1, 13, 0), "a")],
                cutoff=_u(2026, 3, 1, 12, 0))
    r = analyze(p)
    got = r["window"]["fg_hours_total"]
    check("T13.straddle_clamped", abs(got - 1.0) < 0.01 and r["skipped"].get("app_after_cutoff", 0) == 0,
          "hours=%.3f skipped=%s" % (got, r["skipped"]))
    check("NC13.unclamped_would_be_2h", abs(got - 2.0) > 0.5,
          "raw=2.000h vs clamped=%.3fh" % got)

    # --- T12 焦点日默认跳过不完整末日 + UNKNOWN 日不进焦点 ---
    p = os.path.join(td, "t12.db")
    _fixture_db(p, app_rows=[(_u(2026, 3, 1, 9, 0), _u(2026, 3, 1, 10, 0), "a"),
                             (_u(2026, 3, 2, 9, 0), _u(2026, 3, 2, 11, 0), "b")],
                cutoff=_u(2026, 3, 3, 23, 0))
    r = analyze(p)
    check("T12.focus_day_full", r["focus_day"] == "2026-03-02", "focus=%s" % r["focus_day"])

    print("\n".join(msgs))
    print("selftest: %d/%d" % (ok, ok + fail))
    return 0 if fail == 0 else 1


# ------------------------------------------------------------------ 主入口
def main(argv=None):
    ap = argparse.ArgumentParser(description="kc /app/usage + /notification/usage 只读消费器")
    ap.add_argument("--kc", default=None, help="kc 快照副本路径（默认取最新 temp/kc_snapshots/*/knowledgeC.db）")
    ap.add_argument("--json", default=None, help="把结果 JSON 写到该路径")
    ap.add_argument("--emit-profile", default=None, metavar="DIR",
                    help="把「字段 + 溯源」JSON 写到 DIR/kc_app_usage_fields.json（供 memory/kc_profile 消费）")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--day", default=None, help="焦点日 YYYY-MM-DD（默认=最后一个完整日）")
    ap.add_argument("--readonly-proof", action="store_true", help="前后 sha256 自证（副本硬判/源库软判）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    db = a.kc or latest_kc()
    if not db or not os.path.exists(db):
        print("[ERROR] 找不到 kc 副本：%s" % db, file=sys.stderr)
        return 2
    if not os.path.normpath(db).startswith(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))):
        print("[WARN] 目标不在仓库内，仍按只读处理", file=sys.stderr)

    src = "/Users/cuip/Library/Application Support/Knowledge/knowledgeC.db"
    h0 = sha256(db)
    hs0 = sha256(src) if (a.readonly_proof and os.path.exists(src)) else None
    r = analyze(db, top=a.top, focus_day=a.day)
    h1 = sha256(db)
    proof = {"snapshot_before": h0, "snapshot_after": h1, "snapshot_unchanged": h0 == h1,
             "source_hashed": bool(hs0),
             "source_unchanged": (sha256(src) == hs0) if hs0 else None, "source": src}
    r["readonly_proof"] = proof

    print(text_report(r, a.top))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print("\n[json] %s" % a.json)
    if a.emit_profile:
        fields = {
            "schema": "kc_app_usage_fields/v1",
            "generated_by": "tools/kc_app_usage.py --emit-profile",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_dict": {"db": os.path.abspath(db), "cutoff_local": r["cutoff_local"]},
            "sql": {"app_usage": SQL_APP, "notification_usage": SQL_NOTIF, "backlit": SQL_BL, "cutoff": SQL_CUTOFF},
            "fields": [
                {"name": "fg_hours_by_day", "value": {d["day"]: d["fg_hours"] for d in r["days"]},
                 "unit": "小时", "source": "/app/usage ZENDDATE-ZSTARTDATE，per app 先并集再按本地日切分",
                 "note": "UNKNOWN=该日该流无行，与 0 不同"},
                {"name": "fg_hours_by_app_total", "value": {x["app"]: x["value"] for x in r["app_total_hours_rank"]},
                 "unit": "小时", "source": "/app/usage ZVALUESTRING 分组", "note": "仅 Top%d" % a.top},
                {"name": "notif_by_bundle", "value": {x["bundle"]: x["count"] for x in r["notif_rank_raw"]},
                 "unit": "次(投递)", "source": "/notification/usage ⨝ ZSTRUCTUREDMETADATA.BUNDLEID", "note": "raw 口径"},
                {"name": "notif_per_fg_hour", "value": r["notif_per_fg_hour"], "unit": "次/前台小时",
                 "source": "notif_raw_total / fg_hours_total", "note": "分母是前台时长上界，故为下界密度"},
                {"name": "fg_off_overlap_hours", "value": r["window"]["anomaly_off_overlap_h"], "unit": "小时",
                 "source": "/app/usage ∩ /display/isBacklit(v=0)", "note": "越大说明前台信号越不可信"},
            ],
            "window": r["window"],
        }
        os.makedirs(a.emit_profile, exist_ok=True)
        outp = os.path.join(a.emit_profile, "kc_app_usage_fields.json")
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False, indent=1)
        print("[profile] %s" % outp)
    if a.readonly_proof:
        print("[proof] snapshot unchanged=%s source unchanged=%s"
              % (proof["snapshot_unchanged"], proof["source_unchanged"]))
    return 0 if proof["snapshot_unchanged"] else 3


if __name__ == "__main__":
    sys.exit(main())
