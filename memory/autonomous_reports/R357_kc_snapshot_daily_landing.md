# R357 · knowledgeC 每日快照落地（零新增定时 + 当日补采）

- 日期：2026-09-23　｜　TODO#9（工程｜knowledgeC 每日快照落地：零新增定时 + 当日补采）
- 状态：**任务完成**（验收 ①②③④ 逐条对照见下）；**未新增任何 launchd 定时**，两个「真正开始自动采集」的开关均留待批。

---

## 1. 结论（一句话）

今天这一天已经**真采下来**了（18277 对象 / verify PASS / 与昨天 +814 对象），
并且我把「以后每天自动采」做成了**挂进既有反射循环**的现成开关——**已经 dry-run 演示通过、但故意没有打开**，
外加一条更省事的备选（每天 09:30 的 launchd，R347 就已备好 plist）。开与不开，只差您一句话。

---

## 2. TODO#9 验收逐条对照

| # | 验收要求 | 实测结果 |
|---|---|---|
| ① | **不新增 launchd**，把快照挂进**既有反射循环**（仿 `reflect_scripts/07_l2_gate.py`）并**先 dry-run 展示** | ✅ 零新增 launchd。两条路径均已备好并 dry-run：**①-A** 新增 `reflect_scripts/08_kc_snapshot.py`（完整反射契约，真机 `agentmain --reflect` 加载通过）；**①-B**（推荐）在**既有** `07_l2_gate.py` 的 `check()` 里加 4 行钩子 → 复用**已经在跑**的 `com.cuip.genericagent-l2-gate`（30min 一轮）。补丁 `temp/R357_kc_hook_07.patch` 已生成，**未应用** |
| ② | 本轮执行一次**当日补采**，`verify` rc=0 | ✅ `temp/kc_snapshots/2026-09-23/` 已落盘：13,119,488 B、对象 18,277、sha `f849e621adbc`、`verify` **rc=0 / PASS** |
| ③ | 「连续 ≥3 日期累积」**不作为本轮验收** | ✅ 未声称达成；当前累积 **2 天**（09-22、09-23），第 3 天取决于开关是否打开 |
| ④ | 新增定时的批准件**仍单独留待批** | ✅ `temp/pending_decisions/DECISIONS_R347_kc_snapshot_scheduling.md` 已按本轮实况**追加更新**（仍未安装任何东西） |

---

## 3. 本轮真实动作与证据

### 3.1 当日补采（真跑）

```
$ ls -l ~/Library/Application Support/Knowledge/knowledgeC.db*
SRC: -rw-r--r--  1 cuip  staff  ...  knowledgeC.db        (Sep 22 22:25)
SRC: -rw-r--r--  1 cuip  staff  ...  knowledgeC.db-wal    (Sep 23 01:13)   ← 今日变更都在 WAL 里
$ time python3 tools/kc_snapshot.py snapshot
[OK] ... 对象数=18277  覆盖日期=33 天 (2024-12-27 → 2026-09-22)  本次 revision 数=1
real 0m0.095s
$ python3 tools/kc_snapshot.py list
2026-09-22   17463   8c3f55f4c416   13119488  (revisions=1)
2026-09-23   18277   f849e621adbc   13119488  (revisions=1)
$ python3 tools/kc_snapshot.py verify
[PASS] temp/kc_snapshots/2026-09-23
  sha 一致=True integrity=ok 对象数=18277(manifest 18277) 侧车残留=无      rc=0
```

- **单日增量**：17463 → 18277（**+814 对象**）；可用日期覆盖仍是 33 天（2024-12-27 → 2026-09-22）
  —— 今天新产生的行为要到明天才会在库里成"新的一天"，这也再次印证「**不采就永久丢**」。
- 源库全程**只读**：只做 `cp`（含 `-wal/-shm`），未 checkpoint、未 VACUUM、未在源目录建任何文件。

### 3.2 幂等与边界复现（真跑）

| 场景 | 输入 | 实测输出 | 判读 |
|---|---|---|---|
| 同日重跑 | `snapshot`（当天已有同日 revision） | `[skip] 内容与上次快照一致（sha=f849e621adbc…）`，rc=0 | **幂等✔**，不重复占空间 |
| 指定不存在的日期 | `snapshot --date 1999-01-01` | **真写了一份**（该目录无 manifest ⇒ 无从判同 ⇒ 不 skip） | 我原先假设这里会「留空目录」，**被实测证伪**；测试目录已 `rm -rf` 清理 |
| prune 预览 | `prune`（默认 dry-run） | `保留 2 天 / 待删 0 天` | 策略生效、零删除 |

> 记一笔：`--date` 是"命名归档日期"，不是"从库里挑那天数据"，别当时间旅行用。

### 3.3 反射挂载（dry-run 展示，未生效）

**A. 新增 `reflect_scripts/08_kc_snapshot.py`**（已写盘，未注册任何 plist ⇒ **不会自动跑**）

```
$ python3 reflect_scripts/08_kc_snapshot.py --dry
today_done=True  → 将执行: (今日已采，跳过)
$ # 真反射循环加载（后台起 → 读日志 → 按 PID 精确 kill）
[Reflect] loaded reflect_scripts/08_kc_snapshot.py        ← 契约加载成功
check() 返回 = None | 耗时 0.07s | notify 次数 = 0        ← 不唤起 Agent、不误报警
$ # 稳态成本（当天已采后的纯守卫）
_today_done() 单次 0.026 ms
```

**B.（推荐）把钩子加进既有的 `07_l2_gate.py`** —— 补丁已生成、**未应用**

```
$ python3 temp/R357_kc_hook_07_apply.py            # 默认 dry-run（不写盘）
目标: reflect_scripts/07_l2_gate.py  (原 sha=bc2196a696f2  47 行)
  - A1 import 已注入 / A2 钩子定义 已注入 / A3 check() 调用 已注入
将变为 65 行（注入）
DRY-RUN：未写盘。加 --apply 执行。

# 全程在一个**副本**上真跑（仓库里的 07 一字未动），注入/幂等/撤回三段都有：
复制 47 行 → --apply → 65 行           (sha bc2196a696f2 → e4bcb5e9355a)
再 --apply  → 「已存在标记行 ⇒ 视为已注入，跳过」无变化        ← 幂等 ✔
真跑 check() → check()= None | 0.07s | notify= 0               ← 不调 Agent、不误报警
--revert    → 回到 47 行，sha = bc2196a696f2（与原文件**逐字节一致**）✔
再 --revert → 「无标记行 ⇒ 视为未注入，跳过」                 ← 幂等 ✔
撤回后 py_compile OK；仓库 07 的 sha 自始至终 = bc2196a696f2
```

为什么 B 比 A 好：反射脚本**必须各自挂一个 plist 才会跑**（`agentmain --reflect <script>`），
所以 A 名义上"零新增定时"做不到，仍然要加一个 launchd；而 B 的宿主 `com.cuip.genericagent-l2-gate`
是 **30 分钟一轮、无条件跑 `check()`** 的既有常驻（`RunAtLoad` + `KeepAlive`），把 4 行钩子塞进去
= **真正零新增定时**，且顺带修掉了 R347 里方案 B 自陈的风险（"反射器有活时 30min 才跑一轮，可能当天跑不到"）。

启用与回滚（**待您批准后才执行**）：

```bash
cd /Users/cuip/projects/GenericAgent

# 启用（B）：注入 4 行钩子 → 反射进程按文件 mtime 自动热重载，无需 bootout
#   源码证据：agentmain.py:297-303 每轮 `if getmtime(args.reflect) != _mt:` 则 `exec_module(mod)` 并打印 `[Reflect] reloaded`
python3 temp/R357_kc_hook_07_apply.py            # 先 dry-run 看会改什么
python3 temp/R357_kc_hook_07_apply.py --apply    # 真注入（自动备份 07_l2_gate.py.bak-r357）

# 回滚（B）：本脚本的 --revert（**不是 git checkout** —— 07_l2_gate.py 不在 git 跟踪内，
#           `git status` 显示 `?? reflect_scripts/07_l2_gate.py`；原计划的 git apply/checkout 走不通，
#           已改成这个带 sha 校验的自带脚本）
python3 temp/R357_kc_hook_07_apply.py --revert --apply

# 回滚（A）：rm reflect_scripts/08_kc_snapshot.py
# 回滚（C/plist）：launchctl bootout gui/$(id -u)/com.cuip.genericagent-kc-snapshot
# 数据：rm -rf temp/kc_snapshots/   （全部是副本，源库从未被写过）
```

---

## 4. 待批清单（本轮的"要您决策"全在这里）

| # | 待批项 | 建议 | 代价/风险 |
|---|---|---|---|
| 1 | **打开每日采集**（三选一）：<br>A `08_kc_snapshot.py` + 新 plist<br>**B `07_l2_gate.py` 加 4 行钩子（推荐）**<br>C 装 R347 已备好的 09:30 plist | **选 B** | 每天 1 次 `cp` 13 MB（0.1 s）→ 磁盘 +13 MB/天，靠 prune 兜到 ≈200 MB/年；B 不新增任何定时 |
| 2 | **快照放哪儿**：现在在 `temp/kc_snapshots/` | 建议迁出 temp（如 `~/.local/share/kc_snapshots/`） | `temp/` 在 `.gitignore` 内、**零版本控制**，历史上被整体清理过一次；快照放这儿等于"每天辛苦采，一次清理全没了"。与 TODO#8（备份缺口）是同一个坑，建议一并决策。**改动涉及 `tools/kc_snapshot.py` 的 ROOT 常量 ⇒ 按「改自身源码先请示」留待批** |
| 3 | 历史 09-22 快照要不要跟着迁 | 跟着迁（`mv` 即可） | 无 |
| 4 | prune 是否也自动跑 | **暂不**（删除不可逆） | 当前 2 天、待删 0 天，不急 |

---

## 5. 磁盘与容量记账（今天实测）

- 数据卷 `/System/Volumes/Data`：**410 Gi / 460 Gi（97% 已用，剩 ~14 Gi）**
- 单份快照 **13.1 MiB**；当前 2 天 = **25 MiB**
- 增长模型：14 天全量（183 MiB）+ 之后每年 52 份（681 MiB）≈ **首年 0.84 GiB**
  → 在"只剩 14 Gi"的机器上，**这不构成新的压力**，但也不是"随手就能扔"的量级（≈6% 的可用空间）。

---

## 6. 未完成 / 残余风险（说清楚边界）

1. **日期累积仍只有 2 天**（09-22、09-23）。"每天都有"这件事**在开关打开前不会发生**——本轮的交付是"开关就绪 + dry-run 证据"，不是"已经在自动跑"。
2. **07 钩子补丁只在我自己的副本上验证过**，没有真跑过"跨天"路径（把 `_today_done` 打桩为 False 的等价测试已过：0.07 s、静默、notify 0）。首个真实跨天效果要等启用后的第二天。
3. **失败通知未做真发验证**：`check()` 只在 rc≠0 时发飞书私信，本轮"零报警"是因为一直成功。通知链路本身沿用 07 的既有实现（已长期在用），但本脚本这条支路**未经真失败注入**。
4. 快照本身**没有任何异地/版本控制保护**（见待批 #2）。
5. **归档自检的判读**：`verify --ids 357` 会显示 `不一致 7275B vs 0B` —— 这**不是**归档损坏。
   它的对照源取自 **L4 会话归档**，而本轮会话尚未落盘 ⇒ 取不到正文（`src=None`）；且打印里的 "B" 实为**字符数**。
   R357 归档一致性的证据是**两树 sha 相等**（`8e2542…`）+ `sync` 实跑；历史报告侧 R3/R4/R5 为"逐字一致"，
   R1"缺失"/R2"不一致"是 R336 恢复期的既有遗留（与本轮无关）。

---

## 7. 给记忆的建议（**待审，未写入 memory/**）

建议在 L1「定时/自主/反射」段追加半句（约 30 字）：

> kc每日快照：R357 已交付 08_kc_snapshot.py + 07 钩子补丁（`temp/R357_kc_hook_07.patch`），
> 复用既有 l2-gate 反射（零新增定时）；当日补采已验证，**开关待批**；快照在 temp/（无版本控制）待迁。

同时建议在 L2 增补一条工具条目（`tools/kc_snapshot.py` 用法、`--date` 语义、skip 语义、prune 策略）。
以上均**未执行**，等您点头。

---

## 8. 本轮新踩到的坑（可复用教训）

1. **反射脚本不会"放进目录就生效"**：`reflect_scripts/*.py` 每个都由**各自的 launchd**用 `agentmain --reflect <path>` 拉起，没有任何目录扫描。所以"新建 08 就自动挂上了"是错觉，**想真正零新增定时只能往已常驻的 07 里加钩子**。
2. **`kc_snapshot.py snapshot` 的 skip 判据是"与上一次 revision 内容相同"**，与日期无关；指定一个从未有 manifest 的日期会**真的写一份**（不会留空目录）。想验证"某天有没有采"要看目录里有没有 `manifest`，不能只看目录存在。
3. **今日增量藏在 `-wal` 里**：源库主文件 mtime 还停在昨天 22:25，`-wal` 却更新到今天 01:13；`cp` 连 `-wal/-shm` 才拿得到今天的行为（正是既有记忆里那条"缺 -wal 会假空"的同一机理）。
4. **`prune` 默认 dry-run**，删除必须显式 `--apply`——符合"不可逆操作先问人"。
6. **归档自检的两个易误读点**：①`verify` 的对照源是 L4 会话归档 ⇒ **当前会话的报告查不到（0B）**，别当成丢了；
   ②它打印的 `7275B` 是**字符数**不是字节数（UTF-8 中文 ≈0.61×），别当成"大小不符＝损坏"。

5. **自写的 apply/revert 脚本第一次撤回失败**（`str.replace` 的参数顺序写反，撤回反而又插了一份）；
   抓出它的是"撤回后 sha 是否等于原始 sha"这一条**字节级断言**——若只测"注入后能跑通"就会把这个洞带到生产。
   教训：任何改写源码的脚本，验证必须包含**回滚后与原文件逐字节一致**。

---

## 附：本轮产物清单

- `reflect_scripts/08_kc_snapshot.py`（新增，反射契约，**未注册**）
- `temp/R357_kc_hook_07_apply.py`（注入/撤回脚本，默认 dry-run，幂等 + 自动备份，**未执行过 --apply**）
- `temp/R357_kc_hook_07.patch`（36 行 diff，供人工审阅，**未应用**）
- `temp/kc_snapshots/2026-09-23/`（当日快照 + manifest）
- `temp/pending_decisions/DECISIONS_R347_kc_snapshot_scheduling.md`（追加本轮实况）
- 本报告
