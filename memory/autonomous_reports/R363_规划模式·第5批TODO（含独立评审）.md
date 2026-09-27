# R363 规划模式：第 5 批 TODO（产出 + 独立 subagent 评审 + 处方回写）

本轮按 `memory/autonomous_operation_sop/task_planning.md` 进入**规划模式**（硬约束：本轮**不执行**任何 TODO）。
产出：6 条候选 → 独立 subagent 评审（禁自评审）→ 按评审判定**逐条回写处方/替换低分项**。

## 1. 评审怎么做的（过程可信度）
- 召唤：`nohup python3 -u agentmain.py --func temp/review_todos5.txt </dev/null > temp/review_todos5.log 2>&1 & disown`（★本机**无 setsid**，见 haisou SOP）
- 输入 = **仅** 6 条候选 + 一句"读记忆库和本 SOP 自行判断，逐条评分 1-10 并简述理由"（无额外先验，避免诱导）
- subagent 跑了 **12 个 turn**，真去探测而非空评：`df`、`du -sh ~/.cache/* ~/Library/Caches/*`、`which docker`、sqlite **FTS5/trigram 可用性实测**、`grep` L1 陈旧数字、`sources.json` 现状
- 原文：`temp/review_todos5.out.txt`（修复双编码版 `temp/review_todos5.fixed.txt`）

## 2. 评分与处置（★已全部落进 temp/TODO.txt）
| 行 | 条目 | 分 | 处置 |
|---|---|---|---|
| 55 | 磁盘「可再生空间」回收实测 | **9** | 保留·最高优先（改瞄准点） |
| 56 | 记忆「过时与矛盾」审计 | **8** | 保留（先去重 R340 / C5–C18） |
| 53 | 待批积压登记 + 超期提醒 | **7** | 保留（**砍掉 IM 提醒**=撞禁区） |
| 54 | 本地推理第二消费者（trendradar 预筛影子 A/B） | **7** | 保留（先钉死 ground truth） |
| 52 | 历史知识「检索式回忆」最小实证 | **6** | 保留（必须**二选一**，禁"值得进一步研究"） |
| 57 | ~~R362 搜索词候选「项目雷达」预览卡~~ | **5** | **替换** → 「缓存回收脚本化 + 重建验证」 |

执行顺序建议（评审给）：④ → ⑤ → ② → ③ → ①。

## 3. 评审用真探针纠正的硬事实（本轮**新数字**，下轮执行一律以此为准）
- **磁盘比候选写的更险**：数据卷 `414G/460G = 98%`，可用仅 **9.4Gi**（候选写"~14G"= 过时）
- 可回收真排行榜（实测）：`~/.cache` **25G**（uv **21G**）+ `~/Library/Caches` **21G**（JetBrains **13G**、playwright 2.5G、playwright-918M、bitwarden/tutti/orca updater ~1.9G、Google 1.2G、LarkShell 860M）⇒ **合计 46G，是 logs（1.55G，待批）的 20 倍以上**
- **"容器镜像"是错误瞄准点**：docker/OrbStack 实测仅 **544K**（我原候选的前提偏差，已改）
- **FTS5 + trigram 实测可用**；检索语料 = memory 113 + 报告 133 + history ~330 行 ≈ **576 文档，但仅 2.1 MB** ⇒ "值不值得建索引"的先验概率其实不高（该条最大价值在**验收③能否证否**）
- **L1 数字漂移已实锤**：仍写 `Chrome 12501 URL`（R362 实测 **12,377**）、`knowledgeC 17400 条`（R357 实测 **18,277**），且 L1 **完全没有** R347/R357 的 `kc_snapshot` 链路事实
- `temp/pending_decisions/DECISIONS.md` 覆盖**止于 R348**；R355/R357/R359/R360/R361/R362 中"待批/待审"出现 9/8/12/6/2/3 次 ⇒ ≥6 条新待批项**散落未登记**；⚠️ 该台账在 `temp/`（`.gitignore`）= **零版本控制**，与当年报告丢失同类风险
- `project_update_monitor/sources.json` = 8 源（Paseo/Ruflo/Multica/Tutti/Orca/GenericAgent/CCB/Trellis）

## 4. 我按评审做的回写（可复核）
- 52–56 行：每条追加 `【评审 N/10·保留】处方：…`（把评审的具体要求写成验收条件，下轮执行者不必再读评审全文）
- 57 行：整条替换为「缓存「回收脚本化 + 重建验证」」；原 ⑥ 的唯一有价值部分（**搜索词→候选清单**）按评审并入 **第 53 行（待批台账）作附录**
- 种子项保护：`[ ] 生成下一批TODO（规划模式·常驻种子项，勿删）` **原样保留**（SOP 明文：删了就死锁）；当前 `[ ]` 仍为 8 条
- `#45`（helper 编号补丁）**未动**（标注"等用户批准·勿重做"）

## 5. 工具层发现（本次新踩，值得进记忆）
1. **`agentmain.py --func` 的 `*.out.txt` 是"边跑边写"的实时流**（含 `LLM Running (Turn N)` 与工具调用行）⇒ 可当**进度条**用（本轮靠它判定"真在探测而非空转"），末尾出现 `[ROUND END]` 才算完成。
2. **该文件会出现双编码段**：直接用 `cat/tail` 看尾部是乱码，`Path.read_text()` 解码为 utf-8 后再对可疑行做 `l.encode('latin-1').decode('utf-8')` 可还原（本轮 7631 字节中约 4.5KB 受影响）。判"输出坏了"之前先修编码，勿误判为失败。
3. 子进程首屏会打印 `numpy 1.x vs 2.4.4` 的 ImportError（LLMLingua warmup 噪音），**exit=0 时无害**（与 project_update_monitor SOP 记录一致）。

## 6. 本轮边界
- **未执行任何 TODO**（规划模式硬约束），只做规划 + 评审 + TODO 回写
- **memory 零改动**；对记忆的改动建议见 §7（按要求只作**提案**，不做 P0 之外的自作主张）
- 生产零改动；无新增定时任务

## 7. 记忆更新建议（**待审**，未写入）
1. **L1 数字漂移（与待办 ⑤ 呼应，但它是"提案"不是"执行"）**：L1「进程内存/扫描」行的 `Chrome 12501 URL` → **12,377 URL（R362）**、`knowledgeC 17400 条` → **18,277（R357）**，并补一句 `kc_snapshot 每日快照链路已落地(R357)`。**理由**：L1 每轮注入 260 字，错一次的成本是"每一轮"。
2. **L1「定时/自主/反射」行可加极简一句**：`规划模式第5批(2026-09-23)出6条TODO经独立评审(9/8/7/7/6,替换1条);磁盘真实压力=可用9.4Gi/98%,可回收缓存46G(uv21G+JetBrains13G)`。**理由**：这是"每轮决策都要用"的资源前提。
3. **L3 建议**：给 `subagent.md` 或 `autonomous_operation_sop` 补三条实测：①`--func` 输出是实时流可当进度条 ②输出双编码的修复方法 ③评审 prompt 只喂"候选+评分指令"时，subagent 会**自己去读 TODO.txt/history/报告**核验数字（本轮即是）⇒ 规划者无需在 prompt 里塞资料，只要保证候选里的数字**可被证伪**。


---

## 收尾核验补记（Turn 33–35，重读 SOP 后自检）
- **报告落点**：SOP 第 3 行明文警告 —— 报告在 `temp/autonomous_reports/`（`./autonomous_reports/`），**不是** `memory/...`。本轮 `helper.complete_task` 落点正确（`temp/autonomous_reports/R363_规划模式·第5批TODO（含独立评审）.md`，6122B），但我的 TODO [x] 记录行一度写成 `memory/autonomous_reports/...` ⇒ **已改正**。
- **口径勘误**：`history 条数` 我先前写 362 属**未复核**；实测 temp 树 **331** 条（memory 树为 09-23 02:01 快照 326 条）⇒ TODO 第 49/53 行已改为 331。`报告 133 md` 系评审探针口径，实测两树 = memory 106 + temp 28 = **134** ⇒ 已按实测改写。`memory 85 L3` 实测一致 ✓。
- **INDEX.md 归属澄清**（勿手改）：`memory/autonomous_reports/INDEX.md` 是 `tools/autonomous_reports_archive.py` 的**抢救重建产物**（头部自述「生成 2026-09-23 / 索引可见 358 个编号」），维护到 R358；它不是逐轮维护的活索引（R359–R363 均不在其中）⇒ **本轮无需、也不应手工补行**；要纳入需重跑生成器（提案，未做）。
- **自检发现的自身失误（记录以免复现）**：核验时我在脚本里 `cd temp` 后又用相对路径 `temp/autonomous_reports/...`，造成一次**假阴性**（误判报告不存在）。⇒ 核验一律用绝对路径。
