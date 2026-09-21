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

## 已实测行为（2026-09-14，/tmp 隔离 fixture，非仓库内固化测试）
- 正向: `-1→…→3(人工授权)→4→8→5→6→7`，Stage 7 授权后正确硬阻断。
- 受控 A 回接: 同一 release 的 `APPROVED` Stage 7 背书 + `SUCCEEDED` 部署回执成功把状态推进至 `WAITING_STAGE_7.5`。
- 反向全部 exit2: 跳序 claim / 冒名 lease / 缺 required_script / 缺产物 / 未授权闯人工 Stage / Stage7 硬阻断 / 失败或跨 release 部署回执 / 重复回接。

## 当前闭环状态（受控 A，部分闭环）
1. **Stage 7 回接入口已实现**: 外部部署完成后使用 `attest-deployed`；控制面核验 release_id、Stage、审批、SUCCEEDED、deployment_id、completed_at、operator_id、evidence，并回到 7.5。控制面仍不执行部署。
2. **仍未端到端无人值守**: state_controller 的 7.5 监控关闭信号、release_control 的 Stage 9 回流和 Stage 10 总结尚未由仓库内单一 runner 自动互联；当前由 GA 按状态协调。
3. **仓库提交状态需另行核验**: 不把“已验证”误写成“已 commit”；用户原改动 `scan_scope.sh` 勿碰。
4. **测试资产仍需固化**: 当前回接证据来自 `/tmp` 隔离 fixture，仓库内仍需补可复跑测试脚本。

## 使用入口
- 小白用户在 GA 对话框输入自然语言“开始 release-marshal 发布”；见项目 `docs/release-marshal-user-guide.md`。
- GA 负责理解、启动、汇报和协调；生产部署仍在外部部署系统/人工授权处完成。
- 部署完成后把回执交给 GA，GA 再调用受控回接并继续 7.5、9、10；任何失败停在原阶段，不猜测、不跳步。
