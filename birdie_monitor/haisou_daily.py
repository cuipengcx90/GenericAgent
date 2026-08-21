"""海搜会员每日网盘资源监控：搜索→按 hsid 去重→仅新增通知。"""
import html
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, "/Users/cuip/projects/GenericAgent/memory")
from keychain import keys

BASE = "https://haisou.cc"
KEYCHAIN_NAME = "haisou_hs_web_session"
KEYWORDS = [
    "AI Agent", "AI coding", "AI应用开发", "出海SaaS", "Vibe Coding",
    "Claude Code", "AI创业", "独立开发出海", "SaaS出海", "MCP教程",
    "AI产品出海", "懂王", "鸡翅大模型", "赋范",
]
PLATFORMS = ("quark", "baidu", "ali")
LINK_TEMPLATES = {
    "quark": "https://pan.quark.cn/s/{code}",
    "baidu": "https://pan.baidu.com/s/1{code}",
    "ali": "https://www.alipan.com/s/{code}",
}
STATE_PATH = Path(__file__).with_name("haisou_seen_items.json")


def clean(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def load_state():
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def search(session, keyword, platform):
    body = {
        "query": keyword,
        "filters": {"scope": "title", "platforms": [platform], "include_filtered": False},
        "pagination": {"page": 1, "page_size": 20},
    }
    response = session.post(f"{BASE}/api/v2/shares/search", json=body, timeout=30)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or not payload.get("success"):
        error = (payload.get("error") or {}).get("message", response.text[:200])
        raise RuntimeError(f"HTTP {response.status_code}: {error}")
    return (payload.get("data") or {}).get("items") or []


def make_item(keyword, item):
    code = item.get("share_code") or ""
    platform = item.get("platform") or ""
    template = LINK_TEMPLATES.get(platform)
    return {
        "keyword": keyword, "platform": platform, "title": clean(item.get("share_name")),
        "share_code": code, "share_pwd": item.get("share_pwd") or "",
        "link": template.format(code=code) if template and code else None,
        "seen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def run():
    state = load_state()
    first_run = state is None
    state = state or {}
    try:
        cookie = keys.haisou_hs_web_session.use()
    except KeyError:
        return [], first_run, "海搜会话凭证不存在（keychain: haisou_hs_web_session）"
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    session.cookies.set("hs_web_session", cookie, domain="haisou.cc", path="/")
    additions = []
    try:
        for keyword in KEYWORDS:
            for platform in PLATFORMS:
                for item in search(session, keyword, platform):
                    hsid = item.get("hsid")
                    if not hsid or hsid in state:
                        continue
                    record = make_item(keyword, item)
                    state[hsid] = record
                    if not first_run:
                        additions.append(record)
    except Exception as exc:
        # 不保存部分结果，以便下次完整补扫；错误必须触发告警。
        return additions, first_run, str(exc)
    save_state(state)
    return additions, first_run, None


def build_report(additions, first_run, error):
    date = time.strftime("%Y-%m-%d")
    if error:
        return f"⚠️ 海搜监控异常（{date}）\n{error}\n请检查会员会话或海搜服务状态。"
    if first_run:
        return f"✅ 海搜监控已建立基线（{date}），后续仅推送新增资源。"
    lines = [f"🔎 海搜每日监控（{date}）", f"新增 {len(additions)} 条："]
    for item in additions:
        password = item["share_pwd"] or "无"
        lines.append(f"\n【{item['keyword']}｜{item['platform']}】{item['title']}\n  链接: {item['link'] or '未知'}  提取码: {password}")
    return "\n".join(lines)


def notify(text):
    try:
        sys.path.insert(0, "/Users/cuip/projects/GenericAgent/reflect_scripts")
        from reflect_common import notify_feishu
        notify_feishu(text)
    except Exception as exc:
        print(f"[飞书私信推送失败] {exc}")
    try:
        from notify_dingtalk_group import notify_group
        notify_group(text)
    except Exception as exc:
        print(f"[钉钉群推送失败] {exc}")


if __name__ == "__main__":
    additions, first_run, error = run()
    report = build_report(additions, first_run, error)
    print(report)
    # 首次运行只建基线；之后仅异常或新增时通知。
    if error or additions:
        notify(report)
