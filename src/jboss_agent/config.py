""".env と環境変数からアプリケーション設定を読み込む。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """デモアプリの設定値と入力検証をまとめる。"""

    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    google_api_key: SecretStr | None = Field(default=None, alias="GOOGLE_API_KEY")
    gemini_model: str = Field(default="gemini-3.5-flash", alias="GEMINI_MODEL")
    gemini_temperature: float = Field(default=1.0, ge=0.0, le=2.0, alias="GEMINI_TEMPERATURE")

    teams_webhook_url: str | None = Field(default=None, alias="TEAMS_WEBHOOK_URL")
    teams_dry_run: bool = Field(default=True, alias="TEAMS_DRY_RUN")

    server_id: str = Field(default="jboss-01", min_length=1, alias="SERVER_ID")
    fake_jboss_data_dir: str = Field(default=".data/fake_jboss", alias="FAKE_JBOSS_DATA_DIR")
    checkpoint_db_path: str = Field(default=".data/checkpoints.sqlite", alias="CHECKPOINT_DB_PATH")
    runtime_db_path: str = Field(default=".data/runtime.sqlite", alias="RUNTIME_DB_PATH")
    simulator_db_path: str = Field(default=".data/simulator.sqlite", alias="SIMULATOR_DB_PATH")

    max_investigation_rounds: int = Field(default=5, ge=1, alias="MAX_INVESTIGATION_ROUNDS")
    max_recovery_attempts: int = Field(default=2, ge=1, alias="MAX_RECOVERY_ATTEMPTS")

    @field_validator("google_api_key", mode="before")
    @classmethod
    def empty_api_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("teams_webhook_url", mode="before")
    @classmethod
    def empty_webhook_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "gemini_model",
        "server_id",
        "fake_jboss_data_dir",
        "checkpoint_db_path",
        "runtime_db_path",
        "simulator_db_path",
    )
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @property
    def has_google_api_key(self) -> bool:
        return self.google_api_key is not None and bool(self.google_api_key.get_secret_value().strip())

    @property
    def monitoring_thread_id(self) -> str:
        return f"monitor:{self.server_id}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
