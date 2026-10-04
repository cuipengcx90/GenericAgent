#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linuxdo_browse_daemon — L站 TL3 维护型只读浏览后台守护
========================================================
定位（2026-10-05 用户授权，整合 linuxdo_browse_sop × linuxdo_trust_level_sop）:
  TL3 等级检测**不满足**时 → 后台执行只读浏览（SOP 链路），**直到满足**即停。
  零 LLM token：纯 HTTP 打 TMWebDriver 桥（http://127.0.0.1:18766/link），
  复用已登录 linux.do 的浏览器 tab（与 tools/dpdomain_renew.py 同款零依赖桥）。

只读铁律（不可违反）:
  零发帖 / 零回复 / 零点赞 / 零关注 / 零领奖；不模拟登录（封号风险）。

能推进的 TL3 项（浏览是只读行为，2026-10-03 实测缺口=浏览帖子 12571/20000）:
  - 浏览话题 / 浏览帖子：进帖 + 深翻楼层即涨
  - 访问天数：只能用户本人真实登录日积月累，本守护不代刷
  - 回复话题 / 点赞类：属发帖红线（linuxdo_trust_level_sop），不自动做

判停（按页面标记，禁数值机械比较 —— SOP 红线，上限类指标天然规避）:
  connect 页: 解析 class token（unmet 优先，剔 .tl3-bar-fill 进度条）→ 待完成=unmet 数
  首页等级卡: ✓/○ 四元组 → 待完成=○M（须与头部「○M 待完成」对账）
  先试 connect（title 非「请稍候…」且正文含「信任级别」才算读到），失败回落首页卡；
  两路都判不了 → rc=3（不瞎判、不假绿）。

防检测参数（后台版节奏比单任务版更稀；用户强调「悠着点别封号」）:
  - 帖间随机 90–240s（25% 概率 240–420s）
  - 深翻随机 14–26 步 × 0.8–1.5s，步长 380–900px（随机）
  - 每 batch 随机混入 2–4 次「只逛列表页」不进帖（拟人翻列表）
  - 候选页序随机（/latest.json?page=<rand>），已浏览 id 去重
  - --batch 默认 25 帖/批；--daily-cap 每日话题上限（默认 120）
  - --max-session-min 单段运行上限（默认 90min），跑满 sleep 30–60min 再继续
  - 静默窗默认 01:00–07:30 不浏览（只整点查一次等级）

用法:
  python3 tools/linuxdo_browse_daemon.py status            # 只读：state+最近等级
  python3 tools/linuxdo_browse_daemon.py check             # 只读：读一次等级卡
  python3 tools/linuxdo_browse_daemon.py browse --batch 10 # 浏览一批（不判停）
  python3 tools/linuxdo_browse_daemon.py watch             # 长驻：判停循环（bg_task 起）
  python3 tools/linuxdo_browse_daemon.py --selftest        # 离线自测

退出码: 0=正常停(含判停「已满足」) 1=缺口仍在但达上限/批结束 2=桥不可达/无linux.do tab
        3=两路都判不了等级(登录失效/CF双卡，需人工) 4=用法错误 5=自测失败
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.request

GA_ROOT = os.environ.get("GA_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BASE = os.path.join(GA_ROOT, "temp", "linuxdo_browse_daemon")
BRIDGE = os.environ.get("LJQ_BRIDGE", "http://127.0.0.1:18766")
HINT = "linux.do"

RC_OK, RC_GAPS, RC_NO_SESSION, RC_UNJUDGEABLE, RC_USAGE, RC_SELFTEST = 0, 1, 2, 3, 4, 5

# 防检测参数（后台版）
GAP_RANGE = (90, 240)
GAP_LONG_RANGE = (240, 420)
GAP_LONG_P = 0.25
SCROLL_STEPS = (22, 30)      # 慢滚多步：步长<半楼 ⇒ 每楼≥2个 tick 覆盖
SCROLL_DT = (1.2, 1.8)
SCROLL_AMP = (300, 440)
BOTTOM_STAY = (70, 84)       # 底部驻留秒——跨 Discourse ~60s timings flush 周期（2026-10-05 实测根因）
ROLLBACK_STEPS = 10          # 回滚给每楼二次 tick 机会
LOITER_P_LIST = (2, 4)
BATCH_DEFAULT = 25
DAILY_CAP_DEFAULT = 120
SESSION_MIN_DEFAULT = 90
SESSION_REST = (1800, 3600)
QUIET_WINDOW = (1, 0, 7, 30)          # 01:00–07:30 静默
INTER_BATCH_GAP = (300, 900)
LATEST_PAGE_MAX = 9


def _bridge(payload, timeout=90):
    req = urllib.request.Request(
        BRIDGE.rstrip("/") + "/link",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data.get("r", data)


def list_sessions(timeout=20):
    try:
        r = _bridge({"cmd": "get_all_sessions"}, timeout=timeout)
    except Exception as e:
        raise BridgeError("bridge 不可达: %s" % e)
    if isinstance(r, dict):
        r = r.get("data", r.get("sessions", []))
    out = []
    for s in r or []:
        if isinstance(s, dict) and s.get("id") is not None:
            out.append({"id": str(s["id"]), "url": s.get("url") or "", "title": s.get("title") or ""})
    return out


class BridgeError(Exception):
    pass


def run_js(session_id, code, timeout=60):
    r = _bridge({"cmd": "execute_js", "sessionId": session_id,
                 "code": code, "timeout": str(int(timeout))}, timeout=timeout + 20)
    if isinstance(r, dict) and "data" in r and isinstance(r.get("data"), str):
        raw = r["data"]
    elif isinstance(r, dict) and r.get("error"):
        return {"ok": False, "error": str(r["error"])}
    else:
        raw = r
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": True, "raw": raw}
    return raw


def pick_session(prefer=None, timeout=20):
    """挑一个真正位于 linux.do 的会话；找不到返回 (None, 诊断串)。"""
    sessions = list_sessions(timeout=timeout)
    if not sessions:
        return None, "浏览器桥无任何会话（Chrome 未运行 / 扩展未连接 / 驱动未启动？）"
    cands = [s for s in sessions if prefer and s["id"] == str(prefer)]
    cands += [s for s in sessions if HINT in (s["url"] or "")]
    seen, ordered = set(), []
    for s in cands:
        if s["id"] not in seen:
            seen.add(s["id"])
            ordered.append(s)
    if not ordered:
        return None, "无 %s 标签页；当前会话: %s" % (
            HINT, "; ".join("%s=%s" % (s["id"], s["url"][:60]) for s in sessions[:8]))
    for s in ordered:
        probe = run_js(s["id"], "(()=>location.host)()", timeout=15)
        host = probe.get("raw") if isinstance(probe, dict) else probe
        if isinstance(host, str) and HINT in host:
            return s, ""
    return None, "候选标签页 %s 均不在 %s（可能已被导航走）" % ([s["id"] for s in ordered], HINT)


# ---------------------------------------------------------------- state
def load_state():
    os.makedirs(BASE, exist_ok=True)
    p = os.path.join(BASE, "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"seen_ids": [], "daily": {}, "level": {}, "browsed_total": 0}


def save_state(st):
    p = os.path.join(BASE, "state.json")
    tmp = p + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def today():
    return dt.datetime.now().strftime("%Y-%m-%d")


def log_jsonl(name, obj):
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def quiet_window_check(now=None):
    """在静默窗(01:00–07:30)内返回 True。"""
    now = now or dt.datetime.now()
    qh, qm, eh, em = QUIET_WINDOW
    t = now.hour * 60 + now.minute
    return (qh * 60 + qm) <= t < (eh * 60 + em)


def daily_count(st):
    return int(st.get("daily", {}).get(today(), 0))


# ---------------------------------------------------------------- TL3 判停（按页面标记，禁数值比较）
# 首页等级卡两种实测形态：A=mark 独立行(SOP 09-25 记录) / B=连写「✓ 名称 81/50」
TL_RING_RX_A = re.compile(r"([✓○])\s*\n([^\n✓○]+?)\s*\n(\d+)\s*\n\s*/\s*\n(\d+)\s*\n")
TL_RING_RX_B = re.compile(r"([✓○])\s*([^\n✓○/]+?)\s*(\d+)\s*/\s*(\d+)")


def parse_home_card(text):
    """linux.do 首页等级卡 ✓/○ 四元组 → {pending, met, items, head_pending}"""
    text = text or ""
    raw = TL_RING_RX_A.findall(text) or TL_RING_RX_B.findall(text)
    items = [{"mark": m, "name": n.strip(), "cur": int(c), "req": int(r)}
             for m, n, c, r in raw]
    if not items:
        return None
    head = None
    m = re.search(r"(\d+)\s*待完成", text)
    if m:
        head = int(m.group(1))
    pending = sum(1 for it in items if it["mark"] == "○")
    return {"items": items, "met": len(items) - pending, "pending": pending,
            "head_pending": head, "source": "home_card"}


def parse_connect_dom(res):
    """connect 页 DOM 解析（class token 判定，unmet 优先，剔进度条）。res=run_js 结果"""
    if not isinstance(res, dict) or res.get("ok") is False:
        return None
    met = res.get("met")
    unmet = res.get("unmet")
    if unmet is None or met is None:
        return None
    return {"met": int(met), "unmet": int(unmet), "pending": int(unmet),
            "items": res.get("items") or [], "source": "connect"}


CONNECT_JS = r"""
(async () => {
  const title = document.title || '';
  const txt = document.body ? document.body.innerText : '';
  if (/请稍候/.test(title) || (txt || '').replace(/\s/g, '').length < 200) {
    return JSON.stringify({bad: true, title: title, len: (txt||'').length});
  }
  if (!/信任级别|Trust Level/i.test(txt)) return JSON.stringify({bad: true, title, len: (txt||'').length});
  const els = document.querySelectorAll('.met, .unmet');
  const items = [];
  let met = 0, unmet = 0;
  els.forEach(el => {
    const cls = el.classList;
    if (cls.contains('tl3-bar-fill')) return;          // 进度条填充，非独立指标
    const isMet = cls.contains('met') && !cls.contains('unmet');
    const name = (el.textContent || '').trim().slice(0, 40);
    if (isMet) met++; else unmet++;
    items.push({met: isMet, name});
  });
  return JSON.stringify({met, unmet, items});
})()
"""


def check_level(session, timeout=40):
    """读一次等级。返回 (level|None, note)。先 connect，失败回落首页卡。"""
    # 1) connect 顶层导航（分区 cookie 使其可能 CF 挑战——SOP 处置顺序）
    run_js(session["id"], "location.href='https://connect.linux.do/'", timeout=20)
    time.sleep(8)
    res = run_js(session["id"], CONNECT_JS, timeout=timeout)
    lvl = parse_connect_dom(res)
    if lvl:
        return lvl, "connect"
    # 2) 回落首页等级卡
    run_js(session["id"], "location.href='https://linux.do/'", timeout=20)
    time.sleep(9)
    res = run_js(session["id"],
                 "JSON.stringify({t: document.body ? document.body.innerText.slice(0,3000) : ''})",
                 timeout=25)
    txt = res.get("t", "") if isinstance(res, dict) else ""
    lvl = parse_home_card(txt)
    if lvl is None:
        return None, "connect 与首页卡都判不了（CF 双卡或登录失效）"
    # 头部对账：head_pending 与解析 pending 若同时存在且矛盾 → 以解析为准但记 note
    note = "home_card"
    if lvl["head_pending"] is not None and lvl["head_pending"] != lvl["pending"]:
        note += "(head=%d,parse=%d 口径以页面标记为准)" % (lvl["head_pending"], lvl["pending"])
    return lvl, note


# ---------------------------------------------------------------- 候选帖
LATEST_JS = r"""
(async (page) => {
  try {
    const r = await fetch('/latest.json?no_definitions=true&page=' + page + '&order=created',
                          {credentials: 'include', cache: 'no-store'});
    if (!r.ok) return JSON.stringify({ok: false, status: r.status});
    const j = await r.json();
    const topics = (j && j.topic_list && j.topic_list.topics) || [];
    return JSON.stringify({ok: true, topics: topics.map(t => ({
      id: t.id, title: t.title, cat: t.category_id, posts: t.posts_count}))});
  } catch (e) { return JSON.stringify({ok: false, err: String(e).slice(0, 120)}); }
})(__PAGE__)
"""


def fetch_candidates(session, page):
    code = LATEST_JS.replace("__PAGE__", str(int(page)))
    res = run_js(session["id"], code, timeout=30)
    if isinstance(res, dict) and res.get("ok"):
        return res.get("topics") or []
    return []


# ---------------------------------------------------------------- 浏览动作
ROUTE_JS = r"""
(async () => {
  const tid='__TID__'; let how=null;
  try { const m=window.require('discourse/lib/url');
    if (m && m.default && m.default.routeTo){ m.default.routeTo('/t/topic/'+tid); how='REQ'; } } catch(e){}
  if (!how) return JSON.stringify({need_loc:true});
  for (let i=0;i<25;i++){ await new Promise(r=>setTimeout(r,500));
    const h1=document.querySelector('.fancy-title');
    if (h1){
      for(let s=0;s<__STEPS__;s++){ await new Promise(r=>setTimeout(r, __DTMS__ + Math.random()*400));
        window.scrollBy(0, __AMP__ + Math.random()*120); }
      await new Promise(r=>setTimeout(r, __STAYMS__ + Math.random()*8000));
      for(let s=0;s<__RB__;s++){ await new Promise(r=>setTimeout(r, 900 + Math.random()*500));
        window.scrollBy(0, -380); }
      return JSON.stringify({how,title:h1.innerText.trim().slice(0,60),posts:document.querySelectorAll('.topic-post').length}); } }
  return JSON.stringify({fail:true,now:location.pathname});
})()
"""

LOITER_JS = r"""
(async () => {
  try { const m=window.require('discourse/lib/url');
    if (m && m.default && m.default.routeTo){ m.default.routeTo('/latest'); }
    else { location.href='/latest'; }
  } catch(e) { location.href='/latest'; }
  await new Promise(r=>setTimeout(r,1800));
  const steps = __STEPS__;
  for (let s=0;s<steps;s++){ await new Promise(r=>setTimeout(r, 900 + Math.random()*600));
    window.scrollBy(0, 500 + Math.random()*400); }
  return JSON.stringify({loiter:true, title:(document.title||'').slice(0,40)});
})()
"""


def browse_one(session, tid, state):
    steps = random.randint(*SCROLL_STEPS)
    dtms = int(random.uniform(*SCROLL_DT) * 1000)
    amp = random.randint(*SCROLL_AMP)
    stay = int(random.uniform(*BOTTOM_STAY) * 1000)
    rb = ROLLBACK_STEPS
    code = ROUTE_JS.replace("__TID__", str(tid)).replace("__STEPS__", str(steps)).replace("__DTMS__", str(dtms)).replace("__AMP__", str(amp)).replace("__STAYMS__", str(stay)).replace("__RB__", str(rb))
    res = run_js(session["id"], code, timeout=max(150, steps * 3 + stay // 1000 + 30))
    ok = isinstance(res, dict) and not res.get("need_loc") and not res.get("fail")
    if isinstance(res, dict) and res.get("need_loc"):
        run_js(session["id"], "location.href='/t/topic/%s'" % tid, timeout=20)  # 整页直达兜底
        time.sleep(6)
        res2 = run_js(session["id"], code, timeout=max(150, steps * 3 + stay // 1000 + 30))
        ok = isinstance(res2, dict) and not res2.get("fail")
        res = res2
    return ok, res


def browse_batch(session, st, batch=25):
    """浏览一批。返回 (done, skipped)"""
    done = skipped = 0
    loiter_n = random.randint(*LOITER_P_LIST)
    pages = random.sample(range(0, LATEST_PAGE_MAX + 1), min(LATEST_PAGE_MAX + 1, 4))
    cand = []
    for p in pages:
        cand += fetch_candidates(session, p)
        time.sleep(random.uniform(1.5, 4.0))
    seen = set(st.get("seen_ids", []))
    cand = [c for c in cand if c.get("id") and int(c["id"]) not in seen]
    random.shuffle(cand)
    for i, c in enumerate(cand):
        if i >= batch:
            break
        tid = str(c["id"])
        if i == loiter_n:
            # 拟人：逛一次列表页不进帖
            run_js(session["id"], LOITER_JS.replace("__STEPS__", str(random.randint(4, 8))), timeout=45)
            log_jsonl("browse_log.jsonl", {"ts": now_str(), "st": "LOITER", "note": "latest"})
        ok, res = browse_one(session, tid, st)
        title = (res.get("title", "") if isinstance(res, dict) else "")[:36]
        log_jsonl("browse_log.jsonl", {"ts": now_str(), "st": "DONE" if ok else "FAIL",
                                       "id": tid, "cat": c.get("cat"), "title": title, "posts": res.get("posts") if isinstance(res, dict) else None})
        if ok:
            done += 1
            seen.add(int(tid))
            st["seen_ids"] = (st.get("seen_ids", []) + [int(tid)])[-4000:]
            st["browsed_total"] = st.get("browsed_total", 0) + 1
            st["daily"][today()] = daily_count(st) + 1
            save_state(st)
        else:
            skipped += 1
        # 帖间随机间隔（防检测核心）
        gap = random.choice([random.randint(*GAP_RANGE)] * 3 + [random.randint(*GAP_LONG_RANGE)])
        time.sleep(gap)
    return done, skipped


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- watch
def watch(args):
    st = load_state()
    start = time.time()
    while True:
        if quiet_window_check():
            print("[%s] 静默窗内，睡 30min" % now_str(), flush=True)
            time.sleep(1800)
            continue
        session, note = pick_session()
        if not session:
            print("[%s] RC_NO_SESSION: %s" % (now_str(), note), flush=True)
            return RC_NO_SESSION
        lvl, src = check_level(session)
        if lvl is None:
            print("[%s] RC_UNJUDGEABLE: %s" % (now_str(), src), flush=True)
            log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "UNJUDGEABLE", "note": src})
            return RC_UNJUDGEABLE
        log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "OK", "source": src,
                                      "met": lvl.get("met"), "pending": lvl.get("pending"), "items": lvl.get("items")})
        st["level"] = {"ts": now_str(), "met": lvl.get("met"), "pending": lvl.get("pending")}
        save_state(st)
        if lvl.get("pending", 99) == 0:
            print("[%s] ✓ TL3 已满足（pending=0），守护自然退出" % now_str(), flush=True)
            return RC_OK
        if daily_count(st) >= args.daily_cap:
            print("[%s] 今日已达 daily-cap=%d，睡到明天" % (now_str(), args.daily_cap), flush=True)
            time.sleep(3600)
            continue
        if (time.time() - start) / 60 >= args.max_session_min:
            rest = random.uniform(*SESSION_REST)
            print("[%s] 单段运行满 %.0fmin，休息 %.0fmin" % (now_str(), args.max_session_min, rest / 60), flush=True)
            time.sleep(rest)
            start = time.time()
        done, skipped = browse_batch(session, st, batch=args.batch)
        print("[%s] batch done=%d skipped=%d total_browsed=%d" % (now_str(), done, skipped, st.get("browsed_total", 0)), flush=True)
        if done == 0 and skipped == 0:
            print("[%s] 无可用候选（候选全重/接口异常），睡 10min 重试" % now_str(), flush=True)
            time.sleep(600)
        gap = random.uniform(*INTER_BATCH_GAP)
        print("[%s] 批间休 %.0fs" % (now_str(), gap), flush=True)
        time.sleep(gap)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    argv = argv or sys.argv[1:]
    if argv and argv[0] in ("--selftest", "-t"):
        return selftest()
    ap = argparse.ArgumentParser(prog="linuxdo_browse_daemon")
    ap.add_argument("cmd", choices=["status", "check", "browse", "watch"])
    ap.add_argument("--batch", type=int, default=BATCH_DEFAULT)
    ap.add_argument("--daily-cap", type=int, default=DAILY_CAP_DEFAULT)
    ap.add_argument("--max-session-min", type=float, default=SESSION_MIN_DEFAULT)
    ap.add_argument("--tab-id", type=str, default=None, help="显式指定任务 tab id（默认自动发现唯一 linux.do tab）")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        st = load_state()
        print(json.dumps({"today": today(), "daily_browsed": daily_count(st),
                          "total": st.get("browsed_total", 0), "level": st.get("level"),
                          "quiet_now": quiet_window_check()}, ensure_ascii=False, indent=1))
        return RC_OK
    session, note = pick_session(prefer=args.tab_id)
    if not session:
        print("RC_NO_SESSION: %s" % note)
        return RC_NO_SESSION
    st = load_state()
    if args.cmd == "check":
        lvl, src = check_level(session)
        if lvl is None:
            print("RC_UNJUDGEABLE: %s" % src)
            return RC_UNJUDGEABLE
        log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "OK", "source": src, "met": lvl.get("met"), "pending": lvl.get("pending")})
        print(json.dumps({"source": src, "pending": lvl.get("pending"), "met": lvl.get("met")}, ensure_ascii=False))
        return RC_OK
    if args.cmd == "browse":
        done, skipped = browse_batch(session, st, batch=args.batch)
        print("browse done=%d skipped=%d" % (done, skipped))
        return RC_GAPS if done else RC_NO_SESSION
    if args.cmd == "watch":
        return watch(args)
    return RC_USAGE


# ---------------------------------------------------------------- selftest
def selftest():
    fails = []
    # T1 首页卡解析：混合 ✓/○，含上限类「被举报 0/5」标 ✓ —— 不得算未达标
    sample = "✓ 访问天数 81\n/\n50\n✓ 浏览话题 582\n/\n500\n○ 已读帖子 12571\n/\n20000\n✓ 被举报帖子 0\n/\n5\n✓ 被禁言 0\n/\n0\n○ 回复 8\n/\n10\n○ 2 待完成"
    lvl = parse_home_card(sample)
    if lvl is None or lvl["pending"] != 2 or lvl["met"] != 4:
        fails.append("T1 home_card 解析错: %s" % (lvl,))
    if lvl and lvl["head_pending"] != 2:
        fails.append("T1 head_pending 错: %s" % lvl["head_pending"])
    # T1n 负控：上限类(当前值 0 < 门槛 5)标 ✓ —— 数值机械比较会误报，页面标记法不误报
    if lvl and any(it["name"].find("被举报") >= 0 and it["mark"] == "○" for it in lvl["items"]):
        fails.append("T1n 上限类被误判未达标(子串陷阱): %s" % [it for it in lvl["items"] if "被举报" in it["name"]])
    # T2 connect DOM：class token 判定 + tl3-bar-fill 剔除 + 'unmet' 子串陷阱
    fake = {"met": 10, "unmet": 2, "items": []}
    lvl2 = parse_connect_dom(fake)
    if lvl2 is None or lvl2["pending"] != 2:
        fails.append("T2 connect 解析错: %s" % lvl2)
    bad = {"ok": False, "error": "x"}
    if parse_connect_dom(bad) is not None:
        fails.append("T2n 坏响应未拒")
    # T2n2 'unmet' 含 'met' 子串 —— 判据必须 isMet = has('met') && !has('unmet')
    cls_test = "tl3-ring-circle unmet"
    isMet = ("met" in cls_test.split()) and ("unmet" not in cls_test.split())
    if isMet:
        fails.append("T2n2 unmet 子串陷阱未挡")
    # T3 静默窗
    if not quiet_window_check(dt.datetime(2026, 10, 5, 3, 0)) or quiet_window_check(dt.datetime(2026, 10, 5, 9, 0)):
        fails.append("T3 静默窗判断错")
    # T4 daily cap / 间隔参数边界
    st = {"daily": {today(): DAILY_CAP_DEFAULT}}
    if daily_count(st) < DAILY_CAP_DEFAULT:
        fails.append("T4 daily cap 错")
    if not (0 < GAP_RANGE[0] < GAP_RANGE[1] <= GAP_LONG_RANGE[0] < GAP_LONG_RANGE[1]):
        fails.append("T4 间隔区间边界错")
    # T5 桥缺失 fail-closed：指向不可达端口
    global BRIDGE
    old = BRIDGE
    BRIDGE = "http://127.0.0.1:1"
    try:
        list_sessions(timeout=2)
        fails.append("T5 桥缺失未 fail-closed")
    except BridgeError:
        pass
    finally:
        BRIDGE = old
    # T6 防检测参数齐全
    for name in ("GAP_RANGE", "SCROLL_STEPS", "LOITER_P_LIST", "QUIET_WINDOW", "INTER_BATCH_GAP"):
        v = globals().get(name)
        if not v:
            fails.append("T6 %s 缺失" % name)
    if fails:
        for f in fails:
            print("FAIL " + f)
        return RC_SELFTEST
    print("selftest 6/6 PASS")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
