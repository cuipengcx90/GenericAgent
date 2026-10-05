# L站五动作统一防检测 SOP（等级前置 × 随机内核 × 互斥锁）

> 建立 2026-10-05（anyrouter 封号事故复盘后，用户定义）。适用范围：linux.do 上一切**自动化动作**——等级检测 / 浏览 / 回帖 / 点赞 / 发帖。
> 铁律来源：`memory/anti_detection_sop.md`（独立身份 / 随机化从第一版起 / 指纹模拟 / 量级与可停）。
> 站点硬事实：**任何「固定时刻 + 固定间隔 + 稳定量级」的脚本化流量都是被清洗特征**（本次封号嫌疑第一）。

---

## 一、五动作与它们的层级关系

| 动作 | 角色 | 谁执行 | 触发方式 | 日量级（默认，见 `profiles`） |
|---|---|---|---|---|
| 等级检测 | **前置**（只读） | daemon 内 `check_level` | daemon 每天 1–3 次、间隔 5–11h 随机 | 零动作，仅读等级卡 |
| 浏览 | 补量（缺「已读帖子/浏览比例」时） | daemon `browse_batch` | daemon 自动，或提示词触发（拉起 daemon） | cap 140–180（日抖） |
| 回帖 | 补量（缺「回复话题数」时） | `tools/linuxdo_acts.py` reply | **等级路由排队**（daemon 随机消费）/ 提示词 | cap 6（±2 抖） |
| 点赞 | 补量（缺「获赞/送赞」时） | `tools/linuxdo_acts.py` like | 同上 | cap 12（±4 抖） |
| 发帖 | 补量（缺「获赞」且有草稿时） | `tools/linuxdo_acts.py` post | 等级路由排队（无草稿不排）/ 提示词 | cap 1 |

**路由规则（等级检测为前置，2026-10-05 订正版）**：
- **等级检测是唯一的前置入口**：只有它读站点等级卡，读出的缺项按类分流（一次可命中多项，**多缺项并行排队、互不阻塞**）。
- 分流表（`classify_pending` 五桶）：`browse` 类（已读帖子/浏览比例）→ daemon `browse_batch` **自补**；`reply` 类（回复话题数）→ 排 `pending_acts.reply`；`like` 类（送赞）→ 排 `pending_acts.like`；`earn` 类（获赞，站点无机器通道）→ **有草稿**则排 `pending_acts.post`（`post_src=level_route`），**无草稿**则退化为排 `reply`（多写多被赞）；`manual` 类（无法归类）→ 只发私信，脚本绝不代操作。
- 排队与执行**解耦**：等级检查那一刻只往 `state.pending_acts` 排队（`queue_pending_from_route`），真正动手由 daemon 在随机时刻取用（每轮每类最多 1 条），每类成败原因落 `acts_log.jsonl`（如 `SKIP:NO_DRAFT`）。
- 等级达标 ⇒ 不排队 ⇒ 不看帖、不回帖（用户原话：「正常的话不需要回帖，也不需要浏览帖子」）。
- ★**禁止定点唤醒动作**：「浏览/回帖/点赞/发帖」四个动作**没有**各自的定时任务（2026-10-05 用户纠正，四个 `sche_tasks/linuxdo_acts_*.json` 已删除）。它们的发生时刻只有两个来源：①等级路由排队后由 daemon 在随机段内消费；②用户提示词临时触发。任何「到点就动站点」的入口都属违规实现。

## 二、随机内核（单一源 `tools/linuxdo_antidetect.py`）

五个动作共用同一套随机化，禁止各写一套：

- **多段切分**：一天分 2–4 段，段长 6–25 分钟随机（`seg_n/seg_len`）。
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

1. **等级路由自动**（唯一常规路径）：daemon 内每天随机 1–3 次读等级卡 ⇒ 缺项按类**并行**排队 ⇒ 随机段内消费。等级达标则不排、不做任何动作。
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
- `python3 tools/linuxdo_antidetect.py --selftest`（56/56，含 T27 跨层自洽）。
- `python3 tools/linuxdo_acts.py --selftest`（52/52；含锁 T35–T40、拟人接线 T41–T47、页面事件 T48–T50d；自测把 state/lock/通知全部重定向到沙箱）。
- `python3 tools/linuxdo_browse_daemon.py --selftest`（28/28；含风控/休息日/LOCK_BUSY 用例 + 拟人计划接线 + 等级路由 T6c6–T6c9：earn 有草稿排 post / 无草稿绝不排 post / 无缺项不排 / 多缺项并行）。
- `python3 tools/linuxdo_daemon_watchd.py --selftest`（25/25，零站点流量）。
- `python3 tools/sche_tasks_lint.py --only linuxdo_trust_level_check` ⇒ FAIL 0（四个动作任务已删；该器会扫全量 `sche_tasks/`）。
- 判活：`python3 tools/linuxdo_daemon_watchd.py status`（watchd/daemon 两 pid + last_browse）；`temp/linuxdo_browse_daemon/acts_log.jsonl` 新增行、`state.daily` 增长（`bg_task` 日志 0 字节 ≠ 没跑）。

## 九、浏览守护看门狗（进程级自愈，零站点流量）

问题：`linuxdo_browse_daemon.py` 是长驻守护（承担浏览 + 等级前置 + 缺项路由），进程一死只能等下一个定时入口（09:30 / 12:40）才被拉起 ⇒ 最长空窗约 21h，期间浏览/等级/排队动作全停摆，且**用户看不见**（静默失效）。

解决：`tools/linuxdo_daemon_watchd.py`（零站点请求、零 LLM token、只读本地进程表与日志）：

- 子命令：`ensure`（兜底入口，已在跑则原样返回）/ `run [--max-hours 26]` / `status` / `--selftest`（25/25 绿）。
- 三件事：① 探活——守护进程不在 ⇒ 用 `tools/bg_task.py start --name linuxdo_browse -- python3 tools/linuxdo_browse_daemon.py watch` 拉起（幂等）；② 停滞——活动窗内超过 `STALL_S`（3h）无浏览产出（`browse_log.jsonl` 无新增 DONE）⇒ 判卡死并重启；③ 留痕 + 告警——事件写 `temp/linuxdo_watch/watchd.jsonl`，重启/恢复各发一次飞书私信（冷却 `NOTIFY_COOLDOWN_H`=6h，连续重启失败 `FAIL_STREAK_NOTIFY`=3 次再告警并继续重试）。
- 节奏参数（`--interval` 类）只影响**巡检**频率（`CHECK_INTERVAL`=60s），**绝不改动**日限/静默窗/休息日/段间隔——那些仍由 `linuxdo_antidetect` 内核独占决定；本器只重启进程，不重排行为。
- 防抖：两次重启最小间隔 `RESTART_MIN_GAP`=300s；单实例锁 `temp/linuxdo_watch/watchd.lock`。
- 长驻上限 `DEFAULT_MAX_HOURS`=26h：**由每日入口重新武装**（`sche_tasks/linuxdo_trust_level_check.json` 步骤 0 调 `ensure`；2026-10-05 前挂在 `linuxdo_acts_browse.json`，该文件已删除），与 anyrouter watchd 同模式——避免"永久僵尸守护"。
- 判活（人看）：`python3 tools/linuxdo_daemon_watchd.py status` 应同时给出 watchd 与 daemon 两个 pid、`last_browse` 时间；`browse_log` 新增 DONE 或 `state.daily` 增长才是守护真在干活（`bg_task` 日志 0 字节 ≠ 没跑）。
- ★铁律：自测必须走沙箱（T16 断言自测前后**本器三件自有产物**指纹逐字相同），**禁绑现场内容**（曾因把 daemon 持续写入的 `browse_log.jsonl` 纳入生产指纹 ⇒ 恒假红）。

## 十、历史坑（禁再犯）

1. **自测写生产状态**：`linuxdo_browse_daemon.py --selftest` 曾把生产 `state.json` 覆盖成仅 `rest_days`（66B）⇒ 守护 `KeyError: 'daily'` 无声退出，表象像「站点封号」。修法：`_selftest_impl` 走 tempfile 沙箱 + 断言自测前后生产 state 指纹逐字相同。
2. **等级检查后不自还原标签页**：把唯一 linux.do 标签导航到 connect 后不返回 ⇒ 下一轮 daemon 无供应、`done=0`，曾被误判为 CF 封禁。修法：读完 connect 立刻 `location.href='https://linux.do/'` + 等 6s。
3. **探针/监控与主账号同身份**：本案（anyrouter）探针 key = 用户 Pi 同一账号 key，3 天 1244 发全记主账号。**监控动作必须独立身份**（见 `anti_detection_sop.md`）。
4. **合成事件当成真人事件 / 开关本身是坏设计**：JS 注入的 `mousemove/keydown` 带 `isTrusted=false`，采集端一行即可辨认 ⇒ 注入不是「更像人」而是「多一个可疑信号」。**2026-10-05 用户裁决：写死关**（`PAGE_EVENTS_ENABLED = False` 源码常量），`--page-events` 与 `LJQ_PAGE_EVENTS_ON` 保留但已失效（负控：置 env=1 也打不开）。★教训=「默认关 + 开关」等于把风险留给未来的自己，凡是己方认定不该开的能力，直接删掉开关而不是留后门。
5. **固定节奏**：10-04 0–13 点恒定 300s 一轮，随机化滞后 1.5 天 ⇒ 封号后补的随机化属「事后」，规范要求**从第一版起**就随机。
6. **「定点唤醒动作」=架构级错误**（2026-10-05 用户纠正）：曾给浏览/回帖/点赞/发帖各建一个 `sche_tasks` 定点任务（12:40/11:20/15:40/20:10），虽写成「只查 due 不真动手」，但① 与「等级检测为前置路由」的设计冲突（动作该由缺项驱动，不是由时钟驱动）；② 一旦 `due` 判定有偏差或随机内核失效，就退化为「每天固定时刻发请求」——正是被封的特征。**四个任务已删除**，动作只有「等级路由排队 → daemon 随机消费」与「用户提示词临时触发」两条路。★判据：任何新加的 L 站入口，若它在固定时刻可能发出站点请求，即违规。

## 尾注
- 站点侧浏览器操作细则（标签页/CF/登录态）见 `memory/linuxdo_browse_sop.md`、`memory/linuxdo_reply_sop.md`、`memory/linuxdo_trust_level_sop.md`。
- 对外分享镜像（更新 2026-10-05）：OB 知识库已建页 `60 Wiki/topics/linuxdo-anti-detection-system.md`（结构化条目，mature），方案全文备份在 `30 Areas/research/linuxdo/2026-10-05 linux.do 全动作防检测体系（方案全文）.md`；vault 提交 `5efb5e2`（vault=`~/develop/obsidian-note`，无远端只本地 commit）。改本 SOP 涉及的机制/参数后，须同批回填该 OB 页与全文备份，避免两处口径漂移。
