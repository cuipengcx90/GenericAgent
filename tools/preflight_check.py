#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight_check —— 定时任务「前置条件可用性」探针层（L0，只读）

问题（R475 / TODO L185）
------------------------
R467 回测显示既有四层判据（产物 mtime / 结构 / 运行缺席 / 内容指纹）对
「前置条件失效型」任务是系统性盲的：任务照常启动、照常落 done 报告，只是从某天起
永远拿不到数据 —— birdie 2026-08-08 起站点 502 / haisou 2026-09-25 起 SSLEOF /
x_ai 2026-08-18 起无 x.com 会话 / linuxdo 2026-09-27 起登录态丢失。
本器把这些「任务开跑前就必须成立」的条件做成可复跑探针，补在判据链最前面（L0）。

契约
----
探针三态：
  PASS          条件成立
  FAIL          条件不成立（任务必不会产出 ⇒ 聚合 ALERT）
  UNVERIFIABLE  无法判定（环境不支持 / 依赖缺失 / 超时）—— ★不是 PASS
聚合：
  任一 FAIL            ⇒ verdict=ALERT
  无 FAIL 但有 UNVER   ⇒ verdict=WARN
  全 PASS              ⇒ verdict=OK 且 health=False
★ 铁律：全 PASS **不得判健康**。反例（codex 复核点）：x.com 的会话 cookie 在库里存在，
  但 x_ai 自 2026-08-17 起零产出 —— 「前置可用」只排除一类原因，不证明任务会产出。

层外边界（显式声明，避免被读成「全覆盖」）
  1. 只判「前置条件是否成立」，不判产出是否正常（产出面看 R411/R457/R464 三层）；
  2. CDP 标签 id 失效形态（报告里写作 CDP_TABID）在浏览器标签仍存在时发生，本层不可见；
  3. Chrome 未运行 ⇒ 标签类探针 UNVERIFIABLE，本器**不代启动**任何 GUI；
  4. 站点对探针的响应可能与其对任务请求的响应不同（探针只断言「端点在答/可达」）。

只读边界
--------
* Chrome Cookies：只拷副本（连 -wal/-shm）→ 只 `SELECT name,host_key` → 绝不 `SELECT value`；
* 凭据：只判存在性；credential_entry 委托 tools/credential_surface.py 的六探针（零重写）；
* 不启动 GUI、不写仓内文件（--json 只打印或写调用方显式指定的路径）。

用法
----
  python3 tools/preflight_check.py probe [--target ID] [--json] [--no-live]
  python3 tools/preflight_check.py replay [--cases FILE] [--done-dir DIR] [--strict] [--json]
  python3 tools/preflight_check.py check            # 声明面严格校验（fail-closed）
  python3 tools/preflight_check.py cases            # 打印回测用例表
  python3 tools/preflight_check.py proof            # 只读自证（跑前后语料 sha256 一致）
  python3 tools/preflight_check.py --selftest
退出码：0 正常 / 1 聚合为 ALERT 或回测未覆盖 / 2 声明面（registry/cases）不合法
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GA = os.path.dirname(HERE)
# ★登记面单一来源 = 判据表 tools/freshness_criteria.json 的 "preflight" 段（R475）；
#   独立的 preflight_targets.json 已废弃（避免同一登记存在两处）。
DEFAULT_REGISTRY = os.path.join(HERE, "freshness_criteria.json")
DEFAULT_REGISTRY_KEY = "preflight"
DEFAULT_CASES = os.path.join(HERE, "preflight_cases.json")
DEFAULT_DONE = os.path.join(GA, "sche_tasks", "done")
CRED_DECL = os.path.join(GA, "memory", "credential_surface", "credentials.json")
CHROME_ROOT = os.path.expanduser("~/Library/Application Support/Google/Chrome")

VERDICTS = ("PASS", "FAIL", "UNVERIFIABLE")
LEVEL_ORDER = {"OK": 0, "INFO": 1, "WARN": 2, "ALERT": 3}
# 「端点在答」默认放宽到 4xx：403/404 说明服务端活着（真·不可达是传输层失败）
ALIVE_STATUS = [200, 201, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405, 422]

NOTE_OK = "前置可用≠产出正常（不得据此判健康）"


# ------------------------------------------------------------ 基础工具

def fmt_ts(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts else time.time()))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verdict(v):
    assert v in VERDICTS, v
    return v


def _res(kind, verdict, detail, **extra):
    out = {"kind": kind, "verdict": _verdict(verdict), "detail": detail}
    out.update(extra)
    return out


# ------------------------------------------------------------ 五类探针

def probe_chrome_tabs(params):
    """Chrome 是否开着匹配 URL 的标签（不启动 GUI；不依赖 CDP 桥）。"""
    match = params.get("match")
    if not match:
        return _res("chrome_tabs", "UNVERIFIABLE", "params.match 缺失")
    need = int(params.get("min_count", 1))
    timeout_s = int(params.get("timeout_s", 20))
    if sys.platform != "darwin":
        return _res("chrome_tabs", "UNVERIFIABLE", "非 macOS，osascript 不可用")
    if subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True).returncode != 0:
        return _res("chrome_tabs", "UNVERIFIABLE", "Chrome 未运行（不代启动）",
                    matched=0, total=0)
    script = ('tell application "Google Chrome"\n'
              'set out to ""\n'
              'repeat with w in windows\n'
              'repeat with t in tabs of w\n'
              'set out to out & (URL of t) & "\\n"\n'
              'end repeat\n'
              'end repeat\n'
              'return out\n'
              'end tell')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return _res("chrome_tabs", "UNVERIFIABLE", "osascript 超时 %ds" % timeout_s)
    except Exception as e:                                    # noqa: BLE001
        return _res("chrome_tabs", "UNVERIFIABLE", "osascript 调用失败: %s" % type(e).__name__)
    if r.returncode != 0:
        return _res("chrome_tabs", "UNVERIFIABLE",
                    "osascript rc=%d: %s" % (r.returncode, (r.stderr or "").strip()[:120]))
    urls = [u.strip() for u in (r.stdout or "").split("\n") if u.strip()]
    hits = [u for u in urls if match in u]
    verdict = "PASS" if len(hits) >= need else "FAIL"
    return _res("chrome_tabs", verdict,
                "Chrome 标签 %d 个，URL 含 %r 的 %d 个（need>=%d）" % (len(urls), match, len(hits), need),
                matched=len(hits), total=len(urls))


def _chrome_profiles(params):
    root = params.get("browser_root") or CHROME_ROOT
    if not os.path.isdir(root):
        return root, []
    out = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and (name == "Default" or name.startswith("Profile")):
            out.append(d)
    return root, out


def probe_cookie_present(params):
    """Cookies 库里是否存在某域下的某个 cookie 名（**多 profile** 扫描；绝不读 value）。

    边界：与 tools/credential_surface.py 的 browser_cookie 探针不同 —— 后者只查单个
    profile（Default），而本机 16 个 profile 目录中有 10 个含 Cookies 库（R437/R423 实测），
    linux.do 登录态历史上就出现在非 Default profile。单 profile 会对多 profile 场景假 FAIL。
    """
    names = params.get("names") or ([params["name"]] if params.get("name") else [])
    if not names:
        return _res("cookie_present", "UNVERIFIABLE", "params.names 缺失")
    domain = params.get("domain", "")
    root, profiles = _chrome_profiles(params)
    with_db = [p for p in profiles if os.path.exists(os.path.join(p, "Cookies"))]
    if not with_db:
        return _res("cookie_present", "UNVERIFIABLE",
                    "未找到任何 Chrome Cookies 库（%s）" % root, profiles=0)
    tmp = tempfile.mkdtemp(prefix="preflight_cookie_")
    hits, unver, scanned = [], [], 0
    try:
        for p in with_db:
            src = os.path.join(p, "Cookies")
            copy = os.path.join(tmp, os.path.basename(p) + ".db")
            try:
                shutil.copy2(src, copy)                       # RULES#17：连 -wal/-shm
                for ext in ("-wal", "-shm"):
                    if os.path.exists(src + ext):
                        shutil.copy2(src + ext, copy + ext)
                con = sqlite3.connect("file:%s?mode=ro" % copy, uri=True)
                # 只取 name/host_key 两列
                rows = con.execute(
                    "SELECT name, host_key FROM cookies WHERE name IN (%s)"
                    % ",".join("?" * len(names)), tuple(names)).fetchall()
                con.close()
                scanned += 1
                for nm, hk in rows:
                    if not domain or hk.endswith(domain) or hk.lstrip(".") == domain.lstrip("."):
                        hits.append({"profile": os.path.basename(p), "name": nm, "host": hk})
            except Exception as e:                            # noqa: BLE001 — fail-closed
                unver.append("%s:%s" % (os.path.basename(p), type(e).__name__))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if hits:
        return _res("cookie_present", "PASS",
                    "命中 %d 条（profile=%s）" % (len(hits), ",".join(sorted({h["profile"] for h in hits}))),
                    hits=hits, scanned=scanned)
    if scanned == 0:
        return _res("cookie_present", "UNVERIFIABLE",
                    "全部 Cookies 库读取失败: %s" % ",".join(unver[:3]), scanned=0)
    return _res("cookie_present", "FAIL",
                "扫描 %d 个 profile 的 Cookies，未见 %s 域下的 %s" % (scanned, domain or "*", names),
                scanned=scanned, hits=[])


def probe_http_endpoint(params):
    """端点是否「在答」（传输层失败 ⇒ FAIL；状态码不在 expect_status ⇒ FAIL）。"""
    url = params.get("url")
    if not url:
        return _res("http_endpoint", "UNVERIFIABLE", "params.url 缺失")
    method = (params.get("method") or "GET").upper()
    timeout_s = float(params.get("timeout_s", 8))
    expect = params.get("expect_status")
    if expect is None:
        return _res("http_endpoint", "UNVERIFIABLE", "params.expect_status 缺失（须显式声明）")
    expect = set(int(x) for x in expect)
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "ga-preflight/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            got_bytes = len(resp.read(2048))
    except urllib.error.HTTPError as e:
        status, got_bytes = e.code, 0
    except Exception as e:                                    # noqa: BLE001
        return _res("http_endpoint", "FAIL", "传输层失败: %s" % type(e).__name__,
                    url=url, err=type(e).__name__)
    if status in expect:
        return _res("http_endpoint", "PASS", "%s %s ⇒ HTTP %d（在答）" % (method, url, status),
                    url=url, status=status)
    return _res("http_endpoint", "FAIL",
                "%s %s ⇒ HTTP %d 不在 expect_status %s 内" % (method, url, status, sorted(expect)),
                url=url, status=status)


def probe_path_exists(params):
    """任务依赖的仓内文件/目录是否存在（相对 GA 根或绝对路径）。"""
    paths = params.get("paths") or ([params["path"]] if params.get("path") else [])
    if not paths:
        return _res("path_exists", "UNVERIFIABLE", "params.paths 缺失")
    missing = []
    for p in paths:
        q = p if os.path.isabs(p) else os.path.join(GA, p)
        if not os.path.exists(q):
            missing.append(p)
    if missing:
        return _res("path_exists", "FAIL", "缺失 %d/%d：%s" % (len(missing), len(paths), missing),
                    missing=missing)
    return _res("path_exists", "PASS", "%d 条依赖路径齐备" % len(paths))


def _load_cred_module():
    p = os.path.join(HERE, "credential_surface.py")
    if not os.path.exists(p):
        return None, "tools/credential_surface.py 不存在"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_preflight_cred", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as e:                                    # noqa: BLE001
        return None, "credential_surface 加载失败: %s" % type(e).__name__


def probe_credential_entry(params):
    """凭证**存在性**（不读值）：委托 tools/credential_surface.py 的六探针（零重写）。"""
    entry_id = params.get("entry_id")
    if not entry_id:
        return _res("credential_entry", "UNVERIFIABLE", "params.entry_id 缺失")
    decl_path = params.get("decl") or CRED_DECL
    if not os.path.exists(decl_path):
        return _res("credential_entry", "UNVERIFIABLE", "声明面不存在: %s" % decl_path)
    mod, err = _load_cred_module()
    if mod is None:
        return _res("credential_entry", "UNVERIFIABLE", err)
    try:
        decl = json.load(open(decl_path, encoding="utf-8"))
    except Exception as e:                                    # noqa: BLE001
        return _res("credential_entry", "UNVERIFIABLE", "声明面不可解析: %s" % type(e).__name__)
    entry = next((e for e in decl.get("entries", []) if e.get("id") == entry_id), None)
    if entry is None:
        return _res("credential_entry", "FAIL", "声明面里没有 id=%s（声明与调用不一致）" % entry_id)
    kind = entry.get("kind")

    class _Ctx(object):                                       # duck-type credential_surface.Ctx
        root = GA
        tmp = tempfile.mkdtemp(prefix="preflight_cred_")
        fixtures = None

        @staticmethod
        def resolve(rel):
            return rel if os.path.isabs(rel) else os.path.join(GA, rel)

        def fx(self, name):
            return os.path.join(self.tmp, name)

    fn = mod.PROBES.get(kind)
    if fn is None:
        return _res("credential_entry", "UNVERIFIABLE", "未知凭证 kind=%s" % kind)
    try:
        state, detail = fn(entry.get("locator") or {}, _Ctx)
    except Exception as e:                                    # noqa: BLE001
        return _res("credential_entry", "UNVERIFIABLE", "探针异常: %s" % type(e).__name__)
    mapping = {"PRESENT": "PASS", "MISSING": "FAIL", "UNVERIFIABLE": "UNVERIFIABLE"}
    return _res("credential_entry", mapping.get(state, "UNVERIFIABLE"),
                "%s[%s/%s]: %s" % (entry_id, kind, state, detail),
                credential=entry_id, cred_state=state)


PROBE_KINDS = {
    "chrome_tabs": probe_chrome_tabs,
    "cookie_present": probe_cookie_present,
    "http_endpoint": probe_http_endpoint,
    "path_exists": probe_path_exists,
    "credential_entry": probe_credential_entry,
}


def run_probe(spec):
    """执行单条探针规格；任何异常都收敛成 UNVERIFIABLE（绝不冒泡、绝不误判 PASS）。"""
    kind = (spec or {}).get("kind")
    fn = PROBE_KINDS.get(kind)
    if fn is None:
        return _res(str(kind), "UNVERIFIABLE", "未知探针 kind=%s" % kind)
    t0 = time.time()
    try:
        out = fn(spec.get("params") or {})
    except Exception as e:                                    # noqa: BLE001 — fail-closed
        out = _res(kind, "UNVERIFIABLE", "探针异常: %s: %s" % (type(e).__name__, e))
    out["ms"] = int((time.time() - t0) * 1000)
    if spec.get("label"):
        out["label"] = spec["label"]
    return out


# ------------------------------------------------------------ 聚合

def aggregate(results):
    vs = [r["verdict"] for r in results]
    if not vs:
        return {"verdict": "UNVERIFIABLE", "health": False, "note": "无探针（未登记=未知，不是健康）",
                "counts": {}, "caveat": NOTE_OK}
    counts = {v: vs.count(v) for v in VERDICTS}
    if counts.get("FAIL"):
        return {"verdict": "ALERT", "health": False, "counts": counts,
                "note": "前置条件不成立 ⇒ 任务必不会产出", "caveat": NOTE_OK}
    if counts.get("UNVERIFIABLE"):
        return {"verdict": "WARN", "health": False, "counts": counts,
                "note": "存在无法判定的前置条件（未知≠成立）", "caveat": NOTE_OK}
    return {"verdict": "OK", "health": False, "counts": counts,
            "note": NOTE_OK, "caveat": NOTE_OK}


def run_probes(registry, only=None, live=True):
    rows = []
    for t in registry.get("targets", []):
        if only and t.get("id") not in only:
            continue
        probes = t.get("probes") or []
        results = [run_probe(s) for s in probes] if live else [
            _res(s.get("kind"), "UNVERIFIABLE", "--no-live 跳过") for s in probes]
        agg = aggregate(results)
        rows.append({"id": t.get("id"), "label": t.get("label", ""), "limits": t.get("limits", ""),
                     "probes": results, "agg": agg})
    return rows


# ------------------------------------------------------------ 回测（replay）

def family_scan(task_id, tokens, done_dir):
    """★按族限定扫描（family = 任务 id 后缀），返回 [(date, file, line_no, token)]。

    必须族内扫描：报告正文会**回述**别族故障（实测 birdie 报告里的 SSLEOFError 污染
    haisou 的命中扫描），跨族 glob 会把首次命中日判错。
    """
    hits = []
    pat = os.path.join(done_dir, "*_%s.md" % task_id)
    files = sorted(glob.glob(pat))
    for f in files:
        day = os.path.basename(f)[:10]
        try:
            lines = open(f, encoding="utf-8", errors="replace").read().split("\n")
        except OSError:
            continue
        for i, ln in enumerate(lines, 1):
            for tok in tokens:
                if tok in ln:
                    hits.append({"date": day, "file": os.path.basename(f), "line": i, "token": tok})
    hits.sort(key=lambda h: (h["date"], h["file"], h["line"]))
    return files, hits


def _days(a, b):
    import datetime
    da = datetime.date(*[int(x) for x in a.split("-")])
    db = datetime.date(*[int(x) for x in b.split("-")])
    return (da - db).days


def replay(cases, done_dir=DEFAULT_DONE, live=True, registry=None):
    rows = []
    by_id = {t.get("id"): t for t in (registry or {}).get("targets", [])}
    for c in cases.get("cases", []):
        ctype = c.get("type", "evidence")
        row = {"id": c.get("id"), "type": ctype, "target": c.get("target"),
               "onset": c.get("onset"), "expect": c.get("expect")}
        if ctype == "negative_control":
            # ★「全 PASS 不得判健康」：合成一组必 PASS 的探针，聚合必须是 OK 且 health=False
            spec = c.get("probes") or [{"kind": "path_exists", "params": {"paths": ["tools/preflight_check.py"]}}]
            results = [run_probe(s) for s in spec]
            agg = aggregate(results)
            row["agg"] = agg
            ok = (agg["verdict"] == "OK" and agg["health"] is False and bool(agg["caveat"]))
            row["status"] = "NC_OK" if ok else "NC_VIOLATION"
            row["note"] = "全 PASS ⇒ verdict=OK 但 health=False（不得判健康）"
            rows.append(row)
            continue
        if ctype == "coverage":
            # 目标在册但**当天**无任何在册探针 ⇒ 必须显式记 COVERAGE_GAP，不得静默
            tgt = by_id.get(c.get("target")) or {}
            row["registered_probes"] = [s.get("kind") for s in (tgt.get("probes") or [])]
            row["status"] = "COVERAGE_OK" if row["registered_probes"] else "COVERAGE_GAP"
            rows.append(row)
            continue
        # evidence 型：族内扫描 recorded token ⇒ 首次命中日 vs onset
        files, hits = family_scan(c.get("target"), c.get("recorded_tokens") or [], done_dir)
        grace = int(c.get("grace_days", 1))
        if not files:
            row["status"] = "NO_DATA"
            row["note"] = "族内无 done 报告（语料缺失，不得当通过）"
            rows.append(row)
            continue
        if not hits:
            row["status"] = "NO_HIT"
            row["note"] = "族内 %d 份报告均无 recorded_tokens" % len(files)
            rows.append(row)
            continue
        first = hits[0]
        delay = _days(first["date"], c["onset"])
        row.update({"first_hit": first["date"], "first_file": first["file"], "first_line": first["line"],
                    "token": first["token"], "delay_days": delay, "hits": len(hits),
                    "reports_scanned": len(files)})
        if delay < 0:
            row["status"] = "FIRE_EARLY"
            row["note"] = "首次命中早于 onset %d 天（同属该坏窗口的起点前）" % (-delay)
        elif delay <= grace:
            row["status"] = "FIRE_ON_TIME"
        else:
            row["status"] = "FIRE_LATE"
            row["note"] = "迟至 %d 天（grace=%d）" % (delay, grace)
        if live and c.get("live_spec") is not None:
            row["live"] = run_probe(c["live_spec"])
        rows.append(row)
    covered = [r for r in rows if r["status"] in ("FIRE_ON_TIME", "FIRE_EARLY", "NC_OK", "COVERAGE_OK")]
    bad = [r for r in rows if r not in covered]
    pstat = {}
    for r in bad:
        pstat[r["status"]] = pstat.get(r["status"], 0) + 1
    return {"schema": "ga.preflight_replay/v1", "ts": fmt_ts(), "done_dir": done_dir,
            "cases": len(rows), "covered": len(covered), "problem": len(bad),
            "problem_status": pstat,
            "problem_ids": [r["id"] for r in bad], "rows": rows}


# ------------------------------------------------------------ 声明面校验 / 自证


def load_registry(path, key=DEFAULT_REGISTRY_KEY):
    """读登记面：优先判据表里的 `key` 段（单一来源）；顶层带 targets 的独立注册表仍兼容。"""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    reg = doc.get(key)
    if reg is None:
        if isinstance(doc.get("targets"), list):
            return doc
        raise ValueError("文件缺 %r 段（且顶层无 targets）: %s" % (key, path))
    if not isinstance(reg, dict) or not isinstance(reg.get("targets"), list):
        raise ValueError("%r 段必须含 targets 列表: %s" % (key, path))
    return reg


def l0_map(registry, only=None, live=True):
    """供 task_freshness 接线用：{target_id: {verdict, note, counts, limits, probes}}。

    注意 verdict∈ALERT/WARN/OK：OK 也**不是**健康（aggregate 恒置 health=False）。
    """
    out = {}
    for r in run_probes(registry, only=only, live=live):
        out[r["id"]] = {"verdict": r["agg"]["verdict"], "note": r["agg"]["note"],
                        "counts": r["agg"]["counts"], "limits": r.get("limits", ""),
                        "probes": [{"kind": p.get("kind"), "verdict": p.get("verdict"),
                                    "detail": p.get("detail"), "ms": p.get("ms", 0)}
                                   for p in r["probes"]]}
    return out


def check_registry(registry):
    """fail-closed 校验：未知 kind、缺必填 params、重复 id 一律报错。"""
    errs, ids = [], set()
    req = {"chrome_tabs": ["match"], "cookie_present": ["names"], "http_endpoint": ["url", "expect_status"],
           "path_exists": ["paths"], "credential_entry": ["entry_id"]}
    if not registry.get("targets"):
        errs.append("registry.targets 为空")
    for t in registry.get("targets", []):
        tid = t.get("id")
        if not tid:
            errs.append("target 缺 id")
            continue
        if tid in ids:
            errs.append("重复 target id: %s" % tid)
        ids.add(tid)
        if not t.get("probes"):
            errs.append("%s 无 probes" % tid)
        for s in t.get("probes") or []:
            k = s.get("kind")
            if k not in PROBE_KINDS:
                errs.append("%s 未知探针 kind=%s" % (tid, k))
                continue
            for key in req.get(k, []):
                if not (s.get("params") or {}).get(key):
                    errs.append("%s 探针 %s 缺 params.%s" % (tid, k, key))
    return errs


def check_cases(cases):
    errs = []
    seen = set()
    allowed = ("evidence", "negative_control", "coverage")
    for c in cases.get("cases", []):
        cid = c.get("id")
        if not cid:
            errs.append("case 缺 id")
            continue
        if cid in seen:
            errs.append("重复 case id: %s" % cid)
        seen.add(cid)
        t = c.get("type", "evidence")
        if t not in allowed:
            errs.append("%s 未知 case type=%s" % (cid, t))
        if t == "evidence":
            if not c.get("onset"):
                errs.append("%s 缺 onset" % cid)
            elif not c.get("recorded_tokens"):
                errs.append("%s 缺 recorded_tokens" % cid)
            elif not c.get("target"):
                errs.append("%s 缺 target" % cid)
    return errs


def proof(registry_path, cases_path):
    """只读自证：跑 probe+replay 前后，语料体积/sha256 不变。"""
    corpus = [registry_path, cases_path, CRED_DECL,
              os.path.join(GA, "memory", "keychain.py"), os.path.join(HERE, "preflight_check.py")]
    corpus = [p for p in corpus if os.path.exists(p)]
    before = {p: (os.path.getsize(p), sha256_file(p)) for p in corpus}
    registry = json.load(open(registry_path, encoding="utf-8"))
    cases = json.load(open(cases_path, encoding="utf-8"))
    run_probes(registry, live=True)
    replay(cases, live=True, registry=registry)
    after = {p: (os.path.getsize(p), sha256_file(p)) for p in corpus}
    changed = [os.path.basename(p) for p in corpus if before[p] != after[p]]
    return {"schema": "ga.preflight_proof/v1", "ts": fmt_ts(),
            "files": len(corpus), "changed": changed,
            "sizes": {os.path.basename(p): before[p][0] for p in corpus}}


# ------------------------------------------------------------ 输出

def print_probe(rows):
    print("== 前置条件探针（L0） %s" % fmt_ts())
    for r in rows:
        print("-" * 100)
        print("%-26s %-6s %s" % (r["id"], r["agg"]["verdict"], r["agg"]["note"]))
        for p in r["probes"]:
            print("    [%-13s] %-13s %s (%dms)" % (p["verdict"], p["kind"], p["detail"], p.get("ms", 0)))
        if r.get("limits"):
            print("    层外边界: %s" % r["limits"])
    tot = {}
    for r in rows:
        tot[r["agg"]["verdict"]] = tot.get(r["agg"]["verdict"], 0) + 1
    print("-" * 100)
    print("目标 %d：%s" % (len(rows), ", ".join("%s=%d" % kv for kv in sorted(tot.items()))))


def print_replay(rep):
    print("== 前置探针回测 %s（语料 %s）" % (rep["ts"], rep["done_dir"]))
    for r in rep["rows"]:
        if r["type"] == "negative_control":
            print("%-28s %-14s %s" % (r["id"], r["status"], r.get("note", "")))
            continue
        if r["type"] == "coverage":
            print("%-28s %-14s 在册探针=%s" % (r["id"], r["status"], r.get("registered_probes")))
            continue
        print("%-28s %-14s onset=%s first_hit=%s delay=%s token=%r"
              % (r["id"], r["status"], r.get("onset"), r.get("first_hit"),
                 r.get("delay_days"), r.get("token")))
        if r.get("live"):
            print("%-28s   live=%s %s" % ("", r["live"]["verdict"], r["live"]["detail"]))
    print("-" * 100)
    print("用例 %d：覆盖 %d / 问题 %d %s"
          % (rep["cases"], rep["covered"], rep["problem"], rep["problem_ids"]))


# ------------------------------------------------------------ selftest

def _mk_fixture(base):
    """构造沙盒语料：Chrome profile 树 + 族内 done 报告 + Cookies 库。"""
    chrome = os.path.join(base, "chrome")
    for prof, pairs in (("Default", [(".x.com", "auth_token"), (".linux.do", "_forum_session")]),
                        ("Profile3", [(".linux.do", "_t")])):
        d = os.path.join(chrome, prof)
        os.makedirs(d, exist_ok=True)
        db = os.path.join(d, "Cookies")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
        for h, n in pairs:
            con.execute("INSERT INTO cookies VALUES (?,?,?)", (h, n, "SECRET_MUST_NOT_BE_READ"))
        con.commit()
        con.close()
    done = os.path.join(base, "done")
    os.makedirs(done, exist_ok=True)
    # 族 A：onset 当天出现 token（应 FIRE_ON_TIME）；族 B：迟 4 天（应 FIRE_LATE）
    open(os.path.join(done, "2026-08-08_1100_alpha_task.md"), "w").write(
        "# alpha\n\n- 502 Bad Gateway\n")
    open(os.path.join(done, "2026-09-20_1100_beta_task.md"), "w").write("# beta\n\n- ok\n")
    open(os.path.join(done, "2026-09-24_1100_beta_task.md"), "w").write("# beta\n\n- SSLEOFError\n")
    # 回声污染源：alpha(别族) 报告正文回述 beta 的故障 token
    open(os.path.join(done, "2026-09-01_1100_alpha_task.md"), "w").write(
        "# alpha\n\n- 回溯：该站自 09-13 起不可达（SSLEOFError、502）\n")
    return chrome, done


def _local_http(status):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):                           # 静音
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d/" % srv.server_port


def selftest():
    checks = []

    def ck(name, cond, detail=""):
        checks.append((bool(cond), name, detail))

    base = tempfile.mkdtemp(prefix="preflight_st_")
    try:
        chrome, done = _mk_fixture(base)

        # --- 探针层 ---
        r = run_probe({"kind": "path_exists", "params": {"paths": ["tools/preflight_check.py"]}})
        ck("NC1 path_exists 存在⇒PASS", r["verdict"] == "PASS", r["detail"])
        r = run_probe({"kind": "path_exists", "params": {"paths": ["no/such/file.py"]}})
        ck("NC2 path_exists 缺失⇒FAIL", r["verdict"] == "FAIL", r["detail"])
        r = run_probe({"kind": "cookie_present",
                       "params": {"names": ["auth_token"], "domain": ".x.com",
                                  "browser_root": chrome}})
        ck("NC3 cookie_present 命中⇒PASS", r["verdict"] == "PASS", r["detail"])
        r = run_probe({"kind": "cookie_present",
                       "params": {"names": ["_t"], "domain": ".linux.do",
                                  "browser_root": chrome}})
        ck("NC4 多 profile 扫描命中非 Default profile", r["verdict"] == "PASS"
           and r.get("hits") and r["hits"][0]["profile"] == "Profile3", r["detail"])
        r = run_probe({"kind": "cookie_present",
                       "params": {"names": ["nope_cookie"], "domain": ".x.com",
                                  "browser_root": chrome}})
        ck("NC5 cookie 库在但无名⇒FAIL（不是 UNVER）", r["verdict"] == "FAIL", r["detail"])
        r = run_probe({"kind": "cookie_present",
                       "params": {"names": ["_t"], "browser_root": os.path.join(base, "absent")}})
        ck("NC6 无任何 Cookies 库⇒UNVERIFIABLE", r["verdict"] == "UNVERIFIABLE", r["detail"])
        # ★ 隐私负控：探针产物里不得出现 cookie 值
        ck("NC7 探针结果不含 cookie 值明文",
           "SECRET_MUST_NOT_READ" not in json.dumps(run_probe(
               {"kind": "cookie_present", "params": {"names": ["auth_token"], "browser_root": chrome}})),
           "")
        srv, url = _local_http(200)
        r = run_probe({"kind": "http_endpoint", "params": {"url": url, "expect_status": [200]}})
        ck("NC8 http 200 在 expect⇒PASS", r["verdict"] == "PASS", r["detail"])
        srv.shutdown()
        srv, url = _local_http(599)
        r = run_probe({"kind": "http_endpoint", "params": {"url": url, "expect_status": [200]}})
        ck("NC9 http 599 不在 expect⇒FAIL", r["verdict"] == "FAIL", r["detail"])
        r = run_probe({"kind": "http_endpoint", "params": {"url": url, "expect_status": [599]}})
        ck("NC10 同响应对照：599 在 expect⇒PASS（判据灵敏度）", r["verdict"] == "PASS", r["detail"])
        srv.shutdown()
        r = run_probe({"kind": "http_endpoint",
                       "params": {"url": "http://127.0.0.1:1/", "expect_status": [200]}})
        ck("NC11 传输层失败⇒FAIL", r["verdict"] == "FAIL", r["detail"])
        r = run_probe({"kind": "http_endpoint", "params": {"url": url}})
        ck("NC12 缺 expect_status⇒UNVERIFIABLE（fail-closed 不猜）",
           r["verdict"] == "UNVERIFIABLE", r["detail"])
        r = run_probe({"kind": "no_such_kind", "params": {}})
        ck("NC13 未知 kind⇒UNVERIFIABLE 不冒泡", r["verdict"] == "UNVERIFIABLE", r["detail"])
        r = run_probe({"kind": "credential_entry", "params": {"entry_id": "no_such_entry"}})
        ck("NC14 声明面查无该 id⇒FAIL", r["verdict"] == "FAIL", r["detail"])
        r = run_probe({"kind": "credential_entry", "params": {"entry_id": "bc_x_auth_token"}})
        ck("NC15 真凭据探针不崩溃且不读值（PASS/FAIL/UNVER 三态之一）",
           r["verdict"] in VERDICTS and "value" not in json.dumps(r).lower(), r["detail"])
        r = run_probe({"kind": "chrome_tabs", "params": {"match": "no-such-host.invalid"}})
        ck("NC16 chrome_tabs 无匹配⇒非 PASS（FAIL 或 UNVER）", r["verdict"] != "PASS", r["detail"])

        # --- 聚合 ---
        agg = aggregate([_res("path_exists", "PASS", "x"), _res("path_exists", "PASS", "y")])
        ck("NC17 全 PASS⇒OK 但 health=False（★不得判健康）",
           agg["verdict"] == "OK" and agg["health"] is False and agg["caveat"], json.dumps(agg))
        agg = aggregate([_res("path_exists", "PASS", "x"), _res("path_exists", "UNVERIFIABLE", "y")])
        ck("NC18 含 UNVER⇒WARN（未知≠成立）", agg["verdict"] == "WARN", json.dumps(agg))
        agg = aggregate([_res("path_exists", "PASS", "x"), _res("http_endpoint", "FAIL", "y")])
        ck("NC19 含 FAIL⇒ALERT", agg["verdict"] == "ALERT", json.dumps(agg))
        agg = aggregate([])
        ck("NC20 无探针⇒UNVERIFIABLE（未登记不是健康）", agg["verdict"] == "UNVERIFIABLE", json.dumps(agg))

        # --- 回测 ---
        cases = {"cases": [
            {"id": "st_ontime", "type": "evidence", "target": "alpha_task", "onset": "2026-08-08",
             "recorded_tokens": ["502 Bad Gateway"], "grace_days": 1},
            {"id": "st_late", "type": "evidence", "target": "beta_task", "onset": "2026-09-20",
             "recorded_tokens": ["SSLEOFError"], "grace_days": 1},
            {"id": "st_nodata", "type": "evidence", "target": "ghost_task", "onset": "2026-08-08",
             "recorded_tokens": ["x"]},
            {"id": "st_nc", "type": "negative_control", "target": "alpha_task",
             "probes": [{"kind": "path_exists", "params": {"paths": ["tools/preflight_check.py"]}}]},
            {"id": "st_gap", "type": "coverage", "target": "unregistered_task"},
        ]}
        rep = replay(cases, done_dir=done, live=False, registry={"targets": []})
        by = {r["id"]: r for r in rep["rows"]}
        ck("NC21 回测：onset 当天命中⇒FIRE_ON_TIME", by["st_ontime"]["status"] == "FIRE_ON_TIME",
           str(by["st_ontime"]))
        ck("NC22 回测：迟 4 天⇒FIRE_LATE", by["st_late"]["status"] == "FIRE_LATE",
           str(by["st_late"]))
        ck("NC23 回测：★跨族回声不得污染（alpha 报告里有 SSLEOFError）",
           by["st_late"]["first_hit"] == "2026-09-24" and by["st_late"]["first_file"].endswith("beta_task.md"),
           str(by["st_late"]))
        ck("NC24 回测：族内无语料⇒NO_DATA（语料缺失不得当通过）",
           by["st_nodata"]["status"] == "NO_DATA", str(by["st_nodata"]))
        ck("NC25 回测：负控用例⇒NC_OK（全 PASS 不判健康）", by["st_nc"]["status"] == "NC_OK",
           str(by["st_nc"]))
        ck("NC26 回测：未登记探针⇒COVERAGE_GAP 显式", by["st_gap"]["status"] == "COVERAGE_GAP",
           str(by["st_gap"]))
        ck("NC27 回测：problem 计数=3 且逐项点名（late+nodata+gap）",
           rep["problem"] == 3 and sorted(rep["problem_ids"]) == ["st_gap", "st_late", "st_nodata"]
           and rep["problem_status"].get("FIRE_LATE") == 1
           and len([r for r in rep["rows"] if r["status"] == "FIRE_ON_TIME"]) == 1,
           str(rep["problem_ids"]) + " " + json.dumps(rep["problem_status"]))

        # --- 声明面校验 ---
        errs = check_registry({"targets": [{"id": "t", "probes": [{"kind": "nope"}]}]})
        ck("NC28 registry 未知 kind⇒报错", any("未知探针" in e for e in errs), str(errs))
        errs = check_registry({"targets": [{"id": "t", "probes": [{"kind": "http_endpoint", "params": {"url": "u"}}]}]})
        ck("NC29 registry 缺 params 必填项⇒报错", any("expect_status" in e for e in errs), str(errs))
        errs = check_cases({"cases": [{"id": "c", "type": "evidence", "target": "t"}]})
        ck("NC30 cases 缺 onset⇒报错（fail-closed）", any("onset" in e for e in errs), str(errs))

        # --- 真声明面自检 ---
        reg = load_registry(DEFAULT_REGISTRY)
        errs = check_registry(reg)
        ck("NC31 真 registry 通过校验且目标数≥5", not errs and len(reg["targets"]) >= 5, str(errs))
        cs = json.load(open(DEFAULT_CASES, encoding="utf-8"))
        errs = check_cases(cs)
        ck("NC32 真 cases 通过校验且用例数≥5", not errs and len(cs["cases"]) >= 5, str(errs))
        covered_targets = {c.get("target") for c in cs["cases"] if c.get("type") == "evidence"}
        ck("NC33 回测覆盖的 target 全部已在册（防回测与登记脱钩）",
           covered_targets <= {t["id"] for t in reg["targets"]}, str(sorted(covered_targets)))

        # --- 只读自证 ---
        pr = proof(DEFAULT_REGISTRY, DEFAULT_CASES)
        ck("NC34 proof：跑 probe+replay 前后语料 sha256 无变化", pr["changed"] == [], str(pr["changed"]))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    npass = sum(1 for ok, _, _ in checks if ok)
    for ok, name, detail in checks:
        print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  <-- " + str(detail)[:160]))
    print("-" * 80)
    print("selftest: %d passed / %d failed" % (npass, len(checks) - npass))
    return 0 if npass == len(checks) else 1


# ------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description="定时任务前置条件探针层（L0，只读）")
    ap.add_argument("cmd", nargs="?", default="probe",
                    choices=["probe", "replay", "check", "cases", "proof", "selftest"])
    ap.add_argument("--target", action="append", help="只跑指定 target（可多次）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY, help="判据表/注册表文件")
    ap.add_argument("--registry-key", default=DEFAULT_REGISTRY_KEY, help="登记面所在段名（默认 preflight）")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--done-dir", default=DEFAULT_DONE)
    ap.add_argument("--json", nargs="?", const="-", default=None)
    ap.add_argument("--no-live", action="store_true", help="跳过真探针（仅骨架，全 UNVERIFIABLE）")
    ap.add_argument("--strict", action="store_true", help="回测有问题时 rc=1")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest or args.cmd == "selftest":
        return selftest()

    if not os.path.exists(args.registry):
        print("registry 不存在: %s" % args.registry)
        return 2
    try:
        registry = load_registry(args.registry, args.registry_key)
    except (ValueError, json.JSONDecodeError) as e:
        print("registry 读取失败（fail-closed）：%s" % e)
        return 2
    errs = check_registry(registry)
    if errs:
        print("registry 不合法（fail-closed）：")
        for e in errs:
            print("  - %s" % e)
        return 2

    if args.cmd == "check":
        cases = json.load(open(args.cases, encoding="utf-8")) if os.path.exists(args.cases) else {"cases": []}
        cerr = check_cases(cases)
        if cerr:
            print("cases 不合法（fail-closed）：")
            for e in cerr:
                print("  - %s" % e)
            return 2
        by_id = {t["id"] for t in registry["targets"]}
        covered = {c.get("target") for c in cases.get("cases", []) if c.get("type") == "evidence"}
        gaps = sorted(by_id - covered)
        print("registry OK：目标 %d，探针 %d 条" % (len(by_id), sum(len(t["probes"]) for t in registry["targets"])))
        print("cases OK：用例 %d" % len(cases.get("cases", [])))
        print("未纳入回测的在册目标（非错误，仅透明度）：%s" % (gaps or "无"))
        return 0

    if args.cmd == "cases":
        cases = json.load(open(args.cases, encoding="utf-8"))
        for c in cases.get("cases", []):
            print("%-26s %-16s target=%-24s onset=%-12s tokens=%s"
                  % (c.get("id"), c.get("type", "evidence"), c.get("target"),
                     c.get("onset", "-"), c.get("recorded_tokens", "-")))
        return 0

    if args.cmd == "proof":
        out = proof(args.registry, args.cases)
        print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else
              "proof: files=%d changed=%s" % (out["files"], out["changed"]))
        return 0 if not out["changed"] else 1

    if args.cmd == "replay":
        cases = json.load(open(args.cases, encoding="utf-8"))
        cerr = check_cases(cases)
        if cerr:
            print("cases 不合法（fail-closed）：")
            for e in cerr:
                print("  - %s" % e)
            return 2
        rep = replay(cases, done_dir=args.done_dir, live=not args.no_live, registry=registry)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print_replay(rep)
        return 1 if (rep["problem"] and args.strict) else 0

    rows = run_probes(registry, only=set(args.target) if args.target else None, live=not args.no_live)
    if args.json:
        payload = {"schema": "ga.preflight_probe/v1", "ts": fmt_ts(), "targets": rows}
        if args.json == "-":
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            print("written %s" % args.json)
    else:
        print_probe(rows)
    return 1 if any(r["agg"]["verdict"] == "ALERT" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
