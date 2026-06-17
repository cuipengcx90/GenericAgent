# trellis_integration_sop
## GA × Trellis 通用集成 SOP (v2 — Runtime Engine)

适用于：任何含 `.trellis/` 目录的项目（自动检测，无需per-project配置）

### 触发条件
GA 收到开发任务时，检测目标项目根目录是否存在 `.trellis/scripts/get_context.py`：
- 存在 → 按本 SOP 执行 Trellis 工作流
- 不存在 → 常规开发流程

### 核心：单入口 Runtime Engine
所有 Trellis 操作通过 `.ga/trellis_runtime.py` 一个脚本完成：

```bash
python3 .ga/trellis_runtime.py <command> [args]
```

| 命令 | 等效 Claude Code 能力 | 说明 |
|------|----------------------|------|
| `enter` | SessionStart hook | 进入项目时调用，输出 context + task + spec |
| `before-write <path>` | PreToolUse hook | 写代码前调用，按文件路径匹配 spec 层 |
| `after-write <path>` | PostToolUse hook | 写完后调用，输出应执行的质量检查 |
| `cmd <name> [sub]` | /command 路由 | 动态路由到 .claude/commands/ 定义 |
| `skill <name>` | Skill 自动触发 | 读取 skill 定义并输出步骤 |
| `agent <name>` | Sub-agent dispatch | 加载 agent 定义 |
| `status` | 状态查询 | 当前 task/branch/developer |
| `spec [layer]` | Spec 浏览 | 直接输出某层 spec 内容 |

### GA 工作流（3步覆盖全部 Trellis 能力）
1. **进入项目**: `python3 .ga/trellis_runtime.py enter`
2. **每次写代码前**: `python3 .ga/trellis_runtime.py before-write <目标文件>`
3. **写完后**: `python3 .ga/trellis_runtime.py after-write <目标文件>`

按需调用:
- 需要具体工作流: `cmd start-work` / `cmd continue` / `cmd finish-work`
- 需要 brainstorm: `skill brainstorm`
- 需要质量检查: `skill check-backend` 或 `agent check`
- 查看设计指令: `cmd design kickoff`

### 用户如何使用（对话示例）

用户不需要手动执行任何脚本。正常用自然语言下达任务即可，GA 内部自动走 Trellis 流程。

**示例 1：新功能开发**
```
用户: 在 bar_smyze_opserver 项目里实现支付回调通知接口
```
GA 自动执行: enter → task create → brainstorm → before-write → 写代码 → after-write → check

**示例 2：继续未完成的任务**
```
用户: 继续之前的支付回调任务
```
GA 自动执行: enter → 读到 active task → status → 继续写代码

**示例 3：数据库设计**
```
用户: 帮我设计一下优惠券模块的表结构
```
GA 自动执行: enter → cmd db-design design → 输出设计方案

**示例 4：代码质量检查**
```
用户: 检查一下我最近改的代码
```
GA 自动执行: enter → agent check → git diff → 读 spec → 逐文件检查

**示例 5：指定 skill**
```
用户: 用 brainstorm 分析一下这个需求的实现方案
```
GA 自动执行: skill brainstorm → 输出方案

**关键点**: 用户只管描述需求，GA 根据 trellis_integration_sop 自动决定调用哪些 runtime 命令。
脚本是 GA 的内部工具，不是用户操作界面。

### 覆盖度
- 14 Skills → 全覆盖（动态读取 .trellis/.backup/ + .ga/skills/）
- 6 Agents → 全覆盖（读 .claude/agents/）
- 15+ Commands → 全覆盖（动态路由 .claude/commands/）
- Hook 注入 → enter/before-write/after-write 模拟
- Spec 匹配 → before-write 按文件路径自动匹配层

### 新项目接入
只需复制 `.ga/trellis_runtime.py` 到新项目 `.ga/` 下即可（脚本自动定位 .trellis/ 路径）。

### 坑点
- Runtime 不会自动触发，GA 必须主动调用（Agent-Pull 模式）
- `enter` 依赖 `.trellis/scripts/get_context.py`，确保其 common/ 依赖完整
- task archive 前必须确认代码已 commit
- 简单改动（typo/格式）可跳过 before-write/after-write

### 设计原则
- 不修改 .trellis/ 核心代码（只读引用）
- .ga/trellis_runtime.py 是唯一适配层，替代之前的 9 个 markdown 文件
- 动态路由优于静态复制（skill/command 定义变更时零适配成本）
- 与 Claude Code 等效但触发方式不同（Agent-Pull vs Hook-Push）
