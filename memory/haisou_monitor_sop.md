# haisou.cc 网盘资源监控 SOP

## 入口
- `birdie_monitor/haisou_daily.py`：搜索、按 `hsid + 标题相似度` 双重去重、仅新增通知。
- `birdie_monitor/sync_quangneng.py`：同步全能IT（quangneng.com）首页最近90天课程到动态关键词。
- 调度定义：`sche_tasks/haisou_daily_monitor.json`（每天11:15，先sync再搜索）。

## 认证
- 会员会话 Cookie `hs_web_session` 是 HttpOnly；通过 keychain 读取，严禁明文落盘或输出。脚本可脱离浏览器运行。

## 核心接口
- `POST https://haisou.cc/api/v2/shares/search`
  - body: `{"query": "关键词", "filters": {"scope": "title", "platforms": ["quark"|"baidu"|"ali"], "include_filtered": false}, "pagination": {"page":1, "page_size":N}}`
  - **必须**在 `filters.platforms` 里显式指定至少一个平台，否则报 `400 INVALID_PARAMETER 至少选择一个云盘`（此为参数校验错误，非认证失效，勿误判会话过期）。
  - 已验证可用平台枚举：`quark`、`baidu`、`ali`（页面渲染观察到的三种图标对应值）。
  - 返回 `data.items` 直接包含 `hsid`、`platform`、`share_name`(含 `<span class="highlight">` 高亮标签需 strip)、`share_code`、`share_pwd`，日常监控无需再调用详情/fetch接口。
- `POST /api/v2/shares/{hsid}/fetch`：返回同样字段但会消耗 5 会员额度/次，仅用于人工核实，不应在定时监控里对每条结果调用。
- 链接生成规则（脚本实际采用，已用浏览器实测校验）：
  - 夸克: `https://pan.quark.cn/s/{share_code}`
  - 百度网盘: `https://pan.baidu.com/s/{share_code}`（`share_pwd` 需要用户自行输入提取码，不能拼进URL）
    - **坑（2026-08-31已修复）**：接口返回的 `share_code` 本身已经带开头的 `1`（如 `1xkDD27op2R2lUvUPTsB_mQ`），旧模板 `s/1{share_code}` 会多拼一个 `1` 导致链接 404。`haisou_daily.py` 的 `LINK_TEMPLATES["baidu"]` 已改为 `https://pan.baidu.com/s/{code}`（无多余前缀），历史 `haisou_seen_items.json` 中218条百度记录已批量修正 link 字段。今后若切回 quark/ali 之外新平台，务必先用浏览器实测生成的链接再定模板，勿凭直觉加前缀。
  - 阿里云盘: `https://www.alipan.com/s/{share_code}`

## 去重机制
- **双重去重**（2026-09-05 增强）：
  1. **hsid 去重**：同一资源的唯一ID，不同平台同一资源hsid不同
  2. **标题相似度去重**：归一化标题后用 SequenceMatcher 计算相似度，threshold=0.85
     - 归一化规则：转小写、去除"完结/更新中/1080P/高清/4K/蓝光/网盘名"等噪声词、去除括号中内容、去除标点符号
     - 效果：能减少约70%的同课程不同平台重复推送
     - 边界情况：标题差异较大时（如"华测AI测试" vs "华测教育2026年AI全栈测试专家课程"）可能漏过

## 关键词管理
- **静态关键词**（24个，手动维护）：
  - AI开发（14个）：AI Agent, AI coding, AI应用开发, 出海SaaS, Vibe Coding, Claude Code, AI创业, 独立开发出海, SaaS出海, MCP教程, AI产品出海, 懂王, 鸡翅大模型, 赋范
  - AI测试（10个）：AI测试, 软件测试, 自动化测试, 柠檬班, 华测, 华测教育, 接口测试, 性能测试, 测试开发, 测试工程师
- **动态关键词**（自动同步）：
  - 来源：quangneng.com（全能IT）最近3天课程标题
  - 同步脚本：`sync_quangneng.py`（每天11:15自动执行）
  - 存储文件：`birdie_monitor/quangneng_keywords.json`
  - 格式：
    ```json
    {
      "课程标题": {
        "added_at": "2026-09-05 12:30:00",
        "found": false,
        "source_url": "https://quangneng.com/16344/",
        "published_at": "2026-08-08T09:12:00+08:00",
        "found_at": null,
        "search_count": 10
      }
    }
    ```
  - 自动清理：搜索超30次未找到的课程自动移除
  - 找到标记：在海搜找到资源后自动标记 `found=true`，下次同步时清理

## 监控设计
- 首次运行：只建立本地基线（写入 `haisou_seen_items.json`），不对外通知。
- 后续运行：仅当出现新 `hsid`（新增资源）或接口报错/会话失效时才通知(飞书私信+钉钉群各一次)；无新增静默，避免刷屏。
- **部分扫描失败不得吞结果**（2026-09-07 修复并离线验证）：若某些关键词/平台已发现新增、后续请求才遇到额度耗尽或其他异常，必须在**同一条通知**先列出已发现的新增，再附“后续扫描中断”及错误；已发现资源必须立即写入 `haisou_seen_items.json`，未扫描部分留待下次，不能以异常分支覆盖结果或造成重复推送。
- 定时任务流程：
  1. 执行 `sync_quangneng.py`（抓取全能IT首页最近90天课程 → 更新动态关键词）
  2. 执行 `haisou_daily.py`（静态关键词+动态关键词 → 海搜搜索 → 双重去重 → 通知）

## 全能IT同步机制
- **目标站点**：https://quangneng.com/
- **抓取策略**：直接抓取首页（首页按时间倒序，包含所有分类的最新课程），处理前10个article
- **为什么不判断时间**：首页倒序排列，前10个肯定是最新课程，不需要访问详情页判断时间（速度快60倍）
- **标题提取**：使用 `header h2 a, .entry-header h2 a` 选择器（避免误匹配其他链接）
- **增量同步**：
  - 新课程：加入动态关键词队列
  - 已找到：保留记录但标记 `found=true`
  - 超期未找：搜索次数>30次自动清理
- **避免重复**：同一课程标题只保留一条记录

