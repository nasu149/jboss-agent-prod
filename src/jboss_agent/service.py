"""Application service that connects the two LangGraph workflows to the UI."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import Command

from jboss_agent.config import Settings
from jboss_agent.graphs.incident import build_incident_graph
from jboss_agent.graphs.monitoring import build_monitoring_graph
from jboss_agent.mcp.client import READ_TOOL_NAMES, load_jboss_tools
from jboss_agent.persistence import open_checkpointer
from jboss_agent.runtime_store import RuntimeStore
from jboss_agent.simulator import GroundTruthStore


class AgentService:
    def __init__(self, settings: Settings, runtime: RuntimeStore, truth: GroundTruthStore) -> None:
        self.settings = settings
        self.runtime = runtime
        self.truth = truth

    async def run_scan(self) -> dict[str, Any]:
        """Run one monitoring cycle; if needed, continue into incident handling."""
        server_id = self.settings.server_id
        self.runtime.begin_scan(server_id)

        try:
            read_tools, write_tools = await load_jboss_tools()
            async with open_checkpointer(self.settings) as checkpointer:
                monitoring_graph = build_monitoring_graph(
                    read_tools,
                    checkpointer=checkpointer,
                    settings=self.settings,
                )
                monitoring = await monitoring_graph.ainvoke(
                    {"server_id": server_id},
                    config={"configurable": {"thread_id": self.settings.monitoring_thread_id}},
                )

                incident_id = monitoring.get("incident_id")
                self.runtime.complete_scan(
                    server_id,
                    previous_cursor=int(monitoring.get("scan_from_cursor", 0)),
                    current_cursor=int(monitoring.get("current_log_cursor", 0)),
                    incident_id=str(incident_id) if incident_id else None,
                )
                self._record_scan(monitoring)

                if not incident_id:
                    return {"monitoring": monitoring, "incident": None}

                incident_id = str(incident_id)
                thread_id = f"incident:{incident_id}"
                self.runtime.upsert_incident(
                    incident_id=incident_id,
                    thread_id=thread_id,
                    server_id=server_id,
                    category=str(monitoring.get("category", "UNKNOWN")),
                    severity=str(monitoring.get("severity", "MEDIUM")),
                    confidence=float(monitoring.get("confidence", 0.0)),
                    summary=str(monitoring.get("summary", "Incident detected")),
                    status="INVESTIGATING",
                )
                # The simulator answer is linked by ID only; it is never placed in Agent State.
                self.truth.link_latest_unlinked(server_id, incident_id)
                self.runtime.add_activity(
                    server_id,
                    "incident",
                    f"Incident {incident_id} created; starting read-only investigation",
                    incident_id=incident_id,
                )

                incident_graph = build_incident_graph(
                    read_tools,
                    write_tools,
                    checkpointer=checkpointer,
                    settings=self.settings,
                )
                incident = await incident_graph.ainvoke(
                    {
                        "incident_id": incident_id,
                        "server_id": server_id,
                        "category": str(monitoring.get("category", "UNKNOWN")),
                        "severity": str(monitoring.get("severity", "MEDIUM")),
                        "confidence": float(monitoring.get("confidence", 0.0)),
                        "initial_log_lines": list(monitoring.get("new_log_lines", [])),
                        "messages": [],
                        "evidence": [],
                        "investigation_count": 0,
                        "recovery_attempts": 0,
                        "node_trace": [],
                    },
                    config={"configurable": {"thread_id": thread_id}},
                )
                self._persist_incident(monitoring, incident, thread_id)
                return {"monitoring": monitoring, "incident": incident}
        except Exception as exc:
            self.runtime.fail_scan(server_id, str(exc))
            raise

    async def resume_incident(
        self,
        incident_id: str,
        *,
        decision: str,
        proposed_value: int | None = None,
    ) -> dict[str, Any]:
        record = self.runtime.get_incident(incident_id)
        if record is None:
            raise ValueError(f"unknown incident_id: {incident_id}")

        payload: dict[str, Any] = {"decision": decision}
        if proposed_value is not None:
            payload["proposed_value"] = proposed_value

        read_tools, write_tools = await load_jboss_tools()
        async with open_checkpointer(self.settings) as checkpointer:
            graph = build_incident_graph(
                read_tools,
                write_tools,
                checkpointer=checkpointer,
                settings=self.settings,
            )
            result = await graph.ainvoke(
                Command(resume=payload),
                config={"configurable": {"thread_id": record.thread_id}},
            )

        monitoring_stub = {
            "incident_id": record.incident_id,
            "server_id": record.server_id,
            "category": record.category,
            "severity": record.severity,
            "confidence": record.confidence,
            "summary": record.summary,
        }
        self._persist_incident(monitoring_stub, result, record.thread_id)
        return result

    def _record_scan(self, state: dict[str, Any]) -> None:
        lines = len(state.get("new_log_lines", []))
        if not state.get("has_new_logs"):
            message = (
                f"No new server.log lines (cursor {state.get('scan_from_cursor', 0)} "
                f"-> {state.get('current_log_cursor', 0)}); Gemini skipped"
            )
        elif state.get("incident_detected"):
            message = (
                f"{lines} new log lines analyzed "
                f"(cursor {state.get('scan_from_cursor', 0)} -> {state.get('current_log_cursor', 0)}); "
                f"incident suspected: "
                f"{state.get('category')} ({float(state.get('confidence', 0.0)):.0%})"
            )
        else:
            message = (
                f"{lines} new log lines analyzed "
                f"(cursor {state.get('scan_from_cursor', 0)} -> {state.get('current_log_cursor', 0)}); "
                "no incident detected"
            )
        self.runtime.add_activity(self.settings.server_id, "monitoring", message)

    def _persist_incident(self, monitoring: dict[str, Any], result: dict[str, Any], thread_id: str) -> None:
        incident_id = str(monitoring["incident_id"])
        pending = _interrupt_payload(result)
        proposed_action = result.get("proposed_action")
        diagnosis = result.get("diagnosis")
        recovered = result.get("recovered")
        approval = result.get("approval_status")
        failure_reason = result.get("failure_reason")

        if pending is not None:
            status, activity = "PENDING_APPROVAL", "Investigation paused for human approval"
        elif approval == "REJECTED":
            status, activity = "REJECTED", "Human rejected remediation; no write executed"
        elif approval == "BLOCKED":
            status, activity = "BLOCKED", "Remediation blocked by policy"
        elif recovered is True:
            no_action = (proposed_action or {}).get("type") == "NONE"
            status = "RESOLVED_NO_ACTION" if no_action else "RECOVERED"
            activity = "Incident workflow completed successfully"
        elif recovered is False:
            status, activity = "FAILED_SAFE", "Recovery failed; fail-safe escalation reached"
        else:
            status, activity = "COMPLETED", "Incident workflow completed"

        read_names = _read_tool_names(result.get("messages", []))
        self.runtime.upsert_incident(
            incident_id=incident_id,
            thread_id=thread_id,
            server_id=str(monitoring["server_id"]),
            category=str(monitoring.get("category", "UNKNOWN")),
            severity=str(monitoring.get("severity", "MEDIUM")),
            confidence=float(monitoring.get("confidence", 0.0)),
            summary=str(monitoring.get("summary", "Incident detected")),
            status=status,
            pending_approval=pending,
            diagnosis=diagnosis if isinstance(diagnosis, dict) else None,
            proposed_action=proposed_action if isinstance(proposed_action, dict) else None,
            recovered=recovered if isinstance(recovered, bool) else None,
            failure_reason=str(failure_reason) if failure_reason else None,
            investigation_tool_calls=len(read_names),
        )

        server_id = str(monitoring["server_id"])
        if read_names:
            self.runtime.add_activity(server_id, "tool", "Read MCP tools: " + ", ".join(read_names), incident_id=incident_id)
        if isinstance(diagnosis, dict):
            self.runtime.add_activity(server_id, "diagnosis", f"Diagnosis: {diagnosis.get('root_cause', 'UNKNOWN')}", incident_id=incident_id)
        execution = result.get("execution_result")
        if isinstance(execution, dict) and execution.get("tool_name"):
            self.runtime.add_activity(server_id, "write_tool", f"Executed approved MCP write tool: {execution['tool_name']}", incident_id=incident_id)
        self.runtime.add_activity(server_id, "incident", activity, incident_id=incident_id, details={"status": status})


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return dict(value) if isinstance(value, dict) else {"value": value}


def _read_tool_names(messages: list[Any]) -> list[str]:
    names: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            names.extend(
                str(call.get("name"))
                for call in message.tool_calls
                if str(call.get("name")) in READ_TOOL_NAMES
            )
    return names
