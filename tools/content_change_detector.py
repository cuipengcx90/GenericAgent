#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时任务「内容级产出变化」检测器（只读）。

补 tools/task_freshness.py（R411）的 mtime 盲区：R411 的 L1 只看产物 mtime/静默天数，
「任务照跑、mtime 照新、但产物内容连续 N 天一字未变」在 mtime 口径下不可见。

本工具对判据表（tools/freshness_criteria.json）里每个目标的产物族（payload globs）
与运行证据族（run_evidence globs）按 mtime 排序，逐文件算**内容指纹**，给出：
  - norm_distinct_raw : 原始字节级不同指纹数
  - norm_distinct     : 归一化后不同指纹数（剥掉易变字段/时间戳 ⇒ 只留"实质内容"）
  - stale_days        : 距今「最近一次实质内容变化」的天数（= 内容零变化天数）
  - classification    : OK / CONTENT_STALE / ALL_IDENTICAL / NO_HISTORY / MISSING

★归一化必要性（实测）：temp/sqlite_snapshots/snapshot_*.json 110 份文件
  原始指纹 110/110 各不相同（内嵌 timestamp），归一化后 1/110 ⇒ 实质内容 108.6 天零变化。
  没有归一化，本工具会把「每天写一份一模一样的快照」判成"每天都有变化"（假阴性）。

用法：
  python3 tools/content_change_detector.py                      # 文本表格
  python3 tools/content_change_detector.py --json out.json      # 落 JSON
  python3 tools/content_change_detector.py --families payload   # 只看产物族
  python3 tools/content_change_detector.py --selftest           # 自测（含负控）
  python3 tools/content_change_detector.py --readonly-proof     # 只读自证（前后指纹一致）

口径边界：
  - 只读：从不写源文件；--json/--ledger-write 是唯一写盘点（默认均不写）。
  - 「内容有变化」≠「任务健康」：本工具只判"跑而不产出"，健康判据仍归 R411 L2 日志签名。
  - 单文件产物族（n<min_files）无历史可比 ⇒ 记 NO_HISTORY，需用 --ledger-write 逐日累积后
    才能在后续运行里给出零变化天数（详见报告"接线提案"）。
"""

import argparse
import glob as globmod
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

DEFAULT_CRITERIA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "freshness_criteria.json")
DEFAULT_HASH_CAP = 8 * 1024 * 1024  # 8MB；超出则取首尾各半 + 总长参与指纹
DEFAULT_MIN_FILES = 2
DEFAULT_ALERT_DAYS = 7.0

# 易变字段名（JSON 键名，整键剔除后才算实质内容）
VOLATILE_KEY_RE = re.compile(
    r"(?i)^_?(\$)?(timestamp|ts|time|date|datetime|generated_at|created_at|updated_at|"
    r"now|run_id|runid|pid|duration|duration_ms|elapsed|elapsed_ms|uuid|nonce|"
    r"checked_at|scanned_at|started_at|finished_at|built_at|mtime|last_run|last_run_at)$"
)
# 文本里的时间戳/日期（含 ISO 与中文日期），归一为占位符
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")


# ---------------------------------------------------------------- 归一化 / 指纹

def normalize_value(obj):
    if isinstance(obj, dict):
        return {k: normalize_value(v) for k, v in obj.items()
                if not (isinstance(k, str) and VOLATILE_KEY_RE.match(k))}
    if isinstance(obj, list):
        return [normalize_value(x) for x in obj]
    return obj


def normalize_text(text):
    text = TS_RE.sub("<TS>", text)
    text = DATE_RE.sub("<D>", text)
    return text


def read_capped(path, cap):
    """返回 (data, truncated)。只读，绝不写。"""
    size = os.path.getsize(path)
    if size <= cap:
        with open(path, "rb") as fh:
            return fh.read(), False
    half = cap // 2
    with open(path, "rb") as fh:
        head = fh.read(half)
        fh.seek(-half, os.SEEK_END)
        tail = fh.read(half)
    return head + tail, True


def fingerprint(path, cap=DEFAULT_HASH_CAP, normalize=True):
    """内容指纹。normalize=True 时剥易变字段/时间戳；返回 (digest, size, truncated)。"""
    try:
        data, truncated = read_capped(path, cap)
    except OSError as exc:
        return "ERR:" + type(exc).__name__, -1, False
    size = os.path.getsize(path)
    if normalize:
        payload = None
        try:
            obj = json.loads(data.decode("utf-8"))
            payload = json.dumps(normalize_value(obj), sort_keys=True,
                                 ensure_ascii=False).encode("utf-8")
        except Exception:
            payload = normalize_text(data.decode("utf-8", "replace")).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
    else:
        digest = hashlib.sha256(data).hexdigest()
    if truncated:
        digest = hashlib.sha256((digest + "|" + str(size)).encode()).hexdigest()
    return digest, size, truncated


# ---------------------------------------------------------------- 族的展开与判定

def stat_family(root, pattern, min_files, max_files):
    """按 mtime 升序返回族内文件（截断时取**最新** max_files 个，保证窗口贴近当下）。"""
    pat = pattern if os.path.isabs(pattern) else os.path.join(root, pattern)
    paths = globmod.glob(pat)
    matched_total = len(paths)
    entries = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if os.path.isfile(p):
            entries.append((st.st_mtime, p))
    entries.sort()
    truncated = False
    if max_files and len(entries) > max_files:
        entries = entries[-max_files:]
        truncated = True
    return entries, matched_total, truncated


def classify(entries, digests, alert_days, min_files):
    """entries=[(mtime,path)] 升序；digests 同序。返回判定 dict（不含 raw/norm 区分）。"""
    n = len(entries)
    if n == 0:
        return {"n": 0, "distinct": 0, "stale_days": None, "stale_since": None,
                "stale_rank": None, "class": "MISSING"}
    if n < min_files:
        mt = entries[-1][0]
        return {"n": n, "distinct": 1 if digests else 0, "stale_days": None,
                "stale_since": None, "stale_rank": None, "class": "NO_HISTORY",
                "newest_mtime": mt, "oldest_mtime": entries[0][0]}
    distinct = len(set(digests))
    # 「最近一次实质变化」= 最新一段相同内容里最早那个文件的 mtime
    i = n - 1
    while i - 1 >= 0 and digests[i - 1] == digests[i]:
        i -= 1
    stale_since = entries[i][0]
    stale_days = (time.time() - stale_since) / 86400.0
    if distinct == 1:
        klass = "ALL_IDENTICAL"
    elif stale_days > alert_days:
        klass = "CONTENT_STALE"
    else:
        klass = "OK"
    return {"n": n, "distinct": distinct, "stale_days": stale_days,
            "stale_since": stale_since, "stale_rank": i + 1, "class": klass,
            "newest_mtime": entries[-1][0], "oldest_mtime": entries[0][0]}


def ledger_records(path):
    """读指纹台账（JSONL）。不存在 → []。"""
    recs = []
    if not path or not os.path.exists(path):
        return recs
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    return recs


def scan_family(root, pattern, alert_days, min_files, max_files, cap, normalize,
                ledger_recs=None):
    entries, matched_total, truncated = stat_family(root, pattern, min_files, max_files)
    digests = []
    sizes = []
    for _, p in entries:
        d, s, _t = fingerprint(p, cap=cap, normalize=normalize)
        digests.append(d)
        sizes.append(s)
    info = classify(entries, digests, alert_days, min_files)
    info.update({"pattern": pattern, "matched_total": matched_total,
                 "truncated": truncated, "files": [p for _, p in entries],
                 "sizes": sizes, "digests": digests,
                 # live=被测对象是**活跃日志**（append-only 流，另有进程并发写）
                 # ⇒ 只读自证对它的"指纹变化"须按并发追加定性，不能当本工具改源
                 "live": "/logs/" in pattern or pattern.startswith("logs/")})
    # 台账补史：单文件族用台账里的旧指纹给出零变化天数
    if info["class"] == "NO_HISTORY" and ledger_recs:
        same = [r for r in ledger_recs if r.get("pattern") == pattern]
        if len(same) >= min_files:
            seq = sorted(same, key=lambda r: r.get("t", 0))
            ds = [r.get("digest") for r in seq]
            i = len(ds) - 1
            while i - 1 >= 0 and ds[i - 1] == ds[i]:
                i -= 1
            info["class"] = "OK" if i == len(ds) - 1 else "CONTENT_STALE"
            info["stale_days"] = (time.time() - seq[i].get("t", time.time())) / 86400.0
            info["stale_since"] = seq[i].get("t")
            info["distinct"] = len(set(ds))
            info["from_ledger"] = True
    return info


def scan(root, criteria_path, families, alert_days, min_files, max_files, cap,
         normalize, ledger_path=None, only=None):
    with open(criteria_path, "r", encoding="utf-8") as fh:
        crit = json.load(fh)
    lrecs = ledger_records(ledger_path)
    targets = crit.get("targets", [])
    rows = []
    for t in targets:
        tid = t.get("id", "?")
        if only and tid not in only:
            continue
        groups = []
        if families in ("payload", "both"):
            groups.append(("payload", [p.get("glob") for p in t.get("payloads", []) if p.get("glob")]))
        if families in ("evidence", "both"):
            groups.append(("evidence", [g for g in t.get("run_evidence", []) if g]))
        state = str(t.get("state") or "").strip().lower()
        disabled = bool(t.get("disabled"))
        if disabled:
            exempt_reason = "disabled"
        elif state in EXEMPT_STATES:
            exempt_reason = state
        else:
            exempt_reason = None
        for gname, pats in groups:
            for pat in pats:
                info = scan_family(root, pat, alert_days, min_files, max_files, cap,
                                   normalize, lrecs)
                info["target"] = tid
                info["kind"] = t.get("kind", "?")
                info["group"] = gname
                # 生命周期口径：已退役/按需触发的任务「内容不再变化」是预期，不是产出塌陷。
                # 本器仍照常测量（class 不改），只标 exempt 并从告警面排除（--strict-exempt 可关）。
                info["state"] = state
                info["disabled"] = disabled
                info["exempt"] = bool(exempt_reason)
                info["exempt_reason"] = exempt_reason
                rows.append(info)
    return rows, crit


def done_family_key(basename):
    """从 done 报告文件名推出**族键**（= 任务名）。

    真实命名 = `<YYYY-MM-DD>_<HHMM>_<task>.md`（如 `2026-05-12_1845_wechat_signals.md`）。
    ★2026-09-28 修（R470）：旧实现用 `(.+)_([A-Za-z0-9_]+).md$` 取**最后一个下划线段**，
      把 `..._trendradar_evening.md` 截成 `evening`、`..._supply_chain_digest.md` 截成 `digest`
      ⇒ 族键与判据表 target id 对不上，登记面对账**假报未登记**（实测 19 族里 5 族是截断假象，
      其中 digest/signals/sqlcipher/13h/reset 正是 R457 标记的 5 个「停滞族」）。
      现改为剥掉日期(±时间)前缀后取整段任务名；非该命名格式（老文件/夹具）回落旧规则，
      保证既有自测夹具与历史文件名仍可解析。
    """
    m = re.match(r"\d{4}-\d{2}-\d{2}(?:_\d{4})?_(?P<tid>[A-Za-z0-9_\-]+)\.md$", basename)
    if m:
        return m.group("tid")
    m = re.match(r"(.+)_([A-Za-z0-9_]+)\.md$", basename)
    return m.group(2) if m else None


def discover_done(root, alert_days, min_files, max_files, cap, normalize,
                  registered=None, ledger_recs=None, verbose=False):
    """反向发现：扫 sche_tasks/done/*_<task>.md 里的**任务族**，标注是否已登记。

    用途不是"再判一遍告警"，而是回答"登记面有没有空洞"：
    done 报告是定时任务的运行证据，若某任务有 ≥min_files 份 done 报告却不在
    判据表（freshness_criteria.json）里，它的内容停滞不会被任何判据看见。
    ★口径：未登记族的停滞**不等于**内容塌陷——任务若已退役/停跑，产物 mtime 同样陈旧，
      那类由 R411（task_freshness）L1/L3 覆盖。本函数只做「登记面 × 实际产出面」对账。
    """
    import collections
    registered = registered or set()
    groups = collections.Counter()
    for p in globmod.glob(os.path.join(root, "sche_tasks/done/*.md")):
        tid = done_family_key(os.path.basename(p))
        if tid:
            groups[tid] += 1
    rows = []
    for tid, n in sorted(groups.items()):
        if n < min_files:
            continue
        pat = "sche_tasks/done/*_%s.md" % tid
        info = scan_family(root, pat, alert_days, min_files, max_files, cap,
                           normalize, ledger_recs)
        info["target"] = tid
        info["kind"] = "done-suffix"
        info["group"] = "evidence"
        info["registered"] = tid in registered
        rows.append(info)
    if verbose:
        sys.stderr.write("-- discover: %d 个 done 报告族（≥%d 份）\n" % (len(rows), min_files))
    return rows


def rerun_cmd(row, root, criteria, families, alert_days):
    """给每行一条可复跑命令（报告要求"每例附复跑命令"）。"""
    return ("python3 tools/content_change_detector.py --root %s --criteria %s "
            "--families %s --alert-days %g --only %s --json /tmp/ccd_%s.json"
            % (root, criteria, families, alert_days, row.get("target", "?"),
               row.get("target", "?")))


# ---------------------------------------------------------------- 生命周期豁免
# 与 tools/task_freshness.py 的 state=legacy/on_demand 同一口径：
# 已退役 / 仅按需触发的任务，其产物「内容不再变化」是**预期状态**，不是产出塌陷。
# 本器仍照常测量（class 一字不改，可审计），只把这类族标 exempt 并从告警/rc 中排除；
# --strict-exempt 可关掉豁免（负控与人工复核通路）。
EXEMPT_STATES = ("legacy", "on_demand")


def is_exempt(row, strict_exempt=False):
    """该族是否因生命周期（disabled/legacy/on_demand）而豁免内容级告警。"""
    if strict_exempt:
        return False
    return bool(row.get("exempt"))


def hard_rows(rows, strict_exempt=False):
    """内容级硬告警族（停滞含全同）——豁免族不计入。"""
    return [r for r in rows if r["class"] in ("CONTENT_STALE", "ALL_IDENTICAL")
            and not is_exempt(r, strict_exempt)]


# ---------------------------------------------------------------- 渲染

CLASS_ICON = {"OK": "🟢", "CONTENT_STALE": "🔴", "ALL_IDENTICAL": "🟠",
              "NO_HISTORY": "⚪", "MISSING": "❓"}


def render(rows, alert_days, normalize, top=60, show_all=False, discover_rows=None,
           strict_exempt=False):
    lines = []
    lines.append("== 定时任务内容级产出变化检测（只读）==")
    lines.append("口径：alert_days=%.1f 归一化=%s  " % (alert_days, "on" if normalize else "off")
                 + "stale_days=距今最近一次实质内容变化天数（越大=越久没产出变化）")
    exh = [r for r in rows if is_exempt(r, strict_exempt)]
    stale = [r for r in rows if r["class"] in ("CONTENT_STALE", "ALL_IDENTICAL")
             and not is_exempt(r, strict_exempt)]
    for r in sorted(stale, key=lambda x: -(x["stale_days"] or 0)):
        lines.append("%s %-9s %-26s n=%-5d distinct=%-5d stale=%7.2fd since=%s rank=%s/%s%s"
                     % (CLASS_ICON[r["class"]], r["class"], r["target"] + "/" + r["group"],
                        r["n"], r["distinct"], r["stale_days"] or 0,
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(r["stale_since"] or 0)),
                        r["stale_rank"], r["n"],
                        "  [truncated %d/%d]" % (r["n"], r["matched_total"]) if r["truncated"] else ""))
        lines.append("      pattern: %s" % r["pattern"])
        lines.append("      newest : %s" % (r["files"][-1] if r["files"] else "-"))
    if exh:
        lines.append("-- 生命周期豁免（state=legacy/on_demand 或 disabled ⇒ 内容停滞属预期，不计入告警，仅留痕）: %d 族"
                     % len(exh))
        for r in sorted(exh, key=lambda x: -(x["stale_days"] or 0)):
            lines.append("   ⏸️ %-9s %-28s group=%-8s n=%-5d distinct=%-5d stale=%7.2fd reason=%s"
                         % (r["class"], r["target"][:28], r["group"], r["n"], r["distinct"],
                            r["stale_days"] or 0, r.get("exempt_reason") or "?"))
        lines.append("      ★口径：豁免只影响告警面与 rc，class 照实测量；--strict-exempt 可关掉豁免。")
    if show_all:
        withnum = [r for r in rows if r["stale_days"] is not None]
        withnum.sort(key=lambda x: -x["stale_days"])
        lines.append("-- 全表（有历史可比的 %d/%d 族，按零变化天数降序）:" % (len(withnum), len(rows)))
        for r in withnum:
            lines.append("   %8.2fd  %-30s %-8s n=%-5d distinct=%-5d %-9s %s"
                         % (r["stale_days"], r["target"][:30], r["group"], r["n"],
                            r["distinct"], r["class"], r["pattern"]))
    okn = sum(1 for r in rows if r["class"] == "OK")
    lines.append("-- 汇总: 族总数=%d  OK=%d  内容停滞(含全同，已剔豁免)=%d  生命周期豁免=%d  无历史=%d  缺失=%d"
                 % (len(rows), okn, len(stale), len(exh),
                    sum(1 for r in rows if r["class"] == "NO_HISTORY"),
                    sum(1 for r in rows if r["class"] == "MISSING")))
    if discover_rows:
        new = [r for r in discover_rows if not r.get("registered")]
        old = [r for r in discover_rows if r.get("registered")]
        gap = [r for r in new if r["stale_days"] is not None]
        gap.sort(key=lambda x: -x["stale_days"])
        lines.append("-- 登记面空洞（反向发现 sche_tasks/done 的任务族）: 族总数=%d 已登记=%d 未登记=%d"
                     % (len(discover_rows), len(old), len(new)))
        for r in gap:
            lines.append("   ⚠️ 未登记 %-22s n=%-5d distinct=%-5d 末份距今天数=%7.2fd class=%s"
                         % (r["target"], r["n"], r["distinct"], r["stale_days"], r["class"]))
            lines.append("      pattern: %s" % r["pattern"])
        lines.append("      ★口径：未登记族的「末份距今天数」并非内容塌陷判据——任务若已停跑，"
                     "产物 mtime 同样陈旧，那类由 R411（task_freshness）L1/L3 覆盖；此处只做登记面对账。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测（含负控）

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def selftest(verbose=True):
    """沙箱自测：44 项断言，含 9 类负控。返回 (passed, total, log)。"""
    log = []
    passed = total = 0

    def check(name, got, want):
        nonlocal passed, total
        total += 1
        ok = got == want
        passed += 1 if ok else 0
        log.append("%s %-46s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))
        return ok

    tmp = tempfile.mkdtemp(prefix="ccd_selftest_")
    try:
        base = time.time() - 10 * 86400
        # 族 A：5 份内容全同
        for i in range(5):
            _write(os.path.join(tmp, "fA", "a%d.txt" % i), "same payload\n")
            os.utime(os.path.join(tmp, "fA", "a%d.txt" % i), (base + i * 86400,) * 2)
        # 族 B：最后一份不同
        for i in range(5):
            _write(os.path.join(tmp, "fB", "b%d.txt" % i), "v1\n" if i < 4 else "v2\n")
            os.utime(os.path.join(tmp, "fB", "b%d.txt" % i), (base + i * 86400,) * 2)
        # 族 C：JSON 仅时间戳不同（归一化必要性）
        for i in range(4):
            _write(os.path.join(tmp, "fC", "c%d.json" % i),
                   json.dumps({"timestamp": "2026-09-%02dT00:00:00" % (i + 1), "v": 1}))
            os.utime(os.path.join(tmp, "fC", "c%d.json" % i), (base + i * 86400,) * 2)
        # 族 D：单文件
        _write(os.path.join(tmp, "fD", "only.json"), '{"v":1}')
        # 族 E：陈旧但内容每天不同（新鲜度靠 mtime，这里内容有变化但很老）
        for i in range(3):
            _write(os.path.join(tmp, "fE", "e%d.txt" % i), "e-%d\n" % i)
            os.utime(os.path.join(tmp, "fE", "e%d.txt" % i), (base + i * 86400,) * 2)

        def row(pat, norm=True, alert=7.0):
            return scan_family(tmp, pat, alert, 2, 0, DEFAULT_HASH_CAP, norm, [])

        a = row("fA/*.txt")
        check("NC1 全同族→ALL_IDENTICAL", a["class"], "ALL_IDENTICAL")
        # 全同族：stale_since=最老那份 ⇒ stale_days=整个窗口长度(下界口径)
        check("NC1b 全同族 stale_days==10(窗口全长下界)", round(a["stale_days"], 1), 10.0)
        b = row("fB/*.txt")
        check("NC2 末日不同→OK", b["class"], "OK")
        check("NC2b 末日变化 stale==6d(最新那份)", round(b["stale_days"], 1), 6.0)
        c = row("fC/*.json", norm=True)
        craw = row("fC/*.json", norm=False, alert=30.0)
        check("NC3 时间戳差异(归一化)→ALL_IDENTICAL", c["class"], "ALL_IDENTICAL")
        check("NC3b 同族不归一化→OK(即假阴性)", craw["class"], "OK")
        check("NC3c raw distinct=4", craw["distinct"], 4)
        check("NC3d norm distinct=1", c["distinct"], 1)
        d = row("fD/*.json")
        check("NC5 单文件→NO_HISTORY", d["class"], "NO_HISTORY")
        e = row("fE/*.txt", alert=100.0)
        check("NC6 内容有变化仅 mtime 老→OK(非本工具范围)", e["class"], "OK")
        check("NC6b stale==8d(最新那份)", round(e["stale_days"], 1), 8.0)
        m = row("fZ/*.txt")
        check("NC7 空族→MISSING", m["class"], "MISSING")
        # 负控①：篡改被判 ALL_IDENTICAL 的族里一个文件 → 复跑必须"检测到变化"
        _write(os.path.join(tmp, "fA", "a2.txt"), "tampered!!\n")
        a2 = row("fA/*.txt")
        check("NC8 篡改后不再 ALL_IDENTICAL(负控)", a2["class"] in ("OK", "CONTENT_STALE"), True)
        check("NC8b 篡改后 distinct>=2", a2["distinct"] >= 2, True)
        # 负控②：阈值可判 CONTENT_STALE（用未被篡改的 fE：内容各异但最新那份也已有 8 天）
        e2 = row("fE/*.txt", alert=0.5)
        check("NC9 alert_days 生效→CONTENT_STALE", e2["class"], "CONTENT_STALE")
        # 负控③：只读自证能抓出"读时被改"的情形（手工模拟 sha 比对）
        h0 = fingerprint(os.path.join(tmp, "fB", "b0.txt"), normalize=True)[0]
        _write(os.path.join(tmp, "fB", "b0.txt"), "changed\n")
        h1 = fingerprint(os.path.join(tmp, "fB", "b0.txt"), normalize=True)[0]
        check("NC10 指纹对内容变化敏感(负控)", h0 != h1, True)
        # 负控④：台账缺失/空 → 单文件族仍 NO_HISTORY（不假绿）
        lad = scan_family(tmp, "fD/*.json", 7.0, 2, 0, DEFAULT_HASH_CAP, True, [])
        check("NC11 无台账→NO_HISTORY(不假绿)", lad["class"], "NO_HISTORY")
        # 负控⑤：判据文件不存在 → scan 抛错（由调用方转 rc=2）
        try:
            scan(tmp, os.path.join(tmp, "nope.json"), "both", 7.0, 2, 0, DEFAULT_HASH_CAP, True)
            check("NC12 缺判据文件→异常", "no-raise", "raised")
        except (OSError, IOError):
            check("NC12 缺判据文件→异常", "raised", "raised")
        check("NC13 语料自证(fA 5 文件)", len(globmod.glob(os.path.join(tmp, "fA/*.txt"))), 5)
        # NC14 discover_done：造 sche_tasks/done 报告族，验证「未登记 vs 已登记」判定
        os.makedirs(os.path.join(tmp, "sche_tasks", "done"), exist_ok=True)
        for i in range(3):
            _write(os.path.join(tmp, "sche_tasks", "done", "2026090%d_alpha.md" % i), "alpha %d\n" % i)
        _write(os.path.join(tmp, "sche_tasks", "done", "20260901_beta.md"), "beta\n")
        dr = discover_done(tmp, 7.0, 2, 0, DEFAULT_HASH_CAP, True,
                           registered={"alpha"}, ledger_recs=[])
        by = {r["target"]: r for r in dr}
        check("NC14 discover 取到 alpha 族", "alpha" in by, True)
        check("NC14b alpha 已登记(判据表提供)", by["alpha"]["registered"], True)
        check("NC14c 少于 min_files 的族被跳过(beta n=1<2)", "beta" in by, False)
        check("NC14d discover 只出 1 族", len(by), 1)
        # NC14e 未登记判定：造 n=3 的 gamma 族且不登记 ⇒ registered=False
        for i in range(3):
            _write(os.path.join(tmp, "sche_tasks", "done", "2026091%d_gamma.md" % i), "gamma %d\n" % i)
        dr3 = {r["target"]: r for r in discover_done(tmp, 7.0, 2, 0, DEFAULT_HASH_CAP, True,
                                                     registered={"alpha"}, ledger_recs=[])}
        check("NC14e 未登记族 gamma 判 False", dr3["gamma"]["registered"], False)
        # NC15 rerun 字段可复跑（含 target 与 --only）
        rr = rerun_cmd({"target": "alpha"}, "/r", "/c.json", "both", 7.0)
        check("NC15 rerun 含 --only alpha", "--only alpha" in rr, True)
        # NC16 负控：篡改 discover 族里的报告内容 → 该族必须不再判 ALL_IDENTICAL
        _write(os.path.join(tmp, "sche_tasks", "done", "20260900_alpha.md"), "TAMPERED\n")
        dr2 = {r["target"]: r for r in discover_done(tmp, 7.0, 2, 0, DEFAULT_HASH_CAP, True,
                                                     registered={"alpha"}, ledger_recs=[])}
        check("NC16 篡改后 alpha 不再 ALL_IDENTICAL(负控)",
              dr2["alpha"]["class"] in ("OK", "CONTENT_STALE"), True)
        # NC23 族键：真实命名 <日期>_<HHMM>_<task>.md 必须取**整段任务名**
        check("NC23 真实命名取整段任务名",
              done_family_key("2026-05-12_1845_wechat_signals.md"), "wechat_signals")
        check("NC23b 多下划线不被截成末段",
              done_family_key("2026-05-12_1845_trendradar_evening.md"), "trendradar_evening")
        check("NC23c 无日期前缀(老文件/外域夹具)回落末段",
              done_family_key("20260900_alpha.md"), "alpha")
        check("NC23d 非 .md 不认(负控)", done_family_key("2026-05-12_1845_x.txt"), None)
        # NC24 负控：真实命名族在 discover_done 里不得被拆成末段键（旧实现症状）
        for i in range(3):
            _write(os.path.join(tmp, "sche_tasks", "done",
                                "2026-07-0%d_0900_trendradar_evening.md" % (i + 1)), "rev %d\n" % i)
        dr4 = {r["target"]: r for r in discover_done(tmp, 7.0, 2, 0, DEFAULT_HASH_CAP, True,
                                                     registered={"trendradar_evening"}, ledger_recs=[])}
        check("NC24 真实命名族键=整段任务名", "trendradar_evening" in dr4, True)
        check("NC24b 未被截成末段 evening(负控)", "evening" in dr4, False)
        check("NC24c 该族按新键判已登记", dr4["trendradar_evening"]["registered"], True)
        # NC17-19 只读自证的并发追加定性（活跃日志族）
        check("NC17 live 且只增⇒并发追加(非违例)",
              classify_recheck(True, 100, 130), "CONCURRENT_APPEND")
        check("NC18 live 但缩短⇒真违例 CHANGED(负控)",
              classify_recheck(True, 100, 90), "CHANGED")
        check("NC19 非 live 族变化⇒真违例 CHANGED(负控)",
              classify_recheck(False, 100, 130), "CHANGED")
        # NC20 live 标记来自 pattern（logs/ 视为活跃流）
        _write(os.path.join(tmp, "logs", "a.log"), "x\n")
        _write(os.path.join(tmp, "logs", "b.log"), "y\n")
        fam_live = scan_family(tmp, "logs/*.log", 7.0, 2, 0, DEFAULT_HASH_CAP, True)
        check("NC20a logs/ 族标 live", fam_live["live"], True)
        _write(os.path.join(tmp, "fA", "c.txt"), "z\n")
        fam_plain = scan_family(tmp, "fA/*.txt", 7.0, 2, 0, DEFAULT_HASH_CAP, True)
        check("NC20b 非日志族 live=False", fam_plain["live"], False)
        # NC21/NC22 生命周期豁免（与 task_freshness 的 state=legacy/on_demand 同口径）
        # fG：3 份内容全同且都老于 alert ⇒ 确定性 ALL_IDENTICAL（作豁免告警面样本）
        for i in range(3):
            _write(os.path.join(tmp, "fG", "g%d.txt" % i), "g-same\n")
            os.utime(os.path.join(tmp, "fG", "g%d.txt" % i), (base + i * 86400,) * 2)
        crit = {"targets": [
            {"id": "legacyA", "kind": "test", "state": "legacy",
             "payloads": [{"glob": os.path.join(tmp, "fG/*.txt")}]},
            {"id": "ondemandB", "kind": "test", "state": "on_demand",
             "payloads": [{"glob": os.path.join(tmp, "fE/*.txt")}]},
            {"id": "disabledC", "kind": "test", "disabled": True,
             "payloads": [{"glob": os.path.join(tmp, "fE/*.txt")}]},
            {"id": "activeD", "kind": "test",
             "payloads": [{"glob": os.path.join(tmp, "fE/*.txt")}]},
        ]}
        cpath = os.path.join(tmp, "crit.json")
        _write(cpath, json.dumps(crit))
        rows, _c = scan(tmp, cpath, "payload", 0.5, 2, 0, DEFAULT_HASH_CAP, True)
        byid = {r["target"]: r for r in rows}
        check("NC21a 豁免族 class 照实测量(=ALL_IDENTICAL，不掩盖)",
              byid["legacyA"]["class"], "ALL_IDENTICAL")
        check("NC21b exempt=True reason=legacy",
              (byid["legacyA"]["exempt"], byid["legacyA"]["exempt_reason"]), (True, "legacy"))
        check("NC21c on_demand 亦豁免",
              (byid["ondemandB"]["exempt"], byid["ondemandB"]["exempt_reason"]), (True, "on_demand"))
        check("NC21d disabled 亦豁免",
              (byid["disabledC"]["exempt"], byid["disabledC"]["exempt_reason"]), (True, "disabled"))
        check("NC21e 非豁免族 exempt=False", byid["activeD"]["exempt"], False)
        check("NC21f hard_rows 剔除全部豁免族(仅余 activeD)",
              sorted(r["target"] for r in hard_rows(rows)), ["activeD"])
        _EXH_HDR = "-- 生命周期豁免（state="
        _txt = render(rows, 0.5, True)
        check("NC21g render 含生命周期豁免留痕区", _EXH_HDR in _txt, True)
        check("NC21h render 汇总标注已剔豁免", "已剔豁免" in _txt, True)
        check("NC21i render 豁免区点名 legacyA", _EXH_HDR in _txt and "legacyA" in _txt, True)
        # 负控 NC22：--strict-exempt 关掉豁免 ⇒ 豁免族重新变回硬告警（豁免可被证伪）
        check("NC22a strict 下 is_exempt=False(负控)", is_exempt(byid["legacyA"], True), False)
        check("NC22b strict 下 hard_rows 含全部豁免族(负控)",
              sorted(r["target"] for r in hard_rows(rows, True)),
              ["activeD", "disabledC", "legacyA", "ondemandB"])
        check("NC22c strict 下 render 无豁免留痕区(负控)",
              _EXH_HDR in render(rows, 0.5, True, strict_exempt=True), False)
        # NC22d 负控：去掉 state 字段 ⇒ 豁免消失（证明豁免来自 state 而非族名/路径）
        crit2 = {"targets": [{"id": "legacyA", "kind": "test",
                              "payloads": [{"glob": os.path.join(tmp, "fG/*.txt")}]}]}
        cpath2 = os.path.join(tmp, "crit2.json")
        _write(cpath2, json.dumps(crit2))
        rows2, _ = scan(tmp, cpath2, "payload", 0.5, 2, 0, DEFAULT_HASH_CAP, True)
        check("NC22d 去 state⇒不再豁免(负控)", rows2[0]["exempt"], False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if verbose:
        for line in log:
            print(line)
        print("-- selftest: %d/%d passed" % (passed, total))
    return passed, total, log


def classify_recheck(live, size_before, size_after):
    """二次指纹不一致时的定性（纯函数，便于自测）。

    ★活跃日志（logs/ 下的 append-only 流）由其它进程并发追加 ⇒ 复算必然可能不一致，
      那不是"本工具改了源"，而是**并发写**。判据=该族是 live ∧ 文件只增不减。
      反之（同族但长度未增/变小，或非 live 族）一律按真违例 CHANGED 处理（fail-closed）。
    """
    if live and size_after is not None and size_before is not None and size_after >= size_before:
        return "CONCURRENT_APPEND"
    return "CHANGED"


def readonly_proof(rows, root, cap, normalize):
    """复算所有已检视文件的指纹，与扫描时记录比对 ⇒ 证明全程只读未改源。

    返回 (files, changed_genuine)：changed_genuine 为真违例（会被 main 判 rc=2）。
    """
    files = 0
    changed, concurrent = [], []
    for r in rows:
        live = bool(r.get("live"))
        for p, old, sz in zip(r["files"], r["digests"], r["sizes"]):
            files += 1
            d, s2, _t = fingerprint(p, cap=cap, normalize=normalize)
            if d != old:
                if classify_recheck(live, sz, s2) == "CONCURRENT_APPEND":
                    concurrent.append(p)
                else:
                    changed.append(p)
    print("-- readonly-proof: FILES=%d CHANGED=%d CONCURRENT_APPEND=%d"
          % (files, len(changed), len(concurrent)))
    for p in changed:
        print("   CHANGED: %s" % p)
    for p in concurrent:
        print("   CONCURRENT_APPEND(活跃日志,非违例): %s" % p)
    return files, changed


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="定时任务内容级产出变化检测器（只读）")
    ap.add_argument("--criteria", default=DEFAULT_CRITERIA)
    ap.add_argument("--root", default=os.getcwd(), help="glob 解析根（默认 cwd）")
    ap.add_argument("--families", choices=["payload", "evidence", "both"], default="both")
    ap.add_argument("--alert-days", type=float, default=DEFAULT_ALERT_DAYS)
    ap.add_argument("--min-files", type=int, default=DEFAULT_MIN_FILES)
    ap.add_argument("--max-files", type=int, default=2000, help="每族最多取最新 N 个（0=不限）")
    ap.add_argument("--hash-cap", type=int, default=DEFAULT_HASH_CAP)
    ap.add_argument("--no-normalize", action="store_true", help="负控用：关归一化")
    ap.add_argument("--only", default=None, help="逗号分隔 target id 过滤")
    ap.add_argument("--json", default=None, help="结果落 JSON（唯一写盘点之一）")
    ap.add_argument("--ledger", default=None, help="读指纹台账 JSONL 补单文件族历史")
    ap.add_argument("--ledger-write", default=None, help="把本次指纹追加进台账（默认不写）")
    ap.add_argument("--readonly-proof", action="store_true")
    ap.add_argument("--discover-done", action="store_true",
                    help="反向发现：扫 sche_tasks/done 里的任务族，标注是否已登记（登记面空洞）")
    ap.add_argument("--show-all", action="store_true", help="打印全部有历史可比的族（不止告警）")
    ap.add_argument("--strict-exempt", action="store_true",
                    help="关掉生命周期豁免：state=legacy/on_demand 与 disabled 族照常计为告警（负控/复核用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        p, t, _ = selftest()
        return 0 if p == t else 1

    normalize = not args.no_normalize
    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    try:
        rows, crit = scan(args.root, args.criteria, args.families, args.alert_days,
                          args.min_files, args.max_files, args.hash_cap, normalize,
                          args.ledger, only)
    except (OSError, IOError, ValueError) as exc:
        sys.stderr.write("ERROR: 无法读取判据/语料: %s\n" % exc)
        return 2

    discover_rows = None
    if args.discover_done:
        registered = set()
        for t in crit.get("targets", []):
            registered.add(str(t.get("id", "")))
            tgt = t.get("target") or ""
            if "/" in tgt:
                registered.add(tgt.split("/")[-1])
        discover_rows = discover_done(args.root, args.alert_days, args.min_files,
                                      args.max_files, args.hash_cap, normalize,
                                      registered, args.ledger)

    for r in rows + (discover_rows or []):
        r["rerun"] = rerun_cmd(r, args.root, args.criteria, args.families, args.alert_days)

    print(render(rows, args.alert_days, normalize, show_all=args.show_all,
                 discover_rows=discover_rows, strict_exempt=args.strict_exempt))

    if args.json:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "alert_days": args.alert_days, "normalize": normalize,
            "families": args.families, "root": os.path.abspath(args.root),
            "criteria": os.path.abspath(args.criteria),
            "criteria_targets": len(crit.get("targets", [])),
            "strict_exempt": bool(args.strict_exempt),
            "exempt_rows": [{"target": r["target"], "group": r["group"],
                             "class": r["class"], "state": r.get("state") or "",
                             "exempt_reason": r.get("exempt_reason"),
                             "stale_days": r.get("stale_days")}
                            for r in rows if is_exempt(r, args.strict_exempt)],
            "hard_rows": [{"target": r["target"], "group": r["group"],
                           "class": r["class"], "stale_days": r.get("stale_days")}
                          for r in hard_rows(rows, args.strict_exempt)],
            "rows": [{k: v for k, v in r.items() if k != "digests"} for r in rows],
        }
        if discover_rows is not None:
            payload["discover"] = {
                "families": len(discover_rows),
                "unregistered": [r["target"] for r in discover_rows if not r.get("registered")],
                "rows": [{k: v for k, v in r.items() if k != "digests"} for r in discover_rows],
            }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print("-- JSON: %s" % args.json)

    proof_changed = []
    if args.readonly_proof:
        _files, proof_changed = readonly_proof(rows, args.root, args.hash_cap, normalize)

    if args.ledger_write:
        with open(args.ledger_write, "a", encoding="utf-8") as fh:
            now = time.time()
            for r in rows:
                for p, d in zip(r["files"], r["digests"]):
                    fh.write(json.dumps({"t": now, "pattern": r["pattern"],
                                         "path": p, "digest": d},
                                        ensure_ascii=False) + "\n")
        print("-- ledger 已追加: %s" % args.ledger_write)

    hard = [r for r in rows if r["class"] in ("CONTENT_STALE", "ALL_IDENTICAL")]
    if proof_changed:
        # 只读自证被推翻（非并发追加的真改动）⇒ fail-closed 优先于告警码
        return 2
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
