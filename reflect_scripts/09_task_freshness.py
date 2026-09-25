#!/usr/bin/env python3
"""09 任务产物级新鲜度巡检（反射器版）

契约与官方 01-08 一致：INTERVAL + check() + on_done()。
- 轮询本身 0 LLM 成本：check() 只跑本地工具 tools/task_freshness.py（整表扫描实测约 3-5s）。
- 通知节流靠 **ledger 差集**：工具每次运行把本次「硬问题 id 集合」追加进
  temp/task_freshness/ledger.jsonl；本脚本读运行时前后各一行，算出
  newi（新增硬问题）/clri（已恢复），只在**集合发生变化**时推一次飞书私信
  ⇒ 同一个问题挂在那里不会被反复推送（与 07/08 的「全绿零输出」同范式）。
- check() 永远返回 None ⇒ 不唤起 Agent：本巡检只做「发现+报告」，
  修不修（birdie 换站、haisou 额度、ga-api 是否加载）都是人的决策。
- 硬问题定义见 tools/freshness_criteria.json：payload 过期/缺失、降级签名命中、
  launchd 未加载等；标 `soft:true` 的条目（如 x_ai_intel 的「无 x.com 标签页按设计跳过」、
  ga-api 僵尸任务）只打印不计硬问题。

⚠️ 本文件的存在不等于「已接入」：reflect_scripts/ 下没有目录自动扫描
   （已实测：每个脚本靠各自的 launchd `agentmain.py --reflect <script>` 驱动）。
   接入方式（见报告「待批提案」）：
     A) 复用已在跑的 com.cuip.genericagent-l2-gate（30min 循环里加一行调用，零新增定时）；
     B) 新建 com.cuip.genericagent-task-freshness.plist（属「新增定时」，需批准）。
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu  # noqa: E402

INTERVAL = 3600                      # 每小时轮询一次（本地扫描零 LLM 成本；通知靠 ledger 差集去重）
GA = Path('/Users/cuip/projects/GenericAgent')
TOOL = GA / 'tools' / 'task_freshness.py'
LEDGER = GA / 'temp' / 'task_freshness' / 'ledger.jsonl'


def _last_ledger(ledger):
    """读 ledger 最后一行（dict）——工具「本次运行前」的基线。"""
    if not os.path.exists(ledger):
        return None
    last = None
    with open(ledger, encoding='utf-8') as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                last = ln
    try:
        return json.loads(last) if last else None
    except Exception:
        return None


def evaluate(ledger=LEDGER, root=GA, timeout=600):
    """跑一次工具，返回 (result_dict, new_hard_ids, cleared_hard_ids)。"""
    before = _last_ledger(ledger)
    p = subprocess.run([sys.executable, str(TOOL), '--root', str(root),
                        '--ledger', str(ledger), '--json'],
                       capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError('task_freshness 无输出（rc=%s）: %s' % (p.returncode, (p.stderr or '')[-400:]))
    res = json.loads(out)
    now = set(res.get('hard_issue_ids') or [])
    prev = set((before or {}).get('hard_issue_ids') or [])
    first_run = before is None
    newi = sorted(now - prev) if not first_run else []
    clri = sorted(prev - now) if not first_run else []
    return res, newi, clri


def _detail(res, ids, limit=4):
    """把硬问题 id 展开成「判定依据」短行（取该目标第一条 ALERT/WARN 文本）。"""
    by = {r['id']: r for r in res.get('targets', [])}
    lines = []
    for i in ids[:limit]:
        r = by.get(i)
        if not r:
            lines.append('- %s（本次表中未出现）' % i)
            continue
        txt = next((c['text'] for c in r.get('checks', [])
                    if c['level'] in ('ALERT', 'WARN')), '无明细')
        lines.append('- %s：%s' % (i, txt))
    if len(ids) > limit:
        lines.append('- …另有 %d 项' % (len(ids) - limit))
    return '\n'.join(lines)


def check_state(res, newi, clri):
    """纯函数：把差集渲染成待推送文本；无变化返回 None（便于自测，不发通知）。"""
    if not newi and not clri:
        return None
    ts = res.get('ts') or datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    parts = ['🔔 任务产物新鲜度巡检 %s' % ts, '']
    if newi:
        parts.append('新增硬问题 %d 项：' % len(newi))
        parts.append(_detail(res, newi))
    if clri:
        parts.append('')
        parts.append('已恢复 %d 项：%s' % (len(clri), '、'.join(clri)))
    soft = [r for r in res.get('targets', [])
            if r.get('severity') == 'soft' and r.get('verdict') in ('ALERT', 'WARN')]
    if soft:
        parts.append('')
        parts.append('（另有软判据提示 %d 项：%s，不计硬问题）'
                     % (len(soft), '、'.join(r['id'] for r in soft)))
    return '\n'.join(parts)


def check():
    """反射器入口：只做发现+通知，不唤起 Agent。"""
    try:
        res, newi, clri = evaluate()
    except Exception as e:                       # 异常不外抛：打印后继续轮询
        print('[09_task_freshness] 执行异常: %s' % e)
        return None
    msg = check_state(res, newi, clri)
    if msg:
        notify_feishu(msg)
        print('[09_task_freshness] 已推送：新增 %s / 恢复 %s' % (newi or '无', clri or '无'))
    else:
        print('[09_task_freshness] 无变化（硬问题集合未变），静默')
    return None


def on_done(result):
    if result:
        notify_feishu('🤖 任务产物新鲜度\n\n%s' % result)


def selftest():
    """自测：用隔离 ledger 跑两次，验证「首次不误报 / 二次同集合静默 / 差集可算出」。"""
    import tempfile
    checks = []
    d = tempfile.mkdtemp(prefix='tf_reflect_')
    led = os.path.join(d, 'ledger.jsonl')
    res1, newi1, clri1 = evaluate(ledger=led)
    checks.append(('① 首次运行不推「新增」（无基线可比）', newi1 == [] and clri1 == [],
                   'new=%s clr=%s' % (newi1, clri1)))
    msg1 = check_state(res1, newi1, clri1)
    checks.append(('② 首次无变化 ⇒ 不推送', msg1 is None, str(msg1)[:60]))
    res2, newi2, clri2 = evaluate(ledger=led)
    checks.append(('③ 二次同输入 ⇒ 差集为空（不重复推送）', newi2 == [] and clri2 == [],
                   'new=%s clr=%s' % (newi2, clri2)))
    hard_now = sorted(res2.get('hard_issue_ids') or [])
    checks.append(('④ 语料自证：本次硬问题集合与真实环境一致（>0 或 0 均可，但须可解释）',
                   isinstance(hard_now, list) and len(res2.get('targets', [])) > 20,
                   'hard=%s targets=%d' % (hard_now, len(res2.get('targets', [])))))
    # 负控：伪造 prev（把当前硬问题当基线）⇒ 必须算出「已恢复」而不是「新增」
    prev_line = {'ts': 'x', 'hard_issue_ids': hard_now, 'counts': {}, 'issues': [], 'channels': []}
    with open(led, 'w', encoding='utf-8') as f:
        f.write(json.dumps(prev_line, ensure_ascii=False) + '\n')
    res3, newi3, clri3 = evaluate(ledger=led)
    checks.append(('⑤ 负控：基线=当前集合 ⇒ clri 为空、newi 为空（单调幂等）',
                   newi3 == [] and clri3 == [], 'new=%s clr=%s' % (newi3, clri3)))
    # 负控：基线中塞一个假 id ⇒ 必须报「已恢复」
    prev_line['hard_issue_ids'] = hard_now + ['__ghost_issue__']
    with open(led, 'w', encoding='utf-8') as f:
        f.write(json.dumps(prev_line, ensure_ascii=False) + '\n')
    res4, newi4, clri4 = evaluate(ledger=led)
    checks.append(('⑥ 负控：基线含幽灵 id ⇒ 必须报「已恢复 __ghost_issue__」',
                   clri4 == ['__ghost_issue__'], 'new=%s clr=%s' % (newi4, clri4)))

    ok = True
    for name, cond, detail in checks:
        print('   [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                                '' if cond else ('  → ' + str(detail))))
        ok = ok and bool(cond)
    print('反射器自测合计：%s（%d/%d）' % ('PASS' if ok else 'FAIL',
                                          sum(1 for _, c, _ in checks if c), len(checks)))
    print('隔离 ledger：%s（可整目录删除）' % led)
    return 0 if ok else 1


def main():
    argv = sys.argv[1:]
    if '--selftest' in argv:
        return selftest()
    if '--once' in argv:
        check()
        return 0
    print('reflector 09：由 agentmain --reflect 驱动（INTERVAL=%ds）；手动用法 --once / --selftest' % INTERVAL)
    return 0


if __name__ == '__main__':
    sys.exit(main())
