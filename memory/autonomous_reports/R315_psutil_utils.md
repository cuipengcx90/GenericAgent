
# R315 psutil_utils.py — 系统实时监控工具封装

## 任务
封装 psutil 为结构化系统监控工具，覆盖 CPU/内存/磁盘/网络/进程，输出 dict 可直接 JSON 序列化。

## 产出
`memory/psutil_utils.py` — 8 个公开函数 + CLI入口

| 函数 | 功能 | 输出 |
|------|------|------|
| `cpu_snapshot(per_cpu, interval)` | CPU使用率/核心数/频率/负载 | dict |
| `memory_snapshot()` | RAM+Swap用量百分比 | dict |
| `disk_snapshot(all_partitions)` | 分区使用量+IO计数器 | dict |
| `network_snapshot(include_connections)` | 接口/IO/连接状态 | dict |
| `process_snapshot(pid/name)` | 单进程详情或按名模糊搜索 | dict/list |
| `top_processes(n, sort_by)` | Top N进程按CPU或内存排序 | list |
| `system_snapshot(per_cpu)` | 全量快照(CPU+内存+磁盘+网络+启动时间) | dict |
| `watch_resource(interval, callback, duration, metrics)` | 定期采集+回调，可接入scheduler | list |

## 设计要点
- `_safe()` 包裹所有可能抛权限/不支持异常的调用，确保部分失败不影响整体快照
- Apple Silicon 上 `cpu_freq()` 抛 `FileNotFoundError`，已加入异常捕获列表
- `process_snapshot` 使用 `oneshot()` 上下文管理器减少系统调用
- `watch_resource` 支持指定 metrics 子集，避免采集不需要的数据
- 所有输出含 ISO 时间戳，GB/MB 自动换算

## 平台坑记录
| 问题 | 平台 | 处理 |
|------|------|------|
| `cpu_freq()` → FileNotFoundError | macOS Apple Silicon | `_safe()` 返回 None |
| `disk_io_counters()` → None | macOS 部分版本 | 检查返回值 |
| `net_connections()` → AccessDenied | 非root | 可选参数控制 |

## 测试结果（本机 macOS, 10核, 16GB, psutil 5.9.0）
```
✅ Test 1: cpu_snapshot        — 32.2%, 10L/10P cores, load [4.28, 5.59, 7.44]
✅ Test 2: memory_snapshot     — 16.0GB, 80.3% used
✅ Test 3: disk_snapshot       — 9 partitions, 460GB disk
✅ Test 4: network_snapshot    — 38 interfaces, 490MB sent
✅ Test 5: process_snapshot(pid) — self PID, 21MB, 1 thread
✅ Test 6: process_snapshot(name) — 16 python processes found
✅ Test 7: top_processes       — Top 5 by memory, OrbStack 1170MB
✅ Test 8: system_snapshot+JSON — 12387 chars, uptime 1.2h, 536 procs
✅ Test 9: watch_resource      — 4 snapshots in 2s
ALL 9/9 PASSED
```

## JSON 快照样例（截取）
```json
{
  "timestamp": "2026-05-06T...",
  "uptime_hours": 1.2,
  "process_count": 536,
  "cpu": {"cpu_percent": 32.2, "cpu_count_logical": 10},
  "memory": {"ram": {"total_gb": 16.0, "percent": 80.3}},
  "disk": {"partitions": [{"mountpoint": "/", "total_gb": 460.43, "percent": 96.3}]},
  "network": {"io": {"bytes_sent_mb": 490.13, "bytes_recv_mb": 791.58}}
}
```
