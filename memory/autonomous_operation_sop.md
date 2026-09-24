# 自主行动 SOP

⚠️ **路径警告**：autonomous_reports 在 temp/ 下，用`./autonomous_reports/`访问，**不是**`../memory/autonomous_reports/`或`../autonomous_reports/`！TODO在cwd下。
报告存于 `./autonomous_reports/`，文件名 `RXX_简短描述.md`（XX从 history.txt 推断自增）。

授权你进行自主行动，只要不对环境造成副作用都可进行。

## 触发机制（2026-09-22 实测核实，勿凭印象）
- **入口1**：launchd `com.cuip.genericagent-autonomous.plist` → `agentmain.py --reflect reflect/autonomous.py`，常驻，`INTERVAL=1800s` 检查一次。
- **入口2**：GUI 桌面聊天面板开关（`qtapp.py`，默认 OFF），idle>30min 且距上次触发>120s 才注入。
- **守卫（关键）**：`reflect/autonomous.py::check()` 先读 `temp/TODO.txt`，**没有 `[ ]` 未完成项 → 返回 None，完全不调用 LLM（0 token）**。有 → 注入 `[AUTO]🤖...` 任务，这才起一轮 agent（开烧）。
- ⚠️ **死锁坑**：守卫刻意"无TODO不调用"，而规划模式又只在"无TODO"时进入 ⇒ **TODO.txt 必须至少留 1 条 `[ ]` 种子项，否则规划流程永远进不去，自主模式静默永不触发**。规划完成后应保留下一条种子，不要清空。
- ⚠️ **反射器无 idle 判断**：只要 TODO 有 `[ ]`，就按 INTERVAL 持续触发（跑完一轮 → sleep 1800 → 再触发），**与"用户是否离开"无关**；prompt 里"用户已经离开超过30分钟"是文案，不是事实。要真"仅离开时触发"需改源码加 idle 判断。
- **停止开关**：① 把 `temp/TODO.txt` 里的 `[ ]` 全标成 `[x]` 或清空（下一轮守卫即静默）；② `launchctl unload ~/Library/LaunchAgents/com.cuip.genericagent-autonomous.plist`。
- **活跃TODO唯一事实源 = `temp/TODO.txt`**（守卫与 `helper.set_todo()` 同指此处；`memory/autonomous_operation_sop/TODO.txt` 是历史快照，勿再用）。
- 定时任务是**另一套**（`com.cuip.genericagent-scheduler.plist` + `reflect/scheduler.py`），**与自主开关无关，始终在跑**，且每次触发都是一轮完整 agent（如 `folder_watcher`/`price_alert` 曾各 23 轮/天，2026-09-22 folder_watcher 已降为 every_4h）。

## 启动（第一步）
- update_working_checkpoint: `自主行动｜收尾时重读SOP | from autonomous_operation_sop.helper import *; set_todo()/complete_task(tasktitle, historyline, report_path)`

第二步：
```python
from autonomous_operation_sop.helper import *
print(get_history(40))  # 了解历史避免重复
print(get_todo())       # 查看待办
```

## 任务选择
- 有未完成条目 → 取**一条**，直接进入执行，其他条目下次执行
- 无 TODO → 读 `autonomous_operation_sop/task_planning.md` 规划，下次执行
- 不连续两次选相同子任务
- 价值公式：**「AI训练数据无法覆盖」×「对未来协作有持久收益」**

## 执行
- 选定任务后 update_working_checkpoint，将选中的TODO条目和执行注意事项 append 到 checkpoint 中
- 调用code_run准备结尾回调, script为handler._done_hooks.append("重读自主任务sop，检查你刚刚的收尾工作是否正确，不正确则改正")，inline_eval=True （秘密参数）
- 小步快跑，边探测边实验
- 用临时脚本验证假设；禁只读即下结论，完整验证再写报告
- 即使失败也记录实验过程和结果，失败报告同样有价值
- 用户不在线，遇到需要决策的问题写入报告待审，不要卡住

**收尾（4件事缺一不可）**：
1. 重读本sop
2. 在cwd写报告（文件名任意），若有记忆更新建议，附在报告末尾
3. `from/import helper; complete_task(tasktitle, historyline, report_path)` → 自动编号+移报告到 autonomous_reports/+prepend history（historyline 格式：`类型 | 主题 | 结论`，严格单行）
4. `set_todo()` 获取TODO路径 → 将已完成条目标记为 `[x]`（注意前缀）
5. 结束，剩余TODO留到下次再做

## 权限边界
- 无需批准：只读探测、cwd内写操作/脚本实验
- 需写入报告待审：修改 global_mem / memory下SOP、安装软件、外部API调用、删除非临时文件
- 绝对禁止：读取密钥、修改核心代码库、不可逆危险操作

## 等待用户审查
- 用户归来后审查报告，决定批准、修改或拒绝方案

## 坑：同一 TODO 会被**并行会话重复执行**（2026-09-22 实证 R353/R354）
同一 TODO 条目在 18:09 已被另一运行归档为 R353，本运行 18:29 又独立跑了一遍（ASR 全量 ~29min 算力双耗）并把编号推到 R354。
- 收尾前**先查重**：`ls -t temp/autonomous_reports | head -5` + `grep -n '<任务关键词>' memory/autonomous_reports/INDEX.md temp/TODO.txt`；若条目已 `[x]`，直接改 `[x] R<编号>` 并放弃本轮产出（或合并差异），不要再调 `complete_task`。
- `helper.complete_task` 的编号分配非原子（`_next_report_number()` 扫目录），并发下会撞号 ⇒ 与 TODO「自主报告『编号复用』根治」合并规划（认领锁 `temp/.todo_claim.json` + `O_EXCL` 建目标文件）。

## 收尾实测坑（2026-09-23 R365 实操复核）
- **TODO 未完成条目行首无前导空格**：`temp/TODO.txt` 里是 `[ ] 产出 | …` 直接顶格 ⇒ 用 `startswith(' [ ]')` 过滤会**一条都匹配不到**（本轮标记失败一次）；改用 `l.startswith('[ ]')` 或 `grep -n '^\[ \]'`。
- **`complete_task` 会移动并重新编号报告**：它把 `report_path` 移入 `autonomous_reports/R{XX}_{taskname}.md`，编号由 `_next_report_number()` 扫目录取 ⇒ 若报告已按 `R365_*.md` 命名放在该目录，编号会**自增**（实测会变 R366）。做法：报告先写在别处（cwd/临时名），再调 `complete_task`。
- **`complete_task` 自带 IM 推送**（飞书 + 钉钉各 1 条）⇒ 报告里禁写「未发任何通知」这类绝对表述，须写明「未新增/未实现推送逻辑，收尾推送为工具既有行为」。
- **报告入库灾备（既有工具，收尾可跑）**：`python3 tools/autonomous_reports_archive.py sync --apply`（`temp/autonomous_reports` → `memory/autonomous_reports` 增量、逐字）+ `inventory --apply`（重生成 `INDEX.md`/`history.txt`）⇒ **`memory/autonomous_reports/` 才是受 git 跟踪的 durable 归档**（`temp/` 被 `.gitignore` 忽略），`INDEX.md` 行号≈报告编号；两个子命令**不带 `--apply` 即为零副作用预览**，先预览再落盘。
  - **两个子命令分工别搞混（2026-09-25 R390 实测+源码复核）**：`sync --apply` **只搬报告 `.md`，不搬 `history.txt`**；`archive/history.txt`（=`memory/autonomous_reports/history.txt`）只有 **`inventory --apply`** 会写（源码 `cmd_inventory` 末尾把 live history 原样拷入，并另存 `history.md`）⇒ 收尾只跑 sync 时，archive 索引会**落后新报告整条**（实测落后 R390 一条）。
  - **只想对齐 history 时的安全做法**：手动前插一行更省事——live 与 archive 的 history 均为**降序**（新条目在**首行**）；`inventory --apply` 除写 history 外还会顺带按 live history 主题**重命名**归档文件（即 C51 待批风险，实测曾把 8 份定稿报告改名），故不想触发重命名就别跑它；补完 `git add` 该**单文件**提交。

