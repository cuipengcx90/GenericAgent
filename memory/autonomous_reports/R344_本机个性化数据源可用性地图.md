# R344 本机「未挖掘个性化数据源」可用性地图（只读实测）

- 时间：2026-09-22 08:2x-08:4x
- 工具：`tools/personal_source_map.py`（幂等，`all` 一键重生）
- 原始数据：`temp/datasource_map/{probe,samples,sfl,wechat}.json`、`MAP.md`、`db_copy/`（副本约 50.9MB）
- 纪律：**全程只读**——源库一律不写、不 VACUUM、不建 journal，所有查询跑在 temp 副本上（`file:...?mode=ro`）

## 一句话结论

macOS 这 12 类"听起来需要 TCC 授权"的数据源，**实测没有一个是读不了的**：11 个 Sqlite/bplist 都能"复制副本 + 只读查询"，真正的坑只有两
个 ——（a）**必须连 `-wal` 一起拷**（否则行数假性为 0），（b）容器目录**递归 `find` 会被 TCC 挂死**（不是返回 0 条，而是不返回）。可读 ≠ 有
料：Messages / Mail / Photos / Reminders / Notes 全机**空库**，真正有内容的是 **KnowledgeC（17400 条）与 Chrome 历史（12501 条）**。

## 一、能读 / 不能读 / 怎么读（12 源实测）

| 源 | 体积 | 可读? | 实测行数 | 怎么读 |
|---|---|---|---|---|
| **KnowledgeC**（App 使用/通知/屏幕点亮） | 13.1MB | ✅ | **17400**（/app/usage 10000、/notification/usage 5741、/display/isBacklit 1396） | 直接拷副本（含 -wal/-shm）只读查询 |
| **Chrome 历史** | 33.2MB | ✅ | **12501** | 同上；Chrome 运行中也照拷 |
| Safari 历史 | 0.1MB | ✅ | 29 | 同上（量小，已有 TrendRadar 覆盖阅读侧） |
| Notes 新库 NoteStore | 0.3MB | ✅ | 8 对象（含标题 2） | **必须连 -wal**；正文在 `ZICNOTEDATA`（protobuf+gzip），拿到标题≠拿到正文 |
| Notes 旧库 NotesV7 | 0.2MB | ✅ | 0 | 结构在、数据空（本机已迁新库） |
| Messages chat.db | 0.3MB | ✅ | 0（18 表中仅 `_SqliteDatabaseProperties` 有 6 行） | 可读但**空库**（含 1MB WAL 拷入后仍 0） |
| Mail Envelope Index | 0.6MB | ✅ | 0 封信（mailboxes=2） | 可读但没收到过信（Mail 未启用） |
| Reminders | 0.7MB | ✅ | 0 | 可读但未使用 |
| Photos 库 | 2.2MB | ✅ | 0（ZASSET=0） | 可读但库空/iCloud 已优化 |
| 最近应用 sfl3 | 7.9KB | ✅ | 19 条（DingTalk/Ghostty/Chrome/Lark/Obsidian/OrbStack/Typora/UURemote…） | `plutil -convert xml1` 或 `strings -a` |
| 最近文档 sfl3 | 10.3KB | ✅ | 10 条计数 | 同上；路径存为 NSKeyedArchiver bookmark（要真实路径需解归档） |
| 微信容器 | 目录 4 项 | ⚠️ 目录可列、文件遍历挂起 | 账号 `wxid_0063850638312_25f3` 在，库在 `db_storage/`（contact/message/session/favorite/sns/…，另 `msg/attach|video|file`） | **`find` 递归超 25s 不返回**；只能 `ls` 限深；库需密钥解密 |

## 二、真实内容样例（原文片段，已入库 `samples.json`）

- **KnowledgeC /app/usage**：`2026-09-21 18:50:59 com.electron.lark`、`18:50:56 com.google.Chrome`、`18:49:51 com.mitchellh.ghostty`
- **Chrome**：`(OneTab, chrome-extension://…hal)`、`(Example Domain, https://example.com/)`、`(Google, https://www.google.com/)`
- **Safari**：`http://100.114.166.15:18080/`（访问 6 次）、`http://121.40.75.178/`（4 次）
- **Notes**：仅 2 条标题 —— `nodes.txt.new`、`√VZΩZZΩΩΩΩΩZZ`（后者疑似乱码/加密残留）
- **sfl3 最近应用**：`DingTalk.app / Ghostty.app / Google Chrome.app / Lark.app / Obsidian.app / OrbStack.app / Typora.app / UURemote.app`

## 三、三个必须知道的坑（都会造成"看起来没数据"的误判）

1. **WAL 不拷 = 假空**：实测 Notes 不带 `-wal` 只能看到 2 个对象，带上后 8 个；做法是 `cp db db-wal db-shm` 三件套一起拷。**反之**：Messages 即使把 1MB WAL 拷进来仍全 0 行 → 说明是真空库，不是复制姿势问题（这一条用两个源交叉验证过）。
2. **`find -type f` 在 TCC 保护目录里会挂起**（>25s 无返回），不是返回空 —— 判活要用 `ls` 限深 + 显式超时，`wc -l` 得 0 时也分不清"空目录"与"被拦"。
3. **DB 可读 ≠ 目录可遍历**：`~/Library/Containers/com.tencent.xinWeChat` 的 `ls` 正常（Data/…），但递归 `find` 立刻卡死；同理 `~/Library/Group Containers` 下 sqlite 直接 `open()` 就读得到（本机 GA 进程已具备相应权限，无需额外授权动作）。

## 四、推荐：最值得挖掘的 1 个源 —— KnowledgeC

理由：唯一同时满足 ①副本直读零授权 ②量大（17400 条，`/app/usage` 1 万条）③语义新（逐分钟 App 使用/通知/屏幕点亮 = 数字生活底稿）；
且本机 Messages/Mail/Photos/Reminders/Notes 全空，**没有替代品**。
已有关联基础：`timing2_weekly_report.py`（Timing2 300K 条 AppActivity 周报，见 `history.txt`），两者可交叉验证。
风险：**knowledgeC 默认只保留约 4 周历史**（Apple 行为），越早开始累积快照越有价值。

## 五、边界与未验证

- 微信 `db_storage/*` 只确认**路径存在**，未尝试解密读取（属密钥/合规边界，需另行授权）；`msg/` 下 attach/video/file 只列举未统计。
- sfl3 最近文档的**真实路径**未解（NSKeyedArchiver bookmark blob），只给出条目计数。
- Notes 正文未解（protobuf+gzip），结论"本机 Notes 基本没用"基于标题数=2。
- Photos "ZASSET=0" 有两种可能（库为空 / iCloud 优化），未进一步区分（需要 Photos 库 `originals/` 目录核查，本轮未做）。
- 本次全部结论来自副本快照（`db_copy/`），源库可能已变动；重跑 `python3 tools/personal_source_map.py all` 即可刷新。
