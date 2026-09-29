# R517 attestation_v0 消费者面收口（第25批 L215）

> 批次：自主第25批 / TODO L215（评分 5.00，最强约束=**只做消费者与登记面收口，不新增能力、不重做 R510**）
> 仓库：`~/develop/herdr-agents`（提交 `ee66927` 内容 + `ee6a760` 盖章；已 push，`origin/main` = `ee6a760`）
> 时点：2026-09-29

## 1. 背景与问题

R510 落地了阶段五的最小能力 `control-plane/attestation_v0/`（对外背书包度量背书 + 准入探针，只读 CLI）与规格 `docs/product/F14_ATTESTATION_READINESS_SPEC.md`。但按 `consumer_replay_v0` 的 census 口径，它是 **DOC_ONLY**（`prod=0`，只在文档里出现），而 R510 的登记条目**没有给出「为何 DOC_ONLY 是设计态而非漏接线」**；同时 F14 缺少 R501 建立的「登记面快照」小节，阶段五状态陈述存在两处（手写 vs 单一源）的风险。

L215 要的就是把这三处补齐——**不新增能力、不重做 R510 的交付**。

## 2. 交付内容

### 2.1 先取干净基线（防「改完才红」）

在隔离 worktree（主仓 HEAD=`c727d28` 时的干净快照）复跑 census，确认**改动前**基线：`declared=25 / problems=0`，其中 `attestation_v0` 为 `DOC_ONLY prod=0 doc=6`。

### 2.2 登记面：把 DOC_ONLY 的理由写进登记条目

`control-plane/consumer_replay_v0/dispositions.json` 的 `attestation_v0` 条目替换 `note`，逐条交代：

- **doc 命中面（5 个文件）**：`control-plane/README.md`、`control-plane/consumer_replay_v0/dispositions.json`、`docs/product/DEFERRED.md`、`docs/product/F13_REAL_CONSUMER_REPLAY_SPEC.md`、`docs/product/F14_ATTESTATION_READINESS_SPEC.md`——**均为文档提及，无一处是调用**。
- **为何不制造假消费者**：F14 §5/§6 明写零生产接线、只供人手工准入复跑，本仓不存在可接的生产路径。
- **改判触发点**：发布/准入流程真的调用它时，`category` 应变 `WIRED` 并更新本条目（把「为什么现在可以不接线」与「什么时候必须改判」同时写死，避免 DOC_ONLY 成为永久免检标签）。

### 2.3 F14 规格：补 §6「登记面快照」（复现口径）

按 R501/F13 §6 的体例新增快照节，附**可复跑命令**与**实测输出**，使「DOC_ONLY」这一判定可被任何人在干净 clone 上复现，而不是靠读文字相信。规格从 87 行 → **104 行**（`wc -l -c` = `104 7212`，sha256 `cc989fca…`）。

### 2.4 状态陈述回单一源：ROOT_MAP 阶段五行改为生成块

阶段五状态此前手写在 `docs/product/ROOT_MAP.md`；本批把它**并入 `docs/product/ROADMAP_MASTER.md` §7.1 的唯一源**，由 `tools/doc_state_render.py --write` 机械渲染，`--check` 校验（铁律：状态陈述禁手写多份，否则必 drift）。

## 3. 验收

| 验收项 | 判据 | 结果 |
|---|---|---|
| ① census 可复现 | `declared=25 / problems=0`，`ZERO` 空 | 通过 |
| ② 能力自测不弱化 | `attestation.py selftest` 11 passed / `pytest test_attestation.py` 25 passed | 通过 |
| ③ 全仓套件（隔离 worktree `~/develop/herdr-drills/r517_verify2` @ `ee6a760`） | `PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh` = `suites run: 35` / `ALL SUITES PASSED` + `drills: 8 passed, 0 failed` | 通过（日志 `/tmp/r517_run_tests.log`；`herdr-team/bin` 段 348 passed in 199.69s 最长） |
| ④ 盖章闸门（主仓真机） | `derive_guard.py check` | 通过（OK4/MANUAL2） |
| 附：状态/规格门禁 | `doc_state_check --strict` / `authority_boundary_check` / `residual_registry_check` / `deferred_registry_check` | 全 rc=0 |

## 4. 变更清单（相对 `c727d28`）

```
ee66927 docs(L215): attestation_v0 登记理由补全 + F14 快照节 + 阶段五状态回单一源
 control-plane/consumer_replay_v0/dispositions.json |  2 +-
 docs/product/F14_ATTESTATION_READINESS_SPEC.md     | 31 +++++++++++++++++-----
 docs/product/ROADMAP_MASTER.md                     |  2 +-
 docs/product/ROOT_MAP.md                           |  4 +--
 4 files changed, 28 insertions(+), 11 deletions(-)

ee6a760 chore(derive_guard): L215 后重建 l1(211 输入)并重新盖章
 control-plane/derive_guard_v0/manifest.json | 6 +++---
 1 file changed, 3 insertions(+), 3 deletions(-)
```

## 5. 边界与残余

- **本批不含任何能力新增**：`control-plane/attestation_v0/` 源码零改动；`F14` 的改动是「补快照 + 口径」，不是改判据。
- **DOC_ONLY 仍是 DOC_ONLY**：本批做的是「登记 + 说明 + 改判触发点」，**没有**为了把 census 刷成绿色而硬塞一个假消费者（这是 L215 强约束的原话要求）。
- **盖章铁律复现**：`native_tree` 期望里 README 内嵌 `repo: <绝对路径>` 属**仓库局部口径** ⇒ 在 worktree 跑的盖章结论回主仓必报 DRIFT；故套件在 worktree 跑、真机 check 回主仓（R460/R485）。
- 本批未触碰阶段四/阶段五其他在制项；工作区其余 modified 文件均属他会话在制改动。

## 6. 复跑命令

```bash
# 登记面（改判应在这里体现）
cd ~/develop/herdr-agents && python3 control-plane/consumer_replay_v0/replay.py census
# 能力自测
cd control-plane/attestation_v0 && python3 attestation.py selftest
PYTHON=/opt/anaconda3/bin/python3 python3 -m pytest control-plane/attestation_v0 -q
# 状态单一源
python3 tools/doc_state_render.py --check
# 真机盖章（必须在主仓，不在 worktree）
python3 control-plane/derive_guard_v0/derive_guard.py check
```
