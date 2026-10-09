# L站五动作统一防检测 SOP（等级前置 × 随机内核 × 互斥锁）

> 建立 2026-10-05（anyrouter 封号事故复盘后，用户定义）。适用范围：linux.do 上一切**自动化动作**——等级检测 / 浏览 / 回帖 / 点赞 / 发帖。
> 铁律来源：`memory/anti_detection_sop.md`（独立身份 / 随机化从第一版起 / 指纹模拟 / 量级与可停）。
> 站点硬事实：**任何「固定时刻 + 固定间隔 + 稳定量级」的脚本化流量都是被清洗特征**（本次封号嫌疑第一）。

---

## 一、五动作与它们的层级关系

| 动作 | 角色 | 谁执行 | 触发方式 | 日量级（默认，见 `profiles`） |
|---|---|---|---|---|
| 等级检测 | **前置**（只读） | daemon 内 `check_level` | daemon 每天 1 次（**上午 08:00–10:00 窗内随机**；内核 `LEVEL_CHECK_WINDOW=(8,10)`，env `LJQ_LEVEL_WINDOW` 可覆盖；**窗口是硬口径**：`LEVEL_CHECK_TTL_S=6h` 只防「近邻重复」（重启/多入口/同一上午第二次），错过的窗口当日即刻补跑一次，绝不顺延出窗、也不因「昨天查得晚」而自我延续；见坑 §17） | 零动作，仅读等级卡 |
| 浏览 | 补量（缺「已读帖子/浏览比例」时） | daemon `browse_batch` | daemon 自动，或提示词触发（拉起 daemon） | cap 180–220（日抖，内核 `daily_cap=200` ± `cap_jitter=20`，当天摇一次且跨重启稳定） |
| 回帖 | 补量（缺「回复话题数」时） | `tools/linuxdo_acts.py` reply | **等级路由排队**（daemon 随机消费）/ 提示词 | cap 6（±2 抖） |
| 点赞 | 补量（缺「获赞/送赞」时） | `tools/linuxdo_acts.py` like | 同上 | cap 12（±4 抖） |
| 发帖 | 补量（缺「获赞」且有草稿时） | `tools/linuxdo_acts.py` post | 等级路由排队（★无草稿时 daemon 低频自动锻造草稿，见 §十一）/ 提示词 | cap 1 |

**路由规则（等级检测为前置，2026-10-05 订正版）**：
- **等级检测是唯一的前置入口**：只有它读站点等级卡，读出的缺项按类分流（一次可命中多项，**多缺项并行排队、互不阻塞**）。
- 分流表（`classify_pending` 五桶）：`browse` 类（已读帖子/浏览比例）→ daemon `browse_batch` **自补**；`reply` 类（回复话题数）→ 排 `pending_acts.reply`；`like` 类（送赞）→ 排 `pending_acts.like`；`earn` 类（获赞，站点无机器通道）→ **有草稿**则排 `pending_acts.post`（`post_src=level_route`）；**无草稿**则先尝试**低频自动锻造**一份草稿（§十一，`forge`，带冷却与 deai 闸门），本轮退化为排 `reply`（多写多被赞）；`manual` 类（无法归类）→ 只发私信，脚本绝不代操作。
- 排队与执行**解耦**：等级检查那一刻只往 `state.pending_acts` 排队（`queue_pending_from_route`），真正动手由 daemon 在随机时刻取用（每轮每类最多 1 条），每类成败原因落 `acts_log.jsonl`（如 `SKIP:NO_DRAFT`）。
- 等级达标 ⇒ 不排队 ⇒ 不看帖、不回帖（用户原话：「正常的话不需要回帖，也不需要浏览帖子」）。
- ★**禁止定点唤醒动作**：「浏览/回帖/点赞/发帖」四个动作**没有**各自的定时任务（2026-10-05 用户纠正，四个 `sche_tasks/linuxdo_acts_*.json` 已删除）。它们的发生时刻只有两个来源：①等级路由排队后由 daemon 在随机段内消费；②用户提示词临时触发。任何「到点就动站点」的入口都属违规实现。

## 二、随机内核（单一源 `tools/linuxdo_antidetect.py`）

五个动作共用同一套随机化，禁止各写一套：

- **多段切分**：一天分若干段，段长随机（`seg_n/seg_len`）。**浏览档**默认 5–7 段 × 75–150 分钟（2026-10-06 按 10-05 实测自然节奏上调；旧值 2–4 段 × 6–25 分钟只适用于回帖/点赞/发帖）。段位由日期种子派生（同日重启不重排），段间空档由 daemon `maybe_segment_gate()` 等待。
- **段内间隔**：`gap` 240–600s（回帖）/180–600s（点赞），另有 25–35% 概率走 `gap_long`（13–40min）。
- **日限抖动**：`daily_cap ± cap_jitter`，每天重新摇。
- **静默窗**：01:00–07:30 绝不动手；**晨抖** 0–90min（开启时刻每天不同）。
- **随机休息日**：约 1/7 概率整日不动，且与上次休息最小间隔 3 天（不连休）。
- **不算随机的东西**（本次教训）：固定 5 分钟一轮、固定 300s 间隔、09:30 准点 —— 全部视为**未随机化**。

## 三、互斥锁（同一 linux.do 标签只有一个写者）

- 实现：`tools/linuxdo_acts.py: acquire_lock()/release_lock()`，锁文件 `temp/linuxdo_acts/.lock`（`LJQ_ACTS_LOCK` 可重定向；自测必须重定向到沙箱）。
- TTL 1800s；**只按 TTL 判陈旧，不探 pid**（遵守 RULE 5，避免误杀）。
- 覆盖命令：`run / reply-one / like-one / post`（只读命令 `due/state/profiles` 不加锁）。
- 占锁输出 `{"skip": true, "why": "LOCKED_BUSY"}`、**rc=0**（非错误，禁止重试）。
- daemon 执行排队动作时同样持锁（tag `daemon-acts`），`try/finally` 释放；冲突落 `temp/linuxdo_browse_daemon/acts_log.jsonl` 的 `LOCK_BUSY`。

## 四、触发面（只有两种，2026-10-05 订正）

1. **等级路由自动**（唯一常规路径）：daemon 内每天随机错峰 1 次读等级卡 ⇒ 缺项按类**并行**排队 ⇒ 随机段内消费。等级达标则不排、不做任何动作。
2. **提示词触发**（用户随口说「刷会儿帖子」「回两条」「发个帖」）：会话内直接调执行器（见 §五），单动作内部同样走拟人层。

> ★**不存在「定时唤醒动作」这一层**（订正 2026-10-05）：`sche_tasks/linuxdo_acts_{browse,reply,like,post}.json` 四个定点任务**已删除**——它们等于「到点就动站点」，是被检测特征，且与「等级检测为前置路由」的设计冲突。
>
> 现存唯一与 L 站相关的定时任务：

| 文件 | 时刻 | 作用 |
|---|---|---|
| `linuxdo_trust_level_check.json` | 09:30 | **纯本地保活**：①`linuxdo_daemon_watchd.py ensure` 每日重新武装看门狗；②查守护在不在跑，不在则拉起；③`linuxdo_browse_daemon.py status` 记录本地 state。**零站点访问、不触发任何动作**（prompt 内明文禁止 web_scan/web_execute_js 与改随机参数）。 |

> 这个任务的固定时刻本身不构成风险（它不产生任何站点请求）；真正会被判定的「动作发生时刻」由随机内核独占决定。

## 五、执行器 CLI 速查

```bash
python3 tools/linuxdo_acts.py due                 # 只读：三类动作是否到期 + eta（零站点请求）
python3 tools/linuxdo_acts.py profiles            # 只读：当前随机参数
python3 tools/linuxdo_acts.py state               # 只读：日账/上限/暂停
python3 tools/linuxdo_acts.py run reply --apply    # 真执行（不加 --apply 是 dry-run）
python3 tools/linuxdo_acts.py reply-one --topic <tid> --raw "文案" --apply
python3 tools/linuxdo_acts.py like-one --post <pid> --apply
python3 tools/linuxdo_acts.py post --title "..." --raw "..." --category <id> --tags "..." --apply

# 发帖草稿：锻造（真实素材 → codex 人味生成 → deai 闸门）+ 去 AI 味自检
python3 tools/linuxdo_post_forge.py list                       # 只读：待用/已用草稿
python3 tools/linuxdo_post_forge.py forge --dry-run            # 只生成不落盘（看文案）
python3 tools/linuxdo_post_forge.py forge --notify             # 真落草稿 + 飞书私信通知
python3 tools/linuxdo_deai.py check --text "文案"               # 去 AI 味闸门（rc 0 过 / 1 不过）
```

- 现役接口（实测）：回帖 `POST /posts.json {topic_id, raw}` + `X-CSRF-Token`；点赞 `POST /post_actions.json {id, post_action_type_id: 2}`；发帖 `POST /posts.json {title, raw, category, tags}`；成功判据只有响应里的 `id`。
- **两种触发语义（2026-10-05 实测确认，勿混）**：
  - `run <kind>` = 定时/守护路径，**受随机段约束**（不在段内返回 `out_of_segment(...)` + `eta`，rc=0 不是错误）；
  - `<kind>-one`（`reply-one`/`like-one`）与 `post` = **人工指定路径**，不走段判定（`post` 仍受 `post_interval_ok` 日间隔 + cap 1 约束），所以用户临时要求「回这条」「点个赞」「发个帖」可以直接用。
- 长任务（单次可达数分钟）必须后台起：`python3 tools/bg_task.py start --name ldo_acts_reply -- python3 tools/linuxdo_acts.py run reply --apply`，随后 `bg_task.py status --name ...` 轮询（**禁 sleep 阻塞**，见 RULE 15）。

## 六、风控保险丝（不可绕过）

- 命中判据（HTTP/正文）：`BANNED`(403 封禁) / `RATE_LIMIT`(429) / `FORBIDDEN`(403) / `LOGIN_REQUIRED` ⇒ 写 `state.pause_until`（3600–7200s 随机）+ 飞书私信告警；同类 ≥3 次 ⇒ **当日停**。
- CF 挑战（Just a moment / turnstile）只轻量计数，不当作封禁中断。
- 遇到上述信号：**立即停手，禁重试、禁换 tab、禁「感觉还好」硬顶**（本次封号就是反面教材）。

## 七、拟人层（动作内部「动手前后干什么」，单一源 `tools/linuxdo_humanize.py`）

分工：内核管**什么时候做 / 做几件**（跨动作时序），拟人层管**单个动作内部长什么样**（单动作形态）。

- 四类形态：`KIND_BEHAVIOR = browse | reply | like | post`，每类有独立参数（阅读停留、滚动计划、打字计划、写作停顿、决策犹豫、动作间隔）。
- 浏览：先读帖（`read_dwell` = 按正文长度估真人阅读时长）→ 按 `scroll_plan` 分段滚动 → 30% 概率浅读提前收工。
  - 2026-10-05 起 **daemon 浏览路径也走本内核**（此前 daemon 自带一套匀速等高计划 = 独立实现，违反单一源）：`_browse_plan()` 用 `read_dwell_seconds/scroll_plan/wrap_up_seconds/pose` 生成计划，帖间间隔用 `action_gap('browse', base=GAP_RANGE)`（含 25-35% 长休）；深读停留上限 150s、浅读 25s（保原日 cap 可跑完）；每次浏览把 `pose` 写进 `browse_log.jsonl`（可回看真实停留/屏数）；humanize 缺席时回退常量计划（守护仍可跑）。
- 回帖：先读**最新楼**（`latest:` 取尾部）再打字；发送前 `humanize_typing` 模拟打字时长；写完有写作停顿。
- 点赞：读帖后有 `SKIP_AFTER_READ` 犹豫分支（不一定点），避免「见帖必赞」的机器特征。
- 发帖：先 `preface_` 闲逛几帖（禁「进站即发」），写作过程中有分段停顿再提交。
- 睡眠出口唯一：`_sleep()`，受 `LJQ_HUMANIZE_MAX_SLEEP`（默认 90s）截断 ⇒ 长休**永不挂住守护**；`dry` 或 `LJQ_HUMANIZE_OFF=1` 时一律不睡（自测/预览零等待）。
- 页面级合成事件（`PAGE_EVENTS_JS` = 真滚动 + 滚轮 + 同级读焦点 `mousemove` + 合成 `keydown`）**已写死关闭**（`PAGE_EVENTS_ENABLED = False`，源码常量，2026-10-05 用户裁决）：JS 注入事件 `isTrusted=false`，对校验信任位的采集端无效、还会凭空多出可疑事件；`--page-events` / `LJQ_PAGE_EVENTS_ON` 保留为兼容参数但**已失效**（负控 T50c：置 env=1 也打不开）。若将来要做「可信输入」，唯一可行路径是 **CDP `Input.dispatchMouseEvent`**（实测 `isTrusted=true`，见 `temp/ljq_cdptest/FINDINGS.md`），但那属另一层实现，不在 `--page-events` 里。
- 业界对照与缺口清单见 `temp/ljq_anti_detection_survey.md`（TLS/浏览器指纹/IP 声誉由「走真实 Chrome + 家宽」天然覆盖）。

## 八、自测与验收

- `python3 tools/linuxdo_humanize.py --selftest`（25/25）。
- `python3 tools/linuxdo_antidetect.py --selftest`（**65/65**，含 T27 跨层自洽；2026-10-06 R548 新增 T23b2 上午窗断言与段闸门解包用例；2026-10-09 R565 新增 T28/T28b–T28e 一组「窗口优先」回归负控：TTL 让位于窗口、抽点恒不越窗、`due` 与 `next_wait` 同口径）。
- `python3 tools/linuxdo_acts.py --selftest`（**65/65**；含锁 T35–T40、拟人接线 T41–T47、页面事件 T48–T50d、deai 回帖闸门；自测把 state/lock/通知全部重定向到沙箱）。
- `python3 tools/linuxdo_deai.py selftest`（21 checks / 0 failed；★子命令是**位置参数**，写 `--selftest` 报 unrecognized）。
- `python3 tools/linuxdo_post_forge.py selftest`（21 checks / 0 failed；同上位置参数）。
- `python3 tools/linuxdo_browse_daemon.py --selftest`（**36/36**；R556 补 `io` import 后新增 T7o/T7p/T7q 补标签限额/EXISTS 去重/短退避边界；含风控/休息日/LOCK_BUSY 用例 + 拟人计划接线 + 等级路由 T6c6–T6c9：earn 有草稿排 post / 无草稿绝不排 post / 无缺项不排 / 多缺项并行；2026-10-06 R546 新增 T15/T15b/T15c 段表用例：同日段表确定性、重启（清缓存）后段位不变、段表结构无乱序/重叠/零长；T18 段级审计视图用例，含「非 DONE / 坏时间戳 / 昨天」行不得计入、空台账零覆盖、缺台账零覆盖、内核缺席返 note+null 四组负控）。
- `python3 tools/linuxdo_daemon_watchd.py --selftest`（**98 checks / 0 failed**，零站点流量；R546 新增冷启动/多源心跳/环境忙不重启用例；T27 重启审计视图用例（含缺台账零计数+note、全坏行零计数两组负控）；R555 新增桥探活 T29 系列——桥不可达⇒`BRIDGE_DOWN` **不重启**（T29a 有守护/T29b 无守护两种组合）、告警同类 6h 合并（T30 窗口内 5 次⇒1 条且带累计）、恢复闸 ≥300s（T31 停机 100s⇒不发 / 1000s⇒发），以及 `sandbox_state` 助手 + T32 负控：自测前后断言生产 `watchd.jsonl`/`runtime` 指纹**逐字不变**；R556 新增独立桥自愈 T33 系列——桥不可达⇒`ensure_bridge` 自动拉起 `tools/tmwd_bridge_watch.py`（冷却 600s/等 15s/START_FAILED 三态负控）；R558 新增休眠自声明 T34 系列——daemon 心跳自报 `next_wake_ts` 在未来（≤12h）⇒ 判 `OK / RESTING_UNTIL_<phase>_<min>m` 且**不重启不自愈**、心跳已过期 ⇒ 回落真 `RESTART`、`next_wake_ts` 超 12h ⇒ 当坏值回落旧判据、`phase=loop`（`next_wake_ts=null`）⇒ 不影响；T35 断言重启前先 `stop_daemon`（`bg_task stop --name linuxdo_browse`）再 spawn，且 stop 失败（rc=3）不阻断 spawn）。
- `python3 tools/sche_tasks_lint.py --only linuxdo_trust_level_check` ⇒ FAIL 0（四个动作任务已删；该器会扫全量 `sche_tasks/`）。
- 判活：`python3 tools/linuxdo_daemon_watchd.py status`（watchd/daemon 两 pid + last_browse）；`temp/linuxdo_browse_daemon/acts_log.jsonl` 新增行、`state.daily` 增长（`bg_task` 日志 0 字节 ≠ 没跑）。

## 九、浏览守护看门狗（进程级自愈，零站点流量）

问题：`linuxdo_browse_daemon.py` 是长驻守护（承担浏览 + 等级前置 + 缺项路由），进程一死只能等下一个定时入口（09:30 / 12:40）才被拉起 ⇒ 最长空窗约 21h，期间浏览/等级/排队动作全停摆，且**用户看不见**（静默失效）。

解决：`tools/linuxdo_daemon_watchd.py`（零站点请求、零 LLM token、只读本地进程表与日志）：

- 子命令：`ensure`（兜底入口，已在跑则原样返回）/ `run [--max-hours 26]` / `status` / `--selftest`（90 checks / 0 failed 绿）。
- 四件事：① **桥探活（R555 新增，最前置）**——`bridge_ok()` 探 `127.0.0.1:18765`（TMWebDriver 桥）；不可达 ⇒ 先 `ensure_bridge()` **自动拉起独立桥**（`tools/tmwd_bridge_watch.py`，脱离 Galley 座舱，R556；冷却 600s），仍不可达才判 `BRIDGE_DOWN`，**只告警、绝不重启**（桥不在时 daemon 起来也取不到会话：典型是起来即退=被误判「死亡」、或空转=被误判「停滞」，两者都只会变成「重启→再死」的无限循环，今日实测 64 次即此形态）；② 探活——守护进程不在（且桥通）⇒ 用 `tools/bg_task.py start --name linuxdo_browse -- python3 tools/linuxdo_browse_daemon.py watch` 拉起（幂等）；③ 停滞——活动窗内超过 `STALL_S`（3h）**无任何心跳** ⇒ 判卡死并重启；④ 留痕 + 告警——事件写 `temp/linuxdo_watch/watchd.jsonl`，重启/恢复各发一次飞书私信（冷却 `NOTIFY_COOLDOWN_H`=6h，连续重启失败 `FAIL_STREAK_NOTIFY`=3 次再告警并继续重试；R555：**同类告警 6h 合并**——同一原因窗口内只发一条、文案带累计次数，避免每 6 分钟一次私信刷屏）。  〔判据方向不可反〕桥不可达时**不重启**是刻意的：重启解决不了外部前置，只会白搭冷启动并制造日志噪声。
- ★**心跳判据是「多源取最大」而非只看 `browse_log`**（R546 修复，2026-10-06）：`last_progress_ts()` = max(`browse_log.jsonl` 末条 DONE, `state.json` mtime, `level_log.jsonl` mtime, **daemon 进程启动时刻**)。理由=① `browse_log` 只在**整帖跑完**才追加一行，而单帖含深读驻留 + 长驻留 JS（单次 `timeout` ≤215s）⇒ 批内静默 3–4 分钟属**正常**；② 冷启动时 `browse_log` 末条可能是**上一次会话**留下的（实测 00:15 的旧末条距 11:14 已 11h）⇒ 只看它会把**刚起 1 分钟的新进程**当卡死再杀一次，正是「重启风暴」的根因（实测 9h20m 内重启 35 次，`STALLED_38069s` 与日志时间差逐秒相符）。取进程启动时刻即天然获得**冷启动宽限期**（新进程在 `STALL_S` 内永不判停滞）。
- ★**休眠自声明：`heartbeat.json` 的 `next_wake_ts`（R558，2026-10-07）**：daemon 每轮写 `temp/linuxdo_browse_daemon/heartbeat.json`（`ts`/`epoch`/`pid`/`phase`/`next_wake_ts`），循环顶 `phase=loop` 且 `next_wake_ts=null`，每次 `_nap(sec)` **睡前**写 `next_wake_ts=now+sec` ⇒ 看门狗据此区分「**有意长睡**」与「**真卡死**」：`next_wake_ts` 在未来且 ≤ `RESTING_MAX_S`(12h) ⇒ 判 `OK / RESTING_UNTIL_<phase>_<min>m`，**不重启**（这正是 2026-10-07 07:41 那条「重启失败 1 次（STALLED_26645s）」假告警的根因：静默窗里 daemon 睡着、文件不更新，看门狗只认 mtime 就当卡死，而 bg_task 见同名任务在跑 ⇒ rc=4 拒绝启动 ⇒ 被判「拉起失败」）。`next_wake_ts` 已过期或超 12h（坏值）⇒ 一律回落旧停滞判据；★该分支**只在 daemon 进程还活着时生效**（`decide()` 里 `not pids ⇒ DAEMON_DEAD` 在前）⇒ 真死照样立即重启。
- ★**重启前先停同名任务（R558）**：`one_round` 判 `RESTART` 且 `pids` 非空 ⇒ 先 `stop_daemon()`（`bg_task stop --name linuxdo_browse`）再 `spawn()`，否则 bg_task 见「任务已在运行」直接 rc=4 拒启动（保护机制，非故障）；stop 失败（rc=3）不阻断 spawn。
- ★**判停滞 ≠ 一律重启**：若停滞的成因是**外部环境不可用**（`browse_busy_hint()` 在 `BUSY_LOG_S`=20min 窗口内读到 daemon 自报的 `RC_NO_SESSION_RETRY`（无 linux.do 会话）/`前台被占`（浏览器被用户占用））⇒ 判 `BLOCKED_ENV`，只留痕 + 告警，**不重启**（重启解决不了，还白搭一次冷启动）。区分目的是「进程死了」与「世界不给干活」。
- 节奏参数（`--interval` 类）只影响**巡检**频率（`CHECK_INTERVAL`=60s），**绝不改动**日限/静默窗/休息日/段间隔——那些仍由 `linuxdo_antidetect` 内核独占决定；本器只重启进程，不重排行为。
- 防抖：两次重启最小间隔 `RESTART_MIN_GAP`=300s；单实例锁 `temp/linuxdo_watch/watchd.lock`。
- 长驻上限 `DEFAULT_MAX_HOURS`=26h：**由每日入口重新武装**（`sche_tasks/linuxdo_trust_level_check.json` 步骤 0 调 `ensure`；2026-10-05 前挂在 `linuxdo_acts_browse.json`，该文件已删除），与 anyrouter watchd 同模式——避免"永久僵尸守护"。
- 判活（人看）：`python3 tools/linuxdo_daemon_watchd.py status` 应同时给出 watchd 与 daemon 两个 pid、`last_browse` 时间；`browse_log` 新增 DONE 或 `state.daily` 增长才是守护真在干活（`bg_task` 日志 0 字节 ≠ 没跑）。
- ★铁律：自测必须走沙箱（T16 断言自测前后**本器三件自有产物**指纹逐字相同），**禁绑现场内容**（曾因把 daemon 持续写入的 `browse_log.jsonl` 纳入生产指纹 ⇒ 恒假红）。

### 9.1 两个只读审计视图（R547，2026-10-06）

R546 修好了「重启风暴」与「长休不可达」，但事后**看不见**两件事：今天到底按段跑了几段、长休有没有发生；以及守护到底重启过几次、最近一次为什么。这两件都是纯只读视图，**不改任何节奏参数、不发站点请求**：

- `python3 tools/linuxdo_browse_daemon.py status` 新增 `segments` 块（字段名以实测输出为准）：`theoretical`（今天理论几段）/`current_index`（现在第几段，1-based；段间空档为 `null`）/`current_window`（`[起HH:MM, 止HH:MM, 段长分钟]`）/`next_segment`（空档时下一段起点）/`browsed_in_segment`（本段内已浏览帖数）/`segments_seen` 与 `segments_seen_idx`（今天**实际**进过的段数/段号，与 `theoretical` 对照即知节律是否真按段走）/`rests_n`·`rests_long_n`·`rests[{from,to,minutes,long}]`（相邻帖间隔 ≥15min 的休息，`long=true` 即真长休 `SESSION_REST`）。判据=段位由内核 `day_segments(day, kind)` 日期种子派生 ⇒ **同日重启后段位不变**（视图里能直接看出「重启没重置节律」）。
- `python3 tools/linuxdo_daemon_watchd.py status` 新增 `restarts` 块：`restarts_total`/`restarts_today`/`restarts_ok`/`restarts_failed`/`restarts_last_hour`（近一小时，对照上限 `rate_limit_per_hour`）/`last_restart{ts,reason,ok}`（最近一次的**时间与原因**，如 `STALLED_39527s`）/`last_start`；R555 另加 `bridge_reachable`（`127.0.0.1:18765` 是否可达——**判「今天的重启是环境问题还是本器问题」的第一眼字段**：`bridge_reachable=false` 且 `last_restart.reason=DAEMON_DEAD/STALLED` ⇒ 属外部前置，不是守护误杀）。R556 另加 `bridge_runner_pids`（独立桥监督进程 pid 列表）/`bridge_ensure{ts,cooldown_s}`（起桥冷却状态）；判据=逐条重算自 `temp/linuxdo_watch/watchd.jsonl`，**不依赖运行期计数器** ⇒ 守护重启后历史计数不丢。
- ★口径两条：①段级视图只统计**当日**且只认 `DONE` 行的**真实时间戳**（非 DONE / 坏时间戳 / 昨天的行一律不计）；②重启视图里的 `restarts_ok`（真的把子进程拉起来）与 `restarts_failed`（如 `HOLD_BUSY` 让位）分列——`restarts_failed` 是**判据档**不是「异常档」：重启风暴期的 41/4 恰是风暴证据（R545 缺陷①的量化面），修好之后该值应停止增长。
- ★负控：缺台账/空台账 ⇒ 计数 0 且带 `note`，绝不伪造；内核缺席 ⇒ 段级视图返 `null`+note（自测 T18 四组、watchd T27 两组）。

## 十、历史坑（禁再犯）

1. **自测写生产状态**：`linuxdo_browse_daemon.py --selftest` 曾把生产 `state.json` 覆盖成仅 `rest_days`（66B）⇒ 守护 `KeyError: 'daily'` 无声退出，表象像「站点封号」。修法：`_selftest_impl` 走 tempfile 沙箱 + 断言自测前后生产 state 指纹逐字相同。
2. **等级检查后不自还原标签页**：把唯一 linux.do 标签导航到 connect 后不返回 ⇒ 下一轮 daemon 无供应、`done=0`，曾被误判为 CF 封禁。修法：读完 connect 立刻 `location.href='https://linux.do/'` + 等 6s。
3. **探针/监控与主账号同身份**：本案（anyrouter）探针 key = 用户 Pi 同一账号 key，3 天 1244 发全记主账号。**监控动作必须独立身份**（见 `anti_detection_sop.md`）。
4. **合成事件当成真人事件 / 开关本身是坏设计**：JS 注入的 `mousemove/keydown` 带 `isTrusted=false`，采集端一行即可辨认 ⇒ 注入不是「更像人」而是「多一个可疑信号」。**2026-10-05 用户裁决：写死关**（`PAGE_EVENTS_ENABLED = False` 源码常量），`--page-events` 与 `LJQ_PAGE_EVENTS_ON` 保留但已失效（负控：置 env=1 也打不开）。★教训=「默认关 + 开关」等于把风险留给未来的自己，凡是己方认定不该开的能力，直接删掉开关而不是留后门。
5. **固定节奏**：10-04 0–13 点恒定 300s 一轮，随机化滞后 1.5 天 ⇒ 封号后补的随机化属「事后」，规范要求**从第一版起**就随机。
6. **「定点唤醒动作」=架构级错误**（2026-10-05 用户纠正）：曾给浏览/回帖/点赞/发帖各建一个 `sche_tasks` 定点任务（12:40/11:20/15:40/20:10），虽写成「只查 due 不真动手」，但① 与「等级检测为前置路由」的设计冲突（动作该由缺项驱动，不是由时钟驱动）；② 一旦 `due` 判定有偏差或随机内核失效，就退化为「每天固定时刻发请求」——正是被封的特征。**四个任务已删除**，动作只有「等级路由排队 → daemon 随机消费」与「用户提示词临时触发」两条路。★判据：任何新加的 L 站入口，若它在固定时刻可能发出站点请求，即违规。
7. **段级节律只活在进程内存里 ⇒ 一重启就重排**（R546 修复，2026-10-06）：daemon 原先用**进程内变量**记录「今天第几段、段何时结束」，守护被杀重启后段位从头算 ⇒ `SESSION_REST`（110min 段满才休）几乎**永不可能达成**（实测 10-05 全天 102 帖/21.05h 里间隔 ≥25min 仅 2 处，正是「重启风暴把长休掐掉」的证据）。修法：当日活动段改由内核**按日期种子确定性派生**（`linuxdo_antidetect.day_segments(day, kind)`），段间空档靠 `wait_hint()` 等待 ⇒ 重启不重置段位，`T15` 断言「同日段表两次取值逐字相同」、`T15b` 断言段表无乱序/重叠/零长。
8. **同一份常量在两个文件里各写一份 = 死常量**（R546）：旧 daemon 自带 `GAP_LONG_RANGE/GAP_LONG_P` 常量副本，只在自测断言里被引用，真实节奏走的是另一套 ⇒「五动作共用同一随机内核」名存实亡。修法：帖间/批间间隔一律走内核 `gap_seconds('browse')`，`_batch_gap()` 只在内核缺席时回落（回落值同为内核镜像）。★判据：凡「单一源」承诺，必须能用 grep 证明生产路径确实调到该源，自测里的常量不算。
9. **台账被高频分支污染 ⇒ 频次不可取证**（R546）：等级检查的**节流分支**（每 301s 走到一次）照样写 `level_log.jsonl` 并把 `st["level"].ts` 刷成当前时刻 ⇒ 台账里塞满 `throttled` 记录、无法据此判断「上次真检查是何时」。修法：节流分支只打印日志，**不写台账、不刷 `level.ts`**；真检查记录（含 `source`）才落 `level_log`。
10. **改判据必须同批回填文档数字**：SOP/规格里内嵌的「工具真跑输出」（`selftest 25/25` 之类）会随实现变化失配 ⇒ R546 把 §八 的 daemon `28/28`→`32/32`、watchd `25/25`→`52 checks`；R547 做 #5/#7 只读视图后同批复填 daemon `32/32`→`33/33`、watchd `52`→`60 checks`。

11. **`if not tup:` 元组恒真 ⇒ 段闸门静默失效（P0，2026-10-06 R548）**：内核 `linuxdo_antidetect.in_segment()` 返回 `(in_seg, idx)` 元组，daemon 调用处写成 `if not in_segment(day, "browse"): 等空档` ⇒ 元组永远为真值、判断永走同一支 ⇒ **「段间空档」从未生效**，实际是连续浏览（10-05 全天 102 帖 / 21.05h、帖间隔中位 404s 即铁证；R545 把同一现象只归因于「重启风暴摧毁节律」，实为两因并存）。修法=daemon 新增 `maybe_segment_gate()` 显式解包 `in_seg, idx = ...`。★判据：单测要断言「空档期真的返回等待」，而不是只断言函数返回类型/真假。

12. **自测沙箱必须覆盖所有调用入口（同一事故二次发生，2026-10-06 R548）**：`--selftest` 走 CLI 时会把 state/lock/通知重定向到沙箱，但 `_selftest_impl()` 被**直接调用**（本会话为定位 T6c7 而跑 `python3 -c "import ...; _selftest_impl()"`）时无沙箱 ⇒ 12:50 把生产 `temp/linuxdo_browse_daemon/state.json` 覆盖成 66B 空态、并把 `browse_log.jsonl` 截断到 5 行测试垃圾（**真实历史行不可重建**，只剩事故后新写的 DONE）。修法=把沙箱**下沉进 `_selftest_impl`**（幂等，CLI 与直调同安全）；恢复源=`state.json.bak-*`（daemon 每轮自备份，实测 `bak-20261006-122955` 3106B 为 11:52 完整真态，用它精确回填 daily/seen 并打 `_recovered` 标记）。★铁律延续 R545：「自测/金丝雀禁写生产状态」**不能只在 CLI 层挡**。

13. **「进程不健康」与「外部前置不可用」混为一谈 ⇒ 无限重启循环（R555，2026-10-06）**：看门狗只看「进程在不在 + 心跳新不新」，而 TMWebDriver 桥（`127.0.0.1:18765`）缺席时，daemon 要么起来即退（判 `DAEMON_DEAD`）、要么空转（判 `STALLED`）⇒ 重启解决不了任何事，只会每 6 分钟一次：实测今日 `RESTART` 事件 **64 次全同源=桥缺席**（`STALLED_*` 37 条 07:31→11:14 每 360s 且 `ok=false`，`DAEMON_DEAD` 27 条），且 6h 冷却被打成每 6 分钟一条私信。修法=① 重启前先探桥，不可达即 `BRIDGE_DOWN` 只告警不重启；② 同类告警在冷却窗内合并（带累计）；③ 恢复通知加「停机 ≥300s」闸（<5min 属抖动，不发）。★判据：**修「重启风暴」先分清是「进程真掉」还是「环境不给干活」**——两者日志形态相同，唯桥探活能区分。

14. **演练/自测的沙箱必须覆盖「所有输出常量」，不能按名字猜（R555）**：首轮八项沙盒演练用真函数 `one_round()`，脚本把 `LOG`/`STATE`/`RUNTIME` 都重定向了，唯独漏了 `EVENTS`（事件台账 `temp/linuxdo_watch/watchd.jsonl`——**名字里没有 `LOG`**，旧白名单没覆盖）⇒ 20 行假事件真的写进了**生产**台账（全部落在 `18:17:03` 同秒）。处置=按 append-only 原则追加三条 `ANNOTATION` 行如实标注（不改历史行），并把 `sandbox_state()` 助手固化进工具（重定向 EVENTS/RUNTIME/LOCK）+ T32 负控（自测前后断言生产 jsonl/runtime 指纹逐字不变）。★判据：演练脚本落盘后先自证「生产文件指纹未变」再继续；**同名不同义的常量（日志 vs 台账）是漏点高发区**。

15. **吞异常掩盖缺 import ⇒ 限额静默失效（R556，2026-10-06）**：`tools/linuxdo_browse_daemon.py` 的 `try_ensure_tab` 用 `io.open` 读写台账，但模块从未 `import io` ⇒ 每次抛 NameError 被 `except` 吞 ⇒ **补标签限额从未生效**（Chrome 关掉时会无限堆新标签）。修=补 `import io` + 用例 T7o（限额）/T7p（EXISTS 去重）/T7q（短退避边界），断言台账被真写。★判据：`except` 兜底的路径必须先证明「正常路径能走通」，且台账真被写入；「无报错」≠「生效」。

16. **「有意长睡」被当成「卡死」⇒ 假重启告警（P0，2026-10-07 R558）**：静默窗（01:00–07:30）里 daemon 主动长睡，`state.json` / `browse_log.jsonl` 自然停更 ⇒ 看门狗按 `age > STALL_S(3h)` 判 `STALLED`，而 `bg_task` 见同名任务仍在跑 ⇒ `rc=4` 拒启动 ⇒ 被判「已尝试拉起但未成功」并累计到阈值 ⇒ 07:41 发出「重启失败 1 次（STALLED_26645s）」假告警（同一现象 R546 只归因「进度源不够多」，仍未解）。修法=daemon 写**休眠自声明** `heartbeat.json`（`phase` + `next_wake_ts`，每次 `_nap()` 睡前登记），看门狗新增 `RESTING_UNTIL_<phase>_<min>m` 分支：只要 `next_wake_ts` 在未来（且 ≤12h）就判 OK 且**不重启**；`next_wake_ts` 已过期或超 12h（坏值）⇒ 回落旧判据；daemon 真死（`pids` 空）⇒ 仍走 `DAEMON_DEAD` 立即重启。★判据：**只要监控方把「多久没写文件」当作唯一存活信号，就必须先问「它有没有可能是在合法地睡」**——凡是设计上会主动长睡（静默窗、休息日、退避）的进程，都要有一条「我在睡，几点醒」的自声明通路，否则自愈机制必然变成告警噪音源。★副作用坑：`RESTART` 且 `pids` 非空时若不先 `bg_task stop` 就直接 `spawn`，会因「任务已在运行」被 `rc=4` 拒掉（保护机制非故障），故 `one_round` 改为**先停同名任务再拉起**（stop 失败 rc=3 不阻断）。

17. **滑动 TTL 会把抽点「顶出」硬窗口 ⇒ 出现「昨天查过、今天没查」（R565，2026-10-09）**：等级检查原按 `last + TTL(20h)` 决定下次抽点，而允许动手的窗口是**上午 08:00–10:00**——昨天 09:50 抽过，20h 后落在次日 05:50（静默窗内），一推再推就滑出上午窗，叠加「当日不补查」⇒ 整天没查；同时 `next_wait` 与 `due` 各自算锚点，现场出现「内核判未到期(ttl_16h) 但 next_wait 说 due_now」的自相矛盾。修法（用户批准方案 B）：**窗口优先、TTL 让位**——TTL 降至 6h 只防「近邻重复」（重启/多入口/同一上午第二次）；`level_check_ts()` 抽点**恒不越窗**（`last + TTL` 落窗外 ⇒ 改取窗内抽点；已有整窗覆盖时取离上次更远端）；`due` 与 `next_wait` 同锚同分支，永不互斥。★判据：**「多久后到期」与「什么时候允许动手」是两件事**——两者并存的调度，到期时刻必须先被窗口裁剪（clamp）再输出，否则「未到期」与「现在可以」同时为真，谁也说不清该不该做。★回归负控须用**合成状态**直测 TTL 分支（用真实时刻会被静默窗/已到期分支提前拦截，负控形同虚设）。

## 十一、发帖锻造 + 去AI味闸门（2026-10-06 新增，用户要求「发帖要去AI味儿，要像人写的、人想的」）

### 11.1 去AI味机械闸门（单一源 `tools/linuxdo_deai.py`）

判据（`check`，rc 0 过 / 1 不过 / 2 输入错），三类硬伤 + 一类软分：

- **HARD-模板腔**：`首先/其次/最后`、`综上所述`、`值得注意的是`、`1. 2. 3.` 分点罗列、`希望对你有所帮助`、`作为AI`、`让我们`、`总之` 等（模板短语表单一源在脚本常量，禁止在别处再抄一份）。
- **HARD-结构腔**：Markdown 小标题（`##`）、加粗小标题成段、`- ` 列表项成段（真人回帖基本不用）。
- **HARD-空话**：`具有一定的意义`、`是一个重要的` 这类无信息量套话。
- **SOFT-score**：长度自适应的「人味分」（短句阈值与长文不同，避免把 10 字真人短句误杀）；低于 `min_score` 判不过。

用法：`python3 tools/linuxdo_deai.py check --text "..."`（也吃 `--file`）。

### 11.2 回帖也过闸门

`tools/linuxdo_acts.py` 的 `validate_reply()` 已接 `deai`：**回帖文案不过闸门就拒发**（这是发帖之外唯一会带文案的动作 ⇒ 两处文案同一判据）。被拒时返回失败原因，不做「降级发送」。

### 11.3 草稿锻造器 `tools/linuxdo_post_forge.py`

设计目标：**没有草稿时不硬凑文案**，而是用真实素材 + 人味生成一条可发的帖。

```bash
python3 tools/linuxdo_post_forge.py list                    # 只读
python3 tools/linuxdo_post_forge.py forge --dry-run          # 只打印不落盘
python3 tools/linuxdo_post_forge.py forge --notify           # 落草稿 + 私信
```

- **选题来自真实素材**：`pick_material()` 从本机既有产物（如 `temp/anr_codex/real_body.json` 等真实正文/日志）随机取一条，用内容 sha 前若干位做 material id 去重（`exclude_ids`）⇒ 同一素材不会重复起稿。
- **生成走本机 codex CLI**（`exec`，无网络请求；脚本内不发任何 HTTP，可离线跑）⇒ 提示词写死「像人写的、人想说的」，并要求**不用分点、不用小标题、不客套**。
- **落盘前必过 deai 闸门**：不过闸门就重试/放弃，**绝不把 AI 味文案写进草稿**。
- **草稿目录 + 台账**：草稿落在 `temp/linuxdo_post_drafts/`（含 provenance：素材路径 + material_sha + deai 分），已用草稿由 `post` 成功后 rename 成 `used_*`（带原始 id，可回溯）。
- **实测（2026-10-06 首次真跑，`--dry-run`）**：rc=0，产出文案「有 JavaScript 环境，却没有 Node 和网络」，deai `score=100` / `warn=[]`，`draft_path=null`（dry-run 不落盘）⇒ 链路真通。

### 11.4 自动锻造（daemon 低频补稿）

`tools/linuxdo_browse_daemon.py` 在等级路由的 `post` 分支遇到 `SKIP:NO_DRAFT` 时，调用 `_maybe_forge_draft()`：

- **冷却闸门 `_forge_gate()`**（默认按天计，冷却未到返回 `SKIP:COOLDOWN(<x>d)`）；`--apply` 为假时返回 `SKIP:DRY_RUN`（自测零副作用）。
- 真锻造走 `subprocess` 后台起 `linuxdo_post_forge.py forge --notify`，stdout 落 `temp/linuxdo_post_forge/forge.log` ⇒ **不阻塞守护主循环**。
- 成功/失败只影响「本轮能否排 post」，**不影响其他缺项**（reply/like/browse 照常并行）。

### 11.5 与其他节点的关系

- 「等级检测 → 缺项路由 → 五动作」链条不变（§一/§四）；本节只补「**produce 一份人味草稿**」这一环。
- 锻造是**低频**行为（日级冷却）⇒ 仍满足「任何自动化流量禁固定时刻 + 固定间隔」的铁律：锻造时刻由 daemon 的随机段决定，没有独立定时任务。

### 11.6 选题灵感来源：浏览过的帖子（2026-10-06 新增）

- **实现**：`browse_inspiration()`（`tools/linuxdo_post_forge.py`）复用对象偏好模块的 `read_browse_titles()`，从**浏览台账**（默认路径 `temp/linuxdo_browse_daemon/browse_log.jsonl` = `linuxdo_prefs.BROWSE_LOG_DEFAULT`，勿与旧名混写）取最近 **72h 内浏览过的帖子标题（≤12 条）**，作为 `{inspire}` 段注入选题 prompt（`TOPIC_PROMPT`）。
- **零站点**：只读本地 jsonl，不发任何请求；台账缺失/不可读一律 `("", 0)` 且 forge 照常跑（灵感只是加分项，绝不因它失败）。
- **★边界（防洗稿）**：浏览标题**只作气口/切口参考，不作素材本体**——正文仍必须来自真实素材正文（`provenance.material` + `material_sha` 不变），prompt 里已明文写「可以顺着这个气口选切口，但不是抄别人的帖子，内容仍必须来自上面那段记录；蹭不上就别硬蹭」。
- **可追溯**：草稿 `provenance` 新增 `inspire_n`（当次注入的灵感条数；0 = 未用灵感），与 `material_reused` 并列。`--no-inspire` 可关，默认开。
- **实测（2026-10-06）**：读真台账得 12 条（形如「测了下重置后日区 pro 的额度（20）」）；`selftest` **28 checks / 0 failed**（T21–T27 用 sentinel 探针断言「灵感段只在 inspire=True 时入 prompt」+ 读草稿文件断言 `inspire_n` 1/0）。

### 11.7 回访自己帖（revisit）的接线口径（2026-10-06 定案）

- **能力已存在**：`tools/linuxdo_acts.py act_revisit()` 回访自己发过的帖、只读**新楼层**（进度记 `state.revisit[str(tid)].last_num`，幂等；`revisit_due()` 守最小间隔），默认 dry-run，`--apply` 才真动。
- **触发面与五动作一致**：①提示词临时触发（`python3 tools/linuxdo_acts.py revisit --max 1`）；②daemon 随机段内消费。**不新增任何独立定时任务**（守 §一「禁定点唤醒」铁律）。
- **不做无条件周期回访**：不给 daemon 加「每隔 N 小时必回访一次」的循环——那等于给站点制造新的规律流量。**发帖后也不会自动标记回访**（`act_post()` 不写 `revisit.last_ts`，避免「一发帖就必然产生一批回访流量」这种可预测耦合）；想回访时显式 `python3 tools/linuxdo_acts.py revisit --max 1 --now --apply`（`--now` 把 `revisit.last_ts` 置 0 以越过 6h 最小间隔），否则就等下次有需要时再读。
- 结论：revisit 属「有需要才用」的备用动作，不进入等级缺项路由（等级指标不含回访），不改变 §一 的分流表。

## 尾注
- 站点侧浏览器操作细则（标签页/CF/登录态）见 `memory/linuxdo_browse_sop.md`、`memory/linuxdo_reply_sop.md`、`memory/linuxdo_trust_level_sop.md`。
- 对外分享镜像（更新 2026-10-05）：OB 知识库已建页 `60 Wiki/topics/linuxdo-anti-detection-system.md`（结构化条目，mature），方案全文备份在 `30 Areas/research/linuxdo/2026-10-05 linux.do 全动作防检测体系（方案全文）.md`；vault 提交 `5efb5e2`（vault=`~/develop/obsidian-note`，无远端只本地 commit）。改本 SOP 涉及的机制/参数后，须同批回填该 OB 页与全文备份，避免两处口径漂移。
