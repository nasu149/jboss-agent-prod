"""Streamlit で LangGraph / MCP / Human-in-the-loop を 1 回だけ試す学習画面。

画面を業務アプリとして作り込むことは目的にしない。シナリオ投入、Graph 実行、
interrupt の承認・拒否、State/Node trace の確認だけに絞っている。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import streamlit as st
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from jboss_agent.config import get_settings
from jboss_agent.fake_jboss import SCENARIOS, FakeJBoss
from jboss_agent.graph import build_graph
from jboss_agent.mcp.client import load_jboss_tools

settings = get_settings()
fake = FakeJBoss(settings.fake_jboss_data_dir, settings.server_id)
fake.ensure_initialized()


def run_async(coro: Any) -> Any:
    """Streamlit の同期イベントから LangGraph の async 実行を呼ぶ。"""
    return asyncio.run(coro)


def reset_learning_run() -> None:
    """新しい 1 回のデモ用に Checkpointer と thread_id を作り直す。"""
    st.session_state["checkpointer"] = InMemorySaver()
    st.session_state["thread_id"] = f"demo-{uuid.uuid4().hex[:8]}"
    st.session_state["result"] = None


def ensure_learning_run() -> None:
    """初回表示時にだけ InMemorySaver と thread_id を用意する。"""
    if "checkpointer" not in st.session_state:
        reset_learning_run()


async def invoke_graph(resume: bool | None = None) -> dict[str, Any]:
    """MCP Tool を読み、同じ Checkpointer/thread_id で Graph を開始または再開する。"""
    read_tools, write_tools = await load_jboss_tools()
    graph = build_graph(
        read_tools,
        write_tools,
        checkpointer=st.session_state["checkpointer"],
        settings=settings,
    )
    config = {"configurable": {"thread_id": st.session_state["thread_id"]}}
    if resume is None:
        return await graph.ainvoke({"server_id": settings.server_id, "trace": []}, config=config)
    return await graph.ainvoke(Command(resume=resume), config=config)


def interrupt_payload(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """LangGraph の ``__interrupt__`` から画面表示用の payload を取り出す。"""
    if not result:
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", interrupts[0])
    return value if isinstance(value, dict) else {"value": value}


def execute(resume: bool | None = None) -> None:
    """Graph を実行し、結果または例外を Streamlit に表示可能な状態へ保存する。"""
    try:
        with st.spinner("LangGraph を実行中..."):
            st.session_state["result"] = run_async(invoke_graph(resume))
    except Exception as exc:  # noqa: BLE001 - UI boundary: show model/MCP errors to the learner.
        st.error(f"実行に失敗しました: {exc}")


ensure_learning_run()

st.title("JBoss Incident Agent - Learning Minimum")
st.caption("目的: LangGraph の State/Node/Conditional Edge、MCP、Human-in-the-loop を最小コードで追う")

st.code(
    """START → read_log(MCP) → classify_log(Gemini)
                         ↓ category
      ┌──────────────────┼────────────────────┐
      ↓                  ↓                    ↓
 thread pool         datasource          deployment        normal
      └──────────────────┴────────────────────┘             ↓
                         ↓                                  END
                   approval(interrupt)
                    ↓ approve/reject
                execute_fix(MCP)
                         ↓
                verify_recovery(MCP)
                         ↓
                        END""",
    language="text",
)

if not settings.has_google_api_key:
    st.warning("`.env` に GOOGLE_API_KEY を設定すると Agent を実行できます。")

scenario = st.selectbox("疑似シナリオ", SCENARIOS)
left, right = st.columns(2)
if left.button("1. このシナリオを投入", use_container_width=True):
    fake.inject(scenario)
    reset_learning_run()
    st.success(f"{scenario} を投入しました。")

if right.button(
    "2. Agent を実行",
    type="primary",
    use_container_width=True,
    disabled=not settings.has_google_api_key,
):
    execute()

st.subheader("Fake server.log")
st.code("\n".join(fake.read_server_log(settings.server_id)["lines"]), language="text")

result = st.session_state.get("result")
if result:
    st.subheader("LangGraph State")
    st.write(f"**category:** `{result.get('category', '—')}`")
    st.write(f"**summary:** {result.get('summary', '—')}")
    st.write(f"**status:** `{result.get('status', 'RUNNING / INTERRUPTED')}`")
    if result.get("evidence"):
        st.write("**MCP evidence**")
        st.json(result["evidence"])
    if result.get("proposed_action"):
        st.write("**proposed action**")
        st.json(result["proposed_action"])
    if result.get("execution_result"):
        st.write("**write result**")
        st.json(result["execution_result"])
    st.write("**Node trace**")
    st.code(" → ".join(result.get("trace", [])), language="text")

pending = interrupt_payload(result)
if pending:
    st.subheader("3. Human-in-the-loop")
    st.info(pending.get("question", "Approve?"))
    st.json({key: value for key, value in pending.items() if key != "question"})
    approve, reject = st.columns(2)
    if approve.button("Approve", type="primary", use_container_width=True):
        execute(True)
        st.rerun()
    if reject.button("Reject", use_container_width=True):
        execute(False)
        st.rerun()
