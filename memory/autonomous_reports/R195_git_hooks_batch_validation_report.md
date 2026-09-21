# R195 Git Hooks批量部署效果验证+问题修复

## 任务目标
对 `deploy_git_hooks.py` 做本地可验证实测，覆盖批量部署、真实 commit 拦截、问题定位与修复闭环。

## 实测范围
- 批量扫描目录：`github_explore/`
- 目标仓库：
  - `Responsive-AI-Clusters-in-Supply-Chain`
  - `smart-procurement-agents`
  - `supply-chain-optimizer-agent`
- 真实验证仓库：`Responsive-AI-Clusters-in-Supply-Chain/front_end`

## 初始现象
首轮批量部署时，3 个仓库都被识别为 `unknown`，导致 npm 项目未部署 hooks。

## 根因与修复

### 1. monorepo 子项目未被识别
**现象**
- 仓库根目录不是 npm 项目
- 实际前端项目位于 `front_end/`

**根因**
- 工具仅检查仓库根目录，未探测子目录项目

**修复**
- 增加子项目定位逻辑
- 让 npm hooks 部署在真实 package 目录执行，而不是仓库根

### 2. hooksPath 指向错误
**现象**
- 子项目生成了 `.husky`
- 但 Git 提交时未走到该 hook

**根因**
- 现有 Git 配置中的 `core.hooksPath` 覆盖了子项目生成的 `.husky`

**修复**
- 在部署过程中为目标仓库写入正确的本地 `core.hooksPath`

### 3. 相对路径计算异常
**现象**
- 部署在 `set_hooks_path`/路径计算阶段失败

**根因**
- `Path.relative_to()` 使用场景不匹配，路径基准不一致

**修复**
- 改为 `os.path.relpath()` 计算相对路径

### 4. 变量名错误导致部署失败
**现象**
- 部署逻辑执行到设置 hooks 路径时报错

**根因**
- 代码中引用了未定义变量 `hooks_path`
- 正确变量应为 `hooks_rel`

**修复**
- 修正变量名引用

### 5. monorepo hook 执行时找不到 commitlint
**现象**
- `commitlint` 已安装在 `front_end/node_modules`
- 但提交时 hook 仍报缺包

**根因**
- Git 从仓库根执行 hook
- 原脚本使用 `npx --no -- commitlint --edit $1`
- 运行时无法从仓库根解析到 `front_end` 下依赖

**修复**
- 将 `commit-msg` hook 改为显式调用：
  - `front_end/node_modules/.bin/commitlint`
  - `front_end/commitlint.config.js`

## 最终验证结果

### 批量部署结果
- `Responsive-AI-Clusters-in-Supply-Chain`：成功识别为 npm 项目并完成部署
- 其余 2 个仓库：保持 `unknown`，未误部署

### 真实 commit 验证
在 `Responsive-AI-Clusters-in-Supply-Chain` 仓库中，对 `front_end` 新建临时文件并进行真实提交测试：

1. **坏提交消息**
   - 消息：`bad message`
   - 结果：被 `commitlint` 成功拦截

2. **规范提交消息**
   - 消息：`feat: validate monorepo hook`
   - 结果：成功提交

## 产出
- 修复后的 `deploy_git_hooks.py`
- 已验证可工作的 monorepo `commit-msg` hook 生成逻辑
- 本报告

## 结论
R195 已完成本地降级闭环验证。  
工具当前已能正确处理至少一种仓库根非 npm、子目录为 npm 项目的 monorepo 场景，并完成真实 commit message 拦截。

## 后续建议
如后续恢复 `smyze` 环境，可再追加一次多仓库实测，补充更大样本部署成功率与 IDE 集成表现。