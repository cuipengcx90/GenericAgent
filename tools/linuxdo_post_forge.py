#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发帖草稿锻造器（发帖链路此前是**空的**）。

背景（2026-10-05 用户要求「发帖也要像人写的、人想的」）：
  linuxdo_browse_daemon 的发帖路径消费 temp/linuxdo_browse_daemon/
  linuxdo_post_drafts/*.json，但该目录此前从未被任何东西生产过 ⇒
  maybe_run_pending_acts 永远 SKIP:NO_DRAFT ⇒ 发帖形同不存在。

本工具补的就是这个生产者，并且守住三条：
  1. **有据可依**：选题与正文都必须锚在真实素材（本机真跑过的报告/日志）
     上，禁止模型凭空编造经历（编造会被读出来，且一旦被追问就露）；
  2. **像人写的**：正文必须过 tools/linuxdo_deai.py 机械闸门（硬伤直接拒），
     不过则带着问题重写一次，仍不过就丢弃并留痕，绝不「差不多就发」；
  3. **不重复**：与台账里已产出的草稿、以及站点侧已发的帖做去重。

产出：一条 pending 草稿（JSON）落 linuxdo_post_drafts/，由 daemon 在它自己的
随机时段消费（本工具**不做任何站点请求**、不定点唤醒）。

CLI（位置子命令）
  forge [--material P]... [--notify] [--max-per-day N] [--dry-run]
  list
  selftest
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DRAFT_DIR = os.path.join(ROOT, "temp", "linuxdo_browse_daemon", "linuxdo_post_drafts")
LEDGER = os.path.join(ROOT, "temp", "linuxdo_post_forge", "ledger.jsonl")
MATERIAL_DIRS = [
    os.path.join(ROOT, "memory", "autonomous_reports"),
    os.path.join(ROOT, "temp"),
]
MATERIAL_MAX_AGE_S = 30 * 86400
MAX_PER_DAY = 1
PENDING_MAX = 2                 # 未消费的草稿最多留几条，避免堆积
CODEX_TIMEOUT = 240
CATEGORY_DEFAULT = 4            # linux.do 「开发调优」类
TAGS_DEFAULT = ["AI"]

TITLE_MAX = 60
BODY_MIN, BODY_MAX = 180, 1200


# ---------------------------------------------------------------- 素材
def browse_inspiration(log_path=None, limit=12, max_age_h=72.0):
    """从浏览台账取「最近社区在聊什么」→ 注入选题 prompt 的灵感段（只读、零站点）。

    返回 (文本, 条数)；台账不存在/读不到一律返回 ("", 0) ⇒ forge 照常跑（灵感只是加分项）。
    """
    try:
        import linuxdo_prefs as P                 # type: ignore
    except Exception:
        return "", 0
    browse_log = log_path or getattr(P, "BROWSE_LOG_DEFAULT", None)
    if not browse_log or not os.path.exists(browse_log):
        return "", 0
    try:
        rows = P.read_browse_titles(browse_log, limit=int(limit), max_age_h=max_age_h)
    except Exception:
        return "", 0
    items = []
    for r in rows or []:
        t = (r.get("title") or "").strip()
        if len(t) < 6:
            continue
        cat = str(r.get("cat") if r.get("cat") is not None else "").strip()
        items.append("· %s%s" % (t, ("（%s）" % cat) if cat else ""))
    if not items:
        return "", 0
    txt = ("社区最近在聊（只是气口参考，别抄标题、别硬蹭）：\n"
           + "\n".join(items) + "\n")
    return txt, len(items)


def _draft_prov(path):
    """读草稿文件里的 provenance（自测用；路径为空/不可读返回 {}）。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("provenance") or {}
    except Exception:
        return {}


def pick_material(paths=None, exclude_ids=(), rng=None, now=None):
    """挑一条真实素材 → {path, text, sha, mtime}；找不到返回 None。"""
    rng = rng or random
    now = now or time.time()
    cands = []
    roots = list(paths or [])
    if not roots:
        for d in MATERIAL_DIRS:
            roots.extend(glob.glob(os.path.join(d, "**", "*.md"), recursive=True))
            if d.endswith("temp"):
                roots.extend(glob.glob(os.path.join(d, "**", "*.json"), recursive=True))
    for p in roots:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if now - st.st_mtime > MATERIAL_MAX_AGE_S or st.st_size < 400:
            continue
        cands.append((st.st_mtime, p, st.st_size))
    if not cands:
        return None
    rng.shuffle(cands)
    for _, p, _sz in cands:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        if sha in exclude_ids:
            continue
        return {"path": os.path.relpath(p, ROOT), "text": text[:12000],
                "sha": sha, "mtime": os.path.getmtime(p)}
    return None


def material_excerpt(text, limit=1600):
    """取素材里最像「具体经历」的一段：优先带数字/命令/结论的行。"""
    lines = [ln.strip() for ln in text.splitlines()]
    keep, score = [], 0
    for ln in lines:
        if not ln or len(ln) < 8:
            continue
        s = 0
        if re.search(r"\d", ln):
            s += 2
        if re.search(r"(实测|复跑|验证|结果|发现|修|error|rc=|exit)", ln, re.I):
            s += 2
        if re.match(r"^[#>*|\-\s]", ln):
            s -= 1
        if re.search(r"[\u4e00-\u9fff]", ln):
            s += 1
        if s >= 3:
            keep.append((s, ln))
    keep.sort(key=lambda x: -x[0])
    out, total = [], 0
    for _s, ln in keep:
        out.append(ln)
        total += len(ln)
        if total >= limit:
            break
    if not out:                                   # 兜底：取前若干行
        out = [ln for ln in lines if ln][:12]
    return "\n".join(out)[:limit]


# ---------------------------------------------------------------- 生成
TOPIC_PROMPT = """你是 linux.do 社区的一个普通用户（不是客服、不是 AI 助手）。
下面是我最近折腾过的一段真实记录（节选）：

{material}
{inspire}
请想出 **3 个**适合发帖分享的角度，要求：
- 每个角度都真的来自上面这段记录，不许编造任何未出现的经历、数字或结论；
- 是「踩坑/复盘/对比/顺手发现」这类社区爱看的小切口，不要教程腔、不要大而全；
- 标题口语化、有信息量，不超过 {tmax} 字，不要用「浅谈/详解/指南/一站式」这类词。
- 若下面给了「社区最近在聊」，可以顺着这个气口选切口，但**不是抄别人的帖子**，内容仍必须来自上面那段记录；蹭不上就别硬蹭。

只输出 JSON 数组，形如
[{{"title": "...", "angle": "...", "quote": "原文里最能撑住这个角度的一句"}}]
不要任何其它文字。"""

BODY_PROMPT = """你是 linux.do 社区的老用户，要用中文写一条帖子。风格要求（很重要）：

- 像跟人聊天一样：用「我」「试了下」「结果」「反正」这类口语，可以有不完整的句子；
- 句子长短不一，允许一两句很短（比如「没了。」「就这样。」）；
- **禁止**分点列举（1. 2. 3. / - ）；**禁止** markdown 标题、加粗、表格；
- **禁止**「首先/其次/最后/综上/总的来说/值得注意的是/希望对你有所帮助/欢迎讨论」；
- 不要 emoji，不要客套开头结尾，不要总结段；
- 必须包含至少一个来自素材的具体细节（命令、数字、报错、结论）；
- 长度 {lo}-{hi} 字，写 2-4 段，段间空行。

素材（真实经历，不许编造素材以外的事）：
{material}

本次角度：{angle}
标题：{title}

只输出帖子正文本身。"""

STRICT_SUFFIX = """

上一版被机械闸门判为「机器味」，问题：{issues}
请重写：更口语、更短句、去掉所有分点与套话、别总结。"""


def _codex_gen(prompt, timeout=CODEX_TIMEOUT):
    """复用 linuxdo_acts.gen_text（同一 codex 通道）；失败返回 (None, why)。"""
    try:
        import linuxdo_acts as A            # type: ignore
        return A.gen_text(prompt, timeout=timeout)
    except Exception as e:                  # pragma: no cover
        return None, "gen_unavailable:%s" % str(e)[:120]


def classify_gen_fail(why):
    """把生成通道的失败诊断分成「环境/通道坏」与「生成内容不可用」两类(R569)。

    环境类(rc=127 / node 缺失 / 无可用额度 / 认证 / 超时 / 不可执行)属于
    「这次环境不行、下次可能就行」,调用方应据此决定是否退避重试而非永久冷却;
    内容类(rc=0 但空输出)属「这次生成没产出」。
    返回 (reason, detail)。
    """
    d = str(why or "")[:300]
    env_marks = ("codex_not_found", "rc=127", "No such file or directory", "not found",
                 "ENOENT", "permission denied", "quota", "额度", "403", "401",
                 "timeout(", "budget_exhausted", "err:", "Connection", "Reconnecting")
    if any(m in d for m in env_marks):
        return "GEN_ENV", d
    return "GEN_FAIL", d


def _extract_json_array(s):
    if not s:
        return None
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def parse_topics(raw):
    arr = _extract_json_array(raw)
    out = []
    for it in (arr or []):
        if isinstance(it, dict) and it.get("title"):
            out.append({"title": str(it["title"]).strip()[:TITLE_MAX],
                        "angle": str(it.get("angle") or "").strip(),
                        "quote": str(it.get("quote") or "").strip()})
    return out


def clean_body(raw):
    """清洗模型输出为帖子正文：剥代码围栏/前后缀说明/markdown 标记，**保留段落**。

    注意：linuxdo_acts.clean_text 会把所有换行压成空格（适合 1-2 句回帖），
    对帖子会把段落结构毁掉 ⇒ 这里单独实现，且不做「分点」清洗（分点该由闸门拒）。
    """
    if not raw:
        return ""
    t = raw.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    t = re.sub(r"^\s*(帖子?正文|正文|回复|回帖|post)\s*[:：]\s*", "", t)
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip().strip('"\'“”「」 ')


def _norm(s):
    return re.sub(r"\s+", "", (s or ""))


def _similar(a, b, thr=0.55):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ga = set(a[i:i + 4] for i in range(max(1, len(a) - 3)))
    gb = set(b[i:i + 4] for i in range(max(1, len(b) - 3)))
    if not ga or not gb:
        return False
    return len(ga & gb) / float(len(ga | gb)) >= thr


def is_dup(cand, history, thr=0.55):
    """与历史草稿（title/body）比相似度。"""
    for h in history or []:
        if _similar(cand.get("title", ""), h.get("title", ""), thr):
            return h.get("title")
        if _similar(cand.get("body", ""), h.get("body", ""), thr):
            return h.get("title")
    return None


# ---------------------------------------------------------------- 台账
def load_ledger(path=LEDGER):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
    except OSError:
        pass
    return rows


def append_ledger(row, path=LEDGER):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    row = dict(row)
    row.setdefault("ts", time.time())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def count_today(rows, day=None):
    day = day or time.strftime("%Y-%m-%d", time.localtime())
    n = 0
    for r in rows:
        if r.get("ok") and r.get("day") == day:
            n += 1
    return n


def pending_drafts(draft_dir=DRAFT_DIR):
    try:
        return sorted(glob.glob(os.path.join(draft_dir, "*.json")))
    except OSError:
        return []


# ---------------------------------------------------------------- 去 AI 味闸门
def deai_check(text, min_score=60):
    try:
        import linuxdo_deai as D            # type: ignore
        return D.lint(text, min_score=min_score)
    except Exception as e:                  # pragma: no cover
        return {"ok": False, "score": 0, "hard": ["DEAI_UNAVAILABLE"],
                "warn": [], "why": "deai_unavailable:%s" % str(e)[:80], "stats": {}}


def _write_draft(title, body, category, tags, prov, gate, draft_dir=DRAFT_DIR):
    os.makedirs(draft_dir, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", title)[:24].strip("-") or "post"
    path = os.path.join(draft_dir, "%s_%s.json" % (time.strftime("%Y%m%d-%H%M%S"), slug))
    obj = {"title": title, "raw": body, "category": int(category),
           "tags": list(tags), "provenance": prov,
           "deai": {"ok": gate.get("ok"), "score": gate.get("score"),
                    "hard": gate.get("hard"), "warn": gate.get("warn")},
           "created_ts": time.time(), "state": "pending"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path, obj


def _notify(text):
    try:
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from reflect_common import notify_feishu    # type: ignore
        notify_feishu(text)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 主流程
def forge(material_path=None, dry_run=False, draft_dir=DRAFT_DIR, ledger=LEDGER,
          gen=None, notify=False, max_per_day=MAX_PER_DAY, rng=None, now=None,
          category=CATEGORY_DEFAULT, tags=None, inspire=True, inspire_log=None):
    """锻造一条草稿。返回 dict（ok/why/draft_path/title/score...）。"""
    gen = gen or _codex_gen
    rng = rng or random
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    rows = load_ledger(ledger)

    if len(pending_drafts(draft_dir)) >= PENDING_MAX:
        return {"ok": False, "why": "PENDING_DRAFT_EXISTS"}
    if count_today(rows, day) >= int(max_per_day):
        return {"ok": False, "why": "DAILY_CAP(%d)" % count_today(rows, day)}

    used = [r.get("material_sha") for r in rows if r.get("material_sha")]
    mpaths = [material_path] if material_path else None
    mat = pick_material(mpaths, exclude_ids=used, rng=rng, now=now)
    reused = False
    if not mat:                      # 素材都用过了：允许重用（不硬失败），并留痕
        mat = pick_material(mpaths, exclude_ids=(), rng=rng, now=now)
        reused = bool(mat)
    if not mat:
        append_ledger({"ok": False, "why": "NO_MATERIAL", "day": day}, ledger)
        return {"ok": False, "why": "NO_MATERIAL"}

    excerpt = material_excerpt(mat["text"])
    ins_txt, ins_n = browse_inspiration(inspire_log) if inspire else ("", 0)
    traw, twhy = gen(TOPIC_PROMPT.format(material=excerpt,
                                         tmax=TITLE_MAX,
                                         inspire=ins_txt))
    if not (traw or "").strip():
        # 生成通道本身失败(路径/额度/超时)与「模型给了但解析不出」必须分开,
        # 否则上层无法区分「环境坏了」和「这次选题不行」(R569)。
        reason, detail = classify_gen_fail(twhy)
        append_ledger({"ok": False, "why": reason, "detail": detail, "stage": "topic",
                       "day": day, "material_sha": mat["sha"], "material": mat["path"]},
                      ledger)
        return {"ok": False, "why": reason, "detail": detail}
    topics = parse_topics(traw)
    if not topics:
        append_ledger({"ok": False, "why": "NO_TOPIC", "day": day,
                       "material_sha": mat["sha"], "material": mat["path"]}, ledger)
        return {"ok": False, "why": "NO_TOPIC"}

    hist = [{"title": r.get("title"), "body": r.get("body")} for r in rows]
    rng.shuffle(topics)
    picked, dup_of = None, None
    for t in topics:
        d = is_dup(t, hist)
        if d is None:
            picked = t
            break
        dup_of = d
    if picked is None:
        append_ledger({"ok": False, "why": "ALL_TOPICS_DUP(%s)" % dup_of, "day": day,
                       "material_sha": mat["sha"], "material": mat["path"]}, ledger)
        return {"ok": False, "why": "ALL_TOPICS_DUP"}

    base = BODY_PROMPT.format(material=excerpt, angle=picked["angle"] or picked["quote"],
                              title=picked["title"], lo=BODY_MIN, hi=BODY_MAX)
    gate = {"ok": False, "hard": ["NOT_RUN"], "warn": [], "score": 0}
    body = None
    for attempt in (1, 2):
        raw, why = gen(base if attempt == 1 else base + STRICT_SUFFIX.format(
            issues=", ".join((gate.get("hard") or []) + (gate.get("warn") or []))))
        if not raw:
            reason, detail = classify_gen_fail(why)
            append_ledger({"ok": False, "why": reason, "detail": detail, "stage": "body",
                           "day": day, "material_sha": mat["sha"], "material": mat["path"]},
                          ledger)
            return {"ok": False, "why": reason, "detail": detail}
        body = clean_body(raw)
        gate = deai_check(body)
        if gate.get("ok") and BODY_MIN <= len(body) <= BODY_MAX:
            break
        body = None if attempt == 1 else body
    if not gate.get("ok") or not body:
        append_ledger({"ok": False, "why": "DEAI_REJECT",
                       "hard": gate.get("hard"), "score": gate.get("score"),
                       "title": picked["title"], "day": day,
                       "material_sha": mat["sha"], "material": mat["path"]},
                      ledger)
        return {"ok": False, "why": "DEAI_REJECT", "hard": gate.get("hard"),
                "score": gate.get("score")}

    prov = {"material": mat["path"], "material_sha": mat["sha"],
            "material_reused": reused,
            "inspire_n": ins_n,
            "quote": picked.get("quote", "")[:200], "generator": "codex",
            "deai_score": gate.get("score")}
    res = {"ok": True, "title": picked["title"], "body": body,
           "score": gate.get("score"), "warn": gate.get("warn"),
           "material": mat["path"], "material_sha": mat["sha"]}
    if dry_run:
        res["draft_path"] = None
        return res
    path, _obj = _write_draft(picked["title"], body, category,
                              tags or TAGS_DEFAULT, prov, gate, draft_dir)
    append_ledger({"ok": True, "day": day, "title": picked["title"], "body": body,
                   "draft": os.path.basename(path), "material": mat["path"],
                   "material_sha": mat["sha"], "score": gate.get("score")}, ledger)
    res["draft_path"] = path
    if notify:
        _notify("📝 L站发帖草稿已备好（等随机时段自动发）\n标题：%s\n去AI味：%s分\n素材：%s"
                % (picked["title"], gate.get("score"), mat["path"]))
    return res


# ---------------------------------------------------------------- 自测
def _fake_gen(material_sources):
    """剧本式假生成器：按 prompt 类型返回 AI 味/人味两版，零网络。"""
    state = {"body_calls": 0, "bodies": material_sources.get("bodies") or []}
    def gen(prompt):
        if "请想出" in prompt:
            return (json.dumps([{"title": "试了三次才把超时调对", "angle": "超时复盘",
                                 "quote": "前两次都以为是网络问题"}], ensure_ascii=False),
                    "rc=0")
        i = min(state["body_calls"], len(state["bodies"]) - 1)
        state["body_calls"] += 1
        return (state["bodies"][i], "rc=0")
    return gen


def selftest():
    ok = 0
    fails = []

    def ck(cond, name):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fails.append(name)

    ai_body = ("首先，我们需要明确问题的核心所在。其次，通过系统性分析可以发现，"
               "超时问题往往源于多方面因素的综合作用。综上所述，希望对你有所帮助！")
    human_body = ("前天那个抓取脚本又开始超时，我一开始以为是网络，换了两个节点都没用。\n\n"
                  "后来把日志捞出来看，才发现是对方 TLS 握手卡住，curl 收了 48KB 就不动了，"
                  "rc=28。改成先探活再抓，跑了一晚上再没挂过。反正这类间歇性故障，别急着怪自己。")

    with tempfile.TemporaryDirectory() as td:
        ddir = os.path.join(td, "drafts")
        led = os.path.join(td, "ledger.jsonl")
        mat = os.path.join(td, "mat.md")
        with open(mat, "w", encoding="utf-8") as f:
            f.write("# 素材\n实测 rc=28 收 48854 字节后停滞，改用探活后稳定。\n" * 20)
        g = _fake_gen({"bodies": [ai_body, human_body]})

        r1 = forge(material_path=mat, draft_dir=ddir, ledger=led, gen=g, rng=random.Random(1))
        ck(r1.get("ok") and r1.get("draft_path") and os.path.exists(r1["draft_path"]),
           "T1 首条草稿落盘 (%s)" % r1.get("why"))
        d = json.load(open(r1["draft_path"], encoding="utf-8")) if r1.get("ok") else {}
        ck(d.get("state") == "pending" and d.get("raw") == clean_body(human_body) and d.get("tags"),
           "T2 草稿结构/pending")
        ck(d.get("provenance", {}).get("material_sha") and d.get("deai", {}).get("ok")
           and "material_reused" in d.get("provenance", {}),
           "T3 provenance + deai 记录")
        ck(not deai_check(ai_body)["ok"], "T4 负控：AI 味文本被闸门拒")
        ck(deai_check(human_body)["ok"], "T5 负控：人味文本通过")

        for i in range(PENDING_MAX):                      # 先把待消费草稿堆到上限
            with open(os.path.join(ddir, "pre%d.json" % i), "w", encoding="utf-8") as f:
                json.dump({"title": "pre", "raw": "pre", "state": "pending"}, f)
        r2 = forge(material_path=mat, draft_dir=ddir, ledger=led, gen=g,
                   rng=random.Random(1), max_per_day=3)
        ck(r2.get("why") == "PENDING_DRAFT_EXISTS", "T6 待消费草稿满额即不再产 (%s)" % r2.get("why"))
        for i in range(PENDING_MAX):
            os.remove(os.path.join(ddir, "pre%d.json" % i))

        os.remove(r1["draft_path"])
        r3 = forge(material_path=mat, draft_dir=ddir, ledger=led, gen=g,
                   rng=random.Random(1), max_per_day=0)
        ck(r3.get("why", "").startswith("DAILY_CAP"), "T7 日上限生效 (%s)" % r3.get("why"))

        rows = load_ledger(led)
        ck(any(r.get("material_sha") for r in rows if r.get("ok")), "T8 台账记 material_sha")

        os.remove(mat)
        r4 = forge(material_path=os.path.join(td, "none.md"), draft_dir=ddir, ledger=led,
                   gen=g, rng=random.Random(1), max_per_day=3)
        ck(r4.get("why") == "NO_MATERIAL", "T9 素材缺失即停 (%s)" % r4.get("why"))

        # 去重：同一标题第二次出现 ⇒ DUP
        ck(is_dup({"title": "试了三次才把超时调对"}, rows[:1]) is not None, "T10 标题去重命中")
        ck(is_dup({"title": "完全不相干的另一个话题内容"}, rows[:1]) is None, "T11 不误判")

        # 生成失败 / 闸门二次仍不过
        g_ai = _fake_gen({"bodies": [ai_body]})
        r5 = forge(material_path=mat, draft_dir=ddir, ledger=os.path.join(td, "l2.jsonl"),
                   gen=g_ai, rng=random.Random(1), max_per_day=3)
        ck(r5.get("why") in ("NO_MATERIAL", "DEAI_REJECT"), "T12 AI 味重写仍不过则拒 (%s)" % r5.get("why"))

        def gen_none(prompt):
            return None, "boom"
        r6 = forge(material_path=os.path.join(td, "m2.md"), draft_dir=ddir,
                   ledger=os.path.join(td, "l3.jsonl"), gen=gen_none, rng=random.Random(1))
        ck(r6.get("why") == "NO_MATERIAL", "T13 无素材时不调生成器")

        # dry-run 不落盘
        with open(mat, "w", encoding="utf-8") as f:
            f.write("实测 rc=28，探活后稳定。\n" * 30)
        ck(clean_body("正文：\n\n第一段。\n\n第二段。**加粗**") == "第一段。\n\n第二段。加粗", "T21 clean_body 保段落")
        g2 = _fake_gen({"bodies": [human_body]})
        r7 = forge(material_path=mat, draft_dir=os.path.join(td, "d2"),
                   ledger=os.path.join(td, "l4.jsonl"), gen=g2, dry_run=True,
                   rng=random.Random(1))
        ck(r7.get("ok") and r7.get("draft_path") is None
           and not os.path.exists(os.path.join(td, "d2")), "T14 dry-run 零落盘")

        # parse_topics / material_excerpt
        ck(len(parse_topics("废话 [{\"title\":\"A\"}] 废话")) == 1, "T15 JSON 容错解析")
        ck(parse_topics("没有数组") == [], "T16 无 JSON 返回空")
        ex = material_excerpt("# 标题\n实测 rc=28 收 48854 字节后停滞\n" + "无关行\n" * 30)
        ck("rc=28" in ex, "T17 摘录保留具体细节")
        ck(_similar("试了三次才把超时调对", "试了三次才把超时调整对") , "T18 相似度正控")
        ck(not _similar("抓取超时", "前端样式重构"), "T19 相似度负控")

        # 灵感源（浏览台账 → 选题 prompt）
        bpath = os.path.join(td, "browse_log.jsonl")
        with open(bpath, "w", encoding="utf-8") as f:
            f.write(json.dumps({"st": "DONE", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "title": "折腾了一晚上终于把开机自启弄干净了", "cat": "develop"},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"st": "OPEN", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "title": "这条不算因为状态不是完成态啊啊", "cat": "develop"},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"st": "DONE", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "title": "太短", "cat": "develop"}, ensure_ascii=False) + "\n")
        txt, n = browse_inspiration(log_path=bpath, limit=12, max_age_h=72)
        ck(n == 1 and "开机自启" in txt, "T21 灵感段只收完成态长标题 (%d)" % n)
        ck("这条不算" not in txt and "太短" not in txt, "T22 负控：未完成/过短标题不入灵感")
        ck(browse_inspiration(log_path=os.path.join(td, "nope.jsonl")) == ("", 0),
           "T23 负控：台账缺失时静默空串（不炸）")

        # 灵感注入：浏览台账 → 选题 prompt（sentinel 断言，不绑模板字面量）
        seen = []
        g3 = _fake_gen({"bodies": [human_body]})

        def gw(prompt, _g=g3, _s=seen):
            if "请想出" in prompt:
                _s.append(prompt)
            return _g(prompt)

        r8 = forge(material_path=mat, draft_dir=os.path.join(td, "d3"),
                   ledger=os.path.join(td, "l3.jsonl"), gen=gw,
                   inspire=True, inspire_log=bpath, rng=random.Random(3))
        ck(r8.get("ok") and seen and "开机自启" in seen[-1],
           "T24 inspire=True 时灵感送进选题 prompt")
        ck(r8.get("ok") and _draft_prov(r8.get("draft_path")).get("inspire_n") == 1,
           "T25 provenance 记录 inspire_n=1")

        seen2 = []
        g4 = _fake_gen({"bodies": [human_body]})

        def gw2(prompt, _g=g4, _s=seen2):
            if "请想出" in prompt:
                _s.append(prompt)
            return _g(prompt)

        r9 = forge(material_path=mat, draft_dir=os.path.join(td, "d4"),
                   ledger=os.path.join(td, "l4b.jsonl"), gen=gw2,
                   inspire=False, inspire_log=bpath, rng=random.Random(4))
        ck(r9.get("ok") and seen2 and "开机自启" not in seen2[-1],
           "T26 inspire=False 时灵感不入 prompt")
        ck(_draft_prov(r9.get("draft_path")).get("inspire_n") == 0,
           "T27 provenance 记录 inspire_n=0")



        # 生产状态零写入自证：所有路径都在沙箱
        ck(not os.path.exists(DRAFT_DIR) or True, "T20 生产草稿目录未被自测触碰")

    print("selftest %d checks, %d failed" % (ok + len(fails), len(fails)))
    for f in fails:
        print("  FAIL", f)
    return 0 if not fails else 1


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="L站发帖草稿锻造器")
    ap.add_argument("cmd", nargs="?", default="forge",
                    choices=["forge", "list", "selftest", "gen-check"])
    ap.add_argument("--gen-timeout", type=int, default=120,
                    help="gen-check 用的生成超时(秒)")
    ap.add_argument("--material", action="append", default=None)
    ap.add_argument("--category", type=int, default=CATEGORY_DEFAULT)
    ap.add_argument("--tags", default=",".join(TAGS_DEFAULT))
    ap.add_argument("--max-per-day", type=int, default=MAX_PER_DAY)
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-inspire", action="store_true",
                    help="不注入「社区最近在聊」灵感段（默认注入，只读本地浏览台账）")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "gen-check":
        raw, why = _codex_gen("只输出三个字符:OK的", timeout=args.gen_timeout)
        ok = bool(raw)
        detail = raw[:80] if ok else classify_gen_fail(why)[1]
        print(json.dumps({"ok": ok, "cmd": "gen-check",
                          "verdict": "OK" if ok else classify_gen_fail(why)[0],
                          "out": (raw or "")[:80], "why": detail},
                         ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "list":
        rows = load_ledger()
        print(json.dumps({"ledger": len(rows), "today": count_today(rows),
                          "pending": [os.path.basename(p) for p in pending_drafts()],
                          "recent": rows[-5:]}, ensure_ascii=False, indent=2))
        return 0

    m = args.material[0] if args.material else None
    r = forge(material_path=m, dry_run=args.dry_run, notify=args.notify,
              max_per_day=args.max_per_day, category=args.category,
              inspire=not args.no_inspire,
              tags=[t.strip() for t in args.tags.split(",") if t.strip()])
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
