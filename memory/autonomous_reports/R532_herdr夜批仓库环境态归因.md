# R532 · herdr-agents 夜批「仓库环境态」归因（repo_rev / repo_dirty）

**日期**：2026-09-29
**仓库**：herdr-agents（可推送）· 本轮提交 `1cfeab1`（已 push，`rev-list --left-right --count origin/main...HEAD` = `0  0`）
**动机**：夜批失败时，「能力退化」与「环境态（脏工作区/未提交改动）」两种原因在台账里长得一模一样，
判据 1「连续 7 夜无人值守」会因此被误判。本轮把**环境态**变成回执的**一等字段**，并在 streak / watchdog 做归因标注。

---

## 1. 交付内容（3 文件，`git show HEAD --numstat` = 96/6 + 21/4 + 10/1 = **127+/11−**）

| 文件 | 字节 | 行 | sha256 前16 |
|---|---|---|---|
| `control-plane/unattended_v0/unattended.py` | 103556 B | 1979 | `34aa3129cdde0ef6` |
| `tools/nightly_batch.sh` | 15685 B | 277 | `99768b985426959c` |
| `docs/product/F8_UNATTENDED_NIGHT_LEDGER_SPEC.md` | 28340 B | 412 | `7aaeefcf21e7a88a` |

### 1.1 回执侧（`record-night`）

* 新增 `--repo-rev` / `--repo-dirty`（未提交改动条数）；`--repo-dirty` 为负数时 **rc=2 且零写盘**（负控④）。
* 载荷 `rc≠0` 且 `repo_dirty>0` 时，回执命令当场打印归因提示：
  `⚠ 载荷 rc≠0 且仓库有 12 项未提交改动（rev=bbb2222）⇒ 该夜失败**不能单独归因于能力**，先排环境态（脏树会让套件假红；判据=同 rev 干净树复跑）。`

### 1.2 判定侧（`streak` / `watchdog`）

* `receipts_per_night()` 聚合 `repo_dirty`（取 max）与 `repo_rev`；每夜行新增 `env_dirty` / `receipt_repo_dirty` / `repo_rev`，顶层新增 `env_attribution`。
* `print_streak()` 未达标原因串新增 `ENV_DIRTY`；`watchdog` 的 `failed_nights` / `last_receipt` 携带同样字段并打印提示。
* **只加标注，不改判据**：达标与否的算法、阈值、双轴（1a 连续性 / 1b 能力面）口径逐字不变。

### 1.3 驱动侧（`tools/nightly_batch.sh`）

采集 `REPO_REV=$(git rev-parse --short HEAD)` 与 `REPO_DIRTY=$(git status --porcelain | wc -l)`，随回执落账，并多打一行「仓库态」。

---

## 2. 验证（原始输出）

```
# 1) 单元面
python3 control-plane/unattended_v0/unattended.py selftest   → 72/72（66→72，+6 用例，含干净树/负数两条负控）
pytest control-plane/unattended_v0/test_unattended.py        → 26 passed

# 2) 冻结比对面（F8 规格内嵌数字由 gate_claim_replay 从文档正则提取后真跑）
python3 tools/gate_claim_replay.py check                     → rc=0

# 3) 全仓面（孤本 worktree，基线 197ff65/e0e94d0 一脉）
PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh          → 42 套件 ALL SUITES PASSED + drills 8 passed / 0 failed
```

### 2.1 沙盒演示（脏夜归因真的可见）

两夜台账（`/tmp/r532_demo2/ledger.jsonl`）：09-25 `rc=0 / dirty 0`；09-26 `rc=1 / dirty 12 @bbb2222`。

```
record-night 09-26 → ⚠ 载荷 rc≠0 且仓库有 12 项未提交改动（rev=bbb2222）⇒ 该夜失败**不能单独归因于能力**…

streak（窗口 7 夜 / 达标线 0.8）
  2026-09-25   0/0/0    0  0  0.0   否   NO_RUN,TOO_FEW_RUNS,DRILL_LIKE
  2026-09-26   0/0/0    0  0  0.0   否   NO_RUN,TOO_FEW_RUNS,DRILL_LIKE +ENV_DIRTY(12项未提交)
  ⚠ 其中 1 夜的仓库有未提交改动（2026-09-26:12项@bbb2222）⇒ **失败不可单独归因于能力**，先排环境…
  rc=1

watchdog（窗口 2026-09-22 → 2026-09-28）
  2026-09-25   OK      2026-09-26   FAIL      其余 5 夜 缺
  ⚠ 最近该夜 rc≠0 且仓库有 12 项未提交改动（rev=bbb2222）⇒ 归因先看环境（脏树），勿直接判能力退化
  看门狗判定：RECEIPT_FAILED   rc=1

# 负控④：负数
record-night --repo-dirty -1 → record-night: --repo-dirty 必须 ≥0（未提交改动条数）   rc=2
```

---

## 3. 门禁与归因（主仓 derive_guard 非全绿，**与本批无关**）

主仓 `derive_guard.py check` 汇总：`DRIFT=1  MANUAL=2  OK=2  RESYNC=1  STALE=1`。

* `[DRIFT] native_tree 内容变 15`：全为**他会话未提交的 skills WIP**（`claude/plugins/.../SKILL.md` 等），本批 3 文件零命中。
* `[RESYNC] l1_index`：**环境性**。对照实验：在**本批改动之前的提交** `e0e94d0` 另建 worktree，重建 `l1.py build`（222 文件 / 4365 符号）后跑同一门禁，**同样报 `RESYNC=1` + 同样的 native DRIFT + `STALE`** ⇒ 说明该 RESYNC 由「索引重建必然改写 `built_at`」造成，不是本批引入。
* 因此按 D-31 政策（共享工作区存在他人未提交输入时**不重建 l1 / 不盖章**），本批**未**在主仓重建索引或重新盖章。

---

## 4. 复查须知（口径）

* 本报告的「selftest 72/72」「42 套件」是 **2026-09-29 本会话真跑快照**；规格 F8 内嵌数字与之同批回填（`66/66 → 72/72` 三处），由 `gate_claim_replay check` 冻结比对，改实现必须同批改规格，否则 check 红。
* 环境态字段只增标注：判据 1 的达标结论在本批前后**逐字不变**（夜批仍按链长口径未达，DEFERRED D-32 不关闭）。
* 沙盒演示用的是 `/tmp/r532_demo2` 一次性台账，不入仓。

---

## 5. 残余

1. 判据 1「连续 7 夜无人值守」仍**未达**（零额度档按 D-32 不为凑数供料）——本批只解决**失败归因**，不解决**达标**。
2. `nightly_batch.sh` 的仓库态采集在**脏工作区**下会如实记录；若确实需在脏树下跑演练，streak 会标 `ENV_DIRTY` 而非直接判能力退化，属预期行为。
