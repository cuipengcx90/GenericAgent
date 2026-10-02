#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GCP 成本守望 (gcp_cost_watch) —— 只读巡检 cpgoagent-11013 是否在产生费用。

背景：obsidian-note/70 Output/2026-10-02-gcp-billing-incident-review.md
      赠金耗尽 → 2026-09 首现实付 $6.21（$13.39 用量 − 赠金/免费层）
      2026-10-01 19:43(PDT) 停机 ⇒ 目标状态：月成本 $0
      「$0」的三前提：①实例保持停机 ②磁盘 ≤30GB ③不新增收费资源

用法（子命令为位置参数）：
    python3 tools/gcp_cost_watch.py check [--json] [--notify] [--ledger]
    python3 tools/gcp_cost_watch.py selftest

退出码：0=全部正常  1=有 ALERT（可能在花钱）  2=环境问题（gcloud 缺失/未认证）

只读：仅调用 gcloud 的 list/describe/get 类命令，绝不创建/修改/删除任何资源。
赠金余额（Cloud Billing credits）没有公开的 CLI 端点（实测 404），
故「赠金还有没有新券」需人工在控制台核对，脚本会打印 URL 与判据。
"""
import json
import os
import shutil
import subprocess
import sys
import time

PROJECT = os.environ.get("GCP_PROJECT", "cpgoagent-11013")
ZONE = os.environ.get("GCP_ZONE", "us-west1-b")
BILLING = os.environ.get("GCP_BILLING", "017EE2-1FDFF1-148293")
GA_ROOT = os.environ.get("GA_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER = os.path.join(GA_ROOT, "temp", "gcp_cost_watch", "ledger.jsonl")

# 免费层常量（us-west1/us-central1/us-east1）
FREE_MACHINE = "e2-micro"
FREE_ZONES = ("us-west1", "us-central1", "us-east1")
FREE_DISK_GB = 30
PRICE_EXT_IP_MONTH = 3.65      # 运行中实例的外部 IPv4 约 $0.005/h
PRICE_PD_STD_GB_MONTH = 0.04   # pd-standard 超出免费层后
PRICE_SNAPSHOT_GB_MONTH = 0.026

GCLOUD_CANDIDATES = [
    os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
    "/opt/homebrew/share/google-cloud-sdk/bin/gcloud",
    "/opt/homebrew/bin/gcloud",
    "/usr/local/bin/gcloud",
    "/usr/local/share/google-cloud-sdk/bin/gcloud",
]


def find_gcloud():
    p = shutil.which("gcloud")
    if p:
        return p
    for c in GCLOUD_CANDIDATES:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def gcloud(args, timeout=60):
    """跑一条 gcloud 命令，返回 (rc, stdout, stderr)。"""
    g = find_gcloud()
    if not g:
        return 127, "", "gcloud not found"
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(g) + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run([g] + args, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa
        return 1, "", str(e)


def _val(args, timeout=60):
    """取单值输出，失败返回 None。"""
    rc, out, _ = gcloud(args, timeout)
    if rc != 0:
        return None
    return out.strip()


def collect_state():
    """采集现场状态（全部只读）。"""
    st = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "errors": []}

    # 1. 实例
    rc, out, err = gcloud(["compute", "instances", "list", "--project=" + PROJECT,
                           "--format=json"])
    inst = []
    if rc == 0:
        try:
            for it in json.loads(out or "[]"):
                ac = (it.get("networkInterfaces") or [{}])[0].get("accessConfigs") or [{}]
                inst.append({
                    "name": it.get("name"),
                    "status": it.get("status"),
                    "machine": (it.get("machineType") or "").rsplit("/", 1)[-1],
                    "zone": (it.get("zone") or "").rsplit("/", 1)[-1],
                    "tier": ac[0].get("networkTier"),
                    "natIP": ac[0].get("natIP"),
                    "lastStart": it.get("lastStartTimestamp"),
                    "lastStop": it.get("lastStopTimestamp"),
                })
        except Exception as e:  # noqa
            st["errors"].append("instances parse: %s" % e)
    else:
        st["errors"].append("instances list rc=%s %s" % (rc, (err or "").strip()[:120]))
    st["instances"] = inst

    # 2. 磁盘
    rc, out, _ = gcloud(["compute", "disks", "list", "--project=" + PROJECT, "--format=json"])
    disks = []
    if rc == 0:
        try:
            for d in json.loads(out or "[]"):
                disks.append({"name": d.get("name"), "sizeGb": int(d.get("sizeGb") or 0),
                              "type": (d.get("type") or "").rsplit("/", 1)[-1],
                              "status": d.get("status")})
        except Exception as e:  # noqa
            st["errors"].append("disks parse: %s" % e)
    else:
        st["errors"].append("disks list rc=%s" % rc)
    st["disks"] = disks

    # 3. 静态 IP / 4. 快照 / 5. 转发规则+路由器 / 9. Cloud Domains
    def _count(args):
        rc, out, _ = gcloud(args, 60)
        if rc != 0:
            return None
        try:
            return len(json.loads(out or "[]"))
        except Exception:  # noqa
            return None

    st["addresses"] = _count(["compute", "addresses", "list", "--project=" + PROJECT, "--format=json"])
    st["snapshots"] = _count(["compute", "snapshots", "list", "--project=" + PROJECT, "--format=json"])
    st["forwarding_rules"] = _count(["compute", "forwarding-rules", "list", "--project=" + PROJECT, "--format=json"])
    st["routers"] = _count(["compute", "routers", "list", "--project=" + PROJECT, "--format=json"])
    st["domains"] = _count(["domains", "registrations", "list", "--project=" + PROJECT])

    # 6. VM Manager 策略分配（osconfig REST）
    tok = _val(["auth", "print-access-token"], 30)
    st["os_policy_assignments"] = None
    if tok:
        url = ("https://osconfig.googleapis.com/v1/projects/%s/locations/%s/osPolicyAssignments"
               % (PROJECT, ZONE))
        try:
            r = subprocess.run(["curl", "-s", "--max-time", "20", url,
                                "-H", "Authorization: Bearer " + tok],
                               capture_output=True, text=True, timeout=30)
            j = json.loads(r.stdout or "{}")
            st["os_policy_assignments"] = len(j.get("osPolicyAssignments") or [])
        except Exception:  # noqa
            st["os_policy_assignments"] = None

    # 7. 结算账号下挂的项目
    rc, out, _ = gcloud(["billing", "projects", "list", "--billing-account=" + BILLING,
                         "--format=value(projectId)"], 60)
    st["billed_projects"] = [l.strip() for l in (out or "").splitlines() if l.strip()] if rc == 0 else None

    # 8. 预算
    rc, out, _ = gcloud(["billing", "budgets", "list", "--billing-account=" + BILLING], 60)
    budgets = []
    if rc == 0:
        cur = None
        for line in (out or "").splitlines():
            if line.startswith("---"):
                if cur:
                    budgets.append(cur)
                cur = {}
            elif cur is not None and ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k == "displayName":
                    cur["name"] = v
                elif k == "units":
                    cur["amount"] = v.strip("'\"")
                elif k == "nanos":
                    cur["amount"] = str(int(v) / 1e9) if v.isdigit() else v
        if cur:
            budgets.append(cur)
    st["budgets"] = budgets

    return st


# ---------------------------------------------------------------- 判定内核
def evaluate(st):
    """纯函数：吃状态 dict，吐 findings 列表。selftest 用假数据直接调。"""
    f = []

    def add(level, item, detail):
        f.append({"level": level, "item": item, "detail": detail})

    # 1. 实例
    insts = st.get("instances")
    if insts is None:
        add("UNKNOWN", "instances", "无法读取实例列表")
        insts = []
    elif not insts:
        add("OK", "instances", "无实例")
    for it in insts:
        stt = (it.get("status") or "").upper()
        z = it.get("zone") or ""
        region = "-".join(z.split("-")[:2]) if z else ""
        machine = it.get("machine") or ""
        tier = it.get("tier") or ""
        if stt in ("TERMINATED", "STOPPED", "STOPPING"):
            add("OK", "instance:" + (it.get("name") or "?"),
                "已停机(%s)，不收计算费" % stt)
        elif stt in ("RUNNING", "PROVISIONING", "STAGING"):
            est = PRICE_EXT_IP_MONTH
            msg = "**运行中** ⇒ 外部 IPv4 约 $%.2f/月" % PRICE_EXT_IP_MONTH
            free_ok = (machine == FREE_MACHINE and region in FREE_ZONES)
            if not free_ok:
                est += 8.0
                msg += "；机型/区域**不在免费层**(%s/%s) ⇒ 另加按小时计算费(估 +$8)" % (machine, region)
            if tier.upper() == "PREMIUM":
                add("ALERT", "instance:" + (it.get("name") or "?"),
                    msg + "；⚠️ networkTier=**PREMIUM** ⇒ 出口流量免费额度只有 1GB，历史因此多花 $9~12/月！应改回 STANDARD")
            else:
                if free_ok:
                    msg += "；机型/区域在免费层，但**运行即计外部 IPv4 费**（0 成本必须 TERMINATED）"
                add("ALERT", "instance:" + (it.get("name") or "?"),
                    msg + "（tier=%s）。预计月成本 ≈ $%.1f" % (tier or "?", est))
        else:
            add("WARN", "instance:" + (it.get("name") or "?"), "状态=%s（非停机）" % stt)

    # 2. 磁盘
    disks = st.get("disks")
    if disks is None:
        add("UNKNOWN", "disks", "无法读取磁盘")
    else:
        total = sum(d.get("sizeGb", 0) for d in disks)
        over = total - FREE_DISK_GB
        nonstd = [d for d in disks if d.get("type") != "pd-standard"]
        if over > 0:
            add("ALERT", "disks", "合计 %dGB > 免费层 %dGB ⇒ 超出部分约 $%.2f/月"
                % (total, FREE_DISK_GB, over * PRICE_PD_STD_GB_MONTH))
        elif nonstd:
            add("WARN", "disks", "合计 %dGB 但在免费层内；存在非 pd-standard 磁盘(%s) ⇒ 不在免费层口径"
                % (total, ",".join(d.get("type", "?") for d in nonstd)))
        else:
            add("OK", "disks", "合计 %dGB pd-standard，在免费层 %dGB 内" % (total, FREE_DISK_GB))

    # 3. 静态 IP
    a = st.get("addresses")
    if a is None:
        add("UNKNOWN", "addresses", "无法读取")
    elif a == 0:
        add("OK", "addresses", "无静态 IP")
    else:
        add("ALERT", "addresses", "%d 个静态 IP ⇒ 每个约 $%.2f/月" % (a, PRICE_EXT_IP_MONTH))

    # 4. 快照
    s = st.get("snapshots")
    if s is None:
        add("UNKNOWN", "snapshots", "无法读取")
    elif s == 0:
        add("OK", "snapshots", "无快照")
    else:
        add("WARN", "snapshots", "%d 个快照 ⇒ 按存储量计费(约 $%.3f/GB·月)" % (s, PRICE_SNAPSHOT_GB_MONTH))

    # 5. 转发规则 / 路由器 / NAT
    fr, rt = st.get("forwarding_rules"), st.get("routers")
    if fr is None or rt is None:
        add("UNKNOWN", "network-rules", "无法读取转发规则/路由器")
    elif fr == 0 and rt == 0:
        add("OK", "network-rules", "无转发规则 / 无 Cloud Router(NAT)")
    else:
        add("ALERT", "network-rules", "转发规则 %s 个 / 路由器 %s 个 ⇒ 可能产生负载均衡或 NAT 费用" % (fr, rt))

    # 6. VM Manager
    osa = st.get("os_policy_assignments")
    if osa is None:
        add("UNKNOWN", "vm-manager", "无法读取策略分配")
    elif osa == 0:
        add("OK", "vm-manager", "无 OS 策略分配（VM Manager 已关）")
    else:
        add("ALERT", "vm-manager", "存在 %d 个 OS 策略分配 ⇒ VM Manager 会按实例小时计费(~$2.2/月)" % osa)

    # 7. 结算挂载项目
    bp = st.get("billed_projects")
    if bp is None:
        add("UNKNOWN", "billed-projects", "无法读取结算挂载")
    elif bp == [PROJECT]:
        add("OK", "billed-projects", "结算账号下仅 %s" % PROJECT)
    else:
        extra = [p for p in bp if p != PROJECT]
        add("ALERT" if extra else "WARN", "billed-projects",
            "挂载项目=%s%s" % (bp, ("；新增项目=%s ⇒ 需确认其是否在花钱" % extra) if extra else ""))

    # 8. 预算
    bs = st.get("budgets")
    if bs is None:
        add("UNKNOWN", "budgets", "无法读取预算")
    elif not bs:
        add("WARN", "budgets", "没有任何预算告警 ⇒ 花钱无人知")
    else:
        for b in bs:
            try:
                amt = float(b.get("amount") or 0)
            except Exception:  # noqa
                amt = 0.0
            nm = b.get("name") or "?"
            if amt < 5:
                add("WARN", "budget:" + nm,
                    "金额仅 $%g/月 ⇒ 真实月成本远高于此，阈值天天触发=形同虚设（建议提到 $5~$10）" % amt)
            else:
                add("OK", "budget:" + nm, "金额 $%g/月" % amt)

    # 9. Cloud Domains
    dm = st.get("domains")
    if dm is None:
        add("UNKNOWN", "domains", "无法读取 Cloud Domains")
    elif dm == 0:
        add("OK", "domains", "无 Cloud Domains 注册（无年费漏项）")
    else:
        add("WARN", "domains", "%d 个域名注册 ⇒ 有年费" % dm)

    # 10. 赠金（CLI 不可得）
    add("INFO", "credits",
        "赠金余额无公开 CLI 端点（credits 实测 404）⇒ 需人工核对；"
        "URL: https://console.cloud.google.com/billing/%s/credits ; "
        "判据：出现新的 DevProgram 券(剩余>0) ⇒ 赠金仍在发；若某月账单「小计」>0 ⇒ 赠金已停" % BILLING)

    return f


def verdict(findings):
    if any(x["level"] == "ALERT" for x in findings):
        return "ALERT"
    if any(x["level"] == "UNKNOWN" for x in findings):
        return "UNKNOWN"
    if any(x["level"] == "WARN" for x in findings):
        return "WARN"
    return "OK"


def render(st, findings):
    v = verdict(findings)
    icons = {"OK": "✅", "WARN": "⚠️", "ALERT": "🔴", "UNKNOWN": "❔", "INFO": "ℹ️"}
    lines = []
    lines.append("GCP 成本守望 · %s · verdict=%s" % (st.get("collected_at", ""), v))
    lines.append("项目=%s  结算=%s" % (PROJECT, BILLING))
    lines.append("-" * 62)
    for x in findings:
        lines.append("%s [%s] %s" % (icons.get(x["level"], "?"), x["item"], x["detail"]))
    if st.get("errors"):
        lines.append("-" * 62)
        lines.append("采集告警: " + "; ".join(st["errors"])[:300])
    lines.append("-" * 62)
    lines.append("停机三前提：①实例 TERMINATED ②磁盘 ≤30GB ③无新增资源(静态IP/快照/LB/NAT)")
    lines.append("真实金额请看 Console → 结算 → 报告 的「小计」列（不是「使用费」列）。")
    return "\n".join(lines)


def notify(text):
    try:
        sys.path.insert(0, GA_ROOT)
        sys.path.insert(0, os.path.join(GA_ROOT, "reflect_scripts"))
        from reflect_common import notify_feishu  # type: ignore
        notify_feishu(text)
        return True
    except Exception as e:  # noqa
        print("notify failed: %s" % e, file=sys.stderr)
        return False


def write_ledger(st, findings):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "verdict": verdict(findings),
           "collected_at": st.get("collected_at"), "findings": findings}
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return LEDGER


# ---------------------------------------------------------------- selftest
def _fake(**kw):
    base = {"instances": [], "disks": [], "addresses": 0, "snapshots": 0,
            "forwarding_rules": 0, "routers": 0, "os_policy_assignments": 0,
            "billed_projects": [PROJECT], "budgets": [{"name": "b", "amount": "10"}],
            "domains": 0, "errors": []}
    base.update(kw)
    return base


def selftest():
    cases = []

    def check(name, st, expect_verdict=None, expect_level=None, expect_item=None):
        f = evaluate(st)
        v = verdict(f)
        ok = True
        msg = []
        if expect_verdict and v != expect_verdict:
            ok = False
            msg.append("verdict=%s want %s" % (v, expect_verdict))
        if expect_level:
            got = [x["level"] for x in f if expect_item and x["item"].startswith(expect_item)]
            if expect_level not in got:
                ok = False
                msg.append("level %s not in %s" % (expect_level, got))
        cases.append((name, ok, "; ".join(msg)))

    STOPPED = [{"name": "vm", "status": "TERMINATED", "machine": "e2-micro",
                "zone": "us-west1-b", "tier": "STANDARD", "natIP": None,
                "lastStart": None, "lastStop": None}]
    RUNN = [dict(STOPPED[0], status="RUNNING", tier="STANDARD", natIP="1.2.3.4")]
    RUNP = [dict(STOPPED[0], status="RUNNING", tier="PREMIUM", natIP="1.2.3.4")]
    BIG = [dict(STOPPED[0], status="RUNNING", machine="e2-medium", zone="us-west1-b")]

    # P1 完全健康基线
    check("P1 全绿", _fake(instances=STOPPED, disks=[{"name": "d", "sizeGb": 30, "type": "pd-standard"}]),
          "OK")
    # N1 实例运行 ⇒ 必 ALERT
    check("N1 运行中", _fake(instances=RUNN, disks=[{"name": "d", "sizeGb": 30, "type": "pd-standard"}]),
          "ALERT", "ALERT", "instance:")
    # N2 PREMIUM 层级必须点名
    f = evaluate(_fake(instances=RUNP, disks=[]))
    cases.append(("N2 PREMIUM 点名",
                  any("PREMIUM" in x["detail"] and x["level"] == "ALERT" for x in f), ""))
    # N3 非免费层机型
    check("N3 非免费层机型", _fake(instances=BIG, disks=[]), "ALERT", "ALERT", "instance:")
    # N4 磁盘超 30GB
    check("N4 磁盘超标", _fake(instances=STOPPED, disks=[{"name": "d", "sizeGb": 50, "type": "pd-standard"}]),
          "ALERT", "ALERT", "disks")
    # N5 静态 IP
    check("N5 静态IP", _fake(instances=STOPPED, disks=[], addresses=1), "ALERT", "ALERT", "addresses")
    # N6 VM Manager 策略回归
    check("N6 VM Manager", _fake(instances=STOPPED, disks=[], os_policy_assignments=1),
          "ALERT", "ALERT", "vm-manager")
    # N7 预算金额过小 ⇒ WARN
    check("N7 预算过小", _fake(instances=STOPPED, disks=[], budgets=[{"name": "x", "amount": "0.1"}]),
          "WARN", "WARN", "budget:")
    # N8 新增结算项目
    check("N8 新挂项目", _fake(instances=STOPPED, disks=[], billed_projects=[PROJECT, "other"]),
          "ALERT", "ALERT", "billed-projects")
    # N9 快照
    check("N9 快照", _fake(instances=STOPPED, disks=[], snapshots=2), "WARN", "WARN", "snapshots")
    # N10 转发规则/路由器
    check("N10 LB/NAT", _fake(instances=STOPPED, disks=[], forwarding_rules=1), "ALERT", "ALERT", "network-rules")
    # N11 采集失败 ⇒ UNKNOWN（不得伪报 OK）
    check("N11 采集失败不冒充", _fake(instances=None), "UNKNOWN", "UNKNOWN", "instances")
    # N12 赠金必须给 INFO + URL
    f = evaluate(_fake(instances=STOPPED, disks=[]))
    cases.append(("N12 赠金 INFO+URL",
                  any(x["item"] == "credits" and "credits" in x["detail"] and x["level"] == "INFO"
                      for x in f), ""))
    # N13 空实例列表 = OK 而非 WARN
    check("N13 无实例=OK", _fake(instances=[], disks=[]), "OK")

    passed = sum(1 for _, ok, _ in cases if ok)
    for n, ok, m in cases:
        print(("PASS " if ok else "FAIL ") + n + (("  <- " + m) if m else ""))
    print("selftest: %d/%d passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return selftest()
    if cmd != "check":
        print("unknown subcommand: %s (use check|selftest)" % cmd, file=sys.stderr)
        return 2
    as_json = "--json" in argv
    do_notify = "--notify" in argv
    do_ledger = "--ledger" in argv

    if not find_gcloud():
        print("gcloud not found", file=sys.stderr)
        return 2
    rc, out, err = gcloud(["auth", "list", "--format=value(account,status)"], 30)
    if rc != 0 or "*" not in (out or ""):
        print("gcloud 未认证: %s" % ((out or "") + (err or ""))[:200], file=sys.stderr)
        return 2

    st = collect_state()
    findings = evaluate(st)
    v = verdict(findings)

    if as_json:
        print(json.dumps({"verdict": v, "state": st, "findings": findings},
                         ensure_ascii=False, indent=2))
    else:
        print(render(st, findings))

    if do_ledger:
        p = write_ledger(st, findings)
        if not as_json:
            print("台账已追加: %s" % p)

    if do_notify and v in ("ALERT",):
        notify("🔴 GCP 成本守望告警\n" + render(st, findings))

    return 0 if v in ("OK", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
