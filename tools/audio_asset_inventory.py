#!/usr/bin/env python3
"""audio_asset_inventory.py — 本机音视频资产盘点（去重 + 音轨判定）

用途：扫描个人目录里的音视频文件，做 ①字节级去重（sha256）②音轨/时长探测（ffprobe）
     ③产出 JSON + Markdown 清单，供「挑人声 → 长音频转写」下游挑选素材。

判定口径（写死，便于复核）：
  - 去重键 = sha256 全字节哈希；同哈希视为**同一份**，只保留「路径最短/最先扫描到」的一条
  - has_audio = ffprobe 输出里存在 codec_type=audio 的流（缺 ffprobe 或文件坏 → None，不算 True）
  - 时长取 format.duration（秒，浮点），拿不到记 null
  - 只做只读探测，不移动/删除/改名任何文件

用法: python3 audio_asset_inventory.py [--bases ~/Downloads,~/Documents,~/Desktop,~/Movies]
                                      [--depth 2] [--out temp/audio_assets]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".aiff", ".aif", ".wma", ".opus", ".silk"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts", ".mpeg", ".mpg", ".3gp"}


def sha256_of(p: Path, limit_mb: int = 512) -> str | None:
    if p.stat().st_size > limit_mb * 1024 * 1024:
        return None  # 超大件跳过哈希（本项目无此类，留兜底）
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(p: Path) -> dict:
    """ffprobe 探测：音轨流 + 时长 + 容器。失败返回 has_audio=None 并记 err。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, text=True, timeout=30)
    except Exception as e:  # 超时/缺 ffprobe
        return {"has_audio": None, "duration": None, "fmt": None, "audio_codec": None,
                "err": f"{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"has_audio": None, "duration": None, "fmt": None, "audio_codec": None,
                "err": (r.stderr or "").strip()[:200]}
    try:
        d = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"has_audio": None, "duration": None, "fmt": None, "audio_codec": None,
                "err": f"parse: {e}"}
    streams = d.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = (d.get("format") or {}).get("format_name")
    dur = (d.get("format") or {}).get("duration")
    try:
        dur = round(float(dur), 1) if dur is not None else None
    except Exception:
        dur = None
    return {"has_audio": bool(audio), "duration": dur, "fmt": fmt,
            "audio_codec": (audio[0].get("codec_name") if audio else None), "err": None}


def walk(base: Path, depth: int) -> list[Path]:
    """限深遍历（只读）。depth=2 表示 base 及其两层子目录。"""
    out, base = [], base.expanduser()
    if not base.exists():
        return out
    stack = [(base, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            entries = list(os.scandir(d))
        except (PermissionError, OSError):
            continue
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False):
                    if Path(e.name).suffix.lower() in (AUDIO_EXT | VIDEO_EXT):
                        out.append(Path(e.path))
                elif e.is_dir(follow_symlinks=False) and lvl < depth and not e.name.startswith("."):
                    stack.append((Path(e.path), lvl + 1))
            except OSError:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="~/Downloads,~/Documents,~/Desktop,~/Movies")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--out", default="temp/audio_assets")
    a = ap.parse_args()

    bases = [Path(x.strip()) for x in a.bases.split(",") if x.strip()]
    files: list[Path] = []
    for b in bases:
        files += walk(b, a.depth)
    files = sorted(set(files))

    rows = []
    for p in files:
        st = p.stat()
        info = probe(p)
        rows.append({
            "path": str(p),
            "name": p.name,
            "ext": p.suffix.lower(),
            "kind": "audio" if p.suffix.lower() in AUDIO_EXT else "video",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "sha256": sha256_of(p),
            **info,
        })

    # 去重：同 sha256 只留第一条（按 path 排序稳定）
    seen, uniq, dups = {}, [], []
    for r in rows:
        k = r["sha256"] or f"__nosha__{r['path']}"
        if k in seen:
            dups.append({"path": r["path"], "dup_of": seen[k], "sha256": r["sha256"], "size": r["size"]})
            continue
        seen[k] = r["path"]
        uniq.append(r)

    # 非媒体误命中（扩展名像音视频但 ffprobe 完全解析不了，如 TypeScript 的 .ts 配置）
    not_media = [r for r in uniq if r["has_audio"] is None and not r["duration"]]
    uniq = [r for r in uniq if r not in not_media]
    no_audio = [r for r in uniq if r["has_audio"] is False]
    with_a = [r for r in uniq if r["has_audio"]]

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "inventory.json").write_text(json.dumps(
        {"bases": [str(b) for b in bases], "depth": a.depth,
         "scanned": len(rows), "unique": len(uniq),
         "unique_incl_not_media": len(uniq) + len(not_media),
         "dups": dups, "items": uniq},
        ensure_ascii=False, indent=2), encoding="utf-8")

    def md_table(rs):
        lines = ["| # | 文件 | 类型 | 字节 | 时长s | 音轨 | 编码 | 路径 |", "|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(sorted(rs, key=lambda x: -(x["duration"] or 0)), 1):
            lines.append(f"| {i} | {r['name']} | {r['kind']} | {r['size']} | "
                         f"{r['duration'] if r['duration'] is not None else '—'} | "
                         f"{'✅' if r['has_audio'] else ('❌' if r['has_audio'] is False else '?')} | "
                         f"{r['audio_codec'] or '—'} | {r['path']} |")
        return "\n".join(lines)

    md = [f"# 本机音视频资产清单（只读探测）", "",
          f"- 扫描根：{', '.join(str(b) for b in bases)}（限深 {a.depth}）",
          f"- 命中文件 {len(rows)} → 去重后 **{len(uniq) + len(not_media)}**"
          f"（去重含非媒体；可播/可转写口径 **{len(uniq)}**；重复件 {len(dups)}）",
          f"- 含音轨 **{len(with_a)}**；无音轨 **{len(no_audio)}**；探测失败 {len(uniq)-len(with_a)-len(no_audio)}", "",
          "## 全部（按时长降序）", "", md_table(uniq), ""]
    if dups:
        md += ["## 字节重复件（sha256 相同，已折叠）", ""]
        md += [f"- `{d['path']}` ≡ `{d['dup_of']}`（{d['size']}B）" for d in dups]
        md += [""]
    if not_media:
        md += ["## 非媒体误命中（扩展名/内容不匹配，已剔除）", ""] + \
              [f"- `{r['path']}`（{r['size']}B）— {r['err'] or '无音视频流'}" for r in not_media] + [""]
    if no_audio:
        md += ["## 无音轨（不可转写）", ""] + [f"- `{r['path']}`（{r['size']}B, {r['fmt']}）" for r in no_audio] + [""]
    (outdir / "inventory.md").write_text("\n".join(md), encoding="utf-8")

    print(f"scanned={len(rows)} unique={len(uniq)} dups={len(dups)} "
          f"with_audio={len(with_a)} no_audio={len(no_audio)} not_media={len(not_media)}")
    print("top by duration:")
    for r in sorted(with_a, key=lambda x: -(x["duration"] or 0))[:8]:
        print(f"  {r['duration']:>7.1f}s  {r['size']:>10}B  {r['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
