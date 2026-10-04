#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""anyrouter.top 抢坑监控的**进程守护**（零 LLM token）。

为什么需要它（2026-10-03 用户的意见：「跑出问题应该通知，或者自动重启重试」）
  `anyrouter_slot_watch.py --watch` 是个长驻进程，但它自己只能保证：
    · 单轮异常不致死（try/except）
    · 全坏/全 UNKNOWN 连续 3 轮 ⇒ 推一条「监控链路故障」私信
  它保证不了「进程整个没了」或「进程还在但卡死不动」——这两种情况下，当天
  剩下的时段就全瞎了，而且没人知道（2026-10-03 就出现过我误判「跑挂了」的
  情形，靠人工查进程才发现是虚惊）。

  本守护把这件事做成机器的：
    · 子进程不存在        ⇒ 立刻重启（送一条「已自动重启」通知）
    · 子进程在跑但不产出  ⇒ 由 state.json 的 last_ts 判「卡死」
                             （now - last_ts > interval × --stall-factor），
                             超时即杀子进程组并重启
    · 锁被别的实例持有    ⇒ 不抢、不重启，只登记（每天 08:50 的 sche_tasks
                             启动与守护自身可能重叠，单实例语义靠 watch.lock）
    · 短时间反复重启      ⇒ 指数退避（避免紧循环刷日志/刷通知）
    · 守护自己到点就退    ⇒ 与每日 sche_tasks 相位对齐（--max-hours，默认 23）
                             退出前先收掉子进程，不留孤儿

零成本：纯标准库 + 复用 watch 的通知函数；不发任何 LLM 请求。
本机无 setsid ⇒ 由 GA `tools/bg_task.py start` 拉起（见 SOP 用法段）。

用法
  python3 tools/anyrouter_slot_watchd.py --selftest            # 负控自测
  python3 tools/anyrouter_slot_watchd.py --once --dry-run      # 单轮判断，不 spawn
  python3 tools/anyrouter_slot_watchd.py --hello               # 打印将执行的动作与预算
  python3 tools/anyrouter_slot_watchd.py                       # 常驻守护（长任务用 bg_task 起）

退出码：0 正常收尾 / 1 子进程异常（曾发生过重启）/ 2 输入或配置错（fail-closed）
"""

import argparse
import datetime
import ast
import io
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WATCH = os.path.join(HERE, "anyrouter_slot_watch.py")

DEFAULT_STATE_DIR = os.path.join(REPO, "temp", "anyrouter_watch")
DEFAULT_CHECK_EVERY = 60          # 守护自己的巡检间隔（秒）——不产生任何网络请求
DEFAULT_INTERVAL = 300            # 子进程（watch）的探测间隔（用户 2026-10-03 拍板 5 分钟）
DEFAULT_COOLDOWN = 21600          # 命中推送冷却
DEFAULT_MAX_HOURS = 23            # 守护寿命：与每日 08:50 的 sche_tasks 相位对齐
# ★2026-10-04：watch 端已改「随机间隔 300-600s + 概率长休(×3~6 ⇒ 最长 60min)」⇒
# 旧 factor=3（300×3=900s）会把「长休中的正常静默」误判成卡死并重启（实测已发生一次）。
# 现取 8（300×8=2400s / 600×8=4800s），并**优先信 watch 写进 state 的 next_round_ts**
# （见 decide）⇒ 长休期间不被误杀，真卡死仍能在 ~1-1.3h 内被发现。
DEFAULT_STALL_FACTOR = 8          # now - last_ts > interval×factor ⇒ 判卡死
STALL_MIN_GRACE_S = 600           # 判卡死的最小宽限（防冷启动/慢轮误判）
RESTART_BACKOFF = [30, 60, 120, 300, 600]   # 连续重启的退避阶梯（秒）
BACKOFF_RESET_S = 1800            # 距上次重启超过这么久 ⇒ 退避计数归零
NOTIFY_DEDUP_S = 1800             # 「同一动作」通知去重窗

# ---- 本地面板（2026-10-04 用户要求「别再让我自己跑脚本」）----
PANEL_PORT = 8766                 # 面板默认端口（被占则向后试 20 个）
PANEL_TRIES = 20
PANEL_BIND = "0.0.0.0"            # 绑全网卡 ⇒ 手机点飞书卡片按钮也能通（令牌门禁）
HOLD_STOP_FILE = "hold.stop"      # 与 watch.py 同名同义（面板按钮=同一旗标）
HOLD_MINUTES_FILE = "hold.minutes"
CTL_URL_FILE = "ctl.url"
DEFAULT_HOLD_MINUTES = 3.0        # ★用户口径：默认保活 3 分钟（面板可改，别默认占 30 分钟）


# ------------------------------------------------------------------ 小工具
def beijing_now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def ps_lstart(pid):
    """`ps -o lstart= -p PID` ⇒ 启动时刻字符串；进程不在返回 None。

    ★判活一律走 ps，不用 os.kill(pid, 0)：后者对僵尸/权限不足会给假活，
    且无法识别 PID 复用（watch.lock 的身份判据同样依赖 lstart 逐字相同）。
    """
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="],
                             capture_output=True, text=True, timeout=10)
        s = (out.stdout or "").strip()
        return s or None
    except Exception:
        return None


def ps_pgid(pid):
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "pgid="],
                             capture_output=True, text=True, timeout=10)
        s = (out.stdout or "").strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def read_last_ts(state_file):
    """读 state.json 的 last_ts（ISO 带 +08:00）⇒ aware datetime；缺/坏返回 None。"""
    st = read_json(state_file)
    if not isinstance(st, dict):
        return None
    raw = st.get("last_ts")
    if not raw:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(raw))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return d
    except Exception:
        return None


def lock_holder(lock_file):
    """watch.lock ⇒ (pid, lstart) 当且仅当持有者**真的活着**（与 watch 同口径）。"""
    d = read_json(lock_file)
    if not isinstance(d, dict):
        return None
    pid = str(d.get("pid", ""))
    if not pid.isdigit():
        return None
    pid = int(pid)
    cur = ps_lstart(pid)
    if cur and cur == d.get("lstart"):
        return (pid, cur)
    return None


def decide(child_pid, child_lstart, spawn_ts, state_file, now, interval,
           stall_factor=DEFAULT_STALL_FACTOR, dry_run=False,
           stall_min_grace=STALL_MIN_GRACE_S):
    """纯函数：本轮该干什么 ⇒ (action, reason)。可单测。

    action ∈ START / RESTART_DEAD / RESTART_STALLED / HOLD_BUSY / HOLD_OK
      START            = 从来没起过，现在起
      RESTART_DEAD     = 起过但现在进程没了（含 PID 复用）
      RESTART_STALLED  = 进程在，但 last_ts 太久没动（卡死）
      HOLD_OK          = 进程在且产出新鲜
    """
    if not child_pid:
        return "START", "尚无子进程"
    cur = ps_lstart(child_pid)
    if not cur:
        return "RESTART_DEAD", "子进程 pid=%d 已不存在" % child_pid
    if child_lstart and cur != child_lstart:
        return "RESTART_DEAD", "pid=%d 的启动时刻变了（PID 被复用）" % child_pid
    # 冷启动宽限：刚起不久时 last_ts 可能还是上一代留下的旧值，不据此判卡死
    fresh_s = interval + stall_min_grace
    if spawn_ts and (now - spawn_ts) < fresh_s:
        return "HOLD_OK", "子进程存活（冷启动宽限 %.0fs 内）" % fresh_s
    lt = read_last_ts(state_file)
    if lt is None:
        # 没有 state.json：可能是刚启动还没跑完第一轮；超过宽限仍没有 ⇒ 判卡死
        if spawn_ts and (now - spawn_ts) > fresh_s:
            return "RESTART_STALLED", "超过 %.0fs 仍无 state.json 产出" % fresh_s
        return "HOLD_OK", "存活但尚无产出记录（宽限内）"
    age = now - lt.timestamp()
    limit = max(interval * stall_factor, stall_min_grace)
    # ★2026-10-04：watch 端随机节奏（间隔 300-600s、概率长休到 60min）⇒ 它每轮结束会把
    # 「预期下一轮时刻」(next_round_ts, epoch 秒) 写进 state.json；本处据此把阈值放宽到
    # 「到点 + 宽限」，避免长休期间的正常静默被误判卡死而遭重启。字段缺失/过期 ⇒ 逐字回落
    # 到 interval×factor（旧行为）。
    try:
        _nrt = read_json(state_file).get("next_round_ts")
        if _nrt:
            limit = max(limit, float(_nrt) + stall_min_grace - lt.timestamp())
    except Exception:
        pass
    if age > limit:
        return "RESTART_STALLED", "last_ts 已停滞 %.0fs（阈值 %.0fs）" % (age, limit)
    return "HOLD_OK", "存活且产出新鲜（%.0fs 前）" % age


# ------------------------------------------------------------------ 通知
def _load_watch_module():
    """动态加载 watch 模块以复用其通知函数（同仓同目录，纯 stdlib）。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("anw_watch", WATCH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print("[watchd] 加载 watch 模块失败（通知将降级为仅记日志）: %s" % e, flush=True)
        return None


def notify(text, dry_run=False):
    """复用 watch.send_notifications（飞书私信+钉钉群）；失败只记日志不抛出。"""
    if dry_run:
        print("[watchd][dry-run] 本应推送：\n%s" % text, flush=True)
        return False
    mod = _load_watch_module()
    if mod is None:
        return False
    try:
        return bool(mod.send_notifications(text, dry_run=False))
    except Exception as e:
        print("[watchd] 推送异常（已忽略）: %s" % e, flush=True)
        return False


def build_restart_text(action, reason, n, now, log_path):
    verb = {"START": "启动", "RESTART_DEAD": "自动重启（进程没了）",
            "RESTART_STALLED": "自动重启（进程卡死）"}.get(action, action)
    return "\n".join([
        "🔧 anyrouter 抢坑监控：%s" % verb,
        "时间：%s（北京时间）" % now.strftime("%Y-%m-%d %H:%M:%S"),
        "原因：%s" % reason,
        "本次守护累计重启 %d 次；这只说明**监控进程**在自愈，与站点有没有坑无关。" % n,
        "日志：%s" % log_path,
    ])


# ------------------------------------------------------------------ 守护主循环
def append_ledger(path, rec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def child_argv(args, minutes_left):
    """构造将被 spawn 的子进程 argv。

    ★2026-10-04 教训：本函数产出的参数**必须**与 watch.py 的 argparse 形状一致
    （事故：曾用 str(float) 透传 300.0 而子进程声明 type=int ⇒ 子进程秒退）。
    selftest 里有「形状自测 + 负控」守这一点。
    """
    argv = [sys.executable, WATCH, "--watch"]
    if args.notify:
        argv.append("--notify")
    # ★2026-10-04：随机节奏优先——给了区间就透传（不给则回落固定 --interval 旧行为）
    if getattr(args, "interval_min", 0) and getattr(args, "interval_max", 0):
        argv += ["--interval-min", str(args.interval_min),
                 "--interval-max", str(args.interval_max)]
    else:
        argv += ["--interval", str(args.interval)]
    if getattr(args, "sample", None) is not None:
        argv += ["--sample", str(args.sample)]
    argv += ["--cooldown", str(args.cooldown),
             "--fault-streak", str(args.fault_streak),
             "--fault-cooldown", str(args.fault_cooldown)]
    # ★自动占坑（2026-10-04 用户要求）：命中可用即用探测同 session 抢下并保活，确保「能直接用」
    if getattr(args, "auto_hold_minutes", 0):
        argv += ["--auto-hold-minutes", str(args.auto_hold_minutes),
                 "--hold-cooldown", str(getattr(args, "hold_cooldown", 0) or 0)]
    if getattr(args, "state_dir", None):
        argv += ["--state-dir", args.state_dir]
    if minutes_left:
        argv += ["--max-minutes", str(int(minutes_left))]
    return argv


def spawn_watch(args, minutes_left, child_log):
    """真起子进程（新会话 ⇒ 可整组回收）；命令可被 --child-cmd 覆盖（供演练）。"""
    if args.child_cmd:
        argv = list(args.child_cmd)
    else:
        argv = child_argv(args, minutes_left)
        if args.notify:
            argv.append("--notify")
        # ★2026-10-04：随机节奏优先——给了区间就透传（不给则回落固定 --interval 旧行为）
        if getattr(args, "interval_min", 0) and getattr(args, "interval_max", 0):
            argv += ["--interval-min", str(args.interval_min),
                     "--interval-max", str(args.interval_max)]
        else:
            argv += ["--interval", str(args.interval)]
        if getattr(args, "sample", None) is not None:
            argv += ["--sample", str(args.sample)]
        argv += ["--cooldown", str(args.cooldown),
                 "--fault-streak", str(args.fault_streak),
                 "--fault-cooldown", str(args.fault_cooldown)]
        # ★自动占坑（2026-10-04 用户要求）：命中可用即用探测同 session 抢下并保活，确保「能直接用」
        if getattr(args, "auto_hold_minutes", 0):
            argv += ["--auto-hold-minutes", str(args.auto_hold_minutes),
                     "--hold-cooldown", str(getattr(args, "hold_cooldown", 0) or 0)]
        if args.state_dir:
            argv += ["--state-dir", args.state_dir]
        if minutes_left:
            argv += ["--max-minutes", str(int(minutes_left))]
    os.makedirs(os.path.dirname(child_log), exist_ok=True)
    logf = open(child_log, "a", buffering=1)
    _env = dict(os.environ, PYTHONUNBUFFERED="1")
    # 让子进程的故障/恢复文案里「日志：」指向真正在写的那份（本守护给它的日志）
    try:
        _env["GA_ANYROUTER_LOG_HINT"] = os.path.relpath(child_log, REPO)
    except Exception:
        _env["GA_ANYROUTER_LOG_HINT"] = child_log
    p = subprocess.Popen(argv, cwd=REPO, stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=_env)
    return p, logf


def terminate_child(pid, grace=15.0, proc=None):
    """精确回收**我们自己的**子进程组（绝不泛杀 python）。

    ★proc 必须传（当你是它的父进程时）：子进程被杀后若没人 wait()，它会停在
    ZOMBIE 态，`ps -p <pid>` 照样看得见（lstart 非空）⇒ 只看 ps 会误判「没杀掉」。
    （2026-10-04 自测「真杀」用例栽在这里，实测 1 例。）
    """
    pgid = ps_pgid(pid)
    if pgid:
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(pgid, sig)
            except Exception:
                break
            t0 = time.time()
            while time.time() - t0 < wait:
                if proc is not None and proc.poll() is not None:
                    break
                if ps_lstart(pid) is None:
                    break
                time.sleep(0.5)
            if proc is not None and proc.poll() is not None:
                break
    if proc is not None:
        try:
            proc.wait(timeout=5)          # 收尸，防 ZOMBIE 冒充「还活着」
        except Exception:
            pass


def promote_exit_action(action, reason, last_exit):
    """子进程「自己退了」（rc 已知）时，把 START 升级为 RESTART_EXITED。

    为什么不能只写 START：真 watch 是长驻进程，它自己退出=监控断了，这与
    「冷启动」在诊断上完全不同（要能一眼看出「是它退过、我把它拉起来了」）。
    """
    if action == "START" and last_exit is not None:
        return "RESTART_EXITED", "子进程自行退出 rc=%s ⇒ 重新拉起（原判据：%s）" % (
            last_exit[1], reason)
    return action, reason


def start_panel_if_enabled(args, state_dir, log):
    """按需起本地面板；返回 panel info 或 None。

    ★为什么单独成函数：面板启动要用 log()，而 log 是 supervise 的**局部函数**。
    2026-10-04 真事故：面板启动块写在 `def log` 之前 ⇒ `log=log` 触发
    UnboundLocalError，守护一启动就死（症状=bg_task 记 EXITED_ERR rc=1、
    watchd.log 无新行）。抽函数后调用点被强制排在 def log 之后，并有
    结构性负控自测守着这个顺序（见 selftest「面板启动序」）。
    """
    if not (getattr(args, "panel", True) and not args.dry_run and not args.once):
        return None
    panel = start_panel(state_dir, port=getattr(args, "panel_port", None), log=log)
    if panel:
        log("面板已起：%s（手机点飞书卡片按钮即可停保活）" % panel["urls"]["url"])
    return panel


def supervise(args):
    """守护主循环。--once 只跑一轮巡检/决策（供演练与测试）。"""
    state_dir = os.path.abspath(os.path.expanduser(args.state_dir)) if args.state_dir \
        else DEFAULT_STATE_DIR
    state_file = os.path.join(state_dir, "state.json")
    lock_file = os.path.join(state_dir, "watch.lock")
    daemon_log = args.log or os.path.join(state_dir, "watchd.log")
    child_log = os.path.join(state_dir, "watch.log")
    ledger = os.path.join(state_dir, "watchd.jsonl")
    os.makedirs(state_dir, exist_ok=True)

    # ★2026-10-04：默认保活 3 分钟（用户口径）——只在没设置时才播种，绝不覆盖用户改过的值
    try:
        _hm_path = os.path.join(state_dir, HOLD_MINUTES_FILE)
        if not os.path.exists(_hm_path):
            with open(_hm_path, "w") as f:
                json.dump({"minutes": DEFAULT_HOLD_MINUTES, "by": "default",
                           "ts": beijing_now().isoformat()}, f)
    except Exception:
        pass

    t_start = time.time()
    hard_end = t_start + args.max_hours * 3600 if args.max_hours else None
    child_pid, child_lstart, spawn_ts, child_proc, child_fh = None, None, None, None, None
    last_exit = None                    # (pid, rc)：最近一次「子进程自己退了」
    restarts, last_restart_ts, restart_hist = 0, 0.0, []
    last_notify = {}

    def log(msg):
        line = "[watchd %s] %s" % (beijing_now().strftime("%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        try:
            with open(daemon_log, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    log("启动：巡检 %ds，子进程间隔 %ds，寿命 %sh，state-dir=%s"
        % (args.check_every, args.interval, args.max_hours, state_dir))
    if args.dry_run:
        log("dry-run：只判断、不 spawn、不杀进程")

    # ★顺序铁律：面板启动会调用 log()，必须放在 def log 之后（曾在 def 之前调用 ⇒ UnboundLocalError 起不来）
    panel = start_panel_if_enabled(args, state_dir, log)

    try:
        while True:
            now = time.time()
            if child_proc is not None:
                rc = child_proc.poll()
                if rc is not None:                      # 子进程已退出（无论何因）
                    log("子进程 pid=%s 退出 rc=%s" % (child_pid, rc))
                    try:
                        child_fh and child_fh.close()
                    except Exception:
                        pass
                    child_pid, child_proc, child_fh = None, None, None
                    last_exit = (child_pid or -1, rc)

            action, reason = decide(child_pid, child_lstart, spawn_ts, state_file,
                                    now, args.interval, args.stall_factor,
                                    stall_min_grace=args.stall_min_grace)
            action, reason = promote_exit_action(action, reason, last_exit)
            holder = lock_holder(lock_file)
            if holder and holder[0] != (child_pid or -1):
                # 有别的实例（例如 08:50 的 sche_tasks 起的那个）正持有锁 ⇒ 让位
                log("让位：watch.lock 由 pid=%d 持有（%s）" % holder)
                append_ledger(ledger, {"ts": beijing_now().isoformat(), "action": "HOLD_BUSY",
                                       "holder_pid": holder[0], "reason": reason})
                if args.once:
                    return 0
                time.sleep(args.check_every)
                continue

            if action in ("START", "RESTART_DEAD", "RESTART_STALLED", "RESTART_EXITED"):
                # 退避：连续重启时拉长间隔（防紧循环）
                if action != "START" and (now - last_restart_ts) > BACKOFF_RESET_S:
                    restarts = 0
                backoff = args.backoff or RESTART_BACKOFF
                delay = 0 if action == "START" else backoff[min(restarts, len(backoff) - 1)]
                if action != "START" and (now - last_restart_ts) < delay:
                    log("退避中（%ds 内不再重启）：%s" % (delay, reason))
                    if args.once:
                        return 0
                    time.sleep(args.check_every)
                    continue
                log("%s：%s" % (action, reason))
                append_ledger(ledger, {"ts": beijing_now().isoformat(), "action": action,
                                       "reason": reason, "restarts": restarts,
                                       "dry_run": bool(args.dry_run)})
                if args.dry_run:
                    if args.once:
                        return 0
                    time.sleep(args.check_every)
                    continue
                if action != "START":
                    if child_pid:
                        terminate_child(child_pid, proc=child_proc)
                        child_pid, child_proc, child_fh = None, None, None
                    restarts += 1
                    last_restart_ts = now
                minutes_left = int((hard_end - now) / 60) if hard_end else 0
                try:
                    child_proc, child_fh = spawn_watch(args, minutes_left, child_log)
                except Exception as e:
                    log("spawn 失败：%s" % e)
                    notify("⚠️ anyrouter 守护：子进程起不来（%s）\n时间：%s"
                           % (e, beijing_now().strftime("%Y-%m-%d %H:%M:%S")))
                    if args.once:
                        return 2
                    time.sleep(args.check_every)
                    continue
                child_pid, child_lstart, spawn_ts = child_proc.pid, ps_lstart(child_proc.pid), time.time()
                last_exit = None
                log("已启动子进程 pid=%d%s" % (child_pid, "（演练命令）" if args.child_cmd else ""))
                if action != "START" and args.notify:
                    dedup_key = action
                    if now - last_notify.get(dedup_key, 0) > NOTIFY_DEDUP_S:
                        last_notify[dedup_key] = now
                        notify(build_restart_text(action, reason, restarts,
                                                  beijing_now(), child_log))
            else:
                if args.once:
                    log("巡检：%s（%s）" % (action, reason))
                    return 0

            if hard_end and now >= hard_end:
                log("达到 --max-hours 上限，收尾退出（重启 %d 次）" % restarts)
                if child_pid and not args.dry_run:
                    terminate_child(child_pid, proc=child_proc)
                append_ledger(ledger, {"ts": beijing_now().isoformat(), "action": "EXIT",
                                       "restarts": restarts})
                return 1 if restarts else 0
            if args.once:
                return 0
            time.sleep(args.check_every)
    finally:
        try:
            child_fh and child_fh.close()
        except Exception:
            pass
        if panel:
            stop_panel(panel, state_dir, log=log)
            log("面板已收（ctl.url 已清）")


# ------------------------------------------------------------------ 本地面板
def _read_json_file(path):
    """容错读 JSON（也吃 jsonl：取最后一行能解析的）。"""
    try:
        with open(path) as f:
            txt = f.read().strip()
        try:
            return json.loads(txt)
        except Exception:
            for line in reversed(txt.splitlines()):
                try:
                    return json.loads(line)
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _pick_lan_from_ifconfig(txt, udp_ip=None):
    """从 ifconfig 文本里挑真 LAN 私网 IP；失败回落 udp_ip（再剔假 IP），最后 127.0.0.1。

    ★2026-10-04 真事故：lan_ip() 原来只做 UDP connect("8.8.8.8")，而本机 Clash TUN
    接管路由 ⇒ getsockname() 恒得假 IP 198.18.0.1 ⇒ 写进 ctl.url 的「局域网入口」
    手机根本连不上（面板真身 200 在 192.168.1.13:8766）。故改为 ifconfig 优先、
    显式剔除 Clash 假网段 198.18/198.19（同 output_archive_sop 已知坑）。
    """
    ips = []
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", txt or ""):
        ip = m.group(1)
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue                                   # 回环 / 链路本地
        if ip.startswith("198.18.") or ip.startswith("198.19."):
            continue                                   # Clash TUN 假网段
        if (ip.startswith("192.168.") or ip.startswith("10.")
                or re.match(r"172\.(1[6-9]|2[0-9]|3[01])\.", ip)):
            ips.append(ip)
    if ips:
        for ip in ips:                                 # 家用 Wi-Fi 常见 192.168 优先
            if ip.startswith("192.168."):
                return ip
        return ips[0]
    udp = str(udp_ip or "")
    if udp and not (udp.startswith("198.18.") or udp.startswith("198.19.")) and not udp.startswith("127."):
        return udp
    return "127.0.0.1"


def lan_ip():
    """本机真实局域网 IP：ifconfig 私网地址优先（剔 Clash 假 IP 198.18/198.19），
    再回落 UDP 探测，最后 127.0.0.1。"""
    txt = ""
    try:
        txt = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout or ""
    except Exception:
        txt = ""
    udp = None
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.connect(("8.8.8.8", 80))
            udp = sk.getsockname()[0]
        finally:
            sk.close()
    except Exception:
        udp = None
    return _pick_lan_from_ifconfig(txt, udp)


def write_ctl_url(state_dir, port, token, log=None, lan=None):
    """把面板入口写进 <state-dir>/ctl.url（watch.py 侧 ctl_url() 读它，通知/卡片就用它）。"""
    lan = lan or lan_ip()
    d = {"port": port, "token": token, "ts": beijing_now().isoformat(),
         "url": "http://%s:%d/?t=%s" % (lan, port, token),
         "local_url": "http://127.0.0.1:%d/?t=%s" % (port, token),
         "stop_url": "http://%s:%d/stop?t=%s" % (lan, port, token),
         "stop_local_url": "http://127.0.0.1:%d/stop?t=%s" % (port, token)}
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, CTL_URL_FILE), "w") as f:
        json.dump(d, f)
    return d


def _esc(x):
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _page_shell(title, body):
    return ("<!doctype html><html lang=zh><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>" + _esc(title) + "</title><style>"
            "body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;padding:18px;"
            "background:#0f1115;color:#e8eaed}h1{font-size:20px;margin:0 0 12px}"
            "p,li{line-height:1.6;font-size:15px}.card{background:#171a21;border:1px solid #262b36;"
            "border-radius:12px;padding:14px 16px;margin:12px 0}"
            "a.btn{display:inline-block;background:#2b6cff;color:#fff;text-decoration:none;"
            "padding:14px 20px;border-radius:10px;margin:6px 8px 6px 0;font-size:16px}"
            "a.danger{background:#e5484d}a.ghost{background:#2a2f3a}"
            "code{background:#0b0d11;padding:2px 6px;border-radius:6px;font-size:13px}"
            "input[type=number]{font-size:16px;padding:10px;width:90px;border-radius:8px;border:1px solid #3a4150;"
            "background:#0b0d11;color:#e8eaed}"
            "</style></head><body>" + body + "</body></html>")


def _page_home(state_dir, token, log_path=None):
    st = _read_json_file(os.path.join(state_dir, "state.json")) or {}
    hold = os.path.exists(os.path.join(state_dir, HOLD_STOP_FILE))
    hm = _read_json_file(os.path.join(state_dir, HOLD_MINUTES_FILE)) or {}
    mins = hm.get("minutes")
    age = ""
    try:
        lt = float(st.get("last_ts") or 0)
        if lt:
            age = "（%.1f 分钟前）" % ((time.time() - lt) / 60.0)
    except Exception:
        pass
    rows = ["保活：<b>%s</b>" % ("⏹ 已请求停止（下一轮即收手）" if hold else "▶️ 进行中／等待命中"),
            "保活时长：<b>%s</b>" % (("%g 分钟" % mins) if mins else "未设置（用守护参数）"),
            "最近一轮：%s%s" % (_esc(st.get("last_ts") or "?"), age)]
    for k in ("last_verdict", "last_http", "next_round_ts", "hold_claim", "hold_until",
              "fault_streak", "rounds", "budget_day", "budget_used"):
        if st.get(k) is not None:
            rows.append("%s = %s" % (k, _esc(st.get(k))))
    body = ["<h1>🎯 anyrouter 抢坑监控 · 面板</h1>",
            "<div class=card>" + "<br>".join(rows) + "</div>",
            "<div class=card><b>一键操作</b><p>",
            "<a class=\"btn danger\" href=\"/stop?t=" + token + "\">⏹ 停止保活</a>",
            "<a class=\"btn ghost\" href=\"/?t=" + token + "\">🔄 刷新</a>",
            "<a class=\"btn ghost\" href=\"/status?t=" + token + "\">📄 状态 JSON</a>",
            "<a class=\"btn ghost\" href=\"/log?t=" + token + "\">📜 日志尾部</a>",
            "</p></div>",
            "<div class=card><b>保活时长</b><p>点一下即改，下一轮探测生效（默认 %g 分钟）：</p>" % DEFAULT_HOLD_MINUTES,
            "".join("<a class=\"btn ghost\" href=\"/hold-minutes?m=%g&t=%s\">%g 分钟</a>"
                    % (m, token, m) for m in (3, 10, 30, 60, 120)),
            "<form action=\"/hold-minutes\" method=get>"
            "<input type=hidden name=t value=\"" + token + "\">"
            "<input type=number name=m min=1 step=1 value=\"" + str(int(mins or DEFAULT_HOLD_MINUTES)) + "\"> "
            "<button class=\"btn\" type=submit style=\"display:inline-block;background:#2b6cff;color:#fff;"
            "border:0;padding:14px 20px;border-radius:10px;font-size:16px\">设为该分钟数</button></form></div>",
            "<div class=card><p>也可以<b>直接在对话里说「停保活」</b>（我立刻给你停），"
            "或用命令行 <code>python3 tools/anyrouter_slot_watch.py --stop-hold</code>。</p>"
            "<p>停止是立即生效的：正在跑的保活线程 ≤5 秒收手，新命中会重新决策。</p></div>"]
    return _page_shell("anyrouter 面板", "".join(body))


def _page_ok(msg, token):
    return _page_shell("anyrouter 面板", "<div class=card><h1>" + _esc(msg) + "</h1>"
                       "<p><a class=\"btn\" href=\"/?t=" + token + "\">返回面板</a></p></div>")


def panel_status(state_dir):
    st = _read_json_file(os.path.join(state_dir, "state.json")) or {}
    hm = _read_json_file(os.path.join(state_dir, HOLD_MINUTES_FILE)) or {}
    led = None
    try:
        with open(os.path.join(state_dir, "ledger.jsonl")) as f:
            for line in f:
                if line.strip():
                    led = line.strip()
    except Exception:
        pass
    return {"now": beijing_now().isoformat(),
            "hold_stop_requested": os.path.exists(os.path.join(state_dir, HOLD_STOP_FILE)),
            "hold_minutes": hm.get("minutes"),
            "state": st,
            "last_ledger": _read_json_file(os.path.join(state_dir, "ledger.jsonl")) if False else led}


def panel_handle(path, query, state_dir, token, log_path=None):
    """面板路由（纯函数式：给定 state_dir/token 出响应体）⇒ 自测可不经网络直接驱动。"""
    if not token or token not in (query.get("t") or []):
        return 403, "text/plain", "403 令牌不对：请用通知/卡片里那条带 ?t=… 的完整链接打开。"
    if path in ("/stop", "/stop/"):
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(os.path.join(state_dir, HOLD_STOP_FILE), "w") as f:
                json.dump({"ts": beijing_now().isoformat(), "by": "panel",
                           "ua": (query.get("ua") or [""])[0]}, f)
        except Exception as e:
            return 500, "text/plain", "写停止旗标失败：%s" % e
        return 200, "text/html", _page_ok("⏹ 已请求停止保活（≤5 秒生效）", token)
    if path in ("/hold-minutes", "/hold-minutes/"):
        raw = (query.get("m") or [""])[0]
        try:
            m = float(raw)
            if m <= 0 or m > 24 * 60:
                raise ValueError("out of range")
        except Exception:
            return 400, "text/plain", "400 时长不合法：m 必须是 0~1440 分钟的数字（收到 %r）" % raw
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, HOLD_MINUTES_FILE), "w") as f:
            json.dump({"minutes": m, "ts": beijing_now().isoformat(), "by": "panel"}, f)
        return 200, "text/html", _page_ok("✅ 保活时长已设为 %g 分钟（下一轮生效）" % m, token)
    if path in ("/status", "/status/"):
        return 200, "application/json", json.dumps(panel_status(state_dir), ensure_ascii=False, indent=2)
    if path in ("/log", "/log/"):
        try:
            n = int((query.get("n") or ["60"])[0] or 60)
        except Exception:
            n = 60
        try:
            with open(log_path or os.path.join(state_dir, "watch.log")) as f:
                lines = f.read().splitlines()[-max(1, min(n, 500)):]
        except Exception as e:
            lines = ["(读日志失败：%s)" % e]
        return 200, "text/plain", "\n".join(lines)
    if path in ("/", "/index.html"):
        return 200, "text/html", _page_home(state_dir, token, log_path)
    return 404, "text/plain", "404（可用：/ /stop /hold-minutes /status /log）"


def _panel_server(state_dir, port, token, log_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        server_version = "anyrouter-panel/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _go(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                code, ctype, body = panel_handle(u.path, q, state_dir, token, log_path=log_path)
            except Exception as e:                        # 面板自身出错也绝不 500 裸栈
                code, ctype, body = 500, "text/plain", "面板内部错误：%s" % e
            data = body.encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                pass

        do_GET = _go
        do_POST = _go

    return ThreadingHTTPServer((PANEL_BIND, port), H)


def start_panel(state_dir, port=None, log=None, token=None, lan=None):
    """起本地面板；返回 {server, thread, token, port, urls}；起不来返回 None（不阻塞监控）。"""
    import secrets
    import threading
    token = token or secrets.token_hex(8)
    ports = ([int(port)] if port is not None else []) + list(range(PANEL_PORT, PANEL_PORT + PANEL_TRIES))
    srv, last = None, None
    for p in ports:
        try:
            srv = _panel_server(state_dir, p, token, os.path.join(state_dir, "watch.log"))
            break
        except OSError as e:
            last = e
    if srv is None:
        (log or print)("[面板] 起不来（端口占用？）：%s" % last)
        return None
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    th.start()
    urls = write_ctl_url(state_dir, srv.server_address[1], token, log=log, lan=lan)
    return {"server": srv, "thread": th, "token": token, "port": srv.server_address[1], "urls": urls}


def stop_panel(info, state_dir=None, log=None):
    """收面板：关服务 + 删 ctl.url（别把死链留给下一张通知卡片）。"""
    try:
        info["server"].shutdown()
        info["server"].server_close()
    except Exception:
        pass
    try:
        if state_dir:
            os.unlink(os.path.join(state_dir, CTL_URL_FILE))
    except Exception:
        pass


# ------------------------------------------------------------------ 自测
def selftest():
    """负控自测：卡死判据 / PID 复用 / 锁让位 / 真起真杀。"""
    import tempfile
    cases = []
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        cases.append((name, got, want, good))

    TZ = datetime.timezone(datetime.timedelta(hours=8))
    now = time.time()
    tmp = tempfile.mkdtemp(prefix="anw_watchd_st_")
    sf = os.path.join(tmp, "state.json")
    lf = os.path.join(tmp, "watch.lock")
    me = os.getpid()
    my_lstart = ps_lstart(me)

    def write_state(age_s):
        ts = beijing_now() - datetime.timedelta(seconds=age_s)
        with open(sf, "w") as f:
            json.dump({"last_ts": ts.isoformat(), "last_rc": 0, "last_verdict": "POOL_BUSY"}, f)

    # 1) action 判据
    chk("START: 无子进程", decide(None, None, None, sf, now, 300)[0], "START")
    chk("RESTART_DEAD: pid 不存在", decide(999999, None, now - 3600, sf, now, 300)[0], "RESTART_DEAD")
    chk("RESTART_DEAD: PID 复用(lstart 不符)",
        decide(me, "Mon Jan  1 00:00:00 1970", now - 3600, sf, now, 300)[0], "RESTART_DEAD")
    write_state(10)
    chk("HOLD_OK: 存活且新鲜", decide(me, my_lstart, now - 3600, sf, now, 300)[0], "HOLD_OK")
    write_state(3000)
    chk("RESTART_STALLED: last_ts 停滞 3000s", decide(me, my_lstart, now - 3600, sf, now, 300)[0],
        "RESTART_STALLED")
    chk("NC: 冷启动宽限内不判卡死", decide(me, my_lstart, now - 60, sf, now, 300)[0], "HOLD_OK")
    write_state(600)
    chk("NC: 600s 不足阈值", decide(me, my_lstart, now - 3600, sf, now, 300)[0], "HOLD_OK")
    os.unlink(sf)
    chk("NC: 无 state.json 且在宽限内 ⇒ 不判卡死",
        decide(me, my_lstart, now - 60, sf, now, 300)[0], "HOLD_OK")
    chk("RESTART_STALLED: 无 state.json 且超宽限",
        decide(me, my_lstart, now - 3600, sf, now, 300)[0], "RESTART_STALLED")

    # 2) 卡死阈值口径（阈值=max(interval×factor, stall_min_grace)；factor 显式给定）
    write_state(901)
    chk("阈值 900s(factor=3): 901s 判卡死",
        decide(me, my_lstart, now - 3600, sf, now, 300, stall_factor=3)[0], "RESTART_STALLED")
    write_state(899)
    chk("NC 阈值 900s(factor=3): 899s 不判",
        decide(me, my_lstart, now - 3600, sf, now, 300, stall_factor=3)[0], "HOLD_OK")

    # 2b) ★2026-10-04 随机节奏安全网（这组用例就是「长休被误杀」事故的回归）
    write_state(2300)
    chk("随机节奏: factor 默认 8（阈值 2400s）⇒ 2300s 不判卡死",
        decide(me, my_lstart, now - 3600, sf, now, 300)[0], "HOLD_OK")
    write_state(5000)
    chk("随机节奏: 5000s ⇒ 判卡死（真卡）",
        decide(me, my_lstart, now - 3600, sf, now, 300)[0], "RESTART_STALLED")
    write_state(3000)
    with open(sf) as _f:
        _st = json.load(_f)
    _st["next_round_ts"] = now + 3600          # watch 正在长休（预期下轮在 1h 后）
    with open(sf, "w") as _f:
        json.dump(_st, _f)
    chk("随机节奏: 长休中(next_round_ts 在未来) 3000s 不判卡死",
        decide(me, my_lstart, now - 7200, sf, now, 300)[0], "HOLD_OK")
    chk("随机节奏: 长休中 + 已超「到点+宽限」⇒ 判卡死",
        decide(me, my_lstart, now, sf, now + 7200, 300)[0], "RESTART_STALLED")
    with open(sf) as _f:
        _st = json.load(_f)
    _st["next_round_ts"] = now - 100000        # 早已过期 ⇒ 回落常规阈值
    with open(sf, "w") as _f:
        json.dump(_st, _f)
    chk("随机节奏: next_round_ts 过期 ⇒ 回落常规阈值判卡死",
        decide(me, my_lstart, now - 7200, sf, now, 300)[0], "RESTART_STALLED")
    with open(sf, "w") as _f:
        json.dump({"last_ts": (beijing_now() - datetime.timedelta(seconds=5000)).isoformat(),
                   "next_round_ts": "bad"}, _f)      # 坏值 ⇒ 不得崩，回落常规阈值
    chk("随机节奏: next_round_ts 非数字 ⇒ 不崩仍判卡死",
        decide(me, my_lstart, now - 7200, sf, now, 300)[0], "RESTART_STALLED")

    # 3) 锁让位
    chk("锁: 文件缺失 ⇒ None", lock_holder(lf), None)
    with open(lf, "w") as f:
        json.dump({"pid": me, "lstart": my_lstart}, f)
    chk("锁: 本进程持有 ⇒ 命中", lock_holder(lf), (me, my_lstart))
    with open(lf, "w") as f:
        json.dump({"pid": me, "lstart": "Mon Jan  1 00:00:00 1970"}, f)
    chk("NC 锁: lstart 不符 ⇒ 认为无持有者(可接管)", lock_holder(lf), None)
    with open(lf, "w") as f:
        json.dump({"pid": 999999, "lstart": "x"}, f)
    chk("NC 锁: pid 已死 ⇒ 可接管", lock_holder(lf), None)
    with open(lf, "w") as f:
        f.write("{not json")
    chk("NC 锁: 坏 JSON ⇒ 不崩、可接管", lock_holder(lf), None)

    # 4) state 读取
    with open(sf, "w") as f:
        f.write("{bad")
    chk("NC state: 坏 JSON ⇒ None", read_last_ts(sf), None)
    with open(sf, "w") as f:
        json.dump({"last_ts": "2026-10-03T23:59:10.374051+08:00"}, f)
    got = read_last_ts(sf)
    chk("state: ISO(+08:00) 可解析", got is not None and got.tzinfo is not None, True)

    # 5) 真起真杀（拿 /bin/sleep 当替身，绝不碰真 watch）
    p, fh = spawn_watch(argparse.Namespace(child_cmd=["/bin/sleep", "30"], notify=False,
                                           interval=300, cooldown=21600, fault_streak=3,
                                           fault_cooldown=21600, state_dir=tmp),
                        0, os.path.join(tmp, "child.log"))
    time.sleep(0.6)
    chk("真起: 子进程存活", ps_lstart(p.pid) is not None, True)
    chk("真起: 新会话 pgid≠本进程组", ps_pgid(p.pid) != os.getpgid(0), True)
    terminate_child(p.pid, proc=p)
    time.sleep(0.5)
    chk("真杀: 子进程已回收", ps_lstart(p.pid) is None, True)
    try:
        fh.close()
    except Exception:
        pass

    # 6b) 自然退出升级为 RESTART_EXITED（冷启动 vs 「它退过」必须可区分）
    a, r = promote_exit_action("START", "尚无子进程", (123, 0))
    chk("自然退出：START→RESTART_EXITED", a, "RESTART_EXITED")
    chk("自然退出：原因含 rc", "rc=0" in r, True)
    a2, _ = promote_exit_action("START", "尚无子进程", None)
    chk("NC 冷启动：保持 START", a2, "START")
    a3, _ = promote_exit_action("RESTART_STALLED", "x", (123, 0))
    chk("NC 卡死路径不被改写", a3, "RESTART_STALLED")

    # 6) 文案
    txt = build_restart_text("RESTART_STALLED", "last_ts 停滞 3000s", 2, beijing_now(), "/tmp/x.log")
    chk("文案: 卡死重启含原因", "自动重启（进程卡死）" in txt, True)
    chk("NC 文案: 无密钥", "sk-" in txt, False)

    # 7) ★2026-10-04 形状自测：spawn 出去的 argv 必须能被 watch.py 的 argparse 接受
    #    （事故回归：曾用 str(float) 透传 300.0 而子进程声明 type=int ⇒ 子进程秒退、守护空转）
    class _A:
        pass
    _a = _A()
    for k, v in (("notify", True), ("interval", 60), ("interval_min", 300.0), ("interval_max", 600.0),
                 ("sample", 4), ("cooldown", 21600), ("fault_streak", 3), ("fault_cooldown", 21600),
                 ("auto_hold_minutes", 30.0), ("hold_cooldown", 900.0), ("state_dir", tmp)):
        setattr(_a, k, v)
    base = child_argv(_a, 60)[2:]                      # 去掉解释器，只要参数
    r1 = subprocess.run([sys.executable, WATCH] + base + ["--help"],
                        capture_output=True, text=True)
    chk("形状: spawn argv 可被 watch.py 解析", r1.returncode, 0)
    chk("形状: argv 含随机区间参数", ("--interval-min" in base) and ("--interval-max" in base), True)
    bad = list(base)
    bad[bad.index("--interval-min") + 1] = "notanumber"
    r2 = subprocess.run([sys.executable, WATCH] + bad + ["--help"],
                        capture_output=True, text=True)
    chk("NC 形状: 坏值必须让子进程报错（证明自测能失败）", r2.returncode != 0, True)

    # 8) ★2026-10-04 面板：令牌门禁 / 一键停旗标 / 保活时长 / 真 HTTP 一发（沙盒 state-dir）
    def _lj(d, name):
        try:
            with open(os.path.join(d, name)) as f:
                return json.load(f)
        except Exception:
            return {}
    _pd = tempfile.mkdtemp(prefix="anr_panel_")
    chk("面板: 错令牌 ⇒ 403", panel_handle("/", {"t": ["wrong"]}, _pd, "tok")[0], 403)
    chk("NC 面板: 缺令牌 ⇒ 403（不得裸奔）", panel_handle("/stop", {}, _pd, "tok")[0], 403)
    chk("NC 面板: 403 时不得写停止旗标（先验令牌后动状态）",
        os.path.exists(os.path.join(_pd, HOLD_STOP_FILE)), False)
    _c, _ct, _b = panel_handle("/", {"t": ["tok"]}, _pd, "tok")
    chk("面板: 首页 200/HTML", (_c, "text/html" in _ct), (200, True))
    chk("面板: 首页有「停止保活」按钮且链到 /stop", ("/stop?t=tok" in _b) and ("停止保活" in _b), True)
    chk("面板: 首页有保活时长预设 3 分钟", "/hold-minutes?m=3" in _b, True)
    chk("面板: 首页提示可对话里说「停保活」", "停保活" in _b, True)
    _c, _, _ = panel_handle("/stop", {"t": ["tok"]}, _pd, "tok")
    chk("面板: 一键停 ⇒ 落 hold.stop 旗标",
        (_c, os.path.exists(os.path.join(_pd, HOLD_STOP_FILE))), (200, True))
    _c, _, _ = panel_handle("/hold-minutes", {"t": ["tok"], "m": ["7"]}, _pd, "tok")
    chk("面板: 设保活时长 7 分钟", (_c, _lj(_pd, HOLD_MINUTES_FILE).get("minutes")), (200, 7.0))
    chk("NC 面板: m=0 拒（400）", panel_handle("/hold-minutes", {"t": ["tok"], "m": ["0"]}, _pd, "tok")[0], 400)
    chk("NC 面板: m=abc 拒（400）", panel_handle("/hold-minutes", {"t": ["tok"], "m": ["abc"]}, _pd, "tok")[0], 400)
    chk("NC 面板: m=99999 拒（防自杀式长占）",
        panel_handle("/hold-minutes", {"t": ["tok"], "m": ["99999"]}, _pd, "tok")[0], 400)
    chk("NC 面板: 非法值不得覆盖已设时长", _lj(_pd, HOLD_MINUTES_FILE).get("minutes"), 7.0)
    chk("面板: /status 是 JSON", panel_handle("/status", {"t": ["tok"]}, _pd, "tok")[1], "application/json")
    chk("面板: 未知路径 404", panel_handle("/nope", {"t": ["tok"]}, _pd, "tok")[0], 404)
    _u = write_ctl_url(_pd, 8799, "tok", log=lambda m: None, lan="10.0.0.9")
    chk("面板: ctl.url 写局域网入口", _u["url"], "http://10.0.0.9:8799/?t=tok")
    chk("面板: ctl.url 写 stop 端点", _u["stop_url"], "http://10.0.0.9:8799/stop?t=tok")
    _info = start_panel(_pd, port=0, log=lambda m: None)
    try:
        import urllib.request as _ur
        _p = _info["port"]
        _r = _ur.urlopen("http://127.0.0.1:%d/status?t=%s" % (_p, _info["token"]), timeout=5)
        chk("面板: 真 HTTP /status 200", _r.status, 200)
        _badcode = 0
        try:
            _ur.urlopen("http://127.0.0.1:%d/status?t=bad" % _p, timeout=5)
        except Exception as e:
            _badcode = getattr(e, "code", 0)
        chk("NC 面板: 真 HTTP 错令牌 ⇒ 403", _badcode, 403)
        os.unlink(os.path.join(_pd, HOLD_STOP_FILE))
        _r2 = _ur.urlopen("http://127.0.0.1:%d/stop?t=%s" % (_p, _info["token"]), timeout=5)
        chk("面板: 真 HTTP 一键停 ⇒ 200 且落旗标",
            (_r2.status, os.path.exists(os.path.join(_pd, HOLD_STOP_FILE))), (200, True))
        chk("面板: 真 HTTP /hold-minutes 生效",
            (_ur.urlopen("http://127.0.0.1:%d/hold-minutes?m=12&t=%s" % (_p, _info["token"]),
                         timeout=5).status, _lj(_pd, HOLD_MINUTES_FILE).get("minutes")), (200, 12.0))
    finally:
        stop_panel(_info, _pd, log=lambda m: None)
    chk("面板: 退出即清 ctl.url（别把死链留给通知卡片）",
        os.path.exists(os.path.join(_pd, CTL_URL_FILE)), False)
    import socket as _sk
    _occ = _sk.socket()
    _occ.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
    _occ.bind(("0.0.0.0", _p))
    _occ.listen(1)
    try:
        _i2 = start_panel(_pd, port=_p, log=lambda m: None)
        chk("NC 面板: 真占住端口 ⇒ 向后试端口而非崩", (_i2 or {}).get("port", _p) != _p, True)
        chk("NC 面板: 回退后仍写 ctl.url", bool((_i2 or {}).get("urls", {}).get("url")), True)
        if _i2:
            stop_panel(_i2, _pd, log=lambda m: None)
    finally:
        _occ.close()

    # 9) ★2026-10-04 真事故回归：面板启动序（def log 之前调 start_panel(log=log) ⇒ 守护秒死）
    class _NS:
        pass
    _ns = _NS()
    _ns.panel = True
    _ns.panel_port = 0
    _ns.dry_run = True
    _ns.once = False
    import tempfile as _tf
    _d9 = _tf.mkdtemp(prefix="anr_panelseq_")
    chk("NC 面板序: dry-run ⇒ 不起面板（零副作用）",
        start_panel_if_enabled(_ns, _d9, lambda m: None), None)
    _ns.dry_run = False
    _ns.once = True
    chk("NC 面板序: --once ⇒ 不起面板", start_panel_if_enabled(_ns, _d9, lambda m: None), None)
    _ns.once = False
    _logged9 = []
    _i9 = start_panel_if_enabled(_ns, _d9, _logged9.append)
    chk("面板序: 真起面板并回传 urls", bool((_i9 or {}).get("urls", {}).get("url")), True)
    chk("面板序: 起面板会落日志（证明 log 可用）", len(_logged9) >= 1, True)
    chk("面板序: 起面板即写 ctl.url（卡片按钮才有入口）",
        os.path.exists(os.path.join(_d9, CTL_URL_FILE)), True)
    if _i9:
        stop_panel(_i9, _d9, log=lambda m: None)
    # ★结构性负控：调用点必须排在 `def log(` 之后；拿「旧写法」样本反证本判据会报警
    def _panel_seq_ok(src):
        """★用 AST 判结构序（不查字面串——本函数的 docstring 里就写着该串，字面查必自欺）。"""
        try:
            _tree = ast.parse(src)
        except Exception:
            return False
        _i_log = _i_call = None
        for _fn in [n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == "supervise"]:
            for _st in ast.walk(_fn):
                if isinstance(_st, ast.FunctionDef) and _st.name == "log" and _i_log is None:
                    _i_log = _st.lineno
        for _st in ast.walk(_tree):
            if isinstance(_st, ast.Call) and isinstance(_st.func, ast.Name) \
                    and _st.func.id == "start_panel_if_enabled" and _i_call is None:
                _i_call = _st.lineno
        if _i_log is None or _i_call is None:
            return False
        return _i_log < _i_call
    _src_real = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    chk("面板序: 真源码 调用点在 def log 之后", _panel_seq_ok(_src_real), True)
    _src_bad = ("    panel = start_panel(state_dir, port=None, log=log)\n"
                "    def log(msg):\n        pass\n")
    chk("NC 面板序: 旧写法（先调面板后定义 log）必须判 False",
        _panel_seq_ok(_src_bad), False)

    # §10 局域网入口选择：真 LAN 优先、Clash TUN 假 IP 必剔（2026-10-04 真事故）
    _ifc = ("lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
            "        inet 127.0.0.1 netmask 0xff000000\n"
            "utun6: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1420\n"
            "        inet 198.18.0.1 --> 198.18.0.1 netmask 0xfffff800\n"
            "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "        inet 192.168.1.13 netmask 0xffffff00 broadcast 192.168.1.255\n")
    chk("LAN: 有真 LAN + TUN 假 IP ⇒ 取 192.168.1.13（不取 198.18.0.1）",
        _pick_lan_from_ifconfig(_ifc), "192.168.1.13")
    chk("LAN: 无真 LAN(仅 TUN) ⇒ 回落 127.0.0.1 而非假 IP",
        _pick_lan_from_ifconfig(_ifc.split("en0")[0], "198.18.0.1"), "127.0.0.1")
    chk("NC LAN: 只有 10.x ⇒ 取它", _pick_lan_from_ifconfig("        inet 10.0.0.9 x"), "10.0.0.9")
    chk("NC LAN: 172.20 属私网 ⇒ 取它", _pick_lan_from_ifconfig("        inet 172.20.5.5 x"), "172.20.5.5")
    chk("NC LAN: 172.32 非私网 ⇒ 不取（回落 127）",
        _pick_lan_from_ifconfig("        inet 172.32.1.1 x", "198.18.0.1"), "127.0.0.1")
    chk("NC LAN: 169.254 链路本地 ⇒ 不取",
        _pick_lan_from_ifconfig("        inet 169.254.3.4 x", None), "127.0.0.1")
    chk("NC LAN: 空 ifconfig 但 UDP 有真值 ⇒ 用 UDP",
        _pick_lan_from_ifconfig("", "192.168.1.77"), "192.168.1.77")
    chk("LAN: 真机 lan_ip() 不得是 Clash 假 IP 198.18/198.19",
        lan_ip().startswith("198.18.") or lan_ip().startswith("198.19."), False)

    for name, got, want, good in cases:
        print("%s %s: got=%r want=%r" % ("PASS" if good else "FAIL", name, got, want))
    print("selftest: %d passed, %d failed" % (sum(1 for c in cases if c[3]),
                                             sum(1 for c in cases if not c[3])))
    return 0 if ok else 1


# ------------------------------------------------------------------ CLI
def _build_parser():
    ap = argparse.ArgumentParser(description="anyrouter 抢坑监控进程守护（零 LLM token）")
    ap.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY,
                    help="守护巡检间隔秒（默认 60，不发任何网络请求）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="子进程探测间隔秒")
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="命中推送冷却秒")
    ap.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS,
                    help="守护寿命小时（默认 23；到点收掉子进程退出，由每日定时接棒）")
    ap.add_argument("--interval-min", type=float, default=300.0,
                    help="透传给子进程的随机轮间隔下界秒（默认 300；与 --interval-max 同给才生效）")
    ap.add_argument("--interval-max", type=float, default=600.0,
                    help="透传给子进程的随机轮间隔上界秒（默认 600）")
    ap.add_argument("--sample", type=int, default=None,
                    help="透传给子进程的每轮抽样模型数（默认不传=用子进程默认 4）")
    ap.add_argument("--stall-factor", type=float, default=DEFAULT_STALL_FACTOR,
                    help="last_ts 停滞超过 interval×该值 ⇒ 判卡死（默认 3）")
    ap.add_argument("--stall-min-grace", type=int, default=STALL_MIN_GRACE_S,
                    dest="stall_min_grace",
                    help="判卡死的最小宽限秒（默认 %ds；调小仅供演练）" % STALL_MIN_GRACE_S)
    ap.add_argument("--fault-streak", type=int, default=3, dest="fault_streak",
                    help="转发给子进程：连续 N 轮故障才报（默认 3）")
    ap.add_argument("--fault-cooldown", type=int, default=21600, dest="fault_cooldown",
                    help="转发给子进程：故障重复提醒间隔秒")
    ap.add_argument("--auto-hold-minutes", type=float, default=0, dest="auto_hold_minutes",
                    help="转发给子进程：探测命中可用（200）即自动占坑保活 N 分钟"
                         "（默认 0=只报喜不占坑；占坑=用探测同一 session 定时发 1）")
    ap.add_argument("--hold-cooldown", type=float, default=900.0, dest="hold_cooldown",
                    help="转发给子进程：两次自动占坑之间的最小间隔秒（默认 900）")
    ap.add_argument("--state-dir", default=None, help="状态目录（演练/隔离用）")
    ap.add_argument("--log", default=None, help="守护日志路径（默认 <state-dir>/watchd.log）")
    ap.add_argument("--child-cmd", default=None,
                    help="演练用：覆盖子进程命令（空格分隔，如 '/bin/sleep 30'）")
    ap.add_argument("--no-notify", action="store_false", dest="notify", default=True,
                    help="不推任何通知（子进程也不带 --notify）")
    ap.add_argument("--backoff", default=None,
                    help="连续重启的退避阶梯（秒，逗号分隔；默认 %s）"
                         % ",".join(str(x) for x in RESTART_BACKOFF))
    ap.add_argument("--panel", action="store_true", dest="panel", default=True,
                    help="起本地面板（一键停保活/改保活时长；默认开）")
    ap.add_argument("--no-panel", action="store_false", dest="panel",
                    help="不起本地面板（通知里回落命令行提示）")
    ap.add_argument("--panel-port", type=int, default=None, dest="panel_port",
                    help="面板端口（默认 %d，被占则向后试 %d 个；0=随机）" % (PANEL_PORT, PANEL_TRIES))
    ap.add_argument("--dry-run", action="store_true", help="只判断不 spawn/不杀")
    ap.add_argument("--once", action="store_true", help="只跑一轮巡检/决策（供演练）")
    ap.add_argument("--hello", action="store_true", help="打印将要执行的动作与预算后退出")
    ap.add_argument("--selftest", action="store_true", help="负控自测")
    return ap


def main():
    args = _build_parser().parse_args()
    if args.selftest:
        return selftest()
    if args.child_cmd:
        args.child_cmd = args.child_cmd.split()
    if args.backoff:
        args.backoff = [int(x) for x in str(args.backoff).split(",") if x.strip()]
    if args.hello:
        sd = args.state_dir or DEFAULT_STATE_DIR
        args.state_dir = sd
        print(json.dumps({
            "watch": WATCH,
            # ★单一源：显示的就是真会 spawn 的 argv（别在这里另写一份，会漂移）
            "child_argv": ["<python>"] + child_argv(args, int(args.max_hours * 60))[1:],
            "check_every": args.check_every,
            "auto_hold_minutes": args.auto_hold_minutes,
            "hold_cooldown": args.hold_cooldown,
            "max_hours": args.max_hours,
            "stall_rule": "now - last_ts > max(%ds×%.1f, %ds)" % (args.interval, args.stall_factor,
                                                                  STALL_MIN_GRACE_S),
            "state_dir": sd,
            "notify": args.notify,
            "panel": args.panel,
            "panel_port": args.panel_port,
            "dry_run": args.dry_run,
        }, ensure_ascii=False, indent=2))
        return 0
    return supervise(args)


if __name__ == "__main__":
    sys.exit(main())
