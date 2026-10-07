# R560 海搜方向门：只留大模型 / AI Agent / 应用向

- 日期：2026-10-07　仓库：~/projects/GenericAgent
- 触发：用户原话「剔基础慕课:只留大模型/AI Agent/应用向(剩3条)」
- 一句话结论：R559 的 AI 门只回答「是不是 AI」，不回答「是哪一类 AI」⇒ 基础慕课（Python3 入门人工智能 / 机器学习基础课）照样放行；本批新增**第二道门＝方向门**（只放行大模型 / AI Agent / 应用向），单一源 `tools/ai_topic.py`，四处接线；回放到今天真实命中 **37 条 → 正好剩 3 条**，与用户口径逐字吻合。

## 1 判据设计（方向门 vs AI 门）

- AI 门（R559，`is_ai_topic`，L157）：回答「是否 AI 主题」，严格档，UNKNOWN 一律 False。
- 方向门（本批，`is_direction_ai`，L180；解释器 `direction_explain` L162）：
  - **前向载体词（放行）**：`FORWARD_PHRASES` + `_FWD_ASCII_RX`（L107/L127）＝ 大模型 / LLM / AI Agent / 智能体 / RAG / 多智能体 / 应用开发 / 部署实战 等。
  - **基础向词（拒绝）**：命中「入门 / 零基础 / 机器学习 / 深度学习 / 人工智能」等而**无**前向载体词 ⇒ verdict=`FOUNDATION`。
  - 混合语义走「前向优先」：`小白玩转AI大模型应用开发` 含「小白/入门」但同时命中前沿载体词 ⇒ **保留**（已写进 docstring）。
- 两门串联顺序：`is_relevant`（相关门）→ `is_ai_topic`（AI 门）→ `is_direction_ai`（方向门）。

## 2 修复（单一源 + 四处接线）

| # | 落点 | 位置 | 行为 |
|---|---|---|---|
| 1 | `tools/ai_topic.py` | L180 `is_direction_ai` / L162 `direction_explain` | 方向门单一源（含回退实现供导入失败时用） |
| 2 | `birdie_monitor/haisou_daily.py` | 导入 L19 / 开关 L48 / **词源门 L384-395** / **结果门 L477-478** / 摘要 L545+L551 | 查询侧：非应用向词**不发请求**（`[方向查询门]` 留痕）；结果侧：AI 但基础向 ⇒ `DIR_SKIPPED`，不进推送 |
| 3 | `tools/course_pool.py` | 导入 L323 / 回退 L330 / 选择处 L346 | `pick_for_words` 只挑应用向课程 ⇒ 未来并表不再写入基础慕课词 |
| 4 | `birdie_monitor/sync_quangneng.py` | 导入 L22 / 回退 L29 / 合并处 L141 | 同步合并时同门过滤 + 计数/打印 |

## 3 A/B 复算（回放到今天真实命中上）

- 语料：`temp/bg_task/logs/haisou_run.log`（29149 字节 / 467 行，今早 11:15 那轮），正则 `^【(关键词｜平台)】标题` ⇒ **37 条**。
- 三层串联结果：`rel_drop 0 / ai_drop 29 / dir_drop 5 / keep 3`。
- **keep 3 条**（与用户「剩 3 条」逐字吻合）：
  1. `mktx-hagent-【Harness&Hermes】多智能体开发特训营`
  2. `慕课体系课-【Harness&Hermes】多智能体开发特训营`
  3. `林粒粒：小白玩转AI大模型应用开发 - 20.【加餐】AI应用部署链接`
- **dir_drop 5 条**（AI 门放行、方向门拦下，全为 Python3 入门人工智能系列）：
  1. `Python3入门人工智能，掌握机器学习+深度学习`
  2. `033915_mksz418-Python3入门人工智能，掌握机器学习+深度学习`
  3. `慕课《Python3系统入门人工智能》`
  4. `慕课《Python3系统入门人工智能》：人人都应该学会利用AI这个工具`
  5. `【Python教程】Python3入门机器学习 经典算法与应用 轻松入行人工智能（完整版）`

每条附 `direction_explain` 依据（如 `backward=['人工智能','入门','机器学习','深度学习'] ∧ forward=[]`）⇒ **无理由拒收 0 条**。

## 4 自测与验证证据（当次真跑）

| 项 | 命令 | 结果 |
|---|---|---|
| 判据单一源（AI 门+方向门） | `python3 tools/ai_topic.py --selftest` | `{"checks_ok": 69, "checks_failed": 0}` |
| 海搜主链路（三门接线） | `python3 birdie_monitor/haisou_daily.py --selftest` | `selftest: 52/52 checks passed`（含 `T12 方向查询门:前沿词留存、基础向词不发请求`） |
| 课程池 | `python3 tools/course_pool.py selftest` | `selftest: 84 passed, 0 failed` |
| 方向门实证（三处接线沙盒） | `temp/haisou_direction_gate/pick_evidence.txt` | sync 合并：AI 门剔 1 / 方向门剔 1 / 留 1 且零 NameError；`pick_for_words`：三门各 1 全符合预期 |
| 今日 A/B | `temp/haisou_direction_gate/today37_dual_gate.txt` | 37 条 ⇒ keep 3 / ai_drop 29 / dir_drop 5 |

改动量（`git diff --numstat`）：`tools/ai_topic.py` +123/−0、`birdie_monitor/haisou_daily.py` +102/−2、`tools/course_pool.py` +9/−2、`birdie_monitor/sync_quangneng.py` +27/−0。

## 5 坑与边界（本次实测，全部已修/已隔离）

1. **自测越出沙盒、真跑了生产路径 + 真网络请求**（本批最贵一课）：T12 用例最初写在既有 `finally: 恢复沙盒` 之后 ⇒ 它按**生产**常量真跑一轮，写脏 `haisou_cursor.json`（index 7→9，diff 9/8）、`haisou_seen_items.json`（1/13178）、`haisou_progress.jsonl` 两行。处置=先备份 `temp/haisou_cleanup_backup/20261007-145337/`（三文件）再清理；修为给 T12 **显式沙盒**（重定向 CURSOR/SEEN/PROGRESS + `fetch_quota` 桩），并在 cursor 里留 `note` 说明回滚。
2. **T12 用全新空 state ⇒ 假红**：`run()` 的 `first_run=True` 分支不收集 additions，断言 `kept` 恒空。修为**复用主沙盒 state**（同 R554 的教训：夹具要落在同一沙盒语义内）。
3. **R559 遗留真缺陷：`sync_quangneng.py` 用了 `is_ai_topic` 却从未 import**（改后 L141）⇒ 一旦走到合并分支必 `NameError`，而该文件无 argparse、`--selftest` 会被静默忽略直接真跑（R559 §5 已记）。本批补 import + 方向门，并以伪 HTML 课程走**真合并**断言零 NameError。
4. **CLI 形态差异**：`course_pool.py` 的 selftest 是**位置子命令**（`selftest`，写 `--selftest` 报 unrecognized）；`sync_quangneng.py` 无 CLI（裸跑即真同步）——两者都须先 `grep -n "argparse\|__main__"` 再调用。
5. 边界：方向门只加严不放松（AI 门已放行才轮到它）；`quangneng_keywords.json` / `pool_keywords.json` 的数据面清理属 R559 已交付内容，本批不再改动。

## 6 状态与残留

- 已完成：判据单一源 + 四处接线 + 真实语料 A/B（37→3）+ 三处自测全绿。
- 未接线：方向门**尚无独立定时**（随 haisou 日常链路 11:15 档生效，下一次真跑即带门）。
- 残留：`haisou_cursor.json` / `haisou_seen_items.json` 的工作区改动（含本批自测回滚留痕 `note`）**未提交**，与 R559 数据面一并保持现状，避免吞并在制改动。
