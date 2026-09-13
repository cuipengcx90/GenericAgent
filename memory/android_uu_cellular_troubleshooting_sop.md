# Android 蜂窝网络下 UU 远程连接转圈排障 SOP

## 适用场景
Android 手机使用蜂窝数据连接 Mac 上的网易 UU 远程，出现持续转圈、不开 VPN 连不上，开 VPN 后恢复；不处理远程密码/免输密码问题。

## 结论口径
- UU 远程依赖网易云端信令和动态中继，不是手机直接连接 Mac 局域网地址。
- 本场景已验证：Mac 端 UU 登录正常、XPC 服务运行、网易信令长连接正常；失败现场持续约 15 秒时，Mac 没有出现新的远控会话/中继连接。
- 因此当前证据指向“手机 UU → UU 云端/信令的前段未成功”，不是“已连到 Mac 但画面加载失败”。
- VPN 能恢复只证明 VPN 改变了手机出口/DNS/路由后路径可用；不能仅凭此断言一定是联通故障，也可能是 Android UU 客户端缓存、DNS、IPv6、网络会话或服务端调度兼容问题。
- Mac 侧曾验证：Clash 为 rule 模式、TUN=false、GLOBAL=DIRECT；Mac 真实 Wi-Fi 源到当前网易信令/中继 TCP 443 连通。因此不要未经新证据修改 Mac Clash。
- 升级后特殊分支（本次已验证）：4.40.0 安装完成后，Mac UU 可能同时显示 `isLoggedIn=true`、`networkStatus=connected`、`xpcServiceStatus=running`，但手机端仍将 Mac 显示为离线；本机网络通不等于被控设备已完成云端注册。
- 安装包审计未发现官方安装脚本删除 UU 用户配置；不能仅凭升级后离线断言配置被清空或升级必然存在 bug。

## 首次复现与记录
1. 手机关闭 VPN，保持蜂窝数据，打开 UU 发起连接。
2. 若持续转圈，保持页面不退出、不打开 VPN，记录开始时间、运营商、4G/5G、是否刚从 Wi-Fi 切换。
3. 截图保留转圈页面；之后才可打开 VPN复测，记录是否立即成功。
4. 对照测试：同一手机同一时间用 Wi-Fi；或另一台手机用同一蜂窝运营商。对照结果用于区分手机、运营商出口和 UU 服务端。

## Mac 端现场核验（只读）
失败复现期间执行，禁止无条件杀进程：

```bash
adb devices -l
uuyc-cli
uuyc-cli user info
uuyc-cli device list
lsof -nP -a -p $(pgrep -f '/Applications/UURemote.app/Contents/Helpers/UURemoteServer' | head -1) -i
```

判定：
- `uuyc-cli` 应显示 `isLoggedIn=true`、`networkStatus=connected`、`xpcServiceStatus=running`。
- `device list` 中目标设备应为在线；若云端显示离线，先处理 Mac/UU 服务，不归因手机蜂窝。
- 失败转圈时 Mac 只有信令 `ESTABLISHED`、没有新的会话/中继连接：记录为“请求未到达被控端”，继续查手机侧。
- 可用 `nc -s <Mac物理网卡IP> -G 4 -vz <实际返回的中继IP> 443` 做连通性抽查；禁止凭模板猜中继 IP。

## 升级后 Mac 显示离线的分支

仅当问题发生在升级/重新安装授权之后，且 `uuyc-cli status` 同时显示登录、网络和 XPC 正常，而手机端仍显示该 Mac 离线时，按以下顺序处理：

1. 用 `uuyc-cli assist id` 确认本机仍有被控设备标识；不要把设备 ID 写入日志或记忆。
2. 在 UU 本机设置中确认并重新开启“允许他人远程协助本机”（需要用户明确授权）；网络 connected 不能推出手机端可控。
3. 关闭并重新打开 UU；若服务注册仍未恢复，先向用户说明影响并请求授权，再执行一次完整系统重启。不得擅自登出、清除数据或删除配置。
4. 重启后重新打开 UU，重新核验 `uuyc-cli status` 的登录、网络、XPC 状态，以及 `uuyc-cli device list` 的在线状态；手机端再用实际连接测试确认。
5. 若仍离线，回到 launchd/守护进程、安装后脚本和注册日志的只读核验；不要直接重装或修改 Clash。

本次已验证的恢复链是：重新开启远程协助发布状态 → 完整系统重启 → UU 重新启动并完成云端注册。该链只能记录为本次有效处置，不能宣称适用于所有版本或已证明是 4.40.0 的软件缺陷。

## Android 侧处理顺序（均可回退）
### 1. 重启 UU 网络状态
设置 → 应用 → UU远程 → 强行停止；再打开 UU，用蜂窝数据重试。随后在应用信息 → 存储中执行“清除缓存”。

### 2. 重新建立蜂窝会话
开启飞行模式约 15 秒，关闭后等待 4G/5G 恢复，再不挂 VPN 重试。该步骤只刷新移动数据会话，不改 APN。

### 3. 清除 UU 应用数据
仅在前两步无效时执行“清除数据/清除全部数据”。这是高影响可回退步骤：会要求重新登录，可能清掉应用本地设置；执行前必须告知用户并确认。

### 4. 网络设置
若仍失败，检查 Android 设置搜索“私人 DNS”：优先设为“自动”；若没有该入口，跳过，不要让用户为此反复查找。随后检查 SIM/移动网络/APN，保持运营商默认 APN，不擅自修改服务器、端口、用户名、密码或协议。网络重置是最后手段，因为可能清除已保存 Wi-Fi 和蓝牙配对，执行前确认。

### 5. 临时稳定方案
若“不开 VPN失败、开 VPN稳定成功”重复出现，可在 VPN 应用启用分应用/按应用模式，仅让 UU远程走 VPN，避免全局代理。此为绕行方案，不应描述为根治；不要推荐来源不明的 VPN 配置。

## ADB 接入后采集（用户需授权）
USB 连接手机，手机选择文件传输并允许 USB 调试/RSA；先以 `adb devices -l` 确认设备，未出现设备时不得声称已采集日志。使用现有 `memory/adb_ui.py` 做 UI dump；只采集必要信息：

```bash
adb shell getprop | grep -E 'net\.|telephony|ro.build.version'
adb shell settings get global private_dns_mode
adb shell settings get global private_dns_specifier
adb shell dumpsys connectivity
adb shell dumpsys netstats
adb logcat -c
# 用户再次复现后
adb logcat -d -v time | grep -iE 'UU|uuyc|netease|dns|connect|network|timeout|ssl'
```

不得读取密钥、Cookie、token；日志交付前脱敏手机号、设备 ID、账号和认证信息。若 Android 无 root，接受只能取得系统/应用可见日志，不把“没有日志”当成“没有故障”。

## 判定矩阵
- Mac UU 离线：处理 Mac UU 服务/登录/网络，停止手机侧归因。
- Mac 在线，手机蜂窝失败，Wi-Fi成功：优先怀疑蜂窝出口、DNS/IPv6、移动数据会话或运营商到 UU 的路径；需要手机证据或 UU 客服确认。
- 蜂窝失败但飞行模式后恢复：移动数据会话/出口变化相关，记录为高概率网络会话问题，不能称永久根治。
- 清除缓存/数据后恢复：记录为 UU Android 本地状态问题。
- 不开 VPN和开 VPN都失败：不能继续归因于运营商路径，回查账号、设备在线状态和 UU 服务端。
- 手机发起期间 Mac 完全没有新会话：请求未到达被控端；若 Mac出现新会话但画面失败，转查中继/P2P/编解码，不使用本 SOP 的前段结论。

## 反馈 UU 客服模板
“Android 手机使用中国联通蜂窝数据，UU 远程连接在线 Mac 时持续转圈；开启 VPN 后可连接。关闭 VPN失败期间，被控端 UU 在线但未观察到新的会话请求。请排查 Android 蜂窝出口下的信令、DNS/IPv6及动态中继调度兼容性。”

## 证据边界与维护
- 当前没有通过 ADB 采到手机 APN、DNS、IPv6 或 UU logcat；后续不要把这些项目写成已验证结果。
- 每次复现追加日期、网络类型、是否 VPN、结果和 Mac 端是否收到会话；不记录密码、token、完整设备 ID、临时 PID。
- 处理后必须用“关闭 VPN + 蜂窝数据”至少重复测试一次，再报告“已解决”；仅 VPN成功只能报告“绕行有效”。
