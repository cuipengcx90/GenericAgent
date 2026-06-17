#!/usr/bin/env python3
"""Open-digest PoC for WeChat signals.

Pipeline:
1. normalize raw YAML -> raw_messages.json
2. recall high-value candidates from all messages
3. group nearby candidates into threads
4. synthesize themes from threads
5. map themes into bucket views
6. render markdown compatible with current note style

This PoC is intentionally heuristic-first and fully local.
LLM integration can replace theme synthesis later without changing artifacts.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path('/Users/cuip/develop/obsidian-note/WeChat-Signals')
TEMPLATE_CODING = ROOT / '_templates' / 'tpl-daily-coding.md'
TEMPLATE_SAAS = ROOT / '_templates' / 'tpl-daily-saas.md'

CODING_BUCKETS = {
    'tools': {'title': '🔧 工具 & 模型', 'keywords': ['claude', 'gpt', 'gemini', 'cursor', 'cc', 'ccline', 'kiro', 'model', 'api', 'sdk', '编辑器', '模型']},
    'workflow': {'title': '🔄 工作流 & 规范', 'keywords': ['workflow', 'spec', '流程', '规范', '测试', 'review', '提交', '自动化', '效率', '提示词']},
    'multimodel': {'title': '🤖 多模型协作', 'keywords': ['agent', '多agent', '多模型', 'mcp', '编排', 'router', 'tool calling']},
    'architecture': {'title': '📐 架构 & 设计', 'keywords': ['架构', '设计', '系统', '方案', '模块', 'wsl', 'windows', '部署', '前端', '后端']},
    'pitfalls': {'title': '⚠️ 避坑 & 经验', 'keywords': ['bug', '报错', '避坑', '退化', '封号', '失败', '成本', '慢', '坑', '智障', '听不懂']},
}

SAAS_BUCKETS = {
    'seo': {'title': '🔍 SEO & 流量', 'keywords': ['seo', '收录', '关键词', 'google', '排名', 'sitelink', '流量']},
    'monetization': {'title': '💰 变现 & 定价', 'keywords': ['定价', '付费', '订阅', '利润', '变现', '客单价']},
    'infra': {'title': '🌐 域名 & 基建', 'keywords': ['域名', 'cloudflare', 'vercel', '服务器', 'cdn', '部署', '站点地图']},
    'growth': {'title': '📈 增长 & 营销', 'keywords': ['增长', '营销', '推广', '社媒', '产品', '冷启动', '内容']},
    'risks': {'title': '⚠️ 避坑 & 合规', 'keywords': ['风险', '合规', '封号', '侵权', '坑', '注意', '检查']},
}

URL_RE = re.compile(r'https?://\S+')
CJK_RE = re.compile(r'[\u4e00-\u9fff]')
TOKEN_RE = re.compile(r'[A-Za-z][A-Za-z0-9_./:-]{1,}|[\u4e00-\u9fff]{2,}')
NOISE_URL_HINTS = ('vweixinf.tc.qq.com', 'weixin.qq.com/cgi-bin/readtemplate', 'emoji.qpic.cn', 'wxapp.tc.qq.com')
STRUCTURAL_TAGS = {'topic', 'share', 'assertion', 'url', 'number', 'entity', 'file', 'image'}
SEMANTIC_BAD_TOKENS = {
    '之前的', '这个', '那个', '我们', '你们', '他们', '就是', '然后', '因为', '所以', '可以',
    '感觉', '现在', '还是', '没有', '一个', '一下', '不是', '什么', '怎么', '真的', '今天', '昨天',
    '已经', '如果', '需要', '这样', '大家', '自己', '系统消息', '动画表情', '链接', '图片',
    '旺柴', '奸笑', '流泪', '破涕为笑', '脸红', '点睡觉', '这个也是', '不就好了', '真实'
}
STOPWORDS = SEMANTIC_BAD_TOKENS | {'用户要求', '实现算法', '进步没有完成'}
PRODUCT_HINTS = {'claude', 'gpt', 'gemini', 'cursor', 'kiro', 'agent', 'mcp', 'github', 'amazon', 'google', 'cloudflare', 'vercel', 'ccb', 'cc', 'cx'}


def is_low_value_tag(t: str) -> bool:
    k = t.lower().strip()
    if not k:
        return True
    if k in {x.lower() for x in STOPWORDS | STRUCTURAL_TAGS}:
        return True
    if len(t) < 2 or len(t) > 18:
        return True
    if '://' in t or t.startswith('http'):
        return True
    if t.startswith('[') and t.endswith(']'):
        return True
    if any(p in t for p in ['？', '?', '。', '，', ',', '！', '!', '：', ':', '；', ';']):
        return True
    if t.endswith(('吗', '呢', '啊', '呀', '吧')):
        return True
    return False


@dataclass
class Message:
    idx: int
    timestamp: int
    time: str
    sender: str
    mtype: str
    content: str
    local_id: str
    urls: list[str]
    is_meaningful: bool
    score: int
    tags: list[str]


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def is_system_sender(sender: str) -> bool:
    return sender in {'系统消息', '系统', 'unknown'} or sender.startswith('系统')


def is_noise_url(url: str) -> bool:
    return any(hint in url for hint in NOISE_URL_HINTS)


def clean_text(text: str) -> str:
    if not text:
        return ''
    text = text.replace('<nil>', '').strip()
    text = re.sub(r'!\[图片\]\([^)]*\)', '[图片]', text)
    text = re.sub(r'!\[动画表情\]\([^)]*\)', '[动画表情]', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def semantic_tags(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text)
    out = []
    for t in tokens:
        if is_low_value_tag(t):
            continue
        k = t.lower()
        if k in STOPWORDS or t in SEMANTIC_BAD_TOKENS:
            continue
        if CJK_RE.search(t) or any(ch.isupper() for ch in t) or any(ch.isdigit() for ch in t):
            if t not in out:
                out.append(t)
    return out[:8]


def score_message(sender: str, mtype: str, text: str) -> tuple[int, list[str]]:
    score = 0
    labels = []
    lower = text.lower()
    if not text or is_system_sender(sender):
        return 0, labels
    if '[动画表情]' in text:
        return 0, labels
    if mtype in {'share', 'file'}:
        score += 2
        labels.append(mtype)
    if mtype == 'image' and '[图片]' not in text:
        return 0, labels
    urls = [u for u in URL_RE.findall(text) if not is_noise_url(u)]
    if urls:
        score += 2
        labels.append('url')
    if len(text) >= 20:
        score += 1
    if any(k in lower for k in ['为什么', '怎么', '建议', '经验', '避坑', '报错', '方案', '架构', '工作流', 'agent', 'seo', '关键词', '收录', '部署']):
        score += 2
        labels.append('topic')
    if any(k in lower for k in ['claude', 'gpt', 'gemini', 'cursor', 'kiro', 'cloudflare', 'vercel', 'google', 'ccb', 'wsl', 'github']):
        score += 2
        labels.append('entity')
    if re.search(r'\d', text):
        score += 1
        labels.append('number')
    if any(k in text for k in ['？', '?', '→', '=>', 'vs', '不要', '必须', '注意', '建议']):
        score += 1
        labels.append('assertion')
    return score, labels


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    messages = []
    active = Counter()
    for idx, item in enumerate(raw.get('messages', [])):
        text = clean_text(str(item.get('content', '')))
        sender = str(item.get('sender') or 'unknown')
        mtype = str(item.get('type') or 'text')
        score, labels = score_message(sender, mtype, text)
        urls = [u for u in URL_RE.findall(text) if not is_noise_url(u)]
        msg = Message(
            idx=idx,
            timestamp=int(item.get('timestamp') or 0),
            time=str(item.get('time') or ''),
            sender=sender,
            mtype=mtype,
            content=text,
            local_id=str(item.get('local_id') or ''),
            urls=urls,
            is_meaningful=score >= 2 and bool(text) and not is_system_sender(sender),
            score=score,
            tags=semantic_tags(text) + labels,
        )
        messages.append(asdict(msg))
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


def recall_candidates(norm: dict[str, Any]) -> dict[str, Any]:
    out = []
    for m in norm['messages']:
        text = m['content']
        if not text or is_system_sender(m['sender']):
            continue
        short_chatter = len(text) < 6 and m['score'] < 3
        pure_noise = text in {'[动画表情]', '[图片]'} and not m['urls']
        if short_chatter or pure_noise:
            continue
        if m['is_meaningful'] or m['mtype'] in {'share', 'file'}:
            out.append({
                'idx': m['idx'],
                'timestamp': m['timestamp'],
                'time': m['time'],
                'sender': m['sender'],
                'content': text,
                'score': m['score'],
                'tags': m['tags'][:10],
                'urls': m['urls'],
            })
    return {
        'group_name': norm['group_name'],
        'group_id': norm['group_id'],
        'date': norm['date'],
        'candidate_count': len(out),
        'candidates': out,
    }


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
        top_semantic_tags = [k for k, _ in semantic_counter.most_common(6)]
        top_tags = top_semantic_tags + [k for k, _ in structural_counter.most_common(3)]
        speakers = list(dict.fromkeys(x['sender'] for x in items))
        excerpt = ' / '.join(x['content'][:50] for x in items[:3])
        thread_objs.append({
            'thread_id': f'thread_{i}',
            'start_time': items[0]['time'],
            'end_time': items[-1]['time'],
            'message_indices': [x['idx'] for x in items],
            'candidate_count': len(items),
            'speakers': speakers,
            'top_semantic_tags': top_semantic_tags,
            'top_tags': top_tags[:6],
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


def infer_category(group_name: str) -> str:
    name = group_name.lower()
    if any(k in name for k in ['出海', 'seo', '流量']):
        return 'saas'
    return 'coding'


def choose_bucket(category: str, text: str, tags: list[str]) -> list[str]:
    mapping = CODING_BUCKETS if category == 'coding' else SAAS_BUCKETS
    pool = (text + ' ' + ' '.join(tags)).lower()
    hits = []
    for key, meta in mapping.items():
        n = sum(1 for kw in meta['keywords'] if kw.lower() in pool)
        if n:
            hits.append((key, n))
    hits.sort(key=lambda x: (-x[1], x[0]))
    if not hits:
        return ['pitfalls' if category == 'coding' else 'growth']
    return [k for k, _ in hits[:2]]


def summarize_thread(thread: dict[str, Any]) -> tuple[str, str]:
    semantic = [t for t in thread.get('top_semantic_tags', []) if not is_low_value_tag(t)]
    prioritized = []
    fallback = []
    for t in semantic:
        tl = t.lower()
        if any(h in tl for h in PRODUCT_HINTS) or any(ch.isdigit() for ch in t):
            if t not in prioritized:
                prioritized.append(t)
        elif t not in fallback:
            fallback.append(t)
    picked = (prioritized + fallback)[:3]
    first = thread['items'][0]['content'] if thread['items'] else ''
    title = ' / '.join(picked) if picked else first[:24]
    details = []
    for item in thread['items'][:4]:
        details.append(f"@{item['sender']}: {item['content'][:80]}")
    return title or '未命名主题', '；'.join(details)


def synthesize_themes(threads: dict[str, Any], category: str | None = None) -> dict[str, Any]:
    category = category or infer_category(threads['group_name'])
    themes = []
    for i, th in enumerate(sorted(threads['threads'], key=lambda x: x['score'], reverse=True), 1):
        title, detail = summarize_thread(th)
        all_text = ' '.join(it['content'] for it in th['items'])
        all_tags = [t for it in th['items'] for t in it['tags']]
        buckets = choose_bucket(category, all_text, all_tags)
        strength = 'strong' if th['score'] >= 12 else 'medium' if th['score'] >= 7 else 'weak'
        links = []
        seen = set()
        for it in th['items']:
            for u in it.get('urls', []):
                if not is_noise_url(u) and u not in seen:
                    seen.add(u)
                    links.append(u)
        quotes = []
        for it in th['items']:
            if it['content'] not in {'[动画表情]', '[图片]'}:
                quotes.append({'speaker': it['sender'], 'quote': it['content'][:100]})
            if len(quotes) >= 2:
                break
        themes.append({
            'theme_id': f'theme_{i}',
            'title': title,
            'summary': detail[:180],
            'detail': detail,
            'strength': strength,
            'score': th['score'],
            'suggested_buckets': buckets,
            'participants': th['speakers'],
            'evidence_message_indices': th['message_indices'][:12],
            'top_tags': th['top_tags'],
            'links': links[:5],
            'quotes': quotes,
        })
    top3 = [t['title'] for t in themes[:3]]
    return {
        'group_name': threads['group_name'],
        'group_id': threads['group_id'],
        'date': threads['date'],
        'category': category,
        'theme_count': len(themes),
        'top3': top3,
        'themes': themes,
    }


def map_views(theme_doc: dict[str, Any]) -> dict[str, Any]:
    category = theme_doc['category']
    mapping = CODING_BUCKETS if category == 'coding' else SAAS_BUCKETS
    bucket_views = {k: [] for k in mapping}
    for th in theme_doc['themes']:
        for b in th['suggested_buckets']:
            if b in bucket_views:
                bucket_views[b].append({
                    'theme_id': th['theme_id'],
                    'title': th['title'],
                    'summary': th['summary'],
                    'strength': th['strength'],
                    'participants': th['participants'],
                })
    links = []
    quotes = []
    seen_links = set()
    for th in theme_doc['themes']:
        for url in th['links']:
            if url not in seen_links:
                seen_links.add(url)
                links.append({'title': th['title'], 'url': url, 'sharer': th['participants'][0] if th['participants'] else '-', 'heat': th['strength']})
        for q in th['quotes']:
            quotes.append(q)
    semantic_tags = []
    for th in theme_doc['themes']:
        for t in th['top_tags']:
            if t not in STRUCTURAL_TAGS and t not in semantic_tags:
                semantic_tags.append(t)
    return {
        'group_name': theme_doc['group_name'],
        'group_id': theme_doc['group_id'],
        'date': theme_doc['date'],
        'category': category,
        'top3': theme_doc['top3'],
        'themes': theme_doc['themes'],
        'bucket_views': bucket_views,
        'links': links[:15],
        'quotes': quotes[:8],
        'tags': semantic_tags[:15],
    }


def render_bucket_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return '> (no signals today)\n'
    lines = []
    for it in items[:12]:
        people = ', '.join(it['participants'][:3])
        lines.append(f"- **{it['title']}**：{it['summary']}（{it['strength']}｜{people}）")
    return '\n'.join(lines) + '\n'


def render_links(links: list[dict[str, Any]]) -> str:
    if not links:
        return '| - | (no links today) | - | - |\n'
    rows = []
    for lk in links:
        rows.append(f"| {lk['sharer']} | [{lk['title']}]({lk['url']}) | {lk['url']} | {lk['heat']} |")
    return '\n'.join(rows) + '\n'


def render_quotes(quotes: list[dict[str, str]]) -> str:
    if not quotes:
        return '> (no notable quotes)\n'
    return '\n\n'.join(f"> **@{q['speaker']}**: {q['quote']}" for q in quotes[:6]) + '\n'


def render_markdown(view: dict[str, Any], norm: dict[str, Any]) -> str:
    category = view['category']
    template_path = TEMPLATE_CODING if category == 'coding' else TEMPLATE_SAAS
    tpl = template_path.read_text(encoding='utf-8')
    mapping = CODING_BUCKETS if category == 'coding' else SAAS_BUCKETS
    top3 = view['themes'][:3] + [{'title': '', 'detail': ''}] * 3
    repl = {
        '{{date}}': view['date'],
        '{{group_name}}': view['group_name'],
        '{{group_id}}': view['group_id'],
        '{{signals_count}}': str(len(view['themes'])),
        '{{total_msgs}}': str(norm['total_msgs']),
        '{{active_users}}': str(norm['active_users']),
        '{{top1}}': top3[0]['title'],
        '{{top2}}': top3[1]['title'],
        '{{top3}}': top3[2]['title'],
        '{{top1_detail}}': top3[0].get('detail',''),
        '{{top2_detail}}': top3[1].get('detail',''),
        '{{top3_detail}}': top3[2].get('detail',''),
        '{{links_table}}': render_links(view['links']),
        '{{files_list}}': '- (no files extracted)\n',
        '{{notable_quotes}}': render_quotes(view['quotes']),
    }
    for bucket_key, meta in mapping.items():
        placeholder = {
            'coding': {
                'tools': '{{signals_tools}}', 'workflow': '{{signals_workflow}}', 'multimodel': '{{signals_multimodel}}',
                'architecture': '{{signals_architecture}}', 'pitfalls': '{{signals_pitfalls}}'
            },
            'saas': {
                'seo': '{{signals_seo}}', 'monetization': '{{signals_monetization}}', 'infra': '{{signals_infra}}',
                'growth': '{{signals_growth}}', 'risks': '{{signals_risks}}'
            }
        }[category][bucket_key]
        repl[placeholder] = render_bucket_items(view['bucket_views'].get(bucket_key, []))
    for k, v in repl.items():
        tpl = tpl.replace(k, str(v))
    tags_line = '[' + ', '.join(view['tags']) + ']'
    tpl = tpl.replace('tags: []', f'tags: {tags_line}')
    strong = sum(1 for t in view['themes'] if t['strength'] == 'strong')
    medium = sum(1 for t in view['themes'] if t['strength'] == 'medium')
    weak = sum(1 for t in view['themes'] if t['strength'] == 'weak')
    tpl = tpl.replace('strong: 0', f'strong: {strong}')
    tpl = tpl.replace('medium: 0', f'medium: {medium}')
    tpl = tpl.replace('weak: 0', f'weak: {weak}')
    return tpl


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, help='raw yaml path')
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--category', choices=['coding', 'saas'])
    args = ap.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = load_yaml(in_path)
    norm = normalize(raw)
    recall = recall_candidates(norm)
    threads = build_threads(recall)
    themes = synthesize_themes(threads)
    views = map_views(themes)
    md = render_markdown(views, norm)

    write_json(out_dir / 'raw_messages.json', norm)
    write_json(out_dir / 'recall_candidates.json', recall)
    write_json(out_dir / 'threads.json', threads)
    write_json(out_dir / 'themes.json', themes)
    write_json(out_dir / 'daily_digest.json', views)
    (out_dir / 'daily.md').write_text(md, encoding='utf-8')

    print(json.dumps({
        'output_dir': str(out_dir),
        'group_name': norm['group_name'],
        'total_msgs': norm['total_msgs'],
        'candidate_count': recall['candidate_count'],
        'thread_count': threads['thread_count'],
        'theme_count': themes['theme_count'],
        'category': themes['category'],
        'daily_md': str(out_dir / 'daily.md'),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
