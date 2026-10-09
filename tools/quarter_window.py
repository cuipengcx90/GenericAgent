#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""季度时间窗口计算：绩效考评三源取数（git / 禅道 / 飞书）共用的单一源。

口径（半开区间；相邻季度零重叠、零遗漏）：
    Q1 = 1–3 月   Q2 = 4–6 月   Q3 = 7–9 月   Q4 = 10–12 月
    since = <本季首日>T00:00:00+08:00
    until = <下季首日>T00:00:00+08:00   （until 本身不计入）
禁止在 SOP / prompt 里写死某一年某季度；每季开跑时现算。

CLI:
    python3 tools/quarter_window.py                    # 当前季度（北京时间）
    python3 tools/quarter_window.py --quarter Q3-2026  # 指定季度
    python3 tools/quarter_window.py --offset -1        # 相对当前季度偏移
    python3 tools/quarter_window.py --since 2026-07-01 --until 2026-10-01
    python3 tools/quarter_window.py --json
    python3 tools/quarter_window.py --selftest

只读、无副作用；纯标准库。
"""
import argparse
import datetime
import json
import sys

TZ = datetime.timezone(datetime.timedelta(hours=8))
FMT = "%Y-%m-%d %H:%M:%S"


def parse_quarter(label):
    """'Q3-2026' / '2026Q3' / '2026-Q3' / 'q3 2026' -> (2026, 3)"""
    s = str(label).strip().upper().replace(" ", "").replace("_", "-")
    year = quarter = None
    if s.startswith("Q"):
        parts = s[1:].split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            quarter, year = int(parts[0]), int(parts[1])
    elif "Q" in s:
        a, b = s.split("Q", 1)
        b = b.strip("-")
        if a.strip("-").isdigit() and b.isdigit():
            year, quarter = int(a.strip("-")), int(b)
    else:
        parts = s.split("-")
        if len(parts) == 3 and parts[1] == "" and parts[0].isdigit() and parts[2].isdigit():
            year, quarter = int(parts[0]), int(parts[2])
    if year is None or quarter is None or not (1 <= quarter <= 4):
        raise ValueError("无法解析季度标签: %r（示例 Q3-2026 / 2026Q3）" % (label,))
    return year, quarter


def quarter_label(year, quarter):
    return "Q%d-%d" % (quarter, year)


def shift_quarter(year, quarter, offset):
    idx = (year * 4 + (quarter - 1)) + int(offset)
    return idx // 4, idx % 4 + 1


def quarter_of(dt):
    """datetime（应带 +08:00）-> (year, quarter)"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.year, (dt.month - 1) // 3 + 1


def bounds(year, quarter):
    """返回 dict(quarter, since, until, since_ts, until_ts, days, label)"""
    since = datetime.datetime(year, 3 * (quarter - 1) + 1, 1, 0, 0, 0, tzinfo=TZ)
    ny, nq = shift_quarter(year, quarter, 1)
    until = datetime.datetime(ny, 3 * (nq - 1) + 1, 1, 0, 0, 0, tzinfo=TZ)
    return {
        "label": quarter_label(year, quarter),
        "quarter": quarter,
        "year": year,
        "since": since.strftime(FMT),
        "until": until.strftime(FMT),
        "since_ts": since.timestamp(),
        "until_ts": until.timestamp(),
        "days": int((until - since).total_seconds() // 86400),
    }


def parse_day(s):
    """'2026-07-01' 或 '2026-07-01 00:00:00' -> datetime(+08:00)"""
    s = str(s).strip()
    for fmt in (FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError("无法解析日期: %r" % (s,))


def resolve(since=None, until=None, quarter=None, offset=0, now=None):
    """SOP 统一入口：返回窗口 dict。quarter/since+until 至少给一组。"""
    if since or until:
        if not (since and until):
            raise ValueError("--since 与 --until 必须成对给出")
        sd, ud = parse_day(since), parse_day(until)
        if not sd < ud:
            raise ValueError("since 必须早于 until")
        y, q = quarter_of(sd)
        return {
            "label": quarter_label(y, q) + "+custom",
            "quarter": q,
            "year": y,
            "since": sd.strftime(FMT),
            "until": ud.strftime(FMT),
            "since_ts": sd.timestamp(),
            "until_ts": ud.timestamp(),
            "days": int((ud - sd).total_seconds() // 86400),
        }
    if quarter:
        y, q = parse_quarter(quarter)
    else:
        y, q = quarter_of(now or datetime.datetime.now(TZ))
    if offset:
        y, q = shift_quarter(y, q, offset)
    return bounds(y, q)


def main(argv=None):
    ap = argparse.ArgumentParser(description="季度窗口计算（只读）")
    ap.add_argument("--quarter", help="如 Q3-2026 / 2026Q3")
    ap.add_argument("--offset", type=int, default=0, help="相对当前季度偏移，-1=上季度")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON，保留兼容）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        w = resolve(args.since, args.until, args.quarter, args.offset)
    except ValueError as e:
        print("ERROR %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(w, ensure_ascii=False, indent=1))
    return 0


def selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    w1 = bounds(2026, 1)
    ck("Q1 since", w1["since"] == "2026-01-01 00:00:00")
    ck("Q1 until", w1["until"] == "2026-04-01 00:00:00")
    ck("Q1 days=90", w1["days"] == 90)
    w4 = bounds(2026, 4)
    ck("Q4 until 跨年", w4["until"] == "2027-01-01 00:00:00")
    ck("Q4 since", w4["since"] == "2026-10-01 00:00:00")
    # 连续性：相邻季度首尾相接，零重叠零遗漏
    for y, q in [(2026, 1), (2026, 2), (2026, 3), (2026, 4), (2025, 4)]:
        nxt = bounds(*shift_quarter(y, q, 1))
        ck("连续% s" % quarter_label(y, q), bounds(y, q)["until"] == nxt["since"])
    # 偏移
    ck("offset -1", shift_quarter(2026, 1, -1) == (2025, 4))
    ck("offset +1", shift_quarter(2026, 4, 1) == (2027, 1))
    # 解析形态
    ck("label Q3-2026", parse_quarter("Q3-2026") == (2026, 3))
    ck("label 2026Q3", parse_quarter("2026Q3") == (2026, 3))
    ck("bad label -> raise", _raises(parse_quarter, "Q9-2026"))
    # 半开区间边界（负控：跨季时刻必须落对季）
    inq3 = datetime.datetime(2026, 9, 30, 23, 59, 59, tzinfo=TZ)
    inq4 = datetime.datetime(2026, 10, 1, 0, 0, 0, tzinfo=TZ)
    ck("9/30 属 Q3", quarter_of(inq3) == (2026, 3))
    ck("10/1 属 Q4", quarter_of(inq4) == (2026, 4))
    ck("9/30 在 Q3 窗口内", bounds(2026, 3)["since_ts"] <= inq3.timestamp() < bounds(2026, 3)["until_ts"])
    ck("10/1 不在 Q3 窗口内(负控)", not (bounds(2026, 3)["since_ts"] <= inq4.timestamp() < bounds(2026, 3)["until_ts"]))
    # 自定义窗口与成对校验
    cw = resolve("2026-07-01", "2026-10-01")
    ck("custom since", cw["since"] == "2026-07-01 00:00:00")
    ck("custom until", cw["until"] == "2026-10-01 00:00:00")
    ck("缺 until -> raise(负控)", _raises(resolve, "2026-07-01", None))
    ck("since>=until -> raise(负控)", _raises(resolve, "2026-10-01", "2026-07-01"))
    # 动态性：同一代码在不同“今天”给出不同季度（禁写死）
    a = resolve(now=datetime.datetime(2026, 8, 15, tzinfo=TZ))["label"]
    b = resolve(now=datetime.datetime(2026, 11, 15, tzinfo=TZ))["label"]
    ck("动态取季", a == "Q3-2026" and b == "Q4-2026")
    ck("offset 相对动态", resolve(offset=-1, now=datetime.datetime(2026, 1, 5, tzinfo=TZ))["label"] == "Q4-2025")

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("%-28s %s" % (n, "PASS" if ok else "FAIL"))
    print("selftest: %d checks, %d failed" % (len(checks), len(bad)))
    return 0 if not bad else 1


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
