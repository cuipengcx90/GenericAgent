#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sleep_source_crosscheck.py —— kc 熄屏(/display/isBacklit v=0) × pmset 睡眠窗口 逐日对账（只读）

背景（R425 第15批 C4，三路评审 9/6/9=8.00）：
  R415 把 knowledgeC 与 Chrome 采成本地快照，R420 把 /var/log/powermanagement/*.asl 采成 pmset 快照；
  三源此前**零消费者**。本器是三源快照的首个消费者：把「kc 熄屏小时数」与「pmset 系统睡眠小时数」
  按**本地日**逐日对齐，给出共同日期、两侧值、差值、方向，并显式列出「哪些比值可算 / 哪些必须先固定快照时点」。

口径（与 memory/kc_profile/profile.py 同源；该文件在 .gitignore 内、不入克隆，故此处复述并自校）：
  1. kc 只认 `/display/isBacklit` 且 `ZVALUEINTEGER == 0` 为熄屏；v=1 与「无行」（gap）**都不得**计入熄屏
     （profile.py §7 实测：亮屏段大量无状态行，用 v=1 求和会低估 ~70%）。
  2. kc 各流时长 = `ZENDDATE - ZSTARTDATE` 秒（R371 已证 ZVALUEDOUBLE/ZVALUEINTEGER 是位型垃圾）。
  3. 跨本地午夜的区间**必须切分到两日**（profile.py 按 clock-hour 分桶、不分日，故其小时分布不能直接当日值用）。
  4. pmset 只认 events.json 的 `windows[].secs`（= Entering Sleep → Wake 的配对窗口）；`darkwake` 不是睡眠、不计入。
  5. 缺席日期输出 `NO_DATA`，**绝不写成 0**（0 会与「真无睡眠」混淆）。

用法：
  python3 tools/sleep_source_crosscheck.py                          # 默认：live kc 副本 + 最新 pmset 快照
  python3 tools/sleep_source_crosscheck.py --kc <db> --pmset-dir <dir> [--json out.json]
  python3 tools/sleep_source_crosscheck.py --selftest               # 5 组夹具（含 3 负控）

只读：kc 走 `file:...?mode=ro`；pmset 只读 json；不写任何源文件、不改快照器、不接线定时。
"""

import argparse
import glob
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, date

EPOCH_OFF = 978307200  # 2001-01-01 → unix（kc 的 Apple epoch）
SQL_BL = ("select ZSTARTDATE t, ZENDDATE e, ZVALUEINTEGER v from ZOBJECT"
          " where ZSTREAMNAME='/display/isBacklit' order by ZSTARTDATE;")
SQL_APP = ("select ZSTARTDATE t, ZENDDATE e, ZVALUESTRING app from ZOBJECT"
           " where ZSTREAMNAME='/app/usage' order by ZSTARTDATE;")


# ---------------------------------------------------------------- 切分工具
def split_by_day(t0, t1):
    """把 [t0, t1) 按本地日边界切分 → {date: 秒数}。用于跨午夜区间分摊。"""
    out = defaultdict(float)
    cur = t0
    while cur < t1:
        nxt = min(t1, datetime.combine(cur.date() + timedelta(days=1), datetime.min.time()))
        out[cur.date()] += (nxt - cur).total_seconds()
        cur = nxt
    return out


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path), uri=True)


# ---------------------------------------------------------------- kc 侧
def kc_daily(path):
    """→ {date: {'off_h','usage_h','usage_events','backlit_rows'}}（按本地日切分）"""
    con = _ro(path)
    day = defaultdict(lambda: {"off_s": 0.0, "usage_s": 0.0, "usage_events": 0, "backlit_rows": 0})
    rows = con.execute(SQL_BL).fetchall()
    span_lo = span_hi = None
    for t, e, v in rows:
        t = datetime.fromtimestamp((t or 0) + EPOCH_OFF)
        e = datetime.fromtimestamp((e or 0) + EPOCH_OFF)
        span_lo = t if span_lo is None or t < span_lo else span_lo
        span_hi = e if span_hi is None or e > span_hi else span_hi
        day[t.date()]["backlit_rows"] += 1
        if int(v if v is not None else 0) == 0 and e > t:      # ★只有 v=0 计熄屏
            for d, s in split_by_day(t, e).items():
                day[d]["off_s"] += s
    for t, e, _app in con.execute(SQL_APP).fetchall():
        t = datetime.fromtimestamp((t or 0) + EPOCH_OFF)
        e = datetime.fromtimestamp((e or 0) + EPOCH_OFF)
        day[t.date()]["usage_events"] += 1
        if e > t:
            for d, s in split_by_day(t, e).items():
                day[d]["usage_s"] += s
    con.close()
    out = {}
    for d, v in day.items():
        out[d] = {"off_h": round(v["off_s"] / 3600.0, 3),
                  "usage_h": round(v["usage_s"] / 3600.0, 3),
                  "usage_events": v["usage_events"],
                  "backlit_rows": v["backlit_rows"]}
    return out, (span_lo, span_hi)


# ---------------------------------------------------------------- pmset 侧
def pmset_daily(dirpath):
    """→ ({date: {'sleep_h','windows'}}, meta)；只认 windows[].secs，darkwake 不计入。"""
    ev = json.load(open(os.path.join(dirpath, "events.json"), encoding="utf-8"))
    day = defaultdict(lambda: {"sleep_s": 0.0, "windows": 0})
    open_ = 0
    for w in ev.get("windows", []):
        t = datetime.strptime(w["enter"], "%Y-%m-%d %H:%M:%S")
        if not w.get("wake"):            # 未完窗口（日志尾部仍在睡）：不计入任何一天，单独计数上报
            open_ += 1
            continue
        e = datetime.strptime(w["wake"], "%Y-%m-%d %H:%M:%S")
        if e <= t:
            continue
        for d, s in split_by_day(t, e).items():
            day[d]["sleep_s"] += s
        day[t.date()]["windows"] += 1
    meta = {"summary": ev.get("summary", {}),
            "files": len(ev.get("files", [])),
            "open_windows_skipped": open_,
            "darkwake_total": sum(f.get("events", {}).get("darkwake", 0) for f in ev.get("files", []))}
    out = {d: {"sleep_h": round(v["sleep_s"] / 3600.0, 3), "windows": v["windows"]} for d, v in day.items()}
    return out, meta


# ---------------------------------------------------------------- 对账
def crosscheck(kc, pmset):
    """逐日对齐，返回 rows。共同日期 = 两侧都有任何状态行的日期。"""
    dates = sorted(set(kc) | set(pmset))
    rows = []
    for d in dates:
        k = kc.get(d)
        p = pmset.get(d)
        row = {"date": str(d)}
        # ★单侧缺数据时仍保留已有侧的数值（只判 NO_DATA，不把缺席侧当 0）
        if k is not None:
            row.update({"kc_off_h": k["off_h"], "kc_usage_h": k["usage_h"],
                        "kc_usage_events": k["usage_events"], "kc_backlit_rows": k["backlit_rows"]})
        if p is not None:
            row.update({"pmset_sleep_h": p["sleep_h"], "pmset_windows": p["windows"]})
        if k is None or p is None:
            row.update({"state": "NO_DATA", "missing": "kc" if k is None else "pmset"})
            rows.append(row)
            continue
        # kc 侧完全无状态行时也算 NO_DATA（避免把「无数据」当 0）
        if k["backlit_rows"] == 0 and p["windows"] == 0:
            row.update({"state": "NO_DATA", "missing": "both"})
            rows.append(row)
            continue
        diff = round(k["off_h"] - p["sleep_h"], 3)
        row.update({"state": "OK", "diff_h": diff,
                    "direction": ("kc>pmset" if diff > 0.05 else ("pmset>kc" if diff < -0.05 else "equal"))})
        rows.append(row)
    return rows


def kc_off_total(rows):
    """kc 熄屏小时合计（含只有 kc 一侧的日期）——与 profile.py 的 dark_total_hours 同口径自校用。"""
    return round(sum(r["kc_off_h"] for r in rows if "kc_off_h" in r), 3)


def calib_ok(total, expect, tol=0.05):
    """校准判据：本器 kc 熄屏合计须与外部口径（profile.py dark_total_hours）在 tol 内一致。"""
    return abs(total - expect) <= tol


BOUNDARY = [
    ("允许", "同一**快照时点**内比较 kc 熄屏小时与 pmset 睡眠小时（本器口径）", "两者都随快照变动；固定时点后比值才有意义"),
    ("允许", "把 pmset 睡眠视作「系统真实入睡」下界、kc 熄屏视作「屏幕关闭」上界", "口径不同；实测同一时点 5/5 天 kc > pmset（差 +0.67~+7.49h）⇒ 可作方向性上界，不可作换算"),
    ("禁止", "由 kc 熄屏小时**推断** pmset 睡眠小时（或反向）", "同一时点 5 天 kc 恒大于 pmset 但差值无稳定比例（+0.67~+7.49h）；旧记的「方向相反」值在本器口径不可复现（快照时点不同+kc 迟写回填）⇒ 无换算关系，属跨源外推"),
    ("禁止", "跨快照时点比较 kc 侧数值（如 09-25 快照 vs 09-26 live 副本）", "kc 有迟写回填 + 滚动删除（窗口在动）"),
    ("禁止", "把 kc gap（无状态行）算作亮屏或熄屏", "profile.py §7 实测 gap 内前台事件密度与亮屏段同量级"),
    ("禁止", "把 pmset darkwake 计作睡眠", "DarkWake 是短暂唤醒，不构成睡眠窗口"),
    ("禁止", "缺数据日写成 0", "0 与「无睡眠」不可区分 ⇒ 本器输出 NO_DATA"),
]


def render(kc_source, pmset_dir, kc, kc_span, pmset, pmset_meta, rows, kc_path):
    L = []
    L.append("=" * 88)
    L.append("kc 熄屏(/display/isBacklit v=0) × pmset 睡眠窗口 逐日对账（只读）")
    L.append("=" * 88)
    L.append("kc 源      : %s" % kc_path)
    L.append("kc 标签    : %s" % kc_source)
    L.append("kc isBacklit 窗口: %s → %s" % (kc_span[0], kc_span[1]) if kc_span[0] else "kc isBacklit 窗口: n/a")
    L.append("pmset 快照 : %s（%d 文件）" % (pmset_dir, pmset_meta["files"]))
    sm = pmset_meta["summary"]
    L.append("pmset 睡眠窗口总数 %s，睡眠秒数合计 %s，darkwake 合计 %d（不计入睡眠），未完窗口 %d（跳过）"
             % (sm.get("sleep_windows_total", "n/a"), sm.get("sleep_secs_total", "n/a"),
                pmset_meta["darkwake_total"], pmset_meta.get("open_windows_skipped", 0)))
    L.append("")
    L.append("日期        kc熄屏h  kc使用h  后台事件  pmset睡眠h  窗口数  差值(kc−pmset)  方向       状态")
    L.append("-" * 88)
    ok = 0
    for r in rows:
        if r["state"] != "OK":
            part = []
            if "kc_off_h" in r:
                part.append("kc熄屏 %.2fh" % r["kc_off_h"])
            if "pmset_sleep_h" in r:
                part.append("pmset睡眠 %.2fh" % r["pmset_sleep_h"])
            L.append("%s %-30s NO_DATA(缺 %s)%s"
                     % (r["date"], "", r["missing"],
                        ("   已有侧：" + " / ".join(part)) if part else ""))
            continue
        ok += 1
        L.append("%s %8.2f %8.2f %8d %11.2f %6d %14.2f  %-9s OK"
                 % (r["date"], r["kc_off_h"], r["kc_usage_h"], r["kc_usage_events"],
                    r["pmset_sleep_h"], r["pmset_windows"], r["diff_h"], r["direction"]))
    days_kc, days_pm = len(kc), len(pmset)
    common = len(set(kc) & set(pmset))
    L.append("-" * 88)
    L.append("kc 有数据日 %d / pmset 有数据日 %d / **共同日期 %d**（可判定 %d 天）"
             % (days_kc, days_pm, common, ok))
    L.append("kc 熄屏合计 %.2fh（口径自校：应等于 profile.py 同库的 dark_total_hours）" % kc_off_total(rows))
    L.append("")
    L.append("## 允许 / 禁止 推断清单")
    for verdict, claim, why in BOUNDARY:
        L.append("[%s] %s\n        理由：%s" % (verdict, claim, why))
    return "\n".join(L)


# ---------------------------------------------------------------- 自测
def _mk_kc(path, rows):
    """rows: [(start_str, end_str, value)] 本地时间字符串"""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("create table ZOBJECT (Z_PK integer primary key, ZSTREAMNAME text,"
                " ZSTARTDATE real, ZENDDATE real, ZVALUEINTEGER integer, ZVALUESTRING text,"
                " ZSTRUCTUREDMETADATA integer)")
    for s, e, v, kind in rows:
        t = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp() - EPOCH_OFF
        x = datetime.strptime(e, "%Y-%m-%d %H:%M:%S").timestamp() - EPOCH_OFF
        con.execute("insert into ZOBJECT (ZSTREAMNAME,ZSTARTDATE,ZENDDATE,ZVALUEINTEGER,ZVALUESTRING)"
                    " values (?,?,?,?,?)", (kind, t, x, v, "app" if kind == "/app/usage" else None))
    con.commit()
    con.close()


def _mk_pmset(dirpath, windows, files=None):
    os.makedirs(dirpath, exist_ok=True)
    ev = {"tool": "fixture", "source": "pmset", "snapshot_dir": dirpath,
          "known_boundary": "fixture", "files": files or [], "windows": windows,
          "summary": {"windows": len(windows), "sleep_secs": sum(w["secs"] for w in windows)}}
    json.dump(ev, open(os.path.join(dirpath, "events.json"), "w", encoding="utf-8"))


def selftest():
    tmp = tempfile.mkdtemp(prefix="ssc_selftest_")
    ok = True
    nchk = [0]

    def check(name, cond, detail=""):
        nonlocal ok
        nchk[0] += 1
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
        ok = ok and bool(cond)

    # T1 跨午夜窗口必须切到两日
    d1 = os.path.join(tmp, "pm_t1")
    _mk_pmset(d1, [{"enter": "2026-01-01 23:00:00", "wake": "2026-01-02 01:00:00",
                    "reason": "Clamshell Sleep", "secs": 7200}])
    pm, _ = pmset_daily(d1)
    check("T1 pmset 跨午夜切分", abs(pm[date(2026, 1, 1)]["sleep_h"] - 1.0) < 1e-9
          and abs(pm[date(2026, 1, 2)]["sleep_h"] - 1.0) < 1e-9, json.dumps({str(k): v for k, v in pm.items()}))

    # T2 负控：只有 darkwake、无窗口 ⇒ 睡眠 0，且不得凭空产生日
    d2 = os.path.join(tmp, "pm_t2")
    _mk_pmset(d2, [], files=[{"file": "x.asl", "events": {"darkwake": 9, "sleep_enter": 0}}])
    pm2, meta2 = pmset_daily(d2)
    check("T2 负控 darkwake 不计睡眠", pm2 == {} and meta2["darkwake_total"] == 9,
          "windows=%s darkwake=%s" % (pm2, meta2["darkwake_total"]))

    # T3 负控：kc v=1（亮屏）不得计入熄屏；v=0 计入
    k1 = os.path.join(tmp, "kc_t3.db")
    _mk_kc(k1, [("2026-01-01 03:00:00", "2026-01-01 05:00:00", 0, "/display/isBacklit"),
                ("2026-01-01 09:00:00", "2026-01-01 19:00:00", 1, "/display/isBacklit")])
    kc, _ = kc_daily(k1)
    check("T3 负控 v=1 不计熄屏", abs(kc[date(2026, 1, 1)]["off_h"] - 2.0) < 1e-9
          and kc[date(2026, 1, 1)]["backlit_rows"] == 2, json.dumps({str(k): v for k, v in kc.items()}))

    # T4 缺数据日必须 NO_DATA，不得写 0
    rows = crosscheck(kc, {date(2026, 1, 2): {"sleep_h": 0.0, "windows": 0}})
    r2 = [r for r in rows if r["date"] == "2026-01-02"][0]
    check("T4 负控 缺数据≠0", r2["state"] == "NO_DATA" and "kc_off_h" not in r2, json.dumps(r2, ensure_ascii=False))

    # T5 kc /app/usage 时长也按日切分且事件计数正确
    k2 = os.path.join(tmp, "kc_t5.db")
    _mk_kc(k2, [("2026-01-01 23:30:00", "2026-01-02 00:30:00", 0, "/app/usage")])
    kc2, _ = kc_daily(k2)
    check("T5 usage 跨午夜切分", abs(kc2[date(2026, 1, 1)]["usage_h"] - 0.5) < 1e-9
          and abs(kc2[date(2026, 1, 2)]["usage_h"] - 0.5) < 1e-9
          and kc2[date(2026, 1, 1)]["usage_events"] == 1, json.dumps({str(k): v for k, v in kc2.items()}))

    # T6 负控：篡改判据必须被检出（用 v=1 也算熄屏的错误口径 → 结果必须与正确口径不同）
    def wrong_off(path):
        con = _ro(path)
        tot = 0.0
        for t, e, v in con.execute(SQL_BL).fetchall():
            if e and e > (t or 0):
                tot += (e - t)
        con.close()
        return tot
    good = sum(x["off_h"] for x in kc.values())
    bad = wrong_off(k1) / 3600.0
    check("T6 负控 错误口径必被区分", abs(bad - good) > 1e-6, "good=%.3f bad=%.3f" % (good, bad))

    # T7 负控：校准闸门必须能判出「口径漂移」（外部口径对不上时必须失败）
    rows7 = crosscheck({date(2026, 1, 1): {"off_h": 2.0, "usage_h": 0.0, "usage_events": 0, "backlit_rows": 2}},
                       {date(2026, 1, 1): {"sleep_h": 1.0, "windows": 1}})
    tot7 = kc_off_total(rows7)
    check("T7 负控 校准闸门可判漂移", abs(tot7 - 2.0) < 1e-9
          and calib_ok(tot7, 2.0) and (not calib_ok(tot7, 3.0)),
          "tot=%.3f ok@2.0=%s ok@3.0=%s" % (tot7, calib_ok(tot7, 2.0), calib_ok(tot7, 3.0)))

    print("\nselftest: %d/%d PASS（夹具目录 %s）" % (nchk[0] if ok else 0, nchk[0], tmp))
    return 0 if ok else 1

# ---------------------------------------------------------------- main
def latest_pmset_dir():
    cands = sorted(glob.glob("temp/pmset_snapshots/*"))
    return cands[-1] if cands else None


def main():
    ap = argparse.ArgumentParser(description="kc 熄屏 × pmset 睡眠 逐日对账（只读）")
    ap.add_argument("--kc", default="temp/kc_live_crosscheck/knowledgeC.db",
                    help="knowledgeC.db（默认 live 副本；快照请显式传 temp/kc_snapshots/<date>/knowledgeC.db）")
    ap.add_argument("--pmset-dir", default=None, help="pmset 快照目录（默认取 temp/pmset_snapshots/ 最新）")
    ap.add_argument("--json", default=None, help="把结果写到该 json")
    ap.add_argument("--expect-kc-off-total", type=float, default=None,
                    help="校准闸门：kc 熄屏合计须等于该值（±0.05h），用于与外部口径(profile.py dark_total_hours)对账")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    kc_path = a.kc
    if not os.path.exists(kc_path):
        sys.exit("kc 库不存在：%s" % kc_path)
    pdir = a.pmset_dir or latest_pmset_dir()
    if not pdir or not os.path.exists(os.path.join(pdir, "events.json")):
        sys.exit("pmset 快照目录无效：%s" % pdir)
    label = "live 副本（含 -wal，滚动窗口）" if "kc_live_crosscheck" in kc_path else ("快照 %s" % kc_path.split("kc_snapshots/")[-1].split("/")[0])
    kc, kc_span = kc_daily(kc_path)
    pm, pm_meta = pmset_daily(pdir)
    rows = crosscheck(kc, pm)
    txt = render(label, pdir, kc, kc_span, pm, pm_meta, rows, kc_path)
    print(txt)
    tot = kc_off_total(rows)
    if a.expect_kc_off_total is not None:
        if not calib_ok(tot, a.expect_kc_off_total):
            print("\n[CALIB-FAIL] kc 熄屏合计 %.3fh ≠ 期望 %.3fh（口径漂移）" % (tot, a.expect_kc_off_total))
            return 2
        print("\n[CALIB-OK] kc 熄屏合计 %.3fh == 期望 %.3fh（±0.05h）" % (tot, a.expect_kc_off_total))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        json.dump({"kc_source": kc_path, "kc_label": label, "pmset_dir": pdir,
                   "kc_span": [str(kc_span[0]), str(kc_span[1])] if kc_span[0] else None,
                   "pmset_meta": pm_meta, "rows": rows,
                   "counts": {"kc_days": len(kc), "pmset_days": len(pm),
                              "common_days": len(set(kc) & set(pm)),
                              "ok_days": sum(1 for r in rows if r["state"] == "OK")},
                   "boundary": [{"verdict": v, "claim": c, "why": w} for v, c, w in BOUNDARY]},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n[json] %s" % a.json)


if __name__ == "__main__":
    sys.exit(main() or 0)
