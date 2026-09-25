# R416 台账声称 ↔ 磁盘实物 对账器（只读）

- 日期：2026-09-25
- 任务来源：第 13 批 TODO 候选 C2（四路独立评审均分 8.50，并列最高；评审要求收窄为「只抽反引号内路径 + 『接线/定时』类声明，并复用 R410 解析内核」）
- 交付物：`tools/todo_claim_audit.py`（tracked，399 行 / 21322B / sha256 前 16 位 `6958dfd3e547d73d`）
- 台账：`temp/TODO.txt`（132 行 / [x]=68 / [ ]=6 / 99337B / sha1 前 16 位 `540bde27d0c04d2d`）

## 1. 结论（先说结果）

对 68 条 `[x]` 完成项做机械对账后，**真冲突 2 条，均为 kc（knowledgeC 快照）类，与评审预设的反例一致**：

| 类别 | 条数 | 明细 |
|---|---|---|
| `TITLE_OVERSOLD` 标题声称落地 × 正文自承未接线 | 2 | L33、L51（都是 kc 快照「已落地/接线」但零注册点） |
| `PATH_GONE` 声称存在但缺失（硬） | 0 | 唯一 3 个 GONE token 全部自洽或跨仓（见 §5） |
| SOFT（非硬告警，仅供人读） | 4 | `WIRING_WITH_DISCLAIMER`×1、`OUTSIDE_ROOT`×2、`ABSENCE_CONSISTENT`×1 |

即：台账的自我声明**基本诚实**（68 条里硬冲突 0，仅 2 条属于「标题级听起来已接线、正文其实写着未接线」的口径滑坡），这也正是这个工具存在的意义——把这类滑坡变成可复跑的机械判据，而不是靠人读。

## 2. 工具做什么（判定口径）

四条判据，全部只读：

| 判据 | 触发条件 | 例子 |
|---|---|---|
| `PATH_GONE` | 反引号内 token 被判 GONE，且同行**没有**紧邻的否定语（不是「XX 已不存在」这种自承） | 合成样例（见 selftest NC2） |
| `PATH_PHANTOM_ABSENCE` | 同行写着某 token「不存在」，但该 token 其实**在盘上** | 合成样例（NC5） |
| `WIRING_NO_REGISTRATION` | 行内声称「已接线/已注册/定时」的脚本名，在注册面里 **0 命中**，且同行无自承 | 真实反例：`reflect_scripts/01_system_monitor.py`（NC3 用它做正控） |
| `TITLE_OVERSOLD` | **目标段**（`[x] 类型 | 标题` 里的标题）出现「落地/接线/注册」且同时出现机制名词（定时/每日/快照/循环/反射/plist/launchd…），而**正文段**自承未接线/待批 | L33、L51 |

- 抽取内核**零重写**：复用 R410 的 `tools/ref_integrity.py` → `tools/memory_assert_audit.py`（`TOK_RE`/`build_basename_index`/三态 EXISTS/GONE/AMBIG），本工具只做「台账行 → 声明」的语义分类。
- 注册面 = `~/Library/LaunchAgents/*.plist`（29，目录内共 30 项，已排除 `Galley.plist.bak.20260925T181921`）+ `sche_tasks/{,disabled,done}/*.json`（22+3+0=25）+ `reflect_scripts/*.py`（14）；连 `basename` 索引一起是 54 个文件。
- 只读自证：运行前后对 55 个受检文件（台账 + 注册面）算 sha1，变化即报。

## 3. 验收对照

| # | 验收要求 | 实测 |
|---|---|---|
| ① | 解析全部 `[x]` 行并自证条数 | 行数=132 `[x]`=68 `[ ]`=6；行号清单逐条打印 |
| ② | 只抽可判声明（反引号路径 + 接线/定时/注册类词），逐条回读磁盘与 launchd/sche_tasks | token=58（EXISTS 55 / GONE 3）；skip 明细 `compound(+)/产品名:2 / placeholder:3 / url-ish:2 / suffix-fragment:4` |
| ③ | 真跑输出 MISMATCH 明细（≥1 条含 kc 反例） | MISMATCH=2，均为 kc：L33、L51 |
| ④ | 负控 2/2 | selftest 6/6（含 NC1 真存在不误报、NC2 假路径必报），每例断言**真的抽到 token**（防空绿） |
| ⑤ | 只读零写入（sha1 自证） | `受检文件=55 台账+注册面 sha1 变化=无`，耗时 0.3s |
| ⑥ | 复用 R410 抽取内核，禁另写第二套解析器 | `import ref_integrity as ri`，无自建 TOK_RE/文件索引 |

## 4. 逐字实测输出

```
$ python3 tools/todo_claim_audit.py --selftest
== selftest 语料自证：index=(109419, 12334, 'ok') 注册面=54 reflect_scripts=14 ==
  [PASS] NC1 真存在路径不误报  mismatch=[] parsed=1
  [PASS] NC2 假路径必报 PATH_GONE  mismatch=['PATH_GONE'] parsed=1
  [PASS] NC3 称接线无注册点必报 WIRING_NO_REGISTRATION（用真实未接线脚本 01_system_monitor.py）  mismatch=['WIRING_NO_REGISTRATION'] parsed=2
  [PASS] NC4 真免责行不得硬报  mismatch=[] parsed=1
  [PASS] NC5 幻影缺失必报 PATH_PHANTOM_ABSENCE  mismatch=['PATH_PHANTOM_ABSENCE'] parsed=1
  [PASS] NC6 引用 WARN 文案句不得误判幻影（'不存在:' 带冒号）  mismatch=[] parsed=1
selftest 6/6
rc=0
```

```
$ python3 tools/todo_claim_audit.py --soft --json temp/todo_claim_audit_r416.json
== 台账自证 ==
  temp/TODO.txt: 行数=132  [x]=68  [ ]=6  bytes=99337  sha1[:16]=540bde27d0c04d2d
== 语料自证（判定内核真读到东西）==
  注册面文件=54 (plist 29 + sche_tasks 25)  reflect_scripts=14  basename 索引={'files': 109419, 'dirs': 12334, 'cap': 'ok'}
== 抽取结果 ==
  反引号内声明 token=58  逐态={'EXISTS': 55, 'GONE': 3}  有声明行=27
== MISMATCH 明细 ==
  [TITLE_OVERSOLD] L33  【第9批·评审 7+6 / 4+5（合并候选 1+5，两阶段）】kc 快照：守卫接线 + 30 天覆盖首次消费
      目标段声称「落地/接线」某机制，而正文段自承未接线/待批（标题级与正文矛盾）
  [TITLE_OVERSOLD] L51  knowledgeC 每日快照落地：**零新增定时** + 当日补采（评审 7/10，验收已按评审改写）
      目标段声称「落地/接线」某机制，而正文段自承未接线/待批（标题级与正文矛盾）
  ⇒ 合计 2 条，分类={'TITLE_OVERSOLD': 2}
== SOFT（非硬告警）==
  [WIRING_WITH_DISCLAIMER] L33  08_kc_snapshot.py  接线面 0 命中，但同行自承未接线/待批
  [OUTSIDE_ROOT] L75    BARE token 在 GA 树内查无，但同行声明锚在 GA 仓外（内核单根=GA，故降级为软提示，非硬失配）
  [OUTSIDE_ROOT] L75    BARE token 在 GA 树内查无，但同行声明锚在 GA 仓外（内核单根=GA，故降级为软提示，非硬失配）
  [ABSENCE_CONSISTENT] L97    token 已不在盘上，但同行本就写着它「已不存在」=自洽声明，非失配
  ⇒ 合计 4 条，分类={'WIRING_WITH_DISCLAIMER': 1, 'OUTSIDE_ROOT': 2, 'ABSENCE_CONSISTENT': 1}
== 只读自证 ==
  受检文件=55 台账+注册面 sha1 变化=无
  secs=0.3
[json] temp/todo_claim_audit_r416.json (25777B)
rc=0
```

CI 用法：`--strict`（有任何 MISMATCH 即 rc=1）实测 `rc=1`——即当前台账在严格口径下是**不通过**的（这 2 条 kc 声明正是待批事项的产物）。

## 5. 三类反例的逐条判读

**（a）L33 / L51 = 真冲突（评审预设的 kc 反例）**

两条的正文都明确写着「定时注册仍是待批件」「开关待批、未新增任何定时」，但标题写的是「kc 快照：守卫接线」「knowledgeC 每日快照落地」。在注册面（29 plist + 25 sche_tasks + 14 reflect_scripts）里对 `kc_snapshot` 相关脚本名 0 命中。

判读：这不是数据错误，而是**口径滑坡**——「工具已交付 + 手动跑过一次」被标题压缩成「落地/接线」。后续排查若只读标题，会误以为已有每日定时在跑（kc 源库约 4 周滚动删除，这个误解代价高）。建议修法（不在本工具范围内、且需人工拍板）：标题改成「快照器落地（定时待批）」。

**（b）L75 的 2 条 OUTSIDE_ROOT = 假警报（已降级）**

`material.json` 之类 token 在 GA 树内查无，但同行明确写它在**GA 仓外**的绝对路径下。本工具内核单根=GA，跨仓不构成本地证据链，故降级为软提示而非硬失配。

**（c）L97 的 ABSENCE_CONSISTENT = 自洽（非失配）**

唯一真实 GONE 是 `temp/ga_api_server.py`，而该行本身就写着「已不存在/已归档」。工具识别否定词紧邻 token（≤12 字符、排除 `不存在:` 这种引用 WARN 文案的写法）后判为自洽。

## 6. 调试过程与踩坑（工具收敛史）

初版判据过宽，真跑在 68 行台账上爆出 **9 条 TITLE_OVERSOLD + 2 条幻影缺失**，其中 7 条是假警报。逐个定位后收窄三处：

1. **标题级判据从「宽词表」改「声称词 × 机制名词同时命中」**：初版把「接线」当成独立的接线声明（`L7` 的完成记录里写「定时注册仍未接线（待批）」被误当声称）——收窄后 9→1。
2. **目标段按分句切、整句含自承语的先剔除**：`L7` 的「定时注册仍未接线（待批，勿代批）」整句被剔后不再误报；而 `L51` 的「**零新增定时**」不在自承集里，所以仍然保留为真冲突。这个「剔句」与「零不剔」的边界是实测定的，不是拍的。
3. **`不存在:` 带冒号 ⇒ 引用 WARN 文案，不算幻影**（NC6 固化）。

另一个反复踩的老坑：**先证校验器真读到语料**。本工具把头两行固定为自证输出（`index=(109419, 12334, 'ok')` / `注册面=54 reflect_scripts=14`），一旦语料路径写错，看这两行即可秒判，不必等 MISS 刷屏。

## 7. 边界与未接线

- **单根 = GA**：token 落在其它仓（herdr-agents、bar-smyze）时只出 `OUTSIDE_ROOT` 软提示，不判硬失配。
- **只对 `[x]` 行判「声称」**：`[ ]` 待办行不参与（它们本就未声称完成）。
- **注册面 ≠ 运行面**：本工具只验「有没有注册点」，不验「跑没跑成功」（后者归 R411 `tools/task_freshness.py`）。
- **未接线**：未接入 `complete_task` 收尾（在收尾时自动跑一次 `--strict` 会是很有价值的守卫），也未接任何定时。属新增接线/新增定时，须单独批准，故只在此登记。

## 8. 复现命令

```bash
cd ~/projects/GenericAgent
python3 tools/todo_claim_audit.py --selftest            # 6/6
python3 tools/todo_claim_audit.py --soft --json temp/todo_claim_audit_r416.json
python3 tools/todo_claim_audit.py --strict              # 有 MISMATCH ⇒ rc=1
```

原始输出存盘：`temp/r416_raw_run.txt`；机器可读：`temp/todo_claim_audit_r416.json`（25777B）。
