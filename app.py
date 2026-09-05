"""Streamlit entry point for the JBoss incident-response demo."""

from __future__ import annotations

import asyncio
from typing import Any

import streamlit as st

from jboss_agent.config import get_settings
from jboss_agent.fake_jboss import FakeJBossOperations
from jboss_agent.runtime_store import IncidentRecord, RuntimeStore
from jboss_agent.service import AgentService
from jboss_agent.simulator import (
    SCENARIOS,
    FaultInjector,
    GroundTruthEvent,
    GroundTruthStore,
    normalize_diagnosis,
)


settings = get_settings()
runtime = RuntimeStore(settings.runtime_db_path)
truth = GroundTruthStore(settings.simulator_db_path)
fake = FakeJBossOperations(settings.fake_jboss_data_dir, server_id=settings.server_id)
fake.ensure_initialized()
injector = FaultInjector(fake, truth)
service = AgentService(settings, runtime, truth)


def run_async(coro: Any) -> Any:
    """Streamlit callbacks are synchronous; each action gets a short-lived event loop."""
    return asyncio.run(coro)


def run_agent_action(coro: Any, label: str) -> bool:
    try:
        with st.spinner(label):
            run_async(coro)
        return True
    except Exception as exc:  # Surface operational errors in the UI instead of a raw traceback.
        st.error(f"Agent execution failed: {exc}")
        return False


def terminal(record: IncidentRecord) -> bool:
    return record.status not in {"INVESTIGATING", "PENDING_APPROVAL"}


def render_sidebar() -> None:
    with st.sidebar:
        st.header("How to try it")
        st.markdown(
            "1. **Inject Random Event**\n"
            "2. **Run scan now**\n"
            "3. If remediation is proposed, **Approve / Reject**\n"
            "4. Check the incident and activity tables"
        )
        st.divider()
        st.caption(f"Gemini: `{settings.gemini_model}`")
        st.caption(f"Server: `{settings.server_id}`")
        st.caption(f"Teams: `{'DRY RUN' if settings.teams_dry_run else 'LIVE'}`")
        st.caption("JBoss backend: Fake JBoss via MCP/stdio")


def render_server_snapshot() -> None:
    health = fake.get_server_health(settings.server_id)
    thread_pool = fake.get_thread_pool_status(settings.server_id)
    datasource = fake.get_datasource_status(settings.server_id)
    deployment = fake.get_deployment_status(settings.server_id)

    st.subheader("Server status")
    cols = st.columns(4)
    cols[0].metric("Server", str(health["status"]))
    cols[1].metric("Error rate", f"{float(health['request_error_rate']):.1%}")
    cols[2].metric(
        "Thread pool",
        f"{thread_pool['active_threads']}/{thread_pool['max_threads']}",
        help=f"queue={thread_pool['queue_size']}, rejected={thread_pool['rejected_tasks']}",
    )
    cols[3].metric(
        "Datasource",
        f"{datasource['active_count']}/{datasource['max_pool_size']}",
        help=f"timeouts={datasource['timed_out_requests']}",
    )
    st.caption(
        f"Deployment {deployment['name']}: status={deployment['status']}, "
        f"enabled={deployment['enabled']}"
    )


def render_monitoring_status() -> None:
    status = runtime.get_monitoring_status(settings.server_id)
    st.subheader("Monitoring state")
    cols = st.columns(4)
    cols[0].metric("Status", status.status)
    cols[1].metric("Log cursor", status.current_cursor)
    cols[2].metric("Previous cursor", status.previous_cursor)
    cols[3].metric("Last scan", status.last_scan_at or "—")
    if status.last_error:
        st.error(status.last_error)


def render_controls() -> None:
    pending = runtime.list_pending_approvals()
    st.subheader("Controls")
    left, right = st.columns(2)

    if left.button(
        "Run scan now",
        type="primary",
        use_container_width=True,
        disabled=not settings.has_google_api_key,
    ):
        if run_agent_action(service.run_scan(), "Running Monitoring Graph..."):
            st.rerun()

    if right.button(
        "Inject Random Event",
        use_container_width=True,
        disabled=bool(pending),
        help="Injects a fake fault or normal event. The hidden answer is not passed to the Agent.",
    ):
        event = injector.inject_random()
        st.session_state["last_injected_event_id"] = event.event_id
        runtime.add_activity(
            settings.server_id,
            "simulator",
            "Random simulator event injected; Ground Truth hidden from Agent",
        )
        st.success("Event injected. Now click Run scan now.")

    with st.expander("Choose a specific demo scenario"):
        chosen = st.selectbox("Scenario", SCENARIOS)
        if st.button("Inject selected scenario", disabled=bool(pending)):
            event = injector.inject(chosen)
            st.session_state["last_injected_event_id"] = event.event_id
            runtime.add_activity(
                settings.server_id,
                "simulator",
                "Selected simulator event injected; Ground Truth hidden from Agent",
            )
            st.success("Scenario injected. Now click Run scan now.")


def render_approvals() -> None:
    pending = runtime.list_pending_approvals()
    st.subheader("Human approval")
    if not pending:
        st.info("No pending remediation approval.")
        return

    for record in pending:
        payload = record.pending_approval or {}
        with st.container(border=True):
            st.markdown(f"**Incident `{record.incident_id}`**")
            c1, c2, c3 = st.columns(3)
            c1.write(f"Action: `{payload.get('action')}`")
            c2.write(f"Risk: **{payload.get('risk')}**")
            c3.write(
                f"Current → proposed: `{payload.get('current_value')}` → "
                f"`{payload.get('proposed_value')}`"
            )
            st.write(payload.get("reason") or "No reason supplied")

            approve, reject = st.columns(2)
            if approve.button(
                "Approve",
                key=f"approve-{record.incident_id}",
                use_container_width=True,
            ):
                if run_agent_action(
                    service.resume_incident(record.incident_id, decision="approve"),
                    "Resuming the same LangGraph thread...",
                ):
                    st.rerun()

            if reject.button(
                "Reject",
                key=f"reject-{record.incident_id}",
                use_container_width=True,
            ):
                if run_agent_action(
                    service.resume_incident(record.incident_id, decision="reject"),
                    "Rejecting remediation...",
                ):
                    st.rerun()

            default_value = payload.get("proposed_value")
            if isinstance(default_value, int):
                edited = st.number_input(
                    "Edit proposed value",
                    value=default_value,
                    step=1,
                    key=f"edit-value-{record.incident_id}",
                )
                if st.button(
                    "Edit & Approve",
                    key=f"edit-approve-{record.incident_id}",
                    use_container_width=True,
                ):
                    if run_agent_action(
                        service.resume_incident(
                            record.incident_id,
                            decision="edit_and_approve",
                            proposed_value=int(edited),
                        ),
                        "Validating the edited value and resuming...",
                    ):
                        st.rerun()


def render_incidents() -> None:
    incidents = runtime.list_incidents(limit=20)
    st.subheader("Incidents")
    if not incidents:
        st.info("No incidents yet.")
        return

    st.dataframe(
        [
            {
                "incident_id": item.incident_id,
                "status": item.status,
                "category": item.category,
                "severity": item.severity,
                "confidence": item.confidence,
                "read_tool_calls": item.investigation_tool_calls,
                "recovered": item.recovered,
                "created_at": item.created_at,
            }
            for item in incidents
        ],
        use_container_width=True,
        hide_index=True,
    )


def ground_truth_is_revealable(event: GroundTruthEvent) -> bool:
    if event.linked_incident_id:
        record = runtime.get_incident(event.linked_incident_id)
        return bool(record and terminal(record))
    monitoring = runtime.get_monitoring_status(event.server_id)
    return bool(monitoring.last_scan_at and monitoring.last_scan_at >= event.injected_at)


def render_ground_truth() -> None:
    st.subheader("Demo answer check")
    event_id = st.session_state.get("last_injected_event_id")
    event = truth.get(event_id) if event_id else truth.latest(settings.server_id)
    if event is None:
        st.info("Inject an event to create a hidden Ground Truth answer.")
        return
    if not ground_truth_is_revealable(event):
        st.warning("Ground Truth is hidden until the Agent finishes this event.")
        return

    st.write(f"Injected scenario: **{event.scenario}**")
    if event.linked_incident_id:
        record = runtime.get_incident(event.linked_incident_id)
        if record is None:
            return
        actual = normalize_diagnosis((record.diagnosis or {}).get("root_cause"))
        st.write(f"Agent diagnosis: **{actual or 'N/A'}**")
        st.write(f"Diagnosis: **{'Correct' if actual == event.scenario else 'Incorrect'}**")
        st.write(f"Recovery: **{'Success' if record.recovered else 'Failed / not applicable'}**")
    else:
        st.write("Agent created no incident.")
        st.write(
            "Detection: **Correct**"
            if event.scenario == "NORMAL_ACTIVITY"
            else "Detection: **Missed**"
        )


def render_activity() -> None:
    rows = runtime.list_activity(settings.server_id, limit=80)
    st.subheader("Agent activity timeline")
    if not rows:
        st.info("No activity yet.")
        return
    rows.reverse()
    st.dataframe(
        [
            {
                "time": row["timestamp"],
                "incident": row["incident_id"] or "",
                "type": row["event_type"],
                "activity": row["message"],
            }
            for row in rows
        ],
        use_container_width=True,
        hide_index=True,
    )


st.set_page_config(page_title="JBoss Incident Agent", page_icon="🧭", layout="wide")
st.title("JBoss Incident Response Agent")
st.caption("LangGraph + Gemini + MCP + Human-in-the-loop, with a local Fake JBoss backend")

if not settings.has_google_api_key:
    st.error("GOOGLE_API_KEY is not configured. Copy `.env.example` to `.env` and set your Gemini API key.")

render_sidebar()
render_server_snapshot()
render_monitoring_status()
render_controls()
render_approvals()
render_incidents()
render_ground_truth()
render_activity()
