#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_asset_durability.py — memory 非 .md 资产的耐久性闸门（只读）

为什么需要它（TODO L208 / 第24批 C5）：
  .gitignore:55 `memory/**` 会忽略 memory 下**一切**非 .md 资产，只有 `!memory/**/*.md`
  放行散文。于是 memory 里的 .py/.txt/.json/.zip 只有被 `git add -f` 显式入库的才随
  干净克隆可得（先例：memory/credential_surface/credentials.json、
  memory/kc_profile/kc_app_usage_fields.json、memory/tool_surface/*.json）。
  一旦某个被工具/闸门真消费的资产没入库，「干净 clone」里那条链路必坏，而失效点
  不在工具里、也没人报错 —— 是静默的能力空洞。

本器从**资产侧**问三个问题（R418 是从「记忆里被引用的路径」侧看，两者互补）：
  ① 它在 git 里吗？（tracked / tracked+ignored(即 add -f 入库) / ignored / untracked）
  ② 谁真消费它？（代码级引用：PATH 强证据 / GLOB 目录级证据 / BASENAME 弱证据）
  ③ 若未入库，谁保证可恢复？（T0 保护面 temp/t0_baseline.sha256 + T0 归档成员表）

判定：未入库 ∧ 有消费者 ∧ 未登记 ⇒ HOLE（rc=1）。

口径（与 R418 六态对账，不重立）：
  CLONE_OK    ↔ tracked ∧ 未被 ignore        （随克隆可得）
  IGNORED     ↔ 被 .gitignore 命中 ∧ 未 tracked
  LOCAL_ONLY  ↔ 被 ignore ∧ 唯一副本在本机   （需在本器登记面声明）
  GONE        ↔ 登记面指向的资产盘上不存在
  OUT_REPO_*  ↔ 仓外资产（不在本器范围，R418 侧覆盖）
  ⇒ 本器新增的判据只有「有消费者」这一维 + 「有消费者但未入库必须登记」。

引用证据分级（避免把「报告里提过一嘴」当消费者）：
  PATH  强：语料行含 `memory/<...>/<name>` 全路径或去 `memory/` 前缀的**含目录**路径
  GLOB  中：语料行含该资产的父目录路径 ∧ 该扩展名（如 `memory/L4_raw_sessions/*.zip`）
  BASE  弱：语料行只含裸文件名（可能出现在用例夹具/文档字符串里，单条不作数）
  触发口径：PATH ≥1 ∨ GLOB ≥1 ∨ BASE 命中**不同文件** ≥2

子命令（位置参数，非 --flag；与 R471/R480/R499 工具一致）：
  scan    [--root R] [--registry P] [--json-out OUT] [--t0-baseline P]
          [--t0-archive P] [--no-archive] [--quiet]
  selftest
  proof   [--registry P]

只读保证：scan/proof 不写任何仓库文件（除 --json-out 指定的路径）；不联网；不 shell 出网。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOT_DEFAULT = os.path.join(REPO, "memory")
REGISTRY_DEFAULT = os.path.join(REPO, "memory", "asset_durability", "REGISTRY.json")
T0_BASELINE_DEFAULT = os.path.join(REPO, "temp", "t0_baseline.sha256")
T0_ARCHIVE_CACHE = os.path.join(REPO, "temp", "memory_asset_durability", "t0_archive_members.txt")

MACHINE_EXTS = {".json", ".txt", ".py", ".zip", ".sh", ".csv", ".yaml", ".yml", ".tsv", ".log"}
PROSE_EXTS = {".md"}
SCAN_EXTRA_ROOTS = ["tools", "reflect_scripts", "sche_tasks", "llmcore", "memory"]
CONSUMER_EXTS = {".py", ".json", ".sh", ".plist", ".yaml", ".yml"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
MAX_FILE = 4 * 1024 * 1024  # 语料单文件上限（超过则跳过并计数，防静默缩水：会打印 skipped）

VALID_STATUS = {"DECLARED_LOCAL_ONLY", "PENDING_APPROVAL", "DEFER"}


# ---------------------------------------------------------------- 基础工具
def sh(args, cwd=REPO):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    except OSError as e:  # pragma: no cover
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def sha256_file(path, cap=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def walk_files(root, exts=None, skip_dirs=SKIP_DIRS):
    out = []
    for r, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d not in skip_dirs]
        for f in fs:
            p = os.path.join(r, f)
            if exts is None or os.path.splitext(f)[1].lower() in exts:
                out.append(p)
    return sorted(out)


def list_assets(root):
    """memory 下的资产清单（相对仓库路径）。"""
    rows = []
    for p in walk_files(root):
        rel = os.path.relpath(p, REPO).replace(os.sep, "/")
        ext = os.path.splitext(p)[1].lower()
        kind = "PROSE" if ext in PROSE_EXTS else ("MACHINE" if ext in MACHINE_EXTS or ext == "" else "OTHER")
        st = os.stat(p)
        rows.append({"path": rel, "ext": ext, "kind": kind, "bytes": st.st_size,
                     "abs": p})
    return rows


# ---------------------------------------------------------------- git 侧
def git_tracked(rel_prefix="memory"):
    rc, out, err = sh(["git", "-c", "core.quotepath=false", "ls-files", "-z", "--", rel_prefix])
    if rc != 0:
        raise RuntimeError("git ls-files failed: %s" % err.strip())
    return set(x for x in out.split("\0") if x)


def git_ignored(paths):
    """批量 check-ignore；返回 {path: rule}（只含被忽略者）。"""
    if not paths:
        return {}
    proc = subprocess.run(["git", "check-ignore", "-v", "-z", "--stdin"], cwd=REPO,
                          input="\0".join(paths), capture_output=True, text=True)
    fields = [x for x in proc.stdout.split("\0")]
    res = {}
    i = 0
    while i + 3 < len(fields):
        src, lineno, pattern, path = fields[i], fields[i + 1], fields[i + 2], fields[i + 3]
        i += 4
        if path:
            res[path] = "%s:%s %s" % (os.path.relpath(src, REPO), lineno, pattern)
    return res


# ---------------------------------------------------------------- 语料
def consumer_corpus(root=REPO):
    files, skipped = [], []
    for name in SCAN_EXTRA_ROOTS:
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        for p in walk_files(d, exts=CONSUMER_EXTS):
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if rel.startswith("temp/") or rel.startswith("logs/"):
                continue
            files.append(p)
    # 仓库根目录下的脚本（ga.py / agentmain.py / setup.py ...）
    for f in os.listdir(root):
        p = os.path.join(root, f)
        if os.path.isfile(p) and os.path.splitext(f)[1].lower() in CONSUMER_EXTS:
            files.append(p)
    # 仓外 launchd：只读 plist，占位证据（EXTERNAL 面）
    la = os.path.expanduser("~/Library/LaunchAgents")
    if os.path.isdir(la) and os.path.realpath(root) == os.path.realpath(REPO):
        files.extend(walk_files(la, exts={".plist"}))
    uniq, seen = [], set()
    for p in files:
        rp = os.path.realpath(p)
        if rp in seen or not os.path.isfile(p):
            continue
        if os.path.getsize(p) > MAX_FILE:
            skipped.append(os.path.relpath(p, root))
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq, skipped


def read_corpus(files, root=REPO):
    corpus = []
    for p in files:
        try:
            txt = open(p, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if p.startswith(os.path.expanduser("~/Library/LaunchAgents")):
            rel = "~/Library/LaunchAgents/" + os.path.basename(p)
        else:
            rel = os.path.relpath(p, root).replace(os.sep, "/")
        corpus.append((rel, txt))
    return corpus


def asset_tokens(rel):
    """返回 (strong_tokens, glob_parent, basename)。"""
    base = os.path.basename(rel)
    inner = rel[len("memory/"):] if rel.startswith("memory/") else rel
    strong = [rel]
    if "/" in inner:
        strong.append(inner)
    parent = os.path.dirname(inner) or "memory"
    if rel.startswith("memory/"):
        parent = "memory/" + parent if parent != "memory" else "memory"
    return strong, parent, base


def evidence_index(assets, corpus):
    """单遍扫描语料，为每个资产收集引用证据（三档）。

    性能：逐资产×全语料的行级 in 检查在真仓上要跑上亿次比较，故改为
    一次编译三档正则（PATH/GLOB/BASE 各一条交替式），对每行 finditer 反查资产。
    证据上限 CAP=40 行/资产（判据只看档位与文件数，不需要全量行）。
    """
    strong_map, parent_map, base_map = {}, {}, {}
    for i, a in enumerate(assets):
        if a["kind"] != "MACHINE":
            continue
        strong, parent, base = asset_tokens(a["path"])
        for t in strong:
            strong_map.setdefault(t, []).append(i)
        # 顶层资产的 parent 就是 "memory"（太泛，会假触发）⇒ 只登记嵌套目录
        if parent and "/" in parent and parent != base:
            parent_map.setdefault(parent, []).append(i)
        base_map.setdefault(base, []).append(i)

    def _compile(m):
        if not m:
            return None
        pat = "|".join(re.escape(t) for t in sorted(m, key=len, reverse=True))
        return re.compile(pat)

    sre, pre, bre = _compile(strong_map), _compile(parent_map), _compile(base_map)
    rows = {}
    amb = {}
    CAP = 40
    ctx_re = re.compile(r"[A-Za-z0-9_./\\-]*$")

    def _add(pth, tier, rel, ln, line, stripped):
        """登记证据；资产自身提到自己的名字不算消费者（自引用）。"""
        if rel == pth:
            return stripped
        cur = rows.setdefault(pth, [])
        s2 = stripped or line.strip()[:160]
        if len(cur) < CAP:
            cur.append({"file": rel, "line": ln, "tier": tier, "text": s2})
        return s2

    for rel, txt in corpus:
        for ln, line in enumerate(txt.split("\n"), 1):
            stripped = None
            if sre is not None:
                for m in sre.finditer(line):
                    for i in strong_map.get(m.group(0), ()):
                        stripped = _add(assets[i]["path"], "PATH", rel, ln, line, stripped)
            if pre is not None:
                low = line.lower()
                globish = ("/*" in line) or ("glob" in low) or ("listdir" in low) or ("walk" in low)
                for m in pre.finditer(line):
                    for i in parent_map.get(m.group(0), ()):
                        ext = assets[i]["ext"]
                        if ext and ext in line and (("*" + ext) in line or globish):
                            stripped = _add(assets[i]["path"], "GLOB", rel, ln, line, stripped)
            if bre is not None:
                for m in bre.finditer(line):
                    for i in base_map.get(m.group(0), ()):
                        pth = assets[i]["path"]
                        # 裸名前的路径上下文（若有）⇒ 该处指向的是另一个同名文件
                        # （实测 memory/history.txt 的证据全来自 memory/autonomous_reports/history.txt）
                        ctx = ctx_re.search(line, 0, m.start()).group(0)
                        if ctx and "/" in ctx:
                            full = (ctx + m.group(0)).lstrip("./")
                            if not pth.endswith(full):
                                amb.setdefault(pth, set()).add(full)
                                continue
                        stripped = _add(pth, "BASE", rel, ln, line, stripped)
    out = {}
    for a in assets:
        if a["kind"] != "MACHINE":
            continue
        rs = rows.get(a["path"], [])
        tiers = sorted(set(r["tier"] for r in rs))
        base_files = sorted(set(r["file"] for r in rs if r["tier"] == "BASE"))
        # 强档（PATH/GLOB）= 路径级证据；BASE（裸名）无法判定指的是哪个同名文件
        # （实测 memory/history.txt 的 6 条 BASE 证据全来自 memory/autonomous_reports/history.txt）
        # ⇒ 只为弱信号，不单独构成「有消费者」。
        triggered = ("PATH" in tiers) or ("GLOB" in tiers)
        s = {"tiers": tiers, "triggered": triggered, "base_files": base_files,
             "weak": bool(base_files) and not triggered}
        if amb.get(a["path"]):
            s["ambiguous"] = sorted(amb[a["path"]])
        out[a["path"]] = (rs, s)
    return out


# ---------------------------------------------------------------- T0 可恢复性
def load_baseline(path):
    names = set()
    if not path or not os.path.exists(path):
        return None
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            names.add(parts[1])
    return names


def baseline_has(names, rel):
    if names is None:
        return None
    return any(n.endswith("/" + rel) or n == rel for n in names)


def archive_members(archive=None, cache=T0_ARCHIVE_CACHE):
    """T0 归档成员表（只读 tar -tzf；结果缓存到 temp/ 便于复跑）。"""
    if archive is None:
        d = os.path.join(REPO, "temp", "t0_backups")
        cands = []
        if os.path.isdir(d):
            cands = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".tar.gz")]
        if not cands:
            return None, "no-archive"
        archive = cands[-1]
    if cache and os.path.exists(cache):
        mtime_cache = os.path.getmtime(cache)
        if mtime_cache >= os.path.getmtime(archive):
            return [l.strip() for l in open(cache, encoding="utf-8") if l.strip()], archive
    rc, out, err = sh(["tar", "-tzf", archive])
    if rc != 0:
        return None, "tar-failed:%s" % err.strip()[:80]
    if cache:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(out)
    return [l.strip() for l in out.split("\n") if l.strip()], archive


def archive_has(members, rel):
    if members is None:
        return None
    suf = "/" + rel
    return any(m.endswith(suf) or m.split("/")[-1] == os.path.basename(rel) for m in members)


# ---------------------------------------------------------------- 登记面
def load_registry(path):
    if not path or not os.path.exists(path):
        return {"entries": [], "_missing": True}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        # fail-closed：登记面读不动就停，绝不当成「空登记面」继续判（否则洞会被静默放过）
        sys.stderr.write("REGISTRY_UNREADABLE: %s (%s)\n" % (path, exc))
        raise SystemExit(2)
    if not isinstance(data, dict):
        sys.stderr.write("REGISTRY_BAD_SHAPE: %s（须为对象）\n" % path)
        raise SystemExit(2)
    data.setdefault("entries", [])
    return data


def check_registry(registry, assets_by_path, tracked, evidence_fs=None):
    """逐条校验登记项的证据与状态。返回 (entries_map, problems)。"""
    problems = []
    emap = {}
    for e in registry.get("entries", []):
        p = e.get("path")
        prob = []
        if not p:
            problems.append(("BAD_ENTRY", "(no path)", "登记项缺 path"))
            continue
        if p in emap:
            problems.append(("DUPLICATE", p, "同一资产重复登记"))
        emap[p] = e
        if p not in assets_by_path:
            prob.append("STALE_MISSING: 盘上不存在")
        if p in tracked:
            prob.append("STALE_TRACKED: 已入库（登记面应撤销）")
        if e.get("status") not in VALID_STATUS:
            prob.append("BAD_STATUS: %r" % e.get("status"))
        ev = e.get("evidence") or []
        if not ev:
            prob.append("CLAIM_WITHOUT_EVIDENCE: 无证据行")
        for item in ev:
            m = re.match(r"^(.*?):(\d+)$", item or "")
            if not m:
                prob.append("BAD_EVIDENCE_FORM: %s（须 file:line）" % item)
                continue
            f, ln = m.group(1), int(m.group(2))
            if evidence_fs is not None:
                if f not in evidence_fs:
                    prob.append("EVIDENCE_NOFILE: %s" % item)
                    continue
                lines = evidence_fs[f]
            else:
                fp = os.path.join(REPO, f)
                if not os.path.exists(fp):
                    prob.append("EVIDENCE_NOFILE: %s" % item)
                    continue
                lines = open(fp, "r", encoding="utf-8", errors="replace").read().split("\n")
            if ln < 1 or ln > len(lines):
                prob.append("EVIDENCE_LINEOFF: %s" % item)
                continue
            txt = lines[ln - 1]
            toks = e.get("evidence_tokens") or [os.path.basename(p), p]
            if not any(t in txt for t in toks):
                prob.append("EVIDENCE_MISMATCH: %s 该行不含 %s" % (item, "/".join(toks)))
        for pr in prob:
            problems.append((pr.split(":")[0], p, pr))
    return emap, problems


# ---------------------------------------------------------------- 判定内核（纯函数，供 selftest）
def classify(assets, tracked, ignored, corpus, registry, baseline_names=None,
             archive_members_list=None, evidence_fs=None):
    """纯判定内核：返回 (rows, problems)。evidence_fs 供自测注入假证据文件。"""
    assets_by_path = {a["path"]: a for a in assets}
    emap, reg_problems = check_registry(registry, assets_by_path, tracked, evidence_fs)
    ev_index = evidence_index(assets, corpus)
    rows, problems = [], list(reg_problems)
    for a in assets:
        p = a["path"]
        if a["kind"] != "MACHINE":
            continue
        tr = p in tracked
        ig = ignored.get(p)
        ev, summary = ev_index[p]
        if tr:
            state = "TRACKED_FORCED" if ig else "TRACKED"
        elif summary["triggered"]:
            state = "UNTRACKED_CONSUMED"
        elif summary.get("weak"):
            state = "UNTRACKED_WEAK_EVIDENCE"
        else:
            state = "UNTRACKED_UNCONSUMED"
        entry = emap.get(p)
        declared = entry.get("status") if entry else None
        if state == "UNTRACKED_CONSUMED":
            verdict = declared if declared else "HOLE"
        elif state in ("UNTRACKED_UNCONSUMED", "UNTRACKED_WEAK_EVIDENCE"):
            verdict = declared or "INFO"
        else:
            verdict = declared if declared else "OK"
        if verdict == "HOLE":
            problems.append(("HOLE", p, "未被 git 跟踪 ∧ 有消费者 ∧ 未登记"))
        rows.append({
            "path": p, "ext": a["ext"], "bytes": a["bytes"], "tracked": tr,
            "ignore_rule": ig, "state": state,
            "tiers": summary["tiers"], "n_evidence": len(ev),
            "evidence": ev[:6], "declared": declared, "verdict": verdict,
            "t0_baseline": baseline_has(baseline_names, p),
            "t0_archive": archive_has(archive_members_list, p),
            "ambiguous": summary.get("ambiguous"),
            "weak_evidence": bool(summary.get("weak")),
        })
    return rows, problems


def summarize(rows, problems):
    by = {}
    for r in rows:
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    return {"n_assets": len(rows), "by_verdict": by,
            "n_problems": len(problems),
            "problem_kinds": sorted(set(k for k, _, _ in problems))}


# ---------------------------------------------------------------- scan
def scan(args):
    root = args.root or SCAN_ROOT_DEFAULT
    assets = list_assets(root)
    tracked = git_tracked(os.path.relpath(root, REPO).replace(os.sep, "/"))
    cands = [a["path"] for a in assets if a["path"] not in tracked]
    ignored = git_ignored(cands)
    files, skipped = consumer_corpus(REPO)
    corpus = read_corpus(files)
    registry = load_registry(args.registry)
    baseline_names = None if args.no_t0 else load_baseline(args.t0_baseline)
    if args.no_archive or args.no_t0:
        members, arc = None, "skipped"
    else:
        members, arc = archive_members(args.t0_archive)
    rows, problems = classify(assets, tracked, ignored, corpus, registry,
                              baseline_names, members)
    rows.sort(key=lambda r: ({"HOLE": 0}.get(r["verdict"], 1), -r["bytes"]))
    su = summarize(rows, problems)
    su.update({
        "corpus_files": len(corpus),
        "corpus_bytes": sum(len(t) for _, t in corpus),
        "corpus_skipped": skipped,
        "assets_total": len(assets),
        "assets_prose": sum(1 for a in assets if a["kind"] == "PROSE"),
        "registry": args.registry, "registry_missing": registry.get("_missing", False),
        "t0_baseline": args.t0_baseline if baseline_names is not None else None,
        "t0_archive": arc,
        "tracked_total": len(tracked),
    })
    if not args.quiet:
        print("== memory 资产耐久性（只读）==")
        print("语料 %d 文件/%.2fMB（跳过超限 %d）；memory 资产 %d（散文 %d）；tracked %d"
              % (len(corpus), su["corpus_bytes"] / 1e6, len(skipped), len(assets),
                 su["assets_prose"], len(tracked)))
        print("登记面 %s%s；T0 基线 %s；T0 归档 %s"
              % (args.registry, "（缺失⇒按空表）" if su["registry_missing"] else "",
                 os.path.basename(args.t0_baseline) if baseline_names is not None else "off",
                 arc))
        print("判据：未入库 ∧ 有消费者 ∧ 未登记 ⇒ HOLE")
        print("-" * 96)
        head = "%-46s %8s %13s %-10s %-18s" % ("资产", "字节", "状态", "证据档", "判定")
        print(head)
        for r in rows:
            print("%-46s %8d %13s %-10s %-18s" % (
                r["path"][:46], r["bytes"], r["state"], ",".join(r["tiers"]) or "-",
                r["verdict"]))
        print("-" * 96)
        for r in rows:
            if r["evidence"]:
                print("[证据] %s" % r["path"])
                for e in r["evidence"][:3]:
                    print("   %s:%d (%s) %s" % (e["file"], e["line"], e["tier"], e["text"][:110]))
        print("统计 %s" % json.dumps(su["by_verdict"], ensure_ascii=False, sort_keys=True))
        if problems:
            print("问题 %d 条：" % len(problems))
            for k, p, msg in problems:
                print("  [%s] %s — %s" % (k, p, msg))
        else:
            print("问题 0 条")
    payload = {"generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
               "summary": su, "rows": rows,
               "problems": [{"kind": k, "path": p, "msg": m} for k, p, m in problems]}
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        if not args.quiet:
            print("JSON 落盘 %s" % args.json_out)
    return 1 if problems else 0


def proof(args):
    """只读自证：跑一遍 scan 的输入指纹，再跑一遍，比对。"""
    root = SCAN_ROOT_DEFAULT
    reg = args.registry or REGISTRY_DEFAULT

    def fingerprint():
        fp = {}
        for a in list_assets(root):
            fp[a["path"]] = (a["bytes"], int(os.path.getmtime(a["abs"])))
        for p in (reg, T0_BASELINE_DEFAULT):
            fp["@" + p] = os.path.exists(p) and sha256_file(p)
        return fp

    a = fingerprint()
    ns = argparse.Namespace(root=SCAN_ROOT_DEFAULT, registry=reg, json_out=None,
                            t0_baseline=T0_BASELINE_DEFAULT, t0_archive=None,
                            no_archive=False, no_t0=False, quiet=True)
    rc = scan(ns)
    b = fingerprint()
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    print("proof: assets=%d registry=%s scan_rc=%d changed=%s" %
          (len([k for k in a if not k.startswith("@")]), os.path.relpath(reg, REPO) if reg else "-",
           rc, changed or "[]"))
    return 1 if changed else 0


# ---------------------------------------------------------------- 自测
def selftest():
    checks = []

    def chk(name, cond, extra=""):
        checks.append((name, bool(cond), extra))

    def mk(path, size=10, ext=None):
        return {"path": path, "ext": ext if ext is not None else os.path.splitext(path)[1],
                "kind": "MACHINE", "bytes": size, "abs": "/dev/null"}

    corpus = [("tools/consumer.py", "p = 'memory/thing/alpha.txt'\n"),
              ("tools/other.py", "q = 'beta.json'\n"),
              ("tools/globber.py", "glob('memory/thing/*.bin')\n")]
    EV_FS = {"tools/consumer.py": ["p = 'memory/thing/alpha.txt'"],
             "tools/other.py": ["q = 'beta.json'"]}

    def run(assets, tracked=(), ignored=None, corpus_=None, registry=None):
        return classify(assets, set(tracked), ignored or {},
                        corpus_ if corpus_ is not None else corpus,
                        registry or {"entries": []}, evidence_fs=EV_FS)

    # NC1 未跟踪 ∧ 有消费者 ∧ 未登记 ⇒ HOLE
    r, p = run([mk("memory/thing/alpha.txt")])
    chk("NC1 HOLE", r[0]["verdict"] == "HOLE" and any(k == "HOLE" for k, _, _ in p), r[0]["verdict"])
    # NC2 同一资产登记 DECLARED_LOCAL_ONLY 且证据有效 ⇒ 不再 HOLE
    reg = {"entries": [{"path": "memory/thing/alpha.txt", "status": "DECLARED_LOCAL_ONLY",
                        "evidence": ["tools/consumer.py:1"], "rationale": "本机唯一副本"}]}
    r2, p2 = run([mk("memory/thing/alpha.txt")], registry=reg)
    chk("NC2 已登记消解", r2[0]["verdict"] == "DECLARED_LOCAL_ONLY" and not p2, p2[:1])
    # NC3 已跟踪的资产永不判 HOLE（即使有消费者）
    r3, _ = run([mk("memory/thing/alpha.txt")], tracked=["memory/thing/alpha.txt"])
    chk("NC3 tracked 免判", r3[0]["verdict"] == "OK" and r3[0]["state"] == "TRACKED")
    # NC4 未跟踪 ∧ 无消费者 ⇒ INFO 不算问题
    r4, p4 = run([mk("memory/thing/quiet.txt")])
    chk("NC4 无消费者 INFO", r4[0]["verdict"] == "INFO" and not p4, r4[0]["verdict"])
    # NC5 登记项无证据行 ⇒ CLAIM_WITHOUT_EVIDENCE
    bad = {"entries": [{"path": "memory/thing/alpha.txt", "status": "DECLARED_LOCAL_ONLY",
                        "evidence": [], "rationale": "x"}]}
    _, p5 = run([mk("memory/thing/alpha.txt")], registry=bad)
    chk("NC5 无证据必报", any(k == "CLAIM_WITHOUT_EVIDENCE" for k, _, _ in p5), p5)
    # NC6 证据行号对了但该行不含 token ⇒ EVIDENCE_MISMATCH
    bad2 = {"entries": [{"path": "memory/thing/alpha.txt", "status": "DECLARED_LOCAL_ONLY",
                         "evidence": ["tools/other.py:1"], "rationale": "x"}]}
    _, p6 = run([mk("memory/thing/alpha.txt")], registry=bad2)
    chk("NC6 证据行不含 token", any(k == "EVIDENCE_MISMATCH" for k, _, _ in p6), p6)
    # NC7 登记项指向盘上不存在的资产 ⇒ STALE_MISSING
    bad3 = {"entries": [{"path": "memory/thing/gone.txt", "status": "DEFER",
                         "evidence": ["tools/consumer.py:1"], "rationale": "x"}]}
    _, p7 = run([mk("memory/thing/alpha.txt")], registry=bad3)
    chk("NC7 STALE_MISSING", any(k == "STALE_MISSING" for k, _, _ in p7), p7)
    # NC8 登记项指向已入库资产 ⇒ STALE_TRACKED
    _, p8 = run([mk("memory/thing/alpha.txt")], tracked=["memory/thing/alpha.txt"], registry=reg)
    chk("NC8 STALE_TRACKED", any(k == "STALE_TRACKED" for k, _, _ in p8), p8)
    # NC9 语料构造器必须排除散文(.md) 与 temp/ 脚本（否则报告里的提及会被当消费者）
    fix = tempfile.mkdtemp(prefix="ma_fix_", dir=os.path.join(REPO, "temp"))
    try:
        os.makedirs(os.path.join(fix, "tools"))
        os.makedirs(os.path.join(fix, "memory"))
        os.makedirs(os.path.join(fix, "temp"))
        open(os.path.join(fix, "tools", "real.py"), "w", encoding="utf-8").write("x\n")
        open(os.path.join(fix, "memory", "prose.md"), "w", encoding="utf-8").write("memory/thing/x\n")
        open(os.path.join(fix, "temp", "scratch.py"), "w", encoding="utf-8").write("y\n")
        got, _sk = consumer_corpus(fix)
        got_rel = sorted(os.path.relpath(x, fix) for x in got)
        chk("NC9 语料排除 .md/temp", got_rel == ["tools/real.py"], got_rel)
    finally:
        shutil.rmtree(fix, ignore_errors=True)
    # NC15 裸名被更长的路径限定到**另一个同名文件** ⇒ 不算本资产的证据（防假 HOLE，
    #     实测 memory/history.txt 的 BASE 证据全来自 memory/autonomous_reports/history.txt）
    r15, _ = run([mk("memory/thing/beta.json")],
                 corpus_=[("tools/a.py", "p = 'memory/other/beta.json'\n"),
                          ("tools/b.py", "q = 'memory/other/beta.json'\n")])
    chk("NC15 同名他文件不误判", r15[0]["verdict"] == "INFO" and not r15[0]["tiers"]
        and r15[0]["ambiguous"] == ["memory/other/beta.json"],
        (r15[0]["verdict"], r15[0]["tiers"], r15[0]["ambiguous"]))
    # NC15b 同一资产走 PATH 强档时不受歧义规则影响
    r15b, _ = run([mk("memory/thing/beta.json")],
                  corpus_=[("tools/a.py", "x = 'memory/thing/beta.json'\n")])
    chk("NC15b PATH 强档不受影响", r15b[0]["verdict"] == "HOLE" and "PATH" in r15b[0]["tiers"],
        r15b[0]["tiers"])
    # NC15c 资产自身提到自己（自引用）不算消费者
    r15c, _ = run([mk("memory/thing/self.txt")],
                  corpus_=[("memory/thing/self.txt", "self.txt 自引用 self.txt\n")])
    chk("NC15c 自引用不触发", r15c[0]["verdict"] == "INFO" and not r15c[0]["tiers"],
        (r15c[0]["verdict"], r15c[0]["tiers"]))
    # NC10 裸文件名只在 1 个文件出现 ⇒ 证据不足（不触发）
    r10, _ = run([mk("memory/thing/beta.json")])
    chk("NC10 单条 BASE 不触发", r10[0]["tiers"] == ["BASE"] and r10[0]["verdict"] == "INFO",
        r10[0]["tiers"])
    # NC11 裸文件名在 ≥2 文件出现 ⇒ 仍只是弱证据（不判洞；无法确定指哪个同名文件）
    corpus11 = [("tools/a.py", "beta.json\n"), ("tools/b.py", "beta.json\n")]
    r11, _ = run([mk("memory/thing/beta.json")], corpus_=corpus11)
    chk("NC11 BASE×2 只算弱证据", r11[0]["verdict"] == "INFO" and r11[0]["weak_evidence"]
        and r11[0]["state"] == "UNTRACKED_WEAK_EVIDENCE",
        (r11[0]["verdict"], r11[0]["state"]))
    # NC12 目录级 glob 证据 ⇒ 触发（L4 zip 那类）
    r12, _ = run([mk("memory/thing/x.bin", ext=".bin")])
    chk("NC12 GLOB 触发", "GLOB" in r12[0]["tiers"] and r12[0]["verdict"] == "HOLE", r12[0]["tiers"])
    # NC13 非法 status ⇒ BAD_STATUS
    bad4 = {"entries": [{"path": "memory/thing/alpha.txt", "status": "OK",
                         "evidence": ["tools/consumer.py:1"], "rationale": "x"}]}
    _, p13 = run([mk("memory/thing/alpha.txt")], registry=bad4)
    chk("NC13 非法状态必报", any(k == "BAD_STATUS" for k, _, _ in p13), p13)
    # NC14 证据文件不存在 ⇒ EVIDENCE_NOFILE
    bad5 = {"entries": [{"path": "memory/thing/alpha.txt", "status": "DEFER",
                         "evidence": ["tools/nope.py:1"], "rationale": "x"}]}
    _, p14 = run([mk("memory/thing/alpha.txt")], registry=bad5)
    chk("NC14 证据文件不存在", any(k == "EVIDENCE_NOFILE" for k, _, _ in p14), p14)
    # NC17 登记面坏了必须 fail-closed（rc=2），绝不能当成空登记面（否则洞被静默放过）
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        bad_json = os.path.join(td, "bad.json")
        with open(bad_json, "w", encoding="utf-8") as f:
            f.write("not-json")
        try:
            load_registry(bad_json)
            chk("NC17 坏登记面 fail-closed", False, "未抛 SystemExit")
        except SystemExit as exc:
            chk("NC17 坏登记面 fail-closed", exc.code == 2, exc.code)
        # NC18 登记面是数组（形状错）同样 fail-closed
        arr_json = os.path.join(td, "arr.json")
        with open(arr_json, "w", encoding="utf-8") as f:
            f.write("[]")
        try:
            load_registry(arr_json)
            chk("NC18 形状错 fail-closed", False, "未抛 SystemExit")
        except SystemExit as exc:
            chk("NC18 形状错 fail-closed", exc.code == 2, exc.code)
    # 正控：T0 面判据（None 表示未提供，不冒充实测）
    chk("PC1 T0 未提供=UNKNOWN", baseline_has(None, "memory/x.txt") is None)
    chk("PC2 T0 命中", baseline_has({"/repo/memory/x.txt"}, "memory/x.txt") is True)
    chk("PC3 T0 未命中", baseline_has({"/repo/memory/y.txt"}, "memory/x.txt") is False)
    chk("PC4 归档命中(后缀)", archive_has(["ga/memory/x.txt"], "memory/x.txt") is True)
    # 正控：summarize
    su = summarize(r, p)
    chk("PC5 summarize", su["by_verdict"].get("HOLE") == 1 and su["n_problems"] >= 1)

    fails = [c for c in checks if not c[1]]
    for name, ok, extra in checks:
        print("  %s %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else "→ %s" % (extra,)))
    print("selftest %d/%d %s" % (len(checks) - len(fails), len(checks),
                                 "OK" if not fails else "FAILED"))
    return 1 if fails else 0


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="memory 非 .md 资产耐久性闸门（只读）")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("scan", "proof", "selftest"):
        sp = sub.add_parser(name)
        sp.add_argument("--root", default=None)
        sp.add_argument("--registry", default=REGISTRY_DEFAULT)
        sp.add_argument("--json-out", default=None)
        sp.add_argument("--t0-baseline", default=T0_BASELINE_DEFAULT)
        sp.add_argument("--t0-archive", default=None)
        sp.add_argument("--no-archive", action="store_true")
        sp.add_argument("--no-t0", action="store_true")
        sp.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "scan":
        return scan(a)
    if a.cmd == "proof":
        return proof(a)
    if a.cmd == "selftest":
        return selftest()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
