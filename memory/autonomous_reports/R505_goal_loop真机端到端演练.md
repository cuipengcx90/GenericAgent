# R505 goal_loop 真机端到端演练（阶段四工作项3 取证）

- 日期：2026-09-29
- 范围：herdr-agents `control-plane/goal_v0`（F10 契约层 + F11 生产端 `goal_loop.py`）
- 性质：只读取证 + 真跑演练。**herdr-agents 主干零改动**（演练在孤本 worktree 内进行）
- 关联：R489（阶段四契约层并入 main）、R492（goal_loop 生产端接线，遗留「未做真机端到端演练」）、R494/R496（阶段四两支并入 main）

## 1. 结论

1. `goal_loop.py` 的**目标驱动循环已在真机跑通端到端**：正跑根 3 张票据全部 ACCEPTED、不可逆票据经人工放行后进入 ACCEPTED、升级结清、`gate` 判 PASS（rc=0，违规 0 条）。
2. 三条**装置侧硬约束**在真机被证实（此前只有契约层自测）：不可逆闸门落在 `gate`（判定层）而非 `advance`（写入层）；不可逆升级必须写全 `--cost-delta`/`--risk-delta`，缺一即 `ESCALATION_UNDECIDABLE`；**装置侧没有「给票据写放行（approval）」的 CLI 通道**，人工放行只能由外部写入 approval 记录（`scope=irreversible, object_id=<票据>`）。
3. 判定层**不可被人为绕过**：负控根里人已写下放行决策并 resolve 了升级，但因升级记录缺 `risk_delta`，`gate` 仍判 BLOCK（rc=1）——即「人批了」不等于「账本自洽」。
4. 新增独立复核器 `tools/goal_e2e_audit.py`（只读，从 journal 独立重建状态），负控 8/8、正控通过；对正跑根 findings=0，对两个负控根分别报 2 条 / 3 条，与 `gate` 结论方向一致且给出「为什么」的分解。

## 2. 环境与语料（只读快照）

| 项 | 值 |
|---|---|
| worktree | `/Users/cuip/develop/herdr-drills/g4_goal_e2e`（孤本）@ `d45eba47d450d702e7328ea87e6330d2483eff2d` |
| 产品仓主仓 | `~/develop/herdr-agents` HEAD=`039b53ae8450c5b9f21bd08d09c8e2f49f853d96`（工作区有他会话在制改动，本会话**未触碰**） |
| 正跑根 | `runs/clean_20260929/goalroot/goal.jsonl` — 18903B / 35 行 / sha256 前16 `375ff8e18935c674` |
| 负控根 A | `runs/run_r497_main/goalroot/goal.jsonl` — 19379B / 35 行 / sha256 前16 `883125c6277043fe` |
| 负控根 B | `runs/run_r497_e2e/goalroot/goal.jsonl` — 7405B / sha256 前16 `f93b1f6917469c40`（早期探索根：无升级、无 oracle） |
| 两主根是否同一 | 否（`diff -q` rc=1；sha256 前16 不同） |
| 证据清单 | `evidence_auditor/{audit_clean.txt,audit_exploratory.txt,audit_exploratory_with_resume.txt,audit_e2e_root.txt,gate_clean.txt,gate_exploratory.txt,negctl.txt,auditor_selftest.txt,goal_loop_selftest.txt}` |

说明：三个根都在**孤本 worktree** 内，不写主仓；`runs/` 属演练产物，不入版本控制。

## 3. 正跑根（`clean_20260929`）：全链通过

跑法（真 CLI，非桩）：`open` → `add-item ×3` → `evidence ×4` → `dispatch/working` → `advance` → `complete`（带外部 oracle 与验收三要素）→ 不可逆项 `escalate` → 外部写入 approval → `resolve` → `resume` / `status` / `gate`。

- 记录构成：`{goal:1, item:15, evidence:4, event:12, escalation:2, approval:1}`。
- 票据终态：`I1=ACCEPTED(reversible)`、`I2=ACCEPTED(review_required)`、`I3=ACCEPTED(irreversible)`，三张均带 acceptance 段。
- 不可逆闭环：`ESC-I3-1`（`irreversible:true`，`cost_delta:"1"`）→ decision 写入「批准：删除 scratch.tmp（可再生夹具）」→ approval 记录 `{object_id:I3, decision:approve, scope:irreversible, by:user}`。
- `gate`：**[PASS] rc=0**，`违规 0 条（其中三要素 0 条）`，`stats: items=3 events=12 escalations=1 idempotent_replays=0`。
- 独立复核（`goal_e2e_audit.py audit --root …/clean_20260929/goalroot`）：**findings=0 / ok=7**——J1 状态双通路一致（记录态 vs 事件推导）+ 与 `resume` 的 CLI 视图一致；J2 升级三要素/选项/证据/恢复点齐备；J3 三张票据均有外部 owner 事件、oracle 与证据引用；J4 不可逆推进有 `scope=irreversible` 的放行记录。

## 4. 负控根 A（`run_r497_main`）：人批过的账本仍被 BLOCK

- 同形账本（35 记录），`ESC-I3-1` 的 `cost_delta=null` 且缺 `risk_delta`。
- `gate`：**[BLOCK] rc=1**，唯一违规 `ESCALATION_UNDECIDABLE @ escalation:ESC-I3-1.risk_delta: 不可逆升级必须写明风险变化`；`stats` 与正跑根逐字相同。
- 独立复核（带 `--resume-json`）：**findings=1 / ok=6**，findings 恰好是被 gate 抓到的同一事实（`缺 ['cost_delta(不可逆升级必填)','risk_delta(不可逆升级必填)']`）；不带 `--resume-json` 时为 findings=2（多一条「缺独立对账通路」的自我声明）。
- 结论：**fail-closed 生效**——即便 approval 已写、resolve 已落，缺字段仍不放行。

## 5. 负控根 B（`run_r497_e2e`，早期探索）

独立复核报 **findings=3 / ok=1**：`escalations：账本零升级 ⇒ 无法验证三要素`、`item:I1：ACCEPTED 但无外部 oracle ⇒ 自验收`、`resume：未提供 --resume-json`。作为「自验收 + 无升级」形态的负控样本留存（该根未跑 gate 通过路径，仅作复核器灵敏度证据）。

## 6. 独立复核器（本次新增，只读）

- 文件：`tools/goal_e2e_audit.py`（17707B / 363 行 / sha256 前16 `c90262bcbe287b5d`；GA 仓内新增，未跟踪→本次入库）。
- 判据四组：**J1** 条目状态双通路（journal 记录态 vs 事件表独立推导）+ 与 `goal_loop resume` 的 CLI 视图对账（须显式给 `--resume-json`，缺则声明「不计通过」）；**J2** 升级三要素与不可逆代价；**J3** 票据外部 owner 事件 + oracle + 证据引用；**J4** 不可逆推进的放行记录。
- 自证：`selftest` 负控 **8/8**、正控通过；`negctl` **8/8**（NC1 缺 risk_delta / NC2 缺 tried / NC3 ACCEPTED 无 oracle / NC4 证据落点不存在 / NC5 自评通过 / NC6 不可逆无放行 / NC7 条目记录滞后于事件 / POS 全齐备应过）。
- 与生产 `gate` 的关系：本器**不替代 gate**，只做「从 journal 独立重建」的第二通路；两者对三个根的方向一致（正跑 0 违规 / 负控 A 同一事实 / 负控 B 暴露更多缺口）。

## 7. 怎么复跑（约 1 分钟）

```bash
WT=~/develop/herdr-drills/g4_goal_e2e
# 1) 生产端判定层
python3 $WT/control-plane/goal_v0/goal_loop.py gate --root $WT/runs/clean_20260929/goalroot     # 期望 rc=0 PASS
python3 $WT/control-plane/goal_v0/goal_loop.py gate --root $WT/runs/run_r497_main/goalroot      # 期望 rc=1 BLOCK（risk_delta）
# 2) 独立复核（第二通路）
python3 ~/projects/GenericAgent/tools/goal_e2e_audit.py audit \
  --root $WT/runs/clean_20260929/goalroot --resume-json $WT/runs/clean_20260929/final_resume.json   # 期望 findings=0 ok=7
# 3) 器自证
python3 ~/projects/GenericAgent/tools/goal_e2e_audit.py negctl      # 8/8
python3 ~/projects/GenericAgent/tools/goal_e2e_audit.py selftest    # 全部通过
python3 $WT/control-plane/goal_v0/goal_loop.py selftest             # 23/23
```

## 8. 交付物与耐久性

- 演练证据根：`~/develop/herdr-drills/g4_goal_e2e/`（worktree + runs/ + evidence_auditor/），**未入版本控制**（与 R454/R476 同惯例：真机证据根住仓外）。
- 入库（GA 仓）：`tools/goal_e2e_audit.py`（新增只读复核器）。
- herdr-agents：**零改动**（未新增文件、未提交）；发现的三条装置侧约束属既有设计，本报告登记为「供后续使用者注意」，不代改契约。

## 9. 残余与建议（不代批）

1. **D-A 放行无 CLI 通道**：装置侧只能外部写 approval 记录（本次由演练脚本直接追加 JSON 行）。若要让「人在回路放行」可运维，需要一个受控写入口（例如 `goal_loop approve --scope irreversible --object I3 --decision approve --by user`）；建议立项前先确认是否与 contract.py 的写路径单一写者原则冲突（当前 approval 由「外部写入」成为第二个写者）。
2. **D-B 可用性**：本次演练为单进程串行、单 goal 单 root；未覆盖并发多写者、跨进程 resume 竞争等场景（契约层 selftest 已含多写者负控，但真机未演）。
3. **D-C 证据根耐久**：真机证据住仓外（`~/develop/herdr-drills`），与 R418 的克隆耐久议题同类；若要长期留证，需按 T0 备份面纳入。
