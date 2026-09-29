# R528 — 夜批成本闸门 + worktree 清理 + R-01..R-05 残余探针复跑

日期：2026-09-29 21:1x（本报告所有数值均为当场命令原始输出）
仓库：herdr-agents（提交 `977453e`，已 push）、GA（本报告与三索引/L1）

---

## 0. 结论（一句话）

用户四问全部落地：**35 个 worktree 已合并核验后删除（释放 5.11 GiB，备份 940K）**；**「夜批装了就开始烧 token」这一真缺陷已从三个层面堵死**（现场卸载 + 文件移出登录加载面 + 仓内默认改零额度档、真跑档缺授权拒装 `rc=2`）；**R-01..R-05 五条残余探针实跑完毕**（R-01/03/05 `rc=0`，R-02 设计即「人工」，R-04 静态证据 + 1 条 fail-closed）；同时把「不为凑数持续烧额度」这一政策**登记为 D-32**，避免它以后被当成「工作没做完」。

---

## 1. 四问逐条

### 1.1 清理 worktree（清之前先合并）

- 分支侧复核：**无真未合并内容**。`w5_coldstart` 那 1 个提交是 patch 等价（`coldstart_v0` 已在 main），已打 tag 保险；`E4_2` 规格是被收敛的旧设计 ⇒ 只归档不提交；`limit_contract` 三版比对 main 版 283 行是超集（旧版 174/190 行）⇒ 无需合并。
- 归档：`~/develop/herdr-drills/worktree_removal_backup_20260929`（`du -sh` = **940K**）。
- 删除：35 个 worktree 全删 + `git worktree prune`，**释放 5.11 GiB**。
- 现状（当场回读，仅剩主仓 + 本报告的验证用孤本 worktree）：

```
$ git worktree list
/Users/cuip/develop/herdr-agents                              977453e [main]
/Users/cuip/develop/herdr-drills/r528_verify_20260929-210816  977453e (detached HEAD)   ← 本报告验证用，收尾删除
```

### 1.2 D1/D2/D3 待批项

R524 批次已落地（F16 §8.2/§8.4/§8.5 有真机证据）⇒ 无待执行动作。

### 1.3 「7 天夜批要烧 token 吗」

**会烧——而且发现得比预想更糟：不只是「跑起来才烧」，是「装了就每晚自动烧」。**

现场证据与三层处置：

1. live plist 内嵌 `GA_ALLOW_REAL_CLI=1` + 载荷 `drills`（真起 agent、真调真 CLI）⇒ 每晚 3:30 无人值守烧额度。
2. `launchctl bootout` 卸载；**并且发现 plist 文件仍留在 `~/Library/LaunchAgents`**（下次登录会重载！）⇒ 已移出：
   - `~/.herdr/launchagents-disabled/com.herdr.nightly.plist.20260929-2023`（1416B，sha256 `5943e3f49853df7a94ff6664b46eb2e4691031263319019879edc8a733163cbd`）
   - 复核：`launchctl list | grep -i herdr` 仅剩 `com.herdr.console`。
3. 仓内加固（提交 `977453e`，5 文件 62+/10−）：
   - `tools/nightly_batch.sh`：授权门**按载荷分流**——`drills`（烧额度）需 `GA_ALLOW_REAL_CLI=1`；`verify-all`（仓内免依赖、零额度）不设门。新增两条 selftest。
   - `tools/launchd/com.herdr.nightly.plist.template`：`GA_ALLOW_REAL_CLI` 由**写死 1** 改为占位符 `__ALLOW_REAL_CLI__`。
   - `tools/launchd/install.sh`：默认 `HERDR_NIGHTLY_PAYLOAD=verify-all`；`payload=drills` 而 `HERDR_NIGHTLY_ALLOW_REAL_CLI!=1` ⇒ **拒装 `rc=2`**。
   - 文档：`docs/product/F8_UNATTENDED_NIGHT_LEDGER_SPEC.md` §8.1（日界驱动与载荷档位）；`docs/product/DEFERRED.md` D-32。

三态实测（原始输出）：

```
① 默认 --dry-run rc=0
payload   : verify-all（仓内免依赖演练总入口）
载荷命令  : bash tools/verify_all.sh
[dry-run] 未建目录、未起进程、未写台账；真跑请加 GA_ALLOW_REAL_CLI=1 去掉 --dry-run

② HERDR_NIGHTLY_PAYLOAD=drills HERDR_NIGHTLY_ALLOW_REAL_CLI=0 --install rc=2
ERROR: payload=drills 但 HERDR_NIGHTLY_ALLOW_REAL_CLI != 1（每夜都会被 nightly_batch 拒跑）。
       要么显式承认成本：HERDR_NIGHTLY_ALLOW_REAL_CLI=1；要么用默认零额度档 verify-all。

③ drills + ALLOW=1 --dry-run rc=0
payload   : drills（真机 4 项端到端演练（复用 CI 件））
载荷命令  : bash tools/drill_suite_herdr.sh

$ bash tools/nightly_batch.sh --selftest
nightly_batch selftest: 9/9 passed
（含「零额度档免授权可计划（verify-all dry-run）」「烧额度档仍拒跑（drills 无授权）」两条新负控）
```

**口径边界（必须说清）**：零额度档**不烧 token**，但它也**不给阶段门判据 1 供料**（判据 1 要的是真跑夜批台账）。所以这是「不为凑数烧钱」的政策选择，不是「判据 1 达标」，已登记 `DEFERRED.md` D-32（触发条件=出现零额度但等价于真实无人值守负载的档位，或用户显式授权烧额度跑满 7 夜）。

### 1.4 R1–R5 是什么、能不能执行

**R1–R5 = `docs/product/ROADMAP_MASTER.md` §8.1「已接受残余」表的 R-01..R-05**（不是我上条消息里说的候选一二三）。「执行」的语义 = **跑各自探针**，不是新开发。逐条实跑：

| 残余 | 探针 | 实跑结果 |
|---|---|---|
| R-01 E3.1 间接调用点未接线 | `python3 control-plane/trust_v0/real_cli_gate.py audit --strict` | **rc=0**；`cli_direct ∧ 生产路径` 未接线 = 0；`cli_indirect` 未接线 38（即 R-01 接受项） |
| R-02 旁路（反射器）收据账本未接 | 表内标注 **人工**（无收据账本可查） | 无探针（设计如此，非缺陷） |
| R-03 外部裁判（Jev）依赖外部服务/网络 | `python3 control-plane/acceptance_v0/channel_probe.py check --team release`（离线档） | **rc=0**（decl=ok；不打网络） |
| R-04 同 kind 成员共用账号配额（429） | `herdr-team/bin/agent_unstick.py` 静态证据 | `rate_limited→nudge` / `quota_or_login→escalate`（L30/L41/L130/L145/L161/L231）；契约常量 `RETRY_MAX_RESTART` 读不到 ⇒ **1 fail-closed** |
| R-05 装置（herdr）语义偶发不可靠 | `python3 herdr-team/bin/team_drive.py --dir ~/.cache/herdr/teams/_r1_team_drive_drill --max-actions 1` | **rc=0**，相位 `CLOSE_READY`（终态），actions=0（report-only、零副作用） |

---

## 2. 本批改动清单（herdr-agents 977453e，已 push）

```
$ git show HEAD --stat
 5 files changed, 62 insertions(+), 10 deletions(-)
 4	2	docs/product/DEFERRED.md
23	0	docs/product/F8_UNATTENDED_NIGHT_LEDGER_SPEC.md
 1	1	tools/launchd/com.herdr.nightly.plist.template
18	4	tools/launchd/install.sh
16	3	tools/nightly_batch.sh
```

提交前逐文件核过 `git diff -U0`：五个文件的 hunk 全是本会话语义，无他会话在制内容被带入。

## 3. 门禁与验收

| 门禁 | 结果 |
|---|---|
| `tools/nightly_batch.sh --selftest` | **9/9 passed** |
| `tools/authority_boundary_check.py` | rc=0 `fails=0 specs=37 missing=20 grandfathered=20` |
| `tools/residual_registry_check.py` | rc=0 `fails=0 rows=14` |
| `tools/doc_state_check.py --strict` | rc=0 `stage-markers fails=0 hints=9` |
| `tools/deferred_registry_check.py` | 台账行 **32**、D-01..D-32 声明区间已同步；仅剩 2 红见 §4 归属 |
| `control-plane/consumer_replay_v0/replay.py census` | `declared=27 problems=2`（见 §4 归属） |
| `control-plane/derive_guard_v0/derive_guard.py check` | `DRIFT=1 STALE=1 OK=2 MANUAL=2`（见 §4 归属） |
| 全仓套件（孤本 worktree @977453e，先 `l1.py build`） | 见 §5 |

## 4. 非绿项的归属（三条，全部非本批引入）

1. `deferred_registry_check` 2 条 `FUTURE_UNCOVERED §16/§17`：来自 `docs/product/FUTURE_CAPABILITIES.md` 中**他会话未提交的 +37 行**（新增 §16/§17）；本批只把 D-32 登记进台账，与 §16/§17 无关。
2. `census` 2 条 `l1_map_v0 MISMATCH/CATEGORY_VERDICT_CONFLICT`：实测消费者文件 = `control-plane/stage_gate_check.sh`（他会话在制 `M`，+22/−17），非本批文件。
3. `derive_guard`：`DRIFT native_tree` 全为他会话 skills WIP；`STALE l1_index` 与 **本批 4 个文件无关**——核对 `manifest.json` 的 l1 输入面，`tools/nightly_batch.sh`、`tools/launchd/install.sh`、`docs/product/F8_*`、`docs/product/DEFERRED.md` **均不在其中**（脚本判定 `False`）。

按 **D-31 政策**（共享工作区存在他人未提交输入时不重建 l1、不盖章，避免把他人在制内容冻进产物输入哈希），本批**刻意不重建/不盖章**，非遗漏。

## 5. 全仓套件复跑（孤本 worktree）

- 位置：`~/develop/herdr-drills/r528_verify_20260929-210816`（detached HEAD `977453e`，先 `python3 control-plane/l1_map_v0/l1.py build` ⇒ `索引完成: 221 文件 / 4325 符号，耗时 1.19s`；踩的是 R528 旧坑「干净 worktree 不建索引必红 4 例」）。
- 结果（日志 `~/develop/herdr-drills/r528_suite_20260929-210816.log` 原始末行）：

```
drills: 8 passed, 0 failed
=== tools/nightly_batch.sh (夜批驱动 · selftest) ===
---
suites run: 41
ALL SUITES PASSED
```

即 **41 个套件 ALL SUITES PASSED + drills 8 passed / 0 failed**（与 R527 基线 41 一致，本批未增套件）。

## 6. 未做 / 边界

- **判据 1（连续 7 夜 ≥80% 无人值守）仍未达**（政策面 D-32；链长口径见 `unattended.py streak --profile strict`）。
- R-02 仍为「人工」探针（旁路无收据账本）；R-04 有 1 条 fail-closed（契约常量读不到），未扩修。
- 本报告不含任何「真跑夜批」的 token 支出——零额度档 `verify-all` 不调真 CLI。
- 验证用 worktree `r528_verify_20260929-210816` 于收尾删除。
