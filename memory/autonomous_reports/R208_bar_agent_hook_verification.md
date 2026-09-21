
# R208 bar-agent/bar-market-server Git Hook 生效性验证报告

**日期**: 2026-04-30  
**任务**: 验证R207中合规率0%的两个仓库hook是否实际生效  
**结论**: Hook脚本存在且逻辑正确，但因全局 `core.hooksPath` 配置导致完全失效

---

## 一、验证结论

| 仓库 | hook文件存在 | 可执行 | 实际生效 | 根因 |
|------|------------|--------|---------|------|
| bar-agent | ✅ | ✅ | ❌ | 全局hooksPath覆盖 |
| bar-market-server | ✅ | ✅ | ❌ | 全局hooksPath覆盖 |

---

## 二、根因定位

### 根因：全局 `core.hooksPath` 覆盖了 `.git/hooks/`

```
$ git config --global core.hooksPath
/Users/cuip/.git-hooks
```

- Git 读取 hooks 的优先级：`core.hooksPath`（全局/本地配置）> `.git/hooks/`
- `/Users/cuip/.git-hooks` 目录**存在但为空**（0个文件）
- deploy_git_hooks.py 将 hook 写入 `.git/hooks/commit-msg`，但 git 根本不读这个路径
- 结果：所有通过 deploy_git_hooks.py 部署的 hook **全部静默失效**

### 证据链

1. **物理检查**：两仓库 `.git/hooks/commit-msg` 存在且可执行
2. **直接调用测试**：hook脚本逻辑正确
   - 不规范msg `修复登录bug` → rc=1，正确拦截 ✅
   - 规范msg `fix(auth): 修复登录bug` → rc=0，正确放行 ✅
3. **git config检查**：`global core.hooksPath=/Users/cuip/.git-hooks`（空目录）
4. **实际commit记录**：bar-market-server 存在 "优化"/"去除mq"/"饮品制作排名统计" 等不规范提交，证实hook未拦截

### 影响范围

此问题不仅影响 bar-agent/bar-market-server，而是**影响所有通过 deploy_git_hooks.py 部署的仓库**。R204报告中"3仓库拦截验证通过"的结论需要重新审视——当时的验证可能是直接调用脚本而非通过git commit触发。

---

## 三、修复方案

### 方案A（推荐）：修改 deploy_git_hooks.py 写入全局hooksPath

```python
def get_hooks_dir(repo_path):
    """获取实际生效的hooks目录"""
    import subprocess
    # 检查 core.hooksPath 配置
    r = subprocess.run(['git', 'config', 'core.hooksPath'],
                       capture_output=True, text=True, cwd=repo_path)
    if r.returncode == 0 and r.stdout.strip():
        hooks_path = r.stdout.strip()
        # 展开 ~ 
        return Path(hooks_path).expanduser()
    return repo_path / '.git' / 'hooks'
```

优点：自动适配用户环境，无需手动操作  
缺点：全局hooksPath是共享目录，多仓库hook可能冲突

### 方案B：部署时检测并提示

在 deploy_git_hooks.py 中增加检测：
```python
global_hooks = subprocess.run(['git', 'config', '--global', 'core.hooksPath'], ...)
if global_hooks.returncode == 0:
    print(f"⚠️  检测到全局 core.hooksPath={global_hooks.stdout.strip()}")
    print(f"   Hook将写入 .git/hooks/，但git实际读取全局路径")
    print(f"   建议：git config --global --unset core.hooksPath")
```

### 方案C（最彻底）：写入全局hooksPath + 仓库分发机制

将 commit-msg hook 改为分发器：全局hook读取各仓库 `.git/hooks/` 中的脚本并执行。

---

## 四、R204结论修正

R204报告称"3仓库拦截验证通过"，但验证方式是**直接调用hook脚本**（`sh .git/hooks/commit-msg`），而非通过 `git commit` 触发。在存在全局 `core.hooksPath` 的环境下，这两种验证方式结果不同：

- 直接调用：✅ 正常拦截（脚本逻辑正确）
- git commit触发：❌ 不拦截（git读全局hooksPath，为空）

**实际覆盖率：0%（非R204报告的100%）**

---

## 五、记忆更新建议

```
[deploy_git_hooks.py已知缺陷] 
- P0: 未检测 core.hooksPath 全局配置，导致hook写入.git/hooks/但实际不生效
- 影响：smyze所有仓库hook均失效（全局hooksPath=/Users/cuip/.git-hooks为空目录）
- 修复：get_hooks_dir()函数需先读git config core.hooksPath再决定写入路径
- R204"3仓库验证通过"结论有误：验证方式为直接调用脚本，非git commit触发
```
