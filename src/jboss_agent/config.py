"""学習用デモで使う最小限の設定を環境変数と ``.env`` から読む。

本番アプリの設定管理を再現することは目的ではないため、複雑な設定クラスや
DB 接続設定は持たない。Gemini、対象サーバー、Fake JBoss の保存先だけを扱う。
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

    @property
    def has_google_api_key(self) -> bool:
        """Gemini API キーが空でないかを返す。認証可否までは確認しない。"""
        return bool(self.google_api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """``.env`` と環境変数を読み、プロセス内で同じ設定を再利用する。"""
    load_dotenv()
    return Settings(
        google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        server_id=os.getenv("SERVER_ID", "jboss-01"),
        fake_jboss_data_dir=os.getenv("FAKE_JBOSS_DATA_DIR", ".data/fake_jboss"),
    )
