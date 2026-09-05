"""保存済みカーソル以降のログを読み、必要な場合だけ分類・障害 ID 発行・通知を行う。

各ノードは MonitoringState の更新部分を返す。監視サイクルの最後にカーソルを
確定し、同じ thread_id の次回実行へ引き継ぐ。障害の DB 登録はサービス側が行う。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Callable, Mapping

from langgraph.graph import END, START, StateGraph

from jboss_agent.config import Settings
from jboss_agent.graphs.prompts import log_classification_prompt
from jboss_agent.graphs.state import MonitoringState
from jboss_agent.llm import LogClassifier, build_log_classifier
from jboss_agent.models import LogClassification
from jboss_agent.teams import send_teams_alert
from jboss_agent.tool_results import normalize_tool_result


def _tool(tools: Sequence[Any], name: str) -> Any:
    """名前が一致する MCP ツールを探して返す。必須ツールがなければ ValueError とする。"""
    for candidate in tools:
        if candidate.name == name:
            return candidate
    raise ValueError(f"required MCP tool not found: {name}")



def _start_cycle(state: MonitoringState) -> dict[str, object]:
    """前回の分類・通知・追跡結果を初期化し、新しい監視サイクルを始める。

    サーバー ID と保存済みログカーソルは返り値に含めず、そのまま引き継ぐ。
    """
    return {
        "new_log_lines": [],
        "has_new_logs": False,
        "incident_detected": False,
        "category": "NORMAL",
        "confidence": 0.0,
        "summary": "No new logs",
        "evidence": [],
        "incident_id": None,
        "severity": "LOW",
        "teams_notified": False,
        "teams_tool_status": None,
        "node_trace": ["start_cycle"],
    }


def make_collect_logs_node(read_tools: Sequence[Any]):
    """ログ読取ツールを捕捉し、ログ差分を取得する非同期ノードを返す。"""
    read_log = _tool(read_tools, "read_server_log")

    async def collect(state: MonitoringState) -> dict[str, object]:
        """前回のバイト位置からログを読み、差分と今回の開始・終了位置を返す。

        ログ縮小でカーソルが末尾を超えたエラーに限り、先頭から1回読み直す。
        それ以外のツール例外は呼び出し元へ送出する。
        """
        previous = int(state.get("previous_log_cursor", 0))
        cursor_reset = False
        try:
            raw = await read_log.ainvoke({"server_id": state["server_id"], "cursor": previous})
        except Exception as exc:
            # ログ初期化でカーソルが末尾を超えた場合は、ローテーションと同様に扱う。
            if previous <= 0 or "beyond current log size" not in str(exc).lower():
                raise
            previous = 0
            cursor_reset = True
            raw = await read_log.ainvoke({"server_id": state["server_id"], "cursor": 0})

        result = normalize_tool_result(raw)
        lines = [str(line) for line in result.get("lines", [])]
        current = int(result.get("to_cursor", previous))
        return {
            "scan_from_cursor": previous,
            "cursor_reset_detected": cursor_reset,
            "current_log_cursor": current,
            "new_log_lines": lines,
            "log_text": "\n".join(lines),
            "has_new_logs": bool(lines),
            "node_trace": [*state.get("node_trace", []), "collect_logs"],
        }

    return collect


def make_analyze_logs_node(classifier: LogClassifier):
    """指定された分類器を使い、ログ差分の分類結果を状態へ反映するノードを返す。"""

    def analyze(state: MonitoringState) -> dict[str, object]:
        """ログを分類し、スキーマで検証した検知結果・分類・確信度・根拠を返す。

        分類器は LogClassification または辞書形式を返す必要があり、それ以外は拒否する。
        """
        raw = classifier.invoke(log_classification_prompt(state.get("log_text", "")))
        # 本番モデルとテスト用の辞書応答を、同じ検証済みの形式に揃える。
        if isinstance(raw, LogClassification):
            result = raw
        elif isinstance(raw, Mapping):
            result = LogClassification.model_validate(dict(raw))
        else:
            raise TypeError(f"classifier returned unsupported type: {type(raw).__name__}")
        return {
            "incident_detected": result.incident_detected,
            "category": result.category,
            "confidence": result.confidence,
            "summary": result.summary,
            "evidence": result.evidence,
            "node_trace": [*state.get("node_trace", []), "analyze_logs"],
        }

    return analyze


def _create_incident(state: MonitoringState) -> dict[str, object]:
    """新しい障害 ID と重要度を状態に設定する。ここでは DB への登録は行わない。

    分類が UNKNOWN 以外で確信度が 0.85 以上なら HIGH、それ以外は MEDIUM とする。
    """
    confidence = float(state.get("confidence", 0.0))
    category = str(state.get("category", "UNKNOWN"))
    severity = "HIGH" if category != "UNKNOWN" and confidence >= 0.85 else "MEDIUM"
    return {
        "incident_id": f"inc-{uuid.uuid4().hex[:10]}",
        "severity": severity,
        "node_trace": [*state.get("node_trace", []), "create_incident"],
    }


def make_notify_node(notifier: Callable[[dict[str, object]], object] | None = None):
    """通知処理を捕捉したノードを返す。省略時は Teams 通知ツールを使う。"""
    invoke = notifier or send_teams_alert.invoke

    def notify(state: MonitoringState) -> dict[str, object]:
        """障害の概要を通知し、ツールが返した成功フラグとステータスを状態に記録する。"""
        raw = invoke(
            {
                "server_id": state["server_id"],
                "incident_id": state["incident_id"],
                "severity": state["severity"],
                "category": state["category"],
                "confidence": state["confidence"],
                "summary": state["summary"],
            }
        )
        result = normalize_tool_result(raw)
        return {
            "teams_notified": bool(result.get("success")),
            "teams_tool_status": str(result.get("status", "unknown")),
            "node_trace": [*state.get("node_trace", []), "notify_teams"],
        }

    return notify


def _commit_cursor(state: MonitoringState) -> dict[str, object]:
    """今回のログ末尾位置を、次回スキャンで使う previous_log_cursor に移す。"""
    # 次回の監視で同じログを再処理しないよう、読み取り位置を保存する。
    current = int(state.get("current_log_cursor", state.get("previous_log_cursor", 0)))
    return {
        "previous_log_cursor": current,
        "node_trace": [*state.get("node_trace", []), "commit_cursor"],
    }


def _route_after_collect(state: MonitoringState) -> str:
    """ログ差分があれば分類へ、なければ LLM を呼ばずカーソル確定へ分岐する。"""
    return "analyze" if state.get("has_new_logs") else "commit"


def _route_after_analysis(state: MonitoringState) -> str:
    """障害を検知した場合は ID 発行へ、それ以外はカーソル確定へ分岐する。"""
    return "incident" if state.get("incident_detected") else "commit"


def build_monitoring_graph(
    read_tools: Sequence[Any],
    *,
    checkpointer: Any,
    settings: Settings,
    classifier: LogClassifier | None = None,
    notifier: Callable[[dict[str, object]], object] | None = None,
):
    """ログ差分取得から通知・カーソル確定までを結び、コンパイル済みグラフを返す。

    checkpointer が同じ thread_id の状態を保存・復元する。classifier と notifier は
    テストなどで差し替え可能で、省略時は設定に基づく分類器と Teams 通知を使う。
    """
    resolved_classifier = classifier or build_log_classifier(settings)

    graph = StateGraph(MonitoringState)
    graph.add_node("start_cycle", _start_cycle)
    graph.add_node("collect_logs", make_collect_logs_node(read_tools))
    graph.add_node("analyze_logs", make_analyze_logs_node(resolved_classifier))
    graph.add_node("create_incident", _create_incident)
    graph.add_node("notify_teams", make_notify_node(notifier))
    graph.add_node("commit_cursor", _commit_cursor)

    # 差分なし・正常・障害通知後のいずれも、最後にカーソル確定へ合流する。
    graph.add_edge(START, "start_cycle")
    graph.add_edge("start_cycle", "collect_logs")
    graph.add_conditional_edges("collect_logs", _route_after_collect, {"analyze": "analyze_logs", "commit": "commit_cursor"})
    graph.add_conditional_edges("analyze_logs", _route_after_analysis, {"incident": "create_incident", "commit": "commit_cursor"})
    graph.add_edge("create_incident", "notify_teams")
    graph.add_edge("notify_teams", "commit_cursor")
    graph.add_edge("commit_cursor", END)
    return graph.compile(checkpointer=checkpointer)
