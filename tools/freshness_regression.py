#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""freshness_regression.py —— task_freshness 巡检判据的**回归集 harness**（只读）。

背景（R480 / TODO L189，第 21 批替换项 C7'）
--------------------------------------------
R467 用 4 个已知坏窗口 + 1 个正控回放了三层产物判据（16 格 = HIT 8 / LAYER_BLIND 5 /
DISABLED_EXCUSED 2 / NO_DATA 1），但那份结论是**一次性**的：判据或阈值一改，只能人工重跑，
无从判断是否退化。本器把「已知坏/正窗口回归集」固化下来：

  改判据/阈值 → 跑本器 → 输出 漏报数 / 误报数 / 首次命中延迟 与**冻结基线**逐格比对，
  任何一项变差即 rc=1（准入闸门语义）。

判据层本身**零重写**：直接调用 tools/detector_replay.py 的 replay_case()，与 R467 同内核。
本器只负责：夹具加载 → 指标计算 → 基线比对 → 逐格打印 → 退出码。

指标定义（全部机械可复算，禁用「凭印象放行」）
----------------------------------------------
  fn_cases  硬漏报：标注 expect=alert 的窗口，四层全无命中且无豁免 ⇒ 整例漏报
  fn_cells  层漏报：基线 HIT 的 (用例 × 层) 格现在不命中（含被 --disable-layer 关掉者）
  fp_cases  误报：标注 expect=no_alert 的正控窗口出现任何命中
  delay_regs 延迟退化：用例的最早命中延迟（天）大于基线值，或基线有命中而现在全无
  coverage  覆盖度：命中格数 / 基线命中格数

子命令（位置参数，与仓内其它 tools 一致）
------------------------------------------
  run           跑一遍并打印逐格表（不做比对）
  check         跑一遍并与基线比对；退化 ⇒ rc=1（**准入闸门**）
  baseline      只打印冻结基线
  cases         只列出夹具用例清单
  selftest      自测（含负控：关层必致漏报上升、正控污染必报误报、基线篡改必报退化）
  proof         只读自证（跑前后对语料/夹具 sha256 一致）

选项
----
  --fixture PATH      夹具（默认 tools/freshness_regression_cases.json）
  --root PATH         仓库根（默认=本文件上一级）
  --disable-layer X   视为关闭该层判据（可重复；用于「改判据/关判据」的负控）
  --expect-fn-increase  要求本次 fn_cases+fn_cells 相对基线上升（负控专用；不升 ⇒ rc=1）
  --json OUT          结果落盘（JSON）
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIXTURE = os.path.join(HERE, "freshness_regression_cases.json")
DETECTOR = os.path.join(HERE, "detector_replay.py")
LAYERS = ["run_presence", "log_signature", "artifact_signature", "content_change"]


# ---------------------------------------------------------------- 基础工具
def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def load_fixture(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not isinstance(d.get("cases"), list) or not isinstance(d.get("baseline"), dict):
        raise ValueError("夹具结构不合法：须含 cases[] 与 baseline{}")
    ids = [c.get("id") for c in d["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("夹具用例 id 重复：%s" % sorted({i for i in ids if ids.count(i) > 1}))
    if not d["baseline"]:
        raise ValueError("夹具 baseline 为空：先用 --write-baseline 由真跑生成")
    for c in d["cases"]:
        if c.get("expect") not in ("alert", "no_alert"):
            raise ValueError("用例 %s 缺 expect∈{alert,no_alert}" % c.get("id"))
    return d


# ---------------------------------------------------------------- 指标内核
def case_metrics(result, baseline, disabled=(), layers=LAYERS):
    """单个用例 → 机械可复算的指标元组（纯函数，selftest 直接喂合成结果做负控）。"""
    active = [l for l in layers if l not in disabled]
    cells = result.get("layers") or {}
    hits = [l for l in active if cells.get(l, {}).get("verdict") == "HIT"]
    excused = [l for l in layers if cells.get(l, {}).get("verdict") == "DISABLED_EXCUSED"]
    delays = [cells[l].get("delay_days") for l in hits if cells[l].get("delay_days") is not None]
    delay_min = min(delays) if delays else None
    b = baseline or {}
    expect = b.get("expect") or result.get("expect")
    lost = []
    fn_case = False
    fp_case = False
    delay_reg = False
    if expect == "alert":
        if b:
            lost = [l for l in (b.get("hit_layers") or []) if l not in hits and l not in excused]
        if not hits and not excused:
            fn_case = True
        if b.get("delay_min_days") is not None:
            if delay_min is None or delay_min > b["delay_min_days"]:
                delay_reg = True
    elif expect == "no_alert":
        fp_case = bool(hits)
    return {
        "id": result.get("id"),
        "expect": expect,
        "hits": hits,
        "excused": excused,
        "delay_min_days": delay_min,
        "fn_case": fn_case,
        "fn_cells": lost,
        "fp_case": fp_case,
        "delay_reg": delay_reg,
    }


def evaluate(results, baseline, disabled=(), layers=LAYERS):
    per = [case_metrics(r, baseline.get(r.get("id")) or {}, disabled, layers) for r in results]
    base_cells = sum(len((baseline.get(p["id"]) or {}).get("hit_layers") or []) for p in per)
    hit_cells = sum(len(p["hits"]) for p in per)
    return {
        "per_case": per,
        "fn_cases": sum(1 for p in per if p["fn_case"]),
        "fn_cells": sum(len(p["fn_cells"]) for p in per),
        "fn_cell_list": [(p["id"], l) for p in per for l in p["fn_cells"]],
        "fp_cases": [p["id"] for p in per if p["fp_case"]],
        "delay_regs": [p["id"] for p in per if p["delay_reg"]],
        "hit_cells": hit_cells,
        "baseline_cells": base_cells,
        "coverage": (round(hit_cells / base_cells, 4) if base_cells else None),
        "disabled_layers": sorted(disabled),
    }


def regressions(metrics):
    """退化清单（空 = 逐格不差于基线）。"""
    out = []
    for p in metrics["per_case"]:
        if p["fn_case"]:
            out.append("%s: 硬漏报（整例无命中）" % p["id"])
        for l in p["fn_cells"]:
            out.append("%s: 层漏报 %s（基线命中、现在不命中）" % (p["id"], l))
        if p["fp_case"]:
            out.append("%s: 误报（正控窗口出现命中）" % p["id"])
        if p["delay_reg"]:
            out.append("%s: 延迟退化（最早命中 %s 天，基线见夹具）" % (p["id"], p["delay_min_days"]))
    return out


# ---------------------------------------------------------------- 运行
def run_cases(fx, root, disabled=()):
    det = load_module(DETECTOR, "detector_replay_for_regression")
    results = []
    for c in fx["cases"]:
        r = det.replay_case(c, root)
        r.setdefault("id", c.get("id"))
        results.append(r)
    return results


def print_table(results, baseline, metrics):
    print("%-26s %-9s %-22s %-9s %s" % ("case", "expect", "hits", "delay_min", "baseline_delay"))
    for r, p in zip(results, metrics["per_case"]):
        b = baseline.get(p["id"]) or {}
        print("%-26s %-9s %-22s %-9s %s" % (
            p["id"], p["expect"], ",".join(p["hits"]) or "-",
            "-" if p["delay_min_days"] is None else p["delay_min_days"],
            "-" if b.get("delay_min_days") is None else b["delay_min_days"]))
    print("\nfn_cases=%d  fn_cells=%d  fp_cases=%d  delay_regs=%d  覆盖 %s/%s%s" % (
        metrics["fn_cases"], metrics["fn_cells"], len(metrics["fp_cases"]), len(metrics["delay_regs"]),
        metrics["hit_cells"], metrics["baseline_cells"],
        "" if not metrics["disabled_layers"] else "  [已关层: %s]" % ",".join(metrics["disabled_layers"])))


def print_baseline(fx):
    print("%-26s %-9s %-22s %s" % ("case", "expect", "hit_layers", "delay_min_days"))
    for c in fx["cases"]:
        b = fx["baseline"].get(c["id"]) or {}
        print("%-26s %-9s %-22s %s" % (c["id"], b.get("expect") or c.get("expect"),
                                       ",".join(b.get("hit_layers") or []) or "-",
                                       b.get("delay_min_days")))


def write_baseline(fx, path, results):
    fx = dict(fx)
    fx["baseline"] = {
        r["id"]: {
            "expect": r.get("expect"),
            "verdicts": {l: (r.get("layers") or {}).get(l, {}).get("verdict") for l in LAYERS},
            "hit_layers": [l for l in LAYERS if (r.get("layers") or {}).get(l, {}).get("verdict") == "HIT"],
            "delay_min_days": min([(r["layers"][l].get("delay_days") or 0) for l in LAYERS
                                   if (r.get("layers") or {}).get(l, {}).get("verdict") == "HIT"] or [None],
                                  key=lambda x: (x is None, x)),
        }
        for r in results
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fx, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return fx["baseline"]


# ---------------------------------------------------------------- 只读自证
def corpus(roots):
    out = []
    for r in roots:
        for dp, dn, fn in os.walk(r):
            dn[:] = [d for d in dn if d not in (".git", "__pycache__")]
            for f in fn:
                out.append(os.path.join(dp, f))
    return sorted(out)


def proof(root, fixture):
    targets = [fixture, DETECTOR,
               os.path.join(HERE, "alert_lifecycle.py"),
               os.path.join(root, "tools", "freshness_criteria.json"),
               os.path.join(root, "tools", "task_freshness.py")]
    before = {p: sha256_file(p) for p in targets if os.path.exists(p)}
    fx = load_fixture(fixture)
    run_cases(fx, root)
    after = {p: sha256_file(p) for p in before}
    changed = [p for p in before if before[p] != after[p]]
    print("proof: files=%d changed=%s" % (len(before), changed))
    return 0 if not changed else 1


# ---------------------------------------------------------------- selftest
def _syn(cid, verdicts, expect):
    return {"id": cid, "expect": expect, "layers": {l: {"verdict": v} for l, v in verdicts.items()}}


def selftest():
    ok = fail = 0
    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS %s" % name)
        else:
            fail += 1
            print("  FAIL %s %s" % (name, extra))

    fx = load_fixture(DEFAULT_FIXTURE)
    base = fx["baseline"]
    print("[1] 夹具结构 / ground truth 可回查")
    chk("用例数=6（4 坏窗口 + 2 正控）", len(fx["cases"]) == 6, len(fx["cases"]))
    n_bad = sum(1 for c in fx["cases"] if c["expect"] == "alert")
    n_pos = sum(1 for c in fx["cases"] if c["expect"] == "no_alert")
    chk("坏窗口 4 / 正控 2", (n_bad, n_pos) == (4, 2), (n_bad, n_pos))
    det = load_module(DETECTOR, "dr_st_1")
    known = {c["id"] for c in det.CASES}
    chk("夹具用例 id 与 detector_replay.CASES 同源（不重造 ground truth）",
        {c["id"] for c in fx["cases"]} >= known, sorted(known))
    for c in fx["cases"]:
        chk("用例 %s 有 gt_source 与窗口" % c["id"], bool(c.get("gt_source")) and bool(c.get("window_start")))

    print("[2] 正面：标准配置 check 无退化（基线可复现）")
    results = run_cases(fx, det.ROOT_DEFAULT)
    m = evaluate(results, base)
    chk("fn_cases=0", m["fn_cases"] == 0, m["fn_cases"])
    chk("fn_cells=0（逐格不差于 R467 基线）", m["fn_cells"] == 0, m["fn_cell_list"])
    chk("fp_cases=0（正控未被误报）", not m["fp_cases"], m["fp_cases"])
    chk("delay_regs=0（首次命中延迟不退化）", not m["delay_regs"], m["delay_regs"])
    chk("coverage=1.0", m["coverage"] == 1.0, m["coverage"])

    print("[3] 负控 NC1：关掉 artifact_signature ⇒ fn_cells 必上升（漏报可见）")
    m1 = evaluate(results, base, disabled=("artifact_signature",))
    chk("fn_cells>0", m1["fn_cells"] > 0, m1["fn_cells"])
    exp_lost = {cid for cid, b in base.items() if "artifact_signature" in (b.get("hit_layers") or [])}
    chk("漏报格恰为基线 artifact 命中格", {c for c, _ in m1["fn_cell_list"]} == exp_lost,
        (sorted({c for c, _ in m1["fn_cell_list"]}), sorted(exp_lost)))

    print("[4] 负控 NC2：关掉全部层 ⇒ 4 个坏窗口全部硬漏报")
    m2 = evaluate(results, base, disabled=tuple(LAYERS))
    chk("fn_cases=4", m2["fn_cases"] == 4, m2["fn_cases"])
    chk("正控仍不误报", not m2["fp_cases"], m2["fp_cases"])

    print("[5] 负控 NC3：合成结果注入（指标内核级）")
    synth = [
        _syn("bad_a", {"run_presence": "HIT"}, "alert"),
        _syn("bad_b", {"run_presence": "MISS"}, "alert"),
        _syn("pos_a", {"run_presence": "HIT"}, "no_alert"),
    ]
    bsyn = {"bad_a": {"expect": "alert", "hit_layers": ["run_presence", "log_signature"], "delay_min_days": 0},
            "bad_b": {"expect": "alert", "hit_layers": ["run_presence"], "delay_min_days": 0},
            "pos_a": {"expect": "no_alert", "hit_layers": [], "delay_min_days": None}}
    ms = evaluate(synth, bsyn)
    chk("NC3a 坏窗口全 MISS ⇒ fn_cases=1", ms["fn_cases"] == 1, ms["fn_cases"])
    # 合成集里 bad_a 丢了 log_signature、bad_b 由 HIT 转 MISS ⇒ 两格都要被点名（只报一格 = 漏检）
    chk("NC3b 丢两层 ⇒ fn_cells=2 且逐格点名",
        sorted(ms["fn_cell_list"]) == [("bad_a", "log_signature"), ("bad_b", "run_presence")],
        ms["fn_cell_list"])
    chk("NC3c 正控命中 ⇒ fp_cases=[pos_a]", ms["fp_cases"] == ["pos_a"], ms["fp_cases"])
    synth_delay = [_syn("bad_a", {"run_presence": "HIT"}, "alert")]
    synth_delay[0]["layers"]["run_presence"]["delay_days"] = 9
    md = evaluate(synth_delay, bsyn)
    chk("NC3d 延迟 9>0 ⇒ delay_regs=1", md["delay_regs"] == ["bad_a"], md["delay_regs"])
    chk("NC3e regressions() 对上述退化逐条出清单", len(regressions(md)) == 2, regressions(md))

    print("[6] 负控 NC4：基线篡改 ⇒ check 必报退化 rc=1")
    bad_base = json.loads(json.dumps(base))
    bad_base["birdie_0808_0925"]["delay_min_days"] = -1          # 把基线最优延迟改好 ⇒ 现行命中显得变差
    mm = evaluate(results, bad_base)
    chk("NC4a 基线调优 ⇒ 至少一条延迟退化", bool(mm["delay_regs"]), mm["delay_regs"])
    bad_base2 = json.loads(json.dumps(base))
    bad_base2["haisou_0925"]["hit_layers"] = ["run_presence"]     # 基线多登记一个未命中层 ⇒ 现行必须报层漏报
    mm2 = evaluate(results, bad_base2)
    chk("NC4b 基线多登记命中层 ⇒ fn_cells≥1", mm2["fn_cells"] >= 1, mm2["fn_cell_list"])

    print("[7] 幂等 / 只读")
    m_again = evaluate(run_cases(load_fixture(DEFAULT_FIXTURE), det.ROOT_DEFAULT), base)
    chk("两次独立跑 eval 结果逐字相同", json.dumps(m, sort_keys=True) == json.dumps(m_again, sort_keys=True))
    rc = proof(det.ROOT_DEFAULT, DEFAULT_FIXTURE)
    chk("proof rc=0（跑前后 sha256 无变化）", rc == 0, rc)

    print("\nselftest: %d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="task_freshness 判据回归集 harness（只读）")
    ap.add_argument("cmd", nargs="?", default="run",
                    choices=["run", "check", "baseline", "cases", "selftest", "proof", "write-baseline"])
    ap.add_argument("--fixture", default=DEFAULT_FIXTURE)
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--disable-layer", action="append", default=[])
    ap.add_argument("--expect-fn-increase", action="store_true")
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "proof":
        return proof(a.root, a.fixture)
    fx = load_fixture(a.fixture)
    if a.cmd == "cases":
        for c in fx["cases"]:
            print("%-26s %-9s %s..%s  %s" % (c["id"], c["expect"], c["window_start"], c["window_end"],
                                             (c.get("gt_source") or "")[:60]))
        return 0
    if a.cmd == "baseline":
        print_baseline(fx)
        return 0
    bad = [l for l in a.disable_layer if l not in LAYERS]
    if bad:
        print("未知层：%s（可用：%s）" % (bad, ",".join(LAYERS)), file=sys.stderr)
        return 2
    results = run_cases(fx, a.root)
    if a.cmd == "write-baseline":
        base = write_baseline(fx, a.fixture, results)
        print("baseline 已写入 %s（%d 例）" % (a.fixture, len(base)))
        return 0
    metrics = evaluate(results, fx["baseline"], disabled=tuple(a.disable_layer))
    print_table(results, fx["baseline"], metrics)
    regs = regressions(metrics)
    if a.cmd == "run":
        if regs:
            print("退化提示（run 不判成败）：")
            for r in regs:
                print("  - %s" % r)
        return 0
    # check
    rc = 0
    print("")
    if a.expect_fn_increase:
        fn_total = metrics["fn_cases"] + metrics["fn_cells"]
        print("--expect-fn-increase: fn_cases+fn_cells = %d（要求 >0）" % fn_total)
        rc = 0 if fn_total > 0 else 1
        if rc:
            print("FAIL：关层/改判据后漏报未上升 ⇒ 说明该层判据对坏窗口不敏感（回归集失效）")
    elif regs:
        print("REGRESSION: %d 项" % len(regs))
        for r in regs:
            print("  - %s" % r)
        rc = 1
    else:
        print("OK: 逐格不差于基线（fn_cases=%d fn_cells=%d fp_cases=%d delay_regs=%d 覆盖=%s）" % (
            metrics["fn_cases"], metrics["fn_cells"], len(metrics["fp_cases"]),
            len(metrics["delay_regs"]), metrics["coverage"]))
    if a.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.json_out)), exist_ok=True)
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump({"metrics": metrics, "regressions": regs, "root": a.root}, f,
                      ensure_ascii=False, indent=2)
        print("json -> %s" % a.json_out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
