#!/usr/bin/env python3
"""Deterministic TrendRadar morning/evening digest runner.

Why this exists:
- sche_tasks/*.json are executed as free-form LLM prompts by reflect scheduler.
- The old prompt allowed the agent to first call TrendRadar MCP send_notification
  and then GA native notify modules, causing duplicate Feishu DM pushes.
- This script is the single push entrypoint: MCP is used only for data, GA native
  modules are used only for notifications, and a per-day/per-period state file
  prevents duplicate sends.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path('/Users/cuip/projects/GenericAgent')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ga_mcp_client import call_sync, list_tools_sync  # noqa: E402
from llmcore import NativeClaudeSession, mykeys  # noqa: E402
from reflect_scripts.reflect_common import notify_feishu  # noqa: E402
from reflect_scripts.notify_feishu_group import notify_group as notify_feishu_group  # noqa: E402
from reflect_scripts.notify_dingtalk_group import notify_group as notify_dingtalk_group  # noqa: E402

OUT_DIR = ROOT / 'output'
STATE_PATH = OUT_DIR / '.trendradar_digest_state.json'
LOCK_DIR = OUT_DIR / '.trendradar_locks'
ARCHIVE_URL = 'http://192.168.1.40:8899/'

KEYWORDS = [
    'AI 画图 Stable Diffusion ComfyUI FLUX 大模型 GitHub 开源 编程',
    'LLM DeepSeek GPT Claude 开源工具 安全漏洞 GPU 芯片',
]

# Linux.do 社区热帖已原生内置到 TrendRadar(config.yaml 的 rss.feeds 里 id=linux-do，
# impersonate:chrome 走 curl_cffi 过 Cloudflare JA3 拦截)。此处不再自行抓取，
# 数据统一从 get_latest_rss 返回的 material['rss'] 里取(feed_name「Linux.do 社区热帖」)。


def _json_text(obj: Any) -> str:
    """Extract readable text from MCP result content."""
    if isinstance(obj, list):
        parts = []
        for item in obj:
            if isinstance(item, dict):
                parts.append(str(item.get('text') or item.get('data') or item))
            else:
                parts.append(str(item))
        return '\n'.join(parts)
    return json.dumps(obj, ensure_ascii=False, default=str)


def _parse_json_maybe(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return text


# ---------------------------------------------------------------------------
# Raw single-session MCP transport (Streamable HTTP).
#
# Why: ga_mcp_client.call_sync/list_tools_sync open a NEW mcp-python async
# session per call. Against the flaky trendradar server (docker container we
# cannot restart) this churn intermittently hangs session.initialize()
# forever. curl-style raw requests over a single reused session are reliable
# (see trendradar_sop sections 1/6/7). We keep ONE session for the whole
# fetch: initialize -> notifications/initialized -> tools/list -> tool calls.
# ---------------------------------------------------------------------------
import requests  # noqa: E402

MCP_URL = 'http://127.0.0.1:3333/mcp'
_MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}


def _mcp_parse(resp: 'requests.Response') -> dict:
    """Parse an MCP HTTP response body (plain JSON or SSE) into the JSON-RPC msg."""
    resp.encoding = 'utf-8'
    ctype = resp.headers.get('content-type', '')
    if 'text/event-stream' not in ctype:
        return json.loads(resp.text)
    # SSE: pick the data payload that carries a JSON-RPC result/error.
    chosen = None
    for line in resp.text.splitlines():
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict) and ('result' in obj or 'error' in obj):
            chosen = obj
    if chosen is None:
        raise RuntimeError(f'no JSON-RPC result in SSE body: {resp.text[:300]!r}')
    return chosen


def _mcp_open_session(timeout: float = 30.0) -> tuple['requests.Session', dict]:
    s = requests.Session()
    init = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'trendradar-digest', 'version': '1.0'},
        },
    }
    r = s.post(MCP_URL, json=init, headers=_MCP_HEADERS, timeout=timeout)
    r.raise_for_status()
    hdr = dict(_MCP_HEADERS)
    sid = r.headers.get('Mcp-Session-Id') or r.headers.get('mcp-session-id')
    if sid:
        hdr['Mcp-Session-Id'] = sid
    # Required handshake step: tell the server the client is initialized.
    s.post(MCP_URL, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
           headers=hdr, timeout=timeout)
    return s, hdr


def _mcp_list_tools(s: 'requests.Session', hdr: dict, timeout: float = 30.0) -> set[str]:
    r = s.post(MCP_URL, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
               headers=hdr, timeout=timeout)
    r.raise_for_status()
    msg = _mcp_parse(r)
    if 'error' in msg:
        raise RuntimeError(f'tools/list error: {msg["error"]}')
    return {t.get('name') for t in msg.get('result', {}).get('tools', [])}


_MCP_ID = [2]


def _mcp_call(s: 'requests.Session', hdr: dict, name: str, args: dict, timeout: float = 120.0) -> Any:
    _MCP_ID[0] += 1
    payload = {'jsonrpc': '2.0', 'id': _MCP_ID[0], 'method': 'tools/call',
               'params': {'name': name, 'arguments': args}}
    r = s.post(MCP_URL, json=payload, headers=hdr, timeout=timeout)
    r.raise_for_status()
    msg = _mcp_parse(r)
    if 'error' in msg:
        raise RuntimeError(f'{name} error: {msg["error"]}')
    result = msg.get('result', {})
    # tools/call result is typically {content: [{type:'text', text:'...'}], ...}
    return result.get('content', result)


def fetch_material(period: str) -> dict[str, Any]:

    """Fetch TrendRadar data over a single raw MCP session. No notification tool here."""
    s, hdr = _mcp_open_session()
    tools = _mcp_list_tools(s, hdr)
    if 'search_news' not in tools or 'get_latest_rss' not in tools:
        raise RuntimeError(f'TrendRadar MCP tools missing: need search_news/get_latest_rss, got {sorted(tools)}')

    material: dict[str, Any] = {'period': period, 'queries': [], 'rss': None, 'fetched_at': datetime.now().isoformat()}
    for query in KEYWORDS:
        try:
            res = _mcp_call(s, hdr, 'search_news', {
                'query': query,
                'search_mode': 'keyword',
                'date_range': '今天',
                'limit': 30,
                'sort_by': 'relevance',
                'include_url': True,
                'include_rss': True,
                'rss_limit': 30,
            })
            material['queries'].append({'query': query, 'result': _parse_json_maybe(_json_text(res))})
        except Exception as e:
            material['queries'].append({'query': query, 'error': f'{type(e).__name__}: {e}'})

    try:
        rss = _mcp_call(s, hdr, 'get_latest_rss', {
            'days': 1,
            'limit': 80,
            'include_summary': False,
        })
        material['rss'] = _parse_json_maybe(_json_text(rss))
    except Exception as e:
        material['rss_error'] = f'{type(e).__name__}: {e}'

    return material


def build_report(period: str, material: dict[str, Any]) -> str:
    date_str = datetime.now().strftime('%Y-%m-%d')
    title_word = '晨报' if period == 'morning' else '晚报'
    icon = '🌅' if period == 'morning' else '🌙'
    tail = '早安 ☀️' if period == 'morning' else '晚安 🌙'
    period_hint = '今日最值得关注' if period == 'morning' else '今天下午以后/最近更新优先'

    prompt = f"""你是技术情报编辑。请基于下面 TrendRadar MCP 数据 + Linux.do 社区热帖，生成中文 IT 情报{title_word}。

要求：
1. 标题必须是：{icon} **IT情报{title_word} · {date_str}**
2. 挑选 8-12 条，按分类组织：AI画图 / 大模型 / 开源&工具 / 安全 / 芯片GPU。没有内容的分类可省略。
3. 每条格式：▸ 标题（来源）\n  简要1句话说明为什么重要。
4. 另起一个固定分类「💬 Linux.do 社区热议」：从 RSS 数据里 feed_name 为「Linux.do 社区热帖」的条目中挑 3-5 条技术/AI/开源/羊毛资讯相关的热帖（跳过纯闲聊/情绪贴），格式：▸ 帖子标题（linux.do）\n  附原帖链接(url 字段)。若没有该来源的条目，则省略该分类，不要虚构。
5. {period_hint}；合并重复话题，不要虚构来源、版本号、公司公告。
6. 末尾必须包含：\n📡 数据来源：TrendRadar + Linux.do | {tail}\n📚 历史归档全览：{ARCHIVE_URL}\n（同 WiFi 且本机开机时可访问）
7. 只输出报告正文，不要输出过程说明。

数据 JSON（TrendRadar 关键词命中 + RSS 全量，其中 RSS 已含 Linux.do 社区热帖来源）：
{json.dumps(material, ensure_ascii=False, default=str)[:60000]}
"""
    cfg = next((v for k, v in mykeys.items() if 'native' in k and 'claude' in k), None)
    if not cfg:
        raise RuntimeError('No native claude config found in mykeys')
    sess = NativeClaudeSession(cfg=cfg)
    msg = {'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}
    report = ''.join(sess.ask(msg)).strip()
    if f'IT情报{title_word}' not in report:
        report = f'{icon} **IT情报{title_word} · {date_str}**\n\n' + report
    if ARCHIVE_URL not in report:
        report = report.rstrip() + f'\n\n📚 历史归档全览：{ARCHIVE_URL}\n（同 WiFi 且本机开机时可访问）'
    return report


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE_PATH)


def _acquire_lock(period: str) -> Path:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime('%Y-%m-%d')
    lock = LOCK_DIR / f'{period}_{date_str}.lock'
    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(f'pid={os.getpid()} at={datetime.now().isoformat()}\n')
    return lock


def _release_lock(lock: Path) -> None:
    try:
        lock.unlink()
    except FileNotFoundError:
        pass


def send_once(period: str, report: str, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    date_str = datetime.now().strftime('%Y-%m-%d')
    key = f'{date_str}:{period}'
    digest = hashlib.sha256(report.encode('utf-8')).hexdigest()
    state = _load_state()
    prior = state.get(key)
    if prior and prior.get('sent') and not force:
        return {'skipped': True, 'reason': 'already_sent_today', 'prior': prior}

    if dry_run:
        return {'skipped': True, 'reason': 'dry_run', 'sha256': digest}

    results: dict[str, Any] = {'skipped': False, 'sha256': digest, 'channels': {}}

    # CRASH-SAFE CLAIM (根治重复推送):
    # 先把 sent=True 认领标记落盘,再发通道。若进程在发送途中崩溃
    # (如 ChunkedEncodingError)且锁被 finally 释放,reflect 重试时
    # 第二次运行会读到 prior.sent=True 而 skip,绝不重发。
    # 仅当三通道全部失败时回滚认领,让后续重试能真正补发。
    state[key] = {
        'sent': True,
        'sha256': digest,
        'claimed_at': datetime.now().isoformat(),
        'channels': {},
    }
    _save_state(state)

    try:
        notify_feishu(report)
        results['channels']['feishu_dm'] = {'ok': True}
    except Exception as e:
        results['channels']['feishu_dm'] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    try:
        r = notify_feishu_group(report)
        results['channels']['feishu_group'] = {'ok': True, 'result': r}
    except Exception as e:
        results['channels']['feishu_group'] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    try:
        r = notify_dingtalk_group(report)
        results['channels']['dingtalk_group'] = {'ok': True, 'result': r}
    except Exception as e:
        results['channels']['dingtalk_group'] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    all_failed = all(not c.get('ok') for c in results['channels'].values())
    state = _load_state()
    if all_failed:
        # 一条都没发出去:回滚认领,允许后续重试补发
        state.pop(key, None)
        _save_state(state)
        results['all_failed'] = True
        return results

    state[key] = {
        'sent': True,
        'sha256': digest,
        'sent_at': datetime.now().isoformat(),
        'channels': results['channels'],
    }
    _save_state(state)
    return results


def run(period: str, dry_run: bool = False, force: bool = False, self_test: bool = False) -> dict[str, Any]:
    if period not in ('morning', 'evening'):
        raise ValueError('period must be morning or evening')
    if self_test:
        tools = {t.get('name') for t in list_tools_sync('trendradar')}
        assert 'search_news' in tools and 'get_latest_rss' in tools, sorted(tools)
        return {'self_test': True, 'tools_ok': True}

    lock = _acquire_lock(period)
    try:
        date_str = datetime.now().strftime('%Y-%m-%d')
        material = fetch_material(period)
        material_path = OUT_DIR / f'trendradar_{period}_{date_str}.material.json'
        report_path = OUT_DIR / f'trendradar_{period}_{date_str}.txt'
        material_path.write_text(json.dumps(material, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        report = build_report(period, material)
        report_path.write_text(report, encoding='utf-8')
        send_result = send_once(period, report, dry_run=dry_run, force=force)
        return {
            'period': period,
            'report_path': str(report_path),
            'material_path': str(material_path),
            'chars': len(report),
            'send': send_result,
        }
    finally:
        _release_lock(lock)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', choices=['morning', 'evening'], required=True)
    ap.add_argument('--dry-run', action='store_true', help='generate but do not send')
    ap.add_argument('--force', action='store_true', help='ignore same-day sent state')
    ap.add_argument('--self-test', action='store_true', help='validate imports and MCP tool availability only')
    args = ap.parse_args()
    result = run(args.period, dry_run=args.dry_run, force=args.force, self_test=args.self_test)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
