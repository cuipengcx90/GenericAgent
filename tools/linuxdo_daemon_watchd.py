#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L站浏览守护「看门狗」——零站点流量的进程级自愈（2026-10-05）

背景：linuxdo_browse_daemon.py 是长驻守护（承担浏览动作 + 等级前置 + 缺项路由），
一旦进程死掉，只能等下一个定时入口（09:30 / 12:40）才被拉起 ⇒ 最长空窗可达 ~21h，
期间「浏览/等级检查/排队动作」全部停摆，且**用户看不见**（静默失效）。

本器只做四件事（全部本地、零站点请求、零 LLM token）：
  1. 探活：守护进程不在 ⇒ 先探工作台桥；桥不可达 ⇒ 先**自动拉起独立桥**
     （tools/tmwd_bridge_watch.py，脱离 Galley 座舱，R556），仍不可达才判 BRIDGE_DOWN
     （**不重启**、长冷却告警）；桥可达才用 bg_task 拉起（幂等）；
  2. 停滞：在活动窗内长时间没有浏览产出（browse_log 无新增 DONE）⇒ 判为卡死并重启；近 1h 已重启超频则抑制（RESTART_SKIP）；
  3. 留痕 + 告警：事件写 temp/linuxdo_watch/watchd.jsonl，重启/恢复各发一次私信；
  4. 合并告警（R551）：同类事件在 6h 窗口内只发一条汇总（含累计次数），防刷屏但仍看得见；
  5. 休眠自证（R558）：守护睡前写 heartbeat.json 声明「下次预计醒来时刻」⇒ 静默窗长睡
     不再被当成停滞（旧：静默窗 6h 长睡 ⇒ 误判 STALLED 26000s ⇒ 误重启 + bg_task rc=4
     被读成「拉起失败」而告警）；重启前若守护仍在跑，先 bg_task stop 再 start（真自愈）。

防检测前提（用户要求）：
  · 本器**不发任何站点请求**（不 import 浏览器驱动、不跑 JS）——只读本地进程表与日志；
  · 重启不改变任何随机节奏参数（日限/静默窗/休息日/段间隔仍由 linuxdo_antidetect 内核决定）；
  · 绝不写入守护的 state.json（只读它来判活），避免污染日账/等级记录。

用法：
  python3 tools/linuxdo_daemon_watchd.py ensure          # 兜底入口：起看门狗（已在跑则原样返回）
  python3 tools/linuxdo_daemon_watchd.py run [--max-hours 26]
  python3 tools/linuxdo_daemon_watchd.py status
  python3 tools/linuxdo_daemon_watchd.py --selftest
"""

import argparse
import glob
import http.client
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DAEMON_PAT = "linuxdo_browse_daemon.py watch"
BG_NAME = "linuxdo_browse"                 # bg_task 里的名字（与历史入口一致）
DEFAULT_BG = os.path.join(HERE, "bg_task.py")
STATE_DIR = os.path.join(ROOT, "temp", "linuxdo_watch")
EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
LOCK = os.path.join(STATE_DIR, "watchd.lock")
BROWSE_LOG = os.path.join(ROOT, "temp", "linuxdo_browse_daemon", "browse_log.jsonl")

CHECK_INTERVAL = 60          # 巡检间隔（秒）
STALL_S = 3 * 3600           # 活动窗内超过 3h 无浏览产出 ⇒ 判停滞（正常段间隔最长 ~40min + 长休）
RESTART_MIN_GAP = 300        # 两次重启最小间隔（秒），防抖/防重启风暴
NOTIFY_COOLDOWN_H = 6        # 同类告警冷却（小时）
RESTART_RATE_PER_HOUR = 3    # 每小时重启上限（超限改「跳过」；防重启风暴，见 R545 缺陷①）
BUSY_LOG_S = 20 * 60         # 环境忙信号回看窗口（秒）：daemon 日志近 N 分钟含忙标记 ⇒ 判 BLOCKED_ENV
FAIL_STREAK_NOTIFY = 3       # 连续重启失败达 N 次 ⇒ 告警（并继续重试）
BRIDGE_TARGETS = [("127.0.0.1", int(os.environ.get("LJQ_BRIDGE_PORT", "18766"))),
                  ("127.0.0.1", int(os.environ.get("TMWD_PORT", "18765")))]  # 工作台桥：18766=HTTP(守护默认) / 18765=WS(兼容)；两端口均可 env 覆盖(LJQ_BRIDGE_PORT/TMWD_PORT)以便沙盒端到端
BRIDGE_TIMEOUT = 2.0                     # 桥探活超时（秒）
BRIDGE_RUNNER_PAT = "tmwd_bridge_watch.py"   # 独立桥监督进程（不依赖 Galley 座舱）
BRIDGE_RUNNER_NAME = "tmwd_bridge"           # bg_task 任务名（同名幂等）
BRIDGE_ENSURE_WAIT_S = 15                    # 起桥后等桥可达的上限（秒）
BRIDGE_ENSURE_COOLDOWN_S = 600               # 起桥尝试冷却（秒），防每轮空拉
RESTING_MAX_S = 12 * 3600                    # 心跳声明的「下次醒来」最远可信窗口（超出视为坏值，回落旧判据）
HEARTBEAT_REL = os.path.join("temp", "linuxdo_browse_daemon", "heartbeat.json")
HEARTBEAT_FILE = os.path.join(ROOT, HEARTBEAT_REL)
ALERT_MERGE_WINDOW_H = 6                 # 同类重启告警合并窗口（小时）：窗口内只发一条汇总
RECOVERY_NOTIFY_MIN_S = 300              # 停机 ≥ 该秒数才发「已恢复」（一闪而过不打扰）
DEFAULT_MAX_HOURS = 26.0     # 长驻上限；由每日入口重新武装（对齐 anyrouter watchd 模式）


# ---------------------------------------------------------------- 基础工具
def _now():
    return time.time()


def _ts(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t if t is not None else _now()))


def _parse_ev_ts(s):
    """台账时间戳 → epoch（坏值返回 None，绝不抛）。"""
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def sandbox_state(path):
    """把运行态整体重定向到沙盒目录（STATE_DIR/EVENTS/LOCK 一并切换），返回 (old, new)。

    ★R551 教训：EVENTS/LOCK 是导入时冻结的独立常量，只改 STATE_DIR 会让日志/锁仍写生产文件
    （外来演练脚本曾据此把 8 行假事件写进生产 watchd.jsonl）。自测与演练一律走本助手。
    """
    global STATE_DIR, EVENTS, LOCK
    old = (STATE_DIR, EVENTS, LOCK)
    STATE_DIR = path
    EVENTS = os.path.join(path, "watchd.jsonl")
    LOCK = os.path.join(path, "watchd.lock")
    return old, (STATE_DIR, EVENTS, LOCK)


def _log_event(kind, **kw):
    rec = {"ts": _ts(), "kind": kind}
    rec.update(kw)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def _reflect_scripts_dir(dirs=None):
    """定位 GA 的 reflect_scripts 目录（reflect_common 的真身在此，非 tools/）。

    ★2026-10-06 修：原实现只把 HERE(=tools/) 插进 sys.path ⇒ `import reflect_common`
    恒 ModuleNotFoundError ⇒ 重启/恢复告警**静默丢失**（events 里全是 NOTIFY_FAIL，
    用户侧什么都收不到，正是 R545 缺陷②「告警面呈健康而实际不可用」的同类）。
    """
    cands = dirs if dirs is not None else (os.path.join(ROOT, "reflect_scripts"),
                                           os.path.join(HERE, "reflect_scripts"))
    for cand in cands:
        if os.path.isfile(os.path.join(cand, "reflect_common.py")):
            return cand
    return None


def notify(text):
    """飞书私信告警（复用 GA 既有通道；失败不致命，但必须留痕）。"""
    rs = _reflect_scripts_dir()
    if not rs:
        _log_event("NOTIFY_FAIL", note="reflect_common_not_found")
        return False
    try:
        if rs not in sys.path:
            sys.path.insert(0, rs)
        from reflect_common import notify_feishu  # type: ignore
        notify_feishu(text)
        return True
    except Exception as e:                                  # 绝不死看门狗
        _log_event("NOTIFY_FAIL", note=repr(e)[:160])
        return False


# ---------------------------------------------------------------- 探针（全部只读）
def _ps_lines(pat=None):
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,command="],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    rows = []
    for ln in out.splitlines():
        ln = ln.rstrip()
        if not ln.strip():
            continue
        if pat and pat not in ln:
            continue
        rows.append(ln)
    return rows


def daemon_pids(pat=None):
    pat = pat or DAEMON_PAT
    pids = []
    for ln in _ps_lines(pat):
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", ln)
        if not m:
            continue
        cmd = m.group(4)
        if pat not in cmd:
            continue
        if pat == DAEMON_PAT and "watchd" in cmd:      # 看门狗自身不算守护
            continue
        pids.append(int(m.group(1)))
    return pids


def last_browse_ts(path=None):
    """browse_log 最后一条记录的时间戳（秒）。无文件/无记录 ⇒ None。"""
    path = path or BROWSE_LOG
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-65536, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", "ignore").splitlines()
    except Exception:
        return None
    for ln in reversed(tail):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        t = rec.get("ts") or rec.get("time")
        if not t:
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S",):
            try:
                return time.mktime(time.strptime(t, fmt))
            except Exception:
                pass
    return None


# 守护的其它「还在干活」痕迹（每一步 save_state / 等级检查都会刷新其 mtime）
PROGRESS_FILES = (
    os.path.join("temp", "linuxdo_browse_daemon", "state.json"),
    os.path.join("temp", "linuxdo_browse_daemon", "level_log.jsonl"),
    HEARTBEAT_REL,          # R558：守护自声明心跳（mtime 也计入「还在干活」）
)


def _etime_seconds(s):
    """解析 ps 的 etime：[[dd-]hh:]mm:ss / mm:ss ⇒ 秒；非法 ⇒ None。"""
    s = (s or "").strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except Exception:
            return None
    try:
        nums = [int(x) for x in s.split(":")]
    except Exception:
        return None
    if len(nums) == 3:
        h, m, sec = nums
    elif len(nums) == 2:
        h, m, sec = 0, nums[0], nums[1]
    else:
        return None
    return ((days * 24 + h) * 60 + m) * 60 + sec


def daemon_started_ts(pids=None, now=None):
    """守护进程启动时刻（epoch 秒）；多个 pid 取最早。读不到 ⇒ None。

    ★冷启动保护用：browse_log 末条可能是**上一次会话**留下的（距今数小时），
    只按它判停滞会把刚拉起 1 分钟的新进程当卡死再杀一次（R545 缺陷①现场复现）。
    """
    now = now if now is not None else _now()
    pids = list(pids) if pids is not None else daemon_pids()
    if not pids:
        return None
    try:
        r = subprocess.run(["ps", "-o", "pid=,etime=", "-p", ",".join(str(p) for p in pids)],
                           capture_output=True, text=True, timeout=20)
        out = r.stdout
    except Exception:
        return None
    best = None
    for ln in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\S+)\s*$", ln)
        if not m:
            continue
        secs = _etime_seconds(m.group(2))
        if secs is None:
            continue
        st = now - secs
        if best is None or st < best:
            best = st
    return best


def last_progress_ts(browse_ts=None, root=None):
    """多源「最近一次实质推进」时刻（epoch 秒）：browse_log 末条 ts × 进度文件 mtime，取最大。

    ★为什么不能只看 browse_log：①冷启动时末条来自上一会话（可距今数小时）
      ②单帖驻留期间只有**完成**才落一行（长驻留 JS 数分钟）⇒ 批内静默属正常
    ⇒ 守护每推进一步都会刷新 state.json / level_log.jsonl，其 mtime 是更细的心跳。
    """
    root = root or ROOT
    vals = []
    bts = last_browse_ts() if browse_ts is None else browse_ts
    if bts:
        vals.append(float(bts))
    for rel in PROGRESS_FILES:
        p = os.path.join(root, rel)
        try:
            if os.path.exists(p):
                vals.append(os.path.getmtime(p))
        except Exception:
            pass
    return max(vals) if vals else None


def heartbeat_info(path=None, root=None):
    """读守护自声明心跳（R558）。返回 {phase, next_wake_ts, epoch} 或 None（缺/坏一律 None）。

    ★为什么需要它：只看 mtime 分不清「卡死」与「按设计长睡」（静默窗 01:00–07:30 睡 6h、
    随机休息日睡整点复查）。守护睡前会把 next_wake_ts=当时刻+睡眠秒数 写进心跳，
    本器据此判「它在睡，且说得出何时醒」⇒ OK 而非 RESTART。
    """
    p = path or HEARTBEAT_FILE
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        return {"phase": d.get("phase"), "next_wake_ts": d.get("next_wake_ts"),
                "epoch": d.get("epoch"), "pid": d.get("pid")}
    except Exception:
        return None


def quiet_now(now=None):
    """复用反检测内核的静默窗判定（不可用则 fail-open：返回 False）。"""
    try:
        import linuxdo_antidetect as K  # type: ignore
        return bool(K.quiet_now(now))
    except Exception:
        return False


def spawn_daemon(bg=DEFAULT_BG, name=BG_NAME, cwd=ROOT, dry=False):
    """用 bg_task 拉起守护（幂等：已存在同名任务时 bg_task 会自行处置）。"""
    cmd = [sys.executable, bg, "start", "--name", name, "--cwd", cwd,
           "--", "python3", "-u", "tools/linuxdo_browse_daemon.py", "watch"]
    if dry:
        return {"dry": True, "cmd": " ".join(cmd)}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return {"rc": r.returncode, "out": (r.stdout or "").strip()[-400:],
                "err": (r.stderr or "").strip()[-200:]}
    except Exception as e:
        return {"rc": -1, "err": repr(e)[:200]}


def stop_daemon(bg=DEFAULT_BG, name=BG_NAME, dry=False):
    """停同名 bg_task 任务（R558）。守护**活着却判停滞**时，直接 start 会被 bg_task 以
    「同名任务已在 RUNNING」拒绝（rc=4）⇒ 旧码把这当「拉起失败」并连发告警；先停再起才是真自愈。
    停的是 bg_task 记录的任务（其中含该 daemon 及其子进程）；dry 只回命令不执行。"""
    cmd = [sys.executable, bg, "stop", "--name", name]
    if dry:
        return {"dry": True, "cmd": " ".join(cmd)}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"rc": r.returncode, "out": (r.stdout or "").strip()[-200:],
                "err": (r.stderr or "").strip()[-200:]}
    except Exception as e:
        return {"rc": -1, "err": repr(e)[:200]}


def bridge_ok(targets=None, timeout=None):
    """工作台桥探活（只读本地回环，零站点流量）。

    为什么要它：L站浏览守护（linuxdo_browse_daemon）依赖 Galley 工作台桥（TMWebDriver）。
    桥不在时守护启动即 ECONNREFUSED 退出 ⇒ 「重启守护」纯属空转，还会每 5 分钟刷一条告警
    （2026-10-06 实测 27 次）。故先探桥：不可达 ⇒ 判 BRIDGE_DOWN（按环境不可用处理，
    不重启、按冷却告警、桥回来自动恢复）。

    口径：只连 127.0.0.1 的回环端口（BRIDGE_TARGETS）；有 HTTP 响应即视为可达（不判正文）。
    任何异常都返回 False（fail-closed：**宁可不重启**，也不空转刷告警）。
    """
    timeout = BRIDGE_TIMEOUT if timeout is None else timeout
    for host, port in (targets or BRIDGE_TARGETS):
        conn = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request("GET", "/")
            conn.getresponse().read(1)      # 读 1 字节即可证有响应；正文内容不判
            return True
        except Exception:
            continue
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    return False
def browse_busy_hint(now=None):
    """只读判「环境不可用」：daemon stdout 日志近 BUSY_LOG_S 秒内是否出现忙标记。

    忙标记（R545 缺陷②所述 10.5h 零产出的两条真实原因）：
      · RC_NO_SESSION_RETRY —— 浏览器无可用 linux.do 会话（外部前置）
      · 前台跳过 / NO_FRONT —— 站点前台标签被占用/失焦
      · 静默窗 —— 内核既定休息，不算坏（由 quiet_now 单独处理）
    返回 (True, reason) / (False, "")
    """
    now = now if now is not None else _now()
    cands = []
    for pat in (os.path.join(ROOT, "temp", "bg_task", "*%s*.log" % BG_NAME),
                os.path.join(ROOT, "temp", "**", "*linuxdo_browse*.log"),
                os.path.join(ROOT, "logs", "*linuxdo_browse*.log")):
        cands += glob.glob(pat, recursive=True)
    marks = ("RC_NO_SESSION_RETRY", "NO_FRONT", "前台跳过", "no_session")
    newest = None
    for p in cands:
        try:
            m = os.path.getmtime(p)
        except Exception:
            continue
        if now - m > BUSY_LOG_S * 3:            # 日志本身老旧 ⇒ 不作证据
            continue
        if newest is None or m > newest[1]:
            newest = (p, m)
    if not newest:
        return (False, "")
    path = newest[0]
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-65536, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", "ignore").splitlines()
    except Exception:
        return (False, "")
    # 只看近 BUSY_LOG_S 秒内的行（行格式 [YYYY-mm-dd HH:MM:SS] ...）
    for ln in reversed(tail):
        if not any(k in ln for k in marks):
            continue
        ts = None
        if ln.startswith("[") and "]" in ln:
            try:
                ts = time.mktime(time.strptime(ln[1:ln.index("]")], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                ts = None
        if ts is None or (now - ts) <= BUSY_LOG_S:
            for k in marks:
                if k in ln:
                    return (True, k)
    return (False, "")


# ---------------------------------------------- 独立桥自愈（脱离 Galley 座舱）
def bridge_runner_pids():
    """独立桥监督进程（tools/tmwd_bridge_watch.py）的 pid 列表（只读 ps，绝不杀进程）。"""
    out = []
    for ln in _ps_lines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", ln)
        if not m:
            continue
        cmd = m.group(4)
        if BRIDGE_RUNNER_PAT in cmd and "grep" not in cmd:
            out.append(int(m.group(1)))
    return out


def _spawn_cmd(name, argv, dry=False, bg=DEFAULT_BG, cwd=ROOT, timeout=90):
    """用 bg_task 起一个后台任务（同名幂等）。返回 {rc,out,err}。"""
    if dry:
        return {"rc": 0, "dry": True, "argv": list(argv)}
    cmd = [sys.executable, bg, "start", "--name", name, "--cwd", cwd, "--"] + list(argv)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "out": (r.stdout or "").strip()[-400:],
                "err": (r.stderr or "").strip()[-200:]}
    except Exception as e:
        return {"rc": -1, "err": repr(e)[:200]}


def ensure_bridge(dry=False, spawn=None, wait_s=None, now=None, probe=None, sleep_fn=None,
                  runner_pids=None):
    """桥不可达 ⇒ 自动拉起**独立桥**（不再要求用户开着 Galley 座舱）。

    2026-10-06 用户裁决：浏览器可以开着（就用日常 Chrome），但**不允许**把「开着座舱」
    当前置。桥（127.0.0.1:18765 WS / 18766 HTTP）历史上由 Galley 的
    runner.workbench_bridge 顺手拉起 ⇒ 座舱一关桥就没了，看门狗只能干等。
    本函数让看门狗自己兜底（只读探活 + 起一个零 token 的后台监督进程）。

    返回 (status, info)，status ∈
      UP / STARTED / ALREADY_RUNNING / SKIPPED_COOLDOWN / START_FAILED / DRY
    fail-open：任何异常都不阻断巡检（最坏情况退回旧的「只告警」行为）。
    """
    spawn = spawn or _spawn_cmd
    probe = probe or bridge_ok
    pids_fn = runner_pids or bridge_runner_pids
    sleep_fn = sleep_fn or time.sleep
    wait_s = BRIDGE_ENSURE_WAIT_S if wait_s is None else wait_s
    now = now if now is not None else _now()
    try:
        if probe():
            return ("UP", {})
        rt = read_runtime()
        if pids_fn():
            deadline = _now() + wait_s
            while _now() < deadline and not probe():
                sleep_fn(1.0)
            if probe():
                return ("STARTED", {"waited": True, "runner_running": True})
            return ("ALREADY_RUNNING", {})
        last = float(rt.get("bridge_ensure_ts") or 0)
        if last and (now - last) < BRIDGE_ENSURE_COOLDOWN_S:
            return ("SKIPPED_COOLDOWN", {"age_s": now - last})
        if dry:
            return ("DRY", {})
        res = spawn(BRIDGE_RUNNER_NAME,
                    [sys.executable, "-u", os.path.join("tools", BRIDGE_RUNNER_PAT)])
        deadline = _now() + wait_s
        while _now() < deadline and not probe():
            sleep_fn(1.0)
        if probe():
            write_runtime({"bridge_ensure_ts": now})
            return ("STARTED", {"rc": res.get("rc"), "out": res.get("out")})
        write_runtime({"bridge_ensure_ts": now})
        return ("START_FAILED", {"rc": res.get("rc"), "err": res.get("err")})
    except Exception as e:
        return ("START_FAILED", {"err": repr(e)[:200]})


# ---------------------------------------------------------------- 单实例锁
def _lock_pid():
    try:
        with open(LOCK, encoding="utf-8") as f:
            d = json.load(f)
        return int(d.get("pid") or 0)
    except Exception:
        return 0


def acquire_lock(pid=None):
    """单实例锁：真活着 ⇒ 返回 False（调用方应静默退出）。僵锁可回收。"""
    pid = pid or os.getpid()
    os.makedirs(STATE_DIR, exist_ok=True)
    cur = _lock_pid()
    if cur and cur != pid:
        for ln in _ps_lines():
            m = re.match(r"\s*(\d+)\s+.*watchd", ln)
            if m and int(m.group(1)) == cur and "linuxdo_daemon_watchd" in ln:
                return False                        # 真活着 ⇒ 拒绝
        _log_event("LOCK_STALE", old_pid=cur)       # 僵锁回收
    with open(LOCK, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "ts": _ts()}, f)
    return True


def release_lock(pid=None):
    pid = pid or os.getpid()
    if _lock_pid() == pid:
        try:
            os.remove(LOCK)
        except Exception:
            pass


# ---------------------------------------------------------------- 决策核心（可注入，便于自测）
def decide(snapshot):
    """纯函数：由只读快照给出本轮决策。

    snapshot 键：pids(list) / last_done(ts|None) / started(ts|None) / now / quiet(bool) /
                 last_restart_ts / fail_streak / busy_hint(bool) / busy_reason(str) /
                 restart_times(list[ts]) / bridge_ok(bool|None；None=未探，按旧行为处理)
    返回 {action:'OK'|'RESTART'|'RESTART_SKIP'|'BLOCKED_ENV'|'BRIDGE_DOWN', reason:str}

    ★心跳 = max(最近一次实质产出, 本进程启动时刻)：冷启动时 last_done 可能来自**上一次
    会话**（数小时前）⇒ 只按它判会把刚拉起 1 分钟的新进程当卡死再杀（丢正在跑的那一帖）。
    """
    now = snapshot["now"]
    pids = snapshot.get("pids") or []
    last = snapshot.get("last_done")
    started = float(snapshot.get("started") or 0)
    quiet = bool(snapshot.get("quiet"))
    lr = float(snapshot.get("last_restart_ts") or 0)
    cooldown_ok = (now - lr) >= RESTART_MIN_GAP

    if not pids:
        if snapshot.get("bridge_ok") is False:
            # 桥不在 ⇒ 拉起也立刻 ECONNREFUSED 退出：不空转、不刷告警（R551）
            return {"action": "BRIDGE_DOWN", "reason": "BRIDGE_UNREACHABLE"}
        if not cooldown_ok:
            return {"action": "RESTART_SKIP", "reason": "RESTART_COOLDOWN"}
        return {"action": "RESTART", "reason": "DAEMON_DEAD"}
    hb = max([v for v in (last, started) if v]) if (last or started) else None
    if hb is None:
        return {"action": "OK", "reason": "NO_LOG_YET"}
    age = now - hb
    # R558：守护自声明「我在睡，且将在 next_wake_ts 醒来」⇒ 停滞判据不适用（静默窗长睡正当）
    try:
        nw = float(snapshot.get("next_wake_ts"))
    except Exception:
        nw = None
    if nw and nw > now and (nw - now) <= RESTING_MAX_S:
        return {"action": "OK", "reason": "RESTING_UNTIL_%s_%.0fm" % (snapshot.get("phase") or "", (nw - now) / 60.0)}
    if age > STALL_S and not quiet:
        if snapshot.get("bridge_ok") is False:
            # 桥不在：重启必空转（守护起来就拒连）⇒ 按环境不可用处理，不动进程（R551）
            return {"action": "BRIDGE_DOWN", "reason": "BRIDGE_UNREACHABLE_STALL_%.0fs" % age}
        if snapshot.get("busy_hint"):
            # 停滞原因是**外部环境不可用**（无会话/前台被占）⇒ 重启无用，标 BLOCKED_ENV
            return {"action": "BLOCKED_ENV",
                    "reason": "ENV_BUSY_%s_%.0fs" % (snapshot.get("busy_reason") or "UNKNOWN", age)}
        recent = [t for t in (snapshot.get("restart_times") or []) if t and (now - t) <= 3600]
        if len(recent) >= RESTART_RATE_PER_HOUR:
            return {"action": "RESTART_SKIP", "reason": "RESTART_RATELIMIT_%d/h" % len(recent)}
        if not cooldown_ok:
            return {"action": "RESTART_SKIP", "reason": "RESTART_COOLDOWN"}
        return {"action": "RESTART", "reason": "STALLED_%.0fs" % age}
    if age > STALL_S and quiet:
        return {"action": "OK", "reason": "STALL_IN_QUIET_WINDOW"}
    return {"action": "OK", "reason": "ALIVE_FRESH_%.0fs" % age}


def _alert_restart(d, ok, handled, notify_fn, rt):
    """重启类告警合并（R551）：首次立即发，此后同类在窗口内只发一条汇总（含累计次数）。

    handled 结构（存 runtime.json 的 alerts 段，按 kind 分桶）：
      {"RESTART": {"first_ts": 首见, "count": 窗口内累计, "summarized_ts": 上次汇总时刻}, ...}
    口径：kind=RESTART（重启成功）/ RESTART_FAIL（重启失败达阈值）。首见立即告警；
    此后每 ALERT_MERGE_WINDOW_H 小时最多再发一条「累计 N 次」；恢复时由调用方清空 alerts。
    返回 (是否已发, 更新后的 handled)。
    """
    now = _now()
    kind = "RESTART" if ok else "RESTART_FAIL"
    rec = dict(handled.get(kind) or {})
    if not rec:
        rec = {"first_ts": now, "count": 0}
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["last_ts"] = now
    win = ALERT_MERGE_WINDOW_H * 3600
    last_sum = float(rec.get("summarized_ts") or 0)
    first = float(rec.get("first_ts") or now)
    send = (last_sum == 0 and now - first < win) or (last_sum > 0 and (now - last_sum) >= win)
    if send:
        if rec["count"] <= 1:
            if ok:
                notify_fn("⚠️ L站浏览守护被看门狗重启：%s（%s）已拉起，节奏参数未变。"
                          % (d.get("reason"), _ts()))
            else:
                notify_fn("⚠️ L站浏览守护重启失败 1 次（%s）：已尝试拉起但未成功，将退避重试。· %s"
                          % (d.get("reason"), _ts()))
        else:
            notify_fn("⚠️ L站浏览守护%s累计 %d 次（最近：%s）——已按 %dh 窗口合并告警，"
                      "不再逐条推送。· %s"
                      % ("" if ok else "重启失败", rec["count"], d.get("reason"),
                         ALERT_MERGE_WINDOW_H, _ts()))
        rec["summarized_ts"] = now
    handled[kind] = rec
    return send, handled
def read_runtime():
    """只读快照（不写守护 state）。"""
    try:
        with open(os.path.join(STATE_DIR, "runtime.json"), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return d


def write_runtime(patch):
    cur = read_runtime()
    cur.update(patch)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "runtime.json"), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return cur


# ---------------------------------------------------------------- 主循环
def one_round(dry=False, spawn=None, notify_fn=None, snapshot=None, ensure=None, stop=None):
    """一轮巡检。snapshot 非 None 时跳过探针（仅供自测注入，生产恒为探针）。"""
    spawn = spawn or spawn_daemon
    notify_fn = notify_fn or notify
    ensure = ensure or ensure_bridge
    stop = stop or stop_daemon
    rt = read_runtime()
    if snapshot is not None:
        snap = dict(snapshot)
        snap.setdefault("last_restart_ts", rt.get("last_restart_ts"))
        snap.setdefault("fail_streak", rt.get("fail_streak") or 0)
        snap.setdefault("now", _now())
    else:
        bh, bwhy = browse_busy_hint()
        _bridge = bridge_ok()                 # R551：桥探活（桥不在则一切重启都是空转）
        _pids = daemon_pids()
        snap = {"pids": _pids, "last_done": last_progress_ts(),
                "started": daemon_started_ts(_pids), "now": _now(),
                "quiet": quiet_now(), "last_restart_ts": rt.get("last_restart_ts"),
                "fail_streak": rt.get("fail_streak") or 0,
                "busy_hint": bh, "busy_reason": bwhy,
                "bridge_ok": _bridge,
                "restart_times": rt.get("restart_times") or []}
        _hbi = heartbeat_info() or {}
        snap["next_wake_ts"] = _hbi.get("next_wake_ts")      # R558：休眠自证
        snap["phase"] = _hbi.get("phase")
    d = decide(snap)
    _log_event("ROUND", action=d["action"], reason=d["reason"],
               n_pids=len(snap["pids"]), quiet=snap["quiet"],
               last=snap.get("last_done"))
    if d["action"] == "BRIDGE_DOWN":
        # R556：不再只是告警——先自己把**独立桥**拉起来（不依赖 Galley 座舱）
        _st, _info = ensure(dry=dry)
        _log_event("BRIDGE_ENSURE", status=_st)
        if not dry and _st in ("UP", "STARTED", "ALREADY_RUNNING"):
            d = decide(dict(snap, bridge_ok=True))      # 桥已可用 ⇒ 重判（通常 RESTART；判据不变）
            _log_event("ROUND", action=d["action"], reason="AFTER_BRIDGE_ENSURE_" + str(d["reason"]))
        if d["action"] == "BRIDGE_DOWN":
            # 起桥失败/冷却中：守护根本起不来（不是它的错）⇒ 不重启，按 6h 冷却告警一次（R551）
            last_n = float(rt.get("bridge_notified_ts") or 0)
            if _now() - last_n >= NOTIFY_COOLDOWN_H * 3600:
                notify_fn("⛔ L站浏览守护已停：工作台桥（127.0.0.1:18766/18765）不可达，"
                          "看门狗已尝试自动拉独立桥但未成功。· %s" % _ts())
                write_runtime({"bridge_notified_ts": _now(), "notified_down": True,
                               "down_since": _now()})
            _log_event("BRIDGE_DOWN", reason=d["reason"])
            return dict(d, pids=snap["pids"])
    if d["action"] == "BLOCKED_ENV":
        last_n = float(rt.get("blocked_notified_ts") or 0)
        if _now() - last_n >= NOTIFY_COOLDOWN_H * 3600:
            notify_fn("⛔ L站浏览守护停滞=外部环境不可用（%s），看门狗**不重启**（重启无用）。"
                      "请在浏览器保留一个已登录且前台的 linux.do 标签；恢复后我会自动继续。· %s"
                      % (d["reason"], _ts()))
            write_runtime({"blocked_notified_ts": _now()})
        _log_event("BLOCKED_ENV", reason=d["reason"])
        return dict(d, pids=snap["pids"])
    if d["action"] != "RESTART":
        if d["action"] == "OK" and (rt.get("notified_down") or rt.get("blocked_notified_ts")
                                    or rt.get("bridge_notified_ts")):
            # R551：停机时间太短（一闪而过）就不打扰；恢复时清空合并计数，下一轮重新计数
            # 时长闸只针对「重启造成的停机」（一闪而过的重启不打扰）；
            # 环境不可用/桥不可达属真停机，其恢复通知不受闸门限制
            _ds = float(rt.get("down_since") or 0)
            if (not _ds) or (_now() - _ds) >= RECOVERY_NOTIFY_MIN_S:
                notify_fn("✅ L站浏览守护已恢复（看门狗）· %s" % _ts())
            write_runtime({"notified_down": False, "blocked_notified_ts": 0,
                           "bridge_notified_ts": 0, "down_since": 0, "alerts": {},
                           "fail_streak": 0})
        return dict(d, pids=snap["pids"])
    if snap.get("pids"):
        # R558：守护在跑却判停滞 ⇒ 先停同名任务，否则 start 必被 bg_task 拒（rc=4=假「拉起失败」）
        _stop_res = stop(dry=dry)
        _log_event("RESTART_STOP", res=_stop_res)
    res = spawn(dry=dry)
    ok = (res.get("rc") == 0) or res.get("dry")
    _rt_hist = [t for t in (rt.get("restart_times") or []) if t and (_now() - t) <= 3600]
    _rt_hist.append(_now())
    fs = 0 if ok else (rt.get("fail_streak") or 0) + 1
    write_runtime({"restart_times": _rt_hist, "last_restart_ts": _now(), "fail_streak": fs,
                   "notified_down": True if ok else rt.get("notified_down"),
                   "down_since": _now() if ok else rt.get("down_since")})
    _log_event("RESTART", reason=d["reason"], ok=bool(ok), res=res)
    if not dry:
        # R551 合并告警：首见立即发，其后同类在窗口内只发一条汇总（防刷屏）
        if ok:
            _sent, _handled = _alert_restart(d, True, rt.get("alerts") or {}, notify_fn, rt)
            write_runtime({"alerts": _handled})
        elif fs >= FAIL_STREAK_NOTIFY:
            _sent, _handled = _alert_restart(d, False, rt.get("alerts") or {}, notify_fn, rt)
            write_runtime({"alerts": _handled})
    return dict(d, pids=snap["pids"], restart=res)


def cmd_run(args):
    if not acquire_lock():
        print(json.dumps({"skip": True, "why": "LOCK_BUSY"}, ensure_ascii=False))
        return 0
    deadline = _now() + args.max_hours * 3600
    _log_event("START", pid=os.getpid(), max_hours=args.max_hours)
    try:
        while _now() < deadline:
            try:
                r = one_round(dry=args.dry_run)
                if args.verbose:
                    print("[%s] %s %s" % (_ts(), r.get("action"), r.get("reason")), flush=True)
            except Exception as e:                      # 单轮异常绝不死看门狗
                _log_event("ROUND_ERROR", note=repr(e)[:200])
            time.sleep(args.interval)
    finally:
        release_lock()
        _log_event("EXIT", max_hours=args.max_hours)
    return 0


def cmd_ensure(args):
    """兜底入口：已在跑 ⇒ 原样返回；否则后台起一个（幂等）。"""
    me = daemon_pids("linuxdo_daemon_watchd.py run")
    if me:
        print(json.dumps({"watchd": "already_running", "pids": me}, ensure_ascii=False))
        return 0
    cmd = [sys.executable, os.path.abspath(__file__), "run",
           "--max-hours", str(args.max_hours), "--interval", str(args.interval)]
    if args.dry_run:
        print(json.dumps({"dry": True, "cmd": " ".join(cmd)}, ensure_ascii=False))
        return 0
    try:
        p = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as e:
        print(json.dumps({"watchd": "spawn_failed", "err": repr(e)[:200]}, ensure_ascii=False))
        return 1
    _log_event("ENSURE", pid=p.pid)
    print(json.dumps({"watchd": "started", "pid": p.pid, "max_hours": args.max_hours},
                     ensure_ascii=False))
    return 0


def restart_audit(now=None):
    """重启审计视图（R546 遗留建议 #7，只读派生）。

    动机：R545 缺陷①（9h20m 内重启 41 次）当时只能靠翻 watchd.jsonl 手工数；本视图把
    「今日重启几次 / 累计 / 最近一次与原因 / 近一小时次数（对照 RESTART_RATE_PER_HOUR 上限）」
    变成 status 里一眼可读的数。只读本地台账，零站点请求，不改任何判据与节奏。
    """
    nowt = _now() if now is None else float(now)
    day = time.strftime("%Y-%m-%d", time.localtime(nowt))
    out = {"restarts_total": 0, "restarts_today": 0, "restarts_ok": 0,
           "restarts_failed": 0, "restarts_last_hour": 0, "last_restart": None,
           "last_start": None, "rate_limit_per_hour": RESTART_RATE_PER_HOUR,
           "note": None}
    try:
        if not os.path.exists(EVENTS):
            out["note"] = "no_events_file"
            return out
        with open(EVENTS, encoding="utf-8") as f:
            lines = f.readlines()[-20000:]
    except Exception as e:
        out["note"] = "events_unreadable:%s" % e
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        kind = rec.get("kind")
        if kind == "RESTART":
            out["restarts_total"] += 1
            if rec.get("ok"):
                out["restarts_ok"] += 1
            else:
                out["restarts_failed"] += 1
            t = _parse_ev_ts(rec.get("ts"))
            if t is not None:
                if time.strftime("%Y-%m-%d", time.localtime(t)) == day:
                    out["restarts_today"] += 1
                if 0 <= (nowt - t) <= 3600:
                    out["restarts_last_hour"] += 1
            out["last_restart"] = {"ts": rec.get("ts"), "reason": rec.get("reason"),
                                   "ok": rec.get("ok")}
        elif kind == "START":
            out["last_start"] = rec.get("ts")
    return out


def cmd_status(args):
    rt = read_runtime()
    out = {"watchd_pids": daemon_pids("linuxdo_daemon_watchd.py run"),
           "daemon_pids": daemon_pids(),
           "last_browse": _ts(last_browse_ts()) if last_browse_ts() else None,
           "last_progress": _ts(last_progress_ts()) if last_progress_ts() else None,
           "daemon_started": _ts(daemon_started_ts()) if daemon_started_ts() else None,
           "quiet_now": quiet_now(), "runtime": rt,
           # 只读派生（R546 #7）：重启风暴一眼可见 —— 对照 rate_limit_per_hour 上限
           "restarts": restart_audit(),
           # R551：桥可达性（桥不可达时守护必然起不来；此字段让 status 一眼可判）
           "bridge_reachable": bridge_ok(),
           # R556：独立桥监督进程与起桥冷却状态（桥缺失时看门狗会自己拉起）
           "bridge_runner_pids": bridge_runner_pids(),
           "bridge_ensure": {"ts": rt.get("bridge_ensure_ts"),
                             "cooldown_s": BRIDGE_ENSURE_COOLDOWN_S}}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- 自测
def _selftest():
    import tempfile
    fails, checks = [], 0
    global STATE_DIR, EVENTS, LOCK

    def ck(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            fails.append(msg)

    # 生产指纹（自测禁写生产状态）
    # ★只比本器自有产物：browse_log 由**另一进程的 daemon** 持续写入，把它计入指纹会恒假红
    #   （判据=结构，不绑现场内容/RULE 25）；「本器不碰 daemon 状态文件」由 T15 源码级锁住。
    def fp():
        h = {}
        for p in (EVENTS, LOCK, os.path.join(STATE_DIR, "runtime.json")):
            h[p] = (os.path.exists(p), os.path.getsize(p) if os.path.exists(p) else 0)
        return h

    before = fp()

    # T1 决策：守护不在 ⇒ RESTART
    d = decide({"pids": [], "last_done": _now() - 10, "now": _now(),
                "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "RESTART" and d["reason"] == "DAEMON_DEAD", "T1 死进程应重启: %s" % d)
    # T2 冷却内 ⇒ 不重启
    d = decide({"pids": [], "last_done": None, "now": _now(),
                "quiet": False, "last_restart_ts": _now() - 10})
    ck(d["action"] == "RESTART_SKIP" and d["reason"] == "RESTART_COOLDOWN", "T2 冷却应抑制: %s" % d)
    # T3 活着且新鲜 ⇒ OK
    d = decide({"pids": [123], "last_done": _now() - 60, "now": _now(),
                "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "OK" and d["reason"].startswith("ALIVE_FRESH"), "T3 活且新应 OK: %s" % d)
    # T4 活着但停滞（活动窗）⇒ RESTART
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "RESTART" and d["reason"].startswith("STALLED"), "T4 停滞应重启: %s" % d)
    # T5 停滞但静默窗 ⇒ OK（不打扰）
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": True, "last_restart_ts": 0})
    ck(d["action"] == "OK" and d["reason"] == "STALL_IN_QUIET_WINDOW", "T5 静默窗不应重启: %s" % d)
    # T6 无日志（刚起）⇒ OK 而非误判停滞
    d = decide({"pids": [123], "last_done": None, "now": _now(),
                "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "OK" and d["reason"] == "NO_LOG_YET", "T6 无日志应 OK: %s" % d)

    # T6b 心跳声明长睡 ⇒ OK（R558：静默窗 6h 长睡不再被误判停滞）
    _hb = {"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
           "quiet": False, "last_restart_ts": 0, "next_wake_ts": _now() + 3600, "phase": "quiet"}
    d = decide(_hb)
    ck(d["action"] == "OK" and d["reason"].startswith("RESTING_UNTIL"),
       "T6b 心跳声明长睡应 OK: %s" % d)
    # T6c 心跳已过期（说好的醒来时刻已过）⇒ 仍按停滞处理
    d = decide(dict(_hb, next_wake_ts=_now() - 10))
    ck(d["action"] == "RESTART" and d["reason"].startswith("STALLED"), "T6c 过期心跳仍应重启: %s" % d)
    # T6d 坏值/超远（> RESTING_MAX_S）⇒ 回落旧判据（不无限信任声明）
    d = decide(dict(_hb, next_wake_ts=_now() + RESTING_MAX_S + 3600))
    ck(d["action"] == "RESTART", "T6d 超远心跳应回落旧判据: %s" % d)

    # T7 单轮：假探针（死）⇒ 调 spawn + 留痕 + 告警
    calls = {"spawn": 0, "notify": []}
    with tempfile.TemporaryDirectory() as td:
        STATE_DIR = os.path.join(td, "watch")
        EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
        LOCK = os.path.join(STATE_DIR, "watchd.lock")
        r = one_round(dry=False, spawn=lambda dry=False: (calls.__setitem__("spawn", calls["spawn"] + 1) or {"rc": 0}),
                      notify_fn=lambda t: calls["notify"].append(t),
                      snapshot={"pids": [], "last_done": _now(), "quiet": False})
        ck(r["action"] == "RESTART" and calls["spawn"] == 1, "T7 死进程应触发一次 spawn: %s" % r)
        ck(any("重启" in t for t in calls["notify"]), "T7b 重启应告警")
        ck(read_runtime().get("last_restart_ts"), "T7c 应记录重启时刻")
        # T7d 停滞但守护在跑 ⇒ 必先 stop 再 start（R558：否则 rc=4 假「拉起失败」）
        calls["stop"] = 0
        calls["spawn"] = 0          # 归零：T8 断言的是「T7 起共 spawn 1 次」，本用例自带计数
        r = one_round(dry=False,
                      spawn=lambda dry=False: (calls.__setitem__("spawn", calls["spawn"] + 1) or {"rc": 0}),
                      stop=lambda dry=False: (calls.__setitem__("stop", calls["stop"] + 1) or {"rc": 0}),
                      notify_fn=lambda t: calls["notify"].append(t),
                      snapshot={"pids": [123], "last_done": _now() - STALL_S - 100,
                                "quiet": False, "last_restart_ts": 0})
        ck(r["action"] == "RESTART" and calls["stop"] == 1 and calls["spawn"] == 1,
           "T7d 停滞且守护在跑应先停再起: %s %s" % (r, calls))
        # T8 冷却内第二次 ⇒ 不再 spawn
        r2 = one_round(dry=False, spawn=lambda dry=False: (calls.__setitem__("spawn", calls["spawn"] + 1) or {"rc": 0}),
                       notify_fn=lambda t: None,
                       snapshot={"pids": [], "last_done": _now(), "quiet": False})
        ck(r2["action"] == "RESTART_SKIP" and calls["spawn"] == 1, "T8 冷却内不得二次重启: %s" % r2)
        # T9 重启失败 streak + 达阈值告警
        writes = {"last_restart_ts": 0, "fail_streak": FAIL_STREAK_NOTIFY - 1}
        with open(os.path.join(STATE_DIR, "runtime.json"), "w", encoding="utf-8") as f:
            json.dump(writes, f)
        msgs = []
        one_round(dry=False, spawn=lambda dry=False: {"rc": 1, "err": "boom"},
                  notify_fn=lambda t: msgs.append(t),
                  snapshot={"pids": [], "last_done": _now(), "quiet": False})
        ck(any("失败" in m for m in msgs), "T9 连续失败达阈值应告警: %s" % msgs)
        # T10 恢复通知：曾告警下过 + 现 OK ⇒ 发恢复并清零
        write_runtime({"notified_down": True})
        msgs2 = []
        one_round(dry=False, spawn=lambda dry=False: {"rc": 0}, notify_fn=lambda t: msgs2.append(t),
                  snapshot={"pids": [4242], "last_done": _now() - 30, "quiet": False})
        ck(any("已恢复" in m for m in msgs2), "T10 恢复应通知: %s" % msgs2)
        # T11 幂等锁：自己写锁后 acquire 应拒绝第二实例
        with open(LOCK, "w", encoding="utf-8") as f:
            json.dump({"pid": 99999999, "ts": _ts()}, f)
        ck(acquire_lock(pid=os.getpid()) is True, "T11 僵锁应可回收")
        with open(LOCK, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "ts": _ts()}, f)   # 自己（真活）⇒ 视为占用
        ck(acquire_lock(pid=os.getpid()) is True, "T11b 同 pid 幂等")
        # T12 dry-run 不 spawn、不告警
        ck(one_round(dry=True, spawn=lambda dry=False: {"dry": True},
                     notify_fn=lambda t: fails.append("T12 dry-run 不该告警")) is not None, "T12 返回")
        os.remove(LOCK)
        # T13 事件文件可读且含 RESTART
        txt = open(EVENTS, encoding="utf-8").read() if os.path.exists(EVENTS) else ""
        ck("RESTART" in txt, "T13 事件应留痕")

        # ---- T27 重启审计视图（R546 #7，只读派生）：结构判据，不绑现场内容（RULE 25）
        # 固定锚点=当日 12:00 本地时，使「今日/近一小时」判定与时区无关且确定
        _day = time.strftime("%Y-%m-%d", time.localtime(_now()))
        _noon = time.mktime(time.strptime(_day + " 12:00:00", "%Y-%m-%d %H:%M:%S"))
        _rows27 = [
            {"kind": "RESTART", "ok": True, "reason": "DAEMON_DEAD", "ts": _ts(_noon - 7200)},
            {"kind": "START", "ts": _ts(_noon - 3500)},
            {"kind": "RESTART", "ok": True, "reason": "STALLED_1800s", "ts": _ts(_noon - 60)},
            {"kind": "RESTART", "ok": False, "reason": "RESTART_EXITED", "ts": _ts(_noon - 86400)},
            {"kind": "RESTART", "ok": True, "reason": "ENV_RECOVERED", "ts": _ts(_noon - 3 * 86400)},
            {"kind": "OK", "ts": _ts(_noon)},
        ]
        with open(EVENTS, "w", encoding="utf-8") as f:
            for _r in _rows27:
                f.write(json.dumps(_r, ensure_ascii=False) + "\n")
            f.write("not-json\n")           # 负控：坏行不得计入也不得抛
        ra = restart_audit(now=_noon)
        ck(ra["restarts_total"] == 4 and ra["restarts_ok"] == 3 and ra["restarts_failed"] == 1,
           "T27 重启计数口径（只数 RESTART）: %s" % ra)
        ck(ra["restarts_today"] == 2, "T27b 今日应 2 次（锚点日 11:00/11:59）: %s" % ra["restarts_today"])
        ck(ra["restarts_last_hour"] == 1, "T27c 近一小时应 1 次: %s" % ra["restarts_last_hour"])
        ck((ra["last_restart"] or {}).get("reason") == "ENV_RECOVERED"
           and (ra["last_restart"] or {}).get("ok") is True,
           "T27d 最近一次取末条 RESTART（含原因）: %s" % ra["last_restart"])
        ck(ra["last_start"] == _ts(_noon - 3500), "T27e 最近 START: %s" % ra["last_start"])
        ck(ra["rate_limit_per_hour"] == RESTART_RATE_PER_HOUR and not ra["note"],
           "T27f 须带对照上限且无 note: %s" % ra)
        # T27g 负控：台账缺失 ⇒ 零计数 + note（不得伪造）
        os.remove(EVENTS)
        ra2 = restart_audit(now=_noon)
        ck(ra2["restarts_total"] == 0 and ra2["note"] == "no_events_file",
           "T27g 缺台账须零计数+note: %s" % ra2)
        # T27h 负控：全坏行 ⇒ 零计数且不抛
        with open(EVENTS, "w", encoding="utf-8") as f:
            f.write("garbage\n{}\n")
        ra3 = restart_audit(now=_noon)
        ck(ra3["restarts_total"] == 0 and ra3["restarts_failed"] == 0 and not ra3["note"],
           "T27h 坏行不得计入: %s" % ra3)

    STATE_DIR = os.path.join(ROOT, "temp", "linuxdo_watch")
    EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
    LOCK = os.path.join(STATE_DIR, "watchd.lock")

    # T14 只读铁律：本器源码不得含任何站点访问原语（★禁自指：token 拼装写，且只扫自测段之前的正文）
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    body = src.split("# ---------------------------------------------------------------- 自测")[0]
    for bad in ("run" + "_js", "web_" + "scan", "web_execute" + "_js",
                "urllib" + ".request", "requests" + ".", "cur" + "l "):
        ck(bad not in body, "T14 看门狗禁含站点访问原语: %s" % bad)
    # T15 不得写守护 state.json（源码级；只判「写路径」——正文里描述性的提及（如 docstring 声明不写）不算）
    # ★禁自指：token 拼装写，且只扫自测段之前的正文
    _STATE_TOK = "state" + ".json"
    _WRITE_HINTS = ("write_text", "json.dump", "open(", "write(")
    _bad = [l for l in body.split("\n") if _STATE_TOK in l and any(h in l for h in _WRITE_HINTS)]
    ck(not _bad, "T15 存在写守护 state 的代码路径: %s" % (_bad[:3],))
    # T16 生产指纹不变（自测禁写生产状态）
    after = fp()
    ck(before == after, "T16 自测写了生产状态: %s -> %s" % (before, after))
    # T17 status 的 watchd 自匹配：`status` 进程自身不得被算作看门狗（用假 ps 行做结构判据）
    _fake = "  12345 1 python3 tools/linuxdo_daemon_watchd.py status"
    _orig_ps = globals()["_ps_lines"]
    try:
        globals()["_ps_lines"] = lambda pat=None: [_fake]
        ck(daemon_pids("linuxdo_daemon_watchd.py run") == [],
           "T17 status 进程不得自匹配为看门狗: %s" % (daemon_pids("linuxdo_daemon_watchd.py run"),))
    finally:
        globals()["_ps_lines"] = _orig_ps

    # T18 停滞但环境忙（无会话/前台被占）⇒ BLOCKED_ENV（不重启，重启无用）
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": False, "last_restart_ts": 0,
                "busy_hint": True, "busy_reason": "RC_NO_SESSION_RETRY"})
    ck(d["action"] == "BLOCKED_ENV" and "ENV_BUSY_RC_NO_SESSION_RETRY" in d["reason"],
       "T18 环境忙应判 BLOCKED_ENV: %s" % d)
    # T18b 环境忙但静默窗 ⇒ 仍按静默窗处理（不误报）
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": True, "last_restart_ts": 0, "busy_hint": True, "busy_reason": "X"})
    ck(d["action"] == "OK", "T18b 静默窗优先: %s" % d)
    # T19 一小时内已重启 N 次 ⇒ RESTART_SKIP（防重启风暴，缺陷①）
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": False, "last_restart_ts": 0, "busy_hint": False,
                "restart_times": [_now() - 60, _now() - 120, _now() - 180]})
    ck(d["action"] == "RESTART_SKIP" and d["reason"].startswith("RESTART_RATELIMIT"),
       "T19 超频应抑制: %s" % d)
    # T19b 超一小时的旧重启不计入
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": False, "last_restart_ts": 0, "busy_hint": False,
                "restart_times": [_now() - 3700, _now() - 7200, _now() - 10800]})
    ck(d["action"] == "RESTART", "T19b 旧记录不应计入限速: %s" % d)
    # T20 环境忙单轮：不 spawn、告警一次且带冷却
    with tempfile.TemporaryDirectory() as td2:
        STATE_DIR = os.path.join(td2, "watch"); EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
        LOCK = os.path.join(STATE_DIR, "watchd.lock")
        c20 = {"spawn": 0, "notify": []}
        sp = lambda dry=False: (c20.__setitem__("spawn", c20["spawn"] + 1) or {"rc": 0})
        snap_busy = {"pids": [7], "last_done": _now() - STALL_S - 100, "quiet": False,
                     "busy_hint": True, "busy_reason": "前台跳过"}
        r = one_round(dry=False, spawn=sp, notify_fn=lambda t: c20["notify"].append(t), snapshot=dict(snap_busy))
        ck(r["action"] == "BLOCKED_ENV" and c20["spawn"] == 0, "T20 环境忙不得重启: %s" % r)
        ck(len(c20["notify"]) == 1 and "不重启" in c20["notify"][0], "T20b 应告警一次: %s" % c20["notify"])
        one_round(dry=False, spawn=sp, notify_fn=lambda t: c20["notify"].append(t), snapshot=dict(snap_busy))
        ck(len(c20["notify"]) == 1, "T20c 同类告警应冷却: %s" % c20["notify"])
        # T21 环境恢复 ⇒ 发一次「已恢复」并清旗标
        msgs21 = []
        one_round(dry=False, spawn=sp, notify_fn=lambda t: msgs21.append(t),
                  snapshot={"pids": [7], "last_done": _now() - 30, "quiet": False, "busy_hint": False})
        ck(any("已恢复" in m for m in msgs21), "T21 恢复应通知: %s" % msgs21)
        ck(not read_runtime().get("blocked_notified_ts"), "T21b 恢复应清零告警旗标")
    STATE_DIR = os.path.join(ROOT, "temp", "linuxdo_watch")
    EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
    LOCK = os.path.join(STATE_DIR, "watchd.lock")
    # T22 busy 探针：真日志（近窗含忙标记）⇒ True；过期日志 / 无标记 ⇒ False
    _o_root, _o_bg = globals()["ROOT"], globals()["BG_NAME"]
    try:
        with tempfile.TemporaryDirectory() as td3:
            d3 = os.path.join(td3, "temp", "bg_task"); os.makedirs(d3, exist_ok=True)
            globals()["ROOT"] = td3; globals()["BG_NAME"] = "zzprobe"
            lp = os.path.join(d3, "zzprobe.log")
            open(lp, "w", encoding="utf-8").write("[%s] RC_NO_SESSION_RETRY 无会话\n" % _ts())
            bh, why = browse_busy_hint()
            ck(bh and why == "RC_NO_SESSION_RETRY", "T22 近窗忙标记应命中: %s/%s" % (bh, why))
            open(lp, "w", encoding="utf-8").write("[%s] all good, browsing\n" % _ts())
            ck(browse_busy_hint()[0] is False, "T22b 无忙标记不得误报")
            open(lp, "w", encoding="utf-8").write("[%s] RC_NO_SESSION_RETRY old\n"
                                                  % _ts(_now() - BUSY_LOG_S * 5))
            ck(browse_busy_hint()[0] is False, "T22c 过期忙标记不得计入")
    finally:
        globals()["ROOT"], globals()["BG_NAME"] = _o_root, _o_bg

    # T23 冷启动不得误杀：browse_log 末条来自上一会话（很旧）但本进程刚起 ⇒ OK（R545 缺陷①）
    d = decide({"pids": [123], "last_done": _now() - STALL_S * 3, "started": _now() - 60,
                "now": _now(), "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "OK" and d["reason"].startswith("ALIVE_FRESH"),
       "T23 冷启动（进程刚起/旧末条）不得判停滞: %s" % d)
    # T23b 启动即已在窗口内静默但未满一个窗口 ⇒ 仍 OK
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 5, "started": _now() - (STALL_S - 5),
                "now": _now(), "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "OK", "T23b 窗口未满不得重启: %s" % d)
    # T24 进程起得够久且真无产出 ⇒ 仍判停滞（放宽不得吞掉真卡死）
    d = decide({"pids": [123], "last_done": _now() - STALL_S * 2, "started": _now() - STALL_S * 2,
                "now": _now(), "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "RESTART" and d["reason"].startswith("STALLED"),
       "T24 老进程真停滞仍须重启: %s" % d)
    # T24b 无 started 字段（旧注入形态）⇒ 行为与修复前逐字一致
    d = decide({"pids": [123], "last_done": _now() - STALL_S - 100, "now": _now(),
                "quiet": False, "last_restart_ts": 0})
    ck(d["action"] == "RESTART" and d["reason"].startswith("STALLED"),
       "T24b 无 started 时行为不变: %s" % d)
    # T25 多源心跳：取 browse_log 末条与进度文件 mtime 的最大值
    import tempfile as _tf
    _tp = _tf.mkdtemp(prefix="ljqhb")
    os.makedirs(os.path.join(_tp, "temp", "linuxdo_browse_daemon"), exist_ok=True)
    sj = os.path.join(_tp, "temp", "linuxdo_browse_daemon", "state.json")
    open(sj, "w").write("{}")
    old_b = _now() - 10000
    got = last_progress_ts(browse_ts=old_b, root=_tp)
    ck(abs(got - os.path.getmtime(sj)) < 2, "T25 进度文件较新应取胜: %s" % got)
    fut = _now() + 5000                                   # 比任何 mtime 都新的 browse 末条
    ck(abs(last_progress_ts(browse_ts=fut, root=_tp) - fut) < 1,
       "T25b browse_log 较新应取胜")
    ck(last_progress_ts(browse_ts=0, root=_tp) is not None, "T25c 至少返回进度文件 mtime")
    # T25d 两个来源都无 ⇒ None（不得伪造时间）；browse_ts=0 表示「无 browse 来源」
    ck(last_progress_ts(browse_ts=0, root=_tf.mkdtemp(prefix="ljqhb2")) is None,
       "T25d 无任何来源须 None")
    # T26 etime 解析（ps 两格式 + 非法）
    ck(_etime_seconds("06:39") == 399, "T26 mm:ss: %s" % _etime_seconds("06:39"))
    ck(_etime_seconds("01:02:03") == 3723, "T26b hh:mm:ss")
    ck(_etime_seconds("2-01:00:00") == 176400, "T26c dd-hh:mm:ss")
    ck(_etime_seconds("oops") is None, "T26d 非法须 None")

    # ---- T26 通知通道：必须解析到 reflect_scripts/reflect_common.py 且真能 import
    _rs = _reflect_scripts_dir()
    ck(bool(_rs) and os.path.isfile(os.path.join(_rs, "reflect_common.py")),
       "T26 须解析到 reflect_scripts (reflect_common.py 存在)")
    if _rs:
        _r = subprocess.run([sys.executable, "-c",
                             "import sys;sys.path.insert(0,%r);import reflect_common;print('OK')" % _rs],
                            capture_output=True, text=True, timeout=60)
        ck(_r.returncode == 0 and "OK" in _r.stdout,
           "T26b reflect_common 须可 import（否则告警静默丢失）")
    # 负控：把候选目录指向空目录 ⇒ 必须返回 None（不得伪造通道）
    _tf26 = _tf.mkdtemp(prefix="ljqnotify")      # 负控：候选目录里没有 reflect_common.py ⇒ None
    ck(_reflect_scripts_dir([_tf26]) is None, "T26c 负控：空候选目录须返回 None")

    # ---- T28 桥探活（R551）：真起一个回环 HTTP 服务 ⇒ True；关掉 ⇒ False（fail-closed）
    import http.server as _hs
    import threading as _th
    class _H(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *a):
            pass
    _srv = _hs.HTTPServer(("127.0.0.1", 0), _H)
    _sport = _srv.server_address[1]
    _th.Thread(target=_srv.serve_forever, daemon=True).start()
    ck(bridge_ok([("127.0.0.1", _sport)]) is True, "T28 回环服务在 ⇒ 桥判可达")
    _srv.shutdown(); _srv.server_close()
    ck(bridge_ok([("127.0.0.1", _sport)], timeout=0.5) is False,
       "T28b 服务关闭 ⇒ 判不可达（fail-closed：宁可不重启）")
    ck(all(h == "127.0.0.1" for h, _p in BRIDGE_TARGETS),
       "T28c 默认桥目标必须全为回环: %s" % (BRIDGE_TARGETS,))
    # ---- T29/T30 桥不可达 ⇒ BRIDGE_DOWN：不重启、只告警一次（R551 核心修复）
    ck(decide({"pids": [], "now": _now(), "last_done": None, "quiet": False,
               "bridge_ok": False})["action"] == "BRIDGE_DOWN",
       "T29 无进程且桥不可达 ⇒ BRIDGE_DOWN（不得空转重启）")
    ck(decide({"pids": [], "now": _now(), "last_done": None, "quiet": False,
               "bridge_ok": True, "last_restart_ts": 0})["action"] == "RESTART",
       "T29b 桥可达才允许重启守护")
    ck(decide({"pids": [1], "last_done": _now() - STALL_S - 100, "now": _now(),
               "quiet": False, "busy_hint": False, "bridge_ok": False})["action"] == "BRIDGE_DOWN",
       "T29c 停滞但桥不可达 ⇒ BRIDGE_DOWN（不重启）")
    ck(decide({"pids": [], "now": _now(), "last_done": None, "quiet": False,
               "last_restart_ts": 0})["action"] == "RESTART",
       "T29d 负控：旧注入形态（无 bridge_ok 键）行为与修复前逐字一致")
    with tempfile.TemporaryDirectory() as td30:
        STATE_DIR = os.path.join(td30, "watch"); EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
        LOCK = os.path.join(STATE_DIR, "watchd.lock")
        c30 = {"spawn": 0, "notify": []}
        _snap30 = {"pids": [], "last_done": None, "now": _now(), "quiet": False,
                   "bridge_ok": False, "last_restart_ts": 0}
        r30 = one_round(dry=True,
                        spawn=lambda **k: (c30.__setitem__("spawn", c30["spawn"] + 1),
                                           {"rc": 0})[1],
                        notify_fn=lambda t: c30["notify"].append(t), snapshot=dict(_snap30))
        ck(r30["action"] == "BRIDGE_DOWN" and c30["spawn"] == 0,
           "T30 桥不可达须零 spawn: %s spawn=%d" % (r30, c30["spawn"]))
        ck(len(c30["notify"]) == 1 and "桥" in c30["notify"][0],
           "T30b 桥不可达须告警一次: %s" % (c30["notify"],))
        one_round(dry=True, spawn=lambda **k: {"rc": 0},
                  notify_fn=lambda t: c30["notify"].append(t), snapshot=dict(_snap30))
        ck(len(c30["notify"]) == 1, "T30c 同因冷却期内不得重复告警: %s" % (c30["notify"],))
    # ---- T31 合并告警（R551）：同类窗口内只发一条；窗口到期发一条含累计次数的汇总
    c31 = {"n": []}
    _h31 = {}

    def _col31(t):
        c31["n"].append(t)
    for _ in range(3):
        _alert_restart({"reason": "STALLED_9000s"}, True, _h31, _col31, {})
    ck(len(c31["n"]) == 1 and "已拉起" in c31["n"][0],
       "T31 同类 3 次只发 1 条（防刷屏）: %s" % (c31["n"],))
    _h31["RESTART"]["summarized_ts"] = _now() - ALERT_MERGE_WINDOW_H * 3600 - 1
    _alert_restart({"reason": "STALLED_9000s"}, True, _h31, _col31, {})
    ck(len(c31["n"]) == 2 and "累计 4 次" in c31["n"][1],
       "T31b 窗口到期发汇总且含累计次数: %s" % (c31["n"],))
    _alert_restart({"reason": "SPAWN_RC4"}, False, _h31, _col31, {})
    ck(len(c31["n"]) == 3 and "重启失败" in c31["n"][2],
       "T31c 失败类独立计数且首见即发: %s" % (c31["n"],))
    ck(_h31["RESTART"]["count"] == 4 and _h31["RESTART_FAIL"]["count"] == 1,
       "T31d 计数按 kind 分桶: %s" % (_h31,))
    # ---- T32 沙盒助手（R551）：必须同步重定向 STATE_DIR/EVENTS/LOCK，且可复原
    _sb32 = _tf.mkdtemp(prefix="ljqsb32")
    _old32, _new32 = sandbox_state(_sb32)
    ck(EVENTS == os.path.join(_sb32, "watchd.jsonl") and LOCK == os.path.join(_sb32, "watchd.lock"),
       "T32 助手须同步重定向 EVENTS/LOCK: %s" % (_new32,))
    ck(EVENTS != os.path.join(ROOT, "temp", "linuxdo_watch", "watchd.jsonl"),
       "T32b 重定向后不得仍指生产日志路径")
    sandbox_state(_old32[0])
    ck(EVENTS == os.path.join(_old32[0], "watchd.jsonl") and STATE_DIR == _old32[0],
       "T32c 复原后应回到生产路径: %s" % (STATE_DIR,))
    # ---- T33 桥不可达 ⇒ 自动起独立桥（R556）：四态 + one_round 联动 + 负控
    import tempfile as _tf33
    _sb = _tf33.mkdtemp(prefix="ljqsb33")
    sandbox_state(_sb)
    _calls = {"spawn": 0}
    _spawn33 = lambda name, argv, dry=False: (_calls.__setitem__("spawn", _calls["spawn"] + 1)
                                              or {"rc": 0, "out": "ok"})
    # T33a 桥已可达 ⇒ UP，零起进程
    st33a, _ = ensure_bridge(probe=lambda: True, runner_pids=lambda: [], spawn=_spawn33)
    ck(st33a == "UP" and _calls["spawn"] == 0, "T33a 桥可达须 UP 且零 spawn: %s" % st33a)
    # T33b 冷却窗口内 ⇒ SKIPPED_COOLDOWN，绝不重复拉
    write_runtime({"bridge_ensure_ts": _now()})
    st33b, _ = ensure_bridge(probe=lambda: False, runner_pids=lambda: [], spawn=_spawn33)
    ck(st33b == "SKIPPED_COOLDOWN" and _calls["spawn"] == 0,
       "T33b 冷却期内须 SKIPPED_COOLDOWN 且零 spawn: %s spawn=%d" % (st33b, _calls["spawn"]))
    # T33c 冷却过期 ⇒ 真起独立桥，桥起来后 STARTED；且落 bridge_ensure_ts 供下轮冷却
    write_runtime({"bridge_ensure_ts": 0})
    _seq = {"n": 0}

    def _probe33():
        _seq["n"] += 1
        return _seq["n"] >= 2
    st33c, _i = ensure_bridge(probe=_probe33, runner_pids=lambda: [], spawn=_spawn33, wait_s=0.05,
                              sleep_fn=lambda s: None)
    ck(st33c == "STARTED" and _calls["spawn"] == 1 and float(read_runtime().get("bridge_ensure_ts") or 0) > 0,
       "T33c 冷却过期须起独立桥并落冷却戳: %s spawn=%d" % (st33c, _calls["spawn"]))
    # T33d 起桥后仍不可达 ⇒ START_FAILED（不得假报成功）
    st33d, _i = ensure_bridge(probe=lambda: False, runner_pids=lambda: [], spawn=_spawn33, wait_s=0.05,
                              sleep_fn=lambda s: None, now=_now() + BRIDGE_ENSURE_COOLDOWN_S + 1)
    ck(st33d == "START_FAILED", "T33d 起桥后仍不可达须 START_FAILED（fail-closed）: %s" % st33d)
    # T33e 监督进程已在跑但仍探不到 ⇒ ALREADY_RUNNING（不重复起）
    _calls["spawn"] = 0
    st33e, _i = ensure_bridge(probe=lambda: False, runner_pids=lambda: [12345], spawn=_spawn33,
                              wait_s=0.05, sleep_fn=lambda s: None)
    ck(st33e == "ALREADY_RUNNING" and _calls["spawn"] == 0,
       "T33e 监督进程在跑须不重复起: %s spawn=%d" % (st33e, _calls["spawn"]))
    # T33f 生产默认（不注入 pids/probe）：真探本机桥 ⇒ UP 且零 spawn（不得干扰座舱桥）
    _calls["spawn"] = 0
    st33f, _u = ensure_bridge(probe=lambda: True, spawn=_spawn33, runner_pids=lambda: [])
    ck(st33f == "UP" and _calls["spawn"] == 0, "T33f 生产桥在时零动作: %s" % st33f)
    # ---- T33g/T33h one_round 联动：桥恢复后本轮继续按 RESTART 走；起桥失败则维持 BRIDGE_DOWN
    sandbox_state(_sb)
    _c33 = {"spawn": 0, "notify": []}
    _snap33 = {"pids": [], "last_done": None, "now": _now(), "quiet": False,
               "bridge_ok": False, "last_restart_ts": 0}
    r33 = one_round(dry=False,
                    spawn=lambda dry=False: (_c33.__setitem__("spawn", _c33["spawn"] + 1) or {"rc": 0}),
                    notify_fn=lambda t: _c33["notify"].append(t), snapshot=dict(_snap33),
                    ensure=lambda dry=False: ("STARTED", {}))
    ck(r33["action"] == "RESTART" and _c33["spawn"] == 1,
       "T33g 桥自动拉起后本轮须恢复重启守护: %s spawn=%d" % (r33["action"], _c33["spawn"]))
    sandbox_state(_sb)
    _c33b = {"spawn": 0, "notify": []}
    r33b = one_round(dry=False,
                     spawn=lambda dry=False: (_c33b.__setitem__("spawn", _c33b["spawn"] + 1) or {"rc": 0}),
                     notify_fn=lambda t: _c33b["notify"].append(t), snapshot=dict(_snap33),
                     ensure=lambda dry=False: ("START_FAILED", {"err": "x"}))
    ck(r33b["action"] == "BRIDGE_DOWN" and _c33b["spawn"] == 0 and len(_c33b["notify"]) == 1,
       "T33h 起桥失败须维持 BRIDGE_DOWN（不空转重启）: %s spawn=%d notify=%d"
       % (r33b["action"], _c33b["spawn"], len(_c33b["notify"])))
    # T33i 负控：dry 模式下 ensure 不得真起桥（先清零上一用例落的冷却戳）
    sandbox_state(_sb)
    write_runtime({"bridge_ensure_ts": 0})
    st33i, _i = ensure_bridge(dry=True, probe=lambda: False, runner_pids=lambda: [], spawn=_spawn33)
    ck(st33i == "DRY" and _calls["spawn"] == 0, "T33i dry-run 不得真起桥: %s" % st33i)

    # 复原生产路径（防后续用例误写沙箱）
    STATE_DIR = os.path.join(ROOT, "temp", "linuxdo_watch")
    EVENTS = os.path.join(STATE_DIR, "watchd.jsonl")
    LOCK = os.path.join(STATE_DIR, "watchd.lock")
    print("selftest: %d checks, %d failed" % (checks, len(fails)))
    for f in fails:
        print("  FAIL:", f)
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="L站浏览守护看门狗（零站点流量）")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("ensure"); p.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    p.add_argument("--interval", type=float, default=CHECK_INTERVAL); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("run"); p.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    p.add_argument("--interval", type=float, default=CHECK_INTERVAL); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    sub.add_parser("status")
    sub.add_parser("once")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    cmd = args.cmd or "status"
    if cmd == "ensure":
        return cmd_ensure(args)
    if cmd == "run":
        return cmd_run(args)
    if cmd == "once":
        print(json.dumps(one_round(), ensure_ascii=False, indent=1))
        return 0
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
