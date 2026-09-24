# R390 规划模式·第9批 TODO 生成与独立评审

任务来源：`temp/TODO.txt` 常驻种子项「生成下一批TODO（规划模式）」。本轮**只做规划**，不执行任何 TODO 项；产出为新的 TODO 批次 + 两份独立 subagent 评审记录。

## 1. 流程与硬要求（依据 memory/autonomous_operation_sop/task_planning.md）
- 步骤 0：`update_working_checkpoint` 记录「规划模式：产出TODO后必须经过subagent评审，不允许自评审，禁止执行任何TODO」✅
- 步骤 1–3：批判性读 `temp/autonomous_reports/history.txt`（341 条）、盘点报告与记忆，识别低价值模式（浅层验证/无假设巡检/重复探索/泛采集/知名工具基础用法）✅
- 步骤 4–5：产出 7 条候选，格式 `[ ] 类型 | 一句话目标 | 验收标准` ✅
- 步骤 6–7：**召唤 subagent 评审（不得自评审）**；召唤失败重试 ✅（见 §3）
- 步骤 8：读评分，低分删除/替换 ✅（见 §4）
- 步骤 9：写入 TODO.txt 后**立刻结束** ✅

## 2. 候选与现状取证（数字均为本轮实测，命令可复跑）
| 候选 | 关键现状（实测） |
|---|---|
| C1 kc 快照断链修复+守卫 | `temp/kc_snapshots` 仅 `09-22`/`09-23` 两目录，各含 knowledgeC.db 13,119,488 B（12.5 MiB）+ manifest；`reflect_scripts/08_kc_snapshot.py` 头部自述「反射器版·待批准接入」；launchd 无 kc plist |
| C2 活跃日志膨胀治理 | `logs/` ≈1.7 GB；launch_gui.log 1,211,565,564 B mtime Jun 4（无 plist 引用=孤儿）；feishu_agent.log 446,529,005 B 仍写；scheduler_reflect.log 83,859,132 B / 1,334,380 行 |
| C3 密钥/令牌泄漏体检 | `grep -ciE "密钥体检|泄漏体检|secret scan" history.txt` = **0**（无先例）；`temp/` 被 `.gitignore:1` 整目录排除且 `du -sh temp/` = **5.6 GB**（bm_sem 1.1G / audio_assets 468M / evidence 434M / bm_probe 406M / multica_research 358M / loopx_research 269M）；`memory/L4_raw_sessions` 6 个 zip 共 127M |
| C4 WatchPaths 替代轮询 | 候选假设「反射器是常量轮询，可迁移为事件触发」 |
| C5 kc 快照首次消费 | 快照仅 2 天 vs 源库约 4 周窗口（08-28→09-25 逐日 52–1,358 条；17364 对象；最新 ZCREATIONDATE 本地 2026-09-25 01:54） |
| C6 自主任务价值闭环校准 | 目标=预测 vs 实际的元-元评估 |
| C7 冲浪·shell 历史检索 | 点名 atuin |

## 3. 评审召唤记录（关键：槽位可用性）
- 配方照抄上批（R383）：`Popen([sys.executable,'agentmain.py','--func','temp/review_todosN.txt','--llm_no',N], cwd=REPO, start_new_session=True)`，父进程瞬间退出（自孵化子进程），判活用 `pgrep -fl review_todos`。
- 实例 `llm_no 1 / 2 / 3` 三路并发：**llm_no 1、3 因模型无权限失败**（HTTP 403，非本轮可控）；llm_no 5/6 试跑同样 403。
- 换用实测可用的 **llm_no 0 与 llm_no 4** 重跑另两路（`temp/review_todos13.out.txt`、`temp/review_todos14.out.txt`），输入同为 `temp/review_todos10.txt`（7 候选 + 现状）。
- 结论：**两份独立评审在窗口内回收**（llm_no 2 → `temp/review_todos11.out.txt`；llm_no 0 → `temp/review_todos13.out.txt` + 自建 `temp/todo_candidate_scoring.md`）。第 3 路（llm_no 4）在窗口内仍在运行（其产物尾部停在 `LLM Running (Turn 11)`），**未纳入评分**，属预算边界而非失败。

## 4. 逐条评分与处置（数字直接回源核对，禁凭印象）
| 候选 | 评审A(llm_no 2) | 评审B(llm_no 0) | 处置 |
|---|---|---|---|
| C1 kc 快照断链+守卫 | **7** | 4 | 保留（与 C5 合并为两阶段条目） |
| C2 活跃日志治理 | 4 | 6 | 保留但**限定范围**：先做根因修复 + 执行 R359 已算净收益方案 |
| C3 密钥/令牌泄漏体检 | **8** | 7 | **保留（本批最高）** |
| C4 WatchPaths 替代轮询 | 5 | 4 | **淘汰**（前提错位） |
| C5 kc 首次消费：行为底稿 | 6 | 5 | 保留（并入 3 的阶段2） |
| C6 价值闭环校准 | 3 | 4 | **淘汰** |
| C7 冲浪 | 5 | 3 | 保留但**换真冷门话题**（禁 atuin） |

### 4.1 淘汰理由（需可核对，非口味）
- **C4 前提错位**：反射器并非「常量轮询」可迁移对象——它是 launchd `KeepAlive` 常驻脚本内的 `sleep` 循环，工作方式是先 check 再 sleep；没有「轮询常量」可迁到 `WatchPaths`，且官方 `docs/reflect_best_practices.md` 明确「轮询本身零消耗」。候选「轮询成本」论据被证伪。
- **C6 元-元分析**：n=13 无统计力；产出的校准结论**没有消费者**（无流程会读它并改变行为），与 P1 低价值模式（自我感动式产出）同源。评审 A 给 3/10、评审 B 给 4/10。

### 4.2 两评审的独立共识
两位评审各自指出同一结构性事实：**C1/C2/C5/C6 全踩「已做过但待批」的地基**（R357/R359/R371/R373 的分析与工具已在盘，卡在用户批准），台账未决 87 条、最老仅 2 天 ⇒ 瓶颈是**用户注意力**而非检索或能力。故衍生出第 5 条 TODO：**待批项「一页决策包」**（未单独评分，标注为衍生项）。

## 5. 已落盘产物
1. `temp/TODO.txt` — 表头更新为第9批（含 4 保留 + 1 衍生 + 2 淘汰及理由），「## 待执行任务」插入 5 条新 `[ ]`（8 项未完成 = 5 新 + 3 旧保留：helper 补丁、规划种子项、待批台账第2批）。
2. `temp/todo_batch9_draft.md` — 条目草稿（含完整验收标准）。
3. `temp/plan_v10_evidence.md` — 现状取证与两份评审评分留档。
4. 评审原文：`temp/review_todos11.out.txt`、`temp/review_todos13.out.txt`、`temp/todo_candidate_scoring.md`、输入 `temp/review_todos10.txt`（第 4 路 `temp/review_todos14.out.txt` 未完成，留档）。

## 6. 边界与残差（如实声明）
- 本报告**未执行任何 TODO 项**，未改动任何源码/生产脚本；`temp/TODO.txt` 是唯一被写入的活跃配置。
- 第 3 路评审（llm_no 4）超窗口未回；若需第三条独立意见，可下一轮直接读 `temp/review_todos14.out.txt`。
- 数字口径均为 `du -sh` / `ls -l` / `stat` 当场输出（temp 5.6G、logs 1.7G、scheduler_reflect.log 1,334,380 行、launch_gui.log 1,211,565,564 B 且 mtime 2026-06-04 17:07）；其中 launch_gui.log 自 Jun 4 起无增长（孤儿），治理动作中「删除/截断」属破坏性操作，已写入验收要求：**须用户一句批准**。
- C1/C5 的「约 4 周窗口」来自既有的 knowledgeC 滚动删除实测（memory 已记），本轮仅复核到「最新对象时间 = 本地 2026-09-25 01:54」。
