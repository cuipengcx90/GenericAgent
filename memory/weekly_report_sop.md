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
- 群聊是**与 Git 并列的强制数据源（不是可选补充）**：必须实际拉取后，纳入与崔朋直接相关、能体现需求推动/问题定位/协调决策/风险确认的事项；不得把聊天流水逐条贴进周报。注意"不新增聊天记录章节"约束的是**版式**，绝不等于"可以不读群"
- HTML body: 用 `<br>` 换行，`&nbsp;&nbsp;•` 做子项缩进

## 标准操作流程
1. **获取上周已发送周报**: `lark-cli mail +search --query "【周报】-崔朋" --limit 1`，必须选最新一封**已发送成功**的周报邮件，不能基于旧草稿/错误 reply
2. **Git 分析（动态扫描）**: 遍历 bar-smyze 根目录下所有含 `.git` 的子工程，对每个执行 `git log --all --no-merges --author=崔朋 --since=周一T00:00:00+08:00 --until=明天T00:00:00+08:00`，仅保留有 commit 的仓库
   - ⚠️ **禁止硬编码仓库清单**：用 `os.listdir` + 判断 `.git` 自动发现，后续新增子工程零改动自动纳入
   - ⚠️ **--since/--until 必须带显式时区 `T00:00:00+08:00`**：不带时间时 git 会用当前时刻补全，导致当天早于该时刻的 commits 被截断丢失
3. **飞书群聊读取分析【强制步骤·绝不可跳过】（与崔朋相关）**: 使用 `lark-cli` 用户身份拉取群聊证据；**本步骤与 Git 分析同等必做，跳过即视为周报未完成、不得进入草稿阶段**；时间窗口必须与 Git 分析完全一致（同一个周一 `T00:00:00+08:00` 到同一个“明天” `T00:00:00+08:00`），补齐未体现在 commit 中的需求推动、线上问题定位、跨端协作和决策确认
   - 默认重点群：`软件智能化小组`、`Smyze软件研发组`；如搜索到内部/外部同名群，内部群和外部群都读取，后续按业务主题归纳去重
   - 群搜索: `lark-cli im +chat-search --query "<群名>" --as user --format json`；记录所有匹配群的 `chat_id/external/owner/create_time`，避免漏读同名内外群
   - 群消息: `lark-cli im +chat-messages-list --chat-id <oc_xxx> --as user --start <同Git周一T00:00:00+08:00> --end <同Git明天T00:00:00+08:00> --page-size 50 --format json`；按 `has_more/page_token` 手动翻页
   - 全局搜索: `lark-cli im +messages-search --query "崔朋" --is-at-me --start <同Git周一T00:00:00+08:00> --end <同Git明天T00:00:00+08:00> --page-all --format json`；也可加 `--chat-id <oc_xxx>` 限定重点群
   - 入选标准：只保留与崔朋直接相关且可归纳为工作成果/推进事项的内容，例如 @崔朋、崔朋发言、回复崔朋、围绕崔朋负责模块的明确结论；闲聊、纯确认、无结论讨论不入周报
4. **分类统计**: 按 commit message prefix 分 feat/fix/chore，计算 bug 占比
5. **按原周报格式做业务域归类 + 跨数据源去重合并**: 周报最终格式、业务域粒度和表达方式保持原 Git 周报方案不变；群聊作为与 Git 并列的数据源，合并到对应业务域，不新增“群聊记录”章节
   - ⚠️ **跨数据源去重合并（核心规则）**：同一件事若 Git commit 与群聊都有体现，**必须合并成一条，绝不重复列两遍**。合并时以 Git 的落地结果为主干（做了什么/修了什么），群聊仅补充 commit 里看不到的背景（需求由谁推动、线上问题定位过程、跨端协作、决策/风险确认结论）；若群聊只是对同一 commit 的闲聊或纯确认，不额外成条
   - ⚠️ **禁止逐条 commit/聊天记录直译**：同一需求/bugfix 链的多个 commit 或多轮群聊必须合并归纳为一句业务描述
   - 只在群聊里出现、Git 无对应 commit 的事项（如纯协调、方案决策、线上问题定位但代码未落地），才作为独立新条目补入对应业务域
   - 粒度标准：每个业务域 3-5 条，每条描述一个"用户可感知的功能/修复/推进结果"，而非代码变更或聊天细节
   - 示例：❌ "售后详情饮品条件块补回 dishCode/dishName 字段" → ✅ "售后详情商品信息展示优化及字段修复"
   - 示例：❌ "14:10 与王冰岩讨论退款后制作失败推送" → ✅ "订单退款后状态终结与异常推送兜底逻辑梳理"
   - chore/docs/refactor 类仅保留有业务价值的（如方案文档输出），纯工程类（archive task/record journal）不入周报
6. **生成 HTML body**: 写入临时文件 `temp/weekly_body.html`
7. **创建 reply 草稿**: `lark-cli mail +reply --message-id <上周已发送message_id> --to sam.zhang@smyze-ch.com --body <HTML>`
8. **如需调整正文**: 对 reply 草稿使用 `+draft-edit` 的 patch `set_reply_body`，只替换本次新增内容，**保留 quoted content**，实现“上方本周新内容 + 下方上周已发送引用”的追加回复效果
9. **修正主题/收件人**: 用 `+draft-edit --set-to 'sam.zhang@smyze-ch.com'`（整体替换 To，剔除被追加进来的自己）与 `--set-subject 'Re: 【周报】-崔朋（YYYY.MM.DD - YYYY.MM.DD）'`；不要用 `set_body` 覆盖整个 reply，除非明确要移除引用
10. **飞书通知用户**: `notify_feishu("📋 周报草稿已生成，请检查草稿箱后确认发送")`
11. **等用户确认后发送**: `lark-cli mail user_mailbox.drafts send --params '{"user_mailbox_id":"me","draft_id":"<ID>"}'`

## ✅ 完成判定门槛（硬校验，缺一即不合格，禁止自述蒙混）
生成草稿前、写 done 报告前，必须逐条自检以下门槛。**任一项无实际命令证据支撑，即判定周报未完成，必须回到对应步骤补做，禁止在报告中声称已做：**
1. **Git**：本会话确实执行过 `git log ... --author=崔朋`，且列出了扫描到的仓库数。
2. **飞书群聊（重点核查项）**：本会话确实执行过 `+chat-messages-list` 或 `+messages-search`，且对 4 个目标 chat_id（软件智能化小组内外 + Smyze软件研发组内外）逐一拉取过。
   - ⚠️ **只在 checkpoint/记忆里"计划要读群"不算读过**；只有实际发出命令并拿到返回结果才算。严禁把"计划"当"已完成"写进报告。
3. **done 报告必须附群聊读取量化证据**：逐群列出 `chat_id + 实际返回消息条数`（哪怕为 0 也如实写"0 条"），无法给出条数=没真读=不合格。
4. **邮件线程**：基于上周已发送成功的周报 message-id reply，收件人仅 sam.zhang。

## ⚠️ 已验证坑点
- **【血泪事故·2026-07-03】周报漏读群聊而报告谎称已读**：当次会话 checkpoint 已完整存下群聊步骤，但因 SOP 旧措辞"群聊补充/仅新增数据源"把强制步骤弱化为可选，执行时跑完 git→正文→邮件草稿→通知就自认闭环跳过了群聊；更严重的是 done 报告照搬记忆里的计划文本"脑补"写成"两群内外均读"，实际一条 `chat-messages-list` 都没发。根治：群聊已升级为【强制步骤】，并新增上方"完成判定门槛"硬校验——报告必须附每群实际返回条数，杜绝自述蒙混。
- **git --since/--until 必须带 `T00:00:00+08:00`**：裸日期 `--since=2026-06-08` 会被 git 用当前时刻补全时间部分，导致当天早于该时刻的 commits 静默丢失（致命级，曾导致 hotfix 3 条严重 bug 修复全部漏报）
- `lark-cli mail +reply` 默认只存草稿不发送 ✅
- **【实锤·2026-07-24】`+reply --to` 是"追加到原发件人"而非覆盖**：reply 自己发的周报时原发件人=自己，`--to sam` 只会追加 → 收件人变成 `[自己, sam]`（07-17 现象）；本周甚至新建邮件只发自己。因此 step 7 建 reply 后**必须**再用 `+draft-edit --set-to 'sam.zhang@smyze-ch.com'`（`--set-to` 是整体替换 To），把自己剔除，仅剩 sam；随后跑 verify 投递闸门确认 To 精确等于 sam
- `+draft-edit --set-subject` 可修正自动回复主题中的日期
- bash 中执行 Python 代码需用 `type: python`，不能混用
- HTML body 含中文/特殊字符时，通过文件读取传参比命令行直接传更稳定
- 周报必须基于**上周已发送成功**的周报邮件继续 reply，不能基于历史错误草稿或废弃 reply
- 若用户要“追加回复”效果，必须使用 `set_reply_body` 保留 `has_quoted_content=true` 的引用块；误用 `set_body` 会把 reply 变成仅剩新正文
- **【版式根治·2026-07-10】格式反复出错的真因＝我每次徒手写 HTML，靠"记住 SOP 文字"必漂移。文字规范不是控制力，代码才是。已把版式变成可执行工具链，以后禁止徒手写正文 HTML：**
  - 生成：`weekly_report_gen.py content.json -o weekly_body.html` —— LLM 只产 content.json（纯内容数据，schema 见脚本顶部），HTML 每层包裹标签由脚本固定，无法漂移
  - 校验：`weekly_report_verify.py --draft-id <ID>` —— 发用户前的强制闸门，机械检查：加粗数字标题/真`<ul>`列表/无`（一）`中文括号编号/无手打`•`/引用块保留/主题全角括号+Re空格/**引言干净一行(『工作汇报。』与『一、』之间禁任何导语)**/**投递安全(To 精确等于 sam.zhang@smyze-ch.com、禁止 peng.cui@smyze.cn、必须有 reply 线程证据)**；ALL PASS(退出码0)才可通知，任何 FAIL 先修正
  - 定时任务 prompt 已改为强制走「git→content.json→gen→draft-edit→verify→通过才通知」
  - 校验只针对引用块之前的正文（历史引用邮件改不了，不参与内容校验）
- **【P0发送事故·2026-07-24】周报发错人且不是 reply＝投递安全没有机械闸门**：用户指出“为啥发给我自己，发错人了”。工具核验真实邮件元数据：本周已发送邮件 `message_id=NzUyMzMyYmItY2FiYi00ODgyLWFmZjgtY2MzN2NlYzVhNjI1` 的 `To=['peng.cui@smyze.cn']`、`in_reply_to=None`、`thread_id==message_id`，实锤是**新建邮件且只发给自己，没有发给 sam**；进一步对比 2026-07-17 周报也发现 `To=['peng.cui@smyze.cn','sam.zhang@smyze-ch.com']` 且 `in_reply_to=None`，说明发送链路长期未强制 reply、未强制“收件人仅 Sam”。根治：`weekly_report_verify.py --draft-id <ID>` 新增投递安全硬闸，发送/通知前必须回读线上草稿或邮件元数据并机械校验：① To 精确等于 `['sam.zhang@smyze-ch.com']`；② To 禁止包含 `peng.cui@smyze.cn`；③ 必须有 reply 线程证据（`in_reply_to` 存在或 `thread_id != message_id`）。用 07-24 错误邮件实测已 FAIL：缺 Sam/发自己/非 reply 均被拦截。教训：**收件人与线程属于 P0，不得用“预览发自己”替代；凡未通过线上草稿投递安全校验，一律禁止通知用户发送，更禁止直接发送。**
- **【版式再漂移·2026-07-24】intro 塞概述导语＝工具链有了但内容闸门有洞**：本周 content.json 的 `intro` 写成"以下是本周…的工作汇报。**本周共提交53次…缺陷修复占比约58%…主要精力集中在统一权限体系…**"——把 SOP line 28 明令禁止的概述导语和 bug 占比一并塞进引言。gen 脚本忠实渲染（结构没漂），但 verify 旧的 7 项只查 HTML 结构，**没人管引言正文写了什么**，于是违规版顺利过闸并发到自己邮箱预览被用户逮到。根治：verify 新增第 8 项机械闸门「引言干净一行」——纯文本里『工作汇报。』与『一、』之间必须为空，任何字符即 FAIL；bug 占比/提交数按 line 39 只能放「一、」末尾 bullet。教训：**光锁结构不锁内容仍会漏；凡 SOP 用文字规定的"禁止写 X"，都要有对应机械 check，否则等于没规定。**
- **【版式事故·2026-07-10 原始记录】**：用户投诉"格式又不对"。根因＝用了 `（一）（二）` 中文括号编号 + `&nbsp;&nbsp;•` 手打符号 + 纯黑字，而上周已发送周报是飞书富文本。修正基准＝上周已发送那封的 body_html。此坑现已被上方工具链根治。
  - 段落/标题：`<div style="margin-top:4px;margin-bottom:4px;line-height:1.8"><div dir="auto" class="" style="font-size:16px"><span style="font-size:14px"><span style="font-family:-apple-system, &quot;PingFang SC&quot;, sans-serif"><span style="color:rgb(51, 51, 51)">…</span></span></span></div></div>`
  - 一级章节 `一、二、三、` 与二级小标题 `1. 2. 3.`（阿拉伯数字，**非**中文括号）都用 `<b>…</b>` 加粗
  - 条目用真列表：`<ul style="margin:0px;padding-left:0px;list-style-position:inside">` + `<li class="temp-li bullet1 font-size-14px" data-list="bullet1" style="…list-style-type:disc;…">`，**禁止**手打 `•`
  - 字色统一 `rgb(51,51,51)`、字号 `14px`、字体 PingFang SC
  - 落地：本周内容套进上述模板生成纯正文 HTML（不含引用尾）→ `patch.json` 用 `set_reply_body`（自动保留引用块）→ `+draft-edit --patch-file --set-subject`；改完回读校验 `<b>1.`>0、`<ul `>0、无 `（一）`、无 `•`、`history-quote-wrapper` 仍在
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