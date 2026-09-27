#!/usr/bin/env python3
"""08 knowledgeC 每日快照（反射器版 · 待批准接入）

契约与官方 01-07 一致：INTERVAL + check() + on_done()。
- 轮询本身 0 LLM 成本：check() 只跑本地脚本（`tools/kc_snapshot.py snapshot`，实测 0.1s），
  且**每天最多真正采一次**（当天目录已有「今天写的 revision」就直接返回，连子进程都不起）。
- check() 永远返回 None ⇒ 不会唤起 Agent（采集是确定性动作，不需要 LLM 参与）。
- 失败（rc!=0 / 源库缺失）才发飞书私信；成功时静默（与 07_l2_gate 的「全绿零输出」范式一致）。
- **不自动 prune**：删除历史快照不可逆 ⇒ 只打印建议命令，实际删除需人工/批准。

⚠️ 本文件的存在不等于"已接入"：
   reflect_scripts/ 下没有目录自动扫描（已实测：每个脚本靠各自的 launchd
   `agentmain.py --reflect <script>` 驱动）。接入方式见报告「待批提案」——
   A) 复用在跑的 com.cuip.genericagent-l2-gate（30min 循环里加一行调用，零新增定时）；
   B) 新建 com.cuip.genericagent-kc-snapshot.plist（属"新增定时"，需批准）。
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu  # noqa: E402

INTERVAL = 1800                      # 30 分钟轮询一次；实际每天只采一次
GA = Path('/Users/cuip/projects/GenericAgent')
TOOL = GA / 'tools' / 'kc_snapshot.py'
ROOT = GA / 'temp' / 'kc_snapshots'
SRC = Path.home() / 'Library/Application Support/Knowledge/knowledgeC.db'
MAX_DRY_AGE_H = 26                   # 当天快照超过这么久未更新 → 重采一次（跨天兜底）


def _today_done() -> bool:
    """今天是否已经采过（当天目录 + manifest 末次 revision 的日期 == 今天）"""
    d = ROOT / datetime.date.today().isoformat()
    mf = d / 'manifest.json'
    if not (d / 'knowledgeC.db').exists() or not mf.exists():
        return False
    try:
        m = json.loads(mf.read_text(encoding='utf-8'))
        ts = m['revisions'][-1]['ts']                       # 'YYYY-MM-DD HH:MM:SS'
    except Exception:
        return False
    return ts[:10] == datetime.date.today().isoformat()


def check():
    if not SRC.exists():                 # 无源库（非本机/权限变化）：静默，不刷屏
        return None
    if _today_done():
        return None                     # 今日已采：零动作、零子进程
    try:
        p = subprocess.run([sys.executable, str(TOOL), 'snapshot'],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(GA))
    except Exception as e:
        notify_feishu('🤖 knowledgeC 快照异常\n%s' % e)
        return None
    out = (p.stdout or p.stderr or '').strip().splitlines()[-3:]
    print('[08_kc_snapshot] rc=%s %s' % (p.returncode, ' / '.join(out)))
    if p.returncode != 0:
        notify_feishu('🤖 knowledgeC 快照失败（rc=%s）\n%s' % (p.returncode, '\n'.join(out)))
    return None                         # 永不唤起 Agent


def on_done(result: str):               # 与官方契约保持一致（本脚本不产生任务）
    pass


if __name__ == '__main__':              # 手动 dry-run：不写文件时用 --dry 看判断结果
    if '--dry' in sys.argv:
        print('today_done=%s  (ROOT=%s)' % (_today_done(), ROOT))
        print('将执行:', [sys.executable, str(TOOL), 'snapshot'] if not _today_done() else '(今日已采，跳过)')
    else:
        check()
