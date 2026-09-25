#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo_claim_audit.py — 「TODO 声称完成项 vs 磁盘实物」对账器（R416；只读；fail-open）

任务来源：temp/TODO.txt 第 9 行 C2（第13批四路评审 8/10/7/9=8.50）。
背景：`[x]` 行常写「已落地 / 已接线 / 定时注册」而磁盘与注册面上并无对应物；history 量化里
P6_产物查无 = 42 条是全库最大单一低价值模式。本工具把「[x] 行的可判声明」逐条拉回磁盘与
launchd / sche_tasks 回读，输出 MISMATCH 明细（只告警、不改任何文件）。

★不重复建设（验收⑥，硬约束）：
  路径类判定内核 **100% 复用** tools/ref_integrity.py（R410）。本工具 **零行** token 正则与
  classify 实现——只用它的两个公开入口：
    · ri.build_index(no_index) → 复用 memory_assert_audit 的 basename 索引（BARE token 靠它）
    · ri.scan_text(label, text, index, skip, line_asserts) → 复用 report_assert_audit.classify()
本工具新增的只有四件：
  ① [x] 行台账解析 + 自证（行数 / [x] 条数 / 字节 / sha1）
  ② 「可判声明」筛选 = 反引号内路径/命令 + 接线语义词（落地/接线/定时/注册/常驻/launchd/每日…）
  ③ 注册面回读：~/Library/LaunchAgents/*.plist + sche_tasks/{,disabled,done}/*.json
     （reflect_scripts/*.py **只算「脚本在盘」，不算接线**——本机反射脚本各需专属 plist 才生效，
      该结论有 R392/R415 两次实测支撑）
  ④ 极性判定（声称存在 / 声称不存在）+ 免责声明降级 + 四类 MISMATCH

判定口径（唯一对外词表）：
  MISMATCH（硬）：
    PATH_GONE             反引号 token 解析到具体路径但不存在 = 声称存在却缺失
    PATH_PHANTOM_ABSENCE  token 存在、但同行称其「已不存在/已删/退役」 = 声称缺失却存在
    WIRING_NO_REGISTRATION 接线主语（reflect_scripts/<x>.py）称已接线/定时，但注册面 0 命中
                          且同行**无**免责声明
    TITLE_OVERSOLD        目标段（`[x] 类型 | 目标 | 验收` 的第 2 段）出现「落地/接线/定时」类
                          声称，而同行正文自承「未接线/待批/零新增定时」 = 标题级与正文自相矛盾
                          （★kc 反例即此类：L51「每日快照落地」+ 正文「定时注册仍是待批件」）
  SOFT（非硬告警，仍需人看）：
    WIRING_WITH_DISCLAIMER 接线声明 + 无注册点，但同行已明说未接线/待批（诚实声明）
    SCRIPT_ONLY            reflect_scripts 脚本在盘但注册面 0 命中（「脚本在盘≠已接线」）
  CONSISTENT：EXISTS / AMBIG / 负极性且确实不存在（如台账写「X 已不存在」且 X 真不存在）

只读保证：台账 + 注册面文件全部 sha1 前后比对，任何变化即 rc=3。
cwd：一律锚定 GA 根（内部 chdir），避免 R375 式「相对路径吃 cwd」漂移。

用法：
  python3 tools/todo_claim_audit.py                     # 默认审 temp/TODO.txt
  python3 tools/todo_claim_audit.py --todo <path>       # 指定台账
  python3 tools/todo_claim_audit.py --json /tmp/x.json  # 机器可读明细
  python3 tools/todo_claim_audit.py --soft              # 连 SOFT 明细一起打印
  python3 tools/todo_claim_audit.py --strict            # 有硬 MISMATCH → rc=1
  python3 tools/todo_claim_audit.py --selftest          # 负控自证（4 例）
rc 契约：默认 0（fail-open，只告警）；--strict 且有 MISMATCH → 1；写违规 → 3；负控失败 → 2。
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import ref_integrity as ri  # noqa: E402  ← 判定内核唯一来源（验收⑥）

DEFAULT_TODO = os.path.join(REPO, "temp/TODO.txt")
LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
REFLECT_DIR = os.path.join(REPO, "reflect_scripts")
SCHED_DIRS = ["sche_tasks", "sche_tasks/disabled", "sche_tasks/done"]

# 接线语义词：出现在 [x] 行里即认为该行在说「有某个自动机制」（WIRING 主语判据用）
WIRE_RE = re.compile(r"落地|接线|注册|定时|常驻|launchd|LaunchAgent|plist|反射器|每日|每天")
# 免责/自承语言（SOFT 降级用，宽口径）
DISC_RE = re.compile(r"未接线|待批|不自动落|零新增定时|未新增|未安装|未改仓库|留待批|仅出提案|"
                     r"未执行|不得自批|未落|未挂|未装|提案未")
# ★TITLE_OVERSOLD 专用窄口径（R416 实测：宽口径在 68 行台账上出 9 条中 7 条为假警报）
#   声称 = 目标段出现「落地/接线/注册」且**同时**出现机制名词；自承 = 非目标段（正文）出现接线否定
CLAIM_RE = re.compile(r"落地|接线|注册")
MECH_RE = re.compile(r"定时|每日|每天|快照|监控|常驻|循环|反射|plist|launchd")
# 目标段按分句切，**整句**含自承语（未/待批/勿）的句子先剔除，再判「是否在声称落地」。
# ★R416 实测：不剔句时 L7（完成记录里写「定时注册仍未接线（待批）」）会被误报，剔句后全台账只剩
#   两个 kc 反例（L33/L51），精度 2/2。注意「零新增定时」不剔（零不在自承集里）——L51 正是靠它保留声称。
CLAUSE_SPLIT_RE = re.compile(r"[；;。，,]")
CLAUSE_RETRACT_RE = re.compile(r"未接线|未注册|未新增|未落地|待批|勿")
RETRACT_RE = re.compile(r"未接线|未注册|待批|零新增定时|未新增定时|不自动落|未挂|未装|未新增")
# 负极性：仅当否定词**紧跟 token**（≤12 字符、允许括注符号）才算「声称其不存在」，
# 且后面不得是 ':'（那是引用 WARN 文案，如 `[WARN] compress_log.py 不存在: <repo>…`）
NEG_RE = re.compile(r"^[\s）)】」”\"'`（(]{0,3}(?:已不存在|不存在|已删除|已删|已退役|已移|查无|已失效|不再存在)(?!\s*[:：])")
# 跨仓声明：行内出现 GA 仓外的绝对路径 ⇒ BARE token 判 GONE 时应降级（本工具内核单根=GA）
EXTROOT_RE = re.compile(r"(?:/Users/[^\s`）)，。；]+|~/[^\s`）)，。；]+)")


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def load_registration():
    """注册面 = LaunchAgents plist + sche_tasks json（文本全量）。reflect 脚本另列。"""
    reg = {}
    for p in sorted(glob.glob(os.path.join(LAUNCH_AGENTS, "*.plist"))):
        try:
            reg["plist:" + os.path.basename(p)] = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    for sub in SCHED_DIRS:
        for p in sorted(glob.glob(os.path.join(REPO, sub, "*.json"))):
            try:
                reg["sched:" + os.path.relpath(p, REPO)] = open(
                    p, encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    scripts = sorted(os.path.basename(p) for p in glob.glob(os.path.join(REFLECT_DIR, "*.py")))
    return reg, scripts


def reg_hits(subject, reg):
    return [k for k, t in reg.items() if subject in t]


def title_segment(line):
    """"[x] 类型 | 目标 | 验收" → 目标段（第 2 段）；无分隔符则整行前 160 字。"""
    parts = re.split(r"[｜|]", line)
    if len(parts) >= 2:
        return parts[1]
    return line[:160]


def audit_line(lineno, line, index, reg, scripts, skip):
    """单条 [x] 行 → (mismatches[], soft[], refs[], declared_ok)"""
    mism, soft = [], []
    spans = re.findall(r"`([^`]+)`", line)
    bk_tokens = set()
    for s in spans:
        for t in re.split(r"\s+", s.strip()):
            bk_tokens.add(t.strip())
    raw = ri.scan_text(f"TODO:{lineno}", line, index, skip, [])
    ext_root = bool(EXTROOT_RE.search(line)) and not any(
        m.group(0).startswith(REPO) or m.group(0).startswith("./") or m.group(0).startswith("../")
        for m in EXTROOT_RE.finditer(line))
    refs = []
    for rec in raw["refs"]:
        # 只保留「落在反引号内」的 token（验收②：只抽可判声明）
        if not any(rec["tok"] in s for s in spans):
            continue
        end = line.find(rec["tok"])
        window = line[end + len(rec["tok"]):end + len(rec["tok"]) + 12] if end >= 0 else ""
        rec = dict(rec)
        rec["line"] = lineno
        rec["negated"] = bool(NEG_RE.match(window))
        refs.append(rec)
        if rec["verdict"] == "GONE" and not rec["negated"]:
            if ext_root and rec["kind"] == "BARE":
                soft.append({"kind": "OUTSIDE_ROOT", "line": lineno, "tok": rec["tok"],
                             "kind_path": rec["kind"],
                             "why": "BARE token 在 GA 树内查无，但同行声明锚在 GA 仓外"
                                    "（内核单根=GA，故降级为软提示，非硬失配）"})
            else:
                mism.append({"kind": "PATH_GONE", "line": lineno, "tok": rec["tok"],
                             "kind_path": rec["kind"], "resolved": rec["path"],
                             "why": "声称存在但磁盘无此物"})
        elif rec["verdict"] == "GONE" and rec["negated"]:
            soft.append({"kind": "ABSENCE_CONSISTENT", "line": lineno, "tok": rec["tok"],
                         "kind_path": rec["kind"], "resolved": rec["path"],
                         "why": "token 已不在盘上，但同行本就写着它「已不存在」=自洽声明，非失配"})
        elif rec["verdict"] in ("EXISTS", "AMBIG") and rec["negated"]:
            mism.append({"kind": "PATH_PHANTOM_ABSENCE", "line": lineno, "tok": rec["tok"],
                         "kind_path": rec["kind"], "resolved": rec["path"],
                         "why": "声称已不存在但磁盘有此物"})
    # 接线类：主语 = 落在 reflect_scripts 的脚本（已接线过才可能被注册面命中）
    subjects = []
    for rec in refs:
        p = str(rec["path"])
        base = os.path.basename(p)
        if "reflect_scripts/" in p or base in scripts:
            subjects.append(base)
    subjects = sorted(set(s for s in subjects if s.endswith(".py")))
    wired_notion = bool(WIRE_RE.search(line))
    disc = bool(DISC_RE.search(line))
    for s in subjects:
        stem = s[:-3]
        hits = [h for h in reg_hits(stem, reg)]
        if hits:
            continue
        if wired_notion and not disc:
            mism.append({"kind": "WIRING_NO_REGISTRATION", "line": lineno, "subject": s,
                         "why": "称已接线/定时但 LaunchAgents+sche_tasks 0 命中，且同行无免责声明",
                         "script_on_disk": os.path.basename(s) in scripts})
        else:
            soft.append({"kind": "WIRING_WITH_DISCLAIMER" if disc else "SCRIPT_ONLY",
                         "line": lineno, "subject": s,
                         "why": ("接线面 0 命中，但同行自承未接线/待批" if disc
                                 else "脚本在盘但无任何注册点（脚本在盘≠已接线）"),
                         "script_on_disk": os.path.basename(s) in scripts})
    if wired_notion and disc:
        tgt = title_segment(line)
        # 目标段分句，剔掉整句自承（如「定时注册仍未接线（待批，勿代批）」）
        tgt_clean = "".join(c for c in CLAUSE_SPLIT_RE.split(tgt)
                            if not CLAUSE_RETRACT_RE.search(c))
        body = "".join(re.split(r"[｜|]", line)[2:])
        if CLAIM_RE.search(tgt_clean) and MECH_RE.search(tgt_clean) and RETRACT_RE.search(body):
            mism.append({"kind": "TITLE_OVERSOLD", "line": lineno,
                         "title": tgt.strip()[:80],
                         "claim": sorted(set(CLAIM_RE.findall(tgt_clean))),
                         "mech": sorted(set(MECH_RE.findall(tgt_clean))),
                         "retract": sorted(set(RETRACT_RE.findall(body)))[:5],
                         "why": "目标段声称「落地/接线」某机制，而正文段自承未接线/待批（标题级与正文矛盾）"})
    return mism, soft, refs


def run(todo_path):
    t0 = time.time()
    index, index_stat = ri.build_index(False)          # ← 复用内核（验收⑥）
    reg, scripts = load_registration()
    text = open(todo_path, encoding="utf-8", errors="replace").read()
    lines = text.split("\n")
    pre_sha = {}
    for p in [todo_path] + [os.path.join(REPO, s) for s in []]:
        pre_sha[p] = sha1(p)
    reg_paths = []
    for k in reg:
        kind, name = k.split(":", 1)
        reg_paths.append(os.path.join(LAUNCH_AGENTS, name) if kind == "plist"
                         else os.path.join(REPO, name))
    for p in reg_paths:
        if os.path.isfile(p):
            pre_sha[p] = sha1(p)

    xlines = [(i + 1, l) for i, l in enumerate(lines) if l.startswith("[x]")]
    olines = [i + 1 for i, l in enumerate(lines) if l.startswith("[ ]")]
    skip = collections.Counter()
    mism, soft, allrefs = [], [], []
    per_line = {}
    for n, l in xlines:
        m, s, r = audit_line(n, l, index, reg, scripts, skip)
        mism += m
        soft += s
        allrefs += r
        per_line[n] = {"mismatch": m, "soft": s, "refs": r}

    post_sha = {p: sha1(p) for p in pre_sha}
    written = [p for p, s in pre_sha.items() if post_sha[p] != s]
    verdicts = collections.Counter(r["verdict"] for r in allrefs)
    out = {
        "schema": "ga.todo_claim_audit/v1",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "todo": os.path.relpath(todo_path, REPO),
        "ledger": {"lines": len(lines), "x_lines": len(xlines), "open_lines": len(olines),
                   "bytes": len(text.encode()), "sha1": pre_sha[todo_path][:16],
                   "x_line_numbers": [n for n, _ in xlines]},
        "corpus": {"registration_files": len(reg), "launch_agents": len(
            [k for k in reg if k.startswith("plist:")]), "sche_tasks": len(
            [k for k in reg if k.startswith("sched:")]),
            "reflect_scripts": len(scripts),
            "index": {"files": index_stat[0], "dirs": index_stat[1], "cap": index_stat[2]}},
        "claims": {"backtick_refs": len(allrefs), "by_verdict": dict(verdicts),
                   "declared_lines": len([n for n, v in per_line.items() if v["refs"]])},
        "mismatch": mism, "mismatch_by_kind": dict(collections.Counter(m["kind"] for m in mism)),
        "soft": soft, "soft_by_kind": dict(collections.Counter(m["kind"] for m in soft)),
        "skip": dict(skip), "write_violations": written,
        "readonly_checked": len(pre_sha), "secs": round(time.time() - t0, 1),
        "per_line": per_line,
    }
    return out


def report_text(out):
    L = out["ledger"]
    C = out["corpus"]
    print("== 台账自证 ==")
    print(f"  {out['todo']}: 行数={L['lines']}  [x]={L['x_lines']}  [ ]={L['open_lines']}  "
          f"bytes={L['bytes']}  sha1[:16]={L['sha1']}")
    print(f"  [x] 行号: {L['x_line_numbers']}")
    print("== 语料自证（判定内核真读到东西）==")
    print(f"  注册面文件={C['registration_files']} (plist {C['launch_agents']} + "
          f"sche_tasks {C['sche_tasks']})  reflect_scripts={C['reflect_scripts']}  "
          f"basename 索引={C['index']}")
    print("== 抽取结果 ==")
    print(f"  反引号内声明 token={out['claims']['backtick_refs']}  "
          f"逐态={out['claims']['by_verdict']}  有声明行={out['claims']['declared_lines']}")
    print(f"  skip={out['skip']}")
    print("== MISMATCH 明细 ==")
    if not out["mismatch"]:
        print("  （无）")
    for m in out["mismatch"]:
        extra = m.get("tok") or m.get("subject") or m.get("title", "")
        print(f"  [{m['kind']}] L{m['line']}  {extra}\n      {m['why']}"
              + (f"  resolved={m.get('resolved')}" if m.get("resolved") else ""))
    print(f"  ⇒ 合计 {len(out['mismatch'])} 条，分类={out['mismatch_by_kind']}")
    print("== SOFT（非硬告警）==")
    for m in out["soft"]:
        print(f"  [{m['kind']}] L{m['line']}  {m.get('subject','')}  {m['why']}")
    print(f"  ⇒ 合计 {len(out['soft'])} 条，分类={out['soft_by_kind']}")
    print("== 只读自证 ==")
    print(f"  受检文件={out['readonly_checked']} 台账+注册面 sha1 变化={out['write_violations'] or '无'}")
    print(f"  secs={out['secs']}")


def audit_text(text, index, reg, scripts, label="synthetic"):
    """供 --selftest 用：对任意合成台账文本跑同一套判定（返回 mism, soft, 抽到的 refs 数）。"""
    skip = collections.Counter()
    mism, soft = [], []
    parsed = 0
    for i, l in enumerate(text.split("\n"), 1):
        if not l.startswith("[x]"):
            continue
        m, s, r = audit_line(i, l, index, reg, scripts, skip)
        mism += m
        soft += s
        parsed += len(r)
    return mism, soft, parsed


def selftest():
    """负控 6 例（每例都断言**真抽到 token**，防空绿）。"""
    index, st = ri.build_index(False)
    reg, scripts = load_registration()
    real = "tools/ref_integrity.py"
    unreg = next((s for s in scripts if not reg_hits(s[:-3], reg) and s != "__init__.py"), None)
    cases = []

    txt = f"[x] 工程 | 引用闸门复用 | 验收：内核 `{real}` 已在盘（本行无任何接线声明）"
    m, s, p = audit_text(txt, index, reg, scripts)
    cases.append(("NC1 真存在路径不误报", len(m) == 0 and p >= 1, f"mismatch={[x['kind'] for x in m]} parsed={p}"))

    txt = "[x] 工程 | 假路径必报 | 验收：产物 `tools/definitely_missing_zzz.py` 已落盘"
    m, s, p = audit_text(txt, index, reg, scripts)
    cases.append(("NC2 假路径必报 PATH_GONE",
                  any(x["kind"] == "PATH_GONE" for x in m) and p >= 1,
                  f"mismatch={[x['kind'] for x in m]} parsed={p}"))

    if unreg:
        txt = (f"[x] 工程 | kc 快照守卫上定时 | 验收：`reflect_scripts/{unreg}` 已接入定时循环，"
               f"每日自动跑，`tools/kc_snapshot_guard.py` 一并生效")
        m, s, p = audit_text(txt, index, reg, scripts)
        cases.append((f"NC3 称接线无注册点必报 WIRING_NO_REGISTRATION（用真实未接线脚本 {unreg}）",
                      any(x["kind"] == "WIRING_NO_REGISTRATION" for x in m) and p >= 2,
                      f"mismatch={[x['kind'] for x in m]} parsed={p}"))
    else:
        cases.append(("NC3 跳过（所有 reflect 脚本都已注册，无法构造）", False, "no unregistered script"))

    txt = ("[x] 工程 | 修复日志压缩脚本 | 验收：已交付 `tools/log_compress.py`；"
           "定时注册=未接线待批（本行不新增 plist）")
    m, s, p = audit_text(txt, index, reg, scripts)
    cases.append(("NC4 真免责行不得硬报", len(m) == 0 and p >= 1,
                  f"mismatch={[x['kind'] for x in m]} parsed={p}"))

    txt = "[x] 工程 | 清账 | 说明：`temp/compress_log.py` 已不存在，勿再引用"
    m, s, p = audit_text(txt, index, reg, scripts)
    cases.append(("NC5 幻影缺失必报 PATH_PHANTOM_ABSENCE",
                  any(x["kind"] == "PATH_PHANTOM_ABSENCE" for x in m) and p >= 1,
                  f"mismatch={[x['kind'] for x in m]} parsed={p}"))

    txt = "[x] 工程 | 修 WARN 文案 | 现象：日志打 `temp/compress_log.py` 不存在: <repo>/temp 路径"
    m, s, p = audit_text(txt, index, reg, scripts)
    cases.append(("NC6 引用 WARN 文案句不得误判幻影（'不存在:' 带冒号）",
                  not any(x["kind"] == "PATH_PHANTOM_ABSENCE" for x in m) and p >= 1,
                  f"mismatch={[x['kind'] for x in m]} parsed={p}"))

    print(f"== selftest 语料自证：index={st} 注册面={len(reg)} reflect_scripts={len(scripts)} ==")
    bad = 0
    for name, ok, detail in cases:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")
        bad += 0 if ok else 1
    print(f"selftest {len(cases) - bad}/{len(cases)}")
    return 2 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--todo", default=DEFAULT_TODO)
    ap.add_argument("--json", default="")
    ap.add_argument("--soft", action="store_true", help="打印 SOFT 明细")
    ap.add_argument("--strict", action="store_true", help="有 MISMATCH → rc=1")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    os.chdir(REPO)                       # cwd 锚定（防相对路径漂移）
    if a.selftest:
        return selftest()
    if not os.path.isfile(a.todo):
        print(f"[warn] 台账不存在：{a.todo}")
        return 0
    out = run(a.todo)
    report_text(out)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"[json] {a.json} ({os.path.getsize(a.json)}B)")
    if out["write_violations"]:
        print(f"[fail] 只读自证失败：{out['write_violations']}")
        return 3
    if a.strict and out["mismatch"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
