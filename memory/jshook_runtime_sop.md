JS Hook Runtime SOP

适用: 浏览器 JS 运行时逆向、签名定位、请求/加密链路审计。

关键模型
- 三层: 注入通道 -> 通用注入引擎 + Preset -> 离线读取/静态分析。
- 统一写入 `window.__aiHooks['preset-<name>']`。
- 常见 bucket 字段: `ts/url/method/reqHeaders/reqBody/status/respHeaders/respBody/fn/args/ret/algo/key/iv/mode/padding/msg/stack`。

三不变量
- 幂等: preset 开头判断已安装，避免重复包装。
- 保引用: 先保存原始引用/绑定，再调用原方法。
- 不抛错: hook 自身 try/catch，不能影响业务页面。

核心价值
- `stack` 是定位签名/核心逻辑函数的关键；保留前 3-5 帧，可映射到源码文件 + 行列。

注入通道
- 快速验证: DevTools Console 粘贴。
- 首屏前注入优先: CDP `Page.addScriptToEvaluateOnNewDocument` / Playwright `add_init_script` / Puppeteer `evaluateOnNewDocument`。
- 用户脚本: `@run-at document-start`；Tampermonkey 常配 `@grant none`，Violentmonkey 用 page context。

跨上下文坑点
- 同源 iframe 常可自动覆盖；跨域 iframe 需 attach 子 target / 单独处理。
- Worker / Service Worker 不继承主线程 hook，需单独拦截或 attach。

高频 preset
- 网络: `fetch` / `xhr` / `websocket`
- 运行时: `eval` / `Function` / `setTimeout` / `setInterval` / `addEventListener` / `postMessage` / `MutationObserver` / `Proxy` / `Reflect` / `Object.defineProperty`
- 存储: `cookie` / `localStorage` / `sessionStorage` / `indexedDB`
- 加密/编码: `atob` / `btoa` / `crypto.subtle` / `crypto-key-capture` / `CryptoJS AES/Hmac/Hash`
- 反检测/环境: `anti-debug` / `navigator` / `canvas fingerprint` / `webassembly`
- 跳转/DOM: `location.href` / `window.open` / `document.write`

实操顺序
- 先 hook `fetch/xhr + stack`，找请求入口。
- 再沿 stack 去 Sources/断点定位签名函数。
- 遇到加密链路再补 `crypto/atob/btoa/wasm` 等定向 preset。

典型坑
- 页面初始化即签名时，Console 注入太晚，必须首屏前注入。
- 重复注入会双重包装，导致日志污染或行为异常。
- 不保留原引用，容易递归调用或被后续覆盖。
- 响应体/大对象需截断，避免日志过大。