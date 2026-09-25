# dpdomain_renew_sop — DigitalPlat 免费域名自动续费

**入口**
- 工具: `tools/dpdomain_renew.py`（stdlib-only）
  - `check [--json]` 只读盘点 / `run [--apply] [--notify]`（默认 dry-run）/ `--selftest`
  - 退出码: `0` 正常（含"无需续费"）/ `2` 无已登录 dashboard 标签页 / `3` 有域名续费失败 / `4` 用法错 / `5` 自测失败
- 定时: `sche_tasks/dpdomain_renew.json`（`repeat=monthly`、`schedule=10:20`、`max_delay_hours=72`；scheduler 冷却是 **27 天**，见 `reflect/scheduler.py::_parse_cooldown`）
- 状态: `temp/dpdomain_renew_state.json`（同域名 27 天内不重复，丢失无害）

**规则（站点源码 i18n 原文，非猜测）**
- `renewalNotice` = "到期前 120 天内可免费续费。提前续费请使用积分。"
- ⇒ **窗口外一律跳过**（提前续费要花积分）；窗口 = 到期日前 120 天内（`FREE_WINDOW_DAYS` 常量）
- 每月检查 + 120 天窗口 ⇒ 每年约 4 次机会；续费成功到期日 +1 年即退出窗口，不会重复扣

**接口（从前端源码提取 + 实测）**
- 读: `GET /_panel_api/api/domains` → `{ok, domains[]}`，字段 `expiry_date="YYYYMMDD"` / `status` / `can_manual_renew`
- 续费: `POST /_panel_api/api/domains/{domain}/renew`，body `{"renewal_type":"free","years":1}`
- 必需头: `X-CSRF-Token`（= cookie `panel_csrf_token`，可读；没有则 `GET /_panel_api/api/security/csrf`）+ `Idempotency-Key`（随机 UUID）
- 失败码 `csrf_failed` → 脚本重取 token 重试一次

**关键前置（易踩）**
- 走 **TMWebDriver 桥**(`127.0.0.1:18766/link`) 在页面内 `fetch` ⇒ 自动携带登录 Cookie，**不需要** 用户名密码/token
- ⇒ 浏览器必须已有**已登录**的 `dashboard.digitalplat.org` 标签页；否则 `rc=2`（桥**没有** open-tab 命令，任务 prompt 用 `window.open` 兜底重试一次，仍失败则私信提醒人工登录）
- 脚本会校验所选会话的 `location.host` 是否真为目标站（防 tab id 陈旧指向别的站点）

**坑**
1. 写路径**可以安全验证**: 用一个不存在的域名跑 renew，服务端回 `403 You do not have permission to manage this domain` = 说明 CSRF/请求形状已过鉴权层（若是 CSRF 问题会回 `csrf_failed`）。真实"续费成功"分支只在进入窗口时才会跑到。
2. 续费后**必须复核**列表里 `expiry_date` 是否真 +1 年，否则记失败（禁止只看 HTTP 200 就报成功）。
3. 通知只在"续费成功/续费失败/检查失败/无会话"时发，窗口外完全静默。
4. 窗口天数若站点改口径，改 `FREE_WINDOW_DAYS` 一处即可。

**端到端实证（2026-09-25 16:28，无人值守真跑）**
- 任务文件落盘后 scheduler（2min 一跳）立即 `TRIGGER dpdomain_renew (repeat=monthly, schedule=10:20, last_run=None, attempt=1)`
- 子进程按 prompt 跑 `run --apply --notify` → **rc=0**，两域名均「跳过: 未进入 120 天免费窗口(337/365 天)」，**未续费、未发通知**
- 报告写入 scheduler 注入路径 `sche_tasks/done/<YYYY-MM-DD_HHMM>_<tid>.md`（= `[报告路径]` 行，reflect/scheduler.py:188 注入；报告末尾另有「完成后将执行报告写入 {rpt}」）
- 结论：注册 → 触发 → 确定性执行 → 留档 全链路打通；窗口外运行全程静默

**任务 prompt 写法（可复用）**
- 只写「执行哪条命令 + 退出码含义 + 各退出码怎么处置」，不写让 LLM 自己判断业务逻辑（判断全在脚本里）
- 路径一律绝对路径（含 `cd /Users/cuip/projects/GenericAgent`），避免 cwd 漂移
- 收尾写「写入本次任务指令里给出的报告路径」以对齐 scheduler 注入的 `[报告路径]`
