# R346 规划模式（第 3 批）：下批 TODO 生成 + 独立 subagent 评审

- 时间：2026-09-22（用户离开 >30min，反射器触发的自主轮）
- 模式：**规划模式**（`memory/autonomous_operation_sop/task_planning.md`）——本轮不执行任何 TODO，只产出清单并交独立评审
- 输入：`temp/TODO.txt`（仅剩常驻种子项）、`temp/autonomous_reports/history.txt`（314 行 R1–R345）、R335–R345 报告、`temp/pending_decisions/`

## 1. 批判读 history：识别到的三类低价值模式（本批刻意回避）

| 模式 | 历史证据（条数） | 本批处理 |
|---|---|---|
| ①「分析自己的报告/日志/ROI/记忆欠账」的**元分析循环** | R246/R248/R255/R256/R262/R316/R33/R47/R59/R93/R101/R107/R237/R340 等 | 不立项；C 条（对自身报告建索引）因此被删 |
| ②只封装 `*_utils.py` 却**无真实消费者**的工具化 | R300–R315 连续 10 个 utils（tmux/ffmpeg/pdf/image/http/watchdog/scraper/psutil/openssl/sqlite） | 不立项；A/C 两条的"无消费者"硬伤被评审员抓住并据此扣分 |
| ③**重复验证已证实结论**／泛采集标题 | R243（webdriver 误判）/R253、R249/R238"挖小众工具" | 不立项；C/D 两条被判"跨批次重提/重复造轮子" |

同时保留近三批的高价值形态：**真实端到端交付**（R342/R345）、**三线交叉取证**（R343）、**只读实测地图**（R344）。

## 2. 立项前的最小可行性探测（执行 4 项，避免"前提错位"）

| 探测 | 结果 | 对候选的影响 |
|---|---|---|
| `adb devices` | adb 已装（/opt/homebrew/bin/adb），**无设备连接** | 原「Android 手机侧数据源地图」候选直接删除（需用户插线授权，非自主可执行） |
| `ollama` / `llama-cli` / HF 缓存 | ollama **未装**；`llama-cli`/`llama-server` 已装；`~/.cache/huggingface` 895M 含 `BAAI/bge-small-en-v1.5`、`cross-encoder/ms-marco-MiniLM-L-6-v2`、faster-whisper tiny/base/small | 支撑 C 前提（但 C 因语料选错+无消费者被否） |
| 本机音视频（Downloads/Documents/Desktop/Movies 限深2） | 26 个（限深2；评审员全量复扫为 33 个，其中 15 个无音轨） | A 条立项，但口径被评审收窄 |
| 微信 `msg/` 目录 | 可 `ls` 限深（`xwechat_files/wxid_.../msg`），**递归 `find` 会挂起**（同 R344） | A 条只做限深清点，不做递归 |

## 3. 候选与独立评审（6 条 → 评分 A5 B7 C4 D4 E6 F7）

评审方式：候选清单写入 `temp/review_todos3.txt`，`nohup python3 agentmain.py --func <绝对路径>`（pid 15564），
输出 `temp/review_todos3.out.txt`（169 行原始过程）+ **评审报告 `temp/review_todos3.review.md`（11.4KB）**。
评审员自读 insight/global_mem/task_planning/11 份报告/DECISIONS 台账，并**自行跑了 6 项物理探测**（不采信我的摘要）：

1. **音轨扫描**：33 个音视频里 **15 个无音轨**（`test_video_副本*.mp4` ×13 字节完全相同 40634B + `output_mjpeg.mp4` + 飞书录屏），含音轨的 18 个里 2 组字节相同重复件 ⇒ **可用素材 16 个**（题面"26 个"被实测纠正）
2. **knowledgeC 保留期**：ZOBJECT=17455，ZCREATIONDATE 跨度 2024-12-27→2026-09-22 但**仅 33 个不同日期** ⇒ 证实"约 4 周滚动删除"
3. **检索栈**：bge + reranker + llama.cpp 均在，但 `sentence_transformers` **仍 ModuleNotFoundError**（R341"零检索栈"结论未被推翻）
4. **pending_decisions 实况**：发现 `DECISIONS_R343.md`（今日 08:01，比 R343 报告晚 32 分钟）**已把 R343 结论做成可批准物**（D1 死源指名 config.yaml L124-126、D2 拆词含 132 期 total=0 与"30 天可捞回 1396 条"）
5. **远端基础设施**：`known_hosts` 30 条含 `openclaw-server.tailf82c5b.ts.net`、`100.112.252.36`、`100.114.166.15`（=R344 Safari 访问 6 次那台，CGNAT）；docker context=orbstack、kubectl+~/.kube、~/.aliyun、gcloud 均在；但 `tailscale status` = **"Tailscale is stopped."**
6. **launchd + 归档站**：`archive/index.html` 5.92MB、mtime 今日 10:06（新鲜）

### 逐条裁决

| 候选 | 分 | 关键理由（评审原文要点） | 裁决 |
|---|---|---|---|
| A 音频批量转写索引 | 5 | 前提部分错位（15/33 无音轨）、`silk_to_text` 只吃 .silk（"复用"不成立）、**无消费者** | **改口径后收录**（去重+只挑人声+限长音频 top-N，工具改用 asr_cli.py） |
| B KnowledgeC 底稿 | 7 | 前提经独立复算成立；硬伤="每日落一份"隐含新增定时器（持久变更需批准） | **收录**，补"定时注册单列待批"；并与 `DECISIONS_R344` D1 合并为一条 |
| C 本地离线检索 | 4 | 语料即"自身报告"=本批自称要避开的元分析循环；**跨批次重提 R341 已否决的候选**；无消费者 | **删除** |
| D TrendRadar 源治理 diff 包 | 4 | 与 `DECISIONS_R343.md` D1/D2/D4 **逐条对应**=重复造轮子；config 属只读挂载，自动 apply 边际收益低 | **删除**（真增量只剩"给 config 行套 apply.py"，不值得独立一轮） |
| E 远端基础设施地图 | 6 | 前提实测成立且有料；硬伤=tailscale stopped（实得量偏低）、"清单式"无假设 | **收录**，收敛为可证伪命题"本机是否连接过用户自建内网/云主机" |
| F 产物新鲜度审计 | 7 | 正对 R343 暴露的静默失败；归档覆盖率可算；与 `roi_monitor v2` 部分重叠 | **收录**，验收写死边界"只做产物级判据" |

### 评审推荐补充（我未列到、评审认为更值）

- **遗漏1**：TrendRadar 拆词修复的**效果 A/B 离线复算**（整串 vs 逐词重放历史日期，量化每天多捞回多少条）—— 直接驱动 D2 批不批。
- **遗漏2**：报告"唯一副本"防复发闭环的**端到端验证**（`temp/autonomous_reports/` 仍唯一副本无版本控制，R336 §4 钩子至今只是提案）—— 应真跑一次验证双写是否生效。

两条均**收录**。

## 4. 最终写入 `temp/TODO.txt` 的 6 条（+ 常驻种子项）

1. 产出 | KnowledgeC 数字行为底稿：幂等快照器 + 首份基线报告（与 DECISIONS_R344 D1 合并）
2. 工程 | 定时/反射任务「产物级」新鲜度审计（边界：任务计数类归 roi_monitor v2）
3. 环境 | 命题验证：本机是否连接过用户自建内网/云主机（只读，禁连接尝试）
4. 工程 | TrendRadar 拆词修复「效果 A/B 离线复算」（评审推荐·遗漏1）
5. 灾备 | 报告「唯一副本」防复发闭环端到端验证（评审推荐·遗漏2）
6. 产出 | 本机音频资产：去重→挑人声→长音频转写备忘（改口径后收录）

种子项已按 SOP 保留（`[ ] 产出 | 生成下一批TODO（规划模式·常驻种子项，勿删）`），保证下一轮不锁死。

## 5. 边界与未做

- 本轮**未执行任何候选任务**（规划模式纪律），未改任何生产配置、未写任何 memory 散文（记忆更新仍是待审项）。
- 候选 33/15/16 等数字来自**评审员独立实测**；我自己的首轮探测是"限深 2 层 26 个"，两者口径差异（限深 vs 全量）已如实保留在报告里，未做合并粉饰。
- 评审 subagent 是**单评审员单轮**，其评分属启发式；`temp/review_todos3.out.txt` 保留其原始过程（含它自己的探测命令），未二次核验。
