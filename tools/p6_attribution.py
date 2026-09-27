#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6「产物查无」逐条归因器（TODO#164 / R448）—— 只读消费器，纯 stdlib。

背景
----
`temp/history_value/score.py`（R373 六信号六模式量化）用 P6 判据给自主历史条目打分：
  裸文件名不在「磁盘基线」(disk_basenames) ∧ 不在「全仓限深 3 名集」(all_basenames) ⇒ 判「产物查无」。
本器**消费**该产物（scores.json），对每条 P6 命中逐名给出**可复核的裁决 + 证据链**，回答
「查无」到底是哪一类：现役可在仓内解析 / 工具误报 / 语义漂移 / 真断链。

五层证据（全部只读）
--------------------
  L1 repo      : 仓内**任意深度**同名文件（覆盖 temp/ 等 gitignored 区）
  L2 git       : `git log --all --name-only` 历史曾出现的名（曾存在后被删 ⇒ 与「从未存在」区分）
  L3 out       : 仓外根（~/Desktop … ~/Library/Application Support、/tmp、~/.Trash，限深 6）同名文件
  L4 near      : 近名候选（同 stem 异扩展 / 编辑距离 ≤2 / 长 stem 词边界包含）——**仅提示，不作裁决**
  L5 session   : L4 会话原文（复用 tools/l4_search.zips()，流式扫 zip，抽「带路径前缀」窗口 + 创建/计划语汇）
判据顺序（fail-closed，先证据强度后兜底）
  1) 仓内可解析                  → EXISTS_IN_REPO
  2) 仓外存在 / 抽取截断假名      → FALSE_POSITIVE(OUT_OF_REPO_EXISTS | TRUNCATED_EXTRACT)
  3) 会话原文带 temp/ 前缀        → REAL_GONE(TEMPORARY_LIFECYCLE)
  4) .md + `./` 报告口径          → REAL_GONE(REPORT_BODY)
  5) 仅规划语汇（名旁 ±60 字符，带创建语汇时不算） → SEMANTIC_DRIFT(PLAN_ONLY_MENTION)
  6) 名旁有已产出语汇（剔除「待产出」类前缀）      → REAL_GONE(CREATED_THEN_GONE)
  7) 其余                        → REAL_GONE(NO_TRACE)

★近名（near）**只作提示不留判决**：R448 首次真跑把 8/8 近名命中判成误报，逐条复核后
  全是巧合/报告提及/他项目同名（demo.tape 属 herdr 插件资产、model_list_report_test.go 属
  temp 内 multica 克隆、recommendations-*.js 属 Follow 应用 bundle、ai_analyzer.py 属别的
  微信脚本、R214_*/2026-05-13_* 是**谈它的报告**），而候选池还含「会话里被 BARE_RE 抽出的
  名」（未必在盘）⇒ 任何以近名翻案的判据都会系统性假绿。故 near 仅写入 why/JSON 提示。

用法
----
  python3 tools/p6_attribution.py --json temp/p6_attribution/attribution.json
  python3 tools/p6_attribution.py --selftest          # 13/13 PASSED（标签 NC0–NC11），纯合成夹具，零网络
  python3 tools/p6_attribution.py --readonly-proof    # 源产物 sha256 前后一致自证

边界
----
- **只读**：不写被消费品；唯一写盘是 `--json` 指定的输出文件。
- 本器**不改** P6 判据、不改 score.py（BARE_RE 缺右边界的缺陷只登记，见报告 §2）。
- 「仓外 0 命中」只对本次扫描面成立（限深 6 + 跳过名单），不作全盘否定结论。
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
SCORES = os.path.join(REPO, "temp", "history_value", "scores.json")

# ---- 与 score.py 同款抽取器（用于说明「假名」截断；本器只用它做诊断，不用它建判据）----
BARE_RE = r"(?<![A-Za-z0-9_/.-])(?:[A-Za-z0-9_.-]+/)*([A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh|json|md))"
BARE_RE_FIXED = BARE_RE + r"(?![A-Za-z0-9_.-])"

PLAN_KW = re.compile(r"候选|计划|待产出|将产出|产出名|预期|验收|含≥|拟投入|立项|TODO|待办")
CREATE_KW = re.compile(r"已生成|已创建|已产出|新建|生成|落地|写入|产出|完成|保存")
# 规划/否定前缀：其后紧跟的创建语汇不计「已产出」证据（如「待产出」「未生成」「拟生成」）
NEG_PRE = re.compile(r"(待|未|拟|将|计划|预期|预计|候选|准备|需要|建议)\s*$")


def create_kw_hits(seg):
    """已产出语汇计数；剔除带规划/否定前缀者（防把「待产出」当事实证据）"""
    n = 0
    for m in CREATE_KW.finditer(seg):
        if NEG_PRE.search(seg[max(0, m.start() - 3): m.start()]):
            continue
        n += 1
    return n

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".mypy_cache", ".pytest_cache", ".DS_Store"}
OUT_ROOTS = ["~/Desktop", "~/Documents", "~/Downloads", "~/projects", "~/develop", "~/bin",
             "~/Library/Application Support", "~/Library/LaunchAgents", "~/Library/Preferences",
             "~/.codex", "~/.claude", "~/.config", "/tmp", "~/.Trash"]
OUT_MAXDEPTH = 6
OUT_CAP = 400000
OUT_TIME = 60.0


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as ex:                                   # noqa
        return 99, "", str(ex)


# --------------------------------------------------------------------------- L1/L2/L3
def repo_names():
    """仓内任意深度文件名 → {name: [paths]}"""
    out, n = {}, 0
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            n += 1
            out.setdefault(f, []).append(os.path.relpath(os.path.join(root, f), REPO))
    return out, n


def git_names():
    """git 全历史（含已删）出现过的文件名 + 曾 add 过的名"""
    rc, out, err = sh(["git", "-C", REPO, "log", "--all", "--name-only", "--pretty=format:"])
    alln, added = set(), set()
    if rc == 0:
        for ln in out.splitlines():
            ln = ln.strip()
            if ln:
                alln.add(os.path.basename(ln))
    rc2, out2, _ = sh(["git", "-C", REPO, "log", "--all", "--name-only", "--diff-filter=A",
                       "--pretty=format:"])
    if rc2 == 0:
        for ln in out2.splitlines():
            ln = ln.strip()
            if ln:
                added.add(os.path.basename(ln))
    return alln, added, rc


def out_names(roots=None, repo_root=None):
    """仓外根（限深）文件名 → {name: [abs paths]}；显式跳过本仓子树

    ★R448 修：原判据 `os.path.join(root, REPO) == REPO` 恒真（join 遇绝对路径会整段重置）
    ⇒ 每个目录都被 `dirs[:] = []` 清空、seen 恒 0 = L3 证据层**静默失效**（自证行会打印 0，
    但若没人看就误当"仓外确实没有"）。现改为按绝对路径前缀判定 root 是否落在仓内，
    run() 追加 seen==0 告警（fail-closed 提示），selftest 增 NC9 负控锁死该行为。
    返回 (索引, 扫描文件数, 是否触上限, 实际存在的根数)。
    """
    roots = OUT_ROOTS if roots is None else roots
    repo = REPO if repo_root is None else repo_root
    out, seen, t0 = {}, 0, time.time()
    n_roots = 0
    for r in roots:
        base = os.path.expanduser(r)
        if not os.path.isdir(base):
            continue
        n_roots += 1
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            aroot = os.path.abspath(root)
            if aroot == repo or aroot.startswith(repo + os.sep):
                dirs[:] = []
                continue
            depth = len(aroot[len(base):].split(os.sep)) - 1
            if depth >= OUT_MAXDEPTH:
                dirs[:] = []
            for f in files:
                seen += 1
                out.setdefault(f, []).append(os.path.join(root, f))
            if seen > OUT_CAP or time.time() - t0 > OUT_TIME:
                return out, seen, True, n_roots
    return out, seen, False, n_roots


def near_candidates(name, pool_names):
    """近名：同 stem 异扩展 / 编辑距离≤2（名长≥6）/ 长 stem 词边界包含"""
    stem, ext = os.path.splitext(name)
    hits = []
    for c in pool_names:
        if c == name:
            continue
        cs, ce = os.path.splitext(c)
        if cs == stem and ce != ext:
            hits.append((c, "stem-equal-ext-diff"))
        elif len(stem) >= 8 and re.search(r"(^|[_\-.])%s([_\-.]|$)" % re.escape(stem), cs) and cs != stem:
            hits.append((c, "stem-contained"))
        elif len(name) >= 6 and lev(name, c) <= 2:
            hits.append((c, "lev<=2"))
    return hits[:5]


def lev(a, b):
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# --------------------------------------------------------------------------- L5 会话原文
def l4_windows(names, per_name=3, per_name_pref=3):
    """复用 tools/l4_search.zips()，单遍流式扫 zip。

    返回 (windows, meta)：windows[name] = [(zip, lineno, window)]，其中含首 per_name 个
    「展示窗」+ 最多 per_name_pref 个「带路径前缀窗」（合并去重）；
    meta = [每 zip 成员数, zip 数, 耗时, {name: 原文提及行数}]。
    """
    sys.path.insert(0, os.path.join(REPO, "tools"))
    try:
        import l4_search
        zips = l4_search.zips()
    except Exception as ex:                                   # noqa
        print("[WARN] l4_search 不可用(%s)，跳过会话原文层" % ex)
        return {}, [[], 0, 0.0, {}]
    windows, counts, prefw, l4_mentions = {}, {}, {}, {}
    t0 = time.time()
    for zp in zips:
        try:
            zf = zipfile.ZipFile(zp)
        except Exception:                                     # noqa
            continue
        with zf:
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                try:
                    txt = zf.read(info).decode("utf-8", "ignore")
                except Exception:                             # noqa
                    continue
                counts[os.path.basename(zp)] = counts.get(os.path.basename(zp), 0) + 1
                for i, line in enumerate(txt.splitlines(), 1):
                    for nm in names:
                        if nm not in line:
                            continue
                        l4_mentions[nm] = l4_mentions.get(nm, 0) + 1
                        zb = os.path.basename(zp)
                        bucket = windows.setdefault(nm, [])
                        if len(bucket) < per_name:
                            bucket.append((zb, i, line.strip()[:400]))
                        # ★R448 修：首 3 窗只能算「展示样本」，temp/ 前缀这类判据证据可能在
                        # 第 4+ 行才出现（实测 20 个名唯独 3 个被判 TEMPORARY_LIFECYCLE）
                        # ⇒ 另按「带路径前缀」补采最多 per_name_pref 窗，避免证据被截断。
                        m = re.search(r"([\w./~-]{0,90})" + re.escape(nm), line)
                        if m and "/" in m.group(1):
                            pb = prefw.setdefault(nm, [])
                            if len(pb) < per_name_pref:
                                pb.append((zb, i, line.strip()[:400]))
    for nm, pb in prefw.items():                     # 合并（去重），展示窗在前
        seen_k, merged = set(), []
        for w in windows.get(nm, []) + pb:
            k = (w[0], w[1])
            if k in seen_k:
                continue
            seen_k.add(k)
            merged.append(w)
        windows[nm] = merged
    return windows, [counts, len(zips), round(time.time() - t0, 2), l4_mentions]


def prefixes_of(name, window):
    """窗口内该名左侧的目录前缀（判 temp/、仓外绝对路径等）"""
    out = []
    for m in re.finditer(r"([\w./~-]{0,90})" + re.escape(name), window):
        p = m.group(1)
        if "/" in p:
            out.append(p)
    return out


# --------------------------------------------------------------------------- 分类
def classify(fact):
    name = fact["name"]
    if fact["in_repo"]:
        return "EXISTS_IN_REPO", "仓内可解析", fact["in_repo"]
    if fact["out_paths"] or fact["truncated_of"]:
        if fact["truncated_of"]:
            return "FALSE_POSITIVE", "TRUNCATED_EXTRACT", \
                "真身 %s 存在于 %s（BARE_RE 缺右边界，长扩展名被截断）" % (
                    fact["truncated_of"], (fact["out_paths"] or ["仓外"])[0])
        return "FALSE_POSITIVE", "OUT_OF_REPO_EXISTS", fact["out_paths"][0]
    if fact.get("trunc_sess"):
        return "FALSE_POSITIVE", "TRUNCATED_EXTRACT", \
            "会话原文真身为 %s（BARE_RE 缺右边界，长扩展名被截断；源 %s）" % (
                fact["trunc_sess"][0][2], fact["trunc_sess"][0][0])
    note = ""
    if fact["near"]:
        note = "；近名候选 %s（%s，仅提示：R448 实测该信号 8/8 为巧合/报告提及/他项目同名，不作裁决）" % (
            fact["near"][0][0], fact["near"][0][1])
    cre_ctx = fact.get("create_ctx", fact.get("create_hits", 0))
    plan_ctx = fact.get("plan_ctx", fact.get("plan_hits", 0))
    if fact["temp_prefix"]:
        return "REAL_GONE", "TEMPORARY_LIFECYCLE", \
            "会话原文带 temp/ 前缀：%s%s" % (fact["temp_prefix"][:1], note)
    if name.endswith(".md") and fact["dot_slash"]:
        return "REAL_GONE", "REPORT_BODY", "报告正文缺失（./%s）%s" % (name, note)
    if name.endswith(".sh") and fact["root_level"]:
        return "REAL_GONE", "ROOT_UNTRACKED", "曾在仓根、git 全历史 0 ⇒ 未提交过" + note
    if plan_ctx and not cre_ctx:
        return "SEMANTIC_DRIFT", "PLAN_ONLY_MENTION", \
            "名旁仅规划/验收语汇（计划 %d 处 / 创建 0 处），无落盘与路径证据%s" % (plan_ctx, note)
    if cre_ctx:
        return "REAL_GONE", "CREATED_THEN_GONE", \
            "名旁创建语汇 %d 处（曾产出）而现盘上无、git 全历史 0%s" % (cre_ctx, note)
    return "REAL_GONE", "NO_TRACE", \
        "无路径/创建证据（名旁创建 %d / 计划 %d；会话窗口 %d 条）%s" % (
            cre_ctx, plan_ctx, len(fact.get("windows", [])), note)


def facts_for(name, windows, repo_idx, git_all, git_added, out_idx, pool):
    win = windows.get(name, [])
    txt = " ".join(w[2] for w in win)
    pfx = []
    for w in win:
        pfx += prefixes_of(name, w[2])
    temp_prefix = [p for p in pfx if re.search(r"(^|/)temp/", p) or "autonomous_reports/" in p]
    dot_slash = any(p.endswith("./") or p == "./" for p in pfx) or any("./" + name in w[2] for w in win)
    root_level = any(p in ("", "./") for p in pfx) or any((" " + name) in w[2] for w in win)
    truncated_of = None
    for alt in (name + "l", name + "c"):
        if alt in out_idx or alt in repo_idx:
            truncated_of = alt
            break
    # 名旁（±60 字符）语汇证据：比全文计数强得多（防"会话别处以创建语汇谈论别的产物"）
    # 会话原文里该名后紧跟扩展名字符 ⇒ 真身是更长扩展名（.jsonl/.mdx 等），BARE_RE 缺右边界截断
    trunc_sess = []
    for zbn, ln, t in win:
        for m in re.finditer(re.escape(name) + r"([A-Za-z0-9]{1,3})(?![A-Za-z0-9_.-])", t):
            trunc_sess.append((zbn, ln, name + m.group(1)))
    create_ctx = plan_ctx = 0
    for _, _, t in win:
        for m in re.finditer(re.escape(name), t):
            seg = t[max(0, m.start() - 60): m.start() + len(name) + 60]
            if create_kw_hits(seg):
                create_ctx += 1
            if PLAN_KW.search(seg):
                plan_ctx += 1
    return {
        "name": name,
        "in_repo": repo_idx.get(name, []),
        "in_git_all": name in git_all,
        "in_git_added": name in git_added,
        "out_paths": out_idx.get(name, [])[:3],
        "near": near_candidates(name, pool),
        "truncated_of": truncated_of,
        "trunc_sess": trunc_sess,
        "temp_prefix": temp_prefix,
        "dot_slash": dot_slash,
        "root_level": root_level,
        "create_hits": create_kw_hits(txt),
        "plan_hits": len(PLAN_KW.findall(txt)),
        "create_ctx": create_ctx,
        "plan_ctx": plan_ctx,
        "windows": win,
    }


# --------------------------------------------------------------------------- 主流程
def load_p6():
    ents = json.load(io.open(SCORES, encoding="utf-8"))
    ents.sort(key=lambda e: e.get("line", 0))
    hits = []
    for e in ents:
        g = e.get("ghost") or []
        if e.get("P6_产物查无") and g:
            hits.append(e)
    names = []
    for e in hits:
        for g in e["ghost"]:
            if g not in names:
                names.append(g)
    return ents, hits, names


def run(args):
    ents, hits, names = load_p6()
    print("[自证] scores.json 条目 %d / P6 命中 %d / ghost 名（去重）%d" % (len(ents), len(hits), len(names)))
    repo_idx, nfiles = repo_names()
    print("[自证] L1 仓内文件 %d 个（任意深度，含 temp/）" % nfiles)
    git_all, git_added, grc = git_names()
    print("[自证] L2 git 历史文件名 %d（rc=%d；曾 add %d）" % (len(git_all), grc, len(git_added)))
    out_idx, seen, capped, nroots = out_names()
    print("[自证] L3 仓外根 %d/%d 存在 / 扫到文件 %d 个%s ⇒ 名 %d 个" % (
        nroots, len(OUT_ROOTS), seen, "（达上限提前停）" if capped else "", len(out_idx)))
    if nroots and seen == 0:
        print("[WARN] L3 仓外扫描 0 文件 ⇒ 该证据层失效，本次不得据此判 FALSE_POSITIVE(OUT_OF_REPO)")
    windows, l4meta = l4_windows(names)
    print("[自证] L5 会话原文：zip %d / 成员 %s / 耗时 %ss ⇒ 命中名 %d（提及行合计 %d）" % (
        l4meta[1], l4meta[0], l4meta[2], len(windows), sum(l4meta[3].values())))
    pool = set(repo_idx) | set(out_idx) | set(git_all)
    for w in windows.values():
        for _, _, t in w:
            for m in re.finditer(BARE_RE, t):
                pool.add(m.group(1))
    rows = []
    for nm in names:
        f = facts_for(nm, windows, repo_idx, git_all, git_added, out_idx, pool)
        v, sub, why = classify(f)
        rows.append({"name": nm, "verdict": v, "subkind": sub, "why": why,
                     "mentions": sum(1 for e in hits if nm in (e.get("ghost") or [])),
                     "rnums": [e["rnum"] for e in hits if nm in (e.get("ghost") or [])],
                     "evidence": {"in_repo": f["in_repo"][:2], "in_git": f["in_git_added"],
                                  "out": f["out_paths"], "near": f["near"][:2],
                                  "near_note": (f["near"][0][0] if f["near"] else None),
                                  "truncated_of": f["truncated_of"],
                                  "temp_prefix": f["temp_prefix"][:2],
                                  "create_hits": f["create_hits"], "plan_hits": f["plan_hits"],
                                  "windows": f["windows"][:2]}})
    summ = {}
    for r in rows:
        key = r["verdict"] + "/" + r["subkind"]
        summ[key] = summ.get(key, 0) + 1
    print("\n== 裁决汇总（%d 名）" % len(rows))
    for k in sorted(summ):
        print("   %-42s %d" % (k, summ[k]))
    print("\n== 逐名")
    for r in rows:
        print("  [%s/%s] %-38s r=%s  %s" % (r["verdict"], r["subkind"], r["name"],
                                            ",".join("R%d" % x for x in r["rnums"]), r["why"][:90]))
    doc = {"generated_by": "tools/p6_attribution.py", "scores": os.path.relpath(SCORES, REPO),
           "n_entries": len(ents), "n_p6_hits": len(hits), "n_names": len(names),
           "self_proof": {"repo_files": nfiles, "git_names": len(git_all), "out_seen": seen,
                          "out_capped": capped, "l4_meta": l4meta},
           "summary": summ, "rows": rows}
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with io.open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=1))
        print("\n[json] %s (%dB)" % (args.json, os.path.getsize(args.json)))
    return doc


# --------------------------------------------------------------------------- 自证
def selftest():
    ok = []
    # NC0 抽取器：BARE_RE 缺右边界 ⇒ .jsonl 被截成 .json（缺陷留证）
    got = [m.group(1) for m in re.finditer(BARE_RE, "日志写入 logs/rss_hit_rate.jsonl 完")]
    assert got == ["rss_hit_rate.json"], got
    ok.append("NC0 BARE_RE 截断留证 .jsonl→.json")
    fixed = [m.group(1) for m in re.finditer(BARE_RE_FIXED, "日志写入 logs/rss_hit_rate.jsonl 完")]
    assert fixed == [], fixed
    ok.append("NC0b 补右边界后不再抽假名")
    # NC1 仓内存在 ⇒ 不得判 REAL_GONE
    f = dict(name="x.py", in_repo=["tools/x.py"], in_git_all=True, in_git_added=True,
             out_paths=[], near=[], truncated_of=None, temp_prefix=[], dot_slash=False,
             root_level=False, create_hits=0, plan_hits=0, windows=[])
    assert classify(f)[0] == "EXISTS_IN_REPO"
    ok.append("NC1 仓内存在≠查无")
    # NC2 仓外存在 ⇒ FALSE_POSITIVE（负控：不得落 REAL_GONE）
    f["in_repo"] = []
    f["out_paths"] = ["/Users/x/Library/Application Support/Claude/claude_desktop_config.json"]
    assert classify(f)[:2] == ("FALSE_POSITIVE", "OUT_OF_REPO_EXISTS")
    ok.append("NC2 仓外存在=工具误报")
    # NC3 temp/ 前缀 ⇒ REAL_GONE/TEMPORARY_LIFECYCLE
    f2 = dict(f, out_paths=[], truncated_of=None, temp_prefix=["temp/ocr_search.py"],
              windows=[("2026-05.zip", 3, "temp/ocr_search.py 已生成")], create_hits=1)
    assert classify(f2)[:2] == ("REAL_GONE", "TEMPORARY_LIFECYCLE")
    ok.append("NC3 temp 前缀=真断链")
    # NC4 抽取截断假名
    f3 = dict(f2, temp_prefix=[], truncated_of="rss_hit_rate.jsonl",
              out_paths=["/tmp/TrendRadar/logs/rss_hit_rate.jsonl"])
    assert classify(f3)[:2] == ("FALSE_POSITIVE", "TRUNCATED_EXTRACT")
    ok.append("NC4 截断假名=工具误报")
    # NC5 近名**不得单独构成裁决**（R448 校准：8/8 近名命中皆巧合/报告提及/他项目同名）
    assert near_candidates("recommendations.json", {"jetbrains_plugin_recommendations.json"})
    f5 = dict(f2, temp_prefix=[], truncated_of=None, near=[("recommendations-SqCgBATU.js", "stem-equal-ext-diff")],
              create_hits=0, plan_hits=0, create_ctx=0, plan_ctx=0, windows=[])
    assert classify(f5)[:2] == ("REAL_GONE", "NO_TRACE"), classify(f5)
    assert "近名候选" in classify(f5)[2]                  # 仍须在 why 里留提示
    # 负控方向：名旁有创建语汇 ⇒ 不得落 NO_TRACE
    f5b = dict(f5, create_ctx=2)
    assert classify(f5b)[:2] == ("REAL_GONE", "CREATED_THEN_GONE")
    ok.append("NC5 近名仅提示不判误报 + 创建证据升级")
    # NC6 名旁仅规划语汇 ⇒ SEMANTIC_DRIFT（不得落真断链）
    f6 = dict(f2, temp_prefix=[], truncated_of=None, near=[], create_hits=0, plan_hits=3,
              create_ctx=0, plan_ctx=3)
    assert classify(f6)[:2] == ("SEMANTIC_DRIFT", "PLAN_ONLY_MENTION")
    f7 = dict(f6, create_ctx=2)           # 有创建证据 ⇒ 回归真断链（防“无差别漂移”假绿）
    assert classify(f7)[0] == "REAL_GONE"
    ok.append("NC6 计划语汇 vs 创建语汇区分")
    # NC7 判据顺序：仓内 > temp 前缀 > 语汇
    f8 = dict(f6, in_repo=["temp/x.md"])
    assert classify(f8)[0] == "EXISTS_IN_REPO"
    assert classify(dict(f6, temp_prefix=["temp/x.py"]))[:2] == ("REAL_GONE", "TEMPORARY_LIFECYCLE")
    ok.append("NC7 顺序 fail-closed")
    # NC8 lev 守卫（长度差 >2 直接短路返回 9，不逐字符算；等长近名照常算）
    assert lev("demo.sh", "demo.py") == 2 and lev("a.py", "abcdef.py") >= 9
    assert not near_candidates("check_todo_density.py", {"ocr_search.py"})
    ok.append("NC8 编辑距离守卫")
    # NC9 仓外扫描面真读到语料（锁死 R448 修掉的 join 判据坑：修前 seen 恒 0）
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="p6attr_nc9_")
    try:
        os.makedirs(os.path.join(tmp, "root", "sub"))
        io.open(os.path.join(tmp, "root", "a.py"), "w").write("x")
        io.open(os.path.join(tmp, "root", "sub", "b.json"), "w").write("y")
        fake_repo = os.path.join(tmp, "root", "sub_repo")
        os.makedirs(os.path.join(fake_repo, "inner"))
        io.open(os.path.join(fake_repo, "inner", "inside.py"), "w").write("z")
        idx, seen, capped, nroots = out_names(roots=[os.path.join(tmp, "root")],
                                              repo_root=fake_repo)
        assert seen == 2 and "a.py" in idx and "b.json" in idx, (seen, sorted(idx))
        assert "inside.py" not in idx, sorted(idx)    # 负控：本仓子树被跳过
        assert nroots == 1 and capped is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok.append("NC9 仓外扫描面真读到语料且跳过本仓子树")
    # NC10 名旁证据链：真实窗口 → temp 前缀 / 计划语汇（防“窗口没读到”假绿）
    win = {"ocr_search.py": [("2026-05.zip", 5, "→ 已生成 temp/ocr_search.py（5208 字节）")]}
    f10 = facts_for("ocr_search.py", win, {}, set(), set(), {}, set())
    assert f10["temp_prefix"], f10["temp_prefix"]
    assert f10["create_ctx"] >= 1, f10["create_ctx"]
    assert classify(f10)[:2] == ("REAL_GONE", "TEMPORARY_LIFECYCLE")
    win2 = {"x_new_tool.py": [("2026-08.zip", 9, "候选：x_new_tool.py（待产出，含≥3个发现）")]}
    f11 = facts_for("x_new_tool.py", win2, {}, set(), set(), {}, set())
    assert f11["plan_ctx"] >= 1 and f11["create_ctx"] == 0, f11
    assert classify(f11)[:2] == ("SEMANTIC_DRIFT", "PLAN_ONLY_MENTION")
    # 语汇判据本身：规划式前缀（待/未/拟/计划）不算已产出
    assert create_kw_hits("待产出 foo.py") == 0
    assert create_kw_hits("未生成 foo.py") == 0
    assert create_kw_hits("已生成 foo.py") == 1
    assert create_kw_hits("已生成并保存 foo.py") == 2
    win3 = {"rss_hit_rate.json": [("2026-07.zip", 3, "/Users/cuip/projects/TrendRadar/logs/rss_hit_rate.jsonl 命中率 0.62")]}
    f12 = facts_for("rss_hit_rate.json", win3, {}, set(), set(), {}, set())
    assert f12["trunc_sess"] and f12["trunc_sess"][0][2] == "rss_hit_rate.jsonl", f12["trunc_sess"]
    assert classify(f12)[:2] == ("FALSE_POSITIVE", "TRUNCATED_EXTRACT")
    f13 = facts_for("demo.sh", {"demo.sh": [("z.zip", 1, "demo.sh 已运行")]}, {}, set(), set(), {}, set())
    assert not f13["trunc_sess"], f13["trunc_sess"]      # 负控：正常名不误判截断
    ok.append("NC10 名旁证据链（temp 前缀 / 计划语汇 / 规划式前缀剔除 / 会话级截断真身）")
    # NC11 路径前缀窗不被首 3 窗上限吃掉（R448 实测：20 名有 temp/ 前缀却只 3 名被判）
    import zipfile as _zf
    tmpd = tempfile.mkdtemp(prefix="p6attr_nc11_")
    try:
        zp = os.path.join(tmpd, "2026-05.zip")
        lines = ["ghost_tool.py 提及 %d" % i for i in range(1, 6)]
        lines.append("→ 已生成 temp/ghost_tool.py（1234 字节）")
        with _zf.ZipFile(zp, "w") as z:
            z.writestr("sess/a.txt", "\n".join(lines))
        import types
        mod = types.ModuleType("l4_search")
        mod.zips = lambda: [zp]
        sys.modules["l4_search"] = mod
        w, meta = l4_windows(["ghost_tool.py"])
        assert meta[3]["ghost_tool.py"] == 6, meta[3]
        assert any("temp/ghost_tool.py" in t for _, _, t in w["ghost_tool.py"]), w
        f11b = facts_for("ghost_tool.py", w, {}, set(), set(), {}, set())
        assert f11b["temp_prefix"], f11b["temp_prefix"]
        assert classify(f11b)[:2] == ("REAL_GONE", "TEMPORARY_LIFECYCLE"), classify(f11b)
        sys.modules.pop("l4_search", None)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    ok.append("NC11 路径前缀窗不被首 3 窗上限吃掉（temp/ 证据可达）")
    for i, s in enumerate(ok, 1):
        print("  [%d/%d] PASS %s" % (i, len(ok), s))
    print("selftest %d/%d PASSED" % (len(ok), len(ok)))
    return 0


def readonly_proof():
    targets = [SCORES] + [os.path.join(REPO, "tools", "l4_search.py")]
    sys.path.insert(0, os.path.join(REPO, "tools"))
    try:
        import l4_search
        targets += l4_search.zips()
    except Exception:                                         # noqa
        pass
    def dig():
        h = {}
        for t in targets:
            if os.path.exists(t):
                m = hashlib.sha256()
                with open(t, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        m.update(chunk)
                h[os.path.relpath(t, REPO)] = m.hexdigest()[:16]
        return h
    a = dig()
    json.load(io.open(SCORES, encoding="utf-8"))
    b = dig()
    diff = [k for k in a if a[k] != b.get(k)]
    print("[只读自证] 源产物 %d 个，前后 sha256 差异 %d ⇒ %s" % (len(a), len(diff), "一致" if not diff else diff))
    for k in sorted(a):
        print("   %s %s" % (a[k], k))
    return 0 if not diff else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="输出 JSON 路径（唯一写盘）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readonly-proof", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.readonly_proof:
        return readonly_proof()
    run(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
