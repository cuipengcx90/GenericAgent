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

## 站点不可达时的诊断手法(2026-09-20 实测验证)
- 症状：脚本 `exit=1` 止于 `birdie_client.py sign_in()`(`POST /api/user/sign`)，报 `ProxyError ... handshake operation timed out` / `SSLEOFError` / `SSL_ERROR_SYSCALL`。签到是第一步，**早于搜索与推送** → 此时不推送、不耗额度，属"站点侧不可达"而非限额。
- 快速定性三步（勿盲目重试脚本）：
  1. `curl -s -o /dev/null -w '%{http_code}' --max-time 20 -x http://127.0.0.1:7897 https://www.google.com/generate_204` 应为 204（排除代理整体故障；代理来自 macOS 系统代理，无 `*_proxy` 环境变量）。
  2. Clash 逐节点测目标站（只读）：`curl -s --unix-socket /tmp/verge/verge-mihomo.sock "http://localhost/proxies/<urlencoded节点名>/delay?timeout=8000&url=https%3A%2F%2Fwww.birdiesearch.com%2F"`。可用节点名用 `/proxies` 查；`DIRECT` 亦返回 Timeout 可排除"单节点被封"。
  3. 第三方独立出口旁证：`curl -x http://127.0.0.1:7897 'https://r.jina.ai/https://www.birdiesearch.com/'`（超时/422 = 站点对境外机房也不可达）。
- ⚠️ 坑：Clash TUN 开启时本机"直连"同样被接管，`--noproxy '*'` 或裸 socket 的结果**不能**作为真实直连证据；域名解析走 fake-ip(198.18.x)，真实 IP 需用 DoH(`https://1.1.1.1/dns-query?name=...&type=A`)查。
- 复发确认(2026-09-21 11:01 与 11:12 连续两次调度)：仍止于 `/api/user/sign` 的 `SSLEOFError`。当日复测：Clash 组『宝可梦』8/8 节点对目标站 `delay` 测试全失败(`Timeout` / `An error occurred in the delay test`)，**同一批节点 google/generate_204 正常**(308/90/42/188/43/258ms，仅 2 个节点自身超时)；`r.jina.ai` 旁证返回 `422 AssertionFailureError: page.goto: Timeout 15000ms exceeded`。→ 再次独立确认**站点侧不可达**，非节点/配置问题；两次调度均未推送、未耗额度、未写 `birdie_seen_items.json`。
- 复发确认(2026-09-23 11:00 调度)：同症状 `SSLEOFError(8, UNEXPECTED_EOF_WHILE_READING)` 止于 `/api/user/sign`。当日三步复测：①`google/generate_204` = **204（0.89s）** 出口正常 ②Clash `GLOBAL/delay?url=<目标站>` = `Timeout`（同日 GLOBAL 对 google 也 Timeout ⇒ 该 API 的组选择不可单独作证，①已足够）③`r.jina.ai` 旁证 = **422 `page.goto: Timeout 15000ms exceeded`**，而同一调用抓 `example.com` = 200 ⇒ 再次独立确认**站点侧不可达**。本轮零推送、零额度消耗、`birdie_seen_items.json` mtime 仍停 2026-08-07。★附加判据：`socket.create_connection` 能连上 443 只说明连到了 **Clash TUN 假 IP(198.18.0.110)**，**不构成站点可达证据**（勿被"TCP OK"误导）。
- 复发确认(2026-09-25 11:00 调度)：同症状 `SSLEOFError(8, UNEXPECTED_EOF_WHILE_READING)` 止于 `/api/user/sign`（`tools/bg_task.py` 启动 11:01:06、日志末写 11:01:12、`exit=1`、耗时 6s）。当日四步复测：①本机直连与 `-x http://127.0.0.1:7897` 的 `google/generate_204` 均 **204（1.15s / 1.01s）**；②Clash **逐节点 8/8** 对目标站全 `Timeout`，而**同一批 8 个节点 google 全部正常**（96/99/92/90/123/103/152/103ms）；③`r.jina.ai` 抓目标站 = **422 `AssertionFailureError: TimeoutError: navigating to ... waiting until "networkidle"`（19.5s）**，同一调用抓 `example.com` = **200（0.97s）**；④DNS 仍解析为 fake-ip `198.18.0.110` ⇒ 再次独立确认**站点侧不可达**。本轮零推送（脚本未跑到通知分支）、零额度消耗、`birdie_seen_items.json` sha1/mtime **完全未变**（`066c0695928cd83340c2673d3e4671cbaa89bac6` / `2026-08-07 11:00:50`，29 条）。
- ★**失败起点订正（2026-09-25，推翻本节旧记）**：旧记「自 2026-09-13 起每日失败、09-10 为最后一次真正跑通」**有误**。按 `sche_tasks/done/` 内全部 58 份 birdie 报告**逐份核 exit 码**：**2026-08-07 是最后一次真正跑通**（exit=0、当日「今日无新增命中」），**08-08 起每一份均为签到阶段失败**（含被关键词粗筛误判为成功的 08-13「⚠️ 采集失败，未推送」与 08-20「因 BirdieSearch 签到请求 SSL EOF 失败而中断」）⇒ **截至 2026-09-25 已连续 49 天静默失败**（08-08→09-25；其间仅 09-19 无报告）。唯一独立佐证 = 去重库 mtime 停在 **2026-08-07 11:00:50**（脚本只有跑到写库才会更新它），与报告序列一致。★教训：统计「连续失败天数」**必须逐份核 exit 码**——关键词粗筛会把「stdout 中**没有**『今日无新增命中』」这类**否定句**误判为成功。此类长期静默已远超人工介入阈值，应做**连续同因失败告警**而非继续静默重试。

## 已知噪音(勿误判为故障)
脚本 stderr 中出现的 numpy/sklearn 版本警告，来自 `llmcore`/`mykey.py` 导入链的后台线程噪音，与本任务无关，`exit_code=0` 即代表脚本本身执行成功。
