"""BirdieSearch 每日关键词监控任务。
流程: 签到 -> 遍历关键词搜索 -> 解析真实网盘链接 -> 本地去重存储 -> 新增才通知。
遇到接口错误(code!=0/HTTP异常)视为当日限额已到，停止后续关键词循环。
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/Users/cuip/projects/GenericAgent/reflect_scripts")

import birdie_client as bc

KEYWORDS = [
    "AI Agent", "AI coding", "AI应用开发", "出海SaaS",
    "Vibe Coding", "Claude Code", "AI创业",
    "独立开发出海", "SaaS出海", "MCP教程", "AI产品出海",
    "懂王", "鸡翅大模型", "赋范",
]
STATE_PATH = Path(__file__).parent / "birdie_seen_items.json"
REPORT_DIR = Path(__file__).parent / "birdie_reports"


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_tags(s):
    return re.sub(r"</?b>", "", s or "")


def run():
    cookie = bc.load_cookie_header()
    sign_res = bc.sign_in(cookie)
    print("签到结果:", sign_res)

    state = load_state()
    new_items = []  # (keyword, item, real_url)
    quota_hit = False

    for kw in KEYWORDS:
        if quota_hit:
            break
        try:
            res = bc.search(cookie, kw)
        except Exception as e:
            print(f"[{kw}] 搜索请求异常，视为限额已到: {e}")
            quota_hit = True
            break

        if res.get("code") != 0:
            print(f"[{kw}] code={res.get('code')} msg={res.get('message')}，视为限额已到，停止后续关键词")
            quota_hit = True
            break

        items = res.get("body") or []
        print(f"[{kw}] 命中 {len(items)} 条")
        for item in items:
            iid = item.get("_id")
            if not iid or iid in state:
                continue
            try:
                real_url = bc.resolve_link(cookie, item)
            except Exception as e:
                real_url = None
                print(f"  resolve_link失败 _id={iid}: {e}")
            time.sleep(0.5)  # 避免过快触发风控
            state[iid] = {
                "keyword": kw,
                "title": strip_tags(item.get("title")),
                "cloudType": item.get("cloudType"),
                "password": item.get("password"),
                "real_url": real_url,
                "seen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            new_items.append((kw, item, real_url))

    save_state(state)
    return new_items, quota_hit


def build_report(new_items, quota_hit):
    lines = [f"🔎 BirdieSearch 每日监控（{time.strftime('%Y-%m-%d')}）"]
    if not new_items:
        lines.append("今日无新增命中。")
    else:
        lines.append(f"新增 {len(new_items)} 条：")
        for kw, item, real_url in new_items:
            title = strip_tags(item.get("title"))
            cloud = item.get("cloudType")
            pw = item.get("password") or "无"
            link = real_url or "(解析失败，需手动重试)"
            lines.append(f"\n【{kw}】{title}\n  类型:{cloud} 链接:{link} 提取码:{pw}")
    if quota_hit:
        lines.append("\n⚠️ 今日搜索额度已用尽，提前停止。")
    return "\n".join(lines)


def notify(text):
    try:
        sys.path.insert(0, "/Users/cuip/projects/GenericAgent/reflect_scripts")
        from reflect_common import notify_feishu
        notify_feishu(text)
    except Exception as e:
        print(f"[飞书私信推送失败] {e}")
    try:
        from notify_dingtalk_group import notify_group
        notify_group(text)
    except Exception as e:
        print(f"[钉钉群推送失败] {e}")


if __name__ == "__main__":
    new_items, quota_hit = run()
    report = build_report(new_items, quota_hit)
    print(report)
    if new_items or quota_hit:
        notify(report)


