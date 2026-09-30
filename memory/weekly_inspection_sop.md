# 周巡检 SOP (weekly-inspection)

## 触发词
"周巡检"/"周检"/"ops-check"/"巡检报告"/"检查opserver日志"/"查一下最近一周有没有问题"/"生成巡检报告"/"weekly inspection"/"日志有没有报错"

## 技术配置

| 资源 | 参数 |
|------|------|
| SLS project | smyze-cloud |
| SLS logStore | bar-smyze-opserver |
| SLS regionId | cn-shanghai |
| 生产 DB | mcp__dbhub__execute_sql_bar_op_prod |
| Redis（测试参考） | mcp__redis-test__ |
| 代码库 | /Users/cuip/develop/WorkSpaces/idea/smyze/bar-smyze/bar_smyze_opserver |
| 报告输出目录 | {代码库}/docs/analysis/ops-weekly-inspection/ |
| 时间范围 | now-7d ~ now |

## MCP 工具映射（GA调用方式）

```python
from ga_mcp_client import MCPClient
# SLS查询
async with MCPClient("sls") as c:
    result = await c.call_tool("sls_execute_sql", {"project": "smyze-cloud", "logstore": "bar-smyze-opserver", "query": "...", "regionId": "cn-shanghai"})
# DB验证
async with MCPClient("dbhub") as c:
    result = await c.call_tool("execute_sql_bar_op_prod", {"sql": "..."})
```

## 四阶段执行流程

### Phase 1：基础指标扫描（并行3个SLS查询）

所有查询时间范围：now-7d ~ now。

```sql
-- Q1: ERROR 按 location 分组（找热点捕获点）
level: ERROR
| SELECT location, count(*) as cnt
  GROUP BY location ORDER BY cnt DESC LIMIT 20

-- Q2: WARN 按 location 分组（找持续噪音）
level: WARN
| SELECT location, count(*) as cnt
  GROUP BY location ORDER BY cnt DESC LIMIT 20

-- Q3: ERROR 按天+小时分布（找峰值时段，关联部署事件）
level: ERROR
| SELECT date_format(__time__ - __time__ % 3600, '%Y-%m-%d %H:00') as hour, count(*) as cnt
  GROUP BY hour ORDER BY hour
```

### Phase 2：深度分析

**核心原则**：凡 location 为公共切面（Aspect/Handler/Filter/Interceptor），必须获取 content 原文，不允许仅凭 location 定级。公共切面记录的是异常的"被捕获处"而非"抛出处"，直接定级会遗漏真实 Bug。

#### 2A — 捕获层下钻（对 Aspect/Handler/Filter 类 location 必须执行）

**TenantJobAspect**（所有定时Job的公共捕获层）：
```sql
level: ERROR and location: "TenantJobAspect"
```
获取至少 10 条原始日志，从 content 字段提取：
- `Caused by:` 行（真实异常类型，如 ClassCastException / CommunicationsException）
- `at com.smyze...Job` 行（真实 Job 类名）
- 注意：同一时段内可能有多种不同 Job 失败，10条样本比3条更能覆盖少数异常类型

**GlobalExceptionHandler**（所有业务异常的公共捕获层）：
```sql
level: ERROR and location: "GlobalExceptionHandler"
```
获取 10~20 条 content 原文，**按实际 SQL 错误类型分组**：
- `Unknown column` → DDL 未同步（代码先于迁移上线）
- `Data too long` / `MysqlDataTruncation` → 字段长度不足
- `doesn't exist` → 表结构缺失
- `Duplicate entry` → 唯一键冲突
- **不同 SQL 错误类型必须拆分为独立问题，不能合并**

**其他公共层**（ApiEncryptFilter / serviceExceptionHandler）：同样获取 content 样本，提取真实接口路径和异常来源。

#### 2B — 持续告警识别

对 Q2 中 cnt > 500 的 location，额外查询 now-1h 判断"今日是否仍在发生"：
```sql
level: WARN and location: "{location}"
```
今日 now-1h 内仍有日志 → 标记为"持续中"，优先级提升。

按小时趋势寻找突变点：
```sql
level: WARN and location: "{location}"
| SELECT date_format(__time__ - __time__ % 3600, '%Y-%m-%d %H:00') as hour, count(*) as cnt
  GROUP BY hour ORDER BY hour LIMIT 24
```
突变点（某小时骤增）往往关联部署事件或基础设施变更。

#### 2C — 支付/订单/Job 专项

```sql
(location: "OrderV2CreateServiceImpl" or location: "PayNotifyServiceImpl"
 or location: "PayOrderServiceImpl" or location: "CouponActivity") and level: ERROR
| SELECT location, count(*) as cnt GROUP BY location ORDER BY cnt DESC
```

### Phase 3：DB / 代码 / Redis 交叉验证

根据 Phase 2 发现按需执行（不要全部执行，按实际发现匹配）。**报告中每个经过验证的结论旁必须标注 `DB 已核实` 或 `代码已核实`。**

| 发现的问题类型 | 验证动作 |
|--------------|---------|
| GlobalExceptionHandler content 含 `Unknown column` | `SHOW CREATE TABLE {表名}` 确认列是否存在 |
| GlobalExceptionHandler content 含 `Data too long` | `SHOW COLUMNS FROM {表名} LIKE '{列名}'` 确认列类型和长度 |
| 支付通知 WARN | `SELECT COUNT(*), SUM(status=20) FROM pay_notify_task` 确认永久失败记录 |
| 设备"门店不存在" WARN | 提取 bindDeviceCode，`SELECT id, code, enable_status, deleted FROM store_store WHERE code IN (...)` |
| 枚举未识别 WARN（PayMethodEnum 等） | Grep 枚举类文件，确认缺失项 |
| 定时 Job ClassCastException | Grep Job类，查 Redis Lua 返回类型 |
| 库存结算未知类型 | Grep `mapOutboundType`，查 supply_chain_stock_io_fact |
| TenantJobAspect MySQL 连接失败 | 查 `infra_job_log` 确认 Job 在 DB 层是否显示成功 |

**SLS 字段说明**：顶层字段为 `level`、`location`、`message`、`content`。**不存在独立的 `exception` 或 `stack_trace` 顶层字段**。根因从 content 提取：
- 搜索关键字：`content: "Caused by"` 或 `content: "Unknown column"`
- 聚合提取：`regexp_extract(content, 'Caused by: ([^\n]+)', 1)`

### Phase 4：报告生成

#### 前置：读取上一期报告（生成 1.3 生命周期的必需输入）
写报告前，先 `ls docs/analysis/ops-weekly-inspection/` 找到上一期报告，读取其「问题优先级清单」，逐条带入本期做周环比比对。上期每个问题都要在本期 1.3 有结论，不允许静默消失（见报告质量红线第 9 条）。

#### 文件规范
- **路径**：`{代码库}/docs/analysis/ops-weekly-inspection/weekly-inspection-postmortem-{YYYY-MM-DD}.md`
  - 日期格式：**带连字符**，如 `2026-06-23`，不是 `20260623`
  - 文件名不含额外后缀（不加 `-eval-test`、`-draft` 等）

---

#### 【生成层硬约束·强制·从根因防漂】写报告必须先用骨架生成器，禁止手写结构
2026-06-26 第三次纠偏定论：报告反复漂移（ERROR/WARN 合并章、编号 N1、mermaid x-axis 与 bar 不等长）的根因，是**报告每次由 LLM 自由复述模板重写，结构靠自觉**。校验/告警都是事后补救，治标。根治办法是**从生成侧锁死结构**：

1. 先把本期问题整理成一份极简 spec JSON（只描述内容：日期/镜像版本/严重等级/汇总指标/ERROR 问题列表/WARN 问题列表/mermaid 数据/上期对账结论），参考 `python ../memory/gen_report_skeleton.py`（无参时打印用法+SAMPLE_SPEC 样例）。
2. 跑 `python ../memory/gen_report_skeleton.py <spec.json> <报告输出路径>` 生成**结构已锁死的骨架**——13 章标题/编号/顺序、ERROR(第3章)与 WARN(第4章)分章、E1a/E1b/W1 编号、目录锚点、每问题四段式空架、mermaid(x-axis 与 bar 同源同长、渲染必不失败)、纯文本数据条兜底，全部由代码铺好。
3. LLM 只能往骨架里的 `{填:...}` 占位槽填**内容文字**，**禁止增删/重排/改名章节、禁止改编号体系、禁止动 mermaid 的 x-axis/bar 结构**。结构由骨架保证，不由自觉保证。
4. 生成器与校验器(`weekly_inspection_verify.py`)同源契约：骨架产物天然通过校验。只要走生成器，结构层面的 PASS 是确定的，校验退化为最后兜底而非主防线。
5. **填槽必须做「占位符 token 级替换」，禁止整行覆盖**（2026-09-20 实测踩坑）：骨架里有若干**标题行本身内嵌占位符**，如 `**根因（已通过 {填:SLS/DB/代码/Redis} 核实）**`、`**根因（已通过 {填:验证方式} 核实）**`。若实现写成「含 `{填:` 的行→整行替换成答案」，这些行的 `**根因（…）**` 外壳会被吃掉，16 个问题的四段式 根因 标题全部消失；校验器会明确报 `⚠️ [四段式] '**根因' 出现 0 次 < 问题数`（这是唯一能抓住该缺陷的信号，务必当真）。正确做法：`if 行.strip()==占位符: 整行替换（允许答案含多行日志）; else: 行.replace(占位符, 单行答案)`。填完必跑 `grep -c '**根因'` 应等于问题数（ERROR+WARN），再跑校验器。

> 三层防线：**生成层**(骨架锁结构,本节)→ 校验层(verify 机械校验,红线第8条)→ 推送层(通知必达,verify_then_notify.py)。第一层就消除结构漂移，后两层兜底。

---

#### 完整报告模板（结构由 gen_report_skeleton.py 自动生成，本段仅供人工核对最终形态）

````markdown
# SLS 日志周巡检复盘报告（{开始日期} ~ {结束日期}）

> 巡检周期：{开始日期} ~ {结束日期}
> 镜像版本：{从 SLS _image_name_ 字段提取，格式如 bar_smyze_opserver:1.0.30}
> 数据来源：SLS smyze-cloud / bar-smyze-opserver
> 报告日期：{今日日期}
> 严重等级：{最高问题等级，如 P1}
> 状态：{概述，如"部分持续告警，E6 漏查需立即处理"/"全部已收敛"}

---

## 目录

- [1. 概览](#1-概览)
  - [1.3 问题生命周期与周环比](#13-问题生命周期与周环比)
- [2. 问题优先级清单](#2-问题优先级清单)
- [3. ERROR 类问题详细分析](#3-error-类问题详细分析)
  - [E1a · {问题短描述}](#e1a--{锚点})
  - [E1b · {问题短描述}](#e1b--{锚点})
  - ...
- [4. WARN 类问题详细分析](#4-warn-类问题详细分析)
  - [W1 · {问题短描述}](#w1--{锚点})
  - ...
- [5. 巡检漏查专项分析](#5-巡检漏查专项分析)
- [6. 本周行动清单](#6-本周行动清单)
- [7. 经验教训](#7-经验教训)
- [8. 预防措施](#8-预防措施)
- [9. 修复提交记录（待更新）](#9-修复提交记录待更新)
- [10. 关联文档](#10-关联文档)

---

## 1. 概览

### 1.1 巡检数据汇总

| 指标 | 数值 | 说明 |
|------|------|------|
| 巡检周期 | {开始} ~ {结束} | {N} 天 |
| 镜像版本 | {版本} | {是否更新说明} |
| ERROR 总量 | {N} 条 | {峰值日期}峰值 {M} 条 |
| WARN 总量 | {N}+ 条 | {趋势描述} |
| 排查问题数 | {N} 条 | ERROR 类 {X} 条，WARN 类 {Y} 条 |
| 漏查问题数 | {N} 条 | {说明，无则写"无"} |
| 持续告警数 | {N} 条（{编号列表}） | 今日仍在产生 |
| 深度调查覆盖 | {编号列表} | 已用代码+DB+Redis+SLS 交叉验证 |

### 1.2 ERROR 峰值分析

{描述峰值日的构成，列出各问题贡献的条数。若有日志冗余机制（同一请求产生多条日志）需标注。}

### 1.3 问题生命周期与周环比

> 必填章节。目的：让读者一眼看清「每个问题存在多久了、是新增还是遗留、量级在升还是降」。
> 口径：本周窗口 = `now-7d ~ now`；上周窗口 = `now-14d ~ now-7d`，两窗口同一关键词均经 SLS `select count(*)` 实查后填表。
> 构建方法：拿本期问题清单与**上一期巡检报告**的问题清单逐条比对；「首次出现」以历史报告记录为准，「持续期数」= 已连续出现的巡检周期数；趋势箭头基于两窗口发生量环比。
> 边界处理：若某问题集中爆发在本周窗口起始日（恰在上一期报告截止日之后），上周计数会为 0，需用 `*` 标注并说明属同一问题延续，避免误判为「全新」。

**周环比总览**

| 维度 | 本周 | 上周 | 环比 |
|---|---:|---:|---|
| ERROR 总量 | {N} | {N} | {↗/↘/—} |
| WARN 总量 | {N} | {N} | {↗/↘/—} |
| 新增问题 | {N}（{编号}） | — | — |
| 持续遗留 | {N}（{编号}） | — | — |
| 本周收敛/消除 | {N}（{编号}） | — | — |

**本周 vs 上周·高频问题趋势对比（可视化，必填）**

> 双层渲染策略（兼容性兜底）：① mermaid `xychart-beta` 柱状图做头条趋势（IDEA/VSCode/GitHub 渲染，需 mermaid ≥v10.3）；② 紧跟一张 Unicode 数据条表做纯文本兜底，任何 Markdown 阅读器都能直读，xychart 渲不出时不影响阅读。两者数据必须一致，取本周 TOP 6~8 高频问题。
> 数据条生成：取全表最大值为基准等比缩放到固定宽度（如 24 格），`█`=已填，`░`=空；每格代表的条数在表末注明。
> 注意：x-axis 项数必须与每组 bar 的元素数严格相等，否则 mermaid 渲染失败。

```mermaid
xychart-beta
    title "本周 vs 上周 · 高频问题发生量对比"
    x-axis ["{问题1}", "{问题2}", "{问题3}", "{问题4}", "{问题5}", "{问题6}"]
    y-axis "发生量（条）" 0 --> {上限}
    bar [{本周值×N}]
    bar [{上周值×N}]
```

> 第一组（前景）为本周，第二组为上周。

| 问题 | 本周 | 上周 | 环比 |
|---|---|---|---|
| {问题} | `{██░░}` {量} | `{██░░}` {量} | {↗/↘/⚠️} {差值与判断} |

> 数据条按全表最大值等比缩放，每格 ≈ {X} 条；`*` 含义见本节末注释（集中爆发在本周窗口起始日导致上周计数为 0）。

#### 🆕 本周新增（首次在巡检中出现）

| 编号 | 问题 | 优先级 | 首次出现 | 本周发生量 | 上周发生量 | 趋势 | 状态 |
|---|---|---|---|---:|---:|---|---|
| {Wn} | {描述} | {Px} | {YYYY-MM-DD（本周）} | {量} | {量} | {↗ 新爆发} | {未处理/处理中} |

#### 🔁 上周持续遗留（跨周未闭环）

| 编号 | 问题 | 优先级 | 首次出现 | 持续期数 | 本周发生量 | 上周发生量 | 趋势 | 状态 |
|---|---|---|---|:---:|---:|---:|---|---|
| {Wn} | {描述} | {Px} | {YYYY-MM-DD（上周 {旧编号}）} | {N} | {量} | {量} | {↗/↘/↗↗/⚠️} | {持续，待…} |

#### ✅ 本周收敛 / 已消除（上周存在，本周明显下降或归零）

| 上周编号 | 问题 | 上周优先级 | 本周发生量 | 上周发生量 | 趋势 | 当前判断 |
|---|---|---|---:|---:|---|---|
| {En} | {描述} | {Px} | {量} | {量} | {✅ 已消除/↘ 下降 N%} | {根因已修复/量级回落/连续两周零发生} |

> 校正提示（如适用）：若发现上一期判级的问题（尤其 P1/安全类）在本期被漏记或降级，必须在此处用 `⚠️` 标出，给出 SLS 实查证据，并要求补记进问题清单与行动清单——这是「巡检漏查」红线。

---

## 2. 问题优先级清单

| 编号 | 问题描述 | 优先级 | 状态 | 本周发生量 | 备注 |
|------|---------|--------|------|-----------|------|
| E1a | {描述} | **P1** | {已收敛/持续中/持续失败静默} | {量} | {关键信息} |
| ... | ... | ... | ... | ... | ... |

---

## 3. ERROR 类问题详细分析

### E1a · {问题短描述}（{字段/模块}）

**现象**

{时间范围}，{模块/接口} 批量失败，日志关键字：

```
{关键日志摘要，2-5行，含异常类名和关键SQL/参数}
```

{捕获路径说明：哪些 Handler/Aspect 各命中多少次，是否同一请求产生多条日志}

集中时间：{日期 时间}（峰值，共 N 条 ERROR）。**{收敛状态描述}**。

**根因（已通过 {验证方式：SLS/DB/代码/Redis} 核实）**

{根因描述，含代码路径、DDL 状态、配置缺失等具体信息}

**影响**

- {影响范围：涉及哪些接口、哪些用户、持续多久}
- {业务影响：如 C 端无法查看订单、日结数据缺失}

**建议**

1. {具体建议，可操作}
2. {具体建议}

---

{... 对每个 E 问题重复上述四段式 ...}

## 4. WARN 类问题详细分析

{格式同上，每个 W 问题用相同四段式}

---

## 5. 巡检漏查专项分析

{若本次发现了之前巡检遗漏的问题，详细分析为什么漏查、漏查机制是什么}

---

## 6. 本周行动清单

| 优先级 | 行动 | 负责角色 | 截止时间 | 状态 |
|--------|------|---------|---------|------|
| P1 | {行动描述} | {后端开发/SRE/DBA} | {截止日期} | 待处理 |
| ... | ... | ... | ... | ... |

---

## 7. 经验教训

### 教训 {N}：{标题}

{描述问题场景}

**规则**：{可直接写入规范/checklist 的具体规则，含阈值和操作指引}

---

{... 每条教训重复上述格式 ...}

## 8. 预防措施

### 8.1 短期措施（本周内落地）

| 措施 | 执行人 | 说明 |
|------|--------|------|
| {措施描述} | {角色} | {细节} |
| ... | ... | ... |

### 8.2 长期措施（下个迭代规划）

| 措施 | 执行人 | 说明 |
|------|--------|------|
| {措施描述} | {角色} | {细节} |
| ... | ... | ... |

---

## 9. 修复提交记录（待更新）

| 编号 | commit | 说明 | 状态 |
|------|--------|------|------|
| - | TBD | {fix/feat 描述} | 待提交 |
| ... | ... | ... | ... |

---

## 10. 关联文档

| 文档 | 位置 |
|------|------|
| {文档名} | `{路径}` |
| 本报告 | `docs/analysis/ops-weekly-inspection/weekly-inspection-postmortem-{YYYY-MM-DD}.md` |

---
````

---

#### 优先级判定规则

| 级别 | 判定条件 |
|------|---------|
| P1 | 定时 Job 静默失败、支付回调失败+资损风险、核心下单/查询链路 ERROR（DDL缺失等）、安全事件（SQL注入探测等） |
| P2 | 持续告警（连续7天且今日仍在）、枚举缺失、DB连接抖动、日结数据缺失、字段长度不足 |
| P3 | 已收敛单次 ERROR、权限拒绝、DB 核实后确认无资损的防御性校验 ERROR |

**特殊规则**：
- Job/支付/下单/库存类权重 ×2（即使数量少也至少 P2）
- 安全事件一经发现直接 P1
- 公共切面捕获点（TenantJobAspect / GlobalExceptionHandler）必须下钻后再定级
- GlobalExceptionHandler 下的 **不同 SQL 错误类型必须拆分为独立问题**，不能合并
- DB 核实"无资损"后可降级（如 `pay_notify_task` 全部 status=10 则降 P3）

#### 行动清单截止时间规范
- P1 未处理：**今日**
- P2 持续中：**本周内**
- P3：**下个迭代**

#### 报告质量红线
1. **每个问题必须四段式**：现象（含日志摘要 2-5 行）→ 根因（标注验证方式）→ 影响 → 建议
2. **概览汇总表必须完整**：ERROR/WARN总量 + 持续告警数 + 深度调查覆盖
3. **问题优先级清单必须有表格**：编号/描述/优先级/状态/发生量/备注
4. **经验教训必须有规则**：每条教训必须产出一条可执行规则
5. **预防措施分短期/长期**：短期本周、长期下迭代
6. **深度调查≥50%问题**：至少一半问题经过 DB/代码/Redis 交叉验证
7. **锚点目录完整**：每个 E/W 问题在目录中有独立锚点链接
8. **章节结构必须与模板一致**：章节标题、编号、顺序严格对齐模板（`3. ERROR 类` 与 `4. WARN 类` 分两章不可合并；`5. 巡检漏查专项分析` 不可改名或省略；问题编号 ERROR 用 `E1a/E1b`、WARN 用 `W1`）。写完报告后必须逐章对照模板目录自检一次。
   - **【硬卡口·强制·代码级·通知必达】** 报告写入后**禁止直接调用 `notify_feishu()`**。唯一允许的推送方式是走入口：`python ../memory/verify_then_notify.py '<报告路径>' '<飞书正文>'`。**核心原则：通知永远发，校验只决定通知形态，绝不决定是否发**——
     - 校验 PASS → 推送正常通知；
     - 校验 FAIL → **仍然推送**，但改发「降级告警」：正文顶部加 ⚠️ 格式未达标标记 + 失败项清单，正文仍完整保留 P1/P2/P3 问题摘要与报告路径，逼人工修格式但运维信息一条不丢；
     - 报告文件缺失 → 也推送一条「巡检异常·需人工介入」告警，绝不静默。
     校验项：13 必备章节齐全且顺序正确 / ERROR-WARN 分章未合并 / 问题编号合规(禁裸 N1) / 四段式齐全 / mermaid x-axis 项数==每组 bar 元素数 / 目录锚点 / **上期对账(红线第9条,防 P1 静默消失)**。校验 FAIL 后仍需按失败项修报告并重跑入口直到 PASS，但这是质量收尾，不阻断本次告警送达。
   - 设计原因与教训：本入口经历两次纠偏。① 红线第 8/9 条曾以纯文字形式存在但 2026-06-18(漏报 P1)、06-26(章节合并+编号 N1)两次仍漂移，根因=「写报告→自觉跑校验→推送」靠 LLM 自律串联、校验可被跳过 → 故把校验与推送绑成单次入口调用，消除自觉环节。② 初版入口设计成「校验 FAIL 则物理拒推」，这是**严重错误**——格式不达标就吞掉整次告警，等于让巡检结果静默消失，重蹈 06-18 漏报 P1 的覆辙。**运维巡检第一要务是把问题送到人眼前，格式只是次要质量问题，绝不能因格式问题牺牲告警送达**。故最终定为「通知必达 + 校验降级告警」。
9. **上期遗留必须闭环**：上一期报告的每个问题（尤其 P1/安全类）都要在本期「1.3 问题生命周期」中给出结论（持续/收敛/消除），不允许静默消失。上期 P1 在本期被降级或不再出现时，必须有 SLS 实查证据支撑，否则视为漏查。

#### 对话输出（报告写入后同步展示）
1. 问题优先级汇总表（P1/P2/P3，含状态和发生量）
2. 本周 TOP 3 问题（最高优先级，根因一句话）
3. 报告文件完整路径
4. 深度调查覆盖情况（哪些问题经过 DB/代码验证）

## 避坑要点
- SLS 无独立 exception 字段，必须从 content 提取；若本期实查 `content` 为 null 且 `message` 无法按内容检索/返回原文，公共捕获点只能报告计数和“根因未能从SLS取证”，不得把位置名或统计量推断为业务根因、资损或数据缺失；改以源码/DB最小核验标注其各自结论与证据边界。
- TenantJobAspect 取样≥10条（3条可能漏少数异常类型）
- 公共切面不可直接定级，必须先下钻
- 文件名日期带连字符，不加后缀
- GlobalExceptionHandler 不同 SQL 错误类型不可合并为一个问题
- cnt>500 的 WARN 必须查 now-1h 判断是否持续
- SLS from_time/to_time 只支持相对格式(now-7d)，不支持ISO+时区(2026-06-11T00:00:00+08:00会报错)
- 下钻取样用全文关键词搜索(如 `handleTableNotExists`)而非 `location:xxx` 精确匹配（location值含包名缩写）
- logStore 参数是驼峰(大写S)，写成 logstore 会报错
- DB工具名是 `execute_sql_bar_op_prod`（不是execute-query）
- 安全事件(SQL注入探测等)一经发现直接定P1，不等收敛
- Phase2取样数≥10条才能覆盖异常类型多样性（3条太少）
- 报告深度标准：每问题必须有"现象(含日志摘要)→根因(标注验证方式)→影响→建议"四段式
- 巡检不可"跑通即止"：Phase2B/2C/Phase3(DB验证)全部执行完才能写报告
- 发现标杆/差距时必须主动回写SOP（模板/规则/阈值），不等用户二次提醒——这是SOP维护闭环的基本要求
- 【漏报根因·2026-06-18】上周P1(SQL注入探测)在本期报告中静默消失=漏查。根因有三：①旧版红线只管"深度/锚点"未约束章节结构，执行者把ERROR/WARN两章合并、漏查专项改名、编号乱用都没被拦；②流程只盯本周now-7d窗口，从无"读上期报告做对账"步骤，导致上期问题无人复核下落；③SLS示例参数误写小写logstore撞validation error分散注意力。修复=红线第8(结构必须对齐模板+写完逐章自检)+第9条(上期每问题尤其P1须在1.3给结论,降级/消失须SLS证据)+Phase4前置"读上一期报告"+1.3生命周期模板章节。教训:模板类SOP只写"长什么样"不够,必须配"硬性结构红线+写完自检+跨期对账"三件套
- 【可视化】1.3趋势对比用"双层兜底": mermaid xychart-beta头条图(本仓design文档大量用mermaid graph/gantt,IDEA/VSCode/GitHub可渲染)+紧跟Unicode数据条表(纯文本兜底)。坑:①xychart-beta需mermaid≥v10.3,本仓无版本固定声明,故必须有纯文本兜底层;②x-axis项数必须严格等于每组bar元素数,不等则渲染失败(写完用脚本核对);③数据条按全表最大值等比缩放固定宽度,表末注明每格条数。两层数据必须一致且取真实SLS值
- 【日志示例栅栏·2026-09-30】骨架里「日志示例/逐日分布」槽位已由生成器自带 ``` 包裹，填槽内容**只放原始日志行、不要再写 ``` **（否则成为「开-开-内容-闭-闭」嵌套：渲染错位成空块+正文裸露，且两层校验器都不报）。收尾必查：`grep -c '^```' 报告` 为偶数（mermaid 头行 ```mermaid 不计入该正则，故含 1 个 mermaid 图时总数应为奇数）；发现相邻重复栅栏用单遍消除（open,open,…,close,close → open,…,close），改完**必须重跑** `verify_then_notify.py` 前置校验（结构校验不受栅栏影响，别指望它兜底）

## 定时任务（已配置）
- 任务定义：`sche_tasks/weekly_inspection.json`，`enabled:true`
- 触发：`repeat=weekly_last_workday`（chinese_calendar判中国调休，每周最后工作日）+ `schedule="16:00"` + `max_delay_hours=6`
- prompt串完整闭环：读本SOP→全Phase执行(禁跳步)→按Phase4模板写报告→**唯一推送方式=运行 `python ../memory/verify_then_notify.py '<报告路径>' '<飞书正文>'`(禁止直接调notify_feishu)。通知必达:校验PASS发正常通知/FAIL发降级告警(含问题摘要+失败项)/报告缺失发异常告警,任何情况都推送且只推一次,绝不静默。校验FAIL后仍需修报告重跑直到PASS,但不阻断本次告警送达**
- scheduler触发逻辑见 `reflect/scheduler.py:128`
