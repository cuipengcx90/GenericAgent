
# R3 SOP覆盖盲区审计报告

**执行时间**: 2026-04-23  
**任务**: 盘点 memory/ 下所有SOP覆盖盲区，识别缺口与冲突，产出优先级排序

---

## 一、现有SOP完整清单（14个）

| SOP | 覆盖场景 | 访问频次 |
|-----|----------|---------|
| `tmwebdriver_sop` | 浏览器特殊操作/CDP/跨域/Cookie | 2次 |
| `ljqCtrl_sop` | 键鼠物理坐标控制 | - |
| `vision_sop` | 截图/Vision API | 6次(最高) |
| `autonomous_operation_sop` | 自主行动执行流程 | 5次 |
| `memory_management_sop` | 记忆写入规范(META) | 4次 |
| `memory_cleanup_sop` | L1/L2记忆整理原则 | - |
| `plan_sop` | 复杂多步任务规划 | 2次 |
| `scheduled_task_sop` | 定时JSON任务配置 | 6次 |
| `subagent` | 子Agent启动/IO协议 | 3次 |
| `procmem_scanner_sop` | 进程内存特征扫描 | - |
| `github_contribution_sop` | 向开源项目提PR | - |
| `trendradar_sop` | 热搜晚报/MCP调用 | 1次 |
| `verify_sop` | 任务验收执行规范 | - |
| `web_setup_sop` | Web工具链初始化 | 2次 |
| `adb_ui.py` | Android ADB UI自动化 | 3次 |

---

## 二、已发现的SOP冲突/过时（需立即修复）

### 🔴 冲突1：scheduled_task_sop 格式与实现不符
- **SOP声明**：`every_Nh`（如`every_1h`）是合法的repeat格式
- **实际报错**：`trendradar_hotspot.json` 使用 `"every_1h"` 导致 `ERROR: Invalid schedule format`
- **验证来源**：scheduler.log 实测报错（R2审计结果）
- **建议**：读 `reflect/scheduler.py` 源码确认实际支持的格式，修正SOP并修复hotspot配置

### 🟡 冲突2：procmem_scanner_sop 路径规范与系统规则矛盾
- **SOP写法**：`sys.path.append('../memory')` 
- **系统RULES**：`memory模块直接import(已在PATH,禁加虚假前缀)`
- **建议**：删除SOP中冗余的path.append示例，避免混淆

---

## 三、SOP覆盖盲区清单（按优先级）

### 🔴 P1：密钥/凭证管理SOP（高安全风险）
- **现状**：`keychain.py` 存在（1,898B），但**无对应SOP**
- **风险**：Agent在需要API key时可能从错误位置读取、硬编码、或直接暴露
- **建议**：补充 `keychain_sop.md`，规范：何时用keychain vs 环境变量、如何更新凭证、哪些key已存入

### 🔴 P1：通知推送统一SOP
- **现状**：飞书通知逻辑散落在 `reflect_common.py`/`notify_feishu_group.py`；trendradar_sop有部分通知路径；email/Bark等通道无记录
- **风险**：多个脚本重复实现通知、通道选择无规范、token在哪里存储不清楚
- **建议**：补充 `notify_sop.md`，统一：各通知通道API路径、token存储位置、优先级（飞书>？）

### 🟡 P2：异常恢复/错误处理SOP
- **现状**：各SOP分散提及"3次失败请求干预"，但无统一的错误分类、重试策略、回滚规范
- **风险**：遇到网络超时、文件锁、进程崩溃时处理方式不一致，可能数据损坏
- **建议**：补充 `error_recovery_sop.md`：错误分类（可重试/需干预/致命）、标准重试参数、回滚checklist

### 🟡 P2：本地Git工作流SOP
- **现状**：`github_contribution_sop` 只覆盖"给开源项目提PR"场景，**日常本地commit/branch/stash无规范**
- **风险**：自主任务修改代码后可能忘记commit，或commit message不规范导致历史混乱
- **建议**：补充本地Git规范：commit时机、message格式、何时push、.gitignore维护

### 🟡 P2：LLM调用/成本控制SOP
- **现状**：TODO中有"LLM成本分析"任务（尚未执行），但无SOP指导模型选择策略
- **风险**：自主任务随意使用昂贵模型，成本不可控
- **建议**：补充 `llm_call_sop.md`：任务→模型映射表、context压缩规范、何时用subagent分担

### 🟢 P3：系统环境健康检查SOP
- **现状**：无机器启动后的环境自检规范（Python路径、MCP服务状态、关键依赖包）
- **风险**：trendradar_morning永远SKIP的部分原因是机器10:00前未就绪，但无自检机制发现问题
- **建议**：补充 `env_healthcheck_sop.md`：启动后30秒内检查项、失败通知路径

### 🟢 P3：报告生成规范SOP
- **现状**：`autonomous_operation_sop` 只规定文件名格式`RXX_描述.md`，无报告内部结构规范
- **风险**：各次自主任务报告风格不一致，用户难以快速扫描关键信息
- **建议**：在autonomous_operation_sop中补充报告模板：结论先行→数据→建议结构

### ⚪ P4：多Subagent协调SOP
- **现状**：`subagent.md` 覆盖单个subagent，多个并行时资源竞争（端口/文件锁/内存）无规范
- **建议**：subagent.md补充"并行注意事项"章节

---

## 四、汇总

| 类别 | 数量 | 说明 |
|------|------|------|
| 现有SOP | 14 | 覆盖主流场景 |
| 已发现冲突/过时 | 2 | scheduled格式冲突(P1)、procmem路径冗余(P2) |
| 缺失SOP（独立文件） | 4 | keychain/notify/error_recovery/llm_call |
| 现有SOP需补充 | 3 | git工作流/env健康检查/报告结构/多subagent |

**核心结论**：密钥管理和通知推送是两个最高风险盲区（有工具无规范），scheduled_task_sop格式冲突是已造成实际损失的存量问题，建议优先修复。
