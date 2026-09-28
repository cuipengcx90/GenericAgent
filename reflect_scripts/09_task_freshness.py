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


def _keys_of_record(rec):
    """台账「比较键」：新格式记录 = 跨轮稳定事件 ID；旧格式记录 = 硬问题 id（回落）。

    区分依据=记录里有无 `issues` 键（新格式即便零告警也带该键）——因此「新格式 + 空事件集合」
    表示「确实没有告警」，不得回落成 id 空间，否则会把自己与 id 空间比出全量假差集。
    """
    if not isinstance(rec, dict):
        return set(), 'none'
    if 'issues' in rec:
        # R480：键**只**从 issues 派生（issues 只收硬目标 ALERT/WARN）。顶层 event_ids 是汇总字段，
        # 修正前曾并入非硬目标事件 ⇒ 若取用会与「只统计硬目标」的本次结果不对称，制造幻影「已恢复」。
        ks = set()
        for iss in rec.get('issues') or []:
            ks |= set(iss.get('event_ids') or [])
            for e in iss.get('events') or []:
                if e.get('event_id'):
                    ks.add(e['event_id'])
        return ks, 'event'
    return set(rec.get('hard_issue_ids') or []), 'id'


def _keys_of_result(res):
    """本次运行输出的比较键（与台账同口径）：优先事件 ID，工具核未挂上时回落硬问题 id。"""
    ev = set()
    for r in res.get('targets', []):
        if r.get('verdict') not in ('ALERT', 'WARN'):
            continue
        ev |= set(r.get('event_ids') or [])
        for e in r.get('event_details') or []:
            if e.get('event_id'):
                ev.add(e['event_id'])
    if ev:
        return ev, 'event'
    return set(res.get('hard_issue_ids') or []), 'id'


def _label_map(res):
    """事件 ID → 「target·face」可读标签（渲染通知用；查不到的原样回落到硬问题 id 分支）。"""
    m = {}
    for r in res.get('targets', []):
        for e in r.get('event_details') or []:
            if e.get('event_id'):
                m[e['event_id']] = '%s·%s' % (e.get('target', ''), e.get('face', ''))
    return m


def evaluate(ledger=LEDGER, root=GA, timeout=600):
    """跑一次工具，返回 (result_dict, new_keys, cleared_keys)。"""
    before = _last_ledger(ledger)
    p = subprocess.run([sys.executable, str(TOOL), '--root', str(root),
                        '--ledger', str(ledger), '--json'],
                       capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError('task_freshness 无输出（rc=%s）: %s' % (p.returncode, (p.stderr or '')[-400:]))
    res = json.loads(out)
    now, now_mode = _keys_of_result(res)
    prev, prev_mode = _keys_of_record(before)
    first_run = before is None
    res['_key_mode'] = now_mode
    # 键空间切换（旧 id 台账 → 新事件台账）当轮不判差集：两个空间不可比，报了必是假警报
    res['_key_mode_transition'] = bool(not first_run and prev_mode != now_mode)
    if first_run or res['_key_mode_transition']:
        return res, [], []
    return res, sorted(now - prev), sorted(prev - now)


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
        labels = _label_map(res)
        ev = [k for k in newi if k in labels]
        ids = [k for k in newi if k not in labels]
        if ev:                                  # 事件 ID 空间（R480）：同一件事跨轮同 ID
            parts.append('新增告警事件 %d 项：' % len(ev))
            parts.append('\n'.join('- %s [%s]' % (labels[k], k) for k in ev[:4])
                         + ('\n- …另有 %d 项' % (len(ev) - 4) if len(ev) > 4 else ''))
        if ids:                                 # 旧格式回落（硬问题 id）与事件空间混报时兜底
            parts.append('新增硬问题 %d 项：' % len(ids))
            parts.append(_detail(res, ids))
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
    # ---- R480：差集键规范化（合成记录，不跑工具）----
    k_new, m_new = _keys_of_record({'ts': 'x', 'event_ids': ['ev-a', 'ev-b'],
                                    'issues': [{'id': 't1', 'event_ids': ['ev-b'],
                                                'events': [{'event_id': 'ev-c'}]}],
                                    'hard_issue_ids': ['t1']})
    checks.append(('⑤ 键空间：新格式台账按键 = issues 派生的事件 ID（不取顶层汇总、不混硬问题 id）',
                   m_new == 'event' and k_new == {'ev-b', 'ev-c'},
                   '%s %s' % (m_new, sorted(k_new))))
    # 负控：顶层汇总混入非硬目标/历史遗留事件 ⇒ 必须忽略，否则跨轮会报幻影「已恢复」
    k_ph, m_ph = _keys_of_record({'ts': 'x', 'event_ids': ['ev-top-ghost', 'ev-b'],
                                  'issues': [{'id': 't1', 'event_ids': ['ev-b'], 'events': []}]})
    checks.append(('⑤b 负控：顶层 event_ids 的幻影事件被忽略（键只认 issues 派生）',
                   m_ph == 'event' and k_ph == {'ev-b'}, '%s %s' % (m_ph, sorted(k_ph))))
    k_old, m_old = _keys_of_record({'ts': 'x', 'hard_issue_ids': ['t1', 'ghost']})
    checks.append(('⑥ 键空间：旧格式台账回落硬问题 id（兼容 R479 前存量台账）',
                   m_old == 'id' and k_old == {'t1', 'ghost'}, '%s %s' % (m_old, sorted(k_old))))
    # 负控：伪造旧格式 prev（只有 id，无 issues 键）⇒ 键空间切换当轮静默，不得报「全量新增」
    prev_line = {'ts': 'x', 'hard_issue_ids': hard_now + ['__ghost_issue__'],
                 'counts': {}, 'channels': []}
    with open(led, 'w', encoding='utf-8') as f:
        f.write(json.dumps(prev_line, ensure_ascii=False) + '\n')
    res3, newi3, clri3 = evaluate(ledger=led)
    checks.append(('⑥b 负控：旧 id 台账 → 新事件台账 ⇒ 当轮静默 + 标记 transition（防一次假全量）',
                   newi3 == [] and clri3 == [] and res3.get('_key_mode_transition') is True,
                   'new=%s clr=%s trans=%s' % (newi3, clri3, res3.get('_key_mode_transition'))))

    # ---- R480：差集键 = 跨轮稳定事件 ID（新格式台账），旧格式回落硬问题 id ----
    def _ev_ids(res):
        out = []
        for r in res.get('targets', []):
            if r.get('verdict') in ('ALERT', 'WARN'):
                for e in r.get('event_ids') or []:
                    if e not in out:
                        out.append(e)
        return out

    ev_now = _ev_ids(res2)
    new_line = {'ts': 'x2', 'counts': {}, 'hard_issue_ids': hard_now, 'event_ids': ev_now,
                'issues': [{'id': i, 'event_ids': [e for e in ev_now if e.startswith('ev-')],
                            'events': [], 'first': ''} for i in hard_now],
                'channels': []}
    with open(led, 'w', encoding='utf-8') as f:
        f.write(json.dumps(new_line, ensure_ascii=False) + '\n')
    res5, newi5, clri5 = evaluate(ledger=led)
    checks.append(('⑦ 新格式台账（事件 ID 键）⇒ 同一轮幂等静默，且键空间标记为 event',
                   newi5 == [] and clri5 == [] and res5.get('_key_mode') == 'event',
                   'new=%s clr=%s mode=%s' % (newi5, clri5, res5.get('_key_mode'))))

    # 负控：新格式但事件集合为空（保留 issues 键）⇒ 必须判「全部已恢复」，不得回落成 id 空间
    empty_line = dict(new_line, event_ids=[],
                      issues=[{'id': i, 'event_ids': [], 'events': [], 'first': ''} for i in hard_now])
    with open(led, 'w', encoding='utf-8') as f:
        f.write(json.dumps(empty_line, ensure_ascii=False) + '\n')
    res6, newi6, clri6 = evaluate(ledger=led)
    checks.append(('⑧ 负控：新格式空事件集合 ⇒ 判「上一轮无告警、本轮全新增」，不得回落 id 空间',
                   set(newi6) == set(ev_now) and clri6 == [] and all(k.startswith('ev-') for k in newi6),
                   'new=%s clr=%s' % (newi6, clri6)))

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
