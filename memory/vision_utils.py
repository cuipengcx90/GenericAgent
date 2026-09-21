"""vision_utils.py — 多模态视觉问答封装（仿 llmcore 风格）
用法:
    import sys; sys.path.insert(0, '../memory')  # 若 memory 未在 PATH
    from vision_utils import ask_vision

    reply = ask_vision('screenshot.png', '图中有什么文字？')
    reply = ask_vision(pil_img, '描述这张图', backend='openai')

后端: openai（默认） / deepseek（官方 DeepSeek-V4.1-Flash）
配置: openai 使用 mykey.py 中 native_oai_config
      deepseek 使用 macOS Keychain：service=GenericAgent，account=deepseek_api_key
      proxy: mykey.py 顶层 'proxy' 字段（同 llmcore，可选）
"""

import base64, os, sys, json, requests, socket as _socket
from io import BytesIO
from pathlib import Path

# ══════════════════════════════════════════════════════════════
# 代理探测（fallback 核心）
# ══════════════════════════════════════════════════════════════
def _probe_proxy(proxy_url, timeout=1.5):
    """检测代理 TCP 端口是否可达，不可达返回 False"""
    if not proxy_url:
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        host = p.hostname or '127.0.0.1'
        port = p.port or 1080
        s = _socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


# ── 路径注入（与 llmcore 一致）────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.join(_DIR, '..'), _DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 配置常量 ──────────────────────────────────────────────────
DEFAULT_BACKEND = 'openai'
# The verified OpenAI-compatible Vision route; do not fall back to retired Claude config0.
_OAI_KEY = 'native_oai_config'


# ══════════════════════════════════════════════════════════════
# 公开接口
# ══════════════════════════════════════════════════════════════
def ask_vision(image_input, prompt="详细描述这张图片的内容",
               backend=DEFAULT_BACKEND, timeout=60,
               max_pixels=1_440_000):
    """调用多模态大模型分析图片，返回文本回答。

    Args:
        image_input : 文件路径(str/Path) | PIL.Image 对象 | bytes(raw image)
        prompt      : 提问文本
        backend     : 'openai'（默认）或 'deepseek'（官方 DeepSeek-V4.1-Flash）
        timeout     : 请求超时秒数
        max_pixels  : 图片最大像素，超出自动缩放，默认 1440000

    Returns:
        str — 模型回答；出错返回 "Error: ..."
    """
    backend = backend.lower().strip()
    if backend not in ('openai', 'deepseek'):
        return f"Error: 不支持的 vision backend '{backend}'；可选: openai / deepseek"
    try:
        b64, mime = _prepare_image(image_input, max_pixels)
    except Exception as e:
        return f"Error: 图片处理失败 — {type(e).__name__}: {e}"

    # DeepSeek only needs the system Keychain credential. mykey.py is optional
    # here and is consulted solely for its existing proxy preference.
    try:
        mk = _load_mykeys()
    except FileNotFoundError:
        mk = {}
    if backend == 'openai' and _OAI_KEY not in mk:
        return f"Error: OpenAI Vision 配置缺失 — 请在 mykey.py 配置 {_OAI_KEY}"
    try:
        cfg = mk[_OAI_KEY] if backend == 'openai' else _load_deepseek_config()
    except Exception as e:
        return f"Error: DeepSeek Keychain 配置不可用 — {type(e).__name__}: {e}"
    proxy = mk.get('proxy', 'http://127.0.0.1:2082')

    # ── 自动探测代理可用性，不可用则降级直连 ──────────────────
    if proxy and _probe_proxy(proxy):
        proxies    = {'http': proxy, 'https': proxy}
        proxy_mode = f"proxy({proxy})"
    else:
        proxies    = None
        proxy_mode = "direct(proxy-down)"
    print(f"[vision_utils] 连接模式: {proxy_mode}")

    # ── 首次尝试（代理或直连）→ fallback直连 ──────────────────
    attempts = [(proxies, proxy_mode)]
    if proxies is not None:                         # 代理模式才需要直连fallback
        attempts.append((None, 'direct-fallback'))

    last_err = None
    for _px, _label in attempts:
        try:
            return _call_openai(b64, mime, prompt, timeout, _px, cfg)
        except requests.exceptions.Timeout:
            last_err = f"timeout(>{timeout}s) via {_label}"
            continue
        except requests.exceptions.ProxyError as e:
            last_err = f"ProxyError via {_label}: {e}"
            continue
        except requests.exceptions.RequestException as e:
            last_err = f"RequestError via {_label}: {e}"
            break
        except (KeyError, ValueError) as e:
            return f"Error: 响应解析失败 — {e}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    # ── OCR 降级（image_input 为文件路径时）──────────────────
    if isinstance(image_input, (str, bytes, os.PathLike)):
        try:
            from ocr_utils import ocr_image
            ocr_text = ocr_image(str(image_input))
            print(f"[vision_utils] OCR fallback 成功({len(ocr_text)}字符)")
            return f"[OCR-fallback] {ocr_text[:2000]}"
        except Exception as ocr_e:
            print(f"[vision_utils] OCR fallback 失败: {ocr_e}")

    return f"Error: {last_err or 'unknown'}"


# ══════════════════════════════════════════════════════════════
# 内部实现
# ══════════════════════════════════════════════════════════════
def _load_mykeys():
    """与 llmcore._load_mykeys 完全一致：优先 mykey.py，退而 mykey.json"""
    try:
        import mykey
        return {k: v for k, v in vars(mykey).items() if not k.startswith('_')}
    except ImportError:
        pass
    p = os.path.join(_DIR, '..', 'mykey.json')
    if not os.path.exists(p):
        raise FileNotFoundError('[vision_utils] 未找到 mykey.py / mykey.json')
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _load_deepseek_config():
    """Load the official DeepSeek Vision credential from macOS Keychain only."""
    import keyring
    api_key = keyring.get_password('GenericAgent', 'deepseek_api_key')
    if not api_key:
        raise RuntimeError('macOS Keychain 中不存在 GenericAgent/deepseek_api_key')
    return {
        'apibase': 'https://api.deepseek.com',
        'apikey': api_key,
        'model': 'deepseek-flash',
    }


def _prepare_image(image_input, max_pixels=1_440_000):
    """加载 → 缩放 → 转 JPEG → base64，返回 (b64_str, mime_type)"""
    from PIL import Image
    if isinstance(image_input, bytes):
        img = Image.open(BytesIO(image_input))
    elif isinstance(image_input, Image.Image):
        img = image_input.copy()
    elif isinstance(image_input, (str, Path)):
        img = Image.open(image_input)
    else:
        raise TypeError(
            f"image_input 须为路径/PIL.Image/bytes，实际: {type(image_input).__name__}")

    w, h = img.size
    if w * h > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        print(f"  缩放: {w}x{h} -> {nw}x{nh}")

    # 统一转 RGB（消除透明通道）
    if img.mode not in ('RGB', 'L'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
        img = bg

    buf = BytesIO()
    img.save(buf, format='JPEG', quality=85, optimize=True)
    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode()
    print(f"  图片: {w}x{h}  ->  {len(raw)/1024:.1f} KB (base64)")
    return b64, 'image/jpeg'


def _call_claude(b64, mime, prompt, timeout, proxies, cfg):
    """Anthropic Claude Vision — native messages API"""
    base = cfg['apibase'].rstrip('/')
    url = f"{base}/v1/messages"
    resp = requests.post(
        url,
        json={
            'model': cfg['model'],
            'max_tokens': 1024,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {
                        'type': 'base64', 'media_type': mime, 'data': b64}},
                    {'type': 'text', 'text': prompt}
                ]
            }]
        },
        headers={
            'x-api-key': cfg['apikey'],
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        },
        proxies=proxies,
        timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()['content'][0]['text']


def _call_openai(b64, mime, prompt, timeout, proxies, cfg):
    """OpenAI-compat Vision — chat/completions"""
    base = cfg['apibase'].rstrip('/')
    if base.endswith('completions'):
        url = base
    elif '/v1' in base:
        url = f"{base}/chat/completions"
    else:
        url = f"{base}/v1/chat/completions"

    resp = requests.post(
        url,
        json={
            'model': cfg['model'],
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {
                        'url': f'data:{mime};base64,{b64}'}}
                ]
            }]
        },
        headers={
            'Authorization': f"Bearer {cfg['apikey']}",
            'Content-Type': 'application/json'
        },
        proxies=proxies,
        timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']


# ══════════════════════════════════════════════════════════════
# CLI 快速测试
# ══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python vision_utils.py <image_path> [prompt] [backend=claude]")
        sys.exit(1)
    img_path = sys.argv[1]
    q  = sys.argv[2] if len(sys.argv) > 2 else "详细描述这张图片的内容"
    bk = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_BACKEND
    print(f"[vision_utils] backend={bk} | 发送中...\n")
    print(ask_vision(img_path, q, backend=bk))
