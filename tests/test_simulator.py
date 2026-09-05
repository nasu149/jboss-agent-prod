from jboss_agent.fake_jboss import FakeJBossOperations
from jboss_agent.simulator import FaultInjector, GroundTruthStore


def test_ground_truth_is_not_stored_in_agent_visible_jboss_state(tmp_path) -> None:
    fake = FakeJBossOperations(tmp_path / "jboss", server_id="jboss-test")
    truth = GroundTruthStore(tmp_path / "truth.sqlite")
    event = FaultInjector(fake, truth).inject("THREAD_POOL_CONFIGURATION")

    assert "THREAD_POOL_CONFIGURATION" not in fake.state_path.read_text(encoding="utf-8")
    assert truth.get(event.event_id).scenario == "THREAD_POOL_CONFIGURATION"
    assert fake.get_thread_pool_status("jboss-test")["queue_size"] > 0


def test_new_scenario_starts_from_clean_server_state_without_truncating_log(tmp_path):
    fake = FakeJBossOperations(tmp_path / "jboss", server_id="jboss-01")
    truth = GroundTruthStore(tmp_path / "truth.sqlite")
    injector = FaultInjector(fake, truth)

    injector.inject("THREAD_POOL_CONFIGURATION")
    before_size = fake.log_path.stat().st_size
    assert fake.get_server_health("jboss-01")["request_error_rate"] > 0

    injector.inject("NORMAL_ACTIVITY")

    assert fake.log_path.stat().st_size > before_size
    assert fake.get_server_health("jboss-01")["request_error_rate"] == 0.0
    assert fake.get_thread_pool_status("jboss-01")["max_threads"] == 80
    assert fake.get_datasource_status("jboss-01")["max_pool_size"] == 30
    assert fake.get_deployment_status("jboss-01")["status"] == "OK"
