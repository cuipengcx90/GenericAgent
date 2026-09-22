---
name: agent_fleet_sop
type: sop
desc: 本机 7 个 Agent 的规范体系(用户级/项目级)与能力矩阵；GA 的规范缺口与该怎么定位
---

# 本机 Agent 舰队：规范体系 + 能力矩阵（2026-09-22 实测）

## 触发
- 问"某 Agent 会不会读到某规则文件 / 某个规范改动能不能被所有 Agent 看到"
- 问"GA 该不该当主力开发 Agent / 各 Agent 怎么分工"
- 要动 `~/.agent-profiles` 或某 Agent 的规则文件 / `.agent-profiles/projects/`

## 一、规则体系（谁读什么）
**用户级真源 = `~/.agent-profiles`**（唯一真源，产物分发到 6 家）：
- 真源：`rules/common.md`(20.7KB 通用) + `rules/<agent>.md`(专属 delta)，另有 `defaults/agent-common.md`
- 产物：`~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`、`~/.grok/AGENTS.md`、`~/.pi/agent/AGENTS.md`、`~/.omp/agent/AGENTS.md`、`~/.dsh/AGENTS.md`
- 分发器：`bin/agent-rules`（`agent-profile rules sync`）；`state/rules.json` 记 path/sha256/chars/generated_at
- 安全机制：sha256 漂移检测、首跑 `--adopt`、写前体积预检、写后自检、自动备份、整仓 git
- 自动化：`agent-profile shellinit` 给 6 个命令包 wrapper，启动前 `sync --if-needed`（无漂移 ~50ms）
- **GA 不在 6 家之内**；`~/.agent-profiles/projects/` 为空 ⇒ 项目级未实现

**读取机制矩阵（谁能吃文件内引用）**：
| Agent | 全局 | 项目 | `@path` import | 同目录多文件 |
|---|---|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `CLAUDE.md`(无则 `AGENTS.md`) | **支持(≤4 层)** | CLAUDE.md 优先 |
| Codex 0.155.1 | `~/.codex/AGENTS.override.md`→`AGENTS.md` | 逐层 `AGENTS.md` | 文档未提 | 逐层拼接，总 ≤32KiB |
| Grok | `~/.grok/` 四种名 | 逐层四种名 | **不支持** | 都扫，**单文件 ≤10000 字符(硬上限)** |
| Pi 0.86.0 | `~/.pi/agent/AGENTS.md` | 逐层 | **不支持** | 只取一个，AGENTS.md 优先 |
| OMP 18.2.6 | `~/.omp/agent/AGENTS.md` | 逐层 | — | — |
| DSH | `~/.dsh/AGENTS.md` | — | — | — |
| **GA** | **`memory/global_mem_insight.txt`+`global_mem.txt`(get_global_memory 注入)** | **不读 AGENTS.md/CLAUDE.md** | 无 | 无 |
> 铁律：`@AGENTS.md` 这种引用**只有 Claude Code 认**；跨 Agent 唯一可靠机制 = 同文件名 + 实体文件（或软链）。
> Grok 是最紧约束：opserver 项目 `AGENTS.md` 正文已 9243 字符（上限 10000）。
> `~/.grok/config.toml` 的 `[compat.claude] agents = false` 只关掉 `~/.claude/` 全局命名扫描（注释明说要避免共享层注入两遍），**项目目录行为未验**。

**项目级现状（以 opserver 为例，2026-09-22 01:01 重构）**：
- `AGENTS.md`(15986B/9243 字符/263 行) = `<!-- 由项目级规则源生成，勿手工编辑 -->` + `<!-- TRELLIS:START/END -->` 块 + 项目概况/架构/数据库铁律
- `CLAUDE.md`(11698B/6134 字符) = `@AGENTS.md` + Claude 专属段（表达约束等）
- **缺口**：头部声明"由规则源生成"，但**未找到生成器/真源**（`find *rule*` 空、`bin/agent-rules` 无项目级代码）⇒ 项目级目前是"人工维护的产物"，没有同步机制、没有体积闸门

## 二、GA 侧入口（与上面两套都不通）
- 注入面 = `get_global_memory()`：`memory/global_mem_insight.txt`(L1) + `memory/global_mem.txt`(L2) + L3 索引 + L0 META-SOP
- 全仓 grep `AGENTS.md|CLAUDE.md` 仅 1 命中（`frontends/workspace_cmd.py` 的注释）⇒ **用户级 common.md 和项目 AGENTS.md 对 GA 都是死文件**
- GA 另有 `plugins/project_mode.py`（自研项目模式：激活后每轮在 user message 追加 `temp/projects/<名>/project_memory.md` 的 L1 指针），与 Trellis/AGENTS.md 是两套
- 要让 GA 吃统一规范：需给 `get_global_memory()` 加"外部规则文件"入口（**改 GA 源码，须用户批准**）

## 三、能力面（实测目录计数，2026-09-22）
| 能力 | cc | codex | grok | pi | omp | dsh | GA |
|---|---|---|---|---|---|---|---|
| skills | 110 | 44 | 40 | 1 | 0 | 0 | morphling 吸收 + `~/.agents/skills` 共享 |
| subagents | 2 | prompts 5 | — | agent 70 项 | agent 39 项 | — | `memory/subagent.md` + temp 子代理日志 |
| MCP | `~/.claude.json` 10 个 | `config.toml` 9 段 | `[compat.claude] mcps` | 包机制 | 包机制 | — | `ga_mcp_client.py` |
| hooks | settings.json | — | — | 扩展 | plugins | plugins | `plugins/hooks.py` |
| 长期记忆 | CLAUDE.md | AGENTS.md | AGENTS.md | sessions | — | memory 4 项 | **L0–L4 五层** |
| 无人值守/编排 | — | — | — | — | — | — | 反射器 + herdr skill |

## 四、定位建议（已给用户，待其拍板）
- **GA = 主力调度 + 长任务执行者**（反射器/定时/无人值守/herdr 组队是独家能力）；日常写码交给 cc/codex
- 三步落地（均需用户批准）：① GA 纳入 `agent-profiles` 第 7 个 target，产物落到 `GenericAgent/memory/` 并由 `get_global_memory()` 带上（改源码）；② 给项目级补真源+sync+体积闸门（Grok 10000/Codex 32KiB）；③ `project_mode.py` 检测 `.trellis/` 或项目 `AGENTS.md` 时注入铁律指针

## 坑
- `~/.agent-profiles/state/rules.json` 的 chars 是"字符数"不是字节数（CJK 项目别按字节估）
- 别用 `grep -rn 大仓` 找生成器（opserver 含 .trellis/tasks 会超时 60s）；窄路径或 `--exclude-dir`
- 完整调研证据：`temp/agent_fleet_report_20260922.md`（本报告已在写后按告警复查过版本号/字符数/配置计数）

## 三、herdr 组队：GA 能当司机、不能当成员（2026-09-22 herdr 0.9.0 实测）
- `herdr agent start --kind` 的可能值（权威）＝23 个 CLI agent：pi, claude, codex, gemini, cursor, devin, agy, cline, omp, mastracode, opencode, copilot, kimi, kiro, droid, amp, grok, hermes, kilo, qodercli, qwen, maki, muse。**没有 ga**。
- `herdr-team/bin/teamprofile.py:29 KINDS` 硬编码同一集合，`validate` 对不在集合内的 kind 直接报错 `unsupported kind for <name>` ⇒ 写 `kind: ga` 会被拒；成员必须是 herdr 能识别并等"交互就绪"的 CLI agent。
- **"通用"指的是 skill 层与司机层**：README:118 明文 "GA / PI / OMP / 裸 shell 里行为一致" + SKILL.md §Portability（零 GA 依赖、仅 stdlib+PyYAML、herdr/teams-dir/通知全走 env）⇒ 同一套 handoff/team/autopilot skill 在 GA、PI、OMP、裸 shell 里都能当**驱动者**。
- GA 自己的"组队"＝`python agentmain.py --func/--task` 克隆自身（同模型同记忆，同构并行）；herdr 的组队是**异构**（SKILL.md 要求 reviewer 的 kind 必须与每个 coder 不同）⇒ 换脑复核只能靠 herdr，不是 GA 内部子代理能替代的。
- 结论形态：**GA 当 W0/司机 + 异构成员（pi/codex/omp/gemini…）干活 + GA 验收与记忆固化**。

## 四、调度底座与"双身份"（2026-09-22 实测，决定谁当值班长）
- **无人值守的底座是 macOS launchd（系统级，与 Agent 无关）**⇒ 任何有 headless 入口的 agent 都能被定时唤起：`pi -p`（**实测 rc=0 / 2.5s / 输出"正常"**）、`claude -p|--print`、`codex exec` 都在（`pi --help` 原文 "Non-interactive mode: process prompt and exit"）。
- 目前**只有 GA 注册了 plist**（9 条）：`com.cuip.genericagent-{scheduler,autonomous,price-alert,launch,ga-api,pet}` + `com.cuip.archive-{server,rebuild}` + `com.genericagent.desktop-bridge-repair`；PI/CC/codex 一个都没注册（`crontab -l` 空）。
- GA 反射器 plist 构成（`plutil -p` 原文）：`python3 -u agentmain.py --reflect <script>` + `KeepAlive=1` + `RunAtLoad=1` ⇒ 进程常驻、死了自动拉起；脚本内 `INTERVAL=120` 自轮询 `check()`，历史靠持久化文件累积。
- **PI/CC/codex 的 headless 是"一次调用一次退出"**，要常驻需自己加壳（launchd `StartInterval` 反复唤起，或自写 wrapper 循环）；分层记忆沉淀与通知通道也要自己接。
- ⚠️ 曾误述"只有 GA 有调度底座"——正确说法：**模式通用（PI 完全能做），差异是"GA 的壳已搭好在跑，PI 要自己搭"**。
- **PI / Claude Code / codex 都没有调度**：无对应 plist、`crontab -l` 为空；`pi --help` 无 schedule/background 子命令且 `~/.pi/tasks/` 是空目录；`~/.claude/daemon/daemon.log` 原文 `[supervisor] idle 5s with no clients — exiting` ⇒ 那是"空闲即退出"的后台 worker 监督器，**不是调度器**。
- **双身份名单**（herdr 白名单内且本机可用）：pi ✅ / claude ✅ / codex ✅ / omp ✅ / gemini ✅ —— 既能当司机又能当成员；**GA 只能当司机**。
- 便携性实证：`~/.pi/skills/` 里已装 `autopilot`（软链）⇒ PI 可直接驱动 herdr autopilot，无需 GA。
- Claude Code 已接 herdr：`~/.claude/settings.json` 的 hooks 里有 `~/.claude/hooks/herdr-agent-state.sh`，另有 `~/.orca/agent-hooks/claude-hook.sh`；env 含 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`、`teammateMode`、`teammateDefaultModel`。
- 结论：**组队灵活度 PI/CC > GA（双身份 vs 单身份）；无人值守是通用模式，GA 只是唯一"壳已搭好在跑"的那个**（PI/CC 要自己配 launchd + 自接记忆/通知）。两者不互替，但**别把"没配"当"不能"**。
