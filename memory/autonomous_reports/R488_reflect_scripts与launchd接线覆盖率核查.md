# R488 reflect_scripts × launchd 引用覆盖率核查（TODO L196 / 第22批评审提名替换项）

- 报告编号：R488（双侧 history.txt max 实测均为 R487 ⇒ 本报告取 R488）
- 生成时间：2026-09-28
- 上游出处：TODO L196 —— 【第22批·评审提名替换项（原 C6=4/3/6=4.33 三路一致最低：codex 判「待批项周边、应删除」、槽3 主张删、槽4 判「为待批做支持材料」）】reflect_scripts × launchd 引用覆盖率核查
- 产物：`tools/reflect_wiring_check.py`（39567 B，sha256 前 16 = `dec0083b573fbab8`）+ 声明面 `tools/reflect_wiring_registry.json`（5834 B，sha256 前 16 = `242724931427d301`）
- 证据：`temp/reflect_wiring/scan_2026-09-28.txt`（scan 原始输出 4576 B）+ `temp/reflect_wiring/scan_2026-09-28.json`（机器可读 23283 B）

## 1. 背景（为什么做）

R479 在人工排查中发现一个同形缺口：`reflect_scripts/09_task_freshness.py` **没有任何 launchd 引用** ⇒ 巡检生命周期台账从未被调度、7 行数据全出自人工真跑。该结论只覆盖 09 一个脚本、靠人眼看出来，**没有机械全量判据**。

本任务把那句人工观察升级为可复跑的只读对账器：枚举 `reflect/` 与 `reflect_scripts/` 下全部脚本，逐条判定「到底有没有接线」，并把「明知未接线但属预期」的脚本登记成**带证据的声明**（fail-closed：声明缺证据 token 即 rc=2），使「未接线」这件事不再依赖记忆里的一句话。

## 2. 机制前提（先验事实，非假设）

调度入口是 `agentmain.py --reflect <单个脚本路径>`，**一次一条**，没有目录扫描、没有清单文件：

- `~/Library/LaunchAgents/com.cuip.genericagent-l2-gate.plist` 的 `ProgramArguments` 实测为 `["/opt/anaconda3/bin/python3", "-u", "…/agentmain.py", "--reflect", "reflect_scripts/07_l2_gate.py"]`（`plutil -p` 原文，`WorkingDirectory=/Users/cuip/projects/GenericAgent`，`RunAtLoad=1`、`KeepAlive=1`）。
- 因此「覆盖率」不可能靠目录约定推出，**只能逐个脚本对账**——这正是本器存在的理由。

## 3. 判据（四层接线面 + 一层声明）

| 层 | 判据 | 说明 |
|----|------|------|
| W1 launchd | plist 里 `--reflect` 的**相邻 token**（含兄弟路径形态）指向该脚本 | 只认相邻；注释键里出现路径不算 |
| W2 sche_tasks | 在册（enabled）定时条目的 prompt 明确引用该脚本 | 已停用条目（`sche_tasks/disabled/`）不算 |
| W3 间接 | 生产代码里引用该脚本（如 `memory/checklist_helper.py` 引 `reflect/*.py`） | 走 `_W_RE`/`_M_RE` 常量对 |
| W4 文档 | `docs/`、记忆里的说明 | **只作旁证，单凭文档提及不判接线** |
| 声明面 | `tools/reflect_wiring_registry.json`：每条含 id / status / reason / evidence[]（文件存在 + token 逐字命中） | 缺 token、未知 status、陈旧条目、重复 id 一律 rc=2 |

status 取值域：`pending_approval` / `example_template` / `manual_on_demand` / `retired`（外加结构化的 WIRED_LAUNCHD / WIRED_SCHE 等实测态）。

## 4. 真跑实测（2026-09-28，`scan` rc=0）

```
-- 汇总: wired=4 declared=10 orphan=0 覆盖率=28.6% 缺口=0 注册表问题=0
-- 只读自证: files=88 changed=[]
-- rc=0
```

- 脚本面：19 个（场景 14 + helper 5）。
- **真接线 = 4 条**：`reflect/autonomous.py`（`com.cuip.genericagent-autonomous.plist`）、`reflect/scheduler.py`（`com.cuip.genericagent-scheduler.plist`）、`reflect_scripts/06_price_alert.py`（`com.cuip.genericagent-price-alert.plist`）、`reflect_scripts/07_l2_gate.py`（`com.cuip.genericagent-l2-gate.plist`）。四条 plist 均 `exists=True loaded=True`。
- LaunchAgents 目录实测 29 个 plist，其中带 `--reflect` 的**只有 4 个**。
- sche_tasks 在册 25 条（启用 17）：无一条引用场景脚本，唯一脚本引用是 `reflect_scripts/reflect_common.py`；`reflect_scripts/06_price_alert.py` 只出现在**已停用**的 `sche_tasks/disabled/price_alert.json`（不算接线，与 R473 之后的「price_alert 已转反射器 plist」一致）。
- **未接线 10 条，全为声明项（orphan=0）**：01–05 六个官方示例场景中的五个（`01_system_monitor` / `02_email_handler` / `03_daily_report` / `04_github_monitor` / `05_folder_watcher`）声明 `example_template`；`08_kc_snapshot`、`09_task_freshness` 声明 `pending_approval`；`reflect/agent_team_worker.py`、`reflect/checklist_master.py`、`reflect/goal_mode.py` 声明 `manual_on_demand`。
- **缺口 0**：没有任何「既未接线、也无声明」的脚本（即无静默孤儿）。

## 5. 负控与独立复算（L196 验收②）

- `--selftest` 夹具 23/23 PASS，其中负控覆盖：注释键里出现路径不算接线（NC1）、flag 拼错（`--reflectt`）不算（NC2）、目标脚本缺失 ⇒ `BROKEN_WIRING` rc=1（NC3）、未接线且未声明 ⇒ `ORPHAN` rc=1（NC5）、证据文件不存在 ⇒ rc=2（NC6）、证据 token 未命中 ⇒ rc=2（NC7）、未知 status ⇒ rc=2（NC8）、陈旧条目 ⇒ rc=2（NC9）、重复 id ⇒ rc=2（NC10）、sche 引用 ⇒ `WIRED_SCHE`（NC11）、helper 不进 gaps（NC12）、文档提及不计接线（NC13）、plist 未加载 ⇒ rc=1（NC14）、disabled sche 引用不算接线（NC15）、间接接线识别（NC16）、`proof` changed 为空（NC17）、crosscheck 正控逐字一致（NC18）、仅文本提及单列且 only_A 为空（NC19）。
- `crosscheck` 用**独立通路**（plist 文本解析，等价 `grep -lF`）复算接线集合，与结构化判据 A 集逐字一致：`MATCH (|A|=4 |B|=4)`，`only_A=[]`、`only_B=[]`。
- 本会话另用系统 `grep -rlF` 在 `~/Library/LaunchAgents` + `/Library/LaunchAgents` + `/Library/LaunchDaemons` + `sche_tasks/` 上再做一次外部复算，命中的**脚本 id 集合同样是那 4 条**（noisy 计数：06→1466 个文件含大量 `sche_tasks/done/*_price_alert.md` 回执；autonomous→4（1 plist + 3 done 回执）；scheduler→1；07→1）。未接线 10 条外部复算命中数全为 0。
- 只读自证：`proof` = `files=88 changed=[]`（88 个被读文件 sha256 前后一致），全程零写入。

## 6. 边界与已知口径（不可越界解读）

1. **「覆盖率 28.6%」不是缺陷率**：01–05 是官方示例场景（`docs/reflect_best_practices.md` 明文「轮询本身零消耗」且示例脚本供按需起用），`08`/`09` 的补调度属**待批项**（R415 D6 / R479 D2），`reflect/*` 三件是手动/协议脚本（`memory/goal_hive_sop.md` 里由人按需启动）。故「10 条未接线」是**已定性**的，不是漏配。
2. **提及 ≠ 接线**：`sche_tasks/done/*.md` 回执、报告正文、停用条目里出现脚本名一律不计；本器把它们隔离在 W4 层（只作旁证）。
3. **本器不新增任何调度**：只读，不改 plist、不改 sche_tasks、不触碰 `agentmain.py`。
4. **外部复算的 noisy 面**：`grep -rlF` 会把回执/报告算进去，故只用于「集合一致性」，个别脚本的命中**文件数**不可当接线计数（本条为本会话实测踩坑，见 §7）。
5. 未接线清单是**时点快照**：新增脚本或新挂 plist 后须重跑；声明面需随之更新，否则 `STALE_ENTRY`/`ORPHAN` 会 rc≠0。

## 7. 踩坑记录（可复用）

- **`grep` 目录必须带 `-r`**：本轮首次用 `["grep","-lF",pattern,dir1,dir2,dir3]` 批量复算，得到**全部 0 命中**（含已接线的 4 条），与工具结论矛盾；追查发现 `grep` 对目录参数报 `Is a directory` 且 rc=2、stdout 为空 ⇒ 假阴性。改为 `grep -rlF` 后与工具一致。教训：**批量复算脚本先自证「能读到目标」**（本例即 L196 验收②要求「与 grep 独立复算逐字一致」的实际拦路虎)。
- **独立复算要看集合、不看文件数**：done 回执会让 06 的命中文件数膨胀到四位数。
- **注册表写盘前的逐字校验**：10 条声明的 evidence token 全部用 `token in file_text` 先验，任一未命中即整体不落盘（fail-closed），避免写入一份「声明了但证据已过期」的表。

## 8. 结论与残余

- 结论：**接线面完全可解释，零静默孤儿**。真接线 4 条＝3 条反射器 plist + 1 条调度器 plist；未接线 10 条全部有带证据的声明；覆盖率 28.6% 的口径已在 §6 界定。
- 残余（均为**已知待批**，本报告不代批）：
  - `08_kc_snapshot.py` 的定时接入＝R415/R430 D6 待批；
  - `09_task_freshness.py` 的 launchd 条目＝R479 D2 待批（**注意**：不批则台账永远是人工真跑的 7 行）；
  - 本器本身是否接进 `complete_task` 或定时（每轮重算）——**未接线，留待批准**。
- 未做：不改任何 plist / sche_tasks；不评估「已接线脚本是否真的按预期产出」（那属 R411/R457/R467/R480 的判据面）。
