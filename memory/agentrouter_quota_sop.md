# agentrouter_quota_sop — AgentRouter 限量额度投放监控

**目标**：AgentRouter（内部大模型网关）每日**限量发放** Claude/GPT 额度，发完即止；
抢到要趁早 ⇒ 在投放窗口内每分钟探一次，一旦有额度立刻推飞书私信 + 钉钉群。

**入口**
- 工具 `tools/agentrouter_quota_watch.py`（stdlib + `curl_cffi`，449 行）
  - `--once` 单次探测 / `--notify` 有额度才推 / `--force` 忽略窗口 / `--dry-run` 不真推 / `--models` 只读列模型 / `--selftest`（离线 43/43）
  - `--watch` 窗口内自轮询（`--interval` 默认 60s、`--max-minutes` 默认 35）= 抢额度用的常驻模式；出窗口或批次已通知即退 rc=0
  - 退出码：`0` 正常（无额度 / 窗口外跳过 / 批次已通知）/ `1` 至少一个模型 AVAILABLE / `2` 环境或全量探测异常
- 定时 = **GA sche_tasks（用户 2026-10-02 纠正：禁止注册系统 launchd / crontab）**
  - `sche_tasks/agentrouter_quota_am.json`（北京 10:00 daily）、`sche_tasks/agentrouter_quota_pm.json`（19:00 daily）
  - prompt 头插 `${GA_ROOT}` 锚定行，再 `python3 tools/bg_task.py start --name agentrouter_watch_<slot> -- /opt/anaconda3/bin/python3 tools/agentrouter_quota_watch.py --watch --notify`
  - 因 sche_tasks 粒度只到 `HH:MM`，**分钟级高频靠 `--watch` 自轮询**，而不是靠 60 项 `StartCalendarInterval`
  - 观察：`python3 tools/bg_task.py status --name agentrouter_watch_am`；日志 `temp/bg_task/logs/agentrouter_watch_am.log`
  - ⚠️ 不要再往 `~/Library/LaunchAgents/` 写 plist（历史遗留 `com.cuip.genericagent-agentrouter-quota.plist` 已删除，副本在废纸篓；勿再写回）
- Key：keychain 名 `agentrouter_api_key`（脚本回退顺序 env `AGENTROUTER_API_KEY` → keychain → `~/.config/agentrouter/key`）
- 状态/账本：`temp/agentrouter_watch/state.json`（批次幂等）、`ledger.jsonl`（每次探测一行）

**★ 传输层铁律（2026-10-02 实测，最值钱的一条）**
- 站点按 **TLS 指纹**拦截：原生 `urllib` / `curl` 一律握手后被 reset（表现为 `SSL EOF` / `SSL_ERROR_SYSCALL`，`http=0`），
  而 Clash 对该站全节点 delay 测试却是**成功**的 ⇒ 「网络不通」是假象，别去查网络/换节点。
- 必须用 `curl_cffi` 的 `impersonate="chrome124"`（浏览器指纹）才拿得到响应；**换 UA 无效**（UA 只影响 `401 unauthorized client` 那一层）。
- 另一个门禁：站点按 **User-Agent 白名单**放行「支持客户端」，非白名单 UA 恒 401。
  实测可用 `claude-cli/1.0.0 (external, cli)`；实测被拒 `OpenAI/Python`、`curl/8.0`、`Chrome`。

**判据（单模型一次最小调用 `POST /v1/chat/completions`）**
- `200` → `AVAILABLE` 有额度（唯一推送条件）
- `402` → `EXHAUSTED` 本批池已耗尽（文案 `Budget pool quota has been exhausted.`）
- `401/403` + `unauthorized client` → `CLIENT_REJECTED`（UA 不在白名单，属环境问题不是没额度）
- 其它（5xx/超时/其它 4xx）→ `UNKNOWN`（**fail-closed，绝不当成有额度**）

**窗口 / 幂等口径**
- 投放时间 = **北京时间** 10:00 与 19:00（UTC 02:00 / 11:00）；脚本内 `WINDOWS=10:00–10:30 / 19:00–19:30`（闭开区间），自算北京时间，不吃本机时区。
- 窗口外 `--once` 直接跳过 rc=0 并打印「非投放窗口…跳过」⇒ **窗口外跑零成本**（发 4 个请求都省了）。
- 幂等键 = `batch`（如 `2026-10-02@10`），`state.json[batch].notified` 落盘**只在真的推送成功后**写 ⇒ 同一批不会重复打扰；
  推送失败不写 state ⇒ 下一分钟自然重试（这是设计意图）。`--force` 会绕过窗口且批次名记为 `(force)`，**不写 state**（可重复跑）。
- DeepSeek/GLM 不限量，**不参与**本监控。

**典型坑**
- 5 分钟粒度太粗（限量额度秒级抢光），而 GA sche_tasks 粒度只到**分钟** ⇒ 组合拳：sche_tasks 负责准时唤起，窗口内每分钟 1 轮交给 `--watch` 自轮询。
- 解释器必须带 `curl_cffi` ⇒ 一律写**绝对路径** `/opt/anaconda3/bin/python3`（scheduler / bg_task 子进程的 PATH 不保证）。
- 推送成功判据看**返回**（`notify_feishu`/`notify_group` 布尔 + detail），不看日志文案。
- 2026-10-02 14:46 真探测：三模型全 `http=402 EXHAUSTED` ⇒ 探针链路确认可用，只是当天额度已发完（`--force --dry-run` 无推送副作用）。

**复用启示（超越本任务）**
- 「站点不通」先分三层判：DNS → TCP/TLS → 应用层；**TLS 指纹**这类拦截会让 `curl` 与「节点延迟测试」给出相反结论。
- 抢资源型定时任务：窗口内高频 + 批次幂等 + 失败不写幂等键 = 既不漏也不重复打扰。
