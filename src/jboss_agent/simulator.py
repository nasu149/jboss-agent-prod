"""デモの障害注入と、シミュレーター専用の正解情報の保存を行う。"""

from __future__ import annotations

import random
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jboss_agent.fake_jboss import FakeJBossOperations
from jboss_agent.runtime_store import utc_now_iso

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
    event_id: str
    server_id: str
    scenario: Scenario
    injected_at: str
    linked_incident_id: str | None


class GroundTruthStore:
    """Agent が Fake JBoss の状態から正解を読めないよう、保存先を分離する。"""

    def __init__(self, path: str | Path) -> None:
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
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(self, event_id: str, server_id: str, scenario: Scenario) -> GroundTruthEvent:
        injected_at = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ground_truth_events(event_id, server_id, scenario, injected_at) VALUES (?, ?, ?, ?)",
                (event_id, server_id, scenario, injected_at),
            )
        return GroundTruthEvent(event_id, server_id, scenario, injected_at, None)

    def link_latest_unlinked(self, server_id: str, incident_id: str) -> str | None:
        with self._connect() as conn:
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
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ground_truth_events WHERE event_id=?", (event_id,)).fetchone()
        return _event(row) if row else None

    def latest(self, server_id: str) -> GroundTruthEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ground_truth_events WHERE server_id=? ORDER BY injected_at DESC LIMIT 1",
                (server_id,),
            ).fetchone()
        return _event(row) if row else None


class FaultInjector:
    def __init__(self, fake: FakeJBossOperations, truth: GroundTruthStore) -> None:
        self.fake = fake
        self.truth = truth

    def inject_random(self) -> GroundTruthEvent:
        return self.inject(random.SystemRandom().choice(SCENARIOS))

    def inject(self, scenario: Scenario) -> GroundTruthEvent:
        event_id = f"evt-{uuid.uuid4().hex[:10]}"
        self.fake.ensure_initialized()
        # 各シナリオを独立して試せるよう、メトリクスと設定だけを初期化する。
        # 監視カーソルを維持するため、server.log は追記された状態を保つ。
        self.fake.restore_baseline_state()

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

        return self.truth.record(event_id, self.fake.server_id, scenario)


def normalize_diagnosis(value: str | None) -> str | None:
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
    return GroundTruthEvent(
        event_id=row["event_id"],
        server_id=row["server_id"],
        scenario=row["scenario"],
        injected_at=row["injected_at"],
        linked_incident_id=row["linked_incident_id"],
    )
