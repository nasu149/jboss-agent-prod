"""Deterministic safety policy for LLM-proposed remediation actions."""

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
        raise ValueError(f"{key} must be an integer")
    return value


def evaluate_action(action: Mapping[str, Any] | None) -> ActionPolicyResult:
    """Validate an untrusted LLM proposal in ordinary Python."""
    if not action:
        return ActionPolicyResult(False, "BLOCKED", {}, "No remediation action was proposed.")

    action_type = str(action.get("type", "")).strip().upper()
    normalized = dict(action)
    normalized["type"] = action_type

    if action_type == "NONE":
        return ActionPolicyResult(True, "LOW", normalized, "No write operation is required.")

    if action_type == "SET_THREAD_POOL_MAX_THREADS":
        try:
            value = _int_value(action, "proposed_value")
        except ValueError as exc:
            return ActionPolicyResult(False, "BLOCKED", normalized, str(exc))
        if not THREAD_POOL_MIN <= value <= THREAD_POOL_MAX:
            return ActionPolicyResult(False, "BLOCKED", normalized, "thread_pool max_threads must be 1-200")
        normalized["proposed_value"] = value
        return ActionPolicyResult(True, "MEDIUM", normalized, "Validated thread-pool configuration change.")

    if action_type == "SET_DATASOURCE_MAX_POOL_SIZE":
        try:
            value = _int_value(action, "proposed_value")
        except ValueError as exc:
            return ActionPolicyResult(False, "BLOCKED", normalized, str(exc))
        if not DATASOURCE_POOL_MIN <= value <= DATASOURCE_POOL_MAX:
            return ActionPolicyResult(False, "BLOCKED", normalized, "datasource max_pool_size must be 1-200")
        normalized["proposed_value"] = value
        return ActionPolicyResult(True, "MEDIUM", normalized, "Validated datasource configuration change.")

    if action_type == "RESTART_DEPLOYMENT":
        name = str(action.get("deployment_name", "")).strip()
        if not name:
            return ActionPolicyResult(False, "BLOCKED", normalized, "deployment_name is required")
        normalized["deployment_name"] = name
        return ActionPolicyResult(True, "HIGH", normalized, "Deployment restart requires approval.")

    if action_type == "RELOAD_SERVER":
        return ActionPolicyResult(True, "HIGH", normalized, "Server reload requires approval.")

    return ActionPolicyResult(False, "BLOCKED", normalized, f"Unknown or out-of-policy action: {action_type or '<blank>'}")
