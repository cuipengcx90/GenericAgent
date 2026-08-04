#!/usr/bin/env python3
"""重点项目更新监控：首次建基线，后续只报告未通知的远端变化；绝不执行升级。"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.json"
STATE = ROOT / "state.json"
UA = "GenericAgent-project-update-monitor/1.0"


def now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except FileNotFoundError: return default

def atomic_dump(p, data):
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n")
    os.replace(tmp, p)

def _gh_token():
    try:
        r = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""

_GH_TOKEN = _gh_token()

def get_json(url):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if _GH_TOKEN and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {_GH_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r: return json.loads(r.read().decode())

def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent":UA})
    with urllib.request.urlopen(req, timeout=25) as r: return r.read().decode("utf-8", "replace")

def run_version(command):
    try:
        r = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=8)
        text = (r.stdout or r.stderr).strip().splitlines()
        return text[0][:160] if r.returncode == 0 and text else None
    except (OSError, subprocess.TimeoutExpired): return None

def classify(text):
    t = text.lower()
    out=[]
    for label, keys in (("🔒 安全", ["security","cve","vulnerability","permission","credential"]), ("💥 破坏性变更", ["breaking","migration","deprecated","removed","incompatible"]), ("🐛 修复", ["fix","bug","crash","retry","leak","regression"]), ("✨ 新增能力", ["feat","feature","added","support","integration","workflow"]), ("⚡ 性能", ["performance","faster","latency","cache","parallel"])):
        if any(k in t for k in keys): out.append(label)
    return out or ["🔎 待人工解读"]

def snapshot(src):
    repo=src["repo"]
    releases=get_json(f"https://api.github.com/repos/{repo}/releases?per_page=1")
    tags=get_json(f"https://api.github.com/repos/{repo}/tags?per_page=1")
    commits=get_json(f"https://api.github.com/repos/{repo}/commits?per_page=1")
    release=releases[0] if releases else {}
    tag=tags[0] if tags else {}
    commit=commits[0] if commits else {}
    changelog_hash=None
    changelog_body=""
    if src.get("changelog_url"):
        try: changelog_hash=hashlib.sha256(get_text(src["changelog_url"]).encode()).hexdigest()
        except Exception as e: changelog_hash="ERROR:"+type(e).__name__
        # Mintlify 等文档站的正文是前端渲染的，直接抓 HTML 拿不到内容；
        # 这类站点通常在 URL 后加 .md 即可拿到纯 Markdown 原文，作为补充内容源。
        try: changelog_body=get_text(src["changelog_url"].rstrip("/") + ".md")[:20000]
        except Exception: changelog_body=""
    local_versions=[v for v in (run_version(c) for c in src.get("local_commands",[])) if v]
    paths=[p for p in src.get("local_paths",[]) if Path(p).exists()]
    return {"release_tag":release.get("tag_name"), "release_url":release.get("html_url"), "release_published":release.get("published_at"), "release_body":release.get("body","")[:20000], "release_prerelease":release.get("prerelease",False), "tag":tag.get("name"), "tag_sha":tag.get("commit",{}).get("sha"), "commit_sha":commit.get("sha"), "commit_message":commit.get("commit",{}).get("message","")[:3000], "commit_url":commit.get("html_url"), "changelog_hash":changelog_hash, "changelog_body":changelog_body, "local_versions":local_versions, "local_paths":paths, "checked_at":now()}

def changed(old, new):
    # 只以“版本标识”为通知触发依据（正式版或 RC/Beta 的 tag/release_tag 变化、changelog 内容变化）；
    # 单纯的 commit_sha 前进（同一版本号下的增量提交）不算“更新”，避免噪音。
    keys=("release_tag","tag","changelog_hash")
    return [k for k in keys if old.get(k) != new.get(k)]


def recommendation(src, s):
    installed=bool(s["local_versions"] or s["local_paths"])
    version_text = " ".join(filter(None, [s["release_tag"], s["tag"]]))
    pre = s["release_prerelease"] or bool(re.search(r"(?:alpha|beta|rc|nightly)", version_text, re.I))
    if not installed: return "暂不更新：本机未检测到安装/项目引用；继续监控即可。"
    if src["upgrade_policy"] == "manual_review": return "人工决策：本地存在核心项目/配置关联；先在隔离 worktree 验证迁移与回滚，未经确认不升级。"
    if pre: return "观察后更新：检测到预发布版本；等待稳定版或在隔离环境验证。"
    return "建议评估更新：本机已检测到关联；先核对 Release 的破坏性变更、依赖与回滚路径。"

class TranslationError(RuntimeError):
    """自动翻译未通过质量闸门；调用方不得以英文原文替代推送。"""


def _prefer_chinese_section(raw):
    """上游明确提供中文分节时，仅保留该分节，避免中英文重复。"""
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s*(?:简体中文|中文|Chinese\s*\(\s*Simplified\s*\))\s*$", line, re.I)
        if not match:
            continue
        level = len(match.group(1))
        selected = []
        for candidate in lines[index + 1:]:
            heading = re.match(r"^(#{1,6})\s+", candidate)
            if heading and len(heading.group(1)) <= level:
                break
            selected.append(candidate)
        return "\n".join(selected)
    return raw


def _extract_markdown_bullets(raw):
    """提取列表项，并将 Markdown 的缩进续行合并到上一项。"""
    bullets, current = [], None
    in_fence = False
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(">"):
            continue
        if re.match(r"^#{1,6}\s", stripped):
            current = None
            continue
        match = re.match(r"^\s*(?:[-*+•]\s+|\d+[.)]\s+)(.*)$", raw_line)
        if match:
            current = match.group(1).strip()
            if current:
                bullets.append(current)
        elif current is not None and raw_line[:1].isspace():
            # GitHub Markdown 的列表项可用两个或更多空格换行续写。
            current += " " + stripped
            bullets[-1] = current
        else:
            current = None
    return bullets


def _strip_trailing_english_dup(text):
    """部分上游把中文说明和英文重复说明拼在同一行（无分隔符），
    只保留最后一个中文句末标点前的中文部分，避免中英并存。"""
    positions = [m.end() for m in re.finditer(r"[。！？]", text)]
    if not positions:
        return text
    last = positions[-1]
    remainder = text[last:].strip()
    if remainder and not re.search(r"[\u4e00-\u9fff]", remainder):
        return text[:last].strip()
    return text


def _translate_to_chinese(items):
    """使用已登录的本机 Codex CLI 批量翻译，并严格校验 JSON 输出。"""
    if not items:
        return []
    prompt = (
        "将以下 JSON 字符串数组中的英文软件发布说明翻译为简体中文。"
        "严格仅输出同等长度的 JSON 字符串数组，不要解释或代码块。"
        "保留项目名、代码标识符、命令、URL、版本号、PR/issue 编号原样。\n输入："
        + json.dumps(items, ensure_ascii=False)
    )
    output = None
    try:
        with tempfile.NamedTemporaryFile(prefix="project-update-translation-", suffix=".json", delete=False) as f:
            output = f.name
        run = subprocess.run(
            ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
             "--output-last-message", output, prompt],
            text=True, capture_output=True, timeout=120,
        )
        if run.returncode != 0:
            raise TranslationError(f"Codex 退出码 {run.returncode}: {run.stderr[-300:]}")
        translated = json.loads(Path(output).read_text(encoding="utf-8").strip())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise TranslationError(f"Codex 翻译调用失败：{type(exc).__name__}: {exc}") from exc
    finally:
        if output:
            Path(output).unlink(missing_ok=True)
    if not isinstance(translated, list) or len(translated) != len(items) or not all(isinstance(x, str) and x.strip() for x in translated):
        raise TranslationError("Codex 返回结果不是等长的非空 JSON 字符串数组")
    # 阻止模型忽略指令、将英文原文透传进通知。
    if any(not re.search(r"[\u4e00-\u9fff]", item) for item in translated):
        raise TranslationError("Codex 翻译结果含无中文字符的条目")
    return [item.strip() for item in translated]


def chinese_change_summary(release_body, commit_message, changelog_body=""):
    """从上游 Markdown 提取完整中文要点；纯英文内容经本机 Codex 翻译。"""
    raw = _prefer_chinese_section(release_body or changelog_body or commit_message or "")
    bullets = _extract_markdown_bullets(raw)
    if not bullets:
        # 无列表结构时仅作保守句子拆分，仍不纳入标题、引用或代码块。
        filtered = [line.strip() for line in raw.splitlines()
                    if line.strip() and not line.lstrip().startswith(("#", ">", "```"))]
        bullets = [x.strip() for x in re.split(r"(?<=[.!?。！？])\s+", " ".join(filtered)) if len(x.strip()) >= 8]
    # 纯资源下载链接（如 "[macOS arm64](https://...dmg)"）不是变更说明，剔除。
    bullets = [x for x in bullets if not re.match(r"^\[[^\]]+\]\(https?://\S+\)$", x.strip())]
    # GitHub 常见落款格式："(... by @author)"、"... by @author in url"、结尾单独 "in url"；
    # 只移除署名本身，括号内容中除署名外的其余部分（如 PR 链接）必须保留。
    bullets = [re.sub(r"\s+by\s+@[^\s)]+(?:\s+in\s+https?://\S+)?(?=\))"
                       r"|\s+by\s+@[^\s]+(?:\s+in\s+https?://\S+)?$"
                       r"|\s+in\s+https?://\S+$", "", x, flags=re.I).strip()
               for x in bullets]
    # 部分上游在同一行内先给中文说明再重复英文说明（无分隔符），只保留中文部分。
    bullets = [_strip_trailing_english_dup(x) for x in bullets]
    bullets = [x for x in bullets if len(x) >= 4][:6]
    if not bullets:
        return ["上游未提供可提取的变更说明，请通过 Release 链接查看。"]
    # 任一中文字符代表官方已给出中文或中英混排说明，原样保留，绝不二次机翻。
    if not all(re.search(r"[\u4e00-\u9fff]", item) for item in bullets):
        bullets = _translate_to_chinese(bullets)
    return bullets

def report(src, old, s, diffs):
    raw="\n".join([s["release_body"],s["commit_message"]])
    lines=[f"【项目更新】{src['name']}", "", "结论：" + recommendation(src,s), "", "版本与变化：", f"- Release：{s['release_tag'] or '无'}" + ("（预发布）" if s['release_prerelease'] else ""), f"- Tag：{s['tag'] or '无'}；Commit：{(s['commit_sha'] or '无')[:12]}", f"- 变更类型：{'、'.join(classify(raw))}", "", "核心变更（中文摘要，专有术语保留）："]
    lines += [f"{i}. {x}" for i, x in enumerate(chinese_change_summary(s['release_body'], s['commit_message'], s.get('changelog_body', '')), 1)]
    lines += ["", "本机建议：" + recommendation(src,s), f"- 本地版本：{'；'.join(s['local_versions']) or '未检测到 CLI'}", f"- 本地路径：{'；'.join(s['local_paths']) or '未检测到'}", "- 自动升级：已禁用"]
    if s["release_url"]: lines.append(f"- Release：{s['release_url']}")
    if s["commit_url"]: lines.append(f"- Commit：{s['commit_url']}")
    if src.get("changelog_url"): lines.append(f"- Changelog：{src['changelog_url']}")
    text="\n".join(lines)
    if "结论：" not in text or "本机建议：" not in text: raise RuntimeError("报告质量闸门未通过")
    return text

def project_card_element(src, old, s, diffs, index=None):
    """构造单项目卡片区块：始终展示，变化项目才携带精简中文摘要。"""
    name = src["name"]
    seq = f"{index}. " if index else ""
    version = s["release_tag"] or s["tag"] or "未发现版本标识"
    if diffs:
        raw = "\n".join([s["release_body"], s["commit_message"]])
        badge = "🔔 有版本更新"
        detail = "\n".join(f"{i}. {x}" for i, x in enumerate(chinese_change_summary(s["release_body"], s["commit_message"], s.get("changelog_body", ""))[:3], 1))
        detail += f"\n\n💡 **建议**：{recommendation(src, s)}"
        fields = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"🏷️ **当前版本**\n`{version}`"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"🗂️ **变更类型**\n{' · '.join(classify(raw))}"}},
        ]
        elements = [
            {"tag": "div", "fields": fields},
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
        ]
        actions = []
        if s.get("release_url"):
            actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "🔗 查看 Release"}, "type": "primary", "url": s["release_url"]})
        if s.get("commit_url"):
            actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "🔗 查看 Commit"}, "type": "default", "url": s["commit_url"]})
        if actions: elements.append({"tag": "action", "actions": actions})
    else:
        badge = "✅ 无版本更新"
        baseline = old.get("release_tag") or old.get("tag") or version
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": f"与上次推送相比无版本更新 · 🏷️ 当前 `{baseline}`"}}]
    return [{"tag": "div", "text": {"tag": "lark_md", "content": f"**📦 {seq}{badge}｜{name}**"}}] + elements + [{"tag": "hr"}]


def build_notification_card(cfg, observations):
    """单张固定项目清单卡；observations 按 sources.json 顺序包含全部来源。"""
    changed_count = sum(bool(x[3]) for x in observations)
    summary_icon = "🔔" if changed_count else "📊"
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": f"{summary_icon} 本轮检查 **{len(observations)}** 个重点项目 · **{changed_count}** 个有版本更新\n仅版本标识（正式版 / RC / Beta / Changelog）变化触发详情；不会自动升级。"}}, {"tag": "hr"}]
    for i, (src, old, s, diffs) in enumerate(observations, 1):
        elements.extend(project_card_element(src, old, s, diffs, index=i))
    if elements[-1].get("tag") == "hr": elements.pop()
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue" if changed_count else "green", "title": {"tag": "plain_text", "content": "🚀 重点项目更新监控"}},
        "elements": elements,
    }


def no_change_notice(cfg, state):
    """每轮无新增时的短状态，避免伪造“无更新”而遗漏采集异常。"""
    sources = state.get("sources", {})
    versions = []
    for src in cfg["sources"]:
        s = sources.get(src["id"], {})
        version = s.get("release_tag") or s.get("tag")
        if version: versions.append(f"{src['name']} {version}")
    shown = "、".join(versions[:3]) + ("等" if len(versions) > 3 else "")
    return f"【项目更新】与上次推送相比无新增变化。当前已监控 {len(cfg['sources'])} 个项目；{shown or '已完成检查'}。"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--initialize", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--notify", action="store_true", help="有更新时通过飞书私信推送一次；推送成功后才落盘新基线")
    args=ap.parse_args()
    cfg=load(SOURCES,{"sources":[]}); state=load(STATE,{"schema_version":1,"sources":{}})
    reports=[]; observations=[]; errors=[]; pending_state=dict(state.get("sources",{}))
    for src in cfg["sources"]:
        try:
            s=snapshot(src); old=state["sources"].get(src["id"])
            if args.initialize or old is None:
                pending_state[src["id"]]=s; continue
            diffs=changed(old,s)
            observations.append((src, old, s, diffs))
            if diffs or args.all:
                reports.append(report(src,old,s,diffs or ["人工全量检查"]))
            pending_state[src["id"]]=s
        except Exception as e: errors.append(f"{src['name']}: {type(e).__name__}: {e}")
    # 基线是通知幂等键：首次初始化，或本轮无异常且通知成功后，才整体提交。
    # 纯本地预览（未带 --notify）绝不能推进有变化来源，避免“看过但未推送”造成漏报。
    can_commit = args.initialize or (not errors and args.notify)
    candidate_state = dict(state)
    candidate_state["sources"] = pending_state
    # 首次初始化不发消息；采集异常时不发送“无更新”卡，避免误报。
    card = None
    if args.notify and not args.initialize and not errors:
        if len(observations) != len(cfg["sources"]):
            raise RuntimeError("项目观测数量不完整，拒绝发送可能遗漏项目的通知")
        card = build_notification_card(cfg, observations)
    if card:
        sys.path.insert(0, "/Users/cuip/projects/GenericAgent")
        from reflect_scripts.reflect_common import send_feishu_card
        try:
            send_feishu_card(card)
        except Exception as e:
            print(f"推送失败，未落盘新基线，下次将重试同一批更新: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
    if can_commit:
        atomic_dump(STATE, candidate_state)
    if args.initialize: print(f"已初始化 {len(cfg['sources'])} 个来源的基线；未发送历史更新。")
    elif reports: print("\n\n---\n\n".join(reports))
    elif errors: print("本轮采集存在异常，未发送无变化状态。")
    else: print(no_change_notice(cfg, state))
    if errors:
        print("\n## 采集异常（未覆盖既有基线）\n"+"\n".join("- "+x for x in errors), file=sys.stderr)
        return 2
    return 0
if __name__ == "__main__": raise SystemExit(main())
