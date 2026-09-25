#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""const_number_audit.py — 记忆/SOP/定时 prompt「数值断言」↔ 源码常量 只读对账器（TODO 第15批 C5）

背景（RULE21）：dpdomain 续期有「两个数别混」的口径坑 —— 脚本手动去重 `RENEW_COOLDOWN_DAYS = 25`
（`tools/dpdomain_renew.py:61`）与 scheduler 调度冷却（`reflect/scheduler.py:62` `timedelta(days=27)`）。
文字（SOP / 定时 prompt / 记忆）里写下的数到底是哪一个，过去只能靠人眼比对源码。本工具把它机械化：
「同段出现的 .py 路径」= 该段自认的来源，据此回源取常量值再比对。
（2026-09-26 复核订正：本器默认语料实测唯一 MISMATCH 命中在 `temp/TODO.txt` 的描述句自身；
`memory/dpdomain_renew_sop.md` 与 `sche_tasks/dpdomain_renew.json` 实文都已同时写明 25/27 并各自绑对。）

口径声明（报告同步）：
  语料（--corpus 可改）= memory/**/*.md + memory/**/*.txt（默认剔除 autonomous_reports/ 与
        L4_raw_sessions/；--include-reports 打开） + sche_tasks/*.json（顶层；--sche-recursive 递归）
        + memory/global_mem_insight.txt(L1) + memory/global_mem.txt(L2) + temp/TODO.txt（显式列入，见 corpus_files）
  断言 = 「数字 + 时间单位」的 token 对（天/日/小时/分钟/秒/毫秒 + day(s)/hour(s)/min(ute)(s)/sec(ond)(s)/ms），
         **必须同段出现 .py 路径**（同行；同行无路径时回看同段落连续非空行 ≤ --block-lines 行）——无路径不判。
  常量面 = 该 .py 路径解析出的：
         CONST  = 赋值/带注解赋值的数值表达式（支持 + - * / 的字面量算术，如 `25 * 86400`）
         LIT    = 时长构造调用里的关键字字面量（timedelta(days=27) / timeout=... 里的 days=/hours=...）
  判据（三态）：
     一致    = ① 名字命中且值相等（CONSISTENT-NAMED）或 ② 值命中（CONSISTENT-VALUE，弱证据）
     不一致  = 名字命中（claim 上下文词 ↔ 常量名 token 同义/同名）且两侧单位族可换算，值不等
     无法判定= 路径无法解析 / claim 单位无族 / 名字命中但常量名无单位族（**单位不同不硬判**）/ 无名字命中且无值命中
  名字命中 = 常量名 token（按 _ 与驼峰切分，大写）∩ 断言上下文词 token ≠ ∅；上下文词 = 断言 ± --window 字符窗口内的
             ASCII 标识符 token ∪ 中文→英文同义词命中（冷却→COOLDOWN、窗口→WINDOW、超时→TIMEOUT、默认→DEFAULT …）
  只读保证：--readonly-proof 对全部被读文件做 sha256 前后比对，任何变动即 rc=3；本器不写任何被扫文件
退出码：0 = 无「不一致」；2 = 有「不一致」（可作闸门）；3 = 只读/内部错误

用法：python3 tools/const_number_audit.py [--json out.json] [--only <语料路径>] [--show-undecidable]
      [--root GA] [--corpus <dir>]... [--selftest] [--readonly-proof]
"""
import argparse
import ast
import hashlib
import json
import os
import re
import sys
import time

REPO = os.environ.get("GA_ROOT") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ---------------------------------------------------------------- 词表 / 正则
UNIT_PAT = (r"milliseconds?|seconds?|secs?|minutes?|mins?|hours?|days?|items?|entries?|lines?|bytes?"
            r"|毫秒|小时|分钟|秒钟|秒|天|日|字节|次|个|条|行"
            r"|KiB|MiB|GiB|KB|MB|GB|ms|mb|gb|kb|h|b")
CLAIM_RE = re.compile(r"(?<![\w.\-])(\d{1,7}(?:\.\d+)?)\s*(" + UNIT_PAT + r")(?![\w])", re.I)
PY_RE = re.compile(r"(?<![A-Za-z0-9_./-])((?:[\w.@+-]+/)*[\w.@+-]+\.py)(?![A-Za-z0-9_/])")

UNIT_FAM = {
    "天": "DAY", "日": "DAY", "小时": "HOUR", "分钟": "MIN", "秒钟": "SEC", "秒": "SEC", "毫秒": "MS",
    "day": "DAY", "days": "DAY", "hour": "HOUR", "hours": "HOUR", "h": "HOUR",
    "min": "MIN", "mins": "MIN", "minute": "MIN", "minutes": "MIN",
    "sec": "SEC", "secs": "SEC", "second": "SEC", "seconds": "SEC",
    "ms": "MS", "millisecond": "MS", "milliseconds": "MS",
    "次": "COUNT", "个": "COUNT", "条": "COUNT", "item": "COUNT", "items": "COUNT", "entry": "COUNT",
    "entries": "COUNT",
    "行": "LINES", "line": "LINES", "lines": "LINES",
    "字节": "BYTES", "b": "BYTES", "kb": "BYTES", "mb": "BYTES", "gb": "BYTES",
    "kib": "BYTES", "mib": "BYTES", "gib": "BYTES", "byte": "BYTES", "bytes": "BYTES",
}
# 只有时间族之间可换算（其余族只允许同族比较，跨族一律「无法判定」）
FAM_SECONDS = {"WEEK": 604800.0, "DAY": 86400.0, "HOUR": 3600.0, "MIN": 60.0, "SEC": 1.0, "MS": 0.001}
FAM_KW = {
    "DAY": ("DAY", "DAYS"),
    "HOUR": ("HOUR", "HOURS", "HRS"),
    "MIN": ("MIN", "MINS", "MINUTE", "MINUTES"),
    "SEC": ("SEC", "SECS", "SECOND", "SECONDS"),
    "MS": ("MS", "MILLI", "MILLIS", "MILLISECOND", "MILLISECONDS"),
    "COUNT": ("COUNT", "NUM", "NUMBER", "TIMES", "ATTEMPTS", "ATTEMPT", "RETRIES", "RETRY", "QUOTA", "ITEMS"),
    "LINES": ("LINE", "LINES", "ROWS", "ROW", "LEN", "LENGTH"),
    "BYTES": ("BYTE", "BYTES", "SIZE", "CAP", "THRESHOLD", "LIMIT", "MAX", "CEILING"),
}
# 中文上下文词 → 英文 token（用于「名字命中」；保守，宁缺勿滥）
SYN = {
    "冷却": ("COOLDOWN",), "超时": ("TIMEOUT",), "窗口": ("WINDOW",), "保留": ("KEEP", "RETENTION"),
    "留存": ("RETENTION", "KEEP"), "上限": ("MAX", "LIMIT"), "阈值": ("THRESHOLD",), "默认": ("DEFAULT",),
    "周期": ("INTERVAL", "PERIOD"), "间隔": ("INTERVAL",), "重试": ("RETRY",), "配额": ("QUOTA",),
    "缓存": ("CACHE",), "轮询": ("POLL",), "心跳": ("HEARTBEAT",), "过期": ("EXPIRE", "TTL"),
    "期限": ("TTL", "EXPIRE"), "延迟": ("DELAY",), "宽限": ("GRACE",), "时间戳": ("TIMESTAMP",),
    "批次": ("BATCH",), "并发": ("CONCURRENCY",), "速率": ("RATE",), "频率": ("FREQ", "INTERVAL"),
    "看门狗": ("WATCHDOG",), "预算": ("BUDGET",), "最大尝试": ("MAX", "ATTEMPT"), "新鲜度": ("FRESH", "AGE"),
}
DUR_FUNCS = {"timedelta", "Duration", "duration"}
KW_FAM = {"days": "DAY", "hours": "HOUR", "minutes": "MIN", "seconds": "SEC",
          "milliseconds": "MS", "mins": "MIN", "secs": "SEC", "ms": "MS"}
# 常量名里**显式出现的单位词**：只有名字真的带单位词，名字命中才算强证据（防「恰好 token 撞上」）
NAME_UNIT = {
    "DAY": ("DAY", "DAYS", "WEEK", "WEEKS"),
    "HOUR": ("HOUR", "HOURS", "HR", "HRS"),
    "MIN": ("MIN", "MINS", "MINUTE", "MINUTES"),
    "SEC": ("SEC", "SECS", "SECOND", "SECONDS"),
    "MS": ("MS", "MILLI", "MILLIS", "MILLISECOND", "MILLISECONDS"),
    "COUNT": ("COUNT", "NUM", "NUMBER", "TIMES", "ATTEMPTS", "ATTEMPT", "RETRIES", "RETRY", "QUOTA", "ITEMS"),
    "LINES": ("LINE", "LINES", "ROWS", "ROW", "LEN", "LENGTH"),
    "BYTES": ("BYTE", "BYTES", "SIZE", "KIB", "MIB", "GIB", "KB", "MB", "GB"),
}
# 名字里显式写出的单位词（值是「按名字声明的单位」的字符串，用于米制换算）
NAME_UNIT_STR = {t: u for u, ts in NAME_UNIT.items() for t in ts}
# 字节类单位换算（米制 1000 / 二进制 1024 两套，禁互推）
BYTE_MULT = {"B": 1.0, "BYTE": 1.0, "BYTES": 1.0, "KB": 1000.0, "MB": 10 ** 6, "GB": 10 ** 9,
             "KIB": 1024.0, "MIB": 1024.0 ** 2, "GIB": 1024.0 ** 3}
# 人名/数据类单位词（非「数字+单位」断言单位，仅用于识别常量名）
NON_UNIT_TOKENS = {"MAX", "MIN", "LIMIT", "CAP", "THRESHOLD", "SIZE", "WINDOW"}


def name_tokens(name):
    toks = []
    for chunk in re.split(r"[^A-Za-z0-9]+", name or ""):
        if not chunk:
            continue
        for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", chunk):
            toks.append(t.upper())
    return toks


def context_tokens(window):
    toks = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{2,}", window):
        toks.update(name_tokens(m.group(0)))
    for cjk, syns in SYN.items():
        if cjk in window:
            toks.update(syns)
    return toks


def close(a, b, rel=1e-9):
    if a is None or b is None:
        return False
    if a == b:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b), 1.0)


def parse_num(s):
    return float(s.replace(",", "").replace("_", ""))


# ---------------------------------------------------------------- .py 常量面
def _eval_num(node):
    """数值字面量或纯字面量算术。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval_num(node.operand)
        return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub, ast.Div)):
        a, b = _eval_num(node.left), _eval_num(node.right)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        return None if b == 0 else a / b
    return None


def _callee(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def fam_of_name(tokens, fam_hint=None):
    if fam_hint:
        return fam_hint
    for fam, kws in FAM_KW.items():
        for k in kws:
            if k in tokens:
                return fam
    return None


def consts_from_py(path, max_consts=4000):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return [], "READ-FAIL"
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], "PARSE-FAIL"
    out, seen = [], set()

    def add(kind, name, value, line, fam_hint=None):
        key = (kind, name, value, line)
        if key in seen or len(out) >= max_consts:
            return
        seen.add(key)
        toks = name_tokens(name)
        out.append(dict(kind=kind, name=name, value=value, line=line,
                        fam=fam_of_name(toks, fam_hint), tokens=toks,
                        upper=bool(re.fullmatch(r"[A-Z0-9_]+", name) and len(name) >= 3)))

    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            v = _eval_num(n.value)
            if v is not None:
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        add("CONST", t.id, v, n.lineno)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            v = _eval_num(n.value)
            if v is not None:
                add("CONST", n.target.id, v, n.lineno)
        elif isinstance(n, ast.Call):
            fn = _callee(n.func) or "call"
            for kw in n.keywords:
                if not kw.arg or not isinstance(kw.value, ast.Constant):
                    continue
                if not isinstance(kw.value.value, (int, float)) or isinstance(kw.value.value, bool):
                    continue
                fam = KW_FAM.get(kw.arg)
                if fam or kw.arg in ("weeks",):
                    add("LIT", "%s.%s" % (fn, kw.arg), float(kw.value.value), n.lineno, fam)
    return out, "OK"


_CONST_CACHE = {}


def consts_cached(path):
    if path not in _CONST_CACHE:
        _CONST_CACHE[path] = consts_from_py(path)
    return _CONST_CACHE[path]


def resolve_py(tok, src_file, root):
    cands = []
    if os.path.isabs(tok):
        cands.append(tok)
    else:
        cands.append(os.path.join(root, tok))
        cands.append(os.path.join(os.path.dirname(src_file), tok))
        cands.append(os.path.join(root, "tools", os.path.basename(tok)))
        cands.append(os.path.join(root, "memory", os.path.basename(tok)))
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


# ---------------------------------------------------------------- 语料枚举
def corpus_files(root, extra_dirs=(), include_reports=False, sche_recursive=False):
    files = []
    for p in ("memory/global_mem_insight.txt", "memory/global_mem.txt", "temp/TODO.txt"):
        f = os.path.join(root, p)
        if os.path.isfile(f):
            files.append(f)
    mem = os.path.join(root, "memory")
    if os.path.isdir(mem):
        for d, dirs, fs in os.walk(mem):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "L4_raw_sessions")]
            if not include_reports:
                dirs[:] = [x for x in dirs if x != "autonomous_reports"]
            for f in fs:
                if f.endswith((".md", ".txt")):
                    files.append(os.path.join(d, f))
    sch = os.path.join(root, "sche_tasks")
    if os.path.isdir(sch):
        if sche_recursive:
            for d, dirs, fs in os.walk(sch):
                dirs[:] = [x for x in dirs if x != "__pycache__"]
                files += [os.path.join(d, f) for f in fs if f.endswith((".json", ".md"))]
        else:
            files += [os.path.join(sch, f) for f in os.listdir(sch)
                      if f.endswith(".json") and os.path.isfile(os.path.join(sch, f))]
    for d in extra_dirs:
        d = d if os.path.isabs(d) else os.path.join(root, d)
        if os.path.isfile(d):
            files.append(d)
            continue
        for r, dirs, fs in os.walk(d):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            files += [os.path.join(r, f) for f in fs if f.endswith((".md", ".txt", ".json"))]
    return sorted(set(files))


# ---------------------------------------------------------------- 扫描
def iter_assertions(path, root, block_lines=3, window=80):
    """产出 (lineno, line, path_token, py_path_or_None, claims[]) 的断言记录。"""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    lines = text.split("\n")
    for i, line in enumerate(lines):
        claims = [(parse_num(m.group(1)), m.group(2).lower(), m.start(1), m.end(2))
                  for m in CLAIM_RE.finditer(line)]
        if not claims:
            continue
        toks = PY_RE.findall(line)
        if not toks:
            # 同段回退：上下连续非空行里找 .py 路径
            blk = []
            j = i - 1
            while j >= 0 and lines[j].strip() and i - j <= block_lines:
                blk.append(lines[j]); j -= 1
            j = i + 1
            while j < len(lines) and lines[j].strip() and j - i <= block_lines:
                blk.append(lines[j]); j += 1
            for b in blk:
                toks += PY_RE.findall(b)
            if not toks:
                continue
        # 路径解析去重
        resolved, unresolved, seen = [], [], set()
        for t in toks:
            if t in seen:
                continue
            seen.add(t)
            rp = resolve_py(t, path, root)
            (resolved.append(rp) if rp else unresolved.append(t))
        yield dict(file=path, lineno=i + 1, line=line, toks=sorted(seen),
                   resolved=sorted(set(resolved)), unresolved=sorted(set(unresolved)),
                   claims=[(v, u, s, e) for v, u, s, e in claims],
                   same_line=bool(PY_RE.findall(line)))


def _name_unit_str(c):
    """从常量名里取「字面声明的单位词」（米制换算用）；取不到返回 'B'。"""
    for t in (c.get("tokens") or []):
        if t in BYTE_MULT:
            return t
    return "B"


def _strong_name_hit(c, win, phrase):
    """名字命中是否算强证据：
    ①常量名（源码原大小写形态，≥3 字符）在窗口内逐字出现 = 字面绑定；或
    ②模块级全大写常量 ∧ 名字里带显式单位词 ∧ 该单位词在窗口里独立出现且不落在数字短语内。
    phrase = 数字短语区域（claim 前后各若干字符），命中词落在这里 = 自匹配（是断言的单位而非常量名）。
    返回绑定方式字符串（None = 弱命中，不据此判 MISMATCH）。
    """
    name = c.get("name") or ""
    if len(name) >= 3 and name in win:
        return "LITERAL"
    if not c.get("upper"):
        return None
    for t in (c.get("tokens") or []):
        if t not in NAME_UNIT_STR:
            continue
        for m in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", win, re.I):
            if m.group(0).upper() in phrase.upper():
                continue  # 自匹配剔除
            return "UNITWORD:" + t
    return None


def _literal_hit(c, win):
    """常量名（或其 ≥3 字符的 token）在窗口内**逐字**出现（ASCII，大小写不敏感）。
    仅靠 SYN 语义（如「天」→DAYS）算命中太弱，会把跨语义的噪音常量拉进来。"""
    low = win.lower()
    nm = (c.get("name") or "").lower()
    if len(nm) >= 3 and nm in low:
        return True
    for t in (c.get("tokens") or []):
        if len(t) < 3:
            continue
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(t.lower()) + r"(?![A-Za-z0-9])", low):
            return True
    return False


def _non_self_tokens(c, ctset, phrase):
    """名字 token 中「不是断言自身单位」的那些（用于弱命中降级判定）。"""
    out = []
    for t in (c.get("tokens") or []):
        if t in ctset and t.upper() not in phrase.upper():
            out.append(t)
    return out


NEAR = 60  # 弱绑定「邻近」阈值（字符）


def _near(c, dist_fn):
    d = dist_fn(c)
    return d is not None and d <= NEAR


def classify(rec, window=80):
    """对一条断言行内的每个 claim 出判据。返回记录列表。"""
    out = []
    line = rec["line"]
    consts = []
    for rp in rec["resolved"]:
        cs, st = consts_cached(rp)
        for c in cs:
            c = dict(c, src=rp, src_state=st)
            consts.append(c)
    for val, unit, cs_, ce_ in rec["claims"]:
        fam = UNIT_FAM.get(unit)
        win = line[max(0, cs_ - window):min(len(line), ce_ + window)]
        claim_text = line[cs_:ce_]
        phrase = line[max(0, cs_ - 3):min(len(line), ce_ + 12)]
        ctset = context_tokens(win)
        tot = dict(file=rec["file"], lineno=rec["lineno"], claim=claim_text,
                   value=val, unit=unit, fam=fam, tokens=sorted(ctset),
                   resolved=rec["resolved"], unresolved=rec["unresolved"],
                   same_line=rec["same_line"])

        def conv_eq(c):
            """单位族一致的换算等值（两侧都有族且可比才判）。"""
            cf = c["fam"]
            if not (fam and cf):
                return False
            if fam == cf == "BYTES":
                return close(val * BYTE_MULT.get(unit.upper(), 1.0),
                             c["value"] * BYTE_MULT.get(_name_unit_str(c), 1.0))
            if fam == cf:
                return close(val, c["value"])
            if fam in FAM_SECONDS and cf in FAM_SECONDS:
                return close(val * FAM_SECONDS[fam], c["value"] * FAM_SECONDS[cf])
            return False

        def mention_dist(c):
            """常量名（或名字 token）在窗口内相对 claim 起点的最小字符距离。"""
            off = cs_ - max(0, cs_ - window)
            low = win.lower()
            best = None
            for cand in [c["name"]] + list(c["tokens"]):
                if not cand:
                    continue
                cl = cand.lower()
                p = low.find(cl)
                while p >= 0:
                    d = abs(p - off)
                    if best is None or d < best:
                        best = d
                    p = low.find(cl, p + 1)
            return best

        def raw_eq(c):
            """原始数值相等（忽略单位）=弱证据，不作 MISMATCH 依据。"""
            return close(val, c["value"])

        named = [c for c in consts if set(c["tokens"]) & ctset and _literal_hit(c, win)]
        strong = [(c, _strong_name_hit(c, win, phrase)) for c in named]
        strong = [(c, how) for c, how in strong if how]
        sconsts = [c for c, _ in strong]
        bind_of = {id(c): how for c, how in strong}
        eq_strong = [c for c in sconsts if conv_eq(c)]
        eq_all = [c for c in consts if conv_eq(c)]
        raw_all = [c for c in consts if raw_eq(c)]
        if eq_strong:
            tot.update(state="CONSISTENT-NAMED", reason="名字强绑定且值相等",
                       witness=dict(eq_strong[0], bind=bind_of.get(id(eq_strong[0]))))
        elif any(conv_eq(c) for c in named if _near(c, mention_dist)):
            tot.update(state="CONSISTENT-NAMED", reason="名字（弱）命中且值相等（弱证据）",
                       witness=next(c for c in named if conv_eq(c) and _near(c, mention_dist)))
        elif eq_all:
            tot.update(state="CONSISTENT-VALUE",
                       reason="值命中（可换算等值；弱证据，名称无关）", witness=eq_all[0])
        elif strong:
            same_fam = [c for c in sconsts if fam and c["fam"] and fam == c["fam"]]
            if same_fam:
                tot.update(state="MISMATCH", reason="名字强绑定且单位同族但值不等",
                           candidates=same_fam, witness=same_fam[0],
                           binds=[bind_of.get(id(c)) for c in same_fam])
            elif raw_all:
                tot.update(state="CONSISTENT-VALUE",
                           reason="值命中但单位族不可比（弱证据，不作 MISMATCH）",
                           witness=raw_all[0])
            else:
                tot.update(state="UNDECIDABLE",
                           reason="名字强绑定但单位族不同/常量名无单位族（单位不同不硬判）",
                           candidates=sconsts[:6])
        elif named:
            live_named = [c for c in named if _non_self_tokens(c, ctset, phrase)]
            near_named = [c for c in live_named if _near(c, mention_dist)]
            same_fam_weak = [c for c in near_named if fam and c["fam"] and fam == c["fam"]]
            if not live_named:
                tot.update(state="UNDECIDABLE",
                           reason="名字命中全部为自匹配（命中词即断言自身单位/数字）⇒不判",
                           candidates=[c for c in named][:6])
            elif same_fam_weak:
                tot.update(state="SUSPECT-NAMED",
                           reason="名字弱命中且单位同族但值不等（需人工核实；不硬判为不一致）",
                           candidates=same_fam_weak, witness=same_fam_weak[0])
            elif near_named:
                tot.update(state="UNDECIDABLE",
                           reason="名字弱命中（非模块常量/无显式单位词）且单位族不可比⇒不硬判",
                           candidates=near_named[:6])
            else:
                tot.update(state="UNDECIDABLE",
                           reason="名字弱命中但距离过远（>%d 字符）⇒不硬判，避免跨语义噪音" % NEAR,
                           candidates=live_named[:3])
        elif raw_all:
            tot.update(state="CONSISTENT-VALUE",
                       reason="值命中（弱证据，无名字命中）", witness=raw_all[0])
        elif not rec["resolved"]:
            tot.update(state="UNDECIDABLE", reason=".py 路径无法解析", candidates=[])
        else:
            tot.update(state="UNDECIDABLE", reason="无名字命中且无值命中", candidates=[])
        out.append(tot)
    return out


def run(files, root, block_lines=3, window=80, only=None):
    recs, n_lines, t0 = [], 0, time.time()
    for f in files:
        if only and os.path.abspath(f) != os.path.abspath(only):
            continue
        try:
            n_lines += open(f, encoding="utf-8", errors="replace").read().count("\n") + 1
        except OSError:
            continue
        for rec in iter_assertions(f, root, block_lines, window):
            recs += classify(rec, window)
    return recs, n_lines, time.time() - t0


def sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- 自测
def selftest(verbose=True):
    import tempfile
    checks = []

    def chk(name, cond, extra=""):
        checks.append((name, bool(cond), extra))

    root = tempfile.mkdtemp(prefix="const_number_audit_fx_")
    os.makedirs(os.path.join(root, "tools"), exist_ok=True)
    TGT = "COOLDOWN_DAYS = 25\nWINDOW_HOURS = 6\nLIMIT = 3\nITEMS = 5\n"
    with open(os.path.join(root, "tools/fx_targets.py"), "w", encoding="utf-8") as fh:
        fh.write(TGT)
    with open(os.path.join(root, "tools/fx_lit.py"), "w", encoding="utf-8") as fh:
        fh.write("from datetime import timedelta\n"
                 "def span(repeat):\n"
                 "    if repeat == 'monthly':\n"
                 "        return timedelta(days=27)\n"
                 "    if repeat == 'weekly':\n"
                 "        return timedelta(days=6)\n"
                 "    return timedelta(hours=20)\n")
    with open(os.path.join(root, "tools/fx_units.py"), "w", encoding="utf-8") as fh:
        fh.write("WINDOW_SECONDS = 21600\n")
    md = os.path.join(root, "notes.md")
    body = "\n".join([
        "冷却窗口 COOLDOWN_DAYS = 27 天，见 `tools/fx_targets.py`。",        # L1  字面绑定 + 值不等 → MISMATCH
        "",
        "冷却窗口 COOLDOWN 25 天，见 `tools/fx_targets.py`。",               # L3  弱绑定（只有名字 token 字面出现）+ 值等 → CONSISTENT-NAMED
        "",
        "连续运行 54 天（本段无路径）。",                                     # L5  NC2 无 .py 路径 → 不产生断言
        "",
        "冷却窗口 90 次，见 `tools/fx_targets.py`。",                        # L7  NC3 单位族不同 → 不硬判
        "",
        "冷却窗口 27 天，见 `tools/not_here.py`。",                          # L9  NC4 路径不可解析
        "",
        "冷却上限 LIMIT 28800 秒，见 `tools/fx_targets.py`。",               # L11 NC5 字面绑定但常量名无单位族
        "",
        "延迟 3 小时，见 `tools/fx_targets.py`。",                           # L13 值命中弱证据（LIMIT=3）
        "",
        "窗口 WINDOW_SECONDS 6 小时，见 `tools/fx_units.py`。",              # L15 换算正控 6h==21600s（字面名）
        "",
        "月度冷却 27 天，见 `tools/fx_lit.py`。",                            # L17 LIT 值命中
        "",
        "月度冷却 27 天，",                                                  # L19 同段回退（claim 与路径分两行）
        "见 `tools/fx_lit.py`。",
        "",
        "月度冷却 27 天。",                                                  # L22 空行隔断 → 不判
        "",
        "见 `tools/fx_lit.py`。",
        "",
        "R427 与第15批均已闭环。",                                           # L26 非数值断言行
        "",
        "本轮 3 个 items，见 `tools/fx_targets.py`。",                       # L28 自匹配剔除（命中词即断言自身单位）
        "",
        "冷却窗口 COOLDOWN 33 天，见 `tools/fx_targets.py`。",               # L30 弱绑定 + 值不等 → SUSPECT-NAMED
    ]) + "\n"
    open(md, "w", encoding="utf-8").write(body)

    files = corpus_files(root, extra_dirs=[root], include_reports=True, sche_recursive=False)
    before = {f: sha(f) for f in files}
    recs, n_lines, dt = run(files, root)
    after = {f: sha(f) for f in files}
    by = {}
    for r in recs:
        by.setdefault((os.path.basename(r["file"]), r["lineno"]), []).append(r)

    def st(ln, i=0):
        rows = by.get(("notes.md", ln), [])
        return rows[i]["state"] if len(rows) > i else None

    def rs(ln, i=0):
        rows = by.get(("notes.md", ln), [])
        return rows[i].get("reason", "") if len(rows) > i else ""

    chk("selftest 语料切分自证（fixture 齐备且行数>0）",
        len(files) >= 1 and n_lines > 0 and all(os.path.isfile(os.path.join(root, "tools", f))
                                               for f in ("fx_targets.py", "fx_lit.py", "fx_units.py")),
        "files=%d lines=%d" % (len(files), n_lines))
    chk("NC1 字面绑定+值不等 → MISMATCH", st(1) == "MISMATCH", str(st(1)))
    chk("NC1b MISMATCH 记录指到正确常量名（COOLDOWN_DAYS）",
        (by.get(("notes.md", 1), [{}])[0].get("witness") or {}).get("name") == "COOLDOWN_DAYS",
        str((by.get(("notes.md", 1), [{}])[0].get("witness") or {}).get("name")))
    chk("正控 弱绑定+值相等 → CONSISTENT-NAMED", st(3) == "CONSISTENT-NAMED", str(st(3)))
    chk("NC2 本段无 .py 路径 → 不产生断言", ("notes.md", 5) not in by)
    chk("NC3 单位族不同（次 vs 天）→ 非 MISMATCH", st(7) == "UNDECIDABLE", str(st(7)))
    chk("NC4 路径无法解析 → UNDECIDABLE(不可解析)",
        st(9) == "UNDECIDABLE" and "解析" in rs(9), str(st(9)) + "|" + rs(9))
    chk("NC5 常量名无单位族 → UNDECIDABLE（单位不同不硬判）", st(11) == "UNDECIDABLE", str(st(11)))
    chk("值命中为弱证据 → CONSISTENT-VALUE（不得报 MISMATCH）", st(13) == "CONSISTENT-VALUE", str(st(13)))
    chk("单位换算正控（6 小时 == WINDOW_SECONDS 21600）→ CONSISTENT-NAMED",
        st(15) == "CONSISTENT-NAMED", str(st(15)))
    chk("LIT(timedelta days=27) 值命中 → CONSISTENT-VALUE", st(17) == "CONSISTENT-VALUE", str(st(17)))
    chk("同段回退：claim 与路径分两行仍判定", st(19) in ("CONSISTENT-VALUE", "CONSISTENT-NAMED"),
        str([r["state"] for r in by.get(("notes.md", 19), [])]))
    chk("空行隔断 → 不判（无断言）", ("notes.md", 22) not in by)
    chk("非数值断言行（R427/第15批）不产生 claim", ("notes.md", 26) not in by)
    chk("NC6 自匹配剔除：命中词即断言自身单位 → UNDECIDABLE（不得 MISMATCH）",
        st(28) == "UNDECIDABLE" and "自匹配" in rs(28), str(st(28)) + "|" + rs(28))
    chk("NC7 弱绑定+值不等 → SUSPECT-NAMED（需人核实，不得硬判 MISMATCH）",
        st(30) == "SUSPECT-NAMED", str(st(30)))
    chk("只读自证：全部语料 sha256 前后一致", before == after)
    chk("MISMATCH 记录带候选常量（文件:行/名/值）",
        all(r.get("candidates") for r in recs if r["state"] == "MISMATCH"))
    # 负控：篡改常量必报（25 → 27）⇒ 原 MISMATCH 项翻为一致、原弱一致项翻为待核实
    with open(os.path.join(root, "tools/fx_targets.py"), "w", encoding="utf-8") as fh:
        fh.write(TGT.replace("COOLDOWN_DAYS = 25", "COOLDOWN_DAYS = 27"))
    _CONST_CACHE.clear()
    recs2, _, _ = run(files, root)
    by2 = {}
    for r in recs2:
        by2.setdefault((os.path.basename(r["file"]), r["lineno"]), []).append(r)

    def st2(ln):
        rows = by2.get(("notes.md", ln), [])
        return rows[0]["state"] if rows else None

    chk("负控A 篡改常量后 原 MISMATCH 项（27）翻为一致", st2(1) == "CONSISTENT-NAMED", str(st2(1)))
    chk("负控B 篡改常量后 原一致项（25）翻为待核实", st2(3) == "SUSPECT-NAMED", str(st2(3)))
    chk("负控C 篡改后 MISMATCH 清零（无残留假阳性）",
        not [r for r in recs2 if r["state"] == "MISMATCH"],
        str([(os.path.basename(r["file"]), r["lineno"]) for r in recs2 if r["state"] == "MISMATCH"]))
    import shutil
    shutil.rmtree(root, ignore_errors=True)

    ok = sum(1 for _, c, _ in checks if c)
    if verbose:
        for n, c, e in checks:
            print("%s %s%s" % ("PASS" if c else "FAIL", n, ("  [%s]" % e) if (e and not c) else ""))
        print("selftest: %d/%d" % (ok, len(checks)))
    return 0 if ok == len(checks) else 2


# ---------------------------------------------------------------- 报告
def main():
    ap = argparse.ArgumentParser(description="记忆/SOP/定时 prompt 数值 ↔ 源码常量 只读对账器")
    ap.add_argument("--root", default=REPO)
    ap.add_argument("--corpus", action="append", default=[], help="追加语料目录/文件")
    ap.add_argument("--include-reports", action="store_true", help="纳入 memory/autonomous_reports/")
    ap.add_argument("--sche-recursive", action="store_true")
    ap.add_argument("--only", help="只扫这一份语料")
    ap.add_argument("--block-lines", type=int, default=3, help="同段回退行数（默认 3）")
    ap.add_argument("--window", type=int, default=80, help="名字命中上下文窗口字符数（默认 80）")
    ap.add_argument("--json", help="结果写 JSON")
    ap.add_argument("--show-undecidable", action="store_true")
    ap.add_argument("--max-list", type=int, default=40)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readonly-proof", action="store_true", help="被读文件 sha256 前后比对")
    ap.add_argument("--strict", action="store_true", help="闸门模式：MISMATCH 或 SUSPECT 存在即 rc=2")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    root = os.path.abspath(args.root)
    if args.only:
        files = [os.path.abspath(args.only)]
    else:
        files = corpus_files(root, args.corpus, args.include_reports, args.sche_recursive)
    if not files:
        print("no corpus files", file=sys.stderr)
        return 3
    before = {f: sha(f) for f in files} if args.readonly_proof else None
    recs, n_lines, dt = run(files, root, args.block_lines, args.window)
    if args.readonly_proof:
        after = {f: sha(f) for f in files}
        if before != after:
            print("READ-ONLY VIOLATION: corpus changed during scan", file=sys.stderr)
            return 3

    st = {}
    for r in recs:
        st[r["state"]] = st.get(r["state"], 0) + 1
    und = [r for r in recs if r["state"] == "UNDECIDABLE"]
    und_reason = {}
    for r in und:
        und_reason[r["reason"]] = und_reason.get(r["reason"], 0) + 1
    mism = [r for r in recs if r["state"] == "MISMATCH"]

    print("语料文件 %d / 扫描行 %d / 断言行 %d（耗时 %.2fs）" % (
        len(files), n_lines, len({(r["file"], r["lineno"]) for r in recs}), dt))
    print("断言条数 %d：%s" % (len(recs), " / ".join("%s=%d" % kv for kv in sorted(st.items())) or "(无)"))
    print("无法判定细分：" + (" / ".join("%s=%d" % kv for kv in sorted(und_reason.items())) or "(无)"))
    print()
    if mism:
        print("== 不一致 %d 条 ==" % len(mism))
        for i, r in enumerate(mism[:args.max_list], 1):
            c = r["witness"]
            print("[%d] %s:%d  断言「%s」" % (i, os.path.relpath(r["file"], root), r["lineno"], r["claim"]))
            print("     候选常量 %s:%d  %s = %s  (族=%s)" % (
                os.path.relpath(c["src"], root), c["line"], c["name"], c["value"], c["fam"]))
            print("     上下文词 %s" % (",".join(r["tokens"][:12]) or "(空)"))
    else:
        print("== 不一致 0 条 ==")
    susp = [r for r in recs if r["state"] == "SUSPECT-NAMED"]
    if susp:
        print("\n== 待人工核实（弱绑定同名族值不等）%d 条 ==" % len(susp))
        for r in susp[:min(args.max_list, 12)]:
            c = r["witness"]
            print("  %s:%d 「%s」↔ 候选 %s:%d %s=%s  (族=%s)" % (
                os.path.relpath(r["file"], root), r["lineno"], r["claim"],
                os.path.relpath(c["src"], root), c["line"], c["name"], c["value"], c["fam"]))
    cons = [r for r in recs if r["state"] == "CONSISTENT-NAMED"]
    if cons:
        print("\n== 名字命中且一致 %d 条 ==" % len(cons))
        for r in cons[:min(args.max_list, 12)]:
            c = r["witness"]
            print("  %s:%d 「%s」↔ %s:%d %s=%s" % (
                os.path.relpath(r["file"], root), r["lineno"], r["claim"],
                os.path.relpath(c["src"], root), c["line"], c["name"], c["value"]))
    valcons = [r for r in recs if r["state"] == "CONSISTENT-VALUE"]
    if valcons:
        print("\n== 值命中（弱证据）%d 条 ==" % len(valcons))
        for r in valcons[:min(args.max_list, 8)]:
            c = r["witness"]
            print("  %s:%d 「%s」= %s:%d %s=%s" % (
                os.path.relpath(r["file"], root), r["lineno"], r["claim"],
                os.path.relpath(c["src"], root), c["line"], c["name"], c["value"]))
    if args.show_undecidable:
        print("\n== 无法判定 %d 条 ==" % len(und))
        for r in und[:args.max_list]:
            print("  %s:%d 「%s」 — %s" % (os.path.relpath(r["file"], root), r["lineno"], r["claim"], r["reason"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(dict(root=root, files=len(files), lines=n_lines, seconds=dt,
                           states=st, undecidable_reasons=und_reason,
                           results=[{k: v for k, v in r.items() if k != "witness"} for r in recs]),
                      fh, ensure_ascii=False, indent=1)
        print("\nJSON → %s" % args.json)
    if args.strict:
        return 2 if (mism or susp) else 0
    return 2 if mism else 0


if __name__ == "__main__":
    sys.exit(main())
