from __future__ import annotations

from collections import Counter
from typing import Any

from wechat_open_common import CODING_BUCKETS, SAAS_BUCKETS


def _strength_rank(s: str) -> int:
    return {'strong': 3, 'medium': 2, 'weak': 1}.get(s, 0)


def _classify_tail_message(text: str, score: int, attachments: list[dict[str, Any]] | None = None) -> str:
    low = (text or '').lower()
    attachments = attachments or []
    if any(k in text for k in ['风险', '避坑', '报错', '失败', '限制', '放宽', '智障', '坑']) or any(k in low for k in ['error', 'fail', 'risk']):
        return '风险'
    if any(k in text for k in ['结论', '通过', '稳定', '打算', '计划', '决定', '实现', '完成']) or 'callback' in low:
        return '结论'
    if any(k in text for k in ['链接', 'http://', 'https://', '文章', '对比', '分享']) or attachments:
        return '分享'
    return '观点'


def _collect_tail_messages(theme_doc: dict[str, Any], seen_indices: set[int]) -> list[dict[str, Any]]:
    tail_messages: list[dict[str, Any]] = []
    for m in theme_doc.get('candidate_messages', []):
        idx = m.get('idx')
        if not isinstance(idx, int) or idx in seen_indices:
            continue
        text = (m.get('semantic_text') or m.get('content') or '').strip()
        if not text:
            continue
        score = m.get('score', 0)
        attachments = m.get('attachments') or []
        if score < 3 and not attachments:
            continue
        tail_messages.append({
            'idx': idx,
            'sender': m.get('sender', ''),
            'time': m.get('time', ''),
            'content': text,
            'score': score,
            'from': '未成主题候选',
            'kind': _classify_tail_message(text, score, attachments),
        })
        seen_indices.add(idx)
    tail_messages.sort(key=lambda x: (-x.get('score', 0), x.get('time', ''), x.get('idx', 0)))
    return tail_messages[:15]


def map_views(theme_doc: dict[str, Any]) -> dict[str, Any]:
    category = theme_doc['category']
    bucket_defs = CODING_BUCKETS if category == 'coding' else SAAS_BUCKETS

    bucket_views = []
    assigned_threads = set()
    all_links: list[str] = []
    all_mp_links: list[dict[str, Any]] = []
    all_quotes: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    semantic_tags: Counter[str] = Counter()

    for key, meta in bucket_defs.items():
        items = []
        for theme in theme_doc['themes']:
            if key not in theme.get('buckets', []):
                continue
            if theme.get('strength') == 'weak':
                continue
            items.append(theme)
            assigned_threads.add(theme['thread_id'])
            all_links.extend(theme.get('links', []))
            all_mp_links.extend(theme.get('mp_links', []))
            all_quotes.extend(theme.get('quotes', []))
            all_files.extend(theme.get('files', []))
            semantic_tags.update(theme.get('tags', []))
        items.sort(key=lambda x: (_strength_rank(x.get('strength', '')), x['score']), reverse=True)
        bucket_views.append({'key': key, 'title': meta['title'], 'items': items})

    tail_themes = [
        t for t in sorted(theme_doc['themes'], key=lambda x: (_strength_rank(x.get('strength', '')), x['score']), reverse=True)
        if t['thread_id'] not in assigned_threads or t.get('strength') == 'weak'
    ]

    seen_indices = set()
    for bucket in bucket_views:
        for item in bucket['items']:
            for q in item.get('quotes', []):
                idx = q.get('idx')
                if isinstance(idx, int):
                    seen_indices.add(idx)
    for item in tail_themes:
        for q in item.get('quotes', []):
            idx = q.get('idx')
            if isinstance(idx, int):
                seen_indices.add(idx)

    tail_messages = _collect_tail_messages(theme_doc, seen_indices)

    return {
        'group_name': theme_doc['group_name'],
        'group_id': theme_doc['group_id'],
        'date': theme_doc['date'],
        'category': category,
        'top3': theme_doc['top3'],
        'themes': theme_doc['themes'],
        'bucket_views': bucket_views,
        'links': all_links[:15],
        'mp_links': all_mp_links[:10],
        'quotes': all_quotes[:12],
        'files': all_files[:10],
        'tags': [k for k, _ in semantic_tags.most_common(15)],
        'tail_themes': tail_themes,
        'tail_messages': tail_messages,
        'theme_stats': {
            'strong': sum(1 for t in theme_doc['themes'] if t.get('strength') == 'strong'),
            'medium': sum(1 for t in theme_doc['themes'] if t.get('strength') == 'medium'),
            'weak': sum(1 for t in theme_doc['themes'] if t.get('strength') == 'weak'),
        },
    }
