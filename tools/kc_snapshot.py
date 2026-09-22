#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledgeC 幂等快照器 + 数字行为底稿（只读源库）

背景：macOS 的 knowledgeC.db（App 使用 / 通知 / 屏幕点亮 / 蓝牙）实测只保留约 4 周，
      滚动删除 ⇒「越早开始累积越值」。本工具做两件事：
      1) snapshot：把源库连 -wal/-shm 一起拷成**自包含单文件**快照，同日幂等，累积成序列；
      2) report  ：对某份快照算「数字行为底稿」（全真数字，无占位符）。

纪律：
  - 源库**只读**：绝不写源目录、不 checkpoint 源、不 VACUUM 源；所有整理都发生在副本上。
  - 侧车文件名必须是 `<db>-wal` / `<db>-shm`（连字符）。写成 `.wal`（点号）SQLite 不认，
    会造成「副本假空」——R344 的 db_copy 就踩过这个坑。
  - 本工具不注册任何定时任务（launchd/反射器注册属越权，单列为待批项）。

用法：
  python3 tools/kc_snapshot.py snapshot [--date YYYY-MM-DD] [--force]
  python3 tools/kc_snapshot.py report   [--snapshot DIR|latest] [--out FILE]
  python3 tools/kc_snapshot.py list
  python3 tools/kc_snapshot.py verify   [--snapshot DIR|latest]
  python3 tools/kc_snapshot.py prune    [--keep-days 14] [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys

SRC = os.path.expanduser("~/Library/Application Support/Knowledge/knowledgeC.db")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "temp", "kc_snapshots")
ROOT = os.path.abspath(ROOT)
SNAPSHOT_NAME = "knowledgeC.db"            # 快照内的单文件名（自包含，无侧车）
SIDECARS = ["-wal", "-shm"]
KEEP_DAYS_DEFAULT = 14                      # 近 N 天全量保留；更早只留每 ISO 周首份 + 首份


# ---------------------------------------------------------------- 基础工具
def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp(path: str) -> dict:
    st = os.stat(path)
    return {
        "path": path,
        "bytes": st.st_size,
        "mtime": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def day() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def snap_dir(d: str | None) -> str:
    """解析 --snapshot 参数：None/latest → 最新一份；否则对应日期目录。"""
    if d in (None, "latest"):
        items = sorted(x for x in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, x)) and len(x) == 10)
        if not items:
            sys.exit("没有快照目录，先跑 snapshot")
        return os.path.join(ROOT, items[-1])
    if os.path.isdir(d):
        return os.path.abspath(d)
    return os.path.join(ROOT, d)


def open_snap(d: str) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % os.path.join(d, SNAPSHOT_NAME), uri=True)


def counts(con: sqlite3.Connection) -> dict:
    c = con.cursor()
    total = c.execute("select count(*) from ZOBJECT").fetchone()[0]
    dates = [r[0] for r in c.execute(
        "select date(ZSTARTDATE+978307200,'unixepoch','localtime') d from ZOBJECT group by d order by d")]
    return {"objects": total, "distinct_start_days": len(dates), "first_day": dates[0] if dates else None,
            "last_day": dates[-1] if dates else None}


# ---------------------------------------------------------------- 断言/校验
def assert_sidecars_ok(d: str) -> list[str]:
    """副本自洽性：不应残留侧车文件（单文件快照）"""
    leftovers = [f for f in os.listdir(d) if f.startswith(SNAPSHOT_NAME) and f != SNAPSHOT_NAME]
    return leftovers


# ---------------------------------------------------------------- snapshot
def cmd_snapshot(args) -> int:
    if not os.path.exists(SRC):
        sys.exit("源库不存在: %s" % SRC)
    dest = os.path.join(ROOT, args.date or day())
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, ".incoming.db")
    src_before = {s: _stamp(SRC + s) if os.path.exists(SRC + s) else None for s in [""] + SIDECARS}

    # 1) 拷贝主库 + 侧车（侧车名必须带连字符）
    shutil.copy2(SRC, tmp)
    for s in SIDECARS:
        if os.path.exists(SRC + s):
            shutil.copy2(SRC + s, tmp + s)

    # 2) 在**副本**上 checkpoint，把 WAL 折进主文件 → 自包含
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # ★必须切回 DELETE 日志模式：WAL 模式的库用只读连接打开时无法创建 -shm，
        #   实测报 "unable to open database file"。切回后才是真·自包含单文件。
        jm = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        con.commit()
        integ = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    for s in SIDECARS:
        p = tmp + s
        if os.path.exists(p):
            os.remove(p)

    new_hash = sha256(tmp)
    main = os.path.join(dest, SNAPSHOT_NAME)
    for s in SIDECARS:   # 清理历史残留侧车（例如早期 bug 版本留下的 -shm）
        if os.path.exists(main + s):
            os.remove(main + s)

    manifest_path = os.path.join(dest, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    revs = manifest.get("revisions", [])
    if revs and revs[-1]["sha256"] == new_hash and not args.force:
        os.remove(tmp)
        print("[skip] 内容与上次快照一致（sha=%s…），不重复写入" % new_hash[:12])
        return 0

    os.replace(tmp, main)
    if not os.path.exists(main):  # 理论不可达
        sys.exit("快照写入失败")
    c = counts(sqlite3.connect("file:%s?mode=ro" % main, uri=True))
    revs.append({
        "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sha256": new_hash,
        "bytes": os.path.getsize(main),
        "integrity": integ,
        "counts": c,
        "src_before": src_before,
    })
    manifest = {
        "snapshot": SNAPSHOT_NAME,
        "date": os.path.basename(dest),
        "source": SRC,
        "policy": {"keep_all_days": KEEP_DAYS_DEFAULT, "then": "每 ISO 周首份 + 首份永久保留"},
        "revisions": revs,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("[ok] 快照 %s  sha=%s…  %s bytes  integrity=%s" % (
        dest, new_hash[:12], revs[-1]["bytes"], integ))
    print("     对象数=%s  覆盖日期=%s 天 (%s → %s)  本次 revision 数=%d" % (
        c["objects"], c["distinct_start_days"], c["first_day"], c["last_day"], len(revs)))
    leftover = assert_sidecars_ok(dest)
    print("     侧车残留: %s" % (leftover or "无（自包含单文件）"))
    return 0


# ---------------------------------------------------------------- report
APP = "select ZVALUESTRING app, ZSTARTDATE, ZENDDATE from ZOBJECT where ZSTREAMNAME='/app/usage'"
NOTIF = ("select s.Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID b, o.ZSTARTDATE from ZOBJECT o "
         "left join ZSTRUCTUREDMETADATA s on s.Z_PK=o.ZSTRUCTUREDMETADATA "
         "where o.ZSTREAMNAME='/notification/usage'")
H = lambda s: s / 3600.0


def cmd_report(args) -> int:
    d = snap_dir(args.snapshot)
    con = open_snap(d)
    c = con.cursor()
    man = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    rev = man["revisions"][-1]
    L: list[str] = []
    A = L.append

    A("# knowledgeC「数字行为底稿」（快照 %s）" % man["date"])
    A("")
    A("- 生成时间：%s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    A("- 快照：`%s` sha256=%s…  %s bytes  integrity=%s" % (
        os.path.join(d, SNAPSHOT_NAME), rev["sha256"][:16], rev["bytes"], rev["integrity"]))
    A("- 源库（只读）：`%s`" % man["source"])
    A("- 口径：全部为**真数字**；时间从 Core Data 纪元(+978307200s) 转本地时区")
    A("")

    # 1. 覆盖与保留期量化
    rows = c.execute("select date(ZSTARTDATE+978307200,'unixepoch','localtime') d, count(*) "
                     "from ZOBJECT group by d order by d").fetchall()
    dates = [r[0] for r in rows]
    d0, d1 = dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1])
    span = (d1 - d0).days + 1
    # 找最长的连续日段（= 真正有效的滚动窗口）
    best = cur = [dates[0]]
    for a, b in zip(dates, dates[1:]):
        if (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days == 1:
            cur.append(b)
        else:
            cur = [b]
        if len(cur) > len(best):
            best = list(cur)
    A("## 1. 数据覆盖 / 保留期（量化「约 4 周滚动删除」）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A("| 总对象数 ZOBJECT | %d |" % sum(r[1] for r in rows))
    A("| 出现数据的日期数 | %d 天 |" % len(dates))
    A("| 最早 / 最晚 | %s / %s |" % (dates[0], dates[-1]))
    A("| 跨度（含空洞） | %d 天 |" % span)
    A("| 最长**连续**日段 | %d 天（%s → %s） |" % (len(best), best[0], best[-1]))
    A("")
    A("每日对象数（全部 %d 天）：" % len(rows))
    A("")
    for dte, n in rows:
        bar = "▇" * max(1, round(n / 20))
        A("- %s  %5d  %s" % (dte, n, bar))
    A("")
    A("> 解读：稀疏/孤立的早期日期（<100 条）是残留桶，真正的连续窗口只有 **%d 天** —— 与 subagent"
      "复算的「4 周滚动删除」一致。**每过一天就永久丢失一天**，故快照只能靠现在开始每日累积。" % len(best))
    A("")

    # 2. top app
    A("## 2. App 使用时长 Top 15（/app/usage）")
    A("")
    used = c.execute("select count(distinct ZVALUESTRING) from ZOBJECT where ZSTREAMNAME='/app/usage'").fetchone()[0]
    tot_sec = c.execute("select sum(ZENDDATE-ZSTARTDATE) from ZOBJECT where ZSTREAMNAME='/app/usage'").fetchone()[0] or 0
    sess = c.execute("select count(*) from ZOBJECT where ZSTREAMNAME='/app/usage'").fetchone()[0]
    A("共 **%d** 个不同 App、**%d** 个前台会话、累计 **%.1f 小时**。" % (used, sess, H(tot_sec)))
    A("")
    A("| # | App (bundle id) | 时长 h | 会话数 | 平均会话 s | 占比 |")
    A("|---|---|---|---|---|---|")
    top = c.execute("select ZVALUESTRING, sum(ZENDDATE-ZSTARTDATE) s, count(*) n from ZOBJECT "
                    "where ZSTREAMNAME='/app/usage' group by 1 order by s desc limit 15").fetchall()
    for i, (app, s, n) in enumerate(top, 1):
        A("| %d | %s | %.2f | %d | %.0f | %.1f%% |" % (i, app, H(s), n, s / n, 100.0 * s / tot_sec))
    A("")

    # 3. 切换频次
    A("## 3. 切换频次与节奏")
    A("")
    per = c.execute("select date(ZSTARTDATE+978307200,'unixepoch','localtime') d, count(*) n, "
                    "sum(ZENDDATE-ZSTARTDATE) s from ZOBJECT where ZSTREAMNAME='/app/usage' group by d order by d").fetchall()
    act = [(d_, n, s) for d_, n, s in per if n >= 50]
    if act:
        A("| 指标 | 值 |")
        A("|---|---|")
        A("| 活跃日（会话≥50） | %d 天 |" % len(act))
        A("| 日均前台会话数 | %.0f 次 |" % (sum(x[1] for x in act) / len(act)))
        A("| 日均前台时长 | %.2f h |" % (sum(x[2] for x in act) / len(act) / 3600))
        A("| 平均单次前台会话 | %.0f s |" % (sum(x[2] for x in act) / sum(x[1] for x in act)))
        A("| 会话数最多的一天 | %s（%d 次） |" % max(act, key=lambda x: x[1])[:2])
        A("")

    A("## 4. 分时段分布（前台会话起始小时，本地时间）")
    A("")
    hrs = dict(c.execute("select strftime('%H',ZSTARTDATE+978307200,'unixepoch','localtime') h, count(*) "
                         "from ZOBJECT where ZSTREAMNAME='/app/usage' group by h").fetchall())
    mx = max(hrs.values()) if hrs else 1
    for h in range(24):
        n = hrs.get("%02d" % h, 0)
        A("- %02d:00  %4d  %s" % (h, n, "▇" * round(40.0 * n / mx)))
    A("")

    # 5. 屏幕点亮
    A("## 5. 屏幕点亮（/display/isBacklit）")
    A("")
    bl = dict(c.execute("select ZVALUEINTEGER, sum(ZENDDATE-ZSTARTDATE) from ZOBJECT "
                        "where ZSTREAMNAME='/display/isBacklit' group by 1").fetchall())
    on, off = H(bl.get(1, 0)), H(bl.get(0, 0))
    n_on = c.execute("select count(*) from ZOBJECT where ZSTREAMNAME='/display/isBacklit' and ZVALUEINTEGER=1").fetchone()[0]
    A("- 亮屏累计 **%.1f h**（%d 段），熄屏累计 %.1f h ⇒ 亮屏占比 %.1f%%" % (on, n_on, off, 100.0 * on / (on + off)))
    A("- 亮屏日均（按 %d 天连续窗口）≈ **%.2f h/天**" % (len(best), on / len(best)))
    A("")

    # 6. 通知
    A("## 6. 通知（/notification/usage）")
    A("")
    ntot = c.execute("select count(*) from ZOBJECT where ZSTREAMNAME='/notification/usage'").fetchone()[0]
    my = ntot / len(best)
    nrows = c.execute(NOTIF).fetchall()
    named = [r for r in nrows if r[0]]
    A("- 通知总数 **%d** 条，连续窗口内日均 **%.1f 条/天**" % (ntot, my))
    A("- 带 App 归属（structured metadata bundle id）的：**%d / %d = %.1f%%**" % (
        len(named), ntot, 100.0 * len(named) / ntot if ntot else 0))
    if named:
        A("")
        A("| # | 来源 App | 通知数 |")
        A("|---|---|---|")
        for i, (b, n) in enumerate(sorted(((k, sum(1 for x in named if x[0] == k))
                                           for k in {x[0] for x in named}), key=lambda t: -t[1])[:10], 1):
            A("| %d | %s | %d |" % (i, b, n))
    A("")
    A("> 注：`/notification/usage` 的 `ZVALUESTRING` 恒为 'Receive'（无信息量），"
      "App 归属必须 join `ZSTRUCTUREDMETADATA.Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID`。")
    A("")

    # 7. 其它流
    A("## 7. 其它事件流")
    A("")
    A("| 流 | 条数 |")
    A("|---|---|")
    for s, n in c.execute("select ZSTREAMNAME, count(*) from ZOBJECT group by 1 order by 2 desc").fetchall():
        A("| `%s` | %d |" % (s, n))
    A("")
    bt = c.execute("select ZVALUEINTEGER, count(*) from ZOBJECT where ZSTREAMNAME='/bluetooth/isConnected' group by 1").fetchall()
    A("蓝牙连接事件：%s（1=连接 %d 次，0=断开 %d 次）" % (
        bt, dict(bt).get(1, 0), dict(bt).get(0, 0)))
    A("")

    # 8. 局限
    A("## 8. 已知局限 / 未做")
    A("")
    A("- 本报告只覆盖**一份快照**的内容；「4 周滚动窗口」意味着今天看到的 33 天，下个月同样只有 ~30 天（内容不同）。")
    A("- `ZSTARTDATE/ZENDDATE` 是本地时区的 Core Data 秒；跨时区/夏令时未做校正。")
    A("- 前台会话有重叠可能（同一 App 多段），累计时长按段求和，未做去重合并。")
    A("- 未做：与 Timing2（300K 条）交叉验证、App 名称反查（bundle→中文名）、周对比 —— 属 D1「时间去哪了 v2」。")
    A("- **未注册任何定时任务**：本工具只手动跑；每日自动快照属越权改动，单列为待批项。")
    out = args.out or os.path.join(d, "baseline_report.md")
    txt = "\n".join(L) + "\n"
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt)
    con.close()
    print(txt)
    print("[written] %s (%d 字节)" % (out, len(txt.encode("utf-8"))))
    return 0


# ---------------------------------------------------------------- list/verify/prune
def cmd_list(args) -> int:
    if not os.path.isdir(ROOT):
        print("无快照目录")
        return 0
    print("%-12s %-14s %-14s %s" % ("日期", "对象数", "sha256(12)", "bytes"))
    days = sorted(x for x in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, x)))
    for x in days:
        mp = os.path.join(ROOT, x, "manifest.json")
        if not os.path.exists(mp):
            print("%-12s (无 manifest)" % x)
            continue
        m = json.load(open(mp, encoding="utf-8"))
        r = m["revisions"][-1]
        print("%-12s %-14s %-14s %s  (revisions=%d)" % (
            x, r["counts"]["objects"], r["sha256"][:12], r["bytes"], len(m["revisions"])))
    return 0


def cmd_verify(args) -> int:
    d = snap_dir(args.snapshot)
    p = os.path.join(d, SNAPSHOT_NAME)
    m = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    r = m["revisions"][-1]
    h = sha256(p)
    con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    integ = con.execute("PRAGMA integrity_check").fetchone()[0]
    c = counts(con)
    con.close()
    ok = h == r["sha256"] and c == r["counts"] and integ == "ok"
    print("%s %s" % ("[PASS]" if ok else "[FAIL]", d))
    print("  sha 一致=%s integrity=%s 对象数=%s(manifest %s) 侧车残留=%s" % (
        h == r["sha256"], integ, c["objects"], r["counts"]["objects"], assert_sidecars_ok(d) or "无"))
    return 0 if ok else 1


def cmd_prune(args) -> int:
    d0 = dt.date.today() - dt.timedelta(days=args.keep_days)
    days = sorted(x for x in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, x)) and len(x) == 10)
    keep, drop = set(days[:1]), []
    seen_weeks: set[str] = set()
    for x in days:
        dd = dt.date.fromisoformat(x)
        if dd >= d0:
            keep.add(x)
        else:
            wk = "%d-W%02d" % dd.isocalendar()[:2]
            if wk not in seen_weeks:
                seen_weeks.add(wk)
                keep.add(x)
            else:
                drop.append(x)
    print("保留 %d 天 / 待删 %d 天（策略：近 %d 天全量 + 更早每 ISO 周首份 + 首份；%s）" % (
        len(keep), len(drop), args.keep_days, "APPLY" if args.apply else "dry-run"))
    for x in drop:
        if args.apply:
            shutil.rmtree(os.path.join(ROOT, x))
            print("  已删", x)
        else:
            print("  将删", x)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="knowledgeC 幂等快照器 + 数字行为底稿")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--date"); s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_snapshot)
    s = sub.add_parser("report"); s.add_argument("--snapshot", default="latest"); s.add_argument("--out")
    s.set_defaults(func=cmd_report)
    s = sub.add_parser("list"); s.set_defaults(func=cmd_list)
    s = sub.add_parser("verify"); s.add_argument("--snapshot", default="latest"); s.set_defaults(func=cmd_verify)
    s = sub.add_parser("prune"); s.add_argument("--keep-days", type=int, default=KEEP_DAYS_DEFAULT)
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_prune)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
