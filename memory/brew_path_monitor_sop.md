# brew PATH 冲突监控 SOP (brew_path_monitor)

## 产物与入口
- 脚本：`GenericAgent/temp/brew_path_checker.py`；输出：`temp/brew_path_reports/<date>.txt`
- 调度：`sche_tasks/brew_path_monitor.json`（weekly 08:00，max_delay 24h）
- 通知约束：**仅"新冲突/新依赖缺失"才 `notify_feishu`**；与上一份报告同集合 → 静默（防周周重复告警）。

## 判定"是否新问题"的方法（必做）
- 先 `ls -t temp/brew_path_reports/` 取上一份，比对 `## 4. Summary` 的"Tool conflicts / Missing deps"集合；集合未变即不推送。
- 长期存在项（自 2026-05-26 起未变）：`python3/curl/pip3` 多版本 + `pandas` 不可用 + "anaconda 优先于 homebrew"告警 → 一律不算新问题。

## ★关键坑★（否则每周产生误报结论）
- **脚本结论强依赖调用方环境的 PATH**：定时/子进程环境 PATH 只有 5 项(`/opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`)，而用户**登录 shell 实测 35 项且 `/opt/homebrew/bin` 在 `/opt/anaconda3/bin` 之前**。
- 后果：报告里"Anaconda has higher priority than Homebrew (may shadow brew tools)"在真实交互环境下**不成立**（brew 优先）；且工具版本列表/数量会随调用方变化（历史报告 PATH 项数 5/22/35 都出现过），**跨报告直接对比会被误导**。
- 需真实结论时用 `zsh -ilc 'echo $PATH'` 取登录 shell PATH 再评估；比对历史只能比"同一调用环境下"的结果。

## pandas 报错的真实原因
- 非"未安装"：anaconda 里 `pandas==2.2.2` 存在，但 `import pandas` 抛 `ImportError: numpy.core.multiarray failed to import`（pandas 按 numpy 1.x 编译，环境 numpy 已 2.4.4，ABI 不兼容）；homebrew python3.13 无 pandas。
- 修复选项（需用户批准，勿擅自改环境）：`/opt/anaconda3/bin/pip install --force-reinstall --upgrade pandas` 或 `conda install -c conda-forge pandas`；若不需要则可从检查项移除以免固定误报。
