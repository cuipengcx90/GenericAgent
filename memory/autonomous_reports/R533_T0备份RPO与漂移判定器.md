# R533 T0 备份 RPO 与漂移判定器（第26批 C1 / TODO L221，只读）

- 日期：2026-09-29
- 任务来源：`temp/TODO.txt` L221「T0 备份 RPO/漂移判定器」（第26批三路评审 C1，4.33，强收窄=只读）
- 交付：`tools/t0_rpo_drift.py`（27632B / 566 行 / sha256 前16 `1b9626a308d8665d`）
- 关联：R444（D2「备份有时点性，须先定 RPO」）、R461（T0 两套定义差异门禁）、R418（克隆耐久性）

## 1. 问题（为什么需要这个工具）

R444 的真演练只回答了一个问题：**「能不能回到 2026-09-24 09:32 那一刻」**——答案是能（12/12 RESTORED_OK）。
它明确留下了 D2 缺口：**「今天的盘面还有多少没被这份归档覆盖」无法回答**，而且当时 12 条抽样里已经出现
6 条 `LIVE_DRIFT`（归档里的 sha ≠ 盘上现在的 sha）。这意味着"有备份"不等于"当前内容被备份覆盖"，
缺的是一个可复算的 **漂移清单 + 有效 RPO（小时）**。

## 2. 口径（先定判据，再看数字）

### 2.1 分类（逐条归档成员，内容优先，mtime 只做 RPO 二次判定）

| 分类 | 判据 | 是否计 RPO 违规 |
|---|---|---|
| `SAME` | live 内容 sha == 归档记录 sha（**只看内容，不看 mtime**） | 否 |
| `DRIFT` | 内容不一致 且 live mtime > 归档快照点 ⇒ 快照后又被改，未被归档覆盖 | **是** |
| `DRIFT_PRE_SNAPSHOT` | 内容不一致 但 live mtime ≤ 快照点（归档内容异常/时钟偏差） | 否 |
| `LIVE_MISSING` | 盘上已无该文件（删除本身也未被归档覆盖，mtime 不可得） | **是** |
| `NO_ARCHIVE_RECORD` | 归档无该成员的 sha 记录（manifest/SHA256SUMS 缺）⇒ **绝不判 SAME** | 否 |

保护面侧（`temp/t0_baseline.sha256`，口径同 R444 D1）：
`covered`（同时在保护面与归档成员表，**唯一可判漂移的子集**）、`ARCHIVE_MISSING`（在保护面但不在归档成员表，
**判"归档缺失"，绝不判 SAME**）。

### 2.2 有效 RPO

```
有效 RPO（小时） = (now − 最早未备份修改时间) / 3600      无违规时 = 0
「最早未备份修改时间」= 快照点之后被改动文件里最早的 live mtime
参照量 backup_age_hours = (now − 快照点)/3600（归档本身有多旧）
```
语义：>0 表示"这一段时间内的改动，靠这份归档回不去"；=0 表示该归档对当时受覆盖面仍然完全有效。

### 2.3 退出码与只读

- `0` 无 RPO 违规 / `1` 存在 RPO 违规（**发现，不是失败**）/ `2` 输入错误（归档缺失、空归档、基线缺失）
- `--strict-coverage` 时保护面覆盖洞（ARCHIVE_MISSING）也计入 rc=1；默认只报不改 rc（覆盖洞是 R444 D1 的存量事实，不是每日变化量）
- 只读：全程不写源文件；归档与基线 sha256 前后一致自证；产物只落 `--out`（默认 `temp/t0_rpo_drift/<date>/scan.json`，gitignored）

### 2.4 复用而非重写

逐项复用 R444 内核 `tools/backup_restore_drill.py`（装载时逐项核接口，缺一即 fail-closed）：
`sha256_file` / `load_sums` / `load_manifest_sums` / `load_baseline` / `safe_member_name` / `tier_of` / `pick_default_archive`。
tar 成员枚举沿用同一内核的 `tarfile` 读取面 + `safe_member_name` 过滤口径，未另起解析器。
selftest 内含一项"真复用"自证：打印每个被复用函数的来源文件与行号，全部须落在 `backup_restore_drill.py`。

## 3. 真跑结果（2026-09-29 22:33:02）

```
[归档] temp/t0_backups/ga-t0-all-20260924-093228.tar.gz
[基线] temp/t0_baseline.sha256   [活根] /Users/cuip/projects/GenericAgent
[快照点] 2026-09-24 09:32:28（来源 manifest.generated）  [现在] 2026-09-29 22:33:02
[只读自证] 归档 sha256 不变=True 基线 sha256 不变=True
[成员] 普通文件 348（非普通文件 0）  扫描 0.78s
[分类计数] {'SAME': 318, 'DRIFT': 30}
[保护面] 保护面 6299 / 归档 348 / covered 295（0.0468）/ 归档缺失 6004
[有效 RPO] 132.99 小时（最早未备份修改 2026-09-24 09:33:31；归档本身年龄 133.01 小时）
[RPO 违规] 30 条
```
（`proof` 子命令）`files=2 changed=0`：归档 sha256 前16 `48db85b71d6e180d`、基线 sha256 前16 `a3f2e3a5fb4b0723`，扫描前后逐字未变。

### 3.1 结论

1. **归档点已落后 5.5 天**：快照 2026-09-24 09:32:28，距运行时刻 133.01 小时。
2. **348 个受归档保护的文件里 30 个已漂移**（8.6%），即盘上内容已不再被这份归档覆盖；
   其中 L3(SOP) 16 条、其他 12 条、L2 1 条、**L1 1 条**（`memory/global_mem_insight.txt`）。
3. **有效 RPO = 132.99 小时 ≈ 5.54 天**：最早一条未备份修改发生在快照后 63 秒
   （`tools/t0_snapshot.py`，2026-09-24 09:33:31）⇒ 从那一刻起的改动全部只能靠"新备份"找回。
4. **保护面覆盖洞 6004/6299**（covered 仅 295 = 4.68%），与 R444 D1/R461 独立量一致；
   该洞是"归档面 ⊅ 保护面"的存量结构问题，本器只报数不改 rc。
5. 无 `LIVE_MISSING`（348 条全在盘上）、无 `DRIFT_PRE_SNAPSHOT`、无 `NO_ARCHIVE_RECORD`。

### 3.2 30 条 RPO 违规明细（按 mtime 升序，`in_baseline` 列=false 表示该文件不在 T0 保护面但被归档收录）

```
2026-09-24 09:33:31 OTHER tools/t0_snapshot.py            (False)
2026-09-24 09:35:17 L3    memory/tmwebdriver_sop.md       (True)
2026-09-24 17:05:38 L3    memory/weekly_report_sop.md     (True)
2026-09-25 00:12:51 OTHER sche_tasks/navicat_trial_reset.json / project_update_monitor.json / brew_path_monitor.json (True×3)
2026-09-25 03:12:48 L3    memory/subagent.md              (True)
2026-09-25 03:13:29 L3    memory/autonomous_operation_sop.md (True)
2026-09-25 08:12:56 L3    memory/project_update_monitor_sop.md (True)
2026-09-27 02:34:11 L3    memory/fofa_cli_proxy_hunter_sop.md (True)
2026-09-27 04:32:57 L3    memory/haisou_monitor_sop.md    (True)
2026-09-27 05:13:29 L3    memory/birdiesearch_monitor_sop.md (True)
2026-09-28 10:02:09 L3    memory/scheduled_task_sop.md    (True)
2026-09-28 10:03:18 L3    memory/linuxdo_deep_read_sop.md (True)
2026-09-28 15:52:05 L3    memory/scheduled_task_freshness_audit_sop.md (True)
2026-09-28 16:35:22 OTHER memory/autonomous_operation_sop/task_planning.md (True)
2026-09-28 23:03:38 L3    memory/autopilot_unattended_drill_sop.md (True)
2026-09-28 23:57:01 L3    memory/deliverable_audit_sop.md (True)
2026-09-29 02:17:56 L3    memory/roi_monitor_sop.md       (True)
2026-09-29 09:35:11 L3    memory/linuxdo_trust_level_sop.md (True)
2026-09-29 11:02:26 L3    memory/launchpad_folder_sop.md  (True)
2026-09-29 14:36:42 OTHER reflect_scripts/.price_history.json (False)
2026-09-29 17:34:26 L2    memory/global_mem.txt           (True)
2026-09-29 20:16:59 OTHER memory/L4_raw_sessions/all_histories.txt / 2026-09.zip (True)
2026-09-29 21:33:13 OTHER memory/file_access_stats.json  (False)
2026-09-29 22:27:03 OTHER memory/autonomous_reports/{history.txt,history.md,INDEX.md} (True×3)
2026-09-29 22:27:15 L1    memory/global_mem_insight.txt   (True)
```
（明细行数 30；同一时刻多文件已合并书写。完整逐条 JSON 见 `temp/t0_rpo_drift/2026-09-29/scan.json`。）

## 4. 验证（不是"我们的工具自己说对"）

1. **独立复算（不借本工具内核）**：对 3 条 DRIFT + 3 条 SAME 用 `/usr/bin/shasum -a 256` 手工算 live sha，
   与归档 manifest 里记录的 sha 逐条比对 ⇒ DRIFT 3/3 不符、SAME 3/3 相符。
2. **与 R444 12 条抽样对账**：6/6 `LIVE_DRIFT` ⇒ 本器判 `DRIFT`；5/5 `LIVE_SAME` ⇒ 判 `SAME`；
   余 1 条 `reflect_scripts/03_daily_report.py` R444 记 `NO_BASELINE`、本器判 `SAME`——**口径差不是矛盾**：
   R444 的 `NO_BASELINE` 指"该文件不在 T0 保护面（`temp/t0_baseline.sha256`）里"，
   本器的 `SAME/DRIFT` 判的是"归档记录 sha vs 盘上内容"，两者可同时成立（该文件 `in_baseline=False` 且内容未变）。
3. **`selftest` 21/21 passed（rc=0）**，逐条原始输出（`python3 tools/t0_rpo_drift.py selftest`）：
   正控 2 条 =「同内容但 mtime 被触碰 → SAME（不许按 mtime 判漂移）」「完全一致 → SAME」；
   负控 10 条 =「快照后被改 → DRIFT 且计 RPO 违规」「mtime 早于快照点 → DRIFT_PRE_SNAPSHOT 且不计违规」
   「盘上已删 → LIVE_MISSING 且计违规（无 mtime）」「保护面有、归档无 → ARCHIVE_MISSING（不判 SAME）」
   「空归档 → error 且 rc=2」「归档无 sha 记录 → NO_ARCHIVE_RECORD（不判 SAME）」「归档缺失 → rc=2」「基线缺失 → rc=2」
   「只读自证：归档 sha256 前后一致」「只读自证：基线 sha256 前后一致」；
   其余 9 条 = 真读到归档成员（≥5）/真复用内核接口（逐项有来源行号）/保护面覆盖账目自洽（covered+missing==保护面）/
   有效 RPO == (now−最早未备份修改)/3600（约 1 小时）/违规清单排序（drifted 在 deleted 之前）/rc 语义 3 条（有违规 1、无违规 0、--strict-coverage 覆盖洞 1）/复跑确定性（同输入两次分类计数逐字一致）。
4. 扫描 348 文件 0.78s（含 L4 zip 等大件），可日常跑。

## 5. 边界（明确不做什么）

- **不替代 R444**：本器不抽取、不落沙箱、不验可读性；只答"漂移与 RPO"。两者互补（R444 答"回得去吗"，本器答"现在覆盖到哪一刻"）。
- **不重做 R461 的两套 T0 定义对账**：本器只报覆盖洞计数（6004/6299），定义差异由 R461 处置。
- **不重做 R429 的可回收面**、不碰 R418 的克隆可得性。
- 只对 `covered` 子集给判决；保护面其余 6004 条**不推测内容**（它们不在归档里，无从比对）。
- 未接定时、未接 CI、未接 `complete_task`（入口形态为子命令，`tools/usage_census.py --entries` 闸门 rc=0，无需登记）。

## 6. 复现命令

```bash
cd ~/projects/GenericAgent
python3 tools/t0_rpo_drift.py selftest                 # 21/21
python3 tools/t0_rpo_drift.py scan --limit 25          # rc=1（存在 RPO 违规），产物落 temp/t0_rpo_drift/<date>/scan.json
python3 tools/t0_rpo_drift.py proof                    # files=2 changed=0（只读自证）
python3 tools/usage_census.py --entries                # 入口闸门 rc=0（无可复跑入口 12）
```

## 7. 待批（台账 `temp/pending_decisions/DECISIONS_R533_t0_rpo_drift.md`）

- **D1**：把 `scan` 接成定期（每日/每周随 T0 备份节奏）并落账，使 RPO 从"一次性度量"变成"持续可见"。不批后果：RPO 只能人工跑，等于每次都要有人想起它。
- **D2**：引入 RPO 阈值门禁（例如 `--max-rpo-hours`，超阈 rc=1 并使通知可见）。不批后果：没有"多久没备份"的自动告警，漂移只能靠翻报告发现。
- 两条均 `revers=0`（纯只读/加 flag 默认不变），批准语：`批准 DECISIONS_R533_t0_rpo_drift.md:D1`（或 D2）。
