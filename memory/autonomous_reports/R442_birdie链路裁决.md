# R442 · 失效网盘链路（BirdieSearch 监控）「修复 or 退役」裁决

- 编号/日期：R442 / 2026-09-27（探测快照 2026-09-27 05:12:56 CST）
- 来源：`temp/TODO.txt` 第 17 批 C0（三路评审 8.33，本批最高分）
- 类型：分析（只读复现 + 二选一裁决；**不含任何执行动作**）
- 证据目录：`temp/birdie_triage/`（`evidence.md` 115 行 / 8822B、`raw_probes.md`、`checkhost_raw.json`、`report_status.tsv`、`repro_run.txt`）
- 提案台账：`temp/pending_decisions/DECISIONS_R442_birdie_retire.md`（D1 退役 / D2 告警接线，均 revers=0）
- 裁决已写入 SOP 尾注：`memory/birdiesearch_monitor_sop.md`（「★裁决：退役（2026-09-27 R442…）」节）

## 1. 结论

**退役（建议停用该定时任务）。** 故障 100% 在站点侧、我方**无缺陷可修**：`birdiesearch.com` 解析仍正常但 **IP 层（ICMP/TCP443/HTTP）在全球多节点全部超时**，表现为「服务器已下线 / 被网络层封禁」，客户端无论怎么改（UA、签名、超时、代理）都不可能恢复。

- 死亡窗口锁定 **2026-08-07（末次成功）→ 2026-08-08（首份失败）**。
- 产出序列：`sche_tasks/done/*_birdie_daily_monitor.md` 共 **59 份** = 成功 **7** 天（08-01..08-07）+ 失败 **52** 份 / **48** 个日期（08-08→09-25）⇒ 连续 **49 天**零有效产出（区间内仅 09-19 无报告）。
- ★口径订正：SOP 旧记「全部 58 份报告 / 自 09-13 起失败 / 09-10 最后一次跑通」均**有误**，本轮逐份核 exit 码后订正为 59 份 & 08-07 最后成功（订正已回写 SOP L50 与失败起点段）。

## 2. 证据

### 2.1 站点侧：全球不可达（绕开本机代理，第三方网络）
| 探测 | 节点数 | 全超时 | 结论 |
|---|---|---|---|
| check-host HTTP | 6 | 6 | 六国全部 Connection timed out |
| check-host TCP 443 | 6 | 6 | 六国全部 Connection timed out |
| check-host ICMP ping | 4 | 4 | 四国全部 TIMEOUT（回包地址 103.189.141.216） |

⇒ **IP 层三层全灭** ⇒ 排除 TLS/证书/签名/关键词/我方代理等一切本机因素。原始 JSON 含 rid：http=`4da17d3ak654` / tcp443=`4da18007k9d1` / ping=`4da1825akb63`（`temp/birdie_triage/checkhost_raw.json`）。

旁证（本机侧三路独立出口，均失败）：curl 经代理 `000 / 30.02s (rc=28)`；`r.jina.ai` 抓目标站 `422 / 19.31s`；allorigins `520 / 19.15s`；而同一 `r.jina.ai` 抓 `example.com` = `200 / 1.22s`（对照正常）。

DNS 仍解析（域名未弃）：DoH 查 `birdiesearch.com` 与 `www.birdiesearch.com` 均返回 `103.189.141.216`，TTL 38400。

### 2.2 我方侧：无缺陷可修
- 失败点固定：`birdie_monitor/birdie_client.py` 第 73 行（`POST /api/user/sign`，`timeout=15`）。
- 代码零改动：该文件自 `b36a80c`（2026-08-05）以来 `git log` 无任何提交 ⇒ 非代码回归。
- 受控复现一次：`rc=1` / 15.2s / `ProxyError: handshake operation timed out`；去重库 `birdie_seen_items.json` 的 sha1 与 mtime **前后完全一致**（零副作用），无残留进程。

### 2.3 其他时间线证据
- archive.org 末次 200 快照 = 2026-05-13；crt.sh 末次证书 `not_before` = 2026-07-07（机房在关站前仍续签过证书）。
- 调度侧旁证：`logs/scheduler_reflect.log` 末活动 2026-09-26 11:00:47，`done/` 末产物 09-26 10:34 ⇒ 机器睡眠导致约 18.2h 无调度，**属本机睡眠非新缺陷**。

## 3. 为什么裁决是「退役」而不是「修复」

1. 「修复」需要存在可修对象；ICMP 都不通说明**服务器不在线**，任何客户端改动都无效。
2. 域名仍解析、无到期或关站公告可查 ⇒ **我方无从得知它何时回来**；继续每天 11:00 触发只是产出注定失败的报告。
3. 保留成本为零：资产（`birdie_monitor/`、59 份历史报告、SOP）全部保留，站点复活后把配置 `enabled` 置回即可，**无需改代码**。

## 4. 代价与收益（退役）

- 省掉每天一次注定失败的 LLM 会话（49 天 × 52 份噪音报告）；停掉「看起来还在监控」的假象。
- 副作用面：退役后站点若复活不会自动恢复（需人工一句恢复），这是唯一代价。
- 未批准前**保持现状**（`sche_tasks/birdie_daily_monitor.json` 仍 `enabled=true`）。

## 5. 通用教训（本批新增判据）

- **告警缺失才是真问题**：本任务连续 49 天静默失败才被发现——机器上没有任何外部可见信号。⇒ 判据「任意监控任务**连续 ≥3 天同因失败必须一次性提醒用户**（含站点侧不可达这类非本机故障）」已写入 SOP 尾注；接线（`tools/task_freshness.py` / `reflect_scripts/09_task_freshness.py`）作 D2 待批。
- **判读禁关键词粗筛**：必须逐份核 exit 码——否定句（stdout 里「没有今日无新增命中」）会把失败误判成成功。

## 6. 边界与口径

- 全部数值取自 2026-09-27 05:12 前后快照的命令原始输出；此后新增报告不在本口径内。
- check-host 结论依赖该第三方节点网络；本机侧另用三路独立出口交叉验证，两者一致。
- 本轮**零执行动作**：未改任何定时配置、未动 `birdie_monitor/`、未清去重库、未推送任何通知。
- 报告份数/成功失败天数口径 = `temp/birdie_triage/report_status.tsv`（逐份解析 `done/` 全部 `*_birdie_daily_monitor.md` 的状态行）。

## 7. 复跑命令（只读）

```bash
# 报告口径
python3 -c "import glob;fs=sorted(glob.glob('sche_tasks/done/*_birdie_daily_monitor.md'));print(len(fs))"
# 站点复活判据（非 000 即可能复活，再走 SOP 三步定性）
curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x http://127.0.0.1:7897 https://www.birdiesearch.com/api/user/sign
# 全局不可达复验（第三方节点，需联网提交 rid 后取结果）
#   见 temp/birdie_triage/checkhost_raw.json 内的请求 URL
```

## 8. 附加发现：台账「决策」列口径坑（本轮顺手修，含证据）

**现象**：`view_pending.py` 把本报告对应的 `DECISIONS_R442_birdie_retire.md` 统计为「2 条目 / 未决 0 / 已决 2」——待批项在 `--aging`（积压天数视图）里彻底隐身。

**根因**（源码 `temp/pending_decisions/view_pending.py` L29–L38，`is_decided()`）：`决策` 列语义 = 留空 ⇒ 未决；写了待批/待审/未决/⏳/☐/⬜/`[]` 等字样 ⇒ **仍未决**；其余任何非空 ⇒ 已决。本台账把**动作描述**写进了 `决策` 列 ⇒ 被判已决。

**正解口径**（= 既有 24 份台账惯例，如 R382/R393/R404/R417/R428/R434）：动作写 `变更内容（具体动作）` 列，另设一个**留空的** `决策` 列。

**处置**：
- 修 `DECISIONS_R442_birdie_retire.md`（本会话新增）与 `DECISIONS_R429_reclaim_candidates.md`（**旧会话遗留，同一缺陷** ⇒ 其 D1–D3 三条已隐身 1 天）。
- 只做列重排/补空列，单元格文字逐字保留；两个文件均在 gitignored `temp/`，不影响仓库。
- 全量扫描 28 份台账确认仅此 2 份受影响；`DECISIONS.md` 的 2 行「已决」是真状态（批注 `[已执行 09-23]`），保留不动。

**证据**（2026-09-27 修后复跑 `python3 temp/pending_decisions/view_pending.py`）：

```
DECISIONS_R429_reclaim_candidates.md               3     3     0      0 天
DECISIONS_R442_birdie_retire.md                    2     2     0      0 天
合计 147 条 / 未决 145 条
```

修前同一命令为：`R429 3/0/3`、`R442 2/0/2`、`合计 147 条 / 未决 140 条`。

**可复用的自检**：新增台账落盘后必跑 `view_pending.py`，确认自己的条目出现在「未决明细」；「条目数对但未决=0」就是本坑的指纹。
