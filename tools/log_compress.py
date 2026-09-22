#!/usr/bin/env python3
"""
log_compress.py — 长跑日志压缩归档器（copytruncate 模式）

用途
----
压缩由 launchd `StandardOutPath` 重定向持有的长跑日志（默认
`logs/autonomous_reflect.log`）：把历史内容 gzip 归档后，只把日志尾部
若干字节保留回原文件，从而在不打断/不重启写入进程的前提下压低日志体积。

为什么是安全的（copytruncate）
------------------------------
launchd 的 StandardOutPath 以 **O_APPEND** 打开。实测证据（2026-09-22）：
该日志跨越 82 次进程启动、含 2026-01-02 → 09-22 共 188 个不同日期的内容，
且文件最头部仍保留着最早的内容 ⇒ 既不是 O_TRUNC（否则每次重启清空），
也不是无 O_APPEND 的原地覆写（否则新进程从 offset 0 覆写，文件不会单调增长）。
因此本脚本把文件截短后，持有 fd 的写入者下一次 write() 会被内核重新定位到
新的 EOF，**不会产生稀疏空洞 / NUL 填充**。

并发写入的残余风险
------------------
读快照 → 归档 → 回写尾部 → 截断 之间存在微秒级窗口，窗口内新追加的行可能
既不进归档也不留在原位（copytruncate 的固有代价，与 logrotate 相同）。
脚本会在截断前做一次“追读”把窗口尽量收窄；日志属可再生的运行输出，可接受。

用法
----
    python3 tools/log_compress.py                  # 默认目标，超阈值才动作
    python3 tools/log_compress.py --force          # 忽略阈值强制压缩
    python3 tools/log_compress.py --dry-run        # 只报告不落盘
    python3 tools/log_compress.py --log PATH --archive-dir DIR

环境变量
--------
    GA_LOG_COMPRESS_THRESHOLD  触发阈值（字节），默认 8 MiB
    GA_LOG_COMPRESS_KEEP       保留尾部（字节），默认 1 MiB

退出码
------
    0 = 成功（含“未达阈值，跳过”）
    1 = 失败（日志不存在 / 写入校验失败等）
"""

from __future__ import annotations

import argparse
import gzip
import os
import sys
from datetime import datetime
from pathlib import Path

# ── 默认路径（相对本脚本所在仓库根，避免依赖调用方 cwd） ──
_REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG = _REPO_DIR / "logs" / "autonomous_reflect.log"
DEFAULT_ARCHIVE_DIR = _REPO_DIR / "logs" / "archive"

DEFAULT_THRESHOLD = 8 * 1024 * 1024   # 8 MiB
DEFAULT_KEEP = 1 * 1024 * 1024        # 1 MiB


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _unique_archive_path(archive_dir: Path, stem: str) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    cand = archive_dir / f"{stem}.{ts}.log.gz"
    seq = 1
    while cand.exists():
        cand = archive_dir / f"{stem}.{ts}.{seq}.log.gz"
        seq += 1
    return cand


def _align_to_line_start(data: bytes, keep_start: int) -> int:
    """把保留起点对齐到行首，避免截断出半行。"""
    if keep_start <= 0:
        return 0
    nl = data.find(b"\n", keep_start)
    if nl == -1:
        return len(data)  # 找不到换行：整段都是一行，全部保留
    return nl + 1


def compress(log_path: Path, archive_dir: Path, threshold: int, keep: int,
             force: bool = False, dry_run: bool = False) -> tuple[int, str]:
    """执行一次 copytruncate 压缩。返回 (rc, message)。"""
    if not log_path.exists():
        return 1, f"[ERROR] 日志不存在: {log_path}"

    size = log_path.stat().st_size
    if size <= threshold and not force:
        return 0, f"跳过：{_human(size)} 未达阈值 {_human(threshold)}"

    with open(log_path, "rb") as f:
        snapshot = f.read()

    keep_start = _align_to_line_start(snapshot, len(snapshot) - keep)
    prefix, tail = snapshot[:keep_start], snapshot[keep_start:]

    if not prefix:
        return 0, f"跳过：可归档前缀为空（size={_human(len(snapshot))}）"

    if dry_run:
        return 0, (f"[dry-run] 将归档 {_human(len(prefix))} → gzip，"
                   f"日志 {_human(len(snapshot))} → {_human(len(tail))}")

    # 1) 归档被移除的前缀
    archive_path = _unique_archive_path(archive_dir, log_path.name)
    try:
        with gzip.open(archive_path, "wb", compresslevel=6) as gz:
            gz.write(prefix)
    except Exception as e:  # 归档失败 → 绝不改动原日志
        return 1, f"[ERROR] 归档写入失败，未改动日志: {e}"

    # 2) 追读窗口：截断前再读一次期间新追加的字节并入尾部
    try:
        with open(log_path, "rb") as f:
            f.seek(len(snapshot))
            extra = f.read()
    except Exception:
        extra = b""
    tail_full = tail + extra

    # 3) copytruncate：尾部写回文件头，再截断
    try:
        fd = os.open(str(log_path), os.O_RDWR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, tail_full)
            os.ftruncate(fd, len(tail_full))
        finally:
            os.close(fd)
    except Exception as e:
        return 1, f"[ERROR] 回写截断失败（归档已保存: {archive_path.name}）: {e}"

    # 4) 校验
    try:
        new_size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            check = f.read(len(tail_full))
    except Exception as e:
        return 1, f"[ERROR] 校验读取失败: {e}"

    if new_size != len(tail_full) or check != tail_full:
        return 1, (f"[ERROR] 校验不一致: size={new_size} expected={len(tail_full)} "
                   f"（归档仍保留: {archive_path.name}）")

    gz_bytes = archive_path.stat().st_size
    saved = len(snapshot) - new_size
    return 0, (f"{_human(len(snapshot))} → {_human(new_size)} "
               f"(保留尾部 {_human(new_size)}, 释放 {_human(saved)}, "
               f"归档 {archive_path.name} {_human(gz_bytes)})")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("os.write returned 0")
        view = view[n:]


def _human(n: int) -> str:
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.2f}{unit}"
    return f"{n}B"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="copytruncate 长跑日志压缩归档器")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="目标日志路径")
    ap.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR,
                    help="gzip 归档目录")
    ap.add_argument("--threshold", type=int,
                    default=_env_int("GA_LOG_COMPRESS_THRESHOLD", DEFAULT_THRESHOLD),
                    help="触发阈值(字节)")
    ap.add_argument("--keep", type=int,
                    default=_env_int("GA_LOG_COMPRESS_KEEP", DEFAULT_KEEP),
                    help="保留尾部(字节)")
    ap.add_argument("--force", action="store_true", help="忽略阈值强制压缩")
    ap.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    args = ap.parse_args(argv)

    rc, msg = compress(args.log, args.archive_dir, args.threshold, args.keep,
                       force=args.force, dry_run=args.dry_run)
    (print if rc == 0 else (lambda m: print(m, file=sys.stderr)))(msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
