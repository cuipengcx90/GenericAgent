# R556 L站守护去 Galley 依赖：独立桥保活 + 浏览器自愈 + 无会话短退避

日期 2026-10-06 | GA 提交 0714a1e（5 文件 515+/23−）| 相关 R555 / R546 / R550

## 1. 用户诉求
不把「开着 Galley 座舱」当前置；用日常 Chrome（非无头）；浏览器关了要能自动重开。

## 2. 三层自愈链（新增）
1) 独立桥 `tools/tmwd_bridge_watch.py`（新，261 行）：保活 18765 WS / 18766 HTTP，桥不在即拉起，与 Galley 的 runner.workbench_bridge 彻底解耦；selftest PASS。
2) 看门狗 `linuxdo_daemon_watchd.py ensure_bridge()`：桥不可达 ⇒ 先自动拉独立桥（冷却 600s、起后等 15s），仍不可达才判 BRIDGE_DOWN 只告警不重启；`status` 增 bridge_reachable / bridge_runner_pids / bridge_ensure；selftest 86 checks / 0 failed。
3) daemon `try_ensure_tab`：无 linux.do 标签 ⇒ AppleScript 补开并导航——**Chrome 未运行时该调用会顺带拉起 Chrome**；补开前先查 EXISTS 防标签堆积，日限 6 次 / 间隔 10min，台账 `temp/linuxdo_browse_daemon/ensure_tab_ledger.jsonl`。

## 3. 两处现场修复
- 短退避：旧码无会话首轮睡 600–1200s（Chrome 冷启动仅 1–2min，实测白等 13min）⇒ 新增 `SESS_RETRY_SHORT=(20,40)s` 前 `SESS_RETRY_SHORT_MAX=6` 次，用尽才走原长退避。
- ★真缺陷：`try_ensure_tab` 用 `io.open` 读写台账但模块从未 `import io` ⇒ 每次抛 NameError 被 `except` 吞 ⇒ **补开限额从未生效**（可无限堆标签）。修=补 `import io` + 用例 T7o（限额）/T7p（EXISTS 去重）/T7q（短退避边界）；daemon selftest 33 → 35/35。

## 4. 真机实证（2026-10-06 时间线）
- 21:12 全链：watchd 判 BRIDGE_DOWN → ensure_bridge 拉起独立桥（pid 5444）→ daemon 重生 → 补标签顺带拉起 Chrome → 扩展连上会话 → ROUND OK。
- 21:17:08 看门狗以新码重生 daemon（pid 15266，命令行含 `-u tools/linuxdo_browse_daemon.py watch`）；21:17:09 日志 `RC_NO_SESSION_RETRY …（1/8，睡 40s）` ⇒ 短退避生效。
- 21:17:50 日志 `用户在前台看 linux.do，跳过本轮浏览，睡 5min` ⇒ 前台避让正常。

## 5. 文档与耐久
- SOP 回填：`linuxdo_browse_sop.md` 新增「守护生存面：Galley 解耦与浏览器自愈（R556）」节 + daemon 自测 14/14 → 35/35；`linuxdo_acts_sop.md` 看门狗「四件事 → 五件事（含 ensure_bridge）」、自测 77 → 86、status 字段补 bridge_*、新增坑#15（吞异常掩盖缺 import ⇒ 限额静默失效；判据「except 兜底的路径须先证明正常路径走通且台账真被写入」）。
- 通用判据：**「无报错」≠「生效」**；桥不可达属环境面，只告警不重启（与 R555 同口径）。

## 6. 残余
- daemon 运行码已换新（pid 15266，21:17:08 起）；夜间长跑观察段级节律与长休是否按 6–8 段 × 85–160min 走（R550 口径）。
- 自愈链未接定时任务自身；watchd 由 sche_tasks 每日保活重新武装（R550 起）。
