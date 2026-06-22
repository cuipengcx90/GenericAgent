# Reflect 模式应用场景最佳实践

> **Reflect 模式**：让 Agent 在后台定期轮询，条件满足时自动执行任务并推送飞书通知。
> 你不在电脑前，Agent 也能自动感知环境变化并执行任务。

---

## 核心机制

```
check() 每 INTERVAL 秒调用一次
    ↓ 返回 None      → 零消耗，继续等待
    ↓ 返回任务字符串 → 触发 Agent 执行
Agent 执行完成
    ↓
on_done(result) 推送飞书通知
```

**关于 Token 消耗**：只有 `check()` 返回非 None 时才消耗 Token，轮询本身零消耗。

---

## 脚本结构模板

```python
INTERVAL = 60   # 轮询间隔（秒）

def check() -> str | None:
    """返回 None 不触发；返回字符串作为 Agent 任务"""
    if 触发条件:
        return "任务描述"
    return None

def on_done(result: str):
    """Agent 完成后回调，result 是 Agent 输出"""
    notify_feishu(f"通知内容：{result}")
```

**热重载**：修改脚本后自动生效，无需重启。

---

## 公共工具（reflect_common.py）

```python
from reflect_common import notify_feishu, Debounce

notify_feishu("消息内容")          # 直接推送飞书，不经过 Agent

db = Debounce(cooldown=1800)       # 防抖：同一 key 30 分钟内不重复触发
if db.should_fire("cpu_alert"):
    return "任务"
```

---

## 六大应用场景

### 场景一：系统资源监控

**文件**：`01_system_monitor.py`
**触发条件**：CPU / 内存 / 磁盘超过阈值
**Agent 任务**：列出占用最高进程，分析原因，给出处理建议
**飞书推送**：系统监控报告

```python
# 关键配置
CPU_THRESHOLD    = 85   # CPU 告警阈值 %
MEM_THRESHOLD    = 85   # 内存告警阈值 %
DISK_THRESHOLD   = 90   # 磁盘告警阈值 %
INTERVAL         = 60   # 每分钟检查
```

**最佳实践**：
- 配合 `Debounce(cooldown=1800)` 防止同一问题 30 分钟内重复告警
- CPU 阈值建议 85%+，避免正常峰值误报

---

### 场景二：邮件自动处理

**文件**：`02_email_handler.py`
**触发条件**：收件箱有未读邮件
**Agent 任务**：分类邮件类型，起草回复，给出处理建议
**飞书推送**：邮件处理报告

```python
# 关键配置（必填）
IMAP_HOST  = "imap.gmail.com"    # QQ邮箱: imap.qq.com
EMAIL_ADDR = "your@email.com"
EMAIL_PASS = "your_app_password" # 需开启"应用专用密码"
INTERVAL   = 120                 # 每2分钟检查
```

**最佳实践**：
- Gmail 需开启两步验证后生成应用专用密码
- QQ 邮箱需在设置中开启 IMAP 并获取授权码
- 已处理邮件 ID 存入 `_processed` 集合防止重复处理

---

### 场景三：每日定时报告

**文件**：`03_daily_report.py`
**触发条件**：每天指定时间（默认早9点）
**Agent 任务**：分析项目目录、梳理待跟进事项、给出今日工作建议
**飞书推送**：每日报告（含日期）

```python
# 关键配置
REPORT_HOUR   = 9     # 触发小时（24小时制）
REPORT_MINUTE = 0     # 触发分钟
REPORT_PROMPT = "..." # 自定义报告内容
INTERVAL      = 60    # 每分钟检查时间
```

**最佳实践**：
- 用 `_last_trigger_date` 记录已触发日期，防止同一天多次触发
- `REPORT_PROMPT` 可高度定制，例如接入数据库查询昨日数据

---

### 场景四：GitHub 仓库监控

**文件**：`04_github_monitor.py`
**触发条件**：有新 PR 或 Issue（1小时内创建）
**Agent 任务**：总结 PR 改动、分析 Issue 类型、给出处理建议和优先级
**飞书推送**：GitHub 动态报告

```python
# 关键配置（必填）
GITHUB_TOKEN = "ghp_xxxx"           # GitHub → Settings → Developer Settings → Fine-grained tokens
WATCH_REPOS  = ["username/repo"]    # 监控的仓库列表（可多个）
WATCH_PR     = True
WATCH_ISSUE  = True
INTERVAL     = 300                  # 每5分钟检查
```

**最佳实践**：
- Token 只需 `repo:read` 权限，最小授权原则
- `_seen_prs` / `_seen_issues` 防止重复处理同一条目
- 每次只处理一条，避免 Agent 任务过长

---

### 场景五：文件夹自动归档

**文件**：`05_folder_watcher.py`
**触发条件**：监控目录出现新文件（下载完成后）
**Agent 任务**：识别文件用途，规范化命名，自动移动到对应子目录
**飞书推送**：归档操作报告

```python
# 关键配置
WATCH_DIR    = "~/Downloads"         # 监控目录
ARCHIVE_DIR  = "~/Documents/归档"   # 归档目标根目录
STABLE_SECS  = 10                   # 文件稳定判断（秒），避免处理下载中的文件
INTERVAL     = 15                   # 每15秒检查
```

**最佳实践**：
- `STABLE_SECS` 建议 ≥ 10 秒，避免处理仍在下载的文件
- 忽略 `.crdownload` `.part` 等临时文件扩展名
- 已触发文件存入 `_processing` 集合防止重复处理

---

### 场景六：价格/行情监控

**文件**：`06_price_alert.py`
**触发条件**：指定币种在时间窗口内涨跌超过阈值
**Agent 任务**：分析价格异动原因、判断短期趋势、给出操作建议
**飞书推送**：行情播报

```python
# 关键配置
WATCH_COINS   = ["bitcoin", "ethereum"]  # CoinGecko ID，免费无需注册
ALERT_CHANGE  = 0.05   # 触发阈值，0.05 = 5% 波动
WINDOW_SECS   = 600    # 统计窗口（10分钟）
INTERVAL      = 120    # 每2分钟检查
```

**最佳实践**：
- CoinGecko 免费 API，无需注册，速率限制约每分钟30次
- `Debounce(cooldown=1800)` 防止同一币种 30 分钟内重复告警
- 可在 `WATCH_COINS` 添加任意 CoinGecko 支持的币种 ID

---

## 启动方式

```bash
# 单个启动
python agentmain.py --reflect reflect_scripts/01_system_monitor.py

# 多个场景同时运行（每个开一个终端）
python agentmain.py --reflect reflect_scripts/01_system_monitor.py &
python agentmain.py --reflect reflect_scripts/06_price_alert.py &
```

---

## 通用最佳实践

| 原则 | 说明 |
|------|------|
| **防抖必用** | 有持续性条件（CPU高/价格跌）必须加 `Debounce`，避免刷屏 |
| **异常捕获** | `check()` 内所有外部调用包 `try/except`，打印错误继续运行 |
| **稳定性判断** | 监控文件/下载时等待文件稳定再处理，避免读取未完成的文件 |
| **去重记录** | 已处理的 ID/路径存入集合，防止重复触发 |
| **间隔合理** | 外部 API 调用（邮件/GitHub/行情）INTERVAL ≥ 120s，避免限速 |
| **任务精确** | `check()` 返回的任务描述要具体，Agent 执行效果更好 |

---

## 文件结构

```
reflect_scripts/
├── reflect_common.py      # 公共工具：飞书推送 + 防抖
├── 01_system_monitor.py   # 系统资源监控
├── 02_email_handler.py    # 邮件自动处理
├── 03_daily_report.py     # 每日定时报告
├── 04_github_monitor.py   # GitHub 仓库监控
├── 05_folder_watcher.py   # 文件夹自动归档
└── 06_price_alert.py      # 价格/行情监控
```