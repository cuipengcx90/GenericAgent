# Vision API SOP

## ⚠️ 前置规则（必须遵守）

1. **先枚举窗口**：调用 vision 前必须先用 `pygetwindow` 枚举窗口标题，确认目标窗口存在且已激活到前台。窗口不存在就不要截图。
2. **🚫 禁止全屏截图**：必须先利用ljqCtrl截取窗口区域。能截局部（如标题栏）就不截整窗口，能截窗口就绝不全屏。全屏截图在任何场景下都不允许。
3. **能不用 vision 就不用**：如果窗口标题/本地 OCR（`ocr_utils.py`）能获取所需信息，就不要调用 vision API，省 token 且更可靠。Vision 是最后手段。

## 快速用法（✅ vision_utils.py 已验证可用）

```python
# memory 已在 PATH，直接 import
from vision_utils import ask_vision

# 图片文件 / PIL Image / bytes 均可
reply = ask_vision('screenshot.png', '图中有什么文字？')
reply = ask_vision(pil_img, '描述这张图', backend='openai')

# 完整签名
ask_vision(image_input, prompt="详细描述这张图片的内容",
           backend='claude',   # 'claude'(default) | 'openai'
           timeout=60,
           max_pixels=1_440_000)
# backend 配置实测(并入上游 96be945 的记录): 'openai' 默认走 native_oai_config0(xAI，使用配置内代理)；
# 旧配置 native_oai_config15 曾返回 HTTP 503；备用 native_oai_config_fr113 曾实测识图成功
# 上游版本另有 'modelscope' | 'auto' 两个 backend 名(本机 vision_utils 未验证)
# 返回 str：成功为模型回复，失败为 'Error: ...'
```

**配置来源**：`../mykey.py` 中 `native_claude_config0` / `native_oai_config`（含 apikey/apibase/model）  
**proxy**：../mykey.py 顶层 `proxy` 字段（同 llmcore）  
**CLI 测试**：`python ../memory/vision_utils.py <image_path> [prompt] [backend]`

## 如果 vision_utils.py 缺失，重建步骤

1. 参考 `memory/vision_api.template.py` 作为结构参考
2. 用 `_load_mykeys()` 读 mykey.py（同 llmcore），配置 key：`native_claude_config0` / `native_oai_config`
3. 实现 `_prepare_image`（PIL→缩放→JPEG→base64）+ `_call_claude` + `_call_openai`
4. 保底：无可用 config 时申请 `https://modelscope.cn/my/myaccesstoken`

## 本地 OCR 快速用法（rapidocr，已验证可用）

```python
import sys; sys.path.insert(0, '../memory')  # 或已在PATH则无需
from ocr_utils import ocr_image, ocr_screen

# 对图片文件/PIL Image 做 OCR
result = ocr_image('path/to/image.png')
# result = {'text': 全文str, 'lines': [行列表], 'details': [{'bbox':..,'text':..,'conf':float}]}

# 截取屏幕区域 OCR（macOS需屏幕录制权限）
result = ocr_screen(bbox=(x1, y1, x2, y2))
```

### ⚠️ macOS 适配坑
- `ocr_window(hwnd)` 使用 win32gui，**macOS 不可用**，只用 `ocr_image` / `ocr_screen`
- `ocr_screen` 在 macOS 需授予终端「屏幕录制」权限，否则截图全黑
- 推荐：截图保存为文件再 `ocr_image(path)` 最稳
- `enhance=True` 放大+高对比度，对清晰图有害，默认关闭勿开
- 无文字时 result 返回 None（已在 _ocr_rapid 内处理为空dict）
- conf 字段原始值为 str，ocr_utils 已转为 float
