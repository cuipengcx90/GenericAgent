"""
场景3：每日定时报告
每天指定时间自动生成报告并推送到飞书
启动：python agentmain.py --reflect reflect_scripts/03_daily_report.py
"""
import sys
from datetime import datetime
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu

INTERVAL = 60   # 每分钟检查一次时间

# ===== 配置区 =====
REPORT_HOUR   = 9     # 触发小时（24小时制）
REPORT_MINUTE = 0     # 触发分钟
REPORT_PROMPT = (
    "请帮我生成今日工作计划报告：\n"
    "1. 查看 ~/projects/ 目录下最近修改的文件，了解昨天在做什么\n"
    "2. 列出今天可能需要跟进的事项\n"
    "3. 给出今日工作重点建议（3条以内）\n"
    "输出格式简洁，适合飞书消息展示。"
)
# ==================

_last_trigger_date = None

def check():
    global _last_trigger_date
    now = datetime.now()
    today = now.date()
    if (now.hour == REPORT_HOUR and now.minute == REPORT_MINUTE
            and _last_trigger_date != today):
        _last_trigger_date = today
        return REPORT_PROMPT
    return None

def on_done(result: str):
    date_str = datetime.now().strftime("%Y-%m-%d %A")
    notify_feishu(f"📊 每日报告｜{date_str}\n\n{result}")