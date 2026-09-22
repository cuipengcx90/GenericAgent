#!/usr/bin/env python3
"""silk_to_text.py — 微信/QQ 语音 `.silk` → wav → 文字（本机已实证链路，2026-09-22 POC）

链路：
    .silk --pilk.decode(pcm_rate=24000)--> 24kHz mono PCM --wave--> .wav
          --memory/whisper_utils.transcribe--> 文本

实测样例（公开真实样本，非自造）：
    hello.silk 3615B → pcm 94080B → wav 94124B / 1960ms → 「你好」(zh)
    test.silk  2517B → pcm 70080B → wav 70124B / 1460ms → 「It's up to you.」(en)
    来源: github.com/fishCoder/Silk_v3_decoder  app/src/main/assets/{hello,test}.silk

依赖：pilk（已装 0.2.4，含 C 扩展 SilkDecoder/SilkEncoder）；本机 ffmpeg **无** silk 解码器，故不用 ffmpeg 解码。
     转写用 memory/whisper_utils.py（faster-whisper）。

用法：
    python tools/silk_to_text.py decode  a.silk [b.silk ...] [-o OUTDIR]   # 解码→wav(+pcm)，打印字节数/时长
    python tools/silk_to_text.py text    a.wav|a.silk [-m tiny] [--lang zh] # 转写（给 .silk 会先解码）
    python tools/silk_to_text.py batch   DIR [-m tiny]                      # 目录内全部 .silk → OUTDIR/wav + 文本
    python tools/silk_to_text.py probe   DIR                               # 只读统计 .silk 数量/大小分布（不复制、不写入）

纪律（勿破）：
  1) 只处理真实 silk（头 `\\x02#!SILK_V3` 或 `#!SILK`）——头不对直接报错退出，不做任何猜测式转换；
  2) 不下载/不生成"假样本"充当证据；probe 只读，绝不改动源目录；
  3) 输出目录默认 `--out`，或与输入同级的 `out/`，从不写回源；
  4) 长目录扫描请用 probe（限时），不要在 ~/Library/Containers 下递归 find（TCC 会挂起）。

坑（踩过）：
  * `pilk.decode()` 的**返回值不可信**（实测返回 2 / 1 ms）；请用 `pilk.get_duration()` 取真实时长。
  * 不要把 anaconda `site-packages` 插到 sys.path[0]：那里有 vendored 老 `pathlib.py`，会 `from collections import Sequence` 崩。
  * 微信 4.x 桌面端容器内**没有**语音目录（msg/ 只有 attach/file/migrate/video），本工具面向"拿到 .silk 之后"。
"""
from __future__ import annotations

import argparse
import os
import sys
import wave
from pathlib import Path

MAGIC = (b"\x02#!SILK_V3", b"#!SILK_V3", b"#!SILK")
PCM_RATE = 24000


def _check_magic(p: Path) -> tuple[bool, str]:
    with open(p, "rb") as f:
        head = f.read(12)
    for m in MAGIC:
        if head.startswith(m):
            return True, m.decode("latin-1").replace("\x02", "\\x02")
    return False, head[:10].hex()


def decode_one(src: Path, outdir: Path) -> dict:
    import pilk  # 延迟导入：probe 场景无需依赖

    src = Path(src)
    ok, tag = _check_magic(src)
    if not ok:
        raise ValueError(f"{src} 不是 silk 文件（头={tag}）——按纪律拒绝转换")
    outdir.mkdir(parents=True, exist_ok=True)
    wav_p = outdir / (src.stem + ".wav")
    pcm_p = outdir / (src.stem + ".pcm")
    pilk.decode(str(src), str(pcm_p), pcm_rate=PCM_RATE)  # 返回值不可信，时长另取
    raw = pcm_p.read_bytes()
    with wave.open(str(wav_p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(PCM_RATE)
        w.writeframes(raw)
    dur_ms = pilk.get_duration(str(src))
    return {
        "silk": src.name,
        "silk_bytes": src.stat().st_size,
        "magic": tag,
        "pcm_bytes": len(raw),
        "wav_bytes": wav_p.stat().st_size,
        "dur_ms": dur_ms,
        "wav": str(wav_p),
    }


def transcribe(wav_or_silk: Path, model_size="tiny", language=None) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
    import whisper_utils  # type: ignore

    p = Path(wav_or_silk)
    if p.suffix.lower() != ".wav":
        info = decode_one(p, p.parent / "out")
        p = Path(info["wav"])
    out = whisper_utils.transcribe(str(p), model_size=model_size, language=language)
    out["wav"] = str(p)
    return out


def probe(dirpath: Path, max_files=20000) -> dict:
    """只读统计：递归 no-follow 扫描目录，统计 .silk 数量/字节分布（不复制、不写入）。"""
    dirpath = Path(dirpath)
    sizes, exts = [], {}
    n = 0
    for root, _dirs, files in os.walk(dirpath, followlinks=False):
        for f in files:
            n += 1
            if n > max_files:
                return {"aborted": True, "scanned": n}
            ext = Path(f).suffix.lower()
            exts[ext] = exts.get(ext, 0) + 1
            if ext in (".silk", ".slk"):
                try:
                    sizes.append((Path(root) / f).stat().st_size)
                except OSError:
                    pass
    return {
        "dir": str(dirpath),
        "scanned": n,
        "silk_count": len(sizes),
        "silk_bytes_total": sum(sizes),
        "silk_bytes_min": min(sizes) if sizes else None,
        "silk_bytes_max": max(sizes) if sizes else None,
        "ext_hist": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:12]),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="silk → wav → 文字")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode", help=".silk → wav(+pcm)")
    d.add_argument("files", nargs="+")
    d.add_argument("-o", "--out")

    t = sub.add_parser("text", help="转写（.wav 或 .silk）")
    t.add_argument("files", nargs="+")
    t.add_argument("-m", "--model", default="tiny")
    t.add_argument("--lang", default=None)

    b = sub.add_parser("batch", help="目录内全部 .silk → wav + 文本")
    b.add_argument("dir")
    b.add_argument("-m", "--model", default="tiny")
    b.add_argument("--lang", default=None)
    b.add_argument("-o", "--out")

    p = sub.add_parser("probe", help="只读统计 .silk")
    p.add_argument("dir")

    a = ap.parse_args(argv)

    if a.cmd == "decode":
        rc = 0
        for f in a.files:
            src = Path(f)
            try:
                info = decode_one(src, Path(a.out) if a.out else src.parent / "out")
            except Exception as e:
                print(f"{src.name}: 拒绝/失败 -> {e}")
                rc = 3
                continue
            print(f"{info['silk']}: {info['silk_bytes']}B (magic {info['magic']}) -> pcm {info['pcm_bytes']}B "
                  f"-> wav {info['wav_bytes']}B | {info['dur_ms']}ms | {info['wav']}")
        return rc

    if a.cmd == "text":
        for f in a.files:
            r = transcribe(Path(f), model_size=a.model, language=a.lang)
            print(f"{Path(f).name} | wav={r['wav']} | lang={r['language']}({r['language_prob']}) "
                  f"| {r['elapsed']}s | text={r['text']!r}")
        return 0

    if a.cmd == "batch":
        root = Path(a.dir)
        outdir = Path(a.out) if a.out else root / "out"
        silks = sorted([p for p in root.rglob("*.silk") if p.is_file()])
        print(f"发现 {len(silks)} 个 .silk in {root}")
        for s in silks:
            try:
                info = decode_one(s, outdir)
                r = transcribe(Path(info["wav"]), model_size=a.model, language=a.lang)
                print(f"{s.name}: {info['silk_bytes']}B -> {info['wav_bytes']}B | {info['dur_ms']}ms "
                      f"| {r['language']} | {r['text']!r}")
            except Exception as e:  # 单文件失败不中断整批
                print(f"{s.name}: SKIP ({e})")
        return 0

    if a.cmd == "probe":
        import json
        print(json.dumps(probe(Path(a.dir)), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
