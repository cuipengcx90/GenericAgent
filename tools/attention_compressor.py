#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待批台账「注意力压缩器」（R428 / 第15批 TODO C7 第 21 项）。

要解决的问题
    待批台账（temp/pending_decisions/）现役未决 135 条，散在 23 个台账文件里、字段名各不相同。
    R413 的 pending_triage.py 已做「证据四桶 + 单条批准语」，但人仍要逐条拍 135 次。
    本工具做**第二级压缩**：按**主题**把未决条聚成 ≤10 个决策包 —— 每包一句话说清「这一包决策
    是什么 / 不批会怎样 / 一句批准语」，使拍板次数从 135 降到 ≤10。

三条硬约束（本工具的立场）
    1. **覆盖率必须是 100%**：任何未决行若不在主题映射表里，本器直接 rc=2 报错退出（fail-closed），
       绝不静默漏条。台账新增行后必须显式分类才能跑通 —— 这是「漏一条必报」负控的落点。
    2. **包数 ≤ 10**：超过即 rc=2。包定义写在本文件 PACKAGES 里（单一源）。
    3. **只读**：不改台账、不改目标文件、不发通知；`--readonly-proof` 可复证（前后 sha1 全同）。

自动执行队列（本器不代执行）
    「可逆 ∧ 只读」的行单列一队：可逆 = 台账**回滚列非空**且不含不可逆标记；只读 = 目标动作
    不写文件。该队只是「批一句就能跑」的候选，**仍需一句批准**，本器不落地。

用法
    python3 tools/attention_compressor.py                  # 打 10 个决策包
    python3 tools/attention_compressor.py --coverage       # 只打覆盖率对账数字
    python3 tools/attention_compressor.py --json out.json
    python3 tools/attention_compressor.py --md out.md
    python3 tools/attention_compressor.py --readonly-proof
    python3 tools/attention_compressor.py --selftest
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_LEDGER = os.path.join(REPO, "temp", "pending_decisions")
MAX_PACKAGES = 10

TARGET_KEYS = ["目标文件", "目标文件/对象", "目标", "对象", "文件"]
WHAT_KEYS = ["改什么", "改什么（具体动作）", "项", "具体", "变更内容"]
ROLLBACK_KEYS = ["回滚", "回滚方式", "回退"]
RISK_KEYS = ["风险", "规模/风险", "代价"]
EVID_KEYS = ["来源证据", "证据", "依据（实测）", "证据/影响", "来源报告"]

# ── 主题映射表（单一源）────────────────────────────────────────────────────
# 键 = 行的 (台账文件名, ID)。未列出的行 = 未分类 ⇒ 覆盖率 < 100% ⇒ rc=2。
# 分类口径：看**动作落在哪**（不是看报告编号）；同为「记忆文本订正」的跨文件条目归同包。
TOPIC_MEMBERS = {
    "mem-text": {
        "DECISIONS.md": ["C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12", "C13",
                         "C14", "C15", "C16", "C17", "C18", "C24", "C25", "C50",
                         "C53", "C54", "C55", "C56", "C57", "C58", "C59", "C60",
                         "C61", "C63", "C68"],
        "DECISIONS_R343.md": ["D1", "D2", "D3", "D4"],
        "DECISIONS_R344.md": ["D3"],
        "DECISIONS_R382.md": ["D2"],
        "DECISIONS_R384_l1_slim.md": ["D1", "D2"],
        "DECISIONS_R386_followup.md": ["R386a", "R386c"],
        "DECISIONS_R388_bm.md": ["R388c"],
        "DECISIONS_R404_mem_ref_links.md": ["R404A", "R404B"],
        "DECISIONS_R405_net_doctor.md": ["R405A", "R405B"],
        "DECISIONS_R412_l2_snapshot.md": ["R412B"],
        "DECISIONS_R418_clone_durability.md": ["D3"],
        "DECISIONS_R417_planning_gates.md": ["R417C", "R422C"],
        "DECISIONS_R424_spotlight_route.md": ["D1", "D2"],
    },
    "sched-reflect": {
        "DECISIONS.md": ["C20", "C33", "C34", "C37", "C38", "C39", "C40", "C41", "C65"],
        "DECISIONS_R347_kc_snapshot_scheduling.md": ["A", "B", "C"],
        "DECISIONS_R406_slot_routing.md": ["R406A", "R406B"],
        "DECISIONS_R408_claim_lock.md": ["R408C"],
        "DECISIONS_R411_task_freshness.md": ["R411B"],
        "DECISIONS_R427_const_number_audit.md": ["D1", "D2"],
    },
    "disk-reclaim": {
        "DECISIONS.md": ["C30", "C31", "C35", "C36", "C44", "C46"],
        "DECISIONS_R385_disk.md": ["K1", "K2", "K3", "K4", "K5"],
        "DECISIONS_R393_log_reclaim.md": ["L1", "L4"],
    },
    "backup-dr": {
        "DECISIONS.md": ["C26", "C27", "C28", "C29"],
        "DECISIONS_R381_t0_backup.md": ["B1", "B2", "B3", "B4", "B5", "B6"],
        "DECISIONS_R418_clone_durability.md": ["D1", "D2"],
    },
    "tools-durable": {
        "DECISIONS.md": ["C23", "C32", "C48", "C64"],
        "DECISIONS_R386_followup.md": ["R386b"],
        "DECISIONS_R417_planning_gates.md": ["R422B"],
    },
    "chain-hook": {
        "DECISIONS.md": ["C2", "C3", "C4", "C19", "P-a", "P-b"],
        "DECISIONS_R344.md": ["D1"],
        "DECISIONS_R407_number_audit.md": ["R407A"],
        "DECISIONS_R408_claim_lock.md": ["R408A", "R408B"],
        "DECISIONS_R410_ref_integrity.md": ["R410A"],
        "DECISIONS_R417_planning_gates.md": ["R422A"],
        "DECISIONS_R427_const_number_audit.md": ["D3"],
    },
    "report-archive": {
        "DECISIONS.md": ["C22", "C51"],
        "DECISIONS_R382.md": ["D1", "D3", "D4"],
        "DECISIONS_R344.md": ["D4"],
    },
    "snapshot-source": {
        "DECISIONS.md": ["C42", "C43", "C49"],
        "DECISIONS_R344.md": ["D2"],
    },
    "audit-gate": {
        "DECISIONS.md": ["C1", "C21", "C62"],
        "DECISIONS_R407_number_audit.md": ["R407B"],
        "DECISIONS_R410_ref_integrity.md": ["R410B"],
        "DECISIONS_R417_planning_gates.md": ["R417B"],
    },
    "ledger-policy": {
        "DECISIONS.md": ["C47", "C66", "C67"],
        "DECISIONS_R428_attention_pack.md": ["D1", "D2"],
        "DECISIONS_R386_followup.md": ["R386d"],
        "DECISIONS_R388_bm.md": ["R388a", "R388b", "R388d"],
        "DECISIONS_R393_log_reclaim.md": ["L2", "L3"],
    },
}

# 每包的「一句话决策」与「不批后果」（人工撰写，落点在报告里逐条给证据）
PACKAGES = [
    dict(key="mem-text", name="记忆与规则文本订正",
         one="按报告原文改记忆/规则文本（去重、订正口径、新增一行），不动代码与行为",
         cons="记忆继续与实测不符（下轮读记忆即按错口径判断）；悬空指针与重复条目继续误导"),
    dict(key="sched-reflect", name="定时任务与反射器",
         one="决定每个定时/反射器的注册、停用或 prompt 口径修正（拆词、假阳性过滤、缺 key 降级）",
         cons="继续空转与静默失败（x_ai 已 36 天空转、EIA 缺 key 88 天、haisou 假阳性累计 4 条）"),
    dict(key="disk-reclaim", name="磁盘与日志回收",
         one="执行已量化的大件清理与日志回收（每项已有回收量估算与回滚法）",
         cons="磁盘继续高位（97% 满）与孤儿日志继续增长，随时可能因满盘产生新故障"),
    dict(key="backup-dr", name="备份与灾备",
         one="选定 T0/T1 归档目的地与介质、决定是否给快照加定时、把未跟踪报告入仓",
         cons="关键资产仍只在单机 gitignored 目录，克隆或清理即不可恢复"),
    dict(key="tools-durable", name="工具入仓与耐久化",
         one="把已验证但住在 temp/ 的工具/夹具迁入仓库（只加不改内容）",
         cons="temp/ 一被清理即永久灭失（同类事故已发生多次，如 R357/C23 教训）"),
    dict(key="chain-hook", name="收尾链路与流程接线",
         one="把审计与回测挂进 complete_task 收尾链、给派单入口加查重（多为一行调用、非阻断）",
         cons="同类缺陷下轮仍靠人肉发现；报告双写/编号/派单重复的缺口继续存在"),
    dict(key="report-archive", name="报告归档与编号",
         one="决定报告双写链路、编号分配与改名规则、以及缺失正文的恢复范围",
         cons="编号复用与「报告只存 temp」的风险继续存在，归档完整性无闸门"),
    dict(key="snapshot-source", name="数据源快照与采集面",
         one="决定快照保留策略与新增采集源（含只读面是否扩到微信消息库）",
         cons="时间衰减源继续按天丢数据（kc≈4 周滚动删除）；潜在缺口源永不被覆盖"),
    dict(key="audit-gate", name="口径与审计闸门",
         one="落地数字/引用/规划类审计工具的口径与闸门（ROI v2、编号一致性、报告数值回读）",
         cons="报告里的数值/引用继续无机械校验，错值只能靠人偶然发现"),
    dict(key="ledger-policy", name="台账机制与授权/风险口径",
         one="决定台账自身耐久化、目录外写入清理，以及「自主期可否直写记忆」等授权口径",
         cons="授权口径继续悬空、目录外写入与密钥暴露面继续存在，同类流程偏差会重演"),
]

# 判「这个动作写不写文件」的标记词
WRITE_VERBS = ["新建", "新增", "写入", "落地", "迁入", "移入", "入仓", "补", "改", "订正", "替换",
               "追加", "删除", "清理", "清空", "移除", "外移", "接入", "挂进", "注册", "归档",
               "覆盖", "拆分", "禁用", "停用", "安装", "生成", "分配", "修", "调一次", "加一行",
               "插一行", "合并", "统一", "迁移", "复制", "备份", "销毁", "重启"]
# 注意：不收「只读」「不落地」这两个弱措辞 —— 台账里常写「把某只读工具挂进反射器」（工具只读，但动作是新建文件），
# 实测该措辞会把写文件项误判为只读（R408C），故只收「明确说本行动不动文件」的短语。
# 强只读声明：必须是「对整条行动」的陈述（出现在「改什么」开头）。
STRONG_RO_MARKS = ["无文件变更", "无文件改动", "仅记录", "不改任何", "不改动任何"]
# 弱限定语：只是限定范围（如「不删不改任何现有行」随后又说「新增一行」）⇒ **不能**覆盖写动词。
WEAK_RO_MARKS = ["不改任何现有行", "未执行", "零副作用"]
RO_HEAD = 12  # 强只读声明须出现在「改什么」前 12 字内

DECISION_RE = re.compile(r"是否|选一个|选定|选择|二选一|择一|要不要")
IRREVERSIBLE_MARKS = ["不可逆", "无法回滚", "不可恢复", "不可回退", "无法恢复", "不可撤销"]
PATH_RE = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|md|txt|json|plist|yaml|yml|sh|db|sql|toml|ini|cfg|rs|go|swift|js|ts)")
PATH_HINT_RE = re.compile(r"(?:^|[\s`（(])(?:tools|memory|sche_tasks|reflect_scripts|logs|temp|"
                          r"~/Library|~/models|~/\.[a-z]+|/Users/|/etc/|/var/)[\w~./-]*")


# ── 复用件加载 ──────────────────────────────────────────────────────────────
def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_vp(ledger_dir):
    p = os.path.join(ledger_dir, "view_pending.py")
    if not os.path.exists(p):
        raise SystemExit("缺少复用件: %s" % p)
    return load_module(p, "vp_reuse")


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def field(row, keys):
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            return re.sub(r"\s+", " ", v)
    return ""


# ── 行级判定 ────────────────────────────────────────────────────────────────
def writes_files(target, what):
    """目标动作是否写文件（fail-safe：判不准一律当写文件）。

    判序（先硬证据、后语义；每条都有实测反例支撑）：
      1) 目标列含**具体可写路径/路径前缀** ⇒ 写文件
         （任何「只读/无文件变更」措辞都不能覆盖它；NC3）
      2) 「改什么」**开头**即强只读声明（无文件变更/仅记录…） ⇒ 只读（NC4 / R424:D2）
      3) 「改什么」含写动词（新增/挂进/迁移/删除…） ⇒ 写文件
         （弱限定语「不删不改任何现有行」不能推翻它：R405A 实为"新增一行"；NC7、NC8）
      4) 决策型提问（是否/选一个/二选一…）且无写动词 ⇒ 只读（本行只是"要不要"）
      5) 其余 ⇒ 写文件
    """
    head = what[:RO_HEAD]
    if bool(PATH_RE.search(target)) or bool(PATH_HINT_RE.search(target)):
        return True
    if any(m in head for m in STRONG_RO_MARKS):
        return False
    if any(v in what for v in WRITE_VERBS):
        return True
    if any(m in what for m in WEAK_RO_MARKS):
        return False
    if re.search(r"是否|选一个|选定|选择|二选一|择一|要不要", what):
        return False
    return True


def reversible(rollback, what):
    """可逆 = 回滚列非空 ∧ 无不可逆标记。回滚列空 = 判定不可逆（fail-safe）。"""
    rb = (rollback or "").strip()
    if not rb:
        return False
    if any(m in rb for m in IRREVERSIBLE_MARKS):
        return False
    if any(m in what for m in IRREVERSIBLE_MARKS):
        return False
    return True


def classify(row):
    rid, fname = row["_id"], row["_file"]
    pkg = None
    for key, members in TOPIC_MEMBERS.items():
        if rid in members.get(fname, []):
            pkg = key
            break
    target, what = field(row, TARGET_KEYS), field(row, WHAT_KEYS)
    rollback = field(row, ROLLBACK_KEYS)
    wf = writes_files(target, what)
    rev = reversible(rollback, what)
    nature = "writing" if wf else ("decision" if DECISION_RE.search(what) else "note")
    return dict(file=fname, id=rid, pkg=pkg, target=target, what=what, nature=nature,
                risk=field(row, RISK_KEYS), rollback=rollback[:160],
                evidence=field(row, EVID_KEYS)[:200],
                src_date=str(row.get("_src_date") or ""),
                writes_files=wf, reversible=rev,
                auto_queue=(not wf) and rev)


def collect(ledger_dir, vp, rows_override=None):
    if rows_override is None:
        rows_override = []
        for f in sorted(glob.glob(os.path.join(ledger_dir, "DECISIONS*.md"))):
            rs, _gen = vp.parse_file(pathlib.Path(f))
            for r in rs:
                r.setdefault("_decided", vp.is_decided(r.get("决策")))
            rows_override += rs
    pending = [r for r in rows_override if not r.get("_decided")]
    return [classify(r) for r in pending]


def queue_guard(items):
    """自动执行队列的硬闸门：进队的每条必须满足 只读 ∧ 可逆 ∧ 包内。
    返回违规清单（空 = 通过）；供 --selftest NC10 与 build().ok 使用。"""
    bad = []
    for i in items:
        if not i["auto_queue"]:
            continue
        if i["writes_files"] or not i["reversible"]:
            bad.append(dict(file=i["file"], id=i["id"], why="队列成员写文件或不可逆"))
        if not i["pkg"]:
            bad.append(dict(file=i["file"], id=i["id"], why="队列成员未入包"))
    return bad


def build(ledger_dir, vp, rows_override=None, packages=None, topic_members=None):
    global TOPIC_MEMBERS
    pkgs = PACKAGES if packages is None else packages
    tm = TOPIC_MEMBERS if topic_members is None else topic_members
    old = TOPIC_MEMBERS
    TOPIC_MEMBERS = tm
    try:
        items = collect(ledger_dir, vp, rows_override=rows_override)
    finally:
        TOPIC_MEMBERS = old
    unmatched = [dict(file=i["file"], id=i["id"]) for i in items if not i["pkg"]]
    meta = dict(ledger_dir=ledger_dir, packages=len(pkgs), max_packages=MAX_PACKAGES,
                total_pending=len(items), mapped=len(items) - len(unmatched),
                coverage=(100.0 * (len(items) - len(unmatched)) / len(items)) if items else 100.0,
                unmatched=unmatched,
                auto_queue=sum(1 for i in items if i["auto_queue"]),
                writes_files=sum(1 for i in items if i["writes_files"]),
                reversible=sum(1 for i in items if i["reversible"]),
                files=len({i["file"] for i in items}))
    counts = {p["key"]: 0 for p in pkgs}
    for i in items:
        if i["pkg"]:
            counts[i["pkg"]] = counts.get(i["pkg"], 0) + 1
    missing_fields = [p["key"] for p in pkgs
                      if not (str(p.get("one", "")).strip() and str(p.get("cons", "")).strip())]
    empty_pkgs = sorted(k for k, v in counts.items() if v == 0)
    sum_ok = sum(counts.values()) == meta["mapped"]
    meta.update(pkg_counts=counts, pkg_missing_fields=missing_fields,
                pkg_empty=empty_pkgs, pkg_sum_ok=sum_ok, queue_violations=queue_guard(items))
    ok = ((not unmatched) and len(pkgs) <= MAX_PACKAGES and not missing_fields
          and not empty_pkgs and sum_ok and not meta["queue_violations"])
    meta["ok"] = ok
    return items, pkgs, meta


def package_phrase(pkg_key, items):
    ids = ["%s:%s" % (i["file"], i["id"]) for i in sorted(items, key=lambda x: (x["file"], x["id"]))]
    return "批准 包 %s —— %s" % (pkg_key, " ".join(ids))


# ── 渲染 ────────────────────────────────────────────────────────────────────
def render(items, pkgs, meta):
    out = []
    out.append("# 待批台账决策包（≤10 包，一句话一包）")
    out.append("")
    out.append("- 台账目录: `%s`（%d 个文件）" % (meta["ledger_dir"], meta["files"]))
    out.append("- 覆盖率对账: 未决 %d 条 / 已分类 %d 条 = **%.1f%%**（未分类 %d 条）"
               % (meta["total_pending"], meta["mapped"], meta["coverage"], len(meta["unmatched"])))
    out.append("- 包数: %d / 上限 %d" % (meta["packages"], meta["max_packages"]))
    out.append("- 写文件判定: 写文件 %d 条 ｜ 只读 %d 条 ｜ 可逆 %d 条"
               % (meta["writes_files"], meta["total_pending"] - meta["writes_files"], meta["reversible"]))
    out.append("- 自动执行队列（只读 ∧ 可逆）: %d 条 —— **仍需一句批准；本器不代执行**"
               % meta["auto_queue"])
    out.append("- 未分类: %s" % ("（无）" if not meta["unmatched"]
                                 else json.dumps(meta["unmatched"], ensure_ascii=False)))
    out.append("")
    for p in pkgs:
        rows = [i for i in items if i["pkg"] == p["key"]]
        out.append("## %s（`%s`）— %d 条" % (p["name"], p["key"], len(rows)))
        out.append("")
        out.append("- **一句话决策**: %s" % p["one"])
        out.append("- **不批后果**: %s" % p["cons"])
        out.append("- **最小批准语（一包一句）**: `%s`" % package_phrase(p["key"], rows))
        out.append("- 涵盖 ID: " + "、".join(
            "%s:%s" % (i["file"], i["id"])
            for i in sorted(rows, key=lambda x: (x["file"], x["id"]))))
        out.append("")
    q = [i for i in items if i["auto_queue"]]
    out.append("## 自动执行队列（只读 ∧ 可逆）— %d 条" % len(q))
    out.append("")
    out.append("口径：可逆 = 台账**回滚列非空**且无不可逆标记；只读 = 目标动作不写文件（判不准一律当写文件）。")
    out.append("该队**不是已批准**：每条仍需一句批准，且本器不代执行任何动作。")
    out.append("")
    for nt, label in (("decision", "决策型（只需您一句话拍板；本行台账动作不含文件写入）"),
                      ("note", "记录型（补一行/改一句，可逆）")):
        sub = [i for i in q if i["nature"] == nt]
        if not sub:
            continue
        out.append("**%s** — %d 条" % (label, len(sub)))
        out.append("")
        for i in sorted(sub, key=lambda x: (x["file"], x["id"])):
            out.append("- `%s:%s` ｜ %s ｜ 回滚: %s"
                       % (i["file"], i["id"], i["what"][:110] or "（台账未写「改什么」）",
                          i["rollback"] or "（空）"))
        out.append("")
    return "\n".join(out)


def render_coverage(items, pkgs, meta):
    out = ["coverage: pending=%d mapped=%d unmatched=%d coverage=%.1f%% packages=%d/%d "
           "auto_queue=%d writes_files=%d reversible=%d files=%d pkg_sum_ok=%s queue_bad=%d ok=%s"
           % (meta["total_pending"], meta["mapped"], len(meta["unmatched"]), meta["coverage"],
              meta["packages"], meta["max_packages"], meta["auto_queue"], meta["writes_files"],
              meta["reversible"], meta["files"], meta["pkg_sum_ok"],
              len(meta["queue_violations"]), meta["ok"])]
    for p in pkgs:
        n = sum(1 for i in items if i["pkg"] == p["key"])
        out.append("  %-16s %3d" % (p["key"], n))
    if meta["unmatched"]:
        out.append("  UNMATCHED: " + json.dumps(meta["unmatched"], ensure_ascii=False))
    if meta["pkg_missing_fields"] or meta["pkg_empty"] or not meta["pkg_sum_ok"]:
        out.append("  PKG_ISSUES: missing_fields=%s empty=%s sum_ok=%s"
                   % (meta["pkg_missing_fields"], meta["pkg_empty"], meta["pkg_sum_ok"]))
    if meta["queue_violations"]:
        out.append("  QUEUE_VIOLATIONS: " + json.dumps(meta["queue_violations"], ensure_ascii=False))
    return "\n".join(out)


# ── 只读自证 ────────────────────────────────────────────────────────────────
def readonly_proof(ledger_dir, items):
    paths = sorted(glob.glob(os.path.join(ledger_dir, "DECISIONS*.md")))
    paths += [os.path.join(ledger_dir, p) for p in ("view_pending.py", "premise_recheck.py", "actions.py")
              if os.path.exists(os.path.join(ledger_dir, p))]
    paths += [os.path.join(REPO, p) for p in (i["target"] for i in items)] if False else []
    def snap():
        d = {}
        for p in paths:
            try:
                d[p] = sha1(p)
            except OSError:
                d[p] = None
        return d
    a = snap()
    vp = load_vp(ledger_dir)
    items2 = collect(ledger_dir, vp)
    b = snap()
    changed = [p for p in a if a[p] != b[p]]
    print("readonly-proof: FILES=%d CHANGED=%d ITEMS=%d->%d" % (len(paths), len(changed), len(items), len(items2)))
    return 0 if not changed else 1


# ── 自测（含负控）────────────────────────────────────────────────────────────
def _fake_row(f, rid, decided=False, **cols):
    r = dict(cols)
    r["_id"], r["_file"], r["_decided"] = rid, f, decided
    r.setdefault("_src_date", None)
    return r


def selftest():
    ledger_dir = os.path.join(REPO, "temp", "pending_decisions")
    vp = load_vp(ledger_dir)
    results = []

    def chk(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    # 基线：真台账
    items, pkgs, meta = build(ledger_dir, vp)
    chk("baseline coverage==100%", meta["coverage"] == 100.0 and meta["mapped"] == meta["total_pending"],
        "pending=%d mapped=%d" % (meta["total_pending"], meta["mapped"]))
    chk("baseline packages<=10", meta["packages"] <= MAX_PACKAGES, "packages=%d" % meta["packages"])
    chk("baseline ok", meta["ok"], json.dumps(meta["unmatched"], ensure_ascii=False))

    # NC1 漏一条必报：注入一条未分类行
    rows = [_fake_row("DECISIONS.md", "C1", 目标文件="`roi_monitor.py`", 改什么="落地 v2 口径"),
            _fake_row("DECISIONS.md", "ZZ9", 目标文件="`tools/new_thing.py`", 改什么="新建")]
    _i, _p, m1 = build(ledger_dir, vp, rows_override=rows)
    chk("NC1 漏一条必报", (not m1["ok"]) and len(m1["unmatched"]) == 1 and m1["coverage"] < 100.0,
        "unmatched=%s coverage=%.1f%%" % (json.dumps(m1["unmatched"], ensure_ascii=False), m1["coverage"]))

    # NC2 包数 >10 必报
    many = list(PACKAGES) + [dict(key="x%d" % i, name="n", one="o", cons="c") for i in range(3)]
    rows2 = [_fake_row("DECISIONS.md", "C1", 目标文件="`roi_monitor.py`", 改什么="落地 v2 口径")]
    _i2, _p2, m2 = build(ledger_dir, vp, rows_override=rows2, packages=many)
    chk("NC2 包数>10必报", (not m2["ok"]) and m2["packages"] > MAX_PACKAGES,
        "packages=%d" % m2["packages"])

    # NC3 写文件项误判只读必报：文本里有「无文件变更」但目标是真 .py + 写动词
    r3 = _fake_row("DECISIONS.md", "C1", 目标文件="`tools/evil.py`",
                   改什么="无文件变更，只读记录；实为新建 tools/evil.py")
    i3 = classify(r3)
    chk("NC3 写文件不可误判为只读", i3["writes_files"] is True and i3["auto_queue"] is False,
        "writes_files=%s auto_queue=%s" % (i3["writes_files"], i3["auto_queue"]))

    # NC4 真只读样本必须判只读（正控：从真台账里挑）
    r4 = _fake_row("DECISIONS.md", "C1", 目标文件="（口径/授权）", 改什么="无文件变更：仅记录口径")
    i4 = classify(r4)
    chk("NC4 纯记录判只读（正控）", i4["writes_files"] is False, "writes_files=%s" % i4["writes_files"])

    # NC5 回滚列空必须判不可逆
    r5 = _fake_row("DECISIONS.md", "C1", 目标文件="（决策）", 改什么="仅记录", 回滚="")
    i5 = classify(r5)
    chk("NC5 回滚空⇒不可逆", i5["reversible"] is False and i5["auto_queue"] is False,
        "reversible=%s" % i5["reversible"])

    # NC6 不可逆标记必须判不可逆
    r6 = _fake_row("DECISIONS.md", "C1", 目标文件="（决策）", 改什么="仅记录", 回滚="不可逆，删原文")
    i6 = classify(r6)
    chk("NC6 不可逆标记生效", i6["reversible"] is False, "reversible=%s" % i6["reversible"])

    # NC7 「只读」措辞 + 写动词必须判写文件（R408C 实测形态）
    r7 = _fake_row("DECISIONS.md", "C1", 目标文件="反射器（不落地）",
                   改什么="把 `tools/repeat_labor_detect.py` 作为**只读**月度复跑挂进反射器")
    i7 = classify(r7)
    chk("NC7 只读措辞+写动词⇒写文件", i7["writes_files"] is True and i7["auto_queue"] is False,
        "writes_files=%s" % i7["writes_files"])

    # NC8 弱限定语不可推翻写动词（R405A 实测形态：「不删不改任何现有行」+「新增一行」）
    r8 = _fake_row("DECISIONS.md", "C1", 目标文件="",
                   改什么="在 `memory/global_mem_insight.txt` 的 `通知推送通道:` 行之后**新增一行**（不删不改任何现有行）")
    i8 = classify(r8)
    chk("NC8 弱限定语+新增行⇒写文件", i8["writes_files"] is True, "writes_files=%s" % i8["writes_files"])

    # NC9 包缺「不批后果」⇒ 必须 ok=False
    pk = [dict(p) for p in PACKAGES]
    pk[0]["cons"] = ""
    _i9, _p9, m9 = build(ledger_dir, vp, rows_override=rows, packages=pk)
    chk("NC9 包缺不批后果必报", (not m9["ok"]) and pk[0]["key"] in m9["pkg_missing_fields"],
        "missing=%s" % m9["pkg_missing_fields"])

    # NC10 队列成员写文件 ⇒ queue_guard 必须抓（篡改后再验，正控=原样为空）
    tampered = [dict(i) for i in items]
    for i in tampered:
        if i["auto_queue"]:
            i["writes_files"] = True
            break
    v = queue_guard(tampered)
    chk("NC10 队列成员写文件必报", len(v) == 1 and queue_guard(items) == [],
        "violations=%d base=%d" % (len(v), len(queue_guard(items))))

    # 只读自证：跑两次 classify，语料 sha1 不变由 --readonly-proof 单独证
    chk("auto_queue 是 writes_files 的补集∩reversible",
        all(i["auto_queue"] == ((not i["writes_files"]) and i["reversible"]) for i in items), "")

    bad = [n for n, okk, _d in results if not okk]
    for n, okk, d in results:
        print("%s %s%s" % ("PASS" if okk else "FAIL", n, ("  [" + d + "]") if d else ""))
    print("selftest: %d/%d" % (len(results) - len(bad), len(results)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="待批台账注意力压缩器（只读）")
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGER)
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--md", metavar="PATH")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--readonly-proof", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    vp = load_vp(args.ledger_dir)
    items, pkgs, meta = build(args.ledger_dir, vp)

    if args.readonly_proof:
        return readonly_proof(args.ledger_dir, items)

    if args.coverage:
        print(render_coverage(items, pkgs, meta))
        return 0 if meta["ok"] else 2

    txt = render(items, pkgs, meta)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print("wrote %s (%d bytes)" % (args.md, os.path.getsize(args.md)))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(dict(meta=meta,
                           packages=[dict(key=p["key"], name=p["name"], one=p["one"], cons=p["cons"],
                                          ids=[dict(file=i["file"], id=i["id"]) for i in items
                                               if i["pkg"] == p["key"]],
                                          phrase=package_phrase(p["key"],
                                                                [i for i in items if i["pkg"] == p["key"]]))
                                     for p in pkgs],
                           items=items), f, ensure_ascii=False, indent=1)
        print("wrote %s (%d bytes)" % (args.json, os.path.getsize(args.json)))
    if not args.md and not args.json:
        print(txt)
    elif not args.coverage:
        print(render_coverage(items, pkgs, meta))
    return 0 if meta["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
