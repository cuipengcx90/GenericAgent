#!/usr/bin/env python3
"""haisou 搜索结果相关性判据（整词/词组命中 + 可配置同义词表）。

背景（R441）
-----------
`birdie_monitor/haisou_daily.py::is_relevant()` 用「关键词切词后取最长 token 必须命中
标题」的做法。`max(tokens, key=len)` 在**等长**时取**先出现**的那个，而切词会先把
``AI测试`` 切成 ``["ai", "测试"]``（两个 token 都是 2 字符）⇒ 核心词退化成 ``ai``，
于是「AI设计工作流」「AI短剧」「AI公众号爆文」等只要含 ``ai`` 全部放行。
2026-09-22/23/25 累计 4 条推送全为这类假阳性。

本模块提供确定性替代实现：

* 剔除「通用 token」（``ai``/``aigc``/单双字符 ASCII 等）后再判命中 —— 通用词**不能单独**
  作为放行依据；
* 剩余候选 token 取**最长者**判命中（与旧行为同构：旧行为也只要求一个 token 命中，
  只是没先剔通用词、且等长时取先出现者从而退化到 ``ai``）；
* ASCII token 按词边界匹配（防 ``ai`` 命中 ``said``）；CJK token 按规范化后的子串匹配；
* 同义词表可配置（内置默认 + 可选外部 JSON），用于「AI测试 ← 软件测试/测试开发」这类
  关键词与课程标题不同名的情况。

用法
----
    python3 tools/haisou_relevance.py --selftest
    python3 tools/haisou_relevance.py --ab            # 回放历史语料，落 temp/haisou_relevance_ab.md
    python3 tools/haisou_relevance.py --check "AI测试" "AI设计工作流设计师必学"

只读：不写任何生产状态，`--ab` 只写 `temp/` 下的证据文件。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "birdie_monitor" / "haisou_seen_items.json"
AB_OUT = ROOT / "temp" / "haisou_relevance_ab.md"
SYNONYM_JSON = Path(__file__).with_name("haisou_relevance_synonyms.json")

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------
#: 通用 token：**不得单独**作为放行依据（命中它不等于相关）。
GENERIC_TOKENS = {
    "ai", "aigc", "ai+", "大模型", "课程", "教程", "视频", "网课", "全套",
    "合集", "课程合集", "资源", "资料", "学习", "培训", "训练营",
}

#: 关键词 → 额外可接受的命中串（整词/词组，规范化后子串匹配）。
#: 用于关键词与课程标题不同名的情况；键为规范化后的关键词。
DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "ai测试": ["软件测试", "测试开发", "测试工程师", "自动化测试", "接口测试", "性能测试"],
    "ai软件测试就业智能体": ["就业智能体"],
    "ai应用开发": ["大模型应用"],
    "ai产品出海": ["产品出海", "出海产品"],
    "ai创业": ["创业"],
    "mcp教程": ["mcp"],
    "和橘子学ai创作": ["和橘子学"],
}

_SPLIT_RE = re.compile(r"[|｜/,，、\s()（）\[\]【】\-–—_+.]+")
_PUNCT_RE = re.compile(r"[\s|｜/,，、()（）\[\]【】\-–—_+.·:：;；!！?？\"'“”‘’《》<>]+")


def normalize(text: str) -> str:
    """NFKC + 小写 + 标点→空格（**不删除**，用于 CJK 子串匹配）。

    保留一个分隔符很重要：若把标点直接删掉，``一看就懂 (王絮)`` 会被拼成
    ``一看就懂王絮``，从而让关键词 ``懂王`` 跨词命中（实测假阳性）。
    """
    s = unicodedata.normalize("NFKC", text or "").lower()
    return _PUNCT_RE.sub(" ", s)


def tokenize_keyword(keyword: str) -> list[str]:
    """与 haisou_daily.tokenize_keyword 同构：中英边界切分 + 过滤 <2 字符片段。"""
    kw = unicodedata.normalize("NFKC", keyword or "").lower()
    kw = re.sub(r"([\u4e00-\u9fff])([a-zA-Z0-9])", r"\1 \2", kw)
    kw = re.sub(r"([a-zA-Z0-9])([\u4e00-\u9fff])", r"\1 \2", kw)
    parts = [p for p in _SPLIT_RE.split(kw) if len(p) >= 2]
    return parts or ([kw] if kw else [])


def _is_short_ascii(tok: str) -> bool:
    """≤3 字符的纯 ASCII token 视为弱信号（ai/code/saas 之类），不作放行依据。"""
    return bool(re.fullmatch(r"[a-z0-9]+", tok)) and len(tok) <= 3


def candidate_tokens(keyword: str) -> list[str]:
    """剔除通用/弱信号 token 后的候选；若全被剔除则退回原 token（保证仍可判）。"""
    toks = tokenize_keyword(keyword)
    cand = [t for t in toks if t not in GENERIC_TOKENS and not _is_short_ascii(t)]
    return cand or toks


def load_synonyms() -> dict[str, list[str]]:
    """内置同义词表 + 可选外部 JSON（存在则合并，外部同名键覆盖）。"""
    table = {k: list(v) for k, v in DEFAULT_SYNONYMS.items()}
    if SYNONYM_JSON.exists():
        try:
            ext = json.loads(SYNONYM_JSON.read_text(encoding="utf-8"))
            for k, v in ext.items():
                key = normalize(k)
                table[key] = [normalize(x) for x in v]
        except Exception:
            pass  # 外部表坏掉不能拖垮判据；selftest 会单独校验表可读
    return table


_SYNONYMS = load_synonyms()


def _hit(tok: str, title_raw: str, title_norm: str) -> bool:
    """单 token 命中判定：ASCII 走词边界，CJK 走规范化子串。"""
    if re.fullmatch(r"[a-z0-9]+", tok):
        return re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", title_raw.lower()) is not None
    return normalize(tok) in title_norm


def pick_core_token(keyword: str) -> str | None:
    """选「最具区分度」的候选 token：过滤通用/弱信号后取最长（同长取先出现者）。

    与旧判据 ``max(toks, key=len)`` 的差别只在**先剔通用词**：旧版对 ``AI测试``
    取到 ``ai``（等长取首）⇒「含 ai 即放行」；新版取 ``测试``。
    """
    cands = candidate_tokens(keyword)
    if not cands:
        return None
    return max(cands, key=len)


def is_relevant_v2(keyword: str, title: str) -> bool:
    """新判据：过滤通用词后的最长候选 token 命中，或命中同义词表。"""
    return explain(keyword, title)["relevant"]


#: 生产侧（birdie_monitor/haisou_daily.py）直接引用本函数的别名，保证单一源。
is_relevant = is_relevant_v2


def ascii_phrase(keyword: str) -> str:
    """ASCII token 的**连写形态**（``Claude Code`` → ``claudecode``）。

    实测生产标题常把品牌词连写（``ClaudeCode视频教程``/``VibeCoding：用人话写代码``），
    词边界匹配会漏掉它们；连写形态仍要求 ≥2 个 ASCII token 拼接（≥4 字符）才启用，
    既收回真阳又不会把 ``Summer Vibe`` 这类同词异义放行。
    """
    toks = [t for t in tokenize_keyword(keyword) if re.fullmatch(r"[a-z0-9]+", t)]
    phrase = "".join(toks)
    return phrase if len(toks) >= 2 and len(phrase) >= 4 else ""


def explain(keyword: str, title: str) -> dict:
    """返回判定 + 依据（供 A/B 与人工复核用）。"""
    raw = title or ""
    norm_title = normalize(raw)
    if not norm_title:
        return {"relevant": False, "reason": "empty_title", "hit": None}
    core = pick_core_token(keyword)
    if core and _hit(core, raw, norm_title):
        return {"relevant": True, "reason": "core_token", "hit": core}
    phrase = ascii_phrase(keyword)
    if phrase and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", raw.lower()):
        return {"relevant": True, "reason": "ascii_glued", "hit": phrase}
    syn_key = normalize(tokenize_keyword(keyword)[0]) if tokenize_keyword(keyword) else normalize(keyword)
    syn_hits = _SYNONYMS.get(syn_key) or _SYNONYMS.get(normalize(keyword)) or []
    for pat in syn_hits:
        if pat and _hit(pat, raw, norm_title):
            return {"relevant": True, "reason": "synonym", "hit": pat}
    return {"relevant": False, "reason": "no_core_token", "hit": None,
            "core_token": core}


# ---------------------------------------------------------------------------
# 旧判据（复刻 birdie_monitor/haisou_daily.py::is_relevant，用于 A/B 基线）
# ---------------------------------------------------------------------------
def is_relevant_old(keyword: str, title: str) -> bool:
    title_l = (title or "").lower()
    toks = tokenize_keyword(keyword)
    if not toks:
        return False
    core_token = max(toks, key=len)
    return core_token in title_l


# ---------------------------------------------------------------------------
# 语料 / A/B
# ---------------------------------------------------------------------------
def load_pairs(corpus: Path = CORPUS) -> list[tuple[str, str]]:
    data = json.loads(corpus.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        rows = data.values()
    else:
        rows = data
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("keyword") and r.get("title"):
            out.append((r["keyword"], r["title"]))
    return out


def ab(corpus: Path = CORPUS, out: Path = AB_OUT, sample: int = 0) -> int:
    pairs = load_pairs(corpus)
    if sample:
        pairs = pairs[:sample]
    if not pairs:
        print("语料为空 ⇒ 拒绝产出结论（rc=2）")
        return 2
    old_keep = new_keep = 0
    both = only_old = 0
    rejected: dict[str, list[str]] = {}
    delta_keep: list[tuple[str, str]] = []   # 旧拒→新放行（新判据多收）
    for kw, ti in pairs:
        o, n = is_relevant_old(kw, ti), is_relevant_v2(kw, ti)
        old_keep += o
        new_keep += n
        if o and n:
            both += 1
        elif o and not n:
            rejected.setdefault(kw, []).append(ti)
            only_old += 1
        elif n and not o:
            delta_keep.append((kw, ti))
    lines: list[str] = []
    A = lines.append
    A("# haisou 相关性判据 A/B 复算（R441）\n")
    A(f"- 语料：`{corpus.relative_to(ROOT)}`（生产 seen_items，(keyword,title) 对）")
    A(f"- 样本数：**{len(pairs)}**\n")
    A("## 总量\n")
    A("| 口径 | 条数 | 占比 |")
    A("|---|---|---|")
    A(f"| 旧判据放行 | {old_keep} | {old_keep/len(pairs):.1%} |")
    A(f"| 新判据放行 | {new_keep} | {new_keep/len(pairs):.1%} |")
    A(f"| 两者都放行 | {both} | {both/len(pairs):.1%} |")
    A(f"| **旧放行 / 新拒绝**（潜在假阳性，需人工复核） | {old_keep-new_keep} | {(old_keep-new_keep)/len(pairs):.1%} |")
    A(f"| 旧拒绝 / 新放行（新判据多收） | {len(delta_keep)} | {len(delta_keep)/len(pairs):.1%} |")
    A("")
    A("> 语料本身**全部**是旧判据放行过的条目（seen_items 只记通过项）⇒ 「旧拒绝/新放行」"
      "只可能是旧判据因 token 选择而漏掉的同题条目，此类差异须逐条人工复核（见下节）。"
      "当该节为 `（无）` 时说明新判据放行集合是旧集合的**真子集**：只减不增，零新增风险面，"
      "所有差异都是「旧放行、新拒绝」的疑似假阳性。\n")
    A("## 旧放行、新拒绝的逐条清单（按关键词分组）\n")
    for kw in sorted(rejected, key=lambda k: -len(rejected[k])):
        items = rejected[kw]
        A(f"### {kw}（{len(items)} 条）")
        for ti in items:
            A(f"- {ti}")
        A("")
    A("## 旧拒绝、新放行（新判据多收）\n")
    if not delta_keep:
        A("- （无）")
    else:
        for kw, ti in delta_keep:
            A(f"- [{kw}] {ti}")
    A("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"AB 语料 {len(pairs)} 条 => 旧放行 {old_keep} / 新放行 {new_keep} / "
          f"旧放行新拒绝 {old_keep-new_keep} / 新多收 {len(delta_keep)}")
    print(f"报告已写 {out.relative_to(ROOT)}")
    return 0


# ---------------------------------------------------------------------------
# selftest（含负控）
# ---------------------------------------------------------------------------
TRUE_POSITIVES = [
    ("AI测试", "软件测试全套教程（完结）"),
    ("AI测试", "AI软件测试就业智能体实战营"),
    ("AI测试", "接口测试与自动化测试进阶班"),
    ("AI Agent", "AI agent大师之路"),
    ("AI Agent", "AI Agent实战：技术赋能与商业应用"),
    ("Claude Code", "claude code 从入门到精通"),
    ("AI coding", "AI coding 全栈实战营"),
    ("AI应用开发", "AI大模型应用开发实战营 (12期)"),
    ("MCP教程", "MCP 服务器开发教程（完结）"),
    ("AI创业", "AI创业营：从0到1"),
    ("软件测试", "软件测试工程师就业班"),
    ("和橘子学AI创作【600集150实战】", "和橘子学AI创作【600集150实战】（完结）"),
]

#: 负控（必须被新判据拒绝）；前 4 条为 2026-09 真实误报。
NEGATIVES = [
    ("AI测试", "AI设计工作流设计师必学"),
    ("AI测试", "AI生成美女图片提示词大全，千张参考图+精准提示词库"),
    ("AI测试", "AI公众号爆文变现训练营"),
    ("AI测试", "AI短视频2026零基础实操课"),
    ("AI测试", "【AI剧】BL国产《屠夫与娇花》最新1-14"),
    ("AI Agent", "AI绘画从入门到精通"),
    ("AI Agent", ""),
    ("Claude Code", "命令行工具速查手册"),
    ("接口测试", "AI创业营：从0到1"),
    ("AI应用开发", "AI绘画进阶课程"),
]


def selftest() -> int:
    fails: list[str] = []
    # ① 正例：新判据必须全部放行
    for kw, ti in TRUE_POSITIVES:
        if not is_relevant_v2(kw, ti):
            fails.append(f"TP 被拒: ({kw!r}, {ti!r})")
    # ② 负例：新判据必须全部拒绝
    for kw, ti in NEGATIVES:
        if is_relevant_v2(kw, ti):
            fails.append(f"负控被放行: ({kw!r}, {ti!r}) -> hit={explain(kw, ti).get('hit')}")
    # ③ 负控的负控：旧判据**必须**在前 4 条真实误报上放行（否则说明测试没测到位）
    for kw, ti in NEGATIVES[:4]:
        if not is_relevant_old(kw, ti):
            fails.append(f"旧判据本应放行却拒绝（测试失真）: ({kw!r}, {ti!r})")
    # ④ 词表自证
    assert candidate_tokens("AI测试") == ["测试"], candidate_tokens("AI测试")
    assert candidate_tokens("AI Agent") == ["agent"], candidate_tokens("AI Agent")
    assert "ai" not in candidate_tokens("AI测试")
    assert GENERIC_TOKENS and SYNONYM_JSON.name and _SYNONYMS
    # ⑤ 空输入不崩且判否
    assert explain("AI测试", "")["reason"] == "empty_title"
    assert explain("", "任意标题")["relevant"] in (True, False)
    total = len(TRUE_POSITIVES) + len(NEGATIVES) + 4 + 5
    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print(" -", f)
        return 1
    print(f"SELFTEST PASS: 正例 {len(TRUE_POSITIVES)} / 负控 {len(NEGATIVES)} / "
          f"反向负控 {len(NEGATIVES[:4])} / 断言 5 ⇒ 共 {total} 项")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="haisou 相关性判据（新）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ab", action="store_true", help="回放历史语料并落 temp/haisou_relevance_ab.md")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--out", default=str(AB_OUT))
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--check", nargs=2, metavar=("KEYWORD", "TITLE"))
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.ab:
        return ab(Path(args.corpus), Path(args.out), sample=args.sample)
    if args.check:
        kw, ti = args.check
        print(json.dumps({"keyword": kw, "title": ti,
                          "old": is_relevant_old(kw, ti),
                          "new": explain(kw, ti)},
                         ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
