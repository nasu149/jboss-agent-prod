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

# 各シナリオの正常な開始状態。利用時は深いコピーを作り、この定義自体の変更を避ける。
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
    """設定・メトリクス・ログをファイルで模擬する、デモ用の JBoss 操作群。

    読取・書込メソッドは MCP 経由でも使われ、simulate_* はイベント投入専用。
    RLock は同じインスタンス内の排他制御であり、別プロセス間のロックではない。
    """

    def __init__(self, data_dir: str | Path, *, server_id: str = "jboss-01") -> None:
        """対象サーバー ID、ログと状態の保存先、インスタンス内の再入可能ロックを用意する。"""
        self.data_dir = Path(data_dir)
        self.server_id = server_id
        self.log_path = self.data_dir / "server.log"
        self.state_path = self.data_dir / "state.json"
        self._lock = RLock()

    def ensure_initialized(self) -> None:
        """未作成の状態ファイルとログを初期化し、新規ログには起動メッセージを追加する。

        既存ファイルは維持するため、アプリの再描画時にも呼び出せる。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            state = json.loads(json.dumps(_DEFAULT_STATE))
            state["server_id"] = self.server_id
            self._write_state(state)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")
            self.append_log_lines(_DEFAULT_BOOT_LOGS)

    def reset(self, *, include_boot_logs: bool = True) -> None:
        """設定・メトリクスを初期値に戻してログを空にする、デモ・テスト用の完全リセット。

        include_boot_logs が True なら起動ログも書く。既存のログカーソルは無効になり得る。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        state = json.loads(json.dumps(_DEFAULT_STATE))
        state["server_id"] = self.server_id
        self._write_state(state)
        self.log_path.write_text("", encoding="utf-8")
        if include_boot_logs:
            self.append_log_lines(_DEFAULT_BOOT_LOGS)

    def restore_baseline_state(self) -> None:
        """ログを残したまま、設定・メトリクス・設定変更履歴を初期状態に戻す。

        シナリオごとの開始条件を揃えつつ、追記ログを読む監視カーソルを維持する。
        """
        self.ensure_initialized()
        state = json.loads(json.dumps(_DEFAULT_STATE))
        state["server_id"] = self.server_id
        self._write_state(state)

    def append_log_lines(self, lines: list[str]) -> dict[str, int]:
        """UTF-8 のログ行を改行付きで追記し、追記前後のバイト位置を返す。

        from_cursor と to_cursor は行数ではなくファイル先頭からのバイト数。
        空リストでは追記せず、両方に現在の末尾位置を返す。
        """
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
        """cursor のバイト位置から末尾までのログ行と、次回の読取位置を返す。

        cursor には前回の to_cursor を渡す。負の位置、現在の末尾を超える位置、
        未知のサーバー ID は ValueError とする。ログ自体は変更しない。
        """
        self._validate_server_id(server_id)
        self.ensure_initialized()
        if cursor < 0:
            raise ValueError("cursor must be >= 0")

        file_size = self.log_path.stat().st_size
        if cursor > file_size:
            raise ValueError(
                f"cursor {cursor} is beyond current log size {file_size}; the log may have been reset"
            )

        # カーソルを UTF-8 の文字数ではなくバイト位置として扱うため、バイナリで読む。
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
        """対象サーバーの稼働状態、CPU・ヒープ使用率、リクエストエラー率を返す。"""
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["health"]}

    def get_thread_pool_status(self, server_id: str) -> dict[str, object]:
        """スレッド数の上限・使用数・待ち行列・拒否数を、サーバー ID とともに返す。"""
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["thread_pool"]}

    def get_datasource_status(self, server_id: str) -> dict[str, object]:
        """接続プールの上限・使用数・空き数・タイムアウト数を返す。"""
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["datasource"]}

    def get_deployment_status(self, server_id: str) -> dict[str, object]:
        """模擬アプリケーションの名前、状態、有効・無効を返す。"""
        self._validate_server_id(server_id)
        return {"server_id": server_id, **self._read_state()["deployment"]}

    def get_recent_config_changes(self, server_id: str) -> dict[str, object]:
        """保持している直近の設定変更履歴を返す。各記録には日時と変更前後の値を含む。"""
        self._validate_server_id(server_id)
        return {"server_id": server_id, "changes": list(self._read_state()["recent_config_changes"])}

    # ------------------------------------------------------------------
    # 入力検証を行う書き込み操作
    # ------------------------------------------------------------------
    def set_thread_pool_max_threads(self, server_id: str, value: int) -> dict[str, object]:
        """最大スレッド数を 1〜200 の整数に変更し、変更前後の値と成否を返す。

        bool は整数として受け付けない。上限を増やし使用数を収容できれば、待ち行列・
        拒否数・エラー率を解消する。値が変わった場合だけ状態と変更履歴・ログを保存する。
        """
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
        """最大接続数を 1〜200 の整数に変更し、空き接続数を再計算する。

        bool は受け付けない。増枠時はタイムアウト数とエラー率を解消する。値が変わった
        場合だけ状態と変更履歴・ログを保存し、戻り値に変更前後の値と changed を含める。
        """
        self._validate_server_id(server_id)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            raise ValueError("datasource max_pool_size must be an integer in range 1-200")

        with self._lock:
            state = self._read_state_unlocked()
            ds = state["datasource"]
            old = int(ds["max_pool_size"])
            changed = old != value
            ds["max_pool_size"] = value
            # 使用数が新しい上限を超えていても空き数は負にせず、ゼロとして表す。
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
        """指定アプリケーションを有効・正常状態に戻し、エラー率をゼロにする。

        空または未知の名前は ValueError。既に正常な場合は changed=False を返し、
        再起動ログは追加しない。実際のプロセス再起動は行わない。
        """
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
        """再読み込みを模擬し、サーバー状態・エラー率・滞留やタイムアウトを正常化する。

        プールの上限値やデプロイ状態は変更しない。判定対象の指標が既に正常なら
        ファイルを書き換えず changed=False を返す。
        """
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
        """イベント投入用にスレッド使用数・滞留・拒否・エラー率を直接設定する。"""
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
        """イベント投入用に接続使用数・タイムアウト・エラー率を設定し、空き数を再計算する。"""
        with self._lock:
            state = self._read_state_unlocked()
            max_pool = int(state["datasource"]["max_pool_size"])
            state["datasource"]["active_count"] = active_count
            state["datasource"]["available_count"] = max(0, max_pool - active_count)
            state["datasource"]["timed_out_requests"] = timed_out_requests
            state["health"]["request_error_rate"] = error_rate
            self._write_state_unlocked(state)

    def simulate_deployment_failure(self, deployment_name: str = "app.war") -> None:
        """指定アプリを失敗・無効状態にし、エラー率を 40% にする。未知の名前は拒否する。"""
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
        """不足する状態ファイルと空ログだけを作り、起動ログの追記は行わない。

        ログ追記処理から呼んでも、初期化が再びログ追記を呼ぶ循環を避けられる。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            state = json.loads(json.dumps(_DEFAULT_STATE))
            state["server_id"] = self.server_id
            self._write_state(state)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")

    def _validate_server_id(self, server_id: str) -> None:
        """このインスタンスが扱うサーバー ID かを確認し、不一致なら ValueError を送出する。"""
        if server_id != self.server_id:
            raise ValueError(f"unknown server_id: {server_id}")

    def _read_state(self) -> dict[str, Any]:
        """必要なファイルを用意し、ロックを取得して現在の状態を JSON から読み出す。"""
        self.ensure_initialized_without_logs_if_needed()
        with self._lock:
            return self._read_state_unlocked()

    def _read_state_unlocked(self) -> dict[str, Any]:
        """自身ではロックを取得せず状態を読み出す。排他が必要な呼び出し元でロックを保持する。"""
        self.ensure_initialized_without_logs_if_needed()
        with self.state_path.open("r", encoding="utf-8") as stream:
            return json.load(stream)

    def _write_state(self, state: dict[str, Any]) -> None:
        """保存先を用意し、ロックを取得して状態全体を JSON ファイルへ書き込む。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._write_state_unlocked(state)

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        """自身ではロックを取得せず、状態ファイルを指定された辞書の内容で上書きする。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)

    def _record_change(self, state: dict[str, Any], key: str, old: object, new: object) -> None:
        """渡された状態の設定変更履歴に1件追加し、直近 20 件だけ残す。ファイル保存は行わない。"""
        changes = state["recent_config_changes"]
        changes.append({"timestamp": self._now(), "key": key, "old_value": old, "new_value": new})
        # 履歴の肥大化を防ぎつつ、直近の設定変更を診断の手掛かりとして残す。
        del changes[:-20]

    @staticmethod
    def _now() -> str:
        """ログや設定変更履歴に使う現在の UTC 日時を、秒精度の ISO 8601 文字列で返す。"""
        return datetime.now(UTC).isoformat(timespec="seconds")
