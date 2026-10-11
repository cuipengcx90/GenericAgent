# linuxdo_browse_sop (L站只读随机浏览任务)

场景：用户要求去 linux.do 随机浏览 N 帖（跨分类/标签），强调「不要太快、每帖随机停留一段时间、多往下翻」。**只读版铁律：全程零发帖/零回复/零点赞**（回复版是另一任务，曾单独做过 20 帖回复）。

## 流程（单任务 tab）
1. 建工作区 `temp/lbrowse_<date>/`：
   - `plan.json` 候选帖 `[{id,cat,title}]`：跨分类混选，id 去重，剔除已读/已回复过的帖
   - `state.json` `{idx,last_id,ts}`；`log.txt` 每帖一行 `ts n=<k> DONE|SKIP <id> <cat> <摘要> SCROLL<步数>`
2. 每帖两步循环：
   - a. **驻留步（code_run python）**：随机 sleep 60–130s（25% 概率 130–200s）→ 追加 log 行 → `state.idx++` → print 下一帖。**timeout≥200**（sleep 长会被工具强杀）。
   - b. **阅读步（web_execute_js，显式传任务 tab id）**：SPA routeTo → 等标题 → 深翻。
3. 收尾统计以 log 为准（plan 顺序≠实际读数；log.txt 若疑被截断用 log.jsonl 对账），DONE−SKIP 与用户目标对账。

## 阅读步 JS 模板
```js
const tid='123456'; let how=null;
try { const m=window.require('discourse/lib/url');
  if (m && m.default && m.default.routeTo){ m.default.routeTo('/t/topic/'+tid); how='REQ'; } } catch(e){}
if (!how) return JSON.stringify({need_loc:true});          // 探不到 → location.href 兜底
for (let i=0;i<25;i++){ await new Promise(r=>setTimeout(r,500));
  const h1=document.querySelector('.fancy-title');
  if (h1){ for(let s=0;s<14;s++){ await new Promise(r=>setTimeout(r,950)); window.scrollBy(0,420+Math.random()*300); }
    return JSON.stringify({how,title:h1.innerText.trim().slice(0,60),posts:document.querySelectorAll('.topic-post').length}); } }
return JSON.stringify({fail:true,now:location.pathname});
```
返回 `{need_loc:true}` → 下一步 `location.href='/t/topic/'+tid` 整页直达（会重启 SPA，之后恢复 routeTo）。深翻步数随用户「多翻多停」要求取 12–20。

## 坑（实测）
1. **死帖(404)路由后 DiscourseURL 丢失**：routeTo 返回 need_loc → 整页直达恢复；此后每次 routeTo 前仍先探 require。
2. **整页导航后 `require('discourse/lib/url')` 可能 undefined**：routeTo 前必须先探，探不到走 location.href，勿空转重试。
3. **驻留 sleep 必须放 code_run**：web_execute_js 里长 sleep 会挂满超时被强杀且占住 tab。
4. **只动任务 tab**：用户浏览器常有自己 tab，所有 web 调用显式传 `switch_tab_id`，禁碰其他 tab。
5. 「随机」要真随机分布（`random.choice` 混合区间），非固定间隔；深翻步数同取随机。

## 「浏览帖子」计数不涨的根因与修法（2026-10-05 实测）

现象：40 帖只 +291（≈7 楼/帖），浏览话题涨但 posts_read 几乎不动。
根因：Discourse posts_read 只计被 `POST /topics/timings` 上报的楼层——screen-track 每 1000ms tick、tick 瞬间该楼在视口内才 +1s、约 60s flush 提交（话题切换 reset 只提交已收集部分）。快滚（14 步×950ms×~500px）+ 每帖仅待 ~20s ⇒ 每楼 1–2 个 tick 易错过、flush 常赶不上 ⇒ 大量楼层丢失。
修法（慢滚+底驻+回滚三件套，手动与 daemon 同款）：
1. 滚动步长降到 300–440px（<半楼）、间隔 1.2–1.8s、22–30 步 ⇒ 每楼 ≥2 个 tick 覆盖
2. 滚到底后**底部驻留 70–84s**（跨 60s flush 周期，flush 必触发一次）
3. 回滚 10 步（每楼二次 tick 机会）
预期：长帖可计 20–40 楼/帖（旧节奏 ~7）。

### ★深层根因订正（2026-10-05：调参数不是根治，screen-track 有两道闸）
慢滚/底驻只是提高 tick 覆盖率；screen-track 的 tick 本身带两道闸，闸一锁 tick 全停=参数怎么调都没用：
1. **SPA 事件闸**：`scroll` 事件超 180s 无触发 → tick 停走（帖内自动加载楼层、hash 跳转都不发 scroll）。帖内手动 scroll 一次才解锁。
2. **_onscreen 数据源**：tick 只统计 `track._onscreen` 登记的楼层；自动加载的新楼层进不了登记 → 喂 DOM 楼层号 `.topic-post[data-post-number]` 进 `track.set('_onscreen',…)` 才兜得住。

**根治三件套注入**（每帖路由完成后注入一次；已 patch 进 `tools/linuxdo_browse_daemon.py` 的 ROUTE_JS 头部，幂等、`window.__tmInterval` 跨帖存活——**完整实现以 daemon 源码为准**，此处只记机制）：
- ① 拿 track：`Discourse.__container__.lookup('service:screen-track')`（`require('discourse/app')` 在部分版本不存在）
- ② 喂楼层：读 `.topic-post[data-post-number]` 全量喂 `_onscreen`
- ③ 解锁闸：`dispatchEvent(new Event('scroll'))`（+pointerdown/keydown 保险）
- ④ 保证节拍：`window.__tmInterval` 1.5s tick（=screen-track 同款）+ 手动 flush 兜底（跨过 ~60s flush 周期）
- 终验铁证：hook 日志 `POST /topics/timings` 200；`/t/<id>.json` 顶层 `last_read_post_number` 入账（实测 2982661 last_read=8 / 最高 8 楼=整帖读完）。

**pick_session 陷阱（已修三处）**：HINT 子串匹配会把 `connect.linux.do` 的 Error 页 tab 当主站会话（HINT 含「linux.do」子串）→ 误选无 Discourse 环境的 tab、routeTo 全灭。判据改 **host 精确匹配**（候选过滤+兜底导航后 host 复核两处），`connect` tab 保留给 TL3 等级检查用。

**后台跑法**：单帖 dwell+gap 远超 code_run 300s ⇒ 必须 `tools/bg_task.py start --name <id> -- <绝对路径> browse --batch N` 后台跑+轮询 status，禁前台直跑（会被强杀造成「假卡死」）；stdout 有缓冲，对账看 `browse_log.jsonl`+`state.json`（落点 `temp/linuxdo_browse_daemon/`），别只看日志文件大小。

## 接入 TL3 维护链路（2026-10-05 后台版，用户授权）

触发 = linuxdo_trust_level_sop 检测**不满足**时，后台执行本浏览链路**直到满足**（满足即自然停）。
后台运行器 = `tools/linuxdo_browse_daemon.py`（零 LLM token；HTTP 打 TMWebDriver 桥 `127.0.0.1:18766/link` 复用已登录 tab；`--selftest` 离线自测 35/35；桥由独立保活进程 `tools/tmwd_bridge_watch.py` 托管（18765 WS/18766 HTTP），**不再依赖 Galley 座舱**，见文末 R556 节）：
- `watch` 长驻判停循环：每批后重读 TL3 等级，**按页面 ✓/○ 标记**判 pending==0 即退出（禁数值机械比较——上限类指标会误报）；`browse --batch N` 手动一批；`check`/`status` 只读
- 防检测（后台版在单任务版之上再拉稀）：帖间 110–260s（30% 概率 300–600s）+ 慢滚 22–30 步×1.4–2.0s + 底部驻留 90–125s + 25% 浅读（刷到不感兴趣划走型）+ 每批随机 2–5 次只逛列表页不进帖 + `--daily-cap` 每日话题上限（内核 `daily_cap=200` ± `cap_jitter=20` ⇒ 实际 180–220，每天摇一次、跨重启稳定）+ 全天切成 `seg_n=6–8` 段 × 每段 `seg_len=85–160min`（内核 `day_segments` 日期种子派生 ⇒ 重启不重置段位；段总长 ≤ 活动窗 80%，段间随机长休 ~130–150min） + 静默窗结束后晨抖 0–60min 再开工 + 静默窗 01:00–07:30；只读铁律不变（零发帖/回复/点赞/关注）
- 收尾统计同款以 `temp/linuxdo_browse_daemon/browse_log.jsonl` 为准；等级检查记录落 `level_log.jsonl`
- 风控信号与休息日另有独立台账：`risk_log.jsonl`（每次风控信号一行，含 kind/原文片段/动作）、`rest_log.jsonl`（休息日判定）

## 风控保险丝与随机休息日（2026-10-05，anyrouter 被封复盘）

背景：anyrouter 账号被封的教训三条=①累计高频自动请求是封禁主因 ②**把异常当常态**（429/503 当供给态一路重试，直到 403「用户已被封禁」才知）③无自动告警，靠人发现。本守护据此加固：

- **风控即停（铁律）**：daemon 把 HTTP 码与页面/响应文本两级判据映射到信号类别（`RATE_LIMIT`/`FORBIDDEN`/`BANNED`/`LOGIN_REQUIRED`/`CF_CHALLENGE`）；命中即 **写 state + 落 `risk_log.jsonl` + 飞书私信告警**（`reflect_scripts/reflect_common.notify_feishu`）+ **长暂停 60–120min**，绝不静默重试。
- 同类信号当日累计 ≥3 ⇒ **当日停止浏览**并强制告警；同类告警有 6h 冷却（防刷屏）。
- 强信号集 `RISK_STRONG` 只含 `BANNED/RATE_LIMIT/FORBIDDEN/LOGIN_REQUIRED`；`CF_CHALLENGE` 刻意**不**入强信号（常见且多半自愈，否则误伤成小时级停机）。
- **批内熔断（R563 补，2026-10-08）**：风控判定原先在 `browse_batch` **返回之后**才做 ⇒ 抓取阶段的失败诊断容易被静默吞掉，只有凑齐一个完整批后才轮到风控检查，命中窗口偏晚。10-08 实测时间线（browse_log/risk_log 原始台账）：19:55:37 开机后 daemon 起跑（watchd `DAEMON_NEVER_RUNNING` 补拉）→ 20:00–22:02 正常浏览 **20 帖**（`DONE`×20）→ **22:06:47** 抓到当日唯一一次 `FORBIDDEN page=5 status=403`（risk_log 仅此一条）⇒ `handle_risk` 暂停 → 23:20 恢复（间隔 ~74min，符合 RISK_PAUSE 60–120min）。现改为**抓取阶段**判定：`fetch_verdict()`（纯函数）在「**所有**采样页失败 ∧ 诊断为强风控信号 / 封禁文案」时返回 `ABORT` ⇒ `browse_batch` 抛 `RiskAbort`（`browse_log` 落 `ABORT why=risk_abort:<kind>`，**零帖**）⇒ 主循环走 `handle_risk` 长暂停。部分页失败但仍有候选 ⇒ 只 `NOTE` 留痕（单页抖动不该整批空转）。抓取失败时诊断里额外留**响应正文前 200 字**（`LAST_FETCH_DIAG["detail"]`），用于区分 CF 挑战 / 封禁文案 / 限流。
- **随机休息日**：平均每 7 天 1 天完全不浏览（`REST_DAY_P=1/7`），两次休息至少隔 3 天不连休；判定一次即落盘（幂等），当日零浏览。
- 自测：`python3 tools/linuxdo_browse_daemon.py --selftest` ⇒ 计数随版本增长，**当前口径以源码/最近一次运行输出为准**（R572 实测 **38/38 PASS**；本行原记 14/14 系 2026-10-05 时点）。含风控判据正负控、风控链路 stub 通知不真发、休息日幂等与常量边界、T14「自测零污染生产 state」。

## 反检测审查强化（2026-10-05）

- **深浅分层**：真人不是每帖都读到底。daemon `browse_one` 已内置 `LIGHT_P=0.30` 浅读分支（快滚 4–6 屏、步速 0.25–0.5s、驻留 15–30s、不回滚），其余 70% 沿用深读慢滚+底驻；`browse_log.jsonl` 每行带 `mode=LIGHT/DEEP` 字段。
- **判读口径**：浅读帖照样计入浏览话题数（主指标不受损），但 posts_read（楼数）偏少——这是预期行为不是漏报，勿当故障修。
- **分类偏好加权**：每批（≥8 帖）随机定 2–3 个偏好板块占约 60%，其余散选——真人会「进一个板块连看」，均匀跳遍全站分类是机器味（已在 `browse_batch` 实现，偏好分类每批重抽）。
- 回复版的去 AI 味与指纹铁律见 `memory/linuxdo_reply_sop.md` 的「去AI味与反检测强化」节。


## 事故：自测污染现场致守护自杀（2026-10-05，已修）

现象：`watch` 守护跑了几轮后无声退出，`browse_log.jsonl` 不再新增 DONE、`bg_task` 状态转 dead；表象像「站点封了 /latest.json」，实则本地自毁。

根因（已受控复现）：`--selftest` 的 T13 走的是**生产路径**（`rest_day_today()` → `save_state()`），把生产 `state.json` 整个覆盖成只剩 `rest_days` 一个键（66B）；守护下一轮 `load_state()` 读到缺 `daily` 的 state，`KeyError: 'daily'` 当场退出。⇒ **自测把现场给吃了**。

修法三件（提交 `50368cf`，同文件 51+/5−）：
1. `load_state()` 对全部键 `setdefault`（缺键回落默认值，不再 KeyError）；
2. `browse_batch` 取 `daily` 前同款 `setdefault`（双保险）；
3. `--selftest` 改名 `_selftest_impl()` + 外层包装：把 `BASE` 指到 `tempfile.mkdtemp(prefix="ldo_selftest_")` 沙箱跑，跑完 `rmtree` 并还原 `BASE`，再新增 **T14**：比对生产 `state.json` 指纹（mtime+size+sha256）自测前后必须逐字相同，变了即 FAIL。

铁律（通用，已同步 L1）：
- **自测/金丝雀禁写生产状态**：任何带副作用的函数（save_state/落盘台账/发通知）在自测里必须走沙箱 `BASE`/隔离 `--state`，且自测结束**用指纹断言生产侧零变化**——「测试全绿」不能只证明管道通，还要证明它没污染现场（RULES#25 的同族）。
- **守护判活别只看日志尾部**：`bg_task` 的 stdout 常 0 字节（输出不落该文件），权威判据是 `browse_log.jsonl` 新增 `DONE` 行 + `state.json` 的 `daily` 增长。
- 判「站点封了」之前先查本地 state 完整性（键是否齐、字节数是否异常小），别把自家自杀当成平台故障。

## 缺项路由（2026-10-05：由等级前置检查驱动）
`memory/linuxdo_trust_level_sop.md` 的等级卡判读若出现**浏览类缺项**（浏览话题/浏览帖子/已读帖子），即路由到本 SOP 的 daemon 自动补足（只读，随机节奏）；
**须人工的项**（回复/点赞/获赞/访问天数）由 daemon 私信通知（每天≤1 次，幂等），本 SOP 不代操作。
路由实现=daemon `classify_pending()`/`maybe_notify_manual()`（`MANUAL_KINDS` 正则）。

## 守护生存面：Galley 解耦与浏览器自愈（2026-10-06 R556）

用户裁决：**不把「开着 Galley 座舱」当前置**；就用日常 Chrome（非无头）；浏览器关了要能自动重开。三层自愈链：

1. **独立桥保活** `tools/tmwd_bridge_watch.py`（新）：长驻监督 `18765 WS/18766 HTTP` 两端口，桥不在即拉起（失败退避重试、收手守卫），与 Galley 的 runner.workbench_bridge 彻底解耦；`--selftest` 离线自测 PASS。
2. **看门狗兜底** `linuxdo_daemon_watchd.py ensure_bridge()`：桥不可达 ⇒ 先自动拉起独立桥（冷却 600s、起后等 15s），仍不可达才判 `BRIDGE_DOWN` 只告警不重启；`status` 增 `bridge_reachable`/`bridge_runner_pids`/`bridge_ensure`（selftest 86 checks / 0 failed）。
3. **标签自愈** daemon `try_ensure_tab`：无 linux.do 标签 ⇒ AppleScript 补开并导航——**Chrome 未运行时该调用会顺带拉起 Chrome**；补开前先查 EXISTS 防标签堆积，日上限 6 次/间隔 10min，台账 `temp/linuxdo_browse_daemon/ensure_tab.jsonl`(★台账名以源码 `BASE/ensure_tab.jsonl` 为准;旧文档写 `ensure_tab_ledger.jsonl` 系笔误)。

真机链（21:12）：watchd 起→`BRIDGE_DOWN`→独立桥拉起→daemon 重生→补标签顺带拉起 Chrome→扩展连上→ROUND OK；21:17 新码短退避生效（`RC_NO_SESSION_RETRY …（1/8，睡 40s）`）。

- **短退避**：Chrome 冷启动仅 1–2min，旧码无会话首轮睡 10–20min 纯属浪费 ⇒ 新增 `SESS_RETRY_SHORT=(20,40)s`、前 `SESS_RETRY_SHORT_MAX=6` 次短退避，用尽才走原长退避（新码日志可取证 `1/8，睡 40s`）。
- ★真缺陷：`try_ensure_tab` 用 `io.open` 读写台账但模块从未 `import io` ⇒ 每次抛 NameError 被 `except` 吞 ⇒ **补开限额从未生效**（可无限堆标签）。修=补 `import io` + 用例 T7o/T7p/T7q（限额/EXISTS 去重/短退避边界）。★判据：`except` 兜底的路径必须先证明「正常路径能走通」。

## 事故：主站标签被导航至 connect 后自愈失效（2026-10-06 R557）

**症状**：浏览停摆 64min（21:52–22:56），`browse_log` 连出 19 轮 `FAIL why=non-linuxdo-tab:connect.linux.do`；当日计数停在 53（上限 180 ⇒ **不是限额**）。

**根因 A（直接因）**：会话漂移后两条判据互相矛盾——
- `pick_session()` 用精确 host（`in ("linux.do","www.linux.do")`）⇒ 无候选、判「没有可用标签」；
- `try_ensure_tab()` 的 AppleScript EXISTS 判据用**子串** `contains "linux.do"` ⇒ 被 `connect.linux.do` 骗过、判「已有主站标签」⇒ **不自愈**，两边僵持空转。
修法=判据单一源：`MAIN_HOSTS` + `_url_host()` + `_is_main_host()`，`pick_session`/`try_ensure_tab` **共用**；AppleScript 改为回传全部命中 URL（`URLS:` 前缀）再由 Python 精确判 host。**禁再写 `in "linux.do"` 之类子串判据。**

**根因 B（诱因）**：`check_level()` 会把唯一主站标签导航到 `connect.linux.do`，旧码只在「读到等级」那条路径还原 ⇒ 一旦 `CONNECT_JS`/解析异常或进程被打断，标签**永久留在 connect**（本次事故现场即此）。修法=`try/finally` 兜底还原 + `_nav_connect` 标记（没导航过不乱跳）。

**另修**：`browse_batch` 遇 `non-linuxdo-tab` 即中止本批（原先逐帖重试把 19 次机会烧在同一故障上），交回主循环走自愈。

**自测**：33→35。新增 T7r 负控（只有 connect/sharedchat 标签时必须判「无主站」并新建；`_is_main_host` 子串误判必 FAIL）与 T7s 结构闸门（读源码断言三处补丁仍在，**不绑现场内容**）。

**现场恢复**：`osascript` 补一个后台 `https://linux.do/` 标签 ⇒ 会话注册、`pick_session` 立即可选；重启 daemon（新 pid 80887）载入新码 ⇒ 连续 `ROUND OK`。

**教训（通用）**：
1. 同一语义禁两份判据——子串匹配会被同族子域骗过；host 判定必须精确且**单一源**。
2. 「有标签」≠「能用」：自愈判据必须与消费侧（`pick_session`）用同一个函数，否则出现「一边说没有、一边说有」的永久空转。
3. 把主站标签导航去别处的分支都要 `try/finally` 还原，并留可观测证据。
4. 判停摆是否「到上限」先看 `state.daily` 与当日 cap，别凭感觉。

## 看门狗告警语义可读化 + 休眠自声明（R563，2026-10-08）

**背景（用户视角）**：开机后连收两条通知——「被看门狗重启：DAEMON_DEAD」与「维护守护已自动重启（daemon 此前不在跑，已由定时任务步骤 3 拉起）」。二者其实描述**同一件事**（机器整天关机 ⇒ 开机后守护按计划补拉 daemon），措辞却像「出了故障」+「重复提醒」。**无异常**，属正常补拉。

**判据（watchd）**：`decide()` 返回 `{"action":"RESTART","reason":"DAEMON_NEVER_RUNNING"}` 时（daemon 从未在跑），台账/通知一律按**启动**而非**重启**呈现：

- `kind` 三分：`STARTED`（首次启动，reason 以 `DAEMON_NEVER_RUNNING` 开头）/ `RESTART`（真重启成功）/ `RESTART_FAIL`（重启失败达阈值）。
- `STARTED` 文案：「ℹ️ L站浏览守护此前未在运行（<原因>），看门狗已按计划启动它，节奏参数未变。」——**不出现**「重启」「已拉起」字样。
- 是否「曾经活过」用 `_ever_alive(runtime)` 判（近 6h 内有活记录 ⇒ True）；无记录/坏值一律 fail-closed 判 False ⇒ 归「启动」语义（宁可说启动，不说崩溃）。
- 真活着又突然消失 ⇒ 仍是 `DAEMON_DEAD` + `RESTART`/`RESTART_FAIL`，**不变**（不许被新分支吞掉）。

**休眠自声明（R558 既有）**：daemon 每次 `_nap` 前写 `temp/linuxdo_browse_daemon/heartbeat.json`（`ts/pid/phase/next_wake_ts`），watchd 见 `next_wake_ts` 在未来且 ≤12h ⇒ `RESTING_UNTIL_<phase>_<min>m` 判 OK 不重启；静默窗长睡不再被误当停滞。

**自测**:daemon `--selftest` **38/38**(R572 增 T19 会话失效换会话重试 / T20 桥式建标签兜底[osascript -1743]);watchd `--selftest` **98 checks / 0 failed**(含 T34a–T34h:启动文案须含「此前未在运行/不是崩溃」、真重启文案不得含该串、`_ever_alive` 四态);沙盒断言,生产 state/台账指纹前后一致。


## 事故:主站自愈空转(子串判据第二次踩坑,R568 2026-10-10)

**症状**:用户报「帖子一直没浏览」。浏览器里只有 `connect.linux.do` 标签、主站标签不存在;
`try_ensure_tab` 当日 6 次自愈额度已耗尽,而 `ensure_tab.jsonl` 行 3–8 全为
`ok=true note=URLS:...connect.linux.do...` —— 看似「补过标签」,实则一次都没建。

**根因**:AppleScript EXISTS 判据用**子串** `contains "linux.do"` ⇒ 被同族子域骗过,
命中「已有主站标签」分支直接 `return "URLS:"` 早退、跳过建标签;Python 侧又把 `URLS:` 回执
一律当成功(`ok=True`)⇒ 额度记满、主站标签从未补上。与 R557 同源:消费侧 `pick_session`
用精确 host,自愈侧用子串,两边判据不一致。

**修法(三处)**:1 AppleScript 收集标签时置位精确 host 门控 `_main`(`//linux.do/`、
`//www.linux.do/` 及尾部形式),仅 `_main` 非空才 `URLS:` 早退;2 Python `URLS:` 分支
**fail-closed**(回执不含主站 ⇒ 返回 `(False, "未新建主站标签(...)")`,不算成功、不记账为成功);
3 `CREATED:` 分支解析新建结果(保留「建完切回原活动标签」)。自测 **37/37**(T7r「仅 connect/sharedchat
时必须判无主站并新建」负控、T7r2 以 `now+700s` 跨 10min 节流、T7s 结构闸门读源码断言三处补丁仍在、
不绑现场内容;自测走 tempfile 沙箱,生产 state/台账不被写)。

**校验法(判「自愈是否真生效」)**:读 `temp/linuxdo_browse_daemon/ensure_tab.jsonl` 末条 ——
`ok=true ∧ note=URLS:` 只含子域 ⇒ 仍是空转,必须修判据而非补标签;真补上时 note 为 `CREATED:`。
守护存活另看 `heartbeat.json`(phase/next_wake_ts)与 `browse_log.jsonl` 新增 `DONE`。

**铁律**:同一语义禁两份判据;早退/`except` 分支必须先证明「正常路径能走通」,且**不得把「没做事」记成成功**。

## 稳定性三处修复(R572,2026-10-11)

**背景**:用户报「老不稳定,要么就断,要么就啥」⇒ 只读取证后定位三处,全部修在
`tools/linuxdo_browse_daemon.py`(自测 36/36→**38/38**,新增 T19/T20)。

**D1 取帖会话失效 ⇒ 换会话重试**:`fetch_round` 原在单会话上取帖,会话被回收(标签关掉/桥重连)
时整批落空且不重试。现改为:首选会话失败 ⇒ **换一个 linux.do 会话重试一次**;★`RiskAbort`
(风控`BANNED`/`429`等强信号)**不允许被重试绕开** —— 命中即中止本批,绝不换会话再打。

**D2 零产出分类**:批内 0 帖时区分 `FETCH_FAIL`(取帖全失败=链路/环境问题)与
`ALL_SEEN`(抓到了但都见过=正常)。原二者混为一谈,导致「链路坏」与「没新帖」在台账上不可分。

**D3 `osascript` 被系统拒(-1743)⇒ 改走桥建主站标签**:实测 `osascript`
`count windows` / `count tabs` / `get URL` **三命令全返 -1743**(rc=1,Automation 权限被拒),
`ensure_tab.jsonl` 10-10 两行 `note=-1743` ⇒ R568 的「建标签」自愈在无权限时根本走不到。
现修:`_try_bridge_open_main_tab()` —— osascript 失败时改由桥 `window.open` 建**后台**主站标签
(实测不抢前台),建后 3s 内以 `_is_main_host` 精确 host 复核;兜底路径同样 fail-closed
(未建成就返回失败,不记成功)。影响面仅 `try_ensure_tab`;其余链路(取会话/取帖)本就走桥。

**校验判据**:`ensure_tab.jsonl` 末条 —— `ok=true ∧ note=CREATED:` 才是真补上;
`note=URLS:` 只含子域=空转(R568);`note=-1743`=桥兜底未生效(R572 后再见此即回归)。

**铁律**:外部命令的 rc 不可当能力可用证据(-1743 是「没权限」不是「session 忙」);
重试必须与风控信号互斥(升级失败不得绕过熔断);「跑而不产出」要能在台账上区分「坏」与「正常空」。
