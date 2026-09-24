# 冲浪·真冷门工具实测：QoS 调度 `taskpolicy` + 内存剖析三件套 `footprint/heap/leaks`

- 日期：2026-09-25 ｜ 机器：macOS 15.0 (24A8332) arm64 ｜ 话题数：2（按 TODO 限制）
- 口径声明：本文所有**双引号内的英文**均为逐字引用，语料 = 本机 man 输出 + 本轮实测原始输出，原件落在 `temp/coldtools_evidence/`（`man_*.txt` + `raw_evidence.txt` + `raw_evidence2.txt`）；所有数字均为当轮命令原始输出的粘贴，复跑命令随文给出。
- 本轮**零改动**：未改源码/plist/定时任务，未执行任何破坏性操作，未推送任何东西（完成通知走收尾 `complete_task` 的既有通道）。

## 0. 结论速览

1. **话题① `taskpolicy`（/usr/sbin）**：系统自带、免安装、**免 root**（对同用户进程）。能把整棵进程树降到后台 QoS——实测 `ps -o pri` **20 → 4**；支持**对已在跑的进程**（`-p`）与**挂起启动**（`-s`）；包装后行为不变（A/B 输出 md5 相同）。
2. **话题② `footprint`/`heap`/`leaks`（/usr/bin）**：同样自带免 root（限同用户进程），**秒级只读**。它给的 `phys_footprint` 与 `ps` RSS 是**两个口径**：在自家 GA 前端进程上实测 **17.3 GiB vs 18.8 MB**（差约 1000 倍），差在"已换出(swapped)的脏页"——`ps` 会系统性低估长期 idle 的常驻进程。
3. 冷门度取证：`~/.zsh_history` 命中 **0**；`tools/ sche_tasks/ frontends/ agentmain.py ~/Library/LaunchAgents` 命中 **空** ⇒ 这两个工具我们**从未用过**，也从未接进自动化。
4. 负结果（替后来者省时间）：`ktrace` 在本机**必须 root**（rc=77）；`eslogger` 在 `/usr/bin`（**不在** `/usr/sbin`，我第一次路径写错，见 §4）；`sc_usage` 需 root；`kdump`/`latencystat` 本机不存在。

## 1. 选题过程（8 个候选中只有 2 个"免 root 真能用"）

| 候选 | 位置 | 免 root 实测结论 |
| --- | --- | --- |
| `taskpolicy` | /usr/sbin | ✅ 可用（本轮话题②… 见 §2） |
| `footprint` / `heap` / `leaks` / `vmmap` | /usr/bin | ✅ 可用（限同用户进程；本轮话题①… 见 §3） |
| `ktrace` | /usr/bin | ❌ 必须 root |
| `eslogger` | /usr/bin | ⚠️ 仅 `--help`/`--list-events` 免 root（收事件仍需 root） |
| `sc_usage` | /usr/bin | ❌ 必须 root |
| `praudit` | /usr/sbin | 仅能解析 ktrace 产物 ⇒ 随 ktrace 一起否掉 |
| `kdump` / `latencystat` | — | 本机不存在 |

冷门度取证（可复跑）：

```bash
grep -c -E 'taskpolicy|footprint|leaks' ~/.zsh_history
# → 0
grep -rl -E 'taskpolicy|/usr/bin/footprint|/usr/bin/leaks' \
     tools/ sche_tasks/ frontends/ agentmain.py ~/Library/LaunchAgents
# → (空)
```

## 2. 话题① `taskpolicy`：给进程树套后台 QoS（免 root）

### 2.1 man 正文提炼（不是标题搬运）

- 定位原文："execute a program with an altered I/O or scheduling policy or change settings of already running process"
- **最值钱的一句**是继承语义："All children of the specified program also inherit these policies." ⇒ 套在 wrapper 上，整棵进程树一起降级，不必逐个 `renice`（这正对 GA 的"反射器拉起子脚本"结构）。
- 参数速查（取自 man 的 OPTIONS）：`-d/-g` 是 I/O 策略（`default`/`throttle`）；`-c` 是 QoS clamp，取值原文："The argument can either be an integer, or a symbolic string like"，实测三个符号值均可；`-b` = `PRIO_DARWIN_BG`；`-B` 反向退出 BG；`-p` 原文："Change settings for the process specified by pid."；`-s` 原文："Run the program with an initial suspended state by calling"，即 POSIX_SPAWN_START_SUSPENDED。

### 2.2 实测命令与原始输出（全部免 root）

A/B 对比（同一条命令，未包装 vs 包装）：

```bash
/bin/sleep 8 & A=$!; /usr/sbin/taskpolicy -b /bin/sleep 8 & B=$!; sleep 0.7
echo "plain pri=$(ps -o pri= -p $A)  bgq pri=$(ps -o pri= -p $B)"
# → plain pri= 20  bgq pri=  4
```

对**已在跑**的进程生效（`-p`）：

```bash
/bin/sleep 6 & P=$!; echo "before=$(ps -o pri= -p $P)"
/usr/sbin/taskpolicy -b -p $P; sleep 0.4; echo "after=$(ps -o pri= -p $P)"
# → before= 20 / after=  4
```

挂起启动（`-s`）+ 需要时放行：

```bash
/usr/sbin/taskpolicy -s /bin/sleep 5 & Q=$!; sleep 0.5; echo "stat=$(ps -o stat= -p $Q)"
kill -CONT $Q; sleep 0.3; echo "stat_after_CONT=$(ps -o stat= -p $Q)"
# → stat=T   / stat_after_CONT=S
```

取值域与**负控**（证明它真在校验，不是静默接受）：

```bash
for v in utility background maintenance bogus; do printf "%-12s rc=" "$v"
  /usr/sbin/taskpolicy -c $v /bin/echo -n >/dev/null 2>/tmp/e; echo -n "$?  "; head -c 90 /tmp/e; echo; done
# → utility rc=0 / background rc=0 / maintenance rc=0
# → bogus rc=64  taskpolicy: Could not parse 'bogus' as a QoS clamp
```

**行为不变性 A/B**（关键：证明它只动调度属性、不改变程序语义）：

```bash
A=$(python3 pending_decisions/view_pending.py --aging 2>&1 | md5)
B=$(/usr/sbin/taskpolicy -b -c background python3 pending_decisions/view_pending.py --aging 2>&1 | md5)
# → plain=14261aff0aa747a6807b2d044c354362 / bgq  =14261aff0aa747a6807b2d044c354362（IDENTICAL）
```

### 2.3 能否装机

**系统自带，无需安装**：`/usr/sbin/taskpolicy`（119,008 B，owner `root:wheel`，2024-08-23）；`pkgutil --file-info` 只给 volume/path、**无包收据** ⇒ 非 pkg 安装物；CLT 目录（`/Library/Developer/CommandLineTools/usr/bin/`）里也**没有**同名件。⇒ 无"装/卸"概念，随 macOS 走。

### 2.4 本机怎么用（3 条即用 + 1 条待批）

1. **给心跳/反射类脚本降级**：`/usr/sbin/taskpolicy -b -c background python3 <脚本>`。收益=不与前台应用抢时间片/I/O；对我们的常驻 `sleep` 循环等价收益是"更安静"。
2. **事后补救已跑起来的长任务**：`taskpolicy -b -p <pid>`（实测 pri 立刻 20→4），比 kill 温和。
3. **"先挂起、择机放行"**：`taskpolicy -s <cmd>` 起成 T 态，需要时 `kill -CONT`（实测）——可配合我们的懒启动脚本。
4. **待批提案**：给 `sche_tasks/` 里低优先级任务（efficiency_monitor / roi_monitor 类）的 wrapper 加 `-b -c background`。**改 wrapper/plist 属待批**，本轮未动。

## 3. 话题② `footprint` / `heap` / `leaks`：秒级只读内存剖析

### 3.1 man 正文提炼

- `footprint`：定位原文 "gathers memory information about one or more processes"；口径是 "emphasizes memory considered 'dirty' by the kernel for purposes of accounting."；权限边界原文 "footprint must be run as root when inspecting processes that are not owned by the current user." ⇒ **同用户进程免 root**（实测：查自己的子进程与自家前端进程均通）。`-j path` 原文 "also save a JSON representation of the data to the specified path" ⇒ 可落 JSON 做时序。
- `heap`：定位 "List all the malloc-allocated buffers in the process's heap"；条件 "If the target process is running with MallocStackLogging" 才能把"非对象分配"归到类型/调用者，否则只给 zone/class 汇总。
- `leaks`：定位 "Search a process's memory for unreferenced malloc buffers"；正文里最贴我们的一句是："Leaks are particularly dangerous for long-running programs, for eventually the leaks could fill memory and cause the application to crash." ⇒ 我们恰好有一群常驻 Python（fsapp / hub.py / dingtalkapp / agentmain）。

### 3.2 实测（先自造对象，再看真实进程）

子进程（自造 300k 个对象，三个工具各跑一次）：

```bash
python3 -c "import random,time; d=[random.randint(0,9) for _ in range(300000)]; time.sleep(25)" &
sleep 6; P=$(pgrep -n -f random.randint)
echo "pid=$P ps_rss=$(ps -o rss= -p $P)KB"
/usr/bin/heap $P 2>&1 | sed -n '1,18p'
/usr/bin/leaks $P 2>&1 | grep -iE 'nodes malloced|leaked'
/usr/bin/footprint -p $P 2>&1 | grep -E 'phys_footprint|TOTAL'
# → pid=24125 ps_rss= 12416KB
# → heap:  Physical footprint:         8544K
# → leaks: Process 24125: 3073 nodes malloced for 4236 KB
#           Process 24125: 0 leaks for 0 total leaked bytes.
# → footprint: phys_footprint: 8544 KB
```

真实 GA 进程（前端 `fsapp.py`，pid 617，launchd 拉起，已跑 1 天 21 小时，全程只读）：

```bash
ps -o pid=,rss=,vsz=,comm= -p 617
# →   617  18800 435368752 /opt/anaconda3/bin/python3
/usr/bin/footprint -f bytes -p 617 2>&1 | grep phys_footprint
# → phys_footprint: 18525030616 B          （≈ 17.3 GiB）
/usr/bin/footprint -p 617 --sort dirty | sed -n '/Category/,/MALLOC_SMALL/p'
# → MALLOC_MEDIUM 15 GB / MALLOC_NANO 1231 MB / MALLOC_TINY 721 MB
#   untagged (VM_ALLOCATE) 306 MB / MALLOC_SMALL 198 MB
/usr/bin/vmmap -summary 617 | sed -n '12,34p'
# → Physical footprint:         17.3G
# → Writable regions: Total=23.8G written=17.0G(72%) resident=23.8M(0%) swapped_out=17.2G(72%) unallocated=6.6G(28%)
# → REGION 表：MALLOC_MEDIUM 虚拟 15.5G / RSS 384K / SWAPPED 14.8G
```

**洞察（本轮最值钱的一条）**：`footprint` 的 `phys_footprint` 与 `ps` RSS **不可互相换算**——本例 18.8 MB vs 17.3 GiB，差约 1000 倍；`vmmap` 给出了原因：这批内存 **swapped_out=17.2G(72%)**，即 `footprint` 把"已换出的脏页"也算进占用，而 RSS 只数驻留页。两个推论：①排查内存问题时**只看 `ps` 会漏**；②但也**不能**把 17.3 GiB 直接读成"内存爆炸"（它可能只是 malloc zone 的惰性预留被记账，swap 占比高说明系统已把它挪走）。**"是否异常"未定**，已列为下轮候选（重启前后对比 / 带 MallocStackLogging 取样定位）。速度实测：`footprint` ≈ 0.01s、`leaks` ≈ 1.03s ⇒ 够轻，可进日常巡检。

### 3.3 能否装机

**系统自带，无需安装**：`/usr/bin/{footprint,heap,leaks,vmmap}`（`root:wheel`，2024-08-23）；CLT 目录无同名件；`pkgutil --file-info /usr/bin/footprint` 无包收据。

### 3.4 本机怎么用

1. **常驻进程内存基线**：`for p in $(pgrep -f 'fsapp|hub.py|dingtalkapp|agentmain'); do footprint -p $p | grep phys_footprint; done`，用 `-j` 落 JSON 做时序（**接进 efficiency_monitor 属改动，待批**）。
2. **疑似泄漏定位**：`leaks <pid>`（只读、秒级），输出形如 `N nodes malloced for X KB` 与 `M leaks for Y total leaked bytes`（实测示例：3073 nodes malloced for 4236 KB）。
3. **大对象排查**：`heap -sortBySize <pid>`；要类型/调用栈就先让目标进程带 `MallocStackLogging=1` 启动（会变慢，别给常驻进程常开）。

## 4. 负结果清单（同样是产出）

- **`ktrace` 免 root 路线否掉**：`ktrace trace -c /bin/echo hi` → `ktrace must be run as root when tracing the current system`（**rc=77**）；且 macOS 15 改了语法，旧式 `ktrace -f` 被拒（实测输出为 `ktrace: unknown command` 后跟被反引号包裹的选项名）（子命令形如 `ktrace info|trace|dump`）；`ktrace info` 本机还报 "failed to get kernel tracing information: Too many levels of remote in path"（rc=1）。
- **`eslogger` 路径坑（我自己踩的）**：真实路径是 `/usr/bin/eslogger`（1,217,184 B）；我起初按 `/usr/sbin/eslogger` 探测得到"文件不存在"，属**自摆乌龙**，记此以防复踩。免 root 只有 `--help`/`--list-events`（其 ARGUMENTS 原文举例 "e.g. exec fork exit open close iokit_open ..."）。
- `sc_usage`：`/usr/bin/sc_usage` 存在，但 "must be run as root"。
- `kdump` / `latencystat`：本机不存在。

## 5. 下轮 TODO 候选（"发现好工具→加油测项"）

1. **GA 常驻进程内存基线 + fsapp 17.3 GiB 脏页疑点排查**（只读起步：`footprint -j` 时序 + 目标进程重启前后对比；判据=`phys_footprint` 是否随重启回落、swapped 占比是否稳定）。
2. **（待批）`taskpolicy` 接入 `sche_tasks` 低优先级任务**：wrapper 加 `-b -c background`；收益=不抢前台 QoS，风险=这些任务自身完成时间可能拉长（心跳类可接受）。

## 6. 可复跑与产物

- 语料：`temp/coldtools_evidence/man_{taskpolicy,footprint,heap,leaks,ktrace,praudit,eslogger}.txt`
- 证据：`temp/coldtools_evidence/raw_evidence.txt`（8 组）+ `raw_evidence2.txt`（6 组），每条含命令、`rc`、原始输出
- 全部命令只读；唯一副作用是 `-s`/A-B 演示起了几个 `sleep` 进程，均已 `kill`。

## 7. 边界与未验证

- "免 root"仅覆盖**同用户进程**；跨用户/系统进程按 man 需 root（未测，未用 sudo）。
- 17.3 GiB 只作**疑点**，不作"泄漏"结论：未查 malloc zone 记账细节、未做重启对照。
- `leaks`/`heap` 对运行中 Python 的准确度**未做注入负控**（真泄漏 vs 未回收缓存不可区分）。
- 未验证"未装 Xcode/CLT 的机器上这四件套是否存在"（本机有 CLT，但这四件套不在 CLT 目录）。
- `praudit` 只能吃 ktrace 产物 ⇒ 随 ktrace 一起搁置，未单测。
