"""Local Teams notification tool. This integration intentionally is not MCP."""

from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from jboss_agent.config import get_settings


logger = logging.getLogger(__name__)
Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
_delivery_lock = Lock()
_delivered_incidents: set[str] = set()


class TeamsAlert(BaseModel):
    server_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    severity: Severity
    category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)


def _message(alert: TeamsAlert) -> str:
    return (
        "[JBoss Incident Detected]\n"
        f"Server: {alert.server_id}\n"
        f"Severity: {alert.severity}\n"
        f"Category: {alert.category}\n"
        f"Confidence: {alert.confidence:.0%}\n"
        f"Summary: {alert.summary}\n"
        f"Incident ID: {alert.incident_id}"
    )


@tool(args_schema=TeamsAlert)
def send_teams_alert(
    server_id: str,
    incident_id: str,
    severity: Severity,
    category: str,
    confidence: float,
    summary: str,
) -> str:
    """Send one idempotent Teams incident notification."""
    alert = TeamsAlert(
        server_id=server_id,
        incident_id=incident_id,
        severity=severity,
        category=category,
        confidence=confidence,
        summary=summary,
    )
    settings = get_settings()

    with _delivery_lock:
        if incident_id in _delivered_incidents:
            return json.dumps({"success": True, "status": "duplicate_skipped"})

    payload = {"text": _message(alert)}
    if settings.teams_dry_run:
        logger.info("TEAMS_DRY_RUN payload=%s", json.dumps(payload, ensure_ascii=False))
        status = "dry_run"
    else:
        if not settings.teams_webhook_url:
            return json.dumps({"success": False, "status": "missing_webhook_url"})
        with httpx.Client(timeout=10.0) as client:
            response = client.post(settings.teams_webhook_url, json=payload)
            response.raise_for_status()
        status = "sent"

    with _delivery_lock:
        _delivered_incidents.add(incident_id)
    return json.dumps({"success": True, "status": status, "payload": payload}, ensure_ascii=False)
