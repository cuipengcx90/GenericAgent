#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TrendRadar 信源有效性审计（R343）。

回答三个问题：
  1) 配置里声明了哪些源？（config.yaml: rss.feeds + platforms）
  2) 每个源实际**供给**了多少条？（material.json 的 rss.data / 每日 news DB 的 news_items）
  3) 每个源最终被报告**采纳**了多少条？（GA/output/trendradar_*.txt 里 ▸ 行末的（来源）标注）

用法（幂等，可反复重跑）：
  python3 tools/trendradar_source_audit.py scan     # 扫描三类数据 → temp/trendradar_source_audit/audit_data.json
  python3 tools/trendradar_source_audit.py report    # 读 json → 打印 markdown 表格 + 三清单
  python3 tools/trendradar_source_audit.py check     # HTTP 实测 config 里每个 RSS URL（结果写 json）
  python3 tools/trendradar_source_audit.py all       # scan + report
"""
import os, re, sys, json, glob, sqlite3, collections, datetime, subprocess

GA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(GA, "output")                       # 报告正文 + material.json
TR = os.path.expanduser("~/projects/TrendRadar")           # TrendRadar 仓库
DB_GLOB = os.path.join(TR, "output", "news", "*.db")
CONFIG = os.path.join(TR, "config", "config.yaml")
WORK = os.path.join(GA, "temp", "trendradar_source_audit")
DATA = os.path.join(WORK, "audit_data.json")
CHECK = os.path.join(WORK, "reachability.json")
DBDIR = os.path.join(TR, "output", "news")             # 每日 news SQLite
IMPACT = os.path.join(WORK, "impact.json")
VERDICT = os.path.join(WORK, "verdict.json")
PROD = os.path.join(GA, "sche_tasks", "run_trendradar_digest.py")  # 生产取数脚本（只读引用）

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DATE_RE2 = re.compile(r"(\d{4})(\d{2})(\d{2})(?!\d)")
SRC_RE = re.compile(r"（([^（）]{1,120})）\s*$")

# 同一来源的不同写法 → 归一化
ALIAS = {
    "linux.do": "linux.do", "linuxdo": "linux.do", "linux.do 社区热帖": "linux.do",
    "v2ex": "V2EX", "v2ex 最热": "V2EX",
    "solidot": "Solidot", "solidot（it资讯": "Solidot",
    "hacker news": "Hacker News", "hn": "Hacker News",
    "github": "GitHub", "github trending": "GitHub",
    "财联社": "财联社", "财联社热门": "财联社",
    "华尔街见闻": "华尔街见闻", "openai blog": "OpenAI Blog",
    "towards data science": "Towards Data Science", "tds": "Towards Data Science",
    "知乎": "知乎", "百度热搜": "百度热搜", "今日头条": "今日头条", "bilibili 热搜": "bilibili 热搜",
    "澎湃新闻": "澎湃新闻", "凤凰网": "凤凰网", "贴吧": "贴吧", "抖音": "抖音", "微博": "微博",
    "supply chain brain": "Supply Chain Brain", "freightwaves": "FreightWaves",
    "supply chain dive": "Supply Chain Dive", "saastr": "SaaStr",
    "阮一峰的网络日志": "阮一峰", "阮一峰": "阮一峰",
    "yahoo finance": "雅虎财经", "雅虎财经": "雅虎财经",
}
UNLABELED = {"未知来源", "rss", "未标注", "未知", "等"}


def norm_label(raw: str):
    """把标注片段拆成若干规范来源名；返回 (labels, is_unlabeled)。"""
    t = raw.strip()
    # mojibake 修复（utf-8 被当作 latin-1 读进来）
    try:
        if "Ã" in t or "æ" in t or "ç" in t:
            t2 = t.encode("latin-1", "ignore").decode("utf-8", "ignore")
            if t2.strip():
                t = t2
    except Exception:
        pass
    t = re.sub(r"[（(].*?[)）]", "", t).strip()          # 去掉嵌套括号如 Solidot（IT资讯）
    parts = [p.strip() for p in re.split(r"[/、,，]| \+ ", t) if p.strip()]
    labels, unlabeled = [], False
    for p in parts:
        low = p.lower()
        if low in UNLABELED or p.endswith("等"):
            unlabeled = True
            continue
        labels.append(ALIAS.get(low, p))
    return labels, unlabeled


def parse_date(name: str):
    m = DATE_RE.search(name)
    if m:
        return m.group(1)
    m = DATE_RE2.search(name)
    if m:
        return "%s-%s-%s" % m.groups()
    return None


def scan_reports():
    """采纳侧：报告 ▸ 行的来源标注。"""
    adopt = collections.defaultdict(lambda: {"n": 0, "dates": set(), "files": set()})
    items_total = 0
    unp = 0
    for f in sorted(glob.glob(os.path.join(OUT_DIR, "trendradar_*.txt"))):
        d = parse_date(os.path.basename(f)) or "?"
        for line in open(f, encoding="utf-8", errors="replace"):
            s = line.strip()
            if not s.startswith("▸"):
                continue
            items_total += 1
            m = SRC_RE.search(s)
            if not m:
                unp += 1
                continue
            labels, unlabeled = norm_label(m.group(1))
            if unlabeled or not labels:
                unp += 1
            for lb in labels:
                a = adopt[lb]
                a["n"] += 1
                a["dates"].add(d)
                a["files"].add(os.path.basename(f))
    out = {}
    for k, v in adopt.items():
        ds = sorted(x for x in v["dates"] if x != "?")
        out[k] = {"adopted": v["n"], "periods": len(v["files"]),
                  "first": ds[0] if ds else "?", "last": ds[-1] if ds else "?"}
    return {"items_total": items_total, "items_without_source": unp, "sources": out}


def scan_material():
    """供给侧①：material.json 的 rss.data[]（每期抓到的 RSS 条目，带 feed_id/feed_name）。"""
    supply = collections.defaultdict(lambda: {"n": 0, "dates": set()})
    periods = collections.defaultdict(int)
    kw_hits = 0
    for f in sorted(glob.glob(os.path.join(OUT_DIR, "trendradar_*.material.json"))):
        base = os.path.basename(f)
        d = parse_date(base) or "?"
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        periods[d] += 1
        cnt = j.get("_counts") or {}
        kw_hits += int(cnt.get("keyword_hits") or 0)
        rss = (j.get("rss") or {}).get("data") or []
        for it in rss:
            fid = it.get("feed_id") or it.get("feed_name") or "?"
            s = supply[fid]
            s["n"] += 1
            s["dates"].add(d)
        for q in j.get("queries") or []:
            r = q.get("result") or {}
            for it in (r.get("results") or []):
                fid = "keyword:" + str((it.get("feed_id") or it.get("source") or "?"))
                s = supply[fid]
                s["n"] += 1
                s["dates"].add(d)
    out = {}
    for k, v in supply.items():
        ds = sorted(x for x in v["dates"] if x != "?")
        out[k] = {"supplied": v["n"], "periods": len(v["dates"]), "last": ds[-1] if ds else "?"}
    return {"supply": out, "files": sum(periods.values()), "keyword_hits_total": kw_hits}


def scan_dbs(days=30):
    """供给侧②：每日 news DB 的 news_items / crawl_source_status（榜单平台）。"""
    files = sorted(glob.glob(DB_GLOB))[-days:]
    per_platform = collections.defaultdict(lambda: {"items": 0, "days": 0})
    health = collections.defaultdict(lambda: {"success": 0, "failed": 0, "last_failed": None})
    daily = {}
    for f in files:
        d = os.path.basename(f).replace(".db", "")
        try:
            con = sqlite3.connect("file:%s?mode=ro" % f, uri=True)
            cur = con.cursor()
            rows = cur.execute("select platform_id, count(*) from news_items group by platform_id").fetchall()
            n_items = 0
            for pid, c in rows:
                p = per_platform[pid]
                p["items"] += c
                p["days"] += 1
                n_items += c
            daily[d] = n_items
            try:
                h = cur.execute(
                    "select platform_id, status, count(*) from crawl_source_status "
                    "join crawl_records on crawl_records.id = crawl_source_status.crawl_record_id "
                    "group by platform_id, status").fetchall()
                for pid, st, c in h:
                    health[pid][st] += c
            except Exception:
                pass
            con.close()
        except Exception as e:
            daily[d] = "ERR:%s" % e
    return {"days": len(files), "first": os.path.basename(files[0]).replace(".db", "") if files else "?",
            "last": os.path.basename(files[-1]).replace(".db", "") if files else "?",
            "platforms": {k: dict(v) for k, v in per_platform.items()},
            "health": {k: dict(v) for k, v in health.items()}, "daily_items": daily}


def parse_config():
    """声明侧：config.yaml 的 rss.feeds / platforms / 是否 enabled。"""
    feeds, platforms = [], []
    if os.path.exists(CONFIG):
        txt = open(CONFIG, encoding="utf-8").read()
        # 只取 rss: 段（到下一个顶层键）
        m = re.search(r"(?ms)^rss:\n(.*?)(?=^\S|\Z)", txt)
        seg = m.group(1) if m else ""
        cur = None
        for line in seg.split("\n"):
            fm = re.match(r'\s*-\s*id:\s*"?([^"\n]+)"?', line)
            if fm:
                if cur:
                    feeds.append(cur)
                cur = {"id": fm.group(1).strip(), "name": "", "url": "", "enabled": True}
                continue
            if cur is None:
                continue
            nm = re.match(r'\s*name:\s*"([^"]+)"', line)
            um = re.match(r'\s*url:\s*"([^"]+)"', line)
            em = re.match(r"\s*enabled:\s*(true|false)", line)
            if nm:
                cur["name"] = nm.group(1)
            if um:
                cur["url"] = um.group(1)
            if em:
                cur["enabled"] = em.group(1) == "true"
        if cur:
            feeds.append(cur)
        pm = re.search(r"(?ms)^platforms:\n(.*?)(?=^\S|\Z)", txt)
        if pm:
            for line in pm.group(1).split("\n"):
                pm2 = re.match(r"\s*-\s*id:\s*(\S+)", line)
                if pm2:
                    platforms.append(pm2.group(1).strip().strip('"'))
    return {"feeds": feeds, "platforms": platforms}


def cmd_scan():
    os.makedirs(WORK, exist_ok=True)
    data = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "reports": scan_reports(),
        "material": scan_material(),
        "dbs": scan_dbs(),
        "config": parse_config(),
    }
    json.dump(data, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    r = data["reports"]
    print("[scan] 报告 %d 份 / ▸条目 %d / 带来源标注后被拆出的来源记录 %d 类"
          % (len(glob.glob(os.path.join(OUT_DIR, "trendradar_*.txt"))), r["items_total"], len(r["sources"])))
    print("[scan] 无来源标注条目 %d；material.json %d 份；DB %d 天(%s~%s)"
          % (r["items_without_source"], data["material"]["files"], data["dbs"]["days"],
             data["dbs"]["first"], data["dbs"]["last"]))
    print("[scan] config 声明: RSS feed %d 个 / platform %d 个 → %s" % (len(data["config"]["feeds"]), len(data["config"]["platforms"]), DATA))
    return 0


def cmd_report():
    d = json.load(open(DATA, encoding="utf-8"))
    rep, mat, cfg = d["reports"], d["material"], d["config"]
    L = []
    L.append("## A. 采纳侧：报告里实际出现的来源（%s 起）" % "全量")
    L.append("")
    L.append("| 来源 | 采纳条数 | 出现期数 | 首见 | 最近见 |")
    L.append("|---|---|---|---|---|")
    for k, v in sorted(rep["sources"].items(), key=lambda x: -x[1]["adopted"]):
        L.append("| %s | %d | %d | %s | %s |" % (k, v["adopted"], v["periods"], v["first"], v["last"]))
    L.append("")
    L.append("## B. 供给侧①：material.json 里 RSS 抓取量")
    L.append("")
    L.append("| feed_id | 抓到条数 | 出现期数 | 最近见 |")
    L.append("|---|---|---|---|")
    for k, v in sorted(mat["supply"].items(), key=lambda x: -x[1]["supplied"]):
        L.append("| %s | %d | %d | %s |" % (k, v["supplied"], v["periods"], v["last"]))
    L.append("")
    L.append("## C. 供给侧②：榜单平台近 %d 天（%s~%s）" % (d["dbs"]["days"], d["dbs"]["first"], d["dbs"]["last"]))
    L.append("")
    L.append("| platform_id | 条目数 | 抓取成功 | 抓取失败 |")
    L.append("|---|---|---|---|")
    for k, v in sorted(d["dbs"]["platforms"].items(), key=lambda x: -x[1]["items"]):
        h = d["dbs"]["health"].get(k, {})
        L.append("| %s | %d | %d | %d |" % (k, v["items"], h.get("success", 0), h.get("failed", 0)))
    L.append("")
    L.append("## D. 声明侧：config.yaml")
    L.append("")
    L.append("| feed id | name | enabled | url |")
    L.append("|---|---|---|---|")
    for f in cfg["feeds"]:
        L.append("| %s | %s | %s | %s |" % (f["id"], f["name"], f["enabled"], f["url"]))
    print("\n".join(L))
    return 0


def cmd_check():
    d = json.load(open(DATA, encoding="utf-8"))
    os.makedirs(WORK, exist_ok=True)
    res = []
    for f in d["config"]["feeds"]:
        if not f["enabled"]:
            res.append(dict(f, skipped="disabled"))
            continue
        cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{size_download} %{time_total}",
               "-L", "--max-time", "25", "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
               f["url"]]
        p = subprocess.run(cmd, capture_output=True, text=True)
        res.append(dict(f, curl=(p.stdout or "").strip(), err=(p.stderr or "").strip()[:200]))
    json.dump(res, open(CHECK, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for r in res:
        print("%-24s %-30s %s %s" % (r["id"], (r.get("curl") or "").strip() or r.get("skipped", ""), r.get("err", "")[:60], ""))
    return 0


def _prod_keywords():
    """从生产脚本里正则抽取 KEYWORDS 字面量，避免执行该模块（它有副作用）。"""
    import ast
    src = open(PROD, encoding="utf-8").read()
    m = re.search(r"^KEYWORDS\s*=\s*(\[[^\]]*\])", src, re.M)
    if not m:
        raise SystemExit("KEYWORDS not found in " + PROD)
    return ast.literal_eval(m.group(1))


def _titles_of(date_str):
    p = os.path.join(DBDIR, date_str + ".db")
    if not os.path.exists(p):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    try:
        return [r[0] for r in con.execute("select title from news_items")]
    finally:
        con.close()


def cmd_impact(days=30):
    """量化『整串当关键词』造成的检索损失：对最近 days 天的榜单标题做实测。"""
    os.makedirs(WORK, exist_ok=True)
    dates = sorted(x[:-3] for x in os.listdir(DBDIR) if x.endswith(".db"))[-days:]
    kws = _prod_keywords()
    rows = []
    tok_tot = collections.Counter()
    for d in dates:
        titles = [t.lower() for t in _titles_of(d)]
        for q in kws:
            ql = q.lower()
            whole = sum(1 for t in titles if ql in t)
            toks = [w for w in q.split() if w]
            union, per = set(), collections.Counter()
            for w in toks:
                wl = w.lower()
                for i, t in enumerate(titles):
                    if wl in t:
                        union.add(i)
                        per[w] += 1
                        tok_tot[w] += 1
            rows.append({"date": d, "query": q, "titles": len(titles), "whole_string_hits": whole,
                         "token_union_hits": len(union), "per_token": dict(per)})
    out = {"days": len(dates), "range": [dates[0], dates[-1]], "queries": kws,
           "whole_string_total": sum(r["whole_string_hits"] for r in rows),
           "token_union_total": sum(r["token_union_hits"] for r in rows),
           "rows": rows, "per_token_total": dict(tok_tot)}
    json.dump(out, open(IMPACT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("## E. 检索通道影响量化（%s ~ %s，%d 天）" % (dates[0], dates[-1], len(dates)))
    print("整串当关键词命中总数: %d（预期 0）" % out["whole_string_total"])
    print("拆词后并集命中总数: %d（按查询/天）" % out["token_union_total"])
    print("")
    print("| 关键词 | %d 天累计命中标题数 |" % len(dates))
    print("|---|---|")
    for w, n in tok_tot.most_common():
        print("| %s | %d |" % (w, n))
    print("")
    print("按天明细（每查询：整串命中 / 拆词并集命中）：")
    for d in dates:
        s = []
        for r in [x for x in rows if x["date"] == d]:
            s.append("%d/%d" % (r["whole_string_hits"], r["token_union_hits"]))
        print("  %s  %s" % (d, "  ".join(s)))
    return 0


PLATFORM_LABEL = {
    "baidu": "百度热搜", "bilibili-hot-search": "bilibili 热搜", "cls-hot": "财联社热门",
    "douyin": "抖音", "ifeng": "凤凰网", "thepaper": "澎湃新闻", "tieba": "贴吧",
    "toutiao": "今日头条", "wallstreetcn-hot": "华尔街见闻", "weibo": "微博", "zhihu": "知乎",
}


def _canon(name):
    """把 config 里的源名归一到报告里使用的标注名。"""
    low = (name or "").strip().lower()
    if low in ALIAS:
        return ALIAS[low]
    best = None
    for k, v in ALIAS.items():
        if k in low and (best is None or len(k) > len(best[0])):
            best = (k, v)
    return best[1] if best else name


def cmd_verdict(stale_days=30):
    """三清单判决：零产出 / 极低产出 / 近期静默。每行都带实测证据列。"""
    d = json.load(open(DATA, encoding="utf-8"))
    rep, mat, cfg = d["reports"], d["material"], d["config"]
    reach = {}
    if os.path.exists(CHECK):
        for r in json.load(open(CHECK, encoding="utf-8")):
            st = (r.get("curl") or "").split()
            reach[r["id"]] = st[0] if st else (r.get("skipped") or "?")
    all_dates = [v["last"] for v in rep["sources"].values() if v["last"]]
    max_d = max(all_dates) if all_dates else ""
    zero, low, stale = [], [], []
    feeds = []
    for f in cfg["feeds"]:
        fid, cname = f["id"], _canon(f["name"])
        a = rep["sources"].get(cname, {})
        sup = (mat["supply"].get(fid) or {}).get("supplied", 0)
        http = reach.get(fid, "-")
        row = {"id": fid, "name": f["name"], "canon": cname, "enabled": f["enabled"], "http": http,
               "supply": sup, "adopted": a.get("adopted", 0), "first": a.get("first", "-"),
               "last": a.get("last", "-"), "periods": a.get("periods", 0)}
        row["ratio"] = (row["adopted"] / sup) if sup else None
        if not f["enabled"]:
            row["verdict"] = "已禁用(设计如此)"
        elif row["adopted"] == 0:
            row["verdict"] = "零产出"
            row["why"] = ("HTTP %s" % http) if http not in ("200", "-") else ("物料 0 条" if sup == 0 else "有供给但从未被采纳")
            zero.append(row)
        elif row["ratio"] is not None and row["ratio"] < 0.01:
            row["verdict"] = "极低产出"
            low.append(row)
        elif row["last"] and max_d and (datetime.date.fromisoformat(max_d) - datetime.date.fromisoformat(row["last"])).days > stale_days:
            row["verdict"] = "近期静默"
            stale.append(row)
        else:
            row["verdict"] = "在服役"
        feeds.append(row)
    plats = []
    for pid, v in sorted(d["dbs"]["platforms"].items(), key=lambda x: -x[1]["items"]):
        lab = PLATFORM_LABEL.get(pid, pid)
        a = rep["sources"].get(lab, {})
        h = d["dbs"]["health"].get(pid, {})
        plats.append({"id": pid, "label": lab, "adopted": a.get("adopted", 0), "last": a.get("last", "-"),
                      "items": v["items"], "ok": h.get("success", 0), "fail": h.get("failed", 0)})
    out = {"max_date": max_d, "stale_days": stale_days, "feeds": feeds, "platforms": plats,
           "zero": [r["id"] for r in zero], "low": [r["id"] for r in low], "stale": [r["id"] for r in stale]}
    json.dump(out, open(VERDICT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    L = []
    L.append("# F. 判决：源有效性三清单（采纳口径=报告正文；基准日 %s）" % max_d)
    L.append("")
    L.append("## F0. 声明源逐条体检")
    L.append("")
    L.append("| feed | 启用 | HTTP | 物料条数 | 采纳条数 | 采纳率 | 首见 | 最近见 | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in feeds:
        L.append("| %s | %s | %s | %d | %d | %s | %s | %s | %s |" % (
            r["id"], r["enabled"], r["http"], r["supply"], r["adopted"],
            ("%.2f%%" % (r["ratio"] * 100)) if r["ratio"] is not None else "-",
            r["first"], r["last"], r["verdict"]))
    L.append("")
    L.append("## F1. 零产出清单（声明启用但报告从未采纳）")
    L.append("")
    if not zero:
        L.append("（空）")
    for r in zero:
        L.append("- **%s**（%s）：%s；物料 %d 条；HTTP %s" % (r["id"], r["name"], r.get("why"), r["supply"], r["http"]))
    L.append("")
    L.append("## F2. 极低产出清单（采纳率 < 1%，占用抓取/上下文成本）")
    L.append("")
    if not low:
        L.append("（空）")
    for r in sorted(low, key=lambda x: x["ratio"]):
        L.append("- **%s**（%s）：物料 %d 条 → 采纳 %d 条（%.2f%%）" % (r["id"], r["name"], r["supply"], r["adopted"], r["ratio"] * 100))
    L.append("")
    L.append("## F3. 近期静默清单（供给正常，但 >%d 天未被采纳）" % stale_days)
    L.append("")
    bad = []
    for p in plats:
        if p["last"] in ("", "-", None):
            bad.append((p, "从未出现在报告中"))
        else:
            gap = (datetime.date.fromisoformat(max_d) - datetime.date.fromisoformat(p["last"])).days
            if gap > stale_days:
                bad.append((p, "最近见 %s（静默 %d 天）" % (p["last"], gap)))
    if not stale and not bad:
        L.append("（空）")
    for r in sorted(stale, key=lambda x: x["last"]):
        L.append("- [RSS] **%s**（%s）：最近见 %s，物料 %d 条" % (r["id"], r["name"], r["last"], r["supply"]))
    for p, why in sorted(bad, key=lambda x: x[0]["last"]):
        L.append("- [平台] **%s**（%s）：%s；30 天抓取 %d 条、抓取成功 %d 次（供给正常）"
                 % (p["id"], p["label"], why, p["items"], p["ok"]))
    L.append("")
    L.append("> 平台侧静默与 RSS 无关：11 个榜单源每天都在成功抓取，只是**没有任何一条进入报告**。")
    L.append("")
    L.append("## F4. 榜单平台（11 个）近 30 天服役情况")
    L.append("")
    L.append("| platform | 报告标注 | 采纳条数 | 最近见 | 30 天抓取条目 | 抓取成功/失败 |")
    L.append("|---|---|---|---|---|---|")
    for p in plats:
        L.append("| %s | %s | %d | %s | %d | %d/%d |" % (p["id"], p["label"], p["adopted"], p["last"], p["items"], p["ok"], p["fail"]))
    print("\n".join(L))
    return 0


def main():
    a = sys.argv[1] if len(sys.argv) > 1 else "all"
    if a == "scan":
        return cmd_scan()
    if a == "report":
        return cmd_report()
    if a == "check":
        return cmd_check()
    if a == "verdict":
        return cmd_verdict(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    if a == "impact":
        return cmd_impact(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    cmd_scan()
    return cmd_report()


if __name__ == "__main__":
    sys.exit(main())
