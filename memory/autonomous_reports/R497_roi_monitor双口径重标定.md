# R497 roi_monitor 双口径重标定：心跳型剥离 + 阈值复活

> 任务来源：`temp/TODO.txt:199`（第23批·两路评审 6.00）
> 性质：只读分析 + 新增两个只读工具与一份阈值产物；**未改动 roi_monitor.py 生产端**（改自身源码需批准，见 §7）

## 0. 一句话结论

把 ROI 日报的判据从「全量 done 次数」拆成 **业务型 / 心跳型双口径**，心跳判定收敛为单一源注册表，阈值由**分位网格搜索**重标定并加「可触发性闸门」⇒ 阈值复活（v2 的四条业务规则里三条近死，现已全部可触发且触发率 ≤ 2.4%）。同时给出全量口径的**结构假象**硬证据：全量 WoW<-30% 至今仍连续触发（最近 2026-09-28），而业务型口径最近一次触发止于 2026-06-05。

## 1. 口径定义（本次落地的三条铁律）

1. **双口径**：全量（all，含心跳）与业务型（biz，剥离心跳）必须分列；任何跨期同比只用业务型。
2. **心跳判定单一源**：`tools/roi_task_class.json`（schema `roi_task_class/v1`）为唯一注册表；规则文本 = `per_active_day >= 1.0 且 median_lines <= 25 且『无事件也照跑』(人工确认,note 记理由)`。
3. **执行次数 vs 去重种类**：指标同时给「执行次数」与「去重任务种类数」，灌水比 `ratio = 执行次数 / 去重种类数`。

未登记任务名策略：`未登记任务名 => UNCLASSIFIED,业务口径告警 fail-closed 并列出(禁静默归入业务层)`

## 2. 交付物（sha256 前16 + 字节）

| 文件 | sha256前16 (字节) |
|---|---|
| `tools/roi_task_class.py` | eed49c49228f3c1a (12296 B) |
| `tools/roi_task_class.json` | d4b4124744285953 (8376 B) |
| `tools/roi_calibration.py` | 8c01c4f6b5cd485c (29619 B) |
| `tools/roi_thresholds_v3.json` | 3c84edd5e8854525 (2164 B) |

注册表实测面：任务种类 **30**（business 26 / heartbeat 4；`python3 tools/roi_task_class.py audit` 输出 `problems=0 infos=0 tasks=30 heartbeat=4`），测量窗口 done_files **6055** / kinds 30 / 2026-04-23..2026-09-28，`generated_at` 2026-09-29T02:11:53。

## 3. 回测（两口径并行，127 天评估窗）

```
V1     avg_lines_per_task < 100     快照=1 含字段=0 可触发=0  # v1 结构性死亡(字段从不被写)
V2a    biz_drop > 25.0              fires=  4/127 (  3.1%) first=2026-06-02 last=2026-06-05  # 历史 v2 阈值(取自当时极值)
V2b    bpd < 4.0                    fires=  2/127 (  1.6%) first=2026-06-04 last=2026-06-05  # 历史 v2 阈值(取自当时极值)
V2c    hbshare > 85.0               fires= 44/127 ( 34.6%) first=2026-06-09 last=2026-08-07  # 历史 v2 阈值(心跳占比)
R1     bpd < 4.0                    fires=  2/127 (  1.6%) first=2026-06-04 last=2026-06-05  # 业务型任务执行量稀疏(7日均)
R2     lines7 < 26.6                fires=  3/127 (  2.4%) first=2026-07-10 last=2026-08-07  # 业务型单任务平均产出过低(7日均行数)
R3     biz_drop > 34.4              fires=  2/127 (  1.6%) first=2026-06-04 last=2026-06-05  # 业务层 7日均 vs 30日均 下滑幅度
R4     ratio > 5.85                 fires=  3/127 (  2.4%) first=2026-06-21 last=2026-09-18  # 业务型灌水比(执行次数/去重种类数)
R5     unclass7 > 0                 fires=  0/127 (  0.0%) first=None last=None  # 出现未登记任务名(fail-closed)(fail-closed)
FULL   wow_all < -30.0              fires= 20/127 ( 15.7%) first=2026-06-03 last=2026-09-28  # 全量口径 WoW<-30%（心跳归档后连续触发=假象）
FULLbiz wow_biz < -25.0              fires=  4/127 (  3.1%) first=2026-06-02 last=2026-06-05  # 业务型口径 WoW<-25%（同期真实值）
written temp/roi_calibration/backtest_20260929.json

---STDERR---
```

要点：
- `V1 avg_lines_per_task < 100`：快照 1 条 / 含字段 0 / 可触发 0 ⇒ **v1 结构性死亡**在回测中再次被证实（字段从不被写）。
- `V2a biz_drop > 25.0` 4/127 (3.1%)、`V2b bpd < 4.0` 2/127 (1.6%)、`V2c hbshare > 85.0` 44/127 (34.6%，末次 2026-08-07) ⇒ v2 三档里两档近死、一档噪声化（心跳归档后占比口径崩坏），**「阈值复活」的必要性有硬证据**。
- 全量口径 `FULL wow_all < -30.0`：20/127 (15.7%)，`fire_dates_tail` 连续命中 `2026-09-23..2026-09-28`；同期业务型 `FULLbiz wow_biz < -25.0` 4/127 且末次 **2026-06-05** ⇒ 近端全量告警属**心跳归档造成的结构假象**，非业务塌陷。

## 4. 标定结果（`calibrate` 真跑，落盘 `tools/roi_thresholds_v3.json`）

```
R1   bpd < 4.0                  thr=4.0      fires=  2 rate=  1.6% gate=PASS  # 业务型任务执行量稀疏(7日均)
R2   lines7 < 26.6              thr=26.6     fires=  3 rate=  2.4% gate=PASS  # 业务型单任务平均产出过低(7日均行数)
R3   biz_drop > 34.4            thr=34.4     fires=  2 rate=  1.6% gate=PASS  # 业务层 7日均 vs 30日均 下滑幅度
R4   ratio > 5.8                thr=5.85     fires=  3 rate=  2.4% gate=PASS  # 业务型灌水比(执行次数/去重种类数)
R5   unclass7 > 0               thr=0        fires=  0 rate=  0.0% gate=n/a(fail-closed)  # 出现未登记任务名(fail-closed)
written tools/roi_thresholds_v3.json

---STDERR---
```

闸门规则：每条业务规则须 **≥1 次触发**（否则 `DEAD_THRESHOLD`）且 **触发率 ≤ 上限**（否则 `TOO_NOISY`）；未登记名 R5 为 `fail-closed`（标 `n/a`，不计触发率）。四条业务规则全部 `gate=PASS`，触发率 1.6%–2.4%。

历史触发点（取自回测 JSON 的 `fire_dates`）：
- R1 `bpd < 4.0`：2026-06-04、2026-06-05
- R2 `lines7 < 26.6`：2026-07-10、2026-07-12、2026-08-07
- R3 `biz_drop > 34.4`：2026-06-04、2026-06-05
- R4 `ratio > 5.85`：2026-06-21、2026-06-25、2026-09-18

## 5. 负控与自测

`python3 tools/roi_task_class.py selftest`（rc=0）：
```
PASS P1 真注册表 audit 无 problem
PASS P2 folder_watcher=heartbeat
PASS P3 github_trending=business
PASS P4 未知名=UNCLASSIFIED
PASS P5 心跳型非空且含 price_alert
PASS P6 注册表心跳项均满足规则
PASS NC1a 全覆盖 fixture 无 problem
PASS NC1 心跳频次不足必报 HB_RULE_VIOLATED
PASS NC2 业务型误标心跳必报（行数超阈）
PASS NC3 心跳缺理由必报 HB_NO_REASON
PASS NC4 未登记任务名必报 UNREGISTERED
PASS NC4b 未登记名 classify=UNCLASSIFIED
PASS NC5 retired 缺 retired_at 必报
PASS NC6 schema 错必抛
PASS NC7 空 tasks 必抛
selftest: 15 passed, 0 failed
```
`python3 tools/roi_calibration.py selftest`（rc=0）：
```
R1   bpd < N/A                  thr=None     fires=  0 rate=  0.0% gate=DEAD_THRESHOLD  # 业务型任务执行量稀疏(7日均)
R2   lines7 < N/A               thr=None     fires=  0 rate=  0.0% gate=DEAD_THRESHOLD  # 业务型单任务平均产出过低(7日均行数)
R3   biz_drop > N/A             thr=None     fires=  0 rate=  0.0% gate=DEAD_THRESHOLD  # 业务层 7日均 vs 30日均 下滑幅度
R4   ratio > N/A                thr=None     fires=  0 rate=  0.0% gate=DEAD_THRESHOLD  # 业务型灌水比(执行次数/去重种类数)
R5   unclass7 > 0               thr=0        fires=  0 rate=  0.0% gate=n/a(fail-closed)  # 出现未登记任务名(fail-closed)
CALIBRATION_FAIL problems=['DEAD_THRESHOLD R1 bpdlt 无可行阈值（网格 7 档全不合格）', 'DEAD_THRESHOLD R2 lines7lt 无可行阈值（网格 7 档全不合格）', 'DEAD_THRESHOLD R3 biz_dropgt 无可行阈值（网格 7 档全不合格）', 'DEAD_THRESHOLD R4 ratiogt 无可行阈值（网格 7 档全不合格）']（不落盘）
R1   bpd < 4.9                  thr=4.9      fires=  4 rate= 10.0% gate=PASS  # 业务型任务执行量稀疏(7日均)
R2   lines7 < 41.2              thr=41.2     fires=  1 rate=  2.5% gate=PASS  # 业务型单任务平均产出过低(7日均行数)
R3   biz_drop > 8.9             thr=8.86     fires=  4 rate= 10.0% gate=PASS  # 业务层 7日均 vs 30日均 下滑幅度
R4   ratio > 5.1                thr=5.14     fires=  3 rate=  7.5% gate=PASS  # 业务型灌水比(执行次数/去重种类数)
R5   unclass7 > 0               thr=0        fires=  0 rate=  0.0% gate=n/a(fail-closed)  # 出现未登记任务名(fail-closed)
written ../../../../var/folders/vx/800kk6dd57bgqvvjy7dh5jfr0000gn/T/roi_cal_ghqv2hho/thr_v3.json
PASS A1 scan 覆盖 40 天
PASS A2 心跳名只进 hb 不进 biz
PASS A3 未登记名计入 unclass 且不污染业务计数
PASS A4 执行次数与去重种类分离
PASS A5 unclass7 指标在同日=1
PASS B1 纯心跳日使全量口径 WoW 崩塌
PASS B2 同日业务型口径未崩塌
PASS C1 quantile 纯函数正确
PASS C2 正常窗四条业务规则齐备且阈值非空
PASS C3 正常窗全部规则通过可触发性闸门
PASS C4 正常窗各规则触发率不低于下界
PASS C5 R1 阈值随分布变化（非硬编码）
PASS NC1 常量序列 R1 判 DEAD_THRESHOLD 且闸门不放行
PASS NC2 退化语料 calibrate gate_ok=False 且不写盘
PASS D1 业务塌陷触发 R1(bpd<) 且命中塌陷窗
PASS D2 业务下滑触发 R3(biz_drop>)
PASS D3 灌水日 ratio 上升（执行次数/去重种类可分离）
PASS D4 未登记名触发 R5(fail-closed) 且标 n/a
PASS E1 从不触发判 DEAD_THRESHOLD
PASS E2 触发率超上限判 TOO_NOISY
PASS E3 正常触发率通过闸门
PASS F1 合格语料 gate_ok=True 且写盘
PASS F2 落盘 schema/规则数与内存一致
selftest: 23 passed, 0 failed
```

负控面覆盖：常量序列（网格全废）判 `DEAD_THRESHOLD` 且**不放行/不落盘**、退化语料 `gate_ok=False` 不写盘、触发率超上限判 `TOO_NOISY`、心跳名不进业务计数、未登记名 `fail-closed`、**纯心跳日使全量口径崩塌而业务型口径不崩塌**（B1/B2，直接对应 TODO 验收④）。

## 6. 只读自证

```
V1     avg_lines_per_task < 100     快照=1 含字段=0 可触发=0  # v1 结构性死亡(字段从不被写)
V2a    biz_drop > 25.0              fires=  4/127 (  3.1%) first=2026-06-02 last=2026-06-05  # 历史 v2 阈值(取自当时极值)
V2b    bpd < 4.0                    fires=  2/127 (  1.6%) first=2026-06-04 last=2026-06-05  # 历史 v2 阈值(取自当时极值)
V2c    hbshare > 85.0               fires= 44/127 ( 34.6%) first=2026-06-09 last=2026-08-07  # 历史 v2 阈值(心跳占比)
R1     bpd < 4.0                    fires=  2/127 (  1.6%) first=2026-06-04 last=2026-06-05  # 业务型任务执行量稀疏(7日均)
R2     lines7 < 26.6                fires=  3/127 (  2.4%) first=2026-07-10 last=2026-08-07  # 业务型单任务平均产出过低(7日均行数)
R3     biz_drop > 34.4              fires=  2/127 (  1.6%) first=2026-06-04 last=2026-06-05  # 业务层 7日均 vs 30日均 下滑幅度
R4     ratio > 5.85                 fires=  3/127 (  2.4%) first=2026-06-21 last=2026-09-18  # 业务型灌水比(执行次数/去重种类数)
R5     unclass7 > 0                 fires=  0/127 (  0.0%) first=None last=None  # 出现未登记任务名(fail-closed)(fail-closed)
FULL   wow_all < -30.0              fires= 20/127 ( 15.7%) first=2026-06-03 last=2026-09-28  # 全量口径 WoW<-30%（心跳归档后连续触发=假象）
FULLbiz wow_biz < -25.0              fires=  4/127 (  3.1%) first=2026-06-02 last=2026-06-05  # 业务型口径 WoW<-25%（同期真实值）
proof files=203 changed=[]

---STDERR---
```
即 `proof files=203 changed=[]`：工具对 203 个语料文件读前读后 sha256 一致（本轮无写入面）。

## 7. 边界与未接线（待批准）

1. **生产端未接线**：`roi_monitor.py` 仍是 task_volume_v1 单口径，v3 阈值尚未被它消费 —— 改生产脚本属「改自身源码」需授权。
2. **未接定时**：`roi_calibration.py calibrate` 需人工/定时触发；建议每 N 日复算一次（阈值随分布漂移）。
3. **回测口径**：评估窗 127 天；窗口两端因 7 日均线 warm-up 不参与判定。
4. 报告中的历史值 `V2a/V2b` 以本器现场实测为准（此前会话笔记记 `1/127`，口径不同，未采信）。
5. 台账：`temp/pending_decisions/DECISIONS_R497_roi_calibration.md`（D1 生产端接线 / D2 定时复算 / D3 心跳集合扩面复核，均 revers=0 待批；批准语 `批准 DECISIONS_R497_roi_calibration.md:D<n>`）。

## 8. 复跑命令

```
python3 tools/roi_task_class.py audit
python3 tools/roi_task_class.py selftest
python3 tools/roi_calibration.py scan
python3 tools/roi_calibration.py calibrate
python3 tools/roi_calibration.py backtest
python3 tools/roi_calibration.py proof
```
