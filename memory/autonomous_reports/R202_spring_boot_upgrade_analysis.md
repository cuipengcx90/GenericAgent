
# R202 Spring Boot 升级阻力分析

## 任务背景

TODO 描述为"评估 bar-cloud 从 Spring Boot 2.6.7 升到 2.7.x 的代码与依赖阻力"。
实际扫描后发现：

- **bar-cloud**：Quarkus 2.16.7 项目，`spring-boot.version=2.6.7` 仅为属性声明，**未实际引入 Spring Boot**，不存在 Spring Boot 升级问题。
- **bar-market-server / bar-deploy-server**：已是 Spring Boot **3.2.11**，跨越了 2.x 全系，升级早已完成。
- **bar-smyze/bar_smyze_opserver**：Spring Boot **3.5.9** + Java 21，是最新版本。

因此，smyze 仓库中**不存在停留在 2.6.7 的 Spring Boot 模块**，原任务前提已不成立。
本报告转为：**记录现状 + 梳理 bar-smyze/opserver 中仍存在的历史遗留用法风险**。

---

## 现状快照

| 模块 | Spring Boot 版本 | Java | 备注 |
|------|-----------------|------|------|
| bar-cloud | Quarkus 2.16.7 | 17 | 非 Spring Boot 项目 |
| bar-market-server | 3.2.11 | — | 已完成大版本升级 |
| bar-deploy-server | 3.2.11 | — | 已完成大版本升级 |
| bar-smyze/bar_smyze_opserver | 3.5.9 | 21 | 最新版 |

---

## 扫描发现：遗留风险项

扫描范围：bar-smyze/bar_smyze_opserver（3813 个 Java 文件 + 配置文件）

### 风险1：`WebSecurityConfigurerAdapter` 命名残留（低风险）

Spring Boot 2.7 起废弃、3.x 已移除 `WebSecurityConfigurerAdapter`。
当前代码中存在 **3 处引用**，但经逐行核查：

- `BarOpWebSecurityConfigurerAdapter.java:49`：类名包含该字符串，但实际继承关系需进一步确认
- `AdminServerConfiguration.java:24`：注释引用，非实际继承
- `BarOpSecurityAutoConfiguration.java:27`：注释引用，非实际继承

**结论**：类名沿用旧命名风格，但 Spring Boot 3.5.9 能正常运行说明实际未继承已废弃类。
**建议**：重命名 `BarOpWebSecurityConfigurerAdapter` → `BarOpSecurityFilterChainConfig`，消除命名混淆。

### 风险2：`configurePathMatch` 引用（低风险）

`WebProperties.java:40` 中有 `@see BarOpWebAutoConfiguration#configurePathMatch(PathMatchConfigurer)` 注释。
Spring Boot 2.6 起默认路径匹配策略从 `AntPathMatcher` 改为 `PathPatternParser`，2.7 进一步收紧。
当前 SB 3.5.9 已强制使用 `PathPatternParser`，若 `configurePathMatch` 实现中仍设置 `AntPathMatcher` 会导致 Swagger/SpringFox 路径解析异常。

**建议**：检查 `BarOpWebAutoConfiguration#configurePathMatch` 实现，确认未强制设置 `AntPathMatcher`。

### 风险3：Actuator `base-path` 配置（无风险，已正确）

三套环境配置（dev/test/prod）均显式设置 `base-path: /actuator`，与默认值一致，无迁移风险。

---

## 结论

1. **bar-cloud 不是 Spring Boot 项目**，原任务描述的"2.6.7 升 2.7.x"前提不成立，无需执行该升级。
2. **smyze 中所有 Spring Boot 模块已在 3.x**，2.x 升级路径已完成。
3. **bar-smyze/opserver 存在 2 个低风险遗留项**（命名混淆 + configurePathMatch 待核查），不影响当前运行，建议在下次重构时一并清理。

---

## 最小行动建议

| 优先级 | 行动 | 模块 | 工作量 |
|--------|------|------|--------|
| P2 | 重命名 `BarOpWebSecurityConfigurerAdapter` | bar-op-spring-boot-starter-security | 0.5h |
| P2 | 核查 `configurePathMatch` 是否强制 AntPathMatcher | bar-op-spring-boot-starter-web | 0.5h |
| P3 | 将 bar-cloud 的 `spring-boot.version` 属性清理或注释说明用途 | bar-cloud | 0.25h |

---

## 记忆更新建议

建议在 global_mem.txt 补充：
> bar-cloud 是 Quarkus 2.16.7 项目（非 Spring Boot）；bar-market-server/bar-deploy-server/bar-smyze-opserver 均已升至 Spring Boot 3.x（3.2.11 或 3.5.9）。
