"""
场景4：GitHub 仓库监控
监控新 PR / Issue，Agent 自动总结并推飞书
启动：python agentmain.py --reflect reflect_scripts/04_github_monitor.py
"""
import sys, json, urllib.request
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu, Debounce

INTERVAL = 300  # 每5分钟检查一次

# ===== 配置区 =====
GITHUB_TOKEN = "ghp_your_token_here"       # GitHub Personal Access Token
WATCH_REPOS  = [
    "your_username/your_repo",              # 要监控的仓库，可多个
]
WATCH_PR     = True    # 监控 PR
WATCH_ISSUE  = True    # 监控 Issue
# ==================

_seen_prs    = set()
_seen_issues = set()

def _gh_get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GenericAgent-Reflect"
    })
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def check():
    tasks = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    for repo in WATCH_REPOS:
        try:
            if WATCH_PR:
                prs = _gh_get(f"https://api.github.com/repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=5")
                for pr in prs:
                    pid = pr["number"]
                    created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                    if pid not in _seen_prs and created > cutoff:
                        _seen_prs.add(pid)
                        tasks.append(
                            f"GitHub [{repo}] 新 PR #{pid}：《{pr['title']}》\n"
                            f"作者：{pr['user']['login']}，链接：{pr['html_url']}\n"
                            f"请总结这个 PR 的改动目的、影响范围，并给出 code review 要点。"
                        )
            if WATCH_ISSUE:
                issues = _gh_get(f"https://api.github.com/repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=5")
                for issue in issues:
                    if "pull_request" in issue:
                        continue  # PR 也会出现在 issues 里，跳过
                    iid = issue["number"]
                    created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
                    if iid not in _seen_issues and created > cutoff:
                        _seen_issues.add(iid)
                        tasks.append(
                            f"GitHub [{repo}] 新 Issue #{iid}：《{issue['title']}》\n"
                            f"内容：{issue.get('body','')[:500]}\n"
                            f"请判断问题类型（bug/feature/question），给出处理建议和优先级。"
                        )
        except Exception as e:
            print(f"[GitHub监控失败] {repo}: {e}")

    return tasks[0] if tasks else None  # 每次处理一条，下次轮询处理下一条

def on_done(result: str):
    notify_feishu(f"🐙 GitHub 动态\n\n{result}")