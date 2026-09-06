"""Teams 通知の dry-run と未設定時の動作を確認する。"""

from jboss_agent.config import Settings
from jboss_agent.teams import send_teams_alert


def test_teams_dry_run_returns_payload_without_network():
    settings = Settings("", "unused", "jboss-01", ".data/fake_jboss", teams_dry_run=True)

    result = send_teams_alert(
        settings,
        server_id="jboss-01",
        category="DEPLOYMENT_FAILURE",
        summary="app.war failed",
    )

    assert result["success"] is True
    assert result["status"] == "dry_run"
    assert "DEPLOYMENT_FAILURE" in result["payload"]["text"]


def test_teams_requires_url_when_dry_run_is_disabled():
    settings = Settings(
        "",
        "unused",
        "jboss-01",
        ".data/fake_jboss",
        teams_webhook_url="",
        teams_dry_run=False,
    )

    result = send_teams_alert(
        settings,
        server_id="jboss-01",
        category="THREAD_POOL_CONFIGURATION",
        summary="queue is full",
    )

    assert result["success"] is False
    assert result["status"] == "missing_webhook_url"
