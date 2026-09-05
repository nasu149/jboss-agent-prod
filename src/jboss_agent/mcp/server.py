"""Fake JBoss MCP server exposing explicit capabilities over stdio."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jboss_agent.config import get_settings
from jboss_agent.fake_jboss import FakeJBossOperations


mcp = FastMCP("Fake JBoss Capability API")


def _ops() -> FakeJBossOperations:
    settings = get_settings()
    return FakeJBossOperations(settings.fake_jboss_data_dir, server_id=settings.server_id)


# Read-only capabilities: these are the only tools bound to the investigation LLM.
@mcp.tool()
def read_server_log(server_id: str, cursor: int) -> dict[str, object]:
    """Read server.log lines added after a byte cursor."""
    return _ops().read_server_log(server_id, cursor)


@mcp.tool()
def get_server_health(server_id: str) -> dict[str, object]:
    """Read high-level server health metrics."""
    return _ops().get_server_health(server_id)


@mcp.tool()
def get_thread_pool_status(server_id: str) -> dict[str, object]:
    """Read worker thread-pool metrics."""
    return _ops().get_thread_pool_status(server_id)


@mcp.tool()
def get_datasource_status(server_id: str) -> dict[str, object]:
    """Read datasource connection-pool metrics."""
    return _ops().get_datasource_status(server_id)


@mcp.tool()
def get_deployment_status(server_id: str) -> dict[str, object]:
    """Read deployment state."""
    return _ops().get_deployment_status(server_id)


@mcp.tool()
def get_recent_config_changes(server_id: str) -> dict[str, object]:
    """Read recent configuration changes."""
    return _ops().get_recent_config_changes(server_id)


# Write capabilities: never bound to the investigation LLM. Python selects one
# only after policy validation + human approval.
@mcp.tool()
def set_thread_pool_max_threads(server_id: str, value: int) -> dict[str, object]:
    """Set worker max_threads after approval."""
    return _ops().set_thread_pool_max_threads(server_id, value)


@mcp.tool()
def set_datasource_max_pool_size(server_id: str, value: int) -> dict[str, object]:
    """Set datasource max_pool_size after approval."""
    return _ops().set_datasource_max_pool_size(server_id, value)


@mcp.tool()
def restart_deployment(server_id: str, deployment_name: str) -> dict[str, object]:
    """Restart one deployment after approval."""
    return _ops().restart_deployment(server_id, deployment_name)


@mcp.tool()
def reload_server(server_id: str) -> dict[str, object]:
    """Reload the fake server after approval."""
    return _ops().reload_server(server_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
