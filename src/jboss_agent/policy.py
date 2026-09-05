"""LLM が提案した復旧操作を、決定的な安全ルールで検証する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "BLOCKED"]
THREAD_POOL_MIN, THREAD_POOL_MAX = 1, 200
DATASOURCE_POOL_MIN, DATASOURCE_POOL_MAX = 1, 200


@dataclass(frozen=True)
class ActionPolicyResult:
    allowed: bool
    risk: RiskLevel
    normalized_action: dict[str, Any]
    reason: str


def _int_value(action: Mapping[str, Any], key: str) -> int:
    value = action.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} は整数で指定してください。")
    return value


def evaluate_action(action: Mapping[str, Any] | None) -> ActionPolicyResult:
    """LLM の提案をそのまま信用せず、Python の条件分岐で検証する。"""
    if not action:
        return ActionPolicyResult(False, "BLOCKED", {}, "復旧操作が提案されていません。")

    action_type = str(action.get("type", "")).strip().upper()
    normalized = dict(action)
    normalized["type"] = action_type

    if action_type == "NONE":
        return ActionPolicyResult(True, "LOW", normalized, "書き込み操作は不要です。")

    if action_type == "SET_THREAD_POOL_MAX_THREADS":
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

    return ActionPolicyResult(
        False,
        "BLOCKED",
        normalized,
        f"不明な操作、または安全ルールで許可されていない操作です: {action_type or '<blank>'}",
    )
