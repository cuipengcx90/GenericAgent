# R524 herdr-agents 可视化两批交付（1-A/1-B/2-C）+ 写侧最小实现

- 日期：2026-09-29
- 仓：`~/develop/herdr-agents`（GA 侧记 R524；GA 仓仅记录报告与索引）
- 用户批（原话要点）：**①分 2 批执行 ②执行完不要执行到一半 ③完成后进行 1 轮评审 ④最终整体汇报 roadmap 与当前架构阶段**
- 批次划分（按用户批准的 A/B/C 建议）：
  - **批次一** = A（看得见的现状）+ B（能常驻）⇒ 已交付
  - **批次二** = C（能操作）⇒ 本轮交付

---

## 0. 一句话结论

三笔提交（`f63afad` / `c3645ec` / `98e5d43`）已入 `herdr-agents` main；**只读台面 → 自我刷新 → 常驻服务 → 最小写侧**四段闭环，
写侧四道闸门（一次性令牌 / 动作白名单 / 落点仓外 / 留痕）在**真 HTTP 会话**里逐条取证通过，
隔离 worktree 全仓 **38 套件 ALL SUITES PASSED + drills 8/8**，主仓门禁 rc=0（两项红门禁的冲突源经定位属**他会话在制改动**，非本件缺陷，见 §7.3）。

---

## 1. 批次一：A + B（已交付，本报告只作回执）

### 1-A `f63afad`（看得见现状）

| 变更 | 内容 |
| --- | --- |
| 顶部横幅 | 控制面顶部显示当前状态与提示，避免「看到一页空白/旧图以为没在跑」 |
| MISSING 说明 | 面板缺数据时显式说明「缺什么、为什么、怎么办」，不再静默留白 |
| `/board` 透传 | console 可直接透传团队看板页，不必再去记路径 |

### 1-B `c3645ec`（能常驻）

新增 `control-plane/console_v0/launchd/` 三件（模板 + 安装器 + 测试），把 console 做成 **launchd 常驻服务**：

| 文件 | 字节 | 行数 | sha256 前 16 |
| --- | --- | --- | --- |
| `control-plane/console_v0/launchd/install.sh` | 3985 | 98 | `2aa5da3dcc22761a` |
| `control-plane/console_v0/launchd/com.herdr.console.plist.template` | 944 | 32 | `2f21c7d183346a84` |
| `control-plane/console_v0/launchd/test_console_launchd.py` | 4781 | 114 | `c936dfab1d90fae8` |

真机已装上并核验（`launchctl list` 有项、端口可访问），属「本机运行态」，不写进仓内产物。

---

## 2. 批次二（C）：最小写侧

提交 `98e5d43`：`console_v0 批次2-C：最小写侧（一次性令牌 + 动作白名单 + 落点仓外）`，5 文件 **514+/6−**。

| 文件 | 字节 | 行数 | sha256 前 16 | 角色 |
| --- | --- | --- | --- | --- |
| `control-plane/console_v0/write_gate.py` | 11966 | 261 | `95d9402cb41cbefe` | 写侧闸门**单一源**（新增） |
| `control-plane/console_v0/console.py` | 53339 | 1071 | `651bcaceafa7200d` | 接线：仅 `serve` 加 `--write/--write-dir` |
| `control-plane/console_v0/test_write_gate.py` | 5482 | 141 | `81a87ac54e982886` | 写侧 pytest（新增） |
| `docs/product/F16_CONSOLE_SPEC.md` | 10917 | 154 | `2c3b7e43cbf962ad` | 规格补 §8 写侧（38 行） |
| `control-plane/console_v0/README.md` | 4144 | 71 | `8b869fbd6ff2f0f4` | 跑法一行（真实计数） |

其余组件（批次一产物，本次一并复测）：

| 文件 | 字节 | 行数 | sha256 前 16 | 自测 |
| --- | --- | --- | --- | --- |
| `control-plane/console_v0/test_console.py` | 14063 | 315 | `2cb195c483b59b13` | pytest 19 |

**测试计数（现场复测，禁凭印象）**

```
python3 control-plane/console_v0/write_gate.py selftest   # 19/19（含负控）
python3 -m pytest -q control-plane/console_v0/test_write_gate.py      # 11 passed
python3 -m pytest -q control-plane/console_v0/test_console.py         # 17 passed
python3 -m pytest -q control-plane/console_v0/launchd/test_console_launchd.py  # 11 passed
python3 control-plane/console_v0/console.py selftest      # 27/27
```

---

## 3. 写侧安全模型（四道闸门，全部由 `write_gate.py` 单一源实现）

1. **一次性令牌**：`--write-dir/token`，权限 `0600`，32 字符随机串（实测文件 33 B = 32 + 换行）。
   请求须带 `X-Console-Token`；无/错令牌一律 **403 且不执行动作**；成功执行后**立即轮换**并写回同一文件；旧令牌重放 ⇒ 403。
   令牌值**绝不进响应体**（`/write-status` 只报 `enabled`/动作清单/文件路径）。
2. **动作白名单**：只有注册表内的名字可执行（现役 `render` / `scan`），未知动作 ⇒ **404**。
3. **落点仓外**：产物只能写 `--write-dir` 内；请求体必须为空（非空 ⇒ **400** `body_not_empty`，杜绝「带参数绕过白名单」）。
   落点护栏实现（`write_gate.py:55-60`）：`real(path).startswith(real(tempfile.gettempdir()) + os.sep)` 才放行。
   ⇒ **实测坑**：`--write-dir /tmp/...` 会被拒（macOS 上 `$TMPDIR` 是 `/var/folders/...`，`/private/tmp` 不在其下），报
   `ValueError: run_dir 必须在系统临时目录内：/private/tmp/r524_e2e/run`。这是护栏按设计生效，不是缺陷。
4. **留痕**：每次请求（**含拒绝**）追加一行到 `--write-dir/actions.jsonl`。

写侧**不做**的事（边界，属后续批次）：不写任何受跟踪文件、不触发 git 提交、不代跑上游写命令。
即「从界面点一下 ≡ 跑一条只读 CLI」的最小通路；真正的写面（改配置、推进状态）仍走各自 CLI 与闸门。

---

## 4. 真 HTTP 端到端取证（2026-09-29，端口 8791，`--write-dir <$TMPDIR>/r524_e2e/run`）

| 探测 | 实测结果 |
| --- | --- |
| T0 `GET /write-status` | 200，`enabled=true`，`actions=[render, scan]`，`token_file` 指向 0600 文件，响应体无令牌 |
| T1 无令牌 POST | **403** `bad_token`（动作未执行） |
| T2 错令牌 POST | **403** `bad_token` |
| T3 带非空 body | **400** `body_not_empty` |
| T4 未知动作 `/action/nope` | **404** `unknown_action` |
| T5 `scan` | **200**，0.2 s，`bytes=21747`，`artifact_sha256_12=d4b7d397f1cd` |
| T5a 产物核对 | 盘上 `scan.json` 存在，`size=21747`，`sha256=d4b7d397f1cd89ec` ⇒ **声明值 == 实际文件** |
| T6 旧令牌重放 | **403** `bad_token` |
| T7 令牌轮换 | `rotated=True`（响应内新令牌 ≠ 旧令牌，令牌文件已同步） |
| T8 `render`（用新令牌） | **200**，`bytes=18141`，`artifact_sha256_12=ff29924b0451` |
| T11 仓库洁净 | E2E 前后仓内文件指纹一致（`True`）；`scan.json`/`console.html` **只落在 run_dir** |
| T12 render 产物 | 盘上 `console.html` 存在，`size=18141` |

留痕（`actions.jsonl`，事后复测）：**7 行 / 1115 B / sha256 前 12 `4d99d13b991c`** = 2 条 `ACCEPT`（scan/render）+ 5 条 `REJECT`
（`BAD_TOKEN`×3、`BODY_NOT_EMPTY`×1、`UNKNOWN_ACTION`×1）⇒ **拒绝也留痕**这条判据有实证。

run_dir 实测内容：`['actions.jsonl', 'console.html', 'scan.json', 'token']`；`token` 33 B / `0o600`。

**原始取证件**：`temp/console_notes/e2e_write_gate.txt`（16 行逐条输出，本报告 T0–T12 均取自该文件）。
文档内表（`F16_CONSOLE_SPEC.md` §8）数值取自**首轮**真跑（`scan.json` 21750 B / sha12 `41b479ff460e`）；
本报告为**复跑**值。二者都对 —— 产物字节数随当轮内容浮动，**关键判据是「响应声明 sha256 == 盘上文件 sha256」**，两次均命中。

**复跑命令**

```
python3 control-plane/console_v0/console.py serve --write --write-dir "$TMPDIR/r524_e2e/run"
# 另开一个 shell：
cat "$TMPDIR/r524_e2e/run/token"     # 取一次性令牌
curl -s -X POST -H "X-Console-Token: $(cat $TMPDIR/r524_e2e/run/token)" http://127.0.0.1:8791/action/scan
```

---

## 5. 测试与门禁

| 项 | 结果 |
| --- | --- |
| `write_gate.py selftest` | 19/19（含负控） |
| `console.py selftest` | 27/27 |
| 三份 pytest（console / write_gate / launchd） | **19 + 11 + 9 = 39 passed**（`python3 -m pytest -q control-plane/console_v0/` 递归含 `launchd/` 子目录 ⇒ 39 passed in 3.94s） |
| 全仓套件（隔离 worktree） | `suites run: 38` ⇒ **ALL SUITES PASSED** |
| 真机演练 | `drills 8 passed, 0 failed` |
| `derive_guard check`（主仓真身） | **rc=0**（OK4 / MANUAL2） |
| `doc_state_check` / `authority_boundary_check` / `residual_registry_check` | 全 **rc=0** |
| `consumer_replay census` / `deferred_registry_check` | rc=2 / rc=1 —— **冲突源为他会话在制改动**（见下） |

**套件与真机门禁的分工（沿用 R460/R485/R496 铁律）**：阶段门与会写 `control-plane/*/runs/` 的套件一律在**孤本 worktree** 跑；
native README 内嵌 `repo: <绝对路径>` = 仓库局部口径 ⇒ worktree 内必报假 DRIFT，故「**套件在 worktree、真机 check 回主仓**」。

### 5.1 两项红门禁的归属（已定位，非本件缺陷）

- `census rc=2`：报 `l1_map_v0` 的消费者冲突 —— 触发者是工作区**未提交**的 `control-plane/stage_gate_check.sh` 与 `AGENTS.md`（他会话在制），
  干净 HEAD 上为 0。
- `deferred_registry_check rc=1`：`FUTURE_CAPABILITIES.md` 新增 §16 未登记 —— 该文件同为他会话在制改动。
- 本会话 5 个文件在上述两器语料里的命中数为 0（逐文件核过）。

---

## 6. 1 轮独立评审

**评审形态**：只读 1 轮，后台 `codex CLI`（`temp/r523/run_codex_review.sh`）；原始输出 `temp/r523/codex_review.out.txt`（408755B），
成稿 `temp/r523/codex_review.body.md`（18042B / 292 行）；评审件已入仓 `docs/reviews/CONSOLE_V0_WRITE_SIDE_REVIEW.md`。
**耗时口径**：约 16 分钟 / 18 次工具调用（历史评审基线 2–5 分钟）——差异来自评审**真去复现**（见下表「复现」列），非空转。

**判决：REJECT —— 3 条阻断 + 4 条非阻断。**

### 6.1 阻断项逐条处置

| # | 判词 | 评审复现 | 处置 | 修复后证据 |
| --- | --- | --- | --- | --- |
| B1 | `scan --json-out` 绕过写侧闸门，可写任意路径（不经 `WriteGate`/令牌/`run_dir` 边界/ledger） | 替换 `tempfile.gettempdir` 后 `scan --json-out /dev/null` ⇒ 输出 `written: /dev/null`、`rc=0` | 新增 **落点护栏** `out_of_bounds()` 单一源：默认只允许系统临时目录；越界 `rc=2` 且**不落盘**；确需越界须显式 `--allow-out-of-bounds` | 复现脚本现被拒 `rc=2`（`/dev/null` 与仓内路径两例全拒） |
| B2 | 一次性令牌**并发双用**（比对与轮换无锁） | 双线程 barrier 探针 ⇒ `results=[True,True]` | `WriteGate` 加**进程内锁**（比对+轮换+留痕临界区）；并发第二请求 `403` 且留痕 | **真 HTTP** 并发同令牌 ⇒ `[200, 403]`，ledger 记 `[[REJECT][ACCEPT/pre-exec][RESULT/result]]` |
| B3 | `launchd/install.sh` 的 label 可路径穿越 ⇒ `rm -f` 打到 `$HOME` 下任意文件 | `HERDR_CONSOLE_LABEL='../../.ssh/config' … --uninstall` ⇒ `DEST` 归一化后指向 `$HOME/.ssh/config` | label **白名单校验**（只允许 `com.herdr.*` 形态、拒 `/` 与 `..`）+ `DEST` 必须落在 `$HOME/Library/LaunchAgents/` 内 | 复现命令现被拒（写入前退出，零副作用）；新增 pytest 负控 |

### 6.2 非阻断项处置

- **C1 ledger 非可信 append-only**（无 fsync / 无哈希链 / 「产物已建、留痕未写」有漏记窗口）⇒ **部分修**：改为
  **动作前先写 pre-exec 记录、结果再写 result 记录**（漏记窗口从「整个动作」收窄到「两次写之间」）、ledger 文件 `0600`。
  **未做的部分**（诚实登记）：无 fsync、无哈希链；本轮**未做**插桩杀进程验证。⇒ 落入 §7 残余面。
- **C2 令牌响应语义与规格矛盾**（`write_gate.py` 返回新令牌 vs F16 写「令牌值绝不进响应体」）⇒ **改规格口径**：
  F16 明确两处白名单 = 令牌文件 + **轮换响应体字段**，其余响应体一律不含令牌值。
- **C3 非法 `Content-Length` 无留痕**（`int()` 在 `gate.request()` 之前）⇒ 改为**留痕后 400**（`bad_content_length`）。
- **C4 新安全边界无负控** ⇒ 本轮补齐：并发双用 / `--json-out` 越界 / 恶意 label 穿越 / 坏 `Content-Length` 全有负控用例。
- 附带（评审指出）：`console.py` 取 `board.html` 硬编码 `autonomy_v0`（与自家「面板路径用 `find_in_cp()` glob 定位」铁律冲突）⇒ 已改 **glob 定位**。

### 6.3 修复后回归（同一轮内完成）

```
write_gate.py selftest        22/22   （修复前 19/19；该器无汇总行，口径=数 `PASS ` 行、FAIL 行 0）
console.py   selftest        29/29   （修复前 27/27；新增 NC11/NC12 两条负控 ⇒ 27→29）
pytest -q control-plane/console_v0/   43 passed（修复前 39）
```

读侧无回归：修复后 `GET /` 逐字节未变（`board.html` sha256 前 16 = `9eb46c2672c470db`，与批次 2-C 时点一致）。
修复提交 `175bd5f`（7 文件 244+/27−），已 push；全仓套件复跑结果见 §5。

---

## 7. 残余与边界（诚实登记）

| # | 项 | 现状 | 触发时怎么发现 |
| --- | --- | --- | --- |
| R1 | 写侧只有 `render`/`scan` 两个动作 | 有意的**最小**面 | 想看还能点啥 ⇒ `GET /write-status` 的 `actions` |
| R2 | 写侧**不能**改配置/推进状态 | 边界内 | 同上，白名单里没有即不存在 |
| R3 | 令牌无 TTL（只在用后轮换） | 有意的 | 长期未用也不失效 ⇒ 若需 TTL 属新需求 |
| R4 | console 常驻服务属**本机运行态** | 不入仓 | 换机后需重跑 `launchd/install.sh` |
| R5 | `/tmp` 作 `--write-dir` 会被拒 | 护栏设计 | 报 `run_dir 必须在系统临时目录内` |
| R6 | 两项门禁在**含在制改动的工作区**里红 | 非本件 | 干净 HEAD 复跑即 0（§5.1） |

---

## 8. 阶段与路线图现状（单一源，不走手写）

状态唯一源 = `docs/product/ROADMAP_MASTER.md` §7.1，经 `tools/doc_state_render.py` 渲染进 `ROOT_MAP.md`（手改即 DRIFT）：

- **阶段一（裸跑）**：✅ 已达成
- **阶段二（可管）**：✅ 已达成（2026-09-26 阶段门 `stage_gate_check.sh` = `steps: 22 FAIL: 0`，无 SKIP）
- **阶段三（可扩）**：✅ 已达成（2026-09-28 `stage3_gate_check.sh` = `steps: 12 FAIL: 0`；验收 1 真机证据根在仓外 ⇒ 干净 clone 降级 `SKIP-EXT`，登记 `R-11`）
- **阶段四（目标驱动）**：🔶 部分落地（6 工作项中 4 条已有执行面：F8 夜批台账 / F12 成本·配额闸门 / F11 目标驱动循环 / E7 KPI；2 条未开工）；**阶段门未过**，逐条判据见 `STAGE4_WORK_ITEM_LEDGER.md` §2
- **阶段五（对外背书）**：⬜ 未开工（仅立项 `STAGE5_CHARTER.md` + 准入准备件 `attestation_v0`，登记 `DOC_ONLY`）

本次可视化两批落在**阶段四的「可用性」侧面**（不是新阶段），产物登记：`F16_CONSOLE_SPEC.md`（含 Authority boundary 段，入 authority_boundary 门禁语料）。

---

## 9. 复跑清单（一句话全量）

```
cd ~/develop/herdr-agents
python3 control-plane/console_v0/write_gate.py selftest              # 19/19
python3 control-plane/console_v0/console.py selftest                 # 27/27
python3 -m pytest -q control-plane/console_v0/                      # 39 passed（=test_console 19 + test_write_gate 11 + launchd 9）
PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh                  # 38 套件 ALL SUITES PASSED（建议在孤本 worktree 跑）
python3 tools/drill_suite_herdr.sh                                   # drills 8/8
python3 control-plane/derive_guard_v0/derive_guard.py check          # 主仓跑，rc=0
```

### §6.4 评审后的顺手真缺陷修复（flag 正名，提交 959350f）

- **缺陷**：越界护栏的放行开关字面为 `--allow-out-outside`（形如笔误，读作 "out-outside"），用户可见且写进 F16 规格。
- **修法**：正名为 `--allow-outside-tmp`，同步 4 处（docstring / `add_argument` / 两条 rc=2 提示）+ 规格 2 处；**属性名同步**（`a.allow_out_outside` → `a.allow_outside_tmp`，2 处）——首跑漏了属性名，pytest 立刻 4 failed（`AttributeError: 'Namespace' object has no attribute 'allow_out_outside'`），补改后复跑全绿。
- **行为零变化**：默认仍只允许系统临时目录；语义、rc、留痕不变，纯命名。
- **覆盖边界声明**：该正名提交在隔离 worktree 全仓套件**之后**产生（套件锚在 175bd5f），故全仓 38 套件结论不覆盖它；它的覆盖证据=本机 `console.py selftest 29/29` + `pytest -q control-plane/console_v0/ 43 passed`。
- **自测口径（实测原始输出）**：
  - `python3 control-plane/console_v0/write_gate.py selftest` ⇒ 22 行 `PASS` / 0 行 `FAIL`（该器无汇总行，禁按"29/29"式表述复述）
  - `python3 control-plane/console_v0/console.py selftest` ⇒ `selftest: 29/29 passed, 0 failed`
  - `python3 -m pytest -q control-plane/console_v0/` ⇒ `43 passed in 3.98s`

---

## §7 全仓回归（隔离 worktree，锚 175bd5f）

- **命令**：`PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh`（cwd = 隔离 worktree `~/develop/herdr-drills/r523_verify_20260929`，HEAD `175bd5f`）
- **原始输出（末三行）**：

```
---
suites run: 38
ALL SUITES PASSED
```

- **同批演练**：`tools/drill_suite.sh` ⇒ `drills: 8 passed, 0 failed`（contract/m4/readiness/skip/trust/kpi/apcontract/a4）
- **本次新增/变更套件的自证行**：`=== control-plane/console_v0 ===` ⇒ `43 passed in 4.16s`（pytest）
- 套件清单（38）：autopilot/bin、herdr-team/bin、tools/bin、acceptance_v0、acceptance_v1、attestation_v0、autonomy_v0、**console_v0**、consumer_replay_v0、cost_quota_v0、derive_guard_v0、exec_runner_v0、goal_v0、inbox_v0、kpi_report_v0、l1_map_v0、l2_v0、l3_skills_v0、monitor_v0、objection_v0、quota_exit_v0、readiness_v0、skip_ledger_v0、snapshot_v0、stage3_v0、team_harness_v0、trust_v0、unattended_v0、lint_shell_vars、doc_state_check、doc_state_render、residual_registry_check、authority_boundary_check、deferred_registry_check、indirect_sites_report、evidence_roots、gate_claim_replay、drill_suite
- **覆盖边界（必须声明）**：本轮 flag 正名提交 `959350f` 产生于套件**之后**（套件锚 175bd5f）⇒ 38 套件结论不覆盖它；其覆盖证据 = 本机 `console.py selftest 29/29`、`write_gate.py selftest` 22 PASS/0 FAIL、`pytest -q control-plane/console_v0/` 43 passed。

## §8 Roadmap 与当前阶段（单一源口径）

> 状态唯一源 = `docs/product/ROADMAP_MASTER.md` §7.1（由 `tools/doc_state_render.py` 生成进 `ROOT_MAP.md`）；工作项单一源 = `docs/product/STAGE4_WORK_ITEM_LEDGER.md`。

| 阶段 | 状态 | 收敛口径（2026-09-29 快照） |
|---|---|---|
| 阶段一 裸跑 | ✅ 已达成 | §2 门判定 + §2.1 新口径复核 |
| 阶段二 可管 | ✅ 已达成 | `bash control-plane/stage_gate_check.sh` = `steps: 22  FAIL: 0`（无 SKIP）；13 条验收全仓机械复跑绿 |
| 阶段三 多工作区 | ✅ 已达成 | `bash control-plane/stage3_gate_check.sh` = `steps: 12  FAIL: 0`；工作项 3/4 为 🔶（残余 R-12/R-13）；验收 1 真机证据根在仓外 ⇒ 干净 clone 下 `SKIP-EXT`（R-11） |
| 阶段四 夜里自己跑 | 🔶 部分落地 | 6 条工作项：4 条有执行面（W4-01 夜批台账 F8 / W4-02 成本·配额闸门 F12 / W4-03 目标驱动循环 F11 / W4-05 KPI 报表 E7），2 条未开工（W4-04 自动沉淀、W4-06 hard lease——须先有真并发冲突证据）。**阶段门 4 条判据均未达标**：判据 1 链长 6<7（`streak --profile strict` verdict=RATE_BELOW_MIN）、判据 2 逐夜聚合 `rc=1`（人工 1 / 自动 5）、判据 3/4 见台账 §2。残余 R-11/R-12/R-14 |
| 阶段五 对外交付 | ⬜ 未开工 | 仅立项（`STAGE5_CHARTER.md`）+ 准入准备件（`attestation_v0`，规格 F14，零生产接线，census 登记 `DOC_ONLY`） |

**本轮在整条路线图里的位置**：属于**阶段五的「可见面/可用面」前置**——`console_v0` 把此前只能靠 CLI 逐条 grep 的状态（runs/monitors/alerts/goals/unattended/gates/config/stage/capabilities）聚合成一屏，并把**写侧**从「散在各脚本里的 open(...,'w')」收成单一闸门（令牌 + 落点边界 + ledger + 并发独占）。它不改变任何阶段状态：阶段四仍 🔶、阶段五仍 ⬜。

**下一步（按依赖排序，未开工）**：
1. 阶段四判据 1 的真实达标——需要**真的连跑 7 夜**（不是演练），这是唯一不能靠写代码缩短的项；
2. 阶段四判据 2 的机械化——放行留痕的 `channel` 口径要覆盖 `goal.jsonl` 直写路径（`R-14` 残留）；
3. 阶段五准入面——`attestation_v0` 从 `DOC_ONLY` 走到「发布准入流程真调用」才会翻绿；
4. 可视化继续补：`console serve` 常驻托管（launchd）+ 面板筛选/历史趋势（当前是单次快照）。
