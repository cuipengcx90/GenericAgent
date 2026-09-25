#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""net_doctor.py — 网络分层诊断单入口（只读探测，不改任何系统/代理配置）

为什么需要它：RULES#14 的排查命令（Clash API / 198.18.* 假 IP / 目标站探活）
此前散落在多个 SOP 与临时会话里，haisou 事故中「SSL 截断 + peer 198.18.0.112」
曾被误读。本工具把「本机断网 / 代理节点坏 / 站点故障」三段式判定固化为一条命令。

分层（每层都给证据 + 下一步命令）：
  L1 本机链路  : 默认路由 / 接口 IP / DNS / 网关 TCP 可达
  L2 代理层    : Clash(mihomo) 控制面探测（unix socket → 127.0.0.1:9090），
                 读 mode / tun / 当前节点，并对当前节点做 delay 探测
  L3 出口基线  : gstatic generate_204 / baidu / apple（注意：TUN 开启时它们
                 **不是**「真实直连」证据，RULES#14）
  L4 目标站    : 逐目标 DNS → 假 IP 判定 → TCP/TLS/HTTP，给出错误分类

关键环境事实（2026-09-25 实测，写死在代码里避免再次踩坑）：
  * code_run/非交互 shell 的 PATH = /opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
    ⇒ **/sbin 与 /usr/sbin 不在 PATH**，ifconfig/route/ping/netstat/scutil/networksetup
      一律「command not found」。故本工具全部走绝对路径 + 候选回退。
  * Clash(mihomo) 控制面套接字在本机**不恒存在**（未开 TUN / 服务未拉起时只有
    /tmp/verge/clash-verge-service.sock 这个 root 服务锁），⇒ 必须支持「无 Clash 降级」。
  * 既不做「真实直连」断言，也不读任何 Clash 配置/密钥（API 需 secret 时只报告需认证）。

用法：
  python3 tools/net_doctor.py                              # 只跑链路+代理+基线
  python3 tools/net_doctor.py --target https://haisou.cc   # 追加目标站（可多次）
  python3 tools/net_doctor.py --json                       # 机器可读
  python3 tools/net_doctor.py --selftest                   # 负控自测（含假 IP/不可达域名）
  python3 tools/net_doctor.py --no-history                 # 不写历史文件

历史：默认 append 到 temp/net_doctor_history.jsonl（网络状态会漂移，历史只是快照，
**禁止**把任一记录当作永久结论）。
"""

import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ---------------------------------------------------------------- 常量

FAKE_NETS = [ipaddress.ip_network("198.18.0.0/15")]  # RFC2544，Clash fake-ip 常用段

CLASH_SOCK_CANDIDATES = [
    "/tmp/verge/verge-mihomo.sock",
    "/tmp/clash-verge/mihomo.sock",
    "/tmp/mihomo.sock",
]
CLASH_TCP_API = ("127.0.0.1", 9090)

BASELINES = [
    "https://www.gstatic.com/generate_204",
    "https://www.baidu.com/",
    "https://www.apple.com/library/test/success.html",
]

UA = "net_doctor/1.0 (+read-only diagnosis)"

# 绝对路径候选（PATH 里没有 /sbin）
BIN_ROUTE = ["/sbin/route", "/usr/sbin/route"]
BIN_IFCONFIG = ["/sbin/ifconfig", "/usr/sbin/ifconfig"]
BIN_SCUTIL = ["/usr/sbin/scutil"]


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_cmd(cands, args, timeout=5):
    """按候选绝对路径执行；返回 (ok, cmd, out_or_err)。"""
    for c in cands:
        if os.path.exists(c):
            try:
                p = subprocess.run([c] + args, capture_output=True, text=True, timeout=timeout)
                out = (p.stdout or "") + (p.stderr or "")
                return (p.returncode == 0, c, out.strip())
            except Exception as e:  # noqa: BLE001
                return (False, c, "%s: %s" % (type(e).__name__, e))
    return (False, "MISSING", "no candidate found in %s" % cands)


# ---------------------------------------------------------------- L1 本机链路

def probe_default_route():
    ok, cmd, out = run_cmd(BIN_ROUTE, ["-n", "get", "default"])
    rec = {"ok": False, "cmd": cmd, "gateway": None, "interface": None, "raw": out[:400]}
    if ok or "gateway:" in out:
        m = re.search(r"gateway:\s*([0-9.]+)", out)
        i = re.search(r"interface:\s*(\S+)", out)
        if m:
            rec["gateway"] = m.group(1)
        if i:
            rec["interface"] = i.group(1)
        rec["ok"] = bool(m and i)
    return rec


def probe_iface_ip(iface):
    if not iface:
        return {"ok": False, "error": "no default iface"}
    ok, cmd, out = run_cmd(BIN_IFCONFIG, [iface])
    ips = re.findall(r"inet\s+([0-9.]+)", out)
    return {"ok": bool(ips), "cmd": cmd, "iface": iface, "ips": ips}


def probe_dns(host, timeout=4):
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, None)
        ips = sorted({i[4][0] for i in infos})
        return {"ok": True, "host": host, "ips": ips, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "host": host, "error": str(e)[:200],
                "error_class": type(e).__name__, "ms": int((time.time() - t0) * 1000)}


def tcp_probe(host, port, timeout=2):
    """TCP 连通探测。ECONNREFUSED 也算「主机可达」（证明三层通路在）。"""
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return {"ok": True, "host": host, "port": port, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        cls = type(e).__name__
        refused = "Refused" in cls or "refused" in str(e)
        return {"ok": bool(refused), "host": host, "port": port, "refused_but_reachable": refused,
                "error_class": cls, "error": str(e)[:160], "ms": int((time.time() - t0) * 1000)}


def fake_ip_of(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in FAKE_NETS)


# ---------------------------------------------------------------- L2 代理层

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=3):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path
        self.timeout = timeout

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s


def _api_get(conn_factory, path, timeout=3):
    try:
        conn = conn_factory()
        conn.request("GET", path, headers={"User-Agent": UA})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        return {"ok": 200 <= resp.status < 300, "status": resp.status, "body": body[:4000]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "error_class": type(e).__name__,
                "error": str(e)[:200]}


def _ver_of(body):
    """从 /version 响应里取 version 字段（真实 mihomo 返回 {"version": "1.18.x", "meta": true}）。"""
    try:
        j = json.loads(body)
        v = j.get("version")
        if v:
            return str(v)[:80]
    except Exception:  # noqa: BLE001
        pass
    return (body or "").strip()[:200]


def detect_clash(timeout=3, sock_override=None):
    """探测 Clash/mihomo 控制面。返回 endpoint/api_ok/version/mode/tun/node。

    sock_override 仅供测试注入（夹具 unix socket）；默认只认 CLASH_SOCK_CANDIDATES
    与 127.0.0.1:9090，不猜其它路径。"""
    rec = {"api_ok": False, "endpoint": None, "version": None, "mode": None, "tun": None,
           "node": None, "sock_exists": [], "note": None}
    factory = None
    cands = [sock_override] if sock_override else CLASH_SOCK_CANDIDATES
    for p in cands:
        if os.path.exists(p):
            rec["sock_exists"].append(p)
            r = _api_get(lambda p=p: UnixHTTPConnection(p, timeout), "/version", timeout)
            if r["ok"]:
                factory = lambda p=p: UnixHTTPConnection(p, timeout)  # noqa: E731
                rec["endpoint"] = "unix:" + p
                rec["version"] = _ver_of(r["body"])
                break
            if r["status"] == 401:
                rec["endpoint"] = "unix:" + p
                rec["note"] = "API 需认证（未读取任何配置文件/密钥，按无 Clash 降级处理）"
                return rec
    if factory is None:
        r = _api_get(lambda: http.client.HTTPConnection(*CLASH_TCP_API, timeout=timeout), "/version", timeout)
        if r["ok"]:
            factory = lambda: http.client.HTTPConnection(*CLASH_TCP_API, timeout=timeout)  # noqa: E731
            rec["endpoint"] = "tcp:%s:%d" % CLASH_TCP_API
            rec["version"] = _ver_of(r["body"])
        elif r.get("status") == 401:
            rec["endpoint"] = "tcp:%s:%d" % CLASH_TCP_API
            rec["note"] = "API 需认证（未读取任何配置文件/密钥，按无 Clash 降级处理）"
            return rec
    if factory is None:
        rec["note"] = rec["note"] or "未发现 Clash/mihomo 控制面（未开 TUN 或服务未拉起）"
        return rec

    rec["api_ok"] = True
    cfg = _api_get(factory, "/configs", timeout)
    if cfg["ok"]:
        try:
            j = json.loads(cfg["body"])
            rec["mode"] = j.get("mode")
            rec["tun"] = (j.get("tun") or {}).get("enable")
        except Exception:  # noqa: BLE001
            pass
    pr = _api_get(factory, "/proxies", timeout)
    if pr["ok"]:
        try:
            j = json.loads(pr["body"])
            proxies = j.get("proxies", {})
            for name in ("GLOBAL", "Proxy", "PROXY", "节点选择", "Selector"):
                node = proxies.get(name) or {}
                if node.get("now"):
                    rec["node"] = {"group": name, "now": node["now"], "type": node.get("type")}
                    break
            if rec["node"] is None:
                for name, node in proxies.items():
                    if node.get("type") in ("Selector", "URLTest", "Fallback") and node.get("now"):
                        rec["node"] = {"group": name, "now": node["now"], "type": node.get("type")}
                        break
        except Exception:  # noqa: BLE001
            pass
    rec["_factory"] = factory  # 内部使用，输出时剔除
    return rec


def clash_delay(rec, url, timeout_ms=6000):
    """对当前节点做 delay 探测（RULES#14 的命令等价实现）。"""
    factory = rec.get("_factory")
    node = (rec.get("node") or {}).get("now")
    if not factory or not node:
        return {"ok": False, "error": "no clash api / no selected node"}
    q = "/proxies/%s/delay?timeout=%d&url=%s" % (
        urllib.parse.quote(node, safe=""), timeout_ms, urllib.parse.quote(url, safe=""))
    r = _api_get(factory, q, timeout=timeout_ms / 1000.0 + 2)
    out = {"ok": False, "node": node, "url": url}
    if r["ok"]:
        try:
            j = json.loads(r["body"])
            out["ok"] = "delay" in j
            out["delay_ms"] = j.get("delay")
            out["message"] = j.get("message")
        except Exception:  # noqa: BLE001
            out["error"] = "unparsable: " + r["body"][:160]
    else:
        out["error"] = r.get("error") or ("HTTP %s" % r.get("status"))
        out["error_class"] = r.get("error_class")
    return out


# ---------------------------------------------------------------- L3/L4 出口与目标

def http_probe(url, timeout=5, method="GET"):
    t0 = time.time()
    rec = {"url": url, "ok": False, "status": None, "peer": None, "ms": None,
           "error": None, "error_class": None}
    try:
        p = urllib.parse.urlparse(url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(method, p.path or "/", headers={"User-Agent": UA, "Host": p.netloc})
        resp = conn.getresponse()
        rec["status"] = resp.status
        rec["ok"] = 200 <= resp.status < 400
        try:
            rec["peer"] = conn.sock.getpeername()[0]
        except Exception:  # noqa: BLE001
            pass
        resp.read(512)
        conn.close()
    except Exception as e:  # noqa: BLE001
        rec["error_class"] = type(e).__name__
        rec["error"] = str(e)[:200]
    rec["ms"] = int((time.time() - t0) * 1000)
    return rec


TLS_CLASSES = {"SSLError", "SSLCertVerificationError", "SSLEOFError", "CertificateError"}
TCP_TIMEOUT_CLASSES = {"timeout", "TimeoutError", "ConnectionRefusedError",
                       "ConnectionResetError", "OSError", "gaierror", "socket.gaierror"}


def classify_target(t):
    """目标站错误分类（顺序敏感：DNS → 假 IP → 结果）。"""
    if not t["dns"]["ok"]:
        return "DNS_FAIL"
    for ip in t["dns"].get("ips", []):
        if fake_ip_of(ip):
            return "FAKE_IP"
    h = t["http"]
    if h["ok"]:
        return "OK"
    if h["status"] is not None:
        return "HTTP_ERROR_STATUS"
    ec = h.get("error_class") or ""
    if ec in TLS_CLASSES:
        return "TLS_FAIL"
    if "timeout" in ec.lower():
        return "TCP_TIMEOUT"
    return "TCP_FAIL"


CLASS_LABEL = {
    "OK": "可达",
    "DNS_FAIL": "域名不可解析（DNS 层，勿判站点故障）",
    "FAKE_IP": "解析到 TUN 假 IP（无判定价值，勿判站点故障）",
    "TCP_TIMEOUT": "TCP 超时",
    "TCP_FAIL": "TCP 连接失败",
    "TLS_FAIL": "TLS 握手失败（SSL 截断/证书问题）",
    "HTTP_ERROR_STATUS": "HTTP 返回错误状态码",
}
SITE_FAILURE_CLASSES = {"TCP_TIMEOUT", "TCP_FAIL", "TLS_FAIL", "HTTP_ERROR_STATUS"}


def probe_target(url, timeout=5):
    """目标站探测。解析到假 IP 时**不发起连接**（TUN 会吞掉连接白等超时，
    且该结果无判定价值）——保留 skipped 标记作为证据。"""
    host = urllib.parse.urlparse(url).hostname or url
    dns = probe_dns(host, timeout)
    if dns["ok"] and any(fake_ip_of(ip) for ip in dns.get("ips", [])):
        http = {"url": url, "ok": False, "skipped": True, "reason": "fake_ip",
                "status": None, "peer": None, "ms": 0, "error": None, "error_class": None}
    else:
        http = http_probe(url, timeout)
    return {"url": url, "host": host, "dns": dns, "http": http}


# ---------------------------------------------------------------- 判定

def verdict(link, clash, baselines, targets):
    """三段式判定 + 证据链 + 下一步命令。返回 dict(code,label,evidence,next_steps)。"""
    ev = []
    base_ok = [b for b in baselines if b["ok"]]
    base_fail = [b for b in baselines if not b["ok"]]

    if not link["link_ok"]:
        ev.append("默认路由=%s 网关=%s 网关TCP=%s DNS=%s" % (
            link["route"]["ok"], link["route"]["gateway"],
            link["gateway_tcp"].get("ok"), link["dns"]["ok"]))
        return {"code": "LOCAL_DOWN", "label": "本机断网", "evidence": ev, "next_steps": [
            "/sbin/route -n get default    # 看 gateway/interface 是否为空",
            "/sbin/ifconfig en0            # 看 inet 是否有地址（en0 换成默认接口）",
            "/usr/sbin/networksetup -listallhardwareports   # 确认 Wi-Fi/以太网服务名",
            "sudo /usr/sbin/networksetup -setairportpower en0 on   # 仅在确认为 Wi-Fi 关闭时使用",
        ]}

    if clash.get("api_ok") and clash.get("node") and clash.get("_delay_baseline"):
        d = clash["_delay_baseline"]
        if not d.get("ok"):
            ev.append("Clash 当前节点 %s 对 generate_204 delay 失败: %s"
                      % (d.get("node"), d.get("error") or d.get("message")))
            ev.append("但本机链路正常（网关 %s 可达）" % link["route"]["gateway"])
            return {"code": "PROXY_NODE_BAD", "label": "代理节点坏（出口层）", "evidence": ev,
                    "next_steps": [
                        "curl -s --unix-socket %s 'http://localhost/proxies/%s/delay?timeout=8000&url=https://www.gstatic.com/generate_204'"
                        % (clash["endpoint"].split("unix:", 1)[-1], clash["node"]["now"]),
                        "# 逐个节点跑同一命令，找出可用节点后切换（控制面操作，非本工具职责）",
                        "# 若所有节点都失败 ⇒ 订阅/机场侧问题，不是「站点故障」",
                    ]}

    if base_ok and targets:
        site_fail = [t for t in targets if t["class"] in SITE_FAILURE_CLASSES]
        if site_fail and all(t["class"] not in ("FAKE_IP", "DNS_FAIL") for t in site_fail):
            for t in site_fail:
                b = next((x for x in baselines if x["ok"]), None)
                if b:
                    ev.append("%s 失败(%s: %s) 而基线 %s=%s 正常"
                              % (t["url"], t["class"], t["http"].get("error") or t["http"].get("status"),
                                 b["url"], b["status"]))
            return {"code": "SITE_FAILURE", "label": "站点故障（目标侧）", "evidence": ev,
                    "next_steps": [
                        "curl -s https://r.jina.ai/<目标URL>   # 跨网旁证（RULES#14）",
                        "# 若目标是同一站点的多个路径都失败、且其它站点正常 ⇒ 偏站点故障",
                        "# 但 peer 或解析结果落在 198.18.0.0/15 时不得据此断言（TUN 假 IP）",
                        "# 换一个出口/节点再复测一次，避免把出口策略误判成站点故障",
                    ]}

    if base_fail and not base_ok:
        ev.append("基线全失败: %s" % ", ".join(b["url"] + "(" + str(b.get("error_class") or b.get("status")) + ")"
                                             for b in base_fail))
        ev.append("本机链路正常，Clash API=%s" % ("已发现" if clash.get("api_ok") else "未发现（降级）"))
        return {"code": "EGRESS_BAD", "label": "出口层不可达（本机在线但出不去）", "evidence": ev,
                "next_steps": [
                    "# 先确认是否 TUN 接管：/sbin/route -n get default 的 interface 是否 utun*",
                    "# TUN 下「直连」不可信 ⇒ 只能通过控制面判断（见 README/RULES#14）",
                    "curl -s --max-time 5 -o /dev/null -w '%{http_code}\\n' https://www.gstatic.com/generate_204",
                ]}

    ev.append("基线 %d/%d 可达" % (len(base_ok), len(baselines)))
    if targets:
        ev.append("目标: " + ", ".join("%s=%s" % (t["url"], t["class"]) for t in targets))
    return {"code": "OK", "label": "未发现异常", "evidence": ev, "next_steps": []}


# ---------------------------------------------------------------- 主流程

def diagnose(targets, timeout=5, extra_clash_delay=True, clash_sock=None):
    rep = {"ts": now_iso(), "cwd": os.getcwd()}
    route = probe_default_route()
    iface_ip = probe_iface_ip(route.get("interface"))
    gw = route.get("gateway")
    gw_tcp = tcp_probe(gw, 80, timeout=2) if gw else {"ok": False, "error": "no gateway"}
    dns = probe_dns("www.apple.com", timeout=timeout)
    link_ok = bool(route["ok"] and iface_ip["ok"] and (gw_tcp.get("ok") or dns["ok"]))
    link = {"route": route, "iface_ip": iface_ip, "gateway_tcp": gw_tcp, "dns": dns,
            "link_ok": link_ok,
            "tun_hint": (route.get("interface") or "").startswith("utun")}
    rep["link"] = link

    clash = detect_clash(timeout=3, sock_override=clash_sock)
    if clash.get("api_ok") and clash.get("node") and extra_clash_delay:
        clash["_delay_baseline"] = clash_delay(clash, BASELINES[0])
    rep["clash"] = {k: v for k, v in clash.items() if not k.startswith("_")}
    rep["clash_delay"] = clash.get("_delay_baseline")

    # 并行跑基线
    with ThreadPoolExecutor(max_workers=4) as ex:
        baselines = list(ex.map(lambda u: http_probe(u, timeout), BASELINES))
    rep["baselines"] = baselines

    trecs = []
    for u in targets:
        t = probe_target(u, timeout)
        t["class"] = classify_target(t)
        trecs.append(t)
    rep["targets"] = trecs

    rep["verdict"] = verdict(link, clash, baselines, trecs)
    return rep


def render(rep, json_out=False):
    if json_out:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return
    L = []
    L.append("=== net_doctor @ %s ===" % rep["ts"])
    L.append("（结论仅代表本次探测时刻；网络状态会漂移，禁止标为永久结论）")
    lk = rep["link"]
    L.append("\n[L1 本机链路] link_ok=%s  TUN 提示=%s" % (lk["link_ok"], lk["tun_hint"]))
    L.append("  默认路由: ok=%s gateway=%s iface=%s (cmd=%s)"
             % (lk["route"]["ok"], lk["route"]["gateway"], lk["route"]["interface"], lk["route"]["cmd"]))
    L.append("  接口地址: ok=%s iface=%s ips=%s" % (lk["iface_ip"]["ok"], lk["iface_ip"].get("iface"),
                                                   lk["iface_ip"].get("ips")))
    L.append("  网关 TCP: ok=%s %s" % (lk["gateway_tcp"].get("ok"),
                                      lk["gateway_tcp"].get("error_class") or ""))
    L.append("  DNS: ok=%s host=%s ips=%s ms=%s %s"
             % (lk["dns"]["ok"], lk["dns"].get("host"), lk["dns"].get("ips"),
                lk["dns"].get("ms"), lk["dns"].get("error_class") or ""))
    c = rep["clash"]
    if c.get("api_ok"):
        L.append("\n[L2 代理层] endpoint=%s version=%s mode=%s tun=%s"
                 % (c.get("endpoint"), c.get("version"), c.get("mode"), c.get("tun")))
        if c.get("node"):
            L.append("  当前节点: %s = %s" % (c["node"]["group"], c["node"]["now"]))
        d = rep.get("clash_delay")
        if d:
            L.append("  节点 delay(generate_204): ok=%s delay=%sms node=%s %s"
                     % (d.get("ok"), d.get("delay_ms"), d.get("node"),
                        d.get("error") or d.get("message") or ""))
    else:
        L.append("\n[L2 代理层] 降级：%s" % (c.get("note") or "无控制面"))
        L.append("  （降级路径只跑链路 + 目标站，不臆测代理状态）")
    L.append("\n[L3 出口基线]")
    for b in rep["baselines"]:
        L.append("  %-50s ok=%-5s status=%-4s peer=%-16s ms=%s %s"
                 % (b["url"], b["ok"], b["status"], b["peer"], b["ms"], b["error_class"] or ""))
    if rep["targets"]:
        L.append("\n[L4 目标站]")
        for t in rep["targets"]:
            h = t["http"]
            htxt = ("skipped(%s) 未发起连接" % h.get("reason")) if h.get("skipped") else \
                   ("ok=%s status=%s peer=%s ms=%s %s" % (h["ok"], h["status"], h["peer"], h["ms"],
                                                          h.get("error_class") or ""))
            L.append("  %s\n    dns=%s ips=%s\n    http %s\n    ⇒ 分类=%s (%s)"
                     % (t["url"], t["dns"]["ok"], t["dns"].get("ips"), htxt,
                        t["class"], CLASS_LABEL.get(t["class"], "")))
    v = rep["verdict"]
    L.append("\n[判定] %s (%s)" % (v["label"], v["code"]))
    for e in v["evidence"]:
        L.append("  证据: %s" % e)
    if v["next_steps"]:
        L.append("  下一步:")
        for s in v["next_steps"]:
            L.append("    %s" % s)
    print("\n".join(L))


# ---------------------------------------------------------------- 负控自测

def _fake_mihomo_server(sock_path, delay_ok=True):
    """夹具：最小 mihomo 控制面（只实现本工具会调的 4 条路径，其余 404）。
    路径形状与真实 API 一致；未实现路径返回 404 以便暴露拼错的调用。"""
    import http.server
    import socketserver
    import threading

    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            if self.path.startswith("/version"):
                body = json.dumps({"version": "fake-mihomo-1.0", "meta": True})
            elif self.path.startswith("/configs"):
                body = json.dumps({"mode": "rule", "tun": {"enable": False}})
            elif self.path.startswith("/proxies/"):
                body = json.dumps({"delay": 123}) if delay_ok else json.dumps({"message": "dial timeout"})
                self.send_response(200 if delay_ok else 504)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())
                return
            elif self.path.startswith("/proxies"):
                body = json.dumps({"proxies": {"GLOBAL": {"type": "Selector", "now": "node-1"}}})
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

    class Srv(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True

    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = Srv(sock_path, H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, seen


def selftest(timeout=4):
    rnd = int(time.time())
    unreachable = "https://nonexistent-%d.invalid/" % rnd
    fake = "http://198.18.0.112/"
    control = "https://www.baidu.com/"
    checks = []
    t1 = probe_target(unreachable, timeout)
    t1["class"] = classify_target(t1)
    checks.append(("negctl1 不可达域名 ⇒ DNS_FAIL", t1["class"] == "DNS_FAIL",
                   "%s -> %s" % (unreachable, t1["class"])))
    t2 = probe_target(fake, timeout)
    t2["class"] = classify_target(t2)
    checks.append(("negctl2 198.18.x 假 IP ⇒ FAKE_IP", t2["class"] == "FAKE_IP",
                   "%s -> %s" % (fake, t2["class"])))
    t3 = probe_target(control, timeout)
    t3["class"] = classify_target(t3)
    checks.append(("posctl 可达站 ⇒ OK", t3["class"] == "OK", "%s -> %s" % (control, t3["class"])))

    rep = diagnose([unreachable, fake], timeout=timeout)
    v = rep["verdict"]
    checks.append(("negctl3 仅含假IP/不可达域名时 判定!=站点故障",
                   v["code"] != "SITE_FAILURE" and "站点故障" not in v["label"],
                   "实际判定=%s (%s)" % (v["label"], v["code"])))
    checks.append(("negctl4 站点故障判据不含 FAKE_IP/DNS_FAIL 类",
                   not (SITE_FAILURE_CLASSES & {"FAKE_IP", "DNS_FAIL"}),
                   "SITE_FAILURE_CLASSES=%s" % sorted(SITE_FAILURE_CLASSES)))

    # 合成判定控制（不依赖网络；专测三段式映射 + 证据/下一步非空）
    L_ok = {"link_ok": True, "route": {"ok": True, "gateway": "192.168.3.1"},
            "gateway_tcp": {"ok": True}, "dns": {"ok": True}}
    L_bad = {"link_ok": False, "route": {"ok": False, "gateway": None},
             "gateway_tcp": {"ok": False}, "dns": {"ok": False}}
    B_ok = [{"url": "https://www.gstatic.com/generate_204", "ok": True, "status": 204}]
    B_bad = [{"url": "https://www.gstatic.com/generate_204", "ok": False, "status": None,
              "error_class": "timeout"}]
    clash_bad = {"api_ok": True, "endpoint": "unix:/tmp/x.sock",
                 "node": {"group": "GLOBAL", "now": "node-1"},
                 "_delay_baseline": {"ok": False, "node": "node-1", "error": "dial timeout"}}
    T_tls = [{"url": "https://x/", "class": "TLS_FAIL", "http": {"error": "EOF", "status": None}}]
    synth = [
        ("synth 断网 ⇒ LOCAL_DOWN", verdict(L_bad, {}, B_bad, []), "LOCAL_DOWN"),
        ("synth 节点坏 ⇒ PROXY_NODE_BAD", verdict(L_ok, clash_bad, B_ok, []), "PROXY_NODE_BAD"),
        ("synth 站点故障 ⇒ SITE_FAILURE", verdict(L_ok, {"api_ok": False}, B_ok, T_tls), "SITE_FAILURE"),
        ("synth 基线全败 ⇒ EGRESS_BAD", verdict(L_ok, {"api_ok": False}, B_bad, []), "EGRESS_BAD"),
    ]
    for nm, v, want in synth:
        checks.append((nm, v["code"] == want and bool(v["next_steps"]) and bool(v["evidence"]),
                       "实际=%s steps=%d ev=%d" % (v["code"], len(v["next_steps"]), len(v["evidence"]))))

    # 端到端：Clash 控制面分支（夹具 unix socket，非真 mihomo）
    for ok_mode in (False, True):
        sp = os.path.join("/tmp", "net_doctor_fake_mihomo_%s.sock" % ("ok" if ok_mode else "bad"))
        srv, seen = _fake_mihomo_server(sp, delay_ok=ok_mode)
        try:
            rep = diagnose([], timeout=timeout, clash_sock=sp)
            v = rep["verdict"]
            if ok_mode:
                good = v["code"] != "PROXY_NODE_BAD" and rep["clash"]["api_ok"]
                note = "delay ok ⇒ 判定=%s" % v["code"]
            else:
                good = (v["code"] == "PROXY_NODE_BAD" and rep["clash"]["api_ok"]
                        and not (rep.get("clash_delay") or {}).get("ok"))
                note = "delay 504 ⇒ 判定=%s delay_ok=%s" % (v["code"], (rep.get("clash_delay") or {}).get("ok"))
            paths = [x.split("?")[0] for x in seen]
            good = good and "/version" in paths and "/configs" in paths and "/proxies" in paths \
                and any(x.startswith("/proxies/node-1/delay") for x in paths)
            checks.append(("e2e Clash 分支(夹具 delay_%s)" % ("ok" if ok_mode else "fail"), good,
                           "%s; 调用路径=%s" % (note, sorted(set(paths)))))
            checks.append(("e2e 夹具接口形状: /version 应含 version 字段",
                           (rep["clash"].get("version") or "").startswith("fake-mihomo"), ""))
        finally:
            srv.shutdown()
            srv.server_close()
            if os.path.exists(sp):
                os.unlink(sp)

    print("=== net_doctor --selftest @ %s ===" % now_iso())
    npass = 0
    for name, ok, detail in checks:
        print("  [%s] %s  (%s)" % ("PASS" if ok else "FAIL", name, detail))
        npass += 1 if ok else 0
    print("--- %d/%d passed ---" % (npass, len(checks)))
    return 0 if npass == len(checks) else 1


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="网络分层诊断（只读）")
    ap.add_argument("--target", action="append", default=[], help="目标站 URL，可多次")
    ap.add_argument("--timeout", type=float, default=5.0, help="单次探测超时秒（默认 5）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="跑负控自测")
    ap.add_argument("--no-history", action="store_true", help="不写历史文件")
    ap.add_argument("--history-file", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--clash-sock", default=None,
                    help="指定 mihomo 控制面 unix socket（默认只探固定候选；测试注入用）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(timeout=min(args.timeout, 5.0))

    rep = diagnose(args.target, timeout=args.timeout, clash_sock=args.clash_sock)
    if not args.quiet:
        render(rep, json_out=args.json)
    elif args.json:
        print(json.dumps(rep, ensure_ascii=False))

    if not args.no_history:
        path = args.history_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp", "net_doctor_history.jsonl")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            slim = {
                "ts": rep["ts"], "verdict": rep["verdict"]["code"],
                "link_ok": rep["link"]["link_ok"], "tun_hint": rep["link"]["tun_hint"],
                "clash_api": rep["clash"].get("api_ok"),
                "clash_node": (rep["clash"].get("node") or {}).get("now"),
                "clash_delay_ok": (rep.get("clash_delay") or {}).get("ok"),
                "baselines": [{"url": b["url"], "ok": b["ok"], "status": b["status"],
                               "ms": b["ms"], "error_class": b["error_class"]} for b in rep["baselines"]],
                "targets": [{"url": t["url"], "class": t["class"],
                             "peer": t["http"].get("peer"), "status": t["http"].get("status")}
                            for t in rep["targets"]],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(slim, ensure_ascii=False) + "\n")
            if not args.quiet:
                print("\n[历史] 已追加 %s" % path)
        except Exception as e:  # noqa: BLE001
            print("[warn] 写历史失败: %s" % e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
