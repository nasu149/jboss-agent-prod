from jboss_agent.fake_jboss import FakeJBossOperations


def test_thread_pool_write_is_validated_idempotent_and_recovers_backlog(tmp_path) -> None:
    fake = FakeJBossOperations(tmp_path / "fake", server_id="jboss-test")
    fake.reset(include_boot_logs=False)
    fake.set_thread_pool_max_threads("jboss-test", 20)
    fake.simulate_thread_pool_load(active_threads=20, queue_size=10, rejected_tasks=3)

    first = fake.set_thread_pool_max_threads("jboss-test", 80)
    second = fake.set_thread_pool_max_threads("jboss-test", 80)
    status = fake.get_thread_pool_status("jboss-test")

    assert first["changed"] is True
    assert second["changed"] is False
    assert status["queue_size"] == 0
    assert status["rejected_tasks"] == 0
