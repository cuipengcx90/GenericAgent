#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周报「条目归属」机械闸门（周报 SOP 档位 A 的强制校验内核）。

背景（2026-10-01 事故审计）：2026-09-28~09-30 那期周报的「三、门店与设备侧业务问题响应」
三条内容全部来自群聊（软件智能化小组-内）且**没有一条与汇报人崔朋有关**——
高德渠道无弹窗触发数据=马腾飞@周永清；自动退款限额显示"-"=马腾飞@张义群；
C042 点餐屏设备维护中=郭翰林转姚海峰@张义群@王冰岩。
报告却把它们写成"我"的响应事项，下周计划与需配合事项继续写成"我"的待办。
根因不是 SOP 没写限制，而是限制只存在于措辞（"例如 @崔朋…"）里，没有任何一步机械校验；
weekly_report_verify.py 原有 12 项全部只查版式/收件人/日期，不查内容归属。

本工具把「每条正文条目必须能追溯到本人证据」变成可执行判据：

  content.json 的每个 bullet 可选两种形态：
    "纯文本"                                   ← 旧形态，本闸门判 PROVENANCE_MISSING
    {"text": "…", "provenance": ["<token>", …]} ← 新形态，token 白名单见下

  token 白名单（只认这四种，其余一律 TOKEN_UNKNOWN_KIND，fail-closed）：
    git:<sha>          该 sha 必须出现在本周 git dump 里（取前 7~40 位十六进制前缀匹配）
    git-repo:<repo>    该仓库必须存在于 dump 且本周有提交（用于「本周提交概况」类汇总条目）
    chat:<message_id>  该 message_id 必须存在，**且该消息归属本人**（见下）——这是关键判据
    external:<理由>     本条不是本周可溯源产出，而是外部指派/长期目标（理由 ≥4 字）。
                       **这是唯一的人工声明通道**：写了它就必须在审阅时被人看见，
                       cmd_check 会把纯 external 条目单列出来要求人工确认；
                       text 恰为「无」的占位条目豁免（不计入 bullet 校验）。

  「群聊消息归属本人」的三条件（任一满足）：
    ① sender.name == --me（默认 崔朋，可用 --alias 追加昵称）
    ② 正文含本人名（如 "@崔朋" / "崔朋："）
    ③ mentions[] 的 name == 本人
  不满足 ⇒ 该消息不得作为正文条目的证据（CHAT_UNATTRIBUTED）。
  这条判据正是 09-30 那期缺失的一环：那 13 条群消息里「崔朋」出现 0 次。

用法（子命令为位置参数）：
  python3 tools/weekly_report_provenance_gate.py check \
      --content-json temp/content.json \
      --git-json temp/wr_git_raw.json \
      --chats-json temp/wr_chats_raw.json [--me 崔朋] [--json out.json]
  python3 tools/weekly_report_provenance_gate.py attribution --chats-json temp/wr_chats_raw.json
  python3 tools/weekly_report_provenance_gate.py selftest

退出码：0=全 PASS，1=存在违规，2=输入不可用（缺文件/JSON 坏/结构非法，fail-closed）。
只读：任何子命令都不写盘（--json 显式指定时才写）。
"""
import argparse
import json
import os
import re
import sys
import tempfile

ME_DEFAULT = "崔朋"
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
TOKEN_KINDS = ("git", "git-repo", "chat", "external")
PLACEHOLDER_TEXTS = {"无", "暂无", "None", "N/A", "n/a", "-"}


# ─────────────────────────── 载入与解析 ───────────────────────────
class InputError(Exception):
    """输入不可用 ⇒ rc=2（fail-closed，绝不当作 PASS）"""


def load_json(path, what):
    if not path or not os.path.isfile(path):
        raise InputError(f"{what} 不存在：{path}")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        raise InputError(f"{what} 不是合法 JSON：{path}（{exc}）")


def _sha_from_entry(entry):
    """git dump 的单个提交条目 → sha（兼容字符串行与 dict 两种真实形态）。"""
    if isinstance(entry, str):
        # 真实形态："MM-DD HH:MM|<sha>|<subject>"
        for part in entry.split("|"):
            part = part.strip()
            if SHA_RE.match(part):
                return part.lower()
        return None
    if isinstance(entry, dict):
        for key in ("sha", "hash", "commit", "full_sha", "commit_id"):
            val = entry.get(key)
            if isinstance(val, str) and SHA_RE.match(val.strip()):
                return val.strip().lower()
        # 兜底：把 dict 拍平成字符串再按 | 切
        for val in entry.values():
            if isinstance(val, str):
                got = _sha_from_entry(val)
                if got:
                    return got
    return None


def parse_git_dump(path):
    """→ {"shas": set, "repos": {repo: n_commits}, "n": int}"""
    data = load_json(path, "git dump")
    if not isinstance(data, dict):
        raise InputError("git dump 顶层必须是 {repo: [提交…]} 的映射")
    shas, repos, total = set(), {}, 0
    for repo, entries in data.items():
        if isinstance(entries, dict):
            entries = entries.get("commits") or entries.get("messages") or []
        if not isinstance(entries, list):
            raise InputError(f"git dump[{repo}] 不是列表")
        repos[repo] = 0
        for entry in entries:
            repos[repo] += 1
            total += 1
            sha = _sha_from_entry(entry)
            if sha:
                shas.add(sha)
    return {"shas": shas, "repos": repos, "n": total}


def _sender_name(msg):
    sender = msg.get("sender") or {}
    if isinstance(sender, dict):
        return (sender.get("name") or "").strip()
    return str(sender).strip()


def _mention_names(msg):
    out = []
    mentions = msg.get("mentions") or []
    if isinstance(mentions, list):
        for m in mentions:
            if isinstance(m, dict):
                nm = (m.get("name") or "").strip()
                if nm:
                    out.append(nm)
            elif isinstance(m, str):
                out.append(m.strip())
    return out


def chat_attribution(msg, me, aliases):
    """→ (attributed: bool, reason: str)。三条件任一满足即归属本人。"""
    names = {me} | set(aliases or [])
    content = msg.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    sender = _sender_name(msg)
    if sender in names:
        return True, f"sender={sender}"
    for nm in names:
        if nm and nm in content:
            return True, f"content 提及 {nm}"
    for nm in _mention_names(msg):
        if nm in names:
            return True, f"mentions={nm}"
    who = sender or "未知发送人"
    return False, f"sender={who} 且正文/mentions 均未提及 {'/'.join(sorted(names))}"


def parse_chats_dump(path, me, aliases):
    """→ {message_id: {attributed, reason, sender, content, create_time, group}}"""
    data = load_json(path, "chats dump")
    groups = []
    if isinstance(data, dict):
        if "messages" in data and isinstance(data["messages"], list):
            groups = [("", data)]
        else:
            for gname, gval in data.items():
                if isinstance(gval, dict):
                    groups.append((gname, gval))
                elif isinstance(gval, list):
                    groups.append((gname, {"messages": gval}))
    elif isinstance(data, list):
        groups = [("", {"messages": data})]
    else:
        raise InputError("chats dump 结构非法（应为 {群: {messages: […]}} 或消息列表）")

    out, n = {}, 0
    for gname, gval in groups:
        msgs = gval.get("messages") or []
        if not isinstance(msgs, list):
            raise InputError(f"chats dump[{gname}].messages 不是列表")
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            n += 1
            mid = (msg.get("message_id") or msg.get("id") or "").strip()
            if not mid:
                continue
            ok, reason = chat_attribution(msg, me, aliases)
            out[mid] = {
                "attributed": ok,
                "reason": reason,
                "sender": _sender_name(msg),
                "content": (msg.get("content") or "")[:120],
                "create_time": msg.get("create_time") or "",
                "group": gname or (msg.get("chat_id") or ""),
            }
    if n == 0:
        raise InputError("chats dump 里没有任何消息（空语料不得判 PASS）")
    return out


def iter_bullets(content, where="content.json"):
    """→ [(path, bullet_raw, text, provenance)]；结构非法 ⇒ InputError。"""
    if not isinstance(content, dict):
        raise InputError("content.json 顶层必须是对象")
    sections = content.get("sections")
    if not isinstance(sections, list) or not sections:
        raise InputError("content.json 缺 sections（空内容不得判 PASS）")
    found = []
    for sec in sections:
        if not isinstance(sec, dict):
            raise InputError("sections 元素必须是对象")
        stitle = sec.get("title") or "(无标题)"
        for idx, raw in enumerate(sec.get("bullets") or [], 1):
            found.append((f"{stitle}#{idx}", raw))
        for sub in sec.get("subs") or []:
            if not isinstance(sub, dict):
                raise InputError(f"subs 元素必须是对象（{stitle}）")
            btitle = sub.get("title") or "(无标题)"
            for idx, raw in enumerate(sub.get("bullets") or [], 1):
                found.append((f"{stitle} / {btitle}#{idx}", raw))
    if not found:
        raise InputError("content.json 里没有任何 bullet（空内容不得判 PASS）")
    return found


def _split_bullet(raw):
    """→ (text, provenance_list|None, shape)

    shape 语义（刻意区分「内容缺陷」与「输入不可用」）：
      LEGACY_STR / DICT_NO_PROV / DICT_STR_PROV / DICT → 正常形态
      NO_TEXT / BAD_PROV / BAD_TYPE                     → 内容缺陷 ⇒ rc=1 违规
    （rc=2 只留给文件级不可用：缺文件、JSON 坏、空 sections、dump 结构非法）
    """
    if isinstance(raw, str):
        return raw, None, "LEGACY_STR"
    if isinstance(raw, dict):
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            return "", None, "NO_TEXT"
        prov = raw.get("provenance")
        if prov is None:
            return text, None, "DICT_NO_PROV"
        if isinstance(prov, str):
            return text, [prov], "DICT_STR_PROV"
        if isinstance(prov, list) and all(isinstance(x, str) for x in prov):
            return text, prov, "DICT"
        return text, None, "BAD_PROV"
    return "", None, "BAD_TYPE"


# ─────────────────────────── 校验 ───────────────────────────
def check_content(content, git, chats, me, aliases):
    """→ {"bullets": n, "violations": [...], "info": {...}}"""
    violations, items, chat_only, ext_only = [], [], [], []
    for path, raw in iter_bullets(content):
        text, prov, shape = _split_bullet(raw)
        rec = {"path": path, "text": text[:90], "shape": shape, "tokens": prov or []}
        items.append(rec)
        if shape in ("NO_TEXT", "BAD_PROV", "BAD_TYPE"):
            rec["verdict"] = "FAIL"
            violations.append({
                "code": {"NO_TEXT": "BULLET_NO_TEXT", "BAD_PROV": "PROVENANCE_BAD_SHAPE",
                         "BAD_TYPE": "BULLET_BAD_TYPE"}[shape],
                "path": path,
                "detail": {"NO_TEXT": "bullet 对象缺 text（或 text 为空）",
                           "BAD_PROV": "provenance 既不是字符串也不是字符串列表",
                           "BAD_TYPE": "bullet 不是字符串或对象"}[shape],
                "text": repr(raw)[:120],
            })
            continue
        if text.strip() in PLACEHOLDER_TEXTS:
            rec["verdict"] = "PLACEHOLDER"
            continue
        if prov is None or len(prov) == 0:
            rec["verdict"] = "FAIL"
            violations.append({
                "code": "PROVENANCE_MISSING", "path": path,
                "detail": ("bullet 是纯字符串，没有 provenance 字段"
                           if prov is None else "provenance 为空列表"),
                "text": text[:120],
            })
            continue

        bad, ok_tokens, chat_tokens, ext_tokens = [], [], [], []
        for tok in prov:
            if ":" not in tok:
                bad.append(("TOKEN_UNKNOWN_KIND", f"token 缺类型前缀：{tok!r}"))
                continue
            kind, value = tok.split(":", 1)
            value = value.strip()
            if kind not in TOKEN_KINDS:
                bad.append(("TOKEN_UNKNOWN_KIND", f"未知 token 类型 {kind!r}（{tok!r}）"))
                continue
            if kind == "git":
                if not SHA_RE.match(value):
                    bad.append(("GIT_SHA_MALFORMED", f"{tok!r} 不是 7~40 位十六进制 sha"))
                elif not any(s.startswith(value.lower()) or value.lower().startswith(s[:7])
                             for s in git["shas"]):
                    bad.append(("GIT_SHA_NOT_IN_DUMP", f"{tok!r} 不在本周 git dump 中"))
                else:
                    ok_tokens.append(tok)
            elif kind == "git-repo":
                if value not in git["repos"]:
                    bad.append(("GIT_REPO_NOT_FOUND", f"{tok!r} 不在 dump 里"))
                elif git["repos"][value] == 0:
                    bad.append(("GIT_REPO_NO_COMMITS", f"{tok!r} 本周 0 提交，不能作汇总证据"))
                else:
                    ok_tokens.append(tok)
            elif kind == "external":
                reason = value.strip()
                if len(reason) < 4:
                    bad.append(("EXTERNAL_REASON_TOO_SHORT",
                                f"{tok!r} 外部来源须写明 ≥4 字理由"
                                "（如「上级指派」「年度目标」），不接受空泛占位"))
                else:
                    ok_tokens.append(tok)
                    ext_tokens.append(tok)
            else:  # chat
                info = chats.get(value)
                if info is None:
                    bad.append(("CHAT_ID_NOT_FOUND", f"{tok!r} 不在群聊 dump 中"))
                elif not info["attributed"]:
                    bad.append(("CHAT_UNATTRIBUTED",
                                f"{tok!r} 该消息不属于本人：{info['reason']}"
                                f"（发送人 {info['sender']}，内容 {info['content'][:60]!r}）"))
                else:
                    ok_tokens.append(tok)
                    chat_tokens.append(tok)
        if bad:
            rec["verdict"] = "FAIL"
            for code, detail in bad:
                violations.append({"code": code, "path": path, "detail": detail,
                                   "text": text[:120]})
        else:
            rec["verdict"] = "PASS"
        if ok_tokens and len(chat_tokens) == len(ok_tokens) and not bad:
            chat_only.append(path)
        if ok_tokens and len(ext_tokens) == len(ok_tokens) and not bad:
            ext_only.append(path)

    all_chat = set()
    for tok in [t for it in items for t in it["tokens"]]:
        if isinstance(tok, str) and tok.startswith("chat:"):
            all_chat.add(tok.split(":", 1)[1])
    return {
        "me": me,
        "aliases": sorted(aliases or []),
        "bullets": len(items),
        "violations": violations,
        "items": items,
        "info": {
            "chat_only_bullets": chat_only,
            "external_only_bullets": ext_only,
            "external_tokens": sorted({t for it in items for t in it["tokens"]
                                       if isinstance(t, str) and t.startswith("external:")}),
            "chat_tokens_used": sorted(all_chat),
            "chat_messages_total": len(chats),
            "chat_messages_attributed": sum(1 for v in chats.values() if v["attributed"]),
            "git_commits_total": git["n"],
            "git_repos_with_commits": sorted(r for r, n in git["repos"].items() if n),
        },
    }


# ─────────────────────────── CLI ───────────────────────────
def cmd_check(args):
    content = load_json(args.content_json, "content.json")
    git = parse_git_dump(args.git_json)
    chats = parse_chats_dump(args.chats_json, args.me, args.alias)
    res = check_content(content, git, chats, args.me, args.alias)

    print(f"=== weekly_report_provenance_gate: {args.content_json} ===")
    print(f"汇报人 me={args.me}  aliases={sorted(args.alias)}")
    print(f"git: {res['info']['git_commits_total']} 提交 / "
          f"{len(res['info']['git_repos_with_commits'])} 个有提交的仓")
    print(f"chat: {res['info']['chat_messages_total']} 条消息，"
          f"其中归属本人 {res['info']['chat_messages_attributed']} 条")
    print(f"bullets: {res['bullets']}")
    print("-" * 60)
    for it in res["items"]:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "PLACEHOLDER": "SKIP"}[it["verdict"]]
        toks = " ".join(it["tokens"]) if it["tokens"] else "(无)"
        print(f"[{mark}] {it['path']}")
        print(f"       {it['text']}")
        print(f"       provenance: {toks}")
    if res["violations"]:
        print("-" * 60)
        for v in res["violations"]:
            print(f"  FAIL {v['code']}  @ {v['path']}  — {v['detail']}")
    if res["info"]["external_only_bullets"]:
        print("-" * 60)
        print("提示（非违规）以下条目**全部**靠 external 声明支撑（无本周产出证据），"
              "请人工确认确系本人职责：")
        for p2 in res["info"]["external_only_bullets"]:
            print(f"  · {p2}")
    if res["info"]["chat_only_bullets"]:
        print("-" * 60)
        print("提示（非违规）以下条目仅靠群聊单条证据支撑，请人工确认措辞为"
              "「知悉/协助」而非认领：")
        for p in res["info"]["chat_only_bullets"]:
            print(f"  · {p}")
    print(f"\n=== {'ALL PASS ✅' if not res['violations'] else str(len(res['violations'])) + ' FAIL ❌ 禁止通知用户'} ===")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print(f"(JSON 已写入 {args.json})")
    return 0 if not res["violations"] else 1


def cmd_attribution(args):
    chats = parse_chats_dump(args.chats_json, args.me, args.alias)
    ok = [m for m, v in chats.items() if v["attributed"]]
    print(f"=== 群聊归属判定（me={args.me}）===")
    print(f"消息总数 {len(chats)}，归属本人 {len(ok)}，排除 {len(chats) - len(ok)}")
    print("-" * 60)
    for mid, v in chats.items():
        mark = "OK  " if v["attributed"] else "EXCL"
        print(f"[{mark}] {v['create_time']} {v['sender']:<8} | {v['content'][:60]!r}")
        print(f"        chat:{mid}   reason={v['reason']}")
    print("-" * 60)
    print("可安全引用的 chat token：" + (", ".join(f"chat:{m}" for m in ok) or "(无)"))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"me": args.me, "messages": chats}, fh, ensure_ascii=False, indent=1)
        print(f"(JSON 已写入 {args.json})")
    return 0


# ─────────────────────────── 自测（含负控） ───────────────────────────
def _selftest():
    """21 例：正例 6 / 负控 13 / 结构性 2。返回 (ok, total, 明细)。"""
    cases = []

    def mk(tmp):
        git = {"repoA": ["09-30 08:24|ba97a0d0a|docs(kiosk): 补录", "09-29 10:00|deadbee12|feat: x"],
               "repoB": []}
        chats = {"g1": {"messages": [
            {"message_id": "om_MINE1", "content": "崔朋 你看下这个", "create_time": "2026-09-28 10:00",
             "sender": {"name": "王冰岩"}},
            {"message_id": "om_ATME", "content": "@崔朋 帮忙确认", "create_time": "2026-09-28 11:00",
             "sender": {"name": "张义群"}},
            {"message_id": "om_SENT", "content": "我这边提交了", "create_time": "2026-09-28 12:00",
             "sender": {"name": "崔朋"}},
            {"message_id": "om_OTHERS", "content": "@周永清 高德渠道无触发数据", "create_time": "2026-09-28 15:31",
             "sender": {"name": "马腾飞"}},
            {"message_id": "om_MENTION", "content": "请处理", "create_time": "2026-09-29 09:00",
             "sender": {"name": "王冰岩"}, "mentions": [{"name": "崔朋"}]},
        ]}}
        gp = os.path.join(tmp, "git.json")
        cp = os.path.join(tmp, "chats.json")
        json.dump(git, open(gp, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(chats, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
        return gp, cp

    def run(tmp, content, gp, cp, expect, name, me="崔朋", alias=()):
        try:
            git = parse_git_dump(gp)
            chats = parse_chats_dump(cp, me, list(alias))
            res = check_content(content, git, chats, me, list(alias))
            got_rc, codes = (0 if not res["violations"] else 1), [v["code"] for v in res["violations"]]
        except InputError as exc:
            got_rc, codes = 2, [f"InputError:{exc}"]
        exp_rc = {"PASS": 0, "FAIL": 1, "ERR": 2}[expect]
        ok = got_rc == exp_rc
        cases.append((name, expect, got_rc, codes, ok))
        return ok

    with tempfile.TemporaryDirectory() as tmp:
        gp, cp = mk(tmp)

        def C(sections):
            return {"greeting": "x", "intro": "y", "sections": sections}

        # ---- 正例 ----
        run(tmp, C([{"title": "一", "bullets": [
            {"text": "供应链修复", "provenance": ["git:ba97a0d0a"]}]}]), gp, cp, "PASS", "P1 git sha 命中")
        run(tmp, C([{"title": "一", "bullets": [
            {"text": "协调事项", "provenance": ["chat:om_SENT"]}]}]), gp, cp, "PASS", "P2 chat 发送人=本人")
        run(tmp, C([{"title": "一", "bullets": [
            {"text": "协调事项", "provenance": ["chat:om_ATME"]}]}]), gp, cp, "PASS", "P3 chat @本人")
        run(tmp, C([{"title": "一", "bullets": [
            {"text": "协调事项", "provenance": ["chat:om_MENTION"]}]}]), gp, cp, "PASS", "P3b chat mentions 含本人")
        run(tmp, C([{"title": "一", "subs": [{"title": "1", "bullets": [
            {"text": "提交概况", "provenance": ["git-repo:repoA", "git:deadbee12"]}]}]}]), gp, cp, "PASS", "P4 subs+git-repo")
        run(tmp, C([{"title": "三", "bullets": [{"text": "无", "provenance": []}]}]), gp, cp, "PASS", "P5 占位豁免")

        # ---- 负控 ----
        run(tmp, C([{"title": "一", "bullets": ["纯字符串无溯源"]}]), gp, cp, "FAIL", "NC1 旧形态字符串")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": []}]}]), gp, cp, "FAIL", "NC2 空 provenance")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["git:fffffffff"]}]}]),
            gp, cp, "FAIL", "NC3 git sha 不在 dump")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["chat:om_NOTEXIST"]}]}]),
            gp, cp, "FAIL", "NC4 chat id 不存在")
        # NC5 = 真实 09-30 事故复现：引用的是「别人的事」
        run(tmp, C([{"title": "一", "subs": [{"title": "3. 门店与设备侧业务问题响应", "bullets": [
            {"text": "高德渠道无弹窗触发数据", "provenance": ["chat:om_OTHERS"]},
            {"text": "自动退款限额显示-", "provenance": ["chat:om_OTHERS"]},
        ]}]}]), gp, cp, "FAIL", "NC5 ★真实事故复现（他人消息冒充本人事项）")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["webfetch:http://a"]}]}]),
            gp, cp, "FAIL", "NC6 未知 token 类型")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["ba97a0d0a"]}]}]),
            gp, cp, "FAIL", "NC6b 无类型前缀的裸 sha")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["git-repo:repoB"]}]}]),
            gp, cp, "FAIL", "NC7 git-repo 本周 0 提交")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["git-repo:repoZ"]}]}]),
            gp, cp, "FAIL", "NC8 git-repo 不存在")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["git:zzzzzzzz"]}]}]),
            gp, cp, "FAIL", "NC9 sha 形态非法")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": "chat:om_SENT"}]}]),
            gp, cp, "PASS", "NC10 单个字符串 provenance（宽容形态，仍校验）")
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": 42}]}]),
            gp, cp, "FAIL", "NC11 provenance 类型非法")
        # 归属判据可配置：把 me 换成张义群后，om_SENT（发送人崔朋）应不再通过
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["chat:om_SENT"]}]}]),
            gp, cp, "FAIL", "NC12 --me 换人后原证据失效（判据确实生效）", me="张义群")
        # alias：把 me 换成某人 + alias 崔朋 ⇒ 仍通过（证明别名通路有效）
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["chat:om_SENT"]}]}]),
            gp, cp, "PASS", "P6 alias 通路有效", me="Sampan", alias=("崔朋",))
        # external：唯一人工声明通道（计划/长期目标），须写明理由
        run(tmp, C([{"title": "二", "bullets": [
            {"text": "下季度推进 X", "provenance": ["external:年度目标 O3"]}]}]),
            gp, cp, "PASS", "P7 external 合法理由（长期目标/上级指派）")
        run(tmp, C([{"title": "二", "bullets": [
            {"text": "x", "provenance": ["external:ab"]}]}]),
            gp, cp, "FAIL", "NC16 external 理由过短（空泛占位不接受）")
        run(tmp, C([{"title": "二", "bullets": [
            {"text": "x", "provenance": ["external:"]}]}]),
            gp, cp, "FAIL", "NC17 external 空理由")
        run(tmp, C([{"title": "二", "bullets": [
            {"text": "x", "provenance": ["external:上级指派", "git:deadbee12"]}]}]),
            gp, cp, "PASS", "P7b external 与 git 混用仍逐条校验")

        # ---- 结构性（fail-closed）----
        bad_git = os.path.join(tmp, "bad_git.json")
        json.dump({"repoA": "not-a-list"}, open(bad_git, "w", encoding="utf-8"))
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["git:ba97a0d0a"]}]}]),
            bad_git, cp, "ERR", "NC13 git dump 结构非法 ⇒ rc=2")
        empty_chats = os.path.join(tmp, "empty_chats.json")
        json.dump({"g1": {"messages": []}}, open(empty_chats, "w", encoding="utf-8"))
        run(tmp, C([{"title": "一", "bullets": [{"text": "x", "provenance": ["git:ba97a0d0a"]}]}]),
            gp, empty_chats, "ERR", "NC14 空群聊语料 ⇒ rc=2（不得判 PASS）")
        run(tmp, C([{"title": "一", "bullets": []}]), gp, cp, "ERR", "NC15 无任何 bullet ⇒ rc=2")

    passed = sum(1 for c in cases if c[4])
    for name, expect, got, codes, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  expect={expect} got_rc={got} {codes if codes else ''}")
    print(f"\n=== selftest {passed}/{len(cases)} ===")
    return passed == len(cases), len(cases)


def cmd_selftest(_args):
    ok, total = _selftest()
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="周报条目归属机械闸门")
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("check", help="校验 content.json 每条 bullet 的归属证据")
    p1.add_argument("--content-json", required=True)
    p1.add_argument("--git-json", required=True)
    p1.add_argument("--chats-json", required=True)
    p1.add_argument("--me", default=ME_DEFAULT)
    p1.add_argument("--alias", action="append", default=[])
    p1.add_argument("--json", default=None)
    p1.set_defaults(func=cmd_check)

    p2 = sub.add_parser("attribution", help="列出群聊消息归属判定（用于产出 provenance）")
    p2.add_argument("--chats-json", required=True)
    p2.add_argument("--me", default=ME_DEFAULT)
    p2.add_argument("--alias", action="append", default=[])
    p2.add_argument("--json", default=None)
    p2.set_defaults(func=cmd_attribution)

    p3 = sub.add_parser("selftest", help="内置正例/负控自测")
    p3.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except InputError as exc:
        print(f"[INPUT ERROR rc=2] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
