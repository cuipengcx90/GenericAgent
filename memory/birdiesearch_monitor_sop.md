# BirdieSearch 每日网盘资源监控 SOP

## 产物位置(永久)
`/Users/cuip/projects/GenericAgent/birdie_monitor/`
- `birdie_client.py`：签名算法 + 签到 + 搜索 + 链接解析
- `birdie_daily.py`：每日任务主流程(签到→搜索→解析→去重→通知)
- `birdie_seen_items.json`：按 `_id` 去重的本地存量(含真实链接+提取码)
- `birdie_cookies_raw.json`：Cookie 凭证(BirdieSearch.key/key.sig)，仅引用不迁移
- 调度: `sche_tasks/birdie_daily_monitor.json`（每天11:00 daily, max_delay 6h，脚本自含通知，prompt 只允许跑该脚本一次）

## 关键技术点(艰难验证过，勿重新摸索)
1. **签名算法 Char-Code**：
   ```
   s = abs(int((now_ms << 2) * 0.7))
   r = json.dumps(data, separators=(',',':'), ensure_ascii=False) if data else "null"
   full = r + "&" + str(s)
   a = sum(ord(c) for c in full)
   header = f"{s}&{a*s}"
   ```
2. **签到**：POST `/api/user/sign`，已签到返回 `code=-1`（非错误，属正常态）。
3. **搜索**：POST `/api/search`，body含 `word/pages/aide/filter`；`item.password`（提取码）在列表页已直出；`item.url` 是65位hex token，**不是**真实网盘链接。
4. **★链接解析核心突破★**：GET `https://www.birdiesearch.com/redirect?url=<item.url>&type=<item.cloudType>`（带Cookie头），返回 HTML 内嵌 `<script>location.href = "https://pan.baidu.com/..."</script>`，正则 `location\.href = "([^"]+)"` 提取真实链接。百度网盘、夸克网盘均已实测验证通过。
5. **限额判定策略**：前端 JS 未找到精确每日搜索次数上限，采用"实测触发式"——遇 HTTP 超时/异常即视为当日限额已到，立即停止后续关键词循环（不重试、不硬编码数字）。

## 去重与通知规则
- 去重键：`item._id`，存入 `birdie_seen_items.json`。
- 仅当"有新增命中"或"当日限额耗尽"时才推送（飞书私信 + 钉钉群，复用 `reflect_scripts/reflect_common.notify_feishu` 与 `notify_dingtalk_group.notify_group`）。
- "今日无新增命中"且未触发限额 → 静默，不推送。

## 关键词维护
默认关键词列表在 `birdie_daily.py` 顶部 `KEYWORDS`：当前13词，覆盖AI技术类("AI Agent"/"AI coding"/"AI应用开发"/"Vibe Coding"/"Claude Code"/"MCP教程")、出海创业类("出海SaaS"/"AI创业"/"独立开发出海"/"SaaS出海"/"AI产品出海")、抖音等渠道热门课程IP名("懂王"/"鸡翅大模型"/"赋范")。后续增删直接改此列表，无需改动其他逻辑；同步维护 `sche_tasks/birdie_daily_monitor.json` 的 prompt 文本中关键词描述，避免文字与实际列表脱节。

## 关键词遴选与验证方法
- 站外热度调研：优先用已配置的 MCP `tavily-hikari`（`ga_mcp_client.MCPClient("tavily-hikari")` → `call_tool("tavily_search", {...})`），而非网页版Google搜索，可结构化返回结果且不依赖浏览器会话。
- 站内命中量核实：用 `bc.search(cookie, kw, page=1)["total"]` 看该词在 BirdieSearch 站内已收录的资源总量（辅助判断是否值得纳入 KEYWORDS，非精确值，随时间变化，不作长期事实存储）。
- 两者用途不同：tavily 判断该IP/词在抖音等渠道当下是否热门、是否为真实存在的课程/博主IP；bc.search 判断该词在网盘资源站内是否有实际可命中的资源。两者结合判断，避免只看一边。
- ⚠️坑：验证新词有效性会直接调用真实 search 接口，消耗当日免费搜索额度，可能导致当天定时任务因额度不足提前停止。批量测词前需权衡，尽量安排在非任务时段，或接受当天主任务受影响。
- 已知会被平台屏蔽的词：命中平台侵权/敏感策略时返回 `code=-1` 且 `msg` 含"相关内容已删除"（如"极客时间"），此类词即使曾有资源也应排除，不纳入 KEYWORDS。

## 已知噪音(勿误判为故障)
脚本 stderr 中出现的 numpy/sklearn 版本警告，来自 `llmcore`/`mykey.py` 导入链的后台线程噪音，与本任务无关，`exit_code=0` 即代表脚本本身执行成功。
