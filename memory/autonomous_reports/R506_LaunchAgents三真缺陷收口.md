# R506 LaunchAgents 三真缺陷收口（R500 取证 → 落地修复 + 提案）

- 任务：TODO L206 / 第24批 C1「LaunchAgents 三真缺陷收口」（三路评审 7/7/6 = 6.67）
- 日期：2026-09-29（现场数值统一取 §8 快照时点）
- 收窄条件（任务原文）：**仅本机可复现且我方 owned 项动代码**；跨环境/退役项只出提案
- 验收（任务原文）：①改前改后 exit 码/日志签名 A-B 对比 ②修后 lint verify problems 下降且有对账 ③不可逆动作零执行、退役类项待批
- 基线：R500（`tools/launchagent_plist_lint.py` 首次全量体检）findings 45 / WARN 5
- 新增工具：`tools/shell_utf8_var_lint.py`（20915 B / 501 行 / sha256 前16 = `6935e12e97fe3ff9`；位置子命令 `scan|verify|probe|selftest`）

## 1. 结论摘要

| 编号 | 缺陷（R500 取证） | 处置 | 结果 |
|---|---|---|---|
| 服务运行 07:05 无重启（快照 08:37），端口 38127 → HTTP 200 |
| ① | anyrouter `$locked）` 全角吞名 ⇒ unbound variable | **已修代码**：`${locked}`；工具 A/B findings 1→0 | 真实 kickstart 不再报 unbound；**暴露新根因**：job 前提（锁定行）已被移除 ⇒ 处置待批 |
| ④ | 三份超阈大日志（>64 MB） | **已修**：`tools/log_compress.py` copytruncate + gz 归档 | 释放 639866298 B（≈610.3 MiB），lint `LOG_FILE_LARGE` 2→0 |
| ② | ga-api `JOB_DISABLED`（plist 在盘但被 disable，源码缺失） | **只出提案**（属退役类，任务③要求待批） | plist/job 状态零改动 |

lint 复跑（同工具同口径）：`scan` counts `INFO30 / WARN3 / EXEMPT10`，findings **45→43**；`verify` `findings=43 problems=0 ok=True`；`selftest` 29 passed / 0 failed。

## 2. 缺陷③ cloudcli：ABI 失配崩溃循环 → 已修

- 根因：`better-sqlite3` 原生模块 ABI 与运行时 node 不匹配。现场 node = **v26.5.0**（需 `NODE_MODULE_VERSION` 147），旧二进制为更早 ABI ⇒ 每次启动即异常退出；plist 为 `KeepAlive=1` + `RunAtLoad=1`（**无 `ThrottleInterval` 键**）⇒ 进程退出即被重新拉起。
  - 改前证据：`/tmp/com.cuip.cloudcli.err.log` 中 `NODE_MODULE_VERSION` 命中 **24670** 次（R500 早前快照 21812 次 ⇒ 收口前仍在持续崩溃；该计数现已冻结）；`launchctl print` 的 `runs` 累积到 **12338** 次。
- 处置：对包 `@cloudcli-ai/cloudcli@1.29.5`（安装于 `/opt/homebrew/lib/node_modules/@cloudcli-ai/cloudcli`）执行 `npm rebuild`（旧二进制先备份为 `temp/l206_cloudcli/better_sqlite3.node.orig`，1931520 B / mtime `Apr 13 00:12`）。
  - 产物：新 `node_modules/better-sqlite3/build/Release/better_sqlite3.node` = **1915408 B / mtime `2026-09-29 08:30:31`**；原生加载自检 `load_ok Database,Statement,StatementIterator,Backup`。
- A/B（exit 侧）：
  - 改前：`runs` 持续增长、错误日志持续追加（至 08:30:30 冻结）。
  - 改后：08:30:40 spawn 成功，**`runs=12338` 自 08:30:40 起冻结**，`etime=03:39`（@08:34:19），端口 `127.0.0.1:38127` → `200`，插件 server 起于 51988 / 51989，err.log 冻结于 08:30:30（4739992 B / 命中 24670）。
  - 说明：`last exit code = 1` 字段**仍显示旧值**——它记录的是「最近一个已退出进程」，当前进程尚未退出 ⇒ 该 WARN 属长驻进程的陈旧字段（见 §5 D3）。

## 3. 缺陷① anyrouter：变量修正已落地，但 job 前提已消失

### 3.1 代码修复（已落地）

- 现场：`/Users/cuip/.local/bin/anyrouter-ip-refresh` 第 131 行 `$locked）`（全角右括号被 bash 5.3 并进变量名）⇒ `set -u` 下 `unbound variable`。
- 修法：`${locked}`（写前备份 `temp/l206_anyrouter/anyrouter-ip-refresh.orig`）。
- 工具 A/B（`tools/shell_utf8_var_lint.py scan --file <path>`，同一判据器、同一文件两版本）：

| 版本 | files | findings | vulnerable | 首条 |
|---|---|---|---|---|
| 修前（.orig） | 1 | **1** | **1** | `[ERROR] …/anyrouter-ip-refresh.orig:131:18 SHELL_VAR_MULTIBYTE_HAZARD` |
| 修后（生产） | 1 | **0** | **0** | — |

### 3.2 沙箱四例 A/B（含真实配置零写入自证）

夹具：`/tmp/l206_arb`，以 `HOME` 覆写隔离（脚本内 `VDIR="$HOME/…"`、`LOGDIR="$HOME/…"`）。活 IP = **43.109.133.205**（DoH `1.1.1.1` 解析 anyrouter.top 唯一 A 记录，`curl --resolve anyrouter.top:443:<ip>` 探活 **200**）。

| 例 | 夹具 | 脚本版本 | rc | 结果 |
|---|---|---|---|---|
| A1 | 有锁定行 | 修前 .orig | 0 | `锁定值 clash-verge.yaml: 43.109.133.205` → `存活探测 … -> HTTP 200` → `无需改动` |
| A2 | 有锁定行 | 修后生产 | 0 | 与 A1 逐字同 ⇒ **无回归** |
| B1 | 缺锁定行 | 修前 .orig | 1 | stderr 出现 unbound 变量报错（乱码形态，即用户可见的故障形态） |
| B2 | 缺锁定行 | 修后生产 | 1 | stderr 空；日志出现 `未找到唯一的锁定行（<missing>），仅报告，不自动改` + `ABORT: 锁定行异常 (<missing>)` |

- 口径：**B2 的 rc 仍为 1 是设计意图（fail-loud 闸门）**；本次修复消掉的是「崩溃式报错」，不是退出码。四例沙箱文件 `sandbox_changed=[]`，即缺锁定行路径**在任何写入之前**退出。
- 真实配置零写入自证：四例跑完后 `profiles/Merge.yaml` sha256 前16 `9d015689ece7fa38`、`clash-verge.yaml` `8a06b14f629a2eb4`，与跑前逐字一致。
- 真实 kickstart（08:32:18）：`refresh.log` 四行均为干净新文案（不再有乱码 unbound）；`launchctl print` = `state = not running` / `runs = 74` / `last exit code = 1`。

### 3.3 ★新根因（本次实测发现，非 R500 所列）：job 前提已被移除

- 两个配置现均**不含** anyrouter 锁定行：
  - `profiles/Merge.yaml` mtime `2026-09-25 16:28:59`、`clash-verge.yaml` mtime `2026-09-27 20:06:41`（`grep -rn anyrouter` 于该目录仅命中二进制备份）；
  - 存在备份 `profiles/Merge.yaml.bak-remove-hosts-20260925162827`（888 B，`hosts:` 段仍在但**无任何 anyrouter 行**）⇒ 09-25 16:28 有一次**刻意移除**动作（执行者未署名，本报告只陈述事实）。
  - 历史最后已知锁定值可从备份读出：`47.246.23.192`（`Merge.yaml.bak-anyrouter-fix-20260920120532`）与 `104.17.185.207`（`Merge.yaml.bak-anyrouter-20260920123006`）。
- 推论（判据链）：脚本的语义是「只把**已存在**的锁定行 IP 换成更快的一支，锁定行缺失即拒绝自动改」⇒ 在无锁定行的情况下，该 job **按设计**每次 abort，每 30 分钟失败一次（`StartInterval 1800`）。
- 因此「anyrouter 是否继续跑」属**政策取舍**（恢复锁定行 / 停用 job / 改为静默跳过），需拍板 ⇒ 见 §5 D2。本次只修代码，不动 plist、不代改配置。

## 4. 缺陷④日志回收 与 缺陷②ga-api 现状

- 日志（copytruncate：按 `tools/log_compress.py` 归档 gz + 尾部写回；`launchd StandardOutPath=O_APPEND` 已实证截断安全）：

| 文件 | 改前 | 改后 | 归档（logs/archive/） |
|---|---|---|---|
| `logs/feishu_agent.log` | 480543306 B | 1048546 B | `feishu_agent.log.20260929-083035.log.gz` 71859458 B |
| `logs/scheduler_reflect.log` | 104088659 B | 1048542 B | `scheduler_reflect.log.20260929-083041.log.gz` 12231883 B |
| `logs/ga_api_server.log` | 58379981 B | 1048560 B | `ga_api_server.log.20260929-083034.log.gz` 198749 B |

释放合计 = 479494760 + 103040117 + 57331421 = **639866298 B（≈610.3 MiB）**。
- ga-api（未改动，仅取证）：plist 在盘但被 `launchctl disable` ⇒ 永不运行；当前未加载；其源码 `temp/ga_api_server.py` **不存在**；历史日志 726104 条 ASGI 导入错误、已冻结于 09-14。处置（删除 plist 归档 / 恢复源码重建）属退役类 ⇒ 见 §5 D1。

## 5. 待批提案（台账 `temp/pending_decisions/DECISIONS_R506_launchagent_jobs.md`）

- **D1** ga-api 的 plist：归档移除（推荐）或保留现状。
- **D2** anyrouter job 前提：停用调度（推荐）或恢复锁定行（需给出目标 IP）；不建议改脚本为「缺锁定行 ⇒ exit 0 静默跳过」。
- **D3** lint 判据细分：当 job 的 `pid` 存活时，`LAST_EXIT_NONZERO` 降级为 INFO（长驻进程的 `last exit` 是陈旧字段）；改判据属工具行为变更，故列提案。

## 6. 不可逆动作零执行（任务验收③）

- **未**对任何 plist 做 `load/unload/enable/disable/bootout`，**未**修改任何 plist 文件，**未**停止任何服务，**未**清理任何用户目录文件。
- 唯一「运行类」动作 = `launchctl kickstart gui/501/com.cuip.anyrouter-ip-refresh`（一次执行）；其**未写任何配置**（§3.2 的 sha256 自证）。
- 动代码仅限「我方 owned 且本机可复现」两项：anyrouter 变量（带 .orig 备份）与 cloudcli 原生模块重建（带 .orig 备份）。退役类 ②/前提类 ①-新根因一律只出提案。

## 7. 复跑命令

```
cd ~/projects/GenericAgent
/opt/anaconda3/bin/python3 tools/launchagent_plist_lint.py scan --json-out temp/l206_lint/after.json
/opt/anaconda3/bin/python3 tools/launchagent_plist_lint.py verify --json temp/l206_lint/after.json   # findings=43 problems=0 ok=True
/opt/anaconda3/bin/python3 tools/launchagent_plist_lint.py selftest                                   # 29 passed
/opt/anaconda3/bin/python3 tools/shell_utf8_var_lint.py selftest                                      # 40/40
/opt/anaconda3/bin/python3 tools/shell_utf8_var_lint.py scan --file ~/.local/bin/anyrouter-ip-refresh # findings=0
tail -8 ~/.local/state/anyrouter-ip-refresh/refresh.log
launchctl print gui/501/com.cuip.cloudcli | grep -E 'runs =|pid =|last exit code'
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:38127/
```

## 8. 现场快照（2026-09-29 08:34:19）

- lint：`plists=29 owned=16 third_party=13 launchctl=True`；`counts={"EXEMPT": 10, "INFO": 30, "WARN": 3}`；`judges`（现）中 `LOG_FILE_LARGE` 键**已消失**（R500 为 2），`LAST_EXIT_NONZERO: 2`、`JOB_DISABLED: 1`。
- 三条残留 WARN（逐字，取自 2026-09-29 08:37 的 scan 原始输出）：

```
  [WARN  ] LAST_EXIT_NONZERO            com.cuip.anyrouter-ip-refresh 已加载 last exit status=1 (pid=-)
  [WARN  ] LAST_EXIT_NONZERO            com.cuip.cloudcli            已加载 last exit status=1 (pid=27574)
  [WARN  ] JOB_DISABLED                 com.cuip.genericagent-ga-api plist 在盘但被 launchctl disable ⇒ 永不运行
```

- cloudcli：`state = running` / `runs = 12338` / `pid = 27574` / `last exit code = 1`；`ps -o etime= -p 27574` = 07:05；端口 38127 = 200（快照 2026-09-29 08:37:45）。
- 证据留档：`temp/l206_notes/NOTES_R506.md`、`temp/l206_lint/after.json`、`temp/l206_anyrouter/`、`temp/l206_cloudcli/`、`/tmp/l206_arb`（沙箱夹具）。

## 9. 教训（本次实测）

1. **管道 rc 假绿**：`python3 tool.py ... | tail -3` 的退出码是 `tail` 的退出码 ⇒ 曾把「usage 输出」误判成 rc=0 成功；A/B 判据一律取**裸 rc**（本次钉正后才得到 §3.1 的真 A/B）。
2. **「exit 码不变」≠「没修好」**：B1→B2 的 rc 恒为 1，真正的差异在 stderr 与日志签名（崩溃 → 显式闸门）；故任务验收①须把「日志签名 A/B」与「exit 码 A/B」并列为两条判据。
3. **陈旧字段陷阱**：`launchctl print` 的 `last exit code` 对长驻守护进程是历史值，不能当当前健康判据（须配 `pid` + 端口/etime）。
4. **前提消失要先于代码缺陷被发现**：R500 只看到「exit 1 + unbound」，本次修完变量后**真实 kickstart** 才暴露「锁定行已被移除」这一更高层根因 ⇒ 修复类任务必须跑一次真实调度入口，不能只做静态 A/B。
