
# R206 deploy_git_hooks.py 工具质量评估与重构建议

## 评估背景

基于 R203（maven原生hooks实测）、R204（10仓库部署验证）对 deploy_git_hooks.py（275行）进行全面质量评估，覆盖覆盖率、可维护性、扩展性三个维度。

---

## 一、现状评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 功能覆盖率 | 7/10 | npm✅ maven✅ unknown✅；monorepo多模块⚠️ |
| 可维护性 | 6/10 | 单文件275行，方法职责清晰；但npm方法过长(125行)，错误处理不一致 |
| 扩展性 | 5/10 | 无策略模式，新增类型需改deploy_all()；hook模板硬编码在方法体内 |
| 测试覆盖 | 3/10 | 无单元测试，无dry-run模式，无幂等保护 |
| 可观测性 | 6/10 | JSON报告✅；但步骤日志不统一，maven比npm少 |

**综合评分：5.4/10**

---

## 二、问题清单（按优先级）

### P0 - 正确性问题

**问题1：commit-msg hook路径硬编码逻辑有缺陷**

```python
# 当前代码（第123行）
project_rel = hooks_rel[:-len('/.husky')] if hooks_rel.endswith('/.husky') else '.'
```

- `hooks_rel` 是相对于 git root 的路径，如 `bar-cloud/bar-qmai-service/.husky`
- 截断后得到 `bar-cloud/bar-qmai-service`，但 commit-msg hook 中用的是相对于 `.husky/` 的路径
- 在 monorepo 子目录场景下，`node_modules/.bin/commitlint` 路径可能不正确

**问题2：maven hook 无幂等保护**

```python
# deploy_maven_hooks() 直接覆盖，无备份
commit_msg_hook.write_text(hook_content, encoding='utf-8')
```

- 若仓库已有自定义 hook，会被静默覆盖
- 建议：检测已有 hook 内容，若非本工具生成则备份后再覆盖

**问题3：scan_repos() 只扫描一层**

```python
for item in self.repos_root.iterdir():  # 只扫描一层
    if item.is_dir() and (item / '.git').exists():
```

- smyze 中 bar-cloud 是 git 仓库，但其子服务（bar-qmai-service 等）不是独立 git 仓库
- 这是正确的，但文档未说明，容易误解

### P1 - 可维护性问题

**问题4：deploy_npm_hooks() 方法过长（125行）**

- 5个步骤全部内联，难以单独测试
- 建议拆分为 `_install_deps()`, `_init_husky()`, `_create_commit_msg_hook()`, `_update_package_json()`

**问题5：hook 模板硬编码在方法体内**

```python
# 第182-194行：shell脚本字符串直接嵌入Python方法
hook_content = '''#!/bin/sh
# Conventional Commits 格式检查...
```

- 修改 hook 模板需要改 Python 代码
- 建议：提取为类常量或外部模板文件

**问题6：错误处理不一致**

- npm 方法：步骤失败立即 return（快速失败）
- maven 方法：只有一个 try/except，粒度粗
- 建议统一为步骤级错误记录 + 可配置的失败策略

### P2 - 扩展性问题

**问题7：deploy_all() 中类型判断用 if/elif/else**

```python
if repo_type == 'npm':
    ...
elif repo_type == 'maven':
    ...
else:  # unknown 也调用 maven
    ...
```

- 新增类型（如 gradle、go module）需修改 deploy_all()
- 建议：策略模式，注册 deployer_map = {'npm': deploy_npm_hooks, 'maven': deploy_maven_hooks}

**问题8：无 dry-run 模式**

- 当前执行即修改文件，无法预览变更
- 建议增加 `--dry-run` 参数，输出将要执行的操作而不实际执行

**问题9：无回滚机制**

- 部署失败时无法恢复原始状态
- 建议：部署前备份 `.git/hooks/` 目录，失败时自动恢复

---

## 三、重构方案

### 方案A：最小重构（推荐，1-2天）

优先解决 P0+P1，不改架构：

```
deploy_git_hooks.py（重构后）
├── HOOK_TEMPLATES = {...}          # 提取模板常量
├── GitHooksDeployer
│   ├── deploy_npm_hooks()          # 拆分为5个私有方法
│   │   ├── _install_npm_deps()
│   │   ├── _init_husky()
│   │   ├── _set_hooks_path()
│   │   ├── _create_commit_msg_hook()
│   │   └── _update_package_json()
│   ├── deploy_maven_hooks()        # 增加幂等保护+备份
│   └── deploy_all()                # 增加dry-run支持
└── main()                          # 增加--dry-run参数
```

**改动量**：~50行，风险低，可直接在现有文件上修改

### 方案B：策略模式重构（3-5天）

```
deploy_git_hooks/
├── __init__.py
├── deployer.py          # GitHooksDeployer（协调器）
├── strategies/
│   ├── base.py          # BaseDeployStrategy(ABC)
│   ├── npm_strategy.py  # NpmDeployStrategy
│   └── maven_strategy.py# MavenDeployStrategy
├── templates/
│   ├── commit-msg.sh    # maven hook模板
│   └── commit-msg.js    # npm commitlint配置
└── cli.py               # argparse CLI
```

**改动量**：重写，风险中，适合下一个大版本

### 方案C：维持现状+补丁（0.5天）

仅修复 P0 问题（幂等保护+路径修复），其余不动。适合当前阶段。

---

## 四、优先级排序

| 优先级 | 问题 | 方案 | 预估工时 |
|--------|------|------|----------|
| P0-1 | maven hook 无幂等保护 | 方案C | 0.5h |
| P0-2 | npm hook 路径逻辑缺陷 | 方案A | 1h |
| P1-1 | hook 模板提取为常量 | 方案A | 0.5h |
| P1-2 | deploy_npm_hooks 拆分 | 方案A | 2h |
| P2-1 | dry-run 模式 | 方案A | 1h |
| P2-2 | 策略模式重构 | 方案B | 3-5天 |

**建议执行顺序**：P0-1 → P0-2 → P1-1 → dry-run → 其余按需

---

## 五、当前工具优点（保留）

1. `find_project_dir()` 的 monorepo 探测逻辑设计合理（rglob + node_modules 过滤）
2. `detect_repo_type()` 委托给 `find_project_dir()` 避免重复逻辑
3. JSON 报告结构清晰，steps 粒度适中
4. maven hook 使用纯 shell，无外部依赖，跨平台性好

---

## 记忆更新建议

建议在 `global_mem.txt` 补充：

```
deploy_git_hooks.py质量评估(R206, 2026-04-30):
- 综合评分5.4/10；P0问题：maven无幂等保护、npm路径逻辑缺陷
- 推荐方案A最小重构(1-2天)：提取模板常量+拆分npm方法+dry-run
- 策略模式重构(方案B)适合下一大版本
```
