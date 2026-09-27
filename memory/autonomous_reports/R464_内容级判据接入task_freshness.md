# R464 周期任务「结果闭环」：内容级产出信号接入 task_freshness L1 判据

- 编号：R464（编号权威 = `temp/autonomous_reports/history.txt`）
- 日期：2026-09-28
- 来源任务：`temp/TODO.txt` L176（第19批·评审共识替换项；原 C7 = 2/4/4 三路一致淘汰）
- 落点：`tools/task_freshness.py` + `tools/freshness_criteria.json`
- 分工：**判据内核**由 R457 交付（`tools/content_change_detector.py`，613 行）；本任务只做**接线**（本文件不重写任何指纹/归一化/分类逻辑）

---

## 1. 结论

「跑而不产出」此前在 R411 巡检里不可见（L1 只看产物 mtime/静默天数、L2 只看日志降级签名）。
本任务把 R457 的内容指纹内核接进 `task_freshness.py` 的扫描段（新判据层 **L1b**），并按「未登记不生效」原则只登记 1 个已标定目标：

| 对象 | 改前 | 改后 |
|---|---|---|
| `sqlite_snapshot`（`--only` 单目标真跑） | `OK`，rc=**0** | `ALERT`，rc=**1** |
| 全量巡检汇总 | ALERT 3 / OK 23 / INFO 1 / DISABLED 10，硬问题 **2** 个 | ALERT 4 / OK 22 / INFO 1 / DISABLED 10，硬问题 **3** 个（+ `sqlite_snapshot`） |
| 全量巡检 37 目标 verdict 逐目标对比 | — | **仅 `sqlite_snapshot` 变化**（36/37 逐字不变） |

命中原话（改后，`--only sqlite_snapshot`）：

```
  [ALERT] sqlite_snapshot          L1b 产出内容零变化：temp/sqlite_snapshots/snapshot_*.json（n=110 distinct=1 距上次实质变化 108.85d）
汇总(标*为软判据)：ALERT=1；软 ALERT/WARN 0 条；硬问题 1 个：sqlite_snapshot
```

即：受监控 DB 自 05-19/06-04 静止，110 份快照内容指纹 distinct=1、距上次实质变化 108.85 天，而任务每天照常产出新文件（mtime 恒新、L1 判「新鲜」）——正是 L1b 要覆盖的盲区。

---

## 2. 改了什么（两处，共 `118+/4−` + `3+/1−`）

### 2.1 `tools/task_freshness.py`（664 行 / 35326B；sha256 前缀 `19245cb7e2f04abf`）

- 头部判据说明加 L1b 一行（第 12–14 行），并新增两个默认常量：

```python
# ---- L1b 内容级判据默认值（判据内核在 tools/content_change_detector.py，本文件只做接线）----
DEFAULT_CONTENT_MIN_FILES = 3     # 单文件/双文件族无法判「内容零变化」，默认至少 3 份
DEFAULT_CONTENT_MAX_FILES = 500   # 只取最新 N 份（大目录不全扫，窗口贴近当下）
```

- `check_target()` 内、`# ---- L2 静默降级签名 ----`（原第 236 行）**之前**插入 L1b 段（现第 243–290 行）：对每个登记了 `content` 段的目标调用内核 `scan_family(...)`，命中即按类 `bump`：

```
if klass == "ALL_IDENTICAL" and sd > lim:  lvl = "INFO" if soft else "ALERT"   → "L1b 产出内容零变化：…"
elif klass == "CONTENT_STALE" and sd > lim: lvl = "INFO" if soft else "WARN"   → "L1b 产出内容停滞：…"
elif klass in ("MISSING", "NO_HISTORY"):   INFO「L1b 内容判据不可用（需 ≥N 份）」
else:                                       OK「L1b 产出内容在变：…」
```

设计要点：
- **未登记 `content` 段的目标零新增扫描面**（`cspecs` 为空直接跳过）⇒ 心跳型任务不可能因本改动多报；
- 阈值/文件数/hash_cap/normalize 全部可被判据表覆盖，缺省回落到工具内常量（R457 台账 D2 的「常量单一来源」要求一并落地）；
- `soft: true` 或 `ack: "理由"` 降为 INFO（等价 R449 零消费者处置的「显式登记静止源」思路）——**未登记的静止仍报 ALERT/WARN，默认不静默**；
- 样本不足（单文件族）只报「不可用」的 INFO，不假绿也不告警。

### 2.2 `tools/freshness_criteria.json`（262 行 / 18961B；sha256 前缀 `d6ea0cc88b6e48ef`）

- `_doc` 补 `content[]` 字段说明；
- `sqlite_snapshot` 目标新增 `content` 段：

```json
"content": [{"glob": "temp/sqlite_snapshots/snapshot_*.json", "max_content_stale_days": 8, "min_files": 3,
             "note": "内容级判据（R457 内核接线，R464）：110 份快照内容指纹 distinct=1、距上次实质变化 108.6d ⇒ 受监控 DB 静止（不是任务故障，是源无变化）；未登记即 ALERT。若结论为「有意保持静止」，填 ack='理由' 降为 INFO"}]
```

阈值取 `8d` 与同目标既有 payload 静默上限 `max_silence_days=8` 对齐（不是新拍的数字）。

---

## 3. 验收对照（逐条，原始输出）

### ① 历史反例被自动标红（改前/改后等级与 rc）

改前 = `git show HEAD:tools/task_freshness.py` + `git show HEAD:tools/freshness_criteria.json` 落到 `temp/l176/` 后同参数跑：

```
BEFORE rc= 0 | 汇总：OK=1；软 ALERT/WARN 0 条；硬问题 0 个
AFTER  rc= 1 | 汇总：ALERT=1；软 ALERT/WARN 0 条；硬问题 1 个：sqlite_snapshot
```

（全量巡检 rc 改前改后同为 1：该 rc 语义是「存在硬问题」，此前已由 `birdie_daily_monitor`/`haisou_daily_monitor` 触发，故等级变化与硬问题清单 2→3 才是证据；单目标 rc 0→1 是干净的 A/B。）

### ② 负控：真改被判「零变化」的产物内容 ⇒ 必须转绿

用**真实快照副本**（`temp/l176/negctl2/`，`shutil.copy2` 保留 mtime，取最近 3 份真实 `snapshot_*.json`）挂临时判据表端到端真跑：

```
NEGCTL before rc= 1
    [ALERT] negctl_family  L1b 产出内容零变化：temp/l176/negctl2/snapshot_*.json（n=3 distinct=1 距上次实质变化 2.92d）
    汇总：ALERT=1；硬问题 1 个：negctl_family
modified: snapshot_20260925_081539.json          ← 真改一份内容（写入 _negctl_marker）
NEGCTL after rc= 0
    汇总：INFO=1；硬问题 0 个                        ← 判据转绿，ALERT 消失
```

改后那 1 条 INFO 是 `无运行证据文件（glob 未命中：temp/l176/negctl2/done_*.md）`（这个夹具没有 L3 证据），**与 L1b 无关**，原文：

```
  [INFO] negctl_family            无运行证据文件（glob 未命中：temp/l176/negctl2/done_*.md）
```

（提示：首版合成夹具因文件 mtime 太新未触发——`stale_days` 从最近一次实质变化回溯，新造文件天然「刚变过」；故负控必须用**保留 mtime 的真副本**，否则造出假"未触发"。）

### ③ 已知心跳型任务的误报率不升（前后数字）

- 全量 37 目标 verdict 前后对比：**仅 `sqlite_snapshot` 变化**，其余 36 个逐字不变（脚本比对 `set(targets)` 相同、`verdict` diff 列表长度 1）；汇总差异仅 `ALERT 3→4` / `OK 23→22`。
- 新增扫描面自证：改前 `corpus = {"targets": 37, "channels": 8, "payload_files_seen": 601, "log_files_scanned": 3}`，改后**同一 4 个字段逐字相同**，只多 `"content_families": 1`。
- 判据可证伪且默认常量不动：selftest ⑫ 断言「未登记 content 段的目标 contents 为空（默认行为逐字不变）」。

### ④ `--selftest` 含负控、只读、不改被巡检目标行为

```
负控/回归合计：PASS（12/12）      （rc=0）
  [PASS] ⑦ 语料自证：payload=8 且日志=2 且内容族=3（精确计数）
  [PASS] ⑧ 内容零变化（4 份逐字相同，离线 30d）→ content_heartbeat ALERT
  [PASS] ⑨ 已登记静止（ack）→ 同样命中但降为 INFO，且不进硬问题
  [PASS] ⑩ 单文件族（样本不足）→ 判「不可用」，既不假绿也不告警
  [PASS] ⑪ 负控：改写族内一份内容 → content_heartbeat 转绿（distinct=2）
  [PASS] ⑫ 负控：未登记 content 段的目标 contents 为空（默认行为逐字不变）
```

只读证据：三次真跑全部带 `--no-ledger`；`temp/task_freshness/ledger.jsonl` mtime 前后恒为 `Sep 28 00:21:38 2026`（未推进）；改动的两个文件之外 `git status --porcelain -- tools/` 无其他条目。

### ⑤ 与候选 2 的分工 + 台账 D1

- 分工：R457 = **内核**（指纹/归一化/分类/`scan_family` 接口 + 独立 CLI + 31/31 selftest）；R464 = **接线**（巡检调用点 + 判据表登记 + 巡检侧负控/告警等级 + 12/12 selftest）。本任务不重写内核。
- `temp/pending_decisions/DECISIONS_R457_content_change.md`：**D1（接入 R411 巡检）与 D2（常量抽取）均已由本任务交付**，台账行已改为已交付（保持 `决策` 列留空规则之外的单行标注，不改其他行）。

---

## 4. 复跑命令（可逐条重放）

```bash
cd /Users/cuip/projects/GenericAgent
python3 tools/task_freshness.py --selftest                                   # 12/12 rc=0
python3 tools/task_freshness.py --only sqlite_snapshot --no-ledger           # 期望 ALERT rc=1
python3 tools/task_freshness.py --json --no-ledger                           # 全量；与改前 JSON 相比只 sqlite_snapshot 变
python3 tools/content_change_detector.py --selftest                          # 内核 31/31 rc=0（R457 交付）
python3 reflect_scripts/09_task_freshness.py --selftest --state /tmp/x.json   # 反射器 6/6 rc=0（本次实测）
```

改前基线 JSON = `temp/l176/scan_before.json`（47838B，由改前版脚本产出），改后 = `temp/l176/scan_after.json`（69088B）；判据表基线副本 `temp/l176/criteria_head.json`（16175B）。
改前版脚本本体 = `git show HEAD:tools/task_freshness.py`（551 行）；改后 664 行。

**反射器接线是自动生效的**：`reflect_scripts/09_task_freshness.py` 每小时跑巡检并按 ledger 去重（仅新增或恢复才推飞书），本改动后该反射器即自动带 L1b 判据，无需另行接线。

---

## 5. 回滚（revers = 0）

- 撤销 = `git checkout HEAD -- tools/task_freshness.py tools/freshness_criteria.json`（或只删判据表里的 `content` 段）。
- 无数据改动、无外部副作用；判据仅本地只读扫描，默认不写 ledger（`--no-ledger`），写 ledger 也只在反射器内部用于告警去重。

---

## 6. 残差与边界

1. **登记面只 1 个目标**：R457 实测「有历史族」共 23 个、其余 24 族未登记。逐族登记前必须做「该族本就该变化吗」的标定（否则把合法静止的心跳族报成 ALERT），属**扩面**工作，未在本任务内做；`sqlite_snapshot` 是唯一已标定族（与 R457 硬告警一致）。
2. `ack` 语义只是「登记为已知静止」，不改变指纹判定本身；误填 `ack` 会静默掉一条真问题——故 `ack` 必须写理由且出现在告警文本里。
3. `content` 判据的 `stale_days` 是**族内窗口**口径（`max_files` 截断后取最新 N 份），不是全量历史口径；大目录族需注意窗口设置。
4. 本任务不改任何被巡检目标的脚本行为、不发任何通知（告警推送仍由反射器按既有 ledger 去重逻辑负责）。

---

## 7. 交付物清单

| 文件 | 状态 |
|---|---|
| `tools/task_freshness.py` | 修改（`118+/4−`，664 行） |
| `tools/freshness_criteria.json` | 修改（`3+/1−`，262 行） |
| `memory/autonomous_reports/R464_内容级判据接入task_freshness.md` | 本报告 |
| `temp/l176/`（`NOTES.md`/`scan_before.json`/`scan_after.json`/`tf_head.py`/`criteria_head.json`/`criteria_negctl.json`/`negctl2/`） | 证据与基线副本（temp 内，非跟踪） |
| `temp/pending_decisions/DECISIONS_R457_content_change.md` | D1/D2 标注为已交付 |
| `temp/TODO.txt` L176 | 关项 |
