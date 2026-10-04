# R537 anyrouter 探针判据订正与 L 站攻略核对

日期：2026-10-04 ｜ 触发：用户问「首先你这个判断逻辑是否正确啊 你去搜下 L 站 有没有什么攻略」

## 1. 一句话结论

**判据方向对、口径偏紧且有缺口**：`200=SLOT_AVAILABLE / 429·503=POOL_BUSY` 与 L 站社区共识一致；
但（a）站点也用 `500 + 「负载已经达到上限」` 表达同一件事，原表落 `UNKNOWN`；
（b）我今天反复遇到的 `HOLD`（连接挂着、无字节、~150s 被断）在社区有正解——**可能是慢响应/重试中**，
被我按「失败」处理了；（c）社区反复强调「挤进去 ≠ 能蹬上」，命中号池后照样零产出烧额度，
故「可用」必须收紧为**拿到 200 且正文非空**。已把这些折进 SOP §1.1。

## 2. 取证路径（可复跑）

- 贴页匿名可读通道：`r.jina.ai` + 本机 Clash 代理（浏览器贴页/JSON 端点被 CF 拦，jina 可匿名取公开帖正文）。
  ```bash
  curl -sS --max-time 80 -x http://127.0.0.1:7897 "https://r.jina.ai/https://linux.do/t/topic/2512543"
  ```
- 语料存档：`temp/anr_lstation/`（本会话 10 文件 / 76698B）。
- 引文契约：本报告与 SOP 里的引号内文字均从上述语料**逐字回源**（脚本 grep 校验，含负控式 MISS 检查；
  反例见 §4「未证实项」——`session id`/`保活`/`粘性` 三个 token 在 5 篇原文里**全 MISS**）。

## 3. 逐字引文 ↔ 判据对账

| 社区原话（逐字） | 出处 | 对本工具的订正 |
|---|---|---|
| 不要不要不要不要不要只看响应码数字！！！！要看响应码＋Error的具体信息！！！别丢个数字直接对号入座！ | [1809254 Any响应码错误及常见问题大全](https://linux.do/t/topic/1809254) | 判据须 **码 + message 文本**合看 |
| 目前的大部分的nobday（400/403/503）以及负载上限都是上游问题，反复重试和切终端切模型即可！！！ | 同上 | 429/503/500「负载上限」=上游供给态，**非**「没额度」 |
| 大概率是限流，一直尝试重连，10次以后会显示服务不可用。但并不代表any无法使用 | [2267684 配置合集](https://linux.do/t/topic/2267684) | 与 `429→POOL_BUSY` 一致；单次 429 推不出「不可用」 |
| 上下行流量太大，长上下文响应时间几乎线性增大，10-150s都有可能，再大一些的上下文，甚至会触发retrying，但是往往挂着重试，也会缓慢继续响应 | 同上 | **HOLD 的正解**：慢响应/重试中，150s 判死偏激进 |
| any一般都是opus用不上，一直429，haiku没啥人用，gpt5.5倒是随便用 | [2502646](https://linux.do/t/topic/2502646) | **模型维度**：结论只对当轮 `--models` 名单成立 |
| 刚才我挤fable-5半天进不去，然后我换成了4.8，直接重试了两次就进去了 | [2512543 挤坑小技巧](https://linux.do/t/topic/2512543) | 换名/换模型本身即抢坑手段 |
| 如果你的上下文过长它会跳重试 | 同上 | 短请求探针 ≠ 用户长会话可用 |
| 晚上八点多好不容易挤进去了，很开心的等待结果，结果额度一直在消耗，花费了16刀，但是一直没返回结果，最终还是retry了十次之后GG了 | [2524731 挤进去了却又实际没蹬上](https://linux.do/t/topic/2524731) | **「挤进去」≠「能蹬上」**：零产出烧额度 ⇒ 见下口径 |
| 开个新的session对话就没问题，只有长上下文/compact不行，请教怎么fix这个问题？ | [2919522](https://linux.do/t/topic/2919522) | 与「长上下文/compact 易 429」现象同源 |

顺带取到站点自述：`500 {"type":"api_error","message":"当前模型 xxxxx 负载已经达到上限，请稍后重试 "}`
与 `429 {"error":{"type":"new_api_error","message":"当前模型 claude-opus-4-6 负载已经达到上限，请稍后重试 "}}`
——**同一件事两个码**（引自 1809254 帖内报错原文），这是我原判据表漏 `500` 的直接证据。

## 4. 未证实项（已标记 UNVERIFIED，不采纳）

流传的「写脚本统一/保活 session id；挤进去后定时发同 session id 的请求（内容 `1`）保活」——
在本轮抓取的 5 篇原文里 `session id` / `保活` / `粘性` **零命中**，我**没有**找到该说法的源头帖，
故不作为机制写入 SOP、更不据此改工具。

## 5. 落地与待批

- 已落地（本轮）：SOP `memory/anyrouter_quota_sop.md` 新增 §1.1（L 站攻略核对 + 判据修正 + 未证实项），
  语料存 `temp/anr_lstation/`。
- 已落地（本轮）：定时任务 prompt 同批同步 500 语义（`sche_tasks/anyrouter_slot_watch.json` 一处文本），
  `python3 tools/sche_tasks_lint.py --only anyrouter_slot_watch` ⇒ FAIL 0 / WARN 0 / OK 1，verdict PASS。
- 待批台账：`temp/pending_decisions/DECISIONS_R537_anyrouter_criteria.md`（D1–D3，均 revers=0）。
- **代码层：1 项已实现 / 2 项待批**（`tools/anyrouter_slot_watch.py`，同批提交本提交）：
  1. ✅ **已实现**：`classify()` 加前置分支 ⇒ `500` 且 message 含「负载已经达到上限/上限/overload/capacity」归 `POOL_BUSY`；
     反例（裸 `500 Internal Server Error`）仍回落 `UNKNOWN`，selftest 93→**95/95**；
  2. ⏳ 待批：`HOLD` 单列 `HELD_SLOW`，不打「无可用」结论、不推送、不并入 `POOL_BUSY`/`UNKNOWN`（现仍落 `UNKNOWN`）；
  3. ⏳ 待批：`SLOT_AVAILABLE` 口径收紧为「200 且正文非空」（现仅看 HTTP 码）；
  另建议在 `MODELS` 里加一个社区公认易挤的对照名（如 `claude-3-5-haiku`），避免把「opus 挤不进」
  读成「站点没额度」。
- 与今日实测的关系（§6 之外）：`HOLD` 在社区语义里既可对应「挤进去但没蹬上」（2524731），
  也可对应「并发高/上下文长本来就慢」（2267684）——两者都无法从连接层区分，故只能并成一态并**禁作负判**。

## 6. 今日实测快照（判据订正的事实依据，2026-10-04）

- 真 Claude Code 原体经本机代理（leg2=urllib）+原头 → 一次**真 200 流（正文含 `OK`）**；同形复放随后恒 HOLD ⇒ 时变性。
- `dump 原体 ×6` → HOLD 5 / 429 1；`最小工具体 ×6` → 429 6/6；`全量原头复放 ×4` → HOLD 4/4。
- 150s 长超时 3 路 → 全部 RemoteDisconnected（≈149s）。
⇒ 结论：单点单形请求**不足以判定站点可用性**，与 §3 社区共识吻合。

## 7. 校验

- 引文回源：SOP §1.1 与本报告引号内容逐字来自 `temp/anr_lstation/`（grep 命中，校验器自测含 MISS 负控）。
- 未证实项的反向断言：`session id`/`保活`/`粘性` 在该语料中 MISS（若将来命中，须重开该议题）。

## 8. 复测订正（2026-10-04 晚）：探针自伤的根因 = `session_id` 形态被网关切掉

本节推翻了 §1.1 之后的**全部探测读数**，是本次真正值钱的发现。

### 8.1 根因（单变量对照，铁证）

同一最小形状，只换 `metadata.user_id` 里的 `session_id` 一段：

| 形态 | 结果（`temp/anr_truth/truth5_*.jsonl`） |
|---|---|
| 真 CC dump 的 uid | 2/2 传输超时、挂满 61.3s / 60.6s（= 过闸、走上游排队） |
| 任意 uuid4（`only_sess_uuid` / `sess_uuid2`） | 2/2 传输超时 60.7s / 60.6s（同上） |
| 旧实现自造 91 字符长串（`only_dev_real`，**只有 session 是旧形**） | 1/1 `429` @ **1.9s**、`{"error":{"message":"Service Unavailable"...}}` |

ABBA 交错复验（`temp/anr_truth/truth7_20261004_092409.jsonl`，同模型 `gpt-6-astra-cc-format`）：
`P1_uuid` 超时 12.6s → `P2_old` 429 @1.1s → `P3_old` 429 @1.1s → `P4_uuid` 超时 12.6s
—— 形态效应**与顺序无关**，正是单变量对照该有的样子。

⇒ 网关门禁落在 `session_id` **是不是 UUID 形**上；`device_id` / `account_uuid` 都不是门禁（真 CC 的
`account_uuid` 同样是空串）。「快返 429」是**身份被拒**，不是号池态。

### 8.2 影响面

`build_probe_body()` / `build_claim_body()` 一直走 `session_id_for()` 自造长串 ⇒ 自本工具 10-03 落地起，
**每一次探测都被自己的身份格式拒掉**：当日台账 648 次 503 + 173 次 429 全是该 bug 的产物（不是「没坑」，
更不是「没额度」）；抢坑宣称路径同样结构性死。

### 8.3 修法与判据收紧（已落盘，selftest 95→119）

1. `session_id_for()` → `uuid5(NAMESPACE_URL, "anyrouter|<sha256(key)>|<tag>")`：满足 UUID 形态且对
   (key,tag) 确定 ⇒ 跨进程/跨轮稳定（抢坑与保活复用同一身份）；key 只进 sha256，不落盘明文。
2. 「全模型快返」不再单独构成 `GATE_REJECT`：必须有**对照探针**（真 UUID 形身份）**同样**快返才判；对照能走上游
   ⇒ 快返归 `POOL_BUSY`（真号池态）。对照身份取 `ANR_GATE_CONTROL_UID_JSON` → 本机真 CC dump；无对照 ⇒ 该维度
   `unknown`，**不得**凭空升级为 `GATE_REJECT`（`ANR_GATE_CONTROL_UID_JSON=none|off|0` 可显式禁用）。
3. 新增 `elapsed_s` / `latency_class` 落台账 + `QUEUED_NO_UPSTREAM`（传输超时但已连上并挂满 ≥8s ⇒ 过闸、上游无空闲槽）；
   `--timeout` 默认 90s、`--workers` 默认 5。

### 8.4 与 §1.1 的关系（不要把两件事混起来）

§1.1 讲的是**站点语义**（429/503/500 都属「号池供给态」，非没额度）——修正后仍然成立；
本节讲的是**我方探针自伤**（身份格式非法 ⇒ 连站点的号池都没碰到）。两者叠加才解释得通「明明有额度却全 429」。
`session id` 保活/粘性仍属 UNVERIFIED（5 篇原文 0 命中），uuid5 只是**满足门禁 + 保持稳定**，不代表站点真的绑定坑位。
