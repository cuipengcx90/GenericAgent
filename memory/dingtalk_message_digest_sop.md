# 钉钉消息查询与知识库消化 SOP

## 目标
- 用 `dingtalk-workspace-cli` 的命令入口 `dws` 查询钉钉群聊/私聊消息。
- 默认筛选“未贴点赞/表情”的未消化消息；如果真实返回字段不支持表情判定，必须降级标注 `reaction_unknown`，不得谎称完成筛选。
- 原始消息、待消化清单、附件、消化后知识必须严格遵守 `/Users/cuip/develop/obsidian-note` 的 AI Agent 知识库管理规范。

## 认证与工具边界
- 安装包：`dingtalk-workspace-cli`；命令入口：`dws`。
  ```bash
  npm install -g dingtalk-workspace-cli
  command -v dws
  dws version
  ```
- `dws auth login` 走的是**用户 OAuth 授权**，不是 AppKey/AppSecret 应用凭证直接登录。
- `dws auth login --help` 明确支持：
  - OAuth Loopback 流：本机浏览器授权，默认方式。
  - OAuth Device Flow：`dws auth login --device`，适合 SSH/无头环境。
  - 直接提供 Token：`dws auth login --token xxx`。
- `dws auth login --help` 明确不支持：
  - 邮箱/密码登录。
  - 手机号/验证码登录。
  - 应用凭证 AppKey/AppSecret 直接登录。
- 全局参数 `--client-id/--client-secret` 只是覆盖 OAuth 客户端 ID/Secret，用于 OAuth 客户端配置；不是“用应用身份直接读取用户聊天历史”。
- 检查：`dws auth status` 必须为 authenticated；未登录先中断让用户完成授权。

## 必读知识库规范
执行任何落盘/整理前，先读：
- `/Users/cuip/develop/obsidian-note/CLAUDE.md`
- `/Users/cuip/develop/obsidian-note/SCHEMA.md`
- `/Users/cuip/develop/obsidian-note/50 Raw/index.md`
- `/Users/cuip/develop/obsidian-note/00 Inbox/index.md`
- `/Users/cuip/develop/obsidian-note/60 Wiki/SCHEMA.md`
- `/Users/cuip/develop/obsidian-note/60 Wiki/INDEX.md`
- 若要生成报告：再读 `/Users/cuip/develop/obsidian-note/70 Output/index.md`
- 若涉及项目/长期运营：再读 `/Users/cuip/develop/obsidian-note/20 Projects/index.md` 或 `/Users/cuip/develop/obsidian-note/30 Areas/index.md`

关键原则：
- 用户负责输入，AI 负责整理、分类、维护、关联。
- `50 Raw/` 是 source of truth，原始资料层，只增不改；不要把 AI 总结写成 Raw 正文。
- `00 Inbox/` 是临时待处理入口，适合放未消化清单；处理后应分发。
- `60 Wiki/` 是结构化知识库，AI 全权维护；新增/修改 Wiki 前必须按 `60 Wiki/SCHEMA.md` 判断 concepts/entities/topics，并更新 `60 Wiki/INDEX.md`。
- `70 Output/` 是基于 Wiki 产出的报告/分析/总结，不是原始消息存放处。
- `20 Projects/` 放有明确目标、会结束的项目；`30 Areas/` 放持续运营的领域。
- `99 Attachments/` 放图片、截图、文件附件，按来源和日期自动管理。

## 触发输入
- 会话类型：`private` / `group` / `all`。
- 对象：私聊用姓名或 userId/openDingTalkId；群聊用群名或 openConversationId。
- 时间：默认当天 `00:00:00` 到 `23:59:59`；支持任意 `--start/--end`。
- 表情判定：默认认为“点赞/赞”表示已人工阅读消化；若消息没有任何表情字段或点赞类表情为空，则进入待消化候选。

## 范围边界硬约束
- 用户指定私聊对象时，范围就是该对象的单聊；必须只走 `dws chat message list-direct --user <userId>` 或 `--open-dingtalk-id <openDingTalkId>`。
- 私聊任务禁止自行读取 `list-top-conversation`、置顶会话、群聊、`all` 或全量会话来“补齐上下文”。只有用户明确要求“置顶/群聊/全量/所有会话”时才允许扩展范围。
- 若对象解析不清（例如姓名命中本人、同名多人、缺 userId），先询问或只做联系人解析；不得用置顶会话替代用户指定范围。
- 输出报告必须写明实际查询命令和范围；若范围不是用户原话要求，先中断确认，不得先执行。

## Obsidian 落盘结构
根目录固定：`/Users/cuip/develop/obsidian-note`

### 1. Raw 原始消息：只增不改
路径：
```text
50 Raw/dingtalk/YYYY-MM-DD/
```
建议文件：
```text
50 Raw/dingtalk/YYYY-MM-DD/private-会话对象-YYYY-MM-DD_HHMM-HHMM.raw.json
50 Raw/dingtalk/YYYY-MM-DD/private-会话对象-YYYY-MM-DD_HHMM-HHMM.raw.md
50 Raw/dingtalk/YYYY-MM-DD/group-群名-YYYY-MM-DD_HHMM-HHMM.raw.json
50 Raw/dingtalk/YYYY-MM-DD/group-群名-YYYY-MM-DD_HHMM-HHMM.raw.md
```
规则：
- Raw JSON 保存完整返回，便于回放、二次处理、字段复核。
- Raw Markdown 只做可读化，不做价值判断；正文一旦存入不改。
- 文件名包含会话类型、对象、时间窗口；重复运行不要覆盖，必要时加 `run-2`。
- Raw frontmatter 可用：
  ```yaml
  ---
  title: 钉钉消息原始记录 - 会话对象 - YYYY-MM-DD
  created: YYYY-MM-DD
  type: literature
  source: dingtalk
  tags: [dingtalk]
  summary: 钉钉会话在指定时间窗口内的原始消息记录
  ---
  ```

### 2. 未消化清单：Inbox 待处理入口
路径：
```text
00 Inbox/dingtalk-undigested-YYYY-MM-DD.md
```
规则：
- 只放未点赞/未判定的待消化候选，不保存完整原始历史。
- 可包含消息表、主题初分组、待办、待回复、关键链接、附件引用。
- 处理完成后按知识库规范分发到 Projects/Areas/Wiki/Output；不要长期堆在 Inbox。

### 3. 附件：自动管理
路径：
```text
99 Attachments/dingtalk/YYYY-MM-DD/
```
规则：
- 图片、文件、截图、媒体资源统一放这里。
- 文件名应包含会话对象和消息ID，例如：`private-cuip-msg123.png`。
- 若媒体无法下载，保留原始消息ID、mediaId、URL 或 dws 返回引用，不阻断整批处理。

### 4. 消化后知识：按性质分发
- 抽象概念/方法论：`60 Wiki/concepts/`
- 具体工具/产品/平台/人物：`60 Wiki/entities/`
- 多来源综述/完整指南：`60 Wiki/topics/`
- 有明确目标且会结束：`20 Projects/项目名/`
- 长期运营领域：`30 Areas/research/`、`30 Areas/saas/`、`30 Areas/content/`
- 基于 Wiki 的一次性报告：`70 Output/YYYY-MM-DD-主题.md`

## Wiki 编译规则
只有当钉钉消息中沉淀出可复用知识时，才写入 `60 Wiki/`。

执行前：
1. 读 `60 Wiki/SCHEMA.md`。
2. 查 `60 Wiki/INDEX.md`，确认是否已有相关条目。
3. 判断类型：
   - concept：抽象概念/方法论。
   - entity：具体工具/产品/平台/人物。
   - topic：完整综述/指南/资源汇总。

文件规范：
- 文件名统一小写英文 + 连字符；禁止中文、大写、空格、下划线。
- frontmatter 必须包含 `title/type/tags/sources/created/updated/status/summary`。
- `sources` 必须指向 `50 Raw/dingtalk/...` 原始文件。
- 正文中首次提到已有 Wiki 页面，使用完整相对路径 wikilink，例如 `[[entities/stripe]]`。
- 每篇 Wiki 条目至少 2 个 wikilink；新增或修改后必须更新 `60 Wiki/INDEX.md`。
- 防幻觉：每句话都必须能溯源到 Raw；数字、URL、工具名直接从原文复制。

## 命令流程
1. 安装/版本/认证：
   ```bash
   command -v dws || npm install -g dingtalk-workspace-cli
   dws version
   dws auth status
   dws auth login          # 本机 OAuth loopback
   dws auth login --device # SSH/无头环境
   ```
2. 找人/找群：
   ```bash
   dws contact user search --query "姓名" -f json
   dws chat search --query "群名" -f json
   ```
3. 私聊拉取：
   ```bash
   dws chat message list-direct --user <userId> --time "YYYY-MM-DD 00:00:00" --forward true --limit 50 -f json
   ```
   `list-direct` 是从 `--time` 起按方向分页；必须本地过滤 `start <= createTime <= end`。
4. 群聊拉取：
   ```bash
   dws chat message list --group <openConversationId> --time "YYYY-MM-DD 00:00:00" --forward true --limit 50 -f json
   ```
5. 多维搜索/关键词：
   ```bash
   dws chat message search-advanced --conversation-ids <id> --start "YYYY-MM-DDT00:00:00+08:00" --end "YYYY-MM-DDT23:59:59+08:00" --limit 50 -f json
   ```

## 表情筛选规则
- 首次接入真实账号时，必须先抽样 `limit 10`，查看消息 JSON 是否包含 `emotionReplyList`、`emojiList`、`emojis`、`reactions`、`reactionList`、`emotionList` 等字段。
- 当前实测字段：私聊 `list-direct` 返回的消息可包含 `emotionReplyList: [{ emoji, replyUsers }]`；`emoji=赞` 已验证存在。判断“已贴表情/点赞”以 `emotionReplyList` 非空为准；需要判断点赞类时检查其中 `emoji` 是否属于点赞集合。
- 群聊 `dws chat message list --group ...` 当前样本只返回 `content/createTime/openConversationId/openMessageId/sender/senderOpenDingTalkId`，未返回 `emotionReplyList`；群聊场景若无表情字段，必须降级为 `reaction_unknown`。
- 若返回字段存在：过滤无表情或无点赞类表情的消息。
- 若返回字段不存在：SOP 降级为“按时间拉取未判定消息”，在 Staging/Inbox Markdown 中标注 `reaction_unknown`，不要谎称已完成未点赞筛选。
- 点赞类表情集合基于真实字段：已验证 `赞`；候选包括：`like`、`thumb`、`点赞`、`👍`。

## 消化流程
1. 拉取消息并保存 Raw JSON。
2. 生成 Raw Markdown，可读化但不总结、不改写原意。
3. 按表情字段生成未消化候选；字段缺失则标 `reaction_unknown`。
4. 写入 `00 Inbox/dingtalk-undigested-YYYY-MM-DD.md`。
5. 对候选消息做内容展开，不允许只按消息摘要或链接标题下结论：
   - 文本消息：阅读完整正文后提炼。
   - 链接消息：优先查找/生成 `50 Raw/clippings/` 全文；必须实际读取链接正文后再整理；写入 Wiki/Output 时同时保留原始 URL、来源消息、已读取范围和未读取边界，禁止只留下二次摘要导致无法复核。
   - 百度网盘分享：至少打开分享页/提交提取码并记录分享根标题；若未下载/转存/读取正文，只能作为“资源线索/主题归类”入库，表格必须包含原始分享链接、提取码、可见标题、初步判断和处理边界。
   - 图片/截图：先下载或定位附件，用 OCR/视觉工具读出内容；无法读取时只保留为待处理，不写成已消化知识。
   - 文件/PDF：先下载或定位附件，抽取正文/元数据；无法抽取时标注阻塞原因。
6. 对已展开内容做主题聚类：项目、技术资料、链接、图片/附件、待办、需要回复的人。
7. 按 Obsidian 规范分发：
   - 临时想法继续留 Inbox。
   - 完整技术调研进入 `30 Areas/research/`。
   - 项目计划进入 `20 Projects/`。
   - 可复用知识进入 `60 Wiki/` 并更新 INDEX；Wiki 正文必须是消化后的结构化知识，不能只是链接清单。
   - 针对某问题的一次性分析进入 `70 Output/`，Output 必须引用已消化 Wiki 或明确说明仍待消化。
8. 最终回复必须说明：消息总数、未消化候选数、reaction 字段状态、Raw 路径、Inbox 路径、分发结果，以及链接/图片/附件是否已实际读取。

## 幂等与核验
- 重复运行先读已有 Raw，不覆盖，只追加新 run 或新时间窗口。
- 完成核验：Raw JSON 存在、Raw Markdown 存在、Inbox 未消化清单存在；如写 Wiki/Output，必须确认 INDEX/frontmatter/sources 正确。
- 失败降级：认证失败→请用户授权；字段缺失→输出 `reaction_unknown`；媒体下载失败→保留链接/消息ID；单个消息解析失败不中断整批。
- 大批量消息（>=50 条）按知识库大批量整理 SOP：先枚举总数、拆批、覆盖率验证，不靠感觉认为处理完。

## 当前验证样例
- 用户给定测试对象：`崔朋`。
- 测试窗口：今天。
- 参考截图：`/Users/cuip/projects/GenericAgent/temp/desktop_uploads/sess-df84903453bd/505bc7978def__image.png`。
- 当前实测：`dingtalk-workspace-cli@1.0.41` 已全局安装；命令入口 `/opt/homebrew/bin/dws`；`dws auth status` 返回未登录，需先 `dws auth login` 或 `dws auth login --device` 完成用户 OAuth 授权后，才能拉真实消息确认表情字段。
