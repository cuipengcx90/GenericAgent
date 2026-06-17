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

#### 文件规范
- **路径**：`{代码库}/docs/analysis/ops-weekly-inspection/weekly-inspection-postmortem-{YYYY-MM-DD}.md`
  - 日期格式：**带连字符**，如 `2026-06-23`，不是 `20260623`
  - 文件名不含额外后缀（不加 `-eval-test`、`-draft` 等）

---

#### 完整报告模板（严格按此格式输出）

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

#### 对话输出（报告写入后同步展示）
1. 问题优先级汇总表（P1/P2/P3，含状态和发生量）
2. 本周 TOP 3 问题（最高优先级，根因一句话）
3. 报告文件完整路径
4. 深度调查覆盖情况（哪些问题经过 DB/代码验证）

## 避坑要点
- SLS 无独立 exception 字段，必须从 content 提取
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

## 定时任务（已配置）
- 任务定义：`sche_tasks/weekly_inspection.json`，`enabled:true`
- 触发：`repeat=weekly_last_workday`（chinese_calendar判中国调休，每周最后工作日）+ `schedule="16:00"` + `max_delay_hours=6`
- prompt串完整闭环：读本SOP→全Phase执行(禁跳步)→按Phase4模板写报告→`notify_feishu()`推送飞书私信(含P1/P2/P3数量+TOP问题+报告路径，只推一次)
- scheduler触发逻辑见 `reflect/scheduler.py:128`
