# R507 · memory 非 .md 资产耐久性闸门（第24批 C5 / TODO L208）

> 快照时点：{ts}（下列数值均为当场命令原始输出；此后语料/登记面变动不作判据）
> 交付物：`tools/memory_asset_durability.py`（{lines} 行 / sha256 前16 `{sha}`）+ 登记面 `memory/asset_durability/REGISTRY.json`（5491 B）+ 政策 `memory/asset_durability/DURABILITY_POLICY.md`（4142 B）

## 1. 问题

`memory/**` 里非 `.md` 资产默认**既不进 git、也不在任何保护面清单**（`.gitignore:50 memory/*` + `:55 memory/**`，仅 `!memory/**/*.md` 放行），却可能被生产工具直接消费。这类丢失**不报错、只静默降级**（先例：报告正文恢复变「查无」、L2 `global_mem.txt` 无版本历史）。此前没有任何环节回答「哪些有消费者但未入库、各自凭什么算可接受」。

## 2. 判据（强 → 弱三级 + 状态语义）

- 消费证据三档：**PATH**（带 `memory/` 前缀完整路径）> **GLOB**（目录级前缀，按目录消费）> **BASE**（裸文件名，**弱证据只判 INFO 不判洞**）。
- 语料 = 库内脚本/闸门，**排除散文 `.md` 与 `temp/` 脚本**（否则报告里一句提及即被当消费者）；自引用不算消费。
- 状态四态：`DECLARED_LOCAL_ONLY`（有意本地：须实测 T0 保护面 ∧ 归档双覆盖）/ `PENDING_APPROVAL`（修法明确但属政策动作）/ `DEFER`（可再生不可回填，是否纳保护面待定）/ `INFO`（弱证据，仅固化口径）。
- 与既有口径的边界：**不替代 R418**（克隆可得性）、**不替代 T0 保护面**（R444/R461）、只读不删不改。

## 3. 实测（当场原始输出）

```
$ python3 tools/memory_asset_durability.py scan
{scan}

$ python3 tools/memory_asset_durability.py selftest | tail -1
{st}

$ python3 tools/memory_asset_durability.py proof | tail -1
{pf}
```

扫描规模（`--json-out` 落 `temp/mem_durability/scan.json`）：`assets_total=412`、`assets_prose=358`、**非 .md 行 54**、`tracked_total=400`、语料 `corpus_files=236 / 2814244 B`。

54 行按 verdict：`DECLARED_LOCAL_ONLY 7 / OK 42 / PENDING_APPROVAL 2 / DEFER 1 / INFO 2`，**问题 0 条**。

## 4. 九洞清单与处置（首份登记面前的真洞）

首份登记面前，本器报 **9 个**「有消费者但未入库」洞（另 2 条 INFO）。

| # | 资产 | 消费证据 | 处置 |
|---|---|---|---|
| 1–6 | `memory/L4_raw_sessions/2026-04..09.zip` | PATH+GLOB+BASE（`tools/l4_search.py:3`、`tools/report_body_recover.py:14`） | `DECLARED_LOCAL_ONLY`：T0 保护面 + T0 归档双覆盖（实测 True ∧ True） |
| 7 | `memory/L4_raw_sessions/all_histories.txt` | BASE（`tools/l4_search.py:19`） | `DECLARED_LOCAL_ONLY`（10.6 MiB，可由 `compress_session.py` 重建） |
| 8 | `memory/global_mem.txt` | BASE+PATH（`tools/backup_restore_drill.py:15/22`） | `PENDING_APPROVAL` = R418 D1（L2 入库 or 私有备份） |
| 9 | `memory/history.txt` | BASE+PATH（`memory/global_mem.txt:617`、`tools/report_number_gate.py:90`） | `PENDING_APPROVAL`：仅 231 B，`git add -f` 即可闭环 |
| — | `memory/file_access_stats.json` | BASE（`ga.py:160`） | `DEFER`：不在 T0 保护面但归档含其名 ⇒ 两套 T0 口径不一致（R461），纳入与否待定 |

副作用（INFORMATIONAL）：连登记面自身（`memory/asset_durability/REGISTRY.json`）也被列为 INFO（BASE 自引用），`memory/autonomous_history.txt` 同理 → 说明弱证据档位确实没被当成洞。

## 5. A/B 与负控（证明「归零」是登记面的功劳）

```
$ scan --registry <空登记面>   → {ab}        # 9 洞全部回落：判据仍然有效
$ scan --registry <坏 JSON>    → {bad}        # fail-closed，绝不当空登记面
```

- **A/B**：把登记面换成一个合法但为空的对象 ⇒ 洞从 0 **回落到 9** ⇒「问题 0」确由登记面消解，而非判据被写坏。
- **负控（当场发现并修的真缺陷）**：`--registry /dev/null` 触发 `json.decoder.JSONDecodeError` **裸崩**（堆栈直出）。已改为 `REGISTRY_UNREADABLE` / `REGISTRY_BAD_SHAPE` ⇒ `rc=2` 硬停；对应新增自测 **NC17/NC18**，selftest 22 → 24。
- 自测覆盖面：NC1–NC18（HOLE/已登记消解/tracked 免判/无消费者 INFO/无证据必报/证据行不含 token/STALE_MISSING/STALE_TRACKED/语料排除散文与 temp/裸名歧义与自引用/单条与多条 BASE 只算弱证据/GLOB 触发/非法状态/证据文件不存在/坏登记面×2）+ 正控 PC1–PC5。
- **只读自证**：`proof ... changed=[]`（跑前后 412 资产指纹一致）。

## 6. 与既有工作的对账（不重立）

| 既有 | 关系 |
|---|---|
| R418 克隆可得性审计 | R418 回答「被引路径随 clone 是否可得」，本器回答「**未入库但有消费者**的资产是否**有意为之**」；本器把 R418 的 IGNORED 大项（temp/、`memory/**`）收敛成**可判决条目** |
| R444 T0 备份恢复演练 / R461 两套 T0 定义 | `DECLARED_LOCAL_ONLY` 的成立前提就是 T0 双覆盖，本器**复用**其存在性判据，不重做恢复演练 |
| R499 入口面收口（`git add -f` 先例） | 同一坑：memory 下非 `.md` 资产受 ignore，登记面自身也须 `git add -f`（本次已办） |

## 7. 残余与待批

1. `memory/global_mem.txt`：R418 D1（入库 or 私有备份）——政策面，待批。
2. `memory/history.txt`：`git add -f` 入库——政策面，待批。
3. `memory/file_access_stats.json`：两套 T0 口径差异（R461）下的归属待定。
4. 本器**未接定时**（当前为人工复跑入口）。

## 8. 复跑

```
python3 tools/memory_asset_durability.py scan|proof|selftest
```
