"""Small SQLite store for dashboard metadata.

LangGraph State itself is stored separately by the LangGraph checkpointer.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MonitoringStatus:
    server_id: str
    status: str
    last_scan_at: str | None
    last_error: str | None
    previous_cursor: int
    current_cursor: int
    last_incident_id: str | None


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    thread_id: str
    server_id: str
    category: str
    severity: str
    confidence: float
    summary: str
    status: str
    created_at: str
    updated_at: str
    pending_approval: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    proposed_action: dict[str, Any] | None
    recovered: bool | None
    failure_reason: str | None
    investigation_tool_calls: int


class RuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitoring_status (
                    server_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_scan_at TEXT,
                    last_error TEXT,
                    previous_cursor INTEGER NOT NULL DEFAULT 0,
                    current_cursor INTEGER NOT NULL DEFAULT 0,
                    last_incident_id TEXT
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    pending_approval_json TEXT,
                    diagnosis_json TEXT,
                    proposed_action_json TEXT,
                    recovered INTEGER,
                    failure_reason TEXT,
                    investigation_tool_calls INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    incident_id TEXT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT
                );
                """
            )

    def begin_scan(self, server_id: str) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring_status(server_id, status, last_scan_at, last_error)
                VALUES (?, 'SCANNING', ?, NULL)
                ON CONFLICT(server_id) DO UPDATE SET
                    status='SCANNING', last_scan_at=excluded.last_scan_at, last_error=NULL
                """,
                (server_id, now),
            )
        self.add_activity(server_id, "monitoring", "Monitoring scan started")

    def complete_scan(
        self,
        server_id: str,
        *,
        previous_cursor: int,
        current_cursor: int,
        incident_id: str | None,
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring_status(
                    server_id, status, last_scan_at, last_error,
                    previous_cursor, current_cursor, last_incident_id
                ) VALUES (?, 'IDLE', ?, NULL, ?, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    status='IDLE', last_scan_at=excluded.last_scan_at, last_error=NULL,
                    previous_cursor=excluded.previous_cursor,
                    current_cursor=excluded.current_cursor,
                    last_incident_id=COALESCE(excluded.last_incident_id, monitoring_status.last_incident_id)
                """,
                (server_id, now, previous_cursor, current_cursor, incident_id),
            )

    def fail_scan(self, server_id: str, error: str) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring_status(server_id, status, last_scan_at, last_error)
                VALUES (?, 'ERROR', ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    status='ERROR', last_scan_at=excluded.last_scan_at, last_error=excluded.last_error
                """,
                (server_id, now, error),
            )
        self.add_activity(server_id, "error", f"Monitoring scan failed: {error}")

    def get_monitoring_status(self, server_id: str) -> MonitoringStatus:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM monitoring_status WHERE server_id=?", (server_id,)).fetchone()
        if row is None:
            return MonitoringStatus(server_id, "NOT_STARTED", None, None, 0, 0, None)
        return MonitoringStatus(
            server_id=row["server_id"],
            status=row["status"],
            last_scan_at=row["last_scan_at"],
            last_error=row["last_error"],
            previous_cursor=int(row["previous_cursor"]),
            current_cursor=int(row["current_cursor"]),
            last_incident_id=row["last_incident_id"],
        )

    def upsert_incident(
        self,
        *,
        incident_id: str,
        thread_id: str,
        server_id: str,
        category: str,
        severity: str,
        confidence: float,
        summary: str,
        status: str,
        pending_approval: dict[str, Any] | None = None,
        diagnosis: dict[str, Any] | None = None,
        proposed_action: dict[str, Any] | None = None,
        recovered: bool | None = None,
        failure_reason: str | None = None,
        investigation_tool_calls: int = 0,
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO incidents(
                    incident_id, thread_id, server_id, category, severity, confidence,
                    summary, status, created_at, updated_at, pending_approval_json,
                    diagnosis_json, proposed_action_json, recovered, failure_reason,
                    investigation_tool_calls
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    pending_approval_json=excluded.pending_approval_json,
                    diagnosis_json=excluded.diagnosis_json,
                    proposed_action_json=excluded.proposed_action_json,
                    recovered=excluded.recovered, failure_reason=excluded.failure_reason,
                    investigation_tool_calls=excluded.investigation_tool_calls
                """,
                (
                    incident_id,
                    thread_id,
                    server_id,
                    category,
                    severity,
                    confidence,
                    summary,
                    status,
                    now,
                    now,
                    _dump(pending_approval),
                    _dump(diagnosis),
                    _dump(proposed_action),
                    None if recovered is None else int(recovered),
                    failure_reason,
                    investigation_tool_calls,
                ),
            )

    def get_incident(self, incident_id: str) -> IncidentRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        return _incident(row) if row is not None else None

    def list_incidents(self, *, limit: int = 20) -> list[IncidentRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_incident(row) for row in rows]

    def list_pending_approvals(self) -> list[IncidentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE status='PENDING_APPROVAL' ORDER BY created_at"
            ).fetchall()
        return [_incident(row) for row in rows]

    def add_activity(
        self,
        server_id: str,
        event_type: str,
        message: str,
        *,
        incident_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO activity(timestamp, server_id, incident_id, event_type, message, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (utc_now_iso(), server_id, incident_id, event_type, message, _dump(details)),
            )

    def list_activity(self, server_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activity WHERE server_id=? ORDER BY id DESC LIMIT ?",
                (server_id, limit),
            ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "incident_id": row["incident_id"],
                "event_type": row["event_type"],
                "message": row["message"],
                "details": _load(row["details_json"]),
            }
            for row in rows
        ]


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _incident(row: sqlite3.Row) -> IncidentRecord:
    recovered = row["recovered"]
    return IncidentRecord(
        incident_id=row["incident_id"],
        thread_id=row["thread_id"],
        server_id=row["server_id"],
        category=row["category"],
        severity=row["severity"],
        confidence=float(row["confidence"]),
        summary=row["summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        pending_approval=_load(row["pending_approval_json"]),
        diagnosis=_load(row["diagnosis_json"]),
        proposed_action=_load(row["proposed_action_json"]),
        recovered=None if recovered is None else bool(recovered),
        failure_reason=row["failure_reason"],
        investigation_tool_calls=int(row["investigation_tool_calls"]),
    )
