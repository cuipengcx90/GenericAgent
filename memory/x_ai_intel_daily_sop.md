# x_ai_intel_daily 前置条件与失败速判（L3）

任务：每天 09:03 跑 `temp/x_ai_intel/x_intel.py`（6 个账号：OpenAI/AnthropicAI/GoogleDeepMind/huggingface/sama/karpathy；SQLite 按帖子 URL 去重；有新增才推飞书）。

## 关键前置（唯一硬依赖）
**浏览器里必须有一个已登录 x.com 的普通网页标签页，且 TMWebDriver 会话可用。** 缺此条件时脚本在 `set_session("x.com")`（L158）fail-fast 抛 `RuntimeError: No active X browser tab was found`，rc=1，**未进入采集循环 ⇒ 零推送、零写库、零报告**。

## 失败时 3 步只读速判（别急着重跑，prompt 也禁止重试）
```python
import sys; sys.path.insert(0,".")            # cwd=仓库根
from TMWebDriver import TMWebDriver
d=TMWebDriver(); print(d.is_remote, len(d.get_all_sessions()), d.find_session("x.com"))
```
1. `is_remote=True` + 端口 18765/18766 可连 ⇒ 驱动在线（问题在前置，不在驱动）；
2. `get_all_sessions()==0` + `web_scan(tabs_only=True)` 也报无标签 ⇒ 真无会话（两侧同空=真无，非陈旧）；
3. 用 AppleScript 只读枚举真实标签，定性根因：
   `osascript -e 'tell application "Google Chrome" to get URL of tabs of windows'`
   ★**实测坑（2026-09-25）**：若唯一标签是 `chrome-extension://…/onetab.html`（OneTab 2.21 等扩展页），Chrome 会显示"有窗口有标签"，但**扩展页不注入内容脚本 ⇒ 驱动会话恒为 0**——用户把标签收进 OneTab 就等价于"没有浏览器标签"。

## 恢复与处置
- 恢复（需用户动作，代理不得代开标签）：打开并登录 `https://x.com/home` 且**不要收进 OneTab** → 下次调度自动恢复。
- 长期不用则建议归档为 disabled（避免每天一条必然失败记录），归档需批准。
- 判据归类：属 scheduled_task_sop 的**②可见失败** ⇒ 处置是恢复外部条件或退役，**不是改脚本**。

## 零副作用核验（失败时也应回读，证明"没动过东西"）
去重库 `temp/x_ai_intel/x_intel.sqlite3` mtime、`temp/x_ai_intel/reports/` 份数与最新文件、`pgrep -fl x_intel.py`；本任务**只允许**写 `/tmp/x_ai_intel.log`（启动前先备份旧日志再截断，方能判新鲜度）。
