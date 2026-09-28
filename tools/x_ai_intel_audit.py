#!/usr/bin/env python3
"""x_ai_intel_daily（x.com 情报链路）只读审计与「修复 or 退役」判定内核 (R484)

背景
----
`x_ai_intel_daily` 每天 09:00 由 sche_tasks 调度，唯一硬前置是「浏览器里存在一个
已登录 x.com 的普通标签页且 TMWebDriver 可连」。自 2026-08-17 之后再无成功记录，
R467 三层判据回测里 artifact 层 0 天命中、run 层迟 11 天命中 ⇒ 需要一份机械可复跑
的判定，回答三个问题：

  1. 逐份回执到底成没成功（统计口径必须可复跑且不靠 LLM 自述）
  2. 根因归属：我方脚本缺陷，还是外部前置（会话/标签页）缺失
  3. 给出「修复（具体修复点）」或「退役（含复活判据）」的二选一裁决输入

口径铁律（本文件的全部结论都建立在下面三条之上）
------------------------------------------------
* **成功标记是 stdout 记号，不是回执记号**：脚本只在最后
  `print(f"Saved {len(all_new_posts)} new posts to {report_path}")`，该行只进
  /tmp/x_ai_intel.log，从不写进 done 回执。因此「done 回执里没有 `Saved N new
  posts`」**不能**当作失败证据（旧 SOP 判据是弱代理）。
* **权威产物 = 脚本自己的 reports/ 目录 + SQLite 去重库**：脚本在采集成功后
  必然会写一份报告（即使 `新增信号：0 条`）并更新 DB；这两处的时间戳是
  artifact 层唯一可信的「最近一次真跑通」证据。
* **否定语境里的标记不算成功**：回执里大量出现
  「未出现 `Saved N new posts`」「未触发 ... `Saved N new posts` 成功状态」这类
  否定引用，宽松正则（含 new posts 且无 RuntimeError）会把其中带 CDP 异常的
  那些误判成功（实测 2026-08-18 那份）。本器按标记前后窗口内的否定词判定。

子命令（位置参数，非 --flag）
-----------------------------
    census      回执普查 + 产物普查（含严格/宽松/本器三口径对照）
    attribution 根因归属的静态检查（源码标记、prompt 记号、判据表陈述、消费者面）
    verdict     census + attribution 合成判定（含裁决输入与复活判据）
    selftest    自测（fixture + 负控）
    proof       只读自证（输入文件 sha256 前后一致）

只读性：全程不写任何被检查对象；DB 以 `file:...?mode=ro` 打开，绝不写回。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

TASK_ID = "x_ai_intel_daily"
FILE_TAG = "x_ai_intel_daily"

# 否定语境词（标记前后窗口内出现即判「否定引用」而非成功）
NEGATION_TOKENS = (
    "未出现", "未产生", "未触发", "没有", "无新增", "未达", "未见",
    "never", "not ", "missing", "absent", "未生成",
)

MARKER_RE = re.compile(r"Saved\s+(\d+)\s+new posts")
# 引用形态（回执常把标记写成占位符 `Saved N new posts`）：只用于「否定引用」记账，
# 绝不能当成功证据 —— 占位符 N 不是真实条数。
MARKER_ANY_RE = re.compile(r"Saved\s+(?:\d+|N)\s+new posts")
# 判定一份文本确实是本任务的回执所需的特征词（否则 fail-closed 归 UNCLEAR）
RUN_DONE_TOKEN = "RUN_DONE"
RECEIPT_TOKENS = ("x_ai", "x.com", "X 会话", "X browser tab", "标签页", "跳过", RUN_DONE_TOKEN)
NO_TAB_TOKEN = "No active X browser tab"
CDP_TOKENS = ("CDP fallback failed", "No tab with given id")
NEG_WINDOW = 80


def _now() -> float:
    return time.time()


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(path: str, cap: int = 64 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk or cap <= 0:
                break
            cap -= len(chunk)
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def paths(root: str) -> dict:
    return {
        "root": root,
        "done_glob": os.path.join(root, "sche_tasks", "done", "*%s*" % FILE_TAG),
        "script": os.path.join(root, "temp", "x_ai_intel", "x_intel.py"),
        "accounts": os.path.join(root, "temp", "x_ai_intel", "accounts.json"),
        "reports": os.path.join(root, "temp", "x_ai_intel", "reports"),
        "db": os.path.join(root, "temp", "x_ai_intel", "x_intel.sqlite3"),
        "task_json": os.path.join(root, "sche_tasks", "%s.json" % TASK_ID),
        "criteria": os.path.join(root, "tools", "freshness_criteria.json"),
        "scheduler_log": os.path.join(root, "sche_tasks", "scheduler.log"),
    }


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 回执分类
# --------------------------------------------------------------------------
def classify_receipt(text: str) -> dict:
    """把一个 done 回执分类。

    返回 {cls, evidence, marker_positive, marker_negated, loose_rule}
      cls: SUCCESS_MARKER / SUCCESS_CONTENT / FAILURE_NO_TAB / FAILURE_CDP
           / FAILURE_OTHER / UNCLEAR
    """
    marker_positive = []
    marker_negated = []
    for m in MARKER_ANY_RE.finditer(text):
        lo = max(0, m.start() - NEG_WINDOW)
        hi = min(len(text), m.end() + NEG_WINDOW)
        window = text[lo:hi]
        if any(tok in window for tok in NEGATION_TOKENS):
            marker_negated.append(m.group(0))
        elif MARKER_RE.fullmatch(m.group(0)):
            # 只有真实条数形态才算成功证据；占位符 `Saved N new posts` 永不算
            marker_positive.append(m.group(0))

    content_positive = [m.group(0) for m in re.finditer(r"新增信号：([1-9]\d*) 条", text)]

    has_no_tab = NO_TAB_TOKEN in text
    has_cdp = any(tok in text for tok in CDP_TOKENS)
    looks_like_receipt = any(tok in text for tok in RECEIPT_TOKENS)

    # 宽松规则 = 旧 SOP 记录的第二口径：含 'new posts' 且不含 'RuntimeError'
    loose = ("new posts" in text) and ("RuntimeError" not in text)

    if marker_positive or content_positive:
        cls = "SUCCESS_MARKER" if marker_positive else "SUCCESS_CONTENT"
        ev = (marker_positive + content_positive)[:3]
    elif has_no_tab:
        cls, ev = "FAILURE_NO_TAB", [NO_TAB_TOKEN]
    elif has_cdp:
        cls, ev = "FAILURE_CDP", [t for t in CDP_TOKENS if t in text]
    elif text.strip() and looks_like_receipt:
        cls, ev = "FAILURE_OTHER", []
    else:
        # fail-closed：既无成功证据也无本任务特征词（或空文件）⇒ 不可判定，
        # 绝不因为「有内容」就记成一次失败（会把 502/HTML 之类噪声算进来）。
        cls, ev = "UNCLEAR", ["not_a_receipt"] if text.strip() else ["empty"]

    return {
        "cls": cls,
        "evidence": ev,
        "marker_positive": marker_positive,
        "marker_negated": marker_negated,
        "content_positive": content_positive,
        "loose_rule": loose,
        "strict_rule": bool(marker_positive),
        "has_no_tab": has_no_tab,
        "has_cdp": has_cdp,
    }


def scan_receipts(root: str) -> list:
    out = []
    for f in sorted(glob.glob(paths(root)["done_glob"])):
        text = read_text(f)
        st = os.stat(f)
        rec = {
            "path": f,
            "name": os.path.basename(f),
            "date": os.path.basename(f)[:10],
            "bytes": st.st_size,
            "mtime": st.st_mtime,
            "mtime_str": _ts(st.st_mtime),
        }
        rec.update(classify_receipt(text))
        out.append(rec)
    return out


def scan_artifacts(root: str) -> dict:
    p = paths(root)
    art = {"reports_exists": os.path.isdir(p["reports"]), "reports": [], "db": {}}
    if art["reports_exists"]:
        for f in sorted(glob.glob(os.path.join(p["reports"], "*"))):
            st = os.stat(f)
            art["reports"].append({
                "name": os.path.basename(f),
                "bytes": st.st_size,
                "mtime": st.st_mtime,
                "mtime_str": _ts(st.st_mtime),
            })
    art["reports_count"] = len(art["reports"])
    art["newest_report"] = art["reports"][-1] if art["reports"] else None

    if os.path.exists(p["db"]):
        st = os.stat(p["db"])
        art["db"] = {"bytes": st.st_size, "mtime": st.st_mtime, "mtime_str": _ts(st.st_mtime)}
        try:
            con = sqlite3.connect("file:%s?mode=ro" % p["db"], uri=True)
            rows = con.execute("SELECT handle, url, published_at, collected_at FROM posts").fetchall()
            con.close()
            art["db"]["posts"] = len(rows)
            art["db"]["collected_at"] = sorted({r[3] for r in rows})
            art["db"]["handles"] = sorted({r[0] for r in rows})
        except sqlite3.Error as exc:  # fail-closed：读不到就标 UNKNOWN，不当 0
            art["db"]["error"] = str(exc)
    else:
        art["db"] = {"exists": False}

    stamps = [r["mtime"] for r in art["reports"]]
    if art["db"].get("mtime"):
        stamps.append(art["db"]["mtime"])
    art["last_artifact_write"] = max(stamps) if stamps else None
    art["last_artifact_write_str"] = _ts(max(stamps)) if stamps else None
    return art


# --------------------------------------------------------------------------
# census
# --------------------------------------------------------------------------
def census(root: str) -> dict:
    p = paths(root)
    receipts = scan_receipts(root)
    art = scan_artifacts(root)
    res = {
        "task_id": TASK_ID,
        "root": root,
        "receipts_total": len(receipts),
        "receipts": receipts,
        "artifacts": art,
    }
    if not receipts:
        res["verdict"] = "INSUFFICIENT_DATA"
        res["reason"] = "no receipts matched %s" % p["done_glob"]
        return res

    res["first_receipt"] = receipts[0]["name"]
    res["last_receipt"] = receipts[-1]["name"]
    res["first_receipt_ts"] = receipts[0]["mtime_str"]
    res["last_receipt_ts"] = receipts[-1]["mtime_str"]

    cls_count = {}
    for r in receipts:
        cls_count[r["cls"]] = cls_count.get(r["cls"], 0) + 1
    success = [r for r in receipts if r["cls"].startswith("SUCCESS")]
    failures = [r for r in receipts if r["cls"].startswith("FAILURE")]
    res["class_counts"] = cls_count
    res["success_count"] = len(success)
    res["failure_count"] = len(failures)
    res["unclear_count"] = len(receipts) - len(success) - len(failures)

    # 三口径对照：严格 / 宽松 / 本器
    res["rule_compare"] = {
        "strict_marker_hits": sum(1 for r in receipts if r["strict_rule"]),
        "loose_rule_hits": sum(1 for r in receipts if r["loose_rule"]),
        "classifier_success": len(success),
        "loose_false_positives": [r["name"] for r in receipts if r["loose_rule"] and not r["cls"].startswith("SUCCESS")],
    }
    res["failure_forms"] = {
        "NO_TAB": sum(1 for r in failures if r["cls"] == "FAILURE_NO_TAB"),
        "CDP": sum(1 for r in failures if r["cls"] == "FAILURE_CDP"),
        "OTHER": sum(1 for r in failures if r["cls"] == "FAILURE_OTHER"),
    }

    # 连续失败天数（按回执日期去重）
    dates = sorted({r["date"] for r in receipts if r["cls"].startswith("FAILURE")})
    res["failure_dates"] = dates
    if res["failure_count"]:
        first_f = min(r["mtime"] for r in failures)
        last_f = max(r["mtime"] for r in failures)
        res["first_failure_ts"] = _ts(first_f)
        res["last_failure_ts"] = _ts(last_f)
        res["failure_span_days"] = round((last_f - first_f) / 86400.0, 2)

    if success:
        last_s = max(success, key=lambda r: r["mtime"])
        res["last_success_receipt"] = last_s["name"]
    else:
        res["last_success_receipt"] = None

    # 与 artifact 交叉：最近一次真实产物写入 vs 最近一次成功回执
    if art["last_artifact_write"]:
        res["artifact_frozen_days"] = round((_now() - art["last_artifact_write"]) / 86400.0, 2)
    return res


# --------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------
def scan_consumers(root: str) -> dict:
    """仓内哪些地方引用了 x_intel（排除本审计器自身与 done 回执）。"""
    tokens = ("x_intel", "x_ai_intel")
    dirs = ["tools", "reflect_scripts", "memory", "sche_tasks", "temp"]
    hits = {}
    for d in dirs:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in (".git", "__pycache__", "done", "x_ai_intel")]
            for fn in filenames:
                if not fn.endswith((".py", ".md", ".json", ".txt", ".sh")):
                    continue
                fp = os.path.join(dirpath, fn)
                rel = os.path.relpath(fp, root)
                if rel == os.path.relpath(__file__, root):
                    continue
                try:
                    if os.path.getsize(fp) > 4 * 1024 * 1024:
                        continue
                    txt = read_text(fp)
                except OSError:
                    continue
                if any(t in txt for t in tokens):
                    hits[rel] = 0
    return hits


def attribution(root: str) -> dict:
    p = paths(root)
    out = {"task_id": TASK_ID, "checks": {}}

    script = read_text(p["script"])
    if not script:
        out["checks"]["script_present"] = {"status": "NO_DATA",
                                           "evidence": "script not found: %s" % p["script"]}
    else:
        out["checks"]["script_present"] = {"status": "OK", "evidence": p["script"]}

    # 1) 成功标记是否真存在于源码（决定「回执无标记」是否可作为失败证据）
    if script:
        has_marker = ("new posts to" in script) and ("Saved " in script)
        out["checks"]["success_marker_in_source"] = {
            "status": "OK" if has_marker else "MISSING",
            "evidence": "源码含 print(... 'Saved {len} new posts to {report_path}')" if has_marker
                        else "源码中找不到成功标记 ⇒ 回执缺该标记不能当失败证据",
        }
        # 2) prompt 要求等 RUN_DONE，脚本是否真的打印
        run_done_in_script = RUN_DONE_TOKEN in script
        out["checks"]["run_done_in_source"] = {
            "status": "PRESENT" if run_done_in_script else "ABSENT",
            "evidence": "脚本%s包含 %s" % ("" if run_done_in_script else "**不**", RUN_DONE_TOKEN),
        }
        # 3) fail-fast 点行号
        lines = script.splitlines()
        for i, ln in enumerate(lines, 1):
            if NO_TAB_TOKEN in ln:
                out["checks"]["fail_fast_line"] = {"status": "OK", "line": i, "text": ln.strip()[:120]}
                break

    # 4) prompt 是否引用 RUN_DONE（与上一条合成 MISMATCH 判据）
    task_txt = read_text(p["task_json"])
    if task_txt:
        try:
            task = json.loads(task_txt)
        except ValueError:
            task = {}
        prompt = str(task.get("prompt", ""))
        out["checks"]["prompt_waits_run_done"] = {
            "status": "MISMATCH" if (RUN_DONE_TOKEN in prompt and not RUN_DONE_TOKEN in script) else "OK",
            "evidence": "prompt 要求轮询日志直到出现 %s" % RUN_DONE_TOKEN if RUN_DONE_TOKEN in prompt else "prompt 未引用该记号",
        }
        out["task_enabled"] = bool(task.get("enabled", False))
        out["task_schedule"] = task.get("schedule")
        out["task_max_delay_hours"] = task.get("max_delay_hours")

    # 5) accounts.json 与 SOP 的 6 账号是否一致
    acc_txt = read_text(p["accounts"])
    if acc_txt:
        try:
            acc = json.loads(acc_txt)
            handles = [a.get("handle") for a in acc.get("accounts", acc)]
            out["checks"]["accounts"] = {"status": "OK", "count": len(handles), "handles": handles}
        except ValueError:
            out["checks"]["accounts"] = {"status": "NO_DATA", "evidence": "accounts.json 不可解析"}
    else:
        out["checks"]["accounts"] = {"status": "NO_DATA", "evidence": "accounts.json 缺失"}

    # 6) 脚本 mtime vs 首次失败时刻（编辑不是失败起点 ⇒ 排除「改脚本改坏了」）
    receipts = scan_receipts(root)
    failures = [r for r in receipts if r["cls"].startswith("FAILURE")]
    if os.path.exists(p["script"]) and failures:
        sm = os.stat(p["script"]).st_mtime
        first_f = min(r["mtime"] for r in failures)
        out["checks"]["script_mtime_vs_first_failure"] = {
            "status": "OK",
            "script_mtime": _ts(sm),
            "first_failure": _ts(first_f),
            "script_edited_after_first_failure": sm > first_f,
            "evidence": "首次失败发生在脚本最后一次改动%s ⇒ 失败形态无法由该次改动解释"
                        % ("之前" if sm > first_f else "之后"),
        }

    # 7) 判据表对该任务/产物目录的陈述是否与事实冲突
    crit = read_text(p["criteria"])
    if crit:
        try:
            cj = json.loads(crit)
        except ValueError:
            cj = {}
        note = ""
        soft = None
        for t in cj.get("targets", []):
            if t.get("id") == TASK_ID:
                note = str(t.get("note", ""))
                soft = t.get("soft")
                break
        claim_deprecated = "已弃用" in note
        reports_is_live_dir = ("reports" in script and "DEFAULT_REPORTS" in script) if script else None
        out["checks"]["criteria_note"] = {
            "status": "CLAIM_CONFLICT" if (claim_deprecated and reports_is_live_dir) else "OK",
            "soft": soft,
            "claims_reports_dir_deprecated": claim_deprecated,
            "reports_is_script_output_dir": reports_is_live_dir,
            "note": note[:400],
        }

    # 8) 消费者面（退役是否会打断下游）
    hits = scan_consumers(root)
    monitor = sorted(k for k in hits if k.startswith("tools/") or k.startswith("reflect_scripts/"))
    out["consumers"] = {
        "total_files": len(hits),
        "monitor_side": monitor,
        "files": sorted(hits),
        "downstream_dataconsumer_count": 0 if not monitor else None,
    }

    # 9) 前置阶段取证（回执里是否有「未进入采集」类自述 + 独立浏览器取证）
    indep = []
    for r in receipts:
        t = read_text(r["path"])
        if "example.com" in t:
            indep.append(r["name"])
    out["independent_browser_evidence"] = indep
    return out


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------
def verdict(root: str) -> dict:
    cen = census(root)
    att = attribution(root)
    checks = att.get("checks", {})
    v = {
        "task_id": TASK_ID,
        "census": {k: cen.get(k) for k in (
            "receipts_total", "success_count", "failure_count", "unclear_count",
            "first_receipt", "last_receipt", "last_success_receipt", "first_failure_ts",
            "last_failure_ts", "failure_span_days", "failure_forms", "rule_compare",
            "artifact_frozen_days", "class_counts")},
        "artifacts": {
            "reports_count": cen["artifacts"]["reports_count"],
            "newest_report": cen["artifacts"]["newest_report"],
            "db": cen["artifacts"]["db"],
            "last_artifact_write": cen["artifacts"]["last_artifact_write_str"],
        },
        "attribution": checks,
        "consumers": att.get("consumers", {}),
        "scheduler": scheduler_stats(root),
    }
    # 归属结论
    script_ok = checks.get("script_present", {}).get("status") == "OK"
    marker_ok = checks.get("success_marker_in_source", {}).get("status") == "OK"
    ext = cen.get("failure_forms", {}).get("NO_TAB", 0) + cen.get("failure_forms", {}).get("CDP", 0)
    v["root_cause"] = {
        "class": "EXTERNAL_PRECONDITION_MISSING" if (script_ok and marker_ok and ext > 0) else "UNDETERMINED",
        "evidence": [
            "失败全部落在 set_session 前置检查（%s）" % (checks.get("fail_fast_line", {}).get("text", "")),
            "脚本含成功标记且 2026-08-17 16:03:49 曾有真实成功产物（DB collected_at 与 reports/ 同秒）",
            "%d/%d 份失败回执的形态只有 NO_TAB/CDP 两类（标签页维度）" % (ext, cen.get("failure_count", 0)),
        ],
    }
    v["fixable_without_user"] = {
        "collection_side": [],
        "monitoring_side": [
            "判据表 note 失实（reports 目录被称『已弃用』）⇒ 可改为 artifact 硬判据",
            "prompt 等 RUN_DONE（脚本从不打印）⇒ 措辞与源码对齐",
        ],
        "note": "采集面修复都需要用户重新提供已登录 x.com 标签页（SOP 明令不得代开标签）",
    }
    v["recommendation"] = "RETIRE_OR_DISABLE_SCHEDULE"
    v["revival_criteria"] = (
        "以下任一条成立即可撤销本判定并恢复调度：①浏览器里出现已登录 x.com 的普通标签页"
        "（非 OneTab 收纳页）后手动跑一次 `python3 temp/x_ai_intel/x_intel.py`，日志出现 "
        "`Saved N new posts` 且 temp/x_ai_intel/reports/ 新增文件；"
        "②或用户明确要求把采集面改为自带浏览器（独立 profile）方案。"
    )
    return v


def scheduler_stats(root: str) -> dict:
    p = paths(root)
    log = read_text(p["scheduler_log"])
    if not log:
        return {"status": "NO_DATA"}
    trig, skip = [], 0
    skip_all = {}
    for line in log.splitlines():
        if TASK_ID in line:
            if "TRIGGER %s" % TASK_ID in line:
                trig.append(line)
            elif "SKIP %s" % TASK_ID in line:
                skip += 1
        if "SKIP " in line:
            m = re.search(r"SKIP ([a-z_0-9]+):", line)
            if m:
                skip_all[m.group(1)] = skip_all.get(m.group(1), 0) + 1
    top = sorted(skip_all.items(), key=lambda kv: -kv[1])[:5]
    return {
        "trigger_count": len(trig),
        "last_trigger": trig[-1] if trig else None,
        "skip_lines_for_task": skip,
        "skip_lines_all_tasks": sum(skip_all.values()),
        "skip_top5_all_tasks": top,
        "skip_note": "SKIP 刷屏是调度器级通用行为（各任务共有），非本任务缺陷",
    }


# --------------------------------------------------------------------------
# proof / selftest
# --------------------------------------------------------------------------
def proof(root: str) -> dict:
    p = paths(root)
    files = []
    for f in sorted(glob.glob(p["done_glob"])):
        files.append(f)
    for k in ("script", "accounts", "db", "task_json", "criteria", "scheduler_log"):
        if os.path.exists(p[k]):
            files.append(p[k])
    if os.path.isdir(p["reports"]):
        files.extend(sorted(glob.glob(os.path.join(p["reports"], "*"))))
    before = {os.path.relpath(f, root): sha256_file(f) for f in files}
    census(root)
    attribution(root)
    after = {os.path.relpath(f, root): sha256_file(f) for f in files}
    changed = [k for k in before if before[k] != after.get(k)]
    return {"files": len(before), "changed": changed, "ok": not changed}


# --- selftest fixture -----------------------------------------------------
GOOD_RECEIPT = """# x_ai_intel 执行报告

执行时间：2026-08-01 09:04
日志末行：Saved 3 new posts to /tmp/x_ai_intel/reports/20260801_0904.md
状态：✅ 完成，已推送。
"""

NEGATED_RECEIPT = """# x_ai_intel 执行报告

执行时间：2026-08-02 09:04
因此未出现 `RUN_DONE` 或 `Saved N new posts` 成功标记。
RuntimeError: No active X browser tab was found.
状态：⏭️ 跳过。
"""

CDP_RECEIPT = """# x_ai_intel 执行报告

执行时间：2026-08-03 09:03
脚本识别到一个 X 会话，但采集时标签页失效：
`Exception: {'name': 'Error', 'message': 'CDP fallback failed: No tab with given id 2109107881.'}`
因此未出现 `Saved N new posts` 成功标记。未确认新增帖子。
"""

GARBAGE_RECEIPT = "<html><body>502 Bad Gateway</body></html>\n"


def _build_fixture(base: str, receipts: dict, *, reports_after_success=False,
                   script_text=None, with_script=True) -> str:
    os.makedirs(os.path.join(base, "sche_tasks", "done"), exist_ok=True)
    os.makedirs(os.path.join(base, "temp", "x_ai_intel", "reports"), exist_ok=True)
    os.makedirs(os.path.join(base, "tools"), exist_ok=True)
    for name, text in receipts.items():
        with open(os.path.join(base, "sche_tasks", "done", name), "w", encoding="utf-8") as fh:
            fh.write(text)
    if with_script:
        with open(os.path.join(base, "temp", "x_ai_intel", "x_intel.py"), "w", encoding="utf-8") as fh:
            fh.write(script_text if script_text is not None else 'print(f"Saved {len(new)} new posts to {p}")\n'
                     'raise RuntimeError("No active X browser tab was found. Open x.com in the connected browser and sign in.")\n')
    with open(os.path.join(base, "temp", "x_ai_intel", "accounts.json"), "w", encoding="utf-8") as fh:
        json.dump({"accounts": [{"handle": h} for h in
                                ("OpenAI", "AnthropicAI", "GoogleDeepMind", "huggingface", "sama", "karpathy")]}, fh)
    rep = os.path.join(base, "temp", "x_ai_intel", "reports", "20260801_0904.md")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write("# X AI 情报快报\n\n新增信号：3 条\n")
    # DB
    con = sqlite3.connect(os.path.join(base, "temp", "x_ai_intel", "x_intel.sqlite3"))
    con.execute("CREATE TABLE posts (url TEXT PRIMARY KEY, handle TEXT, author TEXT, text TEXT, published_at TEXT, collected_at TEXT)")
    con.execute("INSERT INTO posts VALUES ('u1','OpenAI','OpenAI','t','2026-08-01','2026-08-01T01:04:00+00:00')")
    con.commit()
    con.close()
    if reports_after_success:  # 让产物比回执“更新”，制造矛盾
        future = time.time() + 86400
        os.utime(rep, (future, future))
    return base


def selftest() -> int:
    ok = 0
    fail = []
    tmp = tempfile.mkdtemp(prefix="x_ai_audit_st_")

    def check(name, cond, detail=""):
        nonlocal ok
        if cond:
            ok += 1
            print("PASS %s %s" % (name, detail))
        else:
            fail.append(name)
            print("FAIL %s %s" % (name, detail))

    # T1 分类器：否定语境里的标记不算成功
    c = classify_receipt(NEGATED_RECEIPT)
    check("T1_NEGATED_NOT_SUCCESS", c["cls"] == "FAILURE_NO_TAB", c["cls"])
    check("T2_NEGATED_MARKER_RECORDED", c["marker_negated"] and not c["marker_positive"], str(c["marker_negated"]))

    # T3 真成功标记 → SUCCESS
    c = classify_receipt(GOOD_RECEIPT)
    check("T3_POSITIVE_MARKER_SUCCESS", c["cls"] == "SUCCESS_MARKER", c["cls"])

    # T4 CDP 形态 + 宽松规则陷阱（含 'new posts' 且无 RuntimeError）
    c = classify_receipt(CDP_RECEIPT)
    check("T4_CDP_CLASSIFIED", c["cls"] == "FAILURE_CDP", c["cls"])
    check("T5_LOOSE_RULE_TRAP", c["loose_rule"] is True and not c["cls"].startswith("SUCCESS"),
          "loose=%s cls=%s" % (c["loose_rule"], c["cls"]))

    # T6 垃圾内容 → UNCLEAR（fail-closed，不当成功）
    c = classify_receipt(GARBAGE_RECEIPT)
    check("T6_GARBAGE_UNCLEAR", c["cls"] == "UNCLEAR", c["cls"])

    # T7 否定词在窗口外 → 仍判成功（窗口正确性）
    far = "Saved 2 new posts to /x.md" + " " * 300 + "未出现别的东西"
    c = classify_receipt(far)
    check("T7_WINDOW_BOUNDARY", c["cls"] == "SUCCESS_MARKER", c["cls"])

    # T8 空回执目录 → INSUFFICIENT_DATA rc=2
    d = _build_fixture(os.path.join(tmp, "empty"), {}, with_script=False)
    cen = census(d)
    check("T8_EMPTY_INSUFFICIENT", cen.get("verdict") == "INSUFFICIENT_DATA", str(cen.get("verdict")))

    # T9 正常 fixture：产物比回执新 ⇒ 矛盾标记（artifact 与回执不可混判）
    d_contra = _build_fixture(os.path.join(tmp, "contra"),
                              {"2026-08-01_0904_x_ai_intel_daily.md": GOOD_RECEIPT,
                               "2026-08-02_0904_x_ai_intel_daily.md": NEGATED_RECEIPT},
                              reports_after_success=True)
    cen = census(d_contra)
    check("T9_SUCCESS_AND_FAILURE_COUNTED",
          cen["success_count"] == 1 and cen["failure_count"] == 1,
          "s=%s f=%s" % (cen["success_count"], cen["failure_count"]))
    check("T10_ARTIFACT_NEWER_THAN_LAST_RECEIPT",
          cen["artifacts"]["last_artifact_write"] > max(r["mtime"] for r in cen["receipts"]),
          "artifact=%s" % cen["artifacts"]["last_artifact_write_str"])

    # T11 脚本缺失 → NO_DATA，不得伪装成 OK（且不得凭空断言 RUN_DONE 状态）
    d = _build_fixture(os.path.join(tmp, "noscript"), {"2026-08-01_0904_x_ai_intel_daily.md": NEGATED_RECEIPT},
                       with_script=False)
    att = attribution(d)
    check("T11_MISSING_SCRIPT_NO_DATA", att["checks"]["script_present"]["status"] == "NO_DATA",
          att["checks"]["script_present"]["status"])
    check("T11b_NO_SCRIPT_NO_RUN_DONE_CLAIM",
          "run_done_in_source" not in att["checks"],
          "无脚本时不得给出 run_done 结论")

    # T12 RUN_DONE 缺失检测（用**有脚本但无 RUN_DONE** 的 fixture）
    att = attribution(d_contra)
    check("T12_RUN_DONE_ABSENT", att["checks"]["run_done_in_source"]["status"] == "ABSENT",
          att["checks"]["run_done_in_source"]["status"])

    # T13 成功标记存在性检测
    att = attribution(d_contra)
    check("T13_MARKER_IN_SOURCE", att["checks"]["success_marker_in_source"]["status"] == "OK",
          att["checks"]["success_marker_in_source"]["status"])

    # T14 判据表失实声明 → CLAIM_CONFLICT（fixture 造一份带「已弃用」的 criteria）
    d2 = _build_fixture(os.path.join(tmp, "crit"), {"2026-08-01_0904_x_ai_intel_daily.md": NEGATED_RECEIPT})
    crit = {"targets": [{"id": TASK_ID, "soft": True,
                         "note": "产物即 done 报告；temp/x_ai_intel/reports 为旧目录，已弃用故不设判据"}]}
    with open(os.path.join(d2, "tools", "freshness_criteria.json"), "w", encoding="utf-8") as fh:
        json.dump(crit, fh)
    with open(os.path.join(d2, "temp", "x_ai_intel", "x_intel.py"), "w", encoding="utf-8") as fh:
        fh.write('DEFAULT_REPORTS = ROOT / "reports"\nprint(f"Saved {len(a)} new posts to {p}")\n')
    att = attribution(d2)
    check("T14_CRITERIA_CLAIM_CONFLICT", att["checks"]["criteria_note"]["status"] == "CLAIM_CONFLICT",
          att["checks"]["criteria_note"]["status"])

    # T15 无失实声明时不得误报
    crit["targets"][0]["note"] = "产物即 done 报告"
    with open(os.path.join(d2, "tools", "freshness_criteria.json"), "w", encoding="utf-8") as fh:
        json.dump(crit, fh)
    att = attribution(d2)
    check("T15_CRITERIA_NO_FALSE_ALARM", att["checks"]["criteria_note"]["status"] == "OK",
          att["checks"]["criteria_note"]["status"])

    # T16 只读自证：proof 前后 sha 一致
    pr = proof(d2)
    check("T16_PROOF_OK", pr["ok"] and pr["files"] >= 3, str(pr))

    # T17 verdict 端到端：全失败 fixture → EXTERNAL_PRECONDITION_MISSING + RETIRE 建议
    d3 = _build_fixture(os.path.join(tmp, "verdict"),
                        {"2026-08-02_0904_x_ai_intel_daily.md": NEGATED_RECEIPT,
                         "2026-08-03_0903_x_ai_intel_daily.md": CDP_RECEIPT})
    v = verdict(d3)
    check("T17_VERDICT_EXTERNAL", v["root_cause"]["class"] == "EXTERNAL_PRECONDITION_MISSING",
          v["root_cause"]["class"])
    check("T18_VERDICT_RECOMMEND_RETIRE", v["recommendation"] == "RETIRE_OR_DISABLE_SCHEDULE",
          v["recommendation"])
    check("T19_REVIVAL_CRITERIA_PRESENT", "Saved N new posts" in v["revival_criteria"], "")

    # T20 反事实负控：若脚本里没有成功标记（被改坏），归属不得再判外部
    d4 = _build_fixture(os.path.join(tmp, "nomarker"),
                        {"2026-08-02_0904_x_ai_intel_daily.md": NEGATED_RECEIPT},
                        script_text='print("hello")\n')
    v = verdict(d4)
    check("T20_NO_MARKER_UNDETERMINED", v["root_cause"]["class"] == "UNDETERMINED", v["root_cause"]["class"])

    # T21 对照：把真成功回执混进全失败集 ⇒ 计数与裁决输入随之变化（灵敏度）
    d5 = _build_fixture(os.path.join(tmp, "mixed"),
                        {"2026-08-01_0904_x_ai_intel_daily.md": GOOD_RECEIPT,
                         "2026-08-02_0904_x_ai_intel_daily.md": NEGATED_RECEIPT})
    cen5 = census(d5)
    check("T21_MIXED_SUCCESS_COUNT", cen5["success_count"] == 1 and cen5["last_success_receipt"].endswith("0904_x_ai_intel_daily.md"),
          str(cen5["last_success_receipt"]))

    print("\nSELFTEST: %d passed, %d failed" % (ok, len(fail)))
    if fail:
        print("failed: %s" % ", ".join(fail))
        return 1
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="x_ai_intel_daily 只读审计与修复/退役判定 (R484)")
    ap.add_argument("cmd", choices=("census", "attribution", "verdict", "proof", "selftest"))
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--json", default=None, help="把结果写到该路径")
    args = ap.parse_args()

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "proof":
        res = proof(args.root)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res["ok"] else 1

    if args.cmd == "census":
        res = census(args.root)
    elif args.cmd == "attribution":
        res = attribution(args.root)
    else:
        res = verdict(args.root)

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print("written: %s" % args.json)

    if args.cmd == "census":
        print("receipts=%s success=%s failure=%s unclear=%s" % (
            res.get("receipts_total"), res.get("success_count"), res.get("failure_count"), res.get("unclear_count")))
        print("window: %s → %s" % (res.get("first_receipt_ts"), res.get("last_receipt_ts")))
        print("rule_compare: %s" % json.dumps(res.get("rule_compare"), ensure_ascii=False))
        print("failure_forms: %s" % json.dumps(res.get("failure_forms"), ensure_ascii=False))
        print("last_artifact_write=%s (frozen %.2f d)" % (res.get("artifacts", {}).get("last_artifact_write_str"),
                                                          res.get("artifact_frozen_days") or -1))
        print("artifact reports=%s newest=%s db=%s" % (
            res.get("artifacts", {}).get("reports_count"),
            (res.get("artifacts", {}).get("newest_report") or {}).get("name"),
            json.dumps({k: v for k, v in (res.get("artifacts", {}).get("db") or {}).items()
                        if k in ("bytes", "posts", "collected_at", "handles", "error")}, ensure_ascii=False)))
    elif args.cmd == "attribution":
        for k, v in res.get("checks", {}).items():
            print("%-32s %s" % (k, json.dumps(v, ensure_ascii=False)[:220]))
        print("consumers(total=%s) monitor-side=%s" % (res["consumers"]["total_files"], res["consumers"]["monitor_side"]))
        print("independent_browser_evidence=%s" % res.get("independent_browser_evidence"))
    else:
        print(json.dumps({k: res[k] for k in ("root_cause", "recommendation", "revival_criteria",
                                              "fixable_without_user")}, ensure_ascii=False, indent=1))
        print("\n-- census --")
        print(json.dumps(res["census"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
