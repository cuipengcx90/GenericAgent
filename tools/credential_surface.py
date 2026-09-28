#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""credential_surface.py — 凭证 → 消费者映射与失效判据表（只读体检器）

目的：把「本机有哪些凭证面 / 谁在消费它 / 失效时长什么样 / 现在是否还在」
      从散落的 SOP 与事故记录收拢成一份**可机械复算**的声明表 + 探测器。

设计铁律（写进代码，不靠自觉）：
  1) 只判「存在性 / 名字 / 条数」，绝不读取、打印、落盘任何凭证值。
     - 自建 keychain 走官方 API `keys.ls()`（只出名），不解析 ~/ga_keychain.enc。
     - 浏览器 cookie 只做 `SELECT COUNT(*)`，不 select value/encrypted_value。
     - macOS keychain 只取返回码，stdout 一律丢弃。
     - mykey.py 只做 AST 取变量名，不 eval、不取值。
  2) 探针不确定一律 fail-closed：UNVERIFIABLE（rc=2），绝不当 OK。
  3) selftest 全部走 fixture（合成假值 FAKE-*），不触碰真实密钥/真实浏览器库。

用法：
    python3 tools/credential_surface.py plan     [--decl P] [--json OUT]
    python3 tools/credential_surface.py probe    [--decl P] [--only A,B] [--json OUT]
    python3 tools/credential_surface.py coverage [--decl P] [--json OUT] [--root P]
    python3 tools/credential_surface.py judge    [--decl P] [--json OUT] [--strict]
    python3 tools/credential_surface.py proof    [--decl P] [--root P]
    python3 tools/credential_surface.py selftest

退出码：
    0  一切可判且无硬问题
    1  硬问题（STORE_MISSING / STALE_PATH / TOKEN_GONE / EVIDENCE_GONE / UNDECLARED）
    2  fail-closed（UNVERIFIABLE：探针不可用 / 未知 probe kind / fixture 缺失）
    3  声明表结构错误
"""

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- 常量

GA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DECL = os.path.join("memory", "credential_surface", "credentials.json")

PROBE_KINDS = {
    "ga_keychain_name",   # 自建 keychain（memory/keychain.py → ~/ga_keychain.enc）
    "macos_keychain_item",  # macOS 原生 keychain generic-password（只取 rc）
    "config_var",         # 配置文件（如 mykey.py）里的顶层变量/字典键名
    "env_var",            # 进程环境变量是否存在
    "browser_cookie",     # Chrome Cookies 库里的 (host, name) 是否存在（只计数）
    "dir_entries",        # 目录是否存在及条目数
}

CONSUMER_KINDS = {"repo", "external"}

# 硬问题（rc=1）；其余为提示级
HARD_ISSUES = {
    "UNVERIFIABLE",       # fail-closed：探针不可判也计硬问题（rc=2）
    "STORE_MISSING",
    "STALE_PATH",
    "TOKEN_GONE",
    "EVIDENCE_GONE",
    "EVIDENCE_LINE_DRIFT",
    "UNDECLARED",
}

# coverage 扫描：凭证型名字的正则
CRED_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASS|PWD|COOKIE|SESSION|CRED)", re.I)

# `keys.<name>` 里不是凭证名的成员/方法
KEYCHAIN_METHODS = {
    "get", "set", "ls", "append", "index", "items", "setdefault", "indexOf",
    "pop", "update", "keys", "values", "clear", "copy", "extend", "insert",
    "remove", "sort", "count",
}

# 疑似密钥值（用于泄漏守卫：命中即报 LEAK，不打印命中内容）
LEAK_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bcli_[a-z0-9]{12,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\b[0-9a-f]{32,}\b"),          # 长 hex（cookie/密文常见）
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # 长 base64
    re.compile(r"FAKE-SECRET-[A-Za-z0-9_\-]{4,}"),  # fixture 里只允许出现在 fixture 内
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path, cap=8 << 20):
    try:
        if os.path.getsize(path) > cap:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- 泄漏守卫

def strip_env_paths(text, ctx):
    """剥掉运行环境路径文本（临时目录随机名会命中密钥正则 ⇒ 假 LEAK）。"""
    if not text:
        return text
    for cand in (getattr(ctx, "tmp", None), getattr(ctx, "root", None),
                 getattr(ctx, "fixtures", None)):
        if cand:
            text = text.replace(cand, "<PATH>")
    return text


def leak_scan(text, allow_fixture=False):
    """返回命中条数（不返回命中内容，防二次泄漏）。"""
    if not text:
        return 0
    hits = 0
    for pat in LEAK_PATTERNS:
        for m in pat.finditer(text):
            if allow_fixture and m.group(0).startswith("FAKE-"):
                continue
            hits += 1
    return hits


# ---------------------------------------------------------------- 探针上下文

class Ctx:
    """探针上下文：fixtures 非空时全部探针转向 fixture（selftest 用）。"""

    def __init__(self, root=GA_ROOT, fixtures=None):
        self.root = root
        self.fixtures = os.path.abspath(fixtures) if fixtures else None
        # fixture 根指定了但不存在 ⇒ 全部探针不可判（fail-closed）
        self.fx_missing = bool(self.fixtures) and not os.path.isdir(self.fixtures)
        self.tmp = tempfile.mkdtemp(prefix="cred_surface_")
        self.fx = lambda *p: os.path.join(self.fixtures, *p) if self.fixtures else None

    def resolve(self, rel):
        """相对路径解析：fixture 模式下全部落在 fixture 根。"""
        if os.path.isabs(rel):
            return rel
        return os.path.join(self.fixtures if self.fixtures else self.root, rel)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- 各探针

def probe_ga_keychain_name(locator, ctx):
    name = locator.get("name")
    if not name:
        return "UNVERIFIABLE", "locator.name 缺失"
    if ctx.fixtures:
        p = ctx.fx("keychain_names.json")
        if not os.path.exists(p):
            return "UNVERIFIABLE", "fixture keychain_names.json 缺失"
        names = json.load(open(p, encoding="utf-8"))
    else:
        kc = os.path.join(ctx.root, "memory", "keychain.py")
        if not os.path.exists(kc):
            return "UNVERIFIABLE", "memory/keychain.py 不存在"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_cred_kc", kc)
            mod = importlib.util.module_from_spec(spec)
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf          # 该模块 import 时会 print 一行提示，吞掉
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.stdout = old
            names = mod.keys.ls()     # 只出名，不取值
        except Exception as e:        # noqa: BLE001 — fail-closed
            return "UNVERIFIABLE", f"keychain 加载失败: {type(e).__name__}"
    return ("PRESENT" if name in names else "MISSING"), f"在册 {len(names)} 个名字"


def probe_macos_keychain_item(locator, ctx):
    name = locator.get("name")
    if not name:
        return "UNVERIFIABLE", "locator.name 缺失"
    if ctx.fixtures:
        p = ctx.fx("macos_items.json")
        if not os.path.exists(p):
            return "UNVERIFIABLE", "fixture macos_items.json 缺失"
        items = json.load(open(p, encoding="utf-8"))
        return ("PRESENT" if name in items else "MISSING"), "fixture"
    if not shutil.which("security"):
        return "UNVERIFIABLE", "security 命令不可用"
    # 不带 -g：只出属性不出密码；stdout 直接丢弃，只用返回码
    r = subprocess.run(["security", "find-generic-password", "-s", name],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return "PRESENT", "rc=0"
    if r.returncode == 44:
        return "MISSING", "rc=44 (item not found)"
    return "UNVERIFIABLE", f"rc={r.returncode}"


def _ast_names(path):
    src = read_text(path)
    if src is None:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    names = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
        val = getattr(node, "value", None)
        if isinstance(val, ast.Dict):
            for k in val.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    names.add(k.value)
    return names


def probe_config_var(locator, ctx):
    rel = locator.get("path")
    name = locator.get("name")
    if not rel or not name:
        return "UNVERIFIABLE", "locator.path/name 缺失"
    path = ctx.resolve(rel)
    if not os.path.exists(path):
        return "UNVERIFIABLE", f"配置文件不存在: {rel}"
    names = _ast_names(path)
    if names is None:
        return "UNVERIFIABLE", f"无法解析 {rel}"
    return ("PRESENT" if name in names else "MISSING"), f"顶层名字 {len(names)} 个"


def probe_env_var(locator, ctx):
    name = locator.get("name")
    if not name:
        return "UNVERIFIABLE", "locator.name 缺失"
    if ctx.fixtures:
        p = ctx.fx("env.json")
        if not os.path.exists(p):
            return "UNVERIFIABLE", "fixture env.json 缺失"
        env = json.load(open(p, encoding="utf-8"))
        return ("PRESENT" if name in env else "MISSING"), "fixture"
    return ("PRESENT" if name in os.environ else "MISSING"), "进程环境"


def probe_browser_cookie(locator, ctx):
    host, name = locator.get("host"), locator.get("name")
    if not host or not name:
        return "UNVERIFIABLE", "locator.host/name 缺失"
    if ctx.fixtures:
        db = ctx.fx("cookies.db")
        if not os.path.exists(db):
            return "UNVERIFIABLE", "fixture cookies.db 缺失"
        copy = os.path.join(ctx.tmp, "fx_cookies.db")
        shutil.copy2(db, copy)
        source = db
    else:
        chrome = os.path.expanduser("~/Library/Application Support/Google/Chrome")
        profile = locator.get("profile", "Default")
        src = os.path.join(chrome, profile, "Cookies")
        if not os.path.exists(src):
            return "UNVERIFIABLE", f"Cookies 库不存在: {profile}"
        copy = os.path.join(ctx.tmp, re.sub(r"\W+", "_", host + name) + ".db")
        # RULES#17：必须连 -wal/-shm 一起拷，否则可能"假空"
        shutil.copy2(src, copy)
        for ext in ("-wal", "-shm"):
            if os.path.exists(src + ext):
                shutil.copy2(src + ext, copy + ext)
        source = src
    try:
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        n = con.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key=? AND name=?",
            (host, name)).fetchone()[0]
        con.close()
    except sqlite3.Error as e:
        return "UNVERIFIABLE", f"sqlite 读取失败: {type(e).__name__}"
    return ("PRESENT" if n else "MISSING"), f"matched rows={n}"


def probe_dir_entries(locator, ctx):
    rel = locator.get("path")
    if not rel:
        return "UNVERIFIABLE", "locator.path 缺失"
    path = ctx.resolve(os.path.expanduser(rel))
    if not os.path.isdir(path):
        return "UNVERIFIABLE", f"目录不存在: {rel}"
    try:
        n = len(os.listdir(path))
    except OSError:
        return "UNVERIFIABLE", "目录不可读"
    return ("PRESENT" if n else "MISSING"), f"条目数={n}"


PROBES = {
    "ga_keychain_name": probe_ga_keychain_name,
    "macos_keychain_item": probe_macos_keychain_item,
    "config_var": probe_config_var,
    "env_var": probe_env_var,
    "browser_cookie": probe_browser_cookie,
    "dir_entries": probe_dir_entries,
}


# ---------------------------------------------------------------- 声明表

def load_decl(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_decl(decl):
    problems = []
    if not isinstance(decl, dict):
        return ["声明表根节点必须是对象"]
    if "entries" not in decl or not isinstance(decl["entries"], list):
        problems.append("缺少 entries 列表")
        return problems
    seen = set()
    for i, e in enumerate(decl["entries"]):
        tag = f"entries[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{tag} 不是对象")
            continue
        for k in ("id", "kind", "locator", "probe", "consumers"):
            if k not in e:
                problems.append(f"{tag} 缺字段 {k}")
        eid = e.get("id")
        if eid in seen:
            problems.append(f"{tag} id 重复: {eid}")
        seen.add(eid)
        if e.get("kind") not in PROBE_KINDS:
            problems.append(f"{tag} kind 非法: {e.get('kind')}")
        probe = e.get("probe")
        if not isinstance(probe, dict) or "kind" not in probe:
            problems.append(f"{tag} probe 缺 kind")
        for c in e.get("consumers") or []:
            if not isinstance(c, dict) or not c.get("path"):
                problems.append(f"{tag} consumer 缺 path")
            elif c.get("kind", "repo") not in CONSUMER_KINDS:
                problems.append(f"{tag} consumer kind 非法: {c.get('kind')}")
    excl = decl.get("exclusions", [])
    if not isinstance(excl, list):
        problems.append("exclusions 必须是列表")
    return problems


# ---------------------------------------------------------------- 闸门

def check_consumers(entry, ctx):
    issues = []
    for c in entry.get("consumers") or []:
        rel, token = c.get("path"), c.get("token")
        kind = c.get("kind", "repo")
        if kind == "external":
            issues.append(("EXTERNAL_UNCHECKED", f"{rel} (仓外消费者，仅登记不判)"))
            continue
        path = ctx.resolve(rel)
        if not os.path.exists(path):
            issues.append(("STALE_PATH", f"消费者文件不存在: {rel}"))
            continue
        if not token:
            continue
        txt = read_text(path)
        if txt is None:
            issues.append(("UNVERIFIABLE", f"消费者文件不可读: {rel}"))
            continue
        if token not in txt:
            issues.append(("TOKEN_GONE", f"{rel} 中已无 token: {token}"))
    return issues


def check_evidence(entry, ctx):
    issues = []
    ev = (entry.get("symptom") or {}).get("evidence") or []
    if not ev:
        issues.append(("EVIDENCE_NONE", "未登记症状证据行（提示级）"))
        return issues
    for item in ev:
        rel, line = item.get("path"), item.get("line")
        must = item.get("must_contain", "")
        path = ctx.resolve(rel)
        if not os.path.exists(path):
            issues.append(("EVIDENCE_GONE", f"证据文件不存在: {rel}"))
            continue
        lines = (read_text(path) or "").split("\n")
        if not isinstance(line, int) or line < 1 or line > len(lines):
            issues.append(("EVIDENCE_GONE", f"{rel}:{line} 越界（文件 {len(lines)} 行）"))
            continue
        if must and must not in lines[line - 1]:
            hit = [i + 1 for i, l in enumerate(lines) if must in l]
            if hit:
                issues.append(("EVIDENCE_LINE_DRIFT",
                               f"{rel}:{line} 无关键字，实际在 {hit[:3]}"))
            else:
                issues.append(("EVIDENCE_GONE", f"{rel} 全文无关键字: {must}"))
    return issues


def scan_coverage(decl, ctx):
    """扫仓库消费者的凭证 token，要求「已声明 或 显式排除」。"""
    declared = set()
    for e in decl.get("entries", []):
        name = (e.get("locator") or {}).get("name")
        if name:
            declared.add(name)
        for c in e.get("consumers") or []:
            if c.get("token"):
                declared.add(_token_leaf(c["token"]))
    excluded = {x.get("token") for x in decl.get("exclusions", []) if isinstance(x, dict)}

    if ctx.fixtures:
        files = []
        for root, _, fns in os.walk(ctx.fixtures):
            for fn in fns:
                if fn.endswith(".py"):
                    files.append(os.path.relpath(os.path.join(root, fn), ctx.fixtures))
    else:
        try:
            out = subprocess.run(["git", "ls-files", "*.py"], cwd=ctx.root,
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return [], ["git ls-files 失败（coverage 不可判）"]
        files = [f for f in out.split("\n") if f.strip()]

    findings, problems = [], []
    for rel in files:
        path = ctx.resolve(rel)
        txt = read_text(path, cap=2 << 20)
        if txt is None:
            continue
        for i, line in enumerate(txt.split("\n"), 1):
            for token, kind in _extract_tokens(line):
                status = ("DECLARED" if token in declared
                          else "EXCLUDED" if token in excluded
                          else "UNDECLARED")
                findings.append({"token": token, "kind": kind, "path": rel,
                                 "line": i, "status": status})
    undeclared = [f for f in findings if f["status"] == "UNDECLARED"]
    return findings, ([] if not undeclared else
                      [f"未声明且未排除的凭证 token {len(undeclared)} 处"])


def _token_leaf(token):
    """`keys.foo` / `mykeys.get("foo")` → foo（用于与 locator.name 对齐）。"""
    m = re.search(r'["\']([^"\']+)["\']', token or "")
    if m:
        return m.group(1)
    m = re.search(r"[.(]\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)?$", token or "")
    return m.group(1) if m else (token or "")


def _extract_tokens(line):
    out = []
    for m in re.finditer(r"(?<![A-Za-z0-9_])keys\.([A-Za-z_][A-Za-z0-9_]*)", line):
        nm = m.group(1)
        if nm not in KEYCHAIN_METHODS and not nm.startswith("_"):
            out.append((nm, "ga_keychain"))
    for m in re.finditer(r"mykeys(?:\.get)?[\(\[]\s*[\"']([^\"']+)[\"']", line):
        out.append((m.group(1), "mykey_config"))
    for m in re.finditer(r"os\.environ(?:\.get)?[\(\[]\s*[\"']([A-Z0-9_]+)[\"']", line):
        if CRED_NAME_RE.search(m.group(1)):
            out.append((m.group(1), "env"))
    return out


# ---------------------------------------------------------------- 命令

def build_rows(decl, ctx, only=None):
    rows = []
    for e in decl.get("entries", []):
        if only and e.get("id") not in only:
            continue
        pk = (e.get("probe") or {}).get("kind")
        probe_fn = PROBES.get(pk)
        if ctx.fx_missing:
            state, detail = "UNVERIFIABLE", f"fixture 根不存在: {ctx.fixtures}"
            probe_fn = None
        elif probe_fn is None:
            state, detail = "UNVERIFIABLE", f"未知 probe kind: {pk}"
        else:
            state, detail = probe_fn((e.get("locator") or {}), ctx)
        issues = []
        if state == "MISSING":
            if e.get("required", True):
                issues.append(("STORE_MISSING", "凭证在存储侧不存在"))
            else:
                issues.append(("STORE_MISSING_OPTIONAL", "可选凭证缺失（提示级）"))
        elif state == "UNVERIFIABLE":
            issues.append(("UNVERIFIABLE", detail))
        if not ctx.fx_missing:
            issues += check_consumers(e, ctx)
            issues += check_evidence(e, ctx)
        hard = [i for i in issues if i[0] in HARD_ISSUES]
        rows.append({
            "id": e.get("id"), "kind": e.get("kind"), "probe_kind": pk,
            "required": bool(e.get("required", True)),
            "store": state, "store_detail": detail,
            "issues": [{"code": c, "detail": d} for c, d in issues],
            "hard": len(hard), "verdict": _verdict(state, hard),
        })
    return rows


def _verdict(state, hard):
    if state == "UNVERIFIABLE" or any(c == "UNVERIFIABLE" for c, _ in hard):
        return "UNVERIFIABLE"
    if hard:
        return "DEGRADED"
    if state == "MISSING":
        return "OPTIONAL_MISSING"
    return "OK"


def cmd_plan(decl, ctx, args):
    rows = build_rows(decl, ctx, only=set(args.only.split(",")) if args.only else None)
    print(f"[plan] 声明 {len(rows)} 条（只跑探针，不判消费者/证据）")
    for r in rows:
        print(f"  {r['id']:<44} probe={r['probe_kind']:<20} store={r['store']:<13} {r['store_detail']}")
    return 0


def cmd_probe(decl, ctx, args):
    rows = build_rows(decl, ctx, only=set(args.only.split(",")) if args.only else None)
    out = {"mode": "probe", "rows": [{"id": r["id"], "store": r["store"],
                                      "detail": r["store_detail"]} for r in rows]}
    print(f"[probe] {len(rows)} 条")
    for r in rows:
        print(f"  {r['id']:<44} {r['store']:<13} {r['store_detail']}")
    _maybe_json(out, args.json)
    return 2 if any(r["store"] == "UNVERIFIABLE" for r in rows) else 0


def cmd_coverage(decl, ctx, args):
    findings, problems = scan_coverage(decl, ctx)
    agg = {}
    for f in findings:
        agg[(f["token"], f["status"])] = agg.get((f["token"], f["status"]), 0) + 1
    print(f"[coverage] 命中 token 事件 {len(findings)} 处，唯一 token {len({f['token'] for f in findings})} 个")
    for (tok, st), n in sorted(agg.items()):
        print(f"  {st:<10} {tok:<34} ×{n}")
    print(f"[coverage] 未声明/未排除：{sum(1 for f in findings if f['status'] == 'UNDECLARED')} 处")
    _maybe_json({"mode": "coverage", "findings": findings, "problems": problems}, args.json)
    return 1 if problems else 0


def cmd_judge(decl, ctx, args):
    rows = build_rows(decl, ctx)
    findings, cov_problems = scan_coverage(decl, ctx)
    undeclared = [f for f in findings if f["status"] == "UNDECLARED"]
    leaks = leak_scan(strip_env_paths(json.dumps({"rows": rows, "undeclared": undeclared},
                                                 ensure_ascii=False), ctx),
                      allow_fixture=bool(ctx.fixtures))
    unver = sum(1 for r in rows if r["verdict"] == "UNVERIFIABLE")
    hard_unver = sum(1 for r in rows if r["verdict"] == "UNVERIFIABLE")
    hard_other = (sum(r["hard"] for r in rows) - hard_unver) + len(undeclared) + leaks
    hard = hard_other + hard_unver
    print("[judge] 凭证面体检")
    print(f"  条目 {len(rows)}：OK {sum(1 for r in rows if r['verdict'] == 'OK')}"
          f" / DEGRADED {sum(1 for r in rows if r['verdict'] == 'DEGRADED')}"
          f" / UNVERIFIABLE {unver}"
          f" / OPTIONAL_MISSING {sum(1 for r in rows if r['verdict'] == 'OPTIONAL_MISSING')}")
    for r in rows:
        flag = "!!" if r["hard"] else "  "
        print(f"  {flag} {r['id']:<44} store={r['store']:<13} verdict={r['verdict']}")
        for it in r["issues"]:
            print(f"       - {it['code']}: {it['detail']}")
    print(f"  coverage: token 事件 {len(findings)}，UNDECLARED {len(undeclared)}")
    for f in undeclared[:20]:
        print(f"       - UNDECLARED {f['token']} ({f['kind']}) @ {f['path']}:{f['line']}")
    print(f"  泄漏守卫: hits={leaks}")
    out = {"mode": "judge", "rows": rows,
           "coverage": {"events": len(findings), "undeclared": undeclared},
           "leak_hits": leaks, "hard": hard, "hard_other": hard_other,
           "unverifiable": unver}
    _maybe_json(out, args.json)
    if leaks:
        print("[judge] FAIL: 输出疑似含凭证值（LEAK）")
        return 1
    if hard_other:
        print(f"[judge] FAIL：硬问题 {hard_other} 项")
        return 1
    if unver:
        print(f"[judge] FAIL-CLOSED：不可判 {unver} 条（rc=2）")
        return 2
    print("[judge] PASS")
    return 0


def cmd_proof(decl_path, ctx, args):
    """只读自证：对声明表 + 全部被读文件做 sha256 前后比对。"""
    files = [decl_path]
    decl = load_decl(decl_path)
    for e in decl.get("entries", []):
        for c in e.get("consumers") or []:
            if c.get("kind", "repo") == "repo":
                p = ctx.resolve(c["path"])
                if os.path.exists(p):
                    files.append(p)
        for it in (e.get("symptom") or {}).get("evidence") or []:
            p = ctx.resolve(it["path"])
            if os.path.exists(p):
                files.append(p)
    before = {p: sha256_file(p) for p in sorted(set(files))}
    rc = cmd_judge(decl, ctx, argparse.Namespace(json=None))
    after = {p: sha256_file(p) for p in sorted(set(files))}
    changed = [p for p in before if before[p] != after[p]]
    missing = [p for p in before if not os.path.exists(p)]
    print(f"[proof] files={len(before)} changed={changed} missing={missing}")
    return 1 if (changed or missing) else rc


def _maybe_json(obj, path):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"[json] → {path} ({os.path.getsize(path)}B)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="凭证→消费者映射与失效判据表（只读）")
    ap.add_argument("cmd", nargs="?", default="judge",
                    choices=["plan", "probe", "coverage", "judge", "proof", "selftest"])
    ap.add_argument("--decl", default=None)
    ap.add_argument("--root", default=GA_ROOT)
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--fixtures", default=None)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    decl_path = args.decl or os.path.join(args.root, DEFAULT_DECL)
    if not os.path.exists(decl_path):
        print(f"[error] 声明表不存在: {decl_path}", file=sys.stderr)
        return 3
    try:
        decl = load_decl(decl_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[error] 声明表无法解析: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    problems = validate_decl(decl)
    if problems:
        for p in problems:
            print(f"[schema] {p}", file=sys.stderr)
        return 3

    ctx = Ctx(root=args.root, fixtures=args.fixtures)
    try:
        if args.cmd == "plan":
            return cmd_plan(decl, ctx, args)
        if args.cmd == "probe":
            return cmd_probe(decl, ctx, args)
        if args.cmd == "coverage":
            return cmd_coverage(decl, ctx, args)
        if args.cmd == "proof":
            return cmd_proof(decl_path, ctx, args)
        return cmd_judge(decl, ctx, args)
    finally:
        ctx.cleanup()


# ---------------------------------------------------------------- selftest

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _mk_fixture(base):
    """造一套 fixture：全部用 FAKE-* 合成值，绝不碰真密钥。"""
    fx = os.path.join(base, "fixtures")
    _write(os.path.join(fx, "keychain_names.json"),
           json.dumps(["fixture_present_key"]))
    _write(os.path.join(fx, "macos_items.json"), json.dumps(["fixture_macos_item"]))
    _write(os.path.join(fx, "env.json"), json.dumps({"FIXTURE_TOKEN": "FAKE-SECRET-ENV"}))
    _write(os.path.join(fx, "config.py"),
           'fs_app_secret = "FAKE-SECRET-CFG"\n'
           'other_flag = "x"\n'
           'channel = {"bot_token": "FAKE-SECRET-DICT"}\n')
    _write(os.path.join(fx, "consumer.py"),
           "from keychain import keys\n"
           "from llmcore import mykeys\n"
           "import os\n"
           "A = keys.fixture_present_key.use()\n"
           'B = mykeys.get("fs_app_secret")\n'
           'C = os.environ.get("FIXTURE_TOKEN")\n')
    _write(os.path.join(fx, "notes.md"),
           "# fixture notes\n\nalpha\n"
           "第 4 行：登录态整体失效，需重新登录\n"
           "delta\n")
    db = os.path.join(fx, "cookies.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    con.execute("INSERT INTO cookies VALUES ('.fixture.example','_t','FAKE-SECRET-COOKIE')")
    con.execute("INSERT INTO cookies VALUES ('.fixture.example','other','x')")
    con.commit()
    con.close()

    decl = {
        "version": 1,
        "entries": [
            {"id": "ga_kc:fixture_present_key", "kind": "ga_keychain_name",
             "locator": {"name": "fixture_present_key"},
             "probe": {"kind": "ga_keychain_name"},
             "consumers": [{"path": "consumer.py", "token": "keys.fixture_present_key",
                            "kind": "repo"}],
             "symptom": {"desc": "fixture 症状", "evidence": [
                 {"path": "notes.md", "line": 4, "must_contain": "登录态整体失效"}]}},
            {"id": "macos:fixture_macos_item", "kind": "macos_keychain_item",
             "locator": {"name": "fixture_macos_item"},
             "probe": {"kind": "macos_keychain_item"},
             "consumers": [{"path": "consumer.py", "token": "keys.fixture_present_key"}],
             "symptom": {"desc": "fixture", "evidence": [
                 {"path": "notes.md", "line": 4, "must_contain": "登录态"}]}},
            {"id": "cfg:fs_app_secret", "kind": "config_var",
             "locator": {"path": "config.py", "name": "fs_app_secret"},
             "probe": {"kind": "config_var"},
             "consumers": [{"path": "consumer.py", "token": 'mykeys.get("fs_app_secret"'}],
             "symptom": {"desc": "fixture"}},
            {"id": "env:FIXTURE_TOKEN", "kind": "env_var",
             "locator": {"name": "FIXTURE_TOKEN"},
             "probe": {"kind": "env_var"},
             "consumers": [{"path": "consumer.py", "token": 'os.environ.get("FIXTURE_TOKEN"'}]},
            {"id": "cookie:fixture:_t", "kind": "browser_cookie",
             "locator": {"host": ".fixture.example", "name": "_t", "profile": "Default"},
             "probe": {"kind": "browser_cookie"},
             "consumers": [{"path": "consumer.py", "token": "keys.fixture_present_key"}],
             "symptom": {"desc": "fixture"}},
            {"id": "opt:optional_missing", "kind": "env_var",
             "locator": {"name": "FIXTURE_OPTIONAL_ABSENT"},
             "probe": {"kind": "env_var"}, "required": False, "consumers": []},
        ],
        "exclusions": [{"token": "other_flag", "reason": "非凭证"}],
    }
    decl_path = os.path.join(base, "decl.json")
    _write(decl_path, json.dumps(decl, ensure_ascii=False, indent=2))
    return fx, decl_path


def _run(argv, cwd=None):
    """内部以子进程跑 CLI，返回 (rc, stdout)。"""
    r = subprocess.run([sys.executable, os.path.abspath(__file__)] + argv,
                       capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout + r.stderr


def _run_inproc(fx, decl_path, extra=None, root=None):
    argv = ["judge", "--decl", decl_path, "--fixtures", fx,
            "--root", root or fx]
    if extra:
        argv += extra
    return _run(argv)


def selftest():
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    base = tempfile.mkdtemp(prefix="cred_surface_selftest_")
    try:
        fx, decl_path = _mk_fixture(base)

        # --- 正控：干净 fixture 必须 PASS
        rc, out = _run_inproc(fx, decl_path)
        check("P1 干净 fixture judge rc=0", rc == 0, f"rc={rc}")
        check("P2 PASS 字样", "PASS" in out, out.strip().split("\n")[-1:])

        # --- NC1 删消费者文件 → STALE_PATH rc=1
        d = os.path.join(base, "nc1"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        os.remove(os.path.join(d, "fixtures", "consumer.py"))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), os.path.join(d, "decl.json"))
        check("NC1 删消费者→STALE_PATH rc=1", rc == 1 and "STALE_PATH" in out, f"rc={rc}")

        # --- NC2 消费者里去掉 token → TOKEN_GONE
        d = os.path.join(base, "nc2"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        p = os.path.join(d, "fixtures", "consumer.py")
        _write(p, open(p, encoding="utf-8").read().replace("keys.fixture_present_key.use()", "None"))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), os.path.join(d, "decl.json"))
        check("NC2 token 消失→TOKEN_GONE rc=1", rc == 1 and "TOKEN_GONE" in out, f"rc={rc}")

        # --- NC3 删证据文件 → EVIDENCE_GONE
        d = os.path.join(base, "nc3"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        os.remove(os.path.join(d, "fixtures", "notes.md"))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), os.path.join(d, "decl.json"))
        check("NC3 删证据→EVIDENCE_GONE rc=1", rc == 1 and "EVIDENCE_GONE" in out, f"rc={rc}")

        # --- NC4 虚构凭证名 → STORE_MISSING rc=1（且必须被点名，不得静默）
        d = os.path.join(base, "nc4"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        dp = os.path.join(d, "decl.json")
        j = json.load(open(dp, encoding="utf-8"))
        j["entries"].append({"id": "ga_kc:does_not_exist", "kind": "ga_keychain_name",
                             "locator": {"name": "does_not_exist"},
                             "probe": {"kind": "ga_keychain_name"}, "consumers": []})
        _write(dp, json.dumps(j, ensure_ascii=False))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), dp)
        check("NC4 虚构凭证名被点名 rc=1", rc == 1 and "does_not_exist" in out and "STORE_MISSING" in out,
              f"rc={rc}")

        # --- NC5 未知 probe kind → UNVERIFIABLE rc=2（fail-closed）
        d = os.path.join(base, "nc5"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        dp = os.path.join(d, "decl.json")
        j = json.load(open(dp, encoding="utf-8"))
        j["entries"][0]["probe"] = {"kind": "telepathy"}
        _write(dp, json.dumps(j, ensure_ascii=False))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), dp)
        check("NC5 未知探针→UNVERIFIABLE rc=2", rc == 2 and "UNVERIFIABLE" in out, f"rc={rc}")

        # --- NC6 未声明的凭证 token → UNDECLARED rc=1
        d = os.path.join(base, "nc6"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        p = os.path.join(d, "fixtures", "consumer.py")
        _write(p, open(p, encoding="utf-8").read() + 'D = mykeys.get("undeclared_secret")\n')
        rc, out = _run_inproc(os.path.join(d, "fixtures"), os.path.join(d, "decl.json"))
        check("NC6 未声明 token→UNDECLARED rc=1", rc == 1 and "UNDECLARED" in out, f"rc={rc}")

        # --- NC7 证据行漂移 → EVIDENCE_LINE_DRIFT
        d = os.path.join(base, "nc7"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        dp = os.path.join(d, "decl.json")
        j = json.load(open(dp, encoding="utf-8"))
        j["entries"][0]["symptom"]["evidence"][0]["line"] = 1
        _write(dp, json.dumps(j, ensure_ascii=False))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), dp)
        check("NC7 行漂移→EVIDENCE_LINE_DRIFT rc=1",
              rc == 1 and "EVIDENCE_LINE_DRIFT" in out, f"rc={rc}")

        # --- NC8 关键字全文不存在 → EVIDENCE_GONE
        d = os.path.join(base, "nc8"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        dp = os.path.join(d, "decl.json")
        j = json.load(open(dp, encoding="utf-8"))
        j["entries"][0]["symptom"]["evidence"][0]["must_contain"] = "从未出现过的词"
        _write(dp, json.dumps(j, ensure_ascii=False))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), dp)
        check("NC8 关键字缺失→EVIDENCE_GONE rc=1",
              rc == 1 and "EVIDENCE_GONE" in out, f"rc={rc}")

        # --- NC9 泄漏守卫：密钥值必报、凭证名不得误报
        check("NC9a 泄漏守卫抓真值", leak_scan("token=sk-abcdefghijklmnopqrstuvwx"),
              "sk- 样本")
        check("NC9b 泄漏守卫不误报名字", leak_scan("keys.haisou_hs_web_session") == 0,
              "纯标识符")
        check("NC9c fixture 假值豁免", leak_scan("FAKE-SECRET-ENV", allow_fixture=True) == 0,
              "allow_fixture")

        # --- NC10 可选凭证缺失不构成硬问题
        rc, out = _run_inproc(fx, decl_path)
        check("NC10 可选缺失不硬失败", "OPTIONAL_MISSING" in out and rc == 0, f"rc={rc}")

        # --- NC11 排除项抑制 coverage
        d = os.path.join(base, "nc11"); shutil.copytree(fx, os.path.join(d, "fixtures")); shutil.copy(decl_path, d)
        dp = os.path.join(d, "decl.json")
        j = json.load(open(dp, encoding="utf-8"))
        j["entries"][2]["locator"]["name"] = "fs_app_secret_MISSING"   # 让 config_var 变 MISSING
        j["exclusions"] = []                                          # 去掉排除 → 应报 UNDECLARED
        _write(dp, json.dumps(j, ensure_ascii=False))
        rc, out = _run_inproc(os.path.join(d, "fixtures"), dp)
        check("NC11 排除项移除后报 UNDECLARED rc=1",
              rc == 1 and "UNDECLARED" in out, f"rc={rc}")

        # --- NC12 fixture 缺失 → fail-closed rc=2
        d = os.path.join(base, "nc12"); os.makedirs(d)
        shutil.copy(decl_path, d)
        rc, out = _run_inproc(os.path.join(d, "no_such_fixtures"), os.path.join(d, "decl.json"))
        check("NC12 fixture 缺失→rc=2", rc == 2, f"rc={rc}")

        # --- NC13 声明表结构错误 → rc=3
        d = os.path.join(base, "nc13"); os.makedirs(d)
        bad = os.path.join(d, "bad.json")
        _write(bad, json.dumps({"entries": [{"id": "x", "kind": "nope",
                                             "locator": {}, "probe": {}, "consumers": []}]}))
        rc, out = _run_inproc(fx, bad)
        check("NC13 结构错误→rc=3", rc == 3, f"rc={rc}")

        # --- NC14 coverage 必须真扫到语料（防静默缩水）
        rc, out = _run(["coverage", "--decl", decl_path, "--fixtures", fx,
                        "--root", os.path.dirname(fx)])
        check("NC14 coverage 自证语料非空", rc in (0, 1) and "命中 token 事件" in out
              and "×" in out, f"rc={rc}")

        # --- NC15 只读自证：judge 前后源文件 sha 不变
        before = sha256_file(os.path.join(fx, "consumer.py"))
        _run_inproc(fx, decl_path)
        check("NC15 只读（源文件 sha 不变）",
              sha256_file(os.path.join(fx, "consumer.py")) == before, "sha256")

        # --- NC16 报告输出不含真密钥（正控：judge stdout 无 LEAK 字样）
        rc, out = _run_inproc(fx, decl_path)
        check("NC16 stdout 无泄漏告警", "LEAK" not in out, "")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"=== credential_surface selftest: {passed}/{len(results)} passed ===")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if not ok else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
