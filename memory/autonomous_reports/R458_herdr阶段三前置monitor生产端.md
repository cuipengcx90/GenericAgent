# R458 · herdr-agents 阶段三前置：`task_class=monitor` 生产端（看板 Monitoring 列不再为空）

- 日期：2026-09-28
- 任务来源：TODO（第19批 C1，三路评审 codex/槽3/槽4 = 8/8.5/6 = 7.50，含收窄条件）
- 落点：`herdr-agents`（control-plane/monitor_v0/ 新套件 + 看板投影接线 + F7 规格）
- 提交：`013f58a`（11 文件 1323+/86−）、`c7add19`（2 文件 14+/14−）——均已 push `main`

## 1. 结论（一句话）

FUTURE_CAPABILITIES §6 里的「**字段与消费端就绪、但全仓零生产者**」这一缺口被补上：
新增唯一写者 `control-plane/monitor_v0/monitor.py`（声明表 → `scan` 落账 → `board.py --extra-runs` 投影），
现役 3 个周期性任务被挂成契约记录，**看板 Monitoring 列实测 3 条**（判据从「永不可能为真」变成可机械复跑）。
本批**不做**：探针只到 `file_mtime`（无 http/命令行探针）、无自我调度、告警消费者未接、不改 `alerts_enabled` 语义。

## 2. 交付物

| 文件 | 规模 | 说明 |
| --- | --- | --- |
| `control-plane/monitor_v0/monitor.py` | 547 行 | 唯一写者：`scan`（只读探针 + 程序化落账）/ `list`（只读 + 读侧契约校验），`--selftest` |
| `control-plane/monitor_v0/monitors.json` | 39 行 | 声明表 `ga.monitor-decl/v1`：3 个监控位（`l2_gate` PT30M / `price_alert` PT2M / `autonomous_reflect` PT30M），`${VAR}` 可展开 |
| `control-plane/monitor_v0/test_monitor.py` | 399 行 | pytest 31 passed（含 8 类负控用例） |
| `docs/product/F7_MONITOR_PRODUCER_SPEC.md` | 119 行 | 规格：目标/非目标、契约字段、判据、残余 |
| `control-plane/autonomy_v0/board.py` | +28/−4 | `--extra-runs DIR`（可重复）+ 记录级 `_runs_dir`（同 `run_id` 先到先得、跳过并打 stderr，绝不静默覆盖） |
| `control-plane/autonomy_v0/contract.py` | +6/−0 | `rollup` 排除 `task_class=monitor`（键集逐字不变；monitor 设计上永不终态，混入只会把 `open` 永久抬高、稀释完成率分母） |
| `control-plane/derive_guard_v0/targets.json` | +2/−1 | `board.html` 输入面加 `control-plane/monitor_v0/runs/**` |

`control-plane/autonomy_v0/board.html` 为上面投影层的重渲染产物（走输入哈希，不走字节比对）。

## 3. 验收证据（原始输出）

### ① 生产端自测与套件

```
[monitor] selftest: 40 passed / 0 failed     # monitor.py --selftest ⇒ rc=0
31 passed in 0.30s                           # cd control-plane/monitor_v0 && python3 -m pytest -q
```

### ② 真跑 scan（本机现役 3 个监控位）

```
[monitor] .../control-plane/monitor_v0/monitors.json  monitors=3 written=3 unchanged=0 probe_ok=3 missing=0 overdue=0
  - l2_gate                lane=Monitoring probe=ok      age=556.5s cadence=PT30M
  - price_alert            lane=Monitoring probe=ok      age=39.7s cadence=PT2M
  - autonomous_reflect     lane=Monitoring probe=ok      age=0.3s cadence=PT30M
```

### ③ 看板真显示（判据「Monitoring 列 ≥1 条」）

```
[extra-runs] ../monitor_v0/runs → 本次载入 3 条
已生成 .../control-plane/autonomy_v0/board.html（17 条记录：真装置 9 / 模拟 5）
分列：等待人 7 · 阻塞 0 · 待复核 1 · 可跑 0 · 已认领 1 · 观察中 3 · 完成 5
```

对生成的 HTML 按列切片计数：`Monitoring` 列卡片数 = **3**。

### ④ 负控（两条，均可证伪）

- **声明侧**：删掉一条声明的 `cadence` 后 `scan` ⇒ `[monitor] 错误：monitors[0] 缺必填键 cadence`，**rc=2**（拒收，不静默补默认）。
- **记录侧（读侧同守契约）**：复制一条记录并删 `cadence`，`board.py --extra-runs <dir>` ⇒ **警示区 1 条**，且该记录**不进 Monitoring 列**（该列卡片数 = 0，非 1）。
  `monitor.py list` 对同一篡改 ⇒ `rc=3`（`RC_CONTRACT`）且输出含 `INVALID`；还原后 `invalid=0 / rc=0`，全程零写入。
  `monitor.py:58` 常量：`RC_OK, RC_FAIL, RC_CONFIG, RC_CONTRACT = 0, 1, 2, 3`。

## 4. 门禁（提交前后均复跑）

```
run_tests.sh：25 ALL SUITES PASSED（monitor_v0 段 = 31 passed；日志 temp/bg_task/logs/r458.log）
derive_guard check：汇总 MANUAL=2 OK=4   RC=0
   [OK] native_tree 118 文件逐字节一致 / [OK] l1_index 187 输入未变 / [OK] board_html 63 输入未变 / [OK] trust_gate_copies 2/2
stage_gate_check.sh：steps: 22  FAIL: 0   STAGE-GATE: no FAIL in this repo   RC=0
```

## 5. 踩到的坑（本批新增，值得进记忆）

1. **`scan` 会移动 `board_html` 的输入面 ⇒ 必须同批「重渲染 + 重新盖章」**：`targets.json` 把 `monitor_v0/runs/**` 加进 `board.html` 输入后，任何一次 `scan`（记录里含 `ts`/`age`）都会让 `derive_guard check` 报
   `[RESYNC] board_html 产物与输入同时变化 ⇒ 未经验证，须重新盖章（stamp）`。规范姿势 = `monitor.py scan` → `board.py --extra-runs ...` → `derive_guard stamp --only board_html`。
2. **`l1_index` 的输入面含 `control-plane/monitor_v0/monitor.py`**（`l1_map_v0` 会索引 control-plane 源码）：新增/改动 monitor 源码后同样触发 `RESYNC`，须 `cd control-plane/l1_map_v0 && python3 l1.py build` 再 `stamp --only l1_index`。**只 stamp 不 build 不成立**（RESYNC 语义就是产物与输入都变了）。
3. **README 的测试计数会陈旧**：`control-plane/README.md` 行内写 `30/30`，而套件此时实为 `31 passed`（补了读侧契约用例）⇒ 交付前用实跑数字回读覆盖（RULES#21 同类）。
4. **共享仓提交用临时索引**：本仓真实索引里有他会话 28 条已暂存项（`acceptance_v1/runs/*`、`autonomy_v0/runs/*`），直接 `git commit` 会把它们一并带上；
   用 `GIT_INDEX_FILE=/tmp/...` + `git read-tree HEAD` + `git add -A -- <我的 9 个路径>` + **不带 pathspec** 的 `git commit`，提交后再 `git add -- <同路径>` 把真实索引同步到 HEAD（否则那批路径在真实索引里是「缺失」⇒ 他会话 `commit -a` 会从 HEAD 抹掉它们）。

## 6. 残余与边界（未做，明说）

- 探针只有 `file_mtime`（`signal_fresh:`）：http 探针、命令行探针未做 ⇒ 对「进程活着但信号不写」的场景不可见。
- **无自我调度**：谁按 `cadence` 去跑 `scan` 由外部决定，本器**不注册定时器**（也不做 launchd/cron 接线）。
- 告警消费者未接：超期只在看板/`list` 里体现为 `overdue`，不通知、不升级、不重启（`board_policy.json` 的 `alerts_enabled=false` 语义不变）。
- `board.html` 与 `monitor_v0/runs/**` 是「产物 vs 易变输入」的组合 ⇒ 每次 `scan` 后需重复第 5 节的 3 步；未做成单条原子命令（属下一批候选的接线面）。

## 7. 与既有资产的边界（避免重复劳动）

- 与 `derive_guard`：本批只**扩输入面**与**重新盖章**，不改闸门判定逻辑。
- 与 `E4/E5/F6`：不动准入、配额、无人值守三档；只加「看板有一列能显示无主长程任务」的生产端。
- 与 `l2_watch.py`：本器**不消费**、不巡检 L2；`l2_gate` 只是被挂上来的一个监控位（见证文件 = `${GA_ROOT}/.ga/l2/index.json`）。
