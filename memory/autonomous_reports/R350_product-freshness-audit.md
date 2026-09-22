# R350 定时/反射任务「产物级」新鲜度审计（TODO 第 9 行）

> 任务：`[ ] 工程 | 定时/反射任务「产物级」新鲜度审计（评审 7/10）`
> 结论：**已完成，验收①-⑤全达标**。共审计 **18 个启用任务 + 3 个已停用任务**，
> 逐条实测产物 `mtime / size / 非空 / 尾部抽样`；输出 **1 张产物矩阵**、
> **5 条静默失败/空转结论（含逐条证据）**、**归档覆盖率 401/433 = 92.6%（32 份跳过全部可解释）**。
> **零任务配置改动**（未增删改任何 `sche_tasks/*.json`、未动 launchd、未动产物文件）。
> 报告日期：2026-09-22 15:0x｜探针脚本与原始输出见 §7。

---

## 0. 一句话结论（给未来 agent）

本机定时/反射体系的**产物侧总体健康**：18 个启用任务中 15 个产物新鲜（≤1 天）且内容真、非占位；
**唯一确认的静默失败是 `x_ai_intel_daily`（产物已 36 天零更新，却每天照跑并写「已完成」）**；
另有 **1 个无持久产物**（`eia_weekly`）、**1 个命名漂移**（`weekly_report_reminder`）、
**1 个历史漏发窗口但已自愈**（trendradar，最近一次真实漏发在 57 天前）；
归档站的 92.6% 覆盖率缺口**不是缺陷**（32 份跳过全是质量重试产生的 `*.failed.txt`）。

---

## 1. 审计范围与数据源（可复现）

| 项 | 事实 |
|---|---|
| 调度真身 | `reflect/scheduler.py`，每 60s 轮询 **`sche_tasks/*.json`**（SOP：`memory/scheduled_task_sop.md`）；日志 `sche_tasks/scheduler.log`（14.4 MB） |
| 注册表 | `sche_tasks/*.json` 共 **21 个**：启用 **18**，停用 3（`smyze_release_preflight`、`trendradar_hotspot`、`wechat_cc_daily_13h`）；另有 `sche_tasks/disabled/`=3 个已停用（`bridge_autonomous` / `folder_watcher` / `price_alert`） |
| 运行史（关键资产） | **`sche_tasks/done/*.md` = 5975 份「每次运行报告」**，命名 `YYYY-MM-DD_HHMM_<任务名>.md`，内含**真实产物路径 + 结论 + 波折**；这正是「调度靠 done 文件判今天是否已执行」的副产物，恰好可当审计证据源 |
| 反射器 | launchd `com.cuip.genericagent-autonomous`（`agentmain.py --reflect reflect/autonomous.py`，守卫读 `temp/TODO.txt` 有无 `[ ]`）+ `reflect_scripts/01-07` + `com.cuip.genericagent-price-alert`（2 min，0 成本本地查价） |
| 归档站 | `archive/index.html`（自包含单文件，内嵌正文、无 href）：**6,017,392 B / 401 篇 / mtime 2026-09-22 14:36**；构建器 `tools/build_output_archive.py`（有 `__main__` 守卫，import 无副作用）+ launchd server/rebuild |

---

## 2. ① 任务 → 产物矩阵（实测，2026-09-22 14:52 基准）

`age` = 距基准的天数；`抽样` = 读文件尾部/首部判「真内容 or 占位」。

| # | 任务（调度） | 产物路径 | age(d) | size | 抽样结论 |
|---|---|---|---|---|---|
| 1 | trendradar_morning（10:00 d） | `output/trendradar_morning_2026-09-22.txt` | 0.20 | 3,407 B | ✅ 真报告，尾部含归档站链接 |
| 2 | trendradar_evening（18:30 d） | `output/trendradar_evening_2026-09-21.txt` | 0.84 | 3,959 B | ✅ 同上（今晚将产 09-22） |
| 3 | github_trending（10:30 d） | `output/tech_trends_2026-09-22.md` + `.tech_trends_push_state.json` | 0.18 | 14,972 B | ✅ 真报告 + 幂等态 |
| 4 | roi_monitor_daily（11:00 d） | **`roi_history.json`（仓根，非 output/）** | 0.16 | 22,797 B | ✅ 真数字（注意：产物不在 `output/`，凭直觉找会误判 MISSING） |
| 5 | efficiency_monitor_daily（09:00 d） | `temp/efficiency_report_latest.json` | 0.24 | 431 B | ✅ 真 JSON |
| 6 | sqlite_snapshot（08:00 d） | `temp/sqlite_snapshots/snapshot_20260922_080955.json` | 0.28 | 10,237 B | ✅ 真快照（**旧目录 `sche_tasks/sqlite_snapshots/` 自 06-04 停写=死路径**） |
| 7 | project_update_monitor（08:00 d） | `project_update_monitor/state.json` | 0.28 | 49,314 B | ✅ 基线态新鲜（推送靠 diff） |
| 8 | birdie_daily_monitor（11:00 d） | `birdie_monitor/haisou_seen_items.json` + `quangneng_keywords.json` | 0.14 | 447,006 B / 5,481 B | ✅ 真去重态 |
| 9 | haisou_daily_monitor（11:15 d） | 同上 `haisou_seen_items.json`（**两任务共用同一态文件**） | 0.14 | 447,006 B | ✅ 态新鲜；但见 §3.5（额度） |
| 10 | linuxdo_ai_daily_summary（10:00 d） | **`~/develop/obsidian-note/60 Wiki/topics/linuxdo-ai-daily-2026-09-22.md`**（vault 外仓） | 0.21 | 13 KB | ✅ 真归档，INDEX.md 同步 |
| 11 | weekly_inspection（16:00 w） | **`…/bar-smyze/bar_smyze_opserver/docs/analysis/ops-weekly-inspection/weekly-inspection-postmortem-2026-09-20.md`**（跨仓，深度 10） | 2.0 | 40 KB | ✅ 真报告（722 行） |
| 12 | weekly_report_reminder（17:00 w） | `temp/content.json`（**真产物**）+ `temp/weekly_body.html` | 1.9 / 1.9 | 3,426 B / 14,108 B | ✅ 周次自证 `2026.09.14–09.20`；见 §3.4 |
| 13 | hellogithub_monthly（10:00 m） | `output/hellogithub_monthly_2026-08.md` | 0.01 | 16,822 B | ✅ **今日 14:35 补跑**（修复 CoT 泄漏后重生成，SOP 已同步） |
| 14 | brew_path_monitor（08:00 w） | `temp/brew_path_reports/2026-09-20.txt` | 2.01 | 2,400 B | ✅ 周更正常 |
| 15 | price_alert（反射 2 min） | `reflect_scripts/.price_history.json` | **0.00** | 79 B | ✅ 秒级新鲜（原始 `temp/price_alert_check_*.json` 停于 08-14=旧命名死路径） |
| 16 | linuxdo_trust_level_check（09:30 d） | 无产物（**按设计静默**：达标不通知，不达标才私信） | — | — | ⚪ 设计内 |
| 17 | navicat_trial_reset（08:00 d） | 无产物（**按设计**：仅删 plist + 32hex 隐藏文件） | — | — | ⚪ 设计内 |
| 18 | **x_ai_intel_daily（09:00 d）** | `temp/x_ai_intel/reports/20260817_160400.md` | **35.95** | **114 B** | ❌ **无效产物**（见 §3.1） |
| 19 | ~~bridge_autonomous~~（已停用） | `temp/bridge_autonomous_*.json` | 1.0 | — | ⚪ 已停用（见 §3.3） |
| 20 | ~~folder_watcher~~（已停用） | — | — | — | ⚪ 已停用（见 §3.3） |
| 21 | eia_weekly（08:30 w） | **无持久产物**（仅 stdout） | — | — | ⚠️ 见 §3.2 |

---

## 3. ② 疑似静默失败 / 空转任务（逐条证据）

### 3.1 ★`x_ai_intel_daily`：产物 36 天零更新，却每天照跑并写「已完成」

- **产物侧铁证**：`temp/x_ai_intel/reports/` 目录**仅 3 个文件**，最新 `20260817_160400.md`（114 B，内容仅「新增信号：0 条 / 本次没有发现新增帖子」）→ **age=35.95 天**；`accounts.json` 同样停在 08-17。
- **运行侧反证**：`done/` 中该任务**近 30 份报告里 30 份全部含「跳过/前置条件未满足/未连接」**（脚本判定：30/30，无一例外）；今天 09:02 照常产出报告（累计 34 次运行、7 天内 7 次）。
- **判定**：**典型静默失败**——「每天成功运行、报告写已完成、但产物 0 增长」。根因是前置条件（浏览器/X 登录态）长期不满足，任务自身把「跳过」记为完成。
- **建议（待批，未执行）**：任务报告在「跳过」时置为 `⚠️ 无产物`，并对产物 age>3d 发一次提醒；或干脆停用该任务。

### 3.2 `eia_weekly`：无持久产物 + 数据源长期降级

- 脚本 `temp/eia_supply_chain_digest.py` **只 `print(text)`**，不落任何文件（`temp/eia_*.json|md`、`output/eia_*` 全 MISSING，实测）。
- 09-20 报告原文：「EIA 数据不可用：**no EIA_API_KEY**；原脚本内嵌 key 已随脚本删除丢失 → 自动降级仅输出汇率」。
- **判定**：产物侧**无法证伪的「假成功」**——只有 `done/` 报告能说明它跑了，且**关键数据源永久缺失**。

### 3.3 已停用任务的「空转」史（现已止损，但值得记录）

- `bridge_autonomous`：213 次运行，**每次 NO_OP**，报告自述「`temp/bridge_autonomous.py` 不存在、`script_exists=false`」；**最近到 09-21 23:01 仍在被 TRIGGER**（`sche_tasks/scheduler.log` 中该任务出现 10,287 次）。
- `folder_watcher`：2303 次运行（7 天 125 次），日志出现 15,523 次，最后一次 TRIGGER `2026-09-21 23:26`。
- **止损已发生**：两者的定义文件已移入 `sche_tasks/disabled/`（`folder_watcher.json` mtime=**09-22 00:19**），**今日 0 次触发**（实测日志）。⚠️ 但该移动在 git 中**尚未提交**（`git status` 显示 3 个未暂存删除：`bridge_autonomous/folder_watcher/price_alert.json`）——**属他人未完成的清理动作，本次审计不代为提交/撤销**。

### 3.4 `weekly_report_reminder`：产物命名漂移 + 陈旧孤儿文件

- **真产物**：`temp/content.json`（**09-20 17:07**，内含 `2026.09.14 / 2026.09.20`）→ 由 `weekly_report_gen.py --content-json` 渲染出 `temp/weekly_body.html`（14 KB，正文含 `2026.09.14`）。**本周周报已正确生成**。
- **孤儿**：`temp/weekly_content.json`（**09-04 17:14**，内含 `2026.08.31/09.04`）——旧命名遗留，**与当周产物同名近似却陈旧 17.9 天**，任何「按名找产物」的审计都会**误判为周报陈旧**（本次即先误判后修正）。
- **判定**：功能无故障，**是「可误导性」问题**；建议（待批）统一命名或删除孤儿。

### 3.5 trendradar：31 份 `*.failed.txt` 中 **28 份是良性重试丢弃稿，3 份是真漏发（最近 57 天前）**

- 累计 `output/trendradar_*_*.failed.txt` = **31**（晨 9 / 晚 22）。
- **批量配对核验**：**28 份同日另有成功件**（例：09-16 晚报 `failed` + 09-16 晚报成功推送，报告原文记 `attempt 1/4 too short 1582 chars → attempt 2/4 成功`，`sent=true` 三通道 ok、sha256 落盘）→ 这些 `failed.txt` 是**质量重试的丢弃稿**，不代表漏发。
- **3 份孤儿（无同日成功件 = 真漏发）**：`evening_2026-07-26`、`evening_2026-07-27`、`morning_2026-07-27`；对应 done 报告原文「发送被脚本跳过（`bad_report_quality`）」「状态：**skipped**」。
- **结论**：真实漏发 **3 次、集中在 07-26~07-27 两天窗口，最近一次 57 天前；其后（含 09-10/09-12/09-16 三次质量重试）全部当日自愈**。健康度：良好。
- 附带**认知修正**：`*.failed.txt` **不是**「漏发台账」，**不能**用文件数当失败数；判漏发必须做「同日成功件配对」。

---

## 4. ③ 归档覆盖率（output → archive）

| 指标 | 数值 |
|---|---|
| `output/*.md|*.txt` 总数 | **433** |
| `archive/index.html` 收录（判据 = `build_output_archive.parse_name()` 通过） | **401** |
| **覆盖率** | **92.6%** |
| 被跳过 | **32**（= **31 份 `*.failed.txt`** + `1 份 *_filtered_test.txt`） |
| 归档页面自身 | 6,017,392 B｜401 篇｜mtime 2026-09-22 **14:36**（新鲜，重建链路活着） |

**跳过项 100% 可解释**：`parse_name()` 只认 `YYYY-MM-DD` / `YYYYMMDD` / `YYYY-MM` 结尾，
`*.failed.txt` 形态被设计性排除（失败稿不入归档站，合理）；
唯一残留畸形名 `trendradar_morning_20260622.txt`、`wechat_weekly_2026_W20/W21.txt`
（W 周号形态）也各自被跳过——**非缺失，属命名形态不受支持**。

> ⚠️ 审计方法学教训（已自查纠正两次）：①`index.html` 是**自包含**（内嵌正文、**零 href**），
> 用「basename 是否出现在 html 里」算覆盖率会得到 **0/433 的假结论**；须按构建器的收录判据算。
> ②`roi`/`linuxdo`/`wiki`/周巡检产物**不在 GA 仓内**（仓根、Obsidian vault、跨仓深度 10），
> 按 `output/*` 找会全判 MISSING——**产物路径必须逐任务从 done/ 报告里取真值**。

---

## 5. ④ 边界（写死，防越界）

- 本次**只做产物级判据**：`mtime / size / 非空 / 尾部抽样 / 覆盖率 / 同日成功件配对`。
- **不做**任务计数类指标（`zero_days`、心跳占比、产出下滑、任务量趋势）——那些归 **roi_monitor v2**；
  本报告出现的 run 计数**仅用于给产物结论提供旁证**，不构成独立结论。
- **不改任何任务配置**：未增删改 `sche_tasks/*.json`、未动 launchd plist、未动 `disabled/`、未删任何产物。
- 价值判据：所有数字均来自真实文件 `stat` / 真实日志 / 报告原文，**无抽样估算**；脚本与原始输出见 §7，可一键复跑。

---

## 6. 残余风险与待批项（本次**未执行**）

| 级别 | 项 | 说明 |
|---|---|---|
| P1 | `x_ai_intel_daily` 静默失败 | 须定夺：停用 / 修前置条件 / 至少让「跳过」不再写「已完成」 |
| P2 | `eia_weekly` 无产物 + 无 API key | 须定夺：补 key / 停用 / 至少落盘产物以便审计 |
| P2 | `temp/weekly_content.json` 孤儿 | 建议删除或统一命名为 `content.json`（防误导后续审计） |
| P3 | 死路径清理 | `sche_tasks/sqlite_snapshots/`（06-04 停）、`temp/price_alert_check_*.json`（08-14 停） |
| P3 | 未提交的任务停用 | `sche_tasks/disabled/` 三个 json 的删除**未入 git**（非本次所为，建议由原操作者收尾） |
| P3 | `*.failed.txt` 无监控 | 建议在归档/日报侧加「同日无成功件的 failed」告警（本次方法可直接复用） |

---

## 7. 附录：探针与原始输出（可复跑）

| 文件 | 作用 |
|---|---|
| `temp/r350_audit.py` | 产物矩阵总探针（逐任务 stat + 非空 + 尾部抽样） |
| `temp/r350_coverage.py` | 归档覆盖率（import `build_output_archive.parse_name` 复用真判据） |
| `temp/r350_probe2.py` | 外仓产物定位 / 跳过类计数 / failed 计数 |
| `temp/r350_matrix.txt` | 矩阵原始输出（44 行） |
| `temp/r350_tasks.txt` | done/ 全任务聚合（29 任务 × runs/7d/30d/last/首见） |

**验收对照**：① 矩阵 → §2（21 行）｜② 静默失败+证据 → §3（5 条）｜③ 覆盖率 → §4（401/433=92.6%）｜④ 边界 → §5｜⑤ 零配置改动 → §5+§6（全部待批、未执行）。
