from __future__ import annotations

from typing import Any
import re

from wechat_open_common import choose_bucket, infer_category

SPLIT_RE = re.compile(r'[\n/｜|；;。！？!?]+')
BAD_TITLE_PARTS = {
    '旺柴', '图片', '动画表情', '表情', '链接', '视频', '文件', '公众号', 'url', 'number', 'image', 'video', 'file', '引用'
}
GENERIC_PREFIXES = (
    '这个', '那个', '就是', '然后', '其实', '感觉', '所以', '如果', '但是', '因为', '你们', '我们', '他们'
)


def _clean_part(text: str) -> str:
    text = (text or '').strip()
    text = re.sub(r'\[[^\]]{1,12}\]', ' ', text)
    text = re.sub(r'@[\w\u4e00-\u9fff.-]+', ' ', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'^["“”‘’\-—:：,，.。/|\[\]()]+|["“”‘’\-—:：,，.。/|\[\]()]+$', '', text)
    return text.strip()


def _is_bad_part(text: str) -> bool:
    if not text:
        return True
    lower = text.lower()
    if lower in BAD_TITLE_PARTS:
        return True
    if len(text) <= 1:
        return True
    if re.fullmatch(r'[A-Za-z]{1,3}', text):
        return True
    if re.fullmatch(r'\d+', text):
        return True
    if text.startswith('http'):
        return True
    return False


def _shorten_part(text: str) -> str:
    text = text.strip()
    if len(text) <= 18:
        return text
    for sep in ('，', ',', '：', ':', ' ', '的', '了', '吗'):
        idx = text.find(sep)
        if 4 <= idx <= 18:
            return text[:idx]
    return text[:18]


def _candidate_parts(thread: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    excerpt = (thread.get('excerpt') or '').strip()
    for seg in SPLIT_RE.split(excerpt):
        seg = _clean_part(seg)
        if seg and not _is_bad_part(seg):
            parts.append(seg)
    for tag in thread.get('top_semantic_tags', []):
        tag = _clean_part(tag)
        if tag and not _is_bad_part(tag):
            parts.append(tag)
    return parts


def _build_summary(thread: dict[str, Any], title: str) -> str:
    parts: list[str] = []
    for item in thread.get('items', []):
        semantic = _clean_part((item.get('semantic_text') or '').strip())
        if semantic and not _is_bad_part(semantic):
            parts.append(semantic[:48])
        if len(parts) >= 3:
            break
    if parts:
        return ' / '.join(parts)[:120]
    excerpt = _clean_part((thread.get('excerpt') or '').strip())
    return excerpt[:120] if excerpt else title


def _pick_title(parts: list[str]) -> str:
    if not parts:
        return '综合讨论'
    chosen: list[str] = []
    for part in parts:
        if any(part.startswith(p) for p in GENERIC_PREFIXES) and len(part) > 10:
            continue
        short = _shorten_part(part)
        if short and short not in chosen and not _is_bad_part(short):
            chosen.append(short)
        if len(chosen) >= 2:
            break
    if not chosen:
        for part in parts:
            short = _shorten_part(part)
            if short and not _is_bad_part(short):
                chosen.append(short)
                break
    title = '｜'.join(chosen[:2])
    return title[:36] or '综合讨论'


def summarize_thread(thread: dict[str, Any]) -> tuple[str, str]:
    parts = _candidate_parts(thread)
    title = _pick_title(parts)
    summary = _build_summary(thread, title)
    return title, summary


def synthesize_themes(threads: dict[str, Any]) -> dict[str, Any]:
    category = infer_category(threads['group_name'])
    themes = []
    for th in threads['threads']:
        title, summary = summarize_thread(th)
        score = th['score']
        strength = 'strong' if score >= 12 else 'medium' if score >= 6 else 'weak'
        buckets = choose_bucket(category, summary + ' ' + title, th['top_semantic_tags'])
        links = []
        mp_links = []
        images = []
        videos = []
        files = []
        seen = set()
        quotes = []
        for item in th['items']:
            semantic = (item.get('semantic_text') or '').strip()
            if semantic and len(semantic) >= 8 and len(quotes) < 4:
                quotes.append({'sender': item['sender'], 'time': item['time'], 'content': semantic[:180]})
            for url in item.get('urls', []):
                if url and url not in seen:
                    if 'mp.weixin.qq.com' in url:
                        mp_links.append({'type': 'mp_link', 'url': url, 'label': '公众号文章'})
                    else:
                        links.append(url)
                    seen.add(url)
            for att in item.get('attachments', []):
                atype = att.get('type')
                url = att.get('url')
                if atype == 'image':
                    if url and url not in seen:
                        images.append(att)
                        seen.add(url)
                    elif not url:
                        images.append(att)
                    continue
                if atype == 'video':
                    if url and url not in seen:
                        videos.append(att)
                        seen.add(url)
                    elif not url:
                        videos.append(att)
                    continue
                if atype == 'file':
                    if url and url not in seen:
                        files.append(att)
                        seen.add(url)
                    elif not url:
                        files.append(att)
                    continue
                if atype == 'mp_link':
                    if url and url not in seen:
                        mp_links.append(att)
                        seen.add(url)
                    continue
                if atype == 'link' and url and url not in seen:
                    links.append(url)
                    seen.add(url)
        themes.append({
            'thread_id': th['thread_id'],
            'title': title,
            'summary': summary,
            'strength': strength,
            'score': score,
            'buckets': buckets,
            'speakers': th['speakers'][:8],
            'tags': th['top_semantic_tags'][:8],
            'links': links[:8],
            'mp_links': mp_links[:5],
            'images': images[:6],
            'videos': videos[:4],
            'files': files[:4],
            'attachment_types': th.get('attachment_types', {}),
            'quotes': quotes,
            'message_count': len(th.get('items', [])),
            'time_range': f"{th.get('start_time', '')} ~ {th.get('end_time', '')}".strip(' ~'),
        })
    themes.sort(key=lambda x: x['score'], reverse=True)
    top3 = [t['title'] for t in themes[:3]]
    return {
        'group_name': threads['group_name'],
        'group_id': threads['group_id'],
        'date': threads['date'],
        'category': category,
        'theme_count': len(themes),
        'top3': top3,
        'themes': themes,
        'candidate_messages': [
            {
                'idx': item.get('idx'),
                'time': item.get('time', ''),
                'sender': item.get('sender', ''),
                'content': item.get('content', ''),
                'semantic_text': item.get('semantic_text', ''),
                'score': item.get('score', 0),
                'attachments': item.get('attachments', []),
            }
            for th in threads['threads'] for item in th.get('items', [])
        ],
    }
