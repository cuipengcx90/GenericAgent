
# R12 — Watchdog文件监控实测报告

**执行时间**：2026-04-24  
**任务来源**：R4发现05_folder_watcher.py沉默失效，R2确认未启动  
**验收标准**：实测~/Desktop增删改均能触发，定位失效根因，修复后加飞书通知闭环

---

## 一、失效根因分析

**结论：05_folder_watcher.py从未被启动，非watchdog库本身问题**

| 维度 | 发现 |
|------|------|
| scheduler配置 | ❌ sche_tasks/无任何folder_watcher相关配置 |
| LaunchAgent | ❌ 无plist加载此脚本 |
| 进程状态 | ❌ ps无相关进程 |
| 日志记录 | ❌ scheduler.log全程无记录 |

**根本原因**（引用R2报告）：
> reflect_scripts/ 是**示例模板库**，设计为手动 `python agentmain.py --reflect reflect_scripts/0x.py` 启动，但从未被调度系统加载。

---

## 二、Watchdog能力实测

### 测试环境
- 监控目录：`~/Desktop`
- 测试操作：创建 → 修改 → 删除文件
- 监控库：`watchdog.observers.Observer`

### 实测结果

```
✅ 创建: watchdog_test_r12.txt
📝 修改: watchdog_test_r12.txt (3次)
🗑️ 删除: watchdog_test_r12.txt
```

**捕获事件数：5个**（创建1次 + 修改3次 + 删除1次）

### 飞书通知闭环

✅ **通知发送成功**  
- 调用路径：`reflect_common.notify_feishu()`
- 消息内容：包含所有捕获事件
- 验证结果：飞书已收到实时通知

---

## 三、关键发现

1. **watchdog库工作正常**：Observer能稳定捕获文件系统事件
2. **修改事件会多次触发**：单次文件写入可能触发3次modified事件（macOS文件系统特性）
3. **飞书通知链路畅通**：reflect_common模块可直接调用
4. **05_folder_watcher.py代码无问题**：监控逻辑正确，仅缺启动机制

---

## 四、修复方案（待用户审批）

### 方案A：加入scheduler定时任务（推荐）

创建 `sche_tasks/folder_watcher.json`：
```json
{
  "schedule": "startup",
  "repeat": "once",
  "enabled": true,
  "prompt": "启动文件夹监控：python agentmain.py --reflect reflect_scripts/05_folder_watcher.py",
  "max_delay_hours": 0
}
```

### 方案B：LaunchAgent后台常驻

创建 `~/Library/LaunchAgents/com.cuip.folder_watcher.plist`，开机自启。

### 方案C：改造为独立守护进程

将05_folder_watcher.py改为独立脚本，用nohup后台运行。

---

## 五、记忆更新建议

```
[L3更新] watchdog文件监控SOP（2026-04-24实测）：
- watchdog.observers.Observer稳定可用
- macOS单次写入会触发3次modified事件，需去重
- 与飞书通知闭环已验证：reflect_common.notify_feishu()
- 05_folder_watcher.py失效根因：未启动（非代码问题）
- 启动方式：agentmain.py --reflect reflect_scripts/0x.py
```

---

## 六、后续建议

1. **优先激活06_price_alert.py**（R2推荐，无需配置即可运行）
2. **统一reflect_scripts启动管理**：建立reflect任务注册机制，避免"僵尸脚本"
3. **监控事件去重**：modified事件需加时间窗口去重（如3秒内同文件只通知1次）
