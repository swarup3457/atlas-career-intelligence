"""Parse the Copilot-CLI JSONL event stream.

Copilot CLI (``--log-*``/stream JSON) emits one JSON object per line. This module
extracts, defensively:

* the final assistant text (last assistant/message event with text);
* the single machine-readable Atlas result object embedded in it;
* usage / AI-credit totals;
* tool-call and MCP-tool evidence counts;
* the session id.

Everything degrades safely: malformed lines are skipped, and a missing result is
reported as ``None`` rather than raising.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_ASSISTANT_TYPES = {"assistant", "assistant_message", "message", "agent_message", "response"}
_TEXT_KEYS = ("text", "content", "message", "delta")


def iter_jsonl(raw: str):
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (ValueError, TypeError):
            continue


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [e for e in iter_jsonl(path.read_text(encoding="utf-8", errors="replace")) if isinstance(e, dict)]


def _event_role(ev: dict) -> str:
    return str(ev.get("role") or ev.get("type") or ev.get("event") or "").lower()


def _extract_text(ev: dict) -> str:
    for key in _TEXT_KEYS:
        v = ev.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            parts = []
            for item in v:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    t = item.get("text") or item.get("content")
                    if isinstance(t, str):
                        parts.append(t)
            if parts:
                return "\n".join(parts)
        if isinstance(v, dict):
            t = v.get("text") or v.get("content")
            if isinstance(t, str) and t.strip():
                return t
    return ""


def final_assistant_text(events: list[dict]) -> str:
    """Return the text of the final assistant message.

    Handles the Copilot ``--output-format json`` schema
    (``type: "assistant.message"`` with ``data.content``, and streamed
    ``type: "assistant.message_delta"`` with ``data.deltaContent`` keyed by
    ``data.messageId``), then falls back to a generic role/text scan.
    """
    full_texts: list[str] = []
    delta_order: list[str] = []
    deltas: dict[str, list[str]] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = str(ev.get("type") or ev.get("role") or ev.get("event") or "").lower()
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if t.endswith("assistant.message") or t == "assistant.message":
            txt = data.get("content") or data.get("text") or _extract_text(ev)
            if isinstance(txt, str) and txt.strip():
                full_texts.append(txt)
        elif t.endswith("message_delta"):
            mid = str(data.get("messageId") or "default")
            dc = data.get("deltaContent") or data.get("content") or ""
            if isinstance(dc, str) and dc:
                if mid not in deltas:
                    deltas[mid] = []
                    delta_order.append(mid)
                deltas[mid].append(dc)
    if full_texts:
        return full_texts[-1]
    if delta_order:
        return "".join(deltas[delta_order[-1]])

    # Generic fallback (other shapes / non-Copilot JSONL).
    for ev in reversed(events):
        if isinstance(ev, dict) and _event_role(ev) in _ASSISTANT_TYPES:
            text = _extract_text(ev)
            if text.strip():
                return text
    for ev in reversed(events):
        if isinstance(ev, dict):
            text = _extract_text(ev)
            if text.strip():
                return text
    return ""


def count_tool_calls(events: list[dict], *, mcp_only: bool = False) -> int:
    n = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        role = _event_role(ev)
        name = str(ev.get("name") or ev.get("tool") or ev.get("tool_name") or "")
        is_tool = "tool" in role or bool(name)
        if not is_tool:
            continue
        if mcp_only and not (name.startswith("playwright") or "mcp" in role or "browser" in name.lower()):
            continue
        n += 1
    return n


def session_ids(events: list[dict]) -> list[str]:
    out: list[str] = []
    for ev in events:
        if isinstance(ev, dict):
            sid = ev.get("session_id") or ev.get("sessionId") or ev.get("api_call_id")
            if sid and str(sid) not in out:
                out.append(str(sid))
    return out


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_MARKER = re.compile(r"ATLAS_RESULT\s*[:=]?\s*(\{.*)$", re.DOTALL)


def _balanced_objects(text: str) -> list[str]:
    """Return every top-level balanced ``{...}`` block in ``text``."""
    blocks: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    blocks.append(text[start:i + 1])
                    start = -1
    return blocks


def extract_result_objects(text: str) -> list[dict]:
    """Extract candidate Atlas result objects from the final assistant text.

    Only objects that look like an Atlas result (carry ``atlas_result_version``
    or both ``company`` and ``status``) are returned. Returning more than one is
    itself a contract violation the validator rejects.
    """
    candidates: list[str] = []
    m = _MARKER.search(text)
    if m:
        candidates += _balanced_objects(m.group(1))
    candidates += [b.strip() for b in _FENCE.findall(text)]
    candidates += _balanced_objects(text)

    seen: set[str] = set()
    results: list[dict] = []
    for raw in candidates:
        key = raw.strip()
        if key in seen:
            continue
        seen.add(key)
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if "atlas_result_version" in obj or ("company" in obj and "status" in obj):
            results.append(obj)
    return results


__all__ = [
    "iter_jsonl", "load_events", "final_assistant_text", "count_tool_calls",
    "session_ids", "extract_result_objects",
]
