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
from llmcore import build_mixin_session  # noqa: E402
from reflect_scripts.reflect_common import notify_feishu  # noqa: E402
from reflect_scripts.notify_feishu_group import notify_group as notify_feishu_group  # noqa: E402
from reflect_scripts.notify_dingtalk_group import notify_group as notify_dingtalk_group  # noqa: E402

_LLM_TAG_PATS = [
    r"<" + t + r">.*?</" + t + r">"
    for t in ("thinking", "summary", "tool_use", "file_content")
]


def _strip_llm_tags(text: str) -> str:
    """Strip internal agent tags (e.g. <summary>) leaked from the system prompt.

    Mirrors frontends/fsapp.py::_clean so autonomous report pipelines never
    push raw <summary>/<thinking> blocks to Feishu/DingTalk.
    """
    for pat in _LLM_TAG_PATS:
        text = re.sub(pat, "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


OUT_DIR = ROOT / 'output'
STATE_PATH = OUT_DIR / '.trendradar_digest_state.json'
LOCK_DIR = OUT_DIR / '.trendradar_locks'

from tools.lan_ip import archive_url  # noqa: E402

ARCHIVE_URL = archive_url()

KEYWORDS = [
    'AI 画图 Stable Diffusion ComfyUI FLUX 大模型 GitHub 开源 编程',
    'LLM DeepSeek GPT Claude 开源工具 安全漏洞 GPU 芯片',
]

# Linux.do 社区热帖已原生内置到 TrendRadar(config.yaml 的 rss.feeds 里 id=linux-do，
# impersonate:chrome 走 curl_cffi 过 Cloudflare JA3 拦截)。digest 不自行抓取，
# 统一从 get_latest_rss 结果里按 feed_name「Linux.do 社区热帖」过滤取用。
# 注意：get_latest_rss 是全源混合按时间取 limit 条，V2EX 发帖极频繁会霸榜
# (limit=80 时 80 条里 V2EX 占 59、Linux.do 仅 6)，故 limit 调大到 400 让
# Linux.do 有足够条目进入候选(实测 limit=400 可取到 ~56 条 Linux.do)。


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


def _material_counts(material: dict[str, Any]) -> tuple[int, int]:
    """Return (keyword_hits, rss_count) so callers can tell whether the fetch is empty.

    A healthy TrendRadar returns hundreds of RSS items; an all-zero result means the
    upstream container was mid-restart / first-crawl window and returned success:True
    with empty data. See trendradar_sop §9.
    """
    kw_hits = 0
    for q in material.get('queries') or []:
        res = q.get('result') if isinstance(q, dict) else None
        if isinstance(res, dict):
            kw_hits += int(res.get('total') or len(res.get('results') or []) or 0)
    rss = material.get('rss')
    rss_count = 0
    if isinstance(rss, dict):
        summ = rss.get('summary') or {}
        rss_count = int(summ.get('total') or summ.get('returned') or len(rss.get('data') or []) or 0)
    return kw_hits, rss_count


def _material_is_empty(material: dict[str, Any]) -> bool:
    kw, rss = _material_counts(material)
    return kw == 0 and rss == 0


# 上游临时空窗时的退避重试节奏(秒)。TrendRadar 容器重启/首轮爬虫窗口会短暂返回
# success:True + 空 data,单次抓取会误判"无情报"。见 trendradar_sop §9。
_EMPTY_RETRY_BACKOFF = (8, 20, 40)


def fetch_material(period: str) -> dict[str, Any]:
    """Fetch TrendRadar material, retrying on all-zero results (upstream empty window).

    A single all-zero fetch is almost never real (TrendRadar normally serves hundreds
    of items). Instead of shipping a degraded "no intel" report, retry with backoff and
    keep the best (non-empty) attempt. Records retry metadata on the material.
    """
    attempts = len(_EMPTY_RETRY_BACKOFF) + 1
    material: dict[str, Any] = {}
    for i in range(attempts):
        material = _fetch_material_once(period)
        kw, rss = _material_counts(material)
        material['_counts'] = {'keyword_hits': kw, 'rss': rss}
        material['_attempt'] = i + 1
        if not _material_is_empty(material):
            return material
        if i < len(_EMPTY_RETRY_BACKOFF):
            wait = _EMPTY_RETRY_BACKOFF[i]
            print(f'[fetch_material] empty result (kw=0,rss=0), attempt {i + 1}/{attempts}, '
                  f'retrying in {wait}s (upstream likely mid-restart)', file=sys.stderr)
            time.sleep(wait)
    material['_all_empty_after_retries'] = attempts
    print(f'[fetch_material] still empty after {attempts} attempts; upstream TrendRadar '
          f'has no data right now', file=sys.stderr)
    return material


def _fetch_material_once(period: str) -> dict[str, Any]:
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
            'limit': 400,
            'include_summary': False,
        })
        material['rss'] = _parse_json_maybe(_json_text(rss))
    except Exception as e:
        material['rss_error'] = f'{type(e).__name__}: {e}'

    return material


class ReportQualityError(RuntimeError):
    def __init__(self, msg: str, report: str = ''):
        super().__init__(msg)
        self.report = report  # 保留失败时的真实生成文本，便于人工核查/降级兜底


def _validate_report_quality(period: str, report: str) -> None:
    date_str = datetime.now().strftime('%Y-%m-%d')
    title_word = '晨报' if period == 'morning' else '晚报'
    required = [
        f'IT情报{title_word}',
        date_str,
        '📡 数据来源：TrendRadar',
        ARCHIVE_URL,
    ]
    bad_markers = [
        '流异常中断',
        '未收到完整响应',
        '[!!!',
    ]
    missing = [s for s in required if s not in report]
    markers = [s for s in bad_markers if s in report]
    if markers:
        raise ReportQualityError(f'report contains stream interruption marker: {markers}', report=report)
    if missing:
        raise ReportQualityError(f'report missing required parts: {missing}', report=report)
    if len(report.strip()) < 1800:
        raise ReportQualityError(f'report too short: {len(report.strip())} chars', report=report)


def build_report(period: str, material: dict[str, Any]) -> str:
    date_str = datetime.now().strftime('%Y-%m-%d')
    title_word = '晨报' if period == 'morning' else '晚报'
    icon = '🌅' if period == 'morning' else '🌙'
    tail = '早安 ☀️' if period == 'morning' else '晚安 🌙'
    period_hint = '今日最值得关注' if period == 'morning' else '今天下午以后/最近更新优先'

    prompt = f"""你是技术情报编辑。请基于下面 TrendRadar MCP 数据（RSS 已含 Linux.do 社区热帖），生成中文 IT 情报{title_word}。

要求：
1. 标题必须是：{icon} **IT情报{title_word} · {date_str}**
2. 挑选 8-12 条，按分类组织：AI画图 / 大模型 / 开源&工具 / 安全 / 芯片GPU。没有内容的分类可省略。
3. 每条格式：▸ 标题（来源）\n  简要1句话说明为什么重要。
4. 另起一个固定分类「💬 Linux.do 社区热议」：数据只从下面 JSON 的 RSS 数据里 feed_name 为「Linux.do 社区热帖」的条目中取（字段为 title/url）。从中挑 6-8 条技术/AI/开源/工具/羊毛资讯相关的热帖；若有效技术向条目不足 6 条，可少于 6 条但不要用纯闲聊/情绪贴（如情感、吐槽、生活流水）凑数。格式：▸ 帖子标题（linux.do）\n  附原帖链接(url 字段)。若没有该来源的条目，则省略该分类，不要虚构。
5. {period_hint}；合并重复话题，不要虚构来源、版本号、公司公告。
6. 末尾必须包含：\n📡 数据来源：TrendRadar | {tail}\n📚 历史归档全览：{ARCHIVE_URL}\n（同 WiFi 且本机开机时可访问）
7. 只输出报告正文，不要输出过程说明。

数据 JSON（TrendRadar 关键词命中 + RSS 全量，其中 RSS 已含 Linux.do 社区热帖来源）：
{json.dumps(material, ensure_ascii=False, default=str)[:60000]}
"""
    sess = build_mixin_session()
    print(f'🤖 报告生成（统一 Mixin：{sess.name}）')
    msg = {'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}
    report = _strip_llm_tags(''.join(sess.ask(msg))).strip()
    if f'IT情报{title_word}' not in report:
        report = f'{icon} **IT情报{title_word} · {date_str}**\n\n' + report
    if ARCHIVE_URL not in report:
        report = report.rstrip() + f'\n\n📚 历史归档全览：{ARCHIVE_URL}\n（同 WiFi 且本机开机时可访问）'
    _validate_report_quality(period, report)
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
        material_path.write_text(json.dumps(material, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

        # 重试后仍全空 = 上游 TrendRadar 确实没数据。不推"暂无情报"降级文案到正式渠道,
        # 也不认领当日 state,让后续定时轮次/手动补发仍能发出真正有内容的报告。见 §9。
        if _material_is_empty(material):
            kw, rss = _material_counts(material)
            print(f'[run] material still empty after retries (kw={kw},rss={rss}); '
                  f'skip sending degraded report', file=sys.stderr)
            return {
                'period': period,
                'material_path': str(material_path),
                'skipped_empty': True,
                'attempts': material.get('_attempt'),
                'send': {'skipped': True, 'reason': 'upstream empty after retries'},
            }

        report_path = OUT_DIR / f'trendradar_{period}_{date_str}.txt'
        last_error = None
        report = ''
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                report = build_report(period, material)
                break
            except ReportQualityError as e:
                last_error = f'{type(e).__name__}: {e}'
                last_report = e.report
                print(f'[run] report quality failed, attempt {attempt}/{max_attempts}: {last_error}', file=sys.stderr)
                if attempt >= max_attempts:
                    fail_path = OUT_DIR / f'trendradar_{period}_{date_str}.failed.txt'
                    fail_path.write_text(last_report or last_error or '', encoding='utf-8')
                    title_word = '晨报' if period == 'morning' else '晚报'
                    # 只有"内容太短"这一种失败原因、且确实拿到了非空正文时才降级发送；
                    # 缺失必需片段/流中断标记仍视为真正的生成故障，继续跳过不发。
                    only_too_short = (
                        'too short' in str(e)
                        and 'missing required parts' not in str(e)
                        and 'stream interruption' not in str(e)
                    )
                    if only_too_short and last_report.strip():
                        report = (
                            f'⚠️（今日素材较少，篇幅低于常规标准，仍完整发送）\n\n'
                            + last_report
                        )
                        report_path.write_text(report, encoding='utf-8')
                        send_result = send_once(period, report, dry_run=dry_run, force=force)
                        return {
                            'period': period,
                            'report_path': str(report_path),
                            'material_path': str(material_path),
                            'chars': len(report),
                            'degraded_send': True,
                            'degrade_reason': last_error,
                            'send': send_result,
                        }
                    alert = (
                        f'⚠️ TrendRadar {title_word}生成失败（{date_str}），已跳过推送\n'
                        f'重试 {max_attempts} 次后仍未通过质量校验，最后一次错误：\n{last_error}\n'
                        f'详情见: {fail_path}'
                    )

                    try:
                        notify_feishu(alert)
                    except Exception as notify_err:
                        print(f'[run] failure alert also failed to send: {notify_err}', file=sys.stderr)
                    return {
                        'period': period,
                        'material_path': str(material_path),
                        'report_failed_path': str(fail_path),
                        'skipped_bad_report': True,
                        'send': {'skipped': True, 'reason': 'bad_report_quality', 'error': last_error},
                    }
                # 504/网关抖动通常几十秒到几分钟内会恢复，重试前退避等待
                time.sleep(20 * attempt)
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
