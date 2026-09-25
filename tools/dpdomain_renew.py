#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DigitalPlat 免费域名续费守护 (dashboard.digitalplat.org)
========================================================
依据（2026-09-25 从站点前端源码 + 线上 UI 实测得出，非猜测）:
  * 规则原文（i18n chunk）: "到期前 120 天内可免费续费。提前续费请使用积分。"
    同批文案: freeRenewalWindow="免费续费窗口" / availableWhen120="剩余少于 120 天时可用。"
              requestFreeRenewal="申请免费续费"
  * 续费接口(源码实测): POST /_panel_api/api/domains/{domain}/renew
        body   {"renewal_type":"free","years":1}
        header Idempotency-Key: <uuid4>
  * CSRF(源码实测): 先读 cookie panel_csrf_token(非 HttpOnly, JS 可读)，
        没有则 GET /_panel_api/api/security/csrf -> json.csrf_token
        header 名 X-CSRF-Token；若响应 code=="csrf_failed" 则重取 token 重试一次
  * 列表接口: GET /_panel_api/api/domains -> {"domains":[...],"ok":true}
        关键字段: expiry_date 形如 "20270828" (YYYYMMDD) / slot_type / status /
                 can_manual_renew / lifecycle_type / pending_delete_release_at

为什么不用 TMWebDriver 的 python 包:
  本机 def 环境缺 simple_websocket_server，import 会失败；直接 HTTP 打
  http://127.0.0.1:18766/link 即可（{"cmd":"execute_js",...}），零依赖。

用法:
  python3 tools/dpdomain_renew.py                       # 只读检查
  python3 tools/dpdomain_renew.py check --json          # 机器可读输出
  python3 tools/dpdomain_renew.py run                   # 演练(不真续费)
  python3 tools/dpdomain_renew.py run --apply           # 真续费(仅窗口内域名)
  python3 tools/dpdomain_renew.py run --apply --notify  # 续费后飞书私信
  python3 tools/dpdomain_renew.py --selftest            # 离线自测(不起浏览器)

退出码: 0=正常(含"窗口内暂无域名") 2=无可用浏览器会话/桥不可达 3=有失败
        4=用法错误 5=自测失败
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 常量
WINDOW_DAYS = 120                     # 免费续费窗口（规则原文 120 天）
ORIGIN = "https://dashboard.digitalplat.org"
BRIDGE = os.environ.get("GA_TMWD_ENDPOINT", "http://127.0.0.1:18766")
SESSION_HINT = "dashboard.digitalplat.org"
RENEW_COOLDOWN_DAYS = 25              # 同一域名成功续费后这段时间内不再重复尝试
DEFAULT_DOMAINS = ["cuipengcx.dpdns.org", "nbdash.dpdns.org"]

RC_OK, RC_NO_SESSION, RC_FAIL, RC_USAGE, RC_SELFTEST = 0, 2, 3, 4, 5


# ---------------------------------------------------------------- 浏览器桥
def _bridge(payload, timeout=90):
    """POST 到 TMWebDriver 的 /link。返回 r 字段内容。"""
    req = urllib.request.Request(
        BRIDGE.rstrip("/") + "/link",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data.get("r", data)


def list_sessions(timeout=20):
    r = _bridge({"cmd": "get_all_sessions"}, timeout=timeout)
    if isinstance(r, dict):
        r = r.get("data", r.get("sessions", []))
    out = []
    for s in r or []:
        if isinstance(s, dict) and s.get("id") is not None:
            out.append({"id": str(s["id"]), "url": s.get("url") or "",
                        "title": s.get("title") or ""})
    return out


def run_js(session_id, code, timeout=60):
    """执行 JS 并把返回的 JSON 字符串解析成对象。"""
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
    """挑一个真正位于 dashboard.digitalplat.org 的会话；找不到返回 (None, 诊断串)。"""
    sessions = list_sessions(timeout=timeout)
    if not sessions:
        return None, "浏览器桥无任何会话（Chrome 未运行 / 扩展未连接 / 驱动未启动？）"
    cands = [s for s in sessions if prefer and s["id"] == str(prefer)]
    cands += [s for s in sessions if SESSION_HINT in (s["url"] or "")]
    seen, ordered = set(), []
    for s in cands:
        if s["id"] not in seen:
            seen.add(s["id"])
            ordered.append(s)
    if not ordered:
        return None, "无 %s 标签页；当前会话: %s" % (
            SESSION_HINT, "; ".join("%s=%s" % (s["id"], s["url"][:60]) for s in sessions[:8]))
    for s in ordered:
        probe = run_js(s["id"], "(()=>location.host)()", timeout=15)
        host = probe.get("raw") or probe.get("data") or probe
        if isinstance(host, str) and SESSION_HINT in host:
            return s, ""
    return None, "候选标签页 %s 均不是 %s（可能已被导航走）" % (
        [s["id"] for s in ordered], SESSION_HINT)


# ---------------------------------------------------------------- JS 客户端
# 忠实复刻站点自己的请求包装（模块 40977/58560），行为一致、不引入新语义
JS_CLIENT = r"""
const DPD = (function(){
  const CSRF_HEADER='X-CSRF-Token', CSRF_COOKIE='panel_csrf_token', PREFIX='/_panel_api';
  const SAFE=new Set(['GET','HEAD','OPTIONS','TRACE']);
  function needsCsrf(m){ return !SAFE.has(String(m||'GET').toUpperCase()); }
  function cookieCsrf(){
    try{
      const k=CSRF_COOKIE+'=';
      const c=document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith(k));
      if(!c) return null;
      const v=c.slice(k.length);
      try{ return decodeURIComponent(v); }catch(e){ return v; }
    }catch(e){ return null; }
  }
  async function freshCsrf(){
    try{
      const r=await fetch(PREFIX+'/api/security/csrf',{credentials:'include',cache:'no-store'});
      const j=await r.json().catch(()=>null);
      if(j && typeof j.csrf_token==='string' && j.csrf_token) return j.csrf_token;
    }catch(e){}
    return cookieCsrf();
  }
  async function req(path,opts){
    opts=opts||{};
    const h=new Headers(opts.headers||{});
    let body=opts.body;
    const method=opts.method||(opts.bodyJson!==undefined?'POST':'GET');
    if(opts.bodyJson!==undefined){ h.set('Content-Type','application/json'); body=JSON.stringify(opts.bodyJson); }
    if(needsCsrf(method) && !h.has(CSRF_HEADER)){
      const t=await freshCsrf();
      if(t) h.set(CSRF_HEADER,t);
    }
    const go=()=>fetch(PREFIX+path,{method:method,body:body,headers:h,credentials:'include',cache:'no-store'});
    let r=await go();
    let j=await r.json().catch(()=>null);
    if(!r.ok && j && j.code==='csrf_failed'){
      const t=await freshCsrf();
      if(t){ h.set(CSRF_HEADER,t); r=await go(); j=await r.json().catch(()=>null); }
    }
    return {status:r.status, ok:r.ok, body:j,
            error:(j&&j.error)||null, code:(j&&j.code)||null};
  }
  return {req:req, cookieCsrf:cookieCsrf};
})();
"""

JS_CHECK = "(async()=>{" + JS_CLIENT + """
  try{
    const r=await DPD.req('/api/domains');
    if(!r.ok) return JSON.stringify({ok:false,status:r.status,error:r.error,code:r.code});
    return JSON.stringify({ok:true,domains:(r.body&&r.body.domains)||[],
                           csrf_from_cookie:!!DPD.cookieCsrf()});
  }catch(e){ return JSON.stringify({ok:false,error:String(e)}); }
})()
"""

JS_RENEW_TMPL = "(async()=>{" + JS_CLIENT + """
  const NAMES=%(names)s;
  const out={results:{},after:null};
  for(const n of NAMES){
    try{
      const r=await DPD.req('/api/domains/'+encodeURIComponent(n)+'/renew',
        {method:'POST',headers:{'Idempotency-Key':(crypto.randomUUID?
            crypto.randomUUID():String(Date.now())+Math.random())},
         bodyJson:{renewal_type:'free',years:1}});
      const d=r.body&&r.body.domain;
      out.results[n]={status:r.status,ok:r.ok,error:r.error,code:r.code,
                      expiry_date:(d&&d.expiry_date)||null,
                      domain_status:(d&&d.status)||null};
    }catch(e){ out.results[n]={ok:false,error:String(e)}; }
  }
  try{
    const r=await DPD.req('/api/domains');
    out.after=r.body&&r.body.domains||null;
    out.after_ok=!!r.ok;
  }catch(e){ out.after_err=String(e); }
  return JSON.stringify(out);
})()
"""


# ---------------------------------------------------------------- 纯逻辑
def parse_expiry(v):
    """'20270828' / '2027-08-28' -> date"""
    if v is None:
        return None
    s = str(v).strip().replace("-", "").replace("/", "")
    if len(s) != 8 or not s.isdigit():
        return None
    return dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def days_left(expiry, today=None):
    today = today or dt.date.today()
    return (expiry - today).days


def is_in_window(expiry, today=None, window=WINDOW_DAYS):
    """窗口内 == 0 <= 剩余天数 <= window（到期日当天仍可续；已过期不算）"""
    d = days_left(expiry, today)
    return 0 <= d <= window


def select_by_name(domains, names):
    """按域名字符串挑出目标（忽略大小写/尾部点）"""
    idx = {}
    for d in domains or []:
        key = str(d.get("domain") or d.get("name") or "").lower().rstrip(".")
        if key:
            idx.setdefault(key, d)
    return [idx[n.lower().rstrip(".")] for n in names
            if n.lower().rstrip(".") in idx]


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("[warn] state 写入失败: %s" % e, file=sys.stderr)
        return False


def state_path():
    if os.environ.get("DPDOMAIN_STATE"):
        return os.environ["DPDOMAIN_STATE"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "temp", "dpdomain_renew_state.json")


def recently_renewed(state, domain, today=None, cooldown=RENEW_COOLDOWN_DAYS):
    rec = (state or {}).get(domain) or {}
    ts = rec.get("last_success")
    if not ts:
        return False
    try:
        d = dt.date.fromisoformat(str(ts)[:10])
    except Exception:
        return False
    return (today or dt.date.today()) - d < dt.timedelta(days=cooldown)


def build_plan(domains, today=None, state=None, names=None, window=WINDOW_DAYS):
    """返回每个域名的判定结果列表（纯函数，可离线自测）"""
    today = today or dt.date.today()
    rows = []
    for d in select_by_name(domains, names) if names else (domains or []):
        name = str(d.get("domain") or d.get("name") or "?")
        exp = parse_expiry(d.get("expiry_date"))
        dl = days_left(exp, today) if exp else None
        inw = is_in_window(exp, today, window) if exp else False
        row = {"domain": name, "expiry": exp.isoformat() if exp else None,
               "days_left": dl, "in_window": inw,
               "slot_type": d.get("slot_type"), "status": d.get("status"),
               "lifecycle_type": d.get("lifecycle_type"),
               "can_manual_renew": d.get("can_manual_renew"),
               "renew_due": bool(inw and not recently_renewed(state or {}, name, today)),
               "skip_reason": None}
        if not inw:
            row["skip_reason"] = ("已过期/待删除" if (dl is not None and dl < 0)
                                  else "未进入 %d 天免费窗口(剩余 %s 天)" % (window, dl))
        elif recently_renewed(state or {}, name, today):
            row["skip_reason"] = "%d 天内已成功续费过，跳过" % RENEW_COOLDOWN_DAYS
            row["renew_due"] = False
        rows.append(row)
    return rows


def notify(text):
    """飞书私信（失败不致命）"""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reflect_scripts"))
    try:
        import reflect_common
        return reflect_common.notify_feishu(text)
    except Exception as e:
        print("[warn] 通知失败: %s" % e, file=sys.stderr)
        return None


# ---------------------------------------------------------------- 主流程
def do_check(as_json=False, session=None):
    sess, why = pick_session(prefer=session)
    if not sess:
        res = {"ok": False, "error": "NO_SESSION", "detail": why}
        print(json.dumps(res, ensure_ascii=False) if as_json else "[NO_SESSION] " + why)
        return RC_NO_SESSION, res
    r = run_js(sess["id"], JS_CHECK, timeout=60)
    if not r.get("ok"):
        res = {"ok": False, "error": "API_ERROR", "detail": r}
        print(json.dumps(res, ensure_ascii=False) if as_json else "[API_ERROR] %s" % r)
        return RC_FAIL, res
    res = {"ok": True, "session": sess["id"], "csrf_from_cookie": r.get("csrf_from_cookie"),
           "domains": r.get("domains") or []}
    if not as_json:
        print("会话 %s | 域名 %d 个" % (sess["id"], len(res["domains"])))
        for d in res["domains"]:
            print("  - %-24s 到期 %s (%s)" % (d.get("domain"), d.get("expiry_date_display") or
                                              d.get("expiry_date"), d.get("status")))
    else:
        print(json.dumps(res, ensure_ascii=False))
    return RC_OK, res


def do_run(apply=False, as_json=False, do_notify=False, session=None, names=None, today=None):
    rc, check = do_check(as_json=False, session=session)
    if rc != RC_OK:
        if do_notify:
            notify("[DigitalPlat] 续费检查失败：%s\n%s" % (check.get("error"), check.get("detail", "")))
        return rc, check
    today = today or dt.date.today()
    state = load_state(state_path())
    plan = build_plan(check["domains"], today=today, state=state, names=names)
    due = [p for p in plan if p["renew_due"]]

    print("\n判定（窗口=%d 天，今天 %s）:" % (WINDOW_DAYS, today.isoformat()))
    for p in plan:
        print("  - %-24s 剩余 %5s 天 | %s" % (
            p["domain"], p["days_left"],
            "★需续费" if p["renew_due"] else ("跳过: %s" % p["skip_reason"])))

    if not due:
        print("\n结论: 无域名需要续费（窗口内为空）")
        if as_json:
            print(json.dumps({"ok": True, "applied": False, "plan": plan}, ensure_ascii=False))
        return RC_OK, {"ok": True, "applied": False, "plan": plan}

    if not apply:
        print("\n[dry-run] 若加 --apply 将对以下域名执行免费续费(+1年): %s"
              % ", ".join(p["domain"] for p in due))
        if as_json:
            print(json.dumps({"ok": True, "applied": False, "would_renew": [p["domain"] for p in due],
                              "plan": plan}, ensure_ascii=False))
        return RC_OK, {"ok": True, "applied": False, "would_renew": [p["domain"] for p in due]}

    names_due = [p["domain"] for p in due]
    js = JS_RENEW_TMPL % {"names": json.dumps(names_due)}
    r = run_js(check["session"], js, timeout=120)
    print("\n续费响应: %s" % json.dumps(r.get("results"), ensure_ascii=False))

    after = {str(d.get("domain")): d for d in (r.get("after") or [])}
    summary, failures = [], []
    for p in due:
        n = p["domain"]
        got = r.get("results", {}).get(n) or {}
        new_exp = parse_expiry((after.get(n) or {}).get("expiry_date"))
        advanced = bool(new_exp and p["expiry"] and new_exp.isoformat() != p["expiry"])
        ok = bool(got.get("ok")) and advanced
        summary.append({"domain": n, "ok": ok, "expiry_before": p["expiry"],
                        "expiry_after": (new_exp.isoformat() if new_exp else None),
                        "advanced": advanced, "status": got.get("status"),
                        "error": got.get("error"), "code": got.get("code")})
        if ok:
            st = state.setdefault(n, {})
            st["last_success"] = today.isoformat()
            st["expiry_after"] = new_exp.isoformat()
            st["source"] = "tools/dpdomain_renew.py"
        else:
            failures.append(n)
    if any(s["ok"] for s in summary):
        save_state(state_path(), state)

    for s in summary:
        print("  %s %s: %s -> %s %s" % ("✅" if s["ok"] else "❌", s["domain"],
                                        s["expiry_before"], s["expiry_after"],
                                        s["error"] or ""))

    if do_notify:
        if failures:
            notify("[DigitalPlat] 续费失败，需人工处理: %s\n详情: %s" % (
                ", ".join(failures), json.dumps(summary, ensure_ascii=False)))
        else:
            notify("[DigitalPlat] 已自动免费续费 %d 个域名:\n%s" % (
                len([s for s in summary if s["ok"]]),
                "\n".join("· %s: %s -> %s" % (s["domain"], s["expiry_before"], s["expiry_after"])
                          for s in summary if s["ok"])))

    payload = {"ok": not failures, "applied": True, "summary": summary, "plan": plan}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    return (RC_OK if not failures else RC_FAIL), payload


# ---------------------------------------------------------------- 自测
def selftest():
    import tempfile
    fails = []
    total = []

    def ck(cond, msg):
        total.append(1)
        print(("  PASS " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("[1] parse_expiry 各种写法")
    D = dt.date
    ck(parse_expiry("20270828") == D(2027, 8, 28), "紧凑 YYYYMMDD")
    ck(parse_expiry("2027-08-28") == D(2027, 8, 28), "带横线")
    ck(parse_expiry("") is None and parse_expiry(None) is None and parse_expiry("bad") is None,
       "非法输入->None")

    print("[2] 窗口边界（规则: 0..120 天）")
    t = D(2026, 9, 25)
    ck(is_in_window(D(2026, 9, 25), t) is True, "今天到期=在窗口(days=0)")
    ck(is_in_window(t + dt.timedelta(days=120), t) is True, "剩余=120 天 -> 在窗口")
    ck(is_in_window(t + dt.timedelta(days=121), t) is False, "剩余=121 天 -> 不在窗口")
    ck(is_in_window(t - dt.timedelta(days=1), t) is False, "已过期 -> 不在窗口")
    ck(days_left(D(2027, 8, 28), t) == 337, "实盘 cuipengcx: 337 天")
    ck(is_in_window(D(2027, 8, 28), t) is False, "实盘 cuipengcx 当前不在窗口")

    print("[3] JS 载荷含正确的接口/参数/CSRF 语义")
    j = JS_RENEW_TMPL % {"names": json.dumps(["a.dpdns.org"])}
    for frag in ["/_panel_api", "/api/domains/", "/renew", "Idempotency-Key",
                 "X-CSRF-Token", "panel_csrf_token", "csrf_failed",
                 "renewal_type:'free'", "years:1", "credentials:'include'",
                 "crypto.randomUUID"]:
        ck(frag in j, "载荷含 %r" % frag)
    ck('"a.dpdns.org"' in j, "域名注入正确")
    ck("window.location" not in j and "location.href" not in j,
       "负控: 载荷不做任何页面跳转")

    print("[4] build_plan: 窗口内/外 + 冷却 + 缺失域名")
    doms = [{"domain": "in.dpdns.org", "expiry_date": "20261201", "status": "ok"},
            {"domain": "out.dpdns.org", "expiry_date": "20270828", "status": "ok"},
            {"domain": "exp.dpdns.org", "expiry_date": "20260101", "status": "pendingDelete"}]
    plan = {p["domain"]: p for p in build_plan(doms, today=D(2026, 9, 25), state={})}
    ck(plan["in.dpdns.org"]["renew_due"] is True, "剩余 67 天 -> 需续费")
    ck(plan["out.dpdns.org"]["renew_due"] is False, "剩余 337 天 -> 不需续费")
    ck(plan["exp.dpdns.org"]["renew_due"] is False
       and "已过期" in plan["exp.dpdns.org"]["skip_reason"], "pendingDelete 不自动续费")
    st = {"in.dpdns.org": {"last_success": "2026-09-20"}}
    p2 = {p["domain"]: p for p in build_plan(doms, today=D(2026, 9, 25), state=st)}
    ck(p2["in.dpdns.org"]["renew_due"] is False, "5 天前刚续过 -> 冷却跳过")
    st2 = {"in.dpdns.org": {"last_success": "2026-07-01"}}
    p3 = {p["domain"]: p for p in build_plan(doms, today=D(2026, 9, 25), state=st2)}
    ck(p3["in.dpdns.org"]["renew_due"] is True, "冷却期外 -> 可以再续")
    p4 = build_plan(doms, today=D(2026, 9, 25), state={}, names=["out.dpdns.org"])
    ck([x["domain"] for x in p4] == ["out.dpdns.org"], "names 过滤生效")
    p5 = build_plan(doms, today=D(2026, 9, 25), state={}, names=["nope.dpdns.org"])
    ck(p5 == [], "未知域名 -> 空(不崩)")

    print("[5] state 读写 + 原子替换")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        ck(save_state(sp, {"a": {"last_success": "2026-09-25"}}) is True, "写入")
        ck(load_state(sp)["a"]["last_success"] == "2026-09-25", "回读一致")
        ck(load_state(os.path.join(td, "missing.json")) == {}, "缺文件->空 dict")

    print("[6] 默认域名清单自洽")
    ck(len(DEFAULT_DOMAINS) == 2 and all("." in d for d in DEFAULT_DOMAINS), "两个默认域名")

    print("\n%s  selftest: %d passed, %d failed" %
          ("ALL GREEN" if not fails else "FAILED", 20 + 17 - len(fails), len(fails)))
    return RC_SELFTEST if fails else RC_OK


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="DigitalPlat 免费域名续费守护")
    ap.add_argument("mode", nargs="?", default="check", choices=["check", "run"])
    ap.add_argument("--apply", action="store_true", help="真的执行续费(默认 dry-run)")
    ap.add_argument("--notify", action="store_true", help="飞书私信推送结果")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--session", help="指定 tmwebdriver 会话 id")
    ap.add_argument("--domains", help="只处理这些域名(逗号分隔)")
    ap.add_argument("--selftest", action="store_true", help="离线自测，不连浏览器")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    names = [s.strip() for s in args.domains.split(",")] if args.domains else None
    try:
        if args.mode == "check":
            return do_check(as_json=args.json, session=args.session)[0]
        return do_run(apply=args.apply, as_json=args.json, do_notify=args.notify,
                      session=args.session, names=names)[0]
    except urllib.error.URLError as e:
        print("[NO_SESSION] 浏览器桥 %s 不可达: %s" % (BRIDGE, e))
        return RC_NO_SESSION


if __name__ == "__main__":
    sys.exit(main())
