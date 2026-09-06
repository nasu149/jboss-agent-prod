"""ToolNode による read Tool 選択、Teams 通知、HITL resume を Graph で確認する。"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from jboss_agent.config import Settings
from jboss_agent.graph import build_graph


class StubClassifier:
    def __init__(self, category: str) -> None:
        self.category = category

    def invoke(self, _prompt: str) -> dict[str, str]:
        return {"category": self.category, "summary": f"classified as {self.category}"}


class StubInvestigator:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def ainvoke(self, _messages: list[Any]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.tool_name,
                    "args": {"server_id": "jboss-01"},
                    "id": "call-read-1",
                    "type": "tool_call",
                }
            ],
        )


@pytest.fixture
def settings() -> Settings:
    return Settings("unused", "unused", "jboss-01", ".data/fake_jboss")


def toolset(state: dict[str, Any]):
    @tool
    async def read_server_log(server_id: str) -> dict[str, Any]:
        """Read the current fake server.log."""
        return {"server_id": server_id, "lines": ["dummy log"]}

    @tool
    async def get_thread_pool_status(server_id: str) -> dict[str, Any]:
        """Read worker thread-pool metrics."""
        state["read_calls"].append("get_thread_pool_status")
        return {"server_id": server_id, **state["thread"]}

    @tool
    async def get_datasource_status(server_id: str) -> dict[str, Any]:
        """Read datasource pool metrics."""
        state["read_calls"].append("get_datasource_status")
        return {"server_id": server_id, **state["datasource"]}

    @tool
    async def get_deployment_status(server_id: str) -> dict[str, Any]:
        """Read deployment state."""
        state["read_calls"].append("get_deployment_status")
        return {"server_id": server_id, **state["deployment"]}

    @tool
    async def set_thread_pool_max_threads(server_id: str, value: int) -> dict[str, Any]:
        """Set thread-pool max threads."""
        state["thread"].update(max_threads=value, queue_size=0)
        return {"server_id": server_id, "success": True, "value": value}

    @tool
    async def set_datasource_max_pool_size(server_id: str, value: int) -> dict[str, Any]:
        """Set datasource max pool size."""
        state["datasource"].update(max_pool_size=value, timed_out_requests=0)
        return {"server_id": server_id, "success": True, "value": value}

    @tool
    async def restart_deployment(server_id: str, deployment_name: str) -> dict[str, Any]:
        """Restart one deployment."""
        state["deployment"]["status"] = "OK"
        return {"server_id": server_id, "deployment_name": deployment_name, "success": True}

    reads = [read_server_log, get_thread_pool_status, get_datasource_status, get_deployment_status]
    writes = [set_thread_pool_max_threads, set_datasource_max_pool_size, restart_deployment]
    return reads, writes


def base_state() -> dict[str, Any]:
    return {
        "thread": {"max_threads": 20, "queue_size": 37},
        "datasource": {"max_pool_size": 5, "timed_out_requests": 14},
        "deployment": {"name": "app.war", "status": "FAILED"},
        "read_calls": [],
    }


def notifier(calls: list[dict[str, str]]):
    def send(server_id: str, category: str, summary: str) -> dict[str, Any]:
        calls.append({"server_id": server_id, "category": category, "summary": summary})
        return {"success": True, "status": "test"}

    return send


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "selected_tool", "proposal_node"),
    [
        ("THREAD_POOL_CONFIGURATION", "get_thread_pool_status", "propose_thread_pool"),
        ("DATASOURCE_POOL_EXHAUSTION", "get_datasource_status", "propose_datasource"),
        ("DEPLOYMENT_FAILURE", "get_deployment_status", "propose_deployment"),
    ],
)
async def test_llm_selects_read_tool_and_toolnode_executes_it(
    settings,
    category,
    selected_tool,
    proposal_node,
):
    state = base_state()
    reads, writes = toolset(state)
    notifications: list[dict[str, str]] = []
    graph = build_graph(
        reads,
        writes,
        checkpointer=InMemorySaver(),
        settings=settings,
        classifier=StubClassifier(category),
        investigator=StubInvestigator(selected_tool),
        notifier=notifier(notifications),
    )

    result = await graph.ainvoke(
        {"server_id": "jboss-01", "trace": []},
        config={"configurable": {"thread_id": f"route-{selected_tool}"}},
    )

    assert result["selected_read_tools"] == [selected_tool]
    assert state["read_calls"] == [selected_tool]
    assert selected_tool in result["evidence"]
    assert proposal_node in result["trace"]
    assert "read_tools" in result["trace"]
    assert "__interrupt__" in result
    assert notifications[0]["category"] == category


@pytest.mark.asyncio
async def test_approve_resumes_and_executes_write(settings):
    state = base_state()
    reads, writes = toolset(state)
    saver = InMemorySaver()
    graph = build_graph(
        reads,
        writes,
        checkpointer=saver,
        settings=settings,
        classifier=StubClassifier("THREAD_POOL_CONFIGURATION"),
        investigator=StubInvestigator("get_thread_pool_status"),
        notifier=lambda *_args: {"success": True, "status": "test"},
    )
    config = {"configurable": {"thread_id": "approve-test"}}

    first = await graph.ainvoke({"server_id": "jboss-01", "trace": []}, config=config)
    assert "__interrupt__" in first
    assert state["thread"]["max_threads"] == 20

    final = await graph.ainvoke(Command(resume=True), config=config)
    assert final["status"] == "RECOVERED"
    assert final["recovered"] is True
    assert state["thread"]["max_threads"] == 80
    assert final["trace"] == [
        "read_log",
        "classify_log",
        "notify_teams",
        "prepare_investigation",
        "investigate",
        "read_tools",
        "propose_thread_pool",
        "approval",
        "execute_fix",
        "verify_recovery",
    ]


@pytest.mark.asyncio
async def test_reject_never_executes_write(settings):
    state = base_state()
    reads, writes = toolset(state)
    graph = build_graph(
        reads,
        writes,
        checkpointer=InMemorySaver(),
        settings=settings,
        classifier=StubClassifier("THREAD_POOL_CONFIGURATION"),
        investigator=StubInvestigator("get_thread_pool_status"),
        notifier=lambda *_args: {"success": True, "status": "test"},
    )
    config = {"configurable": {"thread_id": "reject-test"}}

    await graph.ainvoke({"server_id": "jboss-01", "trace": []}, config=config)
    final = await graph.ainvoke(Command(resume=False), config=config)

    assert final["status"] == "REJECTED"
    assert state["thread"]["max_threads"] == 20
    assert "execute_fix" not in final["trace"]


@pytest.mark.asyncio
async def test_normal_activity_skips_teams_toolnode_and_hitl(settings):
    state = base_state()
    reads, writes = toolset(state)
    notifications: list[dict[str, str]] = []
    graph = build_graph(
        reads,
        writes,
        checkpointer=InMemorySaver(),
        settings=settings,
        classifier=StubClassifier("NORMAL_ACTIVITY"),
        investigator=StubInvestigator("get_thread_pool_status"),
        notifier=notifier(notifications),
    )

    result = await graph.ainvoke(
        {"server_id": "jboss-01", "trace": []},
        config={"configurable": {"thread_id": "normal-test"}},
    )

    assert result["status"] == "NO_INCIDENT"
    assert result["trace"] == ["read_log", "classify_log", "normal_activity"]
    assert notifications == []
    assert state["read_calls"] == []
    assert "__interrupt__" not in result
