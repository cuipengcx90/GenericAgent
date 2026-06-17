from __future__ import annotations

from typing import Any

from wechat_open_common import CODING_BUCKETS, SAAS_BUCKETS, short_url, render_bucket_items


def render_markdown(view: dict[str, Any], norm: dict[str, Any]) -> str:
    category = view['category']
    bucket_defs = CODING_BUCKETS if category == 'coding' else SAAS_BUCKETS
    bucket_map = {b['key']: b['items'] for b in view['bucket_views']}
    strength = view.get('theme_stats', {})

    lines = [
        f"# {view['group_name']} 日报（{view['date']}）",
        '',
        '## Top 3',
        '',
    ]
    if view.get('top3'):
        for idx, title in enumerate(view['top3'], 1):
            lines.append(f"{idx}. {title}")
    else:
        lines.append('暂无')

    lines.extend([
        '',
        '## 概览',
        '',
        f"> 原始消息 **{norm.get('total_msgs', 0)}** 条 ｜ 活跃人数 **{norm.get('active_users', 0)}** ｜ 主题总数 **{len(view.get('themes', []))}**",
        f"> 强度分布：高关注 **{strength.get('strong', 0)}** / 持续讨论 **{strength.get('medium', 0)}** / 零散提及 **{strength.get('weak', 0)}**",
        '',
    ])

    for key, meta in bucket_defs.items():
        lines.extend([f"## {meta['title']}", '', render_bucket_items(bucket_map.get(key, [])), ''])

    lines.extend(['## 未入主题 / 长尾消息', ''])
    lines.append(render_bucket_items(view.get('tail_themes', [])))
    lines.append('')
    lines.append('### 游离高信号消息')
    lines.append('')
    if view.get('tail_messages'):
        grouped = {'结论': [], '风险': [], '分享': [], '观点': []}
        for item in view['tail_messages']:
            grouped.setdefault(item.get('kind', '观点'), []).append(item)
        section_titles = [
            ('结论', '#### 未成主题但高价值结论'),
            ('风险', '#### 值得记录的风险/避坑'),
            ('分享', '#### 零散工具/链接分享'),
            ('观点', '#### 值得保留的单条观点'),
        ]
        for kind, subtitle in section_titles:
            lines.append(subtitle)
            lines.append('')
            items = grouped.get(kind) or []
            if not items:
                lines.append('- 暂无')
                lines.append('')
                continue
            for item in items:
                origin = f" ｜ 来自：{item['from']}" if item.get('from') else ''
                lines.append(f"- **{item['sender']}**（{item['time']}｜score={item.get('score', 0)}）{origin}")
                lines.append(f"  - {item['content']}")
            lines.append('')
    else:
        lines.append('- 暂无')
    lines.append('')

    lines.extend(['## 分享的文章/链接', ''])
    all_link_items = []
    for att in view.get('mp_links', []):
        label = (att.get('label') or '公众号文章').strip()
        url = att.get('url', '').strip()
        all_link_items.append((label, url))
    for url in view.get('links', []):
        all_link_items.append(('普通链接', url))
    if all_link_items:
        for label, url in all_link_items[:20]:
            lines.append(f"- {label}｜{short_url(url, 120)}" if url else f"- {label}")
    else:
        lines.append('- 暂无')

    lines.extend(['', '## 分享的文件', ''])
    if view.get('files'):
        for att in view['files'][:15]:
            label = (att.get('label') or 'file').strip()
            url = att.get('url', '').strip()
            lines.append(f"- {label}｜{short_url(url, 120)}" if url else f"- {label}")
    else:
        lines.append('- 暂无')

    lines.extend(['', '## 值得关注的发言', ''])
    if view.get('quotes'):
        for q in view['quotes']:
            lines.append(f"> {q['sender']}（{q['time']}）：{q['content']}")
    else:
        lines.append('> 暂无')

    return '\n'.join(lines) + '\n'
