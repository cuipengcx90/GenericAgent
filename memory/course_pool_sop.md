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
python3 tools/course_pool.py selftest        # 自测（46/46）
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
