# agentrouter_quota_sop — AgentRouter 限量额度投放监控

**目标**：AgentRouter（内部大模型网关）每日**限量发放** Claude/GPT 额度，发完即止；
抢到要趁早 ⇒ 在投放窗口内每分钟探一次，一旦有额度立刻推飞书私信 + 钉钉群。

**入口**
- 工具 `tools/agentrouter_quota_watch.py`（stdlib + `curl_cffi`；696 行 / 30622 B）
  - `--once` 单次探测 / `--notify` 有额度才推 / `--force` 忽略窗口 / `--dry-run` 不真推 / `--models` 只读列模型 / `--selftest`（离线 48/48）
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

**★ 传输层铁律（2026-10-03 复测订正——旧记方向已过时，最值钱的一条）**
- 站点确会按 **TLS 指纹** reset 部分客户端，但**被 reset 的是哪一种会随时间变化**，禁写死单一实现：
  - 2026-10-02 实测：原生 `urllib` / `curl` 被 reset（`SSL EOF` / `SSL_ERROR_SYSCALL`，`http=0`），只有 `curl_cffi` 通；
  - **2026-10-03 复测：方向反转**——`curl` 原生 `rc=35`、`curl_cffi(impersonate="chrome124")` 3/3 被 reset
    （`Recv failure: Connection reset by peer`），而**原生 `urllib` 3/3 拿到真 completion**（真 id `msg_bdrk_*`）。
- 另一层是 **User-Agent 白名单**（与传输层正交）：非白名单 UA 恒 `401 unauthorized client`；
  实测可用 `claude-cli/1.0.0 (external, cli)`；实测被拒 `OpenAI/Python`、`curl/8.0`、`Chrome`。
- ⇒ 现行实现 = `TRANSPORTS = ("urllib", "cffi")` **顺序回退**：任一档拿到响应即用，两档全败才判 `UNKNOWN`（fail-closed），
  并把实际生效档落进结果/台账/日志（`transport` 字段 + 打印 `via=urllib`）。
  **结论：额度真伪只看 `http=200` 与真 completion 正文，`http=0` 只说明当次传输层全灭，不等于没额度。**
**判据（单模型一次最小调用 `POST /v1/chat/completions`）**
- `200` → `AVAILABLE` 有额度（唯一推送条件）
- `402` → `EXHAUSTED` 本批池已耗尽（文案 `Budget pool quota has been exhausted.`）
- `401/403` + `unauthorized client` → `CLIENT_REJECTED`（UA 不在白名单，属环境问题不是没额度）
- 其它（5xx/超时/其它 4xx）→ `UNKNOWN`（**fail-closed，绝不当成有额度**）

**★ 客户端接入面（Pi / CLI 直连，2026-10-03 实测复现）**
- `POST /v1/chat/completions` **带 function tools** 在 **gpt-6-astra 系（reasoning）模型**上恒 `400`，原文：
  `Function tools with reasoning_effort are not supported for MaaS_GP_6_astra_20260903_off in /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'`
- ★**该 400 是模型专属、不是「面」专属**：同样带 tools，`claude-opus-5` / `claude-opus-4-8` 走 chat 面 `200`（真 completion）。
  ⇒ 判「站点不支持工具调用」前必须先**分模型**跑，别把一个模型的 400 当成全站限制。
- ★**400 自带的逃生门无效**：顶层 `reasoning_effort:"none"` 仍 `400`，`reasoning:{"enabled":false}` 亦 `400`
  ⇒ 唯一可用修法 = **客户端把 api 从 `openai-completions` 切到 `openai-responses`**（`baseUrl` 不变，仍是 `https://agentrouter.org/v1`）。
- 决定性验收（Pi 同款头 `User-Agent: claude-cli/2.1.220 (external, cli)` + `x-app: cli`）：
  `POST /v1/responses` + tools ⇒ `200`，且 `output[0].type == "function_call"`（`name=get_time`，`arguments={}`）。
- **UA 门禁细节**：闸门是 **UA 串本身**——`Python-urllib/*`、`curl/8.0` 恒 `401 unauthorized client`；
  ★**单加 `x-app: cli` 无效**（curl-UA + x-app 仍 401）；`claude-cli/*` 不带 `x-app` 也 `200`。
- **死模型**：`gpt-5.6-sol` 在 chat 与 responses **两面都 `503`**（`当前分组 default 下对于模型 gpt-5.6-sol 无可用渠道`）
  ⇒ 属死配置，任何模型清单都该摘掉（Pi 侧已无；本仓监控 `MODELS` 本就只含 3 个在册模型）。
- Pi 侧落地现状（本坑的原始受害者）：`~/.pi/agent/models.json` 的 `agentrouter` provider = `api: openai-responses`
  + `baseUrl: https://agentrouter.org/v1`（2026-10-03 12:53 起多次备份为证）；`agentrouter-anthropic` = `anthropic-messages`（走 `/v1/messages`，不涉此坑）。
- ⚠️ 本仓探针（`agentrouter_quota_watch.py`）只发**无 tools 的最小请求**，故**不触发**该 400，也不会因它误判额度 ⇒ 监控面与本坑互不影响。

**窗口 / 幂等口径**
- 投放时间 = **北京时间** 10:00 与 19:00（UTC 02:00 / 11:00）；脚本内 `WINDOWS=10:00–10:30 / 19:00–19:30`（闭开区间），自算北京时间，不吃本机时区。
- 窗口外 `--once` 直接跳过 rc=0 并打印「非投放窗口…跳过」⇒ **窗口外跑零成本**（发 4 个请求都省了）。
- 幂等键 = `batch`（如 `2026-10-02@10`），`state.json[batch].notified` 落盘**只在真的推送成功后**写 ⇒ 同一批不会重复打扰；
  推送失败不写 state ⇒ 下一分钟自然重试（这是设计意图）。`--force` 会绕过窗口且批次名记为 `(force)`，**不写 state**（可重复跑）。
- DeepSeek/GLM 不限量，**不参与**本监控。

**典型坑**
- 5 分钟粒度太粗（限量额度秒级抢光），而 GA sche_tasks 粒度只到**分钟** ⇒ 组合拳：sche_tasks 负责准时唤起，窗口内每分钟 1 轮交给 `--watch` 自轮询。
- 解释器一律写**绝对路径** `/opt/anaconda3/bin/python3`（scheduler / bg_task 子进程的 PATH 不保证）；`curl_cffi` 现为**次选**传输层，缺席只降级不影响首选 `urllib` 通路。
- 推送成功判据看**返回**（`notify_feishu`/`notify_group` 布尔 + detail），不看日志文案。
- 2026-10-02 14:46 真探测：三模型全 `http=402 EXHAUSTED` ⇒ 探针链路确认可用，只是当天额度已发完（`--force --dry-run` 无推送副作用）。

- **★「站点/额度都正常，但我自己的客户端报错」先查「面」与「模型」的组合**（2026-10-03 实例）：同一 key、同一 UA，`chat/completions` 带 tools 在 `gpt-6-astra` 系恒 400 而 `claude-*` 系 200 ⇒ 修法是**改客户端 api 面**（→ `openai-responses`），不是换 key、不是等额度。详见上文「客户端接入面」节。

**复用启示（超越本任务）**
- 「站点不通」先分三层判：DNS → TCP/TLS → 应用层；**TLS 指纹**这类拦截会让 `curl` 与「节点延迟测试」给出相反结论，且**同一站点不同日可能 reset 不同实现**⇒ 探活器要带回退与「实际生效传输层」标注（本次即 urllib 反转胜出）。
- 抢资源型定时任务：窗口内高频 + 批次幂等 + 失败不写幂等键 = 既不漏也不重复打扰。
- 同批「部分命中 + 部分传输失败」是常态，不是全或无：连续两批（`2026-10-02@19:00`、`2026-10-03@10:00`）实测 `claude-opus-5` `http=200 AVAILABLE`（已双通道推送 `feishu:ok; dingtalk:ok`）而 `claude-opus-4-8`/`gpt-6-astra` 均为 `http=0 UNKNOWN`（`transport: agentrouter.org=SSLError, ps.air-outer.com=ConnectionError/SSLError`）⇒ 命中即推送并退出是设计行为（`run_watch` 命中分支 `return 0`，故 `--watch` 命中时退出码是 0 而非 1）；**勿把同批其它模型的 UNKNOWN 读成「其余模型都没额度」**（UNKNOWN 是 fail-closed，需下一次批次重测）。
- **★state.json `notified` 时间戳口径（2026-10-03 复核）**：值为**本机本地时间**但后缀误标 `+00:00`（实测：19:00 窗口的 pm 记 `2026-10-02T19:01:35+00:00`、10:00 窗口的 am 记 `2026-10-03T10:01:14+00:00`，与 bg_task/ps 的本地启动时刻 19:00:54 吻合；若按 UTC 换算会差 8h）⇒ 判读 **禁做 UTC 偏移换算**；另该键**仅推送成功后写入**，可作送达凭据。
- **★2026-10-03 现场（用户报「监控说有额度、我自己用报错」）**：监控侧为真——`--once --force` 三模型 `http=200 AVAILABLE`（台账 `ledger.jsonl` 2026-10-03T19:00/19:48/19:49/19:49 四行全 200，末两行带 `"transport":"urllib"`），且 `--models` 真列 4 个在册模型（`claude-opus-4-8`/`claude-opus-5`/`deepseek-v4-flash`/`gpt-6-astra`）。用户侧报的错=**同批被 reset 的另一传输层**（curl 系客户端最易中招）⇒ 处置=换 `urllib`/换支持列表内客户端重试，**不要判成「没额度」**。
- **同批修掉的真缺陷**：`_get_urllib()` 缺局部 `import urllib.request`（顶层亦无）⇒ `--models` 恒 `NameError` 静默回落 `curl_cffi` 后报 `http=0`（修复前 `[agentrouter] None http=0 n=0`，修复后 `http=200 n=4`）；教训=**回退链每一档都要有自己的负控**，否则首选静默失效会被当成「站点不通」。
