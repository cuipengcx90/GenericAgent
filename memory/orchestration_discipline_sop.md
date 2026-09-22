# 编排纪律（orchestration_discipline_sop）—— 三铁律 + 五条次律

> 来源：CCB v8.7.1 评估（2026-09-22，`temp/CCB_V8_REVIEW.md`）。结论是**借它的纪律、不借它的产品面/拓扑实现**——所以这份文件只装"规则"，不装它的架构。
> 适用范围：L2 契约 v1、herdr-team / autopilot / release-marshal 的任何"多 agent 干活 → 判定成败"的环节。

## 三条铁律（写在最前，任何编排实现先照着自我检查）

1. **角色产出语义，程序提交权威。**
   agent 的回复只是**提案或证据**；任务状态、依赖、revision、重启、验收、发布、用户可见交付**一律由脚本 + 持久化记录裁决**。
   ⇒ 反面：拿 agent 的自然语言回复当完成信号（"他说做完了"），或让某个 agent 充当"控制层"。
2. **Authority boundary 表（每个输出写明两列 owner）。**
   格式照抄 CCB：`输入/输出 | 语义 owner | 验证与提交 owner`。**任何"状态/拓扑/发布"都不许有 agent 语义 owner。**
   ⇒ 我们已有雏形（L2 的 `task.py` 是权威、teamctl 记 phase）；缺的是把它写成**成文的表**，让每个 skill 有据可依。
3. **mixed outcome 不得降格为 pass。**
   聚合规则（脚本裁决，不是角色修辞）：全 pass → pass；任一 replan → replan；有 partial（或 pass+required blocked）→ partial；全部 required blocked → blocked；**evidence/authority 校验失败 → 不算语义结果，按系统性失败处理**（不许报"完成"）。

## 五条次律（同源，按需引用）

- **exact-once + revision fence + digest 绑定**：同 identity+同 digest 复用原 job；同 identity 冲突正文 **fail closed**；submit 窗口中断后"未知提交状态暂停，不重复提交"。（我们的幂等锁/状态文件是雏形，缺 revision 与 digest 两个字段。）
- **恢复只信 journal，不读 prose 猜完成**：判活优先级 = 状态文件/journal > 进程态 > pane 正文。
  ⇒ 直接对应血泪教训：agent 自报 `working` 而 `read` 失败时不许当健康（G11/G16），必须回状态来源二次取真值。
- **未实现绝不画成已实现**：文档里 ⬜ 就是 ⬜；缺 source/真实证据就不许写成"已交付能力"。（ROOT_MAP 已按此纪律组织。）
- **容量不足必须显式冲突**：起队失败/降规模必须是**显式事件**，禁止静默改成串行、禁止悄悄少起一个成员。
- **诊断三段式**：有界深挖 → 只做受保护/可回滚的恢复 → 改前需批准（`ccb-diagnose` 的形状；我们 `agent_unstick`/`watch`/`escalate` 应向它对齐）。

## 已知会踩的坑（写在这里，避免重犯）

- **桩执行器会伪造成功**：内联/mock 执行器无条件写 PASS，把从未跑过的工具记成 `exit_code=0`。审"测试全绿"必须先验测试本身真执行（RULES 16）。
- **dry-run 不保证无副作用**：我们的 escalate 曾"dry-run 也真写 NEEDS_HUMAN + 真发通知"。
- **自测通知链路必须隔离**：带 `--notify` 跑坏夹具会真发飞书，被误认为生产报警（L2 watch 的 `--state` 隔离）。
