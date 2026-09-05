"""監視の分類・障害の調査・診断に使う Gemini クライアントを生成する。

共通の接続設定に、用途ごとの出力スキーマまたは読み取りツールを組み合わせる。
ここではクライアントを構成し、実際の推論はグラフの各ノードが invoke で実行する。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from langchain_google_genai import ChatGoogleGenerativeAI

from jboss_agent.config import Settings
from jboss_agent.models import IncidentDiagnosis, LogClassification


class LogClassifier(Protocol):
    """監視グラフが要求する分類器のインターフェース。

    invoke を持つ本番クライアントやテスト用の代替実装を、同じ型として扱うための Protocol。
    """

    def invoke(self, input: str) -> object:  # noqa: A002
        """ログ分類用の入力文字列を受け取り、分類結果を返す実装を要求する。

        戻り値の型の確認と LogClassification による検証は、監視ノード側で行う。
        """
        ...


def build_gemini(settings: Settings) -> ChatGoogleGenerativeAI:
    """設定から、全用途で共通の Gemini チャットクライアントを作る。

    API キーが空なら RuntimeError。モデル名・温度は Settings に従い、
    タイムアウトは30秒、最大リトライ数は2に設定する。ここでは推論しない。
    """
    if not settings.has_google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured. Copy .env.example to .env and set your Gemini API key.")
    # キーの存在確認後だけ秘密値を取り出し、クライアントに渡す。
    assert settings.google_api_key is not None
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        api_key=settings.google_api_key.get_secret_value(),
        temperature=settings.gemini_temperature,
        timeout=30,
        max_retries=2,
    )


def build_log_classifier(settings: Settings) -> LogClassifier:
    """LogClassification の JSON Schema に沿った構造化出力を返す分類器を作る。

    スキーマは辞書として渡すため、受け取った結果のモデル検証は監視ノードで行う。
    """
    # モデルに JSON Schema を渡して出力の形を指定する。業務上の安全性は後段で検証する。
    return build_gemini(settings).with_structured_output(
        schema=LogClassification.model_json_schema(),
        method="json_schema",
    )


def build_investigator(settings: Settings, read_tools: Sequence[Any]):
    """渡された読み取りツールを呼び出せる調査用モデルを作る。

    read_tools の中身をここで検査するわけではないため、呼び出し元が読み取り専用の
    ツール集合を渡す。モデルの呼び出し要求の実行はグラフの ToolNode が担当する。
    """
    return build_gemini(settings).bind_tools(list(read_tools))


def build_diagnoser(settings: Settings):
    """原因・根拠・推奨操作を IncidentDiagnosis の JSON Schema で出力するモデルを作る。

    診断結果の検証と操作の安全性評価は後続ノードが行い、このモデルに書き込みは任せない。
    """
    # モデルに JSON Schema を渡して出力の形を指定する。業務上の安全性は後段で検証する。
    return build_gemini(settings).with_structured_output(
        schema=IncidentDiagnosis.model_json_schema(),
        method="json_schema",
    )
