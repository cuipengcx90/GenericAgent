# EVPlayer 授权链路观测（AUTO 单任务收尾）

## 任务目标
在无需用户介入的前提下，观测 `.ev4a` 提交后弹出 `Online Activation` 的授权链路，判断当前阻塞更偏向本地许可证缺失，还是服务端联网校验。

## 已执行步骤
1. 重读自治/恢复/根因分析 SOP，按“单任务闭环”执行。
2. 复用已有稳定复现状态，确认提交 `.ev4a` 后会弹出 `Online Activation`，主窗口仍显示 `00:00/00:00`。
3. 采集授权态窗口、相关 PID、打开文件、网络连接、本地配置目录与二进制字符串。
4. 对比空闲态与授权态的 `/var/tmp/EVPlayer` 本地痕迹。

## 关键证据

### 1) 授权窗稳定存在
UI 读取到：
- `WIN:Online Activation`
- `TXT:在线授权`
- `请输入由商家提供的21位激活码，联网激活`
- 按钮：`激活`

同时主播放窗仍是：
- `WIN:EVPlayer`
- `TXT:00:00/00:00`
- `TXT:v1.4.9`

说明：
- 文件提交流程已推进到授权阶段；
- 但未进入可播放状态。

### 2) 授权态进程无现时网络连接
对授权窗对应 PID（86291）执行：
- `lsof -nP -i -a -p 86291`

结果：
- 未见任何现时 TCP/UDP 连接。

说明：
- 在采样时刻，授权窗弹出后没有保持中的外连；
- 不能证明“完全不联网”，但至少**未观察到持续在线校验会话**。

### 3) 未观察到本地许可证/缓存新增
本地检查结果：
- `/var/tmp/EVPlayer/` 仅见：
  - `conf/Admin.conf`
  - `log/` 目录
- 无新增 `license/lic/licence/db/cache` 一类文件
- `~/Library/Application Support/cn.ieway.EVPlayer` 不存在
- `~/Library/Preferences/cn.ieway.EVPlayer.plist` 仅见窗口/打开面板状态
- `~/Library/Saved Application State/cn.ieway.EVPlayer.savedState/` 仅见 GUI 状态保存

说明：
- 当前未看到明确的本地许可证落盘路径；
- 至少在本轮观测中，授权失败并未生成新的本地授权缓存。

### 4) 二进制内存在明显授权/服务端相关字符串
从 EVPlayer 可执行文件 `strings` 抽取到：
- `Online Activation`
- `ieway.cn`
- `www.ieway.cn/evplayer.html`
- `https://sso.ieway.cn/login`
- `https://ssoevtest.evketang.com/login`
- `getBlackWhiteList`
- `json_success`

说明：
- 程序内置了明显的 ieway 域名、SSO 登录入口以及黑白名单相关逻辑；
- 从实现痕迹看，授权链路**具备服务端依赖能力**。

## 根因分析（按 SOP 格式）

### 现象
提交 `.ev4a` 路径后，EVPlayer 不播放，稳定弹出 `Online Activation` 授权窗，主窗口时间轴保持 `00:00/00:00`。

### 根因
当前最小根因可收敛为：

**EVPlayer 在真正处理该 `.ev4a` 内容前先经过授权门槛校验；当前环境缺少有效激活条件，因此流程在授权阶段被拦截。**

### 量化影响
- 已从“打不开文件”推进到“稳定触发授权校验”；
- 但播放状态仍为 `00:00/00:00`；
- 本轮观测到：
  - 持续网络连接数：`0`
  - 新增本地许可证文件：`0`
  - 明确本地配置文件：`1`（`/var/tmp/EVPlayer/conf/Admin.conf`）
  - Application Support 授权目录：未发现

### 建议方案（待审批）
1. **优先策略：切回可调试副本，直接在授权判断函数/QNetwork 请求边界下断或 hook**
   - 目标：确认授权判定点、机器码生成点、网络请求发起点。
   - 预期收益：能直接判断“本地拦截 / 黑白名单 / 远程 SSO”哪一层生效。
   - 风险：需要继续动态调试，复杂度较高。

2. **若有合法条件：提供 21 位激活码做一次正向验证**
   - 目标：确认通过授权后是否立即进入播放/解密链路。
   - 预期收益：最快验证授权是否为唯一主阻塞。
   - 风险：依赖外部合法授权信息。

3. **逆向静态补充：围绕 `Online Activation` / `getBlackWhiteList` / `ieway.cn` 相关引用继续反查**
   - 目标：缩小授权分支函数集合，为下一轮 LLDB 命中做准备。
   - 预期收益：减少盲打断点。
   - 风险：需要额外分析时间。

## 本轮结论
AUTO 单任务“观测授权链路”已完成。  
结论不是“文件打开失败”，而是：

**`.ev4a` 已能把 EVPlayer 推进到授权校验阶段；当前真实阻塞点是激活/授权门槛。**
