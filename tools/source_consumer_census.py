#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""source_consumer_census.py —— 数据源 × 消费者 对账（只读，R436 / TODO#10）

问题
----
`tools/usage_census.py`（R421）只判 **`tools/*.py` 的引用面**（谁被谁引用）；**数据源侧
无人做同一件事**——于是「采了一批数据却没有任何消费者」没人能量。R423 的 Chrome 快照、
R434 的两条 kc 万级流都是「采完才发现此前零消费者」的例子。本器把这件事做成**可复跑的对账**：
对一批**数据源**逐个回答「有料吗？谁在消费？首个消费者是谁？」，并输出「有料但零消费者」清单。

谁算消费者（写死的定义，验收②）
--------------------------------
按**引用该源的代码文件的角色**三分（角色登记在 `COLLECTORS` / `GUARDS_POLICY`，逐条给理由）：

* **CONSUMER（消费者）** = 读取该源并产出**派生结论/指标**的模块
  （例：`tools/kc_app_usage.py`、`tools/sleep_source_crosscheck.py`、`tools/chrome_history_consume.py`）。
* **COLLECTOR（采集器）** = 只搬运/快照/拷贝，不产出派生结论（例：`tools/rolling_source_snapshot.py`）。
  采集器**不计**消费者（只搬运不算「有人用这份数据」）。
* **GUARD / POLICY（守卫·策略）** = 只做健康判定或把源名当白名单/地图
  （例：`tools/kc_snapshot_guard.py`、`tools/personal_source_map.py`、`tools/usage_census.py`）。
* 仅 durable 记忆 / 仅报告引用 ⇒ 记作参考面，**不计**消费者。

判定结论只有五种：`CONSUMED` / `COLLECTOR_ONLY` / `GUARD_ONLY` / `REF_ONLY` / `NONE`。
**只有 `CONSUMED` 算「有消费者」**；后四种都是「实质零消费者」，其中 `COLLECTOR_ONLY` 与
`GUARD_ONLY` 属「弱消费」（有人在读，但没产出结论），单列不混入。

范围（验收②的前提）
--------------------
* 在册源 = kc 三流（`/app/usage`、`/notification/usage`、`/display/isBacklit`）
  + **全部带 `History` 的 Chrome profile**（Default + 9 个非 Default，实测见真跑输出）。
* **附节**（不是 12 源普查）：同一 kc 快照里**其余流**（`/bluetooth/isConnected` 等）同法判定，
  标 `scope="kc-other"`；它们是 kc 源内部细分，不引入 personal_source_map 的 12 个外部源。

复用（零重写）
--------------
* 引用判定内核：`tools/usage_census.py` 的 `git_grep` / `classify_face` / `unquote_git_path` / `sh`。
* profile 发现·只读拷贝·单库统计：`tools/chrome_profiles_consume.py` 的
  `discover_profiles` / `copy_profile_readonly` / `profile_stats`（拷 `History`+`-wal`/`-shm`，RULE17）。
* 本器**不**重写任何判定逻辑，只做「源清单 + 角色登记 + 汇总」。

只读（验收④）
--------------
* kc 快照副本：`file:...?mode=ro`；
* Chrome：源库一律**只做 sha256**，任何 SQL 都在 `chrome_profiles_consume` 拷贝出的工作区副本上跑；
* `--readonly-proof` 打印源侧指纹前后一致（kc 快照 sha256 + 每个 Chrome History sha256）。

用法
----
    python3 tools/source_consumer_census.py                      # 真跑（只读）
    python3 tools/source_consumer_census.py --json temp/source_census/<date>.json
    python3 tools/source_consumer_census.py --no-profiles        # 只判 kc 面（跳过 Chrome 拷贝）
    python3 tools/source_consumer_census.py --readonly-proof     # 附源侧指纹前后一致自证
    python3 tools/source_consumer_census.py --selftest           # 纯函数自测（含 6 类负控）
"""

from __future__ import annotations

import argparse
import tempfile
import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

GA_ROOT = Path(__file__).resolve().parent.parent
SELF_REL = "tools/source_consumer_census.py"

KC_SNAPSHOT_ROOT = GA_ROOT / "temp" / "kc_snapshots"
PROFILE_WORK_ROOT = GA_ROOT / "temp" / "source_census" / "profiles"

# --------------------------------------------------------------------------- #
# 角色登记（写死；每条的「为什么不算消费者」都写在这里）
# --------------------------------------------------------------------------- #
COLLECTORS = {
    "tools/rolling_source_snapshot.py": "快照采集器：只把源拷进 temp/<src>_snapshots",
    "tools/kc_snapshot.py": "kc 快照器：snapshot 面只搬运（其 report 面见 NOTES 附注）",
}
GUARDS_POLICY = {
    "tools/kc_snapshot_guard.py": "守卫：只算行数判窗口/告警，不产出派生结论",
    "tools/personal_source_map.py": "源地图：把源路径当数据登记",
    "tools/usage_census.py": "引用判定内核：把源名当「可再生产物」白名单",
    "tools/t0_snapshot.py": "T0 备份清单器：只判入没入备份",
}
# 已知已消费源 → 必须存在的消费者（验收③负控的靶子；判不到即报违规）
KNOWN_CONSUMED = {
    "kc:/display/isBacklit": "tools/sleep_source_crosscheck.py",
    "chrome:Default": "tools/chrome_history_consume.py",
    "chrome:Profile 3": "tools/chrome_profiles_consume.py",
}

KC_SCOPE = ["/app/usage", "/notification/usage", "/display/isBacklit"]

VERDICTS = ("CONSUMED", "COLLECTOR_ONLY", "GUARD_ONLY", "REF_ONLY", "NONE")


# --------------------------------------------------------------------------- #
# 复用内核（零重写）
# --------------------------------------------------------------------------- #
def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, GA_ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


_UC = None
_CPC = None


def usage_census():
    global _UC
    if _UC is None:
        _UC = _load("usage_census", "tools/usage_census.py")
    return _UC


def chrome_tool():
    global _CPC
    if _CPC is None:
        _CPC = _load("chrome_profiles_consume", "tools/chrome_profiles_consume.py")
    return _CPC


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def corpus_size() -> int:
    """语料自证（验收②）：tracked 文件数 = git ls-files 行数。"""
    rc, out, _ = usage_census().sh(usage_census().GIT + ["ls-files"])
    if rc != 0:
        return -1
    return len([x for x in out.splitlines() if x.strip()])


# --------------------------------------------------------------------------- #
# 纯函数：分类与判定
# --------------------------------------------------------------------------- #
def split_code_roles(code_hits, collectors=None, guards=None):
    """把「代码面」命中拆成 消费者 / 采集器 / 守卫·策略 三桶（纯函数）。"""
    collectors = COLLECTORS if collectors is None else collectors
    guards = GUARDS_POLICY if guards is None else guards
    consumers, coll, guard = [], [], []
    for h in sorted(set(code_hits)):
        if h in collectors:
            coll.append(h)
        elif h in guards:
            guard.append(h)
        else:
            consumers.append(h)
    return {"consumers": consumers, "collectors": coll, "guards": guard}


def verify_dynamic(entry, root=None):
    """校验一条动态消费者登记：文件存在 ∧ probe 出现在该文件里。返回 (ok, 说明)。"""
    root = Path(root) if root is not None else GA_ROOT
    p = root / entry["path"]
    if not p.is_file():
        return False, f"文件不存在: {entry['path']}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:  # pragma: no cover
        return False, f"不可读: {e}"
    if entry["probe"] not in text:
        return False, f"探针 {entry['probe']!r} 未出现在 {entry['path']}"
    return True, "ok"


def judge_hits(hits, self_path=SELF_REL, dynamic=None, root=None):
    """对一批命中路径做「谁在消费」判定（纯函数，供 selftest）。

    复用 usage_census.classify_face 分面；再做两处**显式**精化（内核零改动）：
      1. `memory/` 下的可执行脚本（`*.py/.sh/.js/.command`）从 durable 面挪回代码面
         —— `memory/kc_profile/profile.py` 就是真消费者，但 classify_face 按目录先归 durable；
      2. 代码面再按角色登记拆 消费者 / 采集器 / 守卫·策略。
    """
    UC = usage_census()
    face = UC.classify_face(list(hits), self_path)
    code = list(face["code"]) + list(face["cfg"])
    moved = [h for h in face["durable"] if h.endswith((".py", ".sh", ".js", ".command"))]
    durable = [h for h in face["durable"] if h not in moved]
    code += moved
    roles = split_code_roles(code)
    # 动态登记：源码里不出现该源的字面量（按目录/字段枚举）⇒ token 检索测不到。
    # 探针必须能在该文件里逐字命中，否则 fail-closed 不计消费者（宁可报「零消费者」）。
    dyn, dyn_bad = [], []
    for e in (dynamic or []):
        oks, why = verify_dynamic(e, root)
        (dyn if oks else dyn_bad).append(e if oks else dict(e, _why=why))
    dyn_files = sorted({e["path"] for e in dyn})
    if roles["consumers"]:
        verdict = "CONSUMED"
    elif dyn_files:
        verdict = "DYNAMIC_CONSUMED"
    elif roles["collectors"]:
        verdict = "COLLECTOR_ONLY"
    elif roles["guards"]:
        verdict = "GUARD_ONLY"
    elif durable or face["reports"]:
        verdict = "REF_ONLY"
    else:
        verdict = "NONE"
    cons = sorted(roles["consumers"], key=lambda p: (not p.startswith("tools/"), p))
    return {
        "verdict": verdict,
        "consumers": cons,
        "dynamic_consumers": dyn_files,
        "dynamic_skipped": [{"path": e["path"], "why": e["_why"]} for e in dyn_bad],
        "first_consumer": cons[0] if cons else (dyn_files[0] if dyn_files else None),
        "collectors": roles["collectors"],
        "guards": roles["guards"],
        "code_hits": sorted(set(code)),
        "mem_hits": len(durable),
        "report_hits": len(face["reports"]),
        "other_hits": len(face["other"]),
    }


def guard_known_consumed(rows, known=None):
    """负控靶子（验收③）：已知已消费源若判不出预期消费者 ⇒ 违规列表（非空即报）。"""
    known = KNOWN_CONSUMED if known is None else known
    by_id = {r["id"]: r for r in rows}
    bad = []
    for sid, expect in known.items():
        r = by_id.get(sid)
        if r is None:
            bad.append(f"{sid}: 源不在册（源缺失）")
            continue
        seen = list(r.get("consumers") or []) + list(r.get("dynamic_consumers") or [])
        if expect not in seen:
            bad.append(f"{sid}: 判为 {r.get('verdict')}，未见预期消费者 {expect}")
    return bad


def zero_consumer(rows):
    """「有料但零消费者」清单：有料 ∧ verdict != CONSUMED（弱消费单列）。"""
    hard = [r for r in rows if r["has_data"] and r["verdict"] in ("NONE", "REF_ONLY")]
    soft = [r for r in rows if r["has_data"] and r["verdict"] in ("COLLECTOR_ONLY", "GUARD_ONLY")]
    return hard, soft


# --------------------------------------------------------------------------- #
# 源侧探测（有料证据）
# --------------------------------------------------------------------------- #
def latest_kc_snapshot(root=KC_SNAPSHOT_ROOT):
    hits = sorted(root.glob("*/knowledgeC.db"))
    if not hits:
        return None
    return Path(hits[-1])


def kc_stream_counts(db: Path):
    """全部 kc 流的行数（快照副本，mode=ro）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return dict(con.execute(
            "select ZSTREAMNAME, count(*) from ZOBJECT group by 1 order by 2 desc").fetchall())
    finally:
        con.close()


def kc_sources(db: Path, all_streams: dict):
    rows = []
    for stream in KC_SCOPE:
        n = int(all_streams.get(stream, 0))
        rows.append({
            "id": f"kc:{stream}", "scope": "kc", "kind": "kc-stream",
            "token": stream, "evidence": f"{Path(db).name}:{n} 行",
            "detail": str(db), "rows": n, "has_data": n > 0, "tokens": [stream],
        })
    for stream, n in sorted(all_streams.items()):
        if stream in KC_SCOPE:
            continue
        rows.append({
            "id": f"kc:{stream}", "scope": "kc-other", "kind": "kc-stream",
            "token": stream, "evidence": f"{Path(db).name}:{int(n)} 行",
            "detail": str(db), "rows": int(n), "has_data": int(n) > 0, "tokens": [stream],
        })
    return rows


def chrome_tokens(profile: str):
    """**每个 profile 专属**的检索 token（保证各 profile 命中集合不同，不被父目录泛词灌水）。

    Default 走 R415 的冷拷贝快照（`temp/chrome_snapshots/<日期>/History` 就是 Default 的库，
    `tools/chrome_history_consume.py` 消费它）；其余 profile 无字面量可检索 ⇒ 靠动态登记。
    """
    if profile == "Default":
        return ["chrome_snapshots", "Chrome/Default"]
    return [f"Chrome/{profile}", f"Google/Chrome/{profile}"]


# 动态消费者登记：源码里**不出现该源的字面量**（按目录/字段枚举），token 检索天然测不到。
# 每条必须给可机械验证的探针（probe=必须出现在该文件里的字符串）；探针失效即 fail-closed
# 不计消费者（宁可报「零消费者」，不可凭空认亲）。
DYNAMIC_CONSUMERS = {
    "chrome:*": [
        {"path": "tools/chrome_profiles_consume.py", "probe": "discover_profiles",
         "why": "R430：按 discover_profiles() 枚举全部 profile，profile 名不进源码字面量"},
    ],
}



def chrome_sources(chrome_root: Path, work_root: Path = PROFILE_WORK_ROOT):
    """逐 profile 的只读证据：discover_profiles → 拷贝 → profile_stats（复用 R430 内核）。"""
    CPC = chrome_tool()
    items = CPC.discover_profiles(Path(chrome_root))
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for it in items:
        rec = CPC.copy_profile_readonly(it, work_root)
        copy_history = work_root / it["profile"] / "History"
        st = {}
        try:
            st = CPC.profile_stats(copy_history)
        except Exception as e:  # 副本不可读也不炸整轮
            st = {"_error": str(e)}
        visits = int(st.get("raw_visits") or st.get("http_visits") or 0)
        rows.append({
            "id": f"chrome:{it['profile']}", "scope": "chrome", "kind": "chrome-profile",
            "token": it["profile"], "evidence": f"History {it['bytes']}B / visits={visits}",
            "detail": str(it["history"]), "rows": visits, "has_data": visits > 0,
            "tokens": chrome_tokens(it["profile"]), "readonly": rec,
            "dynamic": DYNAMIC_CONSUMERS.get("chrome:*", []),
        })
    return rows


def source_fingerprints(rows):
    """源侧指纹（只读自证用）：kc 快照 + 每个 Chrome 源库 + 其 -wal/-shm。"""
    out = {}
    for r in rows:
        p = Path(r["detail"])
        cands = [p]
        if r["kind"] == "chrome-profile" and p.name == "History":
            cands += [Path(str(p) + "-wal"), Path(str(p) + "-shm")]
        for c in cands:
            if c.exists():
                try:
                    out[str(c)] = sha256_file(c)
                except Exception as e:
                    out[str(c)] = f"ERR {e}"
    return out


# --------------------------------------------------------------------------- #
# 真跑
# --------------------------------------------------------------------------- #
def build_rows(chrome_root=None, kc_root=KC_SNAPSHOT_ROOT, work_root=PROFILE_WORK_ROOT,
               with_profiles=True):
    UC = usage_census()
    db = latest_kc_snapshot(kc_root)
    if db is None:
        raise SystemExit(f"[ERR] 找不到 kc 快照：{kc_root}/*/knowledgeC.db")
    streams = kc_stream_counts(db)
    rows = kc_sources(db, streams)
    if with_profiles:
        rows += chrome_sources(chrome_root or chrome_tool().DEFAULT_CHROME_ROOT, work_root)
    for r in rows:
        hits = []
        for t in r["tokens"]:
            hits += UC.git_grep(t)
        r["hits_total"] = len(set(hits))
        r.update(judge_hits(hits, dynamic=r.get("dynamic")))
    return rows, db


def render(rows, db, corpus_files):
    hard, soft = zero_consumer(rows)
    L = []
    L.append("数据源 × 消费者 对账（只读）")
    L.append(f"  kc 快照：{db}")
    L.append(f"  语料：tracked 文件 {corpus_files} 个（git ls-files）；源在册 {len(rows)} 个")
    L.append("")
    L.append(f"{'源':34s} {'有料':4s} {'判定':16s} {'首个消费者':38s} {'采集器':>4s} {'守卫':>4s} {'记忆':>4s} {'报告':>4s} {'动态':>4s}")
    for r in sorted(rows, key=lambda x: (x["scope"], x["id"])):
        L.append(f"{r['id']:34s} {'Y' if r['has_data'] else 'n':4s} {r['verdict']:16s} "
                 f"{(r['first_consumer'] or '无'):38s} {len(r['collectors']):>4d} {len(r['guards']):>4d} "
                 f"{r['mem_hits']:>4d} {r['report_hits']:>4d} {len(r.get('dynamic_consumers') or []):>4d}")
    L.append("")
    L.append(f"【有料但零消费者】{len(hard)} 个：")
    for r in hard:
        L.append(f"  - {r['id']}  ({r['evidence']})  判定={r['verdict']}")
    if not hard:
        L.append("  （无）")
    L.append(f"【弱消费：只有采集器/守卫在读】{len(soft)} 个：")
    for r in soft:
        who = (r["collectors"] + r["guards"])
        L.append(f"  - {r['id']}  ({r['evidence']})  判定={r['verdict']}  在读={', '.join(who)}")
    dyn_rows = [r for r in rows if r.get("dynamic_consumers")]
    L.append(f"【动态消费者（token 检索测不到，已过探针）】{len(dyn_rows)} 个：")
    for r in dyn_rows:
        L.append(f"  - {r['id']}  → {', '.join(r['dynamic_consumers'])}")
    skipped = [(r["id"], s) for r in rows for s in (r.get("dynamic_skipped") or [])]
    if skipped:
        L.append(f"【动态登记探针失效（fail-closed 已不计入）】{len(skipped)} 条：")
        for sid, s in skipped:
            L.append(f"  - {sid}  {s['path']}  {s['why']}")
    if not soft:
        L.append("  （无）")
    return "\n".join(L), hard, soft


def main(argv=None):
    ap = argparse.ArgumentParser(description="数据源 × 消费者 对账（只读）")
    ap.add_argument("--chrome-root", default=None)
    ap.add_argument("--kc-root", default=str(KC_SNAPSHOT_ROOT))
    ap.add_argument("--work-root", default=str(PROFILE_WORK_ROOT))
    ap.add_argument("--no-profiles", action="store_true", help="跳过 Chrome 面（不拷贝任何 profile）")
    ap.add_argument("--json", default=None, help="落 JSON 报告（唯一写入点之一）")
    ap.add_argument("--readonly-proof", action="store_true", help="打印源侧指纹前后一致")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    before = None
    rows, db = build_rows(args.chrome_root, Path(args.kc_root), Path(args.work_root),
                          with_profiles=not args.no_profiles)
    if args.readonly_proof:
        before = source_fingerprints(rows)
    text, hard, soft = render(rows, db, corpus_size())
    print(text)

    # --no-profiles 时 Chrome 源整面缺席（不是「源消失」）⇒ 该模式只对 kc 面做负控，
    # 否则会恒报 2 条「源缺失」把 rc 抬成 2（实测踩到）。
    known_for_guard = KNOWN_CONSUMED
    if args.no_profiles:
        known_for_guard = {k: v for k, v in KNOWN_CONSUMED.items() if not k.startswith("chrome:")}
        print(f"\n[负控范围] --no-profiles ⇒ 只在册 {len(known_for_guard)} 条 kc 侧登记做负控")
    bad = guard_known_consumed(rows, known=known_for_guard)
    if bad:
        print("\n[负控] 已知已消费源判为未消费（必须修）：")
        for b in bad:
            print("  -", b)
    if args.readonly_proof:
        after = source_fingerprints(rows)
        same = before == after
        print(f"\n[只读自证] 源侧指纹 {len(before)} 项，前后一致 = {same}")
        if not same:
            for k in before:
                if before.get(k) != after.get(k):
                    print(f"  CHANGED {k}: {before.get(k)} -> {after.get(k)}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "kc_snapshot": str(db),
            "corpus_tracked_files": corpus_size(),
            "sources": [{k: v for k, v in r.items() if k != "readonly"} for r in rows],
            "zero_consumer": [r["id"] for r in hard],
            "weak_consumer": [r["id"] for r in soft],
            "known_consumed_violations": bad,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[json] {out}")

    return 2 if bad else 0


# --------------------------------------------------------------------------- #
# 自测（纯函数 + 夹具，含负控）
# --------------------------------------------------------------------------- #
def selftest():
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name}")

    print("selftest：角色拆分 / 判定 / 负控 / 语料自证")

    # --- 纯函数：角色拆分
    r = split_code_roles(["tools/kc_app_usage.py", "tools/rolling_source_snapshot.py",
                          "tools/kc_snapshot_guard.py"])
    chk("T1 三桶拆分", r["consumers"] == ["tools/kc_app_usage.py"]
        and r["collectors"] == ["tools/rolling_source_snapshot.py"]
        and r["guards"] == ["tools/kc_snapshot_guard.py"])

    # --- 判定：真消费者在场 ⇒ CONSUMED 且首个消费者=tools/
    j = judge_hits(["tools/sleep_source_crosscheck.py", "memory/kc_profile/profile.py",
                    "memory/autonomous_reports/R426_x.md", "tools/rolling_source_snapshot.py"])
    chk("T2 CONSUMED + tools/ 优先", j["verdict"] == "CONSUMED"
        and j["first_consumer"] == "tools/sleep_source_crosscheck.py"
        and j["consumers"] == ["tools/sleep_source_crosscheck.py", "memory/kc_profile/profile.py"])

    # --- 判定：只有采集器 ⇒ COLLECTOR_ONLY（不得算消费者）
    j = judge_hits(["tools/rolling_source_snapshot.py", "tools/kc_snapshot.py"])
    chk("T3 只采集器=COLLECTOR_ONLY", j["verdict"] == "COLLECTOR_ONLY" and not j["consumers"])

    # --- 判定：只有守卫/策略 ⇒ GUARD_ONLY
    j = judge_hits(["tools/kc_snapshot_guard.py", "tools/personal_source_map.py"])
    chk("T4 只守卫=GUARD_ONLY", j["verdict"] == "GUARD_ONLY" and not j["consumers"])

    # --- 判定：只有记忆/报告 ⇒ REF_ONLY
    j = judge_hits(["memory/global_mem_insight.txt", "memory/autonomous_reports/R434_x.md"])
    chk("T5 只记忆/报告=REF_ONLY", j["verdict"] == "REF_ONLY" and not j["consumers"])

    # --- 判定：零命中 ⇒ NONE
    j = judge_hits([])
    chk("T6 零命中=NONE", j["verdict"] == "NONE" and j["first_consumer"] is None)

    # --- 负控 NC1：memory 下 .py 若被误判为「非代码」会漏掉 profile.py 消费者
    j = judge_hits(["memory/kc_profile/profile.py"])
    chk("NC1 durable 下 .py 归代码面", j["verdict"] == "CONSUMED"
        and j["consumers"] == ["memory/kc_profile/profile.py"])

    # 夹具：三条已知已消费源**全部在册**（少一条会变「源缺失」违规，污染断言）
    def _ok_rows(overrides):
        base = [{"id": "chrome:Default", "verdict": "CONSUMED",
                 "consumers": ["tools/chrome_history_consume.py"]},
                {"id": "chrome:Profile 3", "verdict": "DYNAMIC_CONSUMED", "consumers": [],
                 "dynamic_consumers": ["tools/chrome_profiles_consume.py"]},
                {"id": "kc:/display/isBacklit", "verdict": "CONSUMED",
                 "consumers": ["tools/sleep_source_crosscheck.py"]}]
        for o in overrides:
            for r in base:
                if r["id"] == o["id"]:
                    r.update(o)
        return base

    # --- 负控 NC2：把已知已消费源（Chrome Default）判成未消费 ⇒ guard 必报
    bad = guard_known_consumed(_ok_rows([{"id": "chrome:Default", "verdict": "REF_ONLY",
                                          "consumers": []}]))
    chk("NC2 已消费源判未消费必报", len(bad) == 1 and "chrome:Default" in bad[0])

    # --- 负控 NC3：已知已消费源（kc isBacklit）判未消费 ⇒ 必报
    bad = guard_known_consumed(_ok_rows([{"id": "kc:/display/isBacklit", "verdict": "COLLECTOR_ONLY",
                                          "consumers": []}]))
    chk("NC3 isBacklit 判未消费必报", len(bad) == 1 and "isBacklit" in bad[0])

    # --- 负控 NC4：源缺失也必报（条数=登记表长度，防登记增删后断言过期）
    bad = guard_known_consumed([])
    chk("NC4 源缺失全数必报", len(bad) == len(KNOWN_CONSUMED))

    # --- T13/NC10：动态消费者也算「在场」；把动态那路抹掉必报
    bad = guard_known_consumed(_ok_rows([]))
    chk("T13 动态消费者视同在场", bad == [])
    bad = guard_known_consumed(_ok_rows([{"id": "chrome:Profile 3", "verdict": "REF_ONLY",
                                          "consumers": [], "dynamic_consumers": []}]))
    chk("NC10 动态那路缺失必报", len(bad) == 1 and "Profile 3" in bad[0])

    # --- 负控 NC5：空源（有料=False）不得进「零消费者」清单（防假警报）
    rows = [{"id": "chrome:Guest Profile", "has_data": False, "verdict": "NONE"}]
    hard, soft = zero_consumer(rows)
    chk("NC5 空源不进零消费者清单", hard == [] and soft == [])

    # --- 负控 NC6：有料 + NONE ⇒ 必进清单；有料 + CONSUMED ⇒ 不进
    rows = [{"id": "kc:/app/mediaUsage", "has_data": True, "verdict": "NONE"},
            {"id": "kc:/app/usage", "has_data": True, "verdict": "CONSUMED"},
            {"id": "kc:/bluetooth/isConnected", "has_data": True, "verdict": "GUARD_ONLY"}]
    hard, soft = zero_consumer(rows)
    chk("NC6 零消费者清单只收非 CONSUMED", [r["id"] for r in hard] == ["kc:/app/mediaUsage"]
        and [r["id"] for r in soft] == ["kc:/bluetooth/isConnected"])

    # --- 语料自证（验收②）
    n = corpus_size()
    chk("T7 语料自证>0", n > 0)
    chk("T8 角色登记自洽（采集器/守卫互斥且非空）",
        not (set(COLLECTORS) & set(GUARDS_POLICY)) and len(COLLECTORS) == 2 and len(GUARDS_POLICY) == 4)

    # --- 复用内核可用（零重写的前提）
    UC = usage_census()
    chk("T9 内核函数可调用", callable(UC.git_grep) and callable(UC.classify_face))
    chk("T10 kc 三流在册", all(f"kc:{s}" for s in KC_SCOPE) and len(KC_SCOPE) == 3)

    # --- 动态消费者（token 检索测不到：源码里不出现该源字面量，按目录/字段枚举）
    with tempfile.TemporaryDirectory() as td:
        Path(td, "dyn_consumer.py").write_text("x = discover_profiles()\n", encoding="utf-8")
        okd, why = verify_dynamic({"path": "dyn_consumer.py", "probe": "discover_profiles"}, root=td)
        chk("T11 动态登记探针校验通过", okd and why == "ok")
        j = judge_hits([], dynamic=[{"path": "dyn_consumer.py", "probe": "discover_profiles"}], root=td)
        chk("T12 动态登记 ⇒ DYNAMIC_CONSUMED 且首消费者=该文件",
            j["verdict"] == "DYNAMIC_CONSUMED" and j["first_consumer"] == "dyn_consumer.py"
            and j["dynamic_consumers"] == ["dyn_consumer.py"])

        # --- 负控 NC7：探针失效 ⇒ fail-closed 不计消费者（记 skipped），不得凭空认亲
        j = judge_hits([], dynamic=[{"path": "dyn_consumer.py", "probe": "NOT_IN_FILE_XYZ"}], root=td)
        chk("NC7 探针失效 fail-closed", j["verdict"] == "NONE" and j["dynamic_consumers"] == []
            and len(j["dynamic_skipped"]) == 1 and j["first_consumer"] is None)

        # --- 负控 NC8：登记文件不存在 ⇒ 同样 fail-closed
        j = judge_hits([], dynamic=[{"path": "no_such_file.py", "probe": "x"}], root=td)
        chk("NC8 登记文件不存在 fail-closed", j["verdict"] == "NONE" and len(j["dynamic_skipped"]) == 1)

    # --- 负控 NC9：动态不得掩盖 token 侧消费者；两路并存 ⇒ CONSUMED 且动态仍登记
    j = judge_hits(["tools/chrome_history_consume.py"],
                   dynamic=[{"path": "tools/chrome_profiles_consume.py", "probe": "def discover_profiles"}])
    chk("NC9 token+动态两路并存", j["verdict"] == "CONSUMED"
        and j["consumers"] == ["tools/chrome_history_consume.py"]
        and j["dynamic_consumers"] == ["tools/chrome_profiles_consume.py"])

    print(f"\nselftest: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

