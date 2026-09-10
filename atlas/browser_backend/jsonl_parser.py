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


# ---------------------------------------------------------------------------
# Event-aware completion extraction (result-handoff boundary).
#
# The final assistant *text* is not the authoritative business result. A
# ``task_complete`` tool call carries the machine-readable Atlas object in its
# ``arguments.summary`` and echoes it through several decoded event shapes. This
# section decodes those shapes, extracts the object from each, de-duplicates the
# same object repeated across events, and reports conflicts — without ever
# trusting ``task_complete`` as proof that a job is valid (deterministic
# validation still runs downstream).
# ---------------------------------------------------------------------------

PARSER_VERSION = 2

TASK_COMPLETE_TOOL = "task_complete"

# §8 incremental completion states.
STATE_NO_EVIDENCE = "NO_EVIDENCE"
STATE_BROWSER_EVIDENCE_CAPTURED = "BROWSER_EVIDENCE_CAPTURED"
STATE_PROPOSAL_EMITTED = "PROPOSAL_EMITTED_IN_TASK_COMPLETE"
STATE_VALIDATED = "VALIDATED"
STATE_EVIDENCE_UNFINALIZED = "EVIDENCE_CAPTURED_UNFINALIZED"
STATE_CONFLICTING = "CONFLICTING_RESULTS"


def _event_type(ev: dict) -> str:
    return str(ev.get("type") or ev.get("role") or ev.get("event") or "").lower()


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _texts_from_value(value: Any) -> list[str]:
    """Collect plain-text payloads from a str / list / dict content value."""
    out: list[str] = []
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(_texts_from_value(item))
    elif isinstance(value, dict):
        for k in ("text", "content", "summary", "detailedContent", "value"):
            v = value.get(k)
            if isinstance(v, (str, list, dict)):
                out.extend(_texts_from_value(v))
    return out


def completion_text_candidates(events: list[dict]) -> list[dict]:
    """Return ordered decoded text candidates that may embed the Atlas object.

    Each candidate is ``{"source", "text", "event_index", "event_id",
    "timestamp", "success", "tool_call_id"}``. Sources cover the authoritative
    decoded completion shapes (A-F in the patch spec):

    * ``assistant.message`` content / assembled deltas;
    * ``assistant.message.toolRequests[task_complete].arguments.summary``;
    * ``tool.execution_start[task_complete].arguments.summary``;
    * successful ``tool.execution_complete[task_complete].result.*``;
    * ``session.task_complete.summary``;
    * a future top-level ``result`` event's text.
    """
    out: list[dict] = []
    # Track task_complete tool-call ids so we can match execution_complete events
    # (which carry only the call id, not the tool name).
    tc_call_ids: set[str] = set()

    def add(source: str, text: str, ev: dict, idx: int, *,
            success: Optional[bool] = None, call_id: str = "") -> None:
        if isinstance(text, str) and text.strip():
            out.append({
                "source": source,
                "text": text,
                "event_index": idx,
                "event_id": str(ev.get("id") or ev.get("event_id") or ""),
                "timestamp": str(ev.get("timestamp") or ev.get("ts")
                                  or (ev.get("data") or {}).get("timestamp") or ""),
                "success": success,
                "tool_call_id": call_id,
            })

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        t = _event_type(ev)
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

        # (A) assistant message content.
        if t.endswith("assistant.message"):
            content = data.get("content") or data.get("text") or _extract_text(ev)
            if isinstance(content, str):
                add("assistant.message", content, ev, idx)
            # (B) tool requests embedded on the assistant message.
            for tr in (data.get("toolRequests") or []):
                if isinstance(tr, dict) and tr.get("name") == TASK_COMPLETE_TOOL:
                    cid = str(tr.get("toolCallId") or tr.get("id") or "")
                    if cid:
                        tc_call_ids.add(cid)
                    summ = (tr.get("arguments") or {}).get("summary")
                    add("assistant.message.toolRequest", summ, ev, idx, call_id=cid)

        # (C) tool.execution_start for task_complete.
        elif t.endswith("tool.execution_start"):
            if data.get("toolName") == TASK_COMPLETE_TOOL or data.get("name") == TASK_COMPLETE_TOOL:
                cid = str(data.get("toolCallId") or "")
                if cid:
                    tc_call_ids.add(cid)
                summ = (data.get("arguments") or {}).get("summary")
                add("tool.execution_start", summ, ev, idx, call_id=cid)

        # (D) successful tool.execution_complete for task_complete. The complete
        # event often carries only the call id (no toolName); match it to a known
        # task_complete call, or fall back to a result that embeds an Atlas object.
        elif t.endswith("tool.execution_complete"):
            cid = str(data.get("toolCallId") or "")
            name = data.get("toolName") or data.get("name")
            is_tc = name == TASK_COMPLETE_TOOL or (cid and cid in tc_call_ids)
            success = bool(data.get("success", True))
            res = data.get("result")
            for txt in _texts_from_value(res):
                if is_tc or "atlas_result_version" in txt or ('"company"' in txt and '"status"' in txt):
                    add("tool.execution_complete", txt, ev, idx,
                        success=success, call_id=cid)

        # (E) session.task_complete summary.
        elif t.endswith("session.task_complete"):
            add("session.task_complete", data.get("summary"), ev, idx,
                success=bool(data.get("success", True)))

        # (F) a future direct result event.
        elif t == "result":
            for txt in _texts_from_value(data or ev.get("result") or ev.get("content")):
                add("result", txt, ev, idx)

    return out


def extract_result_objects_from_events(events: list[dict]) -> dict:
    """Event-aware Atlas-object extraction across all completion sources.

    Returns::

        {
          "objects": [<distinct Atlas objects, dedup by canonical form>],
          "provenance": {<canonical>: [<candidate source records>]},
          "primary": <the single agreed object or None>,
          "conflict": <bool: two or more distinct objects>,
          "candidates": <all completion_text_candidates>,
        }

    De-duplication collapses the identical object repeated across events. Two
    genuinely distinct objects are a contract violation: ``conflict`` is set and
    ``primary`` is ``None`` (the caller must not pick one arbitrarily).
    """
    candidates = completion_text_candidates(events)
    provenance: dict[str, list[dict]] = {}
    order: list[str] = []
    for cand in candidates:
        for raw_obj in extract_result_objects(cand["text"]):
            key = _canonical(raw_obj)
            if key not in provenance:
                provenance[key] = []
                order.append(key)
            provenance[key].append({
                "source": cand["source"],
                "event_index": cand["event_index"],
                "event_id": cand["event_id"],
                "timestamp": cand["timestamp"],
                "success": cand["success"],
                "tool_call_id": cand["tool_call_id"],
            })
    objects = [json.loads(k) for k in order]
    conflict = len(objects) > 1
    primary: Optional[dict] = None
    if len(objects) == 1:
        primary = objects[0]
    return {
        "objects": objects,
        "provenance": provenance,
        "primary": primary,
        "conflict": conflict,
        "candidates": candidates,
    }


def task_completion_records(events: list[dict]) -> list[dict]:
    """Return one record per decoded ``task_complete`` completion signal.

    Preserves event index / id / timestamp / success and whether an Atlas object
    was embedded, for provenance and audit. ``task_complete`` being present is
    never treated as proof a job is valid.
    """
    records: list[dict] = []
    for cand in completion_text_candidates(events):
        if cand["source"] in ("assistant.message", "result"):
            # These are generic text channels; only include when they carry an
            # Atlas object (a real completion signal, not narration).
            if not extract_result_objects(cand["text"]):
                continue
        objs = extract_result_objects(cand["text"])
        records.append({
            "source": cand["source"],
            "event_index": cand["event_index"],
            "event_id": cand["event_id"],
            "timestamp": cand["timestamp"],
            "success": cand["success"],
            "tool_call_id": cand["tool_call_id"],
            "has_result_object": bool(objs),
            "result_object_count": len(objs),
        })
    return records


def _has_browser_evidence(events: list[dict]) -> bool:
    """True when any tool execution used a Playwright/MCP/browser tool.

    Robust to the Copilot schema where the tool name lives in ``data.toolName``
    (which the generic :func:`count_tool_calls` does not inspect)."""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = _event_type(ev)
        if "tool" not in t:
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        name = str(data.get("toolName") or data.get("name")
                   or ev.get("name") or ev.get("tool") or "").lower()
        if name.startswith("playwright") or "browser" in name or "mcp" in name or name.startswith("page_"):
            return True
    return count_tool_calls(events, mcp_only=True) > 0


def classify_completion_state(events: list[dict], *, validated: bool = False) -> str:
    """Classify the incremental completion state (§8) from the event log."""
    extraction = extract_result_objects_from_events(events)
    if extraction["conflict"]:
        return STATE_CONFLICTING
    has_browser = _has_browser_evidence(events)
    if extraction["primary"] is not None:
        if validated:
            return STATE_VALIDATED
        return STATE_PROPOSAL_EMITTED
    if has_browser:
        # Tool evidence exists but no finalized proposal object was emitted.
        return STATE_EVIDENCE_UNFINALIZED
    return STATE_NO_EVIDENCE


__all__ = [
    "iter_jsonl", "load_events", "final_assistant_text", "count_tool_calls",
    "session_ids", "extract_result_objects", "PARSER_VERSION", "TASK_COMPLETE_TOOL",
    "completion_text_candidates", "extract_result_objects_from_events",
    "task_completion_records", "classify_completion_state",
    "STATE_NO_EVIDENCE", "STATE_BROWSER_EVIDENCE_CAPTURED", "STATE_PROPOSAL_EMITTED",
    "STATE_VALIDATED", "STATE_EVIDENCE_UNFINALIZED", "STATE_CONFLICTING",
]
