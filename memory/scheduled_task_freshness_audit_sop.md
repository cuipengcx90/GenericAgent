# 定时/反射任务「产物级」新鲜度审计 SOP

来源：R350（2026-09-22 实测，只读，零配置改动）。触发词：静默失败 / 僵尸任务 / 产物过期 / 归档覆盖率。
核心判据：**任务是否"真在产出"，看产物文件，不看它有没有跑、有没有写"已完成"，也不看推送文案。**

## 方法（全只读）
1. 任务清单真源 = `sche_tasks/*.json`（gitignore 但仍被 git 跟踪过的会残留；`sche_tasks/disabled/` 为归档区，`ls sche_tasks` 查启用项）。
2. 从 `done/` 报告里抽**任务自己声明的产物路径** → 对每个产物实测 mtime/大小/尾部抽样，算 age 天数。勿凭任务名猜路径（命名漂移普遍：`weekly_report_reminder` 真产物是 `temp/content.json`，遗留孤儿 `temp/weekly_content.json` 会误导）。
3. 失败件 `*.failed.txt` 二分类：**同日另有成功件 = 质量重试丢弃稿（非故障）**；**孤儿（fail 无同日成功）= 真漏发**。
4. 归档覆盖率判据用 `build_output_archive.parse_name`，别用文件名猜。

## 实测结论（2026-09-22 基线，供日后对比）
- 归档覆盖率 **401/433 = 92.6%**；32 跳过 = 31 份 `*.failed.txt` + 1 份 `*_filtered_test.txt`，均设计内。
- 真静默失败仅 1 个：**`x_ai_intel_daily`**（产物停 08-17 = 36 天；近 30 份报告 30/30 全含「跳过/前置未满足」，却每日 09:00 照跑到 done）。
- `trendradar`：31 份 failed 中 28 份为重试丢弃稿，**3 份真漏发**（07-26 晚 / 07-27 晨晚，因 `bad_report_quality` skipped；57 天前，其后自愈）。
- `eia_weekly`：无持久产物、无推送、`EIA_API_KEY` 长期缺失（静默降级）。
- 僵尸任务 `bridge_autonomous`/`folder_watcher` 空转史（213/2303 次 NO_OP）已移入 `disabled/`（触发止 2026-09-21 23:26），删除已提交（`sche_tasks/*` 已 gitignore，disabled 副本留磁盘）。

## 坑
- **`index.html` 是自包含归档页（无 href）** → 按 basename 匹配算覆盖率会得 `0/433` 假结论。
- `output/` 不含 roi/vault/跨仓产物（如 `roi_history.json` 在仓根、Wiki 在 vault、周巡报告在 `bar_smyze_opserver/`），须从 done 报告或显式已知路径取。
- 产物目录统计别用 `ls -lt | head`（列不全会看错），写脚本一次算完。
- 报告编号真源 = `memory/autonomous_reports/history.txt`（**gitignore**；`history.md` 只是抢救索引、`INDEX.md` 只是展示）。本次 history.txt 缺 R349 行（R349 那次只更新了 history.md）→ `helper._next_report_number()` 算出 349 会与已存在文件撞名（`shutil.move` 报错）。
- 正确收尾姿势：报告写到 cwd → `from autonomous_operation_sop.helper import complete_task; complete_task(name, historyline, report_path)`（它负责编号+移动+prepend history+通知）。**手写报告后不要再手工改编号体系**；若已手工放置，至少跑一次 `max(R###)+1` 校验并与 history.txt 对齐。
