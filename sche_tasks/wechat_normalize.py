from __future__ import annotations

from collections import Counter
from typing import Any

from wechat_theme_models import Message, to_dict
from wechat_open_common import clean_text, extract_attachments, is_system_sender, score_message, semantic_tags


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    messages = []
    active = Counter()
    for idx, item in enumerate(raw.get('messages', [])):
        text = clean_text(str(item.get('content', '')))
        sender = str(item.get('sender') or 'unknown')
        mtype = str(item.get('type') or 'text')
        attachments, semantic_text, urls = extract_attachments(text, mtype)
        score, labels = score_message(sender, mtype, text)
        msg = Message(
            idx=idx,
            timestamp=int(item.get('timestamp') or 0),
            time=str(item.get('time') or ''),
            sender=sender,
            mtype=mtype,
            content=text,
            semantic_text=semantic_text,
            local_id=str(item.get('local_id') or ''),
            urls=urls,
            attachments=attachments,
            is_meaningful=(score >= 2 and bool(semantic_text or text) and not is_system_sender(sender)) or bool(attachments),
            score=score,
            tags=semantic_tags(semantic_text) + labels,
        )
        messages.append(to_dict(msg))
        if text and not is_system_sender(sender):
            active[sender] += 1
    return {
        'group_name': raw.get('chat') or raw.get('group_name') or 'unknown',
        'group_id': raw.get('username') or raw.get('group_id') or '',
        'date': messages[0]['time'][:10] if messages and messages[0]['time'] else '',
        'total_msgs': len(messages),
        'active_users': len(active),
        'active_user_counts': active.most_common(20),
        'messages': messages,
    }
