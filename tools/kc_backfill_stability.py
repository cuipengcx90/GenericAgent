#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kc「迟写回填」稳定期实测（R462，第19批 C4 / TODO L173）——只读。

★本器把「同一天跨快照的差异」分成两类，禁止混谈：
    (1) 迟写回填 BACKFILL：该日原始行**只增不减**（迟到写入）；
    (2) 按龄剔除 EVICTION_HEAD：kc 约 24 天滚动窗口，最老日会被**从头部逐条删掉**
        （只减不增，且被删行的 start 全早于保留下来的最老行）⇒ 这是**数据不可用**，
        不是回填；把它当回填会得到「读数变少=回填」的错误结论。
    `--strict` 只对真变化（BACKFILL / 无法解释的减少 / 改动）返回 rc=1；EVICTION_ONLY 不告警。

背景（为什么需要本器）
  kc 源库对同一「已完成日」的读数会随快照时点变化（迟写回填）。旧记录里有一条
  「09-22 熄屏 0.90h → 5.84h → 11.78h」被当作回填证据，但那三天量分别取自
  不同**快照时点**，且 09-22 当时**尚未结束** ⇒ 两种效应被混在一起：
    (a) 截断效应：快照落在当日内，则当日数据天然不全（与「回填」无关）；
    (b) 迟写回填：日已结束，但行仍在随后到达。
  本器把两者显式分离，并对「已完成日」逐日给出跨快照 Δ / rel，据此判定稳定期。

口径（一律复用既有实现，禁新造）
  - 前台时长 fg_hours / 通知计数：直接取 `tools/kc_app_usage.py` 的 `analyze()` 日序列
    （`days[].fg_hours / app_rows / notif_raw / notif_dedup`），逐字同源。
  - 熄屏时长 dark_hours：`/display/isBacklit` 取 v=0 区间 → 复用 kc_app_usage 的
    `merge()` + `split_by_day()`（与 profile.py「只有 v=0 可判熄屏」同判据）。
    ★自校：Σ(按日 dark) 必须等于 kc_app_usage 自己算的 `window.off_hours`（同一批区间，
    切日不改总量）——对不上即口径漂移，直接判 FAIL。
  - 时点：快照时点取各 `manifest.json` 的**最后一个 revision** 的 `ts`（非目录名、
    非 mtime），并与副本 sha256 对账（manifest 记录的 sha ≠ 实算 sha ⇒ 报 INTEGRITY_FAIL）。

判据
  日 D 在快照 S 中：`day_end(D) <= ts(S)` ⇒ COMPLETED；`ts(S) < day_end(D)` ⇒ TRUNCATED。
  只有「两快照均为 COMPLETED」的配对被纳入稳定期判定；TRUNCATED 一律出表但不参与判定
  （否则会把截断效应当成回填）。零值/缺失日判 UNDECIDABLE，**不得计入「稳定」**。

rc 约定（fail-closed）
  0 = 判定完成；1 = `--strict` 且有日仍在变（不稳定）；2 = 证据不足/输入错误（无可用配对等）。

用法
  python3 tools/kc_backfill_stability.py points                 # 快照时点清单
  python3 tools/kc_backfill_stability.py matrix                 # 日 × 快照 矩阵（含回填/截断标注）
  python3 tools/kc_backfill_stability.py stability [--strict] [--json PATH] [--tol-pct 1.0]
  python3 tools/kc_backfill_stability.py --readonly-proof stability
  python3 tools/kc_backfill_stability.py --selftest
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta

DEFAULT_GLOB = "temp/kc_snapshots/*/knowledgeC.db"
LIVE_MARKER = "Application Support" + "/Knowledge"   # 源库路径片段：本器源码中禁止出现（只读自证用）
                                              # ★故意拆开拼接：本行自身不得包含该完整片段，否则 R2 自检假 FAIL


# ---------------------------------------------------------------- 复用既有口径
def load_kc_app_usage(root):
    """按路径加载 tools/kc_app_usage.py（不改动其源码，只 import 复用）。"""
    p = os.path.join(root, "tools", "kc_app_usage.py")
    spec = importlib.util.spec_from_file_location("kc_app_usage_shared", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(buf)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


# ---------------------------------------------------------------- 快照注册
def snapshot_points(root, glob_pat=DEFAULT_GLOB):
    """枚举快照副本 → [{dir, db, date, ts, sha256, sha_in_manifest, objects, integrity}]"""
    import glob as _glob
    pat = glob_pat if os.path.isabs(glob_pat) else os.path.join(root, glob_pat)
    out = []
    for db in sorted(_glob.glob(pat)):
        d = os.path.dirname(db)
        mf = os.path.join(d, "manifest.json")
        rec = {"dir": os.path.abspath(d), "db": os.path.abspath(db),
               "date": os.path.basename(d), "ts": None, "sha256": sha256(db),
               "sha_in_manifest": None, "objects": None, "revisions": 0,
               "manifest": os.path.abspath(mf) if os.path.exists(mf) else None}
        if rec["manifest"]:
            try:
                m = json.load(open(rec["manifest"], encoding="utf-8"))
                revs = m.get("revisions") or []
                rec["revisions"] = len(revs)
                if revs:
                    last = revs[-1]
                    rec["ts"] = last.get("ts")
                    rec["sha_in_manifest"] = last.get("sha256")
                    rec["objects"] = (last.get("counts") or {}).get("objects")
            except Exception as e:                      # noqa: BLE001
                rec["manifest_error"] = str(e)
        rec["integrity"] = ("ok" if (rec["sha_in_manifest"] is None
                                     or rec["sha_in_manifest"] == rec["sha256"]) else "MISMATCH")
        if rec["ts"]:
            rec["ts_dt"] = datetime.strptime(rec["ts"], "%Y-%m-%d %H:%M:%S")
        out.append(rec)
    return out


# ---------------------------------------------------------------- 单快照度量
def dark_by_day(kau, db_path, cutoff=None):
    """v=0 熄屏区间 → 按日秒数（复用 kc_app_usage.merge/split_by_day）。"""
    con = kau._ro(db_path)
    rows = con.execute(kau.SQL_BL).fetchall()
    con.close()
    iv = []
    for t, e, v in rows:
        if t is None or e is None or e <= t:
            continue
        if int(v if v is not None else 0) == 0:
            iv.append((t, e))
    return kau.split_by_day(kau.merge(iv))


def app_head_day(kau, db_path):
    """该快照 `/app/usage` 最早一行的起点所在**本地日**（= 滚动窗口头）。

    用途：判断「某日在晚快照里整日消失」是否合法——若该日 **早于** 窗口头，
    那是 kc 按龄滚动剔除的正常结果；若 **不早于** 窗口头却整日消失，
    则属无法用剔除解释的数据变动，必须抓出来（否则整日删除会被判 STABLE）。
    """
    con = kau._ro(db_path)
    try:
        mn = con.execute("select min(ZSTARTDATE) from ZOBJECT where ZSTREAMNAME='/app/usage'"
                         ).fetchone()[0]
    finally:
        con.close()
    if mn is None:
        return None
    return datetime.fromtimestamp(mn + KC_EPOCH_OFFSET).strftime("%Y-%m-%d")


def measure(kau, db_path):
    """单快照逐日量：fg/notif 直接取 analyze() 日序列，dark 由上述复用函数合成并自校和。"""
    r = kau.analyze(db_path, top=1)
    days = {x["day"]: x for x in r["days"]}
    dark = dark_by_day(kau, db_path)
    dark_h = {str(d): s / 3600.0 for d, s in dark.items()}
    off_hours = r["window"].get("off_hours", 0.0)
    reconcile = round(sum(dark_h.values()) - off_hours, 4)
    return {"db": r["db"], "cutoff_local": r["cutoff_local"], "days": days,
            "dark_hours": dark_h, "off_hours_reported": off_hours,
            "dark_reconcile_h": reconcile, "window": r["window"]}


def day_end(dstr):
    """本地日 D 的结束时刻（次日 00:00）。"""
    d0 = datetime.strptime(dstr, "%Y-%m-%d")
    return d0 + timedelta(days=1)


# ---------------------------------------------------------------- 变更分类
KC_EPOCH_OFFSET = 978307200          # kc 时间戳以 Apple 参考日(2001-01-01 UTC) 起算


def day_rows(kau, db_path, dstr):
    """本地日 D 内 /app/usage 的**原始行**集合 {(app, start, end)}（已裁到当日边界、未去重）。

    用途：跨快照比较**行的增减**，用来区分「迟写回填（只增）」与「按龄剔除（只减）」。
    """
    d0 = datetime.strptime(dstr, "%Y-%m-%d")
    a = d0.timestamp() - KC_EPOCH_OFFSET
    b = day_end(dstr).timestamp() - KC_EPOCH_OFFSET
    con = kau._ro(db_path)
    try:
        rows = con.execute(
            "select ZSTARTDATE t, ZENDDATE e, ZVALUESTRING app from ZOBJECT"
            " where ZSTREAMNAME='/app/usage' and ZSTARTDATE < ? and ZENDDATE > ?",
            (b, a)).fetchall()
    finally:
        con.close()
    return {(r[2], max(r[0], a), min(r[1], b)) for r in rows if r[1] > r[0]}


def classify_change(earlier, later, boundary_slack=1.0):
    """把「同一天跨两快照的原始行差异」分类（fail-closed：判不准就报 UNEXPLAINED，不猜）。

    SAME              无增无删
    BACKFILL          只增不减 ⇒ 迟到写入（真回填）
    EVICTION_HEAD     只减不增，且被删行的 start 全部不晚于保留下来的最老行起点 ⇒ 按龄剔除
    SHRINK_UNEXPLAINED 只减不增但删到了切割点之后的（较新的）行 ⇒ 不允许当剔除
    SHRINK_ALL        整日行全没了
    REWRITE           有增有删 ⇒ 改写

    边界宽容口径（★R462 实测订正）：宽容必须**绑定在时间轴上**——只承认「起点与切割点同刻
    （±boundary_slack 秒）」的边界行，而**不是**「总共≤N 条行」。旧版按行数宽容（≤1 条）会把
    「中段被删掉 1 条长行」误判成 EVICTION_HEAD（实测：删 1 条 0.314h 的 ghostty 行 ⇒ 报
    EVICTION_ONLY 假阴性）；改成秒级判据后同一篡改被抓为 SHRINK_UNEXPLAINED。
    boundary_slack 单位=秒（默认 1.0，仅吸收浮点/裁剪的 1 秒内偏差）。
    """
    added, removed = later - earlier, earlier - later
    out = {"added_rows": len(added), "removed_rows": len(removed),
           "kept_rows": len(earlier & later)}
    if not added and not removed:
        out["kind"] = "SAME"
        return out
    if added and not removed:
        out["kind"] = "BACKFILL"
        return out
    if removed and not added:
        kept_starts = [r[1] for r in (earlier & later)]
        if not kept_starts:
            out["kind"] = "SHRINK_ALL"
            return out
        cut = min(kept_starts)                       # 仍保留的最老行的起点
        after = [s for s in (r[1] for r in removed) if s > cut + boundary_slack]
        out["cut_ts"] = cut
        out["removed_after_cut"] = len(after)        # 应为 0；边界行(与 cut 同刻)由秒级宽容吸收
        out["kind"] = ("EVICTION_HEAD" if not after else "SHRINK_UNEXPLAINED")
        return out
    out["kind"] = "REWRITE"
    return out


# ---------------------------------------------------------------- 矩阵 / 稳定期
def build_matrix(points, measures):
    """日 × 快照：值 + 状态标签（COMPLETED / TRUNCATED / ABSENT）。"""
    days = sorted({d for m in measures.values() for d in m["days"]})
    rows = []
    for d in days:
        e = day_end(d)
        cell = {}
        for pt in points:
            m = measures[os.path.abspath(pt["db"])]
            ts = pt.get("ts_dt")
            if ts is None:
                cell[pt["date"]] = {"state": "NO_TS"}
                continue
            if e > ts:                                   # 快照落在当日内 ⇒ 截断
                state = "TRUNCATED"
            else:
                state = "COMPLETED"
            dd = m["days"].get(d)
            fg = dd.get("fg_hours") if dd else "UNKNOWN"
            if fg == "UNKNOWN" or fg is None:
                cell[pt["date"]] = {"state": state if state == "TRUNCATED" else "ABSENT",
                                    "fg_hours": None, "note": "无前台行(UNKNOWN)"}
                continue
            cell[pt["date"]] = {
                "state": state, "fg_hours": fg,
                "app_rows": dd.get("app_rows"),
                "notif_raw": dd.get("notif_raw"),
                "notif_dedup": dd.get("notif_dedup"),
                "dark_hours": round(m["dark_hours"].get(d, 0.0), 3),
                "age_days": round((ts - e).total_seconds() / 86400.0, 2),
            }
        rows.append({"day": d, "cells": cell})
    return rows


def stability(points, measures, tol_pct=1.0, kau=None):
    """逐日跨快照配对 → Δ/rel；只有 双 COMPLETED 才判，TRUNCATED/ABSENT/零值一律出表不判。

    对判定为 CHANGING 的日，再做一次**原始行级**分类（BACKFILL vs EVICTION_HEAD），
    因为「读数变少」在 kc 上多半是按龄剔除而非回填。
    """
    if kau is None:
        kau = load_kc_app_usage(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    rows = build_matrix(points, measures)
    pts = [p for p in points if p.get("ts_dt")]
    db_by_date = {p["date"]: p["db"] for p in pts}
    heads = {p["date"]: app_head_day(kau, p["db"]) for p in pts}   # 各快照滚动窗口头(本地日)
    order = {p["date"]: i for i, p in enumerate(pts)}              # 时点先后(pts 已按 ts 升序)
    res = {"tol_pct": tol_pct, "days": [], "evaluated_days": 0, "changing_days": [],
           "changing_kinds": {}, "changing_days_real": [],
           "invalid": [], "max_age_days": None}
    for row in rows:
        d = row["day"]
        obs = []
        for p in pts:
            c = row["cells"][p["date"]]
            obs.append({"snap": p["date"], "ts": p["ts"], "state": c.get("state"),
                        "fg": c.get("fg_hours"), "dark": c.get("dark_hours"),
                        "notif": c.get("notif_dedup"), "age_days": c.get("age_days")})
        # ★整日消失：晚快照该日 ABSENT（无任何前台行），但更早快照有数据，且该日尚未滚出窗口头
        missing = []
        for o in obs:
            if o["state"] != "ABSENT" or o["fg"] is not None:
                continue
            head = heads.get(o["snap"])
            if head is None or d < head:
                continue                       # 早于该快照窗口头 ⇒ 按龄滚动剔除，合法
            i = order.get(o["snap"])
            if i is None:
                continue
            earlier = [x for x in obs if x["state"] == "COMPLETED" and x["fg"] is not None
                       and order.get(x["snap"], 1 << 30) < i]
            if earlier:
                missing.append({"day": d, "snap": o["snap"], "head_day": head,
                                "last_present_snap": earlier[-1]["snap"],
                                "last_fg_h": earlier[-1]["fg"]})
        rec_missing = missing               # 供 rec 落表
        usable = [o for o in obs if o["state"] == "COMPLETED" and o["fg"] is not None]
        usable.sort(key=lambda o: o["age_days"])
        pairs = []
        for a, b in zip(usable, usable[1:]):
            dfg = round(b["fg"] - a["fg"], 3)
            rel = (dfg / a["fg"] * 100.0) if a["fg"] else None
            ddk = None
            if a["dark"] is not None and b["dark"] is not None:
                ddk = round(b["dark"] - a["dark"], 3)
            dn = None
            if a["notif"] is not None and b["notif"] is not None and \
                    a["notif"] != "UNKNOWN" and b["notif"] != "UNKNOWN":
                dn = b["notif"] - a["notif"]
            changed = bool(rel is not None and abs(rel) > tol_pct)
            if rel is None and dfg:
                changed = True
            pairs.append({"from": a["snap"], "to": b["snap"],
                          "age_from": a["age_days"], "age_to": b["age_days"],
                          "delta_fg_h": dfg, "rel_pct": None if rel is None else round(rel, 3),
                          "delta_dark_h": ddk, "delta_notif": dn, "changed": changed})
        rec = {"day": d, "obs": obs, "pairs": pairs, "missing": rec_missing,
               "usable_obs": len(usable),
               "verdict": ("CHANGING" if (rec_missing or any(p["changed"] for p in pairs))
                           else ("NO_PAIR" if len(usable) < 2 else "STABLE"))}
        if rec_missing:
            res["changing_days"].append(d)
            res["changing_kinds"][d] = {"kind": "MISSING_DAY", "missing": rec_missing[0]}
            res["changing_days_real"].append(d)
        if len(usable) < 2 and not rec_missing:
            res["invalid"].append({"day": d, "reason": "usable_obs<2"})
        else:
            res["evaluated_days"] += 1
            if rec["verdict"] == "CHANGING" and d not in res["changing_kinds"]:
                res["changing_days"].append(d)
                # 行级分类：该日「最早→最晚」这一对快照的原始行差异
                ch = next((p for p in pairs if p["changed"]), None)
                try:
                    cls = classify_change(day_rows(kau, db_by_date[ch["from"]], d),
                                          day_rows(kau, db_by_date[ch["to"]], d))
                    cls.update({"from": ch["from"], "to": ch["to"]})
                except Exception as exc:                     # 判不了就明说，不猜
                    cls = {"kind": "UNKNOWN", "error": str(exc)[:120],
                           "from": ch["from"] if ch else None,
                           "to": ch["to"] if ch else None}
                res["changing_kinds"][d] = cls
                if cls["kind"] not in ("EVICTION_HEAD", "SHRINK_ALL"):
                    res["changing_days_real"].append(d)
        if usable:
            res["max_age_days"] = max(res.get("max_age_days") or 0,
                                      max(o["age_days"] for o in usable))
        res["days"].append(rec)
    # 截断面（与回填分离）：快照当日 vs 该日最终值
    trunc = []
    for row in rows:
        cs = [(p["date"], row["cells"][p["date"]]) for p in pts]
        finals = [c for _, c in cs if c.get("state") in ("COMPLETED",) and c.get("fg_hours") is not None]
        if not finals:
            continue
        for name, c in cs:
            if c.get("state") == "TRUNCATED" and c.get("fg_hours") is not None:
                fin = finals[-1]["fg_hours"]
                trunc.append({"day": row["day"], "snap": name,
                              "fg_truncated_h": c["fg_hours"], "fg_final_h": fin,
                              "pct_of_final": round(100.0 * c["fg_hours"] / fin, 1) if fin else None})
    res["truncated_rows"] = trunc
    res["verdict"] = ("NO_EVALUABLE_DAY" if res["evaluated_days"] == 0 else
                      ("STABLE" if not res["changing_days"] else
                       ("CHANGING_REAL" if res["changing_days_real"] else "EVICTION_ONLY")))
    return res


# ---------------------------------------------------------------- 输出
def print_points(points):
    print("快照时点（ts 取 manifest 最后一 revision；sha 对账副本实算）")
    print("%-12s %-20s %10s %12s %9s %s" % ("date", "ts", "bytes", "objects", "revisions", "integrity"))
    for p in points:
        b = os.path.getsize(p["db"]) if os.path.exists(p["db"]) else -1
        print("%-12s %-20s %10d %12s %9d %s" % (p["date"], p["ts"], b, p["objects"],
                                                p["revisions"], p["integrity"]))


def print_matrix(rows, points):
    names = [p["date"] for p in points]
    print("日 × 快照 矩阵（fg=前台小时 / dark=熄屏小时 / 状态 标记截断与缺失）")
    hdr = "day        " + "".join("%-32s" % n for n in names)
    print(hdr)
    for r in rows:
        line = "%-11s" % r["day"]
        for n in names:
            c = r["cells"][n]
            if c.get("fg_hours") is None:
                line += "%-32s" % ("[%s]" % c.get("state"))
            else:
                line += "%-32s" % ("fg %.2f dark %.2f %s" % (c["fg_hours"], c["dark_hours"], c["state"][:4]))
        print(line)


def print_stability(st):
    print("稳定期判定（tol=%.2f%%；仅 双 COMPLETED 配对参与）" % st["tol_pct"])
    for r in st["days"]:
        if not r["pairs"]:
            continue
        print(" %s  %s  usable=%d" % (r["day"], r["verdict"], r["usable_obs"]))
        for p in r["pairs"]:
            print("     %s→%s age %.2f→%.2f d  Δfg=%+.3fh rel=%s%% Δdark=%s Δnotif=%s %s"
                  % (p["from"], p["to"], p["age_from"], p["age_to"], p["delta_fg_h"],
                     p["rel_pct"], p["delta_dark_h"], p["delta_notif"],
                     "CHANGED" if p["changed"] else "same"))
    print(" 评估日数=%d 变动的日=%s 最大滞后=%.2f 天  判定=%s"
          % (st["evaluated_days"], st["changing_days"] or "-",
             st["max_age_days"] or 0, st["verdict"]))
    if st.get("changing_kinds"):
        print(" 变更分类（原始行级；EVICTION_HEAD=按龄剔除，非回填）：")
        for dd, c in sorted(st["changing_kinds"].items()):
            if c.get("kind") == "MISSING_DAY":
                m = c["missing"]
                print("   %s  %-18s 整日消失：晚快照 %s 无任何前台行（窗口头=%s，"
                      "更早快照 %s 尚有 %.2fh）"
                      % (dd, c["kind"], m["snap"], m["head_day"],
                         m["last_present_snap"], m["last_fg_h"]))
                continue
            tail = ("  晚于切割点仍被删=%d" % c["removed_after_cut"]
                    if c.get("removed_after_cut") is not None else "")
            print("   %s  %-18s added=%d removed=%d kept=%d%s"
                  % (dd, c["kind"], c.get("added_rows", -1), c.get("removed_rows", -1),
                     c.get("kept_rows", -1), tail))
    if st["truncated_rows"]:
        print(" 截断面（快照落在当日内，与回填无关；不进判定）：")
        for t in st["truncated_rows"]:
            print("   %s @%s fg=%.2fh = 该日最终值的 %s%%" % (t["day"], t["snap"],
                                                            t["fg_truncated_h"], t["pct_of_final"]))


# ---------------------------------------------------------------- 自测
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

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kau = load_kc_app_usage(root)
    td = tempfile.mkdtemp(prefix="kcbk_")
    u = kau._u

    # 夹具：A=早快照(2026-03-04 12:00，day4 被截断)，B=晚快照(2026-03-05 09:00)
    def mk(name, ts, extra_app=(), extra_bl=(), extra_notif=(), skip_app=(), files=None):
        d = os.path.join(td, name)
        os.makedirs(d, exist_ok=True)
        db = os.path.join(d, "knowledgeC.db")
        if files is None:
            kau._fixture_db(db, app_rows=extra_app, notif_rows=extra_notif, bl_rows=extra_bl)
        else:
            with open(db, "wb") as f:
                f.write(files)
        json.dump({"snapshot": "knowledgeC.db", "date": name,
                   "revisions": [{"ts": ts, "sha256": sha256(db), "bytes": os.path.getsize(db),
                                  "counts": {"objects": None}}]},
                  open(os.path.join(d, "manifest.json"), "w"))
        return db

    # 公共行：day1 1h、day2 1h、day3 2h（A/B 同）
    base_app = [(u(2026, 3, 1, 10), u(2026, 3, 1, 11), "X"),
                (u(2026, 3, 2, 9), u(2026, 3, 2, 10), "X"),
                (u(2026, 3, 3, 9), u(2026, 3, 3, 10), "X"),
                (u(2026, 3, 3, 20), u(2026, 3, 3, 21), "X")]
    base_bl = [(u(2026, 3, 1, 23), u(2026, 3, 1, 23, 30), 0)]
    base_notif = [(u(2026, 3, 2, 9, 5), "com.x", "i1"), (u(2026, 3, 2, 9, 6), "com.x", "i2")]
    # A：day4 仅 1h（截断）；B：day4 补到 2h（回填）+ day2 追加 1h + day3 删 1h + dark/notif 追加
    DROP3 = (u(2026, 3, 3, 20), u(2026, 3, 3, 21), "X")   # 仅 B 缺此行 ⇒ 制造「行被删」反向用例
    A_app = base_app + [(u(2026, 3, 4, 10), u(2026, 3, 4, 11), "X")]
    B_app = [r for r in base_app if r != DROP3] + [
        (u(2026, 3, 2, 14), u(2026, 3, 2, 15), "X"),      # 回填：day2 +1h
        (u(2026, 3, 4, 10), u(2026, 3, 4, 11), "X"),
        (u(2026, 3, 4, 15), u(2026, 3, 4, 16), "X"),      # day4 补齐（对 A 属截断，不进判定）
        # ★03-07 只有 B 有行：使 03-05/03-06 成为范围内的「无行日(UNKNOWN)」
        (u(2026, 3, 7, 10), u(2026, 3, 7, 11), "X")]
    B_bl = base_bl + [(u(2026, 3, 2, 13), u(2026, 3, 2, 14), 0)]
    B_notif = base_notif + [(u(2026, 3, 2, 9, 7), "com.x", "i3")]
    dbA = mk("2026-03-04", "2026-03-04 12:00:00", A_app, base_bl, base_notif)
    dbB = mk("2026-03-08", "2026-03-08 09:00:00", B_app, B_bl, B_notif)

    pts = snapshot_points(td, "*/knowledgeC.db")
    check("N1 points=2 且 ts 取 manifest", len(pts) == 2 and pts[0]["ts"] == "2026-03-04 12:00:00")
    check("N2 integrity 对账一致", all(p["integrity"] == "ok" for p in pts))

    meas = {p["db"]: measure(kau, p["db"]) for p in pts}
    check("N3 dark 按日求和 == kc_app_usage 的 off_hours（口径自校）",
          all(abs(m["dark_reconcile_h"]) <= 0.01 for m in meas.values()),
          str([m["dark_reconcile_h"] for m in meas.values()]))

    st = stability(pts, meas)
    byday = {r["day"]: r for r in st["days"]}
    check("N4 day1 同值 ⇒ STABLE（无差异不算变动）", byday["2026-03-01"]["verdict"] == "STABLE")
    # 负控1：day2 回填 +1h ⇒ 必须报 CHANGING 且 Δ==+1.0h
    p2 = byday["2026-03-02"]["pairs"][0]
    check("NC1 回填必被抓（Δfg=+1.000h）", byday["2026-03-02"]["verdict"] == "CHANGING"
          and abs(p2["delta_fg_h"] - 1.0) < 1e-6, str(p2))
    check("NC2 回填的熄屏也抓到（Δdark=+1.0h）", abs((p2["delta_dark_h"] or 0) - 1.0) < 1e-6, str(p2["delta_dark_h"]))
    check("NC3 回填的通知也抓到（Δnotif=+1）", p2["delta_notif"] == 1, str(p2["delta_notif"]))
    # 负控2：day3 少 1h ⇒ SHRANK
    p3 = byday["2026-03-03"]["pairs"][0]
    check("NC4 行被删必被抓（Δfg=-1.000h）", byday["2026-03-03"]["verdict"] == "CHANGING"
          and abs(p3["delta_fg_h"] + 1.0) < 1e-6, str(p3))
    # 负控3：截断日不得进判定
    check("NC5 截断日(@快照当日) 不进稳定期判定",
          byday["2026-03-04"]["usable_obs"] == 1 and byday["2026-03-04"]["verdict"] == "NO_PAIR",
          str(byday["2026-03-04"]["usable_obs"]))
    check("NC6 截断面单列（day4 @A 占最终值 50%）",
          any(t["day"] == "2026-03-04" and t["snap"] == "2026-03-04"
              and t["pct_of_final"] == 50.0 for t in st["truncated_rows"]), str(st["truncated_rows"]))
    # 负控4：零值/缺失日不得当"稳定"
    check("NC7 无行日判 invalid 而非 STABLE",
          any(x["day"] == "2026-03-05" and x["reason"] == "usable_obs<2" for x in st["invalid"]))
    check("N5 判定汇总=CHANGING_REAL 且变动的日=[day2,day3]",
          st["verdict"] == "CHANGING_REAL" and sorted(st["changing_days"]) == ["2026-03-02", "2026-03-03"],
          str(st["changing_days"]))
    check("N6 评估日数=3（day1/2/3）", st["evaluated_days"] == 3, str(st["evaluated_days"]))

    # 负控5：篡改副本（把 B 的 day2 追加行删掉）⇒ 差异必须消失=可证伪
    d = os.path.dirname(dbB)
    os.remove(dbB)                       # 夹具按空库建表 ⇒ 重写前必须删旧文件
    kau._fixture_db(dbB, app_rows=[r for r in base_app if r != DROP3]
                              + [(u(2026, 3, 4, 10), u(2026, 3, 4, 11), "X"),
                                 (u(2026, 3, 4, 15), u(2026, 3, 4, 16), "X"),
                                 (u(2026, 3, 7, 10), u(2026, 3, 7, 11), "X")],
                    notif_rows=base_notif, bl_rows=base_bl)
    # 重写 manifest sha 以通过 integrity（模拟"另一份快照"）
    mfp = os.path.join(d, "manifest.json")
    m = json.load(open(mfp))
    m["revisions"][-1]["sha256"] = sha256(dbB)
    json.dump(m, open(mfp, "w"))
    pts2 = snapshot_points(td, "*/knowledgeC.db")
    meas2 = {p["db"]: measure(kau, p["db"]) for p in pts2}
    st2 = stability(pts2, meas2)
    check("NC8 篡改副本后可证伪（day2 回 CHANGING→STABLE）",
          st2["changing_days"] == ["2026-03-03"], str(st2["changing_days"]))
    check("N7 day2 的变更被判 BACKFILL（行级分类接进主链路）",
          st["changing_kinds"]["2026-03-02"]["kind"] == "BACKFILL",
          str(st["changing_kinds"].get("2026-03-02")))

    # 负控6：sha 与 manifest 不一致必须报 MISMATCH
    m["revisions"][-1]["sha256"] = "0" * 64
    json.dump(m, open(mfp, "w"))
    pts3 = snapshot_points(td, "*/knowledgeC.db")
    check("NC9 manifest sha 不符 ⇒ MISMATCH（不许静默）",
          any(p["integrity"] == "MISMATCH" for p in pts3))

    # 负控7：单快照 ⇒ 证据不足 rc=2
    d1 = os.path.join(td, "single")
    os.makedirs(d1, exist_ok=True)
    dbS = os.path.join(d1, "knowledgeC.db")
    kau._fixture_db(dbS, app_rows=base_app, notif_rows=base_notif, bl_rows=base_bl)
    json.dump({"date": "single", "revisions": [{"ts": "2026-03-05 09:00:00", "sha256": sha256(dbS)}]},
              open(os.path.join(d1, "manifest.json"), "w"))
    ptsS = snapshot_points(d1, "knowledgeC.db")
    stS = stability(ptsS, {p["db"]: measure(kau, p["db"]) for p in ptsS})
    check("NC10 单快照 ⇒ NO_EVALUABLE_DAY（证据不足）", stS["verdict"] == "NO_EVALUABLE_DAY")

    # 负控8：容差必须生效（tol=100% 时 day2/3 的 ±50% 视为未变）
    stT = stability(pts, meas, tol_pct=100.0)
    check("NC11 tol 生效（tol=100%% ⇒ day2 不再算变）", "2026-03-02" not in stT["changing_days"],
          str(stT["changing_days"]))

    # 负控9：行级分类（剔除 vs 回填 vs 改写）——纯函数，零夹具
    R0 = {("X", 0, 3600), ("X", 7200, 10800), ("Y", 14400, 18000)}
    k_bk = classify_change(R0, R0 | {("X", 36000, 39600)})
    check("NC12 只增不减 ⇒ BACKFILL", k_bk["kind"] == "BACKFILL", str(k_bk))
    k_ev = classify_change(R0, {("X", 7200, 10800), ("Y", 14400, 18000)})
    check("NC13 只减不增且删的都是最老行 ⇒ EVICTION_HEAD",
          k_ev["kind"] == "EVICTION_HEAD" and k_ev["removed_after_cut"] == 0, str(k_ev))
    k_rw = classify_change(R0, {("X", 7200, 10800), ("Z", 999, 1999)})
    check("NC14 有增有删 ⇒ REWRITE", k_rw["kind"] == "REWRITE", str(k_rw))
    k_bad = classify_change(R0, {("X", 0, 3600)})
    check("NC15 违反头部切割(删的是较新行) ⇒ SHRINK_UNEXPLAINED，不许当剔除",
          k_bad["kind"] == "SHRINK_UNEXPLAINED" and k_bad["removed_after_cut"] == 2, str(k_bad))

    # 负控10：整链集成——按龄剔除的日不得被判成「回填」
    dh = os.path.join(td, "head")
    r3a = (u(2026, 3, 3, 9), u(2026, 3, 3, 10), "X")
    r3b = (u(2026, 3, 3, 20), u(2026, 3, 3, 21), "X")
    dbH1 = mk("head/s1", "2026-03-04 12:00:00", extra_app=[r3a, r3b])
    dbH2 = mk("head/s2", "2026-03-05 09:00:00", extra_app=[r3b])
    ptsH = snapshot_points(dh, "*/knowledgeC.db")
    stH = stability(ptsH, {p["db"]: measure(kau, p["db"]) for p in ptsH}, kau=kau)
    kH = stH["changing_kinds"].get("2026-03-03", {})
    check("NC16 集成：按龄剔除 ⇒ kind=EVICTION_HEAD", kH.get("kind") == "EVICTION_HEAD", str(kH))
    check("NC17 集成：verdict=EVICTION_ONLY 且真变化为空（剔除不告警）",
          stH["verdict"] == "EVICTION_ONLY" and stH["changing_days_real"] == [],
          "%s %s" % (stH["verdict"], stH["changing_days_real"]))
    _ = dbH1, dbH2

    # 负控11：★R462 真级负控抓到的假阴性——中段只删 1 条长行，旧版按行数宽容会判 EVICTION_HEAD
    k_mid1 = classify_change(R0, {("X", 0, 3600), ("Y", 14400, 18000)})
    check("NC18 中段只删 1 条（起点远晚于切割点）⇒ SHRINK_UNEXPLAINED，不许被行数宽容吃掉",
          k_mid1["kind"] == "SHRINK_UNEXPLAINED" and k_mid1["removed_after_cut"] == 1, str(k_mid1))
    # 负控12：边界宽容必须仍生效——被删行起点与切割点同刻 ⇒ EVICTION_HEAD（防过度加严）
    R1 = R0 | {("Z", 0, 1800)}
    k_bnd = classify_change(R1, R0)
    check("NC19 与切割点同刻的边界行被删 ⇒ 仍判 EVICTION_HEAD（秒级宽容有效）",
          k_bnd["kind"] == "EVICTION_HEAD" and k_bnd["removed_after_cut"] == 0, str(k_bnd))

    # 负控13：★R462 真级负控抓到的**第三处盲区**——整日消失只记 ABSENT ⇒ 旧版判 STABLE
    #   窗口内(该日 >= 晚快照窗口头)整日消失 = 真变化，必须抓；
    #   早于窗口头整日消失 = kc 按龄滚动剔除，合法，不得报（防过度加严）。
    dmA = os.path.join(td, "missA")
    dmB = os.path.join(td, "missB")
    r1 = (u(2026, 3, 1, 10), u(2026, 3, 1, 11), "X")
    r2 = (u(2026, 3, 2, 9), u(2026, 3, 2, 10), "X")
    r3 = (u(2026, 3, 3, 9), u(2026, 3, 3, 10), "X")
    mk("missA/s1", "2026-03-04 12:00:00", extra_app=[r1, r2, r3])
    mk("missA/s2", "2026-03-05 09:00:00", extra_app=[r1, r3])
    ptsM = snapshot_points(dmA, "*/knowledgeC.db")
    measM = {p["db"]: measure(kau, p["db"]) for p in ptsM}
    stM = stability(ptsM, measM, kau=kau)
    kM = stM["changing_kinds"].get("2026-03-02", {})
    dbM1, dbM2 = ptsM[0]["db"], ptsM[1]["db"]
    check("NC20a 夹具自证：03-02 在早快照有行、在晚快照 0 行（非空负控）",
          len(day_rows(kau, dbM1, "2026-03-02")) == 1
          and len(day_rows(kau, dbM2, "2026-03-02")) == 0,
          "%d/%d" % (len(day_rows(kau, dbM1, "2026-03-02")),
                     len(day_rows(kau, dbM2, "2026-03-02"))))
    check("NC20b 窗口内整日消失 ⇒ kind=MISSING_DAY 且计入真变化",
          kM.get("kind") == "MISSING_DAY" and "2026-03-02" in stM["changing_days_real"],
          str(kM))
    check("NC20c 整日消失的日不得判 STABLE", stM["verdict"] == "CHANGING_REAL",
          "%s %s" % (stM["verdict"], stM["changing_days"]))
    # 早于窗口头整日消失 ⇒ 合法剔除，不报
    mk("missB/s1", "2026-03-04 12:00:00", extra_app=[r1, r2, r3])
    mk("missB/s2", "2026-03-05 09:00:00", extra_app=[r2, r3])   # 03-01 消失，窗口头随之前移到 03-02
    ptsB2 = snapshot_points(dmB, "*/knowledgeC.db")
    stB2 = stability(ptsB2, {p["db"]: measure(kau, p["db"]) for p in ptsB2}, kau=kau)
    check("NC21a 夹具自证：03-01 在晚快照确为 0 行且窗口头已前移",
          len(day_rows(kau, ptsB2[1]["db"], "2026-03-01")) == 0
          and app_head_day(kau, ptsB2[1]["db"]) == "2026-03-02",
          str(app_head_day(kau, ptsB2[1]["db"])))
    check("NC21b 早于窗口头的整日消失 ⇒ 不报（合法滚动剔除，防过度加严）",
          "2026-03-01" not in stB2["changing_days"] and stB2["changing_days_real"] == [],
          "%s %s" % (stB2["verdict"], stB2["changing_days"]))

    # 只读自证：夹具目录 sha 前后一致 + 无新增文件
    snap_before = {p["db"]: sha256(p["db"]) for p in snapshot_points(td, "*/knowledgeC.db")}
    st_re = stability(pts, {p["db"]: measure(kau, p["db"]) for p in pts}, kau=kau)
    _ = print_stability(st_re)
    snap_after = {p["db"]: sha256(p["db"]) for p in snapshot_points(td, "*/knowledgeC.db")}
    check("R1 只读自证：副本 sha 前后一致", snap_before == snap_after)

    # 源码静态守卫：本器不得出现源库路径（只读自证）
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    check("R2 源码不含源库路径片段（不碰 live 源）", LIVE_MARKER not in src)

    print("\n".join(msgs))
    print("selftest %d/%d 通过, 失败 %d" % (ok, ok + fail, fail))
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="kc 迟写回填稳定期实测（只读）")
    ap.add_argument("cmd", nargs="?", default="stability", choices=["points", "matrix", "stability"])
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--glob", default=DEFAULT_GLOB)
    ap.add_argument("--tol-pct", type=float, default=1.0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--strict", action="store_true",
                    help="有日发生**真变化**(回填/改写/无法解释的减少) ⇒ rc=1；仅按龄剔除不算")
    ap.add_argument("--readonly-proof", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()

    root = os.path.abspath(a.root)
    kau = load_kc_app_usage(root)
    points = snapshot_points(root, a.glob)
    if not points:
        print("未找到快照：%s" % a.glob)
        return 2

    if a.readonly_proof:
        before = {p["db"]: sha256(p["db"]) for p in points}

    if a.cmd == "points":
        print_points(points)
        rc = 0
    else:
        bad = [p for p in points if p["integrity"] != "ok"]
        if bad:
            print("INTEGRITY_FAIL: %s" % [p["date"] for p in bad])
            return 2
        measures = {p["db"]: measure(kau, p["db"]) for p in points}
        for p in points:
            m = measures[p["db"]]
            if abs(m["dark_reconcile_h"]) > 0.05:
                print("CALIBRATION_FAIL %s dark 按日求和 vs off_hours 差 %.3fh" % (p["date"], m["dark_reconcile_h"]))
                return 2
        if a.cmd == "matrix":
            print_points(points)
            print_matrix(build_matrix(points, measures), points)
            rc = 0
        else:
            st = stability(points, measures, tol_pct=a.tol_pct, kau=kau)
            print_points(points)
            print()
            print_matrix(build_matrix(points, measures), points)
            print()
            print_stability(st)
            rc = 2 if st["verdict"] == "NO_EVALUABLE_DAY" else (
                1 if (a.strict and st["changing_days_real"]) else 0)
            if a.json:
                out = {"report": "kc_backfill_stability/v1",
                       "points": [{k: p.get(k) for k in
                                   ("date", "ts", "sha256", "objects", "revisions", "integrity", "db")}
                                  for p in points],
                       "stability": {k: v for k, v in st.items() if k != "days"},
                       "days": st["days"],
                       "dark_reconcile": {p["date"]: measures[p["db"]]["dark_reconcile_h"] for p in points}}
                os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
                json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                print("JSON → %s (%d B)" % (a.json, os.path.getsize(a.json)))

    if a.readonly_proof:
        after = {p["db"]: sha256(p["db"]) for p in points}
        same = before == after
        print("只读自证：%d 份副本 sha256 前后%s" % (len(before), "一致" if same else "★不一致★"))
        if not same:
            return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
