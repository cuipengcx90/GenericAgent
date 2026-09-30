# smyze_stock_watch_sop — bar-smyze 供应链库存口径日检（GA 定时任务）

## 触发/入口
- 定时任务在册：`sche_tasks/smyze_stock_watch.json`（`08:30` daily，enabled）。
- 唯一入口（勿依赖 cwd，先 cd）：
  ```bash
  cd /Users/cuip/develop/WorkSpaces/idea/smyze/bar-smyze/bar_smyze_opserver/.claude/worktrees/stock-watch-daily/docs/analysis/supply-chain/stock-watch
  set -o allexport; . /Users/cuip/.config/smyze/stock-watch.env; set +o allexport
  ./deploy/daily-run.sh; echo "exit=$?"
  ```
- 口径/已知基线/部署方式：同目录 `README.md`（红线 13 项 + 关键指标 15 项 + 「较上次变差」观察项）。

## 硬约束（实测）
- **只读巡检**：`sql/01~07` 全为 SELECT，不改生产数据。
- **退出码**：`0`=完成且无红线；`1`=有红线偏离基线；`2`=配置/凭据问题（如 `MYSQL_PWD` 未设，凭据在 `~/.config/smyze/stock-watch.env`）；`3`=有 SQL 执行失败（连库/网络）。
- **推送自带且按日幂等**：脚本经 `lark-cli` 以 user 身份私信崔朋；`STOCK_WATCH_LARK_ALWAYS=1` ⇒ 每天**必推一条简报**；同日重复运行会输出「今日（YYYY-MM-DD）简报已推送过，跳过重复推送」。
  - ⇒ **本任务绝不另调 `notify_feishu` / 不手工补推**；唯一例外=「退出码≠0 且 stdout 无『已推送』」时补一条「推送链路可能异常」告警。
  - ⇒ 同日已跳过≠巡检没跑：本轮仍会新写快照+报告，只是不重复推送；勿为「让它推送」而绕过幂等。
- **已知历史缺口（非新问题）**：`roll.bad_continuity=3`（1057×3 @08-19→08-20 建账边界）、`cross.in_bad_rows=32`（1057 @08-20）；`forensic.bak_neg_frozen_rows=5` 亦为已知值。
- 报告/快照落本目录 `reports/`、`snapshots/`（同日可有多份，文件名带 `HHMMSS`）；GA 侧调度报告落 `sche_tasks/done/`（gitignored）。

## 实测记录（2026-09-29 14:26 首次 GA 侧执行）
- `exit=0`、红线 13/13 OK；简报被幂等跳过（今日已推送过）。
- 当日 `reports/` 已有 6 份（10:11→14:26），说明该链路当天被多次触发；调度若要求「一天一次」需核对触发源，勿擅自改生产配置。
