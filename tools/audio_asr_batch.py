#!/usr/bin/env python3
"""audio_asr_batch.py — 批量语音转写（本机音视频资产 → 逐条文本 + 质量指标）

口径（写死）：
  - 模型: faster-whisper `small` / cpu / int8（离线，模型已在本机 HF 缓存）
  - 语言: 自动检测（不预设 zh），记录 language + language_prob
  - 质量: 每个 segment 取 avg_logprob / no_speech_prob
        · speech_ratio = Σ(seg.end-seg.start) / 音频总时长   （人声覆盖占比）
        · silence_ratio = 1 - speech_ratio                  （静音/纯音乐/无效段占比）
        · conf = exp(mean(avg_logprob))                     （段平均置信度, 0~1）
        · mean_no_speech = mean(no_speech_prob)
  - 幂等: 已存在结果 JSON 则跳过（--force 重跑），进度写入 asr_progress.jsonl
用法: /opt/anaconda3/bin/python3 audio_asr_batch.py [--model small] [--beam 1] [--threads 8]
      [--assets temp/audio_assets] [--inventory ...] [--outdir ...] [--force] [--only N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ASSETS = REPO / "temp" / "audio_assets"   # 默认资产目录（--assets 覆盖）
OUT = ASSETS / "asr"
MEDIA_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".aiff", ".aif", ".wma", ".opus",
             ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts", ".mpeg", ".mpg", ".3gp"}


def safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)[:80]


def to_wav(p: Path, wavdir: Path) -> Path:
    if p.suffix.lower() == ".wav":
        return p
    w = wavdir / f"{safe(p.stem)}.16k.wav"
    if not w.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(p),
                        "-ar", "16000", "-ac", "1", str(w)], check=True, timeout=600)
    return w


def main() -> int:
    global ASSETS, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="small")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--no-vad", action="store_true", help="关闭 VAD 过滤（默认开）")
    ap.add_argument("--assets", default=str(ASSETS), help="资产目录（含 inventory.json）")
    ap.add_argument("--inventory", default="", help="清单 JSON 路径（默认 <assets>/inventory.json）")
    ap.add_argument("--outdir", default="", help="结果目录（默认 <assets>/asr）")
    a = ap.parse_args()

    ASSETS = Path(a.assets)
    OUT = Path(a.outdir) if a.outdir else ASSETS / "asr"
    INVP = Path(a.inventory) if a.inventory else ASSETS / "inventory.json"
    inv = json.loads(INVP.read_text(encoding="utf-8"))
    items = [r for r in inv["items"] if r["has_audio"] and r["ext"] in MEDIA_EXT]
    # 排除 ffprobe 完全解析失败的误命中（如 TypeScript .ts 配置文件）
    items = [r for r in items if r.get("duration")]
    items.sort(key=lambda r: -(r["duration"] or 0))
    if a.only:
        items = items[:a.only]

    OUT.mkdir(parents=True, exist_ok=True)
    wavdir = ASSETS / "wav"
    wavdir.mkdir(exist_ok=True)
    prog = ASSETS / "asr_progress.jsonl"

    # 模型口径与 memory/asr_cli.py 完全一致：同一 _get_model(small, cpu, int8)
    sys.path.insert(0, str(HERE.parent / "memory"))
    from whisper_utils import _get_model
    t_load = time.time()
    import faster_whisper
    _orig = faster_whisper.WhisperModel.__init__
    def _init(self, *ar, **kw):
        kw.setdefault("cpu_threads", a.threads)
        return _orig(self, *ar, **kw)
    faster_whisper.WhisperModel.__init__ = _init   # 让 _get_model 也走 8 线程
    model = _get_model(a.model, device="cpu", compute_type="int8")
    load_s = round(time.time() - t_load, 1)
    print(f"[model] {a.model} loaded in {load_s}s; items={len(items)}", flush=True)

    for i, r in enumerate(items, 1):
        src = Path(r["path"])
        res_path = OUT / f"{safe(src.stem)}.json"
        if res_path.exists() and not a.force:
            print(f"[{i}/{len(items)}] skip (done) {src.name}", flush=True)
            continue
        rec = {"path": str(src), "name": src.name, "duration": r["duration"],
               "model": a.model, "beam": a.beam, "vad": (not a.no_vad),
               "threads": a.threads, "load_s": load_s}
        try:
            wav = to_wav(src, wavdir)
            segs_iter, info = model.transcribe(
                str(wav), beam_size=a.beam, language=None,
                vad_filter=not a.no_vad,
                vad_parameters={"min_silence_duration_ms": 500})
            segs = []
            for s in segs_iter:
                segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                             "text": s.text.strip(),
                             "avg_logprob": round(float(getattr(s, "avg_logprob", 0.0)), 4),
                             "no_speech_prob": round(float(getattr(s, "no_speech_prob", 0.0)), 4)})
            speech = sum(max(0.0, s["end"] - s["start"]) for s in segs)
            dur = r["duration"] or 0.0
            lps = [s["avg_logprob"] for s in segs] or [0.0]
            nsp = [s["no_speech_prob"] for s in segs] or [0.0]
            rec.update({
                "ok": True,
                "language": info.language, "language_prob": round(info.language_probability, 4),
                "text": " ".join(s["text"] for s in segs),
                "segments": segs, "n_segments": len(segs),
                "speech_sec": round(speech, 1),
                "speech_ratio": round(speech / dur, 4) if dur else None,
                "silence_ratio": round(1 - speech / dur, 4) if dur else None,
                "conf": round(math.exp(sum(lps) / len(lps)), 4),
                "mean_no_speech": round(sum(nsp) / len(nsp), 4),
                "chars": len("".join(s["text"] for s in segs)),
            })
        except Exception as e:
            rec.update({"ok": False, "err": f"{type(e).__name__}: {e}"})
        rec["wall_s"] = round(time.time() - t_load, 1)
        res_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(prog, "a", encoding="utf-8") as f:
            f.write(json.dumps({k: rec.get(k) for k in
                                ("name", "ok", "duration", "speech_ratio", "conf",
                                 "mean_no_speech", "n_segments", "language", "chars", "err")},
                               ensure_ascii=False) + "\n")
        st = "OK " if rec.get("ok") else "ERR"
        print(f"[{i}/{len(items)}] {st} {src.name} dur={rec.get('duration')}s "
              f"speech={rec.get('speech_ratio')} conf={rec.get('conf')} "
              f"segs={rec.get('n_segments')} lang={rec.get('language')} "
              f"chars={rec.get('chars')} {rec.get('err') or ''}", flush=True)
    print("[done] all files processed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
