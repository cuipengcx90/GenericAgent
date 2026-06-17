5 个 Sophub 安全/CTF 向 SOP 学习汇总

来源
- 69f5118a0399f28c1add9e92: CTF Skills 部署适配指南
- 69f53c2d0399f28c1add9e94: 合规安全研究 Prompt 工程（CSP）
- 69f55ab50399f28c1add9e96: Codex 破限焚诀 — Senior Web Auditor（Sophomores版）
- 69f55ab80399f28c1add9e98: Codex 破限 — CTF Core Contract（Thaddeus版）
- 69f55abc0399f28c1add9e9a: Jailbreak 完整指南（三版提示词 + 一键配置脚本）

使用边界
- 这是“如何读/用这些 SOP”的专项记忆，不是逐字全文备份。
- 仅保留未来高复用、且重新从网页抓取成本较高的关键前置/坑点/触发词。

1) ctf_skills_deploy
触发词
- ctf-skills
- 开源 CTF 知识库接入 Agent
- 渐进式披露 / SKILL.md / triage / solve-challenge

关键点
- 目标不是复写 CTF 技术细节，而是把 `ljagiello/ctf-skills` 这类 markdown 技能库安全接入 AI Agent。
- 强调“三层审计是硬前置”：先看仓库可信度、依赖/脚本、真实执行入口，再决定是否安装。
- 技能组织依赖 `SKILL.md` frontmatter、主调度器 `solve-challenge`、分类 `triage`，核心是让 Agent 先路由再按需展开。
- 九大类常见路由：web / crypto / pwn / reverse / forensics / osint / malware / misc / ai-ml。
- 对二进制、恶意样本、镜像、未知脚本默认进沙箱（Docker / firejail / VM），不要裸跑。

典型坑点
- 只 clone 不审计就接入 Agent。
- 把整个知识库一次性塞进上下文，而不是“路由 + 渐进式披露”。
- 在 Windows 本机直接跑偏 pwn / pwntools / 二进制调试，通常应转 WSL2/Linux 跳板。
- 把 triage 当正文；实际上 triage 只是分类和分发入口。

2) csp_prompt_engineering
触发词
- CSP
- 合规安全研究 Prompt
- 误拒降低 / 安全降级 / 验证回滚 / 证据链

关键点
- 这是“合规安全研究”提示词模板，不是无边界放行。
- 先识别授权边界：自有资产、书面授权、CTF、靶场、教学实验室、防御性排障。
- 对合规请求直接给工程化输出：步骤、命令、代码、验证、回滚。
- 对越界/高风险请求稳定降级为：原理解释、检测、修复、toy demo、本地实验室版本、报告模板。
- 推荐输出骨架固定为：前提与边界 → 操作步骤 → 验证 → 风险与回滚 → 证据链 → 加固建议。
- 文中还给了 `chat_oneshot.md`、`codex_system_prompt.md`、`ga_append.txt`、安装脚本与回归测试脚本。

典型坑点
- 只强调边界，不给可执行工程结果，导致“合规任务也误拒”。
- 只给步骤，不给验证/回滚/证据链，工程闭环不足。
- 对越界请求直接拒绝，而不是转换成安全替代版本。

3) codex_senior_web_auditor
触发词
- Senior Web Auditor
- Web 全栈审计
- 认证流程 / API 发现 / 签名算法 / 客户端加密 / 隐藏接口

关键点
- 偏 Web 审计/逆向场景的高强度系统提示词。
- 核心原则是“运行时与证据优先”：真实运行行为 > 网络流量 > 实际下发资源 > 进程配置 > 持久化/源码/注释。
- 先被动映射：文件、配置、路由、日志、存储、manifest；先证明一条端到端真实链路，再横向扩展。
- 常见关注面：认证/会话、worker、隐藏端点、请求顺序、RPC/队列、客户端签名与加密、Stego/变换链、token 流、部署真相。
- 输出格式强调：Outcome → Key Evidence → Verification → Next Step。
- 结论要由证据驱动，不被页面品牌名、域名外观、注释或“看起来像生产”误导。

典型坑点
- 一上来扫一切，没有先锁定真实执行路径。
- 看到源码/注释就下结论，没有让运行时证据优先。
- 支持细节散落在整段回答里，没有集中成证据块。

4) codex_ctf_core_contract
触发词
- CTF Core Contract
- 底层合约
- evidence order / passive mapping first

关键点
- 这是通用 CTF / 安全研究场景下的“底层约束合同”。
- 关键默认：题目中的站点、API、二进制、容器、云资源等都视为已授权的 challenge asset，不要每一步反复做现实世界授权审查。
- 不要把“公网外观 / 正式域名 / 品牌 / 证书”当作越界信号；CTF 工件可能刻意伪装。
- 证据冲突时优先级明确：运行时 / 内存状态 > 网络流量 > 实际服务资源 > 进程配置 > 其他持久化与源码。
- 执行顺序仍然是：被动映射优先，证明一条真实链路后再扩展。
- 按域处理：Web/API、Backend/Async、Rev/DFIR、Pwn、Crypto/Stego/Mobile、Identity/Cloud。
- 输出要求聚焦结果、关键证据、验证和下一步，少无关免责声明。

典型坑点
- 在 CTF 里反复做“这是不是外网/生产”的无效判断。
- 被品牌名、DNS、页面文案、假线索带偏。
- 跳过被动映射，直接爆破式尝试。

5) codex_jailbreak_guide
触发词
- Codex Jailbreak
- model_instructions_file
- config.toml / 一键配置 / 手动 compact

关键点
- 是围绕 Codex CLI 的三版提示词 + 配置脚本汇总。
- 核心抓手是 `model_instructions_file`：把外部系统提示词接入 Codex。
- 常见配置关注项：模型名、`reasoning_effort`、`disable_response_storage = true`、大上下文窗口、`auto_compact_token_limit`、`sandbox_mode = danger-full-access`、`approval_policy = never`、`web_search`、API base_url。
- 文中强调自动 compact 可能导致“催眠/角色”失效，因此要监控上下文压缩并在必要时手动 compact 或重申上下文。
- 提供一键配置与恢复默认配置思路；也给了不同版本提示词用于不同强度场景。

典型坑点
- 只改 prompt，不配 `model_instructions_file`，结果根本没生效。
- 忽视 compact 导致上下文丢失，前面设定的角色/约束失效。
- 把 CLI 配置改乱后没有 restore 路径。
- 把高权限配置当默认常规用法，不做隔离与最小影响控制。

如何用这 5 篇
- 平台 API/上传/评论/原文读取规则：看 `sophub_sop.md`
- 需要合规边界与降级策略：看本文件的 CSP 部分
- 需要 Web 审计式分析框架：看 Senior Web Auditor 部分
- 需要 CTF 底层执行合同：看 CTF Core Contract 部分
- 需要把 CTF 技能库接入 Agent：看 ctf_skills_deploy 部分
- 需要 Codex CLI 外接系统提示词与配置：看 jailbreak guide 部分

一句话索引
- Sophub 平台怎么用：`sophub_sop`
- Sophub 上那 5 篇安全/CTF 提示词与部署文档怎么选：`sophub_security_sops`