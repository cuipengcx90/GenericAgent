"""BirdieSearch.com API client.
签名算法(Char-Code头): axios拦截器自定义, 逆向自index-*.js
  s = abs(int((now_ms << 2) * 0.7))
  data_str = "" if data is None else json.dumps(data, separators=(',',':'), ensure_ascii=False)
  full = data_str + "&" + str(s)
  a = sum(ord(c) for c in full)
  header值 = f"{s}&{a*s}"
Cookie本身是签名会话(存了user对象含sign日期)，签到后服务端返回新Set-Cookie需持久化更新。
"""
import ast
import json
import re
import time
import requests
from pathlib import Path

BASE = "https://www.birdiesearch.com"
COOKIE_FILE = Path(__file__).parent / "birdie_cookies_raw.json"


def char_code_header(data):
    s = abs(int((int(time.time() * 1000) << 2) * 0.7))
    if data is None:
        r = ""
    elif isinstance(data, (dict, list)):
        r = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    else:
        r = str(data)
    full = r + "&" + str(s)
    a = sum(ord(c) for c in full)
    return f"{s}&{a * s}"


def load_cookie_header():
    """从本地保存的cookie文件(Python repr格式,含单引号)加载为 'k=v; k2=v2' 形式"""
    raw = COOKIE_FILE.read_text()
    cookies = ast.literal_eval(raw)
    parts = [f"{c['name']}={c['value']}" for c in cookies]
    return "; ".join(parts)


def save_cookie_from_set_cookie(set_cookie_header_list, old_cookie_header):
    """如果响应带Set-Cookie，更新本地持久化文件(原格式为cookie对象列表repr)"""
    if not set_cookie_header_list:
        return
    raw = COOKIE_FILE.read_text()
    cookies = ast.literal_eval(raw)
    by_name = {c["name"]: c for c in cookies}
    for sc in set_cookie_header_list:
        # sc like "BirdieSearch.key=xxx; Path=/; ..."
        kv = sc.split(";", 1)[0]
        if "=" not in kv:
            continue
        name, value = kv.split("=", 1)
        name = name.strip()
        if name in by_name:
            by_name[name]["value"] = value
    COOKIE_FILE.write_text(repr(list(by_name.values())))


def _headers(cookie_header, data):
    return {
        "Content-Type": "application/json",
        "Cookie": cookie_header,
        "Origin": BASE,
        "Referer": BASE + "/search",
        "Char-Code": char_code_header(data),
    }


def sign_in(cookie_header):
    """签到，返回(code,msg,body)。已签到返回code=-1"""
    r = requests.post(f"{BASE}/api/user/sign", headers=_headers(cookie_header, None), timeout=15)
    j = r.json()
    save_cookie_from_set_cookie(r.headers.get("Set-Cookie") and [r.headers["Set-Cookie"]], cookie_header)
    return j


def get_user_info(cookie_header):
    r = requests.post(f"{BASE}/api/user/info", headers=_headers(cookie_header, None), timeout=15)
    return r.json()


def search(cookie_header, word, page=1):
    body = {
        "word": word,
        "pages": page,
        "aide": True,
        "filter": {"cloud": "all", "mode": "whole", "given": "resources", "filesType": "all", "sort": 1},
    }
    r = requests.post(f"{BASE}/api/search", headers=_headers(cookie_header, body), json=body, timeout=15)
    return r.json()


def resolve_link(cookie_header, item):
    """将search()返回的item(url token + cloudType)解析为真实网盘链接。
    原理: GET /redirect?url=<token>&type=<cloudType> 返回HTML,
    内含 <script>location.href = "https://pan.xxx.com/s/..."</script>
    返回真实URL字符串,失败返回None。
    """
    token = item.get("url")
    cloud_type = item.get("cloudType")
    if not token or not cloud_type:
        return None
    r = requests.get(
        f"{BASE}/redirect",
        params={"url": token, "type": cloud_type},
        headers={"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    m = re.search(r'location\.href = "([^"]+)"', r.text)
    return m.group(1) if m else None
