#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sche_tasks prompt 的「cwd 吃态」确定性修复器（R375/C65 · TODO L193）。

背景
----
调度器执行 sche_tasks/*.json 的 prompt 时，cwd 可以是 repo 根、repo/temp 或别处
（R375 实测三 cwd 漂移）。prompt 里写 `../temp/x.py` / `../memory/y.md` 这类
「显式锚定」相对路径，在 cwd=repo 根时会解析到仓库外（repo 的上一级）⇒ 硬失败；
`./temp/x` 在 cwd=repo/temp 时落点漂移进 temp/temp（R375 另一形态）。

修法（沙盒三形态 A/B 实测取优者，见 temp/l193_notes/NOTES.md）
----
1) prompt 头部插入一行锚定（逐字沿用 R375 报告里的原句）：
       : "${GA_ROOT:=<repo根绝对路径>}"; export GA_ROOT; cd "$GA_ROOT" || exit 3
   ⇒ 之后的相对路径一律以 repo 根为基准（`./temp/x` 也归位）。
2) 把 `../{memory,temp,tools}/…` 改写成 `{memory,temp,tools}/…`（依赖上一步 cd）。

边界（安全）
----
* 只动 `../` + `memory|temp|tools` 三种前缀；`../<其它>` 一律不碰并计入 unsupported。
* 写盘必须显式 `--approve "<批准语>"`；缺/错一律 rc=2 且零写入（fail-closed）。
* 写盘前做「字节级往返自证」：`dumps(parse(原文))` 必须逐字节等于原文，
  否则判 FORMAT_MISMATCH 拒绝改该文件（不改写用户文件的排版）。
* 保留原文的尾换行状态（有无都不变）。
* rewrite 幂等：rewrite(rewrite(p)) == rewrite(p)。
* plan / verify / proof 全程只读（verify 只在 temp/ 下写沙盒）。

子命令（位置参数）
----
    plan   [--tasks-dir D] [--json F]     仅报告将做的改动
    verify [--tasks-dir D]                沙盒 A/B：lint 的 FAIL 数 before→after
    apply  --approve "<token>"            真写盘（先备份到 temp/l193_backup/<ts>/）
    proof                                 只读自证：plan 前后文件 sha256 不变
    selftest                              自测（含负控）

批准语（须逐字）：批准 DECISIONS_sche_tasks_cwd_anchor.md:D1
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

APPROVAL_TOKEN = "批准 DECISIONS_sche_tasks_cwd_anchor.md:D1"
APPROVAL_HINT = ('批准 DECISIONS_sche_tasks_cwd_anchor.md:D1 '
                 '（台账 temp/pending_decisions/DECISIONS_sche_tasks_cwd_anchor.md）')

# 锚定行模板（root 在运行期注入；GA 下逐字等于 R375 报告原句）
ANCHOR_TMPL = ': "${GA_ROOT:=%s}"; export GA_ROOT; cd "$GA_ROOT" || exit 3'

# 只改写这三种前缀
REWRITE_RE = re.compile(r'(?<![A-Za-z0-9_./\-])\.\./(memory|temp|tools)(/?)(?![A-Za-z0-9_])')
# 需要锚定的信号：显式 ../ 或 ./ 相对引用，或裸的 memory|temp|tools 开头路径
NEED_RE = re.compile(r'(?<![A-Za-z0-9_./\-])\.\./(?:memory|temp|tools)(?![A-Za-z0-9_])'
                     r'|(?<![A-Za-z0-9_./\-])\./(?![./])'
                     r'|(?<![A-Za-z0-9_./\-])(?:memory|temp|tools)/')
UNSUPPORTED_RE = re.compile(r'(?<![A-Za-z0-9_./\-])\.\./([A-Za-z0-9_.\-]+)')

LINT = os.path.join(HERE, "sche_tasks_lint.py")


# ---------------------------------------------------------------- 核心变换
def anchor_line(root):
    return ANCHOR_TMPL % root


def rewrite_prompt(prompt, root):
    """返回 (new_prompt, info)。纯函数，幂等。"""
    info = {"anchor_added": False, "replacements": [], "unsupported": []}
    new = prompt

    if "GA_ROOT" not in new and NEED_RE.search(new):
        new = anchor_line(root) + "\n" + new
        info["anchor_added"] = True

    out = []
    last = 0
    for m in REWRITE_RE.finditer(new):
        out.append(new[last:m.start()])
        out.append(m.group(1) + m.group(2))
        info["replacements"].append((m.group(0), m.group(1) + m.group(2)))
        last = m.end()
    out.append(new[last:])
    new = "".join(out)

    # 记录未被支持的 ../ 引用（仅提示，不改）
    for m in UNSUPPORTED_RE.finditer(new):
        if m.group(1) not in ("memory", "temp", "tools"):
            info["unsupported"].append(m.group(0))
    return new, info


def dump_like(original_raw, data):
    """按原文排版还原；返回 (text, fidelity_ok)。"""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if original_raw.endswith("\n"):
        body += "\n"
    return body


def check_fidelity(raw, data):
    """原文是否为「indent=2/ensure_ascii=False + 原样尾换行」的规范落盘格式。"""
    return dump_like(raw, data) == raw


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 16), b""):
            h.update(blk)
    return h.hexdigest()


# ---------------------------------------------------------------- 扫描
def load_tasks(tasks_dir):
    """→ (items, errors)；items=[{id, path, raw, data}]"""
    items, errors = [], []
    for p in sorted(glob.glob(os.path.join(tasks_dir, "*.json"))):
        tid = os.path.basename(p)[:-5]
        try:
            raw = open(p, encoding="utf-8").read()
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            errors.append({"id": tid, "path": p, "code": "JSON_PARSE_ERROR", "detail": str(e)})
            continue
        if not isinstance(data, dict):
            errors.append({"id": tid, "path": p, "code": "NOT_OBJECT", "detail": type(data).__name__})
            continue
        items.append({"id": tid, "path": p, "raw": raw, "data": data})
    return items, errors


def analyze(items, root):
    res = []
    for it in items:
        prompt = it["data"].get("prompt") or ""
        if not isinstance(prompt, str):
            res.append({"id": it["id"], "path": it["path"], "changed": False,
                        "code": "PROMPT_NOT_STR", "anchor_added": False,
                        "replacements": [], "unsupported": []})
            continue
        new, info = rewrite_prompt(prompt, root)
        changed = new != prompt
        rec = {"id": it["id"], "path": it["path"], "changed": changed,
               "anchor_added": info["anchor_added"],
               "replacements": info["replacements"],
               "unsupported": info["unsupported"],
               "fidelity": check_fidelity(it["raw"], it["data"]),
               "code": "OK"}
        # 幂等自证
        new2, _ = rewrite_prompt(new, root)
        rec["idempotent"] = (new2 == new)
        rec["new_prompt"] = new
        res.append(rec)
    return res


def render(rec):
    bits = []
    if rec.get("anchor_added"):
        bits.append("+anchor")
    if rec.get("replacements"):
        bits.append("replace×%d" % len(rec["replacements"]))
    if not bits:
        bits.append("no-change")
    return "%s | %s" % (rec["id"], ",".join(bits))


def cmd_plan(args):
    tasks_dir = args.tasks_dir or os.path.join(args.root, "sche_tasks")
    items, errors = load_tasks(tasks_dir)
    recs = analyze(items, args.root)
    print("== plan（只读） tasks_dir=%s tasks=%d errors=%d" % (tasks_dir, len(items), len(errors)))
    nchg = 0
    for r in recs:
        mark = "CHANGE" if r["changed"] else "keep  "
        if r["changed"]:
            nchg += 1
        extra = ""
        if r.get("replacements"):
            extra = " " + " ".join("%s→%s" % (a, b) for a, b in r["replacements"])
        if r.get("unsupported"):
            extra += " [unsupported: %s]" % ",".join(sorted(set(r["unsupported"])))
        if not r.get("fidelity", True):
            extra += " [FORMAT_MISMATCH: apply 将拒改]"
        print("  %s %s%s" % (mark, render(r), extra))
    for e in errors:
        print("  ERROR %s | %s | %s" % (e["id"], e["code"], e["detail"][:80]))
    print("汇总: 将改动 %d / %d，幂等 %d/%d，格式可无损改写 %d/%d"
          % (nchg, len(recs),
             sum(1 for r in recs if r.get("idempotent")),
             len(recs),
             sum(1 for r in recs if r.get("fidelity")),
             len(recs)))
    if args.json:
        out = [{k: v for k, v in r.items() if k != "new_prompt"} for r in recs]
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"tasks_dir": tasks_dir, "root": args.root,
                       "approval_token": APPROVAL_TOKEN,
                       "tasks": out, "errors": errors}, f, ensure_ascii=False, indent=2)
        print("JSON 落盘: %s" % args.json)
    return 1 if errors else 0


def _lint(directory, root):
    """跑 sche_tasks_lint.py，返回 (rc, fail_count, fails)"""
    with tempfile.TemporaryDirectory() as td:
        jf = os.path.join(td, "lint.json")
        cmd = [sys.executable, LINT, "--root", root, "--tasks-dir", directory, "--json", jf]
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        fails = []
        if os.path.exists(jf):
            d = json.load(open(jf, encoding="utf-8"))
            ts = d["tasks"] if isinstance(d, dict) and "tasks" in d else d
            for t in ts:
                for f in t.get("findings", []):
                    if f.get("severity") == "FAIL":
                        fails.append({"id": t["id"], "code": f.get("code"), "detail": f.get("detail")})
        return p.returncode, len(fails), fails


def cmd_verify(args):
    """沙盒 A/B：把改写结果写进 temp 沙盒，跑 lint 对比 FAIL 数（不动生产）。"""
    tasks_dir = args.tasks_dir or os.path.join(args.root, "sche_tasks")
    items, errors = load_tasks(tasks_dir)
    recs = {r["id"]: r for r in analyze(items, args.root)}
    sandbox = os.path.join(args.root, "temp", "l193_sandbox_run", "sche_tasks")
    if os.path.isdir(sandbox):
        shutil.rmtree(sandbox)
    os.makedirs(sandbox, exist_ok=True)
    for it in items:
        r = recs[it["id"]]
        d = dict(it["data"])
        d["prompt"] = r["new_prompt"]
        with open(os.path.join(sandbox, it["id"] + ".json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2))
    rc_b, nf_b, fails_b = _lint(tasks_dir, args.root)
    rc_a, nf_a, fails_a = _lint(sandbox, args.root)
    print("== verify（只读；沙盒=%s）" % sandbox)
    print("  lint before: rc=%s FAIL=%d" % (rc_b, nf_b))
    for f in fails_b:
        print("     FAIL %s | %s | %s" % (f["id"], f["code"], (f["detail"] or "")[:120]))
    print("  lint after : rc=%s FAIL=%d" % (rc_a, nf_a))
    for f in fails_a:
        print("     FAIL %s | %s | %s" % (f["id"], f["code"], (f["detail"] or "")[:120]))
    changed = [r["id"] for r in recs.values() if r["changed"]]
    print("  将改动: %s" % (",".join(changed) if changed else "(无)"))
    print("  幂等: %d/%d" % (sum(1 for r in recs.values() if r.get("idempotent")), len(recs)))
    ok = (nf_a == 0 and errors == [])
    print("  判定: %s" % ("PASS（改写后 FAIL=0）" if ok else "NOT-CLEAN"))
    return 0 if ok else 1


def cmd_apply(args):
    if args.approve is None or args.approve.strip() != APPROVAL_TOKEN:
        print("拒绝：写盘需显式批准语，且须逐字等于：%s" % APPROVAL_HINT)
        print("     缺/错批准语一律零写入（fail-closed）。")
        return 2
    tasks_dir = args.tasks_dir or os.path.join(args.root, "sche_tasks")
    items, errors = load_tasks(tasks_dir)
    recs = {r["id"]: r for r in analyze(items, args.root)}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(args.root, "temp", "l193_backup", stamp)
    os.makedirs(backup, exist_ok=True)
    written, refused = [], []
    for it in items:
        r = recs[it["id"]]
        if not r["changed"]:
            continue
        if not r.get("fidelity"):
            refused.append((it["id"], "FORMAT_MISMATCH"))
            continue
        d = dict(it["data"])
        d["prompt"] = r["new_prompt"]
        new_raw = dump_like(it["raw"], d)
        # 语义自证：除 prompt 外不得有任何差异
        back = json.loads(new_raw)
        same = all(back.get(k) == it["data"].get(k) for k in set(back) | set(it["data"])
                   if k != "prompt")
        if not same:
            refused.append((it["id"], "SEMANTIC_DRIFT"))
            continue
        shutil.copy2(it["path"], os.path.join(backup, it["id"] + ".json"))
        with open(it["path"], "w", encoding="utf-8") as f:
            f.write(new_raw)
        written.append({"id": it["id"], "path": it["path"],
                        "bytes_before": len(it["raw"].encode("utf-8")),
                        "bytes_after": len(new_raw.encode("utf-8")),
                        "sha256_after": sha256_file(it["path"]),
                        "anchor_added": r["anchor_added"],
                        "replacements": r["replacements"]})
    print("== apply 批准语 OK；备份=%s" % backup)
    for w in written:
        print("  WROTE %s bytes %d→%d sha256=%s (anchor=%s, repl=%d)"
              % (w["id"], w["bytes_before"], w["bytes_after"], w["sha256_after"][:16],
                 w["anchor_added"], len(w["replacements"])))
    for tid, why in refused:
        print("  REFUSE %s | %s" % (tid, why))
    if not written:
        print("  （无文件需改动 = 已是目标态，幂等）")
    return 1 if refused else 0


def cmd_proof(args):
    tasks_dir = args.tasks_dir or os.path.join(args.root, "sche_tasks")
    files = sorted(glob.glob(os.path.join(tasks_dir, "*.json")))
    before = {p: sha256_file(p) for p in files}
    rc = cmd_plan(argparse.Namespace(root=args.root, tasks_dir=tasks_dir, json=None))
    after = {p: sha256_file(p) for p in files}
    changed = [p for p in files if before.get(p) != after.get(p)]
    print("== proof（只读自证）files=%d changed=%s" % (len(files), changed))
    return 0 if not changed else 1


# ---------------------------------------------------------------- selftest
def _mkfixture(dirpath, name, obj, raw=None):
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, name + ".json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw if raw is not None else json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return p


def cmd_selftest(args):
    root = args.root
    fails = []

    def ck(name, cond, extra=""):
        print("  %-34s %s %s" % (name, "PASS" if cond else "FAIL", extra))
        if not cond:
            fails.append(name)

    print("== selftest root=%s" % root)
    # --- 单元：rewrite
    n1, i1 = rewrite_prompt("运行 python3 ../temp/get_week_range.py --date 2026-09-01", root)
    ck("S1_anchor_added", i1["anchor_added"] and "GA_ROOT" in n1)
    ck("S1_rewritten", "../temp/" not in n1 and "temp/get_week_range.py" in n1)
    n1b, i1b = rewrite_prompt(n1, root)
    ck("S1_idempotent", n1b == n1 and not i1b["anchor_added"] and not i1b["replacements"])

    n2, i2 = rewrite_prompt("行1: cd $GA_ROOT\n读取 ../memory/x.md", root)
    ck("S2_anchored_no_dup", (not i2["anchor_added"]) and "memory/x.md" in n2)

    n3, i3 = rewrite_prompt("读取 ../otherrepo/temp/x.md", root)
    ck("S3_other_prefix_untouched", n3 == "读取 ../otherrepo/temp/x.md" and not i3["anchor_added"]
       and i3["replacements"] == [] and "../otherrepo" in i3["unsupported"])

    n4, i4 = rewrite_prompt("这是纯说明文字，无任何路径", root)
    ck("S4_no_need_no_change", n4 == "这是纯说明文字，无任何路径" and not i4["anchor_added"])

    n5, _ = rewrite_prompt("跑 ../temp/a.py 与 ../tools/b.py 与 ../memory/c.md", root)
    ck("S5_multi_prefix", ("temp/a.py" in n5) and ("tools/b.py" in n5) and ("memory/c.md" in n5)
       and "../" not in n5.split("\n", 1)[1])
    # 锚定行本身含 ${GA_ROOT} 与 cd，不得被后续处理破坏
    ck("S5_anchor_intact", anchor_line(root) in n5)

    # --- 沙盒：apply 门禁 / 保真 / 幂等
    base = os.path.join(root, "temp", "l193_selftest", time.strftime("%H%M%S") + "-%d" % os.getpid())
    fdir = os.path.join(base, "sche_tasks")
    p_good = _mkfixture(fdir, "t_good", {"schedule": "09:00", "repeat": "daily", "enabled": True,
                                         "prompt": "运行 python3 ../temp/get_week_range.py --date 2026-09-01"})
    p_compact = _mkfixture(fdir, "t_compact", {"enabled": True, "prompt": "跑 ../temp/a.py"},
                           raw='{"enabled": true, "prompt": "跑 ../temp/a.py"}')  # 非规范排版
    p_nonl = _mkfixture(fdir, "t_nonl", {"enabled": True, "prompt": "跑 ../tools/a.py 无尾换行"},
                        raw=json.dumps({"enabled": True, "prompt": "跑 ../tools/a.py 无尾换行"},
                                       ensure_ascii=False, indent=2))
    p_ok = _mkfixture(fdir, "t_keep", {"enabled": True, "prompt": "无相对路径，无需改动"})

    sha_before = {p: sha256_file(p) for p in (p_good, p_compact, p_nonl, p_ok)}

    rc = cmd_apply(argparse.Namespace(root=root, tasks_dir=fdir, approve=None))
    ck("S6_apply_no_approve_rc2", rc == 2)
    rc = cmd_apply(argparse.Namespace(root=root, tasks_dir=fdir, approve="批准 XXX:D9"))
    ck("S7_apply_wrong_token_rc2", rc == 2)
    ck("S7_zero_write_when_refused",
       all(sha256_file(p) == sha_before[p] for p in sha_before))

    rc = cmd_apply(argparse.Namespace(root=root, tasks_dir=fdir, approve=APPROVAL_TOKEN))
    ck("S8_apply_rc_expected", rc == 1, "(rc=%s，t_compact 应被拒)" % rc)
    d_good = json.load(open(p_good, encoding="utf-8"))
    ck("S8_good_anchored", ("GA_ROOT" in d_good["prompt"]) and ("temp/get_week_range.py" in d_good["prompt"])
       and ("../temp/" not in d_good["prompt"]))
    ck("S8_compact_refused_bytes_same", sha256_file(p_compact) == sha_before[p_compact])
    ck("S8_keep_untouched", sha256_file(p_ok) == sha_before[p_ok])
    raw_nonl = open(p_nonl, encoding="utf-8").read()
    ck("S8_trailing_nl_preserved", (not raw_nonl.endswith("\n")) and ("tools/a.py" in raw_nonl))
    ck("S8_other_keys_intact",
       json.load(open(p_good, encoding="utf-8")).get("schedule") == "09:00")

    rc2 = cmd_apply(argparse.Namespace(root=root, tasks_dir=fdir, approve=APPROVAL_TOKEN))
    ck("S9_second_apply_noop", rc2 in (0, 1) and sha256_file(p_good) == sha256_file(p_good))
    items, _ = load_tasks(fdir)
    chg = [r["id"] for r in analyze(items, root) if r["changed"]]
    ck("S9_idempotent_dir", chg == ["t_compact"], "(待改=%s)" % chg)

    # --- 端到端：lint 前后 FAIL
    rc, nf_b, _ = _lint(fdir, root)
    sb = os.path.join(base, "after", "sche_tasks")
    os.makedirs(sb, exist_ok=True)
    for it in items:
        r = [x for x in analyze([it], root)][0]
        d = dict(it["data"])
        d["prompt"] = r["new_prompt"]
        with open(os.path.join(sb, it["id"] + ".json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2))
    rc2, nf_a, fails_a = _lint(sb, root)
    ck("S10_lint_before_has_fail", nf_b > 0, "(FAIL=%d)" % nf_b)
    ck("S10_lint_after_zero", nf_a == 0, "(FAIL=%d %s)" % (nf_a, fails_a[:2]))

    # --- 负控：保真检查必须能识别非规范排版
    ck("NC1_fidelity_detects_compact",
       not check_fidelity('{"enabled": true}', json.loads('{"enabled": true}')))
    ck("NC2_proof_readonly", cmd_proof(argparse.Namespace(root=root, tasks_dir=fdir)) == 0)

    print("\nselftest: %s" % ("ALL PASSED" if not fails else "FAILED"))
    if fails:
        print("FAILED: %s" % ", ".join(fails))
        return 1
    print("selftest ALL PASSED")
    return 0


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="sche_tasks prompt cwd 锚定修复器")
    ap.add_argument("--root", default=ROOT, help="仓库根（默认=本文件上级目录）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("plan")
    p.add_argument("--tasks-dir", default=None)
    p.add_argument("--json", default=None)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("verify")
    p.add_argument("--tasks-dir", default=None)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("apply")
    p.add_argument("--tasks-dir", default=None)
    p.add_argument("--approve", default=None)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("proof")
    p.add_argument("--tasks-dir", default=None)
    p.set_defaults(func=cmd_proof)

    p = sub.add_parser("selftest")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
