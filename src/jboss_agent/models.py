"""LLM の構造化出力と承認入力を表す Pydantic モデルを定義する。

分類名・判断値の選択肢や確信度の範囲など、データ形式の制約をここで定める。
操作別の値の範囲と実行可否は policy.py、承認と実行の順序は障害対応グラフが扱う。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 監視の分類コードと復旧操作コードを列挙し、構造化出力の選択肢を限定する。
IncidentCategory = Literal["NORMAL", "THREAD_POOL", "DATASOURCE_POOL", "DEPLOYMENT", "UNKNOWN"]
RemediationActionType = Literal[
    "NONE",
    "SET_THREAD_POOL_MAX_THREADS",
    "SET_DATASOURCE_MAX_POOL_SIZE",
    "RESTART_DEPLOYMENT",
    "RELOAD_SERVER",
]


class LogClassification(BaseModel):
    """ログ差分から得た障害検知結果・分類・確信度・要約・根拠。

    確信度は 0〜1、要約は1文字以上。evidence は省略時に空リストとする。
    分類と incident_detected の整合性を検証する追加ルールは設けていない。
    """
    incident_detected: bool
    category: IncidentCategory
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)
    # インスタンスごとに新しいリストを作り、別の分類結果と根拠リストを共有しない。
    evidence: list[str] = Field(default_factory=list)


class RemediationAction(BaseModel):
    """診断モデルが提案する復旧操作と、その理由。

    数値設定では current_value と proposed_value、アプリ再起動では deployment_name を
    使用する。各項目は型としては省略可能で、操作に必要な値の確認は後段が担当する。
    このモデルに適合するだけで、操作が許可・承認されたことにはならない。
    """
    type: RemediationActionType
    # 操作に応じて使う引数。現在値が実際のサーバー値と一致するかはこの型では検証しない。
    current_value: int | None = None
    proposed_value: int | None = None
    deployment_name: str | None = None
    # 承認画面向けの説明文。日本語での生成は診断プロンプトで指定する。
    rationale: str = Field(min_length=1)


class IncidentDiagnosis(BaseModel):
    """根本原因、確信度、判断理由、および1つの推奨操作をまとめた診断。

    root_cause は空でない文字列であり、列挙型ではない。原因コードの指定は
    プロンプトで促し、デモの答え合わせでは別途表記を正規化する。
    """
    root_cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    recommended_action: RemediationAction


class ApprovalResponse(BaseModel):
    """承認待ちグラフを再開するときに受け取る、人の判断と任意の編集値。

    decision は承認・拒否・編集して承認の3種類。編集時の proposed_value の必須確認と
    安全性評価は承認ノード側で行う。
    """
    decision: Literal["approve", "reject", "edit_and_approve"]
    proposed_value: int | None = None
