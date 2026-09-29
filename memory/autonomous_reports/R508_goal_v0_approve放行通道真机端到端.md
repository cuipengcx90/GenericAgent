# R508 goal_v0 approve 放行通道：真机端到端复跑与规格口径订正

- 日期：2026-09-29
- 任务来源：用户「要执行」→ 承接上一轮（R-14 三处登记、提交 `3c1aea3`）落下的**唯一未验环节**：`goal_loop.py approve`（装置内写放行）**从未真机端到端跑过**，且演练里那份端到端（R505）跑在 `approve` 提交**之前**。
- 结论一句话：**装置内放行通道已可用且拒绝路径零写入**——真机 44 条用例 44 OK / 0 FAIL，`gate` 由 `BLOCK(IRREVERSIBLE_UNAPPROVED)` 转 `PASS`；顺带查出并订正 F11 规格里一处**退出码口径与实现/测试不一致**的文档缺陷（`4f95531`，已 push）。

---

## 1. 为什么要补这一次真跑

| 事实 | 证据 |
|---|---|
| 此前的 goal 端到端演练跑在 `approve` 之前 | 演练根 `~/develop/herdr-drills/g4_goal_e2e/runs/` 下 `ls -la` 实测的既有 run 目录 mtime ＝ `run_r497_main` **08:20**、`run_r497_e2e` **08:19**、`clean_20260929` **08:21**，而 `approve` 的实现提交 `5b862a1` 提交时间实测为 **2026-09-29 09:12:56 +0800**（`git show -s --format='%h %ci %s'`）|
| 旧演练里不可逆放行**由装置外手工写** journal | `docs/product/ROADMAP_MASTER.md` §8.1 R-14 原文（订正前）：「`goal_loop` 没有「写 approval」的 CLI，真机演练里放行只能由装置外写入 `scope=irreversible` 记录（＝第二写者，破了「单一写者」纪律）」 |
| 本批补齐后**没有独立复跑** | 上一轮只做了代码提交 + 门禁复跑，未跑真机链路 |

⇒ 本轮只做一件事：**在提交后的 HEAD 上，用真进程、真文件、真删除，把放行通道正反两路都跑一遍**，并把拒绝路径「零写入」从「文档声称」变成「sha 实测」。

## 2. 交付物（产品仓 `~/develop/herdr-agents`）

| 提交 | 内容 | 规模 |
|---|---|---|
| `5b862a1` | `goal_v0`: 补「装置内写放行」CLI（R-14 闭环）＝`control-plane/goal_v0/goal_loop.py` + `test_goal_loop.py` + `F11_GOAL_LOOP_WIRING_SPEC.md` | 3 文件，+282 / −14 |
| `e7f8b25` | `chore(derive_guard)`: 重盖章 l1_index（goal_loop 改动后重建） | `manifest.json` 3+ / 3− |
| `8b6d14d` | R-14 改述：不可逆放行已补装置内写入口 ⇒ 残余改为「放行主体不可验真」 | 3 文件，3+ / 3−（`ROADMAP_MASTER.md` / `STAGE3_DECLARATION.md` / `STAGE4_WORK_ITEM_LEDGER.md`） |
| `4f95531`（本轮新增） | `docs(F11)`: 订正 `approve` 的 `SCOPE_NOT_APPLICABLE` 退出码为 `rc=1`（与实现/测试/§8 一致）与判据字段名 `risk_class` | 1 文件，1+ / 1− |

- 推送状态：`git push origin main` ⇒ `8b6d14d..4f95531 main -> main`；`git rev-list --left-right --count origin/main...HEAD` = **0	0**（与远端同步）。
- 工作区边界：提交只包含 F11 单文件 ⇒ `git show --stat HEAD` 仅列出 `docs/product/F11_GOAL_LOOP_WIRING_SPEC.md`；其余 **20 条他会话在制改动**（`.gitignore` / `AGENTS.md` / `CHANGELOG.md` / skills 等）提交前提交后均为 20 条，未被吞。

## 3. 真机端到端（方法）

- 复跑脚本（GA 侧，不入产品仓）：`temp/r508_notes/run_approve_e2e.py`
- 真跑根：`~/develop/herdr-drills/g4_goal_e2e/runs/approve_20260929`（68K）
- 被测仓：`/Users/cuip/develop/herdr-agents`，HEAD=`8b6d14d`
- 契约指纹（逐字取自 run 内 `evidence/contract_sha.json`）：
  - `control-plane/goal_v0/goal.py 0b5452ad27cc1249`
  - `control-plane/goal_v0/goal_loop.py 17f4e2d5e5906588`
- 全过程落 `transcript.txt`（**316 行 / 23466 B**），逐条结果 45 行（44 用例 + `TOTAL`），清单另存 `temp/r508_notes/result_list.txt`。

**用例构成（44，机械计数自 `temp/r508_notes/result_list.txt` 按用例名前缀归类、无兜底项）**：真外部动作 7（`REAL-PRODUCE-*` 3 项真产出 + `REAL-EXEC/REAL-ACCEPT-*` 4 项真跑/真删）＋ 生命周期与放行写 25（`open`/`evidence-*`/`add-item-*`/`*-advance-*`/`escalate`/`resolve`/`APPROVE-*`）＋ 拒绝与重放负控 7（`NC*`）＋ `gate` 3 ＋ `resume` 2 = **44**。

**尾部自证**：

```
gate-BEFORE-approve(expect Block)        rc=1 expect=1 OK
gate-AFTER-approve(expect PASS)          rc=0 expect=0 OK
gate-FINAL                               rc=0 expect=0 OK
TOTAL=44 FAIL=0
```

## 4. 关键证据（逐字引用 transcript）

**4.1 `gate` 拦不可逆动作（放行前）**

```
[rc=1]
[BLOCK] goal=g4-goal-approve @r1 违规 1 条（其中三要素 0 条）
  - IRREVERSIBLE_UNAPPROVED @ item:I3: irreversible 且 state=CLOSE_READY 却无 approve/irreversible 批准记录
  stats: items=3 events=11 escalations=1 idempotent_replays=0
```

**4.2 三条拒绝路径（含退出码语义）**

| 用例 | 期望 | 实测 | 装置输出 |
|---|---|---|---|
| 非不可逆票据放行 | rc=1 | rc=1 ✅ | `[BLOCK] SCOPE_NOT_APPLICABLE：I1 的 risk_class='reversible'，不是 irreversible ⇒ 无需放行（放行只对不可逆动作有意义）` |
| 自批（owner/验收 owner/执行者） | rc=1 | rc=1 ✅ | `[BLOCK] SELF_APPROVAL_BANNED：'ga-verifier' 是本目标的 owner / 验收 owner / 该票据的执行者之一（ga-exec、ga-lead、ga-verifier）⇒ 不能给自己批不可逆动作；请换一个独立的人或角色来放行` |
| 未知票据 | rc=2 | rc=2 ✅ | `[INPUT] item 不存在：'I9'（先 add-item）` |

**4.3 放行与重放**

```
[approve] I3（risk_class=irreversible）放行已落账 by=user（请示 ESC-I3-1）
[approve] IDEMPOTENT：I3 已由 user 放行（scope=irreversible），journal 未变
[BLOCK] APPROVAL_CONFLICT：I3 已有放行记录（by=user）且内容不同 ⇒ 不改写历史（要撤回请另开一条升级请求让人重批）
```

**4.4 放行后链路走通（真删文件 → 验收）**

```
rm -f .../artifacts/g4/scratch.tmp && test ! -f .../artifacts/g4/scratch.tmp
[rc=0]
REAL_DELETE_OK
[advance] I3 CLOSE_READY → ACCEPTED by ga-verifier (event_id=ev-b3c5fee84cd6)
[PASS] goal=g4-goal-approve @r1 违规 0 条（其中三要素 0 条）
  stats: items=3 events=12 escalations=1 idempotent_replays=0
```

**4.5 「拒绝路径零写入」= sha 实测（不是文档声称）**

| 探针点 | journal sha256[:16] | bytes |
|---|---|---|
| 三组坏输入之前（基线） | `36a38ebc655608d3` | 15807 |
| 三组坏输入之后（scope + 自批 + 未知票据） | `36a38ebc655608d3` | 15807 |
| 真放行之后 | `59abdcd43e75d732` | 16121 |
| 幂等重放 + 冲突重放之后 | `59abdcd43e75d732` | 16121 |

对应断言行（脚本内 fail-closed 判据）：

```
# [零写入断言] 拒绝路径零写入: OK
# [零写入断言] 放行确实写了账: OK
# [零写入断言] 幂等/冲突两路零写入: OK
```

⇒ 「服务端拒绝三路零写入」「真放行确实写账」「重放不改写历史」三条**都有前后字节+sha 支撑**。

## 5. 门禁复跑（F11 订正后，主仓真身）

```
$ python3 control-plane/goal_v0/goal_loop.py selftest      → [goal_loop selftest] 29/29 通过   rc=0
$ python3 -m pytest control-plane/goal_v0 -q               → 58 passed in 8.38s            rc=0
$ python3 control-plane/derive_guard_v0/derive_guard.py check
    [OK] native_tree 120 文件逐字节一致；[OK] l1_index 产物与 205 个输入均未变；
    [OK] board_html 产物与 63 个输入均未变；[OK] trust_gate_copies 2 份复制件逐字节一致；
    汇总: MANUAL=2 OK=4                                                                     rc=0
$ python3 tools/doc_state_check.py --strict                → stage-markers fails=0 hints=9   rc=0
$ python3 tools/authority_boundary_check.py                → fails=0 specs=30 missing=20 grandfathered=20  rc=0
$ python3 tools/residual_registry_check.py                 → fails=0 rows=14（点名含 R-14）  rc=0
```

## 6. 全仓套件（孤本 worktree）

跑法（一条命令，本机默认 `python3` 无 pytest ⇒ 必须传 `PYTHON`）：

```
$ cd ~/develop/herdr-drills/r508_verify_20260929    # 孤本 worktree @8b6d14d
$ PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh
```

日志尾部逐字：

```
drills: 8 passed, 0 failed
---
suites run: 33
ALL SUITES PASSED
```

- 套件面：`suites run: 33` + `ALL SUITES PASSED`（33 个套件全绿，含本轮改动的 `control-plane/goal_v0`）
- 演练面：`drills: 8 passed, 0 failed`（免依赖演练八项：contract / m4 / readiness / skip / trust / kpi / apcontract / a4）
- 进程面：`bg_task status --name r508_suite` ⇒ `EXITED_OK pid=16301 exit=0`；日志 `temp/bg_task/logs/r508_suite.log` 8595 B
- 套件数 32 → 33 的差额已归因：`d45eba4`（R496 时点）→ `8b6d14d` 期间新增 `control-plane/consumer_replay_v0`（R501 的真实消费者回放套件，`dd20785`/`039b53a`），逐个 `git ls-tree` 比对测试目录集合得 新增=['consumer_replay_v0'] 消失=[]

## 7. 现场发现并订正的规格缺陷（F11 §7 表）

**缺陷**：同一份 `F11_GOAL_LOOP_WIRING_SPEC.md` 内，`approve` 的「非不可逆票据」退出码**写了两套**：

| 位置 | 原文 | 与实现是否一致 |
|---|---|---|
| §7 判据表 | 「| 目标不是不可逆票据 | 票据的 `kind` 是否属不可逆类（如 `release`） | rc=**2** `SCOPE_NOT_APPLICABLE`，零写入 |」 | ✗ 实现返回 **rc=1** |
| §8 用例构成 | 「非不可逆票据放行 ⇒ rc=**1** 零写入」 | ✓ |

**谁是权威**：实现与测试 —— 判据在 `control-plane/goal_v0/goal_loop.py:669`（`if item.get("risk_class") != SCOPE_IRREVERSIBLE:`）、`raise Refused(...)` 在 `670`，而 `Refused` 定义在 `goal_loop.py:120-121` 且 docstring 逐字写明「契约违规 ⇒ rc=1（拒绝写入）」，测试 `test_goal_loop.py:655` 的用例名即 `nc_approve_non_irreversible_refused_inert`，docstring 写「NC：非不可逆票据无需放行 ⇒ rc=1 且零写入」，且 `approve(..., expect=1)`。

**订正**（提交 `4f95531`，1 行）：`rc=2` → `rc=1`；同批把判据字段名从 `kind` 改为 `risk_class`（实现读的是 `item.get("risk_class")`，`kind` 是另一字段）。

**订正安全性验证**：F11 **不在** `derive_guard` 的输入面（`manifest.json` 里无 F11 token）也不在 l1_map 输入 ⇒ 无需重盖章（订正后 `derive_guard check` 仍 rc=0）；F11 带 `## Authority boundary` 节 ⇒ 不属 20 份豁免基线，改它不会触发 `FAIL_MODIFIED`（订正后 `authority_boundary` fails=0）。

## 8. 边界与残余

1. **放行主体不可验真**（R-14 残余，已在三处登记）：装置只把 `--by` 当字符串落 journal，同机任何进程都能填一个名字；真人核验在装置信任边界之外。触发时怎么发现：人工核对 journal 里 `by` 与真实放行动作是否同一人。
2. **「哪种放行可自动」未定义**（F11 §9）：本层只提供写通道 + 自批禁止，无人值守下的放行政策面不在本批范围。
3. **套件证据的提交时点**：全仓套件跑在 `8b6d14d`（本轮 F11 文档订正之前）；订正仅动 1 行散文文档，且订正后四条门禁（derive_guard / doc_state / authority_boundary / residual）已复跑 rc=0 ⇒ 未重跑全仓套件（成本/收益不划算），此点在此显式声明，不作「HEAD 全绿」的断言。
4. **端到端证据根在仓外**（`~/develop/herdr-drills/…`，不入版本控制）⇒ 与 R505 同口径：报告内录逐字原文，根目录本身靠本机留存。

## 9. 复跑方法（下次一条命令）

```bash
python3 /Users/cuip/projects/GenericAgent/temp/r508_notes/run_approve_e2e.py          # 全量 44 用例
# 只看放行通道最小集（换新根，避免脏账）
python3 - <<'PY'
# 见脚本内 A() 帮助函数：open → add-item(release/irreversible) → advance 到 CLOSE_READY
#   → gate(期 BLOCK) → approve --by user → gate(期 PASS) → approve 重放(期 IDEMPOTENT)
PY
```

判据（三条缺一不可）：`gate` 放行前 `rc=1 / IRREVERSIBLE_UNAPPROVED`、放行后 `rc=0 / PASS`；拒绝路径 journal sha **逐字不变**；重放 `IDENTICAL ⇒ 零写入`、异内容 `APPROVAL_CONFLICT ⇒ 不改写历史`。

## 10. 本轮踩到的两个坑（可复用）

1. **探针文件名字写错 ⇒ 假绿**：零写入探针最初按 `journal.jsonl` 定位，实际账本名是 `goal.jsonl` ⇒ `None == None` 也判「零写入 OK」。修法：探针必须 **fail-closed 定位**（`glob` 唯一命中 + `assert` 文件存在），定位不到直接判失败。
2. **写用例期望码前先核规格哪一套为准**：本轮 `NC5`（未知票据）首跑失败，原因是脚本按「未知 = rc=1」猜；查 F11 §3「子命令与退出码」分类（`2 = 输入不可用`）后改为期望 2 ⇒ 顺藤摸瓜才发现 §7 表自身的 rc=2/rc=1 矛盾（§7 结论）。**用例期望码必须回源到「实现 + 测试 + 规格三处一致」，不一致即先当缺陷查**。
