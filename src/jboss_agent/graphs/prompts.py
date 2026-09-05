"""ログ分類・読み取り調査・構造化診断の3段階で使う LLM 向け入力を組み立てる。

監視ログとツールで得た根拠を渡し、シミュレーターの正解は含めない。
ここでは文字列とメッセージを作るだけで、モデルやツールの呼び出しは行わない。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from jboss_agent.graphs.state import IncidentState

# 監視段階では与えたログだけを分類させ、未取得のメトリクスや正解名の推測を抑える。
LOG_CLASSIFICATION_PROMPT = """You classify JBoss EAP-like server logs for incident monitoring.
Use only supplied log lines. Choose one category: NORMAL, THREAD_POOL, DATASOURCE_POOL,
DEPLOYMENT, UNKNOWN. Set incident_detected=false for genuinely normal activity.
Do not invent metrics, configuration values, or hidden scenario labels.
"""

# 調査段階の行動指示。実際の読み取り限定はグラフ側で渡すツールも制限して実現する。
INVESTIGATION_SYSTEM_PROMPT = """You are a JBoss incident investigator.
You have READ-ONLY JBoss tools only. Use tool evidence before concluding.
Prefer a few relevant tool calls over querying everything blindly. Never ask for or
invent write operations. When evidence is sufficient, respond without more tool calls.
"""

# 承認者が読む説明は日本語で生成し、処理に使う識別子は変えない。
DIAGNOSIS_INSTRUCTIONS = """Produce a structured JBoss incident diagnosis using only the
initial logs and read-only tool evidence. If evidence does not justify a safe action,
use action type NONE. Do not fabricate current_value, proposed_value, or deployment_name.
Use exactly one root_cause code when supported: THREAD_POOL_CONFIGURATION,
DATASOURCE_POOL_EXHAUSTION, DEPLOYMENT_FAILURE, UNKNOWN.
For a clearly observed recent configuration regression, prefer restoring the previous value.
Write reason and recommended_action.rationale in Japanese for the human approval screen.
Keep root_cause and action type codes, tool names, and deployment_name unchanged.
"""


def log_classification_prompt(log_text: str) -> str:
    """ログ差分を分類指示に付加し、監視用分類器に渡す文字列を返す。"""
    return f"{LOG_CLASSIFICATION_PROMPT}\n\nLOG LINES:\n{log_text}"


def initial_investigation_messages(state: IncidentState) -> list[object]:
    """読み取り限定の調査指示と、障害 ID・対象サーバー・初期ログの会話を作る。

    監視の分類は手掛かりとして渡し、ツールで原因を調べるよう依頼する。
    初期ログが空なら、ログが与えられていないことを明示する。
    """
    logs = "\n".join(state.get("initial_log_lines", [])) or "(no initial logs supplied)"
    return [
        SystemMessage(content=INVESTIGATION_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Incident ID: {state['incident_id']}\n"
                f"Server: {state['server_id']}\n"
                f"Initial category hint: {state.get('category', 'UNKNOWN')}\n"
                f"Severity: {state.get('severity', 'UNKNOWN')}\n"
                f"Initial log evidence:\n{logs}\n\n"
                "Investigate the cause using the available read-only tools."
            )
        ),
    ]


def diagnosis_prompt(state: IncidentState) -> str:
    """初期ログと蓄積したツール根拠をまとめ、構造化診断用の入力文字列を返す。

    ツール名と応答内容を列挙し、安全な操作を裏付けられない場合は NONE を促す。
    人が読む提案理由は日本語、操作や原因の識別コードは固定の表記を要求する。
    """
    # 会話全体ではなく、状態に蓄積したツール名と応答を診断の根拠として提示する。
    evidence = (
        "\n".join(f"- {item.get('tool_name')}: {item.get('content')}" for item in state.get("evidence", []))
        or "- No tool evidence captured"
    )
    logs = "\n".join(state.get("initial_log_lines", [])) or "(none)"
    return (
        f"{DIAGNOSIS_INSTRUCTIONS}\n\n"
        f"Server: {state['server_id']}\n"
        f"Initial category: {state.get('category', 'UNKNOWN')}\n"
        f"Initial logs:\n{logs}\n\n"
        f"Read-only tool evidence:\n{evidence}"
    )
