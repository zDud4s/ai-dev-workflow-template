from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from server.transcript_paths import _codex_sessions_root, _transcripts_dir_for_cwd
from server.storage import _bound_path_cache
from server.paths import ROOT

def _lookup_session_task(session_id: str) -> str | None:
    """Best-effort: read the first user message from the Claude transcript
    matching this session_id so the timeline row can show what the run was
    about. Returns None when the transcript is missing or unreadable —
    callers must treat that as "unknown task" without erroring."""
    if not session_id or session_id == "unknown":
        return None
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
    except OSError:
        return None
    if tdir is None or not tdir.is_dir():
        return None
    f = tdir / f"{session_id}.jsonl"
    if not f.is_file():
        return None
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    text = " ".join(p for p in parts if p)
                else:
                    text = ""
                text = " ".join(text.split())  # collapse whitespace
                if not text:
                    continue
                # Skip IDE/system-injected "user" messages so the row shows the
                # first REAL prompt. Claude Code wraps editor state, system
                # reminders, command output, and tool results in <tag>...</tag>
                # envelopes that arrive as type=user but aren't what the
                # operator typed. Tag pattern: lowercase letters / underscores
                # / hyphens. If a user prompt legitimately starts with '<'
                # (e.g. a code snippet), it would have a space or quote before
                # the closing '>'.
                if re.match(r"^<[a-z][a-z0-9_-]*>", text):
                    continue
                return text[:120] + ("…" if len(text) > 120 else "")
    except OSError:
        return None
    return None


def _lookup_session_title(session_id: str) -> str | None:
    """Best-effort: extract the Claude-Code-generated ``ai-title`` record
    from a session transcript so the dashboard can label a collapsed
    transcript pane with the IDE's own chat name instead of relying on
    the first user message (or the bare UUID).

    The ai-title is a meta record Claude writes a few lines into the
    JSONL once it's picked a display title — same string the IDE shows
    in its sessions sidebar. Latest one wins (Claude can rename mid-
    session). Bounded scan keeps the picker snappy on multi-MB
    transcripts; if the title hasn't been written yet, the caller falls
    back to the first-user-message preview."""
    if not session_id or session_id == "unknown":
        return None
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
    except OSError:
        return None
    if tdir is None or not tdir.is_dir():
        return None
    f = tdir / f"{session_id}.jsonl"
    if not f.is_file():
        return None
    MAX_LINES = 200
    MAX_BYTES = 64 * 1024
    title: str | None = None
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            lines_seen = 0
            bytes_seen = 0
            for line in fh:
                lines_seen += 1
                bytes_seen += len(line)
                if lines_seen > MAX_LINES or bytes_seen > MAX_BYTES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "ai-title":
                    continue
                at = rec.get("aiTitle")
                if isinstance(at, str) and at.strip():
                    title = at.strip()[:120]
    except OSError:
        return None
    return title


def _lookup_session_model(session_id: str) -> str | None:
    """Best-effort: the model an IDE session is running, for the status list.

    Returns the first ``message.model`` found near the head of the transcript
    (early-exit), or None. IDE transcripts carry no model in their JOBS-less
    session record, so without this the status row can't show one. Reads only
    the head — never the full file — so it stays cheap on the 2-4s poll.
    """
    if not session_id or session_id == "unknown":
        return None
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
    except OSError:
        return None
    if tdir is None or not tdir.is_dir():
        return None
    f = tdir / f"{session_id}.jsonl"
    if not f.is_file():
        return None
    HEAD_LINES = 400  # enough to pass IDE/system preamble and reach a model
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > HEAD_LINES:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = rec.get("message")
                if isinstance(msg, dict):
                    mdl = msg.get("model")
                    # Skip Claude Code's "<synthetic>" placeholder (injected
                    # assistant messages carry no real model) — keep scanning
                    # for the actual model the session is running.
                    if isinstance(mdl, str) and mdl and not mdl.startswith("<"):
                        return mdl
    except OSError:
        return None
    return None


def _summarise_tool_use(name: str, tinput: dict) -> str:
    """One-line label for a tool_use block: what the agent is doing right now.

    e.g. ``Bash · npm test`` / ``Edit serve.py`` / ``Grep "TODO"``. Falls back
    to the bare tool name when the input shape is unknown.
    """
    name = name or "tool"
    ti = tinput if isinstance(tinput, dict) else {}

    def _base(path):
        if not isinstance(path, str) or not path:
            return ""
        return re.split(r"[\\/]", path.rstrip("\\/"))[-1]

    def _clip(s, n=60):
        s = " ".join(str(s).split())
        return s[:n] + "…" if len(s) > n else s

    if name == "Bash":
        cmd = ti.get("command") or ti.get("description") or ""
        return _clip("Bash · " + cmd) if cmd else "Bash"
    if name in ("Edit", "Write", "Read", "NotebookEdit", "MultiEdit"):
        b = _base(ti.get("file_path") or ti.get("notebook_path"))
        return f"{name} {b}" if b else name
    if name in ("Grep", "Glob"):
        pat = ti.get("pattern") or ""
        return _clip(f'{name} "{pat}"') if pat else name
    if name in ("Agent", "Task"):
        d = ti.get("description") or ti.get("subagent_type") or ""
        return _clip(f"{name} · {d}") if d else name
    if name == "WebFetch":
        return _clip("WebFetch · " + (ti.get("url") or ""))
    if name == "WebSearch":
        return _clip("WebSearch · " + (ti.get("query") or ""))
    if name == "Skill":
        return _clip("Skill · " + (ti.get("skill") or "")) or name
    return name


def _lookup_session_activity(session_id: str) -> dict | None:
    """Best-effort "what is this session doing right now", from the transcript
    tail. Returns ``{"text": str, "kind": str}`` or None.

    Claude Code flushes each content block as its own JSONL record as the turn
    streams, so the most recent content block reflects live state:

      * ``tool_use`` with no following ``tool_result`` → the tool is running
        now (``kind="tool"``, text like ``Bash · npm test``).
      * ``tool_result`` → a tool just returned; the model will continue
        (``kind="result"``, ``processing…``).
      * ``thinking`` → ``kind="thinking"``, ``thinking…``.

    A trailing plain ``text`` block is deliberately treated as "no live
    activity" (returns None): from outside the process a finished reply
    awaiting the user looks identical to one still streaming, so claiming
    "responding…" would overclaim. The caller falls back to a plain "live".

    Reads only a tail window so an active multi-MB transcript stays cheap.
    """
    if not session_id or session_id == "unknown":
        return None
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
    except OSError:
        return None
    if tdir is None or not tdir.is_dir():
        return None
    f = tdir / f"{session_id}.jsonl"
    if not f.is_file():
        return None
    TAIL_BYTES = 64 * 1024
    try:
        size = f.stat().st_size
        with f.open("rb") as fb:
            if size > TAIL_BYTES:
                fb.seek(size - TAIL_BYTES)
                fb.readline()  # drop the partial first line
            chunk = fb.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue  # skip ai-title / mode / last-prompt / summary meta rows
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            # A bare user/assistant string (typed prompt or plain reply) is
            # not a live-progress signal — see docstring.
            return None
        if not isinstance(content, list):
            continue
        # Inspect the LAST meaningful block of this (most recent) record.
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                return {"text": _summarise_tool_use(block.get("name"), block.get("input")), "kind": "tool"}
            if bt == "tool_result":
                return {"text": "processing…", "kind": "result"}
            if bt == "thinking":
                return {"text": "thinking…", "kind": "thinking"}
            if bt == "text":
                # Turn-end vs mid-stream is indistinguishable from outside —
                # don't overclaim. Fall back to a plain "live".
                return None
        # Record had no recognisable block — keep looking further back.
    return None


# Cache of resolved Codex rollout paths: session_id -> Path. A rollout's path
# never changes once created, so this is keyed by id alone (no mtime).
_CODEX_ROLLOUT_PATH_CACHE: dict[str, "Path | None"] = {}
_CODEX_ROLLOUT_PATH_LOCK = threading.Lock()


def _codex_rollout_path(session_id: str) -> "Path | None":
    """Locate the rollout file for a Codex session id under
    ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl``.

    The id is the trailing component of the filename, so a name-glob finds it
    without parsing dates. Result is cached (the path is stable); a negative
    result is cached as None so a missing rollout isn't re-walked every poll.
    """
    if not session_id or session_id == "unknown":
        return None
    with _CODEX_ROLLOUT_PATH_LOCK:
        if session_id in _CODEX_ROLLOUT_PATH_CACHE:
            return _CODEX_ROLLOUT_PATH_CACHE[session_id]
    root = _codex_sessions_root()
    found: "Path | None" = None
    if root is not None:
        try:
            found = next(root.rglob(f"rollout-*{session_id}.jsonl"), None)
        except OSError:
            found = None
    with _CODEX_ROLLOUT_PATH_LOCK:
        _CODEX_ROLLOUT_PATH_CACHE[session_id] = found
        _bound_path_cache(_CODEX_ROLLOUT_PATH_CACHE)
    return found


def _summarise_codex_call(name: str, raw: str) -> str:
    """One-line label for a Codex function_call / custom_tool_call."""
    name = name or "tool"

    def _clip(s, n=60):
        s = " ".join(str(s).split())
        return s[:n] + "…" if len(s) > n else s

    # shell_command / local_shell carry a JSON ``arguments`` with a command.
    if name in ("shell", "shell_command", "local_shell", "container.exec"):
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, ValueError):
            args = {}
        cmd = args.get("command") if isinstance(args, dict) else None
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return _clip("shell · " + cmd) if cmd else "shell"
    # apply_patch carries the patch text; pull the file it touches.
    if name == "apply_patch":
        m = re.search(r"\*\*\* (?:Update|Add|Delete) File: (.+)", raw or "")
        if m:
            f = re.split(r"[\\/]", m.group(1).strip())[-1]
            return f"apply_patch · {f}"
        return "apply_patch"
    return name


def _lookup_codex_activity(session_id: str) -> dict | None:
    """Best-effort live activity for a Codex session, from its rollout tail.

    Mirrors _lookup_session_activity for Codex's rollout schema:
      * ``function_call`` (no following ``function_call_output``) → a tool is
        running now (``shell · …`` / ``apply_patch · file``).
      * ``custom_tool_call`` → ``apply_patch · file``.
      * ``*_output`` → ``processing…``.
      * ``reasoning`` → ``thinking…``.
    A trailing ``agent_message`` / plain message is treated as no live signal.
    """
    path = _codex_rollout_path(session_id)
    if path is None or not path.is_file():
        return None
    TAIL_BYTES = 64 * 1024
    try:
        size = path.stat().st_size
        with path.open("rb") as fb:
            if size > TAIL_BYTES:
                fb.seek(size - TAIL_BYTES)
                fb.readline()
            chunk = fb.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rtype = rec.get("type")
        pl = rec.get("payload") or {}
        ptype = pl.get("type")
        if rtype == "response_item":
            if ptype in ("function_call_output", "custom_tool_call_output"):
                return {"text": "processing…", "kind": "result"}
            if ptype == "function_call":
                return {"text": _summarise_codex_call(pl.get("name"), pl.get("arguments") or ""), "kind": "tool"}
            if ptype == "custom_tool_call":
                return {"text": _summarise_codex_call(pl.get("name"), pl.get("input") or ""), "kind": "tool"}
            if ptype == "reasoning":
                return {"text": "thinking…", "kind": "thinking"}
            if ptype == "message":
                return None  # turn text — don't overclaim
        elif rtype == "event_msg":
            if ptype in ("exec_command_begin",):
                cmd = pl.get("command")
                if isinstance(cmd, list):
                    cmd = " ".join(str(c) for c in cmd)
                return {"text": _summarise_codex_call("shell", json.dumps({"command": cmd})), "kind": "tool"}
            if ptype in ("agent_reasoning", "agent_reasoning_delta"):
                return {"text": "thinking…", "kind": "thinking"}
            if ptype == "agent_message":
                return None
    return None
