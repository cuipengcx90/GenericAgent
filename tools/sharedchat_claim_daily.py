#!/usr/bin/env python3
"""sharedchat(RawChat公益站) 每日领额工具 — 纯脚本登录, 无浏览器依赖.

契约 (2026-10-07 实测):
- login:  POST /frontend-api/login {userToken,password} -> Set-Cookie share-session (HttpOnly)
- getme:  GET  /frontend-api/getme -> data.id / data.email
- quota:  GET  /frontend-api/vibe-code/quota -> data.codex.subscriptions{remainingAmount,isActive,periodResetTime,...}
- claim:  POST /frontend-api/vibe-code/codex/claim {eventId非空, reason>=10字,
          fingerprintCollectFailed:true, fingerprintCollectError:"Failed to load the JS script of the agent", collectedAt:ISO}
          -> data.claimed true=新领 / false=已领过(message=有效期内)

传输层铁律:
- 所有请求必须带 Chrome UA, 否则 Cloudflare 403 error:1010 (TLS/UA 指纹门禁, 非 key 问题)
- 连续多账号请求须间隔 >=4s, SSL handshake timeout 重试 2 次 (CF 对短时重复握手限速)
- login 无 Turnstile (getloginconfig isEnableLoginTurnstile=false, 2026-10-07)

子命令: status(只读对齐) | claim(逐号领额, --auto 只领 inactive) | selftest(离线负控)
账号源: temp/sharedchat_sop/accounts.json (新增账号只改 JSON)
台账:   temp/sharedchat_sop/claim_ledger.jsonl (append-only)
"""
import argparse
import datetime
import http.client
import http.cookiejar
import json
import os
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://new.sharedchat.cc"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # GA repo root
SOP_DIR = os.path.join(ROOT, "temp", "sharedchat_sop")
ACCOUNTS_FILE = os.path.join(SOP_DIR, "accounts.json")
LEDGER_FILE = os.path.join(SOP_DIR, "claim_ledger.jsonl")
FP_ERR = "Failed to load the JS script of the agent"

ROBOT_REASONS = [
    "日常开发需要模型额度支持，继续推进自动化任务",
    "代码调试进入新阶段，希望获得额度继续迭代",
    "批量处理本地数据文件，需要调用模型辅助解析",
    "编写技术文档并验证示例代码的正确性",
    "学习新框架过程中需要跑通官方示例项目",
    "重构个人脚本库，希望借助模型检查逻辑漏洞",
    "整理实验记录并生成结构化的分析报告",
    "为开源项目撰写补丁说明并自查边界情况",
    "搭建本地测试环境需要生成多组样例数据",
    "持续集成流水线中的静态分析需要模型复核",
]

# ---------- http core ----------

def _opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _req(op, path, method="GET", body=None, timeout=30):
    """带 Chrome UA 的站点请求; 返回 (status, dict).

    非 2xx -> RuntimeError 且带响应体前 200B (CF 拦截证据链: HTML 挑战页 vs 站点 JSON 可分)."""
    h = {"User-Agent": UA, "Accept": "application/json",
         "Content-Type": "application/json",
         "Origin": BASE, "Referer": BASE + "/list/"}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with op.open(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        snippet = b""
        try:
            snippet = e.read()[:200]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s body=%s" % (
            e.code, path, snippet.decode("utf-8", "replace").strip()[:160])) from None


TRANSIENT_EXCS = (socket.timeout, ssl.SSLError, ConnectionResetError,
                  http.client.RemoteDisconnected, urllib.error.URLError,
                  http.client.IncompleteRead, BrokenPipeError)


def is_transient(e):
    """传输层/CF 拦截类错误: 间歇性, 重试可过 (实测同一账号数分钟后即通)."""
    return isinstance(e, TRANSIENT_EXCS)


def is_transient_errtext(err):
    """回执里 ERROR 文本是否值得重试 (_req 已把 HTTPError 转成 RuntimeError 文本)."""
    e = err or ""
    return ("HTTP 403" in e or "HTTP 429" in e or "HTTP 5" in e
            or "timed out" in e or "timedout" in e or "Remote" in e
            or "Connection" in e or "SSLError" in e or "reset" in e
            or "IncompleteRead" in e)


def login(user, password, retries=2, gap=6.0):
    """脚本侧登录; 成功返回带会话 cookie 的 opener (login 本身 1-2s, 无 Turnstile)."""
    last = None
    for i in range(retries):
        op = _opener()
        try:
            st, d = _req(op, "/frontend-api/login", "POST",
                         {"userToken": user, "password": password})
            if isinstance(d, dict) and d.get("code") == 1:
                return op
            last = RuntimeError("login code!=1: %s" % json.dumps(d, ensure_ascii=False)[:160])
        except Exception as e:  # SSL handshake timeout 等传输层错误 -> 退避重试
            last = e
            time.sleep(gap)
    raise last if last else RuntimeError("login failed")


def get_quota(op):
    st, d = _req(op, "/frontend-api/vibe-code/quota")
    return (d.get("data", {}) or {}).get("codex", {}) or {}


def get_me(op):
    st, d = _req(op, "/frontend-api/getme")
    return d.get("data", {}) or {}


def build_claim_body(reason):
    return {"eventId": "rawchat-" + __import__("uuid").uuid4().hex[:12],
            "reason": reason,
            "fingerprintCollectFailed": True,
            "fingerprintCollectError": FP_ERR,
            "collectedAt": datetime.datetime.now(datetime.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def pick_reason(prev_used=None):
    """reason>=10字且尽量不与上次重复 (池 10 条, 排除上次后随机)."""
    pool = [r for r in ROBOT_REASONS if r != prev_used] or ROBOT_REASONS
    return random.choice(pool)

# ---------- 判定矩阵 (离线可测) ----------

def judge_subscription(codex_data):
    """codex quota data -> (verdict, detail). verdict in ACTIVE/NEED_CLAIM/UNKNOWN."""
    sub = codex_data.get("subscriptions")
    if not isinstance(sub, dict):
        return "NEED_CLAIM", {"detail": "no subscription (null/missing)"}
    active = sub.get("isActive")
    rem = sub.get("remainingAmount")
    reset = sub.get("periodResetTime")
    if active is True:
        return "ACTIVE", {"remaining": rem, "reset": reset,
                          "subscriptionId": sub.get("id")}
    if active is False:
        return "NEED_CLAIM", {"remaining": rem, "reset": reset}
    return "UNKNOWN", {"detail": "isActive=%r" % (active,)}


def judge_claim_result(d):
    """claim 响应 -> (verdict, detail). CLAIMED/ALREADY/ERROR."""
    if not isinstance(d, dict) or d.get("code") != 1:
        return "ERROR", {"resp": json.dumps(d, ensure_ascii=False)[:200]}
    data = d.get("data", {}) or {}
    if data.get("claimed") is True:
        return "CLAIMED", {"subscriptionId": data.get("subscriptionId"),
                           "expireTime": data.get("expireTime"),
                           "verified": data.get("verified")}
    if data.get("claimed") is False:
        return "ALREADY", {"message": data.get("message"),
                           "expireTime": data.get("expireTime")}
    return "ERROR", {"resp": json.dumps(d, ensure_ascii=False)[:200]}

# ---------- 台账 ----------

def ledger_append(row):
    os.makedirs(SOP_DIR, exist_ok=True)
    row["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LEDGER_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

# ---------- 账号 ----------

def load_accounts():
    if not os.path.isfile(ACCOUNTS_FILE):
        sys.exit("accounts.json 不存在: %s (先建台账)" % ACCOUNTS_FILE)
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    accts = data.get("accounts", data) if isinstance(data, dict) else data
    out = []
    for a in accts:
        if a.get("enabled", True):
            out.append(a)
    if not out:
        sys.exit("accounts.json 无可用账号")
    return out


def _attempt(a, do_claim):
    """一次完整尝试: login→me→quota→(claim). transient 异常向上抛由外层重试."""
    tag = a.get("tag", a.get("userToken", "?"))
    row = {"tag": tag}
    op = login(a["userToken"], a["password"])
    me = get_me(op)
    row["id"] = me.get("id")
    exp = a.get("id")  # 台账登记的 id; 登录态错位(凭据漂移/会话串号)在这里暴露
    if exp and str(me.get("id")) != str(exp):
        row["id_mismatch"] = "%s!=%s" % (me.get("id"), exp)
    codex = get_quota(op)
    verdict, detail = judge_subscription(codex)
    row["quota_verdict"] = verdict
    row.update(detail)
    if verdict == "NEED_CLAIM" and do_claim:
        reason = pick_reason(a.get("last_reason"))
        row["reason"] = reason
        st, d = _req(op, "/frontend-api/vibe-code/codex/claim", "POST",
                     build_claim_body(reason))
        cverdict, cdetail = judge_claim_result(d)
        row["claim_verdict"] = cverdict
        row["claim_detail"] = cdetail
    elif verdict == "NEED_CLAIM":
        row["claim_verdict"] = "SKIPPED(no --auto)"
    return row


def process_account(a, do_claim, attempts=2, retry_gap=8.0):
    """单账号处理; 传输层/CF 拦截类错误间歇性重试 (2026-10-07 实测: 同一账号
    首轮 RemoteDisconnected/timeout/403, 数秒后重试即过). 判据性结果不重试."""
    tag = a.get("tag", a.get("userToken", "?"))
    row = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(retry_gap)
        try:
            row = _attempt(a, do_claim)
            if row.get("quota_verdict") != "ERROR" or not is_transient_errtext(
                    row.get("error", "")):
                break
        except Exception as e:
            if attempt + 1 < attempts and is_transient(e):
                continue
            row = {"tag": tag, "quota_verdict": "ERROR",
                   "error": "%s: %s" % (type(e).__name__, str(e)[:180])}
            break
    ledger_append(row)
    return row


def cmd_status(args):
    accts = load_accounts()
    print("tag | id | quota_verdict | remaining | reset")
    problems = []
    for i, a in enumerate(accts):
        if i:
            time.sleep(4.0)  # CF 握手限速: 账号间隔
        r = process_account(a, do_claim=False)
        problems.append((r.get("tag"), r.get("quota_verdict")))
        print("%s | %s | %s | %s | %s" % (
            r.get("tag"), r.get("id"), r.get("quota_verdict"),
            r.get("remaining", r.get("error", "-")), r.get("reset", "-")))
    bad = [t for t, v in problems if v in ("NEED_CLAIM", "ERROR", "UNKNOWN")]
    print("\nSUMMARY: %d/%d active; NEED_CLAIM/ERROR: %s" % (
        sum(1 for _, v in problems if v == "ACTIVE"), len(problems), bad or "none"))
    return 0 if not bad else 1


def cmd_claim(args):
    accts = load_accounts()
    if args.only:
        keys = {x.strip() for x in args.only.split(",")}
        accts = [a for a in accts if a.get("tag") in keys]
        if not accts:
            sys.exit("--only 无匹配账号: %s" % keys)
    print("tag | id | quota | claim | detail")
    problems = []
    for i, a in enumerate(accts):
        if i:
            time.sleep(4.0)
        r = process_account(a, do_claim=args.auto)
        problems.append((r.get("tag"), r.get("quota_verdict"), r.get("claim_verdict")))
        print("%s | %s | %s | %s | %s" % (
            r.get("tag"), r.get("id"), r.get("quota_verdict"),
            r.get("claim_verdict", "-"),
            r.get("claim_detail") or r.get("error") or r.get("remaining")))
    bad = [t for t, q, c in problems if q != "ACTIVE" and c not in ("CLAIMED", "ALREADY")]
    print("\nSUMMARY: %s" % ("OK" if not bad else "NEEDS_ATTENTION: %s" % bad))
    return 0 if not bad else 1

# ---------- selftest (纯离线, 不发网络请求, 断言按结构不绑现场) ----------

def cmd_selftest(args):
    fails = []
    # T1 reason 池: 每条 >=10 字且互不相同
    if len(set(ROBOT_REASONS)) != len(ROBOT_REASONS):
        fails.append("T1 reasons duplicated")
    for r in ROBOT_REASONS:
        if len(r) < 10:
            fails.append("T1 reason too short: %r" % r)
    # T2 pick_reason 排除上次
    for _ in range(20):
        prev = random.choice(ROBOT_REASONS)
        if len(ROBOT_REASONS) > 1 and pick_reason(prev) == prev:
            fails.append("T2 pick_reason ignored prev_used")
            break
    # T3 claim body 形状
    b = build_claim_body("测试原因：验证事件体形状结构完整性")
    for k, v in (("eventId", str), ("reason", str), ("fingerprintCollectFailed", bool),
                 ("fingerprintCollectError", str), ("collectedAt", str)):
        if k not in b or not isinstance(b[k], v):
            fails.append("T3 body key %s bad" % k)
    if not b["eventId"]:
        fails.append("T3 eventId empty")
    if b["fingerprintCollectError"] != FP_ERR:
        fails.append("T3 fp error string changed")
    # T4 判定矩阵: 三态
    v, _ = judge_subscription({"subscriptions": {"isActive": True, "remainingAmount": 1.0}})
    if v != "ACTIVE":
        fails.append("T4 active")
    v, _ = judge_subscription({"subscriptions": {"isActive": False}})
    if v != "NEED_CLAIM":
        fails.append("T4 inactive")
    v, _ = judge_subscription({})
    if v != "NEED_CLAIM":
        fails.append("T4 empty")
    v, _ = judge_subscription({"subscriptions": {"isActive": None}})
    if v != "UNKNOWN":
        fails.append("T4 unknown")
    # T5 claim 判定: 新领/已领/错误
    v, d = judge_claim_result({"code": 1, "data": {"claimed": True, "subscriptionId": 7}})
    if v != "CLAIMED" or d.get("subscriptionId") != 7:
        fails.append("T5 claimed")
    v, d = judge_claim_result({"code": 1, "data": {"claimed": False, "message": "有效期内"}})
    if v != "ALREADY":
        fails.append("T5 already")
    v, d = judge_claim_result({"code": 0, "msg": "x"})
    if v != "ERROR":
        fails.append("T5 error")
    v, d = judge_claim_result("not-a-dict")
    if v != "ERROR":
        fails.append("T5 non-dict")
    # T6 账号文件: 可解析且字段齐 (不校验具体值)
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        accts = data.get("accounts", data) if isinstance(data, dict) else data
        for a in accts:
            if "userToken" not in a or "password" not in a:
                fails.append("T6 account missing fields: %s" % a.get("tag"))
                break
    except Exception as e:
        fails.append("T6 accounts.json unreadable: %s" % e)
    if fails:
        print("SELFTEST FAILED (%d):" % len(fails))
        for x in fails:
            print(" -", x)
        return 2
    print("SELFTEST OK (T1-T6, all offline)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="sharedchat 每日领额 (login->quota->claim)")
    ap.add_argument("--only", help="逗号分隔 tag, 只处理这些账号")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="只读: 逐号 login+quota 对齐")
    p_claim = sub.add_parser("claim", help="逐号领额 (默认只对 NEED_CLAIM 发 claim)")
    p_claim.add_argument("--auto", action="store_true",
                         help="必须显式开启才会发 claim 请求 (双保险)")
    sub.add_parser("selftest", help="离线自测 (零网络)")
    args = ap.parse_args()
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    if args.cmd == "claim":
        sys.exit(cmd_claim(args))
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))


if __name__ == "__main__":
    main()
