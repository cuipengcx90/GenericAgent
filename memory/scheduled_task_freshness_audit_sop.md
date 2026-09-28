# 定时/反射任务「产物级」新鲜度审计 SOP

来源：R350（2026-09-22 实测，只读，零配置改动）。触发词：静默失败 / 僵尸任务 / 产物过期 / 归档覆盖率。
核心判据：**任务是否"真在产出"，看产物文件，不看它有没有跑、有没有写"已完成"，也不看推送文案。**

## 方法（全只读）
1. 任务清单真源 = `sche_tasks/*.json`（gitignore 但仍被 git 跟踪过的会残留；`sche_tasks/disabled/` 为归档区，`ls sche_tasks` 查启用项）。
2. 从 `done/` 报告里抽**任务自己声明的产物路径** → 对每个产物实测 mtime/大小/尾部抽样，算 age 天数。勿凭任务名猜路径（命名漂移普遍：`weekly_report_reminder` 真产物是 `temp/content.json`，遗留孤儿 `temp/weekly_content.json` 会误导）。
3. 失败件 `*.failed.txt` 二分类：**同日另有成功件 = 质量重试丢弃稿（非故障）**；**孤儿（fail 无同日成功）= 真漏发**。
4. 归档覆盖率判据用 `build_output_archive.parse_name`，别用文件名猜。
5. **快照/diff 类任务专用判据**（2026-09-23 实测补充）：这类任务"无变化"是正常态，不能只看最近一次 diff 就判健康 —— 要 **全量相邻两两对比** 回答"它这辈子触发过告警吗"，并 **同时查被监控目标的 mtime** 区分「真不变」与「探针读空/读错」。两者都指向"目标已冻结"时，结论是**监控目标已死的容量问题**（非静默失败：任务在跑、报告在产、逻辑正确），应建议退役/换活跃目标，不要报故障。
6. **前置条件层（L0，R475 实测补充）**：把「能不能跑」与「有没有产出」**分开判** —— 前置不成立时任务必不会产出，而 run 级/mtime 证据可能照样新鲜。`tools/preflight_check.py`（只读内核，登记面单一源 = 判据表 `tools/freshness_criteria.json` 的 `preflight` 段，6 目标/10 探针，`--selftest 34/34`）：`http_endpoint`（站外可达）/`chrome_tabs`（有无已登录目标站点的标签）/`cookie_present`（cookie 存在性，**只判存在绝不读值**）/`credential_entry`（凭证登记表命中）/`path_exists`（脚本/清单齐备）。`tools/task_freshness.py` 已接该层：前置 FAIL ⇒ 目标 verdict 升 **ALERT**（只加严），OK/WARN 只带 `preflight` 段、不改既有 verdict；`--no-preflight` 可关。**铁律「前置可用 ≠ 产出正常」**（实测 `linuxdo_ai_daily_summary` 前置探针 PASS 而任务仍 ALERT＝连续两日阻塞未归档）⇒ 两层结论分别陈述，勿互相替代。

## 实测结论（2026-09-22 基线，供日后对比）
- 归档覆盖率 **401/433 = 92.6%**；32 跳过 = 31 份 `*.failed.txt` + 1 份 `*_filtered_test.txt`，均设计内。
- **`x_ai_intel_daily`（★2026-09-23 当日实测精修分类）**：产物停 **08-17（37 天）**、近 30 份报告 30/30 全含「跳过/前置未满足」，却每日照跑到 done。**但不要归入"静默失败"** —— 它每次都**显式抛异常堆栈**、每次都留 done 报告、且从不误推送（异常发生在 `set_session`，根本走不到 `notify_feishu`）＝**"看得见的失败"**。真因经只读诊断确认为**外部前置条件长期不满足**：TMWebDriver 驱动在线（09-23 实测挂有 4 个真实标签页：typesafe.ai / google / 电子税务局 / 一网通办），**唯独没有已登录的 x.com 标签页**。⇒ 判据升级：产物停更的任务要分三类：①真静默失败（跑完零证据零报告）②可见失败（总有异常/降级证据＝**外部条件缺失**，处置是恢复条件或退役，不是修脚本）③无意义空转（跑得漂亮但目标已死，如 `sqlite_snapshot`）。
- `trendradar`：31 份 failed 中 28 份为重试丢弃稿，**3 份真漏发**（07-26 晚 / 07-27 晨晚，因 `bad_report_quality` skipped；57 天前，其后自愈）。
- `eia_weekly`：无持久产物、无推送、`EIA_API_KEY` 长期缺失（静默降级）。
- 僵尸任务 `bridge_autonomous`/`folder_watcher` 空转史（213/2303 次 NO_OP）已移入 `disabled/`（触发止 2026-09-21 23:26），删除已提交（`sche_tasks/*` 已 gitignore，disabled 副本留磁盘）。
- ★**`sqlite_snapshot` = 事实僵尸（2026-09-23 该任务当日实测）**：`temp/sqlite_probe.py` 监控的 2 个库（`temp/message_2_merged.db` mtime=2026-05-19、`temp/hive_coupon_fix/m7hpgl58zzb4.db` mtime=2026-06-04）**早已冻结**；`temp/sqlite_snapshots/` 已有 **106 份快照**（20260611 起），全量相邻两两对比 **105 次变化 0 次** ⇒ 其"delta≠0 → 飞书告警"通路**从未触发过一次**，每日空转产一份 10 KB JSON。任务本身运行/报告/判断均正常（**非静默失败**），属监控目标已死。建议退役 or 改 `MONITORED_DBS` 指向活跃库（待用户决定，未改动）。

## 坑
- **`index.html` 是自包含归档页（无 href）** → 按 basename 匹配算覆盖率会得 `0/433` 假结论。
- `output/` 不含 roi/vault/跨仓产物（如 `roi_history.json` 在仓根、Wiki 在 vault、周巡报告在 `bar_smyze_opserver/`），须从 done 报告或显式已知路径取。
- 产物目录统计别用 `ls -lt | head`（列不全会看错），写脚本一次算完。
- 报告编号真源 = **`temp/autonomous_reports/history.txt`**（LIVE 树；`helper.py` 的 `_REPORTS_DIR=_TEMP_DIR/"autonomous_reports"`、`_HISTORY_FILE=_REPORTS_DIR/"history.txt"`，**已核实源码第26-27行**）。归档副本 `memory/autonomous_reports/{history.txt,history.md,INDEX.md}` 由 `tools/autonomous_reports_archive.py sync/inventory` 生成（history.txt **gitignore**、`history.md`/`INDEX.md` 只是展示）。
- ★**两树会分叉导致编号复用**（2026-09-22 R351 实测）：helper 只写/只读 LIVE 树，归档树仅在被显式 sync 后才更新 ⇒ 若上一轮报告只进了归档树，`_next_report_number()` 按 LIVE 首行算出的编号会**与上轮重号**（本次实测 R350 撞号，两 history 各 319 行仅首行不同）。修法：改 ①报告文件名 ②**LIVE history 首行**（只改首行），再跑 `sync --apply` + `inventory --apply`，末了 `diff` 两 history 必须逐行一致。根治（待批）＝编号取两树 max+1 且收尾自动串联 sync/inventory。
- 正确收尾姿势：报告写到 cwd → `from autonomous_operation_sop.helper import complete_task; complete_task(name, historyline, report_path)`（它负责编号+移动+prepend history+通知）。**手写报告后不要再手工改编号体系**；若已手工放置，至少跑一次 `max(R###)+1` 校验并与 history.txt 对齐。
