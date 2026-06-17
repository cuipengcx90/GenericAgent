from __future__ import annotations

from typing import Any

from wechat_open_common import is_system_sender


def recall_candidates(norm: dict[str, Any]) -> dict[str, Any]:
    out = []
    for m in norm['messages']:
        text = m.get('semantic_text', '')
        if (not text and not m.get('attachments')) or is_system_sender(m['sender']):
            continue
        short_chatter = len(text) < 6 and m['score'] < 3 and not m.get('attachments')
        pure_noise = text in {'', '[动画表情]', '[图片]'} and not m['urls'] and not m.get('attachments')
        if short_chatter or pure_noise:
            continue
        if m['is_meaningful'] or m['mtype'] in {'share', 'file', 'image', 'video'}:
            out.append({
                'idx': m['idx'],
                'timestamp': m['timestamp'],
                'time': m['time'],
                'sender': m['sender'],
                'content': m['content'],
                'semantic_text': text,
                'score': m['score'],
                'tags': m['tags'][:10],
                'urls': m['urls'],
                'attachments': m.get('attachments', []),
            })
    return {
        'group_name': norm['group_name'],
        'group_id': norm['group_id'],
        'date': norm['date'],
        'candidate_count': len(out),
        'candidates': out,
    }
