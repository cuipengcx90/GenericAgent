# 时间衰减源留存政策（RETENTION_POLICY）

> 适用对象：本机四个**会被源侧自行滚动删除**的数据源（Chrome 历史 / knowledgeC / pmset 电源日志 / WiFi 日志）。
> 单一判据实现 = `tools/rolling_source_snapshot.py`（采集 + 缺口验证）与 `tools/rolling_source_coverage.py`（数据级覆盖判定）。
> 本文件只写**实测过的窗口与判据**；窗口值随快照时点变化，改口径须同批改本文件（R495 / TODO L197）。

## 1. 为什么需要本政策（缺口 = 永久丢失）

这四类源的共同点：**源侧按自己的节奏删数据，快照是唯一副本**。一旦某天没采、且该天已滚出源窗口，数据就永久不可得——不是"以后补"，而是**永远没有**。所以判据必须回答两个不同问题：

| 问题 | 判据层 | 工具 |
|---|---|---|
| ①「这天有没有快照」 | 目录存在性 | `rolling_source_snapshot.py verify`（R483） |
| ②「这天没有独立快照，但数据是否已在别的快照里」 | 数据级覆盖（按源内时间戳字段反查） | `rolling_source_coverage.py`（R495）+ `verify --coverage-ledger` |
| ③「源侧还留着这天数据吗（能否补采回来）」 | 源侧可采窗口 + 归档抢救 | `rolling_source_snapshot.py` 探针 + `rolling_source_coverage.py salvage --apply`（R504） |

★ 只答①会系统性高估损失：chrome/kc/pmset 的"缺日"实测多为**采样节奏粗**（该日数据在更晚的快照里），而非丢数据；反过来也必须答②才敢把某天判成"真丢失"。
★ 只答①②仍会**把可救的判成死**：只要源侧（含轮转归档）还留着那天，就该**抢救补采**而不是写进 `missing_lost`——判定顺序是 ①→②→③，**先救后判**（R504 实测：wifilog 09-27/09-28 原判"永久丢失"，实际在 `.bz2` 轮转归档里完整存在，已抢救成快照）。

## 2. 四源留存窗口（2026-09-29 快照，`rolling_source_coverage.py window` 实测）

| 源 | 数据窗起点 → 终点 | 窗口 | 距窗口起点滚出 | 判据字段 |
|---|---|---|---|---|
| chrome | 2026-06-30 → 2026-09-28 | 91 天 | 0 天 | `chrome.visits_range`（SQLite visits 时间范围） |
| kc | 2026-09-02 → 2026-09-29 | 28 天 | 0 天 | `kc.app_usage_range` |
| pmset | 2026-09-15 → 2026-09-29 | 15 天 | 0 天 | `pmset.asl_filenames` |
| wifilog | 2026-09-29 → 2026-09-29 | 1 天 | **1 天** | `wifilog.day_tokens` |

★ wifilog 的「1 天」只是**独立快照**口径：源侧另留 12 份轮转归档（`wifi.log.0.bz2 … .10.bz2` + 现行 `wifi.log`），实测**最老仍可抽的一天 = 2026-09-17**（探针 `live_oldest` 返回 `method=rotation-archive-first-ts`，按各归档首行时间戳取最小值，`n_files=12 / n_read=12 / n_errors=0`）⇒ 抢救期约 12 天，不是"过一夜就没了"。

声明留存（保守下界，用于"还剩几天可补"）：chrome ≈90 天 / kc ≈25 天（判丢失取更保守的 23）/ pmset ≈15 天 / wifilog = 1 天（判决口径）／归档抢救窗 ≈12 天（实测 2026-09-17 起，随轮转滚动，非固定值）。

★ 口径铁律：
- **窗口取源内容的 time token**（不是目录 mtime、不是文件大小）⇒ `window` 即使快照目录不存在也能算。
- `days_left` 优先用**探针实测**（`days_left_src=probe`）；探针不可用时退回声明值并标 `src=declared`，**估不冒充实测**（selftest T29 / T29b）。
- 探针窗超出声明留存时**按声明留存收紧**（否则出现 636 天式假乐观；T30 为负控、T31 证明去掉收紧即复现假乐观）。

## 3. 逐源处置（按抢救紧迫度排序）

### 3.1 wifilog（独立快照 1 天，但归档可回溯约 12 天）
- 轮转：`newsyslog` 每日 00:30，`/var/log/wifi.log` 单日轮转成 `.0.bz2` 并顺延 ⇒ 现行文件只含 1 天。
- 政策：**必须每天采**（最省事、零成本）；若漏采 ⇒ **次日立即 `salvage --apply` 抢救**（数据仍在 `.bz2` 归档里），不要直接写进 `missing_lost`。
- 实测（2026-09-29）：缺 09-27 / 09-28 两天，R495 曾判 `UNSUPPORTED`（当时 salvage 只判不采、且探针把 wifilog 的"最老可采日"恒当成 today−1）⇒ **该判定已作废**。R504 改为从轮转归档逐行按日期抽取：09-27 `rows=14657 / 1838303B`、09-28 `rows=13840 / 1750298B`，落成 `temp/…/2026-09-27/`、`2026-09-28/` 两个真快照（`salvaged=True`，provenance 记归档文件名+sha），复跑 `verify` 已转 `OK`。
- 边界：归档窗会随轮转滚动（实测最老 2026-09-17）⇒ 抢救有期限；且**抢救产物只补"这天不能凭空重来"的原始行**，不做任何插值/占位（`EMPTY` 即不落盘）。

### 3.2 pmset（15 天窗口，抢救期约两周）
- 缺日可在窗口内补采；实测缺 09-27 / 09-28，`days_left=12`（`oldest_available=2026-09-15`，`days_left_src=probe`）。
- 政策：缺口不紧急但不无限期；巡检发现缺口应**直接补采**（`snapshot --source pmset`），不只是记账。

### 3.3 kc（≈25 天窗口 + 迟写回填两坑）
- kc 有**迟写回填**（口径见 `memory/kc_profile/BACKFILL_STABILITY.md`：T+1 快照即稳定、T+0 不可用）⇒补采 kc 缺口时**不要用当天快照去冒充历史日**，应直接用"更晚快照含该日数据"这一事实做覆盖判定。
- 实测缺 09-24 / 26 / 27 / 28 四天，全部 `COVERED`（数据在 09-29 快照内），`days_left=18`。
- 口径：探针报 `oldest_available=2024-12-27` 是**文件级**范围（kc 内含旧页），与 `/app/usage` 流的 25 天滚动不同 ⇒ 判丢失一律取保守下界。

### 3.4 chrome（≈90 天窗口，最长，也最容易"看着没缺"骗过）
- 实测缺 09-26 / 09-28，均 `COVERED`（在 09-27 / 09-29 快照的 visits 范围内，`rows_on_day` 均 >0）。
- 探针 `oldest_available=null`、`oldest_probe.rc=2`（源库热态 `database is locked`，只读探针不可得）⇒ 该源只能按声明留存估，属**已声明边界**，不得写成"实测 90 天"。
- 政策：缺口按 90 天预算，优先级最低。

## 4. 账本与判定语义（`temp/source_retention/coverage.jsonl`）

- 一行一条声明（append-only，`schema=ga.rolling_source_coverage/v1`），字段：`source / day / status / rule / evidence / audit_class / ledger_id / ts`。
- `status ∈ {COVERED, UNCOVERED, NO_CLAIM}`；`evidence` 必带**可复算证据**（`artifact` 路径 + `artifact_sha256_16` + `min_day / max_day / n / rows_on_day`）。
- 幂等：同一 (source, day, status, 证据) 重跑 `claim --apply` ⇒ `appended 0 / skipped 8`（实测 8 条重跑零追加）。
- `audit`：逐条重算证据是否仍成立（实测 8 条 → 仍成立 8 / 失效 0，rc=0）。**证据失效即 rc≠0**，不允许"账本永远绿"。
- `salvage`：只对 `UNCOVERED` 且**仍在源窗口内**的日尝试补采。R504 起 wifilog 走**归档抽取**：`salvage --source wifilog --apply` 把 `.bz2/.gz` 轮转归档按行内日期拆到 `<root>/<day>/{wifi.log,events.json}`（`salvaged=True` + provenance），并追加台账 `temp/source_retention/salvage.jsonl`（幂等，同日重跑不重复）。**真的补不回来**（归档里也没有该日）才输出 `UNSUPPORTED` 落 `missing_lost`；抽到 0 行记 `EMPTY` 且**不落盘**——**绝不生成占位/插值文件**（伪造一天数据比缺一天更坏）。
- 与 `verify` 的接线：`verify --coverage-ledger <账本>` 追加数据级复核；**不带该 flag 时输出与 R483 版逐字相同**（A/B 已验）。带账本时汇总 `coverage.verdict ∈ {NO_DATA_LOSS, DATA_LOSS_OR_UNKNOWN}`，并给 `n_data_loss`（UNCOVERED + NO_CLAIM 计数）。

## 5. 日常动作清单

1. 每天采四源（`rolling_source_snapshot.py snapshot`）；wifilog 优先，不可省。
2. 巡检：`rolling_source_snapshot.py verify --coverage-ledger temp/source_retention/coverage.jsonl`
   - `ALERT_PERMANENT_LOSS` ⇒ 立即确认是"没采"还是"已不可补"，并写进当轮报告。
   - `ALERT_GAP` ⇒ 看 `days_left`：小窗口源（wifilog / pmset）尽快补采。
3. 出现新缺口：先 `claim`（dry-run 看证据）→ `claim --apply` 落账 → `audit` 复核 rc=0。
4. 判"永久丢失"前必须跑 `salvage`（wifilog 加 `--apply` 真采）：只有**归档里也抽不到**该日才算真丢失；`COVERED / RECOVERABLE` 一律改判。抢救后复跑 `verify` 确认该源转 `OK`。
5. 抢救成功即留痕：归档名 + sha + 行数写进 `salvage.jsonl` 与该日 `events.json` 的 provenance（防日后把"抢救来的"误当"当天采的"）。

## 6. 边界与已知限制

- 只读：全部子命令不改源库（只读自证：快照树 61 文件 sha256 前后一致）。
- 探针不可用（chrome 源库热锁）时"剩余可补天数"是估计值，已标 `days_left_src=declared`。
- **未接定时**：目前靠人工/自主轮触发；接线提案见报告 R495 §6（D1 待批）。
- wifilog 单日轮转是**硬约束**，但**不等于当日不采就永久丢**：轮转归档（`wifi.log.N.bz2`）可回溯约 12 天（实测最老 2026-09-17）⇒ 正确处置是"次日立刻 `salvage --apply`"，而不是把缺口直接计入永久丢失。归档滚出后才是真丢失。

## 7. 抢救式补采（R504 已实现，2026-09-29）

- 命令：`python3 tools/rolling_source_coverage.py salvage --source wifilog --today <YYYY-MM-DD> --root temp/wifi_snapshots --src-dir /var/log --apply`（不加 `--apply` = 纯 dry-run）。
- 实测：09-27 `rows=14657 / 1838303B`、09-28 `rows=13840 / 1750298B`，各落 `<root>/<day>/{wifi.log,events.json}` 并写 `salvage.jsonl` 2 条；幂等复跑 ⇒ 无缺口日、`appended 0`。
- 补采后 `verify` 该项由 `ALERT_PERMANENT_LOSS` 转 `OK`（wifilog 已采 4 天）。
- 抢救产物与当日快照**同格式**（复用 `parse_wifi_text`），消费端无需改；区别只在 `events.json` 里 `salvaged=True` 与 provenance 段。
