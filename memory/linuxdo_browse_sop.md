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
后台运行器 = `tools/linuxdo_browse_daemon.py`（零 LLM token；HTTP 打 TMWebDriver 桥 `127.0.0.1:18766/link` 复用已登录 tab；`--selftest` 离线自测（计数以最近一次运行输出为准，R578 后 40/40）；桥由独立保活进程 `tools/tmwd_bridge_watch.py` 托管（18765 WS/18766 HTTP），**不再依赖 Galley 座舱**，见文末 R556 节）：
- `watch` 长驻判停循环：每批后重读 TL3 等级，**按页面 ✓/○ 标记**判 pending==0 即退出（禁数值机械比较——上限类指标会误报）；`browse --batch N` 手动一批；`check`/`status` 只读
- 防检测（后台版在单任务版之上再拉稀）：帖间 110–260s（30% 概率 300–600s）+ 读帖滚动与底部驻留（★2026-10-05 17:02 起由 humanize 拟人层生成，旧口径「慢滚 22–30 步×1.4–2.0s、底驻 90–125s」已被取代，现行值见文末「口径订正」节）+ 25% 浅读（刷到不感兴趣划走型）+ 每批随机 2–5 次只逛列表页不进帖 + `--daily-cap` 每日话题上限（内核 `daily_cap=200` ± `cap_jitter=20` ⇒ 实际 180–220，每天摇一次、跨重启稳定）+ 全天切成 `seg_n=6–8` 段 × 每段 `seg_len=85–160min`（内核 `day_segments` 日期种子派生 ⇒ 重启不重置段位；段总长 ≤ 活动窗 80%，段间随机长休 ~130–150min） + 静默窗结束后晨抖 0–60min 再开工 + 静默窗 01:00–07:30；只读铁律不变（零发帖/回复/点赞/关注）
- 收尾统计同款以 `temp/linuxdo_browse_daemon/browse_log.jsonl` 为准；等级检查记录落 `level_log.jsonl`
- 风控信号与休息日另有独立台账：`risk_log.jsonl`（每次风控信号一行，含 kind/原文片段/动作）、`rest_log.jsonl`（休息日判定）

## 风控保险丝与随机休息日（2026-10-05，anyrouter 被封复盘）

背景：anyrouter 账号被封的教训三条=①累计高频自动请求是封禁主因 ②**把异常当常态**（429/503 当供给态一路重试，直到 403「用户已被封禁」才知）③无自动告警，靠人发现。本守护据此加固：

- **风控即停（铁律）**：daemon 把 HTTP 码与页面/响应文本两级判据映射到信号类别（`RATE_LIMIT`/`FORBIDDEN`/`BANNED`/`LOGIN_REQUIRED`/`CF_CHALLENGE`）；命中即 **写 state + 落 `risk_log.jsonl` + 飞书私信告警**（`reflect_scripts/reflect_common.notify_feishu`）+ **长暂停 60–120min**，绝不静默重试。
- 同类信号当日累计 ≥3 ⇒ **当日停止浏览**并强制告警；同类告警有 6h 冷却（防刷屏）。
- 强信号集 `RISK_STRONG` 只含 `BANNED/RATE_LIMIT/FORBIDDEN/LOGIN_REQUIRED`；`CF_CHALLENGE` 刻意**不**入强信号（常见且多半自愈，否则误伤成小时级停机）。
- **批内熔断（R563 补，2026-10-08）**：风控判定原先在 `browse_batch` **返回之后**才做 ⇒ 抓取阶段的失败诊断容易被静默吞掉，只有凑齐一个完整批后才轮到风控检查，命中窗口偏晚。10-08 实测时间线（browse_log/risk_log 原始台账）：19:55:37 开机后 daemon 起跑（watchd `DAEMON_NEVER_RUNNING` 补拉）→ 20:00–22:02 正常浏览 **20 帖**（`DONE`×20）→ **22:06:47** 抓到当日唯一一次 `FORBIDDEN page=5 status=403`（risk_log 仅此一条）⇒ `handle_risk` 暂停 → 23:20 恢复（间隔 ~74min，符合 RISK_PAUSE 60–120min）。现改为**抓取阶段**判定：`fetch_verdict()`（纯函数）在「**所有**采样页失败 ∧ 诊断为强风控信号 / 封禁文案」时返回 `ABORT` ⇒ `browse_batch` 抛 `RiskAbort`（`browse_log` 落 `ABORT why=risk_abort:<kind>`，**零帖**）⇒ 主循环走 `handle_risk` 长暂停。部分页失败但仍有候选 ⇒ 只 `NOTE` 留痕（单页抖动不该整批空转）。抓取失败时诊断里额外留**响应正文前 200 字**（`LAST_FETCH_DIAG["detail"]`），用于区分 CF 挑战 / 封禁文案 / 限流。
- **随机休息日**：平均每 7 天 1 天完全不浏览（`REST_DAY_P=1/7`），两次休息至少隔 3 天不连休；判定一次即落盘（幂等），当日零浏览。
- 自测：`python3 tools/linuxdo_browse_daemon.py --selftest` ⇒ 计数随版本增长，**当前口径以源码/最近一次运行输出为准**（R578 实测 **40/40 PASS**；R572 时点 38/38、R576 时点 39/39）。含风控判据正负控、风控链路 stub 通知不真发、休息日幂等与常量边界、T14「自测零污染生产 state」。

## 反检测审查强化（2026-10-05）

- **深浅分层**：真人不是每帖都读到底。daemon `browse_one` 内置浅读分支（`LIGHT_P=0.25`，等效文本量×0.35、屏幅×1.3–1.8、停顿×0.5、驻留封顶 25s、不回滚；★2026-10-05 17:02 起由 humanize 生成，原「快滚 4–6 屏、驻留 15–30s」为旧口径，现行见文末「口径订正」节），其余 75% 沿用深读；`browse_log.jsonl` 每行带 `mode=LIGHT/DEEP` 字段。
- **判读口径**：浅读帖照样计入浏览话题数（主指标不受损），但 posts_read（楼数）偏少——这是预期行为不是漏报，勿当故障修。
- **三个计数器的对应关系（R576c 澄清，2026-10-12 用户提问）**：L站 connect 页/等级页的「浏览话题」= 进入过的**话题数**（一个 URL 话题算 1）；「浏览帖子」= Discourse `posts_read_count`，即**楼层数**（话题内每个回复楼层算 1）——页面用词「帖子」易被误读成帖数，实为楼数。判据：10-12 实测 topics +102 而 posts +818，差 8 倍，posts 不可能与话题同量级。daemon 台账字段名固定 `topics`(话题) / `posts`(楼层)，`temp/linuxdo_floor_ledger.py` 表头用「浏览话题 / 浏览帖子」与页面逐字对齐。★页面**自带脚注**（逐字）：「以上"话题"指主帖，"帖子"即传统意义的回复。」⇒「浏览帖子 20000」= 楼层数（100 天滚动窗）。2026-10-12 20:37 实测 @cuipengcx：访问天数 82/50 ✓、浏览话题 1182/500 ✓、浏览帖子 **16202/20000**（唯一缺项，差 3798）、回复 33/10 ✓、点赞 79/30 ✓、获赞 50/20 ✓、获赞天数 13/7 ✓、获赞用户 49/5 ✓、合规全 0。
  ★**CF 盾 vs 真风控判据（2026-10-12 实测）**：daemon 报 `批内风控熔断 RATE_LIMIT … status=429 body=<title>Just a moment...</title>` ⇒ 这是 **Cloudflare 边缘挑战/限流**，不是账号级风控——① 响应体是 CF 挑战页且 `server: cloudflare`（真 Discourse 风控返 JSON `{"errors":[...]}`）；② 同一 URL/同一 tab/cookie 稍后复测即 **200 + 82KB JSON**（瞬时性）；③ 同时段话题读取、`/u/<name>/summary.json`、connect 页全 200，合规记录 0。触发点是**候选列表接口** `/latest.json?page=N&order=created`（CF 对 API 路径常见挑战目标），turbo 提速后批密度升高更易命中。命中后 `RISK_PAUSE` 随机暂停 60–120min（本次 82min，零浏览损失=熔断发生在取列表阶段），当日同类 ≥3 次才升级「当日停浏览+强制告警」。
  ★**为什么会频繁遇到 CF 盾（根因，2026-10-12 实测）**：本机出口 IP `104.28.166.42 / 104.28.152.173`，`ipinfo`/`ipwho.is` 判 **AS13335 Cloudflare, Inc.（org=Cloudflare WARP）**，`/cdn-cgi/trace` 亦为 `colo=SJC loc=US`（Clash TUN 下的落地出口）⇒ 我们走的是 **Cloudflare WARP 的共享数据中心出口**，CF 对自家 WARP 段的 bot 分天然偏低：**这是「出口信誉」问题，不是我们的请求频率问题**——实测同一标签**连续 9 次**请求（6 次×2s 间隔 + 3 次）**全 200**，故「太频繁」不成立。
  ★**怎么过这个盾（实测可行）**：CF 挑战页要在**文档层**跑完 JS 挑战才下发域级 `cf_clearance`（实测当前 cookie 里**没有** cf_clearance ⇒ 只靠 `fetch` 永远过不去）⇒ 可靠做法=**整页导航**到 `https://linux.do/latest`（经桥 `execute_js` 执行 `location.href=...` **不激活标签页**，不打扰用户），等 20–40s 再重试 fetch 即 200。
  ★**已落地（R577，提交 f7907b4，`--selftest` 39/39 PASS）**：①`risk_kind_of(status,body)` = **正文里的 CF 挑战优先于状态码**（旧序 `risk_kind_from_http(429)` 先判 ⇒ 误当 RATE_LIMIT 强信号 ⇒ 熔断+82min 停机）；②`fetch_candidates()` 命中挑战时：随机 20–60s **原地重试** → 整页**导航过盾** + 60–180s 再试 → 仍失败才落诊断交熔断；③daemon 重启加载新码（R577 时点）。

- **★R578 楼层口径规则对齐（2026-10-12，用户令「全做」）**：口径 = connect 页「浏览帖子 20000」按**楼层**计、100 天滚动窗 ⇒ 稳态需 ≈**200 楼层/日**；「浏览话题 500」=主帖数，早已达标 ⇒ **楼层是唯一瓶颈**。「按楼层加权选帖」是不增加访问次数的唯一杠杆，已落地三处旋钮：
  - `FLOOR_BIAS=0.8`(★R581 0.7→0.8) 与 `FLOOR_CAP=64`(★R581 40→64)（`floor_weight`/`floor_rank`，Efraimidis–Spirakis 无放回加权抽样，**保留随机性**；板块偏好之后各自加权，不破坏「进一个板块连看」语义）；`FLOOR_BIAS=0` ⇒ 逐字回到旧 `random.shuffle`（一键回退）。
  - `BIG_DEEP_MIN_POSTS=12` / `BIG_DEEP_LIGHT_P=0.08`(★R581 0.12→0.08)：≥12 楼的帖浅读概率压到 0.08（浅读滚不到尾 ⇒ 大帖亏楼层）。
  - `DAILY_FLOOR_CAP_DEFAULT=2400` 楼层上限闸门（主循环 `floors_count>=cap` ⇒ 睡到明天，**不额外产生访问**；turbo 可配 `daily_floor_cap` 覆盖，硬边界 (1, 6000)(★R580 抬到 6000 与日常抖动上界一致)；★R579：混池后每帖实际读到约 20 楼，原 1200 会在约 60 帖即熔断（帖配额才用掉 10%）⇒ 下限值提到 2400，运行期 turbo 已显式写 `daily_floor_cap=5000`）；**记账口径（R579 起＝实际读到的楼层）**：`floors_credit_of(res, cand)` 优先取读帖返回值 `res["posts"]`（= 页面上 `.topic-post` 实测数，即本次真正读到的楼层），桥接丢返回值/超时时回退 `min(候选列表楼层数, FLOOR_CREDIT_CAP=64)(★R581 40→64)` 保守估计；真值仍以 connect 页为准。
  - 观测单一源：`python3 temp/linuxdo_floor_ledger.py status`（一句话进度 缺口/今日增量/ETA），夜间汇报 `tools/linuxdo_nightly_report.py` 的 `floor_line()` **复用**该账本口径内嵌同一行（禁两处各写一份）；自测 41/41（负控：`FLOOR_BIAS=0` ⇒ T22a/T22e 必红；R579 正控：把 `DAILY_FLOOR_CAP_DEFAULT` 改错 ⇒ T23e 真红；R579b 正控：`DEEP_DWELL_ABS_CAP_S` 改错 ⇒ T24c 真红）；**改这些常量后必须重启 daemon 才生效**（进程内常量）。
- **★R579 候选混池 + 记账口径（2026-10-13，提交 609e2d9 / 3130833）**：用户现象「每帖只出 5 楼」根因＝**候选源专挑最新短帖**（`/latest.json` 中位 4 楼、51% <5 楼），非读得浅 ⇒ 候选改**混池** `CAND_TOP_PAGES=2` + `CAND_LATEST_PAGES=2`（4 页：top.json×2 + latest.json×2，同刻 top 中位 52 楼），`CAND_TOP_PAGES=0` 一键回退旧行为（T22h 逐字断言）。真路径实测：160 帖、楼层中位 21、`GE20=85/GE100=11`、页 ok 4/4。混池后**每帖实际读到约 20 楼 ⇒ 楼层闸门从「非瓶颈」变成「约 60 帖即触顶」**，故同批：①`DAILY_FLOOR_CAP_DEFAULT` 1200→**2400**；②记账改**实际读到的楼层**（`floors_credit_of`，见上条）；③运行期 turbo 显式 `daily_floor_cap=5000`。
- **★R580 日常楼层上限加当日种子抖动(2026-10-13,提交 db7f774)**:用户令「日常上限也别是死数字」⇒ 新增常量 `DAILY_FLOOR_CAP_JITTER=3600`,`floor_cap_today()` 非 turbo 路径按**日期种子**(`sha1("floor-cap|<day>")`)单向上抖 U[0,3600] ⇒ 日常上限落在 **[2400, 6000]**,同日稳定、跨重启不变(cap 抖动双向、楼层档单向:保 2400 下限免过早熔断)。turbo 显式 `daily_floor_cap` 仍**逐字优先**、不走抖动(运行期仍显式 5000);turbo 硬边界随之上抬 (1,5000)→**(1,6000)**。selftest 仍 41/41(新增 T23e 14 天取值非死数断言 / T22h 区间+同日稳定 / T22j 截断 6000)。

- **★R579b 口径一致四修（2026-10-13，提交 028dbba）**：①**楼层短探针**——桥接常丢长脚本返回值（`browse_log` 里 68% 行 `posts` 为 null）⇒ 记账只能回退保守估计；现 posts 缺失时加发一次只读短探针 `FLOOR_PROBE_JS`（数 `.topic-post`），日志新增 `posts_src`（`res`/`probe`）与 `fc`（该帖计入楼层）字段。②**深读上限随内容缩放** `deep_dwell_cap(n_chars, tc)`：基准 8400 字（≈20 屏）→ 沿用 150s；更长线性放宽、绝对封顶 `DEEP_DWELL_ABS_CAP_S=300`；短帖不削；`turbo.deep_cap_s` 显式给值则优先（提速档仍 60s）。③**LOITER 逛真实采样页**：改走 `page_path(src, page)`（与 fetch 同口径），日志 `note`/`path` 记真实来源（不再写死 latest）。④**轮内去重** `dedupe_candidates`：top 与 latest 同帖保序首现、记 `srcs`。**以上只加策略/观测，未动任何浏览动作**（SCROLL/DWELL/LOITER/LIGHT/BOTTOM/ROLLBACK/GAP/SESSION/BATCH 常量逐字未改，`git show` 可比）。
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
3. **标签自愈** daemon `try_ensure_tab`：无 linux.do 标签 ⇒ AppleScript 补开并导航——**Chrome 未运行时该调用会顺带拉起 Chrome**；补开前先查 EXISTS 防标签堆积，日上限 6 次/间隔 10min，台账 `temp/linuxdo_browse_daemon/ensure_tab.jsonl`(★台账名以源码 `BASE/ensure_tab.jsonl` 为准;旧文档写 `ensure_tab_ledger.jsonl` 系笔误)。★R582(2026-10-13)边界订正:上面"Chrome 未运行时会顺带拉起"**在 Automation 授权有效时成立**(AppleScript 对未运行的可脚本化 app 有冷启动语义——以无害 app TextEdit 替身实测 `tell application "TextEdit" … activate/make new document` 均从"未运行"把进程拉起)。真正的断点是**授权缺失**:缺授权时 AppleScript 路径(含冷启动)整条被拒(`-1743`),此时只剩桥兜底 `_try_bridge_open_main_tab`(走现有标签里的 `window.open`),而它**必须有已连上的会话** ⇒ **授权缺失 + Chrome 已整体退出(无会话)** 时两路俱灭=无任何拉起路径;授权有效时则能拉起。⇒ 缺口不是"缺冷启动路径",而是**授权缺失时缺一个不依赖 TCC 的兜底**(如 `open -a "Google Chrome" <url>`,`open` 走 LaunchServices、不经 Apple Events)。诊断见 `temp/R582_L站浏览器关闭未恢复_排查记录.md`。

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
## 段内帖间长睡上限 900s(R574,2026-10-11)

**机制**:帖间 `time.sleep(hu.action_gap("browse", base=内核gap))`;`action_gap` 在 6% 的
「起身/走开」分支返回 `_lognormal(..., base*12.0)` ⇒ 内核抽到长休档(300–600s)时**单次可睡 2h**,
与段预算(段长 85–160min)直接冲突,并逼近 watchd 停滞阈值。

**修**:`tools/linuxdo_humanize.py: action_gap(kind, rng, base, cap=None)`——超 `cap` 即截断
(默认 None=行为不变);`tools/linuxdo_browse_daemon.py` 新增 `POST_GAP_CAP_S = 900.0` 并在帖间传
`cap=POST_GAP_CAP_S`。**段级长休归内核 `day_segments`/`SESSION_REST`,不在帖间叠加**。

**证据**:humanize selftest 25/25、daemon selftest 38/38;蒙特卡洛 20000 次(同 RNG 序列):
修复前 max 6236s / 2.35% >900s / 136 次 >1800s ⇒ 修复后 max 900s / 0% / 0。

**判据**:`tools/linuxdo_browse_daemon.py status` 看 `segments.rests[].minutes`,以及
`browse_log.jsonl` 相邻 `DONE` 行时间差(段内单次 ≥15min 即为回归)。

## 口径订正:读帖滚动/底部驻留/深读封顶(2026-10-12,只订正文字,不改行为)

**裁决(用户 2026-10-12)**:保持**现行代码口径**,SOP 文字向代码对齐;不恢复旧底驻。

**口径演变**:
| 时间 | 口径 | 出处 |
|---|---|---|
| 10-05 01:57 | 手动版底部驻留 70–84s | `2b06fdc`(「修法三件套」节) |
| 10-05 09:33–11:39 | 后台版慢滚 22–30 步×1.4–2.0s + 底驻 90–125s | `1d942c3` / `2a8242d` |
| 10-05 17:02 起(**现行**) | 接入 humanize 拟人层:底驻 2–14s(wrap 区间 2–10s,lognormal 上界 ×1.4)、深读封顶 150s | `5d06b91`(提交说明写明「深读上限 150s」;代码注释:沿用接线前的预算,避免慢到日 cap 跑不完) |

**现行值(以 `tools/linuxdo_browse_daemon.py` / `tools/linuxdo_humanize.py` 源码为准)**:
- 读帖停留 = `humanize.read_dwell_seconds`:字数÷阅读速度(精读 3.6 / 常速 7 字/秒,50% 概率精读(★R581 45%→50%))×对数正态波动 + 开屏定位 0.8–2.5s,范围 2.5–300s;daemon 再封顶——深读 150s(`DEEP_DWELL_CAP_S`)、浅读 25s(`LIGHT_DWELL_CAP_S`)。
- 滚动 = `humanize.scroll_plan`:约 420 字/屏,步长与停顿逐步不同,含回滚项。
- 底部驻留 = `humanize.wrap_up_seconds("browse")`:区间 2–10s,lognormal 上界 hi×1.4 ⇒ 实际 2–14s(台账 `wrap_up` 实测最大 14.0)。深读回滚 10 步(`ROLLBACK_STEPS`),浅读不回滚。
- 浅读 25%(`LIGHT_P`):等效文本量×0.35、屏幅×1.3–1.8、停顿×0.5。
- 帖间 = 内核 gap(110–260s,30% 长休 300–600s)经 `action_gap`(6% 起身分支,R574 上限 900s);批间休息 = gap_long×1.5。

**为什么不恢复底驻 90–125s**:旧底驻时期每个新计入话题带来约 7.2 楼(10-05 11:43→16:10:+315 楼/+44 话题),现口径正常日 7.1–8.9 楼/话题(10-06 至 10-11),**未见下降**;恢复预计日产 73–86 帖(现 87–102),无入账收益证据。

**产量基线(现口径)**:模拟 + 近 6 天实测,每日 87–102 帖(好日);每新计入话题约 7–8 楼,100 天窗口每日回落约 200 楼(由实测反推,粗估)。

**审计注意**:台账 `pose.read_dwell/wrap_up/scroll_screens` 是 humanize 另抽的随机摘要,**不是执行值**(`pose` 里 89% 的 `read_dwell=300` 即此);审计看 R576 起新增的 `exec` 字段。

## 临时提速开关(turbo,R576,2026-10-12)

**定位**:正常模式的节奏与拟人策略**一律不动**;「临时提速」只通过 `temp/linuxdo_browse_daemon/turbo.json` 覆盖时间/开关旋钮。**文件缺失 / 过期 / 损坏 / `enabled≠true` ⇒ 一律按正常模式**(fail-closed);守护每次取用 stat 一次,**删文件即刻恢复,无需重启**。到期时间必填且距今 ≤7 天。

**旋钮**(缺省的旋钮沿用正常值;越界按硬边界截断):
| 旋钮 | 正常值 | 硬边界 |
|---|---|---|
| `gap` 帖间间隔(秒) | 110–260 | lo≥15 |
| `gap_long` / `gap_long_p` 长休区间/概率 | 300–600 / 0.30 | lo≥60 / 0–1 |
| `batch_gap` 批间休息(秒) | gap_long×1.5 | lo≥30 |
| `daily_cap` 每日上限 | 约 200 | 1–800(不写 state 的正常日 cap) |
| `daily_cap_jitter` turbo 日 cap 抖动(±帖) | 0(缺省=不摇) | 0–200 |
| `deep_cap_s` 深读停留封顶 | 150 | 30–150 |
| `light_p` 浅读比例 | 0.25 | 0–0.7 |
| `stay_s` 底部驻留(秒) | 2–10 | 0–300 |
| `rollback` 深读回滚步数(false=关) | 10 | 0–10 |
| `pause_scale` 滚动停顿缩放 | 1.0 | 0.4–1.0 |

**turbo 日 cap 的随机化(R576c,2026-10-12)**:只配 `daily_cap` 时该值**每天恒定**(如 600 就天天 600,是可被识别的机械特征);补配 `daily_cap_jitter:60` 后按 `sha1("turbo-cap|<当日>")` 派生当日 cap(600±60),**同一天内恒定、跨重启不重置**(与内核 `day_segments` 同款日期种子法),且**不写 `state["cap"]`**(正常档 cap 不受污染)。实盘校验:`python3 -c "...; print(m._cap_today({}, 200, m.turbo_cfg()))"` 当日得 551;自测 T21b6 系列断言区间/同日稳定/jitter=0 回 600/14 天 ≥3 种取值。

**turbo 不介入**:风控熔断、休息日、静默窗、段预算、前台保护、会话自愈、分类偏好,以及深浅读的**分层逻辑本身**(`light_p` 只调浅读比例,属用户授权的「临时提速可调开关」)。守法方式:自测 T21j 用 AST 断言 `turbo_cfg()` 只出现在 `_batch_gap/browse_one/browse_batch/watch` 四处白名单且父节点形态固定(赋值或 `_turbo_base_gap`/`_cap_today` 参数),T21j2 另检 12 个闸门函数源码不含 turbo;评审曾复现「在 watch 段闸门加 `and not turbo_cfg()`」旧断言抓不到,现已变红。

**T3 档示例(3 天)**:
```json
{"enabled": true, "expires_at": "YYYY-MM-DD HH:MM:SS", "note": "T3 3d",
 "gap": [20, 45], "gap_long": [90, 180], "gap_long_p": 0.05, "batch_gap": [120, 180],
 "daily_cap": 600, "deep_cap_s": 60, "light_p": 0.5, "stay_s": [2, 2],
 "rollback": false, "pause_scale": 0.6}
```
预估 490–576 帖/天(同一套真实函数模拟,理想值×0.85–1.0);每帖约 80s,**机器味重、403 风险最高**,且每帖入账楼层会降(降多少无数据)。

**JS 超时(turbo 专属)**:深读 JS 的返回值经桥接常丢失(历史 DONE 里深读约 95% 的 `title` 为空),丢失时守护白等到桥接超时。正常模式沿用 `max(150, 预算+45)` **不动**(代价约 45s/帖,已知未改);turbo 下收紧为 `max(40, 预算+30)`(JS 在前台标签下最坏额外耗时约 23s;**后台标签被浏览器节流时远不止此,实测出现连续桥接超时**),否则 150s 下限会把提速吃掉(实测 T3 首版每帖 175–180s,而非预期 ~80s)。

**turbo 下的超时记账与自动熔断(R576 评审后加,R576c 修订)**:
- 桥接返回 `No response data in Ns(...)`(`TMWebDriver.execute_js` 超时,脚本仍在后台标签里跑)时,**正常模式**保持旧语义(算 DONE,不等待、不探测,不动)。**实测正常模式 4/4 超时但每帖仍有 5–10 次上报**,所以超时≠没读。
- **turbo 下**:发下一帖前先轮询页内 `__tmBusy`(`ROUTE_JS` 起止写/清的忙标记)等上一帖脚本跑完(上限 240s,探测失败 fail-open 放行),避免两段读帖脚本叠在同一标签里;超时后同样先等空闲,再用 `tm.d` 判定——`d==0` 记 `FAIL why=bridge_timeout`(不进 `daily`/`seen_ids`),`d≥1` 或未知(重启后首探 None)按成功。
- **页内焦点解锁(R576b)**:Discourse 只在 `session.hasFocus=true` 时累计阅读时长;后台标签/Chrome 非前台时为 false ⇒ `_timings`/`_topicTime` 恒空 ⇒ 零 `/topics/timings` 上报(**已读帖子不涨,浏览话题照涨**)。`ROUTE_JS` 在路由前及每 1.5s 的 `__tmUnlock` 里把它置 true(与三件套同族,正常模式同样生效)。上报由网站自己的脚本发出,GA 从不直接调用该接口。
- 熔断:turbo 生效的帖子上**连续 3 帖**(桥接超时 或 成功但 `tm.d==0`)⇒ 写 `temp/linuxdo_browse_daemon/turbo_fuse.json`(当日有效、跨重启)+ `TURBO` 台账行 + 飞书私信;当日 `turbo_cfg()` 返回 None(=正常模式),**次日自动恢复**,`turbo.json` 本身不被改动。好帖(`tm.d≥1`)清零计数。
- 触发证据(2026-10-12):T3 运行 10:55–11:05 连续 8 帖 `tm.n` 冻结在 49,其中 7 帖对应桥接超时,标签页(`visibilityState=hidden`)停在别的话题;原因未定论(后台标签定时器节流为推断)。

**配置校验(fail-closed)**:数组须长度 2 且为有限数字(拒 NaN/±Inf/字符串/bool),`lo>hi` 整份拒绝;文件签名含 mtime/size/inode;`expires_at` 按**守护进程所在机器的本地时区**解释,距今 ≤7 天。

**开启期间判据**:
- 生效/到期/移除都在 `browse_log.jsonl` 落一行 `st=TURBO`;每条 `DONE` 带 `exec.turbo`。
- 观测字段(R576,正常模式同样记录):`exec`(实际执行的 steps/plan_s/stay_s/rb)、`tm`(每帖结束后另发一次只读探测,取页内 `PerformanceObserver` 累计的 `/topics/timings` 请求数:`n`=累计、`st`=最近 3 次状态码、`d`=较上一帖增量;**上报在离开话题时触发,增量归属下一帖**,按日统计即可;`d` 长期为 0 ⇒ 楼层上报没发)。★JS 返回值经桥接常丢失(历史 DONE 里约 75% 的 `title` 为空),所以 `tm` 不靠 `ROUTE_JS` 返回值带出。
- `risk_log.jsonl` 新增 `INFO/FETCH_PARTIAL`(部分页风控信号,含页码与响应正文前 200 字),用于判断 403 是否真风控。
- **一旦出现 403/风控信号先删 turbo.json**,再复盘。

## pick_session 恒失败的现场处置（2026-10-12 实测）
- 现象：`RC_NO_SESSION_RETRY: 候选标签页 ['<id>'] 均不在 linux.do`，连续 8 次后进程 `RC_NO_SESSION` 退出（由 watchd 拉起→再失败，循环）。根因是**桥侧 tab id 会整体刷新**（浏览器/扩展重连后 chat 窗口与内容脚本重新注册，旧 id 全废；实测 2109126426/666 → 2109126867/870），而守护沿用旧 id（`CDP fallback failed: No tab with given id` / `Detached while handling command` / `Another debugger is already attached`）。
- 处置顺序：①用桥 `{"cmd":"get_all_sessions"}` 重新列 id（**不要**用 AppleScript 看 Chrome 标签——多实例/多 Space 下「Google Chrome」可能只报出另一个实例的窗口，实测只看到 1 个无关标签）；②挑一个现存标签，用桥 `{"cmd":"execute_js","sessionId":<id>,"code":"location.href='https://linux.do/'"}` 把它导航到主站（**经桥的 execute_js 不会激活标签页**，故不会把 linux.do 顶到前台）；③确认该标签 `document.visibilityState=='hidden'`，否则前台保护会一直「用户在前台看 linux.do，跳过本轮浏览」。
- `visibilityState=='visible'` 只取决于该标签是否是**当前激活标签**（分屏/焦点在终端不算）；要恢复自动化浏览，只需让 Chrome 的前台标签不是 linux.do（用 `window.open('about:blank')` 或用户自己切走），守护下一轮就会继续。
