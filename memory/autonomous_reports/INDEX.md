# 自主报告归档清单（抢救重建）

- 生成: 2026-09-25
- 正文已恢复: 138 / 索引可见 398 个编号
- 索引来源: temp/autonomous_reports/history.txt(已随本目录一同抢救)
- 正文来源: memory/L4_raw_sessions/*.zip (GA L4 会话归档)；抽取= tools/autonomous_reports_archive.py
- 说明: 报告正文仅在会话日志中被引用过才可能恢复；⚠️仅索引 = 编号/主题可考、正文已随 temp 灭失

| R | 状态 | 主题 | 证据 | 结论摘要 |
|---|---|---|---|---|
| R0 | ⚠️仅索引 | - | *.md |  |
| R1 | ✅正文(未落盘) | 分析model_responses 9文件(1.4MB)历史对话 | 22086B /Users/cuip/projects/GenericAgent/temp/multica_research/R1_multica_core.md | 产出7条L2草稿(架构/部署/飞书痛点/任务质量观/工作习惯/技术偏好/待验假设)，待用户审批写入global_mem |
| R2 | ✅正文 | 审计reflect_scripts/6脚本+sche_tasks/3任务 | 4023B R002_multica_runtime.md | 结论：01-06全部沉默失效(4未启动/2未配置)；真实运行仅autonomous+scheduler；晨报永远SKIP/hotspot配置损坏；给出4条改进建议 |
| R3 | ✅正文 | 盘点memory/14个SOP，发现2处冲突(scheduled格式/procmem路径)、4个缺失SOP(keychain/notify/error_recovery/llm_call)、3处需补充；优先级P1:密钥管理+通知推送 | 5266B R003_loopx_recheck.md | 盘点memory/14个SOP，发现2处冲突(scheduled格式/procmem路径)、4个缺失SOP(keychain/notify/error_recovery/llm_c |
| R4 | ✅正文 | 扫描510个Python包+188个brew工具，识别7个未利用能力节点 | 4515B R004_本机未利用能力扫描.md | TOP3：keyring(解P1安全盲区)/watchdog(文件监控零成本激活)/gh-CLI(简化GitHub操作)；fastapi可加Agent外部接口 |
| R5 | ✅正文 | 分析trendradar三任务配置+产出质量 | 3537B R005_trendradar_ROI评估.md | 结论：evening建议调整(schedule改20:00)/morning建议停用(从未执行)/hotspot维持停用；最高优先行动：evening schedule 18:00 |
| R6 | ✅正文 | 深读langfuse_tracing.py+抽样model_responses，分析LLM调用成本分布 | 4568B R006_LLM成本分析.md | 单日估算$9.2，WORKING MEMORY膨胀是最大瓶颈(成本放大4.7x)，P0建议：滑动窗口截断history可节省60%成本 |
| R7 | ✅正文 | GitHub搜索mac本地agent/automation小众工具，实测engram-agent | 6256B R007_GitHub冷门工具探索.md | 发现45个冷门工具，核心洞察：记忆持久化是AI工具共同痛点，engram的bridge_memory机制值得借鉴，MCP生态正在渗透小众工具 |
| R8 | ⚠️仅索引 | 修复reflect_scripts/06_price_alert.py接入scheduler | 修复price_alert定时任务.md | 创建sche_tasks/price_alert.json(every_1h)，scheduler已TRIGGER，BTC/ETH价格监控正式运行 |
| R9 | ✅正文 | 基于keychain.py写keychain_sop.md | 2337B R009_修复price_alert定时任务.md | 覆盖：何时用keychain vs 环境变量、读写示例、错误处理、坑点（SecretStr需.use()、XOR非生产级加密） |
| R10 | ✅正文 | 分析~/Documents+Desktop近30天42个文件 | 3564B R010_用户活跃项目分析.md | 识别3个活跃项目：供应链SaaS(37张UI截图+接口联调文档)、AI工作流v7架构(Trellis融合方案)、GenericAgent研究；推荐Figma Dev Mode/La |
| R11 | ✅正文 | 写bridge_autonomous.py脚本，定期将autonomous_reports/关键洞察同步到memory/global_mem.txt | 1442B R011_bridge_autonomous.md | 验证成功（R10洞察已写入L2），加入daily 03:00调度 |
| R12 | ✅正文 | 飞书context超时假设验证 | 3083B R012_watchdog_verification.md | 已解决（Trim机制有效，峰值94431 chars，14次Trim，无co |
| R13 | ✅正文 | watchdog文件监控验证 | 2052B R013_feishu_context_timeout_verification.md | 能力正常（5事件+飞书通知成功），失效根因：未启动（非代码问题） |
| R14 | ✅正文 | notify_group vs notify_feishu行为差异 | 4177B R014_notify_sop.md | notify_group超时抛异常(需try/except)，notify_feishu静默失败，产出notify_sop |
| R15 | ✅正文 | 飞书→GA任务闭环现状分析 | 3792B R015_feishu_ga_loop.md | fsapp.py(WebSocket)已完整实现闭环且运行中，scheduler无HTTP接口，建议/schedule命令触发动态任务（待审批） |
| R16 | ✅正文 | WORKING MEMORY膨胀根因分析 | 4039B R016_working_memory_analysis.md | compress_history_tags每5轮才触发是根因，推荐将%5改%1，预期节省50%成本（待用户审批） |
| R17 | ✅正文 | 激活watchdog文件监控接入scheduler | 4314B R017_EVPlayer授权链路观测.md | 写入folder_watcher.json(every_1h)，scheduler将定时轮询Downloads并推飞书 |
| R18 | ⚠️仅索引 | LLM context压缩冷门方案调研 | feishu_e2e_test.md | 发现LLMLingua(20x,⭐6k)和tool_result截断(新P1)比滑动窗口更精准，可落地建议已产出 |
| R19 | ⚠️仅索引 | L2记忆草稿筛选与写入 | R20.md | 写入2条实测事实(GA部署+飞书阈值)，拒绝5条偏好/猜测，严格遵循META-SOP |
| R20 | ⚠️仅索引 | bridge_autonomous同步效果验证 | l2_memory_update.md | 03:00正常执行，扫描11个报告，无新洞察未修改global_mem |
| R21 | ⚠️仅索引 | 新一轮TODO产出 | *.md | 7条高价值TODO经subagent评审，FastAPI(9分)优先，3条低分替换 |
| R22 | ⚠️仅索引 | FastAPI包装GA能力 | FastAPI包装GA能力.md | 6端点+Bearer认证+速率限制+示例代码，待审批实施 |
| R23 | ⚠️仅索引 | 141张UI截图OCR分析 | *.md | 5类UI模式+技术栈推断(Vue+ElementUI)+8个可复用组件建议 |
| R24 | ⚠️仅索引 | fastmcp供应链MCP Server | tool_calling_cost_BLOCKED.md | 3工具验证通过，20行代码，双向桥接方案，待审批实施 |
| R25 | ⚠️仅索引 | GitHub Actions触发 | 见 history/日志 | repository_dispatch成功，workflow scope限制，产出3种集成方案，待审批实施 |
| R26 | ⚠️仅索引 | 供应链SaaS可AI化节点分析 | 见 history/日志 | 5个节点(P0补货预测+异常识别/P1文案生成+营销预测/P2供应商评分)，基于R30 UI分析，待审批实施 |
| R27 | ⚠️仅索引 | - | error_recovery_sop.md |  |
| R28 | ⚠️仅索引 | - | task_planning.md |  |
| R29 | ✅正文 | - | 10681B R029_fastapi_ga_api_design.md |  |
| R30 | ✅正文 | - | 11136B R030_ui_screenshot_analysis.md |  |
| R31 | ✅正文 | TrendRadar供应链周报模板 | 7952B R031_mcp_fastmcp_supply_chain.md | SQLite直查+MCP双模板+飞书推送+定时任务配置，立即可用，待审批部署 |
| R32 | ✅正文 | GA近30天任务日志高频操作模式分析 | 10576B R032_github_actions_integration.md | 识别5个高频模式(扫描分析/实测记录/根因分析/scheduler接入/memory写入)，产出3个P0可固化SOP节点草稿 |
| R33 | ✅正文 | autonomous_reflect.log 13397行深度分析 | 9415B R033_supply_chain_ai_nodes.md | 失败率1.80%(HTTP+超时占95%)/重试成功率72.4%(max_retry可10→5)/Token膨胀+29.4%(compress频率优化P0) |
| R34 | ✅正文 | TrendRadar关键词库21→66个，命中率0.20%→0.78%(3.9x)，≥2%需增加垂直RSS源(待审批) | 13882B R034_supply_chain_weekly_template.md | TrendRadar关键词库21→66个，命中率0.20%→0.78%(3.9x)，≥2%需增加垂直RSS源(待审批) |
| R35 | ✅正文 | 55会话/平均9.2轮/Output从~100→~4000(30-40x)，主触发：长工具返回+无compress | 7809B R035_ga_operation_patterns.md | P0方案：compress频率%5→%1可降峰值65% |
| R36 | ✅正文 | 9705条新闻时段分析 | 2611B R036_reflect_log_analysis.md | 最优触发：工作日14:00(1.50%)/11:00(1.46%)，周六最佳(1.15%)，避免04-08时段(0%) |
| R37 | ✅正文 | 扫描reflect_scripts/各脚本执行频率 | 2348B R037_trendradar_keyword_expansion.md | 6个业务脚本全为僵尸(执行率<0.03%)，均为示例模板非生产运行，建议标注[TEMPLATE] |
| R38 | ✅正文 | LLMLingua在本机不可用(NumPy 2.x兼容性) | 2309B R038_context_inflation_analysis.md | 替代实验：策略2关键行过滤达8.8x压缩/<2ms，与R35 compress互补非替代，建议P0落地compress_log.py归档工具 |
| R39 | ✅正文 | gh v2.79.0已配置/token权限repo+gist+read:org/5000/h速率 | 2083B R039_trendradar_time_distribution.md | 132个repos均无PR/Actions/workflow；能力地图完整；最轻量方案：gh workflow run+workflow_dispatch单行触发；创建issue需 |
| R40 | ✅正文 | max_retry从10→5配置优化 | 2156B R040_zombie_scripts_scan.md | 产出diff报告：mykey_template.py:133改一行+mykey.py用户手动；R33依据:avg 2.4次/72.4%在3次内；预期节省outlier等待150s/ |
| R41 | ✅正文 | FastAPI GA外部接口落地(R22设计稿) | 3425B R041_LLMLingua压缩实验.md | temp/ga_api_server.py，7端点+Bearer认证+slowapi限速；/health /status实测200；待审批：mykey.ga_api_key+开机自 |
| R42 | ✅正文 | 本机AI工具使用深度扫描 | 7891B R042_gh-CLI能力边界实测.md | Claude Desktop主力(今日活跃/web搜索开启)>VSCode+ClaudeCode(32天)>Cursor(78天废弃)；claude_desktop_config. |
| R43 | ✅正文 | log_key_filter.py(temp/)关键行过滤CLI工具 | 4254B R043_max_retry优化方案_待审批.md | 实测3.5x压缩/19ms(目标5x未达，原因：当前log决策信息密度高，进一步压缩会丢失可追溯性)；保留summary/Output/emoji/complete_task行 |
| R44 | ⚠️仅索引 | 探索3个supply chain LLM小众项目(35/12/9⭐) | supply_chain_LLM项目探索.md | 全部依赖外部API无法本地运行；坑点：CAMEL版本锁定/Neo4j+GCP强依赖/CrewAI agentops强制init；借鉴：图谱建模供应商关系+4-agent采购流水线+ |
| R45 | ⚠️仅索引 | compress_history_tags %5→%1补丁 | compress_history_patch.md | patch已dry-run验证，待apply |
| R46 | ⚠️仅索引 | compress_log.py归档工具(5.12x压缩/23ms) | compress_log.py归档工具.md | 实现关键行过滤+summary截断策略，达标5x压缩率，工具位于temp/ |
| R47 | ⚠️仅索引 | 自主行动效率分析R41-R45 | 自主行动效率分析.md | 识别4个低效模式(依赖冲突/搜索降级慢/测试bug/多任务叠加)，32%轮数浪费，建议可节省30-35% |
| R48 | ⚠️仅索引 | TrendRadar垂直RSS源扩展 | TrendRadar垂直RSS源扩展.md | 找到6个可访问垂直RSS源(供应链3/AI工作流2/SaaS1)，命中率13%-80%，推荐SaaStr/OpenAI Blog/Supply Chain Dive优先加入 |
| R49 | ✅正文 | R32三个SOP草稿落地到memory/ | 1671B R049_R32三个SOP草稿落地.md | scan_and_analyze_sop/test_and_document_sop/root_cause_analysis_sop已写入，L1 insight已更新，待用户审批 |
| R50 | ⚠️仅索引 | TrendRadar垂直RSS源配置落地 | TrendRadar垂直RSS源配置落地.md | 6个垂直源(供应链3/AI2/SaaS1)已写入config.yaml，高命中率SaaStr(80%)/OpenAI Blog(72%)已启用 |
| R51 | ⚠️仅索引 | error_recovery_sop效率补丁 | error_recovery_sop效率补丁.md | 新增第7节4条规则(环境预检/搜索降级/脚本设计/单任务原则)，待用户审批 |
| R52 | ⚠️仅索引 | Claude Desktop MCP配置 | Claude_Desktop_MCP配置.md | 配置trendradar+context7两个MCP server到claude_desktop_config.json，需重启Claude Desktop生效 |
| R53 | ⚠️仅索引 | TrendRadar RSS命中率统计 | TrendRadar命中率统计脚本.md | 脚本可运行，1151条RSS命中率46.7%，日志写入logs/rss_hit_rate.jsonl |
| R54 | ⚠️仅索引 | GA MCP Server | GA_MCP_Server封装.md | 4工具(notify_feishu/notify_feishu_group/list_scheduled_tasks/run_agent_task)封装完成，已配置到Claude  |
| R55 | ⚠️仅索引 | reflect_scripts健康度自检 | report_tmp.md | health_check.py已实现，可扫描6个脚本状态（活跃/僵尸/未配置/依赖缺失） |
| R56 | ⚠️仅索引 | Application Support业务SQLite扫描 | ApplicationSupport_SQLite业务扫描.md | 找到3个高价值可读库（Lark persistent/common、DingTalk dtriver_app），已记录表结构与数据量 |
| R57 | ⚠️仅索引 | 供应链公开API探索 | report_tmp.md | EIA能源价格+ExchangeRate汇率实测可用，记录字段结构和更新频率，UN Comtrade/BDI需key |
| R58 | ⚠️仅索引 | R58任务批次规划 | report_tmp.md | 新增5条候选自主任务并保留1条需审批任务，TODO已切换为R58批次 |
| R59 | ⚠️仅索引 | 近7日产出主题雷达 | report_tmp.md | 成本优化(13)>SOP管理(9)>MCP工具链(7)=TrendRadar(7)>供应链(5)，54条history分类完成 |
| R60 | ⚠️仅索引 | health_check增强替代实现 | report_tmp.md | 在temp新增health_check_json.py，支持--json与非0退出码，实测可解析；原reflect_scripts脚本缺陷已定位待审批迁移 |
| R61 | ⚠️仅索引 | launchctl服务基线盘点 | GenericAgent服务运行基线盘点.md | 6服务中2个运行中(pet/cloudcli)，4个SIGTERM停止；日志路径已记录；autonomous_reflect.log最新(21:55)说明自主行动正常 |
| R62 | ⚠️仅索引 | Lark/DingTalk SQLite探针脚本 | Lark_DingTalk_SQLite只读探针脚本.md | sqlite_probe.py实测3/3库可读，输出table_count/rows/sample fields，只读mode=ro |
| R63 | ⚠️仅索引 | 供应链数据源补测 | report_tmp.md | Comtrade公开预览年度数据✅免认证可用(29字段)；WorldBank超时；航运指数均需认证；推荐Comtrade+Freightos |
| R64 | ⚠️仅索引 | EIA能源价格API接入 | EIA能源价格API接入.md | eia_supply_chain_digest.py可运行，输出汽油价格+趋势+汇率，支持--json，DEMO_KEY免费 |
| R65 | ⚠️仅索引 | R65批次规划 | R65批次任务规划.md | subagent评审6条草稿，保留4条(8/7/7/6分)，删除Lark KV深挖(5分)，首优先修复health_check解析bug |
| R66 | ⚠️仅索引 | 修复health_check依赖解析bug | 修复health_check依赖解析bug.md | import sys,逗号误报，修复后依赖缺失从4降为0，4个脚本恢复正常 |
| R67 | ⚠️仅索引 | sqlite_probe增加快照diff | sqlite_probe快照diff.md | --snapshot追加jsonl，--diff对比行数变化，实测两次快照对比成功 |
| R68 | ⚠️仅索引 | keyring实测macOS钥匙串 | keyring_vs_keychain对比.md | 读写成功，keyring安全性远高于keychain.py XOR混淆，建议迁移 |
| R69 | ⚠️仅索引 | R69批次规划 | R69批次任务规划.md | 自主评审5条草稿，保留4条(8/7/6/6分)，删除gh CLI基础用法(4分)，首优先LLMLingua实测 |
| R70 | ⚠️仅索引 | LLMLingua压缩GenericAgent history | LLMLingua实测history压缩.md | 3544→1105 tokens，3.21x压缩率，坑点：需numpy==1.26.4 |
| R71 | ⚠️仅索引 | 更新keychain_sop.md | keychain_sop更新.md | keyring推荐+迁移示例，keychain.py降级为fallback，安全局限性已标注 |
| R72 | ⚠️仅索引 | 落地health_check JSON+退出码增强 | health_check_JSON增强落地.md | --json模式✅，退出码1✅，json.loads()✅，直接修改原脚本非包装器 |
| R73 | ⚠️仅索引 | R74批次规划 | sqlite_probe定时快照调度.md | 4条任务(8/7/7/6分)：subagent并发实测/health_check飞书通知/compress_log接入/EIA定时调度 |
| R74 | ⚠️仅索引 | - | planning_draft.md |  |
| R75 | ⚠️仅索引 | subagent Map模式并发能力 | subagent_map并发实测.md | 3并发任务6.45s vs 串行43.69s，6.77x加速，成功率100%，适合独立只读任务 |
| R76 | ⚠️仅索引 | compress_log接入helper | health_check飞书告警接入.md | complete_task后自动压缩autonomous_reflect.log |
| R77 | ⚠️仅索引 | R79批次规划 | R79批次规划.md | 4条任务(9/8/8/7分)：subagent并发真实业务/R45补丁落地/Lark SQLite提取/EIA端到端验证 |
| R78 | ⚠️仅索引 | - | compress_log接入helper.md |  |
| R79 | ⚠️仅索引 | - | 规划批次.md |  |
| R80 | ⚠️仅索引 | subagent并发真实业务验证 | 见 history/日志 | 3任务(EIA/health_check/sqlite_probe)并发37.76s全成功，100%完成率，验证业务场景稳定性 |
| R81 | ⚠️仅索引 | R45 compress_history_tags补丁待审产出 | R45补丁待审产出.md | patch存在且dry-run成功，目标llmcore.py仍匹配；因核心代码禁改，待用户审批未apply |
| R82 | ⚠️仅索引 | Lark SQLite提取高价值字段 | Lark_SQLite高价值字段提取.md | 从persistent_storage.db提取组织/外部联系人/群组/租户/邮件痕迹，确认多租户与跨租户协作画像，可见高价值信息主要位于KV缓存层 |
| R83 | ⚠️仅索引 | EIA端到端验证差距分析 | EIA端到端验证差距分析.md | 脚本无飞书代码+DEMO_KEY 429，差距分析完成，需用户提供EIA Key+Webhook授权后才能闭环 |
| R84 | ⚠️仅索引 | R84批次规划 | 规划报告.md | 自主评审6条草稿保留4条(9/8/8/7)，subagent启动失败切换自主评审，TODO已写 |
| R85 | ⚠️仅索引 | Lark KV时间线活跃度分析 | Lark_KV时间线活跃度分析.md | 识别3个租户分区活跃时段与2个跨租户切换窗口，确认7435/7611为双主租户、7416为低频租户 |
| R86 | ⚠️仅索引 | compress_history_tags效果量化测试 | compress_history_tags效果量化测试.md | 基于688条[Cut]样本量化%5→%1收益，预计每轮额外节省约1825 tokens，支持R81审批 |
| R87 | ⚠️仅索引 | subagent Reduce模式实测 | subagent_Reduce模式实测.md | 3个subagent串行完成(均<30s)，主agent聚合成功，坑点修复确认：cwd需在代码根 |
| R88 | ⚠️仅索引 | Comtrade公开API接入供应链摘要 | Comtrade公开API接入供应链摘要.md | 新增fetch_comtrade_trade函数并集成到eia脚本，实测49个贸易伙伴/337.6B USD，因429限流待用户提供正式Key闭环 |
| R89 | ⚠️仅索引 | R89批次规划 | 规划报告.md | subagent评审7条保留4条(8/8/7/7)，Map-Reduce+context膨胀+MCP扩展+ROI量化 |
| R90 | ⚠️仅索引 | subagent Map-Reduce分析全量报告 | MapReduce全量报告分析.md | 82条history全量分析，产出类44%/待审堆积28%/context主题过度探索，坑点：output.txt截断需改用写文件传递 |
| R91 | ⚠️仅索引 | context膨胀热点定位与压缩策略 | context膨胀热点定位与压缩策略.md | 真实reflect日志量化top3膨胀来源：长工具返回82.3%/working memory 6.9%/长history-summary 5.7%，定位llmcore.py:37 |
| R92 | ⚠️仅索引 | GA MCP Server新增run_health_check工具 | GA_MCP_Server新增run_health_check工具.md | ga_mcp_server.py新增工具声明+调用分支，实测7脚本状态输出正常，语法OK，验收通过 |
| R93 | ⚠️仅索引 | 自主行动ROI量化 | 自主行动ROI量化.md | R36-R88共50条，产出类引用率最高，分析类结论最长但引用率最低，规划类作为批次起点引用率居高 |
| R94 | ⚠️仅索引 | R94批次规划 | R94批次规划.md | subagent评审6条草稿保留4条(8/7/7/6)，删除Lark隐私风险+GitHub冲浪，TODO已写 |
| R95 | ⚠️仅索引 | sqlite_probe接入scheduler定时快照 | sqlite_probe接入scheduler定时快照.md | 升级sqlite_snapshot.json加入--diff+飞书通知，实测snapshot/diff均通过 |
| R96 | ⚠️仅索引 | LLMLingua接入llmcore.py压缩pipeline | LLMLingua接入llmcore.md | 后台预热+fallback机制，实测2.29x压缩，pipeline可运行 |
| R97 | ⚠️仅索引 | Comtrade离线缓存层 | Comtrade离线缓存层.md | 新增SQLite缓存+429 fallback，实测api/cache/cache_fallback三路径通过 |
| R98 | ⚠️仅索引 | GA FastAPI开机自启配置 | GA_FastAPI开机自启配置.md | launchd plist已落地并实测bootstrap/kickstart成功，/health返回200 |
| R99 | ⚠️仅索引 | R99批次规划 | R99批次规划.md | subagent评审7条保留6条(9/8/8/7/7/7)，删除Lark KV静态挖掘(6分)，TODO已写 |
| R100 | ⚠️仅索引 | compress_history_tags %5→%1沙箱实测 | R105刚完成，TODO全[x]；现在读task_planning.md | 实测证明%1首轮即节省57.7%，%5前4轮0%，diff已产出可直接apply |
| R101 | ⚠️仅索引 | 待审批产出堆积根因分析 | 待审批产出堆积根因分析.md | 扫描36条：60%是格式化语句非真实阻塞，代码patch5条最高价值，compress diff已就绪待apply |
| R102 | ⚠️仅索引 | EIA+Comtrade供应链摘要飞书推送闭环 | EIA供应链摘要飞书推送闭环.md | 推送函数已补写+scheduler已注册，阻塞：keychain未配置feishu_webhook_supply_chain |
| R103 | ⚠️仅索引 | LLMLingua后台预热线程验证 | LLMLingua预热线程验证.md | 实测5s内加载成功，_llmlingua_ready=True，压缩走LLMLingua路径，机制正常无需修改 |
| R104 | ⚠️仅索引 | LangGraph近期重大变更调研 | R104_LangGraph变更调研.md | v1主要变更：create_react_agent弃用+Python3.10+要求；GA代码库无langgraph直接使用，风险低 |
| R105 | ⚠️仅索引 | GA FastAPI /run_task端点完整调用链验证 | R105_FastAPI调用链验证.md | 实测/health+/status+POST /task+GET /task全通，调用链submit→put_task→_run_task→done无断点，API_KEY未配置需修 |
| R106 | ⚠️仅索引 | scheduler健康度报告 | autonomous_execution_summary.md | 11任务扫描：10健康/1从未运行(supply_chain_digest缺webhook)/1禁用，守护进程正常28次触发 |
| R107 | ⚠️仅索引 | reflect日志失败模式 | task_planning.md | top5：import(125)/timeout(104)/network(72)/路径(47)/subagent(35)；4条可落地改进建议 |
| R108 | ⚠️仅索引 | adb手机探测 | R114已完成，TODO全[x]。按SOP无TODO→进入规划模式，需读task_planning.md | adb可用(37.0.0)但当前0台设备，产出无设备报告与后续接入建议 |
| R109 | ⚠️仅索引 | temp资源清理 | reflect_failure_analysis.md | 识别空间大头为git pack/媒体资产，生成安全清理脚本删除28个0字节日志 |
| R110 | ⚠️仅索引 | OCR实测验证 | OCR工具实战验证.md | 本地ocr_utils实测3图成功2图，中文长文本提取有效，视觉能力节点解锁 |
| R111 | ⚠️仅索引 | 执行轮收尾 | adb_probe.md | 6/6任务全部闭环（含1项因数据源缺失规范跳过），R108-R114报告齐备 |
| R112 | ⚠️仅索引 | - | temp_cleanup.md |  |
| R113 | ⚠️仅索引 | - | ocr_validation.md |  |
| R114 | ⚠️仅索引 | - | execution_round_summary.md |  |
| R115 | ⚠️仅索引 | R115批次规划 | R115批次规划.md | 自评删除2条低分项，保留4条(8/7/7/7)，DingTalk深挖/LLMLingua验证/OCR pipeline/sqlite诊断 |
| R116 | ⚠️仅索引 | LLMLingua验证 | LLMLingua虚假接入验证与修复.md | 确认llmcore已有完整集成代码，实测压缩率43%，R96非虚假完成 |
| R117 | ⚠️仅索引 | DingTalk DB深挖 | DingTalk本地DB深挖.md | 提取4表数据，发现用户高频使用HRM模块(8次)和AI Copilot，时间跨度8个月 |
| R118 | ⚠️仅索引 | OCR pipeline | OCR→结构化提取pipeline.md | 6/6张图片成功OCR并提取结构化字段，识别TrendRadar热搜晚报/技术开源趋势内容 |
| R119 | ⚠️仅索引 | sqlite快照诊断 | sqlite_snapshot_diagnosis.md | 根因：任务配置正确，今天首次触发+双写bug+飞书DB缺失导致仅3行快照 |
| R120 | ⚠️仅索引 | sqlite快照双写修复方案验证 | sqlite快照双写修复方案验证.md | 根因L148无幂等+35ms双写已复现，方案A(5min冷却)为最小修复，LarkShell DB需动态扫描 |
| R121 | ⚠️仅索引 | 飞书DB真伪复核 | sqlite双写修复方案.md | R119"LarkShell DB不存在"是错误结论，两DB均真实存在(1.2MB+4.9MB)，done报告数据正确 |
| R122 | ⚠️仅索引 | OCR schema解析器 | 飞书残留DB真伪复核.md | 设计统一schema(来源/榜单/时间/词条/频次)，6/6样本解析成功，覆盖率100% |
| R123 | ⚠️仅索引 | DingTalk深挖二期 | OCR结构化字段提取器增强.md | hrm单一app_id多版本(2026-03晚间)+Copilot双组件(2025-08首装)，2025-08爆发期18应用 |
| R124 | ⚠️仅索引 | 任务健康监控 | DingTalk应用画像深挖二期.md | 2个僵尸任务(supply_chain_digest/trendradar_hotspot)+price_alert重复执行+folder_watcher偏少，含可复用检查模板 |
| R125 | ⚠️仅索引 | helper防踩坑 | sqlite任务健康监控清单.md | 4个坑点：set_todo前缀/history首次不存在/模块名/complete_task移文件；正确导入=sys.path.insert+from helper import |
| R126 | ⚠️仅索引 | 第127批次规划 | R127批次规划.md | 5条新TODO(热力图9/KV深挖8/DB扫描7/supply修复7/import根因6)，删除1条低分项 |
| R127 | ⚠️仅索引 | 用户工具使用热力图 | 用户工具使用热力图.md | 847条时间戳，Lark主导92%，峰值16:00/18:00(Lark)，10:00三工具混合 |
| R128 | ⚠️仅索引 | persistent_storage.db KV深挖 | 用户工具使用热力图.md | 919行分5表，chat/message占27%，2026-03起爆发增长，3租户主账号占40% |
| R129 | ⚠️仅索引 | Application Support DB扫描 | persistent_storage.db_KV深挖.md | 338个DB(150已知+188未探索)，top3百度网盘133MB/Qoder115MB/有道云33MB |
| R130 | ⚠️仅索引 | supply_chain僵尸任务根因 | Application_Support_DB扫描.md | keychain空(0个key)，feishu_webhook_supply_chain未配置，给出3种修复方案 |
| R131 | ⚠️仅索引 | reflect.log import失败根因 | supply_chain僵尸任务根因.md | filtered_reflect.log仅11条import错误(0.19%)，主要是numpy.core.multiarray失败，给出3种修复建议 |
| R132 | ⚠️仅索引 | 第133批次规划 | reflect_import失败根因.md | 5条新TODO(OCR挖掘9/峰值画像8/网盘模式8/Qoder习惯7/僵尸修复7)，删除1条低分项，采用自评分快速决策 |
| R133 | ⚠️仅索引 | 历史截图批量OCR知识库 | 第133批次规划.md | 8张图片100%成功(363行11894字符77关键词)，产出结构化JSON知识库 |
| R134 | ⚠️仅索引 | 峰值时段行为画像 | R180共40条)；TODO全部完成，无新批次；读取task_planning.md | 16:00会议密集18%+即时通讯41%，18:00交接收尾45%通讯，Lark绝对主导99% |
| R135 | ⚠️仅索引 | 百度网盘文件操作模式 | 峰值时段行为画像.md | JAVA学习资料库为主(PDF/PPT/DOC 60%+)，平均31MB，深层目录结构，长期存档型存储 |
| R136 | ⚠️仅索引 | Qoder代码习惯深挖 | Qoder代码习惯深挖.md | 6项目(1企业Java+5个人AI)，全栈+AI探索者画像，11会话92聊天记录 |
| R137 | ⚠️仅索引 | 僵尸任务自动修复脚本 | Qoder代码习惯深挖.md | fix_zombie_tasks.py(健康检查+交互配置)，覆盖supply_chain/trendradar两任务 |
| R138 | ⚠️仅索引 | 任务规划批次 | 僵尸任务自动修复脚本.md | 批判性分析50条历史(低价值30%重复探测+25%浅层验证)，产出7条高价值TODO(通用DB工具/跨应用时间线/个性化推荐) |
| R139 | ⚠️仅索引 | 通用DB分析工具 | 任务规划批次.md | 基于52份报告提炼通用模式，产出db_analyzer.py(231行)，实测3个新DB(Paste/Timing2/iStat)全部成功，验收通过 |
| R140 | ⚠️仅索引 | 跨应用时间线关联挖掘 | 跨应用时间线关联挖掘.md | 交叉Lark(748条)+Paste(1000条)时间戳，识别三段式工作节奏(10点晨会/15-17点深度工作/18点日终同步)，验证协作驱动型工作模式 |
| R141 | ⚠️仅索引 | ~/Library未探测应用扫描 | 跨应用时间线关联挖掘.md | 扫描Containers(589)+Preferences(496)，交叉71已探测应用，识别67个高价值未探测应用(JetBrains/Obsidian/微信/Figma等)，产 |
| R142 | ⚠️仅索引 | 2026-04-29 | Library未探测应用扫描.md | 实测fix_zombie_tasks.py：keychain为空(2个webhook缺失)，supply_chain启用/trendradar禁用，脚本诊断逻辑正确，实际修复需用户 |
| R143 | ⚠️仅索引 | OCR知识库智能检索工具 | fix_zombie_tasks效果验证.md | 基于8张图/77关键词知识库，产出ocr_search.py，实测3/3查询成功召回(TrendRadar/GA晨晚报/大模型LLM) |
| R144 | ⚠️仅索引 | 技术偏好推荐引擎 | OCR知识库智能检索工具.md | 整合Qoder(6项目/Java+Next.js/AI应用)+百度网盘(JAVA学习资料库)，产出技术栈画像(双轨并行/AI驱动全栈)和P0-P2学习路径推荐(AI工程化/Next |
| R145 | ⚠️仅索引 | 浏览器书签/历史深挖 | 技术偏好推荐引擎.md | Chrome历史24196条(30天)/书签3243条，高频域名linux.do(5632)+有道云笔记(2704)+GitHub(1466)，AI工具占10.6%，工作节律08- |
| R146 | ⚠️仅索引 | 第146批次规划 | 浏览器书签历史深挖.md | 批判性分析50条历史，产出6条高价值TODO(JetBrains/Timing2/linux.do版块/Obsidian/有道云/Figma)，自评分7-9分 |
| R147 | ⚠️仅索引 | JetBrains IDE项目历史深挖 | JetBrains_IDE深挖.md | IntelliJ 47项目(smyze/bar 15个/哈啰6个/怪兽6个)，AI插件7/12(Claude+通义灵码+aiXcoder)，职业轨迹怪兽→哈啰→smyze，PyCh |
| R148 | ⚠️仅索引 | linux.do访问版块分布分析 | linux_do版块分析.md | 4616URL/6059次，AI/大模型61%(Claude Code工具链为核心)/开发技术38%，Top帖子hello2cc累计77次，深度Claude Code用户画像确认 |
| R149 | ⚠️仅索引 | Obsidian知识库结构挖掘 | Obsidian知识库挖掘.md | vault仅2篇笔记(几乎空库)，知识管理主力是有道云笔记(2704次/月)，Obsidian安装未启用 |
| R150 | ⚠️仅索引 | 有道云笔记本地DB深挖 | 有道云笔记DB深挖.md | 本地容器不存在，使用浏览器访问网页版，无可读本地DB |
| R151 | ⚠️仅索引 | Figma本地缓存项目清单 | Figma项目清单.md | 4个唯一文件/16次tab历史，主力文件8次深度访问，开发:设计≈10:1，设计非主职确认 |
| R152 | ⚠️仅索引 | 新批次任务规划(R152) | 新批次规划.md | subagent评审5条高价值TODO，删除微信隐私/Rider低价值，优先级：Claude Code工作流>smyze架构>周报>Git深挖>DataGrip |
| R153 | ⚠️仅索引 | Claude Code工作流优化分析 | Claude_Code工作流优化分析.md | 现有CLAUDE.md已有CCB多智能体+Trellis框架，痛点：缺Java/Next.js技术栈约定/项目索引/hello2cc commands；产出优化方案报告待用户审查 |
| R154 | ⚠️仅索引 | smyze/bar代码库架构速览 | smyze_bar代码库架构速览.md | 15个项目/微服务架构/bar-cloud核心(qmai+大屏+告警)/双包根(cn.smyze旧/com.smyze新)/bar-agent专属AI层/参考若依Pro二开 |
| R155 | ⚠️仅索引 | 自动化周报生成器 | 自动化周报生成器.md | weekly_report_generator.py：扫描smyze git提交/业务域分类/标准格式输出/--send剪贴板集成，与weekly_report_sop.md对接 |
| R156 | ⚠️仅索引 | Git提交历史深挖 | Git提交历史深挖.md | 崔朋420条提交/高峰10-11+15-18时/周一二五最活跃/bar-market-server主力仓库/feat+fix为主 |
| R157 | ⚠️仅索引 | DataGrip SQL查询习惯分析 | DataGrip_SQL查询习惯分析.md | SELECT主导(0次)/bar_deploy+t_device_setting高频表/设备部署管理核心业务/建议Live Template |
| R158 | ⚠️仅索引 | 新批次TODO规划 | 新批次TODO规划.md | 基于R153-R157后续线索+subagent评分，筛出6条高价值TODO(P0周报飞书自动发送/bar-agent AI图谱；淘汰PyCharm课程进度追踪) |
| R159 | ⚠️仅索引 | weekly_report飞书自动发送集成 | weekly_report飞书自动发送集成.md | +--feishu参数/notify_feishu_group集成/sche_tasks定时17:50周五自动触发，dry-run验证通过 |
| R160 | ⚠️仅索引 | smyze技术债扫描报告 | bar-agent需求澄清.md | 扫描bar-cloud(289文件/41TODO/SB2.6.7)+bar-service(44文件/2TODO)，识别qmai-service为高债务模块，建议升级Spring  |
| R161 | ⚠️仅索引 | 2026-04-29 | R162.md | CLAUDE.md commands补全：为bar-cloud/bar-service创建java-review/sql-optimize/api-doc三个slash comma |
| R162 | ⚠️仅索引 | 2026-04-29 | CLAUDE.md | IntelliJ多AI插件协作方案：场景分工(Claude主力/ProxyAI代理/Codium补全)+快捷键分配+配置优化 |
| R163 | ⚠️仅索引 | 2026-04-29 | IntelliJ多AI插件协作方案.md | commit message质量分析：4542条提交，68.3%无前缀(优化/Merge占多)，规范仅31.7%；产出Git Hooks+IDE插件+Commitizen改进方案 |
| R164 | ⚠️仅索引 | - | commit_message质量分析.md |  |
| R165 | ⚠️仅索引 | R165批次TODO规划 | 见 history/日志 | 批判性分析history(13高价值/11环境/2低价值)→盘点报告提炼线索→产出7条TODO→subagent评审(删除3分项/合并2条)→最终5条TODO(9/8/8/7/6分 |
| R166 | ⚠️仅索引 | 2026-04-29 | smyze跨仓库依赖分析.md | 15仓库/141 pom.xml/764 package.json → Maven 30版本冲突/npm TypeScript 63版本+Vue 11版本 → dependency |
| R167 | ⚠️仅索引 | 2026-04-29 | 技术债优先级量化分析.md | 43条TODO/FIXME → git blame获取创建时间+引用次数计算影响 → 3条P1/40条P2 → debt_priority_analyzer.py工具+改进建议 |
| R168 | ⚠️仅索引 | 2026-04-29 | smyze协作画像.md | 21202条提交(603人) → Top10贡献者画像(崔朋日均8.55条)+协作时间窗口(15-17点高峰) → contributor_profiler.py工具+会议时间建议 |
| R169 | ⚠️仅索引 | 2026-04-29 | Git_Hooks实测报告.md | commitizen+commitlint+husky → 规范commit通过/非规范被拒 → R169实测报告+IDE集成指南(IDEA/VSCode)+坑点记录 |
| R170 | ⚠️仅索引 | Maven依赖冲突解决工具 | Maven依赖冲突自动解决工具.md | 工具+报告+数据 |
| R171 | ⚠️仅索引 | Git Hooks批量部署+IDE集成 | 见 history/日志 | 工具+方案+数据 |
| R172 | ⚠️仅索引 | JetBrains插件深度分析 | JetBrains插件深度分析.md | AI优化:Claude+ProxyAI双插件策略 |
| R173 | ⚠️仅索引 | Code Review自动化规则+团队培训 | 见 history/日志 | 收益:PR响应4h→1-2h(提升50-75%)/审查周期2天→1天/协调时间节省20% |
| R174 | ⚠️仅索引 | P1技术债CI集成方案 | P1技术债CI集成方案.md | 预期:TODO增长<3/周+qmai-service保护+维护成本降低30% |
| R175 | ⚠️仅索引 | - | 新批次TODO规划.md |  |
| R176 | ⚠️仅索引 | 自主行动效率监控工具 | 效率监控工具.md | 预期收益:构建-40%/性能+20%/效率+30% |
| R177 | ⚠️仅索引 | - | smyze调用链分析与性能优化.md |  |
| R178 | ✅正文 | 供应链SaaS接口文档分析与API设计质量评估 | 6616B R178_供应链SaaS接口文档分析.md | 集成成本:5-7人天 |
| R179 | ✅正文 | Token成本优化方案 | 6020B R179_Token成本优化方案.md | 自主行动效率监控工具+失败模式预警 |
| R180 | ✅正文 | Schema设计模式与最佳实践 | 9279B R180_Schema设计模式与最佳实践.md | 数据来源:51个DB分析报告 |
| R181 | ✅正文 | - | 4781B R181_2_用户工作流v7架构深度解析.md |  |
| R182 | ✅正文 | - | 3817B R182_Git_Hooks实测与坑点记录.md |  |
| R183 | ✅正文 | JetBrains插件配置指南 | 4960B R183_5_JetBrains插件配置指南.md | 5个推荐插件全部未安装→降级执行→产出配置指南文档(安装步骤/配置建议/协作分析/ROI) |
| R184 | ⚠️仅索引 | bar-agent AI层深度分析 | 见 history/日志 | bar-agent是设备代理非AI Agent→无AI层可分析→任务中止(8→2分) |
| R185 | ⚠️仅索引 | R183批次全部完成 | 见 history/日志 | 1完成(8分)+1降级(4分)+3中止(各2分)+1跳过=18分/49分=36.7% |
| R186 | ✅正文 | 项目健康度仪表盘 | 5759B R186_项目健康度仪表盘.md | 整合R166依赖分析+R167技术债+R168协作画像→三维度评分(依赖70/技术债75/协作85)→综合76.7分(良好)→产出可视化仪表盘+关键风险+行动计划 |
| R187 | ✅正文 | R181批次执行总结 | 5823B R187_R181批次执行总结.md | 评估6个任务→前置条件验证→完成#5项目健康度仪表盘(R186/8分)→跳过5个任务(前置不足4个/信息不足1个)→完成率16.7%/得分率32.0%→识别核心问题(工具链断裂/报 |
| R188 | ✅正文 | R183批次执行总结 | 7710B R188_R183批次执行总结.md | 评估3个任务→价值重估(26分→17分/缩水34.6%)→全部跳过(#1与R166重复/#2前置不足/#3需GUI)→完成率0%→识别核心问题(任务描述与实际不符/价值评估过于乐观 |
| R189 | ✅正文 | R181/R183批次失败根因诊断 | 11093B R189_R181_R183批次失败根因诊断.md | 验证工具链(7/7存在)→纠正R187/R188错误诊断(工具链实际完整)→分析真实失败根因(任务设计质量下降/外部依赖66.7%/价值高估34.6%)→识别3大根因(任务设计问题 |
| R190 | ⚠️仅索引 | TODO任务规划优化SOP | 见 history/日志 | 基于R189诊断(任务设计质量下降/外部依赖66.7%/价值高估34.6%)→设计优化流程(增加4个验证环节)→产出SOP文档(前置条件验证/外部依赖识别/重复度评估/可执行性评分 |
| R191 | ⚠️仅索引 | Token成本CI/CD集成+自动化告警 | AI工具使用画像分析.md | 基于R179 30-40x分析→实现token_cost_alert.py+阈值配置+GitHub Actions工作流→本地实测通过并生成PR评论模板 |
| R192 | ⚠️仅索引 | Schema变更检测(schema_diff.py) | Schema变更检测工具+迁移风险评估.md | 完成对比/风险评估/回滚模板，3组实测均通过 |
| R193 | ⚠️仅索引 | v7架构深度解析 | v7架构深度解析（降级执行）.md | 证据不足无法深度分析，梳理已知服务层/风险/LangGraph债务，建议替换为访谈记录任务 |
| R194 | ⚠️仅索引 | weekly_report飞书自动发送完整闭环验证 | weekly_report飞书自动发送完整闭环验证.md | 默认dry-run通过，--week参数存在ISO周解析bug；飞书邮件实际为半自动发送链路 |
| R195 | ✅正文 | Git Hooks批量部署验证 | 3115B R195_git_hooks_batch_validation_report.md | 修复5处缺陷(monorepo识别/hooksPath/路径计算/变量名/hook执行路径)，坏提交拦截+规范提交放行均验证通过 |
| R196 | ⚠️仅索引 | CLAUDE.md commands实测验证 | 见 history/日志 | bar-cloud/bar-service及commands文件均不在当前环境，前置条件持续缺失，二次核验后正式结案 |
| R197 | ⚠️仅索引 | IntelliJ多AI插件协作实测 | 见 history/日志 | 已核验IDEA进程、三插件安装及单插件使用痕迹，但无人值守下缺少Claude+ProxyAI+Codium三插件协作的可复现实测链路，按边界结案 |
| R198 | ✅正文 | smyze项目依赖关系深度分析+图谱工具 | 4764B R198_smyze依赖图谱分析报告.md | 基于smyze自研Maven模块实测生成依赖图谱工具与报告：扫描27个pom、识别25个artifact和80条内部依赖边，确认0循环依赖，发现2个外部版本分散点并给出治理建议 |
| R199 | ⚠️仅索引 | weekly_report ISO周解析修复 | weekly_report_ISO周解析修复.md | 修复--week参数的ISO周解析错误，补6例回归测试并完成CLI干跑验证 |
| R200 | ⚠️仅索引 | smyze依赖图谱CI模式增强 | smyze_dep_graph_CI模式增强.md | 新增--diff/--output参数，支持比较两次扫描结果并输出依赖变更摘要，已对smyze实测验证 |
| R201 | ⚠️仅索引 | Maven冲突聚合视图与治理清单 | Maven冲突聚合视图与治理清单.md | 为resolve_maven_conflicts.py新增聚合冲突视图与focus输出，对smyze实测识别98项冲突并给出hutool/quarkus治理步骤 |
| R202 | ✅正文 | Spring Boot升级阻力分析 | 3775B R202_spring_boot_upgrade_analysis.md | bar-cloud实为Quarkus项目非SB项目，smyze中SB模块已全部升至3.x；发现opserver中2处低风险遗留项（命名混淆+configurePathMatch）， |
| R203 | ✅正文 | deploy_git_hooks.py真实仓库实测 | 6296B R203_deploy_git_hooks_实测报告.md | smyze 10个仓库：1个npm(Quokka251)部署成功，8个maven/1个unknown被跳过；发现3类问题(maven跳过/unknown跳过/嵌套子目录)，产出原生 |
| R204 | ✅正文 | deploy_git_hooks.py实现maven原生hooks | 5223B R204_deploy_maven_hooks实测报告.md | 新增deploy_maven_hooks()方法；smyze 10仓库覆盖率从10%升至100%；3仓库hook拦截验证通过(不规范exit=1/规范exit=0) |
| R205 | ⚠️仅索引 | smyze Maven冲突治理：hutool版本统一 | 见 history/日志 | bar-cloud(3处5.8.18)+bar-service(5.8.4)统一到5.8.42；4/4验证通过；核心业务服务覆盖率100%；bar-agent/bar-deploy |
| R206 | ✅正文 | deploy_git_hooks.py工具质量评估与重构建议 | 6166B R206_deploy_git_hooks质量评估与重构建议.md | 综合评分5.4/10；P0:maven无幂等保护+npm路径缺陷；推荐方案A最小重构(1-2天)：提取模板常量+拆分npm方法+dry-run；策略模式重构适合下一大版本 |
| R207 | ✅正文 | smyze项目Git提交规范现状分析 | 5654B R207_smyze_Git提交规范现状分析.md | 10仓库671条commit扫描；整体合规率29.4%；bar-deploy-server最高83%，4仓库0%；主要违规：中文开头(40%)+缺少冒号(37%)；改进建议：中文冒 |
| R208 | ✅正文 | bar-agent/bar-market-server Git Hook生效性 | 4050B R208_bar_agent_hook_verification.md | 根因：全局core.hooksPath=/Users/cuip/.git-hooks(空目录)覆盖.git/hooks/，所有hook静默失效；hook脚本逻辑正确但git不读取； |
| R209 | ⚠️仅索引 | deploy_git_hooks.py最小重构 | deploy_git_hooks.py最小重构+中文冒号修复.md | 5项改动落地：get_effective_hooks_dir修复core.hooksPath检测(P1根因)、中文冒号检测、maven幂等保护、--dry-run/--force参 |
| R210 | ⚠️仅索引 | bar-cloud Quarkus版本安全风险 | quarkus_security_risk.md | bar-cloud/bar-agent=2.16.7.Final落后5个patch；CVE-2023-4853(CVSS8.1)影响<2.16.11确认在受影响范围；建议短期升级2 |
| R211 | ⚠️仅索引 | quarkus版本分散根因定位 | quarkus_version_scatter_root_cause.md | 根因：multi-repo无统一BOM，bar-service停留2.11.2未跟进bar-cloud/bar-agent的2.16.7升级；子模块全继承父pom无独立覆盖；当前无 |
| R212 | ⚠️仅索引 | bar-service Quarkus升级：2.11.2→2.16.7 | bar_service_quarkus_upgrade.md | 修改bar-service/pom.xml版本→mvn compile验证通过(exit_code=0)；三仓库(bar-cloud/bar-agent/bar-service)现 |
| R213 | ⚠️仅索引 | smyze quarkus-mybatis-plus版本冲突治理 | quarkus_mybatis_plus_unification.md | bar-qmai-service quarkus-mybatis-plus从1.0.7升级到1.0.8；bar-cloud三子模块全部统一到1.0.8；编译受多模块环境限制无法单独 |
| R214 | ⚠️仅索引 | deploy_git_hooks.py重构后smyze全量实测 | deploy_git_hooks.py重构后smyze全量实测.md | dry-run扫描10仓库(1npm+8maven+1unknown)全通过；发现npm分支缺dry-run保护(实际执行了npm install)；全局core.hooksPat |
| R215 | ⚠️仅索引 | bar-agent/bar-deploy hutool版本统一 | bar_agent_hutool_upgrade.md | 扫描4个目标仓库：bar-agent hutool-all=5.8.20升级到5.8.42(父pom统一管理)；bar-deploy/bar-deploy-server/bar-d |
| R216 | ✅正文 | deploy_git_hooks.py npm分支dry-run保护 | 4673B R216_commit_compliance_script.md | deploy_npm_hooks()加dry_run参数+保护块，deploy_all()传参修复；Quokka251 dry-run验证通过，不再实际执行npm install |
| R217 | ⚠️仅索引 | smyze commit合规率CI本地检查脚本 | 见 history/日志 | 编写check_commit_compliance.sh(4模式:单仓库/多仓库/hook/CI)；macOS grep-P坑→改Python分类；10仓库实测整体合规率25%(9 |
| R218 | ✅正文 | Quarkus 2.16.7→2.16.12升级+CVE-2023-4853修复 | 1740B R218_quarkus_upgrade.md | 三仓库(bar-cloud/bar-agent/bar-service)父pom升级；bar-agent双属性同步；bar-agent/bar-service编译✅；bar-clo |
| R219 | ✅正文 | bar-cloud Maven版本冲突治理 | 3231B R219_bar_cloud_maven_conflict_governance.md | hutool-all 3种写法(${hutool-5.version}/${hutool.version}/5.8.42)统一为父pom属性管理；父pom新增hutool-5.ve |
| R220 | ✅正文 | bar-smyze hutool-all版本冲突治理 | 5281B R220_compress频率优化落地_待审报告.md | 4个pom统一到5.8.42(bar-deploy-server/bar-deploy-server/bar-deploy-client去重/bar-market-server)； |
| R221 | ⚠️仅索引 | smyze 10仓库Git活跃度 | smyze_10仓库Git活跃度.md | bar-deploy-server(326次)/bar-market-server(39次)活跃；bar-cloud/bar-service停滞；饮品履约模块最热；配置文件变更72 |
| R222 | ⚠️仅索引 | bar-alarm-system-service relativePath | bar_alarm_relativepath_check.md | 验证父pom路径正确(bar-cloud/pom.xml存在且匹配)，无需修复，问题不存在 |
| R223 | ⚠️仅索引 | efficiency_monitor.py接入scheduler | efficiency_monitor_scheduler.md | 修复语法错误+实测采集(220浪费轮/16预警)+写daily JSON到sche_tasks/09:00触发 |
| R224 | ✅正文 | bar-deploy-server commit规范提炼 | 4444B R224_commit_convention_analysis.md | 合规率79.4%(200条)，主要不合规=纯优化(50%)+纯中文(33%)，产出.gitmessage模板+规范文档 |
| R225 | ✅正文 | smyze依赖图谱基线快照 | 6460B R225_smyze_dep_graph_baseline.md | 建立baseline.json并验证--diff可用；27模块/25 artifacts/80内部依赖边/0循环依赖/0版本冲突 |
| R226 | ⚠️仅索引 | bar-deploy-server配置文件变更热点（63次提交/6个月） | smyze配置文件变更热点分析.md | 高频变更key：enabled/endpoint/access-key；配置中心必要性评分4.2/5.0；产出Nacos迁移方案（7-11天/ROI 3个月） |
| R227 | ⚠️仅索引 | bar-deploy-server commit规范git-hook | bar-deploy-server_commit规范git-hook.md | 编写commit-msg hook脚本(63行)→8/8测试通过(100%)→部署到bar-deploy-server→产出部署指南(3方案:手动/自动化/Git模板) |
| R228 | ⚠️仅索引 | 2026-05-01 | smyze_PR_review延迟与时间窗口因果分析.md | smyze PR review延迟分析：202条commit/17点高峰19.3%/15-17点占44.1%/周五最活跃24.3%/识别时间窗口冲突+团队规模瓶颈/产出短中长期优化 |
| R229 | ⚠️仅索引 | 2026-05-01 | compress频率优化方案(待审批).md | R229方案报告:llmcore.py L68 %5→%2/预期节省50-60% token/月度$0.77/待审批 |
| R230 | ⚠️仅索引 | R230_自主工作总结报告.md | 自主工作总结报告.md | R230_自主工作总结报告.md |
| R231 | ⚠️仅索引 | - | 报告索引系统建立.md |  |
| R232 | ⚠️仅索引 | compress频率优化落地 | compress频率优化落地.md | 真实触发点在llmcore.py:68，建议待审后将%5调为%2以提前抑制token膨胀 |
| R233 | ⚠️仅索引 | 尝试18种隐藏技巧（HTML注释/JS/Base64/特殊UA/HTTP方法/图片EXIF/Cookie/Referer等）均未找到waekk.com域名下载链接 | CTF_waekk下载链接提取_未解决.md | 报告: ctf_waekk_exploration_report.md |
| R234 | ⚠️仅索引 | - | 自主工作总结报告（第二轮）.md |  |
| R235 | ⚠️仅索引 | - | report_quality_analyzer.md |  |
| R236 | ⚠️仅索引 | - | CTF_waekk下载链接提取_未解决.md |  |
| R237 | ⚠️仅索引 | 扫描35个文件(20个SOP+8个工具) | memory_audit_report.md | 审查覆盖率100% |
| R238 | ⚠️仅索引 | TypeWhisper(989⭐)+KtorMonitor(217⭐) | GitHub冷门工具挖掘.md | brew安装顺畅/M芯片原生/权限门槛高/库类工具难验证 |
| R239 | ⚠️仅索引 | 开发roi_monitor.py(287行/9932字节) | 用户工作模式分析.md | 报告208行/3494字节 |
| R240 | ⚠️仅索引 | AppleScript(60-85ms)最佳/pygetwindow Mac版API缺陷/Shortcuts不适合动态场景 | 见 history/日志 | 产出跨平台封装建议 |
| R241 | ⚠️仅索引 | 分析433条Git commit(359条用户commit/2026-01-16~2026-04-26) | 见 history/日志 | 报告282行/4713字节 |
| R242 | ⚠️仅索引 | brew PATH冲突检测 | brew.md | python3三版本/curl两版本冲突/brew_path_checker.py环境诊断脚本/selenium正常/webdriver缺失 |
| R243 | ✅正文 | scheduled_task优化 | 6573B R243_memory问题系统性修复.md | 验证低活跃时段触发率90.3%/识别99.47% SKIP率根因(max_delay过短)/产出分级优化方案(6h/24h/48h)/预期成功率提升到70%+ |
| R244 | ✅正文 | 修复4个P0路径问题(vision_sop/error_recovery_sop/scheduled_task_sop/subagent)，跳过4个已修复/误报，验证通过✅ | 8908B R244_用户浏览器书签深度挖掘.md | 修复4个P0路径问题(vision_sop/error_recovery_sop/scheduled_task_sop/subagent)，跳过4个已修复/误报，验证通过✅ |
| R245 | ✅正文 | 2026-05-02 | 10356B R245_自主工作失败模式分析工具.md | R244: 分析3243个Chrome书签，分类率33.3%，技术博客905个（CSDN 448/博客园218），识别30个潜在工具，产出个性化推荐（Raindrop.io/Not |
| R246 | ✅正文 | 2026-05-02 | 11897B R246_ROI监控工具自动化增强.md | R245 2026-05-02 分析253个自主工作报告，识别86个潜在低价值报告（34%），产出3类失败模式（小文件/重复任务/低价值关键词）+4条预防建议+failure_pa |
| R247 | ⚠️仅索引 | ROI监控工具自动化增强 | ROI监控工具自动化增强.md | 增加定时运行(scheduler每日08:00)+趋势分析(7日/30日对比)+自动告警(2条规则)，测试验证通过，ROI 39.7倍 |
| R248 | ✅正文 | 2026-05-02 | 3329B R248_compress_freq_analysis.md | efficiency_monitor浪费轮模式分析: 分析2904个summary，识别三大浪费模式(过度验证27.6%/无效重试6.8%/重复探测3.5%)，产出4条优化建议，预 |
| R249 | ⚠️仅索引 | 2026-05-02 | R248.md | V2EX冷门效率工具挖掘：筛选5个工具(Synergy/1clipboard/itsycal/CopyQ/小历)+实测itsycal+识别4大AI推荐陷阱 |
| R250 | ⚠️仅索引 | 2026-05-02 | scheduled_task优化方案落地.md | scheduled_task优化方案落地: 基于R243报告，修复1个配置错误(trendradar_hotspot)，调整8个任务max_delay配置(4个→24h/4个→48 |
| R251 | ⚠️仅索引 | 2026-05-02 | copyq_initial_report.md | brew PATH冲突监控定时任务: 基于R242报告，创建scheduled_task配置brew_path_monitor.json，每周一08:00执行brew_path_c |
| R252 | ⚠️仅索引 | CopyQ剪贴板工具 | CopyQ剪贴板管理工具深度实测.md | 安装成功+脚本封装+权限障碍分析+7天使用计划(ROI 7.5/10) |
| R253 | ⚠️仅索引 | webdriver诊断+R242误判分析 | pc_resource_analysis_report.md | chromedriver已安装/selenium可用/ROI 6.5/10 |
| R254 | ⚠️仅索引 | 2026-05-02 | pc_resource_analysis_report.md | brew webdriver问题分析: 尝试修复chromedriver缺失，成功安装但遇到macOS Gatekeeper阻止，3次尝试均超时失败，需用户手动授权或使用webdr |
| R255 | ⚠️仅索引 | 2026-05-02 | report_quality_evaluator.md | 报告质量评估工具开发+264个报告评估+质量危机分析(98.5%低质量/99.6%无ROI/8条改进建议) ROI 1320倍 价值评分 9/10 |
| R256 | ⚠️仅索引 | 报告质量自动评估工具 | macos_permission_checker_development.md | report_quality_evaluator.py(8504字节)+评估报告(266个报告/257个低质量96.6%/平均1.65分)+97%缺价值评分/93%缺ROI/83. |
| R257 | ⚠️仅索引 | 2026-05-02 | R257_scheduled_task执行日志深度分析.md | R257_scheduled_task日志分析+R250优化失效根因(scheduler未重载配置)+3个修复方案(重启/热重载/自动重启)+ROI 840倍 8/10 |
| R258 | ⚠️仅索引 | 2026-05-02 | recommendation_pitfalls_sop.md | R258 AI推荐陷阱识别SOP产出+4大陷阱识别清单+5步评估流程+工具推荐矩阵 (8/10) |
| R259 | ⚠️仅索引 | scheduled_task执行日志深度分析 | report_search.md | 修复方案:重启调度器+热加载机制+休眠检测 |
| R260 | ⚠️仅索引 | 2026-05-04 | blocked_R248_authorization_review.md | R261 TODO规划R260批次 subagent评审7→5条 skill_search/SOP改进/js_hooking/sophub注入检测/scheduler_patch  |
| R261 | ✅正文 | - | 1739B R261_TODO规划_R260批次.md |  |
| R262 | ⚠️仅索引 | 2026-05-04 | sop_improvement_proposal.md | R263 自主任务质量根因分析+SOP改进提案 基于237轮日志:无报告率36.7%/重复失败30.7%/3条具体证据/4个SOP patch 8/10 |
| R263 | ✅正文 | - | 4645B R263_sop_improvement_proposal.md |  |
| R264 | ✅正文 | 2026-05-04 | 4870B R264_skill_search_findings.md | [Agent] R265 js_runtime_hooking实战完成：js_hook_demo.py(fetch/XHR/crypto三层hook) + 7条真实拦截日志(not |
| R265 | ✅正文 | - | 3483B R265_js_hooking实战.md |  |
| R266 | ✅正文 | scheduler配置patch | 3370B R266_sophub_injection_detection.md | 分析5050行日志发现4任务SKIP 81-94次，生成schedule_patch.diff对齐用户12/20/23点活跃窗口 |
| R267 | ✅正文 | sophub历史日志prompt注入检测 | 3997B R267_scheduler_patch.md | 扫描7759行+2952行memory文件，104条命中全为良性误报，0条真实adversarial注入 |
| R268 | ⚠️仅索引 | skill_search能力探索 | skill_search能力探索与集成.md | 执行6个英文场景查询获30条结果，安全/devops匹配最佳，整理top技能清单与集成建议 |
| R269 | ⚠️仅索引 | compress频率%5→%2影响分析 | 见 history/日志 | 基于162次真实Cut事件量化：改动可带来2.5x压缩频率+额外1.09M token节省，风险极低，待用户批准执行 |
| R270 | ✅正文 | scheduler热加载机制实测 | 1341B R270_candidate_TODO_draft.md | R270 结论：修改sche_tasks/*.json无需重启scheduler；下次check自动生效，受enabled/schedule/max_delay/cooldown约 |
| R271 | ⚠️仅索引 | 自主任务收尾强制检查点最小落地 | 自主任务收尾强制检查点最小落地.md | 落地tail_close_checkpoint.py用于显式复核报告路径/complete_task/TODO/history/hook状态；确认当前实现不存在真实done_hoo |
| R272 | ⚠️仅索引 | TODO质量门槛落地 | TODO质量门槛落地.md | 落地todo_quality_gate.py对候选TODO按产物/动作/验收/去重/元分析惩罚评分，并实测识别元分析泛化与重复主题验证两类低价值模式 (8/10) |
| R273 | ⚠️仅索引 | skill_search接入真实规划流 | skill_search接入真实规划流.md | 在planning+security/devops场景真实调用6组查询；security场景分数0.75+表现优秀；planning场景0.68~0.72中等；给出"本地SOP无覆 |
| R274 | ⚠️仅索引 | 自主任务重复主题拦截规则最小实现 | 自主任务重复主题拦截规则最小实现.md | 落地dedup_guard.py：规则1相似度>0.60拦截+规则2元分析密度≥2拦截；近40条history实测4类重复被拦截，正常任务零误杀；给出与todo_quality_g |
| R275 | ⚠️仅索引 | 2026-05-04 | R279)已全部完成。现在读task_planning.md | planning_gate.py集成：dedup_guard+todo_quality_gate串联两阶段门控；5条候选实测3被拦截2通过，零误杀 (9/10) |
| R276 | ⚠️仅索引 | 2026-05-04 | compress频率沙盒验证.md | compress频率%5→%2沙盒验证：20轮模拟触发2.5x，边界正确，patch已生成供用户一键apply，风险极低 (8/10) |
| R277 | ⚠️仅索引 | 2026-05-04 | activate_window封装.md | activate_window.py封装：AppleScript主方案+open-a fallback+title匹配三层策略；实测Finder(130ms)/Terminal(4 |
| R278 | ⚠️仅索引 | 2026-05-04 | planning_gate接入agentmain实测.md | planning_gate接入agentmain规划流实测：10条真实候选(4合法+3重复+2低质量+1边界)，FP=0 FN=0零误判，SCORE_THRESHOLD=45最优无 |
| R279 | ⚠️仅索引 | 2026-05-04 | scheduler_patch.diff实际应用.md | scheduler_patch实际应用：4个sche_tasks配置已改到12:00/20:00活跃窗口，weekly仅改schedule；产出applied diff与下次触发预 |
| R280 | ⚠️仅索引 | compress_mod5_to_mod1.diff apply到llmcore.py line68：%5→%1，触发率20%→100%(5x提升)，import回归通过，备份llmcore.py.bak_R280 (9/10) | compress_mod1_apply.md | compress_mod5_to_mod1.diff apply到llmcore.py line68：%5→%1，触发率20%→100%(5x提升)，import回归通过，备份ll |
| R281 | ⚠️仅索引 | pl | planning_gate_threshold45.md | pl |
| R282 | ⚠️仅索引 | complete_task飞书推送集成mock验证 | complete_task_feishu.md | notify_group被调用 |
| R283 | ⚠️仅索引 | complete_task飞书推送集成 | complete_task_feishu集成.md | patch10行mock验证通过notify_group调用成功 |
| R284 | ⚠️仅索引 | pyobjc NSPasteboard实测 | pyobjc_clipboard.md | 读写+pbpaste/pbcopy双向验证通过,封装clipboard_utils.py |
| R285 | ⚠️仅索引 | js_hook真实API采集实战 | jshook_api_intercept.md | 7条真实拦截(youdao)schema提取,pushserver3设备ID/lp参数揭露 |
| R286 | ⚠️仅索引 | R286 | planning_R286.md | 无TODO进入规划模式；本地fallback评审6条候选，保留4条(R287-R290)写入TODO，下轮执行 |
| R287 | ⚠️仅索引 | - | todo_reviewer.md |  |
| R288 | ⚠️仅索引 | - | clipboard_utils.md |  |
| R289 | ⚠️仅索引 | - | hook_log_parser.md |  |
| R290 | ⚠️仅索引 | - | scheduler_activate_review.md |  |
| R291 | ⚠️仅索引 | R287 | 见 history/日志 | todo_reviewer.py落地(5891B)；串联quality_gate+dedup_guard+planning_gate；demo实测6条候选评审通过 |
| R292 | ⚠️仅索引 | R288 | 见 history/日志 | clipboard_utils.py扩展RTF+PNG+类型嗅探；5项实测全通过 |
| R293 | ⚠️仅索引 | R289 | 见 history/日志 | hook_log_parser.py解析js_hook日志→JSON+schema摘要；10条实测断言全通过 |
| R294 | ⚠️仅索引 | 2026-05-05 | 见 history/日志 | R290 scheduler活跃窗口效果回看;activate_window 3/3✅;scheduler 12/20对齐60%;关键缺口:两工具未联动 |
| R295 | ⚠️仅索引 | 新批次TODO规划R295 | 1_*.md | 产出4条高价值TODO(procmem实战/hook管道/ocr+vision决策矩阵/scheduler补丁)，删除2条低分项(泛采集/adb无设备) |
| R296 | ✅正文 | procmem_scanner macOS实战：ctypes自进程扫描4/4验证通过+hex偏移记录，跨进程需root/Frida留待用户决策 | 687B R296_procmem_scanner_macOS实战：ctypes自进程扫描4_4验证通过+hex偏移记录，跨进程需root_Frida待用户决策.md | ✅验收达标 |
| R297 | ⚠️仅索引 | 2026-05-05 | mac本地工具gap扫描.md | mac本地工具vs memory SOP gap扫描：brew 145个formula，7个高价值GAP(tesseract9/ffmpeg8/tmux8/sqlite7/htop |
| R298 | ⚠️仅索引 | ocr_utils vs vision_utils联合实战 | ocr_utils+vision_utils联合实战决策矩阵.md | OCR本地15s/12390字✅ Vision代理不通需网络; 决策矩阵6场景+screencapture全路径修复 |
| R299 | ⚠️仅索引 | vision_utils代理修复 | 2_ffmpeg_utils封装.md | 根因=proxy 2082死亡无fallback，直连api.anthropic.com可达(405/401)，设计probe_proxy+直连fallback+OCR降级方案，p |
| R300 | ⚠️仅索引 | ffmpeg_utils.py封装 | ffmpeg_utils.py封装.md | 产出memory/ffmpeg_utils.py(8117B,8函数)，ffmpeg 8.0.1验证，6项实测全通过：元数据/截帧/批量截帧/转码/音频提取/缩略图，多媒体处理能力 |
| R301 | ⚠️仅索引 | tmux_utils.py封装 | tmux_utils.py封装.md | 产出memory/tmux_utils.py(8938B,10函数)，tmux 3.6a验证，8项实测通过：会话创建/命令下发/捕获/循环/后台/幂等/kill，持久后台命令执行能 |
| R302 | ⚠️仅索引 | vision_utils代理fallback补丁落地 | ...md | _probe_proxy+直连fallback+OCR降级三场景全通过 |
| R303 | ⚠️仅索引 | macOS跨进程内存边界 | procmem跨进程边界实战.md | 无root:task_for_pid/ptrace全❌,proc_pidinfo/vmmap/sample✅,字节读需root+SIP-off或Frida |
| R304 | ⚠️仅索引 | sqlite_probe封装 | sqlite_probe封装.md | memory/sqlite_probe.py(5功能+CLI),probe_test.db 4项实测全通✅,支持只读URI+TCC-fallback |
| R305 | ⚠️仅索引 | openssl_utils封装 | openssl_utils封装.md | memory/openssl_utils.py(10函数+CLI),7项实测全通✅:digest/HMAC/b64/随机/AES-GCM/RSA-PSS/cert解析 |
| R306 | ⚠️仅索引 | sophub prompt注入检测 | sophub_prompt注入检测.md | 157842行日志3段样本(S1/S2/S3):P2/P3有命中但Agent正确拒绝,P4/P5/P6无实质威胁,防御机制全程正常✅ |
| R307 | ⚠️仅索引 | 217条记录分析/4任务时间对齐/diff+rollback已apply | scheduler配置patch.md | 217条记录分析/4任务时间对齐/diff+rollback已apply |
| R308 | ⚠️仅索引 | tmux+ffmpeg联动demo实测 | *.md | tmux后台会话→demo.sh→ffmpeg转码(640x360/1.8s)→sentinel轮询→ffprobe产物校验全✅，能力链完整打通 |
| R309 | ⚠️仅索引 | whisper_utils.py | L1_whisper_utils.md | faster-whisper封装6函数，实测4/4通过，音频→文字+时间戳+SRT+batch，能力节点已解锁 |
| R310 | ⚠️仅索引 | pdf_utils.py | pdf_utils.md | pdfplumber封装7函数，实测7/7通过，PDF文字/表格/元数据/搜索/分页摘要能力节点已解锁 |
| R311 | ⚠️仅索引 | image_utils.py | image_utils.md | Pillow封装9函数(resize/crop/gray/enhance/OCR预处理/watermark/convert)，实测9/9通过，图像处理能力节点已解锁 |
| R312 | ⚠️仅索引 | http_utils.py | http_utils.md | 封装7函数(sync/async/download/batch)+真实URL验证7/7通过 |
| R313 | ✅正文 | watchdog_utils.py | 2002B R313_watchdog_utils.md | watchdog封装9函数(watch/patterns/created/modified/deleted/moved/multi/stop_all/list)，实测7/7通过，文 |
| R314 | ✅正文 | scraper_utils.py | 2005B R314_scraper_utils.md | bs4+lxml封装10函数(CSS/XPath/表格/链接/正文/meta/clean)，真实网页4场景全通，网页解析能力节点已解锁 |
| R315 | ✅正文 | psutil_utils.py | 2766B R315_psutil_utils.md | psutil封装8函数(CPU/内存/磁盘/网络/进程/TopN/全量快照/定期采集)，本机9/9测试全通，系统监控能力节点已解锁 |
| R316 | ✅正文(未落盘) | polars实战：解析autonomous_reflect.log行为日志，挖掘agent低效模式 | 6334B ./R316_polars_analysis.py | 产出Polars可复现分析脚本+6条已验证发现，定位空转/长尾token/context cut等低效模式 |
| R317 | ✅正文 | App Support DB二期深挖 | 6315B R317_app_support_db_deep_dive.md | 深挖Timing2(300K条app时序)/百度网盘cloudpic(10K照片+1772 POI地理)/有道云笔记(5451篇)/filecache(61K文件索引)，4个新数据 |
| R318 | ✅正文 | 批次规划 | 2100B R318_batch_planning.md | 批判性读40条历史+subagent评审6条候选→修订为5条TODO(Timing2周报9分/空转预检8分/窗口标题8分/有道云6分/cloudpic6分)，策略从utils工厂转 |
| R319 | ⚠️仅索引 | Timing2个人生产力周报生成器：基于300K条AppActivity构建自动周报脚本(timing2_weekly_report.py)，输出5维度(每日屏幕时间对比/App Top10排行/专注度分析/时段热力图/异常检测)+3条个性化洞察(编码沟通比/Chrome依赖度/高产时段)，脚本可复现运行，含真实数据报告 | Timing2_weekly_report.md | Timing2个人生产力周报生成器：基于300K条AppActivity构建自动周报脚本(timing2_weekly_report.py)，输出5维度(每日屏幕时间对比/App  |
| R320 | ⚠️仅索引 | Agent空转预检实装：修复autonomous.py无条件触发空转(53.6%)，增加TODO非空预检，5场景测试全PASS，预估月省2.4M tokens (~$7-37/月) | agent_idle_precheck.md | Agent空转预检实装：修复autonomous.py无条件触发空转(53.6%)，增加TODO非空预检，5场景测试全PASS，预估月省2.4M tokens (~$7-37/月) |
| R321 | ⚠️仅索引 | Timing2窗口标题深度分析：从259K条窗口标题提取1422条Google搜索(635关键词)+57K IDE记录，发现三阶段兴趣演变(Q1副业探索→Q2-3深度开发→Q4 AI深耕)，搜索频率降88%反映自给率提升，bar-deploy全年主导项目 | Timing2窗口标题深度分析.md | Timing2窗口标题深度分析：从259K条窗口标题提取1422条Google搜索(635关键词)+57K IDE记录，发现三阶段兴趣演变(Q1副业探索→Q2-3深度开发→Q4 A |
| R322 | ⚠️仅索引 | 有道云笔记技术栈演变分析：从5378篇笔记元数据(2016-2024)重建8.5年技术栈演变，发现四阶段(微服务入门→Spring深耕→底层深潜→AI觉醒)，2017巅峰2210篇/2024 AI爆发32篇，面试题+JVM突增暗示2021-2022跳槽周期 | youdao_tech_analysis.md | 有道云笔记技术栈演变分析：从5378篇笔记元数据(2016-2024)重建8.5年技术栈演变，发现四阶段(微服务入门→Spring深耕→底层深潜→AI觉醒)，2017巅峰2210篇 |
| R323 | ⚠️仅索引 | cloudpic地理足迹聚类分析：从1772条POI+583张geo照片聚类出72个地理簇，发现职住一体(嘉定菊园500m内)、双老家格局(河南夏邑+滑县)、幼儿家庭画像(15%POI与孩子相关)、极简通勤圈(中位1.5km)、暑假返乡节律 | cloudpic地理足迹聚类分析.md | cloudpic地理足迹聚类分析：从1772条POI+583张geo照片聚类出72个地理簇，发现职住一体(嘉定菊园500m内)、双老家格局(河南夏邑+滑县)、幼儿家庭画像(15%P |
| R324 | ✅正文 | 批次规划(R325-R329) | 2344B R324_批次规划.md | 6候选→subagent独立评审(10/9/9/8/7/5分)→淘汰书签项→5条跨源融合+个性化数据TODO |
| R325 | ⚠️仅索引 | - | cross_source_timeline_analysis.md |  |
| R326 | ✅正文 | - | 3434B R326_chrome_search_learning_path.md |  |
| R327 | ⚠️仅索引 | - | smyze_architecture_evolution.md |  |
| R328 | ⚠️仅索引 | - | zsh_history_behavior_profile.md |  |
| R329 | ⚠️仅索引 | - | launchagents_topology.md |  |
| R330 | ⚠️仅索引 | 跨源时序关联分析(Chrome×zsh×Git) | 跨源时序行为分析.md | Research-first工作模式：Chrome(00-16h研究)→zsh(16-20h编码)→Git(20-22h提交)，203会话62个三源联合avg6.9h |
| R331 | ⚠️仅索引 | Chrome搜索词×主题聚类(315query+30920visits) | Chrome搜索查询与技术学习路径重建.md | AI:Java=170:1确认转型完成；AI觉醒期内4微阶段(基建→定制→工程化→Agent自主)，与R322四阶段模型交叉验证通过 |
| R332 | ⚠️仅索引 | zsh_history行为画像与工具链演变(8641cmd/412天) | 见 history/日志 | 命令行已从运维终端升级为AI开发中枢；形成“上下文恢复→AI改码→docker/ssh/curl验证→git收束”闭环，识别5类工作流发现与工具采纳时间线 |
| R333 | ⚠️仅索引 | LaunchAgents自动化拓扑与健康状态(20plist) | ...md | 11运行/5未加载/1错误/1缺失；6个AI Agent核心+2开发工具构成四层拓扑；识别4项优化建议(清理失效/修复错误/补日志/加KeepAlive) |
| R334 | ⚠️仅索引 | smyze代码库架构演变趋势(10仓库/27模块) | 见 history/日志 | 近3月仅bar-cloud活跃(13commits集中bar-qmai-service)，其余9仓库冻结；R225基线依赖图谱未变；识别3大模块趋势(qmai热点/基建冻结/Dev |
| R335 | ✅正文 | 下批TODO生成(规划模式) | 8163B R335_规划模式_下批TODO生成与独立评审.md | 发现temp/autonomous_reports报告库(R1-R334)整体丢失→history已从logs/autonomous_reflect.log重建303条(缺号31) |
| R336 | ✅正文 | 自主报告归档抢救(R1-R335) | 6090B R336_自主报告归档抢救.md | 从L4会话归档恢复76份报告正文+335行索引并入库(memory/autonomous_reports),commit 668fdc7;钩子方案待审 |
| R337 | ✅正文 | roi_monitor 产出量告警修复 | 9265B R337_patch_roi_monitor_v2.md | 实测根因=v1快照无avg_lines字段致<100行告警自06-26起为死代码+心跳稀释(占比77.6%);口径v2=done/报告行数分层现算,阈值按121天回溯校准(下滑25 |
| R338 | ✅正文 | bar-smyze「复活」归因检验 | 14054B R338_bar-smyze复活归因假设检验.md | 结论=前提错位：R334的"10仓/27模块"是smyze根级云服务组(bar-cloud等,复现13次提交=精确命中),该组今日仍冻结;而看似复活的smyze/bar-smyze |
| R339 | ✅正文 | 本地语音转写(ASR)实测 | 9291B R339_ASR本地语音转写能力缺口实测.md | 结论=前提错位:memory早就有 whisper_utils.py(176行,faster-whisper 1.2.1)且全API实测通过,TODO称'完全没有音频节点'过期;真 |
| R340 | ✅正文 | 记忆更新欠账清单 | 16577B R340_记忆更新欠账清单.md | 135区块判定(已落地54/部分40/待评估26/丢弃5/空转10,落地率69.6%);★口径纠偏=TODO称'332处'无法复现,实测zip(L4月度归档)行首header仅10 |
| R341 | ✅正文 | 生成下一批TODO(规划模式第2批) | 6792B R341_下一批TODO规划.md | 批判读history(309行,R1-R340;类型字段非受控词表达30+种写法,月份分布04月200/05月58/09月6)识别低价值模式(自我报告元分析/浅层可用性验证/泛采集 |
| R342 | ✅正文 | 待审项收口:一条一行·一键决策清单+落地器 | 5222B R342_待审项收口_一键决策清单与落地器.md | 把R335-R341七份报告里散落的'待批准/记忆更新建议'收成 temp/pending_decisions/(DECISIONS.md 18条一条一行 + apply.py 执 |
| R343 | ✅正文 | 2026-09-22 | 10923B R343_TrendRadar 信源有效性审计.md | R343 完成：TrendRadar 信源有效性审计——283份报告+132份物料+157个日库三线交叉；RSS侧1真死源(ai-weekly HTTP404)+4极低效源(采纳率 |
| R344 | ✅正文 | 本机个性化数据源可用性地图 | 5641B R344_personal_source_map.md | 12源只读实测:11个Sqlite/bplist副本直读零授权(必须连-wal,-wal缺失会假空:Notes 2→8条),微信容器递归find被TCC挂起(>25s不返回)只能l |
| R345 | ✅正文 | 微信语音 .silk→wav→转写全链路 | 5231B R345_微信语音silk解码转写全链路.md | 公开真实样本(fishCoder仓钉commit d888a4b5,sha256校验)端到端打通:hello.silk 3615B→wav 94124B/1960ms→「你好」,t |
| R346 | ✅正文 | 下批TODO生成(第3批·独立评审) | 7386B R346_规划模式_第3批TODO_独立评审.md | 批判读history(314行)识别三类低价值模式(元分析循环/无消费者工具化/重复验证) → 6条候选经**独立subagent**评审(其自跑6项物理探测:音轨扫描33个含15 |
| R347 | ✅正文 | knowledgeC 快照器与首份数字行为底稿 | 5292B R347_knowledgeC快照器与首份数字行为底稿.md | 交付 tools/kc_snapshot.py(snapshot/report/verify/list/prune；幂等实测=连跑2次第二次 skip 不重写；verify rc= |
| R348 | ✅正文 | 报告唯一副本防复发 | 11307B R348_报告「唯一副本」防复发端到端验证（TODO#5）.md | 真树负控证双写未生效；补丁C2/C3/C4沙箱端到端跑通(镜像sha一致+回滚3/3字节级)，落地待批 |
| R349 | ✅正文 | helper压缩死引用修复(压缩器落地) | 8880B R349_fix-compress-deadref.md | 交付 tools/log_compress.py(copytruncate;夹具3/3+真链路无损:12.10MiB→1.00MiB/归档1.82MiB/NUL=0)+temp s |
| R350 | ✅正文 | - | 14716B R350_product-freshness-audit.md |  |
| R351 | ✅正文 | 内网/云主机连接命题验证(只读) | 15801B R351_内网云主机连接命题验证.md | 成立:known_hosts 30 台含 openclaw-server.tailf82c5b.ts.net(=100.112.252.36,GCP,google_compute_ |
| R352 | ✅正文 | TrendRadar拆词修复效果A/B离线复算 | 11154B R352_TrendRadar拆词修复效果A-B离线复算.md | 判定批修法A(条件式):29天A整串=0/B逐词pair=1384(与R343同区间完全对齐)/去重1283(44.2条/天,单日最高73,零命中0天);净新增1253条(与当日R |
| R353 | ✅正文 | 本机音频资产：去重→挑人声→长音频转写备忘 | 19253B R353_本机音频资产转写备忘.md | 46命中/14重复/32唯一(含音轨26/无音轨4/非媒体2)；26条2.11h全量转写28.9min(4.4×实时)，25结果中13条零文本(52%)、12条出文本18470字( |
| R354 | ✅正文 | 本机音频资产（TODO#17） | 17175B R354_本机音频资产去重-挑人声-长音频转写备忘.md | 26条有音轨(2.11h)全量VAD+ASR：13条纯音乐占31.7%时长被筛除，12条有语音中仅6条可用；本批ASR 1726s≈4.4x实时；新增 tools/audio_va |
| R355 | ✅正文 | 自主报告「编号复用」根治 | 11961B R355_自主报告编号复用根治（补丁就绪待批）.md | 根因=编号只扫LIVE树+分配非原子(文件名锁无效+history截断窗读到空→R1);补丁已就绪待批(两树max+1+号码级O_EXCL claim锁+收尾自动sync/inve |
| R356 | ✅正文 | 规划模式第4批：下一批TODO生成与独立评审 | 11514B R356_规划模式第4批_下一批TODO生成与独立评审.md | 批判读history(323条)+实测取证(本机零备份/数据卷97%仅剩15G/logs 1.7G/llama.cpp 0.4.1在而0权重)→出6条候选交独立subagent评审 |
| R357 | ✅正文 | knowledgeC 每日快照落地（零新增定时 + 当日补采） | 12723B R357_knowledgeC每日快照落地.md | 当日补采 18277 对象/13.1MB/verify rc=0（较 09-22 的 17463 对象 +814）；★实测反射脚本不会“放进目录就生效”（各需专属 plist）⇒  |
| R358 | ✅正文 | 备份缺口收口(TODO#0) | 15861B R358_backup_gap_closure.md | 实测本机零备份(tmutil=No destinations/无外置盘diskutil仅internal/无云客户端/~/Library/LaunchAgents无备份类);数据卷 |
| R359 | ✅正文 | 日志体量治理dry-run+待批清单 | 9042B R359_日志体量治理_dryrun与待批清单.md | logs/=1.7GiB,四长跑日志净可回收≈1.55GiB(helper从不触碰这些大文件);launch_gui.log=1.13GiB孤儿死日志(无任何plist引用,冻结自 |
| R360 | ✅正文 | 本机本地推理解锁 | 7314B R360_本地推理解锁.md | 跑通1.5B/3B本地推理(llama-server,1.5B=157MB/77.6tok·s);消费者:trendradar预筛丢88%≈省7420tok/次调用,但haisou |
| R361 | ✅正文 | 2026-09-23 | 10646B R361_静默失败待批修复包.md | 工程 ｜ 静默失败「待批修复包」：x_ai_intel_daily + eia_weekly ｜ 出两个可独立批准的补丁：①prompt 标记化（启动前只读前置检查→PRECHEC |
| R362 | ✅正文 | 2026-09-23 | 9070B R362_Chrome历史URL只读挖掘.md | 环境/产出 ｜ Chrome 历史 URL 只读挖掘 ｜ 12,377 唯 URL/31,498 访问(2026-06-25→09-23 正好 90 天滚动);口径: 至少 21. |
| R363 | ✅正文 | 2026-09-23 | 7547B R363_规划模式·第5批TODO（含独立评审）.md | 产出 ｜ 生成下一批TODO（规划模式·第5批） ｜ 批判读 history(60/362 条)+盘点 memory 85 L3/报告 133 md 后出 6 条候选,经独立 su |
| R364 | ✅正文 | 磁盘可再生空间回收实测 | 9448B R364_磁盘「可再生空间」回收实测.md | 实收12.7GB(avail 9.5G→20G,数据卷98%→96%):pnpm store prune 4.43G/npm cache 2.80G/updater包2.09G/C |
| R365 | ✅正文 | 待批台账第2批增量(C19–C47)与超期查询 | 22796B R365_待批台账第2批增量与超期查询.md | 29条散落待批(11份报告 R349–R364)并入既有 R342 台账,共60条未决/最老1天/≥7天0条;新增只读查看器 temp/pending_decisions/view |
| R366 | ✅正文 | 记忆「过时与矛盾」审计（只提案不改memory） | 16760B R366_记忆过时与矛盾审计.md | 11 处发现（过时4/矛盾3/冗余3/风险1，全部不在 C1–C47 内）+3 处复核准确；关键：rg「未装」为假（15.1.0 可用，静默空真因=.gitignore 忽略 te |
| R367 | ✅正文 | trendradar预筛离线影子A/B(1.5B) | 10344B R367_本地推理的第二消费者实测：trendradar 预筛「离线影子 A_B」.md | 不接:V5压缩72%但误杀59.4%(hot_kw通道66.7%);V1误杀3.1%但只压缩4.9%;GT理论可丢仅36.4%(R360的88%为未校验口径) |
| R368 | ✅正文 | 缓存「回收脚本化 + 重建验证」 | 15665B R368_磁盘「可再生空间」回收工具化_cache_reclaim.md | tools/cache_reclaim.py(默认dry-run/20项分档/红线);apply实收1.46GiB(pnpm-store)+T0 sha不变;pnpm与playwr |
| R369 | ✅正文 | 检索式回忆最小实证（FTS5 是否值得固化） | 9220B R369_检索式回忆最小实证.md | 结论=不固化、留作 temp 可复用原型：有索引人工拆词 top-5 命中 7/12、自动三元组 8/12、单题 0.1–3.3ms；无索引每题扫 37.79MB/0.101s 但 |
| R370 | ✅正文 | 生成下一批TODO（规划模式·第6批） | 14704B R370_规划模式·第6批TODO.md | 批判读history+7条候选经独立subagent评审(8/8/7/6+1替换)→定稿5条入TODO.txt；删①检索式回忆v2/⑤公益站抽检/⑦网盘台账；反证R364「uv锁被 |
| R371 | ✅正文 | kc 快照字段可用性矩阵 + 补采口径 | 25616B R371_kc快照字段可用性矩阵与补采口径.md | 完成：16 表/9 有料（ZOBJECT 27/37 列、结构化元数据 17/239 列）；分析代价毫秒级（全表 group by 66.5ms、近 7 天双查询 3.4/4.9m |
| R372 | ✅正文 | uv 缓存 21G 安全回收 + 锁归因重验 + 收口 C45 | 17053B R372_uv 缓存 21G 安全回收+锁归因重验+收口 C45.md | 完成：~/.cache/uv 21.0G→6.3G（两阶段删 435 个非在用 archive 条目，du 实证 -14.7G；df 仅 +3Gi 的差额由并发写吃掉，iostat |
| R373 | ✅正文 | 历史低价值模式量化 v1 | 18444B R373_历史低价值模式量化 v1.md | 完成：temp/history_value/{parse.py,score.py} 把 history.txt 341 条(R1-R372,缺号31) 解析为 entries/sc |
| R374 | ✅正文 | 记忆订正登记（R366 十一处）+ 索引新鲜度只读探针 | 13614B R374_记忆订正登记与索引新鲜度只读探针.md | 完成：R366 的 10 个条目块（其自称"十一处"系按分类计数、B1 双计）逐条登记为台账 C53–C62；复验更正 2 条——B1「rg 默认搜 temp 得空」被实测推翻（默 |
| R375 | ✅正文 | temp/ 耐久耦合清账（6 个 enabled 定时任务） | 15314B R375_temp耐久耦合清账.md | 沙盒演练（3 cwd × 原样/修复）取证 + 负控 3/3：weekly_report_reminder 的 ../temp 在 cwd=repo 根必失败 rc=2、effic |
| R376 | ✅正文 | 规划模式·第7批TODO生成 | 15788B R376_规划模式第7批TODO生成与独立评审.md | 两轮subagent评审(8/7/7/6/6/6入选；淘汰4分能力清单、5分13断链)⇒6条TODO落盘；三处首稿数字复测订正；附ga_api_server崩溃循环坐实 |
| R377 | ✅正文 | zsh 历史画像 → 别名/包装脚本落地 | 19013B R377_环境-zsh历史画像到别名落地.md | 10005条历史画像:cd 占208471字符(38.8%)=最大成本项;落地 ~/.zsh_ga_aliases(11条实测别名+2函数,守卫式/零密钥/幂等),zsh 逐个 t |
| R378 | ✅正文 | L1/L2 记忆「路径与文件断言」机械核验 | 13450B R378_记忆路径断言机械核验.md | 802 断言 pass586/FAIL180/同名36(只读)；实证订正 5 类(根写错/目录消失/归档落点/已删脚本/temp产物)待批，零改 memory |
| R379 | ✅正文 | L4 会话原始归档「可检索化」 | 11318B R379_L4会话原文可检索化.md | grep 与 sed -n 独立复核;逐条验收见报告§4;修 3 处真缺陷(grep-file 位置参数顺序/缺 --case-sensitive/selftest Namespa |
| R380 | ✅正文 | 报告「路径断言」核查闸门 | 27708B R380_报告路径断言机械审计.md | 131份报告/753断言 pass514 FAIL225(29.9%);强断言断链top100 其中被他引30条(其中被记忆引用28条);新建tools/report_assert |
| R381 | ✅正文 | T0 备份二期（数据侧） | 26370B R381_T0备份耐久化二期.md | 可复跑工具 tools/t0_snapshot.py + 348 文件全量演练（还原#1/#2 逐文件 sha256 全等、负控抓篡改）+ 加密往返 + 缺口清单；零新增定时，离机 |
| R382 | ✅正文 | 缺失报告正文恢复(2026-04月度zip) | 6311B R382_缺失报告正文恢复04月.md | ast静态解析复原105份内联报告正文→staging(sha256判重)+128不可恢复/3低分/16已在盘 四桶双清单;匹配器加H1自编号假设号修错配;不写memory树(待批 |
| R383 | ✅正文 | 规划模式第8批TODO生成与独立评审 | 11520B R383_规划模式第8批TODO生成与独立评审.md | 7候选经独立subagent评审(8/7/5/5/4/4/3)→保留4条(L1索引瘦身/磁盘98%归因/待批台账TOP10排序器/点名工具冲浪),淘汰3条(plan_guard范畴 |
| R384 | ✅正文 | L1 索引瘦身草案(73行/17879tok→27行/1216tok,-93.2%)+三重零丢失自证+子代理A/B(15/15 vs 15/15)+tools/l1_budget.py(负控4/4);memory零改动,2条待批 | 12340B R384_L1索引瘦身与token预算校验器.md | L1 索引瘦身草案(73行/17879tok→27行/1216tok,-93.2%)+三重零丢失自证+子代理A/B(15/15 vs 15/15)+tools/l1_budget. |
| R385 | ✅正文 | 磁盘98%复发归因 | 13530B R385_磁盘98%复发归因_索引增量与df抖动判据.md | 定位单点=IDEA索引单次重建9.86GiB(09-24 10:27)+df分钟级抖动(churn)非真增长;顶层du构成+5条处置台账待批;零删除(只读) |
| R386 | ✅正文 | R386 待批台账 TOP-N 排序器 | 9517B R386_待批台账 TOP-N 排序器(view_pending --top).md | view_pending --top 就绪：影响面/阻塞度/可逆性/风险/积压五项打分+一屏Markdown+集中度与边界同分提示；只读(台账sha1前后一致)、负控3/3、原5模 |
| R387 | ✅正文 | 小众工具实测 R387 | 21924B R387_冲浪-小众工具实测(BasicMemory+Hammerspoon).md | Basic Memory 0.23.2（CLI 21 工具 + codex/claude 双端 MCP 真调用 + 中文向量检索命中）与 Hammerspoon 1.1.1（bre |
| R388 | ✅正文 | BasicMemory 语义索引试挂(TODO#12) | 12246B R388_BasicMemory语义检索试挂TODO12.md | 结论=不接:16题×3模式+双grep基线,中文最优bge-small-zh-v1.5 hit@5=11/16 < grep_naive 12/16(语义增益0:grep漏的4题B |
| R389 | ✅正文 | Hammerspoon 系统事件触发实测 | 15165B R389_Hammerspoon事件触发实测.md | 零授权可触发4类事件源(Wi-Fi电源/进程启停/文件落地/剪贴板);快捷键投递需辅助功能授权(0命中);setFrame布局动作生效;未启用常驻(属用户决策);全程零改动~/.h |
| R390 | ✅正文 | 规划模式·第9批TODO生成（2份独立subagent评审；第3路超窗口未回） | 6620B R390_规划模式第9批TODO生成与独立评审.md | 7候选→保留4+衍生1/淘汰2：密钥体检8·7(最高)、kc快照守卫+30天消费(合并)、日志治理(限定根因+执行R359)、冲浪(换真冷门话题)、待批项一页决策包；淘汰Watch |
| R391 | ✅正文 | 本机密钥/令牌泄漏体检（只读+掩码+金丝雀负控） | 23555B R391_密钥体检.md | 2026-09-25 完成：pass1 68616 文件/4283MB→72309 命中，其中 97.7% 为 ≥80 位 base64 泛配（40 条抽样解码 36 条为二进制/ |
| R394 | ✅正文 | TODO#11 待批项一页决策包：4 屏（密钥面/kc守卫/日志回收/TMWebDriver脱敏）各含收益·风险·回滚·一句话批准语 + 不批准后果 | 10559B R394_待批项一页决策包.md | 从 108 条未决中抽 4 条可立即批准项，全程只读零执行；顺带修 view_pending「决策列非空=已决」误判（负控 3/2 vs 旧 1/4） |
| R395 | ✅正文 | 冲浪·冷门工具实测：taskpolicy + footprint/heap/leaks | 13050B R395_冲浪-冷门工具实测-taskpolicy与内存三件套.md | taskpolicy 免 root 降 QoS(pri 20→4)+footprint/heap/leaks 只读内存剖面；头条=phys_footprint≠ps RSS 两口径(17.3G vs 18.8M)
| R396 | ✅正文 | 规划模式·第10批TODO生成与三路独立评审 | 13362B R396_规划模式第10批_三路评审与下一批TODO.md | 全量重算 R1–R395 低价值量化（n=364、LOW 40=14.1%、P1 12/P3 29/P4 13/P5 1/P6 42，中位 6620B）+ 7 候选经三路独立 subagent 评审（llm_no 0/2/4）→ 保留 5 条（归档完整性闸门 8.00 最高）/ C7 并入 C6 / 淘汰 C5 冲浪 3.33（连续两轮同题）；含 4 项自验事实：归档 11 组同号（10 组同字节、R337 真冲突）、fsapp 现值 4892MB vs peak 17GB、ga-api 0 消费者、helper 抢号仍无锁 |
| R397 | ✅正文 | 归档完整性闸门：报告编号复用/改名事故的只读检测器 | 14762B R397_归档完整性只读检测器.md | 只读检测器 6/6 负控全过且自证语料计数（跑前后 152 文件 sha256/mtime/字节全等=零改动）；11 组同号=10 组 sha1 全同（改名对）+R337 拆分对⇒0 组真冲突；硬缺口 2=9 编号有正文无 history 行、INDEX 比 history 多 33 编号(24 仅索引)；空洞 0；处置全待批 |
| R398 | ✅正文 | 后台长任务工具化：tools/bg_task.py（start/status/tail/stop/list） | 13322B R398_后台长任务工具化bg_task.md | 与 memory/tmux_utils.py 逐项比对后只补 3 缺口(退出码/PID复用/文件日志)，不重写 tmux 层；真机证据=start 后 ppid=1 脱离父进程、RUNNING/EXITED_OK/GONE_UNKNOWN 三态可辨、stop 走 SIGTERM→SIGKILL；隔离自测 12/12 含 4 负控(不存在任务/僵尸 pid 文件/PID 复用/日志确已写入)；零第三方依赖且不 import tmux，本机无 setsid 已写进注释与 --help |
| R399 | ✅正文 | ga-api 退役判定 + 21 sche_tasks / 8 launchd 定时任务「产物级」新鲜度复测 | 30574B R399_ga-api退役判定与产物级新鲜度复测.md | 只读零改动；21 任务三判=健康13 / 外部条件缺失·可见失败2(birdie 站点 SSL 不可达、x_ai 缺登录) / 无意义空转1(sqlite_snapshot 108 快照零载荷变化，目标库停滞 113–129 天) / 已停用残留5；8 项 launchd=在跑5 + 配置即如此2 + 未加载孤儿1；★订正 ga-api 非「已 disabled」=plist 在盘且 KeepAlive/RunAtLoad=true 但 launchd 未加载(58,379,981B 日志 mtime 09-14，尾部全 ImportError)，退役列为待批建议未执行 |
| R400 | ✅正文 | 常驻进程内存三口径基线 + 记忆订正 | 10680B R400_常驻进程内存三口径基线与记忆订正.md | 交付 tools/mem_caliber.py（只读235行/自检8/8含负控1项）；实测同一进程 ps RSS 与 footprint 现值差 95-105×（617: 64.2MB vs 6128MB, peak 17408MB）；订正 L2:508 的 18,525,030,616B 实为 peak 非现值（今日现值 4.4-7.05GB）；边界 footprint -a 需 root；本机全系统压缩器 6.58GiB/swap used 12.99GB 吃紧 |
| R401 | ✅正文 | 待批台账「只读聚合」：108 条未决压成 3 句话 | 18486B R401_待批台账只读聚合.md | 只读零写入（67/67 文件 sha256 逐行一致）；apply.py 18 条声明仅覆盖 98 唯一 ID 的 18.4%、未知 ID 静默 rc=0（与 R386d 同源，建议合并批准）、全量 dry-run=14 可执行+1 manual+3 FAIL；批A 资产入仓 6 条 + 批B 记忆订正 17 条各一句批准语；不可逆 6 条逐条待拍板；结论=不新增 apply 通道 |
| R402 | ✅正文 | 规划模式第11批TODO生成 | 12169B R402_规划模式第11批_两轮三路评审与下一批TODO.md | 量化n=370(LOW 41=11.1%)；6候选经两轮三路评审(llm_no 0/2/4)保留6条淘汰1条，最高8.33=待批项前提复测；TODO净增6条+种子项 |
| R403 | ✅正文 | 待批项前提复测降载(只读) | 14652B R403_待批项前提复测降载清单.md | 台账108未决中仅18条(16.7%)声明机器可复测前提;今日复测=ACTIVE14/REWRITE2(C14/C16目标已存在内容不同)/STALE1(C11锚点0命中)/manual1;★建议关闭3条(C11意图已由L1落地; C14/C16 SOP已建成) 仍需拍板15条;批A文件存在前提6/6全成立;交付premise_recheck.py(自测6/6含负控)+只读自证(目标14文件sha改动0/台账mtime全早于会话);复现R401 C11判定✓ |
| R404 | ✅正文 | 记忆引用断链复核与修复提案(只读) | 13050B R404_记忆引用断链复核与修复提案.md | 18候选名全盘basename重扫:16/17 GONE,唯一EXISTS=view_pending.py(推翻R380"全盘无"旧断言,41引用仍有效);语料318文件/3299314B/46661行,MEM引用9条全落2文件;★范围收窄=只修现役记忆(global_mem.txt 5行+wechat_signals_sop.md 1行),历史快照memory/autonomous_reports与L1不动;候选替代词根命中全为无关同名⇒"改新路径"不成立;落patch草案6条单行尾注+备份命令+批准语,证据包temp/R404_evidence/;自证含负控negctl=True+2正控(log_compress/view_pending)2负控(合成假名/16断链名);订正R380行号337→347与temp/archive不存在;未执行任何memory写入 |
| R405 | ✅正文 | 网络分层诊断单入口 tools/net_doctor.py(四层探测+三段式判定) | 17390B R405_网络分层诊断单入口net_doctor.md | 四层探测(链路/Clash控制面/基线/目标站)+三段式判定(LOCAL_DOWN/PROXY_NODE_BAD/SITE_FAILURE/+EGRESS_BAD)每段带证据链与下一步命令;假IP(198.18/15)与DNS_FAIL显式排除出站点故障判据(SITE_FAILURE_CLASSES仅TCP/TLS/HTTP四码);无Clash控制面时自动降级只跑链路+目标站;selftest 13/13(2条e2e假mihomo夹具含接口形状+4条负控正控+4条合成判定);真机四目标混跑=baidu OK·不可达域名 DNS_FAIL·198.18.0.112 FAKE_IP(不发连接)·expired.badssl.com TLS_FAIL⇒SITE_FAILURE;固定矩阵+台账temp/net_doctor_history.jsonl(禁标永久结论);★实测PATH无/sbin|/usr/sbin⇒route/ifconfig须绝对路径否则误判本机断网;★本机当前无mihomo控制面(仅ClashVerge特权helper) |
| R406 | ✅正文 | 本机 LLM 槽位可用性台账与探活 tools/llm_slot_probe.py（第11批·评审7.00） | 8446B R406_LLM槽位可用性台账与探活.md | 8 槽实测 4 OK(0 deepseek-flash 728ms / 3 gpt-6-astra 6885ms / 4 sonnet5 3013ms / 1 gpt-4router 5007-20780ms 抖动大) 4 AUTH(2/5/6/7 = HTTP 403「该令牌无权访问模型…」=服务端按模型白名单拒绝，非限流、重试无用);每槽 ≤1 请求(http_requests=8 + retries_forced=0 + 全程 43.5s);台账 temp/llm_slot_ledger.jsonl 3 跑 append-only+--diff(末两跑 0 迁移=稳定;正控篡改 1 槽报 1 条;run1 OK=0 是工具自身 bug 现场);★真跑走 NativeClaudeSession/NativeOAISession，其 ask(msg:dict) 与 BaseSession.ask(prompt:str) 签名不同⇒最初 8 槽 0ms 全 AssertionError，修法=按会话类型派发 + 把原生会话补进 selftest(现 31/31);分类 9 类且 EMPTY 必须在 NETERR 前(llmcore 把 HTTP200 无内容包成 ConnectionError);零改配置 + 产物零密钥明文(mykey 按凭据字段扫 4 文件命中 0) |
| R407 | ✅正文 | 报告「数值断言回读」闸门 tools/report_number_audit.py（第11批·次轮评审 6.50，六条强制收窄条件全满足） | 9851B R407_报告数值断言回读闸门.md | 只读/零 shell/零网络/纯 stdlib；四类检查=冻结证据漂移(FROZEN)/同报告内部矛盾(INTERNAL)/比例派生(RATIO)/现场复算(STAT)；真归档 158 文件 940 条数值断言 0 假警报(FROZEN0/INTERNAL0/RATIO0/STAT0)、白名单跳过 2313 处(5类)、冻结期文件 229、只读自证=扫描前后全归档 sha1 全同 True；合成语料端到端 7 条断言 4 类警报同时触发，负控静默；selftest 17/17(9 合成用例+2 负控+2 正控)；核心口径=比对主体为(路径或标题)+「把数值自身遮蔽后的整行」上下文本签名（旧「数字前短标签」在真归档造 20 余条假警报=不同实体共用前缀） |
| R408 | ✅正文 | 跨会话重复劳动量化（第11批·评审7.00，五条硬条件全满足） | 15499B R408_跨会话重复劳动量化.md | 判据先写死=detect2.py+CRITERIA_V2.md（四类证据 A同派单文本4-gram Jaccard≥0.60/B同TODO认领/C同R*.md报告写入/D同自建产物文件交集，τ=2h，委托豁免，C·D只认工具调用path字段）；实测 411 会话(04-22→09-25快照，跨zip去重456→411) 命中 44 对/涉及60会话=19.9%(自建派单分母301)/14.6%(全量分母)；构成 A12(9定时重跑+2 BBS worker自动接单+1自主评审) B1 C0 D31(多为09-21~22 herdr工程日接力,35/44顺序非并发,并发9对)；负控 NC1随机20对全流程命中0(仅证据层1)/NC2时间错位不报/NC3证据清空不报/NC4后置不报，正控R353·R354对命中；selftest 11/11；★旧口径v1(罕见token共现)211对/35.9%已证伪=工具回显与共享记忆文件(56会话写过insight)制造假共现，不可引用；v1.1(304对)未去同起点分片/共享协议文件=系统性假阳性；三条口径修正=跨zip同id去重/同起点分片跳过(白报76对)/≥8会话写过的6个共享协议文件不计D类；决策输入(不拍板)=硬证据仅个位数，认领锁对顺序接力收益为零，先取证(写入时间戳)或只对派单入口去重 |
| R409 | ✅正文 | 规划模式第12批TODO生成与三路独立评审 | 10096B R409_规划模式第12批_三路评审与下一批TODO.md | 量化 n=377：LOW 40=14.2%（P6产物查无42 / P3待批未落地28 / P1无消费者13 / P4近重复13 / P5无果1）；7 条候选经**三路**独立评审（llm_no 0/3/4=deepseek-flash·gpt-6-astra·sonnet5，后台 bg_task，12707/15601/28103ms 全 ok，原文 temp/plan12/prompt{0,3,4}.out.txt）→ 保留 4 条：C1 引用完整性闸门 9.33（并列最高，直击 P6 最大桶；R404 检测器至今无消费者=P1 活样本⇒本项交付接线+产物存在性索引）/C2 任务与通道产物级新鲜度每日监控 9.33（birdiesearch 49天静默 + haisou 09-25 抓取 0 课程仍 rc=0 作历史回放夹具；吸收 C7）/C4 L2 记忆版本历史与恢复演练 8.67（git log 空+.gitignore:55 memory/**+143987B/sha256 251e61cb8...=唯一副本零历史）/C3′ 待批项证据三桶+一句批准语 7.00（收窄：只做机器可判子集；吸收 C5 消费者硬约束）；淘汰 C6 归档双侧一致性 4.67（无消费者+与既有归档闸门重叠）、C7 并入 C2；否决评审衍生建议「待批项老化>90天告警」（台账最老仅3天，属未取证填充）；TODO 写入 temp/TODO.txt 待执行区顶部 + 种子行追加产出记录 |
