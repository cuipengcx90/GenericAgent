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
import json
import os
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
DEFAULT_STALL_FACTOR = 3          # now - last_ts > interval×factor ⇒ 判卡死
STALL_MIN_GRACE_S = 600           # 判卡死的最小宽限（防冷启动/慢轮误判）
RESTART_BACKOFF = [30, 60, 120, 300, 600]   # 连续重启的退避阶梯（秒）
BACKOFF_RESET_S = 1800            # 距上次重启超过这么久 ⇒ 退避计数归零
NOTIFY_DEDUP_S = 1800             # 「同一动作」通知去重窗


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


def spawn_watch(args, minutes_left, child_log):
    """真起子进程（新会话 ⇒ 可整组回收）；命令可被 --child-cmd 覆盖（供演练）。"""
    if args.child_cmd:
        argv = list(args.child_cmd)
    else:
        argv = [sys.executable, WATCH, "--watch"]
        if args.notify:
            argv.append("--notify")
        argv += ["--interval", str(args.interval),
                 "--cooldown", str(args.cooldown),
                 "--fault-streak", str(args.fault_streak),
                 "--fault-cooldown", str(args.fault_cooldown)]
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

    # 2) 卡死阈值口径（阈值=max(interval×factor, 600)）
    write_state(901)
    chk("阈值 900s: 901s 判卡死", decide(me, my_lstart, now - 3600, sf, now, 300)[0],
        "RESTART_STALLED")
    write_state(899)
    chk("NC 阈值 900s: 899s 不判", decide(me, my_lstart, now - 3600, sf, now, 300)[0], "HOLD_OK")

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
    ap.add_argument("--stall-factor", type=float, default=DEFAULT_STALL_FACTOR,
                    help="last_ts 停滞超过 interval×该值 ⇒ 判卡死（默认 3）")
    ap.add_argument("--stall-min-grace", type=int, default=STALL_MIN_GRACE_S,
                    dest="stall_min_grace",
                    help="判卡死的最小宽限秒（默认 %ds；调小仅供演练）" % STALL_MIN_GRACE_S)
    ap.add_argument("--fault-streak", type=int, default=3, dest="fault_streak",
                    help="转发给子进程：连续 N 轮故障才报（默认 3）")
    ap.add_argument("--fault-cooldown", type=int, default=21600, dest="fault_cooldown",
                    help="转发给子进程：故障重复提醒间隔秒")
    ap.add_argument("--state-dir", default=None, help="状态目录（演练/隔离用）")
    ap.add_argument("--log", default=None, help="守护日志路径（默认 <state-dir>/watchd.log）")
    ap.add_argument("--child-cmd", default=None,
                    help="演练用：覆盖子进程命令（空格分隔，如 '/bin/sleep 30'）")
    ap.add_argument("--no-notify", action="store_false", dest="notify", default=True,
                    help="不推任何通知（子进程也不带 --notify）")
    ap.add_argument("--backoff", default=None,
                    help="连续重启的退避阶梯（秒，逗号分隔；默认 %s）"
                         % ",".join(str(x) for x in RESTART_BACKOFF))
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
        print(json.dumps({
            "watch": WATCH,
            "child_argv": ["<python>", WATCH, "--watch", "--notify",
                           "--interval", args.interval, "--cooldown", args.cooldown,
                           "--state-dir", sd] + (["--max-minutes", str(int(args.max_hours * 60))]),
            "check_every": args.check_every,
            "max_hours": args.max_hours,
            "stall_rule": "now - last_ts > max(%ds×%.1f, %ds)" % (args.interval, args.stall_factor,
                                                                  STALL_MIN_GRACE_S),
            "state_dir": sd,
            "notify": args.notify,
            "dry_run": args.dry_run,
        }, ensure_ascii=False, indent=2))
        return 0
    return supervise(args)


if __name__ == "__main__":
    sys.exit(main())
