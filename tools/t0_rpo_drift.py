#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T0 备份 RPO 与漂移判定器（只读；R533 / TODO L221，收口 R444 D2）。

问题（R444 D2）：恢复演练只答「能否回到某个时点」，不答「今天的盘面还有多少没被这份归档覆盖」。
本器把「归档快照点之后又被改动的受保护文件」做成机械判据，输出漂移清单与**有效 RPO（小时）**。

复用（禁重写 tar/manifest/baseline/sha 内核，逐项来自 tools/backup_restore_drill.py）：
  sha256_file / load_sums / load_manifest_sums / load_baseline / safe_member_name /
  tier_of / pick_default_archive
（tar 成员枚举沿用同一内核的 `tarfile` 读取面与 `safe_member_name` 过滤口径，未另起解析器。）

分类（逐条归档成员）：
  SAME                live 内容 sha == 归档记录 sha（**只看内容，不看 mtime**）
  DRIFT               live sha != 归档 sha 且 live mtime > 快照点 → RPO 违规
  DRIFT_PRE_SNAPSHOT  live sha != 归档 sha 但 live mtime <= 快照点 → 不判 RPO 违规（归档内容异常/时钟偏差）
  LIVE_MISSING        盘上已无该文件（删除本身未被归档覆盖）→ RPO 违规（mtime 不可得）
  NO_ARCHIVE_RECORD   归档无该成员的 sha 记录（manifest/SHA256SUMS 缺）→ **不判 SAME**

保护面侧（temp/t0_baseline.sha256，口径同 R444 D1）：
  ARCHIVE_MISSING     在 T0 保护面但不在归档成员表 → 判「归档缺失」（**绝不判 SAME**）
  covered             同时在保护面与归档成员表（唯一能判漂移的子集）
  口径铁律：本器只对 covered 子集给漂移/RPO 判决；保护面其余条目只报覆盖洞，不推测其内容。

有效 RPO（小时）= (now - 最早未备份修改时间) / 3600；无违规时为 0。
  「最早未备份修改时间」= 归档快照点之后被改动的文件里，最早的 live mtime。
  另给 backup_age_hours = (now - 快照点)/3600 作参照（归档本身有多旧）。

退出码：0 无 RPO 违规 / 1 存在 RPO 违规 / 2 输入错误（归档缺、空归档、基线缺）。
  --strict-coverage 时，保护面覆盖洞（ARCHIVE_MISSING）也计入 rc=1。
只读：不修改任何源文件；归档与基线 sha256 前后一致自证；产物只落 --out（默认 temp/，gitignore）。

子命令（位置参数）：scan / proof / selftest
  python3 tools/t0_rpo_drift.py scan [--archive X] [--baseline Y] [--root R] [--json] [--out DIR]
  python3 tools/t0_rpo_drift.py proof [--archive X] [--baseline Y]
  python3 tools/t0_rpo_drift.py selftest
"""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GA_ROOT = os.path.dirname(HERE)
KERNEL_PATH = os.path.join(HERE, "backup_restore_drill.py")
DEFAULT_ARCHIVE_GLOB_DIR = os.path.join(GA_ROOT, "temp", "t0_backups")
DEFAULT_BASELINE = os.path.join(GA_ROOT, "temp", "t0_baseline.sha256")
DEFAULT_OUT_DIR = os.path.join(GA_ROOT, "temp", "t0_rpo_drift")

# 复用清单：内核必须提供的接口（缺一即 fail-closed，防止「假复用」）
REUSED_KERNEL_API = ("sha256_file", "load_sums", "load_manifest_sums", "load_baseline",
                     "safe_member_name", "tier_of", "pick_default_archive")

AXIS_SAME = "SAME"
AXIS_DRIFT = "DRIFT"
AXIS_DRIFT_PRE = "DRIFT_PRE_SNAPSHOT"
AXIS_DRIFT_MISSING = "LIVE_MISSING"
AXIS_NO_RECORD = "NO_ARCHIVE_RECORD"


# --------------------------------------------------------------------------- #
# 内核装载
# --------------------------------------------------------------------------- #
def load_kernel(path: str = KERNEL_PATH):
    """按路径装载 R444 内核模块；缺接口即抛错（禁静默降级成自写解析）。"""
    if not os.path.exists(path):
        raise RuntimeError(f"内核不存在: {path}")
    spec = importlib.util.spec_from_file_location("ga_t0_kernel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = [n for n in REUSED_KERNEL_API if not hasattr(mod, n)]
    if missing:
        raise RuntimeError(f"内核缺少被复用接口: {missing}")
    return mod


def kernel_api_report(kernel) -> dict:
    """自证「真复用」：逐项打印来源函数所属文件与行号。"""
    out = {}
    for name in REUSED_KERNEL_API:
        fn = getattr(kernel, name)
        src_file = getattr(getattr(fn, "__code__", None), "co_filename", "?")
        src_line = getattr(getattr(fn, "__code__", None), "co_firstlineno", "?")
        out[name] = {"file": src_file, "line": src_line}
    return out


# --------------------------------------------------------------------------- #
# 时间口径
# --------------------------------------------------------------------------- #
def parse_ts(text: str):
    """把 manifest 的 generated 文本转成本地 epoch 秒；不可解析返回 None。"""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(str(text).strip(), fmt))
        except Exception:  # noqa: BLE001
            continue
    return None


def iso(ts) -> str:
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def resolve_snapshot_ts(manifest_path: str, archive_path: str):
    """归档快照点：优先 manifest.generated（权威），回退归档文件 mtime（旁证）。"""
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = json.load(f)
            ts = parse_ts(m.get("generated"))
            if ts:
                return ts, "manifest.generated"
        except Exception:  # noqa: BLE001
            pass
    if archive_path and os.path.exists(archive_path):
        return os.path.getmtime(archive_path), "archive.mtime"
    return None, "none"


# --------------------------------------------------------------------------- #
# 归档成员枚举（沿用内核 tarfile 读取面 + safe_member_name 过滤口径）
# --------------------------------------------------------------------------- #
def archive_members(archive: str, kernel):
    """返回 (普通文件成员名列表, 非普通文件成员数)。"""
    files, others = [], 0
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf.getmembers():
            if not kernel.safe_member_name(m.name):
                continue
            if m.isfile():
                files.append(m.name)
            else:
                others += 1
    return files, others


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #
def scan(archive: str = "", baseline_path: str = "", root: str = "",
         limit: int = 20, now: float = None, quiet: bool = False) -> dict:
    t0 = time.time()
    root = root or GA_ROOT
    now = float(now) if now is not None else time.time()
    res = {"archive": archive, "baseline": baseline_path, "root": root,
           "generated": iso(now), "now": now, "counts": {}, "items": [],
           "rpo_violations": [], "effective_rpo_hours": 0.0, "backup_age_hours": 0.0,
           "earliest_unbacked_ts": None, "baseline_coverage": {}, "findings": [],
           "kernel_api": {}, "error": None}
    try:
        kernel = load_kernel()
    except Exception as e:  # noqa: BLE001
        res["error"] = f"内核装载失败: {type(e).__name__}: {e}"
        return res
    res["kernel_api"] = kernel_api_report(kernel)

    if not archive or not os.path.exists(archive):
        res["error"] = f"归档不存在: {archive}"
        return res
    if not baseline_path or not os.path.exists(baseline_path):
        res["error"] = f"基线不存在: {baseline_path}"
        return res

    arc_sha_before = kernel.sha256_file(archive)
    base_sha_before = kernel.sha256_file(baseline_path)

    names, others = archive_members(archive, kernel)
    res["non_file_members"] = others
    if not names:
        res["error"] = "空归档（0 个普通文件成员）"
        return res

    sums = kernel.load_sums(archive + ".SHA256SUMS")
    sums.update({k: v for k, v in kernel.load_manifest_sums(archive + ".manifest.json").items()
                 if k not in sums})
    baseline = kernel.load_baseline(baseline_path)
    snap_ts, snap_src = resolve_snapshot_ts(archive + ".manifest.json", archive)
    res["snapshot_ts"] = snap_ts
    res["snapshot_iso"] = iso(snap_ts)
    res["snapshot_source"] = snap_src
    if snap_ts is not None:
        res["backup_age_hours"] = round((now - snap_ts) / 3600.0, 2)

    # 保护面相对路径 ↔ 绝对路径
    base_rel = {}
    for abs_p in baseline:
        if root and abs_p.startswith(os.path.join(root, "")):
            base_rel[os.path.relpath(abs_p, root)] = abs_p
        else:
            base_rel[os.path.normpath(abs_p)] = abs_p
    arch_set = set(names)

    items = []
    for name in names:
        arc_sha = sums.get(name)
        live_path = os.path.join(root, name)
        rec = {"arc": name, "tier": kernel.tier_of(name), "archive_sha256": arc_sha,
               "live_path": live_path, "in_baseline": name in base_rel,
               "live_sha256": None, "live_mtime": None, "live_mtime_iso": "",
               "axis": None, "rpo_violation": False, "reason": ""}
        if arc_sha is None:
            rec["axis"] = AXIS_NO_RECORD
            rec["reason"] = "归档无该成员 sha 记录（manifest/SHA256SUMS 缺）"
            items.append(rec)
            continue
        if not os.path.exists(live_path):
            rec["axis"] = AXIS_DRIFT_MISSING
            rec["reason"] = "盘上已无该文件（删除未被归档覆盖）"
            rec["rpo_violation"] = True
            items.append(rec)
            continue
        try:
            live_sha = kernel.sha256_file(live_path)
        except Exception as e:  # noqa: BLE001
            rec["axis"] = AXIS_NO_RECORD
            rec["reason"] = f"live 不可读: {type(e).__name__}"
            items.append(rec)
            continue
        mtime = os.path.getmtime(live_path)
        rec["live_sha256"] = live_sha
        rec["live_mtime"] = mtime
        rec["live_mtime_iso"] = iso(mtime)
        if live_sha == arc_sha:
            # 口径：内容相同即 SAME —— 即使 mtime 比快照新（触碰/复制/改 mtime 不算漂移）
            rec["axis"] = AXIS_SAME
            rec["reason"] = "内容与归档记录一致（mtime 不参与判定）"
        elif snap_ts is not None and mtime <= snap_ts:
            rec["axis"] = AXIS_DRIFT_PRE
            rec["reason"] = "内容不一致但 mtime 早于快照点 ⇒ 不判 RPO 违规"
        else:
            rec["axis"] = AXIS_DRIFT
            rec["reason"] = "内容不一致且 mtime 晚于快照点 ⇒ 未被归档覆盖"
            rec["rpo_violation"] = True
        items.append(rec)

    res["items"] = items
    counts = {}
    for r in items:
        counts[r["axis"]] = counts.get(r["axis"], 0) + 1
    res["counts"] = counts
    res["total_members"] = len(items)

    viol = [r for r in items if r["rpo_violation"]]
    res["rpo_violations"] = sorted(viol, key=lambda r: (r["live_mtime"] is None, r["live_mtime"] or 0))
    known = [r["live_mtime"] for r in viol if r["live_mtime"] is not None]
    if known:
        earliest = min(known)
        res["earliest_unbacked_ts"] = earliest
        res["earliest_unbacked_iso"] = iso(earliest)
        res["effective_rpo_hours"] = round((now - earliest) / 3600.0, 2)
    res["violations_without_mtime"] = [r["arc"] for r in viol if r["live_mtime"] is None]

    covered = sorted(rel for rel in base_rel if rel in arch_set)
    arch_missing = sorted(rel for rel in base_rel if rel not in arch_set)
    res["baseline_coverage"] = {
        "protection_surface_total": len(base_rel),
        "archive_members_total": len(arch_set),
        "covered": len(covered),
        "covered_sample": covered[:limit],
        "archive_missing": len(arch_missing),
        "archive_missing_sample": arch_missing[:limit],
        "archive_only": len([n for n in arch_set if n not in base_rel]),
        "coverage_ratio": round(len(covered) / len(base_rel), 4) if base_rel else 0.0,
    }

    arc_sha_after = kernel.sha256_file(archive)
    base_sha_after = kernel.sha256_file(baseline_path)
    res["archive_sha256"] = arc_sha_before
    res["baseline_sha256"] = base_sha_before
    res["archive_unchanged"] = arc_sha_before == arc_sha_after
    res["baseline_unchanged"] = base_sha_before == base_sha_after
    res["seconds"] = round(time.time() - t0, 2)

    if not res["archive_unchanged"]:
        res["findings"].append("归档 sha256 在本次扫描中变化 ⇒ 只读自证失败")
    if not res["baseline_unchanged"]:
        res["findings"].append("基线 sha256 在本次扫描中变化 ⇒ 只读自证失败")
    if res["baseline_coverage"]["archive_missing"]:
        res["findings"].append(
            f"保护面覆盖洞: {res['baseline_coverage']['archive_missing']}/"
            f"{res['baseline_coverage']['protection_surface_total']} 条在保护面但不在归档成员表（R444 D1）")
    if counts.get(AXIS_DRIFT_PRE):
        res["findings"].append(f"{counts[AXIS_DRIFT_PRE]} 条内容不符但 mtime 早于快照点（归档内容异常/时钟偏差）")
    if res["violations_without_mtime"]:
        res["findings"].append(f"{len(res['violations_without_mtime'])} 条违规无 mtime（盘上已删）"
                               f"，有效 RPO 只按可得 mtime 计算")

    if not quiet:
        print_summary(res)
    return res


def rc_of(res: dict, strict_coverage: bool = False) -> int:
    if res.get("error"):
        return 2
    if res.get("findings") and any("只读自证失败" in f for f in res["findings"]):
        return 2
    if res.get("rpo_violations"):
        return 1
    if strict_coverage and res.get("baseline_coverage", {}).get("archive_missing"):
        return 1
    return 0


def print_summary(res: dict) -> None:
    print(f"[归档] {res['archive']}")
    print(f"[基线] {res['baseline']}   [活根] {res['root']}")
    print(f"[快照点] {res.get('snapshot_iso')}（来源 {res.get('snapshot_source')}）"
          f"  [现在] {res['generated']}")
    print(f"[只读自证] 归档 sha256 不变={res.get('archive_unchanged')} "
          f"基线 sha256 不变={res.get('baseline_unchanged')}")
    print(f"[成员] 普通文件 {res.get('total_members')}（非普通文件 {res.get('non_file_members')}）"
          f"  扫描 {res.get('seconds')}s")
    print(f"[分类计数] {res.get('counts')}")
    cov = res.get("baseline_coverage", {})
    print(f"[保护面] 保护面 {cov.get('protection_surface_total')} / 归档 {cov.get('archive_members_total')}"
          f" / covered {cov.get('covered')}（{cov.get('coverage_ratio')}）/ 归档缺失 "
          f"{cov.get('archive_missing')}")
    print(f"[有效 RPO] {res.get('effective_rpo_hours')} 小时"
          f"（最早未备份修改 {res.get('earliest_unbacked_iso') or 'N/A'}；"
          f"归档本身年龄 {res.get('backup_age_hours')} 小时）")
    print(f"[RPO 违规] {len(res.get('rpo_violations') or [])} 条")
    for r in (res.get("rpo_violations") or [])[:20]:
        print(f"  - [{r['tier']}] {r['arc']}  {r['axis']}  mtime={r['live_mtime_iso'] or 'N/A'}  {r['reason']}")
    for f in res.get("findings") or []:
        print(f"[发现] {f}")


# --------------------------------------------------------------------------- #
# 只读自证
# --------------------------------------------------------------------------- #
def proof(archive: str = "", baseline_path: str = "", root: str = "") -> int:
    archive = archive or pick_archive()
    baseline_path = baseline_path or DEFAULT_BASELINE
    targets = [p for p in (archive, baseline_path) if p and os.path.exists(p)]
    if not targets:
        print("[proof] 无可用输入（归档与基线都不存在）")
        return 2
    kernel = load_kernel()
    before = {p: kernel.sha256_file(p) for p in targets}
    res = scan(archive, baseline_path, root, quiet=True)
    after = {p: kernel.sha256_file(p) for p in targets}
    changed = [p for p in targets if before[p] != after.get(p)]
    print(f"[proof] files={len(targets)} changed={len(changed)}")
    for p in targets:
        print(f"  {'CHANGED' if p in changed else 'UNCHANGED'}  {before[p][:16]}…  {p}")
    if res.get("error"):
        print(f"[proof] scan error={res['error']}")
        return 2
    print(f"[proof] 分类计数={res.get('counts')} 有效 RPO={res.get('effective_rpo_hours')}h")
    return 0 if not changed else 1


def pick_archive() -> str:
    kernel = load_kernel()
    return kernel.pick_default_archive()


# --------------------------------------------------------------------------- #
# 自测（含负控）
# --------------------------------------------------------------------------- #
def _mk_fixture(tmp: str, snap_ts: float, now: float):
    """造夹具：归档 + manifest + live 树 + 基线（含 6 类负控样本）。"""
    live = os.path.join(tmp, "live")
    files = {
        "memory/global_mem_insight.txt": b"# L1 index line one\n",   # 正控：同内容但 mtime 被触碰
        "memory/drifted.txt": b"old content\n",                        # NC：快照后被改 → 违规
        "memory/pre_snapshot.txt": b"orig content\n",                  # NC：mtime 早于快照 → 不算违规
        "memory/deleted.txt": b"bye\n",                                # NC：盘上已删
        "tools/tool_a.py": b"print('a')\n",                            # 正控：完全一致
    }
    arc = os.path.join(tmp, "fixture.tar.gz")
    with tarfile.open(arc, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    with open(arc + ".manifest.json", "w", encoding="utf-8") as f:
        json.dump({"generated": iso(snap_ts), "root": live,
                   "files": [{"arc": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                             for n, d in files.items()],
                   "archive_sha256": "fixture"}, f, ensure_ascii=False)
    # live 树
    for name, data in files.items():
        if name == "memory/deleted.txt":
            continue
        p = os.path.join(live, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    # 正控：同内容但 mtime 改到「现在」（若按 mtime 判漂移，这里必被误判）
    touch = os.path.join(live, "memory/global_mem_insight.txt")
    os.utime(touch, (now, now))
    # NC：快照后被改
    drifted = os.path.join(live, "memory/drifted.txt")
    with open(drifted, "wb") as f:
        f.write(b"new content after snapshot\n")
    os.utime(drifted, (now - 3600, now - 3600))
    # NC：内容不符但 mtime 早于快照点（回拨）
    pre = os.path.join(live, "memory/pre_snapshot.txt")
    with open(pre, "wb") as f:
        f.write(b"changed but backdated\n")
    os.utime(pre, (snap_ts - 3600, snap_ts - 3600))
    # 基线：四条归档内 + 一条「只在保护面」（NC：归档缺条目）
    baseline = os.path.join(tmp, "baseline.sha256")
    rows = [("memory/global_mem_insight.txt", hashlib.sha256(files["memory/global_mem_insight.txt"]).hexdigest()),
            ("memory/drifted.txt", hashlib.sha256(files["memory/drifted.txt"]).hexdigest()),
            ("memory/pre_snapshot.txt", hashlib.sha256(files["memory/pre_snapshot.txt"]).hexdigest()),
            ("memory/deleted.txt", hashlib.sha256(files["memory/deleted.txt"]).hexdigest()),
            ("tools/tool_a.py", hashlib.sha256(files["tools/tool_a.py"]).hexdigest()),
            ("memory/only_in_baseline.md", "a" * 64)]
    with open(baseline, "w", encoding="utf-8") as f:
        for rel, sha in rows:
            f.write(f"{sha}  {os.path.join(live, rel)}\n")
    return arc, baseline, live


def selftest() -> int:
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))
        print(("  PASS " if cond else "  FAIL ") + name)

    print("== selftest ==")
    now = time.time()
    snap = now - 3 * 86400
    with tempfile.TemporaryDirectory() as tmp:
        arc, baseline, live = _mk_fixture(tmp, snap, now)
        res = scan(arc, baseline, live, now=now, quiet=True)
        by = {r["arc"]: r for r in res["items"]}

        chk("真读到了归档成员（>=5）", len(res["items"]) >= 5 and res["error"] is None)
        chk("真复用了内核接口（逐项有来源行号）",
            all(v["file"].endswith("backup_restore_drill.py") for v in res["kernel_api"].values()))
        # 正控 + NC1（mtime 不参与判定）
        chk("正控 同内容但 mtime 被触碰 → SAME（不许按 mtime 判漂移）",
            by["memory/global_mem_insight.txt"]["axis"] == AXIS_SAME
            and by["memory/global_mem_insight.txt"]["rpo_violation"] is False)
        chk("正控 完全一致 → SAME", by["tools/tool_a.py"]["axis"] == AXIS_SAME)
        # NC：快照后被改 → 违规
        chk("负控 快照后被改 → DRIFT 且计 RPO 违规",
            by["memory/drifted.txt"]["axis"] == AXIS_DRIFT
            and by["memory/drifted.txt"]["rpo_violation"] is True)
        # NC：mtime 早于快照 → 不判违规
        chk("负控 mtime 早于快照点 → DRIFT_PRE_SNAPSHOT 且不计违规",
            by["memory/pre_snapshot.txt"]["axis"] == AXIS_DRIFT_PRE
            and by["memory/pre_snapshot.txt"]["rpo_violation"] is False)
        # NC：盘上已删
        chk("负控 盘上已删 → LIVE_MISSING 且计违规（无 mtime）",
            by["memory/deleted.txt"]["axis"] == AXIS_DRIFT_MISSING
            and by["memory/deleted.txt"]["rpo_violation"] is True
            and res["violations_without_mtime"] == ["memory/deleted.txt"])
        # NC：归档缺条目 → ARCHIVE_MISSING（绝不判 SAME）
        cov = res["baseline_coverage"]
        chk("负控 保护面有、归档无 → ARCHIVE_MISSING（不判 SAME）",
            cov["archive_missing"] == 1
            and cov["archive_missing_sample"] == ["memory/only_in_baseline.md"]
            and all(r["axis"] != AXIS_SAME for r in res["items"]
                    if r["arc"] == "memory/only_in_baseline.md"))
        chk("保护面覆盖账目自洽（covered + missing == 保护面）",
            cov["covered"] + cov["archive_missing"] == cov["protection_surface_total"] == 6
            and cov["covered"] == 5)
        # 有效 RPO
        chk("有效 RPO == (now - 最早未备份修改)/3600（约 1 小时）",
            abs(res["effective_rpo_hours"] - round(3600 / 3600.0, 2)) < 0.01
            and abs(res["backup_age_hours"] - 72.0) < 0.05)
        chk("违规清单排序把可得 mtime 的放在前（drifted 在 deleted 之前）",
            [r["arc"] for r in res["rpo_violations"]] == ["memory/drifted.txt", "memory/deleted.txt"])
        # 只读自证
        chk("负控 只读自证：归档 sha256 前后一致", res["archive_unchanged"] is True)
        chk("负控 只读自证：基线 sha256 前后一致", res["baseline_unchanged"] is True)

        # NC：空归档 → rc=2
        empty = os.path.join(tmp, "empty.tar.gz")
        with tarfile.open(empty, "w:gz"):
            pass
        res_empty = scan(empty, baseline, live, now=now, quiet=True)
        chk("负控 空归档 → error 且 rc=2", bool(res_empty.get("error")) and rc_of(res_empty) == 2)

        # NC：归档缺 sha 记录 → NO_ARCHIVE_RECORD（不判 SAME）
        noSums = os.path.join(tmp, "noSums.tar.gz")
        data = b"hello\n"
        with tarfile.open(noSums, "w:gz") as tf:
            info = tarfile.TarInfo("memory/global_mem_insight.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        res_ns = scan(noSums, baseline, live, now=now, quiet=True)
        chk("负控 归档无 sha 记录 → NO_ARCHIVE_RECORD（不判 SAME）",
            res_ns["items"][0]["axis"] == AXIS_NO_RECORD)

        # NC：归档/基线缺失 → rc=2
        chk("负控 归档缺失 → rc=2", rc_of(scan(os.path.join(tmp, "nope.tar.gz"), baseline, live, now=now, quiet=True)) == 2)
        chk("负控 基线缺失 → rc=2", rc_of(scan(arc, os.path.join(tmp, "nope.sha256"), live, now=now, quiet=True)) == 2)

        # rc 语义
        chk("rc 语义：有违规 → 1", rc_of(res) == 1)
        res_ok = dict(res)
        res_ok["rpo_violations"] = []
        chk("rc 语义：无违规 → 0（覆盖洞默认不改 rc）", rc_of(res_ok) == 0)
        chk("rc 语义：--strict-coverage 时覆盖洞 → 1", rc_of(res_ok, strict_coverage=True) == 1)
        chk("复跑确定性：同输入两次分类计数逐字一致",
            scan(arc, baseline, live, now=now, quiet=True)["counts"] == res["counts"])

    bad = [n for n, ok in checks if not ok]
    print(f"== selftest: {len(checks) - len(bad)}/{len(checks)} passed ==")
    return 0 if not bad else 1


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv and not argv[0].startswith("-") else "scan"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv

    ap = argparse.ArgumentParser(description="T0 备份 RPO 与漂移判定器（只读）")
    ap.add_argument("--archive", default="", help="T0 归档（默认取 temp/t0_backups 最新 *.tar.gz）")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--root", default=GA_ROOT, help="活文件根（默认 GA 仓库根）")
    ap.add_argument("--out", default="", help="产物 JSON 落点（默认 temp/t0_rpo_drift/<date>/scan.json）")
    ap.add_argument("--limit", type=int, default=20, help="清单抽样条数")
    ap.add_argument("--now", type=float, default=None, help="覆盖「现在」（测试用）")
    ap.add_argument("--strict-coverage", action="store_true", help="保护面覆盖洞也计入 rc=1")
    ap.add_argument("--json", action="store_true", help="只打印 JSON（不含逐条明细）")
    args = ap.parse_args(rest)

    if cmd == "selftest":
        return selftest()
    if cmd == "proof":
        return proof(args.archive, args.baseline, args.root)
    if cmd != "scan":
        print(f"未知子命令: {cmd}（可选 scan/proof/selftest）")
        return 2

    archive = args.archive or pick_archive()
    res = scan(archive, args.baseline, args.root, limit=args.limit,
               now=args.now, quiet=args.json)
    out = args.out or os.path.join(DEFAULT_OUT_DIR, time.strftime("%Y-%m-%d"), "scan.json")
    if not res.get("error"):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        payload = {k: v for k, v in res.items() if k != "items"}
        payload["items"] = res["items"]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        if not args.json:
            print(f"[产物] {out}")
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if k != "items"},
                         ensure_ascii=False, default=str))
    return rc_of(res, strict_coverage=args.strict_coverage)


if __name__ == "__main__":
    sys.exit(main())
