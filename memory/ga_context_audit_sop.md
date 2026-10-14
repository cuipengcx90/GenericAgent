# ga_context_audit_sop — GA 上下文占用自查（输入 token 高/Trimmed 频繁/L1 膨胀/定时任务输入大）

## 入口（只读，不写文件；脚本真身在仓库 skills/ 下，不在此重复逻辑）
```bash
python3 skills/ga-context-audit/scripts/ctx_audit.py all
```
子命令：`patches`（补丁是否在源码生效）| `l1`（体积与违规）| `triggers`（反射任务首轮上下文，判断任务间历史是否被携带）| `trim`（硬裁剪比例、平均输入、缓存命中）。

## 判读与处置
- 方法、分层模型、误判清单、已确认问题与上游 issue/PR：`skills/ga-context-audit/SKILL.md` 与 `references/findings.md`，先读再下结论。
- `triggers` 首轮上下文中位应在约 1 万字符内；接近 18 万 ⇒ 反射进程未在补丁后重启或 `FRESH_CONTEXT` 未生效。
- 重启仅限单个进程且确认无任务在跑；改 GA 核心源码须先请示用户；提 issue/PR 等对外动作须先确认。
- L1 里的 SOP 名字是索引能力，不得为压缩 token 而移出或合并 SOP。
