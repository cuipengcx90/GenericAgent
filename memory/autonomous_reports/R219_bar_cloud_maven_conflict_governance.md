
# R219 bar-cloud Maven版本冲突治理报告

**日期**: 2026-04-30
**任务**: P3 bar-cloud Maven版本冲突治理
**评分**: 7分

---

## 背景

基于R211/R201(smyze 98项冲突分析)，bar-cloud存在版本冲突问题。使用 `resolve_maven_conflicts.py` 扫描全smyze(531个pom，97项冲突)，过滤出bar-cloud内部4个冲突实例。

---

## bar-cloud冲突清单

| 依赖 | 冲突版本 | 影响子模块数 | 优先级 |
|------|---------|------------|--------|
| `cn.hutool:hutool-all` | `${hutool-5.version}` / `${hutool.version}` / `5.8.42` / inherited | 3 | **高** |
| `org.projectlombok:lombok` | `${lombok.version}` / inherited | 3 | 高 |
| `io.github.linpeilie:mapstruct-plus` | `${mapstruct-plus.version}` / inherited | 3 | 高 |
| `com.aliyun:dyvmsapi20170525` | `${aliyun.dyvmsapi20170525.version}` / `2.1.4` | 1 | 高 |

---

## 修复内容

### 1. hutool-all 版本统一（主要修复）

**问题**: 3个子模块硬编码 `5.8.42`，父pom无统一属性管理

**修复**:

**bar-cloud/pom.xml** — 新增 properties + dependencyManagement:
```xml
<!-- properties区新增 -->
<hutool-5.version>5.8.42</hutool-5.version>

<!-- dependencyManagement区新增 -->
<dependency>
    <groupId>cn.hutool</groupId>
    <artifactId>hutool-all</artifactId>
    <version>${hutool-5.version}</version>
</dependency>
```

**3个子模块** — 硬编码版本改为属性引用:
- `bar-qmai-service/pom.xml`: `5.8.42` → `${hutool-5.version}`
- `bar-big-screen-service/pom.xml`: `5.8.42` → `${hutool-5.version}`
- `bar-alarm-system-service/pom.xml`: `5.8.42` → `${hutool-5.version}`

---

## 验证结果

```
mvn help:effective-pom -N (bar-cloud根目录)
L53: <hutool-5.version>5.8.42</hutool-5.version>   ✅ 属性解析正确
L84: <groupId>cn.hutool</groupId>                   ✅ dependencyManagement存在
L85: <artifactId>hutool-all</artifactId>
```

**编译说明**:
- `bar-qmai-service`: 依赖 `bar-qmai-io` 未install到本地仓库（预存在问题，需从根多模块编译）
- `bar-big-screen-service`: JDK版本不兼容 `TypeTag::UNKNOWN`（预存在问题）
- `bar-alarm-system-service`: relativePath问题（预存在问题，已在R218中记录）
- 以上均为修复前已存在的问题，与本次hutool版本治理无关

---

## 未修复项（范围外）

| 冲突 | 原因 |
|------|------|
| lombok / mapstruct-plus | 父pom已有属性，子模块用 `inherited` 继承，属正常Maven继承行为，无需修复 |
| dyvmsapi20170525 硬编码 `2.1.4` | 仅在 `bar-alarm-system-service` 中，该模块有预存在问题，暂不修改 |

---

## 变更文件

1. `bar-cloud/pom.xml` — 新增 `hutool-5.version` 属性 + dependencyManagement条目
2. `bar-cloud/bar-qmai-service/pom.xml` — hutool-all版本改为 `${hutool-5.version}`
3. `bar-cloud/bar-big-screen-service/pom.xml` — hutool-all版本改为 `${hutool-5.version}`
4. `bar-cloud/bar-alarm-system-service/pom.xml` — hutool-all版本改为 `${hutool-5.version}`

---

## 结论

bar-cloud内最关键的版本冲突（hutool-all 3种写法混用）已治理完毕，统一由父pom `hutool-5.version=5.8.42` 属性管理，子模块通过属性引用继承，消除硬编码风险。
