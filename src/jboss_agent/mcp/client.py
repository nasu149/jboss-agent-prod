"""Fake JBoss MCP サーバーを起動し、LangGraph から使う Tool を取得する。"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

READ_TOOLS = frozenset(
    {"read_server_log", "get_thread_pool_status", "get_datasource_status", "get_deployment_status"}
)
WRITE_TOOLS = frozenset(
    {"set_thread_pool_max_threads", "set_datasource_max_pool_size", "restart_deployment"}
)


def build_mcp_client() -> MultiServerMCPClient:
    """別 Python プロセスの MCP サーバーへ stdio で接続するクライアントを作る。"""
    return MultiServerMCPClient(
        {
            "fake_jboss": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "jboss_agent.mcp.server"],
            }
        }
    )


async def load_jboss_tools() -> tuple[list[Any], list[Any]]:
    """MCP サーバーから Tool 一覧を取得し、read と write に分けて返す。"""
    tools = list(await build_mcp_client().get_tools())
    names = {tool.name for tool in tools}
    expected = READ_TOOLS | WRITE_TOOLS
    if names != expected:
        raise RuntimeError(f"unexpected MCP tools: expected={sorted(expected)}, actual={sorted(names)}")
    return (
        [tool for tool in tools if tool.name in READ_TOOLS],
        [tool for tool in tools if tool.name in WRITE_TOOLS],
    )


def by_name(tools: Iterable[Any], name: str) -> Any:
    """Tool 一覧から名前が一致するものを返す。"""
    for tool in tools:
        if tool.name == name:
            return tool
    raise ValueError(f"tool not found: {name}")


def as_dict(value: Any) -> dict[str, Any]:
    """MCP adapter の代表的な戻り値を、このデモで扱う辞書へ正規化する。

    ``langchain-mcp-adapters`` の呼び方やバージョン差により、dict / JSON 文字列 /
    content block list / ToolMessage 風オブジェクトのいずれかが返り得る。その転送形式の
    差だけを吸収し、Graph 側を MCP の細部で複雑にしない。
    """
    if isinstance(value, dict):
        structured = value.get("structured_content")
        if isinstance(structured, dict):
            return structured
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return as_dict(value["text"])
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    if isinstance(value, (list, tuple)):
        text_parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                structured = item.get("structured_content")
                if isinstance(structured, dict):
                    return structured
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif isinstance(item, str):
                text_parts.append(item)
        if text_parts:
            return as_dict("\n".join(text_parts))
        return {"raw": value}

    artifact = getattr(value, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        if isinstance(structured, dict):
            return structured

    content = getattr(value, "content", None)
    if content is not None:
        return as_dict(content)

    return {"raw": value}
