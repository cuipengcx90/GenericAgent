#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L站浏览栈「夜间到早间」稳定性汇报器(R573)。

只读本地文件,零站点请求、零 LLM token:
  - temp/linuxdo_watch/watchd.jsonl  守护事件(重启/恢复/告警)
  - temp/linuxdo_browse_daemon/browse_log.jsonl 浏览产出(DONE/LOITER/FAIL)
  - temp/linuxdo_browse_daemon/state.json       日计数/pending
  - temp/linuxdo_browse_daemon/level_log.jsonl  等级检查
  - temp/linuxdo_browse_daemon/ensure_tab.jsonl 标签自愈台账
  - temp/linuxdo_browse_daemon/risk_log.jsonl   风控留痕
  - temp/linuxdo_browse_daemon/heartbeat.json   守护休眠自声明
  - 进程/端口:pgrep + lsof(只读)

判据(三态):
  OK    窗口内有产出、零重启、无风控、桥可达、守护在跑
  WARN  产出偏少 / 有重启但已自愈 / 桥不可达但已拉起 / 静默窗内无产出(正常)
  ALERT 窗口内零产出且无正常理由 / 守护不在跑 / 风控暂停 / 自愈失败(ok=false)

幂等:同日只发一次(temp/linuxdo_nightly/ledger.jsonl),--force 可跳过。

用法:
  python3 tools/linuxdo_nightly_report.py run [--no-notify] [--window-hours 16] [--force] [--json]
  python3 tools/linuxdo_nightly_report.py --selftest
自测沙箱:env LJQ_NIGHTLY_ROOT=<临时根> 时只读该根下同名相对路径、ledger 也落在其内。
"""
import os
import sys
import json
import time
import subprocess
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ICON = {"OK": "✅", "WARN": "⚠️", "ALERT": "🔴"}

BROWSE_REL = os.path.join("temp", "linuxdo_browse_daemon", "browse_log.jsonl")
STATE_REL = os.path.join("temp", "linuxdo_browse_daemon", "state.json")
LEVEL_REL = os.path.join("temp", "linuxdo_browse_daemon", "level_log.jsonl")
ENSURE_REL = os.path.join("temp", "linuxdo_browse_daemon", "ensure_tab.jsonl")
RISK_REL = os.path.join("temp", "linuxdo_browse_daemon", "risk_log.jsonl")
HEART_REL = os.path.join("temp", "linuxdo_browse_daemon", "heartbeat.json")
WATCHD_REL = os.path.join("temp", "linuxdo_watch", "watchd.jsonl")
LEDGER_REL = os.path.join("temp", "linuxdo_nightly", "ledger.jsonl")

QUIET = (1, 30)  # 静默窗 01:00-07:30(内核设计),窗内无产出属正常
STALL_S = 3 * 3600


def base_root():
    return os.environ.get("LJQ_NIGHTLY_ROOT") or ROOT


def pth(rel):
    return os.path.join(base_root(), rel)


def read_jsonl(rel, max_bytes=8 * 1024 * 1024):
    p = pth(rel)
    out = []
    if not os.path.exists(p):
        return out
    try:
        with open(p, "rb") as f:
            data = f.read()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
            data = data.split(b"\n", 1)[-1]
        for line in data.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return out


def read_json(rel):
    p = pth(rel)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ts_epoch(row):
    ts = row.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None
    return None


def in_window(rows, since, until):
    out = []
    for r in rows:
        e = _ts_epoch(r)
        if e is None:
            continue
        if since <= e <= until:
            out.append(r)
    return out


def quiet_overlap(since, until):
    """窗口内落在静默窗(01:00-07:30)的秒数。窗口通常跨两天。"""
    total = 0.0
    day0 = int(time.strftime("%Y-%m-%d", time.localtime(since)).replace("-", ""))
    for offset in (0, 1, 2):
        try:
            base = time.mktime(time.strptime(str(day0), "%Y%m%d")) + offset * 86400
        except Exception:
            continue
        for (h0, m0), (h1, m1) in (((1, 0), (7, 30)),):
            a = base + h0 * 3600 + m0 * 60
            b = base + h1 * 3600 + m1 * 60
            lo, hi = max(a, since), min(b, until)
            if hi > lo:
                total += hi - lo
    return total


def procs_alive():
    """守护/桥进程只读探测。沙箱(LJQ_NIGHTLY_ROOT)下返回 (None, None) 未知。"""
    if os.environ.get("LJQ_NIGHTLY_ROOT"):
        return None, None
    daemon = None
    try:
        r = subprocess.run(["pgrep", "-f", "linuxdo_browse_daemon.py watch"],
                           capture_output=True, text=True, timeout=15)
        daemon = bool(r.stdout.strip())
    except Exception:
        daemon = None
    bridge = None
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP:18765", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=20)
        bridge = bool(r.stdout.strip())
    except Exception:
        bridge = None
    return daemon, bridge


def diagnose(now=None, window_hours=16.0, override_procs=True):
    now = now or time.time()
    since = now - window_hours * 3600
    browse = in_window(read_jsonl(BROWSE_REL), since, now)
    events = in_window(read_jsonl(WATCHD_REL), since, now)
    risks = in_window(read_jsonl(RISK_REL), since, now)
    ensures = in_window(read_jsonl(ENSURE_REL), since, now)
    levels = in_window(read_jsonl(LEVEL_REL), since, now)
    st = read_json(STATE_REL) or {}
    hb = read_json(HEART_REL) or {}

    done = [r for r in browse if r.get("st") == "DONE"]
    loiter = [r for r in browse if r.get("st") == "LOITER"]
    fails = [r for r in browse if r.get("st") == "FAIL"]

    restarts = [e for e in events if e.get("kind") == "RESTART"]
    r_fail = [e for e in restarts if e.get("ok") is False]
    bridge_down = [e for e in events if e.get("kind") == "BRIDGE_DOWN"]
    notify_fail = [e for e in events if e.get("kind") == "NOTIFY_FAIL"]
    risk_events = [r for r in risks if r.get("st") == "RISK"]

    daemon_alive, bridge_alive = (None, None) if override_procs else procs_alive()
    if override_procs:
        daemon_alive, bridge_alive = procs_alive()

    q_sec = quiet_overlap(since, now)
    span = max(1.0, now - since)
    quiet_ratio = q_sec / span

    hb_age = None
    if hb.get("epoch"):
        hb_age = max(0, now - float(hb["epoch"]))

    # 首条 DONE 相对最后一次重启:判断「重启后是否恢复」
    last_restart = None
    for e in restarts:
        t = _ts_epoch(e)
        if t and (last_restart is None or t > last_restart):
            last_restart = t
    recovered_after_restart = None
    if last_restart is not None:
        recovered_after_restart = any((_ts_epoch(r) or 0) > last_restart for r in done)

    findings = []
    verdict = "OK"
    if not done:
        if quiet_ratio > 0.6:
            verdict = "WARN"
            findings.append("窗口内几乎全为静默窗(%.0f%%),无产出属正常" % (quiet_ratio * 100))
        else:
            verdict = "ALERT"
            findings.append("活动窗内零浏览产出")
    if daemon_alive is False:
        verdict = "ALERT"
        findings.append("守护进程不在跑")
    if daemon_alive is None:
        findings.append("守护进程状态未探测(沙箱)")
    if r_fail:
        verdict = "ALERT"
        findings.append("重启失败 %d 次" % len(r_fail))
    elif restarts:
        if verdict == "OK":
            verdict = "WARN"
        findings.append("经历 %d 次自愈重启%s" % (
            len(restarts), "(重启后已恢复产出)" if recovered_after_restart else ""))
    if len(risk_events) >= 3:
        verdict = "ALERT"
        findings.append("窗口内风控信号 %d 次" % len(risk_events))
    elif risk_events:
        if verdict == "OK":
            verdict = "WARN"
        findings.append("风控留痕 %d 次(%s)" % (
            len(risk_events), risk_events[-1].get("kind")))
    if bridge_down:
        if verdict == "OK":
            verdict = "WARN"
        findings.append("桥曾不可达 %d 次(已尝试拉起)" % len(bridge_down))
    if bridge_alive is False:
        if verdict == "OK":
            verdict = "WARN"
        findings.append("当前桥端口 18765 未监听")
    if done and len(done) < 5 and verdict == "OK":
        verdict = "WARN"
        findings.append("窗口内产出偏少(%d 帖)" % len(done))

    level = st.get("level") or {}
    daily = st.get("daily") or {}
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"since": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)),
                   "until": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                   "hours": window_hours},
        "verdict": verdict,
        "findings": findings,
        "browse": {"done": len(done), "loiter": len(loiter), "fail": len(fails),
                   "last": (done[-1].get("ts") if done else None)},
        "watchd": {"restarts": len(restarts), "restart_failed": len(r_fail),
                   "bridge_down": len(bridge_down), "notify_fail": len(notify_fail),
                   "last_restart": (restarts[-1].get("ts") if restarts else None),
                   "last_restart_reason": (restarts[-1].get("reason") if restarts else None)},
        "risk": {"events": len(risk_events),
                 "last_kind": (risk_events[-1].get("kind") if risk_events else None)},
        "ensure_tab": {"n": len(ensures),
                       "last_ok": (ensures[-1].get("ok") if ensures else None),
                       "last_note": (ensures[-1].get("note") if ensures else None)},
        "level": {"ts": level.get("ts"), "met": level.get("met"), "pending": level.get("pending"),
                  "checks_in_window": len(levels)},
        "daily": daily,
        "alive": {"daemon": daemon_alive, "bridge": bridge_alive, "heartbeat_age_s": hb_age},
        "quiet_ratio": round(quiet_ratio, 3),
    }


def floor_line():
    """楼层口径进度一（R578）：复用 temp/linuxdo_floor_ledger.py 的只读口径，失败则返回 None。

    复用而非重写——「楼层/话题」口径与门槛(20000/500)只有一处定义，避免两处漂移。
    """
    try:
        import importlib.util
        p = os.path.join(ROOT, "temp", "linuxdo_floor_ledger.py")
        spec = importlib.util.spec_from_file_location("_ldo_floor_ledger", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _br = base_root()
        if _br != ROOT:                     # 沙箱：只读源改指沙箱内同名相对路径
            m.DAEMON_DIR = os.path.join(_br, "temp", "linuxdo_browse_daemon")
            m.LEVEL_LOG = os.path.join(m.DAEMON_DIR, "level_log.jsonl")
            m.LEDGER = os.path.join(_br, "temp", "linuxdo_floor_ledger.jsonl")
        rows = m.all_rows()
        if not rows:
            return None
        last = rows[-1]
        if last.get("posts") is None:
            return None
        tab = m.daily_table(rows)
        d_p = (tab[-1] if tab else {}).get("d_posts")
        return "· 楼层:%d/%d(%.0f%%) 缺口 %d,今日 +%s" % (
            last["posts"], m.GOAL_POSTS, 100.0 * last["posts"] / m.GOAL_POSTS,
            max(0, m.GOAL_POSTS - last["posts"]), "-" if d_p is None else d_p)
    except Exception:
        return None


def render(d):
    L = []
    L.append("%s L站浏览栈夜间稳定性 %s" % (ICON.get(d["verdict"], ""), d["generated"]))
    L.append("窗口 %s → %s(%.0fh)" % (d["window"]["since"][5:16], d["window"]["until"][5:16],
                                     d["window"]["hours"]))
    b = d["browse"]
    L.append("· 浏览产出:%d 帖(LOITER %d,抓取失败 %d)%s" % (
        b["done"], b["loiter"], b["fail"],
        ",末条 " + b["last"][11:16] if b.get("last") else ""))
    w = d["watchd"]
    L.append("· 自愈:重启 %d(失败 %d),桥不可达 %d%s" % (
        w["restarts"], w["restart_failed"], w["bridge_down"],
        ",末次 %s %s" % (w["last_restart"][11:16], w["last_restart_reason"]) if w.get("last_restart") else ""))
    a = d["alive"]
    L.append("· 存活:守护 %s,桥 %s%s" % (
        {True: "在跑", False: "未跑", None: "未知"}[a["daemon"]],
        {True: "可达", False: "不可达", None: "未知"}[a["bridge"]],
        "(心跳 %.0fs 前)" % a["heartbeat_age_s"] if a.get("heartbeat_age_s") else ""))
    r = d["risk"]
    L.append("· 风控:%d 次%s" % (r["events"], " 末次 " + str(r["last_kind"]) if r["last_kind"] else ""))
    e = d["ensure_tab"]
    if e["n"]:
        L.append("· 标签自愈:%d 次,末次 ok=%s(%s)" % (e["n"], e["last_ok"], (e["last_note"] or "")[:40]))
    lv = d["level"]
    L.append("· 等级:末查 %s met=%s pending=%s" % (lv.get("ts"), lv.get("met"), lv.get("pending")))
    _fl = floor_line()          # ★R578 楼层口径（TL3 门槛真正卡的是它）
    if _fl:
        L.append(_fl)
    L.append("· 结论:%s" % (";".join(d["findings"]) or "窗口内一切正常"))
    return "\n".join(L)


def ledger_path():
    return pth(LEDGER_REL)


def already_sent(today):
    """只有**真推送过**(sent=True)的那条才算今日已汇报。

    dry-run(--no-notify)会在台账留 sent=False 行,它不构成幂等键;
    否则一次预览就把当天真推送永久短路(实测踩到)。
    """
    for row in read_jsonl(LEDGER_REL):
        if row.get("day") == today and row.get("sent"):
            return row
    return None


def write_ledger(day, verdict, sent, note=""):
    p = ledger_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"day": day, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "verdict": verdict, "sent": sent, "note": note},
                           ensure_ascii=False) + "\n")


def notify(text):
    """经 reflect_common 发飞书私信;失败返回 False(fail-loud 打印)。"""
    if os.environ.get("LJQ_NIGHTLY_ROOT"):
        return False
    try:
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from reflect_common import notify_feishu  # noqa
        notify_feishu(text)
        return True
    except Exception as e:
        print("[夜间汇报推送失败] %s" % e)
    return False
def cmd_run(args):
    now = time.time()
    d = diagnose(now=now, window_hours=args.window_hours)
    text = render(d)
    print(text)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    today = time.strftime("%Y-%m-%d")
    prev = already_sent(today)
    if prev and not args.force:
        print("[幂等] 今日已汇报(%s %s),跳过推送" % (prev.get("ts"), prev.get("verdict")))
        return 0
    sent = False
    if args.no_notify:
        print("[dry-run] 未推送、未写台账(请去掉 --no-notify 以真发)")
        return 0
    sent = notify(text)
    write_ledger(today, d["verdict"], sent, note="force" if args.force else "")
    if not sent:
        print("[夜间汇报推送失败] 已记 sent=False,下次仍会重试")
        return 1
    return 0


def selftest():
    import tempfile
    import shutil
    ok = fail = 0
    td = tempfile.mkdtemp(prefix="ljq_nightly_st_")
    os.environ["LJQ_NIGHTLY_ROOT"] = td

    def mk(rel, rows):
        p = pth(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS", name)
        else:
            fail += 1
            print("  FAIL", name)

    now = time.mktime(time.strptime("2026-10-11 08:30:00", "%Y-%m-%d %H:%M:%S"))

    # T1 正常:有产出、零重启
    mk(BROWSE_REL, [{"ts": "2026-10-11 04:10:00", "st": "DONE", "id": 1}] * 8)
    mk(WATCHD_REL, [{"ts": "2026-10-11 05:00:00", "kind": "ROUND", "action": "OK"}])
    # STATE_REL 是单 JSON 对象(非 JSONL)
    with open(pth(STATE_REL), "w", encoding="utf-8") as f:
        json.dump({"daily": {"2026-10-11": 8},
                   "level": {"ts": "2026-10-11 09:48:53", "met": 11, "pending": 1}}, f)
    d = diagnose(now=now, window_hours=16.0)
    chk("T1 verdict OK", d["verdict"] == "OK")
    chk("T1 done=8", d["browse"]["done"] == 8)

    # T2 全静默窗无产出 ⇒ WARN 非 ALERT
    mk(BROWSE_REL, [])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 05:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=6.0)
    chk("T2 quiet WARN", d["verdict"] == "WARN" and d["quiet_ratio"] > 0.6)

    # T3 活动窗零产出 ⇒ ALERT
    mk(BROWSE_REL, [])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 20:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=8.0)
    chk("T3 no-yield ALERT", d["verdict"] == "ALERT")

    # T4 重启失败 ⇒ ALERT
    mk(BROWSE_REL, [{"ts": "2026-10-11 19:00:00", "st": "DONE", "id": 2}] * 6)
    mk(WATCHD_REL, [{"ts": "2026-10-11 16:00:00", "kind": "RESTART", "ok": False, "reason": "DAEMON_DEAD"}])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 20:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=8.0)
    chk("T4 restart-failed ALERT", d["verdict"] == "ALERT")

    # T5 风控>=3 ⇒ ALERT;1 次 ⇒ WARN(须重置 T4 重启夹具,否则归因混淆)
    mk(WATCHD_REL, [{"ts": "2026-10-11 05:00:00", "kind": "ROUND", "action": "OK"}])
    mk(RISK_REL, [{"ts": "2026-10-11 19:0%d:00" % i, "st": "RISK", "kind": "RATE_LIMIT"} for i in range(3)])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 20:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=8.0)
    chk("T5 risk>=3 ALERT", d["verdict"] == "ALERT" and any("风控" in f for f in d["findings"]))
    chk("T5 risk count", d["risk"]["events"] == 3)
    mk(RISK_REL, [{"ts": "2026-10-11 19:00:00", "st": "RISK", "kind": "FORBIDDEN"}])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 20:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=8.0)
    chk("T5b risk=1 WARN", d["verdict"] == "WARN" and d["risk"]["last_kind"] == "FORBIDDEN")

    # T5c st=INFO(抓取失败等噪音)不计入风控
    mk(RISK_REL, [{"ts": "2026-10-11 19:00:00", "st": "INFO", "kind": "FETCH_FAIL"},
                  {"ts": "2026-10-11 19:05:00", "st": "INFO", "kind": "FETCH_FAIL"}])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 20:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=8.0)
    chk("T5c INFO not risk", d["risk"]["events"] == 0 and d["verdict"] == "OK")

    # T6 重启但恢复产出 ⇒ WARN 且 recovered 逻辑不误报 ALERT
    mk(RISK_REL, [])
    mk(WATCHD_REL, [{"ts": "2026-10-11 16:00:00", "kind": "RESTART", "ok": True, "reason": "STALLED_10800s"}])
    d = diagnose(now=time.mktime(time.strptime("2026-10-11 20:00:00", "%Y-%m-%d %H:%M:%S")),
                 window_hours=8.0)
    chk("T6 restart-ok WARN", d["verdict"] == "WARN")

    # T7 幂等 ledger
    write_ledger("2026-10-11", "OK", True)
    chk("T7 ledger read", (already_sent("2026-10-11") or {}).get("verdict") == "OK")

    # T8 render 不崩 + 含关键词
    t = render(d)
    chk("T8 render", "L站浏览栈" in t and "结论:" in t)

    # T9 沙箱下不真发通知
    chk("T9 notify blocked in sandbox", notify("x") is False)

    # T11 楼层口径行(R578):沙箱内追加 connect 读数 ⇒ floor_line 出 20000 口径、render 含该行
    _lv = pth(LEVEL_REL)
    os.makedirs(os.path.dirname(_lv), exist_ok=True)
    with open(_lv, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-10-11 19:30:00", "st": "OK", "source": "connect",
                            "met": 11, "pending": 1,
                            "items": [{"met": True, "name": "16202/ 20000"},
                                      {"met": True, "name": "1182/ 500"}]},
                           ensure_ascii=False) + "\n")
    _fl = floor_line()
    chk("T11 floor_line 20000 口径", bool(_fl) and "20000" in _fl and "16202" in _fl)
    chk("T11b render 含楼层行", "楼层:" in render(d))

    # T10 空目录(源全缺)不崩且不判 OK
    td2 = tempfile.mkdtemp(prefix="ljq_nightly_st2_")
    os.environ["LJQ_NIGHTLY_ROOT"] = td2
    d = diagnose(now=now, window_hours=8.0)
    chk("T10 empty -> non-OK", d["verdict"] in ("ALERT", "WARN"))
    shutil.rmtree(td2, ignore_errors=True)

    os.environ.pop("LJQ_NIGHTLY_ROOT", None)
    shutil.rmtree(td, ignore_errors=True)
    print("selftest %d/%d" % (ok, ok + fail))
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="L站浏览栈夜间稳定性汇报器(只读)")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run"])
    ap.add_argument("--window-hours", type=float, default=16.0)
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())