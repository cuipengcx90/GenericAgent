# GA 桌面版接管本地源码环境 SOP

让 `/Applications/GenericAgent.app` 跑本地源码环境（`/Users/cuip/projects/GenericAgent` + `/opt/anaconda3/bin/python3`），而非官方打包的 runtime（解压到 `/var/folders/.../T/`）。

## 背景/坑
- 官方 .app **硬编码强制用 runtime**，光改 `~/.ga_desktop_settings.json` 的 `project_dir/python_path` 无效（会被覆盖回 runtime）。必须改源码 `frontends/desktop/src-tauri/src/lib.rs`。

## 三处改动（缺一不可）
1. **源码补丁** `frontends/desktop/src-tauri/src/lib.rs`：加 `forced_project_dir()`/`forced_python()` 两函数，读 `~/.ga_desktop_settings.json` 的 `force_project_dir`/`force_python_path`，并让其**优先于** runtime。
2. **配置** `~/.ga_desktop_settings.json`（家目录，非源码树）：加 `force_project_dir`/`force_python_path` 指向本地源码与 anaconda python。
3. **替换二进制 + 重签**：编译带补丁的 `ga-desktop` → 替换 `/Applications/GenericAgent.app/Contents/MacOS/ga-desktop` → **ad-hoc 重签**（`codesign -s -`，否则 macOS 拦截）。

## 验证
bridge 子进程跑 `agentmain.py` + `/opt/anaconda3/bin/python3` 即接管成功。后续正常双击 .app 启动即可（自动读 force 配置）。

## 共享/隔离边界（接管后）
- **共享**（源码树内，三方=桌面/launchd/命令行通吃）：源码、anaconda python、`mykey.py`、`config/*.json`、`memory/`、`reflect_scripts/`、`logs/`、`temp/desktop_sessions.json`。
- **不共享**（运行态各自独立）：进程内存/连接、`~/.ga_desktop_settings.json`(桌面专属)、剪贴板 temp、当前会话上下文。

## 升级覆盖风险
- 官方升级/重装 **会覆盖整个 .app**（含补丁二进制+ad-hoc 签名）→ 退回 runtime。
- `~/.ga_desktop_settings.json` 在家目录，升级不动 → force 配置保留。
- **升级后恢复**：重做改动 1（patch lib.rs）+ 3（编译+替换+重签），改动 2 无需重做。

## 消息通道开关全灰 ≠ 故障（易误判）
- 桌面"消息通道"开关读的是 **bridge 自身 `procs` 表**（只认它亲手 spawn 的进程），非配置开关。
- 飞书/钉钉 IM 自启实际由 **macOS launchd plist** 管（`~/Library/LaunchAgents/com.cuip.feishu-agent.plist`→fsapp.py，`com.cuip.genericagent-launch.plist`→dingtalkapp.py，RunAtLoad=true）。launchd 拉起的进程是孤儿(PPID=launchd)，新 bridge 不跟踪 → UI 全灰，但**通道实际在跑**。
- 桌面 `autostart_extras()` 故意排除 IM（注释明示 IM stay manual）。所以桌面端无法配置 IM 自启，IM 自启归 launchd 管，是两套独立系统。


## [更新 2026-07-18] 上游已原生化桌面接管
origin/main 合并后, 桌面 App 接管机制升级为"方案三": 不再改 project_dir, 改用 `GA_ROOT` env 注入外部源码 (`sanitize_bundle_env` 实现)。
本地 lib.rs 原 `forced_project_dir`/`forced_python` 显式覆盖分支已在 merge 时移除(采上游侧); `forced_*` 函数定义仍保留但覆盖入口已废。
含义: 让安装版 .app 跑本地源码, 现应"设置 external GA source → 注入 GA_ROOT"再重签二进制, 而非旧的 force 覆盖 settings。
环境坑记录: 本次会话 code_run/file_read 的 stdout 展示层被缓存污染(返回旧输出/空), 全程靠进程 exit_code 编码判定结果, stdout 不可信。

## [更新 2026-07-18 已实测] 方案三接管步骤(★取代上面旧 force 补丁法★)
前提验证: 安装版 `/Applications/GenericAgent.app/Contents/MacOS/ga-desktop`(官方 codesign=com.genericagent.app)**已原生含方案三**——`strings 二进制 | grep -c ga_source_override`≥1 且含 `GA_ROOT`。若为真则**无需重编译/替换/重签**, 直接配置即可(旧 SOP 的"编译+替换+ad-hoc重签"仅在安装版还是旧版无 GA_ROOT 时才需要)。
接管三步(全部无需动二进制):
1. 合约校验: 用 **bundle python** 跑本地源的合约探针, 必须返回 `{"ok":true}`:
   `GA_ROOT=<本地源> <bundle_py> <本地源>/frontends/ga_contract_probe.py <本地源>`
   bundle_py = `/Applications/GenericAgent.app/Contents/Resources/runtime/python/bin/python3`
2. 写配置: `~/.ga_desktop_settings.json` **加一个 key** `"ga_source_override":"/Users/cuip/projects/GenericAgent"`。**保留原 project_dir/python_path 不变**(它们仍指 bundle runtime, 方案三只用 GA_ROOT 注入核, bridge/frontend/conductor 仍跑 bundle 自身代码)。改前先 cp 备份。
3. 重启 app: `osascript -e 'quit app "GenericAgent"'`;`pkill -f "GenericAgent.app/Contents/MacOS/ga-desktop"`;`open -a GenericAgent`。
验证接管成功(macOS SIP 禁 `ps eww` 查他进程 env, 改用可观测副作用):
- bridge(desktop_bridge.py) 的 `_ga_root_override()` 读 `--ga-root` arg 或 `GA_ROOT` env(仅当该目录含 agentmain.py 才认), 决定 sessions 目录/mykey/agent cwd/sys.path。
- ★决定性证据★: GA_ROOT 指向本地源时, bridge 会往 **`<本地源>/temp/desktop_sessions/`** 写会话文件(重启时刻的新 mtime); 而 bundle 内 `runtime/app/temp/desktop_sessions` 不存在。比对这两处即可确认, 无需查 env。
