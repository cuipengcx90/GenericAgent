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

## 10. 空数据自愈：取材全 0 时退避重试 + 仍空跳过（2026-07-22）
- 现象：用户收到晨报正文「关键词命中 0 条、RSS 全量 0 条，暂无可用情报」。这**不是报错/崩溃**，是 digest 遇空数据时的诚实降级文案（不瞎编来源）。截图排查用 `vision_utils.ask_vision(img, prompt, backend='openai')`（claude 后端代理挂时报 400，换 openai 后端）。

## 11. LLM思考过程污染报告推送（2026-08-12）
- 现象：飞书私信收到的晨报/晚报**开头70+行是LLM的思考过程**（"用户要求我基于提供的TrendRadar MCP数据..."、"关键要求："、"先分析数据："、"让我浏览数据找出重要条目"），真正报告正文从`## 大模型`等分类标题才开始。
- **根因链条**（复盘确认）：
  1. **触发源**：2026-07-30 `mykey.py`设置`thinking_type: adaptive`（为其他需要推理的任务启用思考模式）
  2. **上游变更**：2026-08-09至10日之间，**Claude API服务端行为变更**：`adaptive`模式从"静默思考不输出"改为"自适应决定是否输出CoT到正文"，复杂任务开始把思考过程注入响应正文
  3. **代码无变化**：本地代码和配置从7月30日起未修改，但8月10日起突然出现污染 → 证明是API行为漂移
  4. **prompt失效**：虽明确要求"只输出报告正文，不要过程说明"（L330），但LLM把prompt要求全部回显 + 分析过程输出，旧代码只调用`_strip_llm_tags`剥内部标签未过滤CoT
- **根治过程**（`run_trendradar_digest.py` 两次修复）：
  - **第一次修复**（2026-08-12 10:46, commit 37134c8）：添加**双重定位过滤**逻辑：
    ① 找到标题行：`{icon} **IT情报{晨报|晚报}`
    ② 从标题后找第一个分类标题：正则`^##\s+[🤖🎨🛠🔒💎📱]`（限定常见分类emoji）
    ③ 重构：`标题行 + \n\n + 分类标题开始的内容`
    - **遗留bug**：正则限定emoji导致无法匹配"## 大模型"/"## 开源&工具"等无emoji标题，21:02重跑仍有74行污染
  - **第二次修复**（2026-08-12 21:03, commit 8520b93）：**放宽正则为`^##\s+`**匹配所有Markdown二级标题（无论是否含emoji），Python单元测试验证成功过滤74行思考过程
- 实测：2026-08-12晚报原始含74行污染 → 过滤后标题直接跳到"## 大模型"，无"用户要求我"/"2. 挑选8-12条"等残留。
- **教训**：
  1. **上游API行为会静默漂移**，防御式过滤比依赖prompt约束更可靠
  2. **正则锚点要宽容**：匹配结构特征（`## `）而非内容细节（特定emoji），避免内容变化导致过滤失效
  3. **代码+配置无变化但产出突变** = 大概率是外部依赖（API/模型）行为变更，需检查时间线而非反复review本地代码
- 校验：`grep -E "用户要求我|关键要求：|先分析数据|让我浏览数据|挑选.*条" output/trendradar_{morning,evening}_$(date +%Y-%m-%d).txt`应无匹配；飞书私信报告直接以`🌅 **IT情报晨报`或`🌙 **IT情报晚报`开头，下一行即为`## 大模型`等分类标题。
- 根因：脚本运行那一刻(约 10:00 晨报窗口)MCP `get_latest_rss`/`search_news` 返回 `success:True` 但 `data:[]`/`total:0`——上游 TrendRadar 容器正处**重启中或当日首轮爬虫未抓完**的空窗期。稍后实测即恢复(RSS 314 条含 Linux.do 30)。属**偶发时序**，非配置/代码故障。
- ★判空铁律★：`success:True` **不等于**有数据。健康的 TrendRadar RSS 应是几百条；`total:0` 且 queries 也全 0 = 空窗。判空要数 `rss.summary.total` + 每个 query 的 `result.total`，二者皆 0 才算全空(见 `_material_counts`)。
- 根治(已实现,`run_trendradar_digest.py`)：
  1. `fetch_material` 拆成外层重试 + `_fetch_material_once`：抓完调 `_material_counts` 判空，全 0 则退避重试(sleep 阶梯,默认 3 次),任一轮拿到数据立即返回。material 里记 `_all_empty_after_retries` 标记。
  2. `run()`：重试后仍全空 → **不 build/send 降级文案到正式渠道**(飞书群/钉钉群),返回 `{skipped:True, reason:'empty_after_retries'}` 且**不认领当日 state** → 后续定时轮次/手动 `--force` 仍能补发真正有内容的报。避免把「暂无情报」推给全员。
- 补发命令：`python3 sche_tasks/run_trendradar_digest.py --period morning --force`(先 `--dry-run --force` 验证有内容再正式发)。ga_mcp_client 正确接口是 `call_sync(server,tool,args)`(非 call_tool)。
- 教训：外部数据源「偶发返空」要在**取材层做退避重试**(时序抖动多半自愈),并在**推送层设空数据闸门**(仍空则跳过而非发降级文案)。二者配合:重试兜住偶发空窗,闸门兜住真·无数据。

## 11. tech_trends 单源无保护炸穿 run() + 死锁阻断补跑（2026-07-25）
- 现象：早上 tech_trends 情报整份没推(三通道全无)。查 `reflect_logs/scheduler_*.log` 见进程在 `fetch_hf` 阶段崩溃、无 RUN_DONE。
- 根因1(炸穿)：`run_tech_trends.py` 的 `fetch_hf`(HuggingFace)顶层 `requests.get` **无 try/except**,huggingface.co 超时(境外站点分流不通,同日 CoinGecko 也连超时)直接抛异常炸穿整个 `run()`,后面 GitHub/HN/Trendshift 的数据和推送全部没执行。对照 `fetch_trendshift` 本就有 try/except 返回 `[]` 优雅降级。
- 根因2(死锁阻断补跑)：崩溃进程留下 0 字节死锁 `/tmp/tech_trends_DATE.lock`(O_EXCL 按日锁),补跑时被「已有同日进程持锁」误拦。**注意锁正常完成后也不主动删,靠次日换文件名+状态文件双保险**;同日崩溃后手动补跑需先确认无进程(`ps aux|grep run_tech_trends`)再 `rm` 死锁。
- 根治(已改)：`fetch_github`/`fetch_hn`/`fetch_hf` 三个顶层请求全部包 try/except 失败 print 告警 + 返回 `[]`,任一源挂掉都能用其余源照常出报告推送。`py_compile` 通过。
- 补跑安全性判据：`_mark_sent` 在**报告生成后、推送前**才写状态文件(`output/.tech_trends_push_state.json`)。崩在 fetch(更早)→状态文件必无今日记录→清死锁后直接 `run()` 正常补跑即可(不需 force,幂等闸门反而是保护)。补跑命令:清死锁后 `python3 -c "import sys;sys.path.insert(0,'sche_tasks');from run_tech_trends import run;run()"`。
- 通用教训：凡对外部源逐个取材的脚本,**每个源的顶层网络请求都必须独立 try/except 降级返回空**,严禁任一源异常炸穿主流程导致整份产出丢失。这是比「加重试」更稳的根治——境外网络可能持续不通,降级能保证有几分数据出几分报告。

## 12. TrendRadar 晨报 LLM 流式中断 → 半截报告仍被推送（2026-07-25）
- 现象：用户截图 07-25 10:00 IT情报晨报,正文在「💻 芯片GPU」段后直接出现 `[!!! 流异常中断，未收到完整响应 !!!]`,后半截(开源工具/Linux.do 等)全缺,但仍照常推给三通道。**这不是"格式没按 SOP",是半成品报告被当正式报推送**。
- 根因：`run_trendradar_digest.py` 的 `build_report()` 把 `sess.ask()` 的流式返回直接拼成字符串返回,LLM 流中途断开时,llmcore 会在文本里插入 `[!!! 流异常中断，未收到完整响应 !!!]` 占位符——但 `build_report` **不校验**,`run()` 拿到就 `report_path.write_text()` 再 `send_once()`,半截内容照发。与第 10 章"空数据"不同:那是取材层空,这是**生成层残缺**,原代码两道闸门都没覆盖生成层。
- 根治(已实现)：新增 `class ReportQualityError` + `_validate_report_quality(period, report)`,检测三类问题→抛错:①流中断标记(`流异常中断`/`未收到完整响应`/`[!!!`);②缺必备结构(`IT情报{晨/晚}报`/当日 date/`📡 数据来源：TrendRadar`/`ARCHIVE_URL`);③正文 `<1800` 字(正常晨报约 1900+)。`build_report` 返回前调用校验;`run()` 里 `build_report` 最多试 **2 次**,仍失败则写 `trendradar_{period}_{date}.failed.txt` 并返回 `{skipped_bad_report:True, reason:'bad_report_quality'}`——**不写正式 .txt、不 send、不认领 state**,后续 `--force` 可补发。`py_compile` 通过。
- 补发验证：`--dry-run --force` 先看正文(注意 dry-run 也会覆盖写 .txt),grep 确认无中断标记 + 有尾部,再 `--force` 正式发。本次补发成功:`2026-07-25:morning` state `sent_at=11:55:37`,三通道 OK,终版 1843 字无残缺。
- 通用教训：**凡"LLM 流式生成→直接推送"的链路,推送前必须有质量闸门检出流中断占位符/结构残缺/异常过短并阻断推送+可重试**。生成层残缺和取材层空数据是两类独立故障,闸门要分别覆盖:取材空→跳过(第10章),生成残→重试后跳过(本章)。

## 13. 三定时任务改用 mykey 统一跨模型兜底 build_mixin_session（2026-07-28）
- 背景：tech_trends/trendradar_digest/hellogithub_monthly 原来各自手写单一模型绑定(如直连 claude-opus-4-8),该模型无权限时无兜底,整份推送失败。
- 根治：`llmcore.py` 新增 `build_mixin_session(cfg=None)`，读 `mykey.py:mixin_config`(含 `llm_nos`/`max_retries`/`base_delay`)构造 `MixinSession`，三脚本统一改调它 `.ask(msg)`，任一节点 4xx/5xx 失败自动滚动到下一节点。以后只需维护 `mykey.py:mixin_config.llm_nos` 顺序，不用逐脚本改代码。
- **坑点(踩过)**：`mixin_config.llm_nos` 存的是各 native 配置内部的**显示名**(如 `claude-relay`/`gpt-native`/`claude-router`/`gpt-6-astra`)，**不是** `mykey.mykeys` 的顶层配置键名(如 `native_claude_config0`)。若直接拿 `llm_nos` 里的字符串去 `resolve_client(name)`/字典查找会 100% 报 `Config not in mykey`。正确做法(仿 `agentmain.py` 的 `load_llm_sessions`)：先把 `mykeys` 里所有 native_*/oai_* 配置都跑一遍 `resolve_client` 拿到全量 client 列表，再按每个 client 的 `backend.name` 去匹配 `llm_nos` 里的名字筛选/排序。
- 验证手法：`py_compile` 全过 → 构造 `MixinSession` 实例断言节点数/名字 → 用 FakeBackend 模拟 504 验证 failover 滚动到下一节点 → 真实网络调用一次 `build_mixin_session().ask()` 确认端到端有效回复(非 mock)。

## 14. run_trendradar_digest.py 单次执行长达10+分钟 → 工具层超时误判 + 僵尸锁排查（2026-08-06 实战）
- 现象：定时任务直接同步跑 `python3 sche_tasks/run_trendradar_digest.py --period morning`，被上层工具 60s/300s 超时打断两次；但底层 python 子进程**不受工具超时影响**，独立跑到 ~13 分钟后（LLM 生成+质量重试链路）才结束，此时锁已被 `finally: _release_lock(lock)` 正常释放，但因中途工具层已"认为"失败，未等到真实产物就返回，未留下 `.txt`/`.failed.txt`。
- 排障法：锁文件 `output/.trendradar_locks/{period}_{date}.lock` 内容含 `pid=<PID> at=<ISO时间>`；若再次运行时因 `O_EXCL` 拿不到锁，先 `cat` 锁文件取 pid，`ps -p <pid>` 判活——进程已不存在即僵尸锁（前次被工具层打断但子进程已跑完/锁已释放），可安全 `rm` 后重跑；进程仍存在则说明前次仍在跑，不要并发起第二个实例，转为轮询等待。
- 正确做法：改用 `nohup python3 sche_tasks/run_trendradar_digest.py --period morning > /tmp/xxx.log 2>&1 &` 后台启动拿到 pid，用 `ps -p <pid>` 轮询（每 20-25s 一次）等待进程结束，再读日志判断真实 `sent`/`skipped`/`degraded_send` 结果，避免被工具 60/300s 超时误判为失败而重复触发。
- 代码实测更新（与本文件 §12 旧描述有出入，以此为准）：`run()` 里 `build_report` 实际 `max_attempts = 4`（非旧文档写的 2 次）。第 4 次仍失败时，若失败原因**仅是"内容过短"**（`'too short' in str(e)` 且不含 `missing required parts`/`stream interruption`）且拿到的 `last_report` 非空，会走**降级发送**分支：给正文加 `⚠️（今日素材较少，篇幅低于常规标准，仍完整发送）` 前缀后照常写 `.txt` + `send_once()`，返回 `degraded_send: True`（**非 skipped**，三通道仍会收到）；只有缺失必需结构片段或含流中断标记时才真正 `skipped_bad_report`（写 `.failed.txt`，不发送）。

## 15. ⚠️ CoT 污染复发（2026-09-21）：模型改用**粗体**分类标题，`^##\s+` 过滤整段失效
- 现象：2026-09-21 晨报飞书/钉钉三通道收到的正文里，**首行标题之后紧跟约 500 字 LLM 思考过程**（"Maybe add a "今日最值得关注" line right after title. Requirement 5 says…"），随后才是第二行真标题与正文；`output/trendradar_morning_2026-09-21.txt`（2844 字）sha256 `975a7dbc…` 与 state 记录一致 → **确认污染版真的发了出去**。
- 新根因（与 §11 的 2026-08-12 修复互补）：当日模型改用**粗体分类标题**（`**🎨 AI画图**`）而非 Markdown 二级标题（`## 🎨 AI画图`），于是 §11 的过滤逻辑 `^##\s+` 在整个文本里 **0 匹配**：`title_idx`（第二次出现 `🌅 **IT情报晨报`）>0 → 只剥掉标题**之前**的内容，标题**之后**的 CoT 一段不落全部留下。
- 格式漂移证据（output 目录实测）：09-10/09-11/09-20/09-21 的产物 `## ` 计数 = **0**（粗体标题），09-13~09-18 = 有 `## `（二级标题）。即**同一脚本、两种标题风格随日期漂移**，任何只锚定单一标题风格的过滤器都会间歇失效。
- 根治方向（尚未实施）：把「找第一个分类标题」的锚点从"单一风格正则"改为**多风格并集**——`^(##\s+|\*\*[^*]+\*\*\s*$)`（二级标题 或 独占一行的粗体标题），或更稳的判据"标题行之后、首个『正文特征行』（如 `▸` 条目 / `**分类**` 行）之前的内容一律丢弃"；同时把 `_validate_report_quality` 增加**污染探针**（检测 `Maybe ` / `Requirement` / `I'll ` / `用户要求我` / `让我` / `先分析` 等 CoT 特征词出现在正文前 600 字内即判不合格 → 触发重试），从"事后过滤"升级为"过滤 + 闸门双保险"。
- 验证方式（改后回归）：09-20 产物（单标题、无污染）应**零变化**；09-21 产物（双标题、含污染）应清空污染段；两份都要 `--dry-run` 目视 + grep 特征词。
- ✅ **已实施（2026-09-21 当天，`sche_tasks/run_trendradar_digest.py`；备份 `run_trendradar_digest.py.bak_20260921_1845`）**：
  1. 抽出 `_extract_report_body(text, icon, period_word)`：先找**最后一个**标题行（`title in line and icon in line and period_word in line`，带"去掉尾部空白后返回原文"兜底），再在标题行之后用**多风格锚点**找正文起点 `^(##\s+|\*\*[^*]+\*\*\s*$)`（标题后 6 行内扫描）；命中前的内容全部计入"已过滤"。
  2. `_validate_report_quality` 增加 **CoT 探针** `_cot_head_hits(report)`：正文头 900 字命中 `Let me ` / `I'll ` / `Now, output` / `Write it out` / `用户要求` / `让我先` 即抛 `ReportQualityError` → 触发 build_report 重试（最多 4 次）。
  3. **降级发送前同样过探针**：原"仅内容过短 → 降级发送 last_report"分支改为 `only_too_short and last_report.strip() and not _cot_head_hits(last_report)`，宁可不发也不把思考过程推出去。
- **验证（真实链路 `--period evening --dry-run`，18:46-18:48）**：4 次尝试中第 1 次被探针拦下（`['Let me ', "I'll "]`）、第 2 次"too short: 1637"，第 3 次成功且**真链路已过滤 15158 字符草稿+CoT**（"标题共出现 2 次"），最终产物 1892 字 / 62 行 / 单标题 / 无任何 CoT 特征词，`send.skipped=true (dry_run)`。夹具回归另证：污染样本过滤后与干净正文**逐字节相等**、干净产物**幂等零变化**。
- 观察：同一轮内模型仍会**先写草稿再重写正文**（标题出现 2-4 次，被过滤 8.6k~15k 字符），因此"取最后一个标题"这一策略是当前最稳的起点判据；探针是最后一道闸门。
- 通用教训：**过滤器的锚点不能依赖"模型这一次用了什么格式"**；上游模型（本例为 Claude 系 mixin 路由）的风格会随端侧/版本漂移，锚点要用结构并集 + 兜底闸门，并**定期统计历史产物里锚点命中数**（如 `grep -c '^## ' output/*.txt`）以提前发现漂移。

---

## §13 信源有效性审计（2026-09-22，TODO#2/R343）

- **工具**：`tools/trendradar_source_audit.py`（GA 仓，幂等只读；子命令 `scan` 扫描产出 / `report` 每源贡献表 / `check` 14 源 HTTP 实测 / `verdict [天数]` 零产出·极低产出·近期静默三清单 / `impact [天数]` 关键词通道损失量化）。报告 `temp/autonomous_reports/R343_*.md`，原始数据 `temp/trendradar_source_audit/`。
- **★平台检索通道整串失配坑（实测铁证）**：TrendRadar `mcp_server/tools/search_tools.py::_search_by_keyword_mode` 是**整串子串**匹配（`query.lower() in title.lower()`），而 GA 侧 `sche_tasks/run_trendradar_digest.py` 的 `KEYWORDS` 把 7~8 个词拼成一个长串去查 → **永远 0 命中**。证据：132 份 `*.material.json` 里两处查询全 `success=true` 但 `total=0`（2026-07-14~09-21 全 0），而同日 DB 实有 11 平台/1274 条标题、单关键词 `华为` 实测命中 7 条；拆词后 30 天可捞回 1396 条。**排查时看到 material.json「success 但 total=0」不要当成库空**（消息里已写"可用数据: 2025-12-21 至今"）。
- **采纳口径**：报告正文里的来源标注（`（VEX）`/`（Solidot）`等）才算采纳；DB 里 title 有记录 ≠ 采纳（平台 11 源就是这样"天天抓、零采纳"）。RSS 侧 14 源里 hacker-news 占采纳 68%（985/1451），其余多为长尾或零产出。
- **死源判定必须有 HTTP 实测**：本次 `ai-weekly`（AIbase 快讯）HTTP 404，另测 4 个候选地址全 404（该站已废 RSS）；配置里给它加 `enabled: false` 即与既有 `yahoo-finance` 同法停用。config 只读挂载，改动只出建议（见 `temp/pending_decisions/DECISIONS_R343.md`）。
- **坑**：`output/news/YYYY-MM-DD.db` 的 `platforms` 表能给平台 id→中文名映射，但报告里写的名字混用中英（`财联社` vs `财联社热门`）→ 比对时要走别名表；读 DB 用 `sqlite3 file:...?mode=ro`（容器写的库，直接 connect 可能锁）。



---

## §14 拆词修复「效果 A/B 离线复算」（2026-09-22，TODO#13/R352）

- **工具**：`tools/trendradar_keyword_ab.py`（GA 仓，只读；`--days/--end/--start/--json/--pair-mode/--boundary`）。产物 `temp/trendradar_keyword_ab/ab_replay.{json,csv,md}`；报告 `memory/autonomous_reports/R352_*.md`。
- **判定**：**批** R343 修法A（拆词后并集检索），但**必须与 `limit 30→≥80` 同批落地**；ASCII 关键词加词边界；修法B（`search_mode='fuzzy'`）**不批**（th=0.3 仅 58 条/29 天，是拆词的 1/22；th=0.6=0）。不批则平台通道继续 0 条。
- **数字（29 个已封盘日 08-24~09-21）**：A 整串=0；B 逐词 pair 口径 **1384**（与 R343 同区间 1384 完全对齐）；按标题去重 1283（44.2/天，单日最高 73，零命中 0 天）；词边界口径 1174 ⇒ 子串误召回 109（8.5%，如 `AI` 命中 `AIM-424`）；与当日 RSS 供给**仅重合 1 条**（净新增 1253=99.9%）；成本 ≈400 token/天（泛词命中占 68.4%，平均标题 25.5 字）。
- **★口径坑（复算必守）**：①R343 的 `token_union` 口径＝**按 `(query, title, platform)` 计数**（同题跨平台算多条），按标题去重会少 7.3%，对账时必须 `--pair-mode` 才逐日逐词全等。②**当日 DB 仍在写入**（TrendRadar 平台持续抓取），拿今天做复算会与历史快照差出几十条 ⇒ 比历史数字必须**剔除当日**。
- **风险面**：`limit=30` 实测 17 次(query·天)触顶、按现状会丢 180 条(13.2%)；生产 KEYWORDS/limit 属生产配置，改动需批准（本轮未动）。
