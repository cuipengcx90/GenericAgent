#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""roi_task_class —— ROI 口径「任务分层」单一源加载器 (schema roi_task_class/v1)。

背景（2026-09-29 R497 / TODO L199）：
  心跳型任务（无事件也照跑、报告短：folder_watcher/price_alert/...）历史占 done/ 的
  73.5%–87%，其归档（09-21/22）令**全量口径**跨期不可比；而 temp 侧 v2 原型把心跳
  集合**硬编码在脚本里**（HEARTBEAT = {...}），违反「判定单一源」。本模块把该判定
  收敛到 `tools/roi_task_class.json`，任何消费方只准通过本模块取层，禁再散落文件名。

子命令（位置参数，兼容 --selftest 写法）：
  classify <name> [<name> ...]   打印每个 done/ 任务名所属层
  names [--layer heartbeat|business]   打印登记名
  audit [--registry P] [--done-dir P]  注册表自洽 + 与 done/ 全集对账（rc=0/1）
  selftest                       自测（含负控），打印 PASS/FAIL 统计
  proof                          只读自证：跑前后输入文件 sha256 一致

口径常量：
  HEARTBEAT_RULE = per_active_day >= 1.0 且 median_lines <= 25 且 note 非空（人工理由）
  unknown_policy = 未登记任务名 => UNCLASSIFIED（fail-closed，业务口径侧必须显式列出）
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GA_ROOT = os.path.dirname(HERE)
DEFAULT_REGISTRY = os.path.join(HERE, "roi_task_class.json")
DEFAULT_DONE_DIR = os.path.join(GA_ROOT, "sche_tasks", "done")

HB_MIN_PER_ACTIVE_DAY = 1.0
HB_MAX_MEDIAN_LINES = 25.0
LAYERS = ("heartbeat", "business")
UNCLASSIFIED = "UNCLASSIFIED"
DONE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+)\.md$")


# ---------------------------------------------------------------- registry ----
def load_registry(path=None):
    p = path or DEFAULT_REGISTRY
    with open(p, encoding="utf-8") as fh:
        reg = json.load(fh)
    if reg.get("schema") != "roi_task_class/v1":
        raise ValueError("unsupported registry schema: %r" % reg.get("schema"))
    if not isinstance(reg.get("tasks"), dict) or not reg["tasks"]:
        raise ValueError("registry has no tasks mapping")
    return reg


def classify(name, reg=None):
    """返回 'heartbeat' | 'business' | 'UNCLASSIFIED'。未登记一律 fail-closed。"""
    reg = reg if reg is not None else load_registry()
    ent = reg["tasks"].get(name)
    if not ent:
        return UNCLASSIFIED
    layer = ent.get("layer")
    return layer if layer in LAYERS else UNCLASSIFIED


def heartbeat_names(reg=None):
    reg = reg if reg is not None else load_registry()
    return sorted(n for n, e in reg["tasks"].items() if e.get("layer") == "heartbeat")


def done_task_names(done_dir=None):
    d = done_dir or DEFAULT_DONE_DIR
    names = set()
    if not os.path.isdir(d):
        return names
    for f in os.listdir(d):
        m = DONE_RE.match(f)
        if m:
            names.add(m.group(3))
    return names


# ------------------------------------------------------------------- audit ----
def audit(reg=None, done_dir=None):
    """返回 (problems, infos)；problems 非空 => rc=1。"""
    reg = reg if reg is not None else load_registry()
    problems, infos = [], []
    tasks = reg["tasks"]

    for name, ent in sorted(tasks.items()):
        layer = ent.get("layer")
        if layer not in LAYERS:
            problems.append("BAD_LAYER %s layer=%r" % (name, layer))
            continue
        if layer == "heartbeat":
            per, med = ent.get("per_active_day"), ent.get("median_lines")
            if per is None or med is None:
                problems.append("HB_NO_EVIDENCE %s 缺 per_active_day/median_lines" % name)
            elif not (per >= HB_MIN_PER_ACTIVE_DAY and med <= HB_MAX_MEDIAN_LINES):
                problems.append(
                    "HB_RULE_VIOLATED %s per_active_day=%s median_lines=%s（规则: >=%s 且 <=%s）"
                    % (name, per, med, HB_MIN_PER_ACTIVE_DAY, HB_MAX_MEDIAN_LINES))
            if not str(ent.get("note") or "").strip():
                problems.append("HB_NO_REASON %s 心跳型必须写明「无事件也照跑」的人工理由" % name)
        if ent.get("status") == "retired" and not str(ent.get("retired_at") or "").strip():
            problems.append("RETIRED_NO_DATE %s status=retired 但缺 retired_at" % name)

    seen = done_task_names(done_dir)
    unreg = sorted(n for n in seen if n not in tasks)
    if unreg:
        problems.append("UNREGISTERED %d 个 done/ 任务名未登记（fail-closed）: %s"
                        % (len(unreg), ",".join(unreg[:12]) + ("..." if len(unreg) > 12 else "")))
    for name in sorted(tasks):
        if name not in seen:
            infos.append("NO_PRODUCE %s 注册表有该名但 done/ 内无报告（已退役或尚未产出）" % name)
    return problems, infos


# ----------------------------------------------------------------- selftest ----
def _mk_registry(tasks):
    return {"schema": "roi_task_class/v1", "tasks": tasks}


def _fixture_done(names, tmp):
    d = os.path.join(tmp, "done")
    os.makedirs(d, exist_ok=True)
    for nm in names:
        open(os.path.join(d, "2026-01-01_1100_%s.md" % nm), "w").close()
    return d


def selftest():
    ok = fail = 0
    results = []

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            results.append("PASS %s" % label)
        else:
            fail += 1
            results.append("FAIL %s" % label)

    # --- 真实注册表
    reg = load_registry()
    probs, infos = audit(reg)
    check("P1 真注册表 audit 无 problem", probs == [])
    check("P2 folder_watcher=heartbeat", classify("folder_watcher", reg) == "heartbeat")
    check("P3 github_trending=business", classify("github_trending", reg) == "business")
    check("P4 未知名=UNCLASSIFIED", classify("no_such_task_xyz", reg) == UNCLASSIFIED)
    check("P5 心跳型非空且含 price_alert", "price_alert" in heartbeat_names(reg))
    check("P6 注册表心跳项均满足规则",
          all(e["per_active_day"] >= HB_MIN_PER_ACTIVE_DAY and e["median_lines"] <= HB_MAX_MEDIAN_LINES
              for e in reg["tasks"].values() if e.get("layer") == "heartbeat"))

    with tempfile.TemporaryDirectory() as tmp:
        d_ok = _fixture_done(["alpha", "beta"], tmp)
        base = {"alpha": {"layer": "business", "status": "active"},
                "beta": {"layer": "business", "status": "active"}}
        check("NC1a 全覆盖 fixture 无 problem", audit(_mk_registry(base), d_ok)[0] == [])

        # NC1: 心跳项违反规则（频次不够）
        r = dict(base)
        r["hb1"] = {"layer": "heartbeat", "per_active_day": 0.5, "median_lines": 10,
                    "note": "x", "status": "active"}
        d2 = _fixture_done(["alpha", "beta", "hb1"], tmp)
        p2 = audit(_mk_registry(r), d2)[0]
        check("NC1 心跳频次不足必报 HB_RULE_VIOLATED",
              any(x.startswith("HB_RULE_VIOLATED hb1") for x in p2))

        # NC2: 心跳项行数过长（把业务型误标心跳）
        r2 = dict(base)
        r2["gt"] = {"layer": "heartbeat", "per_active_day": 3.0, "median_lines": 26,
                    "note": "x", "status": "active"}
        p2 = audit(_mk_registry(r2), d_ok)[0]
        check("NC2 业务型误标心跳必报（行数超阈）",
              any(x.startswith("HB_RULE_VIOLATED gt") for x in p2))

        # NC3: 心跳项缺人工理由
        r3 = dict(base)
        r3["hb2"] = {"layer": "heartbeat", "per_active_day": 2.0, "median_lines": 10,
                     "note": "  ", "status": "active"}
        p3 = audit(_mk_registry(r3), d_ok)[0]
        check("NC3 心跳缺理由必报 HB_NO_REASON", any(x.startswith("HB_NO_REASON hb2") for x in p3))

        # NC4: done/ 出现未登记任务名 => fail-closed
        d4 = _fixture_done(["alpha", "beta", "mystery"], tmp)
        p4 = audit(_mk_registry(base), d4)[0]
        check("NC4 未登记任务名必报 UNREGISTERED", any(x.startswith("UNREGISTERED") for x in p4))
        check("NC4b 未登记名 classify=UNCLASSIFIED", classify("mystery", _mk_registry(base)) == UNCLASSIFIED)

        # NC5: retired 缺日期
        r5 = dict(base)
        r5["old"] = {"layer": "heartbeat", "per_active_day": 5.0, "median_lines": 10,
                     "note": "已归档", "status": "retired"}
        p5 = audit(_mk_registry(r5), d_ok)[0]
        check("NC5 retired 缺 retired_at 必报", any(x.startswith("RETIRED_NO_DATE old") for x in p5))

        # NC6: schema 错必抛
        bad = os.path.join(tmp, "bad.json")
        json.dump({"schema": "nope", "tasks": {"a": {}}}, open(bad, "w"))
        try:
            load_registry(bad)
            check("NC6 schema 错必抛", False)
        except ValueError:
            check("NC6 schema 错必抛", True)

        # NC7: tasks 为空必抛
        empty = os.path.join(tmp, "empty.json")
        json.dump({"schema": "roi_task_class/v1", "tasks": {}}, open(empty, "w"))
        try:
            load_registry(empty)
            check("NC7 空 tasks 必抛", False)
        except ValueError:
            check("NC7 空 tasks 必抛", True)

    print("\n".join(results))
    print("selftest: %d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


# ------------------------------------------------------------------- proof ----
def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def proof():
    """只读自证：本模块运行不写任何输入文件。"""
    inputs = [DEFAULT_REGISTRY, os.path.abspath(__file__)]
    before = {p: _sha(p) for p in inputs if os.path.exists(p)}
    reg = load_registry()
    audit(reg)
    classify("price_alert", reg)
    after = {p: _sha(p) for p in inputs if os.path.exists(p)}
    changed = [p for p in before if before[p] != after.get(p)]
    for p in before:
        print("%s  %s" % (before[p][:16], os.path.relpath(p, GA_ROOT)))
    print("proof files=%d changed=%s" % (len(before), changed))
    return 0 if not changed else 1


# -------------------------------------------------------------------- main ----
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        argv[0] = "selftest"
    ap = argparse.ArgumentParser(prog="roi_task_class.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--done-dir", default=DEFAULT_DONE_DIR)

    sp = sub.add_parser("classify"); sp.add_argument("names", nargs="+")
    sn = sub.add_parser("names"); sn.add_argument("--layer", choices=LAYERS)
    sub.add_parser("audit"); sub.add_parser("selftest"); sub.add_parser("proof")

    if not argv:
        ap.print_help(); return 2
    args = ap.parse_args(argv)

    if args.cmd == "classify":
        reg = load_registry(args.registry)
        rc = 0
        for n in args.names:
            layer = classify(n, reg)
            if layer == UNCLASSIFIED:
                rc = 1
            print("%-32s %s" % (n, layer))
        return rc
    if args.cmd == "names":
        reg = load_registry(args.registry)
        for n in sorted(reg["tasks"]):
            if not args.layer or reg["tasks"][n].get("layer") == args.layer:
                print("%-32s %-9s %s" % (n, reg["tasks"][n].get("layer"),
                                         reg["tasks"][n].get("status")))
        return 0
    if args.cmd == "audit":
        reg = load_registry(args.registry)
        probs, infos = audit(reg, args.done_dir)
        for i in infos:
            print("INFO  " + i)
        for p in probs:
            print("PROBLEM " + p)
        print("audit: problems=%d infos=%d tasks=%d heartbeat=%d"
              % (len(probs), len(infos), len(reg["tasks"]), len(heartbeat_names(reg))))
        return 0 if not probs else 1
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "proof":
        return proof()
    ap.print_help(); return 2


if __name__ == "__main__":
    sys.exit(main())
