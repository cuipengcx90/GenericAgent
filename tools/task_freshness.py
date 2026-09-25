#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_freshness —— 定时任务/外部通道「产物级」新鲜度监控（只读）

来源：TODO#9（第12批三路评审 9.33）+ memory/scheduled_task_freshness_audit_sop.md
背景：birdiesearch 自 2026-08-07 连续 49 天静默失败（任务照跑、报告照写、产物不动），
      直到人工翻日志才发现；haisou 2026-09-25 首例「抓取到 0 个课程仍 SYNC_RC=0」=
      **日志有降级、产物还是新鲜的**，纯 mtime 判据看不出。

判据（三层，逐条目标各出一行）：
  L1 产物新鲜度：payload glob 的最新 mtime 距今天数 > max_silence_days ⇒ STALE；无文件 ⇒ MISSING
  L2 静默降级签名：扫最新日志/证据文件尾部（默认 200KB）命中 degraded_patterns ⇒ DEGRADED
  L3 运行缺席：最近一次运行证据（done 报告/日志）距今 > max_run_gap_days ⇒ NO_RUN
附加：launchd 目标额外查 launchctl list（未加载 ⇒ NOT_LOADED；上次退出码非 0 ⇒ 记 note）。

零副作用：只 stat / 读尾部；不 touch、不删、不改任何被检测文件（唯一写入是 --ledger 台账）。
退出码：0 = 无硬问题；1 = 存在硬问题；2 = 用法/配置错误。

用法：
  python3 tools/task_freshness.py                 # 文本表
  python3 tools/task_freshness.py --json          # JSON（schema ga.task_freshness/v1）
  python3 tools/task_freshness.py --no-ledger     # 不写台账（默认写）
  python3 tools/task_freshness.py --diff          # 与上一条台账比对（新增/消除的问题）
  python3 tools/task_freshness.py --only birdie_daily_monitor,haisou_daily_monitor
  python3 tools/task_freshness.py --selftest      # 合成夹具回归（含负控）
"""
import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GA = os.path.dirname(HERE)
DEFAULT_CRITERIA = os.path.join(HERE, "freshness_criteria.json")
LEDGER_DEFAULT = os.path.join(GA, "temp", "task_freshness", "ledger.jsonl")

# 判定严重度顺序（大 = 更严重）；同一行取最严重
LEVEL_ORDER = {"OK": 0, "INFO": 1, "WARN": 2, "ALERT": 3}


def now_ts():
    return time.time()


def fmt_age(days):
    if days is None:
        return "-"
    if days < 1:
        return "%.2fh" % (days * 24)
    return "%.2fd" % days


def fmt_ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def age_days(t, ref=None):
    return ((ref or now_ts()) - t) / 86400.0


def newest_match(patterns, root):
    """返回 (path, mtime, matched_count) —— 支持绝对 glob。"""
    best, cnt = None, 0
    for pat in patterns or []:
        p = pat if os.path.isabs(pat) else os.path.join(root, pat)
        for f in glob.glob(p, recursive=True):
            if not os.path.isfile(f):
                continue
            cnt += 1
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            if best is None or mt > best[1]:
                best = (f, mt)
    return (best[0], best[1], cnt) if best else (None, None, 0)


def all_matches(patterns, root):
    out = []
    for pat in patterns or []:
        p = pat if os.path.isabs(pat) else os.path.join(root, pat)
        for f in glob.glob(p, recursive=True):
            if os.path.isfile(f):
                out.append(f)
    return sorted(set(out))


def tail_text(path, max_bytes=200_000):
    """只读文件尾部（大日志安全）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def scan_signatures(paths, patterns, root, cap=6):
    """在给定文件里找降级签名，返回命中列表 [{file, pattern, line}]。"""
    hits = []
    for p in paths:
        text = tail_text(p)
        if not text:
            continue
        for pat in patterns or []:
            idx = text.find(pat)
            if idx < 0:
                continue
            # 取该行原文（截断）
            start = text.rfind("\n", 0, idx) + 1
            end = text.find("\n", idx)
            line = text[start:end if end > 0 else len(text)].strip()
            hits.append({"file": os.path.relpath(p, root) if p.startswith(root) else p,
                         "pattern": pat, "line": line[:180]})
            if len(hits) >= cap:
                return hits
    return hits


def launchd_state():
    """{label: {"pid":..,"exit":..}} —— 只跑一次 launchctl list。"""
    st = {}
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return st
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        pid, exit_code, label = parts[0], parts[1], parts[2]
        if not label or label == "Label":      # 跳过表头 PID/Status/Label
            continue
        try:
            st[label] = {"pid": None if pid == "-" else int(pid),
                         "exit": None if exit_code == "-" else int(exit_code)}
        except ValueError:
            continue
    return st


def plist_meta(label):
    """从 ~/Library/LaunchAgents/<label>.plist 读关注字段（只读）。"""
    import plistlib
    p = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
    if not os.path.exists(p):
        return None
    try:
        d = plistlib.load(open(p, "rb"))
    except Exception:
        return {"exists": True, "error": "plist 解析失败"}
    return {"exists": True, "keeplive": bool(d.get("KeepAlive")),
            "stdout": d.get("StandardOutPath"), "watch": d.get("WatchPaths") or [],
            "interval": d.get("StartInterval"),
            "args": " ".join(map(str, (d.get("ProgramArguments") or [d.get("Program", "")])))}


def check_target(t, root, ref=None, ld=None):
    """对一个判据目标求判定。返回 dict。"""
    ref = ref or now_ts()
    res = {"id": t["id"], "kind": t.get("kind", "sche"), "label": t.get("label", t["id"]),
           "cadence": t.get("cadence", ""), "notes": t.get("note", ""),
           "max_run_gap_days": t.get("max_run_gap_days"),
           "checks": [], "verdict": "OK", "severity": t.get("severity", "hard")}

    def bump(level, text):
        res["checks"].append({"level": level, "text": text})
        if LEVEL_ORDER[level] > LEVEL_ORDER[res["verdict"]]:
            res["verdict"] = level

    if t.get("disabled"):
        res["verdict"] = "DISABLED"
        res["checks"].append({"level": "INFO", "text": "按设计停用/归档（enabled=false），不参与新鲜度判定"})
        return res

    # ---- L3 运行缺席 ----
    ev_pats = t.get("run_evidence") or []
    evp, evmt, evn = newest_match(ev_pats, root)
    if evp is None:
        if ev_pats:
            bump("INFO", "无运行证据文件（glob 未命中：%s）" % ", ".join(ev_pats[:2]))
    else:
        a = age_days(evmt, ref)
        res["evidence"] = {"file": os.path.relpath(evp, root) if evp.startswith(root) else evp,
                           "mtime": fmt_ts(evmt), "age_days": round(a, 2), "matched": evn}
        gap = t.get("max_run_gap_days")
        if gap and a > gap:
            bump("ALERT", "L3 运行缺席：最近运行证据 %s（%s）距今 %s > 允许 %sd"
                 % (res["evidence"]["file"], res["evidence"]["mtime"], fmt_age(a), gap))
        else:
            res["checks"].append({"level": "OK", "text": "L3 运行证据新鲜（%s, %s）" % (fmt_age(a), res["evidence"]["file"])})

    # ---- L1 产物新鲜度 ----
    res["payloads"] = []
    for pl in t.get("payloads") or []:
        pth, mt, cnt = newest_match([pl["glob"]], root)
        row = {"glob": pl["glob"], "required": pl.get("required", True),
               "max_silence_days": pl.get("max_silence_days"), "matched": cnt,
               "soft": pl.get("soft", False), "note": pl.get("note", "")}
        if pth is None:
            row.update({"status": "MISSING", "age_days": None})
            lvl = "INFO" if (row["soft"] or not row["required"]) else "ALERT"
            bump(lvl, "L1 产物缺失：%s%s" % (pl["glob"], "（软判据）" if lvl == "INFO" else ""))
        else:
            a = age_days(mt, ref)
            size = os.path.getsize(pth)
            row.update({"file": os.path.relpath(pth, root) if pth.startswith(root) else pth,
                        "mtime": fmt_ts(mt), "age_days": round(a, 2), "bytes": size})
            ms = pl.get("max_silence_days")
            mb = pl.get("min_bytes")
            if ms and a > ms:
                row["status"] = "STALE"
                lvl = "INFO" if row["soft"] else "ALERT"
                bump(lvl, "L1 产物过期：%s（%s）距今 %s > 允许 %sd%s"
                     % (row["file"], row["mtime"], fmt_age(a), ms, "（软判据）" if lvl == "INFO" else ""))
            elif mb and size < mb:
                row["status"] = "HOLLOW"
                lvl = "INFO" if row["soft"] else "WARN"
                bump(lvl, "L1 产物体积异常（疑似空壳）：%s 仅 %dB < 期望 %dB%s"
                     % (row["file"], size, mb, "（软判据）" if lvl == "INFO" else ""))
            else:
                row["status"] = "FRESH"
                res["checks"].append({"level": "OK", "text": "L1 产物新鲜：%s（%s, %dB）"
                                      % (row["file"], fmt_age(a), size)})
        res["payloads"].append(row)

    # ---- L2 静默降级签名 ----
    sig_hits = []
    for sc in t.get("scans") or []:
        files = all_matches(sc["globs"], root)
        fresh = [f for f in files if age_days(os.path.getmtime(f), ref) <= (sc.get("window_days") or 3)]
        if not fresh:
            res["checks"].append({"level": "INFO", "text": "L2 无窗口内日志可扫（%s）" % sc["globs"][0]})
            continue
        target = max(fresh, key=os.path.getmtime)
        hits = scan_signatures([target], sc.get("patterns") or [], root)
        for h in hits:
            h["source"] = "log"
            h["scan_soft"] = bool(sc.get("soft"))   # 扫描级软判据随命中一起带出（勿用路径回查：h["file"] 是相对路径）
            sig_hits.append(h)
    if sig_hits:
        for h in sig_hits[:4]:
            # 扫描级软判据（sc.soft）：命中只作可见性提示，不计入硬问题（如「按设计跳过」类前置缺失）
            lvl = "INFO" if h.get("scan_soft") else "ALERT"
            h["soft"] = (lvl == "INFO")
            bump(lvl, "L2 降级签名命中%s %s: %s「%s」" % ("（软判据）" if lvl == "INFO" else "",
                                                        h["file"], h["pattern"], h["line"][:120]))
    res["signatures"] = sig_hits

    # ---- launchd 专属 ----
    if res["kind"] == "launchd":
        label = t.get("launchd_label")
        meta = plist_meta(label) if label else None
        st = (ld or {}).get(label) if label else None
        res["launchd"] = {"label": label, "plist": meta, "launchctl": st}
        if label:
            if meta is None:
                bump("ALERT", "launchd plist 不存在：~/Library/LaunchAgents/%s.plist" % label)
            elif st is None:
                bump("ALERT", "launchd 未加载：plist 存在但 launchctl list 无 %s（僵尸任务）" % label)
            else:
                info = "launchctl: pid=%s last_exit=%s" % (st["pid"] or "-", st["exit"])
                if st["exit"] not in (0, None):
                    bump("WARN", "launchd 上次退出码非 0（%s）: %s" % (st["exit"], info))
                else:
                    res["checks"].append({"level": "OK", "text": info})
    return res


def check_channel(c, root, ref=None):
    """外部通道：最近成功时刻 + 失败模式（与任务表同屏，C7 并入）。"""
    ref = ref or now_ts()
    pth, mt, cnt = newest_match(c.get("success_artifact") or [], root)
    out = {"id": c["id"], "via": c.get("via", ""), "note": c.get("note", ""),
           "last_success": None, "success_age_days": None, "failure_mode": None,
           "hits": []}
    if pth:
        a = age_days(mt, ref)
        out.update({"last_success": {"file": os.path.relpath(pth, root) if pth.startswith(root) else pth,
                                     "mtime": fmt_ts(mt)}, "success_age_days": round(a, 2)})
    # 失败模式：在最近的证据文件（日志或最近报告）里找签名
    files = all_matches(c.get("scan_globs") or [], root)
    newest = max(files, key=os.path.getmtime) if files else None
    if newest:
        hits = scan_signatures([newest], c.get("failure_patterns") or [], root)
        for h in hits:
            h["source"] = os.path.basename(newest)
        out["hits"] = hits
        if hits:
            out["failure_mode"] = "%s（最新证据 %s）" % (hits[0]["pattern"], fmt_ts(os.path.getmtime(newest)))
    return out


def run(criteria, root, only=None, as_json=False, ledger=LEDGER_DEFAULT, write_ledger=True,
        do_diff=False, ref=None, return_result=False):
    ref = ref or now_ts()
    ld = launchd_state()
    targets = criteria["targets"]
    if only:
        want = set(only)
        targets = [t for t in targets if t["id"] in want]
    rows = [check_target(t, root, ref, ld) for t in targets]
    channels = [check_channel(c, root, ref) for c in criteria.get("channels", [])]

    hard = [r for r in rows if r["severity"] != "soft" and r["verdict"] in ("ALERT", "WARN")]
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    result = {"schema": "ga.task_freshness/v1", "ts": fmt_ts(ref), "root": root,
              "criteria_source": criteria.get("_source", ""), "counts": counts,
              "hard_issue_ids": [r["id"] for r in hard], "targets": rows, "channels": channels,
              "corpus": {"targets": len(rows), "channels": len(channels),
                         "payload_files_seen": sum(p.get("matched", 0) for r in rows for p in r.get("payloads", [])),
                         "log_files_scanned": len({h["file"] for r in rows for h in r.get("signatures", [])})}}

    prev = None
    if write_ledger and ledger:
        prev = _ledger_prev(ledger)
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": result["ts"], "counts": counts,
                                "hard_issue_ids": result["hard_issue_ids"],
                                "issues": [{"id": r["id"], "verdict": r["verdict"],
                                            "first": (r["checks"][0]["text"][:160] if r["checks"] else "")}
                                           for r in rows if r["verdict"] in ("ALERT", "WARN")],
                                "channels": [{"id": c["id"], "last_success": (c["last_success"] or {}).get("mtime"),
                                              "age_days": c["success_age_days"], "failure_mode": c["failure_mode"]}
                                             for c in channels]}, ensure_ascii=False) + "\n")

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print_table(result, prev if do_diff else None)
    if return_result:
        return result
    return 1 if result["hard_issue_ids"] else 0


def _ledger_prev(ledger):
    if not os.path.exists(ledger):
        return None
    last = None
    with open(ledger, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                last = ln
    try:
        return json.loads(last) if last else None
    except Exception:
        return None


def print_table(result, prev=None):
    print("== 任务/通道「产物级」新鲜度 = %s（判据表 %s）" % (result["ts"], result["criteria_source"]))
    print("   目标 %d + 外部通道 %d；产物文件命中 %d；日志扫描 %d"
          % (result["corpus"]["targets"], result["corpus"]["channels"],
             result["corpus"]["payload_files_seen"], result["corpus"]["log_files_scanned"]))
    print("-" * 118)
    print("%-28s %-8s %-9s %-10s %-30s %s" % ("目标", "类型", "节奏", "运行证据", "产物(最新/max)", "判定"))
    print("-" * 118)
    for r in result["targets"]:
        ev = r.get("evidence")
        ev_s = "%s / %sd" % (fmt_age(ev["age_days"]), r.get("max_run_gap_days") or "-") if ev else "-"
        pls = []
        for p in r.get("payloads", []):
            pls.append("%s=%s/%s%s" % (os.path.basename(p["glob"]), fmt_age(p.get("age_days")),
                                       p.get("max_silence_days") or "-", "" if not p.get("soft") else "*"))
        pl_s = "; ".join(pls) or "-"
        if len(pl_s) > 30:
            pl_s = pl_s[:29] + "…"
        print("%-28s %-8s %-9s %-10s %-30s %s" % (r["id"][:28], r["kind"], r.get("cadence", "")[:9],
                                                  ev_s, pl_s, r["verdict"]))
    print("-" * 118)
    issues = [r for r in result["targets"] if r["verdict"] in ("ALERT", "WARN", "INFO", "DISABLED")]
    if issues:
        print("问题明细：")
        for r in issues:
            for c in r["checks"]:
                if c["level"] in ("ALERT", "WARN", "INFO") and r["verdict"] != "DISABLED":
                    print("  [%s%s] %-24s %s" % (c["level"], "*" if r["severity"] == "soft" else "",
                                                 r["id"], c["text"]))
                elif r["verdict"] == "DISABLED":
                    print("  [DISABLED] %-23s %s" % (r["id"], c["text"]))
                    break
    print("-" * 118)
    print("外部通道（最近成功时刻 + 失败模式）：")
    for c in result["channels"]:
        ls = c["last_success"]["mtime"] if c["last_success"] else "无成功产物记录"
        print("  %-22s 最近成功=%-16s (%s)  失败模式=%s"
              % (c["id"], ls, fmt_age(c["success_age_days"]), c["failure_mode"] or "无签名"))
        for h in c["hits"][:1]:
            print("        命中：%s「%s」" % (h["file"], h["line"][:100]))
    print("-" * 118)
    softn = len([r for r in result["targets"]
                 if r["severity"] == "soft" and r["verdict"] in ("ALERT", "WARN")])
    print("汇总(标*为软判据)：%s；软 ALERT/WARN %d 条；硬问题 %d 个%s" % (", ".join("%s=%d" % kv for kv in sorted(result["counts"].items())), softn,
                                     len(result["hard_issue_ids"]),
                                     ("：" + ", ".join(result["hard_issue_ids"])) if result["hard_issue_ids"] else ""))
    if prev is not None:
        newi = sorted(set(result["hard_issue_ids"]) - set(prev.get("hard_issue_ids", [])))
        clri = sorted(set(prev.get("hard_issue_ids", [])) - set(result["hard_issue_ids"]))
        print("与上一条台账(%s)比对：新增硬问题 %s；已消除 %s" % (prev.get("ts"),
              newi or "无", clri or "无"))


# ----------------------------------------------------------------------------- selftest
def selftest(tmp=None):
    """合成夹具回归 + 负控：判据必须随输入变号，健康目标不得报警。

    夹具全部建在 temp/task_freshness_selftest/ 内（自证语料计数）。
    """
    base = tmp or os.path.join(GA, "temp", "task_freshness_selftest")
    if os.path.exists(base):
        shutil.rmtree(base)
    os.makedirs(base)
    now = now_ts()
    def touch(rel, age_days, body=""):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(body)
        ts = now - age_days * 86400
        os.utime(p, (ts, ts))
        return p

    # fixture A：birdiesearch 49 天静默（产物 49d + 今日日志带 SSLError + 今日 done 报告）
    touch("birdie_monitor/birdie_seen_items.json", 49.35, "{}")
    touch("temp/bg_task/logs/birdie_0925.log", 0.35, "requests.exceptions.SSLError: HTTPSConnectionPool(host='www.birdiesearch.com')\n\nexit: 1\n")
    touch("sche_tasks/done/2026-09-25_1100_birdie_daily_monitor.md", 0.35, "# done\n")
    # fixture B：haisou 静默降级（产物新鲜，但日志「抓取到 0 个课程」）
    touch("birdie_monitor/haisou_seen_items.json", 0.34, "{}")
    touch("temp/haisou_20260925_1115.log", 0.34, "🔍 抓取全能IT首页最新课程...\n⚠️ 抓取失败: SSLEOFError\n✓ 抓取到 0 个课程\nSYNC_RC=0\n")
    touch("sche_tasks/done/2026-09-25_1115_haisou_daily_monitor.md", 0.34, "# done\n")
    # fixture C（负控1）：健康任务 —— 产物新鲜、日志无签名、运行证据新鲜
    touch("output/tech_trends_2026-09-25.md", 0.36, "# ok\n")
    touch("temp/bg_task/logs/trr_ok.log", 0.36, "done, 12 items\n")
    touch("sche_tasks/done/2026-09-25_1040_github_trending.md", 0.36, "# done\n")

    criteria = {"_source": "selftest-fixture", "targets": [
        {"id": "birdie_daily_monitor", "kind": "sche", "cadence": "daily", "max_run_gap_days": 2,
         "run_evidence": ["sche_tasks/done/*_birdie_daily_monitor.md"],
         "payloads": [{"glob": "birdie_monitor/birdie_seen_items.json", "max_silence_days": 3}],
         "scans": [{"globs": ["temp/bg_task/logs/birdie_*.log"], "window_days": 3,
                    "patterns": ["SSLError", "exit: 1"]}]},
        {"id": "haisou_daily_monitor", "kind": "sche", "cadence": "daily", "max_run_gap_days": 2,
         "run_evidence": ["sche_tasks/done/*_haisou_daily_monitor.md"],
         "payloads": [{"glob": "birdie_monitor/haisou_seen_items.json", "max_silence_days": 3}],
         "scans": [{"globs": ["temp/haisou_*.log"], "window_days": 3,
                    "patterns": ["抓取失败", "抓取到 0 个课程"]}]},
        {"id": "github_trending", "kind": "sche", "cadence": "daily", "max_run_gap_days": 2,
         "run_evidence": ["sche_tasks/done/*_github_trending.md"],
         "payloads": [{"glob": "output/tech_trends_*.md", "max_silence_days": 2}],
         "scans": [{"globs": ["temp/bg_task/logs/trr_*.log"], "window_days": 3,
                    "patterns": ["SSLError", "Traceback"]}]},
    ], "channels": [
        {"id": "www.birdiesearch.com", "via": "birdie_daily_monitor",
         "success_artifact": ["birdie_monitor/birdie_seen_items.json"],
         "scan_globs": ["temp/bg_task/logs/birdie_*.log"], "failure_patterns": ["SSLError"]},
    ]}

    checks = []
    def expect(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    res = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(criteria, base, as_json=False, write_ledger=False)
    rows = {r["id"]: r for r in res["targets"]}
    print("== selftest：合成夹具（根=%s）" % os.path.relpath(base, GA))
    expect("① 49 天静默 → birdie STALE 且判为硬问题", rows["birdie_daily_monitor"]["verdict"] == "ALERT"
           and any(p["status"] == "STALE" for p in rows["birdie_daily_monitor"]["payloads"]),
           rows["birdie_daily_monitor"]["verdict"])
    expect("② haisou 日志降级签名 → DEGRADED(ALERT) 而产物仍新鲜",
           rows["haisou_daily_monitor"]["verdict"] == "ALERT"
           and rows["haisou_daily_monitor"]["payloads"][0]["status"] == "FRESH"
           and len(rows["haisou_daily_monitor"]["signatures"]) >= 1,
           "%s / payload=%s" % (rows["haisou_daily_monitor"]["verdict"],
                                rows["haisou_daily_monitor"]["payloads"][0]["status"]))
    expect("③ 负控：健康任务不得报警", rows["github_trending"]["verdict"] == "OK",
           rows["github_trending"]["verdict"])
    expect("④ 通道表带出最近成功时刻 + 失败模式",
           res["channels"][0]["last_success"] is not None and res["channels"][0]["failure_mode"],
           str(res["channels"][0]["failure_mode"]))
    # 负控2：把 birdie 产物改成新鲜 ⇒ 必须变号（证明判定随输入变，不是硬编码结论）
    touch("birdie_monitor/birdie_seen_items.json", 0.1, "{}")
    res2 = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    rows2 = {r["id"]: r for r in res2["targets"]}
    expect("⑤ 负控：产物改新鲜 → birdie 不再因 L1 报警（仅剩日志签名维度）",
           all(p["status"] == "FRESH" for p in rows2["birdie_daily_monitor"]["payloads"]),
           rows2["birdie_daily_monitor"]["payloads"][0]["status"])
    # 负控3：清空日志签名 ⇒ haisou 必须转 OK
    touch("temp/haisou_20260925_1115.log", 0.2, "全部正常\n")
    res3 = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    rows3 = {r["id"]: r for r in res3["targets"]}
    expect("⑥ 负控：日志无签名 → haisou 转 OK", rows3["haisou_daily_monitor"]["verdict"] == "OK",
           rows3["haisou_daily_monitor"]["verdict"])
    # 语料自证：确认夹具被真读到
    # 夹具固定语料：3 个 payload 文件（birdie/haisou seen_items + tech_trends 产物）
    # + 2 个带签名的日志（birdie_0925.log / haisou_20260925_1115.log）；精确计数=证明真读到语料
    expect("⑦ 语料自证：payload=3 且日志=2（精确计数）",
           res["corpus"]["payload_files_seen"] == 3 and res["corpus"]["log_files_scanned"] == 2,
           str(res["corpus"]))

    ok = True
    for name, good, detail in checks:
        ok &= good
        print("   [%s] %s%s" % ("PASS" if good else "FAIL", name, ("  → %s" % detail) if not good else ""))
    print("负控/回归合计：%s（%d/%d）" % ("PASS" if ok else "FAIL", sum(1 for _, g, _ in checks if g), len(checks)))
    print("夹具根：%s（可整目录删除，无副作用）" % os.path.relpath(base, GA))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=GA)
    ap.add_argument("--criteria", default=DEFAULT_CRITERIA)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ledger", default=LEDGER_DEFAULT)
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-root", default="")
    a = ap.parse_args()
    if a.selftest:
        return selftest(a.selftest_root or None)
    if not os.path.exists(a.criteria):
        print("判据表不存在：%s" % a.criteria, file=sys.stderr)
        return 2
    criteria = json.load(open(a.criteria, encoding="utf-8"))
    criteria["_source"] = os.path.relpath(a.criteria, a.root)
    only = [s for s in a.only.split(",") if s]
    return run(criteria, a.root, only=only, as_json=a.json,
               ledger=a.ledger, write_ledger=not a.no_ledger, do_diff=a.diff)


if __name__ == "__main__":
    sys.exit(main())
