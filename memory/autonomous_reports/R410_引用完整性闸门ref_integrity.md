# R410 引用完整性闸门 `tools/ref_integrity.py`（C1 · 第12批·三路评审 9.33 并列最高）

> 生成 2026-09-25｜任务来源 `temp/TODO.txt` 第 7 行（C1）｜工件 `tools/ref_integrity.py`（261 行 / 12852 B，只读、fail-open、纯 stdlib、零 shell/零网络）
> 数据 `<repo>/temp/ref_gate/*.json`｜演练证据目录 `temp/ref_gate/`（脚本可复跑）
> 本报告所有数值均由命令当场粘贴原始输出；每处附复现命令。

## §0 一句话

既有两器（R378 记忆侧 / R380 报告侧）能判「引用指向的文件在不在」，但**没有单入口、没有 `path:line` 成对断言、没有结构化产物清单、且 R404 复核器零消费者**；
本项把这些补成**一个收尾可调用的闸门**，并让「报告自称的产物」变成可复用索引（本批第 4 条与归档对账的输入），**判定内核 100% 复用既有两器，零行自研判据**。

## §1 验收① 「已有能力 vs 缺口」对照表（重叠 ≥80% 则只补缺口）

| 既有件 | 覆盖 | 输入/输出 | 与本次重叠 | 缺口（本次补） |
|---|---|---|---|---|
| `tools/memory_assert_audit.py`（R378） | **记忆侧**路径 token 存在性（EXISTS/GONE/AMBIG + 失败分类 F1–F5）**＋行号断言**（`path:NN` → OK/LINEOFF/NOFILE/AMBIG；实测 30 条＝29 OK＋1 AMBIG） | memory 语料 → JSON/文本 | **token 级 Jaccard 0.9153**（见下）；**行号断言同为 30 条**（同语料、本器直接用它的 `LINE_RE`） | 无单入口；输出为「人读」形态、无结构化产物索引；非收尾档位 |
| `tools/report_assert_audit.py`（R380） | **报告侧**路径断言 + 「自称产物」分层 + 被引用度排序（**无行号断言**：源码无相关实现，实测 JSON 无该键） | 报告语料 → top-N/JSON | 判定内核**直接 import 复用**（本器调用其 `classify`） | 无单入口；**报告侧无 `path:line` 断言**；无索引；输出面向「人读 top-N」而非「流程消费」 |
| `tools/archive_integrity.py`（R397） | 归档**编号**维度（同号组/同源 sha1/三源覆盖/缺号） | 归档树 → 报告 | **不重叠**（不同维度） | —（互补：它管编号，本器管引用） |
| `tools/report_number_audit.py`（R407） | 报告**数值**断言回读（FROZEN/INTERNAL/RATIO/STAT） | 报告 → 告警 | **不重叠**（数值 vs 路径） | —（互补：路径/数值两轴） |
| `temp/ref_recheck.py`（R404） | 18 个候选断链名的**今日现况**复核（basename 索引） | 清单 → 复核 | 结论一致（见 §9） | **无消费者**（实测见下）；只在 temp/、非通用入口 |

**重叠率实测（可复跑 `python3 temp/ref_gate/overlap.py`）**：

```
[① 记忆侧 token 级重叠] maa唯一=878 refi唯一=932 交集=865 Jaccard=0.9153 ⇒ PASS(≥0.80)
  maa独有 13 条:['cursor/rules/ctf.md', 'ga/l2/deps.json', 'ga/trellis_runtime.py', 'github/workflows/tests.yml', 'memory/gen_report_skeleton.py', 'memory/herdr_agent_client.py', ...
  refi独有 67 条:['../TMWebDriver.py', '../agentmain.py', '../memory/gen_report_skeleton.py', ...
```

- 口径：两侧都做归一（`~/`→home、剥 `./`、剥 GA 绝对前缀），因为两器记录的写法不同（一方存绝对路径、一方存仓库相对）。
- **91.53% ≥ 80% ⇒ 只补缺口**：本器**没有**新写判据。代码级证据（`tools/ref_integrity.py`）：

```
sys.path.insert(0, dirname(abspath(__file__)))
import memory_assert_audit as maa      # 常量 TOK_RE/LINE_RE/EXTS、basename 索引、语料枚举
import report_assert_audit as raa      # classify()：判定内核，本器零行重写
```

- 差异来源已核（非缺陷）：`maa独有` 13 条=语境修复型 token（如 CJK 前缀修复出的 `cursor/rules/ctf.md`）；`refi独有` 67 条=写法形态（`../` 相对、`SKILLS_DIR/` 占位、`.github/...`）。

**「无消费者」实测（排除 temp/）**：

```
$ for t in ref_recheck memory_assert_audit report_assert_audit archive_integrity report_number_audit ref_integrity; do grep -rl "$t" memory tools sche_tasks reflect_scripts | grep -v '^temp/' | wc -l; done
  ref_recheck -> 3      # 全部为「记忆/报告里提到它」，无任何运行时调用
  ref_integrity -> 2    # 本器文件 + R409 规划报告
```

即：R404 的复核器只被**文字**引用（`memory/global_mem_insight.txt:7`、`R409_规划模式第12批_三路评审与下一批TODO.md`），这正是 TODO 所说「产出无消费者=P1 模式本身」。

## §2 验收② 工具能力（真语料实测，原始输出）

```
$ python3 tools/ref_integrity.py --target memory  --json temp/ref_gate/refi_memory.json  --index-out temp/ref_gate/refi_memory_idx.json
  generated 2026-09-25 18:43:50 | secs 0.1
  corpus {'files': 95, 'bytes': 994609}
  index {'files': 109225, 'dirs': 12318, 'cap': 'ok'}
  verdicts {'EXISTS': 1391, 'GONE': 257, 'AMBIG': 55}
  by_kind {'BARE': 220, 'REPO': 22, 'HOME': 7, 'ABS': 2, 'UP': 4, 'TMP': 2}
  line_asserts total 30 ok 29 bad 1 lineoff 0 nofile 1
  skip {'url-ish': 41, 'prose-slash': 11, 'compound(+)/产品名': 26, 'suffix-fragment': 29, 'placeholder': 30, 'cjk-prefix-repaired': 2}
  write_violations []

$ python3 tools/ref_integrity.py --target reports --json temp/ref_gate/refi_reports.json --index-out temp/ref_gate/refi_reports_idx.json
  generated 2026-09-25 18:43:50 | secs 0.2
  corpus {'files': 161, 'bytes': 1482265}
  index {'files': 109225, 'dirs': 12318, 'cap': 'ok'}
  verdicts {'EXISTS': 2633, 'GONE': 682, 'AMBIG': 22}
  by_kind {'BARE': 489, 'REPO': 76, 'TMP': 14, 'ABS': 79, 'UP': 21, 'HOME': 3}
  line_asserts total 176 ok 160 bad 16 lineoff 6 nofile 10
  skip {'url-ish': 20, 'compound(+)/产品名': 14, 'suffix-fragment': 39, 'prose-slash': 15, 'url': 5, 'placeholder': 16}
  write_violations []
```

- 引用形态：`path`（BARE/REPO/ABS/HOME/UP/TMP 六类）与 `path:line` 成对断言（`line_asserts`，判 `ok/LINEOFF/NOFILE`）。
- `--target all --last 5` = 收尾档（最近 5 份报告 + 记忆侧）。
- **fail-open**：任何内部异常只在 stderr 提示并以 rc=0 返回，不阻断收尾链（闸门定位=提示，不是路障）。
- 观测到的既有断链（本次顺带产出，非本项要修的）：reports 侧 `GONE 682` / `AMBIG 22`；`line_asserts bad 16`（`LINEOFF 6 + NOFILE 10`）。NOFILE 中典型是 basename 索引里不存在的报告外产物名（如 F1 类）。

## §3 验收③ 产物存在性索引（副产品，供本批第 4 条与归档对账复用）

```
$ python3 -c "import json;d=json.load(open('temp/ref_gate/refi_reports_idx.json'));f=d['files'];print(len(f));print(d['schema'])"
150
ga.ref_artifact_index/v1
$ # 含「自称产物」的报告数
62 / 150
```

- 结构：`files[<报告相对路径>] = {declared:[{file,line,tok,kind,path,verdict,drift,claim}], gone:[...]}`；`declared` = 报告里**自称产出**的产物行（`产出|产物|新增|落盘|生成|写入|实现|新建` 等词所在行，口径继承 R380 `SELF_CLAIM`），`gone` = 其中判定为 GONE 的。
- 索引自身声明消费方（写在 JSON 的 `note` 字段，防「无消费者清单」）：①收尾闸门告警 ②待批项「证据三桶」判「证据齐备」 ③归档对账。
- 规模：`files: 109225 / dirs: 12318`（basename 索引，供 BARE 类回退解析）。

## §4 验收④ 负控 ≥3（工具内建 `--selftest`，原始输出）

```
$ python3 tools/ref_integrity.py --selftest
NEGCTL {"n1_fake_selfmade_must_gone": true, "n2_real_file_must_exist_silent": true, "n3_lineoff_detected": true, "n4_claim_lines_collected": true, "p1_real_files_no_gone": true, "n5_off_switch_zero_scan": true}
SELFTEST PASS  6/6
```

对应 TODO 三条要求：
- 「真存在引用不得报警」→ `p1_real_files_no_gone` + `n2_real_file_must_exist_silent`
- 「已知断链必报警」→ `n1_fake_selfmade_must_gone` + `n3_lineoff_detected`（行号漂移必须被抓）
- 「打印语料计数自证真读到」→ 每次真跑的 `corpus {files,bytes}` 与 `index {files,dirs}`；selftest 亦断言计数非零。
- 另有 `n4_claim_lines_collected`（自称产物行被收进索引）与 `n5_off_switch_zero_scan`（关闭开关后零扫描）。

## §5 验收⑤ 只读自证（前后 sha256 全同）

```
$ python3 temp/ref_gate/readonly_proof.py
[run] rc=0  stdout末行: [index] /Users/cuip/projects/GenericAgent/temp/ref_gate/proof_idx.json
文件数 before=389 after=389
  memory: +0 -0 ~0
  tools: +0 -0 ~0
只读自证: PASS 前后 sha256 全同
RC=0
```

- 口径与边界（写在脚本头）：目标目录 = `memory/` 与 `tools/`（被读的语料与工具源码），跑前跑后逐文件 sha256 比对；跑的是**同一工具的真实调用**（rc=0）。
- **不**把 `temp/` 纳入断言：其它会话会并发改写 temp/，把它算进来只能得到噪声（工具自身产物只落 `--json/--index-out` 指定路径，本轮都在 `temp/ref_gate/`）。

## §6 验收⑥ 接进 `helper.complete_task` ⇒ 只落待批台账（本项只交付工具 + 演练证据）

- 本项**未改** `memory/` 下任何源码（`helper.py` 属 `memory/`，按宪法第 1 条须先请示）。
- 决策项落在 `temp/pending_decisions/DECISIONS_R410_ref_integrity.md`：把闸门接进收尾链（跑 `--target all --last N` 的 `--json` 结果、非阻断地附一行告警），批准语写在台账文件内。

## §7 验收⑦ 「谁消费、消费动作是什么」

| 消费者 | 触发时机 | 消费动作 |
|---|---|---|
| **自主会话收尾（人/agent）** | 每轮任务收尾 | `python3 tools/ref_integrity.py --target all --last 5`；只看 stderr/摘要行：有 GONE 就在报告里写明「哪条引用已失效」，不再把失效路径当现状复述 |
| **`helper.complete_task`（待批 R410A）** | 归档报告时 | 以 `--files` 等价形态跑最近报告，把 `GONE` 计数附进返回串与 history 行尾（非阻断） |
| **本批第 4 条（待批项证据三桶）** | 判断「证据齐备」时 | 读 `refi_*_idx.json` 的 `declared[].verdict`：自称产物 EXISTS/GONE 一目了然，替代人工 point-and-check |
| **归档对账（`tools/archive_integrity.py` 使用者）** | 归档核对 | 用 `files` 键集合与归档清单对账（哪些报告自称产出了什么、哪些已消失） |
| **后续记忆维护（如 R404 提案落地时）** | 修断链前后 | 修前跑一次取 GONE 清单、修后再跑一次，用两次 JSON 的差集证明「修掉了」而非「以为修掉了」 |

## §8 实测发现（本次顺带产出，供后续任务取用）

1. **R404 的 6 条现役断链，行号已漂移 +2**（L2 文件在 R404 之后又长了）：
   `global_mem.txt` 记录行 276→278、283→285、319→321、347→349；`L78` 与 `wechat_signals_sop.md:17` 未动。
   本器**按 token**判定，6 条**全部**在 GONE 列表（`planning_gate.py` / `deploy_git_hooks.py`×3 / `R214_deploy_git_hooks.py` / `wechat_classify.py`）。
   ⇒ 结论：**行号型锚点会随文档增长失效，断链台账应以 token+上下文为锚**（R404 报告已按 token 记录，本器与之一致）。
2. **报告侧引用面比记忆侧大得多**：`GONE 682 / AMBIG 22`（reports）vs `GONE 257 / AMBIG 55`（memory）——因为报告里大量「临时产物路径」（`temp/...`）在归档后即消失，属**预期**而非缺陷；判读时应看 `by_kind`（TMP=14）与 `claim` 标记，不要拿总数当质量指标。
3. **行号断言的覆盖差（订正初稿）**：记忆侧既有器（R378）**已有**行号断言（`LINEOFF`/`NOFILE`/`AMBIG`，实测 30 条＝29 OK＋1 AMBIG；本器零改写沿用其 `LINE_RE`，两侧同为 30 条）；**报告侧既有器（R380）没有**（源码无相关实现、实测 JSON 无该键）⇒ 本器把行号断言扩到报告侧：**176 条，其中 LINEOFF 6 / NOFILE 10**（3.4% / 5.7%）。两侧唯一那处非 OK 项**标签口径不同**（`memory/global_mem.txt:24` 的 `~/claude_code_bridge/install.sh:5`：R378 记 `AMBIG`＋`file_lines=None`，本器记 `NOFILE`＋`resolved: .../jetbra/scripts/install.sh (+14 同名)`）——两器结论一致（不可直接采信），本器多留候选线索；跨器比较行号断言数量时须记住这处标签差异。
4. **R404 复核器结论未被推翻**：本器 6/6 命中其 6 条断链，方向一致（一个按名单、一个按全文扫）。

## §9 边界与未决（诚实声明）

- **不做**：不修改任何被审文件、不自动修复引用、不阻断收尾（fail-open）、不判「语义是否合理」。
- **不覆盖**：URL/散文斜杠/占位符等已显式 skip（计数在 `skip{}` 里，报告侧 skip 合计 109 处）；`AMBIG` 类需人工判读。
- **口径依赖**：basename 索引上限 `cap: ok`（未触顶）；索引快照时间随运行时刻变化，跨时点比较须记运行时间。
- **未接线部分**：`helper.complete_task` 的自动调用属待批（§6）；本项只交付工具 + 演练证据。
- **数字时效**：本报告数字为 2026-09-25 18:43–18:46 快照；`temp/` 与 `memory/` 随后续会话继续变更，复跑命令在 §2/§5。

## §10 复现命令清单

```bash
cd ~/projects/GenericAgent
python3 tools/ref_integrity.py --selftest                                   # §4 负控 6/6
python3 tools/ref_integrity.py --target memory  --json temp/ref_gate/refi_memory.json  --index-out temp/ref_gate/refi_memory_idx.json
python3 tools/ref_integrity.py --target reports --json temp/ref_gate/refi_reports.json --index-out temp/ref_gate/refi_reports_idx.json
python3 temp/ref_gate/overlap.py                                            # §1 重叠 0.9153
python3 temp/ref_gate/readonly_proof.py                                     # §5 只读自证 PASS
```
