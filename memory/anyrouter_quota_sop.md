# anyrouter.top 抢坑（slot）监控 SOP

一句话：anyrouter.top 的 Claude 上游**按号池供给**，余额再多也要命中空闲上游才返回 200，否则站点直接 429/503 `Service Unavailable`。正确姿势不是「充钱」而是**高频探测 + 命中即刻使用**。

## 1. 站点机制与判据（2026-10-03 实测）

| HTTP | 站点报文 | 判据 | 含义 |
|---|---|---|---|
| 200 | 正常 completion | `SLOT_AVAILABLE` | 命中空闲上游 ⇒ 立刻抢用 |
| 429 / 503 | `{"error":{"message":"Service Unavailable"}}` | `POOL_BUSY` | 号池满，本轮没抢到 |
| 400 | 「1m 上下文已经全量可用，请启用 1m 上下文后重试」 | `NEED_1M` | 未带 1m beta 头 |
| 400 | 「invalid codex request」（`code=invalid_responses_request`，~1.1s 瞬拒） | `CODEX_SHAPE_REQUIRED` | 该名只吃 **Codex 形状**（走 `/v1/responses` 且带组合键），见 §1.4 |
| 404 | 「当前 API 不支持所选模型」 | `MODEL_UNSUPPORTED` | 该名只走 OpenAI 面（需换名，重试无用） |
| 401 / 403 | 含 `unauthorized client` | `CLIENT_REJECTED` | UA 白名单门禁 |
| 401 / 403 | 其它 | `KEY_INVALID` | 令牌失效 |
| 其它 | — | `UNKNOWN` | 判据外，fail-closed |
| 无响应且 ≥8s | 控制探针也拿不到任何响应 | `LINK_DOWN` | **链路不可达**（网络/代理面故障；fail-closed 计故障、`rc=2`，见 §1.2(3)） |

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

**★2026-10-04 深夜订正（推翻「快返 ⇒ 形状/身份问题」的解读）**：拿**真 CC 原样请求**（95 KB body / 21 tools /
真 UUID `session_id`，源 `temp/anyrouter_watch/cc_full_request.json`）**连发 9 次**，结果**全是 `429 @1.5–2.3 s`**
—— 与探针（515 B、uuid5 身份）**同一量级、同一结论**。故：

- 「1–2 s 快返」**不是**形状/身份被拒的判据，就是站点**号池满**时的正常应答；把快返读成「网关形状门」是**归因错误**
  （探针与真 CC 待遇一致 ⇒ 形状早已不是分水岭）；
- 反向同样不成立：真 CC 有时**挂满 90–100 s 直到断开**，那是 CC **客户端内部重试 + 退避**（`attempt 1 of 10`）的表现，
  **不是**「过闸成功」的证据；探针单发无重试，耗时天然更短；
- 姿势自证命令：`python3 tools/anyrouter_slot_watch.py --verify-pose`（同一时刻分别打「探针体」与「真 CC 体」各 N 发，
  输出 `POSE_ALIGNED` / `POSE_DIVERGED`）—— 两者判决档位一致 ⇒ 姿势不构成差异，剩下只由号池决定。

### （3）新增观测维度与状态

- `elapsed_s` / `latency_class` 落台账：区分「瞬时拒（~1-2s）」与「过闸排队（≥`QUEUED_MIN_S`=8s，直到传输超时）」；
- `QUEUED_NO_UPSTREAM`：传输超时但已连上并挂满 ≥8s **且已证链路可达** ⇒ **过闸、只是上游无空闲槽**（≠链路故障，≠没额度）；
- `LINK_DOWN`（★2026-10-04 补）：传输拿不到响应（`http=0`）且挂满 ≥`QUEUED_MIN_S` 后，**同栈控制探针**
  （`GET /api/status`，任意状态码都算「拿到了响应」）也拿不到任何响应 ⇒ **链路不可达**（网络/代理面故障，
  fail-closed：计故障、`rc=2`、进故障通知）。**与 `QUEUED` 的差别只是证据**：拿不出「链路可达」的证据，
  就不许把「连不上」写成「在排队」；
- 探测并发化（`--workers`，默认 5）+ `--timeout` 默认 90s（旧值偏短会把「排队」误判成失败）。

### （4）本节结论之外的仍未证实项

「session id 保活 / 粘性」在 5 篇 L 站原文里 **0 命中**（见 §1.1）= **UNVERIFIED 不采纳**：uuid5 只是满足形态门禁
并保持稳定，不代表站点真的把号池坑位绑到该 id 上。（★本条的「机制未证实」不阻止做**行为层**兜底：见 §3.4 的
「命中即用同身份持续发请求」——它不依赖该机制成立。）

## 1.3 抢坑实操：社区做法 + 本机实测边界（2026-10-04）

社区（L 站）做法逐字摘要，语料 `temp/anr_lstation/2512543.md`（标题=「挤any的fable-5模型小技巧，和报错问题，叠甲（不一定有用）」）：

| 社区原话（逐字，引自该帖） | 实操含义 |
|---|---|
| 刚才我挤fable-5半天进不去，然后我换成了4.8，直接重试了两次就进去了 | 换模型名本身就是抢坑手段；同站不同名难度差极大（另见 2502646「any一般都是opus用不上，一直429，haiku没啥人用，gpt5.5倒是随便用」） |
| 然后我在cc里面把fable-5和4.8都配置上了，只要4.8挤进去让他对话着，新开一个对话窗口使用fable-5就可以了 | 先用易挤的名字占住，再切到目标模型 |
| 有的佬友用着用着会跳 | 紧随其后的报错文案是 `Server is busy. Retrying now (attempt 1 of 10)` ⇒ 属**号池重试**，不是额度没了 |
| 我的方法是使用claudeCLI版本的使用4.8开一个新的对话查看是不是能正常吐字，如果能就新开个话题一直聊下去，或者新开个项目，不要一直发你好一类的 | 判「挤进去」看有没有**正常吐字**；短促无意义请求不划算 |
| 如果你的上下文过长它会跳重试 | 长上下文自身会触发 retrying（与探针发的短请求不同口径） |

⚠️ 以上是**社区经验**（原帖自称「不一定有用」），非本机验证结论；本工具据此只做两件事：`--models` 扩名对照 + 判定语义区分。

本机实测边界（2026-10-04）：

- **身份形态先行**：`session_id` 必须像真 CC（见 §1.2），否则连号池都碰不到 —— 此时任何换名或重试都无效。
- **抬高超时无收益**：`--timeout 240` 单测 `gpt-6-astra-cc-format` ⇒ `http=0 QUEUED_NO_UPSTREAM (155.9 s/timeout)`，即挂到 155.9 s 后被断开、仍未转 200 ⇒ 默认 `--timeout 90` 维持，抢坑靠**轮次密度**而非单轮长挂。
- **形状非门禁：真 CC 体同刻同等待遇（2026-10-04 深夜）**：真 CC 原样 body（95 KB / 21 tools / 真 UUID）**9 发全 `429@1.5–2.3s`**，
  探针 `429@1.3s` ⇒ 体大小 / tools / 真 UUID 都不改变结局；「挂满 90–100 s」只出现在**带重试的 CC 客户端**里，非过闸证据。
- **形状不再是分水岭（2026-10-04 09:48 同刻 A/B）**：真 CC 原样 body（113945 B / 21 tools）`429@103.0s`、极简探针 body（515 B，uuid5）`429@19.8s`、真 CC body 仅换 uuid5 `session_id` `429@24.3s` —— 三发**都过闸并排队**（无 1–2 s 瞬拒）之后才吃到 429 ⇒ 修好 `session_id` 形态后，body 大小 / 有无 tools **都不再是门禁**；证据 `temp/anr_truth/shape_ab2_*.jsonl`。
- **凌晨那批 200 的归因（2026-10-04 04:52–05:09）**：站点消费日志里的 10 条成功 = **真容量窗口**内的「CC 形状复刻」实验（04:52:08 / 04:54:06 / 04:56:17 三条 `200`，耗时 35–37 s），**不是**本探针发的 —— 同刻 04:53 / 04:58 / 05:03 / 05:08 四轮探针全 429（2026-10-04 全天 117 轮 591 发**零 200**：503×448 / 429×140 / 挂超时×3），因为当时探针还在用旧自造 `session_id`（恒瞬拒），结构性看不见该窗口。修形后此类窗口即由探针正常捕获并推送。
- 命中口径：**200 且正文非空**才算抢到（唯一推送条件）；429/503/500「负载已经达到上限」= 号池满，不推送。
- 会话保活/粘性：5 篇原文 grep 零命中 ⇒ UNVERIFIED，不实现。

## 1.4 ★OpenAI 面模型的「Codex 形状」门禁（2026-10-04，探针已接线）

现象：`gpt-6-astra` 这类**只走 OpenAI 面**的名（在 messages 面 404），拿普通/极简 body 打 `/v1/responses` 会被站点 **400 `invalid codex request`**（`code=invalid_responses_request`，约 1.1 s 瞬拒）；与请求头无关（裸头同样复现）。**它既不是「没额度」也不是「号池满」，而是形状门禁**。

地面真值：隔离 `CODEX_HOME` 跑真 codex 0.157.1 抓包（`temp/anr_codex/real_body.json`，67282 B）⇒ 顶层键 = `model` / `input` / `tool_choice` / `parallel_tool_calls` / `reasoning` / `store` / `stream` / `include` / `prompt_cache_key` / `text` / `client_metadata`；头 = `originator: codex_exec` / `session-id` / `thread-id` / UA。

二分结论：**单独加任一键仍被拒**；极简 body + 这组键 ⇒ 过闸（之后挂满传输超时）。

修法（`tools/anyrouter_slot_watch.py` 已落地）：

- **两套「面」**：`FACE_MESSAGES`（`/v1/messages` + `anthropic-beta: context-1m-2025-08-07`）与 `FACE_CODEX`（`/v1/responses` + 最小过闸体 + codex 头）；
- `face_of(model)`：`MODEL_FACE` 显式登记优先（`gpt-6-astra`→codex、同名 `gpt-6-astra-cc-format`→messages），未登记 `gpt-*` 兜底 codex，其余 messages；
- **形状自愈**：任一面命中 `CODEX_SHAPE_REQUIRED` ⇒ 就地换 codex 面重打**一次**（只换一次，防无限重试），响应留痕 `shape_retry`；
- 新状态 `QUEUED_NO_UPSTREAM` = **过闸但上游无空闲槽**（连接建立后挂满超时，且同栈控制探针已证链路可达；≠链路故障、≠没额度），仍**不推送**；
- 新状态 `LINK_DOWN` = **链路不可达**（同栈控制探针也拿不到响应）⇒ fail-closed 计故障（`rc=2`、走故障通知），
  与 `QUEUED` 的差别是**证据**而非耗时：`elapsed_s` 只决定「要不要取证」，取证结果决定判 `QUEUED` 还是 `LINK_DOWN`；
- ★残余边界（无法在客户端侧区分）：站点把连接**挂住不给响应**（静默丢弃）时控制探针仍能拿到响应 ⇒ 仍判 `QUEUED`。

证据：`--selftest` 119 → 136 → **147 passed / 0 failed**；真站 2026-10-04 `--once --dry-run --models gpt-6-astra,claude-opus-4-8 --timeout 30` ⇒ `gpt-6-astra http=0 QUEUED_NO_UPSTREAM (30.7 s/timeout)`（旧码同刻是 400 瞬拒）、`claude-opus-4-8 http=429 POOL_BUSY (1.6 s/fast)`。

★运维：改判据/名单后**必须重启**（见 §3.3）；本次已按定时同款命令重挂 watchd。

## 1.5 ★「打开网页一直 403」的真实语义（2026-10-04 实测，与抢坑探针无关）

现象：浏览器打开 `anyrouter.top` 页面能显示，但页内满屏 `错误：Request failed with status code 403` / `Failed to load status`。

实测（真浏览器同源 fetch，2026-10-04）：

| 请求 | 状态 | 头 | 说明 |
|---|---|---|---|
| `GET /login`（页面本体） | **200** | 无拦截头 | 网页文件正常，不是「整站被封」 |
| `GET /api/status`、`/api/user/self`、`/api/pricing`、`/api/notice`、`/api/models` | **403** | `Server: ESA` + `X-Tengine-Error: denied by http_ratelimit` | 阿里云 ESA 边缘**按出口 IP 限流**，body 是 ESA 的 `403 Forbidden` 页 |
| `GET /v1/messages` | 404（JSON） | 无拦截头 | `/v1/*` **不被这条规则拦**（只是 GET 不是合法调用） |
| 异网旁证 `r.jina.ai/https://anyrouter.top/api/status` | **200 + 真 JSON**（公告数据） | — | 接口本身健康 ⇒ 403 是**我们这条出口 IP 的限流桶满了**，非封号、非没额度、非「客户端不是 CC」 |

判读铁律：

- **403(`denied by http_ratelimit`) ≠ 封号 ≠ 没额度 ≠ 客户端检测**；它是 CDN 的 per-IP 频次桶（本机出口是 Clash 共享节点，多人共用同一出口 IP）。
- 该 403 只落在 `/api/*`（控制台/用量/定价等前端接口）⇒ **只影响你打开网页看面板**，不影响抢坑：探针走 `/v1/messages`（POST，带 `anthropic-beta: context-1m-2025-08-07`）与 `/v1/responses`，日志里从来只有 `429/500/502`（号池满/上游），**没有出现过 403**。
- 处置：**换出口 IP 即恢复**（Clash 切节点，或用手机热点打开页面对照）；不换就等桶过期。切完仍 403 才是站点对 `/api` 面的全局收紧，只能等。
- 另有 `X-Tengine-Error: denied by http_custom`（python 裸请求 `/` 时见）= CDN **自定义规则**拦裸客户端，返回 200 + 4321 B 反爬 JS 挑战页（`acw_sc__v2`）⇒ 与上面的 `http_ratelimit` 是**两条不同规则**，勿混判。

## 2. 工具

`tools/anyrouter_slot_watch.py`（纯 stdlib + keychain）：

```bash
python3 tools/anyrouter_slot_watch.py --once            # 单轮探测（按名路由到 messages / codex 两面，见 §1.4）
python3 tools/anyrouter_slot_watch.py --once --notify   # 命中即推飞书私信 + 钉钉群
python3 tools/anyrouter_slot_watch.py --watch --notify --interval 300 --cooldown 21600 --max-minutes 1380
python3 tools/anyrouter_slot_watch.py --verify-pose     # 姿势自证：探针体 vs 真 CC 体同刻对照（--pose-n 控制发数）
python3 tools/anyrouter_slot_watch.py --selftest        # 177 项负控自测（2026-10-04 扩充：session 形态/延迟分级/对照闸门/队列态/Codex 面门禁/链路证据门与 link 标记）
```

- 退出码：`1`=有可用（抢到）/ `0`=全 POOL_BUSY / `2`=其它（全 UNKNOWN / 全 MODEL_UNSUPPORTED / KEY_INVALID 等，fail-closed）。
- 默认 `MODELS`（8 个，2026-10-04 扩面）：`claude-opus-4-8`、`claude-opus-4-7`、`claude-sonnet-4-5-20250929`、`claude-opus-4-6`、`claude-sonnet-4-20250514`、`claude-haiku-4-5`、`gpt-6-astra-cc-format`（messages 面）、`gpt-6-astra`（★codex 面，走 `/v1/responses`，见 §1.4）（**名单里一律不带 `[1m]`**，1m 只体现在请求头 `anthropic-beta`）；`--models a,b` 可临时换名单，`--models`（不带值）=列站点在册模型。扩面依据=2026-10-03 全 17 名标定取 messages 面可用代表名 + 2026-10-04 名测（`claude-opus-4-8`/`claude-haiku-4-5` 均在册=429 POOL_BUSY；`gpt-5.5`/`fable-5` 404 未在册）与 L 站攻略点名的「换 4.8」「haiku 没啥人用」。
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

## 3.3 改了名单/判据代码怎么生效（必须重启）+ 干净重启姿势（2026-10-04 实测）

- **运行中的实例不会自动吃到新代码**：watchd 只在启动时拉起**一次**子进程，子进程是长驻 `--watch` 循环 ⇒ 改 `tools/anyrouter_slot_watch.py` 的 `MODELS`/判据后**必须重启**才生效。生效判据=`temp/anyrouter_watch/watch.log` 里「watch 第 N 轮」之后的模型行数（8 名应连续打印 8 行；codex 面那行尾带 `[face=codex]`）+ `ledger.jsonl` 末行 `len(results)`。
- **http=0 的行尾还会带链路证据标记**（2026-10-04 加）：`[link=ok]`=同栈控制探针拿到 HTTP 响应（过闸排队，非我方链路故障）；`[link=down]`=控制探针也拿不到响应 ⇒ 判 `LINK_DOWN`（网络/代理面，rc=2 且走故障通知）；不加=该行耗时 < `QUEUED_MIN_S`(8s) 属快失败，未取证（不标证据就事后没法复核「连不上」与「在排队」）。
- **`bg_task stop` 会留下孤儿探测进程**（实测踩到）：stop 只向 wrapper 的进程组发 SIGTERM，而守护用 `start_new_session` 起探测子进程（自成 pgid）⇒ 守护死了、探测进程仍在跑并**继续持 `watch.lock`**。干净重启四步：

  ```bash
  python3 tools/bg_task.py stop --name anyrouter_slot_watchd                    # 1) 停守护
  ps -eo pid,ppid,pgid,etime,command | grep anyrouter_slot_watch.py | grep -v grep   # 2) 查残留
  kill -TERM -<pgid>          # 3) 仍有残留则按 pgid 精确停（pgid=该 pid；pid 也在 watch.lock 里）
  python3 tools/bg_task.py start --name anyrouter_slot_watchd -- /opt/anaconda3/bin/python3 tools/anyrouter_slot_watchd.py --max-hours 23   # 4) 重起
  ```

- 陈旧 `watch.lock`（指向已死 pid）**无需手工删**：新实例按 pid+lstart 判活后自动接管（实测守护 09:36:30 起、09:36:30 首轮即刷新锁为新 pid）。
- 换名单实例：2026-10-04 `MODELS` 5→7（加 `claude-opus-4-8`/`claude-haiku-4-5`）后按上述姿势重启 ⇒ 首轮 09:36:30 打印 7 行（7/7 POOL_BUSY），硬超时随之为次日 08:35（23h），与次日 08:50 定时接续只留 ~15min 空窗。
- ★换名单实例 2（同日晚）：`MODELS` 7→8（加 codex 面 `gpt-6-astra`）+ 探针改「按名路由两面」后重启；★两次踩到「孤儿探测进程持锁 ⇒ 新 watchd 只 HOLD_BUSY 不接管、旧码继续跑」⇒按 §3.3 四步姿势（停 watchd → 杀持锁探测 → 起 watchd → 核 `watch.lock` pid=新探测 + `watchd.jsonl` START 事件）才真正生效；生效判据还含「首轮 8 行」与 codex 面行尾 `[face=codex]`。

## 3.4 命中即自动抢坑 + 保活 30 分钟（2026-10-04 加装）

只有「命中→通知」时，人不在场坑位照样白抢（用户原话：**枪没上膛**）。本节是补齐的自动段。

- **触发**：任一模型判 `SLOT_AVAILABLE`（200）即触发；触发点与通知同分支但用 `try/except` 包裹 ⇒ **保活出任何异常都不影响探测主循环**。
- **占坑身份**：复用**探测自身的 session**（同 key/tag 派生的 uuid5 + 同 UA/1m 头）——即「刚过闸的那条通路」，不二次握手、不改形状。
- **保活动作**：后台线程按间隔对同（模型、session）发**真实最小 completion**，把该 session 的活跃窗口撑住 `--auto-hold-minutes`（默认 **30**）分钟；`--hold-cooldown`（默认 **900s**）内不重复开新一轮。
- **终止**：被踢（非 200：429/503/401…）或窗口到期即停，**绝不无限续期**。
- **通知文案**：命中推送显式写「**已自动保活 N 分钟**」，并**不再**出现「尽快连着用」这类要人盯的措辞（未开保活时该措辞保留）。
- **通知里给「怎么用」**（2026-10-04 本轮加）：命中推送**直接印出保活所用身份的完整 `session_id`**（uuid5，key/tag 派生）+ 一行可粘贴的复用命令 `claude --session-id <uuid>`，并注明「不指定 session 也照常能用；指定=复用这个正在被保活的身份」。实现=`_build_text(..., sid=...)` 形参 + `run_once()` 传 `session_id_for(key, "anyrouter-probe")`；不传 `sid` 时文本逐字不变。⇒ 收到推送即可原地接上，无需再问「保活的是哪个身份」。自测随之 197 → **200 passed**（三断言：带 sid 出完整 UUID / 出可粘贴命令 / 不带 sid 无 `--session-id`）。
- **CLI / 透传**：`tools/anyrouter_slot_watch.py --auto-hold-minutes 30 --hold-cooldown 900`；`tools/anyrouter_slot_watchd.py` 同名参数并在 `spawn_watch()` 透传（守护默认起 `--auto-hold-minutes 30`）。
- **自测**：watch `--selftest` 197/197（含被踢即停 / 零发 / `minutes=0` 关闭 / 脏值负控）；watchd `--selftest` 27/27。
- **边界**（与 §1.2(4) 的关系）：保活**不假设**站点把坑位绑到 session id 上；它只是「以已过闸的同身份持续发请求」这一**行为层**兜底。§1.2(4) 的 UNVERIFIED 结论不变（那是「机制是否存在于站点侧」），本节的效力仅来自「同一个已过闸身份保持活跃」。
- **成本**：30 分钟窗口 + 900s 冷却 ⇒ 额外额度消耗**非零但有限**（已登记）。
- **残余**：① 号池满时保活无解（它不能创造上游供给）；② 若站点将来改为每请求强绑 session，保活会与被探测请求脱钩，须重新标定。

## 4. 排错

- 全 POOL_BUSY 且长期无 200：先查 `/api/log/self` 是否有成功记录；再试换 kind（`gpt-6-astra-cc-format[1m]`）。
- 启动即「已有实例在跑」：`python3 tools/bg_task.py status --name anyrouter_slot_watch --json` 核对 pid/lstart；确为陈旧进程再 stop。
- 通知未到：查 `memory/notify_channels_sop.md` 的通道口径与体积上限；`--dry-run` 可零推送验证链路；故障通知自检=`python3 tools/anyrouter_slot_watch.py --once --fault-streak 1 --notify`（会真发）。
- 子进程莫名没了/不产出：看 `temp/anyrouter_watch/watchd.jsonl` 的 RESTART_* 事件；守护本身不在跑则重新起（`bg_task start --name anyrouter_slot_watchd`）。
- 长驻期间「一轮都没推」不等于没故障：默认 3 连败才报（`--fault-streak`），要更灵敏就调小。
