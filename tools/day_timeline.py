#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""day_timeline/v1 — 本机四源「单日活动时间线」对齐器（只读，纯 stdlib）

给定一个本地日期，把四个时间衰减源的**每日快照**叠成桶级时间线，并统计
「不可解释区间」（既不在睡眠、也没有前台应用、也没有浏览器访问）。

四源与角色
----------
  kc      /app/usage 前台区间（应用级） + /display/isBacklit 熄屏区间   [主源]
  chrome  History 的 http(s) 访问（host 级）                            [主源]
  pmset   睡眠窗口（enter→wake，darkwake 不计）                          [主源]
  wifi    wifi.log 的 airport/DHCP 等事件行                              [辅源·只标注]

口径铁律（全部来自既有记忆，本工具只执行不发明）
------------------------------------------------
  1. **跨源禁互推**：只在「三主源覆盖窗口的交集」内做判定；交集外的桶一律
     `unknown`，**绝不补零、绝不记为不可解释**。
  2. **kc 迟写回填**：kc 的「今天」在快照时点不可用 ⇒ 覆盖窗口上界恒取
     该快照的 `max(ZCREATIONDATE)`（cutoff），下界取 `min(ZSTARTDATE)`。
     快照按「对本日覆盖秒数最大」选，平手取日期较新者。
  3. **chrome 累积库**：单份 History 覆盖到副本 mtime 为止 ⇒ 上界取
     `min(日末, History mtime)`，下界取日首（库本身是累积的）。
  4. **pmset 按日轮转**：某日只有存在 `<YYYY.MM.DD>.asl` 才算覆盖；
     上界取 `min(日末, events.json mtime)`（**DarkWake 不计入睡眠**）。
  5. **wifi 不参与判定**：只做标注（事件密度），缺源不影响 verdict。
  6. 时区一律**本地时间**（与 kc_app_usage / sleep_source_crosscheck 同口径）。

退出码
------
  0  正常（可能有未知区间提示）
  1  分析完成但有告警（如三方交叉校验不等、不可解释占比畸高）
  2  输入/口径错误（日期非法、主源缺失、无共同覆盖、--as-of 混用快照）

子命令（位置参数，非 --flag）
----------------------------
  render   --date YYYY-MM-DD [--temp DIR] [--bucket 60] [--as-of TS] [--json]
           [--out F] [--no-ascii] [--top N] [--proof]
  selftest 夹具自测（不读真源）
  proof    只读取证（打印输入文件 sha256，前后一致自证）

复用（禁重写内核）：kc_app_usage / sleep_source_crosscheck /
chrome_history_consume / rolling_source_snapshot
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import chrome_history_consume as C      # noqa: E402
import kc_app_usage as K                 # noqa: E402
import rolling_source_snapshot as R      # noqa: E402
import sleep_source_crosscheck as S      # noqa: E402

METHOD = "day_timeline/v1"
GA_ROOT = HERE.parent
DEFAULT_TEMP = GA_ROOT / "temp"
DAY_S = 86400
PRIMARY = ("kc", "chrome", "pmset")          # 参与 explained/unexplained 的源
ANNOT = ("wifi",)                            # 仅标注，不参与判定
DIR_OF = {"kc": "kc_snapshots", "chrome": "chrome_snapshots",
          "pmset": "pmset_snapshots", "wifi": "wifi_snapshots"}
BUCKETS = (1, 2, 5, 15, 30, 60)              # 允许的桶粒度（秒）
# 展示优先级：越靠前越"占位"
DISPLAY_PRECEDENCE = ("unknown", "sleep_fg", "sleep", "fg", "browse",
                      "fg_browse", "unexplained")
CLASS_ZH = {"unknown": "未覆盖(UNKNOWN)", "sleep_fg": "睡眠∧前台(冲突)",
            "sleep": "睡眠", "fg": "前台", "browse": "浏览器访问",
            "fg_browse": "前台∧访问", "unexplained": "不可解释"}
CHAR_OF = {"unknown": "?", "sleep_fg": "X", "sleep": "S", "fg": "F",
           "browse": "B", "fg_browse": "f", "unexplained": "."}


class InputError(Exception):
    """输入/口径错误 → rc=2（fail-closed）"""

    def __init__(self, code, detail):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _iso(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") if isinstance(t, dt.datetime) else None


def _sha16(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


import re  # noqa: E402  （严格格式校验用）


def parse_date(s):
    txt = str(s).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        raise InputError("INVALID_DATE", "日期须为 YYYY-MM-DD（两位零填充），收到 %r" % (s,))
    try:
        return dt.datetime.strptime(txt, "%Y-%m-%d").date()
    except Exception:
        raise InputError("INVALID_DATE", "日期须为 YYYY-MM-DD，收到 %r" % (s,))


def parse_as_of(s):
    txt = str(s).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", txt):
        raise InputError("INVALID_DATE",
                         "--as-of 须为 YYYY-MM-DD[ HH:MM[:SS]]，收到 %r" % (s,))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(str(s).strip(), fmt)
        except Exception:
            continue
    raise InputError("INVALID_DATE", "--as-of 须为 YYYY-MM-DD[ HH:MM[:SS]]，收到 %r" % (s,))


def day_bounds(day):
    d0 = dt.datetime(day.year, day.month, day.day)
    return d0, d0 + dt.timedelta(days=1)


def _fill(mask, s, e, d0, bucket):
    """把 [s,e) 落进桶掩码；返回命中的桶数"""
    if e <= s:
        return 0
    i0 = int(math.floor((s - d0).total_seconds() / bucket))
    i1 = int(math.ceil((e - d0).total_seconds() / bucket))
    i0 = max(0, i0)
    i1 = min(len(mask), i1)
    for i in range(i0, i1):
        mask[i] = True
    return max(0, i1 - i0)


def _fill_sets(sets, s, e, d0, bucket, key):
    if e <= s:
        return
    i0 = max(0, int(math.floor((s - d0).total_seconds() / bucket)))
    i1 = min(len(sets), int(math.ceil((e - d0).total_seconds() / bucket)))
    for i in range(i0, i1):
        sets[i].add(key)


def _ov(intervals, lo, hi):
    """[lo,hi) 与 intervals（(s,e[,x])）的交叠秒数（同源可比口径）"""
    tot = 0.0
    for it in intervals:
        s, e = it[0], it[1]
        a, b = max(s, lo), min(e, hi)
        if b > a:
            tot += (b - a).total_seconds()
    return tot


def _runs(flags):
    """连续 True 段 → [(start_idx, end_idx_exclusive)]"""
    out = []
    i = 0
    n = len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _pick_closest_year(month, dayn, target):
    """wifi/ASL 行不含年份 ⇒ 在 target±1 年里选与 target 日期最接近的"""
    best = None
    for y in (target.year - 1, target.year, target.year + 1):
        try:
            cand = dt.datetime(y, month, dayn)
        except ValueError:
            continue
        dd = abs((cand.date() - target.date()).days)
        if best is None or dd < best[0]:
            best = (dd, y)
    return best[1] if best else target.year


# --------------------------------------------------------------------------
# 源发现（自动发现 = 覆盖最大/最新；也可被显式路径覆盖）
# --------------------------------------------------------------------------
def _kc_span(db_path):
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    try:
        mn = con.execute("SELECT MIN(ZSTARTDATE) FROM ZOBJECT").fetchone()[0]
        mx = con.execute(K.SQL_CUTOFF).fetchone()[0]
    finally:
        con.close()
    if mn is None or mx is None:
        raise InputError("KC_UNREADABLE", "kc 快照无可用时间轴：%s" % db_path)
    return (dt.datetime.fromtimestamp(mn + K.EPOCH_OFF),
            dt.datetime.fromtimestamp(mx + K.EPOCH_OFF))


def discover_kc(temp, day):
    d0, d1 = day_bounds(day)
    best = None
    for db in sorted(glob.glob(str(Path(temp) / DIR_OF["kc"] / "*" / "knowledgeC.db"))):
        snap_day = os.path.basename(os.path.dirname(db))
        try:
            mn, mx = _kc_span(db)
        except Exception:
            continue
        s, e = max(d0, mn), min(d1, mx)
        secs = (e - s).total_seconds()
        if secs <= 0:
            continue
        rec = {"src": "kc", "path": db, "snapshot_day": snap_day,
               "coverage": (s, e), "_secs": secs, "data_min": mn, "cutoff": mx,
               "note": ("FULL" if (s <= d0 and e >= d1) else "PARTIAL")
                       + ("|KCFILL(cutoff=%s)" % _iso(mx))}
        if best is None or secs > best["_secs"] or (
                secs == best["_secs"] and snap_day > best["snapshot_day"]):
            best = rec
    return best


def _chrome_db_in(snap_dir):
    for cand in (Path(snap_dir) / "History", Path(snap_dir) / "Default" / "History"):
        if cand.exists():
            return str(cand)
    return None


def discover_chrome(temp, day):
    d0, d1 = day_bounds(day)
    paths = []
    for d in sorted(glob.glob(str(Path(temp) / DIR_OF["chrome"] / "*")), reverse=True):
        db = _chrome_db_in(d)
        if db:
            paths.append((d, db))
    for d, db in paths:
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(db))
        e = min(d1, mtime)
        if e <= d0:
            continue
        return {"src": "chrome", "path": db, "snapshot_day": os.path.basename(d),
                "coverage": (d0, e), "_secs": (e - d0).total_seconds(),
                "note": ("FULL" if e >= d1 else "PARTIAL") + "|copy_mtime=%s" % _iso(mtime)}
    return None


def discover_pmset(temp, day):
    d0, d1 = day_bounds(day)
    want = "%04d.%02d.%02d.asl" % (day.year, day.month, day.day)
    for d in sorted(glob.glob(str(Path(temp) / DIR_OF["pmset"] / "*")), reverse=True):
        ev_p = Path(d) / "events.json"
        if not ev_p.exists():
            continue
        try:
            ev = json.loads(ev_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        names = []
        for f in ev.get("files", []):
            names.append(f.get("file") if isinstance(f, dict) else str(f))
        if not any(n and want in str(n) for n in names):
            continue
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(ev_p))
        e = min(d1, mtime)
        if e <= d0:
            continue
        return {"src": "pmset", "path": str(ev_p), "snapshot_day": os.path.basename(d),
                "coverage": (d0, e), "_secs": (e - d0).total_seconds(),
                "note": ("FULL" if e >= d1 else "PARTIAL") + "|asl=%s" % want}
    return None


def discover_wifi(temp, day):
    d0, d1 = day_bounds(day)
    cand = Path(temp) / DIR_OF["wifi"] / day.isoformat() / "wifi.log"
    if not cand.exists():
        return None
    return {"src": "wifi", "path": str(cand), "snapshot_day": day.isoformat(),
            "coverage": None, "_secs": 0.0, "note": "ANNOT_ONLY"}


def discover_all(temp, day, overrides=None):
    overrides = overrides or {}
    out = {}
    out["kc"] = discover_kc(temp, day)
    out["chrome"] = discover_chrome(temp, day)
    out["pmset"] = discover_pmset(temp, day)
    out["wifi"] = discover_wifi(temp, day)
    for k, v in overrides.items():
        if v is None:
            out[k] = None          # 显式禁源：不自动发现，交给调用方判「主源缺失」
            continue
        p = Path(v)
        src = k
        if src == "chrome" and p.is_dir():
            p = Path(_chrome_db_in(p) or p)
        if src == "kc" and p.is_dir():
            p = p / "knowledgeC.db"
        if src == "pmset" and p.is_file():
            p = p.parent
        if src == "wifi" and p.is_dir():
            p = p / "wifi.log"
        if src in ("kc", "chrome"):
            if not p.exists():
                raise InputError("MISSING_SOURCE", "%s 显式路径不存在：%s" % (src, p))
            d0, d1 = day_bounds(day)
            if src == "kc":
                mn, mx = _kc_span(str(p))
                s, e = max(d0, mn), min(d1, mx)
                out[src] = {"src": src, "path": str(p), "snapshot_day": "explicit",
                            "coverage": (s, e), "_secs": (e - s).total_seconds(),
                            "data_min": mn, "cutoff": mx,
                            "note": ("FULL" if (s <= d0 and e >= d1) else "PARTIAL")
                                    + "|explicit"}
            else:
                mtime = dt.datetime.fromtimestamp(os.path.getmtime(p))
                e = min(d1, mtime)
                out[src] = {"src": src, "path": str(p), "snapshot_day": "explicit",
                            "coverage": (d0, e), "_secs": (e - d0).total_seconds(),
                            "note": "explicit|copy_mtime=%s" % _iso(mtime)}
        elif src == "pmset":
            if not p.exists():
                raise InputError("MISSING_SOURCE", "pmset 显式路径不存在：%s" % p)
            d0, d1 = day_bounds(day)
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(p / "events.json"))
            e = min(d1, mtime)
            out[src] = {"src": src, "path": str(p / "events.json"), "snapshot_day": "explicit",
                        "coverage": (d0, e), "_secs": (e - d0).total_seconds(),
                        "note": "explicit"}
        elif src == "wifi":
            if not p.exists():
                out[src] = None
            else:
                out[src] = {"src": src, "path": str(p), "snapshot_day": "explicit",
                            "coverage": None, "_secs": 0.0, "note": "ANNOT_ONLY|explicit"}
    return out


# --------------------------------------------------------------------------
# 源加载
# --------------------------------------------------------------------------
def load_kc(rec, day):
    d0, d1 = day_bounds(day)
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(rec["path"]), uri=True)
    try:
        apps = con.execute(K.SQL_APP).fetchall()
        bls = con.execute(K.SQL_BL).fetchall()
    finally:
        con.close()

    def cv(t):
        return dt.datetime.fromtimestamp(t + K.EPOCH_OFF)

    hi = min(d1, rec["cutoff"])   # ★kc 迟写回填：区间一律裁到 cutoff（与 kc_app_usage 同口径）
    rows = skipped = 0
    per_app = defaultdict(list)
    for t, e, app in apps:
        if t is None or e is None or e <= t:
            skipped += 1
            continue
        s, x = cv(t), min(cv(e), hi)
        if x <= d0 or s >= d1:
            continue
        per_app[app or "?"].append((s, x))
        rows += 1
    by_app = {}
    merged_all = []
    for app, iv in per_app.items():
        m = K.merge(iv)
        merged_all.extend(m)
        by_app[app] = round(sum((e - s).total_seconds() for s, e in m), 3)
    merged_all = K.merge(merged_all)

    off, on, bl_skipped = [], [], 0
    for t, e, v in bls:
        if t is None or e is None or e <= t:
            bl_skipped += 1
            continue
        s, x = cv(t), min(cv(e), hi)
        if x <= d0 or s >= d1:
            continue
        if int(v or 0) == 0:
            off.append((s, x))
        else:
            on.append((s, x))
    off = K.merge(off)
    on = K.merge(on)
    return {"fg_merged": merged_all, "fg_by_app": by_app, "fg_rows": rows,
            "fg_skipped": skipped, "off": off, "on": on,
            "off_s": round(sum((e - s).total_seconds() for s, e in off), 3),
            "on_s": round(sum((e - s).total_seconds() for s, e in on), 3),
            "bl_rows": len(off) + len(on), "bl_skipped": bl_skipped}


def load_chrome(rec, day):
    d0, d1 = day_bounds(day)
    visits = C.load_visits(Path(rec["path"]))
    todays = []
    for v in visits:
        t = v[0]
        host = v[1] if len(v) > 1 else ""
        if t is None:
            continue
        if t.tzinfo is not None:
            t = t.astimezone().replace(tzinfo=None)
        if d0 <= t < d1:
            todays.append((t, C.norm_host(host)))
    return {"visits": todays, "n_visits": len(todays), "n_all": len(visits)}


def load_pmset(rec, day):
    ev = json.loads(Path(rec["path"]).read_text(encoding="utf-8"))
    d0, d1 = day_bounds(day)
    wins, open_, skipped = [], 0, 0
    for w in ev.get("windows", []):
        try:
            s = dt.datetime.strptime(w["enter"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            skipped += 1
            continue
        if not w.get("wake"):
            open_ += 1
            continue
        try:
            e = dt.datetime.strptime(w["wake"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            skipped += 1
            continue
        if e <= s:
            skipped += 1
            continue
        if e <= d0 or s >= d1:
            continue
        wins.append((s, e, w.get("reason") or ""))
    return {"windows": wins, "open_windows": open_, "skipped": skipped}


def load_wifi(rec, day):
    tgt = dt.datetime(day.year, day.month, day.day)
    lines = Path(rec["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
    events, n_ts, dhcp = [], 0, 0
    for ln in lines:
        m = R._ts_from_line(ln, day.year)
        if not m:
            continue
        n_ts += 1
        base = dt.datetime.strptime(m, "%Y-%m-%d %H:%M:%S")
        # 年界：文件里不含年份 ⇒ 选最接近目标日的年份
        y = _pick_closest_year(base.month, base.day, tgt)
        t = base.replace(year=y)
        if abs((t.date() - tgt.date()).days) > 1:
            continue
        events.append(t)
        if "DHCP" in ln or "dhcp" in ln:
            dhcp += 1
    events.sort()
    in_day = [t for t in events if t.date() == day]
    agg = None
    try:
        agg = R.parse_wifi_text(lines, year=day.year)
    except Exception as exc:                                  # 兼容签名差异
        agg = {"unavailable": str(exc)[:120]}
    return {"events": in_day, "n_events": len(in_day), "n_lines": len(lines),
            "n_timestamped": n_ts, "dhcp_lines": dhcp,
            "first": _iso(in_day[0]) if in_day else None,
            "last": _iso(in_day[-1]) if in_day else None, "rss_agg": agg}


# --------------------------------------------------------------------------
# 核心对齐
# --------------------------------------------------------------------------
def analyze(date_s, temp=None, paths=None, bucket=60, as_of=None, top=8,
            ascii_map=True, proof=False):
    """→ (res, rc, notes:list)

    口径（写死在代码里，勿凭印象改）：
      * 三主源 = kc / chrome / pmset；wifi 只标注不定论（ANNOT）。
      * coverage[src] = 该源「能观测到」的窗口 = [max(日界下沿, 数据下沿), min(日界上沿, 快照拷贝时刻)]；
        kc 的上沿另受 cutoff（迟写回填边界）约束 —— kc 有迟写回填，量必绑快照时点。
      * covered 桶 = 三主源同时覆盖；交集外一律 UNKNOWN，不补零。
      * 共同覆盖 < 1 桶 ⇒ NO_COMMON_COVERAGE（fail-closed，不给无意义统计）。
      * --as-of 早于任一源的数据 ⇒ MIXED_SNAPSHOT（禁止跨时点混用）。
    """
    day = parse_date(date_s)
    if bucket not in BUCKETS:
        raise InputError("INVALID_BUCKET", "bucket 须∈%s，收到 %r" % (list(BUCKETS), bucket))
    temp = Path(temp) if temp else DEFAULT_TEMP
    d0, d1 = day_bounds(day)
    if as_of is not None:
        if not isinstance(as_of, dt.datetime):
            as_of = parse_as_of(as_of)
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone().replace(tzinfo=None)   # → 本机裸本地时间
    notes = []
    sha_before = {}

    srcs = discover_all(temp, day, paths)
    missing = [s for s in PRIMARY if not srcs.get(s)]
    if missing:
        def _avail(src):
            # ★ 取快照目录自身的名字（曾误取父目录名 ⇒ 提示恒显示 kc_snapshots ×N）
            return sorted(os.path.basename(p)
                          for p in glob.glob(str(temp / DIR_OF[src] / "*")))

        raise InputError("MISSING_SOURCE",
                         "主源缺失（%s）：无法判定该日。--temp 下可用快照：%s"
                         % (",".join(missing),
                            "; ".join("%s=%s" % (DIR_OF[s], _avail(s))
                                      for s in PRIMARY)))
    for s in PRIMARY + ANNOT:
        r = srcs.get(s)
        if r:
            sha_before[s] = _sha16(r["path"])

    kc = load_kc(srcs["kc"], day)
    ch = load_chrome(srcs["chrome"], day)
    pm = load_pmset(srcs["pmset"], day)
    wf = load_wifi(srcs["wifi"], day) if srcs.get("wifi") else None

    # ---- --as-of：禁止跨快照时点混用 ----
    if as_of is not None:
        late = []
        if kc["fg_merged"] and max(e for _, e in kc["fg_merged"]) > as_of:
            late.append("kc:fg")
        if kc["off"] and max(e for _, e in kc["off"]) > as_of:
            late.append("kc:bl")
        if ch["visits"] and max(t for t, _ in ch["visits"]) > as_of:
            late.append("chrome:visit")
        if pm["windows"] and max(e for _, e, _ in pm["windows"]) > as_of:
            late.append("pmset:window")
        if wf and wf["events"] and max(wf["events"]) > as_of:
            late.append("wifi:line")
        if late:
            raise InputError("MIXED_SNAPSHOT",
                             "--as-of=%s 早于以下源的数据：%s ⇒ 禁止把不同时点的观测混用"
                             % (_iso(as_of), ",".join(late)))

    n = DAY_S // bucket
    cov = {k: [False] * n for k in PRIMARY}
    for k in PRIMARY:
        s, e = srcs[k]["coverage"]
        _fill(cov[k], max(s, d0), min(e, d1), d0, bucket)
    covered = [cov["kc"][i] and cov["chrome"][i] and cov["pmset"][i] for i in range(n)]
    cs = max(max(srcs[k]["coverage"][0] for k in PRIMARY), d0)
    ce = min(min(srcs[k]["coverage"][1] for k in PRIMARY), d1)
    cover_secs = (ce - cs).total_seconds()
    if cover_secs < bucket:
        raise InputError("NO_COMMON_COVERAGE",
                         "三主源共同覆盖仅 %.1f 分钟（< 1 桶 = %d 分钟）⇒ 该日统计无意义"
                         % (cover_secs / 60.0, bucket))

    # ---- 四源落桶（一次算清，勿再二次重建）----
    fg_sets = [set() for _ in range(n)]
    br_sets = [set() for _ in range(n)]
    sl = [False] * n
    off = [False] * n
    on = [False] * n
    wfc = [0] * n
    for s, e in kc["fg_merged"]:
        _fill_sets(fg_sets, s, e, d0, bucket, None)
    for t, host in ch["visits"]:
        i = int((t - d0).total_seconds() // bucket)
        if 0 <= i < n:
            br_sets[i].add(host)
    for s, e, _r in pm["windows"]:
        _fill(sl, s, e, d0, bucket)
    for s, e in kc["off"]:
        _fill(off, s, e, d0, bucket)
    for s, e in kc["on"]:
        _fill(on, s, e, d0, bucket)
    if wf:
        for t in wf["events"]:
            i = int((t - d0).total_seconds() // bucket)
            if 0 <= i < n:
                wfc[i] += 1

    classes = []
    for i in range(n):
        has_fg, has_br = bool(fg_sets[i]), bool(br_sets[i])
        if not covered[i]:
            classes.append("unknown")
        elif sl[i]:
            classes.append("sleep_fg" if has_fg else "sleep")
        elif has_fg and has_br:
            classes.append("fg_browse")
        elif has_fg:
            classes.append("fg")
        elif has_br:
            classes.append("browse")
        else:
            classes.append("unexplained")

    cnt = Counter(classes)
    covered_n = n - cnt["unknown"]
    mins = {k: round(v * bucket / 60.0, 3) for k, v in cnt.items()}
    expl = covered_n - cnt["unexplained"]
    aligned = {
        "window": [_iso(cs), _iso(ce)],
        "kc_fg_secs": round(_ov(kc["fg_merged"], cs, ce), 3),
        "chrome_visits": len([t for t, _ in ch["visits"] if cs <= t < ce]),
        "pmset_sleep_secs": round(_ov(pm["windows"], cs, ce), 3),
        "kc_off_secs": round(_ov(kc["off"], cs, ce), 3),
    }

    res = {
        "tool": METHOD, "date": day.isoformat(), "bucket_s": bucket, "n_buckets": n,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources": {k: ({"path": v["path"], "snapshot_day": v["snapshot_day"],
                         "coverage": [_iso(v["coverage"][0]), _iso(v["coverage"][1])]
                         if v["coverage"] else None,
                         "note": v["note"], "sha256_16": sha_before.get(k)}
                        if v else None) for k, v in srcs.items()},
        "coverage": {
            "covered_minutes": round(covered_n * bucket / 60.0, 3),
            "covered_hours": round(covered_n * bucket / 3600.0, 3),
            "unknown_minutes": round(cnt["unknown"] * bucket / 60.0, 3),
            "intersection": [_iso(cs), _iso(ce)],
            "intersection_minutes": round(cover_secs / 60.0, 3),
            "rule": "covered = kc ∧ chrome ∧ pmset（三主源同时覆盖）；交集外一律 UNKNOWN，不补零",
        },
        "classes": {"minutes": mins,
                    "shares": {k: (round(v * bucket / 60.0 / (covered_n * bucket / 60.0), 4)
                                   if covered_n else None) for k, v in cnt.items()}},
        "explained": {
            "minutes": round(expl * bucket / 60.0, 3),
            "share": round(expl / covered_n, 4) if covered_n else None,
        },
        "unexplained": {
            "minutes": round(cnt["unexplained"] * bucket / 60.0, 3),
            "share": round(cnt["unexplained"] / covered_n, 4) if covered_n else None,
            "screen_on_minutes": round(sum(1 for i in range(n)
                                           if classes[i] == "unexplained" and not off[i])
                                       * bucket / 60.0, 3),
            "screen_off_minutes": round(sum(1 for i in range(n)
                                            if classes[i] == "unexplained" and off[i])
                                        * bucket / 60.0, 3),
            "hour_hist": _hour_hist(classes, n, bucket),
            "top_runs": _top_runs(classes, d0, bucket, top),
        },
        "kc": {"fg_minutes": round(sum(kc["fg_by_app"].values()) / 60.0, 3),
               "fg_by_app": _top_apps(kc["fg_by_app"], top),
               "fg_rows": kc["fg_rows"], "fg_skipped": kc["fg_skipped"],
               "off_screen_minutes": round(kc["off_s"] / 60.0, 3),
               "on_screen_minutes": round(kc["on_s"] / 60.0, 3),
               "bl_rows": kc["bl_rows"]},
        "chrome": {"visits": ch["n_visits"], "visits_all": ch["n_all"],
                   "top_hosts": _top_hosts(ch["visits"], top)},
        "pmset": {"sleep_minutes": round(sum((e - s).total_seconds() for s, e, _ in pm["windows"]) / 60.0, 3),
                  "windows": len(pm["windows"]), "open_windows": pm["open_windows"],
                  "skipped": pm["skipped"],
                  "reasons": Counter(r for _s, _e, r in pm["windows"]).most_common(5)},
        "wifi": ({"n_events": wf["n_events"], "n_lines": wf["n_lines"],
                  "n_timestamped": wf["n_timestamped"], "dhcp_lines": wf["dhcp_lines"],
                  "first": wf["first"], "last": wf["last"],
                  "max_per_bucket": max(wfc) if wfc else 0,
                  "rss_parse": _slim_rss(wf["rss_agg"])} if wf else
                 {"n_events": 0, "note": "源缺失（辅源，不影响判定）"}),
        "anomalies": {
            "sleep_and_fg_minutes": round(cnt["sleep_fg"] * bucket / 60.0, 3),
            "fg_and_browse_minutes": round(cnt["fg_browse"] * bucket / 60.0, 3),
        },
        "aligned": aligned,
        "cross_checks": {},
        "verdict": {},
    }
    if ascii_map and bucket <= 60:
        res["ascii"] = _ascii(classes, bucket, d0)

    # ---- 交叉校验（三方复用器对账）----
    checks = {}
    try:
        with _quiet():
            krep = K.analyze(srcs["kc"]["path"], focus_day=day.isoformat())
        row = next((d for d in krep.get("days", [])
                    if d.get("day") == day.isoformat() or d.get("date") == day.isoformat()), None)
        v = row.get("fg_hours") if row else None
        checks["kc_fg_hours_tool"] = round(res["kc"]["fg_minutes"] / 60.0, 3)
        checks["kc_fg_hours_kc_app_usage"] = v
        if isinstance(v, (int, float)):
            checks["kc_delta_s"] = round(abs(checks["kc_fg_hours_tool"] - v) * 3600, 3)
        else:
            checks["kc_note"] = "kc_app_usage 该日无数据（fg_hours=%r）" % (v,)
    except Exception as exc:
        checks["kc_error"] = str(exc)[:160]
    try:
        pout, _meta = S.pmset_daily(str(Path(srcs["pmset"]["path"]).parent))
        # 注意：sleep_source_crosscheck.pmset_daily 的日行以 datetime.date 为键
        prow = pout.get(day) or pout.get(day.isoformat()) or {}
        pv = prow.get("sleep_h")
        checks["pmset_sleep_hours_tool"] = round(res["pmset"]["sleep_minutes"] / 60.0, 3)
        checks["pmset_sleep_hours_ssc"] = pv
        if isinstance(pv, (int, float)):
            checks["pmset_delta_s"] = round(abs(checks["pmset_sleep_hours_tool"] - pv) * 3600, 1)
    except Exception as exc:
        checks["pmset_error"] = str(exc)[:160]
    res["cross_checks"] = checks

    for k, lab in (("kc_delta_s", "kc 前台"), ("pmset_delta_s", "pmset 睡眠")):
        if k in checks and checks[k] > 61:
            notes.append("CROSSCHECK_%s_DELTA=%.1fs（应≈0，>60s 即口径漂移）" % (lab, checks[k]))
    res["verdict"] = {"rc": 0, "notes": list(notes)}
    if proof:
        after = {k: _sha16(srcs[k]["path"]) for k in sha_before}
        changed = [k for k in sha_before if sha_before[k] != after.get(k)]
        res["proof"] = {"before": sha_before, "after": after, "changed": changed,
                        "readonly": not changed}
    return res, (1 if notes else 0), notes


class _quiet:
    """吞掉被复用器可能打印的噪声（不影响返回结构）"""

    def __enter__(self):
        import io
        self._b = io.StringIO()
        self._old = sys.stdout
        sys.stdout = self._b
        return self

    def __exit__(self, *a):
        sys.stdout = self._old
        return False


def _slim_rss(agg):
    if not isinstance(agg, dict):
        return None
    return {k: agg[k] for k in ("n_lines", "n_timestamped", "dhcp_lines", "first", "last")
            if k in agg} or {"keys": sorted(agg)[:8]}


def _top_apps(by_app, top):
    return [{"app": a, "minutes": round(s / 60.0, 2)}
            for a, s in sorted(by_app.items(), key=lambda x: -x[1])[:top]]


def _top_hosts(visits, top):
    c = Counter(h for _t, h in visits)
    return [{"host": h, "visits": v} for h, v in c.most_common(top)]


def _hour_hist(classes, n, bucket):
    hist = {h: Counter() for h in range(24)}
    for i, cl in enumerate(classes):
        h = int((i * bucket) // 3600)
        if 0 <= h < 24:
            hist[h][cl] += 1
    out = {}
    for h in range(24):
        tot = sum(hist[h].values()) or 1
        out["%02d" % h] = {"covered": round((tot - hist[h]["unknown"]) * bucket / 60.0, 1),
                           "unexplained": round(hist[h]["unexplained"] * bucket / 60.0, 1),
                           "sleep": round(hist[h]["sleep"] * bucket / 60.0, 1)}
    return out


def _top_runs(classes, d0, bucket, top):
    runs = _runs([c == "unexplained" for c in classes])
    out = []
    for s, e in runs:
        out.append({"start": _iso(d0 + dt.timedelta(seconds=s * bucket)),
                    "end": _iso(d0 + dt.timedelta(seconds=e * bucket)),
                    "minutes": round((e - s) * bucket / 60.0, 1)})
    out.sort(key=lambda x: -x["minutes"])
    return out[:top]


def _ascii(classes, bucket, d0):
    unit = max(60, bucket)
    per_line = 3600 // unit
    lines = []
    for h in range(24):
        row = []
        for c in range(per_line):
            s = int((h * 3600 + c * unit) / bucket)
            e = int((h * 3600 + (c + 1) * unit) / bucket)
            seg = classes[s:e] or ["unknown"]
            ch = "?"
            for p in DISPLAY_PRECEDENCE:
                if p in seg:
                    ch = CHAR_OF[p]
                    break
            row.append(ch)
        lines.append("%02d|%s|" % (h, "".join(row)))
    return lines


# --------------------------------------------------------------------------
# 渲染 / CLI
# --------------------------------------------------------------------------
def render_text(res):
    L = []
    L.append("== %s  %s  桶=%ds  覆盖=%.2fh  未覆盖=%.2fh" % (
        res["tool"], res["date"], res["bucket_s"],
        res["coverage"]["covered_hours"], res["coverage"]["unknown_minutes"] / 60.0))
    L.append("   交集: %s → %s" % tuple(res["coverage"]["intersection"]))
    for s, v in res["sources"].items():
        if v:
            L.append("   [%s] %s %s cov=%s %s" % (s, v["snapshot_day"],
                                                  Path(v["path"]).name,
                                                  v["coverage"], v["note"]))
        else:
            L.append("   [%s] (无源)" % s)
    m = res["classes"]["minutes"]
    L.append("   分类(分钟): " + "  ".join(
        "%s=%s" % (CLASS_ZH.get(k, k), m[k]) for k in
        ("sleep", "sleep_fg", "fg", "fg_browse", "browse", "unexplained", "unknown")
        if k in m))
    u = res["unexplained"]
    L.append("   ★不可解释: %s 分钟 (占已覆盖 %.2f%%)  屏亮%s/屏熄%s" % (
        u["minutes"], (u["share"] or 0) * 100, u["screen_on_minutes"], u["screen_off_minutes"]))
    for r in u["top_runs"]:
        L.append("     run %s→%s  %s 分钟" % (r["start"][11:], r["end"][11:], r["minutes"]))
    L.append("   kc 前台=%s 分钟  chrome 访问=%s  pmset 睡眠=%s 分钟  wifi 事件=%s" % (
        res["kc"]["fg_minutes"], res["chrome"]["visits"],
        res["pmset"]["sleep_minutes"], res["wifi"].get("n_events")))
    if res["cross_checks"]:
        L.append("   交叉校验: " + json.dumps(res["cross_checks"], ensure_ascii=False))
    if "ascii" in res:
        L.append("   图例  ?=未覆盖  S=睡眠  F=前台  B=浏览器  f=前台∧访问  X=睡眠∧前台  .=不可解释")
        L.extend("   " + ln for ln in res["ascii"])
    for n in res.get("verdict", {}).get("notes", []):
        L.append("   ⚠ " + n)
    return "\n".join(L)


def proof_only(date_s, temp=None, paths=None):
    day = parse_date(date_s)
    srcs = discover_all(Path(temp) if temp else DEFAULT_TEMP, day, paths)
    out = {}
    for k, v in srcs.items():
        if v:
            out[k] = {"path": v["path"], "sha256_16": _sha16(v["path"]),
                      "records": None}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="day_timeline.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("render", help="渲染某日时间线")
    r.add_argument("--date", required=True)
    r.add_argument("--temp", default=None)
    r.add_argument("--bucket", type=int, default=60, choices=list(BUCKETS))
    r.add_argument("--as-of", default=None)
    r.add_argument("--top", type=int, default=8)
    r.add_argument("--json", action="store_true")
    r.add_argument("--out", default=None)
    r.add_argument("--no-ascii", action="store_true")
    r.add_argument("--proof", action="store_true")
    r.add_argument("--kc", default=None)
    r.add_argument("--chrome", default=None)
    r.add_argument("--pmset", default=None)
    r.add_argument("--wifi", default=None)

    p = sub.add_parser("proof", help="只读取证：打印输入文件 sha256")
    p.add_argument("--date", required=True)
    p.add_argument("--temp", default=None)

    sub.add_parser("selftest", help="夹具自测")

    a = ap.parse_args(argv)
    if a.cmd in (None, "selftest"):
        return selftest()
    if a.cmd == "proof":
        print(json.dumps(proof_only(a.date, a.temp), ensure_ascii=False, indent=2))
        return 0
    try:
        # ★ 只有真给出的 --kc/--chrome/--pmset/--wifi 才下发；
        #   未给出必须是「自动发现」，绝不是「显式禁源」（None 在 discover_all 里=禁源）
        _paths = {k: v for k, v in (("kc", a.kc), ("chrome", a.chrome),
                                    ("pmset", a.pmset), ("wifi", a.wifi))
                  if v is not None}
        res, rc, _notes = analyze(a.date, temp=a.temp, bucket=a.bucket, as_of=a.as_of,
                                  top=a.top, ascii_map=not a.no_ascii, proof=a.proof,
                                  paths=_paths)
    except InputError as exc:
        print("ERROR %s" % exc, file=sys.stderr)
        print(json.dumps({"tool": METHOD, "date": a.date, "status": "error",
                          "code": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 2
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render_text(res))
        if a.out:
            print("JSON → %s" % a.out)
    return rc


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _fx_all(base, day):
    """构造四源夹具：kc / chrome / pmset / wifi"""
    base = Path(base)
    (base / "kc_snapshots" / day.isoformat()).mkdir(parents=True, exist_ok=True)
    (base / "chrome_snapshots" / day.isoformat()).mkdir(parents=True, exist_ok=True)
    (base / "pmset_snapshots" / day.isoformat()).mkdir(parents=True, exist_ok=True)
    (base / "wifi_snapshots" / day.isoformat()).mkdir(parents=True, exist_ok=True)
    Y, M, D = day.year, day.month, day.day
    app_rows = [(K._u(Y, M, D, 9, 0, 0), K._u(Y, M, D, 9, 30, 0), "com.alpha"),
                (K._u(Y, M, D, 10, 0, 0), K._u(Y, M, D, 12, 0, 0), "com.beta"),
                (K._u(Y, M, D, 22, 0, 0), K._u(Y, M, D, 23, 0, 0), "com.alpha")]
    bl_rows = [(K._u(Y, M, D, 0, 0, 0), K._u(Y, M, D, 2, 0, 0), 0),      # v=0 ⇒ 熄屏
               (K._u(Y, M, D, 9, 0, 0), K._u(Y, M, D, 23, 0, 0), 1)]     # v=1 ⇒ 亮屏
    kc = base / "kc_snapshots" / day.isoformat() / "knowledgeC.db"
    K._fixture_db(str(kc), app_rows=app_rows, bl_rows=bl_rows,
                  cutoff=K._u(Y, M, D, 23, 0, 0))
    (kc.parent / "manifest.json").write_text("{}", encoding="utf-8")

    ch = base / "chrome_snapshots" / day.isoformat() / "History"
    rows = [("https://a.example/1", C.dt_to_chrome(_loc(day, 14, 0))),
            ("https://b.example/2", C.dt_to_chrome(_loc(day, 14, 5))),
            ("https://a.example/3", C.dt_to_chrome(_loc(day, 20, 0)))]
    C._mkdb(ch, rows)

    pm = base / "pmset_snapshots" / day.isoformat() / "events.json"
    pm.write_text(json.dumps({
        "tool": "test", "files": [{"file": "%04d.%02d.%02d.asl" % (Y, M, D), "events": {}}],
        "windows": [{"enter": "%04d-%02d-%02d 03:00:00" % (Y, M, D),
                     "wake": "%04d-%02d-%02d 03:30:00" % (Y, M, D),
                     "reason": "Clamshell Sleep", "secs": 1800}],
        "summary": {}}), encoding="utf-8")

    wf = base / "wifi_snapshots" / day.isoformat() / "wifi.log"
    wd = [day.strftime("%a %b %d")]
    wf.write_text("\n".join([
        "%s 00:10:00 cuipengdeMacBook-Pro newsyslog[1]: logfile turned over" % wd[0],
        "%s 08:00:00.000 [airport] @[1.0] link up" % wd[0],
        "%s 12:00:00.000 [airport] @[2.0] DHCP ack" % wd[0],
        "%s 20:00:00.000 [airport] @[3.0] roam" % wd[0],
        "%s 23:59:00.000 [airport] @[4.0] roam" % wd[0],
    ]) + "\n", encoding="utf-8")
    return {"kc": str(kc), "chrome": str(ch), "pmset": str(pm.parent),
            "wifi": str(wf)}


def _run_fixture(base, day, **kw):
    return analyze(day.isoformat(), temp=base, **kw)


def _loc(day, hh, mm, ss=0):
    """本机裸本地时刻 → 带本机时区的 aware datetime（chrome epoch 用）"""
    return dt.datetime(day.year, day.month, day.day, hh, mm, ss).astimezone()


def _fx_window(base, day, fg=((9, 0, 0), (9, 30, 0)), bl=(), wake=(3, 30, 0),
               kc_cutoff=(23, 0, 0)):
    """独立目录造一份「可控 kc 窗口」三源夹具（chrome 全日照、pmset 单睡眠窗）"""
    base = Path(base)
    d = day.isoformat()
    for sub in ("kc_snapshots", "chrome_snapshots", "pmset_snapshots"):
        (base / sub / d).mkdir(parents=True, exist_ok=True)
    Y, M, D = day.year, day.month, day.day
    K._fixture_db(str(base / "kc_snapshots" / d / "knowledgeC.db"),
                  app_rows=[(K._u(Y, M, D, fg[0][0], fg[0][1], fg[0][2]),
                             K._u(Y, M, D, fg[1][0], fg[1][1], fg[1][2]), "com.z")],
                  bl_rows=[(K._u(Y, M, D, s[0], s[1], s[2]),
                            K._u(Y, M, D, e[0], e[1], e[2]), v) for (s, e, v) in bl],
                  cutoff=K._u(Y, M, D, kc_cutoff[0], kc_cutoff[1], kc_cutoff[2]))
    ch = base / "chrome_snapshots" / d / "History"
    C._mkdb(ch, [("https://a.example/1", C.dt_to_chrome(_loc(day, 12, 0)))])
    (base / "pmset_snapshots" / d / "events.json").write_text(json.dumps({
        "tool": "test", "files": [{"file": "%04d.%02d.%02d.asl" % (Y, M, D), "events": {}}],
        "windows": [{"enter": "%04d-%02d-%02d 03:00:00" % (Y, M, D),
                     "wake": "%04d-%02d-%02d %02d:%02d:%02d" % ((Y, M, D) + tuple(wake)),
                     "reason": "Clamshell Sleep",
                     "secs": (wake[0] * 3600 + wake[1] * 60 + wake[2]) - 3 * 3600}],
        "summary": {}}), encoding="utf-8")
    return base


def _utime(path, when):
    ts = when.timestamp()
    os.utime(str(path), (ts, ts))


def selftest():
    import shutil
    import tempfile
    ok = fail = 0
    msgs = []

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            msgs.append("  PASS %s" % name)
        else:
            fail += 1
            msgs.append("  FAIL %s %s" % (name, extra))

    day = dt.date(2026, 3, 10)
    Y, M, D = day.year, day.month, day.day
    base = Path(tempfile.mkdtemp(prefix="dt_fx_"))
    try:
        fx = _fx_all(base, day)

        # ---- T1 基准：逐项 = 手工推演（kc cutoff 23:00；睡眠 03:00-03:30）----
        res, rc, notes = _run_fixture(base, day, bucket=60, ascii_map=True, proof=True)
        m = res["classes"]["minutes"]
        check("T1a rc==0", rc == 0, "rc=%s notes=%s" % (rc, notes))
        check("T1b 桶数 1440", res["n_buckets"] == 1440)
        check("T1c 覆盖=1380（kc cutoff 23:00）", res["coverage"]["covered_minutes"] == 1380.0,
              str(res["coverage"]))
        check("T1d 未覆盖=60", res["coverage"]["unknown_minutes"] == 60.0)
        check("T1e 睡眠=30", m.get("sleep") == 30.0, str(m))
        check("T1f 前台=210", m.get("fg") == 210.0, str(m))
        check("T1g 访问=3", m.get("browse") == 3.0, str(m))
        check("T1h 不可解释=1137", m.get("unexplained") == 1137.0, str(m))
        check("T1i 熄屏内的不可解释=120", res["unexplained"]["screen_off_minutes"] == 120.0,
              str(res["unexplained"]["screen_off_minutes"]))
        check("T1j proof 只读", res["proof"]["readonly"] is True)
        check("T1k ascii 24 行", len(res.get("ascii", [])) == 24, str(len(res.get("ascii", []))))
        check("T1l wifi 事件=5", res["wifi"]["n_events"] == 5, str(res["wifi"]["n_events"]))
        # 守恒不变式：任何桶都必落入某一类，绝不丢失/复制（防「补零」）
        check("T1m 分类分钟守恒=1440", abs(sum(m.values()) - 1440.0) < 1e-9, str(sum(m.values())))
        check("T1n 覆盖+未覆盖=1440",
              abs(res["coverage"]["covered_minutes"] + res["coverage"]["unknown_minutes"] - 1440.0) < 1e-9)

        # ---- T1o 对齐面（交集窗口内的原始量，与类统计同源）----
        al = res["aligned"]
        check("T1o 交集窗口=00:00→23:00",
              al["window"] == ["2026-03-10 00:00:00", "2026-03-10 23:00:00"], str(al["window"]))
        check("T1p 窗口内 kc 前台=12600s", al["kc_fg_secs"] == 12600.0, str(al["kc_fg_secs"]))
        check("T1q 窗口内 chrome=3 次", al["chrome_visits"] == 3, str(al["chrome_visits"]))
        check("T1r 窗口内 pmset 睡眠=1800s", al["pmset_sleep_secs"] == 1800.0, str(al["pmset_sleep_secs"]))
        check("T1s 窗口内 kc 熄屏=7200s", al["kc_off_secs"] == 7200.0, str(al["kc_off_secs"]))

        # ---- T2 三方复用器对账（kc_app_usage / sleep_source_crosscheck）----
        cc = res["cross_checks"]
        check("T2a kc 对账可得", "kc_delta_s" in cc, str(cc))
        if "kc_delta_s" in cc:
            check("T2b kc_delta_s==0", cc["kc_delta_s"] == 0.0, str(cc))
        check("T2c pmset 对账可得", "pmset_delta_s" in cc, str(cc))
        if "pmset_delta_s" in cc:
            check("T2d pmset_delta_s<1", cc["pmset_delta_s"] < 1.0, str(cc))

        # ---- T3 睡眠窗口变长：睡眠+60、不可解释-60（互补移动）----
        pm = Path(fx["pmset"]) / "events.json"
        ev = json.loads(pm.read_text(encoding="utf-8"))
        ev["windows"][0]["wake"] = "%04d-%02d-%02d 04:00:00" % (Y, M, D)
        pm.write_text(json.dumps(ev), encoding="utf-8")
        r3, _rc3, _n3 = _run_fixture(base, day, bucket=60, ascii_map=False)
        check("T3 睡眠 30→60 且不可解释 1137→1107",
              r3["classes"]["minutes"].get("sleep") == 60.0 and
              r3["classes"]["minutes"].get("unexplained") == 1107.0,
              str(r3["classes"]["minutes"]))
        ev["windows"][0]["wake"] = "%04d-%02d-%02d 03:30:00" % (Y, M, D)
        pm.write_text(json.dumps(ev), encoding="utf-8")

        # ---- T4 辅源（wifi）缺失：不改判定，只标注 ----
        r4, rc4, _n4 = _run_fixture(base, day, bucket=60, ascii_map=False, paths={"wifi": None})
        check("T4a wifi 缺失 rc=0", rc4 == 0, "rc=%s" % rc4)
        check("T4b 分类逐字不变", r4["classes"]["minutes"] == m,
              "%s vs %s" % (r4["classes"]["minutes"], m))
        check("T4c wifi 记 0 且标注缺失",
              r4["wifi"]["n_events"] == 0 and "缺失" in r4["wifi"].get("note", ""), str(r4["wifi"]))
        check("T4d verdict rc 仍 0", r4["verdict"]["rc"] == 0, str(r4["verdict"]))

        # ---- T5 主源缺失 ⇒ fail-closed rc=2（显式 None / 物理缺文件）----
        try:
            _run_fixture(base, day, paths={"kc": None})
            check("T5a 显式禁 kc ⇒ MISSING_SOURCE", False, "未报错")
        except InputError as exc:
            check("T5a 显式禁 kc ⇒ MISSING_SOURCE", exc.code == "MISSING_SOURCE", exc.code)
        db = base / "kc_snapshots" / day.isoformat() / "knowledgeC.db"
        bak = db.with_suffix(".bak")
        shutil.move(str(db), str(bak))
        try:
            _run_fixture(base, day)
            check("T5b 无 kc 快照 ⇒ MISSING_SOURCE", False, "未报错")
        except InputError as exc:
            check("T5b 无 kc 快照 ⇒ MISSING_SOURCE", exc.code == "MISSING_SOURCE", exc.code)
        finally:
            shutil.move(str(bak), str(db))

        # ---- T5c/T5d CLI 回归：未指定的源 ≠ 显式禁源（曾把 None 下发 ⇒ 全源判缺失）----
        import contextlib as _ctx
        import io as _io
        buf = _io.StringIO()
        with _ctx.redirect_stdout(buf):
            rc_cli = main(["render", "--date", day.isoformat(), "--temp", str(base), "--no-ascii"])
        check("T5c CLI 未指定源 ⇒ 自动发现 rc=0", rc_cli == 0,
              "rc=%s out=%s" % (rc_cli, buf.getvalue()[:160]))
        check("T5c2 CLI 输出非空", len(buf.getvalue()) > 80, str(len(buf.getvalue())))
        buf2 = _io.StringIO()
        with _ctx.redirect_stdout(buf2):
            rc_cli2 = main(["render", "--date", day.isoformat(), "--temp", str(base), "--no-ascii",
                            "--kc", str(base / "kc_snapshots" / day.isoformat() / "knowledgeC.db")])
        check("T5d CLI 显式给正确 --kc 亦 rc=0", rc_cli2 == 0, "rc=%s" % rc_cli2)

        # ---- T5e 缺失提示须给「真快照目录名」（曾误取父目录名 ⇒ 恒显示 kc_snapshots）----
        try:
            analyze(day.isoformat(), temp=base,
                    paths={"kc": None, "chrome": None, "pmset": None})
            check("T5e 三主源禁源 ⇒ MISSING_SOURCE", False, "未报错")
        except InputError as exc:
            check("T5e 三主源禁源 ⇒ MISSING_SOURCE", exc.code == "MISSING_SOURCE", exc.code)
            check("T5e2 提示含真快照目录名 %s" % day.isoformat(),
                  day.isoformat() in exc.detail and "kc_snapshots=" in exc.detail,
                  exc.detail[:200])

        # ---- T6 非法日期 / 非法桶 ⇒ rc=2 ----
        for bad in ("2026-3-10", "20260310", "hello"):
            try:
                analyze(bad, temp=base)
                check("T6 非法日期 %r" % bad, False, "未报错")
            except InputError as exc:
                check("T6 非法日期 %r ⇒INVALID_DATE" % bad, exc.code == "INVALID_DATE", exc.code)
        try:
            analyze(day.isoformat(), temp=base, bucket=7)
            check("T6 非法桶 7 ⇒INVALID_BUCKET", False, "未报错")
        except InputError as exc:
            check("T6 非法桶 7 ⇒INVALID_BUCKET", exc.code == "INVALID_BUCKET", exc.code)

        # ---- T7 --as-of：跨时点混用必须 fail-closed ----
        try:
            _run_fixture(base, day, as_of=dt.datetime(Y, M, D, 12, 0, 0))
            check("T7a as-of 12:00 ⇒MIXED_SNAPSHOT", False, "未报错")
        except InputError as exc:
            check("T7a as-of 12:00 ⇒MIXED_SNAPSHOT", exc.code == "MIXED_SNAPSHOT", exc.code)
        try:
            _r7, rc7, _n7 = _run_fixture(base, day, as_of=dt.datetime(Y, M, D, 23, 59, 30))
            check("T7b as-of 23:59:30（≥四源数据上界）rc=0", rc7 == 0, "rc=%s" % rc7)
        except InputError as exc:
            check("T7b as-of 23:59:30（≥四源数据上界）rc=0", False, exc.code)
        try:
            _r7b, rc7b, _n7b = _run_fixture(base, day, as_of=dt.datetime(Y, M, D + 1, 0, 30, 0))
            check("T7c as-of 次日 00:30 rc=0", rc7b == 0, "rc=%s" % rc7b)
        except InputError as exc:
            check("T7c as-of 次日 00:30 rc=0", False, exc.code)

        # ---- T8 共同覆盖 < 1 桶 ⇒ NO_COMMON_COVERAGE ----
        b8 = _fx_window(base / "w8", day, fg=((0, 0, 0), (0, 0, 20)),
                        kc_cutoff=(0, 0, 20))
        try:
            analyze(day.isoformat(), temp=b8)
            check("T8 共同覆盖 20s ⇒NO_COMMON_COVERAGE", False, "未报错")
        except InputError as exc:
            check("T8 共同覆盖 20s ⇒NO_COMMON_COVERAGE", exc.code == "NO_COMMON_COVERAGE", exc.code)

        # ---- T9 桶粒度：区间类（sleep/fg/unknown）守恒；瞬时类（browse）随粒度变小 ----
        r9, _rc9, _n9 = _run_fixture(base, day, bucket=15, ascii_map=False)
        m9 = r9["classes"]["minutes"]
        check("T9a 区间类守恒(15s vs 60s)",
              all(abs(m9.get(k, 0) - m.get(k, 0)) < 1e-9 for k in ("sleep", "fg", "unknown")),
              str(m9))
        check("T9b 瞬时类不放大(15s≤60s)", m9.get("browse", 0) <= m.get("browse", 0) + 1e-9,
              "%s vs %s" % (m9.get("browse"), m.get("browse")))
        check("T9c 15s 桶分类守恒=1440", abs(sum(m9.values()) - 1440.0) < 1e-9, str(sum(m9.values())))

        # ---- T10 睡眠∧前台冲突可检出 ----
        b10 = _fx_window(base / "w10", day, fg=((3, 0, 0), (3, 20, 0)), wake=(3, 30, 0))
        r10, _rc10, _n10 = analyze(day.isoformat(), temp=b10, ascii_map=False)
        check("T10a 睡眠∧前台冲突=20", r10["classes"]["minutes"].get("sleep_fg") == 20.0,
              str(r10["classes"]["minutes"]))
        check("T10b anomalies 同步", r10["anomalies"]["sleep_and_fg_minutes"] == 20.0,
              str(r10["anomalies"]))

        # ---- T11 覆盖收窄（pmset 副本 mtime=03:00）⇒ 交集外记 unknown，睡眠不计 ----
        b11 = _fx_window(base / "w11", day, fg=((9, 0, 0), (9, 30, 0)),
                         bl=[((0, 0, 0), (2, 0, 0), 0), ((9, 0, 0), (23, 0, 0), 1)])
        _utime(b11 / "pmset_snapshots" / day.isoformat() / "events.json",
               dt.datetime(Y, M, D, 3, 0, 0))
        r11, _rc11, _n11 = analyze(day.isoformat(), temp=b11, ascii_map=False)
        check("T11a 交集收窄到 00:00-03:00（180 分）",
              r11["coverage"]["covered_minutes"] == 180.0, str(r11["coverage"]))
        check("T11b 收窄外记 unknown=1260",
              r11["coverage"]["unknown_minutes"] == 1260.0, str(r11["coverage"]))
        check("T11c 交集外的睡眠不计入", r11["classes"]["minutes"].get("sleep", 0) == 0.0,
              str(r11["classes"]["minutes"]))
        check("T11d 交集内不可解释=180", r11["classes"]["minutes"].get("unexplained") == 180.0,
              str(r11["classes"]["minutes"]))

        # ---- T12 只读取证：跑完源文件 sha 不变 ----
        kc_sha = _sha16(fx["kc"])
        ch_sha = _sha16(fx["chrome"])
        _r12, _rc12, _n12 = _run_fixture(base, day, ascii_map=False, proof=True)
        check("T12 源文件 sha 不变(kc/chrome)",
              _sha16(fx["kc"]) == kc_sha and _sha16(fx["chrome"]) == ch_sha)

        # ---- T13 年界：12/31 行不得被算进 1/1 ----
        d13 = dt.date(2027, 1, 1)
        f13 = _fx_all(base / "f13", d13)
        Path(f13["wifi"]).write_text(
            "Fri Dec 31 23:59:00.000 [airport] @[9] roam\n"
            "Sat Jan 01 00:00:30.000 [airport] @[10] roam\n"
            "Sat Jan 01 12:00:00.000 [airport] @[11] DHCP ack\n", encoding="utf-8")
        wf13 = load_wifi(discover_wifi(base / "f13", d13), d13)
        check("T13a 年界事件归属 1/1 共 2 条", wf13["n_events"] == 2,
              "%s events=%s" % (wf13["n_events"], [str(x) for x in wf13["events"]]))
        check("T13b 12/31 行不被误收", all(t.year == 2027 for t in wf13["events"]),
              str([str(x) for x in wf13["events"]]))

        # ---- T14 chrome 计数与消费器一致 ----
        ch14 = load_chrome(discover_chrome(base, day), day)
        raw = [v for v in C.load_visits(Path(fx["chrome"])) if v[0].date() == day]
        check("T14 chrome 访问数与消费器一致", ch14["n_visits"] == len(raw),
              "%s vs %s" % (ch14["n_visits"], len(raw)))

        # ---- T15 文本渲染 ----
        txt = render_text(res)
        check("T15 渲染含关键行", "不可解释" in txt and "图例" in txt, txt[:120])

        # ---- T16 proof 子命令形状 ----
        pr = proof_only(day.isoformat(), temp=base)
        check("T16 proof 覆盖四源", set(pr) == {"kc", "chrome", "pmset", "wifi"}, str(sorted(pr)))

        # ---- T17 稳定性：同输入两次运行结论逐字一致 ----
        a, _rca, _na = _run_fixture(base, day, ascii_map=False)
        b, _rcb, _nb = _run_fixture(base, day, ascii_map=False)
        a.pop("generated_at", None)
        b.pop("generated_at", None)
        check("T17 两次运行结论一致", json.dumps(a, sort_keys=True, default=str) ==
              json.dumps(b, sort_keys=True, default=str))
    finally:
        shutil.rmtree(str(base), ignore_errors=True)

    print("day_timeline selftest: %d passed / %d failed" % (ok, fail))
    for msg in msgs:
        print(msg)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
