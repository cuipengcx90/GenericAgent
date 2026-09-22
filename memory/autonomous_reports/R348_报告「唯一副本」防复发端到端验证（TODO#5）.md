# R348 报告自主报告「唯一副本」防复发 —— 沙箱端到端验证 + 真树负控

> 任务来源：TODO#5（灾备）「报告『唯一副本』防复发闭环端到端验证」（R336 评审推荐·遗漏2）
> 结论层级：**真树负控已证「双写未生效」；可用补丁（C2/C3/C4）已在沙箱端到端跑通并字节级回滚验证**——但真树落地仍需用户批准（改 memory 下文件属 SOP「需写入报告待审」）。

## 0. TL;DR
1. **现状=风险仍在**：`temp/autonomous_reports/` 至今是唯一副本（且 temp 无版本控制）；`helper.complete_task()` 没有任何 memory 写入；scheduler 的 12h L4 归档也不碰自主报告；SOP 收尾 4 件事里也没有归档步骤。
2. **不是又写一条提案**：本轮把 R336 的 C2/C3/C4 三条补丁**真跑了一遍**——在沙箱里 apply → 真调用 `complete_task()` → 镜像副本出现且 **sha256 与源报告字节一致**；负控（未打补丁）镜像**不存在**；回滚 3/3 字节级校验通过。
3. **补丁即用**：`python3 temp/pending_decisions/apply.py --only C2,C3,C4 --apply`（dry-run 去掉 `--apply`），回滚一条命令；补丁全文见附录 A（因为补丁产物本身在 temp/，故在此留档防丢）。

## 1. 验收项与结果

| # | 验收要求 | 结果 | 证据 |
|---|---|---|---|
| ① | 一次真实 `complete_task` 后 `memory/autonomous_reports/` 出现同内容副本 | **不成立（预期内的坏消息）** | §2.3 真树即时检查：T+0s 镜像缺席 |
| ② | 若未生效，给出可直接 apply 的最小补丁（含回滚） | **已交付且已沙箱验证** | §3 表格 + §4 命令 + 附录 A 全文 |
| ③ | 零副作用（真树 git diff 有据） | **成立** | 沙箱隔离执行（`--root` + 假通知模块）；真树本轮仅新增报告/归档，`git status` 见 §5 |

## 2. 真树负控证据（① 未生效）

### 2.1 静态证据
- `memory/autonomous_operation_sop/helper.py`（sha256 `06a4987333c2d246c0382d6ec67f020454f951784e06cf731e9dc45fe1219d14`，191 行）中 **grep 不到任何** `memory/autonomous_reports` 写入；唯一与 memory 相关的只有 `compress_and_archive()`（压日志）。
- `reflect/scheduler.py:88-99` 的 L4 归档 cron 只把 `temp/model_responses` 压进 `memory/L4_raw_sessions`，**不调用** `tools/autonomous_reports_archive.py sync`。
- `memory/autonomous_operation_sop.md` 收尾 5 步（line 42-47）**无归档/sync 步骤**；讽刺的是 SOP line 3 自己就警告「报告在 temp/ 下」——即系统性风险被写在文档里但没人守。

### 2.2 沙箱负控（同一代码、独立目录、真调用）
未打补丁的 helper 在沙箱里真跑 `complete_task()`：
- 源报告 → `.../temp/autonomous_reports/R1_sandboxctl.md`（存在）
- 镜像 → `.../memory/autonomous_reports/R1_sandboxctl.md`（**不存在**）
⇒ 双写确实未实现（不是「实现了但路径不同」）。

### 2.3 真树即时检查（本次收尾自身）
本报告就是通过真树 `complete_task()` 收尾的；在其返回后**立刻**（尚未执行归档 sync 之前）检查：
- 检查时间：T+0s（与 complete_task 同一脚本内，无中间步骤）
- `memory/autonomous_reports/R348_*.md` 是否存在 → **不存在**（`glob` 返回空列表）⇒ **双写未生效**，与 §2.2 沙箱负控完全一致

> 说明：`memory/autonomous_reports/` 里确实存在 R344–R347 —— 那是**我每轮收尾手动跑 `sync --apply`** 的结果，属于「人肉补丁」，不是 `complete_task()` 自动行为，也正是 C2/C3/C4 要消灭的依赖。

## 3. 沙箱端到端实验（证明补丁有效）

方法：
1. `temp/pending_decisions/sandbox_r348/` = 复制真树的 `memory/autonomous_operation_sop/helper.py`、`reflect/scheduler.py`、`memory/autonomous_operation_sop.md`。
2. 用现成装置打补丁：`apply.py --root sandbox_r348 --only C2,C3,C4 --apply`（dry-run 复跑确认 3 条锚点仍**唯一命中**）。
3. 隔离：helper 的模块级常量（`_REPORTS_DIR/_HISTORY_FILE/_MEMORY_DIR/_TODO_FILE`）全部重定向到沙箱；`notify_feishu_group`/`notify_dingtalk_group` 预置为**假模块**（只计数不发网）。
4. 负控与实验组用**同一份报告内容**（仅文内标签不同）。

| 组 | 打过补丁 | dest 报告 | memory 镜像 | 镜像 sha == 源 sha |
|---|---|---|---|---|
| 负控 | ✗ | 存在 | **不存在** | False |
| 实验组 | ✓ | 存在 | **存在** | **True**（`10d1e418…`） |

其它验证：
- 补丁后 `helper.py` / `scheduler.py` 均 `ast.parse` 语法通过（另：apply 日志显示 helper 192→202 行、scheduler 193→202 行、SOP 55→56 行，与预期一致）。
- `apply.py --rollback 20260922-115223 --root sandbox_r348` → **3/3 项字节级还原**，helper sha 回到 `06a4987…`（补丁可逆）。
- 通知计数=4 次调用全部落在假模块上 ⇒ 本实验**零外部副作用**。

## 4. 可直接 apply 的最小补丁

```bash
# 预览（默认 dry-run，不改任何文件）
python3 temp/pending_decisions/apply.py --only C2,C3,C4
# 落地
python3 temp/pending_decisions/apply.py --only C2,C3,C4 --apply
# 回滚（ts 见 apply 输出；沙箱验证用 20260922-115223）
python3 temp/pending_decisions/apply.py --rollback <ts>
```

| id | 动作 | 目标 | 行数变化 |
|---|---|---|---|
| C2 | insert | `memory/autonomous_operation_sop/helper.py`（complete_task 内新增报告镜像到 memory/ + 拷贝 history） | 192→202 |
| C3 | append | `reflect/scheduler.py`（L4 12h 归档顺带 `sync` 自主报告） | 193→202 |
| C4 | insert | `memory/autonomous_operation_sop.md`（SOP 收尾加第 6 步「完稿后 sync 入库」） | 55→56 |

三者的分工正好覆盖三个失守点：**写入时（C2 立即双写）→ 巡检时（C3 兜底）→ 流程上（C4 不再靠人记）**。

## 5. 残余风险与边界（勿把沙箱结论当真树结论）
1. **C3 未做真实定时触发测试**：只证明「锚点唯一命中 + 补丁后能编译 + 它调用的命令在真树手动跑过成功」。真实 L4 cron 触发（12h 一次）需等下一次自然到点或人工强制，本轮未做（避免扰动常驻调度器）。
2. **沙箱 ≠ 真树**：真树落地会改 `memory/` 下文件 ⇒ 按 SOP「权限边界」属**需批准**项，本轮**未**在真树 apply。
3. **补丁产物本身在 temp/**（`actions.py`/`apply.py`/`sandbox_r348_verify.py`）：若 temp 再次被清，提案会连同一起丢 —— 因此本报告附录 A 内嵌补丁全文，报告一旦归档入库即具备可重建性（自指闭环）。
4. 镜像语义边界：C2 镜像的是**报告 + history**，不含 TODO/reflections；属「防单点丢失」的最小集，不做全量备份。

## 6. 记忆更新建议（待审，未落盘）
- L1 建议追加一句（极简）：`自主报告防丢失：helper.complete_task 尚无 memory 双写，补丁 C2/C3/C4 已沙箱验证待批（见 temp/pending_decisions，apply.py --only C2,C3,C4）`。
- 批准路径：apply 后建议把 `apply.py`/`actions.py` 从 temp 迁入 `tools/pending_decisions/`（受版本控制），否则提案永远睡在易失目录。

## 附录 A：C2/C3/C4 补丁预览全文（apply.py dry-run 输出，逐字）

```diff
--- C2 预览 ---
--- a/memory/autonomous_operation_sop/helper.py
+++ b/memory/autonomous_operation_sop/helper.py
@@ -127,4 +127,14 @@
         return f"[ERROR] 移动报告失败: {e}"
 
+    # ── 1.5 镜像到 memory（R336 §4-A 提案）──
+    # 报告的唯一副本不得只存在于 temp/（temp 在 .gitignore 内，曾整体丢失）
+    try:
+        _mirror = Path(__file__).resolve().parent.parent / "autonomous_reports"
+        _mirror.mkdir(parents=True, exist_ok=True)
+        shutil.copy2(dest_path, _mirror / dest_name)
+        if _HISTORY_FILE.exists():
+            shutil.copy2(_HISTORY_FILE, _mirror / "history.md")
+    except Exception as e:
+        print(f"[WARN] 报告镜像到 memory 失败（不影响主流程）: {e}")
     # ── 2. prepend history ──
     # 自动加编号 + 日期（剥离 agent 可能已写的编号/日期，统一重建）

--- C3 预览 ---
--- a/reflect/scheduler.py
+++ b/reflect/scheduler.py
@@ -96,4 +96,13 @@
             r = batch_process(raw_dir, dry_run=False)
             print(f'[L4 cron] {r}')
+            # R336 §4-B 提案：L4 归档顺带同步自主报告（幂等、静默、失败只记日志）
+            try:
+                import sys as _sys, subprocess as _sp
+                _sp.run([_sys.executable, os.path.join(_dir, '..', 'tools',
+                         'autonomous_reports_archive.py'), 'sync', '--apply'],
+                        capture_output=True, timeout=120)
+            except Exception as e:
+                _logger.error(f'[L4 cron] reports sync failed: {e}')
+
         except Exception as e:
             _logger.error(f'L4 archive failed: {e}')

--- C4 预览 ---
--- a/memory/autonomous_operation_sop.md
+++ b/memory/autonomous_operation_sop.md
@@ -46,4 +46,5 @@
 4. `set_todo()` 获取TODO路径 → 将已完成条目标记为 `[x]`（注意前缀）
 5. 结束，剩余TODO留到下次再做
+6. 报告入库：`python3 tools/autonomous_reports_archive.py sync --apply`（必要时再 `inventory --apply` 重建 INDEX/history）——**报告唯一副本不得只存在于 `temp/`（.gitignore 内，2026-05~09 曾整体丢失）**
 
 ## 权限边界

===== 汇总（root=/Users/cuip/projects/GenericAgent，模式=DRY-RUN）=====
C2   OK  helper.complete_task 报告双写 memory（R336 §4-A） | 锚点唯一命中；memory/autonomous_operation_sop/helper.py 行数 192→202
C3   OK  scheduler L4 归档顺带同步自主报告（R336 §4-B） | 锚点唯一命中；reflect/scheduler.py 行数 193→202
C4   OK  自主SOP 收尾加第 6 步：完稿后 sync 入库（R336 §4-C） | 锚点唯一命中；memory/autonomous_operation_sop.md 行数 55→56
OK=3 ALREADY=0 SKIP(manual)=0 FAIL=0
```

## 附录 B：复现方式
```bash
cd /Users/cuip/projects/GenericAgent
python3 temp/pending_decisions/sandbox_r348_verify.py   # 输出 JSON：control/patched/rollback 三段结论
```
脚本要点：假通知模块 → 沙箱常量重定向 → 负控与实验组各真跑一次 `complete_task()` → 比对 sha → 回滚并复校 sha。


---

## 附录 C：记忆更新建议（SOP 要求置于报告末尾）

> SOP 收尾第 2 条原文：「若有记忆更新建议，**附在报告末尾**」——本节置于文末以满足该要求（§6 的同名内容在上方，两处一致）。

1. **L1 建议（待审，未落盘）**：`自主报告防丢失：helper.complete_task 尚无 memory 双写（真树 T+0s 已证）；补丁 C2/C3/C4 沙箱端到端验证通过待批——python3 temp/pending_decisions/apply.py --only C2,C3,C4`
2. **L1 建议（待审，未落盘）**：`helper.compress_and_archive() 引用的 temp/compress_log.py 全仓库不存在 ⇒ 每次 complete_task 都打 [WARN]，日志压缩从未生效（欠账，非本轮任务范围）`
3. **流程建议**：批准 C2/C3/C4 时，把 `temp/pending_decisions/{apply.py,actions.py}` 迁入 `tools/pending_decisions/`（受版本控制），否则提案永远睡在易失的 temp/。
