#!/usr/bin/env python3
"""每日技术开源趋势报告 - GitHub + HN + HuggingFace Papers"""
import sys, os, json, requests
from bs4 import BeautifulSoup
from datetime import datetime

sys.path.insert(0, '/Users/cuip/projects/GenericAgent')
from llmcore import NativeClaudeSession, mykeys
from reflect_scripts.reflect_common import notify_feishu
from reflect_scripts.notify_feishu_group import notify_group
from tools.lan_ip import archive_url

headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

def fetch_github(since="daily", limit=15):
    r = requests.get(f"https://github.com/trending?since={since}", headers=headers, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    repos = []
    for article in soup.select("article.Box-row")[:limit]:
        name_elem = article.select_one("h2 a")
        if not name_elem: continue
        name = name_elem.get("href", "").strip("/")
        desc = article.select_one("p")
        lang = article.select_one("[itemprop='programmingLanguage']")
        stars_elem = article.select_one("svg.octicon-star")
        today_elem = article.select("span.d-inline-block.float-sm-right")
        repos.append({
            "name": name,
            "url": f"https://github.com/{name}",
            "desc": desc.get_text(strip=True) if desc else "",
            "lang": lang.get_text(strip=True) if lang else "",
            "stars": stars_elem.parent.get_text(strip=True).replace(",","") if stars_elem else "0",
            "today": today_elem[-1].get_text(strip=True).split()[0].replace(",","") if today_elem else "0"
        })
    return repos

def fetch_hn(limit=20):
    ids = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10).json()
    items = []
    for hid in ids[:limit]:
        try:
            item = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{hid}.json", timeout=5).json()
            if item and item.get("title"):
                items.append({
                    "title": item.get("title",""),
                    "url": item.get("url", f"https://news.ycombinator.com/item?id={hid}"),
                    "score": item.get("score", 0),
                    "comments": item.get("descendants", 0)
                })
        except: pass
    return items

def fetch_hf(limit=15):
    r = requests.get("https://huggingface.co/api/daily_papers", timeout=10)
    result = []
    for p in r.json()[:limit]:
        paper = p.get("paper", {})
        result.append({
            "title": paper.get("title",""),
            "url": f"https://huggingface.co/papers/{paper.get('id','')}",
            "upvotes": p.get("upvotes", 0)
        })
    return result

def fetch_trendshift(limit=15):
    """从 trendshift.io 获取每日 engagement 排名的开源项目"""
    try:
        r = requests.get("https://trendshift.io/", headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.group.relative.flex.flex-col")
        repos = []
        for card in cards[:limit]:
            # 名称和链接
            link = card.find("a", href=lambda h: h and "/repositories/" in h)
            if not link:
                continue
            name = link.get_text(strip=True)
            if "/" not in name:
                continue
            href = link.get("href", "")
            # Stars: child[0] 中的数字文本
            header = card.find_all(recursive=False)[0] if card.find_all(recursive=False) else None
            stars = ""
            if header:
                texts = [t.strip() for t in header.get_text(separator="|||").split("|||") if t.strip()]
                # texts 格式: [name, stars_count, forks_or_delta]
                for t in texts[1:]:
                    if any(c.isdigit() for c in t):
                        stars = t
                        break
            # 描述
            desc_elem = card.select_one("p.text-muted-foreground")
            desc = desc_elem.get_text(strip=True) if desc_elem else ""
            # 标签
            tag_elems = card.select("a[href*='/topics/']")
            tags = [t.get_text(strip=True).lstrip("#").strip() for t in tag_elems]
            repos.append({
                "name": name,
                "url": f"https://trendshift.io{href}",
                "stars": stars,
                "desc": desc,
                "tags": ", ".join(tags) if tags else ""
            })
        return repos
    except Exception as e:
        print(f"⚠️ Trendshift 获取失败: {e}")
        return []

STATE_PATH = "/Users/cuip/projects/GenericAgent/output/.tech_trends_push_state.json"

def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _mark_sent(date_str):
    st = _load_state()
    st[date_str] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def run(force=False):
    # === 幂等闸门: 当日只推一次, 防重复推送 (2026-07-14 根治) ===
    date_str = datetime.now().strftime("%Y-%m-%d")
    lock_path = f"/tmp/tech_trends_{date_str}.lock"
    if not force:
        st = _load_state()
        if st.get(date_str):
            print(f"⏭️ 跳过: {date_str} 今日已推送 (at {st[date_str]}) — 防重复。如需强推用 run(force=True)")
            return {"status": "skipped", "reason": "already_sent_today", "date": date_str}
        # 并发锁: 同日同秒双启只放行一个
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            print(f"⏭️ 跳过: 已有同日进程持锁 {lock_path} — 防并发重复。如需强推用 run(force=True)")
            return {"status": "skipped", "reason": "locked", "date": date_str}

    print("📡 拉取数据...")
    gh_daily = fetch_github("daily", 15)
    gh_weekly = fetch_github("weekly", 10)
    hn_items = fetch_hn(20)
    hf_papers = fetch_hf(15)
    ts_repos = fetch_trendshift(15)
    print(f"✅ GitHub日榜{len(gh_daily)}条, 周榜{len(gh_weekly)}条, HN{len(hn_items)}条, HF{len(hf_papers)}条, Trendshift{len(ts_repos)}条")

    date_str = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt = f"""你是技术趋势分析专家。基于以下数据生成中文报告。

## GitHub Trending 日榜
{json.dumps(gh_daily, ensure_ascii=False, indent=2)}

## GitHub Trending 周榜
{json.dumps(gh_weekly, ensure_ascii=False, indent=2)}

## Trendshift 每日热门（基于 engagement 指标排名，含社区讨论信号）
{json.dumps(ts_repos, ensure_ascii=False, indent=2)}

## Hacker News TOP20
{json.dumps(hn_items, ensure_ascii=False, indent=2)}

## HuggingFace Daily Papers
{json.dumps(hf_papers, ensure_ascii=False, indent=2)}

---

严格按以下格式输出，全程中文：

🚀 每日技术开源趋势报告 · {date_str}

━━━━━━━━━━━━━━━━━━━━

⚡ 今日最值得关注（3-5条）
跨渠道同一话题出现多次的优先（GitHub + Trendshift + HN 等），标注来源，说清楚为什么重要

1. 话题名称 [来源：GitHub+Trendshift+HN 等]
   简评：一句话说明为什么重要、值得关注
   链接：URL

━━━━━━━━━━━━━━━━━━━━

📈 GitHub 日榜 TOP10

1. 项目名 | 语言 | 今日+N⭐
   URL
   中文描述：一句话说明这个项目是什么、有什么用

━━━━━━━━━━━━━━━━━━━━

🔥 Trendshift 热门项目 TOP10（engagement 排名）
从 Trendshift 数据中选出与 GitHub 日榜不重复的项目优先展示，重复项目合并到 GitHub 日榜中标注

1. 项目名 | ⭐stars | 标签
   URL
   中文描述：一句话说明这个项目是什么、有什么用

━━━━━━━━━━━━━━━━━━━━

💬 Hacker News TOP10

1. 标题 | 🔥分数 💬评论数
   URL

━━━━━━━━━━━━━━━━━━━━

📄 HuggingFace 论文 TOP10

1. 英文原标题 | 👍upvotes
   中文：标题翻译
   简介：一句话说明研究什么、有什么意义
   URL

━━━━━━━━━━━━━━━━━━━━

📡 数据来源：GitHub Trending / Trendshift / Hacker News / HuggingFace Daily Papers
⏰ 生成时间：{now_str}"""

    print("🤖 AI 分析...")
    cfg = next((v for k, v in mykeys.items() if 'native' in k and 'claude' in k), None)
    sess = NativeClaudeSession(cfg=cfg)
    msg = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    report = ''.join(sess.ask(msg))
    # 正文末尾统一追加归档站链接（历史推送全览，方便追溯/分享）
    report = report.rstrip() + f"\n\n📚 历史归档全览：{archive_url()}\n（同一 WiFi 且本机开机时可访问）"
    print(f"✅ 报告生成，{len(report)} 字符")

    # 推送前即落状态: 内容已就绪, 从此刻起当日任何重复调用都会被幂等闸门拦下
    if not force:
        _mark_sent(date_str)

    # 推送隔离：任何一方失败不影响另一方
    print("📤 推送飞书私信...")
    try:
        notify_feishu(report)
        print("✅ 飞书私信推送完成")
    except Exception as e:
        print(f"⚠️ 飞书私信推送失败: {e}")

    print("📤 推送飞书群...")
    try:
        result = notify_group(report)
        print(f"✅ 飞书群推送完成: {result.get('msg')}")
    except Exception as e:
        print(f"⚠️ 飞书群推送失败: {e}")

    print("📤 推送钉钉群...")
    try:
        from reflect_scripts.notify_dingtalk_group import notify_group as ding_notify
        ding_result = ding_notify(report)
        print(f"✅ 钉钉群推送完成: {ding_result.get('errmsg')}")
    except Exception as e:
        print(f"⚠️ 钉钉群推送失败: {e}")

    with open(f"/Users/cuip/projects/GenericAgent/output/tech_trends_{date_str}.md", "w", encoding="utf-8") as f:
        f.write(report)
    print(f"💾 已保存 output/tech_trends_{date_str}.md")

if __name__ == "__main__":
    run()