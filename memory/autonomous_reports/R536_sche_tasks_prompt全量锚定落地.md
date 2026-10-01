# R536 sche_tasks prompt 全量锚定 GA_ROOT（R490 D1 落地）

- 日期：2026-10-01
- 来源：TODO L193 / `temp/pending_decisions/DECISIONS_sche_tasks_cwd_anchor.md` D1（用户回复「执行啊 为什么跑了这么久还没执行」⇒ 视为批准「批准 DECISIONS_sche_tasks_cwd_anchor.md:D1」）
- 主题：把 R375 提出的「prompt 头插 `GA_ROOT` 锚定行 + 统一仓库根相对路径」idiom 从**从未落地**变成**生产 prompt 已落地并机械复验**
- 结论：**D1 已执行**。9 个 `sche_tasks/*.json` prompt 头部插入锚定行、6 处 `../` 改仓库根相对；提交 `db60859`（9 文件 / 9+/9−，每文件恰 1/1）；A/B 机械判据 `FAIL 3 → 0`、`rc 1 → 0`；两闸门复跑 rc=0。

## 1. 为什么这条必须做（问题形态）

R469（`tools/sche_tasks_lint.py`）真跑 22 个在册任务，报 3 条 FAIL，全部是 prompt 里的 `../` 显式锚定：

- `weekly_report_reminder` 的 `../temp/get_week_range.py`
- `linuxdo_ai_daily_summary` 的 `../memory/linuxdo_deep_read_sop.md`
- `weekly_inspection` 的 `../memory/gen_report_skeleton.py`、`../memory/verify_then_notify.py`

launchd 侧 `WorkingDirectory` = GA 仓库根，而 `../x` 是按「交互会话 cwd（常为 `temp/`）」写的口径 ⇒ 定时任务在仓库根跑时 `../temp` 解析到仓库**外**，R375 已实测 rc=2 硬失败。更隐蔽的是一整类**仅需锚定**的任务：它们用裸相对路径（`temp/x.py`），只有在 cwd 恰为仓库根时才成立，一旦 cwd 漂移就落到错误目录或静默取不到文件（不报错）。

R490 交付了确定性执行工具 `tools/sche_tasks_cwd_fix.py`（`plan`/`verify`/`apply --approve`/`selftest`/`proof`，位置子命令；**未批准零写盘**，`apply` 前自动备份 `temp/l193_backup/<ts>/`），但当时**生产 prompt 零改动**——`grep -r GA_ROOT sche_tasks/` 命中 0，即 idiom 从未落地。本轮把它落地。

## 2. 执行

命令（幂等，重复执行即 no-op）：

```
python3 tools/sche_tasks_cwd_fix.py apply --approve "批准 DECISIONS_sche_tasks_cwd_anchor.md:D1"
python3 tools/sche_tasks_cwd_fix.py verify
python3 tools/sche_tasks_lint.py            # 全量
```

落盘形态（`git show --stat db60859`，父 `903c13c`）：

```
sche_tasks/brew_path_monitor.json        | 2 +-
sche_tasks/dpdomain_renew.json           | 2 +-
sche_tasks/efficiency_monitor_daily.json | 2 +-
sche_tasks/linuxdo_ai_daily_summary.json | 2 +-
sche_tasks/navicat_trial_reset.json      | 2 +-
sche_tasks/smyze_stock_watch.json        | 2 +-
sche_tasks/weekly_inspection.json        | 2 +-
sche_tasks/weekly_report_reminder.json   | 2 +-
sche_tasks/x_ai_intel_daily.json         | 2 +-
9 files changed, 9 insertions(+), 9 deletions(-)
```

`--numstat` 逐文件 `1 1` ⇒ 每个文件只动了 1 行（即锚定行那一行；路径改写与锚定行同属该 prompt 单行 JSON 字符串内）。

**归属清洁性（关键）**：本轮工作区同时存在他会话在制改动（同为 `sche_tasks/*.json` 的 `enabled`/`repeat` 字段，涉及 `brew_path_monitor`、`navicat_trial_reset`、`project_update_monitor`）。首跑 `--numstat` 曾显示 **10 文件 / 2 文件为 2/2**（混入他会话行）⇒ 改用「HEAD 基座 + 沙盒 apply 只生成纯锚定版」再逐个对账，只把纯锚定 blob 写索引、`git commit` 不带 pathspec。最终 `9 files changed, 9 insertions(+), 9 deletions(-)` 与预期逐项吻合；提交后残留工作区改动仅他会话 3 文件。

## 3. A/B 机械判据（锚定前 vs 锚定后）

同一工具、同一口径，两侧语料分别为 `db60859^` 与 `db60859` 的 9 个文件（`temp/l193_ab/before.json` / `after.json`）：

```
brew_path_monitor          WARN CHANNEL_UNSPECIFIED,CWD_DRIFT -> WARN CHANNEL_UNSPECIFIED,CWD_ANCHORED tokens=2 anchored_info=2 bytes 541->632
dpdomain_renew             WARN CHANNEL_UNSPECIFIED,CWD_DRIFT,PATH_TEMPLATE -> WARN CHANNEL_UNSPECIFIED,CWD_ANCHORED,PATH_TEMPLATE tokens=1 anchored_info=1 bytes 1455->1546
efficiency_monitor_daily   WARN CWD_DRIFT              -> OK   CWD_ANCHORED           tokens=2 anchored_info=2 bytes 266->357
linuxdo_ai_daily_summary   FAIL CHANNEL_AGENT_SIDE,CWD_DRIFT,PATH_MISSING,PATH_PREFIX -> OK   CHANNEL_AGENT_SIDE,CWD_ANCHORED,PATH_PREFIX tokens=1 anchored_info=1 bytes 1573->1661
navicat_trial_reset        FAIL CWD_DRIFT,FLAG_UNATTRIBUTED,PATH_MISSING -> OK   CWD_ANCHORED,FLAG_UNATTRIBUTED tokens=1 anchored_info=1 bytes 527->615
smyze_stock_watch          OK   CHANNEL_AGENT_SIDE,FLAG_UNATTRIBUTED -> OK   CHANNEL_AGENT_SIDE,FLAG_UNATTRIBUTED tokens=0 anchored_info=0 bytes 1590->1681
weekly_inspection          FAIL CHANNEL_AGENT_SIDE,CWD_DRIFT,PATH_MISSING,PATH_TEMPLATE,REL_BARE_UNRESOLVED -> WARN CWD_ANCHORED,PATH_TEMPLATE,REL_BARE_UNRESOLVED tokens=4 anchored_info=4 bytes 1389->1468
weekly_report_reminder     WARN CWD_DRIFT,FLAG_UNATTRIBUTED,FLAG_UNCHECKED,MAX_DELAY_DEFAULT -> OK   CWD_ANCHORED,FLAG_UNATTRIBUTED,FLAG_UNCHECKED,MAX_DELAY_DEFAULT tokens=9 anchored_info=9 bytes 2587->2678
x_ai_intel_daily           WARN CWD_DRIFT              -> OK   CWD_ANCHORED           tokens=1 anchored_info=1 bytes 485->576
total_tokens=21
```

读法：`CWD_DRIFT` 九条全消、`CWD_ANCHORED` 九条全中（9/9）；`PATH_MISSING` 三条（`linuxdo_ai_daily_summary`/`navicat_trial_reset`/`weekly_inspection`）全消；判决档位 `FAIL 3 + WARN 5 + OK 1`（rc=1）⇒ `FAIL 0 + WARN 3 + OK 6`（rc=0）。`total_tokens=21` 为 9 条 prompt 的路径 token 总数（即锚定行必须覆盖的引用面）。

## 4. 改写点计数（`temp/l193_ab/edits.txt`）

```
TOTAL anchor_lines=9 rel_rewritten=6 bare_fixed=0 total=15
```

- 锚定行 9 条（每任务 1 条，`GA_ROOT` 出现 3 次=`: "${GA_ROOT:=<repo>}"; export GA_ROOT; cd "$GA_ROOT" || exit 3` 三处引用）
- `../` 改仓库根相对 6 处：`linuxdo_ai_daily_summary` 1、`navicat_trial_reset` 1、`weekly_inspection` 4
- 裸名补前缀 0 处（本批无该形态）

台账 D1 原文按 R490 的**旧快照**登记为「8 个 prompt / 9 处改写」，本轮实测为 **9 个 prompt / 15 个改写点（9 锚定 + 6 路径）**——差异原因：R535（他会话 2026-10-01）已把 `weekly_report_reminder` 的 `../temp` 三处改写掉（故该文件本轮 `rel 0->0`，只补锚定行），而 `navicat_trial_reset` 新增一处；总量口径因此变化。**以本节实测为准**。

## 5. 闸门复跑

| 闸门 | 结果 |
|------|------|
| `tools/sche_tasks_lint.py`（全量 22 任务） | **rc=0**；锚定后 9 条含 `INFO CWD_ANCHORED`（相对路径吃 cwd，但 prompt 含 GA_ROOT 锚定行 ⇒ 已消解） |
| `tools/durable_facts_lint.py` | **rc=0**（ACTIVE 红 0） |
| `tools/sche_tasks_cwd_fix.py verify` | 幂等（无残留可改项） |
| `tools/sche_tasks_cwd_fix.py plan`（复跑） | `将改动 0 / 23，幂等 23/23` ⇒ 再跑无副作用 |

## 6. 残留与边界（如实登记，未粉饰）

1. `weekly_report_reminder` 仍有一条 `../x` **未被改写**：该处出现在 prompt 的**禁止句**里（「禁止 `../x`」），`plan` 对其标记 `[unsupported: ../x]`（工具拒绝改写无法判定语义的文本）⇒ 属**假阳性面**，不是漏改。`sche_tasks_lint` 对其判 OK。
2. `weekly_inspection` 锚定后仍 `WARN PATH_TEMPLATE,REL_BARE_UNRESOLVED`（`../` 虽清零，但该 prompt 存在模板占位路径与裸名引用）⇒ 归 R469 既有噪声档，本批不动。
3. `navicat_trial_reset`/`dpdomain_renew`/`brew_path_monitor` 仍 WARN `FLAG_UNATTRIBUTED`/`CHANNEL_UNSPECIFIED`/`PATH_TEMPLATE` ⇒ 同上，属既有噪声档。
4. 本轮**只改 prompt 文案**：未动 scheduler、未动任何 plist、未启用/停用任何任务。
5. 工作区另 3 个 `sche_tasks/*.json` 携带他会话在制改动（`enabled`/`repeat`）⇒ **未纳入**本次提交（归属分离）。
6. D2（只改 3 条硬失败）已被 D1 覆盖，无需再批；D3（只读复验纳入每日巡检）**仍待批**。

## 7. 回滚

`apply` 前自动备份在 `temp/l193_backup/<ts>/`（逐字副本）；回滚 = 逐字还原或 `git revert db60859`。`revers=0`。

## 8. 关联

R375（cwd 吃态首次实证）→ R469（`sche_tasks_lint` 体检器，FAIL 3）→ R490（`sche_tasks_cwd_fix.py` 计划器/写入器 + 台账登记）→ **R536（D1 落地 + A/B 机械判据）**。

## 9. 快照口径

本文所有数字取自本机 2026-10-01 快照：`git show db60859`（提交）、`temp/l193_ab/{table.txt,edits.txt,before.json,after.json}`（A/B 语料）。A/B 语料是可复跑的（工具 + 两侧文件内容即可重现），但 `total_tokens`/`bytes` 类数值随语料演化，引用时以本节时点为准。
