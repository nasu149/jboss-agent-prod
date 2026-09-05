"""復旧提案の操作種別と引数を、LLM を使わない固定の安全ルールで検証する。

提案や人が編集した値を正規化して、許可可否・リスク・理由を返す。
サーバーの現在値の取得、復旧効果の保証、人の承認、操作の実行は行わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "BLOCKED"]
# 設定変更として許可する絶対範囲。現在値からの増減幅や実環境の容量は判定しない。
THREAD_POOL_MIN, THREAD_POOL_MAX = 1, 200
DATASOURCE_POOL_MIN, DATASOURCE_POOL_MAX = 1, 200


@dataclass(frozen=True)
class ActionPolicyResult:
    """安全ルールの評価結果と、後続処理へ渡す正規化済み操作。

    allowed はルール上の許可であり、人の承認済みという意味ではない。
    データクラスの属性は再代入不可だが、内包する辞書自体は変更可能。
    """
    allowed: bool
    risk: RiskLevel
    normalized_action: dict[str, Any]
    reason: str


def _int_value(action: Mapping[str, Any], key: str) -> int:
    """指定キーの値を厳密な整数として取り出し、欠損や他の型なら ValueError とする。

    Python では bool も int の一種なので明示的に除外し、文字列や小数の変換はしない。
    """
    value = action.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} は整数で指定してください。")
    return value


def evaluate_action(action: Mapping[str, Any] | None) -> ActionPolicyResult:
    """操作を正規化・検証し、許可可否とリスク、画面に表示できる日本語の理由を返す。

    NONE は LOW、範囲内のプール変更は MEDIUM、再起動・再読み込みは HIGH とする。
    未提案・不正な数値・空の対象名・未知の操作は BLOCKED。入力辞書は直接変更しない。
    アプリ名は文字列化して空かを確認するだけで、実在の確認は実行先に任せる。
    """
    if not action:
        return ActionPolicyResult(False, "BLOCKED", {}, "復旧操作が提案されていません。")

    # 呼び出し元の辞書を残したまま、操作コードの前後空白と大小文字を揃える。
    action_type = str(action.get("type", "")).strip().upper()
    normalized = dict(action)
    normalized["type"] = action_type

    # 変更なしだけを LOW とする。許可された書き込みも、グラフ側で別途人の承認を待つ。
    if action_type == "NONE":
        return ActionPolicyResult(True, "LOW", normalized, "書き込み操作は不要です。")

    if action_type == "SET_THREAD_POOL_MAX_THREADS":
        # 型エラーも例外のまま返さず、承認画面に表示できる BLOCKED の理由へ変換する。
        try:
            value = _int_value(action, "proposed_value")
        except ValueError as exc:
            return ActionPolicyResult(False, "BLOCKED", normalized, str(exc))
        if not THREAD_POOL_MIN <= value <= THREAD_POOL_MAX:
            return ActionPolicyResult(
                False, "BLOCKED", normalized, "最大スレッド数は 1〜200 で指定してください。"
            )
        normalized["proposed_value"] = value
        return ActionPolicyResult(
            True, "MEDIUM", normalized, "スレッドプールの設定変更は安全ルールの範囲内です。"
        )

    if action_type == "SET_DATASOURCE_MAX_POOL_SIZE":
        # 型エラーも例外のまま返さず、承認画面に表示できる BLOCKED の理由へ変換する。
        try:
            value = _int_value(action, "proposed_value")
        except ValueError as exc:
            return ActionPolicyResult(False, "BLOCKED", normalized, str(exc))
        if not DATASOURCE_POOL_MIN <= value <= DATASOURCE_POOL_MAX:
            return ActionPolicyResult(
                False, "BLOCKED", normalized, "データソースの最大接続数は 1〜200 で指定してください。"
            )
        normalized["proposed_value"] = value
        return ActionPolicyResult(
            True, "MEDIUM", normalized, "データソースの設定変更は安全ルールの範囲内です。"
        )

    # ここでは対象名の文字列化と空チェックまで行い、存在確認は Fake JBoss 側で行う。
    if action_type == "RESTART_DEPLOYMENT":
        name = str(action.get("deployment_name", "")).strip()
        if not name:
            return ActionPolicyResult(
                False, "BLOCKED", normalized, "対象アプリケーション名を指定してください。"
            )
        normalized["deployment_name"] = name
        return ActionPolicyResult(True, "HIGH", normalized, "アプリケーションの再起動には承認が必要です。")

    if action_type == "RELOAD_SERVER":
        return ActionPolicyResult(True, "HIGH", normalized, "サーバーの再読み込みには承認が必要です。")

    # 既知の許可分岐に一致しない操作は、実行不可として閉じる。
    return ActionPolicyResult(
        False,
        "BLOCKED",
        normalized,
        f"不明な操作、または安全ルールで許可されていない操作です: {action_type or '<blank>'}",
    )
