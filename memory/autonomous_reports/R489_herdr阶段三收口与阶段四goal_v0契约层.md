# R489 herdr-agents 阶段三收口 + 阶段四 goal_v0 契约层（合并入 main 并推送）

## 0. 结论

产品仓 `~/develop/herdr-agents` 本轮落两块内容，均已并入 `main` 并推送：

| 项 | 内容 | 提交 | 验收 |
|---|---|---|---|
| A 阶段三收口 | 正式宣告 + 阶段门脚本 + 残余登记 R-11..R-13 | `7d1687e` | `stage3_gate_check.sh` = 12 步 / FAIL 0 |
| B 阶段四 goal_v0 | 目标驱动循环**契约层**：ga.goal.v1 bundle + 22 条硬规则 + 三态退出码；F10 规格 | `c3f657d` | `--selftest` 26/26、`pytest` 29 passed |
| 盖章 | R460 铁律：在**目标仓库内**重建 l1+native 后重盖章 | `a55ca58` | `derive_guard check` rc=0（OK 4 / MANUAL 2） |

`origin/main = a55ca58`（已 push）。主仓另一活跃会话的 20 条在制改动**批前批后均 20 条**，未被吞（见 §4 三方合并法）。

## 1. A 阶段三收口

- 宣告文件 + 阶段门脚本 `control-plane/stage3_gate_check.sh`（12 步）：真跑 **steps:12 FAIL:0**。
- `docs/product/ROADMAP_MASTER.md §8.1` 残余登记补 **R-11..R-13**，每条含「触发时怎么发现」= 探针路径或显式人工。
- 残余登记门禁 `tools/residual_registry_check.py` 支持**多宣告并集**（阶段二 `STAGE2_DECLARATION.md` + 阶段三宣告），双向覆盖：宣告逐条点名 ∧ 不得点名不存在。
- `ROADMAP_MASTER.md` / `ROOT_MAP.md` 阶段三状态由 ⬜ 翻 ✅（状态块由 `tools/doc_state_render.py` 单一源生成，禁手写）。

## 2. B 阶段四 goal_v0 契约层

- 实现：`control-plane/goal_v0/goal.py`（454 行）+ `test_goal.py`（334 行）。
- 规格：`docs/product/F10_GOAL_LOOP_SPEC.md`（131 行，含 `## Authority boundary` 段，过 `authority_boundary_check.py`）。
- 契约要点：schema = `ga.goal.v1` bundle；**22 条硬规则**；**三态退出码**；schema 不认识 ⇒ rc=2（fail-closed）。
- 证据：`--selftest` **26/26**；`pytest` **29 passed**；F10 表格内 22 条规则码与 `goal.py` 的 RULES 逐字机械核对一致。
- `control-plane/README.md` 增 goal_v0 一行（套件表）。

## 3. 仓级验收（主仓真身）

| 门禁 | 结果 |
|---|---|
| `derive_guard.py check` | rc=0（native_tree / board_html / trust / l1_index 全 OK） |
| `doc_state_render.py --check` | rc=0（ROOT_MAP 状态块与 ROADMAP §7.1 单一源一致） |
| `tools/authority_boundary_check.py` | rc=0 |
| `tools/residual_registry_check.py` | rc=0 |
| 全阶段门 `control-plane/stage_gate_check.sh`（隔离 worktree） | steps: **22**　FAIL: **1**（其中需本地运行态：1）　PASS: 21 |

唯一 FAIL = 步骤 7b「漂移闸门真机 check（本仓当前状态）」，脚本自带标注 `［需本地运行态：runs/ 被 gitignore］`：根因是 native 树 README 内嵌 `repo: <绝对路径>` = **仓库局部口径**，在隔离 worktree 里必报假 DRIFT；同一闸门在**主仓真身**跑 rc=0 ⇒ 非代码缺陷（与 R485 同一结论，本轮二次复现）。

## 4. 方法学：主仓被占时的无损并入（本轮最值钱的一条）

背景：主仓 `main` 有另一活跃会话的 20 条在制改动（含 `docs/product/ROADMAP_MASTER.md` 与我同文件），`ff-only` 被挡。

处置（可复用）：
1. 逐文件比对「我的改动文件」×「他在制文件」，只 2 文件重叠（`ROADMAP_MASTER.md` 12 行、`LOOPX_DEVDOC_SYNC_v1.md` 8 行）；
2. 备份在制文件到 `/tmp/rmerge/`；
3. `git merge-file`（current=**他的工作区版**，base=`HEAD`，other=**我的版本**）三方合并 ⇒ 双方 hunk 不相邻时无损；
4. 校验：他在制改动行数逐字保持、我的新增全在；
5. 提交只看 staged 内容，不带 pathspec（RULES#19）⇒ 提交后 `git status` 仍显示他的 20 条。

盖章铁律（R460 复现）：`derive_guard` 的 native_tree 期望含 `tools/native_plugins.py` 生成的 README，其内容内嵌仓库绝对路径 ⇒ worktree 内盖章的 manifest 在主仓必报 DRIFT。**必须在目标仓库内** `python3 tools/native_plugins.py build` → `cd control-plane/l1_map_v0 && python3 l1.py build`（输入面 197→199）→ `derive_guard.py stamp` 再 check rc=0。

## 5. 边界与残余

- goal_v0 是**契约层**（零生产调用点），未接线到 autopilot/driver：属设计分工，非缺陷。
- 阶段四其余工作项（目标驱动循环的生产接线、验证面）尚未开工。
- 阶段门 7b 在隔离 worktree 恒假红（脚本自身标注需本地运行态）⇒ 以后「套件/阶段门在 worktree、真机 check 回主仓」是标准两全做法。

## 6. 数字出处

- 提交与 HEAD：`git log --oneline -4`、`git rev-parse HEAD origin/main`（= `a55ca58`）。
- 阶段门：`/tmp/sg_stage34.log`（`steps: 22   FAIL: 1（其中需本地运行态：1）`；`PASS` 21 行 / `FAIL` 1 行）。
- 行数：`wc -l` 于 goal.py / test_goal.py / F10 规格。
- 套件：goal_v0 `--selftest` 26/26、`pytest` 29 passed；全仓 `PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh`（隔离 worktree）= `suites run: 31` / `ALL SUITES PASSED` / `drills: 8 passed, 0 failed`（日志 `/tmp/r_stage34_suites.log`；R485 基线 30 套件，本批 +goal_v0）。
