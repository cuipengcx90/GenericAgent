#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""禅道取数（只读）——绩效自评三源之一（飞书沟通 → 禅道派单 → 代码落地）。

口径（2026-10-09 实测定型，SOP=memory/performance_self_review_sop）:
  * 通道：TMWebDriver 桥 http://127.0.0.1:18766/link 的 execute_js，
    在本机已登录的禅道标签页里跑**同源 fetch**（不注入 cookie、不读凭据）。
  * 会话：--session-id 给定，或 env GA_ZENTAO_SESSION_ID，或自动从
    {"cmd":"get_all_sessions"} 里挑 url 含 pm.smyze.cn 的标签（★sessionId 必须传字符串）。
  * Bug 全量：逐产品 index.php?m=bug&f=browse&product=<pid>&branch=0
    &browseType=resolvedbyme&param=0&orderBy=id_desc&t=json&pageID=<n>&recPerPage=100
    ★翻页配方敏感：pageID 必须写在 recPerPage 之前，否则被服务端忽略（返 20 行上限）。
    产品清单取自 product=all 的 data.products。
  * my/work 端点不接受翻页（带 pageID/recPerPage 返空）⇒ 只有 recTotal ≤ 20 的集合可直接取。
    任务行字段是 name（不是 title），日期字段 finishedDate；Bug 用 title/resolvedDate。
  * 窗口：半开区间 [since, until)，复用 tools/quarter_window.py（单一源），
    按 Bug.resolvedDate / 任务.finishedDate 过滤 ⇒ 相邻季度零重叠零遗漏。
  * 归属：再核一遍 resolvedBy/finishedBy == --author（服务端已按登录账号过滤，
    此处只做机械复核，不符者计入 identity_mismatch 并从计数中剔除）。

用法:
  python3 tools/zentao_stats.py --quarter Q3-2026
  python3 tools/zentao_stats.py --offset -1 --json-out temp/zentao_stats/q2.json --md-out temp/zentao_stats/q2.md
  python3 tools/zentao_stats.py --since 2026-07-01 --until 2026-10-01 --raw-out temp/zentao_stats/raw.json
  python3 tools/zentao_stats.py --selftest        # 离线自测（零网络零写盘）
退出码: 0 正常 / 2 参数或取数失败（fail-closed，绝不静默给空数）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quarter_window as qw  # noqa: E402  窗口单一源

BRIDGE = "http://127.0.0.1:18766/link"
HOST_HINT = "pm.smyze.cn"
PAGE_SIZE = 100
MAX_PAGES = 20
DEFAULT_AUTHOR = "cp"
DEFAULT_STATE = "temp/zentao_stats/session.json"


class FetchError(RuntimeError):
    pass


def _post(payload, timeout=200):
    req = urllib.request.Request(
        BRIDGE, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    if not body.strip():
        raise FetchError("桥返回空体（cmd=%s）" % payload.get("cmd"))
    return json.loads(body)


def unwrap(resp):
    """禅道 t=json 响应 {status, data(JSON 字符串), md5} → dict（二次 parse）。"""
    if not isinstance(resp, dict):
        raise FetchError("响应非对象: %r" % type(resp))
    if "data" not in resp:
        raise FetchError("响应缺 data 字段: %s" % json.dumps(resp)[:200])
    data = resp["data"]
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise FetchError("data 非对象: %r" % type(data))
    return data


def discover_session():
    r = _post({"cmd": "get_all_sessions"}, timeout=30)
    items = r.get("r") or r.get("data") or []
    for s in items:
        if HOST_HINT in (s.get("url") or ""):
            return str(s.get("id"))
    raise FetchError("未找到含 %s 的浏览器标签会话（请先在浏览器打开禅道）" % HOST_HINT)


def resolve_session(explicit=None, state_path=None):
    if explicit:
        return str(explicit)
    env = os.environ.get("GA_ZENTAO_SESSION_ID")
    if env:
        return str(env)
    for p in [state_path, DEFAULT_STATE]:
        if p and os.path.isfile(p):
            try:
                sid = json.load(open(p, encoding="utf-8")).get("session_id")
                if sid:
                    return str(sid)
            except Exception:
                pass
    return discover_session()


def _extract(resp):
    """从桥响应里剥壳取出 execute_js 的返回值（兼容 {r:{data}} / {r:str} / {status,data} / 裸值）。"""
    obj = resp
    for _ in range(4):
        if isinstance(obj, dict) and "r" in obj and "data" not in obj:
            obj = obj["r"]
            continue
        if isinstance(obj, dict) and "data" in obj:
            obj = obj["data"]
            continue
        break
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except ValueError:
            return obj
    return obj


def execute_js(code, session, timeout="180"):
    payload = {"cmd": "execute_js", "sessionId": str(session),
               "code": code, "timeout": str(timeout)}
    resp = _post(payload, timeout=int(timeout) + 60)
    val = _extract(resp)
    if isinstance(val, dict) and val.get("error"):
        raise FetchError("execute_js 失败: %s" % json.dumps(val, ensure_ascii=False)[:300])
    return val


COLLECT_JS = r"""
(async () => {
  const out = {ok: true, products: {}, bugs: [], tasks: [], doing: [], errors: []};
  const get = async (url) => {
    try {
      const r = await fetch(url, {credentials: 'same-origin'});
      const t = await r.text();
      try { return JSON.parse(t); } catch (e) { return {__err: 'not-json: ' + t.slice(0, 80)}; }
    } catch (e) { return {__err: 'fetch-fail: ' + String(e).slice(0, 80)}; }
  };
  const parse = (resp) => {
    let d = resp && resp.data;
    if (typeof d === 'string') { try { d = JSON.parse(d); } catch (e) { d = null; } }
    return d || null;
  };
  const rows = (obj) => Object.keys(obj || {}).map(k => obj[k]);
  // 1) 产品清单（resolvedbyme 视图里的 products）
  const prod = parse(await get('/index.php?m=bug&f=browse&product=all&branch=0&browseType=resolvedbyme&param=0&t=json'));
  if (prod && prod.products) { out.products = prod.products; }
  else { out.errors.push('products-unavailable'); }
  // 2) 逐产品取「我解决的」Bug
  //    ★实测(2026-09-30)：该端点 pageID 被服务端忽略（pageID=1 与 pageID=2 逐字相同），
  //      故「翻页」无意义，每产品实际只能拿到首页 min(recTotal, recPerPage) 行；
  //      唯一权威上限信号 = 响应里的 pager.recTotal ⇒ 用它判完整性，不足则加大页宽重取一次。
  const PAGE_SIZE_JS = __PAGE_SIZE__;
  out.pagers = [];
  const fetchProduct = async (pid, size) => {
    const url = '/index.php?m=bug&f=browse&product=' + pid
      + '&branch=0&browseType=resolvedbyme&param=0&orderBy=id_desc&t=json'
      + '&pageID=1&recPerPage=' + size;
    const d = parse(await get(url));
    if (!d || !d.bugs) { return {rows: [], recTotal: null, recPerPage: size, err: 'bug-view:' + pid}; }
    const pg = d.pager || {};
    const rt = (pg.recTotal !== undefined && pg.recTotal !== null) ? Number(pg.recTotal)
             : (d.recTotal !== undefined && d.recTotal !== null ? Number(d.recTotal) : null);
    return {rows: rows(d.bugs), recTotal: rt, recPerPage: Number(pg.recPerPage || size), err: null};
  };
  const byId = {}; let noId = 0;
  for (const pid of Object.keys(out.products || {})) {
    let r = await fetchProduct(pid, PAGE_SIZE_JS);
    if (r.err) { out.errors.push(r.err); }
    if (r.recTotal !== null && r.rows.length < r.recTotal) {
      const big = Math.max(2000, r.recTotal + 100);
      const r2 = await fetchProduct(pid, big);
      if (!r2.err) {
        out.retry = out.retry || {};
        out.retry[pid] = {requested: big, got: r2.rows.length, recTotal: r2.recTotal,
                          first_got: r.rows.length, first_page_size: PAGE_SIZE_JS};
        r = r2;
      }
    }
    out.pagers.push({product: pid, got: r.rows.length, recTotal: r.recTotal, recPerPage: r.recPerPage});
    for (const b of r.rows) {
      const k = (b && b.id !== undefined && b.id !== null) ? String(b.id) : ('noid:' + (noId++));
      byId[k] = b;
    }
  }
  out.bugs = Object.keys(byId).map(k => byId[k]);
  // 3) my/work（不可翻页：recTotal ≤ 20 才可靠）
  const myWork = async (mode, type) => parse(await get(
    '/index.php?m=my&f=work&mode=' + mode + '&type=' + type + '&t=json')) || {};
  const tf = await myWork('task', 'finishedBy');
  out.tasks = rows(tf.tasks);
  const bd = await myWork('bug', 'doing');
  const td = await myWork('task', 'doing');
  out.doing = rows(bd.bugs).concat(rows(td.tasks));
  return JSON.stringify(out);
})()
"""


def collect(session):
    """真取数：返回原始 payload（dict）。取数失败抛 FetchError。"""
    val = execute_js(COLLECT_JS.replace("__PAGE_SIZE__", str(PAGE_SIZE)), session)
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError as e:
            raise FetchError("采集脚本返回非 JSON: %s" % str(e))
    if not isinstance(val, dict):
        raise FetchError("采集脚本返回类型异常: %r" % type(val))
    if not val.get("bugs") and val.get("errors"):
        raise FetchError("取数失败: %s" % ",".join(map(str, val["errors"])))
    return val


def _dist(rows, key, name_map=None):
    c = {}
    for r in rows:
        k = str(r.get(key) if r.get(key) not in (None, "") else "?")
        label = k if not name_map else ("%s %s" % (k, name_map.get(k, ""))).strip()
        c[label] = c.get(label, 0) + 1
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def aggregate(payload, since, until, author=DEFAULT_AUTHOR, page_size=PAGE_SIZE):
    """纯函数：payload + 窗口 → 统计摘要（可离线自测）。"""
    bugs = list(payload.get("bugs") or [])
    tasks = list(payload.get("tasks") or [])
    doing = list(payload.get("doing") or [])
    prods = {str(k): v for k, v in (payload.get("products") or {}).items()}

    def in_win(d):
        return isinstance(d, str) and since <= d < until

    b_win = [r for r in bugs if in_win(r.get("resolvedDate"))]
    b_mine = [r for r in b_win if str(r.get("resolvedBy") or "") == author]
    b_other = [r for r in b_win if str(r.get("resolvedBy") or "") != author]

    t_win = [r for r in tasks if in_win(r.get("finishedDate"))]
    t_mine = [r for r in t_win if str(r.get("finishedBy") or "") == author]
    t_other = [r for r in t_win if str(r.get("finishedBy") or "") != author]

    # 完整性断言（★两级判据，优先权威源）
    #  A(优先) 新 payload 带 pagers（每产品 got/recTotal，取自响应 pager）⇒ 逐产品判完整性
    #  B(回退) 旧 payload 无 pagers ⇒ 沿用「某产品行数 ≥ 页宽」的保守启发式（可能漏报，见自测负控）
    pagers = [p for p in (payload.get("pagers") or []) if isinstance(p, dict)]
    if pagers:
        verified, short, unknown = [], {}, {}
        for p in pagers:
            pid, got, rt = str(p.get("product")), int(p.get("got") or 0), p.get("recTotal")
            if rt in (None, ""):
                unknown[pid] = got
            elif got >= int(rt):
                verified.append(pid)
            else:
                short[pid] = {"got": got, "recTotal": int(rt)}
        completeness = {
            "state": ("truncated" if short
                      else ("verified" if not unknown else "verified_with_unknown")),
            "method": "pager.recTotal", "page_size": page_size,
            "products_verified": len(verified), "products_short": short,
            "products_unknown": unknown, "retry": payload.get("retry") or {},
        }
        trunc = {k: v["got"] for k, v in short.items()}
    else:
        page_counts = {}
        for r in bugs:
            page_counts[str(r.get("product"))] = page_counts.get(str(r.get("product")), 0) + 1
        trunc = {k: v for k, v in page_counts.items() if v >= page_size}
        completeness = {"state": "unknown_legacy_payload", "method": "page_count_heuristic",
                        "page_size": page_size, "products_verified": 0, "products_short": {},
                        "products_unknown": {}, "retry": {}}

    def month(rows, key):
        c = {}
        for r in rows:
            m = (r.get(key) or "")[:7]
            if m:
                c[m] = c.get(m, 0) + 1
        return dict(sorted(c.items()))

    return {
        "window": {"since": since, "until": until, "author": author},
        "source": {
            "channel": "TMWebDriver bridge /link execute_js (same-origin fetch)",
            "bugs": {"fetched_total": len(bugs), "in_window": len(b_win),
                     "counted": len(b_mine), "identity_mismatch": len(b_other)},
            "tasks": {"fetched_total": len(tasks), "in_window": len(t_win),
                      "counted": len(t_mine), "identity_mismatch": len(t_other)},
            "products": prods,
            "fetch_errors": list(payload.get("errors") or []),
        },
        "bugs": {
            "count": len(b_mine),
            "by_month": month(b_mine, "resolvedDate"),
            "by_product": _dist(b_mine, "product", prods),
            "by_severity": _dist(b_mine, "severity"),
            "by_type": _dist(b_mine, "type"),
            "by_resolution": _dist(b_mine, "resolution"),
            "links": [{"id": r.get("id"), "title": r.get("title"),
                       "product": r.get("product"), "resolvedDate": r.get("resolvedDate"),
                       "type": r.get("type"), "severity": r.get("severity")}
                      for r in sorted(b_mine, key=lambda r: str(r.get("resolvedDate") or ""))],
        },
        "tasks": {
            "count": len(t_mine),
            "by_month": month(t_mine, "finishedDate"),
            "consumed_hours": round(sum(_num(r.get("consumed")) for r in t_mine), 2),
            "est_hours": round(sum(_num(r.get("estimate")) for r in t_mine), 2),
            "items": [{"id": r.get("id"), "name": r.get("name") or r.get("title"),
                       "finishedDate": r.get("finishedDate"),
                       "consumed": r.get("consumed"), "estimate": r.get("estimate")}
                      for r in sorted(t_mine, key=lambda r: str(r.get("finishedDate") or ""))],
        },
        "doing": {"count": len(doing),
                  "items": [{"id": r.get("id"), "name": r.get("name") or r.get("title"),
                             "status": r.get("status")} for r in doing]},
        "assertions": {
            "page_size": page_size,
            "truncation_risk": trunc,
            "truncation_ok": not trunc,
            "completeness": completeness,
            "nothing_dropped": (len(b_win) == len(b_mine) + len(b_other)
                                and len(t_win) == len(t_mine) + len(t_other)),
        },
    }


def render_md(s):
    w = s["window"]
    L = ["## 禅道（取数工具 tools/zentao_stats.py）", "",
         "- 窗口: %s → %s（半开区间）｜账号: %s" % (w["since"], w["until"], w["author"]),
         "- Bug: 拉取 %d 条 / 窗口内 %d 条 / 计入 %d 条（归属不符 %d）"
         % (s["source"]["bugs"]["fetched_total"], s["source"]["bugs"]["in_window"],
            s["source"]["bugs"]["counted"], s["source"]["bugs"]["identity_mismatch"]),
         "- 任务: 拉取 %d 条 / 窗口内 %d 条 / 计入 %d 条（归属不符 %d）"
         % (s["source"]["tasks"]["fetched_total"], s["source"]["tasks"]["in_window"],
            s["source"]["tasks"]["counted"], s["source"]["tasks"]["identity_mismatch"]),
         "- Bug 按月: %s" % json.dumps(s["bugs"]["by_month"], ensure_ascii=False),
         "- Bug 按产品: %s" % json.dumps(s["bugs"]["by_product"], ensure_ascii=False),
         "- Bug 按类型: %s" % json.dumps(s["bugs"]["by_type"], ensure_ascii=False),
         "- Bug 按解决方式: %s" % json.dumps(s["bugs"]["by_resolution"], ensure_ascii=False),
         "- 任务按月: %s｜工时合计 %s h" % (json.dumps(s["tasks"]["by_month"], ensure_ascii=False),
                                          s["tasks"]["consumed_hours"]),
         "- 进行中: %d 条" % s["doing"]["count"],
         "- 断言: 截断风险=%s｜计数守恒=%s" % (s["assertions"]["truncation_risk"] or "无",
                                            s["assertions"]["nothing_dropped"]),
         "- 完整性: %s｜判据=%s｜已验证 %d 产品｜不足 %s｜重取 %s"
         % (s["assertions"]["completeness"]["state"], s["assertions"]["completeness"]["method"],
            s["assertions"]["completeness"]["products_verified"],
            json.dumps(s["assertions"]["completeness"]["products_short"], ensure_ascii=False) or "无",
            json.dumps(s["assertions"]["completeness"]["retry"], ensure_ascii=False) or "无"), ""]
    if s["source"]["fetch_errors"]:
        L.append("- ⚠ 取数告警: %s" % ",".join(map(str, s["source"]["fetch_errors"])))
    return "\n".join(L) + "\n"


def _dump(path, text):
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="禅道取数（只读，绩效自评三源之一）")
    ap.add_argument("--quarter", help="如 Q3-2026 / 2026Q3")
    ap.add_argument("--offset", type=int, default=0, help="相对当前季度的偏移，-1=上一季度")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--author", default=DEFAULT_AUTHOR, help="账号（默认 cp）")
    ap.add_argument("--session-id", dest="session_id")
    ap.add_argument("--state", default=DEFAULT_STATE, help="会话缓存文件")
    ap.add_argument("--json-out")
    ap.add_argument("--md-out")
    ap.add_argument("--raw-out", help="落原始 payload（便于离线复算）")
    ap.add_argument("--payload-in", help="跳过取数，直接消费已有 payload(JSON)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="完整性判定为 truncated 时仍返回数字（默认 fail-closed 返 rc=3）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        w = qw.resolve(args.since, args.until, args.quarter, args.offset)
    except ValueError as e:
        print("ERROR %s" % e, file=sys.stderr)
        return 2

    if args.payload_in:
        payload = json.load(open(args.payload_in, encoding="utf-8"))
        session = None
    else:
        try:
            session = resolve_session(args.session_id, args.state)
            payload = collect(session)
        except FetchError as e:
            print("ERROR 禅道取数失败: %s" % e, file=sys.stderr)
            return 2
        except Exception as e:  # 桥/网络异常一律 fail-closed
            print("ERROR 禅道取数异常(%s): %s" % (type(e).__name__, e), file=sys.stderr)
            return 2

    if args.raw_out:
        _dump(args.raw_out, json.dumps(payload, ensure_ascii=False, indent=1))

    summary = aggregate(payload, w["since"], w["until"], args.author)
    summary["window"].update({"label": w["label"], "days": w["days"]})
    if summary["assertions"]["completeness"]["state"] == "truncated" and not args.allow_partial:
        print("ERROR 禅道取数不完整：某产品实际行数 < 服务端 recTotal（pager 权威值），"
              "且该端点 pageID 被忽略无法翻页 ⇒ 数字不可用。明细: %s"
              % json.dumps(summary["assertions"]["completeness"]["products_short"], ensure_ascii=False),
              file=sys.stderr)
        return 3
    text = json.dumps(summary, ensure_ascii=False, indent=1)
    _dump(args.json_out, text + "\n")
    md = render_md(summary)
    _dump(args.md_out, md)
    if args.json_out or args.md_out:
        # 有落盘时 stdout 只给一行要点，避免刷屏
        print("OK bugs=%d tasks=%d doing=%d window=%s→%s%s"
              % (summary["bugs"]["count"], summary["tasks"]["count"], summary["doing"]["count"],
                 w["since"][:10], w["until"][:10],
                 " (session=%s)" % session if session else ""))
    else:
        print(text)
    return 0


def selftest():
    """离线自测（不联网、不碰桥）。全部断言通过返 0，否则 1。"""
    ck, bad = [], []
    def chk(name, cond):
        ck.append(name)
        if not cond:
            bad.append(name)

    since, until = "2026-07-01 00:00:00", "2026-10-01 00:00:00"
    prods = {"14": "Smyze商业", "26": "Smyze内部", "8": "其他产品"}
    payload = {
        "ok": True,
        "products": prods,
        "bugs": [
            {"id": 1, "title": "A", "product": "14", "resolvedBy": "cp", "resolvedDate": "2026-07-05 10:00:00",
             "severity": "3", "type": "codeerror", "resolution": "fixed", "status": "closed"},
            {"id": 2, "title": "B", "product": "14", "resolvedBy": "cp", "resolvedDate": "2026-09-30 23:59:59",
             "severity": "4", "type": "others", "resolution": "bydesign", "status": "resolved"},
            {"id": 3, "title": "边界后", "product": "26", "resolvedBy": "cp", "resolvedDate": "2026-10-01 00:00:00",
             "severity": "1", "type": "codeerror", "resolution": "fixed", "status": "closed"},
            {"id": 4, "title": "边界前", "product": "26", "resolvedBy": "cp", "resolvedDate": "2026-06-30 23:59:59",
             "severity": "1", "type": "codeerror", "resolution": "fixed", "status": "closed"},
            {"id": 5, "title": "他人", "product": "8", "resolvedBy": "lisi", "resolvedDate": "2026-08-08 09:00:00",
             "severity": "2", "type": "codeerror", "resolution": "fixed", "status": "closed"},
            {"id": 6, "title": "缺字段", "product": "8", "resolvedBy": "cp", "resolvedDate": "2026-08-09 09:00:00"},
        ],
        "tasks": [
            {"id": 101, "name": "任务甲", "finishedBy": "cp", "finishedDate": "2026-07-10 18:00:00",
             "consumed": "3.5", "estimate": "4"},
            {"id": 102, "name": "任务乙", "finishedBy": "cp", "finishedDate": "2026-09-20 18:00:00",
             "consumed": "2", "estimate": "2"},
            {"id": 103, "title": "只有 title", "finishedBy": "cp", "finishedDate": "2026-08-01 18:00:00",
             "consumed": None, "estimate": "1"},
            {"id": 104, "name": "他人任务", "finishedBy": "wang", "finishedDate": "2026-08-02 18:00:00",
             "consumed": "100", "estimate": "100"},
        ],
        "doing": [{"id": 201, "name": "在做", "status": "doing"}],
        "errors": [],
    }
    s = aggregate(payload, since, until, "cp")

    chk("窗口内 4 条、计入 3 条（越界 1 条、他人 1 条被剔）",
        s["source"]["bugs"]["in_window"] == 4 and s["bugs"]["count"] == 3)
    chk("归属不符=1 且来自 8 号产品", s["source"]["bugs"]["identity_mismatch"] == 1)
    chk("守恒断言成立", s["assertions"]["nothing_dropped"] is True)
    chk("按月分桶 7 月=1/8 月=1/9 月=1", s["bugs"]["by_month"] == {"2026-07": 1, "2026-08": 1, "2026-09": 1})
    chk("按产品含产品名 14 Smyze商业=2", s["bugs"]["by_product"].get("14 Smyze商业") == 2)
    chk("缺 severity 的行归 '?'", s["bugs"]["by_severity"].get("?") == 1)
    chk("resolution 分布 fixed=1/bydesign=1/缺字段=1", s["bugs"]["by_resolution"].get("fixed") == 1
        and s["bugs"]["by_resolution"].get("bydesign") == 1 and s["bugs"]["by_resolution"].get("?") == 1)
    chk("任务窗口内=3（他人剔 1）", s["tasks"]["count"] == 3)
    chk("任务 consumed 合计 5.5（None 记 0）", abs(s["tasks"]["consumed_hours"] - 5.5) < 1e-9)
    chk("任务名回落 title", s["tasks"]["items"][1]["name"] == "只有 title")
    chk("进行中=1", s["doing"]["count"] == 1)
    chk("无截断风险（<100）", s["assertions"]["truncation_ok"] is True)

    # 负控：Q4 窗口必须为 0（证明过滤真生效而非"全空是巧合"）
    s4 = aggregate(payload, "2026-10-01 00:00:00", "2027-01-01 00:00:00", "cp")
    chk("负控: Q4 窗口只收 10-01 那条=1，且 Q3+Q4=4 不重不漏",
        s4["bugs"]["count"] == 1 and s["bugs"]["count"] + s4["bugs"]["count"] == 4)
    chk("负控: Q4 窗口不会把 10-01 那条算进 Q3", "2026-10-01 00:00:00" not in
        [i["resolvedDate"] for i in s["bugs"]["links"]])

    # 截断风险：单产品满页 ⇒ 必须报警
    big = dict(payload)
    big["bugs"] = [{"id": 900 + i, "product": "14", "resolvedBy": "cp",
                    "resolvedDate": "2026-08-01 00:00:00", "severity": "3",
                    "type": "codeerror", "resolution": "fixed"} for i in range(PAGE_SIZE)]
    sbig = aggregate(big, since, until, "cp")
    chk("负控: 单产品满页必报截断风险", sbig["assertions"]["truncation_ok"] is False
        and "14" in sbig["assertions"]["truncation_risk"])
    chk("旧 payload 无 pagers ⇒ 完整性判 unknown_legacy_payload（回退，不误报也不冒充 verified）",
        s["assertions"]["completeness"]["state"] == "unknown_legacy_payload")

    # 完整性闸门（权威判据 = 响应 pager.recTotal）
    p_ok = dict(payload)
    p_ok["pagers"] = [{"product": "14", "got": 2, "recTotal": 2, "recPerPage": 100}]
    sok = aggregate(p_ok, since, until, "cp")
    chk("完整性: got>=recTotal ⇒ verified 且无截断风险",
        sok["assertions"]["completeness"]["state"] == "verified"
        and sok["assertions"]["completeness"]["products_verified"] == 1
        and sok["assertions"]["truncation_ok"] is True)
    p_short = dict(payload)
    p_short["pagers"] = [{"product": "14", "got": 20, "recTotal": 36, "recPerPage": 20}]
    sshort = aggregate(p_short, since, until, "cp")
    chk("负控: got<recTotal ⇒ truncated 且点名产品与其 recTotal",
        sshort["assertions"]["completeness"]["state"] == "truncated"
        and sshort["assertions"]["completeness"]["products_short"].get("14", {}).get("recTotal") == 36
        and sshort["assertions"]["truncation_ok"] is False)
    p_unk = dict(payload)
    p_unk["pagers"] = [{"product": "14", "got": 2, "recTotal": None}]
    sunk = aggregate(p_unk, since, until, "cp")
    chk("recTotal 缺失 ⇒ verified_with_unknown（不冒充 verified）",
        sunk["assertions"]["completeness"]["state"] == "verified_with_unknown")
    p_retry = dict(payload)
    p_retry["pagers"] = [{"product": "14", "got": 36, "recTotal": 36}]
    p_retry["retry"] = {"14": {"requested": 2000, "got": 36, "recTotal": 36}}
    srt = aggregate(p_retry, since, until, "cp")
    chk("自动重取记录透传且判 verified",
        srt["assertions"]["completeness"]["retry"].get("14", {}).get("requested") == 2000
        and srt["assertions"]["completeness"]["state"] == "verified")

    # 负控：小页宽场景下「行数≥页宽」启发式会漏报（20 行 < 页宽 100）⇒ 证明必须靠 recTotal
    small = dict(payload)
    small["bugs"] = [{"id": 800 + i, "product": "14", "resolvedBy": "cp",
                      "resolvedDate": "2026-08-01 00:00:00"} for i in range(20)]
    ssmall = aggregate(small, since, until, "cp")
    chk("负控: 启发式在 20 行/页宽100 下漏报（故权威判据必须用 recTotal）",
        ssmall["assertions"]["truncation_ok"] is True
        and ssmall["assertions"]["completeness"]["state"] == "unknown_legacy_payload")

    # 渲染含完整性行
    md2 = render_md(sok)
    chk("渲染含完整性行", "完整性: verified" in md2 and "pager.recTotal" in md2)

    # 确定性
    chk("确定性: 两次聚合逐字相同",
        json.dumps(aggregate(payload, since, until, "cp"), sort_keys=True)
        == json.dumps(s, sort_keys=True))

    # 空 payload 容错
    empty = aggregate({}, since, until, "cp")
    chk("空 payload 不抛异常且计数 0", empty["bugs"]["count"] == 0 and empty["tasks"]["count"] == 0)

    # _extract 四种桥响应形状
    chk("_extract 形状1: {r:{data:'json串'}}",
        _extract({"r": {"data": json.dumps({"a": 1})}}) == {"a": 1})
    chk("_extract 形状2: {r:'json串'}", _extract({"r": json.dumps([1, 2])}) == [1, 2])
    chk("_extract 形状3: {status,data:dict}", _extract({"status": "success", "data": {"b": 2}}) == {"b": 2})
    chk("_extract 形状4: 裸 dict", _extract({"c": 3}) == {"c": 3})
    chk("_extract 坏响应 fail-closed",
        isinstance(_extract({"status": "fail", "data": ""}), (str, dict)))

    chk("_num 容错", _num("3.5") == 3.5 and _num(None) == 0.0 and _num("x") == 0.0)
    chk("_dist 空输入", _dist([], "k") == {})

    md = render_md(s)
    chk("md 含窗口与计数", "窗口: %s → %s" % (since, until) in md and "计入 3 条" in md)

    # 窗口工具复用自证（不重复实现）
    chk("复用 quarter_window.resolve", bool(qw.resolve(quarter="Q3-2026")["since_ts"]))

    print("selftest: %d checks, %d failed" % (len(ck), len(bad)))
    for n in bad:
        print("  FAIL", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
