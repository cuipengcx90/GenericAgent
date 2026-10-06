#!/usr/bin/env python3
"""课程池（AI 课程资源台 P1 内核）：多源课程登记 / 跨源去重 / 状态星级 / 搜索词产出 / 看板渲染。

数据分层（耐久性政策 R507）：
- 派生数据（可重抓）：temp/course_pool/pool.json      —— 源适配器每天刷新，gitignored
- 用户数据（不可再生）：memory/course_pool/user_state.json —— 星级/优先级/状态/备注/已找到资源
  （memory/** 被 .gitignore 忽略，非 .md 需 `git add -f` 才能进克隆）

子命令（位置参数，非 --flag；`python3 tools/course_pool.py <子命令> --help` 为权威）：
  fetch-imooc [--dirs D] [--max-pages N] [--dry-run]
                                        抓慕课实战课（内置适配器）入库
  fetch [--source ID] [--max-urls N] [--detail] [--sleep S] [--dry-run] [--include-disabled]
                                        按注册表多源抓取（注册表=tools/course_sources.json）
  sources [--json]                      列出跨源采集注册表
  add-source ID [--name N] [--url U]… [--parser P] [--detail D] [--channel C] [--kind K]
             [--note T] [--disabled] [--force]
                                        新增/覆盖采集源（扩展新站点只改注册表，不改代码）
  add-url ID URL…                       给源追加 URL
  remove-url ID URL…                    从源移除 URL
  upsert --source S [--in-file F|stdin] 入库（含跨源去重）
  list [--kw K] [--status S] [--source S] [--limit N]
  star <course_id> <0-3> [--note T]
  status <course_id> <hunting|found|ignore>
  attach <course_id> <pan_url> [--title T] [--via V]
                                        登记已找到的网盘资源
  words [--limit N] [--out path] [--merge-into path|--no-merge]
                                        产出搜索词文件（默认 birdie_monitor/pool_keywords.json）
                                        并默认合并进 haisou 动态词表（birdie_monitor/quangneng_keywords.json）
  match [--min-score S] [--max-per-course N] [--json-out P] [--apply]
                                        把海搜/盘搜命中按标题相似度挂到课程（默认 dry-run 只预览）
                                        --json-out 写全量候选快照（供看板上「网盘命中候选」面板）
  render [--out path] [--candidates P]  渲染看板（默认 archive/courses/index.html，8899 静态站直接可见）
                                        --candidates 候选快照（默认 temp/course_pool/match_candidates.json，存在才上板）
  stats
  selftest                              内置自测（负控+夹具，不触碰生产数据）
"""
from __future__ import annotations

import argparse
import difflib
import subprocess
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
POOL_PATH = Path(os.environ.get("COURSE_POOL_PATH") or BASE / "temp/course_pool/pool.json")
USER_PATH = Path(os.environ.get("COURSE_USER_PATH") or BASE / "memory/course_pool/user_state.json")
WORDS_OUT = Path(os.environ.get("COURSE_WORDS_OUT") or BASE / "birdie_monitor/pool_keywords.json")
DASH_OUT = Path(os.environ.get("COURSE_DASH_OUT") or BASE / "archive/courses/index.html")
# haisou 每天真正读的动态词表（sync_quangneng.py 是它的作者）：
# 池词合并进去 = 课程池直接给海搜供词，无需 haisou 侧改代码。
MERGE_INTO = Path(os.environ.get("COURSE_MERGE_INTO") or BASE / "birdie_monitor/quangneng_keywords.json")

# ---- 搜索命中 → 课程自动匹配（match 子命令）----
HAISOU_SEEN = Path(os.environ.get("COURSE_HAISOU_SEEN") or BASE / "birdie_monitor/haisou_seen_items.json")
PAN_RAW_GLOB = os.environ.get("COURSE_PAN_RAW_GLOB") or "temp/pan_search/raw_*.json"
PAN_SEEN = Path(os.environ.get("COURSE_PAN_SEEN") or BASE / "temp/pan_search/seen.json")
MATCH_MIN_SCORE = 0.62          # 标题 bigram 覆盖率阈值（低于此不挂，宁缺勿滥）
MATCH_MAX_PER_COURSE = 3        # 单课程最多挂几条命中
MATCH_EXTRA_RX = re.compile(r"(网盘|资源|下载|课程|教程|合集|打包|分享|学习资料)")
# match 干跑快照落点：看板（render）读它上「网盘命中候选」面板；快照本身是只读视图，
# 与 resources（真挂载）完全两回事——挂载必须人工跑 match --apply。
CAND_OUT = Path(os.environ.get("COURSE_CAND_OUT")
                or BASE / "temp/course_pool/match_candidates.json")
CAND_BOARD_LIMIT = 200          # 看板最多展示多少条候选（快照里仍是全量）

# 采集面新鲜度台账（R552 看板 v2）：每次真跑 fetch 落一次摘要，看板读它上「采集面」面板。
# 只记真实抓取（dry-run 不落）；通道 mode 分布/缓存回退 stale 由此可见——「今天抓了吗、
# 抓的是真页面还是 last-good 缓存」一眼可判，不再靠翻日志。
FETCH_LOG = Path(os.environ.get("COURSE_FETCH_LOG")
                 or BASE / "temp/course_pool/fetch_log.json")
FETCH_STALE_HOURS = 48          # 台账超过此时长未刷新 = 看板标「台账过期」

NEW_DAYS = 7            # 首次发现多少天内算「新课」
RESEARCH_GAP_DAYS = 3   # 同一课程两次进搜索词的间隔
DUP_RATIO = 0.72        # 跨源标题判重阈值
STATUSES = ("hunting", "found", "ignore")
PRICE_RX = re.compile(r"￥\s*([0-9]+(?:\.[0-9]+)?)")
STU_RX = re.compile(r"([0-9]+)\s*(?:人报名|人在学|人学过)")
NOISE_RX = re.compile(
    r"(慕课网|源码|视频教程|全套|完整版|无密|高清|更新完|新课|首发|实战课|体系课|"
    r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\))"
)
KEEP_RX = re.compile(r"[0-9a-z\u4e00-\u9fff]+")


def norm_title(text: str) -> str:
    """标题归一化：全角转半角、去噪声词/标点，只留字母数字与汉字。"""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = NOISE_RX.sub(" ", s)
    s = "".join(KEEP_RX.findall(s))
    return s


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def today() -> str:
    return time.strftime("%Y-%m-%d")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def days_since(date_str: str) -> float:
    try:
        t = time.mktime(time.strptime((date_str or "")[:10], "%Y-%m-%d"))
    except (ValueError, TypeError):
        return 9e9
    return (time.time() - t) / 86400.0


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_pool() -> dict:
    data = load_json(POOL_PATH, None)
    if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
        return {"version": 1, "updated_at": now(), "courses": []}
    return data


def load_user() -> dict:
    data = load_json(USER_PATH, None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("courses", {})
    data.setdefault("resources", [])
    data.setdefault("updated_at", now())
    return data


def save_user(user: dict) -> None:
    user["updated_at"] = now()
    save_json(USER_PATH, user)


def apply_user(pool: dict, user: dict) -> dict:
    """把用户数据（星级/优先级/状态/备注）叠加到派生池上，返回新池（不改原对象）。"""
    ucs = user.get("courses") or {}
    out = json.loads(json.dumps(pool))
    res_by_course: dict[str, list] = {}
    for r in user.get("resources") or []:
        res_by_course.setdefault(r.get("course_id", ""), []).append(r)
    for c in out.get("courses", []):
        u = ucs.get(c.get("id", "")) or {}
        c["star"] = int(u.get("star", 0) or 0)
        c["priority"] = int(u.get("priority", 0) or 0)
        c["status"] = u.get("status") or "hunting"
        c["notes"] = u.get("notes") or ""
        c["resources"] = res_by_course.get(c.get("id", ""), [])
        c["is_new"] = days_since(c.get("first_seen", "")) <= NEW_DAYS
    return out


def find_course(pool: dict, course_id: str):
    for c in pool.get("courses", []):
        if c.get("id") == course_id:
            return c
    return None


def dedup_groups(courses: list) -> int:
    """跨源标题判重：并查集给同课打 dup_group；返回组数（>=2 成员的组）。"""
    parent = list(range(len(courses)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    n = len(courses)
    for i in range(n):
        ti = courses[i].get("title", "")
        ni = norm_title(ti)
        if len(ni) < 6:
            continue
        for j in range(i + 1, n):
            if courses[i].get("source") == courses[j].get("source"):
                continue
            nj = norm_title(courses[j].get("title", ""))
            if len(nj) < 6:
                continue
            if difflib.SequenceMatcher(None, ni, nj).ratio() >= DUP_RATIO:
                union(i, j)
    groups: dict[int, list] = {}
    for i in range(n):
        courses[i]["dup_group"] = None
        groups.setdefault(find(i), []).append(i)
    gid = 0
    for root, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        gid += 1
        for i in members:
            courses[i]["dup_group"] = gid
    return gid


def upsert(source: str, records: list, pool: dict | None = None) -> dict:
    """按 (source, source_id) 幂等入库：已存在则刷新价格/热度/标题/链接，不重复新增。"""
    pool = pool if pool is not None else load_pool()
    index = {c.get("id"): c for c in pool.get("courses", [])}
    added = updated = 0
    for rec in records:
        sid = str(rec.get("source_id") or "").strip()
        title = (rec.get("title") or "").strip()
        if not sid or not title:
            continue
        cid = f"{source}:{sid}"
        cur = index.get(cid)
        if cur is None:
            cur = {
                "id": cid, "source": source, "source_id": sid, "title": title,
                "url": rec.get("url") or "", "type": rec.get("type") or "",
                "category": rec.get("category") or "", "price": rec.get("price"),
                "price_orig": rec.get("price_orig"),
                "students": int(rec.get("students") or 0), "level": rec.get("level") or "",
                "tags": sorted(set(rec.get("tags") or [])),
                "first_seen": today(), "last_seen": today(), "last_words_at": None,
                "search_count": 0, "dup_group": None,
            }
            pool.setdefault("courses", []).append(cur)
            index[cid] = cur
            added += 1
        else:
            cur["title"] = title
            if rec.get("url"):
                cur["url"] = rec["url"]
            for k in ("type", "category", "level"):
                if rec.get(k):
                    cur[k] = rec[k]
            if rec.get("price") is not None:
                cur["price"] = rec["price"]
            if rec.get("price_orig") is not None:
                cur["price_orig"] = rec["price_orig"]
            if rec.get("students"):
                cur["students"] = int(rec["students"])
            cur["tags"] = sorted(set(cur.get("tags") or []) | set(rec.get("tags") or []))
            cur["last_seen"] = today()
            updated += 1
    pool["updated_at"] = now()
    groups = dedup_groups(pool.get("courses", []))
    pool["dup_groups"] = groups
    save_json(POOL_PATH, pool)
    return {"added": added, "updated": updated, "total": len(pool.get("courses", [])),
            "dup_groups": groups}


def user_course_entry(user: dict, course_id: str) -> dict:
    return user.setdefault("courses", {}).setdefault(course_id, {})


def set_star(course_id: str, star: int, note: str = "") -> dict:
    user = load_user()
    e = user_course_entry(user, course_id)
    e["star"] = max(0, min(3, int(star)))
    if note:
        e["notes"] = note
    e["updated_at"] = now()
    save_user(user)
    return e


def set_status(course_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status 必须是 {STATUSES} 之一")
    user = load_user()
    e = user_course_entry(user, course_id)
    e["status"] = status
    e["updated_at"] = now()
    save_user(user)
    return e


def attach_resource(course_id: str, pan_url: str, title: str = "", via: str = "") -> dict:
    """登记已找到的资源，并把课程置为 found。"""
    user = load_user()
    rec = {"course_id": course_id, "pan_url": pan_url, "title": title,
           "via": via, "found_at": now()}
    user.setdefault("resources", []).append(rec)
    e = user_course_entry(user, course_id)
    e["status"] = "found"
    e["updated_at"] = now()
    save_user(user)
    return rec


def pick_for_words(merged: dict, limit: int, gap_days: float = RESEARCH_GAP_DAYS) -> list:
    """挑选进入搜索词的课程：未找到、未忽略、且（新课 或 距上次搜索超过 gap 天）。"""
    cands = []
    for c in merged.get("courses", []):
        if c.get("status") in ("found", "ignore"):
            continue
        if c.get("resources"):
            continue
        last = c.get("last_words_at") or ""
        fresh = days_since(c.get("first_seen", "")) <= NEW_DAYS
        if not fresh and last and days_since(last) < gap_days:
            continue
        if not last and not fresh:
            pass
        cands.append(c)
    cands.sort(key=lambda c: (-int(c.get("star") or 0), -int(c.get("students") or 0),
                              c.get("first_seen") or "", c.get("title") or ""))
    return cands[:max(0, int(limit))]


def merge_words_into(target: Path, words: dict, prune_pool_window: bool = True) -> dict:
    """把搜索词合并进 haisou 动态词表：只补新条目，既有条目（含 found/search_count）一律不覆盖。

    ★活跃池词窗口：`origin` 以 `pool:` 开头、且**不在本批词里**的条目会被移出词表。
      池有 148 条，全量常驻会把 haisou 每日预算挤爆（动态词排序后池词吃光 35 次预算），
      故窗口 = 本批词（默认 12 条），池靠逐日轮换覆盖全量。
      两类条目**永不**被剪枝：非池词（如全能课程词，没有 origin=pool: 标记）与 prune_pool_window=False。

    返回 {target, added, kept, removed, total, found_back}：found_back=被剪且已 found 的池词标题，
    供 emit_words 回写池状态（搜到过的词不再重复投喂）。
    """
    target = Path(target)
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:                                   # noqa: BLE001 - 坏文件绝不静默丢弃
            raise RuntimeError(f"target 不是合法 JSON，拒绝覆盖：{target}")
        if not isinstance(existing, dict):
            raise RuntimeError(f"target 结构不是 dict，拒绝覆盖：{target}")
    else:
        existing = {}
    added, kept, removed, found_back = [], 0, [], []
    if prune_pool_window:
        for title, info in list(existing.items()):
            if title in words:
                continue
            origin = str(info.get("origin") or "") if isinstance(info, dict) else ""
            if not origin.startswith("pool:"):
                continue                                    # 非池词永不剪枝
            if isinstance(info, dict) and info.get("found"):
                found_back.append(title)
            del existing[title]
            removed.append(title)
    for title, info in words.items():
        if title in existing:
            kept += 1
            continue
        existing[title] = info
        added.append(title)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return {"target": str(target), "added": len(added), "kept": kept, "removed": removed,
            "total": len(existing), "found_back": found_back}


def emit_words(limit: int = 12, out: Path | None = None,
               merge_into: Path | None = MERGE_INTO) -> dict:
    """产出 haisou 动态词文件（quangneng_keywords.json 兼容 schema）。

    merge_into 非 None 时，同时把同一批词合并进海搜真正读取的动态词表；
    传 None（`--no-merge`）则只写 pool_keywords.json，便于负控与调试。
    """
    out = Path(out) if out else WORDS_OUT
    pool = load_pool()
    merged = apply_user(pool, load_user())
    picked = pick_for_words(merged, limit)
    words = {}
    for c in picked:
        words[c["title"]] = {
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "found": False,
            "source_url": c.get("url") or "",
            "published_at": (c.get("first_seen") or today()) + "T00:00:00+08:00",
            "found_at": None,
            "search_count": 0,          # 表侧计数从 0 起（池侧计数另存池文件，两处不混用）
            "origin": f"pool:{c.get('id')}",
        }
    save_json(out, words)
    stamp = now()
    for c in pool.get("courses", []):
        if c.get("title") in words:
            c["last_words_at"] = stamp
            c["search_count"] = int(c.get("search_count") or 0) + 1
    info = {"out": str(out), "count": len(words)}
    if merge_into is not None:
        info["merge"] = merge_words_into(Path(merge_into), words)
        # 被剪枝且已 found 的池词回写池状态：搜到过的词不再投喂后续窗口
        back = info["merge"].get("found_back") or []
        if back:
            hit = []
            for c in pool.get("courses", []):
                if c.get("title") in back:
                    c["status"] = c.get("status") or "found"
                    c["found_at"] = c.get("found_at") or stamp
                    hit.append(c["title"])
            info["found_back"] = hit
    save_json(POOL_PATH, pool)
    return info


JINA_PREFIX = "https://r.jina.ai/"
IMOOC_DIRS = ("AI", "nt", "cb")     # 方向：AI人工智能 / 前沿技术 / 云计算&大数据

IMOOC_ITEM_RE = re.compile(
    r"^\*\s+\[(?P<title>.+?)\s+(?:(?P<level>零基础|初阶|进阶|专业|高级)\s*·\s*)?"
    r"(?P<students>\d+)人报名\s*(?P<price>[^\]]*?)\]\((?P<url>https://coding\.imooc\.com/class/(?P<sid>\d+)\.html)\)"
)
PAGE_RE = re.compile(r"[?&]page=(\d+)")
PRICE_RE = re.compile(r"￥\s*(\d+(?:\.\d+)?)")


def http_get(url: str, timeout: int = 60, retries: int = 2) -> str:
    last = ""
    for _ in range(max(1, retries)):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "X-Return-Format": "markdown",
            "Accept": "text/plain,*/*",
        })
        try:
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except Exception as exc:      # noqa: BLE001 - 网络层任何异常都退避重试
            last = repr(exc)
            time.sleep(2)
    raise RuntimeError(f"fetch failed: {url} :: {last}")


def parse_imooc_page(text: str) -> list:
    """从 r.jina.ai 渲染的 markdown 里抽实战课条目。"""
    out = []
    for line in text.split("\n"):
        m = IMOOC_ITEM_RE.match(line.strip())
        if not m:
            continue
        prices = PRICE_RE.findall(m.group("price") or "")
        cur = float(prices[-1]) if prices else None
        orig = None
        if len(prices) >= 2:
            cur, orig = float(prices[0]), float(prices[-1])
        out.append({
            "source_id": m.group("sid"),
            "title": m.group("title").strip(),
            "url": m.group("url"),
            "level": m.group("level") or "",
            "students": int(m.group("students")),
            "price": cur,
            "price_orig": orig,
            "type": "实战课",
        })
    return out


# ---------------------------------------------------------------- 分类归类口径
# 统一分类口径：任何源（慕课三类/全能/后续新增）都走这一张表，
# 站点自报分类优先（parse_*_list 里带 category 就用），否则按标题+元信息关键词兜底。
CATEGORY_RULES = (
    ("AI/大模型", ("ai", "人工智能", "aigc", "大模型", "llm", "langchain", "agent",
                   "智能体", "rag", "mcp", "机器学习", "深度学习", "神经网络",
                   "nlp", "prompt", "midjourney", "stable diffusion", "sora", "copilot")),
    ("数据分析", ("数据分析", "数据挖掘", "大数据", "spark", "hadoop", "flink", "hive",
                  "clickhouse", "bi", "可视化", "sql", "pandas", "数仓", "指标体系")),
    ("前端/客户端", ("html", "css", "javascript", "js", "typescript", "vue", "react",
                     "node", "webpack", "vite", "小程序", "前端", "uniapp", "flutter",
                     "android", "ios", "鸿蒙", "harmonyos", "electron")),
    ("后端/架构", ("java", "python", "golang", "go语言", "c++", "rust", "php", "后端",
                   "架构", "微服务", "spring", "django", "mysql", "redis", "kafka",
                   "rocketmq", "linux", "运维", "docker", "k8s", "kubernetes", "nginx")),
    ("测试/工程", ("测试", "自动化测试", "devops", "ci/cd", "git", "重构", "性能优化",
                   "算法", "数据结构", "leetcode", "设计模式")),
    ("产品/运营", ("产品经理", "运营", "增长", "电商", "营销", "新媒体", "短视频",
                   "私域", "创业", "副业", "自媒体", "一人公司", "出海", "变现")),
    ("设计/办公", ("设计", "ui", "ux", "ps", "ppt", "excel", "word", "office",
                   "剪辑", "摄影", "英语", "职场", "办公")),
    ("安全/网络", ("网络安全", "渗透", "漏洞", "逆向", "加固", "等保", "安全")),
)


def classify_category(*texts) -> str:
    """按关键词命中数归类（纯本地、确定性、无网络）。无命中返回「未分类」。"""
    s2 = " ".join(str(t or "") for t in texts).lower()
    best, best_n = "", 0
    for name, kws in CATEGORY_RULES:
        n = sum(1 for k in kws if k in s2)
        if n > best_n:
            best, best_n = name, n
    return best or "未分类"


# ------------------------------------------------ 慕课任意页（首页/体系课/免费课）
# 一站三内链：实战课 /class/N、体系课 /sale/<slug>、免费课 /learn/N
# 页面反爬，走 r.jina.ai 渲染出的 markdown（首页 53KB、体系课列表含 35 门）。
PRICE2_RX = re.compile(r"[\u00a5\uffe5]\s*([0-9]+(?:\.[0-9]+)?)")
_IMOOC_ANY_LINK = re.compile(
    r"\[([^\]]{2,140})\]\((https://(?:"
    r"coding\.imooc\.com/class/(?P<cid>\d+)\.html"
    r"|class\.imooc\.com/sale/(?P<slug>[A-Za-z0-9_\-]+)"
    r"|www\.imooc\.com/learn/(?P<lid>\d+)"
    r"))(?:\?[^)]*)?\)")
_LEVEL_RX = re.compile(r"(零基础|初阶|进阶|中阶|高阶|入门|初级|中级|高级|专业)")


_TITLE_TRIM_RX = re.compile(
    r"\s+(?=[0-9]+人报名|实战课|实战|体系课|体系|免费课|免费|零基础|初阶|初级|入门|进阶|"
    r"中级|中阶|高级|高阶|专业|[0-9]+月特惠)"
)
_NOISE_TITLE_RX = re.compile(
    r"^(?:优惠倒计时|立即购买|免费试听|了解详情|查看更多|更多|查看详情|试看|立即学习|"
    r"马上抢|立即抢购|课程咨询|咨询客服|限时优惠|开学季|抢购|领取)"
)


def parse_imooc_md_any(text, source_hint=""):
    """慕课任意页 markdown -> 课程记录（实战课/体系课/免费课三态统一）。

    同一 slug 在页面里常有多个锚点（优惠倒计时/立即购买/真描述行），先滤营销锚点，
    再按 slug 取最长标题当课程名。元信息取所选中锚点后 300 字符窗口：
      - 免费课：`零基础 · 1123人报名 免费`
      - 体系课：`332 80 小时 108 优惠价 ¥ 2399 ¥ 2999`（首数=学员、中数=课时）
    两个价时取首个为现价、末个为原价，与 parse_imooc_page 同口径。
    """
    t = text or ""
    groups, order = {}, []
    for m in _IMOOC_ANY_LINK.finditer(t):
        title = _clean(m.group(1)).strip()
        if m.group("cid"):
            sid, ctype = m.group("cid"), "实战课"
            url = "https://coding.imooc.com/class/%s.html" % m.group("cid")
        elif m.group("slug"):
            sid, ctype = "sale-" + m.group("slug"), "体系课"
            url = "https://class.imooc.com/sale/%s" % m.group("slug")
        else:
            sid, ctype = "learn-" + m.group("lid"), "免费课"
            url = "https://www.imooc.com/learn/%s" % m.group("lid")
        if (not title or len(title) < 5 or title.endswith("：") or title.endswith(":")
                or _NOISE_TITLE_RX.match(title)):
            continue
        g = groups.get(sid)
        if g is None:
            g = {"sid": sid, "type": ctype, "url": url, "cands": []}
            groups[sid] = g
            order.append(sid)
        g["cands"].append((len(title), title, m.end()))
    out = []
    for sid in order:
        g = groups[sid]
        _n, title, endpos = max(g["cands"], key=lambda x: x[0])
        _tm = _TITLE_TRIM_RX.search(title, 5)
        if _tm:
            title = title[:_tm.start()].strip()
        tail = t[endpos:endpos + 300].replace("\n", " ")
        hm = re.search(r"([0-9]+)\s*小时", tail)
        hours = int(hm.group(1)) if hm else 0
        s3 = re.search(r"([0-9]+)\s+([0-9]+)\s*小时\s+([0-9]+)", tail)
        sm = STU_RX.search(tail) or re.search(r"([0-9]+)\s*人(?:报名|学习|学过)", tail)
        if sm:
            students = int(sm.group(1))
        elif s3:
            students = int(s3.group(1))
        else:
            students = 0
        prices = PRICE2_RX.findall(tail)
        price = float(prices[0]) if prices else None
        price_orig = float(prices[-1]) if len(prices) >= 2 else None
        if price is None and "免费" in tail[:90]:
            price = 0.0
        lm = _LEVEL_RX.search(tail)
        level = lm.group(1) if lm else ""
        cat = classify_category(title, tail)
        out.append({
            "source_id": sid, "title": title, "url": g["url"], "type": g["type"],
            "category": cat, "level": level, "students": students, "hours": hours,
            "price": price, "price_orig": price_orig,
            "tags": [w for w in (g["type"], cat) if w],
        })
    return out


def imooc_page_url(direction: str, page: int) -> str:
    return f"https://coding.imooc.com/?c={direction}&sort=0&unlearn=0&page={page}"


def collect_imooc(dirs=IMOOC_DIRS, max_pages: int = 5, fetch=http_get) -> dict:
    """按方向抓实战课（经 r.jina.ai 跨网渲染），返回 {records, per_dir}。"""
    recs, per_dir = [], {}
    for d in dirs:
        got, pages = [], 0
        for page in range(1, max_pages + 1):
            try:
                text = fetch(JINA_PREFIX + imooc_page_url(d, page))
            except RuntimeError:
                break
            items = parse_imooc_page(text)
            got.extend(items)
            pages += 1
            known = {int(x) for x in PAGE_RE.findall(text)}
            if not known or max(known) <= page:
                break
            time.sleep(2)
        seen = set()
        uniq = []
        for it in got:
            if it["source_id"] in seen:
                continue
            seen.add(it["source_id"])
            uniq.append(it)
        per_dir[d] = {"pages": pages, "items": len(uniq)}
        recs.extend(uniq)
        time.sleep(2)
    return {"records": recs, "per_dir": per_dir}


def stats(merged: dict) -> dict:
    courses = merged.get("courses", [])
    by_status, by_source, by_star = {}, {}, {}
    for c in courses:
        by_status[c.get("status", "hunting")] = by_status.get(c.get("status", "hunting"), 0) + 1
        by_source[c.get("source", "?")] = by_source.get(c.get("source", "?"), 0) + 1
        by_star[str(int(c.get("star") or 0))] = by_star.get(str(int(c.get("star") or 0)), 0) + 1
    res = [r for c in courses for r in (c.get("resources") or [])]
    return {
        "total": len(courses),
        "by_status": by_status,
        "by_source": by_source,
        "by_star": by_star,
        "new": sum(1 for c in courses if c.get("is_new")),
        "with_resource": len({r.get("course_id") for r in res}),
        "dup_groups": int(merged.get("dup_groups") or 0),
        "updated_at": merged.get("updated_at", ""),
    }


def render_html(merged: dict, st: dict | None = None, cands: dict | None = None,
                flock: dict | None = None, dbst: dict | None = None) -> str:
    st = st or stats(merged)
    rows = []
    for c in sorted(merged.get("courses", []),
                    key=lambda c: (-int(c.get("star") or 0), -int(c.get("students") or 0))):
        res = c.get("resources") or []
        tag = {"hunting": "🔍", "found": "✅", "ignore": "🚫"}.get(c.get("status"), "🔍")
        star = "★" * int(c.get("star") or 0)
        links = " ".join(
            f'<a href="{_esc(r.get("pan_url"))}" target="_blank">资源</a>' for r in res)
        price = "" if c.get("price") is None else f'￥{c["price"]:g}'
        rows.append(
            "<tr>"
            f'<td>{tag}{star}</td>'
            f'<td><a href="{_esc(c.get("url"))}" target="_blank">{_esc(c.get("title"))}</a>'
            f'{" <b class=n>NEW</b>" if c.get("is_new") else ""}'
            f'{" <span class=d>dup#" + str(c.get("dup_group")) + "</span>" if c.get("dup_group") else ""}</td>'
            f'<td>{_esc(c.get("source"))}/{_esc(c.get("type"))}</td>'
            f'<td>{_esc(c.get("level"))}</td><td>{c.get("students", 0)}</td><td>{price}</td>'
            f'<td>{links}</td></tr>')
    chips = " ".join(f"<span class=chip>{k}:{v}</span>" for k, v in sorted(st["by_status"].items()))
    fetch_html, fetch_chip = "", ""
    if flock:
        fts = flock.get("ts")
        age_h = None
        try:
            age_h = (time.time() - float(fts)) / 3600.0
        except (TypeError, ValueError):
            age_h = None
        frows, n_stale_src = [], 0
        for sid, v in sorted((flock.get("sources") or {}).items()):
            modes = {}
            for u in (v.get("per_url") or []):
                modes[u.get("mode") or "?"] = modes.get(u.get("mode") or "?", 0) + 1
            if v.get("stale"):
                n_stale_src += 1
            up = v.get("upsert") or {}
            ups = (f"+{up.get('added', 0)}/{up.get('updated', 0)}"
                   if up else "-")
            flag = ("<b class=n>缓存回退</b>" if v.get("stale") else "OK")
            frows.append(
                "<tr>"
                f'<td>{_esc(sid)} {_esc(v.get("name"))}</td>'
                f'<td>{_esc(",".join(v.get("channel") or []))}</td>'
                f'<td>{v.get("records", 0)}</td><td>{ups}</td>'
                f'<td>{_esc(" ".join(f"{k}×{n}" for k, n in sorted(modes.items())))}</td>'
                f'<td>{flag}</td></tr>')
        if age_h is None:
            age_txt = "?"
        elif age_h < 24:
            age_txt = f"{age_h:.1f}h 前"
        else:
            age_txt = f"{age_h / 24:.1f}d 前"
        age_flag = ("<b class=n>台账过期</b>" if (age_h or 0) > FETCH_STALE_HOURS else "")
        fetch_chip = (f"<span class=chip>采集 {age_txt}</span>"
                      + (f"<span class=chip>缓存回退 {n_stale_src} 源</span>"
                         if n_stale_src else ""))
        fetch_html = (
            f"<h2>采集面 <span class=chip>抓取 {_esc(flock.get('ts_str'))}"
            f"（{age_txt}）{age_flag}</span>"
            f"<span class=chip>本批入库 {flock.get('fetched', 0)} 条</span></h2>\n"
            "<p class=cav>「通道分布」= 本轮每个 URL 实际生效的抓取通道；"
            "<b>缓存回退</b>= 站点不可达时用 last-good 缓存兜底（<code>mode=cache</code>），"
            "数据仍可用但非当日新鲜。</p>\n"
            "<table id=ft><thead><tr><th>源</th><th>通道</th><th>记录</th>"
            "<th>新增/更新</th><th>通道分布</th><th>状态</th></tr></thead>\n"
            f"<tbody>{''.join(frows)}</tbody></table>\n")
    cand_html = ""
    if cands is not None:
        allm = cands.get("all_matches") or []
        shown = allm[:CAND_BOARD_LIMIT]
        crows = []
        for m in shown:
            try:
                sc = f"{float(m.get('score') or 0):.2f}"
            except (TypeError, ValueError):
                sc = "?"
            pwd = f' 提取码 {_esc(m.get("pwd"))}' if m.get("pwd") else ""
            crows.append(
                "<tr>"
                f'<td>{_esc(m.get("course_title"))}</td>'
                f'<td><a href="{_esc(m.get("url"))}" target="_blank">{_esc(m.get("hit_title"))}</a>{pwd}</td>'
                f'<td>{sc}</td><td>{_esc(m.get("src"))}</td><td>{_esc(m.get("kw"))}</td></tr>')
        more = "" if len(allm) <= len(shown) else f"（仅显示前 {len(shown)} 条）"
        cand_html = (
            f"<h2>网盘命中候选 <span class=chip>共 {len(allm)}</span>"
            f"<span class=chip>快照 {_esc(cands.get('ts'))}</span>"
            f"<span class=chip>min_score {cands.get('min_score')}</span></h2>\n"
            "<p class=cav>此表由 <b>match 干跑</b>生成，<b>尚未挂载</b>到课程资源；"
            "确认后执行 <code>python3 tools/course_pool.py match --apply</code> 才会写入。"
            f"{more}</p>\n"
            "<table id=ct><thead><tr><th>课程</th><th>命中</th><th>分</th><th>源</th><th>词</th></tr></thead>\n"
            f"<tbody>{''.join(crows)}</tbody></table>\n")
    db_html = ""
    if dbst is not None:
        if not dbst.get("exists"):
            db_html = ("<h2>统一落库 <span class=chip>库未建</span></h2>\n"
                       "<p class=cav>执行 <code>python3 tools/course_pool.py db</code> "
                       "由 pool.json / user_state.json / 已见库 / 盘搜 raw / 候选快照重建；"
                       "fetch 与 match 完成后会自动重建。</p>\n")
        else:
            chips_db = " ".join(
                f"<span class=chip>{k} {dbst.get(k, 0)}</span>"
                for k in ("courses", "resources", "courses_with_res", "haisou_seen",
                          "pan_hits", "pan_seen", "keywords", "sources",
                          "candidates", "fetch_runs"))
            top = " ".join(f'<code>{_esc(x.get("course_id"))}×{x.get("resources")}</code>'
                           for x in (dbst.get("top_courses") or []))
            pan_line = " ".join(f'<code>{_esc(x.get("pan"))}×{x.get("n")}</code>'
                                for x in (dbst.get("pan_by_platform") or []))
            kw_line = " ".join(f'<code>{_esc(x.get("kw"))}×{x.get("n")}</code>'
                               for x in (dbst.get("pan_top_kw") or []))
            pool_line = " ".join(f'<code>{_esc(x.get("pool"))}×{x.get("n")}</code>'
                                 for x in (dbst.get("kw_pool") or []))
            db_html = (
                f"<h2>统一落库 <span class=chip>platform.db</span>{chips_db}</h2>\n"
                "<p class=cav>所有数据面落到一张 SQLite：课程池 / 已挂资源 / 海搜已见 / "
                "盘搜原始命中 / 搜索词池 / 采集源注册 / 匹配候选 / 采集运行台账；"
                "库由工具幂等重建，源文件是唯一事实源。"
                f"资源最多的课程：{top or '-'}；构建于 {_esc(dbst.get('built_at'))}</p>\n"
                f"<p class=cav>盘搜命中按网盘：{pan_line or '-'}<br>"
                f"命中最多的搜索词：{kw_line or '-'}<br>"
                f"搜索词池：{pool_line or '-'}</p>\n")
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>AI 课程资源台</title>
<style>
body{{font:14px/1.5 -apple-system,"PingFang SC",sans-serif;background:#111;color:#ddd;margin:24px}}
h1{{font-size:20px}} .chip{{background:#222;border:1px solid #333;border-radius:12px;padding:2px 10px;margin-right:6px}}
table{{border-collapse:collapse;width:100%;margin-top:12px}} td,th{{border-bottom:1px solid #262626;padding:6px 8px;text-align:left}}
a{{color:#6cf;text-decoration:none}} .n{{color:#e33;font-size:11px}} .d{{color:#a83;font-size:11px}}
.cav{{color:#9a8;font-size:12px;margin:6px 0 0}}
code{{background:#222;border:1px solid #333;border-radius:4px;padding:1px 5px;font-size:12px}}
input{{background:#1b1b1b;color:#ddd;border:1px solid #333;border-radius:6px;padding:6px 10px;width:280px}}
</style>
<h1>AI 课程资源台 <span class=chip>共 {st['total']}</span>{chips}{fetch_chip}
<span class=chip>新课 {st['new']}</span><span class=chip>已有资源 {st['with_resource']}</span>
<span class=chip>跨源重复组 {st['dup_groups']}</span></h1>
<input id=q placeholder="过滤：标题/来源/难度" oninput="f()">
<table id=t><thead><tr><th>标记</th><th>课程</th><th>来源</th><th>难度</th><th>报名</th><th>价格</th><th>资源</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{fetch_html}{cand_html}{db_html}<p style="color:#666">更新：{_esc(st.get('updated_at'))}（本页由 tools/course_pool.py 生成，只读视图）</p>
<script>function f(){{var v=q.value.toLowerCase();
for(var tb of document.querySelectorAll('table tbody')) for(var r of tb.rows)
  r.style.display = r.innerText.toLowerCase().includes(v)?'':'none';}}</script>
</html>"""


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_upsert_file(source: str, path: str) -> dict:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    records = json.loads(raw)
    if isinstance(records, dict):
        records = records.get("records") or records.get("items") or []
    return upsert(source, records)


def cmd_list(merged: dict, kw: str = "", status: str = "", source: str = "",
             limit: int = 50) -> list:
    out = []
    for c in merged.get("courses", []):
        if status and c.get("status") != status:
            continue
        if source and c.get("source") != source:
            continue
        if kw and kw.lower() not in (c.get("title") or "").lower():
            continue
        out.append(c)
    out.sort(key=lambda c: (-int(c.get("star") or 0), -int(c.get("students") or 0)))
    return out[:limit]


def title_score(a: str, b: str) -> float:
    """标题相似度（0–1，下界口径）：归一化后按「短侧 bigram 被覆盖率」。

    只用 norm_title（已剥噪声词/标点/括号）+ 额外剥「网盘/资源/下载/课程/教程/合集」类
    营销词。两侧归一名长度 <6 时一律判 0（负控：`C++`/`AI` 这类短标题不得乱配）。
    """
    na = MATCH_EXTRA_RX.sub("", norm_title(a))
    nb = MATCH_EXTRA_RX.sub("", norm_title(b))
    if len(na) < 6 or len(nb) < 6:
        return 0.0
    A = {na[i:i + 2] for i in range(len(na) - 1)}
    B = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if not A or not B:
        return 0.0
    return len(A & B) / float(min(len(A), len(B)))


def load_hits(include_pan: bool = True, haisou_path=None, pan_glob: str = "") -> list:
    """读「搜索命中」语料（只读，从不写盘）：

    * haisou：birdie_monitor/haisou_seen_items.json（key→{title,link,share_pwd,...}）
    * pan：temp/pan_search/raw_*.json（kw→[{title,url,code,...}]，按文件名倒序取新）

    坏文件 must-fail：命中库不可读时抛错，绝不静默当空（否则会得出「今天无命中」的假结论）。
    """
    hits = []
    hp = Path(haisou_path) if haisou_path else HAISOU_SEEN
    if hp.exists():
        try:
            d = json.loads(Path(hp).read_text(encoding="utf-8"))
        except Exception as e:                                   # noqa: BLE001
            raise RuntimeError(f"命中库不可读，拒绝静默当空：{hp}: {e}")
        if not isinstance(d, dict):
            raise RuntimeError(f"命中库结构不是 dict：{hp}")
        for _k, v in d.items():
            if isinstance(v, dict) and v.get("title") and v.get("link"):
                hits.append({"src": "haisou", "title": v["title"], "url": v["link"],
                             "pwd": v.get("share_pwd") or "", "kw": v.get("keyword") or "",
                             "at": v.get("seen_at") or ""})
    elif haisou_path:
        raise RuntimeError(f"显式指定的命中库不存在：{hp}")
    if include_pan:
        files = sorted(BASE.glob(pan_glob or PAN_RAW_GLOB), reverse=True)
        seen_url = set()
        for fp in files:
            try:
                d = json.loads(Path(fp).read_text(encoding="utf-8"))
            except Exception:                                     # noqa: BLE001 - 单份坏 raw 不拖垮全量
                continue
            if not isinstance(d, dict):
                continue
            for kw, items in d.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    u, t = (it.get("url") or "").strip(), (it.get("title") or "").strip()
                    if not u or not t or u in seen_url:
                        continue
                    seen_url.add(u)
                    hits.append({"src": "pan:" + str(it.get("pan") or "?"), "title": t,
                                 "url": u, "pwd": it.get("code") or "",
                                 "kw": it.get("kw") or kw, "at": it.get("datetime") or ""})
    return hits


def load_candidates(path: Path, explicit: bool = False):
    """读 match 干跑快照（只读，供看板上「网盘命中候选」面板）。

    返回 (payload|None, warn|None)。文件缺失/损坏时默认返回 (None, 原因) 静默降级；
    explicit=True（用户显式指定 --candidates）时直接抛错，避免把坏路径当"没有候选"。
    """
    p = Path(path)
    if not p.exists():
        if explicit:
            raise FileNotFoundError(f"候选快照不存在: {p}")
        return None, f"候选快照不存在: {p}"
    try:
        data = load_json(p, None)
    except Exception as e:                      # noqa: BLE001
        if explicit:
            raise ValueError(f"候选快照无法解析: {p}: {e}")
        return None, f"候选快照无法解析: {p}: {e}"
    if not isinstance(data, dict) or "all_matches" not in data:
        if explicit:
            raise ValueError(f"候选快照格式不符(schema=course_match_candidates/v1): {p}")
        return None, f"候选快照格式不符: {p}"
    return data, None


def cmd_match(min_score: float = MATCH_MIN_SCORE, max_per_course: int = MATCH_MAX_PER_COURSE,
              apply: bool = False, hits_path: str = "", include_pan: bool = True,
              limit: int = 20, include_found: bool = False, json_out: str = "") -> dict:
    """把搜索命中按标题相似度挂到课程上（默认 dry-run，只预览不写盘）。

    策略：每个课程取分数最高的前 max_per_course 条命中；同一 (course_id, pan_url)
    已挂过即跳过（幂等）；默认跳过「已有资源」的课程（只补空缺），--include-found 可放开。

    json_out：把**全量**候选写成快照 JSON（看板 render 消费它上「网盘命中候选」面板）。
    写快照 ≠ 挂载：只有 apply=True 才碰 user_state.json 的 resources。
    """
    hits = load_hits(include_pan=include_pan, haisou_path=hits_path or None)
    merged = apply_user(load_pool(), load_user())
    user = load_user()
    have = {(r.get("course_id"), (r.get("pan_url") or "").strip())
            for r in user.get("resources", [])}
    cands, skipped, applied = [], 0, 0
    for c in merged.get("courses", []):
        cid = c.get("id")
        if not cid or c.get("status") == "ignore":
            continue
        if c.get("resources") and not include_found:
            continue
        rows = []
        for h in hits:
            sc = title_score(c.get("title") or "", h.get("title") or "")
            if sc >= min_score:
                rows.append((sc, h))
        rows.sort(key=lambda x: (-x[0], x[1].get("title") or ""))
        used_url = set()
        for sc, h in rows[:max(1, int(max_per_course))]:
            url = (h.get("url") or "").strip()
            if not url or url in used_url:
                continue
            used_url.add(url)
            key = (cid, url)
            if key in have:
                skipped += 1
                continue
            rec = {"course_id": cid, "course_title": c.get("title") or "",
                   "hit_title": h.get("title") or "", "score": round(sc, 3),
                   "src": h.get("src") or "", "url": url, "pwd": h.get("pwd") or "",
                   "kw": h.get("kw") or "", "hits_at": h.get("at") or ""}
            if apply:
                attach_resource(cid, url, title=h.get("title") or "",
                                via="auto:" + str(h.get("src") or ""))
                have.add(key)
                applied += 1
            else:
                rec["dry_run"] = True
            cands.append(rec)
    payload = {"hits": len(hits), "courses": len(merged.get("courses", [])),
               "min_score": min_score, "apply": bool(apply),
               "applied": applied, "skipped_existing": skipped,
               "candidates": len(cands), "matches": cands[:max(0, int(limit))]}
    if json_out:
        snap = dict(payload)
        snap.update({"ts": now(), "schema": "course_match_candidates/v1",
                     "all_matches": cands})
        p = Path(json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        save_json(p, snap)
        payload["json_out"] = str(p)
    if apply and "error" not in payload:
        try:                       # 挂载后刷新落库
            payload["db"] = db_build()
        except Exception as e:
            payload["db_error"] = str(e)
    return payload


def selftest() -> int:
    import tempfile
    ok = 0
    failed = []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            failed.append(name)

    def _db_fp(path):
        """生产库指纹（sha256）；自测结束用它做「未写生产库」负控（R554）。"""
        p = Path(path)
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

    with tempfile.TemporaryDirectory() as td:
        old = (POOL_PATH, USER_PATH, WORDS_OUT)
        globals()["POOL_PATH"] = Path(td) / "pool.json"
        globals()["USER_PATH"] = Path(td) / "user.json"
        globals()["WORDS_OUT"] = Path(td) / "words.json"
        # R554：默认 DB_PATH 也必须落沙箱——selftest 内 cmd_match(apply=True) 会走
        # db_build() 的默认参数真写生产库（曾把 215 门课覆盖成 2 条夹具）。
        saved_db = DB_PATH
        prod_db = Path(DB_PATH)
        prod_db_before = _db_fp(prod_db)
        globals()["DB_PATH"] = Path(td) / "platform_default.db"
        try:
            sample = (
                "*   [导航](https://www.imooc.com/course/list)\n"
                "*   [Agent 工程化+ AI 编程深度实战营 进阶 · 85人报名 _10月特惠_￥1299￥1480.00 加购物车]"
                "(https://coding.imooc.com/class/1013.html)\n"
                "*   [从0到1 转型 Agent应用开发工程师 零基础 · 29人报名 ￥499￥599.00 加购物车]"
                "(https://coding.imooc.com/class/1040.html)\n"
                "Copyright 2026 imooc.com\n")
            items = parse_imooc_page(sample)
            check("T1 解析条数=2", len(items) == 2)
            check("T2 价格取现价/原价", items[0]["price"] == 1299.0 and items[0]["price_orig"] == 1480.0)
            check("T3 难度/报名/ID", items[1]["level"] == "零基础" and items[1]["students"] == 29
                  and items[1]["source_id"] == "1040")
            check("T4 导航与非课程行不计入", all("imooc.com/course/list" not in x["url"] for x in items))
            check("T5 单页 URL 构造", "c=AI" in imooc_page_url("AI", 3) and "page=3" in imooc_page_url("AI", 3))

            r1 = upsert("imooc", items)
            check("T6 首次入库 added=2", r1["added"] == 2 and r1["total"] == 2)
            r2 = upsert("imooc", items)
            check("T7 幂等复跑 added=0/updated=2", r2["added"] == 0 and r2["updated"] == 2)
            check("T8 空记录跳过（负控）", upsert("imooc", [{"title": ""}, {"source_id": ""}])["added"] == 0)

            upsert("pan", [{"source_id": "p1", "title": "Agent 工程化 + AI 编程深度实战营 (2026)",
                            "students": 3}])
            pool = load_pool()
            check("T9 跨源相似标题成组", int(pool.get("dup_groups") or 0) == 1)
            up2 = upsert("pan", [{"source_id": "p9", "title": "Agent 工程化 + AI 编程深度实战营 (2027)",
                                  "students": 1}])
            check("T10 同源相似不判重（负控）", up2["dup_groups"] == 1)

            user = load_user()
            e = user_course_entry(user, "imooc:1013")
            e["star"] = 3
            e["status"] = "hunting"
            save_user(user)
            merged = apply_user(load_pool(), load_user())
            c = [x for x in merged["courses"] if x["id"] == "imooc:1013"][0]
            check("T11 用户星级叠加", c["star"] == 3)
            check("T12 叠加不改派生池", all("star" not in x for x in load_pool().get("courses", [])))

            set_status("imooc:1040", "found")
            attach_resource("imooc:1013", "https://pan.example/s/abc", via="haisou")
            merged = apply_user(load_pool(), load_user())
            picked = pick_for_words(merged, 10)
            ids = [x["id"] for x in picked]
            check("T13 found 不进搜索词", "imooc:1040" not in ids)
            check("T14 已有资源不进搜索词", "imooc:1013" not in ids)
            check("T15 剩余课程仍入选", "pan:p1" in ids)
            try:
                set_status("imooc:1013", "nonsense")
                check("T16 非法状态须报错（负控）", False)
            except ValueError:
                check("T16 非法状态须报错（负控）", True)

            out = emit_words(limit=5)
            w = json.loads(Path(out["out"]).read_text(encoding="utf-8"))
            need = {"added_at", "found", "source_url", "published_at", "search_count", "origin"}
            check("T17 词表 schema 兼容 haisou", w and all(need <= set(v) for v in w.values()))
            check("T18 产出条数=f 上限", len(w) == min(5, len(picked)))
            check("T19 origin 回指课程池", all(v["origin"].startswith("pool:") for v in w.values()))
            merged2 = apply_user(load_pool(), load_user())
            st = stats(merged2)
            check("T20 统计口径", st["total"] == 4 and st["with_resource"] == 1
                  and st["by_source"]["imooc"] == 2 and st["dup_groups"] == 1)
            html = render_html(merged2, st)
            check("T21 看板含标题与计数", "Agent 工程化" in html and f"共 {st['total']}" in html)
            check("T22 看板无模板残留", "{{" not in html and "}}" not in html)
            bad = render_html({"courses": [{"id": "x:1", "title": "<script>alert(1)</script>",
                                            "source": "x", "status": "hunting"}], "dup_groups": 0})
            check("T23 模板注入转义（负控）", "<script>alert(1)</script>" not in bad)
            check("T24 render 只读不改池", load_pool()["updated_at"] == pool["updated_at"])
            # T25 静态依赖自检：http_get 用到的 urllib.request 必须已导入（2026-10-06 真机
            #   抓取曾因缺 import 直接 NameError，而网络路径不进自测 ⇒ 这里做符号级负控）
            check("T25 urllib.request 已导入（负控）",
                  callable(getattr(getattr(urllib, "request", None), "Request", None)))
            # T26–T30 池词并入 haisou 动态词表：只补新增，既有条目一律不覆盖
            haisou_like = Path(td) / "quangneng_keywords.json"
            haisou_like.write_text(json.dumps({
                "既有课程A": {"added_at": "2026-09-05 16:52:32", "found": True,
                              "source_url": "https://quangneng.com/1/", "published_at": None,
                              "found_at": "2026-09-06 10:00:00", "search_count": 9},
            }, ensure_ascii=False), encoding="utf-8")
            before = json.loads(haisou_like.read_text(encoding="utf-8"))
            out2 = emit_words(limit=5, merge_into=haisou_like)
            after = json.loads(haisou_like.read_text(encoding="utf-8"))
            check("T26 合并新增条数=词表条数", out2["merge"]["added"] == out2["count"]
                  and out2["merge"]["target"] == str(haisou_like))
            check("T27 合并后总数=既有+新增", out2["merge"]["total"] == 1 + out2["count"])
            check("T28 既有条目不覆盖（负控）", after["既有课程A"] == before["既有课程A"]
                  and after["既有课程A"]["search_count"] == 9 and after["既有课程A"]["found"] is True)
            out3 = emit_words(limit=5, merge_into=haisou_like)
            check("T29 幂等复跑 added=0/kept=词表条数", out3["merge"]["added"] == 0
                  and out3["merge"]["kept"] == out3["count"]
                  and out3["merge"]["total"] == out2["merge"]["total"])
            haisou_like.write_text("{not json", encoding="utf-8")
            # T31–T35 活跃池词窗口：池词超出本批窗口即剪枝，非池词永不剪，found 池词反馈回池
            tab = Path(td) / "tab_window.json"
            tab.write_text(json.dumps({
                "池词甲": {"origin": "pool:a", "found": False, "search_count": 0},
                "池词乙": {"origin": "pool:b", "found": True, "found_at": "2026-10-01 10:00:00"},
                "既有课程A": {"search_count": 9, "found": True},
            }, ensure_ascii=False), encoding="utf-8")
            r31 = merge_words_into(tab, {"池词甲": {"origin": "pool:a", "found": False,
                                                   "search_count": 0}})
            got31 = json.loads(tab.read_text(encoding="utf-8"))
            check("T31 窗口外池词被剪枝", set(got31) == {"池词甲", "既有课程A"}
                  and r31["removed"] == ["池词乙"])
            check("T32 非池词不剪枝（负控）", "既有课程A" in got31
                  and got31["既有课程A"]["search_count"] == 9)
            check("T33 found 池词进 found_back（负控）", r31["found_back"] == ["池词乙"])
            tab2 = Path(td) / "tab_window2.json"
            tab2.write_text(json.dumps({
                "池词丙": {"origin": "pool:c", "found": False},
                "既有课程B": {"search_count": 4},
            }, ensure_ascii=False), encoding="utf-8")
            r32 = merge_words_into(tab2, {"池词丙": {"origin": "pool:c"}}, prune_pool_window=False)
            got32 = json.loads(tab2.read_text(encoding="utf-8"))
            check("T34 关闭窗口不剪枝（负控）", r32["removed"] == [] and set(got32) == {"池词丙", "既有课程B"})
            wpath = Path(td) / "words_window.json"
            out35 = emit_words(limit=3, out=wpath, merge_into=None)
            words35 = json.loads(wpath.read_text(encoding="utf-8"))
            pool35 = {c["title"]: int(c.get("search_count") or 0) for c in load_pool()["courses"]}
            check("T35 表侧计数归零·池侧独立累加（负控）", out35["count"] == len(words35)
                  and all(v["search_count"] == 0 for v in words35.values())
                  and any(v > 0 for v in pool35.values()))
            # T36–T44 采集通道链与跨源声明（R550，全离线：网络函数被打桩）
            real_http, real_jina, real_browser = _http_text, _jina_text, browser_get
            html_ok = ('<li data-cid="77" data-typestr="实战课" data-name="真 HTML 课"'
                       ' data-price="100">' + "x" * 260)
            junk = "纯文本噪声" * 80          # 非空、够长，但 HTML 解析器认不出
            try:
                def mk(mode, payload):
                    def _f(url, *a, **kw):
                        if payload is None:
                            raise RuntimeError("stub-%s-fail" % mode)
                        return payload
                    return _f
                globals()["_http_text"] = mk("http", junk)
                globals()["_jina_text"] = mk("jina", html_ok)
                m, _h, errs = fetch_one("http://x/1", prefer=["http", "jina"],
                                        parser=parse_imooc_list)
                check("T36 解析0条的通道判失败并回退（负控）", m == "jina"
                      and any(e.get("err", "").startswith("parsed_0") for e in errs))
                globals()["_jina_text"] = mk("jina", junk)
                try:
                    fetch_one("http://x/2", prefer=["http", "jina"], parser=parse_imooc_list)
                    check("T37 全通道 0 条须抛错（负控）", False)
                except RuntimeError as e:
                    check("T37 全通道 0 条须抛错（负控）", "全部通道失败" in str(e)
                          and "parsed_0" in str(e))
                globals()["_http_text"] = mk("http", "<html></html>")
                globals()["_jina_text"] = mk("jina", html_ok)
                m2, _h2, errs2 = fetch_one("http://x/3", prefer=["http", "jina"],
                                           parser=parse_imooc_list, min_bytes=200)
                check("T38 过短内容判失败（负控）", m2 == "jina"
                      and any("empty_or_short" in e.get("err", "") for e in errs2))
                # jina 返回 markdown 时必须走 md 解析器（否则静默 0 条）
                md = ("*   [跨源课 进阶 · 12人报名 ￥99￥199.00 加购物车]"
                      "(https://coding.imooc.com/class/4242.html)\n") + ("\n" * 300)
                globals()["_http_text"] = mk("http", junk)
                globals()["_jina_text"] = mk("jina", md)
                src = {"id": "iso", "name": "T", "channel": ["jina"],
                       "urls": ["http://x/md"],
                       "parser": "imooc_list", "parser_by_channel": {"jina": "imooc_md_list"}}
                res = collect_source(src, sleep=0)
                check("T39 parser_by_channel 生效（jina 走 md 解析）",
                      bool(res["records"]) and res["records"][0]["source_id"] == "4242"
                      and res["per_url"][0]["mode"] == "jina")
                ok_src = validate_sources({"sources": [dict(src, urls=["http://x/4"])]})
                check("T40 合法声明通过校验", ok_src["sources"][0]["id"] == "iso")
                for bad, tag in ((dict(src, parser_by_channel={"jina": "nope"}), "未知 parser"),
                                 (dict(src, parser_by_channel={"ftp": "imooc_md_list"}), "未知通道")):
                    try:
                        validate_sources({"sources": [bad]})
                        check("T41 非法 parser_by_channel 须拒（负控/%s）" % tag, False)
                    except ValueError:
                        check("T41 非法 parser_by_channel 须拒（负控/%s）" % tag, True)
                try:
                    validate_sources({"sources": [{"id": "z", "name": "z",
                                                   "channel": ["http"], "parser": "no_such"}]})
                    check("T42 未知 parser 须拒（负控）", False)
                except ValueError:
                    check("T42 未知 parser 须拒（负控）", True)
                d = DEFAULT_SOURCES["sources"][0]
                check("T43 默认慕课源声明 jina→md 解析器",
                      d["id"] == "imooc" and (d.get("parser_by_channel") or {}).get("jina") == "imooc_md_list"
                      and "jina" in d["channel"])
                globals()["_http_text"] = mk("http", junk)
                globals()["_jina_text"] = mk("jina", html_ok)
                src_mc = {"id": "mc", "name": "T", "channel": ["http", "jina"],
                          "urls": ["http://x/5"], "parser": "imooc_list",
                          "parser_by_channel": {"jina": "imooc_list"}}
                res_mc = collect_source(src_mc, sleep=0)
                check("T45 多通道源 0 条即回退（回归）",
                      res_mc["per_url"][0]["mode"] == "jina"
                      and res_mc["per_url"][0]["items"] == 1
                      and any(e.get("err", "").startswith("parsed_0")
                              for e in res_mc["per_url"][0]["errs"]))
                check("T44 默认源自身通过校验",
                      validate_sources(DEFAULT_SOURCES)["version"] == DEFAULT_SOURCES["version"])
            finally:
                globals()["_http_text"], globals()["_jina_text"] = real_http, real_jina
                globals()["browser_get"] = real_browser
            try:
                emit_words(limit=1, merge_into=haisou_like)
                check("T30 坏词表拒写（负控）", False)
            except RuntimeError:
                check("T30 坏词表拒写（负控）", haisou_like.read_text(encoding="utf-8") == "{not json")
        finally:
            globals()["POOL_PATH"], globals()["USER_PATH"], globals()["WORDS_OUT"] = old

    import tempfile as _tf
    td2 = _tf.mkdtemp(prefix="cp_match_")
    old2 = globals()["POOL_PATH"], globals()["USER_PATH"]
    try:
        globals()["POOL_PATH"] = Path(td2) / "pool.json"
        globals()["USER_PATH"] = Path(td2) / "user.json"
        save_json(Path(td2) / "pool.json", {"courses": [
            {"id": "imooc:1013", "source": "imooc", "title": "Agent 工程化 + AI 编程深度实战营 进阶",
             "status": "hunting", "star": 3, "students": 900, "first_seen": today(), "last_seen": today(),
             "resources": []},
            {"id": "pan:p1", "source": "quangneng", "title": "Agent 工程化 + AI 编程深度实战营 (2026)",
             "status": "hunting", "star": 0, "students": 0, "first_seen": today(), "last_seen": today(),
             "resources": []},
        ]})
        hpath = Path(td2) / "hits.json"
        hpath.write_text(_json.dumps({
            "h1": {"title": "Agent 工程化 + AI 编程深度实战营（全套）视频教程",
                   "link": "https://pan.example/s/aaa", "keyword": "AI",
                   "seen_at": "2026-10-06 10:00:00"},
            "h2": {"title": "完全无关的考研英语阅读真题精讲", "link": "https://pan.example/s/bbb"},
        }, ensure_ascii=False), encoding="utf-8")
        hh = load_hits(include_pan=False, haisou_path=hpath)
        check("T46 命中库解析（title/link 齐备才收）", len(hh) == 2 and hh[0]["src"] == "haisou"
              and hh[0]["url"] == "https://pan.example/s/aaa")
        check("T47 噪声词差异不降分（视频教程/全套/括号年份）",
              title_score("Agent 工程化 + AI 编程深度实战营 (2026)",
                          "Agent 工程化 + AI 编程深度实战营（全套）视频教程") >= MATCH_MIN_SCORE)
        check("T48 不相关标题低于阈值（负控）",
              title_score("Agent 工程化 + AI 编程深度实战营", "完全无关的考研英语阅读真题精讲") < MATCH_MIN_SCORE)
        check("T49 过短标题不得乱配（负控）",
              title_score("AI", "AI Agent 实战训练营课程") == 0.0
              and title_score("C++", "C++ 高级编程实战大全") == 0.0)
        try:
            load_hits(include_pan=False, haisou_path=Path(td2) / "nope.json")
            check("T50 命中库缺失须报错（负控：禁静默当空）", False)
        except RuntimeError:
            check("T50 命中库缺失须报错（负控：禁静默当空）", True)
        before_user = Path(td2).joinpath("user.json").read_bytes() if Path(td2).joinpath("user.json").exists() else None
        dry = cmd_match(apply=False, hits_path=str(hpath), include_pan=False, limit=10)
        after_user = Path(td2).joinpath("user.json").read_bytes() if Path(td2).joinpath("user.json").exists() else None
        check("T51 dry-run 零写盘且能出候选（负控）",
              dry["applied"] == 0 and dry["candidates"] >= 1 and before_user == after_user)
        ap1 = cmd_match(apply=True, hits_path=str(hpath), include_pan=False, limit=10)
        usr1 = load_user()
        n1 = len([r for r in usr1.get("resources", []) if r.get("via", "").startswith("auto:")])
        # 第二遍放开 include_found，才能走到 (course_id,url) 去重分支：
        # 默认策略下「已有资源」的课程会被前置跳过（skipped_existing 恒 0），这是两回事。
        ap2 = cmd_match(apply=True, hits_path=str(hpath), include_pan=False,
                        include_found=True, limit=10)
        ap3 = cmd_match(apply=False, hits_path=str(hpath), include_pan=False,
                        include_found=True, limit=10)
        usr2 = load_user()
        n2 = len([r for r in usr2.get("resources", []) if r.get("via", "").startswith("auto:")])
        check("T52 apply 写盘且幂等（负控：重复不追加）",
              ap1["applied"] >= 1 and n1 == ap1["applied"]
              and ap2["applied"] == 0 and ap2["skipped_existing"] >= 1
              and n2 == n1 and ap3["candidates"] == 0)
        check("T53 已挂资源的课程不再重复匹配（负控）",
              cmd_match(apply=False, hits_path=str(hpath), include_pan=False)["candidates"] == 0)
        # ---- T54–T58 看板候选面板 + 快照读取（fail-loud vs 静默降级）----
        h_none = render_html({"courses": []}, cands=None)
        check("T54 无候选快照时不得上面板（负控）", "网盘命中候选" not in h_none)
        fake = {"ts": "2026-01-01 00:00:00", "schema": "course_match_candidates/v1",
                "min_score": 0.62,
                "all_matches": [{"course_title": "C1", "hit_title": "H1", "score": 0.9,
                                 "src": "haisou", "url": "https://example.com/x", "kw": "AI"}]}
        h_some = render_html({"courses": []}, cands=fake)
        check("T55 有快照时面板计数=实际条数",
              "网盘命中候选" in h_some and "共 1" in h_some
              and h_some.count("<tr>") - h_none.count("<tr>") == 2
              and h_some.count("<tr>") > h_none.count("<tr>"))
        miss = Path(td2) / "no_cands.json"
        got, warn = load_candidates(miss)
        check("T56 快照缺失默认静默降级（带 warn）", got is None and bool(warn))
        try:
            load_candidates(miss, explicit=True)
            check("T57 explicit=True 缺文件须报错（负控）", False)
        except FileNotFoundError:
            check("T57 explicit=True 缺文件须报错（负控）", True)
        bad = Path(td2) / "bad_cands.json"
        bad.write_text('{"schema": "wrong-schema"}', encoding="utf-8")
        got2, warn2 = load_candidates(bad)
        try:
            load_candidates(bad, explicit=True)
            raise_ok = False
        except ValueError:
            raise_ok = True
        check("T58 格式不符须拒绝（默认降级/显式抛错，负控）",
              got2 is None and bool(warn2) and raise_ok)
    finally:
        # 沙箱必须延续：曾在此处还原成生产路径（old2），致其后用例读生产 pool/user 假红（R552 修）
        globals()["POOL_PATH"] = Path(td2) / "pool.json"
        globals()["USER_PATH"] = Path(td2) / "user.json"

    # ---- last-good 缓存回退（R550）：兜底可用 + 三类负控 ----
    with tempfile.TemporaryDirectory() as tdc:
        oldc = globals().get("CACHE_DIR")
        globals()["CACHE_DIR"] = Path(tdc) / "cache"
        try:
            cu = "https://class.imooc.com/"
            # 夹具须 > min_bytes(200)：缓存回退与通道同样受 min_bytes 门禁，短正文会被判空
            cbody = ("*   [Agent 工程化+ AI 编程深度实战营 进阶 · 85人报名 _10月特惠_￥1299￥1480.00 "
                     "加购物车](https://coding.imooc.com/class/1013.html)\n"
                     "*   [从0到1 转型 Agent应用开发工程师 零基础 · 29人报名 ￥499￥599.00 加购物车]"
                     "(https://coding.imooc.com/class/1040.html)\n"
                     "*   [AI 大模型应用开发实战 中级 · 12人报名 ￥899￥999.00 加购物车]"
                     "(https://coding.imooc.com/class/1055.html)\n"
                     "Copyright 2026 imooc.com\n")
            check("T59 缓存缺失读空（负控）", cache_get(cu)[0] is None)
            cache_put(cu, cbody, "jina")
            got, meta = cache_get(cu)
            check("T60 缓存写入可读且带 age/mode",
                  got == cbody and bool(meta) and meta.get("mode") == "jina"
                  and meta.get("age_hours", 9.0) < 1.0)
            cp = CACHE_DIR / (_cache_key(cu) + ".txt")
            cp.with_suffix(".meta.json").write_text(
                _json.dumps({"url": cu, "ts": time.time() - 240 * 3600, "mode": "jina"}),
                encoding="utf-8")
            check("T61 超期缓存不得回退（负控）", cache_get(cu, max_hours=168)[0] is None)
            check("T62 max_hours=0 表示不限龄", cache_get(cu, max_hours=0)[0] == cbody)
            saved_ch = (globals()["_http_text"], globals()["_jina_text"])

            def _boom(*_a, **_k):
                raise RuntimeError("offline")

            globals()["_http_text"] = _boom
            globals()["_jina_text"] = _boom
            cache_put(cu, cbody, "jina")   # T61 已把它置过期，这里恢复成新鲜缓存
            try:
                mode, txt, errs = fetch_one(cu, prefer=("http", "jina"),
                                            parser=parse_imooc_md_any)
                check("T63 通道全灭回退缓存并标 mode=cache",
                      mode == "cache" and txt == cbody
                      and any(e.get("mode") == "cache" for e in errs))
                try:
                    fetch_one("https://class.imooc.com/?nohit=1", prefer=("http",),
                              parser=parse_imooc_md_any)
                    check("T64 无缓存仍失败（负控）", False)
                except RuntimeError:
                    check("T64 无缓存仍失败（负控）", True)
                cache_put("https://class.imooc.com/?shell=1",
                          "<html>反爬壳页</html>", "http")
                try:
                    fetch_one("https://class.imooc.com/?shell=1", prefer=("http",),
                              parser=parse_imooc_md_any)
                    check("T65 缓存解析 0 条不冒充成功（负控）", False)
                except RuntimeError:
                    check("T65 缓存解析 0 条不冒充成功（负控）", True)
                r = collect_source({"id": "t_src", "name": "t", "channel": ["http"],
                                    "parser": "imooc_md_any", "urls": [cu]}, sleep=0)
                check("T66 collect_source 标记 stale 且条数正确",
                      r["per_url"][0]["mode"] == "cache"
                      and r["per_url"][0].get("stale") is True
                      and len(r["records"]) == 3)
            finally:
                globals()["_http_text"], globals()["_jina_text"] = saved_ch
        finally:
            if oldc is not None:
                globals()["CACHE_DIR"] = oldc

    # ---- T67-T70 采集面台账 + 看板「采集面」面板（R552 看板 v2）----
    with tempfile.TemporaryDirectory() as td:
        saved_fl = globals().get("FETCH_LOG")
        saved_pu = (globals()["POOL_PATH"], globals()["USER_PATH"])
        globals()["FETCH_LOG"] = Path(td) / "fetch_log.json"
        try:
            info = {
                "imooc": {"name": "慕课", "channel": ["direct"], "records": 3, "stale": False,
                          "per_url": [{"url": "u1", "mode": "direct", "stale": False, "records": 3}],
                          "upsert": {"added": 2, "updated": 1}},
                "quangneng": {"name": "全能", "channel": ["cache"], "records": 5, "stale": True,
                              "per_url": [{"url": "u2", "mode": "cache", "stale": True, "records": 5}],
                              "upsert": {"added": 0, "updated": 5}}}
            lp = save_fetch_log(info, 8)
            d = load_fetch_log()
            check("T67 台账落盘可读（源/通道/入库数齐）",
                  lp is not None and d is not None and d["fetched"] == 8
                  and set(d["sources"]) == {"imooc", "quangneng"}
                  and d["sources"]["quangneng"]["stale"] is True
                  and d["sources"]["quangneng"]["per_url"][0]["mode"] == "cache")
            check("T68 干跑不落台账（负控）", save_fetch_log(info, 8, dry_run=True) is None)
            b1 = Path(td) / "bad1.json"; b1.write_text("{not json", encoding="utf-8")
            b2 = Path(td) / "bad2.json"; b2.write_text('{"schema":"other/v1","sources":{}}', encoding="utf-8")
            b3 = Path(td) / "bad3.json"; b3.write_text('{"schema":"course.fetch_log/v1","sources":[]}', encoding="utf-8")
            check("T69 坏台账不冒充（负控：坏JSON/错schema/源非dict 三态归一）",
                  load_fetch_log(Path(td) / "absent.json") is None
                  and load_fetch_log(b1) is None and load_fetch_log(b2) is None
                  and load_fetch_log(b3) is None)
            # 看板：带台账⇒出现「采集面」面板与缓存回退标记；不带⇒无面板、不崩溃
            env = dict(os.environ, COURSE_FETCH_LOG=str(globals()["FETCH_LOG"]),
                       COURSE_DB=str(Path(td) / "render_default.db"))
            r1 = subprocess.run([sys.executable, __file__, "render", "--out", str(Path(td) / "a.html")],
                                capture_output=True, text=True, env=env)
            h1 = (Path(td) / "a.html").read_text(encoding="utf-8") if (Path(td) / "a.html").exists() else ""
            env2 = dict(os.environ, COURSE_FETCH_LOG=str(Path(td) / "nope.json"))
            r2 = subprocess.run([sys.executable, __file__, "render", "--out", str(Path(td) / "b.html")],
                                capture_output=True, text=True, env=env2)
            h2 = (Path(td) / "b.html").read_text(encoding="utf-8") if (Path(td) / "b.html").exists() else ""
            check("T70 看板采集面面板（有台账上面板/缓存回退可见；缺台账静默不崩）",
                  r1.returncode == 0 and "采集面" in h1 and "缓存回退" in h1
                  and r2.returncode == 0 and "采集面" not in h2 and len(h2) > 200)

            # ---- 统一落库 platform.db（沙箱：库里所有断言只碰 td 下的临时库） ----
            # 读侧必须同指 td 沙箱，否则 db_build 读生产 pool/user（曾致 T71/T72 假红，R552 修）
            globals()["POOL_PATH"] = Path(td) / "pool.json"
            globals()["USER_PATH"] = Path(td) / "user.json"
            save_json(Path(td) / "pool.json", {"courses": [
                {"id": "imooc:c1", "source": "imooc", "title": "A", "url": "u1",
                 "first_seen": today(), "last_seen": today()},
                {"id": "pan:c2", "source": "quangneng", "title": "B", "url": "u2",
                 "first_seen": today(), "last_seen": today()},
            ]})
            sample_res = [{"course_id": "imooc:c1", "title": "A", "pan_url": "https://pan.x/a",
                           "need_pass": "", "score": 0.9, "found_at": "2026-10-06 10:00:00"}]
            save_json(Path(td) / "user.json", {"resources": sample_res})
            dbp = Path(td) / "db" / "platform.db"
            d1 = db_build(dbp)
            d2 = db_build(dbp)
            check("T71 落库幂等（两次构建计数逐字相同且库真落盘）",
                  {k: v for k, v in d1.items() if k != "built_at"}
                  == {k: v for k, v in d2.items() if k != "built_at"}
                  and dbp.exists() and d1.get("courses", 0) == 2
                  and d1.get("resources", 0) == 1)
            check("T72 表计数键齐全且 built_at 非空",
                  {"exists", "courses", "resources", "courses_with_res", "haisou_seen",
                   "pan_hits", "candidates", "fetch_runs", "built_at"} <= set(d1)
                  and d1["courses_with_res"] == 1 and bool(d1["built_at"]))
            nope = Path(td) / "nope.db"
            miss = db_stats(nope)
            check("T73 库不存在不建库（负控 exists=False 且文件未产生）",
                  miss.get("exists") is False and not nope.exists())

            merged_s = load_pool()
            h_on = render_html(merged_s, dbst={"exists": True, "courses": 3, "resources": 1,
                                               "courses_with_res": 1, "haisou_seen": 2,
                                               "pan_hits": 4, "candidates": 5, "fetch_runs": 1,
                                               "built_at": "2026-10-06 10:00:00",
                                               "top_courses": [{"course_id": "c1", "resources": 1}]})
            check("T75 看板落库面板（计数/构建时间来自库统计）",
                  "统一落库" in h_on and "courses 3" in h_on and "2026-10-06 10:00:00" in h_on)
            h_none = render_html(merged_s, dbst={"exists": False})
            check("T76 库未建显示「库未建」且不崩（负控）",
                  "统一落库" in h_none and "库未建" in h_none)
            h_off = render_html(merged_s)
            check("T77 不传 dbst 时无落库面板（默认隔离）",
                  "统一落库" not in h_off and len(h_off) > 200)

            env_db = dict(os.environ, COURSE_DB=str(dbp))
            rdb = subprocess.run([sys.executable, __file__, "db", "--db", str(dbp)],
                                 capture_output=True, text=True, env=env_db)
            check("T78 db 子命令真跑（rc=0 且打印 courses 计数）",
                  rdb.returncode == 0 and "courses" in rdb.stdout and "platform.db" in rdb.stdout)
            r3 = subprocess.run([sys.executable, __file__, "render", "--out", str(Path(td) / "c.html")],
                                capture_output=True, text=True, env=env_db)
            h3 = (Path(td) / "c.html").read_text(encoding="utf-8") if (Path(td) / "c.html").exists() else ""
            r4 = subprocess.run([sys.executable, __file__, "render", "--no-db",
                                 "--out", str(Path(td) / "d.html")],
                                capture_output=True, text=True, env=env_db)
            h4 = (Path(td) / "d.html").read_text(encoding="utf-8") if (Path(td) / "d.html").exists() else ""
            check("T79 CLI render 带落库面板 / --no-db 隐藏（负控）",
                  r3.returncode == 0 and "统一落库" in h3
                  and r4.returncode == 0 and "统一落库" not in h4)

            # ---- T80–T83 全量落库面（R553）：盘搜 raw 真形态（关键词字典）+ 四新表 ----
            raw_dir = Path(td) / "panraw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            save_json(raw_dir / "raw_x.json", {
                "AI 出海": [
                    {"kw": "AI 出海", "title": "T1", "pan": "quark",
                     "url": "https://pan.x/1", "code": "ab12",
                     "datetime": "2026-10-06T12:00:00", "channel": "c",
                     "note": "", "topic": "ai_overseas"},
                    {"title": "T2", "pan": "baidu", "url": "https://pan.x/2"},
                ],
                "课程甲": [{"title": "T3", "pan": "quark", "url": "https://pan.x/3"}],
            })
            save_json(raw_dir / "raw_y.json", {          # 同 url 后写覆盖（去重口径）
                "课程乙": [{"title": "T3 改", "pan": "quark", "url": "https://pan.x/3"}]})
            save_json(Path(td) / "pan_seen.json",
                      {"https://pan.x/1": "2026-10-06 12:00:00"})
            save_json(Path(td) / "kw_pool.json",
                      {"池词甲": {"added_at": "2026-10-06", "found": True,
                                  "source_url": "https://a/1", "published_at": "p1"}})
            save_json(Path(td) / "kw_qn.json",
                      {"全能词": {"added_at": "2026-09-05", "found": False,
                                  "source_url": "https://b/2", "published_at": "p2"}})
            saved_g = {k: globals()[k] for k in
                       ("PAN_RAW_GLOB", "PAN_SEEN", "WORDS_OUT", "MERGE_INTO")}
            globals()["PAN_RAW_GLOB"] = str(raw_dir / "raw_*.json")
            globals()["PAN_SEEN"] = Path(td) / "pan_seen.json"
            globals()["WORDS_OUT"] = Path(td) / "kw_pool.json"
            globals()["MERGE_INTO"] = Path(td) / "kw_qn.json"
            try:
                d3 = db_build(dbp)
                check("T80 盘搜 raw 关键词字典形态可入表（字典键兜底 keyword）",
                      d3.get("pan_hits") == 3 and d3.get("pan_seen") == 1)
                con3 = sqlite3.connect("file:%s?mode=ro" % str(dbp), uri=True)
                try:
                    rows3 = con3.execute(
                        "SELECT url,keyword,code,pan,channel,topic FROM pan_hits "
                        "ORDER BY url").fetchall()
                finally:
                    con3.close()
                check("T81 行字段齐全（keyword/code/pan/channel/topic 不丢）",
                      len(rows3) == 3 and rows3[0][1] == "AI 出海"
                      and rows3[0][2] == "ab12" and rows3[0][4] == "c"
                      and rows3[0][5] == "ai_overseas" and rows3[1][1] == "AI 出海"
                      and rows3[1][2] == "" and rows3[2][1] == "课程乙")
                check("T82 重复 url 后写覆盖且总数=去重后（负控：不得重复计数）",
                      d3.get("pan_hits") == 3)
                check("T83 搜索词池/源注册落表（两池分列）",
                      d3.get("keywords") == 2 and d3.get("sources", 0) >= 1
                      and d3.get("schema_tables", 0) >= 11)
            finally:
                for k, v in saved_g.items():
                    globals()[k] = v
        finally:
            if saved_fl is not None:
                globals()["FETCH_LOG"] = saved_fl
            globals()["POOL_PATH"], globals()["USER_PATH"] = saved_pu
            globals()["DB_PATH"] = saved_db

    check("T84 自测不写生产库（负控：生产 DB 指纹前后一致）",
          _db_fp(prod_db) == prod_db_before)
    print(f"selftest: {ok} passed, {len(failed)} failed")
    for f in failed:
        print("  FAIL", f)
    return 0 if not failed else 1


# ==================== 采集源注册表（跨源可扩展，R550） ====================
# 一个「源」= 注册表里一条声明（列表页 URL 模板 + parser + 通道偏好 + 详情页 parser）。
# 抓取通道三档，按源声明 prefer 顺序自动降级：http(直连) → jina(r.jina.ai) → browser(真实 Chrome)。
# 站点反爬升级只改注册表；新增站点=加一条声明 + 一个 parser 名（工具：course_pool sources/fetch/add-url）。
import argparse as _argparse
import json as _json

SOURCES_PATH = Path(os.environ.get("COURSE_SOURCES") or BASE / "tools/course_sources.json")
DRIVER_URL = os.environ.get("GA_DRIVER_URL") or "http://127.0.0.1:18766/link"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _out(obj):
    print(_json.dumps(obj, ensure_ascii=False, indent=2))


def _http_text(url, timeout=30):
    """http 通道：直连取整页文本（服务端渲染的站点用这档）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _jina_text(url, timeout=60):
    """jina 通道：r.jina.ai 渲染取文（部分站点可用；远端 401/超时时自动降级到 browser）。

    ★实测：不带 UA 时 r.jina.ai 直接 401（class.imooc.com 等），必须显式带浏览器 UA。
    """
    req = urllib.request.Request("https://r.jina.ai/" + url, headers={"User-Agent": UA,
                                                                     "Accept": "text/plain"})
    last = None
    for att, wait in enumerate((0, 20, 45)):
        if wait:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
            # ★实测：r.jina.ai 自身会被 CF 挑战（返回「Just a moment...」HTML，HTTP 403），
            # 属分钟级窗口非永久封禁 ⇒ 视为可重试，退避后重打。
            if txt.lstrip().startswith("<!DOCTYPE html") or "Just a moment" in txt[:400]:
                last = RuntimeError("jina CF challenge")
                continue
            return txt
        except urllib.error.HTTPError as e:  # noqa: PERF203
            last = e
            if e.code not in (403, 429, 500, 502, 503, 504):
                raise
        except Exception as e:  # 超时/连接错误同样可重试
            last = e
    raise last if last else RuntimeError("jina failed")


def _driver_cmd(payload, timeout=60):
    req = urllib.request.Request(DRIVER_URL, data=_json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode("utf-8", "replace") or "{}")


def _driver_sessions():
    try:
        r = _driver_cmd({"cmd": "get_all_sessions"}, timeout=15)
    except Exception:
        return []
    got = r.get("r") if isinstance(r, dict) else None
    if isinstance(got, dict):
        got = got.get("sessions") or got.get("tabs") or []
    return [s for s in (got or []) if isinstance(s, dict) and s.get("id")]


def _driver_js(code, sid=None, timeout=60):
    payload = {"cmd": "execute_js", "code": code, "timeout": timeout}
    if sid is not None:
        payload["sessionId"] = str(sid)   # ★必须是字符串：传 int 会静默回落 alive_sessions[0]
    r = _driver_cmd(payload, timeout=timeout + 15)
    out = r.get("r") if isinstance(r, dict) else None
    if isinstance(out, dict):
        return out.get("data") or ""
    return out or ""


def browser_get(url, wait=10.0, host_sid=None, close=True, min_bytes=200):
    """browser 通道：借用户已开的 Chrome 开新标签读整页 outerHTML（服务端反爬时的唯一通道）。

    ★匹配口径：只认「本次新开」的标签，并用 location.href 校验与请求 URL 一致；
    宽松 key 匹配会命中用户已开的同域详情页 ⇒ 解析 0 条且无报错（历史真缺陷）。
    """
    sess = _driver_sessions()
    if not sess:
        raise RuntimeError("browser 通道不可用：驱动无会话（浏览器未开或扩展未唤醒）")
    host = str(host_sid) if host_sid else str(sess[0]["id"])
    before = set(str(s["id"]) for s in sess)
    want = url.split("#")[0].rstrip("/")
    path = want.split("?")[0]
    query = want.split("?", 1)[1] if "?" in want else ""
    _driver_js("window.open(%s);'opened'" % _json.dumps(url), host)
    tid, deadline = None, time.time() + max(3.0, float(wait))
    while time.time() < deadline:
        for s in _driver_sessions():
            sid = str(s["id"])
            if sid in before:
                continue
            u = str(s.get("url") or "")
            if path not in u or (query and query not in u):
                continue
            try:
                href = str(_driver_js("location.href", sid) or "")
            except Exception:
                href = ""
            if href and path in href and (not query or query in href):
                tid = sid
                break
        if tid:
            break
        time.sleep(0.5)
    if not tid:
        newtabs = [str(s.get("url") or "")[:80] for s in _driver_sessions()
                   if str(s["id"]) not in before]
        raise RuntimeError("browser 通道：未出现匹配的新标签 %s（新标签: %s）" % (url, newtabs))
    html = _driver_js("document.documentElement.outerHTML", tid)
    if close:
        try:
            _driver_js("window.close();'closing'", tid)
        except Exception:
            pass
    if not html or len(html) < int(min_bytes):
        raise RuntimeError("browser 通道：页面内容为空 %s" % url)
    return html


_IMOOC_CARD = re.compile(
    r'<li[^>]*data-cid="(?P<sid>\d+)"[^>]*data-typestr="(?P<type>[^"]*)"[^>]*'
    r'data-name="(?P<title>[^"]*)"[^>]*data-price="(?P<price>[^"]*)"', re.S)
_IMOOC_NUM = re.compile(r"(?:(零基础|初阶|进阶|专业|高级)\s*·\s*)?(\d+)\s*人报名")
_ART_RX = re.compile(r"<article[\s\S]*?</article>")
_QN_TITLE = re.compile(
    r'<h2[^>]*entry-title[^>]*>[\s\S]{0,300}?<a[^>]*href="([^"]+)"[^>]*>([\s\S]{0,300}?)</a>', re.I)
_QN_CAT = re.compile(r'rel="category">[\s\S]{0,80}?</i>([^<]{0,40})</a>|rel="category">([^<]{0,40})</a>')


def _clean(s):
    AMP = chr(38)
    ents = ((AMP + "amp;", "&"), (AMP + "quot;", '"'), (AMP + "apos;", "'"),
            (AMP + "#39;", "'"), (AMP + "lt;", "<"), (AMP + "gt;", ">"),
            (AMP + "nbsp;", " "))
    s = str(s or "")
    for a, b in ents:
        s = s.replace(a, b)
    return s.strip()


def parse_imooc_list(html):
    """慕课列表页（服务端渲染卡片属性）→ 课程记录列表。"""
    out, seen = [], set()
    h = html or ""
    for m in _IMOOC_CARD.finditer(h):
        sid = m.group("sid")
        if sid in seen:
            continue
        seen.add(sid)
        tail = h[m.end():m.end() + 1500]
        num = _IMOOC_NUM.search(tail)
        prices = PRICE_RX.findall(tail[:600])
        out.append({
            "source_id": sid, "title": _clean(m.group("title")),
            "url": "https://coding.imooc.com/class/%s.html" % sid,
            "type": _clean(m.group("type")) or "实战课",
            "level": (num.group(1) if num and num.group(1) else ""),
            "students": int(num.group(2)) if num else 0,
            "price": float(prices[0]) if prices else None,
            "price_orig": float(prices[1]) if len(prices) > 1 else None,
        })
    return out


def parse_quangneng_list(html):
    """全能网列表页（WP article 服务端渲染）→ 课程记录列表。"""
    out, seen = [], set()
    for blk in _ART_RX.findall(html or ""):
        t = _QN_TITLE.search(blk)
        if not t:
            continue
        url, title = t.group(1), _clean(t.group(2))
        key = url.split("/qdkf/")[0]
        if not title or key in seen:
            continue
        seen.add(key)
        # 分类：article 内 rel="category" 的第一个（category-ai→AI 等）
        cat_m = re.search(r'categories/[^"]+"[^>]*>([^<]{0,40})</a>', blk)
        tag_m = re.search(r'rel="tag">([^<]{0,40})</a>', blk)
        price_rx = re.compile(r"(?:免费|￥\s*([0-9]+(?:\.[0-9]+)?))")
        pm = re.search(r"€\s*([0-9]+(?:\.[0-9]+)?)|￥\s*([0-9]+(?:\.[0-9]+)?)|免费", blk)
        price = None
        if pm:
            if pm.group(1):
                price = float(pm.group(1))
            elif pm.group(2):
                price = float(pm.group(2))
            else:
                price = 0.0
        out.append({
            "source_id": key.strip("/").split("/")[-1] if key.strip("/") else "",
            "title": title, "url": url, "type": "视频课",
            "category": _clean(cat_m.group(1)) if cat_m else "",
            "tags": _clean(tag_m.group(1)) if tag_m else "",
            "price": price, "level": "", "students": 0,
        })
    return out


def parse_imooc_detail(html, rec):
    """慕课详情页（如 add-url 投喂单课时）补齐价格/人数/简介。"""
    rec = dict(rec or {})
    title = re.search(r'<h1[^>]*course-title[^>]*>([^<]{0,200})</h1>', html)
    if title and not rec.get("title"):
        rec["title"] = _clean(title.group(1))
    prices = PRICE_RX.findall(html[:6000])
    if prices and rec.get("price") in (None, 0, ""):
        rec["price"] = float(prices[0])
    num = _IMOOC_NUM.search(html)
    if num and not rec.get("students"):
        rec["students"] = int(num.group(2))
    if not rec.get("source_id"):
        m = re.search(r"/class/(\d+)\.html", rec.get("url") or html)
        rec["source_id"] = m.group(1) if m else ""
    return rec


def parse_quangneng_detail(html, rec):
    """全能详情页补齐：标题/价格/分类/标签/更新时间（服务端渲染）。"""
    rec = dict(rec or {})
    if not rec.get("title"):
        m = re.search(r"<title>([^<]{1,200})</title>", html)
        if m:
            rec["title"] = _clean(m.group(1).split("-")[0])
    if rec.get("price") in (None, 0):
        pm = re.search(r"€\s*([0-9]+(?:\.[0-9]+)?)|￥\s*([0-9]+(?:\.[0-9]+)?)|(免费)", html[:8000])
        if pm:
            rec["price"] = float(pm.group(1) or pm.group(2) or 0) if (pm.group(1) or pm.group(2)) else 0.0
    cm = re.search(r'categories/[^"]+"[^>]*>([^<]{0,40})</a>', html)
    if cm and not rec.get("category"):
        rec["category"] = _clean(cm.group(1))
    tm = re.search(r"(\d{4}-\d{2}-\d{2})", html)
    if tm and not rec.get("published_at"):
        rec["published_at"] = tm.group(1)
    if not rec.get("source_id"):
        m = re.search(r"quangneng\.com/(\d+)/", rec.get("url") or "")
        rec["source_id"] = m.group(1) if m else ""
    return rec


# ==================== last-good 抓取缓存（反爬兜底，R550） ====================
# 反爬站点（如慕课列表页必须走 jina）在远端被 CF 挑战时通道可能全灭 ⇒ 回退「最近一次
# 成功抓取的正文」并标 mode=cache，保证每日采集不空、且可取证（不伪装成新抓取）。
# 有效期由 COURSE_CACHE_MAX_HOURS 控制（默认 168h/7 天，0=不限龄）。
CACHE_DIR = Path(os.environ.get("COURSE_CACHE_DIR") or BASE / "temp/course_pool_cache")
CACHE_MAX_HOURS = float(os.environ.get("COURSE_CACHE_MAX_HOURS") or 168.0)


def _cache_key(url):
    return hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:16]


def cache_get(url, max_hours=None):
    """取该 URL 最近一次成功抓取的正文 → (text, meta)；过期/缺失返 (None, meta|None)。"""
    p = CACHE_DIR / (_cache_key(url) + ".txt")
    m = p.with_suffix(".meta.json")
    if not p.exists():
        return None, None
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
        meta = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
    except Exception:
        return None, None
    age = (time.time() - float(meta.get("ts") or p.stat().st_mtime)) / 3600.0
    lim = CACHE_MAX_HOURS if max_hours is None else float(max_hours)
    info = {"age_hours": round(age, 2), "ts": meta.get("ts"), "mode": meta.get("mode"),
            "url": meta.get("url") or url}
    if lim > 0 and age > lim:
        info["expired"] = True
        return None, info
    return txt, info


def cache_put(url, text, mode=""):
    """成功抓取后落缓存；写失败绝不影响主流程。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = CACHE_DIR / (_cache_key(url) + ".txt")
        p.write_text(text, encoding="utf-8")
        p.with_suffix(".meta.json").write_text(json.dumps(
            {"url": url, "ts": time.time(), "mode": mode, "bytes": len(text)},
            ensure_ascii=False), encoding="utf-8")
        return str(p)
    except Exception:
        return None


def fetch_one(url, prefer=None, min_bytes=200, parser=None, parser_map=None):
    """通道链：按源声明的 prefer 顺序尝试（http→jina→browser），取第一个可用结果。

    parser 给定时，「非空但解析 0 条」也判该通道失败并继续回退——
    否则通道返回的异形态正文（如 jina markdown 交给 HTML 解析器）会静默 0 条。
    """
    chain = list(prefer or ("http", "jina", "browser"))
    errs, last_html = [], ""
    for mode in chain:
        try:
            if mode == "http":
                html = _http_text(url)
            elif mode == "jina":
                html = _jina_text(url)
            elif mode == "browser":
                html = browser_get(url, wait=10.0)
            else:
                raise ValueError("未知通道 %r" % mode)
            last_html = html or ""
            if not last_html or len(last_html) < int(min_bytes):
                errs.append({"mode": mode, "err": "empty_or_short(%d)" % len(last_html)})
                continue
            pr = (parser_map or {}).get(mode) or parser
            if pr is not None and not pr(last_html):
                errs.append({"mode": mode,
                             "err": "parsed_0_bytes(%d)" % len(last_html)})
                continue
            cache_put(url, last_html, mode)
            return mode, last_html, errs
        except Exception as e:
            errs.append({"mode": mode, "err": str(e)[:200]})
    # 通道全灭 ⇒ 退回 last-good 缓存（按其原始通道选解析器；解析 0 条不算成功）
    txt, cmeta = cache_get(url)
    if txt and len(txt) >= int(min_bytes):
        cmode = (cmeta or {}).get("mode") or ""
        pr = (parser_map or {}).get(cmode) or parser
        if pr is None or pr(txt):
            errs.append({"mode": "cache",
                         "err": "fallback_last_good(age=%sh)" % (cmeta or {}).get("age_hours")})
            return "cache", txt, errs
        errs.append({"mode": "cache", "err": "parsed_0_bytes(%d)" % len(txt)})
    elif cmeta and cmeta.get("expired"):
        errs.append({"mode": "cache", "err": "expired(age=%sh)" % cmeta.get("age_hours")})
    raise RuntimeError("全部通道失败 %s :: %s" % (url, errs))


DEFAULT_SOURCES = {
    "version": 1,
    "sources": [
        {
            "id": "imooc", "name": "慕课网实战", "enabled": True,
            "channel": ["browser", "jina"], "parser": "imooc_list",
            "parser_by_channel": {"jina": "imooc_md_list"},
            "detail": "imooc_detail",
            "kind": "course",
            "note": "全站反爬：浏览器通道优先（实测 40 条/页）；无标签时退 jina 渲染文本（md 解析器）",
            "urls": [
                "https://coding.imooc.com/?page=%d" % p for p in (1, 2, 3)
            ],
        },
        {
            "id": "imooc_class", "name": "慕课体系课", "enabled": True,
            "channel": ["jina", "http"], "parser": "imooc_md_any",
            "parser_by_channel": {"http": "imooc_md_any", "jina": "imooc_md_any"},
            "kind": "course",
            "note": "体系课列表（站点反爬：http 直取只回 JS 壳，走 r.jina.ai 渲染 markdown）",
            "urls": ["https://class.imooc.com/", "https://class.imooc.com/?page=2"],
        },
        {
            "id": "imooc_free", "name": "慕课免费课", "enabled": True,
            "channel": ["jina", "http"], "parser": "imooc_md_any",
            "parser_by_channel": {"http": "imooc_md_any", "jina": "imooc_md_any"},
            "kind": "course",
            "note": "免费课列表（反爬，走 jina；解析 /learn/N 内链）",
            "urls": ["https://www.imooc.com/course/list",
                     "https://www.imooc.com/course/list?page=2"],
        },
        {
            "id": "imooc_home", "name": "慕课首页（含轮播推荐）", "enabled": True,
            "channel": ["jina", "http"], "parser": "imooc_md_any",
            "parser_by_channel": {"http": "imooc_md_any", "jina": "imooc_md_any"},
            "kind": "course",
            "note": "首页轮播/推荐位（含三类课内链），补列表页看不到的新上架投放",
            "urls": ["https://www.imooc.com/"],
        },
        {
            "id": "quangneng", "name": "全能资源网", "enabled": True,
            "channel": ["http", "browser"], "parser": "quangneng_list",
            "detail": "quangneng_detail", "kind": "course",
            "note": "WordPress 服务端渲染，http 直取即可",
            "urls": ["https://www.quangneng.com/"],
        },
    ],
}

_PARSERS = {
    "imooc_list": parse_imooc_list, "quangneng_list": parse_quangneng_list,
    # jina 通道返回 markdown（非 HTML），必须用 md 版解析器，否则静默 0 条
    "imooc_md_list": parse_imooc_page,
    # 首页/体系课/免费课：一站三内链（/class/N、/sale/<slug>、/learn/N）
    "imooc_md_any": parse_imooc_md_any,
}
_DETAILS = {
    "imooc_detail": parse_imooc_detail, "quangneng_detail": parse_quangneng_detail,
}


def load_sources(path=None, create=True):
    p = Path(path) if path else SOURCES_PATH
    if not p.exists():
        if not create:
            raise FileNotFoundError(str(p))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_SOURCES, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return dict(DEFAULT_SOURCES)
    data = json.loads(p.read_text(encoding="utf-8"))
    return validate_sources(data)


def validate_sources(data):
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError("sources.json 结构非法")
    ids = set()
    for s in data["sources"]:
        for k in ("id", "name", "channel", "parser"):
            if not s.get(k):
                raise ValueError("源缺字段 %s: %r" % (k, s))
        if s["id"] in ids:
            raise ValueError("源 id 重复: %s" % s["id"])
        ids.add(s["id"])
        if s["parser"] not in _PARSERS:
            raise ValueError("未知 parser: %s（可选 %s）" % (s["parser"], list(_PARSERS)))
        pbc = s.get("parser_by_channel") or {}
        if not isinstance(pbc, dict):
            raise ValueError("parser_by_channel 须为对象: %r" % (pbc,))
        for ch, pn in pbc.items():
            if ch not in ("http", "jina", "browser"):
                raise ValueError("parser_by_channel 未知通道: %s" % ch)
            if pn not in _PARSERS:
                raise ValueError("parser_by_channel 未知 parser: %s" % pn)
        for ch in s["channel"]:
            if ch not in ("http", "jina", "browser"):
                raise ValueError("未知通道: %s" % ch)
        if s.get("kind", "course") != "course":
            raise ValueError("当前仅支持 kind=course")
    return data


def collect_source(src, max_urls=None, with_detail=False, min_bytes=200, sleep=2.0):
    """按源声明抓取：逐 URL 走通道链 → parser 解析 → 按 source_id 去重。

    返回 {source, records, per_url:[{url,mode,errs,items}], errors:[...]}
    """
    parser = _PARSERS[src["parser"]]
    by_ch = {k: _PARSERS[v] for k, v in (src.get("parser_by_channel") or {}).items()}

    def parser_for(mode):
        if mode == "cache":
            cm = (cache_get(cur_url, max_hours=0)[1] or {}).get("mode") or ""
            if cm in by_ch:
                return by_ch[cm]
        return by_ch.get(mode, parser)

    urls = list(src.get("urls") or [])
    if max_urls:
        urls = urls[: int(max_urls)]
    recs, seen, per_url, errors = [], set(), [], []
    for u in urls:
        cur_url = u
        item = {"url": u, "mode": None, "errs": []}
        try:
            # 多通道源也必须逐通道用「该通道的解析器」做 0 条探针，否则首通道
            # 返回异形态正文时 items=0 而 mode 非空 = 静默 0 条（本批修的真缺陷）。
            mode, html, errs = fetch_one(u, prefer=src.get("channel"),
                                         min_bytes=min_bytes, parser=parser,
                                         parser_map=by_ch)
            item["mode"] = mode
            item["errs"] = errs
            if mode == "cache":
                item["stale"] = True
                item["cache_age_hours"] = (cache_get(u, max_hours=0)[1] or {}).get("age_hours")
            items = parser_for(mode)(html)
            fresh = []
            for r in items:
                sid = str(r.get("source_id") or "").strip()
                key = sid or (r.get("url") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                r["source_id"] = sid or key
                fresh.append(r)
            item["items"] = len(fresh)
            recs.extend(fresh)
        except Exception as e:
            item["errs"] = [{"mode": "*", "err": str(e)[:200]}]
            errors.append({"url": u, "err": str(e)[:200]})
        per_url.append(item)
        if u is not urls[-1]:
            time.sleep(max(0.0, float(sleep)))
    if with_detail and src.get("detail") in _DETAILS:
        dparser = _DETAILS[src["detail"]]
        for r in recs:
            try:
                mode, html, _ = fetch_one(r.get("url") or "", prefer=src.get("channel"),
                                          min_bytes=min_bytes)
                r.update(dparser(html, r) or {})
            except Exception as e:
                errors.append({"url": r.get("url"), "err": "detail:" + str(e)[:180]})
            time.sleep(max(0.0, float(sleep) / 2))
    for r in recs:
        t = r.get("tags")
        if isinstance(t, str):
            r["tags"] = [x.strip() for x in re.split(r"[,，/\s]+", t) if x.strip()]
        elif not isinstance(t, list):
            r["tags"] = []
    return {"source": src["id"], "name": src.get("name") or src["id"],
            "records": recs, "per_url": per_url, "errors": errors,
            "channel": src.get("channel")}


def sources_summary(data=None):
    data = data or load_sources()
    out = []
    for s in data["sources"]:
        out.append({"id": s["id"], "name": s.get("name"), "enabled": bool(s.get("enabled", True)),
                    "channel": s.get("channel"), "parser": s["parser"],
                    "urls": len(s.get("urls") or []), "note": s.get("note") or ""})
    return out


def save_sources(data, path=None):
    p = Path(path) if path else SOURCES_PATH
    validate_sources(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return str(p)


def add_source(data, sid, name, urls, channel=("http", "browser"), parser="quangneng_list",
               detail="", kind="course", note="", enabled=True, force=False):
    ids = {s["id"] for s in data["sources"]}
    if sid in ids and not force:
        raise ValueError("源已存在: %s（要覆盖用 --force）" % sid)
    if parser not in _PARSERS:
        raise ValueError("未知 parser: %s（可选 %s）" % (parser, list(_PARSERS)))
    ent = {"id": sid, "name": name, "enabled": bool(enabled),
           "channel": list(channel), "parser": parser, "detail": detail,
           "kind": kind, "note": note, "urls": list(urls)}
    data["sources"] = [s for s in data["sources"] if s["id"] != sid] + [ent]
    validate_sources(data)
    return ent


def add_urls(data, sid, urls, dedupe=True):
    for s in data["sources"]:
        if s["id"] == sid:
            cur = list(s.get("urls") or [])
            new = [u for u in urls if not (dedupe and u in cur)]
            s["urls"] = cur + new
            validate_sources(data)
            return {"source": sid, "added": len(new), "total": len(s["urls"]), "new": new}
    raise KeyError("源不存在: %s" % sid)


def remove_urls(data, sid, urls):
    for s in data["sources"]:
        if s["id"] == sid:
            before = len(s.get("urls") or [])
            s["urls"] = [u for u in (s.get("urls") or []) if u not in set(urls)]
            validate_sources(data)
            return {"source": sid, "removed": before - len(s["urls"]),
                    "total": len(s["urls"])}
    raise KeyError("源不存在: %s" % sid)


def cmd_sources(json_out=False):
    """列出注册表里的采集源（只读）。"""
    data = load_sources()
    return sources_summary(data)


def save_fetch_log(info: dict, fetched: int, dry_run: bool = False):
    """把一次 fetch 的采集面摘要落盘（看板 render 读）。dry-run 一律不落（负控 T69）。"""
    if dry_run:
        return None
    srcs = {}
    for sid, v in (info or {}).items():
        per = []
        for u in (v.get("per_url") or []):
            rec = u.get("records")
            n_rec = rec if isinstance(rec, int) else len(rec or [])
            per.append({"url": u.get("url"), "mode": u.get("mode"),
                        "stale": bool(u.get("stale")), "records": n_rec})
        srcs[sid] = {"name": v.get("name"), "channel": v.get("channel"),
                     "records": int(v.get("records") or 0),
                     "stale": bool(v.get("stale")), "per_url": per,
                     "upsert": v.get("upsert"), "errors": v.get("errors") or []}
    doc = {"schema": "course.fetch_log/v1", "ts": time.time(),
           "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
           "fetched": int(fetched), "sources": srcs}
    FETCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    FETCH_LOG.write_text(_json.dumps(doc, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return FETCH_LOG


def load_fetch_log(path=None) -> dict | None:
    """读采集面台账；缺失/坏格式一律返回 None（看板静默不上面板，负控 T68/T70）。"""
    p = Path(path) if path else FETCH_LOG
    try:
        d = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("schema") != "course.fetch_log/v1":
        return None
    if not isinstance(d.get("sources"), dict):
        return None
    return d


# ---------- 统一落库（platform.db） ----------
# 口径：课程 × 网盘资源 × 采集台账 全部落到一张 SQLite（默认 temp/course_pool/platform.db，
# build 幂等重建（写 .tmp 后原子替换），fetch/match 跑完自动重建；
# 路径可用 env COURSE_DB 覆盖（自测沙箱走它，绝不碰生产库）。
DB_PATH = Path(os.environ.get("COURSE_DB") or BASE / "temp/course_pool/platform.db")
# 该落点在 .gitignore 内（temp/**）；env COURSE_DB 覆盖，自测/演练一律指沙箱。


def _md5(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()


def db_build(db_path=None):
    """从各数据面源幂等重建 platform.db。只读源、不回写源。返回计数 dict。

    源 schema（实测）：
      pool.json      : {version, updated_at, courses:[{id,source,title,url,students,...}]}
      user_state.json: {courses:{cid:{status,star,...}}, resources:[{course_id,pan_url,title,via,found_at}]}
      haisou_seen    : {url: {title, platform, seen_at, score, ...}}
      pan raw_*.json : {关键词: [{kw,title,pan,url,code,datetime,channel,note,topic}]}
                       或 [{...}] / {items:[...]}
      pan_seen.json  : {url: 首见时间}
      sources.json   : {sources:[{id,name,enabled,channel,kind,urls,...}]}
      *_keywords.json: {词: {added_at,found,source_url,published_at,...}}
      match_candidates.json: {..., matches:[{course_id,url,score,src,title}]}
      fetch_log.json : 单次运行 {schema,ts,ts_str,fetched,sources:{...}} 或列表
    """
    db = Path(db_path) if db_path else DB_PATH
    pool = load_pool()
    user = load_user()
    ucourses = user.get("courses") or {}

    courses = [c for c in (pool.get("courses") or []) if isinstance(c, dict)]
    resources = [r for r in (user.get("resources") or []) if isinstance(r, dict)]

    seen = {}
    if HAISOU_SEEN.exists():
        try:
            _s = json.loads(HAISOU_SEEN.read_text(encoding="utf-8"))
            seen = _s if isinstance(_s, dict) else {}
        except Exception:
            seen = {}

    hits = []
    import glob as _glob
    pat = str(PAN_RAW_GLOB)
    raw_files = sorted(_glob.glob(pat if os.path.isabs(pat) else str(BASE / pat)))
    for f in raw_files:
        f = Path(f)
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = []
        if isinstance(j, dict):
            if isinstance(j.get("items"), list):
                rows = j["items"]
            else:                       # 真实形态：{关键词: [行, ...]}
                for kw, arr in j.items():
                    if not isinstance(arr, list):
                        continue
                    for it in arr:
                        if isinstance(it, dict):
                            it = dict(it)
                            if not (it.get("kw") or it.get("keyword")):
                                it["kw"] = str(kw)
                            rows.append(it)
        elif isinstance(j, list):
            rows = j
        for it in rows:
            if isinstance(it, dict) and it.get("url"):
                it = dict(it)
                it["_src_file"] = f.name
                hits.append(it)

    pan_seen = {}
    if PAN_SEEN.exists():
        try:
            _ps = json.loads(PAN_SEEN.read_text(encoding="utf-8"))
            pan_seen = _ps if isinstance(_ps, dict) else {}
        except Exception:
            pan_seen = {}

    srcs = []
    try:
        srcs = [x for x in (load_sources(create=False).get("sources") or [])
                if isinstance(x, dict)]
    except Exception:
        srcs = []

    kws = {}
    for pool_name, path in (("pool", WORDS_OUT), ("quangneng", MERGE_INTO)):
        try:
            _k = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(_k, dict):
            for w, v in _k.items():
                v = v if isinstance(v, dict) else {}
                kws[str(w)] = {"_pool": pool_name, "added_at": v.get("added_at"),
                               "found": v.get("found"), "source_url": v.get("source_url"),
                               "published_at": v.get("published_at")}

    cand = []
    if CAND_OUT.exists():
        try:
            _c = json.loads(CAND_OUT.read_text(encoding="utf-8"))
        except Exception:
            _c = None
        if isinstance(_c, dict):
            cand = [x for x in (_c.get("matches") or _c.get("all_matches") or [])
                    if isinstance(x, dict)]
        elif isinstance(_c, list):
            cand = [x for x in _c if isinstance(x, dict)]

    fl = []
    if FETCH_LOG.exists():
        try:
            _f = json.loads(FETCH_LOG.read_text(encoding="utf-8"))
        except Exception:
            _f = None
        if isinstance(_f, dict):
            fl = [_f]
        elif isinstance(_f, list):
            fl = [x for x in _f if isinstance(x, dict)]

    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(db.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    try:
        con.executescript("""
        CREATE TABLE courses(id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT,
          url_md5 TEXT, students INTEGER, star INTEGER, status TEXT, updated_at TEXT,
          price REAL, level TEXT, category TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE resources(course_id TEXT, kind TEXT, url TEXT, url_md5 TEXT,
          title TEXT, via TEXT, src TEXT, found_at TEXT, PRIMARY KEY(course_id, url_md5));
        CREATE TABLE haisou_seen(url TEXT PRIMARY KEY, title TEXT, platform TEXT,
          seen_at TEXT, keywords TEXT, score REAL);
        CREATE TABLE pan_hits(url TEXT PRIMARY KEY, title TEXT, platform TEXT,
          keyword TEXT, src_file TEXT, found_at TEXT, code TEXT, channel TEXT,
          pan TEXT, note TEXT, topic TEXT);
        CREATE TABLE pan_seen(url TEXT PRIMARY KEY, first_seen TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, name TEXT, enabled INTEGER,
          channel TEXT, kind TEXT, urls TEXT, parser TEXT, note TEXT);
        CREATE TABLE keywords(word TEXT PRIMARY KEY, pool TEXT, added_at TEXT,
          found INTEGER, source_url TEXT, published_at TEXT);
        CREATE TABLE candidates(course_id TEXT, url TEXT, url_md5 TEXT, score REAL,
          src TEXT, title TEXT, PRIMARY KEY(course_id, url_md5));
        CREATE TABLE fetch_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
          fetched INTEGER, dry_run INTEGER, sources TEXT, raw TEXT);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        for c in courses:
            u = c.get("url") or ""
            cid = str(c.get("id") or "")
            uc = ucourses.get(cid) or {}
            star = uc.get("star", c.get("star", 0))
            status = uc.get("status", c.get("status", ""))
            con.execute("INSERT OR REPLACE INTO courses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                cid, str(c.get("source") or ""), str(c.get("title") or ""), u, _md5(u),
                int(c.get("students") or 0), int(star or 0), str(status or ""),
                str(uc.get("updated_at") or c.get("last_seen") or ""),
                float(c.get("price") or 0.0), str(c.get("level") or ""),
                str(c.get("category") or ""), str(c.get("first_seen") or ""),
                str(c.get("last_seen") or "")))
        for r in resources:
            u = r.get("pan_url") or r.get("url") or ""
            con.execute("INSERT OR REPLACE INTO resources VALUES(?,?,?,?,?,?,?,?)", (
                str(r.get("course_id") or ""), str(r.get("kind") or ""), u, _md5(u),
                str(r.get("title") or ""), str(r.get("via") or ""),
                str(r.get("src") or ""), str(r.get("found_at") or "")))
        for u, it in seen.items():
            it = it if isinstance(it, dict) else {}
            kw = it.get("keywords")
            con.execute("INSERT OR REPLACE INTO haisou_seen VALUES(?,?,?,?,?,?)", (
                str(u), str(it.get("title") or ""), str(it.get("platform") or ""),
                str(it.get("seen_at") or ""),
                json.dumps(kw, ensure_ascii=False) if isinstance(kw, list) else str(kw or ""),
                float(it.get("score") or 0.0)))
        for h in hits:
            u = h.get("url") or ""
            con.execute(
                "INSERT OR REPLACE INTO pan_hits VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    str(u), str(h.get("title") or ""),
                    str(h.get("platform") or h.get("pan") or ""),
                    str(h.get("keyword") or h.get("kw") or ""),
                    str(h.get("_src_file") or ""),
                    str(h.get("found_at") or h.get("datetime") or ""),
                    str(h.get("code") or ""), str(h.get("channel") or ""),
                    str(h.get("pan") or ""), str(h.get("note") or ""),
                    str(h.get("topic") or "")))
        for u, ts in pan_seen.items():
            if isinstance(ts, dict):
                ts = ts.get("first_seen") or ts.get("seen_at") or ""
            con.execute("INSERT OR REPLACE INTO pan_seen VALUES(?,?)",
                        (str(u), str(ts or "")))
        for s in srcs:
            con.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?)", (
                str(s.get("id") or ""), str(s.get("name") or ""),
                1 if s.get("enabled", True) else 0,
                json.dumps(s.get("channel") or [], ensure_ascii=False),
                str(s.get("kind") or "course"),
                json.dumps(s.get("urls") or [], ensure_ascii=False),
                str(s.get("parser") or ""), str(s.get("note") or "")))
        for w, v in kws.items():
            con.execute("INSERT OR REPLACE INTO keywords VALUES(?,?,?,?,?,?)", (
                w, str(v.get("_pool") or ""), str(v.get("added_at") or ""),
                1 if v.get("found") else 0, str(v.get("source_url") or ""),
                str(v.get("published_at") or "")))
        for c in cand:
            u = c.get("url") or ""
            con.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?)", (
                str(c.get("course_id") or ""), u, _md5(u),
                float(c.get("score") or 0.0), str(c.get("src") or ""),
                str(c.get("title") or "")))
        for e in fl:
            ts = str(e.get("ts_str") or e.get("ts") or "").strip()
            if not ts:
                continue
            con.execute("INSERT OR REPLACE INTO fetch_runs(id,ts,fetched,dry_run,sources,raw) "
                        "VALUES((SELECT id FROM fetch_runs WHERE ts=?),?,?,?,?,?)", (
                            ts, ts, int(e.get("fetched") or e.get("total") or 0),
                            1 if e.get("dry_run") else 0,
                            json.dumps(e.get("sources") or {}, ensure_ascii=False),
                            json.dumps(e, ensure_ascii=False)))
        con.execute("INSERT OR REPLACE INTO meta VALUES('built_at',?)", (now(),))
        con.execute("INSERT OR REPLACE INTO meta VALUES('schema',?)",
                    ("course_platform/v2",))
        con.commit()
    finally:
        con.close()
    os.replace(str(tmp), str(db))
    return db_stats(db)


def db_stats(db_path=None):
    """只读统计（不存在则 exists=False，不建库）。"""
    db = Path(db_path) if db_path else DB_PATH
    if not db.exists():
        return {"db": str(db), "exists": False}
    con = sqlite3.connect("file:%s?mode=ro" % str(db), uri=True)
    try:
        n = lambda q: con.execute(q).fetchone()[0]
        tbls = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        nt = lambda t: n("SELECT COUNT(*) FROM %s" % t) if t in tbls else 0
        out = {"db": str(db), "exists": True, "schema_tables": len(tbls),
               "courses": n("SELECT COUNT(*) FROM courses"),
               "resources": n("SELECT COUNT(*) FROM resources"),
               "courses_with_res": n("SELECT COUNT(DISTINCT course_id) FROM resources"),
               "haisou_seen": n("SELECT COUNT(*) FROM haisou_seen"),
               "pan_hits": n("SELECT COUNT(*) FROM pan_hits"),
               "candidates": n("SELECT COUNT(*) FROM candidates"),
               "fetch_runs": n("SELECT COUNT(*) FROM fetch_runs"),
               "sources": nt("sources"), "keywords": nt("keywords"),
               "pan_seen": nt("pan_seen"),
               "built_at": con.execute(
                   "SELECT v FROM meta WHERE k='built_at'").fetchone()[0]}
        return out
    finally:
        con.close()


def cmd_db(db_path=None):
    """`db` 子命令：重建并打印计数。"""
    return db_build(db_path)


def db_platform_overview():
    """给看板用的落库总览（库不存在返回 exists=False，绝不建库）。"""
    st = db_stats()
    if not st.get("exists"):
        return st
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(DB_PATH), uri=True)
        try:
            top = con.execute(
                "SELECT course_id, COUNT(*) c FROM resources GROUP BY course_id "
                "ORDER BY c DESC LIMIT 5").fetchall()
            st["top_courses"] = [{"course_id": a, "resources": b} for a, b in top]
            st["with_new"] = con.execute(
                "SELECT COUNT(*) FROM courses WHERE updated_at <> ''").fetchone()[0]
            tbls = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "pan_hits" in tbls:
                st["pan_by_platform"] = [
                    {"pan": a or "其他", "n": b} for a, b in con.execute(
                        "SELECT pan, COUNT(*) c FROM pan_hits GROUP BY pan "
                        "ORDER BY c DESC LIMIT 8").fetchall()]
                st["pan_top_kw"] = [
                    {"kw": a, "n": b} for a, b in con.execute(
                        "SELECT keyword, COUNT(*) c FROM pan_hits WHERE keyword<>'' "
                        "GROUP BY keyword ORDER BY c DESC LIMIT 8").fetchall()]
            if "keywords" in tbls:
                st["kw_pool"] = [{"pool": a or "?", "n": b} for a, b in con.execute(
                    "SELECT pool, COUNT(*) FROM keywords GROUP BY pool").fetchall()]
            if "sources" in tbls:
                st["sources_on"] = con.execute(
                    "SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
        finally:
            con.close()
    except Exception as e:
        st["overview_error"] = str(e)
    return st


def cmd_fetch(sid=None, max_urls=None, with_detail=False, dry_run=False,
              sleep=2.0, enabled_only=True):
    """按源抓取 → 解析 → 幂等入库。sid=None 表示抓所有启用源。"""
    data = load_sources()
    srcs = [x for x in data.get("sources", [])
            if (sid is None or x.get("id") == sid)
            and (x.get("enabled", True) or not enabled_only)]
    if sid and not srcs:
        raise KeyError("源不存在或未启用: %s" % sid)
    info, total = {}, 0
    for s in srcs:
        r = collect_source(s, max_urls=max_urls, with_detail=with_detail, sleep=sleep)
        n = len(r["records"])
        total += n
        stale = [x["url"] for x in r["per_url"] if x.get("stale")]
        info[s["id"]] = {"name": r.get("name"), "channel": r.get("channel"),
                         "per_url": r["per_url"], "records": n,
                         "stale_urls": stale, "stale": bool(stale),
                         "errors": r["errors"][:5]}
        if not dry_run and n:
            info[s["id"]]["upsert"] = upsert(s["id"], r["records"])
    n_stale = sum(1 for v in info.values() if v.get("stale"))
    log_path = save_fetch_log(info, total, dry_run)
    out = {"sources": info, "fetched": total, "dry_run": bool(dry_run),
           "stale_sources": n_stale,
           "stale_urls": sum(len(v.get("stale_urls") or []) for v in info.values())}
    if log_path:
        out["fetch_log"] = str(log_path)
    if not dry_run:
        try:                       # 落库失败不阻断采集链路，只留痕
            out["db"] = db_build()
        except Exception as e:
            out["db_error"] = str(e)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AI 课程资源台（课程池内核）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("fetch-imooc", help="抓慕课实战课入库")
    p.add_argument("--dirs", default=",".join(IMOOC_DIRS))
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("upsert", help="从 JSON 文件入库")
    p.add_argument("--source", required=True)
    p.add_argument("--in-file", dest="in_file", default="-")

    p = sub.add_parser("list")
    p.add_argument("--kw", default="")
    p.add_argument("--status", default="")
    p.add_argument("--source", default="")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("db", help="重建统一落库 platform.db 并打印计数")
    p.add_argument("--db", default="", help="覆盖库路径（默认 %s）" % DB_PATH)

    p = sub.add_parser("star"); p.add_argument("course_id"); p.add_argument("n", type=int)
    p.add_argument("--note", default="")
    p = sub.add_parser("status"); p.add_argument("course_id"); p.add_argument("status")
    p = sub.add_parser("attach"); p.add_argument("course_id"); p.add_argument("pan_url")
    p.add_argument("--title", default=""); p.add_argument("--via", default="")

    p = sub.add_parser("words")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--out", default="")
    p.add_argument("--merge-into", dest="merge_into", default="",
                   help="合并进 haisou 动态词表（默认 birdie_monitor/quangneng_keywords.json）")
    p.add_argument("--no-merge", action="store_true", help="只写 pool_keywords.json，不并入海搜词表")

    sub.add_parser("stats")
    p = sub.add_parser("render"); p.add_argument("--out", default="")
    p.add_argument("--fetch-log", dest="fetch_log", default="",
                   help=f"采集面台账路径（默认 {FETCH_LOG}；不存在则跳过面板）")
    p.add_argument("--no-fetch-log", dest="no_fetch_log", action="store_true",
                   help="不渲染「采集面」面板（负控/隔离用）")
    p.add_argument("--candidates", default="",
                   help=f"候选快照路径（默认 {CAND_OUT}；不存在则跳过面板）")
    p.add_argument("--no-candidates", dest="no_candidates", action="store_true",
                   help="不渲染「网盘命中候选」面板")
    p.add_argument("--no-db", dest="no_db", action="store_true",
                   help="不渲染「统一落库」面板（负控/隔离用）")
    p = sub.add_parser("sources", help="列出跨源采集注册表")
    p.add_argument("--json", dest="as_json", action="store_true")

    p = sub.add_parser("fetch", help="按注册表跨源抓取并入库")
    p.add_argument("--source", default="", help="只抓某个源 id（默认全部启用源）")
    p.add_argument("--max-urls", type=int, default=0)
    p.add_argument("--detail", action="store_true", help="逐条抓详情页")
    p.add_argument("--sleep", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-disabled", action="store_true")

    p = sub.add_parser("add-source", help="新增/覆盖采集源")
    p.add_argument("id"); p.add_argument("--name", default="")
    p.add_argument("--url", action="append", default=[], help="可重复")
    p.add_argument("--parser", default="quangneng_list")
    p.add_argument("--detail", default="")
    p.add_argument("--channel", default="http,browser")
    p.add_argument("--kind", default="course")
    p.add_argument("--note", default="")
    p.add_argument("--disabled", action="store_true")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("add-url", help="给源追加 URL")
    p.add_argument("id"); p.add_argument("urls", nargs="+")

    p = sub.add_parser("remove-url", help="从源移除 URL")
    p.add_argument("id"); p.add_argument("urls", nargs="+")

    p = sub.add_parser("match", help="把海搜/盘搜命中按标题相似度挂到课程（默认 dry-run）")
    p.add_argument("--min-score", dest="min_score", type=float, default=MATCH_MIN_SCORE)
    p.add_argument("--max-per-course", dest="max_per_course", type=int,
                   default=MATCH_MAX_PER_COURSE)
    p.add_argument("--hits", default="", help="显式命中 JSON（默认读海搜 seen + 盘搜 raw）")
    p.add_argument("--no-pan", dest="no_pan", action="store_true", help="不读盘搜 raw 命中")
    p.add_argument("--include-found", dest="include_found", action="store_true",
                   help="已挂过资源的课程也参与匹配（默认只补空缺）")
    p.add_argument("--limit", type=int, default=20, help="只列前 N 条候选明细")
    p.add_argument("--apply", action="store_true", help="真正写入 user_state.json（默认只预览）")
    p.add_argument("--json-out", dest="json_out", default="",
                   help=f"把全量候选写成快照给看板读（建议 {CAND_OUT}）")

    sub.add_parser("selftest")

    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "sources":
        rows = cmd_sources()
        if a.as_json:
            _print(rows)
        else:
            for r in rows:
                print("%-14s %-4s %-18s %-18s urls=%-3d %s" % (
                    r["id"], "on" if r["enabled"] else "off",
                    ",".join(r["channel"] or []), r["parser"], r["urls"], r["note"]))
        return 0
    if a.cmd == "fetch":
        _print(cmd_fetch(a.source or None, a.max_urls or None, a.detail, a.dry_run,
                         a.sleep, not a.include_disabled))
        return 0
    if a.cmd == "add-source":
        data = load_sources()
        ent = add_source(data, a.id, a.name or a.id, a.url,
                         tuple(x.strip() for x in a.channel.split(",") if x.strip()),
                         a.parser, a.detail, a.kind, a.note, not a.disabled, a.force)
        path = save_sources(data)
        _print({"source": ent["id"], "written": path}); return 0
    if a.cmd == "add-url":
        data = load_sources(); res = add_urls(data, a.id, a.urls)
        save_sources(data); _print(res); return 0
    if a.cmd == "remove-url":
        data = load_sources(); res = remove_urls(data, a.id, a.urls)
        save_sources(data); _print(res); return 0
    if a.cmd == "fetch-imooc":
        dirs = tuple(x.strip() for x in a.dirs.split(",") if x.strip())
        res = collect_imooc(dirs=dirs, max_pages=a.max_pages)
        recs = res["records"]
        info = {"per_dir": res["per_dir"], "records": len(recs)}
        if not a.dry_run and recs:
            info["upsert"] = upsert("imooc", recs)
        _print(info)
        return 0
    if a.cmd == "upsert":
        _print(cmd_upsert_file(a.source, a.in_file)); return 0
    if a.cmd == "words":
        if a.no_merge:
            target = None
        else:
            target = Path(a.merge_into) if a.merge_into else MERGE_INTO
        _print(emit_words(a.limit, a.out or None, target)); return 0
    if a.cmd == "db":
        _print(cmd_db(a.db or None)); return 0
    if a.cmd == "star":
        _print(set_star(a.course_id, a.n, a.note)); return 0
    if a.cmd == "status":
        _print(set_status(a.course_id, a.status)); return 0
    if a.cmd == "attach":
        _print(attach_resource(a.course_id, a.pan_url, a.title, a.via)); return 0
    if a.cmd == "match":
        _print(cmd_match(a.min_score, a.max_per_course, a.apply, a.hits,
                         not a.no_pan, a.limit, a.include_found, a.json_out))
        return 0
    merged = apply_user(load_pool(), load_user())
    if a.cmd == "list":
        for c in cmd_list(merged, a.kw, a.status, a.source, a.limit):
            print(f'{c["id"]:>16}  ★{c.get("star",0)} {c.get("status","")[:4]:<5} '
                  f'{c.get("students",0):>5}人  {c.get("title","")[:60]}')
        return 0
    if a.cmd == "stats":
        _print(stats(merged)); return 0
    if a.cmd == "render":
        cands, warn = (None, None)
        if not a.no_candidates:
            cands, warn = load_candidates(Path(a.candidates) if a.candidates else CAND_OUT,
                                          explicit=bool(a.candidates))
        flock = None if a.no_fetch_log else load_fetch_log(
            Path(a.fetch_log) if a.fetch_log else FETCH_LOG)
        dbst = None if a.no_db else db_platform_overview()
        html = render_html(merged, cands=cands, flock=flock, dbst=dbst)
        out = Path(a.out) if a.out else DASH_OUT
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        res = {"out": str(out), "bytes": len(html.encode("utf-8"))}
        if cands is not None:
            res["candidates"] = len(cands.get("all_matches") or [])
        if warn:
            res["candidates_warn"] = warn
        if dbst is not None:
            res["db"] = {k: dbst.get(k) for k in ("exists", "schema_tables", "courses",
                                                  "resources", "courses_with_res",
                                                  "haisou_seen", "pan_hits", "pan_seen",
                                                  "keywords", "sources", "candidates",
                                                  "fetch_runs")
                         if k in dbst}
        if flock is not None:
            res["fetch_log"] = {"ts": flock.get("ts_str"),
                               "sources": len(flock.get("sources") or {})}
            res["stale_sources"] = sum(1 for v in (flock.get("sources") or {}).values()
                                       if v.get("stale"))
        _print(res)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
