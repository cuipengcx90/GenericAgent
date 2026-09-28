# 三支并行成果合并入 main 与仓级验收（全量套件 30/30 + 阶段门 21/22，唯一 FAIL 为声明的需本地运行态）

- 编号：R485 ｜ 日期：2026-09-28 ｜ 类型：工程（合并 + 验收）
- 仓库：`~/develop/herdr-agents`（origin=`cuipengcx90/herdr-agents`，已 push）
- GA 侧对应工作：报告本体归档（本文件）、三索引补行、GA 仓本地提交

## 1. 背景与任务

上一轮把三条并行轨（子代理 T1/T2/T3）的产出各自落在独立分支/worktree 上，本轮要求「把并行成果整理起来合并」，并在合并后做**仓级验收**（不是只看某一支）。

三支来源（均在工作树内自带提交）：

| 轨 | 内容 | 合并提交 |
|---|---|---|
| stage3 | 外部作者团队包链路 + E29 路径逃逸门（阶段三验收 2/3） | `2c2da7c` |
| herdr-ci | 四项真机端到端演练的确定性单入口 `tools/drill_suite_herdr.sh` | `bd49c4c` |
| stage4-design | 阶段四目标驱动循环契约层设计稿 + `_test_mailbox` 清理复核证据 | `7a9d5dd` |

## 2. 交付：合并链与主仓基座

```text
c52c7c2 stamp: main 合并三支后重盖章(含 stage3_v0 与 doc_state_render/indirect_sites_report 入输入面)
7a9d5dd merge stage4-design: 阶段四目标驱动循环契约层设计稿 + _test_mailbox 清理复核证据
bd49c4c merge herdr-ci: 四项真机端到端演练确定性单入口 tools/drill_suite_herdr.sh
2c2da7c merge stage3: 外部作者团队包链路 + E29 路径逃逸门（阶段三验收2/3）
```

- 三支改动**均为纯新增**，与主仓既有 20 条未提交工作（另一会话在制）**零交集**；合并前先存兜底 sha，全程 `merge`，**未使用任何 `--hard`**。
- 合并后主仓未提交工作仍为 **20 条**（批前=批后），即：本会话没有吞掉也不该吞掉他会话的在制改动。
- 按盖章铁律（R460）**在目标仓库内**重建 l1 索引 + native 树后再 stamp：产物为 `control-plane/derive_guard_v0/manifest.json` 单文件 **10+/5−**，输入面 **192 → 197**（新增 5 项 = 三支带入的 stage3_v0 与 doc_state_render/indirect_sites_report 等）。
- 推送：`c9fe8ae..c52c7c2` 成功，`origin/main == c52c7c2`（ahead 0）。

## 3. 仓级验收证据

### 3.1 全量测试套件（隔离 worktree @7a9d5dd）

在**隔离 worktree**（避免污染主仓在制改动）跑全量：

```text
suites run: 30
ALL SUITES PASSED
drills: 8 passed, 0 failed
```

证据日志：`GA/temp/bg_task/logs/w1_verify_suite.log`（128 行；末 3 行即上述汇总）。
命令：`PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh`（本机默认 `python3` 无 pytest，必须显式 `PYTHON=`）。
基线对比：R476 时点全仓 **28** 套件 ⇒ 本次合并后 **30** 套件全部通过。

### 3.2 主仓级闸门（真身口径）

```text
[OK] native_tree      120 文件逐字节一致
[OK] l1_index         产物与 197 个输入均未变
[OK] board_html       产物与 63 个输入均未变
[OK] trust_gate_copies 2 份复制件与单一源逐字节一致
[MANUAL] control-plane/autonomy_v0/kpi_real.json 已知派生件但本批无机器判据（1 个）
[MANUAL] docs/diagrams/*.html 已知派生件但本批无机器判据（9 个）
汇总: MANUAL=2 OK=4
RC=0
```

另三道仓级闸门：`doc_state_check --strict` rc=0 / `authority_boundary_check` rc=0 / `residual_registry_check` rc=0。

### 3.3 阶段门（22 步）

在**孤本 worktree W2-gate @c52c7c2** 跑 `control-plane/stage_gate_check.sh`（该脚本会写 `control-plane/*/runs/`，放主仓会与另一会话的在制改动混同）：

```text
steps: 22   FAIL: 1（其中需本地运行态：1）   （SKIP 条目不进入统计：它们的证据不在本仓）
FAIL  7b  漂移闸门真机 check（本仓当前状态） ［需本地运行态：runs/ 被 gitignore］  (rc=1)
        汇总: DRIFT=1 MANUAL=1 OK=1 STALE=1 UNKNOWN=1
STAGE-GATE: FAILED (1 step(s))
```

判定：**唯一 FAIL = 步骤 7b，属脚本自带声明的「需本地运行态」类**。脚本头部明写「仅在『需本地运行态』步骤上失败 ⇒ 干净 clone 下这属于『证据载体没入库』，不等于代码坏了」。
根因即 R460 已固化的盖章铁律：native 树 README 内嵌 `repo: <绝对路径>`（仓库局部口径），**worktree 内盖章的 manifest 在 worktree 语境必报 DRIFT**。
反证：**同一闸门在主仓真身跑 = rc=0 / OK=4 / MANUAL=2**（见 3.2）⇒ 不是代码缺陷。

## 4. 关键坑（本批新增/复核）

1. **worktree 跑闸门 ≠ 主仓跑闸门**：凡涉及「派生件与输入面是否一致」的 check，在 worktree 内必然受 `repo:` 绝对路径口径影响而假红 ⇒ 这类步骤应判定为「需本地运行态」，或一律回主仓跑（本批做法：套件在 worktree、真机 check 在主仓）。
2. **阶段门会写 runs/**：在主仓跑会与另一会话的在制改动混同 ⇒ 必须换孤本 worktree；代价是产生 7b 这类假红，两者取其一。
3. **合并前先存兜底 sha、全程 merge**：本次三支均纯新增，但判据不能靠「看起来是新增」，而是合并前逐支核对改动面与主仓 20 条在制工作交集为空。
4. **`PYTHON=` 必须显式**：默认 `python3` 在 PATH 首位是 homebrew python3.13（无 pytest），不显式指定会得到 rc=2 一行 ERROR。

## 5. 边界与残余

- 步骤 7b 的「真机」语义**只在主仓成立**；clean clone 场景下该步永远无法 PASS（`runs/` 被 gitignore），这是**已声明的设计边界**，非本批引入。
- 三支带入的 `feat/w1-*` 分支在合并后**保留未删**（track worktree 已 `remove --force`）；如需清理分支请另行指示。
- 本报告只覆盖「合并 + 验收」；阶段三剩余验收项与阶段四/五工作**不在本批范围**。
- GA 仓只做本地提交（origin 为上游 `lsdefine/GenericAgent`，无推送权限）。

## 6. 复跑命令

```bash
# 合并后全量套件（隔离 worktree）
git -C ~/develop/herdr-agents worktree add --detach /tmp/verify <sha> && \
  cd /tmp/verify && PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh

# 主仓级漂移闸门（真身口径，唯一能过 7b 的位置）
cd ~/develop/herdr-agents && python3 control-plane/derive_guard_v0/derive_guard.py check; echo RC=$?

# 阶段门（22 步；须在孤本 worktree 跑，避免写脏主仓 runs/）
cd <worktree> && bash control-plane/stage_gate_check.sh; echo RC=$?
```
