# TrendRadar MCP SOP（晚报/热搜/RSS任务）

## 关键坑点（多次重试总结）

### 1. MCP协议：Streamable HTTP（非SSE！）完整握手
```python
# 正确方式：3步握手
# Step1: initialize → 获取session_id
r = requests.post(BASE, json=init_payload, headers={"Accept":"application/json, text/event-stream"})
session_id = r.headers.get("Mcp-Session-Id")
# Step2: 必须发 notifications/initialized，否则后续所有工具调用报 Invalid parameters！
requests.post(BASE, json={"jsonrpc":"2.0","method":"notifications/initialized"},
    headers={**H, "Mcp-Session-Id": session_id}, timeout=5)
# Step3: 工具调用，读r.content并regex提取 data: 行
import re; matches = re.findall(r'data: (.+)', r.content.decode())
```

### 2. search_news 含 date_range 参数必返回0条（坑！）
- 勿用 `date_range={"start":"2026-04-23","end":"2026-04-23"}` → 0条
- 改用 `get_latest_news(limit=100)` 后手动按 timestamp 字段过滤今日

### 3. 数据结构差异
| 工具 | 日期字段 | item关键字段 |
|------|---------|------------|
| get_latest_news | timestamp ("2026-04-23 21:xx:xx") | title, platform_name, url, rank |
| get_latest_rss | published_at ("2026-04-23Txx:xx:xx") | title, feed_name, url, summary |
| search_news | date ("2026-04-23") | title, platform_name, similarity_score |
- response结构：`parsed["data"]` 是列表（不是 `parsed["results"]`）

### 4. 有效工具列表
`get_latest_news` / `get_latest_rss` / `search_news` / `send_notification` / `list_available_dates`

### 5. 通知发送
- 飞书群：`from notify_feishu_group import notify_group; notify_group(text)`（需sys.path加reflect_scripts）
- 飞书私信：`from reflect_common import notify_feishu; notify_feishu(text)`
- 钉钉群：`from notify_dingtalk_group import notify_group; notify_group(text)`（模块名是 notify_dingtalk_group，非 ding_notify；sender 同为 notify_group）
- 三通道各自独立 try/except，互不阻塞