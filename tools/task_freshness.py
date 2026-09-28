#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_freshness —— 定时任务/外部通道「产物级」新鲜度监控（只读）

来源：TODO#9（第12批三路评审 9.33）+ memory/scheduled_task_freshness_audit_sop.md
背景：birdiesearch 自 2026-08-07 连续 49 天静默失败（任务照跑、报告照写、产物不动），
      直到人工翻日志才发现；haisou 2026-09-25 首例「抓取到 0 个课程仍 SYNC_RC=0」=
      **日志有降级、产物还是新鲜的**，纯 mtime 判据看不出。

判据（三层，逐条目标各出一行）：
  L1 产物新鲜度：payload glob 的最新 mtime 距今天数 > max_silence_days ⇒ STALE；无文件 ⇒ MISSING
  L1b 产出内容变化：content 段登记的家族内文件内容指纹「零变化/停滞」超阈值 ⇒ ALERT/WARN
       （补齐 L1 的 mtime 盲区「任务跑了、文件也在写，但内容没变」；判据内核复用
        tools/content_change_detector.py，只对显式登记 content 段的目标生效）
  L1c 留存断档层（R483, kind=retention）：时间衰减源（Chrome History ~90d、kc /app/usage ~25d、
       pmset 日目录 ~15d、wifi.log 每日轮转）一旦漏采，当天不补就可能永久滚出源窗口 ⇒ 按
       「日期目录 × 必需文件」审计留存面（不用 mtime）。判据内核复用 tools/rolling_source_snapshot.py
       的 verify（缺口/连续缺/空壳/永久丢失/今天待采），只对显式登记 kind=retention 的目标生效。
  L2 静默降级签名：扫最新日志/证据文件尾部（默认 200KB）命中 degraded_patterns ⇒ DEGRADED
  L3 运行缺席：最近一次运行证据（done 报告/日志）距今 > max_run_gap_days ⇒ NO_RUN
  L4 退役/人工家族（state=legacy|on_demand，R470）：任务定义已删除/归档（legacy）或仅人工触发
       （on_demand）⇒ 缺产出判 LEGACY / ON_DEMAND（静默，不计硬问题）；legacy 族在
       revive_within_days（默认 3d）窗口内**又长出新产出** = 登记已过期 ⇒ WARN（负控可证伪：
       新产物退回窗口外即回 LEGACY；改判据表 state 即整体回退）。
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

# ---- 稳定事件 ID（R480：告警面收口；内核在 tools/alert_event_id.py）----
# 语义：同一 (scope, target, 判据层) 的告警在任何一轮里都是同一个 ID
#       ⇒ 跨轮「同一件事」可去重、可计复发（缺席后再现 = 新 episode）。
# 加法式接线：只往 checks/台账补 layer 与 event_id(s)，不改任何 level/verdict/推送行为。
try:
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import alert_event_id as _aeid
except Exception:                                    # 极端环境下降级为「有检查行、无 ID」
    _aeid = None

# 判据层前缀 → canonical face（与 alert_event_id.ALIASES 同源口径）
LAYER_FACE = {"L0": "preflight", "L1": "artifact", "L1b": "content", "L1c": "artifact",
              "L2": "log", "L3": "run", "L4": "lifecycle"}


def _face_of_text(text):
    """检查行文本以 L0/L1/L1b/L2/L3/L4 打头 ⇒ 返回 canonical face；否则 None。

    注意先长后短（L1b 必须早于 L1），否则 L1b 行会被判成 artifact。
    """
    for lv in ("L1b", "L1c", "L0", "L1", "L2", "L3", "L4"):
        if text.startswith(lv + " "):
            return LAYER_FACE[lv]
    return None


# 判定严重度顺序（大 = 更严重）；同一行取最严重
LEVEL_ORDER = {"OK": 0, "INFO": 1, "WARN": 2, "ALERT": 3}

# ---- L1b 内容级判据默认值（判据内核在 tools/content_change_detector.py，本文件只做接线）----
DEFAULT_CONTENT_MIN_FILES = 3     # 单文件/双文件族无法判「内容零变化」，默认至少 3 份
DEFAULT_CONTENT_MAX_FILES = 500   # 只取最新 N 份（大目录不全扫，窗口贴近当下）

# ---- L4 退役/人工家族（state=legacy|on_demand）----
# legacy：任务定义已删除/归档 ⇒ 缺产出属预期（静默）；但窗口内又长出新产出 = 登记过期 ⇒ WARN
# on_demand：仅人工触发（试用重置等）⇒ 无产出与新产出都属预期（静默，只留证据）
DEFAULT_REVIVE_WITHIN_DAYS = 3.0


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


def check_target(t, root, ref=None, ld=None, pf=None):
    """对一个判据目标求判定。返回 dict。

    pf=L0 前置条件可用性（R475，来自判据表 preflight 段与 preflight_check.l0_map）；
    仅对显式登记的目标传入（未登记=None ⇒ 本层完全不参与，既有判定逐字不变）。
    """
    ref = ref or now_ts()
    res = {"id": t["id"], "kind": t.get("kind", "sche"), "label": t.get("label", t["id"]),
           "cadence": t.get("cadence", ""), "notes": t.get("note", ""),
           "max_run_gap_days": t.get("max_run_gap_days"),
           "checks": [], "verdict": "OK", "severity": t.get("severity", "hard")}

    def bump(level, text, layer=None):
        # layer 可为 canonical face（run/log/…）或 L-前缀（L1/L1b/…）；不传则从 text 前缀推断。
        # 推断不到（如 launchd 状态行）由调用方显式传 run ⇒ 仍无 ID 的行保持原样（加法式）。
        face = None
        if layer:
            face = LAYER_FACE.get(layer, layer)
        else:
            face = _face_of_text(text)
        chk = {"level": level, "text": text}
        if face:
            chk["layer"] = face
            if _aeid is not None:
                eid = _aeid.event_id(t.get("id", ""), face)
                chk["event_id"] = eid
                ids = res.setdefault("event_ids", [])
                if eid not in ids:
                    ids.append(eid)
                det = res.setdefault("event_details", [])
                d = {"target": t.get("id", ""), "face": face, "event_id": eid}
                if d not in det:
                    det.append(d)
        res["checks"].append(chk)
        if LEVEL_ORDER[level] > LEVEL_ORDER[res["verdict"]]:
            res["verdict"] = level

    if t.get("disabled"):
        res["verdict"] = "DISABLED"
        res["checks"].append({"level": "INFO", "text": "按设计停用/归档（enabled=false），不参与新鲜度判定"})
        return res

    # ---- L1c 留存断档层（R483, kind=retention）----
    # 与 L1（多久没写）/L1b（写了但没变）正交：本层问的是「该采的日子有没有采到、还来不来得及补」。
    # 时间衰减源有硬留存窗口（Chrome ~90d / kc ~25d / pmset ~15d / wifi.log 1d）⇒ 漏采当天不补
    # 就可能永久丢失（R481 C4 现场实测 kc 3 天、chrome 2 天、pmset 1 天、wifilog 1 天）。
    # 判据内核复用 tools/rolling_source_snapshot.py verify（日期目录 × 必需文件，零 mtime）；
    # 未登记 kind=retention 的目标在此处行为逐字不变（无新键、无新 check）。
    if t.get("kind") == "retention":
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import rolling_source_snapshot as _rss
        srcs = list(t.get("sources") or [])
        roots = {k: (v if os.path.isabs(v) else os.path.join(GA, v))
                 for k, v in dict(t.get("roots") or {}).items()}
        ld = t.get("live_dates") or {}
        if ld:                       # 夹具/离线：显式给出源侧「最老仍可采日」
            live = {n: {"date": ld[n], "rc": 0, "method": "declared"} for n in srcs if n in ld}
        elif t.get("live_probe", True):
            live = None              # 走真实只读探针（只读打开活库，不写）
        else:                        # 显式关探针 ⇒ fail-closed（不假装窗口无限）
            live = {n: {"date": None, "rc": 2, "method": "probe-disabled"} for n in srcs}
        try:
            today = datetime.date.fromtimestamp(ref).isoformat()
        except Exception:
            today = None
        try:
            out = _rss.verify_all(roots=roots, today=today, live=live, proof=False,
                                  sources=srcs or None)
        except Exception as e:       # 探头坏掉 ⇒ fail-closed，不静默
            bump("ALERT", "L1c 留存探针不可用（%s: %s）" % (type(e).__name__, e))
            return res
        res["retention"] = {"today": out["today"], "counts": out["counts"], "sources": {}}
        if not out.get("sources"):
            bump("ALERT", "L1c 留存探针空面（未扫到任何登记的源）")
            return res
        for name, v in out["sources"].items():
            missing_non_today = [d for d in v["missing_days"] if d != out["today"]]
            res["retention"]["sources"][name] = {
                "verdict": v["verdict"], "n_present": v["n_present"], "n_missing": v["n_missing"],
                "missing_days": missing_non_today, "missing_lost": v["missing_lost"],
                "pending_today": bool(v.get("pending_today")),
                "missing_recoverable": v["missing_recoverable"],
                "oldest_available": v["oldest_available"], "retention_days": v.get("retention_days"),
                "days_left": v["days_left"], "shell_days": v["shell_days"]}
            extra = []
            if v["shell_days"]:
                extra.append("空壳日目录 %d 天（%s）" % (len(v["shell_days"]), v["shell_days"][:3]))
            if v["missing_recoverable"]:
                extra.append("仍可补采 %d 天（最早 %s，留存≈%s 天，剩 %s 天）"
                             % (len(v["missing_recoverable"]), v["missing_recoverable"][0],
                                v.get("retention_days"), v["days_left"]))
            if v["missing_lost"]:
                extra.append("★永久丢失 %d 天（%s）" % (len(v["missing_lost"]), v["missing_lost"][:3]))
            if v["pending_today"]:
                extra.append("今天尚未采（当天仍可采）")
            desc = "%s 已采 %d 天，缺口 %d 天%s" % (name, v["n_present"], v["n_missing"],
                                                   ("；" + "；".join(extra)) if extra else "")
            if v["missing_lost"] or missing_non_today or v["verdict"] == "UNKNOWN":
                bump("ALERT", "L1c 留存断档：%s" % desc)
            elif v["pending_today"]:
                bump("INFO", "L1c 留存待采：%s" % desc)
            else:
                res["checks"].append({"level": "OK", "text": "L1c 留存完备：%s" % desc})

    # ---- L0 前置条件可用性（R475）----
    # 语义：前置不成立 ⇒ 任务必不会产出（run 级证据可能照样新鲜，mtime 层看不见）。
    # 只加严：FAIL⇒ALERT；WARN/OK 只留 INFO，不改既有 verdict。
    if pf is not None:
        res["preflight"] = pf
        pv = pf.get("verdict")
        if pv == "ALERT":
            bad = [p for p in (pf.get("probes") or []) if p.get("verdict") == "FAIL"]
            bump("ALERT", "L0 前置条件不成立（fail-closed）：%s" % "；".join(
                "%s「%s」" % (p.get("kind"), (p.get("detail") or "")[:90]) for p in bad)[:400])
        elif pv == "WARN":
            unk = [p for p in (pf.get("probes") or []) if p.get("verdict") == "UNVERIFIABLE"]
            res["checks"].append({"level": "INFO", "text":
                "L0 前置条件存在无法判定项 %d/%d（未知≠成立，不计硬问题）" % (len(unk), len(pf.get("probes") or []))})
        else:
            res["checks"].append({"level": "INFO", "text":
                "L0 前置条件可用（%d 项探针全通过）" % len(pf.get("probes") or [])})

    # ---- L4 退役/人工家族（state=legacy|on_demand；R470）----
    # 与 disabled 的区别：任务 json 可能已被删除（legacy），此处按**产出面**判定，不依赖调度条目存在。
    st = str(t.get("state") or "").strip().lower()
    if st in ("legacy", "on_demand"):
        res["state"] = st
        lp, lm, ln = newest_match(t.get("run_evidence") or [], root)
        if lp is None:
            a = None
            ev = "无产出（glob 未命中）"
        else:
            a = age_days(lm, ref)
            ev = "最近产出 %s（%s，%s）" % (fmt_ts(lm), fmt_age(a),
                                          os.path.relpath(lp, root) if lp.startswith(root) else lp)
            res["evidence"] = {"file": os.path.relpath(lp, root) if lp.startswith(root) else lp,
                               "mtime": fmt_ts(lm), "age_days": round(a, 2), "matched": ln}
        if st == "legacy":
            revive = float(t.get("revive_within_days") or DEFAULT_REVIVE_WITHIN_DAYS)
            if a is not None and a <= revive:
                bump("WARN", "L4 登记过期：判据表标为 legacy 的族在 %.1f 天窗口内仍产出（%s）"
                             "⇒ 该族已复活，请复核 state" % (revive, ev))
            else:
                res["verdict"] = "LEGACY"
                res["checks"].append({"level": "INFO", "text":
                                      "L4 已退役/归档族：%s；缺产出属预期（静默，不计硬问题）" % ev})
        else:
            res["verdict"] = "ON_DEMAND"
            res["checks"].append({"level": "INFO", "text":
                                  "L4 人工触发族：%s；无固定节奏，不参与新鲜度判定" % ev})
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

    # ---- L1b 内容级产出变化（补齐 L1 的 mtime 盲区：任务跑了、文件也在写，但内容没变）----
    # 判据内核复用 tools/content_change_detector.py（指纹/归一化/分类零重写）；
    # 本段只做「命中即 bump 等级」。未登记 content 段的目标行为逐字不变。
    res["contents"] = []
    cspecs = t.get("content") or []
    if isinstance(cspecs, dict):
        cspecs = [cspecs]
    if cspecs:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import content_change_detector as _ccd
        for cs in cspecs:
            globs = list(cs.get("globs") or []) or ([cs["glob"]] if cs.get("glob") else [])
            lim = cs.get("max_content_stale_days", _ccd.DEFAULT_ALERT_DAYS)
            # ack=「已知静止源」显式登记（等价 R449 零消费者处置的登记思路）：
            # 命中降为 INFO 并带出理由；未登记的静止仍按 ALERT/WARN 报出（默认不静默）。
            ack = cs.get("ack") or ""
            soft = bool(cs.get("soft")) or bool(ack)
            for pat in globs:
                info = _ccd.scan_family(root, pat, lim,
                                        cs.get("min_files", DEFAULT_CONTENT_MIN_FILES),
                                        cs.get("max_files", DEFAULT_CONTENT_MAX_FILES),
                                        cs.get("hash_cap", _ccd.DEFAULT_HASH_CAP),
                                        cs.get("normalize", True))
                info["soft"] = soft
                info["ack"] = ack
                res["contents"].append(info)
                klass, sd = info["class"], info.get("stale_days") or 0.0
                desc = "%s（n=%d distinct=%d 距上次实质变化 %.2fd）" % (pat, info["n"], info["distinct"], sd)
                if ack:
                    tag = "（已登记静止：%s）" % ack
                elif soft:
                    tag = "（软判据）"
                else:
                    tag = ""
                if klass == "ALL_IDENTICAL" and sd > lim:
                    lvl = "INFO" if soft else "ALERT"
                    bump(lvl, "L1b 产出内容零变化：%s%s" % (desc, tag))
                elif klass == "CONTENT_STALE" and sd > lim:
                    lvl = "INFO" if soft else "WARN"
                    bump(lvl, "L1b 产出内容停滞：%s%s" % (desc, tag))
                elif klass in ("MISSING", "NO_HISTORY"):
                    # 样本不足（单文件族）⇒ 明确「不可用」，不假绿也不告警
                    res["checks"].append({"level": "INFO",
                                          "text": "L1b 内容判据不可用（%s，需 ≥%s 份）：%s"
                                                  % (klass, cs.get("min_files", DEFAULT_CONTENT_MIN_FILES), pat)})
                else:
                    res["checks"].append({"level": "OK", "text": "L1b 产出内容在变：%s" % desc})

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
                bump("ALERT", "launchd plist 不存在：~/Library/LaunchAgents/%s.plist" % label, layer="run")
            elif st is None:
                bump("ALERT", "launchd 未加载：plist 存在但 launchctl list 无 %s（僵尸任务）" % label, layer="run")
            else:
                info = "launchctl: pid=%s last_exit=%s" % (st["pid"] or "-", st["exit"])
                if st["exit"] not in (0, None):
                    bump("WARN", "launchd 上次退出码非 0（%s）: %s" % (st["exit"], info), layer="run")
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
        do_diff=False, ref=None, return_result=False, preflight=True, preflight_live=True):
    ref = ref or now_ts()
    ld = launchd_state()
    # ---- L0 前置条件：登记面=判据表 `preflight` 段（单一来源，勿另存一份）----
    pf_map, pf_err = {}, ""
    if preflight and criteria.get("preflight"):
        try:
            _here = os.path.dirname(os.path.abspath(__file__))
            if _here not in sys.path:
                sys.path.insert(0, _here)
            import preflight_check as _pc                    # noqa: PLC0415
            pf_map = _pc.l0_map(criteria["preflight"], only=set(only) if only else None, live=preflight_live)
        except Exception as e:                               # noqa: BLE001
            pf_err = "%s: %s" % (type(e).__name__, e)        # 失败不静默：记进 result（不改判定）
    targets = criteria["targets"]
    if only:
        want = set(only)
        targets = [t for t in targets if t["id"] in want]
    rows = [check_target(t, root, ref, ld, pf=pf_map.get(t["id"])) for t in targets]
    channels = [check_channel(c, root, ref) for c in criteria.get("channels", [])]

    hard = [r for r in rows if r["severity"] != "soft" and r["verdict"] in ("ALERT", "WARN")]
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    result = {"schema": "ga.task_freshness/v1", "ts": fmt_ts(ref), "root": root,
              "criteria_source": criteria.get("_source", ""), "counts": counts,
              "hard_issue_ids": [r["id"] for r in hard], "targets": rows, "channels": channels,
              "preflight": {"enabled": bool(preflight and criteria.get("preflight")),
                            "registered": len(pf_map), "error": pf_err},
              "corpus": {"targets": len(rows), "channels": len(channels),
                         "payload_files_seen": sum(p.get("matched", 0) for r in rows for p in r.get("payloads", [])),
                         "log_files_scanned": len({h["file"] for r in rows for h in r.get("signatures", [])}),
                         "content_families": sum(len(r.get("contents", [])) for r in rows)}}

    prev = None
    if write_ledger and ledger:
        prev = _ledger_prev(ledger)
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        issues_out = [{"id": r["id"], "verdict": r["verdict"],
                       "event_ids": list(r.get("event_ids", [])),
                       "events": list(r.get("event_details", [])),
                       "first": (r["checks"][0]["text"][:160] if r["checks"] else "")}
                      for r in rows if r["verdict"] in ("ALERT", "WARN")]
        # ★顶层 event_ids = 本轮告警面（只并硬目标 ALERT/WARN），与 issues_out 同口径。
        # 不得并入 OK/INFO/ON_DEMAND/DISABLED 行的事件 ID：读取端（反射器差集、R480 消费器）
        # 只统计硬目标，两端口径不一致会制造「幻影已恢复」（R480 实测 x_ai_intel_daily 的 L2 INFO 行）。
        all_ev = sorted({e for r in rows if r["verdict"] in ("ALERT", "WARN")
                         for e in r.get("event_ids", [])})
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": result["ts"], "counts": counts,
                                "hard_issue_ids": result["hard_issue_ids"],
                                "event_ids": all_ev,
                                "issues": issues_out,
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
    # fixture D：L1b 内容级判据 —— 4 份内容逐字相同的「跑而不产出」族（+ 单文件族负控）
    for i, age in enumerate((30, 20, 10, 1), start=1):
        touch("temp/contentfam/snap_%02d.json" % i, age, '{"v":1}\n')
    touch("temp/oneshot/only_01.json", 0.5, '{"v":1}\n')
    touch("sche_tasks/done/2026-09-27_0800_content_heartbeat.md", 0.30, "# done\n")
    touch("sche_tasks/done/2026-09-27_0800_content_acked.md", 0.30, "# done\n")
    touch("sche_tasks/done/2026-09-27_0800_content_oneshot.md", 0.30, "# done\n")
    # fixture E：L4 退役/人工家族（R470）
    #  E1 legacy 静默：标 legacy、产出停在 120 天前 ⇒ 判 LEGACY（静默）
    #  E2 legacy 复活（负控）：标 legacy 但窗口内（1 天前）又产出 ⇒ 必升 WARN「登记过期」
    #  E3 on_demand：人工触发族，产出停在 90 天前 ⇒ 判 ON_DEMAND（静默）
    touch("sche_tasks/done/2026-05-31_0900_legacy_quiet.md", 120.0, "# done\n")
    touch("sche_tasks/done/2026-09-27_0900_legacy_revived.md", 1.0, "# done\n")
    touch("sche_tasks/done/2026-06-30_0900_ondemand_manual.md", 90.0, "# done\n")

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
        # fixture D：L1b 内容级判据（跑而不产出）
        {"id": "content_heartbeat", "kind": "sche", "cadence": "daily", "max_run_gap_days": 2,
         "run_evidence": ["sche_tasks/done/*_content_heartbeat.md"],
         "payloads": [{"glob": "temp/contentfam/snap_*.json", "max_silence_days": 8}],
         "content": [{"glob": "temp/contentfam/snap_*.json", "max_content_stale_days": 8, "min_files": 3}],
         "scans": []},
        # 同上族但显式登记「已知静止」（ack）⇒ 命中须降为 INFO 且不进硬问题
        {"id": "content_acked", "kind": "sche", "cadence": "daily", "max_run_gap_days": 2,
         "run_evidence": ["sche_tasks/done/*_content_acked.md"],
         # 注：刻意不登记 payloads —— 本夹具只测 L1b；若登记，族内 30d 旧文件会先撞 L1 的 mtime 判据
         # （这正是 R457 指出的 mtime 盲区镜像：ack 只对「内容静止」生效，不对「产物过期」生效）
         "content": [{"glob": "temp/contentfam/snap_*.json", "max_content_stale_days": 8, "min_files": 3,
                      "ack": "受监控对象自 2026-05-19 起静止（selftest 夹具）"}],
         "scans": []},
        # 单文件族：样本不足 ⇒ 不可用（不假绿也不告警）
        {"id": "content_oneshot", "kind": "sche", "cadence": "daily", "max_run_gap_days": 2,
         "run_evidence": ["sche_tasks/done/*_content_oneshot.md"],
         "payloads": [{"glob": "temp/oneshot/only_*.json", "max_silence_days": 8}],
         "content": [{"glob": "temp/oneshot/only_*.json", "max_content_stale_days": 8, "min_files": 3}],
         "scans": []},
        # fixture E：L4 退役/人工家族（R470）—— state=legacy 走「登记过期」护栏；on_demand 恒静默
        {"id": "legacy_quiet", "kind": "sche", "cadence": "legacy", "state": "legacy", "severity": "soft",
         "note": "已退役任务（判据表登记，产出面静默）",
         "run_evidence": ["sche_tasks/done/*_legacy_quiet.md"]},
        {"id": "legacy_revived", "kind": "sche", "cadence": "legacy", "state": "legacy", "severity": "soft",
         "revive_within_days": 3,
         "run_evidence": ["sche_tasks/done/*_legacy_revived.md"]},
        {"id": "ondemand_manual", "kind": "sche", "cadence": "on-demand", "state": "on_demand", "severity": "soft",
         "run_evidence": ["sche_tasks/done/*_ondemand_manual.md"]},
    ], "channels": [
        {"id": "www.birdiesearch.com", "via": "birdie_daily_monitor",
         "success_artifact": ["birdie_monitor/birdie_seen_items.json"],
         "scan_globs": ["temp/bg_task/logs/birdie_*.log"], "failure_patterns": ["SSLError"]},
    ]}

    # fixture F：L1c 留存断档层（R483）—— 三个同构 chrome 快照根，只有留存面不同
    _t0 = datetime.date.today()
    def retdir(sub, offsets):
        """建 base/retention/<sub>/<YYYY-MM-DD>/History（chrome 必需文件）。"""
        for off in offsets:
            d = os.path.join(base, "retention", sub,
                             (_t0 - datetime.timedelta(days=off)).isoformat())
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "History"), "w", encoding="utf-8").write("x")
        return os.path.join(base, "retention", sub)
    r_gap = retdir("gap", [5, 4, 1])            # 缺 3、2 天（非今天）+ 今天未采 ⇒ ALERT
    r_pend = retdir("pending", [4, 3, 2, 1])    # 只缺今天 ⇒ INFO（当天仍可采）
    r_lost = retdir("lost", [8, 3])             # 源侧最老仍可采=4 天前 ⇒ 7/6/5 天前不可补
    criteria["targets"] += [
        {"id": "retention_gap", "kind": "retention", "label": "留存断档（selftest 夹具）",
         "sources": ["chrome"], "roots": {"chrome": r_gap}},
        {"id": "retention_pending", "kind": "retention", "label": "只缺今天（selftest 夹具）",
         "sources": ["chrome"], "roots": {"chrome": r_pend}},
        {"id": "retention_lost", "kind": "retention", "label": "永久丢失（selftest 夹具）",
         "sources": ["chrome"], "roots": {"chrome": r_lost},
         "live_dates": {"chrome": (_t0 - datetime.timedelta(days=4)).isoformat()}},
    ]

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
    # + 4 个内容族夹具（temp/contentfam/snap_*.json，经 content_heartbeat 的 payloads 计入）
    # + 1 个单文件族（temp/oneshot/only_*.json）+ 2 个带签名的日志（birdie_0925.log / haisou_20260925_1115.log）
    expect("⑦ 语料自证：payload=8 且日志=2 且内容族=3（精确计数）",
           res["corpus"]["payload_files_seen"] == 8 and res["corpus"]["log_files_scanned"] == 2
           and res["corpus"]["content_families"] == 3,
           str(res["corpus"]))
    # ⑧–⑫ L1b 内容级判据（跑而不产出不得判健康）——本段五条即 R457 候选的接线负控
    hb = rows["content_heartbeat"]
    expect("⑧ 内容零变化（4 份逐字相同，离线 30d）→ content_heartbeat ALERT",
           hb["verdict"] == "ALERT" and hb["contents"][0]["class"] == "ALL_IDENTICAL"
           and hb["contents"][0]["distinct"] == 1,
           "%s / class=%s distinct=%s" % (hb["verdict"], hb["contents"][0]["class"],
                                          hb["contents"][0]["distinct"]))
    ack = rows["content_acked"]
    expect("⑨ 已登记静止（ack）→ 同样命中但降为 INFO，且不进硬问题",
           ack["verdict"] == "INFO" and ack["id"] not in res["hard_issue_ids"],
           "%s / hard=%s" % (ack["verdict"], ack["id"] in res["hard_issue_ids"]))
    one = rows["content_oneshot"]
    expect("⑩ 单文件族（样本不足）→ 判「不可用」，既不假绿也不告警",
           one["verdict"] == "OK" and one["contents"][0]["class"] == "NO_HISTORY",
           "%s / class=%s" % (one["verdict"], one["contents"][0]["class"]))
    # 负控：真改内容 ⇒ 必须转绿（证明判定随输入变，不是硬编码结论）
    touch("temp/contentfam/snap_01.json", 0.1, '{"v":2,"changed":"yes"}\n')
    res4 = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    rows4 = {r["id"]: r for r in res4["targets"]}
    hb4 = rows4["content_heartbeat"]
    expect("⑪ 负控：改写族内一份内容 → content_heartbeat 转绿（distinct=2）",
           hb4["verdict"] == "OK" and hb4["contents"][0]["distinct"] == 2,
           "%s / distinct=%s class=%s" % (hb4["verdict"], hb4["contents"][0]["distinct"],
                                          hb4["contents"][0]["class"]))
    # 负控：未登记 content 段的目标（birdie）行为不得受本改动影响
    expect("⑫ 负控：未登记 content 段的目标 contents 为空（默认行为逐字不变）",
           rows["birdie_daily_monitor"]["contents"] == [],
           str(rows["birdie_daily_monitor"]["contents"][:1]))
    # ⑬–⑯ L4 退役/人工家族语义（R470）—— ⑭⑯ 为负控
    expect("⑬ legacy 族（产出停在 120d）→ 判 LEGACY，不进硬问题",
           rows["legacy_quiet"]["verdict"] == "LEGACY"
           and "legacy_quiet" not in res["hard_issue_ids"],
           "%s / hard=%s" % (rows["legacy_quiet"]["verdict"],
                             "legacy_quiet" in res["hard_issue_ids"]))
    expect("⑭ 负控：legacy 族在 3d 窗口内又产出 → 必升 WARN「登记过期」（判定随输入变）",
           rows["legacy_revived"]["verdict"] == "WARN"
           and "登记过期" in (rows["legacy_revived"]["checks"][-1]["text"] if rows["legacy_revived"]["checks"] else ""),
           "%s / %s" % (rows["legacy_revived"]["verdict"],
                        (rows["legacy_revived"]["checks"][-1]["text"] if rows["legacy_revived"]["checks"] else "")[:40]))
    expect("⑮ on_demand 族（产出停在 90d）→ 判 ON_DEMAND（无固定节奏，静默）",
           rows["ondemand_manual"]["verdict"] == "ON_DEMAND",
           rows["ondemand_manual"]["verdict"])
    expect("⑯ 负控：未登记 state 的目标逐字不变（无 state 键、verdict 仍 OK）",
           "state" not in rows["github_trending"] and rows["github_trending"]["verdict"] == "OK",
           "%s / state=%s" % (rows["github_trending"]["verdict"], rows["github_trending"].get("state")))

    # ⑰–⑲ L0 前置条件层（R475）——⑰ 是负控（前置可用不得报警）；⑱⑲ 证明判定随输入与开关变
    pf_path = os.path.join(base, "output", "tech_trends_2026-09-25.md")
    criteria["preflight"] = {"targets": [{"id": "github_trending", "label": "selftest 前置夹具",
                                          "probes": [{"kind": "path_exists", "params": {"paths": [pf_path]}}]}]}
    res5 = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    gt = {r["id"]: r for r in res5["targets"]}["github_trending"]
    expect("⑰ 负控：L0 前置可用 → 不改变判定（github_trending 仍 OK，preflight 段带出）",
           gt["verdict"] == "OK" and gt["preflight"]["verdict"] == "OK"
           and res5["preflight"]["registered"] == 1 and gt["id"] not in res5["hard_issue_ids"],
           "%s / pf=%s / hard=%s" % (gt["verdict"], gt.get("preflight"), gt["id"] in res5["hard_issue_ids"]))
    # 探针指到不存在的路径 ⇒ 前置不成立必升 ALERT（产物 mtime 全新鲜也拦得住）
    criteria["preflight"]["targets"][0]["probes"][0]["params"]["paths"] = [os.path.join(base, "missing_never.txt")]
    res6 = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    gt6 = {r["id"]: r for r in res6["targets"]}["github_trending"]
    expect("⑱ 负控：L0 前置不成立（路径缺失）→ 必升 ALERT 且进硬问题",
           gt6["verdict"] == "ALERT" and gt6["id"] in res6["hard_issue_ids"]
           and gt6["preflight"]["verdict"] == "ALERT",
           "%s / hard=%s" % (gt6["verdict"], gt6["id"] in res6["hard_issue_ids"]))
    # 关掉 L0 ⇒ 必须回到 OK（证明是开关生效，不是硬编码结论）
    res7 = run(criteria, base, as_json=True, write_ledger=False, return_result=True, preflight=False)
    gt7 = {r["id"]: r for r in res7["targets"]}["github_trending"]
    expect("⑲ 负控：--no-preflight ⇒ 同一夹具回 OK 且 preflight 段缺席（开关生效）",
           gt7["verdict"] == "OK" and "preflight" not in gt7 and res7["preflight"]["registered"] == 0,
           "%s / %s" % (gt7["verdict"], res7["preflight"]))

    # ⑳ 台账顶层 event_ids 口径（R480）：只并硬目标 ⇒ 与读取端（反射器差集/消费器）一致
    led_path = os.path.join(base, "ledger_scope_selftest.jsonl")
    if os.path.exists(led_path):
        os.remove(led_path)
    res8 = run(criteria, base, as_json=True, ledger=led_path, write_ledger=True, return_result=True)
    with open(led_path, encoding="utf-8") as _lf:
        _rec = json.loads(_lf.read().strip().splitlines()[-1])
    _hard = {e for r in res8["targets"] if r["verdict"] in ("ALERT", "WARN") for e in r.get("event_ids", [])}
    _nonhard = {e for r in res8["targets"] if r["verdict"] not in ("ALERT", "WARN") for e in r.get("event_ids", [])}
    expect("⑳ 负控：台账顶层 event_ids 只并硬目标事件（非硬目标事件不得混入）",
           set(_rec["event_ids"]) == _hard and not (set(_rec["event_ids"]) & _nonhard),
           "top=%d hard=%d nonhard=%d" % (len(_rec["event_ids"]), len(_hard), len(_nonhard)))

    # ㉑–㉕ L1c 留存断档层（R483）—— ㉒㉔㉕ 为负控
    rg = rows["retention_gap"]
    _cg = rg["retention"]["sources"]["chrome"]
    expect("㉑ 留存缺口（非今天缺 2 天）→ ALERT 且进硬问题，缺口天数带出",
           rg["verdict"] == "ALERT" and rg["id"] in res["hard_issue_ids"]
           and _cg["n_missing"] >= 1 and _cg["missing_lost"] == []
           and any("留存断档" in c["text"] for c in rg["checks"]),
           "%s / %s" % (rg["verdict"], _cg))
    rp = rows["retention_pending"]
    expect("㉒ 负控：只缺今天（当天仍可采）→ INFO，不进硬问题",
           rp["verdict"] == "INFO" and rp["id"] not in res["hard_issue_ids"]
           and rp["retention"]["sources"]["chrome"]["verdict"] == "WARN_PENDING_TODAY",
           "%s / %s" % (rp["verdict"], rp["retention"]["sources"]["chrome"]["verdict"]))
    _cl = rows["retention_lost"]["retention"]["sources"]["chrome"]
    expect("㉓ 已滚出源窗口的缺口 → ALERT 且 missing_lost 非空（不可补）",
           rows["retention_lost"]["verdict"] == "ALERT" and len(_cl["missing_lost"]) == 3
           and _cl["oldest_available"] == (_t0 - datetime.timedelta(days=4)).isoformat(),
           "%s / lost=%s oldest=%s" % (rows["retention_lost"]["verdict"],
                                       _cl["missing_lost"], _cl["oldest_available"]))
    expect("㉔ 负控：未登记 kind=retention 的目标无 retention 键（既有目标行为逐字不变）",
           "retention" not in rows["github_trending"] and rows["github_trending"]["verdict"] == "OK",
           str(rows["github_trending"].get("retention")))
    retdir("gap", [3, 2])                       # 补齐缺口 ⇒ 只剩今天未采
    res9 = run(criteria, base, as_json=True, write_ledger=False, return_result=True)
    rg9 = {r["id"]: r for r in res9["targets"]}["retention_gap"]
    expect("㉕ 负控：补上缺口目录 → retention_gap 转 INFO（判定随输入变，非硬编码结论）",
           rg9["verdict"] == "INFO" and rg9["id"] not in res9["hard_issue_ids"],
           "%s / %s" % (rg9["verdict"], rg9["retention"]["sources"]["chrome"]["missing_days"]))

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
    ap.add_argument("--no-preflight", action="store_true",
                    help="关闭 L0 前置条件层（前置不成立也可判 OK；默认开启）")
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
               ledger=a.ledger, write_ledger=not a.no_ledger, do_diff=a.diff,
               preflight=not a.no_preflight)


if __name__ == "__main__":
    sys.exit(main())
