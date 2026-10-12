#!/usr/bin/env python3
"""GA 慢 / 费 / 断 诊断工具（只读：只读取 temp/model_responses、memory、git，不写任何文件）。

用法（须在任意目录执行，路径自动按本文件定位）：
  python3 -I tools/ga_diag.py latency [--since 2026-10-10]   各日×各模型单轮延迟与 >120s 次数
  python3 -I tools/ga_diag.py breaks  [--since 2026-10-10]   各会话每个用户消息段的结束方式；错误终止次数与恢复间隔
  python3 -I tools/ga_diag.py l1                              L1 体积、规则违规、估算 token
  python3 -I tools/ga_diag.py cost [--since D] [--until D] [--hit 0.95 --p-hit 0.2 --p-miss 2]
                                                             按每日调用数 × L1 多发 token 估算费用（单价为假设，须自行校准）
口径说明见 docs/LOCAL_COST_AND_STABILITY_GUIDE.md。
"""
import argparse
import collections
import glob
import os
import re
import statistics as st
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MR = os.path.join(ROOT, 'temp', 'model_responses')
L1 = os.path.join(ROOT, 'memory', 'global_mem_insight.txt')
HDR = re.compile(r'^=== (Prompt|Response) === (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?: model=(\S+))?', re.M)
ERR = re.compile(r"""^\s*\[\{'type': 'text', 'text': ['"]!!!""")
T = lambda s: datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
TOK_PER_BYTE = 66000 / 252715     # 校准：25.27 万字节的 L1 ≈ 6.6 万 token（本机粗估，换模型会变）
L1_KEEP_TOKENS = 7000             # 瘦身后 L1 的估算 token


def sessions(since='0000'):
    for p in sorted(glob.glob(os.path.join(MR, 'model_responses_*.txt')), key=os.path.getmtime):
        t = open(p, encoding='utf-8', errors='replace').read()
        ms = list(HDR.finditer(t))
        if ms and ms[-1].group(2) >= since:
            yield os.path.basename(p)[16:-4], t, ms


def is_user_text(body):
    return '"type": "text"' in body[:100] and 'tool_result' not in body[:100]


def cmd_latency(a):
    agg = collections.defaultdict(list)
    for _, t, ms in sessions(a.since):
        for x, y in zip(ms, ms[1:]):
            if x.group(1) == 'Prompt' and y.group(1) == 'Response' and y.group(2) >= a.since:
                agg[(y.group(2)[5:10], y.group(3))].append((T(y.group(2)) - T(x.group(2))).total_seconds())
    print(f"{'日期':6}{'模型':26}{'轮次':>6}{'中位s':>7}{'p90s':>7}{'最大s':>8}{'合计min':>9}{'>120s':>7}")
    for (d, m), v in sorted(agg.items()):
        s = sorted(v); p90 = s[max(0, int(len(s) * .9) - 1)]
        print(f"{d:6}{m:26}{len(v):>6}{st.median(v):>7.0f}{p90:>7.0f}{max(v):>8.0f}{sum(v) / 60:>9.0f}{sum(1 for x in v if x > 120):>7}")


def cmd_breaks(a):
    seg_total = seg_err = 0; kinds = collections.Counter(); wait = 0.0; rec = []
    for sid, t, ms in sessions(a.since):
        segs, cur, last = [], None, None
        for i, m in enumerate(ms):
            body = t[m.end():(ms[i + 1].start() if i + 1 < len(ms) else len(t))]
            if m.group(1) == 'Prompt' and is_user_text(body):
                if cur: segs.append(cur)
                cur = [m.group(2), 0, None]
            elif m.group(1) == 'Response':
                if cur: cur[1] += 1; cur[2] = bool(ERR.match(body))
        if cur: segs.append(cur)
        e = sum(1 for s in segs if s[2]); seg_total += len(segs); seg_err += e
        print(f"{sid}: 用户消息段 {len(segs)}，以 !!!Error 结尾 {e}，轮次合计 {sum(s[1] for s in segs)}")
        resp = [(m.group(2), t[m.end():m.end() + 200]) for m in ms if m.group(1) == 'Response']
        pr = {m.group(2): m for m in ms if m.group(1) == 'Prompt'}
        for i, (ts, b) in enumerate(resp):
            if ts < a.since or not ERR.match(b): continue
            k = re.match(r"""\s*\[\{'type': 'text', 'text': ['"]!!!Error: ([A-Za-z0-9 ]+)""", b)
            kinds[(k.group(1).strip()[:24] if k else '?')] += 1
            nxt = next((c for c in range(i + 1, len(resp)) if not ERR.match(resp[c][1])), None)
            if nxt is not None: rec.append((T(resp[nxt][0]) - T(ts)).total_seconds())
    print(f"\n合计：{seg_total} 段，{seg_err} 段以错误结尾；{since_note(a)}整轮错误响应 {sum(kinds.values())} 次")
    for k, v in kinds.most_common(): print(f"  {v:3}  {k}")
    if rec:
        rec.sort(); n = len(rec)
        print(f"错误后到下一次成功的间隔（含人工发现并输入“继续”的时间，是上界）：样本 {n}，中位 {st.median(rec) / 60:.1f} 分钟，"
              f"p25 {rec[n // 4] / 60:.1f}，p75 {rec[n * 3 // 4] / 60:.1f}，最短 {rec[0]:.0f} 秒")


def since_note(a): return f"{a.since} 起" if a.since != '0000' else ''


def cmd_l1(a):
    sys.path.insert(0, ROOT)
    import plugins.ga_guard as g
    text = open(L1, encoding='utf-8').read()
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    items, _ = g.violations(L1)
    print(f"L1：{len(text)} 字符 / {len(text.encode())} 字节 / {text.count(chr(10)) + 1} 行；估算 {(cjk * .7 + (len(text) - cjk) * .3) / 1000:.1f}K token")
    print(f"官方 L0 指标：≤ 30 行、< 1k token；插件预算：≤ {g.MAX_CHARS} 字符；违规 {len(items)} 处")
    for x in items[:10]: print('  -', x)
    anchors = set(re.findall(r'→([AN]\d{3,4})', text))
    arch = os.path.join(ROOT, 'memory', 'l1_detail_archive.md')
    blocks = set(re.findall(r'^## ([AN]\d{3,4})$', open(arch, encoding='utf-8').read(), re.M)) if os.path.exists(arch) else set()
    print(f"锚点 {len(anchors)} 个，归档中缺失 {len(anchors - blocks)} 个")


def l1_bytes_on(day):
    c = subprocess.run(['git', 'log', '--before', f'{day} 23:59:59', '-1', '--format=%h', '--', 'memory/global_mem_insight.txt'],
                       capture_output=True, text=True, cwd=ROOT).stdout.strip()
    return len(subprocess.run(['git', 'show', f'{c}:memory/global_mem_insight.txt'], capture_output=True, cwd=ROOT).stdout) if c else 0


def cmd_cost(a):
    calls = collections.Counter()
    for _, t, ms in sessions(a.since):
        for m in ms:
            if m.group(1) == 'Response' and a.since <= m.group(2)[:10] <= a.until: calls[m.group(2)[:10]] += 1
    tot = 0.0; n = 0
    print(f"{'日期':11}{'调用数':>7}{'L1 字节':>9}{'多发/次(K tok)':>15}{'多发合计(M)':>12}")
    for d in sorted(calls):
        ex = max(0, l1_bytes_on(d) * TOK_PER_BYTE - L1_KEEP_TOKENS); tot += calls[d] * ex; n += calls[d]
        print(f"{d:11}{calls[d]:>7}{l1_bytes_on(d):>9}{ex / 1000:>15.0f}{calls[d] * ex / 1e6:>12.1f}")
    M = tot / 1e6; unit = a.hit * a.p_hit + (1 - a.hit) * a.p_miss
    print(f"合计 {n} 次调用，L1 多发约 {M:.0f}M 输入 token；单价假设 命中 ¥{a.p_hit}/M、未命中 ¥{a.p_miss}/M、缓存命中率 {a.hit:.0%} ⇒ 约 ¥{M * unit:.0f}")
    print("注意：单价与缓存命中率都是假设，实际以各平台账单为准；L1 的 token 换算为本机粗估。")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest='cmd', required=True)
    for name, fn in (('latency', cmd_latency), ('breaks', cmd_breaks), ('l1', cmd_l1), ('cost', cmd_cost)):
        s = sp.add_parser(name); s.set_defaults(fn=fn)
        if name != 'l1': s.add_argument('--since', default='0000')
        if name == 'cost':
            s.add_argument('--until', default='9999'); s.add_argument('--hit', type=float, default=0.95)
            s.add_argument('--p-hit', type=float, default=0.2); s.add_argument('--p-miss', type=float, default=2.0)
    a = p.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
