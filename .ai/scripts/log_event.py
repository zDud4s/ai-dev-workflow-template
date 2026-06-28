"""Claude Code hook: log workflow phase dispatches to .ai/local/ledgers/events.jsonl.

Invoked by the PostToolUse hook for the Bash tool (see .claude/settings.json,
which points at `.ai/scripts/log_event.py`). Reads the hook payload
from stdin (JSON), detects whether the executed command was a workflow
phase dispatch (Claude or Codex subprocess), and appends a structured
event line if so.

Never raises — any parse failure results in a silent no-op so the user's
workflow is never blocked.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

# Script lives at <repo>/.ai/scripts/log_event.py — repo root is parents[2].
# (Was parents[3] under the old .ai/dashboard/scripts/ home, one level deeper.)
ROOT = Path(__file__).resolve().parents[2]
EVENTS_FILE = ROOT / ".ai" / "local" / "ledgers" / "events.jsonl"
# Cache parent-dir existence across hook invocations in the same process.
# This hook fires on every PostToolUse so the mkdir syscall would otherwise
# run thousands of times per session even though the directory is created once.
_PARENT_DIR_READY = False

# Patterns matching the dispatcher commands documented in .ai/workflow/dispatch.md.
# Claude:  cat /tmp/phase-<name>-prompt.md | claude -p "Execute the attached <name> phase ..." --model <model>
# Codex:   cat /tmp/phase-<name>-prompt.md | codex exec --skip-git-repo-check -m <model> ...
RE_CLAUDE = re.compile(r"\bclaude\s+-p\b[^\n]*?--model\s+([A-Za-z0-9._:\-]+)", re.S)
RE_CODEX = re.compile(r"\bcodex\s+exec\b[^\n]*?\s-m\s+([A-Za-z0-9._:\-]+)", re.S)
# Phase detection: prefer the tmp file path (works for both tools);
# fall back to the inline "Execute the attached <phase> phase" string (claude only).
RE_PHASE_PATH = re.compile(r"/tmp/phase-(\w+)-prompt\.md")
RE_PHASE_INLINE = re.compile(r"Execute the attached\s+(\w+)\s+phase", re.I)


def _msvcrt_lock_at_start(f, msvcrt, mode) -> None:
    f.seek(0)
    msvcrt.locking(f.fileno(), mode, 1)


def detect(command: str) -> dict | None:
    if not command:
        return None
    m_claude = RE_CLAUDE.search(command)
    m_codex = RE_CODEX.search(command)
    if m_claude:
        tool, model = "claude", m_claude.group(1)
    elif m_codex:
        tool, model = "codex", m_codex.group(1)
    else:
        return None
    m_phase = RE_PHASE_PATH.search(command) or RE_PHASE_INLINE.search(command)
    phase = m_phase.group(1).lower() if m_phase else "unknown"
    # Prefer the line that actually carries the dispatch invocation. Taking the
    # last line would, for a multi-line command, capture a trailing cleanup
    # (e.g. `rm -f /tmp/phase-prompt.md`) instead of the matched claude/codex
    # call this hook exists to record.
    matched = m_claude or m_codex
    if "\n" in command:
        line_idx = command.count("\n", 0, matched.start())
        lines = command.splitlines()
        preview = (lines[line_idx] if line_idx < len(lines) else lines[-1]).strip()
    else:
        preview = command.strip()
    if len(preview) > 200:
        preview = preview[:197] + "..."
    return {"tool": tool, "model": model, "phase": phase, "command_preview": preview}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if payload.get("tool_name") != "Bash":
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    detected = detect(command)
    if not detected:
        return
    resp = payload.get("tool_response") or {}
    exit_code = resp.get("exit_code")
    if exit_code is None:
        # Fall back: infer success from absence of obvious error markers.
        exit_code = 0 if not resp.get("interrupted") else None
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "phase_dispatch",
        "session_id": payload.get("session_id"),
        "exit_code": exit_code,
        **detected,
    }
    global _PARENT_DIR_READY
    try:
        if not _PARENT_DIR_READY:
            EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PARENT_DIR_READY = True
        # POSIX O_APPEND is atomic up to PIPE_BUF (4 KiB) so two hook
        # processes writing simultaneously won't interleave bytes mid-line.
        # Windows has no equivalent guarantee — use msvcrt.locking around
        # the write so concurrent dispatches can't shred each other's JSON.
        with EVENTS_FILE.open("ab") as f:
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            if sys.platform == "win32":
                try:
                    import msvcrt
                    _msvcrt_lock_at_start(f, msvcrt, msvcrt.LK_LOCK)
                    try:
                        f.write(line)
                        f.flush()
                    finally:
                        try:
                            _msvcrt_lock_at_start(f, msvcrt, msvcrt.LK_UNLCK)
                        except OSError:
                            pass
                except (ImportError, OSError):
                    # Lock acquisition failed (rare) — fall back to a plain
                    # write rather than dropping the event entirely.
                    f.write(line)
            else:
                try:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        f.write(line)
                        f.flush()
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    f.write(line)
    except Exception:
        # A.P3-6: reset _PARENT_DIR_READY so a deleted parent dir can be
        # recreated on the next call instead of every write silently failing.
        _PARENT_DIR_READY = False
        return


if __name__ == "__main__":
    main()
