#!/usr/bin/env python3
"""asr_cli.py — 格式无关语音转写薄封装（30 行）
依赖: ffmpeg + whisper_utils(需 faster-whisper ⇒ 解释器必须 /opt/anaconda3/bin/python3)
用法: /opt/anaconda3/bin/python3 asr_cli.py <音频> [--model small] [--lang zh] [--srt out.srt]
支持: wav/aiff/m4a/mp3/mp4/flac/ogg（非 wav 自动 ffmpeg 转 16k 单声道）
限制: 微信 .silk 语音 ffmpeg 不认，需 silk-v3-decoder 预转 wav 再喂本工具。
"""
import pathlib, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import whisper_utils  # noqa: E402


def to_wav(p):
    p = pathlib.Path(p)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.suffix.lower() == ".wav":
        return p
    w = pathlib.Path(tempfile.gettempdir()) / f"asr_{p.stem}_16k.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(p),
                    "-ar", "16000", "-ac", "1", str(w)], check=True)
    return w


def transcribe_any(p, model="small", lang=None):
    return whisper_utils.transcribe(to_wav(p), model_size=model, language=lang)


if __name__ == "__main__":
    a = sys.argv[1:]
    g = lambda k, d=None: a[a.index(k) + 1] if k in a else d  # noqa: E731
    r = transcribe_any(a[0], g("--model", "small"), g("--lang"))
    print(f"[{r['language']} {r['language_prob']} | {r['elapsed']}s] {r['text']}")
    if g("--srt"):
        whisper_utils.transcribe_to_srt(to_wav(a[0]), output_path=g("--srt"),
                                        model_size=g("--model", "small"), language=g("--lang"))
