# TrendRadar MCP SOP（晚报/热搜/RSS任务）

## 关键坑点（多次重试总结）

### 1. MCP协议：Streamable HTTP（非SSE！）完整握手
```python
# 正确方式：3步握手
# Step1: initialize → 获取session_id
r = requests.post(BASE, json=init_payload, headers={"Accept":"application/json, text/event-stream"})
session_id = r.headers.get("Mcp-Session-Id")
# Step2: 必须发 notifications/initialized，否则后续所有工具调用报 Invalid parameters！
requests.post(BASE, json={"jsonrpc":"2.0","method":"notifications/initialized"},
    headers={**H, "Mcp-Session-Id": session_id}, timeout=5)
# Step3: 工具调用，读r.content并regex提取 data: 行
import re; matches = re.findall(r'data: (.+)', r.content.decode())
```

### 2. search_news 含 date_range 参数必返回0条（坑！）
- 勿用 `date_range={"start":"2026-04-23","end":"2026-04-23"}` → 0条
- 改用 `get_latest_news(limit=100)` 后手动按 timestamp 字段过滤今日

### 3. 数据结构差异
| 工具 | 日期字段 | item关键字段 |
|------|---------|------------|
| get_latest_news | timestamp ("2026-04-23 21:xx:xx") | title, platform_name, url, rank |
| get_latest_rss | published_at ("2026-04-23Txx:xx:xx") | title, feed_name, url, summary |
| search_news | date ("2026-04-23") | title, platform_name, similarity_score |
- response结构：`parsed["data"]` 是列表（不是 `parsed["results"]`）

### 4. 有效工具列表
`get_latest_news` / `get_latest_rss` / `search_rss(keyword,days,limit)` / `get_trending_topics(top_n,mode,extract_mode)` / `send_notification` / `resolve_date_range` / `list_available_dates`
- ⚠️ 坑（2026-07-12 晚报实战）：**不存在 `search_articles` 工具**（任务 prompt 里写的是它，实际会返回 `Unknown tool: search_articles`）。关键词检索用 `search_rss`，最新新闻用 `get_latest_news(limit=50)` 后手动按 title 关键词/timestamp 过滤。拿不准先 `list_tools_sync('trendradar')` 列真实工具名，别照抄 prompt。

### 5. 通知发送
- 飞书群：`from notify_feishu_group import notify_group; notify_group(text)`（需sys.path加reflect_scripts）
- 飞书私信：`from reflect_common import notify_feishu; notify_feishu(text)`
- 钉钉群：`from notify_dingtalk_group import notify_group; notify_group(text)`（模块名是 notify_dingtalk_group，非 ding_notify；sender 同为 notify_group）
- 三通道各自独立 try/except，互不阻塞

### 6. 502 故障恢复（2026-07-08 实战）
- 现象：`/mcp` POST 持续 502 Bad Gateway，根路径 404（=nginx代理活但后端MCP进程宕）
- 根因：MCP 是 docker 容器 `trendradar-mcp`（127.0.0.1:3333），后端进程崩溃
- 恢复：`docker restart trendradar-mcp`（单容器、可逆），等 startup complete 后重试
- 坑：容器恢复后 `call_sync` async 客户端仍可能 502/asyncgen 报错 → 直接用 requests 走 MCP 协议（见第1节握手），绕过坏客户端
- 坑（2026-07-10 实战）：502 多为**间歇抖动**而非后端全宕。此时**每次 call 都新建 session 反而加剧 churn 触发更多 502**。正解：**单 session 复用**（一次 initialize + notifications/initialized，之后同一 session 内顺序调用所有工具），失败只重发该 call、不重建 session；循环直到拿到 200-with-result 即止。`ga_mcp_client.call_sync` 每调一次即新建 session，故本场景优先用 requests 手写单 session。
- **已固化（2026-07-14 实战）**：`run_trendradar_digest.py` 的 `fetch_material` 已**永久改用 raw requests 单 session**（内部助手 `_mcp_open_session`/`_mcp_call`/`_json_text`/`_parse_json_maybe`），不再走 `ga_mcp_client.call_sync/list_tools_sync`。症状复现：curl 端到端可靠但 Python async 客户端 `session.initialize()` 在坏窗口永久挂起（无超时）。此后取材稳定，勿回退到 async 客户端。
- **字段纠正**：`search_news` 的结果在 `results`/`total` 字段（不是 `data`，`data` 为 None 属正常）；`get_latest_rss` 才是 `data`（实测 80 条）。判空要看对应字段。

### 7. SSE 解析与编码坑
- 后端把大 JSON 拆成多个 `data:` 行片段，逐行 json.loads 会失败（char 6x报错）→ 应拼接所有 data 片段后再 parse
- 中文乱码：requests 默认按 latin1 解码 → 必须 `r.encoding='utf-8'` 再取 `r.text`

### 8. 晨/晚报重复推送根治（2026-07-14 实战）
> ⚠️ **纠偏（2026-07-15 定案）**：8/8.1/8.2 三节修的幂等/崩溃窗口都**不是"私信天天两条"的真凶**，那些是排查时被表层证据带偏的方向。真正让私信每天出现两条一模一样的根因见 **§8.3**——通知通道函数 `notify_group` 内部又补发了一次私信。遇到"私信同会话双发且天天复发"先看 §8.3。
- 现象：飞书私信收到两条一模一样的「IT情报晚报/晨报」。查 `reflect_logs/scheduler_YYYY-MM-DD.log` 确认**当天只触发一次**定时任务，不是调度双触发。
- 根因：旧 `sche_tasks/trendradar_{morning,evening}.json` 的 prompt 是**自由探索式多步任务**，让 LLM 自己取材+推送。执行中 LLM 先调 TrendRadar MCP `send_notification`（MCP 内置飞书通道），随后又按 prompt 步骤用 GA 原生 `notify_feishu` 再发一次 → 同一份报告推两遍。旧 prompt 还写了不存在的工具名 `search_articles`（有效工具是 `search_news`/`get_latest_rss`）。
- 根治：改为**确定性脚本单入口** `sche_tasks/run_trendradar_digest.py --period morning|evening`。任务 JSON 的 prompt 只允许执行这一条命令，明确禁止再调 MCP `send_notification` 或手工通知。
- 脚本内部保证幂等：`output/.trendradar_digest_state.json` 按 **key=`{YYYY-MM-DD}:{period}`** 记录 sent；命中则返回 `skipped/already_sent_today`。另有 `output/.trendradar_locks/{period}_{date}.lock`（O_EXCL）防并发。MCP 只用于取材，GA 原生三模块只用于推送，三通道各自独立 try/except。
- ⚠️ 坑：手工补种防重状态时 key 顺序必须是 `日期:period`（与 `send_once` 的 `f'{date_str}:{period}'` 一致），写成 `period:日期` 不会命中。改后务必用 `send_once()` 实测返回 `skipped` 验证。
- 通知通道模块：飞书私信 `reflect_scripts.reflect_common.notify_feishu`；飞书群 `reflect_scripts.notify_feishu_group.notify_group`；钉钉群 `reflect_scripts.notify_dingtalk_group.notify_group`。

### 8.1 同源第二案例：github_trending「每日技术开源趋势报告」重复推送（2026-07-14）
- 现象：同一份「每日技术开源趋势报告」飞书私信收到两条。但 `gh_trend.log` 只记 1 轮推送、`sche_tasks/done/` 当天只 1 个 github_trending done，日志查不到双触发。
- 根因（与 8 同病不同表现）：`sche_tasks/run_tech_trends.py` 的 `run()` **零幂等**——进来就无条件三通道推送。旧 `github_trending.json` prompt 是 inline `run()`，执行超 60s 被工具超时打断，调度会话的 LLM 为绕开超时**改用 nohup 重新跑了一次 run()** → 推第二遍；nohup 覆盖式日志只留下第二轮，所以日志看起来只有一次。
- 教训：**凡"必须恰好一次"的副作用，幂等必须做进被调函数自身，不能靠 prompt 自觉。** 只要函数无幂等，任何"超时→换法重试"都会重发。
- 根治：①`run(force=False)` 顶部加幂等闸门——读 `output/.tech_trends_push_state.json`，命中当天日期即 `return {"status":"skipped"}`，不拉取不推送；+ `/tmp/tech_trends_{date}.lock`（O_EXCL）防并发。②报告生成后、推送前即 `_mark_sent(date_str)` 落状态，从此刻起当天任何重复调用都被拦。③`force=True` 为强推逃生口。④JSON prompt 改为一次性 nohup 启动 + 轮询 RUN_DONE，明确"即使超时也绝不重跑，run 已自带按日幂等"。
- ⚠️ 幂等键这里是纯日期 `YYYY-MM-DD`（单报告无 period），与 8 的 `日期:period` 区别开。手工补种/校验：`_mark_sent('YYYY-MM-DD')` 后调 `run()` 应返回 `skipped/already_sent_today`。

### 8.2 同源第三案例：send_once「先发后记」崩溃窗口双发（2026-07-14 晚报）
- 现象：飞书私信收到两条一模一样「🌙 IT情报晚报 · 2026-07-14」，两条同分钟（~18:36）。但 `output/.trendradar_digest_state.json` 晚报**只 1 条** `sent_at=18:36:59`（单 sha256），scheduler 日志当天晚报仅 attempt=1，进程也只有 1 个。所有"表层"证据都指向单发，唯独 DM 是两条——查了很久对不上。
- 根因（8/8.1 已修幂等闸门后残留的**时序漏洞**）：`send_once` 顺序是「读 state 判重 → 发三通道 → **最后**才 `_save_state`」。中间整段是网络发送。若某次运行**已发出 DM、但在 `_save_state` 之前进程崩溃**（本环境高发 `ChunkedEncodingError` 流中断），则：state 没落盘 + `run()` 的 finally 释放了锁 → reflect 判失败重试 → 第二次运行读到"今天无记录"→**再发一遍** → 这次跑完才写 state。结果 DM 两条、state 只留成功那次。O_EXCL 锁只能防"同时并发"，防不住"崩溃释放锁后的串行重试"。
- **为何每日复发（2026-07-15 翻执行日志坐实）**：崩溃窗口只解释"单次矛盾"，解释不了"天天两条"。真因在执行层：定时任务由 reflect-agent 执行（`agentmain --reflect scheduler.py`）。查 `temp/reflect_logs/scheduler_2026-07-14.log` 18:30 段，**一次调度内 agent 把 `run_trendradar_digest.py --period evening` 连跑了 6 遍**：前几遍卡在取材（MCP 502 / SSE 握手超时 / `ga_mcp_client.session.initialize()` 挂起），第 6 遍 agent 现场把取材改 raw requests 才成功@18:36:59。取材层挂起在本环境是**常态而非偶发**，每天都触发这套"挂起→重试"风暴；其中某遍已过取材、发出 DM 后被中断/成孤儿进程延迟补发，与最终成功那遍在同分钟窗叠成双发。**排查铁律：重复推送每日复发时，必查调度器日志里该命令实际被跑几遍，而非只看任务 attempt 数。**
- 教训：**幂等标记必须"先于副作用"落盘（claim-before-send），不能"后于副作用"（commit-after-send）。** 只要"发送成功但记录失败"存在窗口，崩溃+重试就会重发。判重读得再早也没用——决定重发与否的是**记录写得多早**。取材层每日抖动是重试风暴的源头，故 §6 的 raw requests 单 session 改造是治本第一道，本节 claim-before-send 是兜底第二道，两道缺一不可。
- 根治：`send_once` 改为——判重通过后**立即**写 `state[key]={sent:True, claimed_at:...}` 认领标记并 `_save_state`，**然后**才发三通道；发完重载 state 更新 `sent_at`+channels 明细。仅当**三通道全部失败**时 `state.pop(key)` 回滚认领，让后续重试能真正补发（逃生口）。这样即便发送途中崩溃，重试第二次进来必读到 `prior.sent=True` → `skipped/already_sent_today`，物理上不可能重发。
- 校验：构造 `state` 含当日 `{日期}:evening` 的 `sent:True`，调 `send_once(...,force=False)` 须返回 `skipped/already_sent_today`（已实测 PASS）。
- 通用护栏（三案例合并结论）：**"恰好一次"的副作用 = 幂等做进被调函数自身 + 认领标记先于副作用落盘 + 全失败才回滚**。缺任何一环，"超时/崩溃→重试"都会重发。

### 8.3 ★真凶★ 通知通道函数内部冗余补发私信（2026-07-15 定案）
- 现象：飞书**私信同一会话**里，每份报告（晨报/晚报/趋势报都中）稳定出现**两条一模一样**，天天如此。8/8.1/8.2 修完幂等后依旧复发。
- 为何前几轮全查偏：所有"脚本层"证据都自洽指向单发——`digest`/`tech_trends` 的状态文件当天各只 1 条 `sent`、scheduler attempt=1、进程 1 个、`notify_feishu` 无重试。于是一路往幂等/崩溃窗口/双调度上猜，全是死路。**盲区**：第二条不经过 digest 的状态记账，所以状态文件永远只显示 1 次——矛盾的出口不在"发了几次脚本"，而在"一次脚本调了几次私信 API"。
- 根因（一次运行内私信被调两次）：`reflect_scripts/notify_feishu_group.py` 的 `notify_group()` 发完群 webhook 后，末尾 `try: from ...reflect_common import notify_feishu; notify_feishu(text)` **又把同一份文本发到了用户私信**（早期当兜底/调试加的，一直没删）。而每个调用方（`run_trendradar_digest.py` L293、`run_tech_trends.py` L237、hellogithub、`ga_mcp_server`）**都已经单独调过 `notify_feishu` 发私信**，于是每份报告：`notify_feishu(report)` 发私信第 1 条 + `notify_feishu_group(report)` 内部再发私信第 2 条 → 私信恒定两条。跟幂等、崩溃窗口、双调度**全都无关**。
- 根治：删掉 `notify_group()` 内那段对 `notify_feishu` 的调用（L30-38），本函数**只负责群 webhook**。私信一律由调用方自己显式调 `notify_feishu`。已提交 `050bba1`。
- 校验：`grep -rn "notify_feishu" reflect_scripts/notify_feishu_group.py` 应无匹配；下一次晨/晚报私信只应出现一条。
- ★排查铁律（血泪）：**"稳定每天两条"几乎一定是代码结构性双发（一次运行内调两次），不是概率性的崩溃/重试**。崩溃窗口、竞态只会偶发，不会天天精准两条。遇到重复推送先分类：偶发→查时序/幂等；每天必现→查一次调用链里同一副作用被触发几次（grep 通知函数的所有调用方 + 每个通知函数内部是否又转调了别的通知函数）。
- ★教训：通知通道函数应**单一职责**——发群的只发群，发私信的只发私信，绝不在一个通道函数里"顺手"再发另一个通道，否则调用方叠加就双发。


## 9. 新增数据源：Linux.do 社区热帖接入晨/晚报（2026-07-17 初版；2026-07-19 纠偏为原生内置）
- 需求：Linux.do 每日热帖并入 IT情报晨报/晚报。
- ★方案纠偏（2026-07-19）：初版在 digest(`run_trendradar_digest.py`)里自写 `fetch_linuxdo` 抓取——用户明确要求「内置到 TrendRadar，不要单独自己实现」。已回退，改由 TrendRadar 的 RSS 引擎原生抓取，digest 只从 `get_latest_rss` 结果里消费。教训：数据源接入优先改引擎(config+fetcher)，不要在下游脚本另起炉灶。
- 关键坑1 CF/JA3：linux.do 全站套 Cloudflare，普通 `requests`（哪怕带 Chrome UA）被按 **JA3/TLS 指纹**拦成 `Just a moment` 挑战页——非 UA 问题。须 `curl_cffi` 的 `impersonate="chrome"`。RSS 端点 `https://linux.do/top.rss?period=daily`(热帖) / `latest.rss`(最新)，标准 Discourse RSS XML。
- 引擎改造（TrendRadar repo，commit `4da7faa`）：
  ① `RSSFeedConfig` 新增 `impersonate` 字段(`fetcher.py`)；
  ② `_fetch_url` 里 impersonate 非空→走 `curl_cffi.requests.get(url, impersonate=...)`，内置 **429 指数退避**；为空则原生 requests 不变；
  ③ 两处 loader（`fetcher.py` 与 `__main__.py` 约 L1130）都 `feed_config.get("impersonate")`；
  ④ `config/config.yaml` 的 `rss.feeds` 加 `id: linux-do`(url=top.rss?period=daily, `impersonate: "chrome"`)。★config.yaml 含真实 api_key/飞书 webhook，**未入库**，仅本地运行态，提交时务必排除；
  ⑤ 依赖 `curl-cffi>=0.15.0`，装进 TrendRadar 的 uv `.venv`（uv 在 `~/.local/bin/uv`，`.venv/bin` 下无 pip，用 `uv pip install`）。
- 关键坑2 读存量非实时：`get_latest_rss`→data_service 读的是**已存库**数据(`read_all_titles_for_rss`)，不实时抓。故 linux-do 出现在 digest 的前提是 TrendRadar 定时抓取(`NewsAnalyzer._crawl_rss_data()`，只抓+存库不推送)已跑过并把 linux-do 写库。手动补种：构造 `NewsAnalyzer` 调 `_crawl_rss_data()`。（注意 MCP server 有 DataService 缓存，验证时新建实例避开缓存。）
- digest 回退（GA repo，commit `1c098ff`）：删 `fetch_linuxdo`/`LINUXDO_*`/`material['linuxdo']`；prompt「💬 Linux.do 社区热议」分类改从 `material['rss']` 里 `feed_name=「Linux.do 社区热帖」`的条目取(用 url 字段)，无则省略不虚构。
- 验证：`.venv` python 端到端 `_crawl_rss_data()` 抓到 30 条 → `get_latest_rss` 返回 28 条 linux-do。PASS。
- 提交：引擎 `4da7faa`(TrendRadar) + digest 回退 `1c098ff`(GA)。初版自写方案曾提交 `3f3822f`，已被 `1c098ff` 回退。
- ★2026-07 真凶复发(晨报又没 linux-do)★：运行态已从本地 .venv 迁到 **Docker**。稳跑的容器 `trendradar`/`trendradar-mcp` 用的是 **pull 下来的旧预构建镜像 `wantcat/trendradar:latest`**（`docker-compose.yml` 是 `image:` 引用，非 build）。旧镜像里 `trendradar/crawler/rss/fetcher.py` 是**纯 requests 实现**、无 impersonate 分支、镜像内也没装 curl_cffi。→ 本地源码早已修好（impersonate+curl_cffi），但**没进运行镜像**，config.yaml 虽被挂载(ro)读到 linux-do 却被忽略，linux.do 被 CF 403「Just a moment」静默丢弃。铁律：**改了本地源码≠生效**，先确认运行态是本地 code(挂载/venv) 还是烤进镜像；`docker inspect <c> --format '{{.Config.Image}}'` + `.Mounts` 判断。
- 恒久修复：`cd TrendRadar/docker && docker compose -f docker-compose-build.yml build && docker compose -f docker-compose-build.yml up -d`（Dockerfile 走 `uv sync --locked`，pyproject/uv.lock 已含 curl-cffi>=0.15.0，自动装齐）。构建产出本地镜像 `docker-trendradar`/`docker-trendradar-mcp` 替换 wantcat。
- 验证(端到端,容器内)：`docker exec trendradar python -c "from curl_cffi import requests as c; print(c.get('https://linux.do/top.rss?period=daily',impersonate='chrome').status_code)"`→200；`docker exec trendradar python -m trendradar` 日志见 `[RSS] Linux.do 社区热帖: 获取 30 条`。PASS(2026-07-20)。
- ★2026-07-21 专栏内容偏少/偏闲聊根因★：linux-do 已进库、`get_latest_rss` 也返回，但用户反馈专栏条目少且混情感/生活贴。实测 `get_latest_rss(limit=80)` 返回满 80 条却是**全源混合按时间排序**，V2EX 发帖极频繁独占 59 条、Linux.do 仅挤到 6 条候选→喂给 LLM 的技术向素材太薄。★正确解★：**不要**再在 digest 自写 top.rss 抓取(重蹈 §9 被否的老路，我一度提交 `8459eea` 又回退)；只需把 `get_latest_rss` 的 `limit` 从 80 调大到 **400**，Linux.do 候选即从 6 → **56 条**(仍全出自 TrendRadar 引擎)，再按 feed_name 过滤 + prompt 里要求「挑技术/AI/开源/工具/羊毛向 6-8 条，不足不用闲聊贴凑数」。提交 `bae47a1`(GA)。教训：混合池「某源条目少」先查是不是**高频源霸占 limit 配额**，优先调 limit 而非另起抓取。
- 坑：mac 宿主无 `timeout` 命令(仅容器内 linux 有)，别在宿主 bash 包 timeout；docker build 数分钟→后台 nohup 跑避 60s 工具超时。
