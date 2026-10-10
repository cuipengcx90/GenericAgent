# R569 L站「发帖 post 为何没执行」根因（2026-10-10）

用户问：「L站流量帖子 怎么没执行啊」。只读取证，结论=**双阻塞，均非崩溃/代码 bug**：
1) 等级缺项路由**结构性不激活** post（设计使然，主因）；
2) 即便要备稿，daemon 运行环境里 codex 生成链路**必败**（真缺陷）。

## 1. 判据链（先定「为什么不发帖」）

L站五动作由「等级检测→缺项路由→五动作」驱动（SOP §21，R542/R544/R550）：
- daemon `queue_pending_from_route` 只把缺项按类排队：浏览类→browse 桶（daemon 自补）、回复/点赞/获赞→reply/like/earn 桶。
- **post 仅当「获赞(earn)」缺项 ∧ 已有草稿**时才排队（SOP §21 + R544 forge 补稿链）。

### R1 结构性闸门（铁证）
- 今日 level_log（末条 `2026-10-08 19:56:07`，source=connect）：`met=11, pending=1`。
- 唯一未达标项 = **14752 / 20000（浏览/阅读）** → 归 browse 桶；daemon 自己 `browse_batch` 补，**永不进 post 路由**。
- 获赞（earn）历届实测 47–50 / 门槛 20 → **恒 MET**（今日 50/20 亦 MET）⇒ post 路径自建成起从未被激活。
- **结论：不是坏了，是不满足触发条件。post 只在「获赞不足」时才出手。**

## 2. 判据链（即便要备稿，管道也断）— R2 真缺陷

- daemon 进程环境 `PATH=/usr/bin:/bin:/usr/sbin:/sbin`（**无 /opt/homebrew/bin**；plist 无 EnvironmentVariables）。
- `codex=/opt/homebrew/bin/codex` 是 `#!/usr/bin/env node` 脚本 ⇒ 需 `node` 在 PATH；而 node 装在 `/opt/homebrew/bin`（v26.5.0）
- **决定性对照（本轮实测）**：在与 daemon 同款受限 PATH 下直调 `acts.gen_text("...PONG")`，0.2s 返回
  `OUT=None  WHY='rc=127 env: node: No such file or directory'`。
- forge 流程：`forge()` 先做 **topic 生成**（`gen(TOPIC_PROMPT...)`）→ gen 返回 None → 走 **NO_TOPIC** 分支
  （ledger 今日 2 行 `ok=false, why=NO_TOPIC`，ts≈1791621231 / 1791622248）。
  即「连标题都生成不出来」→ 不会有草稿 → acts 侧 `SKIP:NO_DRAFT`。
- **补 PATH 后** codex 能起（node v26.5.0），但上游通道 sharedchat-k1 返 **403「没有可用的 Codex 订阅额度或余额」**（耗时 105s）
  ⇒ 即便修好 PATH，文案生成仍会失败于**上游额度**。故 R2 是「PATH + 上游额度」双重失败。

## 3. R3：post 从未真跑
- `acts_log.jsonl` 全为 `2026-10-06 12:50` 两簇**自测夹具**（QUEUED_POST 等），零真实 post 记录。
- 综合 R1 路由不激活 + R2 无草稿 ⇒ post 动作从未真正执行。

## 4. 修复方向（改 tools 自身源码 ⇒ 待批准；未批准前未改动）

- **F1（PATH 注入）**：`tools/linuxdo_acts.py::gen_text` 与 `tools/linuxdo_post_forge.py::_codex_gen`
  spawn codex 时注入 PATH（含 `/opt/homebrew/bin`、`/usr/local/bin`），或直接调用**绝对 node** 起 codex；
  daemon 经 `subprocess.Popen` 继承 env ⇒ 同批修 `tools/linuxdo_browse_daemon.py::_maybe_forge_draft` 的 spawn env。
- **F2（上游额度）**：codex 通道当前 403 无额度 ⇒ 生成链路需换可用通道，或对生成失败做显式降级/告警（与 F1 正交）。
- **F3（路由语义，产品决策非缺陷）**：若希望「无获赞缺项也备稿/发帖」，需显式改 `queue_pending_from_route` 条件——
  属产品取舍，建议保持默认（不为凑数发帖，守防检测铁律）。

## 5. 证据清单（只读自证）
- `temp/linuxdo_browse_daemon/level_log.jsonl` 末条 `2026-10-08 19:56:07`：`met=11, pending=1`，缺项=14752/20000。
- daemon 进程 env：`PATH=/usr/bin:/bin:/usr/sbin:/sbin`；plist 无 EnvironmentVariables。
- 决定性对照：受限 PATH 下 `gen_text` → `rc=127 env: node: No such file or directory`。
- 补 PATH 对照：codex 起得来（node v26.5.0），上游 403 无 Codex 额度。
- forge ledger：今日 2 行 `ok=false, why=NO_TOPIC`。
- `acts_log.jsonl`：仅 10-06 12:50 自测夹具。

## 6. 边界
- 本次为**只读取证**，未改任何 tools 源码；F1/F2 待批准。
- 根因判定基于当日 level_log 与 daemon 运行环境快照；环境/PATH 变化后须重取。