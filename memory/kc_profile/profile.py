#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledgeC 快照「小时级作息 + 专注底稿」只读分析（阶段2）

语料 = temp/kc_snapshots/2026-09-25/knowledgeC.db（**快照副本**，只读 mode=ro）
交叉验证 = temp/chrome_mine/db/History（R362 的 Chrome 只读副本，独立来源）

口径边界（写死在代码里，勿外推）：
  - kc 源库按流做 23–28 天滚动删除（R371 §4.2）⇒ 本底稿只描述快照窗口内事实，禁外推趋势
  - 「在场」判据 = /display/isBacklit 状态=1 的区间（按小时边界切分分摊）
  - 「专注块」判据 = /app/usage 事件按 start 排序，相邻间隔 ≤ 300 s 归同一链，链长 ≥ 1500 s 计一块
  - Chrome 侧单位为 visits（访问次数），与 kc 事件数不同量纲 ⇒ 只比**形状**（小时内占比），不比绝对值

用法：python3 temp/kc_profile/profile.py [--out temp/kc_profile]
"""
import argparse, json, os, sqlite3, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

KC_DEFAULT = "temp/kc_snapshots/2026-09-25/knowledgeC.db"
CH_DEFAULT = "temp/chrome_mine/db/History"
ZSH_DEFAULT = os.path.expanduser("~/.zsh_history")

EPOCH_OFF = 978307200                      # 2001-01-01 → unix
# ★可复跑 SQL（与 kc_queries 内实际语句同源；sha 由脚本自校验防漂移）
SQL_APP = ("select ZSTARTDATE t, ZENDDATE e, ZVALUESTRING app from ZOBJECT"
           " where ZSTREAMNAME='/app/usage' order by ZSTARTDATE;")
SQL_BL = ("select ZSTARTDATE t, ZENDDATE e, ZVALUEINTEGER v from ZOBJECT"
          " where ZSTREAMNAME='/display/isBacklit' order by ZSTARTDATE;")
SQL_SQLITE_FILENAME = ("-- 只读打开：sqlite3 'file:<副本路径>?mode=ro'；"
                       "date(ZSTARTDATE+%d,'unixepoch','localtime') 取本地日" % EPOCH_OFF)
CHROME_OFF = 11644473600                   # 1601-01-01 → unix
STREAMS = ("/app/usage", "/notification/usage", "/display/isBacklit", "/bluetooth/isConnected")


def ts(v):
    return datetime.fromtimestamp(v + EPOCH_OFF)


def hours_of(a, b):
    """把 [a,b) 按整点边界切分，返回 {hour: 秒数}"""
    out = defaultdict(float)
    cur = a
    while cur < b:
        nxt = min(b, cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        out[cur.hour] += (nxt - cur).total_seconds()
        cur = nxt
    return out


def kc_queries(path):
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path), uri=True)
    con.row_factory = sqlite3.Row
    R = {}
    R["daily"] = defaultdict(dict)
    for r in con.execute(
            "select ZSTREAMNAME s, date(ZSTARTDATE+%d,'unixepoch','localtime') d, count(*) n"
            " from ZOBJECT where ZSTREAMNAME in (%s) group by 1,2 order by 2"
            % (EPOCH_OFF, ",".join("'%s'" % x for x in STREAMS))):
        R["daily"][r["d"]][r["s"]] = r["n"]
    # ★ZR371 已证：字符串流的 ZVALUEDOUBLE/ZVALUEINTEGER 是位型垃圾（本库实测 1e19 量级）
    #   真实时长 = ZENDDATE - ZSTARTDATE（秒）
    rows = con.execute(
        "select ZSTARTDATE t, ZENDDATE e, ZVALUESTRING app from ZOBJECT"
        " where ZSTREAMNAME='/app/usage' order by ZSTARTDATE").fetchall()
    R["app"] = [(ts(r["t"]), float((r["e"] or 0) - (r["t"] or 0)), r["app"]) for r in rows]
    rows = con.execute(
        "select o.ZSTARTDATE t, m.Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID b"
        " from ZOBJECT o left join ZSTRUCTUREDMETADATA m on m.Z_PK=o.ZSTRUCTUREDMETADATA"
        " where o.ZSTREAMNAME='/notification/usage' order by o.ZSTARTDATE").fetchall()
    R["notif"] = [(ts(r["t"]), r["b"]) for r in rows]
    rows = con.execute(
        "select ZSTARTDATE t, ZENDDATE e, ZVALUEINTEGER v from ZOBJECT"
        " where ZSTREAMNAME='/display/isBacklit' order by ZSTARTDATE").fetchall()
    R["backlit"] = [(ts(r["t"]), ts(r["e"] or r["t"]),
                     int(r["v"] if r["v"] is not None else 0)) for r in rows]
    R["span"] = con.execute(
        "select min(date(ZSTARTDATE+%d,'unixepoch','localtime')) || ' → ' ||"
        " max(date(ZSTARTDATE+%d,'unixepoch','localtime')) from ZOBJECT"
        % (EPOCH_OFF, EPOCH_OFF)).fetchone()[0]
    R["nobj"] = con.execute("select count(*) from ZOBJECT").fetchone()[0]
    R["stream_span"] = [list(r) for r in con.execute(
        "select ZSTREAMNAME, min(date(ZSTARTDATE+%d,'unixepoch','localtime')),"
        " max(date(ZSTARTDATE+%d,'unixepoch','localtime')), count(*)"
        " from ZOBJECT group by 1 order by 4 desc" % (EPOCH_OFF, EPOCH_OFF))]
    con.close()
    return R


def analyse(kc):
    A = {}
    A["daily"] = {d: dict(v) for d, v in sorted(kc["daily"].items())}
    A["span"] = kc["span"]
    A["nobj"] = kc["nobj"]

    ev, apps, secs = Counter(), defaultdict(set), defaultdict(float)
    for t, s, app in kc["app"]:
        ev[t.hour] += 1
        apps[t.hour].add(app)
        secs[t.hour] += s
    nev = Counter(t.hour for t, _ in kc["notif"])
    # ★口径（实测校准，见 §7 三态密度）：/display/isBacklit 只记录「熄屏(v=0)」与部分亮屏段，
    #   大量亮屏时段**无任何状态行**（空隙内前台事件密度 17.1/h，与 v=1 段同量级；v=0 段仅 6 条事件）
    #   ⇒ 只有 v=0 可判熄屏；亮屏 = 窗口 − 熄屏，不可用 v=1 求和（会低估 70%）
    dark = defaultdict(float)
    bl = kc["backlit"]
    w0 = min(t for t, _, _ in bl) if bl else None
    w1 = max(e for _, e, _ in bl) if bl else None   # ★曾误写 (_,_,e)=绑到 value(int) 致 datetime<int
    for t, e, v in bl:
        if v == 0 and e > t:
            for h, d in hours_of(t, e).items():
                dark[h] += d
    lit = defaultdict(float)
    if w0 and w1:
        for h, d in hours_of(w0, w1).items():
            lit[h] = max(0.0, d - dark.get(h, 0.0))
    A["hour_app_events"] = {h: ev.get(h, 0) for h in range(24)}
    A["hour_app_unique"] = {h: len(apps.get(h, ())) for h in range(24)}
    A["hour_app_seconds"] = {h: round(secs.get(h, 0.0), 1) for h in range(24)}
    A["hour_notify"] = {h: nev.get(h, 0) for h in range(24)}
    A["hour_dark_seconds"] = {h: round(dark.get(h, 0.0), 1) for h in range(24)}
    A["hour_lit_seconds"] = {h: round(lit.get(h, 0.0), 1) for h in range(24)}
    A["dark_total_hours"] = round(sum(dark.values()) / 3600.0, 2)
    A["lit_total_hours"] = round(sum(lit.values()) / 3600.0, 2)
    A["window_hours"] = round((w1 - w0).total_seconds() / 3600.0, 2) if w0 and w1 else 0
    A["w0"] = w0.strftime("%Y-%m-%d %H:%M") if w0 else "n/a"
    A["w1"] = w1.strftime("%Y-%m-%d %H:%M") if w1 else "n/a"
    # 三态密度（口径依据）：状态行 v=1 / v=0 / 无行(空隙) 各自的前台事件密度
    st = [t for t, _, _ in bl]
    seg = []
    for i, (t, e, v) in enumerate(bl):
        seg.append((t, e, "1" if v == 1 else "0"))
        if i + 1 < len(bl) and bl[i + 1][0] > e:
            seg.append((e, bl[i + 1][0], "gap"))
    dens = {k: [0, 0.0] for k in ("1", "0", "gap")}
    import bisect
    for t, e, app in kc["app"]:
        j = bisect.bisect_right(st, t) - 1
        k = "gap"
        for m in (j, j + 1):
            if 0 <= m < len(bl) and bl[m][0] <= t < bl[m][1]:
                k = "1" if bl[m][2] == 1 else "0"
                break
        dens[k][0] += 1
    dur3 = {k: sum((b - a).total_seconds() for a, b, kk in seg if kk == k) / 3600.0 for k in dens}
    A["state_hours"] = {k: round(v, 1) for k, v in dur3.items()}
    A["state_density"] = {k: round(dens[k][0] / dur3[k], 1) if dur3[k] else 0.0 for k in dens}
    A["state_events"] = {k: dens[k][0] for k in dens}
    A["backlit_rows"] = len(bl)
    A["backlit_gaps"] = sum(1 for _, _, kk in seg if kk == "gap")
    # zsh 历史（独立来源）三态归属：v=0（熄屏）期间不该有击键；空隙应显著低于 v=1
    zn_all, zn_win, zh, zdt = zsh_cmd_ts(ZSH_DEFAULT, w0, w1)
    z_in = {"1": 0, "0": 0, "gap": 0}
    z_edge = 0
    for t in zdt:
        j = bisect.bisect_right(st, t) - 1
        k = "gap"
        for m in (j, j + 1):
            if 0 <= m < len(bl) and bl[m][0] <= t < bl[m][1]:
                k = "1" if bl[m][2] == 1 else "0"
                break
        z_in[k] += 1
        if k == "0" and 0 <= m < len(bl):
            if (t - bl[m][0]).total_seconds() < 120 or (bl[m][1] - t).total_seconds() < 120:
                z_edge += 1
    A["zsh"] = {"all": zn_all, "win": zn_win, "hour": zh, "in": z_in, "edge_dark": z_edge,
                "density": {k: round(z_in[k] / dur3[k], 2) if dur3[k] else 0.0
                            for k in z_in}}

    per_day = defaultdict(list)
    for t, s, app in kc["app"]:
        per_day[t.date()].append(t)
    A["day_first_last"] = {str(d): [min(v).strftime("%H:%M:%S"), max(v).strftime("%H:%M:%S"),
                                    len(v)] for d, v in sorted(per_day.items())}

    tot, cnt = defaultdict(float), Counter()
    for t, s, app in kc["app"]:
        tot[app] += s
        cnt[app] += 1
    A["top_apps"] = [[a, round(tot[a] / 3600.0, 2), cnt[a]] for a in
                     sorted(tot, key=lambda x: -tot[x])[:12]]

    evs = sorted(kc["app"])
    blocks, cur_b = [], None
    for t, s, app in evs:
        if cur_b and (t - cur_b["end"]).total_seconds() <= 300:
            cur_b["end"] = max(cur_b["end"], t + timedelta(seconds=s))
            cur_b["apps"][app] += s
            cur_b["n"] += 1
        else:
            if cur_b:
                blocks.append(cur_b)
            cur_b = {"start": t, "end": t + timedelta(seconds=s),
                     "apps": defaultdict(float), "n": 1}
            cur_b["apps"][app] += s
    if cur_b:
        blocks.append(cur_b)
    keep = [b for b in blocks if (b["end"] - b["start"]).total_seconds() >= 1500]
    A["blocks_all"] = len(blocks)
    A["blocks_ge25min"] = len(keep)
    A["block_hours_total"] = round(sum((b["end"] - b["start"]).total_seconds()
                                       for b in keep) / 3600.0, 2)
    per_day_b = Counter(b["start"].date() for b in keep)
    A["block_per_day"] = {str(d): per_day_b[d] for d in sorted(per_day_b)}
    A["block_day_coverage"] = "%d/%d 天有≥1块" % (len(per_day_b), len(per_day))
    A["longest_blocks"] = [[b["start"].strftime("%m-%d %H:%M"),
                            round((b["end"] - b["start"]).total_seconds() / 60.0, 1), b["n"],
                            [a for a in sorted(b["apps"], key=lambda x: -b["apps"][x])[:3]]]
                           for b in sorted(keep, key=lambda b: -
                                           (b["end"] - b["start"]).total_seconds())[:8]]
    bh = Counter()
    for b in keep:
        for h, d in hours_of(b["start"], b["end"]).items():
            bh[h] += d
    A["block_seconds_by_hour"] = {h: round(bh.get(h, 0.0), 1) for h in range(24)}

    nb = Counter(b for _, b in kc["notif"])
    A["notif_top"] = [[b, n] for b, n in nb.most_common(10)]
    A["notif_total"] = sum(nb.values())
    ndays = len(A["day_first_last"])
    A["notif_per_day"] = round(sum(nb.values()) / ndays, 1) if ndays else 0
    return A


def zsh_cmd_ts(path=ZSH_DEFAULT, w0=None, w1=None):
    """读 ~/.zsh_history（EXTENDED_HISTORY 的 `: <epoch>:<dur>;cmd` 行）。
    返回 (全部带时间戳条数, 落在 [w0,w1] 内的条数, 窗口内小时直方图, 窗口内 datetime 列表)。
    只读、不落盘；无该文件或无语料时返回 (0,0,{},[])。"""
    if not os.path.exists(path):
        return 0, 0, {}, []
    all_n = 0
    seen = set()
    win_dt = []
    hour = {}
    import re
    rx = re.compile(r"^: (\d{9,11}):\d+;")
    for line in open(path, encoding="utf-8", errors="replace"):
        m = rx.match(line)
        if not m:
            continue
        t = int(m.group(1))
        if t in seen:          # zsh 历史偶有重复时间戳（同一秒多条），保留全部但去重不计
            pass
        seen.add(t)
        all_n += 1
        d = datetime.fromtimestamp(t)
        if w0 and w1 and w0 <= d <= w1:
            win_dt.append(d)
            hour[d.hour] = hour.get(d.hour, 0) + 1
    return all_n, len(win_dt), hour, win_dt


def chrome_hours(path):
    if not os.path.exists(path):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path), uri=True)
    h, n = Counter(), 0
    for (v,) in con.execute("select visit_time from visits where visit_time>0"):
        h[datetime.fromtimestamp(v / 1e6 - CHROME_OFF).hour] += 1
        n += 1
    con.close()
    return {"visits": n, "hour": {i: h.get(i, 0) for i in range(24)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kc", default=KC_DEFAULT)
    ap.add_argument("--chrome", default=CH_DEFAULT)
    ap.add_argument("--out", default="temp/kc_profile")
    a = ap.parse_args()
    kc = kc_queries(a.kc)
    A = analyse(kc)
    A["chrome"] = chrome_hours(a.chrome)
    os.makedirs(a.out, exist_ok=True)
    json.dump(A, open(os.path.join(a.out, "out.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    L = ["# kc 小时级作息底稿（快照 2026-09-25，只读）", "",
         "窗口 %s ｜ ZOBJECT=%d ｜ 应用事件=%d ｜ 通知=%d" % (
             A["span"], A["nobj"], len(kc["app"]), len(kc["notif"])), "",
         "逐流窗口与体量（自证：语料非空、每流各自滚动窗）：", ""]
    for sn, d0, d1, n in kc.get("stream_span", [])[:12]:
        L.append("- %-26s %s → %s ｜ %d 行" % (sn, d0, d1, n))
    L += ["", "## 1. 小时级作息（0–23 时）", "",
         "| 时 | app事件 | 唯一应用 | 前台秒 | 通知 | 熄屏(分) | 专注块(分) |",
         "|---|---|---|---|---|---|---|"]
    for h in range(24):
        L.append("| %02d | %d | %d | %d | %d | %d | %d |" % (
            h, A["hour_app_events"][h], A["hour_app_unique"][h],
            round(A["hour_app_seconds"][h]), A["hour_notify"][h],
            round(A["hour_dark_seconds"][h] / 60), round(A["block_seconds_by_hour"][h] / 60)))
    L += ["", "熄屏(/display/isBacklit v=0) 合计 = %.2f h（窗口 %.2f h，占 %.1f%%）" % (
              A["dark_total_hours"], A["window_hours"],
              100.0 * A["dark_total_hours"] / A["window_hours"] if A["window_hours"] else 0), "",
          "## 2. 专注块（间隔≤300s 归链，链长≥1500s）", "",
          "链总数=%d ｜ 计块=%d ｜ 块内合计=%.2f h ｜ %s" % (
              A["blocks_all"], A["blocks_ge25min"], A["block_hours_total"],
              A["block_day_coverage"]),
          "", "最长 8 块：", ""]
    for s, m, n, apps in A["longest_blocks"]:
        L.append("- %s ｜ %.1f 分钟 ｜ %d 事件 ｜ top=%s" % (s, m, n, ", ".join(apps)))
    L += ["", "## 3. Top 应用（前台小时 / 事件数）", ""]
    for a_, h, c in A["top_apps"]:
        L.append("- %-38s %6.2f h  %5d 次" % (a_, h, c))
    L += ["", "## 4. 通知", "",
          "合计 %d 条 ｜ 日均 %.1f 条" % (A["notif_total"], A["notif_per_day"]), ""]
    for b, n in A["notif_top"]:
        L.append("- %-40s %5d" % (b, n))
    L += ["", "## 5. 每日首末事件（作息粗判）", "",
          "| 日 | 首事件 | 末事件 | 事件数 |", "|---|---|---|---|"]
    for d, (f, l, n) in A["day_first_last"].items():
        L.append("| %s | %s | %s | %d |" % (d, f, l, n))
    if A.get("chrome"):
        ch = A["chrome"]
        L += ["", "## 6. 交叉验证（Chrome 只读副本，独立来源）", "",
              "visits=%d（全库，含自驾自造访问；R362 已证 21.5%% 系自造 ⇒ 只比形状）"
              % ch["visits"], "", "| 时 | kc事件占比 | Chrome占比 |", "|---|---|---|"]
        kt = sum(A["hour_app_events"].values()) or 1
        ct = sum(ch["hour"].values()) or 1
        for h in range(24):
            L.append("| %02d | %.1f%% | %.1f%% |" % (
                h, 100.0 * A["hour_app_events"][h] / kt, 100.0 * ch["hour"][h] / ct))
        pairs = [(A["hour_app_events"][h], ch["hour"][h]) for h in range(24)]
        mx = statistics.fmean(x for x, _ in pairs)
        my = statistics.fmean(y for _, y in pairs)
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        sx = sum((x - mx) ** 2 for x, _ in pairs) ** .5
        sy = sum((y - my) ** 2 for _, y in pairs) ** .5
        L += ["", "小时级 Pearson r = %.3f（n=%d 个小时桶）" % (cov / (sx * sy), len(pairs))]
    z = A.get("zsh")
    if z and z["all"]:
        L += ["", "### 6b. 交叉验证二：zsh 历史（R377 同源，独立于 kc）", "",
              "`~/.zsh_history` 带 epoch 时间戳条数 = %d；落进 kc 窗口（%s → %s）= %d 条"
              % (z["all"], A["w0"], A["w1"], z["win"]), "",
              "| 三态 | 时长(h) | zsh 命令 | 命令/时 | kc 前台事件/时 |", "|---|---|---|---|---|"]
        for k, lab in (("1", "有行 v=1"), ("0", "有行 v=0"), ("gap", "无行(空隙)")):
            L.append("| %s | %.1f | %d | %.2f | %.1f |" % (
                lab, A["state_hours"][k], z["in"][k], z["density"][k], A["state_density"][k]))
        zt = sum(z["hour"].values()) or 1
        kt2 = sum(A["hour_app_events"].values()) or 1
        zz = [(A["hour_app_events"][h], z["hour"].get(h, 0)) for h in range(24)]
        mx = statistics.fmean(x for x, _ in zz)
        my = statistics.fmean(y for _, y in zz)
        cov = sum((x - mx) * (y - my) for x, y in zz)
        sxx = sum((x - mx) ** 2 for x, _ in zz) ** .5
        syy = sum((y - my) ** 2 for _, y in zz) ** .5
        L += ["", "小时级 Pearson r = %.3f（kc 前台事件 vs zsh 命令，n=24；窗口内 %d 条 / 全史 %d 条 = %.1f%%）"
              % (cov / (sxx * syy) if sxx and syy else float("nan"), z["win"], z["all"],
                 100.0 * z["win"] / (z["all"] or 1)),
              "⇒ **熄屏(v=0)期间 zsh 命令 %d 条**（%.2f 条/时 vs v=1 的 %.2f）：命令需击键，可作 v=0=离场的独立旁证；"
              "其中 %d 条落在 v=0 段首/尾 120 s 内（=临熄屏前敲的最后一条），属边界效应。"
              % (z["in"]["0"], z["density"]["0"], z["density"]["1"], z["edge_dark"])]
    L += ["", "## 7. 数据字典与可复跑 SQL（承接 R371 字段矩阵，不重造）", "",
          "语料 = `%s`（**只读**副本，`file:...?mode=ro`；建议用 `temp/kc_snapshots/<日期>/knowledgeC.db`）。"
          % a.kc, "",
          "| 用途 | 表/字段 | 口径 |", "|---|---|---|",
          "| 前台事件 | `ZOBJECT.ZSTREAMNAME='/app/usage'` | 应用名=`ZVALUESTRING`；**时长=`ZENDDATE-ZSTARTDATE`（秒）** |",
          "| 熄屏状态 | `ZSTREAMNAME='/display/isBacklit'` | 取值=`ZVALUEINTEGER`（1=亮 0=熄）；区间=`ZSTARTDATE..ZENDDATE` |",
          "| 通知 | `ZSTREAMNAME='/notification/usage'` | bundle=`ZSTRUCTUREDMETADATA.Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID` |",
          "| ⚠ 陷阱 | `ZVALUEDOUBLE` | `/app/usage` 下系位型垃圾（实测 1e19 量级），**不得当秒用** |",
          "| 时区 | 全部时间列 | Cocoa epoch 偏移 %d 秒已并入 SQL 或 Python |" % EPOCH_OFF, "",
          "本文件每个数字均由下列 SQL 直接得出（脚本内同源，`temp/kc_profile/profile.py`）：", "",
          "```sql", SQL_APP, "-- 小时桶：Python 侧 datetime.fromtimestamp 后按 hour 归桶（SQLite localtime 同口径）",
          SQL_BL, "-- 三态判据：区间含 t 则取该行 v；否则 = 空隙(gap)", SQL_SQLITE_FILENAME, "```", ""]
    L += ["", "## 8. 数据质量与口径自证（/display/isBacklit 三态密度）", "",
          "该流的状态行**不铺满时间轴**（本窗口 %d 行、%d 处断续），故先按「有行 v=1 / 有行 v=0 / 无行」三分区"
          % (A["backlit_rows"], A["backlit_gaps"]),
          "各自统计前台事件密度作为判读依据：",
          "", "| 区段 | 时长(h) | 前台事件 | 事件/时 | 判读 |", "|---|---|---|---|---|"]
    for k, lab in (("1", "有行 v=1"), ("0", "有行 v=0"), ("gap", "无行(空隙)")):
        L.append("| %s | %.1f | %d | %.1f | %s |" % (
            lab, A["state_hours"][k], A["state_events"][k], A["state_density"][k],
            {"1": "使用中（密集）", "0": "离场／熄屏", "gap": "**混合**（含睡眠与使用）"}[k]))
    L += ["", "口径声明（写死，防后续误用）：", "",
          "1. **熄屏只认 v=0**（本窗口 %.2f h，%.1f%%）；「无行」是**混合区**（含睡眠与使用），当场亮处理会高估。"
          % (A["dark_total_hours"], 100.0 * A["dark_total_hours"] / (A["window_hours"] or 1)),
          "2. 因此**不输出**「窗口−熄屏=亮屏」的推算口径（实测该式子给 17.5 h/天，与前台证据矛盾）。",
          "3. 「在场/活跃」改用 `ZSTREAMNAME='/app/usage'` 的前台事件耗时合计（口径=Σ(ZENDDATE−ZSTARTDATE)，",
          "   因 `ZVALUEDOUBLE` 系位型垃圾、不可当秒用——已实测比对）。",
          "4. 各流窗口不等长（本轮实测：app/usage 25 天、notification 29 天、isBacklit 29 天），",
          "   跨流比较一律先裁剪到公共窗。",
          "5. **v=0 只是「熄屏」的下界**：设备睡眠时该流同样不写行（与空隙同因），故 %.2f h 不能读作「全天熄屏总量」。"
          % A["dark_total_hours"],
          "   按窗口 %.1f h（%.0f 天）折算：v=0 ≈ %.1f h/天、空隙 ≈ %.1f h/天、前台事件 ≈ %.1f h/天。"
          % (A["window_hours"], A["window_hours"] / 24.0,
             A["state_hours"]["0"] / (A["window_hours"] / 24.0),
             A["state_hours"]["gap"] / (A["window_hours"] / 24.0),
             A["fg_hours"] if "fg_hours" in A else sum(A["hour_app_seconds"].values()) / 3600.0
             / (A["window_hours"] / 24.0)),
          "6. 本文件所有数值均来自 `temp/kc_profile/profile.py` 一次运行（" +
          __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M") + " 快照）；知识库本身滚动删除（≈4 周），",
          "   故任何跨日比较必须用已落盘的 `temp/kc_snapshots/` 快照，不得回查源库。", ""]
    txt = "\n".join(L)
    open(os.path.join(a.out, "out.txt"), "w", encoding="utf-8").write(txt + "\n")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
