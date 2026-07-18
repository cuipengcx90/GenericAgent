# reflect_common.py - 公共工具：飞书推送 + 状态去重
import sys, json, urllib.request, time

sys.path.insert(0, '/Users/cuip/projects/GenericAgent')

FEISHU_USER = "ou_d0dd287e47bc0775b5da93aef693fee0"  # 你的 open_id

def _get_token():
    from llmcore import mykeys
    app_id = str(mykeys.get("fs_app_id", "")).strip()
    app_secret = str(mykeys.get("fs_app_secret", "")).strip()
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=data, headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["tenant_access_token"]

def notify_feishu(text: str, open_id: str = FEISHU_USER):
    """推送纯文本到飞书（URL自动识别为链接）"""
    try:
        token = _get_token()
        body = json.dumps({
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("code") != 0:
            print(f"[飞书推送失败] {resp}")
    except Exception as e:
        print(f"[飞书推送失败] {e}")

class Debounce:
    """防抖：同样的 key 在冷却期内不重复触发"""
    def __init__(self, cooldown=3600):
        self._last = {}
        self.cooldown = cooldown

    def should_fire(self, key: str) -> bool:
        now = time.time()
        if now - self._last.get(key, 0) > self.cooldown:
            self._last[key] = now
            return True
        return False

def upload_feishu_image(img_path: str) -> str:
    """上传图片到飞书，返回 image_key。需 requests（已验证可用）。"""
    import requests
    token = _get_token()
    with open(img_path, "rb") as f:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            files={"image": f}, data={"image_type": "message"}, timeout=30,
        ).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"upload_feishu_image failed: {resp}")
    return resp["data"]["image_key"]


def send_feishu_image(img_path: str, open_id: str = FEISHU_USER) -> dict:
    """上传并发送图片到飞书私信。返回发送响应。"""
    image_key = upload_feishu_image(img_path)
    token = _get_token()
    body = json.dumps({
        "receive_id": open_id,
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())
