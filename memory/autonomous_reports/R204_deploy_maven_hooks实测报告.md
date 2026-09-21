
# R204 deploy_git_hooks.py maven原生hooks支持实测报告

## 任务背景

基于 R203 修复补丁，为 `deploy_git_hooks.py` 实现 `deploy_maven_hooks()` 方法，支持 maven/unknown 类型仓库通过原生 git hooks 部署 conventional commits 检查，无需 npm 环境。

---

## 代码变更

### 新增方法：`deploy_maven_hooks()`

位置：`deploy_git_hooks.py`，`GitHooksDeployer` 类中

```python
def deploy_maven_hooks(self, repo: Path) -> dict:
    """为maven/unknown项目部署原生git hooks（不依赖npm）"""
    result = {'repo': repo.name, 'type': self.detect_repo_type(repo), 'status': 'unknown', 'steps': []}
    hooks_dir = repo / '.git' / 'hooks'

    if not hooks_dir.exists():
        result['status'] = 'failed'
        result['error'] = f'.git/hooks 目录不存在: {hooks_dir}'
        return result

    commit_msg_hook = hooks_dir / 'commit-msg'
    hook_content = '''#!/bin/sh
# Conventional Commits 格式检查（原生shell，无需npm）
commit_msg=$(cat "$1")
pattern="^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\\(.+\\))?: .{1,100}"
if ! echo "$commit_msg" | grep -qE "$pattern"; then
  echo "❌ commit message 不符合 Conventional Commits 规范"
  echo "   格式: <type>(<scope>): <subject>"
  echo "   示例: feat(auth): add JWT login"
  echo "   允许类型: feat fix docs style refactor perf test build ci chore revert"
  exit 1
fi
'''
    commit_msg_hook.write_text(hook_content, encoding='utf-8')
    commit_msg_hook.chmod(0o755)
    result['steps'].append({'step': 'create_commit_msg', 'status': 'success'})
    result['status'] = 'success'
    return result
```

### 修改：`deploy_all()` 中 maven/unknown 分支

```python
# 修改前（R203时）
elif repo_type == 'maven':
    return {'repo': repo.name, 'type': 'maven', 'status': 'skipped', 'reason': '建议CI验证'}
else:
    return {'repo': repo.name, 'type': 'unknown', 'status': 'skipped'}

# 修改后
elif repo_type == 'maven':
    print("  → 部署原生git hooks (maven项目，无需npm)")
    result = self.deploy_maven_hooks(repo)
    self.results.append(result)
else:
    print("  → 部署原生git hooks (unknown类型，无需npm)")
    result = self.deploy_maven_hooks(repo)
    self.results.append(result)
```

---

## 实测结果

### 部署结果（10个仓库全量）

| # | 仓库 | 类型 | 结果 | 对比R203 |
|---|------|------|------|---------|
| 1 | Quokka251 | npm | ✅ success | 持平 |
| 2 | bar-market-server | maven | ✅ success | ⬆ 从skipped升级 |
| 3 | tools | unknown | ✅ success | ⬆ 从skipped升级 |
| 4 | bar-agent | maven | ✅ success | ⬆ 从skipped升级 |
| 5 | bar-deploy-server省市区 | maven | ✅ success | ⬆ 从skipped升级 |
| 6 | ruoyi-vue-pro-github | maven | ✅ success | ⬆ 从skipped升级 |
| 7 | ruoyi-vue-pro-ee | maven | ✅ success | ⬆ 从skipped升级 |
| 8 | bar-cloud | maven | ✅ success | ⬆ 从skipped升级 |
| 9 | bar-service | maven | ✅ success | ⬆ 从skipped升级 |
| 10 | bar-deploy-server | maven | ✅ success | ⬆ 从skipped升级 |

**汇总：成功 10 / 跳过 0 / 失败 0（R203：成功1/跳过9）**

覆盖率：**10% → 100%**

---

## Hook 文件验证

对全部 9 个 maven/unknown 仓库验证：

| 检查项 | 结果 |
|--------|------|
| `.git/hooks/commit-msg` 文件存在 | ✅ 9/9 |
| 文件可执行权限 (0o755) | ✅ 9/9 |
| 包含 Conventional Commits 检查逻辑 | ✅ 9/9 |

---

## Hook 拦截效果验证

对 3 个仓库（bar-market-server / bar-cloud / bar-service）执行真实 hook 脚本测试：

| 仓库 | 不规范消息（exit=1） | 规范消息（exit=0） |
|------|---------------------|-------------------|
| bar-market-server | ✅ 拦截 | ✅ 通过 |
| bar-cloud | ✅ 拦截 | ✅ 通过 |
| bar-service | ✅ 拦截 | ✅ 通过 |

测试用例：
- 不规范：`update something` → exit=1，打印错误提示
- 规范：`feat(auth): add JWT login` → exit=0，无输出

---

## 与 R203 对比

| 指标 | R203（修复前） | R204（修复后） |
|------|--------------|--------------|
| 覆盖率 | 10%（1/10） | 100%（10/10） |
| maven 支持 | ❌ 跳过 | ✅ 原生 hooks |
| unknown 支持 | ❌ 跳过 | ✅ 原生 hooks |
| npm 依赖 | 必须 | 仅 npm 仓库需要 |
| hook 拦截验证 | 未验证 | ✅ 3仓库实测通过 |

---

## 遗留问题

1. **pre-commit hook 未部署**：当前只部署 `commit-msg`，未部署 `pre-commit`（代码格式检查）。maven 项目可用 `mvn checkstyle:check` 或 `spotless:check`，待后续任务实现。
2. **hook 幂等性**：重复运行会覆盖已有 hook，若用户已有自定义 hook 会被覆盖。建议增加备份逻辑。
3. **Windows 兼容性**：hook 脚本使用 `#!/bin/sh` 和 `grep -qE`，在 Windows Git Bash 下可用，但 cmd/PowerShell 不支持。

---

## 记忆更新建议

建议在 `global_mem.txt` 补充：

```
deploy_git_hooks.py（cwd）：
- R204后支持maven/unknown原生hooks（deploy_maven_hooks()）
- smyze 10个仓库覆盖率从10%升至100%
- maven原生hooks：纯shell conventional commits检查，无需npm
- 已验证：bar-market-server/bar-cloud/bar-service hook拦截正常
```
