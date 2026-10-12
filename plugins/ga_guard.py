# -*- coding: utf-8 -*-
"""ga_guard：L1 防膨胀插件（纯官方 hook 扩展点，不替换 GA 任何核心函数，不写任何文件）。

背景：GA 每次调用都把 memory/global_mem_insight.txt（L1，本应是极简索引）整份拼进系统提示，
L1 一旦膨胀，每次调用都多花 token。官方 L0 禁止往 L1 写细节与日志记录，但只靠 agent 自觉，无校验。

两个只读检查点：
1. agent_before：任务开始时检查 L1 体积，超限向 stderr 警告（每进程每小时至多一次）。
2. tool_after：每个工具执行后，若 L1 的 mtime 变了（任何方式写入：file_patch / file_write / code_run），
   检查违规；有违规就把一段纠正提示追加到该工具结果的 next_prompt，由 agent 自己用 file_patch 修正。
   插件本身不写任何文件，不替换任何函数；修改的只是返回给 agent 的提示文字。

自测（须在仓库根目录以模块方式运行）：python3 -m plugins.ga_guard --selftest
"""
import os
import re
import sys
import time

import plugins.hooks as hooks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L1 = os.path.join(ROOT, 'memory', 'global_mem_insight.txt')
MAX_CHARS = 24000        # L1 总字符预算（瘦身后约 1.6 万，留 50% 余量；官方 L0 期望 <1k token，当前未达）
LINE_MAX = 500           # 单行预算；超过且无归档锚点的行视为未瘦身
ANCHORED_MAX = 1500      # 带锚点的行也设上限，防止「锚点 + 大段细节」绕过检查
ANCHOR = re.compile(r'→[AN]\d{3,4}')
_EXEMPT = re.compile(r'^(#|\[[A-Z_]+\]|\d+\.\s)')   # 标题、分节、编号规则行不计入（规则不压缩），与 tools/l1_compact.py 口径一致
WARN_EVERY = 3600        # 同一进程内同类警告的最小间隔（秒）
RECORD_MIN = 200         # 带轮次编号 + 日期、且无锚点的长行视为「任务记录」，L0 明文禁止写入 L1
_RECORD = re.compile(r'R\d{3}.*20\d\d-\d\d-\d\d|20\d\d-\d\d-\d\d.*R\d{3}')

_last_warn = 0.0
_seen_mtime = {}


def check_l1(path=None, max_chars=MAX_CHARS, line_max=LINE_MAX):
    """只读检查。返回 (总字符数, 未瘦身超长行数, 警告文本或 None)。"""
    path = path or L1
    try:
        with open(path, encoding='utf-8', errors='replace') as f: text = f.read()
    except OSError:
        return 0, 0, None
    fat = sum(1 for s in text.split('\n')
              if not _EXEMPT.match(s) and (len(s) > ANCHORED_MAX or (len(s) > line_max and not ANCHOR.search(s))))
    if len(text) <= max_chars and not fat: return len(text), 0, None
    return len(text), fat, (f'[ga_guard] L1 超预算：{len(text)} 字符（上限 {max_chars}），{fat} 行超长未瘦身；'
                           f'每次调用都会多发这些内容。预演：python3 tools/l1_compact.py；官方整理流程见 memory/memory_cleanup_sop.md')


def violations(path=None, max_chars=MAX_CHARS, line_max=LINE_MAX):
    """返回 (违规描述列表, 总字符数)。只读。"""
    path = path or L1
    try:
        with open(path, encoding='utf-8', errors='replace') as f: text = f.read()
    except OSError:
        return [], 0
    out = []
    for n, s in enumerate(text.split('\n'), 1):
        if _EXEMPT.match(s): continue
        head = s[:36].replace('\n', ' ')
        if len(s) > ANCHORED_MAX: out.append(f'第 {n} 行 {len(s)} 字（上限 {ANCHORED_MAX}）「{head}…」')
        elif not ANCHOR.search(s) and len(s) > RECORD_MIN and _RECORD.search(s):
            out.append(f'第 {n} 行是任务记录（含轮次编号+日期，{len(s)} 字）「{head}…」')
        elif not ANCHOR.search(s) and len(s) > line_max: out.append(f'第 {n} 行 {len(s)} 字未瘦身（上限 {line_max}）「{head}…」')
    if len(text) > max_chars: out.append(f'L1 总量 {len(text)} 字符，超过预算 {max_chars}')
    return out, len(text)


_ADVICE = ('[ga_guard] 检测到 L1（memory/global_mem_insight.txt）违反 L0 记忆规范（L1 只写关键词和路径，严禁写细节与日志记录）：\n'
           '{items}\n'
           '请立即修正：把过程、实测、数值细节写入 memory/autonomous_reports/ 或对应 L3 SOP，'
           'L1 对应行只留「名称(反直觉触发词)」和文件路径，单行不超过 120 字；'
           '用 file_patch 做词级修改，禁止 file_write 覆盖或用 code_run 重写整个 L1。')


def _l1_feedback(path=None):
    """L1 的 mtime 变化且存在违规时返回纠正提示，否则 None。"""
    path = path or L1
    try: m = os.stat(path).st_mtime_ns
    except OSError: return None
    if _seen_mtime.get(path) == m: return None
    _seen_mtime[path] = m
    items, _ = violations(path)
    if not items: return None
    shown = '\n'.join('  - ' + x for x in items[:5]) + (f'\n  - …另有 {len(items) - 5} 处' if len(items) > 5 else '')
    return _ADVICE.format(items=shown)


@hooks.register('tool_after')
def _on_tool_after(ctx):
    try:
        ret = ctx.get('ret')
        if ret is None or not isinstance(getattr(ret, 'next_prompt', None), str) or not ret.next_prompt: return None
        msg = _l1_feedback()
        if msg: ret.next_prompt = ret.next_prompt + '\n\n' + msg + '\n'
    except Exception as e:
        sys.stderr.write(f'[ga_guard] tool_after 检查失败，已忽略: {e}\n')
    return None        # 不返回 dict：不改动 ctx


@hooks.register('agent_before')
def _on_agent_before(ctx):
    global _last_warn
    now = time.time()
    if now - _last_warn < WARN_EVERY: return None
    _, _, warn = check_l1()
    if warn:
        _last_warn = now
        sys.stderr.write(warn + '\n')
    return None        # 不返回 dict：不改动 ctx


def _selftest():
    import tempfile
    d = tempfile.mkdtemp(prefix='ga_guard_test_'); p = os.path.join(d, 'l1.txt')
    res = []
    ok = lambda c, m: (print(('PASS ' if c else 'FAIL ') + m), res.append(bool(c)))
    open(p, 'w', encoding='utf-8').write('# L1\n键鼠: a | b\n')
    ok(check_l1(p)[2] is None, '小 L1 无警告')
    open(p, 'w', encoding='utf-8').write('# L1\n' + 'x' * 600 + '\n')
    n, fat, w = check_l1(p); ok(fat == 1 and w, '超长未瘦身行触发警告')
    open(p, 'w', encoding='utf-8').write('# L1\n' + 'x' * 600 + ' →A001\n')
    ok(check_l1(p)[2] is None, '带锚点的中等长行不告警')
    open(p, 'w', encoding='utf-8').write('y' * 40000)
    ok(check_l1(p, line_max=10**9)[2] is not None, '总量超限触发警告')
    ok(check_l1(os.path.join(d, 'none'))[2] is None, '文件缺失静默')
    open(p, 'w', encoding='utf-8').write('# ' + 'h' * 700 + '\n[RULES] ' + 'r' * 700 + '\n')
    ok(check_l1(p)[2] is None, '标题/分节行不触发（与 l1_compact 口径一致）')
    open(p, 'w', encoding='utf-8').write('# L1\n' + 'x' * 5000 + ' →A002\n')
    ok(check_l1(p)[2] is not None, '带锚点的超大行也告警（防绕过）')
    open(p, 'w', encoding='utf-8').write('# L1\n[RULES]\n4. ' + '规' * 900 + '\n')
    ok(check_l1(p)[2] is None, '编号规则行不触发（规则不压缩）')
    before = open(p, 'rb').read(); check_l1(p); ok(open(p, 'rb').read() == before, '只读：文件内容未变')
    ok(_on_agent_before({}) is None, 'hook 返回 None，不改 ctx')
    # —— tool_after 反馈：走 GA 官方 BaseHandler.dispatch，验证真实调用链 ——
    global L1
    from agent_loop import BaseHandler, StepOutcome
    l1 = os.path.join(d, 'g.txt'); old_l1, L1 = L1, l1
    _seen_mtime.clear()
    class H(BaseHandler):
        def __init__(self): self.writes = None
        def do_x(self, args, response):
            if self.writes is not None: open(l1, 'w', encoding='utf-8').write(self.writes)
            yield 'run'; return StepOutcome({'ok': 1}, next_prompt='原提示')
        def do_empty(self, args, response): yield 'r'; return StepOutcome(None, next_prompt='')
    def call(h, name='x'):
        g = h.dispatch(name, {}, None)
        try:
            while True: next(g)
        except StopIteration as e: return e.value
    try:
        h = H(); open(l1, 'w', encoding='utf-8').write('# L1\n键鼠: a | b\n'); call(h)
        o = call(h); ok(o.next_prompt == '原提示', '无违规：提示原样')
        h.writes = '# L1\n键鼠: a | b\n' + '新任务记录(R999,2026-10-12)=tools/x.py(' + '细' * 300 + ')\n'
        o = call(h); ok('任务记录' in o.next_prompt and o.next_prompt.startswith('原提示') and 'file_patch' in o.next_prompt, '写入任务记录：提示被追加纠正')
        h.writes = None
        o = call(h); ok(o.next_prompt == '原提示', 'mtime 未变：不重复提示')
        h.writes = '# L1\n键鼠: a | b\n' + 'x' * 600 + '\n'; time.sleep(0.01)
        o = call(h); ok('未瘦身' in o.next_prompt, '写入超长未瘦身行：同样提示')
        h.writes = '# L1\n键鼠: a | b\n' + 'x' * 600 + ' →A001\n'; time.sleep(0.01)
        o = call(h); ok(o.next_prompt == '原提示', '改成带锚点的短索引后：不再提示')
        h.writes = '# L1\n' + 'x' * 600 + '\n'; time.sleep(0.01)
        ok(call(h, 'empty').next_prompt == '', '空 next_prompt 不被改写（不破坏任务结束判定）')
    finally: L1 = old_l1
    print('ALL PASS' if all(res) else 'SOME FAIL')
    return 0 if all(res) else 1


if __name__ == '__main__' and '--selftest' in sys.argv:
    sys.exit(_selftest())
