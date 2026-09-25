# R72 后台长任务工具化：tools/bg_task.py

日期：2026-09-25 | 任务来源：TODO 第 10 批三路评审（评分 5/8/8 = 7.00，本批最高）
产物：`tools/bg_task.py`（530 行，sha256 `f2161b30c9b6174e11de6f670f79ae464dcc2665bc488b410f49322011109521`）

---

## 一、结论（先说是什么，再说是怎么验的）

把 RULES#15 那条靠人记的配方——"起后台 + 事后轮询"——固化成一个只有标准库依赖的 CLI：
`start / status / tail / stop / list`（外加 `selftest`）。它**不重建** `memory/tmux_utils.py` 已有的人机
会话能力（见第二节的能力比对表），只补 tmux 那条路按设计就补不上的三件事：

| 缺口 | 内容 | 为什么 tmux 路线做不到 |
|---|---|---|
| G1 | **退出码语义**：任务结束后拿到 exit code，区分"正常退出(0)"与"异常退出(≠0)" | tmux 的 `session_exists` 只说明会话在不在，命令的结果拿不到 |
| G2 | **PID 身份与复用检测**：登记 pid **同时**登记 `ps -o lstart=` 启动时刻，lstart 不符即判 PID 被复用 | tmux 视角里只有 pane/session，没有"这个 pid 还是不是当初那个进程"的概念 |
| G3 | **持久日志文件 + 机器可读状态**：输出落文件（可 tail/grep/被其它工具读），状态落 JSON 注册表 | tmux 输出在内存 pane 缓冲里；本机 `~/.tmux.conf` 写死 `history-limit 50000`（实测见 §2.3），超限即截断，且不是文件 |

另外把"本机无 setsid"这条踩过的坑写死在代码注释、docstring、`--help` 文案里（§5）。

---

## 二、验收①：与 `memory/tmux_utils.py` 的能力比对表（判定：只补缺口，禁重复建设）

### 2.1 事实基线（先测再用，不凭印象）

```
which tmux: /opt/homebrew/bin/tmux      tmux -V: tmux 3.6a
which setsid: NOT FOUND                 which nohup: /usr/bin/nohup
memory/tmux_utils.py: 287 行；导出 new_session/ensure_session/kill_session/session_exists/
  list_sessions/send_keys/capture_pane/run_command/wait_for_output/run_bg/get_output_lines
```

### 2.2 能力矩阵（按本任务需要的 10 个细项，逐项实测判定）

| # | 能力 | tmux_utils.py | tools/bg_task.py | 证据/备注 |
|---|---|---|---|---|
| 1 | 起后台任务（立即返回） | 有（`new_session` + `run_bg`） | 有 | 实测 start 耗时 0.01s |
| 2 | 查"还在不在" | 有（`session_exists`） | 有（`status`） | — |
| 3 | 看输出 | 有（`capture_pane`/`get_output_lines`） | 有（`tail`，读**文件**） | 语义同、载体不同（内存缓冲 vs 文件） |
| 4 | 停止 | 有（`kill_session`） | 有（`stop`：SIGTERM→宽限→SIGKILL） | tmux 是杀会话；本工具是杀登记的进程组并**验证**已消失 |
| 5 | 列举 | 有（`list_sessions`） | 有（`list`） | — |
| 6 | **退出码** | 无 | 有 | G1；实测 `EXITED_OK exit=0` |
| 7 | **PID 复用识别** | 无 | 有 | G2；负控④实测 `STALE_PID_REUSED` |
| 8 | **机器可读状态（JSON）** | 无 | 有 | `status/list --json` |
| 9 | 零外部依赖 | 否（需 tmux 服务端） | 是（仅标准库） | 实测：`PATH=/usr/bin:/bin`（无 tmux）下 start/status/tail 全通 |
| 10 | 交互式会话能力（attach / send_keys 注入） | **有**（本工具不提供） | 无 | 有意不实现——需要按键盘/看屏就用 tmux_utils |

### 2.3 判定过程（按验收①的规则机械套用）

* **动词级覆盖**：本任务点名的 5 个动词（start/status/tail/stop/list）在 `tmux_utils.py` 里全部有对应实现
  ⇒ 重叠 **5/5 = 100% ≥ 80%**。按验收①"重叠≥80% 则改为只补缺口"的要求，**禁止**把 tmux 那套再实现一遍。
* **细粒度覆盖**：10 个细项里 tmux_utils 覆盖 6 项、缺 3 项（G1/G2/G3），第 10 项是本工具**故意不提供**的
  （回给 tmux_utils）⇒ 实算重叠 **6/10 = 60% < 80%**。
* **两种算法的落地动作一致**：都指向"不重复建设、只补缺口"。因此实现上：
  - 源码里 **0 处调用 tmux**（`grep -nE '(import tmux|\[.tmux.|subprocess.*tmux|which\(.tmux)'` → 无匹配）；
    文档字符串里 7 处提到 tmux 全是在讲"分工"，不是调用。
  - **不实现** session/pane 的原语（无 send_keys、无 capture_pane、无 attach）：交互式场景仍由
    `memory/tmux_utils.py` 负责。
  - 本工具与 tmux_utils 是**并列的两条路**，不是包装或替代（未做 tmux 后端委派——因为 tmux 那侧本来就没缺口，
    再包一层只会多一个失效点；这一条属于本轮的**取舍**，不是遗漏）。
* **反向确认（禁重复建设的实证）**：本工具在**没有 tmux 的 PATH** 下照常工作，说明它不是 tmux 的马甲：

```
$ env PATH=/usr/bin:/bin python3 tools/bg_task.py --state-dir /tmp/bt_notmux start --name notmux -- python3 -c "print('no-tmux-ok')"
[bg_task] started name=notmux pid=77122 pgid=77122 log=/tmp/bt_notmux/logs/notmux.log
$ env PATH=/usr/bin:/bin python3 tools/bg_task.py --state-dir /tmp/bt_notmux status --name notmux
notmux               EXITED_OK        pid=77122    exit=0    log=11B  已退出，退出码 0
$ env PATH=/usr/bin:/bin python3 tools/bg_task.py --state-dir /tmp/bt_notmux tail --name notmux
no-tmux-ok
```

### 2.4 顺带实测：pane 缓冲到底能扛多少（G3 的证据）

```
$ python3 /tmp/probe_cap30.py     # 在 tmux 会话里打印 3 万行后 capture-pane -S -
capture -S - 总行数: 30003 | 其中 L 行: 30000
最早 L 行: L1 | 最后: L30000
history-limit 显式值: history-limit 50000
tmux.conf 是否存在 history-limit: /Users/cuip/.tmux.conf
```

结论：本机 3 万行完整保留（没截断），但上限是**配置项**（`~/.tmux.conf` 写的 50000），换个环境/改配置就变，
且它终究是内存缓冲而非文件 ⇒ 需要"可 tail/grep/被别的工具读"的场景仍应落文件（G3 成立）。

---

## 三、验收②：真起、真停、真分三态（真机输出原样粘贴）

### 3.1 真起 + 脱离父进程 + 独立进程查状态

```
=== 1) 真起长任务（25s 心跳，每秒一行）===
rc= 0
[bg_task] started name=r72demo pid=75092 pgid=75092 log=temp/bg_task/logs/r72demo.log

=== 2) 脱离父进程证据（ppid 应为 1；父进程 python3 bg_task.py 已退出）===
75092     1 75092 Ss   00:00 /bin/bash temp/bg_task/wrappers/r72demo.sh

=== 3) 独立新进程查状态（RUNNING）+ tail ===
r72demo              RUNNING          pid=75092    exit=-    log=15B  运行中（etime=00:02）
hb-1
hb-2
hb-3
[bg_task] --- temp/bg_task/logs/r72demo.log 共 3 行，显示末 3 行
```

`ppid=1` = 发起进程早已退出、子进程被 launchd 收养，这就是"父进程（code_run）被超时强杀也不影响子进程"的实证。

### 3.2 真 stop（SIGTERM 路径）

```
=== 4) 真 stop（SIGTERM→宽限3s；再 status）===
[bg_task] r72demo: 停止后状态=GONE_UNKNOWN （SIGTERM 即退出） ...
stop 后 ps 复核: (进程已不存在)
```

### 3.3 正常退出三态

```
=== 5) 正常退出三态（短任务）===
r72ok                EXITED_OK        pid=75353    exit=0    log=4B  已退出，退出码 0
NAME                 STATE            PID      EXIT  LOG_B      STARTED
r72demo              GONE_UNKNOWN     75092    -     15         2026-09-25 09:09:21
r72ok                EXITED_OK        75353    0     4          2026-09-25 09:09:23
```

### 3.4 三态的完整定义（本工具的真值口径）

| 状态 | 判据 | 用例 |
|---|---|---|
| `RUNNING` | `ps` 有该 pid ∧ stat 非 Z ∧ lstart 与登记值**一致** | §3.1；§4 自测 1) |
| `EXITED_OK` / `EXITED_ERR` | 退出码文件已落盘，值 =0 / ≠0 | §3.3；§4 自测 3) |
| `GONE_UNKNOWN`（进程不存在） | `ps` 无该 pid ∧ 无退出码文件 | §3.2（被 SIGTERM 收掉，来不及写码） |
| `STALE_PID_REUSED`（PID 被复用） | `ps` 有该 pid ∧ lstart 与登记值**不一致** | §4 自测负控④ |
| `ZOMBIE` | `ps` stat 以 Z 开头 | 自测观察态 |
| `NO_SUCH_TASK` | 注册表无该名字，rc=3 | §4 自测负控①② |

设计要点：**判活一律走 `ps`，不用 `os.kill(pid,0)`**——后者对僵尸进程和跨权限进程会给出误导性的"活着"。
`os.killpg` 只用于"停止"，且只打本工具登记过的进程组（绝无泛杀）。

---

## 四、验收③：负控与自测（12/12，含 4 条负控）

```
$ rm -rf /tmp/bt_selftest2 && python3 tools/bg_task.py --state-dir /tmp/bt_selftest2 selftest
[selftest] PASS start 立即返回（<3s）且 rc=0 rc=0 耗时=0.01s
[selftest] PASS start 后状态=RUNNING（ps+lstart 匹配）
[selftest] PASS 日志文件已写入且含命令输出 log=st_run.log size=9
[selftest] PASS 自然退出后 EXITED_OK 且 exit_code=0
[selftest] PASS 负控①：status 不存在任务 rc=3
[selftest] PASS 负控②：tail 不存在任务 rc=3
[selftest] PASS 负控③：死 pid 登记 → GONE_UNKNOWN（不误报 RUNNING）
[selftest] PASS 负控④：PID 复用 → STALE_PID_REUSED（不误报 RUNNING）
[selftest] PASS stop 前 RUNNING
[selftest] PASS stop 后进程真消失（SIGTERM 路径）
[selftest] PASS stop 前 RUNNING（硬骨头）
[selftest] PASS 忽略 SIGTERM 的任务被 SIGKILL 收掉 （SIGTERM→SIGKILL 升级）
[selftest] 12/12 用例通过（state-dir=/tmp/bt_selftest2）
```

负控与验收③的对应关系：
1. **查不存在任务** → `status`/`tail` 均 rc=3 且打印现有任务列表（不静默返 0）；
2. **僵尸/陈旧 pid 文件** → 死 pid 登记必须报 `GONE_UNKNOWN`、**不**报 RUNNING（负控③）；
3. **日志确已写入** → start 后 1s 断言日志非空且含命令自己打印的标记（负控，防"起了但输出丢了"）；
4. **额外加码**：PID 被复用必须报 `STALE_PID_REUSED`（负控④）；忽略 SIGTERM 的进程必须靠 SIGKILL 才收得掉（第 8 条用例）。
   自测全程在**隔离的 `--state-dir`** 里跑，不污染真注册表、不碰任何非自己起的进程。

---

## 五、验收④：写死"本机无 setsid"

* 模块 docstring 第 18-19 行：说明 `which setsid` = NOT FOUND，`setsid nohup ... &` 在本机会静默失败/不生效，
  正确做法是 `subprocess.Popen(..., start_new_session=True)`。
* 代码处（`cmd_start` 前一行的 ★ 注释）同款说明，避免后人"顺手改回 setsid"。
* `--help` 描述行也带这句（`argparse` description），SOP 侧对应条目在 §6 给出建议文本。
* 实测支撑：`which setsid` → NOT FOUND（本轮采集）；`Popen(start_new_session=True)` → `ppid=1`（§3.1）。

---

## 六、SOP 建议（待用户批准后落 memory）

RULES#15 现文本写的是 `setsid nohup xxx > log 2>&1 < /dev/null & disown`；本机无 setsid，建议把该行改为：

```
① 起后台（立即返回）: python3 tools/bg_task.py start --name <job> -- <cmd...>
   老配方若必须手写: python3 -c "import subprocess;subprocess.Popen(['/bin/bash','-lc','<cmd> > log 2>&1'],start_new_session=True,stdin=subprocess.DEVNULL)"   # 本机无 setsid
② 之后每轮看一次: python3 tools/bg_task.py status --name <job>; python3 tools/bg_task.py tail --name <job> --lines 30
```

本次**未**改动 RULES/SOP 文本（属于 memory 资产，按规矩须先获批准），只在此登记建议。

---

## 七、已知边界与残余风险（都在现实里验过，不粉饰）

1. **被强杀的任务没有退出码**：`GONE_UNKNOWN` 只能说明"进程没了且没留码"，无法区分"外部 SIGKILL"与"机器重启"。
   这是设计边界（SIGKILL 语义本身无可捕获），不是缺陷；要精确归因得靠日志末尾内容人工判断。
2. **注册表只增不删**：任务条目会一直留着（含已退出任务）。当前规模（个位数）无成本问题；无自动清理是有意为之
   （清掉就丢了历史与日志指针）。日志文件同理，不自动删。
3. **`tail` 是快照不是流**：按 RULES#15 的用法（每轮调一次、立即返回）设计，没有 `-f`。需要实时跟随请用 `tail -f <log 路径>`。
4. **未做 tmux 后端委派**（§2.3 末条的取舍）。若后续希望"一条命令既能起交互式会话又能拿退出码"，
   应在 tmux_utils 侧加退出码落盘，而不是在本工具里塞 tmux 依赖。
5. **PID 复用检测依赖 lstart 精度（秒级）**：同一秒内 pid 被回收再分配会漏判。概率极低；真出现时表现是误报 RUNNING。
   未做更重的校验（如 `ps -o lstart` + 启动命令指纹），因为性价比不划算；这条留给需要时有意识加强。

---

## 八、复跑命令（读者可自行验证）

```bash
python3 tools/bg_task.py --state-dir /tmp/bt_selftest3 selftest          # 12/12 自测（隔离目录）
python3 tools/bg_task.py start --name demo --shell 'for i in $(seq 1 25); do echo hb-$i; sleep 1; done'
python3 tools/bg_task.py status --name demo --json                       # 机器可读
python3 tools/bg_task.py tail --name demo --lines 5
python3 tools/bg_task.py stop --name demo --grace 3                      # SIGTERM → 升级 SIGKILL
python3 tools/bg_task.py list
shasum -a 256 tools/bg_task.py                                           # f2161b30c9b6174e11de6f670f79ae464dcc2665bc488b410f49322011109521
```
