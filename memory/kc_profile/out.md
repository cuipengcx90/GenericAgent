# kc 小时级作息底稿（快照 2026-09-25，只读）

窗口 2024-12-27 → 2026-09-25 ｜ ZOBJECT=17366 ｜ 应用事件=10003 ｜ 通知=5530

逐流窗口与体量（自证：语料非空、每流各自滚动窗）：

- /app/usage                 2026-09-01 → 2026-09-25 ｜ 10003 行
- /notification/usage        2026-08-28 → 2026-09-25 ｜ 5530 行
- /display/isBacklit         2026-08-28 → 2026-09-25 ｜ 1584 行
- /bluetooth/isConnected     2026-08-31 → 2026-09-23 ｜ 236 行
- /app/mediaUsage            2026-09-10 → 2026-09-22 ｜ 7 行
- /discoverability/signals   2024-12-27 → 2025-04-14 ｜ 6 行

## 1. 小时级作息（0–23 时）

| 时 | app事件 | 唯一应用 | 前台秒 | 通知 | 熄屏(分) | 专注块(分) |
|---|---|---|---|---|---|---|
| 00 | 47 | 11 | 5564 | 60 | 660 | 57 |
| 01 | 26 | 10 | 4864 | 18 | 736 | 0 |
| 02 | 13 | 6 | 4104 | 8 | 817 | 46 |
| 03 | 1 | 1 | 388 | 0 | 783 | 0 |
| 04 | 75 | 8 | 3114 | 7 | 790 | 54 |
| 05 | 9 | 3 | 806 | 0 | 819 | 14 |
| 06 | 4 | 2 | 1587 | 2 | 846 | 0 |
| 07 | 30 | 5 | 4189 | 14 | 883 | 0 |
| 08 | 21 | 11 | 6779 | 31 | 853 | 20 |
| 09 | 331 | 19 | 19791 | 199 | 800 | 275 |
| 10 | 1228 | 25 | 64552 | 682 | 245 | 982 |
| 11 | 923 | 22 | 46904 | 353 | 366 | 906 |
| 12 | 350 | 17 | 28323 | 131 | 753 | 352 |
| 13 | 886 | 20 | 49576 | 534 | 331 | 834 |
| 14 | 978 | 21 | 54842 | 602 | 249 | 875 |
| 15 | 1101 | 18 | 60811 | 505 | 277 | 1064 |
| 16 | 1243 | 18 | 60600 | 690 | 334 | 968 |
| 17 | 1272 | 20 | 54513 | 607 | 414 | 1008 |
| 18 | 1046 | 22 | 45873 | 412 | 496 | 849 |
| 19 | 83 | 11 | 8415 | 65 | 845 | 134 |
| 20 | 47 | 8 | 11896 | 77 | 812 | 124 |
| 21 | 41 | 7 | 11596 | 110 | 744 | 152 |
| 22 | 176 | 18 | 16345 | 233 | 614 | 249 |
| 23 | 72 | 9 | 8227 | 190 | 700 | 53 |

熄屏(/display/isBacklit v=0) 合计 = 252.78 h（窗口 674.26 h，占 37.5%）

## 2. 专注块（间隔≤300s 归链，链长≥1500s）

链总数=243 ｜ 计块=96 ｜ 块内合计=150.27 h ｜ 23/25 天有≥1块

最长 8 块：

- 09-16 12:28 ｜ 392.7 分钟 ｜ 352 事件 ｜ top=com.mitchellh.ghostty, com.google.Chrome, com.electron.lark
- 09-17 12:59 ｜ 356.1 分钟 ｜ 334 事件 ｜ top=com.mitchellh.ghostty, com.electron.lark, com.google.Chrome
- 09-14 13:10 ｜ 326.6 分钟 ｜ 683 事件 ｜ top=com.mitchellh.ghostty, com.google.Chrome, com.tencent.xinWeChat
- 09-10 13:32 ｜ 316.3 分钟 ｜ 373 事件 ｜ top=com.mitchellh.ghostty, com.electron.lark, com.google.Chrome
- 09-18 13:30 ｜ 298.2 分钟 ｜ 397 事件 ｜ top=com.mitchellh.ghostty, com.electron.lark, com.google.Chrome
- 09-01 14:31 ｜ 254.8 分钟 ｜ 364 事件 ｜ top=com.mitchellh.ghostty, com.electron.lark, com.google.Chrome
- 09-04 15:20 ｜ 251.1 分钟 ｜ 355 事件 ｜ top=com.mitchellh.ghostty, com.tencent.xinWeChat, com.electron.lark
- 09-07 14:56 ｜ 248.2 分钟 ｜ 504 事件 ｜ top=com.mitchellh.ghostty, com.google.Chrome, com.electron.lark

## 3. Top 应用（前台小时 / 事件数）

- com.mitchellh.ghostty                   90.08 h   3393 次
- com.google.Chrome                       25.98 h   1921 次
- com.electron.lark                       14.78 h   2198 次
- com.tencent.xinWeChat                    6.86 h    585 次
- md.obsidian                              4.70 h    560 次
- io.github.clash-verge-rev.clash-verge-rev   2.51 h    106 次
- com.alibaba.DingTalkMac                  2.28 h     88 次
- com.apple.systempreferences              1.85 h     37 次
- com.jetbrains.intellij                   1.43 h    262 次
- com.jinghaoshe.qspace.pro                1.43 h    239 次
- com.netease.uuremote                     1.26 h     43 次
- com.apple.Preview                        0.92 h     40 次

## 4. 通知

合计 5530 条 ｜ 日均 221.2 条

- com.electron.lark                         2925
- com.apple.Terminal                        2424
- com.google.Chrome                          135
- com.Snipaste                                23
- com.apple.BTMNotificationAgent              11
- _SYSTEM_CENTER_:com.apple.SoftwareUpdateNotification     5
- com.apple.controlcenter.notifications.low-battery     5
- _SYSTEM_CENTER_:com.apple.diskspaced         2

## 5. 每日首末事件（作息粗判）

| 日 | 首事件 | 末事件 | 事件数 |
|---|---|---|---|
| 2026-09-01 | 13:07:30 | 18:45:09 | 415 |
| 2026-09-02 | 09:50:57 | 18:48:34 | 789 |
| 2026-09-03 | 04:05:30 | 23:56:08 | 815 |
| 2026-09-04 | 00:02:10 | 23:29:10 | 733 |
| 2026-09-05 | 00:35:09 | 23:38:34 | 34 |
| 2026-09-06 | 09:13:39 | 22:10:52 | 39 |
| 2026-09-07 | 01:59:44 | 19:02:21 | 698 |
| 2026-09-08 | 09:48:29 | 19:00:27 | 455 |
| 2026-09-09 | 09:35:21 | 22:01:58 | 246 |
| 2026-09-10 | 01:20:16 | 18:45:29 | 563 |
| 2026-09-11 | 10:12:26 | 23:38:34 | 315 |
| 2026-09-12 | 00:52:09 | 22:49:11 | 50 |
| 2026-09-13 | 01:08:50 | 15:55:09 | 28 |
| 2026-09-14 | 09:47:15 | 18:35:54 | 934 |
| 2026-09-15 | 00:27:04 | 22:59:13 | 407 |
| 2026-09-16 | 06:07:32 | 18:54:50 | 477 |
| 2026-09-17 | 10:06:56 | 18:53:04 | 484 |
| 2026-09-18 | 01:30:11 | 18:27:48 | 587 |
| 2026-09-19 | 10:25:40 | 14:58:58 | 7 |
| 2026-09-20 | 09:44:46 | 18:35:40 | 510 |
| 2026-09-21 | 09:48:17 | 18:50:59 | 310 |
| 2026-09-22 | 09:45:16 | 22:18:54 | 317 |
| 2026-09-23 | 09:40:50 | 19:01:54 | 500 |
| 2026-09-24 | 07:25:58 | 23:58:39 | 264 |
| 2026-09-25 | 00:00:29 | 01:48:59 | 26 |

## 6. 交叉验证（Chrome 只读副本，独立来源）

visits=31498（全库，含自驾自造访问；R362 已证 21.5% 系自造 ⇒ 只比形状）

| 时 | kc事件占比 | Chrome占比 |
|---|---|---|
| 00 | 0.5% | 0.7% |
| 01 | 0.3% | 0.2% |
| 02 | 0.1% | 0.2% |
| 03 | 0.0% | 0.1% |
| 04 | 0.7% | 0.2% |
| 05 | 0.1% | 0.0% |
| 06 | 0.0% | 0.0% |
| 07 | 0.3% | 0.5% |
| 08 | 0.2% | 0.5% |
| 09 | 3.3% | 3.6% |
| 10 | 12.3% | 11.6% |
| 11 | 9.2% | 11.6% |
| 12 | 3.5% | 2.8% |
| 13 | 8.9% | 11.4% |
| 14 | 9.8% | 9.8% |
| 15 | 11.0% | 9.0% |
| 16 | 12.4% | 9.5% |
| 17 | 12.7% | 9.9% |
| 18 | 10.5% | 7.8% |
| 19 | 0.8% | 1.3% |
| 20 | 0.5% | 2.4% |
| 21 | 0.4% | 1.9% |
| 22 | 1.8% | 1.7% |
| 23 | 0.7% | 3.4% |

小时级 Pearson r = 0.952（n=24 个小时桶）

### 6b. 交叉验证二：zsh 历史（R377 同源，独立于 kc）

`~/.zsh_history` 带 epoch 时间戳条数 = 10020；落进 kc 窗口（2026-08-28 02:18 → 2026-09-25 04:34）= 636 条

| 三态 | 时长(h) | zsh 命令 | 命令/时 | kc 前台事件/时 |
|---|---|---|---|---|
| 有行 v=1 | 127.5 | 290 | 2.27 | 39.0 |
| 有行 v=0 | 252.8 | 9 | 0.04 | 0.0 |
| 无行(空隙) | 294.0 | 337 | 1.15 | 17.1 |

小时级 Pearson r = 0.795（kc 前台事件 vs zsh 命令，n=24；窗口内 636 条 / 全史 10020 条 = 6.3%）
⇒ **熄屏(v=0)期间 zsh 命令 9 条**（0.04 条/时 vs v=1 的 2.27）：命令需击键，可作 v=0=离场的独立旁证；其中 8 条落在 v=0 段首/尾 120 s 内（=临熄屏前敲的最后一条），属边界效应。

## 7. 数据字典与可复跑 SQL（承接 R371 字段矩阵，不重造）

语料 = `temp/kc_snapshots/2026-09-25/knowledgeC.db`（**只读**副本，`file:...?mode=ro`；建议用 `temp/kc_snapshots/<日期>/knowledgeC.db`）。

| 用途 | 表/字段 | 口径 |
|---|---|---|
| 前台事件 | `ZOBJECT.ZSTREAMNAME='/app/usage'` | 应用名=`ZVALUESTRING`；**时长=`ZENDDATE-ZSTARTDATE`（秒）** |
| 熄屏状态 | `ZSTREAMNAME='/display/isBacklit'` | 取值=`ZVALUEINTEGER`（1=亮 0=熄）；区间=`ZSTARTDATE..ZENDDATE` |
| 通知 | `ZSTREAMNAME='/notification/usage'` | bundle=`ZSTRUCTUREDMETADATA.Z_DKNOTIFICATIONUSAGEMETADATAKEY__BUNDLEID` |
| ⚠ 陷阱 | `ZVALUEDOUBLE` | `/app/usage` 下系位型垃圾（实测 1e19 量级），**不得当秒用** |
| 时区 | 全部时间列 | Cocoa epoch 偏移 978307200 秒已并入 SQL 或 Python |

本文件每个数字均由下列 SQL 直接得出（脚本内同源，`temp/kc_profile/profile.py`）：

```sql
select ZSTARTDATE t, ZENDDATE e, ZVALUESTRING app from ZOBJECT where ZSTREAMNAME='/app/usage' order by ZSTARTDATE;
-- 小时桶：Python 侧 datetime.fromtimestamp 后按 hour 归桶（SQLite localtime 同口径）
select ZSTARTDATE t, ZENDDATE e, ZVALUEINTEGER v from ZOBJECT where ZSTREAMNAME='/display/isBacklit' order by ZSTARTDATE;
-- 三态判据：区间含 t 则取该行 v；否则 = 空隙(gap)
-- 只读打开：sqlite3 'file:<副本路径>?mode=ro'；date(ZSTARTDATE+978307200,'unixepoch','localtime') 取本地日
```


## 8. 数据质量与口径自证（/display/isBacklit 三态密度）

该流的状态行**不铺满时间轴**（本窗口 1584 行、62 处断续），故先按「有行 v=1 / 有行 v=0 / 无行」三分区
各自统计前台事件密度作为判读依据：

| 区段 | 时长(h) | 前台事件 | 事件/时 | 判读 |
|---|---|---|---|---|
| 有行 v=1 | 127.5 | 4968 | 39.0 | 使用中（密集） |
| 有行 v=0 | 252.8 | 8 | 0.0 | 离场／熄屏 |
| 无行(空隙) | 294.0 | 5027 | 17.1 | **混合**（含睡眠与使用） |

口径声明（写死，防后续误用）：

1. **熄屏只认 v=0**（本窗口 252.78 h，37.5%）；「无行」是**混合区**（含睡眠与使用），当场亮处理会高估。
2. 因此**不输出**「窗口−熄屏=亮屏」的推算口径（实测该式子给 17.5 h/天，与前台证据矛盾）。
3. 「在场/活跃」改用 `ZSTREAMNAME='/app/usage'` 的前台事件耗时合计（口径=Σ(ZENDDATE−ZSTARTDATE)，
   因 `ZVALUEDOUBLE` 系位型垃圾、不可当秒用——已实测比对）。
4. 各流窗口不等长（本轮实测：app/usage 25 天、notification 29 天、isBacklit 29 天），
   跨流比较一律先裁剪到公共窗。
5. **v=0 只是「熄屏」的下界**：设备睡眠时该流同样不写行（与空隙同因），故 252.78 h 不能读作「全天熄屏总量」。
   按窗口 674.3 h（28 天）折算：v=0 ≈ 9.0 h/天、空隙 ≈ 10.5 h/天、前台事件 ≈ 5.7 h/天。
6. 本文件所有数值均来自 `temp/kc_profile/profile.py` 一次运行（2026-09-25 05:03 快照）；知识库本身滚动删除（≈4 周），
   故任何跨日比较必须用已落盘的 `temp/kc_snapshots/` 快照，不得回查源库。

