# anyrouter.top 抢坑（slot）监控 SOP

一句话：anyrouter.top 的 Claude 上游**按号池供给**，余额再多也要命中空闲上游才返回 200，否则站点直接 429/503 `Service Unavailable`。正确姿势不是「充钱」而是**高频探测 + 命中即刻使用**。

## 1. 站点机制与判据（2026-10-03 实测）

| HTTP | 站点报文 | 判据 | 含义 |
|---|---|---|---|
| 200 | 正常 completion | `SLOT_AVAILABLE` | 命中空闲上游 ⇒ 立刻抢用 |
| 429 / 503 | `{"error":{"message":"Service Unavailable"}}` | `POOL_BUSY` | 号池满，本轮没抢到 |
| 400 | 「1m 上下文已经全量可用，请启用 1m 上下文后重试」 | `NEED_1M` | 未带 1m beta 头 |
| 404 | 「当前 API 不支持所选模型」 | `MODEL_UNSUPPORTED` | 该名只走 OpenAI 面（需换名，重试无用） |
| 401 / 403 | 含 `unauthorized client` | `CLIENT_REJECTED` | UA 白名单门禁 |
| 401 / 403 | 其它 | `KEY_INVALID` | 令牌失效 |
| 其它 | — | `UNKNOWN` | 判据外，fail-closed |

- **1m 上下文**：请求须带 `anthropic-beta: context-1m-2025-08-07`；Claude Code CLI 侧写成模型名 `claude-opus-4-7[1m]` / `gpt-6-astra-cc-format[1m]`。
- **传输层**：本机实测 `urllib` 通、`curl_cffi(chrome124)` 对本站 SSLError 握手失败 ⇒ 本工具走 urllib。UA 有白名单（`claude-cli/1.0.0` 可用）。
- 余额 ≠ 可用：2026-10-03 读数 余额 $6472.10 / 历史消耗 $227.90 / 请求 1210；`/api/log/self`（须 `New-Api-User: <uid>` 头）最近一次**成功**是 2026-07-12 ⇒ 近 3 个月零成功，「额度很多但用不了」是常态而非异常。
- 官方公告（约 2026-09-28）：claude 供应难保证，新增 `gpt-6-astra-cc-format`（支持 messages 格式），用法 `claude --model 'gpt-6-astra-cc-format[1m]'`。
- **17 名标定（2026-10-03，带 beta 头，逐个 `/v1/messages`）**：12 名真进号池（claude-3-5-haiku、claude-3-5/3-7-sonnet、Fable-5-1-reversed、opus-4-1/4/4-5/4-6/4-7、sonnet-4、sonnet-4-5、astra-cc-format[1m] 全 503/429=号池满）；`gemini-2.5-pro`/`gpt-5-codex`/`gpt-6-astra` 404=只走 OpenAI 面；`haiku-4-5` 520（Cloudflare 侧异常）；`claude-opus-5-5` 返回站点公告式 400（未放出）。**=名越多命中概率越高，故 MODELS 由 3 扩到 5**。
- `gpt-6-astra-cc-format[1m]` 实测连打 8/8 恒 429（与 claude 的 503 同义=号池满），不存在「重试就转 200」的可靠性。
- 大陆优化端点 `https://a-ocnfniawgw.cn-shanghai.fcapp.run` 对本站 key 返回 403 AccessDenied（FC 侧门禁）⇒ 不作为通道。
- 本机既有资产：`~/.local/bin/anyrouter-ip-refresh`（维护 Clash hosts 里的站点锁定 IP；R506 记录其每 30min abort，因两份 clash 配置的锁定行已被移除）。

## 2. 工具

`tools/anyrouter_slot_watch.py`（纯 stdlib + keychain）：

```bash
python3 tools/anyrouter_slot_watch.py --once            # 单轮探测（默认 5 个 messages 面模型）
python3 tools/anyrouter_slot_watch.py --once --notify   # 命中即推飞书私信 + 钉钉群
python3 tools/anyrouter_slot_watch.py --watch --notify --interval 300 --cooldown 21600 --max-minutes 1380
python3 tools/anyrouter_slot_watch.py --selftest        # 37 项负控自测
```

- 退出码：`1`=有可用（抢到）/ `0`=全 POOL_BUSY / `2`=其它（全 UNKNOWN / 全 MODEL_UNSUPPORTED / KEY_INVALID 等，fail-closed）。
- 默认 `MODELS`（5 个，2026-10-03 全 17 名标定后取 messages 面可用的代表性名）：`claude-opus-4-7`、`claude-sonnet-4-5-20250929`、`claude-opus-4-6`、`claude-sonnet-4-20250514`、`gpt-6-astra-cc-format[1m]`；`--models a,b` 可临时换名单，`--models`（不带值）=列站点在册模型。
- 状态：`temp/anyrouter_watch/{state.json,ledger.jsonl,watch.lock}`；推送幂等键=判定态 + 冷却（`--cooldown`，默认 3600s）。
- **单实例锁**：`watch.lock` 存 pid + `ps -o lstart=` 身份；已有实例在跑则本次直接退出并打印对方 pid/起始时刻（防「僵尸元数据当运行中」误判）。
- watch 语义：命中即推送但**不退出**，冷却窗内不重复；`--max-minutes` 兜底收尾。
- 密钥只从 keychain `anyrouter_api_key` 读，任何输出禁止打印明文。

## 3. 巡检接线（GA 定时，禁系统 launchd/crontab）

`sche_tasks/anyrouter_slot_watch.json`：每天 08:50 起，用 `tools/bg_task.py start --name anyrouter_slot_watch` 挂起 23h 长驻探测（`--interval 300 --max-minutes 1380`）⇒ 对 LLM **零 token 成本**覆盖全天，命中即私信 + 钉钉群。

## 4. 排错

- 全 POOL_BUSY 且长期无 200：先查 `/api/log/self` 是否有成功记录；再试换 kind（`gpt-6-astra-cc-format[1m]`）。
- 启动即「已有实例在跑」：`python3 tools/bg_task.py status --name anyrouter_slot_watch --json` 核对 pid/lstart；确为陈旧进程再 stop。
- 通知未到：查 `memory/notify_channels_sop.md` 的通道口径与体积上限；`--dry-run` 可零推送验证链路。
