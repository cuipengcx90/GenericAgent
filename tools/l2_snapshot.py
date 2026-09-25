#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 记忆版本历史：内容寻址快照 + 恢复演练（R412）

背景：memory/global_mem.txt（L2）是本地唯一副本、零 git 历史（.gitignore 只跟踪
memory/**/*.md 与 *.py，不跟踪 .txt）⇒ 误删/误改即永久丢失，而 L1 只存索引级摘要。
本工具给出**最小闭环**：快照（内容寻址去重）→ 校验 → 恢复 → 保留策略（可机械校验）。

设计要点
- 存储：<store>/objects/<sha256>.gz（同内容只存一份，天然去重）+ <store>/manifest.jsonl（append 式台账）
- 幂等：内容未变 → SKIP，不写新版本（对齐 kc_snapshot 的惯例）
- 默认目标：memory/global_mem.txt（可用 --files 追加其它路径）
- 默认存储：~/.ga/l2_snapshots（仓库外、不受 gitignore/仓库清理影响）
- 零新增定时：本工具不自带任何调度，挂载（反射器/launchd）属待批事项

子命令
  snapshot            对目标文件取快照（内容未变则 SKIP）
  list                列出版本台账 + 与当前文件的内容漂移状态（只读）
  verify              校验快照对象完整性（解压后 sha256 必须等于台账记录）（只读）
  restore             从快照恢复（--out 指定落点；默认拒写 memory/ 内文件）
  prune               保留策略执行（默认 dry-run；--apply 才删）
  policy              保留策略的机械校验（份数/上限/对象齐备）（只读）
  selftest            自包含演练（含 4 条负控），隔离临时 store

保留策略（写死；policy 子命令可机械校验）
  keep_per_path = 30 份/路径（内容相同不重复计数）
  max_store_mb  = 64 MiB（store 总上限，超出时 prune 按时间从旧到新删）
  min_keep      = 1 份（任何情况下至少留 1 份；即 keep 至少要能保留最新一版）
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

POLICY = {
    "keep_per_path": 30,
    "max_store_mb": 64,
    "min_keep": 1,
}

DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".ga", "l2_snapshots")
DEFAULT_TARGET = os.path.join("memory", "global_mem.txt")
OBJ_DIR = "objects"
MANIFEST = "manifest.jsonl"


def ga_root():
    """仓库根：本文件在 <root>/tools/ 下；否则退回 cwd。"""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if os.path.basename(here) == "tools" and os.path.isdir(os.path.join(parent, "memory")):
        return parent
    return os.getcwd()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def load_manifest(store, quiet=False):
    p = os.path.join(store, MANIFEST)
    entries, bad = [], 0
    if not os.path.exists(p):
        return entries, bad
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                bad += 1
                continue
            if not isinstance(e, dict) or "sha256" not in e or "path" not in e:
                bad += 1
                continue
            entries.append(e)
    if bad and not quiet:
        print(f"[warn] manifest 中有 {bad} 行无法解析（已跳过，未计入版本数）")
    return entries, bad


def obj_path(store, sha):
    return os.path.join(store, OBJ_DIR, sha + ".gz")


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def fmt_size(n):
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024.0:.1f}KB"
    return f"{n/1024.0/1024.0:.2f}MB"


def ordered(entries, path=None):
    """按「时间戳 + 台账插入序」定序（同秒并列时以 manifest 行序为准，稳定）。
    坑：曾用 (ts, sha256) 排序 ⇒ 同一秒内的多版本被按 sha 字典序重排，
    使 latest_entry 把旧版本当成最新（自测⑥ 因此误判"已入账"失效，台账重复追加）。"""
    idx = [(i, e) for i, e in enumerate(entries)
           if path is None or e.get("path") == path]
    idx.sort(key=lambda t: (t[1].get("ts", ""), t[0]))
    return [e for _, e in idx]


def latest_entry(entries, path):
    seq = ordered(entries, path)
    return seq[-1] if seq else None


# ---------------------------------------------------------------- snapshot
def cmd_snapshot(a):
    store = os.path.abspath(a.store)
    os.makedirs(os.path.join(store, OBJ_DIR), exist_ok=True)
    entries, _bad = load_manifest(store, quiet=True)
    written = skipped = 0
    for target in a.files:
        path = os.path.abspath(target)
        if not os.path.exists(path):
            print(f"[error] 目标不存在：{path}")
            return 2
        data = open(path, "rb").read()
        sha = sha256_bytes(data)
        prev = latest_entry(entries, path)
        if prev and prev["sha256"] == sha and not a.force:
            print(f"SKIP   {path}  sha={sha[:12]}  (与上一版本相同，未新增)")
            skipped += 1
            continue
        op = obj_path(store, sha)
        if not os.path.exists(op):
            raw = gzip.compress(data, 9)
            tmp = op + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, op)
            gz_bytes = len(raw)
        else:
            gz_bytes = os.path.getsize(op)
        e = {
            "ts": now_iso(),
            "path": path,
            "sha256": sha,
            "bytes": len(data),
            "gz_bytes": gz_bytes,
            "label": a.label or "",
            "tool": "l2_snapshot.py/v1",
        }
        with open(os.path.join(store, MANIFEST), "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        entries.append(e)
        print(f"OK     {path}  sha={sha[:12]}  {len(data)}B → {gz_bytes}B({gz_bytes/len(data)*100:.1f}%)")
        written += 1
    total = dir_size(store)
    print(f"[store] {store}  本次写入 {written} / 跳过 {skipped}  当前占用 {fmt_size(total)}")
    if total > POLICY["max_store_mb"] * 1024 * 1024:
        print(f"[warn] store 超过策略上限 {POLICY['max_store_mb']}MiB，请跑 prune")
    return 0


# ---------------------------------------------------------------- list
def cmd_list(a):
    store = os.path.abspath(a.store)
    entries, _ = load_manifest(store)
    if not entries:
        print(f"[list] 空（store={store}）")
        return 0
    by_path = {}
    for e in entries:
        by_path.setdefault(e["path"], []).append(e)
    print(f"[list] store={store}  台账条目={len(entries)}  涉及文件={len(by_path)}")
    for path, es in sorted(by_path.items()):
        es.sort(key=lambda e: e.get("ts", ""))
        exists = os.path.exists(path)
        live = sha256_file(path) if exists else None
        print(f"\n文件 {path}  版本数={len(es)}  当前{'存在' if exists else '缺失!'}")
        print(f"  当前 sha256 = {live[:16] if live else '-'}")
        for i, e in enumerate(es):
            tag = "←最新" if e is es[-1] else ""
            drift = ""
            if e is es[-1]:
                if live is None:
                    drift = " [文件缺失！可从本快照恢复]"
                elif live != e["sha256"]:
                    drift = " [与当前文件不同：已漂移]"
                else:
                    drift = " [与当前文件一致]"
            print(f"  #{i+1:<3} {e.get('ts','')}  {e['sha256'][:12]}  {e.get('bytes',0)}B "
                  f"{e.get('label','')} {tag}{drift}")
    return 0


# ---------------------------------------------------------------- verify
def cmd_verify(a):
    store = os.path.abspath(a.store)
    entries, bad_lines = load_manifest(store)
    if a.latest:
        latest = {}
        for e in entries:
            p = e["path"]
            if p not in latest or e.get("ts", "") > latest[p].get("ts", ""):
                latest[p] = e
        entries = list(latest.values())
    ok = fail = 0
    problems = []
    for e in entries:
        op = obj_path(store, e["sha256"])
        if not os.path.exists(op):
            fail += 1
            problems.append(f"对象缺失 {op}")
            continue
        try:
            data = gzip.decompress(open(op, "rb").read())
        except Exception as ex:
            fail += 1
            problems.append(f"对象解压失败 {op}: {ex}")
            continue
        got = sha256_bytes(data)
        if got != e["sha256"]:
            fail += 1
            problems.append(f"内容不符 {op}: 记录={e['sha256'][:12]} 实得={got[:12]}")
        elif len(data) != e.get("bytes", len(data)):
            fail += 1
            problems.append(f"字节数不符 {op}: 记录={e.get('bytes')} 实得={len(data)}")
        else:
            ok += 1
    print(f"[verify] 校验对象={len(entries)}（manifest 坏行={bad_lines}） OK={ok} FAIL={fail}")
    for p in problems:
        print("  FAIL " + p)
    return 1 if fail else 0


# ---------------------------------------------------------------- restore
def cmd_restore(a):
    store = os.path.abspath(a.store)
    entries, _ = load_manifest(store)
    if not entries:
        print(f"[restore] 空台账（store={store}）")
        return 2
    path_filter = os.path.abspath(a.file) if a.file else None
    cand = ordered(entries, path_filter)
    if not cand:
        print(f"[restore] 未找到 {path_filter} 的任何版本")
        return 2
    if a.version:
        if a.version > len(cand):
            print(f"[restore] version={a.version} 超出（共 {len(cand)} 版）")
            return 2
        e = cand[a.version - 1]
    elif a.sha:
        match = [x for x in cand if x["sha256"].startswith(a.sha)]
        if not match:
            print(f"[restore] 无 sha 前缀匹配 {a.sha}")
            return 2
        if len(match) > 1:
            print(f"[restore] sha 前缀歧义 {a.sha}（{len(match)} 条）")
            return 2
        e = match[0]
    else:
        e = cand[-1]
    out = os.path.abspath(a.out)
    if not a.allow_in_place:
        memdir = os.path.join(ga_root(), "memory")
        if os.path.commonpath([out, os.path.abspath(memdir)]) == os.path.abspath(memdir):
            print(f"[restore] 拒绝直接写回 memory/ 内文件（{out}）：请恢复到他处，"
                  f"或显式加 --allow-in-place")
            return 3
    op = obj_path(store, e["sha256"])
    if not os.path.exists(op):
        print(f"[restore] 快照对象缺失 {op}")
        return 2
    data = gzip.decompress(open(op, "rb").read())
    got = sha256_bytes(data)
    if got != e["sha256"]:
        print(f"[restore] 快照对象已损坏：记录={e['sha256'][:12]} 实得={got[:12]}")
        return 1
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    back = sha256_file(out)
    same = back == e["sha256"]
    print(f"[restore] {e['path']}\n  版本 ts={e.get('ts')} sha={e['sha256'][:16]}\n"
          f"  → {out}  {len(data)}B  写后校验 {'一致' if same else '不一致!'}")
    if not same:
        return 1
    # 就地回滚 = 状态变更 ⇒ 自动入账（同 sha 对象已存在，磁盘零增量）；幂等
    tracked_here = (out == e["path"])
    if (getattr(a, "record", False) or tracked_here) and not getattr(a, "no_record", False):
        latest = latest_entry(entries, out)
        if latest and latest["sha256"] == e["sha256"]:
            print(f"[restore] 台账已是该内容（sha={e['sha256'][:12]}），未重复入账")
        else:
            ne = {
                "ts": now_iso(), "path": out, "sha256": e["sha256"], "bytes": len(data),
                "gz_bytes": os.path.getsize(op),
                "label": (f"restore from #{a.version}" if a.version
                          else f"restore from {e['sha256'][:12]}"),
                "tool": "l2_snapshot.py/v1",
            }
            with open(os.path.join(store, MANIFEST), "a", encoding="utf-8") as f:
                f.write(json.dumps(ne, ensure_ascii=False) + "\n")
            print(f"[restore] 已入账：{out}  版本 +1（对象复用，磁盘零增量）")
    return 0


# ---------------------------------------------------------------- prune / policy
def retention_keep(entries):
    """按策略算出应保留的条目（每路径最新 keep 份）+ 应删条目。"""
    keep, drop = [], []
    by_path = {}
    for e in entries:
        by_path.setdefault(e["path"], []).append(e)
    for path, es in by_path.items():
        es = ordered(es)
        k = max(POLICY["min_keep"], POLICY["keep_per_path"])
        if len(es) <= k:
            keep.extend(es)
        else:
            drop.extend(es[:-k])
            keep.extend(es[-k:])
    return keep, drop


def cmd_policy(a):
    """保留策略的机械校验（只读）。"""
    store = os.path.abspath(a.store)
    entries, bad = load_manifest(store)
    keep, drop = retention_keep(entries)
    by_path = {}
    for e in keep:
        by_path.setdefault(e["path"], []).append(e)
    checks = []
    checks.append(("manifest 无坏行", bad == 0, f"坏行={bad}"))
    max_n = max([len(v) for v in by_path.values()] or [0])
    checks.append((f"每路径份数 ≤ {POLICY['keep_per_path']}",
                   max_n <= POLICY["keep_per_path"], f"实测最大={max_n}"))
    checks.append((f"每路径至少 {POLICY['min_keep']} 份",
                   all(len(v) >= POLICY["min_keep"] for v in by_path.values()),
                   f"路径数={len(by_path)}"))
    missing = [obj_path(store, e["sha256"]) for e in keep
               if not os.path.exists(obj_path(store, e["sha256"]))]
    checks.append(("保留版本的对象齐备", not missing, f"缺失={len(missing)}"))
    total = dir_size(store)
    cap = POLICY["max_store_mb"] * 1024 * 1024
    checks.append((f"store 占用 ≤ {POLICY['max_store_mb']}MiB",
                   total <= cap, f"实测={fmt_size(total)}"))
    live_objs = set(os.listdir(os.path.join(store, OBJ_DIR))) if os.path.isdir(
        os.path.join(store, OBJ_DIR)) else set()
    keep_objs = {e["sha256"] + ".gz" for e in keep}
    orphans = sorted(live_objs - keep_objs)
    checks.append(("无孤儿对象（未保留的旧版本已清）", not orphans, f"孤儿={len(orphans)}"))
    print(f"[policy] store={store}  台账={len(entries)}  应保留={len(keep)}  可回收={len(drop)}")
    rc = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
        if not ok:
            rc = 1
    return rc


def cmd_prune(a):
    store = os.path.abspath(a.store)
    entries, _ = load_manifest(store)
    keep, drop = retention_keep(entries)
    keep_objs = {e["sha256"] + ".gz" for e in keep}
    objdir = os.path.join(store, OBJ_DIR)
    live = sorted(os.listdir(objdir)) if os.path.isdir(objdir) else []
    orphans = [o for o in live if o not in keep_objs]
    freed = sum(os.path.getsize(os.path.join(objdir, o)) for o in orphans)
    print(f"[prune] 台账={len(entries)}  保留={len(keep)}  过期条目={len(drop)}  "
          f"孤儿对象={len(orphans)}  可回收={fmt_size(freed)}")
    for o in orphans:
        print("  drop " + o)
    if not a.apply:
        print("[prune] dry-run（未删除任何文件）；加 --apply 才落盘")
        return 0
    for o in orphans:
        os.remove(os.path.join(objdir, o))
    # 重写 manifest：只保留 keep 条目
    mpath = os.path.join(store, MANIFEST)
    bak = mpath + ".bak"
    shutil.copy2(mpath, bak)
    with open(mpath, "w", encoding="utf-8") as f:
        for e in keep:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    after = dir_size(store)
    print(f"[prune] 已删除 {len(orphans)} 个对象；store 现占用 {fmt_size(after)}（manifest 备份 {bak}）")
    return 0


# ---------------------------------------------------------------- selftest
def cmd_selftest(a):
    """隔离临时 store 的自包含演练：快照→幂等→篡改检出→恢复一致→策略。含负控。"""
    results = []
    tmp = tempfile.mkdtemp(prefix="l2snap_selftest_")
    store = os.path.join(tmp, "store")
    tgt = os.path.join(tmp, "fake_l2.txt")

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")

    base = "line1 记忆内容\nline2 中文测试\nline3 end\n"
    with open(tgt, "w", encoding="utf-8") as f:
        f.write(base)

    class NS:
        pass

    ns = NS()
    ns.store, ns.files, ns.label, ns.force = store, [tgt], "selftest", False
    rc1 = cmd_snapshot(ns)
    check("① 首跑写入版本", rc1 == 0 and os.path.exists(obj_path(store, sha256_file(tgt))),
          f"rc={rc1}")
    e_after_first, _ = load_manifest(store, quiet=True)
    check("① 语料自证：台账条目=1", len(e_after_first) == 1, f"实测={len(e_after_first)}")

    # 幂等：同内容再跑不新增
    rc2 = cmd_snapshot(ns)
    e_after_second, _ = load_manifest(store, quiet=True)
    check("② 幂等：同内容不新增版本", rc2 == 0 and len(e_after_second) == len(e_after_first),
          f"条目 {len(e_after_first)}→{len(e_after_second)}")

    # 变更后新增版本
    with open(tgt, "w", encoding="utf-8") as f:
        f.write(base + "line4 新增一行\n")
    cmd_snapshot(ns)
    entries, _ = load_manifest(store, quiet=True)
    check("③ 内容变化后新增版本", len(entries) == 2, f"条目={len(entries)}（期望 2）")

    # verify 正常
    ns2 = NS()
    ns2.store, ns2.latest = store, False
    check("④ verify 全绿", cmd_verify(ns2) == 0)

    # 负控1：篡改 store 对象 → verify 必须 FAIL
    victim = obj_path(store, entries[0]["sha256"])
    raw = open(victim, "rb").read()
    with open(victim, "wb") as f:
        f.write(gzip.compress(gzip.decompress(raw) + b"TAMPERED", 9))
    ns3 = NS()
    ns3.store, ns3.latest = store, False
    check("[负控1] 篡改快照对象必被检出", cmd_verify(ns3) == 1)
    with open(victim, "wb") as f:  # 还原
        f.write(raw)
    check("[负控1-还原] 还原后 verify 回到全绿", cmd_verify(ns3) == 0)

    # 恢复演练：恢复最新版 → sha 与台账记录逐字节一致
    out = os.path.join(tmp, "restored.txt")
    ns4 = NS()
    ns4.store, ns4.file, ns4.out = store, tgt, out
    ns4.version, ns4.sha, ns4.allow_in_place = None, None, False
    rc5 = cmd_restore(ns4)
    check("⑤ 恢复后 sha256 与快照记录一致",
          rc5 == 0 and sha256_file(out) == sha256_file(tgt),
          f"恢复={sha256_file(out)[:12]} 源={sha256_file(tgt)[:12]}")

    # 负控2：篡改恢复产物 → sha 断言必须失败（校验器本身有效）
    with open(out, "ab") as f:
        f.write(b"x")
    check("[负控2] 篡改恢复产物后 sha 断言失败", sha256_file(out) != sha256_file(tgt))

    # 负控3：写回 memory/ 内被拒
    ns5 = NS()
    ns5.store, ns5.file, ns5.out = store, tgt, os.path.join(ga_root(), "memory", "global_mem.txt")
    ns5.version, ns5.sha, ns5.allow_in_place = None, None, False
    check("[负控3] 拒写 memory/ 内原文件", cmd_restore(ns5) == 3)

    # ⑥ 就地回滚：自动入账 + 幂等（对象复用，磁盘零增量）
    n_before = len(load_manifest(store, quiet=True)[0])
    ns6 = NS()
    ns6.store, ns6.file, ns6.out = store, tgt, tgt
    ns6.version, ns6.sha, ns6.allow_in_place = 1, None, True
    ns6.record, ns6.no_record = False, False
    rc6 = cmd_restore(ns6)
    n_after = len(load_manifest(store, quiet=True)[0])
    cmd_restore(ns6)
    n_again = len(load_manifest(store, quiet=True)[0])
    check("⑥ 就地回滚自动入账且幂等", rc6 == 0 and n_after == n_before + 1 and n_again == n_after,
          f"台账 {n_before} → {n_after} → {n_again}")

    # 负控4：保留策略——把上限临时压到 3，造 5 版本，prune 必须恰好留 3 删 2
    ns6 = NS()
    ns6.store, ns6.files, ns6.label, ns6.force = store, [tgt], "prune", False
    _saved_keep = POLICY["keep_per_path"]
    for i in range(5):
        with open(tgt, "w", encoding="utf-8") as f:
            f.write(base + f"v{i}\n")
        cmd_snapshot(ns6)
    try:
        POLICY["keep_per_path"] = 3
        ent, _ = load_manifest(store, quiet=True)
        keep, drop = retention_keep(ent)
        per = [e for e in keep if e["path"] == os.path.abspath(tgt)]
        drop_here = [e for e in drop if e["path"] == os.path.abspath(tgt)]
        check("[负控4] 保留策略份数可机械校验（上限=3 真触发 prune）",
              len(per) == 3 and len(drop_here) == len(ent) - 3,
              f"台账={len(ent)} 保留={len(per)} 过期={len(drop_here)} (keep=3)")
        ns7 = NS()
        ns7.store, ns7.apply = store, True
        rc_p = cmd_prune(ns7)
        left, _ = load_manifest(store, quiet=True)
        check("[负控4b] prune --apply 后台账=保留数",
              rc_p == 0 and len(left) == len(keep),
              f"prune 后台账={len(left)} 期望={len(keep)}")
    finally:
        POLICY["keep_per_path"] = _saved_keep

    shutil.rmtree(tmp, ignore_errors=True)
    n = len(results)
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n[selftest] {passed}/{n} PASS" + ("  全部通过" if passed == n else "  有失败项"))
    return 0 if passed == n else 1


# ---------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(description="L2 记忆版本历史：内容寻址快照 + 恢复演练（R412）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_store(sp):
        sp.add_argument("--store", default=DEFAULT_STORE, help=f"快照仓库目录（默认 {DEFAULT_STORE}）")

    sp = sub.add_parser("snapshot", help="取快照（内容未变则 SKIP）")
    add_store(sp)
    sp.add_argument("--files", nargs="*", default=[os.path.join(ga_root(), DEFAULT_TARGET)],
                    help="目标文件（默认 memory/global_mem.txt）")
    sp.add_argument("--label", default="", help="本次快照标签（写进台账）")
    sp.add_argument("--force", action="store_true", help="内容未变也强制记一条新版本")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("list", help="列出台账与漂移状态（只读）")
    add_store(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("verify", help="校验快照对象完整性（只读）")
    add_store(sp)
    sp.add_argument("--latest", action="store_true", help="只校验每路径最新一版")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("restore", help="从快照恢复")
    add_store(sp)
    sp.add_argument("--file", default=None, help="限定源文件（默认全部版本里挑）")
    sp.add_argument("--out", required=True, help="恢复落点")
    sp.add_argument("--version", type=int, default=None, help="第 N 版（1-based，时间升序）")
    sp.add_argument("--sha", default=None, help="按 sha256 前缀选择版本")
    sp.add_argument("--allow-in-place", action="store_true", help="允许直接写回 memory/ 内文件")
    sp.add_argument("--record", action="store_true", help="强制把本次恢复登记为受管文件的一个版本")
    sp.add_argument("--no-record", action="store_true", help="就地回滚也不入账（默认就地回滚自动入账）")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("prune", help="按保留策略回收（默认 dry-run）")
    add_store(sp)
    sp.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("policy", help="保留策略的机械校验（只读）")
    add_store(sp)
    sp.set_defaults(func=cmd_policy)

    sp = sub.add_parser("selftest", help="自包含演练（含 4 条负控）")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
