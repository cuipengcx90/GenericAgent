# R474 F9 告警清单消费者与 ingest 幂等（TODO L184 / 第21批 C6）

> 本报告内的数值均为 2026-09-28 当场复跑所得；herdr-agents 侧任何交付物在本次收尾中**未被修改**。

## 0. 一句话

把 2026-09-25「等人静默 6h26m」事故善后里**缺的那一环**补上：`--alerts-out` 产出的清单过去没有自动消费者
（告警写进文件却没人看，等于回到事故前的形态），而 `ingest` 仍是人工步骤。本批给出**确定性的消费者**
（`alert_pipe.py pipe` + 看板警示区）与**幂等 ingest**，并把「缺口本身」做成可机械检出的判据。

## 1. 来源与验收

- 任务行：`temp/TODO.txt` L184（第21批三路评审 codex/槽3/槽4 = 9 / 8.5 / 6 = **7.83**，本批最高）
- 上游文档：`herdr-agents:docs/product/FUTURE_CAPABILITIES.md` §10「把人叫回来」的闭环补齐
- 验收要求（逐条对账见 §4/§5）：
  1. 消费者必须是**确定性代码路径**（禁 LLM 参与判定）；
  2. `ingest` 从人工步骤变成**幂等命令**；
  3. 缺口本身**可被机械检出**。

## 2. 交付物（提交 `d106f34`，已 push，= `origin/main`）

| 文件 | 行数 | 字节 | sha256[:16] |
|---|---|---|---|
| `control-plane/autonomy_v0/alert_pipe.py` | 414 | 20309 | 40a54404597612e1 |
| `control-plane/autonomy_v0/test_alert_pipe.py` | 265 | 11930 | a18480b88e47fc3c |
| `control-plane/autonomy_v0/board.py`（含 `--alerts-in/--alert-ledger`） | 523 | 28780 | e890bb9ebf8eeccd |
| `control-plane/autonomy_v0/ingest_device.py`（幂等化） | 436 | 20411 | fa6b9a7243c270ba |
| `control-plane/autonomy_v0/test_ingest_device.py` | 475 | 24592 | 5df3e3d1363b80ef |
| `control-plane/autonomy_v0/contract.py` | 534 | 27018 | c9a20f2c787e0f21 |
| `docs/product/F9_ALERT_CONSUMER_SPEC.md` | 178 | 10864 | 9459c3b37bb15d42 |
| `docs/product/FUTURE_CAPABILITIES.md`（§10 收窄） | 297 | 24219 | 90240a711f8a2e34 |

提交口径：`git show --stat d106f34` = **11 files changed, 1223 insertions(+), 89 deletions(-)**；
`git rev-parse origin/main` = `d106f342cbb71bde1b3e6b7398b91eff194da937`。
（其后仓库本地 HEAD 是**他会话**未推提交 `017fc58`「怎么测：单入口分层复跑 + 演练进 CI + 干净克隆可机器复跑」，
与本批无关，本次未触碰、未推送。）

## 3. 契约要点

### 3.1 `alert_pipe.py pipe` —— 清单的确定性消费者

- 输入 = `board.py --alerts-out` 产出的 `ga.board-alerts/v1` 清单；输出 = **append-only JSONL 台账**。
- `alert_id` 由记录派生（同一条告警重跑**不重号**）⇒ 第二轮 `new=0 / repeated=1`。
- **通道缺失不静默**：无通知命令时 `notify_unavailable=true`、`notified=0`，但动作**照落账**并写明
  `NOTIFY_UNAVAILABLE (no notify command configured: set TEAM_DRIVE_NOTIFY_CMD / HERDR_TEAM_NOTIFY_CMD / AUTOPILOT_NOTIFY_CMD, or pass --notify-cmd)`。
- 子命令：`pipe` / `list` / `selftest`。

### 3.2 看板警示区（`board.py` 的第二半）

- `board.py --alerts-in <alerts.json> --alert-ledger <led.jsonl>` 渲染警示区；不带则完全无警示区。
- 导入 `alert_pipe` 失败 ⇒ 渲染「告警清单消费者不可用」；清单不合格 ⇒ 渲染「告警清单不合格（未渲染）」。
  **两种失败都显式呈现，绝不假装消费过**。

### 3.3 `ingest` 幂等

- `ingest_device.py` 按声明幂等落盘：重复 ingest 不产生重复记录（非推进事件旧 phase 按 backward 忽略、不整条作废）。

### 3.4 FUTURE §10 收窄

`docs/product/FUTURE_CAPABILITIES.md` §10 的 diff（`git diff d106f34^ d106f34 -- docs/product/FUTURE_CAPABILITIES.md`）
把「清单**没有**自动消费者、`ingest` 仍是人工步骤」改为「清单**已有**自动消费者（`alert_pipe.py` + 看板警示区 +
`ingest` 按声明幂等落盘）」；**仍未做**=`定期触发与通道策略`（明示归外层调度）。

## 4. 端到端 A/B 复跑（本会话实测）

造一条 `ESCALATED` 记录（`state=ESCALATED` + `history[-1].ts` 早于 `now - 2h`，阈值 `user_gate_wait=2` 小时），
指定 `--now` 固定时间，逐步骤观测：

| 步骤 | 关键输出 |
|---|---|
| `board.py --runs <tmp>/runs --alerts-out <tmp>/alerts.json --now 1758800000` | 清单 1 条：`lane=WaitingOnUser`、`reason="等人 > 2h"`、`age_s=100000.0` |
| `alert_pipe.py pipe --alerts … --ledger led.jsonl --now 1758800000 --json`（第 1 次） | `items_total=1 new=1 repeated=0 notified=0 notify_unavailable=true`；台账 365 B |
| 同上（第 2 次） | `items_total=1 new=0 repeated=1 notified=0`、`actions=[]`；台账 **365 B（字节不变）** |
| `board.py --runs … --out b1.html`（不带清单） | 告警清单标记出现 **0** 次 |
| `board.py --runs … --alerts-in … --alert-ledger … --out b2.html` | 告警清单标记出现 **1** 次，且含「已消费」 |

判定：①②③ 三条验收要求全部落地——动作是纯代码路径；同一输入重跑**幂等**（台账字节不变、动作不重复）；
无通道时**仍留痕**（可机械检出「本应通知但没通道」）。

## 5. 测试与门禁

- `alert_pipe.py --selftest` ⇒ **23/23**。
- `pytest control-plane/autonomy_v0/test_alert_pipe.py` ⇒ **17 passed**；
  `test_ingest_device.py` ⇒ **38 passed**（autonomy_v0 全套 186 passed + 5 subtests）。
- `./run_tests.sh`（后台跑，`.exit=0`）⇒ 尾部 `suites run: 26` / **`ALL SUITES PASSED`**；
  其中 `autopilot/bin` = 144 passed + 12 subtests；`doc_state_check`/`residual_registry_check`/
  `authority_boundary_check`/shell lint 全 `fails=0`。
- `derive_guard.py check` ⇒ rc=0（`board.html` 与 manifest 的 `artifact_sha` 一致）。

## 6. 事故与处置：`board.py` 默认 `--out` 会覆盖**受跟踪**产物

**现象**：为做 A/B 演示跑 `board.py --runs /tmp/.../runs --alerts-out /tmp/.../alerts.json --now …`（**未给 `--out`**）
⇒ 默认 `--out` 落在仓库内 `control-plane/autonomy_v0/board.html`，把已提交的渲染结果换成 1 条记录的演示版
（`git diff --stat` = **29 insertions / 505 deletions**）。

**为何判定可无损复原**：`git rev-parse HEAD:control-plane/autonomy_v0/board.html` = `20d52b1cadb95be3660e9a1a6bbaf70e66a14723`；
`derive_guard_v0/manifest.json` 的 `targets.board_html.artifact_sha` = `9eb46c26…`；
**sha256(HEAD 版) == 9eb46c26…（manifest 值）** ⇒ 覆写前的工作区内容就是 HEAD 内容，没有他会话未提交改动被牵连。

**处置**：`git checkout HEAD -- control-plane/autonomy_v0/board.html`；复核 sha256 = `9eb46c26…`、
`git diff --stat -- <该文件>` 为空、`git status` 不再列出该文件。

**教训（已登记 TODO）**：任何**实验性**跑渲染器都必须显式 `--out` 到临时路径；`board.html` 是
`derive_guard` 的 inputhash target，被改即报 `RESYNC`，会误伤门禁。

## 7. 边界（**不做**的，别顺手扩）

按 `F9_ALERT_CONSUMER_SPEC.md` §7：本仓**不内建 push**（消费者只产出清单/台账，真实推送仍交外部通知器）；
不替外层做定期触发；不改 alert 语义（`lanes.is_overdue` 的阈值仍由 `board_policy` 决定）。

## Authority boundary

- **语义 owner**：告警清单消费语义与 ingest 语义 = `herdr-agents:control-plane/autonomy_v0/`
  （`alert_pipe.py` / `board.py` / `ingest_device.py`）与其规格 `docs/product/F9_ALERT_CONSUMER_SPEC.md`。
- **验证提交 owner**：本报告（GA 侧 `memory/autonomous_reports/R474_*.md`）只做**只读复跑与引用**，
  不修改、不代提交 herdr-agents 的任何文件；本批唯一代码提交 `d106f34` 由 herdr-agents 仓侧完成并已 push。

## 8. 复跑（照抄即得同结论）

```bash
cd ~/develop/herdr-agents/control-plane/autonomy_v0
python3 alert_pipe.py --selftest
/opt/anaconda3/bin/python3 -m pytest test_alert_pipe.py test_ingest_device.py -q
cd ~/develop/herdr-agents && ./run_tests.sh
python3 control-plane/derive_guard_v0/derive_guard.py check
```

端到端 A/B 见 `docs/product/F9_ALERT_CONSUMER_SPEC.md` 末节「复跑」与本文 §4 的步骤表。
**注意**：复跑渲染时必须给 `--out` 指向临时路径（见 §6）。

## 9. 残余与后续

- **未做**：告警的定期触发与通道策略（FUTURE §10 明示归外层调度）。
- **新增待办**：给 `board.py` 加护栏——当 `--runs` 指向仓外且未显式给 `--out` 时警告或拒绝，避免实验误覆盖受跟踪产物（§6）。
- 本批未触碰他会话提交 `017fc58`。收尾复核（2026-09-28 15:0x）时该提交已由他会话推送，
  `origin/main` 已前进至 `d9a16b9`；§2 中 `git rev-parse origin/main` = `d106f34` 为**交付时点**口径。
