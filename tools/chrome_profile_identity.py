#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chrome 多 profile「身份分流证据表」（R437）——把 R430 的 8.48% 漏采面归到具体 profile。

背景
----
R423（`tools/chrome_history_consume.py`）只消费 Default 的 `History`；R430
（`tools/chrome_profiles_consume.py`）量出「本机 10 个目录带 History，非 Default 在 91 天
窗口 2788 次 http = 全机 8.48%」。但 R430 是一份**判决性对账**（会不会改变 R423 的 7 条
决策），没有回答「这 8.48% 到底是谁的」。本工具补的是**事实层**：

    每个 profile 的「身份证据」长什么样，以及这 8.48% 如何分流到各 profile。

设计约束（对齐 TODO#11 的 5 条验收）
------------------------------------
1. **只读**：源侧文件（`Local State`、各 profile 的 `Preferences`、`Extensions/` 目录清单）
   一律只以只读方式读取，读取前后各取一次 sha256 作为 `readonly_proof`；`History` 一律走
   R430 已冻结的**副本**（`temp/chrome_profiles/<day>/<profile>/History`，`mode=ro` 打开）
   或 R415 的 Default 快照副本，绝不以可写方式打开源库。
2. **隐私（验收①）**：输出里**不落** `gaia_id` / 邮箱 / `user_name` / `gaia_name` 原文，
   只留「已登录=是/否」+「脱敏显示名」（首字符 + `***`）/「账号类型（托管与否）」。
   硬敏感文件（`Login Data` / `Login Data For Account` / `Web Data` / `Affiliation Database`
   / `Cookies`）**读到即拒绝**（`SensitiveRefusal`，rc=2）——见验收②的负控。
3. **证据表（验收③）**：每一行给出「数据来源文件路径」；表下附「证据出处」段逐字段列出
   来源文件 + 现取命令（可复跑）。
4. **对账（验收④）**：逐 profile 的 `http_visits` / `raw_visits` / `distinct_hosts` /
   `first_visit` / `last_visit` 与 R430 的 JSON 基线**逐字比对**（±0），不一致即
   `MISMATCH` 且 rc=3。
5. **不做用途猜测（验收⑤）**：表内**没有**「用途假设」列（`--selftest` 用负控钉死这一点）；
   任何推测只能写在报告 R437 的结论段，并逐条标注证据强度。

用法
----
    python3 tools/chrome_profile_identity.py report                 # 出证据表（md 到 stdout）
    python3 tools/chrome_profile_identity.py report --json OUT.json --out-md OUT.md
    python3 tools/chrome_profile_identity.py verify                 # 复核副本未被篡改（负控）
    python3 tools/chrome_profile_identity.py _probe <path>          # 自测钩子：读该路径（敏感即拒绝）
    python3 tools/chrome_profile_identity.py --selftest             # 合成夹具自测（含负控）

复用（零重写）
--------------
- `tools/chrome_history_consume.py`（R423）：`load_visits` / `chrome_to_dt` / `norm_host` /
  `sha256_file`
- `tools/chrome_profiles_consume.py`（R430）：`mask` / `discover_profiles` /
  `copy_profile_readonly` / `verify_copies`
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
import subprocess
import sys
import tempfile
from pathlib import Path

GA_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHROME_ROOT = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
DEFAULT_WORK_ROOT = GA_ROOT / "temp" / "chrome_profiles"
DEFAULT_SNAPSHOT_ROOT = GA_ROOT / "temp" / "chrome_snapshots"
DEFAULT_BASELINE = GA_ROOT / "temp" / "chrome_profiles" / "consume_2026-09-26.json"

# 硬敏感（密码 / 自动填充 / 支付 / 会话凭据）：读到即拒绝，不做任何降级
SENSITIVE_BASENAMES = (
    "login data",
    "login data-journal",
    "login data for account",
    "login data for account-journal",
    "web data",
    "web data-journal",
    "affiliation database",
    "cookies",
    "cookies-journal",
)

# 明确允许的读取面（白名单，写出来是为了让「为什么不读别的」可审计）
ALLOWED_BASENAMES = ("local state", "preferences", "history")


class SensitiveRefusal(RuntimeError):
    """读到硬敏感文件（密码/自动填充/凭据）时的 fail-closed 异常。"""


# --------------------------------------------------------------------------- #
# 复用内核（零重写）
# --------------------------------------------------------------------------- #
def _load_mod(name: str, filename: str):
    p = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CHC = _load_mod("chc_r423_identity", "chrome_history_consume.py")   # R423
CPC = _load_mod("cpc_r430_identity", "chrome_profiles_consume.py")  # R430
mask = CPC.mask
sha256_file = CHC.sha256_file


# --------------------------------------------------------------------------- #
# 只读 + 隐私闸门（所有读取必须经过这里）
# --------------------------------------------------------------------------- #
def sensitive_hit(path) -> str | None:
    """返回命中的敏感文件名（小写），否则 None。检查全部路径分量。"""
    for part in Path(path).parts:
        low = part.strip().lower()
        if low in SENSITIVE_BASENAMES or low.endswith("-journal") and low[:-8] in SENSITIVE_BASENAMES:
            return part
    return None


def guard(path) -> None:
    path = Path(path)  # CLI 传入的是 str：先归一，否则 path.parent 会 AttributeError
    hit = sensitive_hit(path)
    if hit:
        raise SensitiveRefusal(
            f"拒绝读取硬敏感文件 {path.parent.name}/{hit}（密码/自动填充/凭据面）"
        )


def read_text_safe(path, limit: int = 8 << 20) -> str:
    guard(path)
    p = Path(path)
    if p.stat().st_size > limit:
        raise SensitiveRefusal(f"文件超过只读读取上限（{limit} B）：{p}")
    return p.read_text(encoding="utf-8")


def read_json_safe(path):
    return json.loads(read_text_safe(path))


def listdir_safe(path):
    guard(path)
    return sorted(os.listdir(path))


def sqlite_query(path, sql: str, params=()):
    """只读打开副本/快照，禁止源库写打开。"""
    guard(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 身份抽取（只出「事实」与「掩码」，不出原文）
# --------------------------------------------------------------------------- #
def identity_of(rec: dict) -> dict:
    """从 Local State 的一条 info_cache 记录抽出可输出的身份事实（全部脱敏）。"""
    rec = rec or {}
    user_name = (rec.get("user_name") or "").strip()
    gaia_name = (rec.get("gaia_name") or "").strip()
    gaia_id = (rec.get("gaia_id") or "").strip()
    hosted = (rec.get("hosted_domain") or "").strip()
    signed_in = bool(user_name or gaia_name or gaia_id)
    at = rec.get("active_time")
    last_active = None
    if isinstance(at, (int, float)) and at > 0:
        last_active = dt.datetime.fromtimestamp(float(at)).astimezone().strftime("%Y-%m-%d %H:%M")
    return {
        "display_name_masked": mask(rec.get("name") or ""),
        "signed_in": signed_in,
        "account_kind": "托管" if (hosted and hosted.upper() != "NO_HOSTED_DOMAIN") else "非托管",
        "last_active_info_cache": last_active,
        "last_active_epoch": float(at) if isinstance(at, (int, float)) and at > 0 else None,
        "is_managed": bool(rec.get("is_managed")),
        "is_ephemeral": bool(rec.get("is_ephemeral")),
        # 以下三项只以布尔/计数出现，绝不输出值本身
        "has_gaia_id": bool(gaia_id),
        "has_user_name": bool(user_name),
        "has_gaia_name": bool(gaia_name),
    }


def search_engine_of(prefs_path: Path) -> dict:
    """默认搜索引擎：只取 short_name 与 keyword（非 PII）。"""
    out = {"short_name": None, "keyword": None, "source_ok": False}
    try:
        prefs = read_json_safe(prefs_path)
    except Exception as e:
        out["error"] = type(e).__name__
        return out
    turl = ((prefs.get("default_search_provider_data") or {}).get("template_url_data") or {})
    out["short_name"] = turl.get("short_name")
    out["keyword"] = turl.get("keyword")
    out["source_ok"] = True
    return out


def ext_count(profile_dir: Path) -> int | None:
    """扩展数 = Extensions/ 下的一级子目录数（= 扩展 ID 数）；只列目录名不读内容。"""
    ext = profile_dir / "Extensions"
    if not ext.is_dir():
        return None
    return len(listdir_safe(ext))


def visits_stats(history: Path) -> dict:
    raw = sqlite_query(history, "SELECT COUNT(*) FROM visits")[0][0]
    visits = CHC.load_visits(Path(history))
    hosts: dict[str, int] = {}
    for t, h, _v in visits:
        hosts[h] = hosts.get(h, 0) + 1
    top = sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    first = min((v[0] for v in visits), default=None)
    last = max((v[0] for v in visits), default=None)
    return {
        "http_visits": len(visits),
        "raw_visits": raw,
        "distinct_hosts": len(hosts),
        "top_hosts": [{"host": h, "visits": n} for h, n in top],
        "first_visit": first.strftime("%Y-%m-%d %H:%M") if first else None,
        "last_visit": last.strftime("%Y-%m-%d %H:%M") if last else None,
    }


# --------------------------------------------------------------------------- #
# 副本准备（复用 R430：已有冻结副本则校验后复用）
# --------------------------------------------------------------------------- #
def ensure_copies(chrome_root: Path, work_root: Path, day: str):
    """返回 (copies_dir, mode, verify_note)。有 manifest 则复核 sha256 复用，否则真拷。"""
    wd = work_root / day
    man = wd / "manifest.json"
    if man.exists():
        n_ok, bad = CPC.verify_copies(wd)
        return wd, "reuse(R430 冻结副本)", {"verified": n_ok, "bad": bad}
    profiles = CPC.discover_profiles(chrome_root)
    recs = {}
    for item in profiles:
        rec = CPC.copy_profile_readonly(item, wd)
        recs[item["profile"]] = rec
    (wd).mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"date": day, "chrome_root": str(chrome_root),
                               "profiles": recs, "readonly_proof": True},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return wd, f"fresh copy -> {wd}", {"verified": len(recs), "bad": []}


def default_snapshot() -> Path | None:
    if not DEFAULT_SNAPSHOT_ROOT.is_dir():
        return None
    cands = sorted([p for p in DEFAULT_SNAPSHOT_ROOT.iterdir()
                    if p.is_dir() and (p / "History").exists()])
    return cands[-1] if cands else None


# --------------------------------------------------------------------------- #
# 主流程：证据表
# --------------------------------------------------------------------------- #
def build_rows(chrome_root: Path, copies_dir: Path, snap: Path | None,
               baseline: dict | None):
    local_state = chrome_root / "Local State"
    proof = {}
    for label, p in (("Local State", local_state),):
        proof[label] = {"path": str(p), "before": sha256_file(p) if p.exists() else None}
    st = read_json_safe(local_state)
    ic = (st.get("profile", {}) or {}).get("info_cache", {}) or {}

    prof_items = {it["profile"]: it for it in CPC.discover_profiles(chrome_root)}
    extra_dirs = sorted(set(prof_items) - set(ic))
    missing_dirs = sorted(set(ic) - set(prof_items))

    base_by_profile = {}
    if baseline:
        for r in baseline.get("profiles", []) or []:
            base_by_profile[r.get("profile")] = r
    base_default = (baseline or {}).get("base_default_only") or {}
    base_combined = (baseline or {}).get("combined_all_profiles") or {}

    rows = []
    for profile in sorted(ic):
        pdir = chrome_root / profile
        hist = copies_dir / profile / "History"
        row = {"profile": profile, "identity": identity_of(ic[profile])}
        row["in_dir"] = pdir.is_dir()
        row["history_copy"] = str(hist) if hist.exists() else None

        # 扩展数 / 搜索引擎（源侧只读）
        row["ext_count"] = ext_count(pdir) if pdir.is_dir() else None
        prefs = pdir / "Preferences"
        if prefs.exists():
            pb = sha256_file(prefs)
            row["search_engine"] = search_engine_of(prefs)
            row["search_engine"]["readonly_proof"] = (pb == sha256_file(prefs))
        else:
            row["search_engine"] = {"short_name": None, "keyword": None, "source_ok": False}

        # 访问数：Default 走 R415 快照副本；其余走 R430 冻结副本
        if profile == "Default" and snap is not None:
            stats = visits_stats(snap / "History")
            row["visits_source"] = str(snap / "History")
            row["visits_kind"] = "R415 Default 快照副本（VACUUM 产物）"
        elif hist.exists():
            stats = visits_stats(hist)
            row["visits_source"] = str(hist)
            row["visits_kind"] = "R430 逐 profile 冻结副本（原始拷贝）"
        else:
            stats = {"http_visits": None, "raw_visits": None, "distinct_hosts": None,
                     "top_hosts": [], "first_visit": None, "last_visit": None}
            row["visits_source"] = None
            row["visits_kind"] = "缺副本"
        row.update(stats)

        # 与 R430 基线逐年/逐字比对（±0）
        b = base_by_profile.get(profile)
        if b is None and profile == "Default":
            b = base_default if base_default else None
        row["baseline"] = {
            "http_visits": (b or {}).get("http_visits"),
            "raw_visits": (b or {}).get("raw_visits"),
            "distinct_hosts": (b or {}).get("distinct_hosts"),
            "first_visit": (b or {}).get("first_visit"),
            "last_visit": (b or {}).get("last_visit"),
        } if b else None
        mism = []
        compared = 0
        if b:
            # 基线缺失字段(None)不计入对账：base_default_only 只给 http_visits/distinct_hosts,
            # 若把缺字段当 0/None 比对会制造「假失配」（实测 3 处）。
            for k in ("http_visits", "raw_visits", "distinct_hosts",
                      "first_visit", "last_visit"):
                bv = b.get(k)
                if bv is None:
                    continue
                compared += 1
                if row.get(k) != bv:
                    mism.append(f"{k}: ours={row.get(k)} base={bv}")
        row["mismatches"] = mism
        row["compared_fields"] = compared
        rows.append(row)

    for label, p in (("Local State", local_state),):
        proof[label]["after"] = sha256_file(p) if p.exists() else None
        proof[label]["readonly_proof"] = proof[label]["before"] == proof[label]["after"]

    total_http = sum(r["http_visits"] or 0 for r in rows)
    compared_total = sum(r.get("compared_fields", 0) for r in rows)
    for r in rows:
        r["share_of_table_pct"] = round(100.0 * (r["http_visits"] or 0) / total_http, 2) if total_http else None
        base = r.get("baseline") or {}
        r["share_of_combined_pct"] = (round(100.0 * (r["http_visits"] or 0)
                                           / base_combined["http_visits"], 2)
                                      if base_combined.get("http_visits") else None)
    return {
        "chrome_root": str(chrome_root),
        "copies_dir": str(copies_dir),
        "snapshot": str(snap) if snap else None,
        "generated_at": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "readonly_proof": proof,
        "rows": rows,
        "table_http_total": total_http,
        "baseline_combined_http": base_combined.get("http_visits"),
        "baseline_leak": (baseline or {}).get("leak"),
        "info_cache_count": len(ic),
        "extra_history_dirs": extra_dirs,      # 有 History 但不在 info_cache（如 Guest Profile）
        "info_cache_without_dir": missing_dirs,
        "mismatch_total": sum(len(r["mismatches"]) for r in rows),
        "compared_total": compared_total,   # 实际参与对账的字段数（基线缺字段不计）
    }


COLUMNS = [
    "profile", "显示名(脱敏)", "已登录", "账号类型", "最后活跃(profile.active_time)",
    "最后访问(History)", "http访问", "占本表%", "占全机%", "原始visits", "域数",
    "Top3 域名", "扩展数", "默认搜索引擎", "来源",
]


def render_md(res: dict) -> str:
    L = []
    L.append(f"# Chrome profile 身份分流证据表（{res['generated_at']}）")
    L.append("")
    L.append(f"- Chrome 根：`{res['chrome_root']}`")
    L.append(f"- 副本目录：`{res['copies_dir']}`")
    L.append(f"- Default 快照：`{res['snapshot']}`")
    L.append(f"- info_cache 条数：{res['info_cache_count']}；本表行数：{len(res['rows'])}")
    L.append("")
    L.append("| " + " | ".join(COLUMNS) + " |")
    L.append("|" + "|".join(["---"] * len(COLUMNS)) + "|")
    for r in res["rows"]:
        i = r["identity"]
        top3 = "、".join(f"{h['host']}({h['visits']})" for h in r["top_hosts"]) or "—"
        src = f"`{r['visits_source']}`" if r["visits_source"] else "—"
        L.append("| " + " | ".join([
            r["profile"],
            i["display_name_masked"] or "—",
            "是" if i["signed_in"] else "否",
            i["account_kind"],
            i["last_active_info_cache"] or "—",
            r["last_visit"] or "—",
            str(r["http_visits"]) if r["http_visits"] is not None else "—",
            f"{r['share_of_table_pct']}%" if r["share_of_table_pct"] is not None else "—",
            f"{r['share_of_combined_pct']}%" if r["share_of_combined_pct"] is not None else "—",
            str(r["raw_visits"]) if r["raw_visits"] is not None else "—",
            str(r["distinct_hosts"]) if r["distinct_hosts"] is not None else "—",
            top3,
            "—" if r["ext_count"] is None else str(r["ext_count"]),
            (r["search_engine"] or {}).get("short_name") or "—",
            src,
        ]) + " |")
    L.append("")
    L.append(f"- 本表 http 合计：**{res['table_http_total']}** ｜ R430 全机基线："
             f"**{res['baseline_combined_http']}** ｜ 逐行对账 mismatch：**{res['mismatch_total']}**"
             f"（参与对账字段数 {res.get('compared_total', 0)}；基线未提供的字段不计入，避免假失配）")
    if res["extra_history_dirs"]:
        L.append(f"- 附注：另有含 History 但不在 `info_cache` 的目录（无身份面，未入表）="
                 f"{res['extra_history_dirs']}")
    if res["info_cache_without_dir"]:
        L.append(f"- 附注：info_cache 有条目但磁盘无 profile 目录 = {res['info_cache_without_dir']}")
    L.append("")
    L.append("## 证据出处（逐字段 → 文件 → 现取命令）")
    L.append("")
    L.append("| 字段 | 来源文件 | 现取命令 |")
    L.append("|---|---|---|")
    L.append(f"| 显示名/登录态/最后活跃/账号类型 | `{res['chrome_root']}/Local State` "
             f"→ `profile.info_cache` | `python3 -c \"import json;d=json.load(open('Local State'));"
             f"print(list(d['profile']['info_cache']))\"` |")
    L.append(f"| 默认搜索引擎 | `{res['chrome_root']}/<profile>/Preferences` "
             f"→ `default_search_provider_data.template_url_data.short_name` | "
             f"`python3 -c \"import json;print(json.load(open('Preferences'))"
             f"['default_search_provider_data']['template_url_data']['short_name'])\"` |")
    L.append(f"| 扩展数 | `{res['chrome_root']}/<profile>/Extensions/*/`（只列一级子目录名） | "
             f"`ls \"$CHROME_ROOT/Default/Extensions\" \\| wc -l` |")
    L.append(f"| http访问/域数/Top3/时间窗 | Default：`{res['snapshot']}/History`；"
             f"其余：`{res['copies_dir']}/<profile>/History` | "
             f"`python3 tools/chrome_profile_identity.py report`（内部走 "
             f"`chrome_history_consume.load_visits`）；副本 sha256 复核 "
             f"`python3 tools/chrome_profiles_consume.py verify` |")
    L.append(f"| 全机占比分母 | R430 基线 JSON 的 `combined_all_profiles.http_visits` "
             f"= {res['baseline_combined_http']} | 见上表末行 |")
    L.append("")
    L.append(f"复跑：`python3 tools/chrome_profile_identity.py report "
             f"--chrome-root \"{res['chrome_root']}\" --copies-dir {res['copies_dir']}"
             + (f" --snapshot {res['snapshot']}" if res["snapshot"] else "")
             + " --out-md <OUT.md> --json <OUT.json>`")
    return "\n".join(L) + "\n"


def do_report(a) -> int:
    chrome_root = Path(a.chrome_root)
    if not (chrome_root / "Local State").exists():
        print(f"[ERR] 找不到 Local State：{chrome_root}/Local State", file=sys.stderr)
        return 2
    baseline = None
    if a.baseline and Path(a.baseline).exists():
        baseline = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
    day = a.day or dt.date.today().isoformat()
    if a.copies_dir:
        copies_dir = Path(a.copies_dir)
        if not (copies_dir / "manifest.json").exists():
            print(f"[ERR] --copies-dir 需含 manifest.json（否则 History 缺失会被静默算成 0 访问）：{copies_dir}",
                  file=sys.stderr)
            return 2
        n_ok, bad = CPC.verify_copies(copies_dir)
        if bad:
            print(f"[ERR] 指定副本目录校验失败（篡改/截断/缺失 {len(bad)} 项）："
                  f"{bad[:3]}", file=sys.stderr)
            return 2
        mode, vnote = "指定目录", {"verified": n_ok, "bad": bad}
    else:
        copies_dir, mode, vnote = ensure_copies(chrome_root, Path(a.work_root), day)
    snap = Path(a.snapshot) if a.snapshot else default_snapshot()
    if snap is not None and snap.is_file():
        # 容错：--snapshot 既可给「含 History 的目录」也可直接给 History 文件本身
        snap = snap.parent
    res = build_rows(chrome_root, copies_dir, snap, baseline)
    res["copies_mode"] = mode
    res["copies_verify"] = vnote
    md = render_md(res)
    print(md)
    print(f"[info] 副本模式：{mode}；校验：{vnote}")
    print(f"[readonly] Local State sha256 前后一致 = "
          f"{res['readonly_proof']['Local State']['readonly_proof']}")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[info] JSON -> {a.json}")
    if a.out_md:
        Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out_md).write_text(md, encoding="utf-8")
        print(f"[info] MD -> {a.out_md}")
    if res["mismatch_total"]:
        print(f"[MISMATCH] 与 R430 基线不一致 {res['mismatch_total']} 处", file=sys.stderr)
        return 3
    return 0


# --------------------------------------------------------------------------- #
# 自测（合成夹具；含负控）
# --------------------------------------------------------------------------- #
def _mk_history(path: Path, urls):
    CHROME_EPOCH = CHC.CHROME_EPOCH
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
        con.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
        for i, (u, when) in enumerate(urls, start=1):
            con.execute("INSERT INTO urls VALUES (?,?)", (i, u))
            us = int((when - CHROME_EPOCH).total_seconds() * 1_000_000)
            con.execute("INSERT INTO visits VALUES (?,?,?)", (i, i, us))
        con.commit()
    finally:
        con.close()


def _fixture(tmp: Path, bad_baseline: bool = False, sparse_baseline: bool = False):
    root = tmp / "Chrome"
    (root / "Default" / "Extensions" / "aaaa").mkdir(parents=True)
    (root / "Default" / "Extensions" / "bbbb").mkdir(parents=True)
    (root / "Profile 3").mkdir(parents=True)
    (root / "Guest Profile").mkdir(parents=True)
    # 硬敏感文件（只用于负控：任何读取都必须被拒）
    for name in ("Login Data", "Web Data"):
        (root / "Default" / name).write_text("SECRET-PASSWORD-TABLE", encoding="utf-8")
    (root / "Default" / "Network").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Network" / "Cookies").write_text("SECRET-COOKIE", encoding="utf-8")
    # Local State（含真 PII：邮箱 + gaia_id）
    ls = {
        "profile": {"info_cache": {
            "Default": {"name": "Zed", "user_name": "zzz@secret-domain.test",
                        "gaia_name": "Zed Secret", "gaia_id": "999888777666555444333",
                        "hosted_domain": "NO_HOSTED_DOMAIN",
                        "active_time": 1790440984.426077, "is_managed": 0, "is_ephemeral": False},
            "Profile 3": {"name": "Nemo", "user_name": "", "gaia_name": "", "gaia_id": "",
                          "hosted_domain": "corp.example", "active_time": 1700000000.0,
                          "is_managed": 1, "is_ephemeral": False},
        }}
    }
    (root / "Local State").write_text(json.dumps(ls, ensure_ascii=False), encoding="utf-8")
    prefs = {"default_search_provider_data": {"template_url_data": {
        "short_name": "DuckDuckGo", "keyword": "duck"}}}
    for prof in ("Default", "Profile 3"):
        (root / prof / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    now = dt.datetime.now().astimezone()
    _mk_history(root / "Default" / "History",
                [("https://a.test/x", now), ("https://b.test/y", now - dt.timedelta(days=1)),
                 ("https://b.test/z", now - dt.timedelta(days=2))])
    _mk_history(root / "Profile 3" / "History", [("https://c.test/1", now)])
    _mk_history(root / "Guest Profile" / "History", [])
    # 基线（默认与实测一致；bad_baseline 用于负控）
    fmt = "%Y-%m-%d %H:%M"
    t_first, t_last = (now - dt.timedelta(days=2)).strftime(fmt), now.strftime(fmt)
    base = {
        "profiles": [{"profile": "Profile 3", "http_visits": 1 if not bad_baseline else 7,
                      "raw_visits": 1, "distinct_hosts": 1,
                      "first_visit": t_last, "last_visit": t_last}],
        "base_default_only": {"http_visits": 3, "raw_visits": 3, "distinct_hosts": 2,
                              "first_visit": t_first, "last_visit": t_last},
        "combined_all_profiles": {"http_visits": 4},
        "leak": {"http_visits": 1},
    }
    if sparse_baseline:
        # 模拟 R430 的 base_default_only：只提供部分字段，其余为 None（不得当 0 比对）
        for d in (base["profiles"][0], base["base_default_only"]):
            for k in ("raw_visits", "first_visit", "last_visit"):
                d[k] = None
    (tmp / "baseline.json").write_text(json.dumps(base), encoding="utf-8")
    return root, tmp / "baseline.json"


def selftest() -> int:
    res = []

    def chk(name, cond, detail=""):
        res.append((name, bool(cond), detail))
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root, base = _fixture(tmp)

        # --- T1 表结构：列名不含「用途/猜测」（验收⑤的负控）---
        out = subprocess.run([sys.executable, str(Path(__file__)),
                              "report", "--chrome-root", str(root),
                              "--snapshot", str(root / "Default"), "--work-root", str(tmp / "w"),
                              "--baseline", str(base), "--json", str(tmp / "r.json")],
                             capture_output=True, text=True)
        payload = json.loads((tmp / "r.json").read_text(encoding="utf-8")) if (tmp / "r.json").exists() else {}
        chk("T1 report 成功且行数=info_cache 条数",
            out.returncode == 0 and payload.get("info_cache_count") == 2 and len(payload.get("rows", [])) == 2,
            f"rc={out.returncode} rows={len(payload.get('rows', []))}")
        chk("T2 表头不含『用途/假设/猜测』列（验收⑤）",
            all(("用途" not in c and "假设" not in c and "猜测" not in c) for c in COLUMNS),
            f"cols={COLUMNS}")

        # --- T3 隐私负控：PII 原文不得出现 ---
        blob = json.dumps(payload, ensure_ascii=False) + out.stdout
        chk("NC1 输出不含邮箱原文/域名", "secret-domain.test" not in blob and "zzz@" not in blob)
        chk("NC2 输出不含 gaia_id 数字串", "999888777666555444333" not in blob)
        chk("NC3 显示名已脱敏（首字符+***）",
            payload["rows"][0]["identity"]["display_name_masked"] in ("Z***", "N***"),
            f"got={[r['identity']['display_name_masked'] for r in payload['rows']]}")
        chk("T4 登录态为布尔且账号类型正确",
            payload["rows"][0]["identity"]["signed_in"] is True
            and any(r["identity"]["account_kind"] == "托管" for r in payload["rows"]))

        # --- 硬敏感 fail-closed 负控（验收②）---
        hit = sensitive_hit(root / "Default" / "Login Data")
        refused = False
        try:
            read_text_safe(root / "Default" / "Login Data")
        except SensitiveRefusal:
            refused = True
        chk("NC4 guard 命中 Login Data 并拒绝", hit is not None and refused, f"hit={hit}")
        refused2 = False
        try:
            read_json_safe(root / "Default" / "Network" / "Cookies")
        except SensitiveRefusal:
            refused2 = True
        chk("NC5 guard 命中 Cookies 并拒绝", refused2)
        p = subprocess.run([sys.executable, str(Path(__file__)), "_probe",
                            str(root / "Default" / "Web Data")], capture_output=True, text=True)
        chk("NC6 CLI 读 Web Data ⇒ rc≠0", p.returncode != 0, f"rc={p.returncode} err={p.stderr.strip()[:80]}")
        p2 = subprocess.run([sys.executable, str(Path(__file__)), "_probe",
                             str(root / "Default" / "Preferences")], capture_output=True, text=True)
        chk("T5 CLI 读 Preferences ⇒ rc=0（白名单不误伤）", p2.returncode == 0, f"rc={p2.returncode}")

        # --- 事实正确性 ---
        d = {r["profile"]: r for r in payload["rows"]}
        chk("T6 http访问数正确（Default=3 / Profile 3=1）",
            d["Default"]["http_visits"] == 3 and d["Profile 3"]["http_visits"] == 1,
            f"{d['Default']['http_visits']}/{d['Profile 3']['http_visits']}")
        chk("T7 扩展数=2；默认搜索引擎=DuckDuckGo",
            d["Default"]["ext_count"] == 2 and d["Default"]["search_engine"]["short_name"] == "DuckDuckGo")
        chk("T8 每行含来源文件路径与时间戳",
            all(r["visits_source"] for r in payload["rows"])
            and any(r["identity"]["last_active_info_cache"] for r in payload["rows"]))
        chk("T9 对账 0 处 mismatch（±0）", payload["mismatch_total"] == 0,
            f"mismatch={payload['mismatch_total']}")
        chk("T9b 对账确有针对字段（compared_total>0，防『零比对也报绿』）",
            payload.get("compared_total", 0) >= 8,
            f"compared={payload.get('compared_total')}")
        chk("T10 未入表的 History 目录被附注（Guest Profile）",
            payload["extra_history_dirs"] == ["Guest Profile"], str(payload["extra_history_dirs"]))
        chk("T11 Local State 只读自证", payload["readonly_proof"]["Local State"]["readonly_proof"] is True)

        # --- NC7 基线篡改必报 MISMATCH 且 rc=3 ---
        root_b, base_b = _fixture(tmp / "b", bad_baseline=True)
        out_b = subprocess.run([sys.executable, str(Path(__file__)), "report",
                                "--chrome-root", str(root_b),
                                "--snapshot", str(root_b / "Default"),
                                "--work-root", str(tmp / "wb"), "--baseline", str(base_b)],
                               capture_output=True, text=True)
        chk("NC7 基线不一致 ⇒ MISMATCH 且 rc=3",
            out_b.returncode == 3 and "MISMATCH" in out_b.stderr, f"rc={out_b.returncode}")

        # --- NC8 缺 Local State ⇒ rc≠0 ---
        empty = tmp / "nolocal"; (empty / "Default").mkdir(parents=True)
        out_c = subprocess.run([sys.executable, str(Path(__file__)), "report",
                                "--chrome-root", str(empty),
                                "--baseline", str(base)], capture_output=True, text=True)
        chk("NC8 缺 Local State ⇒ rc≠0", out_c.returncode != 0, f"rc={out_c.returncode}")

        # --- NC9 副本被篡改 ⇒ verify 报 TAMPERED rc≠0（复用 R430 verify）---
        vd = tmp / "vcopy" / "Default"; vd.mkdir(parents=True)
        h = vd / "History"; h.write_text("x", encoding="utf-8")
        (tmp / "vcopy" / "manifest.json").write_text(json.dumps(
            {"profiles": {"Default": {"files": {"History": {"copy_sha256": "0" * 64}}}}},
            ensure_ascii=False), encoding="utf-8")
        n_ok, bad = CPC.verify_copies(tmp / "vcopy")
        chk("NC9 副本篡改 ⇒ verify 报 TAMPERED", len(bad) == 1 and "TAMPERED" in bad[0], str(bad))

        # --- NC10 基线缺字段（None）不得当 0 比对 ⇒ 0 MISMATCH 且 rc=0 ---
        root_s, base_s = _fixture(tmp / "s", sparse_baseline=True)
        out_s = subprocess.run([sys.executable, str(Path(__file__)), "report",
                                "--chrome-root", str(root_s),
                                "--snapshot", str(root_s / "Default"),
                                "--work-root", str(tmp / "ws"), "--baseline", str(base_s),
                                "--json", str(tmp / "sparse.json")],
                               capture_output=True, text=True)
        pay_s = json.loads((tmp / "sparse.json").read_text(encoding="utf-8"))
        chk("NC10 基线缺字段不算失配（rc=0 且仍有参与对账的字段）",
            out_s.returncode == 0 and pay_s["mismatch_total"] == 0
            and pay_s.get("compared_total", 0) > 0,
            f"rc={out_s.returncode} mismatch={pay_s['mismatch_total']} "
            f"compared={pay_s.get('compared_total')}")

        # --- NC11 指定副本目录被篡改 ⇒ rc=2（fail-closed，不拿脏数据出表）---
        copies_real = tmp / "wb" / dt.date.today().isoformat()
        n_ok0, bad0 = CPC.verify_copies(copies_real)
        chk("NC11a 未篡改时副本自校验通过", not bad0 and n_ok0 > 0, f"verified={n_ok0} bad={bad0}")
        tam = tmp / "tampered"
        shutil.copytree(copies_real, tam)
        for p in tam.rglob("History"):
            p.write_bytes(p.read_bytes() + b"X")
        out_t = subprocess.run([sys.executable, str(Path(__file__)), "report",
                                "--chrome-root", str(root_b),
                                "--copies-dir", str(tam),
                                "--baseline", str(base_b)], capture_output=True, text=True)
        chk("NC11b 指定副本目录被篡改 ⇒ rc=2", out_t.returncode == 2,
            f"rc={out_t.returncode} err={out_t.stderr.strip()[:80]}")

    n_pass = sum(1 for _n, ok, _d in res if ok)
    print(f"\nselftest: {n_pass}/{len(res)} passed")
    return 0 if n_pass == len(res) else 1


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Chrome 多 profile 身份分流证据表（只读）")
    ap.add_argument("mode", nargs="?", default="report", choices=["report", "verify", "_probe"])
    ap.add_argument("target", nargs="?", default=None, help="_probe 的文件路径")
    ap.add_argument("--chrome-root", default=str(DEFAULT_CHROME_ROOT))
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    ap.add_argument("--copies-dir", default=None, help="复用已有冻结副本目录（需含 manifest.json）")
    ap.add_argument("--snapshot", default=None, help="Default 的 History 快照目录")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="R430 基线 JSON")
    ap.add_argument("--day", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.mode == "verify":
        wd = Path(a.copies_dir) if a.copies_dir else Path(a.work_root) / (a.day or dt.date.today().isoformat())
        n_ok, bad = CPC.verify_copies(wd)
        print(f"verify {wd}: {n_ok} 个副本一致" + (f"，异常 {len(bad)}：{bad}" if bad else "（无篡改）"))
        return 0 if not bad else 1
    if a.mode == "_probe":
        if not a.target:
            print("[ERR] _probe 需要文件路径", file=sys.stderr)
            return 2
        try:
            txt = read_text_safe(a.target)
        except SensitiveRefusal as e:
            print(f"[REFUSE] {e}", file=sys.stderr)
            return 2
        print(f"ok bytes={len(txt)}")
        return 0
    return do_report(a)


if __name__ == "__main__":
    raise SystemExit(main())
