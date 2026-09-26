# kc_profile 关键发现（2026-09-25）

## 数据字典（实测，非推断）
- `/app/usage`：`ZSTARTDATE/ZENDDATE` 为真区间，**时长 = ZENDDATE − ZSTARTDATE**；
  `ZVALUEDOUBLE` 是位型垃圾（1e19 量级），**不可**当秒用（与 R371 警告一致）。
  窗口：2026-09-01 → 2026-09-25，n=10003，Σdur≈159.3 h。
- `/display/isBacklit`：取值列是 `ZVALUEINTEGER`（0/1）。窗口 08-28 → 09-25，n=1584。
- 其余流窗口不等长（notification 29 天、bluetooth 08-31→09-23 …）⇒ 跨流比较先裁公共窗。

## ★口径结论（本轮重点，经三组实测反证后定案）
1. `isBacklit` 的状态行**不铺满时间轴**：本窗口仅 1584 行、**62 处断续**（最大 75.4 h）。
   → 曾有步骤因缺 `ORDER BY` 把断续数误算成 1415（假值），已纠正。
2. 三态内前台事件密度（唯一可靠判据）：
   - 有行 v=1：127.5 h，4844 事件，**38.0 事件/时** ⇒ 使用中
   - 有行 v=0：252.8 h，6 事件，**0.0 事件/时** ⇒ 熄屏/离场
   - 无行(空隙)：294.0 h，5024 事件，**17.1 事件/时** ⇒ **也是使用中**
3. ⇒ **只有 v=0 可判熄屏**；把「无行」当熄屏会严重高估（这正是 127 h 与 159 h 前台矛盾的来源）。
   「窗口 − 熄屏 = 亮屏」的推算口径**作废**（实测给 17.5 h/天，与前台证据矛盾）。
4. 「在场/活跃」改用 `/app/usage` 事件耗时合计（口径=Σ(ZENDDATE−ZSTARTDATE)）。

## 代码坑（本轮踩到并修）
- `w1 = max(e for _, _, e in bl)` 会把生成器变量 `e` 绑到**第三**元素(value:int) ⇒
  `hours_of(w0, w1)` 报 `datetime < int`。正确写法 `max(e for _, e, _ in bl)`。
- `kc["backlit"]` 元组顺序 = (start_dt, end_dt, value_int)。
- `hours_of(a, b)` 吃 **datetime**（内部按整点切分）。
- 空隙统计/区间包含判断必须带 `ORDER BY ZSTARTDATE`，否则 `bl[i+1]` 乱序、空隙全是假象；
  区间包含还须**查最近两行**（j, j+1）而仅一行，避免边界落到相邻行时误判为空隙。

## 交叉验证
- Chrome(History 只读副本) 小时分布 vs kc 小时 app 事件分布：**Pearson r = 0.952**（n=24）。
  （Chrome visits 含自驾自造访问，R362 已证 21.5% 系自造 ⇒ 只比形状不比绝对值。）

---

# 附：`/app/usage` + `/notification/usage` 消费（R434，2026-09-27）

消费者：`tools/kc_app_usage.py`（只读消费器，纯 stdlib）。字段落盘：
本目录 `kc_app_usage_fields.json`（`--emit-profile` 产出，schema `kc_app_usage_fields/v1`）。
真值来源：快照 `temp/kc_snapshots/2026-09-25/knowledgeC.db`（副本，全程不开源库；
只读自证 = 源库 sha256 前后一致）。

## 口径（本器与 profile.py 的分工）
- `/app/usage` 的行 = `(ZSTARTDATE, ZENDDATE, ZVALUESTRING=app)`；**时长 = ZENDDATE − ZSTARTDATE**，
  同一 app 的多行**先并集**再切日（重叠不得重复计时）。
- 快照**采集时点**（`max(ZCREATIONDATE)` = 2026-09-25 22:30:53）之后的区间不计入；
  本快照实测 `app_rows_after_cutoff_kept_out = 0`、`app_nonpositive = 110`（零/负时长行跳过）。
- 窗口 2026-08-28 → 09-25，29 天；其中 4 天（08-28…08-31）`/app/usage` 无行 ⇒ 记 **UNKNOWN**
  （该流最早行 = 2026-09-01 13:07:30；UNKNOWN ≠ 0）。`/notification/usage` 无行日 3 天（09-08/09/19）。
- app 计数：库内 44 个唯一 app，其中 `com.tencent.flue.WeChatAppEx` 仅 1 行且时长为 0
  ⇒ 有正时长的 app = **43**（与 `fields.apps` 一致）。

## 与熄屏口径的对账（同一快照，同器复算）
- 本器 `/display/isBacklit` v=0 合计 **262.736 h** ＝ `profile.py` 同库 `dark_total_hours` **262.74 h**
  ⇒ 两器熄屏口径**逐字一致**（±0.05 h 内）。
- 差异来源已定位：`tools/sleep_source_crosscheck.py` 的默认 kc 源是 **live 副本**
  `temp/kc_live_crosscheck/knowledgeC.db`（滚动窗口，当时自报 249.35 h），
  并非本目录 09-25 快照 ⇒ **门值（249.350 h）绑的是它那次调用所用的源，不可跨源引用**。

## 仅熄屏口径得不出、本次新得的结论（每条附来源）
1. **通知流入面与前台占用面几乎不重叠**：`/notification/usage` 投递 Top2 =
   `com.electron.lark` **2932** + `com.apple.Terminal` **2560**（合计 5492/5706 = **96.3%**），
   而 `com.apple.Terminal` 在 `/app/usage` 里 **0 行**（44 个 app 名单内不存在）。
   ⇒ 通知密度**不是**注意力投入的度量，它是后台任务完成推送。来源：`notif_by_bundle` + SQL 直接计数。
2. **注意力高度集中于单一工具**：前台时长 Top1 `com.mitchellh.ghostty` **92.942 h / 166.521 h = 55.81%**，
   其通知投递为 0 次；Top2 Chrome 27.769 h（16.68%）、Top3 lark 16.684 h（10.02%）。
   来源：`fg_hours_by_app_total`。
3. **前台信号本身有 ~0.6% 不可信**：`/app/usage ∩ 熄屏(v=0)` = **0.981 h / 148 段**，
   最大者 = 2026-09-25 16:59:09→17:51:32 Chrome（3143 s 全落熄屏）。
   来源：`fg_off_overlap_hours` + `anomaly_top`（Top1 见报告 §4）。
4. **整体前台驻留比** = 166.521 h，覆盖 **25 个有数据日（600 h）⇒ 27.8%**；
   若拿 29 天全窗口（696 h，含 4 个 UNKNOWN 日）当分母则 = 23.9%（**偏低的伪值**，分母含无数据日）。
   通知密度 5706/166.521 = **34.27 次/前台小时**（分母是前台时长上界 ⇒ 该值是**下界**密度）。
   来源：`window` + `notif_per_fg_hour`。

## 引用这些数字时的边界
- 「前台」只说明某 app 处于前台，**不说明有人在操作**；驻留型 app（终端/浏览器）系统性高估 ⇒ 时长是**上界**。
- 上面所有数字都随快照时点变动（同 R426 的迟写回填结论）⇒ 引用必须写明「09-25 快照」。
