# notify_dingtalk.py - 钉钉推送工具：私信 + 群消息
"""
用法:
    from notify_dingtalk import notify_dingtalk, notify_dingtalk_group

    notify_dingtalk("服务器异常告警")              # 私信推送给默认用户
    notify_dingtalk("hello", user_id="xxx")       # 私信推送给指定用户
    notify_dingtalk_group("部署完成", conversation_id="cidXXX")  # 群消息
"""
import sys, json, time
import urllib.request

sys.path.insert(0, '/Users/cuip/projects/GenericAgent')

# 默认接收人 (崔朋)
DEFAULT_USER = "28073119765047"

_token_cache = {"token": None, "expire": 0}


def _get_credentials():
    from llmcore import mykeys
    client_id = str(mykeys.get("dingtalk_client_id", "")).strip()
    client_secret = str(mykeys.get("dingtalk_client_secret", "")).strip()
    if not client_id or not client_secret:
        raise RuntimeError("dingtalk_client_id/secret not configured in mykeys")
    return client_id, client_secret


def _get_access_token():
    """获取钉钉 access_token (带缓存)"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire"]:
        return _token_cache["token"]

    client_id, client_secret = _get_credentials()
    url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    payload = {"appKey": client_id, "appSecret": client_secret}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    token = data.get("accessToken", "")
    expire_in = data.get("expireIn", 7200)
    _token_cache["token"] = token
    _token_cache["expire"] = now + expire_in - 60  # 提前60秒过期
    return token


def notify_dingtalk(text: str, user_id: str = None, title: str = "Agent通知"):
    """
    通过机器人发送私信 (Markdown格式)
    :param text: 消息内容 (支持Markdown)
    :param user_id: 接收人staffId，默认崔朋
    :param title: 消息标题
    :return: API响应dict
    """
    try:
        token = _get_access_token()
        client_id, _ = _get_credentials()
        user_id = user_id or DEFAULT_USER

        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": token
        }
        payload = {
            "robotCode": client_id,
            "userIds": [user_id],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False)
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        print(f"[notify_dingtalk] ✓ 已推送给 {user_id}")
        return result
    except Exception as e:
        print(f"[notify_dingtalk] ✗ 推送失败: {e}")
        raise


def notify_dingtalk_group(text: str, conversation_id: str = None, title: str = "Agent通知"):
    """
    通过机器人发送群消息 (Markdown格式)
    :param text: 消息内容 (支持Markdown)
    :param conversation_id: 群会话ID (openConversationId)
    :param title: 消息标题
    :return: API响应dict
    """
    if not conversation_id:
        # 尝试从配置文件加载默认群
        cfg_path = "/Users/cuip/projects/GenericAgent/config/dingtalk_group.json"
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            conversation_id = cfg.get("conversation_id", "")
        except FileNotFoundError:
            raise RuntimeError(
                f"未指定 conversation_id 且配置文件不存在: {cfg_path}\n"
                "请创建配置: {\"conversation_id\": \"cidXXXXXX\"}"
            )

    try:
        token = _get_access_token()
        client_id, _ = _get_credentials()

        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": token
        }
        payload = {
            "robotCode": client_id,
            "openConversationId": conversation_id,
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False)
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        print(f"[notify_dingtalk_group] ✓ 已推送到群 {conversation_id[:20]}...")
        return result
    except Exception as e:
        print(f"[notify_dingtalk_group] ✗ 群推送失败: {e}")
        raise


if __name__ == "__main__":
    # 测试: python notify_dingtalk.py "测试消息"
    import sys as _sys
    msg = _sys.argv[1] if len(_sys.argv) > 1 else "🔔 notify_dingtalk 测试推送"
    notify_dingtalk(msg)
