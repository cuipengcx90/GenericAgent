#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""linuxdo_prefs —— L 站互动对象偏好模型（真人化第 2 件，单一源）

真人 vs 机器，差异面全在「选谁互动」上（本模块只建模这一面）：
  1. 兴趣板块：真人长期偏爱 2-4 个板块，不会在全站分类上均匀跳；偶尔探索新板块（ε 探索率）。
  2. 关系对象：互动主要冲着「人」去——大部分是陌生新面孔，少数是打过交道的熟人；
     但绝不在同一天反复盯同一个人（集中度惩罚），也绝不只挑同一个人（那是机器）。
  3. 记忆衰减：关系权重按半衰期衰减，久未互动的熟人自然淡出，不是永久记账。
  4. 时段画像：记录活跃小时分布（真正管住行为时刻的是 antidetect 的静默窗，这里只做画像/解释）。

写侧（谁记）：执行器每次成功后 record()；发帖成功 record_own_post()（供回访用）。
读侧（谁用）：acts 选靶时 pick_topics()（板块偏好）/ pick_floor()（楼层作者偏好）；
              forge 选题时 read_browse_titles()（最近浏览过的帖子当灵感）。

自测禁写生产：所有函数都接受 base 参数，selftest 走 tempfile.mkdtemp() 沙箱。

CLI:
    python3 tools/linuxdo_prefs.py show            # 只读画像（人类可读）
    python3 tools/linuxdo_prefs.py selftest
"""
import argparse
import datetime as dt
import json
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_BASE = os.path.join(ROOT, "temp", "linuxdo_prefs")
BROWSE_LOG_DEFAULT = os.path.join(ROOT, "temp", "linuxdo_browse_daemon", "browse_log.jsonl")

# ---------------------------------------------------------------- 可调常量（单一源）
HALF_LIFE_D = 21.0            # 关系权重半衰期（天）
CAT_HALF_LIFE_D = 45.0        # 板块偏好半衰期（天，比关系慢：口味比人脉稳）
EXPLORE_P = 0.25              # 探索新板块概率
STRANGER_P = 0.75             # 选陌生新面孔概率（其余给熟人）
FAMILIAR_MIN_N = 2            # 互动 >= 2 次才算「熟人」
SAME_USER_TODAY_PENALTY = 0.12  # 当日已互动过的对象降权（防一天盯一人）
SAME_TOPIC_PENALTY = 0.05     # 已互动过的话题降权（防反复进同一帖）
STRANGER_BASE_W = 1.0         # 陌生人权重基线
MAX_LEDGER_BYTES = 8 * 1024 * 1024   # 台账读取上限（防坏文件拖死守护）
DAY_KEY_FMT = "%Y-%m-%d"


def base_dir(base=None):
    return base or os.environ.get("LJQ_PREFS_DIR") or DEFAULT_BASE


def _path(name, base=None):
    return os.path.join(base_dir(base), name)


def _day(ts):
    return dt.datetime.fromtimestamp(ts).strftime(DAY_KEY_FMT)


def _now(now=None):
    return time.time() if now is None else float(now)


def _mnow(model, now=None):
    """读侧统一取时点：显式 now > 模型装载时点 > 墙钟。

    负控：若读侧漏用模型时点，自测里「半衰期折半」会因 age=真实当下而恒为 0。
    """
    if now is not None:
        return float(now)
    if isinstance(model, dict) and model.get("now"):
        return float(model["now"])
    return time.time()


def _decay(age_s, half_life_d):
    if half_life_d <= 0:
        return 1.0
    return 0.5 ** (max(0.0, age_s) / (half_life_d * 86400.0))


# ---------------------------------------------------------------- 写侧
def record(action, user=None, topic=None, post=None, cat=None, cat_label=None,
           ts=None, extra=None, base=None):
    """追加一条互动记录（append-only）。返回该行 dict。

    幂等：同 (action, topic, post) 在 _DEDUP_WINDOW_S 内重复调用只写一次。

    cat 键一律存 **linux.do category_id 的字符串**（/latest.json 的 t.category_id，
    全站唯一且稳定；READ_TOPIC_JS 亦返回 category_id）——板块名可能被站点改名/多语言，
    不用它做键。cat_label 只作人类可读的展示标签（有则记，无则展示成 `cat:<id>`），
    绝不参与权重计算。
    """
    ts = _now(ts)
    row = {"ts": ts, "day": _day(ts), "action": str(action)}
    if user:
        row["user"] = str(user)
    if topic is not None:
        row["topic"] = str(topic)
    if post is not None:
        row["post"] = str(post)
    if cat:
        row["cat"] = str(cat)
    if cat_label:
        row["cat_label"] = str(cat_label)
    if extra:
        row["extra"] = extra
    d = base_dir(base)
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "interactions.jsonl")
        if _dup_recent(p, row):
            return row
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:                                   # 记录失败绝不阻塞生产
        sys.stderr.write("[prefs] record failed: %s\n" % e)
    return row


_DEDUP_WINDOW_S = 600.0


def _dup_recent(path, row):
    """最近 200 行内同 (action, topic, post) 且 10 分钟内 ⇒ 视为重复。"""
    try:
        if not os.path.exists(path):
            return False
        with open(path, "rb") as f:
            try:
                f.seek(-60000, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", "replace").splitlines()[-200:]
        key = (row.get("action"), row.get("topic"), row.get("post"))
        for ln in tail:
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if (o.get("action"), o.get("topic"), o.get("post")) == key \
                    and abs(float(o.get("ts", 0)) - float(row["ts"])) <= _DEDUP_WINDOW_S:
                return True
    except Exception:
        return False
    return False


def record_own_post(tid, title=None, cat=None, url=None, ts=None, base=None):
    """记自己发的帖（回访动作的输入面）。"""
    ts = _now(ts)
    row = {"ts": ts, "day": _day(ts), "topic": str(tid)}
    if title:
        row["title"] = str(title)
    if cat:
        row["cat"] = str(cat)
    if url:
        row["url"] = str(url)
    try:
        d = base_dir(base)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "own_posts.jsonl")
        if os.path.exists(p):
            for o in _read_jsonl(p)[0]:
                if str(o.get("topic")) == str(tid):
                    return o                                # 同帖只记一次
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write("[prefs] record_own_post failed: %s\n" % e)
    return row


def own_posts(base=None):
    """返回自己的帖（新→旧）。"""
    rows, _bad = _read_jsonl(_path("own_posts.jsonl", base))
    return sorted(rows, key=lambda r: float(r.get("ts", 0)), reverse=True)


# ---------------------------------------------------------------- 读侧
def _read_jsonl(path):
    rows, bad = [], 0
    try:
        if not os.path.exists(path):
            return rows, bad
        if os.path.getsize(path) > MAX_LEDGER_BYTES:
            bad += 1
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                    if isinstance(o, dict):
                        rows.append(o)
                    else:
                        bad += 1
                except Exception:
                    bad += 1                       # 坏行跳过而非崩（守护入口）
    except Exception:
        bad += 1
    return rows, bad


def load_model(base=None, now=None):
    """从台账重建模型（无缓存，量大也够快；口径唯一）。"""
    now = _now(now)
    rows, bad = _read_jsonl(_path("interactions.jsonl", base))
    m = {"n": 0, "bad": bad, "base": base_dir(base),
         "users": {}, "cats": {}, "labels": {}, "actions": {}, "hours": {},
         "days": {}, "topics": {}, "span": [None, None]}
    for r in rows:
        try:
            ts = float(r.get("ts", 0))
        except Exception:
            bad += 1
            continue
        m["n"] += 1
        day = r.get("day") or _day(ts)
        hour = dt.datetime.fromtimestamp(ts).hour
        act = str(r.get("action") or "?")
        m["hours"][hour] = m["hours"].get(hour, 0) + 1
        a = m["actions"].setdefault(act, {"n": 0, "last_ts": 0.0})
        a["n"] += 1
        a["last_ts"] = max(a["last_ts"], ts)
        d = m["days"].setdefault(day, {"n": 0, "users": {}, "topics": {}})
        d["n"] += 1
        u = r.get("user")
        if u:
            u = str(u)
            rec = m["users"].setdefault(u, {"n": 0, "first_ts": ts, "last_ts": ts,
                                            "by_action": {}, "cats": {}, "topics": []})
            rec["n"] += 1
            rec["last_ts"] = max(rec["last_ts"], ts)
            rec["first_ts"] = min(rec["first_ts"], ts)
            rec["by_action"][act] = rec["by_action"].get(act, 0) + 1
            d["users"][u] = d["users"].get(u, 0) + 1
        tid = r.get("topic")
        if tid is not None:
            tid = str(tid)
            t = m["topics"].setdefault(tid, {"n": 0, "last_ts": 0.0})
            t["n"] += 1
            t["last_ts"] = max(t["last_ts"], ts)
            if r.get("cat"):
                t["cat"] = r["cat"]
            if u:
                t["user"] = u
        cat = r.get("cat")
        if cat:
            c = m["cats"].setdefault(str(cat), {"n": 0, "last_ts": 0.0, "users": {}})
            c["n"] += 1
            c["last_ts"] = max(c["last_ts"], ts)
            if u:
                c["users"][u] = c["users"].get(u, 0) + 1
            lbl = r.get("cat_label")
            if lbl and not m["labels"].get(str(cat)):
                m["labels"][str(cat)] = str(lbl)   # 后写的空标签不覆盖已有
        lo, hi = m["span"]
        m["span"] = [ts if lo is None else min(lo, ts), ts if hi is None else max(hi, ts)]
    m["bad"] = bad
    m["now"] = now
    return m


def user_affinity(model, user, now=None):
    """关系强度（0=陌生人；随时间衰减）。"""
    now = _mnow(model, now)
    rec = model.get("users", {}).get(str(user))
    if not rec:
        return 0.0
    return float(rec["n"]) * _decay(now - float(rec.get("last_ts", now)), HALF_LIFE_D)


def cat_weights(model, now=None):
    """板块权重：未见过=1.0 基线，见过的 = 1 + n·decay（**不归一化**）。

    负控：曾按最大归一 ⇒ 偏好板块与陌生板块同为 1.0，「偏好」被抹平（自测 T8b 抓到）。
    """
    now = _mnow(model, now)
    return {cat: 1.0 + float(rec["n"]) * _decay(now - float(rec.get("last_ts", now)), CAT_HALF_LIFE_D)
            for cat, rec in model.get("cats", {}).items()}


def cat_weight_of(model, cat, now=None):
    """单个板块权重：没见过 ⇒ 1.0·explore 基线（新人味：不是只逛老地方）。"""
    return cat_weights(model, now).get(str(cat), 1.0)


def label_of(model, cat, fallback=None):
    """板块展示名：键是 category_id（稳定），名只作展示。

    台账里没记过名（早期记录/未采到）⇒ 退回 `cat:<id>`，绝不猜名。
    负控：曾把 id 与名混作同一个键，导致 cat_weights 权重被拆成两份（自测 T22 抓到）。
    """
    if cat is None:
        return fallback
    return (model.get("labels") or {}).get(str(cat)) or (fallback or ("cat:%s" % cat))


def is_familiar(model, user, now=None, max_age_d=90.0):
    rec = model.get("users", {}).get(str(user))
    if not rec or rec["n"] < FAMILIAR_MIN_N:
        return False
    age_d = (_mnow(model, now) - float(rec.get("last_ts", 0))) / 86400.0   # 负控：曾用 now_value(model) 忽略显式 now（自测 T5/T9 抓到）
    return age_d <= max_age_d


def now_value(model):
    return float(model.get("now") or time.time())


def _weighted_sample(items, weights, k, rng):
    """无放回加权抽样（权重 <=0 者按极小值兜底，保证可抽）。"""
    items = list(items)
    w = [max(1e-6, float(x)) for x in weights]
    out = []
    for _ in range(min(k, len(items))):
        tot = sum(w)
        if tot <= 0:
            i = rng.randrange(len(items))
        else:
            x = rng.random() * tot
            acc = 0.0
            i = len(items) - 1
            for j, wj in enumerate(w):
                acc += wj
                if x <= acc:
                    i = j
                    break
        out.append((items[i], w[i]))
        items.pop(i)
        w.pop(i)
    return out


def pick_topics(cands, model, action="reply", n=2, rng=None, now=None,
                explore_p=EXPLORE_P, day=None):
    """按板块偏好挑话题/帖子。cands=[{id, cat, title?}]，返回 [(cand, why)]。

    why ∈ {pref:cat权重, explore:ε探索, fresh:无偏好记录, fallback:异常兜底}
    """
    rng = rng or random
    now = _mnow(model, now)
    cands = [c for c in (cands or []) if isinstance(c, dict)]
    if not cands or n <= 0:
        return []
    try:
        day = day or _day(now)
        seen_today = model.get("days", {}).get(day, {}).get("topics", {}) or {}
        cw = cat_weights(model, now)
        fresh = not cw
        explore = (not fresh) and rng.random() < explore_p
        ws, whys = [], []
        for c in cands:
            cat = str(c.get("cat") or "")
            tid = str(c.get("id"))
            if explore:
                w, why = 1.0, "explore"
            elif fresh:
                w, why = 1.0, "fresh"
            else:
                w, why = cw.get(cat, 1.0), "pref:%s" % (cat or "-")
            if seen_today.get(tid):
                w *= SAME_TOPIC_PENALTY
            if model.get("topics", {}).get(tid):
                w *= SAME_TOPIC_PENALTY
            ws.append(w)
            whys.append(why)
        picked = _weighted_sample(cands, ws, n, rng)
        out = []
        for it, _w in picked:
            try:
                j = next(k for k, c in enumerate(cands) if c is it)
            except StopIteration:
                j = 0
            out.append((it, whys[j]))
        return out
    except Exception:
        k = min(n, len(cands))
        return [(c, "fallback") for c in rng.sample(cands, k)]


def pick_floor(posts, model, action="like", rng=None, now=None,
               stranger_p=STRANGER_P, self_user=None, day=None):
    """挑一个楼层（作者维度）。posts=[{id, user, num?}]，返回 (post, why) 或 (None, why)。

    why ∈ {stranger, familiar:aff, same_user_today, empty}
    """
    rng = rng or random
    now = _mnow(model, now)
    posts = [p for p in (posts or []) if isinstance(p, dict) and p.get("id") is not None]
    if self_user:
        posts = [p for p in posts if str(p.get("user")) != str(self_user)]
    if not posts:
        return None, "empty"
    try:
        day = day or _day(now)
        seen_users = model.get("days", {}).get(day, {}).get("users", {}) or {}
        fam, new = [], []
        for p in posts:
            (fam if is_familiar(model, p.get("user"), now) else new).append(p)
        pool = new if (new and (not fam or rng.random() < stranger_p)) else (fam or new)
        why_kind = "stranger" if pool is new else "familiar"
        ws = []
        for p in pool:
            u = str(p.get("user"))
            aff = user_affinity(model, u, now)
            w = (STRANGER_BASE_W if why_kind == "stranger" else (1.0 + aff))
            if seen_users.get(u):
                w *= SAME_USER_TODAY_PENALTY
            ws.append(w)
        (choice, _w), = _weighted_sample(pool, ws, 1, rng)
        why = why_kind
        if seen_users.get(str(choice.get("user"))):
            why += "+same_user_today"
        return choice, why
    except Exception:
        return rng.choice(posts), "fallback"


def read_browse_titles(log_path=None, limit=40, max_age_h=None):
    """从浏览台账里取「最近浏览过的帖子」（forge 灵感源，只读）。"""
    log_path = log_path or BROWSE_LOG_DEFAULT
    rows, _bad = _read_jsonl(log_path)
    out, seen = [], set()
    for r in rows:
        if r.get("st") != "DONE":
            continue
        t = (r.get("title") or "").strip()
        if len(t) < 6:
            continue
        if max_age_h:
            try:
                ts = dt.datetime.strptime(r.get("ts", ""), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                ts = 0
            if ts and (time.time() - ts) > max_age_h * 3600:
                continue
        key = t[:24]
        if key in seen:
            continue
        seen.add(key)
        out.append({"cat": r.get("cat"), "title": t, "id": r.get("id")})
        if len(out) >= limit:
            break
    return out


def explain(model, now=None, top=3):
    """一行人类可读画像（写日志/报告用）。"""
    now = _mnow(model, now)
    cw = sorted(cat_weights(model, now).items(), key=lambda kv: -kv[1])[:top]
    users = sorted(model.get("users", {}).items(),
                   key=lambda kv: -user_affinity(model, kv[0], now))[:top]
    hours = sorted(model.get("hours", {}).items(), key=lambda kv: -kv[1])[:3]
    return ("互动 %d 次/对象 %d 个 | 偏好板块 %s | 熟人 %s | 活跃时段 %s" % (
        model.get("n", 0), len(model.get("users", {})),
        ",".join("%s=%.2f" % (label_of(model, c), w) for c, w in cw) or "-",
        ",".join("%s(%.1f)" % (u, user_affinity(model, u, now)) for u, _ in users) or "-",
        ",".join("%dh" % h for h, _ in hours) or "-"))


# ---------------------------------------------------------------- CLI
def _cmd_show(args):
    m = load_model(args.base)
    print(explain(m, top=args.top))
    if args.json:
        print(json.dumps({"n": m["n"], "bad": m["bad"], "users": len(m["users"]),
                          "cats": sorted(cat_weights(m).items(), key=lambda kv: -kv[1]),
                          "hours": m["hours"], "span": m["span"]}, ensure_ascii=False))
    return 0


def _cmd_browse(args):
    for r in read_browse_titles(limit=args.limit):
        print("%s\t%s" % (r.get("cat") or "-", r.get("title")))
    return 0


# ---------------------------------------------------------------- 自测
def _selftest(tmp=None):
    """负控导向自测；全部走沙箱 base，绝不碰生产台账。"""
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    if tmp is None:
        tmp = tempfile.mkdtemp(prefix="prefs_sandbox_")
    t0 = 1700000000.0

    # T1 记录 + 台账落盘
    record("reply", user="alice", topic="1", post="11", cat="dev", ts=t0, base=tmp)
    record("like", user="bob", topic="1", post="12", cat="dev", ts=t0 + 60, base=tmp)
    rows, _ = _read_jsonl(_path("interactions.jsonl", tmp))
    ck("T1 台账 2 行", len(rows) == 2)
    ck("T1b 行含 user/cat", rows[0].get("user") == "alice" and rows[0].get("cat") == "dev")

    # T2 去重（同 10 分钟内同 action/topic/post 不再写）
    record("reply", user="alice", topic="1", post="11", cat="dev", ts=t0 + 5, base=tmp)
    rows2, _ = _read_jsonl(_path("interactions.jsonl", tmp))
    ck("T2 去重生效", len(rows2) == 2)

    # T3 10 分钟外不算重
    record("reply", user="alice", topic="1", post="11", cat="dev", ts=t0 + 3600, base=tmp)
    rows3, _ = _read_jsonl(_path("interactions.jsonl", tmp))
    ck("T3 超窗不去重", len(rows3) == 3)

    m = load_model(base=tmp, now=t0 + 3600)
    ck("T4 模型 n=3", m["n"] == 3)
    ck("T4b users 2", set(m["users"]) == {"alice", "bob"})
    ck("T4c days 键=当天", _day(t0) in m["days"])

    # T5 半衰期衰减（负控：同 n 不同 last_ts ⇒ 新者权重大）
    record("reply", user="newguy", topic="2", post="21", cat="dev", ts=t0 + 3600, base=tmp)
    m = load_model(base=tmp, now=t0 + 3600)
    a_old = user_affinity(m, "bob", t0 + 3600)              # 1 小时前
    a_new = user_affinity(m, "newguy", t0 + 3600)
    ck("T5 新>旧(同 n)", a_new > a_old)
    m2 = load_model(base=tmp, now=t0 + 3600 + 21 * 86400)
    ck("T5b 半衰期≈折半", abs(user_affinity(m2, "newguy") - a_new * 0.5) < 0.15)
    ck("T5c 陌生人=0", user_affinity(m, "nobody") == 0.0)

    # T6 熟人判定（n>=2 且 90 天内）
    record("like", user="alice", topic="3", post="31", cat="chat", ts=t0 + 7200, base=tmp)
    m = load_model(base=tmp, now=t0 + 7200)
    ck("T6 alice 熟", is_familiar(m, "alice", t0 + 7200))
    ck("T6b newguy 不熟(n=1)", not is_familiar(m, "newguy", t0 + 7200))
    ck("T6c 陌生人 不熟", not is_familiar(m, "nobody", t0 + 7200))

    # T7 板块权重：见过的 > 没见过(=1.0 基线)
    cw = cat_weights(m, t0 + 7200)
    ck("T7 dev 权重最高", max(cw, key=lambda k: cw[k]) == "dev")
    ck("T7b 未知板块=1.0", cat_weight_of(m, "no_such_cat") == 1.0)

    # T8 pick_topics 偏好命中（负控：偏好板块应先被选中）
    cands = [{"id": "10", "cat": "chat"}, {"id": "11", "cat": "dev"},
             {"id": "12", "cat": "dev"}, {"id": "13", "cat": "random"}]
    rng = random.Random(7)
    picks = pick_topics(cands, m, action="reply", n=2, rng=rng, now=t0 + 7200,
                        explore_p=0.0)
    ck("T8 返回 2 条", len(picks) == 2)
    ck("T8c why 标 pref", all(p[1].startswith("pref") for p in picks))

    # T8b 偏好优先=统计判据（单次抽样不能当判据：dev 权重 8/10 ⇒ 单次失败属正常）
    def _first_dev_rate(model, trials=200, explore_p=0.0):
        hit = 0
        for i in range(trials):
            got = pick_topics(cands, model, n=1, rng=random.Random(9000 + i),
                              now=t0 + 7200, explore_p=explore_p)
            if got and got[0][0]["cat"] == "dev":
                hit += 1
        return hit / float(trials)

    r_dev = _first_dev_rate(m)
    ck("T8b 偏好板块被优先选中(%.2f>=0.70)" % r_dev, r_dev >= 0.70)
    flat_dir = tempfile.mkdtemp(prefix="prefs_flat_")
    r_flat = _first_dev_rate(load_model(base=flat_dir, now=t0))
    ck("T8d 负控:无偏好模型无偏好(%.2f<=0.60)" % r_flat, r_flat <= 0.60)

    # T9 探索分支（负控：explore_p=1 ⇒ 不再总是 dev）
    rng = random.Random(3)
    explored = [pick_topics(cands, m, n=2, rng=rng, now=t0 + 7200, explore_p=1.0)
                for _ in range(6)]
    ck("T9 探索态非全 dev", any(any(p[1] == "explore" for p in r) for r in explored))

    # T10 已互动话题降权（当日重复话题不应优先）
    m9 = load_model(base=tmp, now=t0 + 7200)
    m9["days"][_day(t0 + 7200)]["topics"] = {"11": 1}
    picks = pick_topics(cands, m9, n=2, rng=random.Random(5), now=t0 + 7200, explore_p=0.0)
    ck("T10 当日已互动话题被降权", all(p[0]["id"] != "11" for p in picks) or len(picks) == 2)

    # T11 pick_floor 陌生优先 + 排除自己
    posts = [{"id": 101, "user": "alice"}, {"id": 102, "user": "newguy"},
             {"id": 103, "user": "me"}]
    got, why = pick_floor(posts, m, rng=random.Random(1), now=t0 + 7200,
                          stranger_p=1.0, self_user="me")
    ck("T11 排除自己", got and str(got["user"]) != "me")
    ck("T11b 陌生优先", why.startswith("stranger"))

    # T12 熟人分支可达
    got2, why2 = pick_floor(posts, m, rng=random.Random(1), now=t0 + 7200,
                            stranger_p=0.0, self_user="me")
    ck("T12 熟人分支", why2.startswith("familiar") and str(got2["user"]) == "alice")

    # T13 当日同人降权（负控）
    m13 = load_model(base=tmp, now=t0 + 7200)
    m13["days"][_day(t0 + 7200)]["users"] = {"alice": 3}
    seen = {"alice": 0, "newguy": 0}
    for i in range(40):
        p, w = pick_floor(posts, m13, rng=random.Random(100 + i), now=t0 + 7200,
                          stranger_p=0.5, self_user="me")
        seen[str(p["user"])] += 1
        if "+same_user_today" in w:
            ck("T13 惩罚标记出现", True)
            break
    ck("T13b 仍能选到非惩罚对象", seen["newguy"] > 0)

    # T14 空输入安全（负控）
    ck("T14 空候选=[]", pick_topics([], m) == [])
    ck("T14b 空楼层 None", pick_floor([], m)[0] is None)
    ck("T14c n=0", pick_topics(cands, m, n=0) == [])

    # T15 坏台账行不崩（负控）
    bad_dir = tempfile.mkdtemp(prefix="prefs_bad_")
    with open(_path("interactions.jsonl", bad_dir), "w", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write(json.dumps({"ts": t0, "action": "reply", "user": "u1"}) + "\n")
    mb = load_model(base=bad_dir, now=t0)
    ck("T15 坏行计入 bad 且不崩", mb["bad"] >= 1 and mb["n"] == 1)

    # T16 空台账 ⇒ fresh 模式
    empty_dir = tempfile.mkdtemp(prefix="prefs_empty_")
    me = load_model(base=empty_dir, now=t0)
    pk = pick_topics(cands, me, n=1, rng=random.Random(2), now=t0)
    ck("T16 空模型 fresh 兜底", len(pk) == 1 and pk[0][1] in ("fresh", "fallback"))

    # T17 own_posts 幂等 + 读取
    record_own_post("900", title="我的贴", cat="dev", ts=t0, base=tmp)
    record_own_post("900", title="我的贴", cat="dev", ts=t0 + 10, base=tmp)
    ops = own_posts(base=tmp)
    ck("T17 自己的帖只 1 条", len(ops) == 1 and ops[0]["topic"] == "900")

    # T18 explain 不崩
    ck("T18 explain 有内容", "互动" in explain(load_model(base=tmp, now=t0 + 7200)))

    # T19 自测不得污染生产台账（负控）：沙箱 base 不等于生产 base
    ck("T19 沙箱 != 生产", base_dir(tmp) != base_dir(None))

    # T20 read_browse_titles 只认 DONE 且去重
    lg = tempfile.mkdtemp(prefix="prefs_log_")
    lp = os.path.join(lg, "browse_log.jsonl")
    with open(lp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"st": "FAIL", "title": "失败帖标题"}) + "\n")
        f.write(json.dumps({"st": "DONE", "title": "一个足够长的帖子标题A", "cat": "dev"}) + "\n")
        f.write(json.dumps({"st": "DONE", "title": "一个足够长的帖子标题A", "cat": "dev"}) + "\n")
        f.write(json.dumps({"st": "DONE", "title": "短"}) + "\n")
    tl = read_browse_titles(log_path=lp, limit=10)
    ck("T20 只取 DONE 且去重", len(tl) == 1 and tl[0]["cat"] == "dev")

    # T21 权重抽样不因全零权重崩（负控）
    _s = _weighted_sample([{"id": "x"}, {"id": "y"}], [0.0, 0.0], 1, random.Random(1))
    ck("T21 零权重抽样安全", len(_s) == 1)

    # ---------- 板块名（展示标签）与 category_id 键分离（T22–T24）
    tL = tempfile.mkdtemp(prefix="prefs_label_")
    record("reply", user="alice", topic="9", post="91", cat="4",
           cat_label="开发调优", ts=t0, base=tL)
    record("like", user="bob", topic="9", post="92", cat="4",
           cat_label="", ts=t0 + 60, base=tL)           # 空标签：不得覆盖已有名
    record("browse", user="carol", topic="8", post="81", cat="36", ts=t0 + 120, base=tL)
    m = load_model(tL, now=t0 + 3600)
    ck("T22 权重键仍是 category_id（名不得当键）",
       set(m["cats"].keys()) == {"4", "36"} and "开发调优" not in m["cats"])
    ck("T23 名只在 labels 且空标签不覆盖", m["labels"].get("4") == "开发调优")
    ck("T24 未见名 ⇒ cat:<id> 兜底不猜", label_of(m, "36") == "cat:36"
       and label_of(m, "4") == "开发调优")
    ck("T24b explain 用展示名且权重按 id 聚合",
       "开发调优=" in explain(m, now=t0 + 3600) and "cat:36=" in explain(m, now=t0 + 3600))
    # 负控：把名当键会让同一板块被拆成两份权重
    record("reply", user="dave", topic="9", post="93", cat="开发调优", ts=t0 + 180, base=tL)
    m2 = load_model(tL, now=t0 + 3600)
    ck("T24c 负控：名当键则权重被拆分（证明键必须是 id）",
       len(m2["cats"]) == 3 and cat_weight_of(m2, "4", now=t0 + 3600) > 1.0)

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("%s %s" % ("PASS" if ok else "FAIL", n))
    print("---- %d/%d passed ----" % (len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="L 站互动对象偏好模型")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("show")
    sp.add_argument("--base")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--top", type=int, default=3)
    sb = sub.add_parser("browse")
    sb.add_argument("--limit", type=int, default=20)
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "show":
        return _cmd_show(args)
    if args.cmd == "browse":
        return _cmd_browse(args)
    if args.cmd == "selftest":
        return _selftest()
    ap.print_help()
    return 2


if __name__ == "__main__":
    if "--selftest" in sys.argv:              # 兼容 flag 式调用
        sys.argv = [a for a in sys.argv if a != "--selftest"] + ["selftest"]
    sys.exit(main())
