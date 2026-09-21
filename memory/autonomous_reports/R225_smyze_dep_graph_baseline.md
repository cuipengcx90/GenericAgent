
# R225 smyze依赖图谱基线快照建立

## 执行摘要

已使用 `smyze_dep_graph_analyzer.py` 对 `~/develop/WorkSpaces/idea/smyze` 建立依赖图谱基线快照，并验证 `--diff` 功能可用。

产出文件：
- 基线快照：`./smyze_dep_baseline.json`
- 报告：`./R225_smyze_dep_graph_baseline.md`

---

## 一、执行过程

### 1.1 工具定位与参数确认
已确认工具路径：
- `/Users/cuip/projects/GenericAgent/temp/smyze_dep_graph_analyzer.py`

帮助信息确认支持：
- `path`：扫描根目录
- `--output`：输出 JSON 文件
- `--diff OLD NEW`：比较两个结果文件

### 1.2 基线生成
执行：
```bash
python3 smyze_dep_graph_analyzer.py ~/develop/WorkSpaces/idea/smyze --output ./smyze_dep_baseline.json
```

执行结果：
- 返回码：`0`
- 基线文件已成功生成

### 1.3 diff功能验证
采用“同文件复制后比较”的无副作用验证方式：

```bash
python3 smyze_dep_graph_analyzer.py --diff smyze_dep_baseline.json smyze_dep_baseline_copy.json
```

返回结果：
- 返回码：`0`
- `added_dependencies = []`
- `removed_dependencies = []`
- `new_conflicts = []`
- `resolved_conflicts = []`

说明 `--diff` 功能工作正常，可用于后续增量比较。

---

## 二、基线结果概览

| 指标 | 数值 |
|------|------|
| total_modules | 27 |
| total_artifacts | 25 |
| internal_edges | 80 |
| cycles | 0 |
| version_conflicts | 0 |

### 2.1 结论解读

1. **模块规模**：当前 smyze 扫描结果包含 27 个模块节点、25 个 artifact 节点
2. **内部依赖边**：共识别 80 条内部依赖关系
3. **循环依赖**：未发现循环依赖（`cycles = 0`）
4. **版本冲突**：未发现版本冲突（`version_conflicts = 0`）

这说明当前依赖结构在“循环依赖”和“版本冲突”两个高风险维度上状态良好，可作为后续变更的稳定基线。

---

## 三、依赖结构观察

### 3.1 出度最高模块 Top 10

| 模块 | 直接依赖数 |
|------|-----------:|
| bar-op-dependencies | 15 |
| bar-op-module-member | 14 |
| bar-op-server | 14 |
| bar-op-module-pay | 11 |
| bar-op-module-system | 11 |
| bar-op-module-infra | 10 |
| bar-agent-server | 1 |
| bar-op | 1 |
| bar-op-module-pay-api | 1 |
| bar-qmai-service | 1 |

### 3.2 结构性洞察

#### A. `bar-op-*` 体系是核心依赖中心
从出度分布看，依赖关系明显集中在 `bar-op-*` 体系，尤其是：

- `bar-op-dependencies`
- `bar-op-server`
- `bar-op-module-member`
- `bar-op-module-pay`
- `bar-op-module-system`
- `bar-op-module-infra`

说明 `bar-op` 是当前 smyze 代码库内最重要的依赖骨架，后续任何依赖升级、starter收敛、模块拆分，都应优先关注这一簇。

#### B. `bar-op-dependencies` 像“BOM/聚合依赖中心”
`bar-op-dependencies` 直接关联 15 个依赖目标，具备明显“统一版本/统一依赖管理入口”特征。

风险提示：
- 一旦其版本策略调整，影响面会很广
- 后续做依赖升级时，应优先生成 diff 并重点审查该节点变化

#### C. `bar-op-server` 承担业务聚合入口角色
`bar-op-server` 直接依赖多个业务模块，包括：
- decoration
- information
- infra
- masterdata
- member
- pay
- product
- promotion
- statistics
- store
- supply-chain
- system
- trade
- protection

这类模块通常是“集成层/装配层”，适合重点监控：
- 新增依赖是否失控
- 是否形成横向耦合扩大
- 是否变成架构膨胀入口

#### D. 当前结构“复杂但干净”
虽然总依赖边达到 80 条，但没有出现：
- 循环依赖
- 版本冲突

这说明当前并非“简单系统”，而是“规模不小但还算受控”的状态，适合尽快把本次结果固化为基线，用于持续 diff。

---

## 四、样例邻接关系

### 4.1 `bar-op-server`
直接依赖：
- bar-op-module-decoration
- bar-op-module-information
- bar-op-module-infra
- bar-op-module-masterdata
- bar-op-module-member
- bar-op-module-pay
- bar-op-module-product
- bar-op-module-promotion
- bar-op-module-statistics
- bar-op-module-store
- bar-op-module-supply-chain
- bar-op-module-system
- bar-op-module-trade
- bar-op-spring-boot-starter-protection

### 4.2 `bar-op-module-system`
直接依赖：
- bar-op-module-infra
- bar-op-spring-boot-starter-biz-data-permission
- bar-op-spring-boot-starter-biz-ip
- bar-op-spring-boot-starter-biz-tenant
- bar-op-spring-boot-starter-excel
- bar-op-spring-boot-starter-job
- bar-op-spring-boot-starter-mq
- bar-op-spring-boot-starter-mybatis
- bar-op-spring-boot-starter-redis
- bar-op-spring-boot-starter-security
- bar-op-spring-boot-starter-test

### 4.3 `bar-op-module-infra`
直接依赖：
- bar-op-spring-boot-starter-biz-tenant
- bar-op-spring-boot-starter-excel
- bar-op-spring-boot-starter-job
- bar-op-spring-boot-starter-monitor
- bar-op-spring-boot-starter-mq
- bar-op-spring-boot-starter-mybatis
- bar-op-spring-boot-starter-redis
- bar-op-spring-boot-starter-security
- bar-op-spring-boot-starter-test
- bar-op-spring-boot-starter-websocket

---

## 五、可执行建议

### 5.1 立即落地
将 `smyze_dep_baseline.json` 作为后续比较基线保留，后续依赖调整后统一执行：

```bash
python3 smyze_dep_graph_analyzer.py ~/develop/WorkSpaces/idea/smyze --output new.json
python3 smyze_dep_graph_analyzer.py --diff smyze_dep_baseline.json new.json
```

### 5.2 建议监控重点
优先关注以下模块的 diff 变化：
- `bar-op-dependencies`
- `bar-op-server`
- `bar-op-module-member`
- `bar-op-module-pay`
- `bar-op-module-system`
- `bar-op-module-infra`

### 5.3 后续演进方向
后续如果继续深化，可增加：
1. 关键模块入度/出度排行
2. 聚合层依赖膨胀趋势
3. starter复用率与重复依赖识别
4. BOM/parent/模块层级的依赖收敛建议

---

## 六、任务完成判定

TODO要求：
- [x] 运行 `smyze_dep_graph_analyzer.py --output` 建立 baseline.json
- [x] 存档基线结果
- [x] 验证 `--diff` 功能可用
- [x] 产出基线报告

结论：**任务完成**。

---

## 七、记忆更新建议

```text
[smyze依赖图谱] 已建立基线快照 ./smyze_dep_baseline.json；
扫描结果：27模块/25 artifacts/80内部依赖边/0循环依赖/0版本冲突；
依赖中心集中在 bar-op-*，其中 bar-op-dependencies(15)、bar-op-server(14)、bar-op-module-member(14) 最关键；
--diff 功能已实测可用，可用于后续依赖变更增量分析。
```
