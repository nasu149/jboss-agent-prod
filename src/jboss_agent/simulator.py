"""デモ用の疑似障害・正常イベントを投入し、答え合わせ用の正解を別 DB に保存する。

Fake JBoss には観測可能な設定・メトリクス・ログだけを反映する。シナリオ名と
障害 ID の対応は GroundTruthStore が管理し、エージェントの診断入力から分離する。
"""

from __future__ import annotations

import random
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jboss_agent.fake_jboss import FakeJBossOperations
from jboss_agent.runtime_store import utc_now_iso

# 正常イベントも選択肢に含め、誤検知しないこともデモで確認できるようにする。
Scenario = Literal[
    "THREAD_POOL_CONFIGURATION",
    "DATASOURCE_POOL_EXHAUSTION",
    "DEPLOYMENT_FAILURE",
    "NORMAL_ACTIVITY",
]
SCENARIOS: tuple[Scenario, ...] = (
    "THREAD_POOL_CONFIGURATION",
    "DATASOURCE_POOL_EXHAUSTION",
    "DEPLOYMENT_FAILURE",
    "NORMAL_ACTIVITY",
)


@dataclass(frozen=True)
class GroundTruthEvent:
    """投入したイベントの正解と、検出された障害への対応を表す変更不可のレコード。

    injected_at は投入を記録した UTC 日時、linked_incident_id は未関連付けなら None。
    """
    event_id: str
    server_id: str
    scenario: Scenario
    injected_at: str
    linked_incident_id: str | None


class GroundTruthStore:
    """エージェントが読む疑似サーバー状態とは別の SQLite DB で正解を管理する。"""

    def __init__(self, path: str | Path) -> None:
        """保存先の親ディレクトリと正解テーブルを、存在しない場合に作成する。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ground_truth_events (
                    event_id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    injected_at TEXT NOT NULL,
                    linked_incident_id TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        """列名で行を参照できる SQLite 接続を開き、WAL モードを設定して返す。"""
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(self, event_id: str, server_id: str, scenario: Scenario) -> GroundTruthEvent:
        """イベントの正解と現在の UTC 日時を保存し、未関連付けのレコードを返す。"""
        injected_at = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ground_truth_events(event_id, server_id, scenario, injected_at) VALUES (?, ?, ?, ?)",
                (event_id, server_id, scenario, injected_at),
            )
        return GroundTruthEvent(event_id, server_id, scenario, injected_at, None)

    def link_latest_unlinked(self, server_id: str, incident_id: str) -> str | None:
        """対象サーバーの最新の未関連付けイベントを障害 ID に結び、そのイベント ID を返す。

        対応候補は投入日時で選び、ログ内容との照合は行わない。候補がなければ None。
        """
        with self._connect() as conn:
            # 未関連付けの最新イベントを対応候補とする、デモ用の簡易的な関連付け。
            row = conn.execute(
                """
                SELECT event_id FROM ground_truth_events
                WHERE server_id=? AND linked_incident_id IS NULL
                ORDER BY injected_at DESC LIMIT 1
                """,
                (server_id,),
            ).fetchone()
            if row is None:
                return None
            event_id = str(row["event_id"])
            conn.execute(
                "UPDATE ground_truth_events SET linked_incident_id=? WHERE event_id=?",
                (incident_id, event_id),
            )
            return event_id

    def get(self, event_id: str) -> GroundTruthEvent | None:
        """イベント ID で正解レコードを取得する。見つからなければ None を返す。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ground_truth_events WHERE event_id=?", (event_id,)).fetchone()
        return _event(row) if row else None

    def latest(self, server_id: str) -> GroundTruthEvent | None:
        """対象サーバーで投入日時が最新の正解レコードを返す。未投入なら None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ground_truth_events WHERE server_id=? ORDER BY injected_at DESC LIMIT 1",
                (server_id,),
            ).fetchone()
        return _event(row) if row else None


class FaultInjector:
    """疑似サーバーにシナリオの兆候を作り、正解を専用ストアへ記録する。"""

    def __init__(self, fake: FakeJBossOperations, truth: GroundTruthStore) -> None:
        """変更対象の Fake JBoss と、正解を記録するストアを保持する。"""
        self.fake = fake
        self.truth = truth

    def inject_random(self) -> GroundTruthEvent:
        """正常系を含む SCENARIOS からランダムに1つを投入し、正解レコードを返す。"""
        return self.inject(random.SystemRandom().choice(SCENARIOS))

    def inject(self, scenario: Scenario) -> GroundTruthEvent:
        """指定シナリオの設定・負荷・ログを作り、投入したイベントの正解レコードを返す。

        先に設定とメトリクスを正常値へ戻し、前の障害の影響を除く。ログは消さず追記し、
        監視カーソルを維持する。未対応のシナリオは ValueError とする。
        """
        event_id = f"evt-{uuid.uuid4().hex[:10]}"
        self.fake.ensure_initialized()
        # 各シナリオを独立して試せるよう、メトリクスと設定だけを初期化する。
        # 監視カーソルを維持するため、server.log は追記された状態を保つ。
        self.fake.restore_baseline_state()

        # 上限を小さくした変更履歴と負荷を作り、以前の設定へ戻す根拠を残す。
        if scenario == "THREAD_POOL_CONFIGURATION":
            self.fake.set_thread_pool_max_threads(self.fake.server_id, 20)
            self.fake.simulate_thread_pool_load(
                active_threads=20, queue_size=37, rejected_tasks=11, error_rate=0.24
            )
            self.fake.append_log_lines(
                [
                    "2026-09-05 18:20:01 WARN  [org.example.web] HTTP worker queue growth detected",
                    "2026-09-05 18:20:02 ERROR [org.example.web] task rejected from worker executor",
                    "2026-09-05 18:20:03 WARN  [org.example.web] HTTP 503 responses increased",
                ]
            )
        elif scenario == "DATASOURCE_POOL_EXHAUSTION":
            self.fake.set_datasource_max_pool_size(self.fake.server_id, 5)
            self.fake.simulate_datasource_load(active_count=5, timed_out_requests=14, error_rate=0.28)
            self.fake.append_log_lines(
                [
                    "2026-09-05 18:21:01 WARN  [org.jboss.jca] datasource pool has no available connection",
                    "2026-09-05 18:21:02 ERROR [org.example.dao] timed out waiting for ExampleDS connection",
                    "2026-09-05 18:21:03 WARN  [org.example.api] database-backed requests returning 503",
                ]
            )
        elif scenario == "DEPLOYMENT_FAILURE":
            self.fake.simulate_deployment_failure("app.war")
            self.fake.append_log_lines(
                [
                    "2026-09-05 18:22:01 ERROR [org.jboss.as.server] deployment app.war failed to start",
                    "2026-09-05 18:22:02 ERROR [org.example.App] application endpoint unavailable",
                    "2026-09-05 18:22:03 WARN  [org.example.health] readiness check returned 503",
                ]
            )
        # 正常系は初期化後のメトリクスを維持し、成功ログだけを追加する。
        elif scenario == "NORMAL_ACTIVITY":
            self.fake.append_log_lines(
                [
                    "2026-09-05 18:23:01 INFO  [org.example.web] request completed status=200 elapsed=42ms",
                    "2026-09-05 18:23:02 INFO  [org.example.jobs] scheduled cleanup completed",
                    "2026-09-05 18:23:03 INFO  [org.example.health] readiness check returned 200",
                ]
            )
        else:  # pragma: no cover
            raise ValueError(f"unsupported scenario: {scenario}")

        # 投入が完了してから正解を別 DB に保存する。Fake JBoss にはシナリオ名を書かない。
        return self.truth.record(event_id, self.fake.server_id, scenario)


def normalize_diagnosis(value: str | None) -> str | None:
    """診断名の大小文字・区切り文字・既知の別名を、答え合わせ用に正規化する。

    空の入力は None、別名表にない値は文字表記だけを正規化して返す。
    """
    if not value:
        return None
    text = value.strip().upper().replace("-", "_").replace(" ", "_")
    return {
        "THREAD_POOL": "THREAD_POOL_CONFIGURATION",
        "THREAD_POOL_EXHAUSTION": "THREAD_POOL_CONFIGURATION",
        "DATASOURCE": "DATASOURCE_POOL_EXHAUSTION",
        "DATASOURCE_POOL": "DATASOURCE_POOL_EXHAUSTION",
        "DEPLOYMENT": "DEPLOYMENT_FAILURE",
    }.get(text, text)


def _event(row: sqlite3.Row) -> GroundTruthEvent:
    """SQLite の行を、画面やサービスが扱う GroundTruthEvent に変換する。"""
    return GroundTruthEvent(
        event_id=row["event_id"],
        server_id=row["server_id"],
        scenario=row["scenario"],
        injected_at=row["injected_at"],
        linked_incident_id=row["linked_incident_id"],
    )
