"""監視状態・障害一覧・活動履歴を、ダッシュボード表示用の SQLite DB に保存する。

グラフの実行途中の状態は別のチェックポインターが保持する。このストアには
表示に必要な要約と、承認後にその実行へ戻るための thread_id を保存する。
各書き込みの接続コンテキストは正常終了時にコミットし、例外時にはロールバックする。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """保存日時に使う現在の UTC 時刻を、秒精度の ISO 8601 文字列で返す。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MonitoringStatus:
    """対象サーバーの最後に保存された監視状態を表すレコード。

    last_scan_at は開始・完了・失敗の各保存時に更新され、成功日時だけを表すものではない。
    previous_cursor と current_cursor は表示用の直近読取区間で、単位はバイト。
    """
    server_id: str
    status: str
    last_scan_at: str | None
    last_error: str | None
    previous_cursor: int
    current_cursor: int
    last_incident_id: str | None


@dataclass(frozen=True)
class IncidentRecord:
    """画面に表示する障害の基本情報と、診断・提案・承認待ち・復旧結果のレコード。

    thread_id はグラフの再開先、recovered は未判定なら None。frozen は属性の再代入を
    防ぐが、pending_approval などの辞書の中身まで変更不可にはしない。
    """
    incident_id: str
    # チェックポイントの実行状態を参照する ID。承認後はこの ID のグラフを再開する。
    thread_id: str
    server_id: str
    category: str
    severity: str
    confidence: float
    summary: str
    status: str
    created_at: str
    updated_at: str
    # 承認待ちの画面表示用データ。グラフ自体の保存状態は別 DB が持つ。
    pending_approval: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    proposed_action: dict[str, Any] | None
    recovered: bool | None
    failure_reason: str | None
    investigation_tool_calls: int


class RuntimeStore:
    """SQLite の監視・障害・活動履歴テーブルへの読み書きをまとめる。

    メソッドごとに接続して更新を確定する。監視状態の更新と add_activity の記録は
    別トランザクションであり、一括で確定・取り消しされるわけではない。
    """

    def __init__(self, path: str | Path) -> None:
        """DB 保存先の親ディレクトリを作成し、不足するテーブルを初期化する。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """列名で値を取得できる SQLite 接続を作り、WAL と10秒のロック待機を設定する。

        接続の取得だけを行い、この時点ではデータの読み書きは行わない。
        """
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        """監視状態・障害・活動履歴の3テーブルを、存在しない場合に作成する。

        既存テーブルのデータは保持する。既存スキーマを変更する移行処理ではない。
        """
        with self._connect() as conn:
            # 監視はサーバーごとに1行、障害は障害 ID ごとに1行、活動履歴は追記形式で保存する。
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
        """監視状態を SCANNING にして日時とエラー表示を更新し、開始履歴を追加する。

        既存行がある場合、前回のカーソルと最後の障害 ID は維持する。
        """
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
        """監視状態を IDLE に戻し、今回の読取区間・完了日時・検知した障害 ID を保存する。

        previous_cursor と current_cursor は今回の開始位置と末尾位置。incident_id が
        None の場合、既存の最後の障害 ID を保持する。活動履歴はこのメソッドでは追加しない。
        """
        now = utc_now_iso()
        with self._connect() as conn:
            # COALESCE で、今回障害がなくても過去の最後の障害 ID を保持する。
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
        """監視状態を ERROR にし、失敗日時とエラー内容を保存して失敗履歴を追加する。

        既存のカーソルや最後の障害 ID は更新せずに残す。
        """
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
        """対象サーバーの監視状態を返す。未登録なら NOT_STARTED の初期値を返す。

        初期値を返すだけで、DB に新しい行は追加しない。
        """
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
        # チェックポイントの実行状態を参照する ID。承認後はこの ID のグラフを再開する。
    thread_id: str,
        server_id: str,
        category: str,
        severity: str,
        confidence: float,
        summary: str,
        status: str,
        # 承認待ちの画面表示用データ。グラフ自体の保存状態は別 DB が持つ。
    pending_approval: dict[str, Any] | None = None,
        diagnosis: dict[str, Any] | None = None,
        proposed_action: dict[str, Any] | None = None,
        recovered: bool | None = None,
        failure_reason: str | None = None,
        investigation_tool_calls: int = 0,
    ) -> None:
        """障害を新規登録するか、既存障害の対応状況・診断・承認情報などを更新する。

        既存行の基本情報（thread_id、サーバー、分類、重要度、確信度、要約）と作成日時は
        変更しない。任意項目を省略した場合も、その既定値で対応する更新対象列を上書きする。
        辞書は JSON、復旧成否は NULL・0・1 として保存する。
        """
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
                    # 構造化データは JSON にし、承認待ち解除時の None は SQL NULL として保存する。
                    _dump(pending_approval),
                    _dump(diagnosis),
                    _dump(proposed_action),
                    # 未判定・失敗・成功を NULL・0・1 で区別する。
                    None if recovered is None else int(recovered),
                    failure_reason,
                    investigation_tool_calls,
                ),
            )

    def get_incident(self, incident_id: str) -> IncidentRecord | None:
        """障害 ID に対応するレコードを復元して返す。見つからなければ None。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        return _incident(row) if row is not None else None

    def list_incidents(self, *, limit: int = 20) -> list[IncidentRecord]:
        """全サーバーの障害を作成日時の新しい順に、limit 件まで取得する。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_incident(row) for row in rows]

    def list_pending_approvals(self) -> list[IncidentRecord]:
        """全サーバーの承認待ち障害を、作成日時の古い順に取得する。"""
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
        """対象サーバーの活動履歴を、現在の UTC 日時で1件追加する。

        message は表示用の説明、event_type は種別。任意の障害 ID と詳細辞書も保存できる。
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO activity(timestamp, server_id, incident_id, event_type, message, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (utc_now_iso(), server_id, incident_id, event_type, message, _dump(details)),
            )

    def list_activity(self, server_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """対象サーバーの活動履歴を、追加 ID の新しい順に limit 件まで返す。

        詳細の JSON は復元して返す。時系列表示で古い順にする場合は呼び出し元で反転する。
        """
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
    """None は SQL NULL 用に維持し、その他は日本語を保持した JSON 文字列にする。

    標準で JSON 化できないオブジェクトは default=str により文字列として保存する。
    """
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None) -> Any:
    """SQL NULL は None、それ以外は JSON を解析した値に戻す。不正な JSON の例外は送出する。"""
    return None if value is None else json.loads(value)


def _incident(row: sqlite3.Row) -> IncidentRecord:
    """DB の障害行を IncidentRecord に変換し、JSON・数値・復旧成否の型を復元する。

    復旧成否の NULL は False にせず None とし、未判定と失敗を区別する。
    """
    # DB の nullable な整数を、未判定を保持した bool または None に戻す。
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
