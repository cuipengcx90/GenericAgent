
# R207 smyze项目Git提交规范现状分析报告

## 扫描范围

- 仓库数：10个（smyze根目录下所有git仓库）
- 扫描深度：每仓库最近100条commit message
- 扫描时间：2026-04-30
- 合规标准：Conventional Commits v1.0（type(scope)?: subject）

---

## 一、整体合规率

| 指标 | 数值 |
|------|------|
| 扫描总commit数 | 671条（排除merge commit） |
| 合规commit数 | 197条 |
| **整体合规率** | **29.4%** |
| merge commit数 | 37条（已排除） |

**结论：整体合规率仅29.4%，低于行业建议的80%基线，规范推行效果有限。**

---

## 二、各仓库合规率排名

| 排名 | 仓库 | 合规率 | 合规/总数 | 评级 |
|------|------|--------|-----------|------|
| 1 | bar-deploy-server | 83.0% | 78/94 | ✅ 良好 |
| 2 | bar-cloud | 67.7% | 63/93 | ⚠️ 待改进 |
| 3 | tools | 48.6% | 36/74 | ⚠️ 待改进 |
| 4 | ruoyi-vue-pro-ee | 14.6% | 14/96 | ❌ 差 |
| 5 | bar-market-server | 5.3% | 2/38 | ❌ 差 |
| 6 | bar-agent | 4.8% | 4/83 | ❌ 差 |
| 7 | Quokka251 | 0.0% | 0/92 | ❌ 无规范 |
| 8 | bar-deploy-server省市区 | 0.0% | 0/99 | ❌ 无规范 |
| 9 | bar-service | 0.0% | 0/1 | ❌ 无规范 |
| 10 | ruoyi-vue-pro-github | 0.0% | 0/1 | ❌ 无规范 |

**分层明显：bar-deploy-server一枝独秀(83%)，bar-cloud次之(68%)，其余7个仓库均低于50%，4个仓库为0%。**

---

## 三、高频违规类型分析

| 违规类型 | 数量 | 占比 | 典型样本 |
|----------|------|------|----------|
| 中文开头 | 189条 | 40.0% | "优化"、"对接极光"、"处理重复叠加时间段" |
| 缺少冒号分隔符 | 174条 | 36.8% | "fix：冲调机code取消"（用中文冒号）、"fix"（无描述） |
| 其他格式 | 92条 | 19.5% | "[feat] 优化..."、"Revert..."、"tls" |
| 首字母大写(非type) | 16条 | 3.4% | "Initial commit"、"Merge pull request" |
| 数字开头 | 3条 | 0.6% | 版本号等 |

### 关键发现

1. **中文冒号问题**：bar-cloud大量使用 `fix：` 而非 `fix:`，导致正则不匹配。这是bar-cloud合规率偏低的主要原因（实际语义合规，格式不合规）。

2. **纯中文commit**：bar-deploy-server省市区、bar-market-server、Quokka251 几乎全部使用中文描述，无type前缀。

3. **无意义commit**：bar-agent、bar-deploy-server 存在大量 `fix`、`优化` 等单词commit，缺乏描述性。

4. **[feat]方括号格式**：Quokka251 使用 `[feat]` 而非 `feat:`，是历史遗留的非标准格式。

---

## 四、合规commit类型分布

| type | 数量 | 占比 |
|------|------|------|
| fix | 109条 | 55.3% |
| feat | 39条 | 19.8% |
| refactor | 39条 | 19.8% |
| chore | 6条 | 3.0% |
| perf | 2条 | 1.0% |
| docs | 1条 | 0.5% |
| test | 1条 | 0.5% |

**fix占比过高(55%)，docs/test/ci几乎为零，说明团队提交以修复为主，文档和测试提交习惯薄弱。**

---

## 五、仓库分层与根因分析

### 第一层：规范执行良好（>60%）
- **bar-deploy-server**（83%）：已部署commit-msg hook（R204第1条），规范执行有保障
- **bar-cloud**（68%）：已部署hook，但中文冒号问题导致误判，实际语义合规率更高

### 第二层：部分规范（20-60%）
- **tools**（49%）：个人工具仓库，规范意识参差不齐

### 第三层：规范缺失（<20%）
- **ruoyi-vue-pro-ee/github**：上游开源仓库，commit风格混杂（PR合并信息+中文冒号）
- **bar-market-server/bar-agent**：业务仓库，团队未形成规范习惯
- **Quokka251/bar-deploy-server省市区**：完全无规范，使用中文或方括号格式

---

## 六、改进建议

### 短期（1周内）

1. **修复中文冒号误判**：在commit-msg hook中增加中文冒号转英文冒号的提示，或在正则中兼容 `：`
   ```sh
   # 在hook中增加提示
   if echo "$msg" | grep -qP '^(feat|fix|...)：'; then
     echo "⚠️  检测到中文冒号，请使用英文冒号 ':'"
     exit 1
   fi
   ```

2. **对bar-agent/bar-market-server/bar-deploy-server省市区补充hook**：R204第1条已部署hook，但需验证这3个仓库的hook是否生效（合规率接近0%说明hook可能未触发或被绕过）

3. **禁止无意义commit**：在hook中增加最小长度检查（subject >= 5字符）

### 中期（1个月内）

4. **团队规范培训**：针对bar-market-server/bar-agent/Quokka251团队，提供Conventional Commits速查卡

5. **CI集成合规检查**：在GitHub Actions/Jenkins中增加commit message lint步骤，PR合并前强制检查

6. **建立合规率基线**：当前29.4%作为基线，目标3个月内提升到60%

### 长期

7. **commitizen集成**：为npm仓库集成commitizen交互式提交工具，降低规范门槛

8. **定期合规率报告**：将本脚本集成到weekly_report，每周自动输出合规率趋势

---

## 七、数据附录

### bar-cloud违规样本（中文冒号问题）
```
fix：冲调机code取消-400000
fix：上下架接口优化
feat：新增xxx功能
```
→ 建议：hook中增加中文冒号检测，引导用户修正

### bar-agent违规样本（无意义commit）
```
fix
fix
臭氧请求报错重试
```
→ 建议：增加subject最小长度(>=5字符)检查

---

## 记忆更新建议

建议在 `global_mem.txt` 补充：

```
smyze Git提交规范现状(R207, 2026-04-30):
- 整体合规率29.4%(197/671)；bar-deploy-server最高83%，4个仓库0%
- 主要违规：中文开头(40%)+缺少冒号(37%)；bar-cloud中文冒号误判严重
- 改进优先级：①修复中文冒号检测 ②bar-agent/bar-market-server hook验证 ③CI集成
```
