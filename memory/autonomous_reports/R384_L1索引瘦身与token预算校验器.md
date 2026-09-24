# R384 · L1 索引瘦身草案 + token 预算校验器（本轮零改动 memory/）

- 日期: 2026-09-24 ｜ 任务来源: `temp/TODO.txt`「R383候选1 产出 | 【第8批·评审 8/10】L1 索引瘦身 + token 预算校验器」
- 交付性质: **草案 + 工具 + 自证/实测**；`memory/` 零改动，2 条待批见 `temp/pending_decisions/DECISIONS_R384_l1_slim.md`
- 工作区: `temp/l1_slim/`（gitignored）｜耐久件: `tools/l1_budget.py`（已入仓）

## 0. 结论摘要（对应验收 5 条）

| 验收项 | 结果 |
|---|---|
| ①token 口径 + 实测值（原始输出） | ✅ o200k_base/cl100k_base 双口径；live L1 = 73 行 **17879** tok（cl100k 21450） |
| ②≤30 行压缩草案 + 低频降级指针 + 逐条 diff 自证 | ✅ v3 = **27 行 / 1216 tok**；三重 diff（名称级/规则级/明细层）**PASS 零判据丢失** |
| ③`tools/l1_budget.py` 校验器 + 自证读到文件 + 负控 | ✅ 负控自测 **4/4**（超限 FAIL / 合规 PASS / 仅 token 超限 FAIL / 缺文件 FAIL） |
| ④导航 A/B 由 subagent 实测（禁自证） | ✅ 30 份子代理答案：v1 **15/15** vs v3 **15/15**，delta **0.0 pp** |
| ⑤改 memory/ 走待批 | ✅ 本轮 **未改** live L1，草案与两条决策留待批 |

**一句话**：把每轮注入的 17879 tok 索引压到 1216 tok（-93.2%），行数 73→27，且在"名称零丢失 + 规则关键词零丢失 + 明细层逐字留档"三重机械自证下，子代理同题检索命中率**没有下降**；唯一未达标项是 L0 的"<1k tokens（期望）"，且已实测证明**保留全部名称时该期望不可达**（下界 1266 tok）。

## 1. 口径与实测（原始输出）

方法：`python3 tools/l1_budget.py --file <path>`；token 用 tiktoken，**同时打印 o200k_base / cl100k_base 两口径**（不同后端分词器不同，单值会误导）；条目同时自证 abspath/字节/sha1/首行/非空行数，用于证明"校验器真读到了目标文件"。

```
文件: memory/global_mem_insight.txt
abspath: /Users/cuip/projects/GenericAgent/memory/global_mem_insight.txt
自证: bytes=53956 chars=32559 lines=73 nonempty=71 sha1=0e18aa5337c4
首行: # [Global Memory Insight]
token(tiktoken): o200k_base=17879 cl100k_base=21450
预算: lines<=30 tokens<=1000
结论: FAIL
[FAIL] 行数 73 > 硬约束 30
[FAIL] token 17879 > 期望上限 1000（口径 o200k_base）

文件: temp/l1_slim/global_mem_insight.v3.txt
abspath: /Users/cuip/projects/GenericAgent/temp/l1_slim/global_mem_insight.v3.txt
自证: bytes=3824 chars=3026 lines=27 nonempty=27 sha1=7231bf0a9e9e
token(tiktoken): o200k_base=1216 cl100k_base=1368
预算: lines<=30 tokens<=1000
结论: FAIL
[FAIL] token 1216 > 期望上限 1000（口径 o200k_base）
```

L0 口径原文（`memory/memory_management_sop.md:33`）：`体积限制：≤ 30 行（硬约束），< 1k tokens（期望）。严禁填写细节（除非极高频任务）`
⇒ v3 **满足硬约束**（27 ≤ 30），**未达期望**（1216 > 1000，缺口 216 tok，超 21.6%）。

### 1.1 "<1k 且保留全部名称"不可达的实测下界

```
unique names = 277
名称清单(仅空格分隔) tokens = 1266  chars= 4888
带最小前缀(每名一行) tokens = 2092
v3 tokens = 1216
```
277 个名称哪怕剥掉**所有**解释、只剩以空格分隔的名字本身，也已是 **1266 tok > 1000**。⇒ L0 的"<1k"与"索引里保留全部能力名"两个目标在 tiktoken o200k 口径下**互斥**；要进 1k 必须再下沉约 30 个低频名称（代价见待批 D2）。

## 2. 压缩草案 v3（27 行全文，逐字）

```
# [Global Memory Insight] ≤30行/<1k tok | 触发词→定位 | 细则→read memory/L2_l1_details.md
Web: tmwebdriver_sop | jshook_runtime_sop | js_runtime_hooking_playbook_sop | codex_web_auditor_prompt_sop | hcaptcha_sop
键鼠/视觉/OCR: ljqCtrl_sop | vision_sop | vision_utils.py | ocr_utils.py | ocr_vision.swift | ui_detect.py
密钥/配置/CLI/风险: keychain.py(+sop) | codex CLI(隔离必401) | codex_jailbreak_risk_sop | llm_prompt_bypass_risk_sop | ctf_core_contract_risk_sop
MCP/数据/检索: ga_mcp_client.py | ga_mcp_server.py | procmem_scanner.py(+sop) | tools/personal_source_map.py | tools/l4_search.py
ASR/语音: asr_cli.py(+asr_sop) | tools/silk_to_text.py
定时/反射/自主: scheduled_task_sop | autonomous_operation_sop(种子项否则死锁) | scheduled_task_freshness_audit_sop | agentmain --reflect | roi_monitor_sop | tools/log_compress.py
报告/归档/台账: tools/autonomous_reports_archive.py | tools/report_assert_audit.py | tools/memory_assert_audit.py | pending_decisions(view_pending.py) | tools/report_body_recover.py
磁盘/手机/桌面: tools/cache_reclaim.py | tools/t0_snapshot.py | ~/.cache/uv(prune阻塞) | adb_ui.py | android_uu_cellular_troubleshooting_sop | ga_desktop_env_override_sop
推送/IM/内容: notify_channels_sop | wechat_signals_sop | wechat_moments_publish_sop | output_archive_sop | feishu_card_troubleshooting_sop | dingtalk_message_digest_sop
报告模板: weekly_report_sop | weekly_inspection_sop | performance_self_review_sop | recommendation_pitfalls_sop
编排/工程: herdr_orch_sop | autopilot_unattended_drill_sop | orchestration_discipline_sop | agent_fleet_sop | release_marshal_unattended_sop | trellis_integration_sop | acceptance_taskset_sop | L2票据地图(herdr-agents:control-plane/l2_v0)
学习/冲浪/外部: deepresearch_sop | github_project_learning_sop | baidu_pan_transfer_sop | fofa_cli_proxy_hunter_sop | linuxdo_deep_read_sop | linuxdo_trust_level_sop | launchpad_folder_sop | ctf_skills_integration_sop | sophub_sop(+security) | navicat_trial_reset_sop | cc_disguise_knowledge
监控(低频): birdiesearch_monitor_sop | haisou_monitor_sop | brew_path_monitor_sop | project_update_monitor_sop | hellogithub_monthly_sop
仓库: herdr-agents=~/develop/herdr-agents | GA=~/projects/GenericAgent | L2节名=场景关键词
L0: memory_management_sop | L2: global_mem.txt | L3: ../memory/*.md,*.py | L4: L4_raw_sessions/
[低频] 未列场景→ ls ../memory/ 或 read memory/L2_l1_details.md(逐字原文,含机制/步骤/实测案例)
[RULES]
1-2 搜索先行:优先cwd,网页只用google,rg未装(静默空)→grep ｜ 交叉验证:禁信摘要,数值进详情
3-5 改前必读,file_read代cat,memory模块import ｜ 闭环:物理模拟后确认,3次失败求助,Git闭环 ｜ 进程:精确PID,禁os.kill判活
6-8 GUI态:win32gui枚举标题 ｜ web JS:原生setter+事件链,点击前检disabled ｜ SOP/utils必用,复杂规划先读plan_sop
9-10 git日期(--since/--until)须带T00:00:00+08:00 ｜ "完成"=完整闭环,发现差距一气呵成(对比→修正→固化SOP)
11-12 同类重复先grep/正则批量定位,禁逐条试 ｜ URL/ID禁套模板,真值实测,出错批量回刷
13-14 禁空报完成:GUI/发布须OCR等价证据,未打通须明示(禁中间态冒充) ｜ 网络:Clash TUN下"直连"不可信,用Clash API逐节点
15-16 后台任务:setsid nohup…</dev/null&disown立即返回+轮询,禁sleep阻塞 ｜ "测试全绿"先验真执行+接口形状(桩替身须验flag形状)
17-20 DB快照连-wal/-shm+mode=ro,Containers(TCC挂起)禁递归find ｜ 引文即契约(「」/逐字/回源/负控/行号)→details ｜ 部分提交:-U0数hunk+核验新增行+blob构造+index.lock僵锁 ｜ 自动改写先dry-run并核清单
21 实测数值(mtime/行数/字节/条数)当场回读原命令,禁凭印象
```

配套明细层草案 `temp/l1_slim/L2_l1_details_proposed.md`（78 行 / 54381B / sha1 c87769ed800a）＝ **live L1 全文逐字留档** + 压缩规则/边界说明；被下沉的 200 个名称都在这里保留原上下文，v3 首行以 `细则→read memory/L2_l1_details.md` 给出入口。

## 3. 零判据丢失的三重 diff 自证（`temp/l1_slim/check_diff.py` 原始输出）

```
# 1) 名称级
live 去重名称 = 277 | v3 保留 = 77 | 下沉到 details = 200 | v3 新出现 = 11
新增标识符存在性核验: ga_mcp_server.py ✅  memory/global_mem.txt ✅  tools/cache_reclaim.py ✅
  tools/memory_assert_audit.py ✅  tools/report_assert_audit.py ✅  tools/t0_snapshot.py ✅
  L2_l1_details.md ⏳（本轮待批新建的目标名，非死链）
# 2) 规则级（[RULES] 21 条逐条关键词）
   小结: 21 条 RULE 中关键词全命中的 = 21/21
# 3) 明细层（details 是否逐字含 v1 全文）
   details 逐字包含 v1 全文: True | details bytes= 54381
结论: PASS —— 零判据丢失（名称级+规则级+逐字明细层三重）
```

判据含义：**名称级**保证任何能力名要么仍在 L1（77 个高频）、要么在明细层留档（200 个）；**规则级**保证 RULES 的行为约束关键词一个不丢；**明细层**保证可回滚（details 逐字含 v1 全文 ⇒ 任何时刻可无损还原 live L1）。

## 4. 导航效用 A/B（子代理实测，禁自证）

- 机制：`temp/l1_slim/queue_agent.py` 起**独立子代理**，闭卷（只给一份索引全文 + 同题），要求自报是否使用工具；单例 ~130s。
- 题集：10 题（6 高频 + 4 低频），按"该能力是否在 L1 里出现/位置"分层（题目定义在 `temp/l1_slim/ab_build.py`）。
- 评分：`temp/l1_slim/ab_grade.py`（按版本×高频/低频统计命中率；答案文件不存在＝未跑，不计入分母）。

| 轮次 | v1（现状 73 行） | v3（27 行） |
|---|---|---|
| 第一轮（10 题） | 10/10（高频 6/6，低频 4/4） | 10/10（高频 6/6，低频 4/4） |
| 复核轮（q4,q7,q8,q9,q10） | 5/5（高频 1/1，低频 4/4） | 5/5（高频 1/1，低频 4/4） |
| **合计 30 份答案** | **15/15** | **15/15** |

delta = **0.0 pp**（同题同模型闭卷检索口径）。全部 30 份答案中唯一的工具使用记录：v3 的 q9 一次 `file_read`，且自述"仅读取本任务提示文件本身，未查阅任何记忆文件"。

**限制（不得外推）**：①子代理与索引同为同一家模型，测的是"索引里能不能定位到条目"，不等于人类读者的导航体验；②样本 10 题，未覆盖全部 277 个名称，属**抽样**非穷尽；③"命中"判据是答案中是否出现正确条目/文件（脚本判定 + 人工核对 conf），不是任务是否真被更好地执行。

## 5. 新工具 `tools/l1_budget.py`（耐久件）

- 用法：`python3 tools/l1_budget.py [--file X] [--max-lines 30] [--max-tokens 1000] [--json] [--selftest]`
- 退出码：0=PASS｜1=FAIL(超预算)｜2=用法/读取错误
- 自证：打印 abspath/stat/sha1/首个非空行/非空行数（防"路径写错→读到空→报 PASS"的假绿）
- tiktoken 缺失时回退字符估算并显式标注 `estimation=True`
- 负控自测原始输出：

```
=== l1_budget --selftest（负控自测）===
[OK] 超限样例(行数+, 内容+): ok=False lines=35 tok=6755 reasons=2
[OK] 合规样例: ok=True lines=3 tok=29 reasons=0
[OK] 仅token超限(行数合规): ok=False lines=10 tok=10030 reasons=1
[OK] 缺失文件: ok=False lines=None tok=None reasons=1
selftest: PASS 4/4
```

## 6. 待批（2 条，未执行）

1. **D1**：用 v3 替换 live L1 + 新建 `memory/L2_l1_details.md`。
2. **D2**：接受 v3 的 1216 tok（建议），或授权再下沉约 30 个低频名以换 <1k。

逐条依据/批准命令/风险表见 `temp/pending_decisions/DECISIONS_R384_l1_slim.md`。

## 7. 可复跑命令（全部只读）

```
python3 tools/l1_budget.py                                  # live L1 现状
python3 tools/l1_budget.py --file temp/l1_slim/global_mem_insight.v3.txt
python3 tools/l1_budget.py --selftest                       # 负控 4/4
python3 temp/l1_slim/check_diff.py                          # 三重零丢失审计
python3 temp/l1_slim/ab_grade.py v1 v3                      # A/B 第一轮
python3 temp/l1_slim/ab_grade.py v1r2 v3r2                  # A/B 复核轮
```

## 8. 边界与残差

- `memory/` 本轮零改动；live L1 内容与 R383 报告所述一致（73 行）。
- 草案与 A/B 材料住在 `temp/`（gitignored，非跟踪）⇒ 已知缺口 C66；**若 D1 获批，届时须把 v3 与 details 一并落到 memory/ 才算耐久**。
- v3 的 token 未达 L0 期望值（1216 vs 1000），按"硬约束满足、期望未达 + 给出下界证明与替代路径"上报，不擅自取舍。
- 未新增/修改任何定时任务；未触碰用户 shell 配置与密钥。
