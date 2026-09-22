# TrendRadar 信源有效性审计（R343 · 2026-09-22）

> **一句话结论**：RSS 侧 14 个声明源里 **1 个真死（ai-weekly，实测 404）、4 个极低效（采纳率 ≤0.75%）**；
> 榜单平台侧 **11 个源全部「每天都在抓、却连续 70~126 天零采纳」**，根因已定位到检索通道
> ——**GA 把 7~8 个关键词拼成一个长串去查，而 TrendRadar 的 keyword 模式是整串子串匹配**，
> 因此 2026-07-14 起 132 期记录里**平台检索命中恒为 0**（实测拆词后 30 天可得 1396 条）。

---

## 0. 事实纠偏（起点前提有误）

| R341 规划时以为 | 实际 |
|---|---|
| 「TrendRadar 仓库 output 里 415+ 期报告带来源标注」 | 报告正文与物料在 **GA 仓库**：`output/trendradar_{morning,evening}_*.txt`（283 份）+ `.material.json`（132 份）= 415 文件 |
| — | TrendRadar 仓库 `output/` 是**抓取侧**：`news/*.db`（157 天，2025-12-21~2026-09-22）、`html/`151、`rss/`150 |
| — | 容器 `trendradar` 挂载：`config → /app/config (ro)`、`output → /app/output (rw)`；`config.yaml` 28685B |

审计因此改为**三条线索交叉**：报告正文（采纳）＋ material.json（择料）＋ 每日 SQLite（供给与抓取健康）。

## 1. 方法与复现

工具：`tools/trendradar_source_audit.py`（纯 stdlib，幂等可重跑）

```bash
python3 tools/trendradar_source_audit.py scan     # → temp/trendradar_source_audit/audit_data.json
python3 tools/trendradar_source_audit.py report   # A~D 表：采纳 / 供给 / 平台 / 声明
python3 tools/trendradar_source_audit.py check    # 14 个源 HTTP 实测 → reachability.json
python3 tools/trendradar_source_audit.py impact 30  # 检索通道损失量化
python3 tools/trendradar_source_audit.py verdict 30 # 三清单判决 → verdict.json
```

口径说明：
- **采纳**只认报告里 `▸` 行**行末**的 `（来源A / 来源B）`（3774 条中标到 3589 条；185 条确实没标）。全篇随便抓 `（…）` 会把页脚的「（同 WiFi 且本机开机时可访问）」这类也算进去，已规避。
- 来源名做归一化（`Linux.do/LINUX.DO → linux.do`、`VEX → V2EX`、`财联社热门 → 财联社`、`Solidot（IT资讯）→ Solidot` …）。
- 长尾标注（qwen.ai / arXiv / Anthropic / NYT / TechCrunch …）**不是幻觉**：物料 RSS 里实测存在 **2325 个唯一域名**，这些标注就是原始文章域名，已逐个比对确认。
- 时间跨度为**全量 283 份报告（2026-04-23 → 2026-09-21）**，远超「≥90 天」的验收要求；DB 抓取健康取近 30 天。

## 2. 结论一：RSS 侧 14 个声明源逐条体检

| feed | 启用 | HTTP | 物料条数 | 采纳条数 | 采纳率 | 首见 | 最近见 | 判定 |
|---|---|---|---|---|---|---|---|---|
| hacker-news | ✔ | 200 | 6649 | 985 | 14.81% | 04-24 | 09-21 | 在服役 |
| linux-do | ✔ | 200 | 5132 | **1013** | **19.74%** | 07-17 | 09-21 | 在服役 |
| v2ex | ✔ | 200 | 17732 | 731 | 4.12% | 04-24 | 09-21 | 在服役 |
| solidot | ✔ | 200 | 2354 | 277 | 11.77% | 05-03 | 09-21 | 在服役 |
| openai-blog | ✔ | 200 | 3152 | 67 | 2.13% | 05-16 | 09-18 | 在服役 |
| github-trending-daily | ✔ | 200 | 767 | 35 | 4.56% | 05-26 | 09-21 | 在服役 |
| towards-data-science | ✔ | 200 | 1713 | 33 | 1.93% | 05-05 | 09-17 | 在服役 |
| supply-chain-brain | ✔ | 200 | 2722 | 12 | 0.44% | 04-28 | 09-01 | 极低产出 |
| freightwaves | ✔ | 200 | 4873 | 9 | 0.18% | 05-05 | 09-06 | 极低产出 |
| supply-chain-dive | ✔ | 200 | 1052 | 7 | 0.67% | 07-24 | 09-14 | 极低产出 |
| saastr | ✔ | 200 | 801 | 6 | 0.75% | 06-08 | 09-06 | 极低产出 |
| ruanyifeng | ✔ | 200 | 214 | 6 | 2.80% | 07-25 | 09-11 | 在服役 |
| **ai-weekly** | ✔ | **404** | **0** | **0** | — | — | — | **零产出（真死源）** |
| yahoo-finance | ✘ | 跳过 | 0 | 0 | — | — | — | 已禁用（设计如此） |

**死源取证（ai-weekly / AIbase 快讯）**
- 配置 `config.yaml:124-126` 声明启用，URL `https://www.aibase.com/rss/news.rss`
- 实测 HTTP **404**（近 30 天其余 13 个源全 200，size 8.6KB~741KB）
- 物料侧 0 条、报告侧 0 次 → 三重一致
- 另测 4 个候选替代地址 `aibase.com/rss`、`/feed`、`/rss.xml`、`news.aibase.com/rss` → **全部 404**，故不建议盲替换 URL，应改为禁用或换源

## 3. 结论二：榜单平台 11 个源「在抓、但不进报告」

近 30 天抓取健康（`crawl_source_status`），**每天 1220 次上下成功、个位数失败**，供给完全正常：

| platform | 报告标注 | 历史采纳 | 最近见 | 30 天抓取条目 | 成功/失败 |
|---|---|---|---|---|---|
| wallstreetcn-hot | 华尔街见闻 | 76 | **07-13** | 730 | 1220/14 |
| zhihu | 知乎 | 38 | **07-12** | 2578 | 1230/4 |
| baidu | 百度热搜 | 19 | 06-29 | 5568 | 1221/13 |
| toutiao | 今日头条 | 13 | 06-30 | 6491 | 1226/8 |
| bilibili-hot-search | bilibili 热搜 | 13 | 06-15 | 4568 | 1217/17 |
| douyin | 抖音 | 9 | 06-17 | 3071 | 1227/7 |
| thepaper | 澎湃新闻 | 8 | 06-29 | 1519 | 1213/21 |
| tieba | 贴吧 | 8 | 06-26 | 1368 | 1223/11 |
| ifeng | 凤凰网 | 8 | **05-18** | 1041 | 1223/11 |
| weibo | 微博 | 5 | 06-18 | 7935 | 1229/5 |
| cls-hot | 财联社热门 | **0** | 从未 | 1319 | 1224/10 |

> 这不是「源失效」，是**通道失效**——见第 4 节。

## 4. 事故级发现：平台检索通道长期 0 命中（根因已确证）

**证据链（四步，全部可复跑）**

1. **132 份 material.json，无一例外**：两处查询 `success: true` 但 `total: 0`，消息统一为
   「未找到匹配的新闻（查询范围: 今天，可用数据: 2025-12-21 至 <当天>）」——注意**后半句说明库里有数据**。
2. **库里确实有数据**：进程内直调 `ParserService.read_all_titles_for_date(2026-09-21)` → **11 个平台 / 1274 条标题**；
   用单关键词 `华为` 实测命中 **7 条**（`_search_by_keyword_mode` 按整串子串匹配）。
3. **根因在匹配语义**：`mcp_server/tools/search_tools.py` 的 keyword 模式
   `match = query_lower in title.lower()` —— **把整个 query 当一个关键词**。
   而 GA 生产脚本 `sche_tasks/run_trendradar_digest.py:60` 里：
   ```python
   KEYWORDS = [
       'AI 画图 Stable Diffusion ComfyUI FLUX 大模型 GitHub 开源 编程',
       'LLM DeepSeek GPT Claude 开源工具 安全漏洞 GPU 芯片',
   ]
   ```
   调用处（约 258 行）传 `search_mode='keyword'`、`limit=30`。
   → 要求这 7~8 个词**连续成句**出现在标题里，现实中概率为 0。**必然 0 命中，永远。**
4. **时间线吻合（旁证）**：material.json 纪元始于 **2026-07-14**，而平台标注最后一次出现在 **2026-07-13**（华尔街见闻）
   / 07-12（知乎）。之后的报告只靠 RSS 供给，于是 linux.do / HN / V2EX / Solidot 占比越来越高。

**影响量化**（`impact 30`，近 30 天）

| 指标 | 数值 |
|---|---|
| 整串当关键词的命中数 | **0**（预期 0，作负控） |
| 拆成单词后并集命中标题数 | **1396**（按查询/天累计） |
| 单日最高 | 08-24 两查询 23/17 条 |

按 `limit=30` 计，**每天最多 60 条平台热榜情报被静默丢弃**（约占每期平台面材料的 100%）。
拆词后高频命中词：`AI 943`、`芯片 164`、`GPT 107`、`DeepSeek 86`、`开源 32`、`Claude 29`、`大模型 28`。

**修复方向（二选一，尚未应用——涉及 GA 生产脚本，按待审流程走）**
- **A（推荐，改动最小）**：GA 侧把 `KEYWORDS` 拆成单词逐个 `search_news`，结果按 title 去重合并后再拼 material。约 10 行，不动 TrendRadar 仓库。
- **B**：改用 `search_mode='fuzzy'`（TrendRadar 侧自带分词/相关性打分），需先确认其匹配语义与 limit 行为。

## 5. config 最小 diff 建议（**只建议，未应用**；config 为容器只读挂载）

```diff
      - id: "ai-weekly"
        name: "AIbase 快讯"
        url: "https://www.aibase.com/rss/news.rss"
+       enabled: false            # 实测 404 达 30 天，物料 0 条、报告 0 次；4 个候选替代地址亦全 404
```

```diff
  # 垂直RSS源（R48扩展）：4 个源合计供给 9448 条 → 累计只采纳 34 条（0.36%），建议降为按需开启
      - id: "freightwaves"
+       enabled: false            # 采纳率 0.18%
      - id: "supply-chain-brain"
+       enabled: false            # 0.44%
      - id: "supply-chain-dive"
+       enabled: false            # 0.67%
      - id: "saastr"
+       enabled: false            # 0.75%
```

**不修改 config 的替代方案**（若想保留这些源做背景）：在 GA 侧 `get_latest_rss(days=1, limit=400)` 后按源分摊配额，
避免 freightwaves（30 天 4873 条）这类高产低效源挤占 400 条混合池名额，把预算让给 linux.do（采纳率 19.74%）。

> 判定「极低产出」用的是**采纳率**（报告里真被用到的比例），不是抓取量。抓取健康度本身没问题——11 个平台每天 1200+ 次成功。

## 6. 未验证 / 残余风险

1. **0 命中起始时间**：只能证明「至少 2026-07-14 起（132 期、约 70 天）」，更早的取数路径无留痕（早期报告由另一条链路生成），故未断言事故总时长。
2. **采纳数据是下界**：3774 条 `▸` 里 185 条（4.9%）没有来源标注，这些条目未被计入任何源；另有极少数标注为「未知来源/RSS」也归入未标注。
3. **源名归一化仍是启发式**：`ALIAS` 表覆盖了实测出现的主要写法（含错拼 VEX），未见的新写法会以原样计入「长尾」。
4. **抓取失败率**：近 30 天每天个位数失败（如 thepaper 21 次），本任务未追因（不影响结论：平台是**零采纳**而非零抓取）。
5. **未做全网寻源**：ai-weekly 只实测了 4 个候选 URL，未穷举 AIbase 是否有官方新 RSS。
6. **两个修复方向都没动手**：A 涉及 GA 生产链路、B 涉及 TrendRadar 语义确认，均需你点头后再动（按待审清单流程）。

## 7. 产物清单

| 路径 | 内容 |
|---|---|
| `tools/trendradar_source_audit.py` | 审计工具（scan / report / check / impact / verdict 五子命令，纯 stdlib，幂等） |
| `temp/trendradar_source_audit/audit_data.json` | 三类数据的扫描结果（采纳 / 供给 / 平台 / 声明） |
| `temp/trendradar_source_audit/audit_full.md` | A~F 六张表全文（20KB，本报告的原始数据面） |
| `temp/trendradar_source_audit/reachability.json` + `check.log` | 14 个源 HTTP 实测原始记录 |
| `temp/trendradar_source_audit/verdict.json` | 三清单机器可读结论 |
| `temp/trendradar_source_audit/impact.json` | 检索通道损失量化明细 |
| `temp/trendradar_source_audit/findings.md` | 边查边记的现场笔记（含路径纠偏） |
