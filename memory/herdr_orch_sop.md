# herdr_orch_sop

用途：用 herdr 启动/编排 agent。按复杂度分两个场景，**多数任务用场景 A 即可**：
- **场景 A（默认）**：单 agent 启动 + 持续交互，用 herdr 原生 CLI 状态机（`agent start/prompt/get/read`）。封装脚本 `../memory/herdr_agent_client.py`（`HerdrAgent` 类）。
- **场景 B（复杂场景）**：多 agent DAG 强编排（多前置 join / 失败重试 / 超时降级），用抓屏/JSONL 哨兵 + P1/P2 编排层，代码在 `temp/herdr_orch/`。

herdr 工具已升级：新增 `agent_status: done`（区分于 idle，代表本轮真正完成且稳定）+ `herdr agent read --source recent-unwrapped` 可拿到干净正文。**场景 B 中"codex 正文不可留存，只剩一个 bullet"的旧结论已不成立**（实测 codex 多轮正文可通过 `recent-unwrapped` 完整拿到），仅在场景 B 仍需强 DAG 编排能力时才有必要用抓屏/JSONL 方案。

---
## 场景 A：单 agent 启动 + 持续交互（默认，优先用）

### 快速用法
```python
from herdr_agent_client import HerdrAgent

ag = HerdrAgent.spawn("worker1", kind="codex", cwd="/abs/path/to/project")
reply = ag.ask("请分析这段代码的问题")   # 阻塞，返回终端最近文本片段
reply2 = ag.ask("继续，请给出修复方案")   # 同一 session 继续多轮
ag.close()   # 关闭 tab，清理 pane
```
kind 取值见 `herdr agent start --help`：pi|claude|codex|gemini|cursor|devin|agy|cline|omp|mastracode|opencode|copilot|kimi|kiro|droid|amp|grok|hermes|kilo|qodercli|maki

### 已验证事实（场景 A）
- `herdr tab create --cwd <dir> --label <name> --no-focus` 建隔离 tab，返回 `root_pane.pane_id`（如 `w9:p6G`）和 `tab.tab_id`；用完必须 `herdr tab close <tab_id>` 清理，否则残留空 tab。
- `herdr agent start <name> --kind <kind> --pane <pane_id>` 在该 pane 里拉起交互式 agent 进程，返回 `agent_status`。
- `herdr agent get <pane_id>` 返回真实状态：观测到的取值有 `idle`（尚未发送/等待输入）、`working`（正在生成）、`done`（本轮已完成且稳定，不同于 idle）、`blocked`（等待审批，如敏感操作确认）。
- `herdr agent read <pane_id> --source recent-unwrapped --lines N` 返回干净的最近终端文本（无 ANSI/无框线），是提取回复内容的首选，比 `herdr pane read` 更适合结构化提取。

### 推荐编排模式（HerdrAgent 内部实现）
1. `spawn`: `tab create --no-focus` 建 pane → `agent start --kind <kind> --pane <pane_id>`，保存 `pane_id`/`tab_id`。
2. `ask(prompt, timeout)`: `agent prompt <pane_id> "<text>"`（**不要**加 `--wait`，见坑1）→ 自行轮询 `agent get <pane_id>`，间隔 1s，直到 `agent_status == "done"`（或 `blocked`，代表需要人工审批）或超时 → `agent read --source recent-unwrapped --lines N` 取回复。
3. `close`: `tab close <tab_id>`，避免残留空 tab 占用 workspace。

### 坑点记录（场景 A）
1. **`agent prompt --wait --until <state>` 不可靠**：实测会一直空等到耗满 `--timeout` 才返回（即使内容早已生成完毕），或直接报 `timeout`/`agent_prompt_stalled`。**但底层 prompt 其实已经真正送达并执行**——即使 CLI 报错/超时，pane 里的对话也已经完成，不代表任务失败。结论：**不要依赖 `--wait`，改用 fire-and-forget + 自行轮询 `agent get`**，更快更准（实测 3-4 秒即可收敛到 `done`）。
2. **codex 首次启动可能触发 npm 自更新**：CLI 检测到新版本会自动 `npm install -g @openai/codex` 并要求重启，导致 pane 内进程被替换，`herdr agent get <pane_id>` 返回 `agent_not_found`。处理：等更新日志打完（`🎉 Update ran successfully`）后，对同一 `pane_id` 重新 `agent start`（无需新建 tab）即可恢复。
3. **工具层超时与 herdr 内部超时会撞车**：若外层调用工具本身有默认超时（如本环境 `code_run` 默认 60s），需让 herdr `--timeout` 明显小于外层超时，否则外层会先把子进程杀掉，拿到的是不可信的 "Timeout Error 强制终止"，而不是 herdr 自身的错误信息。
4. `agent_status` 观测到的取值：`idle`（未发送/等待输入）、`working`（生成中）、`done`（本轮完成且稳定）、`blocked`（等待人工审批，需配合 `agent send-keys` 或 UI 交互处理，未在本轮验证 blocked 场景，遇到时按 pane read 实际内容判断再决定发键）。**没有单独的 `agent wait --until done` 复用价值**——它内部逻辑等同于 `--wait`，同样可能耗满超时，仍建议轮询。

---
## 场景 B：多 agent DAG 强编排（复杂场景才用）

### 已验证事实
- herdr agent target 必须用 `pane_id`（形如 `w9:p3G`），不能用列表里的 agent 名（如 `grok`），否则 `agent_not_found`。
- `herdr_orch.driver.send()` 返回 `None` 是正常设计；不要对返回值切片，发送是否成功看 herdr CLI exit code/异常。
- 真机 grok 已三次回显 `ORCH_DONE: <req_id>`，哨兵完成判定可用。
- 工具调用约 60s 会被杀；不要单次 `ask(timeout=120+)` 长轮询。长任务拆成 `send()` 后多次短 `read()` 轮询。

## 协议坑
- 真机 TUI 可能把正文和 `ORCH_DONE` 挤在同一行，尾部还带时间戳；`is_done` 不能锚定行首/行尾，只能搜索 `ORCH_DONE + req_id`。
- `strip_markers` 应以 DONE 标记为界截断，只保留 DONE 之前文本，并过滤常见 TUI 边框行。
- 抓屏 `herdr agent read` 有固有限制：会混入 TUI 装饰；正文滚出可视区会丢失。哨兵判定可靠，但若要 100% 干净正文，后续优先接 agent JSONL/日志源，不要长期依赖屏幕正文。

## 产品化数据源（P0 已真机验证，做产品级用这条，别再靠抓屏）
- `herdr api snapshot`（JSON）输出每 pane 元数据：`agent / agent_status(idle|working) / cwd / pane_id / agent_session.value`。
- 只有深度集成 agent（claude，source=`herdr:claude`）才有 `agent_session`→有官方 JSONL；grok 这类无该字段→只能退回抓屏+哨兵。**这是 backend 分级的依据**。
- claude 会话日志：`~/.claude/projects/<编码cwd>/<session-uuid>.jsonl`。定位用 `agent_session.value`(UUID) **直接 glob 文件名，不要推导 cwd 编码**（编码把 `/` 和 `_` 都换成 `-`，不可逆，bar-smyze 与 bar_smyze 会撞）。
- JSONL 中 assistant 消息的 `message.content[].text` = 100% 干净正文，无 TUI 噪音、无滚屏丢失。
- 完成判定优先用 `agent_status`（herdr 自维护，比自定义哨兵可靠）；哨兵仅作降级/双保险。
- 护栏：send 前查 `agent_status`，非 idle 拒绝驱动（避免打断 working 中的业务 agent）。
- 完整产品设计（Backend 抽象/P0-P4 里程碑/CCB 差距表/AGPL 合规）见 `temp/herdr_orch/DESIGN.md`。

## P2 编排层（已完成，契约测试全绿）
- `dag.py`(temp/herdr_orch/dag.py) 声明式 DAG，三语义：
  - **多前置 join**：节点 `deps` 全部 ok 才触发（B 等 A1+A2 都完成）；下游 prompt 用 `{节点名}` 占位注入前置回复。
  - **失败重试**：每节点 `max_retries` 次 + 指数退避(backoff)；BackendError/超时都算失败。
  - **超时降级 on_fail**：`abort`(整 DAG 抛 DagError 快速失败) / `skip`(标记 failed，join 阻断下游自动 skip，独立分支继续) / `degrade`(fallback 占位，下游照跑)。
  - 拓扑排序用 Kahn 算法，带环检测 + 缺失依赖检测（都抛 DagError）。
  - 传输经可注入 `runner`（默认走 backend.get_backend().ask）；契约测试注入 mock runner，不碰真机。
- 契约测试 `test_dag.py`：16 项断言(join3/重试3/abort1/skip3/degrade3/环1/缺失依赖1+拓扑序2隐含)全 PASS。
- **修复 bug**：orchestrator.py 原调 `be.run_blocking`(后端无此方法，实际跑不通)，已改为 `be.ask`，与 backend 接口对齐。

## 当前代码入口
- `temp/herdr_orch/protocol.py`：`make_req_id / wrap_prompt / is_done / strip_markers`
- `temp/herdr_orch/driver.py`：`list_agents / read / send / ask`
- `temp/herdr_orch/orchestrator.py`：`Orchestrator.on_done(src,dst,template)` + `run(agent,message)`；`agent` 参数直接传 pane_id/target。（简单线性/callback 边编排）
- `temp/herdr_orch/dag.py`：`DAG(runner=None,backoff=2.0)` + `Node(name,target,prompt,deps,max_retries,timeout,on_fail,fallback)` + `add()/run()`。（P2 声明式多前置 join + 重试 + 降级；复杂拓扑用它）
- `temp/herdr_orch/test_dag.py`：P2 契约测试(16 断言)。
- `temp/herdr_orch/demo_a_to_b.py`：A→B 示例。

## P1 数据源层（已完成，契约测试全绿）
- `HerdrJsonlBackend`(temp/herdr_orch/jsonl_backend.py) 打通：send(护栏+baseline记录)→is_done(双重确认:idle+回复变化,防发送瞬间status未转working的竞态)→read_reply(直读JSONL干净正文)→ask(阻塞轮询,但受60s杀进程限制,长任务改send+多次短read)。
- `backend.py` 分级路由：**判据是 `info.has_jsonl`（=`session_id` 非空 且 `source=="herdr:claude"`），不是 session_id 单独非空**。codex 也报 session_id 但 source=`herdr:codex`→has_jsonl=False→走 screen 后端。（入口函数是 `get_backend(target)`，不是 select_backend）
- `HerdrScreenBackend`(temp/herdr_orch/screen_backend.py) 完成判定**根因修复**：`is_done` = 观测过 working 后回落到可用态(idle/done)；用 `_saw_working` 标志堵 poll0 竞态（send 前可能残留 idle，须先见 working 再回落才算完成）。**已彻底弃用两条旧方案**：①纯哨兵 DONE 计数≥2（codex TUI 折叠在飞 prompt，内嵌 DONE 行常不上屏→计数不可靠→永久超时）②屏文相对 baseline 变化作附加闸（send 会立刻回显 prompt 污染屏文 baseline，一回显即成立，无法区分"仅回显"与"真出正文"）。哨兵/屏文降级为仅供 read_reply 抽正文。
- 真机验证(w9:p45/codex,2026-07-27)：修前 22 轮全 False、50s 超时；修后 ask() 8.4s 返回，超时根因确实解决。
- **codex 正文不可留存的已知局限**：codex TUI 完成后折叠/重绘响应区只留一个 `•` bullet，正文从不留存于可抓取屏文（隔 1 分钟复读仍只有 `•`）。故 screen 后端**完成判定可靠、正文尽力而为**；适合"发指令+确认完成"编排，需可靠回收正文就路由到有 JSONL 的 claude。
- 契约测试 `test_backend.py`：状态语义4/分级路由2/发送护栏1/双重确认3 + **screen 完成判定4**（poll0 闸/working 中/回落 idle/回落 done）全 PASS。
- **测试隔离铁律**：契约测试 mock 了 snapshot(find_agent/read_last_reply) 后，还**必须 stub 掉 `driver.send`**（`driver.send = lambda t,x: None`）。否则 send() 第31行会真打 herdr CLI，报 `agent_not_found` 让测试崩溃。凡后端 send 路径的纯逻辑测试都要断掉传输层真机调用。

## 验证口径
- 本地回归必须覆盖：同行 DONE+时间戳、错误 req_id 不误判、mock DAG A→B 两次调用、mailbox/events 双写。
- 真机端到端至少验证：idle pane 收到 wrapped prompt，并在 `read` 中出现同 req_id 的 `ORCH_DONE`。
- 未经用户授权不要驱动正在 working 的业务 agent；只用 idle pane 或新建隔离 pane。

## 场景 C：便携 skill 交付（脱离 GA/SOP，随处可用）——2026-09-18
- **动机**：用户不一定在 GA 环境用编排，想在任意 herdr 环境让「当前 agent 自驱」组队/接力，不手搓 python。
- **关键事实（真机核验）**：
  - Herdr **无队列/无角色/无编排**——只提供 workspace/tab/agent/pane 原语。官方 `herdr --skill` 全文(12600字)逐词统计 team/role/orchestrat/spawn/delegate/disband **全为 0**，只教机械调 CLI。「拉群组队/角色/依赖/解散」是工作流层，官方没有 = CCB 补的那层。故自写 skill 骑在官方原语上（互补不重复）。
  - `herdr workspace create --cwd --label --env --focus/--no-focus` 返回带 workspace_id 的 JSON；`herdr workspace close <id>` 解散。→ **一个团队 = 一个专属 workspace，解散 = close 它**。
  - `herdr workspace list` 实测返回 `.result.workspaces[].workspace_id`（如 `wF`）；create 的具体 JSON 路径 skill 里标注「运行时从真实响应读、勿预测 ID」。
- **交付物（单一事实源 = 本机统一 skill 源 `~/.agents/skills/`，非另起门户！）**：
  - 本机早有统一源 `~/.agents/skills/`（lark-*/ask/computer-use 全在此），各 agent 的 skills 目录都软链进它（qoder/droid 的 `computer-use L->~/.agents/skills/computer-use` 为铁证）。故 herdr skill 必须并入此源，**禁止另起 `~/.herdr-skills` silo**（已犯此错并迁回）。
  - `~/.agents/skills/herdr-handoff/SKILL.md`: 轻量接力（coder 写完→换 agent/跨模型 review→取回）。
  - `~/.agents/skills/herdr-team/SKILL.md`: 临时组队。以「负责人」身份**按复杂度选路线**（S 直接干 / M coder+reviewer / L 全流程 product→architect→coder→reviewer→tester），**角色始终显式**；team=专属 workspace，依赖靠 lead 把上游产物喂给下游，**默认验收后 `workspace close` 解散**（可选发布后/不解散）。Route L 的文档标准路径由 `herdr-team/bin/teamctl.py` 硬控：状态只能按 `REQUIREMENTS_READY→DESIGN_READY→IMPLEMENTED→REVIEW_PASSED→TEST_PASSED→ACCEPTED→CLOSE_READY` 推进（明确需求可带理由 waive）；每一步需角色匹配、绝对且存在、位于 `project_dir` 内的持久产物和相应证据。提交先持久化 `workflow-pending.json` 意图，再追加事件和原子状态；任意下次命令会在同一 `fcntl` 锁内恰好一次恢复中断提交后清除 pending。所有命令（含 `status`）同锁；`init` 还要求团队目录名等于 team-id、project_dir 已存在、`--actor-pane` 等于同 workspace 的 `--lead-pane-id`，并把 actor 写入初始化事件。委托回放逐事件绑定外层派发 lead；成员登记必须是同 workspace 的非 lead pane；团队目录叶节点禁止符号链接。已用真实 CLI/持久化 JSON 篡改回归覆盖初始化 actor、事件 actor、成员 pane、目录软链（autopilot 全量 51、委托类 17 均通过，独立只读审计 PASS）。此为经控制器的文档路径硬门禁/审计，Herdr 原始 CLI 无 RBAC/hook，且 CLI 参数可被 shell 调用者自报，**不是 OS/RBAC 身份认证**；刻意绕过仍不受其阻止。**Trellis 变体**仅用于已含 `.trellis/` 且可拆多 task 的 L 路线：architect 定稿后由独立 `task-breaker` 用 `task.py create`/`--parent` 生成 task 与依赖，再移交 `trellis-orchestrate` 执行；后者只编排既有 task、不创建 task。移交前必须运行 `herdr-team/bin/trellis_preflight.py` 并取得 exit 0：它硬验 `.trellis/scripts/task.py` 存在及活动 task 数达 `--min-tasks`（参数不得小于1），否则 BLOCKED 并回退原生流程。`task.py validate` 仅校验可选 context 文件，**不**把它误作 DAG 依赖验证。另有 `BEGINNER_GUIDE.md` 提供已实跑的原生闭环命令和恢复说明；**2026-09-20 修复其陈旧命令**（原示例缺 `--workspace-id/--lead-pane-id/--actor-pane`，照抄必 argparse 报错；`register-member` 另需 `--member-pane`），并加「谁是指挥」对齐小节。**leader 归属（两模式对齐）**：直接 team 模式下**发起 pane 只是 sponsor（不写 teamctl）**，运行 lead ＝ 团队 workspace 内**新建的可见 `team-lead` tab**（唯一 teamctl 写入者）；autopilot 委托模式下**发起 autopilot pane ＝ W0 治理**（唯一外层写者，gate/lock/dispatch/receipt/quality-score），团队内仍由那个可见 `team-lead` 独占 teamctl 写权，`delegation.lock` 串行化委托。硬约束：一个 team-id ↔ 一个 workspace_id ↔ 一个 lead_pane_id ↔ 一个写入者；team-lead 消失/身份不符应 BLOCKED 而非让发起 pane/worker 接管。**真实演练(2026-09-20, 真实 CACHE_ROOT)**：native 全链路 8 事件到 CLOSE_READY ✅；autopilot 委托 trellis 到 COMPLETED(complex,quality=1.0) ✅，其中踩坑＝`--mode trellis` **必须过 TASKS_READY**(task-breaker)否则 `cannot enter IMPLEMENTED from DESIGN_READY`；负例(伪造 actor/跳阶段/项目外产物)均 BLOCK ✅；详见 `~/.agents/skills/herdr-team/DRILL_LOG.md`。**「卡死在 CREATED」已定案为非缺陷**：物证 `~/.cache/herdr/teams/review-trellis-58429-missing`(schema1,team_id=unrelated-id,project_dir=/private/tmp/.../not-created,仅 1 条 initialized 事件)＝2026-09-18 手工负例 init 打到真实缓存目录未清理(单元测试用 mkdtemp+覆盖 HOME 隔离,不泄漏)；已改名归档为 `.archived-*`，且新 `init` 强制三参数+写 actor(schema2)堵死该输入。
  - `~/.agents/skills/_herdr-install.sh`（幂等软链器，把两 skill 链进其它 5 个 agent）+ `_herdr-README.md`。
- **安装**：`bash ~/.agents/skills/_herdr-install.sh`。已链：claude/codex/gemini/cursor/qoder(~/.qoder/skills)/droid(~/.factory/skills)。改 skill 只改统一源，全 agent 立即生效。opencode 用 ~/.config/opencode/commands（不同格式，未纳入）。
- **用法回答**：①**必须在 Herdr 环境**（触发 skill 的 lead agent 需 `HERDR_ENV=1`；skill 首行硬门校验，不在则自停）。②**临时销毁**——团队默认验收后解散释放 panes/进程；关前须先把产物落到 workspace 外（commit/写盘），close 后 ID 不复用。
- **跨 repo 团队的 workspace 结构**（用户真实场景：前端/后端/测试是不同工程）：一个 team=一个 workspace；**每个角色 = 一个 tab，`tab create --cwd <该角色的 repo>`**（前端 tab 指前端 repo、后端 tab 指后端 repo、测试 tab 指测试 repo，互不干扰）；tab 内可再 split pane 跑常驻命令（dev server/watch）。**别把多 repo 挤一个 tab**。
- **纯 Herdr 的可靠协作边界**：skill 负责临时团队的角色路由；`herdr agent prompt` 只作为唤醒，不作为可靠传输。团队目录下的 `herdr-team/bin/mailbox.py` 提供原子 `send`、不可变 `received` ACK、去重和 `status`；完成另发 `kind=result`，超时查状态再重试。它是随团队创建和清理的本地文件协调状态，不是 daemon、外部队列或 CCB 依赖。
- **P2P 通信取舍**：默认由 lead 路由（hub-and-spoke）；无依赖角色可用同一 mailbox 直聊。禁止从 `--wait` 推断送达，必须以 ACK/status 为准；没有需要持久化的任务则在解散前删除 mailbox。
- **skill 内固化的真机坑**：never auto-approve trust/hooks 门(blocked→停下问人)；`--wait` 不可靠改 fire-and-forget + 轮询 `agent get`；只 close 自己建的 workspace；`agent read --source recent-unwrapped` 取干净正文；`unknown`≠done。
- **2026-09-21 控制面全面评审（autopilot×herdr-team）结论**：威胁模型=documented-path 审计门禁非 OS/RBAC。评审 A/B/C/D 四项风险**全部闭合**：A/B=委托验收回放团队事件+重算 request sha256+绑定 team_id/workspace/lead_pane；C=teamprofile 拒 worker 名 ∈ {lead,monitor}；D=新增 `team_lock()` 使 `delegated_team_acceptance` 全程持团队 `.workflow.lock`（锁序 run→team 无环），并加 TOCTOU 回归测试。证据：单测 52/52、演练 51/51（A/B/C 38 + D 13，驱动脚本在 temp/，隔离 HOME）。坑：`teamctl/autopilotctl` argparse **子命令必须在 --dir 之前**；teamprofile BLOCK 输出 `ERROR:`；工件须真实存在于 project_dir 内；self 模式完成必经 `DISPATCHED`(无 --team-id→mode=self)；委托锁按"内容+时间戳判活"非 flock。独立外部复评本轮不可用(codex 4router 额度不足/gemini 已弃用/无 claude·grok)，为对抗式自查。
- **2026-09-21 晚补测 R/G 恢复性 + 真 agent 端到端（此前只在会话里口头列过、从未真跑）**：驱动 `temp/drill_recovery.py`（隔离 HOME；`r5`/`e2e` 子参数可单跑），结果 `temp/drill_recovery_results.json`，日志 `temp/drill_recovery_full.log`。R1 lead pane 消失(状态持久、仅原 lead 可写、**无指挥权移交子命令**，leader 真死只能重建团队) / R2 worker 掉线(不卡死、不静默补位) / R3 真杀进程后 pending 事务自动回放、事件只追加不重放(initialized 仅 1 次→断点续跑) / R4 验收证据删改截断一律 BLOCK(以团队事件流回放为准) / R5 真实 `herdr workspace` 建后关：团队态可读、phase 不漂移，**但控制面不校验 pane 存活(R5-6)**，pane 消失仍可推进→workspace 存活检查须放编排层 / G 组：需人工确认时停在 PLAN_READY 且不污染状态、补 REVIEWED 后从断点续跑、未确认期间不空转。真 agent 段：真实 workspace+`herdr agent start pi`+真 pane，`agent prompt --wait` + `agent read --source recent-unwrapped` 取回应答 'OK' 证活，演练后 workspace 已关。总盘 **126 演练 + 52 单测全绿**；详见 DRILL_LOG 第 7/8 节。坑：断言辅助函数要兼容 list/None，否则末段崩溃留下未关的 workspace 与活 agent（先收尾再续调）。
- **2026-09-21 深夜再补（演练 9/10/11，DRILL_LOG §9~§11）**：①`drill_dag.py` 让 **5 个真 pi agent** 真跑完整 L 路线，
  真产出 requirements/design/impl-notes/review/test-report/acceptance/close 七份文件并逐阶段过真门禁（37/37），
  测试用脚本**独立复跑**验证（不信 agent 自述）；②`drill_fuzz.py` 恶意/畸形输入 22/22，
  **真抓到 2 个缺陷并修复**：`teamctl.py`/`autopilotctl.py` 的 `load_state` 遇 "JSON 合法但结构错"（`null`/`[]`/`{}`/字符串/`events` 非数组）
  会抛 traceback 而非 BLOCKED → 已各加 <20 行结构校验（前后对照 `temp/prefix_bug_evidence.txt`）；③独立复评以
  **全新无上下文 pi agent** 顶替（codex 仍 403 额度不足），代码面未发现新高危绕过，文档面抽查两次自相矛盾（75/37/22 vs 2/3/22）不可采信。
- **herdr/pi 操作坑（本轮实测）**：**agent 名全局唯一**（跨 workspace 也冲突 → `agent_name_taken`）；
  **刚 `workspace create` 后立刻 `agent start` 可能失败/agent 秒消失**，要等 pane `interactive_ready` 再启（或重试）；
  **pi 启动到可交互约 20-25s**，早于此发 prompt 会被吞（agent 停在 idle 欢迎屏）→ 先 sleep 25s 或看 pane 内容确认；
  `agent prompt` 可 fire-and-forget 后用 `agent get` 轮询 `agent_status`（长任务别用 `--wait` 干等）；
  **pi 一次读太多大文件会撞 `400 context_length_exceeded`** → 复评/派活 prompt 必须限定文件与行数范围。