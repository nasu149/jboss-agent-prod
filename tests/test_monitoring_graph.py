from __future__ import annotations

import json

import pytest
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from jboss_agent.config import Settings
from jboss_agent.graphs.monitoring import build_monitoring_graph


class FakeClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, input: str) -> object:  # noqa: A002
        self.calls += 1
        return {
            "incident_detected": False,
            "category": "NORMAL",
            "confidence": 0.99,
            "summary": "normal activity",
            "evidence": ["INFO only"],
        }


class LogSource:
    def __init__(self) -> None:
        self.payload = b"2026-09-05 INFO request completed\n"

    def read(self, cursor: int) -> dict[str, object]:
        return {
            "from_cursor": cursor,
            "to_cursor": len(self.payload),
            "lines": self.payload[cursor:].decode().splitlines(),
        }


@pytest.mark.asyncio
async def test_fixed_monitor_thread_persists_cursor_and_skips_llm_without_delta() -> None:
    source = LogSource()

    @tool
    def read_server_log(server_id: str, cursor: int) -> dict[str, object]:
        """Read fake log delta."""
        return source.read(cursor)

    classifier = FakeClassifier()
    graph = build_monitoring_graph(
        [read_server_log],
        checkpointer=InMemorySaver(),
        settings=Settings(GOOGLE_API_KEY="test-key"),
        classifier=classifier,
        notifier=lambda payload: json.dumps({"success": True, "status": "test"}),
    )
    config = {"configurable": {"thread_id": "monitor:jboss-test"}}

    first = await graph.ainvoke({"server_id": "jboss-test"}, config=config)
    second = await graph.ainvoke({"server_id": "jboss-test"}, config=config)

    assert first["has_new_logs"] is True
    assert second["has_new_logs"] is False
    assert classifier.calls == 1


@pytest.mark.asyncio
async def test_monitoring_accepts_mcp_content_block_output() -> None:
    source = LogSource()

    class McpStyleReadTool:
        name = "read_server_log"

        async def ainvoke(self, args: dict[str, object]) -> object:
            payload = source.read(int(args["cursor"]))
            return [{"type": "text", "text": json.dumps(payload)}]

    classifier = FakeClassifier()
    graph = build_monitoring_graph(
        [McpStyleReadTool()],
        checkpointer=InMemorySaver(),
        settings=Settings(GOOGLE_API_KEY="test-key"),
        classifier=classifier,
        notifier=lambda payload: json.dumps({"success": True, "status": "test"}),
    )

    result = await graph.ainvoke(
        {"server_id": "jboss-test"},
        config={"configurable": {"thread_id": "monitor:mcp-content-block"}},
    )

    assert result["has_new_logs"] is True
    assert result["new_log_lines"] == ["2026-09-05 INFO request completed"]
    assert classifier.calls == 1
