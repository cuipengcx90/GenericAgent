# release-marshal 无人值守 SOP（L3）

> 项目内技能，权威文件在 bar-smyze 仓库，本 SOP 只记「怎么驱动 + 已验证行为 + 当前闭环边界」，不复制正文。
> 仓库根: `/Users/cuip/develop/WorkSpaces/idea/smyze/bar-smyze`
> 权威: `skills/release-marshal/SKILL.md`（阶段序列/门禁 G1-G22 唯一真源）；冲突以 SKILL.md + 代码为准，文档次之。

## 三组件（各自纯本地、零外部副作用、决不执行 merge/push/tag/deploy/SQL）
1. `scripts/preflight.sh`：只读预检。文档 `docs/release-marshal-preflight.md`。
2. `scripts/state_controller.py`：本地审批+反馈状态机（Stage 7.5 监控原语）。文档 `docs/release-marshal-state-controller.md`。
   - 子命令: validate-monitor-config / request-approval / resolve-approval / enqueue-feedback。
   - monitor-config.json 默认 `status=DISABLED`，必须人工确认五项(调度方式ScheduleWakeup/监听群chat_ids/时间预算/quota/执行人+拍板人)后手改 `ARMED` 才处理反馈(G13，每次不得沿用旧值)。
   - 反馈只入队 `RECEIVED_UNTRUSTED`/`action=NONE`，quota 满即 BLOCKED 转人工绝不静默丢；审批一次性不可跨会话复用。
   - Stage 7.5 自主修复必须留痕，`scripts/verify_autofix_records.sh --check-records` 硬编码 exit1 强制校验(G22)；已知局限：仅覆盖 fix_kind=sql，code 类 hotfix 风险等级仍是 agent 自评。
3. `scripts/release_control.py`：确定性 Stage 状态机（控制面），本次新写。文档 `docs/release-marshal-control-plane.md`。

## release_control.py 契约
- 序列(路径A): `-1→0→1→2→3→4→8→5→6→7→7.5→9→10`（Stage8 复检紧跟 Stage4，早于 SQL/部署）。
- 命令: create / claim(--stage 租约) / submit(--report) / authorize(--grant-file) / attest-deployed(--grant-file --receipt) / status。
- STAGES 字段: requires(前置须 VERIFIED) / required_script(0=scan_scope 1=infer_order 2&8=audit_merge_loss 9=check_sync_gap) / human(3/7/7.5) / irreversible(仅7)。
- submit 机器校验(全过才 VERIFIED): 持租约PA+stage==cursor+release_id匹配+status=PASS+所有commands exit0+required_script有成功记录+artifacts文件真实非空+next_action==REQUEST_STAGE_下一阶段+前置requires全VERIFIED。**PA 绝不能自行推进，推进权在控制面。**
- Stage7 不可逆点: 即便已授权也返回 `BLOCKED_UNIMPLEMENTED_STAGE_7`、`execution=DISABLED_BY_DESIGN`，控制面永不代跑 DDL/部署/上线。
- 状态与 events.jsonl 原子落盘+fcntl 文件锁，多 agent 并发安全。
- 复用 helper: fail/now/read/write(原子tmp+os.replace+fsync)/append/valid(SAFE正则)/rd/locked/event/state。

## 已知坑（真演练中踩到）
- 命令串必须整体过 `shlex.split`；自己拼 `git -C x merge -m "msg with space"` 时**每个参数都要 quote**，否则 `-m` 消息被空格拆开（真实报错：`merge: DBG-1 - not something we can merge`）。
- `submit --report` 传相对路径是相对 **调用者 cwd** 解析，不是 run-dir；跨目录调用一律传绝对路径。
- `RM_WORKDIR` 不设时会在 cwd（= bar-smyze 仓库根）落 `.release-marshal/`，会污染真实仓库；演练务必显式设到 run 目录。
- 该仓库根本来就存在 `.release-marshal/`，判"是否污染"要比对**前后文件集差**，不能只看目录是否存在。

## 已实测行为（2026-09-14，/tmp 隔离 fixture）
- 正向: `-1→…→3(人工授权)→4→8→5→6→7`，Stage 7 授权后正确硬阻断。
- 受控 A 回接: 同一 release 的 `APPROVED` Stage 7 背书 + `SUCCEEDED` 部署回执成功把状态推进至 `WAITING_STAGE_7.5`。
- 反向全部 exit2: 跳序 claim / 冒名 lease / 缺 required_script / 缺产物 / 未授权闯人工 Stage / Stage7 硬阻断 / 失败或跨 release 部署回执 / 重复回接。

## 当前闭环状态（2026-09-21 真演练后修正）
1. **Stage 7 回接入口已实现**: 外部部署完成后使用 `attest-deployed`；控制面核验 release_id、Stage、审批、SUCCEEDED、deployment_id、completed_at、operator_id、evidence，并回到 7.5。控制面仍不执行部署。
2. **✅ 端到端无人值守已真实跑通（推翻旧结论）**: 2026-09-21 用**真执行 PA**（非桩）在 /tmp 隔离 fixture 上完成 `-1→…→10` 全 13 Stage：
   `-1,0,1,2,3`(人工门禁3) → `4,8,5,6,7`(人工门禁7) → attest-deployed → `WAITING_STAGE_7.5` → 授权 → `7.5,9,10` → `RELEASE_COMPLETE`。
   **Stage 9→10 无人工介入连续自动跑完**，Stage 10 产物 `release-summary.json` 含真实 tag/SHA。28/28 断言通过。
   → 旧记录"7.5/9/10 尚未由单一 runner 互联、非端到端无人值守"**已作废**；单一 runner（`release_marshal_runner.py`）确实可驱动全链，此前只是**从未被真执行验证过**。
3. **旧"验证"的真实性质（审计结论）**: 仓库内 `tests/test_release_marshal_runner.py` 用**内联桩 PA**（第 23 行，临时写出、无条件 `status=PASS`），其 `commands` 里直接伪造 `scan_scope.sh`/`infer_order.sh`/`audit_merge_loss.sh`/`check_sync_gap.sh` 的 `exit_code:0`，**从未真的执行过这些脚本**。即：过去"无人值守端到端全绿"= 编排管道的桩通路，非真实执行 → 用户感知的"模拟演练"属实。
4. **演练工具（GA 侧，可复跑）**:
   - `GenericAgent/temp/rm_fixture.py`：造真 git 多仓 fixture（bare origin + master + feat 分支 + release tag），只写 /tmp。
   - `GenericAgent/temp/rm_real_pa.py`：**真执行 PA** —— 真 `git fetch/merge --no-ff/tag`、真跑 4 个门禁脚本；任一命令非 0 即非 0 退出，绝不伪造 PASS。
   - `GenericAgent/temp/drill_rm_unattended.py`：一键全链 + 8 类故障注入（F1 PA 非 0 / F2 越权提交 / F3 报告 stage 不符 / F4 缺 required_script 记录 / F5 lease 冒领 / F5b 非持有者提交 / F6 跨 release 回执 / F6b 跳序认领）。结果：`temp/drill_rm_unattended_results.json`。
5. **仍未固化**: 该演练脚本仍在 GA `temp/`，未落进 bar-smyze 仓库（需用户授权后再提交）；控制面本身仍不执行生产部署/SQL（设计如此）。

## 使用入口
- 小白用户在 GA 对话框输入自然语言“开始 release-marshal 发布”；见项目 `docs/release-marshal-user-guide.md`。
- GA 负责理解、启动、汇报和协调；生产部署仍在外部部署系统/人工授权处完成。
- 部署完成后把回执交给 GA，GA 再调用受控回接并继续 7.5、9、10；任何失败停在原阶段，不猜测、不跳步。
