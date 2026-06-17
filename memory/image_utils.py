"""
image_utils.py — 图像处理工具（基于 Pillow + cv2）
能力节点：resize/crop/grayscale/截图增强/OCR预处理/格式转换/合成水印
联动：vision_utils.py → ask_vision(img, prompt)
"""
from __future__ import annotations
import pathlib, io
from typing import Optional, Union, Tuple
from PIL import Image, ImageEnhance, ImageFilter, ImageDraw, ImageFont


# ── 类型别名 ──────────────────────────────────────────────────
ImgLike = Union[str, pathlib.Path, Image.Image]


def _load(img: ImgLike) -> Image.Image:
    """统一加载：接受路径字符串/Path/PIL.Image，返回 PIL.Image。"""
    if isinstance(img, Image.Image):
        return img.copy()
    return Image.open(pathlib.Path(img))


def _save_or_return(img: Image.Image, output_path: Optional[ImgLike]) -> Image.Image:
    """若指定输出路径则保存，始终返回 PIL.Image。"""
    if output_path:
        p = pathlib.Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # JPEG不支持RGBA
        save_img = img.convert("RGB") if (p.suffix.lower() in ('.jpg', '.jpeg') and img.mode == 'RGBA') else img
        save_img.save(str(p))
    return img


# ══════════════════════════════════════════════════════════════
# 1. resize — 缩放
# ══════════════════════════════════════════════════════════════
def resize(
    img: ImgLike,
    width: Optional[int] = None,
    height: Optional[int] = None,
    scale: Optional[float] = None,
    keep_aspect: bool = True,
    output_path: Optional[ImgLike] = None,
) -> Image.Image:
    """
    缩放图像。可指定 width/height/scale，keep_aspect=True 时等比缩放。

    Examples:
        resize('a.png', width=800)          # 等比缩到宽800
        resize('a.png', scale=0.5)          # 缩小50%
        resize('a.png', width=640, height=480, keep_aspect=False)
    """
    pil = _load(img)
    w0, h0 = pil.size
    if scale:
        width, height = int(w0 * scale), int(h0 * scale)
    elif width and not height and keep_aspect:
        height = int(h0 * width / w0)
    elif height and not width and keep_aspect:
        width = int(w0 * height / h0)
    if not width or not height:
        raise ValueError("必须指定 width/height 或 scale")
    result = pil.resize((width, height), Image.LANCZOS)
    return _save_or_return(result, output_path)


# ══════════════════════════════════════════════════════════════
# 2. crop — 裁剪
# ══════════════════════════════════════════════════════════════
def crop(
    img: ImgLike,
    left: int,
    top: int,
    right: int,
    bottom: int,
    output_path: Optional[ImgLike] = None,
) -> Image.Image:
    """
    裁剪图像 (left, top, right, bottom)，像素坐标。
    """
    pil = _load(img)
    result = pil.crop((left, top, right, bottom))
    return _save_or_return(result, output_path)


# ══════════════════════════════════════════════════════════════
# 3. to_grayscale — 灰度化
# ══════════════════════════════════════════════════════════════
def to_grayscale(
    img: ImgLike,
    output_path: Optional[ImgLike] = None,
) -> Image.Image:
    """转换为灰度图（mode='L'）。"""
    pil = _load(img)
    result = pil.convert("L")
    return _save_or_return(result, output_path)


# ══════════════════════════════════════════════════════════════
# 4. enhance — 截图增强（亮度/对比度/锐化）
# ══════════════════════════════════════════════════════════════
def enhance(
    img: ImgLike,
    brightness: float = 1.0,
    contrast: float = 1.0,
    sharpness: float = 1.0,
    color: float = 1.0,
    output_path: Optional[ImgLike] = None,
) -> Image.Image:
    """
    增强图像质量，适合截图后处理。

    Args:
        brightness: 亮度因子，1.0=原始，>1增亮
        contrast:   对比度因子
        sharpness:  锐化因子，>1锐化
        color:      饱和度因子，0=灰度，1=原始

    Examples:
        enhance('screenshot.png', contrast=1.5, sharpness=2.0)
    """
    pil = _load(img)
    if brightness != 1.0:
        pil = ImageEnhance.Brightness(pil).enhance(brightness)
    if contrast != 1.0:
        pil = ImageEnhance.Contrast(pil).enhance(contrast)
    if sharpness != 1.0:
        pil = ImageEnhance.Sharpness(pil).enhance(sharpness)
    if color != 1.0:
        pil = ImageEnhance.Color(pil).enhance(color)
    return _save_or_return(pil, output_path)


# ══════════════════════════════════════════════════════════════
# 5. ocr_preprocess — OCR预处理（灰度+二值化+降噪）
# ══════════════════════════════════════════════════════════════
def ocr_preprocess(
    img: ImgLike,
    threshold: int = 128,
    denoise: bool = True,
    scale_factor: float = 2.0,
    output_path: Optional[ImgLike] = None,
) -> Image.Image:
    """
    OCR前处理：放大→灰度→高对比→二值化→降噪，提升识别率。

    Args:
        threshold:    二值化阈值 0-255
        denoise:      是否应用中值滤波降噪
        scale_factor: 放大倍率（小图建议2.0）
    """
    pil = _load(img).convert("RGB")
    # 放大
    if scale_factor != 1.0:
        w, h = pil.size
        pil = pil.resize((int(w * scale_factor), int(h * scale_factor)), Image.LANCZOS)
    # 灰度
    gray = pil.convert("L")
    # 对比度增强
    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    # 锐化
    gray = gray.filter(ImageFilter.SHARPEN)
    # 二值化
    binary = gray.point(lambda x: 255 if x > threshold else 0, '1').convert("L")
    # 降噪
    if denoise:
        binary = binary.filter(ImageFilter.MedianFilter(size=3))
    return _save_or_return(binary, output_path)


# ══════════════════════════════════════════════════════════════
# 6. get_info — 图像元信息
# ══════════════════════════════════════════════════════════════
def get_info(img: ImgLike) -> dict:
    """
    获取图像基本信息。

    Returns:
        {"width", "height", "mode", "format", "size_bytes", "has_exif"}
    """
    pil = _load(img)
    path = pathlib.Path(img) if not isinstance(img, Image.Image) else None
    size_bytes = path.stat().st_size if path and path.exists() else None
    exif = {}
    try:
        exif = pil._getexif() or {}
    except Exception:
        pass
    return {
        "width": pil.width,
        "height": pil.height,
        "mode": pil.mode,
        "format": pil.format,
        "size_bytes": size_bytes,
        "has_exif": len(exif) > 0,
    }


# ══════════════════════════════════════════════════════════════
# 7. add_watermark — 文字水印
# ══════════════════════════════════════════════════════════════
def add_watermark(
    img: ImgLike,
    text: str,
    position: Tuple[int, int] = (10, 10),
    font_size: int = 24,
    opacity: int = 128,
    color: Tuple[int, int, int] = (255, 255, 255),
    output_path: Optional[ImgLike] = None,
) -> Image.Image:
    """
    在图像上叠加文字水印。

    Args:
        position:  (x, y) 像素坐标
        opacity:   透明度 0-255
        color:     RGB颜色
    """
    pil = _load(img).convert("RGBA")
    overlay = Image.new("RGBA", pil.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        font = ImageFont.load_default()
    draw.text(position, text, font=font, fill=(*color, opacity))
    result = Image.alpha_composite(pil, overlay).convert("RGB")
    return _save_or_return(result, output_path)


# ══════════════════════════════════════════════════════════════
# 8. convert_format — 格式转换
# ══════════════════════════════════════════════════════════════
def convert_format(
    img: ImgLike,
    output_path: ImgLike,
    quality: int = 90,
) -> pathlib.Path:
    """
    转换图像格式（PNG↔JPG↔WEBP等），由 output_path 后缀决定格式。

    Returns:
        输出文件路径
    """
    pil = _load(img)
    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in ('.jpg', '.jpeg'):
        pil = pil.convert("RGB")
        pil.save(str(out), quality=quality)
    elif out.suffix.lower() == '.webp':
        pil.save(str(out), format='WEBP', quality=quality)
    else:
        pil.save(str(out))
    return out


# ══════════════════════════════════════════════════════════════
# 9. to_bytes — 序列化为bytes（用于API传输）
# ══════════════════════════════════════════════════════════════
def to_bytes(img: ImgLike, fmt: str = "PNG") -> bytes:
    """
    将图像序列化为字节流，便于 base64 编码或 API 传输。

    Args:
        fmt: 'PNG' | 'JPEG' | 'WEBP'
    """
    pil = _load(img)
    buf = io.BytesIO()
    if fmt.upper() in ('JPEG', 'JPG') and pil.mode == 'RGBA':
        pil = pil.convert('RGB')
    pil.save(buf, format=fmt.upper())
    return buf.getvalue()


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("用法: python image_utils.py <image_file>")
        sys.exit(1)
    p = sys.argv[1]
    info = get_info(p)
    print(f"图像信息: {json.dumps(info, ensure_ascii=False, indent=2)}")
    gray = to_grayscale(p, 'temp/test_gray.png')
    print(f"灰度图: {gray.size} mode={gray.mode}")
    pre = ocr_preprocess(p, output_path='temp/test_ocr_pre.png')
    print(f"OCR预处理: {pre.size}")