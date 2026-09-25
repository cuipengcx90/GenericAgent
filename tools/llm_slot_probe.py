#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_slot_probe —— 本机 LLM 槽位可用性台账与探活（只读，不改任何配置）

背景（TODO 第11批·评审 7.00）：2026-09-25 实测 agentmain 的 llm_no 1/3/5/6 直接 HTTP 403，
评审被迫只用 0/2/4 —— "哪个槽位今天能用"当时只能靠撞。本工具把它变成可复跑的机器判定。

设计约束（逐条对应验收）：
  1) **严格 ≤1 HTTP 请求/槽位**：复用 llmcore 真实传输路径，但探活前把会话副本的
     `max_retries` 强制归零（llmcore._stream_with_retry 是 `for attempt in range(max_retries+1)`）
     并关流式（`stream=False`），再设小超时。selftest 用本地假服务器逐场景**数请求次数**自证。
  2) **台账可复跑比对**：每次探活追加一行 JSON 到 temp/llm_slot_ledger.jsonl，`--ledger-diff`
     打印相邻两跑的 状态/分类 迁移（沿用 history_value 的教训：先写死口径再统计）。
  3) **只读**：不写任何配置；不给 mykey.py 打补丁；不切 agentmain 的当前槽位。
  4) **结论仅作决策输入**：工具只输出事实（状态/分类/耗时/错误头），不代做"该用哪个"的取舍。
  5) **密钥卫生**：任何输出（含错误正文）都过 mask_secrets()；apikey 一律不回显。

口径（两个编号域，勿混用 —— 混用会把"哪个槽位坏了"归因错）：
  * `llm_no` 域 = agentmain.load_llm_sessions() 的顺序（mykey dict 键序，键含 api/config/cookie 且非 mixin），
    其中 mixin 条目是一个整体（名字 = 各成员 name 用 '|' 连接）。这是 agentmain --llm_no 用的编号。
  * `slot` 域 = mykey.py:mixin_config['llm_nos'] 的顺序（0-based），即 MixinSession 内部故障转移次序。
    本工具 `--probe` 默认探 slot 域。

用法：
  python3 tools/llm_slot_probe.py --list                # 零成本：两个编号域映射表（不发请求）
  python3 tools/llm_slot_probe.py --dry-run             # 零成本：打印将要探活的目标与端点
  python3 tools/llm_slot_probe.py --probe               # 真探活（1 请求/槽位）
  python3 tools/llm_slot_probe.py --probe --only 0,3,5  # 只探指定 slot
  python3 tools/llm_slot_probe.py --probe --only deepseek-flash
  python3 tools/llm_slot_probe.py --probe --domain llmno --only 1,3
  python3 tools/llm_slot_probe.py --ledger --diff       # 看台账（末两跑差异）
  python3 tools/llm_slot_probe.py --selftest            # 本地假服务器自测（不发外网请求）
退出码：0=全部 OK / 1=存在非 OK 槽位 / 2=用法或环境错误 / 3=selftest 失败
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

GA_ROOT = Path(__file__).resolve().parent.parent
LEDGER = GA_ROOT / 'temp' / 'llm_slot_ledger.jsonl'

# ---------------------------------------------------------------------------
# 密钥卫生
# ---------------------------------------------------------------------------
_SECRETS: list[str] = []


def register_secrets(cfg_map: dict) -> None:
    """只登记不读取：把配置里的 apikey 值收集起来用于掩码，绝不回显。"""
    _SECRETS.clear()
    for v in (cfg_map or {}).values():
        if isinstance(v, dict):
            k = v.get('apikey') or v.get('api_key')
            if isinstance(k, str) and len(k) >= 8:
                _SECRETS.append(k)


_GENERIC_KEY = re.compile(r'\b(?:sk|cr|xoxb|ghp|glpat)[-_][A-Za-z0-9\-_]{6,}')


def mask_secrets(text) -> str:
    s = '' if text is None else str(text)
    for sec in _SECRETS:
        if sec and sec in s:
            s = s.replace(sec, '***KEY***')
    s = _GENERIC_KEY.sub('***KEY***', s)
    s = re.sub(r'(Bearer\s+)[A-Za-z0-9\-_\.]{6,}', r'\1***KEY***', s)
    return s


# ---------------------------------------------------------------------------
# 分类口径（先写死判据，再统计 —— 否则占比会失真）
# ---------------------------------------------------------------------------
# 类别 → 判据（按顺序匹配 llmcore 抛出的错误串 / HTTP 码）
CLASS_RULES = [
    ('AUTH', r'HTTP\s+(401|403)\b|unauthor|invalid api key|authentication|forbidden|permission'),
    ('QUOTA', r'HTTP\s+429\b|rate limit|quota|insufficient|exceeded your current|balance'),
    ('SERVER', r'HTTP\s+(5\d\d)\b|internal server|bad gateway|service unavailable'),
    ('PARAM', r'HTTP\s+400\b|HTTP\s+404\b|model not found|max_tokens|invalid_request|unknown model'),
    ('TIMEOUT', r'Timeout|timed out|ReadTimeout|ConnectTimeout'),
    # ★ 注意顺序：llmcore 把「HTTP 200 但无内容」也包成 ConnectionError("empty response")，
    #   故 EMPTY 必须排在 NETERR 之前，否则"端点活着但吐空"会被误报成"连不上"。
    ('EMPTY', r'empty response'),
    ('NETERR', r'ConnectionError|ProxyError|SSLError|NewConnectionError|NameResolution|refused|unreachable'),
    ('BADJSON', r'JSONDecodeError|Expecting value'),
]
CLASS_ORDER = [c for c, _ in CLASS_RULES]


def classify(err_text: str, ok: bool) -> str:
    if ok:
        return 'OK'
    t = err_text or ''
    for name, pat in CLASS_RULES:
        if re.search(pat, t, re.I):
            return name
    return 'UNKNOWN'


# ---------------------------------------------------------------------------
# llmcore 装载
# ---------------------------------------------------------------------------
def load_llmcore():
    if str(GA_ROOT) not in sys.path:
        sys.path.insert(0, str(GA_ROOT))
    import llmcore  # noqa: E402
    return llmcore


def mykey(module):
    cfg, _changed = module.reload_mykeys()
    register_secrets(cfg)
    return cfg


def eligible_keys(cfg_map: dict):
    """与 agentmain.load_llm_sessions 同款筛键（顺序即 llm_no 域顺序）。"""
    return [k for k in cfg_map if any(x in k for x in ('api', 'config', 'cookie'))]


def build_llmno_domain(module, cfg_map: dict):
    """复刻 agentmain 的 llmclients 顺序；返回 [(index, key, 展示名, 类型)]。"""
    rows = []
    for i, k in enumerate(eligible_keys(cfg_map)):
        if 'mixin' in k:
            names = cfg_map[k].get('llm_nos', [])
            rows.append({'llm_no': i, 'key': k, 'name': '|'.join(str(n) for n in names),
                         'kind': 'mixin', 'members': [str(n) for n in names]})
            continue
        try:
            c = module.resolve_client(k)
            name = getattr(getattr(c, 'backend', None), 'name', None) or cfg_map[k].get('name', k)
            rows.append({'llm_no': i, 'key': k, 'name': name, 'kind': 'session',
                         'cls': type(getattr(c, 'backend', None)).__name__})
        except Exception as e:  # agentmain 同样吞异常
            rows.append({'llm_no': i, 'key': k, 'name': f'BAD_{k}', 'kind': 'error',
                         'err': mask_secrets(type(e).__name__)})
    return rows


def provider_sessions(module, cfg_map: dict):
    """非 mixin 的 provider 会话，按 backend.name 索引。"""
    out = {}
    for k in eligible_keys(cfg_map):
        if 'mixin' in k:
            continue
        try:
            c = module.resolve_client(k)
        except Exception:
            continue
        if c is None:
            continue
        out[getattr(c.backend, 'name', k)] = c.backend
    return out


def slot_sessions(module, cfg_map: dict):
    """按 mixin_config['llm_nos'] 顺序取会话（= MixinSession 内部次序）。"""
    cfg = cfg_map.get('mixin_config') or {}
    names = [str(n) for n in cfg.get('llm_nos', [])]
    pool = provider_sessions(module, cfg_map)
    rows = []
    for i, n in enumerate(names):
        rows.append({'slot': i, 'name': n, 'session': pool.get(n),
                     'missing': pool.get(n) is None})
    return rows


def endpoint_of(session) -> str:
    """按 llmcore 的真实拼 URL 规则算端点（只出 host+path，无密钥）。"""
    base = getattr(session, 'api_base', '') or ''
    cls = type(session).__name__
    if cls in ('ClaudeSession', 'NativeClaudeSession'):
        path = 'messages'
    elif getattr(session, 'api_mode', '') == 'responses':
        path = 'responses'
    else:
        path = 'chat/completions'
    try:
        sys.path.insert(0, str(GA_ROOT))
        from llmcore import auto_make_url
        return auto_make_url(base, path)
    except Exception:
        return f'{base}/{path}'


def target_info(row, session) -> dict:
    d = {'slot': row.get('slot'), 'name': row.get('name')}
    if session is None:
        d.update(model=None, cls=None, endpoint=None, missing=True)
        return d
    d.update(model=getattr(session, 'model', None),
             cls=type(session).__name__,
             api_mode=getattr(session, 'api_mode', None),
             endpoint=endpoint_of(session),
             missing=False)
    return d


# ---------------------------------------------------------------------------
# 探活（≤1 请求/槽位）
# ---------------------------------------------------------------------------
PROMPT = 'Reply with exactly: OK'
PROBE_MAX_TOKENS = 32


def ask_stream(session, prompt):
    """统一 ask 入口：llmcore 里两类签名并存，用错就 0 请求直接 AssertionError。

    - BaseSession.ask(prompt: str)        —— LLMSession / ClaudeSession / MixinSession
    - NativeClaudeSession.ask(msg: dict)  —— NativeClaudeSession / NativeOAISession
                                             （函数体首行即 `assert type(msg) is dict`）
    判据用 isinstance（不是名字猜测）；模块从类的 __module__ 取，避免重复 import。
    """
    mod = sys.modules.get(type(session).__module__)
    native = getattr(mod, 'NativeClaudeSession', None)
    if native is not None and isinstance(session, native):
        return session.ask({'role': 'user', 'content': [{'type': 'text', 'text': prompt}]})
    return session.ask(prompt)


def probe_one(session, timeout: float = 20.0, prompt: str = PROMPT, max_tokens: int = PROBE_MAX_TOKENS):
    """对会话副本发**一次**最小请求。返回 dict（status/cls/ms/err）。"""
    s = copy.copy(session)                      # 不污染调用方的会话对象
    s.max_retries = 0                           # ★ 硬保证：llmcore 的 for attempt in range(max_retries+1)
    s.stream = False                            # 关流式（JSON 一次性返回）
    s.read_timeout = max(5, int(timeout))
    s.connect_timeout = max(2, min(10, int(timeout)))
    s.max_tokens = max_tokens
    if getattr(s, 'history', None):
        s.history = []
    t0 = time.time()
    chunks, err = [], ''
    try:
        for c in ask_stream(s, prompt):
            if isinstance(c, str) and c.lstrip().startswith('!!!Error:'):
                err = c
            else:
                chunks.append(c if isinstance(c, str) else '')
    except Exception as e:                      # 兜底：llmcore 之外的异常
        err = f'!!!Error: {type(e).__name__}: {e}'
    ms = int((time.time() - t0) * 1000)
    text = mask_secrets(''.join(chunks)).strip()
    ok = (not err) and bool(text)
    if not ok and not err:
        err = '!!!Error: EMPTY_RESPONSE'
    return {'status': 'ok' if ok else 'fail',
            'cls': classify(err, ok),
            'ms': ms,
            'out_len': len(text),
            'err': mask_secrets(err)[:300],
            'retries_forced': 0}


def run_probe(targets, timeout: float, dry_run: bool = False, requests_counter=None):
    """targets = [{'slot'/'llm_no', 'name', 'session', ...}]"""
    results, budget = [], len(targets)
    for t in targets:
        info = {k: v for k, v in t.items() if k != 'session'}
        if dry_run:
            info.update(status='skipped', cls='DRY_RUN', ms=0, err='')
            results.append(info)
            continue
        if t.get('session') is None:
            info.update(status='fail', cls='UNKNOWN', ms=0, err='no session built for this name')
            results.append(info)
            continue
        r = probe_one(t['session'], timeout=timeout)
        if requests_counter is not None:
            requests_counter.append(t.get('name'))
        info.update(r)
        results.append(info)
    assert len(requests_counter if requests_counter is not None else results) <= budget + 0
    return results


def load_ledger():
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def append_ledger(entry: dict):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open('a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def ledger_diff(entries, tail=2):
    """比较末两跑的逐目标 状态/分类 迁移。"""
    runs = entries[-tail:]
    if len(runs) < 2:
        return None
    def key(e):
        return f"{e.get('slot', e.get('llm_no'))}:{e.get('name')}"
    old = {key(e): e for e in runs[0].get('results', [])}
    new = {key(e): e for e in runs[1].get('results', [])}
    changes = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a is None:
            changes.append((k, 'NEW', b.get('cls')))
        elif b is None:
            changes.append((k, 'GONE', a.get('cls')))
        elif (a.get('status'), a.get('cls')) != (b.get('status'), b.get('cls')):
            changes.append((k, f"{a.get('status')}/{a.get('cls')}", f"{b.get('status')}/{b.get('cls')}"))
    return {'runs': [r.get('ts') for r in runs], 'changes': changes}


# ---------------------------------------------------------------------------
# selftest：本地假服务器，逐场景数请求次数
# ---------------------------------------------------------------------------
def _mk_handler(counter, scenario_delay=None):
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):  # 静音
            return

        def _scenario(self):
            seg = self.path.split('/')
            return seg[1] if len(seg) > 1 and seg[1] else 'ok'

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            sc = self._scenario()
            counter.append({'path': self.path, 'scenario': sc, 'bytes': len(body)})
            if sc == 'slow':
                time.sleep(scenario_delay or 5)
            code, payload = {
                'ok': (200, {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}], 'usage': {}}),
                'claude': (200, {'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn'}),
                '403': (403, {'error': {'message': 'invalid api key provided'}}),
                '429': (429, {'error': {'message': 'rate limit exceeded'}}),
                '500': (500, {'error': 'internal server error'}),
                '400': (400, {'error': {'message': 'model not found'}}),
                'badjson': (200, 'not json at all'),
                'empty': (200, {'choices': []}),
                'responses': (200, {'output': [{'type': 'message',
                                                'content': [{'type': 'output_text', 'text': 'OK'}]}],
                                    'usage': {}}),
            }[sc]
            data = payload if isinstance(payload, str) else json.dumps(payload)
            raw = data.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass

    return H


def selftest() -> int:
    from http.server import ThreadingHTTPServer
    module = load_llmcore()
    cfg_map = mykey(module)
    fatal = []

    counter = []
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _mk_handler(counter, scenario_delay=5))
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    def sess(scenario, cls='LLMSession', **over):
        cfg = {'apikey': 'sk-SELFTEST-DUMMY-000000', 'apibase': f'http://127.0.0.1:{port}/{scenario}',
               'model': 'selftest-model', 'name': f'selftest-{scenario}', 'api_mode': 'chat_completions'}
        cfg.update(over)
        s = getattr(module, cls)(cfg=cfg)
        s.max_retries = 9          # ★ 故意设大：证明 probe_one 会强制归零
        return s

    checks = []

    def chk(label, cond, detail=''):
        checks.append((label, bool(cond), detail))
        if not cond:
            fatal.append(label)

    # 1) 1 请求/槽位（含 5xx 重试场景）：max_retries=9 也必须只发 1 次
    for sc, want_cls in [('ok', 'OK'), ('403', 'AUTH'), ('429', 'QUOTA'), ('500', 'SERVER'),
                         ('400', 'PARAM'), ('badjson', 'BADJSON'), ('empty', 'EMPTY')]:
        before = len(counter)
        r = probe_one(sess(sc), timeout=10)
        hits = len(counter) - before
        chk(f'请求次数=1 [{sc}]', hits == 1, f'hits={hits}')
        chk(f'分类正确 [{sc}]', r['cls'] == want_cls, f'got={r["cls"]} err={r["err"][:90]}')

    # 2) Claude 模式端点（/v1/messages）+ 分类
    before = len(counter)
    r = probe_one(sess('claude', cls='ClaudeSession'), timeout=10)
    hits = len(counter) - before
    chk('Claude 会话请求次数=1', hits == 1, f'hits={hits}')
    chk('Claude 端点=/v1/messages', counter[-1]['path'].endswith('/v1/messages'), counter[-1]['path'])
    chk('Claude 分类 OK', r['cls'] == 'OK', r['cls'])

    # 2b) 原生会话：真跑（mixin_config 的 llm_nos）走的就是这条路，
    #     它们的 ask(msg: dict) 与 BaseSession.ask(prompt: str) 签名不同——必须自测锁死，
    #     否则 0 请求直接 AssertionError 也能"全绿"。
    for cls_name, sc, am in [('NativeClaudeSession', 'claude', 'chat_completions'),
                             ('NativeOAISession', 'responses', 'responses')]:
        before = len(counter)
        r = probe_one(sess(sc, cls=cls_name, api_mode=am), timeout=10)
        hits = len(counter) - before
        chk(f'{cls_name} 请求次数=1', hits == 1, f'hits={hits}')
        chk(f'{cls_name} 分类 OK', r['cls'] == 'OK',
            f'cls={r["cls"]} err={r["err"][:90]}')

    # 3) 读超时 → TIMEOUT，且请求仍只有 1 次
    before = len(counter)
    r = probe_one(sess('slow'), timeout=2)
    hits = len(counter) - before
    chk('超时场景分类=TIMEOUT', r['cls'] == 'TIMEOUT', f'cls={r["cls"]} err={r["err"][:80]}')
    chk('超时场景请求次数=1', hits == 1, f'hits={hits}')

    # 4) 连接被拒 → NETERR（挑一个已关闭端口）
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    closed_port = s.getsockname()[1]
    s.close()
    dead = module.LLMSession(cfg={'apikey': 'sk-SELFTEST-DUMMY-000000',
                                  'apibase': f'http://127.0.0.1:{closed_port}', 'model': 'm', 'name': 'dead'})
    dead.max_retries = 9
    r = probe_one(dead, timeout=5)
    chk('连接被拒分类=NETERR', r['cls'] == 'NETERR', f'cls={r["cls"]} err={r["err"][:80]}')

    # 5) 负控：不归零 retries 时，5xx 会被重试 >1 次（证明归零这一条不是装饰）
    raw = module.LLMSession(cfg={'apikey': 'sk-SELFTEST-DUMMY-000000',
                                 'apibase': f'http://127.0.0.1:{port}/500', 'model': 'm', 'name': 'raw'})
    raw.max_retries = 2
    raw.stream = False
    raw.read_timeout, raw.connect_timeout = 5, 3
    raw.max_retry_after = 0.1
    before = len(counter)
    err, _ = '', ''
    for c in raw.ask('ping'):
        if isinstance(c, str) and c.lstrip().startswith('!!!Error:'):
            err = c
    hits_raw = len(counter) - before
    chk('负控：retries=2 时 5xx 被重试(hits>1)', hits_raw > 1, f'hits={hits_raw}')

    # 6) 密钥卫生：错误正文与掩码函数都不含 apikey
    leak = module.LLMSession(cfg={'apikey': 'sk-LEAKCANARY-1234567890',
                                  'apibase': f'http://127.0.0.1:{port}/403', 'model': 'm', 'name': 'leak'})
    r = probe_one(leak, timeout=5)
    chk('掩码：错误串含金丝雀键则不回显', 'sk-LEAKCANARY-1234567890' not in json.dumps(r, ensure_ascii=False),
        r['err'][:80])
    chk('掩码：通用 sk- 形态被替换', mask_secrets('x sk-abcdef123456 y') == 'x ***KEY*** y', mask_secrets('x sk-abcdef123456 y'))

    # 7) dry-run 零请求
    before = len(counter)
    run_probe([{'name': 'a', 'session': sess('ok')}, {'name': 'b', 'session': None}], timeout=5, dry_run=True)
    chk('dry-run 零请求', len(counter) == before, f'hits={len(counter)-before}')

    # 8) 台账 diff 逻辑（合成两跑）
    e1 = {'ts': 't1', 'results': [{'slot': 0, 'name': 'a', 'status': 'ok', 'cls': 'OK'},
                                  {'slot': 1, 'name': 'b', 'status': 'ok', 'cls': 'OK'}]}
    e2 = {'ts': 't2', 'results': [{'slot': 0, 'name': 'a', 'status': 'fail', 'cls': 'AUTH'},
                                  {'slot': 1, 'name': 'b', 'status': 'ok', 'cls': 'OK'},
                                  {'slot': 2, 'name': 'c', 'status': 'ok', 'cls': 'OK'}]}
    d = ledger_diff([e1, e2])
    migr = [c for c in d['changes'] if c[1] not in ('NEW', 'GONE')]
    chk('diff 抓到 1 处迁移', len(migr) == 1, str(d['changes']))
    chk('diff 抓到 NEW 目标', any(c[1] == 'NEW' for c in d['changes']), str(d['changes']))

    # 9) 分类判据表自身可枚举（口径先写死）
    chk('分类口径齐全', set(['OK'] + CLASS_ORDER) >= {'OK', 'AUTH', 'QUOTA', 'SERVER', 'PARAM', 'TIMEOUT', 'EMPTY', 'NETERR'},
        ','.join(['OK'] + CLASS_ORDER))

    srv.shutdown()
    passed = sum(1 for _, ok, _ in checks if ok)
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ''))
    print(f'--- selftest {passed}/{len(checks)} passed ---')
    return 0 if not fatal else 3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _targets(module, cfg_map, domain, only, timeout):
    only_set = None
    if only:
        parts = [p.strip() for p in str(only).split(',') if p.strip()]
        only_set = parts
    if domain == 'slot':
        rows = slot_sessions(module, cfg_map)
        targets = []
        for r in rows:
            if only_set and not (str(r['slot']) in only_set or r['name'] in only_set):
                continue
            d = target_info(r, r['session'])
            d['session'] = r['session']
            targets.append(d)
        return targets
    # llmno 域
    pool_map = {}
    for row in build_llmno_domain(module, cfg_map):
        if row['kind'] != 'session':
            continue
        try:
            c = module.resolve_client(row['key'])
            pool_map[row['llm_no']] = c.backend
        except Exception:
            pool_map[row['llm_no']] = None
    targets = []
    for row in build_llmno_domain(module, cfg_map):
        if only_set and not (str(row['llm_no']) in only_set or row['name'] in only_set):
            continue
        s = pool_map.get(row['llm_no']) if row['kind'] == 'session' else None
        d = {'llm_no': row['llm_no'], 'name': row['name'], 'kind': row['kind']}
        d.update(target_info({'slot': None, 'name': row['name']}, s))
        d['session'] = s
        targets.append(d)
    return targets


def cmd_list(module, cfg_map):
    print('== llm_no 域（agentmain.load_llm_sessions 顺序；--llm_no N 用这个编号）==')
    for row in build_llmno_domain(module, cfg_map):
        extra = '' if row['kind'] == 'session' else f" [{row['kind']}]"
        print(f"  llm_no={row['llm_no']:<2} key={row['key']:<22} name={row['name'][:70]}{extra}")
    print('== slot 域（mykey.py:mixin_config[\'llm_nos\'] 顺序；--probe 默认探这个）==')
    for r in slot_sessions(module, cfg_map):
        s = r['session']
        if s is None:
            print(f"  slot={r['slot']}  name={r['name']:<22} [!! 无对应 provider 会话，无法探活]")
            continue
        print(f"  slot={r['slot']}  name={r['name']:<22} model={getattr(s, 'model', '')!s:<22} "
              f"cls={type(s).__name__:<16} mode={getattr(s, 'api_mode', '')!s:<16} endpoint={endpoint_of(s)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description='本机 LLM 槽位可用性台账与探活（只读）')
    ap.add_argument('--list', action='store_true', help='零成本：打印两个编号域映射表')
    ap.add_argument('--dry-run', action='store_true', help='零成本：打印将探活的目标（不发请求）')
    ap.add_argument('--probe', action='store_true', help='真探活：1 请求/目标')
    ap.add_argument('--domain', choices=['slot', 'llmno'], default='slot')
    ap.add_argument('--only', default='', help='限定目标：slot 序号或名字，逗号分隔')
    ap.add_argument('--timeout', type=float, default=20.0, help='读超时秒（默认 20）')
    ap.add_argument('--ledger', action='store_true', help='打印台账摘要')
    ap.add_argument('--diff', action='store_true', help='配合 --ledger：打印末两跑迁移')
    ap.add_argument('--json', default='', help='把本次结果写入指定 JSON 路径')
    ap.add_argument('--selftest', action='store_true', help='本地假服务器自测（不发外网请求）')
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    module = load_llmcore()
    cfg_map = mykey(module)

    if args.ledger:
        entries = load_ledger()
        print(f'台账 = {LEDGER} （{len(entries)} 跑）')
        for e in entries[-5:]:
            rs = e.get('results', [])
            ok = sum(1 for r in rs if r.get('cls') == 'OK')
            print(f"  {e.get('ts')}  目标={len(rs)} OK={ok}  域={e.get('domain')}")
        if args.diff:
            d = ledger_diff(entries)
            print('末两跑迁移：' + ('（不足两跑）' if not d else ''))
            if d:
                for c in d['changes']:
                    print(f"  {c[0]:<28} {c[1]} -> {c[2]}" if len(c) == 3 else f"  {c}")
        return 0

    if args.list or not (args.probe or args.dry_run):
        cmd_list(module, cfg_map)
        if not (args.probe or args.dry_run):
            print('\n（未指定动作；--probe 真探活 / --dry-run 只打印 / --selftest 自测）')
        return 0

    targets = _targets(module, cfg_map, args.domain, args.only, args.timeout)
    if not targets:
        print('没有匹配的目标（检查 --only / --domain）', file=sys.stderr)
        return 2

    print(f'== 目标 {len(targets)} 个（域={args.domain}，预算=每目标 ≤1 请求）==')
    for t in targets:
        print(f"  {t.get('slot', t.get('llm_no'))}: {t['name']:<24} model={t.get('model')!s:<22} "
              f"endpoint={t.get('endpoint')}")

    counter = []
    t0 = time.time()
    results = run_probe(targets, timeout=args.timeout, dry_run=args.dry_run, requests_counter=counter)
    if not args.dry_run:
        assert len(counter) <= len(targets), f'请求预算被击穿: {len(counter)} > {len(targets)}'
    print(f'\n== 结果（耗时 {time.time()-t0:.1f}s，实际请求 {len(counter)} 次）==')
    for r in results:
        print(f"  {r.get('slot', r.get('llm_no')):<3} {r['name']:<24} {r['status']:<5} {r['cls']:<8} "
              f"{r.get('ms', 0):>6}ms  {r.get('err', '')[:110]}")

    if not args.dry_run:
        entry = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'domain': args.domain,
                 'targets': len(targets), 'http_requests': len(counter),
                 'tool': 'tools/llm_slot_probe.py', 'results': results}
        append_ledger(entry)
        print(f'\n台账已追加: {LEDGER}')
        if args.json:
            Path(args.json).write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding='utf-8')
            print(f'结果 JSON: {args.json}')
        bad = [r for r in results if r['cls'] != 'OK']
        print(f'汇总：OK {len(results)-len(bad)}/{len(results)}；非 OK: ' +
              (', '.join(f"{r['name']}={r['cls']}" for r in bad) if bad else '无'))
        return 1 if bad else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
