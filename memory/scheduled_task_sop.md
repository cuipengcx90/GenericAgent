# 定时任务 SOP

目录：`../sche_tasks/` 放任务定义JSON，`../sche_tasks/done/` 放执行报告

## 任务JSON格式（*.json）
```json
{"schedule":"08:00", "repeat":"daily", "enabled":true, "prompt":"...", "max_delay_hours":6}
```
repeat可选：daily | weekday | weekly | weekly_last_workday | monthly | once | every_Nh（每N小时）| every_Nd（每N天）
max_delay_hours（可选，默认6）：超过schedule多少小时后不再触发，防止开机太晚执行过时任务

## 触发流程
1. ../reflect/scheduler.py 每60秒轮询 sche_tasks/*.json
2. 条件全满足才触发：enabled=true + 当前时间≥schedule + 冷却时间已过（基于done/最新报告时间戳）
3. 触发时拼prompt，含报告路径 `../sche_tasks/done/YYYY-MM-DD_任务名.md`
4. **收到任务后第一件事**：用 update_working_checkpoint 记录报告目标文件路径，防止长任务执行中遗忘
5. 执行完毕后将报告写入上述路径（scheduler靠此文件判断今天已执行）

## 日志与监控
- scheduler自动写日志到 `sche_tasks/scheduler.log`（触发/跳过/错误）
- `scheduler.health_check()` 返回所有任务状态列表（HEALTHY/OVERDUE/DISABLED/NEVER_RUN/ERROR）
- JSON解析错误、schedule格式错误、未知repeat类型均会记录日志

## 注意
- once类型：执行一次后冷却100年（实际效果为永久跳过）
- weekly_last_workday：依赖 `chinese_calendar` 包判断中国法定调休，自动在每周最后一个工作日触发（如国庆前提前到周三）
- weekly + day字段：固定在指定星期几触发（如 `"day":"friday"`），不考虑调休
- 任务文件只管"干什么"，报告路径由scheduler自动生成注入prompt
- sche_tasks目录在../，即code root下

## ⚠️ 调度阻塞防护（2026-05-17 验证）
- check()按文件名字母序遍历，返回第一个可触发任务。若该任务执行失败(done文件未写入)会反复触发，阻塞所有字母序靠后的任务
- 已修复：_pending防抖机制——同任务10min冷却+连续3次未完成→BLOCK跳过，让后续任务有机会
- **避坑**：`max_delay_hours`不要设过大(如48h)，否则卡死任务会长期霸占调度通道；建议≤8h

## ⚠️ 报告落盘核验（2026-09-18 事故教训）
- 写完报告**必须用真实工具回读核验文件存在**（`os.path.exists`+size 或 file_read），不能凭"已写入"的回显判断。
- 事故现象：连续多次 `file__write` 被当成文本回显、实际未执行，报告文件始终不存在，若不核验就会"空报完成"（违反 RULE 13）。
- 判据：write 返回的 `writed_bytes` 须与后续独立读取的真实 size 对得上；对不上或 exists=False 即为未落盘，重写。

## ⚠️ 格式坑点（已验证）
- `schedule` 必须是字符串如 `"08:00"`，不能是数字（整数/浮点均报错）
- `repeat` 数字部分只支持整数：`every_1h` ✅，`every_0.5h` ❌（int()解析报错）
- `repeat: "interval"` 无效，不会报错但会fallback到默认冷却，导致任务永不触发