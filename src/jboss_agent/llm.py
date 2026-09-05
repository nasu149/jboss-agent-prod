"""2つの LangGraph ワークフローで使用する Gemini モデルを生成する。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from langchain_google_genai import ChatGoogleGenerativeAI

from jboss_agent.config import Settings
from jboss_agent.models import IncidentDiagnosis, LogClassification


class LogClassifier(Protocol):
    def invoke(self, input: str) -> object: ...  # noqa: A002


def build_gemini(settings: Settings) -> ChatGoogleGenerativeAI:
    if not settings.has_google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured. Copy .env.example to .env and set your Gemini API key.")
    assert settings.google_api_key is not None
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        api_key=settings.google_api_key.get_secret_value(),
        temperature=settings.gemini_temperature,
        timeout=30,
        max_retries=2,
    )


def build_log_classifier(settings: Settings) -> LogClassifier:
    return build_gemini(settings).with_structured_output(
        schema=LogClassification.model_json_schema(),
        method="json_schema",
    )


def build_investigator(settings: Settings, read_tools: Sequence[Any]):
    """LLM には読み取り専用ツールだけを渡す。"""
    return build_gemini(settings).bind_tools(list(read_tools))


def build_diagnoser(settings: Settings):
    return build_gemini(settings).with_structured_output(
        schema=IncidentDiagnosis.model_json_schema(),
        method="json_schema",
    )
