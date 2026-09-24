#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T0 关键资产快照工具（R381 / TODO#11 二期·数据侧）

设计原则
- **只读源数据**：所有子命令都不修改被归档的文件；演练只在沙箱（/tmp）里删/改副本。
- **零新增定时**：本工具不自带调度，定时/目的地属待批项。
- **可复跑**：任何一次 build 都产出 `manifest.json`（逐文件 sha256+size）与 `SHA256SUMS`，
  `verify` 用它们做逐文件比对，而不是只看体积。

T0 = 「丢了就没了」的数据（R358 一期结论）：`memory/`（含 git 之外的 L4 会话 zip 等）
+ `temp/kc_snapshots/`（knowledgeC 每日底稿，源库约 4 周滚动删除）+ 小型代码资产。

子命令
  list     列出 T0 集合与「temp/ 下哪些数据必须离开」的可执行清单（体积/更新频率/git 状态）
  build    生成 tar.gz + manifest.json + SHA256SUMS（可选 --encrypt 走 openssl AES-256）
  verify   解档到沙箱，逐文件 sha256 + 计数与 manifest 比对（--negctl 会故意篡改副本，验证能报错）
  drill    真演练：build → verify → 删掉解出的副本 → 重新解档再 verify → 打印结论表
  rotate   只保留最近 N 份归档

用法
  python3 tools/t0_snapshot.py list
  python3 tools/t0_snapshot.py drill --only temp/kc_snapshots
  python3 tools/t0_snapshot.py build --out temp/t0_backups
  python3 tools/t0_snapshot.py verify temp/t0_backups/ga-t0-YYYYMMDD.tar.gz
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "temp" / "t0_backups"
WORK = Path(tempfile.gettempdir()) / "t0_snapshot_work"

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".DS_Store"}
EXCLUDE_SUFFIX = {".pyc", ".pyo"}

# T0 集合：path 相对仓库根；recursive=False 时用 globs 白名单
SETS = [
    {"name": "memory", "path": "memory", "recursive": True},
    {"name": "kc_snapshots", "path": "temp/kc_snapshots", "recursive": True},
    {"name": "tools", "path": "tools", "recursive": True},
    {"name": "reflect_scripts", "path": "reflect_scripts", "recursive": True},
    {"name": "sche_tasks_defs", "path": "sche_tasks", "recursive": False, "globs": ["*.json", "*.py"]},
]


# ---------------------------------------------------------------- 基础

def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ok(p: Path) -> bool:
    if p.name in EXCLUDE_DIR_NAMES or p.name.startswith("._"):
        return False
    if p.suffix in EXCLUDE_SUFFIX:
        return False
    return True


def iter_set_files(spec: dict, only: str | None = None):
    """产出 (abs_path, arcname)。only 为正斜杠相对路径前缀过滤。"""
    base = ROOT / spec["path"]
    if only and not (spec["path"] == only or only.startswith(spec["path"] + "/")
                     or spec["path"].startswith(only + "/")):
        return
    if not base.exists():
        return
    if spec.get("recursive", True):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if not _ok(p) or p.is_symlink():
                    continue
                yield p, str(p.relative_to(ROOT))
    else:
        for pat in spec.get("globs", ["*"]):
            for p in sorted(base.glob(pat)):
                if p.is_file() and _ok(p):
                    yield p, str(p.relative_to(ROOT))


def collect(only: str | None = None):
    files = []
    per_set = {}
    for spec in SETS:
        got = list(iter_set_files(spec, only))
        per_set[spec["name"]] = got
        files.extend(got)
    # 去重（同一文件可能被多个集合覆盖）
    seen, uniq = set(), []
    for p, arc in files:
        if arc in seen:
            continue
        seen.add(arc)
        uniq.append((p, arc))
    return uniq, per_set


def manifest_of(pairs):
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(ROOT),
        "files": [
            {"arc": arc, "size": p.stat().st_size, "sha256": sha256_file(p)}
            for p, arc in sorted(pairs, key=lambda x: x[1])
        ],
    }


# ---------------------------------------------------------------- build

def cmd_build(a) -> int:
    only = a.only
    pairs, per_set = collect(only)
    if not pairs:
        print("[build] 无文件可归档（检查 --only）")
        return 2
    out = Path(a.out) if a.out else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = a.tag or (Path(only).name if only else "all")
    arc = out / f"ga-t0-{tag}-{stamp}.tar.gz"

    man = manifest_of(pairs)
    man["sets"] = {k: len(v) for k, v in per_set.items()}
    man["scope"] = only or "all"
    t0 = time.time()
    with tarfile.open(arc, "w:gz") as tf:
        for p, an in sorted(pairs, key=lambda x: x[1]):
            tf.add(str(p), arcname=an, recursive=False)

    man["archive_sha256"] = sha256_file(arc)   # 归档自证哈希（明文被删后仍可核对）

    enc = None
    if a.encrypt:
        enc = arc.with_suffix(".gz.enc")
        pw = os.environ.get("T0_PASSPHRASE")
        if not pw:
            print("[build] --encrypt 需要环境变量 T0_PASSPHRASE（生产口令请放 Keychain，勿写在命令里）")
            return 2
        rc = subprocess.run(
            ["/usr/bin/openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
             "-salt", "-in", str(arc), "-out", str(enc), "-pass", "env:T0_PASSPHRASE"],
            capture_output=True, text=True)
        if rc.returncode != 0:
            print("[build] 加密失败:", rc.stderr.strip())
            return 2

    (out / (arc.name + ".manifest.json")).write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"{f['sha256']}  {f['arc']}" for f in man["files"]]
    (out / (arc.name + ".SHA256SUMS")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    total = sum(f["size"] for f in man["files"])
    print(f"[build] 文件 {len(man['files'])} 个 / {total/1048576:.1f} MiB 源")
    print(f"[build] 归档 {arc}  {arc.stat().st_size/1048576:.1f} MiB  用时 {time.time()-t0:.1f}s")
    print(f"[build] 清单 {arc.name}.manifest.json + .SHA256SUMS")
    arc_sha = man["archive_sha256"]
    if enc:
        enc_sha = sha256_file(enc)
        print(f"[build] 加密 {enc}  {enc.stat().st_size/1048576:.1f} MiB （口令来自 env，未落盘）")
        if not getattr(a, "keep_plain", False):
            # 加密成功且哈希已记 ⇒ 默认删除明文，避免"加密了但明文还躺旁边"
            arc.unlink()
            print(f"[build] 已删除明文 {arc.name}（--keep-plain 可保留）；归档哈希已记入清单")
    print(f"[build] 归档 sha256 = {arc_sha}")
    return 0


# ---------------------------------------------------------------- verify

def _load_manifest(arc: Path):
    m = Path(str(arc) + ".manifest.json")
    if not m.exists() and arc.name.endswith(".enc"):
        m = Path(str(arc)[:-4] + ".manifest.json")
    if not m.exists():
        print(f"[verify] 缺清单 {m}")
        return None
    return json.loads(m.read_text(encoding="utf-8"))


def _extract(arc: Path, dest: Path, encrypted=False, passphrase=None):
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    src = arc
    if encrypted:
        plain = dest.parent / (arc.name + ".plain.tar.gz")
        rc = subprocess.run(
            ["/usr/bin/openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
             "-in", str(arc), "-out", str(plain), "-pass", f"pass:{passphrase}"],
            capture_output=True, text=True)
        if rc.returncode != 0:
            return False, "解密失败: " + rc.stderr.strip()
        src = plain
    with tarfile.open(src, "r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")   # Py>=3.12：拒绝路径穿越/设备文件
        except TypeError:                        # 老版本无 filter 参数
            tf.extractall(dest)
    return True, "ok"


def verify_against_manifest(man, dest: Path, tamper: bool = False):
    """逐文件 sha256 + 计数比对；返回 (ok, 统计, 明细)"""
    exp = {f["arc"]: f for f in man["files"]}
    got = {}
    for dirpath, dirnames, filenames in os.walk(dest):
        for fn in filenames:
            p = Path(dirpath) / fn
            got[str(p.relative_to(dest))] = p
    miss = sorted(set(exp) - set(got))
    extra = sorted(set(got) - set(exp))
    bad = []
    for arc, meta in exp.items():
        p = got.get(arc)
        if not p:
            continue
        if p.stat().st_size != meta["size"] or sha256_file(p) != meta["sha256"]:
            bad.append(arc)
    if tamper and not bad:      # 负控：故意篡改第一个文件的一字节
        victim = sorted(got)[0]
        with open(got[victim], "r+b") as f:
            b = f.read(1)
            f.seek(0)
            f.write(bytes([b[0] ^ 0xFF]) if b else b"\x00")
        print(f"  (负控：已篡改副本 {victim})")
        return verify_against_manifest(man, dest, tamper=False)
    stat = dict(exp_files=len(exp), got_files=len(got), missing=len(miss),
                extra=len(extra), mismatch=len(bad))
    ok = not miss and not extra and not bad
    detail = dict(missing=miss[:5], extra=extra[:5], mismatch=bad[:5])
    return ok, stat, detail


def cmd_verify(a) -> int:
    arc = Path(a.archive)
    if not arc.exists():
        print(f"[verify] 归档不存在 {arc}")
        return 2
    man = _load_manifest(arc)
    if not man:
        return 2
    dest = WORK / "extract"
    ok, msg = _extract(arc, dest, encrypted=a.encrypt, passphrase=os.environ.get("T0_PASSPHRASE"))
    if not ok:
        print("[verify]", msg)
        return 2
    good, stat, detail = verify_against_manifest(man, dest, tamper=a.negctl)
    print(f"[verify] 清单 {stat['exp_files']} 项 / 副本 {stat['got_files']} 项 | 缺失 {stat['missing']} "
          f"| 多出 {stat['extra']} | 内容不一致 {stat['mismatch']} => {'✅逐项一致' if good else '❌不一致'}")
    if not good:
        print("  明细:", json.dumps(detail, ensure_ascii=False))
        return 1
    return 0


# ---------------------------------------------------------------- drill

def cmd_drill(a) -> int:
    """真演练：build → verify → 删副本 → 重新解档 → verify（+ 可选负控）"""
    t0 = time.time()
    out = Path(a.out) if a.out else DEFAULT_OUT
    ns = argparse.Namespace(out=str(out), tag=a.tag, encrypt=False, only=a.only)
    if cmd_build(ns) != 0:
        return 2
    files = sorted(out.glob(f"ga-t0-{a.tag or (Path(a.only).name if a.only else 'all')}-*.tar.gz"),
                   key=lambda p: p.stat().st_mtime)
    arc = files[-1]
    man = _load_manifest(arc)

    dest = WORK / "extract"
    ok1, msg = _extract(arc, dest)
    if not ok1:
        print("[drill] 解档失败:", msg)
        return 2
    r1 = verify_against_manifest(man, dest)
    print(f"[drill] 还原#1 源 vs 副本: {r1[1]} => {'✅逐项一致' if r1[0] else '❌不一致'}")
    if not r1[0]:
        print("  明细:", json.dumps(r1[2], ensure_ascii=False))
        return 1

    shutil.rmtree(dest)                                     # 删掉解出的副本
    ok2, msg = _extract(arc, dest)                          # 归档自足性证明
    r2 = verify_against_manifest(man, dest)
    print(f"[drill] 还原#2 删副本后重新解档: {r2[1]} => {'✅逐项一致' if r2[0] else '❌不一致'}")
    if not r2[0]:
        return 1

    neg_ok = None
    if a.negctl:
        r3 = verify_against_manifest(man, dest, tamper=True)
        neg_ok = not r3[0]
        print(f"[drill] 负控（篡改副本一字节）: {'✅已报不一致' if neg_ok else '❌未报错=校验失效'} "
              f"| 不一致 {r3[1]['mismatch']} 项")

    print(f"[drill] 归档 {arc.name} {arc.stat().st_size/1048576:.1f} MiB | 归档 sha256 {sha256_file(arc)}")
    print(f"[drill] 结论: ✅归档可独立还原，逐文件 sha256+计数一致"
          + (f"；负控 {'通过' if neg_ok else '失败'}" if a.negctl else "")
          + f" | 耗时 {time.time()-t0:.1f}s")
    if a.keep_work:
        print(f"[drill] 沙箱保留在 {dest}")
    else:
        shutil.rmtree(WORK, ignore_errors=True)
        print(f"[drill] 沙箱 {WORK} 已清理")
    return 0


# ---------------------------------------------------------------- list

def _git_tracked():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout
        return set(out.splitlines())
    except Exception:
        return set()


def _sz(p: Path) -> int:
    """体积读取容错：temp/ 下常有瞬时文件（抓取中途消失）⇒ 不因单个 OSError 崩掉整轮"""
    try:
        return p.stat().st_size
    except OSError:
        return 0


def cmd_list(a) -> int:
    pairs, per_set = collect()
    total = sum(_sz(p) for p, _ in pairs)
    print("== T0 集合（丢了就没了）==")
    for spec in SETS:
        got = per_set[spec["name"]]
        sz = sum(_sz(p) for p, _ in got)
        print(f"  {spec['name']:<18} {len(got):>4} 文件 {sz/1048576:>8.1f} MiB   {spec['path']}")
    print(f"  {'TOTAL':<18} {len(pairs):>4} 文件 {total/1048576:>8.1f} MiB")
    print("  体积最大 8 项:")
    for p, arc in sorted(pairs, key=lambda x: -_sz(x[0]))[:8]:
        print(f"    {_sz(p)/1048576:>8.1f} MiB  {arc}")

    if a.temp_scan:
        tracked = _git_tracked()
        now = time.time()
        print("\n== temp/ 顶层：哪些必须离开 temp/（按体积）==")
        print(f"  {'条目':<28}{'体积MiB':>9}{'文件数':>7}{'7天改':>7}  {'git':<9}判读")
        rows = []
        try:
            entries = list((ROOT / "temp").iterdir())
        except OSError:
            entries = []
        for d in sorted(entries, key=lambda p: -_du(p)):
            if d.name in {"t0_backups"}:
                continue
            sz = _du(d)
            if d.is_dir():
                n = recent = 0
                for dp, dn, fn in os.walk(d):
                    dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES]
                    for f in fn:
                        n += 1
                        try:
                            if now - (Path(dp) / f).stat().st_mtime < 7 * 86400:
                                recent += 1
                        except OSError:
                            pass
            else:
                try:
                    n, recent = 1, int(now - d.stat().st_mtime < 7 * 86400)
                except OSError:
                    continue
            rel = str(d.relative_to(ROOT))
            g = "tracked" if rel in tracked else "no"
            rows.append((rel, sz, n, recent, g))
        for rel, sz, n, recent, g in rows[: a.top]:
            verdict = "✅需离机(已确认)" if rel in ("temp/kc_snapshots", "temp/pending_decisions") else \
                      ("⧗待判(7天活跃+未入库)" if recent and g == "no" else "—")
            print(f"  {rel:<28}{sz/1048576:>9.1f}{n:>7}{recent:>7}  {g:<9}{verdict}")
        print("  判读=启发式信号，非结论：「需离机」仅标注已确认不可再生的条目；其余未判定。")
    return 0


def _du(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    tot = 0
    for dp, dn, fn in os.walk(p):
        dn[:] = [x for x in dn if x not in EXCLUDE_DIR_NAMES]
        for f in fn:
            try:
                tot += (Path(dp) / f).stat().st_size
            except OSError:
                pass
    return tot


# ---------------------------------------------------------------- rotate

def cmd_rotate(a) -> int:
    out = Path(a.out) if a.out else DEFAULT_OUT
    # 归档「单元」=一个基准名（ga-t0-<tag>-<stamp>.tar.gz），其明文/.enc/清单/SHA256SUMS 同进退
    units = {}
    for p in list(out.glob("ga-t0-*.tar.gz")) + list(out.glob("ga-t0-*.tar.gz.enc")):
        base = Path(str(p)[:-4]) if str(p).endswith(".enc") else p
        key = str(base)
        units[key] = max(units.get(key, 0.0), p.stat().st_mtime)
    order = sorted(units.items(), key=lambda kv: kv[1], reverse=True)

    def _files(base_str):
        return [base_str, base_str + ".enc", base_str + ".manifest.json", base_str + ".SHA256SUMS"]

    removed = 0
    for base_str, _ in order[a.keep:]:
        for f in _files(base_str):
            fp = Path(f)
            if fp.exists():
                fp.unlink()
        removed += 1
    # 孤儿清单：base 与其 .enc 都不存在 ⇒ 无人可校验，清理
    orphans = 0
    for p in list(out.glob("ga-t0-*.manifest.json")) + list(out.glob("ga-t0-*.SHA256SUMS")):
        base = Path(str(p).rsplit(".manifest.json", 1)[0].rsplit(".SHA256SUMS", 1)[0])
        if not base.exists() and not Path(str(base) + ".enc").exists():
            p.unlink()
            orphans += 1
    print(f"[rotate] 归档单元 {len(order)} 个（明文+.enc 同组）| 保留 {min(len(order), a.keep)} "
          f"| 删除 {removed} | 清理孤儿清单 {orphans} -> {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="T0 关键资产快照（只读源数据）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--out", default=None)
    b.add_argument("--tag", default=None)
    b.add_argument("--only", default=None, help="只归档该相对路径（前缀），用于单目录演练")
    b.add_argument("--encrypt", action="store_true", help="需 env T0_PASSPHRASE")
    b.add_argument("--keep-plain", action="store_true", help="加密后仍保留明文 tar.gz（默认删）")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify")
    v.add_argument("archive")
    v.add_argument("--encrypt", action="store_true")
    v.add_argument("--negctl", action="store_true", help="故意篡改副本，验证校验器能报错")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("drill")
    d.add_argument("--out", default=None)
    d.add_argument("--tag", default=None)
    d.add_argument("--only", default=None)
    d.add_argument("--negctl", action="store_true")
    d.add_argument("--keep-work", action="store_true")
    d.set_defaults(func=cmd_drill)

    l = sub.add_parser("list")
    l.add_argument("--temp-scan", action="store_true", help="附带 temp/ 顶层「必须离开」清单")
    l.add_argument("--top", type=int, default=12)
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("rotate")
    r.add_argument("--out", default=None)
    r.add_argument("--keep", type=int, default=3)
    r.set_defaults(func=cmd_rotate)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
