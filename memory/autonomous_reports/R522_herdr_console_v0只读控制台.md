# R522 herdr-agents 可视化收口：console_v0 一页看全的只读控制台

日期：2026-09-29 ｜ 仓库：`~/develop/herdr-agents`（可推送仓）｜ 提交：`588f654`（已 push `origin/main`）

## 0. 一句话

把 `control-plane/` 下 25 个子系统各自的 CLI 与台账，聚合成**一个只读视图**（9 面板 + problems 计数），
`scan / render / serve / selftest` 四子命令，**零写路径**；本批交付代码 + 规格 + 两处共享登记行，
全仓 38 套件与 8 项真机演练在孤本 worktree 复跑全绿。

## 1. 动机（用户原话方向）

用户提出：「**可视化上面，比如说配置、比如说看结果，做得是不是太少了，使用不方便呀**……
人家（openrig）是不是好多好多界面，不管是 CLI 还是 Web 页面」。
此前产品仓的能力散在 25 个子系统里：各自一个 CLI、各自一份台账，"现在跑到哪了 / 结果在哪 / 配置在哪"要记 20 多条命令。
本批是对该反馈的第一个回应：**读侧可视化**（不新增任何写路径、不改任何子系统行为）。

## 2. 交付清单（提交 588f654，6 文件 / 1292 插入 / 1 删除）

| 文件 | 规模 | sha256 前 16 | 说明 |
|---|---|---|---|
| `control-plane/console_v0/console.py` | 902 行 / 43476 B | `4c6e23a8fcfaf9cd` | 只读聚合器（四子命令 + 9 面板） |
| `control-plane/console_v0/test_console.py` | 261 行 / 11419 B | `7eb2f6eb72c7654f` | pytest 17 passed |
| `control-plane/console_v0/README.md` | 49 行 / 2827 B | `fe374df152d840cd` | 用法与边界 |
| `docs/product/F16_CONSOLE_SPEC.md` | 69 行 / 4309 B | `a79c50b7bf1418ad` | 规格（含 `## Authority boundary`） |
| `control-plane/README.md` | +1 行 | — | 子系统总表追加 console_v0 一行 |
| `control-plane/consumer_replay_v0/dispositions.json` | 10+/1− | — | 登记 console_v0 = DOC_ONLY + 改判触发点 |

（规模与 sha 由 `wc -c/-l` 与 `hashlib.sha256` 当场算出；上表数值即命令原始输出。）

## 3. 设计铁律（本批踩实，写进 F16）

1. **零写路径**：CLI 只有 `scan / render / serve / selftest`；`render --out` 默认写系统临时目录，
   **绝不写进仓库**（否则会污染 worktree 并触发 `derive_guard` 的 artifact 漂移）。
2. **`--gates` 默认关**：现场执行五项门禁会写 `control-plane/*/runs/`，故默认只报 `executed=false`，
   要现场跑必须显式加 `--gates`（对应「阶段门会写 runs/ ⇒ 只在孤本 worktree 跑」的既有铁律）。
3. **面板路径不写死**：一律用 `find_in_cp()` 按**文件名 glob** 定位子系统目录；
   若写死目录名，本文件会被 `consumer_replay` census 记成 `cost_quota_v0 / l1_map_v0` 等**别家的生产消费者**，
   从而**翻转它们已声明的 verdict**（把 DOC_ONLY 刷成 CONSUMED = 制造假消费者）。
4. **文档与实现对账**：README/规格里的面板名、计数、默认值必须回读 `scan` 实输出。
   本批据此修了两处文档漂移——README 的 `--refresh 15`（真实默认 10）、F16 §1 的面板表（与实现不符的旧面板名）。
5. **登记面同步**：新增目录必须同步 `dispositions.json`，否则 census 报 `UNDECLARED`（R510 已踩过）。

## 4. 面板面（`scan` 实测）

```
schema=herdr.console.snapshot/v1 generated=2026-09-29T17:35:29 head=588f654 …（截图口径：当次快照）
  runs           OK       {"count": 23, "missing_dirs": []}
  monitors       OK       {}
  alerts         MISSING  {"count": 0}
  goals          MISSING  {}
  unattended     OK       {"total_nights": 8}
  gates          OK       {"executed": false, "failing": []}
  config         OK       {}
  stage          OK       {}
  capabilities   OK       {"count": 26}
  problems       0
```

- 九面板 = `runs / monitors / alerts / goals / unattended / gates / config / stage / capabilities`，外加独立的 `problems` 计数。
- `alerts / goals` 显示 `MISSING` 属**如实呈现**（本机尚无 alerts 台账与 goal runs），不是故障。
- `render` 实测输出 `written: /tmp/r522_console.html (17084 bytes) problems=0`。

## 5. 验收证据（每条都可复跑）

| 项 | 命令 | 结果 |
|---|---|---|
| 自测 | `python3 control-plane/console_v0/console.py selftest` | `20/20 passed, 0 failed`（末行即原样输出） |
| 单测 | `python3 -m pytest control-plane/console_v0/test_console.py -q` | `17 passed in 1.23s` |
| 快照 | `console.py scan` | `problems 0`（见 §4 原文） |
| 渲染 | `console.py render --out /tmp/r522_console.html` | `17084 bytes`，`problems=0` |
| 登记面 | `consumer_replay_v0/replay.py census` | `declared=26 problems=0`（console_v0 → `DOC_ONLY prod=0 doc=3`） |
| 全仓套件 | 孤本 worktree：`PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh` | `suites run: 38` / `ALL SUITES PASSED` + `drills 8 passed / 0 failed` |
| 主仓五门禁 | `derive_guard check` / `doc_state_check --strict` / `authority_boundary_check` / `residual_registry_check` / `deferred_registry_check` | 全 `rc=0`（derive_guard `MANUAL=2 OK=4`；authority_boundary `specs=33 fails=0`；residual `rows=14`；deferred `0 problem(s)`） |

## 6. 口径与易混淆点

- **"25 个子系统"**：`control-plane/` 下现有 26 个目录（含 `console_v0` 自身），故README/提交信息里的 25 = 26 − 1（被聚合的那些）。判读时勿用 26 去否定 25。
- **census 两红是他会话 WIP**：主仓工作区里有另一会话未提交的 `control-plane/stage_gate_check.sh` 改动，
  它引用了 `l1_map_v0`，使 census 报 `l1_map_v0 MISMATCH / CATEGORY_VERDICT_CONFLICT`（problems=2）。
  用 `git show HEAD:control-plane/stage_gate_check.sh` 临时替换后复跑 ⇒ **`declared=26 problems=0`**；
  探针文件已逐字节还原（`restored bytes equal: True`）。即：**两红与本批零关系**，属 D-31 情形（他人未提交输入存在时不重建 l1、不盖章）。
- **DOC_ONLY 未刷绿（有意为之）**：console 是给人看的读侧视图，仓内没有任何代码路径调用它。
  登记条目写明了「改判触发点 = 出现脚本/服务调用 console.py 产出视图（如 CI 阶段报告）」，
  届时 category 改 WIRED 并更新条目。
- **未盖章**：`derive_guard check` 在本批改动后仍 `rc=0`（`l1_index` 报「产物与 211 个输入均未变」），
  说明 `console_v0/` 与 `F16` **不在 l1 的输入集**，因此本批**不需要重建 l1 / 重新盖章**。

## 7. 提交与推送

- 提交：`588f654` — `console_v0: 一页看全的只读控制台（聚合 25 个子系统，零写路径）`，6 文件 / 1292 插入 / 1 删除。
- 推送：`8d2072a..588f654 main -> main`（`rev-list --left-right --count origin/main...HEAD` = `0 0`）。
- 共享工作区纪律：只 `git add` 本批 6 个路径，提交时**不带 pathspec**；
  他会话的 16 个在制文件（`.gitignore / AGENTS.md / CHANGELOG.md / ROADMAP_MASTER.md / herdr-handoff/*` 等）逐字保留未提交。

## 8. 残余与未做（诚实登记）

1. **`serve` 未接 launchd**：需人工起（`python3 console.py serve`），本批只保证可绑定空闲端口（selftest NC7），未做常驻与开机自启。
2. **无视觉验证证据**：`render` 只核了字节数与 `problems=0`，**没有**浏览器打开截图/OCR 证据；
   若要声称「界面好看/可用」，需补一次真实浏览器打开（下一步可做）。
3. **写侧与配置面未做**：本批刻意只做读侧聚合；"改配置 / 触发动作"这类写侧面板仍属未开工（写侧须过 `real_cli_gate` 双门与留痕）。
4. **阶段五/插件市场类界面**仍在 `FUTURE_CAPABILITIES` 与 `DEFERRED.md` 里，未纳入本批。

## 9. 本批可复用的两条经验

- **「新增一个读侧视图」也要登记消费者面**：只要新目录被 census 扫描到，就必须在 `dispositions.json` 声明，
  否则 CI 里的 `consumer_replay` 套件直接红（R510 已因此失败过一次）。
- **文档里的默认值/面板名必须回读实现**（本批 README 的 `--refresh 15` 与 F16 §1 面板表两处漂移），
  这正是 RULES#21「实测数值回归命令」在**产品文档**上的同一条纪律。
