#!/usr/bin/env python3
"""L1 紧凑工具：把 memory/global_mem_insight.txt 里超长且无锚点的行整行无损迁入
memory/l1_detail_archive.md，L1 只留「名称+触发词+→锚点」。

默认只预演（不写任何文件）；加 --apply 才写：先追加归档，再原子替换 L1，并先备份到 temp/l1_backup/。
用法：python3 tools/l1_compact.py [--apply] | --selftest
"""
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L1 = os.path.join(ROOT, 'memory', 'global_mem_insight.txt')
ARCH = os.path.join(ROOT, 'memory', 'l1_detail_archive.md')
BACKUP_DIR = os.path.join(ROOT, 'temp', 'l1_backup')
LINE_MAX = 500
ANCHOR = re.compile(r'→[AN]\d{3,4}')
OPEN, CLOSE = '(（[「', ')）]」'


def _split_depth0(s, seps):
    out, buf, depth, i = [], '', 0, 0
    while i < len(s):
        ch = s[i]
        if ch in OPEN: depth += 1
        elif ch in CLOSE: depth = max(0, depth - 1)
        hit = next((sp for sp in seps if depth == 0 and s.startswith(sp, i)), None)
        if hit:
            out.append(buf); buf = ''; i += len(hit); continue
        buf += ch; i += 1
    out.append(buf)
    return out


def _find_depth0(s, chars, start=0):
    depth = 0
    for i, ch in enumerate(s):
        if ch in OPEN: depth += 1
        elif ch in CLOSE: depth = max(0, depth - 1)
        elif depth == 0 and i >= start and ch in chars: return i
    return -1


def _short_item(item, trig=22):
    item = item.strip()
    m = re.match(r'^([^()（]+?)\s*[(（](.*)$', item, re.S)
    if not m: return item[:60]
    name, rest = m.group(1).strip(), re.sub(r'[)）]\s*$', '', m.group(2))
    clause = re.split(r'[;；,，=]', rest, maxsplit=1)[0].strip()
    clause = clause[:trig] + ('…' if len(clause) > trig else '')
    return f'{name}({clause})' if clause else name


def _kind(s):
    if not s.strip(): return 'blank'
    if s.startswith('#'): return 'heading'
    if re.match(r'\[[A-Z_]+\]', s): return 'section'
    if re.match(r'\d+\.\s', s): return 'rule'
    if s.startswith('|'): return 'items'
    if s.startswith('- '): return 'entry'
    eq, colon = _find_depth0(s, '='), _find_depth0(s, ':：')
    if re.search(r'R\d{3}', s[:160]) and eq != -1 and (colon == -1 or eq < colon): return 'entry'
    if re.search(r'R\d{3}', s[:160]) and not re.match(r'^[^|]{0,40}[:：]', s): return 'entry'
    return 'items'


def shorten(s, aid):
    k = _kind(s)
    if k == 'rule':
        num, rest = re.match(r'(\d+\.)\s*(.*)$', s, re.S).groups()
        head = rest[:140]
        cut = max(head.rfind('；'), head.rfind('。'), head.rfind('; '))
        return f'{num} {(head[:cut] if cut >= 60 else head).strip()} →{aid}'
    if k == 'entry':
        t = s[2:] if s.startswith('- ') else s
        cut = _find_depth0(t, '=：')
        title = t[:cut] if cut != -1 else t[:80]
        if len(title.strip()) < 12 and not re.search(r'R\d{3}', title) and cut != -1:
            cut2 = _find_depth0(t, '=：', cut + 1)
            if cut2 != -1: title = t[:cut2]
        title = title.strip()[:100]
        for rid in re.findall(r'R\d{3}', t[:200]):
            if rid not in title: title += f'({rid})'
        return f'- {title} →{aid}'
    body = s[1:].strip() if s.startswith('|') else s
    prefix = ''
    m = re.match(r'^([^|()（]{1,40}?[:：])\s*(.*)$', body, re.S)
    if m: prefix, body = m.group(1), m.group(2)
    parts = [p for p in _split_depth0(body, [' | ', ' ｜ ']) if p.strip()]
    return f'{prefix} ' + ' | '.join(_short_item(p) for p in parts) + f' →{aid}'


def _balanced(t):
    return sum(t.count(c) for c in OPEN) == sum(t.count(c) for c in CLOSE)


def _names(s):
    """原行顶层条目名（第一个括号或分隔符之前的 token），用于核对缩短后没有丢条目。"""
    body = s[2:] if s.startswith('- ') else (s[1:].strip() if s.startswith('|') else s)
    m = re.match(r'^([^|()（]{1,40}?[:：])\s*(.*)$', body, re.S)
    if m: body = m.group(2)
    out = []
    for p in _split_depth0(body, [' | ', ' ｜ ', '|', '｜']):
        n = re.split(r'[(（=]', p.strip(), maxsplit=1)[0].strip()
        if n: out.append(n[:40])
    return out


def _safe(old, new, min_save=0.3):
    """缩短结果必须：括号配对、保留每个顶层条目名、至少省 min_save。返回 (是否通过, 原因)。"""
    if len(new) > len(old) * (1 - min_save): return False, '收益不足'
    if not _balanced(old): return False, '原行括号不配对，条目边界不可靠'
    if not _balanced(new): return False, '括号不配对'
    miss = [n for n in _names(old) if n[:12] not in new]
    if miss and _kind(old) == 'items': return False, f'丢条目名 {miss[:2]}'
    return True, ''


def plan(lines, old_arch, line_max=LINE_MAX):
    """返回 (新 L1 行列表, 追加到归档的文本, 迁出行数, 跳过清单)；纯函数，不碰磁盘。
    规则行（[CONSTITUTION]/[RULES] 下的编号行）一律不压缩，只报告。"""
    nxt = max([int(x) for x in re.findall(r'^## N(\d{4})$', old_arch, re.M)] or [0]) + 1
    new, blocks, skipped, n = list(lines), [], [], 0
    for i, s in enumerate(lines):
        if len(s) <= line_max or ANCHOR.search(s) or _kind(s) in ('blank', 'heading', 'section'): continue
        if _kind(s) == 'rule': skipped.append((i + 1, '规则行不压缩')); continue
        aid = f'N{nxt:04d}'
        cand = shorten(s, aid)
        ok, why = _safe(s, cand)
        if not ok: skipped.append((i + 1, why)); continue
        nxt += 1; n += 1
        blocks.append(f'## {aid}\n{s}\n'); new[i] = cand
    return new, '\n'.join(blocks), n, skipped


def _read_strict(path):
    raw = open(path, 'rb').read()
    return raw, raw.decode('utf-8')          # 非法 UTF-8 直接抛错，不做 replace


class _Lock:
    def __init__(self, path): self.path, self.fd = path, None
    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try: self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError: raise SystemExit(f'已有另一个 l1_compact 在运行（{self.path}），退出')
        return self
    def __exit__(self, *a):
        if self.fd is not None: os.close(self.fd); os.path.exists(self.path) and os.unlink(self.path)


def run(l1=L1, arch=ARCH, apply=False, line_max=LINE_MAX, lock=None):
    raw0, text = _read_strict(l1)
    old = open(arch, encoding='utf-8').read() if os.path.exists(arch) else '# L1 细节归档\n'
    new, extra, n, skipped = plan(text.split('\n'), old, line_max)
    after = '\n'.join(new)
    print(f'L1 {len(text)} 字符 → {len(after)} 字符；{n} 行可迁出，{len(skipped)} 行跳过'
          + ('' if apply else '（预演，未写入；加 --apply 执行）'))
    for ln, why in skipped[:10]: print(f'  跳过 行{ln}: {why}')
    if not n or not apply: return n
    with _Lock(lock or os.path.join(os.path.dirname(l1), '.l1_compact.lock')):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        bak = os.path.join(BACKUP_DIR, f'global_mem_insight.{time.strftime("%Y%m%d-%H%M%S")}.{os.getpid()}.txt')
        with open(bak, 'wb') as f: f.write(raw0); f.flush(); os.fsync(f.fileno())
        arch_len = os.path.getsize(arch) if os.path.exists(arch) else 0
        tmp = l1 + '.compact.tmp'
        try:
            with open(arch, 'a', encoding='utf-8', newline='') as f:        # 先写归档
                f.write(('' if old.endswith('\n\n') else '\n') + extra); f.flush(); os.fsync(f.fileno())
            with open(tmp, 'wb') as f: f.write(after.encode('utf-8')); f.flush(); os.fsync(f.fileno())
            if open(l1, 'rb').read() != raw0: raise RuntimeError('L1 在整理期间被改动，放弃替换')
            os.replace(tmp, l1)
        except BaseException:
            if os.path.exists(tmp): os.unlink(tmp)
            with open(arch, 'ab') as f: f.truncate(arch_len)                  # 回滚归档，不留孤儿块
            raise
    print(f'已迁出 {n} 行，备份 {bak}')
    return n


def _selftest():
    import tempfile
    res = []
    ok = lambda c, m: (print(('PASS ' if c else 'FAIL ') + m), res.append(bool(c)))
    d = tempfile.mkdtemp(prefix='l1_compact_test_')
    global BACKUP_DIR
    BACKUP_DIR = os.path.join(d, 'bak')
    mk = lambda name, src: (open(os.path.join(d, name), 'w', encoding='utf-8').write(src), os.path.join(d, name))[1]
    entry = '新任务验证(R999,2026-10-12)=tools/x.py(' + '细节' * 400 + ')'
    items = '测试类: a_sop(触发词甲,很多细节' + 'x' * 300 + ') | b.py(乙) | c_sop(丙,' + 'y' * 300 + ')'
    short = '键鼠: ljqCtrl_sop | vision_sop'
    src = '\n'.join(['# [Global Memory Insight]', short, entry, items, '[RULES]', '1. 短规则'])
    l1, arch = mk('l1.txt', src), os.path.join(d, 'arch.md')
    ok(run(l1, arch, apply=False) == 2 and open(l1, encoding='utf-8').read() == src and not os.path.exists(arch), '预演不写任何文件')
    ok(run(l1, arch, apply=True) == 2, '--apply 迁出 2 行')
    new, a = open(l1, encoding='utf-8').read(), open(arch, encoding='utf-8').read()
    ok(entry in a and items in a, '原文整行无损存入归档')
    ok(short in new and '1. 短规则' in new, '短行与规则不变')
    ok('b.py(乙)' in new and 'a_sop' in new and 'c_sop' in new, '条目名全部保留')
    ok(len(os.listdir(BACKUP_DIR)) == 1, '已备份原 L1')
    ok(run(l1, arch, apply=True) == 0, '幂等：再次执行无迁出')
    # —— 评审边缘用例 ——
    def edge(name, line, expect_skip):
        p = mk(f'e_{name}.txt', '# L1\n' + line); a2 = os.path.join(d, f'a_{name}.md')
        n = run(p, a2, apply=True); out = open(p, encoding='utf-8').read()
        ok((n == 0) == expect_skip and (not expect_skip or out == '# L1\n' + line), f'边缘[{name}]：{"跳过且 L1 不变" if expect_skip else "迁出"}')
    edge('括号未闭合', '测试: a_sop(触发词甲,' + 'x' * 600 + ' | b.py(乙) | c_sop(丙) | d_sop(丁)', True)
    edge('无空格分隔', '测试: ' + '|'.join(f'sop{i}(' + 'x' * 80 + ')' for i in range(8)) + '|d_sop', True)
    edge('长规则', '4. 密钥文件仅引用，' + '不读取不移动' * 60 + '最终红线：禁止外发', True)
    edge('收益不足', '测试: ' + ' | '.join(f'k{i}' for i in range(130)), True)
    edge('带锚点', '测试: ' + 'x' * 700 + ' →A002', True)
    # 严格 UTF-8
    bad = os.path.join(d, 'bad.txt'); open(bad, 'wb').write(b'# L1\n' + b'\xff\xfe' + 'x'.encode() * 600)
    try: run(bad, os.path.join(d, 'a_bad.md'), apply=True); ok(False, '非法 UTF-8 应拒绝')
    except UnicodeDecodeError: ok(open(bad, 'rb').read().startswith(b'# L1\n\xff\xfe'), '非法 UTF-8：拒绝处理且 L1 不变')
    # 写前复核：备份落盘期间 L1 被并发写入 → 放弃替换、归档回滚、无 tmp 残留
    p = mk('race.txt', '# L1\n' + items); a3 = os.path.join(d, 'a_race.md'); open(a3, 'w', encoding='utf-8').write('# L1 细节归档\n')
    before = os.path.getsize(a3); real, hit = os.fsync, []
    def hijack(fd):
        if not hit: hit.append(1); open(p, 'a', encoding='utf-8').write('\n新条目(并发写入)')
        return real(fd)
    os.fsync = hijack
    try: run(p, a3, apply=True); raced = False
    except RuntimeError: raced = True
    finally: os.fsync = real
    ok(raced and '并发写入' in open(p, encoding='utf-8').read(), '并发改动：放弃替换且并发写入的内容仍在')
    ok(os.path.getsize(a3) == before and not os.path.exists(p + '.compact.tmp'), '放弃后归档已回滚、无 tmp 残留')
    print('ALL PASS' if all(res) else 'SOME FAIL')
    return 0 if all(res) else 1


if __name__ == '__main__':
    if '--selftest' in sys.argv: sys.exit(_selftest())
    run(apply='--apply' in sys.argv)
