# R406 · 本机 LLM 槽位可用性台账与探活（tools/llm_slot_probe.py）

- 任务来源：`temp/TODO.txt` 第 11 行（第11批·评审 7.00，环境类）
- 探测时刻：**2026-09-25 16:06:56 +0800**（另有两跑 16:03:38 / 16:05:41，见台账）
- 交付物：`tools/llm_slot_probe.py`（新工具）+ 台账 `temp/llm_slot_ledger.jsonl` + 逐跑明细 `temp/llm_slot_probe/run{1,2,3}.json`
- **口径声明：以下结论仅代表 2026-09-25 16:03–16:07 这一时刻的本机/账号环境**。槽位状态随厂商限流与令牌白名单漂移，任何时候请以当场复跑为准，勿把本报告的 OK/AUTH 当作永久结论。

## 0. 结论（评审该用哪几路）

| 建议 | 槽位 | 名称 | 实测 |
|---|---|---|---|
| 首选 | 0 | deepseek-flash | OK 728ms（最快，适合高频小任务） |
| 首选 | 3 | gpt-6-astra | OK 6885ms |
| 首选 | 4 | sonnet5-4router (claude-sonnet-5[1m]) | OK 3013ms |
| 可用但慢 | 1 | gpt-4router (gpt-5.6-terra) | OK，但两跑 20780ms / 5007ms，抖动大 ⇒ 只作兜底 |
| 别用 | 2 / 5 / 6 / 7 | opus48 / opus5 / luna / sol | 全部 `HTTP 403`，正文为**令牌无权访问该模型**（服务端按模型白名单拒绝，非限流抖动、重试无用） |

补充：多路评审时应尽量跨**不同提供方**（deepseek 官方 / 4router）分散，避免同账号级配额被打满——这与 `autopilot_unattended_drill_sop` 里"同 kind 成员共用账号级配额⇒按配额定规模"的同一条教训一致。

## 1. 复跑命令（可复跑是验收项②）

```bash
cd ~/projects/GenericAgent
python3 tools/llm_slot_probe.py --list                    # 只读枚举槽位，零请求
python3 tools/llm_slot_probe.py --selftest                # 本地假服务器自测（31/31，不发外网请求）
python3 tools/llm_slot_probe.py --probe --timeout 20 \
        --json temp/llm_slot_probe/run4.json              # 真探活：8 槽位 = 8 次请求
python3 tools/llm_slot_probe.py --ledger --diff           # 看历史落账 + 末两跑迁移
python3 tools/bg_task.py start --name llmprobe4 -- python3 tools/llm_slot_probe.py --probe --timeout 20 --json temp/llm_slot_probe/run4.json
```

- 探活实测耗时：**43.5s**（8 槽位串行，含最慢的 6.9s/5.0s 两路）⇒ 建议按上面最后一条用 `bg_task` 后台起，避免工具 60s 超时误判。
- `--probe` 命中非全 OK 时进程退出码为 1（"有槽位不可用"本身是有效结论，不是脚本故障）。

## 2. 逐槽位原始输出与耗时（验收项①）

`temp/llm_slot_probe/run3.json`（ts=2026-09-25T16:06:56+0800，domain=slot，http_requests=8）：

```
slot slot名                 status cls         ms  retries  model
0    deepseek-flash        ok     OK         728  0        deepseek-flash
1    gpt-4router           ok     OK        5007  0        gpt-5.6-terra
2    opus48-4router        fail   AUTH      1468  0        claude-opus-4-8[1m]
3    gpt-6-astra           ok     OK        6885  0        gpt-6-astra
4    sonnet5-4router       ok     OK        3013  0        claude-sonnet-5[1m]
5    opus5-4router         fail   AUTH      1477  0        claude-opus-5[1m]
6    luna-4router          fail   AUTH      1439  0        gpt-5.6-luna
7    sol-4router           fail   AUTH      1441  0        gpt-5.6-sol
全部 retries_forced 之和 = 0
```

**每槽 ≤1 次请求（禁重试风暴）的证据**：`http_requests=8`（= 8 槽位 × 1）+ 每槽 `retries_forced=0` + 8 槽位合计耗时 43.5s（若发生重试风暴，AUTH 四槽不会在 ~1.4s 内返回）。机制上，工具在探活前把会话设成 `max_retries=0 / stream=False`，而 llmcore 的重试循环是 `for attempt in range(max_retries + 1)` ⇒ 归零后严格 1 次请求（selftest 逐场景计数已证）。

4 条 403 的原始正文（同一令牌、同一提供方，仅模型名不同）：

```
slot 2 opus48-4router  !!!Error: HTTP 403: {"error":{"code":"","message":"该令牌无权访问模型 claude-opus-4-8 (request id: ...)","type":"4router_error"}}
slot 5 opus5-4router   !!!Error: HTTP 403: {"error":{"code":"","message":"该令牌无权访问模型 claude-opus-5 (request id: ...)","type":"4router_error"}}
slot 6 luna-4router    !!!Error: HTTP 403: {"error":{"code":"","message":"该令牌无权访问模型 gpt-5.6-luna (request id: ...)","type":"4router_error"}}
slot 7 sol-4router     !!!Error: HTTP 403: {"error":{"code":"","message":"该令牌无权访问模型 gpt-5.6-sol (request id: ...)","type":"4router_error"}}
```

这条与今日评审背景一致（评审被迫只用 llm_no 0/2/4），并**收窄了归因**：不是"4router 挂了"或限流，而是**令牌对该提供方下这几个模型没有权限**——同一提供方的 `gpt-4router`(gpt-5.6-terra) 与 `sonnet5-4router` 同批 OK 可证。因此重试/换节点都不会好，只能改令牌模型白名单或换路。

## 3. 可复跑台账与跨跑 diff（验收项②）

台账 `temp/llm_slot_ledger.jsonl`（3 跑，append-only JSONL，每行含 ts/domain/targets/http_requests/tool/results）：

```
台账 = /Users/cuip/projects/GenericAgent/temp/llm_slot_ledger.jsonl （3 跑）
  2026-09-25T16:03:38+0800  目标=8 OK=0  域=slot
  2026-09-25T16:05:41+0800  目标=8 OK=4  域=slot
  2026-09-25T16:06:56+0800  目标=8 OK=4  域=slot
末两跑迁移：
```

- 末两跑（16:05 / 16:06）**迁移为空 = 8 槽位状态稳定**，说明"4 OK / 4 AUTH"不是瞬时抖动。
- 第 1 跑 OK=0 是**工具自身的 bug 现场**（不是槽位故障），见第 5 节；`--diff` 对着它的前一行即报出 8 条迁移（明细分见下）。

diff 的**正控**（证明"空迁移"确实来自比较、而非比较逻辑失效）：对台账内存副本篡改 1 个槽位后调用同一比较函数：

```
原始 diff: []
篡改后 diff: [('0:deepseek-flash', 'ok/OK', 'fail/AUTH')] => 正控判定: PASS
run1->run2 迁移条数 = 8
```

## 4. 只读与零配置改动（验收项④）

```
$ git status --porcelain mykey.py llmcore.py
(clean)
```

工具只写 `temp/`（gitignored）下的台账与 JSON，不读密钥内容、不改任何配置；输出与台账里**不含任何密钥明文**：用 `mykey` 中「变量名含 key/token/secret」的 11 个字段值（去重后 7 个）对 4 个产物文件做子串扫描，命中 **0**。
（同一扫描首轮用了"所有长字符串"的过宽口径，把模型名/URL 也当密钥，误报 60 条——判"泄露"前必须先把口径限定到凭据字段，否则自造噪音。）

## 5. 工具实现与踩到的坑

`tools/llm_slot_probe.py` 子命令：`--list`（只读枚举）/`--dry-run`（只打印将打的 URL）/`--probe`（真探活，≤1 请求/槽）/`--selftest`（本地假服务器）/`--ledger [--diff]`/`--json`。分类口径（`CLASS_RULES`，按序匹配）= OK / AUTH / QUOTA / SERVER / PARAM / TIMEOUT / EMPTY / NETERR / BADJSON（`EMPTY` 必须排在 `NETERR` 之前，因为 llmcore 把"HTTP 200 但无内容"包成 `ConnectionError("empty response")`，否则"端点活着但吐空"会被误判成"连不上"）。

本轮踩到并已修的**真坑**：探活复用了 llmcore 的真路径，但 8 个槽位里 mixin 槽走的是 `NativeClaudeSession` / `NativeOAISession`，其 `ask(msg: dict)` 与 `BaseSession.ask(prompt: str)` **签名不同**。最初统一按 `ask(prompt_str)` 调用 ⇒ 8 个槽位全部在发请求前 `0ms AssertionError`（台账第一行 OK=0 即此现场）。
修法：按会话类型派发（`ask` 收 dict 的原生会话传 message 列表，其余传字符串），并**把原生会话补进 selftest**（新增 4 条：NativeClaudeSession / NativeOAISession 各"请求次数=1"+"分类 OK"）。
`--selftest` 现值 **31/31 passed**。这条也再次印证 RULES#16：自测必须覆盖"真跑走的那条路径"，否则替身全绿、真机全挂。

## 6. 边界与未做

- 只探"模型能不能被这个令牌调用"，不测**质量/上下文长度/并发上限**；单次响应是极小 prompt，不代表长任务可用性。
- 每跑只发 1 次极小请求（8 次/轮），不做压测；`EMPTY`/`SLOW` 分类存在但本轮未命中。
- 台账只落 `temp/`（gitignored），无版本历史；需要长期趋势应定期复跑并把它交给归档流程。
- 未改任何配置/密钥（零改配置是验收项④）；如需让 2/5/6/7 可用，属"改令牌模型白名单"的用户决策，本报告不代做。
