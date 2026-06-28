#!/usr/bin/env python3
"""
周巡检报告「校验 + 必达推送」入口 (verify-and-always-notify)

核心原则（2026-06-26 教训）：**通知必达，绝不因格式问题静默消失**。
运维巡检第一要务是把 P1/P2/P3 问题送到人眼前；报告格式漂移只是次要的
质量问题，绝不能因为格式不达标就把告警一起吞掉（那等于重蹈 06-18 漏报
P1 的覆辙）。

因此本入口的行为是：
  - 校验 PASS → 推送正常通知（含问题摘要 + 报告路径）
  - 校验 FAIL → **仍然推送**，但改发「降级告警」：在正文顶部加 ⚠️ 格式未达标
                标记 + 失败项清单，正文仍保留 P1/P2/P3 摘要与报告路径，
                逼人工去修格式，但运维信息一条不丢。
  - 报告文件不存在 → 这是比格式更严重的异常，**也必须推送**一条"报告缺失"
                告警，让人知道本次巡检产出异常，而不是悄无声息。

校验结果只影响"通知长什么样"，永远不影响"通知发不发"。

用法：
    python verify_then_notify.py <报告路径.md> "<飞书推送正文>"
    python verify_then_notify.py <报告路径.md> "<正文>" <上期报告.md(可选)>

退出码：0=校验通过且已推送 / 3=校验未通过但已发降级告警 / 4=报告缺失但已发告警
        （退出码用于日志追溯，非阻断；任何情况都已推送）
"""
import sys
import os
import io
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _do_notify(text: str) -> bool:
    """真实推送；失败返回 False 但不抛，避免推送通道异常导致整流程崩溃。"""
    try:
        sys.path.insert(0, "/Users/cuip/projects/GenericAgent")
        from reflect_scripts.reflect_common import notify_feishu
        notify_feishu(text)
        return True
    except Exception as e:
        print(f">>> ⚠️ 推送通道异常: {e}")
        return False


def verify_and_notify(report_path: str, feishu_text: str, prev_path: str = None) -> int:
    """先校验，再【无论结果都推送】。校验仅决定通知形态，不决定是否推送。"""
    # 1. 报告缺失：比格式更严重的异常，也必须告警（不能静默）
    if not os.path.isfile(report_path):
        alert = ("🚨【周巡检异常·需人工介入】\n"
                 f"报告文件未生成或路径错误：{report_path}\n"
                 "本次巡检产出异常，请立即人工检查 SLS 巡检流程与报告生成步骤。")
        _do_notify(alert)
        print(">>> 报告缺失，已发送异常告警（非静默）。")
        return 4

    # 2. 跑结构校验，捕获其打印输出用于嵌进降级告警
    from weekly_inspection_verify import verify
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = verify(report_path, prev_path)
    verify_log = buf.getvalue()
    print(verify_log)  # 同时回显到控制台供日志追溯

    # 3. 无论 PASS/FAIL 都推送，仅改变正文形态
    if rc == 0:
        _do_notify(feishu_text)
        print(">>> ✅ 校验通过，已推送正常通知一次。")
        return 0

    # FAIL：抽取失败项，发降级告警，但保留原始问题摘要与报告路径
    fail_lines = [ln.strip() for ln in verify_log.splitlines() if "❌" in ln]
    fail_block = "\n".join(fail_lines) if fail_lines else "（见调度日志）"
    degraded = (
        "⚠️【周巡检完成·但报告格式未达标，需人工修复】\n"
        "──以下为本次巡检实际问题摘要，请优先处理──\n"
        f"{feishu_text}\n"
        "──格式校验未通过项（不影响上方问题，仅需补齐格式）──\n"
        f"{fail_block}\n"
        f"请按校验项修复报告后重跑 verify_then_notify.py（报告路径见上）。"
    )
    _do_notify(degraded)
    print(">>> ⚠️ 校验未通过，已发送【降级告警】（含问题摘要+失败项），通知未丢失。")
    return 3


# 兼容旧入口名
verify_then_notify = verify_and_notify


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    report = sys.argv[1]
    text = sys.argv[2]
    prev = sys.argv[3] if len(sys.argv) > 3 else None
    sys.exit(verify_and_notify(report, text, prev))
