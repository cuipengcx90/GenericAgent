#!/usr/bin/env python3
"""GenericAgent MCP Server - 将GA核心能力暴露给Claude Desktop"""
import sys, os, json, subprocess
sys.path.insert(0, '/Users/cuip/projects/GenericAgent')

# MCP stdio protocol implementation (minimal, no deps)
def send(obj):
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

def handle(req):
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{
            "protocolVersion":"2024-11-05",
            "capabilities":{"tools":{}},
            "serverInfo":{"name":"genericagent","version":"1.0"}
        }})

    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":rid,"result":{"tools":[
            {
                "name": "notify_feishu",
                "description": "推送文本消息到飞书私信（GenericAgent用户）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要推送的文本内容"}
                    },
                    "required": ["text"]
                }
            },
            {
                "name": "notify_feishu_group",
                "description": "推送文本消息到飞书群（家庭群/工作群）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要推送的文本内容"}
                    },
                    "required": ["text"]
                }
            },
            {
                "name": "list_scheduled_tasks",
                "description": "列出GenericAgent所有定时任务及其状态（启用/禁用/下次执行时间）",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "run_agent_task",
                "description": "向GenericAgent提交一个自然语言任务，后台异步执行，返回任务ID",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "任务描述（自然语言）"},
                        "task_name": {"type": "string", "description": "任务名称（用于标识，无空格）"}
                    },
                    "required": ["prompt", "task_name"]
                }
            },
            {
                "name": "run_health_check",
                "description": "运行GenericAgent健康检查，返回各reflect脚本的依赖/执行状态/配置完整性JSON报告",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            }
        ]}})

    elif method == "tools/call":
        name = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments", {})
        try:
            result = call_tool(name, args)
            send({"jsonrpc":"2.0","id":rid,"result":{
                "content":[{"type":"text","text":result}]
            }})
        except Exception as e:
            send({"jsonrpc":"2.0","id":rid,"result":{
                "content":[{"type":"text","text":f"[ERROR] {e}"}],
                "isError": True
            }})

    elif method == "notifications/initialized":
        pass  # no response needed

    else:
        if rid is not None:
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}})


def call_tool(name, args):
    if name == "notify_feishu":
        from reflect_scripts.reflect_common import notify_feishu
        notify_feishu(args["text"])
        return f"已推送到飞书私信: {args['text'][:50]}..."

    elif name == "notify_feishu_group":
        from reflect_scripts.notify_feishu_group import notify_group
        resp = notify_group(args["text"])
        return f"已推送到飞书群，响应: {resp}"

    elif name == "list_scheduled_tasks":
        import glob
        tasks = []
        for path in sorted(glob.glob('/Users/cuip/projects/GenericAgent/sche_tasks/*.json')):
            try:
                with open(path) as f:
                    t = json.load(f)
                name_t = os.path.basename(path).replace('.json','')
                tasks.append(f"{'✅' if t.get('enabled') else '❌'} {name_t} | {t.get('schedule','?')} {t.get('repeat','?')}")
            except:
                pass
        return "\n".join(tasks) if tasks else "无定时任务"

    elif name == "run_agent_task":
        task_name = args["task_name"]
        prompt = args["prompt"]
        task_dir = f"/Users/cuip/projects/GenericAgent/temp/{task_name}"
        os.makedirs(task_dir, exist_ok=True)
        with open(f"{task_dir}/input.txt", "w") as f:
            f.write(prompt)
        proc = subprocess.Popen(
            [sys.executable, "/Users/cuip/projects/GenericAgent/agentmain.py", "--task", task_name],
            cwd="/Users/cuip/projects/GenericAgent",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return f"任务已提交，PID={proc.pid}，task_name={task_name}，输出在 temp/{task_name}/output.txt"

    elif name == "run_health_check":
        r = subprocess.run(
            [sys.executable, "/Users/cuip/projects/GenericAgent/reflect_scripts/health_check.py", "--json"],
            capture_output=True, text=True, timeout=30,
            cwd="/Users/cuip/projects/GenericAgent"
        )
        try:
            data = json.loads(r.stdout)
            summary = data.get("summary", {})
            results = data.get("results", [])
            lines = [f"健康检查结果 (exit={r.returncode}, 生成时间: {data.get('generated_at','')})"]
            lines.append(f"总计: {summary.get('total',0)} | 活跃: {summary.get('active',0)} | 僵尸/从未: {summary.get('zombie_or_never',0)} | 未配置: {summary.get('unconfigured',0)} | 依赖缺失: {summary.get('broken',0)}")
            for s in results:
                status = s.get("status", "?")
                icon = "✅" if "活跃" in status else "⚠️"
                lines.append(f"{icon} {s.get('script','?')} | {status} | last_run: {s.get('last_run','N/A')}")
            return "\n".join(lines)
        except Exception:
            return r.stdout or r.stderr or f"exit={r.returncode}"

    else:
        raise ValueError(f"未知工具: {name}")


def main():
    import io
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            handle(req)
        except Exception as e:
            sys.stderr.write(f"[ga_mcp] parse error: {e}\n")

if __name__ == "__main__":
    main()
