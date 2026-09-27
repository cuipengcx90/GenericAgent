#!/usr/bin/env python3
"""error_signal.py — 结构化「错误事件」提取器（替代关键词命中计数）

背景（R466 / TODO 第20批 C6）
---------------------------
旧口径 `tail_errors_2000`（temp/efficiency_monitor.py:63-64）把日志尾部 2000 行里
任意一行**包含** error/failed/traceback/exception/no module named/failed: 就 +1。
该日志（logs/autonomous_reflect.log）会把工具输入输出、报告正文、源码片段原样回显，
因此「命中数」被回声严重污染（2026-09-27 实测 26 命中里只有少数是错误事件本体），
同时又会漏掉不含这些词的失败（例如非零退出码、超时）。

本模块改为**结构化事件**计数，判据分三档：

* EVENT（高置信）：锚点必须出现在**行首**（列 0），或为 traceback 块（起止配对，整块只计 1）
* POSSIBLE（低置信）：行内/缩进处出现关键词 —— 典型是正文/源码/清单回声，**不计入 health**
* BENIGN（已知良性噪音）：numpy/pyarrow ABI、LLMLingua warmup 等，**计入 benign 清单但不计入 health**
  （可见性不丢：事件明细里仍能查到）

锚点（行首）
-----------
1. `Traceback (most recent call last):` … 首个行首异常终止行 ⇒ 1 个 traceback 事件
2. `YYYY-MM-DDTHH:MM:SS[.ffffff]Z ERROR|WARN|FATAL|CRITICAL …`（结构化日志，如 codex/rmcp）
3. 行首 `ERROR|FATAL|CRITICAL` / `[ERROR]` / `!!!Error:`（工具显式错误标记）
4. 行首 `rc=1` / `exit code 1` / `returned non-zero exit status 1` / `exit_code=1`（子进程非零退出）
5. 行首 `TimeoutExpired` / `timed out`

绝对禁止：使用第三方库；修改输入文件；把关键词命中数直接当事件数。

CLI
---
    python3 tools/error_signal.py --log logs/autonomous_reflect.log [--window-lines 2000]
    python3 tools/error_signal.py --log ... --json [--out FILE] [--table N]
    python3 tools/error_signal.py --selftest            # 13 组含负控
    python3 tools/error_signal.py --predicate-probe     # /usr/bin/log 能力边界（不解析本日志）

退出码：0 正常 / 1 事件数超阈值（--degrade-threshold） / 2 输入不可用或用法错误
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

__all__ = ["LEGACY_TERMS", "analyze_lines", "analyze_file", "legacy_keyword_hits"]

# ---------------------------------------------------------------- 口径常量

# 旧口径词表（与 temp/efficiency_monitor.py:63 逐字一致，A/B 必须逐字复算）
LEGACY_TERMS = ("error", "failed", "traceback", "exception", "no module named", "failed:")

# 已知良性噪音（命中即计入 benign，不计入 health）
BENIGN_PATTERNS = (
    r"numpy\.core\.multiarray failed to import",
    r"A module that was compiled using NumPy\s*1\.x cannot be run in NumPy\s*2",
    r"NumPy\s*2\.",
    r"\bpyarrow\b.{0,60}?\b(incompat|failed|error)",
    r"\bLLMLingua\b",
    r"\bllmlingua\b",
    r"\bsklearn\b.{0,60}?\b(incompat|failed|error)",
)
_BENIGN_RE = re.compile("|".join(BENIGN_PATTERNS), re.IGNORECASE)

# 事件签名归一化（用于去重：同一问题的重复出现只算 1 个 distinct）
# 只抹掉**易变部分**（时间戳/地址/行号/十六进制 id），不合并不同异常类型，
# 否则会把真不同的故障并成一个（负控 NC15）。
_SIG_RULES = (
    # 注意：_signature 先做 lower()，故 T/Z 必须在 re.I 下匹配，否则时间戳留在签名里
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][0-9:.,]+Z?", re.I), "<ts>"),  # 时间戳
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r'File "[^"]*", line \d+'), "File <path>, line <N>"),
    (re.compile(r"line \d+"), "line <N>"),
    (re.compile(r":\d+:\d+"), ":<N>:<N>"),
    (re.compile(r"\bpid[ =]\d+", re.I), "pid <N>"),
    (re.compile(r"\s+"), " "),
)


def _signature(kind: str, text: str) -> str:
    s = (text or "").strip().lower()
    for rx, rep in _SIG_RULES:
        s = rx.sub(rep, s)
    return f"{kind}|{s[:200]}"


# --- 行首锚点 -----------------------------------------------------------------
# 2026-09-28（R466）扩展：子进程异常常经包装器打印成带前缀形态，实测 logs/*.log 里
# `STDERR Traceback (...)` 9 行、`STDERR: ` 10 行、`ERR ` 5 行、`ERR: ` 3 行；
# 只认行首 `^Traceback` 会让这些整块漏检（R466 覆盖外行核查实测 4 个未覆盖块，
# 全部为带前缀的真实异常块）。
# 跨日志补测（R466 二次扩展）：形态另含小写 `err: `／`err ` 与 `ERROR: `，故前缀
# 白名单改为**仅前缀段**大小写不敏感（用 (?i:…) 内联组，不影响主句大小写语义）。
# 回声/清单式前缀（`    | `/`  5 True `/`231:`/`1. \``）仍不在白名单内；
# 非白名单前缀（如 `WARN `/`apsw: `）同样不计——见 NC21/NC22 负控。
# 跨 11 份 logs/*.log 实测（R466，见 temp/error_signal/ab/prefix_coverage_crosslog.json）：
# 白名单前缀共 36 处（autonomous_reflect.log 4 处，已 100% 覆盖）；未匹配 145 处全部在
# 其它日志，分两类且均属**已声明边界**：①数字/无空格回声（`1060074: Traceback`、`304:Traceback`）
# 6 处（feishu_agent.log，NC19 同族）；②模块名前缀（`pysqlcipher3:  `/`dbapi2:  `/`apsw:  `）
# 139 处（scheduler_reflect.log）。被本监视器扫描的 autonomous_reflect.log 未匹配 = 0。
TRACEBACK_START = re.compile(
    r"^(?:(?i:STDERR|STDOUT|ERR|ERROR)[: ]+)?Traceback \(most recent call last\):"
)
EXC_TERM = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Warning|StopIteration)\b"
)
ISO_LEVEL = re.compile(
    r"^\d{4}-\d{2}-\d{2}T[0-9:.]{6,}Z?\s+(?:ERROR|WARN|WARNING|FATAL|CRITICAL)\b"
)
BARE_LEVEL = re.compile(r"^(?:ERROR|FATAL|CRITICAL)\b")
BRACKET_ERR = re.compile(r"^\[(?:ERROR|FATAL|ERR|FAILED|FAIL)\b")
BANG_ERR = re.compile(r"^!!!\s*Error:")
RC_NONZERO = re.compile(
    r"^(?:rc|exit_code|exit_code_|returncode)\s*[=:]\s*([1-9]\d*)\b"
    r"|^exit (?:code|status)\s*[=:]?\s*([1-9]\d*)\b"
    r"|^returned non-zero exit status\s*([1-9]\d*)\b",
    re.IGNORECASE,
)
TIMEOUT_ANCHOR = re.compile(r"^(?:subprocess\.)?TimeoutExpired\b|^Command '.*' timed out\b")

# 行内（非行首）出现这些词 = 低置信，通常是回声
_RC_ANYWHERE = re.compile(r"(?i)\b(?:rc|exit_code|exit code|return code)\b\s*[=:]?\s*([1-9]\d*)\b")

MAX_TRACEBACK_BLOCK = 80


def legacy_keyword_hits(lines) -> int:
    """旧口径：尾部窗口内包含任一 LEGACY_TERMS 的行数（casefold）。"""
    return sum(1 for line in lines if any(t in line.lower() for t in LEGACY_TERMS))


def _is_benign(text: str) -> bool:
    return bool(_BENIGN_RE.search(text))


def analyze_lines(lines, log_name: str = "<memory>", window_lines: int | None = None):
    """分析给定行序列（**窗口外的不看**），返回结构化事件报告 dict。

    lines: 已切分的行（不含换行符）
    window_lines: 只取最后 N 行（None = 全部）
    """
    total_lines = len(lines)
    window = lines if window_lines is None else lines[-window_lines:]
    window = list(window)

    events: list[dict] = []
    possible: list[dict] = []
    benign: list[dict] = []

    def add_event(kind, lineno, text, benign_text=None, **extra):
        rec = {"kind": kind, "line": lineno, "text": text[:300]}
        rec.update(extra)
        rec["sig"] = _signature(kind, rec.get("exc") or text)
        # 噪音判定须看**整块**：真实 LLMLingua/NumPy 噪音的 traceback 的
        # 末行只是 `ImportError: `，噪音特征全在栈帧里（实测 dingtalk_agent.log
        # 每轮 llmlingua warmup 一条；只看末行会导致 90 条噪音全判成真事件）。
        probe = text if benign_text is None else f"{text}\n{benign_text}"
        if _is_benign(probe):
            rec["benign_reason"] = "known_noise"
            benign.append(rec)
        else:
            events.append(rec)

    i = 0
    n = len(window)
    while i < n:
        line = window[i]
        lineno = total_lines - n + i + 1  # 在原始文件中的 1-based 行号

        if TRACEBACK_START.match(line):
            exc, term_idx = None, None
            limit = min(n, i + 1 + MAX_TRACEBACK_BLOCK)
            for j in range(i + 1, limit):
                if EXC_TERM.match(window[j]):
                    exc, term_idx = window[j].strip(), j
                    break
            block_end = term_idx if term_idx is not None else limit - 1
            block_text = "\n".join(window[i:block_end + 1])
            add_event(
                "traceback", lineno, exc or line,
                benign_text=block_text,
                exc=exc, truncated=term_idx is None, block_lines=block_end - i + 1,
                block=block_text[:600],
            )
            i = block_end + 1
            continue

        m = None
        kind = None
        for kind, rx in (
            ("logger_error", ISO_LEVEL),
            ("level_error", BARE_LEVEL),
            ("bracket_error", BRACKET_ERR),
            ("bang_error", BANG_ERR),
            ("rc_nonzero", RC_NONZERO),
            ("timeout", TIMEOUT_ANCHOR),
        ):
            m = rx.match(line)
            if m:
                break
        if m:
            add_event(kind, lineno, line, group=m.group(0)[:120])
            i += 1
            continue

        low = line.lower()
        if any(t in low for t in LEGACY_TERMS):
            possible.append({"kind": "keyword_hit", "line": lineno, "text": line[:300]})
        elif _RC_ANYWHERE.search(line):
            possible.append({"kind": "rc_marker_inline", "line": lineno, "text": line[:300]})
        i += 1

    by_kind: dict[str, int] = {}
    for e in events:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1

    # rc_nonzero 是**中置信**锚点：本日志会把脚本自身打印的摘要行（如
    # `rc=1  需登记合计=…`）也放在列 0，实测 10 处里 2 处属此类（精度约 80%）。
    # 因此单独计数：健康判定用 strong（不含 rc），rc 量单列可见。
    rc_count = by_kind.get("rc_nonzero", 0)

    # 去重口径：同一签名(=同一问题)重复出现多少次，都只算 1 个 distinct。
    # 实测 logs/ga_api_server.log 尾部 5000 行是同一句 ASGI 导入错误的重试刷屏
    # ⇒ event_count=5000 但 distinct=1，健康判定须用后者（否则单点故障刷屏即误报）。
    sig_all: dict[str, int] = {}
    for e in events:
        sig_all[e["sig"]] = sig_all.get(e["sig"], 0) + 1
    distinct_all = len(sig_all)
    sig_strong = {s: c for s, c in sig_all.items() if not s.startswith("rc_nonzero|")}
    distinct_strong = len(sig_strong)

    return {
        "log": log_name,
        "total_lines": total_lines,
        "window_lines": len(window),
        "window_span": [total_lines - n + 1, total_lines],
        "legacy_keyword_hits": legacy_keyword_hits(window),
        "event_count": len(events),
        "strong_event_count": len(events) - rc_count,
        "rc_event_count": rc_count,
        "events": events,
        "events_by_kind": dict(sorted(by_kind.items())),
        "distinct_event_count": distinct_all,
        "distinct_strong_event_count": distinct_strong,
        "events_by_signature": dict(sorted(sig_all.items(), key=lambda kv: (-kv[1], kv[0]))),
        "benign_count": len(benign),
        "benign": benign,
        "possible_count": len(possible),
        "possible": possible,
    }


def analyze_file(path, window_lines: int | None = 2000, log_name: str | None = None):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 文件尾换行不算一行
    rep = analyze_lines(lines, log_name or str(p), window_lines)
    rep["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    return rep


def verdict(rep, degrade_threshold: int, metric: str = "strong") -> str:
    """阈值判定。

    metric="strong"（默认）= 只用强锚点事件（traceback / 结构化日志行），
    排除 rc_nonzero —— 后者含脚本自印摘要的行首 `rc=1 …`（实测精度约 80%）。
    metric="events" = 含 rc 事件（与早期 A/B 口径一致，保守/更敏感）。
    """
    table = {
        "strong": rep.get("strong_event_count", rep["event_count"]),
        "events": rep["event_count"],
        "distinct_strong": rep.get("distinct_strong_event_count", rep.get("strong_event_count", rep["event_count"])),
        "distinct_events": rep.get("distinct_event_count", rep["event_count"]),
    }
    n = table.get(metric, table["strong"])
    return "DEGRADED" if n > degrade_threshold else "OK"


# ---------------------------------------------------------------- /usr/bin/log 边界

def predicate_probe(log_path: str) -> dict:
    """实测 /usr/bin/log 能否作为本日志的结构化检索替代（R466 收窄条件）。

    结论（2026-09-28 实测，见 report R466）：
      `log show --file <text.log>` ⇒ rc=64, stderr「File must have extension .tracev3
      or .atrc/.ktrace」⇒ **无法读取纯文本日志**；predicate 只作用于 unified log
      （另一数据源），因此不能替代本模块，只能作旁证。
    """
    out = {"log_binary": "/usr/bin/log"}
    try:
        r1 = subprocess.run(
            ["/usr/bin/log", "show", "--file", log_path, "--last", "1h", "--style", "compact"],
            capture_output=True, text=True, timeout=60,
        )
        out["file_mode"] = {
            "rc": r1.returncode,
            "stdout_head": r1.stdout[:200],
            "stderr_head": r1.stderr.strip()[:200],
        }
    except Exception as exc:  # pragma: no cover
        out["file_mode"] = {"error": repr(exc)}
    out["conclusion"] = (
        "PREDICATE_ROUTE_NOT_APPLICABLE_TO_TEXT_LOG"
        if out.get("file_mode", {}).get("rc") == 64
        else "UNEXPECTED_RESULT_CHECK_STDERR"
    )
    return out


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    fails: list[str] = []
    checks = 0

    def ck(cond, label):
        nonlocal checks
        checks += 1
        if not cond:
            fails.append(label)

    # --- 素材：模拟真实日志（含回声与真事件） --------------------------------
    sample = [
        "code run output:",
        "    if isinstance(c,str) and c.lstrip().startswith('!!!Error:'): err=c",  # 回声(缩进)
        "C6 【工程】efficiency_monitor 错误信号结构化替换（关键词计数→错误事件计数）",  # 正文回声
        "      消费者: 消费者,消费 / 弱:报告,接线 / 落点:tools/error_signal.py",     # 正文回声
        "Traceback (most recent call last):",
        '  File "x.py", line 5, in <module>',
        "    raise ValueError('x')",
        "ValueError: x",                                   # => 真事件 #1
        "2026-09-27T22:51:09.072692Z ERROR codex_memories_write::phase2: Phase 2 no changes",  # => #2
        "2026-09-27T22:53:09.264291Z ERROR rmcp::transport::streamable_http_client: fail to delete session",  # => #3
        "rc=1",                                            # => #4
        "exit code 2",                                     # => #5
        "  rc=3 in the middle of a report body mention exit_code=4",  # 行内 → possible
        "无错行 talk about failed login count = 0",         # 行内关键词 → possible
        "--- 负控：关键词塞进报告正文（缩进）不得计为事件 ---",
        "    Report body says: Traceback (most recent call last): happened earlier",  # 缩进 → possible
    ]
    r = analyze_lines(sample, "sample", None)
    ck(r["event_count"] == 5, f"NC0/正例事件数=5 实测 {r['event_count']}")
    ck(r["events_by_kind"].get("traceback") == 1, "NC4 traceback 整块只计 1")
    # possible=5：缩进的源码回声(!!!Error:)/带 error_signal 路径的清单行/行内 rc 提及/
    # 正文里的 failed 字样/缩进的 Traceback 提及（注意 efficiency_monitor 不含 error 子串）
    ck(r["possible_count"] == 5, f"NC1 缩进/行内关键词只进 possible 实测 {r['possible_count']}")
    ck(all(e["kind"] != "keyword_hit" for e in r["events"]), "NC1 回声不得进 events")
    ck(r["legacy_keyword_hits"] >= r["event_count"], "legacy 口径 >= 事件数（旧口径更宽）")

    # NC2 numpy ABI traceback = benign
    benign_sample = [
        "Traceback (most recent call last):",
        '  File "pyarrow/__init__.py", line 1, in <module>',
        "AttributeError: A module that was compiled using NumPy 1.x cannot be run in NumPy 2.0",
    ]
    r2 = analyze_lines(benign_sample, "b", None)
    ck(r2["event_count"] == 0, f"NC2 NumPy ABI 噪音不得计为事件 实测 {r2['event_count']}")
    ck(r2["benign_count"] == 1, "NC2 但须进 benign 可见")

    # NC3 LLMLingua warmup failed
    r3 = analyze_lines(["ERROR LLMLingua warmup failed (numpy ABI, exit=0)"], "b", None)
    ck(r3["event_count"] == 0 and r3["benign_count"] == 1, "NC3 LLMLingua 噪音")

    # NC5 截断 traceback 计 1 且标 truncated
    r5 = analyze_lines(["Traceback (most recent call last):"] + ["  File 'a'"] * 3, "b", None)
    ck(r5["event_count"] == 1 and r5["events"][0]["truncated"] is True, "NC5 截断 traceback")

    # NC6 rc=0 不算；行内 rc 不算事件
    r6 = analyze_lines(["rc=0", "exit_code=0", "日志里提到 rc=5 但这是正文"], "b", None)
    ck(r6["event_count"] == 0, f"NC6 rc=0 不得计事件 实测 {r6['event_count']}")
    ck(r6["possible_count"] == 1, "NC6 行内 rc 记 possible")

    # NC7 窗口裁剪生效
    long_lines = ["filler"] * 50 + ["ValueError: boom"]
    r7 = analyze_lines(long_lines, "b", 10)
    ck(r7["event_count"] == 0 and r7["window_lines"] == 10, "NC7 窗口外事件不计")

    # NC8 缺文件 rc=2（CLI 层）
    try:
        analyze_file("/nonexistent/definitely/missing.log")
        ck(False, "NC8 缺文件应抛 FileNotFoundError")
    except FileNotFoundError:
        ck(True, "NC8")

    # NC9 legacy 口径与旧实现逐字一致（独立复算）
    r9 = analyze_lines(sample, "s", None)
    expect9 = sum(1 for l in sample if any(t in l.lower() for t in LEGACY_TERMS))
    ck(r9["legacy_keyword_hits"] == expect9, f"NC9 legacy 逐字一致 {r9['legacy_keyword_hits']}!={expect9}")

    # NC10 幂等 + 只读（用真实日志副本）
    import tempfile
    real = Path("logs/autonomous_reflect.log")
    if real.exists():
        text = real.read_text(encoding="utf-8", errors="replace")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "copy.log"
            tmp.write_text(text, encoding="utf-8")
            before = hashlib.sha256(tmp.read_bytes()).hexdigest()
            a = analyze_file(tmp, 2000)
            b = analyze_file(tmp, 2000)
            after = hashlib.sha256(tmp.read_bytes()).hexdigest()
            ck(before == after, "NC10 只读：输入文件 sha256 不变")
            ck(json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), "NC10 幂等")
            ck(a["event_count"] >= 1, "NC10 真日志里至少有 1 个 traceback 事件")
            ck(a["legacy_keyword_hits"] > a["event_count"], "NC10 旧口径虚高（回声污染）实证")
    else:
        ck(True, "NC10 跳过（无真日志）")

    # NC11 verdict 阈值（strong 默认 / events 可选）
    rep = {"event_count": 3, "strong_event_count": 2, "rc_event_count": 1}
    ck(verdict(rep, 20) == "OK" and verdict(rep, 2) == "OK",
       "NC11a strong 口径：strong=2 不 > 阈值 2")
    ck(verdict(rep, 2, metric="events") == "DEGRADED",
       "NC11b events 口径：含 rc 的 event_count=3 > 2 ⇒ DEGRADED")
    ck(verdict(rep, 1) == "DEGRADED" and verdict(rep, 1, metric="events") == "DEGRADED",
       "NC11c 两口径在 1 处一致 DEGRADED")
    # NC11d 缺 strong 字段的旧报告不得崩（回退到 event_count）
    ck(verdict({"event_count": 5}, 2) == "DEGRADED", "NC11d 缺字段回退 event_count")

    # NC12 predicate 边界（只在有 /usr/bin/log 时跑，结论须与文档一致）
    if Path("/usr/bin/log").exists() and real.exists():
        pr = predicate_probe(str(real))
        ck(pr["conclusion"] == "PREDICATE_ROUTE_NOT_APPLICABLE_TO_TEXT_LOG",
           f"NC12 /usr/bin/log 不能读文本日志 实测 {pr}")

    # NC13 真实噪音形态（dingtalk_agent.log 实测）：traceback 末行只是
    # `ImportError: `，噪音特征在**栈帧**里 ⇒ 必须按整块判定为 benign
    ding = [
        "Traceback (most recent call last):",
        '  File "/opt/anaconda3/lib/python3.12/threading.py", line 1032, in _bootstrap',
        '  File "/Users/cuip/projects/GenericAgent/llmcore.py", line 48, in _llmlingua_warmup',
        "    from llmlingua import PromptCompressor",
        "ImportError: ",
    ]
    r13 = analyze_lines(ding, "ding", None)
    ck(r13["event_count"] == 0, f"NC13 LLMLingua 栈帧噪音不得计为事件 实测 {r13['event_count']}")
    ck(r13["benign_count"] == 1, "NC13 但须进 benign 可见")

    # NC14 刷屏去重：同一句错误重复 N 次 ⇒ event_count=N 但 distinct=1
    flood = ["ERROR:    Error loading ASGI app. Could not import module \"temp.ga_api_server\"."] * 5000
    r14 = analyze_lines(flood, "flood", None)
    ck(r14["event_count"] == 5000, f"NC14 出现次数照记 实测 {r14['event_count']}")
    ck(r14["distinct_event_count"] == 1, f"NC14 同一问题 distinct 必为 1 实测 {r14['distinct_event_count']}")
    ck(verdict(r14, 20, metric="distinct_strong") == "OK"
       and verdict(r14, 20, metric="strong") == "DEGRADED",
       "NC14b 单点故障刷屏：去重口径 OK / 次数口径 DEGRADED")

    # NC15 去重不得把不同异常合并
    two = [
        "Traceback (most recent call last):", "  File \"a.py\", line 1", "ValueError: a",
        "Traceback (most recent call last):", "  File \"b.py\", line 2", "KeyError: 'k'",
    ]
    r15 = analyze_lines(two, "two", None)
    ck(r15["event_count"] == 2 and r15["distinct_event_count"] == 2,
       f"NC15 两种异常 distinct=2 实测 {r15['distinct_event_count']}")

    # NC16 时间戳/地址归一化不得吃掉消息内容（同类型不同内容 = 2 个 distinct）
    iso = [
        "2026-09-27T22:51:09.072692Z ERROR a: first problem",
        "2026-09-28T01:02:03.000000Z ERROR a: second problem",
    ]
    r16 = analyze_lines(iso, "iso", None)
    ck(r16["distinct_event_count"] == 2, f"NC16 不同消息 distinct=2 实测 {r16['distinct_event_count']}")

    # NC17 同一问题跨时间戳重复 ⇒ distinct=1（归一化生效）
    iso2 = [
        "2026-09-27T22:51:09.072692Z ERROR a: same problem",
        "2026-09-28T01:02:03.000000Z ERROR a: same problem",
    ]
    r17 = analyze_lines(iso2, "iso2", None)
    ck(r17["event_count"] == 2 and r17["distinct_event_count"] == 1,
       f"NC17 同问题跨时间戳 distinct=1 实测 {r17['distinct_event_count']}")

    # NC18 带前缀的真实异常块必须检出（实测形态：子进程 stderr 经包装器落盘）
    pre = [
        "STDERR Traceback (most recent call last):",
        '  File "/Users/x/probe.py", line 12, in <module>',
        "    main()",
        "KeyError: '2026-03-01'",
        "",
        "ERR: Traceback (most recent call last):",
        '  File "/Users/x/y.py", line 3, in <module>',
        "ValueError: boom",
    ]
    r18 = analyze_lines(pre, "prefixed", None)
    ck(r18["event_count"] == 2,
       f"NC18 STDERR/ERR 前缀 traceback 须检出 2 个 实测 {r18['event_count']}")
    ck(r18["distinct_event_count"] == 2, "NC18b 两个不同异常 distinct=2")
    ck(all(e["kind"] == "traceback" for e in r18["events"]), "NC18c 均归类 traceback")

    # NC19 回声/清单式前缀不得被当成新块（否则报告正文被搬运即虚高）
    echo = [
        "231: Traceback (most recent call last):",
        "5 True Traceback (most recent call last):",
        "    | Traceback (most recent call last):",
        "1. `Traceback (most recent call last):`",
    ]
    r19 = analyze_lines(echo, "echo", None)
    ck(r19["event_count"] == 0,
       f"NC19 回声前缀不得计为事件 实测 {r19['event_count']}")

    # NC20 前缀块内的良性噪音仍走 benign（前缀扩展不得绕过噪音判定）
    noise = [
        "STDERR Traceback (most recent call last):",
        '  File "/opt/llmlingua.py", line 9, in <module>',
        "    import llmlingua  # llmlingua numpy ABI mismatch",
        "ImportError: cannot import name 'llmlingua'",
    ]
    r20 = analyze_lines(noise, "prefnoise", None)
    ck(r20["event_count"] == 0 and r20["benign_count"] == 1,
       f"NC20 前缀块内噪音仍降级 实测 events={r20['event_count']} benign={r20['benign_count']}")

    # NC21 前缀白名单大小写不敏感（实测形态：`err: `/`err `/`ERROR: ` 各见 logs/*.log）
    lower = [
        "err: Traceback (most recent call last):",
        '  File "/Users/x/a.py", line 2, in <module>',
        "ImportError: no module named z",
        "",
        "ERROR: Traceback (most recent call last):",
        '  File "/Users/x/b.py", line 7, in <module>',
        "TypeError: bad operand",
    ]
    r21 = analyze_lines(lower, "lowerprefix", None)
    ck(r21["event_count"] == 2,
       f"NC21 小写 err/ERROR 前缀须检出 2 个 实测 {r21['event_count']}")
    ck(r21["distinct_event_count"] == 2, "NC21b 两个不同异常 distinct=2")

    # NC22 非白名单前缀不得放行（否则任何"词 + Traceback"的散文/模块回声都算事件）
    nonwhite = [
        "WARN Traceback (most recent call last):",
        "apsw:  Traceback (most recent call last):",
        "note Traceback (most recent call last):",
    ]
    r22 = analyze_lines(nonwhite, "nonwhite", None)
    ck(r22["event_count"] == 0,
       f"NC22 非白名单前缀不得计为事件 实测 {r22['event_count']}")

    print(f"[error_signal --selftest] checks={checks} failed={len(fails)}")
    for f in fails:
        print("  FAIL:", f)
    return 1 if fails else 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="结构化错误事件提取器（关键词计数的替代）")
    ap.add_argument("--log", default="logs/autonomous_reflect.log")
    ap.add_argument("--window-lines", type=int, default=2000,
                    help="只看日志最后 N 行（旧口径同窗口；0/负数=全文件）")
    ap.add_argument("--degrade-threshold", type=int, default=20,
                    help="窗口内事件数 > 阈值 => verdict=DEGRADED（退出码 1）")
    ap.add_argument("--metric", choices=("strong", "events", "distinct_strong", "distinct_events"),
                    default="strong",
                    help="判定口径：strong=强锚点事件（默认，排除 rc 回声）；events=含 rc；"
                         "distinct_*=先去重（同一问题重复出现只算 1）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="把 JSON 报告写到文件")
    ap.add_argument("--table", type=int, default=0, help="打印前 N 条事件明细")
    ap.add_argument("--predicate-probe", action="store_true", help="/usr/bin/log 能力边界实测")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.predicate_probe:
        print(json.dumps(predicate_probe(args.log), ensure_ascii=False, indent=2))
        return 0

    try:
        rep = analyze_file(args.log, None if args.window_lines <= 0 else args.window_lines)
    except FileNotFoundError as exc:
        print(f"[error_signal] 日志不存在: {exc}", file=sys.stderr)
        return 2
    rep["verdict"] = verdict(rep, args.degrade_threshold, metric=args.metric)
    rep["degrade_threshold"] = args.degrade_threshold
    rep["metric"] = args.metric

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")

    if args.table:
        for e in rep["events"][:args.table]:
            print(f"  L{e['line']:>6} [{e['kind']}] {e['text'][:140]}")
        for b in rep["benign"][:args.table]:
            print(f"  L{b['line']:>6} [benign]  {b['text'][:140]}")

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"log={rep['log']}")
        print(f"window_lines={rep['window_lines']} span={rep['window_span']} "
              f"total_lines={rep['total_lines']}")
        print(f"legacy_keyword_hits={rep['legacy_keyword_hits']} (旧口径，含回声)")
        print(f"error_events={rep['event_count']} by_kind={rep['events_by_kind']}")
        print(f"strong_events={rep['strong_event_count']} rc_events={rep['rc_event_count']}")
        print(f"distinct_events={rep['distinct_event_count']} "
              f"distinct_strong={rep['distinct_strong_event_count']}")
        print(f"benign={rep['benign_count']} possible(回声/行内)={rep['possible_count']}")
        print(f"verdict={rep['verdict']} (metric={rep['metric']}, threshold={args.degrade_threshold})")
    return 1 if rep["verdict"] == "DEGRADED" else 0


if __name__ == "__main__":
    sys.exit(main())
