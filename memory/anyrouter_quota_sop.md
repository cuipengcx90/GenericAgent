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

## 1.1 L 站社区攻略核对与判据订正（2026-10-04）

抓取并逐字回源校验 5 篇公开帖（语料存 `temp/anr_lstation/`，本会话 10 文件/76698B），与原判据逐条对账：

| 社区原话（逐字，引自对应帖） | 出处帖 | 对本工具的订正 |
|---|---|---|
| 不要不要不要不要不要只看响应码数字！！！！要看响应码＋Error的具体信息！！！别丢个数字直接对号入座！ | 1809254 | 判据必须**码 + message 文本**合看，禁只按 HTTP 码对号入座 |
| 目前的大部分的nobday（400/403/503）以及负载上限都是上游问题，反复重试和切终端切模型即可！！！ | 1809254 | 429/503/500「负载上限」=上游供给态，**非**「账号没额度」 |
| 大概率是限流，一直尝试重连，10次以后会显示服务不可用。但并不代表any无法使用 | 2267684 | 与 `429→POOL_BUSY` 一致；「不可用」不能由单次 429 得出 |
| any一般都是opus用不上，一直429，haiku没啥人用，gpt5.5倒是随便用 | 2502646 | **模型维度**：同站不同模型挤坑难度差极大 ⇒ 结论只对当轮 `--models` 名单成立 |
| 刚才我挤fable-5半天进不去，然后我换成了4.8，直接重试了两次就进去了 | 2512543 | 换模型/换名本身就是抢坑手段（= 本工具 `--models` 扩名的依据） |
| 如果你的上下文过长它会跳重试 | 2512543 | 长上下文会触发 retrying；探针发的短请求**不代表**用户长会话可用 |
| 上下行流量太大，长上下文响应时间几乎线性增大，10-150s都有可能，再大一些的上下文，甚至会触发retrying，但是往往挂着重试，也会缓慢继续响应 | 2267684 | **HOLD 有正解**：连接挂住无字节可能是慢响应/重试中，150s 判死偏激进 |
| 晚上八点多好不容易挤进去了，很开心的等待结果，结果额度一直在消耗，花费了16刀，但是一直没返回结果，最终还是retry了十次之后GG了 | 2524731 | **「挤进去」≠「能蹬上」**：命中号池后仍可能零产出烧额度 ⇒ 见下判据口径 |

判据修正（其余各条不变）：
- 新增 `500` + message 含「负载已经达到上限」⇒ **`POOL_BUSY`**（原落 `UNKNOWN`；站点确实用 500 表达负载上限）。★**2026-10-04 已实现**：`classify()` 前置分支（selftest 93→95，含反例「裸 500 → `UNKNOWN`」仍 fail-closed）。
- `HOLD`（连接建立、无字节、超时后被断）单列 **`HELD_SLOW`**：语义 = 可能正在慢响应/重试中，**不作「无可用」判据、不推送**，也不与 `POOL_BUSY`/`UNKNOWN` 混计。（**未实现**：现仍落 `UNKNOWN`，属待批项）
- `SLOT_AVAILABLE` 口径收紧：必须**拿到 200 且正文非空**；仅 TCP/TLS 或首包成功不算「抢到」。
- 结论外推边界：任何一次判定只代表**当轮模型名单 × 当刻**，禁写成「该站有/无额度」。

★未证实项（禁止当机制写入/实现）：网上流传的「统一或保活 session id」「定时发内容 `1` 保温」在本轮抓取的 5 篇原文中 grep **零命中**（`session id`/`保活`/`粘性` 全 MISS）⇒ 标记 UNVERIFIED，不采纳。


## 1.2 ★探针根因订正：`session_id` 形态门禁 + 「快返」语义（2026-10-04 晚间实测）

**这一条推翻 10-03 起的全部探测结论**，凡引用旧台账（`temp/anyrouter_watch/ledger.jsonl`）里
10-04 之前的 429/503 读数，一律先按本节重判。

### （1）门禁落在 `session_id` 形态上，不在号池

`metadata.user_id` 是 CC 客户端自己拼的字符串，站点只看里面 `session_id` 那一段的**形态**：

- 真 CC 的值 = **UUID**（36 字符 `8-4-4-4-12`）；**任意** uuid4 都过闸（3/3 走上游、挂满传输超时）；
- 旧实现自造的 `user_<32hex>_account__session_<uuid>`（91 字符长串）**恒被瞬时拒**（429 @1.9s），与号池有没有空闲上游无关；
- `device_id` / `account_uuid` 都**不是**门禁（真 CC 的 `account_uuid` 同样是空串）。

⇒ 单变量 6 发对照见 `temp/anr_truth/truth5_*.jsonl`（probe5）与 `temp/anr_truth/probe7*.jsonl`（ABBA 交错复验：同模型
`gpt-6-astra-cc-format`，uuid 形 2/2 走上游、旧形 2/2 快返 429）。

**后果（当时有多严重）**：本工具 `build_probe_body()` / `build_claim_body()` 一直走 `session_id_for()` 自造长串，
故自 10-03 落地起**所有** 429/503 都是「身份被拒」的**假阴性**，不是「号池满」——10-04 当天台账 648 次 503 +
173 次 429 全是这个 bug 的产物，抢坑路径同样结构性死。

**修法**：`session_id_for()` 改 `uuid5(NAMESPACE_URL, "anyrouter|<sha256(key)>|<tag>")` —— 既满足 UUID 形态，
又对 (key, tag) 确定 ⇒ 跨进程/跨轮稳定，抢坑与后续保活能复用同一坑位身份；key 只进 sha256，不落盘明文。

### （2）「快返 429」不再单独构成故障判据（GATE_REJECT 收紧）

旧逻辑见「全模型都在极短耗时内被拒」⇒ 判 `GATE_REJECT`（配置/形状坏了）⇒ 发告警。实测反例：`claude-opus-4-7`
在 uuid5 / 旧自造 / 真 dump uid 三种身份下**都是 1.1s 快返**⇒快返本身**证不出**身份被拒。

现判据：全快返**只有**在「对照探针」（真 CC 身份，即 UUID 形）**同样**快返时才判 `GATE_REJECT`；对照探针能走上游
（挂满超时）⇒ 快返属**真号池态**，照旧归 `POOL_BUSY`。对照身份来源按优先级：环境变量
`ANR_GATE_CONTROL_UID_JSON` → 本机真 CC dump（`/tmp/anr_proxy_dump/000_req.json`）；显式设
`ANR_GATE_CONTROL_UID_JSON=none|off|0` 可禁用对照（无对照 ⇒ 该维度判 `unknown`，**不得**凭空升级为
`GATE_REJECT`，fail-closed 只体现在「不冤枉站点」方向）。

### （3）新增观测维度与状态

- `elapsed_s` / `latency_class` 落台账：区分「瞬时拒（~1-2s）」与「过闸排队（≥`QUEUED_MIN_S`=8s，直到传输超时）」；
- `QUEUED_NO_UPSTREAM`：传输超时但已连上并挂满 ≥8s ⇒ **过闸、只是上游无空闲槽**（≠链路故障，≠没额度）；
- 探测并发化（`--workers`，默认 5）+ `--timeout` 默认 90s（旧值偏短会把「排队」误判成失败）。

### （4）本节结论之外的仍未证实项

「session id 保活 / 粘性」在 5 篇 L 站原文里 **0 命中**（见 §1.1）= **UNVERIFIED 不采纳**：uuid5 只是满足形态门禁
并保持稳定，不代表站点真的把号池坑位绑到该 id 上。

## 2. 工具

`tools/anyrouter_slot_watch.py`（纯 stdlib + keychain）：

```bash
python3 tools/anyrouter_slot_watch.py --once            # 单轮探测（默认 5 个 messages 面模型）
python3 tools/anyrouter_slot_watch.py --once --notify   # 命中即推飞书私信 + 钉钉群
python3 tools/anyrouter_slot_watch.py --watch --notify --interval 300 --cooldown 21600 --max-minutes 1380
python3 tools/anyrouter_slot_watch.py --selftest        # 119 项负控自测（2026-10-04 扩充：session 形态/延迟分级/对照闸门/队列态）
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
