# -*- coding: utf-8 -*-
"""GA 上下文占用只读审计。仅用标准库，不写任何文件。

用法: python3 -I ctx_audit.py [--ga-root <GA根>] <子命令>   （默认由脚本位置推出，或读 GA_ROOT）
子命令:
  all        依次运行下面全部检查
  patches    静态检查两处上下文补丁是否已在本地源码生效
  l1         L1 体积/行数/违规（调用仓库自带 tools/ga_diag.py l1）
  triggers   反射进程每次触发的首轮上下文（判断任务间历史是否被携带）
  trim       各进程日志的 Current/Trimmed 比例与平均输入、缓存命中率
"""
import argparse
import os
import re
import statistics as st
import subprocess
import sys

TAIL_BYTES = 24 * 1024 * 1024
CTX = re.compile(r'Current context: (\d+) chars, (\d+) messages')
TRIM = re.compile(r'Trimmed context, current: (\d+) chars')
CACHE = re.compile(r'\[Cache\] input=(\d+) cached=(\d+)')
TRIGGER = re.compile(r'\[Reflect\] triggered: (.{0,60})')
# 脚本位于 <GA 根>/skills/ga-context-audit/scripts/，由真实路径推出 GA 根；可用 --ga-root 或 GA_ROOT 覆盖
DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
FRESH_GUESS = 5000   # 干净起点的估计值（字符），属假设，报告中会标明


def tail_text(path, nbytes=TAIL_BYTES):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode('utf-8', errors='replace')


def read(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


def cmd_patches(root):
    print('## 补丁生效检查（静态）')
    checks = [
        ('ga.py', r"<global_memory>\{get_global_memory\(\)\}</global_memory>", '每 10 轮注入的 L1 包进 <global_memory>（可折叠）'),
        ('llmcore.py', r"\(history\|key_info\|earlier_context\|global_memory\)", '折叠正则含 global_memory'),
        ('agentmain.py', r"FRESH_CONTEXT'?, False\).*reset_conversation\(agent, snapshot=False\)", '反射循环按 FRESH_CONTEXT 清历史'),
        ('reflect/scheduler.py', r"^FRESH_CONTEXT\s*=\s*True", 'scheduler 启用 FRESH_CONTEXT'),
        ('frontends/continue_cmd.py', r"def reset_conversation\(agent, message=.*snapshot=True\)", 'reset_conversation 支持 snapshot 开关'),
    ]
    for rel, pat, desc in checks:
        p = os.path.join(root, rel)
        ok = os.path.isfile(p) and re.search(pat, read(p), re.M) is not None
        print(f"  [{'OK' if ok else '缺'}] {desc}  ({rel})")
    print('  注：补丁已在运行进程生效的前提是进程在补丁之后重启过，见 triggers 的"首轮上下文"。')


def cmd_l1(root):
    print('## L1')
    diag = os.path.join(root, 'tools', 'ga_diag.py')
    if os.path.isfile(diag):
        r = subprocess.run([sys.executable, '-I', diag, 'l1'], capture_output=True, text=True, cwd=root)
        print(r.stdout.rstrip() or r.stderr.rstrip())
        return
    p = os.path.join(root, 'memory', 'global_mem_insight.txt')
    t = read(p)
    print(f"  {len(t)} 字符 / {t.count(chr(10)) + 1} 行（无 ga_diag.py，仅给体积）")


def segments(text):
    """按 [Reflect] triggered 切段，返回 [(任务名, [各调用的上下文字符数])]。"""
    segs, cur = [], None
    for line in text.split('\n'):
        m = TRIGGER.search(line)
        if m:
            if cur and cur[1]: segs.append(cur)
            cur = (m.group(1), [])
            continue
        m = CTX.search(line) or TRIM.search(line)
        if m and cur is not None: cur[1].append(int(m.group(1)))
    if cur and cur[1]: segs.append(cur)
    return segs


def cmd_triggers(root, log='logs/scheduler_reflect.log', since_restart=True):
    print('## 反射触发的首轮上下文')
    path = os.path.join(root, log)
    if not os.path.isfile(path):
        print('  无日志', path); return
    text = tail_text(path)
    restart_segs = None
    if since_restart:
        idx = text.rfind('[Reflect] loaded')
        if idx >= 0: restart_segs = segments(text[idx:])
    for label, segs in (('近期全部', segments(text)), ('最近一次重启之后', restart_segs)):
        if segs is None: continue
        if not segs:
            print(f'  {label}: 无触发'); continue
        first = [s[1][0] for s in segs]
        calls = sum(len(s[1]) for s in segs)
        total = sum(sum(s[1]) for s in segs)
        carried = sum(max(0, s[1][0] - FRESH_GUESS) * len(s[1]) for s in segs)
        print(f"  {label}: 触发 {len(segs)} 次 / 调用 {calls} 次；首轮上下文 中位 {int(st.median(first))} 字符，最大 {max(first)}")
        print(f"    首轮上下文 >4 万字符的触发占 {sum(f > 40000 for f in first) * 100 // len(first)}%；"
              f"携带旧历史估占总输入 {100 * carried // max(total, 1)}%（干净起点按 {FRESH_GUESS} 字符假设）")
    print('  判读：首轮中位应在约 1 万字符以内；接近 18 万说明任务间历史仍被携带（FRESH_CONTEXT 未生效或进程未重启）。')


def cmd_trim(root):
    print('## 硬裁剪比例与平均输入（各日志近 24MB）')
    logs = ['feishu_agent.log', 'scheduler_reflect.log', 'dingtalk_agent.log', 'autonomous_reflect.log']
    for name in logs:
        p = os.path.join(root, 'logs', name)
        if not os.path.isfile(p): continue
        text = tail_text(p)
        cur = len(CTX.findall(text)); trim = len(TRIM.findall(text))
        cache = [(int(a), int(b)) for a, b in CACHE.findall(text)][-500:]
        if cur + trim == 0 and not cache: continue
        s = f"  {name}: Current {cur} / Trimmed {trim}"
        if cur + trim: s += f"（硬裁剪 {100 * trim // (cur + trim)}%）"
        if cache:
            i = sum(a for a, _ in cache); c = sum(b for _, b in cache)
            s += f"；近 {len(cache)} 次平均输入 {i // len(cache) // 1000}K token，缓存命中 {100 * c // max(i, 1)}%"
        print(s)
    print('  判读：硬裁剪占比 >10% 说明历史频繁顶到上限；输入 token 里缓存命中低于 80% 要查中转站是否支持缓存。')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ga-root', default=os.environ.get('GA_ROOT') or DEFAULT_ROOT)
    ap.add_argument('cmd', choices=['all', 'patches', 'l1', 'triggers', 'trim'])
    a = ap.parse_args()
    root = a.ga_root
    if not os.path.isdir(root):
        sys.exit(f'GA 根目录不存在: {root}')
    table = {'patches': cmd_patches, 'l1': cmd_l1, 'triggers': cmd_triggers, 'trim': cmd_trim}
    for name in (table if a.cmd == 'all' else [a.cmd]):
        table[name](root); print()


if __name__ == '__main__':
    main()
