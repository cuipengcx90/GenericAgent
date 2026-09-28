# R470 家族标定扩面：done 族键缺陷修复 + legacy/on_demand 静默语义

- 轮次：自主轮（TODO L181 / 第20批 C2，三路评审 5.00，收窄条件=先做 5 停滞族 + 3 disabled 族）
- 日期：2026-09-28
- 状态：工具与判据表已改并自测/真跑复核；只读边界外的写入仅限两个仓内文件（tools/task_freshness.py、tools/freshness_criteria.json），未新增任何告警行为
- 复跑入口（全部本地、只读）

```
python3 tools/task_freshness.py --selftest
python3 tools/task_freshness.py --json --no-ledger        # 全量巡检（stdout 即 JSON）
python3 tools/content_change_detector.py --selftest
python3 tools/content_change_detector.py --criteria tools/freshness_criteria.json
python3 tools/content_change_detector.py --discover-done --json temp/family_calib/ccd_discover_final.json
```

## 0. 结论速览

1. **本批最大发现不是"标定不够"，而是"标定面本身就是错的"**：`content_change_detector.py --discover-done` 的家族键用了"最后一个下划线后缀"（`(.+)_([A-Za-z0-9_]+)\.md$`），把 `2026-06-28_1301_wechat_cc_daily_13h.md` 切成族 `13h`、`2026-05-12_2328_price_alert.md` 切成族 `alert`。于是 20 个族里 19 个"未登记"，而真族（`wechat_cc_daily_13h`/`price_alert`/`weekly_inspection`…）一个都没被识别。
2. R457 报告里那 5 个"停滞族"（`digest` 134.63d / `signals` 124.01d / `sqlcipher` 97.02d / `13h` 86.38d / `reset` 7.38d）**全部是族键 bug 的产物**，不是内容停滞；真名分别是 `supply_chain_digest` / `wechat_signals` / `wechat_daily_sqlcipher` / `wechat_cc_daily_13h` / `navicat_trial_reset`。
3. 修键后：`discover-done` 族数 20→26，未登记 19→1→**0**（唯一残留 `wechat_weekly` 已在同一批登记）。
4. 判据表新增 `state: legacy / on_demand` 静默语义 + 7 条登记（6 legacy + 1 on_demand），`targets` 37→44；**硬问题名单逐字不变**（4 条），即扩面没有把噪音变成告警。
5. 全量 A/B 双工具一致：任务级 `task_freshness` 除有意改动的 navicat 外 verdict 逐字不变；内容级 `content_change_detector` 用 HEAD 判据表与现判据表跑出的硬名单**完全相同**（2 条，同序）。

## 1. 入口缺陷（根因）

`sche_tasks/done/` 的产物命名是 `YYYY-MM-DD_HHMM_<task_id>.md`，其中 `task_id` 自身**含下划线**（`wechat_cc_daily_13h`、`price_alert`、`weekly_report_reminder`、`supply_chain_digest`…）。

- 旧实现（HEAD）：正则 `(.+)_([A-Za-z0-9_]+)\.md$` 贪婪匹配后取**最后一段**作族名 ⇒ 族名退化成 `13h`/`alert`/`reminder`/`weekly`/`daily`… 这类"末段词"。
- 后果一：与判据表的 `targets[].id`（真 task_id）比对必然全部失配（修前 20 族 19 未登记）。
- 后果二：即使不失配，族聚合也是错的——`*_alert.md` 会把 `price_alert` 与任何其它以 alert 结尾的任务混成一族。
- 证据（修前，`temp/family_calib/ccd_discover_2026-09-28.json`）：
  - `criteria_targets = 37`，`discover.families = 20`，`discover.unregistered` 19 条 = `['13h','alert','check','daily','digest','evening','inspection','monitor','monthly','morning','reminder','reset','signals','snapshot','sqlcipher','summary','trending','watcher','weekly']`。
  - 行内可见族样：`pattern = sche_tasks/done/*_13h.md`、`target = 13h`、`registered = False`。

## 2. 修复 1：`done_family_key()`（tools/content_change_detector.py）

- 新口径：从 basename 里剥掉 `YYYY-MM-DD_HHMM_` 前缀，**其余整段**即族名；仅当不存在日期前缀时才回退旧规则（末段后缀）。
- 落地形态：新增独立函数 `done_family_key()`，`discover_done()` 的分组键由内联正则改为调用它 ⇒ 单点可控、可单测。
- 修正过程中踩到的两处实现坑（已锁进自测）：
  1. 正则可选组漏写 `_`，导致 `wechat_cc_daily_13h` 类名字被截成 `13h`（症状：NC24 负控夹具跑不出预期族）⇒ 定为 `^(\d{4}-\d{2}-\d{2}_\d{4})_(.+)$`。
  2. 首次修字脚本**未真正落盘**（write 结果未校验），复跑仍是旧行为 ⇒ 改为写后立即回读断言。
- 新增断言 **NC23 / NC23b / NC23c / NC23d**（真实命名取整段 / 多下划线不截末段 / 无日期前缀回落末段 / 非 `.md` 不认的负控）与 **NC24 / NC24b / NC24c**（族键=整段任务名 / 未被截成末段 `evening` 的负控 / 该族按新键判已登记）。
- 自测口径校正（实测回读）：`git show HEAD:tools/content_change_detector.py`（=R457 提交 c73da1a）跑 `--selftest` 报 `31/31 passed`，现版报 **`51/51 passed`**，rc 均 0。+20 条中 NC21/NC22 属 §3.4 的豁免特性，NC23/NC24 属本节族键修复。

## 3. 修复 2：`state: legacy / on_demand`（tools/task_freshness.py + 判据表）

### 3.1 语义

- `state: disabled`（既有）：任务已下线，整条静默（不进 ALERT 计数、不进硬问题）。
- 新增 `state: legacy`：**任务实体与产物历史都真实存在**，但调度已退役/归档（例如 done 报告残留、launchd 已 disabled）。静默理由不同于 disabled：它保留"内容级观测"，只是不告警。
- 新增 `state: on_demand`：非周期任务（只在人工触发时跑，如 `navicat_trial_reset`），同样不参与新鲜度欠账判定。
- 分支插在 `disabled` 判断**之后**（保持既有 disabled 路径逐字不变），`LEGACY` / `ON_DEMAND` 均静默、不进硬问题。

### 3.2 反向护栏（防"用 legacy 掩盖真停滞"）

- legacy 族若在 **3 天内重新产出**（说明它其实还在跑）⇒ 输出 `WARN 登记过期`，提示把 `legacy` 改回 `active`。
- 判据表仍照实测量 class（`⏸️ OK ... reason=legacy` / `⏸️ CONTENT_STALE ... reason=on_demand`），豁免**只影响告警面与 rc**，不改测量值。
- `--strict-exempt` 可一键关闭全部豁免（反事实视图）。

### 3.3 判据表变更（tools/freshness_criteria.json）

- `navicat_trial_reset`：`disabled → on_demand`。
- 新增 6 条 `legacy`：`supply_chain_digest`、`wechat_signals`、`wechat_daily_sqlcipher`、`bridge_autonomous`、`folder_watcher`、`price_alert`。
- 补登 `wechat_weekly`（legacy）——它是族键修复后 `--discover-done` 唯一暴露的"有历史产物但未登记"族。
- `_doc` / `_calibrated` / `_counts` 同步更新；`targets` 数 37 → **43**，随后因 `wechat_weekly` 补登 → **44**。

### 3.4 同一豁免语义透传到 content_change_detector

- CCD 侧同批也落了豁免：`is_exempt(entry)` 按判据表 `state ∈ {disabled, legacy, on_demand}` 判豁免；`hard_rows()` 剔除豁免族；`render()` 单列「生命周期豁免」留痕区并在汇总行标注「已剔豁免」；`--strict-exempt` 关闭全部豁免。
- 关键口径：豁免只影响**告警面**，不改测量——豁免族照实给出 class（如 `ALL_IDENTICAL`）与 `reason`（`legacy`/`on_demand`/`disabled`），"不掩盖"由断言 NC21a 锁死。
- 断言：**NC21a–NC21i**（9 条：class 不掩盖 / 三种豁免 reason / 非豁免族 `exempt=False` / `hard_rows` 只余活跃族 / render 留痕区三查）与 **NC22a–NC22d**（4 条 strict 反事实负控：strict 下不豁免、`hard_rows` 含全部豁免族、render 无留痕区、去 state 即不再豁免）。

## 4. 证据（A/B）

### 4.1 `--discover-done`（族键修复的直接影响）

| 时点 | criteria_targets | families | unregistered |
| --- | --- | --- | --- |
| 修前（旧族键） | 37 | 20 | **19** |
| 修键后（未补登） | 44 | 26 | **1**（`wechat_weekly`） |
| 补登后（现表） | 44 | 26 | **0** |

复跑命令与产物：`temp/family_calib/ccd_discover_2026-09-28.json`（修前）、`ccd_discover_fixkey2.json`（修键后）、`ccd_discover_final.json`（现表）。

### 4.2 `task_freshness` 全量（任务级，`--json --no-ledger`，捕获 stdout）

- rc=1；目标 44，verdict 分布：`ALERT 5 / OK 21 / INFO 1 / DISABLED 9 / ON_DEMAND 1 / LEGACY 7`。
- `hard_issue_ids`（逐字不变，与 R464 基线一致）4 条：`birdie_daily_monitor`、`haisou_daily_monitor`、`sqlite_snapshot`、`linuxdo_ai_daily_summary`。
- 生命周期态 8 条：`on_demand`=navicat_trial_reset；`legacy`=supply_chain_digest / wechat_signals / wechat_daily_sqlcipher / bridge_autonomous / folder_watcher / price_alert / wechat_weekly。
- 除 navicat（有意从 disabled 改 on_demand）外，其余目标 verdict 逐字不变；差异仅时间漂移字段（hours/age/mtime/bytes）。

### 4.3 `content_change_detector` 全量（内容级，吃同一判据表）

- 现表非 strict 汇总：`族总数=61 OK=26 内容停滞(含全同，已剔豁免)=2 生命周期豁免=8 无历史=28 缺失=0`，rc=1。
- 2 条硬告警：`sqlite_snapshot/payload ALL_IDENTICAL`（n=111，distinct=1）、`weekly_inspection/payload CONTENT_STALE`。
- **跨判据表 A/B（本批关键闸门）**：用 HEAD 判据表（37 targets）与现判据表（44 targets）各跑一次，硬名单为同一集合同序
  `[(sqlite_snapshot,payload,ALL_IDENTICAL), (weekly_inspection,payload,CONTENT_STALE)]` ⇒ `identical: True`；族总数 53 → 61、豁免 7 → 8（新增 1 个 legacy 族被豁免）。即：扩面**没有**引入内容级噪音告警。
- `--strict-exempt` 反事实（关掉全部豁免）：7 条硬告警，其中 5 条是退役/沉睡任务的 evidence 历史（`supply_chain_digest` 135.07d、`wechat_weekly` 126.02d、`wechat_signals` 124.44d、`wechat_daily_sqlcipher` 97.45d、`navicat_trial_reset` 7.81d 标 `reason=on_demand`）⇒ 正是本批豁免所消掉的噪音面。

## 5. 验收对照（TODO L181 五条）

| 验收项 | 结果 |
| --- | --- |
| ①逐族给判定与依据，报告内给全表 | 全表见 §4.1 + 判据表 `_counts`；26 族中 26 已登记（0 未登记） |
| ②改后全量巡检前后对比：新增 ALERT 仅限真停滞族，legacy 族 0 新增 | 达成：`task_freshness` 硬问题 4 条逐字不变；CCD 硬名单 identical |
| ③负控：legacy 族伪造成活跃须转红；真族产物内容不变须保持 ALERT | 达成：legacy 3d 内复现产出 ⇒ `WARN 登记过期`（自测断言 ⑯）；`sqlite_snapshot`/`weekly_inspection` 内容不变仍判硬告警 |
| ④`--selftest` 含负控 | 达成：`task_freshness` 16/16（含负控⑬⑭⑮⑯），`content_change_detector` **31/31 → 51/51**（+20：NC21a–i / NC22a–d 豁免与 strict 反事实负控、NC23/NC24 族键正负例） |
| ⑤只读 | 本批未新增任何告警行为、未改生产调度；写入仅限 2 个仓内文件 |

## 6. 边界与残余

- **族键修复的影响面**：`--discover-done` 的输出语义变了（族名从"末段词"变成真 task_id）⇒ 任何曾经消费旧族名的下游（若有）都会失配；本仓检索未发现消费方，报告如实登记。
- **登记面仍未穷尽**：26 族里 `NO_HISTORY` 28 条（CCD 口径，含 launchd 状态文件类单文件族）未逐一标定，属 R457 遗留的"余 22 族"，本批按收窄条件只做 5 停滞族 + 3 disabled 族 + 族键修复暴露的 1 族。
- **`legacy` 是状态而非名单**：任务若复活，3 天内产出即触发 `WARN 登记过期`；但"3 天"是经验阈值，未做跨源标定（后续若引入 cadence 字段可按族给不同窗口）。
- **不是停用**：本批未停用任何调度，`legacy` 只作用于巡检判读面。

## 7. 变更文件

- `tools/content_change_detector.py`：新增 `done_family_key()`，`discover_done()` 改用之；新增 `is_exempt()` / `hard_rows()` 生命周期豁免（`state ∈ {disabled, legacy, on_demand}`）+ `render()` 豁免留痕区 + `--strict-exempt` 反事实开关；+NC21–NC24 断言（selftest **31/31 → 51/51**）。
- `tools/task_freshness.py`：新增 `state: legacy / on_demand` 分支与 print_table 两处渲染；+4 条自测断言（selftest 16/16）。
- `tools/freshness_criteria.json`：navicat 改 `on_demand`；+6 legacy；+`wechat_weekly` legacy；`_doc/_calibrated/_counts` 同步。

## 8. 产物

- `temp/family_calib/`：`criteria_head.json`（HEAD 判据表快照）、`ccd_discover_2026-09-28.json`、`ccd_discover_fixkey2.json`、`ccd_discover_final.json`、`ccd_headk2.json`、`ccd_after_fixkey2.json`、`ccd_before.json`、`scan_after.json`、`baseline_scan.json`、`NOTES_r470.md`。
- `temp/r470/tf_after.json`（`task_freshness --json` 原始输出）。
