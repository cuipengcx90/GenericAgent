# R469 sche_tasks prompt 静态体检器（cwd/路径/flag/超时/通道五查）

- 日期：2026-09-28
- 任务来源：TODO L180（第20批·三路评审 codex/槽3/槽4 = 4/7/7 = 6.00，收窄=只做可回源验证的脚本/参数/路径/超时/通道，**不重立 R375 修法**）
- 交付物：`tools/sche_tasks_lint.py`（729 行 / 33274B / sha256 前16 = `ff779ce6d0f69b85`，纯 stdlib 只读）+ 本报告
- 结论：22 个在册任务全量扫描 ⇒ FAIL 3 / WARN 10 / OK 4 / SKIP 5；R375 两例（`../temp`、`./temp`）**双双复现**；自测 `selftest 23/23`（21 负控/正控 + 只读自证 ×2 语义断言）

---

## 1. 权威口径：prompt 在哪个 cwd 执行

| 证据 | 内容 |
|---|---|
| `~/Library/LaunchAgents/com.cuip.genericagent-scheduler.plist:30-31` | `<key>WorkingDirectory</key> <string>/Users/cuip/projects/GenericAgent</string>` |
| `reflect/scheduler.py:105-106` | `for f in sorted(os.listdir(TASKS)):` / `if not f.endswith('.json'): continue` ⇒ 只读**顶层** `sche_tasks/*.json`，不递归 `disabled/` |
| 目录实测 | `/Users/cuip/projects/temp` = False、`/Users/cuip/projects/memory` = False、`/Users/cuip/projects/GenericAgent/temp` = True、`/Users/cuip/projects/GenericAgent/temp/temp` = True |

⇒ 权威 cwd（repo 根）下，prompt 里的 `../temp/...` / `../memory/...` 必然落到 `~/projects/...`（不存在）；`./temp/...` 则随 cwd 漂移（repo 根→`repo/temp`，repo/temp→`temp/temp`）。

三 cwd 沙盒（工具 `--sandbox` 原始输出，两例均命中）：

```
~ sandbox ./temp/efficiency_report_latest.json => repo_root=/Users/cuip/projects/GenericAgent/temp/efficiency_report_latest.json | repo_temp=/Users/cuip/projects/GenericAgent/temp/temp/efficiency_report_latest.json | home=/Users/cuip/temp/efficiency_report_latest.json
~ sandbox ../temp/get_week_range.py => repo_root=/Users/cuip/projects/temp/get_week_range.py | repo_temp=/Users/cuip/projects/GenericAgent/temp/get_week_range.py
~ sandbox ../memory/weekly_report_sop.md => repo_root=/Users/cuip/projects/memory/weekly_report_sop.md | repo_temp=/Users/cuip/projects/GenericAgent/memory/weekly_report_sop.md
```

## 2. 五查判据与分级规则

| 检查 | 判据 | 分级 |
|---|---|---|
| CWD（cwd 漂移） | 相对路径（`./` / `../` / 裸名）在 repo 根 / repo/temp / home 三种 cwd 下解析出**不同**目标 ⇒ 结果随 cwd 漂移 | WARN |
| PATH（路径存在性） | 解析后回源 `os.path.exists`；`../` 锚定但不存在 ⇒ FAIL；裸名（无 `./ ../`）⇒ WARN（可能指向他仓/他 cwd，静态不可判） | FAIL / WARN |
| PATH（模板/截断） | 绝对路径 token 紧跟 `$(...)`/`${...}`/中文占位 ⇒ `PATH_TEMPLATE`；被空格截断但兄弟项存在 ⇒ `PATH_PREFIX`（绝对路径被截断的合理形态） | INFO |
| FLAG（参数可核验） | 脚本用 argparse ⇒ 逐个 flag 比对 `add_argument`；脚本不可读/无 argparse ⇒ `FLAG_UNCHECKED`（INFO，**不判未知**，避免假 FAIL）；flag 无归属脚本 ⇒ `FLAG_UNATTRIBUTED`（INFO，可能属外部 CLI 如 lark-cli） | INFO |
| TIMEOUT（超时声明） | prompt 提到长任务/网络采集但未声明超时/时长 ⇒ `TIMEOUT_UNDECLARED` | WARN |
| CHANNEL（通知通道） | prompt 未提任何通道 ⇒ `CHANNEL_UNSPECIFIED`；提了通道但脚本内无通道标记 ⇒ `CHANNEL_AGENT_SIDE`（由 agent 侧通知，静态不可核验） | WARN / INFO |
| 时间窗 | 未声明 `max_delay_hours` ⇒ `MAX_DELAY_DEFAULT`（scheduler 默认 6h） | INFO |

分级原则：**静态可证的缺陷才判 FAIL**；凡「静态不可判但需人看」的（裸名、不可读脚本、外部 CLI flag、agent 侧通道）一律降级为 WARN/INFO，宁可漏报不造假 FAIL。

## 3. 全量扫描结果（22 个在册任务）

复跑命令：`python3 tools/sche_tasks_lint.py --json temp/sche_tasks_lint/scan.json`（rc=1）

```
summary: {"tasks": 22, "FAIL": 3, "WARN": 10, "OK": 4, "SKIP": 5, "readonly_files": 34, "readonly_changed": []}
```

| 任务 | verdict | codes |
|---|---|---|
| birdie_daily_monitor | OK | - |
| brew_path_monitor | SKIP | SKIPPED_DISABLED |
| dpdomain_renew | WARN | CHANNEL_UNSPECIFIED, CWD_DRIFT, PATH_TEMPLATE |
| efficiency_monitor_daily | WARN | CWD_DRIFT |
| eia_weekly | OK | - |
| github_trending | WARN | CHANNEL_UNSPECIFIED, FLAG_UNATTRIBUTED |
| haisou_daily_monitor | WARN | CHANNEL_UNSPECIFIED |
| hellogithub_monthly | WARN | CHANNEL_UNSPECIFIED |
| linuxdo_ai_daily_summary | **FAIL** | CHANNEL_AGENT_SIDE, CWD_DRIFT, PATH_MISSING, PATH_PREFIX |
| linuxdo_trust_level_check | WARN | CHANNEL_UNSPECIFIED, CWD_DRIFT |
| navicat_trial_reset | SKIP | SKIPPED_DISABLED |
| project_update_monitor | OK | - |
| roi_monitor_daily | OK | - |
| smyze_release_preflight | SKIP | SKIPPED_DISABLED |
| sqlite_snapshot | WARN | CHANNEL_UNSPECIFIED |
| trendradar_evening | WARN | CHANNEL_UNSPECIFIED, CWD_DRIFT, TIMEOUT_UNDECLARED |
| trendradar_hotspot | SKIP | SKIPPED_DISABLED |
| trendradar_morning | WARN | CHANNEL_UNSPECIFIED, CWD_DRIFT, TIMEOUT_UNDECLARED |
| wechat_cc_daily_13h | SKIP | SKIPPED_DISABLED |
| weekly_inspection | **FAIL** | CHANNEL_AGENT_SIDE, CWD_DRIFT, PATH_MISSING, PATH_TEMPLATE, REL_BARE_UNRESOLVED |
| weekly_report_reminder | **FAIL** | CWD_DRIFT, FLAG_UNATTRIBUTED, FLAG_UNCHECKED, MAX_DELAY_DEFAULT, PATH_MISSING |
| x_ai_intel_daily | WARN | CWD_DRIFT |

顶层 22 个 JSON 中 enabled=17 / enabled:false=5（brew_path_monitor、navicat_trial_reset、smyze_release_preflight、trendradar_hotspot、wechat_cc_daily_13h），后者判 SKIP；另有 `sche_tasks/disabled/` 3 个文件（bridge_autonomous、folder_watcher、price_alert）**不被 scheduler 读取**（见 §1），故不在体检面。

## 4. 三条 FAIL 的逐条明细（原始输出摘录）

### 4.1 `weekly_report_reminder` —— R375 原案，两处 `../` 硬失败

```
- WARN   CWD_DRIFT        相对路径 ../temp/get_week_range.py 无锚定：repo 根→/Users/cuip/projects/temp/get_week_range.py / repo/temp→/Users/cuip/projects/GenericAgent/temp/get_week_range.py
- WARN   CWD_DRIFT        相对路径 ../memory/weekly_report_sop.md 无锚定：repo 根→/Users/cuip/projects/memory/weekly_report_sop.md / repo/temp→/Users/cuip/projects/GenericAgent/memory/weekly_report_sop.md
- FAIL   PATH_MISSING     相对路径 ../temp/get_week_range.py 在权威 cwd(/Users/cuip/projects/GenericAgent) 下不存在：/Users/cuip/projects/temp/get_week_range.py
- FAIL   PATH_MISSING     相对路径 ../memory/weekly_report_sop.md 在权威 cwd(/Users/cuip/projects/GenericAgent) 下不存在：/Users/cuip/projects/memory/weekly_report_sop.md
- INFO   FLAG_UNCHECKED   脚本不可读：/Users/cuip/projects/temp/get_week_range.py
- INFO   FLAG_UNCHECKED   temp/weekly_report_gen.py 未使用 argparse，-o 无法静态核验
- INFO   FLAG_UNCHECKED   temp/weekly_report_verify.py 未使用 argparse，--draft-id/--exec-date 无法静态核验
- INFO   FLAG_UNATTRIBUTED 未归属到仓库脚本的 flag（可能属外部 CLI，如 lark-cli）：--set-subject
- INFO   MAX_DELAY_DEFAULT 未声明 max_delay_hours ⇒ scheduler 用默认 6h 窗口
```

与 R375 沙盒实测一致：`../temp` 在权威 cwd ⇒ 必失败 rc=2。**本项只复现与静态标定，不改 prompt**（R375 的「prompt 头插 GA_ROOT 锚定行」修法仍待批，见 `DECISIONS_*`）。

### 4.2 `efficiency_monitor_daily` —— R375 另一形态（`./temp` 落点漂移）

```
- WARN   CWD_DRIFT   相对路径 ./temp/efficiency_report_latest.json 无锚定：repo 根→…/GenericAgent/temp/efficiency_report_latest.json / repo/temp→…/GenericAgent/temp/temp/efficiency_report_latest.json
- WARN   CWD_DRIFT   相对路径 ./logs/autonomous_reflect.log 无锚定：repo 根→…/GenericAgent/logs/… / repo/temp→…/GenericAgent/temp/logs/…
```

`./temp/...` 在 repo 根**是存在的**（故不判 PATH_MISSING），但一旦以 repo/temp 为 cwd 就落进 `temp/temp` ⇒ 判 CWD_DRIFT（WARN），与 R375「落点漂移」口径一致。

### 4.3 `linuxdo_ai_daily_summary` / `weekly_inspection` —— 新发现的 `../memory/*.py` 硬失败

```
[linuxdo_ai_daily_summary] FAIL PATH_MISSING 相对路径 ../memory/linuxdo_deep_read_sop.md 在权威 cwd 下不存在：/Users/cuip/projects/memory/linuxdo_deep_read_sop.md
[weekly_inspection]        FAIL PATH_MISSING 相对路径 ../memory/gen_report_skeleton.py 在权威 cwd 下不存在：/Users/cuip/projects/memory/gen_report_skeleton.py
[weekly_inspection]        FAIL PATH_MISSING 相对路径 ../memory/verify_then_notify.py 在权威 cwd 下不存在：/Users/cuip/projects/memory/verify_then_notify.py
[weekly_inspection] WARN REL_BARE_UNRESOLVED 裸相对名 docs/analysis 在仓库根不存在；未加 ./ ../ 锚定，可能指向他仓/他 cwd（实测 docs/analysis 属 bar-smyze 仓），静态不可判
```

⇒ **三例 FAIL 全部是 `../` 显式锚定的硬失败，零假 FAIL**；两条（linuxdo / weekly_inspection）是 R375 未被记录的**同族缺陷**（`../memory/` 而非 `../temp/`）。

## 5. 可证伪（负控）

`--selftest` 原始输出：`selftest 23/23`（rc=0）。夹具在 `temp/sche_tasks_lint_selftest/` 现造，关键断言：

| 编号 | 注入 | 期望 |
|---|---|---|
| NC1 | `./temp/definitely_missing.py` | 必报 PATH_MISSING |
| NC2 | 存在脚本 + 未知 flag `--gamma` | 必报 FLAG_UNKNOWN |
| NC3 | 绝对路径 + 已知 flag（正控） | 不得出现 PATH_MISSING/FLAG_UNKNOWN |
| NC14/15 | 绝对路径紧跟 `$(date …)` / 中文占位 | 只记 PATH_TEMPLATE，**不得**判 PATH_MISSING |
| NC16 | 绝对路径被空格截断但兄弟项存在 | 只记 PATH_PREFIX，**不得**判 PATH_MISSING |
| NC17/20 | 裸相对名缺失 | 只 WARN（REL_BARE_UNRESOLVED），**不得**判 PATH_MISSING |
| NC18 | `../temp/...` 缺失 | 必报 PATH_MISSING（R375 真形态） |
| NC19 | `./temp/...` 存在但漂移 | 必报 CWD_DRIFT，不得判 PATH_MISSING |
| NC21 | `--only` 逗号多值 `{NC1,NC3}` | 恰好命中两个 |
| NC22 | `--only` 未知 id | 得 0 条（由 CLI 转 rc=2） |
| 只读自证 | 全程 34 个被读文件 sha256 前后比对 | `changed=[]`，且断言「变化即失败」 |

## 6. 只读与边界

- 只读自证：真仓扫描 `只读自证：files=34 changed=[]`；工具不写任何被巡检文件（`--json` 只写调用者指定路径）。
- 已知不可判（显式降级，非缺陷）：裸相对名（他仓同名目录）、脚本不可读、外部 CLI flag、agent 侧通道、`max_delay_hours` 缺省（有 scheduler 默认值）。
- `--only` 逗号多值原先**静默返回 0 条**（本会话实测踩到）⇒ 已修：多值集合过滤 + 未知 id 打 stderr 并 `rc=2`（同一类「静默无效参数」正是本工具要抓的缺陷，被自己踩到一次）。

## 7. 待批提案

见 `temp/pending_decisions/DECISIONS_R469_sche_tasks_prompt.md`：

- **D1**：把本器接入只读巡检（反射器或随 `reflect_scripts/09_task_freshness.py` 一起跑，仅新增项通知），`revers=0`。
- **D2**：三例 `../` 硬失败的修法**并轨 R375 既有待批修法**（prompt 头插 `GA_ROOT` 锚定行），本项不另立、不预改。

## 8. 复跑命令

```bash
cd /Users/cuip/projects/GenericAgent
python3 tools/sche_tasks_lint.py --selftest
python3 tools/sche_tasks_lint.py --json temp/sche_tasks_lint/scan.json
python3 tools/sche_tasks_lint.py --only weekly_report_reminder,efficiency_monitor_daily --sandbox
```
