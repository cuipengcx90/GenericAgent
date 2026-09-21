
# R42 gh-CLI能力边界实测报告

**日期**: 2026-04-24  
**任务**: 验证gh-CLI在本机的实际能力边界，产出能力地图和GitHub Actions集成方案

---

## 1. 环境状态

- **gh版本**: 2.79.0 (2025-09-08)
- **认证状态**: ✅ 已登录 cuipengcx90 (keyring)
- **Token权限**: `admin:public_key`, `gist`, `read:org`, `repo`
- **API速率限制**: 5000/小时，剩余4997

---

## 2. 能力地图（实测结果）

### 2.1 仓库管理（✅ 全部可用）
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| 列出repos | `gh repo list --limit 20 --json name,description` | ✅ 132个repos |
| 查看repo详情 | `gh repo view {repo} --json name,stars,forks` | ✅ 获取stars/forks/branch |
| 获取clone URL | `gh repo view {repo} --json sshUrl,url` | ✅ ssh/https双URL |
| 创建repo | `gh repo create` | ⚠️ 未测试（写操作） |

### 2.2 PR管理（✅ 可用，但用户repos无PR）
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| 列出PR | `gh pr list --repo {repo} --json number,title,state` | ✅ 命令可用，但132个repos均无PR |
| 查看PR详情 | `gh pr view {number}` | ⚠️ 无PR可测试 |
| 创建PR | `gh pr create` | ⚠️ 未测试（写操作） |

### 2.3 Issue管理（⚠️ 部分repos禁用issues）
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| 列出issues | `gh issue list --repo {repo}` | ❌ open-codesign已禁用issues |
| 创建issue | `gh issue create --repo {repo} --title "..." --body "..."` | ⚠️ 未测试（写操作，需待审批） |

### 2.4 GitHub Actions（✅ 可用，但用户repos无workflow）
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| 列出workflows | `gh workflow list --repo {repo}` | ✅ 命令可用，但132个repos均无workflow |
| 查看workflow运行 | `gh run list --repo {repo}` | ✅ 命令可用，但无运行记录 |
| 触发workflow | `gh workflow run {workflow} --repo {repo}` | ⚠️ 无workflow可测试 |
| 查看workflow详情 | `gh workflow view {workflow}` | ⚠️ 无workflow可测试 |

### 2.5 Secrets & Variables（✅ 可用）
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| 列出secrets | `gh secret list --repo {repo}` | ✅ GenericAgent无secrets |
| 列出variables | `gh variable list --repo {repo}` | ✅ GenericAgent无variables |
| 设置secret | `gh secret set {name} --repo {repo}` | ⚠️ 未测试（写操作） |

### 2.6 API直接调用（✅ 强大能力）
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| 获取用户信息 | `gh api user --jq "{login:.login, repos:.public_repos}"` | ✅ 127个public repos, 3 followers |
| 速率限制查询 | `gh api rate_limit` | ✅ 5000/h, 剩余4997 |
| 任意GitHub API | `gh api {endpoint}` | ✅ 可调用任意REST API |

### 2.7 其他能力
| 能力 | 命令示例 | 实测结果 |
|------|---------|---------|
| Gist管理 | `gh gist list` | ✅ 无gist |
| SSH密钥管理 | `gh ssh-key list` | ✅ 1个密钥(cc) |

---

## 3. GitHub Actions集成方案

基于R25历史（repository_dispatch成功，workflow scope限制），结合本次实测，产出3种集成方案：

### 方案1: gh workflow run（最轻量，推荐）
**适用场景**: 已有workflow文件，需要从GA触发执行

```bash
# 前提：repo中有 .github/workflows/xxx.yml，且配置了 workflow_dispatch
gh workflow run deploy.yml \
  --repo cuipengcx90/GenericAgent \
  --ref main \
  --field environment=production \
  --field task_id=R42
```

**优势**:
- 无需修改workflow文件
- 支持传递参数（`--field`）
- 可指定分支（`--ref`）
- 立即返回，异步执行

**限制**:
- workflow必须配置`workflow_dispatch`触发器
- 需要`repo`或`workflow` scope

**集成代码**:
```python
import subprocess

def trigger_workflow(repo, workflow, params=None):
    cmd = f'gh workflow run {workflow} --repo {repo}'
    if params:
        for k, v in params.items():
            cmd += f' --field {k}={v}'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode == 0
```

---

### 方案2: gh api repository_dispatch（R25已验证）
**适用场景**: 需要自定义事件类型，workflow监听`repository_dispatch`

```bash
gh api repos/cuipengcx90/GenericAgent/dispatches \
  --method POST \
  --field event_type=autonomous_task \
  --field client_payload[task_id]=R42 \
  --field client_payload[action]=deploy
```

**优势**:
- 更灵活的事件类型
- 可传递复杂payload（JSON）
- R25已验证成功

**限制**:
- workflow必须监听`repository_dispatch`
- 需要`repo` scope

---

### 方案3: gh run watch（监控执行状态）
**适用场景**: 触发workflow后，需要等待结果

```bash
# 触发workflow
gh workflow run deploy.yml --repo cuipengcx90/GenericAgent

# 获取最新run ID
RUN_ID=$(gh run list --repo cuipengcx90/GenericAgent --workflow deploy.yml --limit 1 --json databaseId --jq '.[0].databaseId')

# 监控执行状态
gh run watch $RUN_ID --repo cuipengcx90/GenericAgent --exit-status
```

**优势**:
- 同步等待结果
- 可获取执行日志
- 失败时返回非0退出码

**限制**:
- 阻塞执行（可能数分钟）
- 需要轮询或watch

---

## 4. 与GA集成的最轻量方案（推荐）

**场景**: GA自主任务完成后，触发GitHub Actions部署/测试

```python
# autonomous_operation_sop/github_actions.py
import subprocess

def trigger_ga_workflow(task_id, action="report"):
    """
    触发GenericAgent的GitHub Actions workflow
    
    Args:
        task_id: 任务ID（如R42）
        action: 动作类型（report/deploy/test）
    
    Returns:
        bool: 是否触发成功
    """
    cmd = f'''gh workflow run autonomous.yml \
        --repo cuipengcx90/GenericAgent \
        --ref main \
        --field task_id={task_id} \
        --field action={action}'''
    
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"✅ Workflow triggered: task_id={task_id}, action={action}")
        return True
    else:
        print(f"❌ Failed: {result.stderr}")
        return False

# 使用示例（在complete_task后调用）
# trigger_ga_workflow("R42", "report")
```

**workflow文件示例** (`.github/workflows/autonomous.yml`):
```yaml
name: Autonomous Task Handler

on:
  workflow_dispatch:
    inputs:
      task_id:
        description: 'Task ID (e.g., R42)'
        required: true
      action:
        description: 'Action type'
        required: true
        default: 'report'

jobs:
  handle:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Process task
        run: |
          echo "Task: ${{ inputs.task_id }}"
          echo "Action: ${{ inputs.action }}"
          # 这里可以调用脚本处理报告、部署等
```

---

## 5. 结论与建议

### 核心发现
1. **gh-CLI功能完整**: repo/PR/issue/workflow/api/secret全覆盖，v2.79.0稳定
2. **用户repos特征**: 132个repos，无PR/无workflow/无Actions运行记录，主要是工具类项目
3. **最轻量方案**: `gh workflow run` + `workflow_dispatch`，单行命令即可触发

### 建议（待审批）
- **P0（立即可做）**: 将`github_actions.py`工具写入`autonomous_operation_sop/`，在`complete_task`后可选触发workflow
- **P1（需创建workflow）**: 在GenericAgent创建`.github/workflows/autonomous.yml`，监听`workflow_dispatch`，处理报告归档/通知推送
- **P2（扩展能力）**: 实现`gh run watch`同步等待，用于需要CI结果的任务（如测试验证）

### 记忆更新建议
- L2新增: `gh-CLI v2.79.0已配置，token权限repo/gist/read:org，5000/h速率`
- L3新增: `github_actions_sop.md`（gh workflow run集成方案+代码示例）
