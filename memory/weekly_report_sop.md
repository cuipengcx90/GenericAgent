# 周报邮件 SOP（lark-cli 版）

## 收件人与主题
- To: sam.zhang@smyze-ch.com（仅此一人，不含自己）
- 主题格式: `Re: 【周报】-崔朋（YYYY.MM.DD - YYYY.MM.DD）`（本周第一个工作日 ~ 最后一个工作日，用 chinese_calendar 动态计算）
- 回复 message-id: 每周从上一封周报邮件获取（`lark-cli mail +search` 搜最新一封）

## 分析的仓库（动态扫描，自动适配新增子工程）
- **扫描根目录**: `/Users/cuip/develop/WorkSpaces/idea/smyze/bar-smyze/`
- **动态发现规则**: 遍历根目录下所有含 `.git` 的子目录（`os.path.isdir(子目录/.git)`），即视为待分析仓库。**禁止硬编码仓库清单**——后续团队会持续新增子工程，必须自动纳入。
- **本周活跃过滤**: 对每个发现的仓库执行 git log，仅保留本周区间内有 `崔朋` commit（数量>0）的仓库进入周报，无 commit 的自动跳过（不报错）。
- author 过滤: `崔朋`，`--no-merges`，本周第一个工作日~今天（用 chinese_calendar 计算）
- ⚠️ **bar-deploy-server 已迁入 bar-smyze 根目录**：旧路径 `/Users/cuip/develop/WorkSpaces/idea/smyze/bar-deploy/bar-deploy-server` 与新路径同一远端（`bar-deploy/bar-deploy-server.git`），属冗余副本，**只扫 bar-smyze 根下的，不再单独遍历 bar-deploy 目录**，避免重复统计。
- 截至本次扫描，根目录下 git 子工程共 11 个（mp/op/opserver/scm/screen/e2e/maintenance_app/market-server/deploy-server/deploy-webadmin/smyze-spec），其中本周通常仅少数有崔朋 commit——具体以动态扫描结果为准，不要预设。

## 开头用语（强制固定·禁止自由发挥）
> 历史问题：开头每周飘——有时直接"一、"无称呼、有时"Sam，您好"、有时即兴写概述导语。周报是**邮件**(To 给上级 Sam 单人)，必须有称呼礼仪。固定如下，逐周不变：
```
Sam，您好：
（空行）
以下是本周（YYYY.MM.DD - MM.DD）的工作汇报。
（空行）
一、本周工作内容
...
```
- 称呼固定 `Sam，您好：` 顶格单独一行
- 引言固定一句 `以下是本周（日期区间）的工作汇报。`，日期用主题里那个区间（本周首个~末个工作日）
- 引言后空一行直接进 `一、`，**禁止**再加任何"本周工作围绕…展开"之类的概述/总结导语（与正文重复，冗余）
- HTML body 同样按此：`Sam，您好：<br><br>以下是本周（…）的工作汇报。<br><br>一、本周工作内容<br>`

## 正文结构与格式
```
一、本周工作内容        ← 按业务域分类 commit + 群聊证据归纳，不按仓库分
  （空行）
二、下周工作计划        ← 简版 3 行即可
  （空行）
三、需配合事项          ← 无则写"无"，独占一行
```
- Bug 修复占比: `fix/(feat+fix)` 百分比，附数量说明，放在一、末尾
- 群聊内容只作为补充证据源：纳入与崔朋直接相关、能体现需求推动/问题定位/协调决策/风险确认的事项；不得把聊天流水逐条贴进周报
- HTML body: 用 `<br>` 换行，`&nbsp;&nbsp;•` 做子项缩进

## 标准操作流程
1. **获取上周已发送周报**: `lark-cli mail +search --query "【周报】-崔朋" --limit 1`，必须选最新一封**已发送成功**的周报邮件，不能基于旧草稿/错误 reply
2. **Git 分析（动态扫描）**: 遍历 bar-smyze 根目录下所有含 `.git` 的子工程，对每个执行 `git log --all --no-merges --author=崔朋 --since=周一T00:00:00+08:00 --until=明天T00:00:00+08:00`，仅保留有 commit 的仓库
   - ⚠️ **禁止硬编码仓库清单**：用 `os.listdir` + 判断 `.git` 自动发现，后续新增子工程零改动自动纳入
   - ⚠️ **--since/--until 必须带显式时区 `T00:00:00+08:00`**：不带时间时 git 会用当前时刻补全，导致当天早于该时刻的 commits 被截断丢失
3. **飞书群聊补充分析（与崔朋相关，仅新增数据源）**: 使用 `lark-cli` 用户身份拉取群聊证据；时间窗口必须与 Git 分析完全一致（同一个周一 `T00:00:00+08:00` 到同一个“明天” `T00:00:00+08:00`），补齐未体现在 commit 中的需求推动、线上问题定位、跨端协作和决策确认
   - 默认重点群：`软件智能化小组`、`Smyze软件研发组`；如搜索到内部/外部同名群，内部群和外部群都读取，后续按业务主题归纳去重
   - 群搜索: `lark-cli im +chat-search --query "<群名>" --as user --format json`；记录所有匹配群的 `chat_id/external/owner/create_time`，避免漏读同名内外群
   - 群消息: `lark-cli im +chat-messages-list --chat-id <oc_xxx> --as user --start <同Git周一T00:00:00+08:00> --end <同Git明天T00:00:00+08:00> --page-size 50 --format json`；按 `has_more/page_token` 手动翻页
   - 全局搜索: `lark-cli im +messages-search --query "崔朋" --is-at-me --start <同Git周一T00:00:00+08:00> --end <同Git明天T00:00:00+08:00> --page-all --format json`；也可加 `--chat-id <oc_xxx>` 限定重点群
   - 入选标准：只保留与崔朋直接相关且可归纳为工作成果/推进事项的内容，例如 @崔朋、崔朋发言、回复崔朋、围绕崔朋负责模块的明确结论；闲聊、纯确认、无结论讨论不入周报
4. **分类统计**: 按 commit message prefix 分 feat/fix/chore，计算 bug 占比
5. **按原周报格式做业务域归类 + 归纳合并**: 周报最终格式、业务域粒度和表达方式保持原 Git 周报方案不变；群聊只作为与 Git 并列的补充数据源，合并到对应业务域，不新增“群聊记录”章节
   - ⚠️ **禁止逐条 commit/聊天记录直译**：同一需求/bugfix 链的多个 commit 或多轮群聊必须合并归纳为一句业务描述
   - 粒度标准：每个业务域 3-5 条，每条描述一个"用户可感知的功能/修复/推进结果"，而非代码变更或聊天细节
   - 示例：❌ "售后详情饮品条件块补回 dishCode/dishName 字段" → ✅ "售后详情商品信息展示优化及字段修复"
   - 示例：❌ "14:10 与王冰岩讨论退款后制作失败推送" → ✅ "订单退款后状态终结与异常推送兜底逻辑梳理"
   - chore/docs/refactor 类仅保留有业务价值的（如方案文档输出），纯工程类（archive task/record journal）不入周报
6. **生成 HTML body**: 写入临时文件 `temp/weekly_body.html`
7. **创建 reply 草稿**: `lark-cli mail +reply --message-id <上周已发送message_id> --to sam.zhang@smyze-ch.com --body <HTML>`
8. **如需调整正文**: 对 reply 草稿使用 `+draft-edit` 的 patch `set_reply_body`，只替换本次新增内容，**保留 quoted content**，实现“上方本周新内容 + 下方上周已发送引用”的追加回复效果
9. **修正主题/收件人**: 必要时用 `+draft-edit` 修正 subject / recipients，但不要用 `set_body` 覆盖整个 reply，除非明确要移除引用
10. **飞书通知用户**: `notify_feishu("📋 周报草稿已生成，请检查草稿箱后确认发送")`
11. **等用户确认后发送**: `lark-cli mail user_mailbox.drafts send --params '{"user_mailbox_id":"me","draft_id":"<ID>"}'`

## ⚠️ 已验证坑点
- **git --since/--until 必须带 `T00:00:00+08:00`**：裸日期 `--since=2026-06-08` 会被 git 用当前时刻补全时间部分，导致当天早于该时刻的 commits 静默丢失（致命级，曾导致 hotfix 3 条严重 bug 修复全部漏报）
- `lark-cli mail +reply` 默认只存草稿不发送 ✅
- `+reply` 的 `--to` 会覆盖原始收件人列表，必须显式指定仅 sam.zhang
- `+draft-edit --set-subject` 可修正自动回复主题中的日期
- bash 中执行 Python 代码需用 `type: python`，不能混用
- HTML body 含中文/特殊字符时，通过文件读取传参比命令行直接传更稳定
- 周报必须基于**上周已发送成功**的周报邮件继续 reply，不能基于历史错误草稿或废弃 reply
- 若用户要“追加回复”效果，必须使用 `set_reply_body` 保留 `has_quoted_content=true` 的引用块；误用 `set_body` 会把 reply 变成仅剩新正文
- 飞书邮件/群聊全流程只用官方 `lark-cli 1.0.15`（larksuite/cli）已登录用户身份；`feishu-cli` 仅曾做环境探测/能力对比，未用于周报邮件、群聊读取、草稿回复或发送流程
- 飞书群聊读取必须用 `lark-cli` 用户身份；同名群可能同时存在内部/外部群，用 `+chat-search` 列出全部匹配群并逐个 `chat_id` 读取（内外都读），按 `external/owner/create_time` 标注来源
- 已验证默认重点群（均含同名内外两个，都需读取）：
  - `软件智能化小组`：内部 `oc_bc6c977f4be7f8a7866758e1d7f18de1`（external=false）/ 外部 `oc_5f23ecc16748dc2eaa425678975c0e56`（external=true）
  - `Smyze软件研发组`：内部 `oc_675f8f4122e9652b3929e949cf933252`（external=false）/ 外部 `oc_afd7af3ae2e37e8c6c6c851b5102e40c`（external=true）
  - 如后续群迁移或改名，重新 `+chat-search` 验证 chat_id

- `+chat-messages-list` 当前无 `--page-all`，读取群历史需按 `has_more/page_token` 手动翻页；`+messages-search` 才支持 `--page-all`
- 群聊数据源不能改变周报版式：仍按原 Git 周报的业务域归类与三段结构输出，不单独增加“群聊内容/聊天记录”章节

## 定时任务
- 每周最后一个工作日 17:00 自动触发（`weekly_last_workday`，支持中国调休）
- 任务文件: `../sche_tasks/weekly_report_reminder.json`
- 流程: 生成草稿 → 飞书私信通知用户 → 用户确认后发送