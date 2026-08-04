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
