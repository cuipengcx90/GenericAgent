# R347｜knowledgeC 快照器 + 首份「数字行为底稿」

**任务来源**：`temp/TODO.txt` TODO#1（时效最高 —— 源库只保留约 4 周，越晚开始越少）
**结论**：交付可复用工具 `tools/kc_snapshot.py`；首份快照 + 首份真实数字基线报告已落盘；
**唯一待批项 = 是否注册每日快照定时任务**（本报告不含任何环境越权改动）。

---

## 1. 交付物（全部已实测跑通）

| 交付 | 路径 | 证据 |
|---|---|---|
| 快照/报告工具 | `tools/kc_snapshot.py`（415 行，子命令 `snapshot / report / verify / list / prune`） | `py_compile` 通过；5 个子命令真跑 |
| 首份快照 | `temp/kc_snapshots/2026-09-22/knowledgeC.db`（13,119,488 B，sha256 `8c3f55f4c41692b2…`） | `verify` → rc=0 |
| 快照元数据 | `…/2026-09-22/manifest.json` | 含 sha256 / 对象数 / 生成时间 / revision 数 |
| 基线报告 | `…/2026-09-22/baseline_report.md`（8,965 B，全真数字、无占位符） | 本报告 §2 复述其关键结论 |
| 并发/覆盖版本 | 同日多次快照 = revision，文件名 `knowledgeC.db.rev2`（不覆盖历史） | 幂等逻辑见 §3 |
| 定时注册待批提案 | `temp/pending_decisions/DECISIONS_R347_kc_snapshot_scheduling.md` + `kc_snapshot_daily.plist`（**未安装**） | 文件已生成 |

## 2. 首份基线（真数字，来自 2026-09-22 快照）

- **覆盖**：17,463 个对象 / 33 个有数据的日期；最长**连续**日段 **29 天**（2026-08-25 → 09-22），更早只剩 4 天孤点（2024-12-27、2025-01、2025-04）。
  → 坐实「约 4 周滚动删除」：**每过一天就永久丢一天**，这也是本条 TODO 被列为时效最高的原因。
- **App 前台时长**：44 个 App、10,012 会话、累计 151.0 h。Top5 =
  ghostty 84.77 h（56.1%，3465 会话，均 88 s）＞ Chrome 24.94 h（16.5%）＞ Lark 14.20 h（9.4%）＞ 微信 6.67 h（4.4%）＞ Obsidian 3.77 h（2.5%）。
- **节奏**：活跃日 18 天，日均 549 次前台会话 / 7.82 h，平均单段 51 s；会话最多的一天 2026-09-14（934 次）。小时分布双峰：10-11 点（1321/1057）与 16-18 点（1380/1253/895），03-08 点几乎为 0。
- **点屏**：亮屏 136.6 h / 728 段，熄屏 240.7 h ⇒ 亮屏占比 36.2%，按 29 天连续窗口 ≈ **4.71 h/天**。
- **通知**：5,741 条，连续窗口内 198 条/天；**100% 可归属 App**。Top = Lark 3540（61.7%）、Terminal 2049（35.7%）、Chrome 108。 这两条合计 97.4% —— Terminal 的 2049 条基本就是本机 agent/定时任务的产物。
- **其它流**：`/bluetooth/isConnected` 251（连接 131 / 断开 120）、`/app/mediaUsage` 6、`/discoverability/signals` 6。

## 3. 幂等 / 安全（本轮实测，非声明）

- **幂等**：连跑两次 `snapshot` → 第二次输出 `[skip] 内容与上次快照一致（sha=8c3f55f4c416…），不重复写入`，revision 数保持 1。同一天源库有新数据时才生成 `rev2`。
- **自包含性**：拷贝 `knowledgeC.db` + **连字符**侧车 `-wal`/`-shm` → 副本上 `wal_checkpoint(TRUNCATE)` → **`PRAGMA journal_mode=DELETE`** → 删侧车 → `quick_check=ok`。
- **只读源库**：全程只在源目录 `cp`，未 checkpoint/VACUUM/写入源；`verify` 用 `file:…?mode=ro` 打开副本。

### ★ 本轮踩到并已修的坑（对后续同类快照通用）

1. **WAL 库「自包含」的假象**：只做 `wal_checkpoint(TRUNCATE)` 后副本仍处于 `journal_mode=WAL`，此时用 `mode=ro` 打开会报
   `sqlite3.OperationalError: unable to open database file` —— **read-only 连接不允许创建 `-shm`**。
   修法：checkpoint 后显式 `PRAGMA journal_mode=DELETE`，再删侧车，才是可真·只读打开的单文件。
   （R344 建立的只读副本正因此类细节而无法直接 RO 打开；那条副本另有「用 `.wal` 点号命名侧车 ⇒ 实际没拷到 WAL」的问题，本工具侧车名统一用连字符。）
2. 之前 bug 版本在快照目录留下的 `-shm` 残留会让 `verify` 判「非自包含」→ `snapshot` 写入前先清理目标目录同名侧车。

## 4. 待批准（唯一一项）

`temp/pending_decisions/DECISIONS_R347_kc_snapshot_scheduling.md`：三个方案（A. launchd 每日 09:30；B. 反射器 15s 轮询；C. 纯手动）。
A 方案 plist 原文已生成但**未安装**；保留策略（近 14 天全量 + 每周首份 ≈ 200 MB 上限）已在 `prune` 实现且默认 dry-run。
**未获批前，本工具不会自己跑第二次。**

## 5. 已知局限 / 未做

- 单份快照只反映当日；纵向对比需等累积（这正是注册定时的意义）。
- 前台会话按段求和、未合并重叠；未做 bundle→中文名反查；未与 Timing2（300K 条）交叉验证 —— 属 D1「时间去哪了 v2」。
- 报告含个人行为数据，存放于 `temp/`（不进 git）。

## 6. 记忆更新建议（待审，未落盘）

1. L1「本机数据源目录只读地图」条目补一句：knowledgeC 快照副本若要 `mode=ro` 打开，**必须先 `PRAGMA journal_mode=DELETE`**（否则报 unable to open）；快照工具 = `tools/kc_snapshot.py`。
2. L2/L3 无需新增 SOP（工具自带 `--help` 与 §3 幂等/安全说明，属应用层而非跨任务方法论）。
