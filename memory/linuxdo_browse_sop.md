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
后台运行器 = `tools/linuxdo_browse_daemon.py`（零 LLM token；HTTP 打 TMWebDriver 桥 `127.0.0.1:18766/link` 复用已登录 tab；`--selftest` 离线自测 6/6）：
- `watch` 长驻判停循环：每批后重读 TL3 等级，**按页面 ✓/○ 标记**判 pending==0 即退出（禁数值机械比较——上限类指标会误报）；`browse --batch N` 手动一批；`check`/`status` 只读
- 防检测（后台版在单任务版之上再拉稀）：帖间 90–240s（25% 概率 240–420s）+ 深翻步数随机 + 每批随机 2–4 次只逛列表页不进帖 + `--daily-cap` 每日话题上限 + 单段 90min 后休息 + 静默窗 01:00–07:30；只读铁律不变（零发帖/回复/点赞/关注）
- 收尾统计同款以 `temp/linuxdo_browse_daemon/browse_log.jsonl` 为准；等级检查记录落 `level_log.jsonl`
