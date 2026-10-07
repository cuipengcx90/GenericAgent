#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 主题判据单一源（2026-10-07，起因 R559）。

背景（现场事故）：海搜推送里混进 Hadoop×9 / K8s×7 / Spring Cloud×6 / Docker×5 /
Go微服务×2 等非 AI 结果，共 29/37 条。根因是**判据自指**：`haisou_relevance.is_relevant(q, title)`
只校验「标题里有没有 query 的最长 token」——Hadoop 这种 query 必然自证通过，
于是「搜什么就推什么」，没有任何一步校验「这条内容是不是 AI 领域」。

本模块提供唯一判据：**只有显式出现 AI 领域信号才算 AI**（严格档，宁缺勿滥）。
- 命中 POSITIVE ⇒ AI（即使同时含 NEGATIVE，如「AI+云原生实战」仍算 AI）。
- 只命中 NEGATIVE 或都没命中 ⇒ 非 AI（判定为 UNKNOWN，仍按非 AI 处理，避免再放垃圾）。

用法：
    from tools.ai_topic import is_ai_topic, verdict, explain
    if not is_ai_topic(title): skip

自测：python3 tools/ai_topic.py --selftest
"""
from __future__ import annotations

import json
import re
import sys

# ---------------------------------------------------------------- 词表（单一源）

# ASCII 信号：必须整词命中（避免 email / airflow / trailing 里的 "ai" 误判）
POSITIVE_ASCII = [
    "ai", "aigc", "genai", "llm", "llms", "gpt", "chatgpt", "openai", "anthropic",
    "claude", "codex", "cursor", "copilot", "windsurf", "agent", "agents", "agentic",
    "rag", "prompt", "prompts", "langchain", "langgraph", "llamaindex", "llama",
    "qwen", "deepseek", "gemini", "mistral", "huggingface", "transformers",
    "pytorch", "tensorflow", "keras", "opencv", "yolo", "whisper", "ollama", "vllm",
    "stable-diffusion", "stablediffusion", "comfyui", "midjourney", "sora",
    "dify", "coze", "mcp", "tts", "asr", "nlp", "ocr", "lora", "finetune",
    "embedding", "multimodal", "mlops",
]

# 英文短语信号：词间允许空格/连字符/下划线（如 "vibe coding" / "vibe-coding"）整词命中
POSITIVE_PHRASES = [
    "vibe coding", "vibe-code",
]
POSITIVE_PHRASES = [t for t in POSITIVE_PHRASES if t not in POSITIVE_ASCII]

# 中文信号：子串命中（无需边界）
POSITIVE_CJK = [
    "人工智能", "大模型", "大语言模型", "语言模型", "智能体", "生成式", "生成ai",
    "提示词", "提示工程", "检索增强", "微调", "机器学习", "深度学习", "神经网络",
    "强化学习", "计算机视觉", "自然语言", "图像识别", "目标检测", "人脸识别",
    "语音识别", "语音合成", "数字人", "多模态", "具身", "知识图谱", "推荐算法",
    "智能驾驶", "自动驾驶", "模型部署", "推理加速", "文生图", "文生视频", "图生图",
    "视频生成", "图像生成", "智能客服", "智能问答", "向量数据库", "语义理解",
    "ai绘画", "ai漫剧", "ai编程", "ai应用", "ai开发", "ai产品", "ai投资", "ai创作",
    "ai变现", "ai工具", "ai测试", "ai落地", "ai工程", "ai赋能", "ai技术", "ai智能",
    "豆包", "扣子", "通义", "文心", "混元", "星火", "即梦", "可灵", "智谱",
    "sora", "netflix",  # 保留位（不参与判定增强，仅占位避免后续误删）
]
POSITIVE_CJK = [t for t in POSITIVE_CJK if t != "netflix"]

# 反向信号：仅用于解释/自测（判定上「有正向即算 AI」，故不构成否决）
NEGATIVE_ASCII = [
    "hadoop", "hbase", "hive", "spark", "flink", "clickhouse", "kafka", "zookeeper",
    "docker", "kubernetes", "k8s", "devops", "spring", "springboot", "springcloud",
    "mybatis", "mysql", "redis", "mongodb", "nginx", "vue", "react", "angular",
    "django", "android", "ios", "golang", "java", "linux", "ctf",
]
NEGATIVE_CJK = [
    "大数据", "数据仓库", "数据分析", "数据库", "微服务", "云原生", "容器化",
    "运维", "软考", "系统规划与管理师", "网络工程师", "网络安全", "渗透测试",
    "影视后期", "影视特效", "剪辑", "平面设计", "ui设计", "前端", "小程序",
    "中间件", "高并发", "分布式", "架构师", "全栈", "服务器", "网络工程",
]

# ------------------------------------------------ 方向门（2026-10-07 第三版，用户口径订正）
# 用户口径（2026-10-07 追加）：「我关注的就是 AI 方向 —— AI 开发 / AI coding / AI Agent /
# AI 应用 / AI 多智能体 / AI 自媒体 / AI 教育 / AI 出海 / AI 绘画 / AI 视频 / AI 编程助手 /
# AI 副业变现 …… 只要是 AI 的都要」。真正不要的只有两类：
#   ① 非 AI 内容（Hadoop / Spring Cloud / K8s / Docker / 软考 / 影视后期）⇒ 由 AI 门 is_ai_topic 挡；
#   ② 「纯入门/通识基础课」（Python3入门人工智能 / 人工智能导论 / 机器学习基础 / 零基础学深度学习）
#      ⇒ 标题同时出现「基础修饰词」与「学科词」且**无**任何「应用/落地/职业方向」信号时剔除。
# 判据 v3 =  mod ∧ subject ∧ ¬applied ⇒ FOUNDATION（剔）；其余一律放行（宽档，避免误杀 AI 应用向）。
# 注：本函数只判「方向」；实际使用须与 AI 门叠加（最终判据 = is_ai_topic ∧ is_direction_ai）。
# 第二版（只留 FORWARD 前沿载体）留存可查：git 8fef781。
FORWARD_ASCII = [
    "llm", "llms", "genai", "aigc", "gpt", "chatgpt", "openai", "anthropic", "claude",
    "codex", "cursor", "copilot", "windsurf", "agent", "agents", "agentic", "rag",
    "langchain", "langgraph", "llamaindex", "llama", "qwen", "deepseek", "gemini",
    "mistral", "ollama", "vllm", "dify", "coze", "mcp", "lora", "finetune", "embedding",
    "multimodal", "prompt", "prompts", "huggingface", "stable-diffusion", "comfyui",
    "midjourney", "sora", "suno", "mlops",
]
FORWARD_CJK = [
    "大模型", "大语言模型", "语言模型", "智能体", "生成式", "生成ai", "提示词", "提示工程",
    "检索增强", "微调", "多模态", "向量数据库", "语义理解", "模型部署", "推理加速",
    "文生图", "文生视频", "图生图", "视频生成", "图像生成", "数字人", "具身",
    "ai应用", "ai开发", "ai产品", "ai工具", "ai变现", "ai落地", "ai工程", "ai赋能",
    "ai编程", "ai创作", "ai绘画", "ai漫剧", "ai客服", "智能客服", "智能问答",
    "豆包", "扣子", "通义", "文心", "混元", "星火", "即梦", "可灵", "智谱",
]
BACKWARD_ASCII = [
    "python", "opencv", "yolo", "pytorch", "tensorflow", "keras", "paddle", "sklearn",
    "scikit", "nlp", "tts", "asr", "ocr", "whisper", "transformers", "numpy", "pandas",
]
BACKWARD_CJK = [
    "入门", "基础", "零基础", "从零", "小白", "机器学习", "深度学习", "神经网络",
    "强化学习", "计算机视觉", "自然语言", "图像识别", "目标检测", "人脸识别",
    "语音识别", "语音合成", "推荐算法", "人工智能", "知识图谱",
]

# 方向门 v3（2026-10-07 第三版）三个词表 —— 判据单一源，勿在别处复制
FOUNDATION_MODIFIER = [                 # 基础修饰词：与学科词同现且无应用信号 ⇒ 判基础课
    "入门", "基础", "零基础", "从零", "小白", "导论", "科普", "通识", "速成", "初学",
]
AI_SUBJECT_CJK = [                      # 学科/技术面（「纯理论入门」多落此列）
    "人工智能", "机器学习", "深度学习", "神经网络", "强化学习", "计算机视觉",
    "自然语言", "图像识别", "目标检测", "人脸识别", "语音识别", "语音合成",
    "推荐算法", "知识图谱", "数据挖掘", "模式识别",
]
APPLIED_STRONG = [                      # 应用/落地/职业方向信号（任一命中 ⇒ 视为「能用」放行）
    # 通用落地形态（★不含裸「应用」二字：教材常用「算法与应用」，会假放行基础课）
    "实战", "项目", "开发", "落地", "工程", "搭建", "部署", "实现", "案例", "企业级",
    "工作流", "自动化", "提效", "产品", "设计", "测试", "运维", "架构", "课程设计",
    # 用户列举的 AI 方向（2026-10-07 追加）
    "编程", "写代码", "助手", "副业", "变现", "出海", "跨境", "自媒体", "运营", "营销",
    "获客", "增长", "教育", "教学", "培训", "讲师", "咨询", "顾问", "解决方案",
    "绘画", "插画", "漫画", "漫剧", "视频", "剪辑", "数字人", "口播", "配音", "写作",
    "文案", "小说", "游戏", "音乐", "音频", "翻译", "办公", "量化", "投资", "电商",
    "创业", "接单", "外包", "客服", "问答", "检索", "知识库", "推荐", "搜索",
    # 应用形态/技术载体
    "智能体", "多智能体", "微调", "提示词", "提示工程", "赋能", "行业",
]
APPLIED_ASCII = ["agent", "agents", "agentic", "rag", "coding", "workflow", "saas"]

_FWD_ASCII_RX = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in FORWARD_ASCII) + r")(?![a-z0-9])"
)
_BWD_ASCII_RX = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in BACKWARD_ASCII) + r")(?![a-z0-9])"
)
_APP_ASCII_RX = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in APPLIED_ASCII) + r")(?![a-z0-9])"
)

_ASCII_RX = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in POSITIVE_ASCII) + r")(?![a-z0-9])"
)


def _phrase_rx(phrase: str):
    parts = [re.escape(p) for p in re.split(r"[ _-]+", phrase.strip()) if p]
    return re.compile(r"(?<![a-z0-9])" + r"[ _-]+".join(parts) + r"(?![a-z0-9])")


_PHRASE_RXS = [(t, _phrase_rx(t)) for t in POSITIVE_PHRASES]
# 前沿短语（同属前沿载体）：vibe coding 等；2026-10-07 第二批补 "ai coding" 对齐中文「ai编程」
FORWARD_PHRASES = list(POSITIVE_PHRASES) + ["ai coding", "ai-coding"]
_FWD_PHRASE_RXS = [(t, _phrase_rx(t)) for t in FORWARD_PHRASES]


def _hits(text: str):
    """返回 (正向命中, 反向命中)；text 需已 lower()。"""
    pos = [t for t in POSITIVE_CJK if t in text]
    pos += _ASCII_RX.findall(text)
    pos += [t for t, rx in _PHRASE_RXS if rx.search(text)]
    neg = [t for t in NEGATIVE_CJK if t in text]
    neg += [t for t in NEGATIVE_ASCII if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text)]
    return sorted(set(pos)), sorted(set(neg))


def explain(text: str) -> dict:
    """给出判定与命中明细，便于排障与人工复核（绝不抛错）。"""
    t = (text or "").lower()
    pos, neg = _hits(t)
    if pos:
        v = "AI"
    elif neg:
        v = "NON_AI"
    else:
        v = "UNKNOWN"
    return {"verdict": v, "positive": pos, "negative": neg}


def verdict(text: str) -> str:
    return explain(text)["verdict"]


def is_ai_topic(text: str) -> bool:
    """严格档：只有显式 AI 信号才算 True（UNKNOWN 一律 False）。"""
    return bool(explain(text)["positive"])


def direction_explain(text: str) -> dict:
    """方向门 v3 明细。

    字段：modifier=基础修饰词、subject=学科/技术面、applied=应用/职业方向信号、
    forward=前沿载体词（大模型/Agent 等）。backward 为旧字段（= modifier ∪ subject），
    保留仅为兼容既有调用与排障输出。
    verdict ∈ {FOUNDATION, APPLIED, FRONTIER, OPEN}：仅 FOUNDATION 判剔。
    """
    t = (text or "").lower()
    mod = [x for x in FOUNDATION_MODIFIER if x in t]
    subj = [x for x in AI_SUBJECT_CJK if x in t]
    subj += _BWD_ASCII_RX.findall(t)          # pytorch/opencv/nlp 等框架与缩写
    app = [x for x in APPLIED_STRONG if x in t]
    app += _APP_ASCII_RX.findall(t)
    fwd = [x for x in FORWARD_CJK if x in t]
    fwd += _FWD_ASCII_RX.findall(t)
    fwd += [x for x, rx in _FWD_PHRASE_RXS if rx.search(t)]
    mod, subj, app, fwd = (sorted(set(x)) for x in (mod, subj, app, fwd))
    if mod and subj and not app:
        v = "FOUNDATION"      # 纯入门/通识基础课 ⇒ 剔
    elif app:
        v = "APPLIED"
    elif fwd:
        v = "FRONTIER"
    else:
        v = "OPEN"            # 无基础修饰词、也无强应用词 ⇒ 宽档放行
    return {"verdict": v, "modifier": mod, "subject": subj, "applied": app,
            "forward": fwd, "backward": sorted(set(mod) | set(subj))}


def is_direction_ai(text: str) -> bool:
    """方向门 v3（2026-10-07 第三版，用户口径订正）：只剔「纯入门/通识基础课」，其余放行。

    ★ 最终判据 = is_ai_topic(text) and is_direction_ai(text)（AI 门 ∧ 方向门）：
      非 AI 内容（Hadoop / Spring Cloud / K8s / Docker / 软考…）由 AI 门挡；
      本门只管方向（是否 AI 不在此判）。
    含「小白/入门」但同时有应用信号者**保留**（如「小白玩转AI大模型应用开发」命中「开发」）。
    """
    return direction_explain(text)["verdict"] != "FOUNDATION"


# ---------------------------------------------------------------- 自测

_POS_CASES = [
    "Python3入门人工智能，掌握机器学习+深度学习",
    "慕课《Python3系统入门人工智能》：人人都应该学会利用AI这个工具",
    "mktx-hagent-【Harness&Hermes】多智能体开发特训营",
    "林粒粒：小白玩转AI大模型应用开发",
    "PyTorch入门到进阶 实战计算机视觉与自然语言处理项目",
    "OpenCV三大经典项目实战 一次掌握计算机视觉核心技能",
    "AI 大模型应用开发实战（2026）",
    "DeepSeek 提示词工程课",
    "Stable Diffusion 从入门到精通 + ComfyUI 工作流",
    "Cursor + Claude Code 双工具实战",
    "AI+云原生：模型部署与推理加速",
    "深度学习之目标检测常用算法原理+实践精讲",
    "Vibe Coding 实战：用自然语言写代码",
]
_NEG_CASES = [
    "Hadoop 系统入门+核心精讲",
    "[尚硅谷]大数据技术之Hadoop视频 - 带源码课件",
    "A00966. Hadoop 3.x大数据开发实战(视频教学版)",
    "黑马-新版Hadoop课程",
    "Docker 系统性入门+进阶实践",
    "Docker环境下的企业级前后端分离项目部署与运维",
    "Kubernetes 入门到进阶实战，系统性掌握 K8s 生产实践",
    "Spring Cloud微服务架构 设计实现广告系统",
    "Go微服务入门到容器化实践，落地可观测微服务项目",
    "ES7+Spark 构建高匹配度搜索服务+千人千面推荐系统",
    "Flink+ClickHouse 玩转企业级实时大数据开发",
    "系统规划与管理师（软考高级）一站式通关课2026",
    "AE影视后期特效-遮罩/调色/抠像/MG动画/3D/Vlog制作（完结）",
    "Vue3+React 前端全栈实战",
]
# 负控：含 "ai" 子串但非整词，绝不能被当成 AI 信号
_BOUNDARY_CASES = [
    "email 营销自动化实战",
    "Airflow 数据管道入门",
    "Trailing 边缘计算教程",
    "Chain of Thought 论文精读",   # 注意：无 ai 整词 ⇒ 判非 AI（严格档的已知代价）
    "阿里云 ECS 运维实战",
    "",
    None,
]

# 方向门用例（2026-10-07 第三版）：用户口径=只剔「纯入门/通识基础课」，AI 各方向全留
_DIR_KEEP = [
    # 前沿 / 应用向（第二批已有）
    "mktx-hagent-【Harness&Hermes】多智能体开发特训营",
    "慕课体系课-【Harness&Hermes】多智能体开发特训营",
    "林粒粒：小白玩转AI大模型应用开发",
    "AI 大模型应用开发实战（2026）",
    "DeepSeek 提示词工程课",
    "Stable Diffusion 从入门到精通 + ComfyUI 工作流",
    "Cursor + Claude Code 双工具实战",
    "柠檬班AI大模型赋能软件测试与Agent开发实战课第三期",
    "AIGC 变现实战营",
    "ChatGPT 应用开发与 RAG 知识库搭建",
    "AI Agent 智能体开发",
    "AI coding 实战营：用 AI 写代码",
    # 2026-10-07 第三版新增：用户点名要的各 AI 方向（第二批严格档曾误杀）
    "AI 自媒体运营从0到1",
    "AI 出海变现实战营",
    "AI 教育产品设计与落地",
    "AI 绘画商业插画实战",
    "AI 视频制作与数字人应用",
    "AI 编程助手实战：用 Claude Code 干活",
    "AI 副业变现指南",
    "大模型应用开发工程师（岗位课）",
    "AI 电商客服机器人搭建",
    "AI 翻译工具在企业办公中的应用",
    # 宽档：AI 技术实操课（无基础修饰词，或带强应用词）⇒ 保留
    "PyTorch入门到进阶 实战计算机视觉与自然语言处理项目",
    "OpenCV三大经典项目实战 一次掌握计算机视觉核心技能",
    "深度学习之目标检测常用算法原理+实践精讲",
]
_DIR_DROP = [
    "Python3入门人工智能，掌握机器学习+深度学习",
    "慕课《Python3系统入门人工智能》：人人都应该学会利用AI这个工具",
    "【Python教程】Python3入门机器学习 经典算法与应用 轻松入行人工智能（完整版）",
    "人工智能导论",
    "机器学习基础",
    "零基础学深度学习",
    "零基础学机器学习：从原理到算法",
    "人工智能通识课",
]
# 组合门（最终判据 is_ai_topic ∧ is_direction_ai）回归：非 AI 必剔 / 用户 AI 方向必留
_COMBO_BAD = [
    "Hadoop 系统入门+核心精讲",
    "[尚硅谷]大数据技术之Hadoop视频 - 带源码课件",
    "Spring Cloud微服务架构 设计实现广告系统",
    "Kubernetes 入门到进阶实战，系统性掌握 K8s 生产实践",
    "Docker 系统性入门+进阶实践",
    "系统规划与管理师（软考高级）一站式通关课2026",
]
_COMBO_GOOD = [
    "AI 自媒体运营从0到1",
    "AI 出海变现实战营",
    "AI 教育产品设计与落地",
    "AI 绘画商业插画实战",
    "AI 视频制作与数字人应用",
    "AI 编程助手实战",
    "AI 副业变现指南",
    "AI Agent 智能体开发",
    "大模型应用开发工程师（岗位课）",
]
# 当日事故数据（R559 A/B 后保留的 8 条 AI 结果）⇒ 方向门后应剩 3 条
_DAY8 = [
    "Python3入门人工智能，掌握机器学习+深度学习",
    "033915_mksz418-Python3入门人工智能，掌握机器学习+深度学习",
    "慕课《Python3系统入门人工智能》",
    "慕课《Python3系统入门人工智能》：人人都应该学会利用AI这个工具",
    "【Python教程】Python3入门机器学习  经典算法与应用 轻松入行人工智能（完整版）",
    "mktx-hagent-【Harness&Hermes】多智能体开发特训营",
    "慕课体系课-【Harness&Hermes】多智能体开发特训营",
    "林粒粒：小白玩转AI大模型应用开发 - 20.【加餐】AI应用部署链接",
]


def selftest() -> int:
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    for c in _POS_CASES:
        ck(f"正例应判 AI: {c[:28]}", is_ai_topic(c))
    for c in _NEG_CASES:
        ck(f"非 AI 应判 False: {c[:28]}", not is_ai_topic(c))
    for c in _BOUNDARY_CASES:
        e = explain(c or "")
        ck(f"边界不得误判 AI(substr 非整词): {str(c)[:24]}",
           not is_ai_topic(c) or (c or "").lower().count("ai") == 0)
    # 负控：别名/解释面
    e = explain("Hadoop 系统入门+核心精讲")
    ck("explain(Hadoop).verdict==NON_AI", e["verdict"] == "NON_AI" and e["negative"])
    e2 = explain("AI 大模型实战 + Docker 部署")
    ck("正向后向并存仍判 AI", e2["verdict"] == "AI" and e2["negative"])
    ck("verdict(UNKNOWN)", verdict("某某综合训练营") == "UNKNOWN")
    # 与事故数据对账：今日 37 条里应保留 8 条（AI 侧）
    ck("事故回归:is_ai_topic 对 Hadoop 镜像全 False",
       all(not is_ai_topic(t) for t in _NEG_CASES))
    # 短语面：vibe coding 属 AI 圈词须命中；video coding 等近似词不得误命中
    ck("短语:'Vibe Coding 实战' 判 AI", is_ai_topic("Vibe Coding 实战：用自然语言写代码"))
    ck("短语:'vibe-coding' 连字符同样命中", is_ai_topic("vibe-coding 训练营"))
    ck("短语负控:'video coding 视频编码实战' 不误命中",
       not is_ai_topic("video coding 视频编码实战"))
    ck("短语负控:'encoding 编码原理' 不误命中", not is_ai_topic("encoding 编码原理"))
    # ---- 方向门 v3（2026-10-07 第三版）----
    for c in _DIR_KEEP:
        ck(f"方向门应保留: {c[:26]}", is_direction_ai(c))
    for c in _DIR_DROP:
        ck(f"方向门应剔(纯入门/通识基础课): {c[:26]}", not is_direction_ai(c))
    e3 = direction_explain("林粒粒：小白玩转AI大模型应用开发")
    ck("方向门:含小白但有应用信号(开发)仍留",
       e3["verdict"] == "APPLIED" and "小白" in e3["modifier"] and "开发" in e3["applied"])
    ck("方向门:空/None 不崩且宽档放行",
       is_direction_ai(None) and direction_explain(None)["verdict"] == "OPEN")
    _e4 = direction_explain("机器学习基础")
    ck("方向门:FOUNDATION 明细可见(mod+subject)",
       _e4["verdict"] == "FOUNDATION" and "基础" in _e4["modifier"] and "机器学习" in _e4["subject"])
    for b in _COMBO_BAD:
        ck(f"组合门:非AI必剔 {b[:22]}", not (is_ai_topic(b) and is_direction_ai(b)))
    for g in _COMBO_GOOD:
        ck(f"组合门:用户AI方向必留 {g[:22]}", is_ai_topic(g) and is_direction_ai(g))
    _kept = [t for t in _DAY8 if is_ai_topic(t) and is_direction_ai(t)]
    ck("事故回归:当日 8 条 AI keep 经方向门后剩 3 条", len(_kept) == 3)
    ck("事故回归:余 3 条均为智能体/大模型方向",
       all(("智能体" in t) or ("大模型" in t) for t in _kept))
    ck("事故回归:被剔者为人工智能/机器学习基础课",
       all(("机器学习" in t) or ("人工智能" in t) for t in _DAY8 if t not in _kept))
    print(json.dumps({"checks_ok": ok, "checks_failed": fail}, ensure_ascii=False))
    return 0 if fail == 0 else 1


def main(argv) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--explain" in argv:
        idx = argv.index("--explain")
        text = argv[idx + 1] if idx + 1 < len(argv) else ""
        print(json.dumps(explain(text), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
