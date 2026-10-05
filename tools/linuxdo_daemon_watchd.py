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
FAIL_STREAK_NOTIFY = 3       # 连续重启失败达 N 次 ⇒ 告警（并继续重试）
DEFAULT_MAX_HOURS = 26.0     # 长驻上限；由每日入口重新武装（对齐 anyrouter watchd 模式）


# ---------------------------------------------------------------- 基础工具
def _now():
    return time.time()


def _ts(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t if t is not None else _now()))


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


def notify(text):
    """飞书私信告警（复用 GA 既有通道；失败不致命）。"""
    try:
        sys.path.insert(0, HERE)
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
           "--", "python3", "tools/linuxdo_browse_daemon.py", "watch"]
    if dry:
        return {"dry": True, "cmd": " ".join(cmd)}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return {"rc": r.returncode, "out": (r.stdout or "").strip()[-400:],
                "err": (r.stderr or "").strip()[-200:]}
    except Exception as e:
        return {"rc": -1, "err": repr(e)[:200]}


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

    snapshot 键：pids(list) / last_done(ts|None) / now / quiet(bool) /
                 last_restart_ts / fail_streak
    返回 {action: 'OK'|'RESTART'|'RESTART_SKIP' , reason: str}
    """
    now = snapshot["now"]
    pids = snapshot.get("pids") or []
    last = snapshot.get("last_done")
    quiet = bool(snapshot.get("quiet"))
    lr = float(snapshot.get("last_restart_ts") or 0)
    cooldown_ok = (now - lr) >= RESTART_MIN_GAP

    if not pids:
        if not cooldown_ok:
            return {"action": "RESTART_SKIP", "reason": "RESTART_COOLDOWN"}
        return {"action": "RESTART", "reason": "DAEMON_DEAD"}
    if last is None:
        return {"action": "OK", "reason": "NO_LOG_YET"}
    age = now - last
    if age > STALL_S and not quiet:
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
        snap = {"pids": daemon_pids(), "last_done": last_browse_ts(), "now": _now(),
                "quiet": quiet_now(), "last_restart_ts": rt.get("last_restart_ts"),
                "fail_streak": rt.get("fail_streak") or 0}
    d = decide(snap)
    _log_event("ROUND", action=d["action"], reason=d["reason"],
               n_pids=len(snap["pids"]), quiet=snap["quiet"],
               last=snap.get("last_done"))
    if d["action"] != "RESTART":
        if d["action"] == "OK" and (rt.get("notified_down")):
            notify_fn("✅ L站浏览守护已恢复（看门狗）· %s" % _ts())
            write_runtime({"notified_down": False, "fail_streak": 0})
        return dict(d, pids=snap["pids"])
    res = spawn(dry=dry)
    ok = (res.get("rc") == 0) or res.get("dry")
    write_runtime({"last_restart_ts": _now(),
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


def cmd_status(args):
    rt = read_runtime()
    out = {"watchd_pids": daemon_pids("linuxdo_daemon_watchd.py run"),
           "daemon_pids": daemon_pids(),
           "last_browse": _ts(last_browse_ts()) if last_browse_ts() else None,
           "quiet_now": quiet_now(), "runtime": rt}
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
