# R337 附：roi_monitor.py v2 候选补丁快照（183 行 unified diff，未落地）

- 生成: 2026-09-22；生成器 = `temp/roi_v2_build.py`（10 处替换，逐处断言唯一命中）
- 状态: **待用户批准落地**；回滚 = `git checkout -- roi_monitor.py` 或 `patch -R -p0`
- 验收证据见同目录 `R337_roi_monitor产出量告警修复(口径v2方案).md`

```diff
--- /Users/cuip/projects/GenericAgent/roi_monitor.py	2026-08-05 01:36:15
+++ /Users/cuip/projects/GenericAgent/temp/roi_monitor_v2_proposed.py	2026-09-22 03:32:30
@@ -9,9 +9,23 @@
 
 1. 从 sche_tasks/done/ 文件名按自然日聚合任务量(可靠、可复现)。
 2. 计算趋势指标:昨日 / 近7日 / 前7日(环比) / 近30日均值。
-3. 检测业务告警:任务量 <3 个/天、平均产出 <100 行/任务。
-4. 向 roi_history.json 追加当日 task-volume 快照,终止停更。
+3. 检测业务告警(分层口径,见下):总任务量 <3 个/天、业务层产出下滑、
+   业务层任务稀疏。
+4. 向 roi_history.json 追加当日 task-volume 快照(task_volume_v2),终止停更。
 
+口径 v2(2026-09-22 修订,与 v1 的差异必须显式说明):
+ - v1 的问题:①`avg_lines_per_task` 从「自己上次写的快照」里读,而快照 schema
+   `task_volume_v1` 从不写该字段 ⇒ 自 2026-06-26 起恒为 None,<100 行/任务告警
+   永不触发(死代码);②旧 total_lines 是「代码行」口径且采集脚本已丢失,不可复现;
+   ③心跳类任务(folder_watcher/price_alert 等)每天 30+ 次、每次十几行,把整体
+   均值稀释到 ~20 行/任务,任何绝对阈值都会失真。
+ - v2 定义:`report_lines(task)` = done/<date>_<hhmm>_<name>.md 的行数(该次执行的
+   产出报告体量),每日 `lines_per_task` = Σ行数 / 任务数,直接从 done/ 现算,
+   **不依赖历史快照**(快照只作留痕)。
+ - v2 分层:HEARTBEAT = 无事件也照跑的通道类;其余为业务层(biz)。
+   告警只看业务层或用相对基线,避免被心跳稀释。
+ - 与 v1 数值**不可比**,故快照 schema_version 升为 task_volume_v2。
+
 注意:历史快照(2026-05-03)的 total_lines/total_bytes 为代码产出口径,
 原始采集脚本已丢失无法精确复现。本脚本采用 task-volume 口径并显式标注
 schema_version,避免与旧的代码行口径混淆。
@@ -29,14 +43,24 @@
 FNAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+)\.md$")
 
 # 业务阈值
-MIN_TASKS_PER_DAY = 3
-MIN_LINES_PER_TASK = 100
+MIN_TASKS_PER_DAY = 3          # 总任务量(含心跳):低于此视为通道整体停摆
+BIZ_DROP_PCT = 25.0            # 业务层 7日均产出 相对 30日 的下滑告警线(实测历史最大 27.5%)
+BIZ_MIN_PER_DAY = 4.0          # 业务层近7日日均任务数下限(实测历史最低 3.86)
+HB_SHARE_HINT_PCT = 85.0       # 心跳占比仅作提示(非告警):历史上 36% 的日期都超过 85%
 
+# 心跳层:无事件也照跑、产出近乎常量的通道类任务(实测 per_day>=1 且中位行数<=25)
+HEARTBEAT_TASKS = {"folder_watcher", "price_alert", "bridge_autonomous", "sqlite_snapshot"}
 
+
 def aggregate_by_day():
-    """从 done/ 文件名聚合每日任务量与当日任务类型分布。"""
+    """从 done/ 文件名聚合每日任务量、类型分布与产出报告行数(分层)。
+
+    返回 (by_day, by_day_types, by_day_lines)
+      by_day_lines[d] = {"hb": [行数...], "biz": [行数...]}
+    """
     by_day = Counter()
     by_day_types = {}
+    by_day_lines = {}
     for f in os.listdir(DONE_DIR):
         m = FNAME_RE.match(f)
         if not m:
@@ -44,7 +68,14 @@
         d, _hm, name = m.groups()
         by_day[d] += 1
         by_day_types.setdefault(d, Counter())[name] += 1
-    return by_day, by_day_types
+        layer = "hb" if name in HEARTBEAT_TASKS else "biz"
+        try:
+            with open(os.path.join(DONE_DIR, f), encoding="utf-8", errors="replace") as fh:
+                lines = fh.read().count("\n") + 1
+        except Exception:
+            lines = 0
+        by_day_lines.setdefault(d, {"hb": [], "biz": []})[layer].append(lines)
+    return by_day, by_day_types, by_day_lines
 
 
 def drange(end_str, n):
@@ -57,9 +88,17 @@
     return (sum(vals) / len(vals) if vals else 0.0), vals
 
 
+def layer_lines_per_task(by_day_lines, days, layer):
+    """窗口内客观产出口径:Σ报告行数 / Σ任务数;无任务返回 None。"""
+    xs = [l for d in days for l in (by_day_lines.get(d, {}).get(layer) or [])]
+    if not xs:
+        return None, 0
+    return sum(xs) / len(xs), len(xs)
+
+
 def compute(today_str=None):
     today_str = today_str or date.today().isoformat()
-    by_day, by_day_types = aggregate_by_day()
+    by_day, by_day_types, by_day_lines = aggregate_by_day()
 
     # 最近一个完整自然日 = 今天前一天
     last_full = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
@@ -76,19 +115,13 @@
     yest_count = by_day.get(last_full, 0)
     zero_days = [d for d in last30 if by_day.get(d, 0) == 0]
 
-    # roi_history 最新快照(代码产出口径,可能陈旧)
-    avg_lines = None
-    snap_date = None
-    try:
-        with open(HISTORY) as fh:
-            hist = json.load(fh)
-        snaps = hist.get("snapshots", []) if isinstance(hist, dict) else hist
-        if snaps:
-            last_snap = snaps[-1]
-            avg_lines = last_snap.get("metrics", {}).get("avg_lines_per_task")
-            snap_date = last_snap.get("date")
-    except Exception:
-        pass
+    # ── 产出量口径 v2:直接从 done/ 现算(分层),不再读自己写的快照 ──
+    lp_biz7, n_biz7 = layer_lines_per_task(by_day_lines, last7, "biz")
+    lp_biz30, n_biz30 = layer_lines_per_task(by_day_lines, last30, "biz")
+    lp_hb7, n_hb7 = layer_lines_per_task(by_day_lines, last7, "hb")
+    biz_drop = (round((1 - lp_biz7 / lp_biz30) * 100, 1)
+                if (lp_biz7 is not None and lp_biz30) else None)
+    hb_share = round(n_hb7 / (n_hb7 + n_biz7) * 100, 1) if (n_hb7 + n_biz7) else None
 
     return {
         "today": today_str,
@@ -102,8 +135,13 @@
         "wow_pct": round((m7 - mp7) / mp7 * 100, 1) if mp7 else None,
         "vs30_pct": round((m7 - m30) / m30 * 100, 1) if m30 else None,
         "zero_days": zero_days,
-        "avg_lines_per_task": avg_lines,
-        "avg_lines_snapshot_date": snap_date,
+        # 产出量 v2(理由:口径可从 done/ 复现;与旧总行数口径不可比)
+        "lines_per_task_biz_7d": round(lp_biz7, 1) if lp_biz7 is not None else None,
+        "lines_per_task_biz_30d": round(lp_biz30, 1) if lp_biz30 is not None else None,
+        "lines_per_task_hb_7d": round(lp_hb7, 1) if lp_hb7 is not None else None,
+        "biz_drop_vs30_pct": biz_drop,
+        "biz_tasks_per_day_7d": round(n_biz7 / 7, 2),
+        "hb_share_7d_pct": hb_share,
         "today_types": dict(by_day_types.get(today_str, {})),
     }
 
@@ -117,9 +155,16 @@
             f"任务量过低:{metrics['last_full_day']} 仅 {metrics['yesterday_count']} 个 "
             f"(<{MIN_TASKS_PER_DAY}/天阈值)"
         )
-    al = metrics.get("avg_lines_per_task")
-    if al is not None and al < MIN_LINES_PER_TASK:
-        alerts.append(f"平均产出过低:{al} 行/任务 (<{MIN_LINES_PER_TASK} 行阈值)")
+    # 产出量:只看业务层(心跳层每次十几行且每天 30+ 次,会稀释整体均值)
+    drop = metrics.get("biz_drop_vs30_pct")
+    if drop is not None and drop > BIZ_DROP_PCT:
+        alerts.append(
+            f"业务层产出下滑:近7日 {metrics['lines_per_task_biz_7d']} 行/任务 vs 近30日 "
+            f"{metrics['lines_per_task_biz_30d']} 行/任务(降 {drop}% > {BIZ_DROP_PCT}%)"
+        )
+    bpd = metrics.get("biz_tasks_per_day_7d")
+    if bpd is not None and bpd < BIZ_MIN_PER_DAY:
+        alerts.append(f"业务层任务稀疏:近7日仅 {bpd} 个/天 (<{BIZ_MIN_PER_DAY})")
     return alerts
 
 
@@ -137,19 +182,26 @@
     entry = {
         "timestamp": datetime.now().isoformat(),
         "date": metrics["last_full_day"],  # 记最近完整日,口径一致
-        "schema_version": "task_volume_v1",
+        "schema_version": "task_volume_v2",
         "metrics": {
             "tasks_that_day": metrics["yesterday_count"],
             "mean7": metrics["mean7"],
             "mean30": metrics["mean30"],
             "wow_pct": metrics["wow_pct"],
+            # v2 新增:分层产出量(done/ 报告行数口径)
+            "lines_per_task_biz_7d": metrics["lines_per_task_biz_7d"],
+            "lines_per_task_biz_30d": metrics["lines_per_task_biz_30d"],
+            "lines_per_task_hb_7d": metrics["lines_per_task_hb_7d"],
+            "biz_drop_vs30_pct": metrics["biz_drop_vs30_pct"],
+            "biz_tasks_per_day_7d": metrics["biz_tasks_per_day_7d"],
+            "hb_share_7d_pct": metrics["hb_share_7d_pct"],
         },
     }
     # 幂等:若已有同日 task_volume 快照则覆盖
     snaps[:] = [
         s for s in snaps
         if not (s.get("date") == entry["date"]
-                and s.get("schema_version") == "task_volume_v1")
+                and s.get("schema_version") == "task_volume_v2")
     ]
     snaps.append(entry)
     with open(HISTORY, "w") as fh:
```
