#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待批项「证据三桶 + 一句批准语」降载包（R413 / 第 12 批 TODO C3′）。

要解决的问题
  台账 temp/pending_decisions/ 现役 123 条未决，人读一遍要翻 ~20 个文件、字段名各不相同，
  拍板成本高到实际没人在拍。本工具把「能机器判的」与「只能人看的」分开，并为每条给出
  一句话 + 可复制批准语 + 不批后果。

四桶（只有机器可判子集会被自动分桶，其余一律标「需人」，禁硬凑）
  A approve  证据齐备 → 可一句批准 : 前提复测 ACTIVE ∧ 消费者已声明 ∧ 证据已声明 ∧ kind≠manual
  B close    建议关闭（前提已消失）: ALREADY_DONE（目标已含目标内容）/ STALE（锚点0命中等）
  C needinfo 需补证/需重写         : REWRITE（歧义或目标被占）/ NO_PREMISE（manual）/ 缺消费者
  D human    需人（无机器可判前提）: 台账其余条目（本工具不替它们编结论）

机器可判子集 = temp/pending_decisions/actions.py 的 18 条声明（C1–C18，R403 同源）。

「谁消费、消费动作」来源（第 ⑤ 条验收）
  · 18 条 C 项由本文件 CONSUMERS 表**显式声明**（作者撰写，附证据指向；缺声明 ⇒ 落 C 桶）；
  · 其余条目台账本身没有该字段 ⇒ 工具打印「台账未声明消费者」而不猜（现状即 105 条 = 需人）。

批准语格式
  `批准 <台账文件>:<ID>`  —— 必须带文件名：ID 在台账间重名（D1×4 / D2×4 / D3×3 / D4×3），
  裸 ID 无法定位；本工具运行时断言批准语全局唯一，不唯一即报警。

只读保证
  本工具不写任何台账/目标文件。`--readonly-proof` 在一次完整运行前后对「读过的所有文件」
  取 sha1 对比，输出 FILES=<n> CHANGED=<n>（自测含负控：改文件后必须报 CHANGED>0）。

用法
  python3 tools/pending_triage.py                     # 完整四桶清单（Markdown）
  python3 tools/pending_triage.py --bucket approve    # 只看某桶
  python3 tools/pending_triage.py --json out.json     # 机器可读
  python3 tools/pending_triage.py --readonly-proof    # 只读自证（rc≠0 表示有文件被改动）
  python3 tools/pending_triage.py --selftest          # 沙盒负控自测（不碰真树）
  python3 tools/pending_triage.py --ledger-dir <d> --root <r>   # 指向沙盒/其它副本
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_LEDGER = os.path.join(REPO, "temp", "pending_decisions")

BUCKET_TITLES = {
    "approve": "A. 可一句批准（证据齐备）",
    "close": "B. 建议关闭（前提已消失）",
    "needinfo": "C. 需补证 / 需重写声明",
    "human": "D. 需人（无机器可判前提，本工具不替其编结论）",
}
BUCKET_ORDER = ["approve", "close", "needinfo", "human"]

# ── 字段拾取（台账各文件列名不统一，见 R413 报告 §2）────────────────────────
ONE_LINE_KEYS = ["改什么", "改什么（具体动作）", "变更内容", "项", "提案内容",
                 "具体", "方案", "目标", "目标文件/对象", "目标文件"]
RISK_KEYS = ["风险", "规模/风险", "代价", "证据/影响"]
EVID_KEYS = ["来源证据", "依据（实测）", "证据", "来源报告", "依据（本轮实测）", "证据/影响"]
REVERS_KEYS = ["回滚"]

# ── CONSUMERS：机器可判子集的「谁消费 / 消费动作 / 不批后果」声明 ──────────
# 依据 = 各条的 title + actions.py 的 evidence 指向的报告；不写无根据的推断。
CONSUMERS = {
    "C1": dict(
        who="ROI 日报判读者（按 roi_monitor_sop 跑日报的会话/人）",
        act="跑 `python3 roi_monitor.py --report`，读 avg_lines_per_task 与阈值告警判趋势",
        cons="`<100 行` 告警自 2026-06-26 起永不触发的口径缺陷继续存在，趋势判读无护栏"),
    "C2": dict(
        who="自主会话收尾（helper.complete_task 的调用方）",
        act="收尾归档时把报告同时写入 memory/ 树",
        cons="报告唯一副本仍可能只落 temp/（.gitignore 内），灾备缺口继续存在"),
    "C3": dict(
        who="定时/反射链路归档路径（reflect/scheduler.py 的调用方）",
        act="归档 L4 时顺带 sync 自主报告",
        cons="定时链路归档仍不覆盖自主报告，memory 侧继续靠人工补"),
    "C4": dict(
        who="后续自主会话收尾执行者（读 memory/autonomous_operation_sop.md 的 agent）",
        act="按 SOP 第 6 步执行报告入库命令",
        cons="SOP 仍不含入库步骤 ⇒ 后续会话按 SOP 收尾继续只留 temp 副本"),
    "C5": dict(
        who="用 procmem_scanner 排查的会话",
        act="按 SOP 判 macOS 跨进程读取限制，避免把「读不到」当「没在跑」",
        cons="该边界不进 SOP ⇒ 同一误判路径继续复发"),
    "C6": dict(
        who="检索 L2 的会话（读 memory/global_mem.txt）",
        act="按节名定位 2026-04 批次用户行为画像",
        cons="该批次画像在 L2 无节可检索"),
    "C7": dict(
        who="按标题检索/全量读 global_mem.txt 的会话与人",
        act="按标题定位章节（重复标题会命中错版）",
        cons="14 处重复标题继续存在 ⇒ 定位慢、易读到旧版内容"),
    "C8": dict(
        who="按 github_contribution_sop 跑贡献统计的会话",
        act="按 gh CLI 版本边界选择命令",
        cons="版本边界缺失 ⇒ 在新版 gh 下照 SOP 操作可能失败"),
    "C9": dict(
        who="发起子代理的会话",
        act="按 subagent.md 用 --func 调用并判后台是否完成",
        cons="用法不完整 ⇒ 继续踩「后台误判完成」"),
    "C10": dict(
        who="处理 bar-smyze 仓组的会话",
        act="按 L2 节区分仓组 / 工具链 / 注入层 / 度量基线",
        cons="该仓组知识仍散落在报告里，L2 检索不到"),
    "C11": dict(
        who="按 L1 判读 ROI 告警的会话",
        act="读 L1 该行取真因与 v2 口径指向",
        cons="L1 仍写着旧真因 ⇒ 判读被误导（R403 实测：修订锚点 0 命中，声明已失效）"),
    "C12": dict(
        who="检索 L1 的会话",
        act="按索引行找语音/音频能力入口",
        cons="语音能力在 L1 无索引行 ⇒ 检索路径缺失"),
    "C13": dict(
        who="按 L1 用进程内存工具（procmem_scanner / footprint 等）的会话",
        act="读该索引行知 macOS 边界",
        cons="边界不进 L1 ⇒ 只有翻 L2/报告才看得到"),
    "C14": dict(
        who="需要本地语音转写的会话",
        act="读 memory/asr_sop.md 作为操作规范",
        cons="（R403 实测该文件已存在）不批只是留一条过期声明，操作规范本身在盘"),
    "C15": dict(
        who="自主会话收尾 / 归档执行者",
        act="按该 L3 执行报告入库",
        cons="归档规范仍散落报告 ⇒ 新会话收尾无 SOP 可依"),
    "C16": dict(
        who="ROI 日报与 R337 口径的使用者",
        act="读 memory/roi_monitor_sop.md 作为口径说明",
        cons="（R403 实测该文件已存在）不批只是留一条过期声明，口径说明本身在盘"),
    "C17": dict(
        who="规划模式（下一批 TODO 生成）执行者",
        act="立项前按 SOP 做最小可行性探测 + 查候选重叠",
        cons="立项前不探测 ⇒ 继续产出「看似可做、实则不可行」的题"),
    "C18": dict(
        who="检索 L2 的会话（判断某能力/产线在不在本机）",
        act="按 L2 节读「本机能力边界与本地产线位置」",
        cons="能力边界仍在各报告里 ⇒ L2 检索不到"),
}


# ── 动态加载复用件（台账目录内，gitignored；缺失则给出明确指引）────────────
def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_helpers(ledger_dir):
    vp = os.path.join(ledger_dir, "view_pending.py")
    pr = os.path.join(ledger_dir, "premise_recheck.py")
    for p in (vp, pr):
        if not os.path.exists(p):
            raise SystemExit("缺少复用件: %s\n（台账目录需含 view_pending.py / premise_recheck.py / actions.py）" % p)
    return _load_module(vp, "vp_reuse"), _load_module(pr, "pr_reuse")


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def first_of(row, keys, limit=None):
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            return v[:limit] if limit else v
    return ""


def phrase_of(fname, rid):
    """可复制批准语：必须带台账文件名（ID 跨文件重名）。"""
    return "批准 %s:%s" % (fname, rid)


# ── 主逻辑 ────────────────────────────────────────────────────────────────
def build_entries(ledger_dir, root, actions=None, rows_override=None, consumers=None):
    """→ (entries, meta)。entries: 每条的桶/批准语/一句话/消费者/后果/证据。只读。

    consumers: 消费者声明表（默认本文件 CONSUMERS；自测可注入假表）。
    缺声明的条目一律落 needinfo —— 该门控是「谁消费」验收的第 ⑤ 条落点，不可绕过。
    """
    ctab = CONSUMERS if consumers is None else consumers
    vp, pr = load_helpers(ledger_dir)
    if actions is None:
        actions = pr.load_actions()
    if rows_override is None:
        import glob
        rows_override = []
        for f in sorted(glob.glob(os.path.join(ledger_dir, "DECISIONS*.md"))):
            import pathlib
            rs, _gen = vp.parse_file(pathlib.Path(f))
            for r in rs:
                r.setdefault("_decided", vp.is_decided(r.get("决策")))
            rows_override += rs
    rows = rows_override
    pending = [r for r in rows if not r.get("_decided")]

    # 机器可判子集：actions.py 的 id 且确实在未决行里
    verdicts = {}
    for a in actions:
        verdicts[a["id"]] = pr.check(a, root)
    amap = {a["id"]: a for a in actions}

    entries = []
    for r in pending:
        rid, fname = r["_id"], r["_file"]
        e = dict(bucket=None, file=fname, id=rid, phrase=phrase_of(fname, rid),
                 one_line=first_of(r, ONE_LINE_KEYS) or "(台账未写「改什么」)",
                 risk=first_of(r, RISK_KEYS, 120),
                 evidence=first_of(r, EVID_KEYS, 160),
                 revers=first_of(r, REVERS_KEYS, 120),
                 src_date=str(r.get("_src_date") or ""),
                 machine=rid in amap, verdict="", detail="",
                 who="", act="", cons="", why=[])
        if rid in amap:
            v = verdicts[rid]
            a = amap[rid]
            e["verdict"], e["detail"] = v["verdict"], v["detail"]
            e["target"] = a["path"]
            c = ctab.get(rid)
            if c:
                e["who"], e["act"], e["cons"] = c["who"], c["act"], c["cons"]
            has_ev = bool((a.get("evidence") or "").strip())
            if not has_ev:
                e["why"].append("actions.py 未给证据指向")
            if not c:
                e["why"].append("未声明消费者（CONSUMERS 缺此 id）")
            if v["verdict"] in ("ALREADY_DONE", "STALE"):
                e["bucket"] = "close"
            elif v["verdict"] == "ACTIVE" and c and has_ev:
                e["bucket"] = "approve"
            else:
                e["bucket"] = "needinfo"
                if v["verdict"] == "REWRITE":
                    e["why"].append("目标已存在/锚点歧义 ⇒ 需重写声明（若意图已落地可改判关闭）")
                if v["verdict"] == "NO_PREMISE":
                    e["why"].append("kind=manual，无机器可判前提")
        else:
            e["bucket"] = "human"
            e["who"] = "（台账未声明消费者 ⇒ 需人判断）"
            e["act"] = "—"
            # 台账没有「后果」列：只**转述**其风险列，并明说这不是后果（禁替其编）
            e["cons"] = ("维持现状（台账未声明后果，其风险列原文：%s）" % e["risk"]) if e["risk"] \
                else "维持现状（台账未声明后果，也未写风险列）"
            e["why"].append("非机器可判子集（不在 actions.py 18 条声明内）")
        entries.append(e)

    phrases = [e["phrase"] for e in entries]
    dup = sorted({p for p in phrases if phrases.count(p) > 1})
    meta = dict(ledger_dir=ledger_dir, root=root,
                files=len({e["file"] for e in entries}),
                total_pending=len(entries), machine=sum(1 for e in entries if e["machine"]),
                counts={b: sum(1 for e in entries if e["bucket"] == b) for b in BUCKET_ORDER},
                duplicate_phrases=dup,
                verdicts={k: verdicts[k]["verdict"] for k in verdicts})
    return entries, meta


def targets_of(ledger_dir, root, actions, rows):
    """一次运行会读到（因而必须保证只读）的文件集合。"""
    paths = set()
    import glob
    for f in glob.glob(os.path.join(ledger_dir, "DECISIONS*.md")):
        paths.add(os.path.abspath(f))
    for p in ("view_pending.py", "premise_recheck.py", "actions.py"):
        q = os.path.join(ledger_dir, p)
        if os.path.exists(q):
            paths.add(os.path.abspath(q))
    for a in actions:
        paths.add(os.path.join(os.path.abspath(root), a["path"]))
    return sorted(paths)


def snapshot(paths):
    out = {}
    for p in paths:
        try:
            out[p] = sha1(p)
        except OSError:
            out[p] = None
    return out


def run_triage(ledger_dir, root):
    vp, pr = load_helpers(ledger_dir)
    actions = pr.load_actions()
    import glob
    import pathlib
    rows = []
    for f in sorted(glob.glob(os.path.join(ledger_dir, "DECISIONS*.md"))):
        rs, _g = vp.parse_file(pathlib.Path(f))
        for r in rs:
            r.setdefault("_decided", vp.is_decided(r.get("决策")))
        rows += rs
    entries, meta = build_entries(ledger_dir, root, actions=actions, rows_override=rows)
    return entries, meta, actions, rows


# ── 渲染 ─────────────────────────────────────────────────────────────────
def render_md(entries, meta, buckets=None, human_full=False):
    out = []
    out.append("# 待批项降载包（证据三桶 + 一句批准语）")
    out.append("")
    out.append("- 数据源: `%s`（%d 个台账文件 / 未决 %d 条）"
               % (meta["ledger_dir"], meta["files"], meta["total_pending"]))
    out.append("- 机器可判子集: %d 条（= `actions.py` 声明）｜其余 %d 条归 D 桶「需人」"
               % (meta["machine"], meta["total_pending"] - meta["machine"]))
    out.append("- 桶计数: " + " | ".join("%s=%d" % (BUCKET_TITLES[b].split(".")[1].strip(), meta["counts"][b])
                                        for b in BUCKET_ORDER))
    out.append("- 批准语格式 `批准 <台账文件>:<ID>`（带文件名，因 ID 跨台账重名）｜"
               "唯一性: %s" % ("OK（无重复）" if not meta["duplicate_phrases"]
                              else "重复 " + ", ".join(meta["duplicate_phrases"])))
    out.append("- 只读: 本工具不改台账与目标文件（`--readonly-proof` 可复证）")
    out.append("- D 桶默认**紧凑一行**（批准语 + 一句话 + 台账风险原文）；要看 D 桶全文用 `--human-full`")
    out.append("- 后果栏口径: A/B/C 三条桶的「不批后果」由本工具 CONSUMERS 表声明；D 桶台账无后果列，"
               "只转述风险列原文，不编")
    out.append("")
    for b in BUCKET_ORDER:
        if buckets and b not in buckets:
            continue
        rows = [e for e in entries if e["bucket"] == b]
        out.append("## %s  —  %d 条" % (BUCKET_TITLES[b], len(rows)))
        out.append("")
        if not rows:
            out.append("（无）")
            out.append("")
            continue
        for e in sorted(rows, key=lambda x: (x["file"], x["id"])):
            if b == "human" and not human_full:
                # 紧凑一行：批准语 + 一句话 + 风险原文（台账未声明后果，故此处不编）
                out.append("- **%s** 　`%s` ｜ %s ｜ 风险: %s"
                           % (e["id"], e["phrase"], e["one_line"], e["risk"] or "（未写）"))
                continue
            out.append("- **%s** 　`%s`" % (e["id"], e["phrase"]))
            out.append("  - 一句话: %s" % e["one_line"])
            out.append("  - 谁消费: %s ｜ 消费动作: %s" % (e["who"], e["act"]))
            out.append("  - 不批后果: %s" % e["cons"])
            line = "  - 依据: %s" % (e["evidence"] or "（台账未写证据列）")
            if e["machine"]:
                line += " ｜ 复测: %s（%s）" % (e["verdict"], e["detail"])
            if e["why"]:
                line += " ｜ 存疑: " + "; ".join(e["why"])
            out.append(line)
            if e["revers"]:
                out.append("  - 回滚: %s" % e["revers"])
        out.append("")
    return "\n".join(out)


# ── 自测（沙盒 + 负控）────────────────────────────────────────────────────
SELFTEST_LEDGER = """# 沙盒台账（R413 selftest）
生成时间：2026-09-25

| ID | 目标文件 | 改什么 | 风险 | 回滚 | 决策 |
| --- | --- | --- | --- | --- | --- |
| T1 | `sb_target.py` | 沙盒-证据齐备（应判可批准） | low | 删块 | |
| T2 | `sb_done.txt` | 沙盒-已失效（应判建议关闭） | low | 删块 | |
| T3 | `sb_nc.txt` | 沙盒-无消费者声明（应判需补证） | low | 删块 | |
"""


def _fake_actions():
    return [
        dict(id="T1", kind="append", path="sb_target.py", content="MARKER-APPENDED",
             risk="low", evidence="沙盒证据", title="沙盒-证据齐备"),
        dict(id="T2", kind="append", path="sb_done.txt", content="MARKER-ALREADY",
             risk="low", evidence="沙盒证据", title="沙盒-已失效"),
        dict(id="T3", kind="append", path="sb_nc.txt", content="MARKER-APPENDED",
             risk="low", evidence="沙盒证据", title="沙盒-无消费者声明"),
    ]


def selftest():
    ok = tot = 0

    def expect(label, cond):
        nonlocal ok, tot
        tot += 1
        print("[%s] %s" % ("PASS" if cond else "FAIL", label))
        ok += 1 if cond else 0

    sb = tempfile.mkdtemp(prefix="r413_selftest_")
    try:
        ledger = os.path.join(sb, "temp", "pending_decisions")
        os.makedirs(ledger)
        root = sb
        # 复用件：真件拷入沙盒（保证 load_helpers 走沙盒路径，不碰真树）
        for p in ("view_pending.py", "premise_recheck.py", "actions.py"):
            shutil.copy(os.path.join(DEFAULT_LEDGER, p), os.path.join(ledger, p))
        with open(os.path.join(ledger, "DECISIONS_T.md"), "w", encoding="utf-8") as f:
            f.write(SELFTEST_LEDGER)
        with open(os.path.join(root, "sb_target.py"), "w", encoding="utf-8") as f:
            f.write("# sandbox\n")
        with open(os.path.join(root, "sb_nc.txt"), "w", encoding="utf-8") as f:
            f.write("# sandbox nc\n")
        with open(os.path.join(root, "sb_done.txt"), "w", encoding="utf-8") as f:
            f.write("MARKER-ALREADY\n")           # 已含追加内容 ⇒ ALREADY_DONE

        actions = _fake_actions()
        # 假消费者表：T1/T2 有声明，T3 **故意不给**（负控 2 的靶子）
        fake_consumers = {
            "T1": dict(who="沙盒消费者甲", act="读 sb_target.py 的标记块", cons="沙盒后果甲"),
            "T2": dict(who="沙盒消费者乙", act="读 sb_done.txt 的标记块", cons="沙盒后果乙"),
        }
        rows = _load_module(os.path.join(ledger, "view_pending.py"), "vp_st").parse_file(
            __import__("pathlib").Path(os.path.join(ledger, "DECISIONS_T.md")))[0]
        for r in rows:
            r["_decided"] = False
        # 夹具补一条**非机器可判**行（T9 不在假 actions 里）⇒覆盖 D 桶与 --human-full
        rows.append({"_id": "T9", "_file": "DECISIONS_T.md", "_src_date": "2026-09-25",
                     "改什么": "沙盒占位（无机器可判前提）", "风险": "低", "_decided": False})
        entries, meta = build_entries(ledger, root, actions=actions, rows_override=rows,
                                      consumers=fake_consumers)
        by = {e["id"]: e for e in entries}

        # 正控：T1 → approve
        expect("正控 T1 证据齐备 ⇒ approve（实际 %s）" % by["T1"]["bucket"], by["T1"]["bucket"] == "approve")
        # 负控 1：T2 已完成（ALREADY_DONE）绝不可入 approve，须 close
        expect("负控1 T2 已完成 ⇒ close 且非 approve（verdict=%s bucket=%s）"
               % (by["T2"]["verdict"], by["T2"]["bucket"]),
               by["T2"]["verdict"] == "ALREADY_DONE" and by["T2"]["bucket"] == "close")
        # 负控 2：T3 无消费者声明 ⇒ 不得入 approve，须 needinfo
        expect("负控2 T3 未声明消费者 ⇒ needinfo 且非 approve（bucket=%s）" % by["T3"]["bucket"],
               by["T3"]["bucket"] == "needinfo")
        # 负控 3：把已完成文件改成未含内容 ⇒ T2 必须翻成 approve（证明判据真的读了文件）
        with open(os.path.join(root, "sb_done.txt"), "w", encoding="utf-8") as f:
            f.write("# cleared\n")
        e2, _m2 = build_entries(ledger, root, actions=actions, rows_override=rows,
                                consumers=fake_consumers)
        b2 = {e["id"]: e["bucket"] for e in e2}
        expect("负控3 清空 sb_done.txt 后 T2 翻为 approve（证明判据读文件）: %s" % b2["T2"],
               b2["T2"] == "approve")
        # 批准语唯一性 + 带文件名
        expect("批准语带台账文件名: %s" % by["T1"]["phrase"],
               by["T1"]["phrase"] == "批准 DECISIONS_T.md:T1")
        expect("批准语全局唯一（meta.duplicate_phrases 空）", meta["duplicate_phrases"] == [])
        # 只读自证：正跑 vs 改动后必须报 CHANGED
        paths = targets_of(ledger, root, actions, rows)
        s1 = snapshot(paths)
        run_ok = all(snapshot(paths)[p] == s1[p] for p in s1)          # 未改 ⇒ 应一致
        expect("只读自证正控：无改动时快照一致", run_ok)
        with open(os.path.join(root, "sb_target.py"), "a", encoding="utf-8") as f:
            f.write("# touched\n")
        s2 = snapshot(paths)
        expect("只读自证负控：改动 sb_target.py 后必须检出 CHANGED",
               s2[os.path.join(root, "sb_target.py")] != s1[os.path.join(root, "sb_target.py")])
        # 渲染冒烟
        md = render_md(entries, meta)
        expect("Markdown 渲染含批准语与四桶标题",
               "批准 DECISIONS_T.md:T1" in md and all(t in md for t in BUCKET_TITLES.values()))
        # 负控：--human-full 必须是活 flag（防"加了参数没接线"的死 flag，实测踩到过）
        md_full = render_md(entries, meta, human_full=True)
        expect("--human-full 真展开 D 桶（行数严格更多）",
               md_full.count("\n") > md.count("\n"))
        print("\n自测: %d/%d PASS" % (ok, tot))
        return 0 if ok == tot else 1
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="待批项证据三桶 + 一句批准语降载包（只读）")
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGER)
    ap.add_argument("--root", default=None, help="目标文件解析根（默认 ledger 上两级）")
    ap.add_argument("--bucket", default=None, choices=BUCKET_ORDER)
    ap.add_argument("--json", default=None, help="另存机器可读 JSON")
    ap.add_argument("--readonly-proof", action="store_true", help="跑一次并自证只读")
    ap.add_argument("--human-full", action="store_true", help="D 桶展开为多条明细（默认紧凑一行）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    ledger = os.path.abspath(a.ledger_dir)
    root = os.path.abspath(a.root) if a.root else os.path.dirname(os.path.dirname(ledger))
    if not os.path.isdir(ledger):
        print("台账目录不存在: %s" % ledger)
        return 2

    entries, meta, actions, rows = run_triage(ledger, root)

    if a.readonly_proof:
        paths = [p for p in targets_of(ledger, root, actions, rows) if os.path.exists(p)]
        s1 = snapshot(paths)
        run_triage(ledger, root)
        s2 = snapshot(paths)
        changed = [p for p in s1 if s1[p] != s2[p]]
        print("只读自证: FILES=%d CHANGED=%d" % (len(s1), len(changed)))
        for p in changed:
            print("  CHANGED: %s" % p)
        return 0 if not changed else 1

    md = render_md(entries, meta, buckets=[a.bucket] if a.bucket else None,
                   human_full=a.human_full)
    print(md)

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(dict(schema="ga.pending_triage/v1",
                           generated=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           ledger_dir=ledger, root=root,
                           counts=meta["counts"], total_pending=meta["total_pending"],
                           machine=meta["machine"], duplicate_phrases=meta["duplicate_phrases"],
                           entries=entries), f, ensure_ascii=False, indent=1)
        print("\n[json] %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
