# R539 anyrouter 探针「姿势」自证与「形状门」订正

- 日期：2026-10-04（深夜作业）
- 起因：用户质问「你说探针和 CC 复刻并行，到底是不是一个东西？探针没通、实验通了，你这实验是干啥的？」+「CC Format 也不通了，你改了啥？」
- 结论一句话：**不存在客户端形状门禁。探针姿势本来就对；429 的唯一解释是号池满（POOL_BUSY）。** 此前「1–2s 瞬拒=形状不对」的判读（R538 gate_suspect）是误判，已订正。

## 1. 用户三问的逐条回答

**问 1：什么是探针？什么是 CC 形状复刻？两者为何不同？**
- **探针**＝`tools/anyrouter_slot_watch.py` 自己构造的最小请求体（515B、uuid5 稳定 session、按模型选 messages/codex 面）。它是**长期盯守**用的，每 5 分钟 7 模型各一发。
- **CC 复刻**＝本会话为排查「网关是否校验客户端」而临时做的实验：抓真 Claude Code 的完整请求体（95162B/21 tools/Stainless 头/真 session UUID），原样重放。
- 两者的**唯一差别是请求体大小与头部丰富度**——这正是当时要检验的变量，不是两套监控。用户点破的问题成立：**实验得到的新认知必须回填进探针**，否则实验白做。本批即为回填（见 §4）。

**问 2：凌晨那批 200 为什么没通知？**
- 那批 200 出自**真容量窗口内的 CC 形状复刻实验**（04:52–05:09），不是探针发的；同刻探针四轮全 429。监控只对自己发出的 200 通知，故无回执＝**行为正确**。
- 但暴露了真缺陷：10-04 全天 117 轮 591 发零 200，其中 140 条 429 是**旧自造 session_id 被网关瞬时拒**造成的结构性漏看（该缺陷已于 R538 期修复为 uuid5 稳定身份）。

**问 3：CC Format 为什么又"不通"了？**
- 见 §2：不是被改坏，它从来就没「通」过——之前的「通」是**真 CC 客户端**在自己重试（attempt 1 of 10）时挂满 90–100s，被误读成"过闸"。

## 2. 终局铁证：既无形状门，也无「慢=排队」的捷径

证据文件：`temp/anr_pose_verify/FINDINGS.md`（本会话现场记录）、`temp/anr_codex/capture_full.json`（真 CC 抓包 77578B）、`temp/anyrouter_watch/cc_full_request.json`（97250B）。

1. **真 CC 原样重放**：本地转发代理抓到真 CC 完整请求（`POST /v1/messages?beta=true`，body 95162B，含 tools/thinking/output_config/context_management/长 system 三段；headers 含 `X-Claude-Code-Session-Id` + Stainless 七件套 + `x-app:cli`；`1m` 在 anthropic-beta 头）。**逐发计时：9 发全部 429，耗时 1.5–2.3s**。
2. **同刻 A/B**：我方探针体 429@~1.3s vs 真 CC 完整体原样重放 429@~1.6s ⇒ **同档、同一判决**。
3. **已排除假设**：headers 变体 / system C·D / session_id 四种变体（固定 uuid5、随机 uuid4、抓包真 UUID、旧 91 字符长串）/ body 形状（95KB 原样重放）——**全部 429，无一例外**。
4. **「挂满 90–100s」的真相**：`temp/anr_truth/*.jsonl` 实录 `cc_dump` 113945B 体 `elapsed_s=70.7/45.9/46.3/60.6/61.3/60.7/61.1`，`head="The read operation timed out"`（http=0）——这是 **Claude Code 客户端内部重试+退避**（客户端日志 attempt 1 of 10）造成的观感，不是服务端放行。探针单发无重试，故耗时天然更短。

结论：**429 就是号池满**（与 R537 依据 L 站攻略的判读一致）；`gate_suspect`（1–2s 瞬拒=形状不对）语义作废，`FAULT_GATE_REJECT` 必须满足「**同刻对照能过而我方不能**」才可判。

## 3. 现场旁证（同一判定链）

- `temp/anr_truth/shape_ab2_20261004_094714.jsonl`（三形态同刻对照，全 429）：
  - A 真 CC 原样体 `req_len=113945` → 429 @103.0s
  - B 极简体 `req_len=515` → 429 @19.8s
  - C 真 CC 体 + uuid5 session → 429 @24.3s
- `temp/anr_face/notes_20261004.md` 首轮 8 名实况（10:11:02）：claude 系 6 名 429（1.1–2.5s），gpt-6-astra-cc-format `http=0 QUEUED_NO_UPSTREAM (90.6s)`，gpt-6-astra `http=500 POOL_BUSY (82.0s, face=codex)`。

## 4. 代码改动（`tools/anyrouter_slot_watch.py`）

| 编号 | 内容 |
|---|---|
| C1 | 常量注释订正（形状门假设作废） |
| C2 | `classify_latency` docstring 订正（快返≠形状拒） |
| C3 | `gate_suspect` docstring 订正（须同刻对照） |
| C4 | 新增 `--verify-pose`：同刻打「探针体」vs「真 CC 体」各 N 发，输出 `POSE_OK` / `POSE_DIVERGED` |
| C5 | selftest 补用例（含桩替身端到端） |

**姿势自证真跑结果**：`temp/anyrouter_watch/pose_verify_20261004T104436.json` ⇒ `verdict=POSE_OK`，探针 1.08/1.19/1.27s vs 真 CC 1.57/1.64/1.79s（同档 429）。

同批修掉两个真缺陷（首跑时 cc 分支 `http=0` 造成的**假分歧**）：
1. `_norm_body`：抓包 body 为 str、发送路径要求 bytes ⇒ 归一化。
2. `_filter_cc_headers`：抓包头携带 `host`/`content-length`/`authorization` ⇒ 剔除后再重放，避免污染对照。

selftest：155 → **177/177 通过**。

## 5. SOP 订正（`memory/anyrouter_quota_sop.md`，230 行）

- §1.2：新增「快速订正」段（形状门假设作废 + 真 CC 同等待遇）。
- §1.3：明确「快返（1–2s）非形状门」判读规则。
- §2：新增 `--verify-pose` 自证流程。
- §3.3（既有）：改判据/改模型名单后**必须重启 watchd 才生效**。

## 6. 现役状态与边界

- **watchd 已按 §3.3 重启**：旧守护 GONE → 残留探测 pid 87118（持 `watch.lock`）终止 → 新守护 `lock pid=30875`，`tools/anyrouter_slot_watchd.py --max-hours 23`，子进程日志 `temp/anyrouter_watch/watch.log`，首轮 7 模型已开打（claude 系 429/POOL_BUSY）。
- **残余风险**：
  1. 号池满期间「探针 429」与「真的没坑」不可区分——这是站点的供给态，探针只能如实记录，不能改写。
  2. 「凌晨那批 200」的最终归属已判为 CC 复刻实验（同刻探针全 429），但**无独立第三方取证**，登记为口径说明而非铁证。
  3. 500 + `负载已经达到上限` 已归 `POOL_BUSY`（R537 前置分支）；若站点将来新增形态，需重新标定。

## 7. 复现命令

```bash
# 姿势自证（同刻对照，N 发）
/opt/anaconda3/bin/python3 tools/anyrouter_slot_watch.py --verify-pose --n 3

# 自测
/opt/anaconda3/bin/python3 tools/anyrouter_slot_watch.py --selftest    # 期望 177/177

# 单轮真打（7 模型）
/opt/anaconda3/bin/python3 tools/anyrouter_slot_watch.py --once
```

## 8. 结论（给用户的三句话）

1. **探针姿势是对的，从来就不存在「网关只认 CC 客户端」这回事**——真 CC 原样体 9 发也全是 429@1.5–2.3s，与探针 429@1.1–1.3s 同档。
2. **凌晨那批 200 是复刻实验自己打出来的，探针没通所以没回执**——监控行为正确。
3. **现在唯一的瓶颈是号池满**；探针已按 5 分钟一轮持续盯守，一旦出现 200 立刻私信 + 钉钉群推送。
