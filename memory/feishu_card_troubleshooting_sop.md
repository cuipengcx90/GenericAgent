# 飞书卡片卡死排查 SOP

## 现象
长任务跑到约 50–60 步后，飞书端卡片停止更新：任务仍显示"进行中"、GA 进程也活着，
但界面一动不动；用户必须再说一句"继续"。同一会话可重复出现（曾观测连续 11 次）。

## 先分类：两套根因，判据完全不同

### A 类：卡片 element 数 / 体积超限 → 发送静默失败
- 判据：日志 `Failed to create card content`，**先看错误码是哪一种**：
  - `11310 element exceeds the limit` → **element 数量超限**
  - `200800 create universal card fail` → 结构非法（如 2.0 用了已移除的 tag）或体积超限
- **关键实测（2026-09-21，逐个实发标定）**：
  - `elements=64` ✅ 通过 / `elements=67` ❌ 被拒 → **硬上限约 65 个 element**
  - `50 步 × 800 字`（4 万字符 / **126KB**）✅ 通过 → **体积不是主要瓶颈**
  - 故历史上"58 步超限"的主因是 **58 个 collapsible_panel 撞到 65 上限**，
    而不是字节数。**按体积去优化是错误方向**（曾把 `_PAGE_CHARS` 设 8000，
    导致每步稍长就 5~19 步翻页，与"50 步一页"的设计意图不符）
- 现行参数：`_PAGE_STEPS=50`（50步+页眉+按钮区≈52 个 element，留余量）
  + `_PAGE_CHARS=30000`（仅作体积兜底，防极长 thinking 把单页推到 MB 级）
- 历史缺陷（2026-09-21 修复，commit `16f9c25`）：
  1. `_rollover_locked` 递增 page_no、清空 msg_id 却未清空 `steps`，而 `_build_locked`
     遍历完整 steps → "新"卡片与超限旧卡片同样大、同样失败，rollover 空转
  2. `_push_sync` 新建失败返回 `(False, False)`，而 `_worker_loop` 只在 `limit=True`
     时 rollover → msg_id 一旦为 None 就永远走新建分支，卡片永久发不出去
- 现状：按钮翻页（`◀ 上一页 / N/M / 下一页 ▶`，页内双判据 50 步 + 8000 字符），
  同一卡片内浏览全部历史，不再产生多条消息

### B 类：code_run 阻塞等待 → 通道静默
- 判据：日志停更数分钟 + `ps` 能看到 `sleep N` 子进程 + `[Timeout Error] 超时强制终止` 频繁
- 成因：GA **没有后台任务工具**（只有 9 个原子工具，`code_run` 同步阻塞、timeout 上限 600s），
  而 `ga.py:315` 的引导语要求"耗时代码丢后台跑并监控到完成"，模型只能写
  `nohup xxx & sleep 300; cat log` 这类长等待
- 现状：L1 RULE 15 要求改两步法 —— ①只启动立即返回 `nohup xxx > log 2>&1 & echo pid=$!`
  ②后续每轮用一次 code_run 查状态 `tail -3 log; ps -p <pid> || echo DONE`

## 排查命令
```bash
cd ~/projects/GenericAgent

# 1) 是否卡片超限（A 类）
tail -2000 logs/feishu_agent.log | grep -c "Failed to create card content"

# 2) 是否 sleep 阻塞（B 类）：看有无卡住的子进程
ps aux | grep -E "sleep [0-9]+|\.ai\.py" | grep -v grep

# 3) 日志是否还在动：3 次采样比字节数
for i in 1 2 3; do stat -f '%z' logs/feishu_agent.log; sleep 5; done

# 4) 抓进程栈定位卡点（kevent = IO 等待）
sample $(pgrep -f frontends/fsapp.py | head -1) 4 -mayDie | head -40

# 5) 只统计"最近一次启动之后"的失败，排除重启前残留
last=$(grep -n "飞书 Agent 已启动" logs/feishu_agent.log | tail -1 | cut -d: -f1)
sed -n "${last},\$p" logs/feishu_agent.log | grep -c "Failed to create card content"
```

## 关键坑（都踩过）
- **rollover（换新卡片）绝不可在 patch 失败时触发** —— 它会清空 `steps`，把全部历史丢掉。
  分页已保证单页不超 element 上限，patch 失败应重试而不是换卡片；只有"新建卡片就失败"
  （卡片从未建立、无历史可丢）才允许 rollover。症状：**明明有翻页按钮，却又冒出一张新卡片**。
  2026-09-21 fix（commit `a846395`）—— 原实现把 `_patch_card_result` 的 limit 直接透传，
  而该 limit 在错误码含 230099 时恒为 True，等于任何 patch 失败都换卡片。
- **改了卡片 JSON 结构必须实发一张验证**，本地语法/单测全过也没用 —— 字段合法性只有飞书服务端知道。
  案例：把按钮包进 `{"tag":"action"}`（1.0 写法）提交后才由线上报 200861 发现，白折腾一轮。
  自检命令（发一张自带按钮的卡片，返回 success 即通过）见本文件「验证卡片结构」一节。
- **卡片 JSON 2.0 已移除 `action` 容器标签**：按钮/下拉等交互组件必须直接作为 element
  放进 `body.elements`；套容器会报 `200861 unsupported tag action`。
- **按钮回调需双端齐备**：①代码注册 `register_p2_card_action_trigger`；②开发者后台「事件与回调」
  订阅 `card.action.trigger`。只做①则点击无任何反应（飞书不会推送）。
- 卡片回调硬约束：响应须 3 秒内、交互有效期 30 天、可更新有效期 14 天。
- `history_info` 里的 `[Agent] 任务完成：…` 只是该轮 `<summary>` 文本，**不代表 loop 退出**；
  据此判断"模型提前收工"会误判 —— 本 SOP 的排查最初就走错了这个方向
- `put_task` 是**队列**：用户说"继续"只是排队，**不中断**正在跑的工具，
  "说完继续就动了"多半是时间巧合
- `_fold_earlier` 显示的 `（55 turns）` 是历史累计轮数，不等于当次 loop 的 turn
- 判断"日志停更"必须先看 `ps` 有无子进程：有 `sleep` → B 类；无子进程且卡在 kevent → 查网络/回调
- 重启 fsapp 后要**只统计最后一次"已启动"标记之后**的失败，否则会把重启前的残留当成新问题

## 验证卡片结构（改完卡片相关代码必做）
本地语法检查与单测**测不出字段合法性**，必须实发一张卡片让飞书校验：

```bash
cd ~/projects/GenericAgent
python3 memory/feishu_card_selftest.py <open_id>
# open_id 取法: grep "收到消息" logs/feishu_agent.log | tail -1
```

输出 `success=True` 即通过；失败会带 `ErrPath`（形如 `body.elements[6](tag: action)`），
直接指出是哪个 element 的哪个字段不合法。

## 相关产物
- 修复提交：`16f9c25`（分页按钮翻页）、`fb96c34`（修正按钮结构）、`2b8ab8f`（本 SOP 与自检脚本）
- L1 RULE 15（global_mem_insight.txt）、自检脚本 `memory/feishu_card_selftest.py`
- 上游 Issue：lsdefine/GenericAgent#814（含分析与建议）

### 官方更新后如何保住这套改动
改动已 commit 在本地 main 分支，官方 `git pull` 属 merge，commit 不会消失；
仅当官方恰好改了同一行才会冲突，按冲突标记手工合并即可。三层保障：

1. **git commit**（主保障）：`git log --oneline -- frontends/fsapp.py` 可随时找回
2. **差异补丁**（仓库外，不受 GA 的 temp 清理影响）：
   - `~/GenericAgent-patches/feishu-card-pagination-vs-official.patch` —— 官方版→当前版
   - `~/GenericAgent-patches/feishu-card-pagination-commits.patch` —— commit 形式完整脉络
3. **本 SOP** 记录改动的三条不可丢约束

重放：
```bash
cd ~/projects/GenericAgent
git apply ~/GenericAgent-patches/feishu-card-pagination-vs-official.patch
```

手工合并时必须保住的三条（丢了任何一条都会复发）：
- 按钮**直放** `body.elements`，不得套 `{"tag":"action"}` 容器（2.0 已移除）
- 分页双判据 `_PAGE_STEPS`(50) + `_PAGE_CHARS`(8000)，只按步数会漏
- `_rollover_locked` 必须清 `steps`、`_push_sync` 新建失败必须回 `limit`
