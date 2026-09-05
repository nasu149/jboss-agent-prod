"""Incident Graph: investigate -> diagnose -> approve -> write -> verify."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from jboss_agent.config import Settings
from jboss_agent.graphs.prompts import diagnosis_prompt, initial_investigation_messages
from jboss_agent.graphs.state import IncidentState
from jboss_agent.llm import build_diagnoser, build_investigator
from jboss_agent.models import ApprovalResponse, IncidentDiagnosis
from jboss_agent.policy import evaluate_action
from jboss_agent.tool_results import normalize_tool_result



def _prepare_investigation(state: IncidentState) -> dict[str, object]:
    if state.get("messages"):
        return {"node_trace": [*state.get("node_trace", []), "prepare_investigation"]}
    return {
        "messages": initial_investigation_messages(state),
        "evidence": state.get("evidence", []),
        "investigation_count": state.get("investigation_count", 0),
        "recovery_attempts": state.get("recovery_attempts", 0),
        "node_trace": [*state.get("node_trace", []), "prepare_investigation"],
    }


def make_investigate_node(model: Any):
    def investigate(state: IncidentState) -> dict[str, object]:
        response = model.invoke(state["messages"])
        if not isinstance(response, AIMessage):
            raise TypeError(f"investigator must return AIMessage, got {type(response).__name__}")
        return {
            "messages": [response],
            "investigation_count": state.get("investigation_count", 0) + 1,
            "node_trace": [*state.get("node_trace", []), "investigate"],
        }

    return investigate


def _route_after_investigate(state: IncidentState) -> str:
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    return "read_tools" if isinstance(last, AIMessage) and last.tool_calls else "diagnose"


def _record_tool_evidence(state: IncidentState) -> dict[str, object]:
    recent: list[ToolMessage] = []
    for message in reversed(state.get("messages", [])):
        if isinstance(message, ToolMessage):
            recent.append(message)
        else:
            break
    recent.reverse()

    evidence = [*state.get("evidence", [])]
    for message in recent:
        evidence.append(
            {
                "tool_name": message.name or "unknown_tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        )
    return {"evidence": evidence, "node_trace": [*state.get("node_trace", []), "record_evidence"]}


def make_round_route(max_rounds: int):
    def route(state: IncidentState) -> str:
        return "diagnose" if state.get("investigation_count", 0) >= max_rounds else "investigate"

    return route


def make_diagnose_node(model: Any):
    def diagnose(state: IncidentState) -> dict[str, object]:
        raw = model.invoke(diagnosis_prompt(state))
        if isinstance(raw, IncidentDiagnosis):
            result = raw
        elif isinstance(raw, Mapping):
            result = IncidentDiagnosis.model_validate(dict(raw))
        else:
            raise TypeError(f"diagnoser returned unsupported type: {type(raw).__name__}")
        return {
            "diagnosis": result.model_dump(),
            "proposed_action": result.recommended_action.model_dump(),
            "node_trace": [*state.get("node_trace", []), "diagnose"],
        }

    return diagnose


def _validate_action(state: IncidentState) -> dict[str, object]:
    result = evaluate_action(state.get("proposed_action"))
    return {
        "proposed_action": result.normalized_action,
        "risk_level": result.risk,
        "policy_reason": result.reason,
        "approval_status": "PENDING" if result.allowed and result.risk != "LOW" else None,
        "node_trace": [*state.get("node_trace", []), "validate_action"],
    }


def _route_after_policy(state: IncidentState) -> str:
    if state.get("risk_level") == "BLOCKED":
        return "blocked"
    if (state.get("proposed_action") or {}).get("type") == "NONE":
        return "no_action"
    return "approval"


def _approval(state: IncidentState) -> dict[str, object]:
    """Pause before side effects. Nothing before interrupt() mutates JBoss."""
    action = dict(state.get("proposed_action") or {})
    payload = {
        "type": "approval_required",
        "incident_id": state["incident_id"],
        "server_id": state["server_id"],
        "action": action.get("type"),
        "current_value": action.get("current_value"),
        "proposed_value": action.get("proposed_value"),
        "deployment_name": action.get("deployment_name"),
        "reason": (state.get("diagnosis") or {}).get("reason"),
        "risk": state.get("risk_level"),
    }
    raw = interrupt(payload)
    if not isinstance(raw, Mapping):
        raise ValueError("approval resume value must be an object")
    response = ApprovalResponse.model_validate(dict(raw))

    trace = [*state.get("node_trace", []), "approval"]
    if response.decision == "reject":
        return {"approval_status": "REJECTED", "node_trace": trace}

    if response.decision == "edit_and_approve":
        if response.proposed_value is None:
            return {"approval_status": "BLOCKED", "policy_reason": "edited value is required", "node_trace": trace}
        action["proposed_value"] = response.proposed_value
        checked = evaluate_action(action)
        if not checked.allowed:
            return {
                "proposed_action": checked.normalized_action,
                "risk_level": checked.risk,
                "policy_reason": checked.reason,
                "approval_status": "BLOCKED",
                "node_trace": trace,
            }
        return {
            "proposed_action": checked.normalized_action,
            "risk_level": checked.risk,
            "policy_reason": checked.reason,
            "approval_status": "APPROVED",
            "node_trace": trace,
        }

    return {"approval_status": "APPROVED", "node_trace": trace}


def _route_after_approval(state: IncidentState) -> str:
    if state.get("approval_status") == "APPROVED":
        return "approved"
    if state.get("approval_status") == "REJECTED":
        return "rejected"
    return "blocked"


def _write_call(state: IncidentState) -> tuple[str, dict[str, object]]:
    checked = evaluate_action(state.get("proposed_action"))
    if state.get("approval_status") != "APPROVED":
        raise PermissionError("write execution requires human approval")
    if not checked.allowed or checked.risk == "BLOCKED":
        raise PermissionError(f"write action blocked: {checked.reason}")

    action = checked.normalized_action
    server_id = state["server_id"]
    if action["type"] == "SET_THREAD_POOL_MAX_THREADS":
        return "set_thread_pool_max_threads", {"server_id": server_id, "value": action["proposed_value"]}
    if action["type"] == "SET_DATASOURCE_MAX_POOL_SIZE":
        return "set_datasource_max_pool_size", {"server_id": server_id, "value": action["proposed_value"]}
    if action["type"] == "RESTART_DEPLOYMENT":
        return "restart_deployment", {"server_id": server_id, "deployment_name": action["deployment_name"]}
    if action["type"] == "RELOAD_SERVER":
        return "reload_server", {"server_id": server_id}
    raise ValueError(f"action does not map to a write tool: {action['type']}")


def _prepare_write(state: IncidentState) -> dict[str, object]:
    name, args = _write_call(state)
    message = AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"write-{uuid.uuid4().hex[:12]}", "type": "tool_call"}],
    )
    return {"messages": [message], "node_trace": [*state.get("node_trace", []), "prepare_write"]}


def _capture_write(state: IncidentState) -> dict[str, object]:
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], ToolMessage):
        raise RuntimeError("write ToolNode did not produce a ToolMessage")
    message = messages[-1]
    return {
        "execution_result": {
            "tool_name": message.name,
            "tool_call_id": message.tool_call_id,
            "content": normalize_tool_result(message.content),
        },
        "recovery_attempts": state.get("recovery_attempts", 0) + 1,
        "node_trace": [*state.get("node_trace", []), "capture_write"],
    }


def _tool_map(tools: Sequence[Any]) -> dict[str, Any]:
    return {tool.name: tool for tool in tools}


def make_verify_node(read_tools: Sequence[Any]):
    tools = _tool_map(read_tools)

    async def verify(state: IncidentState) -> dict[str, object]:
        server_id = state["server_id"]
        action_type = (state.get("proposed_action") or {}).get("type")
        health = normalize_tool_result(await tools["get_server_health"].ainvoke({"server_id": server_id}))
        details: dict[str, Any] = {"health": health}
        healthy = health.get("status") == "UP" and float(health.get("request_error_rate", 1.0)) < 0.05

        if action_type == "SET_THREAD_POOL_MAX_THREADS":
            pool = normalize_tool_result(await tools["get_thread_pool_status"].ainvoke({"server_id": server_id}))
            details["thread_pool"] = pool
            healthy = healthy and int(pool.get("active_threads", 10**9)) <= int(pool.get("max_threads", -1))
            healthy = healthy and int(pool.get("queue_size", 1)) == 0 and int(pool.get("rejected_tasks", 1)) == 0
        elif action_type == "SET_DATASOURCE_MAX_POOL_SIZE":
            ds = normalize_tool_result(await tools["get_datasource_status"].ainvoke({"server_id": server_id}))
            details["datasource"] = ds
            healthy = healthy and int(ds.get("active_count", 10**9)) <= int(ds.get("max_pool_size", -1))
            healthy = healthy and int(ds.get("timed_out_requests", 1)) == 0
        elif action_type == "RESTART_DEPLOYMENT":
            deployment = normalize_tool_result(await tools["get_deployment_status"].ainvoke({"server_id": server_id}))
            details["deployment"] = deployment
            healthy = healthy and deployment.get("status") == "OK" and bool(deployment.get("enabled"))

        return {
            "recovered": healthy,
            "evidence": [*state.get("evidence", []), {"tool_name": "recovery_verification", "content": details}],
            "node_trace": [*state.get("node_trace", []), "verify_recovery"],
        }

    return verify


def make_recovery_route(max_attempts: int):
    def route(state: IncidentState) -> str:
        if state.get("recovered") is True:
            return "recovered"
        return "fail_safe" if state.get("recovery_attempts", 0) >= max_attempts else "retry"

    return route


def _prepare_retry(state: IncidentState) -> dict[str, object]:
    return {
        "messages": [HumanMessage(content="The approved remediation did not recover the server. Re-investigate with read-only tools and challenge the previous diagnosis.")],
        "investigation_count": 0,
        "diagnosis": None,
        "proposed_action": None,
        "risk_level": None,
        "policy_reason": None,
        "approval_status": None,
        "execution_result": None,
        "recovered": None,
        "node_trace": [*state.get("node_trace", []), "prepare_retry"],
    }


def _rejected(state: IncidentState) -> dict[str, object]:
    return {"failure_reason": "Human rejected the proposed write operation.", "node_trace": [*state.get("node_trace", []), "rejected"]}


def _blocked(state: IncidentState) -> dict[str, object]:
    return {"approval_status": "BLOCKED", "failure_reason": state.get("policy_reason") or "Action blocked by policy.", "node_trace": [*state.get("node_trace", []), "blocked"]}


def _no_action(state: IncidentState) -> dict[str, object]:
    return {"recovered": True, "node_trace": [*state.get("node_trace", []), "no_action"]}


def _recovered(state: IncidentState) -> dict[str, object]:
    return {"node_trace": [*state.get("node_trace", []), "recovered"]}


def _fail_safe(state: IncidentState) -> dict[str, object]:
    return {"recovered": False, "failure_reason": "Maximum recovery attempts reached; escalate to a human operator.", "node_trace": [*state.get("node_trace", []), "fail_safe"]}


def build_incident_graph(
    read_tools: Sequence[Any],
    write_tools: Sequence[Any],
    *,
    checkpointer: Any,
    settings: Settings,
    investigator: Any | None = None,
    diagnoser: Any | None = None,
):
    """Build the complete incident workflow.

    Safety boundary: only read_tools are bound to Gemini. write_tools are used by
    ToolNode only after Python policy validation and Human-in-the-loop approval.
    """
    investigator = investigator or build_investigator(settings, read_tools)
    diagnoser = diagnoser or build_diagnoser(settings)

    graph = StateGraph(IncidentState)
    graph.add_node("prepare_investigation", _prepare_investigation)
    graph.add_node("investigate", make_investigate_node(investigator))
    graph.add_node("read_tools", ToolNode(list(read_tools)))
    graph.add_node("record_evidence", _record_tool_evidence)
    graph.add_node("diagnose", make_diagnose_node(diagnoser))
    graph.add_node("validate_action", _validate_action)
    graph.add_node("approval", _approval)
    graph.add_node("prepare_write", _prepare_write)
    graph.add_node("write_tools", ToolNode(list(write_tools)))
    graph.add_node("capture_write", _capture_write)
    graph.add_node("verify_recovery", make_verify_node(read_tools))
    graph.add_node("prepare_retry", _prepare_retry)
    graph.add_node("recovered", _recovered)
    graph.add_node("rejected", _rejected)
    graph.add_node("blocked", _blocked)
    graph.add_node("no_action", _no_action)
    graph.add_node("fail_safe", _fail_safe)

    graph.add_edge(START, "prepare_investigation")
    graph.add_edge("prepare_investigation", "investigate")
    graph.add_conditional_edges("investigate", _route_after_investigate, {"read_tools": "read_tools", "diagnose": "diagnose"})
    graph.add_edge("read_tools", "record_evidence")
    graph.add_conditional_edges("record_evidence", make_round_route(settings.max_investigation_rounds), {"investigate": "investigate", "diagnose": "diagnose"})
    graph.add_edge("diagnose", "validate_action")
    graph.add_conditional_edges("validate_action", _route_after_policy, {"approval": "approval", "blocked": "blocked", "no_action": "no_action"})
    graph.add_conditional_edges("approval", _route_after_approval, {"approved": "prepare_write", "rejected": "rejected", "blocked": "blocked"})
    graph.add_edge("prepare_write", "write_tools")
    graph.add_edge("write_tools", "capture_write")
    graph.add_edge("capture_write", "verify_recovery")
    graph.add_conditional_edges("verify_recovery", make_recovery_route(settings.max_recovery_attempts), {"recovered": "recovered", "retry": "prepare_retry", "fail_safe": "fail_safe"})
    graph.add_edge("prepare_retry", "investigate")
    for terminal in ("recovered", "rejected", "blocked", "no_action", "fail_safe"):
        graph.add_edge(terminal, END)

    return graph.compile(checkpointer=checkpointer)
