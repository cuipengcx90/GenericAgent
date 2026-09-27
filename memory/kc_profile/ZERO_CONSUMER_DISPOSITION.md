# 零消费者流处置登记（kc）

- 由来：TODO#167（第18批，三路评审 5.33）/ R436 对账报出「有料但零消费者」2 条，全部落在 kc。
- 口径：本表只登记 **kc** 流；Chrome 面靠 `KNOWN_CONSUMED` 负控（3 条已消费源）覆盖。
- 机器可读同源：`tools/source_consumer_census.py` 的 `ZERO_CONSUMER_DISPOSITION`（单一源=代码常量，本文件是它的人类可读版；改判定须同时改常量，否则 `--selftest` T14/T15 与 `--verify-json` 会报）。
- 快照绑定：证据取自 `temp/kc_snapshots/2026-09-25/knowledgeC.db`（kc 有迟写回填，数值只对该快照时点成立）。
- 时区口径：house 约定 `datetime.fromtimestamp(ts + 978307200)` = **本地时间**（与本机既有消费者 `tools/kc_app_usage.py:68`、`memory/kc_profile/profile.py` 一致）；下表所有时刻均为本地时间。

## 登记项

### 1. `/discoverability/signals` — **DEPRECATED（弃用）**

| 项 | 实测值 |
|---|---|
| 行数 | 6 |
| 取值字段 | `ZVALUESTRING`（= `ZVALUECLASS` 对应的字符串列） |
| 取值多样性 | **1** —— 6 行全为 `com.apple.spotlight.invoked` |
| 时间跨度 | 2024-12-27 08:55:27 → 2025-04-14 22:03:51 |
| 单条时长 | 全部 0 s（`ZENDDATE == ZSTARTDATE`） |
| 距今（对 2026-09-25 快照） | 末条 **529 天** 前 |

判定理由：该流在本机已停写（且 kc 滚动删除≈4 周，这条 17 个月前的残留能存在说明此后确无新行）；6 行同值、无时长维度，**不足以支撑任何结论**，也无从证明消费者。R371/R392 已把它记为「长尾·不裁剪」。

- 复活判据：出现 ≥1 行新事件（末条距当次快照 < 90 天）**或** `distinct_values ≥ 2` ⇒ 撤销本登记、重评消费者。
- 判定日期：2026-09-27。

### 2. `/app/mediaUsage` — **DEFERRED（保留·暂不消费）**

| 项 | 实测值 |
|---|---|
| 行数 | 7 |
| 取值字段 | `ZVALUESTRING`（= 发起媒体使用的 app bundle id） |
| 取值多样性 | **3** —— `com.alibaba.DingTalkMac`×3 / `wxplayer`×3 / `com.apple.helpviewer`×1 |
| 时间跨度 | 2026-09-10 17:53:28 → 2026-09-22 18:41:23（13 天） |
| 单条时长 | max 36.00 s / min 0.00 s / n=7（全为秒级片段） |

7 行原始行（值 | 起 | 止）：DingTalkMac 09-10 17:53:28→17:53:43（15s）、09-10 17:53:46→17:53:46（0s）、09-10 17:53:50→17:54:09（19s）、helpviewer 09-13 09:24:23→09:24:25（2s）、wxplayer 09-14 17:40:43→17:41:00（17s）、09-14 17:43:45→17:44:21（36s）、09-22 18:41:15→18:41:23（8s）。

判定理由：量级（7 行 / 13 天 / 3 app / 秒级时长）**不足以支撑派生指标**（媒体播放时长、媒体 app 画像都会是伪精确）；但它是唯一带「媒体使用」语义的 kc 流，**显式保留通道、暂不建消费者**，避免日后需要时误判为「已弃用」。

- 立项判据：单日 ≥ 50 行 **或** 30 天累计 ≥ 200 行 ⇒ 立项消费者（届时 status 改并附指标定义）。
- 判定日期：2026-09-27。

## 闸门（三条判据，非空即 rc=2）

`guard_disposition()` 三查：①`ZERO_CONSUMER_UNDISPOSED`（有料零消费者未登记）②`CLAIM_WITHOUT_EVIDENCE`（声称 CONSUMED 但消费者列表为空）③`DISPOSITION_STALE`（登记项源消失或已被真消费者接管）。

## 复跑命令（只读）

```bash
# ① 自测（含 11 类负控：未登记/伪改/源消失/登记过期/探针失效…）
python3 tools/source_consumer_census.py --selftest          # 期望 31 passed, 0 failed

# ② 真跑对账 + 处置闸门（只读；加 --readonly-proof 附源侧 sha256 前后一致）
python3 tools/source_consumer_census.py --readonly-proof
#    期望末段： [处置闸门] 零消费者流（TODO#167）登记面 kc 侧 2 条：/discoverability/signals=DEPRECATED, /app/mediaUsage=DEFERRED
#              [处置闸门] 无违规（未登记零消费者 / 无证据声称 / 登记过期 三类均为 0）

# ③ 复核已归档结论（不重新采集；落盘 JSON 的字段级 fail-closed）
python3 tools/source_consumer_census.py --verify-json temp/source_consumer_census/census_2026-09-27.json
```

## 负控记录（2026-09-27 实测）

把归档 JSON 副本里 `kc:/app/mediaUsage` 的 `verdict` 由 `REF_ONLY` 伪改为 `CONSUMED`（`consumers` 仍为空）：

```
$ python3 tools/source_consumer_census.py --verify-json /tmp/census_forged.json ; echo rc=$?
[verify-json] /tmp/census_forged.json（sources=16 条，生成于 2026-09-27T01:21:39）
  VIOLATION CLAIM_WITHOUT_EVIDENCE kc:/app/mediaUsage: 判为 CONSUMED 但消费者列表为空
  VIOLATION DISPOSITION_STALE kc:/app/mediaUsage: 登记为 DEFERRED 但现已 CONSUMED ⇒ 须撤销登记
rc=2
```

同一条伪改在纯函数层由 `--selftest` 的 `NC11` 覆盖；原 JSON（未改）复核 rc=0。

## 边界（不作大结论）

- 本表只回答「这两条 kc 流该不该管、怎么管」，**不**替代 R436 的全源对账（Chrome 10 profile + kc 6 流的判定仍在 `tools/source_consumer_census.py` 真跑输出里）。
- 未接线定时：本闸门目前靠人工/会话复跑，未并入任何 launchd/sche_tasks（与 R436 一致，接线须另行批准）。
