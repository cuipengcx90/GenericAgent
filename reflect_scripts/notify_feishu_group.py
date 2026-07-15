"""飞书群 Webhook 推送工具（带签名）"""
import hmac, hashlib, base64, time, json, urllib.request

_CFG_PATH = "/Users/cuip/projects/GenericAgent/config/feishu_group.json"

def _load_cfg():
    return json.load(open(_CFG_PATH))

def notify_group(text: str) -> dict:
    """推送纯文本到飞书群（URL自动识别为链接）"""
    cfg = _load_cfg()
    url    = cfg["feishu_group_webhook_url"]
    secret = cfg["feishu_group_webhook_secret"]
    ts = str(int(time.time()))
    sign_str = f"{ts}\n{secret}"
    sign = base64.b64encode(
        hmac.new(sign_str.encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    payload = {
        "timestamp": ts,
        "sign": sign,
        "msg_type": "text",
        "content": {"text": text}
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    # NOTE: 不在此处调用 notify_feishu(text) 补发私信。
    # 所有调用方(digest/tech_trends/hellogithub/ga_mcp)均已单独调 notify_feishu 发私信,
    # 若此处再发一次会导致私信内每条报告出现两条一模一样(2026-07-15 根因)。本函数只负责群 webhook。
    return result
