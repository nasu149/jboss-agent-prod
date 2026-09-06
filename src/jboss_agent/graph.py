"""JBoss 障害一次対応を題材にした、1 本の LangGraph。

処理の中心は次の 4 点。
1. server.log を Gemini が分類し、Conditional Edge で障害/正常を分ける。
2. 障害時は Gemini 自身が read-only MCP Tool を選び、ToolNode が実行する。
3. JBoss への write は Human-in-the-loop の承認後だけ Python が実行する。
4. 障害検知時は LangGraph の通常 Node から Teams へ通知する。

監視スケジューラや複数 Incident の永続管理は持たず、1 人が 1 回の障害対応を
コードで追いやすい構成を優先する。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from jboss_agent.config import Settings
from jboss_agent.mcp.client import as_dict, by_name
from jboss_agent.teams import send_teams_alert

Category = Literal[
    "THREAD_POOL_CONFIGURATION",
    "DATASOURCE_POOL_EXHAUSTION",
    "DEPLOYMENT_FAILURE",
    "NORMAL_ACTIVITY",
]


class LogAnalysis(BaseModel):
    """Gemini に返させる最小限のログ分類結果。"""

    category: Category = Field(description="Most likely category represented by the log")
    summary: str = Field(description="Short reason for the classification")


class AgentState(TypedDict, total=False):
    """Graph の Node 間で共有する State。

    ``messages`` は ToolNode が利用する会話履歴で、``add_messages`` reducer により
    HumanMessage / AIMessage / ToolMessage が順に蓄積される。write Tool は messages に
    含めず、Human approval 後に ``proposed_action`` を Python が直接実行する。
    """

    server_id: str
    log_lines: list[str]
    category: Category
    summary: str
    messages: Annotated[list[Any], add_messages]
    selected_read_tools: list[str]
    evidence: dict[str, Any]
    teams_result: dict[str, Any]
    proposed_action: dict[str, Any] | None
    approved: bool
    execution_result: dict[str, Any]
    recovered: bool
    status: str
    trace: list[str]


def build_gemini(settings: Settings) -> ChatGoogleGenerativeAI:
    """分類と read Tool 選択で共通利用する Gemini client を作る。"""
    if not settings.has_google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured. Copy .env.example to .env and set it.")

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        api_key=settings.google_api_key,
        temperature=0,
        timeout=30,
        max_retries=2,
    )


def build_classifier(settings: Settings) -> Any:
    """ログ分類用の Structured Output client を作る。"""
    return build_gemini(settings).with_structured_output(
        schema=LogAnalysis.model_json_schema(),
        method="json_schema",
    )


def build_investigator(settings: Settings, read_tools: Sequence[Any]) -> Any:
    """詳細調査用 Gemini に read-only MCP Tool だけを bind する。

    ``read_server_log`` は Graph の入口で固定実行済みなので除外し、Gemini には
    thread pool / datasource / deployment の状態確認 Tool から 1 つを選ばせる。
    ``tool_choice="any"`` により、調査 Node では Tool Call を必須にする。
    """
    diagnostic_tools = [tool for tool in read_tools if tool.name != "read_server_log"]
    if not diagnostic_tools:
        raise ValueError("at least one diagnostic read tool is required")
    return build_gemini(settings).bind_tools(diagnostic_tools, tool_choice="any")


def _classification_prompt(lines: list[str]) -> str:
    """4 種類のどれに分けるかだけを Gemini に依頼する Prompt を返す。"""
    log_text = "\n".join(lines)
    return f"""You are classifying a small educational JBoss incident demo.
Choose exactly one category:
- THREAD_POOL_CONFIGURATION
- DATASOURCE_POOL_EXHAUSTION
- DEPLOYMENT_FAILURE
- NORMAL_ACTIVITY

Do not invent another category. Classify only from the log below.

server.log:
{log_text}
"""


def make_read_log_node(read_tools: Sequence[Any]):
    """MCP ``read_server_log`` Tool を固定で呼ぶ入口 Node を返す。"""
    tool = by_name(read_tools, "read_server_log")

    async def read_log(state: AgentState) -> dict[str, object]:
        """Fake JBoss のログ全体を MCP 経由で読み、State に保存する。"""
        result = as_dict(await tool.ainvoke({"server_id": state["server_id"]}))
        return {
            "log_lines": [str(line) for line in result.get("lines", [])],
            "trace": [*state.get("trace", []), "read_log"],
        }

    return read_log


def make_classify_node(classifier: Any):
    """Gemini の分類結果を State に書き込む Node を返す。"""

    def classify(state: AgentState) -> dict[str, object]:
        """Structured Output を検証し、category と summary を保存する。"""
        raw = classifier.invoke(_classification_prompt(state.get("log_lines", [])))
        if isinstance(raw, LogAnalysis):
            result = raw
        elif isinstance(raw, Mapping):
            result = LogAnalysis.model_validate(dict(raw))
        else:
            raise TypeError(f"classifier returned unsupported type: {type(raw).__name__}")

        return {
            "category": result.category,
            "summary": result.summary,
            "trace": [*state.get("trace", []), "classify_log"],
        }

    return classify


def route_after_classification(state: AgentState) -> str:
    """正常ログは終了系へ、3 種類の障害は通知・調査系へ送る。"""
    return "normal" if state["category"] == "NORMAL_ACTIVITY" else "incident"


def make_notify_teams_node(
    settings: Settings,
    notifier: Callable[[str, str, str], dict[str, Any]] | None = None,
):
    """障害分類結果を Teams へ送る通常の LangGraph Node を返す。

    Teams 通知は MCP Tool ではない。Graph の中に普通の Python Node として置くことで、
    LLM が選ぶ MCP Tool と、Graph が明示的に実行する外部連携を分ける。
    """
    send = notifier or (
        lambda server_id, category, summary: send_teams_alert(
            settings,
            server_id=server_id,
            category=category,
            summary=summary,
        )
    )

    def notify(state: AgentState) -> dict[str, object]:
        """server/category/summary を Teams へ送り、送信結果を State に残す。"""
        result = send(state["server_id"], state["category"], state.get("summary", ""))
        return {
            "teams_result": result,
            "trace": [*state.get("trace", []), "notify_teams"],
        }

    return notify


def prepare_investigation(state: AgentState) -> dict[str, object]:
    """Gemini が read Tool を選ぶための調査依頼を messages に追加する。"""
    prompt = f"""Investigate this JBoss incident.

Server: {state["server_id"]}
Category: {state["category"]}
Classification summary: {state.get("summary", "")}

server.log:
{chr(10).join(state.get("log_lines", []))}

Choose exactly ONE bound read-only tool that is most useful for this category.
Call that tool with server_id={state["server_id"]}.
Do not propose or execute any write operation.
"""
    return {
        "messages": [HumanMessage(content=prompt)],
        "trace": [*state.get("trace", []), "prepare_investigation"],
    }


def make_investigate_node(investigator: Any):
    """read Tool を bind した Gemini に、実際の Tool Call を選ばせる Node を返す。"""

    async def investigate(state: AgentState) -> dict[str, object]:
        """messages を Gemini に渡し、Tool Call を含む AIMessage を State に追加する。"""
        response = await investigator.ainvoke(state.get("messages", []))
        if not isinstance(response, AIMessage):
            raise TypeError(f"investigator returned unsupported type: {type(response).__name__}")
        if not response.tool_calls:
            raise RuntimeError("investigator did not choose a read tool")
        return {
            "messages": [response],
            "trace": [*state.get("trace", []), "investigate"],
        }

    return investigate


def capture_tool_evidence(state: AgentState) -> dict[str, object]:
    """ToolNode が追加した ToolMessage を evidence に変換する。

    ToolNode 自体は ``trace`` を更新しないため、この Node で ``read_tools`` を追記し、
    画面上でも ToolNode を通過したことが分かるようにする。
    """
    tool_messages = [message for message in state.get("messages", []) if isinstance(message, ToolMessage)]
    if not tool_messages:
        raise RuntimeError("ToolNode produced no ToolMessage")

    evidence = {str(message.name): as_dict(message) for message in tool_messages}
    return {
        "selected_read_tools": list(evidence),
        "evidence": evidence,
        "trace": [*state.get("trace", []), "read_tools"],
    }


def route_category(state: AgentState) -> str:
    """分類済み category を、固定対処案を作る Node 名へ分岐させる。"""
    return state["category"]


def propose_thread_pool(state: AgentState) -> dict[str, object]:
    """Thread Pool 障害の固定対処案を作る。"""
    return {
        "proposed_action": {
            "tool": "set_thread_pool_max_threads",
            "args": {"server_id": state["server_id"], "value": 80},
            "description": "thread pool の max_threads を 80 にする",
        },
        "trace": [*state.get("trace", []), "propose_thread_pool"],
    }


def propose_datasource(state: AgentState) -> dict[str, object]:
    """Datasource 障害の固定対処案を作る。"""
    return {
        "proposed_action": {
            "tool": "set_datasource_max_pool_size",
            "args": {"server_id": state["server_id"], "value": 30},
            "description": "datasource の max_pool_size を 30 にする",
        },
        "trace": [*state.get("trace", []), "propose_datasource"],
    }


def propose_deployment(state: AgentState) -> dict[str, object]:
    """Deployment 障害の固定対処案を作る。"""
    return {
        "proposed_action": {
            "tool": "restart_deployment",
            "args": {"server_id": state["server_id"], "deployment_name": "app.war"},
            "description": "app.war を再起動する",
        },
        "trace": [*state.get("trace", []), "propose_deployment"],
    }


def normal_activity(state: AgentState) -> dict[str, object]:
    """正常ログなら Teams / ToolNode / MCP write / HITL を呼ばず終了する。"""
    return {
        "proposed_action": None,
        "status": "NO_INCIDENT",
        "trace": [*state.get("trace", []), "normal_activity"],
    }


def approval(state: AgentState) -> dict[str, object]:
    """write Tool の直前で Graph を停止し、人の approve/reject を待つ。"""
    decision = interrupt(
        {
            "question": "この JBoss 変更を実行しますか？",
            "category": state["category"],
            "summary": state.get("summary", ""),
            "evidence": state.get("evidence", {}),
            "action": state.get("proposed_action"),
        }
    )
    if not isinstance(decision, bool):
        raise TypeError("resume value must be True (approve) or False (reject)")
    return {
        "approved": decision,
        "trace": [*state.get("trace", []), "approval"],
    }


def route_approval(state: AgentState) -> str:
    """人の判断を write 実行または拒否終了へ分岐する。"""
    return "execute" if state.get("approved") else "reject"


def make_execute_node(write_tools: Sequence[Any]):
    """承認済みの固定対処案に対応する MCP write Tool を実行する Node を返す。"""
    tool_map = {tool.name: tool for tool in write_tools}

    async def execute(state: AgentState) -> dict[str, object]:
        """Human approval が True の場合だけ、State の固定 Tool/引数を実行する。"""
        if state.get("approved") is not True:
            raise PermissionError("write tool requires human approval")
        action = state.get("proposed_action") or {}
        name = str(action.get("tool", ""))
        if name not in tool_map:
            raise ValueError(f"unsupported write tool: {name}")
        args = action.get("args")
        if not isinstance(args, dict):
            raise TypeError("write tool args must be a dict")
        result = as_dict(await tool_map[name].ainvoke(args))
        return {
            "execution_result": result,
            "trace": [*state.get("trace", []), "execute_fix"],
        }

    return execute


def make_verify_node(read_tools: Sequence[Any]):
    """変更後の Fake JBoss を MCP read Tool で確認する Node を返す。"""
    thread_tool = by_name(read_tools, "get_thread_pool_status")
    datasource_tool = by_name(read_tools, "get_datasource_status")
    deployment_tool = by_name(read_tools, "get_deployment_status")

    async def verify(state: AgentState) -> dict[str, object]:
        """分類ごとの最小条件だけを確認し、成功/失敗を State に保存する。"""
        server_id = state["server_id"]
        category = state["category"]

        if category == "THREAD_POOL_CONFIGURATION":
            result = as_dict(await thread_tool.ainvoke({"server_id": server_id}))
            recovered = int(result.get("max_threads", 0)) >= 80 and int(result.get("queue_size", 1)) == 0
        elif category == "DATASOURCE_POOL_EXHAUSTION":
            result = as_dict(await datasource_tool.ainvoke({"server_id": server_id}))
            recovered = (
                int(result.get("max_pool_size", 0)) >= 30 and int(result.get("timed_out_requests", 1)) == 0
            )
        else:
            result = as_dict(await deployment_tool.ainvoke({"server_id": server_id}))
            recovered = result.get("status") == "OK"

        return {
            "evidence": {**state.get("evidence", {}), "recovery_verification": result},
            "recovered": recovered,
            "status": "RECOVERED" if recovered else "FAILED",
            "trace": [*state.get("trace", []), "verify_recovery"],
        }

    return verify


def rejected(state: AgentState) -> dict[str, object]:
    """人が拒否した場合は write Tool を呼ばずに終了する。"""
    return {
        "recovered": False,
        "status": "REJECTED",
        "trace": [*state.get("trace", []), "rejected"],
    }


def build_graph(
    read_tools: Sequence[Any],
    write_tools: Sequence[Any],
    *,
    checkpointer: Any,
    settings: Settings,
    classifier: Any | None = None,
    investigator: Any | None = None,
    notifier: Callable[[str, str, str], dict[str, Any]] | None = None,
):
    """JBoss 障害一次対応 Graph を構築し、指定 Checkpointer 付きで compile する。

    Gemini に bind するのは diagnostic read Tool だけ。write Tool は ToolNode に渡さず、
    Human approval 後の ``execute_fix`` Node からのみ実行する。
    """
    diagnostic_tools = [tool for tool in read_tools if tool.name != "read_server_log"]
    resolved_classifier = classifier or build_classifier(settings)
    resolved_investigator = investigator or build_investigator(settings, read_tools)

    graph = StateGraph(AgentState)
    graph.add_node("read_log", make_read_log_node(read_tools))
    graph.add_node("classify_log", make_classify_node(resolved_classifier))
    graph.add_node("notify_teams", make_notify_teams_node(settings, notifier))
    graph.add_node("prepare_investigation", prepare_investigation)
    graph.add_node("investigate", make_investigate_node(resolved_investigator))
    graph.add_node("read_tools", ToolNode(diagnostic_tools))
    graph.add_node("capture_tool_evidence", capture_tool_evidence)
    graph.add_node("propose_thread_pool", propose_thread_pool)
    graph.add_node("propose_datasource", propose_datasource)
    graph.add_node("propose_deployment", propose_deployment)
    graph.add_node("normal_activity", normal_activity)
    graph.add_node("approval", approval)
    graph.add_node("execute_fix", make_execute_node(write_tools))
    graph.add_node("verify_recovery", make_verify_node(read_tools))
    graph.add_node("rejected", rejected)

    graph.add_edge(START, "read_log")
    graph.add_edge("read_log", "classify_log")
    graph.add_conditional_edges(
        "classify_log",
        route_after_classification,
        {"incident": "notify_teams", "normal": "normal_activity"},
    )
    graph.add_edge("notify_teams", "prepare_investigation")
    graph.add_edge("prepare_investigation", "investigate")
    graph.add_edge("investigate", "read_tools")
    graph.add_edge("read_tools", "capture_tool_evidence")
    graph.add_conditional_edges(
        "capture_tool_evidence",
        route_category,
        {
            "THREAD_POOL_CONFIGURATION": "propose_thread_pool",
            "DATASOURCE_POOL_EXHAUSTION": "propose_datasource",
            "DEPLOYMENT_FAILURE": "propose_deployment",
        },
    )
    graph.add_edge("propose_thread_pool", "approval")
    graph.add_edge("propose_datasource", "approval")
    graph.add_edge("propose_deployment", "approval")
    graph.add_edge("normal_activity", END)
    graph.add_conditional_edges(
        "approval",
        route_approval,
        {"execute": "execute_fix", "reject": "rejected"},
    )
    graph.add_edge("execute_fix", "verify_recovery")
    graph.add_edge("verify_recovery", END)
    graph.add_edge("rejected", END)

    return graph.compile(checkpointer=checkpointer)
