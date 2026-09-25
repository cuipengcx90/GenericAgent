#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clone_reproduce_audit.py — 「克隆可得性」审计（R418；只读；真 git clone 作判据）

任务来源：temp/TODO.txt 第 11 行（C1，第13批四路评审 7/9/8/8=8.00，收窄=只出清单+待批提案）。
背景（已实测）：memory/ 盘上 340 文件仅 279 tracked（61 untracked）；memory/global_mem.txt（L2）
被 .gitignore:55 `memory/**` 忽略且 `git log -- memory/global_mem.txt` 从未有提交 = **本地唯一
副本**；temp/ 整目录 gitignored。⇒ 记忆/报告里大量引用「新环境克隆后不存在」的路径，换机/
新克隆即断链，且**不报错**（与 R410 ref_integrity 的 EXISTS/GONE 只查本地工作树、看不出这层）。

判定轴（与 R410 正交，故非重复建设）：
    CLONE_OK    随克隆可得（在真 clone 的 HEAD 树里）
    LOCAL_ONLY  仅本地工作树（本地存在、未被 ignore、但未 tracked ⇒ `git add` 后可得）
    IGNORED     gitignored：**新克隆即丢**（L2 / temp / 快照类）
    GONE        本地与克隆都没有（真断链）
    OUT_REPO_*  解析到本仓以外（~/develop/herdr-agents/... 等），另有归属仓

复用（零行重写判定/抽取内核）：
    · 抽取 = tools/memory_assert_audit.py（R378）的 TOK_RE / corpus_files / build_basename_index
    · 路径解析 = tools/report_assert_audit.py 的 classify()（kind/漂移/ambig 同一来源）
本工具**新增**的只有四件：
    ① 真 clone（打印克隆路径 + `git rev-parse HEAD` + tracked 文件数）
    ② git 侧三态批量判定（clone 树存在性 / `ls-files` / `check-ignore --stdin` 一次过）
    ③ L1+L2+L3 被引路径逐条落表（--json 可机读，供后续对账/台账）
    ④ 负控 + 只读自证（corpus sha1 与 `git status --porcelain` 指纹前后一致）

只读保证：不改仓库、不改 .gitignore；任何 corpus 文件被写即 rc=3；`git status --porcelain`
指纹变化即 rc=3。写盘只落 temp/（gitignored）。

用法：
  python3 tools/clone_reproduce_audit.py                       # 默认全流程
  python3 tools/clone_reproduce_audit.py --min-refs 50 --strict # 收窄闸门（<50 或 GONE>0 → rc1）
  python3 tools/clone_reproduce_audit.py --clone-dir /tmp/xx   # 复用已有克隆（跳过 clone）
  python3 tools/clone_reproduce_audit.py --rm                  # 审计完删掉克隆
  python3 tools/clone_reproduce_audit.py --selftest            # 合成仓库 4 例（确定性）
  python3 tools/clone_reproduce_audit.py --negctl              # 真语料 3 例（含本批核心事实）
rc 契约：0 正常；--strict 且 (refs<min_refs 或 GONE>0) → 1；写违规 → 3；selftest/negctl 失败 → 2。
"""
import argparse
import collections
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import memory_assert_audit as maa  # noqa: E402
import report_assert_audit as raa  # noqa: E402

INDEX_ROOTS = [REPO, os.path.join(maa.HOME, "develop"), os.path.join(maa.HOME, "projects"),
               os.path.join(maa.HOME, ".agents")]
PATH_KINDS = ("REPO", "UP", "ABS", "TMP", "HOME", "EXT")   # 解析到具体路径的 kind（BARE 不算）
VERDICTS = ("CLONE_OK", "LOCAL_ONLY", "IGNORED", "GONE", "OUT_REPO_EXISTS", "OUT_REPO_GONE")


def git(args, cwd=REPO, input_text=None):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                       input=input_text)
    return p.returncode, p.stdout, p.stderr


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def status_fingerprint(cwd=REPO):
    rc, out, err = git(["status", "--porcelain"], cwd=cwd)
    return rc, hashlib.sha1(out.encode("utf-8")).hexdigest(), out


def real_clone(dest, repo=REPO):
    """真 clone（--no-hardlinks ⇒ 独立对象库，非硬链接共享）。"""
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest) or "/tmp", exist_ok=True)
    t0 = time.time()
    rc, out, err = git(["clone", "--quiet", "--no-hardlinks", repo, dest], cwd=repo)
    dt = round(time.time() - t0, 1)
    if rc != 0:
        return {"ok": False, "dir": dest, "error": err.strip()[:400], "seconds": dt}
    r1, head, _ = git(["rev-parse", "HEAD"], cwd=dest)
    r2, files, _ = git(["ls-files"], cwd=dest)
    nt = len([l for l in files.splitlines() if l.strip()])
    size = subprocess.run(["du", "-sh", dest], capture_output=True, text=True).stdout.split()[0]
    return {"ok": True, "dir": dest, "head": head.strip(), "tracked_files": nt,
            "size": size, "seconds": dt,
            "cmd": f"git clone --quiet --no-hardlinks {repo} {dest}",
            "note": "本地 clone 会忽略 --depth（git 提示 use file://）；此处有意用全量真 clone 以保守取证"}


def extract(items, index):
    """[(label, text)] → 被引路径记录（判定链全部来自 raa.classify，本工具零行重写）。"""
    skip = collections.Counter()
    recs = []
    for label, text in items:
        for ln, line in enumerate(text.split("\n"), 1):
            for m in maa.TOK_RE.finditer(line):
                tok = m.group(1)
                r = raa.classify(tok, line[:m.start(1)], line, index, skip)
                if not r or r["kind"] not in PATH_KINDS:
                    continue
                recs.append({"file": label, "line": ln, "tok": tok, "kind": r["kind"],
                             "resolved": r["path"], "drift": r["drift"]})
    return recs, skip


def verdicts(paths, clone_dir, repo_root=REPO):
    """路径列表 → 克隆可得性三态（check-ignore 一批过；clone 树存在性=真判据）。"""
    repo_real = os.path.realpath(repo_root)
    out = [None] * len(paths)
    relmap, rels = {}, []
    for i, p in enumerate(paths):
        if not p or not os.path.isabs(p):
            out[i] = {"path": p, "rel": "", "verdict": "OUT_REPO_GONE", "note": "非绝对路径"}
            continue
        rp = os.path.realpath(p)
        if rp == repo_real or rp.startswith(repo_real + os.sep):
            rel = os.path.relpath(rp, repo_real)
            relmap[i] = rel
            rels.append(rel)
        else:
            out[i] = {"path": rp, "rel": "",
                      "verdict": "OUT_REPO_EXISTS" if os.path.lexists(rp) else "OUT_REPO_GONE"}
    ignored = set()
    if rels:
        rc, so, se = git(["check-ignore", "--stdin"], cwd=repo_real,
                         input_text="\n".join(sorted(set(rels))) + "\n")
        ignored = set(l.strip() for l in so.splitlines() if l.strip())
    for i, rel in relmap.items():
        local = os.path.lexists(os.path.join(repo_real, rel))
        in_clone = bool(clone_dir) and os.path.lexists(os.path.join(clone_dir, rel))
        if in_clone:
            v = "CLONE_OK"
        elif local and rel in ignored:
            v = "IGNORED"
        elif local:
            v = "LOCAL_ONLY"
        else:
            v = "GONE"
        out[i] = {"path": os.path.join(repo_real, rel), "rel": rel, "verdict": v,
                  "local": local, "in_clone": in_clone, "ignored_rule": rel in ignored}
    return out


def corpus_items():
    """L1/L2/L3 语料（maa.corpus_files 同一来源：memory/*.md + *.txt + autonomous_operation_sop/*.md）。"""
    items = []
    for p in maa.corpus_files(include_reports=False):
        try:
            items.append((p, open(p, encoding="utf-8", errors="replace").read()))
        except OSError:
            pass
    return items


def audit(a):
    os.chdir(REPO)
    t0 = time.time()
    items = corpus_items()
    if not items:
        print("no corpus found"); return 2
    pre_sha = {p: sha1(p) for p, _ in items}
    rc_s0, fp0, st0 = status_fingerprint()

    cl = None
    if a.clone_dir:
        if os.path.isdir(os.path.join(a.clone_dir, ".git")):
            r1, head, _ = git(["rev-parse", "HEAD"], cwd=a.clone_dir)
            r2, files, _ = git(["ls-files"], cwd=a.clone_dir)
            cl = {"ok": True, "dir": a.clone_dir, "head": head.strip(),
                  "tracked_files": len([l for l in files.splitlines() if l.strip()]),
                  "size": subprocess.run(["du", "-sh", a.clone_dir], capture_output=True,
                                         text=True).stdout.split()[0],
                  "seconds": None, "reused": True}
        else:
            cl = real_clone(a.clone_dir)
    else:
        cl = real_clone(os.path.join(tempfile.gettempdir(),
                                     f"ga_clone_audit_{time.strftime('%Y%m%d_%H%M%S')}"))
    if not cl["ok"]:
        print(json.dumps({"clone": cl}, ensure_ascii=False)); return 2

    index, index_stat = ({}, None) if a.no_index else maa.build_basename_index(INDEX_ROOTS)
    recs, skip = extract(items, index)
    vs = verdicts([r["resolved"] for r in recs], cl["dir"])
    for r, v in zip(recs, vs):
        r["verdict"] = v["verdict"]
        r["rel"] = v["rel"]
        r["ignored_rule"] = v.get("ignored_rule", False)

    by_verdict = collections.Counter(r["verdict"] for r in recs)
    distinct = collections.defaultdict(set)
    for r in recs:
        if r["rel"]:
            distinct[r["verdict"]].add(r["rel"])
    per_file = collections.defaultdict(lambda: collections.Counter())
    for r in recs:
        per_file[r["file"]][r["verdict"]] += 1

    post_sha = {p: sha1(p) for p, _ in items}
    rc_s1, fp1, st1 = status_fingerprint()
    written = [p for p in pre_sha if pre_sha[p] != post_sha[p]]
    status_changed = fp0 != fp1

    # 观测到的 .gitignore 规则（对 IGNORED 命中的规则反查）
    ignored_paths = sorted(distinct["IGNORED"])
    rule_of = {}
    if ignored_paths:
        rc, so, se = git(["check-ignore", "-v", "--stdin"],
                         input_text="\n".join(ignored_paths) + "\n")
        for l in so.splitlines():
            parts = l.split("\t")
            if len(parts) >= 2:
                rule_of[parts[-1].strip()] = parts[0].strip()

    out = {
        "schema": "ga.clone_reproduce_audit/v1",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "clone": cl,
        "corpus": {"files": len(items), "bytes": sum(os.path.getsize(p) for p, _ in items)},
        "refs_total": len(recs),
        "by_verdict": dict(by_verdict),
        "distinct_by_verdict": {k: len(v) for k, v in distinct.items()},
        "skip": dict(skip),
        "per_file": {k: dict(v) for k, v in sorted(per_file.items())},
        "ignored_rules": rule_of,
        "readonly": {"corpus_written": written, "status_changed": status_changed,
                     "status_before": st0, "status_after": st1},
        "seconds": round(time.time() - t0, 1),
        "refs": [{"file": r["file"], "line": r["line"], "tok": r["tok"], "kind": r["kind"],
                  "rel": r["rel"], "verdict": r["verdict"]} for r in recs],
    }
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    secs = cl.get("seconds")
    print(f"[clone] {cl['dir']}  HEAD={cl['head']}  tracked={cl['tracked_files']}  "
          f"size={cl['size']}  " + (f"{secs}s" if secs is not None else "(复用已有 clone，未计时)"))
    print(f"[cmd]   {cl.get('cmd') or '(复用已有 clone，未执行 clone 命令)'}")
    print(f"[corpus] {len(items)} 文件 / {out['corpus']['bytes']} B；被引路径 token {len(recs)} 条")
    print(f"[verdict] {dict(by_verdict)}")
    print(f"[distinct] {out['distinct_by_verdict']}")
    print(f"[skip] {dict(skip)}")
    print(f"[readonly] corpus_written={written} status_changed={status_changed}")
    for v in VERDICTS:
        if distinct.get(v):
            sample = sorted(distinct[v])[:8]
            print(f"  {v} ({len(distinct[v])} 个不同路径) e.g. {sample}")
    if a.min_refs and len(recs) < a.min_refs:
        print(f"[FAIL] 抽取 {len(recs)} < min_refs={a.min_refs}")
    print(f"[json] {a.json}  ({out['seconds']}s)")
    if a.rm:
        shutil.rmtree(cl["dir"], ignore_errors=True); print(f"[rm] {cl['dir']}")
    if written or status_changed:
        print("WRITE VIOLATION / STATUS CHANGED"); return 3
    if a.strict and (len(recs) < a.min_refs or by_verdict.get("GONE")):
        return 1
    return 0


def _synth_repo():
    """合成仓库：tracked / gitignored / untracked / 不存在 四态各一。"""
    d = tempfile.mkdtemp(prefix="cra_synth_")
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(cmd, cwd=d)
    open(os.path.join(d, "tracked.txt"), "w").write("keep\n")
    open(os.path.join(d, ".gitignore"), "w").write("*.log\n")
    open(os.path.join(d, "ignored.log"), "w").write("drop\n")
    open(os.path.join(d, "untracked.txt"), "w").write("new\n")
    git(["add", "tracked.txt", ".gitignore"], cwd=d)
    git(["commit", "-qm", "init"], cwd=d)
    return d


def selftest(a):
    """合成仓库 4 例：CLONE_OK / IGNORED / LOCAL_ONLY / GONE 各判对。"""
    d = _synth_repo()
    try:
        cl = real_clone(os.path.join(tempfile.gettempdir(), "cra_synth_clone"), repo=d)
        if not cl["ok"]:
            print("clone failed", cl); return 2
        paths = [os.path.join(d, x) for x in ("tracked.txt", "ignored.log",
                                              "untracked.txt", "missing.txt")]
        want = ["CLONE_OK", "IGNORED", "LOCAL_ONLY", "GONE"]
        got = [v["verdict"] for v in verdicts(paths, cl["dir"], repo_root=d)]
        ok = got == want
        # 负载守恒：clone 里必须只有 tracked.txt（.gitignore 也在）
        rc, files, _ = git(["ls-files"], cwd=cl["dir"])
        tracked = sorted(l.strip() for l in files.splitlines() if l.strip())
        ok2 = tracked == [".gitignore", "tracked.txt"]
        print(f"[selftest] verdicts want={want} got={got} {'PASS' if ok else 'FAIL'}")
        print(f"[selftest] clone ls-files={tracked} {'PASS' if ok2 else 'FAIL'}")
        return 0 if (ok and ok2) else 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(os.path.join(tempfile.gettempdir(), "cra_synth_clone"), ignore_errors=True)


def negctl(a):
    """真语料 3 例：①不存在路径必报缺失 ②真存在路径不得误报 ③L2 本体必判「新克隆即丢」。"""
    os.chdir(REPO)
    cl = real_clone(os.path.join(tempfile.gettempdir(), "cra_negctl_clone"))
    if not cl["ok"]:
        print("clone failed"); return 2
    cases = [
        ("NC1 不存在路径必报缺失",
         "候选产物：tools/__r418_ghost_probe__.py 已落盘\n",
         {"tools/__r418_ghost_probe__.py": "GONE"}),
        ("NC2 真存在路径不得误报",
         "既有工具 tools/ref_integrity.py 与 tools/log_compress.py\n",
         {"tools/ref_integrity.py": "CLONE_OK", "tools/log_compress.py": "CLONE_OK"}),
        ("NC3 gitignored 必判新克隆即丢（本批核心事实）",
         "事实库 memory/global_mem.txt 与 临时区 temp/report_assert_audit.json\n",
         {"memory/global_mem.txt": "IGNORED"}),
    ]
    results = []
    allok = True
    for name, text, want in cases:
        recs, _ = extract([("negctl", text)], {})
        got = {}
        for r, v in zip(recs, verdicts([r["resolved"] for r in recs], cl["dir"])):
            got[os.path.relpath(v["path"], REPO) if v["rel"] else r["tok"]] = v["verdict"]
        ok = all(got.get(k) == v for k, v in want.items())
        allok &= ok
        results.append({"case": name, "want": want, "got": got, "pass": ok})
        gotstr = ", ".join(f"{k}={got.get(k)}" for k in want)
        print(f"[{name}] want={want} got={{{gotstr}}} {'PASS' if ok else 'FAIL'}  all={got}")
    shutil.rmtree(cl["dir"], ignore_errors=True)
    print(f"[negctl] {sum(r['pass'] for r in results)}/{len(results)} PASS")
    return 0 if allok else 2


def main():
    ap = argparse.ArgumentParser(description="克隆可得性审计（只读；真 clone 作判据）")
    ap.add_argument("--clone-dir", default="", help="复用已有克隆目录（跳过 clone）")
    ap.add_argument("--json", default=os.path.join(REPO, "temp/clone_reproduce_audit.json"))
    ap.add_argument("--min-refs", type=int, default=50, help="被引路径下限（默认 50）")
    ap.add_argument("--strict", action="store_true", help="refs<min 或 GONE>0 → rc=1")
    ap.add_argument("--no-index", action="store_true", help="跳过 basename 索引")
    ap.add_argument("--rm", action="store_true", help="审计完删除克隆")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--negctl", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest(a)
    if a.negctl:
        return negctl(a)
    return audit(a)


if __name__ == "__main__":
    sys.exit(main())
