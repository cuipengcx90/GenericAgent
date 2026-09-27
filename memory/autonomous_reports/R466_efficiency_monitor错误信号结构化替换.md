# R466 efficiency_monitor 错误信号结构化替换（关键词计数 → 错误事件计数）

- 批次/来源：第 20 批规划·三路评审保留条（codex/槽3/槽4 = 9/9/8 = 8.67，最高分）；TODO `temp/TODO.txt` 第 177 行
- 日期：2026-09-28
- 性质：只读内核 + 生产脚本接线（新增键，保留旧键）；无不可逆动作、无推送
- 内核：`tools/error_signal.py`（615 行 / 29668 B / sha256 前 16 `cfaa913e7880bf58`）
- 接线：`temp/efficiency_monitor.py`（203 行 / 9969 B / sha256 前 16 `45d46ee2c80fde62`）

---

## 1. 问题与目标

L1 已有记载：`efficiency_monitor_daily` 的 `tail_errors_2000` 是**关键词粗信号**，分母是日志尾部 2000 行，其中含被搬运的**报告正文/源码/清单回声**；2026-09-27 实测 26 命中里仅 5 条属错误事件本体 ⇒ 该指标既漏又误，且工具本体住在 gitignored `temp/`（耐久缺口 C65/R375）。

目标：把「关键词命中数」换成「**结构化错误事件数**」——子进程 `rc≠0` + traceback 起止配对 + 已知良性白名单（LLMLingua/numpy ABI 等），并给出前后 A/B 与真事件召回率。

## 2. 交付物

| 文件 | 行/字节 | 说明 |
|---|---|---|
| `tools/error_signal.py` | 615 行 / 29668 B | 内核：`analyze_lines`(L139) / `analyze_file`(L256) / `verdict`(L269) / `predicate_probe`(L288) / `legacy_keyword_hits`(L130) / `_is_benign`(L135) / `_signature`(L83) / `TRACEBACK_START`(L104) / `LEGACY_TERMS`(L53) / `BENIGN_PATTERNS`(L56) / `_selftest`(L319) |
| `temp/efficiency_monitor.py` | 203 行 / 9969 B | 接线：`_load_kernel`(L36，导入 L41)、新增键 L91/L115、note L140、health 判定 L165–177、`error_signal`/`error_metric` 字段 L183–184 |

签名（供复跑）：`analyze_file(path, window_lines=None, log_name=None)`；`window_lines` 传 `None` = 全档，传 2000 = 尾部窗口。**窗口 0/负值须传 None**。

## 3. 验收① 同一日志窗口 A/B + 逐条对照表

证据：`temp/error_signal/ab/k2/ab_after_prefix2.json`（125 行 / 2502 B / sha256 `ae869fdbe679498f`）
口径：`legacy_keyword_hits` = 旧关键词计数（保留键 `tail_errors_2000`）；`event_count`/`strong_event_count`/`distinct_strong_event_count` = 新结构化口径。

| 语料 | 行数/窗口 | legacy | events | strong | rc | distinct | distinct_strong |
|---|---|---|---|---|---|---|---|
| `frozen/hist_20260927_0901_window2000.log`（2000 行窗） | 2000 | 26 | 5 | 1 | 4 | 2 | 1 |
| `frozen/autonomous_reflect.frozen.log`（全档） | 21627 | 342 | 41 | 31 | 10 | 31 | 28 |
| 活日志 `logs/autonomous_reflect.log`（尾 2000 行） | 26186 / [24187,26186] | 392 | 4 | 4 | 0 | 4 | 4 |
| 同上（全档） | 26186 | 1184 | 49 | 39 | 10 | 39 | 36 |
| `logs/ga_api_server.log`（尾 5000 行） | 730742 | 5000 | 5000 | 5000 | 0 | **1** | **1** |
| `logs/dingtalk_agent.log`（尾 2000 行） | 7451 / [5452,7451] | 112 | **0** | **0** | 0 | 0 | 0 |

★ 第 5 行是刷屏去重（`_signature`，L83）的直接证据：legacy 与事件数同为 5000（该日志尾部全是同一句 `level_error`），但 **distinct=1** ⇒ 不会被当成 5000 个不同故障。
★ 第 6 行是良性噪音的极端例：112 命中、0 事件、`benign_count=28`。

### 逐条对照（冻结窗 26 条命中，脚本生成）

分类口径：命中行落在事件块内 ⇒ `EVENT(事件块)`；`_is_benign` 为真 ⇒ `BENIGN`；否则 ⇒ `ECHO/引用`。
本次实测分类：**EVENT 2 / BENIGN 0 / ECHO 24** ⇒ 26 条里 24 条（92.3%）非事件本体，与 TODO 中「26 命中仅 5 条属事件本体」同向（差异=口径：本次按「命中行是否落在事件块内」判，5 是事件条数而非命中行数）。

| # | 行号 | 判定 | 原文(截断) |
|---|---|---|---|
| 1 | 35 | EVENT(事件块) | Traceback (most recent call last): |
| 2 | 39 | EVENT(事件块) | AttributeError: module 'bg_task' has no attribute 'start' |
| 3 | 374 | ECHO/引用 | error: pathspec 'control-plane/objection_v0/objection.py' did not match any file(s) known to git |
| 4 | 375 | ECHO/引用 | error: pathspec 'control-plane/objection_v0/test_objection.py' did not match any file(s) known to git |
| 5 | 376 | ECHO/引用 | error: pathspec 'docs/product/F1_OBJECTION_CHAIN_SPEC.md' did not match any file(s) known to git |
| 6 | 535 | ECHO/引用 | R3 \| 2026-04-23 \| 产出 \| 盘点memory/14个SOP，发现2处冲突(scheduled格式/procmem路径)、4个缺失SOP(keychain/notify/error_recovery/ll |
| 7 | 546 | ECHO/引用 | NotFoundError)。真音频端到端(say 合成真语音+已知真值)：中文长句 tiny=繁体+3错 / base=繁体+2错 / **small=0错**(2.4–3.8s/15s)，英文 tiny 即 0 错； |
| 8 | 565 | ECHO/引用 | R442 \| 2026-09-27 \| 分析 \| 失效网盘链路（BirdieSearch 监控）「修复 or 退役」裁决 \| 只读复现后裁决=**退役（建议停用调度，动作待批）**：故障 100% 站点侧——DoH 仍解 |
| 9 | 593 | ECHO/引用 | R442 \| 2026-09-27 \| 分析 \| 失效网盘链路（BirdieSearch 监控）「修复 or 退役」裁决 \| 只读复现后裁决=**退役（建议停用调度，动作待批）**：故障 100% 站点侧——DoH 仍解 |
| 10 | 628 | ECHO/引用 | > R442 \| 2026-09-27 \| 分析 \| 失效网盘链路（BirdieSearch 监控）「修复 or 退役」裁决 \| 只读复现后裁决=**退役（建议停用调度，动作待批）**：故障 100% 站点侧——DoH  |
| 11 | 893 | ECHO/引用 | errors = [] |
| 12 | 897 | ECHO/引用 | return "[ERROR] historyline 必须是单行，不能包含换行符" |
| 13 | 901 | ECHO/引用 | return f"[ERROR] 报告文件不存在: {report_path}" |
| 14 | 915 | ECHO/引用 | except Exception as e: |
| 15 | 916 | ECHO/引用 | return f"[ERROR] 移动报告失败: {e}" |
| 16 | 933 | ECHO/引用 | except Exception as e: |
| 17 | 939 | ECHO/引用 | return f"[ERROR] 写入 history 失败: {e}（报告已回滚）" |
| 18 | 989 | ECHO/引用 | 111:class Reject(Exception): |
| 19 | 1030 | ECHO/引用 | 223:def test_nc_missing_by_flag_is_usage_error(tmp_path): |
| 20 | 1042 | ECHO/引用 | 333:def test_nc_gate_bad_revision_usage_error(tmp_path): |
| 21 | 1083 | ECHO/引用 | `LEDGER_INVALID` / `BLOCKED` / `ALLOW` / `ERROR` |
| 22 | 1359 | ECHO/引用 | [x] 环境 \| 【第17批·三路评审 9/9/7=8.33（并列最高）】失效网盘监控链路「修复 or 明确退役」二选一判定（birdie 49 天静默失败） \| 背景：birdiesearch 自 2026-08-07 |
| 23 | 1376 | ECHO/引用 | (0, '61:def test_submit_creates_submitted_object(tmp_path):\n74:def test_submit_assigns_monotonic_ids(tmp_path |
| 24 | 1429 | ECHO/引用 | (0, 'L4_raw_sessions\n__pycache__\nacceptance_taskset_sop.md\nadb_ui.py\nagent_fleet_sop.md\nandroid_uu_cellul |
| 25 | 1521 | ECHO/引用 | - [x] 能力 \| 本地语音转写(ASR)能力缺口实测 \| 2026-09-22 完成，R339：**前提错位**——`whisper_utils.py`(176行, faster-whisper 1.2.1) 早已存 |
| 26 | 1553 | ECHO/引用 | [x] 环境 \| 【第17批·三路评审 9/9/7=8.33（并列最高）】失效网盘监控链路「修复 or 明确退役」二选一判定（birdie 49 天静默失败） \| 背景：birdiesearch 自 2026-08-07 |

## 4. 验收② 真事件全部召回（≥5 条）

证据：`temp/error_signal/ab/k2/coverage_after_prefix.json`（60 行 / 1363 B / sha256 `012a56ea363cbd98`）

以「正则规定的 traceback 头行」为 ground truth（独立于事件块构造），冻结全档 `heads_total=29`、`heads_covered=29`、**`heads_uncovered=0`**（`uncovered_lines=[]`）；同档 `events=41`、`traceback_events=29` ⇒ 29 个头**逐一进块**，无漏检。窗口档（2000 行）同法实测 1/1 覆盖。

## 5. 验收③ 已知良性噪音 0 命中

- dingtalk 窗口：legacy 112 → events **0**，`benign_count=28`（§3 表内已列）。
- 内核 `BENIGN_PATTERNS`(L56)+`_is_benign`(L135)：LLMLingua / numpy ABI（`numpy.core.multiarray failed to import`、`NumPy 2`）等形态判良性，不计入 strong。
- 接线脚本保留旧逻辑（L150）作为 `legacy_fallback` 时的 numpy 检测，不参与 structured 计数。

## 6. 验收④ 负控：关键词塞进正文不得计数

两处独立证据：
1. **活日志负控**（真实世界）：`legacy=392` vs `structured_events_2000=4`（§3 表），多出的 388 条正是被搬运的报告正文/源码/清单回声。
2. **selftest 内 NC 系列**（30 个标签、39 项断言）：NC1–NC10、NC11a–d、NC12–NC20、NC21/NC21b、NC22，覆盖「良性形态不计」「正文回声不计」「同问题去重」「非白名单前缀不放行」。

## 7. 验收⑤ `--selftest` 含负控

```
$ python3 tools/error_signal.py --selftest
[error_signal --selftest] checks=39 failed=0      # rc=0
```

## 8. 验收⑥ 耐久落点

内核住 **`tools/error_signal.py`**（仓内、随克隆可得、自带 selftest），满足「落点明确耐久化」；生产脚本 `temp/efficiency_monitor.py` 本体仍在 gitignored `temp/`（= R375/C65 已知耐久缺口，本次不扩面，见 §11）。

---

## 9. 内核在实测中修掉的三处真缺陷

| 缺陷 | 现象 | 修法 | 证据 |
|---|---|---|---|
| 良性判定只作用于单行 | 多行块内某行是良性、其余行照计 ⇒ 假事件 | 块级良性判定（整块命中 `_BENIGN_RE`(L65) 才豁免） | selftest NC 系列 |
| 事件不去重 | 同一问题刷屏 ⇒ 事件数=行数（ga_api 5000/5000） | `_signature`(L83) 归一去时间戳/数字后按签名去重 ⇒ distinct 单列 | ga_api 窗口 distinct=1 |
| traceback 头只认行首 `Traceback` | 带前缀写法（`err:`/`err `/`ERROR: `）漏检 | `TRACEBACK_START`(L104) 改**正则内联大小写不敏感白名单**（含 `ERROR`），并保留 `prefixed_any`/`unmatched` 观测位 | selftest NC21/NC21b/NC22 |

### 跨日志前缀边界实测（11 份 `logs/*.log`）

证据：`temp/error_signal/ab/prefix_coverage_crosslog.json`（68 行 / 1351 B / sha256 `03b94558454b9498`）

- 监控目标日志 `logs/autonomous_reflect.log`：whitelisted 36、prefixed_any 4、**unmatched 0** ⇒ 带前缀写法 4 处全部被新白名单捕获（扩锚点的直接收益）。
- 全量表里 `prefixed_any` 非零的还有 `feishu_agent.log`(14，unmatched 6)、`launch_gui.log`(3，unmatched 0)、`scheduler_reflect.log`(151，unmatched 139)。
- 未匹配 145 = 6（feishu，数字回声）+ 139（scheduler，`pysqlcipher3:`/`dbapi2:`/`apsw:` 等**模块名前缀**）；两者都在**非监控日志**内，且属「模块名 + Traceback」的回声形态 ⇒ 不纳入白名单是**刻意的**（否则任何「词 + Traceback」的散文都会被算作事件，NC22 即此负控）。
- 边界已写进源码注释（L104 附近），避免他人日后误扩白名单。

## 10. 生产接线与真跑

接线口径（`temp/efficiency_monitor.py`）：
- 旧键 `tail_errors_2000` **保留**（趋势可比/向后兼容），但**不再决定 health**（L19 注释）；
- 新增 `structured_events_2000` / `structured_strong_2000` / `structured_rc_2000` / `structured_benign_2000` / `structured_possible_2000` / `structured_distinct_2000` / `structured_distinct_strong_2000` / `structured_kinds_2000`；
- `error_metric` 字段明示 `distinct_strong`（L184），health 走结构化口径（L165–177）；
- 内核不可用时回落 `legacy_fallback`（L144），不静默假绿。

真跑（cwd=repo 根，只生成报告、**不推送**），产物 `temp/error_signal/ab/k2/prod_live_report_after.json`（28 行 / 739 B / sha256 `070eaf04a3ecd3ba`）关键字段：

```json
{"date":"2026-09-28","mode":"lightweight_direct_parse","error_signal":"structured_v1",
 "error_metric":"distinct_strong","log_lines":26303,
 "tail_errors_2000":405,"structured_events_2000":4,"structured_strong_2000":4,
 "structured_rc_2000":0,"structured_benign_2000":0,"structured_possible_2000":411,
 "structured_distinct_2000":4,"structured_distinct_strong_2000":4,
 "structured_kinds_2000":{"traceback":4},
 "health":"OK","issues":[],
 "notes":["tail_errors_2000=405 中多为回声/散文引用(同窗口真事件 4 条), 勿当故障计数"]}
```

★ 与 2026-09-27 的实测（26 命中 / 5 事件）相比，本次 405 命中 / 4 事件：**405 与 4 同屏出现**即是替身价值——读者不会再拿 405 当故障数，且 note 会主动提示。

## 11. 边界与残余（不做/未做）

1. **生产脚本本体仍在 `temp/`**（R375/C65 耐久缺口）：本次只把**内核**耐久化；把 monitor 本体搬进 `tools/` 会牵动定时任务 cwd 语义（weekly_report_reminder 类教训），属独立议题，不夹带。
2. **白名单只覆盖「行首或限定前缀」形态**：模块名前缀（`apsw:` 等）刻意不放行（145 处实测全在非监控日志）。若将来监控日志出现该类形态，须先有真实样本再评估，勿凭想象扩。
3. **刷屏去重是「同签名折叠」**：ga_api 尾部 5000 行同句 ⇒ distinct=1 属预期；但**真·5000 个不同异常**会照计 5000（签名含归一后全文，不会误折叠）。
4. **未新增调度**：本批只做「判定内核 + 生产脚本接线」，沿用既有 `efficiency_monitor_daily` 通道。

## 12. 复跑命令

```bash
cd ~/projects/GenericAgent
python3 tools/error_signal.py --selftest                     # checks=39 failed=0
python3 tools/error_signal.py temp/error_signal/frozen/hist_20260927_0901_window2000.log --window 2000
python3 -c "import json;print(json.load(open('temp/error_signal/ab/k2/ab_after_prefix2.json'))['frozen_hist_window2000'])"
```

## 13. 产物清单（sha256 前 16 / 字节）

| 文件 | 字节 | sha256 |
|---|---|---|
| `tools/error_signal.py` | 29668 | `cfaa913e7880bf58` |
| `temp/efficiency_monitor.py` | 9969 | `45d46ee2c80fde62` |
| `temp/error_signal/ab/k2/ab_after_prefix2.json` | 2502 | `ae869fdbe679498f` |
| `temp/error_signal/ab/k2/coverage_after_prefix.json` | 1363 | `012a56ea363cbd98` |
| `temp/error_signal/ab/k2/prod_live_report_after.json` | 739 | `070eaf04a3ecd3ba` |
| `temp/error_signal/ab/prefix_coverage_crosslog.json` | 1351 | `03b94558454b9498` |
| `temp/error_signal/ab/k2/legacy26_table.md` | 脚本生成 | — |
| `temp/error_signal/frozen/hist_20260927_0901_window2000.log` | 208388 | `6a3ce91c6ca1c8f9` |
| `temp/error_signal/frozen/autonomous_reflect.frozen.log` | 1607823 | `21338e6e8761df4e` |

## 14. 约束遵守声明

- 全程只读源日志（冻结副本用于 A/B，未改动任何原始日志）；未推送任何通道（真跑显式不通知）。
- 报告内所有数值均为当场命令原始输出回粘；`file:line` 行号取自 `grep -n` 实读。
- 交付边界：本报告只声明 §3–§10 所列断言；§11 明确列出未做项。
