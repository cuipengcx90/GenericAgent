# R399 ga-api 退役判定 + 21 个 `sche_tasks` / 8 个 launchd 任务「产物级」新鲜度复测

- **测量时刻**：2026-09-25 10:14–10:17（Asia/Shanghai，本机）
- **改动**：**零改动**（只读）。未删/未移任何文件，未改任何 plist 或任务 JSON，未写任何任务产物，未回收任何日志。
- **依据 SOP**：`memory/scheduled_task_freshness_audit_sop.md`（核心口径：**以产物 mtime 为准，不看「跑没跑」**）
- **本文口径**：所有 `age`（天）= 测量时刻 − 文件 mtime；数值与测量时刻绑定，跨时刻不可比。正文数字均取自 §9 附录的当场原始输出。

---

## 1. 结论摘要

| # | 结论 | 数字 |
|---|---|---|
| 1 | 21 个 `sche_tasks` 三分 | 健康 **13** / 外部条件缺失·可见失败 **2** / 无意义空转 **1** / 已停用残留 **5** |
| 2 | 8 个 genericagent launchd | 在跑 **5** / 未运行但配置即如此 **2** / **未加载的孤儿 1（ga-api）** |
| 3 | 09-21 归档 4 个心跳任务后是否出现**新的真静默失败** | **0 例** |
| 4 | 本轮最大发现 | `sqlite_snapshot` 是唯一「技术上在跑、业务上无意义」的僵尸：108 份快照、相邻对比 107 次、**载荷变化 0 次** |
| 5 | ga-api 的性质（订正旧说） | **不是「已 disabled」，而是「plist 在盘 + `KeepAlive=true` + launchd 未加载」的孤儿**；一经 bootstrap 会无限重启刷日志 |
| 6 | 待批建议（本轮不执行） | ga-api 退役（移出 LaunchAgents）+ 回收其 58,379,981 B 日志；并把 `sche_tasks/` 里 5 个 `enabled=false` 残留归位到 `sche_tasks/disabled/` |

---

## 2. 方法与口径（含本轮新增的 4 条判据）

沿用 `scheduled_task_freshness_audit_sop` 的三类分判（真静默失败 / 外部条件缺失 / 无意义空转），并补充本轮实测暴露的判据：

1. **产物 mtime 是唯一硬判据**，但「产物不存在」**不等于**故障——须先读任务设计语义（例：`eia_weekly` 靠推送交付、无持久产物；`pet` 的 `KeepAlive=false` 是设计）。
2. **日志只记失败 ≠ 全程失败**——须读源码确认打印语义（例：`06_price_alert.py` 唯一 `print` 在失败分支，成功是静默的）。
3. **「没到点 / 在飞行中」≠ 停摆**——须交叉 `schedule` 字段 + `logs/scheduler_reflect.log` 尾部（例：10:00 批任务在 10:17 尚无今日产物，但调度器日志显示仍在跑）。
4. **目录位置 ≠ 启用状态**——`sche_tasks/*.json` 里 `enabled=false` 的任务仍在启用目录内，须读 JSON 标志并以源码确认（`reflect/scheduler.py:114`：`if not task.get('enabled', False): continue`；注意调度器源码在 **`reflect/`** 目录，`reflect_scripts/` 下**没有** `scheduler.py`——本文初稿曾误写为后者，已订正）。

**数据源**：`sche_tasks/*.json`（21）、`sche_tasks/disabled/*.json`（3）、`sche_tasks/done/*.md`（6014）、`~/Library/LaunchAgents` 匹配 `genericagent|desktop-bridge`（8）、`launchctl list`、`launchctl print gui/501/<label>`、以及各任务**自声明**的产物路径。

---

## 3. 21 个 `sche_tasks` 逐项判定

（`last_report` 为 `sche_tasks/done/` 里该任务最新报告的文件名前 16 字符 = 日期_时分）

| 任务 | enabled | schedule | last_report | 判定 |
|---|---|---|---|---|
| birdie_daily_monitor | True | 11:00 | 2026-09-24_1132_ | **②外部条件缺失·可见失败** |
| brew_path_monitor | **False** | 08:00 | 2026-09-20_1434_ | ④已停用残留 |
| efficiency_monitor_daily | True | 09:00 | **2026-09-25_0900_** | ①健康（今日已跑） |
| eia_weekly | True | 08:30 | 2026-09-20_1443_ | ①健康（**周任务**，下次 09-27） |
| github_trending | True | 10:30 | 2026-09-24_1031_ | ①健康（10:30 未到） |
| haisou_daily_monitor | True | 11:15 | 2026-09-24_1228_ | ①健康（11:15 未到） |
| hellogithub_monthly | True | 10:00 | 2026-09-22_1435_ | ①健康（**月任务**） |
| linuxdo_ai_daily_summary | True | 10:00 | 2026-09-24_1000_ | ①健康（10:00 批在飞行；Wiki 落点 age 1.0d） |
| linuxdo_trust_level_check | True | 09:30 | **2026-09-25_0931_** | ①健康（今日已跑） |
| navicat_trial_reset | **False** | 08:00 | 2026-09-20_1516_ | ④已停用残留 |
| project_update_monitor | True | 08:00 | **2026-09-25_0801_** | ①健康（今日已跑） |
| roi_monitor_daily | True | 11:00 | 2026-09-24_1253_ | ①健康（今日 11:00 未到） |
| smyze_release_preflight | **False** | 09:00 | NO_REPORT | ④已停用残留（从未有报告） |
| sqlite_snapshot | True | 08:00 | **2026-09-25_0815_** | **③无意义空转** |
| trendradar_evening | True | 18:30 | 2026-09-24_2055_ | ①健康 |
| trendradar_hotspot | **False** | 00:00 | NO_REPORT | ④已停用残留 |
| trendradar_morning | True | 10:00 | 2026-09-24_1008_ | ①健康（10:00 批在飞行） |
| wechat_cc_daily_13h | **False** | 22:00 | 2026-07-03_1511_ | ④已停用残留（**报告停在 83 天前**） |
| weekly_inspection | True | 16:00 | 2026-09-24_1611_ | ①健康（产物 age 0.4d） |
| weekly_report_reminder | True | 17:00 | 2026-09-24_1701_ | ①健康（产物 age 0.7d） |
| x_ai_intel_daily | True | 09:00 | **2026-09-25_0903_** | **②外部条件缺失**（今日有报告，但下游产物停 38.8d） |

计数自证：①健康 13 + ②2 + ③1 + ④5 = **21**。

---

## 4. 三类分判明细

### 4.1 真静默失败：**0 例**

09-21/09-22 归档 4 个心跳任务（`folder_watcher` / `price_alert` / `bridge_autonomous`，以及本次判定为空转的 `sqlite_snapshot` 仍在跑）之后，**没有任何任务出现「在跑但既无产物也无报告、且不报错」的静默失败**——本轮所有异常都能在任务自己的 done 报告里找到明确记载（birdie 41 份失败迹象）、或在调度器日志里找到在飞行证据。

### 4.2 外部条件缺失 / 可见失败：**2 例**

**(a) `birdie_daily_monitor`（每日 11:00，`max_delay_hours=6`）**

- 产物：`birdie_monitor/birdie_seen_items.json`，8202 B，mtime `Aug  7 11:00:50 2026`（**age 49.0 d**）
- 报告统计（`sche_tasks/done/*birdie_daily_monitor.md` 共 **58** 份）：`exit=0` **3** 份（末次成功 `2026-08-07_1100`）；显式 `exit=1` **12** 份（最早 `2026-08-17_1101`，最新 `2026-09-24_1132`）；未记 exit 但正文含 `SSL`/`502`/`超时` 失败特征 **29** 份；未记 exit 且正文为「无新增命中」正常静默 **13** 份；其它 1 份。
- 失败形态（逐字摘自 `2026-09-24_1132` 报告）：签到是脚本**第一步**，异常为 `requests.exceptions.SSLError ... SSLEOFError(8,'[SSL: UNEXPECTED_EOF_WHILE_READING]')`，失败点 `birdie_client.py:73 requests.post('https://www.birdiesearch.com/api/user/sign')` ⇒ **站点侧不可达**，未消耗搜索额度、未推送、未改本地数据。
- **判定**：非静默——每轮都留下报告；性质是**外部条件缺失（目标站点不可达）**，且失败发生在签到阶段（早于搜索/推送）。注意：产物冻结**不能单独作为判据**（该文件是去重台账，仅在有新增命中时才写）。
- 恢复条件（外部）：`www.birdiesearch.com` 恢复可达 + cookie 有效；**本机不可自行修复**。

**(b) `x_ai_intel_daily`（每日 09:00）**

- 今日（09-25 09:03）**有**报告（任务照跑），但下游产物 `reports/` 最新停在 2026-08-17（**age 38.8 d**）。
- 依 `memory/x_ai_intel_daily_sop.md`：前置条件 = **已登录 x.com 的普通标签页**；`OneTab` 等扩展页不算会话 ⇒ 驱动会话恒 0。当前无该前置 ⇒ 任务每轮空转出「无数据」报告。
- **判定**：外部条件缺失（需用户登录 x.com）；非脚本缺陷，**禁自动重试**（SOP 明文「3 步只读速判 / 禁重试」）。

### 4.3 无意义空转：**1 例 —— `sqlite_snapshot`**

- 快照数 **108**（`temp/sqlite_snapshots/*.json`，目录 age 0.1d = 今天 08:15 仍在写）；相邻两两 diff **107** 次；
- **仅比 `dbs` 载荷的载荷变化次数 = 0，变化日 = `[]`**（末快照键 = `['timestamp','dbs']`，dbs 成员 = `temp/message_2_merged.db`、`temp/hive_coupon_fix/m7hpgl58zzb4.db`）；
- 两个被监控库的 mtime：`temp/message_2_merged.db` = `May 19 16:35:49 2026`（**128.7 d**）、`temp/hive_coupon_fix/m7hpgl58zzb4.db` = `Jun  4 02:19:21 2026`（**113.3 d**）；
- **判定**：任务、调度、写盘全部正常，但**监控对象已 4 个月前冻结** ⇒ 每天产出一份与前一天完全相同的快照，零信息量。属配置层失效（监控目标陈旧），建议处置**待批**（停用或改监控有效库）。

### 4.4 健康 13 例（要点）

- 今日已跑并落报告：`project_update_monitor`(08:01)、`sqlite_snapshot`(08:15，虽空转)、`efficiency_monitor_daily`(09:00)、`x_ai_intel_daily`(09:03)、`linuxdo_trust_level_check`(09:31)。
- 10:00 批在飞行：`trendradar_morning`、`linuxdo_ai_daily_summary`（见 §8 未验证项）。
- 产物级新鲜度（今天不必跑的长周期任务）：`weekly_inspection` 产物目录 `~/develop/WorkSpaces/idea/smyze/bar-smyze/bar_smyze_opserver/docs/analysis/ops-weekly-inspection` **age 0.4d**（最新 `weekly-inspection-postmortem-2026-09-24.md`，Sep 25 00:00）；`weekly_report_reminder` 产物 `temp/content.json` **age 0.7d**；`linuxdo_ai_daily_summary` 产物 `~/develop/obsidian-note/60 Wiki/topics` **age 1.0d**。
- `eia_weekly`：`repeat=weekly`、`schedule=08:30`，末次报告 `2026-09-20 14:44`「✅ 脚本执行成功（EIA数据降级，汇率摘要正常生成）」⇒ **周任务正常**（下次 09-27）；EIA 数据源降级但任务成功，非故障。

### 4.5 已停用残留：**5 例（分类学缺口）**

`brew_path_monitor` / `navicat_trial_reset` / `smyze_release_preflight` / `trendradar_hotspot` / `wechat_cc_daily_13h` 的 JSON 都是 `enabled=false`，**但仍留在 `sche_tasks/` 启用目录内**；而 `sche_tasks/disabled/` 只有 3 个（`bridge_autonomous` / `folder_watcher` / `price_alert`）。

- 源码确认不会跑：`reflect/scheduler.py:114` `if not task.get('enabled', False): continue`（实测原文，见 §9·J）。
- 缺口：**「停用」有两种表示法并存**（移入 `disabled/` vs 原地置 `enabled=false`）⇒ 看目录会误判为「21 个活跃任务」。建议（待批）统一归位到 `disabled/`。

---

## 5. 8 个 launchd 作业

| Label | plist 状态 | 实际加载（`launchctl list`） | 日志 age(d) | 判定 |
|---|---|---|---|---|
| com.cuip.genericagent-scheduler | enabled, KeepAlive/RunAtLoad=True | **607** | 0.0 | ①在跑 |
| com.cuip.genericagent-price-alert | enabled, KeepAlive/RunAtLoad=True | **608** | 0.4 | ①在跑（§5.1） |
| com.cuip.genericagent-autonomous | enabled, KeepAlive/RunAtLoad=True | **614** | 0.0 | ①在跑 |
| com.cuip.genericagent-launch | enabled, KeepAlive/RunAtLoad=True | **628** | 2.0 | ①在跑（钉钉 sidecar） |
| com.cuip.genericagent-l2-gate | enabled, KeepAlive/RunAtLoad=True | **634** | 2.0 | ①在跑（§5.2） |
| com.cuip.genericagent-pet | enabled, **KeepAlive=False RunAtLoad=False** | `-`（已加载未运行） | 100.8 | ②非故障（配置即如此） |
| com.genericagent.desktop-bridge-repair | enabled, **WatchPaths=True** RunAtLoad=False | `-`（已加载未运行） | 56.7 | ②非故障（事件驱动，未触发） |
| **com.cuip.genericagent-ga-api** | enabled, **KeepAlive/RunAtLoad=True，无 Disabled 键** | **不在 `launchctl list` 中** | 11.0 | **③未加载的孤儿** |

### 5.1 `price-alert` 反射器：健康（曾差点误判）

- 产物 `reflect_scripts/.price_history.json`，mtime **Sep 25 10:15**（age 0.0），内容 2 币：`{'bitcoin': [1790302503.16276, 84826], 'ethereum': [1790302503.16276, 2697.04]}`。
- 日志 `logs/price_alert_reflect.log` age 0.4d，含 **131** 行「价格监控失败」，**0** 行成功记录。
- **订正**：`reflect_scripts/06_price_alert.py`（共 83 行）唯一的 `print` 在失败分支（第 80 行 `print(f"[价格监控失败] {e}")`）⇒ **成功即静默**；当场实测 `api.coingecko.com` = HTTP **200**（t=0.698 s）⇒ 那 131 行只是历史瞬时抖动。**判定：健康**。

### 5.2 `l2-gate` 反射器：健康

- 产物 `./.ga/l2/{index.json,index.md,snapshot.json}`，mtime 全部为 **Sep 25 09:51**（age 0.0）。
- 其日志 `logs/l2_gate_reflect.log` age 2.0d **不是**异常：该保活件「**全绿零输出，仅 ERROR 才按指纹去重通知**」（见记忆 L2），验证新鲜度必须看副作用 `.ga/l2/index.*` 的 mtime——本轮 mtime 为 0.0d，**与 30min 周期一致**。

### 5.3 `pet` / `desktop-bridge-repair`：非故障（配置即如此）

- `pet`：`KeepAlive=False RunAtLoad=False`，`launchctl list` 显示 `- 0`（已加载、未运行）⇒ 设计如此，日志 100.8d 空白属预期。
- `desktop-bridge-repair`：`WatchPaths=True`、`RunAtLoad=False`，监视文件 `~/.ga_desktop_settings.json`（mtime `Aug  2 13:17:18 2026`，**53.9 d** 未变）。**订正**：它不是「从未触发」——自写日志 `~/Library/Logs/com.genericagent.desktop-bridge-repair.log` 有 **1396 B**，最后一条 `2026-09-18T01:34:54 SKIP override already correct`（此前还有 08-22 / 08-25 / 08-29 / 09-02 / 09-11 各一条），即 **WatchPaths 确实被触发过多次，每次都是幂等 no-op**；launchd 侧的 `.launchd.out.log` / `.launchd.err.log` 均为 **0 B**（`Jul 30 16:24`）。⇒ **非失败**，是「触发即确认无需变更」的正常行为。

---

## 6. ga-api 退役判定（证据链）与建议

### 6.1 五条证据（全部当场实测）

1. plist 存在于 `~/Library/LaunchAgents/com.cuip.genericagent-ga-api.plist`，内容为 `uvicorn temp.ga_api_server:app --host 127.0.0.1 --port 8765`，`RunAtLoad=true`、`KeepAlive=true`，**没有 `Disabled` 键**。
2. **未被 launchd 加载**：`launchctl print gui/501/com.cuip.genericagent-ga-api` → `Could not find service "com.cuip.genericagent-ga-api" in domain for user gui: 501`；`launchctl list` 里无该标签。
3. 目标模块已不存在：`temp/ga_api_server.py` 已删（任务书背景亦如此记载）⇒ 日志尾部全是 `ERROR:    Error loading ASGI app. Could not import module "temp.ga_api_server".`
4. **端口 8765 无服务**（本机无 `lsof`，改用 python socket 实测：`127.0.0.1:8765` → `ConnectionRefusedError [Errno 61] Connection refused`；对照 GA 真正在用的驱动端口 `127.0.0.1:18765` → `CONNECTED`）。**仓内无消费者**——`grep` 对 `8765` 的命中**全部落在 `temp/loopx_research/loopx_repo/**`（LoopX 上游仓库快照的 demo/测试代码）与 `temp/wechat_29groups_raw.json`（长数字假命中）**，与本机 GA 无关；GA 自身 `TMWebDriver.py` 走的是 **18765**。
5. 日志 `logs/ga_api_server.log` = **58,379,981 B**，mtime `Sep 14 10:06:45 2026`（age 11.0d）；**日志头部历史**曾有 `INFO: Uvicorn running on http://127.0.0.1:8765` ⇒ 该服务曾真实可用，之后模块被删、服务被停。

### 6.2 性质订正 + 建议（**待批，本轮不执行**）

- 性质：**不是「已 disabled」，而是「plist 在盘 + `KeepAlive=true` + 未加载」的孤儿**。风险点在于：**一旦有人 `bootstrap` 它，会因模块缺失而无限重启并高速刷日志**（`KeepAlive=true` 的典型行为）。
- 建议动作（一条待批建议，含三项）：①把该 plist 移出 `~/Library/LaunchAgents`（或写成 `Disabled=true` + 明确注释）；②回收 `logs/ga_api_server.log`（**58,379,981 B**，可并入 R393 的日志回收计划 `temp/log_reclaim_plan.py --apply --approve "批准回收"`）；③顺带处置 `sche_tasks/` 里 5 个 `enabled=false` 残留（§4.5）。
- 收益：消除「一经 bootstrap 即日志风暴」的隐患 + 回收 58 MB；风险：近乎为零（无消费者、无监听、模块已删）；回滚：把 plist 移回并 `launchctl bootstrap` 即可（但需先恢复 `temp/ga_api_server.py`）；不批的后果：隐患留存、日志继续占盘。

---

## 7. 本轮口径坑（自纠 6 条）

1. **快照 diff 必须只比 `dbs`**：首测把整个 JSON（含 `timestamp`）做 diff，误报「107/107 全变化」；剔除 `timestamp` 后真值 = **0 次载荷变化**。
2. **「日志 0 成功行」≠ 故障**：`price-alert` 成功是静默的（唯一 `print` 在失败分支）⇒ 差点把健康件误判为「外部条件缺失」。
3. **长周期任务不能按日判**：`eia_weekly` 5 天无报告属正常（`repeat=weekly`）；`hellogithub_monthly` 同理。
4. **ga-api 旧说须订正**：任务背景与我早期工作记忆写的「plist 已 disabled」不准确——实测 **plist 无 `Disabled` 键，是「未加载」**（`launchctl print` 实证）。
5. **「产物不存在」≠ 故障**：`eia_weekly` 无持久产物（靠推送交付）；`pet` 未运行是配置；`desktop-bridge-repair` 是 `WatchPaths` 事件驱动。
6. **目录位置 ≠ 启用状态**：`sche_tasks/` 下 5 个任务是 `enabled=false`，须读 JSON 标志 + `scheduler.py:114` 源码才能判。**且调度器源码在 `reflect/` 不在 `reflect_scripts/`**（初稿写错路径，靠 `grep` 逐字回读才抓到；两目录并存极易混）。

**以下 3 条为交付前「数字/断言回读」阶段自查出的错误（沿用规则 21）**：

7. **「日志 0 B」只适用于 launchd 的 `.out/.err`**：`desktop-bridge-repair` 自写日志是 1396 B 且末条 `2026-09-18T01:34:54 SKIP override already correct` ⇒ 初稿「从未触发」是**错的**，真实行为是「触发多次、每次幂等 no-op」。**判「从未触发」必须同时看自写日志，不能只看 launchd 的 out/err**。
8. **本机没有 `lsof`**（`/bin/sh: lsof: command not found`）⇒ 任何「`lsof` 无输出 ⇒ 无监听」的结论都是**假证**；改用 python `socket.connect` 实测（8765 `ConnectionRefusedError` vs 18765 `CONNECTED`）才是硬证。
9. **`grep` 命中 ≠ 仓内消费者**：`8765` 的命中全在 `temp/loopx_research/loopx_repo/**`（LoopX 上游快照）与 `temp/wechat_29groups_raw.json`（长数字）⇒ 判「有无消费者」必须按**路径归属**过滤（排除 vendored/研究快照），否则会把上游 demo 当成自己代码的消费者。

---

## 8. 未验证 / 边界

- **10:00 批在飞行，未复测到落盘**：10:14–10:17 三次复测，`output/` 今日 0 份、`done/` 今日恒 5 份（08:01 / 08:15 / 09:00 / 09:03 / 09:31），而 `logs/scheduler_reflect.log` 尾部显示调度器**正在跑**（`[MixinSession] Using session (deepseek-flash)`）。⇒ `trendradar_morning`、`linuxdo_ai_daily_summary`（可能含 `hellogithub_monthly`）的今日结论**尚未成立**，建议 10:40 之后另跑一次复测。**本文不对其下判定**（仅标「未到点/在飞行」）。
- `birdie_daily_monitor` 今日 11:00 的轮次在本文测量时刻**还没到**（`schedule=11:00`）；其恢复依赖外部站点。
- `x_ai_intel_daily` 需用户登录 x.com（人工前置，非脚本可解）。
- 未评估：停用 `sqlite_snapshot` 是否有下游依赖（本轮未追踪快照消费者）。
- 未执行：退役、日志回收、残留归位——**均为待批**。
- 未读任何密钥明文；未修改任何被审计对象。

---

## 9. 附录：原始证据（逐字粘贴）

### A. 关键产物 age（测量时刻 2026-09-25 10:16:49）

```
  birdie_seen_items.json             age=None        ← 错误路径（仓库根不存在），已在 B 段用真实路径重测
  l2 索引 .ga/l2/index.json            age=0.0
  price-alert .price_history.json    age=0.0
  Wiki 60 Wiki/topics                age=1.0
  周巡 ops-weekly-inspection           age=0.4
  sqlite 快照目录                        age=0.1
  ga_api_server.log                  age=11.0
  content.json(周报)                   age=0.7
```

> ⚠️ 上表 `birdie_seen_items.json age=None` 是**我的路径猜错**（不在仓库根）。真实路径重测见 B 段——这正是「产物级审计」必须回读原始输出的原因。

### B. birdie 真实产物与逐份报告分类

```
-rw-r--r--  1 cuip  staff     8202 Aug  7 11:00 birdie_monitor/birdie_seen_items.json
Aug  7 11:00:50 2026 8202 birdie_monitor/birdie_seen_items.json
  age(birdie_seen_items.json)= 49.0

  报告天数= 54 总份数= 58 最早= 2026-08-01 最新= 2026-09-24
分类计数: {'未记exit/其它': 1, 'exit=0': 3, '未记exit/无新增命中': 13, '未记exit/文本含失败特征': 29, 'exit=1': 12}
exit=0 的 3 份: ['2026-08-02_1100_', '2026-08-03_1101_', '2026-08-07_1100_']
exit=1 首末: ['2026-08-17_1101_', '2026-08-19_1101_'] ... ['2026-09-24_1121_', '2026-09-24_1132_']
```

失败样本（`2026-09-24_1132_birdie_daily_monitor.md` 头部逐字）：

```
**脚本 exit=1，止于签到的 SSL 握手失败 → 属「站点侧不可达」（site-level outage），非限额、非本机/节点故障、非脚本缺陷。**
- 失败点：`birdie_daily.py:122 run()` → `:42 sign_res = bc.sign_in(cookie)` → `birdie_client.py:73 requests.post('https://www.birdiesearch.com/api/user/sign')`
```

### C. sqlite_snapshot 空转

```
  快照=108 载荷变化=0 变化文件=[]
May 19 16:35:49 2026 temp/message_2_merged.db
Jun  4 02:19:21 2026 temp/hive_coupon_fix/m7hpgl58zzb4.db
```

### D. ga-api 证据

```
=== ga-api 是否被 launchd 加载 ===
Bad request.
Could not find service "com.cuip.genericagent-ga-api" in domain for user gui: 501
=== ga-api 日志尾部 ===
ERROR:    Error loading ASGI app. Could not import module "temp.ga_api_server".
=== ga-api 日志头部（历史）===
INFO:     Started server process [29472]
INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
=== 计数 ===
58379981 Sep 14 10:06:45 2026 logs/ga_api_server.log
```

### E. launchd 8 项

```
文件数= 8
  com.cuip.genericagent-autonomous.plist         enabled   KeepAlive=True RunAtLoad=True Watch=False log=autonomous_reflect.log log_age=0.0
  com.cuip.genericagent-ga-api.plist             enabled   KeepAlive=True RunAtLoad=True Watch=False log=ga_api_server.log log_age=11.0
  com.cuip.genericagent-l2-gate.plist            enabled   KeepAlive=True RunAtLoad=True Watch=False log=l2_gate_reflect.log log_age=2.0
  com.cuip.genericagent-launch.plist             enabled   KeepAlive=True RunAtLoad=True Watch=False log=dingtalk_agent.log log_age=2.0
  com.cuip.genericagent-pet.plist                enabled   KeepAlive=False RunAtLoad=False Watch=False log=desktop_pet.log log_age=100.8
  com.cuip.genericagent-price-alert.plist        enabled   KeepAlive=True RunAtLoad=True Watch=False log=price_alert_reflect.log log_age=0.4
  com.cuip.genericagent-scheduler.plist          enabled   KeepAlive=True RunAtLoad=True Watch=False log=scheduler_reflect.log log_age=0.0
  com.genericagent.desktop-bridge-repair.plist   enabled   KeepAlive=None RunAtLoad=False Watch=True log=com.genericagent.desktop-bridge-repair.launchd.out.log log_age=56.7

=== launchctl 实际加载状态 ===
-	0	com.cuip.genericagent-pet
608	0	com.cuip.genericagent-price-alert
607	0	com.cuip.genericagent-scheduler
-	0	com.genericagent.desktop-bridge-repair
628	0	com.cuip.genericagent-launch
614	0	com.cuip.genericagent-autonomous
634	0	com.cuip.genericagent-l2-gate
```

> 注：`launchctl list` 的 7 行里**没有 ga-api**——这是 §6.1(2) 的直接证据。

### F. price-alert 健康证明

```
-rw-r--r--  1 cuip  staff  79 Sep 25 10:15 reflect_scripts/.price_history.json
2 coins; 样本
{'bitcoin': [1790302503.16276, 84826], 'ethereum': [1790302503.16276, 2697.04]}
coingecko=200 t=0.697664
google204=204 t=0.716408
```

`reflect_scripts/06_price_alert.py`（83 行）打印点：

```
35:        print(f"[价格历史保存失败] {e}")
80:        print(f"[价格监控失败] {e}")
```

### G. 21 任务清单（enabled + 末次报告）

```
  birdie_daily_monitor               enabled=True  sched=11:00  last_report=2026-09-24_1132_
  brew_path_monitor                  enabled=False sched=08:00  last_report=2026-09-20_1434_
  efficiency_monitor_daily           enabled=True  sched=09:00  last_report=2026-09-25_0900_
  eia_weekly                         enabled=True  sched=08:30  last_report=2026-09-20_1443_
  github_trending                    enabled=True  sched=10:30  last_report=2026-09-24_1031_
  haisou_daily_monitor               enabled=True  sched=11:15  last_report=2026-09-24_1228_
  hellogithub_monthly                enabled=True  sched=10:00  last_report=2026-09-22_1435_
  linuxdo_ai_daily_summary           enabled=True  sched=10:00  last_report=2026-09-24_1000_
  linuxdo_trust_level_check          enabled=True  sched=09:30  last_report=2026-09-25_0931_
  navicat_trial_reset                enabled=False sched=08:00  last_report=2026-09-20_1516_
  project_update_monitor             enabled=True  sched=08:00  last_report=2026-09-25_0801_
  roi_monitor_daily                  enabled=True  sched=11:00  last_report=2026-09-24_1253_
  smyze_release_preflight            enabled=False sched=09:00  last_report=NO_REPORT
  sqlite_snapshot                    enabled=True  sched=08:00  last_report=2026-09-25_0815_
  trendradar_evening                 enabled=True  sched=18:30  last_report=2026-09-24_2055_
  trendradar_hotspot                 enabled=False sched=00:00  last_report=NO_REPORT
  trendradar_morning                 enabled=True  sched=10:00  last_report=2026-09-24_1008_
  wechat_cc_daily_13h                enabled=False sched=22:00  last_report=2026-07-03_1511_
  weekly_inspection                  enabled=True  sched=16:00  last_report=2026-09-24_1611_
  weekly_report_reminder             enabled=True  sched=17:00  last_report=2026-09-24_1701_
  x_ai_intel_daily                   enabled=True  sched=09:00  last_report=2026-09-25_0903_
```

### H. 计数与在飞证据

```
sche_tasks/*.json= 21
disabled/*.json= 3
LaunchAgents匹配= 8
done/*.md= 6014
snapshots= 108
=== 今日 10:00 批复测(第四次) ===
10:17:01
0            ← output/ 今日 0 份
       5     ← done/ 今日 5 份
[MixinSession] Using session (deepseek-flash)   ← 调度器日志尾部：仍在跑
```

### I. 复跑命令（可直接执行，只读）

```bash
cd /Users/cuip/projects/GenericAgent
# 21 任务的 enabled/计划/末次报告
python3 - <<'EOF'
import glob,json,os
for f in sorted(glob.glob('sche_tasks/*.json')):
    j=json.load(open(f)); n=os.path.basename(f)[:-5]
    r=sorted(glob.glob(f'sche_tasks/done/*_{n}.md'))
    print(f"{n:34s} enabled={str(j.get('enabled')):5s} sched={j.get('schedule')} last={os.path.basename(r[-1])[:16] if r else 'NO_REPORT'}")
EOF
# sqlite 快照「只比 dbs」载荷变化
python3 - <<'EOF'
import glob,json
sn=sorted(glob.glob('temp/sqlite_snapshots/*.json')); chg=[]; prev=None
for f in sn:
    d=json.load(open(f))['dbs']
    if prev is not None and d!=prev: chg.append(f)
    prev=d
print('快照=',len(sn),'载荷变化=',len(chg))
EOF
# launchd：plist 与真实加载状态对照
ls ~/Library/LaunchAgents | grep -iE 'genericagent|desktop-bridge'
launchctl list | grep -iE 'genericagent|desktop-bridge'
# ga-api 是否加载
launchctl print gui/$(id -u)/com.cuip.genericagent-ga-api 2>&1 | head -3
```

### J. 交付前「回读核对」抓出的三处订正（逐字原始输出）

```
=== reflect/scheduler.py 存在性与 enabled 判据 ===
-rw-r--r--  1 cuip  staff  8151 Aug  5 01:25 reflect/scheduler.py
114:        if not task.get('enabled', False): continue
=== 上下文 ±3 行 ===
112-            _logger.error(f'JSON parse error for {f}: {e}')
113-            continue
114:        if not task.get('enabled', False): continue
115-
116-        repeat = task.get('repeat', 'daily')
117-        sched = task.get('schedule', '00:00')
=== 两目录核实 ===
drwxr-xr-x@  8 cuip  staff  256 Aug  5 01:25 reflect
drwxr-xr-x@ 19 cuip  staff  608 Sep 23 01:21 reflect_scripts
ls: reflect_scripts/scheduler.py: No such file or directory
reflect/scheduler.py
```

```
=== desktop-bridge-repair 日志大小与监视文件 ===
-rw-r--r--  1 cuip  staff     0 Jul 30 16:24 /Users/cuip/Library/Logs/com.genericagent.desktop-bridge-repair.launchd.err.log
-rw-r--r--  1 cuip  staff     0 Jul 30 16:24 /Users/cuip/Library/Logs/com.genericagent.desktop-bridge-repair.launchd.out.log
-rw-r--r--  1 cuip  staff  1396 Sep 18 01:34 /Users/cuip/Library/Logs/com.genericagent.desktop-bridge-repair.log
-rw-r--r--  1 cuip  staff   895 Jul 30 16:25 /Users/cuip/Library/LaunchAgents/com.genericagent.desktop-bridge-repair.plist
Aug  2 13:17:18 2026 404 /Users/cuip/.ga_desktop_settings.json
  <key>WatchPaths</key>
  <array>
    <string>/Users/cuip/.ga_desktop_settings.json</string>
  </array>

--- main .log(1396B Sep18)
2026-08-22T06:34:02 SKIP override already correct
2026-08-25T04:14:16 SKIP override already correct
2026-08-29T13:53:43 SKIP override already correct
2026-09-02T02:05:37 SKIP override already correct
2026-09-11T07:20:21 SKIP override already correct
2026-09-18T01:34:54 SKIP override already correct
--- out
(空)
--- err
(空)
```

```
=== 8765 端口连通性（python 替代 lsof）===
port 8765: ConnectionRefusedError [Errno 61] Connection refused
port 18765: CONNECTED
=== lsof 可用性 ===
/bin/sh: lsof: command not found

=== 8765 grep 命中的真消费者复核 ===
./temp/loopx_research/loopx_repo/benchmark/widesearch/tests/test_run_config.py:26:        gateway_base_url="http://127.0.0.1:8765",
./temp/loopx_research/loopx_repo/loopx/demo.py:168:        "dashboard_status_url": "http://127.0.0.1:8765/status.json",
./temp/loopx_research/loopx_repo/examples/demo-cli-smoke.py:64:        assert payload["dashboard_status_url"] == "http://127.0.0.1:8765/status.json", payload
（以上命中全部落在 LoopX 上游仓库快照 temp/loopx_research/**，非 GA 自身代码）
```

```
=== ga-api plist 是否含 Disabled 键 ===
0
无 Disabled 键
```

```
=== scheduler plist 指向（调度器是 agentmain --reflect，任务目录 sche_tasks/）===
<string>/opt/anaconda3/bin/python3</string>
<string>-u</string>
<string>/Users/cuip/projects/GenericAgent/agentmain.py</string>
<string>--reflect</string>
<string>reflect/scheduler.py</string>
```

> 说明：以上 J 段为**修正后**的证据；正文 §2/§4.5/§5.3/§6.1/§7 已按此同步订正。除本段列出的三处外，其余数字（§9·A–I）均为首轮实测原始输出，未变动。
