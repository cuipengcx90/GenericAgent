"""
ffmpeg_utils.py — FFmpeg 多媒体处理工具封装
依赖: ffmpeg/ffprobe (brew install ffmpeg)
功能:
  - get_media_info(path)         : 提取视频/音频元数据 → dict
  - extract_frame(video, t, out) : 截取指定时刻帧 → 图片文件
  - extract_frames(video, fps, outdir) : 按帧率批量截帧 → 文件列表
  - transcode(src, dst, **opts)  : 转码(格式/分辨率/码率等)
  - extract_audio(video, out)    : 提取音轨 → 音频文件
  - get_duration(path)           : 快速获取时长(秒, float)

坑:
  - ffmpeg -y 覆盖输出文件，不会交互询问
  - ffprobe json输出需 -v quiet 否则stderr有噪音
  - extract_frame t=0 可能黑帧，建议 t>=0.1
  - transcode vcodec='copy' 时不能改分辨率
  - 路径含空格需传list不传str给subprocess
"""
import os
import json
import subprocess
import shutil
from pathlib import Path
from typing import Optional, Union, List, Dict, Any


# ── 内部工具 ──────────────────────────────────────────────────

def _require_ffmpeg():
    if not shutil.which('ffmpeg'):
        raise RuntimeError("ffmpeg未找到，请 brew install ffmpeg")

def _require_ffprobe():
    if not shutil.which('ffprobe'):
        raise RuntimeError("ffprobe未找到，请 brew install ffmpeg")

def _run(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """运行ffmpeg命令，非零退出码抛异常"""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg错误(code={r.returncode}):\n{r.stderr[-800:]}")
    return r


# ── 元数据 ────────────────────────────────────────────────────

def get_media_info(path: str) -> Dict[str, Any]:
    """
    提取媒体文件完整元数据。
    返回 dict 含 format(时长/大小/比特率) 和 streams(视频/音频流信息)。

    示例:
        info = get_media_info('video.mp4')
        print(info['format']['duration'])      # '5.000000'
        print(info['streams'][0]['codec_name']) # 'h264'
    """
    _require_ffprobe()
    r = subprocess.run([
        'ffprobe', '-v', 'quiet',
        '-print_format', 'json',
        '-show_format', '-show_streams',
        str(path)
    ], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe失败: {r.stderr[:400]}")
    return json.loads(r.stdout)


def get_duration(path: str) -> float:
    """
    快速获取媒体时长(秒)。

    示例:
        d = get_duration('video.mp4')  # → 5.0
    """
    info = get_media_info(path)
    return float(info['format'].get('duration', 0))


def get_video_size(path: str):
    """返回 (width, height) 或 (None, None)"""
    info = get_media_info(path)
    for s in info.get('streams', []):
        if s.get('codec_type') == 'video':
            return s.get('width'), s.get('height')
    return None, None


# ── 截帧 ──────────────────────────────────────────────────────

def extract_frame(
    video_path: str,
    timestamp: float = 1.0,
    output_path: Optional[str] = None,
    quality: int = 2
) -> str:
    """
    截取视频指定时刻的帧为图片。

    Args:
        video_path: 输入视频路径
        timestamp:  时间点(秒)，默认1.0s(避免开头黑帧)
        output_path: 输出图片路径，默认同目录 frame_{t}s.jpg
        quality:    JPEG质量 1(最好)~31(最差)，默认2

    返回: 输出图片路径

    示例:
        img = extract_frame('video.mp4', t=2.5, output_path='/tmp/frame.jpg')
    """
    _require_ffmpeg()
    video_path = str(video_path)
    if output_path is None:
        base = os.path.splitext(video_path)[0]
        output_path = f"{base}_frame_{timestamp}s.jpg"
    _run([
        'ffmpeg', '-y',
        '-ss', str(timestamp),
        '-i', video_path,
        '-vframes', '1',
        '-q:v', str(quality),
        str(output_path)
    ])
    if not os.path.exists(output_path):
        raise RuntimeError(f"截帧失败，输出文件不存在: {output_path}")
    return output_path


def extract_frames(
    video_path: str,
    fps: float = 1.0,
    output_dir: Optional[str] = None,
    prefix: str = 'frame',
    fmt: str = 'jpg'
) -> List[str]:
    """
    按指定帧率批量提取帧。

    Args:
        video_path: 输入视频
        fps:        提取帧率(每秒帧数)，默认1fps
        output_dir: 输出目录，默认 {video}_frames/
        prefix:     文件名前缀
        fmt:        图片格式 jpg/png

    返回: 所有输出图片路径列表

    示例:
        frames = extract_frames('video.mp4', fps=0.5, output_dir='/tmp/frames')
    """
    _require_ffmpeg()
    video_path = str(video_path)
    if output_dir is None:
        base = os.path.splitext(video_path)[0]
        output_dir = f"{base}_frames"
    os.makedirs(output_dir, exist_ok=True)
    pattern = os.path.join(output_dir, f"{prefix}_%04d.{fmt}")
    _run([
        'ffmpeg', '-y',
        '-i', video_path,
        '-vf', f'fps={fps}',
        '-q:v', '2',
        pattern
    ])
    files = sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.startswith(prefix) and f.endswith(f'.{fmt}')
    ])
    return files


# ── 转码 ──────────────────────────────────────────────────────

def transcode(
    src: str,
    dst: str,
    vcodec: str = 'libx264',
    acodec: str = 'aac',
    video_bitrate: Optional[str] = None,
    audio_bitrate: Optional[str] = None,
    resolution: Optional[str] = None,
    fps: Optional[float] = None,
    extra_args: Optional[list] = None,
    timeout: int = 300
) -> str:
    """
    视频转码，支持格式转换/分辨率/码率/帧率调整。

    Args:
        src:           输入文件
        dst:           输出文件(扩展名决定容器格式)
        vcodec:        视频编码器，'copy'=不重编，默认'libx264'
        acodec:        音频编码器，'copy'=不重编，默认'aac'
        video_bitrate: 视频码率，如 '1M', '500k'
        audio_bitrate: 音频码率，如 '128k'
        resolution:    分辨率，如 '1280x720'，'copy'时不可用
        fps:           帧率，如 30.0
        extra_args:    额外ffmpeg参数列表
        timeout:       超时秒数

    返回: 输出文件路径

    示例:
        # MP4转低码率
        transcode('input.mp4', 'output.mp4', video_bitrate='500k', resolution='640x360')
        # 提取为GIF
        transcode('input.mp4', 'output.gif', vcodec='gif', acodec=None)
        # 无损复制换容器
        transcode('input.mp4', 'output.mkv', vcodec='copy', acodec='copy')
    """
    _require_ffmpeg()
    cmd = ['ffmpeg', '-y', '-i', str(src)]
    if vcodec:
        cmd += ['-c:v', vcodec]
    if acodec:
        cmd += ['-c:a', acodec]
    elif acodec is None:
        cmd += ['-an']  # 无音频
    if video_bitrate:
        cmd += ['-b:v', video_bitrate]
    if audio_bitrate:
        cmd += ['-b:a', audio_bitrate]
    if resolution and vcodec != 'copy':
        cmd += ['-s', resolution]
    if fps:
        cmd += ['-r', str(fps)]
    if extra_args:
        cmd += extra_args
    cmd.append(str(dst))
    _run(cmd, timeout=timeout)
    if not os.path.exists(dst):
        raise RuntimeError(f"转码失败，输出文件不存在: {dst}")
    return dst


# ── 音频提取 ──────────────────────────────────────────────────

def extract_audio(
    video_path: str,
    output_path: Optional[str] = None,
    codec: str = 'aac',
    bitrate: str = '128k'
) -> str:
    """
    从视频中提取音轨。

    Args:
        video_path:  输入视频
        output_path: 输出音频路径，默认同目录 .m4a
        codec:       音频编解码器，aac/mp3/wav/copy
        bitrate:     码率，copy时忽略

    返回: 输出音频路径

    示例:
        audio = extract_audio('video.mp4', '/tmp/audio.m4a')
    """
    _require_ffmpeg()
    video_path = str(video_path)
    ext_map = {'aac': 'm4a', 'mp3': 'mp3', 'wav': 'wav', 'flac': 'flac', 'copy': 'aac'}
    if output_path is None:
        base = os.path.splitext(video_path)[0]
        ext = ext_map.get(codec, 'aac')
        output_path = f"{base}_audio.{ext}"
    cmd = ['ffmpeg', '-y', '-i', video_path, '-vn', '-c:a', codec]
    if codec != 'copy':
        cmd += ['-b:a', bitrate]
    cmd.append(str(output_path))
    _run(cmd)
    return output_path


# ── 快捷工具 ──────────────────────────────────────────────────

def make_thumbnail(
    video_path: str,
    output_path: Optional[str] = None,
    time_pct: float = 0.1,
    width: int = 320
) -> str:
    """
    自动生成视频缩略图(取视频10%处)。

    示例:
        thumb = make_thumbnail('video.mp4', '/tmp/thumb.jpg', width=480)
    """
    duration = get_duration(video_path)
    t = max(0.1, duration * time_pct)
    if output_path is None:
        base = os.path.splitext(str(video_path))[0]
        output_path = f"{base}_thumb.jpg"
    _require_ffmpeg()
    _run([
        'ffmpeg', '-y',
        '-ss', str(t),
        '-i', str(video_path),
        '-vframes', '1',
        '-vf', f'scale={width}:-1',
        '-q:v', '2',
        str(output_path)
    ])
    return output_path