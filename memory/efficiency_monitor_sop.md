# efficiency_monitor_daily 采集口径与判读（L3）

任务：每天 09:00 跑 `temp/efficiency_monitor.py`（107 行轻量解析器，仅 stdlib，无三方依赖）。
调用（**必须 cwd=仓库根**，prompt 里的 `./temp`、`./logs/...` 全是相对路径）：
```python
import sys; sys.path.insert(0,'./temp')
from efficiency_monitor import EfficiencyMonitor
from pathlib import Path
m=EfficiencyMonitor(Path('./logs/autonomous_reflect.log')); r=m.parse_log(); print(r)
m.save_report('./temp/efficiency_report_latest.json')
```

## 三个口径坑（2026-09-25 实测实证，回读核对前先看这里，避免误报"不一致"）
1. **`log_lines` ≠ `wc -l`**：模块用 Python 文本模式逐行读（通用换行），会额外把**孤立 `\r`**（`\r` 后不跟 `\n`）也断行。实测 `wc -l=38031` 而 `log_lines=38042`，差 11 = 文件中孤立 `\r` 实测个数（`len(re.findall(rb"\r(?!\n)",raw))`）。**不是采集错误**。
2. **`tail_errors_2000` 是关键字命中数、不是故障数**：词表固定为 `('error','failed','traceback','exception','no module named','failed:')` 且 casefold 匹配，取**日志最后 2000 行**。故：自写复算须用同词表（用 `fail` 会多命中行）；命中里多数是**正文/diff/索引文本**回显（如 `git diff -U0` 行、报告正文提到 error 字样）。判据：`health='DEGRADED'` 仅当 `issues` 非空或 `tail_errors > 50`。
3. **`notes` 会"自己消失"，原因在 cwd 相对路径**：`if not Path('./temp/bridge_autonomous.py').exists() and not Path('./autonomous_reports').exists()` 才追加"已归档 bridge_autonomous"note。仓库根 `./autonomous_reports/` 一出现（实测 2026-09-24 10:22 由"缺失报告正文恢复"任务生成），条件即假 ⇒ note 消失。**属可见性漂移、非健康变化**（health 两天均 OK）。

## 判读要点
- `health` 只算真 issue；已知良性噪音（LLMLingua/numpy ABI warmup）与已归档任务缺失**降为 notes 不降级**（2026-09-23 修）。
- 采集前先看日志 mtime 是否早于本次运行时刻：是则所有命中均为历史回显，本轮无新故障。
- 产物 `temp/efficiency_report_latest.json` 覆盖写；`temp/`+`sche_tasks/done/` 均 gitignored ⇒ 无跟踪产物、无需提交。

## 输入源已冻结（2026-10-15 复核实证）
- 采集输入 `logs/autonomous_reflect.log` **无写入方**：mtime 停在 2026-10-04 10:47:46（896628 B），`grep -c '2026-10-15'`=0、档内最大日期 token=2026-09-29；`log_lines=11362`/`log_size_mb=0.86` 已连续多批逐字相同。`launchctl list` 无 `com.cuip.genericagent-autonomous`，`~/Library/LaunchAgents/` 亦无该 plist（在册仅 pet/price-alert/scheduler/desktop-bridge-repair/launch/l2-gate）⇒ 孤儿遗留日志；在写的是 `logs/scheduler_reflect.log`。
- ⇒ 判读铁律：`health='OK'` 只证明**采集器可用**，不等于当日系统健康；`log_*`/`structured_*` 描述的是那个冻结时点。判趋势前须先换输入源。
- 换源属配置变更（改 `sche_tasks/efficiency_monitor_daily.json` 的输入路径），**未获批不得擅改**；可行性只读实测：`python3 tools/error_signal.py --log logs/scheduler_reflect.log` → `distinct_strong=8`、`verdict=OK (metric=strong, threshold=20)`、rc=0 ⇒ 换源当天不会假红。
