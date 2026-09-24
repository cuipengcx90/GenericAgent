#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kc 快照守卫负控（合成夹具，零副作用：不碰真实快照目录/不联网）

用例
  1 全齐（symlink 到真实快照的 db/manifest）            → PASS  rc0
  2 移走最新一份（模拟「今天没采快照」）                → WARN  rc2
  3 篡改 manifest 里的 sha256                          → CRIT  rc3
  4 空目录                                             → CRIT  rc3
  5 --now 推后到 stale=21 天（源库最短窗 23 天）        → CRIT  rc3
  6 私信去重（桩替身计数，不联网）：同指纹同日第二次不发 → sent=1
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

GA = "/Users/cuip/projects/GenericAgent"
REAL = os.path.join(GA, "temp", "kc_snapshots")
GUARD = os.path.join(GA, "tools", "kc_snapshot_guard.py")
DATES = ["2026-09-22", "2026-09-23", "2026-09-25"]


def run(args):
    p = subprocess.run([sys.executable, GUARD] + args, capture_output=True, text=True)
    return p.returncode, p.stdout


def mkfix(dst, dates, real=True):
    os.makedirs(dst, exist_ok=True)
    for d in dates:
        os.makedirs(os.path.join(dst, d), exist_ok=True)
        for f in ("knowledgeC.db", "manifest.json"):
            src = os.path.join(REAL, d, f)
            if real and os.path.exists(src):
                os.symlink(src, os.path.join(dst, d, f))       # 只读语义：symlink 不复制 25 MB


def case(name, ok_rc, rc, out, must_have=()):
    miss = [m for m in must_have if m not in out]
    good = (rc == ok_rc) and not miss
    print("[%s] %-34s rc=%d(期望%d)%s" % ("OK" if good else "FAIL", name, rc, ok_rc,
                                          "" if not miss else " 缺标记%s" % miss))
    return good


def main():
    results = []
    tmp = tempfile.mkdtemp(prefix="kc_guard_fx_")
    print("夹具根:", tmp)

    fix_all = os.path.join(tmp, "all")
    mkfix(fix_all, DATES)
    rc, out = run(["--root", fix_all, "--now", "2026-09-25", "--no-notify"])
    results.append(case("1 全齐", 0, rc, out, ["[PASS]"]))

    fix_old = os.path.join(tmp, "stale")
    mkfix(fix_old, DATES[:-1])
    rc, out = run(["--root", fix_old, "--now", "2026-09-25", "--no-notify"])
    results.append(case("2 移走最新一份(stale=2)", 2, rc, out, ["[WARN]", "陈旧：最新快照 2026-09-23"]))

    fix_sha = os.path.join(tmp, "sha")
    mkfix(fix_sha, DATES)
    p = os.path.join(fix_sha, "2026-09-23", "manifest.json")
    m = json.load(open(p, encoding="utf-8"))
    m["revisions"][-1]["sha256"] = "0" * 64
    os.remove(p)
    json.dump(m, open(p, "w", encoding="utf-8"))
    rc, out = run(["--root", fix_sha, "--now", "2026-09-25", "--no-notify"])
    results.append(case("3 篡改 sha256", 3, rc, out, ["[CRITICAL]", "sha256 不符"]))

    fix_empty = os.path.join(tmp, "empty")
    os.makedirs(fix_empty, exist_ok=True)
    rc, out = run(["--root", fix_empty, "--now", "2026-09-25", "--no-notify"])
    results.append(case("4 空目录", 3, rc, out, ["[CRITICAL]", "快照目录为空"]))

    rc, out = run(["--root", fix_all, "--now", "2026-10-16", "--no-notify"])
    results.append(case("5 推后 21 天（stale=21）", 3, rc, out, ["[CRITICAL]", "只剩 2 天余量"]))

    # 6) 去重桩测：把 reflect_common.notify_feishu 换成计数桩
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    calls = []
    if "reflect_scripts.reflect_common" not in sys.modules:
        sys.path.insert(0, GA)
        import reflect_scripts.reflect_common as rc_mod
    else:
        rc_mod = sys.modules["reflect_scripts.reflect_common"]
    orig = rc_mod.notify_feishu
    rc_mod.notify_feishu = lambda text, open_id=None: calls.append(text) or {"code": 0}
    try:
        st = os.path.join(tmp, "state.json")
        a = g.notify_once("T", "fp1", st, "2026-09-25")
        b = g.notify_once("T", "fp1", st, "2026-09-25")
        c = g.notify_once("T", "fp1", st, "2026-09-26")
    finally:
        rc_mod.notify_feishu = orig
    ok = (a == "sent" and b == "dedup" and c == "sent" and len(calls) == 2)
    print("[%s] %-34s sent/dedup/sent=%s/%s/%s calls=%d" % (
        "OK" if ok else "FAIL", "6 私信去重(桩)", a, b, c, len(calls)))
    results.append(ok)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n负控合计: %d/%d" % (sum(1 for x in results if x), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
