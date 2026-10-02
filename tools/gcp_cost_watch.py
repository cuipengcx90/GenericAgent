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

v2 扩充（2026-10-02）——回答三个问题：
  Q1 定时任务本身花不花钱？本脚本每次只调 gcloud/监控 API 的 list/get（只读），
     不创建任何资源；API 调用本身不计费（Cloud Monitoring 读取在 已启用 API 的
     免费额度内）。真正的成本来自它监控的资源，不来自守望本身。
  Q2 除了实例/磁盘/静态IP/快照/LB/NAT，还有哪些可能花钱的维度？v2 逐项落成检查：
     对象存储桶(免费 5GB 且仅限 us 三区)、Artifact Registry/Container Registry 镜像
     (免费 0.5GB)、App Engine 遗留应用、Cloud Logging 摄取量(免费 50GiB/项目/月)、
     已启用 API 面变化、以及 SQL/Run/Functions/PubSub/Scheduler/DNS/KMS/Secret/
     Firestore/BigQuery 十类按量计费资源面。
  Q3 能不能实时知道？资源面=实时(gcloud 直查)；用量面=近实时(Cloud Monitoring，
     通常滞后 1~3 分钟，本脚本打印最近样本时间戳与滞后分钟数)；费用面=**不可实时**
     (Cloud Billing 无「查当前花费」的公开 CLI/REST，credits 端点实测 404；账单金额
     只能人工看 Console→结算→报告 的「小计」列，或配置 BigQuery 账单导出后自查)。
     另做一项交叉验证：实例状态=停机，但运行时长监控样本出现在停机之后 ⇒ 疑似已重启。

用法（子命令为位置参数）：
    python3 tools/gcp_cost_watch.py check [--json] [--notify] [--ledger] [--accept-services]
    python3 tools/gcp_cost_watch.py selftest
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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

# ---- v2 新增常量：其它维度的免费额度与单价 ----
FREE_STORAGE_GB = 5.0            # Cloud Storage Always Free: 5GB-months（**仅** us-east1/us-west1/us-central1 区域桶）
PRICE_STORAGE_GB_MONTH = 0.026   # standard 多区域 US 约 $0.026/GiB·月（区域桶约 $0.020）
FREE_ARTIFACT_GB = 0.5           # Artifact Registry 免费 0.5GB
PRICE_ARTIFACT_GB_MONTH = 0.10   # 超出后约 $0.10/GiB·月
FREE_LOGGING_GIB_MONTH = 50.0    # Cloud Logging(_Default/_Required 桶) 每项目每月免费 50GiB
PRICE_LOGGING_PER_GIB = 0.50     # 超出后约 $0.50/GiB
MON_ROOT = "https://monitoring.googleapis.com/v3/projects/%s/timeSeries"

# 其它按量计费资源面：gcloud list 命令 → 短名
LIST_CMDS = [
    (["sql", "instances", "list"], "cloud-sql"),
    (["run", "services", "list"], "cloud-run"),
    (["functions", "list"], "cloud-functions"),
    (["pubsub", "topics", "list"], "pubsub"),
    (["scheduler", "jobs", "list"], "cloud-scheduler"),
    (["dns", "managed-zones", "list"], "cloud-dns"),
    (["kms", "keyrings", "list", "--location=global"], "cloud-kms"),
    (["secrets", "list"], "secret-manager"),
    (["firestore", "databases", "list"], "firestore"),
]
# 短名 → (有资源时的级别, 计费说明)
OTHER_META = {
    "cloud-sql": ("ALERT", "Cloud SQL 实例按小时计费(最小档约 $0.01/h)"),
    "cloud-run": ("ALERT", "Cloud Run 按请求/CPU/内存计费(有少量免费额度)"),
    "cloud-functions": ("ALERT", "Cloud Functions 按调用次数/时长计费"),
    "pubsub": ("WARN", "Pub/Sub 按消息量计费(每月前 10GiB 免费)"),
    "cloud-scheduler": ("ALERT", "Cloud Scheduler 每个作业约 $0.10/月"),
    "cloud-dns": ("ALERT", "Cloud DNS 每个托管区域约 $0.20/月"),
    "cloud-kms": ("WARN", "KMS 每个密钥版本约 $0.06/月"),
    "secret-manager": ("WARN", "Secret Manager 超 6 个活跃版本后每个约 $0.06/月"),
    "firestore": ("WARN", "Firestore/Datastore 按存储与读写计费"),
    "bigquery-datasets": ("WARN", "BigQuery 数据集按存储计费(账单导出数据集本身也计费)"),
}
SERVICE_BASELINE = os.path.join(GA_ROOT, "temp", "gcp_cost_watch", "enabled_services.json")

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


def _parse_ts(s):
    """解析 RFC3339（gcloud 'Z'/带偏移、Monitoring 的 'Z' 两种写法都吃）。"""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except Exception:  # noqa
        d = None
        for f in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                d = datetime.strptime(t, f)
                break
            except Exception:  # noqa
                pass
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _listcount(args, timeout=60):
    """按 json 列表长度计数。API 未启用/无权限 → (0, 说明)；其它错误 → (None, 错误首行)。

    只读：仅 list 类命令。
    """
    rc, out, err = gcloud(list(args) + ["--project=" + PROJECT, "--format=json"], timeout)
    low = (err or "").lower()
    if rc == 0:
        try:
            return len(json.loads(out or "[]") or []), ""
        except Exception:  # noqa
            return None, "parse error"
    for k in ("not enabled", "has not been used", "is disabled", "accessnotconfigured",
              "permission_denied", "permission denied", "does not have permission"):
        if k in low:
            return 0, "API 未启用/无权限"
    first = (err or "").strip().splitlines()
    return None, (first[0][:120] if first else "rc=%d" % rc)


def monitoring_point(metric, hours=24, period="3600s", aligner="ALIGN_SUM"):
    """只读查 Cloud Monitoring 时间序列，返回 (points, note)；points=[{end,v},...] 按时间升序。

    需要 access token（gcloud auth print-access-token，只读）。失败返回 (None, 原因)。
    """
    tok = _val(["auth", "print-access-token"], 30)
    if not tok:
        return None, "无法获取 access token"
    now = time.time()
    params = {
        "filter": 'metric.type="%s"' % metric,
        "interval.startTime": datetime.fromtimestamp(now - hours * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": period,
        "aggregation.perSeriesAligner": aligner,
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
    }
    url = (MON_ROOT % PROJECT) + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa
        return None, "monitoring 请求失败: %s" % e
    pts = []
    for ts in (j.get("timeSeries") or []):
        for p in (ts.get("points") or []):
            v = (p.get("value") or {})
            num = v.get("int64Value", v.get("doubleValue"))
            try:
                num = float(num)
            except Exception:  # noqa
                continue
            pts.append({"end": (p.get("interval") or {}).get("endTime"), "v": num})
    pts.sort(key=lambda x: x.get("end") or "")
    return pts, None


def _bq_datasets():
    """BigQuery 数据集个数（REST；未启用/无权限按 0 计）。"""
    tok = _val(["auth", "print-access-token"], 30)
    if not tok:
        return None
    url = "https://bigquery.googleapis.com/bigquery/v2/projects/%s/datasets" % PROJECT
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=20) as r:
            return len(json.loads(r.read().decode("utf-8", "replace")).get("datasets") or [])
    except Exception:  # noqa
        return 0


def load_services_baseline():
    """已启用 API 基线（首次运行由 --accept-services 建立）。"""
    try:
        with open(SERVICE_BASELINE, encoding="utf-8") as fh:
            return json.load(fh).get("services") or None
    except Exception:  # noqa
        return None


def save_services_baseline(services):
    os.makedirs(os.path.dirname(SERVICE_BASELINE), exist_ok=True)
    with open(SERVICE_BASELINE, "w", encoding="utf-8") as fh:
        json.dump({"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "services": sorted(services)}, fh, ensure_ascii=False, indent=2)
    return SERVICE_BASELINE


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

    # ---- v2 扩充：回答「还有哪些可能花钱的维度」「能不能实时知道」 ----
    # 10. Cloud Storage 桶（Always Free 5GB 仅限 us 三区区域桶）
    rc, out, err = gcloud(["storage", "buckets", "list", "--format=json"], 60)
    if rc == 0:
        try:
            st["buckets"] = [{"name": b.get("name"), "location": b.get("location"),
                              "storageClass": b.get("storageClass"),
                              "created": b.get("timeCreated")}
                             for b in (json.loads(out or "[]") or [])]
        except Exception as e:  # noqa
            st["buckets"] = None
            st["errors"].append("buckets parse: %s" % e)
    else:
        st["buckets"] = None
        st["errors"].append("buckets list rc=%s" % rc)

    # 10b. 桶总量（Monitoring，近实时，非实时账）
    pts, note = monitoring_point("storage.googleapis.com/storage/total_bytes",
                                 hours=1, period="300s", aligner="ALIGN_MAX")
    st["bucket_bytes"] = pts[-1]["v"] if pts else None

    # 11. Artifact Registry（免费 0.5GB）
    rc, out, err = gcloud(["artifacts", "repositories", "list", "--format=json"], 60)
    if rc == 0:
        try:
            st["artifacts"] = [{"name": (r.get("name") or "").rsplit("/", 1)[-1],
                                "format": r.get("format"),
                                "sizeBytes": int(r.get("sizeBytes") or 0)}
                               for r in (json.loads(out or "[]") or [])]
        except Exception as e:  # noqa
            st["artifacts"] = None
            st["errors"].append("artifacts parse: %s" % e)
    else:
        st["artifacts"] = None
        st["errors"].append("artifacts list rc=%s" % rc)

    # 12. App Engine 遗留应用（0 实例无实例费，但存储/日志仍可能计费）
    rc, out, err = gcloud(["app", "describe", "--format=json"], 60)
    ae = None
    low = (err or "").lower()
    if rc == 0:
        try:
            j = json.loads(out or "{}")
            srv, ver, ins = _listcount(["app", "services", "list"]), \
                _listcount(["app", "versions", "list"]), _listcount(["app", "instances", "list"])
            ae = {"exists": True, "id": j.get("id"), "location": j.get("locationId"),
                  "services": srv[0], "versions": ver[0], "instances": ins[0]}
        except Exception as e:  # noqa
            st["errors"].append("appengine parse: %s" % e)
    elif "not found" in low or "not enabled" in low or "not exist" in low or "does not have" in low:
        ae = {"exists": False}
    st["app_engine"] = ae

    # 13. Cloud Logging 摄取量（免费 50GiB/项目/月）
    pts, note = monitoring_point("logging.googleapis.com/billing/bytes_ingested",
                                 hours=24, period="86400s", aligner="ALIGN_SUM")
    st["logging_bytes_24h"] = pts[-1]["v"] if pts else None
    st["monitoring_note"] = note

    # 14. 运行时长样本（与实例状态交叉验证：停机后不该再有样本）
    pts, _ = monitoring_point("compute.googleapis.com/instance/uptime",
                              hours=3, period="300s", aligner="ALIGN_MAX")
    st["uptime_last"] = pts[-1] if pts else None
    st["uptime_points"] = len(pts or [])

    # 15. 已启用服务清单（对比基线，发现新增 API 面）
    rc, out, err = gcloud(["services", "list", "--enabled",
                           "--format=value(config.name)"], 60)
    if rc == 0:
        st["enabled_services"] = sorted([l.strip() for l in (out or "").splitlines() if l.strip()])
    else:
        st["enabled_services"] = None
        st["errors"].append("services list rc=%s" % rc)
    base = load_services_baseline()
    st["enabled_services_baseline"] = base
    if st["enabled_services"] and base:
        st["enabled_services_new"] = sorted(set(st["enabled_services"]) - set(base))
        st["enabled_services_gone"] = sorted(set(base) - set(st["enabled_services"]))
    else:
        st["enabled_services_new"] = st["enabled_services_gone"] = None

    # 16. 其它按量计费资源面（十类）
    others = {}
    for args, key in LIST_CMDS:
        n, ote = _listcount(args)
        others[key] = {"count": n, "note": ote}
    others["bigquery-datasets"] = {"count": _bq_datasets(), "note": ""}
    st["other_services"] = others

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
                # 阈值过小=配置建议，不是健康问题：真实月成本远高于此 ⇒ 告警天天触发、
                # 失去信号价值。降为 INFO 以免日检 verdict 恒为 WARN 而掩盖真告警；
                # 改预算属计费配置，脚本只报告不动手（需人工在 Console 调整）。
                add("INFO", "budget:" + nm,
                    "金额仅 $%g/月 ⇒ 真实月成本远高于此，阈值天天触发=形同虚设（建议提到 $5~$10；改预算需人工在 Console 操作）" % amt)
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

    # ---- v2：11. 对象存储桶 ----
    bks = st.get("buckets")
    bbytes = st.get("bucket_bytes")
    if bks is None:
        add("UNKNOWN", "storage", "无法读取桶列表")
    elif not bks:
        add("OK", "storage", "无存储桶")
    else:
        names = ",".join(b.get("name") or "?" for b in bks)
        gb = (float(bbytes) / 1073741824.0) if isinstance(bbytes, (int, float)) else None
        if gb is None:
            add("WARN", "storage", "%d 个桶(%s)；体积不可得 ⇒ 无法确认是否超 5GB 免费额度，需人工核对"
                % (len(bks), names))
        elif gb > FREE_STORAGE_GB:
            add("ALERT", "storage", "%d 个桶合计 %.2fGB > 免费 5GB ⇒ 超出部分约 $%.2f/月 (%s)"
                % (len(bks), gb, (gb - FREE_STORAGE_GB) * PRICE_STORAGE_GB_MONTH, names))
        else:
            add("OK", "storage",
                "%d 个桶合计 %.3fGB，低于免费 5GB；注意 Always Free 仅限 us-east1/us-west1/us-central1 区域桶，"
                "其它位置按约 $%.3f/GiB·月 计 (%s)"
                % (len(bks), gb, PRICE_STORAGE_GB_MONTH, names))

    # 12. Artifact Registry 镜像
    ars = st.get("artifacts")
    if ars is None:
        add("UNKNOWN", "artifacts", "无法读取 Artifact Registry")
    elif not ars:
        add("OK", "artifacts", "无镜像仓库")
    else:
        tot = sum(a.get("sizeBytes") or 0 for a in ars)
        gb = tot / 1073741824.0
        nm = ",".join("%s(%s)" % (a.get("name"), a.get("format") or "?") for a in ars)
        if gb > FREE_ARTIFACT_GB:
            add("ALERT", "artifacts", "%d 个仓库合计 %.2fGB > 免费 0.5GB ⇒ 超出部分约 $%.3f/月 (%s)"
                % (len(ars), gb, (gb - FREE_ARTIFACT_GB) * PRICE_ARTIFACT_GB_MONTH, nm))
        else:
            add("OK", "artifacts", "%d 个仓库合计 %.1fMiB，低于免费 0.5GB (%s)"
                % (len(ars), tot / 1048576.0, nm))

    # 13. App Engine 遗留应用
    ae = st.get("app_engine")
    if ae is None:
        add("UNKNOWN", "app-engine", "无法读取 App Engine")
    elif not ae.get("exists"):
        add("OK", "app-engine", "无 App Engine 应用（不产生实例/存储费）")
    elif (ae.get("instances") or 0) > 0:
        add("ALERT", "app-engine", "存在 App Engine 应用且有 %s 个运行实例 ⇒ 按实例小时计费"
            % ae.get("instances"))
    else:
        add("INFO", "app-engine",
            "存在遗留 App Engine 应用(%s 服务/%s 版本/0 实例)：0 实例不产生实例费，但应用配额存储与日志仍可能计少量费用 ⇒ 建议人工在 Console 评估删除（脚本不动手）"
            % (ae.get("services"), ae.get("versions")))

    # 14. Cloud Logging 摄取量（免费 50GiB/项目/月）
    lb = st.get("logging_bytes_24h")
    if lb is None:
        add("INFO", "logging", "近 24h 日志摄取量不可得（Monitoring 未返回：%s）⇒ 需人工核对是否超 50GiB/月"
            % (st.get("monitoring_note") or "未知原因"))
    else:
        est = float(lb) * 30.0 / 1073741824.0
        if est > FREE_LOGGING_GIB_MONTH:
            add("ALERT", "logging", "近 24h 摄取 %.1fMiB ⇒ 折月约 %.1fGiB > 免费 %.0fGiB ⇒ 超出部分约 $%.2f/月"
                % (float(lb) / 1048576.0, est, FREE_LOGGING_GIB_MONTH,
                   (est - FREE_LOGGING_GIB_MONTH) * PRICE_LOGGING_PER_GIB))
        elif est > FREE_LOGGING_GIB_MONTH * 0.8:
            add("WARN", "logging", "近 24h 摄取 %.1fMiB ⇒ 折月约 %.1fGiB，已接近免费 %.0fGiB"
                % (float(lb) / 1048576.0, est, FREE_LOGGING_GIB_MONTH))
        else:
            add("OK", "logging", "近 24h 日志摄取 %.1fMiB ⇒ 折月约 %.2fGiB，远低于免费 %.0fGiB"
                % (float(lb) / 1048576.0, est, FREE_LOGGING_GIB_MONTH))

    # 15. 停机状态 × 运行时长样本（交叉验证：防「已重启却仍报停机」）
    ups = st.get("uptime_last")
    stops = [i for i in (st.get("instances") or [])
             if (i.get("status") or "").upper() in ("TERMINATED", "STOPPED", "STOPPING") and i.get("lastStop")]
    if not ups or not stops:
        add("INFO", "runtime-crosscheck", "运行时长样本/停机时刻不可得 ⇒ 未做交叉验证（%s）"
            % (st.get("monitoring_note") or "样本缺失"))
    else:
        bad = []
        for i in stops:
            su, ss = _parse_ts(ups.get("end")), _parse_ts(i.get("lastStop"))
            if su and ss and (su - ss).total_seconds() > 600:
                bad.append("%s(样本晚 %.0f 分钟)" % (i.get("name"), (su - ss).total_seconds() / 60.0))
        if bad:
            add("ALERT", "runtime-crosscheck",
                "实例状态为停机，但运行时长监控样本出现在停机之后 ⇒ 疑似已重新启动: %s" % ",".join(bad))
        else:
            add("OK", "runtime-crosscheck", "停机与运行时长样本一致（最近样本 %s，停机后无新样本 ⇒ 未在运行）"
                % ups.get("end"))

    # 16. 已启用 API 面变化
    svc, base = st.get("enabled_services"), st.get("enabled_services_baseline")
    news, gone = st.get("enabled_services_new"), st.get("enabled_services_gone")
    if svc is None:
        add("UNKNOWN", "services", "无法读取已启用服务清单")
    elif not base:
        add("INFO", "services", "已启用 %d 项服务；尚未建立基线 ⇒ 确认无误后运行 check --accept-services 建立（之后新增 API 会点名提醒）"
            % len(svc))
    elif news:
        add("WARN", "services", "新增启用 API: %s ⇒ 新 API 常伴随新资源，需确认其是否在花钱（确认合规后 check --accept-services 刷新基线）"
            % ",".join(news))
    else:
        add("OK", "services", "已启用 %d 项服务，与基线一致%s"
            % (len(svc), ("；已停用: " + ",".join(gone)) if gone else ""))

    # 17. 其它十类按量计费资源面
    others = st.get("other_services") or {}
    if not others:
        add("INFO", "other-resources", "其它资源面未采集（见 state.other_services）")
    else:
        hot, unk = [], []
        for key in [k for _a, k in LIST_CMDS] + ["bigquery-datasets"]:
            ent = others.get(key) or {}
            c, ote = ent.get("count"), ent.get("note")
            lvl, why = OTHER_META.get(key, ("WARN", "按量计费"))
            if c is None:
                unk.append("%s(%s)" % (key, ote or "未知"))
            elif c > 0:
                hot.append((lvl, key, c, why))
        for lvl, key, c, why in hot:
            add(lvl, "other:" + key, "%d 个 ⇒ %s" % (c, why))
        if unk:
            add("INFO", "other-unresolved", "无法判定（API 未启用或无权）: %s" % ",".join(sorted(unk)))
        if not hot:
            add("OK", "other-resources",
                "SQL/Run/Functions/PubSub/Scheduler/DNS/KMS/Secret/Firestore/BigQuery 十类均无资源 ⇒ 无按量计费项")

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
    up = st.get("uptime_last") or {}
    lag = ""
    if up.get("end"):
        d = _parse_ts(up["end"])
        if d:
            lag = "；最近用量样本 %s（滞后约 %.0f 分钟）" % (
                up["end"], max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 60.0))
    lines.append("实时性：资源面=实时(gcloud 直查，采集于 %s)%s" % (st.get("collected_at") or "?", lag))
    lines.append("        用量面=近实时(Cloud Monitoring，通常滞后 1~3 分钟)；费用面=不可实时"
                 "(Cloud Billing 无「当前花费」CLI/REST，credits 端点实测 404 ⇒ 只能人工看 Console「小计」列)")
    lines.append("-" * 62)
    lines.append("停机三前提：①实例 TERMINATED ②磁盘 ≤30GB ③无新增资源(静态IP/快照/LB/NAT/桶/镜像/AppEngine/新API)")
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
            "domains": 0, "errors": [],
            # v2 默认值取「安静路径」：不产生 ALERT/UNKNOWN，避免旧用例被新维度污染
            "buckets": [], "bucket_bytes": None, "artifacts": [],
            "app_engine": {"exists": False}, "logging_bytes_24h": 0,
            "uptime_last": None, "monitoring_note": None,
            "enabled_services": [], "enabled_services_baseline": None,
            "enabled_services_new": None, "enabled_services_gone": None,
            "other_services": {}}
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
    # N7 预算金额过小 ⇒ INFO（配置建议，不影响 verdict；见 evaluate 注释）
    check("N7 预算过小", _fake(instances=STOPPED, disks=[], budgets=[{"name": "x", "amount": "0.1"}]),
          "OK", "INFO", "budget:")
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

    # ---- v2 新维度用例（每条都对应一个真实免费额度/计费点）----
    D30 = [{"name": "d", "sizeGb": 30, "type": "pd-standard"}]
    # T1 桶在 5GB 免费层内 ⇒ OK
    check("T1 桶<5GB", _fake(instances=STOPPED, disks=D30, buckets=[{"name": "b", "location": "US"}],
                             bucket_bytes=1024 * 1024), "OK", "OK", "storage")
    # T2 桶超 5GB ⇒ ALERT
    check("T2 桶>5GB", _fake(instances=STOPPED, disks=D30, buckets=[{"name": "b", "location": "US"}],
                             bucket_bytes=6 * 1024 ** 3), "ALERT", "ALERT", "storage")
    # T3 桶体积取不到 ⇒ WARN（不得冒充 OK）
    check("T3 桶体积不可得", _fake(instances=STOPPED, disks=D30, buckets=[{"name": "b"}], bucket_bytes=None),
          "WARN", "WARN", "storage")
    # T4 Artifact 仓库超免费 0.5GB ⇒ ALERT
    check("T4 镜像仓库超标", _fake(instances=STOPPED, disks=D30,
                                artifacts=[{"name": "r", "format": "DOCKER", "sizeBytes": 700 * 1024 ** 2}]),
          "ALERT", "ALERT", "artifacts")
    # T5 App Engine 有运行实例 ⇒ ALERT
    check("T5 AppEngine 有实例", _fake(instances=STOPPED, disks=D30,
                                      app_engine={"exists": True, "services": 1, "versions": 1, "instances": 2}),
          "ALERT", "ALERT", "app-engine")
    # T6 App Engine 存在但 0 实例 ⇒ INFO（不升 verdict，但要点名遗留应用）
    check("T6 AppEngine 0实例", _fake(instances=STOPPED, disks=D30,
                                     app_engine={"exists": True, "services": 1, "versions": 1, "instances": 0}),
          "OK", "INFO", "app-engine")
    # T7 日志摄取折月超 50GiB ⇒ ALERT
    check("T7 日志摄取超标", _fake(instances=STOPPED, disks=D30, logging_bytes_24h=2 * 1024 ** 3),
          "ALERT", "ALERT", "logging")
    # T8 日志摄取不可得 ⇒ INFO（不冒充 OK）
    check("T8 日志量不可得", _fake(instances=STOPPED, disks=D30, logging_bytes_24h=None,
                                 monitoring_note="monitoring 请求失败"), "OK", "INFO", "logging")
    # T9 停机但运行时长样本晚于停机 ⇒ ALERT（交叉验证核心用例）
    check("T9 疑似重启", _fake(instances=[dict(STOPPED[0], lastStop="2026-10-01T19:43:34+00:00")], disks=D30,
                              uptime_last={"end": "2026-10-02T03:00:00Z", "v": 120.0}),
          "ALERT", "ALERT", "runtime-crosscheck")
    # T10 样本止于停机时刻 ⇒ OK
    check("T10 交叉验证一致", _fake(instances=[dict(STOPPED[0], lastStop="2026-10-02T02:43:00+00:00")], disks=D30,
                                   uptime_last={"end": "2026-10-02T02:45:43Z", "v": 202.0}),
          "OK", "OK", "runtime-crosscheck")
    # T11 新增启用 API ⇒ WARN
    check("T11 新增API", _fake(instances=STOPPED, disks=D30,
                              enabled_services=["a.googleapis.com", "b.googleapis.com"],
                              enabled_services_baseline=["a.googleapis.com"],
                              enabled_services_new=["b.googleapis.com"], enabled_services_gone=[]),
          "WARN", "WARN", "services")
    # T12 其它资源面出现 Cloud SQL 实例 ⇒ ALERT
    check("T12 SQL 实例", _fake(instances=STOPPED, disks=D30,
                               other_services={"cloud-sql": {"count": 1, "note": ""}}),
          "ALERT", "ALERT", "other:cloud-sql")
    # T13 其它资源面全 0 ⇒ OK
    check("T13 其它面全空", _fake(instances=STOPPED, disks=D30,
                                 other_services={"cloud-sql": {"count": 0, "note": ""}}),
          "OK", "OK", "other-resources")

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
    if "--accept-services" in argv and st.get("enabled_services"):
        p = save_services_baseline(st["enabled_services"])
        st["enabled_services_baseline"] = sorted(st["enabled_services"])
        st["enabled_services_new"], st["enabled_services_gone"] = [], []
        if not as_json:
            print("已刷新服务基线: %s (%d 项)" % (p, len(st["enabled_services"])))
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
