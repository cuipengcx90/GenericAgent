
# R34 TrendRadar供应链行业热点周报模板

## 执行信息
- **执行时间**: 2026-04-24
- **数据来源**: TrendRadar output/news + output/rss SQLite数据库
- **实测数据**: 2026-04-24共794条热搜，3条供应链相关（0.38%）

---

## 一、数据现状分析

### 平台覆盖（8大平台）
| 平台 | 条数 | 占比 | 供应链相关度 |
|------|------|------|-------------|
| 微博 | 155 | 19.5% | ⭐⭐ |
| 今日头条 | 146 | 18.4% | ⭐⭐⭐ |
| 百度 | 113 | 14.2% | ⭐⭐ |
| B站热搜 | 91 | 11.5% | ⭐ |
| 抖音 | 72 | 9.1% | ⭐⭐ |
| 知乎 | 56 | 7.1% | ⭐⭐⭐ |
| 贴吧 | 45 | 5.7% | ⭐ |
| 澎湃 | 38 | 4.8% | ⭐⭐⭐⭐ |

### 供应链关键词命中率
- **总热搜**: 794条
- **供应链相关**: 3条（0.38%）
- **命中示例**:
  - `供应链重构成商业航天降本关键` (今日头条 rank=8)
  - `中国电网开启近100亿具身智能设备采购与投资` (财联社 rank=9)
  - `库存逼近历史新低！股市一片狂欢` (华尔街见闻 rank=10)

### 关键词库（20个）
```python
SUPPLY_KEYWORDS = [
    # 中文核心
    '供应链', '仓储', '物流', '采购', '库存', '配送', '餐饮', '零售',
    '进销存', '订货', '补货', '供应商', '食材', '原料', '冷链',
    # 英文补充
    'supply chain', 'logistics', 'inventory', 'procurement', 'warehouse'
]
```

---

## 二、周报模板代码

### 模板1：直接查询SQLite（推荐）

```python
#!/usr/bin/env python3
"""
TrendRadar供应链行业热点周报生成器
路径: /Users/cuip/projects/GenericAgent/reflect_scripts/supply_chain_weekly_report.py
"""
import sqlite3, os, sys
from datetime import datetime, timedelta
from collections import Counter

# 供应链关键词
SUPPLY_KEYWORDS = [
    '供应链', '仓储', '物流', '采购', '库存', '配送', '餐饮', '零售',
    '进销存', '订货', '补货', '供应商', '食材', '原料', '冷链', '商超',
    'supply chain', 'logistics', 'inventory', 'procurement', 'warehouse'
]

def generate_weekly_report(days=7):
    """生成过去N天的供应链热点周报"""
    base_dir = '/Users/cuip/projects/TrendRadar/output/news'
    
    # 收集过去N天的数据
    all_supply_news = []
    today = datetime.now()
    
    for i in range(days):
        date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        db_path = f'{base_dir}/{date}.db'
        if not os.path.exists(db_path):
            continue
        
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT title, platform_id, rank, url, first_crawl_time 
            FROM news_items 
            ORDER BY rank ASC
        """)
        rows = cur.fetchall()
        conn.close()
        
        # 过滤供应链相关
        for row in rows:
            if any(k in row[0] for k in SUPPLY_KEYWORDS):
                all_supply_news.append({
                    'date': date,
                    'title': row[0],
                    'platform': row[1],
                    'rank': row[2],
                    'url': row[3],
                    'time': row[4]
                })
    
    # 生成报告
    report = f"""
# 供应链行业热点周报
**统计周期**: {(today - timedelta(days=days-1)).strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')}
**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## 📊 数据概览
- **热点总数**: {len(all_supply_news)}条
- **覆盖平台**: {len(set(n['platform'] for n in all_supply_news))}个
- **平均排名**: {sum(n['rank'] for n in all_supply_news) / len(all_supply_news):.1f} (越小越热)

---

## 🔥 TOP热点（按排名）
"""
    
    # 按排名排序，取前10
    top_news = sorted(all_supply_news, key=lambda x: x['rank'])[:10]
    for i, n in enumerate(top_news, 1):
        report += f"\n### {i}. {n['title']}\n"
        report += f"- **平台**: {n['platform']} | **排名**: {n['rank']} | **日期**: {n['date']}\n"
        report += f"- **链接**: {n['url']}\n"
    
    # 平台分布
    platform_dist = Counter(n['platform'] for n in all_supply_news)
    report += "\n\n---\n\n## 📈 平台分布\n"
    for platform, count in platform_dist.most_common():
        report += f"- **{platform}**: {count}条\n"
    
    # 关键词云
    keyword_hits = Counter()
    for n in all_supply_news:
        for kw in SUPPLY_KEYWORDS:
            if kw in n['title']:
                keyword_hits[kw] += 1
    
    report += "\n\n---\n\n## 🏷️ 关键词热度\n"
    for kw, count in keyword_hits.most_common(10):
        report += f"- **{kw}**: {count}次\n"
    
    return report, all_supply_news

def push_to_feishu(report_text):
    """推送到飞书群"""
    sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
    from notify_feishu_group import notify_group
    
    # 截取前2000字（飞书限制）
    summary = report_text[:2000]
    if len(report_text) > 2000:
        summary += "\n\n...(完整报告见附件)"
    
    notify_group(summary)
    print("✅ 已推送到飞书群")

if __name__ == '__main__':
    report, data = generate_weekly_report(days=7)
    
    # 保存到文件
    output_path = f'/Users/cuip/projects/GenericAgent/temp/supply_chain_weekly_{datetime.now().strftime("%Y%m%d")}.md'
    with open(output_path, 'w') as f:
        f.write(report)
    print(f"✅ 报告已保存: {output_path}")
    
    # 推送到飞书（可选）
    # push_to_feishu(report)
```

---

### 模板2：通过MCP接口（需服务运行）

```python
#!/usr/bin/env python3
"""
通过TrendRadar MCP接口生成周报（需MCP服务运行在9090端口）
"""
import requests, json, sys
from datetime import datetime, timedelta

BASE = "http://localhost:9090/mcp"
SUPPLY_KEYWORDS = ['供应链', '仓储', '物流', '采购', '库存', '配送', '餐饮', '零售']

def mcp_call(method, params):
    """MCP 3步握手调用"""
    # Step1: initialize
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, 
                   "clientInfo": {"name": "ga", "version": "1.0"}}
    }
    r = requests.post(BASE, json=init_payload, 
                     headers={"Accept": "application/json, text/event-stream"})
    session_id = r.headers.get("Mcp-Session-Id")
    
    # Step2: notifications/initialized
    notify_payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    requests.post(BASE, json=notify_payload, 
                 headers={"Mcp-Session-Id": session_id})
    
    # Step3: 工具调用
    tool_payload = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}
    r = requests.post(BASE, json=tool_payload, 
                     headers={"Mcp-Session-Id": session_id})
    return r.json()

def generate_weekly_report_via_mcp():
    """通过MCP接口生成周报"""
    # 获取最近100条新闻（MCP不支持date_range，需手动过滤）
    result = mcp_call("tools/call", {
        "name": "get_latest_news",
        "arguments": {"limit": 100}
    })
    
    news_list = result.get("result", {}).get("content", [{}])[0].get("text", "[]")
    news_data = json.loads(news_list)
    
    # 过滤供应链相关
    supply_news = []
    for item in news_data.get("data", []):
        if any(k in item['title'] for k in SUPPLY_KEYWORDS):
            supply_news.append(item)
    
    # 生成报告（同模板1）
    report = f"# 供应链行业热点周报\n\n共{len(supply_news)}条相关热点\n\n"
    for i, n in enumerate(supply_news[:10], 1):
        report += f"{i}. [{n['platform_name']}] {n['title']} (rank={n['rank']})\n"
        report += f"   {n['url']}\n\n"
    
    return report

if __name__ == '__main__':
    try:
        report = generate_weekly_report_via_mcp()
        print(report)
    except Exception as e:
        print(f"❌ MCP调用失败: {e}")
        print("提示：确保TrendRadar MCP服务运行在9090端口")
```

---

## 三、定时任务配置

### 方案1：scheduled_task_sop集成

```python
# 在 /Users/cuip/projects/GenericAgent/reflect_scripts/scheduled_tasks.py 中添加

from scheduled_task_sop import ScheduledTask

# 每周一早上9点生成周报
supply_chain_weekly = ScheduledTask(
    name="supply_chain_weekly_report",
    schedule="0 9 * * 1",  # cron: 每周一9:00
    script_path="/Users/cuip/projects/GenericAgent/reflect_scripts/supply_chain_weekly_report.py",
    description="供应链行业热点周报（过去7天）",
    notify_on_failure=True
)
```

### 方案2：macOS launchd

```xml
<!-- ~/Library/LaunchAgents/com.ga.supply-chain-weekly.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ga.supply-chain-weekly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/anaconda3/bin/python3</string>
        <string>/Users/cuip/projects/GenericAgent/reflect_scripts/supply_chain_weekly_report.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/supply_chain_weekly.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/supply_chain_weekly.err</string>
</dict>
</plist>
```

加载任务：
```bash
launchctl load ~/Library/LaunchAgents/com.ga.supply-chain-weekly.plist
launchctl list | grep supply-chain
```

---

## 四、推送方案

### 方案1：飞书群推送（推荐）

```python
def push_to_feishu_group(report_md):
    """推送Markdown格式周报到飞书群"""
    sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
    from notify_feishu_group import notify_group
    
    # 飞书支持Markdown，但有长度限制
    if len(report_md) > 3000:
        # 截取摘要
        summary = report_md[:2800] + "\n\n...(完整报告见附件)"
        notify_group(summary)
    else:
        notify_group(report_md)
```

### 方案2：飞书私信推送

```python
def push_to_feishu_dm(report_md):
    """推送到飞书私信"""
    sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
    from reflect_common import notify_feishu
    notify_feishu(report_md[:2000])  # 私信限制更严格
```

### 方案3：邮件推送

```python
def push_to_email(report_md, to_email):
    """发送邮件（需配置SMTP）"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    msg = MIMEMultipart()
    msg['Subject'] = f'供应链行业热点周报 {datetime.now().strftime("%Y-%m-%d")}'
    msg['From'] = 'ga@example.com'
    msg['To'] = to_email
    
    msg.attach(MIMEText(report_md, 'plain', 'utf-8'))
    
    # SMTP配置（示例）
    # smtp = smtplib.SMTP('smtp.gmail.com', 587)
    # smtp.starttls()
    # smtp.login('user', 'pass')
    # smtp.send_message(msg)
    # smtp.quit()
```

---

## 五、扩展建议

### 5.1 AI增强版（调用LLM总结）

```python
def ai_summarize(news_list):
    """调用GA的LLM能力生成周报摘要"""
    sys.path.insert(0, '/Users/cuip/projects/GenericAgent')
    from llmcore import ask_llm
    
    prompt = f"""
    以下是本周供应链行业热点新闻（{len(news_list)}条）：
    
    {chr(10).join(f"{i+1}. {n['title']}" for i, n in enumerate(news_list[:20]))}
    
    请生成一份200字的周报摘要，包括：
    1. 核心趋势（2-3条）
    2. 值得关注的事件
    3. 对餐饮/零售供应链的影响
    """
    
    summary = ask_llm(prompt)
    return summary
```

### 5.2 多维度分析

```python
def advanced_analysis(news_list):
    """多维度分析"""
    analysis = {
        "sentiment": analyze_sentiment(news_list),  # 情感分析
        "topics": extract_topics(news_list),        # 主题聚类
        "entities": extract_entities(news_list),    # 实体识别（公司/产品）
        "trend": detect_trend(news_list)            # 趋势检测
    }
    return analysis
```

### 5.3 与R33 AI化节点联动

```python
def link_to_ai_nodes(news_list):
    """将热点与R33的AI化节点关联"""
    # 示例：检测"补货"相关热点 → 触发智能补货预测
    for news in news_list:
        if '补货' in news['title'] or '库存' in news['title']:
            # 调用R33节点1：智能补货预测
            trigger_replenishment_suggest(news)
```

---

## 六、实施路线

### Phase 1（第1周）：基础版上线
1. 部署模板1代码到 `reflect_scripts/`
2. 配置定时任务（每周一9:00）
3. 测试飞书群推送

### Phase 2（第2周）：优化迭代
1. 扩充关键词库（根据实际命中率调整）
2. 增加RSS源过滤（Hacker News/GitHub Trending中的供应链项目）
3. 优化报告格式（图表/排版）

### Phase 3（第3-4周）：AI增强
1. 集成LLM生成摘要
2. 情感分析+主题聚类
3. 与R33 AI化节点联动

---

## 七、总结

| 维度 | 结论 |
|------|------|
| 数据可用性 | ✅ SQLite数据完整，8平台794条/天 |
| 供应链覆盖率 | ⚠️ 0.38%（需扩充关键词或增加垂直源）|
| 推荐方案 | 模板1（直接查SQLite）+ 飞书群推送 |
| 定时任务 | scheduled_task_sop集成（每周一9:00）|
| 扩展方向 | AI摘要 + 与R33节点联动 |

**立即可用**：模板1代码已完整，复制到 `reflect_scripts/supply_chain_weekly_report.py` 即可运行。

**关键改进点**：
1. 扩充关键词库（当前20个 → 建议50+）
2. 增加垂直源（36氪/虎嗅/亿邦动力等供应链媒体）
3. RSS源中GitHub Trending的供应链开源项目
