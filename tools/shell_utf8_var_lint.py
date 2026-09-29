#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shell_utf8_var_lint — 只读检测 shell 脚本里「$var 紧跟多字节字符」的变量名吞并隐患。

背景（R506 实证）
----------------
`set -u` + `$var` 紧跟全角标点（如 `$locked）`）时，bash 在 UTF-8 locale 下把多字节
字符并入变量名（POSIX 允许 name 含除 `=` 外的任意多字节字符），于是展开的是
`locked）` 这个**另一个**名字 ⇒ 无论原变量是否已赋值一律报 `unbound variable`；
脚本整体 abort。`${var}` 形态不受影响（右花括号是 ASCII，明确终止名字）。

判据
----
在 **非单引号、非注释、非转义** 的位置：
    $ <ASCII 名字 [A-Za-z_][A-Za-z0-9_]*> 紧跟 ord(ch) > 127 的字符  ⇒ 隐患
特例（故意不报，防假阳性）：
    ${var}（花括号形态）、$1/$@/$*/$?/$$/$!（特殊参数，数字/符号后即终止）、
    `"$var"）`（引号是 ASCII，名字已被终止）、`$var,`（ASCII 标点）。

严重度
------
同一文件里若出现 `set -u` / `set -o nounset` ⇒ ERROR（实际会 abort）；
否则 WARN（变量名不同必然读错值，但不一定崩）。

子命令（位置参数，非 --flag）
----------------------------
    scan   [--file F | --dir D] [--json] [--summary]
    probe  [--bash PATH] [--json]        # 用真实 bash 双 locale 实证判据（只读、临时目录）
    verify [--file F | --dir D] [--bash PATH]   # scan∧probe 双证：有隐患必须实证复现，否则 rc=2
    selftest
退出码：scan 有 findings ⇒ 1；干净 ⇒ 0；usage/内部错 ⇒ 2；verify 未复现 ⇒ 2（fail-closed）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SCHEMA = "ga.shell_utf8_var_lint.v1"
JUDGE = "SHELL_VAR_MULTIBYTE_HAZARD"
SEV_HAZARD = "ERROR"
SEV_HAZARD_SOFT = "WARN"
SET_FLAGS_RE = re.compile(r"(?<![\w-])set\s+(?:-[a-zA-Z]+\s*)+")
NOUNSET_O_RE = re.compile(r"set\s+-o\s+nounset")


NAME_START = re.compile(r"[A-Za-z_]")
NAME_CHARS = re.compile(r"[A-Za-z0-9_]")
SPECIAL = "@*#?$!-0123456789"


# --------------------------------------------------------------------------
# 扫描内核
# --------------------------------------------------------------------------
def has_nounset(text):
    """是否启用 -u / nounset（含 `set -euo pipefail`、shebang `-eu`、`set -o nounset`）。"""
    lines = (text or "").split("\n")
    if lines and lines[0].startswith("#!") and re.search(r"-\w*u\b", lines[0]):
        return True
    for ln in lines:
        s2 = ln.strip()
        if s2.startswith("#"):
            continue
        code = ln.split(" #")[0]
        for m in SET_FLAGS_RE.finditer(code):
            for fl in re.findall(r"-([a-zA-Z]+)", m.group(0)):
                if "u" in fl:
                    return True
        if NOUNSET_O_RE.search(code):
            return True
    return False


def scan_text(text, path=None):
    """返回 findings 列表：[{judge, severity, path, line, col, name, snippet, nounset}]"""
    nounset = has_nounset(text)
    sev = SEV_HAZARD if nounset else SEV_HAZARD_SOFT
    out = []
    state = "normal"          # normal | squote | dquote
    line_no = 1
    col0 = 0                  # 当前行起始的全局下标
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            line_no += 1
            col0 = i + 1
            i += 1
            continue
        if state == "squote":
            if ch == "'":
                state = "normal"
            i += 1
            continue
        if ch == "\\" and state != "squote":
            i += 2
            continue
        if ch == "'" and state == "normal":
            state = "squote"
            i += 1
            continue
        if ch == '"':
            state = "normal" if state == "dquote" else "dquote"
            i += 1
            continue
        if ch == "#" and state == "normal":
            prev = text[i - 1] if i > 0 else "\n"
            if prev in " \t\n;&|(":
                # 注释直到行尾
                j = text.find("\n", i)
                i = n if j < 0 else j
                continue
        if ch == "$":
            j = i + 1
            if j < n and text[j] == "{":
                # ${NAME...} 形态安全，跳到 '}' 之后（支持 ${a:-b} 之类）
                k = text.find("}", j)
                i = n if k < 0 else k + 1
                continue
            if j < n and text[j] in SPECIAL:
                i = j + 1
                continue
            if j < n and NAME_START.match(text[j]):
                k = j
                while k < n and NAME_CHARS.match(text[k]):
                    k += 1
                if k < n and ord(text[k]) > 127:
                    ln_start = col0
                    line_txt = text[ln_start:text.find("\n", ln_start) if
                                    text.find("\n", ln_start) >= 0 else n]
                    out.append({
                        "judge": JUDGE,
                        "severity": sev,
                        "path": path,
                        "line": line_no,
                        "col": i - ln_start + 1,
                        "name": text[j:k],
                        "next": text[k],
                        "nounset": nounset,
                        "snippet": ("%s$%s%s%s" % (line_txt[:i - ln_start],
                                                   text[j:k], text[k],
                                                   line_txt[i - ln_start + 1 +
                                                            (k - j):]))[:160],
                    })
                    i = k
                    continue
                i = k
                continue
            i += 1
            continue
        i += 1
    return out


def scan_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError as e:
        return [{"judge": "READ_ERROR", "severity": "INFO", "path": path,
                 "detail": str(e)}]
    if len(txt) > 8 * 1024 * 1024:
        return [{"judge": "SKIPPED_TOO_LARGE", "severity": "INFO", "path": path,
                 "detail": "%d bytes" % len(txt)}]
    if not looks_like_shell(path, txt):
        return []
    return scan_text(txt, path)


SHELL_SHEBANG = re.compile(r"#!.*\b(bash|sh|zsh|ksh|dash)\b")
SHELL_EXTS = (".sh", ".bash", ".zsh", ".command", ".ksh")


def looks_like_shell(path, txt=None):
    if path and (path.endswith(SHELL_EXTS) or os.path.basename(path) in
                 ("bashrc", ".bash_profile", ".zshrc", ".profile")):
        return True
    if txt is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read(4096)
        except OSError:
            return False
    head = (txt or "")[:200]
    return bool(SHELL_SHEBANG.search(head))


# --------------------------------------------------------------------------
# 实证探针：用真实 bash 证明判据成立（只读；只在临时目录里造夹具）
# --------------------------------------------------------------------------
def find_bash(explicit=None):
    cands = [explicit, "/opt/homebrew/bin/bash", "/bin/bash", "/usr/bin/bash"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("bash")


def _run(bash, script, lc):
    env = dict(os.environ)
    env["LC_CTYPE"] = lc
    env["LC_ALL"] = "" if lc == "C" else env.get("LC_ALL", "")
    if lc == "C":
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
    r = subprocess.run([bash, script], capture_output=True, encoding="utf-8",
                       errors="replace", env=env,
                       cwd=tempfile.gettempdir(), timeout=30)
    return r.returncode, (r.stdout + r.stderr)


UNBOUND_MSGS = ("unbound variable", "未绑定的变量", "unbound")


def is_unbound_msg(text):
    """bash 的 unbound 报错文本随 locale 本地化⇒中英双语判据。"""
    t = (text or "")
    return any(m in t for m in UNBOUND_MSGS)


def probe(bash=None):
    """双形态 × 双 locale 的实证矩阵。返回 {bash, rows:[...], conclusion}。"""
    bash = find_bash(bash)
    if not bash:
        return {"ok": False, "error": "no bash found", "rows": []}
    d = tempfile.mkdtemp(prefix="shutf8_")
    try:
        haz = os.path.join(d, "hazard.sh")
        ok = os.path.join(d, "braced.sh")
        with open(haz, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\nset -euo pipefail\n'
                    'locked="1.2.3.4"\n'
                    'printf "hazard: %s\\n" "（$locked）"\n')
        with open(ok, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\nset -euo pipefail\n'
                    'locked="1.2.3.4"\n'
                    'printf "braced: %s\\n" "（${locked}）"\n')
        rows = []
        for name, script in (("hazard", haz), ("braced", ok)):
            for lc in ("UTF-8", "C"):
                rc, txt = _run(bash, script, lc)
                rows.append({"form": name, "locale": lc, "rc": rc,
                             "unbound": is_unbound_msg(txt),
                             "out": txt.strip()[:200]})
        hz_utf8 = [r for r in rows if r["form"] == "hazard" and r["locale"] == "UTF-8"][0]
        hz_c = [r for r in rows if r["form"] == "hazard" and r["locale"] == "C"][0]
        br = [r for r in rows if r["form"] == "braced"]
        conclusion = {
            "hazard_aborts_under_utf8": hz_utf8["rc"] != 0 and hz_utf8["unbound"],
            "hazard_ok_under_C": hz_c["rc"] == 0,
            "braced_ok_both": all(r["rc"] == 0 for r in br),
        }
        conclusion["ok"] = all(conclusion.values())
        return {"ok": conclusion["ok"], "bash": bash, "rows": rows,
                "conclusion": conclusion}
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def collect_files(files=None, dirs=None, default_dir=None):
    out = []
    for f in (files or []):
        if os.path.isfile(f):
            out.append(os.path.abspath(f))
    for d in (dirs or []):
        if not os.path.isdir(d):
            continue
        for root, dnames, fnames in os.walk(d):
            dnames[:] = [x for x in dnames if x not in (".git", "node_modules")]
            for fn in fnames:
                p = os.path.join(root, fn)
                if looks_like_shell(p):
                    out.append(os.path.abspath(p))
    if default_dir and os.path.isdir(default_dir):
        for root, dnames, fnames in os.walk(default_dir):
            for fn in fnames:
                p = os.path.join(root, fn)
                if looks_like_shell(p):
                    out.append(os.path.abspath(p))
    return sorted(set(out))


def scan(files=None, dirs=None, default_dir=None):
    """扫描；显式给出的文件/目录不存在一律进 missing（调用方须 fail-closed）。

    口径：目录不存在 ⇒ missing；显式文件不存在 ⇒ missing。绝不静默当 0 文件。
    """
    missing = []
    for f in (files or []):
        if not os.path.isfile(f):
            missing.append(os.path.abspath(f))
    for d in (dirs or []):
        if not os.path.isdir(d):
            missing.append(os.path.abspath(d))
    fs = collect_files(files, dirs, default_dir)
    findings = []
    for p in fs:
        findings += scan_file(p)
    return {"schema": SCHEMA, "files": fs, "n_files": len(fs),
            "missing": missing, "n_missing": len(missing),
            "findings": findings, "n_findings": len(findings),
            "vulnerable": sorted({f["path"] for f in findings
                                  if f["judge"] == JUDGE and f.get("severity") == SEV_HAZARD})}


# --------------------------------------------------------------------------
# selftest（含负控 + 真实 bash 实证）
# --------------------------------------------------------------------------
def selftest():
    n_pass = n_fail = 0
    log = []

    def chk(name, cond, extra=None):
        nonlocal n_pass, n_fail
        if cond:
            n_pass += 1
        else:
            n_fail += 1
            log.append("FAIL %s %s" % (name, "" if extra is None else extra))

    # --- 判据正例 -------------------------------------------------------
    haz = 'set -u\nx=1\nsay "未找到唯一的锁定行（$locked）"\n'
    f = scan_text(haz, "t.sh")
    chk("pos_hazard_found", len(f) == 1, f)
    chk("pos_sev_error_with_set_u", f and f[0]["severity"] == SEV_HAZARD, f)
    chk("pos_name_correct", f and f[0]["name"] == "locked", f)
    chk("pos_line_number", f and f[0]["line"] == 3, f)
    chk("pos_next_is_multibyte", f and ord(f[0]["next"]) > 127, f)
    # 无 set -u ⇒ WARN
    f2 = scan_text('x=1\nsay "（$locked）"\n', "t.sh")
    chk("pos_warn_without_nounset", f2 and f2[0]["severity"] == SEV_HAZARD_SOFT, f2)

    # --- 负控（必须不报）------------------------------------------------
    neg = {
        "neg_braced": 'set -u\nsay "（${locked}）"\n',
        "neg_braced_default": 'set -u\nsay "（${locked:-none}）"\n',
        "neg_single_quoted": "set -u\nsay '（$locked）'\n",
        "neg_comment": "set -u\n# （$locked） 注释里的不算\nx=1\n",
        "neg_escaped": 'set -u\nsay "（\\$locked）"\n',
        "neg_ascii_punct": 'set -u\nprintf "%s,%s\\n" "$locked" "$x"\n',
        "neg_quoted_then_multibyte": 'set -u\nsay "($locked)"）tail\n',
        "neg_positional": 'set -u\nsay "($1）"\n',
        "neg_special_at": 'set -u\nsay "($@）"\n',
        "neg_special_star": 'set -u\nsay "($*）"\n',
        "neg_special_q": 'set -u\nsay "($?）"\n',
        "neg_special_dollar": 'set -u\nsay "($$）"\n',
        "neg_special_bang": 'set -u\nsay "($!）"\n',
        "neg_ascii_underscore_then_brace": 'set -u\necho "${a}bc"\n',
        "neg_clean_script": "#!/bin/bash\nset -euo pipefail\necho hi\n",
    }
    for k, txt in sorted(neg.items()):
        got = scan_text(txt, k)
        chk(k, got == [], got)

    # --- 多隐患 / 同文件多行 --------------------------------------------
    multi = 'set -u\nsay "（$a）"\necho "（$b）"\n'
    fm = scan_text(multi, "m.sh")
    chk("multi_two_findings", len(fm) == 2, fm)
    chk("multi_distinct_lines", sorted(x["line"] for x in fm) == [2, 3], fm)

    # --- 负控：显式目标不存在必须 fail-closed（勿静默 0 文件）-----------
    r_missing = scan(files=["/nonexistent/definitely_not_here.sh"])
    chk("neg_missing_listed", r_missing["n_missing"] == 1
        and "/nonexistent/definitely_not_here.sh" in r_missing["missing"],
        r_missing["missing"])
    chk("neg_missing_zero_files", r_missing["n_files"] == 0, r_missing["n_files"])
    import io as _io, contextlib as _ctx
    _buf = _io.StringIO()
    with _ctx.redirect_stdout(_buf):
        _rc = main(["scan", "--file", "/nonexistent/definitely_not_here.sh"])
    chk("neg_missing_rc2", _rc == 2, _rc)
    r_dirmiss = scan(dirs=["/nonexistent/dir_here"])
    chk("neg_missing_dir", r_dirmiss["n_missing"] == 1, r_dirmiss["missing"])

    # --- looks_like_shell / 文件级 --------------------------------------
    d = tempfile.mkdtemp(prefix="shutf8_st_")
    try:
        p1 = os.path.join(d, "h.sh")
        open(p1, "w", encoding="utf-8").write('set -u\nsay "（$locked）"\n')
        p2 = os.path.join(d, "plain.txt")
        open(p2, "w", encoding="utf-8").write('$locked）\n')   # 无 shebang 非 .sh
        p3 = os.path.join(d, "noext")
        open(p3, "w", encoding="utf-8").write('#!/usr/bin/env bash\nset -u\n'
                                             'say "（$locked）"\n')
        chk("file_sh_ext", len(scan_file(p1)) == 1, scan_file(p1))
        chk("file_noext_shebang", len(scan_file(p3)) == 1, scan_file(p3))
        chk("file_non_shell_skipped", scan_file(p2) == [], scan_file(p2))
        # 目录级 + 去重
        r = scan(dirs=[d])
        chk("dir_scan_counts", r["n_files"] == 2, r["n_files"])   # 仅 h.sh 与带 shebang 的 noext 属 shell
        chk("dir_scan_vulnerable", len(r["vulnerable"]) == 2, r["vulnerable"])
        # 大文件跳过（负控：超限不崩且标记跳过）
        pb = os.path.join(d, "big.sh")
        open(pb, "w", encoding="utf-8").write("#!/bin/bash\n" + "x=1\n" * 3000000)
        rb = scan_file(pb)
        chk("big_file_skipped", rb and rb[0]["judge"] == "SKIPPED_TOO_LARGE", rb[:1])
        # 不可读文件 ⇒ READ_ERROR 而非异常
        pr = os.path.join(d, "gone.sh")
        chk("missing_file_read_error",
            scan_file(pr) and scan_file(pr)[0]["judge"] == "READ_ERROR",
            scan_file(pr))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # --- 真实 bash 实证（正控：判据必须能被复现；否则本工具结论无据）-----
    pr = probe()
    chk("probe_ok", pr.get("ok") is True, pr.get("conclusion"))
    con = pr.get("conclusion") or {}
    chk("probe_hazard_aborts_utf8", con.get("hazard_aborts_under_utf8") is True, con)
    chk("probe_hazard_ok_C", con.get("hazard_ok_under_C") is True, con)
    chk("probe_braced_ok_both", con.get("braced_ok_both") is True, con)

    # --- 与真实脚本联动：修好的脚本必须 0 findings -----------------------
    fixed = ('#!/bin/bash\nset -euo pipefail\nlocked="1.1.1.1"\n'
             'say "未找到唯一的锁定行（${locked}）"\n')
    chk("fixed_script_clean", scan_text(fixed, "f.sh") == [], scan_text(fixed, "f.sh"))
    chk("nounset_detected", has_nounset("set -o nounset\n") and
        has_nounset("set -euo pipefail\n") and not has_nounset("# set -u\n"))

    print("selftest: %d passed, %d failed" % (n_pass, n_fail))
    for l in log:
        print("  " + l)
    return 0 if n_fail == 0 else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _opt(rest, name, default=None):
    if name in rest:
        i = rest.index(name)
        return rest[i + 1] if i + 1 < len(rest) else default
    return default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "selftest":
        return selftest()
    if cmd == "probe":
        r = probe(_opt(rest, "--bash"))
        if "--json" in rest:
            print(json.dumps({k: v for k, v in r.items()}, ensure_ascii=False,
                             indent=2))
        else:
            print("bash=%s ok=%s" % (r.get("bash"), r.get("ok")))
            for row in r.get("rows", []):
                print("  %-7s locale=%-6s rc=%d unbound=%s"
                      % (row["form"], row["locale"], row["rc"], row["unbound"]))
            print("conclusion=%s" % json.dumps(r.get("conclusion", {}),
                                               ensure_ascii=False))
        return 0 if r.get("ok") else 2
    if cmd in ("scan", "verify"):
        files = [rest[i + 1] for i, a in enumerate(rest) if a == "--file"]
        dirs = [rest[i + 1] for i, a in enumerate(rest) if a == "--dir"]
        res = scan(files, dirs)
        if res["n_missing"]:
            for m in res["missing"]:
                print("MISSING_TARGET %s（路径不存在⇒fail-closed，勿当 0 文件）" % m,
                      file=sys.stderr)
        if "--json" in rest:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("files=%d findings=%d vulnerable=%d"
                  % (res["n_files"], res["n_findings"], len(res["vulnerable"])))
            for f in res["findings"]:
                print("  [%s] %s:%s:%s %s"
                      % (f.get("severity"), f.get("path"), f.get("line"),
                         f.get("col"), f.get("judge")))
        if res["n_missing"]:
            return 2
        if cmd == "scan":
            return 1 if res["n_findings"] else 0
        # verify：有隐患必须能被真实 bash 复现，否则 rc=2（fail-closed）
        pr = probe(_opt(rest, "--bash"))
        if not pr.get("ok"):
            print("verify: probe 未复现判据（无法证明隐患真实）⇒ fail-closed")
            print("conclusion=%s" % json.dumps(pr.get("conclusion", {}),
                                               ensure_ascii=False))
            return 2
        print("verify: %d findings, probe 复现=%s ⇒ %s"
              % (res["n_findings"], pr["ok"],
                 "RESOLVED_OR_CLEAN" if not res["n_findings"] else "CONFIRMED"))
        return 1 if res["n_findings"] else 0
    print("unknown subcommand: %s" % cmd)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
