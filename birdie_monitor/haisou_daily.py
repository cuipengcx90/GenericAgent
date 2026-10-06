"""海搜会员每日网盘资源监控：搜索→按 hsid+标题去重→仅新增通知。"""
import html
import json
import os
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
# 轮转游标：记录「下一轮从任务列表的第几个任务开始」，保证 147 个任务跨多天全被覆盖
CURSOR_PATH = Path(__file__).with_name("haisou_cursor.json")
# 每轮最多发出的搜索请求数（1 请求 = 1 关键词 × 1 平台）。
# 额度按请求数计：一轮硬扫 147 个请求必被 403 截断，且前面已扫的重复扫、后面的永远扫不到。
# 默认 45 ⇒ 约 4 轮走完一遍 147，并按游标轮转 ⇒ 覆盖率可证明。
DEFAULT_MAX_TASKS = int(os.environ.get("HAISOU_MAX_TASKS", "45"))
# 站点额度口径（2026-10-06 实测）：daily_quota=200「点」，operation_costs.search=2 点/次搜索请求
# ⇒ 每天只有 100 次搜索。默认每轮最多吃掉剩余额度的 50%，把另一半留给用户手动搜。
QUOTA_URL = BASE + "/api/v2/credits/membership/status"
COST_PER_SEARCH = 2
BUDGET_SHARE = float(os.environ.get("HAISOU_BUDGET_SHARE", "0.35"))
# 进度留痕（每轮一行 JSONL）：403 之前扫到第几个任务、命中多少，可事后取证
PROGRESS_LOG_PATH = Path(__file__).resolve().parents[1] / "temp" / "haisou_progress.jsonl"


def load_cursor():
    """读取轮转游标；文件缺失/损坏一律回落 index=0（不因此中断监控）。"""
    try:
        data = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cursor(cursor):
    try:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(json.dumps(cursor, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[游标写入失败] {exc}")


def log_progress(record):
    """追加一行 JSONL 进度；失败只打印，绝不影响主流程。"""
    try:
        PROGRESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROGRESS_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[进度留痕失败] {exc}")


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


def fetch_quota(session):
    """只读站点额度接口（不消耗搜索额度）。失败一律返回 None ⇒ 由调用方 fail-closed 处理。"""
    try:
        resp = session.get(QUOTA_URL, timeout=15)
        payload = resp.json()
    except Exception:
        return None
    data = (payload or {}).get("data") or {}
    try:
        daily = int(data.get("daily_quota"))
        remaining = int(data.get("remaining_today"))
    except (TypeError, ValueError):
        return None
    return {"daily_quota": daily, "remaining_today": remaining,
            "used_today": data.get("used_today"), "expires_at": data.get("expires_at")}


def run(max_tasks=None):
    """按「轮转游标 + 每轮请求预算」扫描，避免每轮从头发 147 个请求被 403 截断。

    返回 (additions, first_run, error) 与旧版一致；额外把本轮进度写进
    haisou_progress.jsonl、把下一轮起点写进 haisou_cursor.json。
    """
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
    # 额度闸门（2026-10-06 新增）：先读站点剩余额度，再决定本轮发多少请求。
    # 旧版硬扫 147 个请求 ⇒ 一次性吃掉当天全部 100 次搜索额度，用户自己就搜不了了。
    quota = fetch_quota(session)
    quota_error = None
    if quota is None:
        quota_error = "额度接口读取失败（fail-closed：本轮不发请求）"
    elif quota["remaining_today"] < COST_PER_SEARCH:
        quota_error = (f"今日额度已用尽（used={quota.get('used_today')}"
                       f"/quota={quota['daily_quota']}，remaining={quota['remaining_today']}）⇒ 本轮跳过")
    if quota_error:
        save_state(state)
        log_progress({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_tasks": None, "budget": 0, "done": 0,
            "start_index": None, "next_index": None, "cycle": None,
            "new_items": 0, "additions": 0, "error": quota_error,
            "quota": quota, "scanned": [],
        })
        return additions, first_run, quota_error
    # 提取已有标题用于去重
    existing_titles = [r.get("title", "") for r in state.values() if isinstance(r, dict)]

    # 合并静态关键词 + 动态关键词（全能IT课程标题）
    all_keywords = list(STATIC_KEYWORDS) + load_dynamic_keywords()
    tasks = [(kw, pf) for kw in all_keywords for pf in PLATFORMS]
    total = len(tasks)
    if total == 0:
        save_state(state)
        return additions, first_run, None

    requested = DEFAULT_MAX_TASKS if max_tasks is None else int(max_tasks)
    remaining_searches = max(0, quota["remaining_today"] // COST_PER_SEARCH)
    quota_cap = max(1, int(remaining_searches * BUDGET_SHARE))
    budget = max(1, min(requested, total, quota_cap))
    cursor = load_cursor()
    start = int(cursor.get("index", 0) or 0) % total
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    done = 0
    error = None
    scanned = []
    try:
        while done < budget:
            idx = (start + done) % total
            keyword, platform = tasks[idx]
            new_here = 0
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
                new_here += 1
            done += 1
            scanned.append({"idx": idx, "kw": keyword, "platform": platform, "new": new_here})
    except Exception as exc:
        error = str(exc)

    next_index = (start + done) % total
    cycle = int(cursor.get("cycle", 0) or 0) + (1 if next_index == 0 and done else 0)
    save_cursor({
        "index": next_index,
        "total": total,
        "cycle": cycle,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_run": {"start": started_at, "done": done, "error": error},
    })
    # 已完成的搜索结果必须落库并随异常通知发送；未扫描部分由游标留待下次执行。
    save_state(state)
    log_progress({
        "ts": started_at,
        "total_tasks": total,
        "budget": budget,
        "quota_before": quota,
        "quota_after": fetch_quota(session),
        "quota_cap": quota_cap,
        "requested": requested,
        "done": done,
        "start_index": start,
        "next_index": next_index,
        "cycle": cycle,
        "new_items": sum(s["new"] for s in scanned),
        "additions": len(additions),
        "error": error,
        "scanned": scanned,
    })
    return additions, first_run, error


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


# ---------------------------------------------------------------------------
# 自测（--selftest）：沙盒内验证「轮转游标 + 请求预算 + 进度留痕 + 异常不丢进度」。
# 全程不触网、不写生产 state/游标/进度（全部重定向到 tempfile 沙盒）。
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile
    import types

    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        print(("PASS " if cond else "FAIL ") + name)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        saved = {k: globals().get(k) for k in
                 ("STATE_PATH", "CURSOR_PATH", "PROGRESS_LOG_PATH", "DYNAMIC_KEYWORDS_PATH", "keys")}
        globals()["STATE_PATH"] = td / "state.json"
        globals()["CURSOR_PATH"] = td / "cursor.json"
        globals()["PROGRESS_LOG_PATH"] = td / "progress.jsonl"
        globals()["DYNAMIC_KEYWORDS_PATH"] = td / "dyn.json"   # 不存在 ⇒ 只有静态词
        fake_keys = types.SimpleNamespace(
            haisou_hs_web_session=types.SimpleNamespace(use=lambda: "fake-cookie"))
        globals()["keys"] = fake_keys
        saved["search"] = globals().get("search")

        total = len(STATIC_KEYWORDS) * len(PLATFORMS)
        seen_calls = []

        def fake_search(session, keyword, platform):
            seen_calls.append((keyword, platform))
            return [{"hsid": f"h-{keyword}-{platform}", "share_name": f"{keyword} 教程 {platform}",
                     "share_pwd": "", "url": f"https://x/{keyword}"}]

        globals()["search"] = fake_search
        # 额度接口打桩（自测禁触网）：默认「满额度 200 点/剩余 200」⇒ T1–T4 的预算语义与旧版一致
        saved["fetch_quota"] = globals().get("fetch_quota")
        quota_state = {"daily_quota": 200, "remaining_today": 200, "used_today": 0}

        def fake_quota(session):
            return dict(quota_state)

        globals()["fetch_quota"] = fake_quota
        try:
            # T1: 首次运行（无 state）⇒ 建基线不推送；游标前进 budget；进度落盘
            a1, first1, e1 = run(max_tasks=5)
            cur = load_cursor()
            ck("T1 first_run=True", first1 is True)
            ck("T1 error=None", e1 is None)
            ck("T1 additions=0(基线)", a1 == [])
            ck("T1 cursor.index=5", cur.get("index") == 5)
            ck("T1 total_tasks", cur.get("total") == total and total == len(STATIC_KEYWORDS) * 3)
            ck("T1 进度行=1", len(PROGRESS_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()) == 1)
            rec = json.loads(PROGRESS_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()[-1])
            ck("T1 进度 done=5", rec["done"] == 5 and rec["start_index"] == 0 and rec["next_index"] == 5)
            ck("T1 进度 scanned 明细=5", len(rec["scanned"]) == 5)

            # T2: 轮转覆盖 —— 让 budget 整除 total，跑满一圈后 index 精确回到起点 0 且 cycle+1
            save_cursor({"index": 0, "total": total, "cycle": 0})
            seen_calls.clear()
            per = total // 4
            for _ in range(4):
                run(max_tasks=per)
            ck("T2 一圈后 index=0", load_cursor().get("index") == 0)
            ck("T2 cycle=1", load_cursor().get("cycle") == 1)
            ck("T2 关键词全覆盖", {c[0] for c in seen_calls} == set(STATIC_KEYWORDS))
            ck("T2 请求数=一圈总量", len(seen_calls) == total and len(seen_calls) == 4 * per)

            # T3: 异常不丢进度 —— 第 3 个任务抛错 ⇒ 前 2 个任务的游标前进量保留
            before = load_cursor().get("index")
            state3 = {"index": before}
            save_cursor(state3)

            def boom(session, keyword, platform):
                if len([1]) and boom.n == 2:
                    raise RuntimeError("模拟 403 额度耗尽")
                boom.n += 1
                return []
            boom.n = 0
            globals()["search"] = boom
            a3, f3, e3 = run(max_tasks=5)
            after = load_cursor()
            ck("T3 error 透出", isinstance(e3, str) and "403" in e3)
            ck("T3 已完成 2 个任务的游标保留", after.get("index") == (before + 2) % total)
            ck("T3 进度行含 error", "403" in json.loads(
                PROGRESS_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()[-1])["error"])
            globals()["search"] = fake_search

            # T4: 去重 —— 同一 hsid 重复返回只入账一次
            def dup_search(session, keyword, platform):
                return [{"hsid": "same-id", "share_name": f"{keyword} 教程", "share_pwd": "", "url": "u"}]
            globals()["search"] = dup_search
            save_cursor({"index": 0})
            globals()["STATE_PATH"] = td / "state2.json"
            globals()["CURSOR_PATH"] = td / "cursor2.json"
            lines_before = len((td / "progress.jsonl").read_text(encoding="utf-8").strip().splitlines())
            _a, _f, _e = run(max_tasks=1)   # 建基线（写入 same-id）
            a4, f4, e4 = run(max_tasks=1)   # 第二轮：同 id 再出现
            lines_after = len((td / "progress.jsonl").read_text(encoding="utf-8").strip().splitlines())
            ck("T4 第二轮不重复入账", a4 == [] and f4 is False)
            ck("T4 每轮各留 1 行进度", lines_after - lines_before == 2)
            globals()["search"] = fake_search   # 计数版装回，否则 T5–T7 的「发了几发」是空判
            # T5 额度闸门：剩余 0 ⇒ 一个请求都不发、零入账、错误可读
            globals()["STATE_PATH"] = td / "state3.json"
            globals()["CURSOR_PATH"] = td / "cursor3.json"
            save_cursor({"index": 0})
            quota_state.update({"remaining_today": 0, "used_today": 200})
            n_before = len(seen_calls)
            a5, f5, e5 = run(max_tasks=5)
            ck("T5 额度耗尽⇒零请求", len(seen_calls) == n_before and a5 == [])
            ck("T5 错误信息可读", "额度" in (e5 or "") and "跳过" in (e5 or ""))
            # T6 份额封顶：remaining=200 ⇒ 100 次可用，只允许吃 50%（BUDGET_SHARE=0.5）
            quota_state.update({"remaining_today": 200, "used_today": 0})
            globals()["STATE_PATH"] = td / "state4.json"
            globals()["CURSOR_PATH"] = td / "cursor4.json"
            save_cursor({"index": 0})
            n0 = len(seen_calls)
            a6, f6, e6 = run(max_tasks=999)
            expect6 = max(1, min(999, total, int((200 // COST_PER_SEARCH) * BUDGET_SHARE)))
            ck(f"T6 份额封顶={expect6} 请求", len(seen_calls) - n0 == expect6)
            # T7 额度接口失败 ⇒ fail-closed（不发请求）
            globals()["fetch_quota"] = lambda session: None
            n1 = len(seen_calls)
            a7, f7, e7 = run(max_tasks=5)
            ck("T7 接口失败 fail-closed", len(seen_calls) == n1 and "fail-closed" in (e7 or ""))
        finally:
            for k, v in saved.items():
                globals()[k] = v
    ok = sum(1 for _, c in checks if c)
    print(f"\nselftest: {ok}/{len(checks)} checks passed")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _max_tasks = None
    if "--max-tasks" in sys.argv:
        try:
            _max_tasks = int(sys.argv[sys.argv.index("--max-tasks") + 1])
        except Exception:
            print("用法: haisou_daily.py [--max-tasks N]")
            sys.exit(2)
    additions, first_run, error = run(max_tasks=_max_tasks)
    report = build_report(additions, first_run, error)
    print(report)
    # 首次运行只建基线；之后仅异常或新增时通知。
    if error or additions:
        notify(report)
