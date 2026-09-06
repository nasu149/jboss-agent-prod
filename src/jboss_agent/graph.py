"""学習ポイントだけを残した 1 本の LangGraph。

このファイルで確認したいのは次の 3 点だけ。

1. Gemini の分類結果を State に入れ、Conditional Edge で処理を分岐する。
2. 分岐先の Node から JBoss の MCP read Tool を呼ぶ。
3. write Tool の直前で ``interrupt()`` し、人が承認した場合だけ再開・実行する。

監視 cursor、DB への Incident 記録、再試行、複雑な Policy などは意図的に持たない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Mapping, TypedDict

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from jboss_agent.config import Settings
from jboss_agent.mcp.client import as_dict, by_name

Category = Literal[
    "THREAD_POOL_CONFIGURATION",
    "DATASOURCE_POOL_EXHAUSTION",
    "DEPLOYMENT_FAILURE",
    "NORMAL_ACTIVITY",
]


class LogAnalysis(BaseModel):
    """Gemini に返させる最小限の Structured Output。"""

    category: Category = Field(description="Most likely category represented by the log")
    summary: str = Field(description="Short reason for the classification")


class AgentState(TypedDict, total=False):
    """Graph の Node 間で受け渡す状態。

    ``trace`` を見ると、どの Node をどの順番で通ったかを学習画面から確認できる。
    ``proposed_action`` は LLM ではなく分岐先 Node が固定形式で作るため、write Tool の
    名前や引数を LLM が自由生成する構造にはしていない。
    """

    server_id: str
    log_lines: list[str]
    category: Category
    summary: str
    evidence: dict[str, Any]
    proposed_action: dict[str, Any] | None
    approved: bool
    execution_result: dict[str, Any]
    recovered: bool
    status: str
    trace: list[str]


def build_classifier(settings: Settings) -> Any:
    """ログ分類だけを担当する Gemini Structured Output client を作る。"""
    if not settings.has_google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured. Copy .env.example to .env and set it.")

    model = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        api_key=settings.google_api_key,
        temperature=0,
        timeout=30,
        max_retries=2,
    )
    return model.with_structured_output(schema=LogAnalysis.model_json_schema(), method="json_schema")


def _classification_prompt(lines: list[str]) -> str:
    """4 種類のどれに分けるかだけを Gemini に依頼する簡潔な Prompt を返す。"""
    log_text = "\n".join(lines)
    return f"""You are classifying a tiny educational JBoss incident demo.
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
    """MCP ``read_server_log`` Tool を捕捉した Node を返す。"""
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
    """分類用 LLM を捕捉し、結果を State に書き込む Node を返す。"""

    def classify(state: AgentState) -> dict[str, object]:
        """Gemini の Structured Output を検証し、category と summary を保存する。"""
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


def route_category(state: AgentState) -> str:
    """LLM が State に書いた category を Conditional Edge の分岐名へ変換する。"""
    return state["category"]


def make_thread_pool_node(read_tools: Sequence[Any]):
    """thread pool の read Tool を呼び、デモ用の固定対処案を作る Node を返す。"""
    tool = by_name(read_tools, "get_thread_pool_status")

    async def inspect(state: AgentState) -> dict[str, object]:
        """現在値を根拠として読み、max_threads を baseline の 80 に戻す案を作る。"""
        evidence = as_dict(await tool.ainvoke({"server_id": state["server_id"]}))
        return {
            "evidence": evidence,
            "proposed_action": {
                "tool": "set_thread_pool_max_threads",
                "args": {"server_id": state["server_id"], "value": 80},
                "description": "thread pool の max_threads を 80 に戻す",
            },
            "trace": [*state.get("trace", []), "inspect_thread_pool"],
        }

    return inspect


def make_datasource_node(read_tools: Sequence[Any]):
    """datasource の read Tool を呼び、デモ用の固定対処案を作る Node を返す。"""
    tool = by_name(read_tools, "get_datasource_status")

    async def inspect(state: AgentState) -> dict[str, object]:
        """現在値を根拠として読み、max_pool_size を baseline の 30 に戻す案を作る。"""
        evidence = as_dict(await tool.ainvoke({"server_id": state["server_id"]}))
        return {
            "evidence": evidence,
            "proposed_action": {
                "tool": "set_datasource_max_pool_size",
                "args": {"server_id": state["server_id"], "value": 30},
                "description": "datasource の max_pool_size を 30 に戻す",
            },
            "trace": [*state.get("trace", []), "inspect_datasource"],
        }

    return inspect


def make_deployment_node(read_tools: Sequence[Any]):
    """deployment の read Tool を呼び、再起動案を作る Node を返す。"""
    tool = by_name(read_tools, "get_deployment_status")

    async def inspect(state: AgentState) -> dict[str, object]:
        """deployment 状態を根拠として読み、``app.war`` の再起動案を作る。"""
        evidence = as_dict(await tool.ainvoke({"server_id": state["server_id"]}))
        return {
            "evidence": evidence,
            "proposed_action": {
                "tool": "restart_deployment",
                "args": {"server_id": state["server_id"], "deployment_name": "app.war"},
                "description": "app.war を再起動する",
            },
            "trace": [*state.get("trace", []), "inspect_deployment"],
        }

    return inspect


def normal_activity(state: AgentState) -> dict[str, object]:
    """正常ログなら MCP write や HITL に進まず、そのまま終了する。"""
    return {
        "proposed_action": None,
        "status": "NO_INCIDENT",
        "trace": [*state.get("trace", []), "normal_activity"],
    }


def approval(state: AgentState) -> dict[str, object]:
    """write Tool の直前で Graph を停止し、人の approve/reject を待つ。

    ``interrupt()`` には JSON 化できる対処案を渡す。再開時はこの Node の先頭から
    再実行され、``Command(resume=True/False)`` の値が interrupt の戻り値になる。
    interrupt より前には副作用を置かない。
    """
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
        raise ValueError("resume value must be True (approve) or False (reject)")
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
            raise ValueError("write tool args must be a dict")
        result = as_dict(await tool_map[name].ainvoke(args))
        return {
            "execution_result": result,
            "trace": [*state.get("trace", []), "execute_fix"],
        }

    return execute


def make_verify_node(read_tools: Sequence[Any]):
    """変更後の Fake JBoss を read Tool で確認し、復旧結果を判定する Node を返す。"""
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
                int(result.get("max_pool_size", 0)) >= 30
                and int(result.get("timed_out_requests", 1)) == 0
            )
        else:
            result = as_dict(await deployment_tool.ainvoke({"server_id": server_id}))
            recovered = result.get("status") == "OK"

        return {
            "evidence": result,
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
):
    """学習用の 1 本の Graph を構築し、指定 Checkpointer 付きで compile する。

    本番では durable checkpointer が必要だが、このデモは 1 人・1 回だけを前提に
    ``InMemorySaver`` を渡す。HITL の resume には同じ checkpointer と thread_id を使う。
    """
    resolved_classifier = classifier or build_classifier(settings)

    graph = StateGraph(AgentState)
    graph.add_node("read_log", make_read_log_node(read_tools))
    graph.add_node("classify_log", make_classify_node(resolved_classifier))
    graph.add_node("inspect_thread_pool", make_thread_pool_node(read_tools))
    graph.add_node("inspect_datasource", make_datasource_node(read_tools))
    graph.add_node("inspect_deployment", make_deployment_node(read_tools))
    graph.add_node("normal_activity", normal_activity)
    graph.add_node("approval", approval)
    graph.add_node("execute_fix", make_execute_node(write_tools))
    graph.add_node("verify_recovery", make_verify_node(read_tools))
    graph.add_node("rejected", rejected)

    graph.add_edge(START, "read_log")
    graph.add_edge("read_log", "classify_log")
    graph.add_conditional_edges(
        "classify_log",
        route_category,
        {
            "THREAD_POOL_CONFIGURATION": "inspect_thread_pool",
            "DATASOURCE_POOL_EXHAUSTION": "inspect_datasource",
            "DEPLOYMENT_FAILURE": "inspect_deployment",
            "NORMAL_ACTIVITY": "normal_activity",
        },
    )
    graph.add_edge("inspect_thread_pool", "approval")
    graph.add_edge("inspect_datasource", "approval")
    graph.add_edge("inspect_deployment", "approval")
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
