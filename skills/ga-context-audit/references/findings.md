# GA 上下文问题：已确认的发现与证据

时间一律为 Asia/Shanghai (UTC+08:00)。数据来自 2026-10-14 的会话，"估算"表示推算值。

## 目录
1. L1 膨胀与整改
2. 每 10 轮注入 L1（上游 issue #832 / PR #834）
3. 反射任务间历史累积（上游 issue #833 / PR #835）
4. L1 为什么达不到官方标准
5. 度量基线与数据来源
6. 设计意图依据（避免误提 PR）

## 1. L1 膨胀与整改

- 10-12 瘦身前 L1 为 25.3 万字节；10-13 因 R577–R583 追加细节，一天内从 1.33 万涨到 1.81 万字符。
- 整改：7 个被撑胖的行（索引行 56、RULES 15/16/21/22/26/27）压回索引形态，原文无损并入 `memory/l1_detail_archive.md`；110 行 `R###` 任务记录条目整行移到归档末节 `L1_REMOVED_20261014`；L1 加一行指向 `autonomous_reports/INDEX.md` 的指针。结果 98 行 / 8634 字符 / 约 3.4K token。
- `plugins/ga_guard.py` 收紧：总量 24000→16000，带锚点行 1500→800，编号规则行原先整行豁免、现上限 300。验证：把 R583 式追加模拟到行 56 和 RULES 21，新口径各报 1 处违规，旧口径都不报。
- 被 `tools/durable_facts_assertions.json` 引用的 L1 行（8、15、126、204）不能删；RULES 15 里的 ``本机 `which setsid`=NOT FOUND`` 被 `decisions_premise_recheck.py` 的 C25 探针引用，瘦身时丢过一次，已补回。
- git 提交闸门方案（`.githooks`）被用户否决：约束必须留在 GA 范围内，用 GA 自己的 plugin hook，不用 git hook。

## 2. 每 10 轮注入 L1

- 位置：`ga.py` 的 `elif turn % 10 == 0: next_prompt += get_global_memory()`，由提交 `bb2d595`（2026-02-27）引入，提交说明写的是"每10轮注入全局记忆防止上下文丢失"，是提醒，不是刷新记忆。
- 问题：系统提示每个任务开头重建，每次请求都带 L1；注入块作为普通用户消息进历史，`compress_history_tags` 只折叠带标签的块，旧副本一直留到硬裁剪。
- 修法（净增 0 行）：`ga.py` 注入包进 `<global_memory>…</global_memory>`；`llmcore.py` 的 `_hist_pat` 加 `global_memory`。
- 模拟（200 轮，每 10 轮注入 9K 字符，驱动真实 `trim_messages_history`）：完整 L1 副本峰值 10→2（`cut_msg_interval=7`）/ 10→3（25，deepseek 槽默认）；硬裁剪 2→1 次。注意折叠是惰性的，仍会同时存在 2–3 份完整副本，目标是限制累积而非精确去重。
- 被否决的备选：仅在 L1 mtime 变化时注入（丢了周期提醒的原意）；注入时删除历史里上一份（需改 Session 层，改动半径大）。

## 3. 反射任务间历史累积

- 机制：`agentmain.py --reflect` 是常驻 agent，`put_task(task, source='reflect')` 共用 `backend.history`，源码注释 "the **full** history is in llmclient"；交互前端有 `/new`（`frontends/continue_cmd.py: reset_conversation`），反射循环没有等价动作。
- 实测（`logs/scheduler_reflect.log`，按 `[Reflect] triggered:` 切分）：303 次触发 / 5699 次调用；首轮上下文中位约 18.8 万字符，紧贴 24 万字符硬裁剪线（context_win 80000×3）；携带的旧历史占总输入约 95%；若每次从约 5K 字符（估算）起步，总输入约降 70%。线性放大，不是指数；任务越多调用越多。
- 先前错误：曾用"消息数≤3"切段得出"携带占 21.8%"，被 `[Reflect] triggered:` 切分纠正。
- 修法：反射模块可选属性 `FRESH_CONTEXT`（与 `INTERVAL`、`ONCE`、`LLM` 同类）；为真时 `reset_conversation(agent, snapshot=False)` 在 `put_task` 前执行。`reset_conversation` 新增 `snapshot=True` 默认参数，保持 `/new` 行为不变；`snapshot=False` 避免每个定时任务写一个 `model_responses_snapshot_*` 文件（这是审计发现的副作用，`reset_conversation` 原本会在清历史前快照日志）。`reflect/scheduler.py` 设为 True。
- 隔离粒度：每次触发前清空（含同一任务重试），开关是模块级而非 job 级；清空 agent.history、全部 backend.history、last_tools、handler（含 key_info）；不清 L1/L2/L3、done 报告、脚本自己的基线文件。
- 24 个启用任务逐个审计 prompt：均不依赖对话历史；"与上次对比"类（smyze_stock_watch、project_update_monitor、haisou_daily_monitor）的基线在脚本自己的文件里；`update_working_checkpoint` 记的是本次调度注入的 `[报告路径]`。这是静态文本审计，未逐个跑过。`weekly_inspection`、`weekly_report_reminder` 等周五触发的任务要在新逻辑下首次运行后复查。
- 已知边界：hub 来的消息空闲时直接 put_task，不经过清空；日志 303 次触发全是定时任务，未出现。

## 4. L1 为什么达不到官方标准

- 官方模板的 L1 只有 23 行 / 1431 字符，L3 名单约 10 个。本机 `memory/` 有 88 个 SOP 类 md、29 个 py，L1 里有约 70–100 个不同的 SOP/脚本名，仅名字就约 0.9K token。
- 官方思路是分层（高频写 key→value，低频只列关键词，兜底 `ls ../memory/`），本机选择"全部名字进 L1"。用户明确保留这一索引能力，所以目标是约 3K token 且不增长。
- 合并同类 SOP 被评估为不合理：拆分是"最小原则、可组合"，合并相反；前缀归并只省约 21 字符。名字后的括号说明约占索引区 29%，可以再精简（用户认可，尚未执行）。
- 按使用频率退出 L1：用户最终选择"不退"。

## 5. 度量基线与数据来源

| 指标 | 10-12 整改前 | 10-14 |
|---|---|---|
| L1 | 25.3 万字节 | 8634 字符 / 约 3.4K token |
| 飞书进程平均输入（近 500 次） | 约 15.3 万 token | 约 6.1 万，缓存命中 90% |
| scheduler 进程平均输入 | 未测 | 约 7.2 万，缓存命中 83% |
| 硬裁剪占比 | 约 17% | 日志尾部约 5%–10% |

来源：`logs/feishu_agent.log`、`logs/scheduler_reflect.log`（含 `[Cache] input=… cached=…`、`Current context`、`Trimmed context` 行）、`temp/model_responses/`（只保留约 3 天）、`memory/L4_raw_sessions/*.zip`（按月压缩的原始会话）、`docs/LOCAL_COST_AND_STABILITY_GUIDE.md`（本机口径手册）。

## 6. 设计意图依据

- 上游 `lsdefine/GenericAgent` 的 CONTRIBUTING：非平凡改动先开 issue；PR 要小改动半径、净行数最好为零或负。
- 上游 PR #282 被维护者关闭，理由是 GA 的记忆 = LLM 对话历史 + 全局记忆，不允许从外部往对话里注入内容。#833/#835 是"清空而非注入"，不冲突，但评审时可能被引用；#251（定时任务输出进会话）方向相反。
- 维护者在 #229 中说明：每个前端是独立进程、各有独立 history，桌面端推荐多开窗口；scheduler/trader 等反射实例各跑各的。
- 向上游推送需用 SSH 协议（`git@github.com:cuipengcx90/GenericAgent.git`）；HTTPS+OAuth 会因缺 `workflow` scope 拒绝含 `.github/workflows` 的分支。本地 GA 与上游已分叉（本地有数百个独有提交），同步用 merge，不 rebase。
