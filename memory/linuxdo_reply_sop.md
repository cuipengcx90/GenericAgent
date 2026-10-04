# linuxdo_reply_sop (L站随机回帖任务)

场景：用户要求去 linux.do 随机搜 N 帖、每帖回 1-2 句有内容的话（**非水贴**）。曾实测 20 帖全部发送成功（2026-10-05 前会话）。

**与 browse_sop 的关系：流程骨架完全复用 `memory/linuxdo_browse_sop.md`**（tab 管理/候选帖发现/驻留/防检测/收尾对账），本 SOP 只写**回帖差异面**。两边同时注意：browse 任务是只读铁律；reply 任务**只发回复**，禁点赞/禁发主帖/禁关注。

## 差异面流程（在 browse 流程之上）

1. **前置冲突检查**：`ldo_watch`/`ldo_b*` daemon 在跑时**不得并发回帖**（同 tab 双方导航打架）→ 先 `python3 tools/bg_task.py status --name ldo_watch`，若 RUNNING 先 stop 或等它静默窗；回帖任务自己用同一套「单任务 tab + 显式传 tab id」纪律。
2. **候选帖 plan.json**：同 browse（跨分类混选、id 去重）；**额外剔除**：已回复过的帖（回自己回过的=刷屏）、锁死帖（`closed`/`archived`，POST 会 424/403）、冷帖不限（无时间偏好）。
3. **每帖三步循环**：
   - a. 驻留步 = browse 同款（code_run sleep 60–200s，timeout≥200）
   - b. 阅读步 = browse 同款模板路由+深翻；**回复前必须真读帖**：正文首楼+2-3 个高赞楼层（直接在 routeTo 后读 DOM，或兜底 `GET /t/<id>.json` 解析 `post_stream`）——防回复驴唇不对马嘴
   - c. **回复步**（见下节 JS）：文案人工/会话内拟好再发，禁脚本模板句
4. 收尾对账：逐条记录 `{id, post_id, title, raw摘要, ts}` 落 `temp/lreply_<date>/log.jsonl`；成功条数=响应含 id 的条数，与用户目标对账。

## 回复步技术（POST /posts.json）

端点已在 `linuxdo_trust_level_sop.md` 证（发主帖带 tags 否则 422；**回复只带 topic_id 不需要 tags**）；csrf 取法同款。在已登录的 linux.do 任务 tab 里执行：

```js
// 每帖回复：topicId+文案由会话传入，禁循环内生成
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const res = await fetch('/posts.json', {method:'POST',
  headers:{'X-CSRF-Token':csrf, 'X-Requested-With':'XMLHttpRequest',
           'Content-Type':'application/x-www-form-urlencoded'},
  body: new URLSearchParams({topic_id:'<topicId>', raw:'<回复文案>'}).toString()});
const j = await res.json();
return JSON.stringify({status: res.status, id: j.id, errors: j.errors||null});
```

- **首帖先试跑校准**：形状有出入时（Discourse 版本差异）第一步就会暴露——首个 200 即锁形，随后批量。
- 判读：`200 + j.id` = 成功（唯一成功证据）；`422` = 参数/校验错（看 `j.errors` 原文）；`429` = 触发频控，**等 ≥10min 再下一帖**；`403` = 登录失效或板块权限，停手检查登录态（勿重试）。
- 限速器：已发 `j.id` 会话内记下，防同帖重复提交。

## 回复内容规则（用户认可的口径，2026-10-05 前实测通过）

1. **1-2 句**，最多 3 句；禁空话水贴（「学习了」「感谢分享」「顶一下」这类零信息句）。
2. **必须点名帖内具体内容**（某个观点/某个参数/某张图的细节），证明真读过。
3. 例外：纯图帖/晒物帖可以只评论图（仍是当时用户认可的唯一例外）。
4. 语气口语化、去 AI 模板腔（禁「1. 2. 3. 分点+客套结尾」；引 `linuxdo_trust_level_sop.md` 去AI味节），可吐槽可追问。
5. 不 @ 他人、不贴链接、不提自己是 AI/自动化。

## 节奏与防检测（比浏览更保守）

- 条间 gap：**180–420s 随机**（25% 概率再 +120–240s），远比浏览版长（发帖比浏览敏感）。
- 每日上限 ≤20 条；不连续两帖同一板块；同板块 2 帖间至少隔 1 个别的板块。
- 失败重试 ≤2 次/帖；429 频控见上（等 10min）。
- 静默窗 01:00–07:30 不发帖（同 browse daemon 口径）。

## 坑（从 browse_sop 继承 + 本任务特有）

1. browse_sop 全部坑（死帖路由/tab 纪律/sleep 位置）直接适用。
2. **回帖比浏览多一步「读内容」**：跳过阅读步直接回 = 文案必然空泛，等于水贴，宁可 SKIP 该帖。
3. 锁帖未发现就 POST：看响应 status/errors 原文判断，勿把 403 全当登录失效。
4. 收尾禁止「看起来发了」：成功条数只认响应 `j.id`，逐条落 log.jsonl。
