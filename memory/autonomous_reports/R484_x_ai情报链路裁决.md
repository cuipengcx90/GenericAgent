# R484 x.com 情报链路（x_ai_intel_daily）「修复 or 退役」裁决

- 编号：R484 ｜ 日期：2026-09-28 ｜ 批次：第22批 C7（TODO L191，三路评审 9/7/7=7.67）
- 类型：**只读判定 + 提案**（不改 SOP、不改调度、不改判据表；退役动作见台账 D1 待批）
- 新工具：`tools/x_ai_intel_audit.py`（803 行 / 36473B / sha256 前 16 = `5b761b9d4a19ee34`；`--selftest` **22 passed, 0 failed**）
- 台账：`temp/pending_decisions/DECISIONS_R484_x_ai_intel_retire.md`（D1 停用调度 = 推荐；D2 监控面修复；D3 接线，均 revers=0）
- 证据：`temp/r484/{census,attribution,verdict,proof}.json`

## 1. 结论（先说结论）

1. **裁决 = 退役（建议停用调度）**：`root_cause.class = EXTERNAL_PRECONDITION_MISSING`，`recommendation = RETIRE_OR_DISABLE_SCHEDULE`。故障归属外部前置（浏览器里没有已登录 x.com 的会话），**我方脚本无缺陷可修**——脚本含成功标记、曾在 2026-08-17 真实成功过一次，此后 40 份回执全部落在前置检查/标签失效两类形态上。
2. **量化**：40 份 done 回执（2026-08-17_1638 → 2026-09-28_0904）中成功 **0**、失败 **40**、不确定 0；首个失败时点 `2026-08-17 17:02:59`，末个 `2026-09-28 09:05:27`，跨度 **41.67 天**；脚本自身产物（`temp/x_ai_intel/reports/` + 去重库）冻结 **42.22 天**。
4. **现场复现（本次新增，见 §3.1）**：21:2x 只读复现到形态② 的**存活实例**——x.com 标签在册、`set_session` 成功，但 JS 通道对该标签恒失败（同批 linux.do 标签成功）；且把该标签设为活动标签亦不恢复 ⇒ 「退役」结论**不依赖**历史回执单方证据。
3. **方法学订正（本报告最重要的一条）**：SOP 与实际最常引用的「严格标记 `Saved N new posts` 命中 0 ⇒ 失败」是**弱代理**——该字样只由脚本 `print` 到 stdout（`/tmp/x_ai_intel.log`），**从不写进 done 回执**。因此「回执里没有严格标记」不能当作失败证据；权威证据是**脚本自身产物**（reports 目录新增 + 去重库 `collected_at` 推进）。本报告全部结论都建立在产物侧。

## 2. 机械复跑（原始输出）

```
$ python3 tools/x_ai_intel_audit.py census --root . --json temp/r484/census.json
receipts=40 success=0 failure=40 unclear=0
window: 2026-08-17 17:02:59 → 2026-09-28 09:05:27
rule_compare: {"strict_marker_hits": 0, "loose_rule_hits": 2, "classifier_success": 0, "loose_false_positives": ["2026-08-18_0903_x_ai_intel_daily.md", "2026-09-16_0904_x_ai_intel_daily.md"]}
failure_forms: {"NO_TAB": 38, "CDP": 2, "OTHER": 0}
last_artifact_write=2026-08-17 16:04:00 (frozen 42.22 d)
artifact reports=5 newest=20260817_160400.md db={"bytes": 12288, "posts": 3, "collected_at": ["2026-08-17T08:03:49.673978+00:00"], "handles": ["OpenAI"]}
```

```
$ python3 tools/x_ai_intel_audit.py attribution --root . --json temp/r484/attribution.json
script_present                   {"status": "OK", "evidence": ".../temp/x_ai_intel/x_intel.py"}
success_marker_in_source         {"status": "OK", "evidence": "源码含 print(... 'Saved {len} new posts to {report_path}')"}
run_done_in_source               {"status": "ABSENT", "evidence": "脚本**不**包含 RUN_DONE"}
fail_fast_line                   {"status": "OK", "line": 158, "text": "raise RuntimeError(\"No active X browser tab was found. Open x.com in the connected browser and sign in.\")"}
prompt_waits_run_done            {"status": "MISMATCH", "evidence": "prompt 要求轮询日志直到出现 RUN_DONE"}
accounts                         {"status": "OK", "count": 6, "handles": ["OpenAI", "AnthropicAI", "GoogleDeepMind", "huggingface", "sama", "karpathy"]}
script_mtime_vs_first_failure    {"status": "OK", "script_mtime": "2026-08-17 16:40:07", "first_failure": "2026-08-17 17:02:59", "script_edited_after_first_failure": false}
criteria_note                    {"status": "CLAIM_CONFLICT", "soft": null, "claims_reports_dir_deprecated": true, "reports_is_script_output_dir": true}
consumers(total=244) monitor-side=['reflect_scripts/09_task_freshness.py', 'tools/alert_event_id.py', 'tools/decisions_premise_recheck.py', 'tools/detector_replay.py', 'tools/freshness_criteria.json', 'tools/freshness_regression_cases.json', 'tools/preflight_cases.json', 'tools/task_freshness.py']
independent_browser_evidence=['2026-09-22_0902_x_ai_intel_daily.md', '2026-09-26_0903_x_ai_intel_daily.md', '2026-09-27_0904_x_ai_intel_daily.md']
```

```
$ python3 tools/x_ai_intel_audit.py verdict --root .
root_cause.class       = EXTERNAL_PRECONDITION_MISSING
recommendation         = RETIRE_OR_DISABLE_SCHEDULE
fixable_without_user.collection_side = []      # 采集面修复需要用户提供已登录标签（不得代开）
fixable_without_user.monitoring_side = ["判据表 note 失实", "prompt 等 RUN_DONE（脚本从不打印）"]
```

```
$ python3 tools/x_ai_intel_audit.py proof --root .
{"files": 51, "changed": [], "ok": true}      # 只读自证：运行前后 51 个源文件 sha256 一致
```

### 2.1 两处计数口径（防误读）

- **失败形态 CDP=2 而非 3**：正文含「`No tab with given id`」的回执实为 **3 份**（`2026-08-18_0903` / `2026-09-16_0904` / `2026-09-27_0904`），但 `2026-09-27` 那份**同时**含「No active X browser tab」⇒ 分类器按优先级归入 `NO_TAB`。故 `NO_TAB 38 + CDP 2 = 40`。
- **宽松规则的 2 个误判**：规则「含 new posts 且不含 RuntimeError」会把 `2026-08-18_0903` 与 `2026-09-16_0904` 判成成功（这两份正文实际写的是「未出现 `RUN_DONE` 或 `Saved N new posts` 成功标记」这类**否定句**）⇒ 该规则不可用作判据。

## 3. 根因归属（可复现证据）

| 主张 | 证据 |
|------|------|
| 失败不是「脚本坏了」 | 脚本 mtime `2026-08-17 16:40:07` **早于**首个失败 `17:02:59`；此后脚本零改动（`attribution.script_mtime_vs_first_failure = OK`）。成功标记与去重/推送逻辑都在源码里（`success_marker_in_source = OK`），且 16:03:49 真实成功过一次 |
| 失败是「外部前置缺失」 | 40/40 失败只落在两形态：①`raise RuntimeError(\"No active X browser tab …\")`（`x_intel.py:158`，缺 x.com 会话，占 38）②`CDP fallback failed: No tab with given id`（匹配到 x.com 但标签 id 已失效，占 2）；均为标签页维度，非数据/解析/推送层 |
| 不是「标签不存在」而是「没有已登录 x.com 的标签」 | 3 份回执带有独立浏览器取证（09-22 / 09-26 / 09-27）：Chrome 里有标签但**没有 x.com**（如 09-27 只有 `example.com`）。与 SOP 记的 OneTab 扩展页坑同因同类：驱动会话恒 0 |
| 调度侧无缺陷（由回执自身自证） | 40 份回执按日连续落盘（08-17 → 09-28，含 `2026-09-28_0904`）⇒ 调度每日确实触发过、任务每日确实被唤起；失败点在脚本内部前置检查（`x_intel.py:158` 抛错），不是调度器跳过或未触发。★注：本报告**不**引用 `logs/*` 中的 SKIP/触发行计数作为证据——现场 grep 得 `x_ai` 874 行 / 含 SKIP 19 行（`logs/scheduler_reflect.log`，09-28 20:12 快照），与更早会话记下的量级不一致，属易漂移的旁证，故弃用 |

### 3.1 本会话现场复现（2026-09-28 21:20–21:40，全程只读）

起点：`tools/preflight_check.py probe --target x_ai_intel_daily` 报 **PASS**（前置「浏览器有 x.com 标签 + cookie」成立）——但通道仍不可用，构成「前置可用 ≠ 产出正常」（R475 铁律）的第二个实例。

| # | 探针（只读） | 原始读数 |
|---|------|------|
| E1 | `TMWebDriver()._remote_cmd({"cmd":"get_all_sessions"})` | 7 个 `ext_ws` 会话，含 **x.com `2109120731`**；全部 `connected_at=1790584483`（= 2026-09-28 16:34:43） |
| E2 | `set_session("x.com")` | **成功** ⇒ 脚本 `x_intel.py:158` 的 fail-fast 点可越过（会话在册） |
| E3 | 逐标签 `execute_js("return document.title", session_id=…)` ×7 | **2 成功**（`2109120716` linux.do / `2109120727` github·painhardcore/pstack）；**5 失败**，均报 `CDP fallback failed: No tab with given id <id>`（含 x.com `2109120731`、Google `2109120720`）⇒ **通道故障按标签发生，非全局** |
| E4 | AppleScript 独立来源（`get URL of tabs of windows` + `get title of tab N`） | x.com 标签**真实存在**（window 1 / tab 5）；OneTab 扩展页在 window 1 / tab 1；x.com 标签 **title 为空** = 未渲染/懒加载 |
| E5 | 判别实验：`set active tab index of window 1 to 5` + `activate` → 等 4s → 复测 E3 → 复原活动索引为 **7** | 复测仍 **2 OK / 5 FAIL**，x.com 标签 title **仍为空** ⇒ **「激活标签」不恢复通道** |

⇒ 三点结论加强：①形态②（`No tab with given id`）在本机**可现场复现**，不是历史回执的孤证；②前置探针 PASS 只说明「标签在册」，**不说明该标签的 JS 通道可用**；③修「采集面」需要的是「让 x.com 标签的内容脚本真正接入」，这既非脚本可自控、也非一次激活可解（E5）——故判「退役/等前置」而非「改脚本」。

## 4. 为什么是「退役」而不是「修复」

- 采集面**没有**可自主修复的缺陷：脚本按设计 fail-fast（缺会话即抛错、零副作用），要成功只需**用户**在浏览器里有一个已登录 x.com 的普通标签——SOP 明令代理不得代开/代登录（本次亦未做任何浏览器侧操作）。
- 该前置自 2026-08-17 起连续 41.67 天未满足，且 40 份回执零成功；继续每日调度只产生噪音回执与「已完成」假象。
- 与 birdie（R442）同范式：先证故障归属，再二选一。区别在于 birdie 是站点侧不可达（世界范围），本链路是**本机前置（用户标签）缺失**——因此复活成本极低（一句命令可验，见台账）。

## 5. 顺带发现（三处文书/实现不一致，均只登记不改）

1. **判据表 note 失实**（`attribution.criteria_note = CLAIM_CONFLICT`）：`tools/freshness_criteria.json` 的 `x_ai_intel_daily` 条目写「`temp/x_ai_intel/reports` 为 08-17 前的旧目录，**已弃用故不设判据**」；而源码 `DEFAULT_REPORTS` 就是该目录，它只是**因无成功而冻结**，并非弃用。台账 D2①。
2. **prompt 等一个不存在的字符串**（`prompt_waits_run_done = MISMATCH`）：`sche_tasks/x_ai_intel_daily.json` 要求「轮询 `/tmp/x_ai_intel.log` 直到出现 RUN_DONE 或异常堆栈」，而脚本源码**不含** `RUN_DONE`（`run_done_in_source = ABSENT`）。台账 D2②（与 R375/R469 同族，可合并）。
3. **SOP 的份数计数会逐日漂移**：本报告口径 = 现场 `ls sche_tasks/done/*_x_ai_intel_daily.md | wc -l` = **40**（2026-09-28 21:16 前后）；SOP 现文写 39（09-28 上午快照）。凡引用该数须标时点。
4. **检索面**：本任务的静态消费者共 244 处（monitor 侧 8 个文件已在 §2 列出）⇒ 改判据/prompt 前先按该清单回归。

## 6. 边界与未做

- 全程只读：**未**改 `sche_tasks/x_ai_intel_daily.json`（`enabled` 仍 true、prompt 未动）、**未**改 `tools/freshness_criteria.json`、**未**改 SOP 正文（仅在文末追加裁决尾注）。浏览器侧仅做**只读探针 + 一次可逆判别实验**：①`set_session` / `execute_js` 只读取值（不导航、不点击、不输入、不提交）；②「激活标签」实验一次（`active tab index` 设为 5 → 复测 → **复原为原值 7**）。**未**导航、**未**改动任何配置、**未**代开或代登录任何标签页。
- 未做（超出本批验收，属后续批次）：把审计器接进反射器巡检（台账 D3）、eia_weekly 同类判定（L191 原文另半，仍 `[x]` 由 R361 覆盖）。
- 数值均为 2026-09-28 快照；回执份数/冻结天数逐日增长，复跑以现场为准。

## 7. 复跑

```bash
python3 tools/x_ai_intel_audit.py selftest    # 22 passed, 0 failed（含 5 类负控）
python3 tools/x_ai_intel_audit.py census      --root .
python3 tools/x_ai_intel_audit.py attribution --root .
python3 tools/x_ai_intel_audit.py verdict     --root .
python3 tools/x_ai_intel_audit.py proof       --root .     # 只读自证
python3 temp/r484/driver_dump.py                          # 会话表（get_all_sessions），只读
python3 temp/r484/js_matrix.py                            # 逐标签 JS 通道矩阵（OK/FAIL），只读
```

## 8. 交付物

| 类型 | 路径 |
|------|------|
| 工具 | `tools/x_ai_intel_audit.py`（census / attribution / verdict / proof / selftest） |
| 报告 | `memory/autonomous_reports/R484_x_ai情报链路裁决.md`（本文件） |
| 台账 | `temp/pending_decisions/DECISIONS_R484_x_ai_intel_retire.md` |
| 证据 | `temp/r484/{census,attribution,verdict,proof}.json` |
| 记忆 | L1 追加 R484 行；`memory/x_ai_intel_daily_sop.md` 文末追加「裁决与复活判据」节 |
| TODO | `temp/TODO.txt` L191 关项 |
