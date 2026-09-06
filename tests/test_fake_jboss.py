"""Fake JBoss の 3 障害が、固定 write 操作で復旧できることを確認する。"""

from jboss_agent.fake_jboss import FakeJBoss


def test_thread_pool_scenario_can_be_recovered(tmp_path):
    fake = FakeJBoss(tmp_path)
    fake.inject("THREAD_POOL_CONFIGURATION")
    assert fake.get_thread_pool_status("jboss-01")["queue_size"] == 37

    fake.set_thread_pool_max_threads("jboss-01", 80)
    status = fake.get_thread_pool_status("jboss-01")
    assert status["max_threads"] == 80
    assert status["queue_size"] == 0


def test_datasource_scenario_can_be_recovered(tmp_path):
    fake = FakeJBoss(tmp_path)
    fake.inject("DATASOURCE_POOL_EXHAUSTION")
    assert fake.get_datasource_status("jboss-01")["timed_out_requests"] == 14

    fake.set_datasource_max_pool_size("jboss-01", 30)
    status = fake.get_datasource_status("jboss-01")
    assert status["max_pool_size"] == 30
    assert status["timed_out_requests"] == 0


def test_deployment_scenario_can_be_recovered(tmp_path):
    fake = FakeJBoss(tmp_path)
    fake.inject("DEPLOYMENT_FAILURE")
    assert fake.get_deployment_status("jboss-01")["status"] == "FAILED"

    fake.restart_deployment("jboss-01", "app.war")
    assert fake.get_deployment_status("jboss-01")["status"] == "OK"
