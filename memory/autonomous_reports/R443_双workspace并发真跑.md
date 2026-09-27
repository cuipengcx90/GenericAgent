# R443 · 双 workspace 并发真跑（阶段三多工作区前置证据）

- 日期：2026-09-27
- 来源：TODO「【第17批·三路评审 7/7/5=6.33】herdr-agents 阶段三前置：双 workspace 并发真跑，落 workspace_id 双值证据」
- 性质：**真跑**（真 herdr 真 pane，非桩、非模拟）；只读判据 + 新增两文件+一测试
- 提交：herdr-agents `cc87e8e`（3 文件 558 insertions，已 push main）

## 1. 结论

阶段三「多工作区」的**前置证据从 0 变为可复现**：在隔离 worktree（基线 sha `e39cef8`）里
**真并发**起两个 herdr workspace（`w42` / `w41`），各 6 个真 pane（lead + 5 角色），
同一时刻窗口**真重叠 3 s**，两端到终态 `ACCEPTED`（落账投影 `CLOSE_READY`），
装置事件、ingest 分片、委派锁三处**全部按 workspace_id 分片且零串扰**；
同 ws 并发写被拒（rc=1）而跨 ws 并发互不影响（rc=0/rc=0）。

判定由**机械断言器**给出（35/35 PASSED），不是人眼看日志——该断言器自带 8 条负控，
且已接入 CI（`test_dual_ws_drill_verifier.py`，autonomy_v0 套件 2 passed）。

## 2. 交付物

| 文件 | 行/字节 | 作用 |
|---|---|---|
| `control-plane/autonomy_v0/examples/dual_ws_concurrent_drill.sh` | 170 行 / 8632 B | 演练本体：起两队、投递、轮询哨兵、判卷、并发锁负控、ingest、调用校验器 |
| `control-plane/autonomy_v0/examples/verify_dual_ws_drill.py` | 330 行 / 16209 B | **只读**机械判据：35 条断言 + `--selftest`（内建夹具 + 8 条负控） |
| `control-plane/autonomy_v0/test_dual_ws_drill_verifier.py` | 41 行 / 1446 B | 把判据器的正控/负控钉进 CI（防判据被改松后真跑假绿） |

可复跑命令（在任何含 herdr 的机器上）：

```
bash control-plane/autonomy_v0/examples/dual_ws_concurrent_drill.sh            # 真跑，产物落 $ROOT
python3 control-plane/autonomy_v0/examples/verify_dual_ws_drill.py --root <ROOT>   # 判据
python3 control-plane/autonomy_v0/examples/verify_dual_ws_drill.py --selftest      # 判据器自测（含负控）
```

## 3. 实测证据

真跑根目录 `/tmp/r443_drill`，日志 `run_v3.log`（★同目录 `run.log` 是 v2 残留，WS=w30/w3Z，**不引用**）。

### 3.1 两案例真并发

| | case a | case b |
|---|---|---|
| workspace_id | `w42` | `w41` |
| lead pane | `w42:p1` | `w41:p1` |
| 成员 pane | `:p2..:p6`（architect/task-breaker/coder/reviewer/tester） | 同构 |
| PANES | 6 | 6 |
| EPOCH_START / END | 1790459522 / 1790459526 | 1790459522 / 1790459525 |
| 各步骤 `_RC=0` 计数 | 12 | 12 |
| 基线 oracle | `BASELINE_RC=1`（起跑点必红，符合预期） | 同 |
| 外部 oracle 判卷 | `ORACLE_AFTER_RC=0` | 同 |

时间窗口重叠 = min(END) − max(START) = **3 s**（真并发，非串行）。

### 3.2 装置事件（事件 sink 副本，逐 ws 独立）

`a/contract_copy/events.jsonl` = 17 行，`b/contract_copy/events.jsonl` = 17 行，各为：

```
initialized 1 + member_registered 5 + member_completed 5 + advanced 6
```

- 每案例文件内 `workspace_id` 集合 = {己方 ws}（a={w42}、b={w41}），**无越界行**
- 每案例文件内 `run_id` 集合 = 单值（`2026-09-27-r443-a-1790459522-63292-herdr-team` / `…-b-…`）
- `advanced` 6 步把相位推到 `ACCEPTED`

### 3.3 ingest 反向分片

```
recs/w42/{.workspace,2026-09-27-r443-a-….json, .jsonl}
recs/w41/{.workspace,2026-09-27-r443-b-….json, .jsonl}
```

- 两分片 run 文件 `state=CLOSE_READY`，`history` 各 4 条（`PENDING→DISPATCHED→WORKING→CLOSE_READY`），
  且末条 reason 为 `device event=advanced phase=ACCEPTED`
- 两分片 run_id 不同、目录名=各自 workspace_id ⇒ 零串写

### 3.4 委派锁（并发负控）

| 场景 | 结果 | 期望 |
|---|---|---|
| 同 ws 两个进程同时 acquire | rc1=1, rc2=0 | **恰好一个成功** ✔ |
| 两个不同 ws 同时 acquire | x1_rc=0, x2_rc=0 | 都成功 ✔ |
| 同 ws 重复 acquire（串行） | `LOCK_REACQUIRE_SAME_WS_RC=1` | 被拒 ✔ |

锁文件按 ws 分片：`locks/{w42,w41}/delegation.lock`（race 场景另有 `locks/r443-race-{same,x1,x2}/`）。

## 4. 机械判据（防假绿）

- 真产物：`35/35 PASSED`（末行）
- 判据器自测：`--selftest` → `[selftest] ALL PASSED`，其中**8 条负控全部被抓出**
  （例：RC 非 0 未报 / 角色缺失 / 两案例串行无重叠 …，每条都触发至少 1 条断言失败）
- CI 接线：`test_dual_ws_drill_verifier.py` 断言「正控 35 条全过 ∧ 负控恰好 8 条被抓 ∧ 无漏报」，
  `pytest control-plane/autonomy_v0/test_dual_ws_drill_verifier.py` → `2 passed`

## 5. 迭代中踩到的三个硬约束（已写进脚本头注释）

1. **team 目录必须直挂** `~/.cache/herdr/teams/<team-id>`：放 `/tmp/...` 下 `teamctl` 直接
   `BLOCKED: team directory must be directly under ...`，随后 register/advance/ingest 连锁失败（v1 的根因）。
2. **`herdr pane run` 是异步 fire-and-forget**：rc=0 只代表投递成功 ⇒ 判卷必须等 pane 内哨兵文件
   （v1 未等哨兵 ⇒ 外部 oracle 假红 `ORACLE_AFTER_RC=1`）。
3. **角色名/单一写者**：登记角色名用连字符 `task-breaker`（下划线名不会进角色表 ⇒ 少登记 1 人，v2 实测
   「5 个成员只记到 4 个」）；`register/complete/advance` 的 `--actor-pane` **必须**是 lead pane（成员 pane 仅绑定取证）。

附：bash 中 `${VAR}` 后紧跟全角括号会被吞进变量名（`set -u` 报未绑定）——仓内 `lint_shell_vars` 已把该形态拦住，本次脚本 0 处违规。

## 6. 边界与未做

- 演练仍在 `/tmp`（默认落点），只跑「1 题 × 2 ws」，**未**接入生产 run 目录（生产 runs 的 workspace_id 仍全为 default）。
- 未接定时、未改任何既有接线面（新增文件 0 修改既有代码）；对 ROADMAP §8.1 的残余登记无影响。
- 真跑依赖 herdr 可用；CI 只跑**判据器**（纯 stdlib + 合成夹具），真跑本身不进 CI（无 herdr 的机器跑不了）。

## 7. 收尾（2026-09-27）

首轮全仓套件报 `trust_v0` C23「repo audit 无阻塞点」失败：根因=本次新增的**演练脚本住在生产路径**
（`examples/`），被 `trust_v0/cmd_audit` 判为「真直调 ∧ 生产 ∧ 未接线」；按同族 `r1_contract_wiring_drill.sh`
同款补 `real_cli_gate` 双门解析块（默认拒绝，未显式放行即拒）后，`audit --strict` blocking=0。

- 提交：`cc87e8e`（3 文件 558 行）+ 收尾修复 `9e5f55e`（20 insertions / 2 deletions），均已 push `main`。
- 全仓套件复跑（`PYTHON=/opt/anaconda3/bin/python3 ./run_tests.sh`）：`suites run: 20` / `ALL SUITES PASSED`，
  进程 `EXITED_OK` exit=0；其中 `herdr-team/bin` 348 passed、`autopilot/bin` 144 passed、`control-plane/trust_v0` 35 passed。
- 教训：**只要脚本被 `cmd_audit` 判为生产路径，就必须显式接线 `real_cli_gate`**（哪怕是演练脚本）；否则新增文件会把全仓门禁打红。
