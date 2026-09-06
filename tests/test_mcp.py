"""実際に stdio MCP サーバーを起動して Tool を取得・実行できることを確認する。"""

import pytest

from jboss_agent.fake_jboss import FakeJBoss
from jboss_agent.mcp.client import as_dict, by_name, load_jboss_tools


@pytest.mark.asyncio
async def test_stdio_mcp_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_JBOSS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SERVER_ID", "jboss-01")
    FakeJBoss(tmp_path).inject("THREAD_POOL_CONFIGURATION")

    read_tools, write_tools = await load_jboss_tools()
    assert len(read_tools) == 4
    assert len(write_tools) == 3

    result = as_dict(await by_name(read_tools, "get_thread_pool_status").ainvoke({"server_id": "jboss-01"}))
    assert result["max_threads"] == 20
    assert result["queue_size"] == 37
