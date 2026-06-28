#!/usr/bin/env python3
"""
周巡检报告「骨架生成器」(generation-layer structure lock)

为什么存在（2026-06-26 第三次纠偏，根因层解决）：
  前两版方案都在「输出侧」打补丁——校验、降级告警，都是报告已经写歪之后
  的补救。但报告每次由 LLM 自由复述模板重写，结构靠自觉，自然反复漂移
  （ERROR/WARN 合并章、编号 N1、mermaid x-axis 与 bar 不等长…）。
  真正的根因解法是**从生成侧锁死结构**：不让 LLM 自由写章节骨架。

做法：
  本脚本吃一份极简 JSON（只描述"有哪些问题、各自短描述与量级"等内容性数据），
  按问题数量用代码生成完整的 13 章骨架——章节标题/编号/顺序、ERROR(第3章)与
  WARN(第4章)分章、E1a/E1b/W1 编号、目录锚点、每个问题的四段式空架子、
  mermaid 块（x-axis 列表与两组 bar 列表同源派生，长度恒等，渲染必不失败）。
  LLM 只需把骨架里的 `{填:...}` 文字槽替换成分析内容，**碰不到结构**。
  结构正确性由代码保证，不由 LLM 自觉保证 → 漂移在生成阶段即不可能发生。

输入 JSON 结构（见 SAMPLE_SPEC）：
  meta: 日期/版本/状态等头部字段
  errors: [{code_suffix?, desc, module}]  → 自动编号 E1a,E1b,E1c...
  warns:  [{desc}]                         → 自动编号 W1,W2,W3...
  trend:  [{name, this_week, last_week}]   → mermaid + 数据条，x-axis 与 bar 同源

用法：
  python gen_report_skeleton.py spec.json > report.md
  # 或
  python gen_report_skeleton.py spec.json /目标/report.md
"""
import sys, json, re


def _slug(s):
    """生成 GitHub 风格锚点：小写、空格转-、去除特殊符号。"""
    s = s.lower().strip()
    s = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def _err_codes(n):
    """ERROR 编号：E1a,E1b,...E1z,E2a... 按字母递增（与模板 E1a/E1b 风格一致）。"""
    out = []
    for i in range(n):
        out.append(f"E1{chr(ord('a') + i)}" if i < 26 else f"E{i//26+1}{chr(ord('a')+i%26)}")
    return out


def _bar_cell(val, vmax, width=24):
    if vmax <= 0:
        filled = 0
    else:
        filled = round(val / vmax * width)
    return "`" + "█" * filled + "░" * (width - filled) + "`"


def gen(spec):
    m = spec["meta"]
    errors = spec.get("errors", [])
    warns = spec.get("warns", [])
    trend = spec.get("trend", [])

    ecodes = _err_codes(len(errors))
    wcodes = [f"W{i+1}" for i in range(len(warns))]

    L = []  # 行缓冲
    a = L.append

    # ---- 头部 ----
    a(f"# SLS 日志周巡检复盘报告（{m['start']} ~ {m['end']}）\n")
    a(f"> 巡检周期：{m['start']} ~ {m['end']}")
    a(f"> 镜像版本：{m.get('image', '{填:镜像版本}')}")
    a("> 数据来源：SLS smyze-cloud / bar-smyze-opserver")
    a(f"> 报告日期：{m.get('report_date', m['end'])}")
    a(f"> 严重等级：{m.get('severity', '{填:最高等级}')}")
    a(f"> 状态：{m.get('status', '{填:状态概述}')}\n")
    a("---\n")

    # ---- 目录（锚点由代码生成，恒正确）----
    a("## 目录\n")
    a("- [1. 概览](#1-概览)")
    a("  - [1.3 问题生命周期与周环比](#13-问题生命周期与周环比)")
    a("- [2. 问题优先级清单](#2-问题优先级清单)")
    a("- [3. ERROR 类问题详细分析](#3-error-类问题详细分析)")
    for c, e in zip(ecodes, errors):
        a(f"  - [{c} · {e['desc']}](#{_slug(c + '-' + e['desc'])})")
    a("- [4. WARN 类问题详细分析](#4-warn-类问题详细分析)")
    for c, w in zip(wcodes, warns):
        a(f"  - [{c} · {w['desc']}](#{_slug(c + '-' + w['desc'])})")
    a("- [5. 巡检漏查专项分析](#5-巡检漏查专项分析)")
    a("- [6. 本周行动清单](#6-本周行动清单)")
    a("- [7. 经验教训](#7-经验教训)")
    a("- [8. 预防措施](#8-预防措施)")
    a("- [9. 修复提交记录（待更新）](#9-修复提交记录待更新)")
    a("- [10. 关联文档](#10-关联文档)\n")
    a("---\n")

    # ---- 1. 概览 ----
    a("## 1. 概览\n")
    a("### 1.1 巡检数据汇总\n")
    a("| 指标 | 数值 | 说明 |")
    a("|------|------|------|")
    a(f"| 巡检周期 | {m['start']} ~ {m['end']} | {m.get('days','{填:N}')} 天 |")
    a("| 镜像版本 | {填:版本} | {填:是否更新} |")
    a("| ERROR 总量 | {填:N} 条 | {填:峰值日期与峰值} |")
    a("| WARN 总量 | {填:N}+ 条 | {填:趋势} |")
    a(f"| 排查问题数 | {len(errors)+len(warns)} 条 | ERROR 类 {len(errors)} 条，WARN 类 {len(warns)} 条 |")
    a("| 漏查问题数 | {填:N，无则写无} | {填:说明} |")
    a("| 持续告警数 | {填:N（编号列表）} | 今日仍在产生 |")
    a("| 深度调查覆盖 | {填:编号列表} | 已用代码+DB+Redis+SLS 交叉验证 |\n")
    a("### 1.2 ERROR 峰值分析\n")
    a("{填:描述峰值日构成，各问题贡献条数；有日志冗余机制需标注}\n")
    a("### 1.3 问题生命周期与周环比\n")
    a("**周环比总览**\n")
    a("| 维度 | 本周 | 上周 | 环比 |")
    a("|---|---:|---:|---|")
    a("| ERROR 总量 | {填:N} | {填:N} | {填:↗/↘/—} |")
    a("| WARN 总量 | {填:N} | {填:N} | {填:↗/↘/—} |")
    a("| 新增问题 | {填:N（编号）} | — | — |")
    a("| 持续遗留 | {填:N（编号）} | — | — |")
    a("| 本周收敛/消除 | {填:N（编号）} | — | — |\n")
    a("**本周 vs 上周·高频问题趋势对比（可视化，必填）**\n")

    # mermaid：x-axis 与两组 bar 全部从 trend 同源派生，长度恒等
    names = [t["name"] for t in trend]
    tw = [t.get("this_week", 0) for t in trend]
    lw = [t.get("last_week", 0) for t in trend]
    vmax = max(tw + lw + [1])
    ylim = int(vmax * 1.1) + 1
    a("```mermaid")
    a("xychart-beta")
    a('    title "本周 vs 上周 · 高频问题发生量对比"')
    a("    x-axis [" + ", ".join(f'"{n}"' for n in names) + "]")
    a(f'    y-axis "发生量（条）" 0 --> {ylim}')
    a("    bar [" + ", ".join(str(v) for v in tw) + "]")
    a("    bar [" + ", ".join(str(v) for v in lw) + "]")
    a("```\n")
    a("> 第一组（前景）为本周，第二组为上周。\n")
    # 纯文本数据条兜底（同源）
    a("| 问题 | 本周 | 上周 | 环比 |")
    a("|---|---|---|---|")
    for n, a_, b_ in zip(names, tw, lw):
        arrow = "↗" if a_ > b_ else ("↘" if a_ < b_ else "—")
        a(f"| {n} | {_bar_cell(a_, vmax)} {a_} | {_bar_cell(b_, vmax)} {b_} | {arrow} {{填:判断}} |")
    a(f"\n> 数据条按全表最大值 {vmax} 等比缩放到 24 格；`*` 见节末注释。\n")
    a("#### 🆕 本周新增（首次在巡检中出现）\n")
    a("| 编号 | 问题 | 优先级 | 首次出现 | 本周发生量 | 上周发生量 | 趋势 | 状态 |")
    a("|---|---|---|---|---:|---:|---|---|")
    a("| {填:Wn} | {填:描述} | {填:Px} | {填:日期（本周）} | {填:量} | 0 | ↗ 新爆发 | {填:状态} |\n")
    a("#### 🔁 上周持续遗留（跨周未闭环）\n")
    a("| 编号 | 问题 | 优先级 | 首次出现 | 持续期数 | 本周发生量 | 上周发生量 | 趋势 | 状态 |")
    a("|---|---|---|---|:---:|---:|---:|---|---|")
    a("| {填:Wn} | {填:描述} | {填:Px} | {填:日期（上周编号）} | {填:N} | {填:量} | {填:量} | {填:趋势} | {填:状态} |\n")
    a("#### ✅ 本周收敛 / 已消除（上周存在，本周明显下降或归零）\n")
    a("| 上周编号 | 问题 | 上周优先级 | 本周发生量 | 上周发生量 | 趋势 | 当前判断 |")
    a("|---|---|---|---:|---:|---|---|")
    a("| {填:En} | {填:描述} | {填:Px} | {填:量} | {填:量} | {填:✅/↘} | {填:判断} |\n")
    a("> 校正提示：若上一期 P1/安全类问题在本期被漏记或降级，必须在此用 `⚠️` 标出 SLS 实查证据并补记。\n")
    a("---\n")

    # ---- 2. 优先级清单 ----
    a("## 2. 问题优先级清单\n")
    a("| 编号 | 问题描述 | 优先级 | 状态 | 本周发生量 | 备注 |")
    a("|------|---------|--------|------|-----------|------|")
    for c, e in zip(ecodes, errors):
        a(f"| {c} | {e['desc']} | {{填:Px}} | {{填:状态}} | {{填:量}} | {{填:关键信息}} |")
    for c, w in zip(wcodes, warns):
        a(f"| {c} | {w['desc']} | {{填:Px}} | {{填:状态}} | {{填:量}} | {{填:关键信息}} |")
    a("\n---\n")

    # ---- 3. ERROR 四段式 ----
    a("## 3. ERROR 类问题详细分析\n")
    for c, e in zip(ecodes, errors):
        a(f"### {c} · {e['desc']}（{e.get('module','{填:模块}')}）\n")
        a("**现象**\n")
        a("{填:时间范围、模块/接口、批量失败描述}\n")
        a("```")
        a("{填:关键日志摘要 2-5 行，含异常类名和关键SQL/参数}")
        a("```\n")
        a("{填:捕获路径说明；集中时间与峰值条数；收敛状态}\n")
        a("**根因（已通过 {填:SLS/DB/代码/Redis} 核实）**\n")
        a("{填:根因描述，含代码路径/DDL状态/配置缺失}\n")
        a("**影响**\n")
        a("- {填:影响范围}")
        a("- {填:业务影响}\n")
        a("**建议**\n")
        a("1. {填:具体建议}")
        a("2. {填:具体建议}\n")
        a("---\n")

    # ---- 4. WARN 四段式 ----
    a("## 4. WARN 类问题详细分析\n")
    for c, w in zip(wcodes, warns):
        a(f"### {c} · {w['desc']}（{w.get('module','{填:模块}')}）\n")
        a("**现象**\n")
        a("{填:时间范围、模块、现象描述}\n")
        a("```")
        a("{填:关键日志摘要 2-5 行}")
        a("```\n")
        a("{填:捕获路径与频次；收敛状态}\n")
        a("**根因（已通过 {填:验证方式} 核实）**\n")
        a("{填:根因描述}\n")
        a("**影响**\n")
        a("- {填:影响范围}")
        a("- {填:业务影响}\n")
        a("**建议**\n")
        a("1. {填:具体建议}")
        a("2. {填:具体建议}\n")
        a("---\n")

    # ---- 5~10 ----
    a("## 5. 巡检漏查专项分析\n")
    a("{填:若发现之前巡检遗漏的问题，分析漏查原因与机制；无则写『本期无漏查』}\n")
    a("---\n")
    a("## 6. 本周行动清单\n")
    a("| 优先级 | 行动 | 负责角色 | 截止时间 | 状态 |")
    a("|--------|------|---------|---------|------|")
    a("| {填:Px} | {填:行动} | {填:角色} | {填:截止} | 待处理 |\n")
    a("---\n")
    a("## 7. 经验教训\n")
    a("### 教训 1：{填:标题}\n")
    a("{填:问题场景}\n")
    a("**规则**：{填:可写入规范的具体规则，含阈值与操作指引}\n")
    a("---\n")
    a("## 8. 预防措施\n")
    a("### 8.1 短期措施（本周内落地）\n")
    a("| 措施 | 执行人 | 说明 |")
    a("|------|--------|------|")
    a("| {填:措施} | {填:角色} | {填:细节} |\n")
    a("### 8.2 长期措施（下个迭代规划）\n")
    a("| 措施 | 执行人 | 说明 |")
    a("|------|--------|------|")
    a("| {填:措施} | {填:角色} | {填:细节} |\n")
    a("---\n")
    a("## 9. 修复提交记录（待更新）\n")
    a("| 编号 | commit | 说明 | 状态 |")
    a("|------|--------|------|------|")
    a("| - | TBD | {填:fix/feat 描述} | 待提交 |\n")
    a("---\n")
    a("## 10. 关联文档\n")
    a("| 文档 | 位置 |")
    a("|------|------|")
    a("| {填:文档名} | `{填:路径}` |")
    a(f"| 本报告 | `docs/analysis/ops-weekly-inspection/weekly-inspection-postmortem-{m.get('report_date', m['end'])}.md` |")

    return "\n".join(L) + "\n"


SAMPLE_SPEC = {
    "meta": {"start": "2026-06-20", "end": "2026-06-26", "image": "bar_smyze_opserver:1.0.30",
             "report_date": "2026-06-26", "severity": "P1", "status": "示例", "days": 7},
    "errors": [{"desc": "订单查询批量失败", "module": "OrderQuery"},
               {"desc": "字典映射缺失", "module": "DictCache"}],
    "warns": [{"desc": "慢SQL告警", "module": "OrderMapper"}],
    "trend": [{"name": "订单失败", "this_week": 120, "last_week": 80},
              {"name": "字典缺失", "this_week": 38, "last_week": 0},
              {"name": "慢SQL", "this_week": 15, "last_week": 22}],
}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("\n# === SAMPLE SPEC ===")
        print(json.dumps(SAMPLE_SPEC, ensure_ascii=False, indent=2))
        sys.exit(2)
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    md = gen(spec)
    if len(sys.argv) > 2:
        open(sys.argv[2], "w", encoding="utf-8").write(md)
        print(f">>> 骨架已生成：{sys.argv[2]}（{md.count(chr(10))} 行）")
    else:
        sys.stdout.write(md)
