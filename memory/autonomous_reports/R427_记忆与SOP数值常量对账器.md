# R427 记忆/SOP/定时 prompt 数值 ↔ 源码常量 只读对账器

> 任务来源：`temp/TODO.txt` 第 15 批候选 C5（三路评审 8.5/7/6 = 7.17）。
> 交付物：`tools/const_number_audit.py`（新建，只读）。报告与工具在同一轮内落地，未改任何被扫文件。

## 1 结论

- 工具：`tools/const_number_audit.py`，760 行 / 36913 B / sha256 前 16 位 `83e17bd579f01e97`，纯 stdlib、默认只读、不联网、不起子进程。
- 判据：只对「同一段内出现了 `.py` 路径」的「数字+单位」断言做回源（无路径 ⇒ 不判），因此不会全库猜。
- 真阳性：RULE21 记录的 dpdomain 口径差被自动抓出（`temp/TODO.txt:18` 与 `R425` 报告里写「27 天」，同段 `.py` 路径指向 `tools/dpdomain_renew.py:61  RENEW_COOLDOWN_DAYS = 25.0`）。
- 信噪比：默认口径（memory/SOP/sche_tasks/L1/L2）MISMATCH = **1** 条，且该条就是上句的真案；含 reports 口径 MISMATCH = **2** 条，两条同指 dpdomain 一事（原文与报告各一处）。
- 自测：`--selftest` **21/21**，含 4 类负控（篡改常量必报 / 无 `.py` 路径不误报 / 单位不同不硬判 / 自匹配剔除）+ 只读自证（全部语料 sha256 前后一致）。

## 2 判据（判据全貌）

| 环节 | 做法 | 关键参数 |
|---|---|---|
| 抽断言 | 语料行内正则抓「数字 + 单位」（`天/小时/分钟/秒/次/个/条/B/KB/MiB/GB/ms` 等） | 单位表 `UNIT_PAT`，字节倍率 1024 进制 |
| 归属路径 | 断言行向前/向后看 `block_lines=3` 行内找 `.py` 路径；无路径 ⇒ 该断言不判 | 路径须在仓库内可解析，否则记「路径无法解析」 |
| 常量面 | AST 抽模块级常量（`NAME = 值`，记录是否全大写）+ 关键字实参字面量（如 `timedelta(days=27)`） | 只读打开，不 import 目标模块 |
| 绑定强度 | **强**＝常量名逐字出现在窗口内，或「全大写常量 ∧ 名字含显式单位词 ∧ 该词在窗口内独立成词」；**弱**＝名字 token 字面命中（≥3 字符 ASCII）但非上述两种 | 弱绑定额外要求与断言的字符距离 ≤ `NEAR = 60` |
| 三态 | 强绑定 ∧ 同单位族 ∧ 值不等 ⇒ **MISMATCH**；弱绑定 ∧ 同族 ∧ 值不等 ⇒ **SUSPECT-NAMED**（只提示，交人核实）；其余 ⇒ **UNDECIDABLE**（含单位族不同、无路径、自匹配、距离过远） | 单位族不同**不**硬判为不一致（负控 NC3） |
| 值相等 | 同族直接比数值；单位可换算时换算后比（如 `6 小时` vs `WINDOW_SECONDS = 21600`） | 值相等且无名字命中 ⇒ 只给 CONSISTENT-VALUE 弱证据 |
| 自匹配剔除 | 命中 token 若落在断言自身的单位/数字里 ⇒ 排除（防「秒」命中 `SECONDS` 这类同形陷阱） | 负控 NC6 |

用法：

```
python3 tools/const_number_audit.py                 # 默认口径：memory/*.md + sche_tasks/*.json + L1/L2
python3 tools/const_number_audit.py --include-reports
python3 tools/const_number_audit.py --json out.json # 机器可读记录
python3 tools/const_number_audit.py --readonly-proof # 跑前后对语料做 sha256 自证
python3 tools/const_number_audit.py --selftest
```

## 3 自测原始输出（21/21）

```
PASS selftest 语料切分自证（fixture 齐备且行数>0）
PASS NC1 字面绑定+值不等 → MISMATCH
PASS NC1b MISMATCH 记录指到正确常量名（COOLDOWN_DAYS）
PASS 正控 弱绑定+值相等 → CONSISTENT-NAMED
PASS NC2 本段无 .py 路径 → 不产生断言
PASS NC3 单位族不同（次 vs 天）→ 非 MISMATCH
PASS NC4 路径无法解析 → UNDECIDABLE(不可解析)
PASS NC5 常量名无单位族 → UNDECIDABLE（单位不同不硬判）
PASS 值命中为弱证据 → CONSISTENT-VALUE（不得报 MISMATCH）
PASS 单位换算正控（6 小时 == WINDOW_SECONDS 21600）→ CONSISTENT-NAMED
PASS LIT(timedelta days=27) 值命中 → CONSISTENT-VALUE
PASS 同段回退：claim 与路径分两行仍判定
PASS 空行隔断 → 不判（无断言）
PASS 非数值断言行（R427/第15批）不产生 claim
PASS NC6 自匹配剔除：命中词即断言自身单位 → UNDECIDABLE（不得 MISMATCH）
PASS NC7 弱绑定+值不等 → SUSPECT-NAMED（需人核实，不得硬判 MISMATCH）
PASS 只读自证：全部语料 sha256 前后一致
PASS MISMATCH 记录带候选常量（文件:行/名/值）
PASS 负控A 篡改常量后 原 MISMATCH 项（27）翻为一致
PASS 负控B 篡改常量后 原一致项（25）翻为待核实
PASS 负控C 篡改后 MISMATCH 清零（无残留假阳性）
selftest: 21/21
```

## 4 真跑原始输出

### 4.1 默认口径（memory/*.md + sche_tasks/*.json + L1/L2）

```
语料文件 121 / 扫描行 12268 / 断言行 231（耗时 0.26s）
断言条数 660：CONSISTENT-VALUE=31 / MISMATCH=1 / SUSPECT-NAMED=12 / UNDECIDABLE=616
无法判定细分：.py 路径无法解析=104 / 名字命中全部为自匹配（命中词即断言自身单位/数字）⇒不判=3 / 名字弱命中但距离过远（>60 字符）⇒不硬判，避免跨语义噪音=9 / 名字弱命中（非模块常量/无显式单位词）且单位族不可比⇒不硬判=32 / 名字强绑定但单位族不同/常量名无单位族（单位不同不硬判）=2 / 无名字命中且无值命中=466

== 不一致 1 条 ==
[1] temp/TODO.txt:18  断言「27 天」
     候选常量 tools/dpdomain_renew.py:61  RENEW_COOLDOWN_DAYS = 25.0  (族=DAY)
     上下文词 21,COOLDOWN,DAYS,DPDOMAIN,JSON,PROMPT,RENEW,RULE,SCHE,SOP,TASKS,TOOLS

== 待人工核实（弱绑定同名族值不等）12 条 ==
  memory/dpdomain_renew_sop.md:54 「2min」↔ 候选 reflect/scheduler.py:54 _TRIGGER_COOLDOWN_MIN=10.0  (族=MIN)
  memory/global_mem.txt:449 「249.350h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)
  memory/global_mem.txt:449 「0.05h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)
  memory/global_mem.txt:449 「262.733h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)
  memory/global_mem.txt:625 「052 B」↔ 候选 tools/usage_census.py:50 TEMP_WALK_CAP_FILES=8000.0  (族=BYTES)
  memory/global_mem_insight.txt:6 「249.350h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)
  memory/global_mem_insight.txt:6 「0.05h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)
  memory/global_mem_insight.txt:6 「7.49h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)
  memory/global_mem_insight.txt:7 「1.46GiB」↔ 候选 temp/efficiency_monitor.py:55 log_size_mb=0.0  (族=BYTES)
  temp/TODO.txt:9 「23052B」↔ 候选 tools/usage_census.py:50 TEMP_WALK_CAP_FILES=8000.0  (族=BYTES)
  temp/TODO.txt:9 「31513B」↔ 候选 tools/usage_census.py:50 TEMP_WALK_CAP_FILES=8000.0  (族=BYTES)
  temp/TODO.txt:48 「5.6 GB」↔ 候选 tools/llm_slot_probe.py:213 PROBE_MAX_TOKENS=32.0  (族=BYTES)

== 值命中（弱证据）31 条 ==
  memory/dpdomain_renew_sop.md:8 「27 天」= reflect/scheduler.py:62 timedelta.days=27.0
  memory/global_mem.txt:449 「0 行」= memory/kc_profile/profile.py:154 z_edge=0.0
  memory/global_mem.txt:529 「090MB」= tools/mem_caliber.py:32 TIMEOUT=90.0
  memory/global_mem.txt:542 「0ms」= tools/llm_slot_probe.py:270 update.ms=0.0
  memory/global_mem_insight.txt:78 「1 行」= temp/deepdive_v2/check_extras.py:43 mut=1.0
  memory/global_mem_insight.txt:82 「1 行」= tools/l2_snapshot.py:386 rc=1.0
  memory/global_mem_insight.txt:82 「1 行」= tools/l2_snapshot.py:386 rc=1.0
  memory/roi_monitor_sop.md:8 「7日」= roi_monitor.py:68 timedelta.days=7.0
```

### 4.2 含 reports 口径（`--include-reports`）

```
语料文件 302 / 扫描行 41536 / 断言行 1161（耗时 0.46s）
断言条数 2301：CONSISTENT-NAMED=5 / CONSISTENT-VALUE=90 / MISMATCH=2 / SUSPECT-NAMED=22 / UNDECIDABLE=2182
无法判定细分：.py 路径无法解析=525 / 名字命中全部为自匹配（命中词即断言自身单位/数字）⇒不判=7 / 名字弱命中但距离过远（>60 字符）⇒不硬判，避免跨语义噪音=18 / 名字弱命中（非模块常量/无显式单位词）且单位族不可比⇒不硬判=65 / 名字强绑定但单位族不同/常量名无单位族（单位不同不硬判）=3 / 无名字命中且无值命中=1564

== 不一致 2 条 ==
[1] memory/autonomous_reports/R425_规划模式第15批三路独立评审与下一批TODO.md:68  断言「27 天」
     候选常量 tools/dpdomain_renew.py:61  RENEW_COOLDOWN_DAYS = 25.0  (族=DAY)
     上下文词 COOLDOWN,DAYS,DPDOMAIN,JSON,PROMPT,RENEW,SCHE,SCHEDULER,SOP,TASKS
[2] temp/TODO.txt:18  断言「27 天」
     候选常量 tools/dpdomain_renew.py:61  RENEW_COOLDOWN_DAYS = 25.0  (族=DAY)
     上下文词 21,COOLDOWN,DAYS,DPDOMAIN,JSON,PROMPT,RENEW,RULE,SCHE,SOP,TASKS,TOOLS

== 待人工核实（弱绑定同名族值不等）22 条 ==
  memory/autonomous_reports/INDEX.md:417 「8446B」↔ 候选 tools/llm_slot_probe.py:213 PROBE_MAX_TOKENS=32.0  (族=BYTES)
  memory/autonomous_reports/R349_draft_fix-compress-deadref.md:34 「1 MiB」↔ 候选 tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0  (族=BYTES)
  memory/autonomous_reports/R349_draft_fix-compress-deadref.md:140 「1 MiB」↔ 候选 tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0  (族=BYTES)
  memory/autonomous_reports/R349_fix-compress-deadref.md:34 「1 MiB」↔ 候选 tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0  (族=BYTES)
  memory/autonomous_reports/R349_fix-compress-deadref.md:140 「1 MiB」↔ 候选 tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0  (族=BYTES)
  memory/autonomous_reports/R365_待批台账第2批增量与超期查询.md:80 「1MiB」↔ 候选 tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0  (族=BYTES)
  memory/autonomous_reports/R409_规划模式第12批_三路评审与下一批TODO.md:38 「4425 B」↔ 候选 tools/llm_slot_probe.py:213 PROBE_MAX_TOKENS=32.0  (族=BYTES)
  memory/autonomous_reports/R414_规划模式第13批_四路评审与下一批TODO.md:39 「4425 B」↔ 候选 tools/llm_slot_probe.py:213 PROBE_MAX_TOKENS=32.0  (族=BYTES)
  memory/autonomous_reports/R421_使用热度×T0备份×tools消费面三向对账.md:13 「6.65 GB」↔ 候选 tools/usage_census.py:50 TEMP_WALK_CAP_FILES=8000.0  (族=BYTES)
  memory/autonomous_reports/R421_使用热度×T0备份×tools消费面三向对账.md:138 「455MB」↔ 候选 tools/usage_census.py:50 TEMP_WALK_CAP_FILES=8000.0  (族=BYTES)
  memory/dpdomain_renew_sop.md:54 「2min」↔ 候选 reflect/scheduler.py:54 _TRIGGER_COOLDOWN_MIN=10.0  (族=MIN)
  memory/global_mem.txt:449 「249.350h」↔ 候选 memory/kc_profile/profile.py:45 timedelta.hours=1.0  (族=HOUR)

== 名字命中且一致 5 条 ==
  memory/autonomous_reports/R349_draft_fix-compress-deadref.md:34 「8 MiB」↔ tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0
  memory/autonomous_reports/R349_draft_fix-compress-deadref.md:140 「8 MiB」↔ tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0
  memory/autonomous_reports/R349_fix-compress-deadref.md:34 「8 MiB」↔ tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0
  memory/autonomous_reports/R349_fix-compress-deadref.md:140 「8 MiB」↔ tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0
  memory/autonomous_reports/R365_待批台账第2批增量与超期查询.md:80 「8MiB」↔ tools/log_compress.py:58 DEFAULT_THRESHOLD=8388608.0

== 值命中（弱证据）90 条 ==
  memory/autonomous_reports/INDEX.md:360 「1.00MiB」= tools/log_compress.py:76 seq=1.0
  memory/autonomous_reports/INDEX.md:407 「5 条」= tools/bg_task.py:66 DEFAULT_GRACE=5.0
  memory/autonomous_reports/INDEX.md:417 「0ms」= tools/llm_slot_probe.py:270 update.ms=0.0
  memory/autonomous_reports/INDEX.md:436 「1 条」= tools/chrome_history_consume.py:458 timedelta.hours=1.0
  memory/autonomous_reports/R246_ROI监控工具自动化增强.md:304 「3个」= roi_monitor.py:32 MIN_TASKS_PER_DAY=3.0
  memory/autonomous_reports/R246_ROI监控工具自动化增强.md:305 「1个」= roi_monitor.py:65 timedelta.days=1.0
  memory/autonomous_reports/R337_roi_monitor产出量告警修复(口径v2方案).md:10 「100 行」= roi_monitor.py:33 MIN_LINES_PER_TASK=100.0
  memory/autonomous_reports/R342_待审项收口_一键决策清单与落地器.md:26 「3 个」= roi_monitor.py:32 MIN_TASKS_PER_DAY=3.0
```

## 5 真阳性：RULE21 的 dpdomain 口径差

两侧原文（工具记录的即为原样）：

- 不一致侧：`temp/TODO.txt:18` 断言「27 天」，同段路径 `tools/dpdomain_renew.py` ⇒ 该文件 `:61` 的 `RENEW_COOLDOWN_DAYS = 25.0`（族 DAY），值不等。
- 一致侧（同一口径的另一半）：`memory/dpdomain_renew_sop.md:8` 断言「27 天」，同段路径 `reflect/scheduler.py` ⇒ `:62 timedelta.days=27.0`，值相等（记 CONSISTENT-VALUE）。
- 读法：同一个「27 天」在 SOP 里指的是 **scheduler 侧冷却**（27），在 TODO/评审报告里被当成 **脚本手动去重窗口**（真值 25）。工具的判据正是「谁在这个段里被引为来源」，所以它把两种引法分开记；这也解释了为什么 RULE21 说「两个数别混」。
- 注意（自指说明）：被标 MISMATCH 的那两段文字本身就在描述这处口径差（不是在犯这个错），所以本条属「工具正确命中已知差异」，不是新增缺陷。真正待做的动作是「改口径」——见第 8 节提案 B。

## 6 假阳性治理：初版 40 条 → 1 条

初版（判据=名字 token 命中即算绑定）在默认口径报 MISMATCH **40 条**，逐条看后归为三类假阳性，对应三步收紧：

1. **自匹配**：命中 token 就落在断言自身里（如断言「秒」命中常量名里的 `SECONDS`）⇒ 加入自匹配剔除。
2. **绑定过弱**：只要词表同义命中（如「窗口」→ `WINDOW`）就算绑定，把跨语义常量拉进来 ⇒ 收紧为「必须字面 token 命中（≥3 字符 ASCII）」或「全大写常量 ∧ 名字含单位词」。
3. **跨语义远距离**：弱绑定但常量名离断言很远 ⇒ 要求字符距离 ≤ 60，超出只记 UNDECIDABLE。

收紧后默认口径只剩 1 条 MISMATCH（即真案）；代价是 12 条转为 SUSPECT-NAMED 提示档（见 4.1 清单，含 `timedelta.hours=1.0`、`TEMP_WALK_CAP_FILES=8000.0` 这类**非同一实体**的弱候选），它们只作「值得人看一眼」的提示。

## 7 弱证据档的已知噪音（诚实声明）

CONSISTENT-VALUE 是「值命中」弱证据，**不构成判定**，其中确实混有噪音，例如：

- `memory/global_mem.txt:529` 断言「090MB」= `tools/mem_caliber.py:32 TIMEOUT=90.0`（数值巧合，语义无关）
- `memory/global_mem_insight.txt:78` 断言「1 行」= `temp/deepdive_v2/check_extras.py:43 mut=1.0`
- `memory/autonomous_reports/INDEX.md:360` 断言「1.00MiB」= `tools/log_compress.py:76 seq=1.0`

⇒ 下游消费建议：只看 **MISMATCH / SUSPECT-NAMED** 两档；CONSISTENT-VALUE 与 CONSISTENT-NAMED 只当「回源成功」的副产品统计。

## 8 边界（明确不做的事）

- 只在同段出现 `.py` 路径时判；**无路径不判**（默认口径 466 条、含 reports 1564 条落在这一档）。
- 不追 import、不跨文件解析引用链；不判 shell / JSON / YAML 里的常量。
- 不做语义等价改写判定（如把常量名换成同义英文）。
- 不自动修改任何文件；发现的问题一律写成待批提案（本报告第 9 节 + 台账）。
- 只读自证：`--readonly-proof` 对全部语料做 sha256 前后比对，跑完打印一致。

## 9 待批提案（台账 `temp/pending_decisions/DECISIONS_R427_const_number_audit.md`）

- **R427A**：把本器接成「每小时本地、仅新增 MISMATCH 才推飞书」的反射器（仿 `reflect_scripts/09_task_freshness.py` 的 ledger 去重）。
- **R427B**：dpdomain 口径三处同批改（`sche_tasks/dpdomain_renew.json` prompt 的「27 天」、`memory/dpdomain_renew_sop.md` 的表述、`tools/dpdomain_renew.py:61` 附近加注释说明「25=手动去重窗口 / 27=scheduler 冷却」）。
- **R427C**：把本器作为 `complete_task` 的报告侧前置（`--include-reports` 口径），报告里的数值若与同段 `.py` 常量相冲则提示。

三条均 `revers=0`（可逆，逐字还原即可）。

## 10 复现命令

```
cd ~/projects/GenericAgent
wc -l tools/const_number_audit.py && shasum -a 256 tools/const_number_audit.py
python3 tools/const_number_audit.py --selftest
python3 tools/const_number_audit.py --readonly-proof
python3 tools/const_number_audit.py --readonly-proof --include-reports
```
