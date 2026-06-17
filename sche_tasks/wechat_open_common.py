from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path('/Users/cuip/develop/obsidian-note/WeChat-Signals')
TEMPLATE_CODING = ROOT / '_templates' / 'tpl-daily-coding.md'
TEMPLATE_SAAS = ROOT / '_templates' / 'tpl-daily-saas.md'

CODING_BUCKETS = {
    'tools': {'title': '🔧 工具 & 模型', 'keywords': ['模型', 'claude', 'gpt', 'gemini', 'cursor', 'codex', '工具', 'mcp', 'agent']},
    'workflow': {'title': '🔄 工作流 & 规范', 'keywords': ['流程', '工作流', '自动化', 'sop', 'hook', '验证', '测试', '部署']},
    'multi': {'title': '🤖 多模型协作', 'keywords': ['多模型', '路由', '编排', '并行', '协作', '切换']},
    'arch': {'title': '📐 架构 & 设计', 'keywords': ['架构', '设计', '抽象', '模块', '接口', '重构', '系统']},
    'pitfall': {'title': '⚠️ 避坑 & 经验', 'keywords': ['坑', '风险', '问题', '报错', '失败', '兼容', '异常']},
}

SAAS_BUCKETS = {
    'growth': {'title': '📈 增长 & 获客', 'keywords': ['增长', '获客', '流量', '转化', '投放', 'seo', '内容']},
    'product': {'title': '🧩 产品 & 定位', 'keywords': ['产品', '定位', '功能', '用户', '需求', 'pmf']},
    'sales': {'title': '💰 销售 & 商业化', 'keywords': ['销售', '报价', '订单', '收入', '利润', '定价']},
    'ops': {'title': '⚙️ 运营 & 交付', 'keywords': ['运营', '交付', '客服', '流程', '效率', '自动化']},
    'signal': {'title': '🌍 市场信号 & 案例', 'keywords': ['市场', '案例', '趋势', '竞品', '出海', 'ai']},
}

STRUCTURAL_TAGS = {'图片', '视频', '文件', '链接', '附件'}
SEMANTIC_BAD_TOKENS = {'撤回', '收到', '哈哈', '好的', '嗯', '哦', '111', '666'}


def load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding='utf-8'))


def clean_text(text: str) -> str:
    if not text:
        return ''
    text = text.replace('\r', '\n')
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def short_url(url: str, limit: int = 80) -> str:
    url = (url or '').strip()
    if len(url) <= limit:
        return url
    return url[: limit - 3] + '...'


def dedup_keep_order(items: list[Any]) -> list[Any]:
    out = []
    seen = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def semantic_tags(text: str) -> list[str]:
    text = clean_text(text)
    tags = []
    low = text.lower()
    mapping = {
        'agent': ['agent', '智能体', '代理'],
        'mcp': ['mcp'],
        '模型': ['模型', 'llm', 'gpt', 'claude', 'gemini', 'qwen'],
        '工作流': ['工作流', 'workflow', '流程'],
        '自动化': ['自动化', 'automation'],
        '架构': ['架构', '设计', '重构'],
        '测试': ['测试', '验证'],
        '部署': ['部署', '发布'],
    }
    for tag, words in mapping.items():
        if any(w in low for w in words):
            tags.append(tag)
    return tags[:8]


def extract_urls(text: str) -> list[str]:
    return dedup_keep_order(re.findall(r'https?://[^\s)]+', text or ''))


def extract_attachments(mtype: str, content: str) -> list[dict[str, str]]:
    urls = extract_urls(content)
    atts: list[dict[str, str]] = []
    low = (mtype or '').lower()
    if low in {'image', 'img'}:
        atts.append({'type': 'image', 'label': 'image', 'url': urls[0] if urls else ''})
    elif low == 'video':
        atts.append({'type': 'video', 'label': 'video', 'url': urls[0] if urls else ''})
    elif low == 'file':
        atts.append({'type': 'file', 'label': 'file', 'url': urls[0] if urls else ''})
    elif low == 'share':
        for u in urls:
            atts.append({'type': 'mp_link' if 'mp.weixin.qq.com' in u else 'link', 'label': 'share', 'url': u})
    return atts


def is_system_sender(sender: str) -> bool:
    s = (sender or '').strip()
    return s in {'系统消息', '系统', '微信团队'}


def extract_attachments(text: str, mtype: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    raw = text or ''
    attachments: list[dict[str, Any]] = []
    urls: list[str] = []

    for label, url in re.findall(r'\[([^\]]+)\]\((https?://[^)]+)\)', raw):
        url = url.strip()
        urls.append(url)
        low = url.lower()
        if 'mp.weixin.qq.com' in low:
            attachments.append({'type': 'mp_link', 'label': label.strip() or '公众号文章', 'url': url})
        else:
            attachments.append({'type': 'link', 'label': label.strip() or 'link', 'url': url})

    for url in re.findall(r'https?://\S+', raw):
        u = url.strip().rstrip('),.;，。；')
        urls.append(u)

    type_map = {
        'image': 'image', 'img': 'image', '图片': 'image',
        'video': 'video', '视频': 'video',
        'file': 'file', '文件': 'file',
        'share': 'share', '链接': 'link',
    }
    mapped = type_map.get((mtype or '').lower(), type_map.get(mtype or '', ''))
    if mapped and not attachments:
        attachments.append({'type': mapped, 'label': clean_text(raw)[:80], 'url': urls[0] if urls else ''})

    semantic = clean_text(raw)
    semantic = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'\1', semantic)
    semantic = re.sub(r'https?://\S+', ' ', semantic)
    semantic = re.sub(r'\s+', ' ', semantic).strip()

    urls = dedup_keep_order([u for u in urls if u])
    attachments = dedup_keep_order(attachments)
    return attachments, semantic, urls


def score_message(*args: Any) -> tuple[int, list[str]]:
    if len(args) == 1 and isinstance(args[0], dict):
        msg = args[0]
        sender = clean_text(msg.get('sender', ''))
        mtype = clean_text(msg.get('mtype', ''))
        semantic = clean_text(msg.get('semantic_text') or msg.get('content') or '')
        atts = msg.get('attachments') or []
    elif len(args) == 3:
        sender = clean_text(args[0])
        mtype = clean_text(args[1])
        semantic = clean_text(args[2])
        atts, _, _ = extract_attachments(args[2] or '', mtype)
    else:
        raise TypeError('score_message expects (msg) or (sender, mtype, text)')

    score = 0
    labels = []
    if semantic:
        score += min(len(semantic) // 12 + 1, 4)
    tags = semantic_tags(semantic)
    if tags:
        score += len(tags) * 2
        labels.extend(tags)
    if any(a.get('type') == 'mp_link' for a in atts):
        score += 3
        labels.append('mp_link')
    if any(a.get('type') == 'link' for a in atts):
        score += 2
        labels.append('link')
    if any(a.get('type') == 'image' for a in atts):
        score += 1
        labels.append('image')
    if any(a.get('type') == 'video' for a in atts):
        score += 2
        labels.append('video')
    if any(a.get('type') == 'file' for a in atts):
        score += 2
        labels.append('file')
    if mtype == 'share':
        score += 2
        labels.append('share')
    if not semantic and mtype in {'image', 'video', 'file', 'share'}:
        score += 1
    if sender and not is_system_sender(sender):
        score += 1
    return score, labels


def choose_buckets(category: str, text: str, tags: list[str]) -> list[str]:
    defs = CODING_BUCKETS if category == 'coding' else SAAS_BUCKETS
    src = f"{text} {' '.join(tags)}".lower()
    hits = []
    for key, meta in defs.items():
        if any(w.lower() in src for w in meta['keywords']):
            hits.append(key)
    return hits or [next(iter(defs.keys()))]


def choose_bucket(category: str, text: str, tags: list[str]) -> list[str]:
    return choose_buckets(category, text, tags)


def infer_category(group_name: str) -> str:
    name = (group_name or '').lower()
    saas_words = ['出海', 'saas', '增长', '获客', '商业', '销售', '运营']
    return 'saas' if any(w in name for w in saas_words) else 'coding'


def render_bucket_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return '- 暂无\n'

    strength_label = {'strong': '高关注', 'medium': '持续讨论', 'weak': '零散提及'}
    lines = []
    for item in items:
        badge = strength_label.get(item.get('strength', ''), item.get('strength', ''))
        lines.append(f"- **{item['title']}** ｜ {badge} ｜ score={item.get('score', 0)}")

        meta_parts = []
        if item.get('message_count') is not None:
            meta_parts.append(f"消息数：{item['message_count']}")
        speakers = item.get('speakers') or []
        if speakers:
            meta_parts.append(f"参与人数：{len(speakers)}")
        if item.get('time_range'):
            meta_parts.append(f"时间范围：{item['time_range']}")
        if meta_parts:
            lines.append('  - ' + ' ｜ '.join(meta_parts))

        if item.get('summary'):
            lines.append(f"  - 摘要：{item['summary']}")

        extras = []
        if item.get('tags'):
            extras.append('标签：' + ' / '.join(item['tags'][:5]))
        if item.get('attachment_types'):
            extras.append('媒体：' + json.dumps(item['attachment_types'], ensure_ascii=False))
        if extras:
            lines.append('  - ' + ' ｜ '.join(extras))

        for q in (item.get('quotes') or [])[:3]:
            lines.append(f"  - 发言：{q['sender']}（{q['time']}）{q['content']}")

        mp_links = item.get('mp_links') or []
        links = dedup_keep_order(item.get('links') or [])
        files = item.get('files') or []
        images = item.get('images') or []
        videos = item.get('videos') or []

        if mp_links:
            samples = []
            for att in mp_links[:2]:
                label = (att.get('label') or '公众号文章').strip()[:36]
                url = att.get('url', '').strip()
                samples.append(f"[{label}]({url})" if url else label)
            lines.append('  - 公众号：' + '；'.join(samples))
        if links:
            lines.append('  - 链接：' + '；'.join(short_url(u) for u in links[:3]))
        if files:
            samples = []
            for att in files[:3]:
                label = (att.get('label') or 'file').strip()
                url = att.get('url', '').strip()
                samples.append(f"{label}: {short_url(url)}" if url else label)
            lines.append('  - 文件：' + '；'.join(samples))
        image_samples = dedup_keep_order([att.get('url', '').strip() for att in images if att.get('url')])[:3]
        if image_samples or images:
            shown = '；'.join(short_url(u, 72) for u in image_samples) if image_samples else '无显式URL'
            lines.append(f"  - 图片：{shown}（共{len(images)}）")
        video_samples = dedup_keep_order([att.get('url', '').strip() for att in videos if att.get('url')])[:3]
        if video_samples or videos:
            shown = '；'.join(short_url(u, 72) for u in video_samples) if video_samples else '无显式URL'
            lines.append(f"  - 视频：{shown}（共{len(videos)}）")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'
