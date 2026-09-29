# R519：herdr-agents —— openrig 吸收项登记（DEFERRED D-25..D-31）+ 门禁自测接线

- 日期：2026-09-29
- 仓：`~/develop/herdr-agents`（已 push origin/main）；本报告为 GA 侧记录
- 本会话提交：`4b9d716`（DEFERRED 增 D-25..D-30 + 门禁自测锚点动态化，14+/3− 与 36+/7−）、`39bec0c`（run_tests 给 5 个门禁器补跑 `--selftest`，9+/1−）、`c727d28`（DEFERRED 增 D-31 + §4/§5 同步，6+/2−）

## 1. 起因

用户提问两件事：①`https://github.com/mvschwarz/openrig` 能不能参考；②「插件市场之类不做的东西，会不会在后面迭代计划里丢了」。第二问暴露一个真缺口：我们只有 R-01..R-14「残余登记」（已做但没做完），**没有一页集中写「刻意不做/主动延后」的台账**（含触发条件与复活判据）。

## 2. 交付一：openrig 对照研究入仓

落点 `docs/research/openrig/`：

| 文件 | 规模 | 说明 |
|---|---|---|
| `REPORT_OPENRIG_VS_US.md` | 10946 B | 对照报告（英文整句引文由脚本从 raw 逐字注入） |
| `raw/` | 26 篇 / 340 KB | 2026-09-29 抓取的 openrig 文档快照（main `5d3f62f17bce`，最近 tag `v0.5.9`） |
| `build_report.py` / `check_quotes_openrig.py` | — | 构建器 + 引文校验器（引文即契约，见 RULES#18） |

**判定：同底料、不同层 ⇒ 产品面不抄，纪律面抄。**
openrig 以 herdr 为宿主终端（本机 herdr 0.9.0 原语同源），但产出是「给人用的会话产品」（TS daemon + TUI + queue/mission）；我们是「给装置用的治理面」（角色语义/四轴投影/五级闸门/残余登记/夜批台账/裁判准入）。

### 可吸收 8 条（A1–A8，摘其原话要点）

| 编号 | 要点 | 对我们的价值 |
|---|---|---|
| A1 | 状态由**单一仲裁点**从分级、有时界的证据算出；三轴永不混合 | 治「同一事实在多个面各写一次」；可把 R478 从「文档状态块」扩到「运行态投影」 |
| A2 | `unknown` 是**一等公民**（oracle 说不出就说不知道） | 直补阶段四判据 2：夜间取不到装置事件应记 unknown，而非记 0 干预 |
| A3 | 派生诊断**读时算、不落盘** | 正是 R479/R480「幻影恢复」的根因修法 |
| A4 | 主动等待(HELD) 与意外停滞(PARKED) **分开** | 修 INCIDENT_2026-09-25「等人静默 6h26m」——我们装置里 HELD 无语义 |
| A5 | fixture 通过只算 **TRIAL**，生产一致窗口才升级；退役只认生产 | 最值钱：治「fixture 绿就当能力可用」 |
| A6 | 边界文档显式写「边界之外刻意不做什么」 | 即用户第二问的正解 ⇒ 已落成 `DEFERRED.md` |
| A7 | 只声明能证明的东西（自洽验证 ≠ 真实性证明） | 借措辞纪律给阶段五 attestation |
| A8 | 研究/评审的准入与成本刻度 | 部分已由 R487 覆盖 |

## 3. 交付二：`DEFERRED.md` 成为「刻意不做/延后」唯一登记处

本批新增 D-25..D-30 = openrig A1/A2/A4/A5/A7/A8（A3 已由 `alert_lifecycle` 跨轮稳定事件 ID 覆盖、A6 由本文件自身落地，故不重复立项）；D-31 为本会话现场摩擦登记：

- **D-31｜派生制品盖章的时机与归属策略（工程 / DEFERRED）**：`derive_guard` 的输入哈希按**工作区内容**算，且 `l1_index` 输入面含 `tools/*.py` 与 control-plane 测试文件 ⇒ 任何一处未提交输入都会让「重建 + 盖章」把**未定稿内容**冻进 `index.json`/`manifest.json`。当前只有「单机、多会话共享一个工作区」这一形态，先写死策略收益低于误伤在制改动的风险。
  - 触发：出现**第二个常态写者**（多人 / 多机 / CI 轮流重建盖章），或出现「盖章要排队等别人提交」。
  - 判据：`derive_guard check` 的 STALE 项须能**逐文件**点名差异输入以证明归属（现状只报「输入 N → N 个」不给文件名，归属靠人工比对 manifest）。
  - 同批同步 §4 范围（D-01..D-31）与 §5 分类计数（工程 9 / 共 31 行），并把旧变更记录里的字面旧计数改为「以门禁输出为准」——否则门禁 A2 的「声明面自洽」判据会因陈年计数假红。

## 4. 交付三：门禁自测接线（治「负控腐烂」）

- `39bec0c`：`run_tests.sh` 给 5 个门禁器补跑 `--selftest`（lint_shell_vars / doc_state_check / residual_registry_check / authority_boundary_check / deferred_registry_check）。**根因**：此前 CI 只跑门禁主体、不跑门禁自己的自测 ⇒ 负控烂了也没人知道。
- `4b9d716` 同笔修一个**真缺陷**：`deferred_registry_check` 的 NC16–NC18 负控锚点原写死建档时的字面计数，台账一长大即失配空转（自测仍报绿）⇒ 改为从当前台账真文动态推导。

## 5. 验证证据（全部现场复跑）

- 隔离 worktree（HEAD=`39bec0c`）全仓 `PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh` ⇒ `suites run: 35` + `ALL SUITES PASSED` + `drills: 8 passed, 0 failed`（日志 `/tmp/r517_run_tests.log` 224 行）。日志中 `lint_shell_vars` 报「1 处 FAIL」是**负控自测**（临时 bad.sh，期望 1 命中实得 1），非真失败。
- 主仓真机门禁（HEAD 收口后）：`derive_guard check` ⇒ `OK=4 MANUAL=2` rc=0（native_tree 120 文件逐字节一致 / l1_index 与 211 个输入均未变 / board_html 与 63 个输入均未变 / trust_gate_copies 2 份一致）；`doc_state_check` / `authority_boundary_check` / `residual_registry_check` / `deferred_registry_check` 全 rc=0（deferred 0 problem，selftest 19 passed）。
- 提交闭环：三笔提交均已在 origin/main（`git merge-base --is-ancestor c727d28 HEAD` ⇒ OK，`rev-list --left-right --count origin/main...HEAD` ⇒ 0/0）。

## 6. 铁律与坑（本轮实测）

1. **门禁的负控锚点禁写死数值**——写死必随被守护对象长大而静默空转（本轮即抓到一例）。
2. **CI 只跑主体、不跑 `--selftest`** = 负控腐烂的温床；门禁进 CI 必须连自测一起跑。
3. **全套件只在隔离 worktree 跑**（会写 `control-plane/*/runs/`，会污染主仓他会话在制改动）；**真机 `derive_guard check` 回主仓跑**（native README 内嵌 `repo: <绝对路径>` = 仓库局部口径，worktree 内必假 DRIFT）。
4. 提交姿势：`git add <单文件>` + `git commit -F msg`（**不带 pathspec**）；提交后核 `--numstat` 新增行数=预期（本次 6/2 逐字相符），防误带他会话在制改动。
5. 读 memory 侧长文件（INDEX.md/history.*）**必须用 Python `encoding="utf-8"` 显式读**，shell `head/cat` 经管道会出乱码假象。

## 7. 残余与未闭环

- 本会话在 herdr-agents 侧**无未闭环项**：原「l1_index STALE」残差在他会话重建 l1 后已转绿（现行 rc=0），D-31 政策保留为登记态。
- 未登记吸收项：openrig 其余条目（如 test-system 的 fixture/agreement 双档组织法）尚未逐条评估，如需吸收须新立 D 编号（`DEFERRED.md` 是唯一登记处）。
- 主仓仍有他会话 16 个未提交改动（本会话零触碰）。
