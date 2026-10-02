# GCP 账单审计 SOP（只读）

## 触发
用户问 GCP 花了多少钱 / 赠金(credit)为什么没了 / 为什么某月扣费 / 要导出账单。

## 前置与入口
- 需用户浏览器已登录的 GCP Console 标签；取数走驱动 **HTTP 桥 `POST http://127.0.0.1:18766/link`**（body 形如 `{"cmd":"...","sessionId":"<字符串>"}`）。
  ★ **18765 是 WebSocket 端点**，对它发普通 HTTP GET 会得 HTTP **426**（Upgrade Required）——426 是调用形状错，**不是**浏览器/导出故障，别据此归因。参考客户端：`temp/gcp_audit/drv.py`（get_all_sessions / set_session / execute_js）。
- ★ 控制台 SPA 在**跨域 iframe** 内；取表要在 `iframe.contentDocument` 上查（父页查不到 table/行）。

## 数据源与导出
- Billing → Reports 的「下载」**只给 CSV**（无 xlsx）。转 Excel 用 **openpyxl**（本机 pandas 因 numpy ABI 不可用，勿走 pandas）。
- CSV 为 9 列列式（**不含赠金分项**）且含「小计」分隔行 ⇒ 解析须容错、汇总须先剔除小计行。

## 口径（关键，先定口径再判读）
- 报表「节省」列 = **促销赠金 + 其他节省**；其他节省 = 免费额度类永久 $0 项（GKE / VM Manager / Networking 等）。
- 拆赠金：Reports → 筛选 chip「节省（全部）」→ 勾 **促销赠金** → 应用。主表列固定四列（月/使用费/节省/小计），赠金分项看提示/单列。
- 对账样例（2025-10~2026-09 实测）：全年节省 **−89.79 = 促销赠金 47.26 + 其他节省 42.53**。

## 本账号已证事实（2026-10-02 快照，非永久）
- 赠金 = FreeTrial **$300**（已过期，剩 282.75 不可用）+ **Google Developer Program premium $10/月 ×3 笔**（2026-01-28 / 03-08 / 07-03，**发放无规律**）。
- 仅 2026-09 产生实付 **$6.21**：当月 Compute Engine 用掉 $9.07，而当月赠金账面只剩 **$2.86** 可抵。

## 纪律 / 坑
- **只读**：禁改预算/付款设置、禁抄录卡号；登录重验证（Google `OR-ORCRI-01`）拦截时**不要代过**。
- 改过控制台筛选后，收尾点「**重置过滤条件**」还原现场并复核 chip。
- 报告内每个数值须回源（CSV 原始行 / 控制台文本），推不出的写「未证实」，禁拿推测补空白。


## CLI 侧成本守望（2026-10-02 落地：无人值守日常巡检，只读）
- **工具** `tools/gcp_cost_watch.py`（`check` / `--json` / `--selftest` 14/14）；**定时** `sche_tasks/gcp_cost_watch.json`（每日 09:40，enabled，max_delay 8h）⇒ scheduler=`agentmain --reflect` 读 `sche_tasks/*.json`，无需注册。设计：全绿静默，仅 ⚠️/❌/rc=2 才推 飞书私信+钉钉群（与其它 AGENT 的 `~/tools/gcp-billing-check.sh` 差异=本工具**含固费/漏费面与预算阈值体检**，且进 GA 任务体系；两者可并存，本工具为准）。
- **停机三前提（任一破即 ALERT）**：①实例 TERMINATED ②磁盘 ≤30GB（免费层 pd-standard 30GB）③无新增资源（静态 IP / 快照 / 转发规则 / Cloud Router-NAT；域名年费单列）。
- **固费/漏费面**（对方脚本缺）：Cloud Domains 年费、VM Manager OS 策略、结算账号下的其它项目。
- **gcloud 本机**：binary `/opt/homebrew/share/google-cloud-sdk/bin/gcloud`，账号 cuipengcx（非交互 shell 无 PATH ⇒ 脚本内写死绝对路径）。
- **赠金 CLI 读不到**（实测 `billingAccounts/*/credits` 404）⇒ 只能人工看 `https://console.cloud.google.com/billing/017EE2-1FDFF1-148293/credits`；判据：出现新的 DevProgram 券(剩余>0) ⇒ 赠金仍在发；某月账单「小计」>0 ⇒ 赠金已停。
- **金额口径**：真实扣款看 Console→结算→报告 的**小计**列（不是「使用费」列）。
- 2026-10-02 11:42 真跑（WARN 的唯一来源=两个预算阈值形同虚设，非资源问题）：
```text
GCP 成本守望 · 2026-10-02T11:42:30+0800 · verdict=WARN
项目=cpgoagent-11013  结算=017EE2-1FDFF1-148293
--------------------------------------------------------------
✅ [instance:cuip-20251215-084618] 已停机(TERMINATED)，不收计算费
✅ [disks] 合计 30GB pd-standard，在免费层 30GB 内
✅ [addresses] 无静态 IP
✅ [snapshots] 无快照
✅ [network-rules] 无转发规则 / 无 Cloud Router(NAT)
✅ [vm-manager] 无 OS 策略分配（VM Manager 已关）
✅ [billed-projects] 结算账号下仅 cpgoagent-11013
⚠️ [budget:实付告警：花钱就通知] 金额仅 $1/月 ⇒ 真实月成本远高于此，阈值天天触发=形同虚设（建议提到 $5~$10）
⚠️ [budget:每月预算提醒] 金额仅 $0.1/月 ⇒ 真实月成本远高于此，阈值天天触发=形同虚设（建议提到 $5~$10）
✅ [domains] 无 Cloud Domains 注册（无年费漏项）
ℹ️ [credits] 赠金余额无公开 CLI 端点（credits 实测 404）⇒ 需人工核对；URL: https://console.cloud.google.com/billing/017EE2-1FDFF1-148293/credits ; 判据：出现新的 DevProgram 券(剩余>0) ⇒ 赠金仍在发；若某月账单「小计」>0 ⇒ 赠金已停
--------------------------------------------------------------
停机三前提：①实例 TERMINATED ②磁盘 ≤30GB ③无新增资源(静态IP/快照/LB/NAT)
真实金额请看 Console → 结算 → 报告 的「小计」列（不是「使用费」列）。
```
- ⚠️ 待办（需用户拍板，只读不代改）：两个预算告警阈值 $1/月、$0.1/月 远低于真实月成本 ⇒ 天天触发、失去告警意义；建议提到 **$5~$10/月**（改预算属计费配置，SOP 纪律禁止代办）。
