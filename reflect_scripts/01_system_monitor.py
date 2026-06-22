"""
场景1：系统资源监控
监控 CPU / 内存 / 磁盘 异常，触发 Agent 排查并推飞书
启动：python agentmain.py --reflect reflect_scripts/01_system_monitor.py
"""
import sys, psutil
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu, Debounce

INTERVAL = 60   # 每分钟检查一次

# 阈值配置
CPU_THRESHOLD    = 85   # CPU 使用率 %
MEM_THRESHOLD    = 85   # 内存使用率 %
DISK_THRESHOLD   = 90   # 磁盘使用率 %
DISK_PATH        = "/"  # 监控的磁盘路径

_db = Debounce(cooldown=1800)  # 同一问题 30 分钟内不重复触发

def check():
    alerts = []

    cpu = psutil.cpu_percent(interval=2)
    if cpu > CPU_THRESHOLD and _db.should_fire("cpu"):
        alerts.append(f"CPU使用率 {cpu:.0f}% 超过{CPU_THRESHOLD}%阈值")

    mem = psutil.virtual_memory()
    if mem.percent > MEM_THRESHOLD and _db.should_fire("mem"):
        alerts.append(f"内存使用率 {mem.percent:.0f}% 超过{MEM_THRESHOLD}%阈值，已用{mem.used//1024//1024//1024:.1f}GB/共{mem.total//1024//1024//1024:.1f}GB")

    disk = psutil.disk_usage(DISK_PATH)
    if disk.percent > DISK_THRESHOLD and _db.should_fire("disk"):
        alerts.append(f"磁盘使用率 {disk.percent:.0f}% 超过{DISK_THRESHOLD}%阈值，剩余{disk.free//1024//1024//1024:.1f}GB")

    if alerts:
        desc = "、".join(alerts)
        return f"系统资源告警：{desc}。请列出占用最高的进程（top 5），分析原因，给出处理建议。"
    return None

def on_done(result: str):
    notify_feishu(f"🖥️ 系统监控报告\n\n{result}")