# R337 roi_monitor 产出量告警修复（口径 v2 方案，待批准落地）

- 日期：2026-09-22
- 类型：工程 / 缺陷修复（分析 + 候选补丁，**未改仓库代码**）
- 状态：分析闭环、补丁可执行、真实数据验证通过；**落地需用户批准**（改的是 GA 自身源码）

## 1. 缺陷确证（实测，非推断）

### 1.1 告警死了 3 个月（死代码）
`roi_monitor.py` 自 2026-06-27 落地，其「平均产出 <100 行/任务」告警的输入取自
**自己上次写的快照**：

```python
last_snap = snaps[-1]
avg_lines = last_snap.get("metrics", {}).get("avg_lines_per_task")   # 第 88 行
```

而 `append_snapshot()` 写快照时 `schema_version="task_volume_v1"`，metrics 只有
`tasks_that_day / mean7 / mean30 / wow_pct` —— **从不写 `avg_lines_per_task`**。
实测 `roi_history.json`：

```
快照 86 个；含 avg_lines_per_task 字段 1 个（非 null 1 个，即 2026-05-03 的旧口径快照）
task_volume_v1 85 个（2026-06-26 → 2026-09-20）
末快照 metrics={'tasks_that_day': 35, 'mean7': 48.71, 'mean30': 41.23, 'wow_pct': 10.4}
        ⇒ metrics.get('avg_lines_per_task') = None
```
⇒ 自 2026-06-26 起 `avg_lines` 恒为 `None`，`detect_alerts()` 里
`if al is not None and al < 100` 永远短路。**该告警 3 个月零触发，且永远不可能触发。**

### 1.2 就算修好取值，阈值也已经失真（真正的坑）
`done/` 里 5962 份执行报告实测分层体量：

| 层 | 任务 | 次数 | 中位行数 | 日均次数 |
|---|---|---|---|---|
| 心跳 | folder_watcher | 2303 | 13 | 17.6 |
| 心跳 | price_alert | 2187 | 16 | 16.4 |
| 心跳 | bridge_autonomous | 213 | 18 | 1.6 |
| 心跳 | sqlite_snapshot | 149 | 22 | 1.1 |
| 业务 | github_trending | 156 | 26 | 1.1 |
| 业务 | trendradar_morning/eve | 145/142 | 30/29 | 1.0 |
| 业务 | roi_monitor_daily | 138 | 54 | 1.0 |

近 7 日：心跳层 267 次 × 22.5 行/任务、业务层 77 次 × 41.3 行/任务，**心跳占比 77.6%**。
⇒ 若按整体均值算，全天 ≈ (267×22.5+77×41.3)/344 ≈ 20.3 行/任务 —— 与 100 行阈值差 5 倍，
**修好取值反而会天天误报**。故必须同时改口径 + 分层 + 重定阈值，三者缺一不可。

## 2. 口径 v2 定义（显式声明，与 v1 不可比）

- `report_lines(task)` = `done/<date>_<hhmm>_<name>.md` 的行数（该次执行的产出报告体量）
- `lines_per_task(day, layer)` = Σ行数 / 任务数，**每次运行从 done/ 现算**
  ⇒ 修掉「读自己快照」的结构性错误（告警输入必须来自原始可复现数据，不能来自自己写的缓存）
- 分层 `HEARTBEAT_TASKS = {folder_watcher, price_alert, bridge_autonomous, sqlite_snapshot}`
  （实测依据：per_day ≥ 1 且中位行数 ≤ 25 的通道类）
- 快照 `schema_version` 升为 **task_volume_v2**，附注说明与 v1 不可比（旧 total_lines 是代码行口径、
  采集脚本已丢失，不可复现）

## 3. 阈值校准（用全部历史回溯，非拍脑袋）

回溯 2026-04-23 → 2026-09-22 共 121 个可评估视点：

- 业务层 `drop_vs30`（7日 vs 30日产出下滑）：min -53 / p50 6.0 / p90 19.4 / p95 22.5 / **max 27.5**
  ⇒ 取 **25%**：命中 1 天（2026-07-10，真实最大下滑），不误报
- 业务层 `biz_tasks_per_day_7d`：min 3.86 / p5 5.43 / p50 7.14
  ⇒ 取 **4.0**：命中 2 天（2026-06-04/05，正是 05-31 停摆后的恢复窗口）
- 「心跳占比」若作告警：121 天里 44 天触发（36%）⇒ **降级为提示(context)，不作告警**（防告警疲劳）

## 4. 真实数据验证（非构造桩）

候选版 `temp/roi_monitor_v2_proposed.py`（=原文件套 10 处最小替换）注入真实路径后 dry-run：

```
today=2026-09-22 完整日=2026-09-21 总任务=63
   biz行/任务: 7d=41.3 30d=40.9 下滑=-1.1% | biz个/天7d=11.0 | hb占比=77.6%   alerts: 无
today=2026-06-02 完整日=2026-06-01 总任务=0
   alerts: ['任务量过低:2026-06-01 仅 0 个 (<3/天阈值)']            ← 真实全通道停摆日
today=2026-07-11 完整日=2026-07-10 总任务=57
   biz行/任务: 7d=25.6 30d=35.2 下滑=27.5%
   alerts: ['业务层产出下滑:近7日 25.6 行/任务 vs 近30日 35.2 行/任务(降 27.5% > 25.0%)']
today=2026-06-05 完整日=2026-06-04
   biz个/天7d=3.86
   alerts: ['业务层任务稀疏:近7日仅 3.86 个/天 (<4.0)']             ← 停摆后恢复期
```
- 三类告警**各自被真实历史数据触发**（05-31/06-01 确实 0 任务；07-10 确实为 121 天最大下滑）
- 今日视点为「无告警」⇒ 误报率检查通过
- 运行方式：`v2.run(today_str=..., write_history=False)`，**未写 roi_history.json**；`git status` 对
  `roi_monitor.py`/`roi_history.json` 均为空（仓库状态零改动）

## 5. 候选补丁（10 处最小替换，183 行 unified diff）

生成方式可复现：`python3 temp/roi_v2_build.py`（逐一断言每处替换**恰好命中 1 次**，否则直接报错退出）。

落地：`cd <repo> && patch -p0 --dry-run < temp/roi_monitor_v2.patch` → 确认后 `patch -p0 < ...`；
或直接取用 `temp/roi_monitor_v2_proposed.py` 覆盖（仅 221 行）。
**回滚**：`git checkout -- roi_monitor.py`（若已提交：`git revert <sha>`）；补丁为纯文本可 `patch -R -p0` 反打。

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

## 6. 风险与边界

- 口径变更 ⇒ **v1 快照与 v2 数值不可比**，`roi_history.json` 会同时留存两代（v1 85 条 + v2 后续），
  图表/下游若按 `schema_version` 过滤需同步（当前无下游消费方，实测仅本脚本读写）。
- 新增一次全量 `done/` 读取（5962 个文件、4.13MB）：实测脚本整体耗时仍在秒级，无性能风险。
- 心跳集合是**人工判定**（依据 per_day≥1 且中位≤25 行）；新增任务若属通道类需手动加入集合。
  替代方案（未采用）：按"名字含 monitor/alert/watcher 关键词"自动判定——误判风险更高。
- 阈值取自 121 天历史（恰逢 04-06 系统上线期），样本偏短；建议 3 个月后按 P95 复校一次。
- 「昨日任务数 <3」沿用原阈值与原语义（=通道整体停摆），未改；它依赖 done/ 现算，天然可复现。

## 7. 记忆更新建议（待审）

1. **L1/L2 修订**：现 L1 写「ROI日报 roi_monitor.py(任务量口径task_volume_v1；avg_lines_per_task 自
   09-19 快照起为 null 致 <100 行告警失效)」—— 实测**最早 empty 是 2026-06-26**（v1 首快照起就没有该字段），
   不是 09-19；应改为「v1 自 06-26 起无产出量字段 ⇒ 该告警为死代码；v2 方案见 R337」。
2. **新增 L3 SOP**（拟名 `roi_monitor_sop`）：告警输入必须来自可复现原始数据（done/ 文件），
   禁止从自己写的快照回读——本缺陷是「缓存自噬」的教科书案例；指标分层(心跳/业务)通用做法。
3. **通用教训**：`schema_version` 升级后必须同步检查**读方**（本次读方仍按旧字段名取值，
   因默认 None 而静默失效——静默失败比报错更危险，检测手段=对每个告警加「最近一次触发时间」探针）。

## 8. 待批准动作清单

| # | 动作 | 需批准原因 |
|---|---|---|
| 1 | 把 `temp/roi_monitor_v2.patch` 应用到 `roi_monitor.py` | 修改 GA 自身源码 |
| 2 | 应用后 `git commit`（回滚点） | 同上 |
| 3 | 3 个月后按 P95 复校阈值 | 需长期观察数据 |
