# R500 LaunchAgents plist 全量体检（只读）

- 任务：TODO L202 / 第23批 C3「LaunchAgents plist 体检」（评审 4.25）
- 日期：2026-09-29（凌晨，现场数值见 §8）
- 性质：**只读审计**。未 disable/enable 任何 job、未改任何 plist、未清理任何日志、未重启服务。
- 新工具：`tools/launchagent_plist_lint.py`（878 行 / 38245 B / sha256 前16 = 35c2121f43da12d1；位置子命令 `scan|verify|proof|selftest`）

## 1. 结论摘要

29 个 `~/Library/LaunchAgents/*.plist`（16 个本机自有 `com.cuip.*` / `com.genericagent.*` 等，13 个第三方）扫描后：

| 项 | 值 |
|---|---|
| counts | INFO 30 / WARN 5 / EXEMPT 10 |
| find 判据分布 | LOADED 19 · REL_SCRIPT_ANCHORED 4 · JOB_NOT_BOOTSTRAPPED_THIRD_PARTY 4 · LAST_EXIT_NONZERO 2 · MODULE_UNRESOLVED_AT_CWD 2 · LOG_FILE_LARGE 2 · PROGRAM_MISSING 2 · JOB_DISABLED 1 · JOB_DISABLED_THIRD_PARTY 2 · INLINE_SCRIPT 2 · LAST_EXIT_NONZERO_THIRD_PARTY 1 · MODULE_AT_CWD 1 · WRAPPER_SCRIPT 1 · BARE_PROGRAM_RESOLVED 1 · INLINE_SCRIPT_PATH_PARENT_MISSING 1 |
| `verify` | rc=0 findings=45 **problems=0**（无自相矛盾项） |
| `proof` | rc=0 files=29 `changed=[]`（29 个 plist sha256 前后一致 = 只读自证） |

**4 条真缺陷（3 条自有 job + 1 条日志面）**：

1. `com.cuip.genericagent-ga-api`：plist 在盘且 `KeepAlive=true`，但被 **launchctl disable** 且**源码文件已不存在** ⇒ 永不运行，只留下 58 MB 的错误日志（体量最大的静默垃圾）。
2. `com.cuip.cloudcli`：加载中但 **last exit status=1**，错误日志已 4 MB / 2.1 万行 Node ABI 不匹配，**2026-09-29 凌晨仍在崩**。
3. `com.cuip.anyrouter-ip-refresh`：加载中但 **last exit status=1**，`StartInterval 1800` ⇒ 每 30 分钟失败一次，日志 60 KB / 333 次同因错误。
4. 两条大日志（458.3 MiB / 99.3 MiB）均超 64 MB 阈值：`feishu_agent.log`、`scheduler_reflect.log`。

另有第三方登记级发现（§5），均非本机自建、不主动处理。

## 2. 方法与口径

- 数据源：`~/Library/LaunchAgents/*.plist`（plistlib 解析）+ `launchctl list` / `launchctl print <domain>/<label>` / `launchctl print-disabled gui/<uid>` + 系统 disabled 权威文件 `/var/db/com.apple.xpc.launchd/disabled.<uid>.plist`。
- 判据（每条 finding 一个 judge，可复算）：
  - 运行面：`LOADED`（在 `launchctl list` 中）/ `JOB_NOT_BOOTSTRAPPED_THIRD_PARTY` / `JOB_DISABLED[_THIRD_PARTY]`（plist 在盘但被 disable ⇒ 永不运行）；
  - 目标面：`PROGRAM_MISSING`、`MODULE_UNRESOLVED_AT_CWD` / `MODULE_AT_CWD`、`REL_SCRIPT_ANCHORED`（相对 token 被 WorkingDirectory 锚定）、`BARE_PROGRAM_RESOLVED`、`WRAPPER_SCRIPT`、`INLINE_SCRIPT`；
  - 退出面：`LAST_EXIT_NONZERO[_THIRD_PARTY]`；
  - 资源面：`LOG_FILE_LARGE`（StandardOutPath 体积 > 64 MB）。
- 只读保证：工具不写任何 plist、不调用 enable/disable/kickstart、不截断日志；`proof` 对 29 个 plist 做 sha256 前后比对照样输出 `changed=[]`。
- **判「是否被禁用」的正确读法（本次踩到的坑）**：`launchctl print-disabled gui/501` 输出可达数十行，**截断读取（本次程序里切成 800 字符）会把目标条目整段丢掉，得出「未被禁用」的假结论**。权威判据二选一并互证：(a) `print-disabled` 输出按 label **grep**（`"com.cuip.genericagent-ga-api" => disabled`）；(b) 直接读 `/var/db/com.apple.xpc.launchd/disabled.501.plist`（本次实测 `com.cuip.genericagent-ga-api: True`，全库 42 条，另见 `com.cuip.jetbrains-patch: False`、`com.cuip.anyrouter-ip-refresh: False`）。

## 3. 真缺陷 1：`com.cuip.genericagent-ga-api`（禁用 + 源码缺失 + 58 MB 错误日志）

**字段（实测）**：`ProgramArguments = [/opt/anaconda3/bin/python3, -m, uvicorn, temp.ga_api_server:app, --host, 127.0.0.1, --port, 8765]`；`KeepAlive = True`；`StandardOutPath = StandardErrorPath = /Users/cuip/projects/GenericAgent/logs/ga_api_server.log`；`EnvironmentVariables.PATH = /opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`。

**铁证**：

- `launchctl print-disabled gui/501` ⇒ `"com.cuip.genericagent-ga-api" => disabled`；`/var/db/com.apple.xpc.launchd/disabled.501.plist` ⇒ `com.cuip.genericagent-ga-api: True`。
- `launchctl print gui/501/com.cuip.genericagent-ga-api` ⇒ rc=113，`Could not find service "com.cuip.genericagent-ga-api" in domain for user gui: 501`（对照：同批 `com.cuip.genericagent-scheduler` rc=0 且 `active count = 1`）⇒ 未加载。
- `logs/ga_api_server.log`：58379981 B（55.7 MiB）、mtime **2026-09-14 10:06:45**（此后无写入）、含 `ERROR:    Error loading ASGI app. Could not import module "temp.ga_api_server".` 共 **726104 行**（全档出现次数）。
- `temp/ga_api_server.py`：**不存在**（`os.path.exists` False）。
- `127.0.0.1:8765`：`ConnectionRefusedError`。

**根因**：uctx 侧先 disable 了该 job，随后（或同时）`temp/ga_api_server.py` 被删除/未落盘 ⇒ uvicorn 每次尝试导入必失败；由于 job 被 disable，`KeepAlive` 不再触发，日志停留在 09-14。

**影响**：58 MB 死日志 + 一个「看着在册其实永不运行」的服务（任何 8765 依赖面都是静默死路）；`temp/` 属 gitignored 运行期资产，源码丢失后无法自愈。

**建议（未实施，待批）**：二选一 —— (a) 确认该 API 已不需要 ⇒ `launchctl bootout` 无（已未加载）+ 移走 plist 与日志归档；(b) 仍需要 ⇒ 恢复 `temp/ga_api_server.py` 并 `launchctl enable`。

## 4. 真缺陷 2/3/4

### 4.1 `com.cuip.cloudcli`：Node ABI 不匹配（仍在崩）

- 字段：`ProgramArguments = [/Users/cuip/Library/Application Support/CloudCLI/start-cloudcli.sh]`；`StandardErrorPath = /tmp/com.cuip.cloudcli.err.log`；`KeepAlive = True`。
- `launchctl list` ⇒ `-	1	com.cuip.cloudcli`（last exit status=**1**）。
- 错误日志：4190976 B（4.0 MiB），含 `NODE_MODULE_VERSION` **21828 行**，mtime **2026-09-29 04:18:57**（当日凌晨仍在写）。
- 原文：`NODE_MODULE_VERSION 131. This version of Node.js requires` / `NODE_MODULE_VERSION 147. Please try re-compiling or re-installing` / `the module (for instance, using npm rebuild or npm install).`
- 本机 `node -v` = **v26.5.0**（需 ABI 147），原生模块编译于 ABI 131 ⇒ 典型 `npm rebuild`/重装依赖问题，属 CloudCLI 侧依赖与其 Node 版本脱节。
- 影响：`KeepAlive` 下反复拉起重试（每次必然失败），持续产生日志；CloudCLI 功能不可用。
- 建议：在 CloudCLI 目录内按当前 Node 重建原生模块，或降低 Node 版本；修复前可先 `launchctl bootout` 止损（未实施）。

### 4.2 `com.cuip.anyrouter-ip-refresh`：`$locked` 未绑定（全角吞名）

- 字段：`ProgramArguments = [/Users/cuip/.local/bin/anyrouter-ip-refresh]`；`StartInterval = 1800`；`PATH = /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`；stdout=stderr=`/Users/cuip/.local/state/anyrouter-ip-refresh/launchd.log`。
- `launchctl list` ⇒ last exit status=**1**；日志 60597 B，含「未绑定的变量」**333 行**，尾行逐字：`/Users/cuip/.local/bin/anyrouter-ip-refresh: 行 136: locked：: 未绑定的变量`（即 `locked` 后紧跟全角冒号被并入变量名）。
- 根因：脚本 `set -uo pipefail` + 行 136 的 `（$locked）` 全角括号写法；**触发条件是 locale**：`LC_CTYPE` 为 UTF-8 时 bash 把全角字符并进变量名 ⇒ unbound；`C` locale 下同一行正常。plist `PATH` 使 shebang 解析到 homebrew 的 bash（5.3，UTF-8 处理与系统 /bin/bash 3.2 不同）⇒ 复现稳定。
- 影响：每 30 分钟失败一次（333 次同因），IP 刷新实际从未生效；日志持续增长。
- 修法（未实施，属外部脚本，需授权）：行 136 改为 `${locked}` 或改写全角标点；或在 plist 里显式 `LC_CTYPE=C`。二者都改一处即可。

### 4.3 两条超阈大日志（`LOG_FILE_LARGE`，>64 MB）

| plist | StandardOutPath | 体积 |
|---|---|---|
| `com.cuip.feishu-agent` | `logs/feishu_agent.log` | 480543306 B（458.3 MiB） |
| `com.cuip.genericagent-scheduler` | `logs/scheduler_reflect.log` | 104088659 B（99.3 MiB） |

两者均 `LOADED`、last exit status=0（功能正常），问题纯在日志无限增长。本机已有 `tools/log_compress.py`（copytruncate：归档 gzip + 尾部写回 + ftruncate；launchd `StandardOutPath` 为 O_APPEND 已实证 ⇒ 截断安全），建议对这两条接一次压缩（未实施）。

## 5. 其他发现（第三方 / 登记级，不处理）

- `com.google.keystone.agent` / `com.google.keystone.xpcservice`：plist 内容为 `<dict/>` 空壳（`PROGRAM_MISSING`：无 Program/ProgramArguments 且无任何触发器）+ 未 bootstrap ⇒ 属 Google 更新器残留，无需动作。
- `com.user.utools-delayed`：args[2] 为 353 字符内联脚本，其中引用 `/database/MacKed/LOCK` 的**父目录不存在**（`INLINE_SCRIPT_PATH_PARENT_MISSING`）+ 未 bootstrap。
- `netdisk_service`、`闪电说`：第三方 `JOB_DISABLED_THIRD_PARTY`（被用户禁用）。
- `com.macpaw.CleanMyMac5.Updater`：last exit status=**-6**（第三方异常退出）。
- `jetbrains.vmoptions`：1890 字符内联脚本 + 裸程序名 `sh` 解析为 `/bin/sh`（`BARE_PROGRAM_RESOLVED`），已加载状态正常。
- `com.cuip.archive-server`：`-m http.server` 在 WorkingDirectory 下无对应文件（标准库模块，非缺陷），已加载 pid=1201。
- 4 条 `REL_SCRIPT_ANCHORED`（autonomous / l2-gate / price-alert / scheduler 的相对脚本 token 已被 WorkingDirectory 正确锚定）—— 属**正面结论**：R375 的 cwd 吃态问题在这 4 条上不成立。

## 6. 复现命令

```bash
cd /Users/cuip/projects/GenericAgent
python3 tools/launchagent_plist_lint.py selftest          # 29/29
python3 tools/launchagent_plist_lint.py scan   --json temp/l202/scan_20260929.json
python3 tools/launchagent_plist_lint.py verify            # rc=0 problems=0
python3 tools/launchagent_plist_lint.py proof             # files=29 changed=[]
# 单条复核
launchctl print-disabled gui/501 | grep genericagent-ga-api
python3 -c "import plistlib;print(plistlib.load(open('/var/db/com.apple.xpc.launchd/disabled.501.plist','rb')).get('com.cuip.genericagent-ga-api'))"
launchctl list | grep -E 'cloudcli|anyrouter'             # 两者 last exit status = 1
```

## 7. 边界与未做

- **只读**：未执行任何 `enable/disable/bootout/bootstrap/kickstart`；未改任何 plist；未截断/删除任何日志；未改 CloudCLI、anyrouter 脚本。
- 未接线：本工具未接定时、未接 `complete_task`，修复动作全部待批（涉及第三方软件与用户 job 状态，属政策面）。
- 未做：`Label` 与 plist 实际文件名不一致的交叉检查（本次 29 个均一致）；系统级 `/Library/LaunchAgents` 与 `/Library/LaunchDaemons` 未纳入（本批范围仅用户域）；日志内容级归因（属 R393 `log_reclaim_plan.py` 边界）。
- 与既有资产的边界：`roi_monitor` 管任务计数、`task_freshness` 管产物新鲜度、R393 管日志可回收量；本器管**调度面配置/运行态一致性**，不重叠。

## 8. 数据快照（2026-09-29 现场）

- scan 逐字：`plists=29 owned=16 third_party=13 launchctl=True`；`counts={"EXEMPT": 10, "INFO": 30, "WARN": 5}`；`judges={...}` 见 §1 表。
- verify：`rc 0` / `findings=45` / `problems=0`；proof：`rc 0 files=29 changed=[] ok=True`。
- 产物：`temp/l202/scan_20260929.json`（25721 B）、`temp/l202/plists_raw.json`（14908 B）、`temp/l202/proof.json`（171 B）、`temp/l202/evidence_counts_20260929.json`（68 B）、`temp/l202/cloudcli_log_20260929.json`（124 B）。
- 关键体积：feishu_agent.log 480543306 B / scheduler_reflect.log 104088659 B / ga_api_server.log 58379981 B / /tmp/com.cuip.cloudcli.err.log 4190976 B / anyrouter launchd.log 60597 B。
- 工具：878 行 / 38245 B / sha256 前16 35c2121f43da12d1。
