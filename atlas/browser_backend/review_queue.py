"""Internal browser/process failure review queue (VS Code repair fallback).

Only *internal* failures (browser/process/parser faults, unresolved internal
retryable statuses) are appended. Externally-blocked companies and clean
no-match companies are terminal and must NOT enter the repair queue.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from atlas.pilot.status_v4 import is_internal_retryable

DEFAULT_QUEUE_PATH = Path("output") / "production" / "browser_review_queue.jsonl"

_REPAIR_PROMPT = ".github/prompts/atlas-repair-company.prompt.md"


def should_enqueue(status: str, *, error: str = "") -> bool:
    """Internal/retryable statuses (or a hard internal error) are enqueued;
    external blocks and clean searches are not."""
    if status and is_internal_retryable(status):
        return True
    if not status and error:
        return True
    return False


def append_review_item(
    result,
    *,
    queue_path: Optional[Path] = None,
    error: str = "",
    last_state: str = "",
    transcript_path: str = "",
    usage_path: str = "",
    mcp_output_dir: str = "",
    retry_count: int = 0,
) -> Optional[Path]:
    """Append one review row for an internal failure. Returns the queue path, or
    ``None`` when the outcome is not internally-repairable."""
    status = getattr(result, "status", "")
    if not should_enqueue(status, error=error):
        return None
    task = getattr(result, "task", None)
    company = getattr(task, "company", "") if task else getattr(result, "company", "")
    row = {
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": getattr(task, "run_id", "") if task else "",
        "task_id": getattr(task, "task_id", "") if task else "",
        "company": company,
        "official_url": getattr(result, "career_entry_url", "")
        or (getattr(task, "career_entry_url", "") if task else ""),
        "last_known_state": last_state or status,
        "internal_error": error or getattr(result, "error", ""),
        "transcript_path": transcript_path,
        "usage_path": usage_path,
        "mcp_output_dir": mcp_output_dir,
        "suggested_vscode_repair": {
            "prompt": _REPAIR_PROMPT,
            "command": f"/atlas-repair-company company={company!r}",
        },
        "retry_count": int(retry_count),
    }
    path = Path(queue_path) if queue_path else DEFAULT_QUEUE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_queue(queue_path: Optional[Path] = None) -> list[dict]:
    path = Path(queue_path) if queue_path else DEFAULT_QUEUE_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return out


__all__ = ["DEFAULT_QUEUE_PATH", "should_enqueue", "append_review_item", "read_queue"]
