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
