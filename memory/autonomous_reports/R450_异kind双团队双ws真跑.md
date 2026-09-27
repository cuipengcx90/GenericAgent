# R450 阶段三验收 1 前置：异 kind 双团队 × 双 workspace 并发真跑

> 任务来源：`temp/TODO.txt:163`（第 18 批规划·三路评审 codex/槽3/槽4 = 9/9/7 = 8.33，并列最高）
> 目标：隔离 worktree 里按 profile 直起 **2 个 kind 集合不交的团队**，各跑到 ACCEPTED；KPI 报表**按 workspace_id 分列**且每个数字可由账本逐条复算。
> 结论：**验收四条全达成**（真跑全绿 + 判卷器 64/64 + 负控实测必红 + 只读双自证）。

## 1. 交付物（herdr-agents, main 已 push）

| 提交 | 内容 | 行数 |
|---|---|---|
| `17edcd5` | 演练本体 + 只读机械判据 + CI 回归 + `ingest --agents` + `kpi_report --by-workspace` | 6 文件，1297 插入 / 32 删除 |
| `179f85a` | KPI 段按 per-ws 分片逐目录传 `--runs-root`（collector 只扫下一层 `*.json`） | 1 文件，15 / 3 |
| `d234111` | ①drill 写 `facts.json` 按 ROLES 真源（连字符）反解 `PANE_/KIND_` 变量安全名 ②verify 的 kind 判据改为「实际 kind ∈ 声明回退链」 | 2 文件，19 / 9 |
| `4c1c993` | verify 分片 run_id 判据去随机化 + 负控新增失败全量打印（见 §5） | 1 文件，6 / 2 |

仓内 5 个文件：
- `control-plane/autonomy_v0/examples/hetero_kind_dual_ws_drill.sh`（361 行，演练本体，需真 herdr，不进 CI）
- `control-plane/autonomy_v0/examples/verify_hetero_kind_drill.py`（538 行，只读机械判据 + `--selftest`）
- `control-plane/autonomy_v0/test_hetero_kind_drill_verifier.py`（45 行，CI 回归）
- `control-plane/autonomy_v0/ingest_device.py`（+59/4，新增 `--agents`：把 role→kind 声明挂在 run 上）
- `control-plane/kpi_report_v0/kpi_report.py`（+187/28，新增 `--by-workspace`）

## 2. 真跑环境与命令

干净克隆（**判据文件改后必须重新 clone**，否则「失败」是环境不是产物）：

```
ROOT=/tmp/hetero_kind_drill4   REPO=/tmp/hetero_kind_drill4/wt      # HEAD=d234111
python3 "$REPO/control-plane/trust_v0/real_cli_gate.py" grant --tool herdr \
  --workspace-dir "$REPO" --actor "$(whoami)" \
  --scope launch --scope prompt --scope read --scope replace          # E3 双门是 AND
GA_ALLOW_REAL_CLI=1 ROOT=$ROOT REPO=$REPO bash "$REPO/control-plane/autonomy_v0/examples/hetero_kind_dual_ws_drill.sh"
python3 "$REPO/control-plane/autonomy_v0/examples/verify_hetero_kind_drill.py" --root "$ROOT"   # 判卷（只读，可重复跑）
```

## 3. 真跑读数（逐字取自产物，非复述）

**窗口与规模**（`a/log`、`b/log`）：两组同一时间窗 `EPOCH_START=1790482385`、`EPOCH_END=1790482413`（**窗口 28s，全程重叠并发**）；每队 `PANES=6`（1 lead + 5 成员）。

**kind 分流**（`a/log` 的 `KINDS_JSON` 与 `kinds.json.kind_set`）：
- A 队 `WS=w4A`：architect/coder/tester = `claude`，task-breaker/reviewer = `codex`，`kind_set=[claude, codex]`
- B 队 `WS=w4B`：architect/coder/tester = `gemini`，task-breaker/reviewer = `kimi`
- 两队 `kind_set` **交集为空**；每个角色 `AGENT_START_RC_<role>=0`（真起，非口头声明）

**状态机与账本**（`a/contract_copy/events.jsonl`、`b/.../events.jsonl`，各 17 行）：
`{initialized: 1, member_registered: 5, member_completed: 5, advanced: 6}`；
六次 advance 依次 `REQUIREMENTS_READY → DESIGN_READY → IMPLEMENTED → REVIEW_PASSED → TEST_PASSED → ACCEPTED`（各 `ADVANCE_*_RC=0`）；
每份事件流 `run_ids` 恰 1 个（`2026-09-27-ht-a-31681-herdr-team` / `...-ht-b-...`）。

**反向 ingest 分片**（`recs/<ws>/<run>.json`）：
`workspace_id=w4A state=CLOSE_READY run_id=2026-09-27-ht-a-31681-herdr-team`；B 队同形，`state=CLOSE_READY`，`INGEST_RC=0`。两目录 `.workspace` 内容分别为 `w4A` / `w4B`。

**KPI 按 ws 分列**（`kpi_ws.json` + `kpi_build.log` / `kpi_verify.log`）：
`records=2 real=2 simulated=0 unknowns=3`；`by_workspace`：`total_workspaces=2`、`workspaces=[w4A,w4B]`、`attribution_key=record.workspace_id`、`sum_check.ok=true`、`mismatch=[]`；
`metrics`：`human_interventions=0`、`rework_rounds=0`（各 `provenance.real=2`，contributors 逐条指向 `recs/w4A/...:kpi.human_interventions` 与 `recs/w4B/...`），`time_to_green / irreversible_actions / forced_interruptions = unknown`（**不写 0**，理由随行：无 ACCEPTED 记录 / 留痕文件不存在）；
`kpi_verify` 输出 `对账通过：5 个指标与账本逐条一致（unknown 3 项）`；`gates.by_workspace_sum_mismatch=0`。

**委派锁按 ws 分片**（`race/` + 各队 `lock_*.log`）：
- 同 ws 真并发 race：`rcs: rc1=1 rc2=0`（第二持有者被拒：`BLOCKED: delegation lock is held by run 'hetero-race-same-w1' ...`）
- 跨 ws 真并发 race：`x1_rc=0 x2_rc=0`（互不互斥）
- 队内复取：两队均 `LOCK_ACQUIRE_RC=0` / `LOCK_REACQUIRE_SAME_WS_RC=1`

**项目侧 oracle**（`oracle_baseline.log` / `oracle_after.log`）：基线 `checks=6 failed=4`（`BASELINE_RC=1`），修复后 `checks=6 failed=0`（`ORACLE_AFTER_RC=0`，`solve.done=0`）——真跑了 CLI 题，不是空跑状态机。

## 4. 判卷器（只读机械判定，不靠肉眼看日志）

`verify_hetero_kind_drill.py --root /tmp/hetero_kind_drill4` ⇒ **`64/64 PASSED`**（含 `--selftest` 合成夹具 + 13 条负控）。
负控实测（补丁后连跑 10 次，输出恒定）：`negctl_injection_caught PASS 注入 A 队事件一行对方 ws ⇒ 新增失败 4 条: case_a_events_own_ws,case_a_events_single_run,case_a_no_cross_contamination,shard_w4A_run_matches_events`，配 `negctl_injection_hits_right_check PASS`、`negctl_mirror_faithful PASS`（未注入镜像与本树逐条一致）。
CI：`control-plane/autonomy_v0/test_hetero_kind_drill_verifier.py` 2 passed；全仓 `suites run: 21 / ALL SUITES PASSED`（`SUITE_RC=0`，跑两遍：补丁前 `r450tests`、补丁后 HEAD `r450suite2`，均 exit=0）。

## 5. 过程中发现并修掉的不确定性（本轮新发现 F1）

**症状**：负控段在「新增失败 3 条」与「4 条」之间随机飘（探针版连跑 10 次 = 4 条×6 / 3 条×4），第 4 条恒为 `shard_w4A_run_matches_events`。

**根因**：该断言写的是 `s.get("run_id") == next(iter({r.get("run_id") for r in ev[c]}))`——用 set 的「任意一个元素」当期望值，**取值随 `PYTHONHASHSEED` 变化**；事件流混入注入行时约 50% 概率取到注入行的 run_id。

**修法（提交 `4c1c993`）**：改判据为 `len(ev_runs) == 1 and s.get("run_id") in ev_runs`（要求事件流**恰好一个** run_id 且分片记录属于它——对「多 run_id=串扰/重复记账」更严，且确定）；并把 `negctl_injection_caught` 消息由 `newly[:3]` 改为全量打印（原先条数 >3 时第 4 个名字被吞，属诊断盲点）。
**验证**：连跑 10 次恒 `64/64 PASSED` 且恒 4 条（含四名）；CI 2 passed。

首轮真跑还暴露并修掉 4 个真缺陷（记录于 `temp/hetero_kind_drill_notes.md`）：成员名超 32 字符硬约束、`kinds` 白名单误读人读散文、克隆陈旧（判据改了必须重 clone）、以及判据内置负控语义错（要求「未注入镜像全绿」⇒ 改差分口径：注入必须**新增**失败并点名 `case_a_events_own_ws`，未注入镜像与本树逐条一致；实现坑=`bad_cln/bad_inj` 必须先 `sorted` 再比）。

## 6. 边界与未做（不许把中间态当完成）

- 本次是**演练树**（`/tmp/hetero_kind_drill4`），生产 `control-plane/autonomy_v0/runs/` 的 `workspace_id` 仍全为 `default`（R435 的 D2 缺口**未被本项关闭**）；阶段三验收 1 只满足了它的「双团队可分列」前置证据。
- 两支团队**共用同一台机器与同一 herdr 实例**，窗口 28s 是「并发重叠」的实测下界（非长驻并发压测）；未做多机/多 daemon。
- R443（同质双 ws，窗口重叠 3s）与本项（异 kind，重叠 28s）是两批证据，口径不同**不可互推**。
- 阶段三其余工作项（配额固定出口、revision 快照、Authority boundary 门禁等）仍在其对应 TODO 行上，本项不代办。

## 7. 复跑入口

```
# 判卷（只读，可反复跑，幂等）
python3 control-plane/autonomy_v0/examples/verify_hetero_kind_drill.py --root /tmp/hetero_kind_drill4
# 判据自测（合成夹具 + 负控，进 CI）
python3 control-plane/autonomy_v0/examples/verify_hetero_kind_drill.py --selftest
PYTHON=/opt/anaconda3/bin/python3 ./run_tests.sh
```

**证据留档**：`temp/r450_evidence.txt`、`temp/r450_evidence2.txt`、`temp/r450_evidence3.txt`（逐字命令输出）、`temp/r450_verify_out.txt`（判卷全文）、`temp/r450_findings.md`（F1 记录）。
