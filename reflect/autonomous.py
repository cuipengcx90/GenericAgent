# reflect/autonomous.py
import os as _os

INTERVAL = 1800
ONCE = False

_TODO = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '../temp/TODO.txt')

def _has_pending_todo():
    """检查TODO.txt是否存在且含未完成项 [ ]"""
    try:
        with open(_TODO, encoding='utf-8') as f:
            return any(line.strip().startswith('[ ]') for line in f)
    except FileNotFoundError:
        return False

def check():
    if not _has_pending_todo():
        return None  # 无待办→跳过LLM调用，避免空转
    return "[AUTO]🤖 用户已经离开超过30分钟，作为自主智能体，请阅读自动化sop，执行自动任务。"