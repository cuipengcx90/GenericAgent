#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""去 AI 味机械闸门（单一源）——论坛文案（发帖/回帖）在落盘或发送前必须过此关。

背景（2026-10-05 用户要求）：任何要发到 linux.do 的文字都必须「像人写的、
人想的」；LLM 生成物有稳定的机器指纹（分点、首先其次、综上所述、排比匀句、
刻意加粗、emoji 堆砌、教程腔），必须机械拦截，不能只靠提示词自律。

设计原则
  1. 只做**可机械判定**的特征，不做文风审美主观评判；
  2. 判据分两档：HARD（硬伤⇒直接拒）与 WARN（扣分不拒）；
  3. 误杀比漏放更贵（人写的被拦=白烧一次生成），故所有阈值都在负控里标定：
     人工写的口语文本必须全数通过；
  4. 单一源：linuxdo_acts 的回帖校验与 linuxdo_post_forge 的草稿闸门都 import 本模块，
     禁止各处复制一份判据。

CLI
  python3 tools/linuxdo_deai.py check --file <path|->   # 读文件或 stdin
  python3 tools/linuxdo_deai.py check --text "..."      # 直接给文本
  python3 tools/linuxdo_deai.py selftest                # 含负控（位置子命令）
  python3 tools/linuxdo_deai.py rules                   # 打印判据清单
退出码：0 = 通过（人味达标）；1 = 拒（有 HARD 或分数低于阈值）；2 = 用法/输入错。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------- 词表 / 常量
# 教科书/客服腔套话：出现即机器味（进 HARD）
TEMPLATE_PHRASES = [
    "综上所述", "总的来说", "总而言之", "总之", "首先", "其次", "最后一点",
    "值得注意的是", "值得一提", "在当今", "随着人工智能", "随着技术的",
    "希望对你有所帮助", "希望对你有帮助", "希望能帮到你", "欢迎讨论", "欢迎交流",
    "欢迎大家在评论区", "如有不对", "不吝赐教", "以上就是", "接下来我们",
    "我们可以看出", "不难看出", "由此可见", "深入探讨", "话不多说", "废话不多说",
    "本文", "综上所述，", "总结一下", "简单总结", "下面我将", "让我们",
    "赋能", "助力", "打造", "一站式", "干货满满", "干货", "分享给大家",
    # 水贴话术（回复侧也要拦，统一到此单一源）
    "感谢分享", "谢谢分享", "学到了", "学到很多", "学到了很多", "顶一下", "支持一下",
    "前排", "收藏了", "mark一下", "水一贴", "路过", "占楼", "沙发",
    "希望对大家有", "希望能给大家", "提升效率", "极大提升", "显著提升",
    # 机器味残留（短回帖里最常见的"客观陈述腔"，2026-10-06 校准补入）
    "需要注意的是", "需要注意", "值得注意", "三个方面", "综合考虑", "非常优秀",
]
# 书面连接词（密度过高 ⇒ 机器腔，进 WARN/累计扣分）
CONNECTIVES = ["因此", "然而", "此外", "同时", "而且", "不仅", "并且",
               "从而", "进而", "另一方面", "与此同时", "换言之", "除此之外"]
# 口语/人味标记：出现即加分（人类写论坛帖的常态）
COLLOQUIAL = ["卧槽", "我操", "草", "哈哈", "hhh", "emmm", "emm", "呃", "诶", "哎",
              "嘛", "呗", "咯", "啦", "咯", "吧", "反正", "其实吧", "讲真", "真的",
              "感觉", "估计", "大概", "顺手", "干脆", "结果", "翻了", "坑", "踩坑",
              "折腾", "白折腾", "搞了", "弄了", "试了", "一开始", "然后呢", "后来",
              "我这边", "老哥", "老铁", "兄弟", "大佬", "有兄弟", "有老哥", "我猜",
              "大概率", "居然", "竟然", "直接", "愣是", "硬是", "真香", "坑爹"]
# 具体性信号：数字/时间/型号/版本/路径等（人写帖常带具体细节）
CONCRETE_RX = re.compile(
    r"(\d+(\.\d+)?\s*(天|小时|分钟|秒|次|条|个|块|块钱|元|刀|美刀|G|M|K|GB|MB|KB|w|W|k|K))"
    r"|(\d{1,4}(\.\d+)?\s*(版|版本|ver|v\d))"
    r"|([0-9]{1,2}[:：][0-9]{2})"
    r"|([A-Za-z][A-Za-z0-9_.\-]{2,}\.(py|js|json|md|sh|log|toml|yaml|yml|db))"
    r"|(\bv?\d+\.\d+(\.\d+)?\b)"
    r"|(20\d{2}[年\-/]\d{1,2})"
    r"|([一二三四五六七八九十百千万两半]{1,3}(点|更|夜|天|号|小时|分钟|秒|次|条|个|块|遍|行|年|月|日|版|周))"
    r"|([A-Za-z][A-Za-z0-9_\-]{3,})")
EMOJI_RX = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
SENT_SPLIT_RX = re.compile(r"[。！？!?；;\n]+")


def _sentences(text: str):
    return [s.strip() for s in SENT_SPLIT_RX.split(text) if len(s.strip()) >= 2]


def _paragraphs(text: str):
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def _split_sents_keep(text: str):
    """按句末标点切句并保留长度信息（用于句长统计）。"""
    parts = re.split(r"(?<=[。！？!?；;])", text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 逗号也算小句，避免长句被当成句子把节奏判断带偏
        for q in re.split(r"[，,]", p):
            q = q.strip()
            if len(q) >= 2:
                out.append(q)
    return out


def lint(text: str, *, min_score: int = 60, strict: bool = False) -> dict:
    """核心闸门。返回 {ok, score, hard, warn, stats}。"""
    t = (text or "").strip()
    hard, warn, stats = [], [], {}
    if len(t) < 12:
        return {"ok": False, "score": 0, "hard": ["TOO_SHORT(%d)" % len(t)],
                "warn": [], "stats": {"chars": len(t)}}

    chars = len(t)
    stats["chars"] = chars

    # ---- HARD 1: markdown 结构痕迹
    if re.search(r"(?m)^\s{0,3}#{1,6}\s+\S", t):
        hard.append("MARKDOWN_HEADER")
    bullet_lines = len(re.findall(r"(?m)^\s{0,3}[-*•]\s+\S", t))
    num_lines = len(re.findall(r"(?m)^\s{0,3}\d{1,2}[.、)]\s*\S", t))
    if bullet_lines >= 2:
        hard.append("BULLET_LIST(%d)" % bullet_lines)
    if num_lines >= 2:
        hard.append("NUMBERED_LIST(%d)" % num_lines)
    if t.count("**") >= 2:
        hard.append("BOLD_MARKUP")
    if re.search(r"`[^`\n]{1,40}`", t) and chars < 400:
        warn.append("INLINE_CODE")
    if re.search(r"(?m)^\s*>\s", t):
        hard.append("QUOTE_BLOCK")

    # ---- HARD 2: 套话
    hits = [p for p in TEMPLATE_PHRASES if p in t]
    if hits:
        hard.append("TEMPLATE_PHRASE(%s)" % ",".join(hits[:4]))

    # ---- HARD 3: emoji 堆砌
    emo = EMOJI_RX.findall(t)
    stats["emoji"] = len(emo)
    if len(emo) >= 6 or (chars > 0 and len(emo) / max(chars, 1) > 0.03):
        hard.append("EMOJI_EXCESS(%d)" % len(emo))

    # ---- 句长节奏（排比匀句 = 机器指纹）
    sents = _split_sents_keep(t)
    stats["sent_n"] = len(sents)
    if len(sents) >= 8:
        lens = [len(s) for s in sents]
        mean = sum(lens) / len(lens)
        var = sum((x - mean) ** 2 for x in lens) / len(lens)
        cv = (var ** 0.5) / mean if mean else 0
        stats["sent_cv"] = round(cv, 3)
        if cv < 0.25:
            hard.append("SENTENCE_UNIFORM(cv=%.2f)" % cv)
        elif cv < 0.38:
            warn.append("SENTENCE_FLAT(cv=%.2f)" % cv)

    # ---- 段首重复（每段都「另外/同时/然后」开头）
    paras = _paragraphs(t)
    if len(paras) >= 3:
        heads = [p[:4] for p in paras]
        dup = max(heads.count(h) for h in set(heads))
        if dup >= 3:
            hard.append("PARA_HEAD_REPEAT(%s)" % heads[0])

    # ---- 连接词密度
    cn = sum(t.count(w) for w in CONNECTIVES)
    stats["connectives"] = cn
    if chars <= 600 and cn >= 4:
        hard.append("CONNECTIVE_DENSE(%d)" % cn)
    elif cn >= 4 and cn / (chars / 100.0) > 0.9:
        warn.append("CONNECTIVE_DENSE(%d)" % cn)

    # ---- 加分项
    coll = [w for w in COLLOQUIAL if w in t]
    stats["colloquial"] = len(coll)
    concrete = CONCRETE_RX.findall(t)
    stats["concrete"] = len(concrete)
    excl = t.count("！") + t.count("!")
    quest = t.count("？") + t.count("?")
    stats["excl"] = excl
    stats["quest"] = quest

    score = 56                                   # 基线（无任何人味信号的干巴段子应低于阈值）
    score += min(24, len(coll) * 8)                # 口语标记
    score += min(20, len(concrete) * 4)            # 具体细节
    score += 6 if len(sents) >= 4 else 0
    if len(sents) >= 6:                            # 句长有起伏
        score += min(10, int(stats.get("sent_cv", 0) * 25))
    score += 4 if (excl + quest) >= 1 else 0
    score += 4 if len(paras) >= 2 else 0
    score -= min(20, cn * 4)
    score -= min(10, len(emo) * 3)
    score -= 6 * len([w for w in warn if not w.startswith("CONNECTIVE")])
    score = max(0, min(100, score))
    stats["score_raw"] = score

    # 短文本（<120 字）拿不到"句子数/段落数/句长起伏"这些加分项，基线 56 会被阈值 60 冤杀
    # ⇒ 短帖按 min_score-15（下限 45）判定；HARD 判据一字不改，机器腔照拦。
    need = min_score if chars >= 120 else max(45, min_score - 15)
    stats["need"] = need
    ok = (not hard) and score >= need
    if strict and warn:
        ok = False
    return {"ok": ok, "score": score, "hard": hard, "warn": warn, "stats": stats}


def format_report(r: dict) -> str:
    lines = ["DEAI %s score=%d" % ("PASS" if r["ok"] else "REJECT", r["score"])]
    if r["hard"]:
        lines.append("  hard: " + " ".join(r["hard"]))
    if r["warn"]:
        lines.append("  warn: " + " ".join(r["warn"]))
    lines.append("  stats: " + json.dumps(r["stats"], ensure_ascii=False,
                                         sort_keys=True))
    return "\n".join(lines)


# ---------------------------------------------------------------- 负控语料
_AI_SAMPLE = """首先，我们需要明确一个核心问题。**自动化监控**的关键在于稳定性。
- 合理的频率控制
- 完善的错误处理
- 及时的通知机制
综上所述，希望对你有所帮助！这样的方案能极大提升效率。
"""
_AI_SAMPLE2 = ("""在当今快速发展的技术环境下，选择合适的方案至关重要。首先，我们要评估性能。"""
               """其次，要考虑维护成本。此外，还要关注扩展性。因此，团队需要综合权衡。"""
               """不仅要注意短期收益，而且要考虑长期演进。然而，风险同样不容忽视。"""
               """同时，我们必须保持谨慎。总之，理性决策才能带来长远价值。""")
_AI_SAMPLE3 = ("""部署很简单。配置很清晰。日志很完整。告警很及时。恢复很迅速。"""
               """流程很规范。文档很详尽。维护很方便。""")
_HUMAN_SAMPLE = """昨天折腾到凌晨两点，就为了一个破签名算法。
一开始以为是 cookie 问题，抓包看了下才发现是我把时间戳当秒算了，实际那边要毫秒。
改完就跑通了，顺手把重试也加了，不然网络抖一下就白干。
也不知道是不是我这边的代理有问题，反正折腾了小半天，最后发现是配置里多写了个空格。
"""
_HUMAN_SAMPLE2 = """这站最近的号池是真挤不进去，我连着试了三四天，基本全是 429。
刚才不死心又发了一次，居然过了，挂了两分钟才出第一个字，慢是真慢，但好歹能用。
有老哥知道这是按账号还是按 IP 限的吗？我这边换节点好像没啥区别。
"""


def selftest() -> int:
    fails, checks = [], 0

    def ck(cond, tag):
        nonlocal checks
        checks += 1
        if not cond:
            fails.append(tag)

    r1 = lint(_AI_SAMPLE)
    ck(not r1["ok"], "NC1 ai-sample1 must reject")
    ck(any(x.startswith("TEMPLATE_PHRASE") for x in r1["hard"]), "NC2 phrase detected")
    ck(any(x.startswith("BULLET_LIST") for x in r1["hard"]), "NC3 bullet detected")

    r2 = lint(_AI_SAMPLE2)
    ck(not r2["ok"], "NC4 ai-sample2 must reject")
    ck(any(x.startswith("CONNECTIVE") or x.startswith("TEMPLATE") for x in r2["hard"]),
       "NC5 connective/template hard")

    r3 = lint(_AI_SAMPLE3)
    ck(not r3["ok"], "NC6 uniform sentences must reject")
    ck(any(x.startswith("SENTENCE_UNIFORM") for x in r3["hard"]),
       "NC7 uniformity detected")

    h1 = lint(_HUMAN_SAMPLE)
    ck(h1["ok"], "NC8 human-sample1 must pass (%s)" % format_report(h1).replace("\n", " | "))
    h2 = lint(_HUMAN_SAMPLE2)
    ck(h2["ok"], "NC9 human-sample2 must pass (%s)" % format_report(h2).replace("\n", " | "))
    ck(h1["score"] > r1["score"], "NC10 human scores above ai")

    # 边界：空/过短 ⇒ 拒（不许把空文本当通过）
    ck(not lint("").get("ok"), "NC11 empty reject")
    ck(not lint("太短了").get("ok"), "NC12 short reject")
    # 单条 bullet 不误杀（人也会打一个短横）
    ck(lint("就一件事：昨天回帖被吞了。\n- 后来发现是长度超了，改短就过。\n我这边反正试了两次。")
       ["ok"], "NC13 single bullet tolerated")
    # markdown 标题必拒
    ck(not lint("## 我的经验\n昨天折腾了下代理，具体是这样：抓包看到 TLS reset，换了客户端就好了。")["ok"],
       "NC14 header reject")
    # 代码块内联码在短文里降级为 warn 不拒
    r14 = lint("试了下 `claude --session-id` 这个参数，还真能复用会话，我这边跑通了。")
    ck(r14["ok"] and "INLINE_CODE" in r14["warn"], "NC15 inline code warn only")
    # strict 模式：warn 即拒
    ck(not lint("试了下 `claude --session-id` 这个参数，还真能复用会话，我这边跑通了。",
                strict=True)["ok"], "NC16 strict rejects warn")
    # 阈值可传
    mid = "反正我是一边看日志一边改，改了三次才把超时调对，前两次都以为是网络问题。"
    ck(lint(mid, min_score=60)["ok"] and not lint(mid, min_score=90)["ok"],
       "NC17 min_score honored (%d)" % lint(mid)["score"])
    rl = lint(_HUMAN_SAMPLE)
    ck(rl["stats"]["concrete"] >= 1 and rl["stats"]["colloquial"] >= 1,
       "NC18 human stats signals present")
    # 真实历史回帖（R441 报告里的样本形态）不应被误杀
    ck(lint("这站号池确实挤，我这边凌晨试了几次也是 429，挂了半天才出一个字，"
            "估计是共享上游，等节后看看会不会好点。")["ok"], "NC19 real-reply-shaped pass")
    ck(not lint("感谢分享，学到了，顶一下！")["ok"], "NC20 water post also rejected")
    ck(lint("反正我是一边看日志一边改，改了三次才把超时调对，前两次都以为是网络问题。")["ok"],
       "NC21 casual long pass")

    print("selftest %d checks, %d failed" % (checks, len(fails)))
    for f in fails:
        print("  FAIL", f)
    return 0 if not fails else 1


def cmd_rules(_args):
    print("HARD(拒): MARKDOWN_HEADER BULLET_LIST NUMBERED_LIST BOLD_MARKUP QUOTE_BLOCK "
          "TEMPLATE_PHRASE EMOJI_EXCESS SENTENCE_UNIFORM PARA_HEAD_REPEAT CONNECTIVE_DENSE")
    print("WARN(扣分): INLINE_CODE SENTENCE_FLAT CONNECTIVE_DENSE")
    print("词表 template=%d connective=%d colloquial=%d"
          % (len(TEMPLATE_PHRASES), len(CONNECTIVES), len(COLLOQUIAL)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="linux.do 文案去 AI 味机械闸门")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="检查文本")
    c.add_argument("--file", default=None, help="文件路径，- 表示 stdin")
    c.add_argument("--text", default=None)
    c.add_argument("--min-score", type=int, default=60)
    c.add_argument("--strict", action="store_true")
    c.add_argument("--json", action="store_true")
    sub.add_parser("selftest")
    sub.add_parser("rules")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd != "check":
        ap.print_usage(sys.stderr)
        return 2

    if args.text is not None:
        txt = args.text
    elif args.file == "-":
        txt = sys.stdin.read()
    elif args.file:
        if not os.path.exists(args.file):
            print("file not found: %s" % args.file, file=sys.stderr)
            return 2
        with open(args.file, encoding="utf-8") as f:
            txt = f.read()
    else:
        print("need --text or --file", file=sys.stderr)
        return 2

    r = lint(txt, min_score=args.min_score, strict=args.strict)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, sort_keys=True))
    else:
        print(format_report(r))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
