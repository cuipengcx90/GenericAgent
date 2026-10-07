"""同步全能IT最近90天课程到海搜监控关键词"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

QUANGNENG_BASE = "https://quangneng.com"
KEYWORDS_FILE = Path(__file__).with_name("quangneng_keywords.json")
MAX_SEARCH_ATTEMPTS = 30  # 最多搜索30次（约1个月）

# ★2026-10-07 修复：本文件的合并处早已调用 is_ai_topic，但**从未 import**
# （上一批「只推 AI」接线漏项）⇒ 真跑必 NameError，且被定时任务里的 subprocess
# 捕获成 stderr（步骤1 静默降级、整轮照跑）。此处补上，并同时接方向门：
# 只留大模型 / AI Agent / 应用向，剔掉基础慕课向（用户 2026-10-07 口径）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from tools.ai_topic import is_ai_topic, is_direction_ai  # noqa: E402
except Exception:  # 回退：保守子集（与单一源同口径）
    def is_ai_topic(text):
        t = (text or "").lower()
        return any(p in t for p in ("人工智能", "大模型", "智能体", "机器学习", "深度学习",
                                    "提示词", "生成式", "文生图", "文生视频", "多模态", "ai"))

    def is_direction_ai(text):
        # 回退版（2026-10-07 v3 宽档）：只挡「纯入门/通识基础课」，AI 应用向全留
        t = (text or "").lower()
        _mod = ("入门", "零基础", "从零", "小白", "导论", "科普", "通识", "速成", "基础")
        _subj = ("人工智能", "机器学习", "深度学习", "计算机视觉", "自然语言", "神经网络",
                 "数据科学", "大模型", "智能体", "ai")
        _app = ("实战", "项目", "开发", "落地", "搭建", "部署", "变现", "副业", "出海", "自媒体",
                "运营", "教育", "绘画", "视频", "编程", "助手", "数字人", "多智能体", "agent",
                "rag", "工作流", "案例", "企业级", "产品", "接单", "创业", "咨询", "客服",
                "知识库", "翻译", "测试", "运维", "设计", "应用")
        if not any(k in t for k in _mod):
            return True
        if not any(k in t for k in _subj):
            return True
        return any(k in t for k in _app)


def fetch_recent_courses(days=90):
    """抓取首页最新课程标题（首页按时间倒序，直接取前N个即可）"""
    courses = {}

    print(f"🔍 抓取全能IT首页最新课程...")

    try:
        # 直接抓取首页（首页按时间倒序，包含所有分类的最新课程）
        resp = requests.get(QUANGNENG_BASE, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 查找课程列表
        articles = soup.select('article.post')

        # 处理前10个article（首页倒序排列，肯定是最新的，不需要判断时间）
        for article in articles[:10]:
            # 提取课程链接
            link_elem = article.select_one('a[href*="/"]')
            if not link_elem:
                continue

            course_url = link_elem.get('href', '')
            if not course_url.startswith('http'):
                course_url = QUANGNENG_BASE + course_url

            # 提取标题（从article的header中提取，避免误匹配其他链接）
            title_elem = article.select_one('header h2 a, .entry-header h2 a')
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)

            # 跳过空标题或过短标题
            if not title or len(title) < 5:
                continue

            # 直接加入课程列表（不访问详情页，不判断时间）
            if title not in courses:
                courses[title] = {
                    "title": title,
                    "source_url": course_url,
                    "published_at": None,  # 首页倒序保证是最新的，不需要精确时间
                    "found_at": None,
                    "search_count": 0
                }
                print(f"✓ {title}")

    except Exception as e:
        print(f"⚠️ 抓取失败: {e}")

    print(f"✓ 抓取到 {len(courses)} 个课程\n")
    return courses


def clean_old_keywords(keywords):
    """清理超过最大搜索次数仍未找到的课程，以及已找到的课程"""
    to_remove = []
    for title, info in keywords.items():
        # 清理已找到的
        if info.get("found", False):
            to_remove.append(title)
        # 清理超期未找到的
        elif info.get("search_count", 0) >= MAX_SEARCH_ATTEMPTS:
            to_remove.append(title)

    removed = 0
    found_removed = 0
    for title in to_remove:
        if keywords[title].get("found", False):
            found_removed += 1
            print(f"🗑️ 清理已找到: {title}")
        else:
            removed += 1
            print(f"🗑️ 清理超期: {title}（已搜索{MAX_SEARCH_ATTEMPTS}次未找到）")
        del keywords[title]

    return removed, found_removed


def sync_keywords():
    """同步全能IT课程到动态关键词"""
    print("=== 同步全能IT课程到海搜关键词 ===\n")

    # 1. 加载现有关键词
    if KEYWORDS_FILE.exists():
        existing = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
    else:
        existing = {}
    print(f"📋 现有关键词: {len(existing)} 个")

    # 2. 抓取最近90天课程（从首页获取）
    new_courses = fetch_recent_courses(days=90)
    print()

    # 3. 合并新课程（去重 + AI 主题门 2026-10-07 R559：站内含软考/影视后期等非 AI 课）
    added = 0
    skipped_non_ai = 0
    skipped_basic = 0
    for title, info in new_courses.items():
        if title in existing:
            continue
        if not is_ai_topic(title):
            skipped_non_ai += 1
            continue
        if not is_direction_ai(title):  # 方向门（2026-10-07）：只留应用向，剔基础慕课
            skipped_basic += 1
            continue
        existing[title] = info
        added += 1
    if skipped_non_ai:
        print(f"🚫 AI主题门跳过: {skipped_non_ai} 个非 AI 课程（如 Hadoop/Docker/软考/影视后期）")
    if skipped_basic:
        print(f"🚫 方向门跳过: {skipped_basic} 个 AI 但属基础慕课/非应用向"
              f"（如 机器学习基础课/零基础入门，2026-10-07）")

    # 4. 清理旧关键词
    removed, found_removed = clean_old_keywords(existing)

    # 5. 保存
    KEYWORDS_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6. 统计
    unfound = sum(1 for info in existing.values() if not info.get("found", False))

    print(f"\n=== 同步完成 ===")
    print(f"新增: {added} 个")
    print(f"清理已找到: {found_removed} 个")
    print(f"清理超期: {removed} 个")
    print(f"待搜索: {unfound} 个")
    print(f"总计: {len(existing)} 个")


if __name__ == "__main__":
    sync_keywords()
