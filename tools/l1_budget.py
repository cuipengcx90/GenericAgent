#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""l1_budget.py —— L1「极简索引层」预算校验器（只读，零写入除 --selftest 的临时文件）

依据：memory/memory_management_sop.md（L0）
  - "L1: global_mem_insight.txt (极简索引层 - 严格控制 ≤30 行)"
  - "体积限制：≤ 30 行（硬约束），< 1k tokens（期望）。严禁填写细节（除非极高频任务）"
  - "同步红线：L1 只写关键词/名称，禁搬细节。括号内只写反直觉的场景触发词(2-4字)，
     禁写机制/方法/步骤。需要评估L1中的token数和索引效用。"

用法：
  python3 tools/l1_budget.py                        # 校验默认 L1
  python3 tools/l1_budget.py --file <path>          # 校验任意草稿（如 temp/l1_slim/global_mem_insight.v2.txt）
  python3 tools/l1_budget.py --file X --max-lines 30 --max-tokens 1000 --json
  python3 tools/l1_budget.py --selftest             # 负控自测：超限样例必 FAIL、正常样例必 PASS、缺文件必 rc2

退出码：0=PASS  1=FAIL(超预算)  2=用法/读取错误
口径（token）：tiktoken 编码计数，**同时打印 o200k_base / cl100k_base 两口径**（不同后端分词器不同，
  单值会误导）；tiktoken 缺失时回退"字符数估算"并在输出显式标注 estimation=True。
自证：输出 abspath / stat / sha1 / 首个非空行 / 非空行数 —— 用于证明"校验器真读到了目标文件"，
  避免"路径写错→读到空→报 PASS"这类假绿（历史已实测过同类事故）。
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile

DEFAULT_L1 = "memory/global_mem_insight.txt"
ENC_PRIMARY = "o200k_base"
ENC_SECONDARY = "cl100k_base"


def _load_tokenizers():
    """返回 (encodings: dict[str, fn], note). fn: str -> int"""
    encs, note = {}, ""
    try:
        import tiktoken  # type: ignore
        cache = {}

        def mk(name):
            def _f(text: str) -> int:
                if name not in cache:
                    cache[name] = tiktoken.get_encoding(name)
                return len(cache[name].encode(text))
            return _f
        for name in (ENC_PRIMARY, ENC_SECONDARY):
            try:
                encs[name] = mk(name)
            except Exception:
                pass
        if not encs:
            note = "tiktoken 已装但无可用编码，回退估算"
        else:
            note = "tiktoken"
    except ImportError:
        note = "tiktoken 未安装，回退字符数估算"
    if not encs:
        encs["est_chars_div2"] = lambda t: (len(t) + 1) // 2
    return encs, note


def measure(path: str, max_lines: int, max_tokens: int):
    """读文件并给出预算判定。返回 dict（含判据与自证字段）。"""
    rep = {
        "path": path,
        "abspath": os.path.abspath(path),
        "max_lines": max_lines,
        "max_tokens": max_tokens,
        "ok": False,
        "reasons": [],
        "read_ok": False,
        "estimated": False,
    }
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        rep["reasons"].append(f"[ERROR] 读取失败: {e}")
        rep["error"] = str(e)
        return rep
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    nonempty = [l for l in lines if l.strip()]
    rep.update({
        "read_ok": len(raw) > 0,
        "bytes": len(raw),
        "chars": len(text),
        "lines": len(lines),
        "nonempty_lines": len(nonempty),
        "sha1": hashlib.sha1(raw).hexdigest(),
        "first_line": (nonempty[0][:120] if nonempty else ""),
    })
    encs, note = _load_tokenizers()
    rep["tokenizer_note"] = note
    rep["tokens"] = {k: f(text) for k, f in encs.items()}
    rep["estimated"] = any(k.startswith("est_") for k in encs)
    primary = encs.get(ENC_PRIMARY) or next(iter(encs.values()))
    rep["tokens_primary"] = primary(text)

    if not rep["read_ok"]:
        rep["reasons"].append("[FAIL] 文件为空: 读不到内容（先证路径正确）")
        return rep
    if len(lines) > max_lines:
        rep["reasons"].append(f"[FAIL] 行数 {len(lines)} > 硬约束 {max_lines}")
    if rep["tokens_primary"] > max_tokens:
        rep["reasons"].append(
            f"[FAIL] token {rep['tokens_primary']} > 期望上限 {max_tokens}"
            f"（口径 {ENC_PRIMARY}）")
    rep["ok"] = not rep["reasons"]
    return rep


def fmt(rep) -> str:
    if not rep.get("read_ok"):
        return "\n".join([f"文件: {rep['path']}",
                          f"abspath: {rep.get('abspath')}"] + rep["reasons"])
    tok = " ".join(f"{k}={v}" for k, v in rep["tokens"].items())
    head = [
        f"文件: {rep['path']}",
        f"abspath: {rep['abspath']}",
        f"自证: bytes={rep['bytes']} chars={rep['chars']} lines={rep['lines']} "
        f"nonempty={rep['nonempty_lines']} sha1={rep['sha1'][:12]}",
        f"首行: {rep['first_line']}",
        f"token({rep['tokenizer_note']}): {tok}"
        + ("  [estimation=True 仅估算，勿当实测]" if rep["estimated"] else ""),
        f"预算: lines<={rep['max_lines']} tokens<={rep['max_tokens']}",
    ]
    if rep["ok"]:
        head.append(f"结论: PASS（行 {rep['lines']}/{rep['max_lines']}，"
                    f"tok {rep['tokens_primary']}/{rep['max_tokens']}）")
    else:
        head.append("结论: FAIL")
        head += rep["reasons"]
    return "\n".join(head)


def selftest(max_lines=30, max_tokens=1000):
    """负控自测：超限样例必 FAIL、合规样例必 PASS、缺失文件必报错。"""
    results = []
    tmpdir = tempfile.mkdtemp(prefix="l1_budget_selftest_")
    # 1) 超限样例（行数超 + 内容超）
    over = os.path.join(tmpdir, "over_limit.txt")
    with open(over, "w", encoding="utf-8") as f:
        for i in range(max_lines + 5):
            f.write(f"这是一个明显超限的示例行 {i} " + "细节内容" * 60 + "\n")
    r1 = measure(over, max_lines, max_tokens)
    results.append(("超限样例(行数+, 内容+)", r1, False))  # 期望 FAIL

    # 2) 合规样例
    ok = os.path.join(tmpdir, "within_budget.txt")
    with open(ok, "w", encoding="utf-8") as f:
        f.write("# [Global Memory Insight]\n")
        f.write("浏览器: tmwebdriver_sop | jshook_runtime_sop\n")
        f.write("L0: memory_management_sop\n")
    r2 = measure(ok, max_lines, max_tokens)
    results.append(("合规样例", r2, True))  # 期望 PASS

    # 3) 仅 token 超限（行数合规）——防止只看行数的假绿
    tok = os.path.join(tmpdir, "over_tokens.txt")
    with open(tok, "w", encoding="utf-8") as f:
        for i in range(10):
            f.write("长行 " + "密集细节内容" * 200 + "\n")
    r3 = measure(tok, max_lines, max_tokens)
    results.append(("仅token超限(行数合规)", r3, False))  # 期望 FAIL

    # 4) 缺失文件——必须报错而非静默 PASS
    r4 = measure(os.path.join(tmpdir, "no_such_file.txt"), max_lines, max_tokens)
    results.append(("缺失文件", r4, None))  # 期望 read_ok=False

    print("=== l1_budget --selftest（负控自测）===")
    allok = True
    for name, rep, expect in results:
        got = rep["ok"]
        if expect is None:
            good = (not rep.get("read_ok"))
        else:
            good = (got == expect)
        allok &= good
        print(f"[{'OK' if good else 'BAD'}] {name}: ok={got} "
              f"lines={rep.get('lines')} tok={rep.get('tokens_primary')} "
              f"reasons={len(rep['reasons'])}")
    print("selftest:", "PASS 4/4" if allok else "FAIL")
    print("临时目录(可删):", tmpdir)
    return 0 if allok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="L1 索引层预算校验器（≤30 行 / <1k tokens）")
    ap.add_argument("--file", default=DEFAULT_L1, help=f"待校验文件（默认 {DEFAULT_L1}）")
    ap.add_argument("--max-lines", type=int, default=30)
    ap.add_argument("--max-tokens", type=int, default=1000)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="负控自测（超限/合规/token超限/缺失）")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest(a.max_lines, a.max_tokens)

    if not os.path.exists(a.file):
        print(f"[ERROR] 文件不存在: {a.file} (abspath={os.path.abspath(a.file)})", file=sys.stderr)
        return 2
    rep = measure(a.file, a.max_lines, a.max_tokens)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(fmt(rep))
    if rep.get("error"):
        return 2
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
