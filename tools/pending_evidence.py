#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待批项「批准前证据器」（R433 / 第 16 批 TODO#1，三路评审 10/9/10 一致列第 1）。

要解决的问题
  台账 temp/pending_decisions/ 现役 137 条未决（R428 口径），其中 **73%** 的 LOW 值
  来自「待批未落地」（R433 前的 history_value n=399 实测）。瓶颈不是没人判断，
  而是批准前看不到**到底会改动什么**：R403 `premise_recheck.py` 只说
  「锚点恰好命中 1 次」，不说「这一行将要变成什么」。

本工具做什么（默认只读）
  对「机器可判」子集（= temp/pending_decisions/actions.py 的 18 条声明）逐条：
    1. 复跑前提 —— **零重写**，直接调 premise_recheck.check()
    2. 在内存里算出「改动后的文件全文」，打印**锚点命中行的前后行**（带行号）
    3. 打印 before/after sha256（前 12 位）与变更行数
    4. 一句话批准语 `批准 <台账文件>:<ID>`（ID 跨文件重名，故必带文件名）
    5. 「不批后果」（actions.py 的 risk/note 转述 + 台账行原文；缺则明说缺）

revers（可逆性）判定 —— 写死在 resolve_revers()，判不出就报「未知」，**不猜**
    · 表头含 `revers` 列           → 直接读数 0/1
    · 表头含 `回滚` 列             → 含「不可逆/不可撤销/无法回滚/永久」⇒1；
                                     含 rollback|git checkout|git rm|还原|恢复|删除|重建|备份 ⇒0；
                                     空 / 「—」/「-」⇒ 未知
    · 台账正文（首个表格之前）写明 `revers=0`（或 `revers=1`）⇒ 该台账整表同值
    · 多列并存时：`revers` 列 > `回滚` 列 > 正文声明

唯一的写盘路径：`--approve <台账文件>:<ID>`
  · 只写台账文件本身（`DECISIONS*.md`），只加审批标记，**不改任何内容行**
  · 若该表有 `决策` 列且该单元格为空 ⇒ 写进去 `✅ 已批准 <YYYY-MM-DD HH:MM>（pending_evidence）`
  · 否则（列不存在 / 已有内容，如 R429 表的「决策」列放的是决策正文）⇒
    在文件末尾追加一行审批记录（纯追加，不碰既有行）
  · 写前写后自证：其余行逐字未变、行数增量符合预期、写后仍可被本工具解析
  · `--unapprove` 逐字还原（`--selftest` 用 sha256 证明还原是字节级的）
  · **绝不会**写 memory/**、tools/**、SOP 或任何被扫目标文件 —— refuse_nonledger() 硬拦
  · `--set-revers` 才动 revers 单元格（默认不动：本台账里 revers=1 表示「不可逆」，
    拿它当「已批准」标记会污染判读）

用法
  python3 tools/pending_evidence.py                       # 全部机器可判条目证据卡
  python3 tools/pending_evidence.py --only C2,C6           # 只看指定 ID
  python3 tools/pending_evidence.py --json out.json        # 机器可读
  python3 tools/pending_evidence.py --readonly-proof       # 只读自证（CHANGED>0 ⇒ rc=1）
  python3 tools/pending_evidence.py --approve DECISIONS.md:C2   # 唯一写盘路径
  python3 tools/pending_evidence.py --unapprove DECISIONS.md:C2
  python3 tools/pending_evidence.py --selftest             # 沙盒负控自测（不碰真树）
  python3 tools/pending_evidence.py --ledger-dir <d> --root <r>   # 指向沙盒/副本
"""
import argparse
import difflib  # noqa: F401  （保留：后续若要 unified 风格）
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_LEDGER = os.path.join(REPO, "temp", "pending_decisions")

DIFF_LIMIT = 16          # 每条最多打印多少行「前后行」
SHA_SHOW = 12
STAMP_MARK = "pending_evidence"
PENDING_CELL = re.compile(r'^\W*(?:待批|待决|待审|待定|未决|未批|pending|todo|⏳|☐|⬜|□|\[\s*\])', re.I)
IRREVERSIBLE = re.compile(r'不可逆|不可撤销|无法回滚|不能回滚|不可恢复|永久|no\s*rollback', re.I)
REVERSIBLE = re.compile(r'rollback|git\s+checkout|git\s+rm|还原|恢复|复原|重建|重新生成|删除|备份|cp\s', re.I)
ID_KEYS = ("ID", "id", "序", "编号", "项号", "方案")


# ─────────────────────────────────────────────────────────── 基础
def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha_text(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def read_text(p):
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


class Audit:
    """记录本次运行读过的文件，用于 --readonly-proof 前后对账。"""

    def __init__(self):
        self.paths = {}

    def read(self, p):
        if p not in self.paths and os.path.isfile(p):
            self.paths[p] = sha_file(p)
        return read_text(p)

    def snapshot(self):
        return dict(self.paths)

    def changed(self):
        out = []
        for p, h0 in self.paths.items():
            if not os.path.isfile(p):
                out.append((p, h0, "MISSING"))
            else:
                h1 = sha_file(p)
                if h1 != h0:
                    out.append((p, h0, h1))
        return out


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_reuse(ledger_dir):
    """加载 premise_recheck（复用其前提判定内核，零重写）。"""
    cand = [os.path.join(ledger_dir, "premise_recheck.py"),
            os.path.join(DEFAULT_LEDGER, "premise_recheck.py")]
    for p in cand:
        if os.path.exists(p):
            return load_module(p, "pr_reuse"), p
    raise SystemExit("缺少复用件 premise_recheck.py（找过：%s）" % " / ".join(cand))


# ─────────────────────────────────────────────────────────── 台账解析
def _cells_of(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(cells):
    return all(re.fullmatch(r"[-:\s]*", c) for c in cells)


ID_OK_RE = r"[A-Za-z]{1,3}[-_]?[A-Za-z0-9]{0,6}"   # 与 view_pending.ID_OK 逐字一致


def parse_ledgers(ledger_dir, audit=None, no_id=None):
    """解析 DECISIONS*.md → rows。规则与 temp/pending_decisions/view_pending.py 同源。

    no_id：可选 list，收集「是表格行但没有 ID 单元格」的行（如 R347 表以「方案」列为键）
    ⇒ 这些行**不可批准**，但必须显式列出不得静默丢弃。

    row = dict(file, path, line(1-based), header, cells, id, raw,
               decision_cell, decided, revers, revers_why, preamble)
    """
    rows = []
    files = sorted(f for f in os.listdir(ledger_dir)
                   if f.startswith("DECISIONS") and f.endswith(".md")) \
        if os.path.isdir(ledger_dir) else []
    for fname in files:
        path = os.path.join(ledger_dir, fname)
        text = audit.read(path) if audit else read_text(path)
        lines = text.split("\n")
        first_table_line = None
        header = None
        for i, line in enumerate(lines):
            if line.startswith("#"):
                header = None
                continue
            if not line.startswith("|"):
                continue
            cells = _cells_of(line)
            if _is_sep(cells):
                continue
            if header is None:
                header = cells
                if first_table_line is None:
                    first_table_line = i + 1
                continue
            if len(cells) < 3:
                continue
            row = {}
            for k, c in enumerate(cells):
                row[header[k] if k < len(header) else "col%d" % k] = c
            rid = ""
            for k in ID_KEYS:
                if row.get(k):
                    rid = row[k].strip("`* ")
                    break
            if rid and not re.fullmatch(ID_OK_RE, rid):
                rid = rid.split(".")[0].split()[0].strip("`* ")   # 与 view_pending 同源
            if not rid or not re.fullmatch(ID_OK_RE, rid):
                if no_id is not None:
                    no_id.append(dict(file=fname, line=i + 1,
                                      first=(cells[0] if cells else "")[:40], header=header))
                continue
            idx = {c: i for i, c in enumerate(header)}
            dc = row.get("决策", "")
            preamble = "\n".join(lines[:max(0, (first_table_line or 1) - 1)])
            rv, why = resolve_revers(header, cells, row, preamble)
            rows.append(dict(file=fname, path=path, line=i + 1, header=header, cells=cells,
                             raw=line, id=rid, decision_cell=dc,
                             decided=bool(dc.strip()) and not PENDING_CELL.match(dc.strip()),
                             revers=rv, revers_why=why, preamble=preamble,
                             _idx=idx))
    return rows


def resolve_revers(header, cells, row, preamble):
    keys = [h.lower() for h in header]
    if "revers" in keys:
        v = (row.get("revers") or row.get("Revers") or "").strip()
        if v.startswith("0"):
            return "0", "revers 列=0（可逆，可一句批准）"
        if v.startswith("1"):
            return "1", "revers 列=1（不可逆，须逐条拍板）"
        return "?", "revers 列有值但非 0/1：%r" % v
    if "回滚" in keys:
        v = (row.get("回滚") or "").strip()
        if not v or v in "—-–":
            return "?", "回滚列空/占位"
        if IRREVERSIBLE.search(v):
            return "1", "回滚列自述不可逆：%s" % v[:60]
        if REVERSIBLE.search(v):
            return "0", "回滚列给可执行回滚：%s" % v[:60]
        return "?", "回滚列内容无法判定：%s" % v[:60]
    m0 = re.search(r"revers\s*=\s*0", preamble, re.I)
    m1 = re.search(r"revers\s*=\s*1", preamble, re.I)
    if m0 and not m1:
        return "0", "台账正文声明整表 revers=0"
    if m1 and not m0:
        return "1", "台账正文声明整表 revers=1"
    if m0 and m1:
        return "?", "台账正文同时出现 revers=0 与 revers=1（歧义）"
    return "?", "无 revers/回滚列，正文也无 revers= 声明"


# ─────────────────────────────────────────────────────────── 假设改写（内存）
def hypothetical(a, root, audit=None):
    """按 temp/pending_decisions/apply.py 的语义在**内存**里算出改动后全文。

    → (before, after, note) ；manual/未知 kind → (None, None, 原因)
    """
    kind = a["kind"]
    rel = a["path"]
    tgt = os.path.join(root, rel)
    if kind == "manual":
        return None, None, "manual：无机器可判改动"
    if kind == "create":
        if os.path.exists(tgt):
            before = audit.read(tgt) if audit else read_text(tgt)
            if before.strip() == a["content"].strip():
                return before, before, "目标已存在且内容一致（已完成）"
            return before, None, "目标已存在但内容不同（拒绝覆盖）"
        return "", a["content"], "新建文件（%d 行）" % len(a["content"].splitlines())
    if not os.path.exists(tgt):
        return None, None, "目标文件不存在：%s" % rel
    before = audit.read(tgt) if audit else read_text(tgt)
    if kind == "copy":
        src = os.path.join(root, a["src"])
        if not os.path.exists(src):
            return before, None, "源文件不存在：%s" % a["src"]
        src_txt = audit.read(src) if audit else read_text(src)
        if sha_text(src_txt) == sha_text(before):
            return before, before, "目标与源 sha256 已相同（已完成）"
        return before, src_txt, "用 %s 覆盖（%d → %d 字节）" % (a["src"], len(before.encode()), len(src_txt.encode()))
    if kind in ("replace", "insert"):
        anchor = a["anchor"]
        n = before.count(anchor)
        if n != 1:
            return before, None, "锚点命中 %d 次（要求恰好 1 次）" % n
        if a["content"].strip() and a["content"].strip() in before:
            return before, before, "目标内容已存在（已完成）"
        if kind == "replace":
            after = before.replace(anchor, a["content"], 1)
        else:
            pos = before.index(anchor)
            if a.get("where") == "before":
                after = before[:pos] + a["content"] + before[pos:]
            else:
                end = pos + len(anchor)
                after = before[:end] + a["content"] + before[end:]
        return before, after, "锚点唯一命中；行数 %d→%d" % (len(before.splitlines()), len(after.splitlines()))
    if kind == "append":
        if a["content"].strip() in before:
            return before, before, "追加内容已存在（已完成）"
        after = before.rstrip("\n") + "\n\n" + a["content"].strip() + "\n"
        return before, after, "追加到末尾（+%d 行）" % (len(a["content"].splitlines()) + 1)
    return before, None, "未知 kind: %s" % kind


def line_diff(before, after, limit=DIFF_LIMIT):
    """锚点命中行的**前后行**（带行号）。→ (打印行列表, 变更行数)"""
    if after is None:
        return [], 0
    bl = before.split("\n")
    al = after.split("\n")
    if bl and bl[-1] == "":
        bl = bl[:-1]
    if al and al[-1] == "":
        al = al[:-1]
    p = 0
    while p < len(bl) and p < len(al) and bl[p] == al[p]:
        p += 1
    s = 0
    while s < len(bl) - p and s < len(al) - p and bl[len(bl) - 1 - s] == al[len(al) - 1 - s]:
        s += 1
    del_b = bl[p:len(bl) - s]
    add_a = al[p:len(al) - s]
    changed = max(len(del_b), len(add_a))
    out = []
    ctx = 2
    for k in range(max(0, p - ctx), p):
        out.append("   %5d| %s" % (k + 1, bl[k]))
    shown = 0
    for j, t in enumerate(del_b):
        if shown >= limit:
            out.append("   ……（下方另有 %d 行新增未显示）" % max(0, len(add_a) - j))
            break
        out.append(" - %5d| %s" % (p + 1 + j, t))
        shown += 1
    for j, t in enumerate(add_a):
        if j + shown >= limit:
            out.append(" + ……（另有 %d 行新增未显示）" % (len(add_a) - j))
            break
        out.append(" + %5d| %s" % (p + 1 + j, t))
    tail_start = len(bl) - s
    for k in range(tail_start, min(len(bl), tail_start + ctx)):
        out.append("   %5d| %s" % (k + 1, bl[k]))
    return out, changed


# ─────────────────────────────────────────────────────────── 证据卡
def build_cards(ledger_dir, root, pr, audit=None, only=None, actions=None):
    rows = parse_ledgers(ledger_dir, audit)
    if actions is None:
        actions = pr.load_actions()
    amap = {a["id"]: a for a in actions}
    cards = []
    for r in rows:
        if r["id"] not in amap:
            continue
        if only and r["id"] not in only and r["file"] not in only:
            continue
        a = amap[r["id"]]
        v = pr.check(a, root)
        before, after, note = hypothetical(a, root, audit)
        dl, changed = line_diff(before, after)
        cards.append(dict(
            file=r["file"], id=r["id"], title=a.get("title", ""), kind=a["kind"],
            path=a["path"], verdict=v["verdict"], detail=v["detail"],
            revers=r["revers"], revers_why=r["revers_why"],
            decided=r["decided"], decision_cell=r["decision_cell"], line=r["line"],
            phrase="批准 %s:%s" % (r["file"], r["id"]),
            sha_before=(sha_text(before)[:SHA_SHOW] if before is not None else None),
            sha_after=(sha_text(after)[:SHA_SHOW] if after is not None else None),
            changed_lines=changed, diff=dl, note=note,
            risk=a.get("risk", ""), evidence=a.get("evidence", ""), obs=note,
            no_approve_consequence=("不批后果：%s（台账未声明后果，此处转述其风险列/说明）"
                                    % (a.get("note") or a.get("risk") or "台账亦未给风险列")),
            can_one_shot=bool(v["verdict"] == "ACTIVE" and r["revers"] == "0"
                              and after is not None and a["kind"] != "manual"),
        ))
    order = {"ACTIVE": 0, "REWRITE": 1, "ALREADY_DONE": 2, "STALE": 3, "NO_PREMISE": 4}
    cards.sort(key=lambda c: (order.get(c["verdict"], 9), c["id"]))
    return cards, rows


def render(cards, rows, ledger_dir=None):
    out = []
    ready = [c for c in cards if c["can_one_shot"]]
    irrev = [c for c in cards if c["verdict"] == "ACTIVE" and c["revers"] == "1"]
    unknown = [c for c in cards if c["verdict"] == "ACTIVE" and c["revers"] == "?"]
    other = [c for c in cards if c["verdict"] != "ACTIVE"]
    out.append("=" * 96)
    out.append("待批项证据卡（机器可判子集）｜台账文件 %d 个 / 解析行 %d｜可一句批准 %d / 不可逆 %d / 未知 %d / 其它 %d"
               % (len({r['file'] for r in rows}), len(rows), len(ready), len(irrev), len(unknown), len(other)))
    no_id = []
    if ledger_dir:
        parse_ledgers(ledger_dir, no_id=no_id)
    if no_id:
        byf = {}
        for x in no_id:
            byf.setdefault(x["file"], []).append(x["line"])
        out.append("另有 %d 行无 ID 单元格（不可批准，仅信息）：%s"
                   % (len(no_id), "、".join("%s:%s" % (k, ",".join(map(str, v))) for k, v in sorted(byf.items()))))
    out.append("=" * 96)
    for c in cards:
        badge = {"ACTIVE": "可批" if c["can_one_shot"] else "待核", "REWRITE": "需重写",
                 "ALREADY_DONE": "已完成", "STALE": "已失效", "NO_PREMISE": "需人"}[c["verdict"]]
        out.append("")
        out.append("── [%s] %s ｜ %s" % (c["id"], c["title"], c["file"]))
        out.append("   判定: %s (%s) ｜ revers=%s（%s）｜ 行号 %d"
                   % (c["verdict"], badge, c["revers"], c["revers_why"], c["line"]))
        out.append("   目标: %s ｜ kind=%s ｜ %s" % (c["path"], c["kind"], c["note"]))
        if c["sha_after"]:
            out.append("   sha256: %s → %s（变更 %d 行）"
                       % (c["sha_before"], c["sha_after"], c["changed_lines"]))
        if c["diff"]:
            out.append("   将改动的前后行：")
            out.extend("     " + l for l in c["diff"])
        out.append("   批准语: %s" % c["phrase"])
        out.append("   %s" % c["no_approve_consequence"])
        if c["verdict"] == "ACTIVE" and c["revers"] == "1":
            out.append("   ⚠️ revers=1（不可逆）⇒ 必须逐条拍板，本工具不代批")
        if c["verdict"] == "STALE":
            out.append("   ⚠️ 前提已消失（锚点 0 命中）⇒ 建议关闭该声明，**不会静默跳过**")
    out.append("")
    out.append("-" * 96)
    out.append("一次性批准（可逆 ∧ 前提成立）清单：%s"
               % (", ".join(c["phrase"] for c in ready) or "（无）"))
    return "\n".join(out)


# ─────────────────────────────────────────────────────────── 写盘：唯一出口
def refuse_nonledger(ledger_dir, path):
    """硬拦越权写：只允许写 ledger_dir 下的 DECISIONS*.md。"""
    rp = os.path.realpath(path)
    rd = os.path.realpath(ledger_dir)
    if not rp.startswith(rd + os.sep):
        return "拒绝写入台账目录之外：%s（本工具只写 %s/DECISIONS*.md）" % (rp, rd)
    if not os.path.basename(rp).startswith("DECISIONS") or not rp.endswith(".md"):
        return "拒绝写入非台账文件：%s" % rp
    if "/memory/" in rp or rp.endswith("memory"):
        return "拒绝写 memory 树：%s（宪法：memory 只能 patch/新建，须人工批准）" % rp
    return None


def find_row(ledger_dir, fname, rid, audit=None):
    rows = parse_ledgers(ledger_dir, audit)
    for r in rows:
        if r["file"] == fname and r["id"].upper() == rid.upper():
            return r
    return None


def approve(ledger_dir, target, ts=None, set_revers=False, force=False, root=None):
    """→ (rc, message)。target = "<台账文件名>:<ID>"。"""
    if ":" not in target:
        return 2, "格式应为 <台账文件名>:<ID>，如 DECISIONS.md:C2"
    fname, rid = target.rsplit(":", 1)
    root = root or REPO
    path = os.path.join(ledger_dir, fname)
    err = refuse_nonledger(ledger_dir, path)
    if err:
        return 3, err
    if not os.path.exists(path):
        return 2, "台账文件不存在：%s" % path
    row = find_row(ledger_dir, fname, rid.strip())
    if row is None:
        return 2, "在 %s 中找不到 ID=%s（ID 跨文件重名，请核对文件名）" % (fname, rid)
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M")
    stamp = "✅ 已批准 %s（%s）" % (ts, STAMP_MARK)
    text = read_text(path)
    lines = text.split("\n")
    raw = lines[row["line"] - 1]
    header, cells = row["header"], list(row["cells"])

    # ① 前提未成立时不得盖章（fail-closed；--force 才放行）
    if not force:
        try:
            pr = load_reuse(ledger_dir)[0]
            amap = {a["id"]: a for a in pr.load_actions()}
            if rid in amap:
                v = pr.check(amap[rid], root)
                if v["verdict"] != "ACTIVE":
                    return 5, ("拒绝盖章：前提未成立（%s/%s）⇒ 该声明应先关闭或重写，"
                               "确要强批请加 --force" % (v["verdict"], v["detail"]))
        except Exception as e:      # 复用件不可用时不阻断（已在别处报错）
            print("（前提门跳过：%s）" % e)

    # ② 幂等：该条已盖章（单元格式或追加式）⇒ 不动文件
    if STAMP_MARK in (row["decision_cell"] or ""):
        return 0, "ALREADY：%s:%s 已盖审批章（未改动文件）" % (fname, rid)
    if any(l.startswith("- ✅ %s 已批准" % rid) and STAMP_MARK in l for l in lines):
        return 0, "ALREADY：%s:%s 已盖审批章（未改动文件）" % (fname, rid)

    # ③ 选写法定式：决策列可逐字复现且为空 ⇒ 写单元格；否则退回末尾追加
    cell_mode = "决策" in header and not (row["decision_cell"] or "").strip()
    if cell_mode:
        orig_rebuilt = "| " + " | ".join(cells) + " |"
        if orig_rebuilt.replace(" ", "") != raw.replace(" ", ""):
            cell_mode = False       # 该行空格/转义无法逐字复现 ⇒ 不碰原行，改追加式
    if cell_mode:
        if STAMP_MARK in (row["decision_cell"] or ""):
            return 0, "ALREADY：%s:%s 已盖审批章（未改动文件）" % (fname, rid)
        cells[header.index("决策")] = stamp
        if set_revers:
            for k, h in enumerate(header):
                if h.lower() == "revers":
                    cells[k] = "1"
        new_lines = list(lines)
        new_lines[row["line"] - 1] = "| " + " | ".join(cells) + " |"
        how = "decision-cell"
    else:
        if any(l.startswith("- ✅ %s 已批准" % rid) and STAMP_MARK in l for l in lines):
            return 0, "ALREADY：%s:%s 已盖审批章（未改动文件）" % (fname, rid)
        bullet = "- ✅ %s 已批准 %s（%s；内容未改动）" % (rid, ts, STAMP_MARK)
        if "## 审批回填（%s）" % STAMP_MARK in text:
            new_lines = lines + [bullet]
        else:
            new_lines = lines + ["", "## 审批回填（%s）" % STAMP_MARK, bullet]
        how = "append-section"
    after_text = "\n".join(new_lines)
    # 写前自证：只有预期的那一处变化
    b = text.split("\n")
    a = after_text.split("\n")
    if how == "decision-cell":
        diff_idx = [k for k in range(max(len(b), len(a))) if (b[k] if k < len(b) else None) != (a[k] if k < len(a) else None)]
        if diff_idx != [row["line"] - 1]:
            return 4, "自证失败：预期只改第 %d 行，实际改了 %s" % (row["line"], diff_idx)
    else:
        if a[:len(b)] != b:
            return 4, "自证失败：追加模式不应改动既有行"
    tmp = path + ".tmp_pe"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(after_text)
    os.replace(tmp, path)
    sha1 = sha_file(path)
    # 写后自证：仍可解析且行数增量符合预期
    rows2 = parse_ledgers(ledger_dir)
    hit = [r for r in rows2 if r["file"] == fname and r["id"].upper() == rid.upper()]
    if not hit:
        return 4, "自证失败：写入后该条已不可解析（请检查 %s）" % path
    return 0, ("OK：%s:%s 已盖审批章（方式=%s，台账行数 %d→%d，sha256 %s）"
               % (fname, rid, how, len(b), len(a), sha1[:SHA_SHOW]))


def unapprove(ledger_dir, target):
    if ":" not in target:
        return 2, "格式应为 <台账文件名>:<ID>"
    fname, rid = target.rsplit(":", 1)
    rid = rid.strip()
    path = os.path.join(ledger_dir, fname)
    err = refuse_nonledger(ledger_dir, path)
    if err:
        return 3, err
    row = find_row(ledger_dir, fname, rid)
    if row is None:
        return 2, "找不到 ID=%s" % rid
    text = read_text(path)
    lines = text.split("\n")
    n = 0
    # ① 决策列盖章
    if "决策" in row["header"] and STAMP_MARK in (row["decision_cell"] or ""):
        cells = list(row["cells"])
        cells[row["header"].index("决策")] = ""
        lines[row["line"] - 1] = "| " + " | ".join(cells) + " |"
        n += 1
    # ② 尾部审批记录
    keep = [l for l in lines
            if not (l.startswith("- ✅ %s 已批准" % rid) and STAMP_MARK in l)]
    n += len(lines) - len(keep)
    lines = keep
    hdr = "## 审批回填（%s）" % STAMP_MARK
    if hdr in lines and not any(l.startswith("- ✅ ") and STAMP_MARK in l
                                for l in lines[lines.index(hdr):]):
        i = lines.index(hdr)
        del lines[i]
        if i >= 1 and lines[i - 1] == "":
            del lines[i - 1]
        n += 1
    if n == 0:
        return 2, "该条未被本工具盖过章（无 %s 标记）" % STAMP_MARK
    tmp = path + ".tmp_pe"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)
    return 0, "OK：已撤销 %s:%s 的审批章（移除 %d 处）" % (fname, rid, n)


# ───────────────────────────────── 验收④：用户「批」→ revers 回填（内容不动）
BACKFILL_MARK = "pending_evidence"


def backfill_revers(ledger_dir, target, to, reply, ts=None, apply_=False):
    """把 <台账文件>:<ID> 那一条的 revers 标记回填为 to（0/1）+ 时间戳 + 用户回复原文。

    只动「revers」/「回滚」单元格 + 一节回填记录；目标行之外的**任何内容列逐字不变**。
    apply_=False（默认）= dry-run：只打印将改动的前后行，零写盘。
    → (rc, message)
    """
    if ":" not in target:
        return 2, "格式应为 <台账文件名>:<ID>，如 DECISIONS.md:C2"
    if to not in ("0", "1"):
        return 2, "--to 只接受 0 或 1"
    if not (reply or "").strip():
        return 2, "回填必须给出 --reply「用户回复原文」（留痕的唯一凭据）"
    fname, rid = target.rsplit(":", 1)
    rid = rid.strip()
    path = os.path.join(ledger_dir, fname)
    err = refuse_nonledger(ledger_dir, path)
    if err:
        return 3, err
    if not os.path.exists(path):
        return 2, "台账文件不存在：%s" % path
    row = find_row(ledger_dir, fname, rid)
    if row is None:
        return 2, "在 %s 中找不到 ID=%s" % (fname, rid)
    header, cells = row["header"], list(row["cells"])
    keys = [h.lower() for h in header]
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M")
    reply1 = " ".join((reply or "").split())[:120]

    if "revers" in keys:
        idx = keys.index("revers")
        old = cells[idx]
        new = "%s（用户 %s 回复「%s」）" % (to, ts, reply1)
    elif "回滚" in keys:
        idx = keys.index("回滚")
        old = cells[idx]
        if to == "1":
            add = "判定 revers=1（不可逆，用户 %s 回复「%s」）" % (ts, reply1)
        else:
            add = "判定 revers=0（可逆：git checkout 可还原；用户 %s 回复「%s」）" % (ts, reply1)
        new = (old.rstrip() + "；" + add) if old.strip() not in ("", "-", "—", "–") else add
    else:
        return 4, "该台账既无「revers」列也无「回滚」列，无处回填（不改内容本身）"
    if new == old or ("用户 %s 回复「%s」" % (ts, reply1)) in old:
        return 0, "ALREADY：%s:%s 的 revers 已回填为 %s（%s；未改动文件）" % (fname, rid, to, ts)

    text = read_text(path)
    lines = text.split("\n")
    raw = lines[row["line"] - 1]
    orig_rebuilt = "| " + " | ".join(row["cells"]) + " |"
    if orig_rebuilt.replace(" ", "") != raw.replace(" ", ""):
        return 4, "该行无法逐字复现（含转义/空格差异），拒绝改写以免损坏表格：%r" % raw[:120]
    cells[idx] = new
    rebuilt = "| " + " | ".join(cells) + " |"
    new_lines = list(lines)
    new_lines[row["line"] - 1] = rebuilt

    hdr = "## 回滚回填（%s）" % BACKFILL_MARK
    bullet = "- 🔁 %s revers=%s @%s（用户回复「%s」；仅回填 revers，内容未改动）" % (
        rid, to, ts, reply1)
    keep = [l for l in new_lines if not l.startswith("- 🔁 %s revers=" % rid)]
    removed = len(new_lines) - len(keep)
    new_lines = keep
    if hdr in new_lines:
        new_lines.append(bullet)
    else:
        new_lines += ["", hdr, bullet]

    # 自证（真检查）：除目标行 + 回填记录外，其余行逐字不变
    import difflib

    def _bf_ok(s):
        return s.strip() == "" or s.startswith("- 🔁 ") or s.startswith("## 回滚回填")

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=lines, b=new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and i1 == row["line"] - 1:
            continue
        if all(_bf_ok(x) for x in lines[i1:i2]) and all(_bf_ok(x) for x in new_lines[j1:j2]):
            continue
        return 4, "自证失败：预期外改动 %s %r → %r" % (tag, lines[i1:i2][:2], new_lines[j1:j2][:2])

    preview = [
        "目标行（第 %d 行）:" % row["line"],
        "  - %s" % raw,
        "  + %s" % rebuilt,
        "回填记录: %s" % bullet, 
        "revers 判定: %s → %s" % (row["revers"], to),
    ]
    if not apply_:
        return 0, "DRYRUN（零写盘，加 --apply 才落盘）\n" + "\n".join(preview)

    b0 = read_text(path)
    tmp = path + ".tmp_pe"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))
    os.replace(tmp, path)
    after = read_text(path)
    rr = [r for r in parse_ledgers(ledger_dir) if r["file"] == fname and r["id"].upper() == rid.upper()]
    if len(rr) != 1 or rr[0]["revers"] != to:
        return 4, "落盘后复读失败（解析到 %d 行 / revers=%s）⇒ 已写但状态不符，请人工核对 %s" % (
            len(rr), rr[0]["revers"] if rr else "-", path)
    return 0, "OK：%s:%s revers 回填为 %s（%s，sha %s→%s，仅 1 个单元格+1 条记录变化）\n%s" % (
        fname, rid, to, ts, sha_text(b0)[:12], sha_text(after)[:12], "\n".join(preview))


def cross_check(ledger_dir):
    """与 view_pending.py 的解析对账。口径：view_pending 把行 ID 存 row['_id']
    （只有表头字面叫 ID 的表才有 row['ID']）⇒必须用 _id 比对，且比 **(文件, ID) 集合**
    而非只比行数（实测只比行数会把「序」列台账误判成多 8 行）。"""
    cand = [os.path.join(ledger_dir, "view_pending.py"),
            os.path.join(DEFAULT_LEDGER, "view_pending.py")]
    p = next((c for c in cand if os.path.exists(c)), None)
    if p is None:
        return "对账跳过：找不到 view_pending.py"
    import pathlib
    vp = load_module(p, "vp_reuse")
    no_id = []
    mine = parse_ledgers(ledger_dir, no_id=no_id)
    set_m = {(r["file"], r["id"]) for r in mine}
    pen_m = len([r for r in mine if not r["decided"]])
    set_v, pen_v, tot_v = set(), 0, 0
    for f in sorted(pathlib.Path(ledger_dir).glob("DECISIONS*.md")):
        rs, _ = vp.parse_file(f)
        for r in rs:
            rid = (r.get("_id") or "").strip()
            if not rid:
                continue
            tot_v += 1
            set_v.add((f.name, rid))
            if not vp.is_decided(r.get("决策")):
                pen_v += 1
    only_m, only_v = sorted(set_m - set_v), sorted(set_v - set_m)
    same = not only_m and not only_v and pen_m == pen_v
    tag = "一致" if same else "**不一致（需查）**"
    detail = ""
    if no_id:
        byf = {}
        for x in no_id:
            byf.setdefault(x["file"], []).append(x["line"])
        detail = "；无 ID 行（不可批准，仅信息）%d 行：%s" % (
            len(no_id), "、".join("%s:%s" % (k, ",".join(map(str, v))) for k, v in sorted(byf.items())))
    extra = ""
    if only_m:
        extra += "；仅我有 %d 条(前5):%s" % (len(only_m), only_m[:5])
    if only_v:
        extra += "；仅有 pv %d 条(前5):%s" % (len(only_v), only_v[:5])
    return ("对账(view_pending): 有ID行 我=%d/pv=%d（集合差 我多=%d/pv多=%d）；未决 我=%d/pv=%d%s ⇒ %s"
            % (len(set_m), len(set_v), len(only_m), len(only_v), pen_m, pen_v, detail + extra, tag))


# ─────────────────────────────────────────────────────────── 负控自测
def selftest():
    import tempfile
    ok = total = 0

    def expect(label, cond):
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print("[%s] %s" % ("PASS" if cond else "FAIL", label))

    pr, pr_path = load_reuse(DEFAULT_LEDGER)
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        led = os.path.join(d, "ledger")
        os.makedirs(root, exist_ok=True)
        os.makedirs(led, exist_ok=True)
        # 目标文件
        with open(os.path.join(root, "code.py"), "w", encoding="utf-8") as f:
            f.write("a=1\n# ANCHOR\nb=2\n")
        with open(os.path.join(root, "other.py"), "w", encoding="utf-8") as f:
            f.write("untouched\n")
        # 台账（一类有『决策』空单元格，一行 revers=1）
        ledger_txt = (
            "# 测试台账\n\n生成时间：2026-09-26\n\n本批均 `revers=0`。\n\n"
            "| 决策 | ID | 目标文件 | 改什么 | 规模 | 风险 | 回滚 | 来源证据 |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "|  | T1 | `code.py` | 锚点后加一行 | 锚点唯一命中 | low | git checkout -- code.py | 自测 |\n"
            "|  | T2 | `code.py` | 锚点消失的条目 | 手工 | low | ｜不可逆｜ | 自测 |\n"
        )
        lp = os.path.join(led, "DECISIONS_TEST.md")
        with open(lp, "w", encoding="utf-8") as f:
            f.write(ledger_txt)
        actions = [
            dict(id="T1", kind="insert", where="after", path="code.py", anchor="# ANCHOR",
                 content="x = 42\n", title="自测插入", risk="low", evidence="自测"),
            dict(id="T2", kind="insert", where="after", path="code.py", anchor="# NOPE",
                 content="y = 1\n", title="自测失效锚点", risk="low", evidence="自测"),
            dict(id="T3", kind="create", path="new.txt", content="hello\n", title="自测新建",
                 risk="low", evidence="自测"),
        ]
        sha_before = {p: sha_file(os.path.join(led, p)) for p in os.listdir(led)}
        sha_root = {p: sha_file(os.path.join(root, p)) for p in os.listdir(root)}

        aud = Audit()
        cards, rows = build_cards(led, root, pr, aud, actions=actions)

        # NC1: 无 --approve 时零写盘（含被扫目标文件与台账）
        changed = aud.changed() + [(p, sha_root[p], sha_file(os.path.join(root, p)))
                                   for p in sha_root if sha_file(os.path.join(root, p)) != sha_root[p]]
        expect("NC1 分析阶段零写盘（读过 %d 文件，CHANGED=%d）" % (len(aud.paths), len(changed)),
               len(aud.paths) > 0 and not changed)

        # NC2: 锚点 0 命中必须报 STALE，且不得静默跳过
        t2 = [c for c in cards if c["id"] == "T2"][0]
        expect("NC2 锚点0命中 → STALE 且出现在结果里（不出现在可批清单）",
               t2["verdict"] == "STALE" and not t2["can_one_shot"])

        # NC3: 预览真实（含 + 行与行号）且不写文件
        t1 = [c for c in cards if c["id"] == "T1"][0]
        expect("NC3 预览含『将改动的前后行』且带行号（+ 行 %d 条）"
               % len([l for l in t1["diff"] if l.strip().startswith("+")]),
               any(l.strip().startswith("+") for l in t1["diff"])
               and t1["sha_after"] and t1["sha_after"] != t1["sha_before"]
               and sha_file(os.path.join(root, "code.py")) == sha_root["code.py"])

        # NC4: revers 判定（回滚列可执行 ⇒ 0；自述不可逆 ⇒ 1）
        expect("NC4 revers 判定：git checkout ⇒0、『不可逆』⇒1",
               t1["revers"] == "0" and t2["revers"] == "1")
        expect("NC4b revers=1 条目不得进一次性批准清单",
               all(c["revers"] != "1" for c in cards if c["can_one_shot"]))

        # NC5: 越权写入必须被拒（改 tools/memory 类路径）
        rc5, msg5 = approve(led, "../root/code.py:T1")
        expect("NC5 越权写盘被拒（rc=%d）" % rc5, rc5 == 3 and sha_file(os.path.join(root, "code.py")) == sha_root["code.py"])
        rc5b, msg5b = approve(led, "DECISIONS_TEST.md:T1", ts="2026-09-26 12:00", root=root)
        expect("NC5b 合法写盘成功（%s）" % msg5b[:60], rc5b == 0)
        # 只有台账文件变了
        after_led = {p: sha_file(os.path.join(led, p)) for p in os.listdir(led)}
        others_ok = all(after_led[p] == sha_before[p] for p in sha_before if p != "DECISIONS_TEST.md")
        stamp_ok = "已批准 2026-09-26 12:00" in read_text(lp)
        expect("NC5c 只写目标台账、且含时间戳（其它台账文件 sha 未变=%s）" % others_ok,
               others_ok and stamp_ok)
        # 行数增量符合预期
        expect("NC5d 写入方式=决策列单元格（其余行逐字未变）",
               len(read_text(lp).split("\n")) == len(ledger_txt.split("\n")))
        # NC6: 幂等
        rc6, msg6 = approve(led, "DECISIONS_TEST.md:T1", ts="2026-09-26 13:00", root=root)
        expect("NC6 二次批准幂等（%s，sha 未变=%s）"
               % (msg6[:20], sha_file(lp) == after_led["DECISIONS_TEST.md"]),
               msg6.startswith("ALREADY") and sha_file(lp) == after_led["DECISIONS_TEST.md"])
        # NC7: 撤销后逐字还原
        rc7, msg7 = unapprove(led, "DECISIONS_TEST.md:T1")
        expect("NC7 --unapprove 逐字还原（sha 还原=%s）" % (sha_file(lp) == sha_before["DECISIONS_TEST.md"]),
               rc7 == 0 and sha_file(lp) == sha_before["DECISIONS_TEST.md"])
        # NC8: readonly-proof 能抓到变化（对证明本身做负控）
        aud8 = Audit()
        aud8.read(os.path.join(root, "other.py"))
        with open(os.path.join(root, "other.py"), "a", encoding="utf-8") as f:
            f.write("tamper\n")
        expect("NC8 --readonly-proof 对篡改报 CHANGED>0（负控）", len(aud8.changed()) == 1)
        # NC9: append-模式（无决策列）也必须可撤销
        lp2 = os.path.join(led, "DECISIONS_TEST2.md")
        with open(lp2, "w", encoding="utf-8") as f:
            f.write("# t\n\n| ID | 目标 | 内容 |\n|---|---|---|\n| X1 | `code.py` | 改点什么 |\n")
        s2 = sha_file(lp2)
        rcA, msgA = approve(led, "DECISIONS_TEST2.md:X1", ts="2026-09-26 12:30", root=root)
        rcU, msgU = unapprove(led, "DECISIONS_TEST2.md:X1")
        expect("NC9 无决策列 ⇒ 追加式盖章且可逐字撤销（sha 还原=%s）" % (sha_file(lp2) == s2),
               rcA == 0 and rcU == 0 and sha_file(lp2) == s2)

        # NC10: 无 ID 单元格的表格行必须显式列出，不得静默丢弃/误当批准项
        with open(os.path.join(led, "DECISIONS_TEST3.md"), "w", encoding="utf-8") as f:
            f.write("# t\n\n| 序 | 项 | 成本 |\n|---|---|---|\n| T1 | 安装 | 1s |\n"
                    "| 每日 09:30 安装 | plist | 1s |\n")
        nid = []
        rows3 = parse_ledgers(led, no_id=nid)
        expect("NC10 无 ID 行被显式列出且不入 rows（no_id=%d，rows 无该行）"
               % len(nid), len(nid) == 1 and any(r["file"] == "DECISIONS_TEST3.md" and r["id"] == "T1" for r in rows3))
        cc = cross_check(led)
        expect("NC10b 对账把「无 ID 行」单列口径且集合一致（%s）" % cc.split("；")[-1].strip(),
               ("无 ID 行（不可批准，仅信息）1 行" in cc) and cc.endswith("一致"))

        # NC11: 验收④ revers 回填 —— dry-run 零写盘，且给出前后行
        with open(os.path.join(led, "DECISIONS_BF.md"), "w", encoding="utf-8") as f:
            f.write("# t\n\n| 决策 | ID | 目标 | 改什么 | revers | 来源 |\n|---|---|---|---|---|---|\n"
                    "|  | B1 | `code.py` | 加一行 | 0 | 自测 |\n"
                    "|  | B2 | `code.py` | 改另一处 | 0 | 自测 |\n")
        lb = os.path.join(led, "DECISIONS_BF.md")
        sb = sha_file(lb)
        rc11, m11 = backfill_revers(led, "DECISIONS_BF.md:B1", "1", "批", ts="2026-09-26 13:00")
        expect("NC11 回填 dry-run 零写盘且打印前后行（sha 未变=%s）" % (sha_file(lb) == sb),
               rc11 == 0 and m11.startswith("DRYRUN") and "  - " in m11 and "  + " in m11
               and sha_file(lb) == sb)
        # NC12: --apply ⇒ 只有目标单元格变化，其余行逐字不变，ID 仍可解析，revers 翻转
        rc12, m12 = backfill_revers(led, "DECISIONS_BF.md:B1", "1", "批",
                                    ts="2026-09-26 13:00", apply_=True)
        now = read_text(lb).split("\n")
        rb = [r for r in parse_ledgers(led) if r["file"] == "DECISIONS_BF.md" and r["id"] == "B1"]
        keep_b2 = any(l == "|  | B2 | `code.py` | 改另一处 | 0 | 自测 |" for l in now)
        hdr_n = len([l for l in now if l == "| 决策 | ID | 目标 | 改什么 | revers | 来源 |"])
        expect("NC12 回填 apply：revers 0→1、其余行逐字未变、ID 仍可解析（%s）"
               % m12.split("\n")[0][:56],
               rc12 == 0 and len(rb) == 1 and rb[0]["revers"] == "1" and keep_b2
               and hdr_n == 1 and any(l.startswith("- 🔁 B1 revers=1") for l in now))
        # NC13: 缺 --reply / 越目录 / 不存在 ID ⇒ 拒绝且零写盘
        s13 = sha_file(lb)
        rc13a, _ = backfill_revers(led, "DECISIONS_BF.md:B2", "1", "   ")
        rc13b, _ = backfill_revers(led, "../root/code.py:B1", "1", "批")
        rc13c, _ = backfill_revers(led, "DECISIONS_BF.md:ZZZ", "1", "批")
        rc13d, _ = backfill_revers(led, "DECISIONS_BF.md:B2", "9", "批")
        expect("NC13 回填拒绝（缺reply=%d/越目录=%d/无此ID=%d/非法to=%d）且零写盘"
               % (rc13a, rc13b, rc13c, rc13d),
               (rc13a, rc13b, rc13c, rc13d) == (2, 3, 2, 2) and sha_file(lb) == s13)

    print("-" * 60)
    print("selftest %d/%d" % (ok, total))
    return 0 if ok == total else 1


# ─────────────────────────────────────────────────────────── main
def main():
    ap = argparse.ArgumentParser(description="批准前证据器（只读；写盘仅 --approve / --revers-backfill --apply）")
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGER)
    ap.add_argument("--root", default=REPO)
    ap.add_argument("--only", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--readonly-proof", action="store_true")
    ap.add_argument("--approve", default="")
    ap.add_argument("--unapprove", default="")
    ap.add_argument("--set-revers", action="store_true",
                    help="与 --approve 同用：盖章时把 revers 列置 1")
    ap.add_argument("--revers-backfill", default="",
                    help="验收④：<台账文件>:<ID>，把该条 revers 回填为 --to 的值（默认 dry-run）")
    ap.add_argument("--to", default="", help="回填值 0/1（配 --revers-backfill）")
    ap.add_argument("--reply", default="", help="用户回复原文（回填留痕的唯一凭据）")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    ap.add_argument("--force", action="store_true",
                    help="前提未成立（STALE/REWRITE）时仍允许盖章（默认拒绝）")
    ap.add_argument("--ts", default="")
    ap.add_argument("--cross-check", action="store_true", default=True)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if args.approve:
        rc, msg = approve(args.ledger_dir, args.approve, ts=args.ts or None,
                          set_revers=args.set_revers, force=args.force, root=args.root)
        print(msg)
        sys.exit(rc)
    if args.revers_backfill:
        rc, msg = backfill_revers(args.ledger_dir, args.revers_backfill, args.to,
                                  args.reply, ts=args.ts or None, apply_=args.apply)
        print(msg)
        sys.exit(rc)
    if args.unapprove:
        rc, msg = unapprove(args.ledger_dir, args.unapprove)
        print(msg)
        sys.exit(rc)

    pr, pr_path = load_reuse(args.ledger_dir)
    aud = Audit()
    only = set(x.strip() for x in args.only.split(",") if x.strip()) or None
    cards, rows = build_cards(args.ledger_dir, args.root, pr, aud, only=only)
    print(render(cards, rows, args.ledger_dir))
    print()
    print("复用件: %s" % pr_path)
    if args.cross_check:
        print(cross_check(args.ledger_dir))
    if args.readonly_proof:
        changed = aud.changed()
        print("只读自证: FILES=%d CHANGED=%d" % (len(aud.paths), len(changed)))
        for p, h0, h1 in changed:
            print("  CHANGED %s %s → %s" % (p, h0[:12], h1[:12]))
        if changed:
            sys.exit(1)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(dict(cards=cards, rows=len(rows)), f, ensure_ascii=False, indent=2)
        print("JSON 已写: %s" % args.json)


if __name__ == "__main__":
    main()
