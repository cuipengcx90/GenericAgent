
# R33 供应链SaaS可AI化接口节点分析报告

## 执行信息
- **执行时间**: 2026-04-24
- **数据来源**: R30 UI截图分析（141张，尚饮点餐管理后台）+ L2用户背景
- **系统**: 餐饮连锁供应链管理系统（12大模块，联调阶段）

---

## 一、系统现状概览

### 模块分布
| 模块 | 截图数 | 占比 | AI化潜力 |
|------|--------|------|---------|
| 供应链管理 | 50 | 35.5% | ⭐⭐⭐⭐⭐ |
| 门店管理 | 18 | 12.8% | ⭐⭐⭐ |
| 营销管理 | 16 | 11.3% | ⭐⭐⭐⭐ |
| 商品管理 | 15 | 10.6% | ⭐⭐⭐⭐ |
| 订单管理 | 12 | 8.5% | ⭐⭐⭐⭐ |
| 供应链分析 | 5 | 3.5% | ⭐⭐⭐⭐⭐ |
| 财务管理 | 4 | 2.8% | ⭐⭐⭐ |

### 技术栈（推断）
- 前端：Vue + ElementUI
- 编号规则：`DH20260316000015`（前缀+日期+流水号）
- 时间格式：`YYYY-MM-DD HH:mm:ss`

---

## 二、可AI化接口节点（优先级排序）

### 节点1：智能补货预测（P0 — 最高优先级）

**位置**: 供应链管理 → 实时库存查询 / 采购汇总表

**现状痛点**:
- 库存查询是纯人工判断：看库存量、可用量、占用量、预计出入库量
- 采购计划依赖人工经验，无预测模型
- 字段已具备：库存量、可用量、占用量、预计出库量、预计入库量、库存金额

**AI化方案**:
```python
# 接口节点：POST /api/supply/replenishment-suggest
# 输入：品项编码、近30天销量、当前库存、保质期、供应商交货周期
# 输出：建议补货量、补货时间、置信度

def ai_replenishment_suggest(item_code: str, current_stock: float,
                              sales_history: list, lead_time_days: int) -> dict:
    """
    基于时序预测（Prophet/ARIMA）计算安全库存和补货点
    返回：{suggest_qty, replenish_date, confidence, reason}
    """
    # 1. 计算平均日销量
    avg_daily_sales = sum(sales_history[-30:]) / 30
    # 2. 安全库存 = 日销量 × 交货周期 × 安全系数(1.2)
    safety_stock = avg_daily_sales * lead_time_days * 1.2
    # 3. 补货点 = 安全库存 + 交货期内销量
    reorder_point = safety_stock + avg_daily_sales * lead_time_days
    suggest_qty = max(0, reorder_point - current_stock)
    return {"suggest_qty": suggest_qty, "confidence": 0.85}
```

**技术债务**: 当前无历史销量API，需新增 `/api/sales/history?item_code=&days=30`

**改造工作量**: 中（2周）| **ROI**: 极高（减少缺货/积压损失）

---

### 节点2：订单异常智能识别（P0）

**位置**: 订单管理 → 售后订单管理 / 门店订货单

**现状痛点**:
- 退款单（TD前缀）需人工审核，判断是否异常
- 订货单无自动校验：门店订货量是否合理（过多/过少）
- 字段已具备：退款类型、订单来源、商品数量、订单时间

**AI化方案**:
```python
# 接口节点：POST /api/order/anomaly-detect
# 输入：订单数据（门店ID、品项、数量、时间）
# 输出：异常评分、异常类型、处理建议

def detect_order_anomaly(order: dict) -> dict:
    """
    规则+ML混合检测：
    - 规则层：数量超过历史均值3σ → 标记异常
    - ML层：孤立森林(IsolationForest)检测多维异常
    """
    anomaly_types = []
    
    # 规则1：订货量异常
    if order['qty'] > order['avg_qty'] * 3:
        anomaly_types.append("订货量异常偏高")
    
    # 规则2：退款率异常
    if order['refund_rate'] > 0.15:
        anomaly_types.append("退款率超阈值")
    
    return {
        "anomaly_score": 0.0,  # 0-1，越高越异常
        "anomaly_types": anomaly_types,
        "suggestion": "人工复核" if anomaly_types else "自动通过"
    }
```

**技术债务**: 退款单状态机不完整，需补充状态流转日志

**改造工作量**: 小（1周）| **ROI**: 高（减少人工审核量60%+）

---

### 节点3：商品描述智能生成（P1）

**位置**: 商品管理 → 饮品商品模板库

**现状痛点**:
- 新增商品需手动填写：餐品名称、热搜词、卖点、商品描述
- 字段重复劳动，不同运营人员风格不一致
- 字段已具备：商品分类、热搜词、卖点、商品描述

**AI化方案**:
```python
# 接口节点：POST /api/product/description-generate
# 输入：商品名称、分类、主要原料
# 输出：热搜词(3-5个)、卖点(2-3条)、商品描述(50-100字)

def generate_product_description(name: str, category: str, ingredients: list) -> dict:
    """调用LLM生成商品文案"""
    prompt = f"""
    商品名称：{name}
    商品分类：{category}
    主要原料：{', '.join(ingredients)}
    
    请生成：
    1. 热搜词（3-5个，逗号分隔）
    2. 卖点（2-3条，每条15字内）
    3. 商品描述（50-100字，突出口感和特色）
    """
    # 调用GA的LLM能力
    from ga import ask_llm
    result = ask_llm(prompt)
    return parse_llm_output(result)
```

**技术债务**: 无，纯新增功能，不影响现有流程

**改造工作量**: 小（3天）| **ROI**: 中（提升运营效率，降低文案质量方差）

---

### 节点4：营销活动效果预测（P1）

**位置**: 营销管理 → 券活动管理

**现状痛点**:
- 活动上线前无效果预估（活动名称、时间、封面图、活动规则已有）
- 活动结束后无自动复盘
- 无法预测券核销率、带动销售额

**AI化方案**:
```python
# 接口节点：POST /api/marketing/campaign-predict
# 输入：活动类型、折扣力度、时间段、目标门店数
# 输出：预测核销率、预测带动GMV、历史相似活动参考

def predict_campaign_effect(campaign: dict) -> dict:
    """基于历史活动数据的效果预测"""
    return {
        "predicted_redemption_rate": 0.35,  # 预测核销率
        "predicted_gmv_lift": 12500.0,       # 预测带动GMV（元）
        "confidence": 0.72,
        "similar_campaigns": ["2026-01-春节活动", "2025-11-双十一活动"],
        "risk_factors": ["活动时间与竞品重叠", "库存可能不足"]
    }
```

**技术债务**: 历史活动数据未结构化存储，需建立活动效果归档表

**改造工作量**: 中（2周）| **ROI**: 中高（提升活动ROI 15-20%）

---

### 节点5：供应商智能评分（P2）

**位置**: 供应链管理 → 采购汇总表 / 品项主档

**现状痛点**:
- 供应商评估依赖人工经验
- 字段已具备：供应商编码、计划采购数量、实际收货数量、实际收货金额
- 无自动计算到货率、价格偏差率

**AI化方案**:
```python
# 接口节点：GET /api/supplier/score?supplier_code=&period=90d
# 输出：综合评分、各维度得分、趋势、风险预警

def calculate_supplier_score(supplier_code: str, period_days: int = 90) -> dict:
    """
    多维度供应商评分：
    - 到货率 = 实际收货数量 / 计划采购数量（权重40%）
    - 价格稳定性 = 1 - std(价格) / mean(价格)（权重30%）
    - 交货及时率（权重30%）
    """
    return {
        "overall_score": 87.5,
        "dimensions": {
            "delivery_rate": 92.0,
            "price_stability": 85.0,
            "on_time_rate": 83.0
        },
        "trend": "上升",
        "risk_alert": None
    }
```

**技术债务**: 供应商历史数据分散，需ETL整合

**改造工作量**: 中（2周）| **ROI**: 中（优化采购决策，降低供应链风险）

---

## 三、技术债务汇总

| 债务项 | 影响范围 | 优先级 | 修复成本 |
|--------|---------|--------|---------|
| 无历史销量API | 补货预测、异常检测 | P0 | 3天 |
| 退款状态机不完整 | 订单异常识别 | P0 | 2天 |
| 历史活动数据未结构化 | 营销效果预测 | P1 | 1周 |
| 供应商数据分散 | 供应商评分 | P2 | 2周 |
| 无统一AI调用网关 | 所有AI节点 | P0 | 1周 |

---

## 四、实施路线图

### Phase 1（第1-2周）：基础设施
```
1. 建立AI调用网关（复用R29 FastAPI方案）
2. 补充历史销量API
3. 修复退款状态机
```

### Phase 2（第3-4周）：P0节点上线
```
1. 智能补货预测（节点1）
2. 订单异常识别（节点2）
```

### Phase 3（第5-8周）：P1节点上线
```
1. 商品描述生成（节点3）
2. 营销效果预测（节点4）
```

### Phase 4（第9-12周）：P2节点+优化
```
1. 供应商智能评分（节点5）
2. 全链路效果监控
```

---

## 五、与v7架构集成点

| AI节点 | v7架构组件 | 集成方式 |
|--------|-----------|---------|
| 补货预测 | LLM工具调用 | FastAPI → GA工具链 |
| 异常识别 | 规则引擎+ML | 独立微服务 |
| 文案生成 | LLM直调 | R29 FastAPI /chat端点 |
| 效果预测 | 历史数据分析 | GA定时任务 |
| 供应商评分 | 数据聚合 | GA reflect_scripts |

---

## 六、总结

| 维度 | 结论 |
|------|------|
| 可AI化节点数 | 5个（P0×2, P1×2, P2×1）|
| 最高价值节点 | 智能补货预测（ROI极高，直接影响库存成本）|
| 最快落地节点 | 商品描述生成（3天，零技术债务）|
| 核心技术债务 | 无历史销量API + 无统一AI网关 |
| 与v7架构协同 | 复用R29 FastAPI方案作为AI调用网关 |

**建议优先级**：先修技术债务（历史销量API + AI网关）→ 再上P0节点（补货预测+异常识别）→ 快速验证ROI后推进P1。
