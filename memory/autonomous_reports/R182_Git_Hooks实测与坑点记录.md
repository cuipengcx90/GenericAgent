
# R182 Git Hooks实测与坑点记录

## 任务背景
基于R169批量部署工具(deploy_git_hooks.py)和git_hooks_deployment.json历史记录，对commit-msg hook进行完整实测，验证commitlint规则、错误处理和husky配置，记录生产环境坑点。

## 实测环境
- **测试仓库**: git_hooks_test
- **Git状态**: 已初始化，有.git目录
- **package.json**: 存在
- **已安装hooks**: commit-msg

## 依赖版本
```json
{
  "husky": "^9.1.7",
  "@commitlint/cli": "^20.5.2",
  "@commitlint/config-conventional": "^20.5.0"
}
```

## 实测场景

### 场景1: 不符合规范的commit message ❌
**测试命令**:
```bash
git commit -m "test commit"
```

**预期**: 拒绝提交
**实际结果**: ✅ 符合预期
- 返回码: 1 (失败)
- 错误信息:
  ```
  ⧗   input: test commit
  ✖   subject may not be empty [subject-empty]
  ✖   type may not be empty [type-empty]
  ✖   found 2 problems, 0 warnings
  ```
- commitlint正常工作，错误提示清晰

### 场景2: 符合规范的commit message ✅
**测试命令**:
```bash
git commit -m "feat: test git hooks"
```

**预期**: 成功提交
**实际结果**: ✅ 符合预期
- 返回码: 0 (成功)
- 提交信息: `[main 3701cee] feat: test git hooks`
- commit-msg hook验证通过

## 发现的坑点

### 🔴 坑点1: husky v10 DEPRECATED警告
**严重程度**: 高 (影响所有使用旧格式的仓库)

**现象**:
```
husky - DEPRECATED

Please remove the following two lines from .husky/commit-msg:

#!/usr/bin/env sh
. "$(dirname -- "$0")/_/husky.sh"

They WILL FAIL in v10.0.0
```

**根因**:
- 当前使用husky 9.1.7
- .husky/commit-msg使用旧格式(包含shebang和husky.sh加载)
- husky v10将不再支持这种格式

**影响范围**:
- git_hooks_deployment.json中5个已部署的npm仓库
- 所有使用deploy_git_hooks.py部署的仓库

**解决方案**:
1. 升级husky配置格式(移除两行代码)
2. 在deploy_git_hooks.py中添加版本检查和格式迁移
3. 提供批量迁移脚本

### ✅ 验证通过: commitlint配置
**配置文件**: commitlint.config.js
**规则**: @commitlint/config-conventional
**验证结果**:
- ✅ 规则验证正常工作
- ✅ 错误提示清晰明确(subject-empty, type-empty)
- ✅ 支持标准commit类型(feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert)

## 改进建议

### 1. 升级husky配置格式 (优先级: 高)
**当前格式** (.husky/commit-msg):
```bash
#!/usr/bin/env sh
. "$(dirname -- "$0")/_/husky.sh"

npx --no -- commitlint --edit $1
```

**推荐格式** (husky v9+):
```bash
npx --no -- commitlint --edit $1
```

### 2. 增强deploy_git_hooks.py (优先级: 中)
- 添加husky版本检查
- 自动检测并迁移旧格式
- 部署后验证hook是否正常工作

### 3. 提供迁移脚本 (优先级: 中)
```python
def migrate_husky_format(repo_path):
    """迁移husky配置到v9+格式"""
    husky_dir = repo_path / '.husky'
    for hook_file in husky_dir.glob('*'):
        if hook_file.is_file() and hook_file.name != '_':
            content = hook_file.read_text()
            # 移除shebang和husky.sh加载
            new_content = remove_deprecated_lines(content)
            hook_file.write_text(new_content)
```

## 闭环验证
- ✅ commit-msg hook功能正常
- ✅ commitlint规则验证正确
- ✅ 错误处理清晰明确
- ⚠️  发现husky v10兼容性问题
- ✅ 提供解决方案和改进建议

## 产出物
- 实测报告 (本文件)
- 坑点记录: husky v10 DEPRECATED警告
- 改进建议: 配置格式迁移方案
- 待办事项: 批量迁移已部署的5个仓库

## 下一步行动
1. 创建husky配置迁移脚本
2. 批量迁移git_hooks_deployment.json中的5个仓库
3. 更新deploy_git_hooks.py添加版本检查
4. 实测pre-commit和pre-push hooks (如需要)
