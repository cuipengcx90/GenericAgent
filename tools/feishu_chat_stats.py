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


def chat_messages(chat_id, since="", until="", timeout=180):
    """取单会话消息（服务端粗窗）。返回 (msgs, err)。"""
    args = ["im", "+chat-messages-list", "--chat-id", chat_id, "--page-all",
            "--page-limit", "50", "--order", "asc", "--format", "json"]
    if since:
        args += ["--start", since]
    if until:
        args += ["--end", until]
    rc, out, err = lark_run(args, timeout=timeout)
    if rc != 0:
        return None, "rc=%s %s" % (rc, (err or out)[:200])
    data = parse_json(out)
    items = _find_list(data, ("create_time", "message_id")) or []
    return items, ""


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
           "per_chat": []}
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
                                "dropped_bad_time": bad})
    out["per_chat"].sort(key=lambda x: (-x["messages"], x["name"] or ""))
    for k in ("own_by_month", "total_by_month"):
        out[k] = dict(sorted(out[k].items()))
    return out


def collect(chats, since="", until="", limit=None, timeout=180, log=None):
    """遍历会话取消息 ⇒ records=[{'chat_id','name','messages','error'}]。"""
    recs = []
    for i, c in enumerate(chats, 1):
        if limit and i > limit:
            break
        msgs, err = chat_messages(c["chat_id"], since, until, timeout=timeout)
        recs.append({"chat_id": c["chat_id"], "name": c["name"],
                     "messages": msgs or [], "error": err})
        if log:
            log("[%d/%s] %s msgs=%s %s" % (i, len(chats), c["name"],
                                           len(msgs or []), err or ""))
    return recs


def run(win, me=DEFAULT_ME, me_ids=(), limit=None, timeout=180, log=None):
    """全流程：会话列表 → 逐会话消息 → 汇总。返回 (result, errors)。"""
    errors = []
    chats, err = chat_list(timeout=timeout)
    if chats is None:
        return None, [err or "chat-list failed"]
    recs = collect(chats, since=win["since"], until=win["until"],
                   limit=limit, timeout=timeout, log=log)
    for r in recs:
        if r.get("error"):
            errors.append("%s: %s" % (r["name"], r["error"]))
    st = summarize(recs, win["since_ts"], win["until_ts"], me=me, me_ids=me_ids)
    st["window"] = {"label": win.get("label"), "since": win["since"],
                    "until": win["until"], "days": win.get("days")}
    st["me"] = me
    st["errors"] = errors
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
        print("lark:", LARK, "im +chat-list --types=p2p,group --page-all --page-limit 50 --format json")
        print("lark:", LARK, "im +chat-messages-list --chat-id <id> --page-all --page-limit 50 --order asc --format json"
              " --start %s --end %s" % (win["since"], win["until"]))
        return 0

    st, errors = run(win, me=args.me, me_ids=tuple(args.me_id),
                     limit=args.limit, timeout=args.timeout,
                     log=lambda s: print(s, file=sys.stderr))
    if st is None:
        print("ERROR: %s" % "; ".join(errors), file=sys.stderr)
        return 1
    print(render_md(st))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
        print("json:", args.json)
    return 0 if not errors else 3



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

    bad = [n for ok, n in checks if not ok]
    for ok, n in checks:
        print(("PASS  " if ok else "FAIL  ") + n)
    print("selftest: %d checks, %d failed" % (len(checks), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
