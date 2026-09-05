"""MCP / LangChain のツール出力を Python の辞書に正規化する。

辞書、JSON 文字列、標準コンテンツブロック、ToolMessage などの形式差を吸収し、
アプリケーション側が転送形式を意識せずに結果を扱えるようにする。
"""

from __future__ import annotations

import json
from typing import Any


def normalize_tool_result(value: Any) -> dict[str, Any]:
    """MCP / LangChain の代表的な結果形式を、処理しやすい辞書に変換する。"""
    if isinstance(value, dict):
        structured = value.get("structured_content")
        if isinstance(structured, dict):
            return structured

        # LangChain / MCP の標準テキストコンテンツブロックを処理する。
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return normalize_tool_result(value["text"])

        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    # ToolNode を介さず引数を渡して直接ツールを呼ぶと、
    # langchain-mcp-adapters はコンテンツブロックのリストを返すことがある。
    if isinstance(value, (list, tuple)):
        text_parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                structured = item.get("structured_content")
                if isinstance(structured, dict):
                    return structured
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif isinstance(item, str):
                text_parts.append(item)

        if text_parts:
            combined = "\n".join(text_parts)
            normalized = normalize_tool_result(combined)
            if "raw" not in normalized:
                return normalized

        return {"raw": value}

    # ToolMessage などでは、構造化データが artifact に入ることがある。
    artifact = getattr(value, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        if isinstance(structured, dict):
            return structured

    content = getattr(value, "content", None)
    if content is not None:
        return normalize_tool_result(content)

    return {"raw": value}
