#!/usr/bin/env python3
"""
bg_task.py — 一次性长任务的「起后台 + 事后轮询」管理器（start / status / tail / stop / list）

为什么需要它（缺口，不是 tmux 的重写）
--------------------------------------------------------------------
本仓库既有 `memory/tmux_utils.py`（tmux 会话管理，287 行，本机 tmux 3.6a 已装）已经覆盖了
「起会话 / 删会话 / 列会话 / 抓屏」这套人机交互原语；本工具**不重复实现那一套**，只补下面 3 个
它按设计就没有的缺口（详见交付报告的能力比对表）：

  G1 退出码语义：tmux 的会话存活 ≠ 命令结果，拿不到 exit code（没有「正常退出 vs 崩」的区分）。
  G2 PID 身份与复用检测：记录 pid **和** `ps -o lstart=` 启动时刻，`lstart` 不一致即判定 PID 被复用
     （僵尸元数据 ≠ 运行中）；tmux_utils 只给 session_exists，无法识别。
  G3 持久日志文件 + 机器可读状态：输出落**文件**（可 tail/grep/被别的工具读），状态落 JSON 注册表；
     tmux 的 pane 缓冲是内存里的，本机 `~/.tmux.conf` 写死 `history-limit 50000`（实测 3 万行完整保留、
     超过上限即截断），且不是文件。

另外它把 RULES#15 的配方固化成代码：**本机没有 `setsid`**（`which setsid` = NOT FOUND，
所以 `setsid nohup ... &` 这类配方在本机会静默失败/不生效），正确做法是
`subprocess.Popen(..., start_new_session=True)` —— 本工具内部即用此法，并 `stdin=DEVNULL` 断开输入、
把 stdout/stderr 重定向进日志文件，因此**父进程（code_run）即使被超时强杀，子进程照跑**。

用法速查
--------------------------------------------------------------------
    python3 tools/bg_task.py start --name job1 -- python3 slow_script.py --flag
    python3 tools/bg_task.py start --name job2 --shell 'cd /tmp && make -j8'
    python3 tools/bg_task.py status                # 全部
    python3 tools/bg_task.py status --name job1 --json
    python3 tools/bg_task.py tail --name job1 --lines 40
    python3 tools/bg_task.py stop --name job1 --grace 5      # SIGTERM → 超时升级 SIGKILL
    python3 tools/bg_task.py list
    python3 tools/bg_task.py --state-dir /tmp/bt selftest  # 隔离自测（真起真杀）

状态机（status 输出）
--------------------------------------------------------------------
    RUNNING              进程活着，且 `ps -o lstart=` 与登记值一致
    ZOMBIE               进程存在但 stat=Z（已退出未回收；对独立会话进程通常很快消失）
    EXITED_OK / EXITED_ERR   退出码文件已落盘（0 / 非 0）
    GONE_UNKNOWN         进程不存在、且没有退出码文件（被 SIGKILL / 机器重启 / 会话外被清）
    STALE_PID_REUSED     PID 被别的进程复用了（登记时的 lstart 与当前不符）⇒ 元数据陈旧，**不是**运行中
    NO_SUCH_TASK         注册表里没有该任务（负控用；rc=3）

设计约束
--------------------------------------------------------------------
* 判活一律走 `ps`，**不用 `os.kill(pid, 0)` 判活**（僵尸/权限会产生假活）。os.killpg 只用于「停止」。
* 停止只针对**本工具登记过的 pid/pgid**（自己的新会话进程组），绝不泛杀 python 进程。
* 每次写注册表都加文件锁（fcntl.flock），并发 start 不会互相覆盖。
* 依赖：仅标准库（Python 3.8+，macOS/Linux）。tmux 不是依赖。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"
DEFAULT_GRACE = 5.0          # SIGTERM 后等待秒数，超时升级 SIGKILL
DEFAULT_LINES = 20


# ─────────────────────────── 基础设施 ───────────────────────────

def repo_root() -> Path:
    """tools/bg_task.py → 仓库根（用于默认 state-dir）。"""
    return Path(__file__).resolve().parent.parent


def default_state_dir() -> Path:
    return repo_root() / "temp" / "bg_task"


class Registry:
    """任务注册表：<state>/tasks.json + 每任务日志/退出码/wrapper。带 flock。"""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.tasks_file = self.dir / "tasks.json"
        self.lock_file = self.dir / ".lock"
        self.logs_dir = self.dir / "logs"
        self.exit_dir = self.dir / "exit"
        self.wrap_dir = self.dir / "wrappers"

    # -- 生命周期 --
    def ensure(self) -> None:
        for d in (self.dir, self.logs_dir, self.exit_dir, self.wrap_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _lock(self, exclusive: bool = True):
        self.ensure()
        fh = open(self.lock_file, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return fh

    # -- 读写 --
    def load(self) -> Dict[str, Dict[str, Any]]:
        if not self.tasks_file.exists():
            return {}
        try:
            return json.loads(self.tasks_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 注册表损坏时不要让使用者以为「没有任务」——显式报错
            raise RuntimeError(f"注册表损坏（JSON 解析失败）：{self.tasks_file}")

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        tmp = self.tasks_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.tasks_file)

    def register(self, name: str, meta: Dict[str, Any]) -> None:
        fh = self._lock()
        try:
            data = self.load()
            data[name] = meta
            self._save(data)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        fh = self._lock(exclusive=False)
        try:
            return self.load().get(name)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def all(self) -> Dict[str, Dict[str, Any]]:
        fh = self._lock(exclusive=False)
        try:
            return self.load()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def paths(self, name: str) -> Tuple[Path, Path, Path]:
        return (self.logs_dir / f"{name}.log", self.exit_dir / f"{name}.exit", self.wrap_dir / f"{name}.sh")


# ─────────────────────────── ps 探针（判活唯一来源） ───────────────────────────

def ps_probe(pid: int) -> Optional[Dict[str, str]]:
    """`ps -p PID -o pid=,stat=,lstart=,etime=,pgid=` → dict；进程不存在返回 None。

    用 ps 而不用 os.kill(pid,0)：后者对僵尸进程/权限不足会给出误导性的「活着」。
    """
    r = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid=,stat=,lstart=,etime=,pgid="],
        capture_output=True, text=True,
    )
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None
    # 例：  617 SN    Fri Sep 25 08:36:43 2026     03:12:45  617
    head = out.split()
    if len(head) < 2:
        return None
    stat = head[1]
    fields = out.split(None, 2)                     # pid, stat, 其余
    rest = fields[2] if len(fields) > 2 else ""
    # lstart 固定 5 段：Www Mmm DD HH:MM:SS YYYY
    parts = rest.split()
    lstart = " ".join(parts[:5]) if len(parts) >= 5 else ""
    tail = parts[5:]
    etime = tail[0] if tail else ""
    pgid = tail[1] if len(tail) > 1 else ""
    return {"pid": str(pid), "stat": stat, "lstart": lstart, "etime": etime, "pgid": pgid}


def task_state(reg: Registry, name: str) -> Dict[str, Any]:
    """计算任务状态（不写盘）。返回 dict：state / pid / detail / exit_code / log / raw ps。"""
    meta = reg.get(name)
    if meta is None:
        return {"name": name, "state": "NO_SUCH_TASK", "detail": f"注册表里没有任务 {name}", "exit_code": None}
    log = Path(meta.get("log", ""))
    exitf = Path(meta.get("exit_file", ""))
    pid = int(meta.get("pid") or 0)
    code = None
    if exitf.exists():
        try:
            code = int((exitf.read_text(encoding="utf-8").strip() or "-1"))
        except ValueError:
            code = -1
    info = ps_probe(pid) if pid else None
    res: Dict[str, Any] = {
        "name": name, "pid": pid, "pgid": meta.get("pgid") or "", "cmd": meta.get("cmd", ""),
        "cwd": meta.get("cwd", ""), "started_at": meta.get("started_at", ""),
        "log": str(log), "log_size": log.stat().st_size if log.exists() else 0,
        "exit_code": code, "ps": info,
    }
    if info is not None and info["stat"].startswith("Z"):
        res.update(state="ZOMBIE", detail="进程已是僵尸态（已退出待回收）")
    elif info is not None:
        if meta.get("lstart") and info["lstart"] and info["lstart"] != meta["lstart"]:
            res.update(state="STALE_PID_REUSED",
                       detail=f"PID {pid} 已被复用（登记 lstart={meta['lstart']!r} ≠ 当前 {info['lstart']!r}）")
        else:
            res.update(state="RUNNING", detail=f"运行中（etime={info['etime']}）")
    elif code is not None:
        res.update(state=("EXITED_OK" if code == 0 else "EXITED_ERR"),
                   detail=f"已退出，退出码 {code}")
    else:
        res.update(state="GONE_UNKNOWN", detail="进程不存在且无退出码文件（可能被 SIGKILL/机器重启/会话外清理）")
    return res


# ─────────────────────────── 子命令 ───────────────────────────

def cmd_start(reg: Registry, args) -> int:
    name = args.name
    if reg.get(name) is not None:
        prev = task_state(reg, name)
        if prev["state"] == "RUNNING":
            print(f"[bg_task] 任务 {name} 已在运行（pid={prev['pid']}），拒绝重复启动；"
                  f"如需重来请先 `stop --name {name}`", file=sys.stderr)
            return 4
        print(f"[bg_task] 同名任务已存在但状态={prev['state']}，覆盖登记并重新启动", file=sys.stderr)

    if args.shell:
        cmd_str = " ".join(args.cmd)
    else:
        cmd_str = shlex.join(args.cmd)
    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else os.getcwd()
    log, exitf, wrapper = reg.paths(name)
    reg.ensure()

    # wrapper：把输出重定向进日志文件，并在结束时落退出码 → 这是 tmux 拿不到的 G1
    wrapper.write_text(
        "#!/bin/bash\n"
        "# 由 tools/bg_task.py 生成，请勿手改\n"
        f"cd {shlex.quote(cwd)} || exit 127\n"
        f"{{\n{cmd_str}\n}} >> {shlex.quote(str(log))} 2>&1\n"
        "__bg_ec=$?\n"
        f"echo $__bg_ec > {shlex.quote(str(exitf))}\n"
        "exit $__bg_ec\n",
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)
    log.write_text("", encoding="utf-8")           # 清空旧日志，避免新旧输出混淆
    if exitf.exists():
        exitf.unlink()

    # ★ 本机无 setsid（which setsid = NOT FOUND）：靠 start_new_session=True 脱离父进程
    #   stdin=DEVNULL 断开输入，输出已由 wrapper 重定向，故父进程被强杀不影响子进程。
    t0 = time.time()
    proc = subprocess.Popen(
        ["/bin/bash", str(wrapper)],
        cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    info = None
    for _ in range(20):                            # 等 ps 能看到（避免瞬时 ENOENT 记空 lstart）
        info = ps_probe(proc.pid)
        if info:
            break
        time.sleep(0.05)
    meta = {
        "name": name, "pid": proc.pid, "pgid": (info or {}).get("pgid", ""),
        "lstart": (info or {}).get("lstart", ""), "cmd": cmd_str, "cwd": cwd,
        "log": str(log), "exit_file": str(exitf), "wrapper": str(wrapper),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "started_epoch": t0, "version": VERSION,
    }
    reg.register(name, meta)
    print(f"[bg_task] started name={name} pid={proc.pid} pgid={meta['pgid']} log={log}")
    print(f"[bg_task] 轮询：python3 {Path(__file__).name} status --name {name}；看日志：tail --name {name}")
    return 0


def cmd_status(reg: Registry, args) -> int:
    names: List[str] = [args.name] if args.name else sorted(reg.all().keys())
    if args.name and reg.get(args.name) is None and not args.json:
        print(f"[bg_task] NO_SUCH_TASK: 注册表里没有任务 {args.name}", file=sys.stderr)
        print(f"[bg_task] 现有任务：{sorted(reg.all().keys()) or '（空）'}", file=sys.stderr)
        return 3
    rows = [task_state(reg, n) for n in names]
    if args.json:
        print(json.dumps(rows if not args.name else rows[0], ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r['name']:<20} {r['state']:<16} pid={r.get('pid') or '-':<8} "
                  f"exit={r.get('exit_code') if r.get('exit_code') is not None else '-':<4} "
                  f"log={r.get('log_size', 0)}B  {r.get('detail','')}")
        if not rows:
            print("（注册表为空）")
    rc = 0
    if args.name and rows and rows[0]["state"] == "NO_SUCH_TASK":
        rc = 3
    return rc


def cmd_tail(reg: Registry, args) -> int:
    meta = reg.get(args.name)
    if meta is None:
        print(f"[bg_task] NO_SUCH_TASK: 注册表里没有任务 {args.name}", file=sys.stderr)
        return 3
    log = Path(meta["log"])
    if not log.exists():
        print(f"[bg_task] 日志文件不存在：{log}", file=sys.stderr)
        return 5
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-args.lines:] if args.lines > 0 else lines
    for ln in tail:
        print(ln)
    print(f"[bg_task] --- {log} 共 {len(lines)} 行，显示末 {len(tail)} 行", file=sys.stderr)
    return 0


def _signal_targets(reg: Registry, name: str):
    st = task_state(reg, name)
    meta = reg.get(name) or {}
    pids: List[int] = []
    if st["state"] == "RUNNING":
        # 以进程组为单位（start_new_session=True ⇒ pgid == 自身 pid），
        # 能连 wrapper 派生的子进程一起收掉，避免只杀 shell 留下孤儿。
        try:
            pgid = int(meta.get("pgid") or meta.get("pid"))
            pids.append(-pgid)                     # 负数 = 进程组
        except (TypeError, ValueError):
            pass
        if not pids:
            pids.append(int(meta["pid"]))
    return st, pids


def cmd_stop(reg: Registry, args) -> int:
    names = [args.name] if args.name else sorted(reg.all().keys())
    if args.name and reg.get(args.name) is None:
        print(f"[bg_task] NO_SUCH_TASK: 注册表里没有任务 {args.name}", file=sys.stderr)
        return 3
    rc = 0
    for name in names:
        st, targets = _signal_targets(reg, name)
        if st["state"] != "RUNNING":
            print(f"[bg_task] {name}: 当前状态 {st['state']}，无需停止（{st['detail']}）")
            continue
        sent = []
        for tgt in targets:
            try:
                os.killpg(-tgt, signal.SIGTERM) if tgt < 0 else os.kill(tgt, signal.SIGTERM)
                sent.append(tgt)
            except ProcessLookupError:
                pass
        # 等宽限
        deadline = time.time() + args.grace
        escalated = False
        while time.time() < deadline:
            if ps_probe(int(reg.get(name)["pid"])) is None:
                break
            time.sleep(0.2)
        else:
            if ps_probe(int(reg.get(name)["pid"])) is not None:      # 仍活 ⇒ 升级 SIGKILL
                escalated = True
                for tgt in sent:
                    try:
                        os.killpg(-tgt, signal.SIGKILL) if tgt < 0 else os.kill(tgt, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                hard_deadline = time.time() + 5.0
                while time.time() < hard_deadline and ps_probe(int(reg.get(name)["pid"])) is not None:
                    time.sleep(0.2)
        st2 = task_state(reg, name)
        flag = "（SIGTERM→SIGKILL 升级）" if escalated else "（SIGTERM 即退出）"
        print(f"[bg_task] {name}: 停止后状态={st2['state']} {flag} {st2['detail']}")
        if st2["state"] in ("RUNNING", "ZOMBIE"):
            rc = 2
    return rc


def cmd_list(reg: Registry, args) -> int:
    data = reg.all()
    if not data:
        print("（注册表为空）")
        return 0
    rows = [task_state(reg, n) for n in sorted(data)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"{'NAME':<20} {'STATE':<16} {'PID':<8} {'EXIT':<5} {'LOG_B':<10} STARTED")
    for r in rows:
        ec = r.get("exit_code")
        print(f"{r['name']:<20} {r['state']:<16} {str(r.get('pid') or '-'):<8} "
              f"{(str(ec) if ec is not None else '-'):<5} {r.get('log_size', 0):<10} {r.get('started_at','')}")
    return 0


# ─────────────────────────── 自测（真起真杀 + 负控） ───────────────────────────

def cmd_selftest(reg: Registry, args) -> int:
    checks: List[Tuple[str, bool, str]] = []

    def check(title: str, ok: bool, detail: str = "") -> None:
        checks.append((title, ok, detail))
        print(f"[selftest] {'PASS' if ok else 'FAIL'} {title} {detail}")

    py = sys.executable
    # 1) 真起长任务 → RUNNING（验收②：真起 + 立即返回）
    t0 = time.time()
    rc = main(["--state-dir", str(reg.dir), "start", "--name", "st_run", "--", py, "-c",
               "import time;print('HELLO_BG',flush=True);time.sleep(6)"])
    started_ok = rc == 0 and (time.time() - t0) < 3.0
    check("start 立即返回（<3s）且 rc=0", started_ok, f"rc={rc} 耗时={time.time()-t0:.2f}s")
    st = task_state(reg, "st_run")
    check("start 后状态=RUNNING（ps+lstart 匹配）", st["state"] == "RUNNING", f"state={st['state']}")

    # 2) 日志确已写入（负控：日志必须非空且含标记）
    time.sleep(1.0)
    log = Path(reg.get("st_run")["log"])
    body = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    check("日志文件已写入且含命令输出", "HELLO_BG" in body, f"log={log.name} size={log.stat().st_size if log.exists() else 0}")

    # 3) 等待自然结束 → EXITED_OK + 退出码 0
    time.sleep(6.0)
    st = task_state(reg, "st_run")
    check("自然退出后 EXITED_OK 且 exit_code=0", st["state"] == "EXITED_OK" and st["exit_code"] == 0,
          f"state={st['state']} exit={st['exit_code']}")

    # 4) 负控：查不存在任务 → rc=3
    rc = main(["--state-dir", str(reg.dir), "status", "--name", "no_such_task"])
    check("负控①：status 不存在任务 rc=3", rc == 3, f"rc={rc}")
    rc = main(["--state-dir", str(reg.dir), "tail", "--name", "no_such_task"])
    check("负控②：tail 不存在任务 rc=3", rc == 3, f"rc={rc}")

    # 5) 负控：陈旧 pid 文件（pid 不存在）→ GONE_UNKNOWN，而不是 RUNNING
    reg.register("st_ghost", {"name": "st_ghost", "pid": 999999, "pgid": "999999", "lstart": "Fri Jan  1 00:00:00 2021",
                              "log": str(reg.paths("st_ghost")[0]), "exit_file": str(reg.paths("st_ghost")[1])})
    st = task_state(reg, "st_ghost")
    check("负控③：死 pid 登记 → GONE_UNKNOWN（不误报 RUNNING）", st["state"] == "GONE_UNKNOWN", f"state={st['state']}")

    # 6) 负控：PID 被复用（用当前进程 pid + 假 lstart）→ STALE_PID_REUSED
    reg.register("st_reuse", {"name": "st_reuse", "pid": os.getpid(), "pgid": str(os.getpid()),
                              "lstart": "Fri Jan  1 00:00:00 2021", "log": str(reg.paths("st_reuse")[0]),
                              "exit_file": str(reg.paths("st_reuse")[1])})
    st = task_state(reg, "st_reuse")
    check("负控④：PID 复用 → STALE_PID_REUSED（不误报 RUNNING）", st["state"] == "STALE_PID_REUSED", f"state={st['state']}")

    # 7) 真 stop：SIGTERM 即退出
    main(["--state-dir", str(reg.dir), "start", "--name", "st_stop", "--", py, "-c", "import time;time.sleep(60)"])
    check("stop 前 RUNNING", task_state(reg, "st_stop")["state"] == "RUNNING")
    rc = main(["--state-dir", str(reg.dir), "stop", "--name", "st_stop", "--grace", "3"])
    st = task_state(reg, "st_stop")
    check("stop 后进程真消失（SIGTERM 路径）", st["state"] != "RUNNING" and rc == 0, f"state={st['state']} rc={rc}")

    # 8) 真 stop：忽略 SIGTERM 的任务 → 必须升级 SIGKILL 才能杀掉
    main(["--state-dir", str(reg.dir), "start", "--name", "st_hard", "--shell",
          "trap '' TERM; while :; do sleep 1; done"])
    time.sleep(0.5)
    check("stop 前 RUNNING（硬骨头）", task_state(reg, "st_hard")["state"] == "RUNNING")
    rc = main(["--state-dir", str(reg.dir), "stop", "--name", "st_hard", "--grace", "2"])
    st = task_state(reg, "st_hard")
    check("忽略 SIGTERM 的任务被 SIGKILL 收掉", st["state"] != "RUNNING", f"state={st['state']} rc={rc}")

    ok = sum(1 for _, o, _ in checks if o)
    print(f"\n[selftest] {ok}/{len(checks)} 用例通过（state-dir={reg.dir}）")
    return 0 if ok == len(checks) else 1


# ─────────────────────────── CLI ───────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bg_task.py",
        description="一次性长任务后台管理器（本机无 setsid ⇒ 用 Popen(start_new_session=True) 脱离父进程）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法速查")[1].split("状态机")[0] if "用法速查" in __doc__ else None,
    )
    p.add_argument("--version", action="version", version=f"bg_task {VERSION}")
    p.add_argument("--state-dir", default=str(default_state_dir()),
                   help=f"注册表/日志目录（默认 {default_state_dir()}；自测请传隔离目录）")
    sub = p.add_subparsers(dest="sub")

    s = sub.add_parser("start", help="启动后台任务（立即返回）")
    s.add_argument("--name", required=True)
    s.add_argument("--cwd", default=None)
    s.add_argument("--shell", action="store_true", help="把命令当 shell 字符串（需引号包裹）")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("status", help="查状态（三态/复用/退出码）")
    s.add_argument("--name", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("tail", help="看日志尾部")
    s.add_argument("--name", required=True)
    s.add_argument("--lines", type=int, default=DEFAULT_LINES)
    s.set_defaults(func=cmd_tail)

    s = sub.add_parser("stop", help="停止（SIGTERM → 超时升级 SIGKILL）")
    s.add_argument("--name", default=None, help="缺省=全部")
    s.add_argument("--grace", type=float, default=DEFAULT_GRACE)
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("list", help="列出全部任务")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("selftest", help="真起真杀自测（建议配 --state-dir 隔离目录）")
    s.set_defaults(func=cmd_selftest)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "sub", None):
        build_parser().print_help()
        return 1
    # start 的 REMAINDER 会带上分隔符 '--'，剥掉
    if args.sub == "start" and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if args.sub == "start" and not args.cmd:
        print("[bg_task] start 需要命令：start --name N -- <cmd...>", file=sys.stderr)
        return 1
    reg = Registry(Path(args.state_dir))
    try:
        return args.func(reg, args)
    except RuntimeError as e:
        print(f"[bg_task] 错误：{e}", file=sys.stderr)
        return 6


if __name__ == "__main__":
    sys.exit(main())
