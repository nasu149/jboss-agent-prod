"""The two small LangGraph State schemas used by the application."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import add_messages

from jboss_agent.models import IncidentCategory


class MonitoringState(TypedDict, total=False):
    server_id: str
    previous_log_cursor: int
    scan_from_cursor: int
    current_log_cursor: int
    cursor_reset_detected: bool
    new_log_lines: list[str]
    log_text: str
    has_new_logs: bool
    incident_detected: bool
    category: IncidentCategory
    confidence: float
    summary: str
    evidence: list[str]
    incident_id: str | None
    severity: str
    teams_notified: bool
    teams_tool_status: str | None
    node_trace: list[str]


class IncidentState(TypedDict, total=False):
    # add_messages is the reducer: nodes can return only new messages and LangGraph
    # appends/merges them into the conversation history.
    messages: Annotated[list[Any], add_messages]
    incident_id: str
    server_id: str
    category: str
    severity: str
    confidence: float
    initial_log_lines: list[str]
    evidence: list[dict[str, Any]]
    investigation_count: int
    diagnosis: dict[str, Any] | None
    proposed_action: dict[str, Any] | None
    risk_level: str | None
    policy_reason: str | None
    approval_status: str | None
    execution_result: dict[str, Any] | None
    recovered: bool | None
    recovery_attempts: int
    failure_reason: str | None
    node_trace: list[str]
