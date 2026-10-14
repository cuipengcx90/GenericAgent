# x_ai_intel_daily 前置条件与失败速判（L3）

任务：每天 09:03 跑 `temp/x_ai_intel/x_intel.py`（6 个账号：OpenAI/AnthropicAI/GoogleDeepMind/huggingface/sama/karpathy；SQLite 按帖子 URL 去重；有新增才推飞书）。

## 关键前置（唯一硬依赖）
**浏览器里必须有一个已登录 x.com 的普通网页标签页，且 TMWebDriver 会话可用。** 缺此条件时脚本在 `set_session("x.com")`（L158）fail-fast 抛 `RuntimeError: No active X browser tab was found`，rc=1，**未进入采集循环 ⇒ 零推送、零写库、零报告**。

## 失败时 3 步只读速判（别急着重跑，prompt 也禁止重试）
```python
import sys; sys.path.insert(0,".")            # cwd=仓库根
from TMWebDriver import TMWebDriver
d=TMWebDriver(); print(d.is_remote, len(d.get_all_sessions()), d.set_session("x.com"))  # ★勿用 d.find_session()：remote 模式下它只遍历本地 self.sessions（恒空）⇒ 假「无会话」
```
1. `is_remote=True` + 端口 18765/18766 可连 ⇒ 驱动在线（问题在前置，不在驱动）；
2. `get_all_sessions()==0` + `web_scan(tabs_only=True)` 也报无标签 ⇒ 真无会话（两侧同空=真无，非陈旧）；
3. 用 AppleScript 只读枚举真实标签，定性根因：
   `osascript -e 'tell application "Google Chrome" to get URL of tabs of windows'`
   ★**实测坑（2026-09-25）**：若唯一标签是 `chrome-extension://…/onetab.html`（OneTab 2.21 等扩展页），Chrome 会显示"有窗口有标签"，但**扩展页不注入内容脚本 ⇒ 驱动会话恒为 0**——用户把标签收进 OneTab 就等价于"没有浏览器标签"。★**第三种形态（2026-09-28 实测）**：3 个标签全是**普通网页**（linux.do）但**没有 x.com** ⇒ 同样判"真无 x.com 会话"（判据仍是两来源同空：agent 侧 `web_scan` + `osascript` 枚举），与 OneTab 坑成因不同、处置相同（等用户开标签）。

## 成功判据与历史失败面（2026-09-27 实测补充）
- **成功标记须严格正则** `Saved \d+ new posts`。宽松规则（"含 new posts 且不含 RuntimeError"）会把第二类失败误判成成功——实测 38 份 done 报告里有 2 份被该规则误判，逐字复核正文均明写"未出现 `RUN_DONE` 或 `Saved N new posts` 成功标记"。
- 历史失败两形态：①`未找到URL包含 'x.com' 的会话` → `RuntimeError: No active X browser tab was found`（缺会话，占多数）；②`CDP fallback failed: No tab with given id <id>`（会话匹配到 x.com 但该标签 id 已失效，如 2026-08-18 / 2026-09-16）。
- 量化：`sche_tasks/done/*_x_ai_intel_daily.md` 共 **39 份**（2026-09-28 现场重算；★该计数逐日增长，判读一律以现场 `ls sche_tasks/done/*_x_ai_intel_daily.md | wc -l` 为准，勿引旧数），严格标记命中 **0**；末次真实成功 = **2026-08-17**（去重库 mtime `2026-08-17 16:03:49` + 当日 5 份报告、末份 `20260817_160400.md`）。
- **速判坑（2026-09-27）**：裸 `TMWebDriver()` 在 `is_remote=False` 时会**自建**服务端（日志出现 `WebSocket server running on ws://127.0.0.1:18765`），此刻 `get_all_sessions()==0` 是"扩展尚未接入"的**瞬时态**，不可当"真无会话"结论；权威读数用 agent 侧同链路 `web_scan(tabs_only=True)`（实测：裸实例 0 → web_scan 见 1 个 `https://example.com/`，仍无 x.com），Chrome 真实标签用 osascript 枚举作第二来源。

## 第四形态：标签在册但该标签 JS 通道死（2026-09-28 现场实测）
- 现象：`get_all_sessions()` 里有 x.com，`set_session("x.com")` **成功**，但 `execute_js` 报 `CDP fallback failed: No tab with given id <id>`；同批其它标签可能正常（实测 7 标签 = 2 OK / 5 FAIL）⇒ **故障按标签粒度**，非驱动整体掉线。
- 判据：`web_scan`/`get_all_sessions` 给的 id 与 AppleScript 枚举的「标签确实存在」是**两件事**（前者=扩展会话表，本次其 id 与 CDP 真值不符亦照列）；两者不可互证「通道可用」。唯一判据是**实跑一次 JS**：
  `python3 -c "import sys;sys.path.insert(0,'.');from TMWebDriver import TMWebDriver;d=TMWebDriver();print(d.execute_js('return document.title', session_id='<x.com 标签 id>'))"`
- ★「激活标签」不能修：实测 `set active tab index` + `activate` + 等 4s 后仍 FAIL（该标签 title 亦仍为空=未渲染）⇒ 别把「切到前台」当修复手段。
- 处置：仍属外部前置 ⇒ 报阻塞、等用户重开标签（或手动在该标签加载一次 x.com），**不改脚本**。

## 恢复与处置
- 恢复（需用户动作，代理不得代开标签）：打开并登录 `https://x.com/home` 且**不要收进 OneTab** → 下次调度自动恢复。
- 长期不用则建议归档为 disabled（避免每天一条必然失败记录），归档需批准。
- 判据归类：属 scheduled_task_sop 的**②可见失败** ⇒ 处置是恢复外部条件或退役，**不是改脚本**。

## 零副作用核验（失败时也应回读，证明"没动过东西"）
去重库 `temp/x_ai_intel/x_intel.sqlite3` mtime、`temp/x_ai_intel/reports/` 份数与最新文件、`pgrep -fl x_intel.py`；本任务**只允许**写 `/tmp/x_ai_intel.log`（启动前先备份旧日志再截断，方能判新鲜度）。

## 第四形态复现 + 一处速判假阴性（2026-09-29 09:03 实测）

- **第四形态复现**：`get_all_sessions()` 7 条中含 x.com 标签（id `2109120745`）⇒ `set_session("x.com")` **成功** ⇒ 随后任意 `execute_js` 均报 `CDP fallback failed: No tab with given id 2109120745.`（title/url/账号按钮三探针全 FAIL）。即「标签在册 ∧ default session 可设 ∧ JS 通道仍死」。
- **脚本实跑同因失败**：日志 `/tmp/x_ai_intel.log` 显示 `x_intel.py` 在 `driver.jump(...)` 处经 `TMWebDriver.execute_js`（`TMWebDriver.py:194`）抛 `Exception: {'name': 'Error', 'message': 'CDP fallback failed: No tab with given id 2109120745.', 'stack': ''}`，**未进入采集循环** ⇒ 零推送、零写库、零报告。
- **速判假阴性坑（本次新发现，已改正上方速判代码）**：`TMWebDriver().find_session("x.com")` 在 `is_remote=True` 时**恒返回 `[]`**（该实现只遍历本地 `self.sessions`，remote 实例本地表为空；只有 `set_session()` 走 `_remote_cmd({"cmd":"find_session"})`）。⇒ 判「有无 x.com 会话」一律以 `set_session()` 返回值为准（或 agent 侧 `web_scan` 作第二来源）；`find_session()` 的空结果**不构成证据**，据此判「无标签」会与事实相反。
- **量化刷新（2026-09-29 现场）**：`ls sche_tasks/done/*_x_ai_intel_daily.md | wc -l` = **40**；严格正则 `Saved [0-9]+ new posts` 命中 **0**；去重库 mtime 仍 `2026-08-17 16:03:49`（无 `-wal/-shm`）；`reports/` 仍 5 份（末份 `20260817_160400.md`）⇒ 末次真实成功仍为 2026-08-17。处置仍属**外部前置**（需用户手动保留一个已登录 x.com 标签，勿收进 OneTab）；退役提案见 `pending_decisions/DECISIONS_R484*`，未批准前保持现状、勿改脚本。
- **第三形态再次复现 + 量化刷新(2026-10-14 09:04 现场)**:`set_session("x.com")` 返回 `None`(日志首行 `警告: 未找到URL包含 'x.com' 的会话`)⇒ 脚本 L158 fail-fast;速判 `is_remote=True`、`get_all_sessions()`=**8**,`osascript` 枚举 8 个真实标签**全为普通网页(linux.do/github/quark/context-mode 等)、无 x.com** ⇒ 两来源同空=真无 x.com 会话(即上文第三形态)。量化刷新:`ls sche_tasks/done/*_x_ai_intel_daily.md | wc -l` = **55**;严格正则 `Saved [0-9]+ new posts` 命中 **0**;去重库仍 12288 B / mtime `Aug 17 16:03`(无 `-wal/-shm`)、`reports/` 仍 5 份(末份 `20260817_160400.md`)⇒ 末次真实成功仍为 2026-08-17。处置不变(属外部前置,需用户保留一个已登录 x.com 标签,勿收进 OneTab)。
