#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reclaim_candidates.py — 「≥1GiB 且可再生」磁盘候选定位 + T0 保护清单对账（全只读，绝不删除）

背景（TODO 第15批·评审 7.00，三路均要求「只定位不回收、不与 R393 抢地盘」）：
  数据卷长期 97–98% 满。已有 tools/disk_attribution.py（全盘归因）与
  tools/cache_reclaim.py（可再生缓存，默认 dry-run）⇒ 本器**不重做归因**，
  只做既有工具没做的那件事：把「≥1GiB 的候选」按**可再生性**与**T0 保护面**
  逐条对齐，并给出「若批准合计可回收」的真值。

与既有工具的边界（重要）：
  * 候选表（20 条，id/档位/建议/重建方式）直接**复用** tools/cache_reclaim.py 的
    ITEMS（importlib 加载，零改写），本器只补它没覆盖的目录；
  * 日志回收（logs/，R393 实测可回收 1.46GiB）**只引用不重复计入**总量，
    单列 boundary="R393" 桶；
  * 本器**没有任何删除动作**：--apply 不存在。回收仍走既有批准语
    （`python3 tools/cache_reclaim.py --apply --yes`）与 R393 的
    `--apply --approve "批准回收"`。

口径（防误判）：
  * 体积一律 `du -k`（**已分配块**，非逻辑长度）——实测 OrbStack 的 data.img.raw
    逻辑 460.43 GiB、实占仅 14.01 GiB（稀疏文件）；用文件长度会假归因；
  * T0 保护面以**既有基线文件**（temp/t0_baseline.sha256，文件数当场读 manifest 自证，勿写死）为准：
    候选路径若等于某 T0 路径、或是其**祖先目录**，即判 PROTECTED_T0 ⇒ 硬拦；
  * 基线缺失/为空时**不得**报「无交集」，必须报 UNVERIFIABLE（没测到 ≠ 没有）；
  * 目录遍历会被 TCC 挂起 ⇒ 每个根独立超时，超时记 ERR，绝不把「没测到」当 0；
  * 无再生方式的目录一律 UNCLASSIFIED 且**不计入**可回收总量。

用法：
  python3 tools/reclaim_candidates.py probe [--json PATH] [--min-gib 1.0]
  python3 tools/reclaim_candidates.py analyze [--probe PATH] [--json-out PATH] [--md-out PATH]
  python3 tools/reclaim_candidates.py --selftest
  python3 tools/reclaim_candidates.py --readonly-proof
退出码：0 无违例；1 有违例（T0 相交 / 重复计入 / UNCLASSIFIED 混入总量）；2 用法或基线不可用。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
TEMP = os.path.join(ROOT, "temp")
DEFAULT_BASELINE = os.path.join(TEMP, "t0_baseline.sha256")
DEFAULT_PROBE = os.path.join(TEMP, "reclaim_probe.json")
MIN_GIB = 1.0
GiB = 1024 ** 3

# ── 扫描根（depth 用 du -k -d N；每根独立超时） ────────────────────────────────
SCAN_ROOTS = [
    ("home-develop",         f"{HOME}/develop",   2, 90),
    ("home-projects",        f"{HOME}/projects",  2, 90),
    ("home-Library-Developer", f"{HOME}/Library/Developer", 2, 60),
    ("home-Library-AppSupport", f"{HOME}/Library/Application Support", 2, 90),
    ("home-Library-Caches",  f"{HOME}/Library/Caches", 2, 90),
    ("home-Library-Containers", f"{HOME}/Library/Containers", 1, 25),
    ("home-Library-GroupContainers", f"{HOME}/Library/Group Containers", 1, 30),
    ("home-dotcache",        f"{HOME}/.cache",    2, 60),
    ("home-Library-Logs",    f"{HOME}/Library/Logs", 2, 45),
    ("home-Downloads",       f"{HOME}/Downloads", 1, 45),
    ("home-Documents",       f"{HOME}/Documents", 1, 45),
    ("home-Desktop",         f"{HOME}/Desktop",   1, 45),
    ("home-Movies",          f"{HOME}/Movies",    1, 30),
    ("home-Pictures",        f"{HOME}/Pictures",  1, 30),
    ("home-Music",           f"{HOME}/Music",     1, 30),
    ("home-Trash",           f"{HOME}/.Trash",    1, 45),
    ("home-dotdirs",         HOME,                1, 120),
    ("opt-homebrew",         "/opt/homebrew",     2, 60),
    ("orphan-OrbStack",      f"{HOME}/OrbStack",  1, 30),
    ("ga-temp",              f"{ROOT}/temp",      2, 90),
    ("ga-logs",              f"{ROOT}/logs",      2, 45),
    ("var-folders",          "/private/var/folders/vx", 2, 30),
]

# ── 新候选的已知再生方式（本器补 cache_reclaim 未覆盖的部分；键为绝对路径前缀） ──
# 说明：只写实测过「可重建/可再生」的项；不确定的一律留 UNCLASSIFIED。
KNOWN_NEW = [
    dict(prefix=f"{ROOT}/temp", klass="approval", advice="按批剪",
         rebuild="GA 运行期产物：快照/报告草稿/实验脚本；快照与待批台账属 T0，须逐目录判",
         cost="丢实验中间态（不可再生项由 T0 基线拦）"),
    dict(prefix=f"{HOME}/Library/Developer/Xcode/DerivedData", klass="safe", advice="删",
         rebuild="Xcode 下次构建自动重建", cost="重编译时间"),
    dict(prefix=f"{HOME}/Library/Developer/CoreSimulator/Devices", klass="approval", advice="剪",
         rebuild="`xcrun simctl delete unavailable` 清理不可用模拟器", cost="模拟器数据丢失"),
    dict(prefix=f"{HOME}/Library/Application Support/Caches", klass="safe", advice="删",
         rebuild="应用下次启动回填", cost="≈0"),
    dict(prefix=f"{HOME}/.cache", klass="approval", advice="逐项判",
         rebuild="各工具缓存按需回填（uv 见 cache_reclaim）", cost="重下依赖"),
    dict(prefix="/private/var/folders", klass="approval", advice="逐项判",
         rebuild="系统/应用临时文件，重启或退出应用后自动清理", cost="丢临时态"),
]


def human(nbytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(nbytes) < 1024 or unit == "TiB":
            return "%.2f %s" % (nbytes, unit)
        nbytes /= 1024.0


def norm(p: str) -> str:
    """路径归一：去尾斜杠、折叠双斜杠（不做 realpath，避免触发 TCC/网络盘）。"""
    p = os.path.normpath(p)
    return p.rstrip("/") or "/"


def is_under(child: str, parent: str) -> bool:
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p + os.sep)


# ── 复用既有候选表 ───────────────────────────────────────────────────────────
def load_cache_reclaim_items(path: str | None = None) -> list[dict]:
    """importlib 加载 tools/cache_reclaim.py 的 ITEMS（只读，零改写）。"""
    src = path or os.path.join(TOOLS, "cache_reclaim.py")
    spec = importlib.util.spec_from_file_location("_cr_for_reclaim", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 模块级只有常量与 lambda 定义，无副作用
    out = []
    for it in getattr(mod, "ITEMS", []):
        p = it.get("path")
        # path 可能是 glob(经 _pw())，只取确定目录
        if not p or "*" in p:
            continue
        out.append(dict(id=it.get("id"), path=norm(p), klass=it.get("klass", "unknown"),
                        advice=it.get("advice", ""), rebuild=it.get("rebuild", ""),
                        cost=it.get("cost", ""), note=it.get("note", ""), origin="cache_reclaim"))
    return out


def load_t0(baseline: str | None = None) -> tuple[set[str], str]:
    """返回 (T0 绝对路径集合, 状态)。状态 ∈ OK / MISSING / EMPTY。"""
    b = baseline or DEFAULT_BASELINE
    if not os.path.isfile(b):
        return set(), "MISSING"
    out = set()
    with open(b, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("  ", 1)
            if len(parts) != 2:
                continue
            out.add(norm(parts[1]))
    return out, ("OK" if out else "EMPTY")


# ── du 探针 ─────────────────────────────────────────────────────────────────
def parse_du(out: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        kb, path = parts
        try:
            kb = int(kb)
        except ValueError:
            continue
        rows.append(dict(kb=kb, path=norm(path)))
    return rows


def du_root(root: str, depth: int, timeout: int) -> dict:
    """du 单根扫描。★ rc≠0 但 stdout 有行时记 PARTIAL 并保留行（实测 ~/OrbStack 的
    docker 容器目录存在 symlink 环 ⇒ du 中途报错 rc=1，但它已打印的部分结果仍是真值，
    若整根丢弃会把 ~ 下所有 ≥1GiB 目录一起误判为「没有」）。-P 明示不跟随符号链接。"""
    try:
        r = subprocess.run(["du", "-k", "-d", str(depth), "-P", root],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(root=root, status="ERR", err="TIMEOUT>%ds" % timeout, rows=[])
    rows = parse_du(r.stdout)
    if r.returncode != 0:
        err = "rc=%d %s" % (r.returncode, r.stderr.strip()[:160])
        return dict(root=root, status=("PARTIAL" if rows else "ERR"), err=err, rows=rows)
    return dict(root=root, status="OK", rows=rows)


def cmd_probe(args) -> int:
    res = {}
    t_all = time.time()
    for name, root, depth, to in SCAN_ROOTS:
        t0 = time.time()
        r = du_root(root, depth, to)
        r.update(name=name, depth=depth, secs=round(time.time() - t0, 1))
        res[name] = r
        tot = sum(x["kb"] for x in r["rows"])
        big = [x for x in r["rows"] if x["kb"] >= MIN_GIB * 1024 * 1024]
        print("[%-4s] %-28s %-52s %7.2f GiB, %d dirs>=%.1fGiB (%.1fs)" % (
            r["status"], name, root, tot / 1048576.0, len(big), args.min_gib, r["secs"]), flush=True)
        for x in sorted(big, key=lambda y: -y["kb"]):
            print("          %8.2f GiB  %s" % (x["kb"] / 1048576.0, x["path"]), flush=True)
    payload = dict(generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                   kind="reclaim_candidates.probe/v1", min_gib=args.min_gib,
                   elapsed=round(time.time() - t_all, 1), roots=res)
    out = args.json or DEFAULT_PROBE
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("probe -> %s (%.1fs)" % (out, payload["elapsed"]))
    return 0


# ── 分析内核（纯函数，便于负控自测） ─────────────────────────────────────────
def classify(path: str, cr_items: list[dict]) -> dict:
    p = norm(path)
    for it in sorted(cr_items, key=lambda x: -len(x["path"])):
        if is_under(p, it["path"]):
            return dict(layer="cache_reclaim", id=it["id"], klass=it["klass"],
                        advice=it["advice"], rebuild=it["rebuild"], cost=it["cost"],
                        origin_path=it["path"], note=it.get("note", ""))
    for kn in sorted(KNOWN_NEW, key=lambda x: -len(x["prefix"])):
        if is_under(p, kn["prefix"]):
            return dict(layer="known_new", id=None, klass=kn["klass"], advice=kn["advice"],
                        rebuild=kn["rebuild"], cost=kn["cost"], origin_path=kn["prefix"], note="")
    return dict(layer="unknown", id=None, klass="unknown", advice="需人工判定", rebuild="",
                cost="", origin_path=None, note="无已知再生方式 ⇒ 不计入可回收总量")


def t0_protection(path: str, t0: set[str]) -> dict:
    """候选是否与 T0 保护面相交。返回 dict(protected, sample, count)。"""
    hits = [t for t in t0 if is_under(t, path)]
    # 候选自身就在 T0 里（=某 T0 根目录）也算相交
    return dict(protected=bool(hits), count=len(hits), sample=sorted(hits)[:3])


def violations_for(rows: list[dict], t0_status: str) -> list[dict]:
    """违例判定（口径 R429）。独立成函数 ⇒ 自测可直接构造「越权行」验证安全网。

    口径：候选目录**含** T0 保护文件本身不算违例 —— 数据目录天然如此
      （例：/Users/cuip 下住着 GA 仓库；~/projects 是仓库父目录）。
      只有「该候选被计入可回收总量」= 我们真的建议动它，却又含 T0 文件，才算违例。
      另：候选自身即 T0 成员（路径精确出现在基线清单里）恒为违例。
    """
    v = []
    for r in rows:
        if r["candidate_in_t0"]:
            v.append(dict(code="CANDIDATE_IS_T0_ITEM", path=r["path"],
                          detail="候选自身即 T0 保护面成员（不可动）", sample=[]))
        if r["protected_t0"] and r["counted"]:
            v.append(dict(code="COUNTED_CANDIDATE_HOLDS_T0", path=r["path"],
                          detail="被计入可回收总量，但其内部含 T0 保护文件 %d 项" % r["t0_hits"],
                          sample=r["t0_sample"]))
    if t0_status != "OK":
        v.append(dict(code="T0_UNVERIFIABLE", path="(baseline)",
                      detail="T0 基线状态=%s ⇒ 无法断言「候选 ∩ T0 = ∅」" % t0_status, sample=[]))
    for r in rows:
        if r["boundary"] == "R393" and r["counted"]:
            v.append(dict(code="R393_DOUBLE_COUNT", path=r["path"],
                          detail="日志项必须只引用不重复计入总量", sample=[]))
        if r["counted"] and r["klass"] not in ("safe", "approval"):
            v.append(dict(code="UNCLASSIFIED_IN_TOTAL", path=r["path"],
                          detail="无再生方式的目录不得计入可回收总量", sample=[]))
    return v


def analyze(probe: dict, cr_items: list[dict], t0: set[str], t0_status: str,
            min_gib: float = MIN_GIB) -> dict:
    """核心：候选抽取 → 分类 → T0 对账 → 去重/边界 → 合计。"""
    violations = []
    cands = []
    seen = set()
    err_roots = []
    for name, r in sorted(probe.get("roots", {}).items()):
        st = r.get("status")
        if st not in ("OK", "PARTIAL"):
            err_roots.append(dict(name=name, root=r.get("root"), err=r.get("err"), partial=False))
            continue
        if st == "PARTIAL":  # rc≠0 但拿到了部分行：行照用，同时显式记告警（不得静默）
            err_roots.append(dict(name=name, root=r.get("root"), err=r.get("err"), partial=True))
        for row in r.get("rows", []):
            if row["kb"] < min_gib * 1024 * 1024:
                continue
            p = norm(row["path"])
            if p in seen:
                continue
            seen.add(p)
            cands.append(dict(path=p, bytes=row["kb"] * 1024, root=name))

    rows = []
    for c in cands:
        info = classify(c["path"], cr_items)
        prot = t0_protection(c["path"], t0)
        rows.append(dict(path=c["path"], gib=round(c["bytes"] / GiB, 3), root=c["root"],
                         layer=info["layer"], id=info["id"], klass=info["klass"], advice=info["advice"],
                         rebuild=info["rebuild"], cost=info["cost"], note=info["note"],
                         boundary=("R393" if is_under(c["path"], os.path.join(ROOT, "logs")) else None),
                         subsumed_by=None, protected_t0=prot["protected"], t0_hits=prot["count"],
                         t0_sample=prot["sample"], candidate_in_t0=(c["path"] in t0),
                         t0_blocked=False, counted=False))

    # ── 计入判定（两阶段，R429） ──────────────────────────────────────────
    # 1) 子项只被「已被计入」的父项吞掉；父项自身不计入（unknown / 边界 / 含 T0）时，
    #    子项必须独立评估 —— 否则 GA temp（5.8GiB, approval）会因挂在 unknown 的
    #    /Users/cuip/projects 下而**凭空消失**（本轮实测踩到，两处口径都不收它）
    # 2) 候选内部含 T0 保护文件的不得整体计入总量（须逐目录判）⇒ 单列 t0_blocked
    rows.sort(key=lambda r: len(r["path"]))
    for i, r in enumerate(rows):
        parent = None
        for cand_p in reversed(rows[:i]):  # 按路径长度升序 ⇒ 倒序即由近及远找最近祖先
            if r["path"] != cand_p["path"] and is_under(r["path"], cand_p["path"]):
                parent = cand_p
                if cand_p["counted"]:  # 只被**已计入**的祖先吞掉（否则整条空间会凭空消失）
                    break
        if parent is not None and parent["counted"]:
            r["subsumed_by"] = parent["path"]
            continue
        if r["boundary"] is not None or r["klass"] not in ("safe", "approval"):
            continue
        if r["protected_t0"]:
            r["t0_blocked"] = True
            continue
        r["counted"] = True

    violations = violations_for(rows, t0_status)

    safe = [r for r in rows if r["counted"] and r["klass"] == "safe"]
    appr = [r for r in rows if r["counted"] and r["klass"] == "approval"]
    never = [r for r in rows if r["klass"] == "never" and not r.get("subsumed_by")]
    unk = [r for r in rows if r["klass"] == "unknown" and not r.get("subsumed_by")]
    blocked = [r for r in rows if r.get("t0_blocked")]
    logs_rows = [r for r in rows if r["boundary"] == "R393"]

    def s(items):
        return round(sum(x["gib"] for x in items), 2)

    return dict(kind="reclaim_candidates.report/v1",
                generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                min_gib=min_gib,
                t0=dict(status=t0_status, files=len(t0), baseline=DEFAULT_BASELINE),
                counts=dict(candidates=len(rows), counted=len(safe) + len(appr),
                            safe=len(safe), approval=len(appr), never=len(never),
                            unknown=len(unk), boundary_R393=len(logs_rows), err_roots=len(err_roots),
                            subsumed=len([r for r in rows if r.get("subsumed_by")]),
                            t0_blocked=len(blocked),
                            holds_t0=len([r for r in rows if r["protected_t0"]])),
                totals_gib=dict(safe=s(safe), approval=s(appr),
                                safe_plus_approval=round(s(safe) + s(appr), 2),
                                boundary_R393=s(logs_rows), unknown_not_counted=s(unk),
                                t0_blocked=s(blocked)),
                rows=rows, boundary_rows=logs_rows, never_rows=never, unknown_rows=unk,
                blocked_rows=blocked, err_roots=err_roots, violations=violations)


# ── 渲染 ────────────────────────────────────────────────────────────────────
def render_md(rep: dict) -> str:
    L = []
    A = L.append
    c = rep["counts"]
    t = rep["totals_gib"]
    A("# ≥1GiB 目录 × 可再生候选 × T0 保护面 对账（只读）")
    A("")
    A("- 生成时刻：%s ｜ 阈值：≥%.2f GiB ｜ T0 基线：%s（%d 文件，状态 %s）" % (
        rep["generated_at"], rep["min_gib"], rep["t0"]["baseline"], rep["t0"]["files"], rep["t0"]["status"]))
    A("- 候选 %d 条（计入 %d = safe %d + approval %d）｜已并入父项 %d ｜非候选 %d（unknown %d + never %d）｜R393 边界 %d ｜扫描失败根 %d" % (
        c["candidates"], c["counted"], c["safe"], c["approval"], c.get("subsumed", 0),
        c["unknown"] + c["never"], c["unknown"], c["never"], c["boundary_R393"], c["err_roots"]))
    A("- **若批准合计可回收 = %.2f GiB**（safe %.2f + approval %.2f）；另 R393 日志 %.2f GiB 单列不计入" % (
        t["safe_plus_approval"], t["safe"], t["approval"], t["boundary_R393"]))
    A("- 候选目录内含 T0 保护文件者 %d 条：仅提示、不计违例（数据目录天然如此，例：仓库父目录）；"
      "唯「被计入总量却又含 T0」或「候选自身即 T0 成员」才算违例" % c.get("holds_t0", 0))
    A("")

    counted = sorted([x for x in rep["rows"] if x["counted"]], key=lambda y: -y["gib"])
    A("## 1. 可再生候选（计入可回收总量，%d 条 / %.2f GiB）" % (len(counted), t["safe_plus_approval"]))
    A("")
    A("| # | 路径 | GiB | 档 | 来源层 | 再生方式 | 代价 | T0 |")
    A("|---|------|-----|----|--------|----------|------|----|")
    for i, r in enumerate(counted, 1):
        A("| %d | `%s` | %.2f | %s | %s | %s | %s | %s |" % (
            i, r["path"], r["gib"], r["klass"], r["layer"], r["rebuild"] or "-", r["cost"] or "-",
            ("含 %d 项⚠（未计入）" % r["t0_hits"]) if r["protected_t0"] else "不在内"))
    A("")

    subm = sorted([x for x in rep["rows"] if x.get("subsumed_by")], key=lambda y: -y["gib"])
    if subm:
        A("## 2. 已并入父项（不重复计入，%d 条）" % len(subm))
        A("")
        A("| # | 路径 | GiB | 档 | 被谁包含 | 再生方式 |")
        A("|---|------|-----|----|----------|----------|")
        for i, r in enumerate(subm, 1):
            A("| %d | `%s` | %.2f | %s | `%s` | %s |" % (
                i, r["path"], r["gib"], r["klass"], r["subsumed_by"], r["rebuild"] or "-"))
        A("")

    if rep["blocked_rows"]:
        A("## 2b. 可再生但含 T0 保护面 ⇒ 不整体计入（%d 条 / %.2f GiB）" % (
            len(rep["blocked_rows"]), t["t0_blocked"]))
        A("")
        A("| # | 路径 | GiB | 档 | 含 T0 项 | 处置 |")
        A("|---|------|-----|----|----------|------|")
        for i, r in enumerate(sorted(rep["blocked_rows"], key=lambda y: -y["gib"]), 1):
            A("| %d | `%s` | %.2f | %s | %d | 须逐目录拆分（整体删除会碰 T0） |" % (
                i, r["path"], r["gib"], r["klass"], r["t0_hits"]))
        A("")

    if rep["boundary_rows"]:
        A("## 3. R393 边界（只引用，不重复计入）")
        for r in rep["boundary_rows"]:
            A("- `%s` %.2f GiB — %s" % (r["path"], r["gib"], r["note"] or "日志回收见 R393 报告"))
        A("")

    noncand = sorted([x for x in rep["rows"] if x["klass"] in ("unknown", "never") and not x.get("subsumed_by")],
                     key=lambda y: -y["gib"])
    if noncand:
        A("## 4. 非候选（≥%.2f GiB，未计入总量）" % rep["min_gib"])
        A("")
        A("| # | 路径 | GiB | 档 | 说明 |")
        A("|---|------|-----|----|------|")
        for i, r in enumerate(noncand, 1):
            why = r["note"] or r["advice"] or "-"
            A("| %d | `%s` | %.2f | %s | %s |" % (i, r["path"], r["gib"], r["klass"], why))
        A("")
    if rep["never_rows"] and not noncand:
        A("## 4. never 档（保留，不碰）")
        for r in rep["never_rows"]:
            A("- `%s` %.2f GiB" % (r["path"], r["gib"]))
        A("")

    if rep["err_roots"]:
        A("## 5. 扫描告警（ERR=整根丢失 / PARTIAL=部分行已采用）")
        for e in rep["err_roots"]:
            A("- [%s] `%s` — %s" % ("PARTIAL" if e.get("partial") else "ERR", e["root"], e["err"]))
        A("")
    A("## 6. 违例")
    if not rep["violations"]:
        A("- 无（无一候选被计入总量却含 T0；无候选自身为 T0 成员；无重复计入；无未分类混入）")
    else:
        for v in rep["violations"]:
            A("- **%s** `%s` — %s" % (v["code"], v["path"], v["detail"]))
    return "\n".join(L) + "\n"


def cmd_analyze(args) -> int:
    probe_path = args.probe or DEFAULT_PROBE
    if not os.path.isfile(probe_path):
        print("probe 不存在：%s（先跑 `probe`）" % probe_path)
        return 2
    probe = json.load(open(probe_path, encoding="utf-8"))
    cr = load_cache_reclaim_items()
    t0, st = load_t0()
    rep = analyze(probe, cr, t0, st, args.min_gib)
    if args.json_out:
        json.dump(rep, open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("json ->", args.json_out)
    if args.md_out:
        open(args.md_out, "w", encoding="utf-8").write(render_md(rep))
        print("md   ->", args.md_out)
    md = render_md(rep)
    print(md if not (args.json_out or args.md_out) else md.split("## 违例")[0])
    print("=== 违例 %d ===" % len(rep["violations"]))
    for v in rep["violations"]:
        print(" [%s] %s — %s" % (v["code"], v["path"], v["detail"]))
    return 1 if rep["violations"] else 0


# ── 自测（含负控） ───────────────────────────────────────────────────────────
def _mkprobe(rows):
    return dict(kind="t", roots={"r1": dict(root="/x", status="OK", rows=[dict(kb=k, path=p) for k, p in rows])})


def cmd_selftest() -> int:
    ok = fail = 0
    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print("  PASS  %s" % name)
        else:
            fail += 1; print("  FAIL  %s" % name)

    cr = [dict(id="uv-cache", path=f"{HOME}/.cache/uv", klass="approval", advice="prune",
               rebuild="uv 回填", cost="重下", note="", origin="cache_reclaim"),
          dict(id="logs", path=f"{ROOT}/logs", klass="approval", advice="保留",
               rebuild="见 R393", cost="—", note="", origin="cache_reclaim")]
    t0 = {f"{ROOT}/memory/global_mem.txt", f"{ROOT}/tools/reclaim_candidates.py",
          f"{ROOT}/temp/kc_snapshots/2026-09-25/knowledgeC.db"}
    G = 1024 * 1024  # 1 GiB in KB

    # 1) 基本分类 + 计数
    rep = analyze(_mkprobe([(2 * G, f"{HOME}/.cache/uv"), (3 * G, f"{HOME}/Documents/mystery")]), cr, t0, "OK")
    chk("1 分类：既有项归 cache_reclaim", any(r["layer"] == "cache_reclaim" and r["klass"] == "approval" for r in rep["rows"]))
    chk("2 分类：未知目录 = unknown 且不计入", any(r["klass"] == "unknown" and not r["counted"] for r in rep["rows"]))
    chk("2b 负控：已登记目录必须计入（.cache 前缀生效）", any(r["path"].endswith("/uv") and r["counted"] for r in rep["rows"]))
    chk("3 无违例（干净输入）", not rep["violations"])

    # 2) <1GiB 过滤
    rep = analyze(_mkprobe([(0.9 * G, "/a"), (1.5 * G, "/b")]), cr, t0, "OK")
    chk("4 负控：<1GiB 被筛掉", [r["path"] for r in rep["rows"]] == ["/b"])

    # 3) T0 口径（R429 订正）：含 T0 保护文件不违例；「计入总量却含 T0」才违例
    rep = analyze(_mkprobe([(2 * G, f"{ROOT}/memory"), (2 * G, f"{ROOT}/tools")]), cr, t0, "OK")
    chk("5 口径：未知候选（未计入）含 T0 不算违例",
        not rep["violations"] and all(r["protected_t0"] for r in rep["rows"]))
    cr3 = cr + [dict(id="mem", path=f"{ROOT}/memory", klass="safe", advice="x", rebuild="r",
                     cost="c", note="", origin="cache_reclaim")]
    rep = analyze(_mkprobe([(2 * G, f"{ROOT}/memory")]), cr3, t0, "OK")
    chk("5b 负控：含 T0 的可再生候选 ⇒ 阻断计入（t0_blocked）而非混入总量",
        [r["t0_blocked"] for r in rep["rows"]] == [True] and rep["counts"]["counted"] == 0)
    # 安全网直测（该分支在 analyze 里被 t0_blocked 提前拦住 ⇒ 只能直接喂越权行验证）
    fake = [dict(candidate_in_t0=False, protected_t0=True, counted=True, t0_hits=3, t0_sample=[],
                 path="/x", boundary=None, klass="safe", subsumed_by=None)]
    chk("5b2 负控：越权行（计入∧含 T0）必报 COUNTED_CANDIDATE_HOLDS_T0",
        [v["code"] for v in violations_for(fake, "OK")] == ["COUNTED_CANDIDATE_HOLDS_T0"])
    rep = analyze(_mkprobe([(2 * G, f"{HOME}/.cache/uv")]), cr, t0 | {f"{HOME}/.cache/uv"}, "OK")
    chk("5c 负控：候选自身即 T0 成员必报 CANDIDATE_IS_T0_ITEM",
        any(v["code"] == "CANDIDATE_IS_T0_ITEM" for v in rep["violations"]))

    # 3b) 渲染覆盖（防"只列计入行"回归：真机曾只列 3 行，吞掉 8 条未分类大目录）
    rep = analyze(_mkprobe([(4 * G, f"{HOME}/.cache"), (2 * G, f"{HOME}/.cache/uv"),
                            (3 * G, f"{HOME}/Documents/mystery")]), cr, t0, "OK")
    md = render_md(rep)
    chk("5d 渲染：计入/已并入/非候选 三节齐全",
        "## 1. 可再生候选" in md and "## 2. 已并入父项" in md and "## 4. 非候选" in md)
    chk("5e 渲染：未分类大目录也出现在报告里（不得静默丢弃）", "/Documents/mystery" in md)
    chk("5f 渲染：已并入子项标出父路径", f"`{HOME}/.cache`" in md)

    # 4) T0 不可验证（缺失基线）
    rep = analyze(_mkprobe([(2 * G, f"{HOME}/.cache/uv")]), cr, set(), "MISSING")
    chk("6 负控：基线缺失报 UNVERIFIABLE 且 rc≠0", any(v["code"] == "T0_UNVERIFIABLE" for v in rep["violations"]))

    # 5) R393 日志不重复计入
    rep = analyze(_mkprobe([(1.8 * G, f"{ROOT}/logs")]), cr, t0, "OK")
    br = [r for r in rep["rows"] if r["boundary"] == "R393"]
    chk("7 边界：logs 单列且不计入总量", len(br) == 1 and not br[0]["counted"] and rep["totals_gib"]["boundary_R393"] == 1.8)
    chk("8 边界：R393 项未混入 safe/approval 合计", rep["totals_gib"]["safe_plus_approval"] == 0.0)

    # 6) 父子包含去重
    rep = analyze(_mkprobe([(4 * G, f"{HOME}/.cache"), (2 * G, f"{HOME}/.cache/uv")]), cr, t0, "OK")
    child = [r for r in rep["rows"] if r["path"].endswith("/uv")]
    chk("9 去重：子目录被父目录 subsumed 且不重复计入", child and child[0]["subsumed_by"] == f"{HOME}/.cache" and not child[0]["counted"])

    # 7) 同一路径重复出现只算一次
    rep = analyze(_mkprobe([(2 * G, "/dup"), (2 * G, "/dup")]), cr, t0, "OK")
    chk("10 去重：同路径重复行只保留一次", len(rep["rows"]) == 1)

    # 8) 扫描失败根必须显式列出，不当 0
    pr = dict(roots={"bad": dict(root="/nope", status="ERR", err="TIMEOUT>25s", rows=[])})
    rep = analyze(pr, cr, t0, "OK")
    chk("11 负控：扫描失败根记 ERR 不当 0", rep["counts"]["err_roots"] == 1 and rep["err_roots"][0]["err"].startswith("TIMEOUT"))

    # 8b) PARTIAL 根：行照用，但必须显式记告警（真机 ~/OrbStack symlink 环实测形态）
    pr = dict(roots={"partial": dict(root="/p", status="PARTIAL", err="rc=1 Can't follow symlink cycle", rows=[dict(kb=3 * G, path="/p/big")])})
    rep = analyze(pr, cr, t0, "OK")
    chk("11b PARTIAL：部分行仍计入且显式记告警",
        len(rep["rows"]) == 1 and rep["err_roots"] and rep["err_roots"][0]["partial"] is True)

    # 9) du 解析
    rows = parse_du("1048576\t/x/y\n4194304\t/x/z\nbogus\n")
    chk("12 du 解析：只收二元组行", [(r["kb"], r["path"]) for r in rows] == [(1048576, "/x/y"), (4194304, "/x/z")])

    # 10) 总量算术（手工对照）
    rep = analyze(_mkprobe([(2 * G, f"{HOME}/.cache/uv"), (3 * G, f"{HOME}/Library/Developer/Xcode/DerivedData")]), cr, t0, "OK")
    chk("13 总量算术：approval 2.0 + safe 3.0 = 5.0", rep["totals_gib"]["approval"] == 2.0 and rep["totals_gib"]["safe"] == 3.0 and rep["totals_gib"]["safe_plus_approval"] == 5.0)

    # 11) 未分类混入总量（构造：强行把 unknown 标 counted）
    r2 = analyze(_mkprobe([(2 * G, "/mystery")]), cr, t0, "OK")
    r2["rows"][0]["counted"] = True
    # 手工跑违例段（同 analyze 内逻辑）——用一次真实 analyze 的 unknown 行断言不计入
    chk("14 未分类不得计入总量", r2["totals_gib"]["safe_plus_approval"] == 0.0)

    # 12) never 档保留但列出
    cr2 = cr + [dict(id="m2", path=f"{HOME}/.m2", klass="never", advice="保留", rebuild="", cost="", note="", origin="cache_reclaim")]
    rep = analyze(_mkprobe([(4 * G, f"{HOME}/.m2")]), cr2, t0, "OK")
    chk("15 never 档列出但不计入可回收", len(rep["never_rows"]) == 1 and rep["totals_gib"]["safe_plus_approval"] == 0.0)

    # 13) 真实候选项复用既有工具（cache_reclaim ITEMS 必须被读进来）
    real = load_cache_reclaim_items()
    chk("16 复用：真实 cache_reclaim ITEMS 条数 >=15", len(real) >= 15)

    # 14) T0 基线真实存在且非空（本机真实语料自证）
    t0r, st = load_t0()
    chk("17 真实 T0 基线可读且 >1000 文件", st == "OK" and len(t0r) > 1000)

    # 15) 只读自证：分析不改 T0 摘要
    d1 = t0_digest(t0r)
    rep = analyze(_mkprobe([(2 * G, f"{HOME}/.cache/uv")]), cr, t0r, "OK")
    d2 = t0_digest(t0r)
    chk("18 只读：分析前后 T0 文件集摘要一致", d1 == d2)

    print("selftest: %d/%d" % (ok, ok + fail))
    return 0 if fail == 0 else 1


def t0_digest(t0: set[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(t0):
        h.update(p.encode("utf-8", "replace"))
    return h.hexdigest()


def cmd_readonly_proof(args) -> int:
    """只读自证：跑两遍分析，JSON 关键字段一致 + T0 基线摘要不变。"""
    probe_path = args.probe or DEFAULT_PROBE
    if not os.path.isfile(probe_path):
        print("probe 不存在：%s" % probe_path)
        return 2
    probe = json.load(open(probe_path, encoding="utf-8"))
    cr = load_cache_reclaim_items()
    t0, st = load_t0()
    b = hashlib.sha256(open(DEFAULT_BASELINE, "rb").read()).hexdigest() if os.path.isfile(DEFAULT_BASELINE) else ""
    r1 = analyze(probe, cr, t0, st, args.min_gib)
    b2 = hashlib.sha256(open(DEFAULT_BASELINE, "rb").read()).hexdigest() if os.path.isfile(DEFAULT_BASELINE) else ""
    r2 = analyze(probe, cr, t0, st, args.min_gib)
    k = lambda r: json.dumps(dict(rows=r["rows"], totals=r["totals_gib"], counts=r["counts"]), sort_keys=True)
    same = k(r1) == k(r2)
    print("baseline sha256 %s->%s %s" % (b[:12], b2[:12], "UNCHANGED" if b == b2 else "CHANGED"))
    print("analyze 两遍一致=%s ｜ T0 文件 %d（%s）" % (same, len(t0), st))
    return 0 if (b == b2 and same) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="≥1GiB 可再生候选定位 + T0 保护面对账（只读）")
    ap.add_argument("cmd", nargs="?", choices=["probe", "analyze"], help="子命令")
    ap.add_argument("--probe", help="probe JSON 路径（默认 temp/reclaim_probe.json）")
    ap.add_argument("--json", help="probe 输出路径")
    ap.add_argument("--json-out", help="分析结果 JSON 路径")
    ap.add_argument("--md-out", help="分析结果 Markdown 路径")
    ap.add_argument("--min-gib", type=float, default=MIN_GIB)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readonly-proof", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return cmd_selftest()
    if a.readonly_proof:
        return cmd_readonly_proof(a)
    if a.cmd == "probe":
        return cmd_probe(a)
    if a.cmd == "analyze":
        return cmd_analyze(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
