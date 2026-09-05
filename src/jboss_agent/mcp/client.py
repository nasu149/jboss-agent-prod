"""LangChain adapter for the separate Fake JBoss MCP server process."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any, Protocol

from langchain_mcp_adapters.client import MultiServerMCPClient


READ_TOOL_NAMES = frozenset(
    {
        "read_server_log",
        "get_server_health",
        "get_thread_pool_status",
        "get_datasource_status",
        "get_deployment_status",
        "get_recent_config_changes",
    }
)
WRITE_TOOL_NAMES = frozenset(
    {
        "set_thread_pool_max_threads",
        "set_datasource_max_pool_size",
        "restart_deployment",
        "reload_server",
    }
)
FORBIDDEN_TOOL_NAMES = frozenset({"execute_jboss_cli", "execute_shell"})


class NamedTool(Protocol):
    name: str


def _names(tools: Iterable[NamedTool]) -> set[str]:
    return {tool.name for tool in tools}


def validate_toolset(tools: Iterable[NamedTool]) -> None:
    names = _names(tools)
    expected = READ_TOOL_NAMES | WRITE_TOOL_NAMES
    if forbidden := names & FORBIDDEN_TOOL_NAMES:
        raise RuntimeError(f"Forbidden generic MCP tools exposed: {sorted(forbidden)}")
    if missing := expected - names:
        raise RuntimeError(f"Fake JBoss MCP server is missing tools: {sorted(missing)}")
    if unexpected := names - expected:
        raise RuntimeError(f"Unexpected MCP tools exposed: {sorted(unexpected)}")


def build_mcp_client() -> MultiServerMCPClient:
    """Start the local MCP server over stdio when tools are requested."""
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
    """Return strict read-only and write-only tool sets."""
    client = build_mcp_client()
    all_tools = list(await client.get_tools())
    validate_toolset(all_tools)

    read_tools = [tool for tool in all_tools if tool.name in READ_TOOL_NAMES]
    write_tools = [tool for tool in all_tools if tool.name in WRITE_TOOL_NAMES]

    if _names(read_tools) != READ_TOOL_NAMES:
        raise RuntimeError("Read-only MCP tool set is incomplete")
    if _names(write_tools) != WRITE_TOOL_NAMES:
        raise RuntimeError("Write MCP tool set is incomplete")
    return read_tools, write_tools
