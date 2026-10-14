# DigitalPlat 面板自动登录 — 现场勘查记录（2026-09-25）

## 目标
给 tools/dpdomain_renew.py 加「会话失效自愈 = 自动登录」。用户确认账号无 2FA。

## 已确证契约（来自前端源码 + 实调）
- API base: `/_panel_api`
- 登录: `POST /_panel_api/api/auth/login`，body JSON `{"email","password","turnstile_token"}`
  - 依据: page-7b9f88b693569a2b.js 内 `onSubmit` -> `L.mutate({email, password, turnstile_token: I})`；表单 input name=email/password
- CSRF: 写方法必须带头 `X-CSRF-Token`（chunk 58560: `_3`="X-CSRF-Token"，值取 cookie `panel_csrf_token`，缺失则 `GET /_panel_api/api/security/csrf`）
  - 实测: 不带该头 -> 400 `{"code":"csrf_failed"}`；带上 -> 400 `{"error":"Turnstile verification is required"}`
  - => **服务端强制校验 Turnstile token**（无 token 一律拒绝，与前端 disabled 一致）
- Turnstile sitekey: `0x4AAAAAAAxuMrGCYFcOwd1N`（chunk 56136 硬编码；非 localhost 时 required=true）
- Turnstile 挂载: chunk 8863 组件 -> 动态插 `https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit` -> `window.turnstile.render(el,{sitekey,theme,callback})`（可见 managed 模式，无 execution/invisible 参数）

## 现有续费链路（memory/dpdomain_renew_sop.md）
- 工具在已登录 dashboard 标签内用桥 fetch（自动带 cookie）=> 平时不需要密码
- rc=2 = 无已登录标签

## 实验记录（都在真实用户 Chrome 的 dashboard 标签内，sessionId=2109120119）
1. 桥 execute_js 有 **10s ACK 超时** => 长任务必须「fire-and-forget 写全局变量 + 稍后回读」
2. 手动 render widget 成功返回 widgetId（cf-chl-widget-r8knq），容器里生成了 wrapper + `<input type=hidden name=cf-turnstile-response>`，但 **73s 后仍无 token、容器内无 iframe 元素**
3. 资源计时确认 3 个 CF 请求：api.js + `/cdn-cgi/challenge-platform/h/b/turnstile/f/av0/rch/<wid>/<sitekey>/auto/fbE/new/normal?lang=auto`（两个 widget 各一次，duration 436ms）
4. 从 dashboard 页内嵌 iframe 加载 `/auth/login`：表单正常渲染（inputs=email/password/cf-turnstile-response），但同样无 token
5. `document.hasFocus()===false`（Chrome 窗口未聚焦）；visibilityState=visible

## 待验证假设
- H1: 窗口未聚焦/后台导致 managed 挑战停滞 => 需激活窗口后重测
- H2: 挑战 iframe 落在 closed shadow root / 被移除，无法由外部读取
- H3: 代理链路对该挑战域不友好 => 用 no-cors fetch 判 网络错误 vs CORS

## 价值判断
若 Turnstile 无法在无人值守下拿到 token，则「密码自动登录」不可行，备选方案：
- A) 会话失效时私信提醒 + 一键链接（人工登录一次，会话可维持数周）
- B) 查面板是否提供 API Key/长期令牌（彻底绕开 Turnstile）
- C) 保持常驻登录标签（现状），仅做「提前预警会话将失效」
