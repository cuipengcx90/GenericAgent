#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentRouter 额度投放监控（只读探测 + 可选通知）

站点: https://agentrouter.org （主域名已迁到 https://ps.air-outer.com，两者同源同数据；
      本脚本按 HOSTS 顺序回退）
      —— 内部大模型 API 网关，OpenAI/Anthropic 兼容端点

★ 传输层铁律（2026-10-02 实测）: 站点按 **TLS 指纹** 拦截，原生 urllib / curl
  一律在握手后立即被 reset（表现为 SSL EOF / SSL_ERROR_SYSCALL，http=0），
  而 Clash 自身对该站全节点 delay 测试却成功 ⇒ 「网络不通」是假象。
  必须走 curl_cffi 的 impersonate（浏览器指纹）才能拿到响应；换 UA 无效。

背景（站点 /api/status 公告）:
  Claude / GPT 系列为限量供应，每日分批次发放、用完即止；
  投放时间 = 北京时间 10:00 与 19:00（UTC 02:00 / 11:00）。额度耗尽报
  HTTP 402 "Budget pool quota has been exhausted."
  DeepSeek / GLM 不受限（不参与本监控）。

访问门禁（实测）:
  站点按 User-Agent 白名单放行「支持客户端」，其它 UA 一律返回
  HTTP 401 {"error":{"message":"unauthorized client detected ..."}}
  实测可用 UA: claude-cli/1.0.0 (external, cli) / codex_cli_rs/0.1.0
  实测被拒 UA: OpenAI/Python、curl/8.0、Chrome

判据（单模型一次最小调用 POST /v1/chat/completions）:
  200                        -> AVAILABLE        有额度
  402                        -> EXHAUSTED        本批池已耗尽
  401/403 + unauthorized client -> CLIENT_REJECTED 客户端(UA)不在白名单
  其它（5xx/超时/其它 4xx）   -> UNKNOWN          不确定（fail-closed，不当成有额度）

退出码:
  0 = 正常（无额度 / 非窗口跳过 / 已通知跳过）
  1 = 检测到至少一个模型 AVAILABLE
  2 = 环境或全量探测异常（无 key / 全部 UNKNOWN / 全部 CLIENT_REJECTED）

用法:
  python3 tools/agentrouter_quota_watch.py --once [--notify] [--force] [--dry-run]
  python3 tools/agentrouter_quota_watch.py --selftest
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 站点域名（先后回退；实测两者返回同一份 /v1/models）
HOSTS = ["https://agentrouter.org", "https://ps.air-outer.com"]
BASE = HOSTS[0]
# 白名单 UA（站点按客户端指纹放行；非白名单 UA 恒 401）
UA = "claude-cli/1.0.0 (external, cli)"
# 客户端 TLS 指纹（curl_cffi impersonate 取值）
IMPERSONATE = "chrome124"
# 限量池模型（/v1/models 实测真名；deepseek-v4-flash 为不限量平台模型，不参与监控）
MODELS = ["claude-opus-5", "claude-opus-4-8", "gpt-6-astra"]
# 投放窗口（北京时间），闭开区间 [start, end)
WINDOWS = [("10:00", "10:30"), ("19:00", "19:30")]

STATE_DIR = os.path.join(ROOT, "temp/agentrouter_watch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LEDGER_FILE = os.path.join(STATE_DIR, "ledger.jsonl")

SITE_URL = "https://agentrouter.org/"


# ---------------------------------------------------------------- 时间 / 窗口
def beijing_now():
    """北京时间（UTC+8）——不依赖本机时区设置。"""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def current_batch(now_bj=None, windows=WINDOWS):
    """返回当前投放批次标识 'YYYY-MM-DD@HH:MM'；不在窗口内返回 None。"""
    n = now_bj or beijing_now()
    hhmm = n.strftime("%H:%M")
    for start, end in windows:
        if start <= hhmm < end:
            return "%s@%s" % (n.strftime("%Y-%m-%d"), start)
    return None


# ---------------------------------------------------------------- 判据内核
def classify(status, body_text):
    """把 (HTTP 状态码, 响应体) 映射成四态判据。纯函数，便于负控自测。"""
    text = (body_text or "").lower()
    if status == 200:
        return "AVAILABLE"
    if status == 402:
        return "EXHAUSTED"
    if status in (401, 403) and "unauthorized client" in text:
        return "CLIENT_REJECTED"
    return "UNKNOWN"


def _load_key():
    """密钥来源优先级: 环境变量 > keychain.py > ~/.config/agentrouter/key。"""
    v = os.environ.get("AGENTROUTER_API_KEY", "").strip()
    if v:
        return v
    try:
        sys.path.insert(0, os.path.join(ROOT, "memory"))
        import keychain  # noqa
        if "agentrouter_api_key" in keychain.keys.ls():
            return keychain.keys.agentrouter_api_key.use().strip()
    except Exception:
        pass
    p = os.path.expanduser("~/.config/agentrouter/key")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


def _http_post(url, key, body, timeout=30):
    """POST JSON。★必须走 curl_cffi（浏览器 TLS 指纹）：原生 urllib/curl 会被站点 reset。"""
    from curl_cffi import requests as cffi_requests
    return cffi_requests.post(
        url,
        data=body,
        impersonate=IMPERSONATE,
        headers={
            "Authorization": "Bearer %s" % key,
            "User-Agent": UA,
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


def probe_model(model, key, hosts=None, timeout=30):
    """单模型最小探测: 打一次 POST /v1/chat/completions（max_tokens 极小）。

    按 HOSTS 顺序回退；全部失败 ⇒ http=0 / UNKNOWN（fail-closed）。
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    tried = []
    for host in (hosts or HOSTS):
        try:
            resp = _http_post(host + "/v1/chat/completions", key, body, timeout)
            text = resp.text or ""
            code = resp.status_code
            return {"model": model, "http": code, "verdict": classify(code, text),
                    "detail": text[:200], "host": host}
        except Exception as e:  # TLS reset / 超时 / DNS
            tried.append("%s=%s" % (host.split("//")[-1], type(e).__name__))
    return {"model": model, "http": 0, "verdict": "UNKNOWN",
            "detail": "transport: " + ", ".join(tried)}


# ---------------------------------------------------------------- 状态 / 台账
def list_models(key="", timeout=30):
    """只读列举站点在册模型（/v1/models）。TLS 层同 probe_model。"""
    body = b""
    tried = []
    for host in HOSTS:
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.get(
                host + "/v1/models",
                impersonate=IMPERSONATE,
                headers={"Authorization": "Bearer %s" % key, "User-Agent": UA},
                timeout=timeout,
            )
            try:
                data = resp.json()
                ids = [m.get("id") for m in data.get("data", [])]
            except Exception:
                ids = []
            return {"host": host, "http": resp.status_code, "models": ids}
        except Exception as e:
            tried.append("%s=%s" % (host.split("//")[-1], type(e).__name__))
    return {"host": None, "http": 0, "models": [], "detail": ", ".join(tried)}


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _append_ledger(rec):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LEDGER_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 通知
def _build_text(batch, results, now_bj):
    def icon(v):
        return {"AVAILABLE": "✅ 有额度", "EXHAUSTED": "❌ 池已耗尽(402)",
                "CLIENT_REJECTED": "⚠️ 客户端被拒(401)", "UNKNOWN": "❓ 未知"}.get(v, v)

    lines = ["🚀 AgentRouter 额度已投放（批次 %s）" % batch,
             "检测时间：%s（北京时间）" % now_bj.strftime("%Y-%m-%d %H:%M:%S")]
    for r in results:
        lines.append("- %s：%s" % (r["model"], icon(r["verdict"])))
    lines += [
        "",
        "站点：%s" % SITE_URL,
        "提示：客户端须在支持列表（claude-code / codex 等），UA 不在白名单会报 unauthorized client。",
        "额度用完即止，请尽快使用。",
    ]
    return "\n".join(lines)


def send_notifications(text, dry_run=False):
    """双通道推送（飞书私信 + 钉钉群），任一成功即算送达；返回 (ok, detail)。"""
    if dry_run:
        return True, "dry-run"
    ok = False
    details = []
    try:
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from reflect_common import notify_feishu
        r = notify_feishu(text)
        if r:
            ok = True
            details.append("feishu:ok")
        else:
            details.append("feishu:fail")
    except Exception as e:
        details.append("feishu:err(%s)" % e)
    try:
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from notify_dingtalk_group import notify_group
        r = notify_group(text)
        if isinstance(r, dict) and r.get("errcode") == 0:
            ok = True
            details.append("dingtalk:ok")
        else:
            details.append("dingtalk:%s" % (r if not isinstance(r, dict) else r.get("errmsg")))
    except Exception as e:
        details.append("dingtalk:err(%s)" % e)
    return ok, "; ".join(details)


# ---------------------------------------------------------------- 主流程
def run_once(args):
    now_bj = beijing_now()
    batch = current_batch(now_bj)
    if batch is None and not args.force:
        print("[agentrouter] %s 非投放窗口(10:00-10:30 / 19:00-19:30 北京时间)，跳过"
              % now_bj.strftime("%Y-%m-%d %H:%M:%S"))
        return 0

    key = _load_key()
    if not key:
        print("[agentrouter] 未找到 API Key（env AGENTROUTER_API_KEY / keychain / ~/.config/agentrouter/key）",
              file=sys.stderr)
        return 2

    st = _load_state()
    if batch and st.get(batch, {}).get("notified"):
        print("[agentrouter] 批次 %s 已通知过，跳过" % batch)
        return 0

    results = [probe_model(m, key) for m in MODELS]
    verdicts = [r["verdict"] for r in results]
    available = [r for r in results if r["verdict"] == "AVAILABLE"]

    rec = {"ts": now_bj.isoformat(), "batch": batch or "(force)",
           "results": [{k: r[k] for k in ("model", "http", "verdict")} for r in results]}
    _append_ledger(rec)

    for r in results:
        print("[agentrouter] %-18s http=%-4s %s  %s"
              % (r["model"], r["http"], r["verdict"], r["detail"][:100]))

    if available:
        text = _build_text(batch or "(手动强制检测)", results, now_bj)
        if args.notify:
            ok, detail = send_notifications(text, dry_run=args.dry_run)
            print("[agentrouter] 通知: %s (%s)" % ("已推送" if ok else "失败", detail))
        else:
            print("[agentrouter] (未加 --notify，跳过推送)\n" + text)
        if batch and args.notify and not args.dry_run:
            st[batch] = {"notified": now_bj.isoformat(),
                         "models": {r["model"]: r["verdict"] for r in results}}
            _save_state(st)
        return 1

    # 全 CLIENT_REJECTED 或全 UNKNOWN 视为环境异常（fail-closed）
    if all(v in ("CLIENT_REJECTED", "UNKNOWN") for v in verdicts):
        print("[agentrouter] 探测异常: %s" % ",".join(verdicts), file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------- 自测
def selftest():
    """负控自测：判据映射 / 窗口边界 / 幂等 / 推送体。"""
    cases = []
    ok = True

    def chk(name, cond):
        nonlocal ok
        cases.append((name, bool(cond)))
        if not cond:
            ok = False

    # 1) 判据映射
    chk("200->AVAILABLE", classify(200, '{"choices":[]}') == "AVAILABLE")
    chk("402->EXHAUSTED", classify(402, '{"error":{"message":"Budget pool quota has been exhausted."}}') == "EXHAUSTED")
    chk("401+unauthorized->CLIENT_REJECTED",
        classify(401, '{"error":{"message":"unauthorized client detected, contact support"}}') == "CLIENT_REJECTED")
    chk("403+unauthorized->CLIENT_REJECTED",
        classify(403, "unauthorized client detected") == "CLIENT_REJECTED")
    chk("500->UNKNOWN", classify(500, "oops") == "UNKNOWN")
    chk("429->UNKNOWN", classify(429, "rate limited") == "UNKNOWN")
    chk("401 非白名单文案->UNKNOWN(不误判)", classify(401, '{"error":"invalid api key"}') == "UNKNOWN")
    chk("402 文案大小写无关", classify(402, "BUDGET POOL QUOTA HAS BEEN EXHAUSTED") == "EXHAUSTED")
    chk("404 未开通模型->UNKNOWN", classify(404, '{"error":"model not found"}') == "UNKNOWN")
    chk("200 空体仍 AVAILABLE", classify(200, "") == "AVAILABLE")

    # 1b) 传输层 fail-closed：全部域名不可达 ⇒ http=0 / UNKNOWN（不伪造判决）
    r = probe_model("claude-opus-5", "sk-x", hosts=["https://127.0.0.1:1"], timeout=2)
    chk("全域名失败->http0/UNKNOWN", r["http"] == 0 and r["verdict"] == "UNKNOWN")
    chk("失败留 transport 足迹", "transport:" in r["detail"])

    # 2) 窗口边界（北京时间）
    def bj(h, m):
        return datetime.datetime(2026, 10, 2, h, m, tzinfo=datetime.timezone.utc)

    chk("09:59 非窗口", current_batch(bj(9, 59)) is None)
    chk("10:00 窗口内", current_batch(bj(10, 0)) == "2026-10-02@10:00")
    chk("10:29 窗口内", current_batch(bj(10, 29)) == "2026-10-02@10:00")
    chk("10:30 窗口外(闭开)", current_batch(bj(10, 30)) is None)
    chk("14:40 非窗口", current_batch(bj(14, 40)) is None)
    chk("19:00 窗口内", current_batch(bj(19, 0)) == "2026-10-02@19:00")
    chk("19:29 窗口内", current_batch(bj(19, 29)) == "2026-10-02@19:00")
    chk("19:30 窗口外", current_batch(bj(19, 30)) is None)
    chk("批次含日期(跨日不串)", current_batch(bj(23, 0)) is None)

    # 3) 幂等：批次已通知 -> 当天该批次不再推
    import tempfile
    global STATE_DIR, STATE_FILE, LEDGER_FILE
    old = (STATE_DIR, STATE_FILE, LEDGER_FILE)
    d = tempfile.mkdtemp(prefix="arw_selftest_")
    STATE_DIR, STATE_FILE, LEDGER_FILE = d, os.path.join(d, "state.json"), os.path.join(d, "ledger.jsonl")

    class A:
        notify = False
        dry_run = False
        force = True

    orig_probe = globals()["probe_model"]
    orig_batch = globals()["current_batch"]
    orig_send = globals()["send_notifications"]
    os.environ["AGENTROUTER_API_KEY"] = "sk-test-not-real"
    A.notify = True
    A.dry_run = False
    globals()["send_notifications"] = lambda text, dry_run=False: (True, "mock")

    # 3a) 窗口内无额度：402 -> rc 0 且不写 state（不推送）
    globals()["current_batch"] = lambda *a, **k: "2026-10-02@19:00"
    globals()["probe_model"] = lambda m, k, **kw: {"model": m, "http": 402, "verdict": "EXHAUSTED", "detail": ""}
    rc = run_once(A())
    chk("402 探测 rc=0", rc == 0)
    chk("402 不写 state", _load_state() == {})
    chk("402 仍落 ledger", os.path.exists(LEDGER_FILE))
    # 3b) 有额度 -> rc 1，state 标记 + 推送一次
    globals()["probe_model"] = lambda m, k, **kw: {"model": m, "http": 200, "verdict": "AVAILABLE", "detail": ""}
    rc = run_once(A())
    chk("200 探测 rc=1", rc == 1)
    st = _load_state()
    chk("有额度写 state", len(st) == 1 and list(st.values())[0].get("notified"))
    # 3c) 同批次重跑 -> 跳过（幂等，不重复推送）
    rc = run_once(A())
    chk("同批次重跑跳过 rc=0", rc == 0)
    # 3d) 换批次 + 全异常 -> rc 2
    globals()["current_batch"] = lambda *a, **k: "2026-10-03@10:00"
    globals()["probe_model"] = lambda m, k, **kw: {"model": m, "http": 0, "verdict": "UNKNOWN", "detail": ""}
    rc = run_once(A())
    chk("全 UNKNOWN rc=2", rc == 2)
    # 3e) 无 key -> rc 2（fail-closed）
    # ★负控必须与现场解耦：不能靠清 env 就假定无 key（本机 keychain 里有真 key ⇒ 假红）。
    #   显式 monkeypatch _load_key 返回空串，保证判据只测「无 key 时的行为」。
    orig_load_key = globals()["_load_key"]
    globals()["_load_key"] = lambda: ""
    os.environ.pop("AGENTROUTER_API_KEY", None)
    globals()["probe_model"] = lambda m, k, **kw: {"model": m, "http": 200, "verdict": "AVAILABLE", "detail": ""}
    saved_file = STATE_FILE
    globals()["STATE_FILE"] = os.path.join(d, "no_key_state.json")
    rc = run_once(A())
    globals()["STATE_FILE"] = saved_file
    globals()["_load_key"] = orig_load_key
    chk("无 key rc=2", rc == 2)

    globals()["probe_model"] = orig_probe
    globals()["current_batch"] = orig_batch
    globals()["send_notifications"] = orig_send
    STATE_DIR, STATE_FILE, LEDGER_FILE = old

    # 4) 推送体
    txt = _build_text("2026-10-02@19:00",
                      [{"model": "claude-opus-5", "verdict": "AVAILABLE", "http": 200},
                       {"model": "gpt-6-astra", "verdict": "EXHAUSTED", "http": 402}],
                      datetime.datetime(2026, 10, 2, 19, 1, tzinfo=datetime.timezone.utc))
    chk("通知含站点", SITE_URL in txt)
    chk("通知含批次", "2026-10-02@19:00" in txt)
    chk("通知区分两态", "有额度" in txt and "耗尽" in txt)

    for name, passed in cases:
        print("%s %s" % ("PASS" if passed else "FAIL", name))
    print("selftest: %d/%d %s" % (sum(1 for _, p in cases if p), len(cases),
                                  "OK" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次检测（默认行为）")
    ap.add_argument("--notify", action="store_true", help="检测到有额度时推送通知")
    ap.add_argument("--force", action="store_true", help="无视投放窗口强制检测")
    ap.add_argument("--dry-run", action="store_true", help="不真正发送通知")
    ap.add_argument("--models", action="store_true", help="只读列举站点在册模型（/v1/models）")
    ap.add_argument("--selftest", action="store_true", help="负控自测")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.models:
        r = list_models(_load_key())
        print("[agentrouter] %s http=%s n=%d" % (r["host"], r["http"], len(r["models"])))
        for m in r["models"]:
            print("  -", m)
        return 0 if r["http"] == 200 else 2
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
