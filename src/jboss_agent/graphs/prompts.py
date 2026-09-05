"""Prompts kept in one place so LLM responsibilities are easy to inspect."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from jboss_agent.graphs.state import IncidentState


LOG_CLASSIFICATION_PROMPT = """You classify JBoss EAP-like server logs for incident monitoring.
Use only supplied log lines. Choose one category: NORMAL, THREAD_POOL, DATASOURCE_POOL,
DEPLOYMENT, UNKNOWN. Set incident_detected=false for genuinely normal activity.
Do not invent metrics, configuration values, or hidden scenario labels.
"""

INVESTIGATION_SYSTEM_PROMPT = """You are a JBoss incident investigator.
You have READ-ONLY JBoss tools only. Use tool evidence before concluding.
Prefer a few relevant tool calls over querying everything blindly. Never ask for or
invent write operations. When evidence is sufficient, respond without more tool calls.
"""

DIAGNOSIS_INSTRUCTIONS = """Produce a structured JBoss incident diagnosis using only the
initial logs and read-only tool evidence. If evidence does not justify a safe action,
use action type NONE. Do not fabricate current_value, proposed_value, or deployment_name.
Use exactly one root_cause code when supported: THREAD_POOL_CONFIGURATION,
DATASOURCE_POOL_EXHAUSTION, DEPLOYMENT_FAILURE, UNKNOWN.
For a clearly observed recent configuration regression, prefer restoring the previous value.
"""


def log_classification_prompt(log_text: str) -> str:
    return f"{LOG_CLASSIFICATION_PROMPT}\n\nLOG LINES:\n{log_text}"


def initial_investigation_messages(state: IncidentState) -> list[object]:
    logs = "\n".join(state.get("initial_log_lines", [])) or "(no initial logs supplied)"
    return [
        SystemMessage(content=INVESTIGATION_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Incident ID: {state['incident_id']}\n"
                f"Server: {state['server_id']}\n"
                f"Initial category hint: {state.get('category', 'UNKNOWN')}\n"
                f"Severity: {state.get('severity', 'UNKNOWN')}\n"
                f"Initial log evidence:\n{logs}\n\n"
                "Investigate the cause using the available read-only tools."
            )
        ),
    ]


def diagnosis_prompt(state: IncidentState) -> str:
    evidence = "\n".join(
        f"- {item.get('tool_name')}: {item.get('content')}" for item in state.get("evidence", [])
    ) or "- No tool evidence captured"
    logs = "\n".join(state.get("initial_log_lines", [])) or "(none)"
    return (
        f"{DIAGNOSIS_INSTRUCTIONS}\n\n"
        f"Server: {state['server_id']}\n"
        f"Initial category: {state.get('category', 'UNKNOWN')}\n"
        f"Initial logs:\n{logs}\n\n"
        f"Read-only tool evidence:\n{evidence}"
    )
