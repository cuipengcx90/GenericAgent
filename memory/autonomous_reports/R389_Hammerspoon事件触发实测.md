# R389 · TODO#11 Hammerspoon「系统事件触发」实测（含：是否常驻的决策依据）

> 实测时间：2026-09-25 01:44–01:50（本机 macOS，Hammerspoon **1.1.1**，`/opt/homebrew/bin/hs` CLI）
> 口径：本文所有数值/报错均为**当场命令原始输出**回贴；证据文件保留在 `temp/hs_probe/`。
> 零改动声明：未修改 `~/.hammerspoon/init.lua`、未新增 LaunchAgents/登录项、未点任何系统授权框；探针脚本以 `hs -c` **内存加载**运行，Hammerspoon 已退出（恢复"未运行"原状）。

---

## 0. 结论（先给答案）

| # | 问题 | 实测结论 |
|---|---|---|
| ① | 系统事件能否触发 | **能，且大部分零授权**：Wi-Fi 变化 / 进程启动退出 / 文件落地 / 剪贴板 4 类**真跑触发成功**（见 §3） |
| ①b | "按快捷键 → 执行动作" | **未打通，原因=缺辅助功能授权**，非 Hammerspoon 缺陷（见 §3.3；2 次合成按键 0 命中，`hotkey.bind` 注册成功） |
| ② | 是否常驻 | **本轮不启用**；常驻的收益面=零授权事件源（可自证），成本/风险面=改 `init.lua` + 自启 +（按 SSID 触发还需定位授权）⇒ 属你的决策，见 §4 |
| ③ | 零改动生产配置 | ✅ `init.lua` 未改（mtime `2026-09-24 22:42:24` **早于本会话**，sha1 `2bb31bee…`）、无自启/登录项新增、无残留进程 |

**一句话**：Hammerspoon 的"事件驱动"能力本机**已可用且已验证**（零授权部分），但要用起来必须有一个人工动作——要么接受"每次手动开 app"，要么做一次权限/自启决策（§5 给了 4 个各 1 分钟的可选项）。

---

## 1. 环境与权限现状（实测原文）

```
$ which hs                              → /opt/homebrew/bin/hs  (readlink -f → /Applications/Hammerspoon.app/Contents/Frameworks/hs/hs)
$ PlistBuddy -c 'Print :CFBundleShortVersionString' /Applications/Hammerspoon.app/Contents/Info.plist  → 1.1.1
$ hs -c 'return tostring(hs.accessibilityState())'                                                      → false
$ hs -c 'local ok,err=pcall(function() return hs.axuielement end) return tostring(ok).." | "..tostring(err)'
  -- Loading extension: axuielement
  01:50:41 ERROR:   LuaSkin: hs.axuielement - module requires accessibility to be enabled; fix in SystemPreferences -> Privacy & Security
  true | table: 0x600000f00740                 ← 模块**能加载**，仅是运行时报缺授权（订正见下表）
$ hs -c 'return "all="..#(hs.window.allWindows() or {}).." vis="..#(hs.window.visibleWindows() or {})'  → all=2 vis=0
$ hs -c '…读 hs.wifi.currentNetwork()…'         → 探针日志行：wifi=power=true ssid=nil linkStatus=nil currentNetwork=nil
```

> 订正记录：`hs.wifi.ssid` 在本机 1.1.1 **不存在**（调用报 `attempt to call a nil value (field 'ssid')`）；SSID/连接信息只能走 `currentNetwork()` / `linkStatus()`。初稿写成“ssid() → nil”不实，已改。

| 探针项 | 结果 | 含义 |
|---|---|---|
| `hs.accessibilityState()` | `false` | 辅助功能**未授权** |
| `hs.axuielement` 模块 | **可加载**，运行时报 `module requires accessibility to be enabled` | 跨应用取窗口/控件不可用（订正：非“拒载”） |
| `visibleWindows` | `0`（`allWindows`=2 全是 Hammerspoon 自己） | 看不到**任何**他应用窗口 |
| `hs.window.find("Google Chrome")` | `0` | 同上 |
| `hs.wifi.currentNetwork()` | `nil`（`linkStatus` 同为 `nil`） | 缺**定位服务**授权 → 拿不到 SSID |
| `hs.hotkey.bind(⌘⌃⌥J, …)` | `Enabled hotkey ⌘⌃⌥J` | 注册**成功**（投递另说，见 §3.3） |

> 注：`allWindows=2 / visibleWindows=2 [Hammerspoon, Hammerspoon]`（第二批探针时）也印证同一件事——**能看见的只有自己**。

---

## 2. 「系统事件源」可用性矩阵（本机实测枚举）

| 事件源 | 本机是否有 watcher | 是否需要授权 | 本会话是否**真触发** |
|---|---|---|---|
| `hs.wifi` | ✅ `watcher:function` | 否 | ✅ 3 次回调 |
| `hs.application` | ✅ `watcher:function` | 否 | ✅ 7 条事件 |
| `hs.pasteboard` | ✅ `watcher:function` | 否 | ✅ 2 条事件 |
| `hs.pathwatcher` | ✅（模块级构造） | 否 | ✅ 2 条事件 |
| `hs.caffeinate` | ✅ `watcher:function` | 否 | ⬜ 未测（需睡眠/唤醒等物理动作） |
| `hs.usb` | ✅ `watcher:function` | 否 | ⬜ 未测（需拔插设备） |
| `hs.battery` | ✅ `watcher:function` | 否 | ⬜ 未测（需电源变化） |
| `hs.screen` | ✅ `watcher:function` | 否 | ⬜ 未测（需改分辨率/接显示器） |
| `hs.hotkey` | ✅（模块级） | **是**（投递） | ❌ 0 命中 |
| `hs.eventtap` | ✅（模块级） | **是** | ❌ 合成按键未被接收 |

```
$ hs -c '…枚举各模块类型…'
wifi=table(watcher:function) ; caffeinate=table(watcher:function) ; usb=table(watcher:function) ;
battery=table(watcher:function) ; screen=table(watcher:function) ; application=table(watcher:function) ;
pasteboard=table(watcher:function) ; path=nil(-) ; hotkey=table(-) ; eventtap=table(-) ; ipc=table(-)
```

---

## 3. 逐项实测明细（附原始证据）

### 3.1 Wi-Fi 变化触发 —— ✅ 真跑成功（且含"断网自杀"防护）

**风险**：本机默认路由就是 `en0` Wi-Fi ⇒ 关 Wi-Fi = 自断网（含我自己的通道）。做法：把「关→取证→再开→取证→连通性自证」**封在同一次 tool call 内**，且恢复动作放 `finally`，与探针同进程的 `hs.wifi.watcher` 负责记录回调。

`temp/hs_probe/events.log` 原文节选：

```
2026-09-25 01:47:54  WIFI_WATCHER_START  ok=true err=nil running=nil | power=true ssid=nil linkStatus=nil currentNetwork=nil
2026-09-25 01:48:03  WIFI_EVENT  callback fired; power=false ssid=nil linkStatus=nil currentNetwork=nil
2026-09-25 01:48:10  WIFI_EVENT  callback fired; power=true  ssid=nil linkStatus=nil currentNetwork=nil
2026-09-25 01:48:10  WIFI_EVENT  callback fired; power=true  ssid=nil linkStatus=nil currentNetwork=nil
```

- `power=false` = 关闭瞬间的回调；`power=true` **两条** = 恢复过程中系统状态多次翻转（不是重复 bug，回调按"状态变化"逐次投递）。
- 关键点：**全程未授予任何权限**，回调照常触发 ⇒「Wi-Fi 断/连」是最便宜的自动化触发器。
- 局限：`ssid/currentNetwork` 恒 `nil` ⇒ 规则只能按"电源开关/连接状态"，**不能**按"连到某个 SSID"（要按 SSID 必须授定位服务）。

网络恢复自证（`temp/hs_probe/wifi_cycle_result.json` + `wifi_cycle_ga.log` 关键字段）：

| 阶段 | 证据 |
|---|---|
| 关之前 | `power=Wi-Fi Power (en0): On`，默认路由 = `en0`，网关 `192.168.3.1` |
| 关之后 | `power=Wi-Fi Power (en0): Off`，查路由返回 `route: writing to routing socket: not in table`（真断网） |
| 恢复后 | `power=On`、`restored=true`，默认路由回 `en0`/`192.168.3.1`；ping 原文 `2 packets transmitted, 2 packets received, 0.0% packet loss`；`curl` **HTTP 200** |

### 3.2 窗口布局动作 —— ✅ 实测生效（作用于 Hammerspoon 自己的窗口）

```
2026-09-25 01:49:44  LAYOUT_BEFORE  win= frame=512,153,437,189 screen=0,0,1512,982
2026-09-25 01:49:44  LAYOUT_AFTER   ok=true err=nil frame=0,0,756,982 target=0,0,756,982
```

- 动作：把窗口 frame 设为「屏幕左半」= `(0,0,1512/2,982)`；结果 frame 精确等于 target（`OK`）。
- **边界（重要）**：`ax=false` 时 `hs.window` 只看得到自家窗口 ⇒ 这条实测证明的是「**API 与动作通路可用**」，而**不是**「能摆布 Chrome/Finder」。要摆布他应用窗口必须先授辅助功能。
- 附带：`hs.window.setFrame` 在本机返回 `ok=true` 且 after 帧与 target 完全一致 ⇒ 无"静默不生效"。

### 3.3 快捷键投递 —— ❌ 未打通（结论：缺辅助功能授权）

证据：

```
2026-09-25 01:48:48  HOTKEY_BIND2  ok=true hk=table:0x600003d8fc80     # ⌘⌃⌥K 注册成功，日志「hotkey: Enabled hotkey ⌘⌃⌥K」
$ hs -c 'hs.eventtap.keyStroke({"cmd","alt","ctrl"},"k") return "posted2"'   → posted2
$ hs -c 'return tostring(_G.GA.hkHits)'                                     → 0
$ grep -c 'HOTKEY_FIRED\|LAYOUT_FIRED' events.log                           → 0
```

- `hotkey.bind` **注册成功**、`hs.eventtap.keyStroke` **调用成功**（`posted`），但随后查询计数 `hkHits` **= 0**、`HOTKEY_FIRED` 日志 **0 条**。
- 同时 `accessibilityState=false` ⇒ 最合理解释：**按键事件（含程序合成）在未授权时不会进入 Hammerspoon 的事件流**。
- 我**没有**放弃的一点：合成按键本身也可能因"非可信来源"被拦，所以严格说这 2 条证据**都**指向"授权"这一根因（互为印证），但**未做授权后的对照实验**（授权必须由你点）。

### 3.4 另外 3 类零授权事件源 —— ✅ 真触发（截至今）

```
2026-09-25 01:48:50  EV_PATH        paths=ga_trigger_1.txt
2026-09-25 01:48:52  EV_PASTEBOARD  clipboard changed; len=19
2026-09-25 01:48:54  EV_APP         计算器 0
2026-09-25 01:48:54  EV_APP         计算器 activated
2026-09-25 01:48:54  EV_APP         Google Chrome deactivated
2026-09-25 01:48:54  EV_APP         计算器 launched
2026-09-25 01:48:59  EV_APP         Google Chrome activated
2026-09-25 01:48:59  EV_APP         计算器 deactivated
2026-09-25 01:48:59  EV_APP         计算器 terminated
2026-09-25 01:49:19  EV_PATH        paths=ga_trigger_2.txt
2026-09-25 01:49:21  EV_PASTEBOARD  clipboard changed; len=11
```

计数器汇总（`cd temp/hs_probe && grep -oE 'EV_[A-Z]+|HOTKEY_FIRED|LAYOUT_FIRED|WIFI_EVENT' events.log | sort | uniq -c`；快照 2026-09-25 01:51，`events.log` 共 60 行、sha1 `04bed49b3598…`；末尾复核动作还会继续追加行）：

```
   7 EV_APP
   3 WIFI_EVENT
   2 EV_PATH
   2 EV_PASTEBOARD
```

`HOTKEY_FIRED` / `LAYOUT_FIRED` 均为 **0 条**（`grep -c` → 0）；`HEARTBEAT` 由调试用 2s timer 产生，非事件源。

- `hs.application.watcher` 能拿到「启动/激活/停用/退出」四态（`计算器` 全程被记录，且能区分是哪个 app）。
- **误报防护**（本轮踩到并已定位）：第一批探针**看似 0 事件**，实为**输出被截断**；加了一条心跳 timer（`HEARTBEAT n=1..21`，每 2s）后，`hs` 命令与事件日志的时序关系才自证清楚。⇒ **调试事件驱动脚本时，必须带心跳/活性信号**，否则"没日志"分不清是"没事件"还是"没在看"。

---

## 4. 要不要常驻？（给决策用的成本/收益）

**支持常驻（收益）**
1. 零授权即可用的事件源已实测：Wi-Fi 断/连、进程启动退出、文件落地、剪贴板 ⇒ 可以无权限实现"断网自动做 X""某个 app 一启动就 Y""监控目录一有文件就 Z"。
2. 成本极低：常驻本体只是 app 在跑；单条规则通常 <20 行 Lua。

**反对"现在就设成常驻"（成本/风险）**
1. **必须有规则脚本才有价值**：常驻一个空 Hammerspoon ≈ 常驻一个进程干看着。而写规则 = 改 `~/.hammerspoon/init.lua` = **改生产配置**（属需你批准的类别，本轮未动）。
2. **半数的直觉用途在授权之后**：窗口布局/快捷键/读他应用窗口内容——未授权时**全不可用**（已实测）。
3. **权限面会扩大**：常驻通常要「开机自启」；若规则要按 SSID 还得加「定位服务」；要热键还得加「辅助功能」。
4. 与现有体系重叠：本机已有 **GA 反射器（每 30min 轮询，零成本）** 与 launchd 任务。若需求是"定时跑"，Hammerspoon 没有优势；**它唯一的不可替代性 = 真事件驱动**（毫秒级、无轮询）。

**建议**：按需启用（§5 选项 A 即可复现本报告全部零授权结论），等你确认真实用例后我再写规则脚本（一次约 10 分钟）。

---

## 5. 给你的最小行动清单（每项 1 分钟）

- **A. 只复现本报告（零授权，不改任何配置）**
  ```
  open -a Hammerspoon
  hs -c 'return dofile("/Users/cuip/projects/GenericAgent/temp/hs_probe/hs_evt_probe.lua")'
  cat /Users/cuip/projects/GenericAgent/temp/hs_probe/events.log
  ```
  （用完按 §6 退出即可）
- **B. 想用快捷键/窗口控制** → 系统设置 → 隐私与安全性 → **辅助功能** → 勾选 Hammerspoon；然后跑
  ```
  hs -c 'return dofile("/Users/cuip/projects/GenericAgent/temp/hs_probe/hs_evt_probe.lua")'
  hs -c 'hs.eventtap.keyStroke({"cmd","alt","ctrl"},"k") return "posted"'
  hs -c 'return tostring(_G.GA.hkHits)'     # 期望 ≥1
  ```
- **C. 想按"连到某个 Wi-Fi"触发** → 隐私与安全性 → **定位服务** → 勾选 Hammerspoon（否则 SSID 恒 nil）。
- **D. 想开机常驻** → Hammerspoon 偏好设置勾 *Launch on login*；规则脚本写入 `~/.hammerspoon/init.lua`（**需你批准后我代写**）。

---

## 6. 复现/清理命令（全部本轮实测过）

```bash
# 起
open -a Hammerspoon
# 载探针（内存加载，不写入 ~/.hammerspoon）
hs -c 'return dofile("/Users/cuip/projects/GenericAgent/temp/hs_probe/hs_evt_probe.lua")'
# Wi-Fi 事件循环（自恢复，含连通性自证）
python3 /Users/cuip/projects/GenericAgent/temp/hs_probe/wifi_cycle.py
# 证据
cat /Users/cuip/projects/GenericAgent/temp/hs_probe/events.log
shasum /Users/cuip/projects/GenericAgent/temp/hs_probe/events.log
# 清理（本轮已执行）
hs -c 'local G=_G.GA; if G.hbTimer then G.hbTimer:stop() end; for _,k in ipairs({"wifiWatcher","pathWatcher","pbWatcher","appWatcher"}) do if G[k] and G[k].stop then pcall(function() G[k]:stop() end) end end; if G.hotkey then pcall(function() G.hotkey:delete() end) end; return "stopped"'
osascript -e 'tell application "Hammerspoon" to quit'
```

退出现场状态取证：

```
init.lua: 2026-09-24 22:42:24  70B  sha1 2bb31bee97bc3e73d3110502403214027ef6610d  (2 行)
LaunchAgents(hammerspoon): none-in-LaunchAgents
登录项(hammerspoon): none-in-login-items
$ pgrep -f 'Hammerspoon.app/Contents/MacOS/Hammerspoon' | wc -l   → 0
```

---

## 7. 边界与残余风险（如实）

1. **热键/合成按键未被接收**这一条，是**基于 `ax=false` 的推断**（两条互补证据），**未做授权后对照**——授权动作属于你。
2. `caffeinate / usb / battery / screen` 四类 watcher **只证"能注册"、未证"能触发"**（需要睡眠、拔插、电源、显示器等物理动作，我无法在不打扰你的前提下制造；这些也**不属"可自主执行"**）。
3. Wi-Fi 事件是**单次循环实测（n=1）**，关机 1 次回调、开机 2 次回调；未做长时间稳定性/抖动观测。
4. `hs.wifi.watcher:running()` 返回 `nil`（该对象无此方法）⇒ 本轮用**事件计数**代替活性判据。
5. `EV_APP` 行里 `计算器 0` 是原始事件码（我的映射表没覆盖 0）；不影响结论（同一时刻的 `launched/activated/terminated` 是明文可读的）。
6. 期间启动了计算器（已 `terminated`，无残留）；未触碰密码/Keychain/系统安全设置。
