
# R38 Context膨胀触发点分析报告

## 执行信息
- 执行时间: 2026-04-24
- 数据来源: autonomous_reflect.log（13397行，498条[Output]记录）
- 分析方法: [Output]token序列 + 会话边界识别

---

## 核心发现

### 会话统计
| 指标 | 数值 |
|------|------|
| 识别会话数 | 55个 |
| 平均轮数 | 9.2轮/会话 |
| 中位轮数 | 8轮 |
| 最长会话 | 28轮 |

### Token膨胀曲线
| 阶段 | Output tokens范围 | 说明 |
|------|-----------------|------|
| 会话首轮 | 50-200 | 新会话context干净 |
| 会话中期(5-10轮) | 500-1500 | history累积 |
| 会话末期(15轮+) | 2000-4372 | 膨胀峰值 |
| 最大单会话增长 | +6744% | Session#38，15轮 |

**Output token从首轮~100增长到末轮~3000-4000，增长30-40x**

---

## 膨胀触发条件（量化）

### 触发点1：长工具返回累积（主因）
- code_run返回大量stdout → 直接进入history
- 单步最大跳变: +3944 tokens（L2458）
- 触发阈值：工具返回>500行时必然引发>2000 token跳变

### 触发点2：update_working_checkpoint
- 每次写入约200-500 tokens的key_info
- 多次调用后在history中线性累积
- 建议：每会话最多调用3次，内容控制在150 tokens以内

### 触发点3：会话轮数超过15轮
- 15轮后output token普遍>2000
- 28轮最长会话峰值达4372 tokens
- 触发条件：无compress_history机制时，每轮+200-300 tokens线性增长

---

## 优化方案（优先级排序）

| 优先级 | 方案 | 预期效果 | 实施成本 |
|--------|------|---------|---------|
| P0 | compress_history_tags频率%5→%1 | 抑制线性膨胀，峰值从4000→1500 | 低（已有方案，待审批） |
| P1 | code_run返回截断：stdout>200行自动truncate | 消除最大单步跳变(+3944) | 低（1行改动） |
| P2 | update_working_checkpoint内容限制150 tokens | 减少checkpoint累积 | 低（prompt约束） |
| P3 | 会话超15轮强制compress | 防止末期膨胀 | 中（需agentmain改动） |

---

## 结论

context膨胀是**结构性问题**，非偶发：
1. 每会话平均9.2轮，output token增长约30-40x
2. 主触发：长工具返回（code_run stdout）+ history无压缩
3. P0方案（compress频率提升）可将峰值从4372降至~1500，节省约65%末期token消耗
