"""監視・障害対応の LangGraph と、画面用の永続データをつなぐサービス。

グラフのチェックポイントは実行の継続・承認後の再開に使い、RuntimeStore は
画面に表示する監視状態・障害一覧・活動履歴を保存する。正解データは障害 ID の
関連付けにのみ使用し、診断の入力には含めない。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import Command

from jboss_agent.config import Settings
from jboss_agent.graphs.incident import build_incident_graph
from jboss_agent.graphs.monitoring import build_monitoring_graph
from jboss_agent.mcp.client import READ_TOOL_NAMES, load_jboss_tools
from jboss_agent.persistence import open_checkpointer
from jboss_agent.runtime_store import RuntimeStore
from jboss_agent.simulator import GroundTruthStore


class AgentService:
    """監視の開始、障害対応の開始・再開、および UI 向けの結果保存をまとめる。"""

    def __init__(self, settings: Settings, runtime: RuntimeStore, truth: GroundTruthStore) -> None:
        """設定、画面用ストア、答え合わせ用ストアを受け取って保持する。

        ここではグラフや MCP 接続を作らず、操作の実行時に用意する。
        """
        self.settings = settings
        self.runtime = runtime
        self.truth = truth

    async def run_scan(self) -> dict[str, Any]:
        """監視を1回実行し、障害が見つかれば調査・対応グラフへ進む。

        監視は固定の thread_id でログの読取位置を引き継ぎ、障害対応は障害ごとの
        thread_id で保存する。戻り値は monitoring と incident の状態で、障害が
        なければ incident は None。承認待ちによる中断も保存して正常に返す。
        例外時は監視エラーを記録したうえで再送出し、呼び出し元に表示を任せる。
        """
        server_id = self.settings.server_id
        self.runtime.begin_scan(server_id)

        try:
            read_tools, write_tools = await load_jboss_tools()
            async with open_checkpointer(self.settings) as checkpointer:
                monitoring_graph = build_monitoring_graph(
                    read_tools,
                    checkpointer=checkpointer,
                    settings=self.settings,
                )
                # 監視用の固定スレッドを再利用し、前回確定したログカーソルを復元する。
                monitoring = await monitoring_graph.ainvoke(
                    {"server_id": server_id},
                    config={"configurable": {"thread_id": self.settings.monitoring_thread_id}},
                )

                incident_id = monitoring.get("incident_id")
                # 監視部分の完了を先に記録する。障害対応の完了・承認待ちは別途保存する。
                self.runtime.complete_scan(
                    server_id,
                    previous_cursor=int(monitoring.get("scan_from_cursor", 0)),
                    current_cursor=int(monitoring.get("current_log_cursor", 0)),
                    incident_id=str(incident_id) if incident_id else None,
                )
                self._record_scan(monitoring)

                if not incident_id:
                    return {"monitoring": monitoring, "incident": None}

                incident_id = str(incident_id)
                # 障害ごとに実行状態を分離し、他の障害の承認再開と混ざらないようにする。
                thread_id = f"incident:{incident_id}"
                self.runtime.upsert_incident(
                    incident_id=incident_id,
                    thread_id=thread_id,
                    server_id=server_id,
                    category=str(monitoring.get("category", "UNKNOWN")),
                    severity=str(monitoring.get("severity", "MEDIUM")),
                    confidence=float(monitoring.get("confidence", 0.0)),
                    summary=str(monitoring.get("summary", "Incident detected")),
                    status="INVESTIGATING",
                )
                # 正解情報は ID でのみ関連付け、Agent の状態には渡さない。
                self.truth.link_latest_unlinked(server_id, incident_id)
                self.runtime.add_activity(
                    server_id,
                    "incident",
                    f"Incident {incident_id} created; starting read-only investigation",
                    incident_id=incident_id,
                )

                incident_graph = build_incident_graph(
                    read_tools,
                    write_tools,
                    checkpointer=checkpointer,
                    settings=self.settings,
                )
                incident = await incident_graph.ainvoke(
                    {
                        "incident_id": incident_id,
                        "server_id": server_id,
                        "category": str(monitoring.get("category", "UNKNOWN")),
                        "severity": str(monitoring.get("severity", "MEDIUM")),
                        "confidence": float(monitoring.get("confidence", 0.0)),
                        "initial_log_lines": list(monitoring.get("new_log_lines", [])),
                        "messages": [],
                        "evidence": [],
                        "investigation_count": 0,
                        "recovery_attempts": 0,
                        "node_trace": [],
                    },
                    config={"configurable": {"thread_id": thread_id}},
                )
                self._persist_incident(monitoring, incident, thread_id)
                return {"monitoring": monitoring, "incident": incident}
        except Exception as exc:
            self.runtime.fail_scan(server_id, str(exc))
            raise

    async def resume_incident(
        self,
        incident_id: str,
        *,
        decision: str,
        proposed_value: int | None = None,
    ) -> dict[str, Any]:
        """保存された障害を、承認・拒否・値を編集した承認の判断で再開する。

        incident_id に対応するチェックポイントへ decision と任意の proposed_value を
        渡し、結果を画面用ストアにも保存して返す。未知の障害 ID は ValueError とする。
        編集値の安全性検証と書き込み可否の判断は、再開先のグラフが担当する。
        """
        record = self.runtime.get_incident(incident_id)
        if record is None:
            raise ValueError(f"unknown incident_id: {incident_id}")

        payload: dict[str, Any] = {"decision": decision}
        if proposed_value is not None:
            payload["proposed_value"] = proposed_value

        read_tools, write_tools = await load_jboss_tools()
        async with open_checkpointer(self.settings) as checkpointer:
            graph = build_incident_graph(
                read_tools,
                write_tools,
                checkpointer=checkpointer,
                settings=self.settings,
            )
            # 同じ thread_id の保存状態を復元し、承認待ちの interrupt に判断結果を渡す。
            result = await graph.ainvoke(
                Command(resume=payload),
                config={"configurable": {"thread_id": record.thread_id}},
            )

        # 再開時は監視をやり直さず、障害レコードの基本情報を保存処理へ渡す。
        monitoring_stub = {
            "incident_id": record.incident_id,
            "server_id": record.server_id,
            "category": record.category,
            "severity": record.severity,
            "confidence": record.confidence,
            "summary": record.summary,
        }
        self._persist_incident(monitoring_stub, result, record.thread_id)
        return result

    def _record_scan(self, state: dict[str, Any]) -> None:
        """ログ差分の有無と障害検知結果を、監視の活動履歴として1件保存する。"""
        lines = len(state.get("new_log_lines", []))
        if not state.get("has_new_logs"):
            message = (
                f"No new server.log lines (cursor {state.get('scan_from_cursor', 0)} "
                f"-> {state.get('current_log_cursor', 0)}); Gemini skipped"
            )
        elif state.get("incident_detected"):
            message = (
                f"{lines} new log lines analyzed "
                f"(cursor {state.get('scan_from_cursor', 0)} -> {state.get('current_log_cursor', 0)}); "
                f"incident suspected: "
                f"{state.get('category')} ({float(state.get('confidence', 0.0)):.0%})"
            )
        else:
            message = (
                f"{lines} new log lines analyzed "
                f"(cursor {state.get('scan_from_cursor', 0)} -> {state.get('current_log_cursor', 0)}); "
                "no incident detected"
            )
        self.runtime.add_activity(self.settings.server_id, "monitoring", message)

    def _persist_incident(self, monitoring: dict[str, Any], result: dict[str, Any], thread_id: str) -> None:
        """グラフの状態を画面用の障害レコードと活動履歴に変換して保存する。

        中断、拒否、安全ルールによる中止、復旧結果の順に表示ステータスを決める。
        monitoring は障害の基本情報、result はグラフの実行結果、thread_id は
        承認後に同じ実行を再開するための識別子。
        """
        incident_id = str(monitoring["incident_id"])
        pending = _interrupt_payload(result)
        proposed_action = result.get("proposed_action")
        diagnosis = result.get("diagnosis")
        recovered = result.get("recovered")
        approval = result.get("approval_status")
        failure_reason = result.get("failure_reason")

        # 承認待ちを最優先にし、途中の復旧結果から完了扱いにならないようにする。
        if pending is not None:
            status, activity = "PENDING_APPROVAL", "調査を一時停止し、復旧操作の承認を待っています"
        elif approval == "REJECTED":
            status, activity = "REJECTED", "復旧操作が拒否されました。書き込みは実行していません"
        elif approval == "BLOCKED":
            status, activity = "BLOCKED", "安全ルールにより復旧操作を中止しました"
        elif recovered is True:
            no_action = (proposed_action or {}).get("type") == "NONE"
            status = "RESOLVED_NO_ACTION" if no_action else "RECOVERED"
            activity = "障害対応が正常に完了しました"
        elif recovered is False:
            status, activity = "FAILED_SAFE", "復旧できなかったため処理を停止しました。運用担当者の対応が必要です"
        else:
            status, activity = "COMPLETED", "障害対応が完了しました"

        read_names = _read_tool_names(result.get("messages", []))
        self.runtime.upsert_incident(
            incident_id=incident_id,
            thread_id=thread_id,
            server_id=str(monitoring["server_id"]),
            category=str(monitoring.get("category", "UNKNOWN")),
            severity=str(monitoring.get("severity", "MEDIUM")),
            confidence=float(monitoring.get("confidence", 0.0)),
            summary=str(monitoring.get("summary", "Incident detected")),
            status=status,
            pending_approval=pending,
            diagnosis=diagnosis if isinstance(diagnosis, dict) else None,
            proposed_action=proposed_action if isinstance(proposed_action, dict) else None,
            recovered=recovered if isinstance(recovered, bool) else None,
            failure_reason=str(failure_reason) if failure_reason else None,
            investigation_tool_calls=len(read_names),
        )

        server_id = str(monitoring["server_id"])
        if read_names:
            self.runtime.add_activity(server_id, "tool", "Read MCP tools: " + ", ".join(read_names), incident_id=incident_id)
        if isinstance(diagnosis, dict):
            self.runtime.add_activity(server_id, "diagnosis", f"Diagnosis: {diagnosis.get('root_cause', 'UNKNOWN')}", incident_id=incident_id)
        execution = result.get("execution_result")
        if isinstance(execution, dict) and execution.get("tool_name"):
            self.runtime.add_activity(server_id, "write_tool", f"承認された MCP 書き込みツールを実行しました: {execution['tool_name']}", incident_id=incident_id)
        self.runtime.add_activity(server_id, "incident", activity, incident_id=incident_id, details={"status": status})


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """最初の LangGraph 中断から承認用データを取り出し、辞書として返す。

    中断がなければ None。value 属性を持つ中断オブジェクトにも、生の値にも対応する。
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return dict(value) if isinstance(value, dict) else {"value": value}


def _read_tool_names(messages: list[Any]) -> list[str]:
    """AIMessage が要求した読み取りツール名を、重複を残して呼び出し順に返す。

    実行結果ではなく要求メッセージを数えるため、成功した呼び出しの一覧ではない。
    """
    names: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            names.extend(
                str(call.get("name"))
                for call in message.tool_calls
                if str(call.get("name")) in READ_TOOL_NAMES
            )
    return names
