#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绩效取数（源③ 飞书）：按**季度窗口**统计会话消息总量与本人发言。

口径要点（与绩效 SOP 一致；窗口由 quarter_window 现算，禁写死季度）：
  * 只读：lark-cli `im +chat-list` / `im +chat-messages-list`，只取不写。
  * 窗口：服务端 --start/--end 交给 lark-cli，同时**客户端二次过滤** create_time
    （服务端边界与时区口径未必等同 +08:00 半开区间）⇒ 双保险，并统计窗口外丢弃数。
  * 归属：本人发言 = sender.name 含 --me（默认 崔朋）或 sender_id ∈ --me-id；
    时间不可判定/归属不可判定的一律不计入本人产出（宁少不错）。
  * 输出：会话数 / 消息总数 / 本人发言数 / 本人发言按月分布；--json 落盘。
  * 分页完整性（★2026-10-09 增）：lark-cli `--page-limit` 是**页数上限**（1–1000），
    单会话最多取 page_limit×page_size 条。旧值 50 页×50 条=2500 ⇒ 大群被静默截断
    （实测 6 个会话恰好 2500）。现默认取上限 1000 页，并用 lark-cli 返回的
    `meta.pagination.complete` 判定是否真取完；未取完 ⇒ 标记 truncated 且非零退出
    （下界 ≠ 真值，禁当完整结果用）。

CLI:
    python3 tools/feishu_chat_stats.py --quarter Q3-2026 [--json /tmp/out.json]
    python3 tools/feishu_chat_stats.py --since 2026-07-01T00:00:00+08:00 --until 2026-10-01T00:00:00+08:00
    python3 tools/feishu_chat_stats.py --dry-run --quarter Q3-2026
    python3 tools/feishu_chat_stats.py --selftest
"""
import argparse
import datetime as _dt
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import quarter_window as qw
except Exception:  # pragma: no cover
    qw = None

LARK = os.environ.get("LARK_CLI", "/opt/homebrew/bin/lark-cli")
DEFAULT_ME = os.environ.get("GA_ME_NAME", "崔朋")
# 分页：--page-limit 单位是**页**（lark-cli 上限 1000）；--page-size ≤50。
# 旧值 50 ⇒ 单会话最多 50×50=2500 条（大群静默截断，实测 6 会话恰好 2500）。
# ★超时升档：大群（数千条 × 50 条/页）可能超过单次 CLI 超时（rc=124），
#   命中超时自动按倍数升档重试，避免整批数字被单个大群拖成缺失。
TIMEOUT_ESCALATE = float(os.environ.get("GA_FEISHU_TIMEOUT_ESCALATE", "4"))
TIMEOUT_MAX = int(os.environ.get("GA_FEISHU_TIMEOUT_MAX", "1800"))
PAGE_LIMIT_DEFAULT = int(os.environ.get("GA_FEISHU_PAGE_LIMIT", "1000"))
PAGE_SIZE_DEFAULT = int(os.environ.get("GA_FEISHU_PAGE_SIZE", "50"))
# ★分块续拉（2026-10-09）：`--page-all` 一把梭在大群上会跑满/超过单次 CLI 超时
#   （实测单群 3776 条需 >3 分钟），且中途失败整只会话丢数据。改为自管 page_token
#   逐块拉：每块单独计时与重试、进度可留痕、块间幂等 ⇒ 超时只影响该块，不丢整会话。
# ★★分块每块必须带 `--page-all`（2026-10-09 真机 A/B 证）：lark-cli 的 `--page-limit`
#   只对 `--page-all` 生效——不带它时 `--page-limit N` 被忽略、每次只回 1 页
#   （实测 --page-size 2：不带 ⇒ pagination.pages=1/items=2；带 --page-limit 3 ⇒ pages=3）。
#   漏带 ⇒ chunk_pages 配置失效、调用次数放大 ~cp 倍（大群数百次子进程），
#   故下方分块 args 恒定带 `--page-all`，且自测断言该 token 必在（T8c 负控）。
CHUNK_PAGES_DEFAULT = int(os.environ.get("GA_FEISHU_CHUNK_PAGES", "20"))
CHAT_LIST_PAGE_LIMIT = int(os.environ.get("GA_FEISHU_CHATLIST_PAGE_LIMIT", "50"))


def _find_list(obj, keys):
    """在任意嵌套结构里找第一个「元素为 dict 且含任一 key」的列表。

    lark-cli 的 JSON 包装层不稳定（有时裸 list，有时 {data:{items:[...]}}），
    故按「元素特征」递归找，比按固定路径取值稳。
    """
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in keys):
            return obj
        for x in obj:
            r = _find_list(x, keys)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_list(v, keys)
            if r:
                return r
    return None


def _ts(v):
    """create_time 归一为**秒**（int）。飞书给字符串秒；兼容毫秒与 ISO。无法判定⇒None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        s = str(v).strip()
        if not s:
            return None
        if s.isdigit():
            n = float(s)
        else:
            try:
                return int(_dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
            except Exception:
                return None
    if n > 1e12:  # 毫秒
        n = n / 1000.0
    return int(n)


def in_window(ts, since_ts, until_ts):
    """半开区间 [since, until)。ts=None 一律不计入（时间不可判定 ≠ 窗口内）。"""
    return ts is not None and since_ts <= ts < until_ts


def month_of(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m")


def lark_run(args, timeout=180, env=None):
    """跑 lark-cli；返回 (rc, stdout, stderr)。所有调用集中在此，便于自测替换。"""
    e = dict(os.environ)
    e["PATH"] = os.path.dirname(LARK) + os.pathsep + e.get("PATH", "")
    if env:
        e.update(env)
    try:
        p = subprocess.run([LARK] + list(args), env=e, timeout=timeout,
                           capture_output=True, text=True)
    except FileNotFoundError:
        return (127, "", "lark-cli not found: %s" % LARK)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout after %ss" % timeout)
    return (p.returncode, p.stdout or "", p.stderr or "")


def _is_timeout(err, rc=None):
    """统一超时判据（单一源）：超时类错误的唯一识别点。

    向后兼容旧行为（只认文案里的 timeout）并额外认 rc=124（subprocess 超时约定）。
    禁在别处再写一份「timeout 子串」判据——两处判据必然漂移。
    """
    if rc is not None and int(rc) == 124:
        return True
    return bool(err) and "timeout" in str(err).lower()


def parse_json(text):
    """从 lark-cli 输出里抽 JSON（可能带前导日志行）。失败⇒None。"""
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for ch in ("{", "["):
        i = s.find(ch)
        if i >= 0:
            try:
                return json.loads(s[i:])
            except Exception:
                continue
    return None


def chat_list(timeout=180):
    """返回会话列表 [{'chat_id','name'}]。"""
    args = ["im", "+chat-list", "--types=p2p,group", "--page-all", "--page-limit", "50",
            "--format", "json"]
    rc, out, err = lark_run(args, timeout=timeout)
    if rc != 0:
        return None, "chat-list rc=%s %s" % (rc, (err or out)[:300])
    data = parse_json(out)
    items = _find_list(data, ("chat_id", "name")) or []
    chats = []
    for it in items:
        cid = it.get("chat_id") or it.get("id")
        if cid:
            chats.append({"chat_id": cid, "name": it.get("name") or it.get("title") or cid})
    return chats, ""


def parse_pagination(payload):
    """从 lark-cli 输出解析分页元信息。

    返回 dict：complete(bool|None) / pages(int|None) / items(int|None) /
    next_token(str) / total(int|None) / source('meta'|'absent')。
    complete=None ⇒ 元信息不可得（旧版 CLI），调用方须回退启发式判定。
    """
    out = {"complete": None, "pages": None, "items": None,
           "next_token": "", "total": None, "source": "absent"}
    if not isinstance(payload, dict):
        return out
    meta = payload.get("meta")
    pag = meta.get("pagination") if isinstance(meta, dict) else None
    if isinstance(pag, dict):
        if isinstance(pag.get("complete"), bool):
            out["complete"] = pag["complete"]
        for k in ("pages", "items"):
            v = pag.get(k)
            if isinstance(v, int):
                out[k] = v
        if isinstance(pag.get("next_token"), str):
            out["next_token"] = pag["next_token"]
        out["source"] = "meta"
    data = payload.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("total"), int):
            out["total"] = data["total"]
        if out["complete"] is None and isinstance(data.get("has_more"), bool):
            out["complete"] = not data["has_more"]
            if out["source"] == "absent":
                out["source"] = "has_more"
            if isinstance(data.get("page_token"), str):
                out["next_token"] = out["next_token"] or data["page_token"]
    return out


def page_meta(n_items, page_limit, page_size, payload=None):
    """判定本会话是否取全（截断则下界，禁当完整结果）。

    优先 lark-cli 的 meta.pagination.complete；元信息缺席时回退
    「条数==页数上限×页大小」启发式（触顶即可疑，宁可标截断）。
    """
    m = parse_pagination(payload)
    cap = int(page_limit) * int(page_size)
    hit_cap = n_items >= cap
    if m["complete"] is None:
        m["complete"] = not hit_cap
        m["truncated"] = hit_cap
        m["source"] = "heuristic"
    else:
        m["truncated"] = (m["complete"] is False) or hit_cap
    m["cap"] = cap
    m["fetched"] = n_items
    return m


def chat_messages(chat_id, since="", until="", timeout=180,
                  page_limit=None, page_size=None):
    """取单会话消息（服务端粗窗）。返回 (msgs, err, meta)。

    meta = page_meta(...)：分页完整性（truncated=True ⇒ 下界）。
    """
    pl = PAGE_LIMIT_DEFAULT if page_limit is None else page_limit
    ps = PAGE_SIZE_DEFAULT if page_size is None else page_size
    args = ["im", "+chat-messages-list", "--chat-id", chat_id, "--page-all",
            "--page-limit", str(pl), "--page-size", str(ps),
            "--order", "asc", "--format", "json"]
    if since:
        args += ["--start", since]
    if until:
        args += ["--end", until]
    rc, out, err = lark_run(args, timeout=timeout)
    if rc != 0:
        return None, "rc=%s %s" % (rc, (err or out)[:200]), None
    data = parse_json(out)
    items = _find_list(data, ("create_time", "message_id")) or []
    return items, "", page_meta(len(items), pl, ps, data)


def _find_pagination(obj):
    """递归找 meta.pagination（lark-cli 的包装层不稳定）。找不到⇒None。"""
    if isinstance(obj, dict):
        p = obj.get("pagination")
        if isinstance(p, dict):
            return p
        for v in obj.values():
            r = _find_pagination(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_pagination(v)
            if r is not None:
                return r
    return None


def chat_messages_chunked(chat_id, since="", until="", timeout=180,
                          page_limit=None, page_size=None, chunk_pages=None,
                          log=None, name="", chunk_retries=1):
    """分块续拉单会话消息（自管 page_token）。返回 (msgs, err, meta)。

    与 chat_messages 的差别只在**怎么翻页**：
      * 每块最多 chunk_pages 页，用上一块返回的 next_token 续拉；
      * 每块单独计时，超时只重拉该块（token 幂等 ⇒ 不重不漏，也不丢整会话）；
      * 进度可留痕（log 回调），失败时 meta.resume_token 可用于续拉。
    meta 增键：chunked/chunks/chunk_timeouts/chunk_pages/pages_done/resume_token。
    """
    pl = PAGE_LIMIT_DEFAULT if page_limit is None else int(page_limit)
    ps = PAGE_SIZE_DEFAULT if page_size is None else int(page_size)
    cp = CHUNK_PAGES_DEFAULT if chunk_pages is None else int(chunk_pages)
    cp = max(1, min(cp, pl))
    msgs, token, pages_done, chunks, touts = [], None, 0, 0, 0
    complete, server_total, last_err, rc = None, None, "", 0
    while pages_done < pl:
        take = min(cp, pl - pages_done)
        args = ["im", "+chat-messages-list", "--chat-id", chat_id,
                "--page-all",                       # ★必须：否则 --page-limit 失效（每次仅 1 页）
                "--page-limit", str(take), "--page-size", str(ps),
                "--order", "asc", "--format", "json"]
        if token:
            args += ["--page-token", token]
        if since:
            args += ["--start", since]
        if until:
            args += ["--end", until]
        rc, out = 0, ""
        for _attempt in range(1 + max(0, int(chunk_retries))):
            rc, out, err = lark_run(args, timeout=timeout)
            if rc == 0:
                break
            last_err = "rc=%s %s" % (rc, (err or out)[:200])
            if not _is_timeout(last_err, rc=rc):
                break           # 非超时（鉴权/参数类）重试无用，如实报错
            touts += 1
            if log:
                log("  ↳ %s 第 %d 块超时（timeout=%ss）⇒重拉该块"
                    % (name or chat_id, chunks + 1, timeout))
        if rc != 0:
            break
        data = parse_json(out)
        items = _find_list(data, ("create_time", "message_id")) or []
        msgs.extend(items)
        pag = _find_pagination(data) or {}
        try:
            pages_done += int(pag.get("pages") or take)
        except Exception:
            pages_done += take
        chunks += 1
        if isinstance(pag.get("total"), int):
            server_total = pag["total"]
        token = pag.get("next_token") or ""
        if isinstance(pag.get("complete"), bool):
            complete = pag["complete"]
        else:
            # 元信息缺席（旧版 lark-cli / 包装层变化）⇒ 回退 has_more，
            # 两者皆无则保持 None（下面按「未取完」处理，宁可标下界）
            hm = pag.get("has_more")
            if not isinstance(hm, bool):
                _d = data.get("data") if isinstance(data, dict) else None
                if isinstance(_d, dict):
                    hm = _d.get("has_more")
            if not isinstance(hm, bool) and isinstance(data, dict):
                # 包装层差异：has_more 也可能落在顶层（官方 API 在 data 下，两者都收）
                hm = data.get("has_more")
            if isinstance(hm, bool):
                complete = not hm
                if log:
                    log("  · %s 第 %d 块无 pagination.complete ⇒ 按 data.has_more=%s 回退"
                        % (name or chat_id, chunks, hm))
        if log:
            log("  · %s 第 %d 块 pages=%s items=%s 累计=%d token=%s complete=%s"
                % (name or chat_id, chunks, pag.get("pages"), len(items),
                   len(msgs), "有" if token else "无", complete))
        if complete is True or not token:
            break
    if rc != 0:
        meta = page_meta(len(msgs), pl, ps, None)
        meta.update({"complete": False, "truncated": True, "source": "chunk_error",
                     "resume_token": token or None, "error": last_err})
    else:
        meta = page_meta(len(msgs), pl, ps,
                         {"meta": {"pagination": {"complete": complete,
                                                  "pages": pages_done,
                                                  "items": len(msgs),
                                                  "next_token": token,
                                                  "total": server_total}}})
        if complete is None:
            # 元信息与 has_more 都拿不到 ⇒ 无法证明取全（旧实现会落回启发式并可能判 complete）
            meta["complete"] = False
            meta["truncated"] = True
            meta["source"] = "chunk_unknown"
        elif token and complete is not True:
            meta["complete"] = False
            meta["truncated"] = True
            meta["source"] = "chunk_resume"
    meta.update({"chunked": True, "chunks": chunks, "chunk_timeouts": touts,
                 "chunk_pages": cp, "pages_done": pages_done})
    return msgs, (last_err if rc != 0 else ""), meta


def is_own(msg, me=DEFAULT_ME, me_ids=()):
    """本人发言判定：sender.name 含 me ∨ sender.id ∈ me_ids。判定不了⇒False（宁少不错）。"""
    s = msg.get("sender") or {}
    if not isinstance(s, dict):
        return False
    if me and me in str(s.get("name") or ""):
        return True
    sid = s.get("id") or s.get("sender_id") or ""
    blob = json.dumps(sid, ensure_ascii=False) if not isinstance(sid, str) else sid
    for x in me_ids:
        if x and x in blob:
            return True
    return False


def summarize(records, since_ts, until_ts, me=DEFAULT_ME, me_ids=()):
    """records=[{'chat_id','name','messages':[...]}] ⇒ 统计 dict。

    只统计窗口内消息；窗口外记 dropped_in_chat/dropped_bad_time（可观测，防静默丢数据）。
    """
    out = {"chats": 0, "chats_with_msg": 0, "messages": 0, "own_messages": 0,
           "own_by_month": {}, "total_by_month": {}, "dropped_bad_time": 0,
           "per_chat": [], "truncated_chats": [],
           "pagination": {"chats_reported": 0, "chats_incomplete": 0,
                          "pages_total": 0, "items_total": 0, "server_total": 0}}
    for r in records:
        msgs = r.get("messages") or []
        keep, bad = [], 0
        for m in msgs:
            ts = _ts(m.get("create_time"))
            if ts is None:
                bad += 1
                continue
            if in_window(ts, since_ts, until_ts):
                keep.append((ts, m))
        own = [(t, m) for (t, m) in keep if is_own(m, me, me_ids)]
        out["chats"] += 1
        out["dropped_bad_time"] += bad
        if keep:
            out["chats_with_msg"] += 1
        for t, _m in keep:
            k = month_of(t)
            out["total_by_month"][k] = out["total_by_month"].get(k, 0) + 1
        for t, _m in own:
            k = month_of(t)
            out["own_by_month"][k] = out["own_by_month"].get(k, 0) + 1
        out["messages"] += len(keep)
        out["own_messages"] += len(own)
        out["per_chat"].append({"chat_id": r.get("chat_id"), "name": r.get("name"),
                                "messages": len(keep), "own": len(own),
                                "dropped_bad_time": bad,
                                "server_total": (r.get("pagination") or {}).get("total")})
        pg = r.get("pagination") or {}
        if pg:
            p = out["pagination"]
            p["chats_reported"] += 1
            p["pages_total"] += pg.get("pages") or 0
            p["items_total"] += pg.get("fetched") or 0
            p["server_total"] += pg.get("total") or 0
            if pg.get("truncated"):
                p["chats_incomplete"] += 1
                out["truncated_chats"].append({
                    "chat_id": r.get("chat_id"), "name": r.get("name"),
                    "fetched": pg.get("fetched"), "cap": pg.get("cap"),
                    "pages": pg.get("pages"), "total_server": pg.get("total"),
                    "source": pg.get("source")})
    out["per_chat"].sort(key=lambda x: (-x["messages"], x["name"] or ""))
    for k in ("own_by_month", "total_by_month"):
        out[k] = dict(sorted(out[k].items()))
    return out


def filter_chats(chats, include=(), skip=()):
    """按会话名子串过滤：先 include（非空则只留命中者），再 skip（丢弃命中者）。

    整库遍历会扫进运维告警群等噪声（实测 20:05 报错群 121 块/6050 条）⇒ 收窄
    必须是一等参数、且过滤结果要落进产物，否则「到底扫了哪些会话」不可追溯。
    """
    inc = [s for s in (include or []) if s]
    skp = [s for s in (skip or []) if s]
    kept, dropped_inc, dropped_skp = [], [], []
    for c in chats or []:
        nm = c.get("name") or ""
        if inc and not any(s in nm for s in inc):
            dropped_inc.append(nm)
            continue
        if skp and any(s in nm for s in skp):
            dropped_skp.append(nm)
            continue
        kept.append(c)
    return kept, {"include": inc, "skip": skp,
                  "dropped": len(dropped_inc) + len(dropped_skp),
                  "dropped_by": {"include_miss": len(dropped_inc),
                                 "skip_hit": len(dropped_skp)}}


def collect(chats, since="", until="", limit=None, timeout=180, log=None,
            page_limit=None, page_size=None, chunked=False, chunk_pages=None,
            include_chats=(), skip_chats=()):
    """遍历会话取消息 ⇒ records=[{'chat_id','name','messages','error','pagination'}]。

    ★超时自愈：大群（数千条×50 条/页）可能超过单次 CLI 超时（rc=124）⇒ 命中超时
    自动升档重试（TIMEOUT_ESCALATE 倍，上限 TIMEOUT_MAX），避免整批数字因单个大群缺失。
    """
    recs = []
    chats, _flt = filter_chats(chats, include_chats, skip_chats)
    if _flt["dropped"] and log:
        log("会话过滤：%d 个被排除（include 未命中 %d / skip 命中 %d），余 %d 个"
            % (_flt["dropped"], _flt["dropped_by"]["include_miss"],
               _flt["dropped_by"]["skip_hit"], len(chats)))
    for i, c in enumerate(chats, 1):
        if limit and i > limit:
            break
        to = timeout

        def _fetch(_to):
            if chunked:
                return chat_messages_chunked(c["chat_id"], since, until, timeout=_to,
                                             page_limit=page_limit, page_size=page_size,
                                             chunk_pages=chunk_pages, log=log,
                                             name=c["name"])
            return chat_messages(c["chat_id"], since, until, timeout=_to,
                                 page_limit=page_limit, page_size=page_size)

        msgs, err, pg = _fetch(to)
        retries = 0
        while _is_timeout(err) and to < TIMEOUT_MAX:
            nxt = min(int(to * TIMEOUT_ESCALATE), TIMEOUT_MAX)
            if nxt <= to:
                break
            to = nxt
            retries += 1
            if log:
                log("[%d/%s] %s 超时 ⇒ 升档重试 timeout=%ss" % (i, len(chats), c["name"], to))
            msgs, err, pg = _fetch(to)
        recs.append({"chat_id": c["chat_id"], "name": c["name"],
                     "messages": msgs or [], "error": err, "pagination": pg,
                     "timeout_s": to, "timeout_retries": retries,
                     "chunked": bool(chunked),
                     "chunk_timeouts": int((pg or {}).get("chunk_timeouts") or 0)})
        if log:
            extra = ""
            if pg:
                extra = " pages=%s complete=%s%s" % (
                    pg.get("pages"), pg.get("complete"),
                    " TRUNCATED" if pg.get("truncated") else "")
            if retries:
                extra += " retries=%d" % retries
            log("[%d/%s] %s msgs=%s%s %s" % (i, len(chats), c["name"],
                                             len(msgs or []), extra, err or ""))
    return recs


def run(win, me=DEFAULT_ME, me_ids=(), limit=None, timeout=180, log=None,
        page_limit=None, page_size=None, chunked=False, chunk_pages=None,
        include_chats=(), skip_chats=()):
    """全流程：会话列表 → 逐会话消息 → 汇总。返回 (result, errors)。"""
    errors = []
    chats, err = chat_list(timeout=timeout)
    if chats is None:
        return None, [err or "chat-list failed"]
    recs = collect(chats, since=win["since"], until=win["until"],
                   limit=limit, timeout=timeout, log=log,
                   page_limit=page_limit, page_size=page_size,
                   chunked=chunked, chunk_pages=chunk_pages,
                   include_chats=include_chats, skip_chats=skip_chats)
    for r in recs:
        if r.get("error"):
            errors.append("%s: %s" % (r["name"], r["error"]))
    st = summarize(recs, win["since_ts"], win["until_ts"], me=me, me_ids=me_ids)
    st["window"] = {"label": win.get("label"), "since": win["since"],
                    "until": win["until"], "days": win.get("days")}
    st["me"] = me
    st["errors"] = errors
    st["paging"] = {"chunked": bool(chunked),
                    "chunk_pages": (int(chunk_pages) if (chunked and chunk_pages)
                                    else (CHUNK_PAGES_DEFAULT if chunked else None)),
                    "page_limit": (PAGE_LIMIT_DEFAULT if page_limit is None else page_limit),
                    "page_size": (PAGE_SIZE_DEFAULT if page_size is None else page_size),
                    "chunk_timeouts": sum(r.get("chunk_timeouts", 0) for r in recs)}
    return st, errors


def render_md(st):
    """人类可读摘要（推送给人看的那版）。"""
    L = []
    w = st.get("window") or {}
    L.append("窗口 %s | %s → %s" % (w.get("label"), w.get("since"), w.get("until")))
    L.append("会话 %d（有消息 %d）| 消息总数 %d | 本人发言 %d"
             % (st["chats"], st["chats_with_msg"], st["messages"], st["own_messages"]))
    L.append("本人发言按月：" + ", ".join("%s=%d" % kv for kv in st["own_by_month"].items()))
    L.append("全量按月：" + ", ".join("%s=%d" % kv for kv in st["total_by_month"].items()))
    pg = st.get("pagination") or {}
    if pg:
        L.append("分页：会话已报 %d（未取完 %d）| 页数合计 %d | 服务端条数合计 %d"
                 % (pg.get("chats_reported", 0), pg.get("chats_incomplete", 0),
                    pg.get("pages_total", 0), pg.get("server_total", 0)))
    tr = st.get("truncated_chats") or []
    if tr:
        L.append("⚠ 分页未取完（结果为下界，勿当完整）：" + "; ".join(
            "%s(取到%s/上限%s%s)" % (x.get("name"), x.get("fetched"), x.get("cap"),
                                    ("，服务端共%s" % x["total_server"]) if x.get("total_server") else "")
            for x in tr[:10]))
    top = [x for x in st["per_chat"] if x["messages"] > 0][:10]
    if top:
        L.append("Top 会话：" + "; ".join("%s(%d/本人%d)" % (x["name"], x["messages"], x["own"])
                                          for x in top))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="绩效取数（飞书会话/本人发言，季度窗口）")
    ap.add_argument("--quarter", help="如 Q3-2026 / 2026Q3（与 --since/--until 二选一）")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--me", default=DEFAULT_ME, help="本人显示名（默认 %s）" % DEFAULT_ME)
    ap.add_argument("--me-id", action="append", default=[], help="本人 sender id（可多次）")
    ap.add_argument("--limit", type=int, default=None, help="只扫前 N 个会话（自测/抽查）")
    ap.add_argument("--page-limit", type=int, default=PAGE_LIMIT_DEFAULT,
                    help="单会话最多取多少**页**（lark-cli 上限 1000，默认 %d）" % PAGE_LIMIT_DEFAULT)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE_DEFAULT,
                    help="每页条数（≤50，默认 %d）" % PAGE_SIZE_DEFAULT)
    ap.add_argument("--include-chats", action="append", default=[], metavar="SUBSTR",
                    help="只扫会话名含该子串的会话（可多次；整库遍历前建议收窄）")
    ap.add_argument("--skip-chats", action="append", default=[], metavar="SUBSTR",
                    help="排除会话名含该子串的会话（可多次，如 告警/运维）")
    ap.add_argument("--chunked", dest="chunked", action="store_true", default=True,
                    help="分块续拉（自管 page-token，大群更稳；**默认开**）")
    ap.add_argument("--no-chunked", dest="chunked", action="store_false",
                    help="关闭分块续拉（回退旧的一次性 --page-all 行为，仅排障用）")
    ap.add_argument("--chunk-pages", type=int, default=CHUNK_PAGES_DEFAULT,
                    help="分块模式下每块最多页数（默认 %d）" % CHUNK_PAGES_DEFAULT)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--json", help="结果落盘路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的 lark-cli 调用")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if qw is None:
        print("ERROR: 需要 tools/quarter_window.py（同目录）", file=sys.stderr)
        return 2
    win = qw.resolve(since=args.since, until=args.until, quarter=args.quarter,
                     offset=args.offset)

    if args.dry_run:
        print("window:", json.dumps({k: win.get(k) for k in
                                     ("label", "since", "until", "since_ts", "until_ts", "days")},
                                    ensure_ascii=False))
        print("lark:", LARK, "im +chat-list --types=p2p,group --page-all --page-limit %d --format json"
              % CHAT_LIST_PAGE_LIMIT)
        if args.chunked:
            cp = max(1, min(int(args.chunk_pages), int(args.page_limit)))
            print("lark:", LARK, "im +chat-messages-list --chat-id <id> --page-limit %d"
                  " --page-size %d --order asc --format json --start %s --end %s"
                  "   # 分块续拉：每块 %d 页，用上块 next_token 续（page-all 由本工具自理）"
                  % (args.page_limit, args.page_size, win["since"], win["until"], cp))
        else:
            print("lark:", LARK, "im +chat-messages-list --chat-id <id> --page-all --page-limit %d"
                  " --page-size %d --order asc --format json --start %s --end %s"
                  "   # --no-chunked（旧行为，仅排障）"
                  % (args.page_limit, args.page_size, win["since"], win["until"]))
        return 0

    st, errors = run(win, me=args.me, me_ids=tuple(args.me_id),
                     limit=args.limit, timeout=args.timeout,
                     page_limit=args.page_limit, page_size=args.page_size,
                     chunked=args.chunked, chunk_pages=args.chunk_pages,
                     include_chats=tuple(args.include_chats),
                     skip_chats=tuple(args.skip_chats),
                     log=lambda s: print(s, file=sys.stderr))
    if st is None:
        print("ERROR: %s" % "; ".join(errors), file=sys.stderr)
        return 1
    print(render_md(st))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
        print("json:", args.json)
    if errors:
        print("ERROR: %d 个会话取数失败（结果不含这些会话，数字为下界）：%s"
              % (len(errors), " | ".join(errors[:5])), file=sys.stderr)
        return 3
    if (st.get("pagination") or {}).get("chats_incomplete"):
        print("WARN: 有会话分页未取完（数字为下界，勿当完整值）", file=sys.stderr)
        return 3
    return 0



def _chk(checks, cond, name):
    checks.append((bool(cond), name))


def _selftest():
    """纯函数夹具自测：不触网、不调 lark-cli、不写生产状态。"""
    checks = []
    TZ = _dt.timezone(_dt.timedelta(hours=8))

    def ts(s):
        return int(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())

    # --- 窗口（依赖 quarter_window，同目录单一源）---
    win = None
    if qw is not None:
        try:
            win = qw.resolve(quarter="Q3-2026")
        except Exception as e:  # pragma: no cover
            _chk(checks, False, "quarter_window.resolve 可调用: %s" % e)
    _chk(checks, win is not None, "quarter_window 可用")
    if win:
        _chk(checks, str(win.get("since", "")).startswith("2026-07-01"), "Q3 起点=07-01")
        _chk(checks, str(win.get("until", "")).startswith("2026-10-01"), "Q3 终点=10-01")
        _chk(checks, win.get("since_ts") == ts("2026-07-01 00:00"), "起点 ts 口径")
        _chk(checks, win.get("until_ts") == ts("2026-10-01 00:00"), "终点 ts 口径")

    # --- _ts 归一 ---
    _chk(checks, _ts(str(ts("2026-07-05 10:00"))) == ts("2026-07-05 10:00"), "_ts 秒字符串")
    _chk(checks, _ts(ts("2026-07-05 10:00") * 1000) == ts("2026-07-05 10:00"), "_ts 毫秒归一")
    _chk(checks, _ts("2026-07-05T10:00:00+08:00") == ts("2026-07-05 10:00"), "_ts ISO")
    _chk(checks, _ts(None) is None and _ts("") is None and _ts("abc") is None,
         "无法判定⇒None（负控）")
    _chk(checks, _ts(True) is None, "bool 不当时间（负控）")

    # --- _find_list ---
    nested = {"data": {"items": [{"chat_id": "c1", "name": "n1"}, {"chat_id": "c2", "name": "n2"}]}}
    got = _find_list(nested, ("chat_id", "name"))
    _chk(checks, isinstance(got, list) and len(got) == 2, "_find_list 嵌套定位")
    _chk(checks, _find_list({"a": []}, ("chat_id",)) is None, "_find_list 空⇒None（负控）")

    # --- is_own + summarize 夹具（不触网）---
    s0, s1 = ts("2026-07-01 00:00"), ts("2026-10-01 00:00")

    def msg(t, who, mid="m"):
        return {"create_time": str(t) if t is not None else None,
                "message_id": mid, "sender": {"name": who}}

    msgs = [msg(ts("2026-07-05 10:00"), "崔朋", "m1"),
            msg(ts("2026-08-03 09:00"), "张三", "m2"),
            msg(ts("2026-09-30 23:59"), "崔朋-x", "m3"),
            msg(ts("2026-06-30 23:59"), "崔朋", "m4"),
            msg(None, "崔朋", "m5")]
    st = summarize([{"chat_id": "c1", "name": "群A", "messages": msgs}], s0, s1, me="崔朋")
    _chk(checks, st["messages"] == 3, "窗口内计数=3（剔窗口外）")
    _chk(checks, st["own_messages"] == 2, "本人发言=2")
    _chk(checks, st["dropped_bad_time"] == 1, "坏时间单列=1（负控：不静默）")
    _chk(checks, st["own_by_month"] == {"2026-07": 1, "2026-09": 1}, "本人按月")
    _chk(checks, st["total_by_month"] == {"2026-07": 1, "2026-08": 1, "2026-09": 1}, "全量按月")
    _chk(checks, st["chats"] == 1 and st["chats_with_msg"] == 1, "会话计数")
    _chk(checks, is_own(msg(1, "张三"), "崔朋") is False, "他人不算本人（负控）")
    _chk(checks, is_own({"sender": {"id": "ou_abc"}}, "崔朋", me_ids=("ou_abc",)) is True, "按 id 认领")
    _chk(checks, is_own({"sender": {}}, "崔朋") is False, "归属不可判定⇒False（负控）")
    _chk(checks, in_window(s0, s0, s1) and not in_window(s1, s0, s1), "半开区间 [since,until)")
    _chk(checks, in_window(None, s0, s1) is False, "None 不入窗（负控）")
    _chk(checks, month_of(s0) == "2026-07", "month_of")
    _chk(checks, "本人发言 2" in render_md(st), "render_md 含关键数")

    # --- 分页完整性（截断判定；✱ 旧版 50 页上限会静默截断大群）---
    _chk(checks, parse_pagination(None)["complete"] is None, "无 payload ⇒ complete 未知（负控）")
    okpay = {"data": {"total": 123, "has_more": False, "messages": []},
             "meta": {"pagination": {"complete": True, "pages": 3, "items": 120,
                                     "next_token": ""}}}
    pg_ok = page_meta(120, 1000, 50, okpay)
    _chk(checks, pg_ok["complete"] is True and pg_ok["truncated"] is False
         and pg_ok["total"] == 123, "取完 ⇒ 不标截断且带服务端总数")
    badpay = {"meta": {"pagination": {"complete": False, "pages": 2, "items": 100,
                                      "next_token": "t"}}}
    pg_bad = page_meta(100, 2, 50, badpay)
    _chk(checks, pg_bad["truncated"] is True and pg_bad["complete"] is False,
         "meta 声明未取完 ⇒ 截断（负控）")
    pg_top = page_meta(2500, 50, 50, {"data": {"messages": []}})
    _chk(checks, pg_top["truncated"] is True and pg_top["source"] == "heuristic",
         "无 meta 且触顶 ⇒ 判截断（负控：2500=50×50）")
    pg_near = page_meta(2499, 50, 50, {"data": {"messages": []}})
    _chk(checks, pg_near["truncated"] is False, "未触顶 ⇒ 不标截断")
    _chk(checks, parse_pagination({"data": {"has_more": True}})["complete"] is False,
         "has_more 兜底 ⇒ 未取完")
    tr_chat = {"chat_id": "c9", "name": "大群", "messages": msgs, "error": "",
               "pagination": {"complete": False, "truncated": True, "fetched": 2500,
                              "cap": 2500, "pages": 50, "total": 9999, "source": "meta"}}
    st_tr = summarize([tr_chat], s0, s1, me="崔朋")
    _chk(checks, st_tr["pagination"]["chats_incomplete"] == 1
         and st_tr["pagination"]["server_total"] == 9999, "截断汇总入 pagination（负控）")
    _chk(checks, st_tr["truncated_chats"] and st_tr["truncated_chats"][0]["name"] == "大群",
         "truncated_chats 逐条列出")
    _chk(checks, "分页未取完" in render_md(st_tr), "render_md 出截断告警")
    st_ok = summarize([{"chat_id": "c1", "name": "群A", "messages": msgs, "error": "",
                        "pagination": {"complete": True, "truncated": False,
                                       "fetched": 3, "cap": 50000, "pages": 1,
                                       "total": 3, "source": "meta"}}], s0, s1, me="崔朋")
    _chk(checks, st_ok["truncated_chats"] == [] and st_ok["pagination"]["chats_incomplete"] == 0,
         "正常会话不产截断告警（负控）")
    _chk(checks, PAGE_LIMIT_DEFAULT >= 50, "默认页数上限 ≥ 旧值 50（不许回退到截断值）")
    _chk(checks, PAGE_SIZE_DEFAULT <= 50, "页大小 ≤ lark-cli 上限 50")

    # --- 超时升档自愈（大群 rc=124 不许吞成缺失）---
    _orig_cm = globals()["chat_messages"]

    def _make(fail_times, err="timeout after %ss"):
        calls = []

        def _f(chat_id, since="", until="", timeout=180, page_limit=None, page_size=None):
            calls.append(timeout)
            if len(calls) <= fail_times:
                return None, (err % timeout) if "%s" in err else err, None
            return ([msg(ts("2026-07-05 10:00"), "崔朋", "m1")], "",
                    {"complete": True, "truncated": False, "fetched": 1,
                     "cap": 1000, "pages": 1, "total": 1, "source": "meta"})
        return _f, calls

    _f1, _c1 = _make(1)
    globals()["chat_messages"] = _f1
    try:
        _r1 = collect([{"chat_id": "c1", "name": "大群"}], since=s0, until=s1, timeout=180)
    finally:
        globals()["chat_messages"] = _orig_cm
    _chk(checks, _c1 == [180, 720], "超时⇒按倍升档重试（180→720）")
    _chk(checks, _r1[0]["error"] == "" and _r1[0]["timeout_retries"] == 1
         and len(_r1[0]["messages"]) == 1, "升档后取到数据且留痕 retries=1")

    _f2, _c2 = _make(99)
    globals()["chat_messages"] = _f2
    try:
        _r2 = collect([{"chat_id": "c2", "name": "死群"}], since=s0, until=s1, timeout=180)
    finally:
        globals()["chat_messages"] = _orig_cm
    _chk(checks, _c2 == [180, 720, 1800] and _c2[-1] <= TIMEOUT_MAX + 1,
         "永久超时⇒升到上限即止（负控：不无限重试）")
    _chk(checks, _r2[0]["error"] and _r2[0]["timeout_retries"] == 2,
         "永久超时仍如实留错（负控：不静默）")

    _f3, _c3 = _make(99, err="rc=1 boom")
    globals()["chat_messages"] = _f3
    try:
        _r3 = collect([{"chat_id": "c3", "name": "报错群"}], since=s0, until=s1, timeout=180)
    finally:
        globals()["chat_messages"] = _orig_cm
    _chk(checks, _c3 == [180] and _r3[0]["timeout_retries"] == 0,
         "非超时错误不重试（负控：不掩盖真错误）")

    # --- 分块续拉（自管 page_token；单块超时只赔一块，不丢整会话）---
    def _mk_lark(pages=6, ps=50, fail_idx=(), timeout_idx=()):
        """替身：两块语义严格分离——`n`=调用序号(判超时/失败，模拟网络抽风)，
        `blk`=块号(由 page-token 派生，决定返回哪一页数据)。二者混用会把重试误当翻页。"""
        st = {"n": 0, "calls": []}

        def _f(args, timeout=180, env=None):
            pl_ = int(args[args.index("--page-limit") + 1])
            tok = args[args.index("--page-token") + 1] if "--page-token" in args else ""
            blk = int(tok[3:]) if tok.startswith("tok") else 0
            n = st["n"]
            st["n"] += 1
            st["calls"].append({"n": n, "i": blk, "page_limit": pl_, "token": tok,
                                "page_all": ("--page-all" in args),
                                "timeout": timeout})
            if n in timeout_idx:
                return 124, "", "lark-cli timeout after %ss" % timeout
            if n in fail_idx:
                return 1, "", "boom: permission denied"   # 非超时类，禁含 timeout 字样
            i = blk
            n_done = i * pl_
            left = max(0, pages - n_done)
            get = min(pl_, left)
            last = (n_done + get) >= pages
            items = [{"create_time": str(1751000000 + i * 10000 + k),
                      "message_id": "m%d-%d" % (i, k),
                      "sender": {"name": "崔朋" if k % 5 else "张三"}} for k in range(get * ps)]
            pag = {"complete": last, "pages": get, "items": len(items),
                   "next_token": "" if last else "tok%d" % (i + 1)}
            return 0, json.dumps({"items": items, "meta": {"pagination": pag}}), ""

        return _f, st

    _orig_lr = globals()["lark_run"]
    try:
        # T1 分块翻到底：6 页 × chunk 2 ⇒ 3 块、300 条、complete、非截断
        f, st = _mk_lark(pages=6)
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c1", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, er == "" and len(ms) == 300 and mt["chunks"] == 3 and mt["pages_done"] == 6,
             "分块续拉：6 页分 3 块取全 300 条")
        _chk(checks, mt["complete"] is True and mt["truncated"] is False
             and [c["token"] for c in st["calls"]] == ["", "tok1", "tok2"],
             "分块续拉：page_token 逐块正确传递且判完整")

        # T2 第 2 块超时 ⇒ 只重拉该块，总数不减、留痕 1 次
        f, st = _mk_lark(pages=6, timeout_idx=(1,))
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c2", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, er == "" and len(ms) == 300 and mt["chunk_timeouts"] == 1,
             "分块续拉：单块超时重拉成功后取全（chunk_timeouts=1）")

        # T3 第 2 块连续两次超时 ⇒ 如实报错 + 标截断 + 留 resume_token
        f, st = _mk_lark(pages=6, timeout_idx=(1, 2))
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c3", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, er != "" and mt["truncated"] is True and mt["complete"] is False
             and mt["resume_token"] and mt["chunks"] == 1 and len(ms) == 100,
             "分块续拉：块反复超时⇒报错+截断+可续拉 token（不静默当完整）")

        # T4 非超时错误不重试（负控：不许掩盖真错误）
        f, st = _mk_lark(pages=6, fail_idx=(1,))
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c4", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, "permission denied" in er and mt["chunk_timeouts"] == 0
             and len(st["calls"]) == 2,
             "分块续拉：非超时错误不重试（负控）")

        # T5 页数上限仍生效（不许靠分块绕过截断保护）
        f, st = _mk_lark(pages=6)
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c5", page_limit=4, page_size=50, chunk_pages=2)
        _chk(checks, mt["pages_done"] == 4 and mt["truncated"] is True and len(ms) == 200,
             "分块续拉：触到 page_limit 上限仍判截断（下界语义）")

        # T8c 分块每块必须带 --page-all（负控：缺它时 lark-cli 忽略 --page-limit、每次仅 1 页
        #     ⇒ chunk_pages 静默失效、调用次数放大；2026-10-09 真机 A/B 证实的真缺陷）
        _chk(checks, bool(st["calls"]) and all(c.get("page_all") for c in st["calls"]),
             "分块每块须带 --page-all（否则 --page-limit/chunk_pages 静默失效）")
        _chk(checks, all(c.get("page_all") for c in st["calls"]) and len(st["calls"]) == 2,
             "分块调用次数=celling(page_limit/chunk_pages)（page-all 生效下方可成立）")

        # T7 元信息未知（无 complete 且无 has_more）⇒ 必须判「未知」而非完整（负控：禁 fail-open）
        def _mk_nometa(pages=6, ps=50):
            st = {"n": 0, "calls": []}
            def _f(args, timeout=180, env=None):
                pl_ = int(args[args.index("--page-limit") + 1])
                tok = args[args.index("--page-token") + 1] if "--page-token" in args else ""
                blk = int(tok[3:]) if tok.startswith("tok") else 0
                st["n"] += 1; st["calls"].append(blk)
                get = min(pl_, max(0, pages - blk * pl_))
                items = [{"create_time": "1", "message_id": "n%d-%d" % (blk, k),
                          "sender": {"name": "崔朋"}} for k in range(get * ps)]
                return 0, json.dumps({"data": {"items": items},
                                      "meta": {"pagination": {}}}), ""
            return _f, st
        f, st = _mk_nometa(pages=6)
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c7", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, mt["complete"] is False and mt["truncated"] is True
             and mt["source"] == "chunk_unknown" and len(ms) == 100,
             "分块续拉：元信息未知⇒未知即截断（负控：禁 fail-open 当完整）")

        # T8 顶层 data.has_more=False 兜底 ⇒ 判完整（一次即止）
        def _mk_hasmore():
            st = {"n": 0}
            def _f(args, timeout=180, env=None):
                st["n"] += 1
                items = [{"create_time": "1", "message_id": "h%d" % k,
                          "sender": {"name": "崔朋"}} for k in range(3)]
                return 0, json.dumps({"data": {"items": items, "has_more": False},
                                      "meta": {"pagination": {}}}), ""
            return _f, st
        f, st = _mk_hasmore()
        globals()["lark_run"] = f
        ms, er, mt = chat_messages_chunked("c8", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, mt["complete"] is True and mt["truncated"] is False and st["n"] == 1,
             "分块续拉：无 pagination 时按 data.has_more=False 兜底判完整（一次即止）")

        # T8b 包装层把 has_more 放顶层 ⇒ 同样兜底判完整（兼容旧包装）
        f, st = _mk_hasmore()
        _orig_f = f

        def _f_top(args, timeout=180, env=None):
            rc, out, err = _orig_f(args, timeout=timeout, env=env)
            d = json.loads(out)
            d2 = {"data": {"items": d["data"]["items"]}, "has_more": False,
                  "meta": {"pagination": {}}}
            return rc, json.dumps(d2), err
        globals()["lark_run"] = _f_top
        ms, er, mt = chat_messages_chunked("c8b", page_limit=10, page_size=50, chunk_pages=2)
        _chk(checks, mt["complete"] is True and mt["truncated"] is False,
             "分块续拉：has_more 在顶层也能兜底判完整（兼容旧包装）")

        # T6 collect 分派：chunked=True 必须走分块实现（负控：默认仍走旧实现）
        _orig_cmc = globals()["chat_messages_chunked"]
        seen = []
        try:
            def _stub_chunked(cid, since="", until="", **kw):
                seen.append(kw.get("chunk_pages"))
                return ([{"create_time": "1", "message_id": "x",
                          "sender": {"name": "崔朋"}}], "",
                        {"complete": True, "truncated": False, "chunk_timeouts": 2})

            globals()["chat_messages_chunked"] = _stub_chunked
            globals()["chat_messages"] = lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("chunked 模式不应调用 chat_messages"))
            rs = collect([{"chat_id": "cX", "name": "nX"}], chunked=True, chunk_pages=3)
            _chk(checks, seen == [3] and rs[0]["chunked"] is True
                 and rs[0]["chunk_timeouts"] == 2,
                 "collect：chunked=True 走分块实现并透传 chunk_pages")
        finally:
            globals()["chat_messages_chunked"] = _orig_cmc
    finally:
        globals()["lark_run"] = _orig_lr

    # T9 会话过滤（纯函数；负控：不误放/不误杀）
    _c0 = [{"chat_id": "1", "name": "工程群"}, {"chat_id": "2", "name": "运维故障告警群"},
           {"chat_id": "3", "name": "禅道通知"}]
    _k1, _f1 = filter_chats(_c0, include=("工程", "禅道"))
    _chk(checks, [c["name"] for c in _k1] == ["工程群", "禅道通知"]
         and _f1["dropped_by"]["include_miss"] == 1,
         "会话过滤：include 只留命中（告警群被排除）")
    _k2, _f2 = filter_chats(_c0, skip=("告警",))
    _chk(checks, [c["name"] for c in _k2] == ["工程群", "禅道通知"]
         and _f2["dropped_by"]["skip_hit"] == 1,
         "会话过滤：skip 精确剔除（负控：不误杀其它会话）")
    _k3, _f3 = filter_chats(_c0)
    _chk(checks, len(_k3) == 3 and _f3["dropped"] == 0,
         "会话过滤：无参时零过滤（保持整库遍历默认行为）")

    bad = [n for ok, n in checks if not ok]
    for ok, n in checks:
        print(("PASS  " if ok else "FAIL  ") + n)
    print("selftest: %d checks, %d failed" % (len(checks), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
