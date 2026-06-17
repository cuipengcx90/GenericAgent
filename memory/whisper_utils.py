"""
whisper_utils.py — 音频转录工具（基于 faster-whisper）
能力节点：音频→文字+时间戳，支持本地文件/语言检测/多格式
"""
from __future__ import annotations
import pathlib, time
from typing import Optional

_model_cache: dict = {}


def _get_model(model_size: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
    """加载/缓存 WhisperModel，避免重复初始化。"""
    key = (model_size, device, compute_type)
    if key not in _model_cache:
        from faster_whisper import WhisperModel
        _model_cache[key] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _model_cache[key]


def transcribe(
    audio_path: str | pathlib.Path,
    model_size: str = "tiny",
    language: Optional[str] = None,
    beam_size: int = 5,
    device: str = "cpu",
    compute_type: str = "int8",
) -> dict:
    """
    转录音频文件，返回含文本、时间戳、语言的 dict。

    Returns:
        {
            "text": str,           # 完整转录文本
            "language": str,       # 检测到的语言代码
            "language_prob": float,
            "segments": [{"start", "end", "text"}, ...],
            "elapsed": float,      # 转录耗时(秒)
        }
    """
    audio_path = pathlib.Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"音频文件不存在: {audio_path}")

    model = _get_model(model_size, device, compute_type)
    t0 = time.time()
    segments_iter, info = model.transcribe(
        str(audio_path), beam_size=beam_size, language=language
    )
    segments = [
        {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
        for s in segments_iter
    ]
    elapsed = round(time.time() - t0, 3)
    full_text = " ".join(s["text"] for s in segments)
    return {
        "text": full_text,
        "language": info.language,
        "language_prob": round(info.language_probability, 4),
        "segments": segments,
        "elapsed": elapsed,
    }


def transcribe_to_srt(
    audio_path: str | pathlib.Path,
    output_path: Optional[str | pathlib.Path] = None,
    model_size: str = "tiny",
    language: Optional[str] = None,
) -> str:
    """
    转录音频并生成 SRT 字幕内容（可选写文件）。

    Returns:
        SRT 格式字符串
    """
    result = transcribe(audio_path, model_size=model_size, language=language)

    def fmt_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(result["segments"], 1):
        lines.append(str(i))
        lines.append(f"{fmt_time(seg['start'])} --> {fmt_time(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    srt = "\n".join(lines)

    if output_path:
        pathlib.Path(output_path).write_text(srt, encoding="utf-8")

    return srt


def detect_language(
    audio_path: str | pathlib.Path,
    model_size: str = "tiny",
) -> tuple[str, float]:
    """
    仅做语言检测，返回 (language_code, probability)。
    """
    model = _get_model(model_size)
    audio_path = pathlib.Path(audio_path)
    _, info = model.transcribe(str(audio_path), beam_size=1, language=None)
    return info.language, round(info.language_probability, 4)


def transcribe_segment(
    audio_path: str | pathlib.Path,
    start_sec: float,
    end_sec: float,
    model_size: str = "tiny",
    language: Optional[str] = None,
) -> dict:
    """
    仅转录指定时间段（start_sec ~ end_sec）。
    内部用 clip_start/clip_duration 参数，需 faster-whisper >= 0.10。
    """
    audio_path = pathlib.Path(audio_path)
    model = _get_model(model_size)
    duration = end_sec - start_sec
    segs_iter, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        language=language,
        clip_timestamps=f"{start_sec},{end_sec}",
    )
    segments = [
        {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
        for s in segs_iter
    ]
    return {
        "text": " ".join(s["text"] for s in segments),
        "language": info.language,
        "segments": segments,
        "range": [start_sec, end_sec],
    }


def batch_transcribe(
    audio_paths: list[str | pathlib.Path],
    model_size: str = "tiny",
    language: Optional[str] = None,
) -> list[dict]:
    """
    批量转录多个音频文件，复用同一模型实例。

    Returns:
        list of transcribe() results, each with added "file" key
    """
    results = []
    for p in audio_paths:
        r = transcribe(p, model_size=model_size, language=language)
        r["file"] = str(p)
        results.append(r)
    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python whisper_utils.py <audio_file> [model_size]")
        sys.exit(1)
    audio = sys.argv[1]
    size = sys.argv[2] if len(sys.argv) > 2 else "tiny"
    result = transcribe(audio, model_size=size)
    print(f"语言: {result['language']} ({result['language_prob']})")
    print(f"耗时: {result['elapsed']}s")
    print(f"文本: {result['text']}")
    for s in result["segments"]:
        print(f"  [{s['start']}s→{s['end']}s] {s['text']}")