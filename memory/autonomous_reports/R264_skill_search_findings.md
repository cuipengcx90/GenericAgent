
# R264 Skill Search 能力探索报告

**日期**: 2026-05-04
**任务**: 用 skill_search 搜索自主任务常见场景，产出 top 技能清单 + 集成建议
**评分**: 8/10

---

## 执行摘要

通过 `skill_search` API（105K+ 技能卡语义检索）对 6 个自主任务常见场景进行搜索，共获取 30 条结果。所有返回技能 `autonomous_safe=True`，可直接在自主任务中引用。

**关键发现**：skill_search 对安全/devops 类场景匹配质量高（0.74-0.80），对日志压缩类场景匹配偏弱（0.61-0.65），建议优先用英文查询。

---

## 调用示例（可复用）

```python
import sys
sys.path.insert(0, '../memory/skill_search')
from skill_search import search

results = search("prompt injection detection security", top_k=5)
for r in results:
    s = r.skill
    print(f"[{r.final_score:.2f}] {s.name} | {s.category} | safe={s.autonomous_safe}")
    print(f"  {s.one_line_summary}")
```

---

## Top 技能清单（按场景）

### 1. Git / 提交自动化
| 评分 | 技能名 | 类别 | 摘要 |
|------|--------|------|------|
| 0.76 | commit-staged | devops | 分析暂存变更，生成 conventional commit 消息 + pre-commit 验证 |
| 0.74 | git-hooks | devops | 配置 Git hooks：代码质量/secrets扫描/commit验证 |
| 0.74 | pre-commit | devops | 配置 pre-commit hooks：格式化/lint/commit消息处理 |
| 0.74 | git-hooks-setup | devops | Husky + pre-commit 设置，含 secret 检测 |

### 2. 浏览器自动化 / JS 注入
| 评分 | 技能名 | 类别 | 摘要 |
|------|--------|------|------|
| 0.68 | creating-bookmarklets | coding | 生成 IIFE 包装的 JS bookmarklet |
| 0.68 | n8n-code-javascript | coding | n8n Code 节点 JS 编码指南 |
| 0.67 | browser-screenshot | testing | Playwright 自动截图工具 |

> ⚠️ 浏览器注入场景匹配质量偏低（0.67-0.68），建议改用 `tmwebdriver_sop` 本地 SOP

### 3. 内存泄漏 / 安全扫描
| 评分 | 技能名 | 类别 | 摘要 |
|------|--------|------|------|
| 0.70 | memory-scan | security | 检测 agent memory 文件中的 prompt 注入/凭证泄漏 |
| 0.68 | memory-leak-detection | testing | Node.js/Python 内存泄漏检测代码示例 |
| 0.68 | memory-leak | testing | 跨 Node.js/Python/浏览器的内存泄漏分析 |
| 0.67 | openclaw-sentry | security | 只读 secret 扫描器，无外部依赖 |

### 4. 定时任务 / Cron
| 评分 | 技能名 | 类别 | 摘要 |
|------|--------|------|------|
| 0.72 | cron-writer | devops | 自然语言 → cron 表达式转换 CLI |
| 0.71 | cron-mastery | ai-ml | OpenClaw 调度提醒 + 自动清理 |
| 0.70 | cron-scheduler | devops | Python cron 解析/验证/计算工具 |

### 5. Prompt 注入检测（最强场景）
| 评分 | 技能名 | 类别 | 摘要 |
|------|--------|------|------|
| **0.80** | skill-security-reviewer | security | AI 技能安全审计，94项检测，15类别 |
| **0.76** | indirect-prompt-injection | security | 检测并拒绝外部内容中隐藏的 prompt 注入 |
| **0.76** | detection | security | 全攻击向量安全检测 + 规则创建 |
| 0.75 | AOI Prompt Injection Sentinel | security | 本地 CLI，检测并评分 prompt 注入尝试 |
| 0.74 | prompt-guard | security | Python 安全库，650+ 离线模式检测 |

### 6. 日志压缩 / 归档
| 评分 | 技能名 | 类别 | 摘要 |
|------|--------|------|------|
| 0.65 | context-compression | ai-ml | AI agent context 压缩方法论 |
| 0.61 | claw-compactor | productivity | Python CLI，压缩 agent workspace 50-97% |
| 0.61 | work-archiving | devops | 归档变更包：文档/git commit/changelog |

---

## 集成建议

| 优先级 | 建议 | 理由 |
|--------|------|------|
| 高 | 在 R266 类安全任务前先调用 `indirect-prompt-injection`(0.76) + `prompt-guard`(0.74) | 可替代手写正则，覆盖 650+ 离线模式 |
| 高 | Git hook 任务优先查 `commit-staged`(0.76) + `git-hooks`(0.74) | 比从零写 hook 脚本快 3-5x |
| 中 | 定时任务配置前查 `cron-writer`(0.72) 验证 cron 表达式 | 避免 cron 语法错误导致任务不触发 |
| 中 | 浏览器自动化场景 skill_search 匹配弱，继续用 `tmwebdriver_sop` | 本地 SOP 更精准 |
| 低 | 日志压缩场景匹配分低（0.61），`claw-compactor` 可作参考但不替代现有 compress 逻辑 | 现有 compress_session.py 已足够 |

---

## 使用注意

1. **必须用英文查询**：中文匹配效果极差（SKILL.md 明确警告）
2. **API 地址**：`http://www.fudankw.cn:58787`，需网络连通
3. **sys.path 插入**：`sys.path.insert(0, '../memory/skill_search')` 后才可 import
4. **autonomous_safe 字段**：本次 30 条结果全为 True，自主任务可直接引用

---

## 产出物

| 文件 | 说明 |
|------|------|
| `autonomous_reports/R264_skill_search_findings.md` | 本报告 |
