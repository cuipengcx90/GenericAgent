#!/usr/bin/env python3
"""07 L2 验收闸门巡检（反射器版）

契约与官方 01-06 一致：INTERVAL + check() + on_done()。
- 轮询本身 0 LLM 成本：check() 只跑本地脚本（重建索引 + 机械验收）。
- 有 ERROR 时由 l2_watch 自己推送（带"错误指纹"去重，24h 内同一批错只推一次），
  并且 check() 返回 None —— 因为「补证据 / 重开票据」是人的决策，不该让 Agent 去乱动 Trellis 状态。
- 若想让 Agent 介入分析，把 FORWARD_TO_AGENT 打开（会返回任务描述，由反射器唤起 Agent）。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu  # noqa: E402

INTERVAL = 1800          # 30 分钟
GA = Path('/Users/cuip/projects/GenericAgent')
# 2026-09-22 起 L2 控制面（l2_v0 + l2_watch）移入工程仓 control-plane/，权威在仓里
WATCH = Path('/Users/cuip/develop/herdr-agents/control-plane/l2_watch.py')
if not WATCH.exists():                                  # 兼容旧位置
    WATCH = GA / 'temp' / 'l2_watch.py'
FORWARD_TO_AGENT = False  # True = 有 ERROR 时唤起 Agent 一起诊断


def check():
    if not WATCH.exists():
        return None
    try:
        p = subprocess.run(
            [sys.executable, str(WATCH), '--repo', str(GA), '--quiet', '--notify'],
            capture_output=True, text=True, timeout=180)
    except Exception as e:                      # 异常不外抛：打印后继续轮询
        print(f'[07_l2_gate] l2_watch 执行异常: {e}')
        return None
    if p.returncode == 0:
        return None                             # 全绿：静默，零成本
    tail = (p.stdout or p.stderr or '').strip().splitlines()[-8:]
    if FORWARD_TO_AGENT:
        return ("L2 验收闸门不通过：\n" + "\n".join(tail) +
                "\n请诊断，注意：状态权威仍是 .trellis/scripts/task.py，禁止手改 .trellis 下的文件。")
    return None


def on_done(result: str):
    if result:
        notify_feishu(f"🤖 L2 验收闸门\n\n{result}")
