from __future__ import annotations

from collections import Counter
from typing import Any

from wechat_open_common import SEMANTIC_BAD_TOKENS, STRUCTURAL_TAGS


def build_threads(recall: dict[str, Any], gap_seconds: int = 900) -> dict[str, Any]:
    threads = []
    cur = []
    last_ts = None
    for c in recall['candidates']:
        if not cur:
            cur = [c]
            last_ts = c['timestamp']
            continue
        current_semantic = {t for t in cur[-1]['tags'] if t not in STRUCTURAL_TAGS}
        new_semantic = {t for t in c['tags'] if t not in STRUCTURAL_TAGS}
        share_tag = bool(current_semantic & new_semantic)
        close = c['timestamp'] - (last_ts or c['timestamp']) <= gap_seconds
        if close or share_tag:
            cur.append(c)
        else:
            threads.append(cur)
            cur = [c]
        last_ts = c['timestamp']
    if cur:
        threads.append(cur)

    thread_objs = []
    for i, items in enumerate(threads, 1):
        semantic_counter = Counter(
            t for x in items for t in x['tags'] if len(t) > 1 and t not in STRUCTURAL_TAGS and t not in SEMANTIC_BAD_TOKENS
        )
        structural_counter = Counter(t for x in items for t in x['tags'] if t in STRUCTURAL_TAGS)
        attachment_counter = Counter(att['type'] for x in items for att in x.get('attachments', []))
        top_semantic_tags = [k for k, _ in semantic_counter.most_common(6)]
        top_tags = top_semantic_tags + [k for k, _ in structural_counter.most_common(3)]
        speakers = list(dict.fromkeys(x['sender'] for x in items))
        semantic_bits = [(x.get('semantic_text') or '').strip() for x in items if (x.get('semantic_text') or '').strip()]
        excerpt = ' / '.join(semantic_bits[:3])[:180]
        thread_objs.append({
            'thread_id': f'thread_{i}',
            'start_time': items[0]['time'],
            'end_time': items[-1]['time'],
            'message_indices': [x['idx'] for x in items],
            'candidate_count': len(items),
            'speakers': speakers,
            'top_semantic_tags': top_semantic_tags,
            'top_tags': top_tags[:6],
            'attachment_types': dict(attachment_counter),
            'score': sum(x['score'] for x in items),
            'excerpt': excerpt,
            'items': items,
        })
    return {
        'group_name': recall['group_name'],
        'group_id': recall['group_id'],
        'date': recall['date'],
        'thread_count': len(thread_objs),
        'threads': thread_objs,
    }
