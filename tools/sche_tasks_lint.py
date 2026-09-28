#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sche_tasks prompt 静态体检器（R469 / temp/TODO.txt L180，第20批 C3）

只读静态 linter：对 `sche_tasks/*.json` 的 `prompt` 做五个维度的**可回源**体检，
输出逐条判据 + 结论表。不改任何 prompt、不重立 R375 的 GA_ROOT 修法（该修法仍待批）。

五查维度
--------
1. cwd   —— prompt 里的相对路径（`./x`、`../x`、`bare/x`）是否与执行 cwd 解耦：
            无 `GA_ROOT` 锚定行时，同一 token 在不同 cwd 下解析到**不同目标**即判 CWD_DRIFT。
            权威 cwd 取 launchd `WorkingDirectory`（= 仓库根）；对照面另取 仓库根/temp 与 $HOME。
2. path  —— 引用的路径/脚本是否真实存在（相对 token 按权威 cwd=仓库根 解析）。
3. flag  —— prompt 命令行里传给某 `.py` 的 `--flag` 是否真在该脚本的 argparse 里定义
            （AST 抽 add_argument；仅在脚本确实 import argparse 时判，否则记 FLAG_UNCHECKED）。
4. 超时  —— 被长生脚本（内置 registry，附证据）是否在 prompt 里给了超时/后台化的提示。
            另记录 JSON 侧是否显式声明 `max_delay_hours`（缺省由 scheduler 用默认值）。
5. 通道  —— 通知通道词是否在白名单内；声明的通道与脚本内的通道标记是否相容；
            只说「通知」不说通道则记 CHANNEL_UNSPECIFIED（提示级）。

口径与边界（诚实披露）
--------------------
* 本器只做**静态**判据：能证明「引用的东西不存在 / flag 不存在 / 相对路径吃 cwd」，
  不能证明任务业务正确性；对「由 agent 侧（LLM 按 SOP）执行的推送」只能记 INFO。
* 相对 token 的“不存在”是在**权威 cwd**下判定的；换 cwd 可能碰巧命中（正是耦合的形态）。
* 只读自证：读取的每个文件在建表时与结束时各算一次 sha256，比对输出 changed 列表。

用法
----
    python3 tools/sche_tasks_lint.py                      # 扫描 <repo>/sche_tasks/*.json
    python3 tools/sche_tasks_lint.py --only weekly_report_reminder --sandbox
    python3 tools/sche_tasks_lint.py --json temp/out.json
    python3 tools/sche_tasks_lint.py --root R --tasks-dir D  # 沙盒/负控用
    python3 tools/sche_tasks_lint.py --selftest           # 21 组负控/正控自测

退出码：0 = 无 FAIL；1 = 存在 FAIL；2 = 用法或内部错误。
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import shutil
import sys

# ---------------------------------------------------------------- 常量 / 注册表

PATH_ROOTS = (
    "temp", "memory", "tools", "sche_tasks", "logs", "output", "reflect",
    "reflect_scripts", "frontline", "frontends", "docs",
    "birdie_monitor", "project_update_monitor",
)

ABS_RE = re.compile(r"/Users/[A-Za-z0-9_.\-/]+")
#: 边界一律用 ASCII 类——中文也属 \w，用 \w 做 lookbehind 会静默漏配
ASCII_LEFT = r"(?<![A-Za-z0-9_./\-])"
REL_RE = re.compile(
    ASCII_LEFT + r"(?:\.{1,2}/)?(?:%s)/[A-Za-z0-9_.\-]*" % "|".join(PATH_ROOTS)
)
BARE_PY_RE = re.compile(ASCII_LEFT + r"([A-Za-z0-9_\-]+\.(?:py|sh))(?![\w.\-])")

#: 长生任务脚本 registry（每条附证据出处，来自耐久记忆/SOP；仅作提示级判据）
LONG_RUNNING = {
    "sche_tasks/run_trendradar_digest.py": "trendradar_sop：单次可达 10+ 分钟，须后台起 + 轮询",
    "project_update_monitor/monitor.py": "project_update_monitor_sop：单轮 5-15 分钟（逐源 codex 翻译）",
    "birdie_monitor/haisou_daily.py": "haisou_monitor_sop：网络抓取 + 额度限流，须后台起",
    "temp/run_wechat_ocr_daily.py": "wechat_signals_sop：OCR 截图链路长跑",
}

#: 通知通道白名单（词面 → 规范名）
CHANNELS = {
    "飞书私信": "feishu_dm",
    "飞书群": "feishu_group",
    "钉钉群": "dingtalk_group",
    "钉钉": "dingtalk",
}
#: 脚本内通道标记（用于「声明 ↔ 实现」相容性）
IMPL_MARKERS = {
    "feishu": ("飞书", "feishu", "lark"),
    "dingtalk": ("钉钉", "dingtalk"),
}
TIMEOUT_HINTS = ("超时", "timeout", "后台", "nohup", "bg_task", "disown", "轮询", "start_new_session")

#: basename 索引要跳过的沙盒/大目录（含 R375 演练副本，会造成同名二义）
SKIP_DIR_PARTS = ("/.git", "/node_modules", "/coupling_lab", "/kc_snapshots",
                  "/chrome_snapshots", "/pmset_snapshots", "/model_responses",
                  "/deepdive_v2", "/t0_backups", "/__pycache__")

CWD_SANDBOXES = ("repo_root", "repo_temp", "home")


# ---------------------------------------------------------------- 基础工具

def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


class ReadProof:
    """只读自证：记录读取过的文件 sha256，结束前复算。"""

    def __init__(self):
        self.before = {}

    def note(self, path):
        if path and os.path.isfile(path) and path not in self.before:
            self.before[path] = sha256_file(path)

    def verify(self):
        changed = []
        for p, h0 in self.before.items():
            h1 = sha256_file(p)
            if h1 != h0:
                changed.append(p)
        return len(self.before), sorted(changed)


def _strip_edges(tok: str) -> str:
    return tok.rstrip(".,;:)]}`'\"。，；：）】」』")


def extract_path_tokens(prompt: str):
    """→ [{'tok','kind'}]  kind ∈ {abs, rel}；跳过 URL 内部片段。"""
    out, seen = [], set()
    for m in ABS_RE.finditer(prompt):
        head = prompt[max(0, m.start() - 8):m.start()]
        if "://" in head:
            continue
        tok = _strip_edges(m.group(0))
        if tok and tok not in seen:
            seen.add(tok)
            out.append({"tok": tok, "kind": "abs", "end": m.start() + len(tok)})
    for m in REL_RE.finditer(prompt):
        head = prompt[max(0, m.start() - 8):m.start()]
        if "://" in head:
            continue
        tok = _strip_edges(m.group(0))
        if not tok or tok.endswith("/"):
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append({"tok": tok, "kind": "rel", "end": m.start() + len(tok)})
    return out


def extract_script_refs(prompt: str):
    """→ [{'raw','bare'}]：带路径的 .py/.sh，以及无路径的裸脚本名。"""
    refs, seen = [], set()
    for t in extract_path_tokens(prompt):
        if t["tok"].endswith((".py", ".sh")):
            if t["tok"] not in seen:
                seen.add(t["tok"])
                refs.append({"raw": t["tok"], "bare": os.path.basename(t["tok"])})
    for m in BARE_PY_RE.finditer(prompt):
        name = m.group(1)
        if name in seen:
            continue
        # 若该出现点已在某个路径 token 内部，跳过
        if any(p["raw"].endswith("/" + name) and p["raw"] in prompt for p in refs):
            continue
        seen.add(name)
        refs.append({"raw": name, "bare": name})
    return refs


def build_basename_index(root: str, max_depth: int = 4):
    """仓库内脚本名 → 路径列表（跳过沙盒/大目录，防同名二义）。"""
    idx = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and
                           not any((os.path.join(dirpath, d)).endswith(part) or part.lstrip("/") == d
                                   for part in SKIP_DIR_PARTS)]
        for fn in filenames:
            if fn.endswith((".py", ".sh")):
                idx.setdefault(fn, []).append(os.path.join(dirpath, fn))
    return idx


def argparse_flags(path: str):
    """→ (flags:set|None, has_argparse:bool)。None = 读不了。"""
    try:
        src = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None, False
    has = ("import argparse" in src) or ("from argparse" in src)
    flags = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set(), has
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and a.value.startswith("-"):
                    flags.add(a.value)
    return flags, has


def resolve_under(cwd: str, tok: str) -> str:
    return os.path.normpath(os.path.join(cwd, tok))


def sandbox_targets(tok: str, root: str, home: str):
    """相对 token 在三个 cwd 沙盒下的解析结果（R375 口径）。"""
    cwds = {"repo_root": root, "repo_temp": os.path.join(root, "temp"), "home": home}
    return {name: (resolve_under(c, tok), os.path.exists(resolve_under(c, tok)))
            for name, c in cwds.items()}


def follow_char(prompt: str, end):
    """token 结束处的下一个字符（判是否被模板变量/CJK 截断）。"""
    if end is None or end >= len(prompt):
        return ""
    return prompt[end]


def is_template_truncated(prompt: str, end) -> bool:
    """token 后紧跟 $ { ( 或非 ASCII ⇒ 只是模板/中文占位前缀，非完整路径断言。"""
    fc = follow_char(prompt, end)
    if not fc:
        return False
    return fc in "$({[<" or ord(fc) > 127


def has_sibling_prefix(tok: str, limit: int = 6) -> str:
    """tok 不存在但 tok* 有兄弟匹配（绝对路径被空格/中文截断）⇒ 返回首个匹配。"""
    if len(tok) < 4 or not os.path.isdir(os.path.dirname(tok) or "/"):
        return ""
    try:
        hits = sorted(glob.glob(tok + "*"))[:limit]
    except Exception:
        return ""
    hits = [h for h in hits if h != tok]
    return hits[0] if hits else ""


def prompt_lines(prompt: str):
    return prompt.split("\n")


FLAG_RE = re.compile(r"(?<![\w-])(--?[A-Za-z][A-Za-z0-9\-]*)")

# 子句切分（仅用于 flag 归属）：中英标点 + 连续空白
CLAUSE_SEP_RE = re.compile(r"[，。；、（）()【】\[\]{}：:!！?？\"“”'‘’|]+|\s{2,}")


def split_clauses(ln: str):
    """把一行切成子句（保留原序，不跨标点）⇒ flag 只归同子句内且在其前的脚本。"""
    out, start = [], 0
    for m in CLAUSE_SEP_RE.finditer(ln):
        if m.start() > start:
            out.append(ln[start:m.start()])
        start = m.end()
    if start < len(ln):
        out.append(ln[start:])
    return out


# ---------------------------------------------------------------- 体检

def _add(findings, check, code, severity, detail, task=None):
    findings.append({"task": task, "check": check, "code": code,
                     "severity": severity, "detail": detail})


def lint_task(tid, data, root, home, idx, proof):
    """→ (verdict, findings, info)"""
    F = []
    info = {"id": tid, "enabled": bool(data.get("enabled")),
            "repeat": data.get("repeat"), "schedule": data.get("schedule"),
            "max_delay_hours": data.get("max_delay_hours"),
            "prompt_bytes": len(data.get("prompt") or ""), "path_tokens": []}
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        _add(F, "prompt", "MISSING_PROMPT", "FAIL", "prompt 缺失或为空", tid)
        info["verdict"] = "FAIL"
        info["findings"] = F
        info["codes"] = sorted({f["code"] for f in F})
        return info

    anchored = bool(re.search(r"GA_ROOT", prompt))
    tokens = extract_path_tokens(prompt)
    refs = extract_script_refs(prompt)

    # --- 1. cwd：相对 token 的 cwd 依赖 ------------------------------------
    rel_tokens = [t["tok"] for t in tokens if t["kind"] == "rel"]
    for tok in rel_tokens:
        targets = sandbox_targets(tok, root, home)
        info["path_tokens"].append({"tok": tok,
                                    "targets": {k: v[0] for k, v in targets.items()},
                                    "exists_at_repo_root": targets["repo_root"][1]})
        distinct = {os.path.realpath(v[0]) for v in targets.values()}
        if len(distinct) > 1:
            if anchored:
                _add(F, "cwd", "CWD_ANCHORED", "INFO",
                     "相对路径 %s 吃 cwd，但 prompt 含 GA_ROOT 锚定行" % tok, tid)
            else:
                _add(F, "cwd", "CWD_DRIFT", "WARN",
                     "相对路径 %s 无锚定：repo 根→%s / repo/temp→%s" %
                     (tok, targets["repo_root"][0], targets["repo_temp"][0]), tid)

    # --- 2. path：存在性 ---------------------------------------------------
    # 分级口径：显式锚定（./ ../）缺失 = FAIL；**裸**相对名可能指向别的仓库/别的 cwd
    # （实测 docs/analysis 属 bar-smyze 仓）⇒ 只报 WARN，不假 FAIL。
    for t in tokens:
        if t["kind"] != "rel":
            continue
        tok = t["tok"]
        p = resolve_under(root, tok)
        if os.path.exists(p):
            continue
        if tok.startswith("./") or tok.startswith("../"):
            _add(F, "path", "PATH_MISSING", "FAIL",
                 "相对路径 %s 在权威 cwd(%s) 下不存在：%s" % (tok, root, p), tid)
        else:
            sib = has_sibling_prefix(p)
            if sib:
                _add(F, "path", "PATH_PREFIX", "INFO",
                     "相对名 %s 在仓库根不存在，但 %s 存在（原文可能被空格/中文截断）" %
                     (tok, os.path.relpath(sib, root)), tid)
            else:
                _add(F, "path", "REL_BARE_UNRESOLVED", "WARN",
                     "裸相对名 %s 在仓库根(%s)不存在；未加 ./ 或 ../ 锚定，"
                     "可能指向他仓/他 cwd（实测 docs/analysis 属 bar-smyze 仓），"
                     "静态不可判" % (tok, root), tid)
    for t in tokens:
        if t["kind"] != "abs":
            continue
        tok = t["tok"]
        if os.path.exists(tok):
            continue
        if is_template_truncated(prompt, t.get("end")):
            _add(F, "path", "PATH_TEMPLATE", "INFO",
                 "绝对路径片段 %s 紧跟模板变量/中文占位，非完整路径断言：…%s" %
                 (tok, prompt[t["end"]:t["end"] + 12].split("\n")[0]), tid)
            continue
        sib = has_sibling_prefix(tok)
        if sib:
            _add(F, "path", "PATH_PREFIX", "INFO",
                 "绝对路径 %s 不存在，但 %s 存在（原文被空格/中文截断）" % (tok, sib), tid)
            continue
        if tok.startswith(os.path.abspath(root) + os.sep):
            _add(F, "path", "PATH_MISSING", "FAIL", "仓库内绝对路径不存在：%s" % tok, tid)
        else:
            _add(F, "path", "PATH_OUT_OF_REPO_MISSING", "WARN",
                 "仓外绝对路径当下不存在（可能由任务运行时创建）：%s" % tok, tid)

    # --- 3. flag：脚本 argparse ------------------------------------------
    resolved_scripts = {}
    for r in refs:
        raw = r["raw"]
        cands = []
        if raw.startswith("/"):
            cands = [raw]
        elif "/" in raw:
            cands = [resolve_under(root, raw)]
        else:
            cands = list(idx.get(raw, []))
        cands = [c for c in cands if not any(part in c for part in SKIP_DIR_PARTS)]
        if len(cands) == 1:
            resolved_scripts[raw] = cands[0]
        elif len(cands) > 1:
            non_temp = [c for c in cands if not c.startswith(os.path.join(root, "temp") + os.sep)]
            pick = (non_temp or cands)[0]
            resolved_scripts[raw] = pick
            _add(F, "flag", "SCRIPT_AMBIGUOUS", "INFO",
                 "脚本名 %s 命中多份，取 %s（其余：%s）" %
                 (raw, os.path.relpath(pick, root),
                  ", ".join(os.path.relpath(c, root) for c in cands if c != pick)), tid)
        else:
            sev = "FAIL" if raw.startswith("/") else "WARN"
            _add(F, "flag", "SCRIPT_UNRESOLVED", sev,
                 "脚本引用无法解析：%s" % raw, tid)
    for raw, path in resolved_scripts.items():
        proof.note(path)

    # flag 归属：按「子句」就近（同子句内、且位于 flag 之前的脚本引用才算归属）
    # 反例（实测）：同一行里既有 `python3 ../temp/get_week_range.py` 又有散文句
    # 「后续 git log 的 --since/--until」⇒ 按整行归属会把 git 的 flag 算到
    # 脚本头上 = 假 FLAG_UNKNOWN。
    flags_by_script = {}
    orphans = []
    for ln in prompt_lines(prompt):
        for cl in split_clauses(ln):
            for m in FLAG_RE.finditer(cl):
                f, pos = m.group(1), m.start()
                hit, best = None, -1
                for raw, path in resolved_scripts.items():
                    for tok in (raw, os.path.basename(path)):
                        p = cl.find(tok)
                        if 0 <= p < pos and p > best:
                            best, hit = p, path
                if hit is None:
                    orphans.append(f)
                else:
                    flags_by_script.setdefault(hit, set()).add(f)
    for hit, flags in flags_by_script.items():
        defined, has_argparse = argparse_flags(hit)
        if defined is None:
            _add(F, "flag", "FLAG_UNCHECKED", "INFO", "脚本不可读：%s" % hit, tid)
            continue
        if not has_argparse:
            _add(F, "flag", "FLAG_UNCHECKED", "INFO",
                 "%s 未使用 argparse，%s 无法静态核验" %
                 (os.path.relpath(hit, root), "/".join(sorted(flags))), tid)
            continue
        for f in sorted(flags):
            if f not in defined:
                _add(F, "flag", "FLAG_UNKNOWN", "FAIL",
                     "%s 的 argparse 无此 flag：%s（已定义：%s）" %
                     (os.path.relpath(hit, root), f,
                      ",".join(sorted(defined)) or "<none>"), tid)
    if orphans:
        _add(F, "flag", "FLAG_UNATTRIBUTED", "INFO",
             "未归属到仓库脚本的 flag（可能属外部 CLI，如 lark-cli）：%s" %
             ",".join(sorted(set(orphans))), tid)

    # --- 4. 超时 ----------------------------------------------------------
    if data.get("max_delay_hours") is None:
        _add(F, "timeout", "MAX_DELAY_DEFAULT", "INFO",
             "未声明 max_delay_hours ⇒ scheduler 用默认 6h 窗口", tid)
    have_hint = any(h in prompt for h in TIMEOUT_HINTS)
    for raw, path in resolved_scripts.items():
        relp = os.path.relpath(path, root).replace(os.sep, "/")
        if relp in LONG_RUNNING and not have_hint:
            _add(F, "timeout", "TIMEOUT_UNDECLARED", "WARN",
                 "长生脚本 %s（%s）但 prompt 未给超时/后台化提示" %
                 (relp, LONG_RUNNING[relp]), tid)

    # --- 5. 通道 ----------------------------------------------------------
    found_ch = [(k, v) for k, v in CHANNELS.items() if k in prompt]
    for k, _ in found_ch:
        pass
    unknown_like = re.findall(r"(微信群|企业微信|短信|邮件通知)", prompt)
    for u in sorted(set(unknown_like)):
        _add(F, "channel", "CHANNEL_UNKNOWN", "WARN",
             "出现非白名单通道词：%s（白名单：%s）" % (u, "/".join(CHANNELS)), tid)
    mentions_push = bool(re.search(r"通知|推送", prompt))
    if mentions_push and not found_ch:
        _add(F, "channel", "CHANNEL_UNSPECIFIED", "WARN",
             "prompt 提到推送/通知但未指明通道", tid)
    if found_ch:
        wants = set()
        for k, cname in found_ch:
            if cname.startswith("feishu"):
                wants.add("feishu")
            else:
                wants.add("dingtalk")
        impl = set()
        blob = "\n".join(open(p, encoding="utf-8", errors="ignore").read()
                         for p in resolved_scripts.values()
                         if os.path.isfile(p)) if resolved_scripts else ""
        for key, markers in IMPL_MARKERS.items():
            if any(m in blob for m in markers):
                impl.add(key)
        if impl and not (wants & impl):
            _add(F, "channel", "CHANNEL_MISMATCH", "WARN",
                 "prompt 声明通道 %s，但脚本内标记仅 %s" %
                 (",".join(sorted(wants)), ",".join(sorted(impl))), tid)
        if not impl:
            _add(F, "channel", "CHANNEL_AGENT_SIDE", "INFO",
                 "声明通道 %s，脚本内无通道标记 ⇒ 由 agent 侧通知，静态不可核验" %
                 ",".join(sorted(wants)), tid)

    sev_rank = {"FAIL": 3, "WARN": 2, "INFO": 1}
    verdict = "OK"
    if F:
        worst = max(sev_rank[f["severity"]] for f in F)
        verdict = {3: "FAIL", 2: "WARN", 1: "OK"}[worst]
    info["verdict"] = verdict
    info["findings"] = F
    info["codes"] = sorted({f["code"] for f in F})
    return info


def lint_all(root, tasks_dir, only=None, want_disabled_dir=True):
    proof = ReadProof()
    idx = build_basename_index(root)
    home = os.path.expanduser("~")
    results = []
    files = sorted(f for f in os.listdir(tasks_dir) if f.endswith(".json"))
    for f in files:
        tid = f[:-5]
        if only and tid not in only:
            continue
        path = os.path.join(tasks_dir, f)
        proof.note(path)
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            results.append({"id": tid, "enabled": None, "verdict": "FAIL",
                            "codes": ["JSON_PARSE_ERROR"],
                            "findings": [{"task": tid, "check": "prompt",
                                          "code": "JSON_PARSE_ERROR", "severity": "FAIL",
                                          "detail": "JSON 解析失败：%s" % e}]})
            continue
        if not data.get("enabled", False):
            results.append({"id": tid, "enabled": False, "verdict": "SKIP",
                            "codes": ["SKIPPED_DISABLED"], "findings": []})
            continue
        results.append(lint_task(tid, data, root, home, idx, proof))
    files_n, changed = proof.verify()
    summary = {"tasks": len(results),
               "FAIL": sum(1 for r in results if r["verdict"] == "FAIL"),
               "WARN": sum(1 for r in results if r["verdict"] == "WARN"),
               "OK": sum(1 for r in results if r["verdict"] == "OK"),
               "SKIP": sum(1 for r in results if r["verdict"] == "SKIP"),
               "readonly_files": files_n, "readonly_changed": changed}
    return results, summary


# ---------------------------------------------------------------- 输出

def print_report(results, summary, root, tasks_dir, show_sandbox=False):
    print("sche_tasks prompt 静态体检｜root=%s｜tasks_dir=%s" % (root, tasks_dir))
    print("在册(顶层 json) %d：FAIL %d / WARN %d / OK %d / SKIP(disabled) %d" %
          (summary["tasks"], summary["FAIL"], summary["WARN"], summary["OK"], summary["SKIP"]))
    print("-" * 96)
    for r in results:
        if r["verdict"] == "SKIP":
            continue
        print("[%-4s] %-28s %s" % (r["verdict"], r["id"], ",".join(r.get("codes", []))))
        for f in r.get("findings", []):
            print("        - %-6s %-24s %s" % (f["severity"], f["code"], f["detail"]))
        if show_sandbox:
            for t in r.get("path_tokens", []):
                tgt = t["targets"]
                if len({v for v in tgt.values()}) > 1:
                    print("        ~ sandbox %s => repo_root=%s | repo_temp=%s | home=%s" %
                          (t["tok"], tgt["repo_root"], tgt["repo_temp"], tgt["home"]))
    print("-" * 96)
    print("只读自证：files=%d changed=%s" % (summary["readonly_files"], summary["readonly_changed"]))
    print("verdict: %s" % ("FAIL" if summary["FAIL"] else "PASS"))


# ---------------------------------------------------------------- selftest

SELFTEST_DIR = None


def _write(p, content):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fp:
        fp.write(content)


def run_selftest():
    """23 组断言：21 负控 + 正控 + 只读自证。"""
    base = os.path.join(os.getcwd(), "temp", "sche_tasks_lint_selftest")
    if os.path.isdir(base):
        shutil.rmtree(base)
    root = os.path.join(base, "repo")
    tasks = os.path.join(root, "sche_tasks")
    home = os.path.join(base, "fakehome")
    os.makedirs(os.path.join(root, "temp"), exist_ok=True)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(home, "temp"), exist_ok=True)
    _write(os.path.join(root, "temp", "good.py"),
           "import argparse\np=argparse.ArgumentParser()\np.add_argument('--alpha')\n"
           "p.add_argument('-b','--beta')\n")
    _write(os.path.join(root, "temp", "noargs.py"), "print('no cli options')\n")
    _write(os.path.join(home, "temp", "elsewhere.py"), "# only under home/temp\n")
    # PATH_PREFIX 夹具：目录名含空格 ⇒ 绝对路径 token 会被空格截断
    os.makedirs(os.path.join(root, "temp", "notes dir"), exist_ok=True)
    _write(os.path.join(root, "temp", "notes dir", "x.md"), "#\n")
    os.makedirs(os.path.join(root, "temp", "dpdomain_renew"), exist_ok=True)

    cases = []            # (tid, json_obj, expect_codes, expect_absent)
    def T(tid, prompt, expect, absent=(), **extra):
        d = {"enabled": True, "repeat": "daily", "schedule": "00:00", "prompt": prompt}
        d.update(extra)
        cases.append((tid, d, expect, absent))

    NC = "NC"
    # NC1 不存在路径（负数：期望必报）
    T(NC + "1_missing_path", "运行 python3 ./temp/definitely_missing.py --alpha", ["PATH_MISSING"])
    # NC2 未知 flag（脚本存在且有 argparse）
    T(NC + "2_unknown_flag", "运行 python3 temp/good.py --gamma", ["FLAG_UNKNOWN"])
    # NC3 干净 prompt：绝对路径 + 已知 flag ⇒ 不应有任何 FAIL/WARN
    T(NC + "3_clean", "运行 python3 %s/temp/good.py --alpha" % root, [], ["PATH_MISSING", "FLAG_UNKNOWN"])
    # NC4 相对路径 + GA_ROOT 锚定 ⇒ 不判 CWD_DRIFT（也不应 PATH_MISSING，因 good.py 存在）
    T(NC + "4_anchored", "GA_ROOT=...; cd $GA_ROOT\n运行 python3 temp/good.py --alpha", ["CWD_ANCHORED"], ["CWD_DRIFT"])
    # NC5 相对路径无锚定 ⇒ CWD_DRIFT
    T(NC + "5_unanchored", "运行 python3 temp/good.py --alpha", ["CWD_DRIFT"])
    # NC6 prompt 缺失
    cases.append((NC + "6_missing_prompt", {"enabled": True, "repeat": "daily"}, ["MISSING_PROMPT"], []))
    # NC7 JSON 坏（由调用侧注入）
    cases.append((NC + "7_bad_json", "<<<BADJSON>>>", ["JSON_PARSE_ERROR"], []))
    # NC8 disabled ⇒ SKIP
    cases.append((NC + "8_disabled", {"enabled": False, "prompt": "x"}, ["SKIPPED_DISABLED"], []))
    # NC9 非白名单通道词
    T(NC + "9_unknown_channel", "完成后用企业微信通知用户", ["CHANNEL_UNKNOWN"])
    # NC10 提到通知但不指明通道
    T(NC + "10_channel_unspecified", "任务完成后通知用户即可", ["CHANNEL_UNSPECIFIED"])
    # NC11 长生脚本无超时提示
    T(NC + "11_timeout", "运行 python3 %s/sche_tasks/run_trendradar_digest.py --period morning" % root,
      ["TIMEOUT_UNDECLARED"], ["PATH_MISSING"])
    # NC12 脚本无 argparse 但传了 flag ⇒ FLAG_UNCHECKED（不是 FLAG_UNKNOWN）
    T(NC + "12_no_argparse", "运行 python3 temp/noargs.py --whatever", ["FLAG_UNCHECKED"], ["FLAG_UNKNOWN"])
    # NC13 根外绝对路径不存在 ⇒ PATH_OUT_OF_REPO_MISSING
    T(NC + "13_out_of_repo", "读取 /Users/__nobody__/nope.md", ["PATH_OUT_OF_REPO_MISSING"],
      ["PATH_MISSING"])
    # NC14 绝对路径紧跟模板变量（$(date)/${..}）⇒ 只记 PATH_TEMPLATE，不得判 PATH_MISSING
    #     （真实样本：dpdomain_renew 报告路径 …/temp/dpdomain_renew/REPORT_$(date +%Y%m%d).md）
    T(NC + "14_tmpl_abs", "写入 %s/temp/dpdomain_renew/REPORT_$(date +%%Y%%m%%d).md" % root,
      ["PATH_TEMPLATE"], ["PATH_MISSING"])
    # NC15 绝对路径紧跟中文占位 ⇒ 同样只记 PATH_TEMPLATE
    T(NC + "15_cjk_abs", "报告写到 %s/temp/dpdomain_renew/REPORT_（日期）.md" % root,
      ["PATH_TEMPLATE"], ["PATH_MISSING"])
    # NC16 绝对路径被空格截断，但兄弟项存在 ⇒ PATH_PREFIX（INFO），不得判 PATH_MISSING
    T(NC + "16_prefix_abs", "读取 %s/temp/notes dir/x.md 的全文" % root,
      ["PATH_PREFIX"], ["PATH_MISSING"])
    # NC17 裸相对名不存在 ⇒ WARN 级 REL_BARE_UNRESOLVED（他仓同名目录常见，禁判 FAIL）
    T(NC + "17_bare_rel", "把结果写入 docs/analysis/weekly.json", ["REL_BARE_UNRESOLVED"],
      ["PATH_MISSING"])
    # NC18 ../ 锚定的相对路径不存在 ⇒ PATH_MISSING（R375 真实形态）
    T(NC + "18_parent_rel", "运行 python3 ../temp/get_week_range.py --alpha", ["PATH_MISSING"])
    # NC20 裸相对名（无 ./ ../ 锚定）缺失 ⇒ 只 WARN，不得判 PATH_MISSING
    #     （真实样本：weekly_inspection 的 docs/analysis 属 bar-smyze 仓）
    T(NC + "20_bare_missing", "把结论写到 docs/analysis/weekly.json", ["REL_BARE_UNRESOLVED"],
      ["PATH_MISSING"])
    # NC19 ./ 锚定 + 无 GA_ROOT ⇒ CWD_DRIFT（R375 另一形态：repo/temp 下落点漂移）
    T(NC + "19_dot_anchor", "运行 python3 ./temp/good.py --alpha", ["CWD_DRIFT"],
      ["PATH_MISSING"])
    # NC23 跨子句引用：同一行里脚本归前句、--since/--until 属后句的 git 散文引用
    #      ⇒ 不得把后句 flag 判成前脚本的 FLAG_UNKNOWN（真实样本：weekly_report_reminder）
    T(NC + "23_cross_clause", "运行 python3 %s/temp/good.py --alpha，后续 git log 的 --since/--until 原样使用" % root,
      ["FLAG_UNATTRIBUTED"], ["FLAG_UNKNOWN"])
    # NC24 flag 出现在脚本之前（同一子句）：归属要求「脚本须在 flag 之前」⇒ 同样不得判 FLAG_UNKNOWN
    T(NC + "24_flag_before_script", "git log --since=2026-09-01 之后运行 python3 %s/temp/good.py --alpha" % root,
      ["FLAG_UNATTRIBUTED"], ["FLAG_UNKNOWN"])

    os.makedirs(tasks, exist_ok=True)
    for tid, d, _, _ in cases:
        p = os.path.join(tasks, tid + ".json")
        if d == "<<<BADJSON>>>":
            _write(p, "{not json,,,")
        else:
            _write(p, json.dumps(d, ensure_ascii=False))
    # 长生脚本夹具（供 NC11 解析到）
    _write(os.path.join(root, "sche_tasks", "run_trendradar_digest.py"),
           "import argparse\np=argparse.ArgumentParser()\np.add_argument('--period')\n")

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        results, summary = lint_all(root, tasks)
    finally:
        if old_home:
            os.environ["HOME"] = old_home

    ok = 0
    fails = []
    for tid, _, expect, absent in cases:
        r = next((x for x in results if x["id"] == tid), None)
        if r is None:
            fails.append("%s 缺失结果" % tid)
            continue
        codes = set(r.get("codes", []))
        bad = [c for c in expect if c not in codes] + [c for c in absent if c in codes]
        if bad:
            fails.append("%s 期望 %s，实得 %s（问题：%s）" % (tid, expect, sorted(codes), bad))
        else:
            ok += 1
    # NC21/NC22：--only 过滤语义（多值逗号集合命中两个；未知 id 得 0 条 ⇒ 由 main 转 rc=2）
    try:
        r_sub, _ = lint_all(root, tasks, only={"NC1_missing_path", "NC3_clean"})
        if sorted(x["id"] for x in r_sub) == ["NC1_missing_path", "NC3_clean"]:
            ok += 1
        else:
            fails.append("NC21 only 多值过滤失败：%s" % sorted(x["id"] for x in r_sub))
    except Exception as e:
        fails.append("NC21 异常：%s" % e)
    try:
        r_none, _ = lint_all(root, tasks, only={"no_such_task_id"})
        if r_none == []:
            ok += 1
        else:
            fails.append("NC22 未知 id 应得 0 条，实得 %d" % len(r_none))
    except Exception as e:
        fails.append("NC22 异常：%s" % e)
    # 只读自证断言
    if summary["readonly_changed"]:
        fails.append("只读自证失败：changed=%s" % summary["readonly_changed"])
    else:
        ok += 1
    total = len(cases) + 3
    print("selftest %d/%d" % (ok, total))
    for f in fails:
        print("  ✗ %s" % f)
    return 0 if not fails else 1


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="sche_tasks prompt 静态体检器（只读）")
    ap.add_argument("--root", default=None, help="仓库根（默认=本文件上级目录）")
    ap.add_argument("--tasks-dir", default=None, help="sche_tasks 目录（默认 <root>/sche_tasks）")
    ap.add_argument("--only", default=None,
                    help="只体检指定任务 id（可逗号分隔多个；未知 id ⇒ rc=2）")
    ap.add_argument("--json", dest="json_out", default=None, help="结果落 JSON")
    ap.add_argument("--sandbox", action="store_true", help="打印 3-cwd 沙盒解析明细")
    ap.add_argument("--selftest", action="store_true", help="跑自测（含负控）")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    root = os.path.abspath(args.root or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    tasks_dir = os.path.abspath(args.tasks_dir or os.path.join(root, "sche_tasks"))
    if not os.path.isdir(tasks_dir):
        print("tasks_dir 不存在：%s" % tasks_dir, file=sys.stderr)
        return 2
    only_ids = None
    if args.only:
        only_ids = {x.strip() for x in args.only.split(",") if x.strip()}
        if not only_ids:
            print("--only 为空", file=sys.stderr)
            return 2
    results, summary = lint_all(root, tasks_dir, only=only_ids)
    print_report(results, summary, root, tasks_dir, show_sandbox=args.sandbox)
    if args.json_out:
        payload = {"generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                   "root": root, "tasks_dir": tasks_dir, "summary": summary,
                   "tasks": results}
        with open(args.json_out, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
        print("JSON: %s" % args.json_out)
    if only_ids:
        got = {r["id"] for r in results}
        missing = sorted(only_ids - got)
        if missing:
            print("未知任务 id（在册顶层 json 中不存在）：%s" % ", ".join(missing), file=sys.stderr)
            return 2
    return 1 if summary["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
