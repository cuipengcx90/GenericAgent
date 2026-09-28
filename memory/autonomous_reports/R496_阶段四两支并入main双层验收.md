# R496 阶段四两支在制成果并入 main 的双层验收（第23批 C1 / TODO L198）

日期：2026-09-29 ｜ 仓库：herdr-agents（main）｜ 验收基线 HEAD：`d45eba4`（== origin/main）
本批性质：**只读验收**，对 herdr-agents 主干零改动（仅建隔离 worktree 复跑 + 只读闸门；worktree 内运行态产物不回流）。
判定口径：TODO L198 五条（①提交进 origin/main ②主仓真机闸门 rc=0 ③隔离 worktree 全仓套件 ALL PASSED + drills 8/8 ④阶段门唯一 FAIL 若属「需本地运行态」须给主仓 rc=0 对照 ⑤旧契约 selftest 不回归）。

## 结论

**五条验收全部通过。** 阶段四两支在制成果（goal_loop 生产端 / cost_quota 闸门）与阶段五立项（STAGE5_CHARTER）已由并行会话并入 `main` 并推送远端；本批在两处独立环境复核：隔离 worktree 套件面 32 套件 `ALL SUITES PASSED` + drills 8/8，主仓真机面四道门禁 + 旧契约 selftest 全 rc=0。
阶段门的唯一 FAIL（步骤 7b）经同命令回主仓复跑 rc=0 ⇒ **环境/口径差，非代码缺陷**（与 R485/R460 同一归因）。

## 1. 验收①：三支提交确在 main 且已同步远端

| 项 | 命令 | 结果 |
|---|---|---|
| main HEAD | `git rev-parse HEAD` | `d45eba47d450d702e7328ea87e6330d2483eff2d` |
| origin/main | `git rev-parse origin/main` | 同上（逐字相同） |
| 三支祖先性 | `git merge-base --is-ancestor {fcecb0e,a316d6e,ce960b4} HEAD` | 三者 `$?=0`（对 `origin/main` 亦 `=0`） |
| 双侧差 | `git rev-list --left-right --count origin/main...HEAD` | `0	0` |
| 工作区 | `git status --porcelain | wc -l` | `20`（全为他会话在制改动，本批文件零命中） |

★ 首测曾在**主仓**执行 `git cat-file -t fecb0e` 报 `Not a valid object name`，复测同一条命令正常（`commit`）。归因：该瞬间存在并发 git 操作（他会话 fetch/gc 重写 pack 索引）⇒ **「短名解析失败」不得单独作为「对象不存在」的判据**，须复测或用 `git merge-base --is-ancestor` 直判。

## 2. 三支在制成果（入 main 的内容面）

| 支 | tip | 相对父提交 diffstat | 落点 |
|---|---|---|---|
| 阶段四工作项3 goal_loop 生产端 | `fcecb0e` | 4 files, +1652 −1 | `control-plane/goal_v0/{goal_loop.py 857行, test_goal_loop.py 614行}`、`docs/product/F11_GOAL_LOOP_WIRING_SPEC.md 180行`、`control-plane/README.md` |
| cost_quota 闸门补强 | `a316d6e` | 4 files, +1546 −97 | `control-plane/cost_quota_v0/{cost_quota.py 924行, test_cost_quota.py}`、`docs/product/F12_COST_QUOTA_GATE_SPEC.md` |
| 阶段五立项 | `ce960b4` | 2 files, +431 | `docs/product/STAGE5_CHARTER.md 369行`、`STAGE5_CHARTER_RESULT.md 62行` |

合并形态：`d700d99`（双父 merge：父1 `a55ca58` = 本线、父2 `6ec2a38` = verify 链）→ 其后 `d45eba4` 重盖章（l1 输入面 199→203）。

## 3. 验收②：主仓真机闸门（只读，全 rc=0）

| 闸门 | 命令（cwd = `~/develop/herdr-agents`） | rc | 输出要点（逐字） |
|---|---|---|---|
| 派生闸门 | `python3 control-plane/derive_guard_v0/derive_guard.py check` | 0 | `汇总: MANUAL=2 OK=4`，含 `[OK] native_tree 120 文件逐字节一致` / `[OK] l1_index 产物与 203 个输入均未变` / `[OK] board_html 产物与 63 个输入均未变` / `[OK] trust_gate_copies 2 份复制件与单一源逐字节一致` |
| 状态陈述 | `python3 tools/doc_state_render.py --check` | 0 | `一致（生成块 = 唯一源渲染结果）` |
| 权威边界 | `python3 tools/authority_boundary_check.py` | 0 | `authority-boundary fails=0 specs=29 missing=20 grandfathered=20` |
| 残余登记 | `python3 tools/residual_registry_check.py` | 0 | `residual-registry fails=0 rows=13`（点名并集 R-01..R-13） |

## 4. 验收⑤：旧契约 selftest 不回归

`python3 control-plane/goal_v0/goal.py selftest` ⇒ rc=0，末行 `[goal_v0 selftest] 26/26 通过`；负控（非法状态转移 / 同一 revision 多写者 / event_id 复用 / 升级不可决策 / 升级选项缺 impact）逐条仍报对应码（`ILLEGAL_TRANSITION` / `MULTIPLE_WRITERS` / `EVENT_ID_REUSE` / `ESCALATION_UNDECIDABLE`）。
即：本批为 goal_v0 新增的 `goal_loop.py` **未触碰**契约层判定语义。

## 5. 验收③：隔离 worktree 全仓套件面

环境：`~/develop/herdr-drills/r496_verify_20260929`（`git worktree`，HEAD=`d45eba4`），命令 `PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh`（后台 bg_task 起，日志 `temp/bg_task/logs/r496_suite.log`，8294B）。

```
suites run: 32
ALL SUITES PASSED
```

免依赖演练段（`tools/drill_suite.sh`）：

```
  ✅ contract   ✅ m4   ✅ readiness   ✅ skip   ✅ trust   ✅ kpi   ✅ apcontract   ✅ a4
drills: 8 passed, 0 failed
```

★ 与 R485 基线（30 套件）对比：本轮 32 套件 ⇒ 阶段四两支新增 2 个 pytest 套件（goal_v0 / cost_quota_v0）已随 main 进 CI（`run_tests.sh` 用 `for d in control-plane/*/` glob 自动发现，套件数不含"手工登记"环节）。
★ 套件会改写受跟踪的 `control-plane/acceptance_v1/runs/*.json`（本批 worktree 内 `git status` 恰 4 条修改），**不提交、不回流**。
★ 本机默认 `python3` 无 pytest ⇒ 必须 `PYTHON=/opt/anaconda3/bin/python3`（否则 rc=2 一行 ERROR）。

## 6. 验收④：阶段门（22 步）与主仓对照

worktree 内跑 `control-plane/stage_gate_check.sh`（该脚本会写 `control-plane/*/runs/` ⇒ **只能在孤本 worktree 跑**，否则污染主仓他会话在制改动）：

```
FAIL  7b  漂移闸门真机 check（本仓当前状态） ［需本地运行态：runs/ 被 gitignore］  (rc=1)
steps: 22   FAIL: 1（其中需本地运行态：1）   （SKIP 条目不进入统计：它们的证据不在本仓）
仅在"需本地运行态"步骤上失败 —— 先按各批规格重建 `control-plane/*/runs/` 再判； 干净 clone 下这属于"证据载体没入库"，不等于代码坏了
STAGE-GATE: FAILED (1 step(s))
```

**同一条命令在 worktree 与主仓的差异（逐项定性）**：

| 检查项 | worktree 结果 | 主仓结果 | 归属 |
|---|---|---|---|
| `native_tree` | `[DRIFT] 内容变 15：README.md, claude/plugins/herdr-agents/skills/autopilot/FAILURE_MODES.md, ...` | `[OK] 120 文件逐字节一致` | **本机安装树口径 + 他会话未提交 skills WIP**：native 期望面内嵌 `repo: <绝对路径>`（R460 已证=仓库局部口径），且 `~/.claude/plugins` 是从主仓（含在制改动）刷新的安装树，worktree 内必假 DRIFT |
| `l1_index` | `[RESYNC]` | `[OK] 产物与 203 个输入均未变` | worktree 无本线重盖章后的 `.ga/l1` 运行态上下文 + 输入集含 worktree 内新增运行态文件 |
| `board_html` | `[STALE]（输入面缩小）` | `[OK] 产物与 63 个输入均未变` | worktree 缺 `control-plane/*/runs/`（gitignore）⇒ board 派生输入面比主仓小 |
| `trust_gate_copies` | `[OK]` | `[OK] 2 份复制件与单一源逐字节一致` | 一致 |
| `kpi_real.json` / `docs/diagrams/*.html` | `[MANUAL] 1` | `[MANUAL] 2` | 已知派生件（无机器判据），差异同为 worktree 缺 runs 面 |

⇒ 7b 的 rc=1 **不是代码缺陷**，判据是「同一命令在目标仓库（主仓）rc=0」。该结论与 R485（阶段门 7b 假红）、R460（native README `repo:` 局部口径）**独立复现同一根因**，本次为第三次实测确认。

## 7. 本批未做 / 边界

- 未做任何 herdr-agents commit/push（验收对象是**已并入**的成果，本批不代他会话做事）。
- 未重建 `control-plane/*/runs/`（属各批规格的重建动作，且会写主仓工作区）。
- 未改 E1 规格 / derive_guard 基线（改豁免规格会触发 `FAIL_MODIFIED`）。
- 未验远端 CI（本仓无该配置；验收面=本机真机复跑）。

## 8. 复现命令

```bash
# ①
cd ~/develop/herdr-agents && git rev-parse HEAD origin/main
for c in fcecb0e a316d6e ce960b4; do git merge-base --is-ancestor $c HEAD; echo "$c rc=$?"; done
# ②(主仓, 只读)
python3 control-plane/derive_guard_v0/derive_guard.py check
python3 tools/doc_state_render.py --check
python3 tools/authority_boundary_check.py
python3 tools/residual_registry_check.py
# ⑤
python3 control-plane/goal_v0/goal.py selftest
# ③④(隔离 worktree, 勿在主仓跑 ④)
git worktree add ~/develop/herdr-drills/r496_verify_20260929 d45eba4
cd ~/develop/herdr-drills/r496_verify_20260929
PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh        # suites run: 32 / ALL SUITES PASSED / drills 8/8
bash control-plane/stage_gate_check.sh                     # steps: 22 FAIL: 1 (7b, 需本地运行态)
python3 control-plane/derive_guard_v0/derive_guard.py check # worktree 内 rc=1；回主仓同命令 rc=0
```
