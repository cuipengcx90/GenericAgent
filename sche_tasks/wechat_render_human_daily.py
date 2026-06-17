from __future__ import annotations

from collections import Counter
from typing import Any


STOPWORDS = {
    '这', '那个', '这个', '一个', '一下', '然后', '感觉', '有点', '就是', '还是', '已经',
    '我们', '你们', '他们', '可以', '今天', '昨天', '真的', '直接', '现在', '后面', '这个GPT',
    '那个CC', '这个CC', '之前', '一直', '什么', '怎么', '这样', '是不是', '因为', '所以'
}
BAD_TITLE_HINTS = {
    '这CC它怎么像个反骨仔一样啊', '虽然没这么离谱', '血亏', '爱马仕', '小龙虾', 'share',
    '卧槽', '真他妈给更新啊', '是不太行', '他听不懂话', '综合讨论'
}


def _clean_text(text: str) -> str:
    text = (text or '').replace('!图片', '').replace('!视频', '').replace('!文件', '').replace('\n', ' ')
    return ' '.join(text.split()).strip()


def _joined(theme: dict[str, Any]) -> str:
    return ' '.join([
        _clean_text(theme.get('title', '')),
        _clean_text(theme.get('summary', '')),
        ' '.join(theme.get('tags', []) or []),
        ' '.join(_clean_text(q.get('content', '')) for q in (theme.get('quotes', []) or [])[:4]),
    ])


def _theme_kind(theme: dict[str, Any]) -> str:
    text = _joined(theme)
    low = text.lower()
    if any(k in text for k in ['发布', '更新', '新版本', '上线', '修复']) or 'pr #' in low or 'release' in low:
        return '发布更新'
    if any(k in text for k in ['问题', '报错', 'bug', '卡顿', '错位', '粘贴', '溢出', '限制', '额度']) or 'bug' in low:
        return '问题反馈'
    if any(k in text for k in ['怎么用', '用途', '推荐', 'prompt', '入门', '能做什么', '体验']) or 'prompt' in low:
        return '使用讨论'
    if any(k in text for k in ['agent', '工作流', '编排', '架构', '多 worker', 'callback', '链式']) or 'agent' in low:
        return '能力演进'
    return '日常讨论'


def _candidate_phrases(theme: dict[str, Any]) -> list[str]:
    phrases: list[str] = []
    raw_parts = [theme.get('title', ''), theme.get('summary', '')]
    for q in (theme.get('quotes', []) or [])[:4]:
        raw_parts.append(q.get('content', ''))
    for raw in raw_parts:
        cleaned = _clean_text(raw)
        for seg in cleaned.replace('：', ' ').replace(':', ' ').replace('/', ' ').replace('；', ' ').replace('|', ' ').split():
            seg = seg.strip(' ，。,.!?！？[]【】()（）-"“”')
            if len(seg) < 2 or seg in STOPWORDS or seg in BAD_TITLE_HINTS:
                continue
            if seg.startswith('http'):
                continue
            if seg not in phrases:
                phrases.append(seg)
    return phrases


def _editor_title(theme: dict[str, Any]) -> str:
    text = _joined(theme)
    low = text.lower()
    kind = _theme_kind(theme)
    tags = set(theme.get('tags', []) or [])

    if 'pr #411' in low or 'tui v2' in low:
        return 'GA PR #411 发布与 TUI v2 升级'
    if '链式调用' in text or 'callback' in low:
        return '多 Agent 链式调用测试与工作流编排'
    if any(k in text for k in ['额度', '重置']) or '限额' in text:
        return '额度重置与使用限制反馈'
    if any(k in text for k in ['源码曝光', '官方同步', '更新好快']) or '开源' in text:
        return '开源版本更新速度与能力对比讨论'
    if any(k in text for k in ['怎么买', '在哪里买', '怎么用', 'cc和cx']) or '新手' in text or '入门' in text:
        return '新用户入门与购买使用讨论'
    if any(k in text for k in ['prompt', '溢出', '卡顿', '错位', '粘贴']) or ('prompt' in low and kind == '问题反馈'):
        return '长 Prompt 输入问题与体验反馈'
    if 'agent' in low and any(k in text for k in ['编排', '工作流', 'callback', '链式']):
        return 'Agent 编排能力与协作方式讨论'
    if 'share' in tags and any(k in text for k in ['对比', '框架', '爱马仕', '小龙虾']):
        return 'AI Agent 框架对比与选型讨论'

    phrases = [p for p in _candidate_phrases(theme) if p not in {'share'}]
    base = '、'.join(phrases[:2]) if phrases else ''
    if not base:
        return {
            '发布更新': '产品发布与版本进展',
            '问题反馈': '使用问题与反馈汇总',
            '使用讨论': '使用方式与适用场景讨论',
            '能力演进': '能力演进与工作流讨论',
            '日常讨论': '群内集中讨论',
        }[kind]
    if kind == '发布更新':
        return f'{base} 发布与进展'
    if kind == '问题反馈':
        return f'{base} 问题与反馈'
    if kind == '使用讨论':
        return f'{base} 使用讨论'
    if kind == '能力演进':
        return f'{base} 能力演进讨论'
    return f'{base} 讨论'


def _pick_points(theme: dict[str, Any], limit: int = 3) -> list[str]:
    points: list[str] = []
    for q in theme.get('quotes', []) or []:
        content = _clean_text(q.get('content', ''))
        if len(content) < 8:
            continue
        if content not in points:
            points.append(content)
        if len(points) >= limit:
            break
    summary = _clean_text(theme.get('summary', ''))
    if summary and summary not in points:
        points.append(summary)
    return points[:limit]


def _summary(theme: dict[str, Any]) -> str:
    points = _pick_points(theme, 3)
    msg_count = theme.get('message_count', 0)
    participants = theme.get('participant_count', 0)
    kind = _theme_kind(theme)
    if not points:
        return f'该话题当天有 {msg_count} 条相关消息，参与人数 {participants}，但讨论比较分散。'
    lead = {
        '发布更新': '今天群里有一条比较集中的更新线，',
        '问题反馈': '今天群里出现了一波比较明确的问题反馈，',
        '使用讨论': '今天群里围绕使用方式和适用场景有较多交流，',
        '能力演进': '今天群里关于 Agent 能力、协作方式和工作流编排的讨论比较集中，',
        '日常讨论': '今天群里围绕一个相对集中的话题展开了交流，',
    }[kind]
    body = '；'.join(points[:2])
    tail = f'相关消息 {msg_count} 条，参与人数 {participants}。'
    return f'{lead}{body}。{tail}'


def _highlight_line(theme: dict[str, Any]) -> str:
    title = _editor_title(theme)
    points = _pick_points(theme, 1)
    if not points:
        return title
    point = points[0]
    for bad in [
        '这CC它怎么像个反骨仔一样啊',
        '虽然没这么离谱，但是他干过那种把限制放松的操作，就是一个变量数值要求在0.94和1.06之间，他直接给放宽了',
        '链接|爱马仕 vs 小龙虾：两大开源 AI Agent 框架深度对比',
    ]:
        if point == bad:
            point = ''
            break
    return f'{title}: {point[:60]}' if point else title


def render_human_daily(view: dict[str, Any], norm: dict[str, Any]) -> str:
    themes = sorted(list(view.get('themes', []) or []), key=lambda x: (x.get('score', 0), x.get('message_count', 0)), reverse=True)
    focus = themes[:5]
    highlights = focus[:3]
    tail = view.get('tail_messages', []) or []
    tail_ctr = Counter(x.get('kind', '观点') for x in tail)

    lines = [
        '【今日讨论日报】',
        '',
        '今日亮点',
        '',
        '今天群里最值得关注的有几件事：',
        '',
    ]
    if highlights:
        for i, th in enumerate(highlights, 1):
            lines.append(f'{i}. {_highlight_line(th)}')
    else:
        lines.append('1. 今日暂无特别集中的高热讨论')

    lines.extend(['', '-------------------------------------', ''])

    for i, th in enumerate(focus, 1):
        lines.append(f'{i}. {_editor_title(th)}')
        lines.append('')
        lines.append(_summary(th))
        lines.append('')
        for q in (th.get('quotes', []) or [])[:3]:
            sender = (q.get('sender') or '群友').strip() or '群友'
            content = _clean_text(q.get('content', ''))
            if content:
                lines.append(f'[{sender}] {content}')
        lines.extend(['', '-------------------------------------', ''])

    if tail:
        lines.append('补充观察')
        lines.append('')
        lines.append(
            f'除重点主题外，群里还有一些值得记录的零散信息：高价值结论 {tail_ctr.get("结论", 0)} 条，风险/避坑 {tail_ctr.get("风险", 0)} 条，资源分享 {tail_ctr.get("分享", 0)} 条，单点观点 {tail_ctr.get("观点", 0)} 条。'
        )
        lines.append('')
        for item in tail[:5]:
            sender = item.get('sender', '群友')
            content = _clean_text(item.get('content', ''))
            if content:
                lines.append(f'- [{sender}] {content}')
        lines.extend(['', '-------------------------------------', ''])

    lines.append('数据概览')
    lines.append('')
    lines.append(f'今日共处理 {norm.get("total_msgs", 0)} 条消息，活跃人数 {norm.get("active_users", 0)}，识别出 {len(themes)} 个主题。')
    return '\n'.join(lines) + '\n'
