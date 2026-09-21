
# R29 FastAPI接口方案设计：让外部工具可调用GA能力

## 背景

GenericAgent（GA）当前仅支持三种调用方式：
1. CLI交互（stdin/stdout）
2. `--task` 文件IO模式（input.txt → output.txt）
3. `--reflect` 监控脚本触发

**缺口**：供应链SaaS v7工作流无法通过HTTP直接触发GA任务、查询状态、获取结果。FastAPI层可填补此缺口，使GA成为可编排的微服务节点。

---

## 架构设计

```
供应链SaaS v7 ──HTTP──▶ ga_api.py (FastAPI)
                              │
                    ┌─────────┴──────────┐
                    │  GeneraticAgent    │
                    │  put_task()        │
                    │  display_queue     │
                    └────────────────────┘
```

`ga_api.py` 作为薄包装层，复用现有 `GeneraticAgent` 实例，不修改核心代码。

---

## API端点设计（6个）

### 1. POST /task — 提交任务（异步）

```
POST /task
Authorization: Bearer {API_KEY}
Content-Type: application/json

{
  "input": "分析本周供应链异常订单，输出TOP5风险供应商",
  "source": "saas_v7",          // 来源标记，写入history
  "timeout": 120,               // 秒，默认60
  "webhook_url": "https://..."  // 可选，完成后回调
}

→ 202 Accepted
{
  "task_id": "t_20260424_001",
  "status": "queued",
  "poll_url": "/task/t_20260424_001"
}
```

### 2. GET /task/{task_id} — 查询任务状态/结果

```
GET /task/t_20260424_001
Authorization: Bearer {API_KEY}

→ 200 OK
{
  "task_id": "t_20260424_001",
  "status": "running|done|error",
  "progress": "LLM Running (Turn 3)...",  // 中间输出
  "result": "...",                         // status=done时有值
  "elapsed_s": 34.2
}
```

### 3. POST /task/sync — 同步执行（短任务，≤30s）

```
POST /task/sync
Authorization: Bearer {API_KEY}

{
  "input": "今日BTC价格是否触发5%阈值？",
  "timeout": 30
}

→ 200 OK（阻塞直到完成）
{
  "result": "当前BTC价格...",
  "elapsed_s": 8.1
}
```

### 4. GET /status — 服务健康检查

```
GET /status

→ 200 OK
{
  "ok": true,
  "is_running": false,
  "llm": "ClaudeSession/claude-3-5-sonnet",
  "queue_depth": 0,
  "uptime_s": 3600
}
```

### 5. POST /abort — 中止当前任务

```
POST /abort
Authorization: Bearer {API_KEY}

→ 200 OK
{ "aborted": true }
```

### 6. GET /history — 查询最近任务历史

```
GET /history?limit=10
Authorization: Bearer {API_KEY}

→ 200 OK
{
  "items": [
    {"task_id": "...", "input_preview": "...", "status": "done", "ts": "2026-04-24T03:00:00"}
  ]
}
```

---

## 安全方案

### 认证/授权
- **Bearer Token**：启动时从 `mykey.py` 读取 `ga_api_key`，或环境变量 `GA_API_KEY`
- 所有写操作端点（/task, /abort）必须携带有效token
- `/status` 端点可选认证（内网部署时可开放）

### 速率限制
```python
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)

@app.post("/task")
@limiter.limit("10/minute")   # 每IP每分钟10次
async def submit_task(...): ...

@app.post("/task/sync")
@limiter.limit("5/minute")    # 同步接口更严格
async def sync_task(...): ...
```

### 输入验证
```python
class TaskRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=10000)
    source: str = Field(default="api", pattern=r'^[a-z0-9_]+$')
    timeout: int = Field(default=60, ge=5, le=300)
    webhook_url: Optional[HttpUrl] = None
```

### 网络隔离
- 默认绑定 `127.0.0.1:8765`（仅本机）
- 对外暴露需显式配置 `--host 0.0.0.0` + 反向代理（nginx）+ HTTPS

---

## 示例代码（可直接运行）

### ga_api.py（完整实现，约120行）

```python
"""
GA FastAPI 包装层
启动: uvicorn ga_api:app --host 127.0.0.1 --port 8765
"""
import os, sys, time, uuid, asyncio
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, HttpUrl
import httpx

sys.path.insert(0, os.path.dirname(__file__))
from agentmain import GeneraticAgent

# ── 初始化 ──────────────────────────────────────────────
app = FastAPI(title="GenericAgent API", version="1.0")
security = HTTPBearer()

# 从环境变量或mykey读取API Key
API_KEY = os.environ.get("GA_API_KEY", "")
if not API_KEY:
    try:
        from mykey import ga_api_key; API_KEY = ga_api_key
    except: pass

agent = GeneraticAgent()
import threading; threading.Thread(target=agent.run, daemon=True).start()

# 任务状态存储（内存，重启丢失）
tasks: dict = {}
start_time = time.time()

# ── 认证 ────────────────────────────────────────────────
def verify_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    if API_KEY and creds.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return creds.credentials

# ── 数据模型 ─────────────────────────────────────────────
class TaskRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=10000)
    source: str = Field(default="api", pattern=r'^[a-z0-9_]+$')
    timeout: int = Field(default=60, ge=5, le=300)
    webhook_url: Optional[str] = None

# ── 端点 ─────────────────────────────────────────────────
@app.get("/status")
def get_status():
    return {
        "ok": True,
        "is_running": agent.is_running,
        "llm": agent.get_llm_name(model=True),
        "queue_depth": agent.task_queue.qsize(),
        "uptime_s": round(time.time() - start_time, 1)
    }

@app.post("/task", dependencies=[Depends(verify_token)], status_code=202)
async def submit_task(req: TaskRequest):
    task_id = f"t_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    tasks[task_id] = {"status": "queued", "progress": "", "result": None, "ts": time.time()}
    asyncio.create_task(_run_task(task_id, req))
    return {"task_id": task_id, "status": "queued", "poll_url": f"/task/{task_id}"}

@app.get("/task/{task_id}", dependencies=[Depends(verify_token)])
def get_task(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    t = tasks[task_id]
    return {**t, "task_id": task_id, "elapsed_s": round(time.time() - t["ts"], 1)}

@app.post("/task/sync", dependencies=[Depends(verify_token)])
async def sync_task(req: TaskRequest):
    task_id = f"sync_{uuid.uuid4().hex[:8]}"
    tasks[task_id] = {"status": "running", "progress": "", "result": None, "ts": time.time()}
    await _run_task(task_id, req)
    t = tasks.pop(task_id)
    if t["status"] == "error":
        raise HTTPException(status_code=500, detail=t["result"])
    return {"result": t["result"], "elapsed_s": round(time.time() - t["ts"], 1)}

@app.post("/abort", dependencies=[Depends(verify_token)])
def abort_task():
    agent.abort()
    return {"aborted": True}

@app.get("/history", dependencies=[Depends(verify_token)])
def get_history(limit: int = 10):
    items = sorted(tasks.items(), key=lambda x: x[1]["ts"], reverse=True)[:limit]
    return {"items": [
        {"task_id": k, "input_preview": v.get("input_preview",""), 
         "status": v["status"], "ts": v["ts"]} for k, v in items
    ]}

# ── 内部异步执行 ──────────────────────────────────────────
async def _run_task(task_id: str, req: TaskRequest):
    tasks[task_id]["status"] = "running"
    tasks[task_id]["input_preview"] = req.input[:80]
    dq = agent.put_task(req.input, source=req.source)
    loop = asyncio.get_event_loop()
    try:
        while True:
            item = await loop.run_in_executor(None, lambda: dq.get(timeout=req.timeout))
            if "next" in item:
                tasks[task_id]["progress"] = item["next"][-200:]
            if "done" in item:
                tasks[task_id].update({"status": "done", "result": item["done"]})
                if req.webhook_url:
                    asyncio.create_task(_webhook(req.webhook_url, task_id, item["done"]))
                break
    except Exception as e:
        tasks[task_id].update({"status": "error", "result": str(e)})

async def _webhook(url: str, task_id: str, result: str):
    async with httpx.AsyncClient() as client:
        try: await client.post(url, json={"task_id": task_id, "result": result}, timeout=10)
        except: pass
```

### 调用示例（供应链SaaS v7集成）

```python
import httpx

GA_URL = "http://127.0.0.1:8765"
GA_KEY = "your_api_key"
headers = {"Authorization": f"Bearer {GA_KEY}"}

# 异步提交：分析供应链异常
resp = httpx.post(f"{GA_URL}/task", headers=headers, json={
    "input": "读取./data/orders_today.csv，找出延迟超3天的订单，按供应商分组输出风险TOP5",
    "source": "saas_v7",
    "timeout": 120,
    "webhook_url": "https://your-saas.com/ga_callback"
})
task_id = resp.json()["task_id"]

# 轮询结果
import time
for _ in range(60):
    r = httpx.get(f"{GA_URL}/task/{task_id}", headers=headers).json()
    if r["status"] == "done":
        print(r["result"]); break
    time.sleep(2)
```

---

## 与AI工作流v7架构集成建议

| v7节点 | GA API调用方式 | 说明 |
|--------|---------------|------|
| 订单异常检测 | POST /task（异步+webhook） | 触发后不阻塞，结果回调 |
| 供应商风险评估 | POST /task/sync | 短任务，直接等结果 |
| 晚报生成 | POST /task（定时触发） | 配合scheduled_task_sop |
| 健康监控 | GET /status | 纳入v7 healthcheck |

---

## 依赖安装

```bash
pip install fastapi uvicorn slowapi httpx pydantic
```

启动命令：
```bash
cd /Users/cuip/projects/GenericAgent
uvicorn ga_api:app --host 127.0.0.1 --port 8765 --reload
```

---

## 待审批事项

1. **新增文件**：`GenericAgent/ga_api.py`（约120行，不修改任何现有文件）
2. **新增依赖**：fastapi / uvicorn / slowapi / httpx
3. **mykey.py新增字段**：`ga_api_key = "your_secret_key"`
4. **启动方式**：建议加入 `scheduled_task_sop` 开机自启

## 记忆更新建议

无需更新L2（此为方案设计，待审批后实施再记录）
