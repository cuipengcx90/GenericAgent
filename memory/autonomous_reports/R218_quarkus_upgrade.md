
# R218 Quarkus 2.16.7→2.16.12升级+CVE-2023-4853修复

## 任务目标
基于R210(CVE-2023-4853 CVSS8.1，影响Quarkus<2.16.11)，将smyze三个仓库的Quarkus版本从2.16.7.Final升级到2.16.12.Final，修复安全漏洞并验证编译通过。

---

## 变更内容

### 修改文件

| 文件 | 变更 |
|------|------|
| `bar-cloud/pom.xml` | `quarkus.platform.version` 2.16.7.Final → 2.16.12.Final |
| `bar-agent/pom.xml` | `quarkus.platform.version` + `quarkus.version` 均升级 |
| `bar-service/pom.xml` | `quarkus.platform.version` 2.16.7.Final → 2.16.12.Final |

### CVE修复
- **CVE-2023-4853**：Quarkus HTTP Policy Bypass，CVSS 8.1（高危）
- 影响范围：Quarkus < 2.16.11.Final
- 修复版本：2.16.12.Final（含2.16.11安全补丁+后续bugfix）

---

## 编译验证结果

| 仓库 | 结果 | 备注 |
|------|------|------|
| bar-agent | ✅ SUCCESS | 含bar-agent-server子模块 |
| bar-service | ✅ SUCCESS | |
| bar-cloud | ⚠️ 预存在问题 | bar-alarm-system-service父pom relativePath错误（升级前已存在，非回归） |

### bar-cloud说明
`bar-alarm-system-service/pom.xml` 的 `<parent>` 缺少 `<relativePath>` 标签，导致Maven无法在本地解析父POM。已通过 `git stash` 验证该问题在升级前（2.16.7.Final）同样存在，**与本次升级无关**。bar-cloud父pom版本已正确更新为2.16.12.Final。

---

## 坑点记录
- bar-agent有两个quarkus版本属性：`quarkus.platform.version` 和 `quarkus.version`，需同步升级
- bar-cloud的Maven reactor在POM解析阶段失败，`-pl`排除模块无效（解析在reactor构建前）
- bar-alarm-system-service是预存在问题，建议后续在P3(Maven版本冲突治理)中一并修复
