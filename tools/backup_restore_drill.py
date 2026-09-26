#!/usr/bin/env python3
"""T0 关键资产「备份 → 恢复」抽样真演练（只读源 + 沙箱恢复）。

口径（重要，先读）：
  * 本工具**不制作备份**，也不推进任何「离机/异地目的地」待批项（R381 §7 / R418 D1-D3）。
  * 它做的只有一件事：拿**已经存在**的 T0 归档（默认 temp/t0_backups/*.tar.gz），
    从中抽 12 个成员恢复到沙箱（默认 /tmp），逐条做三重比对：
      ① 恢复副本 sha256  == 归档自带 .SHA256SUMS / manifest 记录的 sha256（备份自身完整性）
      ② 恢复副本 sha256  == 盘上活文件 sha256（备份是否落后于现场）
      ③ 恢复副本 sha256  == temp/t0_baseline.sha256 里的基线 sha256（现场是否已漂移）
    外加「读出内容首 200 字节」证明副本真的可用（不只是哈希对得上）。
  * 归档文件本身全程只读：开始/结束各算一次归档 sha256 并断言不变。

分层抽样（T0 层定义的现实约束）：
  L1（global_mem_insight.txt）与 L2（global_mem.txt）是**单文件层**，各只有 1 个成员，
  无法「各取 4」；因此本工具按 tier 配额 1(L1) + 1(L2) + 4(SOP/L3) + 6(L4 归档/报告/tools)
  取 12 个，并保证 L1/L2 **100% 覆盖**（1/1）。组合在 details.json 里逐条标注 tier。

用法：
  python3 tools/backup_restore_drill.py --selftest
  python3 tools/backup_restore_drill.py                      # 真演练，产物落 temp/backup_drill/<date>/
  python3 tools/backup_restore_drill.py --files memory/global_mem.txt ...   # 复跑固定集合
  python3 tools/backup_restore_drill.py --archive <tar.gz> --staging /tmp/x --out temp/backup_drill/2026-09-27
退出码：0=12/12 全部 RESTORED_OK；1=有失败条目；2=参数/环境错误。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GA_ROOT = os.path.dirname(HERE)
DEFAULT_ARCHIVE_GLOB_DIR = os.path.join(GA_ROOT, "temp", "t0_backups")
DEFAULT_BASELINE = os.path.join(GA_ROOT, "temp", "t0_baseline.sha256")
PREVIEW_BYTES = 200
SEED = 20260927

# ---- tier 配额（见文件头「分层抽样」） ----
TIER_QUOTA = [("L1", 1), ("L2", 1), ("L3", 4), ("OTHER", 6)]

STATUS_OK = "RESTORED_OK"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _split_sums_line(line: str):
    """'<sha>  <name>' 或 '<sha> *<name>' → (sha, name)；非法返回 None。"""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    if "  " in line:
        sha, name = line.split("  ", 1)
    elif " " in line:
        parts = line.split(None, 1)
        if len(parts) != 2:
            return None
        sha, name = parts
    else:
        return None
    name = name.lstrip("*").strip()
    if len(sha) != 64 or not name:
        return None
    return sha.lower(), name


def load_sums(path: str) -> dict:
    """读 .SHA256SUMS / manifest.json（后者由调用方转成同样结构）。"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = _split_sums_line(line)
            if rec:
                out[rec[1]] = rec[0]
    return out


def load_manifest_sums(path: str) -> dict:
    """t0_snapshot.py 产出的 .manifest.json → {arc_name: sha256}。"""
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return out
    for it in (m.get("files") or []):
        if isinstance(it, dict) and it.get("arc") and it.get("sha256"):
            out[it["arc"]] = str(it["sha256"]).lower()
    return out


def load_baseline(path: str) -> dict:
    """temp/t0_baseline.sha256 → {绝对路径: sha256}（格式 '<sha>  <abs path>'）。"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = _split_sums_line(line)
            if rec:
                out[rec[1]] = rec[0]
    return out


def safe_member_name(name: str) -> bool:
    """拒绝绝对路径 / 穿越 / 空名，防 tar 解包写出沙箱。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    n = name.replace("\\", "/")
    if n.startswith("../") or "/../" in n or n == "..":
        return False
    if os.path.isabs(n):
        return False
    return True


def preview_bytes(path: str, n: int = PREVIEW_BYTES) -> dict:
    """读前 n 字节；文本给原文（容错截断），二进制给 hex。

    坑：按字节截断会把多字节字符切成两半 → decode 抛错 → 误判「二进制」而回退 hex
    （实测中文文件 200 字节边界常命中，global_mem_insight.txt 被当二进制）。
    故先裁掉最多 3 个尾部字节再解码（UTF-8 单字符最多 4 字节）；含 NUL 才判二进制。
    """
    with open(path, "rb") as f:
        raw = f.read(n)
    if b"\x00" in raw:
        return {"bytes": len(raw), "is_text": False, "preview": raw.hex()}
    txt = None
    for trim in range(0, 4):
        try:
            txt = raw[: len(raw) - trim].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    if txt is None:
        return {"bytes": len(raw), "is_text": False, "preview": raw.hex()}
    return {"bytes": len(raw), "is_text": True, "preview": txt}


# --------------------------------------------------------------------------- #
# 抽样
# --------------------------------------------------------------------------- #
def tier_of(arc_name: str) -> str:
    if arc_name == "memory/global_mem_insight.txt":
        return "L1"
    if arc_name == "memory/global_mem.txt":
        return "L2"
    if arc_name.startswith("memory/") and arc_name.endswith(".md") and "/" not in arc_name[7:]:
        return "L3"
    return "OTHER"


def select_members(names, quota=None, seed=SEED, files=None):
    """确定性地抽 12 个成员（同 seed 同归档 → 同集合，可复跑）。"""
    names = [n for n in names if n and safe_member_name(n)]
    if files:
        picked = []
        for f in files:
            tier = tier_of(f)
            picked.append((tier, f) if f in names else (tier + "?", f))
        return picked
    quota = quota or TIER_QUOTA
    rnd = random.Random(seed)
    by_tier = {}
    for n in names:
        by_tier.setdefault(tier_of(n), []).append(n)
    for k in by_tier:
        by_tier[k].sort()
    picked = []
    used = set()
    for tier, k in quota:
        pool = [n for n in by_tier.get(tier, []) if n not in used]
        # L1/L2 单文件层：取全部（100% 覆盖），不足 k 不补
        take = pool if tier in ("L1", "L2") else rnd.sample(pool, min(k, len(pool)))
        for n in take:
            picked.append((tier, n))
            used.add(n)
    return picked


# --------------------------------------------------------------------------- #
# 单条校验
# --------------------------------------------------------------------------- #
def restore_one(tf: tarfile.TarFile, arc_name: str, staging: str, archive_sha: str,
                baseline_sha, live_path: str) -> dict:
    rec = {"arc": arc_name, "tier": tier_of(arc_name),
           "archive_sha256": archive_sha, "baseline_sha256": baseline_sha,
           "status": None, "reason": "", "errors": []}
    # 1) 抽取（只抽这一个成员，写到沙箱）
    dest = os.path.join(staging, arc_name)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        src = tf.extractfile(arc_name)
        if src is None:
            rec["status"], rec["reason"] = "EXTRACT_ERROR", "不是普通文件成员（目录/链接？）"
            return rec
        with open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    except KeyError:
        rec["status"], rec["reason"] = "MISSING_IN_ARCHIVE", "归档中不存在该成员"
        return rec
    except Exception as e:  # noqa: BLE001
        rec["status"], rec["reason"] = "EXTRACT_ERROR", f"{type(e).__name__}: {e}"
        return rec

    rec["restored_path"] = dest
    rec["restored_size"] = os.path.getsize(dest)
    rec["restored_sha256"] = sha256_file(dest)

    # 2) 读出内容首 200 字节（可用性判据，不只看哈希）
    try:
        if rec["restored_size"] == 0:
            rec["status"], rec["reason"] = "EMPTY", "恢复副本 0 字节"
            return rec
        rec["preview"] = preview_bytes(dest)
        if not rec["preview"]["preview"]:
            rec["status"], rec["reason"] = "EMPTY", "首 200 字节为空"
            return rec
    except Exception as e:  # noqa: BLE001
        rec["status"], rec["reason"] = "UNREADABLE", f"{type(e).__name__}: {e}"
        return rec

    # 3) 三重比对
    if archive_sha and rec["restored_sha256"] != archive_sha:
        rec["errors"].append("vs_archive_mismatch")
    if baseline_sha and rec["restored_sha256"] != baseline_sha:
        rec["errors"].append("vs_baseline_mismatch")
    live_sha = None
    if os.path.exists(live_path):
        try:
            live_sha = sha256_file(live_path)
        except Exception as e:  # noqa: BLE001
            rec["errors"].append(f"live_unreadable:{type(e).__name__}")
    rec["live_sha256"] = live_sha
    if live_sha and rec["restored_sha256"] != live_sha:
        rec["errors"].append("vs_live_mismatch")

    if "vs_archive_mismatch" in rec["errors"]:
        rec["status"], rec["reason"] = "RESTORED_MISMATCH", "恢复副本与归档记录 sha256 不一致"
    else:
        rec["status"], rec["reason"] = STATUS_OK, "副本 sha256 与归档记录一致且可读"
    rec["live_axis"] = _live_axis(live_sha, baseline_sha, rec)
    return rec


def _live_axis(live_sha, baseline_sha, rec) -> str:
    if live_sha is None:
        return "LIVE_MISSING"          # 盘上已无该文件
    if baseline_sha is None:
        return "NO_BASELINE"           # T0 清单里没有它
    if live_sha != baseline_sha:
        return "LIVE_DRIFT"            # 现场已改动，基线过期
    if rec.get("restored_sha256") and rec["restored_sha256"] != live_sha:
        return "BACKUP_BEHIND"         # 备份落后于现场（现场已更新）
    return "LIVE_SAME"


# --------------------------------------------------------------------------- #
# 演练主体
# --------------------------------------------------------------------------- #
def pick_default_archive() -> str:
    if not os.path.isdir(DEFAULT_ARCHIVE_GLOB_DIR):
        return ""
    cands = [os.path.join(DEFAULT_ARCHIVE_GLOB_DIR, f)
             for f in os.listdir(DEFAULT_ARCHIVE_GLOB_DIR)
             if f.endswith(".tar.gz")]
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0] if cands else ""


def run_drill(archive: str, baseline_path: str, staging: str, out_dir: str,
              files=None, sample=12, quiet=False, root: str = None) -> dict:
    t0 = time.time()
    root = root or GA_ROOT
    res = {"archive": archive, "baseline": baseline_path, "staging": staging, "root": root,
           "out_dir": out_dir, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "counts": {}, "items": [], "findings": []}
    if not archive or not os.path.exists(archive):
        res["error"] = f"归档不存在: {archive}"
        return res

    archive_sha_before = sha256_file(archive)
    with tarfile.open(archive, "r:gz") as tf:
        names = [n for n in tf.getnames() if safe_member_name(n)]
        picked = select_members(tf.getnames(), files=files, seed=SEED)
        if not files and sample and len(picked) > sample:
            picked = picked[:sample]
        sums = load_sums(archive + ".SHA256SUMS")
        sums.update({k: v for k, v in load_manifest_sums(archive + ".manifest.json").items()
                     if k not in sums})
        baseline = load_baseline(baseline_path)
        os.makedirs(staging, exist_ok=True)
        for tier, arc_name in picked:
            live_path = os.path.join(root, arc_name)
            rec = restore_one(tf, arc_name, staging, sums.get(arc_name),
                              baseline.get(live_path), live_path)
            rec["tier"] = tier
            res["items"].append(rec)
    archive_sha_after = sha256_file(archive)
    res["archive_sha256"] = archive_sha_before
    res["archive_unchanged"] = archive_sha_before == archive_sha_after

    counts = {}
    for r in res["items"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    res["counts"] = counts
    res["total"] = len(res["items"])
    res["restorable"] = counts.get(STATUS_OK, 0)
    res["seconds"] = round(time.time() - t0, 2)

    # 旁证轴 1：归档记录缺失（无法判「与备份一致」）
    res["no_archive_record"] = [r["arc"] for r in res["items"] if not r["archive_sha256"]]
    # 旁证轴 2：现场侧异常（盘上丢失 / 已漂移 / 备份落后）
    res["live_issues"] = {r["arc"]: r.get("live_axis") for r in res["items"]
                          if r.get("live_axis") in ("LIVE_MISSING", "LIVE_DRIFT", "BACKUP_BEHIND")}
    # 旁证轴 3：T0 清单里被引用但盘上已无的条目（本次只做统计，不参与 12 条抽样）
    lost = sorted(p for p in baseline if not os.path.exists(p))
    res["baseline_refs_missing_on_disk"] = {"n": len(lost), "sample": lost[:10]}

    # 产物：manifest.sha256（临时清单，格式同 T0 基线）
    manifest_lines = []
    for r in res["items"]:
        if r.get("restored_sha256") and r.get("restored_path"):
            manifest_lines.append(f"{r['restored_sha256']}  {r['restored_path']}")
    res["manifest_sha256_lines"] = len(manifest_lines)

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "manifest.sha256"), "w", encoding="utf-8") as f:
            f.write("\n".join(manifest_lines) + ("\n" if manifest_lines else ""))
        with open(os.path.join(out_dir, "details.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(staging, "manifest.sha256"), "w", encoding="utf-8") as f:
            f.write("\n".join(manifest_lines) + ("\n" if manifest_lines else ""))

    if not quiet:
        print_summary(res)
    return res


def print_summary(res: dict) -> None:
    print(f"[归档] {res['archive']}")
    print(f"[基线] {res['baseline']}  [沙箱] {res['staging']}")
    print(f"[只读自证] 归档 sha256 不变: {res.get('archive_unchanged')} "
          f"({res.get('archive_sha256','')[:16]}…)")
    print(f"[可恢复] {res.get('restorable')}/{res.get('total')}  ({res.get('seconds')}s)")
    for r in res["items"]:
        ok = "OK  " if r["status"] == STATUS_OK else "FAIL"
        note = ""
        if r.get("live_axis") and r["live_axis"] not in ("LIVE_SAME",):
            note = f"  live={r['live_axis']}"
        if r["errors"]:
            note += "  " + ",".join(r["errors"])
        print(f"  {ok} [{r['tier']:<5}] {r['arc']}  ({r.get('restored_size','-')} B){note}"
              + (f"  ← {r['reason']}" if r["status"] != STATUS_OK else ""))
    print(f"[状态计数] {res.get('counts')}")
    if res.get("findings"):
        for f in res["findings"]:
            print(f"[发现] {f}")


# --------------------------------------------------------------------------- #
# 自测（含负控）
# --------------------------------------------------------------------------- #
def _mk_fixture(tmp: str) -> str:
    """造一个夹具归档：正常成员 + 空成员 + 一个「SHA256SUMS 之后被篡改」的成员。"""
    arc = os.path.join(tmp, "fixture.tar.gz")
    files = {
        "memory/global_mem_insight.txt": b"# L1 index line one\n",
        "memory/global_mem.txt": b"facts: alpha beta gamma\n",
        "memory/some_sop.md": b"# SOP\nstep 1\nstep 2\n",
        "memory/empty_file.txt": b"",
        "tools/tool_a.py": b"print('a')\n",
    }
    with tarfile.open(arc, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    # SHA256SUMS：先按真实内容算，再篡改其中两条的期望值（负控）
    sums = {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}
    lines = [f"{sums['memory/global_mem_insight.txt']}  memory/global_mem_insight.txt",
             f"{'0'*64}  memory/global_mem.txt",           # 负控 A：期望 sha 错 → MISMATCH
             f"{sums['memory/some_sop.md']}  memory/some_sop.md",
             f"{sums['memory/empty_file.txt']}  memory/empty_file.txt",
             f"{sums['tools/tool_a.py']}  tools/tool_a.py"]
    with open(arc + ".SHA256SUMS", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return arc


def selftest() -> int:
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))
        print(("  PASS " if cond else "  FAIL ") + name)

    print("== selftest ==")
    with tempfile.TemporaryDirectory() as tmp:
        arc = _mk_fixture(tmp)
        live = os.path.join(tmp, "live")
        os.makedirs(os.path.join(live, "memory"), exist_ok=True)
        # 「活文件」：与归档同内容，制造 LIVE_SAME
        pairs = {"memory/global_mem_insight.txt": b"# L1 index line one\n",
                 "memory/some_sop.md": b"# SOP\nstep 1\nstep 2\n",
                 "memory/global_mem.txt": b"facts: alpha beta gamma\n"}
        for n, d in pairs.items():
            p = os.path.join(live, n)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(d)
        baseline = os.path.join(tmp, "baseline.sha256")
        with open(baseline, "w", encoding="utf-8") as f:
            f.write(f"{hashlib.sha256(pairs['memory/global_mem_insight.txt']).hexdigest()}  "
                    f"{os.path.join(live,'memory/global_mem_insight.txt')}\n")
            f.write(f"{'1'*64}  {os.path.join(live,'memory/global_mem.txt')}\n")   # 负控 B：基线漂移
            f.write(f"{hashlib.sha256(pairs['memory/some_sop.md']).hexdigest()}  "
                    f"{os.path.join(live,'memory/some_sop.md')}\n")
            f.write(f"{'2'*64}  {os.path.join(live,'memory/ghost_file.txt')}\n")   # 负控 C：盘上不存在

        out = os.path.join(tmp, "out")
        stg = os.path.join(tmp, "stg")
        res = run_drill(arc, baseline, stg, out, quiet=True, root=live)
        by = {r["arc"]: r for r in res["items"]}

        chk("真读到了归档（成员 >= 5）", len(res["items"]) >= 5)
        chk("正常成员判 RESTORED_OK", by["memory/global_mem_insight.txt"]["status"] == STATUS_OK)
        chk("可读内容非空（首 200 字节）", bool(by["memory/global_mem_insight.txt"]["preview"]["preview"]))
        chk("负控A 篡改归档期望 sha → RESTORED_MISMATCH",
            by["memory/global_mem.txt"]["status"] == "RESTORED_MISMATCH")
        chk("空成员 → EMPTY", by["memory/empty_file.txt"]["status"] == "EMPTY")
        chk("负控B 基线 sha 不符 → LIVE_DRIFT",
            by["memory/global_mem.txt"].get("live_axis") == "LIVE_DRIFT")
        chk("活文件与归档同 → LIVE_SAME",
            by["memory/global_mem_insight.txt"].get("live_axis") == "LIVE_SAME")
        chk("归档只读自证（sha256 前后一致）", res["archive_unchanged"] is True)
        chk("负控C 基线引用盘上不存在 → 计入 baseline_refs_missing_on_disk",
            res["baseline_refs_missing_on_disk"]["n"] == 1)
        with_sha = sum(1 for r in res["items"] if r.get("restored_sha256"))
        # 夹具 5 个成员：3 个 RESTORED_OK（insight/some_sop/tool_a）+1 MISMATCH+1 EMPTY
        chk("manifest.sha256 落盘且行数==有恢复副本的条目数",
            os.path.exists(os.path.join(out, "manifest.sha256"))
            and res["manifest_sha256_lines"] == with_sha == 5
            and res["restorable"] == 3 and res["total"] == 5)
        chk("失败条目未计入可恢复数（MISMATCH/EMPTY 如实计数）",
            res["counts"].get("RESTORED_MISMATCH") == 1 and res["counts"].get("EMPTY") == 1)
        chk("沙箱落点在临时目录下（未写用户目录）",
            stg.startswith("/tmp") or stg.startswith(tempfile.gettempdir()))

        # 负控 D：缺成员
        res2 = run_drill(arc, baseline, stg + "2", out + "2",
                         files=["memory/not_in_archive.md"], quiet=True, root=live)
        chk("负控D 成员不存在 → MISSING_IN_ARCHIVE",
            res2["items"][0]["status"] == "MISSING_IN_ARCHIVE")

        # 负控 E：路径穿越必须被拒（不参与抽取）
        chk("负控E 绝对/穿越路径被拒",
            (not safe_member_name("/etc/passwd")) and (not safe_member_name("../x"))
            and (not safe_member_name("a/../../b")) and safe_member_name("memory/a.md"))

        # 负控 F：归档不存在 → 明确报错而非假绿
        res3 = run_drill(os.path.join(tmp, "nope.tar.gz"), baseline, stg + "3", "", quiet=True, root=live)
        chk("负控F 归档缺失 → error 且 restorable=0",
            bool(res3.get("error")) and res3.get("restorable", 0) == 0)

        # 负控 G：manifest/sums 都缺 → 标注 no_archive_record（不判 OK 也不判 MISMATCH）
        arc2 = os.path.join(tmp, "noSums.tar.gz")
        with tarfile.open(arc2, "w:gz") as tf:
            info = tarfile.TarInfo("memory/x.md"); data = b"hello\n"; info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        res4 = run_drill(arc2, baseline, stg + "4", "", quiet=True, root=live)
        chk("负控G 无归档记录 → 进入 no_archive_record 且仍可判 OK",
            res4["no_archive_record"] == ["memory/x.md"]
            and res4["items"][0]["status"] == STATUS_OK)

        # 负控H/I：200 字节截断落在多字节字符中间时不得误判二进制（曾把中文文件打成 hex）
        mb = os.path.join(tmp, "mb.txt")
        with open(mb, "wb") as f:
            f.write("中".encode("utf-8") * 100)          # 每字 3 字节：200 = 66*3+2 → 切在第 67 字中间
        pv = preview_bytes(mb, 200)
        chk("负控H 多字节边界仍判文本（不落 hex）",
            pv["is_text"] is True and "中" in pv["preview"] and len(pv["preview"]) == 66)
        binp = os.path.join(tmp, "bin.dat")
        with open(binp, "wb") as f:
            f.write(b"AB\x00CD" * 40)
        pv2 = preview_bytes(binp, 200)
        chk("负控I 含 NUL 判二进制（给 hex）",
            pv2["is_text"] is False and all(c in "0123456789abcdef" for c in pv2["preview"][:40]))

    bad = [n for n, ok in checks if not ok]
    print(f"== selftest: {len(checks)-len(bad)}/{len(checks)} passed ==")
    return 0 if not bad else 1


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T0 备份→恢复抽样真演练（只读源 + 沙箱恢复）")
    ap.add_argument("--archive", default="", help="T0 归档（默认取 temp/t0_backups 最新 *.tar.gz）")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--staging", default="", help="恢复沙箱（默认 /tmp/ga_backup_drill_<date>）")
    ap.add_argument("--out", default="", help="产物目录（默认 temp/backup_drill/<date>/）")
    ap.add_argument("--files", nargs="*", default=None, help="复跑固定成员集合")
    ap.add_argument("--root", default=GA_ROOT, help="活文件根（默认 GA 仓库根；自测用夹具根）")
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    date = time.strftime("%Y-%m-%d")
    archive = args.archive or pick_default_archive()
    # 默认沙箱：显式 /tmp（TODO 口径「恢复副本落 /tmp」；不写用户目录/家目录）
    default_tmp = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) \
        else tempfile.gettempdir()
    staging = args.staging or os.path.join(default_tmp, f"ga_backup_drill_{date}")
    out = args.out or os.path.join(GA_ROOT, "temp", "backup_drill", date)
    res = run_drill(archive, args.baseline, staging, out, files=args.files,
                    sample=args.sample, quiet=args.json, root=args.root)
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if k != "items"}, ensure_ascii=False))
    if res.get("error"):
        return 2
    return 0 if res.get("restorable") == res.get("total") and res.get("total") else 1


if __name__ == "__main__":
    sys.exit(main())
