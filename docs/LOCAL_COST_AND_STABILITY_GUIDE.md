# 本机 GA 成本与稳定性口径手册（慢 / 费 / 断 / 质量差）

> **这是本机口径文档，不是上游文档。** 记录 2026-10-12 对本机 GenericAgent“任务又慢又费 token、跑不完、质量差”的排查结论、最新确认的配置口径、已落地的改动与回退、运维与自检手册。
> 上游的 `USAGE_GUIDE.md`、`mykey_template.py` 与代码有多处默认值不一致，**以代码为准**（见 §2.1）。
> 时间一律为 Asia/Shanghai (UTC+08:00)。标“估”的数字是推算，不是账单。
> 完整复盘报告（含证据推导过程）在 OB 知识库：`~/develop/obsidian-note/70 Output/2026-10-12-ga-slow-and-token-waste-postmortem.md`。

| 项 | 内容 |
|---|---|
| 版本 | 2026-10-12 v1 |
| 适用 | 本机 `~/projects/GenericAgent`，飞书 / 钉钉 / GA-admin / 三个反射进程，mixin 十槽 |
| 相关资产 | `plugins/ga_guard.py`、`tools/l1_compact.py`、`tools/ga_diag.py`、`memory/l1_detail_archive.md`、L0 `memory/memory_management_sop.md` 新增一行 |
| 未提交 | 以上改动均未做 git 提交 |

---

## 0. 一页纸结论：这次为什么费 token、慢、完成质量差，怎么解决

**不是单一原因，是五个独立问题叠加。上下文大小只是放大器，不是根因。** “老断，我只能一直喊继续”的判断被数据证实。

### 0.1 问题、原因、解决方案

| # | 问题 | 现象 | 根因（证据） | 解决方案 | 状态 |
|---|---|---|---|---|---|
| P1 | **慢** | 每轮等很久 | 第三方中转站每轮固定 26–50 秒，官方直连 2–11 秒（同等输出长度对比，10-10 至 10-12 约 3000 轮实测：官方 671、中转 2356）。10-10 17:55 三个会话同时卡 70–95 分钟：mixin 逐槽串行试 10 个槽，`read_timeout=300` 只是空闲超时，没有单轮总时限，叠加当时出网链路故障 | 官方 `deepseek-flash` 放首位可根治 | ⏸ 你决定不调顺序；`read_timeout` 降到 90–120、加单轮总时限也未做 |
| P2 | **跑不完 / 要喊继续** | 任务做到一半停了 | 10 个槽全部失败后，`!!!Error` 文本没有工具调用，被当成最终回复，任务就此结束。21 个会话段 13 段这样结束；10-10 起 23 次，HTTP 524 ×11、断流 ×7、SSL/代理/连接 ×5。错误后到下一次成功响应中位 22.4 分钟，没有一次短于 60 秒 | 不做自动重试（几秒的有限重试救不回；无限重试无意义；还需替换核心函数） | ⏸ 接受现状，用 `ga_diag.py breaks` 监控 |
| P3 | **费 token + 质量差** | 每次调用输入很大 | L1 记忆索引膨胀到 25.3 万字节（约 6.6 万 token，估），每次调用都发，每 10 轮再整份注入一次。128 行带 `R###` 编号的任务记录占 78%，违反 L0 “严禁写日志记录”，且无校验 | L1 瘦身为“名称+触发词+锚点”，细节无损入归档；插件校验反馈；L0 加一行规则 | ✅ 已落地 |
| P4 | **费 token** | 上下文几乎不裁剪 | `context_win` 被当成窗口大小，8 个槽配 1M；它实际是**裁剪触发点**，1M ≈ 不裁剪。来源：10-07 的 GA 配置审计在“`context_win` 兑现 1M”的理解下补入（§0.4） | 12 个槽统一为官方推荐的 80000 | ✅ 已落地 |
| P5 | **费 token + 跑不完** | 单任务 665–905 轮 | 任务范围膨胀：`489708` 从“配 token”变成逆向和排查风控；`813791` 修完真问题后又写报告、改 SOP、做汇报器、提交（约 41% 轮次是这类动作）；每次错误中断后“继续”还要补做 | 任务开头划边界、做完 `/new`（§8） | ⏸ 需要使用习惯配合 |

### 0.2 “质量差”是怎么来的

1. L1 每次整份注入约 16 万字符，已占 80000 触发点（24 万字符）的约 68%，两份就超限，**强制硬删**历史：日志里约 17% 的采样出现 `Trimmed context`。被删的原始工具输出找不回来，agent 只能重读文件、重新探测，步数继续膨胀。
2. 请求失败终止后的“继续”，是在一个被截断的上下文里补做被打断的步骤。
3. 任务范围膨胀后，单会话越长，早期关键信息越容易被压缩掉。
4. 瘦身后的风险：细节要按锚点读归档，agent 若不去读，可能重复踩坑——**没有做导航 A/B，需观察**。

### 0.3 费用结论

10-08 至 10-11 共 3535 次调用，L1 每次多发约 56K–59K token，合计约 206M。按假设单价估算浪费约 **¥60–¥93（缓存命中 86%–95%，官方公开价口径）**，上限约 ¥411（中转站不支持缓存）。**金额是假设推算，必须用各平台控制台核对**（§5）。

### 0.4 配置与问题的时间线（“以前没这么慢”对应什么）

| 时间（Asia/Shanghai） | 事件 | 与本次问题的关系 |
|---|---|---|
| 09-21 | deepseek 曾设 `context_win=100000`，131 轮堆到约 28 万字符，首 token 极慢像“卡死”，撤回到 80000（`mykey.py` 注释） | 官方早已踩过大 `context_win` 的坑 |
| 09-23 起 | L1 记忆索引开始快速膨胀（33KB → 10-12 253KB） | P3 |
| 10-04 | `wududu` 中转提到首位，官方 `deepseek-flash` 降到第 6 位（省公司 key） | P1 的前提 |
| **10-07 06:44–06:50** | **GA 配置审计**：审计前这些槽的 `context_win`、`max_tokens`、`reasoning_effort` **全部没配**（走代码默认值）；审计后 8 个槽 `context_win=1000000`（当时的结论是“兑现 1M 的正确写法”，**该结论已被推翻**）、所有槽 `reasoning_effort=high`、补 `max_tokens`（deepseek 384000） | P4 的来源；`high` 与 384000 对延迟的影响**未证实** |
| 10-09 17:52 | 加入 `wzw` 槽 | – |
| 10-10 17:53 | 首槽改指本机 magpie 网关 `group/sharedchat-pool`；17:55 起三个会话同时卡 70–95 分钟 | 时间相邻，**因果未证实** |
| 10-12 | 本次整改 | – |

关于 10-07 审计引入的 `reasoning_effort=high` 与 `max_tokens=384000`：官方 `deepseek-flash` 在同样参数下单轮中位只有 3–4 秒，所以它们**解释不了**中转站的 26–50 秒；是否对中转站有额外影响没有对照数据（10-07 之前没有逐轮时间戳），可用运行时 `/session.reasoning_effort=medium` 对单个槽试一下。10-07 的审计文档本身有价值（`connect_timeout` 是死键等发现仍成立），**只有“`context_win=1000000` 兑现 1M”这一条是错的**，已在该文档加更正说明。

---

## 1. 口径对齐（最新确认的说法 ↔ 曾经的误解）

| 概念 | 最新确认的口径 | 曾经的误解 / 常见误读 |
|---|---|---|
| `context_win` | GA 内部的**历史裁剪触发点**。字符阈值 = `context_win × 3`。**不是**上下文窗口大小，不会发给模型，也不限制模型接收多少 | 以为是“上下文窗口”，配 1M 就是开 1M |
| 真正的 1M 窗口 | Claude 槽靠模型名后缀 `[1m]`（发送时去掉后缀并加 `context-1m` beta 头）；GPT、GLM 等 OAI 兼容槽 GA 不发任何开关，窗口由模型和中转站决定 | 给 OAI 槽加 `[1m]` 也有效 |
| GA 与 Codex / Claude Code 的差异 | GA 直接配**压缩触发点**，窗口大小没处配，要你自己确认；GA 的压缩是有损截断，不做摘要 | 以为同一个旋钮 |
| 80000 | deepseek 的**代码默认值**（`llmcore.py` 对 deepseek 特判），也是 GA 开发者的建议（“别开 1M，80k 够了，用步数换”，来自用户提供的聊天截图，文档里无出处）。它是配置值，不是 8 万 token | 以为是被放大后的值 / 以为等于 8 万 token |
| token 与字符换算 | GA 默认按 1 token ≈ 3 字符设计。**本机日志粗拟合：约 0.47 token/字符 + 固定开销**（§2.5），换模型会变 | 先按 3.3 字符/token 估 |
| L1 | 每次调用都发的**极简索引**。官方 L0：≤ 30 行、< 1k token、只写关键词和路径、严禁写细节和日志记录 | 以为是可以随意补登任务记录的地方 |
| 请求失败 | mixin 每槽只试一次（槽内重试被强制置 0），全槽失败后返回错误文本，**任务结束**，需人工“继续” | 以为 `max_retries=3` 会重试 |
| 80000 对应的实际 token | 触发点 24 万字符 ≈ 11.3 万 token 历史 + 固定开销（§2.5） | – |
| 重试要不要做 | **不做**。数据：恢复中位 22.4 分钟 | 一度打算做整轮重试（需替换 `raw_ask`，违反“不替换官方核心”约束，已删除） |
| 槽顺序 | **不动**。每次调用的 token 数与顺序无关，顺序只决定失败时谁先接、谁付费 | – |
| 插件约束 | 不替换官方核心函数；只用官方 `plugins.hooks`；插件本身不写文件 | 第一版用猴子补丁，已废弃 |

---

## 2. 配置参数全表

### 2.1 渠道参数

“官方推荐”只列模板和文档里明确写了的；没写的标“无”。默认值以代码（`llmcore.py`）为准。

| 参数 | 作用 | 代码默认 | 模板 / USAGE_GUIDE 写法 | 官方推荐 | 本机现状 |
|---|---|---|---|---|---|
| `apikey` / `apibase` | 鉴权与端点；`sk-ant-` 用 x-api-key，其余 Bearer | 必填 | 必填 | 必填 | 已配（不记录在文档） |
| `model` | 模型名；`[1m]` 后缀触发 1M beta（仅 Claude 槽） | `''` | 同 | 带 `[1m]` 才有 1M | 仅 2 个 Claude 槽带 `[1m]` |
| `name` | 展示名，同时是 mixin 的引用名 | 取 `model` | 同 | 无 | 已配 |
| `proxy` | 单槽代理；不填则不走 | 无 | 同 | 无 | 未配（走系统代理 `127.0.0.1:7897`） |
| `context_win` | **裁剪触发点**（§2.5） | 38000；deepseek 80000 | 30000（deepseek 70000）/ 28000 | GA 开发者建议 80k | **12 槽 80000**；`agnes` 未配 |
| `trim_keep_prefix` | 硬删时保留最前 K 条 | 0 | 0 | 无 | 未配 |
| `max_retries` | 单槽重试次数（429/5xx） | 4 | 默认 1（示例写 3） | 无 | 槽内 3，**在 mixin 下被强制置 0，无效** |
| `max_retry_after` | 服务端 `retry-after` 超过它就放弃该次重试 | 60 秒 | – | 无 | 未配 |
| `timeout` | 连接超时（**真正读取的键名是 `timeout`**，模板写的 `connect_timeout` 不生效） | 流式 5 / 非流式 10 | `connect_timeout` 5 | 无 | OAI 槽 10–15；Claude 槽未配 |
| `read_timeout` | 流式**读取空闲超时**，不是单轮总时限 | 流式 40 / 非流式 240 | 30 | 中转渠道 120–300 | 300 |
| `stream` | 是否 SSE 流式 | True | True | CDN 截断 SSE 时才设 False | 未配 |
| `verify` | TLS 证书校验 | True | – | 无 | 未配 |
| `api_mode` | `chat_completions` 或 `responses`（仅 OAI 槽） | `chat_completions` | 同 | 无 | `chat_completions` |
| `api_key_header` | 鉴权头风格 | `auto` | – | 无 | 未配 |
| `fake_cc_system_prompt` | 反代 Claude Code 协议的渠道必须开（仅 Claude 槽） | False | 反代渠道置 True | 反代渠道置 True | 两个 Claude 槽均为 True |
| `user_agent` | UA（仅 Claude 槽） | `claude-cli/2.1.280` | `claude-cli/2.1.113` | 被 UA 白名单拒绝时可 pin 老版本 | 未配 |
| `extra_sys_prompt` / `_file` | 追加系统提示 | 空 | – | 无 | 未配 |

### 2.2 推理与采样参数

| 参数 | 作用 | 默认 | 官方推荐 | 本机现状 |
|---|---|---|---|---|
| `reasoning_effort` | 思考强度：none / minimal / low / medium / high / xhigh / max | 无 | 无硬性推荐 | **所有槽 `high`** |
| `thinking_type` | Claude 思考块：adaptive / enabled / disabled | 无 | adaptive 为 Claude Code 默认 | 未配 |
| `thinking_budget_tokens` | 仅 `enabled` 时生效 | 无 | low≈4096、medium≈10240、high≈32768 | 未配 |
| `omit_thinking` | 历史里不保留思考块 | False | 无 | 未配 |
| `temperature` | 采样温度 | 1 | 精确任务 0.3–0.5，创意 1.0 | 未配 |
| `max_tokens` | 单次回复上限 | 无（模板写 8192） | 长输出调到 16384–32768 | deepseek 槽 384000，其余 128000–131072 |
| `service_tier` | auto / default / priority / flex | 无 | 无 | 未配 |

`reasoning_effort=high` 与 `max_tokens=384000` 会让每轮思考和输出更长，可能是中转站延迟变大的原因之一；**未对比 `medium`，不能下结论**。运行时可用 `/session.reasoning_effort=medium` 对当前槽临时试，换模型或重启后失效。

### 2.3 mixin（故障转移）参数

| 参数 | 实际含义 | 代码默认 | 模板 | 本机 |
|---|---|---|---|---|
| `llm_nos` | 槽顺序，按 `name` 或序号引用 | 空 | 空 | 10 个槽（§2.4） |
| `max_retries` | **最多尝试 `max_retries + 1` 个槽**（`candidates[:retries+1]`），不是同槽重试 | 3 | 10 | 10 |
| `base_delay` | 模板称“退避起始延迟”；代码只在 `llmcore.py` 保存了该值，**我没找到使用处，疑似无效** | 3.0 | 0.5 | 0.5 |
| `spring_back` | 切到备用槽后多久回到首槽 | 300 秒 | 300 | 120 |

补充行为：认证类错误（含 401、403、unauthorized、forbidden 等字样）会让该槽在**本进程内永久禁用**，直到会话重建；日志累计 `sharedchat`、`wududu` 各被禁用 7 次，`all backends are disabled` 14 次（无逐行时间戳，不确定发生在哪些天）。

### 2.4 本机 13 个渠道实配表

| mixin 顺序 | name | model | 主机 | 类型 | `context_win` | `max_tokens` | `timeout` | `read_timeout` |
|---|---|---|---|---|---|---|---|---|
| 1 | sharedchat-gpt-5.6-sol | group/sharedchat-pool | 127.0.0.1:3425（本机 magpie 网关路由组） | OAI | 80000 | 128000 | 10 | 300 |
| 2 | deepseek-wududu | deepseek-v4.1-flash | wududu.edu.kg（第三方中转） | OAI | 80000 | 384000 | 15 | 300 |
| 3 | deepseek-metapi | DeepSeek-V4.1-Flash | metapi.lilililwan.xyz | OAI | 80000 | 384000 | 15 | 300 |
| 4 | glm-metapi | GLM-5.3-Flash | metapi.lilililwan.xyz | OAI | 80000 | 131072 | 15 | 300 |
| 5 | deepseek-wzw | deepseek-v4.1-flash | wzw.pp.ua（第三方 new-api 网关） | OAI | 80000 | 384000 | 15 | 300 |
| 6 | deepseek-flash | deepseek-flash | api.deepseek.com（官方，公司 key） | OAI | 80000 | 384000 | 15 | 300 |
| 7 | sol-4router | gpt-6.1-sol | 4router.net | OAI | 80000 | 128000 | 10 | 300 |
| 8 | gpt-4router | gpt-5.6-terra | 4router.net | OAI | 80000 | 128000 | 10 | 300 |
| 9 | sonnet55-4router | claude-sonnet-5-5[1m] | 4router.net | Claude 原生 | 80000 | 128000 | 默认 | 300 |
| 10 | opus55-4router | claude-opus-5-5[1m] | 4router.net | Claude 原生 | 80000 | 128000 | 默认 | 300 |
| 不在 mixin | luna-4router | gpt-6-luna | 4router.net | OAI | 80000 | 128000 | 10 | 300 |
| 不在 mixin | gpt-6-astra-hxi | gpt-6-astra | runanytime.hxi.me（10-04 起持续 403，已移出） | OAI | 80000 | 128000 | 15 | 300 |
| 不在 mixin | agnes-2.5-flash | agnes-2.5-flash | api.agnes-ai.cn | OAI | 默认 38000 | 默认 | 15 | 300 |

要点：
- `deepseek-v4.1-flash` 这个模型名同时对应 wududu 和 wzw 两个中转站，**响应日志里无法区分**。
- 槽顺序、`read_timeout`、重试均未动；只统一了 `context_win`。
- 2026-10-04 起官方 `deepseek-flash` 被放到第 6 位以减少公司 key 消耗，这是已知的取舍：中转站慢，官方快。

### 2.5 `context_win` 的换算与取值

**公式**（`llmcore.py` 的 `trim_messages_history` 与 `BaseSession`）：

| 项 | 公式 |
|---|---|
| 开始裁剪的阈值 | 历史字符数 > `context_win × 3` |
| 裁剪后目标 | 阈值 × `trim_keep_rate`（默认 0.6；模型名含 deepseek 时代码写死 0.3，不可配置） |
| 倍率 `m` | `clamp(context_win ÷ 基准 × 0.75, 1, 3)`；基准：deepseek 80000，其余 38000 |
| 轻压缩间隔 | 基准 × `m`；基准：deepseek 25 轮，其余 8 轮 |
| 工具输出截断上限 | 基准长度 × `m`（部分工具按 0.5 增长率） |

倍率封顶为 3：非 deepseek 槽配到约 15.2 万、deepseek 槽配到约 32 万以上，再加大只会推迟裁剪，不再放大工具上限。

**换算成 token（本机粗拟合，换模型会变，须用日志重新校准）**：

输入 token ≈ 0.47 × 历史字符数 + 固定开销 F。F 是系统提示、工具定义、工作记忆与 L1 的总和：

| 阶段 | F（估） | 依据 |
|---|---|---|
| 瘦身前 | 约 7 万 token | 回归截距，其中 L1 约 6.6 万 |
| 瘦身后 | 约 1.1 万 token | L1 约 6.6K + 系统提示/工具约 4K |

| 目标：想在多少 token 处裁剪 | 瘦身后应配的 `context_win` ≈ (目标 − 1.1 万) ÷ 1.41 |
|---|---|
| 1M 窗口、70% 处（70 万） | 约 49 万 |
| 1M 窗口、80% 处（80 万） | 约 56 万 |
| 400K 窗口、70% 处（28 万） | 约 19 万 |
| 200K 窗口、70% 处（14 万） | 约 9 万 |

**本机取值：80000。** 它在瘦身后的触发点约为 12.4 万 token 输入（瘦身前约 18.3 万），裁剪后目标：deepseek 槽约 4.5 万、其余槽约 7.9 万（均为估）。若某个槽的真实窗口小于触发点，请求会先撞窗口被拒；GA 不会自动探测窗口，**配置前必须向模型和中转站确认**。

---

## 3. 记忆 L0–L4 与 L1 规范

### 3.1 官方指标与本机现状

| 项 | 官方 L0 / 整理 SOP | 本机现状（2026-10-12） |
|---|---|---|
| 行数 | ≤ 30 行（硬约束） | 207 行 |
| 体积 | < 1k token（期望） | 16277 字符 ≈ 6.6K token（估） |
| 内容 | 只写关键词和路径；括号只放反直觉触发词；**严禁写细节，更加严禁写日志记录** | 全索引版：每个条目保留“名称 + 触发词 + 锚点”，细节在归档 |
| 修改方式 | 只能少量 patch，禁止 overwrite 与 code_run 重写 | 瘦身时我做过一次整份替换（经同意、有备份，但与官方推荐不同） |
| 整理方式 | agent 按 `memory_cleanup_sop.md` 按需整理；官方**没有**定时整理机制 | 同左 |

**为什么不再压到官方指标：** 再省约 5.5K token/次，按 10-11 的 1436 次调用，约 7.9M token/天，官方公开价口径下约 ¥2/天（缓存命中 95%）到 ¥16/天（全未命中）；且要把 29 条规则中的大部分降到 L3，需要对规则优先级做判断，质量风险无法验证。**若以后要对齐：** 采用两级索引——L1 压到 ≤ 30 行放类别名和 RULES，现在的 207 行索引作为第二级文件按需读取。09-24 的 R384 做过 27 行版本并 A/B 无退化（30 份答案），但当时 L1 只有 73 行。

### 3.2 瘦身机制与资产

| 资产 | 说明 |
|---|---|
| `memory/l1_detail_archive.md` | 200 行原文**整行无损**迁入；每个块的标题是锚点 `## A###`（A + 原行号）或 `## N####`（后续迁出，按序号） |
| L1 里的 `→A###` / `→N####` | 指向归档同名块；需要细节时 `grep` 锚点 |
| `tools/l1_compact.py` | 手动工具，默认只预演；`--apply` 才写。规则行不压缩；括号不配对、收益不足 30%、丢条目名都会跳过；先备份、先写归档、替换前复核、失败回滚；`--selftest` 15 项 |
| `plugins/ga_guard.py` | 只用官方 hook，不写文件：`agent_before` 超限向 stderr 警告；`tool_after` 在 L1 被写入且违规时，把纠正提示追加到该工具结果的 `next_prompt`，由 agent 自己用 `file_patch` 修正 |
| L0 新增一行（`memory_management_sop.md`，“同步红线”下） | `R###` 任务记录不进 L1，只写 `autonomous_reports/` 与三索引；L1 最多加一行 ≤ 120 字指针 |

`ga_guard` 的违规口径（与 `l1_compact` 一致）：标题、分节、编号规则行豁免；带 `R###` 编号和日期且无锚点的长行（>200 字）视为任务记录；无锚点单行 >500 字；带锚点单行 >1500 字；总量 >24000 字符。

### 3.3 L1 中必须保留的字句

项目里有机械断言依赖 L1 的原文（`tools/durable_facts_assertions.json`、`tools/decisions_premise_recheck.py`）。瘦身时丢过这些字句，导致 4 条 ACTIVE 断言变成不可验证、`C25` 误判。已补回。**以后任何整理 L1 的操作都必须保留：**

| 字句 | 被谁依赖 |
|---|---|
| `注册面=LaunchAgents 29+sche_tasks 25=54` | `R477-A8` |
| `t0_baseline.sha256 6299条` | `R477-A15`（数字须等于基线文件非空行数） |
| `25条entries` | `R477-A16` |
| `targets 45→46` | `R573-A19`（数字须等于 `freshness_criteria.json` 的 targets 数） |
| ``本机 `which setsid`=NOT FOUND`` | `C25` |

校验命令：`python3 -I tools/durable_facts_lint.py | tail -2`，应为 `CONTRADICTED(ACTIVE)=0`。

### 3.4 仍是截断版的规则

L1 第 18、19 条规则（“引文即契约”“部分提交三坑”）的原文分别约 5853、2308 字符，L1 里保留前约 140 字加锚点；尾部是案例细节，在归档里。其余被截断的规则，若尾部含禁止类条款，已恢复全文。

---

## 4. 失败与重试口径

| 项 | 口径 |
|---|---|
| 行为 | 10 个槽顺序各试一次；全部失败返回 `!!!Error` 文本；`agent_loop` 把无工具调用的响应当成结束；`do_no_tool` 只对“空响应”重试，不处理非空错误文本 |
| 数据 | 3 个会话 21 段，13 段以错误结尾（`489708` 7/11、`813791` 5/8、`292582` 1/2）；10-10 起 23 次整轮错误，终止前累计等待 114 分钟 |
| 恢复 | 错误后到下一次成功响应中位 22.4 分钟，p25 9.9、p75 51.6、最短 146 秒（含人工输入“继续”的时间，是上界） |
| 决定 | 不做自动重试；不动槽顺序；不加单轮总时限；不改 `read_timeout` |
| 监控 | `python3 -I tools/ga_diag.py breaks --since YYYY-MM-DD`，看以错误结尾的段数和类型 |
| 若以后要做 | 改源码路径：在 `agent_loop` 或 `ga.do_no_tool` 里对 `!!!Error` 做有上限的退避重试；或给 mixin 加单轮总时限和禁用槽的冷却恢复。均需先过 GA 宪法第 1 条“改自身源码先请示” |

---

## 5. 费用口径

### 5.1 估算方法

`多发 token = Σ 每日调用数 × (当日 L1 token − 7000)`；L1 token = L1 字节 × 66000/252715（本机校准：25.27 万字节 ≈ 6.6 万 token）。调用数取自 `temp/model_responses` 响应头（所有进程）。

### 5.2 结果（10-08 至 10-11，共 3535 次调用，约 206M token）

| 口径 | 假设（**截至 2026-10，价格以各平台控制台为准**） | 金额（人民币） |
|---|---|---|
| A | DeepSeek 公开价口径（命中 ¥0.2/M、未命中 ¥2/M，是对 V3.2 价格的印象，**未核实 `v4.1`**），缓存命中 95% | 约 ¥60 |
| B | 同价，缓存命中 86%（日志整体命中率） | 约 ¥93 |
| C | 中转站不支持缓存，全部按 ¥2/M | 约 ¥411 |
| D | 中转按 ×2 倍率且无缓存（¥4/M） | 约 ¥823 |

- 其中 354 次走 GPT、Claude、GLM 类槽，单价应更高，未单独计价。
- L1 在系统提示开头，是稳定前缀，缓存命中应高于整体 86%，口径 A 到 B 更接近；**中转站是否真的缓存没有验证**。
- 10-11 单日多发约 84M token：口径 A 约 ¥32/天，口径 C 约 ¥169/天。瘦身后每天约省这个数。
- 参照：飞书与 scheduler 日志（09-18 起）累计输入约 1941M token、输出约 11.8M，口径 A 约 ¥905；L1 多发约占 20%–25%。
- 任务膨胀（报告、索引、提交类轮次，约 61M token）口径 A 约 ¥28，量级远小于 L1。
- **核对方法：** 登录 DeepSeek 控制台，以及 wududu、metapi 等中转站的计费页，查 10-08 至 10-11 的实际消费，与上表对比；再用 `ga_diag.py cost` 带上真实单价重算。

### 5.3 验收口径（改动后观察）

| 指标 | 改动前基线 | 预期 | 命令 |
|---|---|---|---|
| 平均每次输入 token | 约 15.3 万（飞书日志累计）；改动前最近 500 次 17.4 万，缓存命中 87% | 约 9–10 万（估，仅 L1 一项的影响） | §7.3 的 awk 一行 |
| L1 体积 | 25.3 万字节 | 1.6 万字符，且不再增长 | `ga_diag.py l1` |
| `Trimmed context` 占比 | 约 17% | 明显下降 | §7.3 |
| 中转站单轮延迟中位 | 32–40 秒 | 受 L1 与上下文缩小影响，可能下降 | `ga_diag.py latency` |
| 错误终止段占比 | 13/21 | 不会因本次改动改变（中转站稳定性问题） | `ga_diag.py breaks` |

初步观察（样本小，含改动前后，**不能作为结论**）：10-12 当日中转站 `deepseek-v4.1-flash` 单轮中位 22 秒（174 轮），此前两天为 32–40 秒。

---

## 6. 本次改动清单与回退

| 项 | 改动 | 回退 |
|---|---|---|
| `mykey.py` | 12 个槽 `context_win` 统一为 80000；其余未动 | 备份 `mykey.py.bak-20261012-before-ctxwin`（原 8 个 1M + 4 个 80000）、`mykey.py.bak-20261012-before-80000`（曾短暂用过的 450000 版） |
| `memory/global_mem_insight.txt`（L1） | 瘦身 + 补丁 10 行（5 处断言字句 + 5 条规则全文） | `temp/l1_backup/global_mem_insight.20261012.d9598830391b.txt`（原始 25.3 万字节）、`…after-slim.txt`（瘦身后、补丁前） |
| `memory/l1_detail_archive.md` | 新增；200 行原文无损 | 删除后 L1 锚点会悬空，需先回退 L1 |
| `memory/memory_management_sop.md`（L0） | 新增一行“任务记录不进 L1”，在“同步红线”示例下 | 删除该行 |
| `plugins/ga_guard.py` | 新增；纯 hook；自测 16 项 | 删除文件 |
| `tools/l1_compact.py` | 新增；手动工具 | 删除文件 |
| `tools/ga_diag.py` | 新增；只读诊断 | 删除文件 |
| `docs/LOCAL_COST_AND_STABILITY_GUIDE.md`、`docs/INDEX.md` | 新增本文档并登记 | 删除文件与索引行 |
| GA 核心文件 | `llmcore.py`、`ga.py`、`agent_loop.py`、`agentmain.py` **未改动**（对比 git HEAD） | – |
| 进程 | 2026-10-12 11:22–11:27 重启五个 agent 进程（§7.1） | – |

独立评审（子代理只读）发现并已修复：并发写入被静默丢弃、归档追加非事务、缩短丢条目名与括号不配对、规则行被截断、插件与工具判定口径不一致、非法 UTF-8 被改写。我自己引入的回归：瘦身丢了断言依赖字句（§3.3）。

---

## 7. 运维手册

### 7.1 服务与重启

| launchd 标签 | 运行内容 | 备注 |
|---|---|---|
| `com.cuip.feishu-agent` | `frontends/fsapp.py`（飞书） | 日志 `logs/feishu_agent.log`；全局一个 agent，历史跨对话累积 |
| `com.cuip.genericagent-launch` | `frontends/dingtalkapp.py`（钉钉） | 日志 `logs/dingtalk_agent.log` |
| `com.cuip.genericagent-scheduler` | `agentmain.py --reflect reflect/scheduler.py` | 日志 `logs/scheduler_reflect.log`；定时任务执行器，**重启前必须确认没有任务在跑** |
| `com.cuip.genericagent-price-alert` | `--reflect reflect_scripts/06_price_alert.py` | 价格告警，轮询零 LLM 成本 |
| `com.cuip.genericagent-l2-gate` | `--reflect reflect_scripts/07_l2_gate.py` | Trellis 控制面巡检，与 L1/L2 记忆无关 |
| 其他 | `genericagent-ga-api`（未加载）、`genericagent-pet`、`desktop-bridge-repair` | 不涉及 agent 对话 |

GA-admin 的 chat hub 不加载 GA 代码；chat worker 每次新开对话都是新进程，自动加载插件，**无需重启**。L 站浏览守护等 `tools/` 进程不经过 `agentmain`，也无需重启。

重启步骤：

1. 空闲的服务直接重启：`launchctl kickstart -k gui/$(id -u)/<标签>`。
2. 调度器：先看 `tail logs/scheduler_reflect.log` 和 `sche_tasks/scheduler.log` 是否有任务在跑；等 `sche_tasks/done/<日期>_<HHMM>_<任务名>.md` 出现后再重启。中途打断会占用该任务的尝试次数，连续 3 次未完成会被调度器 BLOCK。
3. 确认：`launchctl list | grep -E "feishu-agent|genericagent"`，飞书日志出现 `connected to wss://...`，日志里无 `[hooks]` 加载错误。
4. 重启会清空各 agent 的内存历史（这对省 token 是好事）；飞书里需要可用 `/restore` 恢复。

插件改动只对**新启动的进程**生效；L1 与 `mykey.py` 按文件修改时间热加载。

### 7.2 症状 → 命令 → 判读

| 症状 | 命令（均只读） | 判读 |
|---|---|---|
| 慢 | `python3 -I tools/ga_diag.py latency --since 2026-10-10` | 同类输出下中转站中位 ≥ 官方的 10 倍 ⇒ P1；看 `>120s` 列和“最大s”找整轮卡死 |
| 老断 / 要喊继续 | `python3 -I tools/ga_diag.py breaks --since 2026-10-10` | “以错误结尾”占比高 ⇒ P2；看错误类型与恢复间隔 |
| 费 | `python3 -I tools/ga_diag.py l1`；`python3 -I tools/ga_diag.py cost --since 2026-10-08 --until 2026-10-11` | L1 超限或违规 ⇒ P3；cost 的单价要换成你的真实价 |
| 质量差 | §7.3 统计 `Trimmed context` 占比；`ga_diag.py l1` | 占比高、L1 大 ⇒ 硬删频繁；其次看会话轮次是否膨胀（P5） |
| 官方断言 | `python3 -I tools/durable_facts_lint.py \| tail -2` | 应为 `CONTRADICTED(ACTIVE)=0` |

### 7.3 日志一行统计

```bash
cd ~/projects/GenericAgent/logs
# 近 500 次调用：平均输入 token 与缓存命中率
grep -o "\[Cache\] input=[0-9]* cached=[0-9]*" feishu_agent.log | tail -n 500 \
  | awk -F'[= ]' '{i+=$3;c+=$5;n++} END{printf "近%d次 平均输入 %.0fK 缓存命中 %.0f%%\n", n, i/n/1000, 100*c/i}'
# 硬删频率
printf "Current context: %s 次，Trimmed context: %s 次\n" "$(grep -c 'Current context' feishu_agent.log)" "$(grep -c 'Trimmed context' feishu_agent.log)"
```

### 7.4 自测

```bash
cd ~/projects/GenericAgent
python3 -m plugins.ga_guard --selftest      # 须在仓库根目录以模块方式运行；16 项
python3 -I tools/l1_compact.py --selftest   # 15 项
python3 -I tools/l1_compact.py              # 对真实 L1 预演（只读）
```

---

## 8. 使用习惯规范（解决 P5 与“继续”类续接）

1. **一个任务一个会话**：做完用 `/new`，不要用“继续”把新需求接进旧会话。历史会一直累积到被硬删。
2. **任务开头划边界**，例如：

   > 只做 X。不要写报告、不要改 SOP、不要更新 L1/索引、不要 git 提交；做完告诉我结果，再问我是否沉淀。

3. **别让探测代替交付**：agent 连续 30 轮仍在探测，就让它先 `ask_user` 汇报并确认方向（GA 自己在第 175 轮会强制要求汇报）。
4. **ask_user 会让会话停住**：日志里看到“工具耗时”几十分钟到几小时，多半是在等你回复，不是任务在跑。
5. **长调试靠文件接力**：关键发现写入文件（GA 每 31 轮会提示），不要指望早期的原始工具输出一直在上下文里。
6. **别在飞书/GA-admin 的同一会话里并行塞多个任务**；反射进程的历史是跨任务共用的，定时任务之间会互相带上下文。

---

## 9. 遗留与待决策

| 项 | 状态 | 说明 |
|---|---|---|
| P2 请求失败即终止 | 未解决 | 接受现状，用 `ga_diag.py breaks` 监控；要解决只能改源码 |
| 单轮总时限、`read_timeout` 降到 90–120、禁用槽冷却恢复 | 未做 | 都需改配置或源码 |
| 首槽 magpie 网关很慢（单轮 62、508、43 秒） | 未处理 | 要么排到快槽之后，要么排查网关 |
| 官方槽顺序 | 不动 | 你的决定 |
| 治本：agent 不再往 L1 写任务记录 | 已加 L0 规则 + 插件反馈 | agent 看到提示后是否真的修正，**未实测** |
| 瘦身后导航质量 | 无 A/B | 观察一两天，agent 若不读归档可能重复踩坑 |
| L1 对齐官方指标（≤ 30 行 / < 1k token） | 不做 | 收益小、风险大，路径见 §3.1 |
| `reasoning_effort=high` / `max_tokens=384000` 的影响 | 未量化 | 可用运行时 `/session.reasoning_effort=medium` 试 |
| `base_delay` 疑似无效 | 未验证 | 只在 `llmcore.py` 里保存了值 |
| stderr 警告的可见性 | 未核实 | 飞书与 GA-admin 的 stderr 是否落在能看到的日志里 |
| GA-admin 是否加载 `plugins/` | 未实测 | 只做了代码判断 |
| 密钥 | **建议评估轮换** | 排查时因 BSD sed 不支持 `\s`，有 2 个 apikey 明文出现在终端输出里 |
| 其他会话的未提交改动 | 未触碰 | `memory/linuxdo_browse_sop.md`、`tools/linuxdo_browse_daemon.py` 等 |

---

## 10. 更正记录（排查过程中说错、后来更正的）

| 当时的说法 | 更正 |
|---|---|
| deepseek 槽的 80000 是“默认值的两倍多” | 80000 就是 deepseek 的代码默认值；被放大的是 8 个 1M 槽 |
| 1 token ≈ 3.3 字符 | 本机日志粗拟合约 0.47 token/字符 + 固定开销 |
| 每 180 轮要喊一次“继续”（`max_turns=180`） | 真正原因是请求失败后任务被判结束 |
| 80000 与 450000 哪个好 | GA 开发者建议 80k；450000 只是让 L1 的注入不触发硬删，反而掩盖问题；已改回 80000 |
| `context_win` 换算里固定开销按 7 万 | 7 万是瘦身前（L1 占 6.6 万）；瘦身后约 1.1 万，公式已更新（§2.5） |
| 第一版插件替换 `raw_ask` 和 `get_global_memory` | 违反“不替换官方核心”，已删除，重构为纯 hook |
| 整份覆盖 L1 | 违反 L0“只能少量 patch”的红线；经同意、有备份，但与官方推荐做法不同 |
| 10-07 审计结论“`context_win: 1000000` 才是兑现 1M 的正确写法” | 错误：`context_win` 是裁剪触发点，1M 等于关掉裁剪；该审计同批的其他发现（`connect_timeout` 死键、Claude 缺省思考档位、OAI 不发输出上限、mixin 各槽阈值要对齐）仍成立 |
| 瘦身时把规则截断到约 140 字 | 7 条规则的被截尾部含禁止类条款，其中体量小的 5 条已恢复全文 |
