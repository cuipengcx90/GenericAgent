#!/usr/bin/env python3
"""HelloGitHub 月度精选榜 + 月刊推送
数据源：
  1. 月度精选榜: hellogithub.com/?sort_by=featured&rank_by=monthly
  2. 月刊 Volume: hellogithub.com/periodical/volume/{num}
"""
import sys, requests
from datetime import datetime, timedelta

sys.path.insert(0, '/Users/cuip/projects/GenericAgent')
from llmcore import NativeClaudeSession, mykeys
from reflect_scripts.reflect_common import notify_feishu
from reflect_scripts.notify_feishu_group import notify_group

API = 'https://api.hellogithub.com/v1'
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_monthly_featured(year=None, month=None, pages=3):
    """抓取月度精选榜（sort_by=featured, rank_by=monthly）
    默认取上个月数据（当月数据可能不全）"""
    if year is None or month is None:
        last_month = datetime.now().replace(day=1) - timedelta(days=1)
        year, month = last_month.year, last_month.month

    items = []
    for p in range(1, pages + 1):
        try:
            params = {
                'sort_by': 'featured', 'rank_by': 'monthly',
                'tid': 'all', 'year': year, 'month': month, 'page': p
            }
            r = requests.get(f'{API}/', params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            d = r.json()
            if not d.get('success') or not d.get('data'):
                break
            items.extend(d['data'])
            if not d.get('has_more'):
                break
        except Exception as e:
            print(f"⚠️ 月度精选第{p}页失败: {e}")
            break
    print(f"🏆 月度精选榜({year}-{month:02d}) 抓取 {len(items)} 条")
    return items, year, month


def fetch_latest_volume():
    """抓取最新一期月刊 Volume（自动获取最新期号）"""
    try:
        # 先获取最新期号
        r0 = requests.get(f'{API}/periodical/', headers=HEADERS, timeout=15)
        r0.raise_for_status()
        d0 = r0.json()
        volumes = d0.get('volumes', [])
        if not volumes:
            print("⚠️ 无法获取月刊期号列表")
            return None, None
        latest_num = volumes[0].get('num', volumes[0]) if isinstance(volumes[0], dict) else volumes[0]

        # 获取该期内容
        r = requests.get(f'{API}/periodical/volume/', params={'num': latest_num}, headers=HEADERS, timeout=15)
        r.raise_for_status()
        d = r.json()
        if not d.get('success'):
            return None, None

        vol_num = d.get('current_num', latest_num)
        publish_at = d.get('publish_at', '')
        categories = []
        for cat in d.get('data', []):
            cat_name = cat.get('category_name', '')
            cat_items = []
            for item in cat.get('items', []):
                cat_items.append({
                    'name': item.get('full_name', item.get('name', '')),
                    'desc': item.get('description', ''),
                    'stars': item.get('stars', 0),
                    'url': item.get('github_url', ''),
                })
            categories.append({'category': cat_name, 'items': cat_items})

        meta = {'vol': vol_num, 'publish_at': publish_at, 'total_categories': len(categories),
                'total_items': sum(len(c['items']) for c in categories)}
        print(f"📖 月刊第{vol_num}期 | {publish_at[:10]} | {meta['total_categories']}分类 {meta['total_items']}项目")
        return meta, categories
    except Exception as e:
        print(f"⚠️ 月刊获取失败: {e}")
        return None, None


def run():
    print("=" * 50)
    print(f"🚀 HelloGitHub 月度推送 | {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 50)

    # 1. 月度精选榜
    featured_items, year, month = fetch_monthly_featured()
    # 2. 最新月刊
    vol_meta, vol_cats = fetch_latest_volume()

    if not featured_items and not vol_cats:
        print("❌ 无数据，跳过")
        return

    # 构造月度精选文本
    featured_text = f"时间范围: {year}年{month}月\n"
    for i, item in enumerate(featured_items, 1):
        featured_text += (
            f"{i}. {item.get('full_name','')} [{item.get('primary_lang','N/A')}]\n"
            f"   {item.get('title','')} | 点击:{item.get('clicks_total',0)}\n"
            f"   {item.get('summary','')[:100]}\n"
        )

    # 构造月刊文本
    vol_text = ""
    vol_num = "?"
    if vol_meta and vol_cats:
        vol_num = vol_meta['vol']
        vol_text = f"第{vol_num}期 | 发布于 {vol_meta['publish_at'][:10]} | {vol_meta['total_items']}个项目\n"
        for cat in vol_cats:
            vol_text += f"\n【{cat['category']}】\n"
            for item in cat['items']:
                vol_text += f"  ★{item['stars']} {item['name']}\n    {item['desc'][:80]}\n"

    date_str = f"{year}-{month:02d}"
    prompt = f"""你是开源项目推荐专家。基于以下 HelloGitHub 数据，生成一份中文精选推送。

## 数据源

### 月度精选榜（{year}年{month}月最受欢迎的开源项目）
{featured_text}

### HelloGitHub 月刊（第{vol_num}期，编辑精选的优质项目）
{vol_text}

## 要求
1. 标题行：🌟 HelloGitHub 精选月报 · {date_str}
2. 分两个板块：
   板块一「🏆 月度精选榜」：从月度精选榜中挑 10-12 个最有价值的项目，按主题分组（🤖 AI智能 / 🛠️ 效率工具 / 🖥️ 开发框架 / 🔒 安全运维 等）
   板块二「📖 月刊第{vol_num}期精选」：从月刊各分类中各挑 1-2 个亮点项目
3. 每个项目一行：emoji + 项目名 (语言/★stars) — 一句话推荐
4. 每个分组之间空一行，视觉清晰
5. 末尾加一行分隔，然后：📡 数据来源：HelloGitHub · hellogithub.com
6. 总字数 800-1500 字
7. 严格基于上述数据，不要虚构
8. 【重要】输出纯文本，绝对禁止任何 Markdown 语法（禁止 **加粗**、# 标题、- 列表符、` 反引号等），只用 emoji、空格、换行排版
9. 用分隔线 ═══════════ 分隔板块，美观大方
"""

    print("\n🤖 AI 生成报告中...")
    cfg = next((v for k, v in mykeys.items() if 'native' in k and 'claude' in k), None)
    sess = NativeClaudeSession(cfg=cfg)
    msg = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    report = ''.join(sess.ask(msg))
    print(f"✅ 报告生成完成 ({len(report)}字)")

    # 推送
    print("📤 推送飞书...")
    notify_feishu(report)
    result = notify_group(report)
    print(f"✅ 推送完成: {result.get('msg')}")

    # 保存
    out_path = f"/Users/cuip/projects/GenericAgent/output/hellogithub_monthly_{date_str}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"💾 已保存 {out_path}")


if __name__ == "__main__":
    run()