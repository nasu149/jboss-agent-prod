"""監視グラフ：ログ差分取得 → 分類 → 障害登録 → 通知。"""

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
    for candidate in tools:
        if candidate.name == name:
            return candidate
    raise ValueError(f"required MCP tool not found: {name}")



def _start_cycle(state: MonitoringState) -> dict[str, object]:
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
    read_log = _tool(read_tools, "read_server_log")

    async def collect(state: MonitoringState) -> dict[str, object]:
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
    def analyze(state: MonitoringState) -> dict[str, object]:
        raw = classifier.invoke(log_classification_prompt(state.get("log_text", "")))
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
    confidence = float(state.get("confidence", 0.0))
    category = str(state.get("category", "UNKNOWN"))
    severity = "HIGH" if category != "UNKNOWN" and confidence >= 0.85 else "MEDIUM"
    return {
        "incident_id": f"inc-{uuid.uuid4().hex[:10]}",
        "severity": severity,
        "node_trace": [*state.get("node_trace", []), "create_incident"],
    }


def make_notify_node(notifier: Callable[[dict[str, object]], object] | None = None):
    invoke = notifier or send_teams_alert.invoke

    def notify(state: MonitoringState) -> dict[str, object]:
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
    # 次回の監視で同じログを再処理しないよう、読み取り位置を保存する。
    current = int(state.get("current_log_cursor", state.get("previous_log_cursor", 0)))
    return {
        "previous_log_cursor": current,
        "node_trace": [*state.get("node_trace", []), "commit_cursor"],
    }


def _route_after_collect(state: MonitoringState) -> str:
    return "analyze" if state.get("has_new_logs") else "commit"


def _route_after_analysis(state: MonitoringState) -> str:
    return "incident" if state.get("incident_detected") else "commit"


def build_monitoring_graph(
    read_tools: Sequence[Any],
    *,
    checkpointer: Any,
    settings: Settings,
    classifier: LogClassifier | None = None,
    notifier: Callable[[dict[str, object]], object] | None = None,
):
    """Streamlit のスキャンボタンから使う、状態を永続化する監視グラフを構築する。"""
    resolved_classifier = classifier or build_log_classifier(settings)

    graph = StateGraph(MonitoringState)
    graph.add_node("start_cycle", _start_cycle)
    graph.add_node("collect_logs", make_collect_logs_node(read_tools))
    graph.add_node("analyze_logs", make_analyze_logs_node(resolved_classifier))
    graph.add_node("create_incident", _create_incident)
    graph.add_node("notify_teams", make_notify_node(notifier))
    graph.add_node("commit_cursor", _commit_cursor)

    graph.add_edge(START, "start_cycle")
    graph.add_edge("start_cycle", "collect_logs")
    graph.add_conditional_edges("collect_logs", _route_after_collect, {"analyze": "analyze_logs", "commit": "commit_cursor"})
    graph.add_conditional_edges("analyze_logs", _route_after_analysis, {"incident": "create_incident", "commit": "commit_cursor"})
    graph.add_edge("create_incident", "notify_teams")
    graph.add_edge("notify_teams", "commit_cursor")
    graph.add_edge("commit_cursor", END)
    return graph.compile(checkpointer=checkpointer)
