# hCaptcha 过验 SOP

适用: 页面出现hCaptcha复选框时的人机通过操作规范。

## 策略
- 全程 ljqCtrl 物理键鼠，禁止 JS/CDP 操作 captcha 元素
- CDP 仅允许切 tab 到前台，不做其他操作
- 定位: vision_sop 截图识别复选框坐标 -> ljqCtrl.Click
- 鼠标轨迹要自然: 先 SetCursorPos 到附近，稍等再 Click 目标

## 坑点
- hCaptcha 检测 isTrusted、鼠标轨迹、时序，非物理操作必被拦截
- 禁止快速重试，失败后等 5+ 秒，连续失败会升级难度
- 禁止提取 token/cookie 绕过，会触发账号安全机制
- 超难验证码(多轮图片/拼图) -> 立即转人工，不要重试加剧难度
