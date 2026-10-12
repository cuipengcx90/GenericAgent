# AI 课程资源台 SOP（course_pool）

> 定位：跨源采集「AI 课程/教程」资源，统一入池 → 看板 → 后续关联网盘资源。
> 单一入口：`python3 tools/course_pool.py <子命令>`（**位置参数**，非 `--flag`；每个子命令 `--help` 才是权威）。
> 本 SOP 由 R551 交付时沉淀；配套报告 `memory/autonomous_reports/R551_AI课程资源台P1跨源采集骨架.md`。

## 一、数据分层（耐久性铁律）

| 层 | 路径 | 性质 | 可否再生 |
|---|---|---|---|
| 派生 | `temp/course_pool/pool.json` | 抓取结果，每天刷新 | ✅ 重抓即得（gitignored） |
| 用户 | `memory/course_pool/user_state.json` | 星级/状态/备注/已找到资源 | ❌ 不可再生（memory/** 被忽略，**非 .md 需 `git add -f`** 才进克隆） |
| 配置 | `tools/course_sources.json` | 源注册表（已跟踪） | ✅ 但改前先读 |
| 展示 | `archive/courses/index.html` | 看板（archive/ 被 .gitignore:199 忽略） | ✅ `render` 重生成 |

★禁把用户数据写进 `temp/`（会被清理/不入库）；禁把抓取产物写进 `memory/`（污染耐久层）。

## 二、常用命令

```bash
python3 tools/course_pool.py selftest        # 自测（84/84，含 T84「不写生产库」负控）
python3 tools/course_pool.py db              # 统一落库：全量重建 + JSON 摘要（幂等）
python3 tools/course_pool.py stats           # 池统计 JSON
python3 tools/course_pool.py list --kw AI --limit 20
python3 tools/course_pool.py fetch --source quangneng --dry-run
python3 tools/course_pool.py fetch --source imooc --max-pages 3
python3 tools/course_pool.py render --out archive/courses/index.html
```

## 三、加一个采集站（可扩展核心）

改 `tools/course_sources.json`（或用 `add-source` 子命令），条目字段：
`id / name / url / parser / detail / channel / kind / disabled / note`。

- `parser`：该站列表页抽课方式（站点自定义）；
- `detail`：是否进详情页二次抓；
- `channel`：`browser → jina → http` 通道优先级；
- `disabled: true`：临时停用不删配置；`fetch --include-disabled` 才会跑它。

抓取入口统一：`fetch --source <id>`（`--dry-run` 只解析不入池）。**禁在主流程为某个站写 if-else**——新站一律走注册表。

## 四、与搜索面（海搜/盘搜/全能）的关系

P1 把「全能 quangneng 课程列表」接进池作为 search 侧一个源；池按 course_id/标题归一去重，`dup_groups` 暴露跨源重复。海搜/盘搜仍走各自脚本（`birdie_monitor/`，配额与轮转口径见 haisou_monitor_sop）；后续新搜索源同样以注册表条目接入，**不另写散脚本**。

## 五、定时接线（11:15 档）

`sche_tasks/haisou_daily_monitor.json`：GA_ROOT 锚定 → registry fetch → 配额预检(R549 口径,HAISOU_BUDGET_SHARE=0.35) → 命中 upsert → stats+done 回执。校验 `python3 tools/sche_tasks_lint.py --only haisou_daily_monitor` 必须 PASS。**定时只采集入池，不自动下载/下单**。

## 六、坑（实测）

1. ★**本机回环端口被抢占**：网页抓包代理类遗留件绑 127.0.0.1 同端口会把「站点被劫持/被封」伪装成本地故障（R551 案例：pid 绑 8899 → 回环返回 WAF 页、LAN 正常）。排查=「进程命令行 → 谁绑端口 → 是否含 127.0.0.1」；处置前先落审计文件再停精确 PID，恢复命令写进审计。
2. `archive/courses/index.html` 与池 JSON 不进 git：耐久性=靠重抓重渲染，属可再生数据；若要留档须用户批准另立路径。
3. docstring 与 argparse 会漂移（R551 实测：docstring 写了不存在的 proof 子命令）⇒ 修文档必须逐子命令取 `--help` 对账，禁凭印象补。
4. 本机无 `lsof`/`netstat`：端口归属用 Python（socket 枚举/psutil）+ 进程命令行交叉定位。

## 七、词表产物与入库口径

- `birdie_monitor/pool_keywords.json`（`words` 默认产出，可用 `COURSE_WORDS_OUT` 改）＝**逐课程搜索词状态**（found / search_count / origin=pool:<src>:<id>），供海搜沿用；`--merge-into birdie_monitor/quangneng_keywords.json` 时同时并入海搜动态词表。
- 两个词表都**入库**（`quangneng_keywords.json` 既有、`pool_keywords.json` 作初始种子）：池 DB 与看板在 gitignored 路径，词表是 clone 后唯一可直接沿用的状态；日更重跑会更新它们并产生 diff，属预期。
- `--no-merge` 只写 `pool_keywords.json`，用于负控/调试。

## 八、网盘资源自动匹配（`match`，P2）

把「搜索命中」（海搜 + 盘搜）按标题相似度匹配到池内课程，补 `resources[]`，**默认 dry-run**：

`python3 tools/course_pool.py match [--min-score 0.62] [--max-per-course N] [--limit N] [--hits 文件] [--no-pan] [--include-found] [--apply]`

- 命中源（只读，从不写盘）：海搜 `birdie_monitor/haisou_seen_items.json`（`COURSE_HAISOU_SEEN` 可覆盖；真值 1427 条）＋盘搜 `temp/pan_search/raw_*.json`（`COURSE_PAN_RAW_GLOB`；含 quark 11448 等，合计 13412 条）。
- 相似度：标题归一（`norm_title`：去噪声词/平台前缀/符号，ASCII 词边界）后 `title_score`（token 覆盖 + 序列比）≥ `min_score` 才收；同课程同 URL 去重。
- 入库口径：**只补空缺**（课程已有 resources 默认跳过）；`--include-found` 才走 (cid,url) 去重路径。
- 坏库 must-fail：命中库不可读/结构非 dict ⇒ 抛错，绝不静默当空（否则得「今天无命中」假结论）。

### 8.1 候选面板（干跑结果上看板，只预览不落库）

- 快照：`match --json-out <路径>` 写**全量**候选（`schema=course_match_candidates/v1`，键 `all_matches` 为全量、`matches` 仅前 `--limit` 条）；默认落点 `temp/course_pool/match_candidates.json`（`COURSE_CAND_OUT` 可改）。
- 上板：`render` 默认读该默认快照，存在即渲染「网盘命中候选」节（chip：`共 N` / `快照 <ts>` / `min_score`，正文行数 = `all_matches` 条数，最多 `CAND_BOARD_LIMIT=200` 条）。
- 只预览：面板自带「**尚未挂载**」提示，写 `resources[]` 须显式 `match --apply`（阈值 `--min-score` 待人工确认）。
- fail-loud 边界：默认路径缺失/坏文件 ⇒ 静默降级（不上板 + 提示）；**显式** `--candidates <路径>` 缺失/坏 ⇒ 直接抛错，绝不把坏路径当「没有候选」。

### 8.2 看板访问（8899）

- 8899 的服务根目录是 `archive/`（output-archive 站的 `http.server --directory archive`）⇒ 课程看板的正确 URL 是 **`http://127.0.0.1:8899/courses/index.html`**；访问裸 `/` 会拿到另一个站（约 6.9MB、无 `网盘命中候选`），据此判「面板没上」= **假阴性**。
- 本机 curl 须 `--noproxy "*"`（Clash 接管会返 400/挑战页）；LAN 访问用当前真实 IP（`ifconfig` 取，IP 会变，旧 IP 恒 400）。
- 判「服务在跑」仍以 pid + 启动命令行核对（历史上有闲置脚本劫持回环 8899 的先例）。

### 8.3 阈值标定实测（2026-10-06 快照）

- 库现状：`courses 215 / resources 83`，**有资源课程仅 44 门**（其余 171 门为空）⇒ 默认 `match` 干跑（含盘搜 13412 命中）**候选 = 0**，即「能挂的都挂了」，不是坏了。
- 171 门空课程 × 1822 条去重命中标题的最佳分分布：0.1 档 1 / 0.2 档 27 / 0.3 档 30 / 0.4 档 44 / 0.5 档 41 / 0.6 档 28；**≥0.62 为 0 门**，0.55–0.62 有 28 门（最高 0.615）。
- 结论：`MATCH_MIN_SCORE=0.62` 卡在边缘且**当前不动**——0.6 档多为泛类目撞词（共享 AI / 实战 等词）=假匹配。
- 想多挂资源应扩**语料 / 搜索面**（更多词、更多源、更长窗口），而非降阈值。
- `--include-found` 只重扫已挂课程且 (cid,url) 去重 ⇒ 不产生重复；全量 215×13412≈2.9M 次打分 >60s，须 `--limit` 或后台跑。

## 九、采集面台账与看板「采集面」面板（v2）

### 9.1 台账（谁在什么时候采到了什么）

- 每次**真跑** `fetch` 落 `temp/course_pool/fetch_log.json`（`COURSE_FETCH_LOG` 可覆盖），**dry-run 不落**（否则台账会被演练污染）。
- 内容＝`run_ts / duration_s / records / upsert / per_source{source: {ok, urls, records, modes{mode: n}, stale, errors}}`；`modes` 取值 `browser|jina|http|cache`。
- 读侧三态归一 **fail-closed**：坏 JSON / 错 schema / 源条目非 dict ⇒ `load_fetch_log()` 返 `None`（面板上「台账不可读」），**绝不把坏台账当空台账**。
- `mode=cache` 或 `stale=true` ⇒ 该源本次是**缓存回退**（走 `temp/course_pool_cache/<sha1(url)[:16]>.txt`（`.meta.json` 记 age），即 last-good 副本），看板打 stale chip。

### 9.2 看板面板

- `render` 在池统计面板下方新增「采集面」节：逐源 最近抓取时间 / 通道分布 / records / upsert / stale 标记；台账 `run_ts` 距今 > 48h ⇒ 整节标「台账过期」。
- 开关：`--fetch-log <路径>` 显式指定；`--no-fetch-log` 关闭该节（隔离/对比用）。
- 路径缺失 ⇒ 静默降级（不上板）；**显式**路径坏 ⇒ 抛错（同 §8.1 口径）。

### 9.3 分类与价格字段

- 分类口径单一源＝`CATEGORY_RULES`（模块级常量表）：**站点自报分类优先**，缺则按标题+元信息关键词兜底；新增源只需在解析里带 `category`，不再各写 if-else。
- 价格：`price`（现价）与 `price_orig`（原价/划线价）分列，缺失写 `None` 而非 0（0 会被误读成免费）。

### 9.4 慕课网三源（反爬：走 r.jina.ai）

`imooc_class`（体系课列表）/`imooc_free`（免费课列表）/`imooc_home`（首页轮播与推荐位，补列表页看不到的新上架）；三者 `channel=[jina,http]`、`parser=imooc_md_any`，http 只回 JS 壳故须 jina 渲染 markdown 后再抽内链。

## 十、统一落库（platform.db＝全站单一数据面）

所有面（采集/搜索/匹配）都落同一库：`temp/course_pool/platform.db`（gitignored）。
- 表：`courses / resources / candidates / haisou_seen / fetch_runs`（原有）+ `sources / keywords / pan_seen / pan_hits`（四表方案）；`schema_tables=11`。
- 重建：`python3 tools/course_pool.py db`（幂等全量：池 + 盘搜原始响应 + 海搜已见库）；JSON 摘要即输出。
- 看板：`render` 输出的「统一落库」面板读 `db_platform_overview`；`db --platform` 可单独出平台概览。
- 盘搜原始响应：`temp/pan_search/raw_*.json` 为「关键词→行数组」dict 形，按 url 去重写 `pan_hits`（实测 59196 行→11985 条）；**数据源是 raw 响应不是 seen.json**。
- 匹配：`match --apply` 写 `resources`；海搜已见（仓根 `birdie_monitor/haisou_seen_items.json`）整表同步进 `haisou_seen`。

### 10.1 自测沙箱铁律（R554 事故）

selftest 曾**在进程内**经 `cmd_match→db_build()` 默认参数真写生产库（夹具 2 门课覆盖 215）。铁律：
1. selftest 内一切写库路径必须进沙箱：`DB_PATH` 重定向到 `<tmp>/platform_default.db`（finally 还原）；
2. spawn 的子进程（render/db）显式带 `COURSE_DB=<tmp>/...`，不依赖继承；
3. 负控 T84＝自测前后对生产库做指纹（size+mtime+sha256）比对，不一致即 FAIL——防复发靠断言不靠自觉。
4. 现场 A/B（2026-10-06）：pre-fix 版对生产库副本 sha 变 + courses 215→2；fixed 版 sha 不变 + 84/84。

## 十一、主题优先级分层(2026-10-12 R575)

**起因(用户)**:「网盘搜索(海搜+盘搜)每日任务优先级有问题,最近搜出来的全是 AI 副业 / AI 自媒体;
这块我有关注但优先级低。我刚开始关注的是 AI Agent、AI Coding、AI 应用开发。」
盘搜侧**症状**:当日 6 条 = 2 编程 + 2 副业 + 2 漫剧,低优先面占比过高。

**根因**:`TOPICS` 四组**等权顺序** + 每词同一 `--max-per-kw`,而变现/自媒体组
(`ai_solo`)天然高产(10-11 raw 逐词:AI 一人公司 46 / AI 工具变现 36 / AI 副业 15 条合格),
把核心面挤到后面。

**实现(排序 + 条数预算,不另发请求)**=`tools/pan_search.py` 模块级常量:
- `PRIORITY_CORE = ["ai_coding"]`(排最前)、`PRIORITY_LOW = ["ai_solo"]`(排最后);
- `LOW_PER_KW = 3`:低优先主题**每词**保留上限(默认 cap 的缩小档),经纯函数 `per_kw_cap()` 取;
- 纯函数 `topic_order(keys)` / `per_kw_cap(topic, cap)` 供自测;开关 `PAN_PRIORITY=0` 关闭(逐字回旧等权行为);
- `ai_coding` 词表补 `AI Agent` / `AI 智能体` / `AI 应用开发`(用户点名的核心面,原先 `ai_coding` 缺这三词)。

**A/B(离线重算 10-11 raw,含真 judge 过滤;可复跑)**:
低优先占**总推送**比 24/86 = 27.9% → 9/71 = 12.7%;核心面 `ai_coding` 21 条不变;
`ai_solo` 每词由 8 压到 3(AI 一人公司 46→3 / AI 副业 15→3 / AI 工具变现 36→3)。
展示序由「coding → solo → overseas → course」改为「coding → overseas → course → solo」。

**判据(可复跑)**:`python3 tools/pan_search.py selftest` ⇒ 43/43(位置子命令,**写 `--selftest` 只打 usage rc=2**)。
**自测沙箱**:selftest 会把 `TEMP` 重定向到临时目录并在 finally 还原,
负控 E0/E/E1 断言生产 `temp/pan_search/` 指纹前后一致(防夹具 raw 写进生产)。

**归属边界**:本批只声明「排序与每词预算已改 + 离线 A/B 已算 + 自测通过」,
**不**主张「当日推送构成已改变」——真实构成见下一轮 11:45 定时的 done 报告。
