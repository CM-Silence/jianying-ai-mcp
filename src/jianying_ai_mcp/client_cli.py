"""First-party MCP client for a host that can run commands but has no connector."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .openfilm_runtime import runtime_home
from .config import MCP_CALL_TIMEOUT_SECONDS


def _installed_entry(config: Path | None) -> dict:
    path = config or runtime_home() / "mcp-stdio.json"
    if config is None and not path.is_file():
        return {"command": sys.executable, "args": ["-m", "jianying_ai_mcp"], "env": {}}
    return json.loads(path.read_text(encoding="utf-8-sig"))["mcpServers"]["jianying-ai"]


def serve_http(server, argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="jianying-ai-mcp serve-http")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 需要 1–65535 的整数")
    os.environ.update(_installed_entry(args.config).get("env", {}))
    server.settings.host = "127.0.0.1"
    server.settings.port = args.port
    server.run(transport="streamable-http")


async def _request(args, action: str, arguments: dict) -> dict:
    timeout = timedelta(seconds=args.timeout)
    if args.url:
        transport = streamablehttp_client(args.url, timeout=timeout, sse_read_timeout=timeout)
    else:
        entry = _installed_entry(args.config)
        transport = stdio_client(StdioServerParameters(
            command=entry["command"], args=entry.get("args", []), cwd=entry.get("cwd"),
            env={**os.environ, **entry.get("env", {}), "JIANYING_MCP_TRANSPORT": "stdio"},
        ))
    async with transport as streams, ClientSession(*streams[:2], read_timeout_seconds=timeout) as client:
        await client.initialize()
        if action == "tools":
            catalog = []
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    if tool.name == args.name:
                        return tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    catalog.append(tool.name)
                cursor = page.nextCursor
                if not cursor:
                    break
            if args.name:
                raise ValueError(f"没有找到工具 {args.name}")
            return {"tools": catalog}
        reply = await client.call_tool(args.name, arguments)
        if reply.isError:
            return {"success": False, "error": " ".join(block.text for block in reply.content if block.type == "text")}
        if reply.structuredContent is not None:
            return reply.structuredContent
        return json.loads(reply.content[0].text)


def run_client(action: str, argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog=f"jianying-ai-mcp {action}")
    parser.add_argument("name", nargs="?" if action == "tools" else None)
    if action == "call":
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--args", help="JSON 对象")
        group.add_argument("--args-file", help="UTF-8 JSON 文件，传 - 从标准输入读取")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--config", type=Path, help="stdio 客户端配置，默认读取安装器生成的配置")
    transport.add_argument("--url", help="运行中的 Streamable HTTP 服务，例如 http://127.0.0.1:8000/mcp")
    parser.add_argument("--timeout", type=int, default=MCP_CALL_TIMEOUT_SECONDS, help="请求超时秒数")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 需要为正数")
    try:
        arguments = {}
        if action == "call":
            raw = args.args or "{}"
            if args.args_file:
                raw = (sys.stdin.buffer.read().decode("utf-8-sig") if args.args_file == "-" else
                       Path(args.args_file).read_text(encoding="utf-8-sig"))
            arguments = json.loads(raw)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
        result = asyncio.run(_request(args, action, arguments))
    except Exception as exc:
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("success") is False:
        raise SystemExit(1)
