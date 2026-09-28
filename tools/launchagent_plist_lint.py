#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launchagent_plist_lint.py -- LaunchAgents plist 静态+状态体检（只读）

背景
----
R375 / R469 已证：定时任务侧「prompt 吃 cwd」会让相对路径失效。
但 *launchd* 侧（~/Library/LaunchAgents/*.plist）从未系统体检：
  - ProgramArguments 里的相对脚本路径依赖 WorkingDirectory；
  - 绝对路径 token 可能在文件系统上已不存在；
  - 裸解释器名（python3/bash）在 launchd 的最小 PATH 下未必可解析；
  - WorkingDirectory / StandardOutPath 父目录不存在 ⇒ launchd 不会创建；
  - plist 存在但 label 未 bootstrap（或已被 `launchctl disable`）⇒ 永不运行；
  - 已加载但 last exit status != 0 ⇒ 反复失败。

本工具只读：不 load/unload/enable/disable 任何服务，不写用户目录，
不读取脚本内容以外的东西（脚本仅做存在性/可执行位判定，不执行）。
plist 用 plistlib 静态解析；状态来自 `launchctl list` 与
`launchctl print-disabled gui/<uid>`（只读子命令）。

判据（每条 finding 一个 judge 码）
---------------------------------
ERROR 级（结构性破损，必失败）
  ARG_PATH_PARENT_MISSING   args 里绝对路径的父目录不存在
  SCRIPT_ARG_MISSING        脚本型参数（.py/.sh/...）文件不存在
  PROGRAM_MISSING           首参数（或 --- 之后的程序）文件不存在
  CWD_MISSING               WorkingDirectory 不存在
  LOG_DIR_MISSING           Standard{Out,Error}Path 的父目录不存在
  REL_SCRIPT_UNANCHORED     相对脚本 token 且无 WorkingDirectory（R375 同类）
  REL_SCRIPT_MISSING        相对脚本 token + WorkingDirectory，但解析后文件不存在
  MODULE_CWD_MISSING        `-m <mod>` 且无 WorkingDirectory（launchd 默认 cwd=/ ⇒ 通常不可导入）
WARN 级（可能静默失效）
  BARE_PROGRAM_UNRESOLVED   首参数是裸名且在 plist PATH / launchd 默认 PATH 下找不到
  JOB_DISABLED              plist 在盘、RunAtLoad 真，但 `launchctl disable` 状态 ⇒ 永不运行
  JOB_NOT_BOOTSTRAPPED      plist 在盘但不在 launchctl list（且非 disabled）
  LAST_EXIT_NONZERO         已加载但 last exit status != 0
  LOG_FILE_LARGE            日志文件 > --log-warn-mb（默认 64MB）
INFO / EXEMPT
  REL_SCRIPT_ANCHORED       相对脚本已由 WorkingDirectory 正确锚定（合规）
  BARE_PROGRAM_RESOLVED     裸名已解析
  MODULE_AT_CWD             `-m` 模块在 WorkingDirectory 下可定位
  LAST_EXIT_ZERO / LOADED   正常态
  NO_LAUNCHCTL_STATE        离线模式（--no-launchctl）下状态未知
  THIRD_PARTY_*             非本机自有（com.cuip./com.genericagent./ai.hermes. 之外）一律只登记不判

用法
----
  python3 tools/launchagent_plist_lint.py scan [--plist-dir DIR] [--json-out F]
                                              [--no-launchctl] [--log-warn-mb N]
  python3 tools/launchagent_plist_lint.py verify --json F      # 复核快照内部一致性
  python3 tools/launchagent_plist_lint.py proof  [--json-out F]# 只读自证（输入 sha256 前后一致）
  python3 tools/launchagent_plist_lint.py selftest             # 夹具+负控（不触碰真实 plist）

子命令是位置参数（`--selftest` 形式会报 unrecognized）。
退出码：0 无 ERROR；1 有 ERROR；2 用法/输入错误。
"""
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile

SCHEMA = "ga.launchagent_lint.v1"
DEFAULT_PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")
LAUNCHD_DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

OWNED_LABEL_PREFIXES = ("com.cuip.", "com.genericagent.", "ai.hermes.")
SCRIPT_EXTS = (
    ".py", ".pyw", ".sh", ".bash", ".zsh", ".command", ".js", ".mjs", ".cjs",
    ".rb", ".pl", ".php", ".scpt", ".applescript", ".swift", ".jar", ".py3",
)

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"
EXEMPT = "EXEMPT"
SEV_ORDER = {ERROR: 0, WARN: 1, INFO: 2, EXEMPT: 3}

KNOWN_JUDGES = {
    "ARG_PATH_PARENT_MISSING", "ARG_PATH_FILE_MISSING", "PROGRAM_MISSING",
    "SCRIPT_ARG_MISSING", "INLINE_SCRIPT", "INLINE_SCRIPT_PATH_PARENT_MISSING",
    "ON_DEMAND_NO_PROGRAM",
    "CWD_MISSING", "LOG_DIR_MISSING",
    "REL_SCRIPT_UNANCHORED", "REL_SCRIPT_MISSING", "REL_SCRIPT_ANCHORED",
    "MODULE_CWD_MISSING", "MODULE_AT_CWD", "MODULE_UNRESOLVED_AT_CWD",
    "BARE_PROGRAM_UNRESOLVED", "BARE_PROGRAM_RESOLVED",
    "JOB_DISABLED", "JOB_NOT_BOOTSTRAPPED", "JOB_NOT_BOOTSTRAPPED_THIRD_PARTY",
    "JOB_DISABLED_THIRD_PARTY", "LOADED", "LAST_EXIT_NONZERO",
    "LAST_EXIT_NONZERO_THIRD_PARTY", "LOG_FILE_LARGE", "LOG_FILE_MISSING",
    "NO_LAUNCHCTL_STATE", "THIRD_PARTY_NOT_LOADED", "WRAPPER_SCRIPT",
}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def sha256_file(path, cap=None):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            if cap and n >= cap:
                break
    return h.hexdigest(), n


def is_exec(p):
    return os.path.isfile(p) and os.access(p, os.X_OK)


def which_in(paths, name):
    """在给定 PATH 列表里查裸名（只查文件存在+可执行位，不执行）。"""
    for d in paths:
        if not d:
            continue
        cand = os.path.join(d, name)
        if is_exec(cand):
            return cand
    return None


def arg_looks_like_path(tok):
    if not isinstance(tok, str) or not tok:
        return False
    if tok.startswith("-"):
        return False
    return ("/" in tok) or tok.startswith("~")


def expand(tok):
    return os.path.expanduser(tok)


def is_inline_script_tok(tok):
    """args 里塞了整段 shell 脚本（/bin/sh -c "..." 形态）时不按路径判。"""
    if not isinstance(tok, str) or not tok:
        return False
    if "\n" in tok:
        return True
    if " " in tok and not os.path.exists(tok) and any(c in tok for c in "$;=&|"):
        return True
    return False


def abs_tokens_in_text(txt, cap=40):
    toks = re.findall(r'/[A-Za-z0-9_.@%+~/-]{2,}', txt)
    seen = []
    for t in toks:
        if t not in seen:
            seen.append(t)
        if len(seen) >= cap:
            break
    return seen


def looks_like_script(tok):
    if not isinstance(tok, str) or not tok:
        return False
    low = tok.lower()
    if any(low.endswith(e) for e in SCRIPT_EXTS):
        return True
    return tok.startswith("./") or tok.startswith("../")


# --------------------------------------------------------------------------
# plist 解析
# --------------------------------------------------------------------------
def load_plist(path):
    with open(path, "rb") as f:
        return plistlib.load(f)


def plist_args(d):
    a = d.get("ProgramArguments")
    if isinstance(a, list) and a:
        return [x for x in a if isinstance(x, str)]
    p = d.get("Program")
    if isinstance(p, str):
        return [p]
    return []


def classify_owner(label, args, path):
    if any(label.startswith(p) for p in OWNED_LABEL_PREFIXES):
        return "owned"
    for t in args:
        if "projects/GenericAgent" in t or "/GenericAgent/" in t:
            return "owned"
    if "GenericAgent" in os.path.basename(path):
        return "owned"
    return "third_party"


# --------------------------------------------------------------------------
# 核心检查（纯函数：plist 字典 + launchd 状态 → findings）
# --------------------------------------------------------------------------
def check_plist(record, launchd, log_warn_bytes):
    """record: {plist, path, label, args, cwd, out, err, env_path, triggers}
    launchd: {loaded:{label:(pid,status)}, disabled:{label:bool}, available:bool}
    """
    out = []
    label = record["label"]
    pl = os.path.basename(record["path"])
    owned = record["owner"] == "owned"

    def add(judge, sev, detail, **extra):
        assert judge in KNOWN_JUDGES, judge
        if not owned and sev in (ERROR, WARN) and not judge.endswith("_THIRD_PARTY"):
            sev = EXEMPT  # 非自有 plist 只登记不判
        f = {"judge": judge, "severity": sev, "label": label, "plist": pl,
             "detail": detail}
        f.update(extra)
        out.append(f)

    args = record["args"]
    cwd = record["cwd"]

    # --- WorkingDirectory -------------------------------------------------
    if cwd:
        if not os.path.isdir(expand(cwd)):
            add("CWD_MISSING", ERROR, "WorkingDirectory 不存在: %s" % cwd)
    # --- ProgramArguments 路径 token ------------------------------------
    def check_path_token(idx, tok, is_program):
        if is_inline_script_tok(tok):
            add("INLINE_SCRIPT", INFO,
                "args[%d] 为内联脚本（%d 字符），仅做绝对路径存在性检查"
                % (idx, len(tok)))
            for t2 in abs_tokens_in_text(tok):
                par = os.path.dirname(t2)
                if par and not os.path.isdir(par):
                    add("INLINE_SCRIPT_PATH_PARENT_MISSING", WARN,
                        "内联脚本引用路径父目录不存在: %s" % t2)
            return
        t = expand(tok)
        if os.path.isabs(t):
            parent = os.path.dirname(t)
            if not os.path.isdir(parent):
                add("ARG_PATH_PARENT_MISSING", ERROR,
                    "args[%d] 父目录不存在: %s" % (idx, tok))
            elif os.path.isdir(t):
                pass
            elif not os.path.exists(t):
                if is_program:
                    add("PROGRAM_MISSING", ERROR, "程序文件不存在: %s" % tok)
                elif looks_like_script(tok):
                    add("SCRIPT_ARG_MISSING", ERROR,
                        "脚本型参数不存在: %s" % tok)
                else:
                    add("ARG_PATH_FILE_MISSING", INFO,
                        "路径不存在但父目录在（可能运行时创建）: %s" % tok)
            return
        # 相对路径 token
        if looks_like_script(tok) or "/" in tok:
            if cwd:
                resolved = os.path.normpath(os.path.join(expand(cwd), tok))
                if os.path.exists(resolved):
                    add("REL_SCRIPT_ANCHORED", INFO,
                        "相对 token %s 已被 WorkingDirectory 锚定 → %s" % (tok, resolved),
                        resolved=resolved)
                else:
                    add("REL_SCRIPT_MISSING", ERROR,
                        "相对 token %s 解析为 %s，文件不存在" % (tok, resolved),
                        resolved=resolved)
            else:
                add("REL_SCRIPT_UNANCHORED", ERROR,
                    "相对 token %s 且无 WorkingDirectory（launchd 默认 cwd=/）" % tok)

    if not args:
        if record.get("mach") or record.get("session") or record.get("triggers"):
            add("ON_DEMAND_NO_PROGRAM", INFO,
                "无 ProgramArguments（MachServices/session/触发器型 on-demand，"
                "由系统或事件按需拉起）")
        else:
            add("PROGRAM_MISSING", ERROR, "无 Program/ProgramArguments 且无任何触发器")
    else:
        prog = args[0]
        if os.path.isabs(expand(prog)):
            check_path_token(0, prog, True)
        else:
            paths = [p for p in (record["env_path"] or "").split(":") if p] or \
                    LAUNCHD_DEFAULT_PATH.split(":")
            hit = which_in(paths, prog) if "/" not in prog else None
            if "/" in prog:
                check_path_token(0, prog, True)
            elif hit:
                add("BARE_PROGRAM_RESOLVED", INFO,
                    "裸程序名 %s 在 PATH 中解析为 %s" % (prog, hit), resolved=hit)
            else:
                add("BARE_PROGRAM_UNRESOLVED", WARN,
                    "裸程序名 %s 未能在 PATH(%s) 中解析" % (prog, ":".join(paths)))
        for i, tok in enumerate(args[1:], start=1):
            if tok == "-m" or tok.startswith("-"):
                continue
            if is_inline_script_tok(tok) or arg_looks_like_path(tok) or looks_like_script(tok):
                check_path_token(i, tok, False)

        # --- /bin/sh 之类 wrapper ----------------------------------------
        if os.path.basename(args[0]) in ("sh", "bash", "zsh", "env", "dash"):
            for tok in args[1:]:
                if not is_inline_script_tok(tok) and (looks_like_script(tok)
                                                      or arg_looks_like_path(tok)):
                    add("WRAPPER_SCRIPT", INFO,
                        "由 %s 包装执行: %s" % (os.path.basename(args[0]), tok))

        # --- python -m ----------------------------------------------------
        if "-m" in args:
            mi = args.index("-m")
            mod = args[mi + 1] if mi + 1 < len(args) else ""
            mod = mod.split(":")[0]
            if cwd:
                rel = mod.replace(".", "/")
                cands = [os.path.join(expand(cwd), rel + ".py"),
                         os.path.join(expand(cwd), rel, "__init__.py")]
                hit = next((c for c in cands if os.path.exists(c)), None)
                if hit:
                    add("MODULE_AT_CWD", INFO,
                        "-m %s 在 WorkingDirectory 下可定位 → %s" % (mod, hit),
                        resolved=hit)
                else:
                    add("MODULE_UNRESOLVED_AT_CWD", INFO,
                        "-m %s 在 WorkingDirectory 下无对应文件（可能来自已安装包）: %s"
                        % (mod, cands[0]))
            else:
                add("MODULE_CWD_MISSING", ERROR,
                    "-m %s 且无 WorkingDirectory（launchd 默认 cwd=/）" % mod)

    # --- 日志目录 / 体积 ---------------------------------------------------
    seen_logs = set()
    for key in ("StandardOutPath", "StandardErrorPath"):
        p = record.get("out" if key == "StandardOutPath" else "err")
        if not p:
            continue
        if expand(p) in seen_logs:
            continue  # out/err 指向同一文件时只判一次
        seen_logs.add(expand(p))
        p = expand(p)
        parent = os.path.dirname(p)
        if parent and not os.path.isdir(parent):
            add("LOG_DIR_MISSING", ERROR, "%s 父目录不存在: %s" % (key, p))
        elif os.path.isfile(p):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            if size > log_warn_bytes:
                add("LOG_FILE_LARGE", WARN,
                    "%s 体积 %.1f MB > %.0f MB" % (key, size / 1048576.0,
                                                    log_warn_bytes / 1048576.0),
                    bytes=size)
        elif parent and os.path.isdir(parent):
            add("LOG_FILE_MISSING", INFO, "%s 尚未创建: %s" % (key, p))

    # --- launchd 状态 -----------------------------------------------------
    if not launchd.get("available"):
        add("NO_LAUNCHCTL_STATE", INFO, "离线模式：未读取 launchctl 状态")
        return out
    loaded = launchd["loaded"].get(label)
    disabled = launchd["disabled"].get(label)
    if loaded is None:
        if disabled:
            add("JOB_DISABLED" if owned else "JOB_DISABLED_THIRD_PARTY",
                WARN if owned else EXEMPT,
                "plist 在盘但被 launchctl disable ⇒ 永不运行")
        else:
            add("JOB_NOT_BOOTSTRAPPED" if owned else "JOB_NOT_BOOTSTRAPPED_THIRD_PARTY",
                WARN if owned else EXEMPT,
                "plist 在盘但不在 launchctl list ⇒ 未 bootstrap/已 bootout")
    else:
        pid, status = loaded
        if status not in ("0",):
            sev = WARN if owned else EXEMPT
            add("LAST_EXIT_NONZERO" if owned else "LAST_EXIT_NONZERO_THIRD_PARTY",
                sev, "已加载 last exit status=%s (pid=%s)" % (status, pid),
                exit_status=status, pid=pid)
        else:
            add("LOADED", INFO, "已加载 last exit status=0 (pid=%s)" % pid)
    return out


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def read_launchctl_state(uid=None):
    st = {"available": False, "loaded": {}, "disabled": {}}
    uid = os.getuid() if uid is None else uid
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        if r.returncode != 0:
            return st
    except (OSError, FileNotFoundError):
        return st
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            st["loaded"][parts[2]] = (parts[0], parts[1])
    try:
        r = subprocess.run(["launchctl", "print-disabled", "gui/%d" % uid],
                           capture_output=True, text=True)
        if r.returncode == 0:
            for m in re.finditer(r'"([^"]+)"\s*=>\s*(disabled|enabled|true|false)',
                                 r.stdout):
                st["disabled"][m.group(1)] = m.group(2).lower() in ("disabled", "true")
    except (OSError, FileNotFoundError):
        pass
    st["available"] = True
    return st


def collect_records(plist_dir):
    recs = []
    inputs = []
    if not os.path.isdir(plist_dir):
        return recs, inputs
    for name in sorted(os.listdir(plist_dir)):
        if not name.endswith(".plist"):
            continue
        path = os.path.join(plist_dir, name)
        if not os.path.isfile(path):
            continue
        inputs.append(path)
        try:
            d = load_plist(path)
        except Exception as e:  # plistlib 解析失败=真问题
            recs.append({"plist": name, "path": path,
                         "label": name[:-6], "args": [], "cwd": None,
                         "out": None, "err": None, "env_path": None,
                         "triggers": {}, "owner": "owned",
                         "parse_error": "%s: %s" % (type(e).__name__, e)})
            continue
        args = plist_args(d)
        label = d.get("Label") or name[:-6]
        env = d.get("EnvironmentVariables") or {}
        recs.append({
            "plist": name, "path": path, "label": label, "args": args,
            "cwd": d.get("WorkingDirectory"),
            "out": d.get("StandardOutPath"), "err": d.get("StandardErrorPath"),
            "env_path": env.get("PATH") if isinstance(env, dict) else None,
            "mach": bool(d.get("MachServices")),
            "session": d.get("LimitLoadToSessionType"),
            "triggers": {k: d[k] for k in
                         ("StartInterval", "StartCalendarInterval", "WatchPaths",
                          "RunAtLoad", "KeepAlive") if k in d},
            "owner": classify_owner(label, args, path),
        })
    return recs, inputs


def scan(plist_dir=None, use_launchctl=True, log_warn_mb=64.0, uid=None):
    plist_dir = plist_dir or DEFAULT_PLIST_DIR
    recs, inputs = collect_records(plist_dir)
    launchd = read_launchctl_state(uid) if use_launchctl else \
        {"available": False, "loaded": {}, "disabled": {}}
    findings = []
    for r in recs:
        if r.get("parse_error"):
            findings.append({"judge": "PROGRAM_MISSING", "severity": ERROR,
                             "label": r["label"], "plist": r["plist"],
                             "detail": "plist 解析失败: %s" % r["parse_error"]})
            continue
        findings.extend(check_plist(r, launchd, int(log_warn_mb * 1048576)))
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    judges = {}
    for f in findings:
        judges[f["judge"]] = judges.get(f["judge"], 0) + 1
    owned = [r for r in recs if r["owner"] == "owned"]
    return {
        "schema": SCHEMA,
        "plist_dir": plist_dir,
        "launchctl_available": bool(launchd["available"]),
        "n_plists": len(recs),
        "n_owned": len(owned),
        "n_third_party": len(recs) - len(owned),
        "counts": counts,
        "judges": judges,
        "findings": findings,
        "records": [{"plist": r["plist"], "label": r["label"], "owner": r["owner"],
                     "n_args": len(r.get("args") or []),
                     "cwd": r.get("cwd"), "triggers": r.get("triggers")}
                    for r in recs],
        "inputs": [{"path": p, "sha256": sha256_file(p)[0],
                    "bytes": os.path.getsize(p)} for p in inputs],
    }


# --------------------------------------------------------------------------
# verify / proof
# --------------------------------------------------------------------------
def verify(json_path):
    problems = []
    with open(json_path) as f:
        d = json.load(f)
    if d.get("schema") != SCHEMA:
        problems.append("schema 不符: %r" % d.get("schema"))
    fs = d.get("findings") or []
    for f in fs:
        if f.get("judge") not in KNOWN_JUDGES:
            problems.append("未知 judge: %r" % f.get("judge"))
        if f.get("severity") not in SEV_ORDER:
            problems.append("未知 severity: %r" % f.get("severity"))
        if not f.get("label") or not f.get("plist"):
            problems.append("finding 缺 label/plist: %r" % f)
    # 计数自洽
    c = {}
    for f in fs:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    if c != (d.get("counts") or {}):
        problems.append("counts 不自洽: 记录=%r 复算=%r" % (d.get("counts"), c))
    # ERROR 只在 owned 上（third_party 一律 EXEMPT/WARN，不得有 ERROR）
    labels = {r["label"]: r for r in (d.get("records") or [])}
    for f in fs:
        r = labels.get(f["label"])
        if r and r.get("owner") == "third_party" and f["severity"] == ERROR:
            problems.append("第三方 plist 被判 ERROR: %s/%s" % (f["label"], f["judge"]))
    # 每条 ERROR/WARN 必带 detail
    for f in fs:
        if f["severity"] in (ERROR, WARN) and not f.get("detail"):
            problems.append("严重 finding 无 detail: %r" % f)
    return {"schema": SCHEMA, "json": json_path, "n_findings": len(fs),
            "problems": problems, "ok": not problems, "counts": c}


def proof(json_out=None):
    """只读自证：两次采集的输入 sha256 必须逐字一致。"""
    dir_hashes_1 = {}
    for name in sorted(os.listdir(DEFAULT_PLIST_DIR)):
        p = os.path.join(DEFAULT_PLIST_DIR, name)
        if name.endswith(".plist") and os.path.isfile(p):
            dir_hashes_1[name] = sha256_file(p)[0]
    # 真正跑一次采集（内部会再读一遍）
    res = scan()
    dir_hashes_2 = {}
    for name in sorted(os.listdir(DEFAULT_PLIST_DIR)):
        p = os.path.join(DEFAULT_PLIST_DIR, name)
        if name.endswith(".plist") and os.path.isfile(p):
            dir_hashes_2[name] = sha256_file(p)[0]
    changed = [k for k in sorted(set(dir_hashes_1) | set(dir_hashes_2))
               if dir_hashes_1.get(k) != dir_hashes_2.get(k)]
    out = {"schema": SCHEMA, "files": len(dir_hashes_2), "changed": changed,
           "ok": not changed, "n_plists": res["n_plists"],
           "counts": res["counts"]}
    if json_out:
        with open(json_out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return out


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _mk_plist(path, label, args, cwd=None, out=None, err=None, env_path=None):
    d = {"Label": label, "ProgramArguments": args, "RunAtLoad": True}
    if cwd:
        d["WorkingDirectory"] = cwd
    if out:
        d["StandardOutPath"] = out
    if err:
        d["StandardErrorPath"] = err
    if env_path:
        d["EnvironmentVariables"] = {"PATH": env_path}
    with open(path, "wb") as f:
        plistlib.dump(d, f)
    return path


def selftest():
    """夹具+负控：全部在 tmpdir 内，绝不触碰真实 ~/Library/LaunchAgents。"""
    n_pass = n_fail = 0
    log = []

    def chk(name, cond, extra=""):
        nonlocal n_pass, n_fail
        if cond:
            n_pass += 1
            log.append("PASS %s" % name)
        else:
            n_fail += 1
            log.append("FAIL %s %s" % (name, extra))

    with tempfile.TemporaryDirectory() as td:
        pdir = os.path.join(td, "LaunchAgents")
        os.makedirs(pdir)
        real_dir = os.path.join(td, "real")
        os.makedirs(real_dir)
        script = os.path.join(real_dir, "job.py")
        with open(script, "w") as f:
            f.write("print(1)\n")
        os.chmod(script, 0o755)
        logdir = os.path.join(td, "logs")
        os.makedirs(logdir)

        # F0 完全干净
        _mk_plist(os.path.join(pdir, "com.cuip.clean.plist"), "com.cuip.clean",
                  ["/usr/bin/python3", script], cwd=real_dir,
                  out=os.path.join(logdir, "clean.log"))
        # F1 相对脚本 + cwd（应 ANCHORED，非 ERROR）
        _mk_plist(os.path.join(pdir, "com.cuip.rel_ok.plist"), "com.cuip.rel_ok",
                  ["/usr/bin/python3", "job.py"], cwd=real_dir)
        # F2 相对脚本无 cwd（应 REL_SCRIPT_UNANCHORED ERROR）
        _mk_plist(os.path.join(pdir, "com.cuip.rel_unanchored.plist"),
                  "com.cuip.rel_unanchored", ["/usr/bin/python3", "job.py"])
        # F3 相对脚本 + cwd 但文件不存在（应 REL_SCRIPT_MISSING ERROR）
        _mk_plist(os.path.join(pdir, "com.cuip.rel_missing.plist"),
                  "com.cuip.rel_missing", ["/usr/bin/python3", "nope.py"],
                  cwd=real_dir)
        # F4 cwd 不存在
        _mk_plist(os.path.join(pdir, "com.cuip.cwd_missing.plist"),
                  "com.cuip.cwd_missing", ["/usr/bin/python3", script],
                  cwd=os.path.join(td, "no_such_dir"))
        # F5 程序不存在
        _mk_plist(os.path.join(pdir, "com.cuip.prog_missing.plist"),
                  "com.cuip.prog_missing", [os.path.join(real_dir, "ghost.sh")])
        # F6 日志父目录不存在
        _mk_plist(os.path.join(pdir, "com.cuip.logdir_missing.plist"),
                  "com.cuip.logdir_missing", ["/usr/bin/python3", script],
                  out=os.path.join(td, "no_dir", "a.log"))
        # F7 裸名未解析 / F8 裸名已解析
        _mk_plist(os.path.join(pdir, "com.cuip.bare_bad.plist"),
                  "com.cuip.bare_bad", ["definitely-not-a-binary-xyz"])
        _mk_plist(os.path.join(pdir, "com.cuip.bare_ok.plist"),
                  "com.cuip.bare_ok", ["python3"],
                  env_path="/opt/homebrew/bin:/usr/bin:/bin")
        # F9 -m 无 cwd（ERROR）
        _mk_plist(os.path.join(pdir, "com.cuip.mod_nocwd.plist"),
                  "com.cuip.mod_nocwd", ["/usr/bin/python3", "-m", "pkg.mod"])
        # F10 -m + cwd 且可定位
        _mk_plist(os.path.join(pdir, "com.cuip.mod_ok.plist"),
                  "com.cuip.mod_ok", ["/usr/bin/python3", "-m", "job"],
                  cwd=real_dir)
        # F11 已加载但退出码非 0
        _mk_plist(os.path.join(pdir, "com.cuip.exit1.plist"), "com.cuip.exit1",
                  ["/usr/bin/python3", script])
        # F12 在盘但未 bootstrap
        _mk_plist(os.path.join(pdir, "com.cuip.notloaded.plist"),
                  "com.cuip.notloaded", ["/usr/bin/python3", script])
        # F13 被 disable
        _mk_plist(os.path.join(pdir, "com.cuip.disabled.plist"),
                  "com.cuip.disabled", ["/usr/bin/python3", script])
        # F14 第三方未加载 ⇒ EXEMPT
        _mk_plist(os.path.join(pdir, "com.vendor.thing.plist"), "com.vendor.thing",
                  ["/usr/bin/true"])
        # F15 解析失败
        with open(os.path.join(pdir, "com.cuip.bad_plist.plist"), "wb") as f:
            f.write(b"not a plist at all")

        launchd = {
            "available": True,
            "loaded": {
                "com.cuip.clean": ("-", "0"),
                "com.cuip.rel_ok": ("-", "0"),
                "com.cuip.exit1": ("123", "1"),
            },
            "disabled": {"com.cuip.disabled": True},
        }

        res_all = []
        recs, inputs = collect_records(pdir)
        logdir2 = logdir
        for r in recs:
            if r.get("parse_error"):
                res_all.append((r["label"], {"judge": "PROGRAM_MISSING",
                                             "severity": ERROR}))
                continue
            for f in check_plist(r, launchd, 64 * 1048576):
                res_all.append((r["label"], f))

        def judges_of(label):
            return sorted(f["judge"] for (l, f) in res_all if l == label)

        def sev_of(label):
            return sorted(f["severity"] for (l, f) in res_all if l == label)

        # 正控：干净 plist 无 ERROR/WARN
        chk("clean_no_error", ERROR not in sev_of("com.cuip.clean"),
            sev_of("com.cuip.clean"))
        chk("clean_no_warn", WARN not in sev_of("com.cuip.clean"),
            sev_of("com.cuip.clean"))
        # 判据各自命中
        chk("rel_ok_anchored", "REL_SCRIPT_ANCHORED" in judges_of("com.cuip.rel_ok"),
            judges_of("com.cuip.rel_ok"))
        chk("rel_unanchored_error",
            "REL_SCRIPT_UNANCHORED" in judges_of("com.cuip.rel_unanchored"),
            judges_of("com.cuip.rel_unanchored"))
        chk("rel_missing_error", "REL_SCRIPT_MISSING" in judges_of("com.cuip.rel_missing"),
            judges_of("com.cuip.rel_missing"))
        chk("cwd_missing_error", "CWD_MISSING" in judges_of("com.cuip.cwd_missing"))
        chk("prog_missing_error", "PROGRAM_MISSING" in judges_of("com.cuip.prog_missing"))
        chk("logdir_missing_error", "LOG_DIR_MISSING" in judges_of("com.cuip.logdir_missing"))
        chk("bare_bad_warn", "BARE_PROGRAM_UNRESOLVED" in judges_of("com.cuip.bare_bad"))
        chk("bare_ok_resolved", "BARE_PROGRAM_RESOLVED" in judges_of("com.cuip.bare_ok"))
        chk("mod_nocwd_error", "MODULE_CWD_MISSING" in judges_of("com.cuip.mod_nocwd"))
        chk("mod_ok_at_cwd", "MODULE_AT_CWD" in judges_of("com.cuip.mod_ok"))
        chk("exit_nonzero_warn", "LAST_EXIT_NONZERO" in judges_of("com.cuip.exit1"))
        chk("not_bootstrapped_warn",
            "JOB_NOT_BOOTSTRAPPED" in judges_of("com.cuip.notloaded"))
        chk("disabled_warn", "JOB_DISABLED" in judges_of("com.cuip.disabled"))
        chk("third_party_exempt",
            "JOB_NOT_BOOTSTRAPPED_THIRD_PARTY" in judges_of("com.vendor.thing")
            and ERROR not in sev_of("com.vendor.thing"))
        chk("parse_fail_error", "PROGRAM_MISSING" in judges_of("com.cuip.bad_plist"))
        # INFO 级不得被记为 ERROR（严重度分级正确）
        chk("args_file_missing_is_info", all(
            f["severity"] == INFO for (l, f) in res_all
            if f["judge"] == "ARG_PATH_FILE_MISSING"))
        # 离线模式：不得出现加载态判据
        recs2, _ = collect_records(pdir)
        off = []
        for r in recs2:
            if r.get("parse_error"):
                continue
            off += check_plist(r, {"available": False, "loaded": {}, "disabled": {}},
                               64 * 1048576)
        chk("offline_no_job_judge", not any(
            f["judge"] in ("JOB_DISABLED", "JOB_NOT_BOOTSTRAPPED", "LOADED",
                           "LAST_EXIT_NONZERO", "JOB_NOT_BOOTSTRAPPED_THIRD_PARTY")
            for f in off))
        chk("offline_has_marker", any(f["judge"] == "NO_LAUNCHCTL_STATE" for f in off))

        # 负控 1：给 rel_unanchored 补 cwd（且文件存在）后必须转绿
        _mk_plist(os.path.join(pdir, "com.cuip.rel_unanchored.plist"),
                  "com.cuip.rel_unanchored", ["/usr/bin/python3", "job.py"],
                  cwd=real_dir)
        recs3, _ = collect_records(pdir)
        j3 = []
        for r in recs3:
            if r["label"] == "com.cuip.rel_unanchored" and not r.get("parse_error"):
                j3 += [f["judge"] for f in check_plist(r, launchd, 64 * 1048576)]
        chk("negctl_unanchored_fixed", "REL_SCRIPT_UNANCHORED" not in j3
            and "REL_SCRIPT_ANCHORED" in j3, j3)

        # 负控 2：删掉干净 plist 引用的脚本文件后必须报错
        os.unlink(script)
        recs4, _ = collect_records(pdir)
        j4 = []
        for r in recs4:
            if r["label"] == "com.cuip.clean" and not r.get("parse_error"):
                j4 += [f["judge"] for f in check_plist(r, launchd, 64 * 1048576)]
        chk("negctl_clean_breaks_when_script_gone",
            any(x in j4 for x in ("ARG_PATH_PARENT_MISSING", "PROGRAM_MISSING",
                                  "REL_SCRIPT_MISSING", "SCRIPT_ARG_MISSING")), j4)

        # 负控 3：把 exit1 的状态改成 0 ⇒ LAST_EXIT_NONZERO 必须消失
        with open(script, "w") as f:
            f.write("print(1)\n")
        os.chmod(script, 0o755)
        launchd2 = json.loads(json.dumps(launchd))
        launchd2["loaded"]["com.cuip.exit1"] = ("-", "0")
        recs5, _ = collect_records(pdir)
        j5 = []
        for r in recs5:
            if r["label"] == "com.cuip.exit1" and not r.get("parse_error"):
                j5 += [f["judge"] for f in check_plist(r, launchd2, 64 * 1048576)]
        chk("negctl_exit_cleared", "LAST_EXIT_NONZERO" not in j5 and "LOADED" in j5, j5)

        # 负控 4：日志体积告警可被阈值关闭
        big = os.path.join(logdir2, "big.log")
        with open(big, "wb") as f:
            f.write(b"x" * (5 * 1048576))
        _mk_plist(os.path.join(pdir, "com.cuip.biglog.plist"), "com.cuip.biglog",
                  ["/usr/bin/python3", script], out=big)
        recs6, _ = collect_records(pdir)
        def biglog_judges(threshold_mb):
            for r in recs6:
                if r["label"] == "com.cuip.biglog" and not r.get("parse_error"):
                    return [f["judge"] for f in
                            check_plist(r, launchd, int(threshold_mb * 1048576))]
            return []
        chk("biglog_warns_at_1mb", "LOG_FILE_LARGE" in biglog_judges(1))
        chk("biglog_quiet_at_64mb", "LOG_FILE_LARGE" not in biglog_judges(64))

        # 负控 5：verify 必须能抓出被人为篡改的 counts
        with tempfile.TemporaryDirectory() as td2:
            snap = os.path.join(td2, "s.json")
            json.dump({"schema": SCHEMA, "counts": {"ERROR": 99}, "findings": [],
                       "records": []}, open(snap, "w"))
            v = verify(snap)
            chk("negctl_verify_catches_count_mismatch", not v["ok"], v)
            json.dump({"schema": SCHEMA, "counts": {"ERROR": 1},
                       "findings": [{"judge": "BOGUS_JUDGE", "severity": ERROR,
                                     "label": "x", "plist": "y", "detail": "d"}],
                       "records": [{"label": "x", "owner": "owned"}]}, open(snap, "w"))
            v = verify(snap)
            chk("negctl_verify_catches_unknown_judge",
                any("未知 judge" in p for p in v["problems"]), v)
            # 正控：合法快照应 ok
            json.dump({"schema": SCHEMA, "counts": {},
                       "findings": [], "records": []}, open(snap, "w"))
            v = verify(snap)
            chk("verify_ok_on_clean_snapshot", v["ok"], v)

        # 只读自证：tmpdir 内不得产生额外文件（除我们自己造的）
        created = sorted(os.listdir(pdir))
        chk("no_stray_files_in_plist_dir",
            all(n.endswith(".plist") for n in created), created)

    print("selftest: %d passed, %d failed" % (n_pass, n_fail))
    for l in log:
        if l.startswith("FAIL"):
            print("  " + l)
    return 0 if n_fail == 0 else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd = argv[0]
    rest = argv[1:]

    def opt(name, default=None):
        if name in rest:
            i = rest.index(name)
            return rest[i + 1] if i + 1 < len(rest) else default
        return default

    if cmd == "selftest":
        return selftest()

    if cmd == "scan":
        pdir = opt("--plist-dir")
        log_mb = float(opt("--log-warn-mb", 64.0) or 64.0)
        use_launchctl = "--no-launchctl" not in rest
        res = scan(pdir, use_launchctl, log_mb)
        js = opt("--json-out")
        if js:
            with open(js, "w") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
        print("plists=%d owned=%d third_party=%d launchctl=%s"
              % (res["n_plists"], res["n_owned"], res["n_third_party"],
                 res["launchctl_available"]))
        print("counts=%s" % json.dumps(res["counts"], ensure_ascii=False, sort_keys=True))
        print("judges=%s" % json.dumps(res["judges"], ensure_ascii=False, sort_keys=True))
        for f in sorted(res["findings"], key=lambda x: (SEV_ORDER[x["severity"]], x["plist"])):
            print("  [%-6s] %-28s %-28s %s"
                  % (f["severity"], f["judge"], f["label"], f["detail"]))
        if js:
            print("json=%s" % js)
        return 1 if res["counts"].get(ERROR) else 0

    if cmd == "verify":
        j = opt("--json")
        if not j:
            print("verify 需要 --json <快照>")
            return 2
        r = verify(j)
        print("verify findings=%d problems=%d ok=%s"
              % (r["n_findings"], len(r["problems"]), r["ok"]))
        for p in r["problems"][:20]:
            print("  ! " + p)
        return 0 if r["ok"] else 1

    if cmd == "proof":
        r = proof(opt("--json-out"))
        print("proof files=%d changed=%s ok=%s counts=%s"
              % (r["files"], r["changed"], r["ok"],
                 json.dumps(r["counts"], sort_keys=True)))
        return 0 if r["ok"] else 1

    print("unknown subcommand: %s" % cmd)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
