
# R203 deploy_git_hooks.py 真实仓库实测报告

## 任务背景

将 `deploy_git_hooks.py` 部署到 smyze 下所有真实 Git 仓库，记录失败类型与修复方案，产出 SOP 更新建议。

---

## 实测环境

- 工具路径：`/Users/cuip/projects/GenericAgent/temp/deploy_git_hooks.py`
- 目标根目录：`/Users/cuip/develop/WorkSpaces/idea/smyze`
- Node.js：v23.11.0 | npm：11.6.2 | npx：11.6.2
- 执行时间：2026-04-30，耗时 94.1s

---

## 仓库扫描结果（10个）

| # | 仓库 | 类型 | 结果 | 原因 |
|---|------|------|------|------|
| 1 | Quokka251 | npm | ✅ success | 完整5步骤全通过 |
| 2 | bar-market-server | maven | ⏭ skipped | 工具判定maven跳过 |
| 3 | tools | unknown | ⏭ skipped | 工具判定unknown跳过 |
| 4 | bar-agent | maven | ⏭ skipped | 工具判定maven跳过 |
| 5 | bar-deploy-server省市区 | maven | ⏭ skipped | 工具判定maven跳过 |
| 6 | ruoyi-vue-pro-github | maven | ⏭ skipped | 工具判定maven跳过 |
| 7 | ruoyi-vue-pro-ee | maven | ⏭ skipped | 工具判定maven跳过 |
| 8 | bar-cloud | maven | ⏭ skipped | 工具判定maven跳过 |
| 9 | bar-service | maven | ⏭ skipped | 工具判定maven跳过 |
| 10 | bar-deploy-server | maven | ⏭ skipped | 工具判定maven跳过 |

**汇总：成功 1 / 跳过 9 / 失败 0**

---

## Quokka251 部署产物验证

项目目录：`Quokka251/Fronted/TV-Screen-V3`（嵌套子目录，非仓库根）

| 产物 | 状态 |
|------|------|
| `.husky/` 目录 | ✅ 已创建 |
| `.husky/commit-msg` | ✅ 已创建 |
| `commitlint.config.js` | ✅ 已创建 |
| `node_modules/husky` | ✅ 已安装 |
| `node_modules/@commitlint` | ✅ 已安装 |
| `node_modules/commitizen` | ✅ 已安装 |
| `git core.hooksPath` | ✅ `Fronted/TV-Screen-V3/.husky` |
| `package.json scripts.commit` | ✅ `cz` |

commit-msg hook 内容（使用绝对子路径，可跨目录工作）：
```sh
#!/usr/bin/env sh
"Fronted/TV-Screen-V3/node_modules/.bin/commitlint" --config "Fronted/TV-Screen-V3/commitlint.config.js" --edit "$1"
```

---

## 失败类型清单

### 类型A：maven项目被跳过（8个仓库）

**根因**：工具 `find_project_dir()` 检测到根目录有 `pom.xml` 即判定为 maven，直接 skip，不尝试部署。

**影响**：smyze 中 80% 的仓库（8/10）无法通过此工具部署 git hooks。

**修复方案**：maven 项目同样可以部署 git hooks（通过 `.git/hooks/` 原生脚本，不依赖 npm）。

修复补丁（在 `deploy_repo` 方法中增加 maven 原生 hooks 部署分支）：

```python
def deploy_maven_hooks(self, repo: Path) -> dict:
    """为maven项目部署原生git hooks（不依赖npm）"""
    result = {'repo': repo.name, 'type': 'maven', 'status': 'unknown', 'steps': []}
    hooks_dir = repo / '.git' / 'hooks'
    
    # commit-msg hook：检查conventional commits格式
    commit_msg_hook = hooks_dir / 'commit-msg'
    hook_content = '''#!/bin/sh
# Conventional Commits 格式检查（原生shell，无需npm）
commit_msg=$(cat "$1")
pattern="^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\\(.+\\))?: .{1,100}"
if ! echo "$commit_msg" | grep -qE "$pattern"; then
  echo "❌ commit message 不符合 Conventional Commits 规范"
  echo "   格式: <type>(<scope>): <subject>"
  echo "   示例: feat(auth): add JWT login"
  exit 1
fi
'''
    try:
        commit_msg_hook.write_text(hook_content)
        commit_msg_hook.chmod(0o755)
        result['steps'].append({'step': 'create_commit_msg', 'status': 'success'})
        result['status'] = 'success'
    except Exception as e:
        result['steps'].append({'step': 'create_commit_msg', 'status': 'failed', 'error': str(e)})
        result['status'] = 'failed'
    return result
```

### 类型B：unknown类型仓库被跳过（1个：tools）

**根因**：`tools` 仓库无 `package.json` 也无 `pom.xml`，为纯 shell 脚本仓库，工具无法识别。

**影响**：shell/脚本类仓库无法部署。

**修复方案**：unknown 类型同样可用原生 git hooks，与 maven 修复方案相同。

### 类型C：嵌套子目录项目（潜在风险，Quokka251已触发）

**根因**：Quokka251 的 npm 项目在 `Fronted/TV-Screen-V3/` 子目录，而 `.git` 在仓库根。工具通过 `find_project_dir()` 向下搜索找到了正确的子目录，但 `git core.hooksPath` 被设置为相对路径 `Fronted/TV-Screen-V3/.husky`，这在仓库根执行 `git commit` 时有效，但若在子目录执行则路径解析可能出错。

**当前状态**：已部署成功，路径使用绝对子路径写入 hook 脚本，实际可用。

---

## 修复补丁汇总

在 `deploy_git_hooks.py` 的 `deploy_repo()` 方法中，将 maven/unknown 的 skip 逻辑替换为原生 hooks 部署：

```python
# 原代码（第~50行）
if repo_type == 'maven':
    return {'repo': repo.name, 'type': 'maven', 'status': 'skipped', 'reason': '建议CI验证'}
if repo_type == 'unknown':
    return {'repo': repo.name, 'type': 'unknown', 'status': 'skipped'}

# 修复后
if repo_type in ('maven', 'unknown'):
    return self.deploy_maven_hooks(repo)  # 使用原生git hooks
```

---

## SOP 更新建议

1. **前置探测**：运行前先统计仓库类型分布（`scan_repos()` + `find_project_dir()`），若 npm 仓库占比 < 20%，提示用户是否启用 maven 原生 hooks 模式。

2. **maven 原生 hooks 支持**：增加 `deploy_maven_hooks()` 方法，用纯 shell 实现 conventional commits 检查，无需 npm 环境。

3. **嵌套子目录警告**：当 `proj_dir != repo` 时，打印警告并记录 `hooksPath` 相对路径，提示用户在仓库根执行 `git commit` 才能触发 hook。

4. **dry-run 模式**：增加 `--dry-run` 参数，只打印将要执行的操作，不实际修改文件，便于 CI 环境预检。

5. **smyze 仓库现状**：smyze 下 10 个 Git 仓库中仅 1 个 npm（Quokka251），8 个 maven，1 个 shell。当前工具只能覆盖 10%，需 maven 原生 hooks 支持才能覆盖全量。

---

## 记忆更新建议

建议在 `global_mem.txt` 补充：
> smyze Git仓库分布：10个仓库，1个npm(Quokka251/Fronted/TV-Screen-V3)，8个maven，1个shell(tools)；deploy_git_hooks.py当前只支持npm，maven需原生hooks补丁。
