from jboss_agent.runtime_store import RuntimeStore


def test_runtime_store_tracks_cursor_and_pending_approval(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.begin_scan("jboss-test")
    store.complete_scan("jboss-test", previous_cursor=10, current_cursor=42, incident_id="inc-1")
    assert store.get_monitoring_status("jboss-test").current_cursor == 42

    store.upsert_incident(
        incident_id="inc-1",
        thread_id="incident:inc-1",
        server_id="jboss-test",
        category="THREAD_POOL",
        severity="HIGH",
        confidence=0.9,
        summary="thread issue",
        status="PENDING_APPROVAL",
        pending_approval={"action": "SET_THREAD_POOL_MAX_THREADS", "proposed_value": 80},
    )
    pending = store.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0].pending_approval["proposed_value"] == 80
