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

# AI 主题判据单一源（2026-10-07 R559：用户要求海搜只要 AI 结果）
sys.path.insert(0, "/Users/cuip/projects/GenericAgent")
try:
    from tools.ai_topic import is_ai_topic  # noqa: E402
    _AI_SRC = "tools/ai_topic.py (单一源)"
except Exception:  # 回退：离线内联判据（与单一源同一口径的保守子集）
    _AI_SRC = "inline fallback (保守子集)"
    _AI_POS = ("人工智能", "大模型", "智能体", "机器学习", "深度学习", "提示词",
               "生成式", "文生图", "文生视频", "多模态", "具身", "ai")
    _AI_NEG = ("hadoop", "docker", "kubernetes", "k8s", "spring", "微服务",
               "软考", "影视后期", "剪辑")
    def is_ai_topic(text):
        t = (text or "").lower()
        if any(p in t for p in _AI_POS):
            return True
        return False  # 严格档：无正向信号一律 False（UNKNOWN/NON_AI 同判）

AI_SKIPPED = [0]  # AI 主题门（结果侧）丢弃计数，供运行摘要与统计读取
# AI 门总开关：生产恒 True；自测用合成语料（"接口测试 教程 quark" 等无 AI 语义）
# 会误伤既有 T1–T10 的预算/轮转断言，故自测期间临时关闭，并由 T11 单独覆盖门控行为。
AI_GATE_ENABLED = True

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
# 静态词每日轮转配额（2026-10-06）：静态词（AI测试/柠檬班…）历史上每天被 72 请求全量重搜、
# 增量≈0；改为每天只轮转一部分，把预算让给课程词。
DEFAULT_STATIC_DAILY = int(os.environ.get("HAISOU_STATIC_DAILY", "15"))
# 课程词平台优先级：先 quark（网盘资源最集中），baidu/ali 用剩余预算补齐。
DYNAMIC_PLATFORM_ORDER = ("quark", "baidu", "ali")
# 站点额度口径（2026-10-06 实测）：daily_quota=200「点」，operation_costs.search=2 点/次搜索请求
# ⇒ 每天只有 100 次搜索。默认每轮最多吃掉剩余额度的 50%，把另一半留给用户手动搜。
QUOTA_URL = BASE + "/api/v2/credits/membership/status"
COST_PER_SEARCH = 2
BUDGET_SHARE = float(os.environ.get("HAISOU_BUDGET_SHARE", "0.35"))
# 进度留痕（每轮一行 JSONL）：403 之前扫到第几个任务、命中多少，可事后取证
PROGRESS_LOG_PATH = Path(__file__).resolve().parents[1] / "temp" / "haisou_progress.jsonl"


def rotate_window(items, start, head):
    """从 start 处环形切出长度 head 的窗口（纯函数，便于自测轮转覆盖）。"""
    n = len(items)
    if n == 0 or head <= 0:
        return []
    s = int(start or 0) % n
    return (items[s:] + items[:s])[:head]


def next_dyn_index(dyn_total, head, dyn_start):
    """动态词窗口推进：窗口装不下全部词时按 head 步进；装得下则归零。"""
    if dyn_total <= 0 or dyn_total <= head:
        return 0
    return (int(dyn_start or 0) + head) % dyn_total


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


def core_query(title):
    """把课程全名压成「更适合网盘搜索」的核心词。

    网盘上传命名常与课程原名不一致（年份/期数/「完结无密」等修饰被改），全名当 query 常 0 命中；
    去修饰后的核心词命中率更高。此处只做保守剥离（括号内容/年份/期数/常见后缀），不猜简称。
    """
    q = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", " ", title or "")
    q = re.sub(r"(20\d{2}\s*年?|第[一二三四五六七八九十百\d]+期|完结|无密|完整版|全套|高清|"
               r"视频\+?代码|视频教程|更新完|新课|首发)", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" -_·|、,，")
    return q


def load_dynamic_keywords():
    """加载动态关键词（全能IT课程标题）。

    返回 list[dict]={title, query, published_at}，仅含尚未 found 的课程；
    按发布时间倒序（新课程优先），query 取文件里登记的核心词、缺失则现算。
    """
    if not DYNAMIC_KEYWORDS_PATH.exists():
        return []
    data = json.loads(DYNAMIC_KEYWORDS_PATH.read_text(encoding="utf-8"))
    rows = []
    for title, info in data.items():
        if not isinstance(info, dict):
            info = {}
        if info.get("found", False):
            continue
        q = (info.get("query") or "").strip() or core_query(title) or title
        rows.append({"title": title, "query": q, "published_at": info.get("published_at") or ""})
    rows.sort(key=lambda r: r["published_at"], reverse=True)
    return rows


def record_keyword_attempt(title, queries):
    """把「今天用什么 query 搜过、各命中多少条原始结果」写回课程词文件。

    旧版从不递增 search_count ⇒ 「超期未找到自动清理」永不触发、进度也不可观测。
    进度写回失败绝不影响主流程。
    """
    if not title or not DYNAMIC_KEYWORDS_PATH.exists():
        return
    try:
        data = json.loads(DYNAMIC_KEYWORDS_PATH.read_text(encoding="utf-8"))
        if title not in data or not isinstance(data[title], dict):
            return
        info = data[title]
        info["search_count"] = int(info.get("search_count", 0) or 0) + 1
        info["last_search_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        info["last_queries"] = queries
        DYNAMIC_KEYWORDS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[warn] 课程词进度写回失败（不影响扫描）: {exc}")


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


def run(max_tasks=None, static_daily=None):
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

    # 任务优先级（2026-10-06 重排，修「搜不出东西却把额度烧光」）：
    #   P0 课程词 × quark      —— 用户主诉求（排行榜站新课程）必须每天先跑
    #   P1 静态词轮转窗口      —— 老词增量极低，每天只轮转一部分
    #   P2 课程词 × baidu/ali  —— 仅用剩余预算补齐
    dynamic = load_dynamic_keywords()
    # 查询侧 AI 门（2026-10-07 R559）：非 AI 词根本不发请求（词源含 imooc 云计算&大数据
    # 方向与全能IT排行站，天然混入 Hadoop/Docker/K8s/Spring 等；发出去必然自证命中）。
    n0 = len(dynamic)
    if AI_GATE_ENABLED:
        dynamic = [d for d in dynamic
                   if is_ai_topic(d.get("query", "")) or is_ai_topic(d.get("title", ""))]
    if len(dynamic) != n0:
        print(f"[AI查询门] 动态词过滤: {n0} -> {len(dynamic)}（{n0 - len(dynamic)} 个非 AI 词跳过）")
    s0 = len(STATIC_KEYWORDS)
    static_kws = [kw for kw in STATIC_KEYWORDS if (not AI_GATE_ENABLED) or is_ai_topic(kw)]
    if len(static_kws) != s0:
        print(f"[AI查询门] 静态词过滤: {s0} -> {len(static_kws)}（{s0 - len(static_kws)} 个非 AI 词跳过）")
    dyn_quark = [(d["query"], "quark", True, d["title"]) for d in dynamic]
    static_tasks = [(kw, pf, False, None) for kw in static_kws for pf in PLATFORMS]
    dyn_other = [(d["query"], pf, True, d["title"])
                 for pf in DYNAMIC_PLATFORM_ORDER[1:] for d in dynamic]
    total = len(dyn_quark) + len(static_tasks) + len(dyn_other)
    if total == 0:
        save_state(state)
        return additions, first_run, None

    requested = DEFAULT_MAX_TASKS if max_tasks is None else int(max_tasks)
    static_daily = DEFAULT_STATIC_DAILY if static_daily is None else int(static_daily)
    remaining_searches = max(0, quota["remaining_today"] // COST_PER_SEARCH)
    quota_cap = max(1, int(remaining_searches * BUDGET_SHARE))
    budget = max(1, min(requested, total, quota_cap))
    cursor = load_cursor()
    static_total = len(static_tasks)
    start = int(cursor.get("index", 0) or 0) % max(1, static_total)
    # 静态词保底窗口（2026-10-06）：课程词/池词增长到 ≥ 预算时，旧式 dyn[:budget] 会把静态面
    # 挤成 0（静态词永不覆盖）。这里先给静态留一个 1/5 保底槽位，再让动态词占其余。
    static_floor = min(static_total, max(1, budget // 5))
    head = max(0, budget - static_floor)
    # 动态词轮转窗口（2026-10-06）：动态词 ≥ 窗口时旧式 dyn[:head] 会让排在后面的课程池词
    # 永远轮不到（实测 37 条动态词 / 窗口 28 ⇒ 12 条池词只覆盖 3 条）。按游标轮转起点，
    # 保证 ⌈动态词数/窗口⌉ 天内每个词至少被搜一次。
    dyn_total = len(dyn_quark)
    dyn_start = int(cursor.get("dyn_index", 0) or 0) % max(1, dyn_total)
    plan = list(rotate_window(dyn_quark, dyn_start, head))
    remaining = max(0, budget - len(plan))
    # 静态词窗口：至少 static_daily，至多占剩余预算的一半（另一半留给课程词的 baidu/ali 补齐）
    static_n = min(static_total, remaining, max(static_daily, remaining // 2))
    plan += [static_tasks[(start + i) % static_total] for i in range(static_n)]
    remaining = budget - len(plan)
    if remaining > 0:
        plan += dyn_other[:remaining]
    remaining = budget - len(plan)
    if remaining > 0:   # 课程词余额用不完 ⇒ 继续轮转静态词，不让当日额度空转
        more = min(static_total - static_n, remaining)
        plan += [static_tasks[(start + static_n + i) % static_total] for i in range(more)]
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    done = 0
    error = None
    scanned = []
    dyn_hits = {}
    static_used = 0
    for keyword, platform, is_dyn, course_title in plan:
        if done >= budget:
            break
        variants = [keyword]
        if is_dyn and platform == DYNAMIC_PLATFORM_ORDER[0]:
            core = core_query(course_title or keyword)
            if core and core != keyword:
                variants.append(core)  # 全名 0 原始命中时回落核心词（多花 1 次搜索）
        new_here = 0
        queries = []
        try:
            for q in variants:
                if done >= budget:
                    break
                items = search(session, q, platform)
                done += 1
                new_from_q = 0
                for item in items:
                    hsid = item.get("hsid")
                    if not hsid or hsid in state:
                        continue
                    # 标题去重检查
                    title = clean(item.get("share_name"))
                    # 相关性校验：过滤 haisou 分词模糊匹配带来的无关垃圾结果
                    if not is_relevant(q, title):
                        continue
                    # AI 主题门（2026-10-07 R559）：is_relevant 是自指判据（标题含查询词即真），
                    # 非 AI 词（Hadoop/Docker/K8s/Spring/软考/影视后期…）一旦发出去必然自证命中，
                    # 故此处再做一次与词源无关的独立判定，只放行 AI 主题。
                    if AI_GATE_ENABLED and not is_ai_topic(title):
                        AI_SKIPPED[0] += 1
                        continue
                    if is_title_duplicate(title, existing_titles):
                        continue
                    record = make_item(q, item)
                    state[hsid] = record
                    existing_titles.append(title)  # 添加到已有标题列表
                    if not first_run:
                        additions.append(record)
                    new_from_q += 1
                    new_here += 1
                queries.append({"q": q, "platform": platform,
                                "items": len(items), "new": new_from_q})
                if items:  # 该 query 有原始命中 ⇒ 不必再试下一个形态
                    break
        except Exception as exc:
            error = str(exc)
            scanned.append({"idx": len(scanned), "kw": keyword, "platform": platform,
                            "new": new_here, "is_dynamic": is_dyn, "error": str(exc)})
            break
        if not is_dyn:
            static_used += 1
        elif course_title:
            dyn_hits.setdefault(course_title, []).extend(queries)
            if any(q["new"] > 0 for q in queries):
                mark_keyword_found(course_title)
        scanned.append({"idx": len(scanned), "kw": keyword, "platform": platform,
                        "new": new_here, "is_dynamic": is_dyn, "queries": queries})

    for course_title, queries in dyn_hits.items():
        record_keyword_attempt(course_title, queries)

    next_index = (start + static_used) % max(1, static_total)
    cycle = int(cursor.get("cycle", 0) or 0) + (1 if next_index == 0 and static_used else 0)
    # 动态词窗口推进：窗口小于动态词总数时按 head 步进轮转；一次能装下则回到 0。
    dyn_next = next_dyn_index(dyn_total, head, dyn_start)
    save_cursor({
        "index": next_index,
        "total": static_total,
        "cycle": cycle,
        "dyn_index": dyn_next,
        "dyn_total": dyn_total,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_run": {"start": started_at, "done": done, "error": error,
                     "static_used": static_used, "plan_len": len(plan)},
    })
    # 已完成的搜索结果必须落库并随异常通知发送；未扫描部分由游标留待下次执行。
    save_state(state)
    log_progress({
        "ts": started_at,
        "total_tasks": total,
        "dyn_total": len(dyn_quark),
        "static_total": static_total,
        "plan_len": len(plan),
        "static_used": static_used,
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
        "ai_skipped": AI_SKIPPED[0],
        "error": error,
        "scanned": scanned,
    })
    if AI_SKIPPED[0]:
        print(f"[AI结果门] 丢弃非 AI 命中 {AI_SKIPPED[0]} 条（{_AI_SRC}）")
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
                 ("STATE_PATH", "CURSOR_PATH", "PROGRESS_LOG_PATH", "DYNAMIC_KEYWORDS_PATH", "keys",
                  "AI_GATE_ENABLED")}
        globals()["STATE_PATH"] = td / "state.json"
        globals()["CURSOR_PATH"] = td / "cursor.json"
        globals()["PROGRESS_LOG_PATH"] = td / "progress.jsonl"
        globals()["DYNAMIC_KEYWORDS_PATH"] = td / "dyn.json"   # 不存在 ⇒ 只有静态词
        # 合成语料（"接口测试 教程 quark" 等）无 AI 语义，开闸会把 T1–T10 的预算/轮转断言
        # 全部打成"空结果"⇒ 自测期间关闸，门控行为由 T11 用单元判据单独覆盖。
        globals()["AI_GATE_ENABLED"] = False
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
            # 契约（2026-10-06 改）：静态词按窗口轮转，多轮累计覆盖全量后才回到起点
            save_cursor({"index": 0, "total": total, "cycle": 0})
            seen_calls.clear()
            per = total // 4
            for _ in range(4):
                run(max_tasks=per, static_daily=per)
            ck("T2 一盘后 index=0", load_cursor().get("index") == 0)
            ck("T2 cycle=1", load_cursor().get("cycle") == 1)
            ck("T2 关键词全覆盖", {c[0] for c in seen_calls} == set(STATIC_KEYWORDS))
            ck("T2 请求数=一盘总量", len(seen_calls) == total and len(seen_calls) == 4 * per)

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
            a6, f6, e6 = run(max_tasks=999)   # 无课程词 ⇒ 余额全部轮转静态词，仍受份额封顶
            expect6 = max(1, min(999, total, int((200 // COST_PER_SEARCH) * BUDGET_SHARE)))
            ck(f"T6 份额封顶={expect6} 请求", len(seen_calls) - n0 == expect6)
            # T8 课程词优先 + 全名 0 命中回落核心词 + 逐词留痕写回
            dyn_path = td / "dyn.json"
            long_name = "新课程（第3期）完结无密"
            dyn_path.write_text(json.dumps(
                {long_name: {"source_url": "u", "query": long_name, "search_count": 0, "found": False}},
                ensure_ascii=False), encoding="utf-8")
            globals()["DYNAMIC_KEYWORDS_PATH"] = dyn_path
            globals()["STATE_PATH"] = td / "state8.json"
            globals()["CURSOR_PATH"] = td / "cursor8.json"
            save_cursor({"index": 0})
            calls8 = []

            def search8(session, q, platform):
                calls8.append((q, platform))
                return []          # 全 0 命中 ⇒ 必须回落核心词

            globals()["search"] = search8
            a8, f8, e8 = run(max_tasks=4, static_daily=1)
            cq = core_query(long_name)
            ck("T8 课程词原样先搜", calls8 and calls8[0] == (long_name, "quark"))
            ck("T8 0 命中回落核心词", len(calls8) > 1 and calls8[1] == (cq, "quark"))
            ck("T8 静态词排在其后", len(calls8) > 2 and calls8[2][0] in STATIC_KEYWORDS)
            back = json.loads(dyn_path.read_text(encoding="utf-8"))[long_name]
            ck("T8 进度写回 attempts", back.get("search_count") == 1)
            lq8 = back.get("last_queries") or []
            ck("T8 进度写回 queries", [q["q"] for q in lq8][:2] == [long_name, cq])
            ck("T8 落痕含平台", [q["platform"] for q in lq8][:2] == ["quark", "quark"])
            ck("T8 未找到不置 found", back.get("found") is False)
            globals()["DYNAMIC_KEYWORDS_PATH"] = td / "dyn_missing.json"   # 复原：不存在
            globals()["search"] = fake_search

            # T7 额度接口失败 ⇒ fail-closed（不发请求）
            globals()["fetch_quota"] = lambda session: None
            n1 = len(seen_calls)
            a7, f7, e7 = run(max_tasks=5)
            ck("T7 接口失败 fail-closed", len(seen_calls) == n1 and "fail-closed" in (e7 or ""))

            # T9 静态保底（2026-10-06）：课程词条数 ≥ 预算时，静态面不得被挤成 0
            globals()["fetch_quota"] = fake_quota          # T7 曾把它换成 fail-closed
            quota_state.update({"remaining_today": 200, "used_today": 0})
            many = {f"课程{i}": {"source_url": "u", "query": f"课程{i}",
                                 "search_count": 0, "found": False} for i in range(40)}
            dyn9 = td / "dyn9.json"
            dyn9.write_text(json.dumps(many, ensure_ascii=False), encoding="utf-8")
            globals()["DYNAMIC_KEYWORDS_PATH"] = dyn9
            globals()["STATE_PATH"] = td / "state9.json"
            globals()["CURSOR_PATH"] = td / "cursor9.json"
            save_cursor({"index": 0})
            calls9 = []

            def search9(session, q, platform):
                calls9.append((q, platform))
                return [{"hsid": f"{q}-{platform}", "share_name": f"{q} 课程",
                         "share_pwd": "", "url": "u"}]

            globals()["search"] = search9
            quota_state.update({"remaining_today": 200, "used_today": 0})
            _a9, _f9, _e9 = run(max_tasks=10)
            stat9 = [q for q, _p in calls9 if q in STATIC_KEYWORDS]
            ck("T9 课程词占满仍轮转静态词", len(calls9) == 10 and len(stat9) >= 1)
            ck("T9 保底量级=预算 1/5", len(stat9) == max(1, 10 // 5))
            ck("T9 未被保底的动态词让位", sum(1 for q, _p in calls9 if q not in STATIC_KEYWORDS) == 8)
            globals()["DYNAMIC_KEYWORDS_PATH"] = td / "dyn_missing.json"
            globals()["search"] = fake_search

            # T10 动态词轮转窗口覆盖（修「窗口外的池词永远轮不到」）
            words10 = [f"w{i}" for i in range(37)]
            head10 = 28
            start = 0
            seen = set()
            for _ in range(5):          # 模拟连续 5 轮
                win = rotate_window(words10, start, head10)
                ck(f"T10 窗口长度={head10}", len(win) == head10)
                assert len(set(win)) == len(win)
                seen.update(win)
                start = next_dyn_index(len(words10), head10, start)
            ck("T10 五轮覆盖全部 37 词", len(seen) == 37)
            ck("T10 轮转步进=窗口长", next_dyn_index(37, 28, 0) == 28)
            ck("T10 步进回绕取模", next_dyn_index(37, 28, 28) == 19)
            ck("T10 窗口装得下时归零", next_dyn_index(10, 10, 3) == 0)
            ck("T10 空列表安全", rotate_window([], 0, 5) == [] and next_dyn_index(0, 5, 0) == 0)
            # 边界：窗口刚好等于词数 ⇒ 每轮同一批（全量覆盖不丢）
            ck("T10 窗口=全量时不轮转", next_dyn_index(28, 28, 7) == 0)

            # T11 AI 主题门（起因：09-29 推送 37 条里 29 条非 AI，来自 imooc 池词 Hadoop/Docker/K8s）
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "ai_topic_probe", str(Path(__file__).resolve().parent.parent / "tools" / "ai_topic.py"))
            _at = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_at)
            ck("T11 非AI:Hadoop 标题被拒",
               _at.is_ai_topic("Hadoop 系统入门+核心精讲") is False)
            ck("T11 非AI:Docker/K8s 标题被拒",
               _at.is_ai_topic("Docker+Kubernetes 实战") is False)
            ck("T11 非AI:软件测试 标题被拒",
               _at.is_ai_topic("软件测试 教程 quark") is False)
            ck("T11 AI:AI agent 标题放行", _at.is_ai_topic("AI agent大师之路") is True)
            ck("T11 AI:Claude Code 标题放行", _at.is_ai_topic("Claude Code 实战") is True)
            _n_ai = [k for k in STATIC_KEYWORDS if _at.is_ai_topic(k)]
            ck("T11 静态词门:确有非 AI 词被砍", 0 < len(_n_ai) < len(STATIC_KEYWORDS))
            ck("T11 静态词门:AI 词全部保留",
               all(k in _n_ai for k in ("AI Agent", "Claude Code", "AI测试")))
            # 结果门接线（2026-10-07 重写）：用固定混合语料（1 条 AI + 1 条非 AI）单测结果门本体。
            # 不能复用 fake_search：其标题由查询词拼成，而查询侧门已把静态词收窄成 AI 词
            # ⇒ 合成标题全是 AI 标题、结果门无事可做（旧断言因此假红）。故同时钉住
            # is_relevant（上游相关性门）以隔离被测对象，只留结果门这一层判据。
            # ⚠️ 语料标题须与旧用例的合成标题（形如 "<kw> 教程"）低相似：实测
            # "AI Agent 实战课程" 与 T 系列早前落库的 "AI Agent 教程" 相似度 ≥0.85
            # ⇒ 被 is_title_duplicate 静默丢弃（真因在去重、不在结果门），断言假红。
            _mix11 = [
                {"hsid": "t11-ai", "share_name": "大模型微调实战", "share_pwd": "", "url": "u"},
                {"hsid": "t11-cc", "share_name": "Excel 函数实战", "share_pwd": "", "url": "u"},
            ]

            def search11(session, q, platform):
                return [dict(h) for h in _mix11]

            _saved_rel = globals()["is_relevant"]
            _saved_search11 = globals()["search"]
            globals()["search"] = search11
            globals()["is_relevant"] = lambda q, title: True   # 隔离上游门，只测结果门
            globals()["AI_GATE_ENABLED"] = True
            AI_SKIPPED[0] = 0
            try:
                _a11, _f11, _e11 = run(max_tasks=1)
                _sk11 = AI_SKIPPED[0]
            finally:
                globals()["AI_GATE_ENABLED"] = False
                globals()["is_relevant"] = _saved_rel
                globals()["search"] = _saved_search11
            _kept11 = [r.get("title", "") for r in (_a11 or [])]
            _k11 = [k for k in (load_state() or {}) if str(k).startswith("t11")]
            print(f"[dbg T11] kept={_kept11} keys={_k11} skipped={_sk11}")
            ck(f"T11 结果门接线:开闸只留 AI 命中"
               f"(kept={_kept11} skipped={_sk11} first={_f11} err={_e11})",
               len(_kept11) == 1 and "大模型" in _kept11[0]
               and "Excel" not in _kept11[0] and _sk11 == 1)
            globals()["AI_SKIPPED"][0] = 0
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
