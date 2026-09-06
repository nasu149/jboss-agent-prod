"""JBoss 障害検知結果を Microsoft Teams へ通知する小さな連携モジュール。"""

from __future__ import annotations

from typing import Any

import httpx

from jboss_agent.config import Settings


def _payload(server_id: str, category: str, summary: str) -> dict[str, str]:
    """Teams Webhook に送る最小の text payload を作る。"""
    text = (
        "[JBoss Incident Detected]\n"
        f"Server: {server_id}\n"
        f"Category: {category}\n"
        f"Summary: {summary}"
    )
    return {"text": text}


def send_teams_alert(
    settings: Settings,
    *,
    server_id: str,
    category: str,
    summary: str,
) -> dict[str, Any]:
    """障害分類結果を Teams Webhook へ送る。

    ``TEAMS_DRY_RUN=true`` の場合はネットワーク送信せず、送る予定の payload を返す。
    dry-run を無効にした状態で URL が未設定なら失敗結果を返す。
    """
    payload = _payload(server_id, category, summary)

    if settings.teams_dry_run:
        return {"success": True, "status": "dry_run", "payload": payload}

    if not settings.teams_webhook_url.strip():
        return {"success": False, "status": "missing_webhook_url", "payload": payload}

    response = httpx.post(settings.teams_webhook_url, json=payload, timeout=10.0)
    response.raise_for_status()
    return {
        "success": True,
        "status": "sent",
        "status_code": response.status_code,
        "payload": payload,
    }
