"""LLM 分類による分岐と Human-in-the-loop の resume を最小 Graph で確認する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from jboss_agent.config import Settings
from jboss_agent.graph import build_graph


@dataclass
class StubTool:
    name: str
    callback: Any

    async def ainvoke(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.callback(args)


class StubClassifier:
    def __init__(self, category: str) -> None:
        self.category = category

    def invoke(self, _prompt: str) -> dict[str, str]:
        return {"category": self.category, "summary": f"classified as {self.category}"}


@pytest.fixture
def settings() -> Settings:
    return Settings("unused", "unused", "jboss-01", ".data/fake_jboss")


def toolset(state: dict[str, Any]) -> tuple[list[StubTool], list[StubTool]]:
    def read_log(_args):
        return {"lines": ["dummy log"]}

    def get_thread(_args):
        return dict(state["thread"])

    def get_datasource(_args):
        return dict(state["datasource"])

    def get_deployment(_args):
        return dict(state["deployment"])

    def set_thread(args):
        state["thread"].update(max_threads=args["value"], queue_size=0)
        return {"success": True, "value": args["value"]}

    def set_datasource(args):
        state["datasource"].update(max_pool_size=args["value"], timed_out_requests=0)
        return {"success": True, "value": args["value"]}

    def restart(_args):
        state["deployment"]["status"] = "OK"
        return {"success": True}

    read_tools = [
        StubTool("read_server_log", read_log),
        StubTool("get_thread_pool_status", get_thread),
        StubTool("get_datasource_status", get_datasource),
        StubTool("get_deployment_status", get_deployment),
    ]
    write_tools = [
        StubTool("set_thread_pool_max_threads", set_thread),
        StubTool("set_datasource_max_pool_size", set_datasource),
        StubTool("restart_deployment", restart),
    ]
    return read_tools, write_tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "expected_node"),
    [
        ("THREAD_POOL_CONFIGURATION", "inspect_thread_pool"),
        ("DATASOURCE_POOL_EXHAUSTION", "inspect_datasource"),
        ("DEPLOYMENT_FAILURE", "inspect_deployment"),
    ],
)
async def test_llm_category_routes_to_expected_node(settings, category, expected_node):
    state = {
        "thread": {"max_threads": 20, "queue_size": 37},
        "datasource": {"max_pool_size": 5, "timed_out_requests": 14},
        "deployment": {"name": "app.war", "status": "FAILED"},
    }
    reads, writes = toolset(state)
    graph = build_graph(
        reads,
        writes,
        checkpointer=InMemorySaver(),
        settings=settings,
        classifier=StubClassifier(category),
    )
    result = await graph.ainvoke(
        {"server_id": "jboss-01", "trace": []},
        config={"configurable": {"thread_id": "route-test"}},
    )

    assert expected_node in result["trace"]
    assert "__interrupt__" in result
    assert "execute_fix" not in result["trace"]


@pytest.mark.asyncio
async def test_approve_resumes_same_checkpoint_and_executes_write(settings):
    state = {
        "thread": {"max_threads": 20, "queue_size": 37},
        "datasource": {"max_pool_size": 30, "timed_out_requests": 0},
        "deployment": {"name": "app.war", "status": "OK"},
    }
    reads, writes = toolset(state)
    saver = InMemorySaver()
    graph = build_graph(
        reads,
        writes,
        checkpointer=saver,
        settings=settings,
        classifier=StubClassifier("THREAD_POOL_CONFIGURATION"),
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
        "inspect_thread_pool",
        "approval",
        "execute_fix",
        "verify_recovery",
    ]


@pytest.mark.asyncio
async def test_reject_never_executes_write(settings):
    state = {
        "thread": {"max_threads": 20, "queue_size": 37},
        "datasource": {"max_pool_size": 30, "timed_out_requests": 0},
        "deployment": {"name": "app.war", "status": "OK"},
    }
    reads, writes = toolset(state)
    graph = build_graph(
        reads,
        writes,
        checkpointer=InMemorySaver(),
        settings=settings,
        classifier=StubClassifier("THREAD_POOL_CONFIGURATION"),
    )
    config = {"configurable": {"thread_id": "reject-test"}}

    await graph.ainvoke({"server_id": "jboss-01", "trace": []}, config=config)
    final = await graph.ainvoke(Command(resume=False), config=config)

    assert final["status"] == "REJECTED"
    assert state["thread"]["max_threads"] == 20
    assert "execute_fix" not in final["trace"]


@pytest.mark.asyncio
async def test_normal_activity_ends_without_interrupt(settings):
    state = {
        "thread": {"max_threads": 80, "queue_size": 0},
        "datasource": {"max_pool_size": 30, "timed_out_requests": 0},
        "deployment": {"name": "app.war", "status": "OK"},
    }
    reads, writes = toolset(state)
    graph = build_graph(
        reads,
        writes,
        checkpointer=InMemorySaver(),
        settings=settings,
        classifier=StubClassifier("NORMAL_ACTIVITY"),
    )

    result = await graph.ainvoke(
        {"server_id": "jboss-01", "trace": []},
        config={"configurable": {"thread_id": "normal-test"}},
    )
    assert result["status"] == "NO_INCIDENT"
    assert result["trace"] == ["read_log", "classify_log", "normal_activity"]
    assert "__interrupt__" not in result
