#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待批台账「第二批增量」前提复测器（C19–C47 / TODO L175 / 第19批 C6）。

目的（收窄口径，三路评审）
  台账 temp/pending_decisions/DECISIONS.md 行 71–99（C19–C47，29 条）是在 09-22/09-23
  写下的。今天的现实是否还支撑这些声明？本器**只回答两件事**：
    · 关闭：前提已消失（已被别的次任务做掉 / 目标已不存在 / 环境已变）
    · 重写：意图仍在，但声明里的目标/落点/输入已过期，需要改写后才谈拍板
  本器**不产出「可批准」清单**（那是人拍板的事），**不改台账、不代批、不写任何被扫文件**。

与既有件分工
  R403 premise_recheck（temp/pending_decisions/）  只测 apply.py **声明面**的 18 条（可执行项）
  R433 pending_evidence（tools/）                 逐条给**批准前证据预览**（会写什么/可逆性）
  R453 pending_cost_sheet（tools/）               抽样 5 条给**代价/代价形态**
  本器                                            C19–C47 **全量 29 条**的**前提三态判定**（关闭/重写/前提仍成立）
  复用：台账行解析直接 import pending_evidence.parse_ledgers（零重写解析内核）。

三态
  CLOSE    前提消失 ⇒ 建议关闭（附证据）
  REWRITE  声明过期 ⇒ 建议重写后再拍板（附证据+建议改法）
  ACTIVE   前提经复测仍成立 ⇒ 仍需人拍板（不代批）
  MANUAL   无机器可判前提（价值取舍/外部账号/需求确认）⇒ 单列「需人工」+理由

用法
  python3 tools/decisions_premise_recheck.py                      # 人读表格（root=仓库根）
  python3 tools/decisions_premise_recheck.py --json temp/x.json    # 机器可读
  python3 tools/decisions_premise_recheck.py --root /tmp/sb        # 沙盒树（自测用）
  python3 tools/decisions_premise_recheck.py --only C21,C34,C36
  python3 tools/decisions_premise_recheck.py --readonly-proof      # 台账 sha256 前后自证
  python3 tools/decisions_premise_recheck.py --selftest            # 负控自测（不碰真树）
"""
import argparse
import glob as globmod
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LEDGER_DIR = os.path.join(REPO, "temp", "pending_decisions")

MB = 1024 * 1024
GB = 1024 * 1024 * 1024

# ---------------------------------------------------------------- 探针

def _p(root, rel):
    if rel.startswith("~"):
        return os.path.expanduser(rel)
    return os.path.join(root, rel)


def pr_file_contains(root, rel, pat):
    path = _p(root, rel)
    if not os.path.isfile(path):
        return False, "%s 不存在" % rel
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:                                  # noqa: BLE001
        return False, "%s 读取失败:%s" % (rel, e)
    hit = pat in txt
    return hit, "%s %s %r（%dB）" % (rel, "含" if hit else "不含", pat, len(txt.encode()))


def pr_file_exists(root, rel):
    path = _p(root, rel)
    ok = os.path.exists(path)
    return ok, "%s %s" % (rel, "存在" if ok else "缺失")


def pr_glob_count(root, pattern):
    hits = [h for h in globmod.glob(os.path.join(root, pattern), recursive=True) if os.path.isfile(h)]
    return len(hits), "%s ⇒ %d 个文件 %s" % (pattern, len(hits), [os.path.relpath(h, root) for h in hits[:3]])


def pr_dir_bytes(root, rel):
    path = _p(root, rel)
    if not os.path.exists(path):
        return 0, "%s 不存在" % rel
    try:
        out = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=120).stdout.split()
        kb = int(out[0])
    except Exception as e:                                  # noqa: BLE001
        return 0, "%s du 失败:%s" % (rel, e)
    return kb * 1024, "%s = %.2f GiB" % (rel, kb * 1024 / GB)


_BIN_CAND = {
    "lsof": ["/usr/sbin/lsof", "/usr/bin/lsof"],
    "pgrep": ["/usr/bin/pgrep", "/bin/pgrep"],
}


def _bin(name):
    """PATH 可能不含 /usr/sbin ⇒ 按候选绝对路径解析（本机实测 which lsof 为空）。"""
    for c in _BIN_CAND.get(name, []):
        if os.path.exists(c):
            return c
    return name


def pr_port_listen(root, port):
    try:
        out = subprocess.run([_bin("lsof"), "-nP", "-iTCP:%d" % int(port), "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception as e:                                  # noqa: BLE001
        return False, "lsof 失败:%s" % e
    ok = bool(out.strip())
    return ok, "port %s %s" % (port, "有监听" if ok else "无监听")


def pr_proc_absent(root, name):
    try:
        out = subprocess.run([_bin("pgrep"), "-fl", name], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:                                  # noqa: BLE001
        return False, "pgrep 失败:%s" % e
    return (not out), "proc %r %s" % (name, "运行中:" + out.split("\n")[0][:60] if out else "不存在")


def pr_json_key(root, rel, key):
    path = _p(root, rel)
    if not os.path.isfile(path):
        return None, "%s 不存在" % rel
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:                                  # noqa: BLE001
        return None, "%s JSON 解析失败:%s" % (rel, e)
    v = d.get(key)
    return v, "%s[%r] = %r" % (rel, key, v)


def pr_keychain_absent(root, service):
    try:
        rc = subprocess.run(["security", "find-generic-password", "-s", service],
                            capture_output=True, text=True, timeout=30).returncode
    except Exception as e:                                  # noqa: BLE001
        return False, "security 失败:%s" % e
    return rc != 0, "keychain %r %s" % (service, "缺失" if rc != 0 else "存在")


PROBES = {
    "file_contains": pr_file_contains,
    "file_exists": pr_file_exists,
    "glob_count": pr_glob_count,
    "dir_bytes": pr_dir_bytes,
    "port_listen": pr_port_listen,
    "proc_absent": pr_proc_absent,
    "json_key": pr_json_key,
    "keychain_absent": pr_keychain_absent,
}

# ---------------------------------------------------------------- 项表（写死，见报告 §4 逐条依据）

SPEC = [
    # id, 目标(台账原文摘要), 主探针, 真→, 假→, 备注/证据探针
    dict(id="C19", target="memory/autonomous_operation_sop/helper.py L29",
         goal="日志压缩脚本改指 tools/log_compress.py",
         probe=("file_contains", "memory/autonomous_operation_sop/helper.py", "tools/log_compress"),
         true="CLOSE",
         else_probe=("file_contains", "memory/autonomous_operation_sop/helper.py", "compress_log"),
         else_true="ACTIVE", else_false="REWRITE",
         ev=[("file_contains", "memory/autonomous_operation_sop/helper.py", "compress_log.py")],
         why={"CLOSE": "helper 已指向 tools/log_compress.py ⇒ 已应用",
              "ACTIVE": "helper 仍引用 compress_log.py（经 _TEMP_DIR 拼接，故不出现 'temp/' 字面）⇒ 前提成立",
              "REWRITE": "helper 既不指 tools/log_compress.py 也不提 compress_log.py ⇒ 声明已过期，须重写"}),
    dict(id="C20", target="sche_tasks/run_trendradar_digest.py",
         goal="平台检索通道拆词修复 + limit 30→≥80",
         probe=("file_contains", "sche_tasks/run_trendradar_digest.py", "逐词"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_contains", "sche_tasks/run_trendradar_digest.py", "\"limit\": 30")],
         why={"CLOSE": "脚本已含逐词并集 ⇒ 已应用",
              "ACTIVE": "脚本仍无逐词逻辑 ⇒ 前提成立"}),
    dict(id="C21", target="tools/audio_asset_inventory.py",
         goal="口径不一致：JSON 32 / MD 30 → 统一为剔除非媒体口径",
         probe=("file_contains", "tools/audio_asset_inventory.py", "非媒体"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_contains", "tools/audio_asset_inventory.py", "unique_incl_not_media")],
         why={"CLOSE": "工具已含非媒体口径并双口径输出 ⇒ 已应用（创建提交 a8053ea 即含）",
              "ACTIVE": "双口径仍缺 ⇒ 前提成立"}),
    dict(id="C22", target="memory/autonomous_operation_sop/helper.py",
         goal="编号复用根治（两树 max+1 + 号码级 O_EXCL claim 锁 + 收尾收敛）",
         probe=("file_contains", "memory/autonomous_operation_sop/helper.py", "O_EXCL"),
         true="CLOSE", false="ACTIVE",
         ev=[("glob_count", "temp/R355*")],
         why={"CLOSE": "helper 已含 O_EXCL claim ⇒ 已应用",
              "ACTIVE": "helper 无 claim 锁（临时补丁仍在 temp/）⇒ 前提成立"}),
    dict(id="C23", target="tools/（新入库 2 文件）",
         goal="夹具 temp/rnum_fixture.py + temp/R355_apply_patch.sh 迁入仓库",
         probe=("file_exists", "tools/rnum_fixture.py"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_exists", "temp/rnum_fixture.py"), ("file_exists", "temp/R355_apply_patch.sh")],
         why={"CLOSE": "夹具已在 tools/ ⇒ 已应用",
              "ACTIVE": "夹具仍只在 temp/ ⇒ 前提成立"}),
    dict(id="C24", target="memory/global_mem_insight.txt + memory/global_mem.txt",
         goal="记忆提案：L1 补消费者论证句 + L2 新建「本机存储与灾备事实」",
         probe=("file_contains", "memory/global_mem.txt", "本机存储与灾备"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_contains", "memory/global_mem_insight.txt", "消费者论证")],
         why={"CLOSE": "L2 已有该小节 ⇒ 已应用",
              "ACTIVE": "L1 句/L2 小节仍缺 ⇒ 前提成立"}),
    dict(id="C25", target="RULES #15（GA 规则文本）",
         goal="RULES #15 订正：本机无 setsid，改为 nohup </dev/null&disown",
         probe=("file_contains", "memory/global_mem_insight.txt", "本机 `which setsid`=NOT FOUND"),
         true="CLOSE", false="ACTIVE",
         ev=[("glob_count", "temp/user_prompt_*.md")],
         why={"CLOSE": "订正口径已落 L1（运行期注入载体）⇒ 已应用",
              "ACTIVE": "L1 无此订正 ⇒ 前提成立"}),
    dict(id="C26", target="硬件（钱包决策）", manual="是否购置外置盘做 Time Machine（花费决策，无机器判据）"),
    dict(id="C27", target="T0 冷备介质", manual="U 盘 / 加密上传 / 两者（介质二选一，需人拍板）"),
    dict(id="C28", target="tools/ga_backup.py（新建）",
         goal="把 R358 备份命令固化为脚本",
         probe=("file_exists", "tools/ga_backup.py"),
         true="CLOSE", false=None,
         else_probe=("file_exists", "tools/t0_snapshot.py"), else_true="REWRITE", else_false="ACTIVE",
         why={"CLOSE": "ga_backup.py 已存在 ⇒ 已应用",
              "REWRITE": "ga_backup.py 缺，但 tools/t0_snapshot.py 已承接 build/verify/drill ⇒ 声明应改指向该工具",
              "ACTIVE": "无任何承接件 ⇒ 前提成立"}),
    dict(id="C29", target="memory/backup_gap_sop.md（新建）",
         goal="新 L3 SOP：资产分层 T0–T3 / 演练口径 / 加密链路",
         probe=("file_exists", "memory/backup_gap_sop.md"),
         true="CLOSE", false=None,
         else_probe=("glob_count", "temp/pending_decisions/R358*"), else_true="ACTIVE", else_false=None,
         else2_probe=("file_exists", "tools/t0_snapshot.py"), else2_true="REWRITE", else2_false="ACTIVE",
         why={"CLOSE": "SOP 已存在 ⇒ 已应用",
              "REWRITE": "SOP 缺；R358 原文要点已不在盘（temp 被清）但 tools/t0_snapshot.py 已承载口径 ⇒ 声明应改指向既有载体",
              "ACTIVE": "无承接件 ⇒ 前提成立"}),
    dict(id="C30", target="logs/（4 个日志）",
         goal="日志体量治理：launch_gui.log 归档后删原文（回收 ~1.09GiB）等",
         probe=("dir_bytes", "logs"),
         bands=[(2 * GB, ">=", "ACTIVE"), (2 * GB, "<", "CLOSE")],
         ev=[("file_exists", "logs/launch_gui.log")],
         why={"ACTIVE": "logs/ ≥2GiB，回收前提成立", "CLOSE": "logs/ 已瘦到 <2GiB ⇒ 已应用"}),
    dict(id="C31", target="tools/log_compress.py 参数",
         goal="归档保留策略（现「>8MiB 才压、保留最近 1MiB」）",
         probe=("file_contains", "tools/log_compress.py", "1 * 1024 * 1024"),
         true="ACTIVE", false="ACTIVE",
         why={"ACTIVE": "保留策略仍为默认值 ⇒ 属价值取舍，仍需人拍板"}),
    dict(id="C32", target="temp/local_llm/（7 文件）",
         goal="本地推理产线落位：temp/local_llm/ → tools/",
         probe=("file_exists", "tools/local_llm"),
         true="CLOSE", false="ACTIVE",
         ev=[("glob_count", "temp/local_llm/*")],
         why={"CLOSE": "已落位 tools/ ⇒ 已应用", "ACTIVE": "仍在 temp/（清理即灭失）⇒ 前提成立"}),
    dict(id="C33", target="sche_tasks/run_trendradar_digest.py",
         goal="trendradar 预筛接入本地 1.5B：先影子模式",
         probe=("file_contains", "sche_tasks/run_trendradar_digest.py", "SHADOW"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_contains", "sche_tasks/run_trendradar_digest.py", "shadow")],
         why={"CLOSE": "已有影子分支 ⇒ 已应用", "ACTIVE": "无影子分支 ⇒ 前提成立"}),
    dict(id="C34", target="birdie_monitor/haisou_daily.py（is_relevant）",
         goal="haisou 假阳性修复（正则形态过滤/词边界）",
         probe=("file_contains", "birdie_monitor/haisou_daily.py", "haisou_relevance"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_exists", "tools/haisou_relevance.py")],
         why={"CLOSE": "已改引单一源 tools/haisou_relevance.py（R441）⇒ 已应用",
              "ACTIVE": "判定函数仍为内联旧口径 ⇒ 前提成立"}),
    dict(id="C35", target="~/models/gguf/（3B 权重 2.0G）",
         goal="是否删除 3B 权重",
         probe=("dir_bytes", "~/models/gguf"),
         bands=[(1 * GB, ">=", "ACTIVE"), (1 * GB, "<", "CLOSE")],
         why={"ACTIVE": "3B 权重仍在盘 ⇒ 仍需人拍板（需重下载才能恢复）", "CLOSE": "已删除 ⇒ 已应用"}),
    dict(id="C36", target="llama-server 1.5B 常驻进程（port 18080）",
         goal="是否停掉常驻",
         probe=("port_listen", 18080),
         true="ACTIVE", false=None,
         else_probe=("proc_absent", "llama-server"), else_true="CLOSE", else_false="REWRITE",
         why={"ACTIVE": "端口仍有监听 ⇒ 仍需人拍板",
              "CLOSE": "既无监听也无进程 ⇒ 常驻已不在，前提消失（声明里的 stop 命令亦已无对象）",
              "REWRITE": "端口无监听但进程存在 ⇒ 声明里的端口/停法需重写"}),
    dict(id="C37", target="sche_tasks/x_ai_intel_daily.json + eia_weekly.json + reflect_scripts/07_*",
         goal="静默失败修复包应用（PRECHECK_SKIP_NO_X_TAB / DEGRADED_NO_EIA_KEY）",
         probe=("file_contains", "sche_tasks/x_ai_intel_daily.json", "PRECHECK_SKIP_NO_X_TAB"),
         true="CLOSE", false="ACTIVE",
         ev=[("file_contains", "sche_tasks/eia_weekly.json", "DEGRADED_NO_EIA_KEY")],
         why={"CLOSE": "标记已注入 ⇒ 已应用", "ACTIVE": "标记缺失 ⇒ 前提成立"}),
    dict(id="C38", target="定时任务 x_ai_intel_daily",
         goal="是否停用或归档该任务",
         probe=("json_key", "sche_tasks/x_ai_intel_daily.json", "enabled"),
         true="ACTIVE", false="CLOSE",
         why={"ACTIVE": "任务仍 enabled ⇒ 仍需人拍板", "CLOSE": "已停用/归档 ⇒ 已应用"}),
    dict(id="C39", target="EIA_API_KEY（外部密钥）",
         goal="补 key 或砍掉 eia 的 EIA 段",
         probe=("keychain_absent", "EIA_API_KEY"),
         true="ACTIVE", false="CLOSE",
         why={"ACTIVE": "keychain 无该 key ⇒ 仍需人提供或批准删段", "CLOSE": "key 已入 keychain ⇒ 已应用"}),
    dict(id="C40", target="新反射器脚本（额度看板）", manual="需人先定登录态来源（浏览器会话 / 用户 token）"),
    dict(id="C41", target="reflect_scripts/08_kc_snapshot.py 或 07 守卫",
         goal="Chrome History 每日快照（合并批准，复用 07 的 30min launchd）",
         probe=("glob_count", "sche_tasks/*rolling*"),
         bands=[(1, ">=", "CLOSE"), (0, "==", None)],
         else_probe=("file_exists", "tools/rolling_source_snapshot.py"), else_true="REWRITE", else_false="ACTIVE",
         ev=[("file_exists", "tools/rolling_source_snapshot.py"), ("file_exists", "sche_tasks/sqlite_snapshot.json")],
         why={"CLOSE": "已有调度条目 ⇒ 已应用",
              "REWRITE": "工具 tools/rolling_source_snapshot.py 已存在（R415/R420/R445）但无任何调度 ⇒ 原声明「实现快照」前提过期，应重写为「接线」（=R430 D6）",
              "ACTIVE": "工具与调度皆无 ⇒ 前提成立"}),
    dict(id="C42", target="sources.json",
         goal="追加监控源（13 候选；评审判定：先证「频次非截断假象」再挑）",
         probe=("file_exists", "memory/domain_gap/COLLECTION_CANDIDATES.md"),
         true="REWRITE", false="ACTIVE",
         ev=[("glob_count", "temp/**/source_candidates*")],
         why={"REWRITE": "「先证频次非截断」已由 R423/R430/R438 完成（候选清单已落 memory/domain_gap/COLLECTION_CANDIDATES.md）⇒ 声明应重写为「挑项目」",
              "ACTIVE": "候选清单仍缺 ⇒ 前提成立"}),
    dict(id="C43", target="新快照产物（smyze.cn 运营指标）", manual="需人确认需求是否真存在"),
    dict(id="C44", target="~/Library/Caches/JetBrains/*",
         goal="清理旧版缓存 ~11–13G",
         probe=("dir_bytes", "~/Library/Caches/JetBrains"),
         bands=[(5 * GB, ">=", "ACTIVE"), (5 * GB, "<", "CLOSE")],
         why={"ACTIVE": "缓存仍 ≥5GiB ⇒ 仍需人批准", "CLOSE": "缓存已瘦 ⇒ 已应用"}),
    dict(id="C45", target="~/.cache/uv",
         goal="uv 缓存回收（09-23 已手工回收至 6.3G，声明「剩余无需动作」）",
         probe=("dir_bytes", "~/.cache/uv"),
         bands=[(8 * GB, ">=", "REWRITE"), (8 * GB, "<", "CLOSE")],
         why={"REWRITE": "缓存已回涨到 ≥8GiB（09-23 为 6.3G）⇒ 声明「剩余无需动作」失效，须重写为新一轮回收",
              "CLOSE": "缓存已瘦 ⇒ 已应用"}),
    dict(id="C46", target="~/.rustup（toolchains 旧版）",
         goal="清理 rustup 旧 toolchain ~2.6G",
         probe=("glob_count", ".rustup_home_probe/*"),   # 占位，真实探针见下
         bands=[],
         custom="rustup"),
    dict(id="C47", target="口径/授权（三处同问合并）", manual="自主运行期「可否直写 memory/L1」的授权口径二选一（流程政策，需人拍板）"),
]


def custom_rustup(root, spec):
    """C46 专用：数 ~/.rustup/toolchains 下的目录。"""
    p = os.path.expanduser("~/.rustup/toolchains")
    if not os.path.isdir(p):
        return "CLOSE", ["%s 不存在" % p]
    dirs = [d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d))]
    ev = ["%s ⇒ %d 个 toolchain: %s" % (p, len(dirs), dirs)]
    if len(dirs) > 1:
        return "ACTIVE", ev
    return "CLOSE", ev

# ---------------------------------------------------------------- 内核

def run_probe(root, probe, cache):
    key = json.dumps(probe, ensure_ascii=False, sort_keys=True, default=str)
    if key not in cache:
        fn = PROBES[probe[0]]
        val, ev = fn(root, *probe[1:])
        cache[key] = (val, ev)
    return cache[key]


def _cmp(val, op, thr):
    if op == ">=":
        return val >= thr
    if op == "<":
        return val < thr
    if op == "==":
        return val == thr
    raise ValueError(op)


def evaluate(spec, root, cache):
    """返回 (verdict, evidence list)。"""
    if spec.get("manual"):
        return "MANUAL", [spec["manual"]]
    if spec.get("custom") == "rustup":
        v, ev = custom_rustup(root, spec)
        return v, ev

    ev = []
    for p in spec.get("ev", []):
        val, e = run_probe(root, p, cache)
        ev.append(e)

    if "bands" in spec and spec["bands"]:
        val, e = run_probe(root, spec["probe"], cache)
        ev.insert(0, e)
        for thr, op, verdict in spec["bands"]:
            if verdict and _cmp(val, op, thr):
                return verdict, ev
        # 未命中任何带 ⇒ 落到 else（若有）
        if spec.get("else_probe"):
            v2, e2 = run_probe(root, spec["else_probe"], cache)
            ev.append(e2)
            return (spec["else_true"] if v2 else spec["else_false"]), ev
        return "BAND_UNMATCHED", ev          # fail-closed：分区必须穷尽

    val, e = run_probe(root, spec["probe"], cache)
    ev.insert(0, e)
    v = spec["true"] if val else spec.get("false")      # else 链项无 "false" 键
    if v is None and spec.get("else_probe"):
        v2, e2 = run_probe(root, spec["else_probe"], cache)
        ev.append(e2)
        v = spec["else_true"] if v2 else spec["else_false"]
    if v is None and spec.get("else2_probe"):
        v2, e2 = run_probe(root, spec["else2_probe"], cache)
        ev.append(e2)
        v = spec["else2_true"] if v2 else spec["else2_false"]
    return v, ev


def load_ledger_index(ledger_dir, audit=None):
    """复用 pending_evidence.parse_ledgers 解析台账（零重写）；返回 id -> row。"""
    pe = os.path.join(REPO, "tools", "pending_evidence.py")
    spec = importlib.util.spec_from_file_location("pe_reuse", pe)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.parse_ledgers(ledger_dir)
    idx = {}
    for r in rows:
        rid = (r.get("id") or "").strip()
        if not rid:
            continue
        idx.setdefault(rid, []).append(r)
    return idx, len(rows)


def hash_tree(paths):
    out = {}
    for p in paths:
        for dirpath, _dirs, files in os.walk(p):
            for f in sorted(files):
                fp = os.path.join(dirpath, f)
                try:
                    out[fp] = hashlib.sha256(open(fp, "rb").read()).hexdigest()
                except Exception:                            # noqa: BLE001
                    out[fp] = "ERR"
    return out


def run(root, ledger_dir, only=None, cache=None):
    cache = cache if cache is not None else {}
    ledger_idx, nrows = load_ledger_index(ledger_dir)
    results = []
    for spec in SPEC:
        if only and spec["id"] not in only:
            continue
        rows = ledger_idx.get(spec["id"], [])
        ground = {
            "in_ledger": bool(rows),
            "decided": bool(rows[0].get("decided")) if rows else None,
            "file": rows[0].get("file") if rows else None,
            "line": rows[0].get("line") if rows else None,
        }
        verdict, ev = evaluate(spec, root, cache)
        if not ground["in_ledger"]:
            verdict = "UNKNOWN_LEDGER"        # fail-closed：台账里找不到该 ID 不许静默跳过
            ev.insert(0, "台账内无该 ID（fail-closed）")
        results.append(dict(id=spec["id"], target=spec["target"], goal=spec.get("goal", ""),
                            verdict=verdict, evidence=ev, ground=ground,
                            why=(spec.get("why") or {}).get(verdict, spec.get("manual", ""))))
    return results, nrows


def print_table(results, nrows):
    order = ["CLOSE", "REWRITE", "ACTIVE", "MANUAL", "UNKNOWN_LEDGER"]
    buckets = {k: [] for k in order}
    for r in results:
        buckets.setdefault(r["verdict"], []).append(r)
    print("台账行数=%d  复测项=%d" % (nrows, len(results)))
    for k in order:
        if not buckets.get(k):
            continue
        print("\n== %s (%d) ==" % (k, len(buckets[k])))
        for r in buckets[k]:
            print("  %s  %s  [台账 %s:%s decided=%s]" % (r["id"], r["target"], r["ground"]["file"],
                                                        r["ground"]["line"], r["ground"]["decided"]))
            if r["why"]:
                print("      判由: %s" % r["why"])
            for e in r["evidence"][:4]:
                print("      证据: %s" % e)


# ---------------------------------------------------------------- 自测（负控）

def selftest():
    checks = []

    def ck(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    tmp = tempfile.mkdtemp(prefix="dpr_selftest_")
    try:
        # sandbox 树
        sb = os.path.join(tmp, "sb")
        os.makedirs(os.path.join(sb, "memory/autonomous_operation_sop"))
        os.makedirs(os.path.join(sb, "tools"))
        os.makedirs(os.path.join(sb, "logs"))
        os.makedirs(os.path.join(sb, "temp/local_llm"))
        os.makedirs(os.path.join(sb, "birdie_monitor"))
        os.makedirs(os.path.join(sb, "sche_tasks"))

        # NC1: 已应用项（helper 已指 tools/log_compress）标为待批 ⇒ 必须判 CLOSE
        open(os.path.join(sb, "memory/autonomous_operation_sop/helper.py"), "w").write(
            "# shim -> tools/log_compress.py\n")
        # NC2: 未应用项不许判 CLOSE（audio 工具不含「非媒体」）
        open(os.path.join(sb, "tools/audio_asset_inventory.py"), "w").write("# legacy\n")
        # NC3: 已应用「夹具入仓」⇒ CLOSE
        open(os.path.join(sb, "tools/rnum_fixture.py"), "w").write("x=1\n")
        # NC4: logs 沙盒 1MB ⇒ C30 不许判 ACTIVE（阈值 2GiB）
        open(os.path.join(sb, "logs/launch_gui.log"), "w").write("x" * 1024)
        # NC5: 影子标记存在 ⇒ C33 CLOSE
        open(os.path.join(sb, "sche_tasks/run_trendradar_digest.py"), "w").write("SHADOW = True\n")
        # NC6: haisou 已引单一源 ⇒ C34 CLOSE
        open(os.path.join(sb, "birdie_monitor/haisou_daily.py"), "w").write(
            "from haisou_relevance import is_relevant\n")

        res, nrows = run(sb, LEDGER_DIR)
        by = {r["id"]: r["verdict"] for r in res}

        ck("NC1 已应用项(helper→tools/log_compress) 必报 CLOSE", by.get("C19") == "CLOSE", by.get("C19"))
        ck("NC2 未应用项(audio 无双口径) 不许判 CLOSE", by.get("C21") != "CLOSE", by.get("C21"))
        ck("NC3 夹具入仓(沙盒有 tools/rnum_fixture.py) ⇒ CLOSE", by.get("C23") == "CLOSE", by.get("C23"))
        ck("NC4 沙盒 logs 仅 1MB ⇒ C30 不判 ACTIVE", by.get("C30") != "ACTIVE", by.get("C30"))
        ck("NC5 影子标记存在 ⇒ C33 CLOSE", by.get("C33") == "CLOSE", by.get("C33"))
        ck("NC6 haisou 引单一源 ⇒ C34 CLOSE", by.get("C34") == "CLOSE", by.get("C34"))
        ck("NC7 政策面(C26/C27/C47) 恒为 MANUAL",
           all(by.get(i) == "MANUAL" for i in ("C26", "C27", "C47")),
           [by.get(i) for i in ("C26", "C27", "C47")])
        ck("NC8 台账内不存在的 ID 判 UNKNOWN_LEDGER（fail-closed）",
           run(sb, LEDGER_DIR, only=set())[0] is not None and _unknown_check(sb), None)

        # NC9 只读自证：台账 sha256 前后一致
        before = hash_tree([LEDGER_DIR])
        run(sb, LEDGER_DIR)
        after = hash_tree([LEDGER_DIR])
        ck("NC9 --readonly-proof：台账 sha256 前后一致", before == after and len(before) > 0,
           "files=%d" % len(before))

        # NC10 阈值边界：沙盒 logs 恰好 2GiB ⇒ ACTIVE（>=），略小 ⇒ 非 ACTIVE
        big = os.path.join(sb, "logs")
        shutil.rmtree(big)
        os.makedirs(big)
        with open(os.path.join(big, "a.log"), "wb") as fh:
            fh.truncate(2 * GB)                    # sparse
        res2, _ = run(sb, LEDGER_DIR, only={"C30"})
        # du 口径=已分配块（R429 坑①）：sparse 文件不计入 ⇒ C30 判 CLOSE=口径自证
        ck("NC10 du 口径=已分配块：稀疏 2GiB 不计入 ⇒ C30 判 CLOSE（防按逻辑大小误判）",
           res2[0]["verdict"] == "CLOSE", res2[0]["verdict"])

        # NC11 假参照：C36 端口/进程探针不得因异常而假绿（返回布尔而非抛错）
        res3, _ = run(sb, LEDGER_DIR, only={"C36"})
        ck("NC11 C36 探针返回三态之一", res3[0]["verdict"] in ("ACTIVE", "CLOSE", "REWRITE"),
           res3[0]["verdict"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        print("%s %s%s" % ("PASS" if passed else "FAIL", name, "" if passed else "  detail=%r" % (detail,)))
    print("selftest: %d/%d %s" % (sum(1 for c in checks if c[1]), len(checks), "OK" if ok else "FAILED"))
    return 0 if ok else 1


def _unknown_check(sb):
    """临时注入一个台账里不存在的 ID，验证 fail-closed（真实负控）。"""
    SPEC.append(dict(id="C999", target="(负控)台账不存在", goal="nc",
                     probe=("file_exists", "no_such_file_xyz"), true="CLOSE", false="ACTIVE"))
    try:
        res, _ = run(sb, LEDGER_DIR, only={"C999"})
        return bool(res) and res[0]["verdict"] == "UNKNOWN_LEDGER"
    finally:
        SPEC.pop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=REPO)
    ap.add_argument("--ledger-dir", default=LEDGER_DIR)
    ap.add_argument("--only", default=None, help="逗号分隔的 ID 列表")
    ap.add_argument("--json", default=None, help="结果落盘路径")
    ap.add_argument("--readonly-proof", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    before = hash_tree([args.ledger_dir]) if args.readonly_proof else None
    results, nrows = run(args.root, args.ledger_dir, only=only)
    print_table(results, nrows)

    if args.readonly_proof:
        after = hash_tree([args.ledger_dir])
        changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
        print("\n--readonly-proof: files=%d changed=%d %s" % (len(before), len(changed),
                                                              "OK" if not changed else changed[:5]))
        if changed:
            return 2
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        json.dump({"n_ledger_rows": nrows, "results": results},
                  open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("json -> %s" % args.json)
    bad = [r["id"] for r in results if r["verdict"] == "UNKNOWN_LEDGER"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
