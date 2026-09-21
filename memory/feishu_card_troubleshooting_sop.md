# 飞书卡片卡死排查 SOP

## 现象
长任务跑到约 50–60 步后，飞书端卡片停止更新：任务仍显示"进行中"、GA 进程也活着，
但界面一动不动；用户必须再说一句"继续"。同一会话可重复出现（曾观测连续 11 次）。

## 先分类：两套根因，判据完全不同

### A 类：卡片体积超限 → 发送静默失败
- 判据：日志出现 `发送失败: 230099, Failed to create card content, ext=ErrCode: 200800`
- 关键数字（实测）：超限点约 **58 步**；单步 detail 均值 272 字符、中位数 80、最大 9003
  → 单步体积波动极大，**只按步数设限不安全**，必须叠加字符预算
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
- `history_info` 里的 `[Agent] 任务完成：…` 只是该轮 `<summary>` 文本，**不代表 loop 退出**；
  据此判断"模型提前收工"会误判 —— 本 SOP 的排查最初就走错了这个方向
- `put_task` 是**队列**：用户说"继续"只是排队，**不中断**正在跑的工具，
  "说完继续就动了"多半是时间巧合
- `_fold_earlier` 显示的 `（55 turns）` 是历史累计轮数，不等于当次 loop 的 turn
- 判断"日志停更"必须先看 `ps` 有无子进程：有 `sleep` → B 类；无子进程且卡在 kevent → 查网络/回调
- 重启 fsapp 后要**只统计最后一次"已启动"标记之后**的失败，否则会把重启前的残留当成新问题

## 相关产物
- 修复提交：`16f9c25`（fsapp.py）、L1 RULE 15（global_mem_insight.txt）
- 上游 Issue：lsdefine/GenericAgent#814（含分析与建议）
- 补丁备份：`temp/patches/feishu-card-pagination.patch`（temp/ 会被清理，需另存）
