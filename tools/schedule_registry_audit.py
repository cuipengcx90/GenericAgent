#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""schedule_registry_audit.py — 调度注册面「三源对账」只读审计器（只保留 ④ 象限）

TODO L214（第25批 C7「调度注册面 ④ 象限对账器」的收窄实现）。

已有工具的判据边界（本器补的是它们的交集空洞）：
  R411 task_freshness  判「在册目标的产物是否过期」——只看见在册目标；
  R488 reflect_wiring  判「reflect 脚本是否被接线」——只看见 reflect/ 目录里的脚本；
  R480/R511 告警面     判「告警是否被消费/污染」——只看见已登记目标；
  ⇒ 无人回答：**盘上有产物（done 家族），但四源（声明/归档/实装/在册）全都查不到它**
     这一类「停跑后声明文件被删、产物仍留在盘上」的家族，对上述所有判据都是隐形的。

本器输入面（四源，全部只读）：
  S1 声明  sche_tasks/*.json          文件名词干 == 家族名
  S2 归档  sche_tasks/disabled/*.json 文件名词干 == 家族名
  S3 实装  ~/Library/LaunchAgents/*.plist  文本（归一化后）含家族 token
  S4 在册  reflect_scripts/*.py + reflect/*.py  文本（归一化后）含家族 token
  P  产物  sche_tasks/done/*.md 家族（键复用 tools/content_change_detector.done_family_key，R470）

四象限：DECLARED_ACTIVE / DECLARED_ARCHIVED / LAUNCHD_ONLY / REFLECT_ONLY / UNREGISTERED(=④)。
④ 为唯一告警面：rc=1。另给「近名登记」提示（token 交集但非同名）——提示级，不参与判决
（沿用 R448 教训：近名信号 8/8 假误报，只可作提示）。

子命令（位置参数，非 --flag）：
  scan [--json PATH]  扫描并打印 ④ 清单（含语料计数自证）
  proof               只读自证：扫描前后语料 sha256 逐项一致
  crosscheck          与 R488 reflect_wiring_check 的 launchd 接线集独立交叉核验（逐集相等）
  selftest            夹具 + 10 组用例（含负控）

rc: 0=无 ④  1=存在 ④（登记面空洞；不等于链路故障，需人读产物新旧）  2=输入无效（缺产物面 /
    全部登记源为空 / 源不可读 / crosscheck 依赖缺失）

只读保证：本器不写任何调度文件、不调用 launchctl、不 import 被审计脚本；仅 glob/读文本 + 可选
写 --json 产物到调用者指定路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

GA_ROOT = Path(__file__).resolve().parent.parent
DONE_GLOB = "sche_tasks/done/*.md"
DECL_ACTIVE_GLOB = "sche_tasks/*.json"
DECL_ARCHIVED_GLOB = "sche_tasks/disabled/*.json"
REFLECT_GLOBS = ("reflect_scripts/*.py", "reflect/*.py")
CRITERIA_REL = "tools/freshness_criteria.json"
R488_REL = "tools/reflect_wiring_check.py"
DEFAULT_LAUNCHAGENTS = ("~/Library/LaunchAgents",)

SCHEMA = "ga.schedule_registry_audit/v1"
UNREGISTERED = "UNREGISTERED"
STATUSES = ("DECLARED_ACTIVE", "DECLARED_ARCHIVED", "LAUNCHD_ONLY", "REFLECT_ONLY", UNREGISTERED)

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[_-](\d{2})(\d{2})")
_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def norm(s: str) -> str:
    """归一化：非字母数字一律折叠为 '_'，小写。

    目的：`com.cuip.genericagent-price-alert.plist` 与家族 `price_alert` 必须能互相命中
    （连字符/下划线/点号在文件名里自由混用，用下划线边界正则匹配会漏）。
    """
    return _TOKEN_RE.sub("_", s or "").strip("_").lower()


def text_tokens(s: str):
    """归一化后再切 token 序列（用于『整 token 序贯命中』判定）。"""
    return [t for t in norm(s).split("_") if t]


def contains_tokens(text_tokens_, fam_tokens) -> bool:
    """家族 token 序列须在文本 token 序列中**连续整 token**出现。

    为什么不能直接子串匹配：`com.test.delta2` 归一化后含 'delta2'，直接 `'delta' in '...delta2...'`
    为真 ⇒ 邻号家族会互相冒充（实测 NC5/NC9 失败根因）。
    """
    n = len(fam_tokens)
    if n == 0 or len(text_tokens_) < n:
        return False
    for i in range(len(text_tokens_) - n + 1):
        if text_tokens_[i:i + n] == fam_tokens:
            return True
    return False


def fam_key(basename: str) -> str:
    """产物→家族键。复用 R470 的单一源 content_change_detector.done_family_key；不可用时内联回退。"""
    try:
        sys.path.insert(0, str(GA_ROOT / "tools"))
        from content_change_detector import done_family_key  # type: ignore

        return done_family_key(basename)
    except Exception:
        m = _DATE_RE.match(basename)
        stem = basename[:-3] if basename.endswith(".md") else basename
        if not m:
            return norm(stem)
        rest = stem[m.end():].lstrip("_-")
        return norm(rest) or norm(stem)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(root: Path, la_dirs) -> list:
    """语料面（用于只读自证）：产物 + 三源登记文件 + 登记辅助文件。"""
    out = []
    for pat in (DONE_GLOB, DECL_ACTIVE_GLOB, DECL_ARCHIVED_GLOB) + REFLECT_GLOBS:
        out.extend(sorted(root.glob(pat)))
    for d in la_dirs:
        p = Path(os.path.expanduser(str(d)))
        if p.is_dir():
            out.extend(sorted(p.glob("*.plist")))
    crit = root / CRITERIA_REL
    if crit.is_file():
        out.append(crit)
    return sorted(set(out))


def corpus_digest(files) -> dict:
    return {str(p): _sha256(p) for p in files if p.is_file()}


# --------------------------------------------------------------------------- inputs


def collect_products(root: Path) -> dict:
    """产物面：done/*.md → 家族 → 文件列表。"""
    fams = {}
    for p in sorted(root.glob(DONE_GLOB)):
        k = fam_key(p.name)
        d = fams.setdefault(k, {"family": k, "files": [], "dates": []})
        d["files"].append(p)
        m = _DATE_RE.match(p.name)
        if m:
            d["dates"].append(m.group(1))
    for k, d in fams.items():
        d["products"] = len(d["files"])
        d["oldest"] = min(d["dates"]) if d["dates"] else None
        d["newest"] = max(d["dates"]) if d["dates"] else None
        d["newest_mtime"] = max(f.stat().st_mtime for f in d["files"])
        d["newest_mtime_iso"] = datetime.fromtimestamp(d["newest_mtime"]).strftime("%Y-%m-%d %H:%M:%S")
    return fams


def collect_registry(root: Path, la_dirs):
    """登记面：S1 声明 / S2 归档 / S3 实装 / S4 在册。"""
    idx = {
        "decl_active": {},   # stem -> relpath
        "decl_archived": {},
        "launchd": [],       # {"name","path","label","norms":[...]}  含文件名与全文归一
        "reflect": [],       # {"name","path","norm"}
        "dups": [],
    }
    for p in sorted(root.glob(DECL_ACTIVE_GLOB)):
        stem = p.stem
        if stem in idx["decl_active"]:
            idx["dups"].append(f"sche_tasks/{stem}.json")
        idx["decl_active"][stem] = str(p.relative_to(root))
    for p in sorted(root.glob(DECL_ARCHIVED_GLOB)):
        stem = p.stem
        if stem in idx["decl_archived"]:
            idx["dups"].append(f"sche_tasks/disabled/{stem}.json")
        idx["decl_archived"][stem] = str(p.relative_to(root))
    for d in la_dirs:
        dd = Path(os.path.expanduser(str(d)))
        if not dd.is_dir():
            continue
        for p in sorted(dd.glob("*.plist")):
            txt = _read_text(p)
            label = ""
            try:
                with open(p, "rb") as fh:
                    data = plistlib.load(fh)
                if isinstance(data, dict):
                    label = str(data.get("Label") or "")
            except Exception:
                label = ""
            idx["launchd"].append({
                "name": p.name,
                "path": str(p),
                "label": label,
                "tokens": text_tokens(txt + " " + label + " " + p.name),
            })
    for pat in REFLECT_GLOBS:
        for p in sorted(root.glob(pat)):
            idx["reflect"].append({
                "name": p.name,
                "path": str(p.relative_to(root)),
                "tokens": text_tokens(_read_text(p) + " " + p.name),
            })
    return idx


def _tokens(name: str):
    return {t for t in norm(name).split("_") if len(t) >= 3}


def _hints(fam: str, registered_names, token_df=None, stop_df=5, cap=3) -> list:
    """近名登记提示（提示级，不参与判决）。

    噪音控制（实测：不控噪音时「共享 monitor/daily」这类泛词会把提示刷成 8 条无信息量）：
    先剔除在登记面出现 ≥stop_df 次的通用 token（如 daily/monitor），再按共享 token 数排序取前 cap 条。
    """
    fam_t = _tokens(fam)
    df = token_df or {}
    out = []
    for rn in sorted(registered_names):
        if not rn or rn == fam:
            continue
        inter = {t for t in (fam_t & _tokens(rn)) if df.get(t, 0) < stop_df}
        if inter:
            out.append({"name": rn, "shared_tokens": sorted(inter)})
    out.sort(key=lambda h: (-len(h["shared_tokens"]), h["name"]))
    return out[:cap]


def freshness_ids(root: Path) -> set:
    p = root / CRITERIA_REL
    if not p.is_file():
        return set()
    try:
        data = json.loads(_read_text(p))
    except Exception:
        return set()
    found = set()

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("id"), str):
                found.add(o["id"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return found


# --------------------------------------------------------------------------- scan


def scan(root: Path, la_dirs) -> dict:
    fams = collect_products(root)
    idx = collect_registry(root, la_dirs)
    if not fams:
        return {"schema": SCHEMA, "generated": now_iso(), "ga_root": str(root),
                "error": f"产物面为空：{DONE_GLOB} 无文件（无法对账）", "rc": 2,
                "counts": {"families": 0}, "families": [], "unregistered": []}
    if not idx["decl_active"] and not idx["decl_archived"] and not idx["launchd"] and not idx["reflect"]:
        return {"schema": SCHEMA, "generated": now_iso(), "ga_root": str(root),
                "error": "登记面四源全空：声明/归档/实装/在册一条都没有（输入无效）", "rc": 2,
                "counts": {"families": len(fams)}, "families": [], "unregistered": []}

    registered_names = set(idx["decl_active"]) | set(idx["decl_archived"])
    registered_names |= {p["name"][:-6] for p in idx["launchd"] if p["name"].endswith(".plist")}
    registered_names |= {p["label"] for p in idx["launchd"] if p["label"]}
    registered_names |= {Path(p["path"]).stem for p in idx["reflect"]}
    fresh = freshness_ids(root)
    token_df = {}
    for rn in registered_names:
        for t in _tokens(rn):
            token_df[t] = token_df.get(t, 0) + 1

    rows = []
    for fam, d in sorted(fams.items()):
        ftok = text_tokens(fam)
        ra = idx["decl_active"].get(fam)
        rb = idx["decl_archived"].get(fam)
        lm = [p["name"] for p in idx["launchd"] if contains_tokens(p["tokens"], ftok)]
        rf = [p["path"] for p in idx["reflect"] if contains_tokens(p["tokens"], ftok)]
        if ra:
            status = "DECLARED_ACTIVE"
        elif rb:
            status = "DECLARED_ARCHIVED"
        elif lm:
            status = "LAUNCHD_ONLY"
        elif rf:
            status = "REFLECT_ONLY"
        else:
            status = UNREGISTERED
        rows.append({
            "family": fam,
            "products": d["products"],
            "oldest": d["oldest"],
            "newest": d["newest"],
            "newest_mtime_iso": d["newest_mtime_iso"],
            "status": status,
            "evidence": {
                "decl_active": ra,
                "decl_archived": rb,
                "launchd": lm,
                "reflect": rf,
            },
            "hints": _hints(fam, registered_names, token_df),
            "in_freshness_criteria": fam in fresh,
            "sample_files": [f.name for f in d["files"][-3:]],
        })
    unreg = sorted([r for r in rows if r["status"] == UNREGISTERED],
                   key=lambda r: (-r["products"], r["family"]))
    dist = {}
    for r in rows:
        dist[r["status"]] = dist.get(r["status"], 0) + 1
    return {
        "schema": SCHEMA,
        "generated": now_iso(),
        "ga_root": str(root),
        "sources": {
            "done_glob": DONE_GLOB,
            "decl_active": len(idx["decl_active"]),
            "decl_archived": len(idx["decl_archived"]),
            "launchd": len(idx["launchd"]),
            "launchd_dirs": [str(Path(os.path.expanduser(str(d)))) for d in la_dirs],
            "reflect": len(idx["reflect"]),
            "dup_stems": idx["dups"],
        },
        "counts": {
            "families": len(rows),
            "products": sum(r["products"] for r in rows),
            "status": dist,
            "unregistered": len(unreg),
            "unregistered_products": sum(r["products"] for r in unreg),
        },
        "families": rows,
        "unregistered": unreg,
        "rc": 1 if unreg else 0,
    }


def render(rep: dict) -> str:
    L = []
    if rep.get("error"):
        L.append(f"ERROR: {rep['error']}")
        return "\n".join(L)
    s, c = rep["sources"], rep["counts"]
    L.append("== 语料自证 ==")
    L.append(f"  产物 {s['done_glob']} → 家族 {c['families']} / 报告 {c['products']} 份")
    L.append("  登记面: sche_tasks %d / disabled %d / plists %d / reflect %d"
             % (s["decl_active"], s["decl_archived"], s["launchd"], s["reflect"]))
    L.append(f"  plist 目录: {', '.join(s['launchd_dirs'])}")
    if s["dup_stems"]:
        L.append(f"  ⚠ 重复词干: {s['dup_stems']}")
    L.append("== 家族状态分布 ==")
    for st in STATUSES:
        if st in c["status"]:
            L.append(f"  {st:<18} {c['status'][st]}")
    L.append(f"== ④ 无任何登记（{c['unregistered']} 家族 / {c['unregistered_products']} 份产物）==")
    if not rep["unregistered"]:
        L.append("  （无）")
    for r in rep["unregistered"]:
        ev = r["evidence"]
        L.append(f"  {r['family']:<34} 产物 {r['products']:>3} 份  最后产出 {r['newest'] or '?'}"
                 f"（mtime {r['newest_mtime_iso']}）{'  [freshness在册]' if r['in_freshness_criteria'] else ''}")
        L.append(f"      证据: sche_tasks/-={ev['decl_active'] or '-'} disabled/-={ev['decl_archived'] or '-'}"
                 f" launchd={ev['launchd'] or '-'} reflect={ev['reflect'] or '-'}")
        L.append(f"      样本: {', '.join(r['sample_files'])}")
        if r["hints"]:
            hs = "; ".join(f"{h['name']} (共享 {'/'.join(h['shared_tokens'])})" for h in r["hints"])
            L.append(f"      近名登记（提示级，不参与判决）: {hs}")
    L.append("== 提示级近名（全家族） ==")
    n = 0
    for r in rep["families"]:
        if r["status"] == UNREGISTERED and r["hints"]:
            continue
        if r["hints"]:
            n += 1
            hs = "; ".join(h["name"] for h in r["hints"])
            L.append(f"  {r['family']:<34} ↔ {hs}")
    if n == 0:
        L.append("  （无）")
    L.append(f"rc={rep['rc']}  （rc=1 仅表示存在登记面空洞，不等于链路故障；请结合产物新旧人工判读）")
    return "\n".join(L)


# --------------------------------------------------------------------------- crosscheck


def launchd_reflect_set(la_dirs):
    """独立实现 R488 的 W1 规则：ProgramArguments 中相邻 token 对 `--reflect <path>`。

    返回 (wired_plist_basenames, wired_script_relpaths, unreadable)。
    """
    plists, scripts, unreadable = set(), set(), []
    for d in la_dirs:
        dd = Path(os.path.expanduser(str(d)))
        if not dd.is_dir():
            continue
        for p in sorted(dd.glob("*.plist")):
            try:
                with open(p, "rb") as fh:
                    data = plistlib.load(fh)
            except Exception as e:
                unreadable.append(f"{p.name}: {type(e).__name__}")
                continue
            if not isinstance(data, dict):
                continue
            args = data.get("ProgramArguments") or []
            if not isinstance(args, list):
                continue
            hit = False
            for i in range(len(args) - 1):
                if str(args[i]) == "--reflect":
                    hit = True
                    scripts.add(str(args[i + 1]))
            if hit:
                plists.add(p.name)
    return plists, scripts, unreadable


def parse_r488(report: dict):
    """从 R488 `--json` 报告里抽取 launchd 接线面（两条通路：launchd 段 + scripts 段）。"""
    a, b = set(), set()
    for it in report.get("launchd") or []:
        if isinstance(it, dict):
            if it.get("plist"):
                a.add(Path(str(it["plist"])).name)
            if it.get("script"):
                b.add(str(it["script"]))
    for it in report.get("scripts") or []:
        if isinstance(it, dict) and it.get("status") == "WIRED_LAUNCHD":
            for ev in it.get("evidence") or []:
                if str(ev).endswith(".plist"):
                    a.add(Path(str(ev)).name)
                    b.add(str(it.get("id")))
    return a, b


def compare_wiring(mine_plists, mine_scripts, theirs_plists, theirs_scripts):
    return {
        "plists_equal": set(mine_plists) == set(theirs_plists),
        "scripts_equal": set(mine_scripts) == set(theirs_scripts),
        "plists": sorted(mine_plists),
        "plists_theirs": sorted(theirs_plists),
        "scripts": sorted(mine_scripts),
        "scripts_theirs": sorted(theirs_scripts),
        "plists_only_mine": sorted(set(mine_plists) - set(theirs_plists)),
        "plists_only_theirs": sorted(set(theirs_plists) - set(mine_plists)),
    }


def crosscheck(root: Path, la_dirs) -> dict:
    tool = root / R488_REL
    if not tool.is_file():
        return {"rc": 2, "error": f"依赖缺失: {R488_REL}"}
    tmp = Path(tempfile.mkdtemp(prefix="sra_xcheck_"))
    out = tmp / "r488.json"
    try:
        r = subprocess.run([sys.executable, str(tool), "json", str(out)],
                           cwd=str(root), capture_output=True, text=True, timeout=300)
        if not out.is_file():
            return {"rc": 2, "error": f"R488 未产出 JSON（rc={r.returncode}）: {(r.stderr or r.stdout or '')[-300:]}"}
        report = json.loads(_read_text(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    mine_p, mine_s, unreadable = launchd_reflect_set(la_dirs)
    theirs_p, theirs_s = parse_r488(report)
    cmp_ = compare_wiring(mine_p, mine_s, theirs_p, theirs_s)
    ok = cmp_["plists_equal"] and cmp_["scripts_equal"]
    return {"rc": 0 if ok else 1, "verdict": "AGREE" if ok else "MISMATCH",
            "mine": {"plists": sorted(mine_p), "scripts": sorted(mine_s)},
            "theirs": {"plists": sorted(theirs_p), "scripts": sorted(theirs_s)},
            "diff": cmp_,
            "unreadable_plists": unreadable,
            "note": "两实现独立（本器 plistlib 自解析 / R488 自有读取器），只比集合不比文件数（R488 坑）"}


# --------------------------------------------------------------------------- selftest


NC = []


def _case(name, cond, detail=""):
    NC.append((name, bool(cond), detail))
    return bool(cond)


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _mk_fixture(base: Path):
    """夹具：6 个家族覆盖 5 种状态 + 1 个近名提示。"""
    root = base / "repo"
    la = base / "LaunchAgents"
    la.mkdir(parents=True)
    prod = {
        "2026-01-01_0000_alpha.md": "alpha 1",
        "2026-01-02_0000_alpha.md": "alpha 2",
        "2026-01-03_0000_beta.md": "beta",
        "2026-01-04_0000_gamma.md": "gamma",
        "2026-01-05_0000_delta.md": "delta",
        "2026-01-06_0000_epsilon.md": "epsilon",
        "2026-01-07_0000_wechat_daily_sqlcipher_output.md": "wc out",
    }
    for n, t in prod.items():
        _write(root / "sche_tasks" / "done" / n, t)
    _write(root / "sche_tasks" / "alpha.json", '{"id":"alpha","enabled":true}')
    _write(root / "sche_tasks" / "wechat_daily_sqlcipher.json", '{"id":"wechat_daily_sqlcipher","enabled":true}')
    _write(root / "sche_tasks" / "disabled" / "beta.json", '{"id":"beta","enabled":false}')
    _write(root / "reflect_scripts" / "01_epsilon.py", "# epsilon reflector\n")
    (la / "com.test.delta.plist").write_bytes(plistlib.dumps(
        {"Label": "com.test.delta", "ProgramArguments": ["/bin/sh", "-c", "echo delta"]}))
    # 邻号家族负控：plist 名/token 含 delta2 但**不含整 token** delta（且其 reflect 指向中性脚本）
    (la / "com.test.delta2.plist").write_bytes(plistlib.dumps(
        {"Label": "com.test.delta2", "ProgramArguments": ["/usr/bin/python3", "--reflect", "reflect_scripts/00_helper.py"]}))
    return root, la


def selftest() -> int:
    base = Path(tempfile.mkdtemp(prefix="sra_selftest_"))
    try:
        root, la = _mk_fixture(base)
        rep = scan(root, [la])
        got = {r["family"] for r in rep["unregistered"]}
        _case("NC1 凭空造『有产物无登记』必抓", got == {"gamma", "wechat_daily_sqlcipher_output"}, f"got={sorted(got)}")
        _case("NC1b rc=1（存在 ④）", rep["rc"] == 1, f"rc={rep['rc']}")
        st = {r["family"]: r["status"] for r in rep["families"]}
        _case("NC2 声明命中=DECLARED_ACTIVE", st["alpha"] == "DECLARED_ACTIVE", st["alpha"])
        _case("NC3 归档声明=DECLARED_ARCHIVED（不算 ④）", st["beta"] == "DECLARED_ARCHIVED", st["beta"])
        _case("NC4 仅实装=LAUNCHD_ONLY（不算 ④）", st["delta"] == "LAUNCHD_ONLY", st["delta"])
        _case("NC5 仅在册=REFLECT_ONLY（不算 ④）", st["epsilon"] == "REFLECT_ONLY", st["epsilon"])
        wc = [r for r in rep["unregistered"] if r["family"] == "wechat_daily_sqlcipher_output"][0]
        _case("NC6 近名登记只提示不改判",
              wc["status"] == UNREGISTERED and any(h["name"] == "wechat_daily_sqlcipher" for h in wc["hints"]),
              str(wc["hints"]))
        _case("NC7 语料计数自证（家族=6/产物=7；alpha 两文件同族）",
              rep["counts"]["families"] == 6 and rep["counts"]["products"] == 7, str(rep["counts"]))
        _case("NC7b token 边界负控：com.test.delta2 不注册家族 delta",
              rep["families"][[r["family"] for r in rep["families"]].index("delta")]["evidence"]["launchd"] == ["com.test.delta.plist"],
              str(rep["families"][[r["family"] for r in rep["families"]].index("delta")]["evidence"]))

        # 只改声明 ⇒ 必须改判
        _write(root / "sche_tasks" / "gamma.json", '{"id":"gamma","enabled":true}')
        rep2 = scan(root, [la])
        st2 = {r["family"]: r["status"] for r in rep2["families"]}
        _case("NC8 只改声明即改判（gamma ④→DECLARED_ACTIVE）",
              st2["gamma"] == "DECLARED_ACTIVE" and "gamma" not in {r["family"] for r in rep2["unregistered"]},
              st2["gamma"])
        _write(root / "sche_tasks" / "wechat_daily_sqlcipher_output.json", '{"id":"wechat_daily_sqlcipher_output"}')
        rep3 = scan(root, [la])
        _case("NC8b ④ 清空后 rc=0", rep3["rc"] == 0 and rep3["counts"]["unregistered"] == 0, str(rep3["counts"]))
        _case("NC8c 未登记族数 2→1→0（单调）",
              (rep["counts"]["unregistered"], rep2["counts"]["unregistered"], rep3["counts"]["unregistered"]) == (2, 1, 0),
              f"{rep['counts']['unregistered']}/{rep2['counts']['unregistered']}/{rep3['counts']['unregistered']}")

        # 反向负控：撤实装 ⇒ 家族回落 ④
        (la / "com.test.delta.plist").unlink()
        rep4 = scan(root, [la])
        st4 = {r["family"]: r["status"] for r in rep4["families"]}
        _case("NC9 撤实装即改判（delta LAUNCHD_ONLY→④）", st4["delta"] == UNREGISTERED, st4["delta"])

        # 语料空 ⇒ rc=2
        for f in (root / "sche_tasks" / "done").glob("*.md"):
            f.unlink()
        rep5 = scan(root, [la])
        _case("NC10 产物面为空 rc=2", rep5["rc"] == 2 and "error" in rep5, f"rc={rep5['rc']}")

        # 登记面全空 ⇒ rc=2
        root2, la2 = _mk_fixture(base / "b2")
        for f in (root2 / "sche_tasks").glob("*.json"):
            f.unlink()
        for f in (root2 / "sche_tasks" / "disabled").glob("*.json"):
            f.unlink()
        for f in (root2 / "reflect_scripts").glob("*.py"):
            f.unlink()
        for f in la2.glob("*.plist"):
            f.unlink()
        rep6 = scan(root2, [la2])
        _case("NC11 登记面四源全空 rc=2", rep6["rc"] == 2 and "error" in rep6, f"rc={rep6['rc']}")

        # 只读自证：sandbox 语料 sha256 前后一致
        root3, la3 = _mk_fixture(base / "b3")
        before = corpus_digest(collect_files(root3, [la3]))
        scan(root3, [la3])
        scan(root3, [la3])
        after = corpus_digest(collect_files(root3, [la3]))
        _case("NC12 只读自证（沙盒语料 sha256 前后一致 + 两遍一致）",
              before == after and len(before) > 0, f"files={len(before)}")

        # crosscheck 比较器：正/负控
        mp, ms = {"a.plist"}, {"reflect/x.py"}
        _case("NC13 crosscheck 比较器：同集 AGREE", compare_wiring(mp, ms, {"a.plist"}, {"reflect/x.py"})["plists_equal"])
        cmp_bad = compare_wiring(mp, ms, {"b.plist"}, {"reflect/x.py"})
        _case("NC14 crosscheck 比较器：异集必 MISMATCH",
              (not cmp_bad["plists_equal"]) and cmp_bad["plists_only_mine"] == ["a.plist"])
        fake = {"launchd": [{"plist": "/tmp/a.plist", "script": "reflect/x.py"}]}
        tp, ts = parse_r488(fake)
        _case("NC15 R488 JSON 解析（launchd 段）", tp == {"a.plist"} and ts == {"reflect/x.py"}, f"{tp}/{ts}")
        fake2 = {"scripts": [{"id": "reflect/y.py", "status": "WIRED_LAUNCHD",
                              "evidence": ["/tmp/c.plist"]}]}
        tp2, ts2 = parse_r488(fake2)
        _case("NC16 R488 JSON 解析（scripts 段）", tp2 == {"c.plist"} and ts2 == {"reflect/y.py"}, f"{tp2}/{ts2}")
        _case("NC17 R488 解析不吃非接线项",
              parse_r488({"scripts": [{"id": "reflect/z.py", "status": "DECLARED_pending_approval",
                                       "evidence": ["x.md"]}]}) == (set(), set()))

        # 真仓库只读自证（沙盒之外的唯一真跑面，只读不判分）
        real_before = corpus_digest(collect_files(GA_ROOT, DEFAULT_LAUNCHAGENTS))
        scan(GA_ROOT, DEFAULT_LAUNCHAGENTS)
        real_after = corpus_digest(collect_files(GA_ROOT, DEFAULT_LAUNCHAGENTS))
        _case("NC18 真仓只读自证（语料 sha256 前后一致）",
              real_before == real_after and len(real_before) > 0, f"files={len(real_before)}")

        failed = [n for n, ok, _ in NC if not ok]
        for name, ok, detail in NC:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
        print(f"== selftest: {len(NC) - len(failed)}/{len(NC)} passed ==")
        return 0 if not failed else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="调度注册面四源对账（只读）")
    ap.add_argument("cmd", nargs="?", default="scan",
                    choices=["scan", "proof", "crosscheck", "selftest"])
    ap.add_argument("--root", default=str(GA_ROOT))
    ap.add_argument("--launchagents", action="append", default=None,
                    help="可重复；默认 ~/Library/LaunchAgents")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    la = a.launchagents or list(DEFAULT_LAUNCHAGENTS)

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "crosscheck":
        res = crosscheck(root, la)
        print(json.dumps(res, ensure_ascii=False, indent=2) if a.json_out else
              f"verdict={res.get('verdict', 'ERROR')}\n"
              + (f"error={res.get('error')}\n" if res.get("error") else "")
              + f"mine={res.get('mine', {}).get('plists')}\ntheirs={res.get('theirs', {}).get('plists')}\n"
              + f"scripts mine={res.get('mine', {}).get('scripts')}\ntheirs={res.get('theirs', {}).get('scripts')}\n"
              + f"rc={res['rc']}")
        if a.json_out:
            Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        return int(res["rc"])
    if a.cmd == "proof":
        files = collect_files(root, la)
        before = corpus_digest(files)
        rep = scan(root, la)
        after = corpus_digest(collect_files(root, la))
        changed = sorted([k for k in set(before) | set(after) if before.get(k) != after.get(k)])
        print(f"files={len(before)} changed={changed} scan_rc={rep['rc']}")
        return 0 if not changed else 1

    rep = scan(root, la)
    if not a.quiet:
        print(render(rep))
    if a.json_out:
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        if not a.quiet:
            print(f"-- JSON → {a.json_out}")
    return int(rep["rc"])


if __name__ == "__main__":
    sys.exit(main())
