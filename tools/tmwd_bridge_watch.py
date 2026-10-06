#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TMWebDriver 独立桥常驻监督（脱离 Galley 座舱）。

桥是什么
--------
L 站自动化依赖一个本机小服务（"桥"）：
  127.0.0.1:18765 (WebSocket) ← Chrome 扩展 assets/tmwd_cdp_bridge 连这里
  127.0.0.1:18766 (HTTP /link) ← 脚本发指令（get_all_sessions / execute_js …）
历史上桥由 Galley 座舱的 runner.workbench_bridge 顺手拉起 ⇒ 座舱不开就没桥
（2026-10-06：座舱 16:47 才开，之前 18766 拒连，L 站守护空转/被看门狗反复重启）。

★ TMWebDriver.py 的 __main__ 没有阻塞语句 ⇒ 单独跑它构造完 daemon 线程即被杀、
  进程秒退 —— 这就是它从未被独立常驻的原因。本脚本用 -c + 常驻 sleep 循环补上。

本脚本（launchd 常驻，监督循环）
--------------------------------
每轮：
  1) probe()：探 127.0.0.1:<port+1> 且真发一次只读命令 /link get_all_sessions
  2) UP ⇒ 别人（座舱）的桥在用：让位，睡 60s 复查（绝不抢端口）
     PORT_ONLY ⇒ 端口被占但 /link 不应答（僵死桥）：同样不抢，睡 60s 复查；
                 连续多轮仍僵死 ⇒ 飞书提醒人工清理（绝不自动杀别的进程）
  3) DOWN ⇒ 我们自己拉桥（-c 常驻），桥死了立即进入下一轮重拉
判活只认「真发命令有应答」，端口连通≠桥可用（实测 WS 面活着不代表 HTTP 面）。

CLI
---
  （无参）      常驻监督，launchd KeepAlive=true 用
  --check       只探一次：UP rc=0 / DOWN rc=1 / PORT_ONLY rc=3
  --selftest    沙盒临时端口自测（同进程探测；不碰生产端口）
环境：TMWD_PORT=桥 WS 端口（默认 18765）
"""
import json
import os
import socket
import subprocess
import atexit
import signal
import sys
import time
import urllib.request

GA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PYTHON = "/opt/anaconda3/bin/python3"  # 唯一装了 simple_websocket_server/bottle 的解释器
LOG = os.path.join(GA_ROOT, "logs", "tmwd_bridge_watch.log")


def port_busy(port, host="127.0.0.1", timeout=1.0):
    """与 TMWebDriver 同口径：port+1(HTTP 面) 可连。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port + 1)) == 0
    finally:
        s.close()


def link_alive(port, timeout=5.0):
    """真发一次只读命令，确认 HTTP 面活着。返回 (alive, sessions, err)。"""
    url = "http://127.0.0.1:%d/link" % (port + 1)
    body = json.dumps({"cmd": "get_all_sessions"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            n = len(json.loads(r.read().decode()).get("r", []))
        return True, n, ""
    except Exception as e:
        return False, 0, "%s: %s" % (type(e).__name__, e)


def probe(port):
    """返回 (status, alive, sessions, err)，status ∈ UP/DOWN/PORT_ONLY。"""
    if not port_busy(port):
        return "DOWN", False, 0, ""
    alive, n, err = link_alive(port)
    return ("UP" if alive else "PORT_ONLY"), alive, n, err


def bridge_cmd(port):
    """起桥的 python -c：构造后必须常驻 sleep 循环，否则 daemon 线程随脚本退出被杀。"""
    # 孤儿自退：守护被 SIGKILL/系统事件强杀时信号收尾不可靠 ⇒ 子进程自己盯 ppid，
    # 守护一旦消失（ppid 变 1）5 秒内自行退出，绝不留孤儿占端口。
    return ("from TMWebDriver import TMWebDriver\n"
            "d = TMWebDriver(port=%d)\n"
            "import os, time\n"
            "while True:\n"
            "    time.sleep(5)\n"
            "    if os.getppid() == 1:\n"
            "        break\n") % port


def ensure_bridge(port, log_line=print):
    """一轮决策。返回 (child_Popen 或 None, status)。绝不抢别人占的端口。"""
    st, _, _, _ = probe(port)
    if st == "UP":
        log_line("[bridge-watch] 桥已在 127.0.0.1:%d（/link 应答正常），让位复查" % port)
        return None, st
    if st == "PORT_ONLY":
        log_line("[bridge-watch] 端口 %d(+1=%d) 被占但 /link 不应答（僵死桥）→ 不抢，睡后复查；连续多轮请人工清理" % (port, port + 1))
        return None, st
    py = pick_python()
    log_line("[bridge-watch] 桥不在，启动: %s -c 'TMWebDriver(port=%d)+常驻'" % (py, port))
    os.chdir(GA_ROOT)
    p = subprocess.Popen([py, "-u", "-c", bridge_cmd(port)],
                         stdout=sys.stdout, stderr=sys.stderr,
                         start_new_session=True)
    return p, st


_CHILD = {"p": None}


def _terminate_child(signum=None, _frame=None):
    """收手：把我们拉起的桥（独立进程组）一并带走，绝不留下孤儿占端口。"""
    p = _CHILD.get("p")
    if p is not None and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
    if signum is not None:
        sys.exit(0)


def serve(port, interval=60):
    """launchd 常驻监督主循环：别人的桥在→让位；我们拉桥→桥死了立即重拉。"""
    signal.signal(signal.SIGTERM, _terminate_child)
    signal.signal(signal.SIGINT, _terminate_child)
    atexit.register(_terminate_child)
    while True:
        p, st = ensure_bridge(port)
        if p is None:
            time.sleep(interval)  # UP/PORT_ONLY：让位，隔一阵复查
            continue
        _CHILD["p"] = p
        try:
            rc = p.wait()
        except KeyboardInterrupt:
            _terminate_child()
            return 0
        finally:
            if _CHILD.get("p") is p:
                _CHILD["p"] = None
        print("[bridge-watch] 桥进程退出 rc=%s，立即重拉" % rc)


def check(port):
    """--check：打印状态行，返回 rc。"""
    st, _, n, _ = probe(port)
    if st == "UP":
        print("bridge UP on 127.0.0.1:%d sessions=%d" % (port, n))
        return 0
    if st == "PORT_ONLY":
        print("bridge PORT_ONLY on 127.0.0.1:%d（端口开但 /link 不应答）" % port)
        return 3
    print("bridge DOWN on 127.0.0.1:%d" % port)
    return 1


def pick_python():
    """优先 anaconda（有 simple_websocket_server/bottle），否则当前解释器。"""
    if os.path.exists(DEFAULT_PYTHON):
        return DEFAULT_PYTHON
    return sys.executable


def main(argv):
    port = int(os.environ.get("TMWD_PORT") or 18765)
    if "--selftest" in argv:
        return selftest()
    if "--check" in argv:
        return check(port)
    return serve(port)


def selftest():
    """沙盒：临时端口验证 探活→起桥→让位决策→僵死桥负控→收手。同进程探测，不碰生产端口。"""
    port, dead = 28790, 28770
    ok = True
    if port_busy(port):
        print("SELFTEST FAIL: 沙盒端口 %d 已被占用，无法干净自测" % port)
        return 2
    if port_busy(dead):
        print("SELFTEST FAIL: 沙盒端口 %d 已被占用，无法干净自测" % dead)
        return 2

    st = probe(port)[0]
    print("T1 未起服务：probe=%s（期望 DOWN）: %s" % (st, st == "DOWN"))
    ok = ok and st == "DOWN"

    # 回归锁：桥命令必须常驻，去掉 while True 会秒退（2026-10-06 实测根因）
    has_loop = "while True" in bridge_cmd(port)
    print("T2 起桥命令含常驻循环（防秒退回归锁）: %s" % has_loop)
    ok = ok and has_loop

    py = pick_python()
    child = subprocess.Popen([py, "-u", "-c", bridge_cmd(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=GA_ROOT, start_new_session=True)
    up, n, err = False, 0, ""
    t0 = time.time()
    while time.time() - t0 < 40:
        up, n, err = link_alive(port, timeout=2.0)
        if up:
            break
        time.sleep(0.5)
    print("T3 起桥后 /link 真应答: %s (sessions=%d, %.1fs)" % (up, n, time.time() - t0))
    ok = ok and up

    act, st = ensure_bridge(port, log_line=lambda *a: None)
    print("T4 桥已在时决策：action=%s status=%s（期望 None/UP，让位不抢）: %s"
          % ("start" if act else "skip", st, act is None and st == "UP"))
    ok = ok and act is None and st == "UP"

    rc = check(port)
    print("T5 --check 对活桥 rc=%d（期望 0）: %s" % (rc, rc == 0))
    ok = ok and rc == 0

    # 负控：兄弟端口被占但 HTTP 面是死的（僵死桥）⇒ 必须判 PORT_ONLY、rc=3、绝不抢
    fake = socket.socket()
    fake.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    fake.bind(("127.0.0.1", dead + 1))
    fake.listen(4)
    act2, st2 = ensure_bridge(dead, log_line=lambda *a: None)
    rc2 = check(dead)
    print("T6 僵死桥：status=%s rc=%d action=%s（期望 PORT_ONLY/3/skip）: %s"
          % (st2, rc2, "start" if act2 else "skip",
             st2 == "PORT_ONLY" and rc2 == 3 and act2 is None))
    ok = ok and st2 == "PORT_ONLY" and rc2 == 3 and act2 is None
    fake.close()

    down2 = probe(dead)[0]
    print("T7 假桥关闭后 probe=%s（期望 DOWN）: %s" % (down2, down2 == "DOWN"))
    ok = ok and down2 == "DOWN"

    child.terminate()
    try:
        child.wait(timeout=5)
    except Exception:
        child.kill()
    time.sleep(0.5)
    down = probe(port)[0]
    print("T8 收手后 probe=%s（期望 DOWN）: %s" % (down, down == "DOWN"))
    ok = ok and down == "DOWN"

    print("SELFTEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
