#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""l4_coverage_audit.py — L4 会话归档覆盖度审计（只读；近窗口「原文留存空洞」清单）

为什么需要它
------------
memory/L4_raw_sessions/*.zip 是本机唯一的会话原文归档面（tools/l4_search.py 实测
6 zip / 415 成员），但**近期会话是否入档从来没人核过**。更危险的是：会话原文还有别的
留存面（temp/model_responses/*.txt、temp/desktop_sessions/*.json、logs/ 滚动日志），
它们都是**滚动**的 —— 某段时间若三个面都不在，则「原文级回源」永久缺失（R423 实测
恢复会话被误写内容时，正是靠 logs/*.log 与 temp/model_responses/ 才救回）。

本器只回答一个问题：**近 N 天里，哪些日子没有任何原文留存面**，并把该日的报告编号
（来自 memory/autonomous_reports/history.txt）列出来，作为「可能丢失结论原文」的量级。

设计口径（与既有件的关系，避免重复造轮子）
----------------------------------------
* 复用 tools/l4_search.py 内核（动态 import 其 L4DIR/zips()），**不重写** zip 枚举。
* R418 clone_reproduce_audit 判「引用路径能否随克隆可得」；R507 memory_asset_durability
  判「非 .md 资产是否耐久」；本器只管**时间维度**的原文留存面空洞，不判路径、不判内容。
* 判据分强/弱两档，防「回声污染」：强=行内时间戳 `YYYY-MM-DD[ T]HH:MM:SS`；
  弱=裸日期 token。某源对某日计「覆盖」的条件= strong≥1 或 weak≥WEAK_MIN(=3)。
* 硬约束：**zip 内有文件 ≠ 已归档会话原文** —— 只有文件名符合 `MMDD_HHMM-MMDD_HHMM.txt`
  的成员才算会话原文（其他成员计入 non_session 计数，只作 INFO，不授予覆盖）。
* 只读：全程 'r'/'rb' 打开，不写源；仅当显式 --json/--md 时落产物。

用法
----
  python3 tools/l4_coverage_audit.py scan [--days 30] [--end YYYY-MM-DD] [--root DIR]
                                           [--json FILE] [--md FILE]
  python3 tools/l4_coverage_audit.py proof        # 只读自证（源面 sha256 前后一致）
  python3 tools/l4_coverage_audit.py selftest     # 合成夹具负控（不碰真实语料）

退出码：0=窗口内无空洞；1=存在空洞（报告，本身不是错误）；2=用法/语料不合格（fail-closed）。

产出（真实语料，2026-09-29 首跑）见 memory/l4_coverage/COVERAGE.md（由 --md 生成）。
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ── 常量（可被 selftest 夹具覆盖的路径全部经 Corpus 解析） ──────────────────────────
MAX_BYTES_PER_FILE = 256 * 1024 * 1024   # 单文件扫描上限；超限记 SKIPPED，绝不静默跳过
WEAK_MIN = 3                             # 弱证据阈值（裸日期 token 数）
# 权威面（授予「该日有原文可回源」）：L4 zip 成员名 / all_histories 的 SESSION 头。
# 提示面（只作旁证，绝不授予覆盖）：滚动日志与响应文本——它们含跨日回声
# （命令输出、报告正文、记忆文本被回显），实测能把 2020/2027 这类日期刷进命中集。
AUTHORITATIVE_SOURCES = ("l4_zip", "all_histories")
HINT_SOURCES = ("model_responses", "desktop_sessions", "logs_live", "logs_archive")
SESSION_RX = re.compile(r"^SESSION:\s*(\d{2})(\d{2})_\d{4}-(\d{2})(\d{2})_\d{4}\s*$", re.M)
EVIDENCE_SAMPLE = 3                      # 每天每源最多留几条样证
WINDOW_DAYS_DEFAULT = 30

DATE_RX = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")
STRONG_RX = re.compile(r"(20\d\d)-(\d\d)-(\d\d)[ T]\d\d:\d\d:\d\d")
# 会话原文成员名：MMDD_HHMM-MMDD_HHMM.txt（起止可跨日/跨月）
MEMBER_RX = re.compile(r"^(\d{2})(\d{2})_(\d{4})-(\d{2})(\d{2})_(\d{4})\.txt$")
ZIP_RX = re.compile(r"^(20\d\d)-(\d\d)\.zip$")
HIST_RX = re.compile(r"^R(\d+)\s*\|\s*(20\d\d-\d\d-\d\d)\s*\|")

SOURCE_LABELS = {
    "l4_zip": "L4会话原文(zip)",
    "all_histories": "all_histories.txt",
    "model_responses": "temp/model_responses",
    "desktop_sessions": "temp/desktop_sessions",
    "logs_live": "logs/*.log",
    "logs_archive": "logs/archive/*.gz",
}


# ── 内核复用（不重写 zip 枚举） ─────────────────────────────────────────────────
def load_l4_kernel():
    import importlib.util
    p = os.path.join(REPO, "tools", "l4_search.py")
    spec = importlib.util.spec_from_file_location("l4_search_kernel", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Corpus(object):
    """语料路径解析；--root 让 selftest 指向合成夹具。"""

    def __init__(self, root=None):
        self.root = os.path.abspath(root or REPO)
        self.l4dir = os.path.join(self.root, "memory", "L4_raw_sessions")
        self.all_histories = os.path.join(self.l4dir, "all_histories.txt")
        self.model_responses = os.path.join(self.root, "temp", "model_responses")
        self.desktop_sessions = os.path.join(self.root, "temp", "desktop_sessions")
        self.logdir = os.path.join(self.root, "logs")
        self.log_archive = os.path.join(self.logdir, "archive")
        self.reports_history = os.path.join(self.root, "memory", "autonomous_reports", "history.txt")


# ── 证据收集 ──────────────────────────────────────────────────────────────────
class Coverage(object):
    def __init__(self, days):
        self.days = list(days)
        self.day_set = set(self.days)
        self.counts = {d: {} for d in self.days}       # day -> source -> count(强证据条数)
        self.weak = {d: {} for d in self.days}         # day -> source -> 弱 token 数
        self.evidence = {d: {} for d in self.days}     # day -> source -> [样证]
        self.all_counts = {}                           # 全部命中日（不限窗口）: day -> source -> n
        self.strong_counts = {}                        # 全窗口日: 仅强证据（行内时间戳）命中
        self.hint_days = {}                            # source -> set(day)（提示面覆盖日，不授予覆盖）
        self.hint_evidence = {}                        # day -> source -> [样证]
        self.cov_kind = {}                             # source -> 'authoritative' | 'hint'
        self.stats = {}                                # source -> 统计（自证用）
        self.skipped = []                              # [{path, reason, bytes}]

    def add(self, day, source, count, weak=0, ev=None, authoritative=True):
        self.cov_kind[source] = "authoritative" if authoritative else "hint"
        if not authoritative:
            # 提示面：只登记「见过该日」与样证；不进入 counts/weak/all_counts/strong_counts
            self.hint_days.setdefault(source, set()).add(day)
            if ev:
                lst = self.hint_evidence.setdefault(day, {}).setdefault(source, [])
                if len(lst) < EVIDENCE_SAMPLE:
                    lst.append(ev)
            return
        if count > 0 or weak > 0:
            ac = self.all_counts.setdefault(day, {})
            ac[source] = ac.get(source, 0) + 1
        if count > 0:
            sc = self.strong_counts.setdefault(day, {})
            sc[source] = sc.get(source, 0) + 1
        if day not in self.day_set:
            return
        if count:
            self.counts[day][source] = self.counts[day].get(source, 0) + count
        if weak:
            self.weak[day][source] = self.weak[day].get(source, 0) + weak
        if ev:
            lst = self.evidence[day].setdefault(source, [])
            if len(lst) < EVIDENCE_SAMPLE:
                lst.append(ev)

    def hint_by(self, day):
        """提示面：只回答「该日在哪些滚动面里出现过」，不参与覆盖判定。"""
        return [s for s in HINT_SOURCES if day in self.hint_days.get(s, ())]

    def covered_by(self, day):
        """强证据≥1 或 弱≥WEAK_MIN 才算覆盖（仅权威面可授予）。"""
        out = []
        for s in SOURCE_LABELS:
            if self.counts[day].get(s, 0) >= 1 or self.weak[day].get(s, 0) >= WEAK_MIN:
                out.append(s)
        return out


def _read_text(path, skipped, limit=MAX_BYTES_PER_FILE):
    """返回文本（gzip 自动解压）；超限/读失败记 skipped 并返回 None。"""
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return f.read(limit)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception as e:                                    # noqa: BLE001
        skipped.append({"path": path, "reason": "read_error:%s" % type(e).__name__, "bytes": -1})
        return None


def scan_text_tokens(cov, source, text, ev_label, authoritative=True):
    """把文本里的强/弱日期 token 按日累加。"""
    strong = {}
    for m in STRONG_RX.finditer(text):
        d = "%s-%s-%s" % m.groups()
        strong[d] = strong.get(d, 0) + 1
    weak = {}
    for m in DATE_RX.finditer(text):
        d = "%s-%s-%s" % m.groups()
        weak[d] = weak.get(d, 0) + 1
    for d, n in strong.items():
        cov.add(d, source, n, ev=ev_label, authoritative=authoritative)
    for d, n in weak.items():
        w = n - strong.get(d, 0)
        if w > 0:
            cov.add(d, source, 0, weak=w, ev=None, authoritative=authoritative)
    return strong, weak


def resolve_years(days_known):
    """从已知日集里取年份候选（升序）。"""
    return sorted({d[:4] for d in days_known})


def scan_session_headers(cov, source, text, ev_label, years):
    """all_histories 的 `SESSION: MMDD_HHMM-MMDD_HHMM` 结构头 → 权威覆盖日。

    文件内无年份字样 ⇒ 年份解析规则：候选年 = L4 zip 语料出现的年份；
    仅 1 个候选年时直接采用（本仓即 2026 单一候选）；
    多候选时按文件顺序取「离前一条最近」的年（单调就近），首条取最小候选年。
    """
    heads = SESSION_RX.findall(text)
    prev = None
    n = 0
    for a, b, c, d in heads:
        # 正则分组=(月,日,月,日) ⇒ 日期 token 必须拼月+日；跨日会话起止各记一次
        toks = [(a + b, ev_label), (c + d, ev_label)]
        for t, lb in toks:
            if len(years) == 1:
                y = years[0]
            else:
                cands = sorted(years)
                if prev is None:
                    y = cands[0]
                else:
                    y = min(cands, key=lambda yy: (abs(date(int(yy), int(t[:2]), int(t[2:])) - prev).days, yy))
            try:
                day = date(int(y), int(t[:2]), int(t[2:]))
            except ValueError:
                continue
            if prev is None or day >= prev:
                prev = day
            cov.add(day.isoformat(), source, 1, ev=lb)
            n += 1
    return len(heads), n


def scan_l4_zips(cov, corpus, kernel):
    """会话原文 zip：成员名区间 → 覆盖日；非会话成员只计数。"""
    zips = []
    if kernel is not None:
        L4DIR_K = kernel.L4DIR
        old = kernel.L4DIR
        try:
            kernel.L4DIR = corpus.l4dir
            zips = kernel.zips()                      # 复用内核枚举
        finally:
            kernel.L4DIR = old
        kernel_check = (os.path.normpath(L4DIR_K) == os.path.normpath(corpus.l4dir))
    else:                                             # 内核不可用（夹具/异常）→ 本地 glob，并记账
        zips = sorted(glob.glob(os.path.join(corpus.l4dir, "*.zip")))
        kernel_check = False
    per_zip = []
    for zp in zips:
        mz = ZIP_RX.match(os.path.basename(zp))
        if not mz:
            cov.skipped.append({"path": zp, "reason": "zip_name_not_YYYY-MM", "bytes": -1})
            continue
        year, month = int(mz.group(1)), int(mz.group(2))
        n_all = n_sess = n_nonsess = 0
        days = set()
        with zipfile.ZipFile(zp) as zf:
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                n_all += 1
                mm = MEMBER_RX.match(os.path.basename(info.filename))
                if not mm:
                    n_nonsess += 1
                    continue
                n_sess += 1
                sm, sd, em, ed = (int(mm.group(i)) for i in (1, 2, 4, 5))
                try:
                    a = date(year, sm, sd)
                    b = date(year, em, ed)
                    if b < a:
                        b = date(year + 1, em, ed)
                except ValueError:
                    cov.skipped.append({"path": "%s::%s" % (os.path.basename(zp), info.filename),
                                        "reason": "bad_member_date", "bytes": info.file_size})
                    continue
                cur = a
                while cur <= b:
                    days.add(cur.isoformat())
                    cur += timedelta(days=1)
        for d in sorted(days):
            cov.add(d, "l4_zip", 1, ev=os.path.basename(zp))
        per_zip.append({"zip": os.path.basename(zp), "members": n_all, "session_members": n_sess,
                        "non_session_members": n_nonsess, "days": len(days),
                        "min_day": min(days) if days else None, "max_day": max(days) if days else None})
    cov.stats["l4_zip"] = {"kernels_l4dir_match": kernel_check, "zips": per_zip}
    return per_zip


def scan_dir_tokens(cov, corpus, source, directory, pattern):
    files = sorted(glob.glob(os.path.join(directory, pattern))) if os.path.isdir(directory) else []
    n_ok = 0
    for fp in files:
        try:
            sz = os.path.getsize(fp)
        except OSError:
            cov.skipped.append({"path": fp, "reason": "stat_failed", "bytes": -1})
            continue
        if sz > MAX_BYTES_PER_FILE:
            cov.skipped.append({"path": fp, "reason": "size_over_limit", "bytes": sz})
            continue
        t = _read_text(fp, cov.skipped)
        if t is None:
            continue
        scan_text_tokens(cov, source, t, os.path.basename(fp), authoritative=False)
        n_ok += 1
    cov.stats[source] = {"dir": directory, "files_matched": len(files), "files_scanned": n_ok}
    return n_ok


def scan_logs(cov, corpus):
    for source, d, pat, gz in (("logs_live", corpus.logdir, "*.log", False),
                               ("logs_archive", corpus.log_archive, "*.gz", True)):
        files = sorted(glob.glob(os.path.join(d, pat))) if os.path.isdir(d) else []
        n_ok = 0
        for fp in files:
            try:
                sz = os.path.getsize(fp)
            except OSError:
                cov.skipped.append({"path": fp, "reason": "stat_failed", "bytes": -1})
                continue
            if sz > MAX_BYTES_PER_FILE:
                cov.skipped.append({"path": fp, "reason": "size_over_limit", "bytes": sz})
                continue
            t = _read_text(fp, cov.skipped)
            if t is None:
                continue
            scan_text_tokens(cov, source, t, os.path.basename(fp), authoritative=False)
            n_ok += 1
        cov.stats[source] = {"dir": d, "files_matched": len(files), "files_scanned": n_ok, "gzip": gz}


def load_report_days(corpus):
    """history.txt → {day: [R编号...]}（不读报告正文，只做时间对账）。"""
    out = {}
    if not os.path.isfile(corpus.reports_history):
        return out, 0
    n = 0
    with open(corpus.reports_history, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = HIST_RX.match(line)
            if not m:
                continue
            n += 1
            out.setdefault(m.group(2), []).append("R%s" % m.group(1))
    return out, n


def audit(corpus, days=WINDOW_DAYS_DEFAULT, end=None, kernel=None):
    end_d = date.fromisoformat(end) if end else date.today()
    start_d = end_d - timedelta(days=days - 1)
    window = [(start_d + timedelta(days=i)).isoformat() for i in range(days)]
    cov = Coverage(window)

    if not os.path.isdir(corpus.l4dir) and not os.path.isdir(os.path.join(corpus.root, "temp")):
        raise CorpusError("语料根不存在：%s（既无 memory/L4_raw_sessions 也无 temp/）" % corpus.root)

    scan_l4_zips(cov, corpus, kernel)
    years = resolve_years({d for d, srcs in cov.all_counts.items() for s in srcs if s == "l4_zip"})
    # all_histories.txt：结构头（SESSION: MMDD_HHMM-MMDD_HHMM）才是权威证据；
    # 其正文里的自由日期 token 一律不采（蒸馏文本含跨日回声，见文件头注释）。
    if os.path.isfile(corpus.all_histories):
        t = _read_text(corpus.all_histories, cov.skipped)
        if t is not None:
            nh, nd = scan_session_headers(cov, "all_histories", t, "all_histories.txt", years)
            cov.stats["all_histories"] = {"files_scanned": 1, "session_headers": nh, "days_recorded": nd,
                                          "bytes": os.path.getsize(corpus.all_histories),
                                          "year_candidates": years}
    else:
        cov.stats.setdefault("all_histories", {})["missing"] = True

    scan_dir_tokens(cov, corpus, "model_responses", corpus.model_responses, "*.txt")
    scan_dir_tokens(cov, corpus, "desktop_sessions", corpus.desktop_sessions, "*.json")
    scan_logs(cov, corpus)

    frontier = max(cov.all_counts.keys(), default=None)      # 权威面已覆盖到的最大日
    # 归档起点 = 权威面（L4 zip / all_histories）在本窗口内的最小日。
    # 语义区分：晚于它却无原文 = 真空洞（该有却无）；早于它 = 超出归档面覆盖范围，
    # 属「无从要求」的未归档区，仍列进空洞清单但单独标注，避免把归档起点之前的日期
    # 读成「丢了 N 天原话」（对齐 day_timeline 的 unknown 不补零口径）。
    aut_start = None
    for d0, srcs0 in cov.all_counts.items():
        if set(srcs0) & set(AUTHORITATIVE_SOURCES):
            aut_start = d0 if aut_start is None else min(aut_start, d0)
    per_day = []
    holes = []
    pending = []
    for d in window:
        srcs = cov.covered_by(d)
        hint = cov.hint_by(d)
        if srcs:
            verdict = "COVERED"
        elif frontier and d > frontier:
            verdict = "PENDING_ARCHIVE"        # 尚未到归档边界（L4 滚动滞后），不算空洞
        else:
            verdict = "HOLE"
        row = {"day": d, "verdict": verdict, "sources": srcs, "hint_sources": hint,
               "before_archive_start": bool(aut_start and d < aut_start),
               "counts": {s: cov.counts[d].get(s, 0) for s in srcs},
               "weak": {s: cov.weak[d].get(s, 0) for s in srcs},
               "evidence": {s: cov.evidence[d].get(s, []) for s in srcs}}
        per_day.append(row)
        if verdict == "HOLE":
            holes.append({"day": d, "before_archive_start": bool(aut_start and d < aut_start)})
        elif verdict == "PENDING_ARCHIVE":
            pending.append({"day": d})
    rep_days, n_hist = load_report_days(corpus)
    spans = {}
    for d, srcs in cov.all_counts.items():
        for s in srcs:
            lo, hi = spans.get(s, (d, d))
            spans[s] = (min(lo, d), max(hi, d))
    spans_s = {}
    for d, srcs in cov.strong_counts.items():
        for s in srcs:
            lo, hi = spans_s.get(s, (d, d))
            spans_s[s] = (min(lo, d), max(hi, d))
    thin = [r["day"] for r in per_day if len(r["sources"]) == 1]
    for h in holes:
        h["reports"] = sorted(rep_days.get(h["day"], []))
        h["hint_sources"] = cov.hint_by(h["day"])
    for h in pending:
        h["hint_sources"] = cov.hint_by(h["day"])
    hint_spans = {}
    for s, ds in cov.hint_days.items():
        if ds:
            hint_spans[s] = {"min_day": min(ds), "max_day": max(ds), "days": len(ds)}

    res = {"schema": "ga.l4_coverage/v1", "root": corpus.root, "window": {"start": window[0], "end": window[-1],
           "days": days}, "per_day": per_day, "holes": holes,
           "source_span": {s: {"min_day": v[0], "max_day": v[1]} for s, v in sorted(spans.items())},
           "source_span_strong": {s: {"min_day": v[0], "max_day": v[1]} for s, v in sorted(spans_s.items())},
           "thin_days": thin, "pending_archive": pending,
           "frontier": frontier, "archive_start": aut_start, "hint_span": hint_spans,
           "report_assoc": {"history_lines": n_hist, "days_with_reports": len(rep_days)},
           "stats": cov.stats, "skipped": cov.skipped}
    res["summary"] = {"days": days, "covered": sum(1 for r in per_day if r["verdict"] == "COVERED"),
                      "holes": len(holes), "pending_archive": len(pending),
                      "holes_before_archive_start": sum(1 for h in holes if h.get("before_archive_start")),
                      "holes_inside_archive": sum(1 for h in holes if not h.get("before_archive_start")),
                      "hole_days_with_reports": sum(1 for h in holes if h.get("reports")),
                      "thin_days": len(thin),
                      "skipped_files": len(cov.skipped)}
    return res


class CorpusError(Exception):
    pass


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def _hint_txt(r):
    h = r.get("hint_sources") or []
    return ("|".join(h)) if h else "-"


def render(res):
    L = []
    w = res["window"]
    L.append("== 语料自证 ==")
    for s, st in sorted(res["stats"].items()):
        bits = ["%s=%s" % (k, v) for k, v in sorted(st.items())]
        L.append("  %-17s %s" % (s, " ".join(bits)))
    if res["skipped"]:
        L.append("  skipped_files=%d" % len(res["skipped"]))
        for sk in res["skipped"][:5]:
            L.append("    - %s (%s, %sB)" % (os.path.basename(sk["path"]), sk["reason"], sk["bytes"]))
    L.append("")
    L.append("== 权威面可回源区间（授予覆盖）==")
    for s, v in sorted(res["source_span"].items()):
        L.append("  %-17s %s .. %s" % (s, v["min_day"], v["max_day"]))
    L.append("")
    L.append("== 提示面出现区间（只作旁证，不授予覆盖）==")
    for s, v in sorted(res.get("hint_span", {}).items()):
        L.append("  %-17s %s .. %s (%d 天)" % (s, v["min_day"], v["max_day"], v["days"]))
    if not res.get("hint_span"):
        L.append("  (无)")
    L.append("")
    L.append("== 逐日（%s .. %s, %d 天）==" % (w["start"], w["end"], w["days"]))
    L.append("  归档起点（权威面最小日）: %s" % (res.get("archive_start") or "-"))
    L.append("  %-10s %-16s %-28s %s" % ("date", "verdict", "authoritative", "hint"))
    for r in res["per_day"]:
        L.append("  %-10s %-16s %-28s %s" % (r["day"], r["verdict"],
                                             ",".join(r["sources"]) or "-", _hint_txt(r)))
    L.append("")
    if res["holes"]:
        L.append("== 空洞（权威面缺该日，且已过归档边界 %s）==" % res["frontier"])
        L.append("   其中早于归档起点 %s 的 %d 天属「超出归档面覆盖范围」（无从要求），"
                 "归档起点内的真空洞 %d 天"
                 % (res.get("archive_start") or "-", res["summary"].get("holes_before_archive_start", 0),
                    res["summary"].get("holes_inside_archive", 0)))
        for h in res["holes"]:
            rep = ("报告=%s" % ",".join(h["reports"])) if h.get("reports") else "报告=—"
            L.append("  %s  %s  提示面=%s%s" % (h["day"], rep, _hint_txt(h),
                                                   "  [早于归档起点]" if h.get("before_archive_start") else ""))
    else:
        L.append("== 空洞 == 无 ==")
    if res.get("pending_archive"):
        L.append("")
        L.append("== 待归档（晚于边界 %s，L4 滚动滞后，不计空洞）==" % res["frontier"])
        for h in res["pending_archive"]:
            L.append("  %s  提示面=%s" % (h["day"], _hint_txt(h)))
    if res["thin_days"]:
        L.append("")
        L.append("== 单源薄覆盖日（仅 1 个权威面）==")
        L.append("  " + " ".join(res["thin_days"]))
    s = res["summary"]
    L += ["", "汇总：days=%d covered=%d holes=%d pending=%d hole_with_reports=%d thin=%d skipped=%d"
          % (s["days"], s["covered"], s["holes"], s.get("pending_archive", 0),
             s["hole_days_with_reports"], s.get("thin_days", 0), s["skipped_files"])]
    return "\n".join(L)


def render_md(res):
    w = res["window"]
    s = res["summary"]
    L = ["# L4 会话归档覆盖度审计", "",
         "- 语料根：`%s`" % res["root"],
         "- 窗口：%s .. %s（%d 天）" % (w["start"], w["end"], w["days"]),
         "- 归档边界（权威面最大覆盖日）：`%s`" % res["frontier"],
         "- 归档起点（权威面最小覆盖日）：`%s`" % (res.get("archive_start") or "（本窗口内无权威面）"), "",
         "## 判据", "",
         "覆盖仅由**权威面**授予：L4 zip 的会话成员名（`MMDD_HHMM-MMDD_HHMM.txt`）与",
         "`all_histories.txt` 的 `SESSION:` 结构头。滚动面（`model_responses` / `desktop_sessions` /",
         "`logs_live` / `logs_archive`）只作旁证——它们含跨日回声（命令输出、报告正文、记忆文本被回显），",
         "实测能把 2020/2027 之类日期刷进命中集，故一律不授予覆盖。", "",
         "## 汇总", "",
         "| 指标 | 值 |", "| --- | --- |",
         "| 覆盖日 | %d |" % s["covered"],
         "| 空洞日 | %d |" % s["holes"],
         "| — 其中早于归档起点（无从严要求） | %d |" % s.get("holes_before_archive_start", 0),
         "| — 其中归档面内真空洞 | %d |" % s.get("holes_inside_archive", 0),
         "| 待归档日 | %d |" % s.get("pending_archive", 0),
         "| 空洞日中有报告编号 | %d |" % s["hole_days_with_reports"],
         "| 单源薄覆盖日 | %d |" % s.get("thin_days", 0),
         "| 跳过文件 | %d |" % s["skipped_files"], "",
         "## 各留存面区间", ""]
    L.append("| 面 | 类型 | min | max |")
    L.append("| --- | --- | --- | --- |")
    for src, v in sorted(res["source_span"].items()):
        L.append("| `%s` | 权威 | %s | %s |" % (src, v["min_day"], v["max_day"]))
    for src, v in sorted(res.get("hint_span", {}).items()):
        L.append("| `%s` | 提示 | %s | %s |" % (src, v["min_day"], v["max_day"]))
    L += ["", "## 空洞清单", ""]
    if res["holes"]:
        L.append("| 日 | 关联报告 | 提示面 |")
        L.append("| --- | --- | --- |")
        for h in res["holes"]:
            L.append("| %s | %s | %s |" % (h["day"], ",".join(h["reports"]) or "—", _hint_txt(h)))
    else:
        L.append("（无）")
    if res.get("pending_archive"):
        L += ["", "## 待归档日（晚于边界，不计空洞）", "",
              " ".join(h["day"] for h in res["pending_archive"])]
    L += conclusion_lines(res)
    return "\n".join(L) + "\n"


def conclusion_lines(res):
    """结论节：全部数值由本函数从 res 机械派生（单一源，禁手写进文档）。"""
    s = res["summary"]
    w = res["window"]
    st = res.get("archive_start")
    sp = res.get("source_span", {})
    lz = sp.get("l4_zip", {})
    ah = sp.get("all_histories", {})
    L = ["", "## 结论（由本函数机械生成，勿手改）", ""]
    if st:
        nd = _day_span_days(st, res["frontier"])
        L.append("- 归档面窗口：`%s .. %s`（%d 天）；窗口内覆盖 %d 天、真空洞 %d 天"
                 % (st, res["frontier"], nd, nd - s.get("holes_inside_archive", 0),
                    s.get("holes_inside_archive", 0)))
    else:
        L.append("- 归档面窗口：本窗口内**无权威面**（既无会话原文 zip 成员，也无 `SESSION:` 头）")
    L.append("- 窗口全幅 %s .. %s（%d 天）：覆盖 %d 天、空洞 %d 天 = 早于归档起点 %d 天（无从要求）"
             "+ 归档面内真空洞 %d 天；待归档 %d 天（L4 滚动滞后，不计空洞）"
             % (w["start"], w["end"], w["days"], s["covered"], s["holes"],
                s.get("holes_before_archive_start", 0), s.get("holes_inside_archive", 0),
                s.get("pending_archive", 0)))
    L.append("- 双权威面：`l4_zip` %s .. %s、`all_histories` %s .. %s"
             % (lz.get("min_day", "-"), lz.get("max_day", "-"),
                ah.get("min_day", "-"), ah.get("max_day", "-")))
    hs = res.get("hint_span", {})
    if hs:
        hd = sorted(((v["min_day"], k) for k, v in hs.items()))
        L.append("- 提示面（滚动日志/响应）区间：`%s` 起 %s .. `%s` 止 %s —— 含跨年回声，"
                 "只作旁证，**不授予覆盖**"
                 % (hd[0][1], hd[0][0], hd[-1][1], hd[-1][0]))
    L += ["", "### 消费决策（每条给出「未来决策 ← 指标」映射）", "",
          "1. 要回源某日**原话**：以 `l4_zip` 成员名（`MMDD_HHMM-MMDD_HHMM.txt`）定位 zip 内文件；",
          "   `all_histories.txt` 的 `SESSION:` 头只证明「该日存在会话」，不证明正文可取。",
          "2. 要判「是否该补归档」：看**归档面内真空洞**（%d 天）与**单源薄覆盖**（%d 天）两个数；"
          % (s.get("holes_inside_archive", 0), s.get("thin_days", 0)),
          "   早于归档起点的空洞不构成补归档诉求（该窗口本就没有归档面）。",
          "3. 要判「有没有丢原话」：只有归档面内真空洞 + 该日有关联报告编号（%d 天）才值得逐日追；"
          % s.get("hole_days_with_reports", 0),
          "   本窗口该数为 0/非 0 即为判据落点。",
          "4. 审计窗口起点应取归档起点；早于它的日期一律标 `before_archive_start`，"
          "避免读成「丢了 N 天会话」。"]
    return L


def _day_span_days(a, b):
    d0 = date.fromisoformat(a)
    d1 = date.fromisoformat(b)
    return (d1 - d0).days + 1


def fingerprint(corpus):
    paths = []
    paths += sorted(glob.glob(os.path.join(corpus.l4dir, "*.zip")))
    if os.path.isfile(corpus.all_histories):
        paths.append(corpus.all_histories)
    for d, pat in ((corpus.model_responses, "*.txt"), (corpus.desktop_sessions, "*.json"),
                   (corpus.logdir, "*.log"), (corpus.log_archive, "*.gz")):
        paths += sorted(glob.glob(os.path.join(d, pat)))
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.encode("utf-8", "replace"))
        try:
            stt = os.stat(p)
            h.update(("%d|%d" % (stt.st_size, int(stt.st_mtime))).encode())
        except OSError:
            h.update(b"missing")
    return h.hexdigest(), len(paths)


def cmd_proof(a):
    c = Corpus(a.root)
    before, n = fingerprint(c)
    res = audit(c, days=a.days, end=a.end, kernel=load_l4_kernel() if not a.no_kernel else None)
    after, n2 = fingerprint(c)
    ok = (before == after and n == n2)
    print("files=%d sha256_before=%s sha256_after=%s unchanged=%s" % (n, before[:16], after[:16], ok))
    print("scan: holes=%d covered=%d" % (res["summary"]["holes"], res["summary"]["covered"]))
    return 0 if ok else 1


# ── 自测（合成夹具负控） ──────────────────────────────────────────────────────
def _mkzip(path, members):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members:
            zf.writestr(name, body)


def _fixture(root, holes_spec=None):
    """合成语料：D=2026-09-20 .. 09-24 覆盖，其余（09-15..09-19）留空。"""
    for sub in ("memory/L4_raw_sessions", "temp/model_responses", "temp/desktop_sessions",
                "logs/archive", "memory/autonomous_reports"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    holes_spec = holes_spec or {}
    # L4: 2026-09.zip（09-20..09-24 会话原文；另含一个非会话成员）
    _mkzip(os.path.join(root, "memory/L4_raw_sessions/2026-09.zip"),
           [("0920_1000-0922_1200.txt", "session a"),
            ("0924_0900-0924_1000.txt", "session b"),
            ("notes.txt", "not a session transcript")])
    open(os.path.join(root, "memory/L4_raw_sessions/all_histories.txt"), "w").write(
        "2026-09-21 10:00:00 history\n")
    # 弱证据源
    open(os.path.join(root, "temp/model_responses/model_responses_1.txt"), "w").write(
        "date 2026-09-23\n2026-09-23\n2026-09-23\n")
    open(os.path.join(root, "temp/desktop_sessions/sess-1.json"), "w").write(
        "{\"t\": \"2026-09-24 11:00:00\"}")
    open(os.path.join(root, "logs/app.log"), "w").write("2026-09-22 09:00:00 boot\n")
    if holes_spec.get("archive_gz"):
        with gzip.open(os.path.join(root, "logs/archive/app.log.20260923-010101.log.gz"), "wt") as f:
            f.write("2026-09-23 01:01:01 rotated\n")
    # history.txt：空洞日也有报告编号（用于「可能丢失结论」关联）
    open(os.path.join(root, "memory/autonomous_reports/history.txt"), "w").write(
        "R900 | 2026-09-16 | 产出 | 空洞日样例 | x\n"
        "R901 | 2026-09-22 | 产出 | 覆盖日样例 | x\n")
    return root


def _run_audit(root, days, end, kernel=None):
    import io
    import contextlib
    buf = io.StringIO()
    try:
        res = audit(Corpus(root), days=days, end=end, kernel=kernel)
    except CorpusError as e:
        return 2, str(e), None
    with contextlib.redirect_stdout(buf):
        print(render(res))
    return (1 if res["holes"] else 0), buf.getvalue(), res


def cmd_selftest(a):
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="l4cov_selftest_")
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print("  %s %s %s" % ("✅" if cond else "❌", name, extra))

    try:
        # T1 正常夹具：窗口只覆盖 09-20..09-24 ⇒ 09-20..24 COVERED，越界日不算
        t1 = _fixture(os.path.join(base, "t1"), {"archive_gz": True})
        rc, out, res = _run_audit(t1, days=10, end="2026-09-24")
        chk("T1 rc=1(有空洞)", rc == 1)
        chk("T1 09-20/session 原文覆盖", any(r["day"] == "2026-09-20" and "l4_zip" in r["sources"]
                                            for r in res["per_day"]))
        r23 = [r for r in res["per_day"] if r["day"] == "2026-09-23"][0]
        chk("T1 09-23 滚动面命中但不授予覆盖（sources 空、只进 hint）",
            r23["sources"] == [] and "model_responses" in r23["hint_sources"]
            and "logs_archive" in r23["hint_sources"], r23["hint_sources"])
        chk("T1 09-23 verdict=%s（非 COVERED）" % r23["verdict"], r23["verdict"] != "COVERED")
        # T2 负控：语料根不存在 ⇒ rc=2
        rc2, out2, _ = _run_audit(os.path.join(base, "no_such_root_ZZQX"), days=5, end="2026-09-24")
        chk("T2 空语料 rc=2", rc2 == 2, out2[:40])
        # T3 负控：挪走 zip（会话原文面消失）⇒ 09-20 必报 HOLE
        t3 = _fixture(os.path.join(base, "t3"))
        os.remove(os.path.join(t3, "memory/L4_raw_sessions/2026-09.zip"))
        rc3, out3, res3 = _run_audit(t3, days=6, end="2026-09-22")
        chk("T3 挪走 zip ⇒ 09-20 报 HOLE(rc=1)", rc3 == 1 and any(h["day"] == "2026-09-20" for h in res3["holes"]))
        # T4 负控：zip 内有文件但**非会话原文** ⇒ 不授予覆盖
        t4 = _fixture(os.path.join(base, "t4"))
        _mkzip(os.path.join(t4, "memory/L4_raw_sessions/2026-08.zip"), [("notes.txt", "plain notes")])
        rc4, out4, res4 = _run_audit(t4, days=6, end="2026-09-19")
        chk("T4 非会话成员不授予覆盖 ⇒ 09-15..19 全 HOLE",
            rc4 == 1 and all(r["verdict"] == "HOLE" for r in res4["per_day"]))
        z = (res4["stats"]["l4_zip"]["zips"] or [{}])
        chk("T4 zip 统计含 non_session_members=%s" % [q.get("non_session_members") for q in z],
            any(q.get("non_session_members", 0) == 1 for q in z))
        # T5 弱证据不足阈值 ⇒ 不算覆盖（单条裸日期）
        t5 = _fixture(os.path.join(base, "t5"))
        open(os.path.join(t5, "temp/model_responses/model_responses_1.txt"), "w").write("2026-09-19\n")
        rc5, out5, res5 = _run_audit(t5, days=6, end="2026-09-22")
        chk("T5 单条弱 token 不足阈值 ⇒ 09-19 HOLE",
            any(h["day"] == "2026-09-19" for h in res5["holes"]))
        # T6 报告编号关联：空洞日 09-16 关联 R900
        rc6, out6, res6 = _run_audit(t1, days=12, end="2026-09-24")
        h16 = [h for h in res6["holes"] if h["day"] == "2026-09-16"]
        chk("T6 空洞日关联报告编号", h16 and h16[0]["reports"] == ["R900"], h16)
        chk("T6 history 行自证", res6["report_assoc"]["history_lines"] == 2)
        # T7 越界日不计入窗口（09-24 之后的日期不出现在窗口）
        chk("T7 窗口边界", res6["window"]["start"] == "2026-09-13" and res6["window"]["end"] == "2026-09-24")
        # T8 超限文件记 SKIPPED 而非静默
        t8 = _fixture(os.path.join(base, "t8"))
        big = os.path.join(t8, "logs/big.log")
        open(big, "w").write("x" * 10)
        old_lim = globals()["MAX_BYTES_PER_FILE"]
        globals()["MAX_BYTES_PER_FILE"] = 4
        try:
            rc8, out8, res8 = _run_audit(t8, days=3, end="2026-09-22")
        finally:
            globals()["MAX_BYTES_PER_FILE"] = old_lim
        chk("T8 超限记 SKIPPED(size_over_limit)",
            any(s["reason"] == "size_over_limit" for s in res8["skipped"]), res8["skipped"][:1])
        # T9 只读：扫描前后指纹一致
        f1 = fingerprint(Corpus(t1))[0]
        _run_audit(t1, days=10, end="2026-09-24")
        f2 = fingerprint(Corpus(t1))[0]
        chk("T9 只读（指纹前后一致）", f1 == f2)
        # T10 内核复用：真实内核 L4DIR 指向被正确改写并复原
        try:
            k = load_l4_kernel()
            before = k.L4DIR
            scan = _run_audit(t1, days=3, end="2026-09-22", kernel=k)
            chk("T10 内核 L4DIR 复原", k.L4DIR == before, k.L4DIR)
            chk("T10 内核模式仍能识别会话成员",
                scan[2]["stats"]["l4_zip"]["zips"] and scan[2]["stats"]["l4_zip"]["zips"][0]["session_members"] == 2)
        except Exception as e:                                        # noqa: BLE001
            chk("T10 内核复用", False, repr(e))
        # T11 渲染自证：文本含语料计数行与空洞清单
        chk("T11 渲染含语料自证", "== 语料自证 ==" in out and "== 空洞" in out)
        md6 = render_md(res6)
        chk("T11 md 渲染含汇总表与空洞节", "| 指标 | 值 |" in md6 and "## 空洞清单" in md6)
        # T12 负控：滚动面（日志）强时间戳/回声一律不得授予覆盖
        t12 = _fixture(os.path.join(base, "t12"))
        with open(os.path.join(t12, "logs/echo.log"), "w") as f:
            f.write("2020-09-13 10:00:00 noise\n" * 5)
            f.write("2026-09-19 08:00:00 live\n" * 5)
        rc12, out12, res12 = _run_audit(t12, days=5, end="2026-09-22")
        chk("T12 sources 只可能是权威面（提示面永不入列）",
            all(not (set(r["sources"]) - set(AUTHORITATIVE_SOURCES)) for r in res12["per_day"]))
        r19b = [r for r in res12["per_day"] if r["day"] == "2026-09-19"][0]
        chk("T12 09-19 仅日志强时间戳 ⇒ 不判 COVERED，只进 hint",
            r19b["verdict"] != "COVERED" and "logs_live" in r19b["hint_sources"], r19b["verdict"])
        chk("T12 窗口外的 2020 回声日不入任何 sources",
            not any(r["day"] == "2020-09-13" for r in res12["per_day"]))
        chk("T12 covered 恒等于「有权威源的日数」",
            res12["summary"]["covered"] == sum(1 for r in res12["per_day"] if r["sources"]))
        # T13 结构性不变量：晚于归档边界的日一律 PENDING_ARCHIVE，且不计入 holes
        fr = res6["frontier"]
        later = [r for r in res6["per_day"] if fr and r["day"] > fr]
        chk("T13 frontier=%s 之后的日全判 PENDING_ARCHIVE" % fr,
            all(r["verdict"] == "PENDING_ARCHIVE" for r in later), [r["day"] for r in later])
        chk("T13 PENDING_ARCHIVE 不计入 holes",
            not (set(h["day"] for h in res6["holes"]) & set(r["day"] for r in later)))
        # T14 all_histories：SESSION 结构头授予覆盖，正文裸时间戳不授予
        t14 = _fixture(os.path.join(base, "t14"))
        ahp = os.path.join(t14, "memory/L4_raw_sessions/all_histories.txt")
        with open(ahp, "w") as f:
            f.write("SESSION: 0912_1000-0912_1010\n[USER]: hi\n2026-09-13 10:00:00\n")
        rc14, out14, res14 = _run_audit(t14, days=4, end="2026-09-14")
        r12b = [r for r in res14["per_day"] if r["day"] == "2026-09-12"][0]
        r13b = [r for r in res14["per_day"] if r["day"] == "2026-09-13"][0]
        chk("T14 SESSION 头授予权威覆盖", "all_histories" in r12b["sources"], r12b["sources"])
        chk("T14 头计数自证 session_headers=1",
            res14["stats"]["all_histories"]["session_headers"] == 1)
        chk("T14 正文裸时间戳不授予覆盖（该日 sources 不含 all_histories）",
            "all_histories" not in r13b["sources"], r13b["sources"])
        # T15 归档起点判据：空洞按「早于归档起点 / 归档面内真空洞」二分
        chk("T15 t1 归档起点=2026-09-20", res6.get("archive_start") == "2026-09-20",
            res6.get("archive_start"))
        bet = [h["day"] for h in res6["holes"] if h.get("before_archive_start")]
        ins = [h["day"] for h in res6["holes"] if not h.get("before_archive_start")]
        chk("T15 t1 早于起点=09-13..09-19（7 天）", bet == ["2026-09-%02d" % d for d in range(13, 20)], bet)
        chk("T15 t1 归档面内真空洞=09-23（提示面有、原文无）", ins == ["2026-09-23"], ins)
        chk("T15 汇总自证 before+inside == holes",
            res6["summary"]["holes_before_archive_start"] + res6["summary"]["holes_inside_archive"]
            == res6["summary"]["holes"])
        chk("T15 t3 无权威面 ⇒ archive_start=None 且未知全算真空洞（fail-visible）",
            res3.get("archive_start") is None
            and res3["summary"]["holes_inside_archive"] == len(res3["holes"])
            and all(not h.get("before_archive_start") for h in res3["holes"]))
        chk("T15 t1 文本标注「早于归档起点」且起点行在册",
            "归档起点（权威面最小日）: 2026-09-20" in out and "[早于归档起点]" in out)
        md15 = render_md(res6)
        chk("T15 md 含结论节与内外拆分行",
            "## 结论（由本函数机械生成，勿手改）" in md15
            and "其中早于归档起点" in md15 and "其中归档面内真空洞" in md15)
        print("selftest: %s" % ("PASS" if ok else "FAIL"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0 if ok else 2


def cmd_scan(a):
    corpus = Corpus(a.root)
    try:
        res = audit(corpus, days=a.days, end=a.end, kernel=None if a.no_kernel else load_l4_kernel())
    except CorpusError as e:
        print("CORPUS_ERROR: %s" % e, file=sys.stderr)
        return 2
    print(render(res))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print("\njson -> %s" % a.json)
    if a.md:
        os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(render_md(res) + "\n")
        print("md   -> %s" % a.md)
    return 1 if res["holes"] else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":          # 别名：兼容 --flag 风格调用
        argv = ["selftest"] + argv[1:]
    p = argparse.ArgumentParser(prog="l4_coverage_audit.py", description="L4 会话原文留存面覆盖度审计（只读）")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--days", type=int, default=WINDOW_DAYS_DEFAULT)
        sp.add_argument("--end", default=None, help="窗口末日 YYYY-MM-DD，默认今天")
        sp.add_argument("--root", default=None, help="语料根（默认仓库根；selftest 用夹具）")
        sp.add_argument("--no-kernel", action="store_true", help="不复用 l4_search 内核（回退 glob）")

    sp = sub.add_parser("scan"); add_common(sp)
    sp.add_argument("--json", default=None); sp.add_argument("--md", default=None)
    sp.set_defaults(fn=cmd_scan)
    sp = sub.add_parser("proof"); add_common(sp); sp.set_defaults(fn=cmd_proof)
    sp = sub.add_parser("selftest"); sp.set_defaults(fn=cmd_selftest)
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        a = p.parse_args(["scan"])
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
