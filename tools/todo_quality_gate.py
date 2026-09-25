#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo_quality_gate.py — 规划候选「结构性」质量闸门（只读，纯 stdlib）。

背景（第13批 C4）：历史报告把三个规划闸门工具记为已交付，其中
`todo_quality_gate.py` 在本机全盘零命中 = 真 ghost（`tools/dedup_guard.py
--prove-ghost` 给出 find + git log --all 双证据）。本器复活它，只做一件事：
在候选**写出来的当下**做结构 lint，把两类已知低价值模式在源头拦掉——
  P1_元分析无消费者（13 条）→ 缺「谁在什么场景消费它」
  P4_近重复/空转（13 条）  → 无可机械证伪的验收（写不出判据=做完也无法判真假）

不做的事（重要，勿当评审器用）：
  * 不排序、不打分、不替代 subagent 评审（评审看的是「值不值得做」，
    本器只看「写没写清消费者与验收」）。
  * 不判断候选是否创新/是否重复（那是 dedup_guard 的活）。
  * 不改任何文件：只读 + 打印，rc 仅表示「有没有结构上必拒收的条目」。

判据（全部机械、可复现、按原文打印命中证据）：
  A 验收判据数 mech >= 2：正则命中「rc=0 / selftest / 负控 / sha1 / grep /
    git 子命令 / SQL / 数字+单位(行|字节|B|条|%|天|次|秒|ms) / --flag / 可复跑」
    等可机械证伪信号；`①…②…` 编号只作辅助。
  B 消费者信号：强信号（消费者/消费/被引用/下游/下一次/下一批/未来/不批后果/
    不做会怎样/复盘）或 弱信号（报告/记忆/SOP/接线/纳入）+ 产物落点（tools/、
    memory/、sche_tasks/、LaunchAgents 等路径样式）。
  C 空话判据：若全文只出现「提升/优化/完善/增强/关注/梳理」类动词而无 A 类信号
    ⇒ 直接拒收（VAGUE_ONLY）。
用法：
  python3 tools/todo_quality_gate.py --candidates temp/plan13/prompt.txt
  python3 tools/todo_quality_gate.py --candidates F --labels "kept=C1,C2,C3,C4|dropped=C5,C6"
  python3 tools/todo_quality_gate.py --selftest
  python3 tools/todo_quality_gate.py --prove-ghost      # 复核「历史上曾缺失」
退出码：0=无必拒收；1=有 REJECT_*；2=用法/输入错误。
"""
import argparse, hashlib, json, os, re, sys

MECH_PATS = [
    (r"\brc\s*[=: ]\s*-?\d", "rc="),
    (r"selftest", "selftest"),
    (r"负控", "负控"),
    (r"sha1|sha256", "sha"),
    (r"\bgit\s+(clone|log|status|rev-parse|show|grep)", "git"),
    (r"\bgrep\b|SQL|sqlite|\bfind\b", "grep/sql/find"),
    (r"file:[^ \n]*mode=ro", "ro-uri"),
    (r"\d+(?:\.\d+)?\s*(?:行|字节|条|%|天|次|秒|ms|B)\b", "数字+单位"),
    (r"--[a-z][a-z0-9-]{1,}", "--flag"),
    (r"可复跑|可复现|可复算|打印自证|自证", "自证"),
    (r"①|②|③|④|⑤|⑥", "编号判据"),  # 辅助项：计数时剔除（见 mech_hits）
]
STRONG_CONSUMER = ["消费者", "消费", "被引用", "下游", "下一次", "下一批", "未来",
                   "不批后果", "不做会怎样", "复盘", "被谁", "谁在用", "谁在什么"]
WEAK_CONSUMER = ["报告", "记忆", "SOP", "接线", "纳入", "归档"]
ARTIFACT_PATH = [r"\btools/[A-Za-z0-9_./-]+", r"\bmemory/[A-Za-z0-9_./-]+",
                 r"\bsche_tasks/[A-Za-z0-9_./-]+", r"\breflect_scripts/[A-Za-z0-9_./-]+",
                 r"LaunchAgents", r"\btemp/[A-Za-z0-9_./-]+"]
VAGUE = ["提升", "优化", "完善", "增强", "关注", "梳理", "加强", "改进"]
CAND_RE = re.compile(r"^\s*(C\d+)\b[\s:：]*(.*)$")


def hard_tokens(s):
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,}", s))


def parse_candidates(text):
    """候选块 = 以 C<n> 开头、到下一个 C<n> 或文档尾。返回 [(id, body)]"""
    lines = text.splitlines()
    out, cur, buf = [], None, []
    for ln in lines:
        m = CAND_RE.match(ln)
        nxt = ln[m.end(1):m.end(1) + 1] if m else ""
        if m and nxt not in (":", "："):
            # 真候选 = 「C<n> 【类型】…」；`C1: <1-10 整数> …` 属输出格式模板行，跳过
            if cur:
                out.append((cur, "\n".join(buf).strip()))
            cur, buf = m.group(1), [m.group(2)]
        elif cur is not None:
            if ln.startswith("## "):  # 进入下一章节即终止
                out.append((cur, "\n".join(buf).strip()))
                cur, buf = None, []
            else:
                buf.append(ln)
    if cur:
        out.append((cur, "\n".join(buf).strip()))
    return out


AUX_ONLY = {"编号判据"}


def mech_hits(body):
    """返回 (计分命中, 辅助命中)。编号判据只作辅助——它天生出现在任何验收段里，
    计入会把「只写了①②③但无可机械证伪内容」的候选误放行。"""
    hits, aux = [], []
    for pat, name in MECH_PATS:
        if re.search(pat, body):
            (aux if name in AUX_ONLY else hits).append(name)
    return hits, aux


def consumer_hits(body):
    strong = [w for w in STRONG_CONSUMER if w in body]
    weak = [w for w in WEAK_CONSUMER if w in body]
    paths = []
    for p in ARTIFACT_PATH:
        paths += re.findall(p, body)
    return strong, weak, sorted(set(paths))


def verdict_of(cid, body, min_mech=2, consumer_mode="reject"):
    mech, aux = mech_hits(body)
    strong, weak, paths = consumer_hits(body)
    vague = [w for w in VAGUE if w in body]
    notes = []
    if not mech and vague:
        v = "REJECT_VAGUE_ONLY"
    elif len(mech) < min_mech:
        v = "REJECT_WEAK_ACCEPTANCE"
    elif not strong and not (weak and paths):
        v = "REJECT_NO_CONSUMER" if consumer_mode == "reject" else "WARN_NO_CONSUMER"
    else:
        v = "PASS"
        if len(mech) < 3:
            notes.append("mech<3")
        if not strong:
            notes.append("consumer=弱信号+落点")
    if re.search(r"不得|禁", body):
        notes.append("含禁止项")
    return {"id": cid, "verdict": v, "mech": mech, "aux": aux, "consumer_strong": strong,
            "consumer_weak": weak, "artifacts": paths, "vague": vague,
            "chars": len(body), "tokens": len(hard_tokens(body)), "notes": notes}


def run(cands, min_mech=2, consumer_mode="reject"):
    return [verdict_of(cid, body, min_mech, consumer_mode) for cid, body in cands]


def fmt(r):
    line = f"[{r['id']}] {r['verdict']}  chars={r['chars']} tokens={r['tokens']} mech={len(r['mech'])}"
    if r["notes"]:
        line += " | " + ",".join(r["notes"])
    line += "\n      mech命中: " + (",".join(r["mech"]) or "无")
    if r.get("aux"):
        line += f"  (辅助:{','.join(r['aux'])})"
    line += "\n      消费者: " + (",".join(r["consumer_strong"]) or "无强信号")
    if r["consumer_weak"]:
        line += " / 弱:" + ",".join(r["consumer_weak"])
    if r["artifacts"]:
        line += " / 落点:" + ",".join(r["artifacts"][:4])
    if r["vague"]:
        line += "\n      空话词: " + ",".join(r["vague"])
    return line


def prove_ghost():
    """复跑 C4 验收①：find + git log --all 双证据证「历史上真缺失」。"""
    import subprocess
    names = ["planning_gate.py", "dedup_guard.py", "todo_quality_gate.py"]
    big = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    ok = 0
    for n in names:
        hits = [p for p in subprocess.run(["find", big, "-name", n, "-not", "-path", "*/.git/*"],
                                          capture_output=True, text=True).stdout.split() if p.endswith(n)]
        added = [l for l in subprocess.run(["git", "log", "--all", "--diff-filter=A", "--name-only",
                                            "--pretty=format:%h", "--", n, f"*/{n}"],
                                           capture_output=True, text=True, cwd=big).stdout.split() if l]
        verdict = "GHOST(本器除外)" if not added else "曾存在"
        if n == "todo_quality_gate.py" and os.path.exists(os.path.join(big, "tools", n)):
            # 本器自身是唯一命中；历史上从未有提交新增该路径
            verdict = "GHOST(历史零提交，本器为复活)" if len(hits) <= 1 else "多处存在"
        print(f"  {n}: 磁盘命中={len(hits)} {hits} | git新增提交={added or '无'} -> {verdict}")
        ok += 1 if (not added) else 0
    print(f"# 双证据：{ok}/{len(names)} 个工具历史零提交（find 只命中本次复活副本）")
    return 0


def selftest():
    fails = []
    def chk(name, cond, extra=""):
        print(("  PASS " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    good = ("C1 【工程】落点 tools/x.py 跑通\n验收：①`python3 tools/x.py --selftest` 12/12 含 2 负控 "
            "②rc=0 ③输出 sha256 自证只读 ④被下一批规划读取（消费者=规划模式）")
    r = verdict_of("C1", good)
    chk("NC1 完整候选必 PASS", r["verdict"] == "PASS", f"verdict={r['verdict']}")

    noacc = "C2 【分析】做个分析，需要时看看\n有消费者=报告会被引用，未来复盘用。"
    r = verdict_of("C2", noacc)
    chk("NC2 无机械验收必拒收", r["verdict"] == "REJECT_WEAK_ACCEPTANCE", f"verdict={r['verdict']}")

    vague = "C3 【产出】提升报告质量，优化协作流程，完善记忆，关注长期收益。"
    r = verdict_of("C3", vague)
    chk("NC3 纯空话必拒收", r["verdict"] == "REJECT_VAGUE_ONLY", f"verdict={r['verdict']}")

    nocons = ("C4 【能力】写个工具 tools/y.py\n验收：①rc=0 ②selftest 含负控 ③打印 --json")
    r = verdict_of("C4", nocons)
    chk("NC4 缺消费者必拒收", r["verdict"] == "REJECT_NO_CONSUMER", f"verdict={r['verdict']}")

    chk("NC5 空输入不得静默通过", [r["verdict"] for r in run([])] == [] and len(run([])) == 0,
        f"candidates={len(run([]))}")

    rs = run([("C1", good), ("C2", noacc), ("C3", vague), ("C4", nocons)])
    chk("NC6 计数自证 4 条 3 类拒收", len(rs) == 4 and sum(1 for x in rs if x["verdict"].startswith("REJECT")) == 3,
        f"n={len(rs)} reject={sum(1 for x in rs if x['verdict'].startswith('REJECT'))}")
    print(f"selftest {6 - len(fails)}/6")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates")
    ap.add_argument("--labels", help='如 "kept=C1,C2,C3,C4|dropped=C5,C6"')
    ap.add_argument("--min-mech", type=int, default=2)
    ap.add_argument("--consumer-mode", choices=["reject", "warn"], default="reject",
                    help="reject=缺消费者即拒收（C4 验收要求）；warn=只告警（旧格式候选文本实测会全量假拒收）")
    ap.add_argument("--json")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--prove-ghost", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.prove_ghost:
        sys.exit(prove_ghost())
    if not a.candidates or not os.path.exists(a.candidates):
        print("--candidates 必须给存在的文件"); sys.exit(2)

    before = {p: hashlib.sha256(open(p, "rb").read()).hexdigest()
              for p in [os.path.join("tools", f) for f in os.listdir("tools") if f.endswith(".py")]}
    text = open(a.candidates, encoding="utf-8").read()
    cands = parse_candidates(text)
    print(f"# todo_quality_gate  candidates={len(cands)} (源 {a.candidates})  min_mech={a.min_mech}")
    if not cands:
        print("# 语料计数自证：0 条候选 ⇒ 不作为「全部通过」的结论，退出码 2"); sys.exit(2)
    rs = run(cands, a.min_mech, a.consumer_mode)
    for x in rs:
        print(fmt(x))
    rej = [x["id"] for x in rs if x["verdict"].startswith("REJECT")]
    print(f"\n# 统计 {json.dumps({'candidates': len(rs), 'pass': len(rs) - len(rej), 'reject': len(rej), 'rejected_ids': rej}, ensure_ascii=False)}")

    if a.labels:
        groups = dict()
        for part in a.labels.split("|"):
            k, v = part.split("=")
            groups[k.strip()] = {s.strip() for s in v.split(",") if s.strip()}
        vmap = {x["id"]: x["verdict"] for x in rs}
        print("\n# 标定（与历史评审结论对照；本轮 labels 来自规划批次决议）")
        for g, ids in groups.items():
            ids = {i for i in ids if i in vmap}
            p = sum(1 for i in ids if vmap[i] == "PASS")
            print(f"  {g}: n={len(ids)} PASS={p} REJECT={len(ids)-p}  ids={sorted(ids)}")
    after = {p: hashlib.sha256(open(p, "rb").read()).hexdigest()
             for p in [os.path.join("tools", f) for f in os.listdir("tools") if f.endswith(".py")]}
    print(f"# 只读自证 tools/*.py sha256 {'UNCHANGED' if before == after else 'CHANGED!!'}")
    if a.json:
        json.dump(rs, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"# json -> {a.json}")
    sys.exit(1 if rej else 0)


if __name__ == "__main__":
    main()
