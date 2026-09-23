# R375｜temp/ 耐久耦合清账（6 个 enabled 定时任务）· TODO#55

生成时间：2026-09-24（本机）｜任务来源：`temp/TODO.txt` 第 55 行｜编号依据：`memory/autonomous_reports/history.txt`（编号权威，R375 已核未被占用）

## 0. 一句话结论

6 个任务里**只有 2 个真的吃 cwd**（`efficiency_monitor_daily`、`weekly_report_reminder`），其中 `weekly_report_reminder` 的 `../temp` 在任何常规 cwd 下都越界——今日 scheduler 的 cwd 就是 repo 根，**实测 rc=2**，属**正在踩的真 bug**；其余 4 个靠绝对路径 / 自带 `cd` 已锚定，但脚本与产物仍落在**被 .gitignore 整目录忽略**的 `temp/` 里 ⇒ 真正的耐久缺口是「**资产位置**」而不是「cwd 语义」。

修复口径已在沙盒**实测有效**（prompt 头部插一行 GA_ROOT 锚定 ⇒ 3 个 cwd 下落点一致、rc 全 0），但**生产 prompt 一行未改**（待批，见 §7 与台账 **C65**）。

## 1. 结论速览

| 任务 | 语义类 | 原样：3 个 cwd 的 rc / 落点 | 修复后 | 判定 |
|---|---|---|---|---|
| `brew_path_monitor` | C 裸相对 + 自带 `cd` | rc=0/0/0，落点 `./temp/brew_path_reports/<日期>.txt` **唯一** | 同左 | 已锚定（靠硬编码绝对路径 `cd`，换根不可移植） |
| `efficiency_monitor_daily` | A `./temp` + `./logs` | rc=0/0/**1**；落点 **3 种**：`./temp/efficiency_report_latest.json`（cwd=repo 根）、`./temp/temp/efficiency_report_latest.json`（cwd=repo/temp，**落点漂移**）、`''` 且 rc=1（cwd=elsewhere，`ModuleNotFoundError: No module named 'efficiency_monitor'`） | rc=0/0/0，落点唯一 | **真耦合**（漂移 + 失败面都在） |
| `eia_weekly` | D 硬编码绝对路径 | rc=0/0/0，无新建文件（只打印） | 同左 | 已锚定（不可移植） |
| `sqlite_snapshot` | D 硬编码绝对路径 | rc=0/0/0，无新建文件 | 同左 | 已锚定（不可移植） |
| `weekly_report_reminder` | B `../temp` | rc=**2**/0/0 —— **cwd=repo 根时失败**；另外两处"成功"属巧合（`../temp` 恰好命中） | rc=0/0/0 | **真 bug**（今日生产 cwd 即 repo 根） |
| `x_ai_intel_daily` | D 硬编码绝对路径 + `/tmp` 日志 | rc=0/0/0，落点唯一 | 同左 | 已锚定（不可移植；日志写在 `/tmp`） |

「落点唯一」= 3 个 cwd 下产物路径完全相同；「不可移植」= 路径写死 `/Users/cuip/projects/GenericAgent`，换根 / 换机即断。

## 2. 逐字证据：6 个任务 prompt 里的 temp 引用行

来源：会话内导出的 prompt 原文 `temp/coupling_lab/prompts_dump.txt`。**本报告以下小节内以反引号或直引号给出的命令/路径均为逐字摘录，未改写**。

- `brew_path_monitor`
  - `python3 temp/brew_path_checker.py > temp/brew_path_reports/$(date +%Y-%m-%d).txt 2>&1`
  - `cat temp/brew_path_reports/$(date +%Y-%m-%d).txt`
  - （同一 prompt 另有 `cd /Users/cuip/projects/GenericAgent`，这是它「三 cwd 一致」的原因）
- `efficiency_monitor_daily`：`import sys; sys.path.insert(0,'./temp'); from efficiency_monitor import EfficiencyMonitor; from pathlib import Path; m=EfficiencyMonitor(Path('./logs/autonomous_reflect.log')); r=m.parse_log(); print(r); m.save_report('./temp/efficiency_report_latest.json')`
- `eia_weekly`：`执行EIA能源价格摘要任务：运行 python /Users/cuip/projects/GenericAgent/temp/eia_supply_chain_digest.py`（后略）
- `sqlite_snapshot`：`1. 运行 python /Users/cuip/projects/GenericAgent/temp/sqlite_probe.py --snapshot 记录当前快照` / `2. 运行 python /Users/cuip/projects/GenericAgent/temp/sqlite_probe.py --diff 对比最近两次快照`
- `weekly_report_reminder`：`第1步必须先运行 python3 ../temp/get_week_range.py 获取确定性日期参数（git_since/git_until/display_range）`（后略）
- `x_ai_intel_daily`：`nohup python3 /Users/cuip/projects/GenericAgent/temp/x_ai_intel/x_intel.py > /tmp/x_ai_intel.log 2>&1 &`

**计数口径说明（防数字漂移）**：TODO#55 原文写的是「brew_path_monitor:6 / efficiency_monitor_daily:5 / eia_weekly:5 / sqlite_snapshot:5 / weekly_report_reminder:5 / x_ai_intel_daily:6」。本轮用可复跑命令按正则 `\.\./temp|\./temp|/temp/` 计得：brew=0、efficiency=2、eia=1、sqlite=2、weekly=2、x_intel=1（brew=0 是因为它写的是裸相对 `temp/...`）。**两套数字口径不同**（旧口径疑为宽匹配 `temp` 子串），本报告以逐字行为准，不引用旧计数作判据。

## 3. 现状事实（每条附可复跑命令）

1. **cwd 由 launchd 决定**：`plutil -p ~/Library/LaunchAgents/com.cuip.genericagent-scheduler.plist` ⇒ `WorkingDirectory => /Users/cuip/projects/GenericAgent`（`com.cuip.genericagent-autonomous.plist` 同值）⇒ 今天定时任务的 cwd **就是 repo 根**。
2. **派发链不改 cwd**：`grep -rn 'chdir' agentmain.py reflect/scheduler.py reflect/autonomous.py` ⇒ 无输出。
3. **temp/ 不入库**：`.gitignore` 第 1、2 行 = `temp/`、`tmp/`；`git check-ignore -v temp/TODO.txt` ⇒ `.gitignore:1:temp/	temp/TODO.txt`。⇒ 放进 `temp/` 的脚本与产物**不入 git**，任何一次 temp 清理即永久灭失。
4. **残留物证（A 类落点漂移确曾发生）**：`temp/temp/` 真实存在且含 60+ 项历史文件，其中 `temp/temp/efficiency_report_latest.json`（mtime 2026-09-23 09:43）——正是「cwd=repo/temp 时 `./temp` 被解释成 `temp/temp`」的产物。注意：该目录多数文件来自其它历史脚本，**本报告只用它证明该类漂移确实发生过，不逐文件归因**。
5. **`../temp` 的越界后果**：`~/projects/temp` **不存在**（`ls -d /Users/cuip/projects/temp` ⇒ NOT_EXIST）⇒ cwd=repo 根时 `../temp/get_week_range.py` 必然找不到（§5 有 rc=2 的 stderr 原文）。
6. **脚本侧同样吃 cwd**：`temp/efficiency_monitor.py` 内含 `Path('./sche_tasks/done')`、`Path('./temp/bridge_autonomous.py')` 与 `./logs`、`./temp/...` 等相对路径（详见 §7 L2）。
7. 全仓 `sche_tasks/*.json` 共 22 个，其中 **enabled=18**；本轮只清 TODO 指定的 6 个。

## 4. 演练装置（零副作用边界与保真处理）

- **沙盒**：`temp/coupling_lab/repo`（每次重建），所有命令在其中执行；`elsewhere` 作为第三 cwd 哨兵。
- **命令来源**：`prompts_dump.txt` 逐字抄录；唯一保真变换是**把原样命令里的 GA 绝对路径与真实 `/tmp/x_ai_intel.log` 替换为沙盒路径**（避免真写 `/tmp`、真碰生产目录），替换在 `run_audit2.py` 内一行完成并留档。
- **解释器分级**：只对**纯 stdlib、无网络无 DB 无推送**的两个脚本（`temp/efficiency_monitor.py`、`temp/get_week_range.py`）用**真解释器真跑**；其余 4 个任务（联网 / SQLite / 飞书推送）走 PATH 前置的**桩 python**，桩打印 `CWD=… ARGV=[…] STUB_OK` 或 `SCRIPT_NOT_FOUND=<路径>`，并落 `results/stub_calls.log`。
- **落点检测**：先写标记文件 `results/_marker`，跑完按 `mtime > 标记` 扫全树 ⇒ 只统计"本次真正新建/改写"的文件（旧版按目录集合差集会误报已存在文件，已弃用）。
- **修复版口径**：原样 body 不动，**只在 prompt 头部插一行**锚定：
  `: "${GA_ROOT:=/Users/cuip/projects/GenericAgent}"; export GA_ROOT; cd "$GA_ROOT" || exit 3`
  `efficiency_monitor_daily` 因 `sys.path` 与输出路径都相对 `./temp`，修复版另外把这两处改成 `$GA_ROOT` 绝对形式（其余 5 个任务只加锚定行）。
- **两处已知偏差与消解（诚实披露）**：
  1. 早前版本的 `efficiency_monitor_daily` 命令误用 `./logs/automated.log`（真 prompt 是 `./logs/autonomous_reflect.log`）⇒ **已改回逐字并整轮重跑**，矩阵结论不变（仅日志文件名差异，不改变 `./logs` 语义）。
  2. `x_ai_intel_daily` 修复版首跑 rc=1，经定位是**演练装置自身的字符串替换把 `$GA_ROOT/tmp/…` 误当作"要替换的日志名"改坏**，非真实缺陷；已修 harness 并重跑至 rc=0。

## 5. 演练矩阵：原样 vs 修复 × 3 个 cwd（全量，来自 `results/results2.json`）

rc 取值逐 cwd 顺序 = `repo 根` / `repo/temp` / `elsewhere`。

| 任务 | 口径 | rc | 本次新建文件（落点） |
|---|---|---|---|
| `brew_path_monitor` | orig | 0/0/0 | `./temp/brew_path_reports/2026-09-24.txt`（3 个 cwd 同一路径） |
| `brew_path_monitor` | fixed | 0/0/0 | 同上 |
| `efficiency_monitor_daily` | orig | 0/0/**1** | `./temp/__pycache__/efficiency_monitor.cpython-312.pyc` + `./temp/efficiency_report_latest.json`（repo 根）｜`./temp/temp/efficiency_report_latest.json`（repo/temp）｜无（elsewhere） |
| `efficiency_monitor_daily` | fixed | 0/0/0 | `./temp/efficiency_report_latest.json`（3 个 cwd 同一路径） |
| `eia_weekly` | orig / fixed | 0/0/0 | 无（仅打印；桩解释器） |
| `sqlite_snapshot` | orig / fixed | 0/0/0 | 无（仅打印；桩解释器） |
| `weekly_report_reminder` | orig | **2**/0/0 | 无（repo 根下直接失败） |
| `weekly_report_reminder` | fixed | 0/0/0 | 无（真解释器真跑，仅打印日期参数） |
| `x_ai_intel_daily` | orig / fixed | 0/0/0 | `./tmp/x.log`（orig 与 fixed 的日志名差异来自 §4 的沙盒替换，非语义差异） |

**两处失败面的 stderr 原文**（逐字）：

- `efficiency_monitor_daily` 在 cwd=elsewhere：`ModuleNotFoundError: No module named 'efficiency_monitor'`
- `weekly_report_reminder` 在 cwd=repo 根：`python3: can't open file '/Users/cuip/projects/GenericAgent/temp/coupling_lab/repo/../temp/get_week_range.py': [Errno 2] No such file or directory`

判读：`../temp` 在 cwd=repo 根时越过 repo 到 `~/projects/temp`（不存在）；在 cwd=elsewhere 与 cwd=repo/temp 时"碰巧"命中 ⇒ **同一个 prompt 的成败完全取决于执行者 cwd**，这就是「耦合」的可观测形态。

## 6. 负控：把 `temp/` 移走 ⇒ 应失败；复原 ⇒ 应通过

做法：整目录移走沙盒里的 `temp/`，再跑**修复版**（最有利于"已修好"的一侧），随后复原重跑。

| 任务 | 移走 temp/ 后 | 复原后 | 报错原文（移走后） |
|---|---|---|---|
| `brew_path_monitor` | **rc=1** | rc=0，落点正确 | `bash: 行 1: temp/brew_path_reports/2026-09-24.txt: No such file or directory` |
| `efficiency_monitor_daily` | **rc=1** | rc=0，落点正确 | `ModuleNotFoundError: No module named 'efficiency_monitor'` |
| `weekly_report_reminder` | **rc=2** | rc=0 | `python3: can't open file '…/repo/temp/get_week_range.py': [Errno 2] No such file or directory` |

负控结论（重要，别读偏）：**cwd 锚定修不了这一层**。三条 `rc≠0` 证明的是「脚本与产物本身住在 `temp/` 里」这一层耦合——`temp/` 一旦被清理（它是 gitignored，清理是常规操作），任务必挂。所以「耐久耦合清账」必须包含**资产迁出 temp/**，而不能只改 prompt 路径写法。

**未能真跑的范围（显式标注）**：`eia_weekly`（联网取 EIA 数据）、`sqlite_snapshot`（读写用户 SQLite 快照）、`x_ai_intel_daily`（发飞书推送）三个任务在两次演练中一律走桩解释器 ⇒ 对它们，本轮只证明了「命令能解析、落点随 cwd 稳定、无 temp 相对引用」这一层，**没有**证明它们的业务行为。负控对这三者不适用（桩不读脚本）。

## 7. 修复提案（分三级）+ 与台账 C19/C23/C32 合并为 C65

- **L1（最小改动、零语义变化，已实测有效）**：6 个任务的 prompt 头部各插一行
  `: "${GA_ROOT:=/Users/cuip/projects/GenericAgent}"; export GA_ROOT; cd "$GA_ROOT" || exit 3`
`brew_path_monitor` 原有的 `cd` 行可保留（幂等）。实测：`efficiency_monitor_daily`、`weekly_report_reminder` 由「漂移 / 失败」变为 3 个 cwd 落点一致且 rc=0。
- **L2（自定位，改脚本）**：`temp/efficiency_monitor.py` 把 `./logs`、`./temp`、`./sche_tasks/done` 改为由 `Path(__file__).resolve().parent.parent` 派生的仓库根拼接（同仓 `x_intel.py` / `sqlite_probe.py` 已是这种写法）。
- **L3（耐久，= C19/C23/C32 的本体）**：`temp/*.py` 迁入 `tools/`（入仓、受 git 跟踪），产物落仓库跟踪目录或 `output/`；这样 `temp/` 被清也不影响任务。

**台账动作**：在 `temp/pending_decisions/DECISIONS.md` 追加 **C65「temp/ 耐久耦合清账（合并 C19/C23/C32）」**，把上述三级修复 + 本轮演练证据（含负控 3/3）挂在一条上，并注明 C19/C23/C32 由本条**统一承接**（不另起一套）。

## 8. 本轮未做（待批；生产零改动）

1. `sche_tasks/*.json` 的 6 个 prompt **一行未改**（L1 只做了沙盒验证）。
2. `temp/efficiency_monitor.py` 等脚本**未改**（L2 只给出口径）。
3. `temp/*.py` **未迁仓**（L3 属于 C19/C23/C32 的待批范围）。
4. 台账条目 C65 为**新增登记**；条目所载动作仍需你批准后才执行。
5. 观察（非本任务范围、仅记录）：待批台账自身也住在 gitignored 的 `temp/pending_decisions/` 下，与 C51 家族同源；本轮不改。

## 9. 证据清单（可复跑）

| 证据 | 位置 / 命令 |
|---|---|
| 演练脚本（含逐字命令、保真替换、负控） | `temp/coupling_lab/run_audit2.py` |
| 演练结果 JSON（runs / verdict / notemp / restored） | `temp/coupling_lab/results/results2.json` |
| 桩调用记录（CWD/ARGV/命中脚本） | `temp/coupling_lab/results/stub_calls.log` |
| 6 任务 prompt 原文 | `temp/coupling_lab/prompts_dump.txt` |
| 中间取证（含脚本级相对路径清单） | `temp/coupling_lab/FINDINGS_interim.md` |
| cwd 权威来源 | `plutil -p ~/Library/LaunchAgents/com.cuip.genericagent-scheduler.plist` |
| 忽略规则 | `git check-ignore -v temp/TODO.txt` |
| 漂移物证 | `ls -la temp/temp/efficiency_report_latest.json` |

## 10. 记忆更新建议（待批，本轮未写 L1/L2）

拟在 L1「定时/自主/反射」段追加一句（约 130 字，刻意保持极简）：

> temp/ 耐久耦合清账：6 个 enabled 任务中仅 2 个真吃 cwd——weekly_report_reminder 的 `../temp` 在 cwd=repo 根（launchd WorkingDirectory 实值）下必失败 rc=2，efficiency_monitor_daily 的 `./temp` 在 cwd=repo/temp 时写进 temp/temp/ 落点漂移；prompt 头插 `GA_ROOT` 锚定行可修（沙盒 3 cwd 实测 rc=0 落点一致），但「脚本/产物住在 gitignored 的 temp/」才是耐久缺口（负控：移走 temp/ 三条命令 3/3 失败）⇒ 合并为台账 C65，承接 C19/C23/C32；详见 R375。
