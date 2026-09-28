#!/usr/bin/env python3
"""R480 巡检告警的**跨轮/跨面稳定事件 ID**（纯函数内核，零写入）。

解决的问题
----------
同一底层问题（例：birdie 自 2026-08-08 起静默）会被三条消费面各自表述：

  1. `tools/task_freshness.py` 巡检结果 —— 判据层记法 `L1`/`L1b`/`L2`/`L3`/`L0`/`L4`
  2. `tools/detector_replay.py` 窗口回放 —— 层名 `artifact_signature`/`log_signature`/…
  3. `tools/alert_lifecycle.py` 台账 episode —— 读上面两面的结论

三面各自拿名字做键 ⇒ 同一问题在不同面里是三个不同字符串，跨轮对账只能靠人肉比对。
本模块给出**唯一确定**的事件 ID：由 (scope, target, canonical_face) 经规范化 JSON 做
sha256 派生，不含时间/随机/环境量 ⇒
  * 跨轮同 ID：同一问题今天的观测与昨天的观测 ID 相同；
  * 跨面同 ID：`L2` 与 `log_signature` 归一到同一 canonical face ⇒ ID 相同；
  * 重复回放不重复计数：`distinct_events()` 按 (event_id, round) 去重；
  * 复发可区分：`episodes()` 按轮序切连续段，缺席后再现 = 新 episode。

边界（收窄）
------------
  * 只覆盖 task_freshness 巡检面（+ 其两个消费者），不新增告警等级、不改推送；
  * 纯函数、零写入：本模块不打开任何文件写句柄（`proof` 只读 sha256）；
  * 未知 face token **fail-closed**（raise），禁止静默归到 "unknown"。

CLI
---
  python3 tools/alert_event_id.py id <target> <face> [--scope S]
  python3 tools/alert_event_id.py events --from <obs.json|obs.jsonl>
  python3 tools/alert_event_id.py episodes --from <obs.json> [--rounds a,b,c]
  python3 tools/alert_event_id.py audit [--from <台账.jsonl>] [--json]
  python3 tools/alert_event_id.py proof --files a,b
  python3 tools/alert_event_id.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

SCHEMA = "ga.alert_event_id/v1"
DEFAULT_SCOPE = "task_freshness"
# 默认消费源：R411/R479 的巡检台账（records 形态）；不存在时 CLI 报用法而非静默
HERE = os.path.dirname(os.path.abspath(__file__))
GA_ROOT = os.path.dirname(HERE)
DEFAULT_LEDGER = os.path.join(GA_ROOT, "temp", "task_freshness", "ledger.jsonl")
ID_PREFIX = "ev-"
ID_HEX = 12
ID_RE = re.compile(r"^ev-[0-9a-f]{%d}$" % ID_HEX)

# canonical face 词表（唯一权威；两面记法都归到这里）
FACES = ("preflight", "artifact", "content", "log", "run", "lifecycle")

# 别名表：键一律小写、下划线形式；值必须是 FACES 成员
ALIASES = {
    # ---- task_freshness 巡检面（判据层记法）----
    "l0": "preflight", "preflight": "preflight",
    "l1": "artifact", "payload": "artifact", "artifact": "artifact",
    "l1b": "content", "content": "content", "content_change": "content",
    "l2": "log", "log": "log", "log_signature": "log", "signature": "log",
    "l3": "run", "run": "run", "run_presence": "run",
    "l4": "lifecycle", "lifecycle": "lifecycle",
    # ---- detector_replay / R467 回放面 ----
    "artifact_signature": "artifact",
    "payload_stale": "artifact",
    "content_stale": "content",
    "sig": "log",
    "launchd": "run",
    "run_absence": "run",
}

# 静态纯洁性门禁：这些 token 出现在实现里 = ID 不再确定（禁止）
FORBIDDEN_TOKENS = ("time" + ".time", "datetime" + ".now", "rand" + "om.",
                    "uu" + "id", "os.get" + "pid")


# ------------------------------------------------------------------ 归一
def canonical_face(token):
    """把任意面的层名归一到 canonical face；未知 token fail-closed。"""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("canonical_face: 空/非字符串 face token: %r" % (token,))
    t = token.strip().lower().replace("-", "_")
    t = re.sub(r"\s+", "_", t)
    if t in ALIASES:
        return ALIASES[t]
    raise ValueError("canonical_face: 未登记的 face token %r（禁止静默归一）" % (token,))


def canonical_scope(scope):
    s = str(scope or DEFAULT_SCOPE).strip().lower()
    if not re.match(r"^[a-z0-9_]+$", s):
        raise ValueError("canonical_scope: 非法 scope %r" % (scope,))
    return s


def _canonical_payload(scope, target, face):
    return json.dumps({"scope": canonical_scope(scope), "target": str(target).strip(),
                       "face": canonical_face(face)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_id(target, face, scope=DEFAULT_SCOPE):
    """稳定事件 ID：ev-<12 hex>。同一 (scope,target,face) 恒等。"""
    if not str(target).strip():
        raise ValueError("event_id: target 不可为空")
    h = hashlib.sha256(_canonical_payload(scope, target, face).encode("utf-8")).hexdigest()
    return ID_PREFIX + h[:ID_HEX]


def event_view(target, face, scope=DEFAULT_SCOPE):
    """带上下文的视图（供 JSON 落盘/展示）。"""
    return {"event_id": event_id(target, face, scope), "scope": canonical_scope(scope),
            "target": str(target).strip(), "face": canonical_face(face)}


# ------------------------------------------------------------------ 去重 / 复发
RECORDED_ID_RE = re.compile("^" + re.escape(ID_PREFIX) + "[0-9a-f]{%d}$" % ID_HEX)


def _norm_obs(obs, scope):
    """归一一条观测。三形态：
    a) (target, face) ⇒ 现算 ID（权威）；
    b) (target, face, event_id) ⇒ 现算并**校验与记录 ID 一致**（不一致=记录自相矛盾，拒）；
    c) (event_id) 单记录态（台账自描述）⇒ 只校验 ID 形状（不重算，因无 face 可依据）。
    """
    if not isinstance(obs, dict):
        raise ValueError("观察记录必须是 dict: %r" % (obs,))
    sc = obs.get("scope") or scope
    tgt, face, rec = obs.get("target"), obs.get("face"), obs.get("event_id")
    if tgt and face:
        face = canonical_face(face)
        eid = event_id(tgt, face, sc)
        if rec and str(rec) != eid:
            raise ValueError("事件 ID 与 (target,face) 不一致: 记录 %r ≠ 现算 %r" % (rec, eid))
    elif rec:
        if not RECORDED_ID_RE.match(str(rec)):
            raise ValueError("记录的 event_id 形状非法: %r" % (rec,))
        eid, face = str(rec), None
    else:
        raise ValueError("观察记录缺 (target,face) 或 event_id: %r" % (obs,))
    rnd = obs.get("round")
    rnd = "" if rnd is None else str(rnd)
    return {"event_id": eid, "target": str(tgt or ""), "face": face or "",
            "round": rnd, "subject": str(obs.get("subject") or ""),
            "raw_index": obs.get("raw_index")}


def face_from_prefixed_text(text):
    """从巡检 check 文本前缀（L0/L1/L1b/L2/L3/L4）推 face；推不出返回 None（不猜）。

    仅供显式 --infer-face 使用（历史台账行无自描述 events 时的**受控**回填）；
    默认路径不调用 ⇒ 不会静默猜测面。
    """
    m = re.match(r"^\s*(L[0-4](?:b)?)\b", str(text or ""))
    if not m:
        return None
    try:
        return canonical_face(m.group(1))
    except ValueError:
        return None


def expand_records(items, infer_face=False):
    """把「轮次记录」（task_freshness 台账行：{ts, issues:[{id,event_ids,events:[…]}]}）
    展开成逐条观测；非轮次记录原样透传。

    记录态优先用 issues[].events（自描述 target/face ⇒ 可重算校验），退回 event_ids
    （单 ID 形态：只校验形状）；两者皆无且 infer_face=True 时，从 issue['first'] 文本
    前缀推面（受控回填，subject 标 FACE_INFERRED 留痕）；推不出则跳过（不猜）。
    """
    out = []
    for i, rec in enumerate(items or []):
        if isinstance(rec, dict) and "issues" in rec and not rec.get("target"):
            rnd = str(rec.get("ts") or ("round%d" % i))
            for iss in rec.get("issues") or []:
                tgt = iss.get("id")
                evs = iss.get("events") or []
                if evs:
                    for e in evs:
                        out.append({"target": e.get("target") or tgt, "face": e.get("face"),
                                    "event_id": e.get("event_id"), "round": rnd,
                                    "subject": str(iss.get("verdict") or "")})
                elif iss.get("event_ids"):
                    for eid in iss.get("event_ids") or []:
                        out.append({"event_id": eid, "round": rnd,
                                    "subject": str(iss.get("verdict") or "")})
                elif infer_face and tgt:
                    f = face_from_prefixed_text(iss.get("first"))
                    if f:
                        out.append({"target": tgt, "face": f, "round": rnd,
                                    "subject": "FACE_INFERRED"})
            continue
        out.append(rec)
    return out


def distinct_events(observations, scope=DEFAULT_SCOPE):
    """按 (event_id, round) 去重统计。

    「重复回放不重复计数」= 同一 (事件,轮次) 观测出现 N 次只计 1 次观测；
    但 raw_observations 保留原始条数供审计 ⇒ 两数不相等即可看出重复喂入。
    """
    events, seen, raw = {}, set(), 0
    for i, o in enumerate(observations or []):
        o = dict(o)
        o.setdefault("raw_index", i)
        n = _norm_obs(o, scope)
        raw += 1
        key = (n["event_id"], n["round"])
        if key in seen:
            continue
        seen.add(key)
        e = events.setdefault(n["event_id"], {"event_id": n["event_id"], "target": n["target"],
                                              "face": n["face"], "scope": canonical_scope(scope),
                                              "rounds": [], "subjects": []})
        e["rounds"].append(n["round"])
        if n["subject"] and n["subject"] not in e["subjects"]:
            e["subjects"].append(n["subject"])
    for e in events.values():
        e["rounds"].sort()
        e["observations"] = len(e["rounds"])
        e["first_round"], e["last_round"] = (e["rounds"][0] if e["rounds"] else "",
                                            e["rounds"][-1] if e["rounds"] else "")
    return {"schema": SCHEMA, "kind": "distinct_events", "scope": canonical_scope(scope),
            "events": {k: events[k] for k in sorted(events)},
            "distinct": len(events), "observations_raw": raw, "observations_dedup": len(seen),
            "duplicates_dropped": raw - len(seen)}


def episodes(observations, scope=DEFAULT_SCOPE, rounds=None):
    """按轮序切连续段：缺席后再现 = 新 episode（复发次数 = episodes-1）。"""
    ded = distinct_events(observations, scope)
    order = list(rounds) if rounds else sorted({r for e in ded["events"].values() for r in e["rounds"]})
    idx = {r: i for i, r in enumerate(order)}
    by_event, total, rec = {}, 0, 0
    for eid, e in ded["events"].items():
        pos = sorted(idx[r] for r in e["rounds"] if r in idx)
        runs = 0
        prev = None
        for p in pos:
            if prev is None or p != prev + 1:
                runs += 1
            prev = p
        by_event[eid] = {"episodes": runs, "rounds": e["rounds"], "target": e["target"], "face": e["face"]}
        total += runs
        rec += max(runs - 1, 0)
    return {"schema": SCHEMA, "kind": "episodes", "scope": canonical_scope(scope),
            "rounds": order, "by_event": by_event, "episodes": total,
            "recurrences": rec, "events": ded["distinct"]}


# ------------------------------------------------------------------ 静态门禁 / 只读自证
def static_gate(text):
    """静态纯洁性门禁：实现里出现非确定 token ⇒ 返回违规行清单（空 = 通过）。"""
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        code = line.split("#")[0]
        for tok in FORBIDDEN_TOKENS:
            if tok in code:
                bad.append((i, tok, line.strip()[:90]))
    return bad


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def proof(paths):
    before = {p: sha256_file(p) for p in paths}
    _ = distinct_events([{"target": "__proof__", "face": "log", "round": "r0"}])
    after = {p: sha256_file(p) for p in paths}
    changed = sorted(p for p in before if before[p] != after[p])
    return {"files": len(before), "changed": changed, "before": before, "after": after}


# ------------------------------------------------------------------ CLI
def _load_obs(path):
    with open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    if not txt:
        return []
    if txt.startswith("[") or txt.startswith("{"):
        try:
            data = json.loads(txt)
        except ValueError:
            data = None
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("observations"), list):
            return data["observations"]
    out = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


def audit_ledger(recs):
    """台账不变量（R480 口径）：带 `issues` 键的新格式行，其**顶层** `event_ids` 必须等于
    「issues 派生集合」（issues 只收硬目标 ALERT/WARN 的事件）。

    背景：顶层 `event_ids` 是汇总字段，修正前曾把非硬目标（OK/INFO）的事件也并了进去；消费者
    只统计硬目标 ⇒ 两侧不对称 ⇒ 跨轮差集会报幻影「已恢复」。本函数把该不变量做成可复算检查，
    供 `audit` 子命令对任意台账文件跑（只读，不改判）。

    返回 {"checked": 参与检查的行数（带 issues 键的行）, "violations": [...]}；
    违规条目含 top_only（顶层多出的=幻影）与 issues_only（顶层漏记的=漏报）两侧。
    """
    viol = []
    checked = 0
    for i, rec in enumerate(recs or []):
        if not isinstance(rec, dict) or "issues" not in rec:
            continue                      # 旧格式行（无 issues 键）不参与本不变量
        checked += 1
        top = set(rec.get("event_ids") or [])
        derived = set()
        for iss in rec.get("issues") or []:
            derived |= set(iss.get("event_ids") or [])
            for e in iss.get("events") or []:
                if e.get("event_id"):
                    derived.add(e["event_id"])
        if top != derived:
            viol.append({"index": i, "ts": rec.get("ts"),
                         "top_only": sorted(top - derived),
                         "issues_only": sorted(derived - top)})
    return {"checked": checked, "violations": viol}


def main(argv=None):
    ap = argparse.ArgumentParser(description="巡检告警稳定事件 ID（只读）")
    ap.add_argument("cmd", choices=("id", "events", "episodes", "audit", "proof", "selftest"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    ap.add_argument("--from", dest="src")
    ap.add_argument("--rounds", default="")
    ap.add_argument("--files", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--infer-face", action="store_true", dest="infer_face",
                    help="历史台账行无事件字段时，从 check 文本前缀（L0/L1/L1b/L2/L3/L4）"
                         "受控回填 face（默认不回填，不猜）")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "id":
        if len(a.args) < 2:
            print("用法: id <target> <face> [--scope S]", file=sys.stderr)
            return 2
        print(event_id(a.args[0], a.args[1], a.scope))
        return 0
    if a.cmd in ("events", "episodes"):
        src = a.src or a.files
        if not src:
            src = DEFAULT_LEDGER if os.path.exists(DEFAULT_LEDGER) else None
        if not src:
            print("用法: %s --from <台账/观测文件>（默认 %s）" % (a.cmd, DEFAULT_LEDGER),
                  file=sys.stderr)
            return 2
        obs = expand_records(_load_obs(src), infer_face=a.infer_face)
        if a.cmd == "events":
            out = distinct_events(obs, a.scope)
        else:
            rounds = [r for r in (a.rounds.split(",") if a.rounds else []) if r]
            out = episodes(obs, a.scope, rounds or None)
        print(json.dumps(out, ensure_ascii=False, indent=1) if a.json else
              json.dumps({k: v for k, v in out.items() if k != "events"}, ensure_ascii=False))
        return 0
    if a.cmd == "audit":
        src = a.src or a.files
        if not src:
            src = DEFAULT_LEDGER if os.path.exists(DEFAULT_LEDGER) else None
        if not src:
            print("用法: audit --from <台账文件>（默认 %s）" % DEFAULT_LEDGER, file=sys.stderr)
            return 2
        recs = _load_obs(src)
        au = audit_ledger(recs)
        if a.json:
            print(json.dumps({"src": src, "records": len(recs), "audited": au}, ensure_ascii=False))
        else:
            print("audit: records=%d checked=%d violations=%d"
                  % (len(recs), au["checked"], len(au["violations"])))
            for v in au["violations"]:
                print("  [VIOLATION] line %s ts=%s top_only=%s issues_only=%s"
                      % (v["index"] + 1, v["ts"], v["top_only"], v["issues_only"]))
        return 0 if not au["violations"] else 1
    if a.cmd == "proof":
        paths = [p for p in a.files.split(",") if p]
        pr = proof(paths)
        print("proof: files=%d changed=%s" % (pr["files"], pr["changed"]))
        return 0 if not pr["changed"] else 1
    return 2


# ------------------------------------------------------------------ 自测
def collision_report(pairs):
    """碰撞扫描：pairs = [(target, face, event_id), ...] ⇒ 返回「同一键多个 ID」的键清单。

    键 = (target, canonical_face)。正常实现下每个键恰 1 个 ID（返回 []）；
    若别名表被误合并（如把 content 归到 artifact）⇒ 同键出现 2 个 ID 即被抓出。
    """
    by_key = {}
    for tgt, face, eid in pairs:
        by_key.setdefault((str(tgt).strip(), canonical_face(face)), set()).add(eid)
    return sorted(k for k, v in by_key.items() if len(v) > 1)


def _corpus_ids(scope=DEFAULT_SCOPE, targets=None):
    tg = targets if targets is not None else _default_targets()
    return [(t, f, event_id(t, f, scope)) for t in tg for f in FACES]


def _default_targets():
    here = os.path.dirname(os.path.abspath(__file__))
    crit = os.path.join(here, "freshness_criteria.json")
    if os.path.exists(crit):
        try:
            with open(crit, encoding="utf-8") as f:
                data = json.load(f)
            ids = [t["id"] for t in data.get("targets", []) if t.get("id")]
            if ids:
                return ids
        except Exception:                                    # noqa: BLE001
            pass
    return ["synthetic_%02d" % i for i in range(10)]


def selftest():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # [1] 基本契约
    i1 = event_id("birdie_daily_monitor", "log")
    chk("① 格式 ev-<12hex>", bool(ID_RE.match(i1)), i1)
    chk("① 目标/层不同 ⇒ ID 不同", len({event_id("a", "log"), event_id("b", "log"),
                                        event_id("a", "run")}) == 3)
    chk("① 空 target 必报错", _raises(lambda: event_id("", "log")))
    chk("① 非法 scope 必报错", _raises(lambda: event_id("a", "log", "Bad Scope!")))

    # [2] 跨轮稳定（同一问题不同轮次观测）
    i2 = event_id("birdie_daily_monitor", "log")
    chk("② 跨轮同 ID（两次求值逐字相同）", i1 == i2, "%s vs %s" % (i1, i2))
    obs2 = [{"target": "birdie_daily_monitor", "face": "log", "round": "2026-09-26"},
            {"target": "birdie_daily_monitor", "face": "log", "round": "2026-09-27"}]
    d2 = distinct_events(obs2)
    chk("② 两轮观测归同一事件（distinct=1）", d2["distinct"] == 1, d2["distinct"])
    chk("② 事件保留两轮时间戳", d2["events"][i1]["rounds"] == ["2026-09-26", "2026-09-27"],
        d2["events"][i1]["rounds"])

    # [3] 跨面同 ID（别名归一）
    variants = ["L2", "l2", "log", "log_signature", " LOG-SIGNATURE "]
    ids = {event_id("birdie_daily_monitor", v) for v in variants}
    chk("③ 巡检面/回放面记法归一到同一 ID", len(ids) == 1 and i1 in ids, sorted(ids))
    chk("③ L1/L1b 不混（artifact≠content）",
        event_id("x", "L1") != event_id("x", "L1b"))
    chk("③ L0/L4 各自成面", {event_id("x", "L0"), event_id("x", "L4")} ==
        {event_id("x", "preflight"), event_id("x", "lifecycle")})

    # [4] fail-closed：未登记 token 必须报错，不得静默归一
    chk("④ NC 未登记 face 必报错（fail-closed）", _raises(lambda: canonical_face("L9")))
    chk("④ NC 空 face 必报错", _raises(lambda: canonical_face("  ")))
    chk("④ NC 非字符串 face 必报错", _raises(lambda: canonical_face(None)))

    # [5] 去重：重复回放不重复计数
    obs5 = [{"target": "t1", "face": "log", "round": "r1"},
            {"target": "t1", "face": "log", "round": "r1"},
            {"target": "t1", "face": "log", "round": "r2"}]
    d5 = distinct_events(obs5)
    chk("⑤ 同 (事件,轮) 重复喂入只计一次", d5["observations_dedup"] == 2, d5["observations_dedup"])
    chk("⑤ 原始条数保留供审计", d5["observations_raw"] == 3 and d5["duplicates_dropped"] == 1,
        "%s/%s" % (d5["observations_raw"], d5["duplicates_dropped"]))
    chk("⑤ NC 顺序打乱 ⇒ 结果逐字相同",
        json.dumps(distinct_events(list(reversed(obs5))), sort_keys=True) ==
        json.dumps(d5, sort_keys=True))
    d5b = distinct_events(obs5 + obs5)
    chk("⑤ NC 整体回放两次 ⇒ 事件集与去重计数逐字相同（原始条数按设计翻倍）",
        d5b["events"] == d5["events"] and d5b["observations_dedup"] == d5["observations_dedup"]
        and d5b["observations_raw"] == 2 * d5["observations_raw"],
        "dedup=%s raw=%s" % (d5b["observations_dedup"], d5b["observations_raw"]))

    # [6] 复发：缺席后再现 = 新 episode
    obs6 = [{"target": "t1", "face": "log", "round": r} for r in ("r1", "r2", "r4")]
    e6 = episodes(obs6, rounds=["r1", "r2", "r3", "r4"])
    chk("⑥ 缺席一轮后再现 ⇒ episodes=2（复发1）",
        e6["by_event"][event_id("t1", "log")]["episodes"] == 2 and e6["recurrences"] == 1,
        e6["by_event"][event_id("t1", "log")])
    e6b = episodes([{"target": "t1", "face": "log", "round": r} for r in ("r1", "r2", "r3")],
                   rounds=["r1", "r2", "r3", "r4"])
    chk("⑥ 连续在场 ⇒ episodes=1（无复发）", e6b["recurrences"] == 0, e6b["recurrences"])

    # [7] 碰撞扫描（语料自证）
    corpus = _corpus_ids()
    ids_all = [c[2] for c in corpus]
    chk("⑦ 语料自证：targets×faces 条数可数", len(corpus) == len(_default_targets()) * len(FACES),
        len(corpus))
    chk("⑦ 全语料 ID 两两不碰撞", len(set(ids_all)) == len(ids_all),
        "%d/%d" % (len(set(ids_all)), len(ids_all)))
    chk("⑦ 全语料 ID 格式一致", all(ID_RE.match(x) for x in ids_all))
    # NC：把 L1/L1b 故意合成同一面 ⇒ 碰撞扫描必须报碰撞（证明扫描是承重的）
    flat = {(t, f): event_id(t, f) for t, f in ((c[0], c[1]) for c in corpus)}
    tamper = {f: f for f in FACES}
    tamper["content"] = "artifact"
    col = collision_report([(c[0], tamper[c[1]], c[2]) for c in corpus])
    chk("⑦ NC 合并两面后碰撞扫描必报碰撞（扫描承重）",
        len(col) == len(_default_targets()),
        "%d 键相撞" % len(col))
    chk("⑦ 未篡改语料碰撞=0（同批对照组）", collision_report(corpus) == [])

    # [10] 轮次记录展开（task_freshness 台账行）与记录态 ID
    a1 = event_id("birdie_daily_monitor", "log")
    a2 = event_id("birdie_daily_monitor", "run")
    ledger_line = {"ts": "2026-09-28 18:47",
                   "issues": [{"id": "birdie_daily_monitor", "verdict": "ALERT",
                               "event_ids": [a1, a2],
                               "events": [{"target": "birdie_daily_monitor", "face": "log",
                                           "event_id": a1},
                                          {"target": "birdie_daily_monitor", "face": "run",
                                           "event_id": a2}]}]}
    ex = expand_records([ledger_line])
    chk("⑩ 轮次记录按 issue 展开成逐条观测（2 条）", len(ex) == 2, ex)
    d10 = distinct_events(ex)
    chk("⑩ 展开后事件集正确且可重算校验（自描述 events 与现算 ID 一致）",
        set(d10["events"]) == {a1, a2} and d10["distinct"] == 2, sorted(d10["events"]))
    chk("⑩ 轮次键取台账 ts（两条观测同轮）",
        {e["rounds"][0] for e in d10["events"].values()} == {"2026-09-28 18:47"})
    # 同一轮次记录喂两次（重跑）⇒ 去重不重复计数
    d10b = distinct_events(expand_records([ledger_line, ledger_line]))
    chk("⑩ 同一台账行重复喂入 ⇒ 去重计数不变（原始条数翻倍）",
        d10b["observations_dedup"] == d10["observations_dedup"]
        and d10b["observations_raw"] == 2 * d10["observations_raw"],
        "dedup=%s raw=%s" % (d10b["observations_dedup"], d10b["observations_raw"]))
    # 缺 events 自描述时退回 event_ids（记录态 ID：只校验形状）
    only_ids = {"ts": "r1", "issues": [{"id": "x", "event_ids": [a1]}]}
    d10c = distinct_events(expand_records([only_ids]))
    chk("⑩ 无自描述 events 时按 event_ids 记录态展开", set(d10c["events"]) == {a1},
        sorted(d10c["events"]))
    chk("⑩ NC 记录态 ID 形状非法必报错",
        _raises(lambda: distinct_events([{"event_id": "ev-ZZZ", "round": "r1"}])))
    chk("⑩ NC 无 target/face 且无 event_id 必报错",
        _raises(lambda: distinct_events([{"round": "r1"}])))
    chk("⑩ NC 记录 ID 与 (target,face) 不一致必报错（记录自相矛盾）",
        _raises(lambda: distinct_events([{"target": "birdie_daily_monitor", "face": "run",
                                          "event_id": a1, "round": "r1"}])))
    chk("⑩ 非轮次记录原样透传（单观测形态不受影响）",
        expand_records([{"target": "x", "face": "log", "round": "r1"}]) ==
        [{"target": "x", "face": "log", "round": "r1"}])
    # 历史台账（无事件字段）⇒ 默认不回填；显式 --infer-face 才从 check 文本前缀受控回填
    old_line = {"ts": "r1", "issues": [{"id": "ga-api", "verdict": "ALERT",
                                        "first": "L3 运行证据新鲜（11.39d, logs/ga_api_server.log）"}]}
    chk("⑩ NC 历史台账默认不猜面（展开为 0 条）", expand_records([old_line]) == [])
    inf = expand_records([old_line], infer_face=True)
    chk("⑩ 显式 infer_face 时按前缀受控回填（L3⇒run，运行证据层）且留痕 subject",
        len(inf) == 1 and inf[0]["face"] == "run" and inf[0]["subject"] == "FACE_INFERRED", inf)
    chk("⑩ 回填后与现算 ID 一致（同 target/面 ⇒ 同 ID）",
        set(distinct_events(inf)["events"]) == {event_id("ga-api", "L3")})
    bad_line = {"ts": "r1", "issues": [{"id": "x", "verdict": "ALERT", "first": "无前缀文本"}]}
    chk("⑩ NC 前缀不可解析 ⇒ 即使 infer_face 也不猜（0 条）",
        expand_records([bad_line], infer_face=True) == [])

    # [10b] 台账不变量（R480 口径）：顶层 event_ids == issues 派生集合
    _clean = {"ts": "r1", "event_ids": [a1],
              "issues": [{"id": "birdie_daily_monitor", "verdict": "ALERT",
                          "event_ids": [a1], "events": []}]}
    au1 = audit_ledger([_clean])
    chk("⑩b 台账不变量：顶层=issues 派生 ⇒ 0 违规（且计入 checked）",
        au1["checked"] == 1 and au1["violations"] == [], au1)
    _ghost = dict(_clean, event_ids=[a1, event_id("x_ai_intel_daily", "L2")])
    au2 = audit_ledger([_ghost])
    chk("⑩b NC 幻影事件（非硬目标）混入顶层必报违规 top_only",
        len(au2["violations"]) == 1 and
        au2["violations"][0]["top_only"] == [event_id("x_ai_intel_daily", "L2")], au2)
    au3 = audit_ledger([dict(_clean, event_ids=[])])
    chk("⑩b NC 顶层漏记（issues 有而顶层空）必报违规 issues_only",
        len(au3["violations"]) == 1 and au3["violations"][0]["issues_only"] == [a1], au3)
    au4 = audit_ledger([{"ts": "r1", "hard_issue_ids": ["t1"]}, _clean])
    chk("⑩b 旧格式行（无 issues 键）不参与本不变量（checked 只数新格式行）",
        au4["checked"] == 1 and au4["violations"] == [], au4)

    # [8] 静态纯洁性门禁
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    chk("⑧ 实现内无非确定 token（静态门禁）", not static_gate(src), static_gate(src))
    chk("⑧ NC 注入随机源必被门禁抓出",
        bool(static_gate("import rand" + "om\nx = rand" + "om.rand" + "om()")))
    chk("⑧ NC 注释里的 token 不算违规（只查代码）",
        not static_gate("# time" + ".time 只是注释"))

    # [9] 只读自证
    pr = proof([os.path.abspath(__file__)])
    chk("⑨ 跑前后 sha256 无变化（零写入）", pr["changed"] == [], pr["changed"])

    ok = True
    for name, cond, detail in checks:
        print("   [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                                "" if cond else ("  → " + str(detail)[:160])))
        ok = ok and cond
    print("alert_event_id selftest: %d passed, %d failed" %
          (sum(1 for _, c, _ in checks if c), sum(1 for _, c, _ in checks if not c)))
    return 0 if ok else 1


def _raises(fn):
    try:
        fn()
    except Exception:                                        # noqa: BLE001
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
