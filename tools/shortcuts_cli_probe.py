#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS `shortcuts` CLI 单项探针 / 调用器（R439，只读优先，纯 stdlib）

背景（2026-09-27 实测，TODO#13）：
  /usr/bin/shortcuts run <name> -i <in> -o <out> [--output-type UTI] 可把「用户自建快捷指令」
  接进 GA。但**退出码不可信**（坏输入路径也 rc=0）且**可无限挂起**（本机实测：字典输入
  跑到 WFTranslateTextAction(需远程执行) 后 >38s 不返回、-o 不落盘、rc 不返回）。
  ⇒ 唯一可信判据 = ①真执行痕迹（Shortcuts.sqlite 的 ZSHORTCUTRUNEVENT 条数/时间，
     source=commandline）②调用方要的 -o 产物是否落盘。超时与进程组清理必须由调用方强制。

三态判定（classify）：
  UNAVAILABLE  二进制缺失 / 找不到该快捷指令
  NEEDS_AUTH   Automation(TCC) 拒绝（-1743 / errAEEventNotPermitted / Not authorized）
  AVAILABLE    rc=0 ∧ 未超时 ∧ runevent 有增量 ∧（未要产物 或 产物已落盘）
  NOT_PROVEN   rc=0 但**任一证据缺失**（例如 rc=0 却零副作用）⇒ 不得判「可用」
  HANG         触发超时 ⇒ 不得判「可用」

子命令：
  probe                   只读：二进制/清单/运行记录基线（给带时间戳原始输出）
  run <name|id> [-i IN] [-o OUT] [--output-type UTI] [--timeout N]
                          真实调用（默认 15s 硬超时 + 进程组清理 + 前后 runevent 对账）
  selftest                ≥4 用例含负控（不存在的名→UNAVAILABLE；无授权→NEEDS_AUTH
                         且不得判 AVAILABLE；rc=0 无副作用→NOT_PROVEN；超时→HANG；
                         缺 -wal 的快照副本会「假空」= RULE17 负控）

只读边界：本工具不写用户快捷指令库；sqlite 一律读副本（连 -wal/-shm，见 RULE17），
  全程不开源库；--selftest 用合成库，不触碰真库。
"""
import argparse
import datetime
import glob
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

SHORTCUTS_BIN = "/usr/bin/shortcuts"
SHORTCUTS_DB = os.path.expanduser("~/Library/Shortcuts/Shortcuts.sqlite")
RUNEVENT_SQL = ("select count(*), "
                "ifnull(max(datetime(ZDATE+978307200,'unixepoch','localtime')),'-') "
                "from ZSHORTCUTRUNEVENT;")

STATE_AVAILABLE = "AVAILABLE"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_NEEDS_AUTH = "NEEDS_AUTH"
STATE_NOT_PROVEN = "NOT_PROVEN"
STATE_HANG = "HANG"

# ---- 需要授权（Automation / TCC）的实测特征串 ---------------------------------
AUTH_PATTERNS = (
    "-1743", "erraeventnotpermitted", "not authorized", "not permitted",
    "apple events", "未获得授权", "未获授权", "需要授权", "权限不足",
)
NOT_FOUND_PATTERNS = (
    "找不到快捷指令", "could not be found", "no shortcut", "shortcut not found",
    "unknown shortcut",
)


def now_ts():
    return datetime.datetime.now().isoformat(timespec="seconds")


def raw(tag, detail):
    """所有原始输出一律带时间戳（验收①：原始输出须带时间戳）。"""
    line = "[%s] %s" % (now_ts(), tag)
    if detail:
        line += " | " + detail
    print(line)
    return line


# ---------------------------------------------------------------- 进程与运行记录
def safe_run(argv, timeout, cwd=None):
    """跑命令；超时则杀**整个进程组**（shortcuts 会派生 BackgroundShortcutRunner）。
    返回 (rc|None, stdout, stderr, elapsed, timed_out)。"""
    start = time.time()
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True, cwd=cwd)
    except FileNotFoundError as e:
        return None, "", "FileNotFoundError: %s" % e, 0.0, False
    timed_out = False
    out = err = ""
    try:
        out, err = p.communicate(timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        rc = None
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            p.communicate(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out, err = "", ""
    return rc, out or "", err or "", round(time.time() - start, 2), timed_out


def copy_db_with_wal(dst_dir):
    """按 RULE17 连 -wal/-shm 一起拷（缺 -wal 会「假空」）。返回副本路径或 None。"""
    if not os.path.exists(SHORTCUTS_DB):
        return None
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "Shortcuts.sqlite")
    for suf in ("", "-wal", "-shm"):
        src = SHORTCUTS_DB + suf
        if os.path.exists(src):
            shutil.copy2(src, dst + suf)
    return dst


def runevent_snapshot(dst_dir):
    """只读取 ZSHORTCUTRUNEVENT 基线。返回 dict(count,last,copied_with_wal) 或 None。"""
    copied = copy_db_with_wal(dst_dir)
    if not copied:
        return None
    with_wal = os.path.exists(copied + "-wal")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % copied, uri=True, timeout=10)
        try:
            row = con.execute(RUNEVENT_SQL).fetchone()
        finally:
            con.close()
    except Exception as e:
        return {"count": None, "last": "-", "copied_with_wal": with_wal, "error": str(e)}
    return {"count": int(row[0]), "last": row[1], "copied_with_wal": with_wal}


# --------------------------------------------------------------------- 三态判定
def classify(rc, timed_out, stderr, runevent_delta, out_path_requested, out_file_exists,
             binary_present=True, input_missing=False):
    """纯函数：给定证据返回 (state, reason)。负控要点：
    - 无 Automation 权限 ⇒ NEEDS_AUTH，**不得** AVAILABLE
    - rc=0 但零副作用 ⇒ NOT_PROVEN，**不得** AVAILABLE
    - 超时 ⇒ HANG，**不得** AVAILABLE
    - 不存在的名字 ⇒ UNAVAILABLE
    - 输入路径不存在 ⇒ NOT_PROVEN（实测 shortcuts 仍 rc=0 且静默，**不得** AVAILABLE）
    """
    err = (stderr or "").lower()
    if not binary_present:
        return STATE_UNAVAILABLE, "shortcuts 二进制不存在"
    for pat in NOT_FOUND_PATTERNS:
        if pat.lower() in err:
            return STATE_UNAVAILABLE, "找不到该快捷指令（rc=%s）" % rc
    for pat in AUTH_PATTERNS:
        if pat.lower() in err:
            return STATE_NEEDS_AUTH, "Automation/TCC 拒绝（命中特征 %r）" % pat
    if input_missing:
        return STATE_NOT_PROVEN, "输入路径不存在（shortcuts 仍 rc=%s 且无 stderr ⇒ 静默）" % rc
    if timed_out:
        return STATE_HANG, "调用超时未返回（已强杀进程组）⇒ 不可判可用"
    if rc != 0:
        return STATE_UNAVAILABLE, "rc=%s 且非上述可识别原因" % rc
    # rc == 0 ⇒ 必须用副作用自证
    if runevent_delta is None:
        return STATE_NOT_PROVEN, "rc=0 但无法读取运行记录（无法证明真执行）"
    if runevent_delta <= 0:
        return STATE_NOT_PROVEN, "rc=0 但运行记录零增量（rc 不可信）"
    if out_path_requested and not out_file_exists:
        return STATE_NOT_PROVEN, "rc=0 且已执行，但要求的 -o 产物未落盘"
    return STATE_AVAILABLE, "rc=0 ∧ 运行记录 +%d ∧ 产物%s" % (
        runevent_delta, "已落盘" if out_path_requested else "未要求")


# ----------------------------------------------------------------------- 子命令
def cmd_probe(args):
    print("== shortcuts CLI 只读探针 @ %s" % now_ts())
    raw("binary", "%s exists=%s" % (SHORTCUTS_BIN, os.path.exists(SHORTCUTS_BIN)))
    if not os.path.exists(SHORTCUTS_BIN):
        print("STATE: %s（%s）" % (STATE_UNAVAILABLE, "二进制缺失"))
        return 1
    raw("binary.stat", subprocess.run(["ls", "-l", SHORTCUTS_BIN], capture_output=True,
                                      text=True).stdout.strip())
    if not args.no_live:
        rc, out, err, el, to = safe_run([SHORTCUTS_BIN, "list", "--show-identifiers"],
                                        timeout=args.timeout)
        raw("shortcuts list --show-identifiers", "rc=%s elapsed=%ss out=%r err=%r"
            % (rc, el, out.strip()[:400], err.strip()[:200]))
        if err.strip():
            raw("list.stderr.cls", classify(rc, to, err, None, False, False)[0])
    with tempfile.TemporaryDirectory(prefix="sc_probe_") as td:
        snap = runevent_snapshot(td)
        if snap is None:
            raw("runevent", "无 Shortcuts.sqlite（无用户快捷指令库）")
        else:
            raw("runevent", "count=%s last=%s copied_with_wal=%s%s"
                % (snap.get("count"), snap.get("last"), snap.get("copied_with_wal"),
                   (" err=" + snap["error"]) if snap.get("error") else ""))
    return 0


def cmd_run(args):
    with tempfile.TemporaryDirectory(prefix="sc_run_") as td:
        before = runevent_snapshot(td)
        n0 = before.get("count") if before else None
        argv = [SHORTCUTS_BIN, "run", args.name]
        if args.input_path:
            argv += ["-i", args.input_path]
        if args.output_path:
            argv += ["-o", args.output_path]
        if args.output_type:
            argv += ["--output-type", args.output_type]
        raw("invoke", " ".join(argv))
        rc, out, err, el, to = safe_run(argv, timeout=args.timeout)
        raw("result", "rc=%s elapsed=%ss timed_out=%s out=%r err=%r"
            % (rc, el, to, out.strip()[:400], err.strip()[:300]))
        after = runevent_snapshot(td)
        n1 = after.get("count") if after else None
        delta = None if (n0 is None or n1 is None) else (n1 - n0)
        raw("runevent", "before=%s after=%s delta=%s last=%s"
            % (n0, n1, delta, after.get("last") if after else "-"))
        exists = bool(args.output_path) and os.path.exists(args.output_path)
        if args.output_path:
            raw("output", "path=%s exists=%s size=%s"
                % (args.output_path, exists,
                   os.path.getsize(args.output_path) if exists else "-"))
        in_missing = bool(args.input_path) and not os.path.exists(args.input_path)
        if in_missing:
            raw("input", "路径不存在 %s（实测 shortcuts 仍 rc=0 且无 stderr ⇒ 静默）"
                % args.input_path)
        state, why = classify(rc, to, err, delta, bool(args.output_path), exists,
                              input_missing=in_missing)
        raw("STATE", "%s（%s）" % (state, why))
        return 0 if state == STATE_AVAILABLE else 1


# ----------------------------------------------------------------------- selftest
def _mk_db(path, rows, journal_wal=True):
    """合成运行事件库，**返回保持打开的连接**（调用方负责 close）。

    journal_wal=True 时：先把建表 DDL checkpoint 进主库，再插入数据而**不** checkpoint，
    且连接保持打开（否则最后一个连接关闭会自动 checkpoint 并删除 -wal ⇒ 假不了）。
    用途：考 RULE17「缺 -wal 副本读数是假空」。
    """
    con = sqlite3.connect(path)
    if journal_wal:
        con.execute("pragma journal_mode=wal")
    con.execute("create table ZSHORTCUTRUNEVENT (Z_PK integer primary key, ZOUTCOME integer,"
                " ZSHORTCUT integer, ZDATE real, ZIDENTIFIER text, ZSOURCE text)")
    con.commit()
    if journal_wal:
        con.execute("pragma wal_checkpoint(truncate)")  # 表结构入主库、WAL 截断
    for i, (outcome, source) in enumerate(rows, start=1):
        con.execute("insert into ZSHORTCUTRUNEVENT values (?,?,?,?,?,?)",
                    (i, outcome, 1, 812140602.4 + i, "id-%d" % i, source))
    con.commit()
    return con


def _selftest():
    cases = []

    def chk(name, got, want, extra=""):
        ok = (got == want)
        cases.append((name, ok, "got=%s want=%s %s" % (got, want, extra)))

    # ---- classify 用例（含全部点名负控） --------------------------------------
    chk("T1 rc0+有增量+产物在 ⇒ AVAILABLE",
        classify(0, False, "", 1, True, True)[0], STATE_AVAILABLE)
    chk("NC1 不存在快捷指令 ⇒ UNAVAILABLE",
        classify(1, False, "Error: 未能完成该操作。找不到快捷指令\n", None, False, False)[0],
        STATE_UNAVAILABLE)
    chk("NC1b 英文 not found ⇒ UNAVAILABLE",
        classify(1, False, "Error: Shortcut could not be found\n", None, False, False)[0],
        STATE_UNAVAILABLE)
    chk("NC2 无 Automation 权限 ⇒ NEEDS_AUTH（不得 AVAILABLE）",
        classify(1, False, "Not authorized to send Apple events to Shortcuts. (-1743)", None,
                 False, False)[0], STATE_NEEDS_AUTH)
    chk("NC2b 中文授权文案 ⇒ NEEDS_AUTH（不得 AVAILABLE）",
        classify(1, False, "该操作需要授权后才能继续", None, False, False)[0], STATE_NEEDS_AUTH)
    chk("NC3 rc=0 零副作用 ⇒ NOT_PROVEN（不得 AVAILABLE）",
        classify(0, False, "", 0, True, False)[0], STATE_NOT_PROVEN)
    chk("NC3b rc=0 但读不到运行记录 ⇒ NOT_PROVEN",
        classify(0, False, "", None, False, False)[0], STATE_NOT_PROVEN)
    chk("NC4 超时 ⇒ HANG（不得 AVAILABLE）",
        classify(None, True, "", 2, True, True)[0], STATE_HANG)
    chk("NC5 二进制缺失 ⇒ UNAVAILABLE",
        classify(None, False, "", None, False, False, binary_present=False)[0],
        STATE_UNAVAILABLE)
    chk("NC8 输入路径不存在 ⇒ NOT_PROVEN（不得 AVAILABLE）",
        classify(0, False, "", 1, True, False, input_missing=True)[0], STATE_NOT_PROVEN)
    chk("T2 未要产物但真执行 ⇒ AVAILABLE",
        classify(0, False, "", 3, False, False)[0], STATE_AVAILABLE)

    # ---- RULE17 负控：缺 -wal 的副本「假空」 ---------------------------------
    with tempfile.TemporaryDirectory(prefix="sc_st_") as td:
        real = os.path.join(td, "Shortcuts.sqlite")
        con = _mk_db(real, [(1, "commandline"), (1, "apple-event")], journal_wal=True)
        try:
            keep_db = os.path.join(td, "keep.sqlite")
            shutil.copy2(real, keep_db)  # 只留主库（模拟丢 -wal）
            with_wal = os.path.join(td, "with.sqlite")
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(real + suf):
                    shutil.copy2(real + suf, with_wal + suf)
            def count_of(p):
                con2 = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
                try:
                    return con2.execute("select count(*) from ZSHORTCUTRUNEVENT").fetchone()[0]
                except sqlite3.Error:
                    return 0
                finally:
                    con2.close()
            c_full, c_nowal = count_of(with_wal), count_of(keep_db)
        finally:
            con.close()
        chk("T3 连 -wal 副本读数正确", c_full, 2)
        chk("NC6 缺 -wal ⇒ 假空（必须少于真值，证明 RULE17 必要）",
            c_nowal < c_full, True, "nowal=%s full=%s" % (c_nowal, c_full))

    # ---- 原始输出必须带时间戳 ------------------------------------------------
    line = raw("selftest.ts", "probe")
    chk("NC7 原始输出行须带 [ISO 时间戳]", line.startswith("[") and "T" in line[:22], True,
        "line=%r" % line[:60])

    bad = [n for n, ok, _ in cases if not ok]
    print("-- selftest %d/%d PASS" % (len(cases) - len(bad), len(cases)))
    for n, ok, info in cases:
        print("   %s %s | %s" % ("PASS" if ok else "FAIL", n, info))
    if bad:
        print("FAILED: %s" % ", ".join(bad))
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="macOS shortcuts CLI 探针（R439）")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe", help="只读探针（二进制/清单/运行记录基线）")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--no-live", action="store_true", help="不真跑 list（纯本地）")
    r = sub.add_parser("run", help="真实调用（硬超时 + 前后 runevent 对账）")
    r.add_argument("name", help="快捷指令名或 identifier")
    r.add_argument("-i", "--input-path")
    r.add_argument("-o", "--output-path")
    r.add_argument("--output-type")
    r.add_argument("--timeout", type=int, default=15)
    sub.add_parser("selftest", help="自检（含负控）")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
