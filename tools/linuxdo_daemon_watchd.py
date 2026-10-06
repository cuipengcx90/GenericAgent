#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L站浏览守护「看门狗」——零站点流量的进程级自愈（2026-10-05）

背景：linuxdo_browse_daemon.py 是长驻守护（承担浏览动作 + 等级前置 + 缺项路由），
一旦进程死掉，只能等下一个定时入口（09:30 / 12:40）才被拉起 ⇒ 最长空窗可达 ~21h，
期间「浏览/等级检查/排队动作」全部停摆，且**用户看不见**（静默失效）。

本器只做三件事（全部本地、零站点请求、零 LLM token）：
  1. 探活：守护进程不在 ⇒ 用 bg_task 拉起（幂等）；
  2. 停滞：在活动窗内长时间没有浏览产出（browse_log 无新增 DONE）⇒ 判为卡死并重启；
  3. 留痕 + 告警：事件写 temp/linuxdo_watch/watchd.jsonl，重启/恢复各发一次私信（带冷却，防刷屏）。

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
                 restart_times(list[ts])
    返回 {action:'OK'|'RESTART'|'RESTART_SKIP'|'BLOCKED_ENV', reason:str}

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
        if not cooldown_ok:
            return {"action": "RESTART_SKIP", "reason": "RESTART_COOLDOWN"}
        return {"action": "RESTART", "reason": "DAEMON_DEAD"}
    hb = max([v for v in (last, started) if v]) if (last or started) else None
    if hb is None:
        return {"action": "OK", "reason": "NO_LOG_YET"}
    age = now - hb
    if age > STALL_S and not quiet:
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
def one_round(dry=False, spawn=None, notify_fn=None, snapshot=None):
    """一轮巡检。snapshot 非 None 时跳过探针（仅供自测注入，生产恒为探针）。"""
    spawn = spawn or spawn_daemon
    notify_fn = notify_fn or notify
    rt = read_runtime()
    if snapshot is not None:
        snap = dict(snapshot)
        snap.setdefault("last_restart_ts", rt.get("last_restart_ts"))
        snap.setdefault("fail_streak", rt.get("fail_streak") or 0)
        snap.setdefault("now", _now())
    else:
        bh, bwhy = browse_busy_hint()
        _pids = daemon_pids()
        snap = {"pids": _pids, "last_done": last_progress_ts(),
                "started": daemon_started_ts(_pids), "now": _now(),
                "quiet": quiet_now(), "last_restart_ts": rt.get("last_restart_ts"),
                "fail_streak": rt.get("fail_streak") or 0,
                "busy_hint": bh, "busy_reason": bwhy,
                "restart_times": rt.get("restart_times") or []}
    d = decide(snap)
    _log_event("ROUND", action=d["action"], reason=d["reason"],
               n_pids=len(snap["pids"]), quiet=snap["quiet"],
               last=snap.get("last_done"))
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
        if d["action"] == "OK" and (rt.get("notified_down") or rt.get("blocked_notified_ts")):
            notify_fn("✅ L站浏览守护已恢复（看门狗）· %s" % _ts())
            write_runtime({"notified_down": False, "blocked_notified_ts": 0, "fail_streak": 0})
        return dict(d, pids=snap["pids"])
    res = spawn(dry=dry)
    ok = (res.get("rc") == 0) or res.get("dry")
    _rt_hist = [t for t in (rt.get("restart_times") or []) if t and (_now() - t) <= 3600]
    _rt_hist.append(_now())
    write_runtime({"restart_times": _rt_hist, "last_restart_ts": _now(),
                   "fail_streak": 0 if ok else (rt.get("fail_streak") or 0) + 1})
    _log_event("RESTART", reason=d["reason"], ok=bool(ok), res=res)
    if ok and not dry:
        notify_fn("⚠️ L站浏览守护被看门狗重启：%s（%s）已拉起，节奏参数未变。"
                  % (d["reason"], _ts()))
    elif not ok and not dry:
        fs = (read_runtime().get("fail_streak") or 0)
        if fs >= FAIL_STREAK_NOTIFY:
            notify_fn("❌ L站浏览守护重启失败 ×%d：%s（详见 temp/linuxdo_watch/watchd.jsonl）"
                      % (fs, res))
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
           "restarts": restart_audit()}
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
