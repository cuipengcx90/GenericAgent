#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledgeC 快照守卫（只读判定 + 异常私信；脚手架零副作用）

背景（依据 R371 实测）：源库 `knowledgeC.db` 按流做 **23–28 天滚动删除**（`/app/usage`
≈23 天、其余三流 ≈28.4 天），删除按「now − 窗口」的时间边界批量执行 ⇒ **每过一天就永久
丢掉一天**。快照（`tools/kc_snapshot.py`）是唯一耐久面；只要「距最近一份快照的天数」远
小于 23 天，就不会丢窗口内旧数据。

本守卫只做三件事：**判定 → 告警 → 退出码**。
  - **不采快照**（采集是 `kc_snapshot.py snapshot` 的事）
  - **不注册任何定时任务**（接线/常驻属待批项，见 TODO#1 验收②）
  - **只读**：快照与源库都以 `file:...?mode=ro` 打开；除 `--state` 去重文件外不写任何文件

三级判据
  PASS      最新快照 staleness ≤ `--stale-warn`，且所有快照完整性通过
  WARN      staleness ≥ `--stale-warn`（默认 2 天：今天/昨天都没有新快照 ⇒ 每日节奏断了）
  CRITICAL  staleness ≥ `--stale-crit`（默认 21 天：距源库最短窗 23 天只余 2 天余量）
            或 快照目录为空 / 任一快照 sha256·对象数·quick_check 不符 / 侧车残留

退出码：0=PASS  2=WARN  3=CRITICAL

用法
  python3 tools/kc_snapshot_guard.py                    # 判定；异常时私信（同指纹当天只发一次）
  python3 tools/kc_snapshot_guard.py --no-notify        # 只判定（负控 / CI）
  python3 tools/kc_snapshot_guard.py --notify-dry-run   # 打印将发送的正文，不发送
  python3 tools/kc_snapshot_guard.py --root /tmp/fx --state /tmp/gs.json   # 夹具隔离
  python3 tools/kc_snapshot_guard.py --now 2026-10-20   # 用「那一天」判定（复核用）
  python3 tools/kc_snapshot_guard.py --live             # 附加源库窗口读数（只读）
  python3 tools/kc_snapshot_guard.py --json             # 机器可读输出
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GA = os.path.dirname(HERE)
ROOT_DEFAULT = os.path.join(GA, "temp", "kc_snapshots")
SRC = os.path.expanduser("~/Library/Application Support/Knowledge/knowledgeC.db")
SNAPSHOT_NAME = "knowledgeC.db"
STALE_WARN_DEFAULT = 2      # 今天/昨天必须有快照
STALE_CRIT_DEFAULT = 21     # 源库最短滚动窗 ≈23 天（R371 §4.2）
STATE_DEFAULT = os.path.join(GA, "temp", "kc_guard_state.json")

PASS, WARN, CRIT = "PASS", "WARN", "CRITICAL"
RC = {PASS: 0, WARN: 2, CRIT: 3}
RANK = {PASS: 0, WARN: 1, CRIT: 2}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_dirs(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        # 目录名必须是 YYYY-MM-DD（10 字符且可解析）
        try:
            dt.date.fromisoformat(name)
        except ValueError:
            continue
        if os.path.isdir(p):
            out.append(name)
    return out


def check_one(root: str, name: str) -> list[str]:
    """单份快照的自洽性检查 ⇒ 问题字符串列表（空=通过）"""
    d = os.path.join(root, name)
    issues: list[str] = []
    db = os.path.join(d, SNAPSHOT_NAME)
    man = os.path.join(d, "manifest.json")
    if not os.path.exists(man):
        return ["%s: manifest.json 缺失" % name]
    if not os.path.exists(db):
        return ["%s: %s 缺失" % (name, SNAPSHOT_NAME)]
    leftovers = [f for f in os.listdir(d) if f.startswith(SNAPSHOT_NAME) and f != SNAPSHOT_NAME]
    if leftovers:
        issues.append("%s: 侧车残留 %s（快照应为自包含单文件）" % (name, leftovers))
    try:
        m = json.load(open(man, encoding="utf-8"))
        rev = m["revisions"][-1]
    except Exception as e:                                   # noqa: BLE001
        return issues + ["%s: manifest 不可解析(%s)" % (name, e)]
    h = sha256(db)
    if h != rev["sha256"]:
        issues.append("%s: sha256 不符（盘 %s… / manifest %s…）" % (name, h[:12], rev["sha256"][:12]))
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        integ = con.execute("PRAGMA quick_check").fetchone()[0]
        nobj = con.execute("select count(*) from ZOBJECT").fetchone()[0]
        ncln = con.execute("select count(distinct date(ZSTARTDATE+978307200,'unixepoch','localtime'))"
                           " from ZOBJECT").fetchone()[0]
        con.close()
    except Exception as e:                                   # noqa: BLE001
        return issues + ["%s: 只读打开/查询失败(%s)" % (name, e)]
    if integ != "ok":
        issues.append("%s: quick_check=%s" % (name, integ))
    want = rev.get("counts", {}).get("objects")
    if want is not None and nobj != want:
        issues.append("%s: 对象数 %d ≠ manifest %s" % (name, nobj, want))
    wcln = rev.get("counts", {}).get("distinct_start_days")
    if wcln is not None and ncln != wcln:
        issues.append("%s: 覆盖天数 %d ≠ manifest %s" % (name, ncln, wcln))
    return issues


def source_window() -> dict | None:
    """源库当前窗口（只读；失败返回 None，绝不因此改变判定级别）"""
    if not os.path.exists(SRC):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % SRC, uri=True)
        nobj = con.execute("select count(*) from ZOBJECT").fetchone()[0]
        days = [r[0] for r in con.execute(
            "select date(ZSTARTDATE+978307200,'unixepoch','localtime') d from ZOBJECT"
            " group by d order by d").fetchall()]
        first = [list(r) for r in con.execute(
            "select ZSTREAMNAME, min(date(ZSTARTDATE+978307200,'unixepoch','localtime'))"
            " from ZOBJECT where ZSTREAMNAME in ('/app/usage','/notification/usage',"
            "'/display/isBacklit','/bluetooth/isConnected') group by 1").fetchall()]
        con.close()
    except Exception:                                        # noqa: BLE001
        return None
    return {"objects": nobj, "days": len(days), "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None, "stream_first": first}


def evaluate(root: str, now: dt.date, warn_days: int, crit_days: int, live: bool) -> dict:
    names = snapshot_dirs(root)
    issues: list[str] = []
    broken: list[str] = []
    for n in names:
        bad = check_one(root, n)
        if bad:
            broken.append(n)
            issues.extend(bad)
    if not names:
        latest = None
        stale = None
        issues.append("快照目录为空或不存在：%s" % root)
    else:
        latest = names[-1]
        stale = (now - dt.date.fromisoformat(latest)).days

    level = PASS
    if not names or broken:
        level = CRIT
    if stale is not None:
        if stale >= crit_days:
            level = CRIT
        elif stale >= warn_days and RANK[level] < RANK[WARN]:
            level = WARN
    if stale is not None and stale >= warn_days:
        issues.append("陈旧：最新快照 %s，距今 %d 天（阈值 warn=%d / crit=%d）" % (latest, stale, warn_days, crit_days))
    if stale is not None and stale >= crit_days:
        issues.append("陈旧：距源库最短滚动窗(≈23 天)只剩 %d 天余量 ⇒ 已有不可恢复风险" % (23 - stale))

    res = {"level": level, "root": root, "now": now.isoformat(), "snapshots": names,
           "count": len(names), "latest": latest, "staleness_days": stale,
           "broken": broken, "issues": issues,
           "thresholds": {"warn": warn_days, "crit": crit_days}}
    if live:
        res["source"] = source_window()
    return res


def alert_text(res: dict, tag: str = "") -> str:
    head = "【kc 快照守卫】%s · %s" % (res["level"], res["now"])
    if tag:
        head += " · %s" % tag
    L = [head, ""]
    if res["level"] == PASS:
        L.append("状态：最新快照 %s（%d 天前），%d 份快照完整性全部通过。" % (
            res["latest"], res["staleness_days"], res["count"]))
    else:
        L.append("快照：%d 份（最新 %s，距今 %s 天）" % (
            res["count"], res["latest"], res["staleness_days"]))
        L.append("问题：")
        L += ["- " + x for x in res["issues"]]
        L.append("")
        L.append("建议：python3 tools/kc_snapshot.py snapshot   # 整库拷贝语义：采一份即覆盖整窗")
        if res.get("source"):
            s = res["source"]
            L.append("（源库只读读数：%d 对象 / %d 天 / %s → %s）" % (
                s["objects"], s["days"], s["first_day"], s["last_day"]))
    L.append("")
    L.append("目录：%s" % res["root"])
    L.append("（由 tools/kc_snapshot_guard.py 生成；同一指纹同一天只发一次）")
    return "\n".join(L)


def notify_once(text: str, fingerprint: str, state_path: str, today: str) -> str:
    """按 (指纹, 日期) 去重后发送飞书私信；返回 'sent' / 'dedup' / 'failed:…'"""
    st = {}
    if os.path.exists(state_path):
        try:
            st = json.load(open(state_path, encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            st = {}
    if st.get(fingerprint) == today:
        return "dedup"
    sys.path.insert(0, GA)
    try:
        from reflect_scripts.reflect_common import notify_feishu
        r = notify_feishu(text)
        if not r:
            return "failed:notify_feishu 返回空"
    except Exception as e:                                   # noqa: BLE001
        return "failed:%s" % e
    st[fingerprint] = today
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    return "sent"


def main() -> int:
    ap = argparse.ArgumentParser(description="knowledgeC 快照守卫（只读判定 + 私信告警）")
    ap.add_argument("--root", default=ROOT_DEFAULT)
    ap.add_argument("--now", help="YYYY-MM-DD（默认今天；用于复核/演练）")
    ap.add_argument("--stale-warn", type=int, default=STALE_WARN_DEFAULT)
    ap.add_argument("--stale-crit", type=int, default=STALE_CRIT_DEFAULT)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--notify-dry-run", action="store_true")
    ap.add_argument("--test-tag", default="", help="告警标题追加标记（自检用，如 自检消息）")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = dt.date.fromisoformat(a.now) if a.now else dt.date.today()
    res = evaluate(a.root, now, a.stale_warn, a.stale_crit, a.live)
    text = alert_text(res, a.test_tag)

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("[%s] %s  快照 %d 份 / 最新 %s / staleness=%s 天 / 破损 %s" % (
            res["level"], res["root"], res["count"], res["latest"], res["staleness_days"],
            res["broken"] or "无"))
        for x in res["issues"]:
            print("  - " + x)
        if res.get("source"):
            s = res["source"]
            print("  [源库只读] %d 对象 / %d 天 / %s → %s" % (
                s["objects"], s["days"], s["first_day"], s["last_day"]))
            print("  [源库只读] 逐流最早日 " + " | ".join(
                "%s %s" % (nm, d) for nm, d in s["stream_first"]))

    if res["level"] != PASS and not a.no_notify:
        fp = hashlib.sha1(("\n".join(res["issues"]) + res["root"]).encode()).hexdigest()[:12]
        if a.notify_dry_run:
            print("\n--- notify dry-run（不发送）fingerprint=%s ---" % fp)
            print(text)
        else:
            out = notify_once(text, fp, a.state, now.isoformat())
            print("  [notify] %s（fingerprint=%s）" % (out, fp))
    return RC[res["level"]]


if __name__ == "__main__":
    sys.exit(main())
