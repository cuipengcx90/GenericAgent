# R359 | 日志体量治理：压缩覆盖率从 reflect 扩到长跑日志（dry-run + 待批清单）

- 日期：2026-09-23
- 类型：工程 | 自主任务（TODO 第17项，规划者新增·未经本轮评审）
- 边界：**全程 dry-run/只读**，未压缩、未删除、未改任何 plist 或生产配置，零新增定时。

## 0. 结论先行
`logs/` 实测 **1.7GiB**（数据卷 96% 满，仅剩 ~18GiB）。四个长跑日志占绝对多数，
按 `tools/log_compress.py`（keep 1MiB 尾部）处理后**净可回收 ≈ 1.55GiB**，其中单个
`launch_gui.log`（**1.13GiB、孤儿死日志**）就占 ~1.09GiB。R349 的压缩器只在
`complete_task` 对默认目标（`autonomous_reflect.log`）生效，这四个大文件 helper 从不触碰——
本条与 R349 **不重叠**（见 §4）。全部落**待批清单**，不擅动。

## 1. 逐文件实测（可回收量 + 真实压缩比）
gzip 比率取各文件首 20MiB 样本 `gzip -6` 实测；archive/净回收按 keep=1MiB 尾部推算。

| 日志 | 现体积 | 写入者(plist) | 状态 | gzip比 | 归档估算 | 处置 | **净可回收** |
|---|---|---|---|---|---|---|---|
| launch_gui.log | 1155.4MiB | **无任何 plist 引用** | **孤儿·冻结自 06-04** | 18.3x | ~63MiB | gzip→删原文(无需留尾) | **~1092MiB** |
| feishu_agent.log | 407.8MiB | com.cuip.feishu-agent | 活跃(09-23) | 10.5x | ~39MiB | copytruncate 留1MiB | ~368MiB |
| scheduler_reflect.log | 77.9MiB | com.cuip.genericagent-scheduler | 活跃(09-23) | 8.2x | ~9.5MiB | copytruncate 留1MiB | ~67MiB |
| ga_api_server.log | 55.7MiB | com.cuip.genericagent-ga-api | **停滞自 09-14+崩溃循环** | 278.5x | ~0.2MiB | copytruncate 留1MiB | ~55MiB |
| **合计** | **~1.70GiB** | | | | ~112MiB | | **~1.58GiB** |

dry-run 原始输出（`log_compress.py --dry-run --force`）：
```
feishu_agent     : 将归档 406.81MiB → gzip，日志 407.81MiB → 1023.99KiB
ga_api_server    : 将归档  54.68MiB → gzip，日志  55.68MiB → 1023.98KiB
scheduler_reflect: 将归档  76.90MiB → gzip，日志  77.90MiB → 1024.00KiB
```
（launch_gui 走单独处置见 §3，不用 copytruncate。）

## 2. copytruncate + O_APPEND 安全性（逐文件实证，lsof 本机不可用）
本机 `lsof`/`lsof +D`/`lsof -p` 对自身与他进程均返回空（受限，且 `proc_pidinfo` 读 fd
标志本机不可靠·见 L2 记忆），故改用与 R349 同款**文件增长特征**做经验判据：
> launchd `StandardOutPath` 以 **O_APPEND** 打开 ⇒ ①文件头部保留最早内容（非 O_TRUNC，
> 否则每次重启清空）②跨多次进程重启单调增长（非无 O_APPEND 覆写，否则从 offset 0 覆盖）。
> 满足二者即证 O_APPEND，则截断后持有 fd 的写者下次 write() 被内核重定位到新 EOF，
> **不产生稀疏空洞/NUL 填充**——copytruncate 安全。

逐文件证据（head=最早内容 / tail=最新 / 重启横幅计数）：
- **feishu_agent.log**：head=`飞书 Agent 已启动（长连接模式）`，tail=近期正常，重启横幅 **14280** 次，单调增长 → O_APPEND ✔ copytruncate 安全。
- **scheduler_reflect.log**：head=最早 Traceback，tail=近期 `[MixinSession]`，重启 **1013** 次 → O_APPEND ✔。
- **launch_gui.log**：head=`[Launch] Using port 18588`，tail=`Main window failed to start`，重启横幅 **140537** 次；**但自 06-04 冻结、无 plist 引用、无活写者** ⇒ 不需 copytruncate，直接 gzip→删原文最安全（无并发窗口）。
- **ga_api_server.log**：head=`Started server process [29472]`，tail=**`ERROR: Could not import module "temp.ga_api_server"`**（崩溃循环），停滞自 09-14；278.5x 高压缩正是该 ERROR 刷屏所致。log 层面 copytruncate 安全，但**另有 bug 需单独处理**（见 §5）。

残余风险：copytruncate 的读快照→归档→回写尾部→截断之间存在微秒级窗口，窗口内新行可能
既不进归档也不留原位（与 logrotate 同源，日志可再生可接受）。R349 已实证 reflect 类安全；
本报告对 feishu/scheduler 用同一 O_APPEND 机制 + 逐文件增长特征**另证**成立。

## 3. 待批清单（每文件：阈值/保留/归档目录/命令）——**批准后才执行**
统一归档目录：`logs/archive/`（已存在，当前 1.8MiB）。

```bash
# ① launch_gui.log —— 孤儿死日志，最大回收项。先归档再删原文（无活写者，无需留尾）
gzip -c logs/launch_gui.log > logs/archive/launch_gui.$(date +%Y%m%d).log.gz \
  && : # 人工核对 archive 完整后再: rm logs/launch_gui.log   # 回收 ~1.09GiB（删原文=1.13GiB）

# ② feishu_agent.log —— 活跃，copytruncate 留 1MiB 尾
GA_LOG_COMPRESS_THRESHOLD=$((8*1024*1024)) GA_LOG_COMPRESS_KEEP=$((1024*1024)) \
  python3 tools/log_compress.py --log logs/feishu_agent.log --force   # 回收 ~368MiB

# ③ scheduler_reflect.log —— 活跃，copytruncate 留 1MiB 尾
python3 tools/log_compress.py --log logs/scheduler_reflect.log --force   # 回收 ~67MiB

# ④ ga_api_server.log —— 停滞+崩溃循环；先修 §5 的 bug，再 copytruncate
python3 tools/log_compress.py --log logs/ga_api_server.log --force   # 回收 ~55MiB
```
建议参数：阈值 8MiB / 保留 1MiB（沿用 R349 默认）；归档保留份数策略未擅定，
建议 `logs/archive/` 保留最近 3 份/文件（需人工确认后再加清理规则，本轮不提供自动清理）。

**零新增定时**：不建议为这些大日志新增 launchd。可选做法（均待批）：
(a) 手动按需跑上述命令；(b) 仿 R357 把单文件压缩挂进既有反射循环（如 scheduler/l2-gate），
但反射脚本各需专属 plist 才生效（R357 已证），故本轮**只出方案不接线**。

## 4. 与 R349 待批项的关系（避免重复劳动）
- R349 交付 `tools/log_compress.py`（单文件接口，tracked）+ `temp/compress_log.py` 薄 shim，
  修掉了 `complete_task` 的死引用 WARN。它**只在收尾对默认目标 `autonomous_reflect.log` 生效**。
- R349 唯一遗留待批 = helper 第 29 行 `_COMPRESS_SCRIPT` 改指 `tools/`（因 temp/ 被 gitignore，
  shim 一旦被清 WARN 复发）。**那是"修死引用/防复发"，与本条无关。**
- 本条（R17）= **扩覆盖面**：把压缩从 reflect 类（KB~MB 级）扩到四个长跑大日志（百MB~GB 级），
  这些 helper 从不触碰。两者互补、无重叠：R349 管"接口存在且默认目标生效"，本条管"大文件被纳入治理"。

## 5. 附带发现（非本任务范围，仅记录）——ga_api_server 崩溃循环
`ga_api_server.log` 尾部持续刷 `ERROR: Error loading ASGI app. Could not import module
"temp.ga_api_server"`，停滞自 09-14。launchctl 中 `com.cuip.genericagent-ga-api` 存在，
但启动即导入失败。这是**独立 bug（模块路径/工作目录错配）**，非日志问题；278.5x 异常压缩比即其刷屏证据。
建议单开一条排查项（不在本条验收内，未擅改）。

## 6. 验收对照
- ① 逐文件实测可回收量+压缩比（dry-run，先不真删）：✔ §1 表 + dry-run 原文，未真删。
- ② copytruncate 对 launchd O_APPEND 日志安全性，对 launch_gui/feishu **另证**：✔ §2 逐文件增长特征（feishu 14280 重启单调增长；launch_gui 孤儿走删原文路径）。
- ③ 待批清单（每日志：阈值/保留份数/归档目录），不擅改 plist/生产配置：✔ §3，零执行零改配置。
- ④ 说明与 R349 待批项关系：✔ §4，确认互补无重叠。

## 7. 现场账
- 处理前：`logs/` 1.7GiB，数据卷 412Gi 已用 / 18Gi 可用 / 96%。
- 批准并执行 §3 后预期：`logs/` ≈ 120~150MiB（含 ~112MiB 归档），**净腾出 ~1.55GiB**。
- 本报告仅 dry-run/只读，磁盘状态未改变。

## 8. 记忆更新建议（待审，未擅自落地）
> 依据自主 SOP §权限边界：修改 global_mem / memory 下 SOP 属「需写入报告待审」。
> 故本节仅为**建议**，未直接改任何记忆文件（本轮曾误直接 patch L1，已回滚）。

- **建议位置**：`memory/global_mem_insight.txt` → 「定时/自主/反射」行 → 「日志压缩 tools/log_compress.py(...)」子句末尾追加一句：
  > `;★R359大日志治理待批:logs/=1.7G,四长跑日志净回收≈1.55G(launch_gui.log=1.13G孤儿死日志无plist引用冻结自06-04最强,feishu 407M/scheduler 78M走copytruncate),lsof本机恒空不可用→改文件增长特征证O_APPEND`
- **理由**：①「launch_gui.log 是无 plist 引用的孤儿死日志」属跨会话环境事实，未来可直接省掉再探测；②「lsof 本机恒空不可用」是工具级避坑，避免后续重复踩；③与既有「日志压缩」条目同主题，就地补一句最省 token。
- **待批准动作**（本轮未做）：①应用待批清单 §3 的压缩阈值/保留策略；②helper 第29行改指 tools/（R349 遗留，与本条互补）；③单开 「ga_api_server 崩溃循环」排查项。
