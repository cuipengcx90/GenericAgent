#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绩效取数（源① git）：跨仓 + 跨 worktree 的本人提交统计。

口径要点（与绩效 SOP 一致，禁写死季度，窗口由 quarter_window 现算）：
  * 根 = **父工程树**（默认 ~/develop/WorkSpaces/idea/smyze），递归找 `.git`（目录=普通仓，
    文件=worktree/submodule 的 gitfile），`--depth` 默认为 8。
  * 去重 = 按 `git rev-parse --git-common-dir` 归并：同一仓的多个 worktree/子仓副本只算一次，
    否则分支头会在多个 worktree 里被重复统计（旧口径「只扫父目录顶层」会漏 worktree 分支头）。
  * 计数 = 单个 `git log` 调用同时产出「提交数 + 行数 + 文件数 + 月度分布」，作者/时间过滤走
    git 自身（`--author` / `--since` / `--until`，一律带 +08:00，半开区间）。
  * 范围模式 `--mode`：head(仅 HEAD) / branches(本地全部分支，默认) / all(含远端跟踪分支)。

CLI:
    python3 tools/git_contrib_stats.py --quarter Q3-2026
    python3 tools/git_contrib_stats.py --since 2026-07-01 --until 2026-10-01 --out /tmp/x.json
    python3 tools/git_contrib_stats.py --mode all --top 20
    python3 tools/git_contrib_stats.py --selftest

只读（不写仓库、不 fetch、不切分支）；纯标准库。
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import quarter_window as qw  # 单一源：窗口计算
except Exception:  # pragma: no cover - 兜底，保持工具可独跑
    qw = None

DEFAULT_ROOT = os.path.expanduser("~/develop/WorkSpaces/idea/smyze")
DEFAULT_AUTHOR = "崔朋"
MODES = ("head", "branches", "all")
PRUNE_DIRS = {
    "node_modules", ".venv", "venv", ".tox", ".gradle", ".idea", ".cache",
    ".pnpm-store", "Pods", ".dart_tool", "__pycache__", "dist", "build",
    ".next", "target", ".mypy_cache", ".pytest_cache",
}
RS = "\x1e"
US = "\x1f"


# ---------------------------------------------------------------- 基础设施
def _run(args, cwd=None, timeout=300):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", "EXC:%s" % e
    return p.returncode, p.stdout, p.stderr


def discover_git_dirs(root, depth=8, prune=True):
    """返回 [(worktree_path, gitdir_or_gitfile)]；.git 目录/文件都识别。"""
    out = []
    root = os.path.abspath(os.path.expanduser(root))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        rel = os.path.relpath(dirpath, root)
        d = 0 if rel == "." else rel.count(os.sep) + 1
        if d >= depth:
            dirnames[:] = []
        if prune:
            dirnames[:] = [x for x in dirnames if x not in PRUNE_DIRS]
        if ".git" in dirnames:
            out.append((dirpath, os.path.join(dirpath, ".git")))
            dirnames.remove(".git")
        elif ".git" in filenames:
            out.append((dirpath, os.path.join(dirpath, ".git")))
    return sorted(out)


def repo_key(worktree, gitdir):
    """按 git-common-dir 归一（worktree 与主仓同键）；返回 (key, is_worktree_root)"""
    rc, so, _ = _run(["git", "-C", worktree, "rev-parse", "--git-common-dir"], timeout=60)
    if rc == 0 and so.strip():
        g = so.strip()
        if not os.path.isabs(g):
            g = os.path.join(worktree, g)
        return os.path.realpath(g), os.path.isdir(gitdir)
    if os.path.isfile(gitdir):
        try:
            txt = open(gitdir, encoding="utf-8", errors="replace").read().strip()
            if txt.startswith("gitdir:"):
                return os.path.realpath(os.path.join(worktree, txt.split(":", 1)[1].strip())), False
        except OSError:
            pass
    return os.path.realpath(gitdir), False


def select_representatives(entries):
    """同一 common-dir 只留一个代表；优先「主仓根」（.git 是目录），否则字典序首个。"""
    groups = collections.OrderedDict()
    for wt, gd in entries:
        key, is_main = repo_key(wt, gd)
        groups.setdefault(key, []).append((wt, is_main))
    picked, merged = [], 0
    for key, items in groups.items():
        mains = [w for w, m in items if m]
        chosen = sorted(mains)[0] if mains else sorted(w for w, _ in items)[0]
        merged += len(items) - 1
        picked.append({"path": chosen, "key": key, "aliases": sorted(w for w, _ in items)})
    return picked, merged


_GIT_VER = None


# ---------------------------------------------------------------- 统计内核
def parse_log(out, month_key=True):
    """解析 `git log --pretty=format:%x1e%H%x1f%ad%x1f%an --numstat` 输出。"""
    commits = []
    for rec in out.split(RS):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        lines = rec.split("\n")
        head = lines[0].split(US)
        if len(head) >= 4:
            sha, date, date_c, author = (head[0].strip(), head[1].strip(),
                                         head[2].strip(), head[3].strip())
        elif len(head) >= 3:
            sha, date, author = head[0].strip(), head[1].strip(), head[2].strip()
            date_c = ""
        else:
            continue
        ins = dels = 0
        files = set()
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            a, b, path = parts
            if a.isdigit():
                ins += int(a)
            if b.isdigit():
                dels += int(b)
            if path:
                files.add(path)
        commits.append({
            "sha": sha, "date": date, "author": author,
            "insertions": ins, "deletions": dels,
            "files": len(files), "month": date[:7] if month_key else "",
            "month_committer": (date_c[:7] if month_key else ""),
        })
    return commits


def _git_version():
    """返回 (major, minor) 供能力判定；取不到时返回 (0, 0)。"""
    global _GIT_VER
    if _GIT_VER is None:
        try:
            out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"(\d+)\.(\d+)", out or "")
            _GIT_VER = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        except Exception:
            _GIT_VER = (0, 0)
    return _GIT_VER


def since_arg(since):
    """时间窗下界参数。

    ★实测坑（git 2.39.5 / Apple Git-154）：`--since` 是**遍历截断**语义——
    当历史里出现「父提交日期比子提交新」（rebase / cherry-pick / 导入 / 改日期）时，
    遍历到旧提交即停止，会把更早遍历到的新提交一并丢掉（极端情况整段结果为空，
    且 exit code 仍为 0 = 静默漏数）。`--since-as-filter`(git>=2.36) 是纯过滤、不截断，
    故优先使用；旧版 git 回退 `--since`（collect() 会记 warning）。
    env GITSTATS_SINCE_MODE=filter|cut 可强制。
    """
    mode = os.environ.get("GITSTATS_SINCE_MODE", "").strip().lower()
    if mode == "cut":
        return "--since=%s" % since, "cut"
    if mode == "filter" or _git_version() >= (2, 36):
        return "--since-as-filter=%s" % since, "filter"
    return "--since=%s" % since, "cut"


def log_args(mode, since, until, author, bin0=None):
    a = ["git", "log", "--numstat", "--no-merges",
         "--pretty=format:%s%%H%s%%ad%s%%cd%s%%an" % (RS, US, US, US),
         "--date=format-local:%Y-%m"]
    a.append(since_arg(since)[0])
    a.append("--until=%s" % until)
    if author:
        a.append("--author=%s" % author)
    if mode == "head":
        a.append("HEAD")
    elif mode == "branches":
        a.append("--branches")
    elif mode == "all":
        a.append("--all")
    return a


def collect(root=DEFAULT_ROOT, author=DEFAULT_AUTHOR, since=None, until=None, mode="branches",
            depth=8, prune=True, top=15, timeout=600, include=(), exclude=()):
    if mode not in MODES:
        raise ValueError("mode 必须是 %s" % (MODES,))
    found = discover_git_dirs(root, depth=depth, prune=prune)
    picked, merged = select_representatives(found)
    if include:
        picked = [p for p in picked if any(t in p["path"] for t in include)]
    if exclude:
        picked = [p for p in picked if not any(t in p["path"] for t in exclude)]

    commits, errors = [], []
    per_repo = collections.OrderedDict()
    for p in picked:
        args = log_args(mode, since, until, author)
        rc, out, err = _run(args, cwd=p["path"], timeout=timeout)
        if rc != 0:
            errors.append({"path": p["path"], "rc": rc, "err": (err or "")[-200:]})
            per_repo[p["path"]] = 0
            continue
        cs = parse_log(out)
        per_repo[p["path"]] = len(cs)
        for c in cs:
            c["repo"] = os.path.basename(p["path"]) or p["path"]
            c["repo_path"] = p["path"]
        commits.extend(cs)

    # ★去重口径（2026-10-09 新增）：同一 sha 会被多个仓/worktree 副本各计一次
    # （实测 branches 模式 raw 2173 中有 253 条跨仓重复 sha）。raw=逐仓求和（受仓集合影响）；
    # unique=按 sha 去重后的真实提交数 ⇒ 对外汇报/绩效口径一律以 unique 为准。
    commits_u, dups_removed, _seen = [], 0, set()
    for c in commits:
        _sha = c["sha"]
        if _sha in _seen:
            dups_removed += 1
            continue
        _seen.add(_sha)
        commits_u.append(c)
    per_repo_u = collections.OrderedDict()
    for c in commits_u:
        per_repo_u[c["repo_path"]] = per_repo_u.get(c["repo_path"], 0) + 1

    by_month = collections.OrderedDict()
    for c in sorted(commits, key=lambda x: x["date"]):
        m = c["month"] or "unknown"
        b = by_month.setdefault(m, {"commits": 0, "insertions": 0, "deletions": 0})
        b["commits"] += 1
        b["insertions"] += c["insertions"]
        b["deletions"] += c["deletions"]

    by_month_u = collections.OrderedDict()
    for c in sorted(commits_u, key=lambda x: x["date"]):
        _m = c["month"] or "unknown"
        _b = by_month_u.setdefault(_m, {"commits": 0, "insertions": 0, "deletions": 0})
        _b["commits"] += 1
        _b["insertions"] += c["insertions"]
        _b["deletions"] += c["deletions"]

    by_month_committer = collections.OrderedDict()
    for c in sorted(commits, key=lambda x: x.get("month_committer") or ""):
        m = c.get("month_committer") or "unknown"
        b = by_month_committer.setdefault(m, {"commits": 0, "insertions": 0, "deletions": 0})
        b["commits"] += 1
        b["insertions"] += c["insertions"]
        b["deletions"] += c["deletions"]

    # ★口径透明化：git 的 --since/--until（含 --since-as-filter）按 **committer 日期** 过滤，
    # 而「月分布」传统按 **author 日期**（=提交实际写就时间）。rebase/cherry-pick 会让两者分属
    # 不同月份（实测 1 例 a4d29df: author 2026-06 / committer 2026-09）⇒ 两套分布都给出，
    # 并把 author 日期落在窗口外的条数显式标出（不改动 commits 计数，保持与 git 过滤一致）。
    def _d(s):
        return (s or "").replace("T", " ")[:19]
    out_of_window = [c["sha"][:12] for c in commits_u
                     if since and until and not (since[:19] <= _d(c.get("date")) < until[:19])]
    tot_ins = sum(c["insertions"] for c in commits)
    tot_del = sum(c["deletions"] for c in commits)
    top_repos = sorted(
        ({"repo": k, "commits": v} for k, v in per_repo.items() if v),
        key=lambda x: -x["commits"])[:top]
    top_repos_u = sorted(
        ({"repo": k, "commits": v} for k, v in per_repo_u.items() if v),
        key=lambda x: -x["commits"])[:top]
    return {
        "schema": "perf.git_contrib/v1",
        "root": os.path.abspath(os.path.expanduser(root)),
        "author": author, "since": since, "until": until, "mode": mode,
        "repos_gitdirs_found": len(found),
        "repos_after_common_dir_dedup": len(picked),
        "repos_merged_by_dedup": merged,
        "repos_with_commits": sum(1 for v in per_repo.values() if v),
        "commits": len(commits),
        "commits_unique": len(commits_u),
        "dups_removed": dups_removed,
        "repos_with_commits_unique": sum(1 for v in per_repo_u.values() if v),
        "insertions_unique": sum(c["insertions"] for c in commits_u),
        "deletions_unique": sum(c["deletions"] for c in commits_u),
        "insertions": tot_ins, "deletions": tot_del, "lines_total": tot_ins + tot_del,
        "by_month": by_month,
        "by_month_unique": by_month_u,
        "by_month_committer": by_month_committer,
        "out_of_window_author": {
            "count": len(out_of_window),
            "note": "author 日期落在窗口外(committer 日期在窗口内, 常见于 rebase/cherry-pick); 仍计入 commits",
            "sha12": out_of_window[:10],
        },
        "top_repos": top_repos,
        "top_repos_unique": top_repos_u,
        "errors": errors,
        "repos_picked": [x["path"] for x in picked],
        "commits_detail": commits,
    }


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="git 贡献统计（只读）")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--author", default=DEFAULT_AUTHOR)
    ap.add_argument("--quarter")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--mode", default="branches", choices=MODES)
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--include", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--no-detail", action="store_true", help="不输出逐提交明细（省体积）")
    ap.add_argument("--dump-repos", dest="dump_repos", help="写出选中的仓清单(路径)JSON，用于与独立通路对账")
    ap.add_argument("--out", help="JSON 落盘路径（旧名，等价 --json-out）")
    ap.add_argument("--json-out", dest="json_out", help="JSON 落盘路径（与其余绩效工具同名）")
    ap.add_argument("--md-out", dest="md_out", help="Markdown 摘要落盘路径")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    w = _window(args)
    if not w:
        return 2
    res = collect(args.root, args.author, w["since"], w["until"], args.mode,
                  args.depth, not args.no_prune, args.top, args.timeout,
                  args.include, args.exclude)
    res["window"] = {"label": w.get("label"), "since": w["since"], "until": w["until"]}
    if args.dump_repos:
        import os as _os2
        _d2 = _os2.path.dirname(_os2.path.abspath(args.dump_repos))
        if _d2:
            _os2.makedirs(_d2, exist_ok=True)
        with open(args.dump_repos, "w", encoding="utf-8") as _fh2:
            json.dump({"root": res["root"], "author": res["author"], "mode": res["mode"],
                       "repos_gitdirs_found": res["repos_gitdirs_found"],
                       "repos_after_common_dir_dedup": res["repos_after_common_dir_dedup"],
                       "repos": res.get("repos_picked") or []},
                      _fh2, ensure_ascii=False, indent=1)
    if args.no_detail:
        res.pop("commits_detail", None)
    txt = json.dumps(res, ensure_ascii=False, indent=1, default=str)
    jout = args.json_out or args.out
    if jout:
        _write(jout, txt)
        print("wrote %s (%d bytes)" % (jout, len(txt.encode("utf-8"))))
    else:
        print(txt)
    if args.md_out:
        md = render_md(res)
        _write(args.md_out, md)
        print("wrote %s (%d bytes)" % (args.md_out, len(md.encode("utf-8"))))
    return 0 if not res["errors"] else 0


def _write(path, text):
    import os as _os
    d = _os.path.dirname(_os.path.abspath(path))
    if d and not _os.path.isdir(d):
        _os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def render_md(res):
    """紧凑 Markdown 摘要（数值全部取自 res，不另算）。"""
    w = res.get("window") or {}
    L = []
    L.append("# git 贡献统计（只读）")
    L.append("")
    L.append("- 窗口: %s  %s → %s" % (w.get("label"), w.get("since"), w.get("until")))
    L.append("- 作者: %s ; 模式: %s ; 根: %s" % (res.get("author"), res.get("mode"), res.get("root")))
    L.append("- 仓库: 去重后 %s（合并 %s）, 有提交 %s" % (
        res.get("repos_after_common_dir_dedup"), res.get("repos_merged_by_dedup"),
        res.get("repos_with_commits")))
    L.append("- 提交(去重后/唯一 sha): **%s** 次" % res.get("commits_unique"))
    L.append("- 提交(逐仓求和/含跨仓重复): %s 次（剔除重复 %s）" % (
        res.get("commits"), res.get("dups_removed")))
    L.append("- 新增/删除(去重口径): %s / %s 行（合计 %s）" % (
        res.get("insertions_unique"), res.get("deletions_unique"),
        (res.get("insertions_unique") or 0) + (res.get("deletions_unique") or 0)))
    bm = res.get("by_month") or {}
    if bm:
        L.append("- 月分布(author 日期): " + ", ".join("%s=%s" % (k, v) for k, v in sorted(bm.items())))
    bmu = res.get("by_month_unique") or {}
    if bmu:
        L.append("- 月分布(去重后, author 日期): " + ", ".join("%s=%s" % (k, v.get("commits"))
                                                          for k, v in sorted(bmu.items())))
    bmc = res.get("by_month_committer") or {}
    if bmc:
        L.append("- 月分布(committer 日期): " + ", ".join("%s=%s" % (k, v.get("commits"))
                                                          for k, v in sorted(bmc.items())))
    oow = res.get("out_of_window_author") or {}
    if oow.get("count"):
        L.append("- ★author 日期落窗口外 %s 条（committer 日期在窗内；rebase/cherry-pick 所致，仍计数）: %s"
                 % (oow.get("count"), ", ".join(oow.get("sha12") or [])))
    tr = res.get("top_repos") or []
    if tr:
        L.append("")
        L.append("## Top 仓库")
        for i, x in enumerate(tr[:15], 1):
            if isinstance(x, dict):
                L.append("%d. %s: %s 次" % (i, x.get("repo") or x.get("name"), x.get("commits")))
            else:
                L.append("%d. %s" % (i, x))
    errs = res.get("errors") or []
    if errs:
        L.append("")
        L.append("## 错误 %d 条" % len(errs))
        for e in errs[:10]:
            L.append("- %s" % e)
    return "\n".join(L) + "\n"


def _window(args):
    if qw is None:
        print("ERROR 缺少 quarter_window.py（同目录）", file=sys.stderr)
        return None
    try:
        return qw.resolve(args.since, args.until, args.quarter)
    except ValueError as e:
        print("ERROR %s" % e, file=sys.stderr)
        return None


# ---------------------------------------------------------------- 自测（真 git 夹具）
def _sh(args, cwd=None, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=e, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("cmd failed %s: %s" % (args, p.stderr[-300:]))
    return p.stdout


def _commit(repo, name, date, fname, body=3, cdate=None):
    """cdate 给定时可让 author/committer 日期分属不同月（模拟 rebase/cherry-pick）。"""
    open(os.path.join(repo, fname), "w").write("x\n" * body)
    _sh(["git", "add", "-A"], cwd=repo)
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": "%s@t" % name,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": "%s@t" % name,
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": cdate or date}
    _sh(["git", "commit", "-m", "c-%s" % fname], cwd=repo, env=env)


def selftest():
    checks = []

    def ck(n, c):
        checks.append((n, bool(c)))

    tmp = tempfile.mkdtemp(prefix="gitstats_selftest_")
    repoA = os.path.join(tmp, "repoA")
    repoB = os.path.join(tmp, "repoB")
    os.makedirs(repoA)
    _sh(["git", "init", "-q", "-b", "main"], cwd=repoA)
    _sh(["git", "config", "user.email", "t@t"], cwd=repoA)
    _sh(["git", "config", "user.name", "t"], cwd=repoA)
    IN = "2026-08-10T10:00:00+08:00"
    OUT = "2026-05-10T10:00:00+08:00"
    ME = "崔朋"
    # ★提交顺序须按日期**升序**（git 的 --since 是遍历截断语义，非单调日期会漏算；
    #   倒挂抗性另有专门用例）
    _commit(repoA, ME, OUT, "d.txt")          # 时间窗负控（祖先，日期最早）
    _commit(repoA, ME, IN, "a.txt")           # 命中
    _commit(repoA, ME, IN, "b.txt", body=5)   # 命中
    _commit(repoA, "别人", IN, "c.txt")        # 作者过滤负控
    # worktree（同 common-dir，分支头只在 worktree 里）
    wt = os.path.join(tmp, "repoA_wt")
    _sh(["git", "worktree", "add", "-q", "-b", "feat/x", wt], cwd=repoA)
    _commit(wt, ME, IN, "e.txt")              # 仅分支模式可见
    # 另一个独立仓（探到但窗口内零提交）
    os.makedirs(repoB)
    _sh(["git", "init", "-q", "-b", "main"], cwd=repoB)
    _sh(["git", "config", "user.email", "t@t"], cwd=repoB)
    _sh(["git", "config", "user.name", "t"], cwd=repoB)
    _commit(repoB, ME, OUT, "z.txt")

    found = discover_git_dirs(tmp, depth=4)
    ck("发现 .git 目录/文件", len(found) == 3)
    ck("识别 worktree 的 gitfile", any(os.path.isfile(g) for _, g in found))
    picked, merged = select_representatives(found)
    ck("common-dir 去重后 2 仓", len(picked) == 2)
    ck("去重合并数=1", merged == 1)
    keys = [p["key"] for p in picked]
    ck("去重键唯一", len(set(keys)) == len(keys))

    r_head = collect(tmp, ME, "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "head", 4)
    ck("HEAD 模式=2 提交", r_head["commits"] == 2)
    ck("作者过滤生效(别人不算)", all(c["author"] == ME for c in r_head["commits_detail"]))
    ck("时间窗过滤(5 月不算)", not any(c["date"].startswith("2026-05") for c in r_head["commits_detail"]))
    ck("行数>0", r_head["insertions"] > 0)
    ck("月度分布仅 2026-08", list(r_head["by_month"].keys()) == ["2026-08"])
    ck("行数记账=明细求和", r_head["lines_total"] == sum(c["insertions"] + c["deletions"] for c in r_head["commits_detail"]))
    ck("worktree 分支头在 HEAD 模式不可见(负控)", r_head["commits"] == 2)

    r_br = collect(tmp, ME, "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "branches", 4)
    ck("branches 模式=3 提交(含 worktree 分支)", r_br["commits"] == 3)
    ck("branches ⊃ head", r_br["commits"] >= r_head["commits"])
    ck("窗口外仓计 0 且不计入", r_br["repos_with_commits"] == 1)

    # ★日期倒挂抗性：父提交比子提交新（rebase / 导入 / 改日期场景）
    repoI = os.path.join(tmp, "repoInv")
    os.makedirs(repoI)
    _sh(["git", "init", "-q", "-b", "main"], cwd=repoI)
    _sh(["git", "config", "user.email", "t@t"], cwd=repoI)
    _sh(["git", "config", "user.name", "t"], cwd=repoI)
    _commit(repoI, ME, IN, "new.txt")    # 新（祖先）
    _commit(repoI, ME, OUT, "old.txt")   # 旧（HEAD）⇒ 倒挂
    r_inv = collect(os.path.join(tmp, "repoInv"), ME,
                    "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "head", 4)
    if since_arg("2026-07-01T00:00:00+08:00")[1] == "filter":
        ck("日期倒挂仍能取到窗口内提交(as-filter)", r_inv["commits"] == 1)
    else:
        ck("日期倒挂抗性(SKIP: git<2.36 无 as-filter)",
           None if r_inv["commits"] == 0 else False)

    r_none = collect(tmp, "不存在的人", "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "all", 4)
    ck("陌生作者=0(负控)", r_none["commits"] == 0)
    # 去重收益：不带去重（模拟旧口径）会重复计 worktree
    all_dirs = [w for w, _ in found]
    ck("含 worktree 的 gitdir 数 > 去重仓数", len(all_dirs) > len(picked))
    # ★author/committer 日期分属不同月（rebase / cherry-pick）：过滤按 committer，月分布按 author
    repoX = os.path.join(tmp, "repoX")
    os.makedirs(repoX)
    _sh(["git", "init", "-q", "-b", "main"], cwd=repoX)
    _sh(["git", "config", "user.email", "t@t"], cwd=repoX)
    _sh(["git", "config", "user.name", "t"], cwd=repoX)
    _commit(repoX, ME, "2026-05-04T09:00:00+08:00", "x.txt",
            cdate="2026-08-11T13:00:00+08:00")
    r_x = collect(repoX, ME, "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "branches", 4)
    ck("跨日期提交被计入(按 committer 过滤)", r_x["commits"] == 1)
    ck("author 月分布=2026-05", list(r_x["by_month"].keys()) == ["2026-05"])
    ck("committer 月分布=2026-08", list(r_x["by_month_committer"].keys()) == ["2026-08"])
    ck("窗口外 author 计数=1 且标注", r_x["out_of_window_author"]["count"] == 1)
    r_x2 = collect(repoX, ME, "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "head", 4)
    ck("同口径 head 模式一致(1 提交)", r_x2["commits"] == 1)
    repoY = os.path.join(tmp, "repoY")
    os.makedirs(repoY)
    _sh(["git", "init", "-q", "-b", "main"], cwd=repoY)
    _sh(["git", "config", "user.email", "t@t"], cwd=repoY)
    _sh(["git", "config", "user.name", "t"], cwd=repoY)
    _commit(repoY, ME, "2026-05-05T09:00:00+08:00", "y.txt",
            cdate="2026-05-06T09:00:00+08:00")
    r_y = collect(repoY, ME, "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "branches", 4)
    ck("负控:committer 也在窗口外 ⇒ 0 提交", r_y["commits"] == 0)
    ck("负控:无提交时 out_of_window 计数=0", r_y["out_of_window_author"]["count"] == 0)

    # 窗口工具联动
    if qw is not None:
        w = qw.resolve(quarter="Q3-2026")
        ck("窗口联动 Q3-2026", w["since"] == "2026-07-01 00:00:00" and w["until"] == "2026-10-01 00:00:00")

    bad = [n for n, ok in checks if ok is False]
    skip = [n for n, ok in checks if ok is None]
    for n, ok in checks:
        print("%-46s %s" % (n, "PASS" if ok else ("SKIP" if ok is None else "FAIL")))
    print("selftest: %d checks, %d failed, %d skipped (fixture=%s)" % (len(checks), len(bad), len(skip), tmp))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
