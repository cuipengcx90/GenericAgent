#!/usr/bin/env python3
"""跨会话重复劳动检测器 v1.2（只读 L4，流式，禁解压；R408 交付物，判据原文+实测见
memory/autonomous_reports/R408_跨会话重复劳动量化.md）。

v1（mention 共现）实测不可用：NC1 随机对 1/20 误报、整体 211 对/35.9%，
根因=tool 输出与记忆文件正文会把大量路径灌进会话 token 集（"都在读同一批记忆"≠重复劳动）。
v1.1 改用四类**可解释的强证据**（见 CRITERIA_V2.md）：
  A. DISPATCH  派单文本同源：两会话首个 Prompt 正文（剔除 [AUTO] 心跳横幅）4-gram Jaccard ≥ 0.6 且归一长度 ≥ 150
  B. CLAIM     同一队列项认领：前 40KB 内 `TODO#N「标题」` 归一化后（N,标题）相同
  C. REPORT    同一报告产物被两个会话各写一次：file_write/file_patch 的 path 字段相同（R*.md）
  D. SELFWRITE 同一自建产物被两个会话各写一次：file_write/file_patch 的 path 字段相同（任意文件）
判定: 时间可比(gap ≤ τ) ∧ (A ∨ B ∨ C ∨ D)；委托标记（双方前 4KB 含 子代理/subagent）单列豁免。

v1.2 三条口径修正（均由真库首跑暴露的系统性假阳性驱动）：
  1) 同一会话在多个月度 zip 里重复出现 ⇒ 按 id 去重（456 → 411）；
  2) 同起点分片（id 起点段相同，如 0519_0004-0519_0007/0008）是同一会话续写 ⇒ 跳过（按 1.1 口径白报 76 对）；
  3) 被 ≥8 个会话写过的文件是共享协议文件（TODO.txt/global_mem*.txt 等 6 个）⇒ 不作 D 类证据（1.1 里占 184 对中的 144）。

用法: python3 tools/repeat_labor_detect.py --selftest | --scan [--no-cache]
产物默认写 <GA>/temp/repeat_labor/out/（可用 REPEAT_LABOR_OUT 覆盖）；工作副本原在 temp/repeat_labor/detect2.py。
"""
import argparse, collections, glob, json, os, re, sys, random, zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_ga(start):
    d = start
    for _ in range(6):
        if os.path.isdir(os.path.join(d, 'memory', 'L4_raw_sessions')):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


GA = _find_ga(HERE) or os.environ.get('GA_ROOT')
if not GA:
    sys.exit('[error] 未找到 GA 根（含 memory/L4_raw_sessions 的目录）；可用 GA_ROOT=... 指定')
L4 = os.path.join(GA, 'memory', 'L4_raw_sessions')
OUT = os.environ.get('REPEAT_LABOR_OUT') or os.path.join(GA, 'temp', 'repeat_labor', 'out')
TAU = 7200
JACCARD_MIN = 0.6
DISP_MIN = 150

MEMBER_RE = re.compile(r'^(\d{2})(\d{2})_(\d{2})(\d{2})-(\d{2})(\d{2})_(\d{2})(\d{2})\.txt$')
ZIP_RE = re.compile(r'(\d{4})-(\d{2})\.zip$')
AUTO_BANNER = re.compile(r'\[AUTO\][^\n]{0,200}')
RPT_PATH_RE = re.compile(r'(?:temp|memory)/autonomous_reports/R\d+_[^\'"\s\\]{0,60}\.md')
CLAIM_RE = re.compile(r'TODO\s*#(\d+)\s*[「\u300c]?([^」\u300d\n]{4,60})')
DELEG = ('子代理', 'subagent', 'delegated')


def parse_member(name, year):
    m = MEMBER_RE.match(name)
    if not m:
        return None
    a = list(map(int, m.groups()))
    try:
        s = datetime(year, a[0], a[1], a[2], a[3])
        e = datetime(year, a[4], a[5], a[6], a[7])
    except ValueError:
        return None
    if e < s:
        e = e.replace(year=e.year + 1)
    return s, e


def norm(s):
    keep = []
    for ch in s:
        if ch.isalnum():
            keep.append(ch.lower())
        elif '\u4e00' <= ch <= '\u9fff':
            keep.append(ch)
    return ''.join(keep)


def unescape(raw):
    raw = raw.replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
    return raw


def dispatch_of(text):
    m = re.search(r'=== Prompt ===[^\n]*\n(.*?)=== Response ===', text, re.S)
    if not m:
        return None
    raw = unescape(m.group(1))[:4000]
    raw = AUTO_BANNER.sub(' ', raw)
    raw = re.sub(r'"(role|content|type|text)"\s*:\s*', ' ', raw)
    raw = re.sub(r'[\{\}\[\]"]', ' ', raw)
    n = norm(raw)
    return n if len(n) >= DISP_MIN else None


def ngrams(s, k=4):
    return {s[i:i + k] for i in range(max(0, len(s) - k + 1))}


def jaccard(a, b):
    A, B = ngrams(a), ngrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def cjk_ratio(s):
    if not s:
        return 0.0
    return sum(1 for c in s if '\u4e00' <= c <= '\u9fff') / len(s)


def claims_of(head):
    """只认 (TODO 号, 标题) 且标题须中文为主，避免抓到 markdown 表格等 ASCII 垃圾。"""
    out = set()
    for num, title in CLAIM_RE.findall(head):
        t = norm(title)
        if len(t) >= 6 and cjk_ratio(title) >= 0.6:
            out.add(num + '|' + t[:30])
    return out


WRITE_NAME_RE = re.compile(r"'name':\s*'file_(?:write|patch)'")
PATH_SQ_RE = re.compile(r"'path':\s*'((?:[^'\\]|\\.)*)'")


def writes_of(text):
    """严格版: 只取 file_write/file_patch 工具调用里 'path' 字段（单引号形式）。

    正文内容里的路径已被转义（\\' 或双引号形式）⇒ 不会被 PATH_SQ_RE 命中。
    返回全部自建/自改文件路径；R*.md 报告再单独挑出。
    """
    out, rpt = set(), set()
    for line in text.split('\n'):
        if 'file_' not in line:
            continue
        for m in WRITE_NAME_RE.finditer(line):
            pm = PATH_SQ_RE.search(line, m.end())
            if pm:
                p = pm.group(1)
                if len(p) >= 6 and '..' not in p:
                    out.add(p)
                rm = RPT_PATH_RE.search(p)
                if rm:
                    rpt.add(rm.group(0))
    return out, rpt


def extract2(text):
    w, rpt = writes_of(text)
    return {'disp': dispatch_of(text), 'claims': claims_of(text[:40000]),
            'rpt': rpt, 'writes': w,
            'deleg': any(k.lower() in text[:4096].lower() for k in DELEG)}


SHARED = set()          # 共享协议文件（被大量会话写入的记忆/台账/通道文件），由 scan 计算注入
SHARE_MIN = 8


def shared_files(sessions, k=SHARE_MIN):
    """写入会话数 >= k 的文件 = 共享协议文件，不作重复劳动证据"""
    from collections import Counter
    c = Counter()
    for s in sessions:
        for p in s['writes']:
            c[p] += 1
    return {p for p, n in c.items() if n >= k}, c


def judge(a, b):
    """返回 (dup, evidence dict)（不含时间门）"""
    ev = {}
    if a['disp'] and b['disp']:
        j = jaccard(a['disp'], b['disp'])
        if j >= JACCARD_MIN:
            ev['dispatch'] = round(j, 3)
    r = sorted(a['rpt'] & b['rpt'])
    if r:
        ev['report'] = r[:4]
    c = sorted(a['claims'] & b['claims'])
    if c:
        ev['claim'] = c[:4]
    w = sorted((a['writes'] & b['writes']) - SHARED)
    if w:
        ev['selfwrite'] = w[:4]
    return bool(ev), ev


def stem(sid):
    """会话 id 形如 0519_0004-0519_0007.txt，取起点段用于识别同一会话的分片/续写"""
    return sid.split('-')[0]


def pairs(sessions, tau, exempt=True):
    dups, deleg, cont, timed = [], [], 0, 0
    for i in range(len(sessions)):
        for j in range(i + 1, len(sessions)):
            a, b = sessions[i], sessions[j]
            if stem(a['id']) == stem(b['id']):
                cont += 1
                continue
            gap = max(0.0, (max(a['start'], b['start']) - min(a['end'], b['end'])).total_seconds())
            if gap > tau:
                timed += 1
                continue
            dup, ev = judge(a, b)
            if not dup:
                continue
            rec = {'a': a['id'], 'b': b['id'], 'gap_s': gap, 'ev': ev,
                   'delegated': bool(a['deleg'] and b['deleg'])}
            (deleg if (exempt and rec['delegated']) else dups).append(rec)
    pairs.stats = {'same_start_skipped': cont, 'time_skipped': timed,
                   'candidates': len(sessions) * (len(sessions) - 1) // 2}
    return dups, deleg


def dedupe(S):
    """同一成员可能在多个月度 zip 中重复出现 ⇒ 按 id 去重（保首个）"""
    seen, out = set(), []
    for s in S:
        if s['id'] in seen:
            continue
        seen.add(s['id']); out.append(s)
    return out


def load(use_cache=True):
    os.makedirs(OUT, exist_ok=True)
    cache = os.path.join(OUT, 'tokens_v11.json')
    if use_cache and os.path.exists(cache):
        S = json.load(open(cache, encoding='utf-8'))
        for s in S:
            s['start'] = datetime.fromisoformat(s['start']); s['end'] = datetime.fromisoformat(s['end'])
            s['claims'] = set(s['claims']); s['rpt'] = set(s['rpt']); s['writes'] = set(s['writes'])
        n0 = len(S); S = dedupe(S)
        print(f'[cache] sessions={n0} dedup={len(S)}')
        return S
    S = []
    for zp in sorted(glob.glob(os.path.join(L4, '*.zip'))):
        zm = ZIP_RE.search(os.path.basename(zp)); year = int(zm.group(1)) if zm else 2026
        n = nb = 0
        with zipfile.ZipFile(zp) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rng = parse_member(os.path.basename(info.filename), year)
                n += 1; nb += info.file_size
                if not rng:
                    continue
                x = extract2(zf.read(info).decode('utf-8', 'replace'))
                S.append({'id': os.path.basename(info.filename), 'start': rng[0], 'end': rng[1],
                          'disp': x['disp'], 'claims': x['claims'], 'rpt': x['rpt'],
                          'writes': x['writes'],
                          'deleg': x['deleg'], 'bytes': info.file_size})
        print(f'[zip] {os.path.basename(zp)} members={n} bytes={nb}')
    n0 = len(S); S = dedupe(S)
    print(f'[dedup] raw={n0} unique={len(S)}')
    json.dump([{**s, 'start': s['start'].isoformat(), 'end': s['end'].isoformat(),
                'claims': sorted(s['claims']), 'rpt': sorted(s['rpt']), 'writes': sorted(s['writes'])} for s in S],
              open(cache, 'w', encoding='utf-8'), ensure_ascii=False)
    return S


def scan(args):
    S = load(use_cache=not args.no_cache)
    global SHARED
    SHARED, wcnt = shared_files(S)
    print(f'[shared] files written by >={SHARE_MIN} sessions: {len(SHARED)}')
    for p, n in wcnt.most_common(8):
        if n >= SHARE_MIN:
            print(f'    {n:3d}  {p}')
    ndisp = sum(1 for s in S if s['disp']); nrpt = sum(1 for s in S if s['rpt'])
    nclaim = sum(1 for s in S if s['claims'])
    print(f'[scan] sessions={len(S)} dispatch={ndisp} claims={nclaim} report_writers={nrpt}')
    dups, deleg = pairs(S, TAU)
    funnel = dict(getattr(pairs, 'stats', {}))
    print('[funnel]', json.dumps(funnel, ensure_ascii=False))
    by_class = collections.Counter(k for r in dups for k in r['ev'])
    files = collections.Counter(f for r in dups for f in r['ev'].get('selfwrite', []))
    inv0 = collections.Counter(x for r in dups for x in (r['a'], r['b']))
    print('[class]', json.dumps(dict(by_class), ensure_ascii=False))

    def classname(c):
        return {'dispatch': 'A 同任务声明', 'claim': 'B 同 TODO 项声明', 'report': 'C 同报告文件',
                'selfwrite': 'D 同产物文件'}[c]

    inv = {x for r in dups for x in (r['a'], r['b'])}
    print(f'[dup] pairs={len(dups)} sessions={len(inv)} deleg_pairs={len(deleg)}')
    for r in dups[:30]:
        print(f"  {r['a'][:12]} + {r['b'][:12]} gap={int(r['gap_s'])} {r['ev']}")
    # 灵敏度 tau
    sens = {f'tau{t}': len(pairs(S, t)[0]) for t in (1800, 7200, 86400)}
    print('[sens]', json.dumps(sens))
    # 正控：R353/R354 已知重复劳动对必须命中
    PC = ('0922_1313-0923_0201.txt', '0922_1804-0922_2342.txt')
    pcfound = [r for r in dups if {r['a'], r['b']} == set(PC)]
    print(f"[PC] R353/R354 known-dup pair: {'PASS' if pcfound else 'FAIL'} {pcfound[:1]}")
    nc2 = nc3 = nc4 = None
    t = dups[0] if dups else None
    if t:
        sa = next(s for s in S if s['id'] == t['a']); sb = next(s for s in S if s['id'] == t['b'])
        # NC2 时间门负控：把 b 移到与 a 相距 > tau 之后，必须不报
        delta = (sa['end'] - sb['start']) + timedelta(seconds=TAU + 3600)
        sb2 = dict(sb); sb2['start'] = sb['start'] + delta; sb2['end'] = sb['end'] + delta
        d2, _ = pairs([sa, sb2], TAU)
        nc2 = {'pair': (t['a'], t['b']), 'shift_h': round(delta.total_seconds() / 3600, 2),
               'gap_h': round((max(sa2 := sb2['start'], sa['start']) - min(sa['end'], sb2['end'])).total_seconds() / 3600, 2),
               'dup_after': bool(d2), 'expect': False}
        # NC3 证据去除负控：清空 A 的全部证据，必须不报
        sa3 = dict(sa); sa3.update({'disp': None, 'rpt': set(), 'claims': set(), 'writes': set()})
        d3, _ = pairs([sa3, sb], TAU)
        nc3 = {'removed': 'all-evidence-of-A', 'dup_after': bool(d3), 'expect': False}
    # NC1/NC4 随机跨日
    rnd = random.Random(20260925)
    byd = {}
    for s in S:
        byd.setdefault(s['start'].strftime('%m-%d'), []).append(s)
    days = sorted(byd); hits = []
    for _ in range(20):
        d1, d2_ = rnd.sample(days, 2)
        a = rnd.choice(byd[d1]); b = rnd.choice(byd[d2_])
        dup, ev = judge(a, b)
        pipe, _ = pairs([a, b], TAU)
        hits.append({'a': a['id'], 'b': b['id'], 'ev_only': dup, 'ev': ev, 'pipeline': bool(pipe)})
    nc4 = {'n': len(hits), 'pipeline_flagged': sum(1 for h in hits if h['pipeline']),
          'evidence_only_flagged': sum(1 for h in hits if h['ev_only']), 'expect_pipeline': 0}
    print(f"[NC1] random_crossday evidence_only={nc4['evidence_only_flagged']}/20 (诊断项, 含时间门)")
    print('[NC2]', nc2, 'PASS' if nc2 and not nc2['dup_after'] else 'FAIL')
    print('[NC3]', nc3, 'PASS' if nc3 and not nc3['dup_after'] else 'FAIL')
    print('[NC4]', nc4, 'PASS' if nc4['pipeline_flagged'] == 0 else 'FAIL')
    # 分母（口径 A：有派单文本或有自建产物的会话）与并发/顺序拆解（仅按会话区间重叠，见 CRITERIA_V2 §7.1）
    wset = {s['id'] for s in S if s['disp'] or s['writes']}
    S_by = {s['id']: s for s in S}
    nconc = 0
    for r in dups:
        A, B = S_by[r['a']], S_by[r['b']]
        if (min(A['end'], B['end']) - max(A['start'], B['start'])).total_seconds() > 0:
            nconc += 1
    print(f'[rate] pairs={len(dups)} involved={len(inv)} work_union={len(wset)} '
          f'rate_work={len(inv)/len(wset):.3f} rate_all={len(inv)/len(S):.3f} concurrent={nconc}')
    payload = {'frozen': 'CRITERIA_V2.md', 'snapshot': datetime.now().strftime('%F %T'),
               'corpus': {'sessions': len(S), 'dispatch': ndisp, 'claims': nclaim, 'report_writers': nrpt,
                          'selfwrite_sessions': sum(1 for s in S if s['writes'])},
               'shared_files': {'min_sessions': SHARE_MIN, 'files': sorted(SHARED)},
               'funnel': funnel,
               'evidence_classes': {classname(c): n for c, n in by_class.most_common()},
               'top_overlap_files': files.most_common(15),
               'top_overlap_sessions': inv0.most_common(15),
               'positive_control': {'pair': list(PC), 'hit': bool(pcfound), 'ev': pcfound[0]['ev'] if pcfound else None},
               'metrics': {'pairs_dup': len(dups), 'sessions_involved': len(inv), 'deleg_pairs': len(deleg),
                           'sessions_work_union': len(wset), 'sessions_all': len(S),
                           'rate_work_union': round(len(inv) / max(1, len(wset)), 4),
                           'rate_all': round(len(inv) / max(1, len(S)), 4),
                           'pairs_concurrent': nconc, 'pairs_sequential': len(dups) - nconc},
               'sensitivity': sens, 'nc1': hits, 'nc2': nc2, 'nc3': nc3, 'nc4': nc4,
               'dups': dups, 'delegated': deleg}
    os.makedirs(OUT, exist_ok=True)
    json.dump(payload, open(os.path.join(OUT, 'report_v11.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('[out]', os.path.join(OUT, 'report_v11.json'))


def selftest():
    ok = fail = 0
    def chk(n, c, x=''):
        nonlocal ok, fail
        if c: ok += 1; print('  PASS', n)
        else: fail += 1; print('  FAIL', n, x)
    base = '从LoopX的真实案例里提炼无人值守怎么流转的可迁移模式输入只读仓库浅克隆' * 3
    A = {'id': 'A', 'start': datetime(2026, 9, 22, 9, 22), 'end': datetime(2026, 9, 22, 9, 23),
         'disp': norm(base), 'claims': set(), 'rpt': set(), 'writes': set(), 'deleg': False}
    B = {'id': 'B', 'start': datetime(2026, 9, 22, 9, 28), 'end': datetime(2026, 9, 22, 9, 29),
         'disp': norm(base), 'claims': set(), 'rpt': set(), 'writes': set(), 'deleg': False}
    chk('same dispatch => dup', pairs([A, B], TAU)[0])
    C = dict(A, id='C', disp=norm('完全不同的任务关于数据库备份与磁盘清理的检查清单与报告' * 3))
    chk('different dispatch => not dup', not judge(A, C)[0])
    D = dict(A, id='D', disp=None); E = dict(B, id='E', disp=None)
    chk('heartbeat/no-dispatch => not dup', not judge(D, E)[0])
    F = dict(A, id='F', disp=None, rpt={'memory/autonomous_reports/R353_x.md'})
    G = dict(B, id='G', disp=None, rpt={'memory/autonomous_reports/R353_x.md'})
    chk('same report write => dup', judge(F, G)[0])
    H = dict(A, id='H', disp=None, claims={'17|本机音视频资产去重挑人声长音频转写'})
    I = dict(B, id='I', disp=None, claims={'17|本机音视频资产去重挑人声长音频转写'})
    chk('same TODO claim => dup', judge(H, I)[0])
    H2 = dict(A, id='H2', disp=None, claims=set(),
              writes={'tools/audio_asr_batch.py', 'temp/audio_assets/MEMO.md'})
    I2 = dict(B, id='I2', disp=None, claims=set(), writes={'tools/audio_asr_batch.py'})
    chk('shared selfwritten file => dup', judge(H2, I2)[0])
    J2 = dict(H2, id='J2', writes={'tools/log_compress.py'})
    chk('disjoint selfwritten files => not dup', not judge(H2, J2)[0])
    old = SHARED
    globals()['SHARED'] = {'memory/global_mem_insight.txt'}
    P1 = dict(A, id='P1', disp=None, writes={'memory/global_mem_insight.txt'})
    Q1 = dict(B, id='Q1', disp=None, writes={'memory/global_mem_insight.txt'})
    chk('shared protocol file excluded', not judge(P1, Q1)[0])
    globals()['SHARED'] = old
    X1 = dict(A, id='0519_0004-0519_0007.txt'); Y1 = dict(B, id='0519_0004-0519_0008.txt')
    d1, _ = pairs([X1, Y1], TAU)
    chk('same-start shard skip', not d1 and getattr(pairs, 'stats', {}).get('same_start_skipped') == 1)
    J = dict(A, id='J', start=datetime(2026, 9, 22, 15, 0), end=datetime(2026, 9, 22, 15, 1))
    chk('gap>tau => not dup', not pairs([A, J], TAU)[0])
    K = dict(A, id='K', deleg=True); L = dict(B, id='L', deleg=True)
    d, g = pairs([K, L], TAU)
    chk('delegated exempt', not d and len(g) == 1)
    print(f'[selftest] {ok} passed, {fail} failed')
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--selftest', action='store_true'); ap.add_argument('--scan', action='store_true')
    ap.add_argument('--no-cache', action='store_true')
    a = ap.parse_args()
    if a.selftest: sys.exit(selftest())
    if a.scan: scan(a); return
    ap.print_help()


if __name__ == '__main__':
    main()
