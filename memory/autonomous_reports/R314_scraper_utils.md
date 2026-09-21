
# R314 — scraper_utils.py 网页内容提取工具封装

## 任务
封装 bs4 + lxml，提供 CSS选择器/XPath/表格提取/链接采集/正文净化等能力，与 http_utils 联动。

## 产出
`memory/scraper_utils.py` — 10 个公开函数 + CLI

### 函数清单
| 函数 | 说明 |
|------|------|
| `parse(html, parser)` | HTML字符串→BeautifulSoup对象 |
| `fetch_parse(url, ...)` | 抓取URL并解析（联动http_utils） |
| `css_select(soup_or_html, selector)` | CSS选择器提取元素列表 |
| `xpath_select(html, xpath)` | XPath提取（基于lxml） |
| `extract_links(soup, base_url, ...)` | 提取所有链接（绝对化+过滤） |
| `extract_table(soup, index)` | 提取HTML表格→list[dict] |
| `extract_tables_all(soup)` | 提取页面所有表格 |
| `extract_text(soup, selector)` | 提取净化文本（去script/style/hidden） |
| `extract_meta(soup)` | 提取meta信息（title/description/og等） |
| `clean_html(html)` | 移除script/style/注释/hidden，保留结构 |

### 设计亮点
- **输入统一**：所有函数接受 HTML字符串 或 BeautifulSoup 对象
- **http_utils联动**：`fetch_parse()` 直接从URL到解析完成一步到位
- **净化完整**：extract_text 移除 script/style/noscript/display:none/hidden 属性元素
- **链接绝对化**：base_url 自动将相对路径转绝对，支持同域过滤+扩展名过滤
- **表格智能**：自动识别表头，补齐缺失列，空header自动命名

### 修复记录
- extract_text 初版遗漏 hidden 元素过滤，已补充 `display:none` 和 `hidden` 属性移除

## 测试结果
4/4 场景全通过：
- ✅ Scenario 1: example.com — fetch_parse + CSS + XPath + meta + links + text
- ✅ Scenario 2: 表格提取 — 3行dict, key/value正确
- ✅ Scenario 3: clean_html — script/style/comment/hidden全移除
- ✅ Scenario 4: httpbin.org/html — 真实页面解析（Moby-Dick全文提取）

## 依赖
- beautifulsoup4 4.12.3, lxml 5.2.1, httpx (via http_utils)
