#!/usr/bin/env python3
"""audio_vad_scan.py — 纯 VAD 语音占比扫描（挑人声的便宜筛子，不跑 ASR）

口径：silero VAD（faster-whisper 自带），min_silence_duration_ms=500
输出：逐条 duration / speech_sec / speech_ratio / n_segments / longest_speech / vad_sec
用途：先筛，再只对 speech_ratio≥阈值的条目跑 audio_asr_batch.py（VAD 比 ASR 便宜约 2 个数量级）
用法: /opt/anaconda3/bin/python3 tools/audio_vad_scan.py [--assets temp/audio_assets]
      [--inventory ...] [--out ...] [--wavdir ...] [--min-silence-ms 500] [--only N]
"""
from __future__ import annotations

import argparse, json, subprocess, sys, time, wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "memory"))
from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default=str(REPO / "temp" / "audio_assets"))
    ap.add_argument("--inventory", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--wavdir", default="")
    ap.add_argument("--min-silence-ms", type=int, default=500)
    ap.add_argument("--only", type=int, default=0)
    a = ap.parse_args()

    ASSETS = Path(a.assets)
    WAV = Path(a.wavdir) if a.wavdir else ASSETS / "wav_vad"
    WAV.mkdir(parents=True, exist_ok=True)
    OUTP = Path(a.out) if a.out else ASSETS / "vad_scan.json"
    inv = json.load(open(Path(a.inventory) if a.inventory else ASSETS / "inventory.json",
                         encoding="utf-8"))
    items = [r for r in inv["items"] if r["has_audio"] and r.get("duration")]
    items.sort(key=lambda r: -(r["duration"] or 0))
    if a.only:
        items = items[:a.only]

    wavs = {}
    for r in items:
        w = WAV / (Path(r["path"]).stem + ".16k.wav")
        if not w.exists():
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", r["path"],
                            "-ar", "16000", "-ac", "1", str(w)], check=False)
        wavs[r["path"]] = w

    opts = VadOptions(min_silence_duration_ms=a.min_silence_ms)
    out = []
    for i, r in enumerate(items, 1):
        w = wavs[r["path"]]
        if not w.exists():
            out.append({"path": r["path"], "err": "no wav"}); print(i, "no wav", flush=True); continue
        with wave.open(str(w)) as f:
            sr = f.getframerate(); audio = np.frombuffer(f.readframes(f.getnframes()), np.int16)
        audio = audio.astype(np.float32) / 32768.0
        t = time.time()
        segs = get_speech_timestamps(audio, opts, sampling_rate=sr)
        dur = len(audio) / sr
        sp = sum(s["end"] - s["start"] for s in segs) / sr
        out.append({"path": r["path"], "name": r["name"], "duration": round(dur, 1),
                    "speech_sec": round(sp, 1), "speech_ratio": round(sp / dur, 4) if dur else 0,
                    "n_segments": len(segs),
                    "longest_speech": round(max([(s["end"] - s["start"]) / sr for s in segs], default=0), 1),
                    "vad_sec": round(time.time() - t, 1)})
        print(f"[{i}/{len(items)}] {r['name']} dur={dur:.0f}s speech_ratio={out[-1]['speech_ratio']} "
              f"n={len(segs)} vad={out[-1]['vad_sec']}s", flush=True)
    json.dump(out, open(OUTP, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("VAD_SCAN_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
