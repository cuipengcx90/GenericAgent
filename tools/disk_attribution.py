#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""disk_attribution.py — 磁盘占用归因快照 / 差异对比（全只读，绝不删除）

背景：本机数据卷长期 97–98% 满，"回收后又复发"。历史报告（R358/R359/R364/R368/R372）
各自只测了当时的热点；缺一份**可复跑、可对比**的归因基线，导致"到底什么在长"只能靠记忆。

用法：
  python3 tools/disk_attribution.py snapshot [--out DIR] [--quick]   # 采一份归因快照(JSON)
  python3 tools/disk_attribution.py top [JSON]                      # 打印快照 top 榜
  python3 tools/disk_attribution.py diff OLD.json NEW.json          # 逐项增量 + df 增量
  python3 tools/disk_attribution.py selftest                        # 负控自测(diff 数学)

口径（重要，防误判）：
  * 一律用 `du -sk`（**已分配块**），不用文件逻辑长度。实测 OrbStack 的
    `data.img.raw` 逻辑 460.43 GiB、实占仅 14.01 GiB（稀疏文件）——用 `find -size`
    会把它当 460 GiB 的巨无霸，属假归因。
  * `df` 的 "used" 含 APFS 快照 / purgeable / 不可读目录；因此
    `df_used − Σdu(可读项)` 的差额 ≠ "神秘占用"，而是**不可见口径**（快照+purgeable+无权限）。
    专门用 `du -skx /System/Volumes/Data` 可量化"可读总量"，差额才有意义。
  * 目录遍历会被 TCC 挂起（`~/Library/Containers` 尤甚），故每项都有硬超时，
    超时记 `null` 并在输出里明确标注，绝不把"没测到"当 0（R17 教训）。
"""
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DATA = "/System/Volumes/Data"

# 归因项：覆盖本机已知的量大目录（2026-09-24 实测标定）
ITEMS = [
    f"{DATA}/Applications",
    f"{DATA}/Library",
    f"{DATA}/opt",
    f"{DATA}/private",
    f"{DATA}/usr",
    f"{DATA}/System",
    f"{HOME}/Library/Caches",
    f"{HOME}/Library/Containers",
    f"{HOME}/Library/Group Containers",
    f"{HOME}/Library/Application Support",
    f"{HOME}/Library/Logs",
    f"{HOME}/Library/Developer",
    f"{HOME}/.cache",
    f"{HOME}/.local",
    f"{HOME}/.npm",
    f"{HOME}/develop",
    f"{HOME}/projects",
    f"{HOME}/Documents",
    f"{HOME}/Downloads",
    f"{HOME}/Movies",
    f"{HOME}/Music",
    f"{HOME}/Pictures",
    f"{HOME}/Desktop",
    f"{HOME}/.Trash",
]
QUICK_SKIP = {  # --quick 时跳过的重项（慢/易卡 TCC）
    f"{HOME}/Library/Containers",
    f"{HOME}/Library/Group Containers",
    f"{HOME}/develop",
    f"{DATA}/Applications",
    f"{DATA}/Library",
    f"{DATA}/private",
}

TIMEOUT_DEFAULT = 120


def run(cmd, timeout):
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        return "", f"TIMEOUT>{timeout}s", round(time.time() - t0, 1)


def gi(x):
    return round(x / 1048576.0, 2)  # KB -> GiB


def df_data():
    out, _, _ = run(f"df -k {DATA}", 30)
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return {}
    f = lines[1].split()
    # Filesystem 1024-blocks Used Available Capacity Mounted
    return {"fs": f[0], "kb_total": int(f[1]), "kb_used": int(f[2]), "kb_avail": int(f[3]),
            "capacity": f[4], "total_gib": gi(int(f[1])), "used_gib": gi(int(f[2])),
            "avail_gib": gi(int(f[3]))}


def snapshots():
    out, _, _ = run("tmutil listlocalsnapshots /", 30)
    names = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Snapshots")]
    return names


def du_kb(path, timeout):
    if not os.path.exists(path):
        return None, "MISSING", 0.0
    out, err, el = run(f"du -sk {json.dumps(path)} 2>/dev/null", timeout)
    out = out.strip()
    if not out:
        return None, ("TIMEOUT" if "TIMEOUT" in err else "UNREADABLE"), el
    try:
        return int(out.split()[0]), "", el
    except (ValueError, IndexError):
        return None, "PARSE_ERR", el


def take_snapshot(quick=False, timeout=TIMEOUT_DEFAULT, items=None):
    items = items if items is not None else ITEMS
    snap = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "host": os.uname().nodename,
            "df": df_data(), "snapshots": snapshots(), "items": {}, "errors": {}}
    for p in items:
        if quick and p in QUICK_SKIP:
            snap["errors"][p] = "SKIPPED(quick)"
            continue
        kb, err, el = du_kb(p, timeout)
        snap["items"][p] = gi(kb) if kb is not None else None
        if err:
            snap["errors"][p] = f"{err} ({el}s)"
    return snap


def print_top(snap, n=25):
    print(f"快照 {snap['ts']}  df: used {snap['df'].get('used_gib')} GiB / "
          f"avail {snap['df'].get('avail_gib')} GiB ({snap['df'].get('capacity')})")
    print(f"本地快照: {len(snap['snapshots'])} 个")
    rows = [(v, k) for k, v in snap["items"].items() if v is not None]
    rows.sort(reverse=True)
    tot = 0.0
    for v, k in rows[:n]:
        print(f"  {v:8.2f} GiB  {k}")
    tot = sum(v for v, _ in rows)
    print(f"  ---- 可读项合计 {tot:.2f} GiB（未计未测/超时项，见 errors）")
    if snap.get("errors"):
        print("  未测到的项（不是 0！）:")
        for k, v in snap["errors"].items():
            print(f"    - {k}: {v}")


def diff(old, new):
    """返回 (df_delta, [(path, delta, old_v, new_v)])，按 |delta| 降序。"""
    d_df = None
    if old.get("df", {}).get("kb_used") is not None and new.get("df", {}).get("kb_used") is not None:
        d_df = round((new["df"]["kb_used"] - old["df"]["kb_used"]) / 1048576.0, 2)
    rows = []
    for p in sorted(set(old.get("items", {})) | set(new.get("items", {}))):
        a, b = old.get("items", {}).get(p), new.get("items", {}).get(p)
        if a is None or b is None:
            rows.append((p, None, a, b))
        else:
            rows.append((p, round(b - a, 2), a, b))
    rows.sort(key=lambda r: (abs(r[1]) if r[1] is not None else -1), reverse=True)
    return d_df, rows


def selftest():
    """负控：diff 的数学与排序必须对；缺项必须标注而不是当 0。"""
    a = {"ts": "a", "df": {"kb_used": 100 * 1048576}, "items": {"/x": 10.0, "/y": 5.0, "/z": None}, "errors": {}}
    b = {"ts": "b", "df": {"kb_used": 130 * 1048576}, "items": {"/x": 12.5, "/y": 4.0, "/z": 1.0}, "errors": {}}
    d, rows = diff(a, b)
    assert d == 30.0, d
    assert rows[0][0] == "/x" and rows[0][1] == 2.5, rows
    assert ("/z", None, None, 1.0) in rows, "缺项必须标 None 而非差值"
    # 负控：篡改期望应当失败
    try:
        assert d == 31.0, "负控期望失败"
        raise AssertionError("负控未生效：错误增量竟被接受")
    except AssertionError as e:
        if "负控未生效" in str(e):
            raise
    print("selftest OK: df_delta=30.0 排序首位=/x(+2.5) 缺项=None  (负控 1/1 按预期失败)")
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "snapshot":
        out_dir = "temp/disk_probe/attrib"
        quick = "--quick" in argv
        if "--out" in argv:
            out_dir = argv[argv.index("--out") + 1]
        os.makedirs(out_dir, exist_ok=True)
        snap = take_snapshot(quick=quick)
        path = os.path.join(out_dir, time.strftime("attrib_%Y%m%d_%H%M%S.json"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        print(f"已写入 {path}")
        print_top(snap)
        return 0
    if cmd == "top":
        p = argv[1] if len(argv) > 1 else None
        if not p:
            d = "temp/disk_probe/attrib"
            files = sorted(os.path.join(d, f) for f in os.listdir(d)) if os.path.isdir(d) else []
            if not files:
                print("没有快照，先跑 snapshot")
                return 2
            p = files[-1]
        print_top(json.load(open(p, encoding="utf-8")))
        return 0
    if cmd == "diff":
        old = json.load(open(argv[1], encoding="utf-8"))
        new = json.load(open(argv[2], encoding="utf-8"))
        d, rows = diff(old, new)
        print(f"df used 增量: {d} GiB   ({old['ts']} -> {new['ts']})")
        for p, dv, a, b in rows[:30]:
            if dv is None:
                print(f"  {'N/A':>8}  {p}   (old={a} new={b}，缺项不比较)")
            elif dv != 0:
                print(f"  {dv:+8.2f} GiB  {p}   ({a} -> {b})")
        return 0
    if cmd == "selftest":
        return selftest()
    print(f"未知子命令: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
