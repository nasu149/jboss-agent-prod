from pathlib import Path

from jboss_agent.config import Settings


def test_settings_load_minimal_env(tmp_path: Path, monkeypatch) -> None:
    for name in ("GOOGLE_API_KEY", "GEMINI_MODEL", "TEAMS_DRY_RUN", "SERVER_ID"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "GOOGLE_API_KEY=test-key\nGEMINI_MODEL=gemini-test\nTEAMS_DRY_RUN=true\nSERVER_ID=jboss-test\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env)
    assert settings.has_google_api_key
    assert settings.gemini_model == "gemini-test"
    assert settings.teams_dry_run is True
    assert settings.monitoring_thread_id == "monitor:jboss-test"


def test_empty_api_key_is_unconfigured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("GOOGLE_API_KEY=\n", encoding="utf-8")
    assert Settings(_env_file=env).has_google_api_key is False
