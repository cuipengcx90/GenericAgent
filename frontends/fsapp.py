import fcntl, glob, json, os, queue as Q, re, sys, threading, time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
from frontends.chatapp_common import format_restore
from frontends.continue_cmd import handle_frontend_command as handle_continue_frontend, reset_conversation


def _load_feishu_keys():
    try:
        import mykey
        return {k: v for k, v in vars(mykey).items() if not k.startswith('_')}
    except ImportError as e:
        if getattr(e, 'name', None) != 'mykey':
            raise
    key_path = os.path.join(PROJECT_ROOT, 'mykey.json')
    with open(key_path, encoding='utf-8') as f:
        return json.load(f)


mykeys = _load_feishu_keys()

import traceback
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

_TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "summary", "tool_use", "file_content")]
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
_AUDIO_EXTS = {".opus", ".mp3", ".wav", ".m4a", ".aac"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
_MSG_TYPE_MAP = {"image": "[image]", "audio": "[audio]", "file": "[file]", "media": "[media]", "sticker": "[sticker]"}

TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
MEDIA_DIR = os.path.join(TEMP_DIR, "feishu_media")
os.makedirs(MEDIA_DIR, exist_ok=True)
LOCK_PATH = os.path.join(TEMP_DIR, "feishu_agent.lock")
_lock_file = None


def _acquire_process_lock():
    global _lock_file
    _lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[ERROR] another fsapp instance is already running; lock={LOCK_PATH}")
        sys.exit(2)
    _lock_file.write(f"{os.getpid()}\n")
    _lock_file.flush()


_TRUNC_TAIL = 300  # 截断兜底时保留原文尾部字符数


def _clean(text):
    for pat in _TAG_PATS:
        text = re.sub(pat, "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_files(text):
    return re.findall(r"\[FILE:([^\]]+)\]", text or "")


def _strip_files(text):
    return re.sub(r"\[FILE:[^\]]+\]", "", text or "").strip()


def _display_text(text):
    cleaned = _strip_files(_clean(text))
    if cleaned:
        return cleaned
    tail = (text or "").strip()[-_TRUNC_TAIL:]
    return "（无文本输出）" + (f"\n…{tail}" if tail else "")


def _to_allowed_set(value):
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(x).strip() for x in value if str(x).strip()}


def _parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _extract_share_card_content(content_json, msg_type):
    parts = []
    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")
    return "\n".join([p for p in parts if p]).strip() or f"[{msg_type}]"


def _extract_interactive_content(content):
    parts = []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return [content] if content.strip() else []
    if not isinstance(content, dict):
        return parts
    title = content.get("title")
    if isinstance(title, dict):
        title_text = title.get("content", "") or title.get("text", "")
        if title_text:
            parts.append(f"title: {title_text}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")
    elements = content.get("elements", [])
    if isinstance(elements, list):
        for row in elements:
            if isinstance(row, dict):
                parts.extend(_extract_element_content(row))
            elif isinstance(row, list):
                for el in row:
                    parts.extend(_extract_element_content(el))
    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))
    header = content.get("header", {})
    if isinstance(header, dict):
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    return [p for p in parts if p]


def _extract_element_content(element):
    parts = []
    if not isinstance(element, dict):
        return parts
    tag = element.get("tag", "")
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str) and text:
            parts.append(text)
        for field in element.get("fields", []) or []:
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    content = field_text.get("content", "") or field_text.get("text", "")
                    if content:
                        parts.append(content)
    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            content = text.get("content", "") or text.get("text", "")
            if content:
                parts.append(content)
        url = element.get("url", "") or (element.get("multi_url", {}) or {}).get("url", "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = element.get("alt", {})
        if isinstance(alt, dict):
            parts.append(alt.get("content", "[image]") or "[image]")
        else:
            parts.append("[image]")
    for child in element.get("elements", []) or []:
        parts.extend(_extract_element_content(child))
    for col in element.get("columns", []) or []:
        for child in (col.get("elements", []) if isinstance(col, dict) else []):
            parts.extend(_extract_element_content(child))
    return parts


def _extract_post_content(content_json):
    def _parse_block(block):
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if block.get("title"):
            texts.append(block.get("title"))
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "img" and el.get("image_key"):
                    images.append(el["image_key"])
        text = " ".join([t for t in texts if t]).strip()
        return text or None, images

    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs
    return "", []


APP_ID = str(mykeys.get("fs_app_id", "") or "").strip()
APP_SECRET = str(mykeys.get("fs_app_secret", "") or "").strip()
ALLOWED_USERS = _to_allowed_set(mykeys.get("fs_allowed_users", []))
PUBLIC_ACCESS = not ALLOWED_USERS or "*" in ALLOWED_USERS
AGENT_TIMEOUT_SEC = 3600  # was 900; other frontends (tg/streamlit/qt/discord) have no hard cutoff at all
FEISHU_CLIENT_TIMEOUT_SEC = 8
CARD_PATCH_WARN_SEC = 2.0

agent = None
_agent_lock = threading.Lock()


def get_agent():
    """Lazy-load the heavy GA runtime after the Feishu websocket is alive."""
    global agent
    if agent is not None:
        return agent
    with _agent_lock:
        if agent is None:
            from agentmain import GeneraticAgent
            from frontends.chatapp_common import install_agent_extensions
            install_agent_extensions(GeneraticAgent)
            agent = GeneraticAgent()
            threading.Thread(target=agent.run, daemon=True, name="ga-agent-runner").start()
        return agent


def _warm_agent_async():
    try:
        get_agent()
    except Exception as e:
        print(f"[ERROR] agent warmup failed: {e}")


client, user_tasks = None, {}


def create_client():
    return (
        lark.Client.builder()
        .app_id(APP_ID)
        .app_secret(APP_SECRET)
        .log_level(lark.LogLevel.INFO)
        .timeout(FEISHU_CLIENT_TIMEOUT_SEC)
        .build()
    )


def _card_raw(elements):
    return json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": elements},
    }, ensure_ascii=False)


def _card(text):
    return _card_raw([{"tag": "markdown", "content": text}])


def _send_raw(receive_id, payload, msg_type, rtype):
    t0 = time.time()
    try:
        body = CreateMessageRequest.builder().receive_id_type(rtype).request_body(
            CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type).content(payload).build()
        ).build()
        r = client.im.v1.message.create(body)
        dt = time.time() - t0
        if dt > CARD_PATCH_WARN_SEC:
            print(f"[WARN] send_message slow {dt:.2f}s type={msg_type} rtype={rtype}")
        if r.success():
            return r.data.message_id if r.data else None
        print(f"发送失败: {r.code}, {r.msg}")
    except Exception as e:
        print(f"[ERROR] _send_raw 网络异常 after {time.time() - t0:.2f}s: {e}")
    return None


def _patch_card(message_id, card_json):
    return _patch_card_result(message_id, card_json)[0]


def _patch_card_result(message_id, card_json):
    t0 = time.time()
    try:
        body = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder().content(card_json).build()
        ).build()
        r = client.im.v1.message.patch(body)
        dt = time.time() - t0
        if dt > CARD_PATCH_WARN_SEC:
            print(f"[WARN] patch_card slow {dt:.2f}s message_id={message_id}")
        if not r.success():
            print(f"[ERROR] patch_card 失败: {r.code}, {r.msg}")
        msg = f"{getattr(r, 'code', '')} {getattr(r, 'msg', '')}".lower()
        return r.success(), ("230099" in msg or "11310" in msg or "element exceeds the limit" in msg)
    except Exception as e:
        print(f"[ERROR] _patch_card 网络异常 after {time.time() - t0:.2f}s: {e}")
        return False, False


def send_message(receive_id, content, msg_type="text", use_card=False, receive_id_type="open_id"):
    if use_card:
        return _send_raw(receive_id, _card(content), "interactive", receive_id_type)
    if msg_type == "text":
        return _send_raw(receive_id, json.dumps({"text": content}, ensure_ascii=False), "text", receive_id_type)
    return _send_raw(receive_id, content, msg_type, receive_id_type)


def update_message(message_id, content):
    return _patch_card(message_id, _card(content))


def _upload_image_sync(file_path):
    try:
        with open(file_path, "rb") as f:
            request = CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()
            ).build()
            response = client.im.v1.image.create(request)
            if response.success():
                return response.data.image_key
            print(f"[ERROR] upload image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] upload image failed {file_path}: {e}")
    return None


def _upload_file_sync(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    file_type = _FILE_TYPE_MAP.get(ext, "stream")
    file_name = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            request = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type(file_type).file_name(file_name).file(f).build()
            ).build()
            response = client.im.v1.file.create(request)
            if response.success():
                return response.data.file_key
            print(f"[ERROR] upload file failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] upload file failed {file_path}: {e}")
    return None


def _download_image_sync(message_id, image_key):
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type("image").build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download image failed {image_key}: {e}")
    return None, None


def _download_file_sync(message_id, file_key, resource_type="file"):
    if resource_type == "audio":
        resource_type = "file"
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(resource_type).build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download {resource_type} failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download {resource_type} failed {file_key}: {e}")
    return None, None


def _download_and_save_media(msg_type, content_json, message_id):
    data, filename = None, None
    if msg_type == "image":
        image_key = content_json.get("image_key")
        if image_key and message_id:
            data, filename = _download_image_sync(message_id, image_key)
            if not filename:
                filename = f"{image_key[:16]}.jpg"
    elif msg_type in ("audio", "file", "media"):
        file_key = content_json.get("file_key")
        if file_key and message_id:
            data, filename = _download_file_sync(message_id, file_key, msg_type)
            if not filename:
                filename = file_key[:16]
            if msg_type == "audio" and filename and not filename.endswith(".opus"):
                filename = f"{filename}.opus"
    if data and filename:
        file_path = os.path.join(MEDIA_DIR, os.path.basename(filename))
        with open(file_path, "wb") as f:
            f.write(data)
        return file_path, filename
    return None, None


def _describe_media(msg_type, file_path, filename):
    if msg_type == "image":
        return f"[image: {filename}]\n[Image: source: {file_path}]"
    if msg_type == "audio":
        return f"[audio: {filename}]\n[File: source: {file_path}]"
    if msg_type in ("file", "media"):
        return f"[{msg_type}: {filename}]\n[File: source: {file_path}]"
    return f"[{msg_type}]\n[File: source: {file_path}]"


def _send_local_file(receive_id, file_path, receive_id_type="open_id"):
    if not os.path.isfile(file_path):
        send_message(receive_id, f"⚠️ 文件不存在: {file_path}", receive_id_type=receive_id_type)
        return False
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXTS:
        image_key = _upload_image_sync(file_path)
        if image_key:
            send_message(receive_id, json.dumps({"image_key": image_key}, ensure_ascii=False), msg_type="image", receive_id_type=receive_id_type)
            return True
    else:
        file_key = _upload_file_sync(file_path)
        if file_key:
            msg_type = "media" if ext in _AUDIO_EXTS or ext in _VIDEO_EXTS else "file"
            send_message(receive_id, json.dumps({"file_key": file_key}, ensure_ascii=False), msg_type=msg_type, receive_id_type=receive_id_type)
            return True
    send_message(receive_id, f"⚠️ 文件发送失败: {os.path.basename(file_path)}", receive_id_type=receive_id_type)
    return False


def _send_generated_files(receive_id, raw_text, receive_id_type="open_id"):
    for file_path in _extract_files(raw_text):
        _send_local_file(receive_id, file_path, receive_id_type)


def _build_user_message(message):
    msg_type = message.message_type
    message_id = message.message_id
    content_json = _parse_json(message.content)
    parts, image_paths = [], []
    if msg_type == "text":
        text = str(content_json.get("text", "") or "").strip()
        if text:
            parts.append(text)
    elif msg_type == "post":
        text, image_keys = _extract_post_content(content_json)
        if text:
            parts.append(text)
        for image_key in image_keys:
            file_path, filename = _download_and_save_media("image", {"image_key": image_key}, message_id)
            if file_path and filename:
                parts.append(_describe_media("image", file_path, filename))
                image_paths.append(file_path)
            else:
                parts.append("[image: download failed]")
    elif msg_type in ("image", "audio", "file", "media"):
        file_path, filename = _download_and_save_media(msg_type, content_json, message_id)
        if file_path and filename:
            parts.append(_describe_media(msg_type, file_path, filename))
            if msg_type == "image":
                image_paths.append(file_path)
        else:
            parts.append(f"[{msg_type}: download failed]")
    elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
        parts.append(_extract_share_card_content(content_json, msg_type))
    else:
        parts.append(_MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))
    return "\n".join([p for p in parts if p]).strip(), image_paths


def _fmt_tool_call(tc):
    name = tc.get('tool_name', '?')
    args = {k: v for k, v in (tc.get('args') or {}).items() if not k.startswith('_')}
    return f"- `{name}`({json.dumps(args, ensure_ascii=False)[:200]})"


def _build_step_detail(resp, tool_calls):
    """从 LLM response + tool_calls 组装单步展开详情（纯函数）。"""
    parts = []
    thinking = (getattr(resp, 'thinking', '') or '').strip() if resp else ''
    if thinking:
        parts.append(f"### 💭 Thinking\n{thinking}")
    if tool_calls:
        parts.append("### 🛠 Tool Calls\n" + "\n".join(_fmt_tool_call(tc) for tc in tool_calls))
    content = _display_text((getattr(resp, 'content', '') or '')).strip() if resp else ''
    if content and content != '...':
        parts.append(f"### 📝 Output\n{content}")
    return "\n\n".join(parts)


class _TaskCard:
    """飞书任务卡片：后台线程 create/patch，避免飞书 API 慢时阻塞 agent 主循环。"""
    _DETAIL_LIMIT = 4000
    _FINAL_LIMIT = 4000
    _QUEUE_LIMIT = 8
    _STEP_FLUSH_INTERVAL = 3.0

    def __init__(self, receive_id, rid_type):
        self.rid, self.rtype = receive_id, rid_type
        self.steps = []          # [(summary, detail), ...]
        self.status = "🤔 思考中..."
        self.final = None
        self.msg_id = None
        self.page_no = 1
        self.turn_no = 0
        self.turn_base = 1
        self.note = None
        self._lock = threading.RLock()
        self._q = Q.Queue(maxsize=self._QUEUE_LIMIT)
        self._last_step_flush = 0.0
        self._worker = threading.Thread(target=self._worker_loop, name="fs-card-patch", daemon=True)
        self._worker.start()

    def _step_panel(self, idx, summary, detail):
        detail = detail or "_(无输出)_"
        if len(detail) > self._DETAIL_LIMIT:
            detail = detail[:self._DETAIL_LIMIT] + f"\n\n…(已截断,共 {len(detail)} 字符)"
        return {
            "tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": f"Turn {idx} · {summary}"}},
            "elements": [{"tag": "markdown", "content": detail}],
        }

    def _build_locked(self):
        header = f"**{self.status}**"
        if self.page_no > 1:
            header += f"\n\n📄 工作卡片 {self.page_no}"
        els = [{"tag": "markdown", "content": header}]
        if self.note:
            els.append({"tag": "markdown", "content": self.note})
        for i, (s, d) in enumerate(self.steps, self.turn_base):
            els.append(self._step_panel(i, s, d))
        if self.final:
            els += [{"tag": "hr"}, {"tag": "markdown", "content": self.final}]
        return _card_raw(els)

    def _enqueue(self, reason, force=False):
        if not force and reason == "step" and time.time() - self._last_step_flush < self._STEP_FLUSH_INTERVAL:
            return
        if reason == "step":
            self._last_step_flush = time.time()
        try:
            self._q.put_nowait(reason)
        except Q.Full:
            try:
                self._q.get_nowait()
            except Q.Empty:
                pass
            try:
                self._q.put_nowait(reason)
            except Q.Full:
                print(f"[WARN] fs card queue still full, drop {reason}")

    def _push_sync(self):
        with self._lock:
            card = self._build_locked()
            msg_id = self.msg_id
        if msg_id:
            return _patch_card_result(msg_id, card)
        msg_id = _send_raw(self.rid, card, "interactive", self.rtype)
        with self._lock:
            if msg_id:
                self.msg_id = msg_id
        return bool(msg_id), False

    def _worker_loop(self):
        while True:
            reason = self._q.get()
            # Coalesce bursts so one slow Feishu patch does not build a backlog.
            while True:
                try:
                    reason = self._q.get_nowait()
                except Q.Empty:
                    break
            t0 = time.time()
            ok, limit = self._push_sync()
            dt = time.time() - t0
            if dt > CARD_PATCH_WARN_SEC:
                print(f"[WARN] fs card push slow {dt:.2f}s reason={reason} ok={ok} limit={limit}")
            if limit:
                with self._lock:
                    self._rollover_locked()
                self._push_sync()

    def _rollover_locked(self):
        self.page_no += 1
        self.msg_id = None
        self.final = None
        self.note = "⚠️ 上一张工作卡片达到飞书限制，本页继续展示后续进展。"

    # ── 公开接口 ──

    def start(self):
        self._enqueue("start", force=True)

    def step(self, summary, detail=""):
        with self._lock:
            self.turn_no += 1
            self.steps.append((summary, detail))
            self.status = f"⏳ 工作中 · Turn {self.turn_no}"
        self._enqueue("step")

    def done(self, text):
        with self._lock:
            self.status = "✅ 已完成"
            self.final = (text or "_(无文本输出)_")[:self._FINAL_LIMIT]
        self._enqueue("done", force=True)

    def fail(self, msg):
        with self._lock:
            self.status = f"❌ {msg}"
        self._enqueue("fail", force=True)


def _make_task_hook(card, done_event, on_final):
    """飞书任务 hook：每轮 patch 卡片状态；结束触发 on_final(raw) 处理附件。"""
    def hook(ctx):
        try:
            if ctx.get('exit_reason'):
                resp = ctx.get('response')
                raw = resp.content if hasattr(resp, 'content') else str(resp)
                card.done(_display_text(raw))
                on_final(raw)
                done_event.set()
            elif ctx.get('summary'):
                detail = _build_step_detail(ctx.get('response'), ctx.get('tool_calls') or [])
                card.step(ctx['summary'], detail)
        except Exception as e:
            print(f"[fs hook] error: {e}")
    return hook


def handle_message(data):
    event, message, sender = data.event, data.event.message, data.event.sender
    open_id = sender.sender_id.open_id
    chat_id = message.chat_id
    if not PUBLIC_ACCESS and open_id not in ALLOWED_USERS:
        print(f"未授权用户: {open_id}")
        return
    user_input, image_paths = _build_user_message(message)
    if not user_input:
        if chat_id:
            send_message(chat_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}", receive_id_type="chat_id")
        else:
            send_message(open_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}")
        return
    print(f"收到消息 [{open_id}] ({message.message_type}, {len(image_paths)} images): {user_input[:200]}")
    if message.message_type == "text" and user_input.startswith("/"):
        return handle_command(open_id, user_input, chat_id)

    def run_agent():
        user_tasks[open_id] = {"running": True}
        receive_id = chat_id or open_id
        rid_type = "chat_id" if chat_id else "open_id"
        done_event = threading.Event()
        hook_key = f"fs_{open_id}"
        card = _TaskCard(receive_id, rid_type)
        card.start()
        on_final = lambda raw: _send_generated_files(receive_id, raw, receive_id_type=rid_type)
        ag = get_agent()
        if not hasattr(ag, '_turn_end_hooks'): ag._turn_end_hooks = {}
        ag._turn_end_hooks[hook_key] = _make_task_hook(card, done_event, on_final)
        try:
            ag.put_task(user_input, source="feishu", images=image_paths)
            start = time.time()
            while not done_event.wait(timeout=3):
                if not user_tasks.get(open_id, {}).get("running", True):
                    ag.abort()
                    card.fail("已停止")
                    break
                if time.time() - start > AGENT_TIMEOUT_SEC:
                    ag.abort()
                    card.fail("任务超时")
                    break
        except Exception as e:
            traceback.print_exc()
            card.fail(f"错误: {e}")
        finally:
            if hasattr(ag, '_turn_end_hooks'):
                ag._turn_end_hooks.pop(hook_key, None)
            user_tasks.pop(open_id, None)

    threading.Thread(target=run_agent, daemon=True).start()


def handle_command(open_id, cmd, chat_id=None):
    def _send_cmd_response(content):
        receive_id = chat_id or open_id
        rtype = "chat_id" if chat_id else "open_id"

        def _worker():
            try:
                send_message(receive_id, content, receive_id_type=rtype)
            except Exception as e:
                print(f"[ERROR] async command response failed: {e}")

        threading.Thread(target=_worker, daemon=True).start()

    parts = (cmd or "").split()
    op = (parts[0] if parts else "").lower()
    ag = get_agent() if op != "/help" else None
    if op == "/stop":
        print(f"[fs cmd] /stop requested by {open_id}")
        if open_id in user_tasks:
            user_tasks[open_id]["running"] = False
            print(f"[fs cmd] marked task stopped for {open_id}")
        ag.abort()
        _send_cmd_response("正在停止...")
        print(f"[fs cmd] /stop dispatched for {open_id}")
    elif op == "/new":
        _send_cmd_response(reset_conversation(ag))
    elif op == "/help":
        _send_cmd_response("命令列表:\n/stop - 停止当前任务\n/status - 查看状态\n/llm - 查看当前模型列表\n/llm [n] - 切换到第 n 个模型\n/restore - 恢复上次对话历史\n/continue - 列出可恢复会话\n/continue [n] - 恢复第 n 个会话\n/new - 开启新对话并清空当前上下文\n/help - 显示帮助")
    elif op == "/status":
        llm = ag.get_llm_name() if ag.llmclient else "未配置"
        _send_cmd_response(f"状态: {'🔴 运行中' if ag.is_running else '🟢 空闲'}\nLLM: [{ag.llm_no}] {llm}")
    elif op == "/llm":
        if not ag.llmclient:
            return _send_cmd_response("❌ 当前没有可用的 LLM 配置")
        if len(parts) > 1:
            try:
                ag.next_llm(int(parts[1]))
                return _send_cmd_response(f"✅ 已切换到 [{ag.llm_no}] {ag.get_llm_name()}")
            except Exception:
                return _send_cmd_response(f"用法: /llm <0-{len(ag.list_llms()) - 1}>")
        lines = [f"{'→' if cur else '  '} [{i}] {name}" for i, name, cur in ag.list_llms()]
        _send_cmd_response("LLMs:\n" + "\n".join(lines))
    elif op == "/restore":
        try:
            restored_info, err = format_restore()
            if err:
                return _send_cmd_response(err.replace("❌ ", ""))
            restored, fname, count = restored_info
            ag.history.extend(restored)
            ag.abort()
            _send_cmd_response(f"已恢复 {count} 轮对话\n来源: {fname}\n(仅恢复上下文，请输入新问题继续)")
        except Exception as e:
            _send_cmd_response(f"恢复失败: {e}")
    elif op == "/continue" or cmd.startswith("/continue"):
        _send_cmd_response(handle_continue_frontend(ag, cmd))
    else:
        _send_cmd_response(f"未知命令: {cmd}")


def main():
    global client
    _acquire_process_lock()
    if not APP_ID or not APP_SECRET:
        print("错误: 请在 mykey.py 或 mykey.json 中配置 fs_app_id 和 fs_app_secret")
        sys.exit(1)
    client = create_client()
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(handle_message)
               .register_p2_im_message_message_read_v1(lambda _: None)
               .build())
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
    print("=" * 50 + "\n飞书 Agent 已启动（长连接模式）\n" + f"App ID: {APP_ID}\n等待消息...\n" + "=" * 50)
    retry_delay = 5
    while True:
        try:
            cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
            cli.start()
        except Exception as e:
            print(f"[WARN] 飞书长连接断开或启动失败: {e}")
        print(f"[INFO] {retry_delay}s 后重连...")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 120)
        # 重连时刷新 client
        try:
            client = create_client()
        except Exception:
            pass


if __name__ == "__main__":
    main()
