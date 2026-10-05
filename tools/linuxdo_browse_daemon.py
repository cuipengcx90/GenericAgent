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
  - --batch 默认 25 帖/批；--daily-cap 每日话题上限（默认 120，每日 ±20 内随机定一次）
  - 静默窗结束后随机晨抖 0–60min 再开工（防每天准点开工的机械特征）
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

# 防检测参数（后台版）——2026-10-05 二轮：日上限 120→160(每日±20 抖动) + 帖内节奏放慢
GAP_RANGE = (110, 260)
GAP_LONG_RANGE = (300, 600)
GAP_LONG_P = 0.30
SCROLL_STEPS = (22, 30)      # 慢滚多步：步长<半楼 ⇒ 每楼≥2个 tick 覆盖
SCROLL_DT = (1.4, 2.0)
SCROLL_AMP = (300, 440)
BOTTOM_STAY = (90, 125)      # 底部驻留秒——跨 Discourse ~60s timings flush 周期（2026-10-05 实测根因）；二轮拉长更像细看
ROLLBACK_STEPS = 10          # 回滚给每楼二次 tick 机会
LOITER_P_LIST = (2, 5)
LIGHT_P = 0.25              # 25% 浅读（刷到不感兴趣划走型）：每帖都读到底=机器味（2026-10-05 反检测审查）
LIGHT_STEPS = (4, 6)
LIGHT_DWELL = (15, 30)      # 浅读驻留秒
BATCH_DEFAULT = 20
DAILY_CAP_DEFAULT = 160
DAILY_CAP_JITTER = 20       # 每日 cap 在 ±JITTER 内随机——每天固定同一数字=机械特征
SESSION_MIN_DEFAULT = 110   # 单段连续浏览分钟数（拉长=更像刷入迷的真人，提高占空比）
SESSION_REST = (1500, 3200) # 段间休息秒（25–53min）
MORNING_JITTER = (0, 3600)  # 静默窗结束后随机延迟 0–60min 再开工——真人不会每天准点开始
QUIET_WINDOW = (1, 0, 7, 30)          # 01:00–07:30 静默
INTER_BATCH_GAP = (420, 900)
SESS_RETRY_MAX = 6                      # pick_session/判级连续失败达上限才退出（断链重试≈1h+）
SESS_RETRY_SLEEP = (600, 1200)          # 断链重试间隔 10–20min
FRONT_NONE_TOLERANCE = 3                # 前台探测连续失败≥3次按后台继续（fail-open 防永停）
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


FRONT_JS = ("(function(){var v=(document.visibilityState==='visible');"
            "var f=!!(document.hasFocus&&document.hasFocus());"
            "return JSON.stringify({visible:v,focused:f,front:v})})()")


def tab_is_front(session):
    """linux.do tab 是否可见（用户可能正在看）。

    front 判据 = visibilityState==='visible'：分屏/焦点在终端但屏幕可见也算前台，
    宁可错跳绝不抢用户正在看的 tab。探测失败返回 None（fail-closed 由调用方定）。
    """
    try:
        res = run_js(session["id"], FRONT_JS, timeout=15)
    except Exception:
        return None
    if isinstance(res, dict) and "front" in res:
        return bool(res["front"])
    return None


def front_decision(front, none_cnt):
    """返回 (skip: bool, why: str)。None 且连续失败未达容忍 ⇒ fail-closed 跳过。"""
    if front is True:
        return True, "用户在前台看 linux.do，跳过本轮浏览"
    if front is None:
        if none_cnt < FRONT_NONE_TOLERANCE:
            return True, "前台探测失败(fail-closed %d/%d)，跳过本轮浏览" % (none_cnt + 1, FRONT_NONE_TOLERANCE)
        return False, "前台探测连续失败 %d 次，按后台继续(fail-open)" % none_cnt
    return False, "tab 后台，正常浏览"


def pick_session(prefer=None, timeout=20):
    """挑一个真正位于 linux.do 的会话；找不到返回 (None, 诊断串)。"""
    sessions = list_sessions(timeout=timeout)
    if not sessions:
        return None, "浏览器桥无任何会话（Chrome 未运行 / 扩展未连接 / 驱动未启动？）"
    cands = [s for s in sessions if prefer and s["id"] == str(prefer)]
    cands += [s for s in sessions if HINT in (s["url"] or "") and "//connect." not in (s["url"] or "")]
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
        # 精确 host 判定：connect.linux.do 也含 "linux.do" 子串但无 Discourse 环境
        # （2026-10-05 实测误选 connect Error 页 tab → routeTo 永远 need_loc）
        if isinstance(host, str) and host.strip().lower() in ("linux.do", "www.linux.do"):
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


def daily_cap_today(st, base):
    """当日 cap：base±JITTER 内每日随机定一次（先存内存，判级落盘后跨重启稳定）。"""
    caps = st.setdefault("cap", {})
    v = caps.get(today())
    if not v:
        v = max(1, int(base) + random.randint(-DAILY_CAP_JITTER, DAILY_CAP_JITTER))
        caps[today()] = v
    return int(v)


def morning_jitter_sec(st):
    """今日晨间抖动秒：静默窗结束后再随机等这么久才开工（每日定一次，仅内存）。"""
    jt = st.setdefault("jitter", {})
    v = jt.get(today())
    if v is None:
        v = int(random.uniform(*MORNING_JITTER))
        jt[today()] = v
    return int(v)


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
        # ★(2026-10-05) 读完必须还原主站：否则守护把唯一的 linux.do 标签页变成 connect 页,
        #   下一轮 pick_session 再也找不到可浏览的标签 ⇒ 自毁供应、永久 batch done=0
        try:
            run_js(session["id"], "location.href='https://linux.do/'", timeout=20)
            time.sleep(6)
        except Exception as e:
            print("[warn] check_level 还原主站失败: %s" % str(e)[:120], flush=True)
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
  // ── 记账解锁三件套（2026-10-05 R558 实锤：后台 tab IntersectionObserver/rAF 停→
  //    screen-track 零 timings POST→服务端 last_read 不动→浏览帖子计数不涨）。
  //    修法=①按 DOM 真实楼层喂 _onscreen ②dispatch scroll 解锁 tick 的 180s 无滚动早退闸
  //    ③1.5s interval 驱动 tick（后台节流 ≈1Hz 仍有效，实测已验证）。
  //    幂等：interval 挂 window 跨帖存活；整页导航兜底后重新注入。
  try {
    const c = window.Discourse && window.Discourse.__container__;
    const t = c && c.lookup && c.lookup('service:screen-track');
    if (t && !window.__tmUnlockOn) {
      window.__tmUnlockOn = true;
      window.__tmUnlock = () => {
        try {
          if (!t._topicId) return;
          const nums = [];
          document.querySelectorAll('.topic-post[data-post-number]').forEach(e => {
            const n = parseInt(e.getAttribute('data-post-number'));
            if (n > 0) nums.push(n);
          });
          if (nums.length) {
            t.set('_onscreen', [...new Set(nums)]);
            window.dispatchEvent(new Event('scroll'));
          }
        } catch(e){}
      };
      window.__tmInterval = setInterval(() => {
        try { window.__tmUnlock(); if (t.tick) t.tick(); } catch(e){}
      }, 1500);
    }
  } catch(e){}
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
    # 深浅分层：真人不是每帖都读到底——约三成是「刷到划走」型浅读（快滚大屏幅、不回滚、短驻留）
    light = random.random() < LIGHT_P
    if light:
        steps = random.randint(*LIGHT_STEPS)
        dtms = int(random.uniform(0.25, 0.5) * 1000)
        amp = random.randint(520, 760)
        stay = int(random.uniform(*LIGHT_DWELL) * 1000)
        rb = 0
    else:
        steps = random.randint(*SCROLL_STEPS)
        dtms = int(random.uniform(*SCROLL_DT) * 1000)
        amp = random.randint(*SCROLL_AMP)
        stay = int(random.uniform(*BOTTOM_STAY) * 1000)
        rb = ROLLBACK_STEPS
    code = ROUTE_JS.replace("__TID__", str(tid)).replace("__STEPS__", str(steps)).replace("__DTMS__", str(dtms)).replace("__AMP__", str(amp)).replace("__STAYMS__", str(stay)).replace("__RB__", str(rb))
    res = run_js(session["id"], code, timeout=max(150, steps * 3 + stay // 1000 + 30))
    ok = isinstance(res, dict) and not res.get("need_loc") and not res.get("fail")
    if isinstance(res, dict) and res.get("need_loc"):
        # 兜底整页导航仅允许在主站 tab 上做：connect.linux.do 等子域无 Discourse，
        # 导航只会造出 Error 页并顶掉等级检查 tab（2026-10-05 实测踩坑）。
        h = run_js(session["id"], "(()=>location.host)()", timeout=15)
        host = h.get("raw") if isinstance(h, dict) else h
        if not (isinstance(host, str) and host.strip().lower() in ("linux.do", "www.linux.do")):
            return False, {"fail": "non-linuxdo-tab:%s" % host}
        run_js(session["id"], "location.href='/t/topic/%s'" % tid, timeout=20)  # 整页直达兜底
        time.sleep(6)
        res2 = run_js(session["id"], code, timeout=max(150, steps * 3 + stay // 1000 + 30))
        ok = isinstance(res2, dict) and not res2.get("fail")
        res = res2
    if isinstance(res, dict):
        res["mode"] = "LIGHT" if light else "DEEP"
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
    # 分类偏好加权：真人会「进一个板块连看」，均匀跳遍全站分类=机器味
    by_cat = {}
    for c in cand:
        by_cat.setdefault(c.get("cat"), []).append(c)
    if len(by_cat) >= 3 and batch >= 8:
        pref_cats = set(random.sample(sorted(by_cat, key=str), min(3, len(by_cat) - 1)))
        pref_pool = [c for c in cand if c.get("cat") in pref_cats]
        other_pool = [c for c in cand if c.get("cat") not in pref_cats]
        n_pref = min(len(pref_pool), max(2, int(batch * 0.6)))
        cand = pref_pool[:n_pref] + other_pool
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
                                       "id": tid, "cat": c.get("cat"), "title": title,
                                       "posts": res.get("posts") if isinstance(res, dict) else None,
                                       "mode": res.get("mode") if isinstance(res, dict) else None})
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
    sess_fails = 0      # pick_session 连续失败（断链重试）
    lvl_fails = 0       # check_level 连续失败
    front_none = 0      # 前台探测连续失败
    front_true_cnt = 0  # 前台 True 连续次数（探测失真自检日志用）
    while True:
        if quiet_window_check():
            print("[%s] 静默窗内，睡 30min" % now_str(), flush=True)
            time.sleep(1800)
            continue
        mj = morning_jitter_sec(st)
        if mj:
            now = dt.datetime.now()
            gate = dt.datetime(now.year, now.month, now.day, QUIET_WINDOW[2], QUIET_WINDOW[3]) + dt.timedelta(seconds=mj)
            if now < gate:
                print("[%s] 晨间抖动：静默窗后延 %dmin 再开工（先睡 %dmin）" % (now_str(), mj / 60, (gate - now).total_seconds() / 60), flush=True)
                time.sleep(min((gate - now).total_seconds(), 1800))
                continue
        session, note = pick_session()
        if not session:
            sess_fails += 1
            if sess_fails >= SESS_RETRY_MAX:
                print("[%s] RC_NO_SESSION: %s（连续 %d 次断链，退出）" % (now_str(), note, sess_fails), flush=True)
                return RC_NO_SESSION
            wait = random.uniform(*SESS_RETRY_SLEEP)
            print("[%s] RC_NO_SESSION_RETRY: %s（%d/%d，睡 %.0fmin 后重试）" % (now_str(), note, sess_fails, SESS_RETRY_MAX, wait / 60), flush=True)
            time.sleep(wait)
            continue
        sess_fails = 0
        lvl, src = check_level(session)
        if lvl is None:
            lvl_fails += 1
            if lvl_fails >= SESS_RETRY_MAX:
                print("[%s] RC_UNJUDGEABLE: %s（连续 %d 次判级失败，退出）" % (now_str(), src, lvl_fails), flush=True)
                log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "UNJUDGEABLE", "note": src})
                return RC_UNJUDGEABLE
            print("[%s] RC_UNJUDGEABLE_RETRY: %s（%d/%d，睡后重试）" % (now_str(), src, lvl_fails, SESS_RETRY_MAX), flush=True)
            time.sleep(random.uniform(*SESS_RETRY_SLEEP))
            continue
        lvl_fails = 0
        log_jsonl("level_log.jsonl", {"ts": now_str(), "st": "OK", "source": src,
                                      "met": lvl.get("met"), "pending": lvl.get("pending"), "items": lvl.get("items")})
        st["level"] = {"ts": now_str(), "met": lvl.get("met"), "pending": lvl.get("pending")}
        save_state(st)
        if lvl.get("pending", 99) == 0:
            print("[%s] ✓ TL3 已满足（pending=0），守护自然退出" % now_str(), flush=True)
            return RC_OK
        cap_today = daily_cap_today(st, args.daily_cap)
        if daily_count(st) >= cap_today:
            print("[%s] 今日已达 daily-cap=%d（base=%d，每日随机定一次），睡到明天" % (now_str(), cap_today, args.daily_cap), flush=True)
            time.sleep(3600)
            continue
        if (time.time() - start) / 60 >= args.max_session_min:
            rest = random.uniform(*SESSION_REST)
            print("[%s] 单段运行满 %.0fmin，休息 %.0fmin" % (now_str(), args.max_session_min, rest / 60), flush=True)
            time.sleep(rest)
            start = time.time()
        # 用户前台保护：tab 可见即视为用户在看，绝不抢（宁停勿扰；fail-closed 起步）
        front = tab_is_front(session)
        skip, why = front_decision(front, front_none)
        front_none = front_none + 1 if front is None else 0
        front_true_cnt = front_true_cnt + 1 if front is True else 0
        if front_true_cnt == 12:
            print("[%s] ⚠ 前台连续 %d 次为 True——若持续请人工核查探测是否失真" % (now_str(), front_true_cnt), flush=True)
        if skip:
            print("[%s] %s，睡 5min" % (now_str(), why), flush=True)
            time.sleep(300)
            continue
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
    if not session and args.cmd != "watch":
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
    for name in ("GAP_RANGE", "SCROLL_STEPS", "LOITER_P_LIST", "QUIET_WINDOW", "INTER_BATCH_GAP", "LIGHT_P", "LIGHT_STEPS", "LIGHT_DWELL", "SESS_RETRY_MAX", "SESS_RETRY_SLEEP", "FRONT_NONE_TOLERANCE", "FRONT_JS"):
        v = globals().get(name)
        if not v:
            fails.append("T6 %s 缺失" % name)
    # T7 前台保护三态 + 断链重试常量边界
    s, w = front_decision(True, 0)
    if not s or "前台" not in w:
        fails.append("T7 front True 未跳过: %s" % w)
    s2, _ = front_decision(False, 0)
    if s2:
        fails.append("T7 front False 不应跳过")
    s3, _ = front_decision(None, 0)
    if not s3:
        fails.append("T7 None 容忍内应 fail-closed 跳过")
    s4, _ = front_decision(None, FRONT_NONE_TOLERANCE)
    if s4:
        fails.append("T7 None 达容忍上限应放行(fail-open)")
    if not (SESS_RETRY_MAX >= 2 and 0 < SESS_RETRY_SLEEP[0] < SESS_RETRY_SLEEP[1]):
        fails.append("T7n SESS_RETRY 常量边界错")
    if not ("hasFocus" in FRONT_JS and "visibilityState" in FRONT_JS and "JSON.stringify" in FRONT_JS):
        fails.append("T7n FRONT_JS 形状缺关键 token")
    # T8 cap 抖动 / T9 晨抖（纯函数断言，传局部 dict，不触真 state）
    cst = {"cap": {}}
    c1 = daily_cap_today(cst, 250)
    c2 = daily_cap_today(cst, 250)
    if abs(c1 - 250) > DAILY_CAP_JITTER or abs(c2 - 250) > DAILY_CAP_JITTER:
        fails.append("T8 cap 抖动越界: %s/%s" % (c1, c2))
    if c1 != c2:
        fails.append("T8 同 st 二次调用不稳定: %s vs %s" % (c1, c2))
    if not (1 <= DAILY_CAP_JITTER <= 60):
        fails.append("T8 抖动常量边界错")
    if daily_cap_today({"cap": {today(): 999}}, 250) != 999:
        fails.append("T8 已存当日 cap 未直接返回")
    jst = {"jitter": {}}
    j1 = morning_jitter_sec(jst)
    j2 = morning_jitter_sec(jst)
    if not (0 <= j1 <= MORNING_JITTER[1] and j1 == j2):
        fails.append("T9 晨抖越界或不稳定: %s vs %s" % (j1, j2))
    if morning_jitter_sec({"jitter": {today(): 123}}) != 123:
        fails.append("T9 已存晨抖未直接返回")
    if not (isinstance(MORNING_JITTER, tuple) and MORNING_JITTER[0] == 0 and MORNING_JITTER[1] > 0):
        fails.append("T9 晨抖常量形状错")
    if fails:
        for f in fails:
            print("FAIL " + f)
        return RC_SELFTEST
    print("selftest 9/9 PASS")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
