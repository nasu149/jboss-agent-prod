from __future__ import annotations

import json
from types import SimpleNamespace

from jboss_agent.tool_results import normalize_tool_result


def test_normalize_direct_dict() -> None:
    value = {"lines": ["one"], "to_cursor": 10}
    assert normalize_tool_result(value) == value


def test_normalize_json_text() -> None:
    value = {"lines": ["one"], "to_cursor": 10}
    assert normalize_tool_result(json.dumps(value)) == value


def test_normalize_mcp_standard_text_content_block_list() -> None:
    value = {"lines": ["one"], "to_cursor": 10}
    mcp_result = [{"type": "text", "text": json.dumps(value)}]
    assert normalize_tool_result(mcp_result) == value


def test_normalize_tool_message_structured_artifact() -> None:
    value = {"status": "UP", "request_error_rate": 0.0}
    tool_message_like = SimpleNamespace(
        artifact={"structured_content": value},
        content="human-readable fallback",
    )
    assert normalize_tool_result(tool_message_like) == value
