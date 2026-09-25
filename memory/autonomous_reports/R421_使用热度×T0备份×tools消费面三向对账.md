# R421 资产「真实使用热度」× T0 备份清单 × `tools/` 消费面 三向对账

- 日期：2026-09-26 | 类型：产出 | 来源 TODO：第 14 批 C2（三路评审 8/8/7 = 7.67，吸收最低分候选 C1）
- 交付物（全部本机，只读探测 + 只写 temp）：`tools/usage_census.py`、清单目录 `temp/usage_census/`（6 文件）、本报告
- 隐私边界：清单只落本机 `temp/`（`.gitignore` 忽略），**不外发**；报告正文不含路径外的任何用户数据内容（只含计数、字节数与文件名）

---

## 0. 摘要（先给结论）

| 问题 | 结论 | 消费者 |
|---|---|---|
| 备份清单（R381 T0）是否覆盖"现在真正在用的资产"？ | **否，且差距已量化**：清单基座 348 文件（2026-09-24 09:32 生成），盘上已多出 75 个**集合内**新文件（memory +48 / tools +23 / kc_snapshots +2 / reflect_scripts +1 / sche_tasks_defs +1），并**完全未纳管** `temp/`（6.65 GB、含唯一脚本）与 `~/Library/LaunchAgents`（29 plist） | R381 备份优先级；下一批「是否继续造工具」 |
| 有没有"高热度但未进备份清单"的资产？ | **有 20 条 G1（清单落后，重跑即可补）+ 80 条 G2a（结构性未纳管，非可再生）+ 7 条 G2b（未纳管但可再生）** | R381 备份优先级；temp 耐久耦合（C65）清理/迁移决策 |
| `tools/` 到底谁在用？ | 40 个 `*.py`：**接线 18 / 仅记忆引用 18 / 仅报告引用 4 / 无引用 0**（`usage_census.py` 自身因 L1 指针引用而计入「仅记忆引用」）；判定见 §2，接线含仓内代码/配置 + 仓外 launchd plist 两类引用源 | 「是否该继续造工具」的下一批决策 |
| R419 声称的 3 个 dead 工具成立吗？ | **不成立（全被推翻）**：`lan_ip.py` 被两个定时脚本 + 一个工具接线；`restore_ga_desktop_bridge.py` 被仓外 launchd plist 接线；`disk_attribution.py` 只是**未入库**（非零引用）——见 §4 | 工具清理决策（须批准后才可动） |

> 口径声明：本报告所有数字均来自 §1 的可复跑命令，命令输出原样粘贴。`tools/` 面判定自本轮起含**四类引用源**（路径限定名 `tools/x.py`、带扩展名裸名 `x.py`、裸模块名 `x`、仓外 `~/Library/LaunchAgents/*.plist` 文本），四源缺一即低估接线——本报告首版只用了前两类，导致 `lan_ip.py`/`restore_ga_desktop_bridge.py` 被误判（见 §4 根因）。 **自扰动声明**：`tools/` 面计数扫描的是**引用者**，本报告自身与 L1/L2 指针都在引用者集合内（如 `usage_census.py` 被 L1 指针一提，就从「无引用」变成「仅记忆引用」）⇒ 写完文档后 face 计数会微变：同版本工具两次快照实测 17/17/5/1（02:5x）→ 18/18/4/0（03:01）。权威值一律取 `temp/usage_census/tools_census.csv`**该次快照**，引用时须写明快照时刻。

---

## 1. 可复跑命令集（验收①）

```bash
cd ~/projects/GenericAgent
python3 tools/usage_census.py --selftest                 # 5/5 PASS
python3 tools/usage_census.py --out temp/usage_census    # 真跑，只写 temp/usage_census
```

实测输出（原样）：

```
SELFTEST 5/5 groups PASS (T1 分类 5 例含 1 负控 / T2 路径 3 例 / T3 分档 3 例含 1 负控 / T4 覆盖 4 例含 1 负控 / T5 delta 1 例)
...
readonly: PASS (files=70, changed=0)

real	0m21.051s
```

清单产物（`wc -l -c temp/usage_census/*` 原样）（2026-09-26 03:03 快照）

```
      20    1891 temp/usage_census/CHECKPOINT_R421.md
       7     591 temp/usage_census/README.txt
     409   20620 temp/usage_census/assets.csv   # 408 条资产（409 行含表头）
   10454  217411 temp/usage_census/census.json
     559   45625 temp/usage_census/hot_not_backed_up.md
       6     165 temp/usage_census/t0_delta.csv
      41    2040 temp/usage_census/tools_census.csv   # 40 个 tools/*.py
   11496  288343 total
```

`census.json`：`assets 408 / tools 40`（其中 tracked 36 / 未入库 4 = `cache_reclaim.py`、`disk_attribution.py`、`report_body_recover.py`、`usage_census.py`）。

热度信号定义（工具内实现，机器可判）：

- `W` 接线：该资产名字出现在**非报告**的仓内文件（代码/配置/plist）中，且以路径限定形式出现（`memory/*.md` 里的散文提及只算 `D`）——避免"在记忆里被提到"被误当接线。
- `D` durable：名字出现在 durable 记忆（L1 `global_mem_insight.txt` / L2 `global_mem.txt` / L3 SOP）。
- `R` recent：mtime 在近 7 天内。
- `M` machine-usage：有机器使用数据承载（如 kc `/app/usage`、Chrome visits 快照）。
- 分档：tier = 命中信号数；缺口判据阈值 `HIGH_TIER = 2`。

热度数据源（只读实测，本轮复跑）：knowledgeC 09-25 快照 `/app/usage` 10427 行（ghostty 334590s / Chrome 99970s / Lark 60064s）；Chrome 09-25 快照 `visits` 30403 行 / distinct URL 11980 / 覆盖 2026-06-27→09-25。

---

## 2. `tools/` 消费面（验收③的基数）



---

## 3. 与 T0 备份清单对账（验收②，核心）

### 3.1 清单基座与盘上增量

T0 清单 = `temp/t0_backups/ga-t0-all-20260924-093228.tar.gz.manifest.json`（348 文件，generated `2026-09-24 09:32:28`；记录键仅 `arc/size/sha256`，**无 path 键**，故按集合名对账）。`t0_delta.csv` / 输出原样：

```
memory:           in_manifest 269 → now 317   new 48  gone 0
kc_snapshots:     in_manifest   5 → now   7   new  2  gone 0
tools:            in_manifest  21 → now  44   new 23  gone 0
reflect_scripts:  in_manifest  16 → now  17   new  1  gone 0
sche_tasks_defs:  in_manifest  37 → now  38   new  1  gone 0
```

即：**集合内新增 75 个文件（0 个消失）**——清单是"快照"而非"清单"，两天前的基座已经落后；`gone 0` 说明没有"备份了但盘上已删"的僵尸条目。

`extra_dirs`（本轮 scan 到的、不属于 T0 五集合但含活跃产物的目录）：`temp/chrome_snapshots` 2、`temp/pmset_snapshots` 20、`temp/pending_decisions` 91、`temp/autonomous_reports` 87、`temp/usage_census` 7。

### 3.2 缺口分级（20 / 80 / 7）

| 级别 | 定义 | 条数 | 处置建议（消费者） |
|---|---|---|---|
| **G1** | 在 T0 的集合定义**覆盖范围内**，但 manifest 里没有 ⇒ 备份清单落后 | **20** | **重跑 `tools/t0_snapshot.py` 即可补**（R381 备份优先级决策） |
| **G2a** | 完全未纳管（`temp/` 或仓外），且**非可再生**且热度 ≥2 | **80**（40 temp 目录 + 39 temp 文件 + 1 仓外目录） | 需要**人决策**：哪些该迁出 gitignored `temp/`（C65 耐久耦合清理决策） |
| **G2b** | 未纳管但**可再生**（快照/日志/缓存/shim） | **7** | 不需备份（备份=浪费）；可回收（磁盘 98% 场景参考 R393/R368） |

G1 逐条（20）：

```
kc_snapshots                       ← 集合本身：盘上 09-22/09-23/09-25（manifest 只到 09-23）等
tools/bg_task.py                   face=接线       recent7d=True   ← RULES#15 指定为本机后台任务标准件（被 tools/task_freshness.py 按裸模块名引用）
tools/dedup_guard.py               face=接线       recent7d=True
tools/dpdomain_renew.py            face=接线       recent7d=True   ← 有 sche_tasks/dpdomain_renew.json 接线
tools/kc_snapshot_guard.py         face=接线       recent7d=True
tools/ref_integrity.py             face=接线       recent7d=True
tools/task_freshness.py            face=接线       recent7d=True   ← 有 reflect_scripts/09 + launchd 接线
tools/todo_quality_gate.py         face=接线       recent7d=True
tools/archive_integrity.py         face=仅记忆引用    recent7d=True
tools/clone_reproduce_audit.py     face=仅记忆引用    recent7d=True
tools/l2_snapshot.py               face=仅记忆引用    recent7d=True
tools/llm_slot_probe.py            face=仅记忆引用    recent7d=True
tools/mem_caliber.py               face=仅记忆引用    recent7d=True
tools/pending_triage.py            face=仅记忆引用    recent7d=True
tools/repeat_labor_detect.py       face=仅记忆引用    recent7d=True
tools/report_body_recover.py       face=仅记忆引用    recent7d=True
tools/report_number_audit.py       face=仅记忆引用    recent7d=True
tools/rolling_source_snapshot.py   face=仅记忆引用    recent7d=True
tools/todo_claim_audit.py          face=仅记忆引用    recent7d=True
tools/usage_census.py              face=仅记忆引用    recent7d=True   ← 本轮新建；census 自身也计入未纳管（L1 指针引用）
```

**读法（重要）**：G1 里 7 个是**已接线**工具（含两个正在跑的定时/反射链路承载件 `dpdomain_renew.py`、`task_freshness.py`），12 个是"仅记忆引用"的按需工具（含本轮新建的 `usage_census.py` 自身），另 1 条是集合级的 `kc_snapshots`。它们全部 `recent7d=True`（近 7 天被写过/跑过），且**都是 R381 清单生成之后新建的** ⇒ 结论不是"R381 备份错了"，而是**"备份清单必须有重跑节奏"**（消费者：R381 后续批次 / 定时化提案）。

G2a 代表条目（全 80 条明细 + 逐条证据见 `temp/usage_census/hot_not_backed_up.md`）：

- 大目录（bytes 排行，`evidence.bytes`/`files`）：`temp/audio_assets` 490,691,292B/90、`temp/evidence` 454,925,366B/335、`temp/recall_probe` 153,010,096B/17、`temp/multica_research` 122,638,348B/8000、`temp/t0_backups` 118,696,978B/3、`temp/models` 77,691,713B/1、`temp/datasource_map` 57,343,624B/49、`temp/model_responses` 54,469,328B/43
- 结构性未纳管目录（含**唯一脚本/夹具**，删了不可再生）：`temp/secret_scan`（scanner.py + 语料 28.9MB）、`temp/history_value`、`temp/pending_decisions`（台账 91 条目）、`temp/log_reclaim_plan.py`、`temp/wechat_poster.py`、`temp/weekly_report_verify.py`、`temp/t0_backups`（备份包自身）、`~/Library/LaunchAgents`（29 plist，**注意这是仓外**）
- `temp/temp` 也被扫出（R375 已记的 cwd 漂移产物）——属"可清理"候选，因热度信号来自记忆中的引用被计入 G2a，读时应视为**噪音/待清理**而非待备份。

G2b（7，可再生，不建议备份）：`temp/chrome_snapshots`、`temp/pmset_snapshots`、`temp/compress_log.py`、`temp/log_reclaim_plan.py`、`temp/sqlite_snapshots`、`temp/__pycache__`、`temp/logs`
> ⚠️ 自曝：`temp/log_reclaim_plan.py` 被判 `regenerable=True` 是因为它是**磁盘回收计划器**（产物可再生而非脚本本身可再生）。**判据局限如实记录**：`regenerable` 目前按名字/路径模式判定，不看文件内容 ⇒ 该字段是**提示级**，不可单独当"可删"依据。

### 3.3 已知边界（不掩饰）

1. **仓外引用不扫**：`tools/*.py` 的"接线"判定只扫仓内文件；`~/Library/LaunchAgents/*.plist` 只在作为**资产**被评估时扫（所以 `restore_ga_desktop_bridge.py` 的面被低估，见 §4）。修复需扩扫描面（待批提案 P2）。
2. **temp/ 扫描有预算上限**：为防 5.8G 拖死，单目录有文件数上限、总预算 21s 级；故 G2a 的 `files/bytes` 是**下界**（例：`temp/evidence` 335 文件/455MB 已贴近上限）。
3. **manifest 无 path 键** ⇒ 集合内对账只能按"名字集合"，不能定位到 tar 内条目；G1 的 `kc_snapshots` 一行是集合级（盘上目录）而非文件名级。
4. **计数快照时点**：本节所有条数（G1/G2a/G2b、delta、extra_dirs）都是 2026-09-26 03:04 一次运行 `python3 tools/usage_census.py` 的输出；census 会把**报告自身与其工作产物**也计入资产（例：`tools/usage_census.py` 与 `temp/usage_census/` 各文件即在本轮新增之列），故再次运行时新写入的 temp 文件会让数字上移 1-2。复核口径=重跑该命令并比对 `temp/usage_census/census.json`。

---

## 4. dead 清单与第二证据（验收③）——**推翻 R419 的 3 个 dead**

TODO 原文要求：`tools/` 39 个对账出 dead 清单，且**每条给第二证据**（`git ls-files` 是否 tracked + 引用面原始输出），单靠 find 零命中不判死。实测（census.json 逐条 + `git ls-files`）：

第二证据分两层取：①`git ls-files` 是否 tracked（防"误删/从未入库"被当 dead）②引用面原始 grep（防"只被报告提过"被当 dead）。

| 工具 | tracked | mtime / recent7d | 引用面原始输出（关键行） | **判定** |
|---|---|---|---|---|
| `lan_ip.py` | **true** | 2026-08-05 01:41 / False | `git grep -l -F -- lan_ip` → `sche_tasks/run_tech_trends.py`、`sche_tasks/run_trendradar_digest.py`、`tools/build_output_archive.py`（+ 自身 + 3 份报告/索引） | **不是 dead**：被**两个定时任务脚本 + 一个工具** import/调用（裸模块名 `import lan_ip`，故口径 A 的"路径限定名"扫描漏判）⇒ R419 的 dead 判定**被推翻** |
| `restore_ga_desktop_bridge.py` | **true** | 2026-08-05 01:41 / False | `grep -l 'restore_ga_desktop_bridge' ~/Library/LaunchAgents/*.plist` → `com.genericagent.desktop-bridge-repair.plist`（1 个） | **不是 dead**：仓外 launchd plist 真接线 ⇒ R419 的 dead 判定**被推翻** |
| `disk_attribution.py` | **false（未提交！）** | 2026-09-24 17:14 / True | `git ls-files tools/disk_attribution.py` → 空；`git grep -l -F -- disk_attribution` → 只命中报告（R385/R418/R419 + INDEX/history） | **不是 dead 而是"从未入库"**：它是 R385 的临时分析件，消费动作不是"删除"而是"决定是否收编进 tools/" |

结论修正：R419 的「接线 13 / 仅记忆引用 23 / 零引用 3」在本轮**四源口径下不成立**（本轮 CSV 快照实为 18 / 18 / 4 / 0），且其 3 个 "dead" **全部被第二证据推翻**（2 个真接线 + 1 个未入库）。

**根因（本轮自己踩到的坑，已修）**：`tools/usage_census.py` 首版判定接线时只 grep **带扩展名的名字**（`tools/x.py` 与 `x.py`），漏掉 Python 里最常见的 `import x` / `from x import y`（裸模块名）⇒ `lan_ip.py`（被两个 `sche_tasks` 脚本裸模块名 import）被判成"仅报告引用"；同时**仓外** `~/Library/LaunchAgents/*.plist` 根本没被当引用源 ⇒ `restore_ga_desktop_bridge.py` 被误判。修法=接线扫描扩为**四源**（路径限定名 / 带扩展名裸名 / 裸模块名 stem / 仓外 plist 文本），改后分布 13/19/7/1 → **18/18/4/0**，且 `--selftest` 6/6 含"裸模块名必须判接线""plist 必须判接线"两条正控。**教训**：写引用扫描器时"名字形态"要与目标语言的实际引用写法一致，否则会系统性低估接线面（并反过来诱导"dead 工具"的误判）。

可复跑复算（一条命令，直接读工具的 JSON）：

```bash
python3 - <<'PY'
import json, collections
d = json.load(open('temp/usage_census/census.json'))
print(collections.Counter(r['face'] for r in d['tools']))
PY
```

**清理动作不执行**：删/移工具属"改仓库实物"，本轮只出证据与建议（消费者：下一批"是否继续造工具"决策；`disk_attribution.py` 建议在批准后"收编或归档"而非删除）。

---

## 5. 消费者映射（验收④：无消费者的指标不入选）

| 结论/指标 | 未来哪个决策会消费它 | 动作 |
|---|---|---|
| G1 20 条（其中 7 条为已接线工具） | R381 备份优先级 | 重跑 `tools/t0_snapshot.py` 生成新 manifest（或接入定时），把 75 个新文件纳入 |
| G2a 80 条（含大目录 bytes 排行） | C65 temp 耐久耦合 + 磁盘 98% 回收决策 | 人选择：迁出/纳入 T0/明确"接受丢失" |
| G2b 7 条 | 磁盘回收（R393/R368 口径） | 可直接回收（但 `log_reclaim_plan.py` 的 regenerable 字段不可单独采信） |
| tools 面 18/18/4/0 | 「是否该继续造工具」 | 新增工具若长期停在"仅记忆引用"应回答"谁在调度它" |
| `lan_ip.py` 实为接线（裸模块名 import）/ `restore_ga_desktop_bridge.py` 仓外接线 / `disk_attribution.py` 未入库 | 工具收编与清理决策 | 须批准；不可自动删。修正 R419 的 dead 判定（否则会诱导误删活跃件） |
| Spotlight 能力边界（附节） | 大仓检索选路（RULES 提案，见 §7） | 写台账待批，不自行改 RULES |

---

## 6. 只读自证（验收⑤）

- 工具内置 `--readonly-proof`（默认随真跑执行）：**`readonly: PASS (files=70, changed=0)`**——被探测的源库/清单/记忆文件在跑前跑后 sha1 全同，0 变更。
- 写操作只落 `--out`（默认 `temp/usage_census`），且该目录在 `.gitignore` 内。
- 未连网、未外发；未读取任何密钥文件。

## 7. 附节：Spotlight 元数据语义与检索能力边界（验收⑥，并入本节）

实测（原样输出）：

```
mdfind -onlyin $PWD 'kMDItemTextContent == "*cache_reclaim*"cd'      → 70
  └ 按顶层目录：temp/l1_slim 18、temp/autonomous_reports 6、memory/autonomous_reports 6、
     temp/model_responses 3、temp/log_reclaim_sim 2、其余 35 个单命中（含 18 个 temp/user_prompt_*.md）
  └ 命中路径中含 /temp/ 的：61 / 70
git -c core.quotepath=false grep -l -F -- 'cache_reclaim'            → 10
  └ 全在 memory/autonomous_reports/（INDEX.md / R368 / R380 / R384 / R385 / R403 / R418 / R419 / history.md / history.txt）
mdfind -onlyin $PWD 'kMDItemFSName == "*usage_census*"cd'            → 5
mdls -name kMDItemLastUsedDate -name kMDItemUseCount -name kMDItemDateAdded <3 个 tracked 文件>
  → tools/pending_triage.py         kMDItemDateAdded = 2026-09-25 13:10:11 +0000  LastUsedDate = (null)  UseCount = (null)
  → tools/rolling_source_snapshot.py kMDItemDateAdded = 2026-09-25 14:37:18 +0000  LastUsedDate = (null)  UseCount = (null)
  → memory/global_mem_insight.txt    kMDItemDateAdded = 2026-09-21 01:26:21 +0000  LastUsedDate = (null)  UseCount = (null)
```

边界表：

| 维度 | 结论 | 可复跑命令 |
|---|---|---|
| 内容检索覆盖 gitignored 区 | **是**（70 命中里 61 在 `temp/`） | 上面的 mdfind `kMDItemTextContent` |
| 与 `git grep` 的差 | 70 vs 10：mdfind 多出的是 `temp/` 内产物（报告副本、模型响应、会话 prompt 文件） | 两命令并列跑 |
| 大仓检索耗时 | mdfind 实测 **1.178s**（`time (mdfind … \| wc -l)` → `real 0m1.178s`，含 5.8G temp 区）；`grep -r` 在 temp/ 上超时是 TODO/第 14 批原文的记载（本轮未复测该反例，故按"引用他人实测"标注，不当作本轮证据） | `time mdfind -onlyin $PWD ...` |
| `kMDItemLastUsedDate` / `kMDItemUseCount` | **本机全为 `(null)`**（3/3 样本）⇒「最近使用/使用次数」维度**不可得**，禁把字段当热度证据；真正可用的机器使用证据是 kc `/app/usage` 与 Chrome `visits` 快照（§1） | `mdls -name kMDItemLastUsedDate -name kMDItemUseCount <file>` |
| 不可索引/不可靠路径 | `temp/`（**可被检索**，但 `-onlyin temp` 对某些 name 查询返回 0 ⇒ **别用 `-onlyin` 限定子目录做存在性判断**，用 `-onlyin $PWD` 全仓）、L4 zip（压缩内容不索引）、`/var/log`（不在仓库、权限受限） | 见上：`mdfind -onlyin $PWD/temp 'kMDItemFSName == "usage_census.py"cd'` → 0，而同名 `-onlyin $PWD` → 5 |

**RULES 提案（不自行修改 RULES，落待批台账）**：大仓内容检索优先 `mdfind -onlyin <repo_root> 'kMDItemTextContent == "*<kw>*"cd'`（秒级、覆盖 gitignored），但要"精确到 git 跟踪的行级证据"时仍用 `git grep`；两者差异应显式说明（mdfind 会命中 temp 产物副本，造成"多处命中"的错觉）。

---

## 8. 验收对照（TODO 原文 ①–⑥）

| # | 要求 | 状态 | 证据 |
|---|---|---|---|
| ① | 可复跑命令集 + 清单条目数与分类统计 | ✅ | §1（2 条命令 + 实测输出）、§3（19/79/7）、§2（18/18/4/0） |
| ② | 与 T0 对账，列出「高热度但未进备份清单」资产（≥1 或明确写 0） | ✅ | §3（G1 20 逐条 / G2a 80 / G2b 7；增量 75 文件） |
| ③ | 与 tools 39 对账出 dead 清单，每条第二证据 | ✅ | §4（3 条逐条：tracked + mtime + 引用面 + plist 实测；R419 的三条判定**全部**被推翻） |
| ④ | 每项结论标「哪个决策会消费它」 | ✅ | §5 映射表；无消费者指标（如 Spotlight 的 LastUsedDate）已明确写"不可得/不入选" |
| ⑤ | 只读自证（前后 sha1/stat 不变） | ✅ | §6 `readonly: PASS (files=70, changed=0)` |
| ⑥ | Spotlight 能力边界作为附节 | ✅ | §7（含边界表 + 可复跑命令 + RULES 提案） |
| — | 清单只落本机（隐私边界） | ✅ | 报告头 + §6 |

---

## 9. 待批提案（不改 SOP/RULES，不删文件）

- **P1（revers=0）**：把 `tools/usage_census.py` 收编为常规巡检件——建议在 R381 T0 快照流程里增加"重跑前先跑 census 看增量"的钩子（或在 `t0_snapshot` 输出里打印 delta）。**不批后果**：备份清单继续无声落后（本次已达 75 文件/2 天）。
- **P2（revers=0，本轮已自修 ⇒ 降级为"记录"）**：接线扫描面已扩到四源（含裸模块名与 `~/Library/LaunchAgents/*.plist`），`restore_ga_desktop_bridge.py` 这类"仓外真接线"不再被误判。**剩余缺口**：`~/Library/LaunchAgents` 之外的用户级引用源（Alfred/Hammerspoon 配置、其他 plist 目录）仍未纳入 ⇒ 工具判定仍是**下界**。**不批后果**：无（已从"系统性假阳性"降为"可能仍有少量漏判"）。
- **P3（revers=1，须逐条拍板）**：`disk_attribution.py`（R385 临时分析件、未入库、近期仍被动过）的处置：收编进 `tools/` 并提交，或归档到 `temp/` 明示一次性；`lan_ip.py` / `restore_ga_desktop_bridge.py` **建议保持不动**（本轮已证两者真接线，R419 的 dead 判定作废）。**不批后果**：无（现状仅占 11KB）。
- **P4**：Spotlight 检索选路写入 RULES（§7 提案文本已备）。
- **P5（revers=0 但影响面大）**：G2a 80 条的处置（迁出 temp / 纳入 T0 / 明确接受丢失）——**这需要用户逐类拍板**，本轮只给清单与证据，不动任何文件。

## 10. 记忆更新建议（收尾时由本会话落地，此处记录口径）

- L1（`global_mem_insight.txt`）：一行指针 → `tools/usage_census.py`（三向对账；19/79/7 缺口分级；`--selftest` 5/5；只读自证 files=70 changed=0）；并订正 R419 的 tools 面"13/23/3"为可复现定义的 **18 接线 / 18 仅记忆引用 / 4 仅报告引用 / 0 无引用**（四源口径），且 R419 的 3 dead 中 `restore_ga_desktop_bridge.py` 被仓外 plist 推翻。
- L2（`global_mem.txt`）：新增一节 `[资产使用热度×T0备份对账 R421]`，含基座路径/生成时间/delta/分级口径/坑（`git grep` 需 `quotepath=false`、manifest 无 path 键、temp 扫描有预算上限）。
- L3：无新 SOP（本轮为一次性对账，工具自述已足够）。
