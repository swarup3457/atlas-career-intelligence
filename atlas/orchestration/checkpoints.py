"""Atlas LangGraph checkpoint helpers.

Thin wrapper around the already-proven ``langgraph-checkpoint-sqlite``
package so callers don't need to know the exact import path/API shape,
and so a future LangGraph version bump only needs to be adapted in one
place.

This module does NOT invent a custom checkpoint framework — it wraps the
official supported SqliteSaver, exactly as validated in
tests/langgraph_reliability_graph.py and tests/real_web_orchestration_graph.py.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


@contextlib.contextmanager
def open_checkpointer(db_path: Path) -> Iterator[SqliteSaver]:
    """Open the official SQLite-backed LangGraph checkpointer."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        yield checkpointer


def thread_config(thread_id: str) -> dict:
    """Build the LangGraph `config` dict for a given durable thread id."""
    return {"configurable": {"thread_id": thread_id}}
