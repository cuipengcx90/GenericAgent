"""
tmux_utils.py — tmux 持久会话管理工具封装
依赖: tmux (brew install tmux), tmux 3.x+
功能:
  - new_session(name)          : 创建后台tmux会话
  - kill_session(name)         : 销毁会话
  - session_exists(name)       : 判断会话是否存在
  - list_sessions()            : 列出所有会话 → list[dict]
  - send_keys(name, keys)      : 向会话发送按键/命令
  - run_command(name, cmd)     : 发送命令+Enter，等待输出
  - capture_pane(name)         : 捕获会话当前屏幕内容 → str
  - wait_for_output(name, pattern, timeout) : 等待输出匹配模式
  - ensure_session(name)       : 不存在则创建，幂等

坑:
  - tmux list-sessions 在无会话时 returncode=1 且 stderr 有错误信息，需特判
  - capture-pane 默认只捕获可见区域，加 -S - 可捕获全部历史（内容量大）
  - send-keys 不自动加 Enter，需显式传 'Enter'
  - new-session -d 为后台创建，不占用终端；不加 -d 会尝试 attach
  - 会话名含特殊字符(空格/点)会导致 tmux 命令解析出错，建议用下划线
  - run_command 使用 sleep 轮询，不适合耗时很长的命令，超时请用 wait_for_output
  - capture-pane 输出末尾有大量空格/空行，已做 strip 处理
"""

import subprocess
import shutil
import time
import re
from typing import Optional, List, Dict

# ── 内部工具 ──────────────────────────────────────────────────

def _require_tmux():
    if not shutil.which('tmux'):
        raise RuntimeError("tmux未找到，请 brew install tmux")

def _run(cmd: list, check: bool = False) -> subprocess.CompletedProcess:
    """运行tmux命令，返回CompletedProcess，不自动抛异常（tmux有些命令失败是正常的）"""
    return subprocess.run(cmd, capture_output=True, text=True)


# ── 会话管理 ──────────────────────────────────────────────────

def session_exists(name: str) -> bool:
    """
    检查指定tmux会话是否存在。

    示例:
        if session_exists('myapp'):
            print('会话运行中')
    """
    _require_tmux()
    r = _run(['tmux', 'has-session', '-t', name])
    return r.returncode == 0


def list_sessions() -> List[Dict[str, str]]:
    """
    列出所有tmux会话，返回 list[dict]，每项含 name/windows/created/status。

    示例:
        for s in list_sessions():
            print(s['name'], s['status'])
    """
    _require_tmux()
    r = _run(['tmux', 'list-sessions', '-F',
              '#{session_name}|#{session_windows}|#{session_created}|#{session_attached}'])
    if r.returncode != 0:
        return []  # 无会话时 returncode=1，正常情况
    sessions = []
    for line in r.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split('|')
        if len(parts) >= 4:
            sessions.append({
                'name': parts[0],
                'windows': parts[1],
                'created': parts[2],
                'status': 'attached' if parts[3] == '1' else 'detached'
            })
    return sessions


def new_session(
    name: str,
    width: int = 220,
    height: int = 50,
    start_dir: Optional[str] = None
) -> str:
    """
    创建新的后台tmux会话（-d 模式，不占终端）。
    如果同名会话已存在，直接返回（幂等）。

    Args:
        name:      会话名（建议仅用字母数字下划线）
        width:     终端列数，默认220
        height:    终端行数，默认50
        start_dir: 工作目录，默认当前目录

    返回: 会话名

    示例:
        new_session('myapp', start_dir='/tmp')
    """
    _require_tmux()
    if session_exists(name):
        return name
    cmd = ['tmux', 'new-session', '-d', '-s', name, '-x', str(width), '-y', str(height)]
    if start_dir:
        cmd += ['-c', start_dir]
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"创建会话失败: {r.stderr.strip()}")
    return name


def ensure_session(name: str, **kwargs) -> str:
    """
    幂等创建会话：存在则返回，不存在则创建。

    示例:
        ensure_session('worker')
        send_keys('worker', 'python server.py', enter=True)
    """
    if not session_exists(name):
        new_session(name, **kwargs)
    return name


def kill_session(name: str) -> bool:
    """
    销毁tmux会话。不存在时返回False，不抛异常。

    示例:
        kill_session('myapp')
    """
    _require_tmux()
    if not session_exists(name):
        return False
    r = _run(['tmux', 'kill-session', '-t', name])
    return r.returncode == 0


# ── 命令发送 ──────────────────────────────────────────────────

def send_keys(name: str, keys: str, enter: bool = True) -> None:
    """
    向tmux会话发送按键序列。

    Args:
        name:  会话名（支持 'session:window.pane' 格式）
        keys:  发送内容，如 'ls -la' 或 'C-c'（Ctrl+C）
        enter: 是否自动追加 Enter，默认True

    示例:
        send_keys('myapp', 'python server.py')
        send_keys('myapp', 'C-c', enter=False)  # Ctrl+C中断
    """
    _require_tmux()
    cmd = ['tmux', 'send-keys', '-t', name, keys]
    if enter:
        cmd.append('Enter')
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"send-keys失败({name}): {r.stderr.strip()}")


def capture_pane(name: str, history: bool = False) -> str:
    """
    捕获tmux会话当前可见屏幕内容（或全部历史）。

    Args:
        name:    会话名
        history: True=捕获全部历史(慢)，False=仅可见区域(快)

    返回: 屏幕内容字符串（已strip）

    示例:
        out = capture_pane('myapp')
        print(out)
    """
    _require_tmux()
    cmd = ['tmux', 'capture-pane', '-t', name, '-p']
    if history:
        cmd += ['-S', '-']
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"capture-pane失败({name}): {r.stderr.strip()}")
    return r.stdout.strip()


def run_command(
    name: str,
    cmd: str,
    wait: float = 0.5,
    capture: bool = True
) -> Optional[str]:
    """
    向会话发送命令+Enter，等待指定时间后捕获输出。
    适合快速命令（<1s），耗时命令请用 wait_for_output。

    Args:
        name:    会话名
        cmd:     shell命令字符串
        wait:    等待秒数，默认0.5s
        capture: 是否捕获并返回输出，默认True

    返回: 屏幕内容字符串 或 None

    示例:
        out = run_command('myapp', 'echo hello')
        print(out)
    """
    send_keys(name, cmd, enter=True)
    if wait > 0:
        time.sleep(wait)
    if capture:
        return capture_pane(name)
    return None


def wait_for_output(
    name: str,
    pattern: str,
    timeout: float = 30.0,
    poll_interval: float = 0.3
) -> Optional[str]:
    """
    轮询等待会话输出匹配正则pattern，超时返回None。

    Args:
        name:          会话名
        pattern:       正则表达式，如 r'\\$\\s*$'(等待shell提示符)
        timeout:       超时秒数，默认30s
        poll_interval: 轮询间隔秒数，默认0.3s

    返回: 匹配时的屏幕内容 或 None(超时)

    示例:
        # 等待Python REPL就绪
        wait_for_output('repl', r'>>> ')
        send_keys('repl', 'import os')
    """
    _require_tmux()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            content = capture_pane(name)
            if re.search(pattern, content):
                return content
        except RuntimeError:
            pass
        time.sleep(poll_interval)
    return None


# ── 快捷工具 ──────────────────────────────────────────────────

def run_bg(
    session_name: str,
    cmd: str,
    start_dir: Optional[str] = None
) -> str:
    """
    在新（或已有）会话中后台运行命令，不等待结果。
    常用于启动长期运行的服务/守护进程。

    返回: 会话名

    示例:
        run_bg('server', 'python -m http.server 8888', start_dir='/tmp')
    """
    ensure_session(session_name, start_dir=start_dir)
    send_keys(session_name, cmd, enter=True)
    return session_name


def get_output_lines(name: str, last_n: int = 20) -> List[str]:
    """
    获取会话屏幕最后N行非空内容。

    示例:
        lines = get_output_lines('myapp', last_n=10)
    """
    content = capture_pane(name)
    lines = [l for l in content.splitlines() if l.strip()]
    return lines[-last_n:]