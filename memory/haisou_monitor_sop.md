# haisou.cc 网盘资源监控 SOP

## 入口
- `birdie_monitor/haisou_daily.py`：搜索、按 `hsid` 去重、仅新增通知。
- 调度定义：`sche_tasks/haisou_daily_monitor.json`。

## 认证
- 会员会话 Cookie `hs_web_session` 是 HttpOnly；通过 keychain 读取，严禁明文落盘或输出。脚本可脱离浏览器运行。

## 核心接口
- `POST https://haisou.cc/api/v2/shares/search`
  - body: `{"query": "关键词", "filters": {"scope": "title", "platforms": ["quark"|"baidu"|"ali"], "include_filtered": false}, "pagination": {"page":1, "page_size":N}}`
  - **必须**在 `filters.platforms` 里显式指定至少一个平台，否则报 `400 INVALID_PARAMETER 至少选择一个云盘`（此为参数校验错误，非认证失效，勿误判会话过期）。
  - 已验证可用平台枚举：`quark`、`baidu`、`ali`（页面渲染观察到的三种图标对应值）。
  - 返回 `data.items` 直接包含 `hsid`、`platform`、`share_name`(含 `<span class="highlight">` 高亮标签需 strip)、`share_code`、`share_pwd`，日常监控无需再调用详情/fetch接口。
- `POST /api/v2/shares/{hsid}/fetch`：返回同样字段但会消耗 5 会员额度/次，仅用于人工核实，不应在定时监控里对每条结果调用。
- 链接生成规则（脚本实际采用；仅夸克已用 requests 验证 HTTP 200）：
  - 夸克: `https://pan.quark.cn/s/{share_code}`
  - 百度网盘: `https://pan.baidu.com/s/1{share_code}`（`share_pwd` 需要用户自行输入提取码，不能拼进URL）
  - 阿里云盘: `https://www.alipan.com/s/{share_code}`

## 监控设计
- 首次运行：只建立本地基线（写入 `haisou_seen_items.json`），不对外通知。
- 后续运行：仅当出现新 `hsid`（新增资源）或接口报错/会话失效时才通知(飞书私信+钉钉群各一次)；无新增静默，避免刷屏。
- 关键词列表维护在脚本内 `KEYWORDS` 常量，参考 birdiesearch_monitor_sop 的选词逻辑（真实存在且网盘站内有资源命中的词）。

