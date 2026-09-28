#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reflect_wiring_check.py — reflect 反射器脚本 × 调度面 引用覆盖率核查（只读）

TODO L196（第22批·评审提名替换项）
背景：`agentmain.py --reflect <单个脚本>` 一次只驱动一个脚本，reflect_scripts/ 与 reflect/
      都没有「目录自动扫描」（08_kc_snapshot.py docstring 实测结论）⇒ 脚本「在盘上」≠「被调度」。
      R479 靠人工发现 09_task_freshness.py 无任何 launchd 引用（吞吐台账从未被调度）；本器把该
      判据机械化 + 全量化，并给「设计上不接线」的脚本一个必须带证据的声明位。

判据面（强度降序，命中即定级）：
  W1 launchd   : plist 的 ProgramArguments 里出现**相邻 token 对** `--reflect <path>`
                 （严格接口形状：注释、其它 flag、字符串里的路径都不算接线）
  W2 sche_tasks: sche_tasks/**.json（含 disabled/）里**启用中**的条目文本出现脚本相对路径
  W3 indirect  : 已被 W1/W2 接线的脚本源码里出现另一脚本路径（调度器委派）
  （旁证，不计接线）code/docs 提及、仅出现在已停用调度条目里的提及

每个「场景脚本」（源码含 `def check(`）归入 W1/W2/W3 之一，否则=机械未接线；未接线项再由注册表
tools/reflect_wiring_registry.json 给出处置声明（example_template / pending_approval /
manual_on_demand / retired）。声明 fail-closed：id 必须在盘、status 必须合法、每条证据的
文件必须存在且**逐字命中 token**（否则 rc=2，判注册表无效而非"已声明"）。
未接线且未声明 ⇒ ORPHAN（rc=1）。辅助模块（无 check()）单列为 helper，只报消费者面。

子命令（位置参数，非 --flag）：
  scan       全量报表 + 汇总（默认）
  gaps       只打缺口（ORPHAN / 坏接线 / 已接线但未加载）与注册表问题
  crosscheck 用**独立读取器**（plutil -p / 原文正则）复算接线集并与解析结果逐字对账
  json <path> 把完整报表写成 JSON（只写指定路径）
  proof      只打语料指纹（只读自证：跑前后 sha256 一致）
  selftest   合成夹具 + 负控自测

rc: 0=无缺口  1=有缺口（ORPHAN / BROKEN_WIRING / NOT_LOADED）  2=注册表或夹具无效
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GA_ROOT = Path(__file__).resolve().parent.parent

REFLECT_DIRS = ["reflect_scripts", "reflect"]
SCENARIO_MARK = "def check("
SCHE_GLOBS = ["sche_tasks/*.json", "sche_tasks/disabled/*.json"]
SCHE_PATH_RE = re.compile(r"(?:reflect_scripts|reflect)/[A-Za-z0-9_]+\.[A-Za-z0-9]+")
SCHE_ENABLED_RE = re.compile(r'"enabled"\s*:\s*(true|false)')

DISPOSITIONS = ("example_template", "pending_approval", "manual_on_demand", "retired")
REGISTRY_REL = "tools/reflect_wiring_registry.json"

REF_SCAN_SUBDIRS = ["tools", "birdie_monitor", "memory", "docs"]
REF_ROOT_FILES = ["README.md", "USAGE_GUIDE.md", "AGENTS.md"]
REF_MAX_BYTES = 4 * 1024 * 1024
REF_EXCLUDE_NAMES = {".git", "node_modules", ".venv", "venv", "temp", "logs", "__pycache__", "dist", ".ga"}


def default_plist_dirs():
    return [
        Path(os.path.expanduser("~/Library/LaunchAgents")),
        Path("/Library/LaunchAgents"),
        Path("/Library/LaunchDaemons"),
    ]


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 16), b""):
                h.update(blk)
    except OSError:
        return "ERR"
    return h.hexdigest()


# ---------------------------------------------------------------- discovery


def discover_scripts(ga: Path, dirs=None) -> list:
    dirs = dirs or REFLECT_DIRS
    out = []
    for d in dirs:
        base = ga / d
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.py")):
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                txt = ""
            out.append(
                {
                    "id": str(p.relative_to(ga)),
                    "path": p,
                    "has_check": SCENARIO_MARK in txt,
                    "lines": txt.count("\n") + (1 if txt and not txt.endswith("\n") else 0),
                    "bytes": p.stat().st_size,
                }
            )
    return sorted(out, key=lambda s: s["id"])


def read_plist_dirs(dirs) -> list:
    out = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            out.append({"path": str(d), "kind": "DIR_MISSING", "data": None})
            continue
        for p in sorted(d.glob("*.plist")):
            rec = {"path": str(p), "kind": "PLIST", "data": None, "parse_error": None}
            try:
                with open(p, "rb") as f:
                    rec["data"] = plistlib.load(f)
            except Exception as e:  # noqa: BLE001 - 记录而非抛出（真实树上存在非 plist 文件）
                rec["kind"] = "PLIST_UNREADABLE"
                rec["parse_error"] = "%s: %s" % (type(e).__name__, e)
            out.append(rec)
    return out


def plist_wirings(plists: list, ga: Path) -> list:
    """从 ProgramArguments 抽 `--reflect <path>` 相邻 token 对（严格接口形状）。"""
    out = []
    for rec in plists:
        data = rec.get("data")
        if not isinstance(data, dict):
            continue
        args = data.get("ProgramArguments") or []
        if not isinstance(args, list):
            continue
        label = data.get("Label") or Path(rec["path"]).stem
        for i, tok in enumerate(args):
            if tok != "--reflect" or i + 1 >= len(args):
                continue
            raw = str(args[i + 1])
            tgt = Path(raw)
            abs_tgt = tgt if tgt.is_absolute() else (ga / tgt)
            try:
                rel = str(abs_tgt.resolve().relative_to(ga.resolve()))
            except ValueError:
                rel = None
            out.append(
                {
                    "plist": rec["path"],
                    "label": label,
                    "script": rel,
                    "target_raw": raw,
                    "target_abs": str(abs_tgt),
                    "target_exists": abs_tgt.exists(),
                    "disabled": bool(data.get("Disabled", False)),
                }
            )
    return out


def plutil_text(path: str) -> str:
    """独立读取器（与 plistlib 不同实现）——用于 crosscheck。"""
    try:
        r = subprocess.run(["/usr/bin/plutil", "-p", path], capture_output=True, text=True, timeout=20)
        return r.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def default_loader() -> dict:
    """launchctl list → {label: (pid, status)}（只读）。"""
    out = {}
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return out
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3:
            pid, status, label = parts[0], parts[1], parts[2]
            out[label] = (pid, status)
    return out


def read_sche_files(ga: Path, globs=None) -> list:
    globs = globs or SCHE_GLOBS
    out = []
    for g in globs:
        for p in sorted(glob.glob(str(ga / g))):
            pp = Path(p)
            try:
                txt = pp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            enabled = False
            try:
                d = json.loads(txt)
                enabled = bool(d.get("enabled", False))
            except Exception:  # noqa: BLE001
                enabled = False
            if "/disabled/" in str(pp):
                enabled = False
            refs = sorted(set(SCHE_PATH_RE.findall(txt)))
            out.append(
                {
                    "rel": str(pp.relative_to(ga)),
                    "enabled_declared": enabled,
                    "in_disabled_dir": "/disabled/" in str(pp),
                    "refs": refs,
                    "bytes": len(txt.encode("utf-8", "replace")),
                }
            )
    return out


def build_ref_corpus(ga: Path) -> dict:
    corpus = {}
    files = []
    for name in REF_ROOT_FILES:
        p = ga / name
        if p.is_file():
            files.append(p)
    for sub in REF_SCAN_SUBDIRS:
        base = ga / sub
        if not base.is_dir():
            continue
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in REF_EXCLUDE_NAMES]
            for n in names:
                if n.startswith("."):
                    continue
                files.append(Path(root) / n)
    for p in files:
        try:
            if p.stat().st_size > REF_MAX_BYTES:
                continue
            corpus[str(p.relative_to(ga))] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return corpus


def find_refs(corpus: dict, ids) -> dict:
    refs = {i: [] for i in ids}
    for rel, txt in corpus.items():
        for i in ids:
            if i in txt:
                kind = "code" if rel.endswith(".py") else "doc"
                refs[i].append({"file": rel, "kind": kind})
    return refs


# ---------------------------------------------------------------- registry


def load_registry(ga: Path, rel=REGISTRY_REL) -> tuple:
    p = ga / rel
    if not p.is_file():
        return {}, [{"code": "MISSING_REGISTRY", "detail": str(rel)}]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {}, [{"code": "REGISTRY_JSON_ERROR", "detail": "%s: %s" % (type(e).__name__, e)}]
    entries = d.get("entries")
    if not isinstance(entries, list):
        return {}, [{"code": "REGISTRY_SHAPE", "detail": "entries 必须是 list"}]
    out = {}
    errs = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("id"):
            errs.append({"code": "ENTRY_SHAPE", "detail": repr(e)[:120]})
            continue
        eid = e["id"]
        if eid in out:
            errs.append({"code": "DUPLICATE_ID", "detail": eid})
            continue
        out[eid] = e
    return out, errs


def validate_registry(ga: Path, entries: dict, script_ids: set) -> tuple:
    errs = []
    ok = {}
    for eid, e in entries.items():
        if eid not in script_ids:
            errs.append({"code": "STALE_ENTRY", "detail": "%s 不在盘上（角色/路径已变？）" % eid})
            continue
        st = e.get("status")
        if st not in DISPOSITIONS:
            errs.append({"code": "UNKNOWN_STATUS", "detail": "%s status=%r" % (eid, st)})
            continue
        reason = (e.get("reason") or "").strip()
        if len(reason) < 10:
            errs.append({"code": "EMPTY_REASON", "detail": eid})
            continue
        evs = e.get("evidence")
        if not isinstance(evs, list) or not evs:
            errs.append({"code": "NO_EVIDENCE", "detail": eid})
            continue
        bad = []
        for ev in evs:
            if not isinstance(ev, dict) or not ev.get("path") or not ev.get("token"):
                bad.append("evidence 形状错误")
                continue
            ep = ga / ev["path"]
            if not ep.is_file():
                bad.append("证据文件不存在: %s" % ev["path"])
                continue
            try:
                txt = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                bad.append("证据文件不可读: %s" % ev["path"])
                continue
            if ev["token"] not in txt:
                bad.append("证据 token 未逐字命中: %s :: %s" % (ev["path"], ev["token"]))
        if bad:
            errs.append({"code": "BAD_EVIDENCE", "detail": "%s | %s" % (eid, "; ".join(bad))})
            continue
        ok[eid] = e
    return ok, errs


# ---------------------------------------------------------------- analyze


def fingerprint(ga: Path, scripts, plists, sche, registry_rel) -> dict:
    fp = {}
    for s in scripts:
        fp[s["id"]] = sha256_file(s["path"])
    for rec in plists:
        if rec.get("kind") == "PLIST":
            fp[rec["path"]] = sha256_file(Path(rec["path"]))
    for f in sche:
        p = ga / f["rel"]
        if p.is_file():
            fp[f["rel"]] = sha256_file(p)
    rp = ga / registry_rel
    if rp.is_file():
        fp[registry_rel] = sha256_file(rp)
    return fp


def analyze(ga: Path, *, plist_dirs=None, registry_rel=REGISTRY_REL, loader=None, reflect_dirs=None,
            sche_globs=None, with_corpus=True) -> dict:
    ga = Path(ga)
    scripts = discover_scripts(ga, reflect_dirs)
    scenarios = [s for s in scripts if s["has_check"]]
    helpers = [s for s in scripts if not s["has_check"]]
    sc_ids = {s["id"] for s in scenarios}

    plists = read_plist_dirs(plist_dirs or default_plist_dirs())
    pw = plist_wirings(plists, ga)
    sche = read_sche_files(ga, sche_globs)
    sw = []
    for f in sche:
        if not f["enabled_declared"]:
            continue
        for r in f["refs"]:
            sw.append({"script": r, "sche": f["rel"], "enabled": True})

    fp_before = fingerprint(ga, scripts, plists, sche, registry_rel)

    loaded = (loader or default_loader)()
    launchd_map = {}
    for w in pw:
        launchd_map.setdefault(w["script"], []).append(w)
    sche_map = {}
    for w in sw:
        sche_map.setdefault(w["script"], []).append(w)

    # W3 indirect
    wired_ids = {w["script"] for w in pw if w["script"]} | set(sche_map)
    indirect_map = {}
    for w in sorted(wired_ids):
        p = ga / w
        if not p.is_file():
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for sid in sorted(sc_ids):
            if sid != w and sid in src:
                indirect_map.setdefault(sid, []).append(w)

    entries, reg_errs = load_registry(ga, registry_rel)
    entries_ok, reg_errs2 = validate_registry(ga, entries, sc_ids)
    reg_errs = reg_errs + reg_errs2

    corpus = build_ref_corpus(ga) if with_corpus else {}
    refs = find_refs(corpus, sorted(sc_ids)) if corpus else {i: [] for i in sc_ids}

    # 声明面（未接线但被注册表声明）
    rows = []
    gaps = []
    ref_only = []
    for w in pw:
        if not w["target_exists"]:
            gaps.append({"kind": "BROKEN_WIRING", "script": w["script"] or w["target_raw"],
                         "detail": "plist=%s target=%s" % (Path(w["plist"]).name, w["target_raw"])})
        elif w["script"] is None:
            gaps.append({"kind": "WIRING_OUTSIDE_REPO", "script": w["target_raw"],
                         "detail": "plist=%s 指向仓库外" % Path(w["plist"]).name})
    for s in scenarios:
        sid = s["id"]
        if sid in launchd_map:
            ws = launchd_map[sid]
            broken = [w for w in ws if not w["target_exists"]]
            not_loaded = []
            for w in ws:
                lab = w["label"]
                st = loaded.get(lab)
                if w["disabled"] or st is None:
                    not_loaded.append({"label": lab, "pid": None if st is None else st[0]})
            if broken:
                status = "BROKEN_WIRING"
            elif not_loaded:
                status = "WIRED_LAUNCHD_NOT_LOADED"
                gaps.append({"kind": "NOT_LOADED", "script": sid,
                             "detail": "labels=%s" % ",".join(n["label"] for n in not_loaded)})
            else:
                status = "WIRED_LAUNCHD"
            note = "labels=%s loaded=%d/%d" % (
                ",".join(w["label"] for w in ws), len(ws) - len(not_loaded), len(ws))
            rows.append({"id": sid, "status": status, "surface": "W1 launchd", "note": note,
                         "evidence": [w["plist"] for w in ws]})
        elif sid in sche_map:
            ws = sche_map[sid]
            status = "WIRED_SCHE"
            rows.append({"id": sid, "status": status, "surface": "W2 sche_tasks",
                         "note": "files=%s" % ",".join(w["sche"] for w in ws),
                         "evidence": [w["sche"] for w in ws]})
        elif sid in indirect_map:
            rows.append({"id": sid, "status": "WIRED_INDIRECT", "surface": "W3 indirect",
                         "note": "via=%s" % ",".join(indirect_map[sid]),
                         "evidence": indirect_map[sid]})
        else:
            d = entries_ok.get(sid)
            if d:
                rows.append({"id": sid, "status": "DECLARED_" + d["status"], "surface": "registry",
                             "note": (d.get("reason") or "")[:70],
                             "evidence": [e["path"] for e in d.get("evidence", [])],
                             "evidence_strength": d.get("evidence_strength", "strong")})
            else:
                rows.append({"id": sid, "status": "ORPHAN", "surface": "-",
                             "note": "未接线且未声明", "evidence": []})
                gaps.append({"kind": "ORPHAN", "script": sid, "detail": "无任何调度面引用且无注册表声明"})

        # 旁证：code/docs 提及（不计接线）
        rl = refs.get(sid, [])
        if rl:
            ref_only.append({"script": sid, "files": rl[:6], "count": len(rl)})

    # 仅出现在已停用调度条目里的引用（提示级）
    disabled_refs = []
    for f in sche:
        if f["enabled_declared"]:
            continue
        for r in f["refs"]:
            disabled_refs.append({"script": r, "sche": f["rel"]})

    helpers_out = []
    for h in helpers:
        rl = refs.get(h["id"], [])
        helpers_out.append({"id": h["id"], "consumers": [x["file"] for x in rl][:8], "count": len(rl)})

    fp_after = fingerprint(ga, scripts, plists, sche, registry_rel)
    changed = sorted(k for k in set(fp_before) | set(fp_after) if fp_before.get(k) != fp_after.get(k))

    counts = {
        "scripts": len(scripts),
        "scenarios": len(scenarios),
        "helpers": len(helpers),
        "wired": sum(1 for r in rows if r["status"].startswith("WIRED")),
        "declared": sum(1 for r in rows if r["status"].startswith("DECLARED_")),
        "orphan": sum(1 for r in rows if r["status"] == "ORPHAN"),
        "gaps": len(gaps),
        "registry_errors": len(reg_errs),
        "coverage_pct": round(100.0 * sum(1 for r in rows if r["status"].startswith("WIRED")) / len(scenarios), 1)
        if scenarios else 0.0,
    }

    rc = 0
    if reg_errs:
        rc = 2
    elif gaps:
        rc = 1

    return {
        "schema": "ga.reflect_wiring/v1",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ga_root": str(ga),
        "plist_dirs": [str(d) for d in (plist_dirs or default_plist_dirs())],
        "scripts": rows,
        "helpers": helpers_out,
        "launchd": [{"plist": w["plist"], "label": w["label"], "script": w["script"],
                     "target_raw": w["target_raw"], "exists": w["target_exists"],
                     "disabled": w["disabled"],
                     "loaded": w["label"] in loaded} for w in pw],
        "plist_unreadable": [{"path": r["path"], "error": r["parse_error"]}
                             for r in plists if r["kind"] == "PLIST_UNREADABLE"],
        "sche_tasks": sche,
        "sche_disabled_refs": disabled_refs,
        "unwired": sorted(r["id"] for r in rows if not r["status"].startswith("WIRED")),
        "declarations": [{"id": k, "status": v["status"], "reason": v["reason"],
                          "evidence": v["evidence"]} for k, v in sorted(entries_ok.items())],
        "gaps": gaps,
        "registry_errors": reg_errs,
        "ref_only": ref_only,
        "counts": counts,
        "proof": {"files": len(fp_after), "changed": changed},
        "rc": rc,
    }


def render_text(rep: dict) -> str:
    L = []
    L.append("== reflect 接线覆盖率核查（只读） %s" % rep["generated"])
    L.append("GA=%s" % rep["ga_root"])
    L.append("")
    L.append("-- 场景脚本（含 def check()）: %d ; 辅助模块: %d" % (rep["counts"]["scenarios"], rep["counts"]["helpers"]))
    for r in rep["scripts"]:
        L.append("  %-34s %-26s %s" % (r["id"], r["status"], r["note"]))
        if r.get("evidence"):
            L.append("      证据: %s" % ", ".join(r["evidence"])[:150])
    L.append("")
    L.append("-- launchd 接线（`--reflect <path>` 相邻 token 对）: %d" % len(rep["launchd"]))
    for w in rep["launchd"]:
        L.append("  %-46s %-42s exists=%s loaded=%s" % (
            Path(w["plist"]).name, w["label"], w["exists"], w["loaded"]))
    if rep["plist_unreadable"]:
        L.append("  （不可读 plist: %s）" % ", ".join(Path(x["path"]).name for x in rep["plist_unreadable"]))
    L.append("")
    L.append("-- sche_tasks 条目: %d（启用中 %d）" % (
        len(rep["sche_tasks"]), sum(1 for f in rep["sche_tasks"] if f["enabled_declared"])))
    for f in rep["sche_tasks"]:
        if f["refs"]:
            L.append("  %-52s enabled=%-5s refs=%s" % (f["rel"], f["enabled_declared"], ",".join(f["refs"])))
    if rep["sche_disabled_refs"]:
        L.append("  （仅出现在已停用条目里的引用: %s）" % ", ".join(
            "%s@%s" % (d["script"], d["sche"]) for d in rep["sche_disabled_refs"]))
    L.append("")
    L.append("-- 机械未接线清单（%d）" % len(rep["unwired"]))
    for u in rep["unwired"]:
        st = [r["status"] for r in rep["scripts"] if r["id"] == u][0]
        L.append("  %-34s %s" % (u, st))
    L.append("")
    L.append("-- 缺口（%d）" % len(rep["gaps"]))
    for g in rep["gaps"]:
        L.append("  %-18s %-34s %s" % (g["kind"], g["script"], g["detail"]))
    if rep["registry_errors"]:
        L.append("-- 注册表问题（%d）" % len(rep["registry_errors"]))
        for e in rep["registry_errors"]:
            L.append("  %-18s %s" % (e["code"], e["detail"]))
    L.append("")
    L.append("-- 汇总: wired=%d declared=%d orphan=%d 覆盖率=%.1f%% 缺口=%d 注册表问题=%d" % (
        rep["counts"]["wired"], rep["counts"]["declared"], rep["counts"]["orphan"],
        rep["counts"]["coverage_pct"], rep["counts"]["gaps"], rep["counts"]["registry_errors"]))
    L.append("-- 只读自证: files=%d changed=%s" % (rep["proof"]["files"], rep["proof"]["changed"] or "[]"))
    L.append("-- rc=%d" % rep["rc"])
    return "\n".join(L)


def crosscheck(ga: Path, rep: dict, plist_dirs=None) -> tuple:
    """独立读取器复算（等价 `grep -lF` 的文本引用语义）：

      A = 结构化接线集（W1 launchd ∪ W2 sche；W3 indirect 不计——它来自源码文本而非调度面）
      B = 文本引用集（plist 的 `plutil -p` 文本 ∪ 启用中的 sche 原文）
      A-B 必须为空（结构化认为接线而文本找不到 ⇒ 解析缺陷，fail-closed）
      B-A = 文本提及但非接线（注释键 / flag 拼错 / 停用条目）⇒ 提示级，连同出处一并列出
    """
    ids = {r["id"] for r in rep["scripts"]}
    A = {r["id"] for r in rep["scripts"]
         if r["status"] in ("WIRED_LAUNCHD", "WIRED_LAUNCHD_NOT_LOADED", "WIRED_SCHE")}
    B = set()
    where = {}
    for d in (plist_dirs or default_plist_dirs()):
        d = Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.plist")):
            txt = plutil_text(str(p))
            if not txt:
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
            for i in ids:
                if i in txt:
                    B.add(i)
                    where.setdefault(i, []).append(Path(p).name)
    for g in SCHE_GLOBS:
        for p in sorted(glob.glob(str(ga / g))):
            txt = Path(p).read_text(encoding="utf-8", errors="replace")
            m = SCHE_ENABLED_RE.search(txt)
            enabled = (m.group(1) == "true") if m else False
            if "/disabled/" in p:
                enabled = False
            if not enabled:
                continue
            for i in ids:
                if i in txt:
                    B.add(i)
                    where.setdefault(i, []).append(Path(p).name)
    only_b = sorted(B - A)
    return sorted(A), sorted(B), sorted(A - B), only_b, {k: where.get(k, []) for k in only_b}


# ---------------------------------------------------------------- selftest

FIX_DOC = "本模板设计为手动启动，示例：python agentmain.py --reflect 04_d.py\n"
FIX_EVIDENCE = "手动启动"


def _mk(p: Path, txt: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt, encoding="utf-8")


def build_fixture(root: Path, *, declare_fifth=True, missing_target=False, extra_plists=True):
    ga = root / "ga"
    rs = ga / "reflect_scripts"
    _mk(rs / "01_a.py", "INTERVAL=60\ndef check():\n    return None\n")
    _mk(rs / "02_b.py", "INTERVAL=60\ndef check():\n    return None\n")
    _mk(rs / "03_c.py", "INTERVAL=60\ndef check():\n    return None\n")
    _mk(rs / "04_d.py", "INTERVAL=60\ndef check():\n    return None\n")
    _mk(rs / "05_e.py", "INTERVAL=60\ndef check():\n    return None\n")
    _mk(rs / "06_f.py", "INTERVAL=60\ndef check():\n    return None\n")
    _mk(rs / "helpers.py", "def notify():\n    return 1\n")
    _mk(ga / "reflect" / "dispatcher.py", "INTERVAL=60\nTARGET='reflect_scripts/03_c.py'\ndef check():\n    return None\n")
    _mk(ga / "docs" / "x.md", FIX_DOC)
    _mk(ga / "sche_tasks" / "t1.json",
        json.dumps({"enabled": True, "prompt": "跑 reflect_scripts/02_b.py 干点事"}, ensure_ascii=False))
    _mk(ga / "sche_tasks" / "disabled" / "old.json",
        json.dumps({"enabled": False, "prompt": "旧任务 reflect_scripts/06_f.py"}, ensure_ascii=False))
    plists = root / "plists"
    plists.mkdir(parents=True, exist_ok=True)
    tgt = str(rs / ("99_missing.py" if missing_target else "01_a.py"))
    for name, label, path in (
        ("good.plist", "com.test.good", tgt),
        ("dispatch.plist", "com.test.dispatch", str(ga / "reflect" / "dispatcher.py")),
    ):
        with open(plists / name, "wb") as f:
            plistlib.dump({"Label": label, "RunAtLoad": True, "KeepAlive": True,
                           "ProgramArguments": ["/usr/bin/python3", "agentmain.py", "--reflect", path]}, f)
    if extra_plists:
        # NC1 负控夹具：路径只出现在注释键里（不是 ProgramArguments 的相邻 token 对）
        with open(plists / "comment_only.plist", "wb") as f:
            plistlib.dump({"Label": "com.test.comment", "_comment": "see reflect_scripts/05_e.py",
                           "ProgramArguments": ["/usr/bin/python3", "agentmain.py", "--reflect"]}, f)
        # NC2 负控夹具：flag 拼错
        with open(plists / "typo.plist", "wb") as f:
            plistlib.dump({"Label": "com.test.typo",
                           "ProgramArguments": ["/usr/bin/python3", "agentmain.py", "--reflct",
                                                str(ga / "reflect_scripts/05_e.py")]}, f)
    entries = [
        {"id": "reflect_scripts/04_d.py", "status": "example_template", "reason": "官方示例模板，手动启动",
         "evidence": [{"path": "docs/x.md", "token": FIX_EVIDENCE}], "evidence_strength": "strong"},
    ]
    if declare_fifth:
        entries.append({"id": "reflect_scripts/05_e.py", "status": "manual_on_demand",
                        "reason": "设计为手动启动 demo 脚本", "evidence": [{"path": "docs/x.md", "token": FIX_EVIDENCE}]})
        entries.append({"id": "reflect_scripts/06_f.py", "status": "example_template",
                        "reason": "示例模板，仅被已停用调度条目提及",
                        "evidence": [{"path": "docs/x.md", "token": FIX_EVIDENCE}]})
    reg = {"schema": "ga.reflect_wiring/v1", "entries": entries}
    _mk(ga / "tools" / "reflect_wiring_registry.json", json.dumps(reg, ensure_ascii=False, indent=1))
    return ga, plists


FIX_LOADER = lambda: {"com.test.good": ("111", "0"), "com.test.dispatch": ("222", "0")}  # noqa: E731


def _an(ga, plists, **kw):
    return analyze(ga, plist_dirs=[plists], loader=FIX_LOADER, **kw)


def selftest() -> int:
    checks = []

    def chk(name, cond, extra=""):
        checks.append((name, bool(cond), extra))

    tmp = Path(tempfile.mkdtemp(prefix="reflect_wiring_st_"))
    try:
        # 正控：05/06 已声明；06 只在 disabled 条目里被引用（不算接线）；helpers 无 check
        ga, plists = build_fixture(tmp / "pos", declare_fifth=True, extra_plists=False)
        rep = _an(ga, plists)
        st = {r["id"]: r["status"] for r in rep["scripts"]}
        chk("POS rc=0（全部有接线或已声明）", rep["rc"] == 0, "rc=%s gaps=%s" % (rep["rc"], rep["gaps"]))
        chk("POS 04_d 已声明=DECLARED_example_template", st["reflect_scripts/04_d.py"] == "DECLARED_example_template", st)
        chk("POS helpers.py 不列入场景", all(r["id"] != "reflect_scripts/helpers.py" for r in rep["scripts"]))
        chk("POS helpers 单列且带消费者", any(h["id"] == "reflect_scripts/helpers.py" for h in rep["helpers"]))
        chk("POS 真接线恰两条被识别",
            sorted(w["label"] for w in rep["launchd"]) == ["com.test.dispatch", "com.test.good"], rep["launchd"])

        # 负控夹具（注释键里出现路径 / flag 拼错）：单独一套，避免污染正控的 crosscheck 口径
        ga_n, plists_n = build_fixture(tmp / "neg", declare_fifth=True, extra_plists=True)
        rep_n = _an(ga_n, plists_n)
        st_n = {r["id"]: r["status"] for r in rep_n["scripts"]}
        chk("NC1 注释键里的路径不算接线",
            all(w["label"] != "com.test.comment" for w in rep_n["launchd"])
            and st_n["reflect_scripts/05_e.py"].startswith("DECLARED"), rep_n["launchd"])
        # NC2 flag 拼错不算接线
        chk("NC2 flag 拼错不算接线", all(w["label"] != "com.test.typo" for w in rep_n["launchd"]), rep_n["launchd"])
        # NC3 --reflect 指向不存在的脚本 ⇒ BROKEN_WIRING rc=1
        ga_b, plists_b = build_fixture(tmp / "miss", declare_fifth=True, missing_target=True)
        rep4 = _an(ga_b, plists_b)
        chk("NC3 目标缺失 ⇒ BROKEN_WIRING 且 rc=1",
            rep4["rc"] == 1 and any(g["kind"] == "BROKEN_WIRING" for g in rep4["gaps"]), rep4["gaps"])
        # NC4/NC5 撤掉声明 ⇒ 该脚本被判 ORPHAN 且 rc=1
        ga_c, plists_c = build_fixture(tmp / "orph", declare_fifth=False)
        rep5 = _an(ga_c, plists_c)
        st5 = {r["id"]: r["status"] for r in rep5["scripts"]}
        chk("NC5 未接线未声明 ⇒ ORPHAN rc=1",
            st5.get("reflect_scripts/05_e.py") == "ORPHAN" and rep5["rc"] == 1, st5)
        # NC6 证据文件不存在 ⇒ rc=2
        ga_d, plists_d = build_fixture(tmp / "ev1", declare_fifth=True)
        regp = ga_d / "tools" / "reflect_wiring_registry.json"
        reg = json.loads(regp.read_text(encoding="utf-8"))
        reg["entries"][0]["evidence"] = [{"path": "docs/nope.md", "token": FIX_EVIDENCE}]
        regp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        rep6 = _an(ga_d, plists_d)
        chk("NC6 证据文件不存在 ⇒ rc=2 BAD_EVIDENCE",
            rep6["rc"] == 2 and any(e["code"] == "BAD_EVIDENCE" for e in rep6["registry_errors"]),
            rep6["registry_errors"])
        # NC7 证据 token 未命中 ⇒ rc=2
        ga_e, plists_e = build_fixture(tmp / "ev2", declare_fifth=True)
        regp = ga_e / "tools" / "reflect_wiring_registry.json"
        reg = json.loads(regp.read_text(encoding="utf-8"))
        reg["entries"][0]["evidence"] = [{"path": "docs/x.md", "token": "这句话不存在于文档"}]
        regp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        rep7 = _an(ga_e, plists_e)
        chk("NC7 证据 token 未命中 ⇒ rc=2", rep7["rc"] == 2 and any(
            "token 未逐字命中" in e["detail"] for e in rep7["registry_errors"]), rep7["registry_errors"])
        # NC8 未知 status ⇒ rc=2
        ga_f, plists_f = build_fixture(tmp / "st", declare_fifth=True)
        regp = ga_f / "tools" / "reflect_wiring_registry.json"
        reg = json.loads(regp.read_text(encoding="utf-8"))
        reg["entries"][0]["status"] = "whatever"
        regp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        rep8 = _an(ga_f, plists_f)
        chk("NC8 未知 status ⇒ rc=2 UNKNOWN_STATUS",
            rep8["rc"] == 2 and any(e["code"] == "UNKNOWN_STATUS" for e in rep8["registry_errors"]), rep8["registry_errors"])
        # NC9 陈旧条目（脚本不在盘）⇒ rc=2
        ga_g, plists_g = build_fixture(tmp / "stale", declare_fifth=True)
        regp = ga_g / "tools" / "reflect_wiring_registry.json"
        reg = json.loads(regp.read_text(encoding="utf-8"))
        reg["entries"].append({"id": "reflect_scripts/99_zzz.py", "status": "example_template",
                               "reason": "早已删除的脚本", "evidence": [{"path": "docs/x.md", "token": FIX_EVIDENCE}]})
        regp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        rep9 = _an(ga_g, plists_g)
        chk("NC9 陈旧条目 ⇒ rc=2 STALE_ENTRY",
            rep9["rc"] == 2 and any(e["code"] == "STALE_ENTRY" for e in rep9["registry_errors"]), rep9["registry_errors"])
        # NC10 重复 id ⇒ rc=2
        ga_h, plists_h = build_fixture(tmp / "dup", declare_fifth=True)
        regp = ga_h / "tools" / "reflect_wiring_registry.json"
        reg = json.loads(regp.read_text(encoding="utf-8"))
        reg["entries"].append(dict(reg["entries"][0]))
        regp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        rep10 = _an(ga_h, plists_h)
        chk("NC10 重复 id ⇒ rc=2 DUPLICATE_ID",
            rep10["rc"] == 2 and any(e["code"] == "DUPLICATE_ID" for e in rep10["registry_errors"]), rep10["registry_errors"])
        # NC11 sche 引用被识别为接线
        chk("NC11 sche 引用 ⇒ WIRED_SCHE", st["reflect_scripts/02_b.py"] == "WIRED_SCHE", st)
        # NC12 helper（无 check）不判 ORPHAN
        chk("NC12 helper 不进 gaps", all(g["script"] != "reflect_scripts/helpers.py" for g in rep["gaps"]))
        # NC13 仅文档提及 ≠ 接线（05_e 在撤声明版里是 ORPHAN，但 04_d 有文档提及）
        chk("NC13 文档提及不计接线（04_d 未接线但有 doc 引用）",
            any(x["script"] == "reflect_scripts/04_d.py" for x in rep5["ref_only"]), rep5["ref_only"])
        # NC14 已接线但未加载 ⇒ NOT_LOADED
        ga_i, plists_i = build_fixture(tmp / "notl", declare_fifth=True)
        rep14 = analyze(ga_i, plist_dirs=[plists_i], loader=lambda: {}, reflect_dirs=["reflect_scripts", "reflect"])
        chk("NC14 未加载 ⇒ WIRED_LAUNCHD_NOT_LOADED 且 rc=1",
            rep14["rc"] == 1 and any(r["status"] == "WIRED_LAUNCHD_NOT_LOADED" for r in rep14["scripts"]), rep14["gaps"])
        # NC15 已停用调度条目里的引用不算接线
        st15 = {r["id"]: r["status"] for r in rep["scripts"]}
        chk("NC15 disabled sche 引用不算接线（06_f 未接线）", not st15["reflect_scripts/06_f.py"].startswith("WIRED"), st15)
        # NC16 间接接线（dispatcher 已接线 ⇒ 03_c 记 WIRED_INDIRECT）
        chk("NC16 间接接线识别", st["reflect_scripts/03_c.py"] == "WIRED_INDIRECT", st)
        # NC17 只读自证：指纹不变
        chk("NC17 proof changed 为空", rep["proof"]["changed"] == [], rep["proof"])
        # NC18 crosscheck：正控=结构化接线与文本复算逐字一致
        A, B, d1, d2, where2 = crosscheck(ga, rep, plist_dirs=[plists])
        chk("NC18 crosscheck 正控逐字一致", not d1 and not d2, "only_A=%s only_B=%s" % (d1, d2))
        # NC19 crosscheck：注释键/拼错 flag 只算「文本提及」，且 only_A 必须为空（fail-closed）
        A3, B3, d3, d4, where3 = crosscheck(ga_n, rep_n, plist_dirs=[plists_n])
        chk("NC19 仅文本提及单列且 only_A 为空",
            not d3 and d4 == ["reflect_scripts/05_e.py"]
            and where3.get("reflect_scripts/05_e.py", []) == ["comment_only.plist", "typo.plist"],
            "only_A=%s only_B=%s where=%s" % (d3, d4, where3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in checks if c)
    for name, c, extra in checks:
        print("  %-52s %s%s" % (name, "PASS" if c else "FAIL", "" if c else "  <<< %s" % extra))
    print("selftest: %d/%d PASS" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True, description="reflect 接线覆盖率核查（只读）")
    ap.add_argument("cmd", nargs="?", default="scan",
                    choices=["scan", "gaps", "crosscheck", "json", "proof", "selftest"])
    ap.add_argument("arg", nargs="?", default=None, help="json 子命令的输出路径")
    ap.add_argument("--ga", default=str(GA_ROOT))
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    ga = Path(args.ga)
    if args.cmd == "proof":
        rep = analyze(ga)
        print("files=%d changed=%s" % (rep["proof"]["files"], rep["proof"]["changed"] or "[]"))
        return 0 if not rep["proof"]["changed"] else 1

    rep = analyze(ga)

    if args.cmd == "json":
        out = Path(args.arg) if args.arg else None
        if not out:
            print("json 子命令需要输出路径", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print("wrote %s (%d bytes) rc=%d" % (out, out.stat().st_size, rep["rc"]))
        return rep["rc"]

    if args.cmd == "gaps":
        print("== 缺口（%d）+ 注册表问题（%d）" % (len(rep["gaps"]), len(rep["registry_errors"])))
        for g in rep["gaps"]:
            print("  %-18s %-34s %s" % (g["kind"], g["script"], g["detail"]))
        for e in rep["registry_errors"]:
            print("  REG %-14s %s" % (e["code"], e["detail"]))
        print("-- 只读自证: files=%d changed=%s" % (rep["proof"]["files"], rep["proof"]["changed"] or "[]"))
        print("rc=%d" % rep["rc"])
        return rep["rc"]

    if args.cmd == "crosscheck":
        A, B, d1, d2, where = crosscheck(ga, rep)
        print("A(结构化接线 W1∪W2) = %s" % A)
        print("B(文本引用,等价 grep -lF) = %s" % B)
        print("only_A(结构化有、文本无) = %s" % d1)
        print("only_B(文本提及但非接线) = %s" % d2)
        for k, v in where.items():
            print("    %s <- %s" % (k, ", ".join(v)))
        verdict = "MATCH" if (not d1 and not d2) else ("DEFECT" if d1 else "TEXT_ONLY")
        print("crosscheck: %s (|A|=%d |B|=%d)" % (verdict, len(A), len(B)))
        return 1 if d1 else 0

    print(render_text(rep))
    return rep["rc"]


if __name__ == "__main__":
    sys.exit(main())
