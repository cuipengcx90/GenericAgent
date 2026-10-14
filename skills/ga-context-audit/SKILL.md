---
name: ga-context-audit
description: 排查 GenericAgent（GA，本机 ~/projects/GenericAgent）上下文过大、token 费用高、任务慢、Trimmed context 频繁、L1 记忆索引膨胀、定时任务（scheduler 反射进程）输入偏大等问题的方法与审计脚本。只要用户提到 GA 上下文占用、L1/L0/L2/L3/L4 记忆分层、global_mem_insight、ga_guard、每 10 轮注入、反射任务历史累积、context_win、Trimmed context、输入 token 偏高、想给 GA 上游提上下文相关 issue 或 PR，就使用本 skill，即使用户没有说"上下文审计"。不适用：GA 的网络/中转站延迟、请求失败中断（那是另一类问题，见 docs/LOCAL_COST_AND_STABILITY_GUIDE.md 的 P1/P2）。
---

# GA 上下文占用排查

先跑审计脚本拿事实，再对照下面的分层模型定位来源，最后才决定改什么。结论必须来自日志或代码，不要凭印象；本机用过的"估算"都要标明是估算。

## 第一步：跑脚本

```bash
python3 -I ~/projects/GenericAgent/skills/ga-context-audit/scripts/ctx_audit.py all
```

脚本只读，输出四块：补丁是否在本地源码生效、L1 体积与违规、反射任务首轮上下文、各进程硬裁剪比例与平均输入。GA 根目录默认由脚本位置推出（真身在 GA 仓库的 `skills/` 下），可用 `--ga-root` 或环境变量 `GA_ROOT` 覆盖。

## 第二步：按分层模型定位来源

GA 每次请求的输入由这些部分构成，只有 L1 是常驻成本：

| 层 | 文件 | 进上下文的方式 | 官方要求 |
|---|---|---|---|
| L0 | `memory/memory_management_sop.md` | 不自动注入，写记忆前要求先读 | 无数值 |
| L1 | `memory/global_mem_insight.txt` | 系统提示每次请求都带，另外每 10 轮追加一次 | ≤30 行、<1K token（本机做不到，见下） |
| L2 | `memory/global_mem.txt` | 按需 file_read | 可膨胀 |
| L3 | `memory/*.md`、`*.py` | 按需 file_read | 尽量短 |
| L4 | `memory/L4_raw_sessions/` | 不进上下文，会话归档 | 无 |

输入 token 的主要组成，按占比排序：对话历史（工具输出、思考，按设计累积到 `context_win×3` 字符才硬裁剪）、固定开销（系统提示、工具定义、L1，约 1 万 token）、重复注入的 L1 副本、任务范围膨胀。

## 第三步：对照已知问题

已确认的两个设计缺口和一个膨胀根因，细节、证据和上游 issue/PR 编号见 `references/findings.md`：

1. **L1 膨胀**：agent 把 `R###` 任务记录追加进 L1，违反 L0 的"严禁写日志记录"。修法是细节迁入 `memory/l1_detail_archive.md`，由 `plugins/ga_guard.py` 校验。
2. **每 10 轮注入 L1 无脑追加**（`ga.py` 的 `turn % 10 == 0`）：系统提示已带 L1，注入块是普通文本，旧副本不会被折叠。修法是包进 `<global_memory>` 标签并加入 `compress_history_tags` 的折叠正则。
3. **反射进程任务间不清历史**：`--reflect` 进程是常驻 agent，所有任务共用 `backend.history`，几乎每个定时任务从 15–24 万字符的旧历史起步。修法是反射脚本的 `FRESH_CONTEXT` 属性，为真时每次触发前 `reset_conversation(agent, snapshot=False)`。

判断补丁是否真的生效，看脚本的"最近一次重启之后"那一行：首轮上下文中位应在约 1 万字符以内。进程没在补丁后重启时，静态检查是 OK 而运行结果仍是旧的。

## 约束与边界

- **L1 里的 SOP 名字不能动**。agent 靠 L1 里的 SOP 名字找 SOP，这是索引能力；官方 <1K token 的目标对 70+ 个 SOP 不现实，合理目标是约 3K token 且不增长。不要为了接近官方指标去合并 SOP 或把名单挪出 L1，用户已明确否决。
- **不改 GA 核心源码，除非用户批准**。GA 宪法要求改自身源码先请示；优先用 `plugins/` 的官方 hook。本会话的两处核心补丁是经用户同意后应用的，同时已提上游。
- **对外动作需确认**：提 issue、提 PR、推 fork 都是对外发布，先让用户确认方案和文案，不要把"提 issue"理解成可立即执行。
- 重启 GA 进程前确认无任务在跑（看 `logs/scheduler_reflect.log` 尾部和 `sche_tasks/done/`）；用 `launchctl kickstart -k gui/$(id -u)/com.cuip.genericagent-<名>` 只重启单个进程。
- 日志时间一律换算为 Asia/Shanghai (UTC+08:00) 再汇报。

## 常见误判

- **名字出现次数不是使用证据**：L1 把每个 SOP 名注入进每个会话，所以在 L4 里统计名字会得到数千次。要判断 SOP 是否被用，统计 `file_read` 参数里的 `memory/<名>.md` 路径。
- **按消息数切分会话会低估**：GA 新任务开始时历史并不回到个位数。切分反射任务用日志里的 `[Reflect] triggered:` 行。
- **`weekly_last_workday` 任务只在本周最后一个工作日触发**，看 `schedule` 时间不够，必须同时看 `repeat`。
- macOS 没有 `timeout` 命令；`python3 -I` 不带 anaconda 的 site-packages，需要 `requests` 等依赖时用 `/opt/anaconda3/bin/python3`。
- 日志里 `Current context` 的 `chars` 是历史消息的 JSON 字符数，不含系统提示和工具定义；换算 token 用本机拟合约 0.47 token/字符，换模型会变。

## 验证补丁效果

```bash
python3 -I ~/projects/GenericAgent/skills/ga-context-audit/scripts/ctx_audit.py triggers
rg -a "triggered:|Current context" ~/projects/GenericAgent/logs/scheduler_reflect.log | tail -6
```

触发后每个 `triggered` 行下的第一条 `Current context` 应远小于重启前的约 18.8 万字符。仍然很大时先查进程启动时间是否晚于补丁（`ps -axo pid,lstart,command | rg reflect/scheduler`），再查 `FRESH_CONTEXT` 是否被 scheduler 热重载读到。
