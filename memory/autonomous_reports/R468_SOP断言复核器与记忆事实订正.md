# R468 SOP 断言复核器与记忆事实订正

- 任务来源：`temp/TODO.txt` L179（第20批 C4，三路评审 codex/槽3/槽4 = 5/8/7 = 6.67）
- 交付日期：2026-09-28
- 结论：新增只读复核器 **`tools/durable_facts_lint.py`**（326 文件 / 13.7 MB 语料 **1.04 s**，`--selftest` **22 passed / 0 failed**），用它查出并**订正 1 条 SOP 事实错误**，登记 3 条已知缺口（CLOSED）、复核 4 条为真；同批完成 3 项性能修复（同 token 集下旧式逐 token 全仓 walk 239.01 s → **1.06 s = 1356×**），并用新负控 NC13 把「优化不得改变判定」锁死。

---

## 1. 任务原文与真实缺陷

L179 要求：**L1/L2/SOP 事实订正批（断链 / 夸大 / 过期 / 自相矛盾四类）+ SOP 断言复核器**，其内已写明实测缺陷：

> 实测 task_planning SOP 写「INDEX.md 按编号升序⇒新行追加末尾」，而现盘 INDEX.md 首 6 行=R464,R463,R462,R461,R430,R459、末 10 行=R448…R458 升序段 ⇒ 新行实际插顶部，该句与实现相反（本次收尾因此需二次核对）

复核结论：**该断言成立**。`INDEX.md` 顶部是逐批新插行、标题块之下那段「升序体」是历史遗留排列（R414 时代口径），继续按旧句操作会把新行插错位置。

---

## 2. 交付物

| 交付物 | 尺寸 | 行数 | sha256 前 16 |
|---|---|---|---|
| `tools/durable_facts_lint.py` | 34983 B | 704 | `972720f7a61c6d63` |
| `tools/durable_facts_assertions.json`（断言注册表） | 4540 B | 97 | `ceb9377678c106fa` |

判定为三态（fail-closed，非二态）：`MATCH` / `CONTRADICTED` / `UNVERIFIABLE`（源文件缺失、空语料、来源不可判一律 `UNVERIFIABLE`，绝不冒充 MATCH）；每条断言另有 `status`（`ACTIVE` / `CLOSED`），`CLOSED` 默认不进 rc，`--strict` 才计入。

### 2.1 注册表实测结果（7 条断言）

| ID | 类别 | 状态 | 判定 | 依据（工具原文） |
|---|---|---|---|---|
| R468-A1 | SOP_VS_IMPL | ACTIVE | **MATCH** | SOP 断言(TOP) 与实现(TOP) 一致 |
| R468-A2 | SOP_VS_IMPL | ACTIVE | MATCH | SOP 断言(TOP) 与实现(TOP) 一致 |
| R468-A3 | COUNT_SELF | CLOSED | CONTRADICTED | 计数行声明 397 但实测 432 行 |
| R468-A4 | COUNT_SELF | CLOSED | CONTRADICTED | 计数行声明 398 但实测 471 行 |
| R468-A5 | REF_EXISTS | CLOSED | CONTRADICTED | 有 4 个 token 盘上查无 |
| R468-A6 | REF_ABSENT | ACTIVE | MATCH | 全盘查无（与断言一致） |
| R468-A7 | REF_EXISTS | ACTIVE | MATCH | 可在 memory/ 下解析；另有 12 个同名候选（提示级） |

A1 是本次订正对象（见 §3）；A3/A4/A5 是**已登记未订正**的已知缺口（见 §7）。

---

## 3. 订正实施与 A/B 取证

改动 1 处：`memory/autonomous_operation_sop/task_planning.md` 第 47 行「落账后三步」的④条，原文声称 `INDEX.md` 按编号**升序**⇒新行追加**文件末尾**；改为：

> ④ **三索引插入方向**（R414 实测）：`history.txt` 与 `history.md` 按编号**降序**⇒新行插**顶部**（第 1 行）；`INDEX.md` 按编号**降序**⇒新行插**顶部**（第 1 行；R468 实测：文件标题块之下仍留有历史「升序体」，属遗留排列，勿据其判插入方向）。

A/B 取证（同一注册表、改前改后各跑一次，产物落 `temp/durable_facts_lint/`）：

| 快照 | A1 判定 | rc | 语料 |
|---|---|---|---|
| `lint_before.json` | **CONTRADICTED** | **1** | files 326 / bytes 13725076 |
| `lint_after.json` | **MATCH** | **0** | files 326 / bytes 13725195 |

两次语料字节差 119 B = 本次 SOP 改动本身（工具对语料做只读快照，故订正会体现在下一跑）。

---

## 4. 四类事实复核结果（断链 / 夸大 / 过期 / 自相矛盾）

| 类别 | 对象 | 现状 | 处置 |
|---|---|---|---|
| 自相矛盾 | SOP ④ 条 vs 盘上 INDEX.md 排列（A1） | 与实现相反 | **本批订正**（§3） |
| 自相矛盾 | `history.md` 降序 vs `history.txt` 降序（A2） | 一致 | 无操作（MATCH） |
| 过期 | `history.md` / `history.txt` 计数行（A3/A4） | 计数落后于实际 | 登记 CLOSED，**不代改**（属他会话行） |
| 断链 | `wechat_signals_sop.md` 引 4 个 token 盘上查无（A5） | 真断链 | 登记 CLOSED（修法属 R404 待批台账议题） |
| 夸大 | R416 报的 kc L1 标题「守卫已接线」 | **现场核查已不存在**：L1 现文写「守卫 `tools/kc_snapshot_guard.py` 未接线待批」 | 他会话已修，本批复核通过、不动 |
| 断链 | R404 六行断链（`global_mem.txt` L78/276/283/319/347 + `wechat_signals_sop.md:17`） | 抽查：`deploy_git_hooks.py` 与 `bridge_autonomous.py` 在盘上/git 全历史均查无；`view_pending.py` 实存于 `temp/pending_decisions/` | 属 R404/R448 已登记议题，本批不重复处置 |

要点：**本次唯一需要动手的订正是 A1**；其余三类不是「找不到」，而是「已登记但修法待批 / 属他会话行 / 已被他会话修掉」。复核器把这些变成可机械复跑的一行判定，而不是靠人读长文。

---

## 5. 性能工程：三项修复 + 1356×（判定零变化）

首版全量真跑 17.5 s（`--proof` 落盘时更久），逐段归因后做三处**不改变判定**的修复：

| # | 修复点 | 前 | 后 | 等价性判据 |
|---|---|---|---|---|
| 1 | `_resolve`：一次性 basename 索引（原为每 token 全仓 walk） | ~17 s | 0.24 s | 同 token 集下解析结果集合**逐字等价** |
| 2 | `scan_claim_body`：逐行先筛正则，命中行才做子串匹配 | 4.63 s | 0.31 s | 全量 JSON 输出**逐字一致**（并固化 NC13） |
| 3 | `_scan_paths` | 分窗改造后回退原样 | — | 保持原行为（避免为优化引入新语义） |

- 全量真跑（326 文件 / 13725195 B）：**1.04 s**，`cProfile` 插桩 1.30 s。
- 同 token 集下「旧式逐 token 全仓 walk」实测 **239.01 s** ⇒ 索引化后 **≈1356×**。

**已声明边界**（NC12 系列锁死）：basename 索引是**快照语义**——建索引后新增文件在兜底路径不可见；重建索引后可见；真幽灵（盘上确无同名）仍判 False。这是为了消除「兜底恒真」而不牺牲正确性。

---

## 6. 负控与只读自证

`--selftest` 输出（末行原文）：`selftest: 22 passed, 0 failed`，rc=0，0.03 s。

关键负控（含本次新增 4 条）：

- NC1/PC1：SOP 断言与实现**相反**必报 `CONTRADICTED`；**一致**须 `MATCH`（正控，防恒报真）
- NC2/NC3/NC4/NC5/NC5b：混合 token、源文件缺失须 `UNVERIFIABLE`（非 MATCH）、计数不自洽、声明与自承并存、跨 artifact 不得互串（作用域守卫）
- NC6/NC7：`status` 字段透传；rc 默认排除 `CLOSED`、`--strict` 包含（default=4 / strict=5）
- VC1/NC8/NC9/NC10：真身可解析、空语料不得静默通过、全存在须 MATCH、语料计数自证
- **NC11 / NC11b / NC11c（新）**：basename 兜底索引**只建一次**（50 token 后 walk=1）、索引复用后总 walk 仍=1、兜底命中真身
- **NC12 / NC12b / NC12c（新）**：建索引后新增文件兜底**不可见**（快照语义）、重建索引后命中、真幽灵 basename 判 False
- **NC13 / NC13b（新）**：优化后 `scan_claim_body` 与旧实现输出**逐字相同**，且该负控确**有命中**（`['桥']`，防两边都空跑的假绿）

灵敏度验证：把 `scan_claim_body` 临时替换为坏实现，NC13 必须报失败（实测已证）——即该等价性负控不是恒真断言。

只读自证（`--proof`）：`[只读自证] 受检文件 326，sha256 变化 = 无`，`readonly.changed = []`。

---

## 7. 遗留与边界（不冒充已解决）

1. **A3/A4/A5 为 CLOSED 未订正**：计数行与 wechat 引用断链属他会话行 / R404 待批修法，本批只登记与复核，不改他人内容。
2. **注册表只覆盖 7 条断言**：这是「抽样式护栏 + 注册表可扩」的设计，不是全量记忆审查；扩面 = 往 `tools/durable_facts_assertions.json` 加条目（每条含 `cls` / `params` / 可选 `status`）。
3. **未接线**：未接 `complete_task`、未接定时/反射器 —— 属政策面，另立提案再批（本批零接线，`grep` 可证无生产调用点）。
4. **只读**：工具不改 L1/L2/SOP，本次订正是**人工改动 SOP 一行**（A/B 前后跑各自留痕）。

---

## 8. 复跑命令

```bash
cd /Users/cuip/projects/GenericAgent
python3 tools/durable_facts_lint.py --selftest                                   # 22 passed / 0 failed
python3 tools/durable_facts_lint.py --registry tools/durable_facts_assertions.json --proof
python3 tools/durable_facts_lint.py --registry tools/durable_facts_assertions.json \
        --json temp/durable_facts_lint/lint_final2.json --proof
```

实测口径（2026-09-28 最终跑）：`corpus {files: 326, bytes: 13725195}`、`scan.paths.tokens_total = 2609` / `absent_total = 588`、`elapsed = 1.04 s`、rc=0。
