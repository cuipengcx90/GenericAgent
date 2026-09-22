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

### L2 决策票据地图（只读视图层，2026-09-22 起）
> **单一入口文档**：想知道"现在跑什么/到哪一步/有什么问题"先看 `temp/ROOT_MAP.md`（通盘规划 `temp/DRIVER_AUTOPILOT_ROADMAP.md`，工具用法 `temp/l2_v0/README.md`）。
> 现状（2026-09-22）：**只做了 L2；L1/L3 未做**；阶段一进行中，阶段二/三未开始；GA gate = 0 ERROR / 2 WARN。

Trellis 管"单票状态机"，L2 管**跨票视图**：需求→票据→依赖→状态→**证据**。工具在 GA `temp/l2_v0/`（只读 `.trellis/`，产物写 `<repo>/.ga/l2/`；**权威永远是 `task.py`**）。

```bash
temp/l2_v0/bin/l2 next     # 开工前：现在能接的活 + 接力包（在仓库任意子目录可用，自动向上找 .trellis）
temp/l2_v0/bin/l2 gate     # 验收前：rc=0 通过 / rc=2 有 ERROR ⇒ 不得 ACCEPTED / CLOSE_READY
temp/l2_v0/bin/l2 index    # 派生活动过时（票据改了没重建索引）时重建
```
- **依赖边**来源优先级：官方可选字段 `task.json.meta.depends_on` > GA 本地台账 `.ga/l2/deps.json` > 旧写法 `dependsOn/dependencies`（`parent` 视为隐式前置）。官方对 `meta.depends_on` 是"不要求填、不生成、不写回，有就用"。
- `completed/archived` 必须能指出证据（commit/pr_url/**票据目录里除 task.json/prd.md 外的任何产物文件**）否则 ERROR ⇒ 所以 agent 干完活要留下痕迹，不能只改状态。
- 保活：反射器 `07_l2_gate.py`（30 min，全绿静默，仅 ERROR 私信，指纹去重 24h）。

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
- **任务会话态（2026-09-22 实测修复）**：Trellis 活跃任务只认会话键（`.trellis/.runtime/sessions/<key>.json`），无会话身份时不猜（多会话并存会拒绝）。适配层已自动注入 `TRELLIS_CONTEXT_ID=<GA会话键>`+`TRELLIS_PLATFORM=ga` ⇒ GA 下 `task current` 可解析（实测 rc=0）；`task current` 无任务时改为输出诊断（此前是 rc=1 零输出）；新增 `task adopt` 把遗留 `.current-task` 采纳为会话任务
- `.ga/` 在 opserver `.gitignore:79` 被忽略 ⇒ 适配层改动不进版本控制（换机/升级需另存）
- **L2 的两个真坑（已修，勿回退）**：①归档票据在 `tasks/archive/<YYYY-MM>/<id>/`，取证必须走解析器否则对归档任务全部假阳性报"无证据"；②证据规则别只认少数文件名（会漏 check.jsonl/implement.jsonl/research/ 这类约定产物）。验收器自己就会误判 ⇒ 必须有负样例夹具（`temp/l2_v0/fixtures/`）。
- 反射器（`agentmain --reflect`）循环是**先 `check()` 再 sleep(INTERVAL)**，上电即跑一轮；`check()` 返 None 就不唤起 Agent（零 LLM 成本）。判断"守护进程里跑没跑"要看副作用（产物 mtime），不能看日志——全绿本来就没输出。

### 设计原则
- 不修改 .trellis/ 核心代码（只读引用）
- .ga/trellis_runtime.py 是唯一适配层，替代之前的 9 个 markdown 文件
- 动态路由优于静态复制（skill/command 定义变更时零适配成本）
- 与 Claude Code 等效但触发方式不同（Agent-Pull vs Hook-Push）
