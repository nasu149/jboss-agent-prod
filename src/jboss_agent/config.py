"""環境変数と作業ディレクトリの .env から設定を読み、型と値を検証する。

Settings がモデル・通知・保存先・試行上限をまとめ、get_settings が読み込み結果を
プロセス内で再利用する。環境変数は .env の同名設定より優先される。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """デモアプリの設定値、既定値、および読み込み時の入力検証をまとめる。

    Field の alias が環境変数名に対応し、数値の上下限なども読み込み時に検証する。
    保存先はパス文字列として保持し、実際のディレクトリや DB の作成は各ストアに任せる。
    """

    # .env は作業ディレクトリ基準で読み、キー名の大文字小文字を区別せず、余分な設定は無視する。
    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # SecretStr は通常の表示でキーをマスクする。クライアント生成時だけ秘密値を取り出す。
    google_api_key: SecretStr | None = Field(default=None, alias="GOOGLE_API_KEY")
    gemini_model: str = Field(default="gemini-3.5-flash", alias="GEMINI_MODEL")
    gemini_temperature: float = Field(default=1.0, ge=0.0, le=2.0, alias="GEMINI_TEMPERATURE")

    # 通知設定。既定では DRY RUN とし、実際の通知の扱いは Teams ツール側で判断する。
    teams_webhook_url: str | None = Field(default=None, alias="TEAMS_WEBHOOK_URL")
    teams_dry_run: bool = Field(default=True, alias="TEAMS_DRY_RUN")

    server_id: str = Field(default="jboss-01", min_length=1, alias="SERVER_ID")
    fake_jboss_data_dir: str = Field(default=".data/fake_jboss", alias="FAKE_JBOSS_DATA_DIR")
    # グラフの再開用、画面表示用、答え合わせ用の DB を分けて保存する。
    checkpoint_db_path: str = Field(default=".data/checkpoints.sqlite", alias="CHECKPOINT_DB_PATH")
    runtime_db_path: str = Field(default=".data/runtime.sqlite", alias="RUNTIME_DB_PATH")
    simulator_db_path: str = Field(default=".data/simulator.sqlite", alias="SIMULATOR_DB_PATH")

    # 調査ラウンドの上限と、再調査をまたいで数える復旧試行の上限をそれぞれ設定する。
    max_investigation_rounds: int = Field(default=5, ge=1, alias="MAX_INVESTIGATION_ROUNDS")
    max_recovery_attempts: int = Field(default=2, ge=1, alias="MAX_RECOVERY_ATTEMPTS")

    @field_validator("google_api_key", mode="before")
    @classmethod
    def empty_api_key_is_none(cls, value: object) -> object:
        """API キーが空文字または空白だけなら、SecretStr への変換前に None にする。

        空でない入力はそのまま返し、前後の空白の除去は行わない。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("teams_webhook_url", mode="before")
    @classmethod
    def empty_webhook_is_none(cls, value: object) -> object:
        """Webhook URL の空文字・空白だけの入力を None に揃え、未設定として扱う。"""
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
        """モデル名・サーバー ID・保存先の前後の空白を除去し、空なら ValueError とする。

        文字列の空チェックであり、モデルの存在やパスへのアクセス可否は確認しない。
        """
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @property
    def has_google_api_key(self) -> bool:
        """空白以外の内容を持つ API キーが設定されているかを返す。認証の成否は確認しない。"""
        return self.google_api_key is not None and bool(self.google_api_key.get_secret_value().strip())

    @property
    def monitoring_thread_id(self) -> str:
        """対象サーバーの監視チェックポイントを継続利用するための固定 ID を返す。"""
        return f"monitor:{self.server_id}"


# Streamlit の再実行でも、同じプロセス内では検証済みの設定を再利用する。
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """検証済み Settings を生成し、以降の呼び出しでは同じインスタンスを返す。

    環境変数や .env の変更は自動反映されない。再読み込みにはプロセスを再起動するか、
    get_settings.cache_clear() でキャッシュを破棄してから呼び直す。
    """
    return Settings()
