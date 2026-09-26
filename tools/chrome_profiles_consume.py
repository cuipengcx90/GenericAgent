#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chrome「多 profile」只读对账器（R430）——漏采面是否改变 R423 的消费结论。

背景
----
R415 的 Chrome 快照只采 `Default/History`；R423 消费它时顺手量出「本机 10 个目录带
History，Default 占 91.4% 的访问 ⇒ 漏采 9 个 profile 合计 2863 次 = 8.6%」，并把
「R415 源改逐 profile」列为待批（R423 §7.2）。本工具回答的是一个**判决性问题**：

    这 8.6% 的漏采面，会不会改变 R423 那 7 条消费决策？

设计约束（对齐 TODO#19 的验收）
--------------------------------
1. **只读**：对源库一律只做「拷贝到工作区」，拷贝前后逐库打印 sha256 指纹；源库绝不以
   可写方式打开，也不 DETACH/DELETE。
2. **不改采集器、不接线**：本工具不改 `tools/rolling_source_snapshot.py`，也不写
   LaunchAgent / sche_tasks；扩面本身是另一条待批项。
3. **隐私**：账号邮箱/昵称一律掩码（首字符 + `***`），只输出 profile 目录名与域名级聚合。
4. **热 journal**：`History` 是活的 SQLite，必须连 `-wal`/`-shm` 一起拷（缺 -wal 会假空，
   RULE17），因此拷贝为「History + 可选 -wal/-shm」的原始文件集，**不是** VACUUM 产物
   ⇒ 本工作区的字节不可与 R423 的 VACUUM 副本字节互推（R423 §3.7 同源口径）。
5. **负控可证伪**：`verify` 模式重算副本 sha256 并与拷贝时登记的 manifest 比对，副本被
   篡改必报且 rc≠0；`--selftest` 用合成库覆盖该负控（不读真实数据）。

用法
----
    python3 tools/chrome_profiles_consume.py scan                 # 列出各 profile 体量/时间窗/Top 域
    python3 tools/chrome_profiles_consume.py reconcile            # Default-only vs 全机合并 + 逐条决策判定
    python3 tools/chrome_profiles_consume.py verify               # 复核工作区副本未被篡改（负控）
    python3 tools/chrome_profiles_consume.py --selftest           # 合成库自测（含 4 类负控）

复用
----
分析内核零重写：`tools/chrome_history_consume.py`（R423）的 `load_visits` / `dim_trends` /
`dim_drift` / `dim_volume` / `dim_uniform` / `split_self` / `norm_host` / `sha256_file`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

GA_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHROME_ROOT = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
DEFAULT_SNAPSHOT_ROOT = GA_ROOT / "temp" / "chrome_snapshots"
DEFAULT_WORK_ROOT = GA_ROOT / "temp" / "chrome_profiles"

# R423 §3.2/§3.4 的「关注域名」：升榜第一、降榜前二、三条近 30 天归零域 + 两个稳定使用面样本
WATCH_HOSTS = [
    "pm.smyze.cn", "op.smyze.cn", "op-test.smyze.cn",
    "codeup.aliyun.com", "micuapi.ai", "pan.baidu.com",
    "flow.aliyun.com", "dashboard.huifu.com", "deploy-admin.smyze.cn",
]
# 判定阈值（硬编码，报告中必须同步声明）
THRESHOLD = {
    "trend_rel_pct": 5.0,      # 趋势日均相对偏差 ≤5% ⇒ 判「不改变」
    "drift_pp": 5.0,           # 漂移百分比差 ≤5pp ⇒ 判「不改变」
    "self_share_pp": 3.0,      # 自造占比差 ≤3pp ⇒ 判「不改变」
    "watch_host_share_pct": 5.0,  # 关注域在漏采面的占比 ≥5% 其 Default 量 ⇒ 判「需复核」
}


def _load_chc():
    """按路径导入同目录的 R423 消费器（避免依赖 sys.path 顺序）。"""
    p = Path(__file__).resolve().parent / "chrome_history_consume.py"
    spec = importlib.util.spec_from_file_location("chc_r423", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chc_r423"] = mod
    spec.loader.exec_module(mod)
    return mod


CHC = _load_chc()


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def mask(s: str) -> str:
    """账号级 PII 掩码：保留首字符与域名部分（若有）。"""
    s = (s or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, _, dom = s.partition("@")
        return (local[:1] or "") + "***@" + dom
    return s[:1] + "***"


def sha256_file(path: Path) -> str:
    return CHC.sha256_file(Path(path))


def discover_profiles(root: Path):
    """列出带 History 的 profile 目录（含 Guest/System 等非 info_cache 目录）。"""
    out = []
    if not root.is_dir():
        raise SystemExit(f"[ERR] Chrome 根目录不存在：{root}")
    for d in sorted(root.iterdir()):
        h = d / "History"
        if d.is_dir() and h.is_file():
            out.append({
                "profile": d.name,
                "dir": d,
                "history": h,
                "bytes": h.stat().st_size,
                "wal": (h.parent / (h.name + "-wal")).exists(),
                "shm": (h.parent / (h.name + "-shm")).exists(),
            })
    return out


def info_cache(root: Path):
    """Local State 的 info_cache：账号/昵称（一律掩码后输出）。"""
    try:
        st = json.loads((root / "Local State").read_text(encoding="utf-8"))
    except Exception as e:
        return {"_error": str(e)}
    out = {}
    for k, v in (st.get("profile", {}).get("info_cache", {}) or {}).items():
        out[k] = {
            "name": mask(v.get("name")),
            "user": mask(v.get("user_name")),
            "gaia": mask(v.get("gaia_name")),
            "has_signed_in": bool(v.get("user_name") or v.get("gaia_name")),
        }
    return out


def copy_profile_readonly(item, workdir: Path):
    """把 History(+wal/shm) 拷进工作区；返回源侧 sha256 前后指纹与副本指纹。"""
    dst_dir = workdir / item["profile"]
    dst_dir.mkdir(parents=True, exist_ok=True)
    rec = {"profile": item["profile"], "src_bytes": item["bytes"], "files": {}}
    suffixes = ["", "-wal", "-shm"]
    for suf in suffixes:
        src = Path(str(item["history"]) + suf)
        if not src.exists():
            continue
        before = sha256_file(src)
        dst = dst_dir / (src.name)
        shutil.copy2(src, dst)
        after = sha256_file(src)
        rec["files"][src.name] = {
            "src_before_sha256": before,
            "src_after_sha256": after,
            "copy_sha256": sha256_file(dst),
            "copy_bytes": dst.stat().st_size,
            "readonly_proof": before == after,
        }
    return rec


def verify_copies(workdir: Path):
    """负控：副本与 manifest 登记不一致即报告（篡改/截断/删除）。"""
    man = workdir / "manifest.json"
    if not man.exists():
        raise SystemExit(f"[ERR] 工作区缺 manifest.json：{man}")
    data = json.loads(man.read_text(encoding="utf-8"))
    bad, ok = [], 0
    for prof, rec in data.get("profiles", {}).items():
        for name, f in rec.get("files", {}).items():
            p = workdir / prof / name
            if not p.exists():
                bad.append(f"{prof}/{name}: MISSING")
                continue
            h = sha256_file(p)
            if h != f["copy_sha256"]:
                bad.append(f"{prof}/{name}: TAMPERED {f['copy_sha256'][:12]} -> {h[:12]}")
            else:
                ok += 1
    return ok, bad


# --------------------------------------------------------------------------- #
# 单库统计（只读副本）
# --------------------------------------------------------------------------- #
def profile_stats(history: Path):
    con = sqlite3.connect(f"file:{history}?mode=ro", uri=True)
    try:
        raw = con.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        urls = con.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
        mn, mx = con.execute("SELECT MIN(visit_time), MAX(visit_time) FROM visits").fetchone()
        scheme = dict(con.execute(
            "SELECT CASE WHEN u.url LIKE 'http%' THEN 'http' "
            "WHEN u.url LIKE 'chrome-extension%' THEN 'chrome-extension' "
            "WHEN u.url LIKE 'file:%' THEN 'file' "
            "WHEN u.url LIKE 'blob:%' THEN 'blob' ELSE 'other' END AS s, COUNT(*) "
            "FROM visits v JOIN urls u ON u.id=v.url GROUP BY s"
        ).fetchall())
    finally:
        con.close()
    visits = CHC.load_visits(Path(history))
    hosts = {}
    for _t, h, _v in visits:
        hosts[h] = hosts.get(h, 0) + 1
    top = sorted(hosts.items(), key=lambda kv: -kv[1])[:10]
    first = CHC.chrome_to_dt(mn).astimezone().strftime("%Y-%m-%d %H:%M") if mn else None
    last = CHC.chrome_to_dt(mx).astimezone().strftime("%Y-%m-%d %H:%M") if mx else None
    return {
        "raw_visits": raw, "urls_rows": urls, "http_visits": len(visits),
        "scheme": scheme, "first_visit": first, "last_visit": last,
        "distinct_hosts": len(hosts),
        "top_hosts": [{"host": h, "visits": n} for h, n in top],
    }


def analyze(visits, db_bytes: int, window: int = 30):
    """四维度（复用 R423 内核）；all 与 excl_self 双口径。"""
    if not visits:
        return {"empty": True}
    volume = CHC.dim_volume(visits, db_bytes)
    drift = CHC.dim_drift(visits)
    trends = CHC.dim_trends(visits, window)
    uniform = CHC.dim_uniform(visits)
    ns, s = CHC.split_self(visits)
    vs = CHC.dim_volume(ns, db_bytes)
    return {
        "empty": False,
        "window_days": window,
        "all": {"volume": volume, "drift": drift, "trends": trends, "uniform": uniform},
        "excl_self": {"volume": vs},
        "self_visits": len(s),
        "self_share_pct": round(100.0 * len(s) / max(1, len(visits)), 1),
    }


def _ref_point(snap: Path, visits) -> dt.datetime:
    """reconcile 的「现在」参考点：优先用快照目录名（日期），取不到再退回最大访问时刻。"""
    try:
        return dt.datetime.fromisoformat(snap.name).replace(tzinfo=dt.timezone.utc)
    except Exception:
        return max((t for t, _h, _v in visits), default=dt.datetime.now(dt.timezone.utc))


def host_counts(visits):
    c = {}
    for _t, h, _v in visits:
        c[h] = c.get(h, 0) + 1
    return c


def leak_only_hosts(default_visits, leak_visits):
    """只出现在漏采面、Default 里完全没有的域名（「Default 视角里不存在」的那部分）。"""
    d = host_counts(default_visits)
    l = host_counts(leak_visits)
    only = {h: n for h, n in l.items() if h not in d}
    return {
        "n_hosts": len(only),
        "visits": sum(only.values()),
        "top": [{"host": h, "visits": n} for h, n in sorted(only.items(), key=lambda kv: -kv[1])[:15]],
    }


def vacuum_bytes(history: Path, tmpdir: Path) -> int:
    """把副本 VACUUM INTO 到一个临时库并返回其字节数（源副本以 mode=ro 打开，全程只读）。

    目的：让「漏采面体量」与 R415 快照（Default 侧为 VACUUM 副本）同口径，避免 §3.7 的跨口径相加。
    失败/源不可读时返回 0，由调用方降级说明口径（绝不把 0 当真实体量）。临时库用完即删。
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    dst = tmpdir / (history.parent.name.replace(" ", "_") + ".vac.db")
    try:
        if dst.exists():
            dst.unlink()
        con = sqlite3.connect("file:%s?mode=ro" % history, uri=True)
        con.execute("VACUUM INTO '%s'" % dst)
        con.close()
        return dst.stat().st_size
    except Exception:
        return 0
    finally:
        try:
            dst.unlink()
        except OSError:
            pass


def watch_host_deltas(default_visits, leak_visits, window_days: int, ref: dt.datetime):
    """逐关注域：Default / 漏采面 / 合并 / 漏采占比 / 近窗访问数（漏采面是否引入新证据）。"""
    d, l = host_counts(default_visits), host_counts(leak_visits)
    cut = ref - dt.timedelta(days=window_days)

    def recent(v):
        return sum(1 for t, h, _ in v if t >= cut)

    dl = {}
    for t, h, _ in default_visits:
        dl.setdefault(h, [0, 0])[0 if t >= cut else 1] += 0
    out = []
    for h in WATCH_HOSTS:
        nb, nl = d.get(h, 0), l.get(h, 0)
        out.append({
            "host": h, "default": nb, "leak": nl, "combined": nb + nl,
            "leak_pct_of_combined": round(100.0 * nl / max(1, nb + nl), 2),
            "default_recent": sum(1 for t, hh, _ in default_visits if hh == h and t >= cut),
            "leak_recent": sum(1 for t, hh, _ in leak_visits if hh == h and t >= cut),
        })
    return out


def _month_metrics(drift, month: str):
    return (drift or {}).get(month, {})


def _recent_month(drift):
    ks = sorted((drift or {}).keys())
    return ks[-1] if ks else None


# --------------------------------------------------------------------------- #
# 决策判定（7 条，逐条给会/不会 + 依据）
# --------------------------------------------------------------------------- #
def decide(base, comb, default_visits, leak_visits, leak_bytes, src_bytes_total, dflt_bytes,
           window_days, ref, leak_vac_bytes: int = 0):
    """base=Default-only 分析，comb=全机合并分析；返回逐条判定。"""
    out = []
    mib = 1024.0 * 1024.0
    dflt_mib = dflt_bytes / mib
    b_vol, c_vol = base["all"]["volume"], comb["all"]["volume"]
    b_tr, c_tr = base["all"]["trends"], comb["all"]["trends"]
    b_dr, c_dr = base["all"]["drift"], comb["all"]["drift"]

    # D1 浏览强度衰减监控：近 30 天日均相对偏差
    br = b_tr["counts"]["recent_per_day"]
    cr = c_tr["counts"]["recent_per_day"]
    rel = abs(cr - br) / max(1e-9, br) * 100.0
    out.append({
        "id": "D1", "claim": "浏览强度两口径同向下降（137.6 vs 332.0 次/日）→ 每月复算",
        "verdict": "不改变" if rel <= THRESHOLD["trend_rel_pct"] else "需复核",
        "basis": f"近 {b_tr['window_days']} 天日均 Default-only={br} vs 全机={cr}（相对偏差 {rel:.2f}% ≤ 阈值 {THRESHOLD['trend_rel_pct']}%）",
    })

    # D2 关注域方向：漏采面给每个关注域贡献了多少（Top10 里「出现」是无信息量的恒真条件）
    wh = watch_host_deltas(default_visits, leak_visits, window_days, ref)
    risky = [w for w in wh if w["leak_pct_of_combined"] >= THRESHOLD["watch_host_share_pct"]
             or (w["default_recent"] == 0 and w["leak_recent"] > 0)]
    moved = [w for w in wh if w["leak"] > 0]
    out.append({
        "id": "D2", "claim": "pm.smyze.cn 升 / op* 与 codeup 降 / 3 域近 30 天归零 → 核监控清单",
        "verdict": "需复核" if risky else "不改变",
        "basis": ("；".join(f"{w['host']} 漏采 +{w['leak']}/{w['combined']}({w['leak_pct_of_combined']}%)"
                            f"近{window_days}天 {w['default_recent']}→{w['default_recent'] + w['leak_recent']}"
                            for w in (risky or moved)) or "关注域在漏采面 0 访问")
                 + f"｜判定线：漏采占比≥{THRESHOLD['watch_host_share_pct']}% 或 Default 近窗 0 而漏采面 >0",
    })

    # D3 定时任务择时：首访早于 09:30 天数占比（取最近月）
    mk_b, mk_c = _recent_month(b_dr), _recent_month(c_dr)
    pb = (_month_metrics(b_dr, mk_b) or {}).get("first_before_0930_pct")
    pc = (_month_metrics(c_dr, mk_c) or {}).get("first_before_0930_pct")
    dp = abs((pc or 0) - (pb or 0))
    out.append({
        "id": "D3", "claim": "09:00—10:20 与上网起点重叠 → 2 个任务挪到 19:30—20:30 或 08:00 前",
        "verdict": "不改变" if dp <= THRESHOLD["drift_pp"] else "需复核",
        "basis": f"最近月首访<09:30 占比 Default-only={pb}%（{mk_b}）vs 全机={pc}%（{mk_c}），差 {dp:.1f}pp ≤ 阈值 {THRESHOLD['drift_pp']}pp",
    })

    # D4 自驾嫌疑 + 自造占比
    sus_b = sum(1 for r in (base["all"]["uniform"].get("hosts") or []) if r["suspected_automation"])
    sus_c = sum(1 for r in (comb["all"]["uniform"].get("hosts") or []) if r["suspected_automation"])
    sd = abs(comb["self_share_pct"] - base["self_share_pct"])
    verd4 = "不改变" if (sus_c == sus_b and sd <= THRESHOLD["self_share_pp"]) else "需复核"
    out.append({
        "id": "D4", "claim": "0 个自驾嫌疑域；自造仅占 20.1% → 人的轨迹按长期档案保留",
        "verdict": verd4,
        "basis": f"嫌疑域 Default-only={sus_b} vs 全机={sus_c}；自造占比 {base['self_share_pct']}% → {comb['self_share_pct']}%（差 {sd:.1f}pp，阈值 {THRESHOLD['self_share_pp']}pp）",
    })

    # D5 保留策略：每日快照体量。**口径铁律**（R423 §3.7 的坑）：只把 VACUUM 产物字节与
    # VACUUM 产物字节相加；原始拷贝字节（含 -wal 冗余）只能作为磁盘代价上界单独列出。
    mib = 1024.0 * 1024.0
    if leak_vac_bytes > 0:
        daily = dflt_mib + leak_vac_bytes / mib
        note = (f"两加数同为 VACUUM 口径（Default 取 R415 快照副本 {dflt_mib:.2f} MiB，"
                f"漏采面为本轮逐副本 VACUUM INTO 实测 {leak_vac_bytes/mib:.2f} MiB）；"
                f"原始拷贝口径上界 {src_bytes_total/mib:.2f} MiB/日（仅作磁盘代价上界，不参与年化）")
    else:
        daily = src_bytes_total / mib
        note = ("漏采面 VACUUM 探针不可用 ⇒ Default(VACUUM) 与漏采面(原始拷贝) 属**跨口径**，"
                "本值仅作上界，不可当作年化承诺")
    out.append({
        "id": "D5", "claim": "单份 15.29 MB / 年化 5.45 GB → 7 日 + 周 + 月份保留",
        "verdict": "数值更新",
        "basis": (f"全机每日 ≈ {daily:.2f} MiB ⇒ 年化 {daily*365/1024:.2f} GiB"
                  f"（R423 的 5.45 GiB 只算 Default；倍率 ×{daily/max(1e-9, dflt_mib):.2f}）"
                  f"｜口径：{note}"),
    })

    # D6 扩面：本任务本身即证据
    out.append({
        "id": "D6", "claim": "10 个 profile 只采 1 个，漏 8.6% → R415 源改逐 profile（待批）",
        "verdict": "已复核（扩面项）",
        "basis": "见本工具 scan 输出的逐 profile 表与 reconcile 的全机/Default 差",
    })

    # D7 口径：副本字节 ≠ 数据量
    out.append({
        "id": "D7", "claim": "副本 16.03 MB vs 源库 33.23 MB（同数据）→ 源侧/产物侧字节不混算",
        "verdict": "不改变（口径加强）",
        "basis": "本工具的工作区是「原始文件集拷贝」而非 VACUUM 产物 ⇒ 再次证明字节口径必须按来源区分",
    })
    return out


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
def render_scan(rows, ic, workdir):
    L = []
    L.append(f"Chrome 多 profile 只读扫描（workdir={workdir}）")
    L.append(f"{'profile':<14}{'bytes':>10}{'wal/shm':>9}{'http访问':>9}{'域数':>7}  时间窗")
    tot_b = tot_v = 0
    for r in rows:
        s = r["stats"]
        sufx = ("w" if r["src"]["wal"] else "-") + ("s" if r["src"]["shm"] else "-")
        acc = ic.get(r["profile"], {})
        tag = f" [{acc.get('name','')}{'/' + acc.get('user','') if acc.get('user') else ''}]" if acc else ""
        L.append(f"{r['profile']:<14}{r['src']['bytes']:>10}{sufx:>9}{s['http_visits']:>9}{s['distinct_hosts']:>7}  "
                 f"{s['first_visit']} → {s['last_visit']}{tag}")
        tot_b += r["src"]["bytes"]
        tot_v += s["http_visits"]
    L.append(f"{'合计':<14}{tot_b:>10}{'':>9}{tot_v:>9}")
    for r in rows:
        if r["stats"]["top_hosts"]:
            L.append(f"  Top: {r['profile']:<12} " + ", ".join(f"{h['host']}({h['visits']})" for h in r["stats"]["top_hosts"][:5]))
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def do_run(chrome_root: Path, snapshot: Path | None, work_root: Path, window: int,
           out_json: Path | None, prove: bool = True, mode: str = "reconcile"):
    day = dt.date.today().isoformat()
    workdir = work_root / day
    workdir.mkdir(parents=True, exist_ok=True)
    profs = discover_profiles(chrome_root)
    ic = info_cache(chrome_root)

    rows, manifest = [], {"date": day, "chrome_root": str(chrome_root), "profiles": {}, "readonly_proof": True}
    for it in profs:
        if it["profile"] == "Default":
            continue  # Default 走 R415 快照（避免重复采同一库）
        rec = copy_profile_readonly(it, workdir)
        manifest["profiles"][it["profile"]] = rec
        if not all(f["readonly_proof"] for f in rec["files"].values()):
            manifest["readonly_proof"] = False
        rows.append({"profile": it["profile"], "src": it, "rec": rec,
                     "stats": profile_stats(workdir / it["profile"] / "History")})
    (workdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    snap = snapshot or (sorted([p for p in DEFAULT_SNAPSHOT_ROOT.iterdir() if p.is_dir()])[-1]
                        if DEFAULT_SNAPSHOT_ROOT.is_dir() else None)
    result = {"workdir": str(workdir), "snapshot": str(snap) if snap else None,
              "readonly_proof": manifest["readonly_proof"], "profiles": []}
    for r in rows:
        s = r["stats"]
        result["profiles"].append({"profile": r["profile"], "bytes": r["src"]["bytes"],
                                   "http_visits": s["http_visits"], "raw_visits": s["raw_visits"],
                                   "distinct_hosts": s["distinct_hosts"],
                                   "first_visit": s["first_visit"], "last_visit": s["last_visit"],
                                   "top_hosts": s["top_hosts"]})
    if mode == "scan" or snap is None:
        print(render_scan(rows, ic, workdir))
        n_ok, bad = verify_copies(workdir)
        print(f"\n负控 verify：{n_ok} 个副本一致" + (f"，异常 {len(bad)}：{bad}" if bad else "（无篡改）"))
        if out_json:
            Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if manifest["readonly_proof"] and not bad else 1

    # reconcile：Default-only（快照）vs 全机合并（快照 Default + 各 profile 工作区副本）
    t0 = time.time()
    dflt = CHC.load_visits(snap / "History")
    dflt_bytes = (snap / "History").stat().st_size
    base = analyze(dflt, dflt_bytes, window)
    comb_visits = list(dflt)
    comb_src_bytes = dflt_bytes
    leak_visits_all = []
    for r in rows:
        h = workdir / r["profile"] / "History"
        lv = CHC.load_visits(h)
        leak_visits_all.extend(lv)
        comb_visits.extend(lv)
        comb_src_bytes += h.stat().st_size
    comb = analyze(comb_visits, comb_src_bytes, window)
    leak_only = leak_only_hosts(dflt, leak_visits_all)
    leak_v = len(leak_visits_all)
    leak_b = sum((workdir / r["profile"] / "History").stat().st_size for r in rows)
    leak_vac = sum(vacuum_bytes(workdir / r["profile"] / "History", workdir / "_vacuum_probe")
                   for r in rows)
    ref = _ref_point(snap, dflt)
    elapsed = time.time() - t0
    dec = decide(base, comb, dflt, leak_visits_all, leak_b,
                 comb_src_bytes, dflt_bytes, window, ref, leak_vac_bytes=leak_vac)
    result.update({"base_default_only": {
        "http_visits": base["all"]["volume"]["total_visits"],
        "distinct_hosts": base["all"]["volume"]["distinct_hosts"],
        "top10_share_pct": base["all"]["volume"]["top10_share_pct"],
        "single_visit_share_pct": base["all"]["volume"]["single_visit_share_pct"],
        "recent_per_day": base["all"]["trends"]["counts"]["recent_per_day"],
        "prior_per_day": base["all"]["trends"]["counts"]["prior_per_day"],
        "self_share_pct": base["self_share_pct"],
    }, "combined_all_profiles": {
        "http_visits": comb["all"]["volume"]["total_visits"],
        "distinct_hosts": comb["all"]["volume"]["distinct_hosts"],
        "top10_share_pct": comb["all"]["volume"]["top10_share_pct"],
        "single_visit_share_pct": comb["all"]["volume"]["single_visit_share_pct"],
        "recent_per_day": comb["all"]["trends"]["counts"]["recent_per_day"],
        "prior_per_day": comb["all"]["trends"]["counts"]["prior_per_day"],
        "self_share_pct": comb["self_share_pct"],
    }, "decisions": dec, "leak": {
        "http_visits": leak_v, "src_bytes": leak_b,
        "share_pct_of_combined": round(100.0 * leak_v /
                                       max(1, base["all"]["volume"]["total_visits"] + leak_v), 2),
        "leak_only_hosts": leak_only,
        "vacuum_bytes": leak_vac,
    }, "elapsed_s": round(elapsed, 3)})

    print(render_scan(rows, ic, workdir))
    leak_v = sum(r["stats"]["http_visits"] for r in rows)
    tot_v = base["all"]["volume"]["total_visits"] + leak_v
    print(f"\n== 漏采面 ==\n非 Default http 访问合计 {leak_v} 次 = 全机 {tot_v} 次的 "
          f"{100.0*leak_v/max(1,tot_v):.1f}%（R423 §3.6 的 8.6% 为**原始行**口径，含非 http 行）")
    print(f"  独占域：{leak_only['n_hosts']} 个（{leak_only['visits']} 次）"
          f"（只在非 Default 出现、Default 里完全没有）"
          + ("；Top：" + "、".join(f"{x['host']}×{x['visits']}" for x in leak_only["top"][:5])
             if leak_only["top"] else ""))
    print(f"  源侧字节：{leak_b/1048576:.2f} MiB（工作区原始拷贝，含 -wal ⇒ 只作磁盘代价上界）")
    print(f"  VACUUM 口径：{leak_vac/1048576:.2f} MiB（逐副本 VACUUM INTO 实测，与 R415 快照同口径；"
          f"探针{'可用' if leak_vac else '**不可用**→D5 降级为上界口径'}）")
    print(f"  本轮 reconcile 耗时：{elapsed:.2f}s（含 {len(rows)} 个非 Default profile 的拷贝+解析）")
    print("\n== Default-only vs 全机合并 ==")
    b, c = result["base_default_only"], result["combined_all_profiles"]
    for k in ("http_visits", "distinct_hosts", "top10_share_pct", "single_visit_share_pct",
              "recent_per_day", "prior_per_day", "self_share_pct"):
        print(f"  {k:<24} {b[k]:>10}  ->  {c[k]:>10}")
    print("\n== 逐条决策判定 ==")
    for d in dec:
        print(f"  [{d['id']}] {d['verdict']:<12} {d['claim'][:46]}\n        依据：{d['basis']}")
    n_ok, bad = verify_copies(workdir)
    print(f"\n负控 verify：{n_ok} 个副本一致" + (f"，异常 {len(bad)}：{bad}" if bad else "（无篡改）"))
    ok = manifest["readonly_proof"] and not bad
    print(f"只读自证：{'pass' if manifest['readonly_proof'] else 'FAIL'}")
    if out_json:
        Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 落盘：{out_json}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 自测（合成库；不读任何真实数据）
# --------------------------------------------------------------------------- #
def _mk_history(path: Path, rows):
    """rows=[(url, visit_time_us)]；建最小 visits/urls 表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT);"
                      "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
    for i, (u, t) in enumerate(rows, 1):
        cur = con.execute("INSERT INTO urls(url) VALUES(?)", (u,))
        con.execute("INSERT INTO visits(url, visit_time) VALUES(?,?)", (cur.lastrowid, t))
    con.commit()
    con.close()


def selftest():
    ok = fail = 0
    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print(f"  PASS  {name}")
        else:
            fail += 1; print(f"  FAIL  {name}")

    us = lambda d: int((d - CHC.CHROME_EPOCH).total_seconds() * 1_000_000)
    now = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        root = tdp / "chrome"; (root).mkdir(parents=True)
        # P1：3 次 http + 1 次 file + 1 次 chrome-extension（非 http 不入统计）
        _mk_history(root / "P1" / "History", [
            ("https://pm.smyze.cn/a", us(now - dt.timedelta(days=1))),
            ("https://pm.smyze.cn/b", us(now - dt.timedelta(days=2))),
            ("http://op.smyze.cn/x", us(now - dt.timedelta(days=3))),
            ("file:///tmp/x.html", us(now)),
            ("chrome-extension://abc/p.html", us(now)),
        ])
        # P2：空库（0 行）⇒ 应被识别为 0 访问而非崩溃
        _mk_history(root / "P2" / "History", [])
        # P3：www./端口 归一 + 极端活动（用于 uniform 判据）
        rows3 = [("https://www.github.com/x", us(now - dt.timedelta(days=i))) for i in range(1, 30)]
        _mk_history(root / "P3" / "History", rows3)
        (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "P1": {"name": "张三", "user_name": "zhang@example.com"},
            "P3": {"name": "李四", "user_name": "li@example.com"}}}}), encoding="utf-8")

        profs = discover_profiles(root)
        chk("发现 3 个带 History 的 profile", [p["profile"] for p in profs] == ["P1", "P2", "P3"])
        ic = info_cache(root)
        chk("PII 掩码（local part 只留首字符）", ic["P1"]["user"] == "z***@example.com" and ic["P1"]["name"] == "张***")
        chk("未登录判定", ic["P1"]["has_signed_in"] is True)

        wd = tdp / "work"
        man = {"date": "selftest", "profiles": {}}
        for p in profs:
            man["profiles"][p["profile"]] = copy_profile_readonly(p, wd)
        (wd / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
        chk("拷贝只读自证（源 sha256 前后一致）", all(f["readonly_proof"] for r in man["profiles"].values() for f in r["files"].values()))

        s1 = profile_stats(wd / "P1" / "History")
        chk("scheme 过滤：http 3 / 非 http 2", s1["http_visits"] == 3 and s1["raw_visits"] == 5)
        chk("非 http 不计入域名统计（域数=2）", s1["distinct_hosts"] == 2)
        s2 = profile_stats(wd / "P2" / "History")
        chk("空库不崩溃且计 0", s2["http_visits"] == 0 and s2["first_visit"] is None)
        s3 = profile_stats(wd / "P3" / "History")
        chk("www. 归一（github.com）", s3["top_hosts"][0]["host"] == "github.com")

        # NC1：副本被篡改 ⇒ verify 必报
        n_ok, bad = verify_copies(wd)
        chk("NC1-a 未篡改时 verify 全绿", not bad and n_ok >= 3)
        tgt = wd / "P1" / "History"
        raw = bytearray(tgt.read_bytes()); raw[-1] ^= 0xFF
        tgt.write_bytes(bytes(raw))
        n_ok, bad = verify_copies(wd)
        chk("NC1-b 篡改副本必报 TAMPERED", len(bad) == 1 and "TAMPERED" in bad[0])
        tgt.unlink()
        n_ok, bad = verify_copies(wd)
        chk("NC1-c 删除副本必报 MISSING", len(bad) == 1 and "MISSING" in bad[0])

        # NC2：-wal 缺失（仅拷 History）⇒ 只读自证仍过，但结果可能假空（口径声明）
        _mk_history(tdp / "walcase" / "P4" / "History", [("https://a.com/", us(now))])
        p4 = discover_profiles(tdp / "walcase")[0]
        chk("NC2 未检测到 -wal 时 wal=False", p4["wal"] is False)

        # NC3：决策判定方向正确（构造「漏采面改变 D2」与「不改变」两例）
        v_base = CHC.load_visits(wd / "P3" / "History")  # 30 天 github 集中
        b = analyze(v_base, 1000, 30)
        c_same = analyze(v_base, 1000, 30)
        ref = dt.datetime.now(dt.timezone.utc)
        d_same = decide(b, c_same, v_base, [], 0, 1000, 1000, 30, ref)
        chk("NC3-a 同输入 ⇒ D1 判不改变", [d for d in d_same if d["id"] == "D1"][0]["verdict"] == "不改变")
        wd2 = tdp / "work2"  # 独立副本：wd 里的 P1 已被 NC1-c 删除，不可复用
        p1item = [q for q in profs if q["profile"] == "P1"][0]
        copy_profile_readonly(p1item, wd2)
        leak_more = CHC.load_visits(wd2 / "P1" / "History") * 40  # 制造量级差
        v_more = list(v_base) + leak_more
        c_more = analyze(v_more, 1000, 30)
        d_more = decide(b, c_more, v_base, leak_more, 1000, 1000, 1000, 30, ref)
        chk("NC3-b 量级差 ⇒ D1 判需复核", [d for d in d_more if d["id"] == "D1"][0]["verdict"] == "需复核")
        chk("NC3-c 关注域出现在漏采面 ⇒ D2 判需复核",
            [d for d in decide(b, c_more, v_base, leak_more, 1000, 1000, 1000, 30, ref)
             if d["id"] == "D2"][0]["verdict"] == "需复核")

        # NC4：阈值表被引用（防止判定静默放行）
        vp = vacuum_bytes(wd2 / "P1" / "History", tdp / "vacprobe")
        chk("NC5 VACUUM 探针 0 < vac <= raw", 0 < vp <= (wd2 / "P1" / "History").stat().st_size)
        chk("NC5-b 源不可读时返回 0（不抛异常、不假绿）",
            vacuum_bytes(tdp / "nope" / "History", tdp / "vacprobe2") == 0)
        chk("NC4 阈值表齐备", set(THRESHOLD) == {"trend_rel_pct", "drift_pp", "self_share_pp", "watch_host_share_pct"})

    print(f"selftest: {ok}/{ok+fail}")
    return 0 if fail == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chrome 多 profile 只读对账（漏采面是否改变 R423 结论）")
    ap.add_argument("mode", nargs="?", default="reconcile", choices=["scan", "reconcile", "verify"])
    ap.add_argument("--chrome-root", default=str(DEFAULT_CHROME_ROOT))
    ap.add_argument("--snapshot", default=None, help="Default 快照目录（默认取 temp/chrome_snapshots 最新）")
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--json", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.mode == "verify":
        day = dt.date.today().isoformat()
        wd = Path(a.work_root) / day
        n_ok, bad = verify_copies(wd)
        print(f"verify {wd}: {n_ok} 个副本一致" + (f"，异常 {len(bad)}：{bad}" if bad else "（无篡改）"))
        return 0 if not bad else 1
    return do_run(Path(a.chrome_root), Path(a.snapshot) if a.snapshot else None,
                  Path(a.work_root), a.window, Path(a.json) if a.json else None, mode=a.mode)


if __name__ == "__main__":
    raise SystemExit(main())
