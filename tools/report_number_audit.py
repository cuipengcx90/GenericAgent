#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_number_audit.py — 自主报告「数值断言」可核查闸门（TODO 行12，只读）

与既有断言族互补：`tools/report_assert_audit.py`（报告内路径断言，R378/R379）、
`tools/memory_assert_audit.py`（L1/L2 记忆断言）只覆盖「路径」。RULES#21 的
「报告里的实测数值须当场贴原始输出」此前全靠人肉（R375 把物证 mtime 写成 09-23、
真值 09-20 就是靠回读才发现）。本工具只做**数值**。

## 口径（评审强制的六条收窄条件，逐条对应实现）

① 比对优先级 = **冻结证据一致性**（报告里的主张 vs 同一报告内粘贴的原始输出）
   > **报告内部一致性**（同一报告的派生比例/单位冲突）
   > **现场复算**（仅限静止可复算类：sha1/sha256、字节数、行数）。
② `NEVER` 白名单**先定死**（`--list-never` 可打印）：时间戳/mtime、耗时、进程内存/负载、
   外部站点与网络数据、增长型累计计数 —— 命中即整行 skip（只计 skipped，绝不报警）。
   白名单故意取宽：放宽只损失覆盖率，不会产生假警报。
③ 真阳性样本 = 静止可复算类（selftest 用真实冻结文件 + 合成漂移语料做回归）；
   对**真归档只要求 0 假警报**（`--selftest` 内直接断言）。
④ **不执行任意 shell**：本工具**零 subprocess**，所有"复算"都是用 Python 内置
   `os.path.getsize` / 数字节 / `hashlib` 现算 ⇒ "命令白名单"在结构上无可注入面
   （selftest 断言源码里不出现 `subprocess`）。
⑤ 只读：对全部被扫描的报告文件做 sha1 前后比对，任何字节变化即 rc=3。
⑥ 归一化复用 history_value 教训：NFKC + 去空白 + 去千分位 `,`/`_` + 统一
   `=`/`：`/全角括号 ⇒ `99 / 341` 与 `99/341`、`1,211,565,564 B` 与 `1211565564B` 等价。

## 断言语料
`memory/autonomous_reports/R*.md`（durable）+ `temp/autonomous_reports/R*.md`（同名以 memory 侧为准）；
`--files GLOB` 可只扫指定文件；`--incl-index` 附带扫 `INDEX.md`/`history.md`/`history.txt`。
`--frozen GLOB` 指定「静止可复算类」现场复算目标（默认=两侧归档报告自身）。

## 输出与退出码
文本或 `--json <path>`；rc=0 无报警 / 1 有报警 / 2 用法错 / 3 检测到写入。
"""
import argparse
import glob as globmod
import hashlib
import json
import os
import re
import sys
import unicodedata

REPO = os.environ.get("GA_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_D = os.path.join(REPO, "memory/autonomous_reports")
TEMP_REPORTS_D = os.path.join(REPO, "temp/autonomous_reports")
FIXTURE_D = os.path.join(REPO, "temp/number_audit_fixture")
RNUM = re.compile(r"^R\d+")
DEFAULT_FROZEN = [
    os.path.join(REPORTS_D, "R*.md"),
    os.path.join(TEMP_REPORTS_D, "R*.md"),
]

# ── ② 永不自动比对白名单（先定死；放宽只损失覆盖率，不产生假警报）──────────────
NEVER_PATTERNS = [
    ("时间戳/日期", r"mtime|ctime|时间戳|日期|发布时间|截至|始于|结束于|\bdate\b|\d{4}-\d{2}-\d{2}"),
    ("耗时/时变", r"耗时|用时|elapsed|等待|超时|秒|ms\b|毫秒|分钟|小时|\bsleep\b|\btimeout\b"),
    ("进程/内存/负载", r"\bpid\b|RSS|phys_footprint|内存|占用|负载|load\s*avg|CPU|线程|文件描述符|\bfd\b|QoS"),
    ("外部/网络数据", r"站点|网页|URL|接口|响应|下载|上传|网络|HTTP|HTTP|限流|额度|配额|token|API|\bqps\b"),
    ("增长型计数", r"累计|迄今|历史总|自\s*20\d{2}|since|总量|总览|至今"),
]
NEVER_RE = [re.compile(p, re.I) for _, p in NEVER_PATTERNS]

# ── 可复算键（canon）与单位 ────────────────────────────────────────────────
# 只有这些键参与「内部/冻结一致性」与「现场复算」；其余键一律只统计不判定。
KEY_PAT = [
    ("lines", re.compile(r"^(行数|行|lines?|line_count|总行数)$")),
    ("bytes", re.compile(r"^(字节|字节数|大小|体积|bytes?|size|b|kb|mb|gb)$")),
    ("sha1", re.compile(r"^(sha1|sha-1)$")),
    ("sha256", re.compile(r"^(sha256|sha-256)$")),
    ("md5", re.compile(r"^(md5)$")),
]
NUM = r"(?:\d{1,3}(?:[,_]\d{3})+|\d+(?:\.\d+)?)"
UNIT = r"(B|KB|MB|GB|KiB|MiB|GiB|bytes|字节|行|条|%|ms|s)"
# 键必须**锚定**为别名（不能用「前面任意汉字串」当键：'本文件行数 372' 会把
# '本文件行数' 整个吃成键 ⇒ 一个 claim 都抽不到，实测踩到）
KEY_ALTS = (r"行数|总行数|行数上限|line_count|lines?|字节数|字节|大小|体积|bytes?|size"
            r"|sha1|sha-1|sha256|sha-256|md5")
CLAIM_KEY_RE = re.compile(
    rf"(?P<key>{KEY_ALTS})\s*[:：=＝]?\s*(?P<num>{NUM})\s*(?P<unit>{UNIT})?", re.I)
# 裸「数字+单位」：8446B / 9.5KB / 372 行（RE_B）
BARE_UNIT_RE = re.compile(rf"(?P<num>{NUM})\s*(?P<unit>B|KB|MB|GB|KiB|MiB|GiB|字节|行)\b")
BARE_UNIT_KEY = {"b": "bytes", "kb": "bytes", "mb": "bytes", "gb": "bytes",
                 "kib": "bytes", "mib": "bytes", "gib": "bytes",
                 "bytes": "bytes", "字节": "bytes", "行": "lines"}
SHA_RE = re.compile(r"(?P<key>sha1|sha-1|sha256|sha-256|md5)\D{0,8}(?P<hex>[0-9a-fA-F]{7,64})", re.I)
# 比例只认「紧邻」两种写法：`a/b … p%`（间隔 ≤4 个非数字非栅格字符）与 `p%（a/b）`。
# 间隔里禁止 `|`、`)`/`）`：表格相邻单元格会被缝成假比例（实测 R204
# `| 10%（1/10） | 100%（10/10） |` 的第1格曾与第2格缝成「1/10=100%」假警报）。
RATIO_AFTER_RE = re.compile(r"(?P<a>\d{1,7})\s*/\s*(?P<b>\d{1,7})(?!\.?\d)[^\d%|】】:：，,、\n]{0,4}?(?P<p>\d+(?:\.\d+)?)\s*%")
RATIO_BEFORE_RE = re.compile(r"(?P<p>\d+(?:\.\d+)?)\s*%\s*[（(]\s*(?P<a>\d{1,7})\s*/\s*(?P<b>\d{1,7})")
EXTS = (".md", ".py", ".json", ".jsonl", ".txt", ".sh", ".swift", ".log", ".yaml", ".yml", ".toml", ".csv")
PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])((?:[\w.@+-]+/)*[\w.@+-]+\.(?:" + "|".join(e[1:] for e in EXTS) + r"))(?![A-Za-z0-9_/])")
CODE_FENCE = "```"


def nfkc(s):
    return unicodedata.normalize("NFKC", s)


def norm_num(s):
    """归一化数字：去空白/千分位。"""
    return re.sub(r"[\s,_]", "", nfkc(s))


def canon_key(raw):
    k = nfkc(raw or "").strip().lower()
    for canon, pat in KEY_PAT:
        if pat.match(k):
            return canon
    return None


def to_bytes(num, unit):
    v = float(norm_num(num))
    u = nfkc(unit or "").lower()
    if u in ("kb", "kib"):
        return v * (1024 if u == "kib" else 1000)
    if u in ("mb", "mib"):
        return v * (1024 ** 2 if u == "mib" else 1000 ** 2)
    if u in ("gb", "gib"):
        return v * (1024 ** 3 if u == "gib" else 1000 ** 3)
    return v


def same_value(key, a, b):
    """同键两值是否一致（按键类型给容差）。"""
    (n1, u1), (n2, u2) = a, b
    if key == "bytes":
        v1, v2 = to_bytes(n1, u1), to_bytes(n2, u2)
        return abs(v1 - v2) <= max(1.0, 0.02 * max(v1, v2))  # 1000/1024 双口径容差 2%
    if key in ("sha1", "sha256", "md5"):
        return norm_num(n1).lower().startswith(norm_num(n2).lower()) or \
               norm_num(n2).lower().startswith(norm_num(n1).lower())
    return norm_num(n1) == norm_num(n2)


def never_hit(line):
    for name, pat in zip([n for n, _ in NEVER_PATTERNS], NEVER_RE):
        if pat.search(line):
            return name
    return None


def bare_b_ambiguous(m):
    """裸 `B` 太小写不出尺寸：`1.5B` / `3B` / `[3B  ]` 都是**模型参数量名**（R360 表格
    "1.5B + 现状 prompt"），当字节断言会与同段别处配对成假警报。规则=裸 B 只认
    ≥3 位整数字节数（8446B/300B），小数或 1-2 位一律不抽。"""
    if m.group("unit").lower() != "b":
        return False
    num = m.group("num")
    return ("." in num) or len(num) < 3


def bracket_wrapped(line, m):
    """`[1.5B]` / `[3B  ]` 这类方括号 token 是**标签**（模型参数量名），不是尺寸断言。"""
    pre = line[:m.start()].rstrip()
    post = line[m.end():].lstrip()
    if pre.endswith(("[", "(", "（")) or post.startswith(("]", ")", "）")):
        return True
    return False


def context_label(line, span):
    """「上下文本签名」= 把**本数值自身**遮蔽后的整行文本（剥排版符/空白、小写）。

    为什么不是"数字前的短标签"：真归档 6+16 处假警报全是**不同实体共用前缀**
    （表格一行多列 `| f.txt | 34 | 267KB | $1.36 | 6.5KB |`、代码块里不同服务
    `--log logs/feishu_agent.log --force  # 回收 ~368MiB` vs `scheduler_reflect.log`、
    `原文件`/`新文件`、`自证: bytes=... chars=...`、T0 备份清单各 zip 行），
    只取前缀或紧邻 8 字都会把它们并成一条。遮蔽本数值后，"复制件的漂移"
    （同一句话抄两份、只改一处数字）签名必然相同，而上面这些不同实体签名必然不同。"""
    a, b = span
    masked = line[:a] + "#" + line[b:]
    lab = re.sub(r"[\s*`_>|#\[\]()（）]+", "", masked).lower()
    return lab[:160]


def subject_of(line, heading, path_tok):
    if path_tok:
        return path_tok
    if heading:
        return heading[:60]
    return None


def scan_text(text, fname="<mem>"):
    """扫描一份报告文本，返回 claims/ratios/skips/alarms（不含现场复算）。"""
    lines = text.split("\n")
    claims, ratios, skips = [], [], []
    alarms = []
    heading, in_code = "", False
    for i, raw in enumerate(lines, 1):
        line = raw
        if line.strip().startswith(CODE_FENCE):
            in_code = not in_code
            continue
        if line.startswith("#"):
            heading = re.sub(r"^#+\s*", "", line).strip()
        nv = never_hit(line)
        if nv:
            skips.append({"file": fname, "line": i, "reason": nv})
            continue
        path_tok = None
        m = PATH_RE.search(line)
        if m:
            path_tok = m.group(1)
        def mk_claim(key, num, unit, span, code):
            """主体 = 路径/标题 + 「上下文本签名」（遮蔽本数值后的整行）。"""
            lab = context_label(line, span)
            base = subject_of(line, heading, path_tok) or "«报告级»"
            return {
                "file": fname, "line": i, "key": key, "num": num, "unit": unit,
                "subject": base + ("::" + lab if lab else ""), "path": path_tok,
                "subject_src": "path" if path_tok else ("heading" if heading else "report"),
                "code": code, "text": line.strip()[:120],
            }
        claimed_spans = []
        for m in CLAIM_KEY_RE.finditer(line):
            canon = canon_key(m.group("key"))
            if not canon:
                continue
            claimed_spans.append(m.span())
            num_span = (m.start("num"), m.end("unit") if m.group("unit") else m.end("num"))
            claims.append(mk_claim(canon, m.group("num"), m.group("unit") or "",
                                   num_span, in_code))
        # 裸「数字+单位」：8446B / 9.5KB / 372 行（键由单位反推；与上面的键式重叠时跳过）
        for m in BARE_UNIT_RE.finditer(line):
            if any(s <= m.start() < e for s, e in claimed_spans):
                continue
            if bracket_wrapped(line, m):
                continue
            if bare_b_ambiguous(m):
                continue
            key = BARE_UNIT_KEY.get(m.group("unit").lower())
            if not key:
                continue
            claimed_spans.append(m.span())
            claims.append(mk_claim(key, m.group("num"), m.group("unit") or "",
                                   m.span(), in_code))
        for m in SHA_RE.finditer(line):
            claims.append(mk_claim(canon_key(m.group("key")), m.group("hex"), "",
                                   m.span("hex"), in_code))
        for rgx in (RATIO_AFTER_RE, RATIO_BEFORE_RE):
            for m in rgx.finditer(line):
                try:
                    a, b, p = int(m.group("a")), int(m.group("b")), float(m.group("p"))
                except ValueError:
                    continue
                if b == 0:
                    continue
                ratios.append({"file": fname, "line": i, "a": a, "b": b, "p": p, "ps": m.group("p"),
                               "approx": any(ch in line for ch in ("≈", "约", "~", "大约")),
                               "calc": round(100.0 * a / b, 2), "text": line.strip()[:120]})
    # ── ①a 冻结证据 / 内部一致性：同 (subject,key) 多值 ──
    buckets = {}
    for c in claims:
        buckets.setdefault((c["subject"], c["key"]), []).append(c)
    for (subj, key), items in buckets.items():
        if len(items) < 2:
            continue
        for x in items:
            for y in items:
                if x["line"] >= y["line"]:
                    continue
                if same_value(key, (x["num"], x["unit"]), (y["num"], y["unit"])):
                    continue
                kind = "FROZEN" if (x["code"] != y["code"]) else "INTERNAL"
                alarms.append({
                    "kind": kind, "file": x["file"], "line": x["line"], "also_line": y["line"],
                    "msg": f"{subj} 的「{key}」两值不一致: 行{x['line']}={x['num']}{x['unit']} vs "
                           f"行{y['line']}={y['num']}{y['unit']}"
                           + ("（一处在代码块=粘贴的原始输出，一处在正文）" if kind == "FROZEN" else ""),
                })
    # ── ①b 派生比例自洽 ──
    for r in ratios:
        # 容差按报告显示的精度给（25%（99/390）是合法的整数百分比舍入；`≈/约` 再放宽半格）
        dec = len(r["ps"].split(".")[1]) if "." in r["ps"] else 0
        tol = max(3.0, 0.5 * (10 ** -dec) + 0.05 + (0.5 if r["approx"] else 0.0))
        if abs(r["calc"] - r["p"]) > tol:
            alarms.append({"kind": "RATIO", "file": r["file"], "line": r["line"],
                           "msg": f"{r['a']}/{r['b']} 应为 {r['calc']}%，报告写 {r['p']}%（容差±{round(tol, 2)}）"})
    return claims, ratios, skips, alarms


def frozen_set(globs):
    out = {}
    for g in globs:
        for p in globmod.glob(g):
            if os.path.isfile(p):
                out[os.path.abspath(p)] = p
    return out


def stat_check(claims, frozen, root):
    """现场复算（静止可复算类）：只对 frozen 集合内的路径算 字节/行数/sha1/sha256。"""
    out = []
    for c in claims:
        if c["key"] in ("bytes", "lines", "sha1", "sha256", "md5") and c.get("path"):
            cand = c["path"]
            cands = [cand] if os.path.isabs(cand) else [os.path.join(REPO, cand), os.path.join(root, cand)]
            hit = None
            for p in cands:
                ap = os.path.abspath(p)
                if ap in frozen:
                    hit = ap
                    break
            if not hit:
                continue
            raw = open(hit, "rb").read()
            if c["key"] == "bytes":
                real, shown = str(len(raw)), f"{c['num']}{c['unit']}"
                ok = same_value("bytes", (str(len(raw)), ""), (c["num"], c["unit"]))
            elif c["key"] == "lines":
                real, shown = str(raw.count(b"\n")), f"{c['num']}{c['unit']}"
                ok = norm_num(c["num"]) == str(raw.count(b"\n"))
            else:
                alg = c["key"] if c["key"] != "sha256" else "sha256"
                real = hashlib.new(alg, raw).hexdigest()
                shown = c["num"]
                ok = real.startswith(shown.lower()) or shown.lower().startswith(real[:len(shown)])
            if not ok:
                out.append({"kind": "STAT", "file": c["file"], "line": c["line"],
                            "msg": f"现场复算不符: {os.path.relpath(hit, REPO)} 的 {c['key']} 实测={real}，报告写 {shown}"})
    return out


def files_to_scan(files=None, incl_index=False):
    seen, out = set(), []
    for d in (REPORTS_D, TEMP_REPORTS_D):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f in seen:
                continue
            if incl_index and f in ("INDEX.md", "history.md", "history.txt"):
                seen.add(f); out.append(os.path.join(d, f)); continue
            if f.endswith(".md") and RNUM.match(f):
                seen.add(f); out.append(os.path.join(d, f))
    if files:
        out = []
        for g in files:
            out.extend(sorted(p for p in globmod.glob(g) if os.path.isfile(p)))
    return out


def sha1_of(p):
    return hashlib.sha1(open(p, "rb").read()).hexdigest()


def run(paths, frozen_globs, root):
    before = {p: sha1_of(p) for p in paths}
    frozen = frozen_set(frozen_globs)
    claims, ratios, skips, alarms = [], [], [], []
    for p in paths:
        text = open(p, encoding="utf-8", errors="replace").read()
        c, r, s, a = scan_text(text, os.path.relpath(p, REPO))
        claims += c; ratios += r; skips += s; alarms += a
    alarms += stat_check(claims, frozen, root)
    after = {p: sha1_of(p) for p in paths}
    written = [p for p in before if before[p] != after[p]]
    stats = {
        "files": len(paths), "claims": len(claims), "canon_claims":
            sum(1 for c in claims if c["key"] in ("bytes", "lines", "sha1", "sha256", "md5")),
        "ratio_pairs": len(ratios), "skipped_never": len(skips),
        "skip_reasons": {n: sum(1 for s in skips if s["reason"] == n) for n in [x for x, _ in NEVER_PATTERNS]},
        "frozen_files": len(frozen), "alarms": len(alarms),
        "alarm_kinds": {k: sum(1 for a in alarms if a["kind"] == k) for k in ("FROZEN", "INTERNAL", "RATIO", "STAT")},
        "written_detected": written,
    }
    return stats, alarms


# ── 自测（含负控）────────────────────────────────────────────────────────
def _mk(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def selftest():
    ok, fails = [], []
    def chk(name, cond, extra=""):
        (ok if cond else fails).append(f"{name}{(' :: ' + str(extra)) if extra else ''}")

    # 负控 0：源码里不得出现 subprocess（条件④的结构性证明）
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    chk("negctl-无 shell: 源码不含 subprocess 导入/调用",
        not re.search(r"(^|\n)\s*(import|from)\s+subprocess|subprocess\.\w+\(", src))

    (ok_f, drift_f, ratio_f, unit_f, never_f, norm_f,
     prefix_f, bracket_f, rbound_f) = (
        _mk(os.path.join(FIXTURE_D, f"{n}.md"), t) for n, t in [
            ("case_ok", "## 结果\n报告 8446B，行数 372。\n```text\n8446B 行数 372\n```\n99 / 341 占 29.0%\n"),
            ("case_drift", "## 结果\n本文件行数 372。\n```text\n本文件行数 374。\n```\n"),
            ("case_ratio", "## 结果\n99 / 341（12.0%）\n"),
            ("case_unit", "## 结果\n归档 8446B\n归档 9.5KB\n"),
            ("case_never", "## 结果\nmtime 2026-09-23 09:43，耗时 1200ms，RSS 265.6MB\n"),
            ("case_norm", "## 结果\n1,211,565,564 B 与 1211565564B 是同一值；99 / 341 与 99/341 同一式\n"),
            ("case_prefix_distinct", "## 结果\n        47.2 MiB  memory/L4_raw_sessions/2026-09.zip\n        16.0 MiB  memory/L4_raw_sessions/2026-05.zip\n"),
            ("case_bracket_model", "## 结果\n[1.5B] keep=6 drop=44\n[3B  ] keep=2 drop=48\n"),
            ("case_ratio_boundary", "## 结果\n2. `[ ] 候选2 工程 | 【第8批·评审 7/10】磁盘 98% 复发归因`\n"),
        ])
    # 真阳性·静止可复算类：拿真实冻结文件（本次自测的目标=history.txt）当场核对
    real_frozen = os.path.join(REPORTS_D, "history.txt")
    real_lines, real_bytes = open(real_frozen, "rb").read().count(b"\n"), os.path.getsize(real_frozen)
    stat_bad = _mk(os.path.join(FIXTURE_D, "case_stat_bad.md"),
                   f"## 结果\nmemory/autonomous_reports/history.txt 行数 {real_lines + 7}\n")
    stat_ok = _mk(os.path.join(FIXTURE_D, "case_stat_ok.md"),
                  f"## 结果\nmemory/autonomous_reports/history.txt 行数 {real_lines}、字节 {real_bytes}B\n")

    frozen = frozen_set([real_frozen])

    def scan_one(p):
        text = open(p, encoding="utf-8", errors="replace").read()
        c, r, s, a = scan_text(text, os.path.basename(p))
        a += stat_check(c, frozen, FIXTURE_D)
        return c, r, s, a

    c, r, s, a = scan_one(ok_f)
    chk("真阳性-负控: 一致语料 0 报警", not a, a)
    c, r, s, a = scan_one(drift_f)
    chk("真阳性-冻结漂移 报 FROZEN", any(x["kind"] == "FROZEN" for x in a), a)
    c, r, s, a = scan_one(ratio_f)
    chk("真阳性-比例不自洽 报 RATIO", any(x["kind"] == "RATIO" for x in a), a)
    c, r, s, a = scan_one(unit_f)
    chk("真阳性-单位冲突 报 INTERNAL", any(x["kind"] == "INTERNAL" for x in a), a)
    c, r, s, a = scan_one(never_f)
    chk("负控-白名单: 时变观测不报警", not a, a)
    chk("负控-白名单: 已计入 skipped", s and all(x["reason"] for x in s), s)
    c, r, s, a = scan_one(prefix_f)
    chk("负控-同前缀不同实体: 不报警（R381 类）", not a, a)
    # 正控：同一句复制件的**字节**漂移（同上下文签名，值不同）必须报
    copy_drift = _mk(os.path.join(FIXTURE_D, "case_copy_drift.md"),
                     "## 结果\n备份包 47.2 MiB（memory/L4_raw_sessions/2026-09.zip）\n"
                     "```text\n备份包 16.0 MiB（memory/L4_raw_sessions/2026-09.zip）\n```\n")
    c, r, s, a = scan_one(copy_drift)
    chk("真阳性-复制件字节漂移 报 FROZEN", any(x["kind"] == "FROZEN" for x in a), a)
    c, r, s, a = scan_one(bracket_f)
    chk("负控-方括号模型名[1.5B]: 不报警", not a, a)
    c, r, s, a = scan_one(rbound_f)
    chk("负控-比例跨排版边界(7/10】…98%): 不报警", not a, a)
    c, r, s, a = scan_one(norm_f)
    chk("负控-归一化: 千分位/空格不报警", not a, a)
    c, r, s, a = scan_one(stat_bad)
    chk("真阳性-现场复算(真实冻结文件) 报 STAT", any(x["kind"] == "STAT" for x in a), a)
    c, r, s, a = scan_one(stat_ok)
    chk("负控-现场复算: 正确数值不报警", not a, a)

    # 真归档 0 假警报（只按 FROZEN/INTERNAL/RATIO 计：STAT 对归档会因历史改写而命中，见报告口径）
    paths = files_to_scan()
    stats, alarms = run(paths, DEFAULT_FROZEN, REPO)
    hard = [x for x in alarms if x["kind"] in ("FROZEN", "INTERNAL", "RATIO")]
    chk(f"真归档扫描: 语料非空(files={stats['files']}>100)", stats["files"] > 100, stats["files"])
    chk("真归档扫描: 0 假警报(FROZEN/INTERNAL/RATIO) [%d 条]" % len(hard), not hard, hard[:3])
    chk("只读自证: 未检测到写入", not stats["written_detected"], stats["written_detected"])

    print(f"--- selftest {len(ok)}/{len(ok) + len(fails)} passed ---")
    for f in fails:
        print("  [FAIL]", f)
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description="报告数值断言闸门（只读）")
    ap.add_argument("--files", nargs="*", help="只扫这些 glob（默认两侧归档 R*.md）")
    ap.add_argument("--frozen", nargs="*", default=DEFAULT_FROZEN, help="现场复算目标 glob")
    ap.add_argument("--incl-index", action="store_true", help="附带扫 INDEX.md/history.md/history.txt")
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--list-never", action="store_true", help="打印永不比对白名单")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.list_never:
        for name, pat in NEVER_PATTERNS:
            print(f"{name}: {pat}")
        return 0
    paths = files_to_scan(args.files, args.incl_index)
    stats, alarms = run(paths, args.frozen, REPO)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"stats": stats, "alarms": alarms}, fh, ensure_ascii=False, indent=1)
    if not args.quiet:
        print(f"files={stats['files']} claims={stats['claims']} "
              f"(可复算键={stats['canon_claims']}) ratio_pairs={stats['ratio_pairs']} "
              f"skipped(白名单)={stats['skipped_never']} frozen={stats['frozen_files']}")
        print(f"alarms={stats['alarms']} {stats['alarm_kinds']}")
        for a in alarms[:80]:
            print(f"  [{a['kind']}] {a['file']}:{a['line']} {a['msg']}")
    if stats["written_detected"]:
        print("[FATAL] 检测到写入，违反只读保证:", stats["written_detected"], file=sys.stderr)
        return 3
    return 1 if stats["alarms"] else 0


if __name__ == "__main__":
    sys.exit(main())
