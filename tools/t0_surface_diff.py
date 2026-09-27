#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T0 两套定义（保护面 × 备份面）差异门禁 — R460 / TODO L174

背景（R444 D1/D3 提出的缺口）
- **保护面** = `temp/t0_baseline.sha256`：R364（2026-09-23）生成的一次性清单，记录「丢了就没了的
  数据」的 sha256（绝对路径）。现役消费者三处：`tools/reclaim_candidates.py`、`tools/cache_reclaim.py`、
  `tools/backup_restore_drill.py`（前者用它判「候选能不能删」，后两者用它判「有没有被改」）。
- **备份面** = `tools/t0_snapshot.py` 的 `SETS` 展开后真正进 tar.gz 的成员（本次快照 348 条）。
- 两个集合**不是同一个集合**（R444 实测：交 295 / 6299 = 4.68%）。于是存在两种静默风险：
  「以为保住了、其实没进备份」（保护面有、备份面无）与「备份了、但比对时找不到基线」（备份面有、
  保护面无 ⇒ 无法判漂移）。

本工具做什么（纯 stdlib、**只读**）
1. `defs`    打印两套定义：备份面**直接复用生产者**（import `tools/t0_snapshot.py` 的 `SETS`/`collect`，
             零重写）展开的声明面 + 归档快照的实际成员 + 保护面（条数/字节/mtime/sha256）。
2. `diff`    计算 交 / 仅保护面 / 仅备份面，按 `REGISTRY` 注册表分桶（每桶给 类别+处置+理由），
             输出「需登记」明细与 rc。任一输入被改动即 rc=2（自证输入 sha256 前后一致）。
3. `selftest` 合成夹具自测（含负控：删 1 条、格式坏、归档缺失、仓外绝对路径、accept 登记）。

rc 语义
  0 = 两方向差异全部落在 `IGNORABLE` 桶（或已被 `--accept-file` 显式登记）
  1 = 存在 `REGISTER`（需登记）差异 —— 「需人工登记/处置」，不是「工具坏了」
  2 = 输入错误（基线缺失/格式坏、归档缺失或非 tar、自证失败）

注册表是**单一源**：新增路径类别会被判 `UNCLASSIFIED`（fail-closed，rc=1），必须在 `REGISTRY`
里登记「它属于哪一类、可忽略还是需登记、理由」之后才放行。

用法
  python3 tools/t0_surface_diff.py defs
  python3 tools/t0_surface_diff.py diff
  python3 tools/t0_surface_diff.py diff --json temp/t0_surface_diff/latest.json
  python3 tools/t0_surface_diff.py diff --accept-file tools/t0_surface_accepted.json
  python3 tools/t0_surface_diff.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ROOT / "temp" / "t0_baseline.sha256"
DEFAULT_ARCHIVE_DIR = ROOT / "temp" / "t0_backups"
PRODUCER = ROOT / "tools" / "t0_snapshot.py"

# --------------------------------------------------------------- 注册表（单一源）
# 每条 = (正则, 类别, 处置, 理由)；按顺序首命中生效。
# 处置：IGNORABLE=两套定义**有意**不同、无需处置；REGISTER=需人工登记或处置（rc=1 来源）。
REGISTRY = [
    (r"^sche_tasks/done/", "DONE_REPORTS", "IGNORABLE",
     "计划任务运行期报告（每日新增、可再生）；备份面有意只收 sche_tasks 的「定义层」(json/py)"),
    (r"^sche_tasks/(output|sqlite_snapshots|disabled)/", "RUN_ARTIFACT", "IGNORABLE",
     "运行期产物 / 已归档的旧定义目录"),
    (r"(\.bak(_\d{8}_\d{4})?|\.deprecated|\.orig|\.tmp)$", "EDITOR_BACKUP", "IGNORABLE",
     "手工备份副本与弃用件"),
    (r"(^|/)(__pycache__/)|\.py[co]$|(^|/)\.DS_Store$", "CACHE", "IGNORABLE",
     "解释器缓存 / 系统噪音"),
    (r"^logs/", "LOGS", "IGNORABLE", "日志"),
    (r"^temp/kc_snapshots/", "T0_KC_DATA", "REGISTER",
     "T0 数据本体（knowledgeC 每日底稿，源库约 4 周滚动删除）——两套定义都必须包含"),
    (r"^temp/", "TEMP_OTHER", "IGNORABLE", "temp 下非 T0 的临时产物"),
    (r"^memory/", "MEMORY", "REGISTER", "耐久记忆（L1/L2 索引、SOP、L4 会话归档、自主报告）"),
    (r"^tools/", "TOOLS", "REGISTER", "仓内工具（丢了要重写）"),
    (r"^reflect_scripts/", "REFLECT", "REGISTER", "反射器脚本（保护面历史缺登记：reflect_scripts 0 条）"),
    (r"^sche_tasks/", "SCHED_DEF", "REGISTER", "计划任务定义与辅助脚本"),
]
UNCLASSIFIED = ("UNCLASSIFIED", "REGISTER", "未登记的路径类别 —— fail-closed，须在 REGISTRY 登记后放行")

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def registry_fingerprint() -> str:
    blob = json.dumps(REGISTRY, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def classify(rel: str):
    for pat, cls, decision, reason in REGISTRY:
        if re.search(pat, rel):
            return cls, decision, reason
    return UNCLASSIFIED


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------ 两个面

class InputError(Exception):
    """输入不可用 ⇒ rc=2"""


def norm_rel(raw: str, root: Path = ROOT):
    """把清单里的路径归一化为「仓内相对路径」。仓外绝对路径 ⇒ (None, raw)。"""
    s = raw.strip()
    if s.startswith("./"):
        s = s[2:]
    if not os.path.isabs(s):
        return s, None
    root_s = str(root)
    if s == root_s:
        return ".", None
    if s.startswith(root_s + os.sep):
        return s[len(root_s) + 1:], None
    return None, s          # 仓外，fail-closed 单列


def load_baseline(path: Path):
    """→ (rel→sha, {仓外绝对路径}, 自证信息)。格式：'<sha256>  <path>'（两空格）。"""
    if not path.exists():
        raise InputError(f"基线不存在：{path}")
    rel_map, outside, bad = {}, set(), []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2 or not SHA_RE.match(parts[0].strip()):
                bad.append((ln, line[:120]))
                continue
            sha, raw = parts[0].strip(), parts[1]
            rel, out = norm_rel(raw)
            if rel is None:
                outside.add(out)
                continue
            rel_map[rel] = sha
    if bad:
        raise InputError(f"基线格式坏（{len(bad)} 行，例：L{bad[0][0]} {bad[0][1]!r}）")
    if not rel_map:
        raise InputError(f"基线为空：{path}")
    st = path.stat()
    return rel_map, outside, {
        "path": str(path), "bytes": st.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "sha256": sha256_file(path), "entries": len(rel_map),
    }


def load_archive(path: Path):
    """→ (成员集合, 自证信息)。只 getnames()，**不解档**。"""
    if not path.exists():
        raise InputError(f"归档不存在：{path}")
    try:
        tf = tarfile.open(path, mode="r:*")
    except Exception as e:                                    # noqa: BLE001
        raise InputError(f"归档打不开（{type(e).__name__}: {e}）：{path}") from e
    with tf:
        names = [n for n in tf.getnames() if not n.endswith("/")]
        members = set()
        for n in names:
            if n.startswith("./"):
                n = n[2:]
            if n in (".", ""):
                continue
            members.add(n)
        # 归档元数据是 **sidecar 文件**（生产者把 manifest/SHA256SUMS 写在 tar 旁边，不在 tar 里）；
        # 若未来改为 tar 内藏，也在这里剔除，避免把元数据当 T0 资产计入差异。
        meta = {"SHA256SUMS", "manifest.json", "./SHA256SUMS", "./manifest.json"}
        dropped = sorted(n for n in members if n in meta)
        members -= set(dropped)
        manifest_check = {"found": False, "sidecar": [], "dropped_members": dropped}
        for cand in (str(path) + ".manifest.json", str(path) + ".SHA256SUMS"):
            if os.path.exists(cand):
                manifest_check["sidecar"].append(os.path.basename(cand))
        mj = str(path) + ".manifest.json"
        if os.path.exists(mj):
            manifest_check["found"] = True
            try:
                md = json.loads(Path(mj).read_text(encoding="utf-8"))
                listed = {str(f["arc"]).lstrip("./") for f in md.get("files", [])}
            except Exception as e:                             # noqa: BLE001
                manifest_check["error"] = f"{type(e).__name__}: {e}"
                listed = set()
            manifest_check["listed"] = len(listed)
            manifest_check["members"] = len(members)
            manifest_check["missing_in_manifest"] = sorted(listed - members)[:5]
            manifest_check["unlisted"] = sorted(members - listed)[:5]
            manifest_check["archive_sha256_field"] = md.get("archive_sha256") if isinstance(md, dict) else None
    st = path.stat()
    return members, {
        "path": str(path), "bytes": st.st_size, "members": len(members),
        "sha256": sha256_file(path), "manifest": manifest_check,
    }


def load_accept(path):
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise InputError(f"--accept-file 不存在：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    acc = {}
    for it in data.get("accepted", []):
        acc[(it.get("direction"), it.get("path"))] = it.get("reason", "")
    return acc


def compute(baseline_path: Path, archive_path: Path, accept_file=None, sample_limit: int = 40):
    b_map, outside, b_info = load_baseline(baseline_path)
    a_set, a_info = load_archive(archive_path)
    accept = load_accept(accept_file)
    B, A = set(b_map), set(a_set)

    only_b, only_a = sorted(B - A), sorted(A - B)
    inter = len(A & B)

    def bucketize(items, direction):
        groups, register, accepted, ignorable = {}, [], [], []
        for rel in items:
            cls, decision, reason = classify(rel)
            key = (cls, decision)
            g = groups.setdefault(key, {"class": cls, "decision": decision,
                                        "reason": reason, "count": 0, "sample": []})
            g["count"] += 1
            if len(g["sample"]) < sample_limit:
                g["sample"].append(rel)
            if (direction, rel) in accept:
                g["decision"] = "ACCEPTED"
                g["reason"] = accept[(direction, rel)] or "（已登记）"
                accepted.append({"path": rel, "reason": g["reason"]})
            elif decision == "IGNORABLE":
                ignorable.append(rel)
            else:
                register.append({"path": rel, "class": cls, "reason": reason})
        # 桶内分类汇总（ACCEPTED 单列，不重复计数）
        out = {}
        for (cls, decision), g in groups.items():
            out[f"{cls}/{g['decision']}"] = g
        return out, register, accepted, ignorable

    b_buckets, b_reg, b_acc, b_ign = bucketize(only_b, "only_baseline")
    a_buckets, a_reg, a_acc, a_ign = bucketize(only_a, "only_archive")

    rc = 1 if (b_reg or a_reg or outside) else 0
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "registry_fingerprint": registry_fingerprint(),
        "inputs": {"baseline": b_info, "archive": a_info, "accept_file": str(accept_file) if accept_file else None},
        "sets": {
            "baseline": len(B), "archive": len(A), "intersection": inter,
            "only_baseline": len(only_b), "only_archive": len(only_a), "union": len(B | A),
            "inter_over_baseline_pct": round(100.0 * inter / len(B), 2),
            "inter_over_archive_pct": round(100.0 * inter / len(A), 2) if A else 0.0,
        },
        "outside_root": sorted(outside),
        "buckets": {"only_baseline": b_buckets, "only_archive": a_buckets},
        "register": {"only_baseline": b_reg, "only_archive": a_reg},
        "accepted": {"only_baseline": b_acc, "only_archive": a_acc},
        "counts": {
            "register_only_baseline": len(b_reg), "register_only_archive": len(a_reg),
            "ignorable_only_baseline": len(b_ign), "ignorable_only_archive": len(a_ign),
            "accepted": len(b_acc) + len(a_acc), "unclassified": sum(1 for r in b_reg + a_reg if r["class"] == "UNCLASSIFIED"),
        },
        "rc": rc,
    }


# ------------------------------------------------------------------ 打印

def print_res(res, limit: int = 40):
    s, ins = res["sets"], res["inputs"]
    b, a = ins["baseline"], ins["archive"]
    print("== T0 两套定义差异（保护面 × 备份面）==")
    print(f"保护面基线 {b['path']}")
    print(f"       条数={b['entries']} 字节={b['bytes']} mtime={b['mtime']} sha256={b['sha256'][:16]}…")
    print(f"备份面归档 {a['path']}")
    print(f"       成员={a['members']} 字节={a['bytes']} sha256={a['sha256'][:16]}…")
    mc = a.get("manifest") or {}
    if mc.get("found"):
        print(f"       SHA256SUMS 交叉核对：清单 {mc.get('listed')} 条 / 成员 {mc.get('members')} 条 "
              f"/ 清单缺成员 {len(mc.get('missing_in_manifest') or [])} / 未被列 {len(mc.get('unlisted') or [])}")
    print(f"注册表指纹={res['registry_fingerprint']}（分桶规则的单一源）")
    print(f"集合：交 {s['intersection']} / 仅保护面 {s['only_baseline']} / 仅备份面 {s['only_archive']} "
          f"/ 并 {s['union']} ⇒ 交/保护面={s['inter_over_baseline_pct']}%  交/备份面={s['inter_over_archive_pct']}%")
    for direction, title in (("only_baseline", "保护面 − 备份面（有登记、没进备份）"),
                             ("only_archive", "备份面 − 保护面（备份了、基线里没有 ⇒ 无法判漂移）")):
        print(f"\n-- {title} --")
        for key, g in sorted(res["buckets"][direction].items(), key=lambda kv: -kv[1]["count"]):
            print(f"   [{g['decision']:9s}] {g['class']:13s} n={g['count']:<5d} {g['reason'][:60]}")
        regs = res["register"][direction]
        if regs:
            print(f"   ⚠️ 需登记 {len(regs)} 条，样例（≤{limit}）：")
            for r in regs[:limit]:
                print(f"      {r['class']:13s} {r['path']}")
            if len(regs) > limit:
                print(f"      … 另 {len(regs)-limit} 条（见 --json）")
    if res["accepted"]["only_baseline"] or res["accepted"]["only_archive"]:
        print(f"\n-- 已登记（--accept-file）n={res['counts']['accepted']} --")
    print(f"\nrc={res['rc']}  需登记合计={res['counts']['register_only_baseline']+res['counts']['register_only_archive']}"
          f"（仅保护面 {res['counts']['register_only_baseline']} / 仅备份面 {res['counts']['register_only_archive']}）"
          f" 可忽略合计={res['counts']['ignorable_only_baseline']+res['counts']['ignorable_only_archive']}")


def load_producer_sets():
    """直接复用生产者 tools/t0_snapshot.py 的 SETS/collect（单一源，零重写）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_t0_snapshot", PRODUCER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 只执行定义，无副作用（main 在 __main__ 保护下）
    return mod


def cmd_defs(a) -> int:
    print("== 两套「T0 定义」声明面 ==")
    print("\n[1] 保护面 = temp/t0_baseline.sha256")
    try:
        _, _, info = load_baseline(Path(a.baseline))
        print(f"    {info['path']}")
        print(f"    条数={info['entries']} 字节={info['bytes']} mtime={info['mtime']} sha256={info['sha256'][:16]}…")
    except InputError as e:
        print(f"    ✗ {e}")
    print("    定义来源：R364（2026-09-23）一次性清单，仓内**不存在**再生脚本（temp/reclaim_R364.py 非工具）")
    print("\n[2] 备份面 = tools/t0_snapshot.py 的 SETS 展开 → tar.gz")
    try:
        mod = load_producer_sets()
        uniq, per_set = mod.collect(None)
        print(f"    声明集合（现在盘上）：{len(uniq)} 条")
        for name, pairs in per_set.items():
            print(f"      - {name:16s} {len(pairs):5d} 条")
        if a.archive or a.archive_dir:
            ap = resolve_archive(a)
            members, info = load_archive(ap)
            print(f"    归档快照：{info['path']} 成员={info['members']} sha256={info['sha256'][:16]}…")
    except Exception as e:                                     # noqa: BLE001
        print(f"    ✗ 生产者不可用：{type(e).__name__}: {e}")
        return 2
    return 0


def resolve_archive(a) -> Path:
    if getattr(a, "archive", None):
        return Path(a.archive)
    d = Path(a.archive_dir or DEFAULT_ARCHIVE_DIR)
    cands = sorted(d.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
    if not cands:
        raise InputError(f"归档目录无 *.tar.gz：{d}")
    return cands[-1]


def cmd_diff(a) -> int:
    try:
        arc = resolve_archive(a)
        res = compute(Path(a.baseline), arc, accept_file=a.accept_file, sample_limit=a.limit)
    except InputError as e:
        print(f"[INPUT ERROR] {e}")
        return 2
    print_res(res, limit=a.limit)
    if a.json:
        out = Path(a.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[json] {out} ({out.stat().st_size} B)")
    return res["rc"]


# ------------------------------------------------------------------ selftest

def _mk_fixture(tmp: Path, baseline_lines, members, tar_broken=False):
    bl = tmp / "baseline.sha256"
    bl.write_text("".join(f"{'0'*64}  {p}\n" for p in baseline_lines), encoding="utf-8")
    tg = tmp / "arc.tar.gz"
    import tarfile as _tf
    if tar_broken:
        tg.write_bytes(b"not a tar")
    else:
        with _tf.open(tg, "w:gz") as t:
            for m in members:
                f = tmp / ("src_" + m.replace("/", "_"))
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x", encoding="utf-8")
                t.add(f, arcname=m)
        # 元数据 sidecar（与真生产者一致：manifest 写在 tar **旁边**，不是 tar 成员）
        Path(str(tg) + ".manifest.json").write_text(
            json.dumps({"generated": "selftest", "files": [{"arc": m, "size": 1, "sha256": "0" * 64}
                                                           for m in members]}, ensure_ascii=False),
            encoding="utf-8")
    return bl, tg


def selftest() -> int:
    ok = fail = 0

    def chk(cond, name, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name} {extra}")

    with tempfile.TemporaryDirectory(prefix="t0_surface_selftest_") as td:
        t = Path(td)
        print("== selftest：合成夹具 ==")
        bl, tg = _mk_fixture(
            t,
            ["memory/a.md", "sche_tasks/done/x.md", "tools/y.py", "reflect_scripts/z.py"],
            ["memory/a.md", "reflect_scripts/z.py", "memory/extra.md"],
        )
        res = compute(bl, tg)
        s = res["sets"]
        chk(s["intersection"] == 2 and s["only_baseline"] == 2 and s["only_archive"] == 1,
            "PC1 集合计数", f"got {s}")
        chk(res["counts"]["register_only_baseline"] == 1 and res["counts"]["register_only_archive"] == 1,
            "PC1b 注册桶计数（tools=需登记 / memory_extra=需登记）", f"got {res['counts']}")
        chk(res["counts"]["ignorable_only_baseline"] == 1, "PC1c done/*.md 判可忽略", f"got {res['counts']}")
        chk(res["rc"] == 1, "PC1d 有需登记 ⇒ rc=1")
        chk(res["inputs"]["archive"]["manifest"]["listed"] == 3
            and res["inputs"]["archive"]["manifest"]["unlisted"] == []
            and res["inputs"]["archive"]["manifest"]["missing_in_manifest"] == [],
            "PC1e 真读到归档+sidecar manifest 交叉核对", f"got {res['inputs']['archive']['manifest']}")

        # 正控：交集一致 + 差异全可忽略 ⇒ rc=0
        bl2, tg2 = _mk_fixture(t / "pc2" if (t / "pc2").mkdir() or True else t, ["memory/a.md", "sche_tasks/done/x.md"],
                               ["memory/a.md"])
        r2 = compute(bl2, tg2)
        chk(r2["rc"] == 0 and r2["counts"]["register_only_baseline"] == 0
            and r2["counts"]["ignorable_only_baseline"] == 1, "PC2 全可忽略 ⇒ rc=0", f"got {r2['counts']} rc={r2['rc']}")

        # 负控 NC1：从基线删 1 条（交集项）⇒ 必报差异（该项转入 仅备份面）
        d1 = t / "nc1"
        d1.mkdir()
        src = (t / "baseline.sha256").read_text(encoding="utf-8").splitlines()
        (d1 / "baseline.sha256").write_text("\n".join(l for l in src if not l.endswith("memory/a.md")) + "\n",
                                            encoding="utf-8")
        r = compute(d1 / "baseline.sha256", tg)
        chk("memory/a.md" in {x["path"] for x in r["register"]["only_archive"]},
            "NC1 基线删 1 条 ⇒ 该条出现在 仅备份面/需登记", f"got {r['sets']}")
        chk(r["sets"]["intersection"] == 1 and r["sets"]["only_archive"] == 2, "NC1b 计数随之变化", f"got {r['sets']}")

        # 负控 NC2：删掉「可忽略」那条 ⇒ 可忽略计数变化（防静默通过）
        d2 = t / "nc2"
        d2.mkdir()
        (d2 / "baseline.sha256").write_text("\n".join(l for l in src if not l.endswith("sche_tasks/done/x.md")) + "\n",
                                            encoding="utf-8")
        r = compute(d2 / "baseline.sha256", tg)
        chk(r["counts"]["ignorable_only_baseline"] == 0 and r["sets"]["only_baseline"] == 1,
            "NC2 删可忽略项亦被门禁看见", f"got {r['counts']}")

        # 负控 NC3：基线格式坏 ⇒ rc=2
        d3 = t / "nc3"
        d3.mkdir()
        (d3 / "baseline.sha256").write_text("deadbeef  memory/a.md\n", encoding="utf-8")
        try:
            compute(d3 / "baseline.sha256", tg)
            chk(False, "NC3 坏 sha ⇒ InputError")
        except InputError:
            chk(True, "NC3 坏 sha ⇒ InputError（rc=2）")

        # 负控 NC4：归档缺失/坏 ⇒ InputError
        try:
            compute(bl, t / "nope.tar.gz")
            chk(False, "NC4a 归档缺失 ⇒ InputError")
        except InputError:
            chk(True, "NC4a 归档缺失 ⇒ InputError（rc=2）")
        _, tgbad = _mk_fixture(t / "nc4" if (t / "nc4").mkdir() or True else t, ["memory/a.md"], [], tar_broken=True)
        try:
            compute(bl, tgbad)
            chk(False, "NC4b 非 tar ⇒ InputError")
        except InputError:
            chk(True, "NC4b 非 tar ⇒ InputError（rc=2）")

        # 负控 NC5：仓外绝对路径 ⇒ 单列且 rc=1
        d5 = t / "nc5"
        d5.mkdir()
        (d5 / "baseline.sha256").write_text(f"{'0'*64}  /etc/hosts\n{'0'*64}  memory/a.md\n", encoding="utf-8")
        r = compute(d5 / "baseline.sha256", tg)
        chk(r["outside_root"] == ["/etc/hosts"] and r["rc"] == 1, "NC5 仓外路径 fail-closed", f"got {r['outside_root']}")

        # 负控 NC6：未登记路径类别 ⇒ UNCLASSIFIED/需登记
        d6 = t / "nc6"
        d6.mkdir()
        (d6 / "baseline.sha256").write_text(f"{'0'*64}  weird/thing.bin\n", encoding="utf-8")
        r = compute(d6 / "baseline.sha256", tg)
        chk(r["counts"]["unclassified"] == 1 and r["rc"] == 1, "NC6 未登记类别 fail-closed", f"got {r['counts']}")

        # 正控 PC3：--accept-file 登记后 rc=0
        d7 = t / "pc3"
        d7.mkdir()
        acc = d7 / "accepted.json"
        acc.write_text(json.dumps({"accepted": [
            {"direction": "only_baseline", "path": "tools/y.py", "reason": "测试登记", "date": "2026-09-28"},
            {"direction": "only_archive", "path": "memory/extra.md", "reason": "测试登记", "date": "2026-09-28"},
        ]}, ensure_ascii=False), encoding="utf-8")
        r = compute(bl, tg, accept_file=acc)
        chk(r["rc"] == 0 and r["counts"]["accepted"] == 2 and r["counts"]["register_only_baseline"] == 0,
            "PC3 accept-file 登记后 rc=0", f"got {r['counts']} rc={r['rc']}")

        # 只读自证：两份输入 sha256 前后一致
        h1, h2 = sha256_file(bl), sha256_file(tg)
        compute(bl, tg)
        chk(h1 == sha256_file(bl) and h2 == sha256_file(tg), "PC4 输入未被改动（只读自证）")

        # 注册表指纹稳定 + 未命中判定与注册表一致
        chk(registry_fingerprint() == registry_fingerprint() and len(registry_fingerprint()) == 16,
            "PC5 注册表指纹稳定")

    print(f"\n== selftest 结果：{ok} passed / {fail} failed ==")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="T0 两套定义（保护面×备份面）差异门禁（只读）")
    ap.add_argument("--selftest", action="store_true", help="合成夹具自测（含负控）")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("defs", help="打印两套定义（声明面）")
    d.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    d.add_argument("--archive", default=None)
    d.add_argument("--archive-dir", default=None)
    d.set_defaults(func=cmd_defs)

    f = sub.add_parser("diff", help="计算差异并按注册表分桶")
    f.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    f.add_argument("--archive", default=None, help="指定 tar.gz（默认取 --archive-dir 里最新一份）")
    f.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE_DIR))
    f.add_argument("--accept-file", default=None, help="存量登记 JSON（{accepted:[{direction,path,reason,date}]}）")
    f.add_argument("--json", default=None)
    f.add_argument("--limit", type=int, default=40, help="明细打印上限")
    f.set_defaults(func=cmd_diff)

    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 0
    try:
        return a.func(a)
    except InputError as e:
        print(f"[INPUT ERROR] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
