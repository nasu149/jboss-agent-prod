"""Normalize MCP/LangChain tool outputs into ordinary Python dictionaries.

MCP tools may surface their result as a dict, JSON text, LangChain standard
content blocks, or a ToolMessage carrying structured content. The application
logic should not care which transport representation was used.
"""

from __future__ import annotations

import json
from typing import Any


def normalize_tool_result(value: Any) -> dict[str, Any]:
    """Return a machine-readable dict from common MCP/LangChain result shapes."""
    if isinstance(value, dict):
        structured = value.get("structured_content")
        if isinstance(structured, dict):
            return structured

        # Standard LangChain/MCP text content block.
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return normalize_tool_result(value["text"])

        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    # langchain-mcp-adapters can return a list of standard content blocks when
    # a tool is invoked directly with args instead of through ToolNode.
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

    # ToolMessage / similar objects may expose structured content as artifact.
    artifact = getattr(value, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        if isinstance(structured, dict):
            return structured

    content = getattr(value, "content", None)
    if content is not None:
        return normalize_tool_result(content)

    return {"raw": value}
