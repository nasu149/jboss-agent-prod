"""監視と障害対応の各ノードが共有する LangGraph の状態スキーマ。

TypedDict はデータの形を型として表し、実行時の値の検証は行わない。total=False の
ため、途中段階でまだないキーを許容する。各ノードは更新するキーだけを返し、
reducer のないキーは上書きされる。チェックポイントが実行途中の状態を保存する。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import add_messages

from jboss_agent.models import IncidentCategory


class MonitoringState(TypedDict, total=False):
    """同じ監視スレッドで繰り返し使う、ログ差分と分類・通知結果の状態。

    previous_log_cursor は次回読み始めるバイト位置で、サイクル末尾に更新される。
    scan_from_cursor と current_log_cursor は今回実際に読んだ区間の開始・末尾を表す。
    """
    server_id: str
    # ログ位置はバイト単位。ログが縮小した場合は先頭から読み直した事実も保持する。
    previous_log_cursor: int
    scan_from_cursor: int
    current_log_cursor: int
    cursor_reset_detected: bool
    # 今回のログ差分と、LLM に渡す結合済みテキスト。
    new_log_lines: list[str]
    log_text: str
    has_new_logs: bool
    # ログ分類の判断と根拠。障害なしの場合、incident_id は None。
    incident_detected: bool
    category: IncidentCategory
    confidence: float
    summary: str
    evidence: list[str]
    incident_id: str | None
    severity: str
    # 通知ツールの実行結果と、この監視サイクルで通過したノード。
    teams_notified: bool
    teams_tool_status: str | None
    node_trace: list[str]


class IncidentState(TypedDict, total=False):
    """障害ごとの調査履歴、診断、承認、復旧結果を保持する状態。

    messages だけは add_messages で統合する。evidence と node_trace は自動追記
    されないため、追加するノードが既存のリストを含めて返す必要がある。
    承認待ちではこの状態を保存し、同じ障害の thread_id で再開する。
    """
    # add_messages が会話履歴を統合するため、各ノードは新しいメッセージだけを返せる。
    # 既存のメッセージは保持し、同じ ID のメッセージは更新する。
    messages: Annotated[list[Any], add_messages]
    incident_id: str
    server_id: str
    category: str
    severity: str
    confidence: float
    # 監視から引き継いだ初期ログと、調査・復旧確認で蓄積するツール根拠。
    initial_log_lines: list[str]
    evidence: list[dict[str, Any]]
    # 調査モデルの呼び出し回数。再調査時にゼロへ戻し、ツール呼び出し数とは区別する。
    investigation_count: int
    # 診断と提案、および Python の安全ルールによる評価。再調査時はクリアする。
    diagnosis: dict[str, Any] | None
    proposed_action: dict[str, Any] | None
    risk_level: str | None
    policy_reason: str | None
    # 承認の判断結果と書き込み応答。実行応答を得ても、それだけでは復旧成功としない。
    approval_status: str | None
    execution_result: dict[str, Any] | None
    # 復旧判定は未判定なら None。NONE の提案では変更なしの正常完了として True にする。
    recovered: bool | None
    # 書き込み結果の取得ごとに加算する累計試行数。再調査でも引き継ぐ。
    recovery_attempts: int
    failure_reason: str | None
    node_trace: list[str]
