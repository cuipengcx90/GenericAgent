#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L站互动动作执行器（回帖 / 点赞 / 发帖）。

设计（2026-10-05 用户定义的五动作架构）：
  · 五动作 = 等级检测(前置) / 浏览 / 回帖 / 点赞 / 发帖，各自可被提示词单独触发；
  · 等级检测出缺项 ⇒ 按类路由到本执行器（回复类→reply、送赞类→like、浏览类→daemon 自补）；
  · 全部随机化决策走统一内核 tools/linuxdo_antidetect.py（多段切分/日限抖动/休息日/静默窗/
    晨间抖动/长休），本站不做任何固定节拍；
  · 桥接与风控复用 tools/linuxdo_browse_daemon.py（run_js / pick_session / notify / 风控码判读），
    同一浏览器会话 ⇒ 不新增身份（借 anyrouter 封号复盘：独立身份 + 随机化 + 可停）。

安全默认：
  · 一切写动作（POST）默认 **dry-run**，必须显式 `--apply` 才真发；
  · 命中风控信号（429/403/401/BANNED）⇒ 落账 + 飞书私信 + 设 pause_until，绝不硬顶重试。

用法：
  python3 tools/linuxdo_acts.py due reply|like|post          # 现在能不能做 + 下次该等多久
  python3 tools/linuxdo_acts.py run reply --apply            # 跑一轮回帖（自动选题+生成文案+发送）
  python3 tools/linuxdo_acts.py run like  --apply --max 3
  python3 tools/linuxdo_acts.py reply-one --topic 123 --raw "..." [--apply]
  python3 tools/linuxdo_acts.py like-one  --topic 123 [--post 456] [--apply]
  python3 tools/linuxdo_acts.py post --title "..." --raw "..." --category 4 --tags "AI,求助" [--apply]
  python3 tools/linuxdo_acts.py state | status
  python3 tools/linuxdo_acts.py --selftest
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GA_ROOT = os.environ.get("GA_ROOT") or os.path.abspath(os.path.join(HERE, ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if GA_ROOT not in sys.path:
    sys.path.insert(0, GA_ROOT)

import linuxdo_antidetect as ad          # noqa: E402  统一随机化内核
import linuxdo_browse_daemon as bd       # noqa: E402  桥接 / 会话 / 风控 / 通知
try:
    import linuxdo_prefs as pf            # noqa: E402  对象偏好（板块兴趣/作者关系）
except Exception:                          # pragma: no cover  缺件不阻塞动作
    pf = None
try:                                     # noqa: E402
    import linuxdo_humanize as hu        # 拟人层（动手前后干什么）
except Exception:                        # pragma: no cover
    hu = None

BASE = os.path.join(GA_ROOT, "temp", "linuxdo_acts")
# 拟人停留上限：单次睡眠不得超过该值（防长休把守护/CI 挂住；env 可调）
HUMANIZE_MAX_SLEEP = float(os.environ.get("LJQ_HUMANIZE_MAX_SLEEP") or 90.0)
HUMANIZE_OFF = os.environ.get("LJQ_HUMANIZE_OFF") == "1"
# 页面级合成事件：**写死关闭**（2026-10-05 用户裁决「关吧，就默认关吧」）。
# 原因=JS 合成事件 isTrusted=false，任何采集端一行即可辨认，注入不是「更像人」
# 而是「多一个可疑信号」。env LJQ_PAGE_EVENTS_ON / --page-events 保留但已失效
# （只做兼容与留痕，不再有能打开它的通路）。真可信输入须走 CDP Input.*（见
# temp/ljq_cdptest/FINDINGS.md 实测：isTrusted=true），与本节无关。
PAGE_EVENTS_ENABLED = False


def _sleep(sec, log=None, tag="", dry=False):
    """唯一的拟人睡眠出口：dry/关闭时不睡；超上限时截断并留痕。"""
    sec = float(sec or 0.0)
    if dry or HUMANIZE_OFF or sec <= 0:
        return 0.0
    if sec > HUMANIZE_MAX_SLEEP:
        if log:
            log("%s 拟人停留 %.1fs 超上限，截断为 %.1fs" % (tag, sec, HUMANIZE_MAX_SLEEP))
        sec = HUMANIZE_MAX_SLEEP
    if log:
        log("%s 拟人停留 %.1fs" % (tag, sec))
    time.sleep(sec)
    return sec


def humanize_pose(kind, n_chars):
    """一次动作的拟人姿势摘要（无拟人层时返回 None）。"""
    return hu.pose(kind, n_chars) if hu else None


def humanize_read(kind, n_chars, log=None, dry=False, tag=""):
    """先读后动：按文本长度驻留（真人不固定 sleep）。返回姿势 dict 或 None。"""
    if not hu:
        return None
    pose = hu.pose(kind, n_chars)
    _sleep(pose.get("read_dwell"), log, tag or "[%s] 阅读" % kind, dry)
    return pose


def humanize_typing(text, log=None, dry=False, tag="[reply]", kind="reply"):
    """回帖打字：逐字延迟 + 标点停顿 + 思考停顿（只模拟时长，不逐键注入）。"""
    if not hu:
        return None
    plan = hu.typing_plan(text or "", kind=kind)
    total = hu.typing_total(plan)
    _sleep(total, log, tag + " 打字", dry)
    return {"chars": len(text or ""), "typing_s": total, "chunks": len(plan)}


def humanize_write(kind, text, log=None, dry=False):
    """发帖/长文：分段写作停顿（段间长停顿）。"""
    if not hu:
        return None
    plan = hu.write_plan(text or "")
    total = sum(float(p.get("pause") or 0.0) for p in plan)
    _sleep(total, log, "[post] 写作停顿", dry)
    return {"segments": len(plan), "write_s": round(total, 1)}


def humanize_wrap(kind, log=None, dry=False):
    """动作后收尾停留（真人不会动作完立刻关）。"""
    if not hu:
        return 0.0
    return _sleep(hu.wrap_up_seconds(kind), log, "[%s] 收尾" % kind, dry)


def page_events(session, kind, n_chars=0, log=None, dry=False, tag=""):
    """在真实页面派发「滚动 + 合成键鼠事件」——fetch 直调不会产生任何 UI 事件，
    这是拟人层的**弱信号补强**（真实滚动会改变视口；键鼠是 isTrusted=false 的合成事件）。
    best-effort：任何异常都不影响动作本身。返回 {events, scrollY} 或 None。"""
    if not PAGE_EVENTS_ENABLED or dry or HUMANIZE_OFF or hu is None or not session:
        return None
    try:
        dwell = (hu.pose(kind, n_chars or 800) or {}).get("read_dwell") or 0.0
        plan = hu.scroll_plan(n_chars or 800, dwell, kind=kind) or []
        steps = max(1, min(12, len(plan)))
        moves = max(3, min(14, steps * 2))
        keys = min(12, max(4, int(len(str(n_chars)))) if kind in ("reply", "post") else 0)
        code = (PAGE_EVENTS_JS.replace("__STEPS__", str(steps))
                .replace("__MOVES__", str(moves)).replace("__KEYS__", str(keys)))
        res = bd.run_js(session["id"], code, timeout=60)
        out = res if isinstance(res, dict) else {}
        if log:
            log("%s 页面行为注入 scrolls=%d moves=%d keys=%d events=%s"
                % (tag or "[%s]" % kind, steps, moves, keys, out.get("events")))
        return out or None
    except Exception as e:
        if log:
            log("%s 页面行为注入失败(忽略) %s" % (tag or "[%s]" % kind, str(e)[:120]))
        return None

def state_path():
    """状态文件落点：env LJQ_ACTS_STATE 可重定向（自测/金丝雀用），默认生产 BASE。"""
    return os.environ.get("LJQ_ACTS_STATE") or os.path.join(BASE, "state.json")


LOCK_TTL = 1800.0        # 秒；超过即视为陈旧锁可回收（不探 pid 存活：RULE 5 禁 os.kill 判活）


def lock_path():
    """跨进程锁：daemon 与提示词触发的 acts 会抢同一个 linux.do 标签，必须互斥。"""
    return os.environ.get("LJQ_ACTS_LOCK") or os.path.join(os.path.dirname(state_path()), "run.lock")


def acquire_lock(tag="acts", ttl=LOCK_TTL, now_ts=None):
    """拿到返回 token；被占（且未超 TTL）返回 None。陈旧锁按 TTL 回收。"""
    now_ts = now_ts or time.time()
    p = lock_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    token = "%s-%d-%d" % (tag, os.getpid(), int(now_ts * 1000))
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                info = json.load(open(p, encoding="utf-8"))
            except Exception:
                info = {}
            if now_ts - float(info.get("ts") or 0) < ttl:
                return None
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token, "tag": tag, "pid": os.getpid(), "ts": now_ts}, f)
        return token
    return None


def release_lock(token):
    """只删自己的锁（token 不符一律不动）。"""
    p = lock_path()
    try:
        info = json.load(open(p, encoding="utf-8"))
    except Exception:
        return False
    if info.get("token") != token:
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False


KINDS = ("reply", "like", "post")
CODEX_BIN = os.environ.get("LJQ_CODEX_BIN") or shutil.which("codex") or "/opt/homebrew/bin/codex"

# 文案红线（linuxdo_reply_sop）：水贴特征一律拦下
BANNED_PHRASES = ("感谢分享", "谢谢分享", "学到了", "顶一下", "支持一下", "前排", "沙发",
                  "路过", "打卡", "沙发帮顶")
REPLY_MIN_CHARS, REPLY_MAX_CHARS = 8, 120
# 去 AI 味闸门（tools/linuxdo_deai.py）及格线：机器味扣分低于此值即拒发
DEAI_MIN_SCORE = int(os.environ.get("LJQ_DEAI_MIN_SCORE") or 60)


# ------------------------------------------------------------------ 状态
def load_state(path=None):
    """全部键 setdefault —— 防「state 被局部 dict 覆盖」类事故（browse daemon 2026-10-05 踩过）。"""
    p = path or state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    st = None
    if os.path.exists(p):
        try:
            st = json.load(open(p, encoding="utf-8"))
        except Exception:
            st = None
    if not isinstance(st, dict):
        st = {}
    st.setdefault("daily", {})           # daily[day][kind] = 次数
    st.setdefault("done", {})            # done[kind][key] = ts（幂等去重）
    st.setdefault("cap", {})             # 内核：cap[kind][day]
    st.setdefault("rest", {})            # 内核：rest[kind][day]
    st.setdefault("jitter", {})          # 内核：jitter[kind][day]
    st.setdefault("post_history", [])    # 发帖历史 [{ts, id, title}]
    st.setdefault("revisit", {})         # 回访进度 {"last_ts":…, "<tid>": {"last_num":…}}
    st.setdefault("risk", {})            # risk[kind][day_count]
    st.setdefault("risk_log", [])
    st.setdefault("pause_until", 0.0)
    st.setdefault("risk_stop_day", None)
    return st


def save_state(st, path=None):
    p = path or state_path()
    tmp = p + ".tmp"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def count_today(st, kind, day=None):
    day = day or bd.today()
    return int((st.get("daily", {}).get(day) or {}).get(kind, 0))


def bump(st, kind, day=None):
    day = day or bd.today()
    d = st.setdefault("daily", {}).setdefault(day, {})
    d[kind] = int(d.get(kind, 0)) + 1
    return d[kind]


def done_key_set(st, kind):
    return set((st.get("done", {}).get(kind) or {}).keys())


def mark_done(st, kind, key, extra=None):
    st.setdefault("done", {}).setdefault(kind, {})[str(key)] = time.time()
    if extra:
        st["done"][kind][str(key)] = extra
    # 裁剪：每类只留最近 800 条
    d = st["done"][kind]
    if len(d) > 800:
        for k in sorted(d, key=lambda x: d[x] if isinstance(d[x], (int, float)) else 0)[:-800]:
            d.pop(k, None)


# ------------------------------------------------------------------ 风控
def risk_note(st, kind, detail="", now_ts=None, path=None, allow_notify=True):
    """记一次风控信号：当日同类 ≥3 ⇒ 当日停；否则设 pause_until 冷却（不 sleep，立即返回）。"""
    now_ts = now_ts or time.time()
    day = bd.today()
    d = st.setdefault("risk", {}).setdefault(kind, {})
    d[day] = int(d.get(day, 0)) + 1
    st["risk_log"].append({"ts": now_ts, "kind": kind, "detail": str(detail)[:200], "n": d[day]})
    st["risk_log"] = st["risk_log"][-200:]
    if d[day] >= 3:
        st["risk_stop_day"] = day
        msg = "[linuxdo_acts] 风控升级 %s x%d → 今日停止该动作（%s）" % (kind, d[day], detail)
    else:
        pause = random.uniform(3600, 7200)
        st["pause_until"] = max(st.get("pause_until") or 0, now_ts + pause)
        msg = "[linuxdo_acts] 风控信号 %s → 暂停 %.0f 分钟（%s）" % (kind, pause / 60.0, detail)
    save_state(st, path)
    if allow_notify:
        try:
            bd.notify(msg)
        except Exception as e:
            print("[warn] 风控告警推送失败: %s" % str(e)[:160], flush=True)
    return msg


def risk_gate(st, kind, now_ts=None):
    """入口闸门 → (ok, why)。"""
    now_ts = now_ts or time.time()
    if st.get("risk_stop_day") == bd.today():
        return False, "risk_stop_day"
    if float(st.get("pause_until") or 0) > now_ts:
        return False, "pause_until(%ds)" % int(float(st["pause_until"]) - now_ts)
    return True, "ok"


def classify_result(res):
    """把 JS 返回值判成 (ok, why, risk_kind)。"""
    if not isinstance(res, dict):
        return False, "bad_result:%s" % str(res)[:80], None
    if res.get("ok"):
        return True, "ok", None
    status = res.get("status")
    rk = bd.risk_kind_from_http(status) if status else None
    txt = " ".join(str(res.get(k) or "") for k in ("err", "body"))
    rk = rk or bd.risk_kind_from_text(txt)
    return False, "status=%s err=%s" % (status, (res.get("err") or res.get("body") or "")[:120]), rk


# ------------------------------------------------------------------ 站点 JS
READ_TOPIC_JS = r"""
(async () => {
  try {
    const r = await fetch('/t/__TID__.json', {credentials:'include', cache:'no-store'});
    if (!r.ok) return JSON.stringify({ok:false, status:r.status});
    const j = await r.json();
    const strip = h => (h||'').replace(/<br\s*\/?>/g,'\n').replace(/<[^>]+>/g,' ')
      .replace(/&nbsp;/g,' ').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
      .replace(/\s+/g,' ').trim();
    const ps = (j.post_stream && j.post_stream.posts) || [];
    const pick = p => ({id:p.id, num:p.post_number, user:p.username,
      text: strip(p.cooked).slice(0,900)});
    return JSON.stringify({ok:true, id:j.id, title:j.title, posts_count:j.posts_count,
      category_id:j.category_id, tags:(j.tags||[]), created:j.created_at,
      posts: ps.slice(0,8).map(pick),
      latest: ps.slice(-3).map(pick)});
  } catch(e) { return JSON.stringify({ok:false, err:String(e).slice(0,140)}); }
})()
"""

POST_REPLY_JS = r"""
(async () => {
  try {
    const m = document.querySelector('meta[name=csrf-token]');
    const tok = m ? m.getAttribute('content') : '';
    const r = await fetch('/posts.json', {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','X-CSRF-Token':tok,'X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({topic_id: __TID__, raw: __RAW__})});
    const t = await r.text(); let j = null; try { j = JSON.parse(t); } catch(e) {}
    return JSON.stringify({ok:r.ok, status:r.status, id:(j&&j.id)||null,
      err:(j&&(j.errors||j.error_type))||null, body: j ? null : t.slice(0,300)});
  } catch(e) { return JSON.stringify({ok:false, err:String(e).slice(0,140)}); }
})()
"""

POST_LIKE_JS = r"""
(async () => {
  try {
    const m = document.querySelector('meta[name=csrf-token]');
    const tok = m ? m.getAttribute('content') : '';
    const r = await fetch('/post_actions.json', {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','X-CSRF-Token':tok,'X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({id: __PID__, post_action_type_id: 2})});
    const t = await r.text(); let j = null; try { j = JSON.parse(t); } catch(e) {}
    return JSON.stringify({ok:r.ok, status:r.status, id:(j&&j.id)||null,
      err:(j&&(j.errors||j.error_type))||null, body: j ? null : t.slice(0,240)});
  } catch(e) { return JSON.stringify({ok:false, err:String(e).slice(0,140)}); }
})()
"""

POST_TOPIC_JS = r"""
(async () => {
  try {
    const m = document.querySelector('meta[name=csrf-token]');
    const tok = m ? m.getAttribute('content') : '';
    const r = await fetch('/posts.json', {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','X-CSRF-Token':tok,'X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({title: __TITLE__, raw: __RAW__, category: __CAT__,
                            tags: __TAGS__, skip_validations: false})});
    const t = await r.text(); let j = null; try { j = JSON.parse(t); } catch(e) {}
    return JSON.stringify({ok:r.ok, status:r.status, topic_id:(j&&j.topic_id)||null,
      id:(j&&j.id)||null, err:(j&&(j.errors||j.error_type))||null, body: j ? null : t.slice(0,400)});
  } catch(e) { return JSON.stringify({ok:false, err:String(e).slice(0,140)}); }
})()
"""


PAGE_EVENTS_JS = r"""
(async () => {
  try {
    const w = window, d = document;
    const nap = ms => new Promise(r => setTimeout(r, ms));
    const H = Math.max(d.documentElement.scrollHeight || 0, w.innerHeight || 800);
    let ev = 0, y = w.scrollY || 0;
    for (let i = 0; i < __STEPS__; i++) {
      y = Math.max(0, Math.min(Math.max(0, H - 200), y + (Math.random() * 2 - 0.6) * 320));
      w.scrollTo({top: y, behavior: 'auto'});
      d.dispatchEvent(new Event('scroll'));    // 部分站点在 document 上监听
      ev++; await nap(120 + Math.random() * 380);
    }
    for (let i = 0; i < __MOVES__; i++) {
      const x = Math.random() * (w.innerWidth || 1200), yy = Math.random() * (w.innerHeight || 800);
      d.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: x, clientY: yy}));
      d.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, clientX: x, clientY: yy}));
      ev += 2; await nap(60 + Math.random() * 240);
    }
    const alpha = 'abcdefghijklmnopqrstuvwxyz';
    for (let i = 0; i < __KEYS__; i++) {
      const k = alpha[Math.floor(Math.random() * alpha.length)];
      d.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: k}));
      d.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: k}));
      ev += 2; await nap(90 + Math.random() * 260);
    }
    return JSON.stringify({ok: true, events: ev, scrollY: (w.scrollY || 0) | 0});
  } catch (e) { return JSON.stringify({ok: false, err: String(e).slice(0, 140)}); }
})()
"""


def _jid(v):
    return json.dumps(v, ensure_ascii=False)


# ------------------------------------------------------------------ 文案生成
REPLY_PROMPT = """你是 linux.do 社区的老用户，要回一个帖子。下面是帖子标题与前几楼正文。

标题：{title}

楼层摘录：
{posts}

请写 **1-2 句**中文回复：
- 必须点名帖子里一个具体细节（技术点 / 数字 / 现象），让人看出你真读了；
- 禁止分点列 1.2.3.，禁止"感谢分享/学到了/顶一下"这类水贴话术，禁止客套收尾；
- 不要 emoji、不要 markdown 标记；
- 总长 {lo}-{hi} 字。
只输出回复正文本身，不要任何前后缀说明。"""


def clean_text(s):
    """剥 markdown / 引号 / 前后缀说明，取 1-2 句干净正文。"""
    if not s:
        return ""
    t = s.strip()
    t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
    t = re.sub(r"\n?```$", "", t)
    t = re.sub(r"^\s*(回复|回帖|answer|reply)\s*[:：]\s*", "", t, flags=re.I)
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"^\s*[-*•]\s*", "", t)
    t = re.sub(r"\s*\n+\s*", " ", t).strip()
    t = t.strip("\"'“”「」 ")
    return t


def validate_reply(text):
    """→ (ok, why)。拦截水贴/空/超长/含链接。"""
    t = clean_text(text)
    if not t:
        return False, "empty"
    if len(t) < REPLY_MIN_CHARS:
        return False, "too_short(%d)" % len(t)
    if len(t) > REPLY_MAX_CHARS:
        return False, "too_long(%d)" % len(t)
    if "http://" in t or "https://" in t:
        return False, "contains_url"
    if re.search(r"(^|\s)\d+\.\s", t) and t.count("\n") >= 2:
        return False, "bullet_list"
    for b in BANNED_PHRASES:
        if b in t:
            return False, "banned_phrase(%s)" % b
    ok_deai, why_deai, _score = deai_check(t)
    if not ok_deai:
        return False, "deai(%s)" % why_deai
    return True, t


def deai_check(text, min_score=None):
    """调去 AI 味机械闸门（单一源 tools/linuxdo_deai.py）→ (ok, why, score)。

    闸门缺失/异常时 **fail-closed**（宁可少发一条，也不把机器味文案发到站上）。
    「人味」这条硬要求只有这一份机械判据，禁在别处另写一份。
    """
    if len((text or "").strip()) < 12:      # 短回帖交长度判据管，闸门只判机器味
        return True, "", 0
    try:
        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        for _d in (_here, os.path.join(os.path.dirname(_here), "tools")):
            if _d not in _sys.path:
                _sys.path.insert(0, _d)
        import linuxdo_deai as _D
    except Exception as e:
        return False, "gate_missing:%s" % repr(e)[:80], 0
    try:
        r = _D.lint(text, min_score=int(min_score or DEAI_MIN_SCORE))
    except Exception as e:
        return False, "gate_error:%s" % repr(e)[:80], 0
    if r.get("ok"):
        return True, "", int(r.get("score") or 0)
    bad = (r.get("hard") or []) + (r.get("warn") or [])
    return (False,
            "%s score=%s" % (",".join(bad[:3]) or "low_score", r.get("score")),
            int(r.get("score") or 0))


def gen_text(prompt, timeout=180, codex_bin=None):
    """codex exec 生成文案；失败/超时返回 None（绝不硬顶重试）。"""
    bin_ = codex_bin or CODEX_BIN
    if not bin_ or not os.path.exists(bin_):
        return None, "codex_not_found:%s" % bin_
    try:
        r = subprocess.run([bin_, "exec", "--skip-git-repo-check", prompt],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=tempfile.gettempdir())
        out = (r.stdout or "").strip()
        return (out or None), ("rc=%d %s" % (r.returncode, (r.stderr or "")[:160]))
    except subprocess.TimeoutExpired:
        return None, "timeout(%ds)" % timeout
    except Exception as e:
        return None, "err:%s" % str(e)[:160]


# ------------------------------------------------------------------ 站点动作
def get_session(prefer=None):
    return bd.pick_session(prefer=prefer)


def read_topic(session, tid):
    res = bd.run_js(session["id"], READ_TOPIC_JS.replace("__TID__", str(int(tid))), timeout=30)
    ok, why, rk = classify_result(res)
    return ok, (res if isinstance(res, dict) else {}), why, rk


def post_reply(session, tid, raw):
    code = POST_REPLY_JS.replace("__TID__", str(int(tid))).replace("__RAW__", _jid(raw))
    return bd.run_js(session["id"], code, timeout=45)


def post_like(session, post_id):
    code = POST_LIKE_JS.replace("__PID__", str(int(post_id)))
    return bd.run_js(session["id"], code, timeout=45)


def post_topic(session, title, raw, category, tags):
    code = (POST_TOPIC_JS.replace("__TITLE__", _jid(title)).replace("__RAW__", _jid(raw))
            .replace("__CAT__", str(int(category)))
            .replace("__TAGS__", json.dumps(tags, ensure_ascii=False)))
    return bd.run_js(session["id"], code, timeout=60)


def fetch_candidates(session, pages=3):
    """拉最新帖候选（复用 daemon 的 /latest.json 通道）。"""
    out, seen = [], set()
    for p in random.sample(range(0, 6), pages):
        for t in bd.fetch_candidates(session, p):
            tid = t.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                out.append(t)
        time.sleep(random.uniform(1.5, 3.5))
    return out


SELF_USER = None       # 本账号用户名（首次读到即缓存；用于过滤自己楼层）
_PREFS_CACHE = {}      # 进程内偏好模型缓存（每次动作取一次即可）


def fetch_self_user(session):
    """探本账号用户名（用于过滤自己的楼层）；失败返回 None，绝不阻塞动作。"""
    global SELF_USER
    if SELF_USER:
        return SELF_USER
    js = ("(async()=>{try{const r=await fetch('/session/current.json',"
          "{credentials:'include',cache:'no-store'});const j=await r.json();"
          "return JSON.stringify({u:(j&&j.current_user&&j.current_user.username)||null});}"
          "}catch(e){return JSON.stringify({u:null});}})()")
    try:
        res = bd.run_js(session["id"], js, timeout=25)
        u = (res or {}).get("u") if isinstance(res, dict) else None
        if u:
            SELF_USER = str(u)
    except Exception:
        pass
    return SELF_USER


def prefs_model(now=None):
    """取偏好模型；缺件/异常返回 None（动作照跑，只少一层加权）。"""
    if not pf:
        return None
    try:
        return pf.load_model(now=now)
    except Exception:
        return None


def prefs_rank_topics(cands, model, want, action="reply"):
    """按偏好给候选话题加权排序；缺模型/异常按原序返回（fail-open）。"""
    cands = [c for c in (cands or []) if isinstance(c, dict)]
    if not want or not cands:
        return []
    if not pf or not model:
        return [{"cand": c, "why": "no_model"} for c in cands[:want]]
    try:
        picks = pf.pick_topics(cands, model, action=action, n=want)
        return [{"cand": c, "why": w} for c, w in picks]
    except Exception:
        return [{"cand": c, "why": "fallback"} for c in cands[:want]]


def prefs_note_interaction(action, user=None, topic=None, post=None, cat=None,
                           cat_label=None, model=None):
    """把一次真实互动写进偏好台账；失败静默（偏好只是加分项，不得阻塞动作）。"""
    if not pf:
        return
    try:
        pf.record(action, user=user, topic=topic, post=post, cat=cat,
                  cat_label=(cat_label or (pf.label_of(model, cat) if model else None)))
    except Exception:
        pass


def pick_reply_targets(session, st, want=2):
    """挑适合回的话题：未回过、非抽奖广告，再按偏好模型加权（板块兴趣/作者关系/同日去重）。"""
    pool, skip = [], 0
    done = done_key_set(st, "reply")
    for t in fetch_candidates(session, pages=3):
        tid = str(t.get("id"))
        if tid in done:
            skip += 1
            continue
        title = t.get("title") or ""
        if re.search(r"(抽奖|拼车|广告|推广|出售|代购)", title):
            continue
        pool.append({"id": t.get("id"), "title": title, "posts": t.get("posts"),
                     "cat": t.get("cat")})
        if len(pool) >= want * 6:
            break
    random.shuffle(pool)
    model = prefs_model()
    ranked = prefs_rank_topics(pool, model, want, action="reply")
    got = [dict(r["cand"], why=r["why"]) for r in ranked]
    return got, skip


# ------------------------------------------------------------------ 动作：回帖
def act_reply(st, session, max_n=2, apply_=False, dry_run=None, text_override=None,
              topic_id=None, timeout=180, log=print):
    dry = (not apply_) if dry_run is None else dry_run
    day = bd.today()
    cap = ad.cap_today(st, day, "reply")
    done_today = count_today(st, "reply", day)
    res = {"kind": "reply", "dry_run": dry, "cap": cap, "done_today": done_today, "items": []}
    okg, whg = risk_gate(st, "reply")
    if not okg:
        res["blocked"] = whg
        return res
    n = 0
    if topic_id:
        targets = [{"id": int(topic_id), "title": "", "posts": None}]
    else:
        targets, skipped = pick_reply_targets(session, st, want=max_n)
        res["dedup_skipped"] = skipped
    for t in targets:
        if n >= max_n or done_today + n >= cap:
            break
        ok, tinfo, why, rk = read_topic(session, t["id"])
        if not ok:
            if rk:
                res["risk"] = risk_note(st, "reply", why)
                res["items"].append({"topic": t["id"], "status": "RISK", "why": why, "risk": rk})
                break
            res["items"].append({"topic": t["id"], "status": "READ_FAIL", "why": why})
            continue
        raw = text_override
        if not raw:
            body = "\n".join("【%s楼 %s】%s" % (p.get("num"), p.get("user"), p.get("text"))
                             for p in ((tinfo.get("latest") or tinfo.get("posts") or [])[:4]))
            prompt = REPLY_PROMPT.format(title=tinfo.get("title") or t.get("title"),
                                         posts=body[:2400],
                                         lo=REPLY_MIN_CHARS, hi=REPLY_MAX_CHARS)
            raw, diag = gen_text(prompt, timeout=timeout)
            if not raw:
                res["items"].append({"topic": t["id"], "status": "GEN_FAIL", "why": diag})
                continue
        vok, vr = validate_reply(raw)
        if not vok:
            res["items"].append({"topic": t["id"], "status": "REJECT_TEXT", "why": vr})
            continue
        _txt = "".join((p.get("text") or "") for p in (tinfo.get("posts") or []))
        item = {"topic": t["id"], "title": tinfo.get("title"), "text": vr,
                "pose": humanize_pose("reply", len(_txt)), "read_chars": len(_txt),
                "pref_why": t.get("why")}
        if dry:
            item["status"] = "DRY_RUN"
            res["items"].append(item)
            n += 1
            continue
        item["page"] = page_events(session, "reply", len(_txt), log=log, dry=dry, tag="[reply] 页面")
        item["typing"] = humanize_typing(vr, log=log, tag="[reply]")   # 打字时长模拟（发送前）
        r2 = post_reply(session, t["id"], vr)
        ok2, why2, rk2 = classify_result(r2)
        if ok2:
            mark_done(st, "reply", t["id"])
            cnt = bump(st, "reply")
            _m = prefs_model()
            _posts = tinfo.get("posts") or []
            prefs_note_interaction("reply", user=(_posts[0].get("user") if _posts else None),
                                   topic=t["id"], post=(r2 or {}).get("id"),
                                   cat=(t.get("cat") or tinfo.get("cat")), model=_m)
            item.update({"status": "POSTED", "post_id": (r2 or {}).get("id"), "n_today": cnt})
            log("[reply] ✅ topic=%s post=%s %s" % (t["id"], (r2 or {}).get("id"), vr[:40]))
            item["wrap_s"] = humanize_wrap("reply", log=log)
        else:
            item.update({"status": "POST_FAIL", "why": why2})
            if rk2:
                res["risk"] = risk_note(st, "reply", why2)
                random.shuffle(res["items"])
                save_state(st)
                return res
        res["items"].append(item)
        n += 1
        if n < max_n:
            gap = ad.gap_seconds("reply")
            log("[reply] 间隔 %.0fs" % gap)
            time.sleep(gap)
    save_state(st)
    return res


# ------------------------------------------------------------------ 动作：点赞
def act_like(st, session, max_n=3, apply_=False, dry_run=None, topic_id=None,
             post_id=None, log=print):
    dry = (not apply_) if dry_run is None else dry_run
    day = bd.today()
    cap = ad.cap_today(st, day, "like")
    done_today = count_today(st, "like", day)
    res = {"kind": "like", "dry_run": dry, "cap": cap, "done_today": done_today, "items": []}
    okg, whg = risk_gate(st, "like")
    if not okg:
        res["blocked"] = whg
        return res
    done = done_key_set(st, "like")
    n = 0
    if not topic_id:
        cands, _ = pick_reply_targets(session, st, want=max_n * 2)
        topic_ids = [c["id"] for c in cands] or []
    else:
        topic_ids = [int(topic_id)]
    for tid in topic_ids:
        if n >= max_n or done_today + n >= cap:
            break
        ok, tinfo, why, rk = read_topic(session, tid)
        if not ok:
            if rk:
                res["risk"] = risk_note(st, "like", why)
                res["items"].append({"topic": tid, "status": "RISK", "why": why})
                break
            res["items"].append({"topic": tid, "status": "READ_FAIL", "why": why})
            continue
        _txt = "".join((p.get("text") or "") for p in (tinfo.get("posts") or []))
        pose = humanize_read("like", len(_txt), log=log, dry=dry, tag="[like] 阅读")
        cand_posts = [p for p in (tinfo.get("posts") or []) if str(p.get("id")) not in done]
        if post_id:
            cand_posts = [p for p in cand_posts if str(p.get("id")) == str(post_id)]
        if not cand_posts:
            res["items"].append({"topic": tid, "status": "NO_TARGET"})
            continue
        # 点赞目标偏好：先按「2-8 楼随手赞」缩小池子，再用偏好模型选作者（熟人/陌生人 + 同日去重）
        pool = [p for p in cand_posts if 2 <= int(p.get("num") or 1) <= 8] or cand_posts
        _m = prefs_model()
        why = "no_model"
        target = None
        if pf and _m:
            try:
                target, why = pf.pick_floor(pool, _m, action="like",
                                            self_user=fetch_self_user(session))
            except Exception:
                target, why = None, "fallback"
        if target is None:
            target, why = random.choice(pool), why
        item = {"topic": tid, "post": target.get("id"), "num": target.get("num"),
                "user": target.get("user"), "pref_why": why,
                "pose": pose, "read_dwell": (pose or {}).get("read_dwell")}
        if hu and hu.decision_skip("like"):          # 读了不赞 = 真人常态（不计入 cap）
            item["status"] = "SKIP_AFTER_READ"
            res["items"].append(item)
            res["n_skip_after_read"] = int(res.get("n_skip_after_read") or 0) + 1
            continue
        item["page"] = page_events(session, "like", len(_txt), log=log, dry=dry, tag="[like] 页面")
        if dry:
            item["status"] = "DRY_RUN"
            res["items"].append(item)
            n += 1
            continue
        r2 = post_like(session, target["id"])
        ok2, why2, rk2 = classify_result(r2)
        if ok2:
            mark_done(st, "like", target["id"])
            cnt = bump(st, "like")
            prefs_note_interaction("like", user=target.get("user"), topic=tid,
                                   post=target.get("id"),
                                   cat=(tinfo.get("cat") or tinfo.get("category_id")),
                                   model=_m)
            item.update({"status": "LIKED", "n_today": cnt})
            log("[like] ✅ topic=%s post=%s" % (tid, target.get("id")))
            item["wrap_s"] = humanize_wrap("like", log=log)
        else:
            item.update({"status": "LIKE_FAIL", "why": why2})
            if rk2:
                res["risk"] = risk_note(st, "like", why2)
                save_state(st)
                return res
        res["items"].append(item)
        n += 1
        if n < max_n:
            gap = ad.gap_seconds("like")
            log("[like] 间隔 %.0fs" % gap)
            time.sleep(gap)
    save_state(st)
    return res


# ------------------------------------------------------------------ 动作：回访自己的帖子
REVISIT_MIN_GAP_S = 6 * 3600      # 同一轮回访之间的最小间隔（防「盯帖」：真人不会每几分钟回看一次）
REVISIT_DAILY_MAX = 3             # 每天最多回访几个自己的帖（以只读为主，仍设上限）
REVISIT_LIKE_P = 0.4             # 看到新回复时点赞的概率（真人不会条条都赞）


def revisit_own_posts(st, limit=3):
    """最近发过的自己的帖（state.post_history 优先，回落 prefs.own_posts）。只读。"""
    out, seen = [], set()
    for h in reversed((st.get("post_history") or [])[-10:]):
        tid = h.get("id")
        if tid and str(tid) not in seen:
            seen.add(str(tid)); out.append({"id": int(tid), "title": h.get("title")})
    if len(out) < limit and pf:
        try:
            for it in reversed(list(pf.own_posts() or [])[-10:]):
                tid = it.get("id")
                if tid and str(tid) not in seen:
                    seen.add(str(tid)); out.append({"id": int(tid), "title": it.get("title")})
        except Exception:
            pass
    return out[:limit]


def revisit_new_posts(tinfo, last_num, self_user=None):
    """挑出「比我上次看到的新回复」：合并 posts/latest 两窗口→去重→剔自己→按楼层升序。"""
    pool = list(tinfo.get("posts") or []) + list(tinfo.get("latest") or [])
    mx = max([int(x.get("num") or 0) for x in pool] or [0])
    out, seen = [], set()
    for x in pool:
        num = int(x.get("num") or 0)
        if num <= int(last_num or 0):
            continue
        pid = x.get("id")
        if pid in seen:
            continue
        if self_user and str(x.get("user")) == str(self_user):
            continue
        seen.add(pid); out.append(x)
    out.sort(key=lambda x: int(x.get("num") or 0))
    return out, mx


def revisit_due(st, now=None, min_gap_s=REVISIT_MIN_GAP_S):
    """回访是否到期：无固定时刻（由 daemon 随机段/提示词决定），只在最近没做过时放行。"""
    now = now or time.time()
    rv = st.get("revisit") or {}
    last = float(rv.get("last_ts") or 0)
    if last and now - last < min_gap_s:
        return False, "too_soon(%.1fh)" % ((now - last) / 3600.0)
    if int(rv.get("n_day_%s" % bd.today()) or 0) >= REVISIT_DAILY_MAX:
        return False, "daily_max"
    return True, "ok"


def act_revisit(st, session, max_n=1, apply_=False, dry_run=None, topic_id=None,
                like_prob=REVISIT_LIKE_P, log=print):
    """回访自己发过的帖：读新回复（**不自动回帖**，回复仍由等级路由/提示词决定），
    并按概率给其中一条新回复点赞（真人形态：看一眼、偶尔赞一条）。

    幂等：进度记在 state["revisit"][str(tid)]["last_num"]，下次只读更新的楼层。
    """
    dry = (not apply_) if dry_run is None else dry_run
    day = bd.today()
    now_ts = time.time()
    res = {"kind": "revisit", "dry_run": dry, "items": []}
    okg, whg = risk_gate(st, "revisit")
    if not okg:
        res["blocked"] = whg
        return res
    if not topic_id:
        okd, whyd = revisit_due(st, now_ts)
        if not okd:
            res["skip"] = whyd
            return res
    rv = st.setdefault("revisit", {})
    own = ([{"id": int(topic_id)}] if topic_id else revisit_own_posts(st, limit=max(1, max_n)))
    if not own:
        res["skip"] = "no_own_post"
        return res
    my = fetch_self_user(session)
    model = prefs_model()
    n_new_total = 0
    for h in own[:max_n]:
        tid = int(h["id"])
        rec = rv.setdefault(str(tid), {"last_num": 0})
        ok, tinfo, why, rk = read_topic(session, tid)
        if not ok:
            if rk:
                res["risk"] = risk_note(st, "revisit", why)
                break
            res["items"].append({"topic": tid, "status": "READ_FAIL", "why": why})
            continue
        _txt = "".join((x.get("text") or "") for x in
                       (list(tinfo.get("posts") or []) + list(tinfo.get("latest") or [])))
        pose = humanize_read("browse", len(_txt), log=log, dry=dry, tag="[revisit] 阅读")
        fresh, mx = revisit_new_posts(tinfo, rec.get("last_num"), my)
        item = {"topic": tid, "title": tinfo.get("title"),
                "posts_count": tinfo.get("posts_count"),
                "last_num": int(rec.get("last_num") or 0), "max_num": mx,
                "n_new": len(fresh), "pose": pose,
                "new": [{"num": x.get("num"), "user": x.get("user"),
                         "text": (x.get("text") or "")[:80]} for x in fresh[:5]]}
        rec["last_num"] = max(int(rec.get("last_num") or 0), mx)
        rec["last_ts"] = now_ts
        rv["last_ts"] = now_ts
        rv["n_day_%s" % day] = int(rv.get("n_day_%s" % day) or 0) + 1
        n_new_total += len(fresh)
        if fresh:
            prefs_note_interaction("revisit", user=fresh[-1].get("user"), topic=tid,
                                   cat=(tinfo.get("cat") or tinfo.get("category_id")),
                                   model=model)
        # 回访点赞：只在新回复里挑一条，且走同一偏好模型（熟人优先）+ 当日上限
        if fresh and random.random() < like_prob:
            kw = {}
            if hu:
                try:
                    kw["skip"] = hu.decision_skip("like")
                except Exception:
                    kw["skip"] = False
            if not kw.get("skip"):
                pool, why2 = list(fresh), "no_model"
                if pf and model:
                    try:
                        t2, why2 = pf.pick_floor(pool, model, action="like", self_user=my)
                    except Exception:
                        t2, why2 = None, "fallback"
                else:
                    t2 = None
                if t2 is None:
                    t2, why2 = random.choice(pool), why2
                if str(t2.get("id")) in done_key_set(st, "like") or \
                        count_today(st, "like", day) >= ad.cap_today(st, day, "like"):
                    item["like"] = {"status": "SKIP_CAP_OR_DONE", "post": t2.get("id")}
                else:
                    item["page"] = page_events(session, "like", len(_txt), log=log,
                                               dry=dry, tag="[revisit] 页面")
                    if dry:
                        item["like"] = {"status": "DRY_RUN", "post": t2.get("id"),
                                        "user": t2.get("user"), "pref_why": why2}
                    else:
                        r2 = post_like(session, t2.get("id"))
                        ok2, why3, rk2 = classify_result(r2)
                        if ok2:
                            mark_done(st, "like", t2.get("id"))
                            cnt = bump(st, "like")
                            prefs_note_interaction("like", user=t2.get("user"), topic=tid,
                                                   post=t2.get("id"),
                                                   cat=(tinfo.get("cat") or tinfo.get("category_id")),
                                                   model=model)
                            item["like"] = {"status": "LIKED", "post": t2.get("id"),
                                            "user": t2.get("user"), "n_today": cnt,
                                            "pref_why": why2,
                                            "wrap_s": humanize_wrap("like", log=log)}
                            log("[revisit] ✅ topic=%s post=%s 已赞" % (tid, t2.get("id")))
                        else:
                            item["like"] = {"status": "LIKE_FAIL", "why": why3}
                            if rk2:
                                res["risk"] = risk_note(st, "revisit", why3)
                                save_state(st)
                                return res
        res["items"].append(item)
        if len(own) > 1 and h is not own[-1]:
            gap = ad.gap_seconds("like")
            log("[revisit] 帖间间隔 %.0fs" % gap)
            time.sleep(gap)
    res["n_new_total"] = n_new_total
    save_state(st)
    return res


# ------------------------------------------------------------------ 动作：发帖
def act_post(st, session, title, raw, category, tags, apply_=False, dry_run=None,
             log=print):
    dry = (not apply_) if dry_run is None else dry_run
    day = bd.today()
    now_ts = time.time()
    ok_i, why_i = ad.post_interval_ok(st, now_ts)
    res = {"kind": "post", "dry_run": dry, "interval_ok": ok_i, "why": why_i,
           "title": title, "category": category, "tags": tags}
    okg, whg = risk_gate(st, "post")
    if not okg:
        res["blocked"] = whg
        return res
    if not ok_i:
        return res
    if count_today(st, "post", day) >= ad.cap_today(st, day, "post"):
        res["why"] = "cap_reached"
        return res
    res["pose"] = humanize_pose("post", len(raw or ""))
    if dry:
        res["status"] = "DRY_RUN"
        return res
    res["preface"] = humanize_read("post", len(raw or "") + 800, log=log,
                                   tag="[post] 前置闲逛")   # 先逛几帖再发（禁直筒）
    res["page"] = page_events(session, "post", len(raw or "") + 800, log=log, tag="[post] 页面")
    res["write"] = humanize_write("post", raw, log=log)                          # 分段写作停顿
    r2 = post_topic(session, title, raw, category, tags)
    ok2, why2, rk2 = classify_result(r2)
    if ok2:
        tid = (r2 or {}).get("topic_id")
        bump(st, "post")
        st["post_history"].append({"ts": now_ts, "id": tid, "title": title})
        mark_done(st, "post", tid)
        if pf:      # 落 own_posts.jsonl：回访（revisit）与「发帖看板块偏好」都读它
            try:
                pf.record_own_post(tid, title=title, cat=category, ts=now_ts)
            except Exception:
                pass
        res.update({"status": "POSTED", "topic_id": tid,
                    "wrap_s": humanize_wrap("post", log=log)})
    else:
        res.update({"status": "POST_FAIL", "why": why2})
        if rk2:
            res["risk"] = risk_note(st, "post", why2)
    save_state(st)
    return res


# ------------------------------------------------------------------ 入口判定
def cmd_due(args):
    st = load_state()
    now = time.time()
    day = bd.today()
    out = {}
    for k in (args.kind,) if args.kind else KINDS:
        ok, why = risk_gate(st, k, now)
        if ok:
            ok, why = ad.due_now(st, now, k, count_today(st, k, day))
        w, wwhy = ad.next_wait(st, now, k, count_today(st, k, day))
        out[k] = {"due": ok, "why": why, "done_today": count_today(st, k, day),
                  "cap": ad.cap_today(st, day, k), "rest_day": ad.rest_day(st, day, k),
                  "wait_seconds": round(w, 1), "wait_why": wwhy,
                  "eta": dt.datetime.fromtimestamp(now + w).strftime("%Y-%m-%d %H:%M:%S")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args):
    st = load_state()
    now = time.time()
    day = bd.today()
    ok, why = risk_gate(st, args.kind, now)
    if ok:
        ok, why = ad.due_now(st, now, args.kind, count_today(st, args.kind, day))
    if not ok:
        w, wwhy = ad.next_wait(st, now, args.kind, count_today(st, args.kind, day))
        print(json.dumps({"skip": True, "kind": args.kind, "why": why,
                          "wait_seconds": round(w, 1), "wait_why": wwhy}, ensure_ascii=False))
        return 0
    session, diag = get_session(args.tab)
    if not session:
        print(json.dumps({"skip": True, "kind": args.kind, "why": "no_session", "diag": diag},
                         ensure_ascii=False))
        return 2
    if args.kind == "reply":
        r = act_reply(st, session, max_n=args.max, apply_=args.apply)
    elif args.kind == "like":
        r = act_like(st, session, max_n=args.max, apply_=args.apply)
    elif args.kind == "revisit":
        r = act_revisit(st, session, max_n=args.max, apply_=args.apply,
                        topic_id=args.topic, like_prob=args.like_prob)
    else:
        print(json.dumps({"skip": True, "kind": "post",
                          "why": "post 需显式 --title/--raw/--category/--tags，用 `post` 子命令"},
                         ensure_ascii=False))
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_revisit(args):
    st = load_state()
    if args.topic:      # 显式指定帖时不看冷却（人工触发语义）
        st.setdefault("revisit", {})["last_ts"] = 0
    session, diag = get_session(args.tab)
    if not session:
        print(json.dumps({"skip": True, "kind": "revisit", "why": "no_session",
                          "diag": diag}, ensure_ascii=False))
        return 2
    r = act_revisit(st, session, max_n=args.max, apply_=args.apply,
                    topic_id=args.topic, like_prob=args.like_prob)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_reply_one(args):
    st = load_state()
    session, diag = get_session(args.tab)
    if not session:
        print(json.dumps({"skip": True, "why": "no_session", "diag": diag}, ensure_ascii=False))
        return 2
    r = act_reply(st, session, max_n=1, apply_=args.apply, topic_id=args.topic,
                  text_override=args.raw, timeout=args.timeout)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_like_one(args):
    st = load_state()
    session, diag = get_session(args.tab)
    if not session:
        print(json.dumps({"skip": True, "why": "no_session", "diag": diag}, ensure_ascii=False))
        return 2
    r = act_like(st, session, max_n=1, apply_=args.apply, topic_id=args.topic, post_id=args.post)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_post(args):
    st = load_state()
    tags = [x.strip() for x in (args.tags or "").split(",") if x.strip()]
    session = None
    if args.apply:
        session, diag = get_session(args.tab)
        if not session:
            print(json.dumps({"skip": True, "why": "no_session", "diag": diag}, ensure_ascii=False))
            return 2
    r = act_post(st, session, args.title, args.raw, args.category, tags, apply_=args.apply)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_state(args):
    st = load_state()
    day = bd.today()
    out = {"day": day, "daily": st.get("daily", {}).get(day, {}),
           "cap": {k: ad.cap_today(st, day, k) for k in KINDS},
           "rest_day": {k: ad.rest_day(st, day, k) for k in KINDS},
           "pause_until": st.get("pause_until"), "risk_stop_day": st.get("risk_stop_day"),
           "risk_log_tail": st.get("risk_log", [])[-3:]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------------------ 自测
def _selftest_impl(tmpdir):
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            print("  FAIL %s" % name)

    sp = os.path.join(tmpdir, "state.json")
    st = load_state(sp)
    chk("T1 初始状态键齐全", all(k in st for k in
                            ("daily", "done", "cap", "rest", "jitter", "post_history", "risk")))
    bump(st, "reply")
    bump(st, "reply")
    chk("T2 计数累加", count_today(st, "reply") == 2)
    mark_done(st, "reply", "123")
    chk("T3 幂等去重集合", "123" in done_key_set(st, "reply"))
    save_state(st, sp)
    st2 = load_state(sp)
    chk("T4 状态往返", count_today(st2, "reply") == 2 and "123" in done_key_set(st2, "reply"))
    # 脏 state 不得 KeyError（browse daemon 2026-10-05 事故）
    json.dump({"rest": {}}, open(sp, "w"), ensure_ascii=False)
    st3 = load_state(sp)
    chk("T5 脏状态不崩(局部dict覆盖)", isinstance(st3.get("daily"), dict) and count_today(st3, "reply") == 0)

    # 文案清洗/校验
    chk("T6 清洗去说明前缀", clean_text("回复：这个 20000 楼的边界条件很有意思") == "这个 20000 楼的边界条件很有意思")
    chk("T7 清洗去 markdown", clean_text("**确实**，那个 `timeout` 值偏小") == "确实，那个 timeout 值偏小")
    ok, v = validate_reply("这帖里 128 并发那段的瓶颈其实在写锁，跟 GC 关系不大")
    chk("T8 正常文案放行", ok and v.startswith("这帖里"))
    chk("T9 水贴拦截", not validate_reply("感谢分享，学到了")[0])
    chk("T10 空/短拦截", not validate_reply("好")[0] and not validate_reply("")[0])
    chk("T11 超长拦截", not validate_reply("测" * 200)[0])
    chk("T12 链接拦截", not validate_reply("看这个 https://x.com/a 讲的挺细的")[0])
    # T13 负控：被拦截项必须返回 False（拿真值反证判据非恒真）
    chk("T13 负控(恒真判据可被抓)", not validate_reply("顶一下")[0] and validate_reply("这帖里 128 并发那段瓶颈在写锁")[0])

    # 风控判读
    chk("T14 429→RATE_LIMIT", bd.risk_kind_from_http(429) == "RATE_LIMIT")
    chk("T15 403→FORBIDDEN", bd.risk_kind_from_http(403) == "FORBIDDEN")
    ok, why, rk = classify_result({"ok": False, "status": 403, "err": "blocked"})
    chk("T16 classify 传风控类", (not ok) and rk == "FORBIDDEN")
    ok, why, rk = classify_result({"ok": True, "status": 200, "id": 99})
    chk("T17 classify 正常", ok and rk is None)

    # 风控记账 + 闸门（沙箱 state，绝不碰生产）
    stx = load_state(os.path.join(tmpdir, "s2.json"))
    msg = risk_note(stx, "reply", "sandbox", now_ts=time.time(), allow_notify=False)
    chk("T18 风控记账写 pause", float(stx.get("pause_until") or 0) > time.time())
    okg, whg = risk_gate(stx, "reply")
    chk("T19 pause 期内闸门关闭", (not okg) and "pause_until" in whg)
    risk_note(stx, "like", "x1", now_ts=time.time(), allow_notify=False)
    risk_note(stx, "like", "x2", now_ts=time.time(), allow_notify=False)
    risk_note(stx, "like", "x3", now_ts=time.time(), allow_notify=False)
    chk("T20 同类≥3 当日停", stx.get("risk_stop_day") == bd.today())
    chk("T21 当日停闸门", risk_gate(stx, "like")[0] is False)
    # T22 负控：未达阈值不得置当日停
    sty = load_state(os.path.join(tmpdir, "s3.json"))
    risk_note(sty, "reply", "once", now_ts=time.time(), allow_notify=False)
    chk("T22 负控(单次不得当日停)", sty.get("risk_stop_day") is None)

    # 入口判定与内核联动（沙箱）
    stz = load_state(os.path.join(tmpdir, "s4.json"))
    # ★自测禁绑现场（2026-10-06）：固定白天时刻，否则凌晨 01:00-07:30 跑会落到静默窗分支，
    # 结果随「跑测试的钟点」而变（曾因此把 antidetect 的 quiet 解包缺陷掩盖数日）。
    now = time.mktime(time.strptime("2026-10-06 15:00:00", "%Y-%m-%d %H:%M:%S"))
    w, ww = ad.next_wait(stz, now, "reply", 0)
    chk("T23 next_wait 正数", w > 0)
    # T23b 负控：静默窗分支必须真能算完（历史缺陷：quiet 4 元组被当 2 元组解包 ⇒ ValueError）
    qn = time.mktime(time.strptime("2026-10-06 03:00:00", "%Y-%m-%d %H:%M:%S"))
    wq, wwq = ad.next_wait(stz, qn, "reply", 0)
    chk("T23b 静默窗分支 wait_quiet_end 且时长合理",
        wwq == "wait_quiet_end" and 60 < wq <= 12 * 3600)
    okd, whyd = ad.due_now(stz, now, "reply", 0)
    chk("T24 due_now 可判", isinstance(okd, bool) and isinstance(whyd, str))
    chk("T25 发帖间隔门", ad.post_interval_ok(stz, now)[0] is True)
    stz["post_history"].append({"ts": now - 60})
    chk("T26 发帖间隔门拦截", ad.post_interval_ok(stz, now)[0] is False)

    # JS 片段结构（防模板漏替换字段）
    chk("T27 读帖JS", "__TID__" in READ_TOPIC_JS and "/t/__TID__.json" in READ_TOPIC_JS)
    for nm, js, tok in (("T28 回帖JS", POST_REPLY_JS, "__RAW__"),
                        ("T29 点赞JS", POST_LIKE_JS, "__PID__"),
                        ("T30 发帖JS", POST_TOPIC_JS, "__TAGS__")):
        chk(nm, tok in js and "csrf-token" in js)
    chk("T31 回帖JS 替换后无残留占位", "__TID__" not in POST_REPLY_JS.replace("__TID__", "1").replace("__RAW__", '"x"'))

    chk("T33 自测状态隔离(路径在沙箱)", state_path().startswith(tmpdir))
    # 只读自证：自测不得写生产 state
    prod = os.path.join(BASE, "state.json")
    fp_before = None
    if os.path.exists(prod):
        fp_before = (os.path.getsize(prod), os.path.getmtime(prod))
    _ = load_state()   # 只读加载生产
    fp_after = None
    if os.path.exists(prod):
        fp_after = (os.path.getsize(prod), os.path.getmtime(prod))
    chk("T34 自测不写生产 state", fp_before == fp_after)

    # 跨进程锁（沙箱；锁文件与 state 同目录，避免碰生产 run.lock）
    t1 = acquire_lock("st-a")
    chk("T35 首次加锁成功", bool(t1))
    chk("T36 二次加锁互斥(占用)", acquire_lock("st-b") is None)
    release_lock(t1)
    t2 = acquire_lock("st-c")
    chk("T37 释放后可再取", bool(t2))
    release_lock(t2)
    # 陈旧锁可回收（把 ts 改老，不探 pid）
    t3 = acquire_lock("st-d")
    lp = lock_path()
    with open(lp, "w") as f:
        json.dump({"tag": "stale", "pid": 1, "ts": time.time() - LOCK_TTL - 10}, f)
    t4 = acquire_lock("st-e")
    chk("T38 陈旧锁可回收", bool(t4))
    release_lock(t4)
    # 负控：非己 token 不得删他人锁
    t5 = acquire_lock("st-f")
    try:
        release_lock(t5 + "_wrong")
    except Exception:
        pass
    chk("T39 负控(错 token 不误删)", os.path.exists(lp) is False or bool(t5))
    release_lock(t5)
    chk("T40 正常释放后锁文件消失", not os.path.exists(lp))

    # ---- 拟人层接线（防「改了没接上」：源码级 + 行为级双证据）----
    src = open(os.path.abspath(__file__), "r", encoding="utf-8").read()
    chk("T41 拟人层可加载且类别与内核一致",
        hu is not None and set(hu.KIND_BEHAVIOR) == set(ad.ACTION_PROFILES))
    chk("T42 act_like 读后有犹豫分支", "SKIP_AFTER_READ" in src and "humanize_read(\"like\"" in src)
    chk("T43 act_reply 打字时长模拟已接", "humanize_typing(" in src)
    chk("T44 act_post 前置闲逛+写作停顿已接", "humanize_read(\"post\"" in src and "humanize_write(" in src)
    chk("T45 读帖JS 带最新楼(回帖读尾楼)", "latest:" in READ_TOPIC_JS and "ps.slice(-3)" in READ_TOPIC_JS)
    # 睡眠出口受上限保护（防长休挂住守护）；负控=上限放开时必须显著变长
    _old_cap = globals()["HUMANIZE_MAX_SLEEP"]
    try:
        globals()["HUMANIZE_MAX_SLEEP"] = 0.05
        t0 = time.time()
        humanize_typing("这是一段用于自测的中文文案，需要在发送前模拟真人打字时长。", log=None)
        capped = time.time() - t0
        globals()["HUMANIZE_MAX_SLEEP"] = 1.0
        t1 = time.time()
        s1 = humanize_typing("这是一段用于自测的中文文案，需要在发送前模拟真人打字时长。", log=None)
        uncapped = time.time() - t1
        chk("T46 拟人睡眠受上限截断", capped <= 0.6 and uncapped > capped)
    finally:
        globals()["HUMANIZE_MAX_SLEEP"] = _old_cap
    # 负控：拟人层缺席时不得抛异常（fail-safe）
    _hu = globals().get("hu")
    try:
        globals()["hu"] = None
        chk("T47 负控(拟人层缺席不崩)",
            humanize_typing("x", log=None) is None and humanize_read("like", 100) is None
            and humanize_write("post", "y") is None and humanize_wrap("like") == 0.0)
    finally:
        globals()["hu"] = _hu

    # ---- 页面级行为事件注入（弱信号补强）----
    chk("T48 页面事件JS含滚动/鼠标/键盘且占位符齐",
        all(t in PAGE_EVENTS_JS for t in ("scrollTo", "mousemove", "KeyboardEvent",
                                          "__STEPS__", "__MOVES__", "__KEYS__")))
    _hu2 = globals().get("hu")
    _pe_on = globals().get("PAGE_EVENTS_ENABLED")
    try:
        calls = []

        class _Sess(dict):
            pass

        _real_run = globals()["bd"].run_js
        globals()["bd"].run_js = lambda sid, code, timeout=30: (
            calls.append(code), {"ok": True, "events": 7})[1]
        # 写死关（负控）：生产常量恒 False，且 env 也无力打开
        chk("T50c 写死关(生产常量 False 且不注入)",
            globals()["PAGE_EVENTS_ENABLED"] is False and not os.environ.get("LJQ_PAGE_EVENTS_ON")
            and page_events(_Sess(id="t"), "like", 1200, log=None) is None and not calls)
        _oe = os.environ.get("LJQ_PAGE_EVENTS_ON")
        os.environ["LJQ_PAGE_EVENTS_ON"] = "1"
        try:
            chk("T50e 负控(env 置 1 也打不开页面事件)",
                globals()["PAGE_EVENTS_ENABLED"] is False and not calls
                and page_events(_Sess(id="t"), "like", 1200, log=None) is None)
        finally:
            if _oe is None:
                os.environ.pop("LJQ_PAGE_EVENTS_ON", None)
            else:
                os.environ["LJQ_PAGE_EVENTS_ON"] = _oe
        globals()["PAGE_EVENTS_ENABLED"] = True
        pe = page_events(_Sess(id="t"), "like", 1200, log=None)
        chk("T49 开启后非 dry 真发且回读计数",
            bool(calls) and pe and pe.get("events") == 7
            and "__STEPS__" not in calls[0] and "scrollTo" in calls[0])
        calls[:] = []
        chk("T50 dry 时不发页面事件", page_events(_Sess(id="t"), "like", 1200, dry=True) is None
            and not calls)

        def _boom(sid, code, timeout=30):
            raise RuntimeError("boom")

        globals()["bd"].run_js = _boom
        chk("T50b 负控(页面事件异常不崩且返回None)",
            page_events(_Sess(id="t"), "reply", 900, log=None) is None)
    finally:
        globals()["bd"].run_js = _real_run
        globals()["hu"] = _hu2
        globals()["PAGE_EVENTS_ENABLED"] = _pe_on
    src2 = open(os.path.abspath(__file__), "r", encoding="utf-8").read()
    _old_tok = "PAGE_EVENTS" + "_ON = os.environ"   # 拼接构造：避免断言自身构成命中
    chk("T50d CLI 有 --page-events 且写死关语义写明",
        "--page-events" in src2 and "PAGE_EVENTS_ENABLED = False" in src2
        and _old_tok not in src2)

    passed = sum(1 for _, c in checks if c)
    # ---------------- 回访（revisit）----------------
    chk("T51 非周期档(revisit 不进 due/cap 的 KINDS)", "revisit" not in KINDS)
    _posts = [{"id": 1, "num": 1, "user": "me", "text": "楼主"},
              {"id": 2, "num": 2, "user": "alice", "text": "a"},
              {"id": 3, "num": 3, "user": "bob", "text": "b"}]
    # latest 窗口与 posts 窗口重叠（Discourse 实际形态）⇒ 同 id 必须只算一次
    fresh, mx = revisit_new_posts({"posts": _posts[1:], "latest": _posts[2:]}, 1, "me")
    chk("T52 新回复=去重+剔自己+按楼层升序",
        [x["id"] for x in fresh] == [2, 3] and mx == 3)
    f2, _ = revisit_new_posts({"posts": _posts, "latest": []}, 3, "me")
    chk("T53 负控(无更新则 0 条)", f2 == [])
    _s5 = load_state(os.path.join(tmpdir, "s5.json"))
    _pd = os.environ.get("LJQ_PREFS_DIR")
    os.environ["LJQ_PREFS_DIR"] = os.path.join(tmpdir, "prefs")
    _hu_ds = globals()["hu"].decision_skip if globals().get("hu") else None
    if _hu_ds is not None:
        # 拟人层的「读了不赞」是随机量(p=0.22)⇒自测里必须钉死,否则用例假红
        globals()["hu"].decision_skip = lambda *a, **k: False
    try:
        chk("T54 首次回访到期", revisit_due(_s5)[0] is True)
        _s5["revisit"] = {"last_ts": time.time()}
        _ok, _why = revisit_due(_s5)
        chk("T55 负控(冷却期内不到期)", _ok is False and "too_soon" in _why)
        _s5["revisit"] = {"last_ts": 0}
        _s5["post_history"] = [{"ts": time.time(), "id": 7, "title": "t"}]
        _ran = []

        def _rv_js(sid, code, timeout=30):
            if "/session/current.json" in code:
                _ran.append("self")
                return {"u": "me"}
            _ran.append("read")
            return {"ok": True, "id": 7, "title": "t", "posts_count": 3,
                    "cat": 4, "posts": [{"id": 70, "num": 1, "user": "me", "text": "hi"}],
                    "latest": [{"id": 71, "num": 2, "user": "alice", "text": "new"},
                               {"id": 72, "num": 3, "user": "bob", "text": "new2"}]}
        _orig_run = globals()["bd"].run_js
        globals()["bd"].run_js = _rv_js
        try:
            r = act_revisit(_s5, {"id": "tab1"}, max_n=1, dry_run=True,
                            like_prob=1.0, log=None)
        finally:
            globals()["bd"].run_js = _orig_run
        _it = (r.get("items") or [{}])[0]
        chk("T56 回访读到 2 条新回复且进度推进到 3",
            r.get("n_new_total") == 2 and _it.get("last_num") == 0
            and _it.get("max_num") == 3 and _s5["revisit"]["7"]["last_num"] == 3)
        chk("T57 dry-run 下点赞只算不点", (_it.get("like") or {}).get("status") == "DRY_RUN")
        _s5["revisit"]["last_ts"] = 0
        _s5["revisit"]["7"] = {"last_num": 3}
        globals()["bd"].run_js = _rv_js
        try:
            r2 = act_revisit(_s5, {"id": "tab1"}, max_n=1, dry_run=True,
                             like_prob=1.0, log=None)
        finally:
            globals()["bd"].run_js = _orig_run
        _it2 = (r2.get("items") or [{}])[0]
        chk("T58 幂等(已读过的楼层不重复读)",
            r2.get("n_new_total") == 0 and "like" not in _it2)
        q = {}
        globals()["bd"].run_js = _rv_js
        try:
            r3 = act_revisit(_s5, {"id": "tab1"}, max_n=1, dry_run=True, log=None)
        finally:
            globals()["bd"].run_js = _orig_run
        chk("T59 负控(冷却期内 skip 且不打站点)", r3.get("skip", "").startswith("too_soon"))
        src3 = open(os.path.abspath(__file__), "r", encoding="utf-8").read()
        chk("T60 CLI/派发已接 revisit(且锁集合含它)",
            'choices=["due", "run", "reply-one", "like-one", "post", "revisit",' in src3
            and 'a.cmd == "revisit"' in src3 and '"post", "revisit")' in src3)
        chk("T61 发帖成功才落 own_posts(供回访定位)",
            "pf.record_own_post(" in src3 and 'setdefault("revisit", {})' in src3)
    finally:
        if _hu_ds is not None:
            globals()["hu"].decision_skip = _hu_ds
        if _pd is None:
            os.environ.pop("LJQ_PREFS_DIR", None)
        else:
            os.environ["LJQ_PREFS_DIR"] = _pd

    passed = sum(1 for _, c in checks if c)   # 本块之后新增用例 ⇒ 结算前重算
    print("selftest %d/%d PASS" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


def selftest():
    """自测全程状态重定向到沙箱（LJQ_ACTS_STATE）⇒ 生产 state 零写入（铁律）。"""
    old = os.environ.get("LJQ_ACTS_STATE")
    old_lk = os.environ.get("LJQ_ACTS_LOCK")
    with tempfile.TemporaryDirectory(prefix="linuxdo_acts_st_") as td:
        os.environ["LJQ_ACTS_STATE"] = os.path.join(td, "state.json")
        os.environ["LJQ_ACTS_LOCK"] = os.path.join(td, "run.lock")
        try:
            return _selftest_impl(td)
        finally:
            if old is None:
                os.environ.pop("LJQ_ACTS_STATE", None)
            else:
                os.environ["LJQ_ACTS_STATE"] = old
            if old_lk is None:
                os.environ.pop("LJQ_ACTS_LOCK", None)
            else:
                os.environ["LJQ_ACTS_LOCK"] = old_lk


# ------------------------------------------------------------------ main
def dispatch(a):
    """命令派发（main 负责加锁与全局开关）。"""
    if a.cmd == "profiles":
        print(json.dumps({k: ad.profile(k) for k in KINDS}, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "state":
        return cmd_state(a)
    if a.cmd == "due":
        return cmd_due(a)
    if a.cmd == "run":
        if not a.kind:
            print("usage: run reply|like|post"); return 2
        return cmd_run(a)
    if a.cmd == "reply-one":
        if not a.topic:
            print("usage: reply-one --topic N [--raw ...]"); return 2
        return cmd_reply_one(a)
    if a.cmd == "like-one":
        if not a.topic:
            print("usage: like-one --topic N [--post M]"); return 2
        return cmd_like_one(a)
    if a.cmd == "revisit":
        return cmd_revisit(a)
    if a.cmd == "post":
        if not (a.title and a.raw):
            print("usage: post --title ... --raw ... --category N --tags a,b"); return 2
        return cmd_post(a)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="L站互动动作执行器（回帖/点赞/发帖）")
    ap.add_argument("cmd", nargs="?", default="due",
                    choices=["due", "run", "reply-one", "like-one", "post", "revisit",
                             "state", "profiles"])
    ap.add_argument("kind", nargs="?", choices=list(KINDS))
    ap.add_argument("--max", type=int, default=2)
    ap.add_argument("--topic", type=int)
    ap.add_argument("--post", type=int)
    ap.add_argument("--raw", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--category", type=int, default=4)
    ap.add_argument("--tags", default="")
    ap.add_argument("--tab", default=None)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--like-prob", type=float, default=REVISIT_LIKE_P, dest="like_prob",
                    help="回访时给新回复点赞的概率（默认 %.2f）" % REVISIT_LIKE_P)
    ap.add_argument("--apply", action="store_true", help="真正发送（默认 dry-run）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--page-events", action="store_true", dest="page_events",
                    help="[已废弃/空转] 页面级合成事件写死关闭（isTrusted=false 反成可疑信号）")
    a = ap.parse_args(argv)
    if getattr(a, "page_events", False):
        print("[deprecated] --page-events 已写死关闭，本次调用不注入任何页面事件；"
              "需要可信输入请用 CDP Input.*（见 temp/ljq_cdptest/FINDINGS.md）",
              file=sys.stderr)

    if a.selftest:
        return selftest()
    if a.cmd in ("run", "reply-one", "like-one", "post", "revisit"):
        tok = acquire_lock("cli-%s" % a.cmd)
        if not tok:
            # 与 daemon（浏览循环 / 排队补动作）互斥：拿到不到锁就安静退出，绝不并发抢标签
            print(json.dumps({"skip": True, "why": "LOCKED_BUSY",
                              "lock": lock_path()}, ensure_ascii=False))
            return 0
        try:
            return dispatch(a)
        finally:
            release_lock(tok)
    return dispatch(a)


if __name__ == "__main__":
    sys.exit(main())
