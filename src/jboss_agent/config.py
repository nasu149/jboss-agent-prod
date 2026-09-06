"""学習用デモの設定を環境変数と ``.env`` から読む。

Gemini、Teams 通知、対象 Fake JBoss の設定だけを扱う。DB や複数環境向けの
複雑な設定管理は持たない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """このデモを動かすために必要な設定だけを保持する。"""

    google_api_key: str
    gemini_model: str
    server_id: str
    fake_jboss_data_dir: str
    teams_webhook_url: str = ""
    teams_dry_run: bool = True
    langgraph_debug: bool = False

    @property
    def has_google_api_key(self) -> bool:
        """Gemini API キーが空でないかを返す。認証可否までは確認しない。"""
        return bool(self.google_api_key.strip())


def _env_bool(name: str, default: bool) -> bool:
    """true/false 系の環境変数を bool に変換する。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """``.env`` と環境変数を読み、プロセス内で同じ設定を再利用する。"""
    load_dotenv()
    return Settings(
        google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        server_id=os.getenv("SERVER_ID", "jboss-01"),
        fake_jboss_data_dir=os.getenv("FAKE_JBOSS_DATA_DIR", ".data/fake_jboss"),
        teams_webhook_url=os.getenv("TEAMS_WEBHOOK_URL", ""),
        teams_dry_run=_env_bool("TEAMS_DRY_RUN", True),
        langgraph_debug=_env_bool("LANGGRAPH_DEBUG", False),
    )
