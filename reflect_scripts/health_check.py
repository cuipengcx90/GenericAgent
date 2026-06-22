#!/usr/bin/env python3
"""reflect_scripts 健康度自检工具
检查每个脚本的：1.依赖可用性 2.最后执行时间 3.配置完整性

支持：
- --json 输出JSON格式
- --notify 在存在 broken/zombie/never 问题时发送飞书群告警

退出码规范：
  0 = 全部正常
  1 = 存在未配置/从未执行/僵尸
  2 = 存在依赖缺失
"""
import os, sys, re, ast, importlib, subprocess, json, argparse
from datetime import datetime
from pathlib import Path

GA_ROOT = Path("/Users/cuip/projects/GenericAgent")
SCRIPTS_DIR = GA_ROOT / "reflect_scripts"
LOGS_DIR = GA_ROOT / "logs"
SCHE_DONE = GA_ROOT / "sche_tasks" / "done"

# ── 1. 扫描脚本文件 ──
def get_scripts():
    return sorted([f for f in SCRIPTS_DIR.glob("*.py")
                   if not f.name.startswith("_") and f.name not in ("reflect_common.py", "notify_feishu_group.py")])

# ── 2. 提取import依赖 ──
def extract_imports(path):
    deps = []
    try:
        src = path.read_text(errors="replace")
        for line in src.split("\n"):
            line = line.strip()
            m = re.match(r"^import (\S+)", line)
            if m:
                # 处理 `import sys, os` 多模块情况，去掉逗号后分割
                for tok in re.split(r"[\s,]+", m.group(1)):
                    if tok:
                        deps.append(tok.split(".")[0])
            m = re.match(r"^from (\S+) import", line)
            if m and not m.group(1).startswith("."): deps.append(m.group(1).split(".")[0])
    except: pass
    # 去掉标准库和内部模块
    stdlib = {"sys","os","re","json","time","datetime","pathlib","subprocess","threading",
              "hashlib","hmac","base64","urllib","email","imaplib","ast","importlib","argparse"}
    internal = {"reflect_common","notify_feishu_group","llmcore","reflect_scripts"}
    return [d for d in set(deps) if d not in stdlib and d not in internal]

# ── 3. 检查依赖是否可import ──
def check_dep(dep):
    try:
        importlib.import_module(dep)
        return True
    except ImportError:
        return False

# ── 4. 从日志找最后执行时间 ──
def last_run_time(script_name):
    # 在scheduler_reflect.log中搜索脚本名
    log = LOGS_DIR / "scheduler_reflect.log"
    if not log.exists():
        return None
    # 搜索最后出现的时间戳
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")
    stem = Path(script_name).stem
    last_ts = None
    try:
        content = log.read_text(errors="replace")
        # 找包含脚本名的行附近的时间戳
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if stem in line or script_name in line:
                # 向前找时间戳
                for j in range(max(0,i-3), min(len(lines),i+3)):
                    m = pattern.search(lines[j])
                    if m:
                        try:
                            ts = datetime.fromisoformat(m.group(1).replace("T"," "))
                            if last_ts is None or ts > last_ts:
                                last_ts = ts
                        except: pass
    except: pass
    return last_ts

# ── 5. 检查配置完整性（硬编码占位符） ──
PLACEHOLDER_PATTERNS = [
    r"your@email\.com", r"your_app_password", r"ghp_your_token",
    r"your_username", r"YOUR_", r"<your"
]
def has_placeholder(path):
    src = path.read_text(errors="replace")
    for p in PLACEHOLDER_PATTERNS:
        if re.search(p, src, re.IGNORECASE):
            return True
    return False

# ── 主检查 ──
def run():
    scripts = get_scripts()
    now = datetime.now()
    results = []

    for s in scripts:
        deps = extract_imports(s)
        missing = [d for d in deps if not check_dep(d)]
        last_run = last_run_time(s.name)
        placeholder = has_placeholder(s)

        if missing:
            status = "❌ 依赖缺失"
        elif placeholder:
            status = "⚠️  未配置"
        elif last_run is None:
            status = "💤 从未执行"
        else:
            days = (now - last_run).days
            if days > 7:
                status = f"🧟 僵尸({days}天未运行)"
            elif days > 1:
                status = f"😴 低活跃({days}天前)"
            else:
                status = "✅ 活跃"

        results.append({
            "script": s.name,
            "status": status,
            "deps": deps,
            "missing_deps": missing,
            "last_run": last_run.strftime("%Y-%m-%d %H:%M") if last_run else "N/A",
            "has_placeholder": placeholder
        })

    return results


def maybe_notify(results, broken, zombie):
    """存在 broken/zombie/never 时发送飞书告警；失败不影响主流程退出码。"""
    if broken == 0 and zombie == 0:
        return None
    try:
        sys.path.insert(0, str(GA_ROOT))
        from reflect_scripts.notify_feishu_group import notify_group
        bad_items = [r for r in results if ('依赖缺失' in r['status'] or '僵尸' in r['status'] or '从未执行' in r['status'])]
        lines = [
            '【health_check告警】',
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"问题汇总: broken={broken}, zombie_or_never={zombie}",
        ]
        for r in bad_items[:10]:
            lines.append(f"- {r['script']}: {r['status']}")
        if len(bad_items) > 10:
            lines.append(f"- ...其余{len(bad_items)-10}项见health_check --json")
        return notify_group("\n".join(lines))
    except Exception as e:
        return {"notify_error": str(e)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="reflect_scripts 健康度自检")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    parser.add_argument("--notify", action="store_true", help="存在broken/zombie/never时发送飞书群告警")
    args = parser.parse_args()

    results = run()
    active = sum(1 for r in results if r['status'].startswith('✅'))
    zombie = sum(1 for r in results if '僵尸' in r['status'] or '从未' in r['status'])
    uncfg  = sum(1 for r in results if '未配置' in r['status'])
    broken = sum(1 for r in results if '依赖缺失' in r['status'])

    notify_resp = maybe_notify(results, broken, zombie) if args.notify else None

    # 退出码：2=broken, 1=有问题, 0=全部正常
    exit_code = 2 if broken > 0 else (1 if (zombie + uncfg) > 0 else 0)

    if args.json:
        output = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "active": active, "zombie_or_never": zombie,
                "unconfigured": uncfg, "broken": broken,
                "total": len(results)
            },
            "exit_code": exit_code,
            "results": results
        }
        if args.notify:
            output["notify"] = notify_resp
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        sys.exit(exit_code)

    print(f"\n{'='*60}")
    print(f"reflect_scripts 健康度报告  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    for r in results:
        print(f"\n{r['script']}")
        print(f"  状态: {r['status']}")
        print(f"  依赖: {r['deps'] if r['deps'] else '(无外部依赖)'}")
        if r['missing_deps']:
            print(f"  ❌ 缺失: {r['missing_deps']}")
        print(f"  最后执行: {r['last_run']}")
        if r['has_placeholder']:
            print(f"  ⚠️  含未替换占位符（需配置）")
    print(f"\n{'='*60}")
    print(f"汇总: 活跃={active} 僵尸/未执行={zombie} 未配置={uncfg} 依赖缺失={broken} 共{len(results)}个")
    if args.notify:
        print(f"飞书通知: {notify_resp}")
