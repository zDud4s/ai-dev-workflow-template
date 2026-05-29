"""Shared policy for identifying stale auto-improver transcripts.

We treat ``failed`` runs as worth keeping because the transcript may be the
only diagnostic record for the failed LLM call.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Literal

RESOLVED_STATUSES = frozenset({"applied", "installed", "rolled_back", "rejected", "no_change"})
STALE_DAYS = 7
LEDGER_WINDOW_SECONDS = 3600

_SKILL_RE = re.compile(r"(?im)^\s*SKILL:\s*(?P<skill>[^\r\n]+?)\s*$")


def _message_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    return ""


def _record_content(obj: dict) -> str:
    message = obj.get("message")
    if isinstance(message, dict):
        return _message_text(message.get("content"))
    return _message_text(obj.get("content"))


def _parse_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).timestamp()


def _now_epoch(now) -> float:
    if isinstance(now, _dt.datetime):
        dt = now
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc).timestamp()
    return float(now)


def is_improver_transcript(path) -> tuple[bool, str | None, float | None]:
    """Return whether ``path`` is an improver transcript, plus skill and ts.

    The identifying marker is deliberately narrow: the first user message
    must contain both the strict output-format header and a ``SKILL:`` line.
    """
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "user":
                    continue
                text = _record_content(obj)
                match = _SKILL_RE.search(text)
                if "OUTPUT FORMAT (STRICT)" in text and match:
                    skill = match.group("skill").strip()
                    first_user_ts = _parse_ts(obj.get("timestamp") or obj.get("ts"))
                    return True, skill, first_user_ts
                return False, None, None
    except OSError:
        return False, None, None
    return False, None, None


def _has_assistant_record(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") == "assistant":
                    return True
    except OSError:
        return False
    return False


def load_ledger_rows(ledger_path) -> list[dict]:
    rows: list[dict] = []
    try:
        with Path(ledger_path).open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return rows


def _matching_ledger_rows(skill: str | None, first_user_ts: float | None, ledger_rows: list[dict]) -> list[dict]:
    if not skill or first_user_ts is None:
        return []
    out: list[dict] = []
    for row in ledger_rows:
        if row.get("skill") != skill:
            continue
        row_ts = _parse_ts(row.get("ts"))
        if row_ts is None:
            continue
        if abs(row_ts - first_user_ts) <= LEDGER_WINDOW_SECONDS:
            out.append(row)
    return out


def classify_transcript(
    path,
    ledger_rows: list[dict],
    now,
) -> Literal["orphan", "resolved", "unmatched_pre_audit", "keep"]:
    p = Path(path)
    is_improver, skill, first_user_ts = is_improver_transcript(p)
    if not is_improver:
        return "keep"
    if not _has_assistant_record(p):
        return "orphan"
    try:
        stale = (_now_epoch(now) - p.stat().st_mtime) >= (STALE_DAYS * 86400)
    except OSError:
        return "keep"
    if not stale:
        return "keep"
    matches = _matching_ledger_rows(skill, first_user_ts, ledger_rows)
    if not matches:
        return "unmatched_pre_audit"
    if all(str(row.get("status") or "") in RESOLVED_STATUSES for row in matches):
        return "resolved"
    return "keep"
