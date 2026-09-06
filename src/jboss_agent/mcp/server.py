"""Fake JBoss の read/write 操作を MCP Tool として公開するサーバー。"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jboss_agent.config import get_settings
from jboss_agent.fake_jboss import FakeJBoss

mcp = FastMCP("Fake JBoss")


def _jboss() -> FakeJBoss:
    settings = get_settings()
    return FakeJBoss(settings.fake_jboss_data_dir, settings.server_id)


@mcp.tool()
def read_server_log(server_id: str) -> dict[str, object]:
    """Read the current JBoss server.log for incident analysis."""
    return _jboss().read_server_log(server_id)


@mcp.tool()
def get_thread_pool_status(server_id: str) -> dict[str, object]:
    """Read current worker thread-pool metrics."""
    return _jboss().get_thread_pool_status(server_id)


@mcp.tool()
def get_datasource_status(server_id: str) -> dict[str, object]:
    """Read current datasource connection-pool metrics."""
    return _jboss().get_datasource_status(server_id)


@mcp.tool()
def get_deployment_status(server_id: str) -> dict[str, object]:
    """Read the current deployment status."""
    return _jboss().get_deployment_status(server_id)


@mcp.tool()
def set_thread_pool_max_threads(server_id: str, value: int) -> dict[str, object]:
    """Change max_threads after human approval."""
    return _jboss().set_thread_pool_max_threads(server_id, value)


@mcp.tool()
def set_datasource_max_pool_size(server_id: str, value: int) -> dict[str, object]:
    """Change datasource max_pool_size after human approval."""
    return _jboss().set_datasource_max_pool_size(server_id, value)


@mcp.tool()
def restart_deployment(server_id: str, deployment_name: str) -> dict[str, object]:
    """Restart one deployment after human approval."""
    return _jboss().restart_deployment(server_id, deployment_name)


def main() -> None:
    """stdio transport で MCP サーバーを起動する。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
