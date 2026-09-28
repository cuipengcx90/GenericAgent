# R490 sche_tasks `../` 硬失败锚定修复 + 补丁执行工具（TODO L193 / 第22批 C2）

- 日期：2026-09-28
- 任务来源：temp/TODO.txt L193（第22批 C2；评审收窄条件=「并入 R375」，不另立修法）
- 前置结论来源：R469（tools/sche_tasks_lint.py 真跑 FAIL 3，全为 `../` 显式锚定硬失败）、R375（cwd 吃态 + GA_ROOT 锚定行修法，待批 C65）
- 本报告口径：所有数值均为 2026-09-28 当次命令原始输出快照；工具名/大小/sha 会随源码演进变化，引用时请现场复算

## 0. 结论（先给答案）

1. **三条硬失败（实为 6 处 PATH_MISSING）的唯一成因已复现并在沙盒验证消除**：prompt 里以 `../memory/…`、`../temp/…` 写相对路径，而权威 cwd 是 repo 根（`/Users/cuip/projects/GenericAgent`）⇒ 解析成 `/Users/cuip/projects/memory/…`（父目录）必不存在。
2. **修法沿用 R375 的单一 idiom（不新造）**：prompt 头部插一行锚定，并把 `../{memory,temp,tools}/X` 改写为 `{memory,temp,tools}/X`（锚定行已 `cd "$GA_ROOT"`）。
3. **生产 prompt 仍零改动**：生产侧只跑了只读 `plan` / `verify`，未执行 `apply`；改动动作与批准语已登记待批台账。
4. **顺带修掉一个 lint 假阳性**：旧版把同一行散文句里的 `--since/--until` 归到前面的 `temp/get_week_range.py` 上，报「该脚本 argparse 无此 flag」；已改为 flag 归属**子句级**判定，并补 2 条负控（selftest 23→25）。
5. **交付物**：新工具 `tools/sche_tasks_cwd_fix.py`（plan/verify/apply/selftest/proof，未批准零写盘）+ 修订 `tools/sche_tasks_lint.py` + 台账 `temp/pending_decisions/DECISIONS_sche_tasks_cwd_anchor.md`（D1/D2/D3）+ 本报告 + 证据目录 `temp/l193_notes/`。

## 1. 问题与背景

R469 真跑（22 个在册 `sche_tasks/*.json`）结论为 FAIL 3 / WARN 10 / OK 4 / SKIP 5，三条 FAIL 逐字为：

- `weekly_report_reminder`：`../temp/get_week_range.py` 在权威 cwd 下不存在
- `linuxdo_ai_daily_summary`：`../memory/linuxdo_deep_read_sop.md` 在权威 cwd 下不存在
- `weekly_inspection`：`../memory/gen_report_skeleton.py`、`../memory/verify_then_notify.py` 在权威 cwd 下不存在

R375 已从另一侧（效率监控落点漂移 `temp/temp/`）指出同一根因：**定时任务 prompt 的相对路径吃 cwd**。当时给出的修法是「prompt 头插 `GA_ROOT` 锚定行」，但那条提案一直停留在待批状态（C65），生产 prompt 里从未落地。

本批（第22批 C2）评审给的收窄条件即：**不要重新发明修法，直接把 R375 的锚定行落到这三条上**，并以只读方式给出全量待改清单。

现场核对（本会话）：

- 生产 `sche_tasks/*.json` 中 **grep `GA_ROOT` 命中 0** ⇒ 锚定 idiom 在 22 条 prompt 里一条都没有；lint 的 `CWD_ANCHORED` 分支从未被生产数据触发（即该分支此前只有 selftest 覆盖）。
- 权威 cwd 由 lint 定义为 repo 根，`../memory/…` 必然解析到 `/Users/cuip/projects/memory/…`。

## 2. 补丁形态（逐字）

锚定行（`ANCHOR_TMPL`，只在 prompt 头部插入一次；用户目录不同则宏默认值随之，可由环境 `GA_ROOT` 覆盖）：

```
: "${GA_ROOT:=/Users/cuip/projects/GenericAgent}"; export GA_ROOT; cd "$GA_ROOT" || exit 3
```

路径改写（仅限仓内三目录，且都要求锚定行先落地）：

| 原写法 | 改写后 | 说明 |
|---|---|---|
| `../memory/X` | `memory/X` | 锚定行已 cd 到 repo 根 |
| `../temp/X` | `temp/X` | 同上 |
| `../tools/X` | `tools/X` | 同上 |

两条判据（避免误伤）：

- `NEED_RE` 只匹配 `memory|temp|tools` 之前的 `../`；`../otherrepo/foo` 这类**不触发**锚定（少插一行比多插一行安全）。
- `REWRITE_RE` 带负向后视 `(?<![A-Za-z0-9_./-])` 与右边界 `(?![A-Za-z0-9_])`，避免把 `foo../temp` 或 `tempdir` 误改。

写回保真：对每个 JSON 文件只做字符串级替换，`plan` 阶段逐条校验「改后 JSON 仍可解析且除目标 prompt 字段外逐字节不变」，记 `fidelity`；不满足即标 `FORMAT_MISMATCH` 并拒改（`apply` 跳过）。

## 3. 交付物清单（本会话实测快照）

| 文件 | 大小 | 行数 | sha256 前16 |
|---|---|---|---|
| `tools/sche_tasks_lint.py`（修订） | 35111 B | 761 | `ba9d0a00363b71c3` |
| `tools/sche_tasks_cwd_fix.py`（新建） | 20630 B | 469 | `c8af0727d277b05c` |
| `temp/pending_decisions/DECISIONS_sche_tasks_cwd_anchor.md`（台账） | 4721 B | 39 | — |
| `temp/l193_notes/verify_production.txt` | 1536 B | — | — |
| `temp/l193_notes/plan_production.json` | 7544 B | — | — |
| `temp/l193_notes/lint_production.json` | 22855 B | — | — |

子命令（位置参数，非 `--flag`；本机默认 `python3` 即可，无第三方依赖）：

```
python3 tools/sche_tasks_cwd_fix.py plan [--json OUT]            # 只读：列出将改动项
python3 tools/sche_tasks_cwd_fix.py verify                       # 只读：沙盒改写 → 复跑 lint，报 before/after
python3 tools/sche_tasks_cwd_fix.py apply --approve "<批准语>"   # 唯一写盘；备份到 temp/l193_backup/<ts>/
python3 tools/sche_tasks_cwd_fix.py selftest                     # 沙盒夹具，不碰生产
python3 tools/sche_tasks_cwd_fix.py proof                        # 只读自证
```

## 4. 实测数据

### 4.1 工具 selftest

- `python3 tools/sche_tasks_lint.py --selftest` ⇒ `selftest 25/25`，rc=0（原 23 条 + 本批新增 NC23/NC24）。
- `python3 tools/sche_tasks_cwd_fix.py selftest` ⇒ `selftest: ALL PASSED`，rc=0（25 条断言行，含 S1–S10 正例、幂等用例 `S9_second_apply_noop`、`S10_lint_before_has_fail`/`S10_lint_after_zero` 与 NC1/NC2 负控）。

### 4.2 生产只读 plan（22 任务）

```
汇总: 将改动 8 / 22，幂等 22/22，格式可无损改写 22/22
```

8 条待改（逐条实测，`anchor_added` 全 True、`fidelity` 全 True）：

| task_id | 锚定行 | 路径改写 |
|---|---|---|
| `brew_path_monitor` | + | — |
| `dpdomain_renew` | + | — |
| `efficiency_monitor_daily` | + | — |
| `linuxdo_ai_daily_summary` | + | `../memory`→`memory` ×1 |
| `navicat_trial_reset` | + | `../memory`→`memory` ×1 |
| `weekly_inspection` | + | `../memory`→`memory` ×4 |
| `weekly_report_reminder` | + | `../temp`→`temp` ×2、`../memory`→`memory` ×1 |
| `x_ai_intel_daily` | + | — |

注：8 条里 5 条只需插锚定行（它们的相对路径本来就是 `temp/x_ai_intel` 这类**漂移型**而非**必失败型**——在 cwd=repo 根时正常、在 cwd=repo/temp 时漂进 `temp/temp`）；这与 lint 的 WARN `CWD_DRIFT` 口径一致。

### 4.3 生产只读 verify（沙盒改写 → 复跑 lint）

```
lint before: rc=1 FAIL=6
  FAIL linuxdo_ai_daily_summary | PATH_MISSING | ../memory/linuxdo_deep_read_sop.md
  FAIL weekly_inspection         | PATH_MISSING | ../memory/weekly_inspection_sop.md
  FAIL weekly_inspection         | PATH_MISSING | ../memory/gen_report_skeleton.py
  FAIL weekly_inspection         | PATH_MISSING | ../memory/verify_then_notify.py
  FAIL weekly_report_reminder    | PATH_MISSING | ../temp/get_week_range.py
  FAIL weekly_report_reminder    | PATH_MISSING | （第二处）
（改写后）
lint after:  rc=0 FAIL=0
```

沙盒目录：`temp/l193_sandbox_run/sche_tasks`（全程未触碰 `sche_tasks/` 真身；`verify` rc=0）。

### 4.4 lint 判据面上 A/B（生产）

22 任务判据分布**零变化**：`FAIL 3 / WARN 10 / OK 4 / SKIP 5`；唯一差异是 `project_update_monitor` 多出 1 条 `FLAG_UNATTRIBUTED` **INFO**（`tools/sche_tasks_lint.py` 当前真跑 rc=1、verdict FAIL，与 R469 逐条一致）。

### 4.5 lint 假阳性修复（子句级 flag 归属）

旧版症状（R469 报告里已出现过该条 WARN 的误读）：

```
FAIL weekly_report_reminder | 相对路径 temp/get_week_range.py 的 argparse 无此 flag：--since / --until（已定义：--date）
```

根因：prompt 里 `temp/get_week_range.py` 与后续散文句（描述 `git log --since/--until`）**在同一行**，旧判定按「整行找最后一个脚本路径」归属，把散文句的 flag 算到了脚本头上。

修法（`tools/sche_tasks_lint.py`）：新增 `FLAG_RE` / `CLAUSE_SEP_RE` / `split_clauses()`，flag 归属改为**子句级**，且仅当 flag 出现在该脚本 token **之后**才归属它；跨子句、flag 在前的一律不报。新增两条负控：

- `NC23`：跨子句的 flag 不得误报 `FLAG_UNKNOWN`
- `NC24`：flag 出现在脚本之前的同一行，不得误报

修后该处不再计入 FAIL（weekly_report_reminder 仅剩真实 PATH_MISSING），本批 A/B 判据分布零变化即由此保证。

## 5. 待批台账（唯一决策面，未批准前保持现状）

`temp/pending_decisions/DECISIONS_sche_tasks_cwd_anchor.md`（39 行 / 4721 B；决策列留空、裸 ID，已跑 `view_pending` 对账入账 3 条）：

- **D1（推荐）**：对 8 条任务执行 `apply --approve "批准 DECISIONS_sche_tasks_cwd_anchor.md:D1"` —— 全量锚定 + 9 处路径改写，落盘前自动备份到 `temp/l193_backup/<ts>/`。
- **D2**：只修 3 条**硬失败**任务（`linuxdo_ai_daily_summary` / `weekly_inspection` / `weekly_report_reminder`），其余 5 条漂移型暂不动。
- **D3**：把 `tools/sche_tasks_cwd_fix.py` 的 `verify` 接入 `efficiency_monitor_daily` / 反射器巡检段（与 R469 D1 同源，避免两条 lint/修复路径分叉）。

三条均 `revers=0`（apply 前有全量备份目录，可原样回拷）。**不批后果**：三条 FAIL 继续每次巡检报红，且 `../` 写法的 cwd 依赖继续在 cwd 漂移时静默失败（R375 的 `temp/temp` 漂移即此形态）。

## 6. 边界与残余

1. **生产 prompt 零改动**——`apply` 未执行；`verify` 只在沙盒副本上跑。
2. **未接线**：`tools/sche_tasks_cwd_fix.py` 未接 `complete_task`、未加定时、未接 `efficiency_monitor_daily`（D3 待批）；`tools/sche_tasks_lint.py` 的接线仍为 R469 D1 待批状态。
3. **未验证项**：D1 落盘后的「真跑定时任务」未做（需等定时窗口；沙盒只证 prompt 文本与 lint 判据，不证运行时行为）。故本报告只声明「静态改写 + 判据复跑」层面的完成。
4. **锚定行默认值硬编码用户路径**（`/Users/cuip/projects/GenericAgent`）——单机口径；跨机需以环境 `GA_ROOT` 覆盖（这正是用 `:=` 默认值而非写死 `cd /…` 的原因）。
5. `plan_production.json` 中 `approval_token` 字段就是批准语原文，执行者可直接引用，避免手打漂移。

## 7. 复现命令（逐条可跑）

```
python3 tools/sche_tasks_lint.py --selftest
python3 tools/sche_tasks_cwd_fix.py selftest
python3 tools/sche_tasks_cwd_fix.py plan --json temp/l193_notes/plan_production.json
python3 tools/sche_tasks_cwd_fix.py verify
python3 tools/sche_tasks_lint.py --json temp/l193_notes/lint_production.json
python3 temp/pending_decisions/view_pending.py --aging      # 台账对账
```

## 8. 快照口径声明

本报告中的工具行数/字节/sha256、selftest 计数、plan/verify/lint 输出均为 **2026-09-28 本会话命令的原始输出快照**；`tools/sche_tasks_lint.py` 在本次修订后 sha 已变（R469 记为 `ff779ce6d0f69b85`），引用请以现场 `sha256sum` 为准。本报告不含任何逐字引用的外部语料，全部内容为实测输出与现场描述。
