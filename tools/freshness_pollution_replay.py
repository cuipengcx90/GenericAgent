#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""freshness_pollution_replay —— task_freshness L2 污染信号端到端回放（只读，沙盒）

背景（R511）：tools/task_freshness.py 的 L2「静默降级签名」原为纯子串命中
（text.find(pat)），而运行期日志/报告正文会把关键词原样回显（R466 实测 26 条命中里
24 条是回声）⇒「报告里提到过失败」会被误当成「日志里有失败」。R511 给该层加了命中
上下文分级（事件本体 strong / 回声引用 echo），echo 只作 INFO 提示、不升 ALERT。

本器把这一层在**沙盒**里端到端回放：把夹具样本写成临时日志文件 → 走真实的
task_freshness.run() 判据链 → 比对人工标注的 ground truth。

子命令（**位置参数**，与 detector_replay 同风格）：
  cases             列出用例与期望（只读，零写入）
  run [--json F]    沙盒回放全部用例（写 temp/ 下沙盒 + 可选 JSON）
  check             回放 + 闸门判定：rc=0 全过 / rc=1 有违例 / rc=2 夹具不可用
  selftest          内置负控（关分类器⇒误报必现；样本改坏⇒违例必现；空用例⇒fail-closed；
                    echo_policy=keep⇒回到旧行为）——闸门必须可被证伪
  proof             只读自证（夹具/内核 sha256 前后一致，零写入）
  agree             与 tools/error_signal.py 独立对账（只读；验收③：真事件行不误杀）

闸门判据（check）：
  polluted 用例：verdict != ALERT（零误报）
  true_positive 用例：verdict == ALERT（零漏报）
  mixed 用例：既 ALERT 又保留回声计数（污染不掩盖真信号）
  control 用例：不 ALERT 且 events == 0（不假绿、不误报）
  + 每条另比对 events/echoes 的上下界（ground truth 为区间断言）

只读边界：只写 <GA>/temp/freshness_pollution_replay/**；绝不改 task_freshness.py、
绝不写判据台账、绝不联网。
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time

GA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KERNEL = os.path.join(GA, "tools", "task_freshness.py")
CASES = os.path.join(GA, "tools", "freshness_pollution_cases.json")
SANDBOX = os.path.join(GA, "temp", "freshness_pollution_replay")


def sha256_file(path, cap=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if cap and h.digest_size and os.path.getsize(path) > cap:
                break
    return h.hexdigest()


def load_kernel(path=KERNEL):
    spec = importlib.util.spec_from_file_location("tf_pollution_%d" % int(time.time() * 1000), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_cases(path=None):
    path = path or CASES
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _mkdone(root, target_id, ts=None):
    """沙盒内建一份新鲜 run_evidence（避免 L3 运行缺席干扰 L2 判定）。"""
    d = os.path.join(root, "done")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s_%s.md" % ((ts or datetime.datetime.now()).strftime("%Y-%m-%d_%H%M"), target_id))
    open(p, "w", encoding="utf-8").write("# done\n")
    return p


def replay_one(tf, case, sandbox, force_classify=None, force_keep=False):
    """把一个用例写进沙盒并跑真实判据链，返回实际观测量。

    force_classify: None=按生产默认；'strong'/'echo'=强制覆盖分类器（负控用）
    force_keep:     True=给扫描段塞 echo_policy=keep（回到旧行为，负控用）
    """
    root = os.path.join(sandbox, "case_" + case["id"])
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root)
    n_written = 0
    for rel, body in case["files"].items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        n_written += 1
    target = json.loads(json.dumps(case["target"]))          # deep copy，绝不改夹具
    _mkdone(root, target["id"])
    if force_keep:
        for sc in target.get("scans") or []:
            sc["echo_policy"] = "keep"
    crit = {"_source": "freshness_pollution_replay", "targets": [target], "channels": []}

    orig = tf.classify_hit_context
    if force_classify is not None:
        tf.classify_hit_context = (lambda text, idx, pattern="", _c=force_classify:
                                   (_c, "forced-by-negctl"))
    try:
        res = tf.run(crit, root, as_json=True, write_ledger=False, return_result=True)
    finally:
        tf.classify_hit_context = orig

    row = res["targets"][0]
    return {"id": case["id"], "kind": case["kind"], "label": case.get("label", ""),
            "files": n_written, "verdict": row["verdict"],
            "alert": row["verdict"] == "ALERT",
            "events": row.get("signature_events", 0),
            "echoes": row.get("signature_echoes", 0),
            "signatures": row.get("signatures", []),
            "checks": [c["text"] for c in row["checks"]]}


def judge(case, got):
    """把实际观测量与 ground truth 比对，返回违例说明列表（空=通过）。"""
    exp = case.get("expect") or {}
    bad = []
    if "alert" in exp and got["alert"] != bool(exp["alert"]):
        bad.append("alert 期望 %s 实测 %s" % (bool(exp["alert"]), got["alert"]))
    if "events_min" in exp and got["events"] < exp["events_min"]:
        bad.append("events 期望 ≥%s 实测 %s" % (exp["events_min"], got["events"]))
    if "events" in exp and got["events"] != exp["events"]:
        bad.append("events 期望 ==%s 实测 %s" % (exp["events"], got["events"]))
    if "echoes_min" in exp and got["echoes"] < exp["echoes_min"]:
        bad.append("echoes 期望 ≥%s 实测 %s" % (exp["echoes_min"], got["echoes"]))
    if "echoes" in exp and got["echoes"] != exp["echoes"]:
        bad.append("echoes 期望 ==%s 实测 %s" % (exp["echoes"], got["echoes"]))
    return bad


def replay_all(tf, cases, sandbox, force_classify=None, force_keep=False):
    out = []
    for case in cases:
        got = replay_one(tf, case, sandbox, force_classify=force_classify, force_keep=force_keep)
        got["problems"] = judge(case, got)
        got["expect"] = case.get("expect") or {}
        out.append(got)
    return out


def summarize(rows):
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    fp = sum(1 for r in rows if r["kind"] == "polluted" and r["alert"])
    fn = sum(1 for r in rows if r["kind"] == "true_positive" and not r["alert"])
    ctl = sum(1 for r in rows if r["kind"] == "control" and (r["alert"] or r["events"]))
    probs = sum(len(r["problems"]) for r in rows)
    return {"cases": len(rows), "by_kind": {k: len(v) for k, v in sorted(by_kind.items())},
            "false_positive": fp, "false_negative": fn, "control_violations": ctl,
            "problems": probs}


def run_cmd(as_json=None, force_classify=None, force_keep=False, sandbox=None):
    obj = load_cases()
    cases = obj["cases"]
    if not cases:
        print("[fail-closed] 夹具用例为空（0 条）⇒ 拒绝给出「全绿」结论")
        return 2
    sb = sandbox or os.path.join(SANDBOX, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(sb, exist_ok=True)
    tf = load_kernel()
    rows = replay_all(tf, cases, sb, force_classify=force_classify, force_keep=force_keep)
    sm = summarize(rows)
    print("== L2 污染信号端到端回放（沙盒 %s）" % os.path.relpath(sb, GA))
    print("   夹具：%s | 用例 %d（%s）" % (os.path.relpath(CASES, GA), sm["cases"],
                                          " ".join("%s=%d" % kv for kv in sm["by_kind"].items())))
    for r in rows:
        print("   [%s] %-16s %-40s verdict=%-6s events=%s echoes=%s" %
              ("FAIL" if r["problems"] else "PASS", r["id"], r["label"][:40],
               r["verdict"], r["events"], r["echoes"]))
        for p in r["problems"]:
            print("        └─ 违例：%s" % p)
    print("   —— 误报(polluted 却 ALERT)=%d 漏报(true_positive 未 ALERT)=%d 正控违例=%d 违例合计=%d" %
          (sm["false_positive"], sm["false_negative"], sm["control_violations"], sm["problems"]))
    if as_json:
        with open(as_json, "w", encoding="utf-8") as f:
            json.dump({"sandbox": sb, "summary": sm, "rows": rows}, f, ensure_ascii=False, indent=1)
        print("   JSON → %s" % as_json)
    return 1 if sm["problems"] else 0


# ------------------------------------------------------------------ selftest
def selftest(tmp=None):
    """内置负控：每一条都必须让闸门**转红**（能证伪），否则本器无判别力。"""
    checks = []

    def expect(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    obj = load_cases()
    cases = obj["cases"]
    base = tmp or os.path.join(GA, "temp", "freshness_pollution_selftest")
    if os.path.isdir(base):
        shutil.rmtree(base)
    os.makedirs(base)
    tf = load_kernel()

    # 正跑：闸门必须全绿
    rows = replay_all(tf, cases, os.path.join(base, "ok"))
    sm = summarize(rows)
    expect("① 正跑：闸门全绿（误报 0 / 漏报 0 / 正控 0）",
           sm["problems"] == 0 and sm["false_positive"] == 0 and sm["false_negative"] == 0,
           str(sm))

    # 覆盖面自证：三类用例都得真存在（否则「全绿」无意义）
    kinds = {c["kind"] for c in cases}
    expect("② 语料自证：polluted/true_positive/mixed/control 四类齐备",
           {"polluted", "true_positive", "mixed", "control"} <= kinds, str(sorted(kinds)))
    n_files = sum(len(c["files"]) for c in cases)
    expect("③ 语料自证：夹具声明文件数与沙盒实际写入数一致（%d）" % n_files,
           all(r["files"] == len(c["files"]) for r, c in zip(rows, cases)) and n_files > 0,
           str([r["files"] for r in rows]))

    # NC1：强制把所有命中判成「事件本体」⇒ 污染用例必误报（证明闸门抓得住假绿）
    bad = replay_all(tf, cases, os.path.join(base, "nc_force_strong"), force_classify="strong")
    sb = summarize(bad)
    expect("④ 负控：关掉分类器（全判事件本体）⇒ 误报数必 >0 且闸门判红",
           sb["false_positive"] > 0 and sb["problems"] > 0, str(sb))

    # NC2：关分类器给「回退开关」——echo_policy=keep ⇒ 回到旧行为，污染用例必 ALERT
    kept = replay_all(tf, cases, os.path.join(base, "nc_keep"), force_keep=True)
    sk = summarize(kept)
    expect("⑤ 负控：echo_policy=keep ⇒ 回到旧行为（污染用例必 ALERT，说明开关是唯一差异）",
           sk["false_positive"] > 0 and sk["problems"] > 0, str(sk))

    # NC3：样本改坏（把回声行改成真日志行却仍标 expect.alert=false）⇒ 违例必现
    broken = json.loads(json.dumps(cases))
    for c in broken:
        if c["id"] == "c1_markdown_list":
            c["files"] = {"logs/a.log": "2026-09-29 09:00:00 ERROR 站点故障 rc=1\n"}
    brow = replay_all(tf, broken, os.path.join(base, "nc_broken"))
    expect("⑥ 负控：把回声样本改成真事件行（期望仍为不告警）⇒ 必报违例",
           summarize(brow)["problems"] > 0, str(summarize(brow)))

    # NC4：空用例 ⇒ fail-closed（不得输出全绿）
    empty = os.path.join(base, "empty_cases.json")
    with open(empty, "w", encoding="utf-8") as f:
        json.dump({"schema": obj["schema"], "cases": []}, f)
    saved = CASES
    globals()["CASES"] = empty
    try:
        rc = run_cmd()
    finally:
        globals()["CASES"] = saved
    expect("⑦ 负控：夹具 0 条用例 ⇒ rc=2 fail-closed（不假绿）", rc == 2, "rc=%s" % rc)

    # NC5：强制判 echo ⇒ 真阳性用例必漏报（反方向证伪）
    allecho = replay_all(tf, cases, os.path.join(base, "nc_force_echo"), force_classify="echo")
    se = summarize(allecho)
    expect("⑧ 负控：全判回声 ⇒ 真阳性漏报数必 >0 且闸门判红",
           se["false_negative"] > 0 and se["problems"] > 0, str(se))

    ok = True
    for name, good, detail in checks:
        ok &= good
        print("   [%s] %s%s" % ("PASS" if good else "FAIL", name, ("  → %s" % detail) if not good else ""))
    print("负控/回归合计：%s（%d/%d）" % ("PASS" if ok else "FAIL",
                                        sum(1 for _, g, _ in checks if g), len(checks)))
    print("沙盒根：%s（可整目录删除，无副作用）" % os.path.relpath(base, GA))
    return 0 if ok else 1


def proof_cmd():
    """只读自证：夹具与内核 sha256 前后一致（本器不写这两处）。"""
    files = {p: sha256_file(p) for p in (KERNEL, CASES)}
    rc = run_cmd()
    after = {p: sha256_file(p) for p in (KERNEL, CASES)}
    changed = [os.path.relpath(p, GA) for p in files if files[p] != after[p]]
    print("proof files=%d changed=%s (kernel/fixture)" % (len(files), changed))
    return 0 if (not changed and rc in (0, 1)) else 1


def cases_cmd():
    obj = load_cases()
    print("夹具：%s（schema=%s，%d 条用例）" % (os.path.relpath(CASES, GA), obj.get("schema"),
                                               len(obj["cases"])))
    for c in obj["cases"]:
        exp = c.get("expect") or {}
        print("  %-16s %-13s alert=%-5s events=%-4s echoes=%-4s | %s" %
              (c["id"], c["kind"], exp.get("alert"), exp.get("events", exp.get("events_min")),
               exp.get("echoes", exp.get("echoes_min")), c.get("label", "")))
    return 0


def agree_cmd(as_json=None):
    """与 tools/error_signal.py（R466 错误信号内核）独立对账 —— 只读、零写入。

    判据（比「两器都判事件」更强且可证伪）：
      ① error_signal 的**事件块覆盖行**中、含判据 pattern 的行，本器必须判 strong
         （否则 = 本器把真事件误杀成回声，记 fn ⇒ rc=1）；
      ② 本器判 strong 却不在覆盖内的行，须有 error_signal possible(keyword_hit) 佐证
         ⇒ 记 known_delta（有据口径差异）；若两边都无感 ⇒ 记 fn（无据差异）。
    反向**不要求**逐条一致：error_signal 的锚点集更窄（级别/rc 要求**行首**），
    任务日志的 `TS LEVEL ... rc=N` 行按其口径只算 keyword_hit 不算事件；本器要的正是
    「判据表 pattern 命中且非回声」⇒ 两者口径不同，差异必须可解释、可回源。
    """
    sys.path.insert(0, os.path.join(GA, "tools"))
    import error_signal as es  # 复用 R466 内核，零重写
    tf = load_kernel()
    obj = load_cases()
    pat = obj.get("pattern_token")
    rows, fn, deltas = [], [], []
    for c in obj["cases"]:
        lines = []
        for _p, txt in (c.get("files") or {}).items():
            lines.extend(txt.splitlines())
        rep = es.analyze_lines(lines, log_name=c["id"])
        cover = set()
        for e in rep.get("events", []):
            st = e.get("line")
            bl = e.get("block_lines") or 1
            if st:
                cover.update(range(st, st + bl))
        pos = {e.get("line") for e in rep.get("possible", []) if e.get("line")}
        strong, echo = [], []
        for n, ln in enumerate(lines, 1):
            if pat and pat in ln:
                ctx, _why = tf.classify_hit_context(ln, ln.index(pat), pat)
                (strong if ctx == "strong" else echo).append(n)
        for n in strong:
            if n not in cover:
                rec = {"case": c["id"], "line": n, "text": lines[n - 1][:80]}
                if n in pos:
                    rec["es"] = "possible"
                    deltas.append(rec)
                else:
                    fn.append(rec)
        for n in sorted(cover):
            if pat and n <= len(lines) and pat in lines[n - 1] and n not in strong:
                fn.append({"case": c["id"], "line": n, "text": lines[n - 1][:80],
                           "why": "es 事件块覆盖行未被本器判 strong"})
        rows.append({"case": c["id"], "kind": c.get("kind"), "strong": strong, "echo": echo,
                     "es_cover": sorted(cover), "es_possible": sorted(pos)})
    out = {"schema": "r511.error_signal_agreement/v4",
           "criterion": "es 事件块覆盖行(含pattern) ⊆ 本器 strong；本器 strong 不在覆盖内须有 es possible 佐证",
           "n_cases": len(rows), "fn": fn, "known_deltas": deltas, "rows": rows}
    for r in rows:
        print("  %-17s %-13s strong=%-8s echo=%-8s es_cover=%-10s es_possible=%s" %
              (r["case"], r["kind"], r["strong"], r["echo"], r["es_cover"], r["es_possible"]))
    print("agree: n=%d fn=%d known_deltas=%d" % (len(rows), len(fn), len(deltas)))
    if as_json:
        with open(as_json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("agree JSON → %s" % as_json)
    return 1 if fn else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="task_freshness L2 污染信号端到端回放（只读沙盒）")
    ap.add_argument("cmd", choices=["cases", "run", "check", "selftest", "proof", "agree"])
    ap.add_argument("--json", default=None, help="run/check 时把结果 JSON 落盘")
    ap.add_argument("--sandbox", default=None, help="覆盖沙盒根（默认 temp/freshness_pollution_replay/<ts>）")
    a = ap.parse_args(argv)
    if a.cmd == "cases":
        return cases_cmd()
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "proof":
        return proof_cmd()
    if a.cmd == "agree":
        return agree_cmd(as_json=a.json)
    if a.cmd == "run":
        return run_cmd(as_json=a.json, sandbox=a.sandbox)
    # check：同一回放 + 明确闸门语义（rc 就是判决）
    rc = run_cmd(as_json=a.json, sandbox=a.sandbox)
    print("闸门：%s" % ("PASS" if rc == 0 else ("FAIL(有违例)" if rc == 1 else "FAIL(fail-closed)")))
    return rc


if __name__ == "__main__":
    sys.exit(main())
