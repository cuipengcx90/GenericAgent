# R513 herdr-agents 延后/刻意不做项台账 DEFERRED.md + 机械门禁

日期：2026-09-29
仓库：`~/develop/herdr-agents`（产品仓，可推送）
提交：`f8764ec`（建档 + 门禁）→ `0d4e07e`（A2 声明面自洽判据 + 台账计数订正）

---

## 1. 一句话

产品仓里此前散落着「这块我们不做 / 这个延后」的判断（ROADMAP_MASTER §8.1、FUTURE_CAPABILITIES 的 15 个小节、各规格正文），
但没有**唯一登记处**、也没有任何机械约束能发现「新写了一句不做却没登记」。
本轮把这件事做成**台账 + 机械门禁**：`docs/product/DEFERRED.md` 是唯一登记处，
`tools/deferred_registry_check.py` 是判据（已接进 `run_tests.sh` 成为第 N 个套件），
真实跑出 `0 problem(s)` rc=0、`selftest: 19 passed, 0 failed`。

## 2. 交付物与提交

| 提交 | 内容 | 变更规模 |
|---|---|---|
| `f8764ec` | `docs/product/DEFERRED.md`(新建) + `tools/deferred_registry_check.py`(新建) + `run_tests.sh`(接入) | 3 文件 558 插入 |
| `0d4e07e` | 门禁补 A2 声明面自洽判据 + 3 组负控；台账 §4/§5 计数订正 | 2 文件 51 插入 / 4 删除 |

逐文件实测（2026-09-29 12:38 提交后从工作树读取）：

- `docs/product/DEFERRED.md`：15027 B，sha256 前 16 = `15b32212025c3fd8`；行数口径见 §5 门禁自报的「81 行」。
- `tools/deferred_registry_check.py`：23376 B / 513 行，sha256 前 16 = `f75a578328f21310`。
- `run_tests.sh:160-163`：

```
if [ -f tools/deferred_registry_check.py ]; then
  echo "=== tools/deferred_registry_check.py (延后/不做项台账门禁) ==="
  if "$PY" tools/deferred_registry_check.py; then :; else echo "  FAIL deferred_registry_check" >&2; fail=1; fi
```

推送：`026b703..0d4e07e  HEAD -> main`（origin = `github.com:cuipengcx90/herdr-agents.git`）。

## 3. 台账口径与结构

`DEFERRED.md` 五个小节（`grep -n '^#'` 实测行号）：

| 行 | 小节 |
|---|---|
| 1 | `# 延后 / 刻意不做项台账（DEFERRED）` |
| 13 | `## 1. 判定口径` |
| 21 | `## 2. 台账` |
| 50 | `## 3. 已闭环（曾列为延后/未做，现已落地，留档不再计入台账）` |
| 62 | `## 4. 与其它登记面的关系（防重复登记）` |
| 73 | `## 5. 变更记录` |

§2 台账列（表头逐字）：`| ID | 项 | 类别 | 现状态 | 为什么现在不做 | 触发条件（何时做） | 复活/完成判据（怎么发现该做了） | 来源 | 关联残余 |`

真实计数（脚本对 24 行逐行解析，非人工誊抄）：

- 台账行 **24** 行，ID `D-01`..`D-24`，无跳号。
- 类别分布：**形态 7 / 生态 3 / 能力 9 / 工程 5**（合计 24）。
- 现状态分布：**WONTFIX 9 / PARTIAL 8 / DEFERRED 7**。
- §3 已闭环留档 **7** 条：`F-04` `F-05a` `F-06a` `F-08a` `F-09a` `F-10a` `F-14a`。

设计要点（这是台账有价值的部分）：

1. **每一行都必须能回答「怎么发现该做了」**——即复活/完成判据。写「人工（产品决策）」也允许，但必须显式写出来，
   不允许留空（门禁对空格有独立负控 NC2）。
2. **来源列必须可回源**——门禁把来源格里的 `xxx.md §N` 解析出来，回到该文件里检查小节真存在。
3. **§4 专门写「与其它登记面的关系」**——ROADMAP_MASTER §8.1（残余 R-01..R-14）与 `FUTURE_CAPABILITIES.md`（§1..§15）
   是另外两个登记面，三者关系写死在 §4，防止「同一条不在两个地方各记一遍」。

## 4. 门禁判据（五段）

工具 docstring 里的判据清单（逐条对应到实现）：

- **A. 结构**：每行九个格子不缺、类别必须在白名单 `{形态, 生态, 能力, 工程}` 内、来源路径存在且小节存在。
- **A2. 声明面自洽**（本轮新增）：§4/§5 里**手写**的「`D-01..D-NN` 范围」「形态/生态/能力/工程 计数」「已闭环留档 N 条」
  必须与 §2/§3 的真表逐项相等，任一项对不上即 `DECLARED_ABSENT`。
- **B. 与 §8.1 双向**：`ROADMAP_MASTER.md` §8.1 里写明「有意未做 / 有意不做」的残余，其 `R-NN` 必须进台账；
  反之台账「关联残余」格点到的 `R-NN` 必须真在 §8.1 里。
- **C. FUTURE 全覆盖**：`FUTURE_CAPABILITIES.md` 的每个 `## N.` 小节都必须被台账点名（既不漏也不多点名不存在的）。
- **D. 声明扫描**：扫全 `docs/`（实测 256 文件），其它文件里若又写了一遍「有意未做 / 有意不做」而该处未登记，即报。

## 5. 真跑输出（逐字粘贴）

```
$ python3 tools/deferred_registry_check.py; echo rc=$?
语料: docs/product/DEFERRED.md (81 行/15027 B) + docs/product/ROADMAP_MASTER.md + docs/product/FUTURE_CAPABILITIES.md；docs 扫描 256 文件
台账行 24；§8.1 残余 14 条；§8.1 标注「有意未做」['R-07', 'R-13']；FUTURE 小节 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15] 已点名 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]；带「不占 M 编号」声明 3 行；带「有意未做」声明 6 行
deferred_registry_check: 0 problem(s)
rc=0
```

```
$ python3 tools/deferred_registry_check.py --selftest 2>&1 | tail -3
  PASS NC6 §8.1 有意未做未登记 (rc=1)
  PASS NC15 台账缺失 fail-closed (rc=2)
selftest: 19 passed, 0 failed
```

（自报行数 81 与 §2 的 15027 B 同源；工具按文本切分计数，脚本另一次按行迭代得 80 行，差 1 为尾换行口径，不影响判据。）

## 6. 建档首跑抓出的三处真缺陷（`f8764ec`）

台账不是「写完就绿」——第一次跑门禁就把我自己写漏的地方抓了出来（这三条已写进 `f8764ec` 的提交信息）：

1. **`R-07` 漏登**：ROADMAP §8.1 里标了「有意未做」，但台账里没有对应行（判据 B 抓的）。
2. **`FUTURE_CAPABILITIES.md` §N 命名被反引号切断**：判据 C 解析小节号时，标题里的反引号把 `§N` 切开，
   导致「已点名集合」与「真存在集合」对不上（解析口径问题，已修）。
3. **形态锚点写成「公开发行 SaaS」**：台账里对 D-01 的形态描述与 §8.1 原文用词不一致，回源校验不过。

## 7. 台账自身的自相矛盾 → A2 判据的由来（`0d4e07e`）

**发现**：§4 表格里写「（本文件 D-01..D-23）」，§5 变更记录写「首批 D-01..D-23（形态 6 / 生态 3 / 能力 11 / 工程 3）」，
而建档后 §2 真表已增到 **D-24**、真实分布是 **形态 7 / 生态 3 / 能力 9 / 工程 5**。
（旧值逐字取自 `git show f8764ec:docs/product/DEFERRED.md` 的第 69、75 行。）
两处都是**手写摘要**在表增长后没跟着改——正是「状态陈述禁手写多份」那条铁律的又一次实例。

**处置**：

1. 同批修正台账 §4/§5 的两处声明值（并就地把订正记进 §5 变更记录）。
2. 给门禁补 **A2 声明面自洽**判据 + 3 组负控（selftest 16 → 19），使这类漂移今后由机器抓，而不是靠人眼；
   判据实现即常量 `DECL_RANGE_RX` / `DECL_COUNT_RX` / `DECL_CLOSED_RX`（`deferred_registry_check.py:61-63`）。

**过程坑（值得记）**：订正时我在变更记录里**引用了旧错值**（写「原写 D-01..D-23」），
结果被自家 A2 判据当成真声明值抓成误报。⇒ 结论：**声明型判据的文档里，不能字面复述被否定的旧值**；
改写为「原写范围少一代」「原分类计数与真表不符」这类不含字面量的措辞后双绿。

## 8. 残余与下一步

- 台账本身零残余：门禁 rc=0、selftest 19/19、已进 `run_tests.sh`。
- 未做（登记在案，不在本轮范围）：
  - 台账条目的 `PARTIAL` 8 条中，一部分对应阶段四/五的未闭环工作项，其状态推进仍须逐项真跑（与 `STAGE4_WORK_ITEM_LEDGER.md` 有交叉，已在 §4 写明关系）。
  - 「带『不占 M 编号』声明 3 行 / 带『有意未做』声明 6 行」这两处计数既打印**也参与判据**：
    「不占 M 编号」出现 ⇒ 台账必须点名「插件市场」（`PLUGIN_MARKET_UNDECLARED`，负控 NC13）；
    带 `R-NN` 的「有意未做」行若未登记 ⇒ `UNBOUND_DEFERRED_LINE`（负控 NC6）。
- 下一步（第 25 批 TODO L211）：**阶段四判据 2 探针化**——把「不可逆放行」聚合为可机械核算的逐夜指标。
  实测两处留痕源（2026-09-29）：① `~/.cache/herdr/teams/*/unattended-decisions.jsonl` —— 真机 **9 文件/85 行**，
  写入者是 `herdr-team/bin/pretrust.py` 与 `herdr-team/bin/agent_unstick.py`（不在 `unattended_v0/` 内）；
  ② goal 侧 `control-plane/goal_v0/goal.py:125` 的 `_has_approval(bundle, obj_id, scope)`，`scope="irreversible"` 在 :273 被检查。
  夜边界可直接复用 `control-plane/unattended_v0/unattended.py:89` 的 `night_of(ts, boundary_hour=DEFAULT_BOUNDARY_HOUR=8)`。

## 9. 复跑方式

```bash
cd ~/develop/herdr-agents
python3 tools/deferred_registry_check.py            # 判据（0 problem(s) rc=0）
python3 tools/deferred_registry_check.py --selftest # 19 组用例（含 15+ 负控）
python3 tools/deferred_registry_check.py --json     # 机读输出
bash run_tests.sh                                   # 全仓套件（门禁在 L160-163 被调用）
```


## 10. 全仓套件与演练复跑（2026-09-29，孤本 worktree）

- 命令：`cd <隔离 worktree> && PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh`
- 结果原文尾部（`temp/bg_task/logs/dr513_suites.log`，151 行/9276B）：
```
  ✅ apcontract
  ✅ a4
drills: 8 passed, 0 failed
---
suites run: 35
ALL SUITES PASSED
```
- 逐套件末行（同日志原样摘录）：

| 套件 | 末行 |
|---|---|
| `autopilot/bin` | 144 passed, 12 subtests passed in 71.93s (0:01:11) |
| `herdr-team/bin` | 348 passed in 199.58s (0:03:19) |
| `tools/bin` | 18 passed in 0.40s |
| `control-plane/acceptance_v0` | 51 passed in 4.95s |
| `control-plane/acceptance_v1` | 21 passed in 0.96s |
| `control-plane/attestation_v0` | 25 passed in 0.16s |
| `control-plane/autonomy_v0` | 186 passed, 5 subtests passed in 4.52s |
| `control-plane/consumer_replay_v0` | 23 passed in 6.60s |
| `control-plane/cost_quota_v0` | 26 passed in 2.11s |
| `control-plane/derive_guard_v0` |   (no pytest cases; ran as scripts) |
| `control-plane/exec_runner_v0` | 8 passed in 3.93s |
| `control-plane/goal_v0` | 58 passed in 5.43s |
| `control-plane/inbox_v0` | 26 passed in 0.54s |
| `control-plane/kpi_report_v0` | 22 passed in 0.06s |
| `control-plane/l1_map_v0` | 8 passed in 0.70s |
| `control-plane/l2_v0` |   (no pytest cases; ran as scripts) |
| `control-plane/l3_skills_v0` | 26 passed in 0.02s |
| `control-plane/monitor_v0` | 31 passed in 0.33s |
| `control-plane/objection_v0` | 33 passed in 1.93s |
| `control-plane/quota_exit_v0` | 11 passed in 0.50s |
| `control-plane/readiness_v0` | 27 passed in 0.09s |
| `control-plane/skip_ledger_v0` |   (no pytest cases; ran as scripts) |
| `control-plane/snapshot_v0` | 19 passed in 1.29s |
| `control-plane/stage3_v0` | 2 passed in 2.14s |
| `control-plane/team_harness_v0` | 207 passed, 20 subtests passed in 5.55s |
| `control-plane/trust_v0` | 35 passed in 2.65s |
| `control-plane/unattended_v0` | 20 passed in 0.08s |
| `tools/lint_shell_vars.py (repo-wide shell lint)` | lint_shell_vars: OK（扫了 42 个 shell 脚本，0 处 `$VAR`+非 ASCII） |
| `tools/doc_state_check.py (文档状态陈述门禁)` | stage-markers fails=0 hints=9 |
| `tools/doc_state_render.py (状态块生成源)` |   一致（生成块 = 唯一源渲染结果） |
| `tools/residual_registry_check.py (残余登记门禁)` | residual-registry fails=0 rows=14 |
| `tools/authority_boundary_check.py (规格必填段门禁)` | authority-boundary fails=0 specs=31 missing=20 grandfathered=20 |
| `tools/deferred_registry_check.py (延后/不做项台账门禁)` | deferred_registry_check: 0 problem(s) |
| `tools/indirect_sites_report.py (E3.1 间接调用点定性)` |   selftest: 13 passed, 0 failed |
| `tools/drill_suite.sh (免依赖演练)` | ALL SUITES PASSED |

结论：**suites run: 35 / ALL SUITES PASSED，drills 8 passed / 0 failed**（含本件新增的门禁套件；`deferred_registry_check: 0 problem(s)`）。
