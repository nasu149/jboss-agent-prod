"""実際に stdio MCP サーバーを起動して read/write Tool を往復できることを確認する。"""

import pytest

from jboss_agent.config import get_settings
from jboss_agent.fake_jboss import FakeJBoss
from jboss_agent.mcp.client import as_dict, by_name, load_jboss_tools


@pytest.mark.asyncio
async def test_stdio_mcp_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_JBOSS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SERVER_ID", "jboss-01")
    get_settings.cache_clear()
    FakeJBoss(tmp_path).inject("THREAD_POOL_CONFIGURATION")

    read_tools, write_tools = await load_jboss_tools()
    assert len(read_tools) == 4
    assert len(write_tools) == 3

    read_thread = by_name(read_tools, "get_thread_pool_status")
    before = as_dict(await read_thread.ainvoke({"server_id": "jboss-01"}))
    assert before["max_threads"] == 20
    assert before["queue_size"] == 37

    write_thread = by_name(write_tools, "set_thread_pool_max_threads")
    write_result = as_dict(await write_thread.ainvoke({"server_id": "jboss-01", "value": 80}))
    assert write_result["success"] is True

    after = as_dict(await read_thread.ainvoke({"server_id": "jboss-01"}))
    assert after["max_threads"] == 80
    assert after["queue_size"] == 0

    get_settings.cache_clear()
