# herdr_orch_sop

用途：用 herdr 的 agent/pane 传输层做多 agent 编排；当前最小骨架在 `temp/herdr_orch/`。

## 已验证事实
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

## 当前代码入口
- `temp/herdr_orch/protocol.py`：`make_req_id / wrap_prompt / is_done / strip_markers`
- `temp/herdr_orch/driver.py`：`list_agents / read / send / ask`
- `temp/herdr_orch/orchestrator.py`：`Orchestrator.on_done(src,dst,template)` + `run(agent,message)`；`agent` 参数直接传 pane_id/target。
- `temp/herdr_orch/demo_a_to_b.py`：A→B 示例。

## P1 数据源层（已完成，契约测试全绿）
- `HerdrJsonlBackend`(temp/herdr_orch/jsonl_backend.py) 打通：send(护栏+baseline记录)→is_done(双重确认:idle+回复变化,防发送瞬间status未转working的竞态)→read_reply(直读JSONL干净正文)→ask(阻塞轮询,但受60s杀进程限制,长任务改send+多次短read)。
- `backend.py` 分级路由：有 agent_session(claude)→jsonl 后端；无→screen 后端(抓屏+哨兵降级)。
- 契约测试 `test_backend.py`：10 项断言(状态语义4/分级路由2/发送护栏1/双重确认3)全 PASS。
- **测试隔离铁律**：契约测试 mock 了 snapshot(find_agent/read_last_reply) 后，还**必须 stub 掉 `driver.send`**（`driver.send = lambda t,x: None`）。否则 send() 第31行会真打 herdr CLI，报 `agent_not_found` 让测试崩溃。凡后端 send 路径的纯逻辑测试都要断掉传输层真机调用。

## 验证口径
- 本地回归必须覆盖：同行 DONE+时间戳、错误 req_id 不误判、mock DAG A→B 两次调用、mailbox/events 双写。
- 真机端到端至少验证：idle pane 收到 wrapped prompt，并在 `read` 中出现同 req_id 的 `ORCH_DONE`。
- 未经用户授权不要驱动正在 working 的业务 agent；只用 idle pane 或新建隔离 pane。
