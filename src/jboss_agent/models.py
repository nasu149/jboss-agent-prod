"""LLM の構造化出力と、各処理で共有するデータ型を定義する。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


IncidentCategory = Literal["NORMAL", "THREAD_POOL", "DATASOURCE_POOL", "DEPLOYMENT", "UNKNOWN"]
RemediationActionType = Literal[
    "NONE",
    "SET_THREAD_POOL_MAX_THREADS",
    "SET_DATASOURCE_MAX_POOL_SIZE",
    "RESTART_DEPLOYMENT",
    "RELOAD_SERVER",
]


class LogClassification(BaseModel):
    incident_detected: bool
    category: IncidentCategory
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


class RemediationAction(BaseModel):
    type: RemediationActionType
    current_value: int | None = None
    proposed_value: int | None = None
    deployment_name: str | None = None
    rationale: str = Field(min_length=1)


class IncidentDiagnosis(BaseModel):
    root_cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    recommended_action: RemediationAction


class ApprovalResponse(BaseModel):
    decision: Literal["approve", "reject", "edit_and_approve"]
    proposed_value: int | None = None
