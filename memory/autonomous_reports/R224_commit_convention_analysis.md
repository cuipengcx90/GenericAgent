
# R224 bar-deploy-server Commit 规范最佳实践提炼

## 执行摘要

分析 bar-deploy-server 200条commit，合规率 **79.4%**（排除Merge），对比5个低合规仓库，提炼可复制规范模板。

---

## 一、数据概览

| 仓库 | 合规率 | 合规/总计 | 主要不合规模式 |
|------|--------|-----------|----------------|
| bar-deploy-server | **79.4%** | 154/194 | 纯"优化"(50%)、纯中文无前缀(33%) |
| bar-cloud | 41.9% | 39/93 | 中文冒号`fix：`、纯中文 |
| bar-market-server | 5.3% | 2/38 | 几乎全部纯中文 |
| bar-agent | 4.8% | 4/83 | 几乎全部纯中文 |
| bar-deploy-server省市区 | 0.0% | 0/99 | 全部纯中文 |
| bar-service | 0.0% | 0/1 | Initial commit |

**结论**：bar-deploy-server 是团队唯一高合规仓库，领先第二名(bar-cloud)近40个百分点。

---

## 二、bar-deploy-server 规范模式提炼

### 2.1 Type 分布（200条）

| type | 数量 | 占比 | 含义 |
|------|------|------|------|
| refactor | ~74 | 48% | 重构优化（最高频） |
| fix | ~34 | 22% | Bug修复 |
| feat | ~28 | 18% | 新功能 |
| chore | ~12 | 8% | 构建/依赖 |
| perf/docs/test | ~6 | 4% | 其他 |

**洞察**：refactor占比异常高(48%)，说明该仓库处于持续重构阶段（playback模块重构为主）。

### 2.2 Scope 使用规律（Top 10）

```
playback(26) > product(26) > release(9) > device(9) > dashboard(7)
> oauth(6) > websocket(4) > publish(3) > config(2) > utils(2)
```

- **67%的合规commit带scope**，scope与业务模块强对应
- scope命名规则：英文小写单词，对应Java包名/功能模块

### 2.3 高质量commit样本

```
feat(playback): 引入事件驱动机制实现播放配置下发与WebSocket通知解耦
refactor(playback): buildDetailResponse批量优先重构及N+1查询修复
fix(oauth): 修复OAuthRefreshProperties Bean注册问题
refactor(playback): 抽取PlaybackConfigBaseInfo基类并优化分页响应为扁平结构
chore(release): 版本发布准备
```

**规律**：subject用中文，动词开头，说明"做了什么"+"为什么/效果"。

---

## 三、不合规根因分析

### 3.1 不合规模式分布

| 模式 | 数量 | 占比 | 根因 |
|------|------|------|------|
| 纯"优化"（无上下文） | 20 | 50% | 快速提交时懒于描述 |
| 纯中文无type前缀 | 13 | 33% | 未养成CC习惯 |
| 其他（fix无冒号等） | 7 | 17% | 格式细节疏忽 |

### 3.2 低合规仓库差异

bar-cloud 的典型错误：
- `fix:全部上架&下架`（缺空格）
- `fix：冲调机code取消-400000`（中文冒号）

bar-agent/bar-market-server：完全未采用CC规范，全部纯中文描述。

**根因**：规范未在团队层面推广，仅崔朋个人在bar-deploy-server坚持使用。

---

## 四、可复制规范模板

### 4.1 .gitmessage 模板

已产出文件：`./bar_deploy_server_gitmessage`（可复制为 `~/.gitmessage`）

配置命令：
```bash
git config --global commit.template ~/.gitmessage
# 或仅对单仓库生效：
git config commit.template .gitmessage
```

### 4.2 smyze 团队 Commit 规范（精简版）

```
格式：<type>(<scope>): <subject>

type（必填）：
  feat     新功能
  fix      Bug修复
  refactor 重构
  chore    构建/依赖/工具
  docs     文档
  test     测试
  perf     性能优化

scope（推荐）：业务模块英文名
  playback | product | dashboard | device | oauth
  websocket | publish | config | release | utils

subject（必填）：
  ✅ 中文简洁描述，动词开头，说明做了什么
  ❌ 禁止：优化 / fix / 单测 / 处理问题（无上下文）

示例：
  feat(playback): 新增播放配置批量下发接口
  fix(oauth): 修复Token刷新时Bean注册冲突
  refactor(product): 抽取公共基类减少重复代码
```

---

## 五、推广建议

1. **立即可做**：将 `.gitmessage` 模板分发给团队，`git config commit.template` 配置
2. **下一步**：基于本规范写 commit-msg git hook 自动校验（已列入TODO P4）
3. **优先仓库**：bar-cloud（41.9%，有基础）> bar-agent（4.8%，需从零推广）

---

## 六、记忆更新建议

```
[smyze commit规范] bar-deploy-server合规率79.4%(200条)，团队最高；
主要不合规=纯"优化"(50%)+纯中文无前缀(33%)；
scope规律：英文小写业务模块名，67%合规commit带scope；
.gitmessage模板已产出(./bar_deploy_server_gitmessage)
```
