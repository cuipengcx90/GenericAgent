# R445 · Chrome 全 profile 只读快照器（TODO 第17批 C4）

- 日期：2026-09-27
- 类型：能力（采集器扩面）
- 任务来源：`temp/TODO.txt` L123 = `[ ] 环境 | 【第17批·三路评审 7/5/6=6.00】Chrome 全 profile 只读快照器（不改既有采集面）+ 一次真采对账`
- 交付物：`tools/rolling_source_snapshot.py`（新增 `--profiles {off,all}`）+ 首份全 profile 真采 `temp/chrome_snapshots/2026-09-27/profiles/`

## 1. 结论

1. 采集器已具备「全 profile」能力位：`--profiles all` 逐 profile 落 `History` + `manifest.json`，并在 `<date>/profiles/index.json` 汇总逐 profile sha256 / http 计数 / 侧车审计 / 与基线对账。**默认 `off`，原代码路径不变**（不改现有采集面、不动默认源）。
2. 一次真采（2026-09-27）落 10 个 profile（Default + 9 个非 Default），`hard_fail_profiles=[]`，对账 `verdict=ok`。
3. 与 R437 冻结副本对账：9 个非 Default profile 的副本字节 sha256 **9/9 不同**，但 sqlite 逻辑量（urls/visits/page_count/freelist_count/page_size）**9/9 一致**（Profile 4 为新增数据：urls 290→292、visits 1106→1113）⇒ 差异属**物理布局**（本次走 sqlite3 backup API 重写页，R437 冻结副本是 raw copy），**非数据丢失**。
4. 过程中发现并修掉一个**真缺陷**：`already-done` 复跑会把 index 记录降级（丢 `method`/`integrity`/`src_verdict`/`readonly_selfproof`）⇒ 改为从同目录存档 `manifest.json` 回填；已加 T19 三条自测（含负控）。

## 2. 验收项对照

| 验收要求 | 实测证据 |
|---|---|
| ① 加 `--profiles all`，默认行为逐字不变 | `snapshot --source chrome`（off）rc=0，输出 `[SKIP] chrome 当天快照已存在…`；默认产物 `temp/chrome_snapshots/2026-09-27/History` sha256 前后一致（`708d80216f3d…`）；profiles 逻辑只在 `--profiles all` 时生效（参数定义 `s.add_argument('--profiles', choices=['off','all'], default='off', …)` 在 L1667，边界注释在 L978） |
| ② `--selftest` 含负控 | `selftest: 44/44 PASS`（rc=0）；负控含：raw 方法缺 `-wal` 必报、profile 目录消失必报、总量对账不通过必报、T19 存档无 method 时确实取不到（回填非恒真） |
| ③ 一次真采落 temp + 打印逐 profile sha256 + 与 R437 冻结副本对账 | 见 §3、§4 |
| ④ 不接定时 | 未改任何调度文件；`index.json` 的 `note` 明写「未接定时（接线属 R423-D6 待批）」 |

## 3. 真采结果（2026-09-27）

命令：

```
python3 tools/rolling_source_snapshot.py snapshot --source chrome --profiles all --force
```

产出 `temp/chrome_snapshots/2026-09-27/profiles/index.json`（14820 B，sha256 前缀 `ce938c78aa5c36aa`），
`du -sk` 该目录 = `25544` KB；`profiles/Default/manifest.json` 12065 B、`profiles/Guest Profile/manifest.json` 9884 B。

逐 profile（`status=already-done` 取自随后一次不加 `--force` 的复跑；sha256 为产物字节 sha）：

| profile | 方法 | 产物字节 | sha256(前12) | integrity | src_verdict | 侧车类型 |
|---|---|---|---|---|---|---|
| Default | raw-file-copy+rollback+vacuum-into | 15785984 | 708d80216f3d | ok | unchanged | product |
| Guest Profile | sqlite3-backup-api(child-proc, ro-src) | 163840 | baa1b8587819 | ok | unchanged | product |
| Profile 3 | sqlite3-backup-api | 4677632 | efac46ab572b | ok | unchanged | product |
| Profile 4 | sqlite3-backup-api | 1269760 | 34fcbbed6485 | ok | unchanged | product |
| Profile 6 | sqlite3-backup-api | 790528 | 071273bcc1b1 | ok | unchanged | product |
| Profile 7 | sqlite3-backup-api | 565248 | a9df1909360a | ok | unchanged | product |
| Profile 8 | sqlite3-backup-api | 884736 | e70749f739d2 | ok | unchanged | product |
| Profile 9 | sqlite3-backup-api | 1376256 | f37dbff6a5a1 | ok | unchanged | product |
| Profile 10 | sqlite3-backup-api | 278528 | 310711b01918 | ok | unchanged | product |
| Profile 11 | sqlite3-backup-api | 221184 | e0c6d152e50d | ok | unchanged | product |

对账（基线 = `temp/chrome_profiles/consume_2026-09-26.json`，逐 profile http 访问数）：

| profile | base | now | delta | 判定 |
|---|---|---|---|---|
| Default | 30098 | 29865 | -233 | same |
| Profile 4 | 1031 | 1037 | +6 | grown |
| 其余 8 个 | 0/94/68/276/121/177/655/366 | 同 base | 0 | same |

总量行：`now=32659 base=32886 delta=-227 verdict=same ref=32886 floor=31241.7 tol=0.05`；
`note` = 「下界式判定：允许增长；缺 profile / 缩水超容差 / 总量低于下界 ⇒ FAIL」；`extra_profiles=[]`、`violations=[]`。

**Default 的 -233 是预期，不是丢数据**：Chrome `visits` 是 90 天滚动窗口，窗口右移会把最老那天整体裁掉（同一机制在 R415/R423 已记录），故对账设计为「允许增长 + 只对缩水设容差下界」。

## 4. 与 R437 冻结副本的对账

R437 冻结副本 = `temp/chrome_profiles/2026-09-26/`（顶层 `manifest.json`，9 个非 Default profile，全部 `readonly_proof=true`）。

| profile | frozen copy_bytes | frozen copy_sha256(前12) | today bytes | today sha256(前12) | sha 相同 |
|---|---|---|---|---|---|
| Guest Profile | 163840 | aedde51ef5c1 | 163840 | baa1b8587819 | 否 |
| Profile 3 | 4685824 | fce3932df470 | 4677632 | efac46ab572b | 否 |
| Profile 4 | 1277952 | f07a43033375 | 1269760 | 34fcbbed6485 | 否 |
| Profile 6 | 819200 | 0d36d6cade03 | 790528 | 071273bcc1b1 | 否 |
| Profile 7 | 589824 | 08b901d9c479 | 565248 | a9df1909360a | 否 |
| Profile 8 | 884736 | 7d0d1926dd35 | 884736 | e70749f739d2 | 否 |
| Profile 9 | 1376256 | 65c36e772906 | 1376256 | f37dbff6a5a1 | 否 |
| Profile 10 | 294912 | 1387f9c9b916 | 278528 | 310711b01918 | 否 |
| Profile 11 | 229376 | 91e0de545c8c | 221184 | e0c6d152e50d | 否 |

**判定「同内容 / 异布局」的第二判据（sqlite 逻辑量，只读打开）**：

| profile | urls frozen→today | visits frozen→today | page_count | freelist | page_size |
|---|---|---|---|---|---|
| Guest Profile | 0→0 | 0→0 | 40 | 0 | 4096 |
| Profile 3 | 102→102 | 276→276 | 1142 | 1063 | 4096 |
| Profile 4 | 290→292 | 1106→1113 | 310 | 119 | 4096 |
| Profile 6 | 71→71 | 121→121 | 193 | 64 | 4096 |
| Profile 7 | 95→95 | 177→177 | 138 | 9 | 4096 |
| Profile 8 | 370→370 | 655→655 | 216 | 0 | 4096 |
| Profile 9 | 237→237 | 366→366 | 336 | 0 | 4096 |
| Profile 10 | 44→44 | 94→94 | 68 | 0 | 4096 |
| Profile 11 | 31→31 | 68→68 | 54 | 0 | 4096 |

结论：页数/空闲页/页大小全等 ⇒ sha256 不同只因**页内容布局不同**（backup API 重写 vs raw copy），除 Profile 4 有 2 条新 URL / 7 条新访问（与 §3 对账 `grown` 一致）外数据等价。

> 口径提醒（沿用 R423/R430 铁律）：VACUUM/backup 产物字节数与源库字节数不可互加、不可当数据量口径；逐 profile 副本 sha256 只在**同一方法**内可比。

## 5. 修复的真缺陷（本批新增）

**现象**：对本已存在的当日产物再跑一次（不加 `--force`）时，走 `already-done` 分支重新组装的记录丢掉 `method` / `integrity` / `src_verdict` / `readonly_selfproof`，导致 `index.json` 从「可审计」降级为「方法未知」——表面 rc=0、无告警，属静默降级。

**修法**：`already-done` 分支改为从同目录存档 `manifest.json` 的 revision 回填这些字段（取不到就诚实留空，不臆造）。

**验证**（原始输出）：

```
PASS T19 正例-幂等复跑 index 记录与首跑逐字段一致(仅 status 归一) [['status'], ['status'], ['status']]
PASS T19 正例-回填到 method/完整性/只读自证(非空) {'Default': ('sqlite3-backup-api(child-proc, ro-src)', ...), ...}
PASS T19 负控-存档 revision 无 method ⇒ 记录确实取不到(回填非恒真) [('Default', None), ('Guest Profile', None), ('Profile 3', None)]
selftest: 44/44 PASS
```

端到端（真树）：`--force` 重采恢复完整记录 → 不加 `--force` 复跑 → 两轮记录逐字段相同（`两次一致(不降级): True`），且 `index.json` 在两次 `already-done` 运行间**逐字节相同**。

## 6. 边界与未接线

- **不接定时**：本条只补采集器能力位；每日接线属 R423-D6（待批），`index.json` 的 `note` 已写明。
- 默认行为隔离：`--profiles off` 时既不写 profiles 目录内容，也不触碰默认产物（本次验证：默认 History sha 前后一致 + `[SKIP]`）。
- 基线依赖：`temp/chrome_profiles/consume_*.json` 不存在时，本次**不做对账**（index 里 `note` 明示），不阻断采集。
- 隐私：只读 Chrome `History` 侧车与计数，不读 `Login Data`/`Cookies`/`Web Data`（沿用 R437 的拒读清单）。

## 7. 复用与命令

```
python3 tools/rolling_source_snapshot.py snapshot --source chrome                 # 默认：只采 Default（行为不变）
python3 tools/rolling_source_snapshot.py snapshot --source chrome --profiles all  # 全 profile
python3 tools/rolling_source_snapshot.py selftest                                 # 44/44
```
