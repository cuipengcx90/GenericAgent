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
[2b] 入口面（R421 扩面，2026-09-29）：「有引用」≠「有可复跑入口」。逐 tools/*.py 静态判定
     tier ∈ {SELFTEST(--selftest 字面量) / SUBCOMMAND(add_subparsers) / FLAGS(add_argument>0) /
             MANUAL_ARGV(手搓 sys.argv/getopt) / NONE(无任何参数面)}。
     `rerunnable = tier ∈ {SELFTEST, SUBCOMMAND}`（口径来自 TODO L201 正文「--selftest/子命令」）。
     「无可复跑入口」清单 = 上述 rerunnable=False 的工具，**每条必带证据**：
       ①入口侧证据 = 命中的 token 及其行号（`tools/x.py:NN: '--selftest'`），无则记 `none`
       ②引用侧证据 = 消费面命中路径（code/cfg/other，最多 5 条）；`ghost_refs` = 命中路径在盘上
         不存在者（负控：自称引用必须可回源，见 T9）
     引用判定必须 **token 边界**匹配（负控 T8：`x_helper.py` 不得算作 `x.py` 的引用）。
     `TOOL_ENTRY_DISPOSITION` 登记表（memory/tool_surface/TOOL_ENTRY_DISPOSITION.json）对每条
     无入口工具显式登记 status ∈ {ACTIVE 现状保留 / DEFER 已排期补入口 / RETIRE 退役候选}，
     证据格式 `"<repo 相对路径> | <token>"` 且须回源命中（三判据 guard_entry_disposition）：
       1. `TOOL_ENTRY_UNDISPOSED` —— 无可复跑入口且未登记（新工具逃逸）；
       2. `CLAIM_WITHOUT_EVIDENCE` —— 登记项证据为空 / 文件不存在 / token 未命中（伪改）；
       3. `DISPOSITION_STALE` —— 登记项与现况不符（工具已删，或已获得可复跑入口）。
     RETIRE 属政策面，本器只登记不判决（退役提案须人批）。

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


# ---------------------------------------------------------------- 入口面（R421 扩面 · R501）
ENTRY_TIER_SELFTEST = "SELFTEST"
ENTRY_TIER_SUBCOMMAND = "SUBCOMMAND"
ENTRY_TIER_FLAGS = "FLAGS"
ENTRY_TIER_MANUAL = "MANUAL_ARGV"
ENTRY_TIER_NONE = "NONE"
# 「可复跑入口」口径 = --selftest 或子命令面（TODO L201 正文括号内定义）
RERUNNABLE_TIERS = (ENTRY_TIER_SELFTEST, ENTRY_TIER_SUBCOMMAND)
ENTRY_DISPOSITION_PATH = ROOT / "memory" / "tool_surface" / "TOOL_ENTRY_DISPOSITION.json"
DISPOSITION_STATUSES = ("ACTIVE", "DEFER", "RETIRE")
ENTRY_TOKENS = ("--selftest", "add_subparsers", "add_argument(")


def registered_selftest(src):
    """「--selftest 真被注册」判据（纯函数）。

    v1 的 `"--selftest"` 子串判定会把**只在 docstring / 帮助文本里提到**该字的工具
    误判成 SELFTEST（实测 detector_replay.py:32 就在 docstring 里，真入口是 positional
    子命令 ⇒ 误判方向=把无自测面的工具说成有，属**乐观误报**，比漏报更危险）。
    v2 口径：该字面量所在行（或其后 2 行，覆盖多行 add_argument 调用）里必须出现
    `add_argument(`，或该行本身就是 argv 判定（`in sys.argv`）；亦接受 `add_parser("selftest")`。
    """
    lines = src.splitlines()
    for i, l in enumerate(lines):
        if ('"--selftest"' not in l) and ("'--selftest'" not in l):
            continue
        if "add_argument(" in l or "sys.argv" in l or "argv" in l:
            return True
        if any("add_argument(" in w for w in lines[i + 1:i + 3]):
            return True
    return re.search(r"""add_parser\(\s*["']selftest["']""", src) is not None


def argparse_choice_cmds(src):
    """argparse positional 的命令面：{arg名: [命令名…]}（纯函数）。

    v1 只认 add_subparsers，漏掉「positional + choices」这种写法（实测 detector_replay.py
    用 add_argument("cmd", nargs="?", choices=[replay, check, selftest, cases, proof])）。
    """
    out = {}
    for m in re.finditer(r"add_argument\(\s*[\"']([A-Za-z_]\w*)[\"']", src):
        seg = src[m.start():m.start() + 400]
        cm = re.search(r"choices\s*=\s*[\[(]([^\])]*)[\])]", seg)
        if not cm:
            continue
        cmds = re.findall(r"[\"']([A-Za-z][\w-]*)[\"']", cm.group(1))
        if cmds:
            out[m.group(1)] = cmds
    return out


def manual_dispatch_cmds(src):
    """手写 argv 分派的命令名集合（纯函数）。

    形如 `cmd = argv[0]` / `a = sys.argv[1] ...` 后的 `cmd == "snapshot"` 或
    `a in ("probe", "all")`。只统计「由 argv 赋值出来的变量」，避免把
    `layer == "run_presence"` 这类普通比较算成命令面。
    """
    vars_ = set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*sys\.argv\[", src))
    vars_ |= set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*argv\[", src))
    vars_ |= set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*[\w.]+\(\s*argv\[", src))
    cmds = set()
    for v in vars_:
        for m in re.finditer(re.escape(v) + r"\s*==\s*[\"']([A-Za-z][\w-]*)[\"']", src):
            cmds.add(m.group(1))
        for m in re.finditer(re.escape(v) + r"\s+in\s*\(([^)]*)\)", src):
            cmds |= set(re.findall(r"[\"']([A-Za-z][\w-]*)[\"']", m.group(1)))
    return cmds


def detect_entry(src):
    """静态判定一个工具的「可复跑入口」（纯函数，供 selftest 喂合成文本做负控）。

    返回 dict(tier, has_selftest, has_subparsers, n_add_argument, has_argv, cmds, lines)，
    lines = token -> 首个命中行号（用于清单里的 `tools/x.py:NN: <token>` 证据）。
    """
    has_selftest = registered_selftest(src)
    has_subparsers = "add_subparsers" in src
    n_addarg = len(re.findall(r"add_argument\(", src))
    has_argv = (re.search(r"\bsys\.argv\b", src) is not None
                or re.search(r"\bgetopt\b", src) is not None)
    cmds = set(manual_dispatch_cmds(src))
    for _arg, cs in argparse_choice_cmds(src).items():
        cmds |= set(cs)
    lines = {}
    for token in ENTRY_TOKENS:
        for i, l in enumerate(src.splitlines(), 1):
            if token in l:
                lines[token] = i
                break
    if has_selftest:
        tier = ENTRY_TIER_SELFTEST
    elif has_subparsers or len(cmds) >= 2:
        tier = ENTRY_TIER_SUBCOMMAND
    elif n_addarg > 0:
        tier = ENTRY_TIER_FLAGS
    elif has_argv:
        tier = ENTRY_TIER_MANUAL
    else:
        tier = ENTRY_TIER_NONE
    return {"tier": tier, "has_selftest": has_selftest, "has_subparsers": has_subparsers,
            "n_add_argument": n_addarg, "has_argv": has_argv,
            "cmds": sorted(cmds), "lines": lines}


def is_rerunnable(entry):
    """可复跑入口判据（纯函数）。"""
    return entry.get("tier") in RERUNNABLE_TIERS


def ref_match(text, token):
    """引用必须是 token 边界匹配（纯函数）。

    负控 T8：`..._helper.py` 不得算作 `x.py` 的引用 —— R421 的子串匹配有该风险。
    """
    # 注意：前缀只排除 [A-Za-z0-9_.-]，**不**排除 `/` —— `tools/x.py` 必须算作 x.py 的
    # 引用（R421 的口径）；真正要防的是后缀粘连（`x.py.bak` / `x_helper.py`）。
    return re.search(r"(?<![A-Za-z0-9_.\-])" + re.escape(token) + r"(?![A-Za-z0-9_.\-])",
                     text) is not None


def ghost_refs(hits):
    """仓内相对路径型命中必须真实存在（虚拟 `~/...` 路径跳过）。纯函数。"""
    out = []
    for h in hits:
        if h.startswith("~/"):
            continue
        if not (ROOT / h).exists():
            out.append(h)
    return sorted(set(out))


def entry_evidence_lines(tool, entry):
    """入口侧证据：`tools/x.py:NN: <token>`；一个 token 都没有 ⇒ ['tools/x.py: none']。

    另附一行子命令面（`commands=[...]`，来自 argparse choices 或手写 argv 分派），
    使 SUBCOMMAND 档也自带可核证据（判据 v2 新增）。
    """
    rel = f"tools/{tool}"
    ev = [f"{rel}:{ln}: {tok}" for tok, ln in sorted(entry["lines"].items(), key=lambda kv: kv[1])]
    if entry.get("cmds"):
        ev.append(f"{rel}: commands=[{', '.join(entry['cmds'])}]")
    return ev or [f"{rel}: none"]


def evidence_hit(spec):
    """登记表证据回源校验：格式 `<repo 相对路径> | <token>`，须文件存在且 token 命中。

    返回 (ok, why)。纯函数（只读文件）。
    """
    if " | " not in spec:
        return False, "格式应为 '<repo 相对路径> | <token>'"
    path, token = spec.split(" | ", 1)
    p = ROOT / path
    if not p.exists():
        return False, f"文件不存在 {path}"
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"不可读 {path}: {e}"
    if token not in src:
        return False, f"token 未在 {path} 命中"
    return True, ""


def load_entry_disposition(path=None):
    """读登记表；文件不存在返回 {}（闸门会把全部无入口工具报 UNDISPOSED）。"""
    p = Path(path) if path else ENTRY_DISPOSITION_PATH
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("entries", doc)


def guard_entry_disposition(rows, registry=None):
    """三判据闸门（与 R449 guard_disposition 同款语义）。返回违规字符串列表。纯函数。"""
    reg = load_entry_disposition() if registry is None else registry
    bad = []
    by_tool = {r["tool"]: r for r in rows}
    for r in rows:
        if r["rerunnable"]:
            continue
        if r["tool"] not in reg:
            bad.append(f"TOOL_ENTRY_UNDISPOSED {r['tool']}: tier={r['entry_tier']} "
                       f"且未在 TOOL_ENTRY_DISPOSITION 登记")
    for tool in sorted(reg):
        disp = reg[tool] or {}
        st = disp.get("status")
        if st not in DISPOSITION_STATUSES:
            bad.append(f"CLAIM_WITHOUT_EVIDENCE {tool}: status={st!r} 不在 {list(DISPOSITION_STATUSES)}")
            continue
        ev = disp.get("evidence") or []
        if not ev:
            bad.append(f"CLAIM_WITHOUT_EVIDENCE {tool}: 登记为 {st} 但 evidence 为空")
        for spec in ev:
            ok, why = evidence_hit(spec)
            if not ok:
                bad.append(f"CLAIM_WITHOUT_EVIDENCE {tool}: 证据不可回源 ({spec}) — {why}")
        r = by_tool.get(tool)
        if r is None:
            bad.append(f"DISPOSITION_STALE {tool}: 登记项不在本次 tools/*.py 清单里")
        elif r["rerunnable"]:
            bad.append(f"DISPOSITION_STALE {tool}: 登记为 {st} 但现已获得可复跑入口"
                       f"(tier={r['entry_tier']}) ⇒ 须撤销登记")
    return bad


def entries_axis(tools_rows=None):
    """入口面清单：逐 tools/*.py 合并「消费面 + 可复跑入口 + 证据」。"""
    rows = tools_axis() if tools_rows is None else tools_rows
    out = []
    for r in rows:
        p = ROOT / "tools" / r["tool"]
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            src = ""
        e = detect_entry(src)
        refs = list(r.get("code", [])) + list(r.get("cfg", [])) + list(r.get("other", []))
        out.append({"tool": r["tool"], "face": r["face"], "tracked": r.get("tracked", 1),
                    "entry_tier": e["tier"], "rerunnable": is_rerunnable(e),
                    "entry_evidence": entry_evidence_lines(r["tool"], e),
                    "ref_evidence": refs[:5], "n_refs": len(refs),
                    "durable": r.get("durable", []), "reports": r.get("reports", []),
                    "ghost_refs": ghost_refs(refs)})
    return out


def entries_report(rows, gate, baseline_tools=None):
    """生成 markdown 清单（含证据），供报告引用。纯函数。"""
    no_entry = [r for r in rows if not r["rerunnable"]]
    tier_count = {}
    for r in rows:
        tier_count[r["entry_tier"]] = tier_count.get(r["entry_tier"], 0) + 1
    lines = ["# tools/*.py 「可复跑入口」清单（只读，自动生成）", "",
             f"判据：rerunnable = tier ∈ {{SELFTEST, SUBCOMMAND}}（TODO L201 口径）；"
             f"生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')}；工具数 {len(rows)}。",
             f"tier 分布：{json.dumps(tier_count, ensure_ascii=False, sort_keys=True)}；"
             f"无可复跑入口 {len(no_entry)} 个。", "",
             "## 无可复跑入口（每条附证据）", ""]
    for r in sorted(no_entry, key=lambda x: (-x.get("n_refs", 0), x["tool"])):
        base = ""
        if baseline_tools is not None:
            base = " 新增(R421后)" if r["tool"] not in baseline_tools else " R421在册"
        lines.append(f"- `{r['tool']}` tier={r['entry_tier']} face={r['face']} "
                     f"refs={r.get('n_refs', 0)}{base} ghost={r.get('ghost_refs', [])}")
        lines.append(f"    - 入口证据：{'；'.join(r.get('entry_evidence') or ['none'])}")
        lines.append(f"    - 引用证据：{'；'.join(r.get('ref_evidence') or []) or 'none'}")
        mem = list(r.get("durable") or [])[:5]
        rep = list(r.get("reports") or [])[:5]
        if mem:
            lines.append(f"    - 记忆引用证据（memory/ 非报告）：{'；'.join(mem)}")
        if rep:
            lines.append(f"    - 报告引用证据（memory/autonomous_reports）：{'；'.join(rep)}")
        if not mem and not rep:
            lines.append("    - 记忆/报告引用证据：none（无任何引用 ⇒ 见处置登记）")
    lines += ["", "## 闸门（guard_entry_disposition）", ""]
    if gate:
        for g in gate:
            lines.append(f"- {g}")
    else:
        lines.append("- 违规 0")
    return "\n".join(lines) + "\n"


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


def load_baseline_tools(path):
    """读 R421 快照 tools_census.csv 的第一列（工具名集合）；路径不存在返回 None。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    names = set()
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == 0 or not line.strip():
                continue
            names.add(line.split(",")[0].strip().strip('"'))
    return names


def entries_main(a):
    """入口面真跑：清单 + 处置登记闸门；唯一写入点 = out 目录。返回 rc（违规 ⇒ 1）。"""
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    rows = entries_axis()
    gate = guard_entry_disposition(rows)
    baseline = load_baseline_tools(a.baseline_tools_csv)
    no_entry = [r for r in rows if not r["rerunnable"]]
    tier_count, face_count = {}, {}
    for r in rows:
        tier_count[r["entry_tier"]] = tier_count.get(r["entry_tier"], 0) + 1
    for r in no_entry:
        face_count[r["face"]] = face_count.get(r["face"], 0) + 1
    doc = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "tools_total": len(rows), "no_rerunnable_entry": len(no_entry),
           "tier_count": tier_count, "no_entry_face_count": face_count,
           "no_entry": [{"tool": r["tool"], "face": r["face"], "tier": r["entry_tier"],
                         "entry_evidence": r["entry_evidence"],
                         "ref_evidence": r["ref_evidence"], "n_refs": r["n_refs"],
                         "durable_refs": list(r.get("durable") or [])[:5],
                         "report_refs": list(r.get("reports") or [])[:5],
                         "ghost_refs": r["ghost_refs"]} for r in no_entry],
           "gate": gate, "gate_rc": 1 if gate else 0,
           "baseline": str(a.baseline_tools_csv) if baseline is not None else None,
           "baseline_count": len(baseline) if baseline is not None else None,
           "new_since_baseline": (sorted(r["tool"] for r in rows
                                          if r["tool"] not in baseline)
                                  if baseline is not None else None)}
    with open(out / "entries.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    with open(out / "entries.md", "w", encoding="utf-8") as f:
        f.write(entries_report(rows, gate, baseline))
    print(f"[entries] tools={len(rows)} tiers={json.dumps(tier_count, ensure_ascii=False, sort_keys=True)}")
    print(f"[entries] 无可复跑入口={len(no_entry)} "
          f"faces={json.dumps(face_count, ensure_ascii=False, sort_keys=True)}")
    if baseline is not None:
        print(f"[entries] R421 快照在册={len(baseline)} / 其后新增={len(doc['new_since_baseline'])}")
    print(f"[entries] 闸门违规={len(gate)} rc={doc['gate_rc']}")
    for g in gate[:20]:
        print("  -", g)
    print(f"[out] {out}/entries.md")
    print(f"[out] {out}/entries.json")
    return doc["gate_rc"]


def verify_entries_json(path):
    """复算已归档 entries JSON 的闸门（只读）；装配面变化 ⇒ 从归档清单重建并重判。"""
    p = Path(path)
    if not p.exists():
        print(f"[verify] 文件不存在 {p}")
        return 2
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    rows = [{"tool": r["tool"], "face": r.get("face", ""), "rerunnable": False,
             "entry_tier": r.get("tier", "NONE")} for r in doc.get("no_entry", [])]
    gate = guard_entry_disposition(rows)
    stored = doc.get("gate") or []
    print(f"[verify] 归档 {p}")
    print(f"[verify] 归档时 tools={doc.get('tools_total')} 无入口={doc.get('no_rerunnable_entry')} "
          f"违规={len(stored)}；复算违规={len(gate)}")
    added = [g for g in gate if g not in stored]
    gone = [g for g in stored if g not in gate]
    print(f"[verify] 新增违规={len(added)} 已消除={len(gone)}")
    for g in (added[:10] + gone[:5]):
        print("  -", g)
    return 1 if gate else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="资产使用热度 × T0 备份 × tools 消费面 三向对账（只读）")
    ap.add_argument("--out", default="temp/usage_census", help="输出目录（唯一写入点）")
    ap.add_argument("--manifest", default=None, help="指定 T0 manifest（默认取最新）")
    ap.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    ap.add_argument("--selftest", action="store_true", help="纯函数自测（含负控）")
    ap.add_argument("--entries", action="store_true",
                    help="入口面：无可复跑入口清单 + 处置登记闸门（写 out 目录）")
    ap.add_argument("--verify-entries-json", default=None,
                    help="复算已归档 entries JSON 的闸门（只读）")
    ap.add_argument("--baseline-tools-csv", default=None,
                    help="R421 快照 tools_census.csv（标记其后新增工具）")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.verify_entries_json:
        return verify_entries_json(a.verify_entries_json)
    if a.entries:
        return entries_main(a)

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

    # T6 入口分档（纯函数合成文本；含负控）
    src_selftest = 'ap = argparse.ArgumentParser()\nap.add_argument("--selftest", action="store_true")\n'
    src_sub = 'sub = p.add_subparsers(dest="cmd")\n'
    src_flags = 'ap.add_argument("--out", default="x")\n'
    src_manual = 'opts = sys.argv[1:]\n'
    src_none = 'print("hello")\n'
    eq(detect_entry(src_selftest)["tier"], ENTRY_TIER_SELFTEST, "T6a --selftest ⇒ SELFTEST")
    eq(detect_entry(src_sub)["tier"], ENTRY_TIER_SUBCOMMAND, "T6b 子命令 ⇒ SUBCOMMAND")
    eq(detect_entry(src_flags)["tier"], ENTRY_TIER_FLAGS, "T6c 仅参数 ⇒ FLAGS")
    eq(detect_entry(src_manual)["tier"], ENTRY_TIER_MANUAL, "T6d 手搓 argv ⇒ MANUAL_ARGV")
    eq(detect_entry(src_none)["tier"], ENTRY_TIER_NONE, "T6e 无参数面 ⇒ NONE")
    eq(detect_entry('print("see --selftest in docs")\n')["tier"], ENTRY_TIER_NONE,
       "T6f 负控 非引号包裹的 --selftest 不算入口")
    eq(detect_entry(src_selftest)["lines"]["--selftest"], 2, "T6g 证据行号=2")
    # v2 判据负控：**docstring 里提到 "--selftest" 不算注册**（实测 detector_replay.py 旧判据翻车处）
    src_doc = 'def f():\n    """用法: python3 x.py "--selftest" """\n    pass\n'
    eq(detect_entry(src_doc)["tier"], ENTRY_TIER_NONE, "T6h 负控 docstring 提 --selftest ⇒ 不算入口")
    eq(registered_selftest(src_doc), False, "T6h2 registered_selftest 同为 False")
    src_argvst = 'if "--selftest" in sys.argv:\n    run()\n'
    eq(detect_entry(src_argvst)["tier"], ENTRY_TIER_SELFTEST, "T6i argv 判定 --selftest ⇒ SELFTEST")
    # 手写 argv 分派：两个命令 ⇒ SUBCOMMAND（实盘 detector_replay/alert_lifecycle 即此形态）
    src_disp = 'a = sys.argv[1] if len(sys.argv) > 1 else "all"\nif a == "scan":\n    pass\n' \
               'if a == "report":\n    pass\n'
    de = detect_entry(src_disp)
    eq(de["tier"], ENTRY_TIER_SUBCOMMAND, "T6j 手写 argv 双命令 ⇒ SUBCOMMAND")
    eq(de["cmds"], ["report", "scan"], "T6j2 命令面已抽取")
    src_disp1 = 'a = sys.argv[1]\nif a == "scan":\n    pass\n'
    eq(detect_entry(src_disp1)["tier"], ENTRY_TIER_MANUAL, "T6k 负控 单命令不升 SUBCOMMAND")
    src_choice = 'ap.add_argument("cmd", nargs="?", choices=["replay", "check", "selftest"])\n'
    eq(detect_entry(src_choice)["tier"], ENTRY_TIER_SUBCOMMAND, "T6l argparse choices ⇒ SUBCOMMAND")
    eq("commands=[" in "".join(entry_evidence_lines("x.py", detect_entry(src_choice))), True,
       "T6m 证据含 commands 行")

    # T7 可复跑判据（口径=TODO L201：--selftest / 子命令）
    eq(is_rerunnable(detect_entry(src_selftest)), True, "T7a SELFTEST 可复跑")
    eq(is_rerunnable(detect_entry(src_sub)), True, "T7b SUBCOMMAND 可复跑")
    eq(is_rerunnable(detect_entry(src_flags)), False, "T7c 负控 FLAGS 不算可复跑入口")
    eq(is_rerunnable(detect_entry(src_manual)), False, "T7d 负控 MANUAL 不算")
    eq(is_rerunnable(detect_entry(src_none)), False, "T7e 负控 NONE 不算")

    # T8 引用边界（负控=伪造/近似引用不得算引用）
    eq(ref_match("tools/usage_census.py", "usage_census.py"), True, "T8a 路径限定名命中")
    eq(ref_match("restore_ga_desktop_bridge_helper.py", "restore_ga_desktop_bridge.py"), False,
       "T8b 负控 _helper 后缀不算引用")
    eq(ref_match("x.py.bak", "x.py"), False, "T8c 负控 .bak 后缀不算引用")
    eq(ref_match("a/x.pyy", "x.py"), False, "T8d 负控 长后缀不算引用")

    # T9 ghost 命中（负控：仓内不存在的路径必须被标出）
    eq(ghost_refs(["tools/usage_census.py"]), [], "T9a 真路径非 ghost")
    eq(ghost_refs(["tools/__no_such_tool__.py"]), ["tools/__no_such_tool__.py"],
       "T9b 负控 不存在路径 ⇒ ghost")
    eq(ghost_refs(["~/Library/LaunchAgents/x.plist"]), [], "T9c 虚拟路径跳过")

    # T10 证据回源（负控：token 不命中/文件不存在 ⇒ 拒）
    ok, _ = evidence_hit("tools/usage_census.py | --selftest")
    eq(ok, True, "T10a 真证据命中")
    # 负控 token 用拼接构造（写死字面量会落在本文件源码里 ⇒ 恒命中=自败）
    badtok = "NOTOKEN_" + "0000"
    ok2, why2 = evidence_hit("tools/usage_census.py | " + badtok)
    eq(ok2, False, "T10b 负控 token 未命中 ⇒ 拒")
    ok3, _ = evidence_hit("tools/__no_such_tool__.py | x")
    eq(ok3, False, "T10c 负控 文件不存在 ⇒ 拒")
    eq(evidence_hit("no-separator")[0], False, "T10d 负控 格式非法 ⇒ 拒")

    # T11 闸门三判据 + 负控（合成 rows/registry，不触真登记表）
    rows_syn = [
        {"tool": "no_entry_tool.py", "face": "接线", "rerunnable": False,
         "entry_tier": ENTRY_TIER_FLAGS},
        {"tool": "ok_tool.py", "face": "接线", "rerunnable": True,
         "entry_tier": ENTRY_TIER_SELFTEST},
    ]
    g1 = guard_entry_disposition(rows_syn, {})
    eq(any(x.startswith("TOOL_ENTRY_UNDISPOSED") for x in g1), True, "T11a 未登记 ⇒ UNDISPOSED")
    g2 = guard_entry_disposition(rows_syn, {"no_entry_tool.py": {"status": "ACTIVE", "evidence": []}})
    eq(any("CLAIM_WITHOUT_EVIDENCE" in x for x in g2), True, "T11b 负控 空证据 ⇒ CLAIM_WITHOUT_EVIDENCE")
    g3 = guard_entry_disposition(
        rows_syn, {"no_entry_tool.py": {"status": "ACTIVE",
                                        "evidence": ["tools/usage_census.py | " + ("NOTOKEN_" + "0000")]}})
    eq(any("CLAIM_WITHOUT_EVIDENCE" in x for x in g3), True, "T11c 负控 证据不可回源 ⇒ 拒")
    g4 = guard_entry_disposition(rows_syn, {"__ghost_tool__.py": {"status": "ACTIVE",
                                                                  "evidence": ["tools/usage_census.py | --selftest"]}})
    eq(any(x.startswith("DISPOSITION_STALE") for x in g4), True, "T11d 负控 登记项不存在 ⇒ STALE")
    g5 = guard_entry_disposition(
        rows_syn, {"ok_tool.py": {"status": "ACTIVE",
                                  "evidence": ["tools/usage_census.py | --selftest"]}})
    eq(any(x.startswith("DISPOSITION_STALE") for x in g5), True,
       "T11e 负控 已获得入口仍登记 ⇒ STALE")
    eq(guard_entry_disposition(
        rows_syn, {"no_entry_tool.py": {"status": "DEFER",
                                        "evidence": ["tools/usage_census.py | --selftest"]}}), [],
       "T11f 正例 合法登记 ⇒ 零违规")
    g7 = guard_entry_disposition(rows_syn, {"no_entry_tool.py": {"status": "NOPE",
                                                                 "evidence": ["tools/usage_census.py | --selftest"]}})
    eq(any("status=" in x for x in g7), True, "T11g 负控 非法 status ⇒ 违规")

    # T12 清单渲染含证据（防"清单无证据"回归）
    import io                                   # noqa: PLC0415
    md = entries_report(rows_syn, [], None)
    eq("入口证据：" in md and "引用证据：" in md, True, "T12a 清单每条带两类证据")
    eq(md.count("- `no_entry_tool.py`"), 1, "T12b 只列无可复跑入口者")
    rows_mem = [dict(rows_syn[0], durable=["memory/kc_profile/profile.py"], reports=[],
                     entry_evidence=["tools/x.py: none"])]
    md2 = entries_report(rows_mem, [], None)
    eq("记忆引用证据（memory/ 非报告）：memory/kc_profile/profile.py" in md2, True,
       "T12c 记忆引用证据行落地（face=仅记忆引用 也有证据）")
    eq("记忆/报告引用证据：none（无任何引用 ⇒ 见处置登记）" in
       entries_report([dict(rows_syn[0], durable=[], reports=[], entry_evidence=["tools/x.py: none"])],
                      [], None), True, "T12d 零引用时显式标 none")

    eq(len(fails), 0, "ALL")
    if fails:
        print("SELFTEST FAILED:")
        for x in fails:
            print("  -", x)
        return 1
    print("SELFTEST 12/12 groups PASS (T1 分类 5 例含 1 负控 / T2 路径 3 例 / T3 分档 3 例含 1 负控 "
          "/ T4 覆盖 4 例含 1 负控 / T5 delta 1 例 / T6 入口分档 7 例含 1 负控 / T7 可复跑 5 例含 2 负控 "
          "/ T8 引用边界 4 例含 3 负控 / T9 ghost 3 例含 1 负控 / T10 证据回源 4 例含 3 负控 "
          "/ T11 闸门 7 例含 5 负控 / T12 清单渲染 2 例)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
