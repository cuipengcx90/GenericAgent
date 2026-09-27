#!/usr/bin/env python3
"""cache_reclaim.py —— 磁盘「可再生空间」回收工具箱（TODO#58：把 R364 的一次性回收固化成可重复动作）

设计原则（来自 R364 实测结论）
  * 只处理**可再生**缓存：包管理器 store / 下载缓存 / 应用 HTTP 缓存 / updater 残留安装包。
  * 红线：绝不碰 logs/、Documents/、Downloads/、Pictures/、Photos/、用户文件；绝不自动升级任何工具。
  * T0 资产（memory/、temp/kc_snapshots/、tools/、sche_tasks/）在 --apply 前后做 sha256 全量比对，必须**逐字节不变**。
  * 分三档：safe（默认可回收）/ approval（需人批准：停服务、关 IDE、重下大件）/ never（不碰）。
  * 本机坑：uv、pnpm 不在非交互 shell 的 PATH 上（uv=~/.local/bin/uv, pnpm=~/Library/pnpm/pnpm）⇒ 一律用绝对路径解析。
  * uv 缓存被 `uv tool` 常驻 daemon 持有 .lock，`uv cache prune` 会静默等锁直到超时（R364 实证）⇒ 默认降级为 approval。

用法
  python3 tools/cache_reclaim.py                     # dry-run（默认，只读）：按可回收量排序 top10
  python3 tools/cache_reclaim.py --all               # 列出全部候选（含保留项/理由）
  python3 tools/cache_reclaim.py --apply --yes       # 显式确认后执行 safe 档，逐项记 before/after 字节
  python3 tools/cache_reclaim.py --apply --yes --only pnpm-store,npm-cache
  python3 tools/cache_reclaim.py --verify            # T0 资产 sha256 vs 基线（temp/t0_baseline.sha256）
  python3 tools/cache_reclaim.py --probe uv|playwright   # 「删→重建→可用」闭环实测（记耗时/网络依赖）
  python3 tools/cache_reclaim.py --verify --baseline temp/t0_baseline.sha256
退出码：0 正常；1 --verify 有差异 或 --apply 后 T0 变了
"""
from __future__ import annotations
import argparse, glob, hashlib, json, os, re, shutil, subprocess, sys, time

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # GA 仓库根
TEMP = os.path.join(ROOT, "temp")
DEFAULT_BASELINE = os.path.join(TEMP, "t0_baseline.sha256")
T0_ROOTS = ["memory", "temp/kc_snapshots", "tools", "sche_tasks"]

# ── 二进制解析（非交互 shell 假阴性坑）────────────────────────────────────────
BIN_DIRS = [f"{HOME}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", f"{HOME}/Library/pnpm", f"{HOME}/bin"]

def which(name: str) -> str | None:
    for d in BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(name)


def sh(cmd, timeout=180):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return dict(rc=p.returncode, sec=round(time.time() - t0, 1),
                    out=(p.stdout or "")[-600:], err=(p.stderr or "")[-300:], timeout=False)
    except subprocess.TimeoutExpired:
        return dict(rc=None, sec=round(time.time() - t0, 1), out="",
                    err=f"TIMEOUT>{timeout}s（子进程被强杀，该步可能只做了一半）", timeout=True)
    except FileNotFoundError as e:
        return dict(rc=127, sec=0.0, out="", err=f"未找到可执行文件: {e}", timeout=False)


def du_b(path) -> int:
    """占用字节（du -sk，读不到返回 -1）"""
    if not os.path.exists(path):
        return 0
    r = sh(["du", "-sk", path], timeout=300)
    try:
        return int(r["out"].split()[0]) * 1024
    except Exception:
        return -1


def pgrep(pattern: str) -> list[str]:
    ps = sh(["ps", "-Ao", "pid=,command="], timeout=30)
    pat = re.compile(pattern)
    out = []
    for line in ps["out"].splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        if pat.search(cmd) and "cache_reclaim.py" not in cmd:
            out.append(f"{pid} {cmd[:90]}")
    return out


def rm_contents(d: str, label: str) -> dict:
    """删目录内容、保留目录本身"""
    t0 = time.time()
    if not os.path.isdir(d):
        return dict(action=label, cmd=f"rm -rf {d}/*", rc=1, sec=0.0, out="目录不存在", err="")
    n, errs = 0, 0
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            n += 1
        except Exception:
            errs += 1
    return dict(action=label, cmd=f"rm -rf {d}/*", rc=0, sec=round(time.time() - t0, 1),
                out=f"删除 {n} 个子项" + (f"，{errs} 个跳过" if errs else ""), err="")


# ── 候选注册表 ────────────────────────────────────────────────────────────────
# klass: safe / approval / never      advice: 删 / 剪 / prune / 保留
def _pw():
    return f"{HOME}/Library/Caches/ms-playwright"


UPDATER_NAMES = ["bitwarden-updater", "@tutti-osdesktop-updater", "orca-updater",
                 "@getpaseodesktop-updater", "org.yuanli.utools.ShipIt", "utools-updater"]


def _updater_size() -> int:
    """只算 updater 残留目录之和（不是整个 ~/Library/Caches）"""
    return sum(du_b(f"{HOME}/Library/Caches/{n}") for n in UPDATER_NAMES
               if os.path.isdir(f"{HOME}/Library/Caches/{n}"))


def _rustup_nightly_size() -> int:
    """~/.rustup 里只算 nightly 工具链目录（可 uninstall 的部分）"""
    base = f"{HOME}/.rustup/toolchains"
    if not os.path.isdir(base):
        return 0
    return sum(du_b(os.path.join(base, d)) for d in os.listdir(base) if "nightly" in d)


def _pw_old_size() -> int:
    """ms-playwright 中**旧版本**（非最新）目录占用"""
    base = _pw()
    if not os.path.isdir(base):
        return 0
    vers = {}
    for d in os.listdir(base):
        m = re.match(r"^(chromium|chromium_headless_shell|firefox|webkit)-(\d+)$", d)
        if m:
            vers.setdefault(m.group(1), []).append((int(m.group(2)), d))
    tot = 0
    for _, lst in vers.items():
        lst.sort()
        for _, d in lst[:-1]:
            tot += max(0, du_b(os.path.join(base, d)))
    return tot


ITEMS = [
    # --- safe：可再生且当前无用例占用 -----------------------------------------
    dict(id="pnpm-store", path=f"{HOME}/Library/pnpm/store", klass="safe", advice="prune",
         run=lambda: sh([which("pnpm") or "pnpm", "store", "prune"], 300),
         rebuild="任意项目 `pnpm install` 时按需回填（内容寻址仓库，只删无引用包）", cost="≈0"),
    dict(id="npm-cache", path=f"{HOME}/.npm/_cacache", klass="safe", advice="删",
         run=lambda: sh([which("npm") or "npm", "cache", "clean", "--force"], 180),
         rebuild="`npm install` 自动回填", cost="≈0"),
    dict(id="brew-cleanup", path=f"{HOME}/Library/Caches/Homebrew", klass="safe", advice="prune",
         run=lambda: sh([which("brew") or "brew", "cleanup", "-s"], 180),
         rebuild="`brew install` 时重下", cost="小"),
    dict(id="brew-downloads", path=f"{HOME}/Library/Caches/Homebrew/downloads", klass="safe", advice="删",
         run=lambda: rm_contents(f"{HOME}/Library/Caches/Homebrew/downloads", "brew 下载缓存"),
         rebuild="下次安装时重下", cost="小"),
    dict(id="electron-cache", path=f"{HOME}/Library/Caches/electron", klass="safe", advice="删",
         run=lambda: rm_contents(f"{HOME}/Library/Caches/electron", "electron 下载缓存"),
         rebuild="electron-builder 下次重下", cost="小"),
    dict(id="node-gyp", path=f"{HOME}/Library/Caches/node-gyp", klass="safe", advice="删",
         run=lambda: rm_contents(f"{HOME}/Library/Caches/node-gyp", "node-gyp 头文件缓存"),
         rebuild="下次原生模块编译时重下", cost="小"),
    dict(id="pip-cache", path=f"{HOME}/Library/Caches/pip", klass="safe", advice="删",
         run=lambda: rm_contents(f"{HOME}/Library/Caches/pip", "pip wheel 缓存"),
         rebuild="`pip install` 自动回填", cost="小"),
    dict(id="bun-cache", path=f"{HOME}/.bun/install/cache", klass="safe", advice="删",
         run=lambda: rm_contents(f"{HOME}/.bun/install/cache", "bun 安装缓存"),
         rebuild="`bun install` 自动回填", cost="小"),
    dict(id="chrome-http", path=f"{HOME}/Library/Caches/Google/Chrome", klass="safe", advice="删",
         run=lambda: rm_contents(f"{HOME}/Library/Caches/Google/Chrome", "Chrome HTTP 缓存"),
         rebuild="浏览时自动回填（Chrome 在跑亦安全）", cost="≈0"),
    dict(id="updater-residues", path=f"{HOME}/Library/Caches", klass="safe", advice="删",
         run=lambda: _updaters(), size_fn=lambda: sum(du_b(f"{HOME}/Library/Caches/{n}") for n in UPDATER_NAMES if os.path.isdir(f"{HOME}/Library/Caches/{n}")),
         rebuild="下次自动更新时重下", cost="小",
         note="electron-updater 残留安装包（bitwarden/tutti/orca/getpaseo/utools；占用=仅这几个目录之和，非整个 Caches）"),
    # --- approval：需人批准（停服务/关 IDE/重下大件/MCP 在用）------------------
    dict(id="jetbrains-index", path=f"{HOME}/Library/Caches/JetBrains", klass="approval", advice="删",
         run=lambda: rm_contents(_jb_index(), "JetBrains 索引"),
         size_fn=lambda: du_b(_jb_index()),
         guard=lambda: (bool(pgrep(r"/IntelliJ|idea/Contents")), "IDEA 正在运行，删索引需先退出 IDE"),
         rebuild="重开 IDE 自动重建索引（分钟级）", cost="分钟级等待"),
    dict(id="uv-cache", path=f"{HOME}/.cache/uv", klass="approval", advice="prune",
         run=lambda: sh([which("uv") or "uv", "cache", "prune"], 240),
         guard=lambda: (bool(pgrep(r"uv tool|uvx")), "有 uv tool 常驻进程持有 .lock ⇒ prune 会静默等到超时（R364 实证），须先停服务"),
         rebuild="`uv tool/uv pip` 下次按需回填", cost="重下依赖"),
    dict(id="rustup-nightly", path=f"{HOME}/.rustup", klass="approval", advice="剪",
         run=lambda: sh([which("rustup") or "rustup", "toolchain", "uninstall", "nightly"], 240),
         size_fn=_rustup_nightly_size,
         rebuild="`rustup toolchain install nightly` 重下（~2.6GB）", cost="重下 GB 级"),
    dict(id="ms-playwright-old", path=_pw(), klass="approval", advice="剪",
         run=lambda: _pw_prune_old(), size_fn=_pw_old_size, rebuild="`npx playwright install <版本>` 重下", cost="重下 ~0.3GB",
         note="只删旧版本，保留最新在用版本"),
    dict(id="ms-playwright-mcp", path=f"{HOME}/Library/Caches/ms-playwright-mcp", klass="never", advice="保留",
         run=None, rebuild="浏览器 profile 可能含登录态", cost="需重新登录"),
    dict(id="logs", path=f"{ROOT}/logs", klass="approval", advice="保留",
         run=None, rebuild="日志压缩另有专用工具 tools/log_compress.py", cost="—"),
    # --- never：绝不自动删 ----------------------------------------------------
    dict(id="m2", path=f"{HOME}/.m2", klass="never", advice="保留", run=None,
         rebuild="业务仓构建依赖，删后需重下全部依赖", cost="高"),
    dict(id="local-uv-tools", path=f"{HOME}/.local", klass="never", advice="保留", run=None,
         rebuild="uv 安装面（在用），非缓存", cost="—"),
    dict(id="models-gguf", path=f"{HOME}/models/gguf", klass="never", advice="保留", run=None,
         rebuild="本地推理模型权重，重下数 GB", cost="高"),
    dict(id="agent-runtimes", path=f"{HOME}/.pi", klass="never", advice="保留", run=None,
         rebuild="Agent runtime（在用）", cost="—"),
]


def _jb_index() -> str:
    base = f"{HOME}/Library/Caches/JetBrains"
    if not os.path.isdir(base):
        return base
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d, "index")
        if os.path.isdir(p):
            return p
    return base


def _updaters() -> dict:
    names = UPDATER_NAMES
    tot, done = 0, []
    for n in names:
        d = f"{HOME}/Library/Caches/{n}"
        if os.path.exists(d) and os.path.isdir(d):
            r = rm_contents(d, f"updater: {n}")
            done.append(n)
            tot += 1
    return dict(action="删 updater 残留", cmd="rm -rf 各 updater 缓存", rc=0, sec=0.0,
                out=f"处理 {tot} 项: {', '.join(done) or '无'}", err="")


def _pw_prune_old() -> dict:
    base = _pw()
    if not os.path.isdir(base):
        return dict(action="playwright 旧版剪枝", cmd="-", rc=1, sec=0.0, out="目录不存在", err="")
    vers = {}
    for d in os.listdir(base):
        m = re.match(r"^(chromium|chromium_headless_shell|firefox|webkit)-(\d+)$", d)
        if m:
            vers.setdefault(m.group(1), []).append((int(m.group(2)), d))
    removed, keep = [], []
    for name, lst in vers.items():
        lst.sort()
        keep.append(lst[-1][1])
        for _, d in lst[:-1]:
            shutil.rmtree(os.path.join(base, d), ignore_errors=True)
            removed.append(d)
    return dict(action="playwright 旧版剪枝", cmd="rm -rf <旧版本目录>", rc=0, sec=0.0,
                out=f"删 {removed or '无'} | 保留 {keep}", err="")


# ── T0 资产校验 ──────────────────────────────────────────────────────────────
def t0_digest() -> tuple[str, dict]:
    per = {}
    for rel in T0_ROOTS:
        base = os.path.join(ROOT, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    h = hashlib.sha256(open(fp, "rb").read()).hexdigest()
                except Exception:
                    continue
                per[os.path.relpath(fp, ROOT)] = h
    blob = "\n".join(f"{h}  {p}" for p, h in sorted(per.items()))
    return hashlib.sha256(blob.encode()).hexdigest(), per


def cmd_verify(baseline: str | None) -> int:
    dig, per = t0_digest()
    print(f"[verify] T0 资产 {len(per)} 个文件  sha256 汇总={dig[:16]}…")
    if not baseline:
        print(f"[verify] 未给基线，仅打印当前摘要（可存为 {DEFAULT_BASELINE}）")
        return 0
    if not os.path.isfile(baseline):
        print(f"[verify] 基线不存在: {baseline} ⇒ 视为首次，写入当前摘要")
        open(baseline, "w").write("\n".join(f"{h}  {p}" for p, h in sorted(per.items())))
        return 0
    old = {}
    for line in open(baseline):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        h, _, p = line.partition("  ")
        if p.startswith(ROOT + os.sep):
            p = os.path.relpath(p, ROOT)   # R364 基线存绝对路径 ⇒ 归一到相对，否则假差异
        old[p] = h
    added = sorted(set(per) - set(old))
    removed = sorted(set(old) - set(per))
    changed = sorted(p for p in set(per) & set(old) if per[p] != old[p])
    print(f"[verify] 基线 {os.path.relpath(baseline, ROOT)}: {len(old)} 个文件")
    print(f"[verify] 新增={len(added)} 删除={len(removed)} 内容变化={len(changed)}")
    for tag, lst in (("+", added), ("-", removed), ("~", changed)):
        for p in lst[:15]:
            print(f"        {tag} {p}")
        if len(lst) > 15:
            print(f"        {tag} …另 {len(lst)-15} 个")
    if added or removed or changed:
        print("[verify] 结论：T0 **有差异**（注意：memory/ 会被其他会话正常写入 ⇒ 差异需人工区分来源）")
        return 1
    print("[verify] 结论：T0 **完全一致**（逐字节不变）✅")
    return 0


# ── dry-run / apply ─────────────────────────────────────────────────────────
def _guard_of(it) -> tuple[bool, str]:
    if it["klass"] == "never":
        return True, "never 档：不碰"
    g = it.get("guard")
    if g:
        hit, why = g()
        if hit:
            return True, why
    return False, ""


def scan(items, do_du=True):
    rows = []
    for it in items:
        path = it["path"]
        if not os.path.exists(path):
            continue
        locked, why = _guard_of(it)
        # never 档恒为 never（不因占用而升格成 approval）；safe 且被占用 ⇒ 升格 approval
        klass = it["klass"] if it["klass"] == "never" else (
            "approval" if (it["klass"] == "approval" or locked) else it["klass"])
        sz = (it["size_fn"]() if "size_fn" in it else du_b(path)) if do_du else -1
        rows.append(dict(id=it["id"], path=path, klass=klass, advice=it["advice"],
                         size=sz, blocked=locked, blocked_why=why,
                         note=it.get("note", ""), rebuild=it.get("rebuild", ""), cost=it.get("cost", "")))
    rows.sort(key=lambda r: -(r["size"] if r["size"] > 0 else 0))
    return rows


def print_table(rows, top=None, verbose=False):
    show = rows[:top] if top else rows
    print(f"{'#':>2} {'id':<20} {'档':<9} {'建议':<6} {'占用':>9}  说明")
    print("-" * 108)
    for i, r in enumerate(show, 1):
        size = "?" if r["size"] < 0 else (f"{r['size']/2**30:.2f}GB" if r["size"] > 2**20 else f"{r['size']/2**20:.1f}MB")
        flag = "⛔" if r["blocked"] else "  "
        print(f"{i:>2} {r['id']:<20} {r['klass']:<9} {r['advice']:<6} {size:>9} {flag}{r['note']}")
        if verbose and r["blocked_why"]:
            print(f"   ⛔ 阻止原因: {r['blocked_why']}")
        if verbose:
            print(f"   重建: {r['rebuild']}  代价: {r['cost']}")
            print(f"   路径: {r['path']}")


def cmd_apply(items, ids, yes: bool, include_approval=False) -> int:
    if not yes:
        print("拒绝执行：--apply 必须同时给 --yes（显式确认）")
        return 2
    sel = [it for it in items if (not ids or it["id"] in ids)]
    if not include_approval:
        sel = [it for it in sel if it["klass"] == "safe"]
    dig_before, _ = t0_digest()
    free_b0 = shutil.disk_usage("/System/Volumes/Data").free
    ts = time.strftime("%Y%m%d_%H%M%S")
    logp = os.path.join(TEMP, f"cache_reclaim_{ts}.json")
    res = []
    for it in sel:
        locked, why = _guard_of(it)
        if locked or it.get("run") is None:
            print(f"[skip] {it['id']}: {why or 'no-op'}")
            res.append(dict(id=it["id"], skipped=why or "no-op"))
            continue
        if not os.path.exists(it["path"]):
            print(f"[skip] {it['id']}: 路径不存在")
            continue
        b = du_b(it["path"])
        r = it["run"]()
        a = du_b(it["path"])
        r.update(id=it["id"], path=it["path"], klass=it["klass"], advice=it["advice"],
                 bytes_before=b, bytes_after=a, freed=b - a)
        res.append(r)
        print(f"[{it['id']}] {it['advice']} 前={b/2**30:.2f}GB 后={a/2**30:.2f}GB "
              f"实收={(b-a)/2**30:.2f}GB rc={r['rc']} {r['sec']}s")
        if r.get("out"):
            print("    out:", r["out"].replace("\n", " | ")[:200])
        if r.get("err"):
            print("    err:", r["err"].replace("\n", " | ")[:200])
    free_b1 = shutil.disk_usage("/System/Volumes/Data").free
    dig_after, _ = t0_digest()
    total = sum(max(0, r.get("freed", 0)) for r in res)
    summary = dict(ts=ts, mode="apply", t0_digest_before=dig_before, t0_digest_after=dig_after,
                   t0_unchanged=dig_before == dig_after,
                   free_before_gb=round(free_b0 / 2**30, 2), free_after_gb=round(free_b1 / 2**30, 2),
                   sum_freed_gb=round(total / 2**30, 2), items=res)
    json.dump(summary, open(logp, "w"), ensure_ascii=False, indent=1)
    print(f"\n=== 合计实收 {total/2**30:.2f}GB | 可用 {free_b0/2**30:.2f}GB → {free_b1/2**30:.2f}GB "
          f"(Δ{(free_b1-free_b0)/2**30:+.2f}GB) ===")
    print(f"=== T0 资产 2026... sha256 {'未变 ✅' if dig_before==dig_after else '已变 ❌'} "
          f"({dig_before[:12]}→{dig_after[:12]}) ===")
    print(f"=== 记账日志: {os.path.relpath(logp, ROOT)} ===")
    return 0 if dig_before == dig_after else 1


# ── 闭环探针：删→重建→可用 ─────────────────────────────────────────────────
def probe_playwright(keep=False) -> int:
    probe = os.path.join(TEMP, "pw_probe")
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=probe)
    out = dict(probe="playwright", browsers_path=probe)
    b0 = du_b(probe)
    if os.path.isdir(probe):
        shutil.rmtree(probe, ignore_errors=True)
    out["delete"] = dict(bytes_freed=b0)
    print(f"[probe] 删 {probe} 释放 {b0/2**20:.1f}MB")
    np = which("npx") or which("npm")
    t0 = time.time()
    p = subprocess.run([np, "--no-install", "playwright", "install", "chromium"],
                       capture_output=True, text=True, env=env, timeout=900)
    out["rebuild"] = dict(sec=round(time.time() - t0, 1), rc=p.returncode,
                          out=(p.stdout or "")[-400:], err=(p.stderr or "")[-300:])
    print(f"[probe] 重建 rc={p.returncode} {out['rebuild']['sec']}s  {(p.stdout or '')[-160:].strip()}")
    out["rebuild"]["bytes"] = du_b(probe)
    # 可用性①：playwright CLI 真起浏览器截图（不依赖 node 模块解析）
    shot = os.path.join(TEMP, f"pw_shot_{time.strftime('%Y%m%d_%H%M%S')}.png")
    t1 = time.time()
    u = subprocess.run([np, "--no-install", "playwright", "screenshot",
                        "--viewport-size=320,200", "about:blank", shot],
                       capture_output=True, text=True, env=env, timeout=240)
    png_ok = os.path.exists(shot) and os.path.getsize(shot) > 0
    out["usable"] = dict(mode="cli-screenshot", sec=round(time.time() - t1, 1), rc=u.returncode,
                         png=(os.path.basename(shot) if png_ok else None),
                         png_bytes=(os.path.getsize(shot) if png_ok else 0),
                         out=(u.stdout or "").strip()[-200:], err=(u.stderr or "").strip()[-300:])
    print(f"[probe] 可用性①(CLI截图) rc={u.returncode} {out['usable']['sec']}s "
          f"png={out['usable']['png_bytes']}B")
    # 可用性②（回落）：node + 显式 NODE_PATH 指向 npx 缓存里的 playwright 包
    if not (u.returncode == 0 and png_ok):
        npdirs = [d for d in glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules"))
                  if os.path.isdir(os.path.join(d, "playwright"))]
        env2 = dict(env, NODE_PATH=os.pathsep.join(npdirs))
        js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();"
              "const pg=await b.newPage();await pg.goto('https://example.com',{timeout:30000});"
              "console.log('TITLE='+await pg.title());await b.close();})()")
        jsf = os.path.join(TEMP, "pw_probe.js")
        open(jsf, "w").write(js)
        t2 = time.time()
        q = subprocess.run([which("node"), jsf], capture_output=True, text=True, env=env2, timeout=180)
        out["usable_fallback"] = dict(sec=round(time.time() - t2, 1), rc=q.returncode,
                                      out=(q.stdout or "").strip()[-200:], err=(q.stderr or "").strip()[-300:])
        print(f"[probe] 可用性②(回落 node) rc={q.returncode} {out['usable_fallback']['sec']}s "
              f"{out['usable_fallback']['out']}")
    ok_usable = (u.returncode == 0 and png_ok) or out.get("usable_fallback", {}).get("rc") == 0
    if not keep:
        sz = du_b(probe)
        shutil.rmtree(probe, ignore_errors=True)
        out["cleanup"] = dict(bytes_removed=sz)
        print(f"[probe] 清理探针目录，回收 {sz/2**20:.1f}MB")
    logp = os.path.join(TEMP, f"cache_reclaim_probe_playwright_{time.strftime('%Y%m%d_%H%M%S')}.json")
    json.dump(out, open(logp, "w"), ensure_ascii=False, indent=1)
    print(f"[probe] 日志: {os.path.relpath(logp, ROOT)}")
    return 0 if (out["usable"]["rc"] == 0 and "TITLE=" in out["usable"]["out"]) else 1


def probe_uv() -> int:
    out = dict(probe="uv")
    uv = which("uv")
    out["uv_bin"] = uv
    print(f"[probe] uv={uv}")
    lock = match = None
    for pat in ("uv tool", "uvx"):
        for line in pgrep(pat):
            pid = line.split()[0]
            l = sh(["/usr/sbin/lsof", "-p", pid], timeout=30)["out"]
            hit = [x for x in l.splitlines() if ".cache/uv/.lock" in x]
            if hit:
                match = l
                print(f"[probe] 发现常驻 {pat} pid={pid} 持锁: {hit[0].strip()}")
            else:
                print(f"[probe] 发现常驻 {pat} pid={pid}（未持 .lock）")
    if uv:
        t0 = time.time()
        r = sh([uv, "cache", "prune"], timeout=45)
        out["prune"] = dict(sec=round(time.time() - t0, 1), rc=r["rc"], timeout=r["timeout"], err=r["err"])
        print(f"[probe] uv cache prune rc={r['rc']} {out['prune']['sec']}s timeout={r['timeout']} {r['err'][:120]}")
    logp = os.path.join(TEMP, f"cache_reclaim_probe_uv_{time.strftime('%Y%m%d_%H%M%S')}.json")
    json.dump(out, open(logp, "w"), ensure_ascii=False, indent=1)
    print(f"[probe] 日志: {os.path.relpath(logp, ROOT)}（结论见报告：uv 路线被常驻服务锁死⇒需批准）")
    return 0


def probe_pnpm() -> int:
    """pnpm store 闭环（自包含隔离）：探索用 store 建→真删→重下→可用，不碰全局 store"""
    pnpm = which("pnpm") or f"{HOME}/Library/pnpm/pnpm"
    probe_store = "/tmp/pnpm_probe_store"
    proj = "/tmp/pnpm_probe_proj"
    out = dict(probe="pnpm", store=probe_store, pnpm=pnpm, pnpm_version=sh([pnpm, "--version"])["out"].strip())
    for d in (probe_store, proj):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(proj, exist_ok=True)
    open(os.path.join(proj, "package.json"), "w").write(
        '{"name":"probe","version":"1.0.0","private":true,"dependencies":{"left-pad":"1.3.0","nanoid":"3.3.4"}}')
    env = dict(os.environ, PNPM_HOME=f"{HOME}/Library/pnpm")
    t0 = time.time()
    p = subprocess.run([pnpm, "install", "--store-dir", probe_store, "--reporter=append-only"],
                       cwd=proj, capture_output=True, text=True, env=env, timeout=300)
    out["seed"] = dict(sec=round(time.time() - t0, 1), rc=p.returncode,
                       out=(p.stdout or "")[-300:], store_bytes=du_b(probe_store))
    print(f"[probe] 建隔离 store rc={p.returncode} {out['seed']['sec']}s store={out['seed']['store_bytes']/2**20:.1f}MB")
    b = du_b(probe_store)
    shutil.rmtree(probe_store, ignore_errors=True)
    shutil.rmtree(os.path.join(proj, "node_modules"), ignore_errors=True)
    out["delete"] = dict(bytes_freed=b, exists_after=os.path.exists(probe_store))
    print(f"[probe] 删 store 释放 {b/2**20:.1f}MB（存在性={os.path.exists(probe_store)}）")
    t1 = time.time()
    q = subprocess.run([pnpm, "install", "--store-dir", probe_store, "--reporter=append-only"],
                       cwd=proj, capture_output=True, text=True, env=env, timeout=300)
    txt = (q.stdout or "") + (q.stderr or "")
    dl = re.findall(r"downloaded (\d+)", txt)
    out["rebuild"] = dict(sec=round(time.time() - t1, 1), rc=q.returncode,
                          downloaded=dl[-1] if dl else "?", store_bytes=du_b(probe_store),
                          out=txt[-300:])
    print(f"[probe] 重建 rc={q.returncode} {out['rebuild']['sec']}s downloaded={out['rebuild']['downloaded']} "
          f"store={out['rebuild']['store_bytes']/2**20:.2f}MB")
    t2 = time.time()
    node = which("node")
    r = subprocess.run([node, "-e", "const lp=require('left-pad');const {nanoid}=require('nanoid');"
                                   "console.log('LEFT_PAD='+lp('7',3,'0')+' NANOID_LEN='+nanoid(8).length)"],
                       cwd=proj, capture_output=True, text=True, timeout=60)
    out["usable"] = dict(sec=round(time.time() - t2, 1), rc=r.returncode,
                         out=(r.stdout or "").strip(), err=(r.stderr or "")[-200:])
    print(f"[probe] 可用性 rc={r.returncode}: {out['usable']['out']} {out['usable']['err']}")
    ok = (q.returncode == 0 and r.returncode == 0 and "LEFT_PAD=007" in out["usable"]["out"]
          and out["rebuild"]["store_bytes"] > 0)
    for d in (probe_store, proj):
        shutil.rmtree(d, ignore_errors=True)
    out["verdict"] = "PASS" if ok else "FAIL"
    logp = os.path.join(TEMP, f"cache_reclaim_probe_pnpm_{time.strftime('%Y%m%d_%H%M%S')}.json")
    json.dump(out, open(logp, "w"), ensure_ascii=False, indent=1)
    print(f"[probe] 结论={out['verdict']} 日志: {os.path.relpath(logp, ROOT)}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="可再生缓存回收（默认 dry-run，绝不碰用户数据）")
    ap.add_argument("--all", action="store_true", help="列出全部候选（含保留项）")
    ap.add_argument("--top", type=int, default=10, help="dry-run 展示条数，默认 10")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印重建方式/代价/路径")
    ap.add_argument("--apply", action="store_true", help="执行回收（需 --yes）")
    ap.add_argument("--yes", action="store_true", help="显式确认")
    ap.add_argument("--only", default="", help="仅处理指定 id（逗号分隔）")
    ap.add_argument("--include-approval", action="store_true", help="连 approval 档也执行（默认只做 safe）")
    ap.add_argument("--verify", action="store_true", help="T0 资产 sha256 校验")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="T0 基线文件路径")
    ap.add_argument("--probe", choices=["uv", "playwright", "pnpm"], help="删→重建→可用 闭环实测")
    a = ap.parse_args()
    os.makedirs(TEMP, exist_ok=True)

    if a.verify:
        return cmd_verify(a.baseline)
    if a.probe == "playwright":
        return probe_playwright()
    if a.probe == "uv":
        return probe_uv()
    if a.probe == "pnpm":
        return probe_pnpm()
    if a.apply:
        return cmd_apply(ITEMS, set(x for x in a.only.split(",") if x), a.yes, a.include_approval)

    rows = scan(ITEMS)
    print(f"=== dry-run（只读，零副作用） T0 资产 sha256 汇总={t0_digest()[0][:12]}… ===")
    print(f"=== 磁盘: " + sh(["df", "-h", "/System/Volumes/Data"])["out"].splitlines()[-1].strip() + " ===")
    print_table(rows if a.all else rows[: a.top], None, a.verbose)
    safe = [r for r in rows if r["klass"] == "safe" and not r["blocked"]]
    print(f"\n可立即回收（safe 档，合计 {sum(max(0,r['size']) for r in safe)/2**30:.2f}GB）: "
          + ", ".join(f"{r['id']}({r['advice']})" for r in safe))
    ap_rows = [r for r in rows if r["klass"] == "approval"]
    print(f"需批准（approval 档，合计 {sum(max(0,r['size']) for r in ap_rows)/2**30:.2f}GB）: "
          + ", ".join(f"{r['id']}" for r in ap_rows))
    print("执行: --apply --yes [--only a,b]   （不加 --yes 一律拒绝）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
