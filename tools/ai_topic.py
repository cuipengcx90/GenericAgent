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

_ASCII_RX = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in POSITIVE_ASCII) + r")(?![a-z0-9])"
)


def _phrase_rx(phrase: str):
    parts = [re.escape(p) for p in re.split(r"[ _-]+", phrase.strip()) if p]
    return re.compile(r"(?<![a-z0-9])" + r"[ _-]+".join(parts) + r"(?![a-z0-9])")


_PHRASE_RXS = [(t, _phrase_rx(t)) for t in POSITIVE_PHRASES]


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
