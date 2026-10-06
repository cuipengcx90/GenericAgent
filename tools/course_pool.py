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
  render [--out path]                   渲染看板（默认 archive/courses/index.html，8899 静态站直接可见）
  stats
  selftest                              内置自测（负控+夹具，不触碰生产数据）
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
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
MATCH_MIN_SCORE = 0.62          # 标题 bigram 覆盖率阈值（低于此不挂，宁缺勿滥）
MATCH_MAX_PER_COURSE = 3        # 单课程最多挂几条命中
MATCH_EXTRA_RX = re.compile(r"(网盘|资源|下载|课程|教程|合集|打包|分享|学习资料)")

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


def render_html(merged: dict, st: dict | None = None) -> str:
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
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>AI 课程资源台</title>
<style>
body{{font:14px/1.5 -apple-system,"PingFang SC",sans-serif;background:#111;color:#ddd;margin:24px}}
h1{{font-size:20px}} .chip{{background:#222;border:1px solid #333;border-radius:12px;padding:2px 10px;margin-right:6px}}
table{{border-collapse:collapse;width:100%;margin-top:12px}} td,th{{border-bottom:1px solid #262626;padding:6px 8px;text-align:left}}
a{{color:#6cf;text-decoration:none}} .n{{color:#e33;font-size:11px}} .d{{color:#a83;font-size:11px}}
input{{background:#1b1b1b;color:#ddd;border:1px solid #333;border-radius:6px;padding:6px 10px;width:280px}}
</style>
<h1>AI 课程资源台 <span class=chip>共 {st['total']}</span>{chips}
<span class=chip>新课 {st['new']}</span><span class=chip>已有资源 {st['with_resource']}</span>
<span class=chip>跨源重复组 {st['dup_groups']}</span></h1>
<input id=q placeholder="过滤：标题/来源/难度" oninput="f()">
<table id=t><thead><tr><th>标记</th><th>课程</th><th>来源</th><th>难度</th><th>报名</th><th>价格</th><th>资源</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p style="color:#666">更新：{_esc(st.get('updated_at'))}（本页由 tools/course_pool.py 生成，只读视图）</p>
<script>function f(){{var v=q.value.toLowerCase();
for(var r of t.tBodies[0].rows) r.style.display = r.innerText.toLowerCase().includes(v)?'':'none';}}</script>
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


def cmd_match(min_score: float = MATCH_MIN_SCORE, max_per_course: int = MATCH_MAX_PER_COURSE,
              apply: bool = False, hits_path: str = "", include_pan: bool = True,
              limit: int = 20, include_found: bool = False) -> dict:
    """把搜索命中按标题相似度挂到课程上（默认 dry-run，只预览不写盘）。

    策略：每个课程取分数最高的前 max_per_course 条命中；同一 (course_id, pan_url)
    已挂过即跳过（幂等）；默认跳过「已有资源」的课程（只补空缺），--include-found 可放开。
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
    return {"hits": len(hits), "courses": len(merged.get("courses", [])),
            "min_score": min_score, "apply": bool(apply),
            "applied": applied, "skipped_existing": skipped,
            "candidates": len(cands), "matches": cands[:max(0, int(limit))]}


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

    with tempfile.TemporaryDirectory() as td:
        old = (POOL_PATH, USER_PATH, WORDS_OUT)
        globals()["POOL_PATH"] = Path(td) / "pool.json"
        globals()["USER_PATH"] = Path(td) / "user.json"
        globals()["WORDS_OUT"] = Path(td) / "words.json"
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
    finally:
        globals()["POOL_PATH"], globals()["USER_PATH"] = old2

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
    """jina 通道：r.jina.ai 渲染取文（部分站点可用；远端 401/超时时自动降级到 browser）。"""
    return _http_text("https://r.jina.ai/" + url, timeout=timeout)


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
            return mode, last_html, errs
        except Exception as e:
            errs.append({"mode": mode, "err": str(e)[:200]})
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
        return by_ch.get(mode, parser)

    urls = list(src.get("urls") or [])
    if max_urls:
        urls = urls[: int(max_urls)]
    recs, seen, per_url, errors = [], set(), [], []
    for u in urls:
        item = {"url": u, "mode": None, "errs": []}
        try:
            # 多通道源也必须逐通道用「该通道的解析器」做 0 条探针，否则首通道
            # 返回异形态正文时 items=0 而 mode 非空 = 静默 0 条（本批修的真缺陷）。
            mode, html, errs = fetch_one(u, prefer=src.get("channel"),
                                         min_bytes=min_bytes, parser=parser,
                                         parser_map=by_ch)
            item["mode"] = mode
            item["errs"] = errs
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
        info[s["id"]] = {"name": r.get("name"), "channel": r.get("channel"),
                         "per_url": r["per_url"], "records": n,
                         "errors": r["errors"][:5]}
        if not dry_run and n:
            info[s["id"]]["upsert"] = upsert(s["id"], r["records"])
    return {"sources": info, "fetched": total, "dry_run": bool(dry_run)}


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
    if a.cmd == "star":
        _print(set_star(a.course_id, a.n, a.note)); return 0
    if a.cmd == "status":
        _print(set_status(a.course_id, a.status)); return 0
    if a.cmd == "attach":
        _print(attach_resource(a.course_id, a.pan_url, a.title, a.via)); return 0
    if a.cmd == "match":
        _print(cmd_match(a.min_score, a.max_per_course, a.apply, a.hits,
                         not a.no_pan, a.limit, a.include_found))
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
        html = render_html(merged)
        out = Path(a.out) if a.out else DASH_OUT
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        _print({"out": str(out), "bytes": len(html.encode("utf-8"))})
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
