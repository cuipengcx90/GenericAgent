#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""goal_loop 真机端到端演练的**独立复核器**（只读，纯 stdlib）。

定位：不调 goal_loop.py 的任何代码，只读它落下的 journal（append-only JSONL），
自己重建状态并判三条判据；再与 CLI 的 resume 输出对账（两条独立通路一致才算过）。

三条判据（对应阶段四工作项3 的验收面）：
  J1 跨轮不丢：条目状态/证据/升级 全部可从 journal 纯函数式重建；重建结果与 CLI resume 逐条一致。
  J2 升级可决策：每条升级记录必须含三要素（卡在哪/试过什么/需要什么决策）+ 可执行选项(id+impact)
                + 证据引用 + 恢复点(resume_token)；不可逆升级还必须写明代价变化(cost_delta)与风险变化(risk_delta)。
  J3 真活&真验收：ACCEPTED 条目必须有 acceptance 命令、外部验收 owner、ora 之外的外部验收事件，
                且其证据引用在盘上真实存在（不是空引用）。

负控（--negctl）：在 /tmp 造四类坏 journal（缺 risk_delta / 缺 tried / ACCEPTED 无 oracle /
  证据引用指向不存在的文件）⇒ 每条必须被抓；正控必须过。

用法（位置子命令）：
  python3 tools/goal_e2e_audit.py audit --root <goalroot> [--resume-json <f>] [--json]
  python3 tools/goal_e2e_audit.py negctl
  python3 tools/goal_e2e_audit.py selftest
  python3 tools/goal_e2e_audit.py proof --root <goalroot> [--root <goalroot> ...]
退出码：0=全过；1=有 finding；2=用不了。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

KIND_KEYS = ("goal", "item", "event", "escalation", "evidence", "approval")


# ----------------------------------------------------------------- 读账本
def read_journal(root):
    p = os.path.join(root, "goal.jsonl")
    if not os.path.isfile(p):
        raise SystemExit("rc=2 用不了：找不到 %s" % p)
    recs = []
    with open(p, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception as e:
                raise SystemExit("rc=2 用不了：第 %d 行不是 JSON（%s）" % (i, e))
    return p, recs


def rebuild(recs):
    """纯函数式重建：journal 是唯一权威，不看任何进程内状态。"""
    b = {"goal": None, "items": {}, "events": [], "escalations": {},
         "evidence": {}, "approvals": [], "counts": {}}
    for r in recs:
        kind = r.get("rec")
        b["counts"][kind] = b["counts"].get(kind, 0) + 1
        pay = None
        for k in KIND_KEYS:
            if k in r:
                pay = r[k]
                break
        if kind == "goal" and isinstance(pay, dict):
            b["goal"] = pay
        elif kind == "item" and isinstance(pay, dict):
            b["items"][pay.get("item_id")] = pay
        elif kind == "event" and isinstance(pay, dict):
            b["events"].append(pay)
        elif kind == "escalation" and isinstance(pay, dict):
            b["escalations"][pay.get("escalation_id")] = pay
        elif kind == "evidence" and isinstance(pay, dict):
            b["evidence"][pay.get("evidence_id")] = pay
        elif kind == "approval" and isinstance(pay, dict):
            b["approvals"].append(pay)
    return b


def state_from_events(b):
    """从事件表独立推导条目状态（不信任 item 记录里的 state 字段）。

    实测形状（2026-09-29）：event 用 object_type/object_id/from_state/to_state，
    与 item 记录的 item_id/state 字段不同名 ⇒ 必须按 object_type 过滤。
    """
    st = {}
    for it in b["items"]:
        st[it] = "PENDING"
    for ev in b["events"]:
        if ev.get("object_type") not in (None, "item"):
            continue
        iid, to = ev.get("object_id"), ev.get("to_state")
        if iid in st and to:
            st[iid] = to
    return st


def evidence_refs_of(b, iid, item):
    """证据引用可能落在 item 记录，也可能落在事件 payload（实测：advance --evidence-ref 走 payload）。"""
    refs = list(item.get("evidence_refs") or [])
    for ev in b["events"]:
        if ev.get("object_type") in (None, "item") and ev.get("object_id") == iid:
            refs += list((ev.get("payload") or {}).get("evidence_refs") or [])
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ----------------------------------------------------------------- 判据
def audit(root, resume_json=None, evidence_check=True):
    jp, recs = read_journal(root)
    b = rebuild(recs)
    findings = []
    ok = []

    if b["goal"] is None:
        findings.append(("J0", "journal", "没有 goal 记录 ⇒ 不是一份目标账本"))
        return jp, b, findings, ok

    # --- J1 重建 vs 事件推导 vs CLI resume ---
    der = state_from_events(b)
    mism = [(k, b["items"][k].get("state"), der[k]) for k in der if b["items"][k].get("state") != der[k]]
    if mism:
        findings.append(("J1", "items", "item 记录内 state 与事件推导不一致：%s" % mism))
    else:
        ok.append(("J1", "条目状态可从事件表独立推导，%d 条一致" % len(der)))
    if resume_json and os.path.isfile(resume_json):
        rj = json.load(open(resume_json, encoding="utf-8"))
        cli = {i["item_id"]: i["state"] for i in rj.get("items") or []}
        if cli != der:
            findings.append(("J1", "resume", "journal 重建 %s ≠ CLI resume %s" % (der, cli)))
        else:
            ok.append(("J1", "与 CLI resume 对账一致（%d 条）" % len(cli)))
    else:
        findings.append(("J1", "resume", "未提供 --resume-json ⇒ 缺独立对账通路（不计通过）"))

    # --- J2 升级可决策 ---
    if not b["escalations"]:
        findings.append(("J2", "escalations", "账本零升级 ⇒ 无法验证三要素（本演练应至少 1 条）"))
    for eid, e in sorted(b["escalations"].items()):
        miss = [f for f in ("where_stuck", "decision_needed") if not str(e.get(f) or "").strip()]
        if not (e.get("tried") or []):
            miss.append("tried")
        opts = e.get("options") or []
        bad_opt = [o for o in opts if not (isinstance(o, dict) and o.get("id") and o.get("impact"))]
        if not opts or bad_opt:
            miss.append("options(id+impact)")
        if not (e.get("evidence_refs") or []):
            miss.append("evidence_refs")
        if not e.get("resume_token"):
            miss.append("resume_token")
        if e.get("irreversible"):
            for f in ("cost_delta", "risk_delta"):
                if e.get(f) in (None, ""):
                    miss.append("%s(不可逆升级必填)" % f)
        if miss:
            findings.append(("J2", "escalation:%s" % eid, "缺：%s" % miss))
        else:
            ok.append(("J2", "escalation:%s 三要素+选项+证据+恢复点齐备%s"
                       % (eid, "（含不可逆代价）" if e.get("irreversible") else "")))

    # --- J3 真活 & 真验收 ---
    # 实测形状：acc = {"checks":[{"cmd": ...}], "owner": ..., "oracle": ...}；
    # 验收事件用 object_type/object_id/to_state；--evidence-ref 走 event.payload。
    acc_actors = {ev.get("actor") for ev in b["events"]
                  if ev.get("to_state") == "ACCEPTED" and ev.get("actor")}
    for iid, it in sorted(b["items"].items()):
        if der.get(iid, it.get("state")) != "ACCEPTED":
            continue
        acc = it.get("acceptance") or {}
        cmds = [c.get("cmd") for c in (acc.get("checks") or []) if isinstance(c, dict)]
        if not any(str(c or "").strip() for c in cmds):
            findings.append(("J3", "item:%s" % iid, "ACCEPTED 但 acceptance 无 cmd ⇒ 无法机械验收"))
        if not acc.get("owner"):
            findings.append(("J3", "item:%s" % iid, "ACCEPTED 但无验收 owner（外部验收者）"))
        elif acc["owner"] not in acc_actors:
            findings.append(("J3", "item:%s" % iid, "无 owner(%s) 的外部验收事件 ⇒ 自评通过" % acc["owner"]))
        if not acc.get("oracle"):
            findings.append(("J3", "item:%s" % iid, "ACCEPTED 但无外部 oracle ⇒ 自验收"))
        refs = evidence_refs_of(b, iid, it)
        if not refs:
            findings.append(("J3", "item:%s" % iid, "ACCEPTED 但零证据引用"))
        for r in refs:
            if r not in b["evidence"]:
                findings.append(("J3", "item:%s" % iid, "引用未登记证据 %s" % r))
        if evidence_check:
            for r in refs:
                ev = b["evidence"].get(r) or {}
                where = ev.get("where") or ev.get("path")
                if where and not os.path.exists(where):
                    findings.append(("J3", "evidence:%s" % r, "证据落点不存在：%s" % where))
        if all(f[1] != ("item:%s" % iid) for f in findings):
            ok.append(("J3", "item:%s 外部 owner 事件+oracle+证据引用齐备（%d 条证据）" % (iid, len(refs))))

    # --- 不可逆放行（与 gate 的独立复算） ---
    approved = set()
    for ap in b["approvals"]:
        if ap.get("decision") == "approve" and (ap.get("scope") in ("irreversible", "*", "any")):
            approved.add(ap.get("object_id"))
    for iid, it in sorted(b["items"].items()):
        if it.get("risk_class") == "irreversible" and it.get("state") not in ("PENDING", "CANCELLED"):
            if iid not in approved:
                findings.append(("J4", "item:%s" % iid, "不可逆且已推进但无放行记录"))
            else:
                ok.append(("J4", "item:%s 不可逆推进有放行记录（approval scope=irreversible）" % iid))
    return jp, b, findings, ok


# ----------------------------------------------------------------- 负控
def _mkroot(tmp, items, escs, events=None, approvals=None, evidence=None):
    os.makedirs(tmp, exist_ok=True)
    lines = [{"rec": "goal", "goal": {"goal_id": "g", "goal_revision": 1, "statement": "s",
                                      "owner": "o", "status": "OPEN"}}]
    for it in items:
        lines.append({"rec": "item", "item": it})
    for ev in (events or []):
        lines.append({"rec": "event", "event": ev})
    for e in escs:
        lines.append({"rec": "escalation", "escalation": e})
    for a in (approvals or []):
        lines.append({"rec": "approval", "approval": a})
    for e in (evidence or []):
        lines.append({"rec": "evidence", "evidence": e})
    with open(os.path.join(tmp, "goal.jsonl"), "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")


def _it(iid, state="ACCEPTED", risk="reversible", oracle="oc", refs=None, owner="v"):
    # 真实形状（实测）：acceptance.checks[].cmd / oracle / owner
    return {"item_id": iid, "goal_id": "g", "goal_revision": 1, "item_revision": 1,
            "kind": "code_edit", "summary": "s", "state": state, "risk_class": risk,
            "acceptance": {"checks": [{"cmd": "true"}], "owner": owner, "oracle": oracle},
            "evidence_refs": (refs if refs is not None else ["E1"])}


def _ev(iid, frm, to, actor, refs=None):
    # 真实形状（实测）：object_type/object_id/from_state/to_state + payload.evidence_refs
    return {"object_type": "item", "object_id": iid, "from_state": frm, "to_state": to,
            "actor": actor, "payload": {"evidence_refs": (refs or [])}}


def _es(eid, irreversible=False, risk_delta=1):
    return {"escalation_id": eid, "goal_id": "g", "reason_kind": "k",
            "where_stuck": "w", "tried": ["t"], "decision_needed": "d",
            "options": [{"id": "a", "impact": "i"}], "evidence_refs": ["E1"],
            "resume_token": "goal:g@r1#0", "cost_delta": 1, "risk_delta": risk_delta,
            "irreversible": irreversible, "decision": None}


def negctl(verbose=True):
    """四类坏 journal 每条必抓 + 一条正控必过。返回 (n_caught, n_expect, details)"""
    cases, res = [], []
    d = tempfile.mkdtemp(prefix="goal_audit_neg_")
    # NC1 不可逆升级缺 risk_delta
    p1 = os.path.join(d, "nc1"); _mkroot(p1, [], [_es("E-A", True, risk_delta=None)])
    cases.append(("NC1 不可逆升级缺 risk_delta", p1, "J2"))
    # NC2 升级缺 tried
    e2 = _es("E-B"); e2["tried"] = []
    p2 = os.path.join(d, "nc2"); _mkroot(p2, [], [e2]); cases.append(("NC2 升级缺 tried", p2, "J2"))
    # NC3 ACCEPTED 无 oracle
    p3 = os.path.join(d, "nc3"); _mkroot(p3, [_it("I1", oracle=None)], [], [_ev("I1", "CLOSE_READY", "ACCEPTED", "v")])
    cases.append(("NC3 ACCEPTED 无 oracle", p3, "J3"))
    # NC4 证据落点不存在（真副本，路径指向不存在文件）
    p4 = os.path.join(d, "nc4")
    ev4 = {"evidence_id": "E1", "where": os.path.join(d, "no_such_file.txt")}
    _mkroot(p4, [_it("I1")], [], [_ev("I1", "CLOSE_READY", "ACCEPTED", "v")], evidence=[ev4])
    cases.append(("NC4 证据落点不存在", p4, "J3"))
    # NC5 自评通过（验收 actor ≠ acceptance.owner）
    p6 = os.path.join(d, "nc5")
    _mkroot(p6, [_it("I1", owner="ga-verifier")], [],
            [_ev("I1", "CLOSE_READY", "ACCEPTED", "ga-exec")])
    cases.append(("NC5 自评通过(actor!=owner)", p6, "J3"))
    # NC6 不可逆已推进但无放行记录
    p7 = os.path.join(d, "nc6")
    _mkroot(p7, [_it("I1", risk="irreversible")], [], [_ev("I1", "CLOSE_READY", "ACCEPTED", "v")])
    cases.append(("NC6 不可逆无放行", p7, "J4"))
    # NC7 item 记录状态滞后于事件（真实事故形态：记录停在 PENDING、事件已 ACCEPTED）
    p8 = os.path.join(d, "nc7")
    _mkroot(p8, [_it("I1", state="PENDING")], [], [_ev("I1", "CLOSE_READY", "ACCEPTED", "v")])
    cases.append(("NC7 item 记录滞后于事件", p8, "J1"))
    # 正控
    p5 = os.path.join(d, "pos")
    real = os.path.join(d, "real_evidence.txt"); open(real, "w").write("x")
    _mkroot(p5, [_it("I1", risk="irreversible")], [_es("E-C", True)],
            [_ev("I1", "CLOSE_READY", "ACCEPTED", "v")],
            approvals=[{"object_id": "I1", "decision": "approve", "scope": "irreversible"}],
            evidence=[{"evidence_id": "E1", "where": real}])
    cases.append(("POS 正控：全齐备应过", p5, None))
    os.makedirs(d, exist_ok=True)
    # 正控需要一个可行的 resume 对账：不传 ⇒ J1 会报「缺独立对账通路」
    # ★只排除 J1/resume 子项，勿整体屏蔽 J1（否则「item 态与事件不一致」判据恒不可测）
    for name, root, expect in cases:
        _, _, f, _ = audit(root, resume_json=None)
        codes = {x[0] for x in f if not (x[0] == "J1" and x[1] == "resume")}
        caught = (expect in codes) if expect else (not codes)
        res.append((name, caught, sorted(codes)))
        if verbose:
            print("  %-28s %s codes=%s" % (name, "CAUGHT" if caught else "MISSED", sorted(codes)))
    return sum(1 for _, c, _ in res if c), len(res), res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("audit"); a.add_argument("--root", required=True)
    a.add_argument("--resume-json"); a.add_argument("--json", action="store_true")
    a.add_argument("--no-evidence-check", action="store_true")
    sub.add_parser("negctl")
    sub.add_parser("selftest")
    p = sub.add_parser("proof"); p.add_argument("--root", action="append", required=True)
    args = ap.parse_args()
    if args.cmd == "negctl":
        n, tot, res = negctl()
        print("[negctl] %d/%d" % (n, tot))
        return 0 if n == tot else 1
    if args.cmd == "selftest":
        n, tot, res = negctl(verbose=False)
        print("[selftest] 负控 %d/%d" % (n, tot))
        ok = True
        for name, c, codes in res:
            print("  %-28s %s %s" % (name, "OK" if c else "FAIL", codes))
            ok = ok and c
        print("[selftest] %s" % ("全部通过" if ok else "有失败"))
        return 0 if ok else 1
    if args.cmd == "proof":
        rows = []
        for r in args.root:
            jp, recs = read_journal(r)
            h1 = hashlib.sha256(open(jp, "rb").read()).hexdigest()
            audit(r, resume_json=os.path.join(r, "..", "final_resume.json") if os.path.exists(
                os.path.join(r, "..", "final_resume.json")) else None)
            h2 = hashlib.sha256(open(jp, "rb").read()).hexdigest()
            rows.append((r, h1, h2, h1 == h2))
            print("%s\n  sha256(before)=%s\n  sha256(after )=%s  unchanged=%s"
                  % (r, h1[:16], h2[:16], h1 == h2))
        print("[proof] files=%d changed=%s" % (len(rows), [r for r, _, _, c in rows if not c]))
        return 0 if all(c for *_, c in rows) else 1
    if args.cmd != "audit":
        ap.print_help(); return 2
    jp, b, f, ok = audit(args.root, resume_json=args.resume_json,
                         evidence_check=not args.no_evidence_check)
    if args.json:
        print(json.dumps({"root": args.root, "journal": jp, "counts": b["counts"],
                          "findings": f, "ok": ok,
                          "goal_id": (b["goal"] or {}).get("goal_id")}, ensure_ascii=False, indent=1))
    else:
        print("[audit] root=%s journal=%s" % (args.root, jp))
        print("  records=%s" % b["counts"])
        for k, w, m in f:
            print("  [FINDING %s] %s：%s" % (k, w, m))
        for k, m in ok:
            print("  [OK %s] %s" % (k, m))
        print("  findings=%d ok=%d" % (len(f), len(ok)))
    return 0 if not f else 1


if __name__ == "__main__":
    sys.exit(main())
