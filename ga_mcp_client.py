#!/usr/bin/env python3
"""
GA MCP Client - 统一的MCP调用模块，支持stdio/http/sse三种transport。

使用方式:
    from ga_mcp_client import MCPClient

    async with MCPClient("trendradar") as client:
        tools = await client.list_tools()
        result = await client.call_tool("get_latest_news", {"limit": 10})

配置: mcp_servers.json (与本文件同目录)
选择逻辑:
    - 有 url 字段 → http (Streamable HTTP)
    - 有 command 字段 → stdio (子进程)
    - transport="sse" → SSE (旧版兼容)
"""

import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "mcp_servers.json"


def _load_config() -> dict[str, Any]:
    """加载mcp_servers.json配置"""
    with open(CONFIG_PATH) as f:
        return json.load(f)["mcpServers"]


def _resolve_env(env_dict: dict[str, str] | None) -> dict[str, str] | None:
    """解析环境变量中的 ${VAR} 引用"""
    if not env_dict:
        return None
    resolved = {}
    for k, v in env_dict.items():
        if v.startswith("${") and v.endswith("}"):
            env_name = v[2:-1]
            resolved[k] = os.environ.get(env_name, "")
        else:
            resolved[k] = v
    return {**os.environ, **resolved}


def _detect_transport(config: dict[str, Any]) -> str:
    """根据配置自动检测transport类型"""
    explicit = config.get("transport")
    if explicit:
        return explicit  # "stdio" / "http" / "sse"
    if "url" in config:
        return "http"
    if "command" in config:
        return "stdio"
    raise ValueError(f"Cannot detect transport from config: {config}")


class MCPClient:
    """统一MCP Client，支持 async with 上下文管理"""

    def __init__(self, server_name: str, config: dict[str, Any] | None = None):
        """
        Args:
            server_name: mcp_servers.json中的server名称
            config: 可选，直接传入配置dict（跳过文件加载）
        """
        self.name = server_name
        if config is None:
            all_cfg = _load_config()
            if server_name not in all_cfg:
                raise KeyError(f"Server '{server_name}' not found in {CONFIG_PATH}")
            config = all_cfg[server_name]
        self.config = config
        self.transport_type = _detect_transport(config)
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    async def connect(self) -> None:
        """建立连接"""
        if self.transport_type == "stdio":
            await self._connect_stdio()
        elif self.transport_type == "http":
            await self._connect_http()
        elif self.transport_type == "sse":
            await self._connect_sse()
        else:
            raise ValueError(f"Unknown transport: {self.transport_type}")
        logger.info(f"[MCP] Connected to '{self.name}' via {self.transport_type}")

    async def _connect_stdio(self) -> None:
        command = self.config["command"]
        if command in ("npx", "uvx"):
            command = shutil.which(command) or command
        params = StdioServerParameters(
            command=command,
            args=self.config.get("args", []),
            env=_resolve_env(self.config.get("env")),
        )
        transport = await self._stack.enter_async_context(stdio_client(params))
        read, write = transport
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.session = session

    async def _connect_http(self) -> None:
        url = self.config["url"]
        transport = await self._stack.enter_async_context(streamablehttp_client(url))
        read, write, _get_session_id = transport
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.session = session

    async def _connect_sse(self) -> None:
        url = self.config["url"]
        transport = await self._stack.enter_async_context(sse_client(url))
        read, write = transport
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.session = session

    async def disconnect(self) -> None:
        """断开连接并清理资源"""
        try:
            await self._stack.aclose()
        except Exception as e:
            logger.warning(f"[MCP] Cleanup error for '{self.name}': {e}")
        self.session = None

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出server提供的所有工具"""
        if not self.session:
            raise RuntimeError(f"Not connected to '{self.name}'")
        resp = await self.session.list_tools()
        tools = []
        for tool in resp.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
            })
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None,
                        retries: int = 2, delay: float = 1.0) -> list[dict[str, Any]]:
        """调用工具，带重试机制。返回content列表(每项含type/text等)"""
        if not self.session:
            raise RuntimeError(f"Not connected to '{self.name}'")
        arguments = arguments or {}
        for attempt in range(1, retries + 1):
            try:
                result = await self.session.call_tool(tool_name, arguments)
                # CallToolResult.content -> list[TextContent|ImageContent|...]
                contents = []
                for item in result.content:
                    entry = {"type": item.type}
                    if hasattr(item, "text"):
                        entry["text"] = item.text
                    if hasattr(item, "data"):
                        entry["data"] = item.data
                    if hasattr(item, "mimeType"):
                        entry["mimeType"] = item.mimeType
                    contents.append(entry)
                return contents
            except Exception as e:
                logger.warning(f"[MCP] {tool_name} attempt {attempt}/{retries} failed: {e}")
                if attempt < retries:
                    await asyncio.sleep(delay)
                else:
                    raise


# ─── 便捷同步接口（供非async场景快速调用） ───

def call_sync(server_name: str, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
    """同步调用MCP工具（内部创建event loop）"""
    async def _run():
        async with MCPClient(server_name) as client:
            return await client.call_tool(tool_name, arguments)
    return asyncio.run(_run())


def list_tools_sync(server_name: str) -> list[dict[str, Any]]:
    """同步列出工具"""
    async def _run():
        async with MCPClient(server_name) as client:
            return await client.list_tools()
    return asyncio.run(_run())


# ─── CLI 测试入口 ───

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python ga_mcp_client.py <server_name> [tool_name] [args_json]")
        print(f"\nConfigured servers: {list(_load_config().keys())}")
        sys.exit(0)

    server = sys.argv[1]
    if len(sys.argv) == 2:
        # 仅列出工具
        tools = list_tools_sync(server)
        print(f"\n{'='*60}")
        print(f"Server: {server} | Tools: {len(tools)}")
        print(f"{'='*60}")
        for t in tools:
            print(f"  - {t['name']}: {t['description'][:80]}")
    else:
        tool = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        result = call_sync(server, tool, args)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
