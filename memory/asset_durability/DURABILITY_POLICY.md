# memory 非 .md 资产耐久性政策（R507）

> 本文件是**判据与口径的单一源**；机器可读登记面 = `memory/asset_durability/REGISTRY.json`（受 `.gitignore` 的 `memory/**` 规则忽略 ⇒ 入库须 `git add -f`）。
> 执行器 = `tools/memory_asset_durability.py`（纯 stdlib，只读；子命令 `scan|proof|selftest` 为**位置参数**）。

## 1. 要防的是什么

`memory/**` 里有一类资产**不是 `.md`**：它们的耐久语义与文档不同——文档由 `!memory/**/*.md` 放行入库，而非 `.md` 资产默认**既不进 git、也不在任何保护面清单里**，却可能被生产工具/闸门直接消费（例：`memory/L4_raw_sessions/*.zip` 被 `tools/l4_search.py` 消费）。
一旦这类资产丢失，**链路不会报错、只会静默降级**（典型：报告正文恢复变「查无」）。

本政策要回答的唯一问题：**哪些「有消费者但未入库」的 memory 资产存在**，以及每条各自凭什么算「可接受」。

## 2. 判据（三层，强 → 弱）

| 档位 | 判定 | 说明 |
|---|---|---|
| PATH | 消费语料里出现**带 `memory/` 前缀的完整路径** | 强证据，可单独判洞 |
| GLOB | 出现**目录级前缀**（如 `memory/L4_raw_sessions/`），按目录消费 | 强证据 |
| BASE | 只出现**裸文件名** | 弱证据：同名文件在别处也存在 ⇒ **只判 INFO，不判洞** |

- 语料 = 库内脚本/闸门（含 `tools/`、`sche_tasks/`），**排除散文 `.md` 与 `temp/` 脚本**——否则报告里的一句提及就会被当成消费者（自测 NC9 锁此边界）。
- 自引用不算消费（NC15c）；裸名被更长的路径限定到**另一个同名文件**时不算本资产证据（NC15）。
- 已入库（tracked）资产永不判洞（NC3）。

## 3. 状态语义（登记面里的 `status`）

| status | 含义 | 何时可写 |
|---|---|---|
| `DECLARED_LOCAL_ONLY` | 有意不入库：体积大/可重建，靠 **T0 保护面 + T0 归档**双覆盖 | 本器必须实测 `t0_baseline=True` ∧ `t0_archive=True` |
| `PENDING_APPROVAL` | 有耐久缺口，修法已明确但**属政策动作，需人批准** | 登记时必须写明待批动作与代价 |
| `DEFER` | 可再生但不可回填（历史计数类），是否纳入保护面**待定** | 登记时必须写明两套 T0 口径差异 |
| `INFO` | 弱证据/自引用，**不算问题**，登记只为固化口径 | — |

## 4. 复跑与验收

```
python3 tools/memory_asset_durability.py scan      # 判洞；有未登记洞 ⇒ rc=1
python3 tools/memory_asset_durability.py proof     # 只读自证（跑前后 sha 不变）
python3 tools/memory_asset_durability.py selftest  # 24 用例（含 NC1–NC18 + 正控）
```

- **A/B 判据**：把登记面置空（`--registry <空 JSON>`）必须**回落到全部洞**（R507 实测 rc=1）——证明「归零」是登记面的功劳，不是判据失效。
- **fail-closed**：登记面读不动（坏 JSON）或形状错（非对象）⇒ `rc=2` 硬停，**绝不当作空登记面**继续判（NC17/NC18）。加此守卫的原因：坏文件曾让脚本崩在 `json.load` 上，错误路径是「崩溃」而非「静默放过」，但把两种失败都收敛成显式 rc=2 更安全。

## 5. 边界（本政策不做什么）

- **不替代 R418 的克隆可得性审计**：R418 判「被引路径随 clone 是否可得」，本器判「**未入库但有消费者**的 memory 资产是否有意为之」。
- **不代替 T0 保护面**：`DECLARED_LOCAL_ONLY` 的前提是 T0 双覆盖，T0 清单本身由 `tools/t0_snapshot.py` 与 `temp/t0_baseline.sha256` 维护（两套定义差异见 R461）。
- 只读：本器不删、不改、不入库任何资产；`PENDING_APPROVAL` 条目的修法**一律待人批准**。

## 6. 当前待批（2026-09-29 快照）

1. `memory/global_mem.txt`（L2 层唯一副本、无 git 历史）：入库 or 私有备份 —— 即 R418 的 D1。
2. `memory/history.txt`（231 B）：`git add -f` 入库即可闭环。

> 两条均 `revers=0`（可逆），但触及「memory 入库边界」政策面，故不自动执行。
