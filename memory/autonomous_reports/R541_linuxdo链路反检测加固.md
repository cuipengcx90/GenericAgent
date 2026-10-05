# R541 linux.do 链路反检测加固（anyrouter 封号复盘联动）

日期：2026-10-05　执行：GA 会话（temp cwd）　状态：已落地并提交

## 1. 触发与背景

用户 2026-10-05 原话：

> 我检查一下吧。你把我的any router已经弄疯了，不要把我的L站和linux do也弄疯了。你要好好反思复盘一下……然后你复盘一下，借鉴参考一下，保证不要把 linux do 也弄疯了。因为就刷那个随机看帖、随机回帖这个东西，你看一下。有没有什么要调整的？

已确认的事实（另会话复盘 + 本会话核对，见 L2「anyrouter 探针事故复盘(2026-10-05)」）：

- 探针 `tools/anyrouter_slot_watch.py` 用的是**用户 Pi 同账号同一 key**（sha256 前 8 位 `1129516c` 三处一致：Pi `models.json` 的 anyrouter / anyrouter-codex provider、keychain `agentrouter_api_key`）。
- 3 天共 304 轮 / 1244 发（10-03 284、10-04 936、10-05 24）全部记在主账号名下；10-04 0–13 点还是固定 300s 节奏（11–16 轮/时），随机化滞后用户提醒 1.5 天。
- 10-05 11:56 账号被封（403「用户已被封禁」）。**因果未证**（L 站有人「一次没用也封」= 按名单批量清洗），但该探针是账号名下唯一脚本化异常流量 = 第一嫌疑。

对照规范：`memory/anti_detection_sop.md`（四铁律＝独立身份 / 随机化从第一版起 / 指纹模拟 / 量级与可停）。据此对 linux.do 两条链路（浏览 daemon、随机回帖）做加固。

## 2. 本次改动（三项）

### A. 风控保险丝（浏览 daemon 内置）

`tools/linuxdo_browse_daemon.py`（988 行 / 46707 B）新增常量与函数：

```
 92:RISK_PAUSE = (3600, 7200)        # 命中单次风控信号 → 暂停 60–120min
 93:RISK_ESCALATE_N = 3              # 同类信号当日累计 ≥3 ⇒ 当日停止浏览 + 强制告警
 94:RISK_ALERT_COOLDOWN_H = 6        # 同类告警最小间隔（小时），避免刷屏
103:RISK_STRONG = ("BANNED", "RATE_LIMIT", "FORBIDDEN", "LOGIN_REQUIRED")
105:REST_MIN_GAP_DAYS = 3            # 两次休息日至少间隔 3 天（不连休）
241:def notify(text):
255:def risk_kind_from_http(status):
266:def risk_kind_from_text(txt):
312:def handle_risk(st, kind, detail=""):
326:def rest_day_today(st):
```

行为：命中 `RISK_STRONG` 之一 ⇒ 写 state（暂停截止时间 + 同类计数）+ 飞书告警（同类 6h 内不重复）+ 暂停 3600–7200s 随机；同类当日累计 ≥3 ⇒ 当日停止浏览。CF 挑战（非 STRONG）只轻量计数、不中断。

### B. 随机休息日

`REST_DAY_P = 1.0/7.0`（平均每周 1 天完全不浏览，降总量 + 拟人），且与上次休息日最小间隔 `REST_MIN_GAP_DAYS = 3`（不连休）。判断实现在 `rest_day_today()`（:326），watch 主循环进入即判定。

### C. 回帖口径收紧 + 记忆同步

- `memory/linuxdo_reply_sop.md`（70 行 / 6641 B）：条间 240–600s 随机（旧 180–420s）、日 ≤10（旧 ≤20）、单任务 ≤5；补「遇 429/403/BANNED 立即停当日回帖并记录原因，禁『感觉还好』硬顶」。
- `memory/linuxdo_browse_sop.md`（85 行 / 9422 B）：补风控保险丝与随机休息日节；`--selftest` 痕迹由旧 6/6 更新为 **13/13**（两处：:64 与 :78）。
- `memory/anti_detection_sop.md`（36 行 / 4590 B）：两行旧口径订正（见 §4 diff）。

## 3. 验证证据（原始输出）

离线自测（本会话真跑）：

```
$ python3 tools/linuxdo_browse_daemon.py --selftest 2>&1 | tail -5
[2026-10-05 13:25:52] 风控信号 BANNED → 暂停 0 分钟
[2026-10-05 13:25:52] 风控信号 BANNED → 暂停 0 分钟
[2026-10-05 13:25:52] 风控升级 BANNED x3 → 当日停止浏览
selftest 13/13 PASS
```

（用例计数为动态推导，非硬编码字符串；含风控判据正/负控、风控链路 stub 通知不真发、休息日幂等与常量边界。历史上因计数写死在输出串里曾出现「9/9 PASS」假绿，已修。）

watch 已在跑最新代码（重启时间晚于 daemon 代码改动）：

```
$ python3 tools/bg_task.py status --name ldo_watch
ldo_watch            RUNNING          pid=32895    exit=-    log=0B  运行中（etime=02:59）
$ pgrep -fl linuxdo_browse_daemon | head -5
32897 .../Python tools/linuxdo_browse_daemon.py watch
```

（`log=0B` 属 stdout 块缓冲，非未运行；判活以 pid + state 增长为准。）

## 4. 变更清单（git）

| 提交 | 内容 |
|---|---|
| `d4f225b` | daemon 加风控保险丝 + 随机休息日；reply/browse SOP 同步口径；selftest 9/9→13/13（cases 动态计数含负控） |
| `8b5f17d` | memory：anyrouter 探针事故复盘（2026-10-05 封号）+ `anti_detection_sop` 反检测规范 + `linuxdo_trust_level_sop` 加随机化缺口条款 |
| `e8657f8` | L1 索引两行订正（browse/reply SOP 口径同步，2/2 行） |
| 本批 | `anti_detection_sop.md` 两行旧口径订正（-U0：

```
@@ -34,2 +34,2 @@
-`tools/linuxdo_browse_daemon.py`：cap 140–180 … / 只读铁律。
-`memory/linuxdo_reply_sop.md`：条间 180–420s 随机、日 ≤20 …
+… 旧行 + ★2026-10-05 风控保险丝与随机休息日；离线自测 13/13。
+… 条间 240–600s 随机、日 ≤10、单任务 ≤5（2026-10-05 收紧）、遇 429/403/BANNED 立即停当日并记录。
```

## 5. 残余风险与未动面（明示）

1. **等级检查仍是准点特征**：`scheduler`（reflect/scheduler.py:140-149）到点即跑、无随机延迟（`max_delay_hours` 是过期跳过窗口而非随机抖动）⇒ 09:30 等级检查是固定时刻特征；且它与 browse daemon 每日顺带读等级卡（`level_log.jsonl`）**功能冗余**。处置（停用/降频/随机化）待用户批。
2. **回帖无 daemon、无 sche_tasks 定时**：仅会话内手触发，故本次只能收紧 SOP 文案口径，不能强制节流；若要长跑应改造成 daemon。
3. **保险丝是事后型**：只能被信号触发后暂停，不能预防站点侧无声标记。
4. **因果仍不可证**：anyrouter 封号与探针的因果关系未被证实；本次加固是按「量级与可停」原则做的风险削减，不宣称可杜绝封禁。
5. 站点若已对账号形成历史画像，无本地观测手段。

## 6. 复算命令

```bash
python3 tools/linuxdo_browse_daemon.py --selftest | tail -1                 # 13/13 PASS
grep -n 'RISK_PAUSE\|RISK_ESCALATE_N\|REST_DAY_P\|REST_MIN_GAP_DAYS' tools/linuxdo_browse_daemon.py
grep -n 'selftest' memory/linuxdo_browse_sop.md memory/linuxdo_reply_sop.md
python3 tools/bg_task.py status --name ldo_watch
wc -c -l tools/linuxdo_browse_daemon.py memory/linuxdo_browse_sop.md memory/linuxdo_reply_sop.md memory/anti_detection_sop.md
```
