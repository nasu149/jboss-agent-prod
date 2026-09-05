"""障害の調査・診断・安全性検証・人の承認・復旧操作・結果確認をつなぐグラフ。

調査用 LLM は読み取りツールだけを使い、書き込みツールと引数は承認後に Python が
決める。復旧できなければ回数上限まで再調査し、新しい提案にも再び承認を求める。
各ノードの戻り値は IncidentState の更新部分で、メッセージは専用 reducer が統合する。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from jboss_agent.config import Settings
from jboss_agent.graphs.prompts import diagnosis_prompt, initial_investigation_messages
from jboss_agent.graphs.state import IncidentState
from jboss_agent.llm import build_diagnoser, build_investigator
from jboss_agent.models import ApprovalResponse, IncidentDiagnosis
from jboss_agent.policy import evaluate_action
from jboss_agent.tool_results import normalize_tool_result


def _prepare_investigation(state: IncidentState) -> dict[str, object]:
    """初回の調査メッセージとカウンターを用意する。会話があれば作り直さず維持する。"""
    if state.get("messages"):
        return {"node_trace": [*state.get("node_trace", []), "prepare_investigation"]}
    return {
        "messages": initial_investigation_messages(state),
        "evidence": state.get("evidence", []),
        "investigation_count": state.get("investigation_count", 0),
        "recovery_attempts": state.get("recovery_attempts", 0),
        "node_trace": [*state.get("node_trace", []), "prepare_investigation"],
    }


def make_investigate_node(model: Any):
    """調査用モデルを捕捉し、次の読み取り要求または調査の結論を生成するノードを返す。"""

    def investigate(state: IncidentState) -> dict[str, object]:
        """現在の会話をモデルに渡し、AIMessage を追記して調査ラウンド数を1増やす。

        ここではツールを実行せず、モデルが返した呼び出し要求を次の分岐で処理する。
        """
        response = model.invoke(state["messages"])
        if not isinstance(response, AIMessage):
            raise TypeError(f"investigator must return AIMessage, got {type(response).__name__}")
        return {
            "messages": [response],
            "investigation_count": state.get("investigation_count", 0) + 1,
            "node_trace": [*state.get("node_trace", []), "investigate"],
        }

    return investigate


def _route_after_investigate(state: IncidentState) -> str:
    """最後の AI 応答にツール要求があれば読み取りへ、なければ診断へ進む。"""
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    return "read_tools" if isinstance(last, AIMessage) and last.tool_calls else "diagnose"


def _record_tool_evidence(state: IncidentState) -> dict[str, object]:
    """会話末尾に連続するツール応答だけを取り出し、診断用の根拠一覧へ追加する。

    直前のツール実行分だけを対象にし、過去の会話全体を再収集しない。
    """
    recent: list[ToolMessage] = []
    # 末尾から直前のツール応答群だけを集め、AI 応答に達したところで止める。
    for message in reversed(state.get("messages", [])):
        if isinstance(message, ToolMessage):
            recent.append(message)
        else:
            break
    recent.reverse()

    evidence = [*state.get("evidence", [])]
    for message in recent:
        evidence.append(
            {
                "tool_name": message.name or "unknown_tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        )
    return {"evidence": evidence, "node_trace": [*state.get("node_trace", []), "record_evidence"]}


def make_round_route(max_rounds: int):
    """読み取り後に調査を続けるかを、モデル呼び出し回数の上限で決める分岐関数を返す。"""

    def route(state: IncidentState) -> str:
        """調査ラウンド数が max_rounds に達したら、追加調査を止めて診断へ進む。"""
        return "diagnose" if state.get("investigation_count", 0) >= max_rounds else "investigate"

    return route


def make_diagnose_node(model: Any):
    """構造化診断モデルを捕捉し、原因と対処案を状態へ設定するノードを返す。"""

    def diagnose(state: IncidentState) -> dict[str, object]:
        """初期ログとツール根拠から診断を生成し、スキーマで検証した辞書を返す。

        推奨操作は後続の安全性検証で使えるよう、診断全体とは別のキーにも格納する。
        """
        raw = model.invoke(diagnosis_prompt(state))
        if isinstance(raw, IncidentDiagnosis):
            result = raw
        elif isinstance(raw, Mapping):
            result = IncidentDiagnosis.model_validate(dict(raw))
        else:
            raise TypeError(f"diagnoser returned unsupported type: {type(raw).__name__}")
        return {
            "diagnosis": result.model_dump(),
            "proposed_action": result.recommended_action.model_dump(),
            "node_trace": [*state.get("node_trace", []), "diagnose"],
        }

    return diagnose


def _validate_action(state: IncidentState) -> dict[str, object]:
    """提案を Python の安全ルールで検証し、正規化した操作・リスク・判断理由を返す。"""
    result = evaluate_action(state.get("proposed_action"))
    return {
        "proposed_action": result.normalized_action,
        "risk_level": result.risk,
        "policy_reason": result.reason,
        "approval_status": "PENDING" if result.allowed and result.risk != "LOW" else None,
        "node_trace": [*state.get("node_trace", []), "validate_action"],
    }


def _route_after_policy(state: IncidentState) -> str:
    """安全ルール違反は中止、NONE は変更なしで完了、それ以外は承認待ちへ分岐する。"""
    if state.get("risk_level") == "BLOCKED":
        return "blocked"
    if (state.get("proposed_action") or {}).get("type") == "NONE":
        return "no_action"
    return "approval"


def _approval(state: IncidentState) -> dict[str, object]:
    """提案内容を画面向けの中断データとして渡し、人の判断を待つ。

    再開値を ApprovalResponse として検証し、拒否・承認・編集後の承認を状態に反映する。
    編集値には安全ルールを再適用する。再開時はノードの先頭から実行されるため、
    interrupt より前にサーバー変更などの副作用を置かない。
    """
    action = dict(state.get("proposed_action") or {})
    payload = {
        "type": "approval_required",
        "incident_id": state["incident_id"],
        "server_id": state["server_id"],
        "action": action.get("type"),
        "current_value": action.get("current_value"),
        "proposed_value": action.get("proposed_value"),
        "deployment_name": action.get("deployment_name"),
        "reason": (state.get("diagnosis") or {}).get("reason"),
        "risk": state.get("risk_level"),
    }
    # ここで状態を保存して人の判断を待つ。再開時には、このノードの先頭から再実行される。
    raw = interrupt(payload)
    if not isinstance(raw, Mapping):
        raise ValueError("承認結果はオブジェクト形式で指定してください。")
    # 再開データも外部入力として検証し、不正な判断値を実行側へ流さない。
    response = ApprovalResponse.model_validate(dict(raw))

    trace = [*state.get("node_trace", []), "approval"]
    if response.decision == "reject":
        return {"approval_status": "REJECTED", "node_trace": trace}

    if response.decision == "edit_and_approve":
        if response.proposed_value is None:
            return {
                "approval_status": "BLOCKED",
                "policy_reason": "編集後の値を指定してください。",
                "node_trace": trace,
            }
        action["proposed_value"] = response.proposed_value
        # 人が編集した値も、LLM の提案と同じ安全ルールで検証する。
        checked = evaluate_action(action)
        if not checked.allowed:
            return {
                "proposed_action": checked.normalized_action,
                "risk_level": checked.risk,
                "policy_reason": checked.reason,
                "approval_status": "BLOCKED",
                "node_trace": trace,
            }
        return {
            "proposed_action": checked.normalized_action,
            "risk_level": checked.risk,
            "policy_reason": checked.reason,
            "approval_status": "APPROVED",
            "node_trace": trace,
        }

    return {"approval_status": "APPROVED", "node_trace": trace}


def _route_after_approval(state: IncidentState) -> str:
    """明示的な APPROVED だけを書き込みへ進め、拒否またはその他の状態は終了側へ送る。"""
    if state.get("approval_status") == "APPROVED":
        return "approved"
    if state.get("approval_status") == "REJECTED":
        return "rejected"
    return "blocked"


def _write_call(state: IncidentState) -> tuple[str, dict[str, object]]:
    """承認と安全性を再確認し、実行する書き込みツール名と引数の組を返す。

    未承認または許可されない操作は PermissionError、対応するツールがない操作は
    ValueError とする。この関数自体はツールを実行しない。
    """
    checked = evaluate_action(state.get("proposed_action"))
    if state.get("approval_status") != "APPROVED":
        raise PermissionError("書き込み操作には人による承認が必要です。")
    if not checked.allowed or checked.risk == "BLOCKED":
        raise PermissionError(f"書き込み操作を中止しました: {checked.reason}")

    action = checked.normalized_action
    server_id = state["server_id"]
    if action["type"] == "SET_THREAD_POOL_MAX_THREADS":
        return "set_thread_pool_max_threads", {"server_id": server_id, "value": action["proposed_value"]}
    if action["type"] == "SET_DATASOURCE_MAX_POOL_SIZE":
        return "set_datasource_max_pool_size", {"server_id": server_id, "value": action["proposed_value"]}
    if action["type"] == "RESTART_DEPLOYMENT":
        return "restart_deployment", {"server_id": server_id, "deployment_name": action["deployment_name"]}
    if action["type"] == "RELOAD_SERVER":
        return "reload_server", {"server_id": server_id}
    raise ValueError(f"action does not map to a write tool: {action['type']}")


def _prepare_write(state: IncidentState) -> dict[str, object]:
    """承認済み操作を、書き込み ToolNode が実行できる AIMessage の呼び出し形式に変換する。"""
    # 承認済みかを再確認し、Python が書き込みツールと引数を決定する。
    name, args = _write_call(state)
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": f"write-{uuid.uuid4().hex[:12]}", "type": "tool_call"}
        ],
    )
    return {"messages": [message], "node_trace": [*state.get("node_trace", []), "prepare_write"]}


def _capture_write(state: IncidentState) -> dict[str, object]:
    """直前の書き込み ToolMessage を正規化して保存し、復旧試行回数を1増やす。

    ツール応答がなければ RuntimeError。応答の保存だけでは復旧成功とは判定しない。
    """
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], ToolMessage):
        raise RuntimeError("write ToolNode did not produce a ToolMessage")
    message = messages[-1]
    return {
        "execution_result": {
            "tool_name": message.name,
            "tool_call_id": message.tool_call_id,
            "content": normalize_tool_result(message.content),
        },
        "recovery_attempts": state.get("recovery_attempts", 0) + 1,
        "node_trace": [*state.get("node_trace", []), "capture_write"],
    }


def _tool_map(tools: Sequence[Any]) -> dict[str, Any]:
    """ツールを名前で引ける辞書に変換する。同名が複数ある場合は後のものを使う。"""
    return {tool.name: tool for tool in tools}


def make_verify_node(read_tools: Sequence[Any]):
    """読取ツールを捕捉し、復旧後のメトリクスを確認する非同期ノードを返す。"""
    tools = _tool_map(read_tools)

    async def verify(state: IncidentState) -> dict[str, object]:
        """サーバーが UP かつエラー率 5% 未満であることと、操作別の正常条件を確認する。

        プール変更は容量と滞留・タイムアウト、再起動はデプロイ状態も確認する。
        判定結果を recovered に、取得した指標を根拠一覧に追加して返す。
        """
        server_id = state["server_id"]
        action_type = (state.get("proposed_action") or {}).get("type")
        health = normalize_tool_result(await tools["get_server_health"].ainvoke({"server_id": server_id}))
        details: dict[str, Any] = {"health": health}
        # 復旧は LLM の自己申告ではなく、実際に取得したメトリクスで判定する。
        # 必須指標が欠けた場合も成功判定しないよう、異常側のデフォルト値を使う。
        healthy = health.get("status") == "UP" and float(health.get("request_error_rate", 1.0)) < 0.05

        if action_type == "SET_THREAD_POOL_MAX_THREADS":
            pool = normalize_tool_result(
                await tools["get_thread_pool_status"].ainvoke({"server_id": server_id})
            )
            details["thread_pool"] = pool
            healthy = healthy and int(pool.get("active_threads", 10**9)) <= int(pool.get("max_threads", -1))
            healthy = (
                healthy and int(pool.get("queue_size", 1)) == 0 and int(pool.get("rejected_tasks", 1)) == 0
            )
        elif action_type == "SET_DATASOURCE_MAX_POOL_SIZE":
            ds = normalize_tool_result(await tools["get_datasource_status"].ainvoke({"server_id": server_id}))
            details["datasource"] = ds
            healthy = healthy and int(ds.get("active_count", 10**9)) <= int(ds.get("max_pool_size", -1))
            healthy = healthy and int(ds.get("timed_out_requests", 1)) == 0
        elif action_type == "RESTART_DEPLOYMENT":
            deployment = normalize_tool_result(
                await tools["get_deployment_status"].ainvoke({"server_id": server_id})
            )
            details["deployment"] = deployment
            healthy = healthy and deployment.get("status") == "OK" and bool(deployment.get("enabled"))

        return {
            "recovered": healthy,
            "evidence": [
                *state.get("evidence", []),
                {"tool_name": "recovery_verification", "content": details},
            ],
            "node_trace": [*state.get("node_trace", []), "verify_recovery"],
        }

    return verify


def make_recovery_route(max_attempts: int):
    """復旧成否と書き込み試行回数の上限で、終了または再調査を選ぶ分岐関数を返す。"""

    def route(state: IncidentState) -> str:
        """復旧済みなら成功終了し、未復旧なら試行上限に応じて停止または再調査へ進む。"""
        if state.get("recovered") is True:
            return "recovered"
        return "fail_safe" if state.get("recovery_attempts", 0) >= max_attempts else "retry"

    return route


def _prepare_retry(state: IncidentState) -> dict[str, object]:
    """前の診断を見直す指示を追記し、提案・承認・実行結果をクリアして再調査を始める。

    会話と根拠、累計の復旧試行回数は維持し、調査ラウンド数だけをゼロに戻す。
    """
    return {
        "messages": [
            HumanMessage(
                content="The approved remediation did not recover the server. Re-investigate with read-only tools and challenge the previous diagnosis."
            )
        ],
        "investigation_count": 0,
        "diagnosis": None,
        "proposed_action": None,
        "risk_level": None,
        "policy_reason": None,
        "approval_status": None,
        "execution_result": None,
        "recovered": None,
        "node_trace": [*state.get("node_trace", []), "prepare_retry"],
    }


def _rejected(state: IncidentState) -> dict[str, object]:
    """人が変更を拒否した理由と終了ノードの履歴を記録する。"""
    return {
        "failure_reason": "提案された変更は人によって拒否されました。",
        "node_trace": [*state.get("node_trace", []), "rejected"],
    }


def _blocked(state: IncidentState) -> dict[str, object]:
    """安全ルールによる中止を承認状態と失敗理由に記録する。"""
    return {
        "approval_status": "BLOCKED",
        "failure_reason": state.get("policy_reason") or "安全ルールにより操作を中止しました。",
        "node_trace": [*state.get("node_trace", []), "blocked"],
    }


def _no_action(state: IncidentState) -> dict[str, object]:
    """変更不要の判断を、ワークフロー上の正常完了として recovered=True にする。

    この経路では書き込みやメトリクスによる復旧確認を行わない。
    """
    return {"recovered": True, "node_trace": [*state.get("node_trace", []), "no_action"]}


def _recovered(state: IncidentState) -> dict[str, object]:
    """復旧確認を通過したことをノード履歴に追記する。"""
    return {"node_trace": [*state.get("node_trace", []), "recovered"]}


def _fail_safe(state: IncidentState) -> dict[str, object]:
    """復旧試行上限に達したため、未復旧と担当者の対応が必要な理由を記録して終了する。"""
    return {
        "recovered": False,
        "failure_reason": "復旧試行回数の上限に達しました。運用担当者による対応が必要です。",
        "node_trace": [*state.get("node_trace", []), "fail_safe"],
    }


def build_incident_graph(
    read_tools: Sequence[Any],
    write_tools: Sequence[Any],
    *,
    checkpointer: Any,
    settings: Settings,
    investigator: Any | None = None,
    diagnoser: Any | None = None,
):
    """調査から復旧確認までのノードと分岐を結び、コンパイル済みグラフを返す。

    LLM へ渡すツールは read_tools のみ。write_tools は安全性検証と人の承認後に
    専用 ToolNode で実行する。checkpointer は承認待ちの保存・再開を担う。
    調査・復旧の上限は settings を使い、モデルはテストなどで差し替え可能。
    """
    investigator = investigator or build_investigator(settings, read_tools)
    diagnoser = diagnoser or build_diagnoser(settings)

    graph = StateGraph(IncidentState)
    graph.add_node("prepare_investigation", _prepare_investigation)
    graph.add_node("investigate", make_investigate_node(investigator))
    graph.add_node("read_tools", ToolNode(list(read_tools)))
    graph.add_node("record_evidence", _record_tool_evidence)
    graph.add_node("diagnose", make_diagnose_node(diagnoser))
    graph.add_node("validate_action", _validate_action)
    graph.add_node("approval", _approval)
    graph.add_node("prepare_write", _prepare_write)
    graph.add_node("write_tools", ToolNode(list(write_tools)))
    graph.add_node("capture_write", _capture_write)
    graph.add_node("verify_recovery", make_verify_node(read_tools))
    graph.add_node("prepare_retry", _prepare_retry)
    graph.add_node("recovered", _recovered)
    graph.add_node("rejected", _rejected)
    graph.add_node("blocked", _blocked)
    graph.add_node("no_action", _no_action)
    graph.add_node("fail_safe", _fail_safe)

    # 調査ループは読み取り専用。ツール要求がなくなるかラウンド上限に達したら診断へ進む。
    graph.add_edge(START, "prepare_investigation")
    graph.add_edge("prepare_investigation", "investigate")
    graph.add_conditional_edges(
        "investigate", _route_after_investigate, {"read_tools": "read_tools", "diagnose": "diagnose"}
    )
    graph.add_edge("read_tools", "record_evidence")
    graph.add_conditional_edges(
        "record_evidence",
        make_round_route(settings.max_investigation_rounds),
        {"investigate": "investigate", "diagnose": "diagnose"},
    )
    # 診断の提案を安全ルールに通し、変更操作は必ず人の承認を経由させる。
    graph.add_edge("diagnose", "validate_action")
    graph.add_conditional_edges(
        "validate_action",
        _route_after_policy,
        {"approval": "approval", "blocked": "blocked", "no_action": "no_action"},
    )
    graph.add_conditional_edges(
        "approval",
        _route_after_approval,
        {"approved": "prepare_write", "rejected": "rejected", "blocked": "blocked"},
    )
    graph.add_edge("prepare_write", "write_tools")
    graph.add_edge("write_tools", "capture_write")
    graph.add_edge("capture_write", "verify_recovery")
    graph.add_conditional_edges(
        "verify_recovery",
        make_recovery_route(settings.max_recovery_attempts),
        {"recovered": "recovered", "retry": "prepare_retry", "fail_safe": "fail_safe"},
    )
    # 未復旧なら再調査から診断・承認をやり直し、以前の承認を使い回さない。
    graph.add_edge("prepare_retry", "investigate")
    for terminal in ("recovered", "rejected", "blocked", "no_action", "fail_safe"):
        graph.add_edge(terminal, END)

    return graph.compile(checkpointer=checkpointer)
