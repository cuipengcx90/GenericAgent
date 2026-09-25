#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mem_caliber.py —— macOS 常驻进程内存「三口径」只读快照（TODO#74 / R400）

三个口径（2026-09-25 实测，**不可互推**）：
  A. ps RSS   : 常驻物理页大小，单位 KB（macOS）。**不含**被压缩/换出的页
                ⇒ 对长期 idle 进程会剧烈抖动并系统性低估（实测同一进程 5 分钟内 38.8MB↔265.6MB）。
  B. footprint: /usr/bin/footprint -p <pid> 的 phys_footprint（**含已压缩页**）——这是「现值」。
  C. peak     : 同输出的 phys_footprint_peak —— 进程**生命周期高水位**，不是稳态值。
                它同时等于 `vmmap -summary` 的 "Physical footprint (peak)" 与 `heap <pid>` 头部同行，三器一致。
辅助解释列（vmmap -summary）：written / resident / swapped_out；RSS ≈ resident，footprint ≈ written(含压缩)。

边界（实测）：
  * `footprint -a`（全机扫描）**必须 root**；`footprint -p <pid>` 对自己（同用户）进程免 root。
  * `heap`/`vmmap`/`leaks` 对同用户进程免 root；跨用户进程会失败（本工具会跳过并标注）。
  * 全程只读，不修改任何进程状态。

用法：
  python3 tools/mem_caliber.py                    # 取 RSS 前 8 名的常驻进程
  python3 tools/mem_caliber.py --pids 617,614     # 指定 pid
  python3 tools/mem_caliber.py --top 12 --json /tmp/cal.json
  python3 tools/mem_caliber.py --selftest         # 自检（含解析负控）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

TIMEOUT = 90


def sh(cmd, timeout=TIMEOUT):
    """返回 (stdout+stderr, rc)。超时/异常不抛，rc=-1。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]", -1
    except Exception as e:  # noqa: BLE001
        return "[ERR %s]" % e, -1


def top_by_rss(n):
    """RSS 前 n 名的常驻进程 [(pid, rss_kb, etime, comm)]。"""
    out, _ = sh(["ps", "-axo", "pid=,rss=,etime=,comm="])
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if m:
            rows.append((int(m.group(1)), int(m.group(2)), m.group(3), m.group(4).strip()))
    rows.sort(key=lambda r: -r[1])
    return rows[:n]


def ps_one(pid):
    out, _ = sh(["ps", "-o", "rss=,etime=,comm=", "-p", str(pid)])
    line = out.strip().splitlines()[0] if out.strip() else ""
    m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)$", line)
    if not m:
        return None
    return {"rss_kb": int(m.group(1)), "etime": m.group(2), "comm": m.group(3).strip()}


def parse_footprint(text):
    """解析 footprint 输出；失败返回 None（fail-closed，不猜）。"""
    d = {}
    m = re.search(r"Footprint:\s*([\d.]+)\s*([KMG]?B)", text)
    if m:
        d["header_footprint"] = m.group(0).split(":", 1)[1].strip()
    m = re.search(r"phys_footprint:\s*([^\n]+)", text)
    d["cur"] = m.group(1).strip() if m else None
    m = re.search(r"phys_footprint_peak:\s*([^\n]+)", text)
    d["peak"] = m.group(1).strip() if m else None
    cats, total = [], None
    for line in text.splitlines():
        if line.strip().endswith("TOTAL") and "Category" not in line:
            toks = line.split()
            if len(toks) >= 8:
                total = {"dirty": " ".join(toks[0:2]), "clean": " ".join(toks[2:4]),
                         "reclaimable": " ".join(toks[4:6]), "regions": toks[6]}
                continue
        m = re.match(r"\s*([\d.]+ [KMG]B|0 B)\s+([\d.]+ [KMG]B|0 B)\s+([\d.]+ [KMG]B|0 B)\s+(\d+)\s+(\S.*)$", line)
        if m and m.group(5).strip() != "Category":
            cats.append({"dirty": m.group(1), "clean": m.group(2), "reclaimable": m.group(3),
                         "regions": int(m.group(4)), "cat": m.group(5).strip()})
    d["total"] = total
    cats.sort(key=lambda c: -_mb(c["dirty"]))
    d["top_cats"] = cats[:3]
    if d.get("cur") is None and d.get("header_footprint") is None:
        return None
    return d


def _mb(s):
    """'4418 MB' / '64 KB' / '0 B' → MB(float)；解析失败返回 0。"""
    m = re.match(r"([\d.]+)\s*([KMG]?B)", (s or "").strip())
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2)
    return v * {"B": 1 / 1048576.0, "KB": 1 / 1024.0, "MB": 1.0, "GB": 1024.0}.get(u, 0.0)


def parse_vmmap(text):
    """取 vmmap -summary 的两行关键统计。"""
    d = {}
    m = re.search(r"Writable regions:\s*Total=(\S+)\s+written=(\S+)\s*\((\d+)%\)\s+resident=(\S+)\s*\((\d+)%\)\s+swapped_out=(\S+)", text)
    if m:
        d.update(written=m.group(2), written_pct=m.group(3), resident=m.group(4),
                 resident_pct=m.group(5), swapped_out=m.group(6))
    m = re.search(r"ReadOnly portion of Libraries:\s*Total=(\S+)\s+resident=(\S+)\s*\((\d+)%\)", text)
    if m:
        d.update(ro_total=m.group(1), ro_resident=m.group(2), ro_resident_pct=m.group(3))
    m = re.search(r"Physical footprint:\s*([^\n]+)", text)
    d["phys"] = m.group(1).strip() if m else None
    m = re.search(r"Physical footprint \(peak\):\s*([^\n]+)", text)
    d["peak"] = m.group(1).strip() if m else None
    return d


def snapshot(pid):
    r = {"pid": pid, "ps": ps_one(pid)}
    fp_out, rc_fp = sh(["footprint", "-p", str(pid)])
    r["footprint"] = parse_footprint(fp_out)
    r["footprint_rc"] = rc_fp
    if r["footprint"] is None:
        r["footprint_err"] = fp_out.strip().splitlines()[:2]
    vm_out, rc_vm = sh(["vmmap", "-summary", str(pid)])
    r["vmmap"] = parse_vmmap(vm_out) if rc_vm == 0 else None
    return r


def render(rows, stamp):
    out = []
    out.append("采样时刻 %s（口径见文件头：RSS=常驻页, footprint=现值含压缩, peak=生命周期高水位）" % stamp)
    hdr = "%-7s %-26s %10s %10s %10s %7s %9s %9s %9s" % (
        "PID", "进程", "RSS(MB)", "现值(MB)", "peak(MB)", "fp/RSS", "recov(MB)", "written", "swapped")
    out.append(hdr)
    out.append("-" * len(hdr))
    for r in rows:
        ps_ = r.get("ps") or {}
        fp = r.get("footprint") or {}
        vm = r.get("vmmap") or {}
        rss = ps_.get("rss_kb", 0) / 1024.0
        cur = _mb(fp.get("cur") or fp.get("header_footprint"))
        peak = _mb(fp.get("peak"))
        recl = _mb((fp.get("total") or {}).get("reclaimable"))
        comm = (ps_.get("comm") or "?").split("/")[-1][:26]
        out.append("%-7s %-26s %10.1f %10.1f %10.1f %7s %9.1f %9s %9s" % (
            r["pid"], comm, rss, cur, peak,
            ("%.1fx" % (cur / rss)) if rss else "-", recl,
            vm.get("written", "-"), vm.get("swapped_out", "-")))
    return "\n".join(out)


def selftest():
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name + ((" | " + str(extra)) if extra else ""))
        ok = ok and cond

    # 1) 解析正样例（真实输出片段）
    sample = """\
=======
python3 [617]: 64-bit    Footprint: 4418 MB (16384 bytes per page)
=======
  Dirty      Clean  Reclaimable    Regions    Category
1613 MB        0 B          0 B         84    MALLOC_MEDIUM
 10 MB        0 B          0 B          1    page table
    ---        ---          ---        ---    ---
4418 MB    5248 KB        64 KB       4696    TOTAL
Auxiliary data:
    phys_footprint: 4418 MB
    phys_footprint_peak: 17 GB
"""
    p = parse_footprint(sample)
    chk("解析物理足迹正样例", p is not None and p["cur"] == "4418 MB" and p["peak"] == "17 GB",
        p and (p["cur"], p["peak"], p["total"]))
    chk("TOTAL 行四列", p and p["total"] == {"dirty": "4418 MB", "clean": "5248 KB",
                                             "reclaimable": "64 KB", "regions": "4696"}, p and p["total"])
    chk("分类行排序/截断", p and len(p["top_cats"]) == 2 and p["top_cats"][0]["cat"] == "MALLOC_MEDIUM",
        p and [c["cat"] for c in p["top_cats"]])
    # 2) 负控：垃圾输入必须 fail-closed
    chk("负控 垃圾输入→None", parse_footprint("hello world\nno numbers here") is None)
    # 3) 单位换算
    chk("KB→MB", abs(_mb("1024 KB") - 1.0) < 1e-6 and abs(_mb("1 GB") - 1024.0) < 1e-6)
    # 4) 真机自跑（对本进程自身，免 root）
    me = os.getpid()
    r = snapshot(me)
    chk("真机自采样 ps.rss", bool(r["ps"]) and r["ps"]["rss_kb"] > 0, r["ps"])
    chk("真机自采样 footprint.cur", bool(r["footprint"]) and r["footprint"].get("cur") is not None,
        r["footprint"] and r["footprint"].get("cur"))
    chk("真机自采样 vmmap.written", bool(r["vmmap"]) and r["vmmap"].get("written") is not None,
        r["vmmap"])
    print("selftest: " + ("ALL PASS" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="macOS 进程内存三口径只读快照")
    ap.add_argument("--pids", help="逗号分隔 pid 列表；缺省取 RSS 前 --top 名")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--json", help="把原始快照写到该路径")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    stamp = sh(["date", "+%Y-%m-%d %H:%M:%S %Z"])[0].strip()
    if a.pids:
        pids = [int(x) for x in re.split(r"[,\s]+", a.pids.strip()) if x]
        rows = []
        for pid in pids:
            r = {"pid": pid}
            r.update({k: v for k, v in snapshot(pid).items() if k != "pid"})
            rows.append(r)
    else:
        rows = []
        for pid, _rss, _et, _c in top_by_rss(a.top):
            rows.append(snapshot(pid))
    print(render(rows, stamp))
    print("\n提示：peak≫现值 ⇒ 历史尖峰（先查尖峰成因，勿当泄漏）；Reclaimable≈0 ⇒ 无可丢弃页，"
          "想降内存只能让应用自己释放 dirty；需深挖时用 footprint -v / heap <pid> -showSizes。")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"stamp": stamp, "rows": rows}, f, ensure_ascii=False, indent=1)
        print("JSON: %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
