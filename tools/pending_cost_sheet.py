#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待批项「批准 / 不批准」代价一页对比（R453 / 第 18 批 TODO 替换项 L169）。

问题
  待批台账 temp/pending_decisions/ 现役 100+ 条未决。决策者每次看到的是「一条条待批」，
  看不出**每条的成本与不做的代价**，于是持续积压。既有三件都没解决这件事：
    · R413 tools/pending_triage.py      —— 四桶**穷举全部**机器可判条目，不抽样、不排序、不给代价
    · R428 tools/attention_compressor.py —— 按**主题包**聚 137 条，给「一句话决策」，不逐条给代价
    · R433 tools/pending_evidence.py     —— **单条**的完整改动面证据卡（含 diff），面向执行，无抽样
  同样缺的是「抽几条、摆出代价、让人 30 秒内决定先看哪条」。本工具就是这一页。

本工具做什么（纯只读）
  1. 分层抽样 N 条（默认 5）。层序固定、层内按**积压天数降序**、同层最多取 1 条，
     同一行只进最早命中的层 ⇒ 5 条覆盖 5 种代价形态：
       S1 不可逆（revers=1）        —— 须逐条拍板，**永不**进「可批」
       S2 建议关闭（STALE）
       S3 需补证 / 重写（REWRITE）
       S4 可一句批准（can_one_shot = verdict ACTIVE ∧ revers=0 ∧ 有改动面 ∧ 非 manual）
       S5 需人工（无机器前提 / 可逆性未知）
  2. 逐条给三块：**批准态代价**（改动面 / 是否覆盖 / 可逆性）、**不批代价**、**复跑命令 + rc 语义**。
  3. 输出「一页代价对比」markdown；`--json` 落机器可读件（默认不写盘，只打 stdout）。
  4. `--check <sheet.json>` 复核器：把 revers=1 的项标成「可批」必须报错（负控见 --selftest）。
  5. `--readonly-proof` 只读自证：台账区 sha256 前后一致。

复用
  台账解析 / 前提判定 / 证据卡 **零重写**：直接调 tools/pending_evidence.py 的
  `load_reuse()` + `build_cards()`（后者返回 (cards, rows)：cards = actions.py 的 18 条声明，
  rows = 全量 149 行）。积压天数**直接复用 view_pending.parse_file 的 `_src_date`**（单一源，
  三级 fallback：「来源*」列的 `MM-DD` → 台账头部「生成时间」→ 文件 mtime）；复用件不可得时
  才本地近似，并在输出里显式标注「本地近似」。★首跑实测：自算 mtime 得 2 天而权威口径为 5 天
  （DECISIONS.md 生成时间 09-22）⇒**必须**同源，不许各算一套。

本工具**不**
  · 不改台账、不代批准、不代关闭、不写任何被扫目标文件
  · 不重算改动面（复用 R433），不做主题聚类（那是 R428），不穷举（那是 R413）
  · 唯一写盘 = `--json` / `--md` 指定路径

用法
  python3 tools/pending_cost_sheet.py                        # 5 条抽样，打一页
  python3 tools/pending_cost_sheet.py --sample 8 --json /tmp/sheet.json --md /tmp/sheet.md
  python3 tools/pending_cost_sheet.py --check /tmp/sheet.json # 复核器（rc=1 = 有违例）
  python3 tools/pending_cost_sheet.py --readonly-proof
  python3 tools/pending_cost_sheet.py --selftest
  python3 tools/pending_cost_sheet.py --ledger-dir <d> --root <r>   # 指向沙盒/副本
"""
import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_LEDGER = os.path.join(REPO, "temp", "pending_decisions")
SCHEMA = "ga.pending_cost_sheet/v1"

STRATA = [
    ("S1", "不可逆", "revers=1：一旦执行无法原样回退，必须逐条拍板"),
    ("S2", "建议关闭", "机器前提已失效（STALE）：留着只会稀释注意力"),
    ("S3", "需补证/重写", "机器前提与现状不符（REWRITE）：按原样批会批错东西"),
    ("S4", "可一句批准", "前提 ACTIVE ∧ 可逆 ∧ 改动面可算 ∧ 非 manual"),
    ("S5", "需人工", "无机器前提或可逆性未知：机器给不出代价，只登记"),
]

LABEL_OK = "可一句批准"
LABEL_IRREV = "不可逆·须逐条拍板"
LABEL_STALE = "建议关闭（须你确认）"
LABEL_REWRITE = "需补证/重写（勿按原样批）"
LABEL_MANUAL = "需人工判断（机器前提不可得）"
LABEL_UNKNOWN_REVERS = "可逆性未知（须先补 revers 再判）"

# 复核器口径（T4 实测踩坑后定，2026-09-27）：
#   ① CLAIM_MARKS：**动作完成声明**，扫**全字段** —— 出现在任何地方都表示本页越权代执行。
#   ② CMD_MARKS  ：**命令 flag**，只扫 repro[].cmd / repro[].rc（= 本工具**自撰**的命令区）。
#      理由：台账行原文里合法引用了 `--apply --approve`（如 R429 的回收须显式 --apply），
#      把它算违例 = 假警报（首跑 T4 即此因）；而命令区出现 --approve 才是真「教人代批」。
CLAIM_MARKS = ["已批准", "已关闭", "已执行", "已代批准", "已代关闭"]
CMD_MARKS = ["--approve", "--unapprove", "--apply"]
OK_LABELS = {LABEL_OK}


# ───────────────────────────────────────────── 复用既有件
def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_evidence(ledger_dir):
    """复用 R433 证据器（台账解析 + 前提判定 + 卡片）。"""
    cand = [os.path.join(HERE, "pending_evidence.py"),
            os.path.join(REPO, "tools", "pending_evidence.py")]
    for p in cand:
        if os.path.exists(p):
            return load_module(p, "pe_reuse")
    raise SystemExit("缺少复用件 tools/pending_evidence.py（找过：%s）" % " / ".join(cand))


# ───────────────────────────────────────────── 积压天数（与 view_pending 同源）
_VP_CACHE = {}


def load_view_pending(ledger_dir):
    """复用 temp/pending_decisions/view_pending.py 的 _src_date 口径（单一源）。

    返回 {(台账文件名, ID): (积压天数, 口径说明)}；取不到复用件时返回 {}，
    由 row_age_days 走本地近似并**显式标注**（口径不许悄悄漂移：首跑实测
    view_pending 报 5 天而我自算 2 天，根因=它的 fallback 链含「台账生成时间」）。
    """
    key = os.path.abspath(ledger_dir)
    if key in _VP_CACHE:
        return _VP_CACHE[key]
    idx = {}
    cand = [os.path.join(ledger_dir, "view_pending.py"),
            os.path.join(REPO, "temp", "pending_decisions", "view_pending.py")]
    vp_path = next((x for x in cand if os.path.exists(x)), None)
    vp = None
    if vp_path:
        try:
            vp = load_module(vp_path, "vp_reuse")
        except Exception:
            vp = None
    if vp is not None:
        today = datetime.date.today()
        names = [n for n in sorted(os.listdir(ledger_dir))
                 if n.startswith("DECISIONS") and n.endswith(".md")]
        failed = []
        for name in names:
            try:
                rows, gen = vp.parse_file(pathlib.Path(os.path.join(ledger_dir, name)))
            except Exception as e:  # noqa: BLE001 —— 但绝不静默：留证并告警
                failed.append("%s: %s: %s" % (name, type(e).__name__, e))
                continue
            for r in rows:
                d = r.get("_src_date")
                if d is None:
                    continue
                from_src = any(vp.DATE_IN_SRC.search(v or "")
                               for k, v in r.items() if str(k).startswith("来源"))
                who = "来源列" if from_src else ("台账生成时间" if gen else "台账 mtime")
                idx[(name, r.get("_id"))] = ((today - d).days,
                                            "%s %s" % (who, d.isoformat()))
        # ★首跑实测坑：漏 import pathlib 让 parse_file 抛 NameError，被 `except: continue`
        # 静默吃掉 ⇒ 索引恒空、积压全部回落「本地近似」（口径悄悄漂移且看着正常）。
        # 故此处硬告警：复用件在但一条都没建成，必须让人看见。
        if names and not idx:
            sys.stderr.write("[WARN] 台账积压口径复用失败：%s 个台账文件全部解析异常 ⇒ 回落本地近似\n"
                             % len(names))
            for f in failed[:5]:
                sys.stderr.write("        %s\n" % f)
    _VP_CACHE[key] = idx
    return idx


def row_age_days(row, today=None, year=None, vp_idx=None):
    """积压天数：优先 view_pending 口径（单一源）；缺则本地近似并标注。"""
    today = today or datetime.date.today()
    if vp_idx:
        hit = vp_idx.get((row.get("file"), row.get("id")))
        if hit:
            return hit
    year = year or today.year
    hdr = row.get("header") or []
    cells = row.get("cells") or []
    for k, v in zip(hdr, cells):
        if str(k).startswith("来源"):
            m = re.search(r"(\d{2})-(\d{2})", v or "")
            if m:
                try:
                    d = datetime.date(year, int(m.group(1)), int(m.group(2)))
                except ValueError:
                    break
                return (today - d).days, "来源列 %s-%s（本地近似）" % (m.group(1), m.group(2))
    p = row.get("path")
    if p and os.path.exists(p):
        d = datetime.date.fromtimestamp(os.path.getmtime(p))
        return (today - d).days, "台账 mtime %s（本地近似）" % d.isoformat()
    return None, ""


# ───────────────────────────────────────────── 抽样
def stratum_of(row, card):
    """返回该行命中的**最早**一层（分层抽样用）。"""
    if row.get("revers") == "1":
        return "S1"
    v = (card or {}).get("verdict")
    if v == "STALE":
        return "S2"
    if v == "REWRITE":
        return "S3"
    if card and card.get("can_one_shot"):
        return "S4"
    return "S5"


def label_of(row, card):
    if row.get("revers") == "1":
        return LABEL_IRREV
    if row.get("revers") == "?" or row.get("revers") is None:
        if not (card and card.get("can_one_shot")):
            return LABEL_UNKNOWN_REVERS
    v = (card or {}).get("verdict")
    if v == "STALE":
        return LABEL_STALE
    if v == "REWRITE":
        return LABEL_REWRITE
    if card and card.get("can_one_shot"):
        return LABEL_OK
    return LABEL_MANUAL


def repro_for(file_name, rid, row, card):
    cmds = [
        {"cmd": "python3 tools/pending_evidence.py --only %s --json /tmp/pe_%s.json" % (rid, rid),
         "rc": "0=证据卡已产出（含改动面前后行与 sha）；≠0=台账/声明解析异常（**不是**「不可批准」的信号）"},
        {"cmd": "python3 temp/pending_decisions/premise_recheck.py --json /tmp/pr.json",
         "rc": "0=前提复测跑完（结论看 JSON 的 verdict 字段：ACTIVE/REWRITE/STALE/NO_PREMISE）；2=参数/自测模式"},
        {"cmd": "python3 temp/pending_decisions/view_pending.py --id %s" % rid,
         "rc": "0=打印该条是否未决与积压天数；空输出=该 ID 不在台账（须先查 ID 拼写）"},
        {"cmd": "python3 tools/pending_cost_sheet.py --check /tmp/sheet.json",
         "rc": "0=本页自洽（无越权标签/无缺项）；1=有违例，逐条见 violations"},
    ]
    if row.get("revers") == "1":
        cmds.append({"cmd": "python3 tools/pending_evidence.py --only %s" % rid,
                     "rc": "0=只读预览；不可逆项的批准语必须由人逐条给，本页不代写"})
    seen, uniq = set(), []
    for c in cmds:
        if c["cmd"] in seen:
            continue
        seen.add(c["cmd"])
        uniq.append(c)
    return uniq


def build_entry(row, card, stratum, today=None, vp_idx=None):
    age, age_src = row_age_days(row, today=today, vp_idx=vp_idx)
    label = label_of(row, card)
    c = card or {}
    if c:
        cost_approve = ("改动面：%s → %s；变更 %s 行；sha %s → %s"
                        % (c.get("kind"), c.get("path"), c.get("changed_lines"),
                           c.get("sha_before") or "n/a",
                           c.get("sha_after") or "n/a（前提不成立，无改动面可算）"))
        # note 同时承载「前提判定理由」；只有 ACTIVE 时它才是「批了得到」，
        # STALE/REWRITE 时它是**拒绝理由**，放进收益行会读反（首跑实测）
        benefit = (c.get("note") or c.get("obs") or "") if c.get("verdict") == "ACTIVE" else ""
        risk = c.get("risk") or ""
        decline = c.get("no_approve_consequence") or ""
        verdict, detail = c.get("verdict"), c.get("detail")
    else:
        cost_approve = ("无 tools/pending_decisions 侧的机器声明（actions.py 未含该 ID）"
                        "⇒ 改动面无法机器估算，批准即等于人工执行")
        benefit = ""
        risk = _cell(row, ("风险", "说明", "备注"))
        decline = "台账未声明后果：%s" % (_cell(row, ("后果", "影响", "不批")) or "（该列不存在）")
        verdict, detail = None, None
    return {
        "stratum": stratum,
        "label": label,
        "file": row["file"],
        "id": row["id"],
        "line": row["line"],
        "title": (c.get("title") or _cell(row, ("项", "事项", "标题", "变更内容", "目标", "问题")) or "").strip(),
        "age_days": age,
        "age_src": age_src,
        "revers": row.get("revers"),
        "revers_why": row.get("revers_why"),
        "decided": row.get("decided"),
        "verdict": verdict,
        "verdict_detail": detail,
        "can_one_shot": bool(c.get("can_one_shot")),
        "cost_approve": cost_approve,
        "benefit": benefit,
        "cost_decline": decline,
        "risk": risk,
        "phrase": ("批准 %s:%s" % (row["file"], row["id"])) if c.get("can_one_shot") else "",
        "repro": repro_for(row["file"], row["id"], row, c),
    }


def _cell(row, keys):
    hdr = [str(h) for h in (row.get("header") or [])]
    cells = row.get("cells") or []
    for k in keys:
        for i, h in enumerate(hdr):
            if h == k or h.startswith(k):
                if i < len(cells):
                    return cells[i]
    return ""


def sample(rows, cards, n=5, today=None, vp_idx=None):
    amap = {(c["file"], c["id"]): c for c in cards}
    picked, used = [], set()
    for sid, sname, sdesc in STRATA:
        if len(picked) >= n:
            break
        cands = []
        for r in rows:
            if r["decided"]:
                continue
            if (r["file"], r["id"]) in used:
                continue
            c = amap.get((r["file"], r["id"]))
            if stratum_of(r, c) != sid:
                continue
            age, _ = row_age_days(r, today=today, vp_idx=vp_idx)
            cands.append((-1 if age is None else age, r["file"], r["id"], r, c))
        if not cands:
            continue
        cands.sort(key=lambda t: (-t[0], t[1], t[2]))
        age, _f, _i, r, c = cands[0]
        used.add((r["file"], r["id"]))
        picked.append(build_entry(r, c, sid, today=today, vp_idx=vp_idx))
    # 若某层空缺导致不足 n，用剩余最老的补足（同一行不重复）
    if len(picked) < n:
        rest = []
        for r in rows:
            if r["decided"] or (r["file"], r["id"]) in used:
                continue
            age, _ = row_age_days(r, today=today, vp_idx=vp_idx)
            rest.append((-1 if age is None else age, r["file"], r["id"], r))
        rest.sort(key=lambda t: (-t[0], t[1], t[2]))
        for age, f, i, r in rest:
            if len(picked) >= n:
                break
            used.add((f, i))
            picked.append(build_entry(r, amap.get((f, i)), "S*", today=today, vp_idx=vp_idx))
    return picked


def build_sheet(ledger_dir, root, n=5, today=None):
    pe = load_evidence(ledger_dir)
    pr, pr_path = pe.load_reuse(ledger_dir)
    cards, rows = pe.build_cards(ledger_dir, root, pr)
    vp_idx = load_view_pending(ledger_dir)
    entries = sample(rows, cards, n=n, today=today, vp_idx=vp_idx)
    filled = [e["stratum"] for e in entries]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ledger_dir": ledger_dir,
        "premise_recheck": pr_path,
        "sample_rule": ("层序 %s，层内按积压天数降序，同层最多 1 条，同一行只进最早命中的层"
                        % "→".join(s[0] for s in STRATA)),
        "count": len(entries),
        "stats": {
            "ledger_rows": len(rows),
            "machine_cards": len(cards),
            "undecided_rows": sum(1 for r in rows if not r["decided"]),
            "revers1_rows": sum(1 for r in rows if r.get("revers") == "1"),
            "strata_filled": sorted(set(filled)),
            "strata_total": [s[0] for s in STRATA],
        },
        "entries": entries,
    }


# ───────────────────────────────────────────── 渲染
def render_md(sheet):
    st = sheet["stats"]
    out = []
    out.append("# 待批项「批准 / 不批准」代价一页对比")
    out.append("")
    out.append("生成：%s ｜ 台账：`%s`" % (sheet["generated_at"], sheet["ledger_dir"]))
    out.append("抽样规则：%s（本次 %d 条）" % (sheet["sample_rule"], sheet["count"]))
    out.append("台账面：%d 行（未决 %d，revers=1 共 %d）｜其中机器可判（actions.py 声明）%d 条"
               % (st["ledger_rows"], st["undecided_rows"], st["revers1_rows"], st["machine_cards"]))
    out.append("")
    out.append("**本页只做代价对比，不含任何代批准、代关闭动作**；复核器 `--check` 会拦下"
               "「把不可逆项标成可批」与任何越权动作语。")
    out.append("")
    out.append("## 决策速览")
    out.append("")
    out.append("| # | 层 | 标签 | 条目 | 积压 | 可逆 | 批准语 |")
    out.append("|---|---|---|---|---|---|---|")
    for i, e in enumerate(sheet["entries"], 1):
        age = "%s 天" % e["age_days"] if e["age_days"] is not None else "?"
        rev = {"0": "可逆", "1": "**不可逆**", "?": "未知"}.get(str(e["revers"]), str(e["revers"]))
        out.append("| %d | %s | %s | `%s:%s` | %s | %s | %s |"
                   % (i, e["stratum"], e["label"], e["file"], e["id"], age, rev,
                      ("`%s`" % e["phrase"]) if e["phrase"] else "—（须人判）"))
    out.append("")
    out.append("## 逐条代价")
    for i, e in enumerate(sheet["entries"], 1):
        out.append("")
        out.append("### %d) [%s] `%s:%s`%s" % (i, e["label"], e["file"], e["id"],
                                            (" — " + e["title"]) if e["title"] else ""))
        out.append("- 台账位置：`%s:%d`｜积压：%s 天（%s）"
                   % (e["file"], e["line"],
                      e["age_days"] if e["age_days"] is not None else "?", e["age_src"] or "n/a"))
        out.append("- 前提判定：%s%s" % (e["verdict"] or "无机器判据",
                                       ("（%s）" % e["verdict_detail"]) if e["verdict_detail"] else ""))
        out.append("- **批准态代价**：%s" % e["cost_approve"])
        out.append("- 可逆性：revers=%s%s" % (e["revers"], ("（%s）" % e["revers_why"]) if e["revers_why"] else ""))
        if e["benefit"]:
            out.append("- 批了得到：%s" % e["benefit"])
        if e["risk"]:
            out.append("- 风险列：%s" % e["risk"])
        out.append("- **不批代价**：%s" % (e["cost_decline"] or "台账与声明都未给后果（信息缺口本身即待补项）"))
        out.append("- 复核命令：")
        for r in e["repro"]:
            out.append("  - `%s`" % r["cmd"])
            out.append("    - rc 语义：%s" % r["rc"])
        out.append("- 批准语：%s" % (("`%s`" % e["phrase"]) if e["phrase"]
                                    else "**本页不给**（%s）" % e["label"]))
    out.append("")
    out.append("## 与既有三件的边界")
    out.append("")
    out.append("| 工具 | 面 | 输出单位 | 是否抽样 | 是否给代价 |")
    out.append("|---|---|---|---|---|")
    out.append("| R413 `pending_triage.py` | 穷举全部机器可判条目 | 四桶（A可批/B建议关闭/C需补证/D需人） | 否 | 只给 A 桶的批准语 |")
    out.append("| R428 `attention_compressor.py` | 全部未决按主题聚类 | 10 个主题包 | 否 | 每包一句话决策 |")
    out.append("| R433 `pending_evidence.py` | 18 条声明逐条 | 单条证据卡（含 diff） | 否 | 给改动面，不给不批代价 |")
    out.append("| 本工具 R453 | **抽样 %d 条覆盖 5 种代价形态** | 一页对比 | **是** | **批准态 + 不批代价 + 复跑命令** |" % sheet["count"])
    out.append("")
    return "\n".join(out) + "\n"


# ───────────────────────────────────────────── 复核器
def check_sheet(sheet, ledger_dir=None, ledger_dir_check=True):
    v = []
    ents = sheet.get("entries") or []
    if sheet.get("count") != len(ents):
        v.append({"code": "COUNT_MISMATCH",
                  "detail": "count=%s 而 entries=%d" % (sheet.get("count"), len(ents))})
    known = None
    if ledger_dir and ledger_dir_check:
        try:
            pe = load_evidence(ledger_dir)
            rows = pe.parse_ledgers(ledger_dir)
            known = {(r["file"], r["id"]) for r in rows}
        except Exception as exc:      # noqa: BLE001
            v.append({"code": "LEDGER_UNREADABLE", "detail": str(exc)})
    seen = set()
    for e in ents:
        key = (e.get("file"), e.get("id"))
        if not e.get("file") or not e.get("id"):
            v.append({"code": "NO_SRC", "detail": "缺 file/id：%r" % (e.get("id"),)})
        if key in seen:
            v.append({"code": "DUP_ENTRY", "detail": "%s:%s 重复出现" % key})
        seen.add(key)
        if known is not None and key not in known:
            v.append({"code": "UNKNOWN_ID", "detail": "%s:%s 不在台账（ID 拼写/台账文件对不上）" % key})
        if e.get("label") in OK_LABELS:
            bad = []
            if str(e.get("revers")) != "0":
                bad.append("revers=%s" % e.get("revers"))
            if e.get("verdict") != "ACTIVE":
                bad.append("verdict=%s" % e.get("verdict"))
            if not e.get("can_one_shot"):
                bad.append("can_one_shot=false")
            if not e.get("phrase"):
                bad.append("缺批准语")
            if bad:
                v.append({"code": "REVERS_CONFLICT",
                          "detail": "%s:%s 标为「%s」但 %s"
                                    % (e.get("file"), e.get("id"), e.get("label"), "、".join(bad))})
        if e.get("revers") == "1" and e.get("phrase"):
            v.append({"code": "IRREVERSIBLE_PHRASE",
                      "detail": "%s:%s 是不可逆项却带批准语（本页不得代写）" % key})
        if not e.get("repro"):
            v.append({"code": "MISSING_REPRO", "detail": "%s:%s 无复跑命令" % key})
        else:
            for r in e["repro"]:
                if not r.get("cmd") or not r.get("rc"):
                    v.append({"code": "MISSING_REPRO",
                              "detail": "%s:%s 复跑命令缺 cmd 或 rc 语义" % key})
                    break
        if not e.get("cost_approve"):
            v.append({"code": "MISSING_COST", "detail": "%s:%s 缺批准态代价" % key})
        if not e.get("cost_decline"):
            v.append({"code": "MISSING_COST", "detail": "%s:%s 缺不批代价" % key})
        blob = json.dumps(e, ensure_ascii=False)
        for m in CLAIM_MARKS:
            if m in blob:
                v.append({"code": "WRITE_ACTION_LEAK",
                          "detail": "%s:%s 含动作声明「%s」（本页只做代价对比，不得代执行）"
                                    % (key[0], key[1], m)})
        cmdblob = " ".join([(r.get("cmd") or "") + " " + (r.get("rc") or "") for r in e.get("repro") or []])
        for m in CMD_MARKS:
            if m in cmdblob:
                v.append({"code": "WRITE_ACTION_LEAK",
                          "detail": "%s:%s 命令区出现写盘 flag「%s」（本页命令必须只读）"
                                    % (key[0], key[1], m)})
    return v


# ───────────────────────────────────────────── 只读自证
def tree_fingerprint(paths):
    h = {}
    for base in paths:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for f in sorted(files):
                p = os.path.join(dirpath, f)
                try:
                    with open(p, "rb") as fh:
                        h[p] = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    h[p] = "UNREADABLE"
    return h


# ───────────────────────────────────────────── CLI
def main():
    ap = argparse.ArgumentParser(description="待批项代价一页对比（只读）")
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGER)
    ap.add_argument("--root", default=REPO)
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--json", default="")
    ap.add_argument("--md", default="")
    ap.add_argument("--check", default="", help="复核一份已有 sheet.json（rc=1=有违例）")
    ap.add_argument("--readonly-proof", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if a.check:
        data = json.load(open(a.check, encoding="utf-8"))
        v = check_sheet(data, a.ledger_dir)
        print("复核 %s：entries=%d 违例=%d" % (a.check, len(data.get("entries") or []), len(v)))
        for x in v:
            print("  [%s] %s" % (x["code"], x["detail"]))
        if not v:
            print("  OK（无违例）")
        return 1 if v else 0

    before = tree_fingerprint([a.ledger_dir]) if a.readonly_proof else None
    sheet = build_sheet(a.ledger_dir, a.root, n=a.sample)
    md = render_md(sheet)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(sheet, f, ensure_ascii=False, indent=1)
        print("JSON 已写：%s" % a.json)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(md)
        print("MD 已写：%s" % a.md)
    print(md)
    if a.readonly_proof:
        after = tree_fingerprint([a.ledger_dir])
        changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
        print("只读自证：FILES=%d CHANGED=%d" % (len(before), len(changed)))
        for c in changed:
            print("  变动：%s" % c)
        if changed:
            return 1
    return 0


# ───────────────────────────────────────────── 自测（含负控）
def selftest():
    ok = 0
    fails = []

    def chk(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
            print("  PASS %s" % name)
        else:
            fails.append(name)
            print("  FAIL %s %s" % (name, extra))

    print("== 沙盒准备（拷贝台账副本，绝不动真树）==")
    src = DEFAULT_LEDGER
    if not os.path.isdir(src):
        print("  FAIL 真台账目录不存在：%s" % src)
        return 1
    tmp = tempfile.mkdtemp(prefix="pcs_selftest_")
    led = os.path.join(tmp, "ledger")
    shutil.copytree(src, led)
    sheet = build_sheet(led, REPO, n=5)
    print("  沙盒台账：%s（rows=%d cards=%d）"
          % (led, sheet["stats"]["ledger_rows"], sheet["stats"]["machine_cards"]))
    chk("T1 抽样条数 == 5", sheet["count"] == 5, "count=%s" % sheet["count"])
    chk("T2 抽样无重复项", len({(e["file"], e["id"]) for e in sheet["entries"]}) == sheet["count"])
    chk("T3 层序覆盖 S1..S5（允许空缺但不得乱序）",
        [s for s in sheet["stats"]["strata_filled"] if s != "S*"] ==
        sorted(set(sheet["stats"]["strata_filled"]) - {"S*"}))
    chk("T4 未修改页自洽（0 违例）", check_sheet(sheet, led) == [], str(check_sheet(sheet, led))[:200])
    chk("T5 确定性：两次构建同 ID 序列",
        [ (e["file"], e["id"]) for e in build_sheet(led, REPO, n=5)["entries"] ] ==
        [ (e["file"], e["id"]) for e in sheet["entries"] ])

    # 不变量：全量行里 revers=1 者一律不得标成「可一句批准」
    pe = load_evidence(led)
    pr, _ = pe.load_reuse(led)
    cards, rows = pe.build_cards(led, REPO, pr)
    amap = {(c["file"], c["id"]): c for c in cards}
    bad = [(r["file"], r["id"]) for r in rows if r.get("revers") == "1"
           and label_of(r, amap.get((r["file"], r["id"]))) == LABEL_OK]
    chk("T6 不变量：全量台账无「revers=1 却标可批」", bad == [], str(bad[:3]))

    print("== 负控（改坏副本必须被复核器抓住）==")
    def mut(entry_filter, f):
        s = copy.deepcopy(sheet)
        for e in s["entries"]:
            if entry_filter(e):
                f(e)
                return s
        s["entries"][0]  # 兜底
        f(s["entries"][0])
        return s

    # NC1（必测③）：把一条 revers=1 项标成「可批」
    s = copy.deepcopy(sheet)
    se = s["entries"][0]
    se["revers"] = "1"
    se["label"] = LABEL_OK
    se["phrase"] = "批准 %s:%s" % (se["file"], se["id"])
    v = check_sheet(s, led)
    chk("NC1 revers=1 却标「可批」→ REVERS_CONFLICT",
        any(x["code"] == "REVERS_CONFLICT" for x in v), str(v)[:200])
    chk("NC1b 同时报不可逆项带批准语 → IRREVERSIBLE_PHRASE",
        any(x["code"] == "IRREVERSIBLE_PHRASE" for x in v), str(v)[:200])

    # NC2：删掉复跑命令
    s = mut(lambda e: True, lambda e: e.__setitem__("repro", []))
    v = check_sheet(s, led)
    chk("NC2 缺复跑命令 → MISSING_REPRO",
        any(x["code"] == "MISSING_REPRO" for x in v), str(v)[:200])

    # NC3：注入越权动作语
    s = mut(lambda e: True, lambda e: e.__setitem__(
        "cost_decline", "已批准，无需处理"))
    v = check_sheet(s, led)
    chk("NC3 越权动作语 → WRITE_ACTION_LEAK",
        any(x["code"] == "WRITE_ACTION_LEAK" for x in v), str(v)[:200])

    # NC4：删一条 → 计数不符
    s = copy.deepcopy(sheet)
    s["entries"].pop()
    v = check_sheet(s, led)
    chk("NC4 删一条 → COUNT_MISMATCH",
        any(x["code"] == "COUNT_MISMATCH" for x in v), str(v)[:200])

    # NC5：缺不批代价
    s = mut(lambda e: True, lambda e: e.__setitem__("cost_decline", ""))
    v = check_sheet(s, led)
    chk("NC5 缺不批代价 → MISSING_COST",
        any(x["code"] == "MISSING_COST" for x in v), str(v)[:200])

    # NC6：ID 拼错 → UNKNOWN_ID
    s = mut(lambda e: True, lambda e: e.__setitem__("id", "ZZZ_NOPE"))
    v = check_sheet(s, led)
    chk("NC6 台账里没有的 ID → UNKNOWN_ID",
        any(x["code"] == "UNKNOWN_ID" for x in v), str(v)[:200])

    # NC7：rc 语义被抹掉
    s = mut(lambda e: True, lambda e: e.__setitem__(
        "repro", [{"cmd": "echo hi", "rc": ""}]))
    v = check_sheet(s, led)
    chk("NC7 复跑命令缺 rc 语义 → MISSING_REPRO",
        any(x["code"] == "MISSING_REPRO" for x in v), str(v)[:200])

    # NC8：命令区出现写盘 flag（真「教人代批」）必须报；而同字样出现在台账引文里不算
    s = mut(lambda e: True, lambda e: e.__setitem__(
        "repro", [{"cmd": "python3 tools/pending_evidence.py --approve DECISIONS.md:C1",
                   "rc": "0=已批准"}]))
    v = check_sheet(s, led)
    chk("NC8 命令区 --approve → WRITE_ACTION_LEAK",
        any(x["code"] == "WRITE_ACTION_LEAK" for x in v), str(v)[:200])
    s2 = mut(lambda e: True, lambda e: e.__setitem__(
        "cost_approve", "台账原文：须显式 --apply --approve 才回收"))
    chk("NC8b 台账引文里的 --apply 不算违例（避免假警报）",
        not any(x["code"] == "WRITE_ACTION_LEAK" for x in check_sheet(s2, led)))

    # 只读自证：整个 selftest 不得改动沙盒台账
    fp1 = tree_fingerprint([led])
    render_md(sheet)
    fp2 = tree_fingerprint([led])
    chk("T7 渲染不改台账（沙盒 sha256 一致）", fp1 == fp2)
    chk("T8 真台账仍未被写", tree_fingerprint([src]) == tree_fingerprint([src]))
    shutil.rmtree(tmp, ignore_errors=True)

    print("\nselftest: %d passed, %d failed" % (ok, len(fails)))
    if fails:
        print("失败项：%s" % ", ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
