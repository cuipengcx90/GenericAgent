#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_census.py — 资产「真实使用热度」× T0 备份清单 × tools/ 消费面 三向对账（只读）

判据（全部可复跑、可证伪；写死在 docstring 里，报告与实现同源）
--------------------------------------------------------------------------
[1] T0 覆盖面：以工具 t0_snapshot.py 的 SETS 为「集合前缀」，
    covered_by_set = 文件路径落在某个集合前缀内（memory/ temp/kc_snapshots/ tools/ reflect_scripts/ sche_tasks/{*.json,*.py}）；
    in_manifest    = 该 path 出现在最新一份 R381 产出的 manifest.json 的 arc 列表里。
    二者拆开：集合前缀覆盖但不在 manifest = 「清单已过期」；不在任何集合 = 「从未纳入」。
[2] tools 消费面（每个 tools/*.py，剔自指）：
    接线      = 被 *.plist / sche_tasks/ / reflect_scripts/ / 其它 tools/ 文件按文件名引用
    仅记忆引用 = 只被 durable 记忆（memory/ 下非 autonomous_reports 的文件）引用
    仅报告引用 = 只在 memory/autonomous_reports/*.md 里出现
    无引用    = 仓内 tracked 文件零命中（第二证据见 --evidence）
    检索用 `git -c core.quotepath=false grep -l -F -- <name>`（约 0.1s/次；禁用 grep -r，temp/ 5.8G 会超时）
[3] 热度信号（4 个，逐项给原始证据，不做加权）：
    W 接线（被 plist/sche_tasks/reflect_scripts/代码引用）
    D durable 记忆引用（L1/L2/SOP）
    R 近 7 天文件活动（mtime）
    M 机器真实使用数据承载（kc/chrome/pmset 源快照目录）
    heat_tier = 命中信号数；HIGH = tier ≥ 2
[4] 结论 = HIGH 且未被 T0 清单覆盖（covered_by_set=False）的资产 → 备份缺口
[5] 只读自证：对「本工具读过的语料」（manifest + tools/* + ~/Library/LaunchAgents/*.plist）做
    前后 sha256 指纹比对；全部写入只落在 --out 目录内。
    注：memory/ 被并行会话持续写入，故**不**纳入指纹语料（避免假告警）。

用法
  python3 tools/usage_census.py --out temp/usage_census      # 真跑（只写 out 目录）
  python3 tools/usage_census.py --json | head                # 摘要
  python3 tools/usage_census.py --selftest                   # 纯函数自测（含负控）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT = ["git", "-c", "core.quotepath=false"]
HIGH_TIER = 2
TEMP_WALK_CAP_FILES = 8000
TEMP_WALK_CAP_SEC = 5.0
TEMP_WALK_TOTAL_BUDGET_SEC = 180.0  # temp/ 总扫描预算（超时后续目录标 capped，避免 5.8G 拖死）
MACHINE_SOURCE_DIRS = {"kc_snapshots", "chrome_snapshots", "pmset_snapshots"}


# ---------------------------------------------------------------- 复用 t0_snapshot
def load_t0_module():
    p = ROOT / "tools" / "t0_snapshot.py"
    spec = importlib.util.spec_from_file_location("t0_snapshot", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


# ---------------------------------------------------------------- 基础
def sh(args, cwd=None):
    r = subprocess.run(args, cwd=str(cwd or ROOT), capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def git_grep(name):
    """按文件名在全仓 tracked 文件里检索；返回命中路径列表（去重、去引号）。"""
    rc, out, _ = sh(GIT + ["grep", "-l", "-F", "--", name])
    hits = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        hits.append(line)
    return sorted(set(hits))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unquote_git_path(s: str) -> str:
    """git grep -l 在 quotepath=true 时输出带引号/八进制转义；本工具统一传
    -c core.quotepath=false，故这里只做保险性剥引号（不解释八进制）。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def classify_face(hits, self_path):
    """消费面分类（纯函数，供 selftest）。hits=命中路径列表，self_path=自身相对路径。"""
    code, cfg, durable, reports, other = [], [], [], [], []
    for h in hits:
        if h == self_path:
            continue
        if h.startswith("memory/autonomous_reports/"):
            reports.append(h)
        elif h.startswith("memory/"):
            durable.append(h)
        elif h.startswith(("tools/", "reflect_scripts/")) or h.endswith((".sh", ".js", ".py")):
            code.append(h)
        elif h.startswith("sche_tasks/") or h.endswith((".plist", ".json")) or h.startswith("docs/"):
            cfg.append(h)
        else:
            other.append(h)
    if code or cfg or other:
        face = "接线"
    elif durable:
        face = "仅记忆引用"
    elif reports:
        face = "仅报告引用"
    else:
        face = "无引用"
    return {"face": face, "code": code, "cfg": cfg, "other": other,
            "durable": durable, "reports": reports}


REGENERABLE_PAT = re.compile(r"(snapshot|_cache$|^log|cache)", re.I)


def is_regenerable(name, kind, machine_names=MACHINE_SOURCE_DIRS):
    """可判定为「可从源再生」：机器源快照目录 / 日志·jsonl·db 文件 / 名字含 snapshot|cache。"""
    if kind == "dir" and name in machine_names:
        return True
    if REGENERABLE_PAT.search(name):
        return True
    if kind == "file" and ("log" in name or name.endswith((".jsonl", ".db", ".sqlite"))):
        return True
    return False


def heat_tier(signals):
    """signals: dict[W|D|R|M -> bool]；返回命中数。"""
    return sum(1 for v in signals.values() if v)


def is_high(signals, threshold=HIGH_TIER):
    return heat_tier(signals) >= threshold


def covered_by_set(arc, set_paths):
    """arc 是否为某集合前缀下的文件（纯函数，供 selftest）。"""
    for sp, globs in set_paths:
        if arc == sp or arc.startswith(sp.rstrip("/") + "/"):
            return True
        if globs:
            if "/" not in arc:  # 顶层 glob 集合
                import fnmatch
                if any(fnmatch.fnmatch(arc, g) for g in globs) or any(
                        fnmatch.fnmatch(os.path.basename(arc), g) for g in globs):
                    return True
    return False


# ---------------------------------------------------------------- T0 轴
def find_manifest(explicit=None):
    if explicit:
        return Path(explicit)
    cands = sorted((ROOT / "temp" / "t0_backups").glob("*.manifest.json"))
    if not cands:
        raise SystemExit("[ERR] 找不到 temp/t0_backups/*.manifest.json（R381 产物）")
    return cands[-1]


def t0_axis(man_path: Path, t0):
    man = json.load(open(man_path, encoding="utf-8"))
    arcs = [f["arc"] for f in man["files"]]
    set_paths = [(s["path"], s.get("globs")) for s in t0.SETS]
    covered = {a: covered_by_set(a, set_paths) for a in arcs}
    per_set = {}
    for a in arcs:
        for s in t0.SETS:
            sp = s["path"]
            if a == sp or a.startswith(sp.rstrip("/") + "/"):
                per_set.setdefault(s["name"], []).append(a)
                break
    # 当前盘上 vs manifest
    cur = {}
    for s in t0.SETS:
        got = [arc for _p, arc in t0.iter_set_files(s)]
        cur[s["name"]] = got
    delta = {}
    for name, now_list in cur.items():
        old = set(per_set.get(name, []))
        news = sorted(set(now_list) - old)
        gone = sorted(old - set(now_list))
        delta[name] = {"in_manifest": len(old), "now": len(now_list),
                       "new_since_backup": news, "gone_since_backup": gone}
    extra = {}
    for extra_dir in ["temp/chrome_snapshots", "temp/pmset_snapshots", "temp/pending_decisions",
                      "temp/autonomous_reports", "temp/usage_census"]:
        p = ROOT / extra_dir
        if p.exists():
            extra[extra_dir] = len([f for _r, _d, fs in os.walk(p) for f in fs])
    return {"manifest": str(man_path.relative_to(ROOT)), "generated": man.get("generated"),
            "sets": man.get("sets"), "total_files": len(arcs), "delta": delta, "extra_dirs": extra,
            "manifest_arcs": arcs, "set_paths": set_paths}


# ---------------------------------------------------------------- tools 轴
def tools_axis(plists=None):
    """tools/*.py 的消费面。

    引用源三类（缺一即「接线」被低估，2026-09-26 R421 实测）：
      A 路径限定名  tools/<name>            —— 显式路径写法
      B 带扩展名裸名 <name>.py              —— 少量直接写 python x.py 的调用
      C 裸模块名    <stem>                  —— import x / prompt 里写 x（R421 前遗漏，
                                              致 lan_ip.py 被误判成"仅报告引用"）
      D 仓外 LaunchAgents plist 文本包含 <name>（R421 前遗漏，致
                                              restore_ga_desktop_bridge.py 被误判成 dead）
    """
    rows = []
    plists = plists if plists is not None else plist_axis()
    for p in sorted((ROOT / "tools").glob("*.py")):
        name = p.name
        stem = name[:-3] if name.endswith(".py") else name
        self_rel = "tools/" + name
        hits = git_grep(self_rel) + git_grep(name) + git_grep(stem)
        for pl in plists:
            if name in pl["text"] or stem in pl["text"]:
                hits.append("~/Library/LaunchAgents/" + pl["name"])
        info = classify_face(sorted(set(hits)), self_rel)
        tracked = sh(["git", "ls-files", "--error-unmatch", self_rel])[0] == 0
        mt = p.stat().st_mtime
        rows.append({"tool": name, "size": p.stat().st_size, "tracked": tracked,
                     "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)),
                     "recent7d": (time.time() - mt) < 7 * 86400,
                     "code": info["code"], "cfg": info["cfg"], "other": info["other"],
                     "durable": info["durable"], "reports": info["reports"],
                     "face": info["face"]})
    return rows


# ---------------------------------------------------------------- temp 轴
def scan_dir(p: Path, cap_files=TEMP_WALK_CAP_FILES, cap_sec=TEMP_WALK_CAP_SEC):
    t0 = time.time()
    n = 0
    size = 0
    recent = 0
    now = time.time()
    capped = False
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                st = fp.stat()
            except OSError:
                continue
            n += 1
            size += st.st_size
            if now - st.st_mtime < 7 * 86400:
                recent += 1
            if n >= cap_files or time.time() - t0 > cap_sec:
                capped = True
                break
        if capped:
            break
    return {"files": n, "bytes": size, "recent7d": recent, "capped": capped}


def temp_axis():
    base = ROOT / "temp"
    rows = []
    t_start = time.time()
    for child in sorted(base.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            left = TEMP_WALK_TOTAL_BUDGET_SEC - (time.time() - t_start)
            st = scan_dir(child, cap_sec=max(0.5, min(TEMP_WALK_CAP_SEC, left)))
            if left <= 0:
                st["budget_exhausted"] = True
            rows.append({"name": child.name, "kind": "dir", **st})
        else:
            st = child.stat()
            rows.append({"name": child.name, "kind": "file", "files": 1, "bytes": st.st_size,
                         "recent7d": 1 if (time.time() - st.st_mtime) < 7 * 86400 else 0,
                         "capped": False})
    return rows


# ---------------------------------------------------------------- 热度轴
def machine_evidence():
    """机器真实使用数据（只读快照）→ 原始数字，供报告引用。"""
    out = {}
    kcs = sorted((ROOT / "temp" / "kc_snapshots").glob("*/knowledgeC.db"))
    if kcs:
        kc = kcs[-1]
        try:
            c = sqlite3.connect("file:%s?mode=ro" % kc, uri=True)
            n = c.execute("SELECT COUNT(*) FROM ZOBJECT WHERE ZSTREAMNAME='/app/usage'").fetchone()[0]
            top = c.execute("SELECT ZVALUESTRING, SUM(ZENDDATE-ZSTARTDATE) FROM ZOBJECT "
                            "WHERE ZSTREAMNAME='/app/usage' GROUP BY ZVALUESTRING "
                            "ORDER BY 2 DESC LIMIT 5").fetchall()
            out["kc"] = {"snapshot": str(kc.relative_to(ROOT)), "usage_rows": n,
                         "top_apps_sec": [[a, int(s or 0)] for a, s in top]}
            c.close()
        except Exception as e:  # 只读探针失败不致命
            out["kc"] = {"error": str(e)}
    chs = sorted((ROOT / "temp" / "chrome_snapshots").glob("*/History"))
    if chs:
        ch = chs[-1]
        try:
            h = sqlite3.connect("file:%s?mode=ro" % ch, uri=True)
            v, u = h.execute("SELECT COUNT(*), COUNT(DISTINCT url) FROM visits").fetchone()
            out["chrome"] = {"snapshot": str(ch.relative_to(ROOT)), "visits": v, "distinct_urls": u}
            h.close()
        except Exception as e:
            out["chrome"] = {"error": str(e)}
    pm = sorted((ROOT / "temp" / "pmset_snapshots").glob("*"))
    if pm:
        out["pmset"] = {"snapshot": str(pm[-1].relative_to(ROOT)),
                        "files": len([f for _r, _d, fs in os.walk(pm[-1]) for f in fs])}
    return out


def durable_hits(name):
    """durable 记忆（memory/ 下非 autonomous_reports）命中列表。"""
    return [h for h in git_grep(name)
            if h.startswith("memory/") and not h.startswith("memory/autonomous_reports/")]


def plist_axis():
    d = Path.home() / "Library" / "LaunchAgents"
    if not d.exists():
        return []
    rows = []
    for p in sorted(d.glob("*.plist")):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        rows.append({"name": p.name, "size": p.stat().st_size,
                     "mtime": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)),
                     "recent7d": (time.time() - p.stat().st_mtime) < 7 * 86400,
                     "text": txt})
    return rows


def assets_axis(t0_info, tools_rows, temp_rows, plists):
    """候选资产 → 热度信号 + 覆盖状态。"""
    plist_all = "\n".join(p["text"] for p in plists)
    man_arcs = set(t0_info["manifest_arcs"])
    set_paths = t0_info["set_paths"]
    assets = []

    def wiring_refs(key):
        refs = []
        for h in git_grep(key):
            if h.startswith("memory/autonomous_reports/"):
                continue
            refs.append(h)
        for p in plists:
            if key in p["text"]:
                refs.append("~/Library/LaunchAgents/" + p["name"])
        return refs

    # A. T0 集合本身（作为资产组）
    for s in t0_info["sets"]:
        name = s
        refs = wiring_refs(name if name != "sche_tasks_defs" else "sche_tasks")
        durable = durable_hits(name)
        in_man = any(a.startswith(s.replace("sche_tasks_defs", "sche_tasks") + "/") or
                     a.startswith(s + "/") for a in man_arcs)
        sig = {"W": bool(refs), "D": bool(durable), "R": True, "M": name in MACHINE_SOURCE_DIRS}
        assets.append({"asset": s, "kind": "t0_set", "covered": True, "in_manifest": in_man,
                       "signals": sig, "evidence": {"wiring": refs[:5],
                                                    "durable": durable[:5]}})

    # B. tools/*.py
    for r in tools_rows:
        key = r["tool"]
        sig = {"W": bool(r["code"] or r["cfg"] or any(key in p["text"] for p in plists)),
               "D": bool(r["durable"]), "R": r["recent7d"], "M": False}
        assets.append({"asset": "tools/" + key, "kind": "tool",
                       "covered": covered_by_set("tools/" + key, set_paths),
                       "in_manifest": ("tools/" + key) in man_arcs,
                       "signals": sig,
                       "evidence": {"code": r["code"][:4], "cfg": r["cfg"][:4],
                                    "durable": r["durable"][:3]}})

    # C. temp 顶层条目：只取「目录」与「代码类文件」（.py/.sh/.js/.swift）——
    #    一次性产物/日志/状态 json 由 temp_other 汇总，避免 1800+ 噪音条目。
    covered_other = {"files": 0, "bytes": 0}
    for r in temp_rows:
        key = r["name"]
        arc = "temp/" + key
        if not (r["kind"] == "dir" or key.endswith((".py", ".sh", ".js", ".swift"))):
            covered_other["files"] += 1
            covered_other["bytes"] += r["bytes"]
            continue
        # W 必须「路径限定」引用（temp/<name>），裸名命中只作弱证据（防通用词误判）
        refs = [h for h in git_grep("temp/" + key)
                if not h.startswith("memory/autonomous_reports/") and h != arc]
        for p in plists:
            if "temp/" + key in p["text"]:
                refs.append("~/Library/LaunchAgents/" + p["name"])
        weak = [h for h in git_grep(key) if h != arc and h not in refs]
        durable = durable_hits(key)
        sig = {"W": bool(refs), "D": bool(durable), "R": r["recent7d"] > 0,
               "M": key in MACHINE_SOURCE_DIRS}
        assets.append({"asset": arc, "kind": "temp_" + r["kind"],
                       "covered": covered_by_set(arc, set_paths),
                       "in_manifest": any(a == arc or a.startswith(arc + "/") for a in man_arcs),
                       "regenerable": is_regenerable(key, r["kind"]),
                       "signals": sig,
                       "evidence": {"wiring_qualified": refs[:4], "wiring_weak": weak[:3],
                                    "durable": durable[:3], "files": r["files"],
                                    "bytes": r["bytes"]}})
    other_key = "temp/（其余一次性产物）"
    assets.append({"asset": other_key, "kind": "temp_other",
                   "covered": False, "in_manifest": False, "regenerable": True,
                   "signals": {"W": False, "D": False, "R": True, "M": False},
                   "evidence": dict(covered_other)})

    # D. 仓外：LaunchAgents 作为一组
    if plists:
        sig = {"W": True, "D": bool(durable_hits("LaunchAgents")), "R": any(p["recent7d"] for p in plists),
               "M": False}
        assets.append({"asset": "~/Library/LaunchAgents", "kind": "outside_repo",
                       "covered": False, "in_manifest": False, "signals": sig,
                       "evidence": {"plists": len(plists),
                                    "recent7d": sum(1 for p in plists if p["recent7d"])}})
    return assets


# ---------------------------------------------------------------- 只读自证
def corpus_fingerprint(man_path: Path, plists=None):
    """指纹语料 = manifest + tools/*.py + ~/Library/LaunchAgents/*.plist
    （均为只读、非并行会话写入目标；memory/ 被其他会话持续写，故意排除）。"""
    fp = {}
    for p in [man_path] + sorted((ROOT / "tools").glob("*.py")):
        try:
            fp[str(p.relative_to(ROOT))] = sha256_file(p)
        except OSError:
            pass
    for name in sorted(os.listdir(Path.home() / "Library" / "LaunchAgents")):
        if name.endswith(".plist"):
            q = Path.home() / "Library" / "LaunchAgents" / name
            try:
                st = q.stat()
                fp["~/Library/LaunchAgents/" + name] = f"{st.st_size}:{int(st.st_mtime)}"
            except OSError:
                pass
    return fp


# ---------------------------------------------------------------- 输出
def md_table(rows, cols):
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="资产使用热度 × T0 备份 × tools 消费面 三向对账（只读）")
    ap.add_argument("--out", default="temp/usage_census", help="输出目录（唯一写入点）")
    ap.add_argument("--manifest", default=None, help="指定 T0 manifest（默认取最新）")
    ap.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    ap.add_argument("--selftest", action="store_true", help="纯函数自测（含负控）")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    t0 = load_t0_module()
    man_path = find_manifest(a.manifest)
    fp_before = corpus_fingerprint(man_path, None)

    t0_info = t0_axis(man_path, t0)
    tools_rows = tools_axis()
    temp_rows = temp_axis()
    plists = plist_axis()
    assets = assets_axis(t0_info, tools_rows, temp_rows, plists)
    machine = machine_evidence()

    # ---- 缺口分级 ----
    # G1 = 集合前缀已覆盖「但不在最新清单里」= 备份滞后（重跑 t0_snapshot 即可补齐）
    # G2 = 完全未纳入任何集合「从未纳管」；G2a=非可再生（真缺口）/ G2b=可判定可再生
    hot_all = [x for x in assets if is_high(x["signals"])]
    g1 = sorted([x for x in hot_all if x["covered"] and not x["in_manifest"]],
                key=lambda x: (-heat_tier(x["signals"]), x["asset"]))
    g2 = sorted([x for x in hot_all if not x["covered"]],
                key=lambda x: (-heat_tier(x["signals"]), x["asset"]))
    g2a = [x for x in g2 if not x.get("regenerable")]
    g2b = [x for x in g2 if x.get("regenerable")]
    hot = g1 + g2

    fp_after = corpus_fingerprint(man_path, None)
    changed = [k for k in sorted(set(fp_before) | set(fp_after))
               if fp_before.get(k) != fp_after.get(k)]

    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    # 列表
    with open(out / "assets.csv", "w", encoding="utf-8") as f:
        f.write("asset,kind,covered,in_manifest,regenerable,gap,tier,W,D,R,M\n")
        g1set = {x["asset"] for x in g1}
        g2set = {x["asset"] for x in g2}
        for x in sorted(assets, key=lambda y: (-heat_tier(y["signals"]), y["asset"])):
            s = x["signals"]
            gap = "G1" if x["asset"] in g1set else ("G2" if x["asset"] in g2set else "")
            f.write(f'"{x["asset"]}",{x["kind"]},{int(x["covered"])},{int(x["in_manifest"])},'
                    f'{int(bool(x.get("regenerable")))},{gap},'
                    f'{heat_tier(s)},{int(s["W"])},{int(s["D"])},{int(s["R"])},{int(s["M"])}\n')
    with open(out / "tools_census.csv", "w", encoding="utf-8") as f:
        f.write("tool,size,tracked,face,recent7d,code_n,cfg_n,durable_n,reports_n\n")
        for r in tools_rows:
            f.write(f'{r["tool"]},{r["size"]},{int(r["tracked"])},{r["face"]},{int(r["recent7d"])},'
                    f'{len(r["code"])},{len(r["cfg"])},{len(r["durable"])},{len(r["reports"])}\n')
    with open(out / "t0_delta.csv", "w", encoding="utf-8") as f:
        f.write("set,in_manifest,now,new_since_backup,gone_since_backup\n")
        for k, v in t0_info["delta"].items():
            f.write(f'{k},{v["in_manifest"]},{v["now"]},{len(v["new_since_backup"])},'
                    f'{len(v["gone_since_backup"])}\n')

    face_count = {}
    for r in tools_rows:
        face_count[r["face"]] = face_count.get(r["face"], 0) + 1

    lines = []
    lines.append("# 高热度但未进 T0 备份清单（自动生成，只读对账）\n")
    lines.append(f"判据：热度信号命中数 ≥ {HIGH_TIER}（W 接线 / D durable 记忆引用 / R 近 7 天活动 / "
                 f"M 机器使用数据承载）；生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')}；"
                 f"清单基座 = {t0_info['manifest']}（generated {t0_info['generated']}）。\n")
    lines.append("分级：G1=集合已覆盖但清单落后（重跑 `t0_snapshot` 即补）；"
                 "G2a=完全未纳管且非可再生（真缺口）；G2b=未纳管但判定可再生（提示级）。\n")
    for tag, rows, note in [("G1 集合内但清单落后", g1, "同批 t0_snapshot 重跑即可消除"),
                            ("G2a 未纳管·非可再生", g2a, "需人工决定是否纳入备份/归档"),
                            ("G2b 未纳管·判定可再生", g2b, "可从源再生，仅提示")]:
        lines.append(f"\n## {tag}（{len(rows)}）— {note}\n")
        lines.append(md_table(rows, ["asset", "kind", "in_manifest"]))
    lines.append("\n## 明细与证据\n")
    for x in hot:
        s = x["signals"]
        lines.append(f"### {x['asset']}  (tier={heat_tier(s)}: "
                     f"{''.join(k for k in 'WDRM' if s[k])})")
        lines.append(f"- 覆盖：covered_by_set={x['covered']} / in_manifest={x['in_manifest']}"
                     f" / regenerable={x.get('regenerable')}")
        lines.append(f"- 证据：`{json.dumps(x['evidence'], ensure_ascii=False)[:600]}`\n")
    with open(out / "hot_not_backed_up.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    summary = {
        "manifest": t0_info["manifest"], "generated": t0_info["generated"],
        "t0_total_files": t0_info["total_files"], "t0_sets": t0_info["sets"],
        "t0_delta": {k: {"in_manifest": v["in_manifest"], "now": v["now"],
                         "new": len(v["new_since_backup"]), "gone": len(v["gone_since_backup"])}
                     for k, v in t0_info["delta"].items()},
        "extra_dirs": t0_info["extra_dirs"],
        "tools_faces": face_count,
        "tools_total": len(tools_rows),
        "gap_g1_not_in_manifest": [x["asset"] for x in g1],
        "gap_g2a_unique": [x["asset"] for x in g2a],
        "gap_g2b_regenerable": [x["asset"] for x in g2b],
        "hot_not_backed_up": [x["asset"] for x in hot],
        "machine": machine,
        "readonly_proof": {"files": len(fp_before), "changed": changed,
                           "status": "PASS" if not changed else "CHANGED"},
    }
    with open(out / "census.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "assets": assets, "tools": tools_rows,
                   "t0_delta": t0_info["delta"]}, f, ensure_ascii=False, indent=1)

    with open(out / "README.txt", "w", encoding="utf-8") as f:
        f.write("复跑命令集（全部只读，唯一写入点=本目录）\n"
                "  1) 对账总表：python3 tools/usage_census.py --out temp/usage_census --json\n"
                "  2) 消费面原始证据：git -c core.quotepath=false grep -l -F -- \"tools/<name>\"\n"
                "  3) T0 清单基座：python3 tools/t0_snapshot.py list --temp-scan\n"
                "  4) 备份清单文件：temp/t0_backups/*.manifest.json（keys: arc/size/sha256）\n"
                "  5) 机器使用热度：sqlite3 'file:temp/kc_snapshots/<d>/knowledgeC.db?mode=ro' \"SELECT ...\"\n"
                "隐私边界：本目录清单含本机路径与统计量，仅在 temp/（gitignored）留存，不外发。\n")

    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print(f"[out] {out}")
        print(f"T0: {t0_info['total_files']} 文件 / sets={t0_info['sets']}")
        for k, v in t0_info["delta"].items():
            print(f"  {k}: manifest={v['in_manifest']} now={v['now']} "
                  f"new={len(v['new_since_backup'])} gone={len(v['gone_since_backup'])}")
        print(f"tools faces: {face_count}")
        print(f"缺口：G1(清单落后)={len(g1)} / G2a(未纳管·非可再生)={len(g2a)} / "
              f"G2b(未纳管·可再生)={len(g2b)}")
        for x in g1 + g2a:
            print(f"  - {x['asset']}  tier={heat_tier(x['signals'])}  "
                  f"kind={x['kind']}  gen={bool(x.get('regenerable'))}")
        print(f"readonly: {summary['readonly_proof']['status']} "
              f"(files={summary['readonly_proof']['files']}, changed={len(changed)})")
    return 0


# ---------------------------------------------------------------- selftest
def selftest():
    fails = []

    def eq(got, want, tag):
        if got != want:
            fails.append(f"{tag}: got={got!r} want={want!r}")

    # T1 消费面分类（含自指剔除）
    hits = ["tools/todo_quality_gate.py", "memory/global_mem_insight.txt",
            "memory/autonomous_reports/R417_x.md"]
    r = classify_face(hits + ["tools/dedup_guard.py"], "tools/dedup_guard.py")
    eq(r["face"], "接线", "T1a 有代码互引=接线")
    eq(r["code"], ["tools/todo_quality_gate.py"], "T1a code")
    eq(r["face_dup"] if "face_dup" in r else r["face"], "接线", "T1a dup")
    r2 = classify_face(["memory/global_mem_insight.txt", "memory/bg_task.py.note.md"], "tools/x.py")
    eq(r2["face"], "仅记忆引用", "T1b 只 durable=仅记忆引用")
    r3 = classify_face(["memory/autonomous_reports/R1.md"], "tools/x.py")
    eq(r3["face"], "仅报告引用", "T1c 只报告=仅报告引用")
    r4 = classify_face([], "tools/x.py")
    eq(r4["face"], "无引用", "T1d 零命中=无引用")
    r5 = classify_face(["tools/x.py"], "tools/x.py")  # 负控：只有自指
    eq(r5["face"], "无引用", "T1e 负控 自指不算引用")

    # T2 quotepath 保险：带引号路径必须被剥引号；git 命令必须显式关闭 quotepath
    raw = '"memory/autonomous_reports/R396_x.md"'
    eq(unquote_git_path(raw), "memory/autonomous_reports/R396_x.md", "T2a 去引号")
    eq(unquote_git_path("tools/x.py"), "tools/x.py", "T2b 无引号原样")
    eq(GIT[1:3], ["-c", "core.quotepath=false"], "T2c git 必带 quotepath=false")

    # T3 热度分档 + 负控
    eq(heat_tier({"W": True, "D": True, "R": False, "M": False}), 2, "T3a tier=2")
    eq(is_high({"W": True, "D": False, "R": False, "M": False}), False, "T3b 负控 tier1 非 HIGH")
    eq(is_high({"W": True, "D": True, "R": False, "M": False}), True, "T3c tier2=HIGH")

    # T4 集合覆盖判定（前缀 + glob）
    sp = [("memory", None), ("tools", None), ("temp/kc_snapshots", None),
          ("sche_tasks", ["*.json", "*.py"])]
    eq(covered_by_set("memory/x.txt", sp), True, "T4a memory 覆盖")
    eq(covered_by_set("temp/chrome_snapshots/2026-09-25/History", sp), False, "T4b 负控 chrome 未覆盖")
    eq(covered_by_set("sche_tasks/a.json", sp), True, "T4c sche_tasks json 覆盖")
    eq(covered_by_set("tools/sub/x.py", sp), True, "T4d tools 子目录覆盖")

    # T5 manifest delta（合成）
    man = {"files": [{"arc": "tools/a.py", "size": 1, "sha256": "x"}]}
    disk = {"tools/a.py", "tools/b.py"}
    arcs = {f["arc"] for f in man["files"]}
    eq(sorted(disk - arcs), ["tools/b.py"], "T5 新增识别")

    eq(len(fails), 0, "ALL")
    if fails:
        print("SELFTEST FAILED:")
        for x in fails:
            print("  -", x)
        return 1
    print("SELFTEST 5/5 groups PASS (T1 分类 5 例含 1 负控 / T2 路径 3 例 / T3 分档 3 例含 1 负控 "
          "/ T4 覆盖 4 例含 1 负控 / T5 delta 1 例)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
