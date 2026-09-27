# R430 · Chrome 多 profile 只读对账：R415 漏采是否改变 R423 的决策

- 日期：2026-09-26 ｜ 类型：只读对账（无写入、无删除、无网络）
- 工具：`tools/chrome_profiles_consume.py`（新增；`--selftest` 18/18，含 6 条负控）
- 数据源：本机 Chrome 10 个 profile 的 `History`（只读拷贝副本）+ R415 唯一一份快照
  `temp/chrome_snapshots/2026-09-25`（Default）
- 产物：`temp/chrome_profiles/consume_2026-09-26.json`（机器可读）/
  `temp/chrome_profiles/scan_2026-09-26.txt`（逐 profile 原表）/ 工作区
  `temp/chrome_profiles/2026-09-26/`（副本）
- 复跑（在仓库根执行）：`python3 tools/chrome_profiles_consume.py reconcile --chrome-root "$HOME/Library/Application Support/Google/Chrome" --json temp/chrome_profiles/consume_2026-09-26.json`
  （`reconcile` 默认落 JSON；`scan` 只出表；`verify` 只核副本 sha256）

## §0 结论（一句话）

漏采面（9 个非 Default profile）在窗口内共 **2788 次 http 访问 = 全机 32886 次的 8.48%**，
对 R423 表内 **D1/D2/D3/D4 与 D7 的判定无一改变**（全部偏差落在各自阈值内），**D6（扩面）已复核成立**；
唯一需要更新的是 **D5 保留策略的体量口径：年化 5.45 GiB → 7.10 GiB（×1.30）**，
并顺带固化了一条口径铁律（VACUUM 产物 vs 原始拷贝**不可互加**，见 §5）。

## §1 方法（做了什么、怎么保证只读）

1. `scan`：只读枚举 `$CHROME_ROOT/*/History`（含 `Guest Profile` 等非数字目录名），对每个库取
   `http_visits / 域数 / 时间窗 / 登录标识（掩码）`；空库（0 行）不崩溃、计 0。
2. 拷贝：把每个 profile 的 `History` 连同 `-wal/-shm`（若有）拷进工作区，**源库只以 `mode=ro` 打开**；
   `verify` 用 sha256 复核副本与源一致（`readonly_proof`）。
3. `reconcile`：把 9 个非 Default profile 与 Default 快照按同一窗口/同一 `www.` 归一规则合并，
   得到 `Default-only` vs `全机合并` 两套指标，再逐条重跑 R423 的 D1–D7 判定（阈值与判据线照旧）。
4. 判定 3 态：`不改变 / 数值更新 / 需复核`（`需复核` 只在「漏采占比 ≥ 阈值」或「Default 近窗为 0 而漏采面 >0」时出现）。
5. `--selftest` 18/18，含负控：篡改副本必报 TAMPERED、删副本必报 MISSING、量级差必判需复核、
   关注域出现在漏采面必判需复核、VACUUM 探针源不可读返回 0（不假绿）。

## §2 逐 profile 原表（`scan` 原始输出，逐字粘贴）

```
Chrome 多 profile 只读扫描（workdir=/Users/cuip/projects/GenericAgent/temp/chrome_profiles/2026-09-26）
profile            bytes  wal/shm   http访问     域数  时间窗
Guest Profile     163840       --        0      0  None → None
Profile 10        294912       --       94      4  2026-01-13 10:06 → 2026-04-04 16:08 [t***]
Profile 11        229376       --       68      4  2026-01-13 21:08 → 2026-04-04 17:11 [t***]
Profile 3        4685824       --      276     21  2026-01-17 23:14 → 2026-04-24 21:39 [破***/s***@gmail.com]
Profile 4        1277952       --     1031     38  2026-06-23 13:40 → 2026-09-21 11:42 [测***]
Profile 6         819200       --      121      9  2026-01-07 14:29 → 2026-04-04 17:10 [果***/r***@gmail.com]
Profile 7         589824       --      177     23  2026-01-06 17:21 → 2026-04-05 16:32 [玉***/s***@gmail.com]
Profile 8         884736       --      655     29  2026-01-09 17:24 → 2026-04-04 16:17 [t***/t***@gmail.com]
Profile 9        1376256       --      366     13  2026-01-11 08:07 → 2026-04-04 16:08 [g***]
合计              10321920              2788
  Top: Profile 10   chatgpt.com(64), auth.openai.com(23), api.cpass.cc(6), pay.openai.com(1)
  Top: Profile 11   chatgpt.com(48), auth.openai.com(9), api.cpass.cc(8), pay.openai.com(3)
  Top: Profile 3    anyrouter.top(62), ocn93f5d9olj.feishu.cn(39), us-east-1.run.claw.cloud(29), github.com(27), run.claw.cloud(24)
  Top: Profile 4    localhost(308), agentrouter.org(139), test-deploy-admin.smyze.cn(113), op-test.smyze.cn(91), anyrouter.top(77)
  Top: Profile 6    accounts.google.com(28), auth.openai.com(23), myaccount.google.com(23), api.cpass.cc(21), chatgpt.com(19)
  Top: Profile 7    accounts.google.com(32), chatgpt.com(30), api.cpass.cc(25), outlook.live.com(17), login.live.com(9)
  Top: Profile 8    chatgpt.com(135), accounts.google.com(99), myaccount.google.com(84), youtube.com(73), one.google.com(52)
  Top: Profile 9    accounts.google.com(189), chatgpt.com(57), google.com(35), auth.openai.com(31), cr6zhdxsal.feishu.cn(28)

负控 verify：9 个副本一致（无篡改）
```

## §3 漏采面量化

- http 访问：**2788** 次 = 全机 32886 次的 **8.48%**
  （R423 §3.6 的 8.6% 是**原始行**口径，含 `file:`/`blob:` 等非 http 行；两者差 0.1pp，非矛盾，口径写明即可）
- 独占域：**50 个 / 693 次**
  （只在非 Default 出现、Default 完全没有）。Top15：myaccount.google.com×108、api.cpass.cc×100、sls.console.aliyun.com×71、one.google.com×60、ocn93f5d9olj.feishu.cn×39、signin.aliyun.com×32、us-east-1.run.claw.cloud×29、flowus.cn×28、run.claw.cloud×24、accounts.youtube.com×17、outlook.live.com×17、pay.openai.com×14、developers.google.com×13、ping0.cc×12、families.google.com×12
- 域版图：distinct_hosts 865 → 915；
  Top10 占比 58.5% → 54.3%；单次域占比 29.0% → 28.3%
- 节奏：近 30 天日均 187.7 → 189.6；前 60 天 407.8 → 422.7
- 体量（两口径，禁相加）：原始拷贝 9.84 MiB（含 `-wal` 冗余，**磁盘代价上界**）/
  VACUUM 口径 4.64 MiB（**与 R415 快照同口径**）
- 本轮 `reconcile` 耗时 0.234s（9 个非 Default profile 的拷贝 + 解析）

## §4 逐条决策对账（R423 表 7 行 → 本轮机判）

| # | R423 原判定 | 本机判定 | 依据（机器输出） | 是否改变 |
|---|------------|---------|-----------------|---------|
| D1 | 浏览强度两口径同向下降（137.6 vs 332.0 次/日）→ 每月复算 | 不改变 | 近 30 天日均 Default-only=187.7 vs 全机=189.6（相对偏差 1.01% ≤ 阈值 5.0%） | 否 |
| D2 | pm.smyze.cn 升 / op* 与 codeup 降 / 3 域近 30 天归零 → 核监控清单 | 不改变 | op.smyze.cn 漏采 +19/2033(0.93%)近30天 319→319；op-test.smyze.cn 漏采 +91/3223(2.82%)近30天 195→195；dashboard.huifu.com 漏采 +2/402(0.5%)近30天 0→0｜判定线：漏采占比≥5.0% 或 Default 近窗 0 而漏采面 >0 | 否 |
| D3 | 09:00—10:20 与上网起点重叠 → 2 个任务挪到 19:30—20:30 或 08:00 前 | 不改变 | 最近月首访<09:30 占比 Default-only=29.2%（2026-09）vs 全机=29.2%（2026-09），差 0.0pp ≤ 阈值 5.0pp | 否 |
| D4 | 0 个自驾嫌疑域；自造仅占 20.1% → 人的轨迹按长期档案保留 | 不改变 | 嫌疑域 Default-only=0 vs 全机=0；自造占比 20.1% → 18.8%（差 1.3pp，阈值 3.0pp） | 否 |
| D5 | 单份 15.29 MB / 年化 5.45 GB → 7 日 + 周 + 月份保留 | 数值更新 | 全机每日 ≈ 19.93 MiB ⇒ 年化 7.10 GiB（R423 的 5.45 GiB 只算 Default；倍率 ×1.30）｜口径：两加数同为 VACUUM 口径（Default 取 R415 快照副本 15.29 MiB，漏采面为本轮逐副本 VACUUM INTO 实测 4.64 MiB）；原始拷贝口径上界 25.13 MiB/日（仅作磁盘代价上界，不参与年化） | **是（数值）** |
| D6 | 10 个 profile 只采 1 个，漏 8.6% → R415 源改逐 profile（待批） | 已复核（扩面项） | 见本工具 scan 输出的逐 profile 表与 reconcile 的全机/Default 差 | 复核成立 |
| D7 | 副本 16.03 MB vs 源库 33.23 MB（同数据）→ 源侧/产物侧字节不混算 | 不改变（口径加强） | 本工具的工作区是「原始文件集拷贝」而非 VACUUM 产物 ⇒ 再次证明字节口径必须按来源区分 | 加强 |

**D5 的净效果**：全机每日快照 ≈ **19.93 MiB**（Default 15.29 MiB + 漏采面 4.64 MiB，同为 VACUUM 口径）
⇒ 年化 **7.10 GiB**，是 R423 只算 Default 的 1.30 倍。
结论仍是 **7 日 + 周 + 月份** 三档保留（量级未跨档），但预算按 7.10 GiB 记，而不是 5.45 GiB。

## §5 口径铁律（本轮新增/加强）

1. **VACUUM 产物 ≠ 原始拷贝**：同一份 Chrome `History`，VACUUM INTO 后 15.29 MiB，原始拷贝 9.84 MiB（9 个 profile）
   与源库 33.23 MiB 又是第三个数。三者**互相不可推算**，只能按来源各自标口径。
2. **跨口径禁相加**：D5 的两加数必须同为 VACUUM 口径；工具内 `vacuum_bytes()` 逐副本实跑 VACUUM INTO
   得到漏采面同口径值，探针失败即降级为「上界」并显式标注（`NC5-b` 负控保证不假绿）。
3. **8.5% vs 8.6%**：R423 的 8.6% 是原始行（含非 http）口径，本工具 8.48% 是 http 口径；
   同源不同筛，写进结论时必须带口径词。
4. **R423 表实为 7 行**（D1–D7），其 §0 一句话小结写「6 条消费决策」= 口径不一致（分组计数漏了 D4 归属项）。
   本轮以表为准：**7 条**，其中 6 条不改变、1 条数值更新。

## §6 边界与未接线（不等同完成）

- 本轮**未改动任何源库**，也未删任何 profile / 缓存（`readonly_proof=True`，9 副本 sha256 全一致）。
- **未接线**：R415 采集面「逐 profile」仍在待批台账（D6 扩面项），本轮只给证据不改采集器。
- **未接线**：本对账的每日自动复算（与 R415 快照同跑）未接定时；现在只能手动 `reconcile`。
- 独占域里出现第三方中转/API 域名（如 `api.cpass.cc`、`anyrouter.top`）只作**访问计数**呈现，
  不对其用途/合规做任何判断。
- 时间窗：非 Default profile 的最后访问多停在 2026-04（Profile 3 到 04-24、Profile 4 到 09-21），
  即这些 profile 多为历史遗留；本结论只对**本次快照时点**负责。
