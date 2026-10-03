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

`sche_tasks/anyrouter_slot_watch.json`：每天 08:50 起，用 `tools/bg_task.py start --name anyrouter_slot_watchd -- /opt/anaconda3/bin/python3 tools/anyrouter_slot_watchd.py --max-hours 23` 挂起**自愈守护**（守护包着探测脚本）⇒ 对 LLM **零 token 成本**覆盖全天，命中即私信 + 钉钉群。

## 3.1 自愈守护 `tools/anyrouter_slot_watchd.py`（2026-10-04 加装）

长驻探测的两个致命退化：进程悄悄死掉、进程活着但**卡死/不产出**。守护每 60s 巡检子进程，两种都当场重启（指数退避），子进程自然退出（跑满 `--max-minutes`）也按 RESTART_EXITED 续起：

```bash
python3 tools/anyrouter_slot_watchd.py --max-hours 23                     # 生产用法（随 bg_task 起）
python3 tools/anyrouter_slot_watchd.py --once --dry-run                   # 只巡检一轮，不真起子进程
python3 tools/anyrouter_slot_watchd.py --stall-min-grace 60 --max-hours 0.05   # 演练卡死升级路径
python3 tools/anyrouter_slot_watchd.py --selftest                         # 27 项自测（含负控）
```

- 判定事件落 `temp/anyrouter_watch/watchd.jsonl`：`START` / `RESTART_DEAD` / `RESTART_STALLED` / `RESTART_EXITED` / `HOLD_BUSY` / `HOLD_OK`；子进程 stdout 落 `temp/anyrouter_watch/watch.log`。
- 单实例锁沿用探测脚本自己的 `watch.lock`：锁被占 ⇒ `HOLD_BUSY` 让位（不会双开），锁过期 ⇒ 接管。
- 默认参数：巡检 60s、停滞判据 600s（`--stall-min-grace`）、退避上限 `--backoff`（默认 300s）、寿命 `--max-hours`。

## 3.2 故障也会把人叫回来（探测脚本侧）

「轮询什么都不推」等于静默失效，故探测脚本自带故障通知（与「抢到坑」通知共用 feishu 私信 + 钉钉群通道）：

- `--fault-streak`（默认 3）连续 N 轮拿不到可用态 ⇒ 推一条故障通知，含**日志路径**与最近判定/错误摘要；
- `--fault-cooldown`（默认 21600s = 6h）同一故障不反复刷；
- 故障恢复（再次拿到 POOL_BUSY/可用等正常判定）⇒ 推「已恢复」；
- 故障文案由 `--log-hint` / 环境变量 `GA_ANYROUTER_LOG_HINT` 可覆盖日志路径（守护启动子进程时自动注入 `watch.log` 路径）。

## 4. 排错

- 全 POOL_BUSY 且长期无 200：先查 `/api/log/self` 是否有成功记录；再试换 kind（`gpt-6-astra-cc-format[1m]`）。
- 启动即「已有实例在跑」：`python3 tools/bg_task.py status --name anyrouter_slot_watch --json` 核对 pid/lstart；确为陈旧进程再 stop。
- 通知未到：查 `memory/notify_channels_sop.md` 的通道口径与体积上限；`--dry-run` 可零推送验证链路；故障通知自检=`python3 tools/anyrouter_slot_watch.py --once --fault-streak 1 --notify`（会真发）。
- 子进程莫名没了/不产出：看 `temp/anyrouter_watch/watchd.jsonl` 的 RESTART_* 事件；守护本身不在跑则重新起（`bg_task start --name anyrouter_slot_watchd`）。
- 长驻期间「一轮都没推」不等于没故障：默认 3 连败才报（`--fault-streak`），要更灵敏就调小。
