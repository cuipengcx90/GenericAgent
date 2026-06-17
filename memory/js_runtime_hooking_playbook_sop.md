# JS 运行时 Hook 作战 SOP

## 适用场景
- 需要分析前端请求参数生成、签名、加解密、混淆执行链
- 需要在浏览器运行时抓取 fetch/xhr/websocket/crypto 等关键证据
- 需要在不大改目标逻辑的前提下做可回退的可观测性注入

---

## 标准步骤
1. **先定目标**: 明确要抓的是请求、签名、密钥、明文、栈还是 worker 通信
2. **优先早注入**: 首选 document-start / new document 级注入，避免 hook 太晚错过首包
3. **先抓网络层**: 从 `fetch`/`xhr`/`websocket` 开始，记录 url、method、headers、body、响应摘要
4. **再抓调用栈**: 保留关键 3-5 帧 stack，定位参数生成入口，避免全栈噪音
5. **按需抓加密层**: 追加 `atob`/`btoa`/`crypto.subtle`/CryptoJS 等 hook，只记录必要字段
6. **处理多上下文**: 单独考虑 iframe、worker、service worker；主线程 hook 不会自动继承
7. **保持幂等可回退**: hook 前检查是否已安装；保存原引用；所有包装用 try/catch
8. **验证证据链**: 至少确认一次“输入 → 参数生成 → 请求发出 → 响应返回”的闭环

---

## 坑点
- 最大风险是注入太晚，首包和初始化密钥会直接丢失
- 不要一开始同时 hook 全部 API，先 fetch/xhr，再逐步加 crypto/wasm
- worker、iframe 常常是证据断点，主线程结果不能直接外推
- stack 只留关键帧，避免日志爆炸和噪音淹没入口
- 仅做可观测性修改；禁止默认改业务逻辑、跳过校验或扩大行为范围
