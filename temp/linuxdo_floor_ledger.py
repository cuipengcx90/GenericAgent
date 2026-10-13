#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""linuxdo_floor_ledger —— L 站「每日浏览增量」小账（只读汇聚）

用途：一句话回答「今天 L 站涨了多少楼层 / 话题」。
数据源（全部只读，零站点访问、零副作用）：
  1) temp/linuxdo_browse_daemon/level_log.jsonl —— daemon 自己的 connect 页读数（12 项，含 浏览话题/浏览帖子）
  2) temp/linuxdo_floor_ledger.jsonl          —— 本工具的手工/补录读数

用法：
  python3 temp/linuxdo_floor_ledger.py show              # 打印每日表（默认）
  python3 temp/linuxdo_floor_ledger.py status            # 一句话进度（楼层缺口 + 今日增量 + ETA）
  python3 temp/linuxdo_floor_ledger.py add <话题> <帖子> [note]   # 手工补一条读数
  python3 temp/linuxdo_floor_ledger.py today             # 只看今天
  python3 temp/linuxdo_floor_ledger.py credits [天数]     # ★R583 楼层记账口径日汇总(browse_log 的 fc 拆 实测/估算/旧行)
  python3 temp/linuxdo_floor_ledger.py board [天数]       # 生成「楼层进度看板」markdown（写 temp/linuxdo_floor_board.md 并打印）

口径：浏览话题(topic_count)=主帖数（进入过的话题数）；浏览帖子(posts_read)=楼层数（connect 页「浏览帖子 / 20000」；Discourse 里一个话题内每条回复各算 1 楼，故同日 Δ帖子 常是 Δ话题 的 5–10 倍；页面用词「帖子」易误读成帖数）。
      「日增量」= 当日最后一条读数 − 上一条可用读数（同日多条则取当日首条为基线）。
"""
import json
import os
import re
import sys
from datetime import datetime

GA = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DAEMON_DIR = os.path.join(GA, "temp", "linuxdo_browse_daemon")
LEVEL_LOG = os.path.join(DAEMON_DIR, "level_log.jsonl")
LEDGER = os.path.join(GA, "temp", "linuxdo_floor_ledger.jsonl")


def _num(s):
    m = re.search(r"([\d,]+)\s*/\s*([\d,]+)", s or "")
    if not m:
        return None, None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def read_level_log():
    """从 daemon 的 level_log 里抽 connect 读数。"""
    out = []
    if not os.path.exists(LEVEL_LOG):
        return out
    for line in open(LEVEL_LOG, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("source") != "connect" or not r.get("items"):
            continue
        # 12 项顺序固定（daemon 注释：connect 实测固定 12 项顺序）
        #   [0]访问天数/50 [1]浏览话题/500 [2]浏览帖子/20000 [3]回复话题 ...
        # 用**门槛值**识别而非位置（门槛 500→话题，20000→帖子），双保险。
        topics = posts = None
        for idx, it in enumerate(r["items"]):
            nm = (it.get("name") or "").replace("\n", " ").strip()
            cur, req = _num(nm)
            if cur is None:
                continue
            if req == 20000:
                posts = cur
            elif req == 500:
                topics = cur
        if topics is None and len(r["items"]) > 1:
            topics, _ = _num(r["items"][1].get("name") or "")
        if posts is None and len(r["items"]) > 2:
            posts, _ = _num(r["items"][2].get("name") or "")
        if topics is None and posts is None:
            continue
        out.append({"ts": r.get("ts"), "date": (r.get("ts") or "")[:10],
                    "topics": topics, "posts": posts, "src": "level_log"})
    return out


def read_manual():
    out = []
    if not os.path.exists(LEDGER):
        return out
    for line in open(LEDGER, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        out.append({"ts": r.get("ts"), "date": (r.get("ts") or "")[:10],
                    "topics": r.get("topics"), "posts": r.get("posts"),
                    "src": r.get("src") or "manual", "note": r.get("note")})
    return out


def all_rows():
    rows = read_level_log() + read_manual()
    rows = [r for r in rows if r.get("ts")]
    rows.sort(key=lambda r: (r["ts"], str(r.get("src"))))
    return rows


def daily_table(rows):
    """→ [{date, first_ts, last_ts, topics, posts, d_topics, d_posts, n}]"""
    byday = {}
    for r in rows:
        byday.setdefault(r["date"], []).append(r)
    days = sorted(byday)
    out = []
    prev_last = None
    for d in days:
        rs = byday[d]
        first, last = rs[0], rs[-1]
        base = first if len(rs) > 1 else (prev_last if prev_last else None)
        d_t = d_p = None
        if base and last.get("posts") is not None and base.get("posts") is not None:
            d_t = last["topics"] - base["topics"] if (last.get("topics") is not None and base.get("topics") is not None) else None
            d_p = last["posts"] - base["posts"]
        win_h = None
        if base:
            try:
                a = datetime.strptime(base["ts"][:19], "%Y-%m-%d %H:%M:%S")
                b = datetime.strptime(last["ts"][:19], "%Y-%m-%d %H:%M:%S")
                win_h = round((b - a).total_seconds() / 3600.0, 1)
            except Exception:
                win_h = None
        out.append({"date": d, "n": len(rs), "first_ts": first["ts"], "last_ts": last["ts"],
                    "topics": last.get("topics"), "posts": last.get("posts"),
                    "d_topics": d_t, "d_posts": d_p, "win_h": win_h,
                    "base": (base or {}).get("ts")})
        prev_last = last
    return out


def fmt(v):
    return "-" if v is None else ("%+d" % v if isinstance(v, int) and False else str(v))


def show(only_today=False):
    last = None
    if os.path.exists(LEDGER):
        for line in open(LEDGER, encoding="utf-8", errors="ignore"):
            if line.strip():
                try:
                    last = json.loads(line)
                except Exception:
                    pass
    _ = last
    rows = all_rows()
    tab = daily_table(rows)
    if only_today:
        tab = tab[-1:] if tab else []
    print("日期        读数  浏览话题      浏览帖子(楼层)  Δ话题   Δ帖子(楼层)  窗口h  基线时刻")
    for t in tab:
        print("%-10s %3d  %-8s   %-8s  %-6s %-6s %-6s %s" % (
            t["date"], t["n"],
            "-" if t["topics"] is None else t["topics"],
            "-" if t["posts"] is None else t["posts"],
            "-" if t["d_topics"] is None else ("+%d" % t["d_topics"]),
            "-" if t["d_posts"] is None else ("+%d" % t["d_posts"]),
            "-" if t.get("win_h") is None else ("%.1f" % t["win_h"]),
            (t["base"] or "")[5:] or "-"))
    if tab:
        t = tab[-1]
        print("\n最近一条：%s  浏览话题 %s / 浏览帖子 %s" % (t["last_ts"], t["topics"], t["posts"]))


def add(topics, posts, note=""):
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "topics": topics, "posts": posts,
           "src": "manual", "note": note}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("已追加:", json.dumps(rec, ensure_ascii=False))


GOAL_POSTS = 20000          # connect 页「浏览帖子」门槛（=**楼层数**，100 天滚动窗）
GOAL_TOPICS = 500           # connect 页「浏览话题」门槛（=主帖数）
STEADY_FLOOR_DAILY = GOAL_POSTS // 100      # 稳态所需楼层/日 = 20000/100 天


def status():
    """一句话进度（只读）：楼层缺口 + 今日增量 + ETA。供人工/日报调用。"""
    rows = all_rows()
    if not rows:
        print("楼层：暂无读数")
        return 1
    last = rows[-1]
    posts, topics = last.get("posts"), last.get("topics")
    if posts is None:
        print("楼层：最近读数缺「浏览帖子」项（ts=%s）" % last.get("ts"))
        return 1
    gap = max(0, GOAL_POSTS - posts)
    tab = daily_table(rows)
    t = tab[-1] if tab else {}
    d_p, d_t = t.get("d_posts"), t.get("d_topics")
    _ps = "-" if topics is None else str(topics)
    print("楼层 %d/%d (%.1f%%)  缺口 %d  |  话题 %s/%d %s" % (
        posts, GOAL_POSTS, 100.0 * posts / GOAL_POSTS, gap, _ps, GOAL_TOPICS,
        "✓" if (topics or 0) >= GOAL_TOPICS else "✗"))
    print("今日 +%s 楼层 / +%s 话题（最近读数 %s）" % (
        "-" if d_p is None else d_p, "-" if d_t is None else d_t, last.get("ts")))
    if gap == 0:
        print("已达门槛（100 天滚动窗，旧读数会到期滑出）")
    else:
        _pace = ("约 %.1f 天" % (gap / float(d_p))) if d_p else "无法估算（今日无增量读数）"
        print("稳态需 %d 楼层/日 → 约 %.0f 天补平；按今日节奏 → %s" % (
            STEADY_FLOOR_DAILY, gap / float(STEADY_FLOOR_DAILY), _pace))
    return 0


BOARD = os.path.join(GA, "temp", "linuxdo_floor_board.md")


def board(days=14):
    """生成「楼层进度看板」（markdown，逐日表）；写文件并打印。只读、零站点访问。

    数据源同 all_rows()（daemon 的 level_log 自动读数 + 手工补录），故**日更自动**：
    daemon 每次读 connect 页即汇入一行，无需人工落账。
    """
    rows = all_rows()
    tab = daily_table(rows)
    if days > 0:
        tab = tab[-days:]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = ["# L 站楼层进度看板", "",
         "_生成 %s（只读；源=daemon level_log + 手工补录）_" % now, ""]
    if not rows:
        L.append("暂无读数。")
    else:
        last = rows[-1]
        posts, topics = last.get("posts"), last.get("topics")
        gap = max(0, GOAL_POSTS - (posts or 0))
        L.append("**当前**：浏览帖子(楼层) %s / %d（缺口 %d） | 浏览话题(主帖) %s / %d %s"
                 % (posts, GOAL_POSTS, gap, topics, GOAL_TOPICS,
                    "✓" if (topics or 0) >= GOAL_TOPICS else "✗"))
        t = tab[-1] if tab else {}
        L.append("**今日**：+%s 楼层 / +%s 话题（最近读数 %s）"
                 % ("-" if t.get("d_posts") is None else t["d_posts"],
                    "-" if t.get("d_topics") is None else t["d_topics"], last.get("ts")))
        # ★R583:楼层记账口径一行(实测/估算/旧行)——账面楼层里多少是真读到的
        _cd = _credit_days().get((last.get("ts") or "")[:10], {})
        if _cd:
            _n = sum(v["n"] for v in _cd.values())
            _m = _cd.get("measured", {"n": 0, "fc": 0})
            _e = _cd.get("estimate", {"n": 0, "fc": 0})
            _g = _cd.get("legacy", {"n": 0, "fc": 0})
            L.append("**记账口径(今日)**:实测 %d 帖/%d 楼 · 估算 %d 帖/%d 楼 · 旧行 %d 帖/%d 楼(实测占比 %s)"
                     % (_m["n"], _m["fc"], _e["n"], _e["fc"], _g["n"], _g["fc"],
                        "-" if not _n else ("%.0f%%" % (100.0 * _m["n"] / _n))))
        L.append("")
        L.append("| 日期 | 读数 | 浏览话题 | 楼层 | Δ话题 | Δ楼层 | 窗口h |")
        L.append("|---|---|---|---|---|---|---|")
        for t in tab:
            L.append("| %s | %d | %s | %s | %s | %s | %s |" % (
                t["date"], t["n"],
                "-" if t["topics"] is None else t["topics"],
                "-" if t["posts"] is None else t["posts"],
                "-" if t["d_topics"] is None else ("+%d" % t["d_topics"]),
                "-" if t["d_posts"] is None else ("+%d" % t["d_posts"]),
                "-" if t.get("win_h") is None else ("%.1f" % t["win_h"])))
    md = "\n".join(L) + "\n"
    with open(BOARD, "w", encoding="utf-8") as f:
        f.write(md)
    print(md, end="")
    print("[已写入] %s" % BOARD)
    return 0


BROWSE_LOG = os.path.join(DAEMON_DIR, "browse_log.jsonl")


def _credit_days():
    """→ {date: {kind: {"n":帖数, "fc":楼层和}}};kind ∈ measured/estimate/legacy(R583 口径)。"""
    byday = {}
    if not os.path.exists(BROWSE_LOG):
        return byday
    for line in open(BROWSE_LOG, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("st") != "DONE":
            continue
        fc = r.get("fc")
        if not isinstance(fc, (int, float)):
            continue
        d = (r.get("ts") or "")[:10]
        k = r.get("fc_src") or "legacy"
        slot = byday.setdefault(d, {})
        e = slot.setdefault(k, {"n": 0, "fc": 0})
        e["n"] += 1
        e["fc"] += int(fc)
    return byday


def credits(days=7):
    """★R583:「楼层记账口径」日汇总(只读)——把 daemon browse_log 的每帖 fc 按日拆
    「实测(measured)/估算(estimate)/旧行(无 fc_src)」三档,回答「账面楼层里有多少是
    真读到、多少是回退估的」。用于验证 R583 探针语法修复 + 记账口径收紧后的效果。

    口径:仅统计 st=="DONE" 且有 fc 的行;fc_src 缺失=旧行(R583 之前,口径不可分辨)。
    """
    byday = _credit_days()
    if not byday:
        print("楼层记账:暂无 browse_log DONE 行")
        return 1
    days_sorted = sorted(byday)[-days:]
    print("日期        帖子  实测(measured)        估算(estimate)        旧行(legacy)         实测占比")
    for d in days_sorted:
        s = byday[d]
        n = sum(v["n"] for v in s.values())
        fct = sum(v["fc"] for v in s.values())
        m = s.get("measured", {"n": 0, "fc": 0})
        e = s.get("estimate", {"n": 0, "fc": 0})
        g = s.get("legacy", {"n": 0, "fc": 0})
        share = "-" if not n else ("%.0f%%" % (100.0 * m["n"] / n))
        print("%-10s %4d  %3d 帖 %6d 楼  %3d 帖 %6d 楼  %3d 帖 %6d 楼  %5s" % (
            d, n, m["n"], m["fc"], e["n"], e["fc"], g["n"], g["fc"], share))
    print("(实测占比=fc_src=measured 的帖数 / 当日有 fc 的 DONE 帖数;旧行=本批之前口径不可分辨)")
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "show"
    if cmd == "show":
        show()
    elif cmd == "status":
        return status()
    elif cmd == "today":
        show(only_today=True)
    elif cmd == "add":
        add(int(args[1]), int(args[2]), args[3] if len(args) > 3 else "")
    elif cmd == "board":
        return board(int(args[1]) if len(args) > 1 and args[1].lstrip("-").isdigit() else 14)
    elif cmd == "credits":
        return credits(int(args[1]) if len(args) > 1 and args[1].lstrip("-").isdigit() else 7)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
