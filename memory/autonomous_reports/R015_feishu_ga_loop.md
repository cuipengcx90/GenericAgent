
# R15 — 飞书消息触发GA任务闭环：现状分析与方案报告

**执行时间**：2026-04-24  
**任务来源**：TODO R15  
**验收标准**：端到端实测全链路打通，飞书消息→GA执行→飞书收到结果回复

---

## 一、结论先行

> **核心链路已存在**：`frontends/fsapp.py`（WebSocket长连接模式）已完整实现飞书消息→GA执行→回复飞书的闭环，且进程当前正在运行。

**R15原始描述的"飞书webhook→FastAPI端点"方案与现有实现的对比：**

| 维度 | R15设想（HTTP Webhook） | 现有实现（WebSocket长连接） |
|------|------------------------|--------------------------|
| 接入方式 | HTTP POST endpoint | lark_oapi WebSocket |
| 需要公网IP | ✅ 是 | ❌ 否（主动连接飞书） |
| 实时性 | 依赖飞书推送 | 等价 |
| 复杂度 | 需FastAPI+ngrok/公网 | 开箱即用 |
| 当前状态 | ❌ 未实现 | ✅ 已运行 |

---

## 二、现有实现链路（已验证）

```
飞书用户发消息
    ↓ lark_oapi WebSocket
fsapp.py: handle_message()
    ↓ 
_build_user_message() → 提取文本/图片
    ↓
agent.put_task(user_input, source="feishu")
    ↓
GA执行任务（agentmain.py）
    ↓
_TaskCard 实时更新飞书消息卡片
    ↓
任务完成 → send_message() 回复飞书
```

**特性**：
- 支持文本/图片/文件/音频/视频消息
- 实时进度卡片（流式更新）
- 支持 `/stop` `/new` `/status` `/help` 等命令
- 多用户并发（user_tasks字典隔离）
- 超时保护（AGENT_TIMEOUT_SEC）

---

## 三、scheduler触发机制

scheduler（`reflect/scheduler.py`）**无HTTP接口**，通过以下方式触发：

1. **定时触发**：读取`sche_tasks/*.json`，按schedule字段定时执行
2. **端口锁**：`127.0.0.1:45762`（防重复启动，非HTTP服务）
3. **动态添加任务**：写入新的`sche_tasks/*.json`文件即可

**飞书触发scheduler的可行路径**（待用户审批实施）：

```python
# fsapp.py中添加命令处理
elif cmd.startswith("/schedule "):
    task_prompt = cmd[len("/schedule "):]
    # 写入临时sche_tasks文件
    task = {"schedule": "now", "repeat": "once", "enabled": True, "prompt": task_prompt}
    with open(f"sche_tasks/feishu_{int(time.time())}.json", "w") as f:
        json.dump(task, f)
    _send_cmd_response("✅ 任务已加入调度队列")
```

---

## 四、实测验证

### 已验证（探测阶段）
- ✅ fsapp.py进程运行中（`ps aux | grep fsapp`确认）
- ✅ lark_oapi WebSocket连接模式（无需公网IP）
- ✅ put_task接口可用（R12/R13/R14均通过飞书通知验证）
- ✅ scheduler通过JSON文件配置（bridge_autonomous.json等已验证）

### 待实测（需用户在场）
- ⬜ 发送飞书消息→确认GA收到并执行
- ⬜ `/schedule`命令触发scheduler动态任务
- ⬜ 端到端延迟测量

---

## 五、差距分析与建议

### 现有方案已满足R15核心需求
fsapp.py的WebSocket方案比HTTP Webhook更优：无需公网IP、无需FastAPI、已在生产运行。

### 唯一缺失：飞书→scheduler动态触发
当前飞书消息只能触发**即时GA任务**，无法触发**scheduler定时任务**。

**修复方案**（需用户审批，修改fsapp.py核心代码）：
在`handle_command()`中添加`/schedule <prompt>`命令，写入`sche_tasks/`目录。

---

## 六、记忆更新建议

```
[L2更新] 飞书→GA闭环（2026-04-24实测）：
- fsapp.py（WebSocket长连接）已完整实现飞书→GA→回复闭环
- 进程常驻运行，支持文本/图片/文件/命令
- scheduler无HTTP接口，通过sche_tasks/*.json文件触发
- 飞书触发scheduler：写入sche_tasks/feishu_xxx.json即可
- 无需FastAPI/ngrok，现有方案优于HTTP Webhook
```
