# linux.do 帖子深度阅读与总结 SOP

## 触发条件
用户发一个 linux.do 帖子链接（尤其是"合集/整理贴"类，正文里挂了很多子帖链接），要求深度阅读总结，包含子链接。
**默认行为**：完成阅读总结后，同步执行步骤7归档到 Obsidian 知识库（`/Users/cuip/develop/obsidian-note/`），不需要用户每次重复要求；用户明确说"不用存/不归档"才跳过。

## 核心原则（踩坑教训，务必遵守）
1. **禁止凭"发出过读取请求"就宣称"全文已读完"**。必须用 `fetched.length === posts_count` 做机械校验，校验不过要重试补拉，不许口头担保。
2. **主帖与每个子帖逐一处理，不允许合并总结**。哪怕两三个子帖内容相似，也要分开列出 ID/标题/楼层数/核心内容，用户明确要求"按子帖子也进行总结"。
3. **保留热门楼层原文**：按点赞数排序取前 3-5 层，摘录原文关键句，不要压缩成一句话概括丢失细节和"原味"。
4. **输出格式：按列数选表格或列表，不要一刀切**。
   - 依据：唯一实测证据是一次OCR截图显示断点落在表格渲染处，不能证实是"飞书表格必截断"，更可能是宽表格挤压/换行导致的显示问题，不是内容丢失。**未做过表格vs列表的等长对照测试，这条不是确凿结论，只是保守规避**。
   - ≤3列的简单对照（如：插件名/一句话功能/是否推荐）：用表格，比列表更直观，横向对比是表格的强项。
   - ≥4列或单元格内容是长句子：改成"每个条目一个分块，属性作为子要点列出"，不要硬塞进宽表格。
   - 多工具/多插件对比这种"条目×属性"矩阵：按属性列数判断，列数多时可按功能分类拆成多张小表格（每张≤3列），而不是无条件都做成列表。
5. **涉及"伪造请求头/绕过服务商客户端限制"一类内容**：可以如实总结帖子在讨论什么，但不协助实现、不提供可执行的绕过方法。
6. **长内容如需分段发送，必须一次性把所有分段连续发完**，不要只发第 1 段就结束这一轮等用户喊"继续"——那会被误认为截断。
7. **归档到知识库时，正文必须关联原始外部链接并去重**：不能只在 `sources` 字段挂内部文件路径，正文提到"子帖N/某帖"要挂 `https://linux.do/t/topic/{id}` 真链接，结尾加"参考链接"小节列出主帖+全部子帖 URL；同一 topic 重复引用时复用同一链接，写完用正则统计 `linux.do/t/topic/\d+` 出现次数核实唯一，不能有重复挂载或漏挂外部链接。

## 前置条件
执行前用 `web_scan(tabs_only=true)` 确认已有 linux.do 域名下的 tab（同源 fetch 需要）。没有则先导航打开帖子链接对应的 tab。

## 执行步骤

### 步骤1：抓取主帖全部楼层（不分页丢失）
在目标 tab 用 `web_execute_js` 执行（topicId 从链接 `/t/topic/<ID>` 提取）：
- `fetch('/t/{topicId}.json')` 拿到 `post_stream.stream`（全部楼层 id 顺序）和已内联的前若干楼 `post_stream.posts`。
- 计算 `missing = stream中不在posts里的id`，用 `/t/{topicId}/posts.json?post_ids[]=...&post_ids[]=...` 按 **每批≤50个** 分批补拉（URL过长/接口限制未知，50是安全值，已验证200 OK）。
- 全部 posts 按 `post_number` 排序后，**机械校验** `posts.length === posts_count`，不相等要继续补拉，不允许跳过校验。

### 步骤2：cooked(HTML) → 保留结构的文本
用 `DOMParser` 解析 `cooked` 字段：
- `<a>` 替换为 `文本[href]`（保留链接，子帖引用都在这里）
- `<img>` 替换为 `[img:alt文本]`
- 取 `body.textContent`，压缩连续空行
（此逻辑已在浏览器 JS 中验证跑通，见 turn 中 `cookedToText` 函数，可直接复用）

### 步骤3：提取子帖链接列表
对主帖文本做正则 `/\/t\/topic\/(\d+)/g` 抓取所有 `topic_id`，去重去掉主帖自身 ID，得到子帖 ID 清单。**必须逐个访问**，不能只挂个标题不读正文。

### 步骤4：对每个子帖重复步骤1-2
对子帖清单里的每个 ID，同样调用 `/t/{subId}.json` + 分批补拉，走一遍步骤1-2的抓取和转文本逻辑，产出该子帖的 `title / posts_count / 全部楼层文本`。
- 子帖数量多时（如20+个），可用批处理脚本循环跑完，不要因为数量大就抽样几个应付。
- 若子帖内又嵌套了别的 `/t/topic/` 链接（二级子帖），默认**不再往下递归**，只在正文摘要里提及该链接是什么，除非用户明确要求继续深入。

### 步骤5：识别每个帖子的热门楼层
用 `post.actions_summary` 里 `id===2`（点赞like）的 `count` 排序，取 **前3-5层**，摘录该楼层原文关键句（不是概括，是引用/贴近原话），放进总结里体现"原味"。楼层数少（≤5层）的帖子可以全楼层都摘。

### 步骤6：输出格式要求
- 主帖先给一句话定位 + 楼层数。
- 每个子帖单独一段：`ID《标题》，N楼` + 该帖核心内容 + （如有）热门楼层原文摘录。**不允许把多个子帖合并成一段**。
- 全文只用分点/编号列表，**不使用 markdown 表格**（飞书渲染宽表格会截断，已踩坑确认）。
- 如内容长需要分段发送，一次性把所有分段连续发完，不要发一段就等用户喊"继续"。
- 结尾给"是否有可推荐内容/操作建议"的结论，若涉及绕过服务商限制/伪造客户端身份等内容，如实说明帖子在讨论什么，但明确不协助实现，只讨论不执行。

### 步骤7：归档到 Obsidian 知识库（默认执行，除非用户说不需要）
知识库根目录 `/Users/cuip/develop/obsidian-note/`，规范详见该目录下 `50 Raw/index.md` 与 `60 Wiki/SCHEMA.md`（**改前务必读一遍，不要凭记忆**）。

**7.1 写入 Raw（原文存档，只增不改）**
- 目录：`50 Raw/linux-do/{topicId}-{英文短slug}/`（slug 用全小写连字符，如 `2504439-pi-agent-ecosystem`）
- 文件：`00-main.md`（主帖）+ `01-{subId}.md` ~ `NN-{subId}.md`（每个子帖一个文件，按子帖出现顺序编号）
- 每个文件 frontmatter：
```yaml
---
title: "帖子标题"
source: "https://linux.do/t/topic/{id}"
created: 帖子发布日期(YYYY-MM-DD，取不到则留空)
collected: 今天日期(YYYY-MM-DD)
type: literature
source_type: linux-do
tags: [领域标签]
summary: 一句话概括本帖核心内容
---
```
- 正文：用步骤2产出的 cookedToText 结果，保留原始楼层结构（`--- #N @author ---` 形式），不精简、不省略。

**7.2 写入/更新 Wiki（结构化条目）**
- 判断类型：帖子讨论的是某个具体工具/项目 → 可在 `60 Wiki/entities/` 建或补条目；帖子是多来源综合讨论一个主题（如某工具的生态、多个子帖串联的完整话题）→ 在 `60 Wiki/topics/` 建综述条目，这是合集贴的默认落点。
- `sources` 字段列出全部 Raw 文件路径（主帖+所有子帖），**必须加引号**，如：
```yaml
sources:
  - "50 Raw/linux-do/2504439-pi-agent-ecosystem/00-main.md"
  - "50 Raw/linux-do/2504439-pi-agent-ecosystem/01-1680124.md"
```
- 正文按 SOP 步骤6产出的总结整理（主帖定位 + 各子帖小节 + 结论），达到 `mature` 标准：summary + 至少3个核心要点 + 至少1个 wikilink（如有相关 entities 条目可链接）。
- **正文必须关联原始外部链接，不能只在 `sources` 挂内部路径**：正文里提到"子帖N/某帖"时要挂对应 `https://linux.do/t/topic/{id}` 真链接；结尾加一个"参考链接"小节，列出主帖+全部子帖的原始 URL。
- **对 URL 去重**：同一 topic 只出现一次即可（重复引用同一子帖时复用同一链接，不重复罗列）；写完用正则统计一遍 `linux.do/t/topic/\d+` 出现次数确认唯一。
- 更新 `60 Wiki/INDEX.md` 对应分类表格，加一行（文件名/标签/状态/摘要）。

**7.3 校验**
- Raw 文件数 = 1(主帖) + 子帖数，逐一确认已写入磁盘。
- Wiki 条目的 `sources` 数量与 Raw 文件数一致，不能漏挂。
- 完成后用一句话告知用户归档路径，不需要用户额外确认位置（除非帖子类型模糊难判断 entities/topics 时才问）。

## 效率优化（2026-07-30 归档耗时复盘，避免逐条摸索）
1. **entities/topics 落点判断不要多轮试关键词**：先一次性 `grep -iE "关键词1|关键词2|关键词3"` 覆盖所有候选词跑一遍 topics+entities 两个目录，不要一个词一个词试、也不要因为找不到就顺手打开无关文件核对（如误读 lbs-geo-search 之类），grep 无匹配即直接判定新建。
2. **INDEX.md 插入锚点用 grep -n 一次定位，不分段 file_read 摸索**：先 `grep -n "^## "` 定位分类标题行号 + 目标关键词最近邻行号，一步锁定插入位置，禁止靠分段翻页找"插在哪一行"。
3. **多楼层批量抽取，不逐条 code_run**：需要摘录的高赞楼层（如 top5 by like）一次性在一个脚本里循环 print 全部，不要每层单独发一次工具调用。
4. **归档后校验合并成一个脚本**：文件数核对（Raw文件数=1+子帖数）、sources 数量比对、URL 去重统计（正则计数）三项在同一次 `code_run` 里一起做完，不要拆成三次单独调用。

## 复用脚本片段
浏览器 JS（`web_execute_js`，需在 linux.do 域名下的 tab 执行）：
```javascript
async function fetchAllPosts(topicId) {
  const r = await fetch(`/t/${topicId}.json`);
  const j = await r.json();
  const streamIds = j.post_stream.stream;
  let posts = j.post_stream.posts.slice();
  const gotIds = new Set(posts.map(p=>p.id));
  const missing = streamIds.filter(id=>!gotIds.has(id));
  for (let i=0;i<missing.length;i+=50) {
    const batch = missing.slice(i,i+50);
    const qs = batch.map(id=>`post_ids[]=${id}`).join('&');
    const r2 = await fetch(`/t/${topicId}/posts.json?${qs}`);
    const j2 = await r2.json();
    posts.push(...j2.post_stream.posts);
  }
  posts.sort((a,b)=>a.post_number-b.post_number);
  return {title: j.title, posts_count: j.posts_count, posts};
}
function cookedToText(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  doc.querySelectorAll('a').forEach(a=>{
    const href = a.getAttribute('href')||'';
    a.replaceWith(doc.createTextNode(`${a.textContent}[${href}]`));
  });
  doc.querySelectorAll('img').forEach(img=>img.replaceWith(doc.createTextNode(`[img:${img.getAttribute('alt')||''}]`)));
  return doc.body.textContent.replace(/\n{3,}/g,'\n\n').trim();
}
function getLikeCount(post) {
  const a = (post.actions_summary||[]).find(x=>x.id===2);
  return a ? (a.count||0) : 0;
}
```

---

## 「人工智能技术日报+播客」每日归档专用链路（2026-09-21 实测沉淀）

适用：定时任务 `linuxdo_ai_daily_summary`（每天处理**至多一篇**新日报 → 归档 Obsidian → 仅新增且验收通过才飞书私信）。

### 发现（比 Google 更可靠）
- 日报作者固定 `xtc0809`（分类 前沿快讯 cat 78），标题形如 `linux.do人工智能技术日报+播客-YY-MM-DD`（**标题日期 = 发布日**；PDF 里的数据日期 = 标题日期 - 1）。
- **首选**站内搜索：同源 tab `fetch('/search.json?q=' + encodeURIComponent('人工智能技术日报 order:latest'))` → `topics[]` 即完整时间倒序列表（含 topic_id / title / created_at），一次拿全，还能交叉确认无其他作者同类贴。
- 备选：`/user_actions.json?username=xtc0809&filter=5&offset=0&limit=40`（filter=5 = 该用户创建的主题）。
- ⚠️ `https://linux.do/u/<name>/activity/topics.json` **不是**有效 API（返回 HTML，解析后 `user_actions` 为空），别用。
- ⚠️ Google `site:linux.do/t/topic "人工智能技术日报"` 结果严重滞后，只能兜底。

### 限流与 429（重要）
- linux.do 在密集请求后会进入 **Cloudflare IP 级 429**：页面顶部返回 `Just a moment...`。特征：**浏览器 fetch 与 curl_cffi（各 impersonate 版本、各 UA）同时 429** → 是 IP 限流而非指纹问题，换指纹/加代理都无用。
- 对策：**等待约 10 分钟**后自动恢复（实测 200）。不要试图绕 CF、不要切代理硬打。
- 附带结论：`connect.linux.do` 的 CF 卡死（见 `linuxdo_trust_level_sop`）与本次 429 是两类不同故障，不要混淆。

### 正文=纯附件时的取 PDF
- 主帖 body 只有「PDF 附件 + catbox 播客 mp3 + 小宇宙播客页 + 往期回顾链接」。
- ⚠️ **`posts_count` 不固定**：09-21 期之前=1（无回帖），2026-09-22 期=5（主帖 + 4 条回帖，如「建议弄个黑色主题」「打卡」等）。**不要拿 `posts_count==1` 当判据**；仍按通用规则抓 `post_stream.stream` 分批补齐并机械校验 `posts.length === posts_count`（5 条一次到位）。Raw 里回帖也要落一段「## 回帖（原文提取）」。
- 主帖里给的是 `/uploads/short-url/xxxx.pdf`（**该路径用 curl/curl_cffi 直连会 404**——short-url 需经浏览器会话重定向）。
- 正确取法二选一：
  1. 浏览器 fetch 后读 `r.url` 拿真实 CDN 实链，如 `https://cdn3.ldstatic.com/original/4X/0/0/f/<hash>.pdf`，再用 curl_cffi 直连下载（实测 200 / application/pdf）；
  2. 直接对 `/uploads/short-url/xxx.pdf` 做浏览器 fetch → `blob()`（SOP 里 PDF blob 链路）。
- 抽文本：`pdftotext -layout in.pdf out.txt`（本机已装，实测 1.9MB PDF → 29k 字符 / 815 行），比 Python 库更省事。

### 归档结构（与既有 09-14~09-18 期保持一致）
- Raw `50 Raw/linux-do/YYYY-MM-DD linux.do人工智能技术日报+播客.md`：frontmatter（`source_url` 主帖 / `author: xtc0809` / `report_date` 数据日期）+ 来源 blockquote（PDF/播客/小宇宙真链）+ `## PDF 全文（原文提取）` + `## 主帖正文（原文提取 · 含播客与往期链接）`。
- Wiki `60 Wiki/topics/linuxdo-ai-daily-YYYY-MM-DD.md`：今日亮点 → 模型/工具 → 教程/实战 → 社区与治理 → 总体趋势（照抄 PDF 末尾「今日技术趋势」）→ 数据概览表（抓取总量/标签新增/历史命中/今日全新话题/昨日话题后续/最终纳入日报/话题组）→ 参考链接。
- INDEX：`60 Wiki/INDEX.md` 的「### AI 工具 / Claude Code」组按**最新在前**插入一行 `[[topics/linuxdo-ai-daily-...]]`。
- 播客链接两个都要保留：catbox mp3 + 小宇宙 `https://www.xiaoyuzhoufm.com/podcast/<id>`（后者从主帖文本里直接抄，别套模板）。

### 浏览器与收尾坑（2026-09-22 实测）
- 临时 tab 收尾：用 CDP `{"cmd":"cdp","tabId":<id>,"method":"Page.close"}`，再用 `{"cmd":"tabs"}` 复核回到原单 tab；页内 `window.close()` 对新开的 CDP 驱动 tab 不保证生效，别依赖。
- `web_execute_js` 的 script 传 **JSON 数组**（想一次跑 `[{cdp...},{tabs}]`）不被支持 → 报 `Unknown cmd: undefined`；必须逐条单独调用。
- 归档后要提交 vault git 仓（历史惯例 `chore(vault): 归档 linux.do AI 技术日报 ...`）：`git add` Raw+Wiki+INDEX 三件；**提交前先 `git diff -- "60 Wiki/INDEX.md"` 确认本次只有自己新增的一行**（该文件用户可能同时在改）。若发现上轮遗留未提交的 Raw（如 09-21），顺手单独补一次提交。

### 验收脚本要点
Raw/Wiki/INDEX 三文件存在且非空 → frontmatter `sources` 逐条解析到真实文件 → 正文 wikilink ≥2 且均存在 → 正文中每个 linux.do topic URL **只出现一次**（主帖 URL 只放「参考链接」一处）→ 任一 FAIL 则不推送。


