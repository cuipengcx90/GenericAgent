
# R6 LLM成本分析报告

**执行时间**: 2026-04-23  
**任务**: 分析LLM调用成本分布、高消耗场景和质量瓶颈

---

## 一、成本总览（2026-04-23单日）

| 指标 | 数值 |
|------|------|
| 总估算成本 | **$9.22** |
| 总调用轮次 | 289轮 |
| 总数据量 | 1.9MB |
| 平均每轮成本 | $0.032 |

**估算方法**: 基于文件大小（字符数÷3≈token数），Claude Sonnet定价 input $3/1M + output $15/1M

---

## 二、TOP5高消耗会话

| 文件 | 轮次 | 大小 | 估算成本 | 均prompt | 时间段 |
|------|------|------|---------|---------|--------|
| model_responses_24260.txt | 79 | 524KB | **$2.45** | 5.0KB | 18:45-? |
| model_responses_68791.txt | 47 | 354KB | $1.63 | 5.7KB | 17:21-? |
| model_responses_30966.txt | 55 | 330KB | $1.55 | 4.6KB | 22:56-? |
| model_responses_24259.txt | 34 | 267KB | $1.36 | 6.5KB | 21:01-? |
| model_responses_98704.txt | 40 | 240KB | $1.24 | 5.1KB | 16:46-? |

**TOP5占比**: $8.23 / $9.22 = **89.3%**

---

## 三、成本热点深度分析

### 🔴 热点1: Prompt膨胀（最严重）

**案例**: model_responses_24260.txt（最贵会话）
- 首轮prompt: 1.4KB
- 平均prompt: 5.0KB
- **膨胀率: +257.7%**

**根因**: WORKING MEMORY累积
- 71/80轮（88.8%）包含`<history>`标签
- 70/80轮（87.5%）包含`<key_info>`标签
- 每轮自动注入完整历史 → prompt线性增长

**成本影响**:
- 假设无膨胀（恒定1.4KB）: 79轮 × 1.4KB ≈ $0.52
- 实际成本（平均5KB）: **$2.45**
- **膨胀导致成本放大4.7倍**

### 🟡 热点2: 长会话（次要）

**特征**: 
- 5个会话超过40轮（最长79轮）
- 长会话占总成本89.3%

**场景识别**:
- model_responses_24260.txt: 自主行动模式（[AUTO]触发）
- 其他长会话: 可能是复杂任务或调试循环

**优化空间**: 中等（需要任务拆解或中断机制）

### 🟢 热点3: 平均prompt过大

**数据**:
- 全局平均prompt: 5.2KB
- 最大单prompt: 6.5KB（model_responses_24259.txt）

**对比基准**: 简单对话通常<1KB，5KB说明包含大量上下文

---

## 四、质量瓶颈

### 问题1: 无精确token计数

**现状**:
- `llmcore._record_usage`只`print`到stdout，不持久化
- `langfuse_tracing.py`上传到云端，本地无法审计
- model_responses日志只有文本，无usage字段

**影响**: 无法精确分析cache命中率、真实成本、异常消耗

### 问题2: WORKING MEMORY设计缺陷

**问题**:
- 每轮自动注入完整history（无截断）
- key_info累积但无压缩机制
- 导致prompt膨胀 → 成本线性增长

**建议**: 见下节优化建议

---

## 五、可行优化建议

### 🎯 P0: 修复WORKING MEMORY膨胀（预期节省60%+成本）

**方案A: 滑动窗口**
```python
# 只保留最近N轮history（如N=10）
if len(history) > 10:
    history = history[-10:]
```

**方案B: 摘要压缩**
```python
# 每10轮压缩一次history为摘要
if turn % 10 == 0:
    history = summarize(history)
```

**方案C: 分层存储**
```python
# 关键信息存key_info（持久），临时信息存history（滑动窗口）
# 当前实现：两者都累积 → 改为key_info精简+history截断
```

**预期效果**: 
- prompt从5KB降至2KB → 成本降60%
- 最贵会话从$2.45降至$1.0

### 🎯 P1: 增加本地token计数持久化

**方案**: 修改`llmcore._record_usage`
```python
def _record_usage(usage, api_mode):
    # 原有print逻辑
    print(f"[Cache] ...")
    
    # 新增：写入JSON日志
    log_path = "temp/token_usage.jsonl"
    with open(log_path, 'a') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'pid': os.getpid(),
            'usage': usage,
            'api_mode': api_mode
        }, f)
        f.write('\n')
```

**价值**: 可精确审计成本、分析cache效率、发现异常

### 🎯 P2: 长会话熔断机制

**方案**: 在agent_loop中增加轮次限制
```python
if turn > 50:
    return "会话过长，建议拆分任务或人工介入"
```

**价值**: 防止失控循环、降低单会话成本上限

---

## 六、总结

| 维度 | 结论 |
|------|------|
| **成本水平** | 单日$9.2，月度预估$276（若每日相同） |
| **最大瓶颈** | WORKING MEMORY膨胀导致成本放大4.7倍 |
| **优化潜力** | P0方案可节省60%+成本 → 月度$110 |
| **数据盲区** | 无本地token计数，无法精确审计 |

**最高优先级行动**: 实施WORKING MEMORY滑动窗口（方案A），一次改动即可将成本从$9.2/天降至$3.7/天。
