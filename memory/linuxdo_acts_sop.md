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
| 回帖 | 补量（缺「回复话题数」时） | `tools/linuxdo_acts.py` reply | 提示词 / sche_tasks / 等级路由排队 | cap 6（±2 抖） |
| 点赞 | 补量（缺「获赞/送赞」时） | `tools/linuxdo_acts.py` like | 同上 | cap 12（±4 抖） |
| 发帖 | 主动产出（极低频） | `tools/linuxdo_acts.py` post | 仅人工/明确指令触发 | cap 1 |

**路由规则（等级检测为前置）**：
- 未达标项按类分流：浏览类 → daemon 自补；回复类 → 回帖执行器；送赞类 → 点赞执行器；无法归类 → 只通知人工（脚本绝不代操作）。
- 排队与执行**解耦**：等级检查那一刻只往 `state.pending_acts` 排队（`queue_pending_from_route`），真正动手由 daemon 在随机时刻取用（每轮最多各 1 条）。
- 等级达标 ⇒ 不排队 ⇒ 不看帖、不回帖（用户原话：「正常的话不需要回帖，也不需要浏览帖子」）。

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

## 四、触发面（三种，任选其一）

1. **提示词触发**（用户随口说「刷会儿帖子」「回两条」）：读 `memory/linuxdo_browse_sop.md` / `linuxdo_reply_sop.md`，或直接调用执行器。
2. **sche_tasks 定时**（各动作独立任务，见下）——任务只做「触发 + 查 due」，**不在固定时刻真的动手**：不在随机段内 `due=false` 就静默结束。
3. **等级路由自动**（daemon 内）——只在等级缺项时才发生。

已建定时任务（`sche_tasks/`，prompt 头均带 GA_ROOT 锚定行）：

| 文件 | 时刻 | 作用 |
|---|---|---|
| `linuxdo_trust_level_check.json` | 09:30 | 只读保活 + 兜底拉起 daemon（**零站点访问**） |
| `linuxdo_acts_browse.json` | 12:40 | 确保浏览守护在跑（浏览动作由 daemon 随机执行） |
| `linuxdo_acts_reply.json` | 11:20 | 回帖触发（due 判定 + `--apply`，后台起） |
| `linuxdo_acts_like.json` | 15:40 | 点赞触发 |
| `linuxdo_acts_post.json` | 20:10 | 发帖触发（日限 1，默认不 due） |

> 时刻本身固定不是问题——**动作发生的时刻**由随机内核决定；但如果某动作长期在固定时刻真的发出请求，视为内核失效，须查 `due` 输出。

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
- 长任务（单次可达数分钟）必须后台起：`python3 tools/bg_task.py start --name ldo_acts_reply -- python3 tools/linuxdo_acts.py run reply --apply`，随后 `bg_task.py status --name ...` 轮询（**禁 sleep 阻塞**，见 RULE 15）。

## 六、风控保险丝（不可绕过）

- 命中判据（HTTP/正文）：`BANNED`(403 封禁) / `RATE_LIMIT`(429) / `FORBIDDEN`(403) / `LOGIN_REQUIRED` ⇒ 写 `state.pause_until`（3600–7200s 随机）+ 飞书私信告警；同类 ≥3 次 ⇒ **当日停**。
- CF 挑战（Just a moment / turnstile）只轻量计数，不当作封禁中断。
- 遇到上述信号：**立即停手，禁重试、禁换 tab、禁「感觉还好」硬顶**（本次封号就是反面教材）。

## 七、自测与验收

- `python3 tools/linuxdo_acts.py --selftest`（含锁用例 T35–T40；自测把 state/lock/通知全部重定向到沙箱）。
- `python3 tools/linuxdo_browse_daemon.py --selftest`（含风控/休息日/LOCK_BUSY 用例）。
- `python3 tools/sche_tasks_lint.py --only linuxdo_acts_reply,linuxdo_acts_like,linuxdo_acts_post,linuxdo_acts_browse` ⇒ FAIL 0。
- 判活：`temp/linuxdo_browse_daemon/acts_log.jsonl` 新增行、`state.daily` 增长（`bg_task` 日志 0 字节 ≠ 没跑）。

## 八、历史坑（禁再犯）

1. **自测写生产状态**：`linuxdo_browse_daemon.py --selftest` 曾把生产 `state.json` 覆盖成仅 `rest_days`（66B）⇒ 守护 `KeyError: 'daily'` 无声退出，表象像「站点封号」。修法：`_selftest_impl` 走 tempfile 沙箱 + 断言自测前后生产 state 指纹逐字相同。
2. **等级检查后不自还原标签页**：把唯一 linux.do 标签导航到 connect 后不返回 ⇒ 下一轮 daemon 无供应、`done=0`，曾被误判为 CF 封禁。修法：读完 connect 立刻 `location.href='https://linux.do/'` + 等 6s。
3. **探针/监控与主账号同身份**：本案（anyrouter）探针 key = 用户 Pi 同一账号 key，3 天 1244 发全记主账号。**监控动作必须独立身份**（见 `anti_detection_sop.md`）。
4. **固定节奏**：10-04 0–13 点恒定 300s 一轮，随机化滞后 1.5 天 ⇒ 封号后补的随机化属「事后」，规范要求**从第一版起**就随机。

## 尾注
- 站点侧浏览器操作细则（标签页/CF/登录态）见 `memory/linuxdo_browse_sop.md`、`memory/linuxdo_reply_sop.md`、`memory/linuxdo_trust_level_sop.md`。
- 对外分享镜像：暂未镜像到 OB；如需对外，按 `60 Wiki/topics/` 格式建页。
