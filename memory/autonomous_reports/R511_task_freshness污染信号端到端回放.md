# R511 · task_freshness 污染信号端到端回放（误报率不退化）

- 日期：2026-09-29
- 来源：第24批·替换项（codex 提名；本轮 C7 三路平均 4.50 最低已淘汰 = codex 4 / 槽3 3.5 / 槽4 6）
- 对应 TODO：temp/TODO.txt 第 210 行
- 落点：`tools/task_freshness.py`（改）+ `tools/freshness_pollution_replay.py`（新）+ `tools/freshness_pollution_cases.json`（新夹具）

## 1. 背景与问题定义

`tools/task_freshness.py` 的 L2「静默降级签名」层原本是**纯子串命中**（`text.find(pat)`）：
任何一行只要出现判据表里的关键词，就被当成「日志里发生了失败」。

这在本机早已被量化过是假阳性来源：R466 实测（efficiency_monitor 同族问题）「26 条命中里只有 2 条落在事件块内，24 条是报告正文/源码/清单的回声」；
R467 又实测 artifact 层对「跑而不产出」无力、log 层对 birdie 迟至 48 天。也就是说 L2 的**命中**与**事件**是两件事，
而 L2 当时只有前者。R511 的任务就是把二者分开，并用端到端回放证明「误报率不退化」且现网判定不被改动。

## 2. 改动（三处，均在 tools/task_freshness.py 内）

| 位置 | 内容 |
|---|---|
| `tools/task_freshness.py:164` | 新增 `LOG_HEAD_RX`（日志行头：时间戳 + 级别 / `rc=` / `Traceback` 等） |
| `tools/task_freshness.py:244` | 新增 `classify_hit_context(text, idx, pattern)` → 返回 `("strong"/"echo", 理由)` |
| `tools/task_freshness.py:269` | `scan_signatures(..., classify=False)`：默认与旧版**逐字一致**（既有调用点不受影响） |
| `tools/task_freshness.py:618-620` | L2 段按 `echo_policy` 决定是否分级调用 `classify=True`；`echo_policy=keep` 回到旧行为（A/B 与回退用） |
| `tools/task_freshness.py:639-640` | 结果新增 `signature_events`（事件本体命中数，硬信号口径）与 `signature_echoes`（回声命中数，仅可见性提示） |

判定语义：**strong（事件本体）⇒ 可升 ALERT 并进硬问题；echo（回声/引用）⇒ 只出 INFO 可见性提示，绝不升 ALERT。**
回声形态判定：markdown 列表/标题/引用行、fenced code block 内、引号成对包裹、缩进 ≥4 且含代码特征。
"同一文件既有回声又有事件"时，事件本体必须照样被保留（污染不得掩盖真信号，回放用例 c7 覆盖）。

## 3. 端到端回放器（固化验收①，可复跑）

新增 `tools/freshness_pollution_replay.py`（**位置子命令**：`cases` / `run` / `check` / `selftest` / `proof` / `agree`），
夹具 `tools/freshness_pollution_cases.json`：8 条用例，每条自带样本文件内容 + ground truth 期望（`polluted` / `true_positive` / `mixed` / `control`），
在沙盒目录里落盘后跑真 `task_freshness` 内核，逐条比对期望与实际。

真跑（`python3 tools/freshness_pollution_replay.py run`）：

```
c1_markdown_list  polluted       verdict=INFO   events=0 echoes=2
c2_code_fence     polluted       verdict=INFO   events=0 echoes=1
c3_quoted         polluted       verdict=INFO   events=0 echoes=1
c4_indented_code  polluted       verdict=INFO   events=0 echoes=1
c5_true_log       true_positive  verdict=ALERT  events=1 echoes=0
c6_traceback      true_positive  verdict=ALERT  events=1 echoes=0
c7_mixed          mixed          verdict=ALERT  events=1 echoes=1
c8_healthy        control        verdict=OK     events=0 echoes=0
—— 误报(polluted 却 ALERT)=0 漏报(true_positive 未 ALERT)=0 正控违例=0 违例合计=0
```

`--selftest` 8/8 PASS，含 8 组可证伪的负控：

1. 关掉分类器（全判事件本体）⇒ 误报数必 >0 且闸门判红
2. `echo_policy=keep` ⇒ 回到旧行为（污染用例重新变 ALERT，证明开关是唯一差异）
3. 把回声样本改成真事件行（期望仍不告警）⇒ 必报违例
4. 夹具 0 条用例 ⇒ rc=2 fail-closed（不假绿）
5. 全判回声 ⇒ 真阳性漏报数必 >0 且闸门判红
（另有 3 项为语料自证与正跑闸门，合计 8/8）

`proof`：`proof files=2 changed=[] (kernel/fixture)` —— 只读自证，回放器与夹具 sha256 前后一致。

| 文件 | 行数 | 字节 | sha256 前16 |
|---|---|---|---|
| tools/task_freshness.py | 1185 | 67487 | 5b61236ca02adaa3 |
| tools/freshness_pollution_replay.py | 384 | 18275 | 4757d91239f25e0e |
| tools/freshness_pollution_cases.json | 288 | 6074 | c51161fe4def698f |

## 4. 验收②：现网 A/B 逐目标不退化（同刻对照）

同一时刻、同一 cwd，用 `git show HEAD:tools/task_freshness.py` 取出**改动前**版本与**改动后**版本各跑一次全量
（`--json --no-ledger`），逐目标比对 `verdict` 与 `level`：

```
counts head {'ALERT': 5, 'OK': 23, 'DISABLED': 9, 'ON_DEMAND': 1, 'LEGACY': 7}
counts after{'ALERT': 5, 'OK': 23, 'DISABLED': 9, 'ON_DEMAND': 1, 'LEGACY': 7}
targets head/after: 45 45   verdict+level diff: []
L2 signature hits head 0   after 0
```

结论：**45/45 目标 verdict+level 逐字相同**，L2 命中数两边都是 0 —— 即本机现网当前没有 L2 命中，
本次分级改动在现网"零影响空间"内，属于「先装护栏、不改变现状」的安全改动。
（过程记录：早先与 `/tmp/tf_before.json` 对比时曾出现 28 处差异，实为新增键 `None→0` 与 x_ai 一条**时间漂移**；
同刻对照把漂移排除后差异归零，故本报告以同刻对照为验收证据，早先那份跨时点对比不作判据。）

## 5. 验收③：与 error_signal（R466 内核）独立对账

新增 `agree` 子命令：对同批夹具，用 `tools/error_signal.py`（R466 错误信号内核，复用不重写）独立判事件块覆盖行，
与本器 `strong` 判据对账。判据（比「两器都判事件」更强且可证伪）：

- ① `error_signal` 事件块覆盖行中、含判据 pattern 的行，本器必须判 strong（否则 = 误杀真事件 ⇒ 记 `fn`，rc=1）
- ② 本器判 strong 却不在覆盖内的行，须有 `error_signal` `possible(keyword_hit)` 佐证 ⇒ 记 `known_delta`（有据口径差异）

真跑 `agree`：

```
c1_markdown_list  polluted      strong=[]  echo=[2, 3]  es_cover=[]  es_possible=[]
c2_code_fence     polluted      strong=[]  echo=[3]     es_cover=[]  es_possible=[]
c3_quoted         polluted      strong=[]  echo=[1]     es_cover=[]  es_possible=[]
c4_indented_code  polluted      strong=[]  echo=[2]     es_cover=[]  es_possible=[]
c5_true_log       true_positive strong=[1] echo=[]      es_cover=[]  es_possible=[1]
c6_traceback      true_positive strong=[3] echo=[]      es_cover=[1,2,3,4] es_possible=[]
c7_mixed          mixed         strong=[2] echo=[1]     es_cover=[]  es_possible=[2]
c8_healthy        control       strong=[]  echo=[]      es_cover=[]  es_possible=[]
agree: n=8 fn=0 known_deltas=2
```

`fn=0` ⇒ 本器未误杀任何被独立内核认定为事件块的行（c6 的 4 行覆盖全部被本器判 strong）。
两条 `known_delta` 都是**口径差异且有据**：error_signal 的锚点集更窄（级别 / `rc=` 要求**行首**），
任务日志 `TS LEVEL ... rc=N` 这类行按其口径只算 `keyword_hit`（`es_possible`）不算事件，本器按判据表 pattern 判 strong。
反向不要求逐条一致，故不记违例。副产物 JSON：`/tmp/r511_agree.json`（1677B）。

## 6. 残余与边界

1. **回放器与分级逻辑尚未接线到任何定时/CI**：`freshness_pollution_replay.py` 目前只能手动跑（与本仓其它只读闸门一致，接线待批）。
2. **现网 L2 命中为 0**：本次改动的收益目前只在"未来命中"上体现；验证它真的能拦住污染，靠的是回放夹具而非现网真实数据。
3. `echo_policy=keep` 是唯一回退开关（判据表可按目标配置），但**未做**全局开关的 CLI 层暴露（需改判据表）。
4. 回放器的 8 条用例是**人工构造**的样本，不代表全部真实日志形态；新增形态应补用例（`cases` 子命令打印当前 ground truth）。
5. 未改动的边界：`scan_signatures` 默认 `classify=False`，其它调用点（channel 检查等）行为逐字不变；本报告未逐一枚举调用点，以全量 A/B 45/45 相同为证。

## 7. 复跑命令

```bash
cd /Users/cuip/projects/GenericAgent
python3 tools/freshness_pollution_replay.py cases                    # 打印夹具与 ground truth
python3 tools/freshness_pollution_replay.py run                      # 端到端回放（验收①）
python3 tools/freshness_pollution_replay.py check                    # 闸门语义（rc 即判决）
python3 tools/freshness_pollution_replay.py selftest                 # 8/8（含 8 组负控）
python3 tools/freshness_pollution_replay.py proof                    # 只读自证 files=2 changed=[]
python3 tools/freshness_pollution_replay.py agree                    # 与 error_signal 独立对账（验收③）
python3 tools/task_freshness.py --selftest                           # 29/29（内含㉖–㉙ 四条 L2 分级用例）
python3 tools/task_freshness.py --json --no-ledger                   # 全量（验收②基准）
```

## 8. 结论

- 验收①：回放 8 用例误报 0 / 漏报 0 / 正控 0；8 组负控可证伪（关分类器、全判回声、keep 开关、空夹具等都会红）。
- 验收②：同刻 A/B 45/45 目标 verdict+level 逐字不变（counts 集合一致，L2 命中 0/0）⇒ 零退化、零现状改动。
- 验收③：与 error_signal 独立对账 `fn=0`，2 条差异均为有据口径差（行首锚点要求不同），非缺陷。
- 交付物：1 个改动 + 2 个新文件；`task_freshness --selftest` 29/29、回放器 selftest 8/8、proof changed=[]。
