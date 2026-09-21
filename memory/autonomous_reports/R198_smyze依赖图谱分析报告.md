# R198 smyze项目依赖关系深度分析+图谱工具

## 任务
对应 TODO（第11行）：
`产出 | smyze项目依赖关系深度分析+图谱工具 | 基于R154的15个项目架构→深挖项目间依赖关系(Maven/npm)→识别循环依赖/版本冲突/架构风险→产出依赖图谱工具+可视化报告`

## 数据概览

- **扫描根目录**: `/Users/cuip/develop/WorkSpaces/idea/smyze`
- **排除范围**: ruoyi、Quokka（第三方框架，非自研）
- **pom.xml 总数**: 27个
- **解析成功**: 27/27
- **识别 artifact**: 25个
- **内部依赖边**: 80条
- **循环依赖**: 0个 ✅
- **外部版本分散点**: 2个 ⚠️

## 项目层级结构

### 顶层独立服务（入度=0，无被依赖）
| 项目 | 说明 |
|------|------|
| bar-cloud | 云服务层（qmai/大屏/告警） |
| bar-market-server | 市场/营销服务 |
| bar-deploy-server | 部署服务 |
| bar-service | 基础服务层 |
| bar-agent | AI工作流代理层 |
| bar-op | 运营后台（若依二开） |

### 核心被依赖模块（高入度）
| 模块 | 被依赖方 | 说明 |
|------|----------|------|
| bar-op-module-infra | 10+ 模块 | 基础设施层，最高被依赖 |
| bar-op-common | 多模块 | 公共工具类 |
| bar-op-module-system | 多模块 | 系统管理模块 |
| bar-agent-io | bar-agent-server | Agent IO接口层 |
| bar-qmai-io | bar-qmai-service | QMAI IO接口层 |
| bar-service-io | bar-service-material | 服务IO接口层 |

### 典型依赖链
```
bar-op
  └─ bar-op-dependencies
       ├─ bar-op-common
       ├─ bar-op-spring-boot-starter-*（15个starter）
       └─ ...

bar-op-server
  ├─ bar-op-module-system
  │    └─ bar-op-module-infra（核心基础设施）
  ├─ bar-op-module-pay
  │    ├─ bar-op-module-member
  │    └─ bar-op-module-pay-api
  └─ bar-op-module-trade
       └─ bar-op-module-trade-api

bar-agent-server → bar-agent-io
bar-qmai-service → bar-qmai-io
bar-service-material → bar-service-io
```

## 循环依赖分析

**结论：0个循环依赖** ✅

smyze自研模块间依赖关系为有向无环图（DAG），架构分层清晰：
- IO接口层（*-io）作为叶节点，只被上层服务依赖
- starter层作为横切关注点，被业务模块依赖
- 业务模块间通过 API 子模块（*-api）解耦，避免直接循环

## 版本冲突分析

### ⚠️ 发现2个外部依赖版本分散点

| 依赖 | 版本列表 | 风险等级 |
|------|----------|----------|
| `cn.hutool:hutool-all` | 5.8.4 / 5.8.18 / 5.8.20 | 中风险 |
| `io.quarkiverse.mybatis:quarkus-mybatis-plus` | 1.0.7 / 1.0.8 | 低风险 |

**hutool-all 分析**：
- 5.8.4 → 5.8.18 → 5.8.20 跨越多个小版本
- 不同子项目引用不同版本，可能导致运行时类冲突
- 建议：在父 pom 的 `<dependencyManagement>` 中统一锁定到 5.8.20

**quarkus-mybatis-plus 分析**：
- 1.0.7 vs 1.0.8 差异极小，低风险
- 建议：统一到 1.0.8

### 版本表达式说明
大量内部依赖使用 `${revision}` 或 `${project.parent.version}` 占位符，属于 Maven CI Friendly Versions 标准用法，不计入冲突。

## 架构风险评估

| 风险项 | 等级 | 说明 |
|--------|------|------|
| 循环依赖 | ✅ 无 | DAG结构健康 |
| hutool版本分散 | ⚠️ 中 | 3个版本并存 |
| quarkus版本分散 | ℹ️ 低 | 相邻小版本 |
| bar-op模块耦合度 | ⚠️ 中 | infra层被10+模块依赖，改动影响面大 |
| IO接口层隔离 | ✅ 良好 | *-io模块作为接口边界，解耦效果好 |

## 与R166对比

| 维度 | R166（全量含ruoyi） | R198（自研模块） |
|------|---------------------|-----------------|
| pom数量 | 141个 | 27个 |
| 版本冲突 | 30个（含ruoyi内部） | 2个（外部依赖） |
| 循环依赖 | 未检测 | 0个 |
| 分析深度 | 跨仓库宏观 | 自研模块精细图谱 |

## 产出物

| 文件 | 说明 |
|------|------|
| `smyze_dep_graph_analyzer.py` | 依赖图谱分析工具，支持自定义根目录 |
| `smyze_dep_graph.json` | 本次分析完整数据（edges/cycles/conflicts） |

### 工具用法
```bash
# 分析默认路径
python smyze_dep_graph_analyzer.py

# 分析指定路径
python smyze_dep_graph_analyzer.py /path/to/smyze
```

## 改进建议

### 短期（1周）
1. 在 `bar-op/pom.xml` 的 `<dependencyManagement>` 中统一锁定 hutool-all 到 5.8.20
2. 统一 quarkus-mybatis-plus 到 1.0.8

### 中期（1个月）
3. 为 bar-op-module-infra 建立变更影响评估流程（改动前先跑依赖图谱）
4. 将本工具集成到 CI，每次 PR 自动输出依赖变更 diff

### 长期
5. 考虑将 bar-op-spring-boot-starter-* 系列提取为独立 BOM，降低 starter 层的版本管理复杂度
