"""Parse one Claude transcript JSONL line into dashboard session events.

Extracted from serve.py. ``_jsonl_line_to_session_events`` turns a single
transcript record (user / assistant / tool_use / tool_result / result / system)
into the flat ``{type, ...}`` event list the chat pane renders, splitting
assistant content blocks (text / thinking / tool_use) into separate events and
tolerating malformed / non-conversation lines (returns ``[]``). Pure (stdlib
json only); serve.py re-exports it via a shim.
"""
from __future__ import annotations

import json


def _jsonl_line_to_session_events(line: str) -> "list[dict]":
    """Normalize one JSONL line from a Claude transcript into SessionEvent dicts.

    Returns a list (possibly empty) of events WITHOUT a ``seq`` field — the
    caller assigns ``seq`` per emitted event, since one transcript line can
    expand into several events (an assistant turn carries text + one or more
    tool_use blocks; a user turn carries tool_result blocks). Emitting one
    event per block — rather than collapsing the whole line into a single
    event — is what lets the canvas render a tool_use pill with its real name
    and input, and attach each tool_result to the pill it belongs to.

    SessionEvent schema (seq added by caller):
      message:     {"kind":"message","role":str,"text":str,"partial":False,"state":None}
      tool_use:    {"kind":"tool_use","role":str,"id":str,"name":str,"input":dict,"text":str}
      tool_result: {"kind":"tool_result","role":str,"tool_use_id":str,"is_error":bool,"content":str,"text":str}
      thinking:    {"kind":"thinking","role":str,"text":str,"partial":False,"state":None}
      system:      {"kind":"system","role":"system","text":str,"partial":False,"state":None}
    """
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []

    msg_type = obj.get("type")
    message = obj.get("message") or {}
    role = message.get("role") or obj.get("role")

    # user / assistant message lines
    if msg_type in ("user", "assistant"):
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
            if not text:
                return []
            return [{
                "kind": "message", "role": role or msg_type, "text": content,
                "partial": False, "state": None,
            }]
        if not isinstance(content, list):
            return []
        # One event per block, in transcript order: text -> message, tool_use ->
        # tool_use (with id/name/input so the pill renders), tool_result ->
        # tool_result (with tool_use_id so it binds to the pill).
        events: "list[dict]" = []
        text_buf: "list[str]" = []

        def _flush_text():
            joined = "".join(text_buf)
            text_buf.clear()
            if joined.strip():
                events.append({
                    "kind": "message", "role": role or msg_type, "text": joined,
                    "partial": False, "state": None,
                })

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_buf.append(block.get("text") or "")
            elif btype == "thinking":
                # Chain-of-thought. Flush any buffered text first so the
                # thought lands in transcript order (thinking precedes the
                # answer), then emit it as its own event. The client renders
                # it as a collapsed <details> inside the assistant bubble, so
                # long monologues don't drown the answer but stay inspectable.
                _flush_text()
                thought = block.get("thinking")
                if isinstance(thought, str) and thought.strip():
                    events.append({
                        "kind": "thinking", "role": role or msg_type,
                        "text": thought, "partial": False, "state": None,
                    })
            elif btype == "tool_use":
                _flush_text()
                name = block.get("name") or "tool"
                tinput = block.get("input")
                if not isinstance(tinput, dict):
                    tinput = {}
                events.append({
                    "kind": "tool_use", "role": role,
                    "id": block.get("id") or "", "name": name, "input": tinput,
                    "text": name, "partial": False, "state": None,
                })
            elif btype == "tool_result":
                _flush_text()
                result_content = block.get("content")
                result_text = ""
                if isinstance(result_content, str):
                    result_text = result_content
                elif isinstance(result_content, list):
                    result_text = " ".join(
                        b.get("text") or "" for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                events.append({
                    "kind": "tool_result", "role": role,
                    "tool_use_id": block.get("tool_use_id") or "",
                    "is_error": bool(block.get("is_error")),
                    "content": result_text, "text": result_text,
                    "partial": False, "state": None,
                })
        _flush_text()
        return events

    # system / init lines
    if msg_type in ("system", "init"):
        content = obj.get("content") or message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                b.get("text") or "" for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            return []
        return [{
            "kind": "system", "role": "system", "text": content,
            "partial": False, "state": None,
        }]

    # Unknown or empty type — skip
    return []
