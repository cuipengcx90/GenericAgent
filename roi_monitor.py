#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ROI 监控脚本 (roi_monitor_daily 调度任务)

历史上仓库内长期缺失 roi_monitor.py,导致该任务连续多日降级运行
(06-24/25/26 均记 SUCCESS_DEGRADED_NO_SCRIPT),且 roi_history.json
快照自 2026-05-03 停更。本脚本于 2026-06-27 落地,职责:

1. 从 sche_tasks/done/ 文件名按自然日聚合任务量(可靠、可复现)。
2. 计算趋势指标:昨日 / 近7日 / 前7日(环比) / 近30日均值。
3. 检测业务告警:任务量 <3 个/天、平均产出 <100 行/任务。
4. 向 roi_history.json 追加当日 task-volume 快照,终止停更。

注意:历史快照(2026-05-03)的 total_lines/total_bytes 为代码产出口径,
原始采集脚本已丢失无法精确复现。本脚本采用 task-volume 口径并显式标注
schema_version,避免与旧的代码行口径混淆。
"""
import os
import re
import json
from collections import Counter
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DONE_DIR = os.path.join(BASE, "sche_tasks", "done")
HISTORY = os.path.join(BASE, "roi_history.json")

FNAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+)\.md$")

# 业务阈值
MIN_TASKS_PER_DAY = 3
MIN_LINES_PER_TASK = 100


def aggregate_by_day():
    """从 done/ 文件名聚合每日任务量与当日任务类型分布。"""
    by_day = Counter()
    by_day_types = {}
    for f in os.listdir(DONE_DIR):
        m = FNAME_RE.match(f)
        if not m:
            continue
        d, _hm, name = m.groups()
        by_day[d] += 1
        by_day_types.setdefault(d, Counter())[name] += 1
    return by_day, by_day_types


def drange(end_str, n):
    end = date.fromisoformat(end_str)
    return [(end - timedelta(days=i)).isoformat() for i in range(n)]


def mean(by_day, days):
    vals = [by_day.get(d, 0) for d in days]
    return (sum(vals) / len(vals) if vals else 0.0), vals


def compute(today_str=None):
    today_str = today_str or date.today().isoformat()
    by_day, by_day_types = aggregate_by_day()

    # 最近一个完整自然日 = 今天前一天
    last_full = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()

    last7 = drange(last_full, 7)
    prev7 = drange((date.fromisoformat(last_full) - timedelta(days=7)).isoformat(), 7)
    last30 = drange(last_full, 30)

    m7, v7 = mean(by_day, last7)
    mp7, _ = mean(by_day, prev7)
    m30, v30 = mean(by_day, last30)

    today_count = by_day.get(today_str, 0)
    yest_count = by_day.get(last_full, 0)
    zero_days = [d for d in last30 if by_day.get(d, 0) == 0]

    # roi_history 最新快照(代码产出口径,可能陈旧)
    avg_lines = None
    snap_date = None
    try:
        with open(HISTORY) as fh:
            hist = json.load(fh)
        snaps = hist.get("snapshots", []) if isinstance(hist, dict) else hist
        if snaps:
            last_snap = snaps[-1]
            avg_lines = last_snap.get("metrics", {}).get("avg_lines_per_task")
            snap_date = last_snap.get("date")
    except Exception:
        pass

    return {
        "today": today_str,
        "today_count": today_count,
        "last_full_day": last_full,
        "yesterday_count": yest_count,
        "mean7": round(m7, 2),
        "seq7": [by_day.get(d, 0) for d in last7],
        "mean_prev7": round(mp7, 2),
        "mean30": round(m30, 2),
        "wow_pct": round((m7 - mp7) / mp7 * 100, 1) if mp7 else None,
        "vs30_pct": round((m7 - m30) / m30 * 100, 1) if m30 else None,
        "zero_days": zero_days,
        "avg_lines_per_task": avg_lines,
        "avg_lines_snapshot_date": snap_date,
        "today_types": dict(by_day_types.get(today_str, {})),
    }


def detect_alerts(metrics):
    """返回业务告警列表(空=正常)。"""
    alerts = []
    # 任务量:以最近完整日为准(今日为进行中数据,不作硬判定)
    if metrics["yesterday_count"] < MIN_TASKS_PER_DAY:
        alerts.append(
            f"任务量过低:{metrics['last_full_day']} 仅 {metrics['yesterday_count']} 个 "
            f"(<{MIN_TASKS_PER_DAY}/天阈值)"
        )
    al = metrics.get("avg_lines_per_task")
    if al is not None and al < MIN_LINES_PER_TASK:
        alerts.append(f"平均产出过低:{al} 行/任务 (<{MIN_LINES_PER_TASK} 行阈值)")
    return alerts


def append_snapshot(metrics):
    """向 roi_history.json 追加当日 task-volume 快照(幂等:同日覆盖)。"""
    try:
        with open(HISTORY) as fh:
            hist = json.load(fh)
    except Exception:
        hist = {"snapshots": []}
    if not isinstance(hist, dict):
        hist = {"snapshots": hist}
    snaps = hist.setdefault("snapshots", [])

    entry = {
        "timestamp": datetime.now().isoformat(),
        "date": metrics["last_full_day"],  # 记最近完整日,口径一致
        "schema_version": "task_volume_v1",
        "metrics": {
            "tasks_that_day": metrics["yesterday_count"],
            "mean7": metrics["mean7"],
            "mean30": metrics["mean30"],
            "wow_pct": metrics["wow_pct"],
        },
    }
    # 幂等:若已有同日 task_volume 快照则覆盖
    snaps[:] = [
        s for s in snaps
        if not (s.get("date") == entry["date"]
                and s.get("schema_version") == "task_volume_v1")
    ]
    snaps.append(entry)
    with open(HISTORY, "w") as fh:
        json.dump(hist, fh, ensure_ascii=False, indent=2)
    return entry


def run(today_str=None, write_history=True):
    metrics = compute(today_str)
    alerts = detect_alerts(metrics)
    snap = append_snapshot(metrics) if write_history else None
    return {"metrics": metrics, "alerts": alerts, "snapshot": snap}


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
