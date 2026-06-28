#!/usr/bin/env python3
"""
周巡检报告结构校验器 (weekly-inspection structure verifier)

用途：把 weekly_inspection_sop 红线第 1~9 条从"靠自觉的文字提醒"升级为
"机械可执行的硬卡口"。定时任务/手动巡检写完报告后必须跑此脚本，
不通过则禁止 notify_feishu 推送（背景：2026-06-18 漏报上周 P1、
2026-06-26 ERROR/WARN 合并章节+编号乱用，均因纯文字红线被绕过）。

用法：
    python weekly_inspection_verify.py <报告路径.md>
    # 自动定位同目录上一期报告做对账；也可显式指定：
    python weekly_inspection_verify.py <本期.md> <上期.md>

退出码：0=全部通过；1=有 FAIL（禁止推送）。WARN 不阻断但需人工确认。
"""
import re
import sys
import glob
from pathlib import Path

# 模板必备章节（红线第 8 条：标题/编号/顺序严格对齐，禁合并禁改名禁省略）
REQUIRED_SECTIONS = [
    "## 1. 概览",
    "### 1.1",
    "### 1.2",
    "### 1.3",
    "## 2. 问题优先级清单",
    "## 3. ERROR 类问题详细分析",
    "## 4. WARN 类问题详细分析",
    "## 5. 巡检漏查专项分析",
    "## 6. 本周行动清单",
    "## 7. 经验教训",
    "## 8. 预防措施",
    "## 9. 修复提交记录",
    "## 10. 关联文档",
]


def _find_prev_report(cur_path: Path):
    """同目录按文件名中的日期排序，取本期之前最近的一期。"""
    sibs = sorted(glob.glob(str(cur_path.parent / "weekly-inspection-postmortem-*.md")))
    sibs = [s for s in sibs if Path(s).resolve() != cur_path.resolve()
            and "eval-test" not in s]
    # 文件名里的日期升序，取严格小于当前文件名的最后一个
    cur_name = cur_path.name
    prev = [s for s in sibs if Path(s).name < cur_name]
    return Path(prev[-1]) if prev else None


def _extract_issue_ids(text: str):
    """提取报告中所有问题编号（E1a/E1b/W1/P1...），用于上期对账。"""
    ids = set()
    # 标题里的 ### E1a · / ### W3 ·
    ids |= set(re.findall(r'^###\s+(E\d+[a-z]?|W\d+|P\d+)\b', text, re.M))
    # 表格首列 | E1a | / | W3 | / | P1 |
    ids |= set(re.findall(r'^\|\s*(E\d+[a-z]?|W\d+|P\d+)\s*\|', text, re.M))
    return ids


def verify(report_path: str, prev_path: str = None):
    cur = Path(report_path)
    text = cur.read_text(encoding="utf-8")
    fails, warns, oks = [], [], []

    # ---- 校验 1：13 必备章节齐全且顺序正确（红线第 8 条）----
    last_pos = -1
    order_ok = True
    for sec in REQUIRED_SECTIONS:
        pos = text.find(sec)
        if pos == -1:
            fails.append(f"[章节] 缺失必备章节: {sec}")
        elif pos < last_pos:
            order_ok = False
        else:
            last_pos = pos
    if order_ok and not any("缺失必备章节" in f for f in fails):
        oks.append("13 必备章节齐全且顺序正确")
    elif not order_ok:
        fails.append("[章节] 章节顺序与模板不一致")

    # ---- 校验 2：ERROR/WARN 必须分两章，禁合并 ----
    if "## 3. ERROR" in text and "## 4. WARN" in text:
        oks.append("ERROR/WARN 已分两章（未合并）")
    else:
        fails.append("[章节] ERROR 与 WARN 未按模板分为第 3、4 两章")

    # ---- 校验 3：问题编号规范 E\d[a-z]?/W\d，禁裸 N1 等自创编号 ----
    bad_ids = set(re.findall(r'^###\s+(N\d+|[A-DF-VX-Z]\d+)\b', text, re.M))
    titles = re.findall(r'^###\s+([A-Z]\d+[a-z]?)\b', text, re.M)
    legal = [t for t in titles if re.fullmatch(r'(E\d+[a-z]?|W\d+)', t)]
    if bad_ids:
        fails.append(f"[编号] 出现非法问题编号(应为 E1a/W1): {sorted(bad_ids)}")
    elif legal:
        oks.append(f"问题编号合规: {legal}")

    # ---- 校验 4：每个 E/W 问题四段式齐全（红线第 1 条）----
    issue_count = len(legal)
    for seg in ["**现象**", "**根因", "**影响**", "**建议**"]:
        c = text.count(seg)
        if issue_count and c < issue_count:
            warns.append(f"[四段式] '{seg}' 出现 {c} 次 < 问题数 {issue_count}，可能有问题缺段")
    if issue_count and all(text.count(s) >= issue_count
                           for s in ["**现象**", "**根因", "**影响**", "**建议**"]):
        oks.append(f"四段式齐全（{issue_count} 个问题）")

    # ---- 校验 5：mermaid xychart x-axis 项数 == 每组 bar 元素数（防静默渲染失败）----
    for block in re.findall(r'```mermaid(.*?)```', text, re.S):
        if "xychart" not in block:
            continue
        m = re.search(r'x-axis\s+\[(.*?)\]', block, re.S)
        if not m:
            fails.append("[图表] xychart 缺 x-axis 定义")
            continue
        xn = len(re.findall(r'"[^"]*"', m.group(1)))
        bars = re.findall(r'bar\s+\[(.*?)\]', block, re.S)
        for i, b in enumerate(bars):
            bn = len([x for x in b.split(',') if x.strip()])
            if bn != xn:
                fails.append(f"[图表] mermaid 第{i+1}组 bar 元素数 {bn} != x-axis 项数 {xn}（会渲染失败）")
        if bars and all(len([x for x in b.split(',') if x.strip()]) == xn for b in bars):
            oks.append(f"mermaid xychart 校验通过(x-axis={xn}, {len(bars)}组bar)")

    # ---- 校验 6：双层兜底——有 mermaid 图必须有纯文本数据条兜底 ----
    if "xychart" in text:
        if re.search(r'[█░▓]', text) or re.search(r'本周.*上周.*环比', text):
            oks.append("可视化双层兜底存在（mermaid + 纯文本数据条）")
        else:
            warns.append("[图表] 有 mermaid 但未见纯文本数据条兜底层")

    # ---- 校验 7：目录锚点（红线第 7 条）----
    if "## 目录" in text and "(#" in text.split("## 目录")[1][:2000]:
        oks.append("目录锚点存在")
    else:
        warns.append("[目录] 未检测到带锚点的目录")

    # ---- 校验 8：上期对账（红线第 9 条，最关键，防 P1 静默消失）----
    prev = Path(prev_path) if prev_path else _find_prev_report(cur)
    if prev and prev.exists():
        prev_text = prev.read_text(encoding="utf-8")
        prev_ids = _extract_issue_ids(prev_text)
        # 上期高优先级/安全问题必须在本期 1.3 生命周期章节出现并给结论
        sec13 = ""
        m = re.search(r'### 1\.3(.*?)(?=\n## |\Z)', text, re.S)
        if m:
            sec13 = m.group(1)
        concl_kw = ["持续", "收敛", "消除", "归零", "遗留", "已修复", "复现"]
        missing = []
        for pid in sorted(prev_ids):
            # 在本期全文(优先1.3)中是否被提及并给结论
            mentioned = pid in text
            in_lifecycle = pid in sec13
            if not mentioned:
                missing.append(pid)
        if missing:
            fails.append(f"[对账] 上期问题在本期未提及(疑似静默消失): {missing} | 上期={prev.name}")
        elif not sec13:
            fails.append("[对账] 缺 1.3 问题生命周期章节，无法对账")
        else:
            has_concl = any(k in sec13 for k in concl_kw)
            if has_concl:
                oks.append(f"上期对账通过：{len(prev_ids)} 个上期问题均在本期被提及 | 上期={prev.name}")
            else:
                warns.append("[对账] 1.3 章节未见结论关键词(持续/收敛/消除等)")
    else:
        warns.append("[对账] 未找到上一期报告，跳过对账（首期可忽略）")

    # ---- 输出 ----
    print(f"=== 周巡检报告结构校验：{cur.name} ===")
    for o in oks:
        print(f"  ✅ {o}")
    for w in warns:
        print(f"  ⚠️  {w}")
    for f in fails:
        print(f"  ❌ {f}")
    print(f"--- 结果：{len(oks)} 通过 / {len(warns)} 警告 / {len(fails)} 失败 ---")
    if fails:
        print(">>> FAIL：禁止 notify_feishu 推送，先修复上述 ❌ 项后重跑校验。")
        return 1
    if warns:
        print(">>> PASS(带警告)：结构合规，⚠️ 项需人工确认后再推送。")
    else:
        print(">>> PASS：结构完全对齐模板，可推送。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    rp = sys.argv[1]
    pp = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(verify(rp, pp))
