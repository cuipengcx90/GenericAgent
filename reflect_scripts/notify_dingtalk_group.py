"""钉钉群 Webhook 推送工具"""
import json, urllib.request

_CFG_PATH = "/Users/cuip/projects/GenericAgent/config/dingtalk_group.json"

def _load_cfg():
    return json.load(open(_CFG_PATH))

def notify_group(text: str) -> dict:
    """推送纯文本到钉钉群"""
    cfg = _load_cfg()
    url = cfg["dingtalk_group_webhook_url"]
    payload = {
        "msgtype": "text",
        "text": {"content": text}
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())
