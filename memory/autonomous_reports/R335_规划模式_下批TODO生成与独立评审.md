# R335 规划模式：下批 TODO 生成 与 独立评审

- 触发：用户离开 >30min，自主反射器唤醒；`temp/TODO.txt` 唯一待执行项 =「生成下一批TODO（规划模式）」
- 执行边界：本轮**只做规划，不执行任何任务**（task_planning.md 明令）
- 产出：`temp/TODO.txt`（5 条任务 + 1 条常驻规划种子项），本报告

---

## 一、意外发现：自主报告库整体丢失（真实数据事故）

盘点阶段发现 `temp/autonomous_reports/` **不存在**：

- 全盘搜索 `R\d+_*.md` → **0 个文件**；`autonomous_reports` 目录在 `~`（修剪 Library）内也是 0 个
- `git check-ignore` 证实 `temp/` 在 `.gitignore`（第 1 行）→ **git 无法恢复**
- 历史记录同样丢失：`memory/autonomous_history.txt` 只有 2026-05-02 的 R256 一条；真实列表是 `temp/autonomous_reports/history.txt`（随目录一起消失）

### 抢救：history 已重建

以 `logs/autonomous_reflect.log`（11MB / 205k 行，日志中含历史 `complete_task` 的 prepend 原文 + 历史会话转录）为源，正则重建：

| 项 | 结果 |
|---|---|
| 唯一 R 编号 | **303 条**（R1–R334） |
| 缺号 | 31 个（27-30/74/78/79/112-114/164/175/177/181/182/231/234-236/261/263/265/287-290/325-329），多为当时的规划占位号或未落地项 |
| 首行 / 末行 | R334（2026-05-11）/ R1（2026-04-23） |
| 落盘 | `temp/autonomous_reports/history.txt`（59KB，303 行） |

### 报告正文可恢复性（实测，非推断）

| 恢复源 | 实测结果 | 判定 |
|---|---|---|
| `memory/L4_raw_sessions/2026-05.zip` | 115 文件；**375 处** `R3xx_*.md` 文件名引用；**332 处** `## 记忆更新建议` 报告签名 | ★最强源 |
| `memory/L4_raw_sessions/2026-04.zip` | 84 会话；含 `file_write/patch` 完整 content | ★强源 |
| 两个 zip 合计 | **324 个 R 编号**以文件名出现；history 303 条中 **293 条命中** | 可恢复率 ≈ 97% |
| `logs/autonomous_reflect.log` | 仅 36 处「记忆更新建议」/110 处 `#R` | 次级源 |
| `temp/model_responses/*.txt` | 全是 9 月文件（报告期是 04-23~05-11），仅 1 个文件 28 处命中 | **基本无关（原候选TODO写错了源，评审纠正）** |

→ 结论：报告正文**大部分可从会话归档抢救回来**。评审已据此改写 TODO 第 1 项（含"落到受版本控制目录 + 落盘即入库钩子"以断根因：写回 temp/ 会再丢）。

---

## 二、历史低价值模式（批判性阅读结论）

1. **"utils 工厂"期（R280–R315）**：连续十余条全是「把某库封装成 `xxx_utils.py` + 浅层自测通过」。边际递减、无真实使用场景驱动，属"容易完成但收益低"。
2. **同源个人数据反复切片**（R317/321/322/323/330/331/332）：Chrome 历史 / zsh 历史 / 有道云 / cloudpic 被反复重新分析，结论互相交叉验证但**无新假设**。
3. **本次提交的候选里也复现了该病**：6 条中 3 条在"消费旧报告"（R333 建议、R334 基线）。评审据此建议新增一条"面向未来"的记忆欠账项。
4. 有价值的长青型任务特征：绑定**在跑的生产链路**或**自身效能/数据资产**，能复利（如 trendradar 定时链、周报链、ROI 监控、报告归档）。

---

## 三、独立评审（subagent，禁自评审；真机 23 轮核验）

调用：`python agentmain.py --func <abs>/temp/review_todos.txt --nobg`（pid 63610，输出 `temp/review_todos.out.txt`，含 `[ROUND END]`）。
评审员**未被喂任何结论**，被要求自行读记忆库 + task_planning.md 判断，并且逐条真机核验前提。

| # | 候选任务 | 分 | 关键核验 / 处置 |
|---|---|---|---|
| 1 | 自主报告归档抢救 | **8** | 唯一"修根因"任务；但**题面恢复源选错**（真源= L4 zip）→ 改源 + 重写验收 + 加"入受控目录" |
| 2 | roi_monitor 产出量告警修复 | **7** | 前提逐行证真：`metrics.avg_lines_per_task` 因快照 schema 换代恒 None；心跳类占 71.5% → 保留，但要求**先定义口径**（防造不可比指标） |
| 3 | LaunchAgents 整改落地 | **4** | **前提过期**（R333 是 2026-05 的：现在 27 个 plist、2 个错误项 cloudcli/anyrouter，非"1 个"）；且"错误项=0"需改 `~/Library/LaunchAgents` + load/unload = SOP 明列**需批准**，而 autonomous/scheduler/price-alert 自身就在这批 plist 里 → **删除** |
| 4 | 反射器语义审计 | **4** | 要"验证"的两条结论（无 TODO→ `check()` 返 None；无 idle 判断）**已被 SOP 作为同日实测事实写死**，重做属禁止的重复探索；死锁是**有意设计**（0 成本 + 留种子） → **删除**（如需推进 idle 判断应升格为用户决策提案） |
| 5 | bar-smyze 冻结仓库体检 | **6** | 实测 11 个 git 子工程；R334"其余 9 仓冻结"**已被推翻**（opserver 39 分钟前有提交等）→ 保留，但改为**可证伪假设 H1**（dsh/trellis 注入层 + release-marshal/CCB 驱动复活），只读约束 |
| 6 | 小众工具挖掘 | **6** | 同型已做 4 次（R7/R44/R249/R63），方向过宽=无假设 → 收窄到**唯一能力缺口：本地语音转写（记忆库零"音频输入"节点，已有 vision/ocr/ffmpeg）** |
| 7 | 记忆更新欠账清单（评审新增） | — | 从 332 处 `## 记忆更新建议` 与现有 memory 比对，找"被丢弃的建议"；与 #1 协同，直击"核心产出是记忆" |

最终 `TODO.txt` = **保留 4 条（8/7/6/6，均已按评审改写验收）+ 新增 1 条 + 常驻规划种子项**，删除 #3/#4。

---

## 四、边界与未做事项

- 本轮**未执行任何 TODO**（规划模式约束）；未修改任何 GA 源码、未改 `~/Library/LaunchAgents`、未动任何业务仓库。
- 本轮已写的文件：`temp/TODO.txt`（重写）、`temp/autonomous_reports/history.txt`（重建，303 条）、本报告、`temp/review_todos.txt|.out.txt|.log`（评审留痕）。
- 未解决残差：报告正文**尚未真正恢复**（只证明了可恢复）——已作为新 TODO 第 1 项；`temp/autonomous_reports/` 仍是"唯一副本 + 无版本控制"状态，事故可复发。

---

## 五、装置级经验（可复用）

1. **`agentmain.py --func` 的 prompt 路径必须用绝对路径**：子进程 cwd = `script_dir`（GA 根），而 agent 的文件工具 cwd = `temp/`，用相对路径会 FileNotFound（本次踩坑 1 次）。
2. **后台起 subagent 用 `bash -lc` 会挂住**（子进程组未完全脱离，工具 60s 超时被杀）；改用绝对路径 + `> log 2>&1 &` 一次成功。判断完成看输出文件是否含 `[ROUND END]`，进程数用 `ps aux | grep -c '[a]gentmain.py --func'`。
3. **`temp/` 无版本控制**，任何"值得留存的产物"都不该只落这里。

---

## 记忆更新建议（待用户批准后写入）

1. **[L1 索引] 自主/反射 段补充**：活跃 TODO = `temp/TODO.txt`；**历史与报告库曾于 2026-05~09 之间整体丢失**，`temp/autonomous_reports/history.txt` 已从 `logs/autonomous_reflect.log` 重建（303 条，R1–R334，缺号 31 个）；**灾备恢复源 = `memory/L4_raw_sessions/YYYY-MM.zip`（含完整 tool_use 与报告正文，可恢复率≈97%）**，次源 `logs/autonomous_reflect.log`。
2. **[L1 索引] 自主/反射 段修正**：`roi_monitor` 的 `avg_lines_per_task` 恒 `null` 的**根因** = 快照 `schema_version` 换代（`task_volume_v1` 不再含该字段），非"快照停更"；`<100行/任务` 告警因此**永不触发**（待修）。
3. **[L3] `subagent.md` 增补**：`--func` 模式 prompt 必须绝对路径（子进程 cwd=`script_dir`，agent 文件工具 cwd=`temp/`）；后台启动方式与"看 `[ROUND END]` 判完成"。
4. **[L2] smyze/bar 代码库架构 已过期**：现"其余 9 仓冻结"不成立（11 个子工程中 8 个在 09-11~09-21 有提交），待新 TODO 的归因结论落地后替换。
5. **[L3] 新增提案（暂缓）**：`L4_raw_sessions` 已被证实是"报告/会话级灾备"，建议在 SOP 中把"重要产物同时入 L4 式归档"写成硬约束（本次 TODO 第 1 项落地后一并写入）。
