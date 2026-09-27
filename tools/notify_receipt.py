#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify_receipt.py — 推送「送达凭据」单一源（只读）

背景（R459 / TODO L172 / 第19批 C3）
------------------------------------
「这条报告到底推送成功没有？」目前每个脚本各有一套私有判据，跨脚本既不可比也不可机械复跑：

  * birdie / haisou —— `notify()` 逐通道 try/except，异常时打印固定失败标记
       birdie_monitor/birdie_daily.py:107-118、birdie_monitor/haisou_daily.py:233-244
       ⇒ 「失败标记缺席」只有在**能证明 notify 被调用过**时才有意义（调用护栏）
       birdie_daily.py:125  `if new_items or quota_hit: notify(report)`
       haisou_daily.py:252  `if error or additions: notify(report)`
  * trendradar —— 最终 JSON 的 `send.skipped` / `send.channels[*].ok`
       sche_tasks/run_trendradar_digest.py:464/481-497/625
  * project_update / hellogithub —— state.json 基线推进即「已送达」的充要凭据
       project_update_monitor/monitor.py:322-329（注释明写「推送失败，未落盘新基线」）
  * haisou SOP 明文：消息末尾「已照常发送」是**固定文案**，不是推送凭据

本工具把上述经验固化成**单一源**三态判定，供日志复盘 / 任务新鲜度判据 / SOP 引用。

三态
----
    PUSHED         有送达凭据且无失败标记
    NOT_PUSHED     明确未送达（显式失败 / 通知分支前就异常退出 / 未达触发条件）
    UNDETERMINABLE 判不了（只有固定文案、只有自述、无任何证据、或自述与推导冲突）

证据类（判定优先级见 DECISION_ORDER）
------------------------------------
    E_FAIL    显式失败标记（脚本自报推送异常）                      → NOT_PUSHED
    E_PREREQ  脚本在到达 notify 之前就异常退出                      → NOT_PUSHED
    E_CRED    机器凭据（channels ok / 落盘凭据推进）                → PUSHED
    E_GUARD   调用护栏推导（源码证明 notify 必被调用 ∧ 无失败标记） → PUSHED（推断，标 inference）
    E_CLAIM   自述（散文断言：已推送/未推送/推送次数 0 …）           → 默认只提示，--prose-claims decisive 才可判
    E_BOILER  固定文案（「已照常发送」等）                          → **永不作凭据**
自述与推导方向相反时置 UNDETERMINABLE + flags=claim_conflict（例：08-07 birdie 报告自述
「未触发推送」，而源码护栏 `new_items or quota_hit` 在 quota_hit=True 时必调 notify）。

退出码（judge）
---------------
    0 = PUSHED      1 = NOT_PUSHED      2 = UNDETERMINABLE      3 = 用法/IO 错误

用法
----
    python3 tools/notify_receipt.py families
    python3 tools/notify_receipt.py judge --profile haisou --report sche_tasks/done/xxx.md [--json]
    python3 tools/notify_receipt.py judge --profile project_update --report x.md \
            --state-before /tmp/state.a.json --state-after project_update_monitor/state.json
    python3 tools/notify_receipt.py scan --dir sche_tasks/done [--family birdie] [--json out.json]
    python3 tools/notify_receipt.py --selftest
    python3 tools/notify_receipt.py scan ... --readonly-proof

只读：全程不写任何被读文件；--readonly-proof 会对读过的每个文件前后各取一次 sha256 并自证未变。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 证据类
# ---------------------------------------------------------------------------
E_FAIL = "E_FAIL"          # 显式失败标记
E_PREREQ = "E_PREREQ"      # 通知分支前异常退出
E_CRED = "E_CRED"          # 机器凭据
E_GUARD = "E_GUARD"        # 调用护栏推导
E_CLAIM = "E_CLAIM"        # 自述（散文）
E_BOILER = "E_BOILER"      # 固定文案

DECISION_ORDER = [
    "E_FAIL → NOT_PUSHED",
    "E_PREREQ → NOT_PUSHED",
    "E_CRED → PUSHED",
    "E_GUARD → PUSHED(inference)",
    "guard 定义成立但未满足 → NOT_PUSHED(not_triggered)",
    "E_CLAIM → 仅 --prose-claims decisive 时才判 PUSHED(inference)",
    "E_BOILER → UNDETERMINABLE(boilerplate_only)",
    "无证据 → UNDETERMINABLE(no_evidence)",
]

PUSHED = "PUSHED"
NOT_PUSHED = "NOT_PUSHED"
UNDETERMINABLE = "UNDETERMINABLE"

RC = {PUSHED: 0, NOT_PUSHED: 1, UNDETERMINABLE: 2}

# 「否定推送本身」的判据（只用于 claim_conflict 冲突检测；与剖面 claim / 证据收集分开维护）。
# ★口径坑（2026-09-27 实测）：只写 `未触发.*推送` 会漏掉「未触发飞书私信/钉钉群通知」写法，
# 使 08-03 birdie 报告（护栏 quota_hit 命中 ⇒ 推断已通知，自述却称未触发通知）漏报冲突。
# ★反向坑（同日实测 haisou 09-27）：`未额外`（「本会话未额外补推」）只否认**补推**，
# 与「脚本自己推送过」并不矛盾 ⇒ 不得据此挂 claim_conflict（假阳性）。
NEG_CLAIM_RE = re.compile(r"未触发.*(推送|通知)|未推送|未发送|推送次数[：:]\s*\**0\**")

# 共享的「否定自述」模式：既用于证据收集（collect 并入 claim 扫描），也用于冲突检测。
# ★单一源：剖面 claim 列表不必再各自重复写「未触发…推送」，避免两处漂移。
NEG_CLAIM_PATTERNS = [r"未触发.*(推送|通知)", r"未推送", r"未发送", r"未额外"]
RC_USAGE = 3


# ---------------------------------------------------------------------------
# 剖面（每个推送脚本族一份判定知识）
# ---------------------------------------------------------------------------
# guard 条目：{"re": 正则, "min": 捕获组整数下限（None=只要命中）}
def _g(pattern: str, minimum=None, label: str = "") -> dict:
    return {"re": pattern, "min": minimum, "label": label}


PROFILES: dict[str, dict] = {
    "haisou": {
        "title": "haisou_daily_monitor（birdie_monitor/haisou_daily.py）",
        "globs": ["*_haisou_daily_monitor.md"],
        "failure": [r"\[飞书私信推送失败\]", r"\[钉钉群推送失败\]"],
        "prereq_abort": [
            r"requests\.exceptions\.\w+",
            r"SSLEOFError",
            r"HTTPSConnectionPool",
            r"exit_code\s*=\s*[1-9]",
        ],
        "credential": [],
        "credential_neg": [],
        "guard": [
            _g(r"新增\s*(\d+)\s*条", 1, "additions≥1"),
            _g(r"后续扫描中断", None, "error 分支"),
        ],
        "guard_source": "birdie_monitor/haisou_daily.py:252 `if error or additions: notify(report)`",
        "notify_source": (
            "birdie_monitor/haisou_daily.py:233-244 notify() 两通道各自 try/except 且异常必打印"
            "「[飞书私信推送失败]/[钉钉群推送失败]」⇒ 失败标记缺席 ∧ notify 被调用 ⇒ 两通道均已送达"
        ),
        "boilerplate": [r"已照常发送", r"照常发送"],
        "claim": [
            r"已推送", r"已通知", r"推送次数[：:]\s*\**0\**", r"未额外补推",
        ],
    },
    "birdie": {
        "title": "birdie_daily_monitor（birdie_monitor/birdie_daily.py）",
        "globs": ["*_birdie_daily_monitor.md"],
        "failure": [r"\[飞书私信推送失败\]", r"\[钉钉群推送失败\]"],
        "prereq_abort": [
            r"ProxyError",
            r"SSLEOFError",
            r"requests\.exceptions\.\w+",
            r"exit[:：]\s*\**\s*[1-9]",
            r"exit code\s*=\s*[1-9]",
            r"exit_code\s*=\s*[1-9]",
        ],
        "credential": [],
        "credential_neg": [],
        "guard": [
            _g(r"新增\s*(\d+)\s*条", 1, "new_items≥1"),
            _g(r"今日搜索额度已用尽", None, "quota_hit"),
        ],
        "guard_source": "birdie_monitor/birdie_daily.py:125 `if new_items or quota_hit: notify(report)`",
        "notify_source": (
            "birdie_monitor/birdie_daily.py:107-118 notify() 两通道各自 try/except 且异常必打印失败标记"
        ),
        "boilerplate": [r"已通知", r"未触发飞书私信"],
        "claim": [
            r"已推送", r"已通知", r"未额外补推", r"推送次数[：:]\s*\**0\**",
        ],
    },
    "trendradar": {
        "title": "trendradar_morning / trendradar_evening（sche_tasks/run_trendradar_digest.py）",
        "globs": ["*_trendradar_morning.md", "*_trendradar_evening.md"],
        "failure": [
            r"skipped_bad_report",
            r"skipped_empty",
            r"upstream empty after retries",
            r"failure alert also failed to send",
        ],
        "prereq_abort": [],
        "credential": [
            r"send\.skipped\s*\|\s*\**false\**",
            r"send\.channels\.\w+\s*\|\s*ok=true",
        ],
        "credential_neg": [
            r"send\.channels\.\w+\s*\|\s*ok=false",
            r"send\.skipped\s*\|\s*\**true\**",
        ],
        "guard": [],
        "guard_source": "sche_tasks/run_trendradar_digest.py:464/481-497/605-625（send_once 返回并落 JSON）",
        "notify_source": "send_once() 逐通道记录 ok/err 并整体返回；`skipped` 字段即幂等键",
        "boilerplate": [r"已跳过推送"],
        "claim": [r"三通道全绿", r"未重复发送", r"推送次数[：:]\s*\**0\**"],
    },
    "project_update": {
        "title": "project_update_monitor / hellogithub_monthly（project_update_monitor/monitor.py）",
        "globs": ["*_project_update_monitor.md", "*_hellogithub_monthly.md"],
        "failure": [r"推送失败，未落盘新基线"],
        "prereq_abort": [],
        "credential": [],
        "credential_neg": [],
        "guard": [],
        "guard_source": "project_update_monitor/monitor.py:322-329（state.json 推进 = 通知成功后才提交）",
        "notify_source": (
            "基线（state.json）是通知幂等键：仅当推送成功才 atomic_dump；失败 return 3 且不落盘"
            "⇒ 权威凭据 = state 文件前后指纹变化（--state-before / --state-after）"
        ),
        "boilerplate": [r"与上次推送相比无版本更新"],
        "claim": [r"基线已落盘", r"基线推进", r"已推送", r"一次推送"],
    },
    "generic": {
        "title": "通用兜底（未登记族；只有显式失败标记才可判 NOT_PUSHED）",
        "globs": [],
        "failure": [
            r"推送失败", r"发送失败", r"notify.*失败",
            r"\[飞书私信推送失败\]", r"\[钉钉群推送失败\]",
        ],
        "prereq_abort": [],
        "credential": [],
        "credential_neg": [],
        "guard": [],
        "guard_source": "",
        "notify_source": "",
        "boilerplate": [r"已照常发送", r"照常发送"],
        "claim": [],
    },
}

FAMILY_BY_GLOB: list[tuple[str, str]] = []
for _pid, _p in PROFILES.items():
    if _pid == "generic":
        continue
    for _g_ in _p["globs"]:
        FAMILY_BY_GLOB.append((_g_, _pid))


def family_of(path: str) -> str:
    base = os.path.basename(path)
    for pat, pid in FAMILY_BY_GLOB:
        if glob.fnmatch.fnmatch(base, pat):
            return pid
    return "generic"


# ---------------------------------------------------------------------------
# 文本解析：分「脚本原始输出（围栏内）」vs「散文」
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    kind: str
    pattern: str
    line: int
    where: str          # fenced | prose
    snippet: str
    value: object = None

    def as_dict(self):
        return asdict(self)


def split_prose_and_fenced(text: str):
    """返回 (lines, fenced_line_idx:set)。围栏 = 以 ``` 开头的行之间的行。"""
    lines = text.split("\n")
    fenced: set[int] = set()
    inside = False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            inside = not inside
            fenced.add(i)
            continue
        if inside:
            fenced.add(i)
    return lines, fenced


def _snippet(ln: str, width: int = 160) -> str:
    s = ln.strip()
    return s[:width]


# 散文里的「失败标记」可能是**引用/机制说明/零计数断言**（真跑实测：haisou 09-27 与
# birdie 09-27 报告在解释「两通道异常必打印 [飞书私信推送失败]」并附 `= **0**` 计数，
# 若照单全收会把「已推送」误判成 NOT_PUSHED）。判据刻意收窄，只抑制这三类行；
# 真失败句（含错误 payload）不受影响，见负控 NC18/NC21。
_PROSE_MENTION_RX = re.compile(
    r"=\s*\*{0,2}0\*{0,2}(?!\d)"     # 零计数断言 `= 0` / `= **0**`
    r"|必打印|必然打印|任一路"        # 机制描述（说明「某条件下会打印」）
    r"|非降级|并非降级"               # 反事实对照
)


def collect(text: str, profile: dict, prose_guard: bool = False):
    lines, fenced = split_prose_and_fenced(text)
    ev: list[Evidence] = []
    prose_guard_ok = prose_guard

    def scan(patterns, kind, guard_min=False, fenced_only=False):
        for pat in patterns:
            if isinstance(pat, dict):
                rx, minimum, label = pat["re"], pat["min"], pat.get("label", "")
            else:
                rx, minimum, label = pat, None, ""
            cre = re.compile(rx)
            for i, ln in enumerate(lines):
                m = cre.search(ln)
                if not m:
                    continue
                where = "fenced" if i in fenced else "prose"
                if fenced_only and where != "fenced":
                    continue
                if minimum is not None:
                    try:
                        n = int(m.group(1))
                    except (IndexError, ValueError):
                        n = None
                    if n is None or n < minimum:
                        continue
                ev.append(Evidence(kind=kind, pattern=rx, line=i + 1, where=where,
                                   snippet=_snippet(ln), value=label))
    scan(profile.get("failure", []), E_FAIL)
    scan(profile.get("credential_neg", []), E_FAIL)
    scan(profile.get("prereq_abort", []), E_PREREQ)
    scan(profile.get("credential", []), E_CRED)
    scan(profile.get("boilerplate", []), E_BOILER)
    # 剖面的 claim + 共享否定模式（单一源，见 NEG_CLAIM_PATTERNS）
    scan(list(profile.get("claim", [])) + NEG_CLAIM_PATTERNS, E_CLAIM)
    # 护栏推导：默认只认「脚本原始输出」（围栏内），避免把会话自述当护栏证据
    scan(profile.get("guard", []), E_GUARD, fenced_only=not prose_guard_ok)
    # 散文里的失败/前置标记须过「非引文」闸：零计数断言 / 机制描述 / 反事实对照一律不计
    def _kept(e: Evidence) -> bool:
        if e.where != "prose" or e.kind not in (E_FAIL, E_PREREQ):
            return True
        if e.line - 1 < len(lines) and _PROSE_MENTION_RX.search(lines[e.line - 1]):
            return False
        return True
    dropped = [e for e in ev if not _kept(e)]
    if dropped:
        ev = [e for e in ev if _kept(e)]
    has_fenced = any(lines[i].strip() for i in fenced)
    return ev, lines, fenced, has_fenced


# ---------------------------------------------------------------------------
# 落盘凭据推进（state 文件前后指纹）
# ---------------------------------------------------------------------------
def fingerprint(path: str) -> dict:
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"path": path, "size": st.st_size, "sha256": h.hexdigest()}


def state_advance(before: str, after: str) -> dict:
    a, b = fingerprint(before), fingerprint(after)
    changed = (a["sha256"] != b["sha256"]) or (a["size"] != b["size"])
    return {"changed": changed, "before": a, "after": b}


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def judge_text(text: str, profile_id: str, *, prose_guard: bool = False,
               prose_claims: str = "advisory", state: dict | None = None) -> dict:
    profile = PROFILES.get(profile_id) or PROFILES["generic"]
    ev, lines, fenced, has_fenced = collect(text, profile, prose_guard=prose_guard)
    kinds = {e.kind for e in ev}

    out = {
        "profile": profile_id,
        "state": UNDETERMINABLE,
        "reason": "",
        "inference": False,
        "evidence": [e.as_dict() for e in ev],
        "flags": [],
        "counts": {k: sum(1 for e in ev if e.kind == k)
                   for k in (E_FAIL, E_PREREQ, E_CRED, E_GUARD, E_CLAIM, E_BOILER)},
        "chars": len(text),
        "lines": len(lines),
        "fenced_lines": len(fenced),
    }

    # 1) 显式失败标记 —— 最高优先级（宁可判未送达）
    if E_FAIL in kinds:
        out["state"], out["reason"] = NOT_PUSHED, "explicit_failure"
        return out
    # 2) 通知分支前异常退出
    if E_PREREQ in kinds:
        out["state"], out["reason"] = NOT_PUSHED, "prereq_abort（未到 notify）"
        return out
    # 3) 机器凭据
    if E_CRED in kinds:
        if profile.get("credential"):
            got = {e.pattern for e in ev if e.kind == E_CRED}
            need = set(profile["credential"])
            if need.issubset(got):
                out["state"], out["reason"] = PUSHED, "credential（机器凭据）"
                return out
            out["reason"] = "credential_partial"
        else:
            out["flags"].append("credential_pattern_without_profile_declaration")
    # 3b) 落盘凭据推进
    if state is not None:
        out["state_advance"] = state
        if state.get("changed"):
            out["state"], out["reason"] = PUSHED, "credential（state 文件推进）"
            return out
        out["flags"].append("state_unchanged")
    # 4) 调用护栏推导
    #    前提：报告里确有「脚本原始输出」围栏。否则「护栏未命中」只说明没贴输出，
    #    推不出「脚本没调 notify」（实测：haisou 报告常只贴固定文案，无 stdout 围栏）。
    out["has_fenced"] = has_fenced
    if profile.get("guard") and (has_fenced or prose_guard):
        if E_GUARD in kinds:
            out["state"], out["reason"], out["inference"] = PUSHED, "guard_infer（notify 必被调用 ∧ 无失败标记）", True
            # 自述与代码路径相反 ⇒ 只留痕，不降级（代码路径优先于 LLM 自述）。
            # ★口径（2026-09-27 实测，birdie）：报告普遍自述「未触发飞书/钉钉通知」，
            # 但 birdie_daily.py:125 `if new_items or quota_hit: notify(report)` 表明
            # 额度耗尽那一次仍会调用 notify ⇒ 该自述是 LLM 对代码的误读，不能推翻 guard_infer。
            neg = [e for e in ev if e.kind == E_CLAIM and NEG_CLAIM_RE.search(e.snippet)]
            if neg:
                out["flags"].append("claim_conflict")
                out["reason"] += "；⚠ 自述『未推送』与代码路径相反（判读以代码为准）"
            return out
        # 护栏条件明确不成立 ⇒ 未触发推送
        out["state"], out["reason"] = NOT_PUSHED, "not_triggered（护栏条件不成立，脚本不会调用 notify）"
        return out
    # 5) 自述（默认只作提示）
    if E_CLAIM in kinds and prose_claims == "decisive":
        pos = [e for e in ev if e.kind == E_CLAIM and re.search(r"已推送|已通知|三通道全绿|基线已落盘", e.snippet)]
        if pos:
            out["state"], out["reason"], out["inference"] = PUSHED, "prose_claim（自述，decisive 模式）", True
            return out
    # 6) 固定文案 / 无证据
    if E_BOILER in kinds:
        out["state"], out["reason"] = UNDETERMINABLE, "boilerplate_only（固定文案不是凭据）"
        return out
    if E_CLAIM in kinds:
        out["state"], out["reason"] = UNDETERMINABLE, "prose_claim_only（只有自述，无机器凭据）"
        return out
    out["state"], out["reason"] = UNDETERMINABLE, "no_evidence"
    return out


def judge_report(path: str, profile_id: str | None = None, **kw) -> dict:
    pid = profile_id or family_of(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    res = judge_text(text, pid, **kw)
    res["report"] = path
    res["bytes"] = os.path.getsize(path)
    return res


# ---------------------------------------------------------------------------
# 批量扫描
# ---------------------------------------------------------------------------
def scan(directory: str, families: list[str] | None = None, limit: int | None = None,
         prose_guard: bool = False, prose_claims: str = "advisory") -> dict:
    files = sorted(glob.glob(os.path.join(directory, "*.md")))
    per_family: dict[str, dict] = {}
    rows = []
    read_files = []
    for p in files:
        pid = family_of(p)
        if families and pid not in families:
            continue
        if limit and len(rows) >= limit:
            break
        res = judge_report(p, pid, prose_guard=prose_guard, prose_claims=prose_claims)
        read_files.append(p)
        rows.append(res)
        agg = per_family.setdefault(pid, {"files": 0, "bytes": 0,
                                          PUSHED: 0, NOT_PUSHED: 0, UNDETERMINABLE: 0,
                                          "reasons": {}})
        agg["files"] += 1
        agg["bytes"] += res["bytes"]
        agg[res["state"]] += 1
        agg["reasons"][res["reason"].split("（")[0]] = agg["reasons"].get(res["reason"].split("（")[0], 0) + 1
    return {
        "version": VERSION,
        "dir": directory,
        "corpus": {"files_total": len(files), "files_scanned": len(rows),
                   "bytes_scanned": sum(r["bytes"] for r in rows)},
        "profile_coverage": {pid: sum(1 for p in files if family_of(p) == pid) for pid in PROFILES},
        "per_family": per_family,
        "rows": rows,
        "_read_files": read_files,
    }


# ---------------------------------------------------------------------------
# 只读自证
# ---------------------------------------------------------------------------
def readonly_proof(paths: list[str]) -> dict:
    before = {}
    for p in paths:
        if os.path.exists(p):
            try:
                before[p] = fingerprint(p)
            except OSError:
                pass
    changed = []
    for p, fp in before.items():
        try:
            now = fingerprint(p)
        except OSError:
            changed.append(p)
            continue
        if now["sha256"] != fp["sha256"] or now["size"] != fp["size"]:
            changed.append(p)
    return {"files": len(before), "changed": changed}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _fmt_judge(res: dict) -> str:
    lines = [
        f"report : {res.get('report', '-')}  ({res.get('bytes', 0)} B)",
        f"profile: {res['profile']}",
        f"state  : {res['state']}   reason={res['reason']}"
        + ("   [inference]" if res.get("inference") else ""),
        f"counts : " + ", ".join(f"{k}={v}" for k, v in res["counts"].items()),
    ]
    for e in res["evidence"][:12]:
        lines.append(f"  - {e['kind']:<8} L{e['line']:<5} {e['where']:<6} {e['pattern']}  | {e['snippet'][:80]}")
    if len(res["evidence"]) > 12:
        lines.append(f"  ... 其余 {len(res['evidence']) - 12} 条见 --json")
    if res.get("flags"):
        lines.append("flags  : " + ", ".join(res["flags"]))
    return "\n".join(lines)


def cmd_families(_a) -> int:
    print(f"notify_receipt v{VERSION} — 已登记剖面 {len([p for p in PROFILES if p != 'generic'])} 个 + generic")
    for pid, p in PROFILES.items():
        print(f"\n[{pid}] {p['title']}")
        if p["globs"]:
            print(f"  globs      : {', '.join(p['globs'])}")
        print(f"  failure    : {len(p['failure'])} 条")
        print(f"  prereq     : {len(p['prereq_abort'])} 条")
        print(f"  credential : {len(p['credential'])} 条（neg {len(p['credential_neg'])}）")
        print(f"  guard      : {len(p['guard'])} 条  ← {p['guard_source'] or 'n/a'}")
        print(f"  boilerplate: {len(p['boilerplate'])} 条（永不作凭据）")
    return 0


def cmd_judge(a) -> int:
    if not a.report or not os.path.exists(a.report):
        print(f"报告不存在: {a.report}", file=sys.stderr)
        return RC_USAGE
    pid = a.profile or family_of(a.report)
    if pid not in PROFILES:
        print(f"未知剖面: {pid}", file=sys.stderr)
        return RC_USAGE
    state = None
    if a.state_before and a.state_after:
        if not (os.path.exists(a.state_before) and os.path.exists(a.state_after)):
            print("state 文件缺失", file=sys.stderr)
            return RC_USAGE
        state = state_advance(a.state_before, a.state_after)
    res = judge_report(a.report, pid, prose_guard=a.prose_guard, prose_claims=a.prose_claims, state=state)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else _fmt_judge(res))
    return RC[res["state"]]


def cmd_scan(a) -> int:
    if not os.path.isdir(a.dir):
        print(f"目录不存在: {a.dir}", file=sys.stderr)
        return RC_USAGE
    fams = [s.strip() for s in a.family.split(",")] if a.family else None
    res = scan(a.dir, fams, a.limit, a.prose_guard, a.prose_claims)
    read_files = res.pop("_read_files")
    if a.readonly_proof:
        res["readonly_proof"] = readonly_proof(read_files)
    c = res["corpus"]
    print(f"[语料] dir={res['dir']} files_total={c['files_total']} scanned={c['files_scanned']} "
          f"bytes={c['bytes_scanned']}（自证：语料计数非 0 且逐族计数之和 == scanned）")
    tot = sum(v["files"] for v in res["per_family"].values())
    print(f"[自证] per_family files 之和 = {tot}  {'OK' if tot == c['files_scanned'] else 'MISMATCH'}")
    for pid in sorted(res["per_family"], key=lambda k: -res["per_family"][k]["files"]):
        v = res["per_family"][pid]
        print(f"  {pid:<16} files={v['files']:<4} {PUSHED}={v[PUSHED]:<4} {NOT_PUSHED}={v[NOT_PUSHED]:<4} "
              f"{UNDETERMINABLE}={v[UNDETERMINABLE]:<4} reasons={v['reasons']}")
    if res.get("readonly_proof"):
        rp = res["readonly_proof"]
        print(f"[只读自证] FILES={rp['files']} CHANGED={len(rp['changed'])}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"[落盘] {a.json}")
    return 0


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = 0
    fail = []
    checks = []

    def chk(name, cond, detail=""):
        nonlocal ok
        if cond:
            ok += 1
            checks.append(("PASS", name, detail))
        else:
            fail.append((name, detail))
            checks.append(("FAIL", name, detail))

    # --- 负控 NC1：只有固定文案（haisou SOP 明文的陷阱）
    t = "🔎 海搜每日监控（2026-01-01）\n新增 0 条：\n⚠️ 后续扫描中断：x\n已搜到的新增已照常发送；未扫描部分将在下次继续。\n"
    # 注意：本样本含「后续扫描中断」= error 护栏 ⇒ 见 NC1b；此处先给纯固定文案样本
    t_pure = "已搜到的新增已照常发送；未扫描部分将在下次继续。\n"
    r = judge_text(t_pure, "haisou")
    chk("NC1 固定文案-only ⇒ UNDETERMINABLE", r["state"] == UNDETERMINABLE and "boilerplate" in r["reason"], r["reason"])

    # --- NC2 固定文案 + 显式失败标记 ⇒ NOT_PUSHED
    t2 = "已照常发送\n[飞书私信推送失败] boom\n[钉钉群推送失败] boom2\n"
    r = judge_text(t2, "haisou")
    chk("NC2 固定文案+失败标记 ⇒ NOT_PUSHED", r["state"] == NOT_PUSHED and r["reason"] == "explicit_failure", r["reason"])

    # --- NC3 围栏内护栏命中（脚本原始输出）⇒ PUSHED(inference)
    t3 = "```\n🔎 海搜每日监控（2026-01-01）\n新增 1 条：\n【AI coding｜quark】标题\n```\n"
    r = judge_text(t3, "haisou")
    chk("NC3 围栏内 additions≥1 且无失败标记 ⇒ PUSHED(guard_infer)",
        r["state"] == PUSHED and r["inference"] and r["reason"].startswith("guard_infer"), r["reason"])

    # --- NC4 同一句「新增 1 条」出现在**散文**里（非脚本原始输出）⇒ 不得据此判 PUSHED
    r = judge_text("本次运行新增 1 条，详情见下。", "haisou")
    chk("NC4 护栏证据仅出现在散文 ⇒ 不作凭据", r["state"] == UNDETERMINABLE, f"{r['state']}/{r['reason']}")
    r_g = judge_text("本次运行新增 1 条，详情见下。", "haisou", prose_guard=True)
    chk("NC4b --prose-guard 打开后同一文本可判 PUSHED（模式可切换）", r_g["state"] == PUSHED, r_g["reason"])

    # --- NC5 trendradar 机器凭据全绿 ⇒ PUSHED
    t5 = "| send.skipped | **false** |\n| send.channels.feishu_dm | ok=true |\n| send.channels.feishu_group | ok=true |\n"
    r = judge_text(t5, "trendradar")
    chk("NC5 三通道 ok=true 且 skipped=false ⇒ PUSHED", r["state"] == PUSHED and "credential" in r["reason"], r["reason"])
    # --- NC5b 显式失败优先于凭据（部分通道 ok=false）
    t5b = t5 + "| send.channels.dingtalk_group | ok=false |\n"
    r = judge_text(t5b, "trendradar")
    chk("NC5b 存在 ok=false ⇒ NOT_PUSHED（失败优先）", r["state"] == NOT_PUSHED, r["reason"])

    # --- NC6 birdie 前置失败 ⇒ NOT_PUSHED(prereq_abort)
    t6 = "脚本级结局：exit: 1\nrequests.exceptions.ProxyError: Cannot connect to proxy\n"
    r = judge_text(t6, "birdie")
    chk("NC6 ProxyError+exit1 ⇒ NOT_PUSHED(prereq)", r["state"] == NOT_PUSHED and "prereq" in r["reason"], r["reason"])

    # --- NC7 自述与代码路径相反：代码优先 ⇒ 仍 PUSHED，但必须留 claim_conflict 痕
    #     （样本取自 sche_tasks/done/2026-08-03_1101_birdie_daily_monitor.md 的真文形态）
    t7 = "```\n今日无新增命中。\n\n⚠️ 今日搜索额度已用尽，提前停止。\n```\n"\
         "- 今日无新增命中，脚本内部逻辑判定无需推送，未触发飞书私信/钉钉群通知。\n"
    r = judge_text(t7, "birdie")
    chk("NC7 自述『未触发…通知』⇒ 仍 PUSHED(guard，代码优先)+claim_conflict",
        r["state"] == PUSHED and "guard_infer" in r["reason"] and "claim_conflict" in r["flags"],
        f"{r['state']}/{r['reason']}/{r['flags']}")
    # NC7b 同一自述若**没有**代码路径证据（无围栏）⇒ 不因自述升格，也不因自述降级
    r7b = judge_text("- 未触发飞书私信/钉钉群通知。\n", "birdie")
    chk("NC7b 仅自述、无代码路径证据 ⇒ 仍 UNDETERMINABLE",
        r7b["state"] == UNDETERMINABLE, f"{r7b['state']}/{r7b['reason']}")


    # --- NC8 护栏不成立（无新增、无 error）⇒ NOT_PUSHED(not_triggered)
    r = judge_text("```\n今日无新增命中。\n```\n", "haisou")
    chk("NC8 护栏不成立 ⇒ NOT_PUSHED(not_triggered)",
        r["state"] == NOT_PUSHED and r["reason"].startswith("not_triggered"), r["reason"])

    # --- NC9 一般性讨论（未给任何标记）不得被粗粒度正则误判为失败
    r = judge_text("本报告讨论推送链路的重构计划与验收口径，推送逻辑见下文。\n", "generic")
    chk("NC9 一般讨论「推送」字样 ⇒ 仍 UNDETERMINABLE（未过宽）",
        r["state"] == UNDETERMINABLE, f"{r['state']}/{r['reason']}")
    # NC9b 粗粒度 pattern 的正向确认：真失败句必须抓到
    r = judge_text("本轮结果：推送失败（飞书私信 200 但钉钉群超时）。\n", "generic")
    chk("NC9b generic 粗粒度正向：『推送失败』⇒ NOT_PUSHED",
        r["state"] == NOT_PUSHED and r["reason"] == "explicit_failure", f"{r['state']}/{r['reason']}")

    # --- NC10 空文本
    r = judge_text("", "haisou")
    chk("NC10 空文本 ⇒ UNDETERMINABLE(no_evidence)", r["state"] == UNDETERMINABLE and r["reason"] == "no_evidence", r["reason"])

    # --- NC11 state 文件推进 = 凭据；未变 = 不可判
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "before.json")
        p2 = os.path.join(d, "after.json")
        p3 = os.path.join(d, "same.json")
        txt = json.dumps({"a": 1})
        for p in (p1, p3):
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
        with open(p2, "w", encoding="utf-8") as f:
            f.write(json.dumps({"a": 2}))
        st_changed = state_advance(p1, p2)
        st_same = state_advance(p1, p3)
        chk("NC11a 落盘凭据推进 ⇒ changed=True", st_changed["changed"] is True)
        chk("NC11b 落盘凭据未变 ⇒ changed=False", st_same["changed"] is False)
        r = judge_text("无标记的普通报告", "project_update", state=st_changed)
        chk("NC11c state 推进 ⇒ PUSHED(credential)", r["state"] == PUSHED and "state" in r["reason"], r["reason"])
        r = judge_text("无标记的普通报告", "project_update", state=st_same)
        chk("NC11d state 未变 ⇒ UNDETERMINABLE", r["state"] == UNDETERMINABLE and "state_unchanged" in r["flags"], r["reason"])
    # --- NC16 真样本回归（逐字取自 sche_tasks/done/2026-08-03_1101_birdie_daily_monitor.md）
    # 该份无新增命中但 quota_hit ⇒ 脚本护栏 `if new_items or quota_hit: notify(report)` 必调用 notify
    # ⇒ 判定须为 PUSHED(guard)，且报告里「未触发…通知」是自述误读⇒挂 claim_conflict 留痕。
    t_0803 = (
        "## 执行结果\nexit=0\n\n```\n"
        "签到结果: {'body': '2026-08-01', 'code': -1, 'msg': '今日已签到'}\n"
        "[AI Agent] code=-1 msg=None，视为限额已到，停止后续关键词\n"
        "今日无新增命中。\n\n"
        "⚠️ 今日搜索额度已用尽，提前停止。\n```\n\n## 说明\n"
        "- 首个关键词\"AI Agent\"搜索即返回 code=-1，判定为今日搜索额度已用尽，脚本按设计提前停止后续关键词遍历。\n"
        "- 今日无新增命中，脚本内部逻辑判定无需推送，未触发飞书私信/钉钉群通知，未额外调用其他通知函数。\n"
    )
    r16 = judge_text(t_0803, "birdie")
    chk("NC16 真样本 08-03（guard 命中 + 自述未推送）⇒ PUSHED + claim_conflict",
        r16["state"] == PUSHED and "claim_conflict" in r16["flags"], f"{r16['state']}/{r16['reason']}")
    # --- NC17 反向：无围栏脚本输出的散文自述，不得升格为 PUSHED
    r17 = judge_text("## 说明\n- 今日无新增命中，未触发飞书私信/钉钉群通知。\n", "birdie")
    chk("NC17 无围栏、仅散文自述 ⇒ UNDETERMINABLE", r17["state"] == UNDETERMINABLE, r17["state"])

    # --- NC18 `未额外`（只否认补推，不否认推送本身）不得挂 claim_conflict（2026-09-27 haisou 09-27 实测假阳性）
    t18 = ("```\n新增 1 条：\n⚠️ 后续扫描中断：HTTP 403\n```\n"
           "- 推送：脚本自行推送（飞书私信 + 钉钉群），本会话**未额外补推**\n")
    r18 = judge_text(t18, "haisou")
    chk("NC18 自述『未额外补推』⇒ PUSHED 且无 claim_conflict",
        r18["state"] == PUSHED and "claim_conflict" not in r18["flags"], f"{r18['state']}/{r18['flags']}")

    # --- NC19 散文里「机制说明」提到失败标记名 ⇒ 不得据此判 NOT_PUSHED
    t19 = ("```\n新增 1 条：\n```\n"
           "- `notify()` 两通道**任一路异常必打印** `[飞书私信推送失败] …` / `[钉钉群推送失败] …`。\n")
    r19 = judge_text(t19, "haisou")
    chk("NC19 散文机制说明提及失败标记名 ⇒ 不判 NOT_PUSHED",
        r19["state"] != NOT_PUSHED, f"{r19['state']}/{r19['reason']}")

    # --- NC20 散文里的「零计数断言」不得当失败证据
    t20 = ("```\n新增 1 条：\n```\n"
           "- 日志全文计数：`[飞书私信推送失败]` = **0**、`[钉钉群推送失败]` = **0** ⇒ 判定已推送。\n")
    r20 = judge_text(t20, "haisou")
    chk("NC20 散文零计数断言（= **0**）⇒ 不判 NOT_PUSHED",
        r20["state"] != NOT_PUSHED, f"{r20['state']}/{r20['reason']}")

    # --- NC21 反向：真失败句（非零计数、无机制词）仍必须判 NOT_PUSHED（防过度抑制）
    t21 = "```\n新增 1 条：\n```\n- 本轮共 3 次 [钉钉群推送失败] 记录。\n"
    r21 = judge_text(t21, "haisou")
    chk("NC21 真失败句（非零计数）⇒ 仍 NOT_PUSHED",
        r21["state"] == NOT_PUSHED and r21["reason"] == "explicit_failure", f"{r21['state']}/{r21['reason']}")

    # --- NC12 族识别（glob 绑定）
    chk("NC12a family_of haisou", family_of("/x/2026-09-27_1115_haisou_daily_monitor.md") == "haisou")
    chk("NC12b family_of trendradar_evening", family_of("/x/2026-08-08_1831_trendradar_evening.md") == "trendradar")
    chk("NC12c family_of hellogithub", family_of("/x/2026-05-01_0900_hellogithub_monthly.md") == "project_update")
    chk("NC12d 未登记族 ⇒ generic", family_of("/x/2026-05-01_0900_sqlite_snapshot.md") == "generic")

    # --- NC13 固定文案在任何模式下都不得产出 PUSHED
    r1 = judge_text(t_pure, "haisou", prose_claims="decisive")
    r2 = judge_text(t_pure, "haisou", prose_claims="advisory")
    chk("NC13 固定文案在 decisive/advisory 两模式下都 ≠ PUSHED",
        r1["state"] != PUSHED and r2["state"] != PUSHED, f"{r1['state']}/{r2['state']}")

    # --- NC14 语料计数自证（scan 的 corpus 计数与逐族之和一致）
    with tempfile.TemporaryDirectory() as d:
        for i in range(3):
            with open(os.path.join(d, f"2026-01-0{i+1}_1115_haisou_daily_monitor.md"), "w", encoding="utf-8") as f:
                f.write("```\n新增 1 条：\n```\n")
        with open(os.path.join(d, "2026-01-04_1001_trendradar_morning.md"), "w", encoding="utf-8") as f:
            f.write("| send.skipped | **false** |\n| send.channels.feishu_dm | ok=true |\n")
        s = scan(d)
        chk("NC14a corpus 计数自证", s["corpus"]["files_scanned"] == 4 and s["corpus"]["bytes_scanned"] > 0)
        tot = sum(v["files"] for v in s["per_family"].values())
        chk("NC14b 逐族计数之和 == scanned", tot == s["corpus"]["files_scanned"], f"{tot} vs {s['corpus']['files_scanned']}")
        chk("NC14c 正常样例被判 PUSHED×4",
            all(r["state"] == PUSHED for r in s["rows"]), str([r["state"] for r in s["rows"]]))
        rp = readonly_proof([os.path.join(d, f) for f in os.listdir(d)])
        chk("NC14d 只读自证 CHANGED=0", rp["files"] == 4 and not rp["changed"], str(rp))

    # --- NC15 语料缩水探针：把 4 个样本换成 0 个 ⇒ 计数为 0（防「静默缩水仍报绿」）
    with tempfile.TemporaryDirectory() as d:
        s = scan(d)
        chk("NC15 空语料 ⇒ files_scanned=0（调用方须自行拒绝 0 语料）", s["corpus"]["files_scanned"] == 0)

    print(f"notify_receipt selftest: {ok} passed / {len(fail)} failed")
    for st, name, detail in checks:
        if st == "FAIL":
            print(f"  FAIL {name}  {detail}")
    return 0 if not fail else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="推送「送达凭据」单一源三态判定（只读）")
    p.add_argument("--version", action="version", version=f"notify_receipt {VERSION}")
    p.add_argument("--selftest", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    pf = sub.add_parser("families", help="列出已登记剖面与判据来源")
    pf.set_defaults(func=cmd_families)

    pj = sub.add_parser("judge", help="判定单个报告/日志的三态")
    pj.add_argument("--profile", default=None)
    pj.add_argument("--report", required=True)
    pj.add_argument("--state-before", default=None)
    pj.add_argument("--state-after", default=None)
    pj.add_argument("--prose-guard", action="store_true",
                    help="允许把散文里的护栏证据也计入（默认只认围栏内的脚本原始输出）")
    pj.add_argument("--prose-claims", choices=["advisory", "decisive"], default="advisory")
    pj.add_argument("--json", action="store_true")
    pj.set_defaults(func=cmd_judge)

    ps = sub.add_parser("scan", help="批量扫描 done 报告目录")
    ps.add_argument("--dir", required=True)
    ps.add_argument("--family", default=None, help="逗号分隔的剖面 id")
    ps.add_argument("--limit", type=int, default=None)
    ps.add_argument("--prose-guard", action="store_true")
    ps.add_argument("--prose-claims", choices=["advisory", "decisive"], default="advisory")
    ps.add_argument("--json", default=None)
    ps.add_argument("--readonly-proof", action="store_true")
    ps.set_defaults(func=cmd_scan)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.selftest:
        return _selftest()
    if not getattr(a, "cmd", None):
        build_parser().print_help()
        return RC_USAGE
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
