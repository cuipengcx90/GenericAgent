"""海搜会员每日网盘资源监控：搜索→按 hsid+标题去重→仅新增通知。"""
import html
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

sys.path.insert(0, "/Users/cuip/projects/GenericAgent/memory")
from keychain import keys

BASE = "https://haisou.cc"
KEYCHAIN_NAME = "haisou_hs_web_session"
# 静态关键词（手动维护）
STATIC_KEYWORDS = [
    # AI开发
    "AI Agent", "AI coding", "AI应用开发", "出海SaaS", "Vibe Coding",
    "Claude Code", "AI创业", "独立开发出海", "SaaS出海", "MCP教程",
    "AI产品出海", "懂王", "鸡翅大模型", "赋范",
    # AI测试
    "AI测试", "软件测试", "自动化测试", "柠檬班", "华测", "华测教育",
    "接口测试", "性能测试", "测试开发", "测试工程师",
]
PLATFORMS = ("quark", "baidu", "ali")
LINK_TEMPLATES = {
    "quark": "https://pan.quark.cn/s/{code}",
    "baidu": "https://pan.baidu.com/s/{code}",
    "ali": "https://www.alipan.com/s/{code}",
}
STATE_PATH = Path(__file__).with_name("haisou_seen_items.json")
# 动态关键词文件（全能IT课程标题）
DYNAMIC_KEYWORDS_PATH = Path(__file__).with_name("quangneng_keywords.json")


def clean(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def load_state():
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def load_dynamic_keywords():
    """加载动态关键词（全能IT课程标题），仅返回未找到的课程"""
    if not DYNAMIC_KEYWORDS_PATH.exists():
        return []
    data = json.loads(DYNAMIC_KEYWORDS_PATH.read_text(encoding="utf-8"))
    # 仅返回未找到的课程标题
    return [title for title, info in data.items() if not info.get("found", False)]


def mark_keyword_found(keyword):
    """标记动态关键词已找到"""
    if not DYNAMIC_KEYWORDS_PATH.exists():
        return
    data = json.loads(DYNAMIC_KEYWORDS_PATH.read_text(encoding="utf-8"))
    if keyword in data:
        data[keyword]["found"] = True
        data[keyword]["found_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        DYNAMIC_KEYWORDS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def normalize_title(title):
    """标题归一化：去噪后用于相似度比对"""
    if not title:
        return ""
    # 转小写，去除常见噪声词
    s = title.lower()
    noise = ["完结", "更新", "全集", "高清", "1080p", "720p", "百度网盘", "阿里云盘", "夸克网盘",
             "(", ")", "（", "）", "[", "]", "【", "】", "|", "-", "_", "·"]
    for n in noise:
        s = s.replace(n, "")
    # 去除多余空格
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def is_title_duplicate(new_title, existing_titles, threshold=0.85):
    """判断标题是否与已有标题重复（相似度>threshold）"""
    norm_new = normalize_title(new_title)
    if not norm_new:
        return False
    for existing in existing_titles:
        norm_exist = normalize_title(existing)
        if not norm_exist:
            continue
        ratio = SequenceMatcher(None, norm_new, norm_exist).ratio()
        if ratio >= threshold:
            return True
    return False


def tokenize_keyword(keyword):
    """把关键词按分隔符及中英文边界切成核心词，过滤过短噪声片段。"""
    # 如“和橘子学AI创作”切成“和橘子学”“ai”“创作”，避免整串无法命中。
    keyword = re.sub(r"([\u4e00-\u9fff])([a-zA-Z0-9])", r"\1 \2", keyword)
    keyword = re.sub(r"([a-zA-Z0-9])([\u4e00-\u9fff])", r"\1 \2", keyword)
    parts = re.split(r"[|｜/,，、\s()（）\[\]【】\-]+", keyword)
    tokens = [p for p in parts if len(p) >= 2]
    return tokens or [keyword]


# --- 相关性判据：单一源 = <repo>/tools/haisou_relevance.py（A/B 复算见报告 R441） ---
# 旧版按 max(tokens, key=len) 取核心词，对 "AI测试" 等关键词会因等长取首拿到 "ai"
# ⇒ 含 ai 即放行（2026-09-22/23 累计 4 条假阳性）。新判据先剔通用/弱信号 token 再取最长，
# 并支持同义词表与 ASCII 连写形态（ClaudeCode/VibeCoding）。
_JUDGER_SRC = "tools/haisou_relevance.py (单一源)"
try:
    _ROOT = Path(__file__).resolve().parents[1]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from tools.haisou_relevance import is_relevant  # noqa: E402,F401
except Exception as _e:  # 回退：内联降级版（无同义词表），保证 monitor 不因缺文件而中断
    _JUDGER_SRC = f"inline-fallback ({type(_e).__name__})"
    _GENERIC = {
        "ai", "aigc", "ai+", "大模型", "课程", "教程", "视频", "网课", "全套",
        "合集", "课程合集", "资源", "资料", "学习", "培训", "训练营",
    }

    def is_relevant(keyword, title):
        """降级版：剔除通用/弱信号 token 后，最长核心词须命中标题。"""
        title_l = (title or "").lower()
        toks = tokenize_keyword(keyword.lower())
        cand = [t for t in toks if t not in _GENERIC and not (re.fullmatch(r"[a-z0-9]+", t) and len(t) <= 3)]
        cand = cand or toks
        if not cand:
            return False
        return max(cand, key=len) in title_l


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
    # 提取已有标题用于去重
    existing_titles = [r.get("title", "") for r in state.values() if isinstance(r, dict)]

    # 合并静态关键词 + 动态关键词（全能IT课程标题）
    all_keywords = list(STATIC_KEYWORDS) + load_dynamic_keywords()

    try:
        for keyword in all_keywords:
            for platform in PLATFORMS:
                for item in search(session, keyword, platform):
                    hsid = item.get("hsid")
                    if not hsid or hsid in state:
                        continue
                    # 标题去重检查
                    title = clean(item.get("share_name"))
                    # 相关性校验：过滤 haisou 分词模糊匹配带来的无关垃圾结果
                    if not is_relevant(keyword, title):
                        continue
                    if is_title_duplicate(title, existing_titles):
                        continue
                    record = make_item(keyword, item)
                    state[hsid] = record
                    existing_titles.append(title)  # 添加到已有标题列表

                    # 如果是动态关键词（全能IT课程），标记为已找到
                    if keyword not in STATIC_KEYWORDS:
                        mark_keyword_found(keyword)

                    if not first_run:
                        additions.append(record)
    except Exception as exc:
        # 已完成的搜索结果必须落库并随异常通知发送；未扫描部分留待下次执行。
        save_state(state)
        return additions, first_run, str(exc)
    save_state(state)
    return additions, first_run, None


def build_report(additions, first_run, error):
    date = time.strftime("%Y-%m-%d")
    if first_run and not additions:
        lines = [f"✅ 海搜监控已建立基线（{date}），后续仅推送新增资源。"]
    else:
        lines = [f"🔎 海搜每日监控（{date}）", f"新增 {len(additions)} 条："]
        for item in additions:
            password = item["share_pwd"] or "无"
            lines.append(f"\n【{item['keyword']}｜{item['platform']}】{item['title']}\n  链接: {item['link'] or '未知'}  提取码: {password}")
    if error:
        lines.append(f"\n⚠️ 后续扫描中断：{error}\n已搜到的新增已照常发送；未扫描部分将在下次继续。")
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
