"""LangGraph の実行状態を SQLite に永続化する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from jboss_agent.config import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[Any]:
    """ログカーソルと承認待ちの中断状態を SQLite に保存する。"""
    db_path = Path(settings.checkpoint_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        yield saver
