# R388 Basic Memory 只读语义索引「试挂」评测（TODO#12）

> 派生自 R387（BasicMemory 冲浪实测）。本轮目标：把 `memory/*.md` 做成**只读**语义索引，用真实数字回答「它能否替代现在的 grep 多文件检索」。
> 时间：2026-09-25 00:2x 收口（评测数据 23:33–00:27 采集）。
> 全部实验在隔离 HOME（`temp/bm_sem/home`）内完成，**真实 `~/.basic-memory` 零写入**（证据见 §6）。

---

## §0 结论（验收③：二选一）

**不接。** 理由（三条，均基于实测数字）：

1. **中文最佳配置也没超过 grep**：16 题问答集上，Basic Memory 最优中文模型 `BAAI/bge-small-zh-v1.5` hit@5 = **11/16**，而一句停用词过滤的 `grep` 基线（`grep_naive`）= **12/16**。语义检索**不是增量**，是**退步**。
2. **语义增益为零**：`grep_naive` 漏的 4 题（Q5/Q14/Q15/Q16），Basic Memory 中文化后**全部也漏**（无一被语义救回）；反而多漏 1 题（Q12「图片里的文字怎么自动识别」→ `vision_sop.md`，`grep` 能命中，向量检索漏）。
3. **代价不成比例**：单查询 **2.66–3.72 s**（grep 为本地毫秒级）、需常驻服务 + 模型缓存（本轮 3 个模型共占数百 MB）、需维护一份与 `memory/` 同步的索引（多一份可漂移状态）。收益为负、成本为正。

**保留意见**：若未来把「人工锚点检索」（`grep_oracle` = 16/16）作为基线，则 Basic Memory 更无立足之地；本轮**不改**任何检索链路，也不建索引、不装定时。

---

## §1 验收对照表

| # | 验收要求 | 实测证据 | 结论 |
|---|---|---|---|
| ① | 只读索引 `memory/*.md`，不动权威树、不装定时 | `HOME=temp/bm_sem/home` 隔离运行；`memory/` 内文件 mtime 未变；无 cron/launchd 新增 | ✅ |
| ② | ≥10 题、真数字对比 grep | **16 题** × 3 检索模式 + 2 种 grep 基线（§3 表）；逐题明细见 §4 | ✅ |
| ③ | 结论二选一 | §0：**不接** | ✅ |
| ④ | 目录外写入入待批台账 | 真实 `~/.basic-memory` 内 `grep bm_sem` 命中 **0**（§6）⇒ 本轮**无目录外写入**，故台账无新增项；若未来接续需写入该目录（68M 缓存/`cloud_client_id`/auto-update），届时须先入台账 | ✅（零写入） |
| ⑤ | 只索引 `.md` | `reindex --full` 输出 `project index: 85 observed, 85 indexed, 0 deleted`，与 `ls memory/*.md \| wc -l` = **85** 完全一致（同目录另有 34 个非 `.md` 文件未被索引） | ✅ |

---

## §2 方法与语料

- **语料**：`~/projects/GenericAgent/memory/*.md`（顶层 85 个，全部是 SOP/记忆文件）。
  - `ls memory/*.md | wc -l` → `85`；`find memory -maxdepth 1 -type f | wc -l` → `119`（34 个非 `.md` 未入索引）。
- **问答集**：**16 题**，全部为「用户口语化提问 → 应命中的那一篇记忆文件」形式，题目与 16 个 target 一一不同文件（无重复 target）。
- **检索模式**：Basic Memory 的 `default` / `vector` / `hybrid` 三种 query 类型各跑一遍，取**前 5 条结果的 fileName 集合**判定命中。
- **两条 grep 基线**：
  - `grep_naive`：从题干自动抽取实词（去停用词）后 grep 全库 → **12/16**（模拟「人随手 grep」）；
  - `grep_oracle`：人工写定的锚点关键词 → **16/16**（上界参照）。
  - 时间合计 **0 s**（本地文件遍历）。
- **评测脚本**：`temp/bm_sem/eval.py`（结果落 `eval_results_{en,zh,jina}.json`）。
- **模型轮次**：
  1. `phase3`：fastembed 默认英文模型 `BAAI/bge-small-en-v1.5`（384d）；
  2. `phase4`：`BAAI/bge-small-zh-v1.5`（512d，需同时配 `semantic_embedding_dimensions=512`）；
  3. `phase6`：`jinaai/jina-embeddings-v2-base-zh`（768d，最后一次升级对照）。

---

## §3 结果（真数字，16 题）

| 配置 | 模型 / 维度 | default hit@5 | vector hit@5 | hybrid hit@5 | 平均单查询延迟 |
|---|---|---|---|---|---|
| phase3 | `bge-small-en-v1.5` / 384 | **3/16** | 3/16 | 3/16 | 3.54–3.72 s |
| phase4 | `bge-small-zh-v1.5` / 512 | **11/16** | 11/16 | 11/16 | 2.66–2.77 s |
| phase6 | `jina-embeddings-v2-base-zh` / 768 | **5/16** | 5/16 | 5/16 | 3.17–3.34 s |
| 基线 A | `grep_naive`（实词） | — | — | **12/16** | ~0 |
| 基线 B | `grep_oracle`（人工锚点） | — | — | **16/16** | ~0 |

补充实测：

- **三种检索模式几乎同分**：`default == hybrid` 在 15–16/16 题上结果相同，`default == vector` 在 15/16 题上相同 ⇒ 排名实际由**向量通道**主导，所谓 hybrid/FTS 增益在本语料上不可见。
- **中文查询的 FTS 通道完全无效**：中文提问时关键词（FTS）通道命中 **0**，结果全部来自向量集合 ⇒ 中文场景下 Basic Memory 退化为「纯向量 top-k」，无 keyword 兜底。
- **模型越大不一定越好**：768d 的 jina-zh 反而从 11/16 掉到 5/16（在 Basic Memory 的调用方式下），说明其嵌入/相似度口径与本语料不匹配，不能靠「换更大模型」翻盘。
- **向量 chunk 规模**：jina 配置下 `search_vector_embeddings_rowids` 计 **2515** 行（85 篇 → 平均 ~29.6 chunk/篇）。
- **索引与向量两轮均干净**：phase4/phase6 日志均为 `85 observed, 85 indexed, 0 deleted`、`0 skipped, 0 errors`（即失败不是「没嵌入成功」造成的）。

---

## §4 失效模式（关键判读）

**逐题分档（以 zh-512 为主）**

| 类别 | 题号 | 说明 |
|---|---|---|
| Basic Memory 命中、grep 也命中 | Q1,Q2,Q3,Q4,Q6,Q7,Q8,Q9,Q10,Q11,Q13（11 题） | 语义检索**没有创造任何额外价值** |
| Basic Memory 漏、grep 也漏 | Q5,Q14,Q15,Q16（4 题） | 共同盲区（题干口语化、与目标文档用词几乎无交集） |
| Basic Memory 漏、**grep 能命中** | Q12（1 题） | 语义检索的**净损失**：Q12「图片里的文字怎么自动识别出来」→ `vision_sop.md`；grep 关键词可中，向量 top-5 不含 |

**未命中题的「真实排名」探针**（page-size 拉满到 85 篇全库，取目标文档 rank；命中题不列）：

| 题号 | 提问 | 目标文档 | 真实 rank（/85） |
|---|---|---|---|
| Q5 | 怎么看别的进程里存了什么数据 | `procmem_scanner_sop.md` | 53 |
| Q12 | 图片里的文字怎么自动识别出来 | `vision_sop.md` | 7 |
| Q14 | 网页内容由脚本动态渲染怎么抓到生成的结构 | `jshook_runtime_sop.md` | 45 |
| Q15 | 定时任务悄悄失败没有任何提示怎么提前发现 | `scheduled_task_freshness_audit_sop.md` | 7 |
| Q16 | 换了台电脑后命令行工具都找不到了 | `brew_path_monitor_sop.md` | 72 |

判读：

- 不是「差一点」——除 Q12/Q15 排第 7，其余落在 **45/53/72**，即便把 top-k 放宽到 10 仍会漏两题；放宽到 20 也漏两题。
- 5 个失败样本的共性是**题干与文档字面几乎不重叠**（口语 vs 术语），这正是"语义检索该赢"的场景，却仍然失败 ⇒ 症结不在模型大小，而在**整篇 85 个长文档做文档级嵌入**（记忆文件平均数千字，一篇里几十个主题，单向量被稀释），以及 `semantic_min_similarity=0.55` 的截断（召回规模由配置截断而非全覆盖，本轮未逐题留存召回条数，故不给数字）。

---

## §5 机制发现（复现/运维要点，均已实测）

1. **配置键**：`semantic_embedding_model`（模型名）与 `semantic_embedding_dimensions`（维度）**必须成对一致**；只改模型不改维度 → 维度不符；维度写错 → 查询直接 **300 s 超时**（不报错，只是挂住）。
2. **`reindex --full` 不要带 `--project`**：一旦指针指向被判为「云项目」的标识（本轮踩到 `--project memory-index`），reindex 会**整段静默跳过**（日志仍打印 `Project:` 行与 100% 进度条，实际 `0 observed, 0 indexed`），随后向量表为空、查询超时。→ **重建后必须回查 `search_vector_embeddings_rowids` 行数 > 0**，别只看日志末尾。
3. **中文提问走纯向量**：FTS 命中 0（§3），结果集合只由向量通道给出，其规模由 `semantic_min_similarity`（本轮 0.55）与 `vector_k`（本轮 100）截断；`hybrid` 与 `default` 结果基本相同。
4. **延迟**：单查询稳定 **2.7–3.7 s**（含服务 HTTP + 本地 embedding），比 grep 高 3 个数量级；批量 16 题一轮评测约 45–60 s。
5. 索引产物：`home/.basic-memory/memory.db`（SQLite，含 FTS 表 + 向量表 `search_vector_embeddings_rowids`）。

---

## §6 隔离与「零外部写入」证据（验收④的依据）

- 运行环境：`HOME=/Users/cuip/projects/GenericAgent/temp/bm_sem/home`，服务端口与真实实例不同；全部索引/缓存/DB 落在该隔离目录内。
- **真实实例日志零命中**（实测输出）：
  ```
  $ ls -la ~/.basic-memory/*.log
  -rw-r--r--  1 cuip  staff  75409 Sep 24 23:22 /Users/cuip/.basic-memory/basic-memory.log
  $ grep -c bm_sem ~/.basic-memory/*.log
  0
  $ grep -rl bm_sem ~/.basic-memory/
  （无输出）
  ```
  真实 `~/.basic-memory`（68 M，由 R387 会话 2026-09-24 22:24–23:22 产生，日志末次写入 23:22）中**没有任何本会话痕迹** ⇒ 本轮**未向该目录写入**，因此「目录外写入入待批台账」一项为空。
- **可审计证据文件**（均在本目录）：`eval_results_{en,zh,jina}.json`（逐题结果）、`rank_probe_zh.json`（5 道未命中题真实排名 `{Q5:53,Q12:7,Q14:45,Q15:7,Q16:72}`）、`phase{3,4,6}.log`（重建与嵌入计数）、`grep_baseline.json`（两条 grep 基线）。
- `ls -lt memory/*.md | head -2`：最新 mtime 为 `2026-09-24 17:05`（早于本会话）⇒ `memory/` 权威树未被修改（只读挂载式索引，未移动/未改写任何 `.md`）；未新增任何 cron/launchd/定时任务。

---

## §7 待批事项（本轮均未执行）

1. **（建议：不采纳）** 把 Basic Memory 接成日常检索通道 —— 基于 §0 结论，不建议；若仍想接，最低成本方案是「只对**短文档**或**问题-答案沉淀**索引用」，而非整片 SOP 全库。
2. **（建议）** 若未来再评估同类工具，基线固定为 `grep_oracle`（人工锚点，16/16）而非 `grep_naive`，并**必须把「模型缓存 + 常驻服务」计入成本**。
3. **（记忆侧可选）** §5 的两条硬坑（`--project` 静默跳过、维度不一致→300s 超时）值得进 L2 的 BasicMemory 段落；因本轮结论为「不接」，是否记账待批。

---

## §8 残留与边界（诚实清单）

- 问答集 **16 题**为自建、规模小；结论在「中文口语提问 → 单篇 SOP」这一任务形态内有效，**不能外推**到代码检索、英文检索或长文摘要。
- 仅测了 **3 个 fastembed 模型**；理论上存在更匹配的模型/reranker 可提升，但 §4 的失败结构（目标 rank 45–72）表明**瓶颈是文档级整篇嵌入**，非模型选择，故未继续烧轮次。
- `jina-zh` 轮 768d 结果异常偏低（5/16），未深挖是模型口径还是 Basic Memory 适配问题（结论不依赖它）。
- 未测并发、未测增量更新一致性、未测删除/改名后的索引漂移。

---

## §9 复跑命令（自包含）

```bash
# 目录
cd ~/projects/GenericAgent/temp/bm_sem

# 隔离 HOME 启动服务（端口与真实实例区分）
HOME=$PWD/home basic-memory serve --port <隔离端口> &

# 指定中文模型 + 维度后重建（注意：不要加 --project）
HOME=$PWD/home basic-memory reindex --full

# 回查向量表必须 > 0（否则 reindex 被静默跳过）
sqlite3 "file:$PWD/home/.basic-memory/memory.db?mode=ro" \
  "select count(*) from search_vector_embeddings_rowids"

# 跑评测（16 题 × default/vector/hybrid）
python3 -u eval.py && mv eval_results.json eval_results_<tag>.json

# 结果
python3 -c "import json;r=json.load(open('eval_results_zh.json'));print(sum(x['bm_default_hit'] for x in r),'/16')"
```

**负控/自证**：`grep_naive` 与 `grep_oracle` 两条基线在同一问答集上分别给出 12/16 与 16/16（不同数字 ⇒ 题集与判据确实在区分，而非「一律判中」）。
