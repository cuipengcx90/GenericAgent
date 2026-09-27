#!/usr/bin/env python3
"""verify_quotes_repo.py — 引文回源校验器（**仓库快照语料**变体）

用途：审计/研读报告中用双引号标注的"逐字原文"，回源到指定代码仓库/文档树里逐字校验。
与 herdr-agents:docs/research/loopx/verify_quotes.py 的分工：那个 walk temp+memory+docs，
本件按 `--repo` 指定任意语料根（如本机仓库快照），专治"报告对象是外部仓库"的场景。

用法：
    python3 verify_quotes_repo.py <doc.md> --repo /path/to/corpus_root [--max-mb 8]

判定口径（与 RULES#18 一致）：
  - 只对**英文**引文做逐字契约（中文双引号内按意译，不校验）
  - 归一：NFKC + 去全部空白 + 小写 + 剥 ** ` \\ 与全角引号
  - 语法：引号按全文顺序 pos[k]~pos[k+1] 就近配对（避免行内多引文乱配）
  - 语料：单文件上限可调（默认 8MB），**上限过小会静默跳过**大文件（实测 400KB 上限
    漏掉 462KB 的 daemon.go ⇒ 真引文全判 MISS）；本器会打印语料文件数与最大文件字节数自证
退出码：0=无 MISS，1=有 MISS（便于 CI/闸门）
"""
import os, re, sys, unicodedata, argparse

EXTS = {".go", ".md", ".sql", ".json", ".ts", ".tsx", ".yaml", ".yml", ".txt",
        ".sh", ".js", ".css", ".mod", ".env", ".proto", ".toml", ".py", ".rs", ".java",
        ".example", ".jsonc", ".html", ".c", ".h", ".cpp"}
STRIP = ["**", "`", "\\", '"', "'", "\u201c", "\u201d", "\u2018", "\u2019", "\u300c", "\u300d"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s).lower()
    for t in STRIP:
        if t in ("\u201c", "\u201d", "\u2018", "\u2019"):
            continue
        # `**` 不能删空：去空白会把源码注释的「 * 」续行前缀粘成 `**`
        # （` *\n * text` → `**text`），删空即吃掉前缀 ⇒ 逐字正确的引文被误报 MISS。
        # 统一折叠为单个 `*`（两侧同规则，粗体与非粗体仍等价）。
        s = s.replace(t, "*") if t == "**" else s.replace(t, "")
    return s


def build_corpus(repo: str, max_bytes: int):
    parts, nfiles, biggest = [], 0, 0
    for r, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "vendor", "dist", ".next"}]
        for f in files:
            if os.path.splitext(f)[1] not in EXTS:
                continue
            p = os.path.join(r, f)
            try:
                if os.path.getsize(p) > max_bytes:
                    print(f"[skip >{max_bytes}B] {p}", file=sys.stderr)
                    continue
                parts.append(open(p, encoding="utf-8", errors="ignore").read())
                nfiles += 1
                biggest = max(biggest, os.path.getsize(p))
            except Exception as e:  # noqa
                print(f"[unreadable] {p}: {e}", file=sys.stderr)
    print(f"[corpus] files={nfiles} max_file={biggest}B root={repo}", file=sys.stderr)
    return norm("\n".join(parts))


def check(doc: str, corpus: str):
    txt = open(doc, encoding="utf-8").read()
    head = re.search(r"^## ", txt, re.M)
    body = txt[head.start():] if head else txt          # 跳过文首"引文范围声明"区
    pos = [m.start() for m in re.finditer('"', body)]
    ok, miss = 0, []
    for a, b in zip(pos[0::2], pos[1::2]):
        q = body[a + 1:b]
        if len(q) < 12 or re.search(r"[\u4e00-\u9fff]", q):   # 中文按意译，不校验
            continue
        if len(q.split()) < 4:                                # 代码引用/短标识符，非散文引文
            continue
        n = norm(q)
        if len(n) < 10:
            continue
        if n in corpus:
            ok += 1
        else:
            miss.append((body[:a].count("\n") + (txt[:head.start()].count("\n") if head else 0) + 1, q[:90]))
    return ok, miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--max-mb", type=float, default=8.0)
    a = ap.parse_args()
    corpus = build_corpus(a.repo, int(a.max_mb * 1024 * 1024))
    ok, miss = check(a.doc, corpus)
    print(f"doc={a.doc} quotes_ok={ok} miss={len(miss)}")
    for ln, q in miss:
        print(f"  MISS L{ln} | {q}")
    sys.exit(1 if miss else 0)


if __name__ == "__main__":
    main()
