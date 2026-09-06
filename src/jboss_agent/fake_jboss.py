"""MCP の接続先として使う、非常に小さな Fake JBoss。

UI プロセスと MCP サーバープロセスは別プロセスなので、状態はメモリではなく
``state.json`` と ``server.log`` に保存する。ここで再現するのは学習に必要な
3 種類の障害だけであり、実際の JBoss の完全なシミュレーターではない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

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

_BASELINE: dict[str, Any] = {
    "thread_pool": {"max_threads": 80, "active_threads": 12, "queue_size": 0},
    "datasource": {"max_pool_size": 30, "active_count": 8, "timed_out_requests": 0},
    "deployment": {"name": "app.war", "status": "OK"},
}

_LOGS: dict[Scenario, list[str]] = {
    "THREAD_POOL_CONFIGURATION": [
        "WARN HTTP worker queue is growing",
        "ERROR task rejected from worker executor",
        "WARN HTTP 503 responses increased",
    ],
    "DATASOURCE_POOL_EXHAUSTION": [
        "WARN ExampleDS has no available connection",
        "ERROR timed out waiting for datasource connection",
        "WARN database-backed requests returning 503",
    ],
    "DEPLOYMENT_FAILURE": [
        "ERROR deployment app.war failed to start",
        "ERROR application endpoint unavailable",
        "WARN readiness check returned 503",
    ],
    "NORMAL_ACTIVITY": [
        "INFO request completed status=200 elapsed=42ms",
        "INFO scheduled cleanup completed",
        "INFO readiness check returned 200",
    ],
}


class FakeJBoss:
    """ファイル共有だけで read/write MCP Tool の動作を模擬する。"""

    def __init__(self, data_dir: str | Path, server_id: str = "jboss-01") -> None:
        self.data_dir = Path(data_dir)
        self.server_id = server_id
        self.state_path = self.data_dir / "state.json"
        self.log_path = self.data_dir / "server.log"

    def reset(self) -> None:
        """サーバー状態とログを正常な初期状態へ戻す。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._write_state(json.loads(json.dumps(_BASELINE)))
        self.log_path.write_text("INFO JBoss EAP started\n", encoding="utf-8")

    def ensure_initialized(self) -> None:
        """初回起動時だけ baseline を作り、既存のデモ状態は維持する。"""
        if not self.state_path.exists() or not self.log_path.exists():
            self.reset()

    def inject(self, scenario: Scenario) -> None:
        """1 回の学習デモ用に baseline を作り直し、選択した疑似障害を投入する。

        シナリオ名そのものは state/log に保存しない。Agent はログと MCP read Tool の
        観測結果だけを見て分類する。
        """
        if scenario not in SCENARIOS:
            raise ValueError(f"unsupported scenario: {scenario}")

        self.reset()
        state = self._read_state()
        if scenario == "THREAD_POOL_CONFIGURATION":
            state["thread_pool"] = {"max_threads": 20, "active_threads": 20, "queue_size": 37}
        elif scenario == "DATASOURCE_POOL_EXHAUSTION":
            state["datasource"] = {"max_pool_size": 5, "active_count": 5, "timed_out_requests": 14}
        elif scenario == "DEPLOYMENT_FAILURE":
            state["deployment"] = {"name": "app.war", "status": "FAILED"}

        self._write_state(state)
        self.log_path.write_text("\n".join(_LOGS[scenario]) + "\n", encoding="utf-8")

    def read_server_log(self, server_id: str) -> dict[str, object]:
        """現在の ``server.log`` 全体を返す。1 回だけ動かすため cursor は持たない。"""
        self._validate_server(server_id)
        self.ensure_initialized()
        return {"server_id": server_id, "lines": self.log_path.read_text(encoding="utf-8").splitlines()}

    def get_thread_pool_status(self, server_id: str) -> dict[str, object]:
        """worker thread pool の現在値を返す。"""
        self._validate_server(server_id)
        return {"server_id": server_id, **self._read_state()["thread_pool"]}

    def get_datasource_status(self, server_id: str) -> dict[str, object]:
        """datasource pool の現在値を返す。"""
        self._validate_server(server_id)
        return {"server_id": server_id, **self._read_state()["datasource"]}

    def get_deployment_status(self, server_id: str) -> dict[str, object]:
        """``app.war`` の現在状態を返す。"""
        self._validate_server(server_id)
        return {"server_id": server_id, **self._read_state()["deployment"]}

    def set_thread_pool_max_threads(self, server_id: str, value: int) -> dict[str, object]:
        """thread pool 上限を変更する。デモでは 80 に戻すと queue も解消する。"""
        self._validate_server(server_id)
        if not 1 <= value <= 200:
            raise ValueError("value must be between 1 and 200")
        state = self._read_state()
        state["thread_pool"]["max_threads"] = value
        if value >= 80:
            state["thread_pool"]["queue_size"] = 0
        self._write_state(state)
        return {"success": True, "tool": "set_thread_pool_max_threads", "value": value}

    def set_datasource_max_pool_size(self, server_id: str, value: int) -> dict[str, object]:
        """datasource pool 上限を変更する。デモでは 30 に戻すと timeout も解消する。"""
        self._validate_server(server_id)
        if not 1 <= value <= 200:
            raise ValueError("value must be between 1 and 200")
        state = self._read_state()
        state["datasource"]["max_pool_size"] = value
        if value >= 30:
            state["datasource"]["timed_out_requests"] = 0
        self._write_state(state)
        return {"success": True, "tool": "set_datasource_max_pool_size", "value": value}

    def restart_deployment(self, server_id: str, deployment_name: str) -> dict[str, object]:
        """指定 deployment を正常状態へ戻す。"""
        self._validate_server(server_id)
        state = self._read_state()
        if deployment_name != state["deployment"]["name"]:
            raise ValueError(f"unknown deployment: {deployment_name}")
        state["deployment"]["status"] = "OK"
        self._write_state(state)
        return {"success": True, "tool": "restart_deployment", "deployment_name": deployment_name}

    def _validate_server(self, server_id: str) -> None:
        if server_id != self.server_id:
            raise ValueError(f"unknown server_id: {server_id}")

    def _read_state(self) -> dict[str, Any]:
        self.ensure_initialized()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
