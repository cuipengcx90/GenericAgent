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

## 正文结构与格式
```
一、本周工作内容        ← 按业务域分类 commit，不按仓库分
  （空行）
二、下周工作计划        ← 简版 3 行即可
  （空行）
三、需配合事项          ← 无则写"无"，独占一行
```
- Bug 修复占比: `fix/(feat+fix)` 百分比，附数量说明，放在一、末尾
- HTML body: 用 `<br>` 换行，`&nbsp;&nbsp;•` 做子项缩进

## 标准操作流程
1. **获取上周已发送周报**: `lark-cli mail +search --query "【周报】-崔朋" --limit 1`，必须选最新一封**已发送成功**的周报邮件，不能基于旧草稿/错误 reply
2. **Git 分析（动态扫描）**: 遍历 bar-smyze 根目录下所有含 `.git` 的子工程，对每个执行 `git log --all --no-merges --author=崔朋 --since=周一T00:00:00+08:00 --until=明天T00:00:00+08:00`，仅保留有 commit 的仓库
   - ⚠️ **禁止硬编码仓库清单**：用 `os.listdir` + 判断 `.git` 自动发现，后续新增子工程零改动自动纳入
   - ⚠️ **--since/--until 必须带显式时区 `T00:00:00+08:00`**：不带时间时 git 会用当前时刻补全，导致当天早于该时刻的 commits 被截断丢失
3. **分类统计**: 按 commit message prefix 分 feat/fix/chore，计算 bug 占比
4. **按业务域归类 + 归纳合并**: 跨仓库同一业务合并（如发票=mp+op+opserver 的相关 commit）
   - ⚠️ **禁止逐条 commit 直译**：同一需求/bugfix 链的多个 commit 必须合并归纳为一句业务描述
   - 粒度标准：每个业务域 3-5 条，每条描述一个"用户可感知的功能/修复"，而非代码变更细节
   - 示例：❌ "售后详情饮品条件块补回 dishCode/dishName 字段" → ✅ "售后详情商品信息展示优化及字段修复"
   - chore/docs/refactor 类仅保留有业务价值的（如方案文档输出），纯工程类（archive task/record journal）不入周报
5. **生成 HTML body**: 写入临时文件 `temp/weekly_body.html`
6. **创建 reply 草稿**: `lark-cli mail +reply --message-id <上周已发送message_id> --to sam.zhang@smyze-ch.com --body <HTML>`
7. **如需调整正文**: 对 reply 草稿使用 `+draft-edit` 的 patch `set_reply_body`，只替换本次新增内容，**保留 quoted content**，实现“上方本周新内容 + 下方上周已发送引用”的追加回复效果
8. **修正主题/收件人**: 必要时用 `+draft-edit` 修正 subject / recipients，但不要用 `set_body` 覆盖整个 reply，除非明确要移除引用
9. **飞书通知用户**: `notify_feishu("📋 周报草稿已生成，请检查草稿箱后确认发送")`
10. **等用户确认后发送**: `lark-cli mail user_mailbox.drafts send --params '{"user_mailbox_id":"me","draft_id":"<ID>"}'`

## ⚠️ 已验证坑点
- **git --since/--until 必须带 `T00:00:00+08:00`**：裸日期 `--since=2026-06-08` 会被 git 用当前时刻补全时间部分，导致当天早于该时刻的 commits 静默丢失（致命级，曾导致 hotfix 3 条严重 bug 修复全部漏报）
- `lark-cli mail +reply` 默认只存草稿不发送 ✅
- `+reply` 的 `--to` 会覆盖原始收件人列表，必须显式指定仅 sam.zhang
- `+draft-edit --set-subject` 可修正自动回复主题中的日期
- bash 中执行 Python 代码需用 `type: python`，不能混用
- HTML body 含中文/特殊字符时，通过文件读取传参比命令行直接传更稳定
- 周报必须基于**上周已发送成功**的周报邮件继续 reply，不能基于历史错误草稿或废弃 reply
- 若用户要“追加回复”效果，必须使用 `set_reply_body` 保留 `has_quoted_content=true` 的引用块；误用 `set_body` 会把 reply 变成仅剩新正文

## 定时任务
- 每周最后一个工作日 17:00 自动触发（`weekly_last_workday`，支持中国调休）
- 任务文件: `../sche_tasks/weekly_report_reminder.json`
- 流程: 生成草稿 → 飞书私信通知用户 → 用户确认后发送