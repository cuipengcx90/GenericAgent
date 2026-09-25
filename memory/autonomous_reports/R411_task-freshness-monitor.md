# R411 任务与外部通道「产物级新鲜度」每日监控（第12批 C2·三路评审 9/10/9=9.33 并列最高）

- 日期：2026-09-25
- 任务来源：`temp/TODO.txt:9`（第 12 批 C2，吸收 C7 通道健康）
- 一句话结论：**交付了单入口只读巡检工具 + 可复用判据表，并在真实环境跑通**——37 个目标 + 8 个外部通道里，**真实的静默失败只有 2 个**（birdiesearch 已 49.35 天、haisou 今日抓取失败），另 1 个软判据提示（x_ai_intel 前置缺 x.com 标签）与 1 个僵尸任务（ga-api）；本轮同时把首跑的 4 个硬问题收窄到 2 个（清掉 2 条误报）。
- **未完成项（明示）**：验收⑤「零新增定时」**未接线**——按纪律「改他人 plist / 新增定时须批准」，反射器已写好并可手动跑通，接线方式以两条待批提案落在台账（见 §6）。

## 1. 交付物（全部物理落盘）

| 文件 | 行/字节 | sha256 前 16 | mtime | 说明 |
|---|---|---|---|---|
| `tools/task_freshness.py` | 550 行 / 27119 B | `2def436349f7418f` | 2026-09-25 19:27:08 | 单入口巡检器（三维判据 + 通道表 + ledger + selftest） |
| `tools/freshness_criteria.json` | 260 行 / 18064 B | `9c383edb2dac2325` | 2026-09-25 19:26:25 | 判据表 = 37 目标 + 8 通道（单一数据源，改判据不动代码） |
| `reflect_scripts/09_task_freshness.py` | — | — | — | 反射器版（`--once` / `--selftest`），通知靠 ledger 差集去重 |
| `temp/task_freshness/FINDINGS.md` | — | — | — | 标定过程与踩坑（含首跑误报逐条归因） |
| `temp/task_freshness/run_final.txt` | 80 行 | — | — | §3 表 1 的原始输出（19:27 快照） |
| `temp/task_freshness/ledger.jsonl` | 1 行 | — | — | 探测时刻台账（19:27，hard=`['birdie_daily_monitor','haisou_daily_monitor']`） |

判据表计数（`json` 直读）：`targets=37`，其中 `sche=22`／`launchd=15`，`disabled=10`；另 `channels=8`。
★ 与 TODO 原文的「21 个 sche_tasks + 8 个 launchd」不一致：实测 22 + 15；差量来源 = 判据表按**实际在盘文件**枚举（`sche_tasks/*.json` 含 disabled 归档项、launchd 按 `~/Library/LaunchAgents/com.cuip.*.plist` 枚举），**以文件为准，TODO 的 21/8 是估计值**。

## 2. 判据口径（三层，先写死再跑）

1. **L1 产物过期/缺失**：目标声明的 `payloads[].glob` 最新 mtime 距今 > `max_silence_days` ⇒ ALERT；glob 未命中且 `required=true` ⇒ ALERT；命中但字节数 < `min_bytes` 且设了该字段 ⇒「空壳产物」ALERT。
2. **L2 降级签名**：在最近日志/报告里 grep 失败签名（如 `exit: 1`、`SSLError`、`403`、`抓取到 0 个课程`）⇒ 按 `soft` 标记决定 ALERT 或 INFO。**这是本工具存在的核心理由**：haisou 产物 mtime 是新鲜的，纯 mtime 判据永远看不见它今天抓取失败。
3. **L3 运行缺席**：`run_evidence` 最新文件距今 > `max_run_gap_days` ⇒ NO_RUN/ALERT。
- `disabled:true` 的目标一律 DISABLED，**不参与硬问题计数**（归档任务不算失败）。
- 无产物可得者显式标 NA（如 `pet`、`env-ga-lang`），不硬凑判据。
- 硬问题 = `severity != "soft"` 的 ALERT/WARN 目标 id 集合，落 ledger，供反射器做差集去重。

## 3. 实测（19:27 快照，原始输出）

```
== 任务/通道「产物级」新鲜度 = 2026-09-25 19:27（判据表 tools/freshness_criteria.json）
   目标 37 + 外部通道 8；产物文件命中 590；日志扫描 3
汇总(标*为软判据)：ALERT=3, DISABLED=10, INFO=1, OK=23；软 ALERT/WARN 1 条；硬问题 2 个：birdie_daily_monitor, haisou_daily_monitor
```

**两个真问题（逐条证据，均来自原始输出）**

- `[ALERT] birdie_daily_monitor  L1 产物过期：birdie_monitor/birdie_seen_items.json（2026-08-07 11:00）距今 49.35d > 允许 3d`
  外加同一目标的 3 条日志签名：`SSLError`（`urllib3.exceptions.MaxRetryError: HTTPSConnectionPool(host='www.birdiesearch.com', port=443): Max retries exceeded with`）、`Max retries exceeded`、`exit: 1`。
  ⇒ 与 `birdiesearch_monitor_sop` 的订正结论（连续 49 天静默失败、起点 2026-08-08）**独立复现一致**。
- `[ALERT] haisou_daily_monitor  L2 降级签名命中 temp/haisou_20260925_1115.log`，4 条：`抓取失败`（`HTTPSConnectionPool(host='quangneng.com', port=443): Max retries exceeded with url: / (Caused by SSLError(SSLEO`）、`SSLEOFError`、`抓取到 0 个课程`、`403`（`⚠️ 后续扫描中断：HTTP 403: 今日额度已用完，请明天再试`）。
  ⇒ 产物 `haisou_seen_items.json=8.16h/3` **新鲜**，若无 L2 层则该失败完全不可见。

**软判据 / 僵尸任务（不计硬问题）**

- `[INFO] x_ai_intel_daily  L2 降级签名命中（软判据） sche_tasks/done/2026-09-25_0903_x_ai_intel_daily.md: No active X browser tab` ⇒ 按 `x_ai_intel_daily_sop` 属**设计内跳过**（无前台已登录标签页），故标 `soft` 只提示。
- `[ALERT*] ga-api  launchd 未加载：plist 存在但 launchctl list 无 com.cuip.genericagent-ga-api（僵尸任务）` ⇒ 软标记，与既有「ga-api 退役」待批建议同向，不重复立项。

**外部通道（8 个，同屏给「最近成功时刻 + 失败模式」= C7 并入，不另立台账）**

- 原文示例（`run_final.txt` 末段）：`EIA（供应链数据源）  最近成功=2026-09-20 14:44 (5.20d)  失败模式=无签名`。
- 通道行与任务行同表输出，失败模式为「无签名 / 有签名」两值，最近成功时刻按最近一次含成功标记的产物或日志行取。

## 4. 首跑 4 个硬问题 → 收窄到 2 个（误报逐条归因）

| 首跑误报 | 归因 | 处置 |
|---|---|---|
| `github_trending` | glob `*trend*.log` 误命中 trendradar 的日志 | glob 收窄为专属前缀；另加 `min_bytes=4000` 判「空壳产物」 |
| `eia_weekly` | 失败签名词表里含泛词「降级」，命中正常文本 | 去掉泛词，改具体签名 |
| linux.do 相关 | 「CF」等词把正常「等待自愈」文本判失败 | 改未解决措辞 |
| `x_ai_intel_daily` | 前置缺失≠任务失败 | 新增 `scans[].soft` 支持，改软判据 |

另修两个**显示/契约类** bug（不改判据语义）：① `launchd_state` 表头把 int 当字符串格式化会崩；② 结果字典未带 `max_run_gap_days`，表头「运行证据」列恒显示 `-d`，补字段后正确显示（如 `8.40h / 2d`）；③ `run(as_json=True)` 误返回 rc 导致 selftest 崩溃，改为 `return_result` 显式契约。

★ 其中第 ③ 类与「软标记」都属 RULES#16 的同类教训：**先证工具真按预期执行**，再谈结论。软标记第一版实现放在汇总处、用 `h["file"]` 与 glob 全路径比对，因相对/绝对路径不一致而**静默失效**（跑了但没生效，与「没跑」外观相同）⇒ 改为在扫描循环内同源打标 `h["scan_soft"]`。

## 5. 回归与自测（可复跑）

- 巡检器：`python3 tools/task_freshness.py --selftest` ⇒ `负控/回归合计：PASS（7/7）`，`selftest_rc=0`，夹具根 `temp/task_freshness_selftest`（可整目录删）。
  - 覆盖：① 49 天静默 → birdie STALE 且判为硬问题（= TODO 验收③的历史回放夹具）② haisou 日志签名 → DEGRADED(ALERT) 而产物仍新鲜 ③ 负控：健康任务不得报警（= 验收③负控）④ 通道表带出最近成功时刻 + 失败模式 ⑤ 负控：产物改新鲜 → 不再因 L1 报警 ⑥ 负控：日志无签名 → 转 OK ⑦ 语料自证：payload=3 且日志=2（精确计数）。
- 反射器：`python3 reflect_scripts/09_task_freshness.py --selftest` ⇒ `反射器自测合计：PASS（6/6）`，含两条负控（基线含幽灵 id ⇒ 必须报「已恢复」；同输入 ⇒ 差集为空不重复推送）。
- 真链路去重实证：先跑一次落台账，再 `--once` ⇒ 输出 `[09_task_freshness] 无变化（硬问题集合未变），静默`、`reflect_rc=0`（不推送、不打扰）。

复现命令：

```bash
cd ~/projects/GenericAgent
python3 tools/task_freshness.py --root ~/projects/GenericAgent          # 落台账的真实巡检
python3 tools/task_freshness.py --selftest                              # 7/7
python3 reflect_scripts/09_task_freshness.py --once                     # 反射器一次（静默=无新问题）
python3 reflect_scripts/09_task_freshness.py --selftest                 # 6/6
```

## 6. 待批提案（写进台账，本轮不接线）

- **A（推荐，零新增定时）**：把 09 挂进已在跑的 `com.cuip.genericagent-l2-gate`（30 分钟循环里加一行调用）。属**改他人既有 plist**，故待批。
- **B**：新建 `com.cuip.genericagent-task-freshness.plist`。属**新增定时**，故待批。
- **C（顺带）**：`ga-api` 僵尸任务（plist 在但未加载、日志 11.39d 未动）与既有「ga-api 退役 + 日志回收」待批建议合并，**不另立条目**。

## 7. 边界与残余风险

- 状态会漂移：本报告全部数值是 **2026-09-25 19:27 快照**，不得当永久结论（`hard=2` 明日可能变化）。
- L2 签名依赖词表：词表是「少报」优先的设计（宁可漏，不可误报），故**不宣称覆盖全部失败模式**；未命中不等于成功。
- 不自动修：反射器只发现+通知，**不会**去改任务配置、不重跑任务、不动任何产物。
- `tools/*.json` 判据表住在跟踪目录（可提交）；`temp/` 下产物与台账不入库（`.gitignore`），故关键事实同时写入本报告与 L1/L2 记忆。
