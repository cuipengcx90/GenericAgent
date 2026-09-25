# R420 · pmset 电源/睡眠日志接入滚动源快照器（第14批 C0，三路评审 8.00 最高）

- 日期：2026-09-26
- 任务来源：temp/TODO.txt 第14批 C0（`[ ] 环境 | 【第14批·三路评审 9/6/9=8.00（本批最高…）】易失滚动源接入`）
- 范围声明：**只做源接入 + 真采 + 解析**。**不注册定时、不改 SOP**（R415 已声明「定时未接线=待批」，本批沿用）。
- 交付物：`tools/rolling_source_snapshot.py`（+544/−2 行，1226 行 / 64159 B）+ 快照 `temp/pmset_snapshots/2026-09-26/`、`temp/wifi_snapshots/2026-09-26/`
- 证据脚本（temp，非交付）：`temp/pmset_evidence.py` → `temp/pmset_evidence_2026-09-26.log`；`temp/kc_conv_probe.py`、`temp/kc_conv_probe2.py`

---

## §1 结论

1. `/var/log/powermanagement/` 已作为**目录型滚动源**接入快照器（逐文件拷贝 + 内容寻址硬链接去重），并新增 `powermanagement` 解析器：逐 `.asl` 用官方解码器 `syslog -f` 抽取 `sleep_enter / darkwake / wake / ac / battery` 事件与「睡眠窗口」区间。
2. 今日真采落地：**16 文件 / 33,364,032 字节**，逐文件 `sha256` 与源目录**全部一致**（只读自证），快照自带 `manifest.json`（含每文件 `sha256`/`mtime`/`dst_match`）与 `events.json`/`events.tsv`/`windows.tsv`。
3. `--selftest` **22/22 PASS, rc=0**（R415 时为 12/12 ⇒ 本批新增 10 例，含 4 类新负控：源缺失 / 空源 / **DarkWake 不得计入 wake** / 委派失败不假绿）。
4. 与 knowledgeC 熄屏口径对账：**两源不可互推**（详见 §4.4）——不仅数值不等，且**方向都不一致**；更关键的是 kc 存在**迟写回填**，同一历史日的 kc 值随快照变新而剧变（09-22：0.90h → 5.84h → 11.78h）。本项结论按 TODO 原文「一致即写一致」的口径，写为**不一致 + 机制解释**。
5. wifi.log 作同批第二源：睡眠窗口内网络行为 **0 行**（quiet-during-sleep），与 pmset 窗口互证；未输出 SSID。

---

## §2 交付物与改动

### 2.1 快照器改动（`tools/rolling_source_snapshot.py`）

| 项 | 内容 |
|---|---|
| 新源 `pmset` | `kind=dir_copy`，`src_dir=/var/log/powermanagement`，`include=['*.asl','StoreData']`，落点 `temp/pmset_snapshots/`，`artifact=events.json` |
| 新源 `wifilog` | `kind=file_copy`，`src_file=/var/log/wifi.log`，落点 `temp/wifi_snapshots/`，`artifact=wifi.log`（newsyslog 每日 00:30 轮转） |
| 新解析器 | `parse_powermanagement`（`syslog -f` 解码→逐行归类；窗口= `Entering Sleep state` → 下一次 `Wake`/`DarkWake`）、`parse_wifilog`（airport 事件 + DHCP 状态变更，**不输出 SSID**） |
| 去重 | 目录型源按内容寻址：同内容文件走硬链接（selftest T7 实证 `{'2026.09.20.asl': 'hardlink', 'StoreData': 'hardlink'}`） |
| 未接线登记 | `NOTES` 明写「只做源接入 + 解析，**未接定时**（待批）」；另登记两条边界：`wifi.log.N.bz2` 未纳入、`pmset -g log` 是活性视图 |

改动量（现场）：

```
544	2	tools/rolling_source_snapshot.py
```

### 2.2 真采产物

```
=== 快照目录 ===
temp/pmset_snapshots/2026-09-26/
  2026.09.12.asl  333590   2026-09-13.asl  498270   … 2026.09.26.asl  26140
  StoreData            8
  events.json    103110   events.tsv   739   manifest.json  8488   windows.tsv  40292

=== manifest（源侧自证） ===
revisions[-1] = {"ts": "2026-09-26 02:11:13", "date": "2026-09-26", "n_files": 16,
                 "total_bytes": 33364032,
                 "fileset_sha256": "8d63dfd5ea514dd44a56af5ef299cef0daa8222820b2ac94c132fcca3d1a40e9"}
```

wifi 侧：`temp/wifi_snapshots/2026-09-26/wifi.log` **127544 B**，`revisions[-1].sha256` 前 16 位 `79731653cbd40b30`。

### 2.3 口径说明（防误读）

`list` 子命令打印的 `bytes` 列是**当日产物文件**（`src['artifact']`）的大小，不是源侧拷贝总量：

```
■ pmset   电源/睡眠日志（/var/log/powermanagement 全目录：*.asl + StoreData）
   根=…/temp/pmset_snapshots  天数=1  最新=2026-09-26
     2026-09-26  103110 B            ← = events.json 的字节数
   不可再生=10/10  当前覆盖=零覆盖（全仓无任何 powermanagement 快照；实测窗口 2026.09.12–今 ≈15 天）
```

源侧总量（33,364,032 B）在 `manifest.json` 的 `total_bytes`。两数**不是矛盾**，是两种口径。

---

## §3 验收逐条对账（TODO 原文 ①–⑥）

| # | 验收要求 | 结果 | 证据 |
|---|---|---|---|
| ① | 快照器新增该源且 `--selftest` 全绿（含 ≥1 新增负控） | ✅ | `selftest: 22/22 PASS`（rc=0）；新增负控 4 类：T8 空源被拒 / T9 DarkWake 不计入 wake / T5 委派失败不假绿 / T2 源缺失被拒 |
| ② | 真采今日一份并打印路径/字节/sha1（字节用 `stat -f %z` 现取） | ✅ | 16 文件 / 33364032 B；`fileset_sha256=8d63dfd5…`；逐文件 sha 见 §4.1（`stat -f "%z %N" …` 复跑命令已写入证据日志） |
| ③ | 对 ≥1 个 `.asl` 解析出当日睡眠/唤醒/充电事件计数，命令可复跑且原始输出入报告 | ✅ | §4.2 全 15 份 `.asl` 逐日表（复跑：`python3 tools/rolling_source_snapshot.py window --source pmset --all-days`） |
| ④ | 与 knowledgeC 熄屏口径对账同日差异并给出解释 | ✅ | §4.4：**不一致**（数值与方向均不同），并给出机制解释 + row 级铁证 |
| ⑤ | 源目录 16 文件 sha256 快照前后逐个一致（只读自证） | ✅ | §4.1 逐文件 `YES` 16/16；`合计 16 文件 / 33364032 字节；逐文件 sha256 全同 = True` |
| ⑥ | `/var/log/wifi.log` 作同批第二源只做「睡眠窗口内是否断网」对账，**不输出 SSID、不单独立项** | ✅ | §4.5：`窗口内 0 行（DHCP 0）`；解析器无任何 SSID 字段；源登记在 `NOTES`（未单独立项） |

---

## §4 实测证据（原始输出）

### 4.1 只读自证：快照 ↔ 源目录逐文件对照

```
[1] 快照目录 <-> 源目录 逐文件对照（sha256 前后一致=只读自证）
    快照: /Users/cuip/projects/GenericAgent/temp/pmset_snapshots/2026-09-26
    file                      bytes(snap)   bytes(src) sha256[0:16]     same
    2026.09.12.asl                 333590       333590 9854f189f529e61a YES
    2026.09.13.asl                 498270       498270 1a55d883fc36b155 YES
    2026.09.14.asl                1058084      1058084 03f20a529bbee655 YES
    2026.09.15.asl                1619193      1619193 687e44ae34faf9ae YES
    2026.09.16.asl                2096039      2096039 e172a29e07e33fbc YES
    2026.09.17.asl                3042875      3042875 ec89c454bd5f0c53 YES
    2026.09.18.asl                1220370      1220370 628a45b4fb3fe0ef YES
    2026.09.19.asl                1291895      1291895 4fc6bf323cdba443 YES
    2026.09.20.asl                2325944      2325944 9bb7f6b3c2d87aa2 YES
    2026.09.21.asl                3052223      3052223 5afbb34dad547ab6 YES
    2026.09.22.asl                5114812      5114812 54379a8e28b8f172 YES
    2026.09.23.asl                6419251      6419251 9e7e490308facaf5 YES
    2026.09.24.asl                4245327      4245327 bb795087a0905955 YES
    2026.09.25.asl                1020011      1020011 057a6afc2176270e YES
    2026.09.26.asl                  26140        26140 2e16356a29909ffb YES
    StoreData                           8            8 d7edf658c0031468 YES
    合计 16 文件 / 33364032 字节；逐文件 sha256 全同 = True
```

### 4.2 逐日 `.asl` 事件计数（解析通路=官方解码器 `syslog -f`）

```
[2] 逐日 .asl 解码 + 事件计数（复跑: python3 tools/rolling_source_snapshot.py window --source pmset --all-days）
    file                 lines method   sleep_enter   darkwake   wake    ac  batt  sleep_h
    2026.09.12.asl         221 syslog             0          0      0     2     0     0.00
    2026.09.13.asl         354 syslog             0          0      0     6     0     0.00
    2026.09.14.asl         252 syslog             0          0      0     6     0     0.00
    2026.09.15.asl         724 syslog             0          0      0     8     0     0.00
    2026.09.16.asl         815 syslog             0          0      0     2     0     0.00
    2026.09.17.asl        1393 syslog             0          0      0     4     0     0.00
    2026.09.18.asl         831 syslog             0          0      0     4     0     0.00
    2026.09.19.asl         811 syslog            36         34      1    52     0     8.01
    2026.09.20.asl        1738 syslog            37         37      1    80     0     9.04
    2026.09.21.asl        1230 syslog             0          0      0     6     0     0.00
    2026.09.22.asl        2333 syslog            88         88      1     4     0     2.15
    2026.09.23.asl        4962 syslog           299        297      2     9     0     9.42
    2026.09.24.asl        3684 syslog           197        195      3     9     0     8.90
    2026.09.25.asl         531 syslog             0          0      0     8     0     0.00
    2026.09.26.asl          10 syslog             0          0      0     0     0     0.00
```

汇总（`events.json` → `summary`）：

```
{"n_files": 15, "n_decoded_lines": 19889,
 "events_total": {"sleep_enter": 657, "darkwake": 651, "wake": 8, "ac": 200, "battery": 0},
 "sleep_windows_total": 657, "sleep_secs_total": 135118,
 "decode_days_with_events": 5, "decoder_match_all": true}
```

判读要点（本批新得，非推测）：

- **只有 5 天有睡眠事件**（09-19/20/22/23/24）；其余 10 天 `sleep_enter=0`、仅有 `ac`（供电）计数 ⇒ 那几天**未进入系统睡眠**（仅合盖/屏幕事件不会进 pmset 睡眠态）。`battery=0` 全期 ⇒ 本机近期一直插电。
- `darkwake` 几乎与 `sleep_enter` 1:1（651/657）——DarkWake 是同一晚的多次短唤醒，**不得与 `wake`（8 次，真唤醒）混计**（selftest T9 即锁死该判据：喂 `DarkWake from …` 单行时 `wake=0`）。
- 窗口合计 135118 s ≈ **37.5 小时**，分布在 5 天（09-23 最长 9.42h）。
- `decoder_match_all=true`：解码行数与原始字节侧 `raw_counts` 三键（`Entering Sleep state` / `DarkWake from` / `Wake from`）逐份一致——**双通路互证**。

### 4.3 `pmset -g log` 活视图对照（仅作通路一致性旁证）

```
[3] pmset -g log 活视图对账（复跑: sudo pmset -g log | grep -c "Entering Sleep state"）
    活视图总行数=48164；"Entering Sleep state"=657；"DarkWake"=1306
    ★ 活视图为 ASL 索引滚动读，本机实测回溯窗口短于目录（本项只作通路一致性旁证）
```

活视图 `Entering Sleep state=657` 与快照解析 `sleep_enter=657` **逐数相等** ⇒ 解析器未漏行未重复。但活视图回溯仅到 **2026-09-19**（09-12~09-18 那 7 份 `.asl` 不在活视图内，解析计数为 0 也与之自洽），**故活视图不能替代快照**。

### 4.4 与 knowledgeC 熄屏口径对账（验收④，结论=**不一致**）

```
[4] knowledgeC /display/isBacklit 口径对账（v=0=熄屏；kc 快照复制副本 mode=ro）
  [kc 快照 2026-09-22] isBacklit 行数=1447
    2026-09-19  kc熄屏=17.02h  kc窗口=24.00h | pmset睡眠窗口=8.01h | 差=9.00h
    2026-09-20  kc熄屏=1.71h  kc窗口=24.00h | pmset睡眠窗口=9.04h | 差=-7.33h
    2026-09-22  kc熄屏=0.90h  kc窗口=24.00h | pmset睡眠窗口=2.15h | 差=-1.26h
  [kc 快照 2026-09-23] isBacklit 行数=1512
    2026-09-22  kc熄屏=5.84h  kc窗口=24.00h | pmset睡眠窗口=2.15h | 差=3.68h
  [kc 快照 2026-09-25] isBacklit 行数=1763
    2026-09-22  kc熄屏=11.78h  kc窗口=24.00h | pmset睡眠窗口=2.15h | 差=9.63h
    2026-09-23  kc熄屏=15.26h  kc窗口=24.00h | pmset睡眠窗口=9.42h | 差=5.84h
    2026-09-24  kc熄屏=15.58h  kc窗口=24.00h | pmset睡眠窗口=8.90h | 差=6.68h
```

两条独立证据说明为什么**不能**用它们互推：

**(a) 方向都不一致。** 09-19 kc 反而**长于** pmset（+9.00h），09-20 又**短于** pmset（−7.33h）。若两者测同一件事，符号不该翻转 ⇒ 语义不同：kc `isBacklit=0` 测的是**屏幕熄灭**（合盖、屏保熄屏、系统睡眠都会触发），pmset 窗口测的是**系统睡眠态**。

**(b) kc 存在迟写回填，同一历史日的值随快照变新而剧变。**

```
=== kc 收敛探针（固定日 2026-09-22 跨快照对比；只读副本）===
kc 快照        rows0  rowsD    off_h       first_start(DAY)        last_start(DAY)
2026-09-22       719     43     0.90    2026-09-22 00:28:36    2026-09-22 02:46:00
2026-09-23       751    108     5.84    2026-09-22 00:28:36    2026-09-22 15:49:28
2026-09-25       876    109    11.78    2026-09-22 00:28:36    2026-09-22 18:03:08
★ 判读：同一历史日的 off_h 随快照变新而增大 ⇒ kc 存在迟写/回填，历史值须以最新快照为准且旧值不可复用
```

row 级 diff 定位到**唯一差异**（09-23 快照 vs 09-25 快照，键对齐 `ZSTARTDATE`）：

```
=== 行级差异（日 2026-09-22）: 09-23 快照 n=108 vs 09-25 快照 n=109 ===
A.start   A.end   A.v | B.start                B.end                  B.v | diff
None      None    None| 2026-09-22 18:03:08    2026-09-23 01:40:28    0   | NEW
小计: ZENDDATE 变长/改写=1 新增行=1 取值变=0（同名 ZSTARTDATE 键对齐）
```

即：只多出 **1 行**——`09-22 18:03:08 → 09-23 01:40:28`（7.62h 的熄屏区间，v=0）。该行恰好在 **09-23 凌晨区间结束之后**才写入 kc。而 5.84h → 11.78h 的增量 +5.94h，正等于 `18:03 → 24:00` 的 5.95h ⇒ **kc 的行要等熄屏区间结束才落库**，跨夜长区间需 T+1 才可见。**故「当天采的 kc」对当天/历史日必然不完整，历史值必须以最新快照为准且不可复用。**

结论：对账结果为**不一致**，且已定位为两源语义差异 + kc 迟写两重原因；本工具同时保留两个源，**不做互相校验的自动化**（避免把两种口径的差当成数据错误）。

### 4.5 wifi.log 睡眠窗口对账（验收⑥）

```
[5] wifi.log 睡眠窗口内对账（不输出 SSID；复跑: python3 tools/rolling_source_snapshot.py window --source wifilog）
    wifi 行数=1012（带时间戳=1012，DHCP=3），源文件 sha256 见 manifest
    ★ 睡眠窗口内 wifi 行为 0（quiet-during-sleep）⇒ 睡眠期无网络活动，与 pmset 窗口互证
```

边界（已登记进工具 `NOTES`）：只采当前 `wifi.log`（newsyslog 每日 00:30 轮转），`wifi.log.N.bz2`（N=0..10）**未纳入**，故更早窗口的同期对账不可得。

---

## §5 selftest 原始输出（验收①）

```
T2 负控-源缺失被拒(rc=2)                     PASS
T3 负控-漂移被抓 / 负控-不误报(identical)     PASS
T4 负控-可写连接探针=readonly False           PASS
T4 负控-只读连接探针=readonly True            PASS
T5 负控-委派失败不假绿                        PASS
  ├ 解析: 1 份 .asl → 解码 4 行（days_with_events=1，decoder_match_all=True）
  │  事件合计: sleep_enter=2 darkwake=1 wake=1 ac=0 battery=0
  └  睡眠窗口: 2 段，共 5.3 小时
T6 正例-目录快照生成+事件解析 {'sleep_enter': 2, 'darkwake': 1, 'wake': 1, 'ac': 0, 'battery': 0}  PASS
T6 正例-解码器/原始字节双路口径一致            PASS
T6 正例-源侧自证 unchanged                     PASS
T6 正例-逐文件逐字节校验                       PASS
T7 正例-同内容硬链接去重 {'2026.09.20.asl': 'hardlink', 'StoreData': 'hardlink'}  PASS
T8 负控-空源被拒(rc=2)                        PASS
T9 负控-DarkWake 不计入 wake {'sleep_enter': 0, 'darkwake': 1, 'wake': 0, 'ac': 0, 'battery': 0}  PASS
T10 正例-窗口内判 activity / 负控-窗口外判 quiet  PASS
T11 正例-单文件快照+解析+自证 2                PASS
---
selftest: 22/22 PASS
```

旧版（HEAD 版本）实测为 `selftest: 12/12 PASS`、用例仅 T1–T5，本批扩到 T1–T11 / 22/22（+10 例）；T9 是本批核心负控：**`DarkWake from` 必须不计入 `wake`**（本机实测 DarkWake 与 sleep_enter 近 1:1，若混计会把「一晚 299 次短唤醒」误报成 299 次真唤醒）。

---

## §6 边界与未接线（**未做、不代批**）

1. **定时未注册**：`sche_tasks/` 与 `LaunchAgents/` 均未新增任何条目 ⇒ 快照仍靠人工/后续批次触发。本批明文不做（TODO 原文：不注册定时、不改 SOP）。
2. **窗口仅约 15 天且不可再生**：现存最老 `2026.09.12.asl`；`.asl` 随轮转删除，**越晚开始快照，缺口越永久**。
3. `wifi.log.N.bz2` 未纳入（见 §4.5）；`pmset -g log` 活性视图回溯短于目录（见 §4.3）。
4. kc 迟写回填（§4.4b）是**既有事实**，本批只做定位与记录，未改 `memory/kc_profile/profile.py` 的口径，也未接线守卫 `tools/kc_snapshot_guard.py`。

---

## §7 复跑命令（PowerShell/bash 均可照抄）

```bash
# 1) 自测
python3 tools/rolling_source_snapshot.py selftest

# 2) 真采（幂等：同日已存在则返回 already-done）
python3 tools/rolling_source_snapshot.py snapshot --source pmset
python3 tools/rolling_source_snapshot.py snapshot --source wifilog

# 3) 覆盖/窗口
python3 tools/rolling_source_snapshot.py list
python3 tools/rolling_source_snapshot.py window --source pmset --all-days

# 4) 源侧字节（权威口径）
stat -f "%z %N" /var/log/powermanagement/* | tail -3
```

---

## §8 与 R415 的衔接

R415 已把 `pmset .asl` 列为「下一批首选（15 天窗口 · 零覆盖 · 36M）」，本批即兑现：源接入完成、当日快照落地、解析可用、只读自证闭环。**下一批的候选动作**（本批不做）：把三个源（chrome/kc/pmset+wifi）注册为每日定时快照——须用户批准（`revers=0`，属「注册定时」类变更）。
