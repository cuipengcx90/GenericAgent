#!/usr/bin/env python3
"""R316 Agent行为日志 Polars 深度分析 — 可复现脚本
用法: python R316_polars_analysis.py
数据源: ../logs/autonomous_reflect.log
"""
import re, os, sys
import polars as pl
from collections import Counter

LOG_PATH = os.path.join(os.path.dirname(__file__), '..', 'logs', 'autonomous_reflect.log')

def parse_sessions(log_path: str) -> list[dict]:
    """将原始日志解析为session列表"""
    with open(log_path, 'r', errors='replace') as f:
        lines = f.readlines()
    
    sessions = []
    cur = None
    for raw in lines:
        line = raw.strip()
        if line.startswith('[Reflect] triggered:'):
            if cur:
                sessions.append(cur)
            trigger_text = line[len('[Reflect] triggered:'):].strip()
            trigger_type = 'auto' if '[AUTO]' in trigger_text else 'user' if trigger_text else 'unknown'
            cur = {
                'trigger_type': trigger_type,
                'trigger_text': trigger_text[:100],
                'turns': 0, 'tokens_out': 0, 'tool_calls': 0,
                'errors': [], 'summaries': [], 'cuts': 0,
                'cache_reads': 0, 'cache_creations': 0,
                'tools_detail': Counter()
            }
        elif cur is None:
            continue
        elif line.startswith('LLM Running'):
            cur['turns'] += 1
        elif line.startswith('[Output] tokens='):
            m = re.search(r'tokens=(\d+)', line)
            if m:
                cur['tokens_out'] += int(m.group(1))
        elif line.startswith('[Cache]'):
            m_r = re.search(r'read=(\d+)', line)
            m_c = re.search(r'creation=(\d+)', line)
            if m_r: cur['cache_reads'] += int(m_r.group(1))
            if m_c: cur['cache_creations'] += int(m_c.group(1))
        elif line.startswith('[Cut]'):
            cur['cuts'] += 1
        elif line.startswith('[ERROR]') or line.startswith('[FATAL]'):
            cur['errors'].append(line[:200])
        elif line.startswith('<summary>'):
            cur['summaries'].append(line)
        elif '🛠️' in line:
            cur['tool_calls'] += 1
            m = re.match(r'🛠️\s*(\w+)', line)
            if m:
                cur['tools_detail'][m.group(1)] += 1
    if cur:
        sessions.append(cur)
    return sessions

def build_dataframe(sessions: list[dict]) -> pl.DataFrame:
    """构建polars DataFrame"""
    records = []
    for i, s in enumerate(sessions):
        records.append({
            'session_id': i,
            'trigger_type': s['trigger_type'],
            'trigger_text': s['trigger_text'],
            'turns': s['turns'],
            'tokens_out': s['tokens_out'],
            'tool_calls': s['tool_calls'],
            'error_count': len(s['errors']),
            'cuts': s['cuts'],
            'cache_reads': s['cache_reads'],
            'cache_creations': s['cache_creations'],
            'code_run_count': s['tools_detail'].get('code_run', 0),
            'file_read_count': s['tools_detail'].get('file_read', 0),
            'web_scan_count': s['tools_detail'].get('web_scan', 0),
            'tokens_per_turn': s['tokens_out'] / s['turns'] if s['turns'] > 0 else 0,
            'tools_per_turn': s['tool_calls'] / s['turns'] if s['turns'] > 0 else 0,
        })
    return pl.DataFrame(records)

def analyze(df: pl.DataFrame):
    """输出所有分析结果"""
    print(f"DataFrame: {df.shape}")
    print(df.describe())
    
    total = df.height

    # Finding 1: 空转率
    print("\n═══ Finding 1: Session空转分析 ═══")
    zero = df.filter(pl.col('turns') == 0).height
    one = df.filter(pl.col('turns') == 1).height
    le2 = df.filter(pl.col('turns') <= 2).height
    print(f"  0-turn sessions: {zero} ({zero/total*100:.1f}%)")
    print(f"  1-turn sessions: {one} ({one/total*100:.1f}%)")
    print(f"  ≤2-turn total: {le2} ({le2/total*100:.1f}%)")
    
    # Finding 2: 高耗时session Token消耗
    print("\n═══ Finding 2: 高耗时session Token消耗 ═══")
    heavy = df.filter(pl.col('turns') > 15)
    total_tokens = int(df['tokens_out'].sum())
    heavy_tokens = int(heavy['tokens_out'].sum())
    print(f"  Heavy sessions (>15T): {heavy.height} ({heavy.height/total*100:.1f}%)")
    if total_tokens > 0:
        print(f"  Token share: {heavy_tokens} / {total_tokens} = {heavy_tokens/total_tokens*100:.1f}%")
    else:
        print("  Token share: N/A (total_tokens=0)")
    print("  Top 5 by turns:")
    top5 = df.sort('turns', descending=True).head(5)
    print(top5.select(['session_id', 'turns', 'tokens_out', 'trigger_text']))
    
    # Finding 3: 重试分析
    print("\n═══ Finding 3: 重试/失败session分析 ═══")
    retry = df.filter(pl.col('error_count') > 0)
    normal = df.filter(pl.col('error_count') == 0)
    print(f"  With errors: {retry.height} ({retry.height/total*100:.1f}%)")
    if retry.height > 0:
        print(f"  Avg turns (error): {retry['turns'].mean():.1f} vs normal: {normal['turns'].mean():.1f}")
        print(f"  Avg tokens (error): {retry['tokens_out'].mean():.0f} vs normal: {normal['tokens_out'].mean():.0f}")
    
    # Finding 4: 工具分布
    print("\n═══ Finding 4: 工具使用分布 ═══")
    code_total = int(df['code_run_count'].sum())
    file_total = int(df['file_read_count'].sum())
    tool_total = int(df['tool_calls'].sum())
    print(f"  Total tool calls: {tool_total}")
    if tool_total:
        print(f"  code_run: {code_total} ({code_total/tool_total*100:.1f}%)")
        print(f"  file_read: {file_total} ({file_total/tool_total*100:.1f}%)")
    
    # Finding 5: Context cut压力
    print("\n═══ Finding 5: Context Cut分析 ═══")
    with_cut = df.filter(pl.col('cuts') > 0)
    print(f"  Sessions with cuts: {with_cut.height} ({with_cut.height/total*100:.1f}%)")
    if with_cut.height > 0:
        no_cut = df.filter(pl.col('cuts') == 0)
        print(f"  Avg turns (cut): {with_cut['turns'].mean():.1f} vs no-cut: {no_cut['turns'].mean():.1f}")
    
    # Finding 6: Token效率分位
    print("\n═══ Finding 6: Token效率分布 ═══")
    active = df.filter(pl.col('turns') > 0)
    if active.height > 0:
        tpt = active['tokens_per_turn']
        for q in [0.1, 0.25, 0.5, 0.75, 0.9]:
            print(f"  P{int(q*100)}: {tpt.quantile(q):.0f} tokens/turn")

if __name__ == '__main__':
    if not os.path.exists(LOG_PATH):
        print(f"Log not found: {LOG_PATH}")
        sys.exit(1)
    sessions = parse_sessions(LOG_PATH)
    df = build_dataframe(sessions)
    analyze(df)
