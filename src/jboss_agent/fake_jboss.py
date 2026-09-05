"""デモ用の Fake JBoss。状態をファイルに保存する。

LangGraph と別プロセスの MCP サーバーが同じサーバー状態を参照できるようにする。
Agent が参照するこの状態には、シミュレーターの正解情報を含めない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

_DEFAULT_STATE: dict[str, Any] = {
    "server_id": "jboss-01",
    "health": {
        "status": "UP",
        "cpu_percent": 34.0,
        "heap_used_percent": 41.0,
        "request_error_rate": 0.0,
    },
    "thread_pool": {
        "name": "default",
        "max_threads": 80,
        "active_threads": 12,
        "queue_size": 0,
        "rejected_tasks": 0,
    },
    "datasource": {
        "name": "ExampleDS",
        "max_pool_size": 30,
        "active_count": 8,
        "available_count": 22,
        "timed_out_requests": 0,
    },
    "deployment": {
        "name": "app.war",
        "status": "OK",
        "enabled": True,
    },
    "recent_config_changes": [],
}

_DEFAULT_BOOT_LOGS = [
    "2026-09-05 17:00:00 INFO  [org.jboss.as] WFLYSRV0025: JBoss EAP started",
    "2026-09-05 17:00:05 INFO  [org.example.App] health endpoint returned 200",
]


class FakeJBossOperations:
    """MCP 経由で公開する、動作が決定的な JBoss シミュレーター。"""

    def __init__(self, data_dir: str | Path, *, server_id: str = "jboss-01") -> None:
        self.data_dir = Path(data_dir)
        self.server_id = server_id
        self.log_path = self.data_dir / "server.log"
        self.state_path = self.data_dir / "state.json"
        self._lock = RLock()

    def ensure_initialized(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            state = json.loads(json.dumps(_DEFAULT_STATE))
            state["server_id"] = self.server_id
            self._write_state(state)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")
            self.append_log_lines(_DEFAULT_BOOT_LOGS)

    def reset(self, *, include_boot_logs: bool = True) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        state = json.loads(json.dumps(_DEFAULT_STATE))
        state["server_id"] = self.server_id
        self._write_state(state)
        self.log_path.write_text("", encoding="utf-8")
        if include_boot_logs:
            self.append_log_lines(_DEFAULT_BOOT_LOGS)

    def restore_baseline_state(self) -> None:
        """Reset metrics/configuration without truncating server.log.

        Each injected demo scenario should start from the same healthy baseline, while
        the append-only log must remain intact so the monitoring cursor stays valid.
        """
        self.ensure_initialized()
        state = json.loads(json.dumps(_DEFAULT_STATE))
        state["server_id"] = self.server_id
        self._write_state(state)

    def append_log_lines(self, lines: list[str]) -> dict[str, int]:
        self.ensure_initialized_without_logs_if_needed()
        with self._lock:
            from_cursor = self.log_path.stat().st_size
            if lines:
                with self.log_path.open("ab") as stream:
                    for line in lines:
                        stream.write(line.rstrip("\n").encode("utf-8"))
                        stream.write(b"\n")
            to_cursor = self.log_path.stat().st_size
        return {"from_cursor": from_cursor, "to_cursor": to_cursor}

    # ------------------------------------------------------------------
    # 読み取り専用の操作
    # ------------------------------------------------------------------
    def read_server_log(self, server_id: str, cursor: int) -> dict[str, object]:
        self._validate_server_id(server_id)
        self.ensure_initialized()
        if cursor < 0:
            raise ValueError("cursor must be >= 0")

        file_size = self.log_path.stat().st_size
        if cursor > file_size:
            raise ValueError(
                f"cursor {cursor} is beyond current log size {file_size}; the log may have been reset"
            )

        with self.log_path.open("rb") as stream:
            stream.seek(cursor)
            raw = stream.read()
            to_cursor = stream.tell()

        return {
            "server_id": server_id,
            "from_cursor": cursor,
            "to_cursor": to_cursor,
            "lines": raw.decode("utf-8").splitlines(),
        }

    def get_server_health(self, server_id: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["health"]}

    def get_thread_pool_status(self, server_id: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["thread_pool"]}

    def get_datasource_status(self, server_id: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["datasource"]}

    def get_deployment_status(self, server_id: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["deployment"]}

    def get_recent_config_changes(self, server_id: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        return {"server_id": server_id, "changes": list(self._read_state()["recent_config_changes"])}

    # ------------------------------------------------------------------
    # 入力検証を行う書き込み操作
    # ------------------------------------------------------------------
    def set_thread_pool_max_threads(self, server_id: str, value: int) -> dict[str, object]:
        self._validate_server_id(server_id)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            raise ValueError("thread_pool max_threads must be an integer in range 1-200")

        with self._lock:
            state = self._read_state_unlocked()
            pool = state["thread_pool"]
            old = int(pool["max_threads"])
            changed = old != value
            pool["max_threads"] = value

            # 処理能力が戻ったときに、滞留中のタスクが解消する動作を再現する。
            if value > old and int(pool["active_threads"]) <= value:
                pool["queue_size"] = 0
                pool["rejected_tasks"] = 0
                state["health"]["request_error_rate"] = 0.0

            if changed:
                self._record_change(
                    state,
                    "thread_pool.max_threads",
                    old,
                    value,
                )
                self._write_state_unlocked(state)

        if changed:
            self.append_log_lines(
                [f"{self._now()} INFO  [org.jboss.as] thread-pool max_threads changed {old} -> {value}"]
            )
        return {
            "server_id": server_id,
            "operation": "set_thread_pool_max_threads",
            "previous_value": old,
            "value": value,
            "changed": changed,
            "success": True,
        }

    def set_datasource_max_pool_size(self, server_id: str, value: int) -> dict[str, object]:
        self._validate_server_id(server_id)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            raise ValueError("datasource max_pool_size must be an integer in range 1-200")

        with self._lock:
            state = self._read_state_unlocked()
            ds = state["datasource"]
            old = int(ds["max_pool_size"])
            changed = old != value
            ds["max_pool_size"] = value
            ds["available_count"] = max(0, value - int(ds["active_count"]))
            if value > old:
                ds["timed_out_requests"] = 0
                state["health"]["request_error_rate"] = 0.0
            if changed:
                self._record_change(state, "datasource.max_pool_size", old, value)
                self._write_state_unlocked(state)

        if changed:
            self.append_log_lines(
                [f"{self._now()} INFO  [org.jboss.as] datasource max_pool_size changed {old} -> {value}"]
            )
        return {
            "server_id": server_id,
            "operation": "set_datasource_max_pool_size",
            "previous_value": old,
            "value": value,
            "changed": changed,
            "success": True,
        }

    def restart_deployment(self, server_id: str, deployment_name: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        deployment_name = deployment_name.strip()
        if not deployment_name:
            raise ValueError("deployment_name is required")

        with self._lock:
            state = self._read_state_unlocked()
            deployment = state["deployment"]
            if deployment_name != deployment["name"]:
                raise ValueError(f"unknown deployment: {deployment_name}")
            was_healthy = deployment["status"] == "OK" and deployment["enabled"] is True
            deployment["status"] = "OK"
            deployment["enabled"] = True
            state["health"]["request_error_rate"] = 0.0
            self._write_state_unlocked(state)

        if not was_healthy:
            self.append_log_lines(
                [f"{self._now()} INFO  [org.jboss.as] deployment {deployment_name} restarted successfully"]
            )
        return {
            "server_id": server_id,
            "operation": "restart_deployment",
            "deployment_name": deployment_name,
            "changed": not was_healthy,
            "success": True,
        }

    def reload_server(self, server_id: str) -> dict[str, object]:
        self._validate_server_id(server_id)
        with self._lock:
            state = self._read_state_unlocked()
            already_healthy = (
                state["health"]["status"] == "UP"
                and float(state["health"]["request_error_rate"]) == 0.0
                and int(state["thread_pool"]["queue_size"]) == 0
                and int(state["thread_pool"]["rejected_tasks"]) == 0
                and int(state["datasource"]["timed_out_requests"]) == 0
            )
            state["health"]["status"] = "UP"
            state["health"]["request_error_rate"] = 0.0
            state["thread_pool"]["queue_size"] = 0
            state["thread_pool"]["rejected_tasks"] = 0
            state["datasource"]["timed_out_requests"] = 0
            if not already_healthy:
                self._write_state_unlocked(state)
        if not already_healthy:
            self.append_log_lines([f"{self._now()} INFO  [org.jboss.as] server reload completed"])
        return {
            "server_id": server_id,
            "operation": "reload_server",
            "changed": not already_healthy,
            "success": True,
        }

    # ------------------------------------------------------------------
    # シミュレーター専用の補助処理。MCP ツールとしては公開しない。
    # ------------------------------------------------------------------
    def simulate_thread_pool_load(
        self,
        *,
        active_threads: int,
        queue_size: int,
        rejected_tasks: int,
        error_rate: float = 0.2,
    ) -> None:
        with self._lock:
            state = self._read_state_unlocked()
            state["thread_pool"]["active_threads"] = active_threads
            state["thread_pool"]["queue_size"] = queue_size
            state["thread_pool"]["rejected_tasks"] = rejected_tasks
            state["health"]["request_error_rate"] = error_rate
            self._write_state_unlocked(state)

    def simulate_datasource_load(
        self,
        *,
        active_count: int,
        timed_out_requests: int,
        error_rate: float = 0.2,
    ) -> None:
        with self._lock:
            state = self._read_state_unlocked()
            max_pool = int(state["datasource"]["max_pool_size"])
            state["datasource"]["active_count"] = active_count
            state["datasource"]["available_count"] = max(0, max_pool - active_count)
            state["datasource"]["timed_out_requests"] = timed_out_requests
            state["health"]["request_error_rate"] = error_rate
            self._write_state_unlocked(state)

    def simulate_deployment_failure(self, deployment_name: str = "app.war") -> None:
        with self._lock:
            state = self._read_state_unlocked()
            if deployment_name != state["deployment"]["name"]:
                raise ValueError(f"unknown deployment: {deployment_name}")
            state["deployment"]["status"] = "FAILED"
            state["deployment"]["enabled"] = False
            state["health"]["request_error_rate"] = 0.4
            self._write_state_unlocked(state)

    # ------------------------------------------------------------------
    # ファイル操作の補助処理
    # ------------------------------------------------------------------
    def ensure_initialized_without_logs_if_needed(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            state = json.loads(json.dumps(_DEFAULT_STATE))
            state["server_id"] = self.server_id
            self._write_state(state)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")

    def _validate_server_id(self, server_id: str) -> None:
        if server_id != self.server_id:
            raise ValueError(f"unknown server_id: {server_id}")

    def _read_state(self) -> dict[str, Any]:
        self.ensure_initialized_without_logs_if_needed()
        with self._lock:
            return self._read_state_unlocked()

    def _read_state_unlocked(self) -> dict[str, Any]:
        self.ensure_initialized_without_logs_if_needed()
        with self.state_path.open("r", encoding="utf-8") as stream:
            return json.load(stream)

    def _write_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._write_state_unlocked(state)

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)

    def _record_change(self, state: dict[str, Any], key: str, old: object, new: object) -> None:
        changes = state["recent_config_changes"]
        changes.append({"timestamp": self._now(), "key": key, "old_value": old, "new_value": new})
        del changes[:-20]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")
