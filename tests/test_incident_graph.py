from __future__ import annotations

import pytest
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from jboss_agent.config import Settings
from jboss_agent.graphs.incident import build_incident_graph


class Investigator:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, input: object) -> AIMessage:  # noqa: A002
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_thread_pool_status",
                        "args": {"server_id": "jboss-test"},
                        "id": "read-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Thread-pool configuration regression is sufficiently evidenced.")


class Diagnoser:
    def invoke(self, input: str) -> object:  # noqa: A002
        return {
            "root_cause": "THREAD_POOL_CONFIGURATION",
            "confidence": 0.95,
            "reason": "max_threads was reduced and the executor is saturated",
            "recommended_action": {
                "type": "SET_THREAD_POOL_MAX_THREADS",
                "current_value": 20,
                "proposed_value": 80,
                "deployment_name": None,
                "rationale": "restore previous capacity",
            },
        }


@pytest.mark.asyncio
async def test_incident_pauses_for_approval_then_executes_write_and_recovers() -> None:
    state = {"max_threads": 20, "queue": 9, "rejected": 2, "error_rate": 0.2}

    @tool
    def get_thread_pool_status(server_id: str) -> dict[str, object]:
        """スレッドプールの状態を読み取る。"""
        return {
            "server_id": server_id,
            "max_threads": state["max_threads"],
            "active_threads": 20,
            "queue_size": state["queue"],
            "rejected_tasks": state["rejected"],
        }

    @tool
    def get_server_health(server_id: str) -> dict[str, object]:
        """サーバーの健康状態を読み取る。"""
        return {"server_id": server_id, "status": "UP", "request_error_rate": state["error_rate"]}

    @tool
    def set_thread_pool_max_threads(server_id: str, value: int) -> dict[str, object]:
        """スレッドプールの上限を変更する。"""
        state.update(max_threads=value, queue=0, rejected=0, error_rate=0.0)
        return {"server_id": server_id, "value": value, "success": True}

    graph = build_incident_graph(
        [get_thread_pool_status, get_server_health],
        [set_thread_pool_max_threads],
        checkpointer=InMemorySaver(),
        settings=Settings(GOOGLE_API_KEY="test-key"),
        investigator=Investigator(),
        diagnoser=Diagnoser(),
    )
    config = {"configurable": {"thread_id": "incident:test"}}
    paused = await graph.ainvoke(
        {
            "incident_id": "inc-test",
            "server_id": "jboss-test",
            "category": "THREAD_POOL",
            "severity": "HIGH",
            "confidence": 0.9,
            "initial_log_lines": ["task rejected"],
            "messages": [],
            "evidence": [],
            "investigation_count": 0,
            "recovery_attempts": 0,
            "node_trace": [],
        },
        config=config,
    )
    assert paused["__interrupt__"][0].value["action"] == "SET_THREAD_POOL_MAX_THREADS"

    resumed = await graph.ainvoke(Command(resume={"decision": "approve"}), config=config)
    assert resumed["recovered"] is True
    assert resumed["execution_result"]["tool_name"] == "set_thread_pool_max_threads"
