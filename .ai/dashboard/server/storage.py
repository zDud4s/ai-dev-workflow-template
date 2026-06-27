"""Cached JSONL reader + path-cache bounding for the dashboard server.

Extracted from serve.py. Both helpers are generic (callers pass the path),
so this module holds only its own caches and depends on the stdlib. serve.py
re-exports these names, so ``serve._load_jsonl_cached`` keeps working.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path

# Max entries for the path-keyed parse caches. They are mtime-keyed
# (re-reading the same file overwrites its entry), so growth comes only from
# distinct files/sessions seen — but over a long-lived server that is still
# unbounded. Evict oldest-inserted entries past this cap.
_PATH_CACHE_MAX = 1024


def _write_text_lf(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with LF line endings, regardless of platform.

    Python's ``Path.write_text`` defaults to ``newline=None`` which translates
    ``\\n`` to the OS line terminator (``\\r\\n`` on Windows). The repo's
    ``.gitattributes`` pins ``*.yaml`` / ``*.md`` to ``eol=lf``, so writing
    those files through the dashboard previously produced spurious
    ``"CRLF will be replaced by LF"`` git warnings on Windows."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _bound_path_cache(cache: dict, max_size: int = _PATH_CACHE_MAX) -> None:
    # Plain dicts preserve insertion order, so popping the front drops the
    # least-recently-added entries. Call under the cache's own lock.
    while len(cache) > max_size:
        try:
            cache.pop(next(iter(cache)))
        except (StopIteration, KeyError):
            break


# Generic mtime-invalidated cache for append-only JSONL ledgers (jobs,
# improvements, skill metrics, ...). Every list/aggregate endpoint used to
# re-read and re-parse its whole ledger on every call — at ~100MB the
# dashboard became unresponsive. The cache returns the same parsed ``list``
# object until the file's mtime changes, so a cache hit is a single
# ``stat()`` + dict lookup. The cache lock guards only the dict; the actual
# read happens between two lock acquisitions on purpose so a slow disk
# can't block other readers. Two concurrent first-callers may parse the
# same payload twice; the second write just replaces the first with an
# identical value, which is harmless.
#
# Write-side locks on the ledgers (``_JOBS_PERSIST_LOCK``,
# ``_IMPROVEMENTS_LEDGER_LOCK``, ``_SKILL_METRICS_LOCK``) are independent —
# we never hold the cache lock while opening the file, so there is no
# deadlock path.
_JSONL_CACHE: dict[str, tuple[int, list[dict]]] = {}
_JSONL_CACHE_LOCK = threading.Lock()


def _load_jsonl_cached(path: Path) -> list[dict]:
    """Return parsed rows from a JSONL file, cached until ``mtime`` changes.

    Behaviour matches the prior hand-rolled readers: blank lines are skipped,
    decode errors fall back to the unicode replacement character, and
    ``json.JSONDecodeError`` on individual lines is silently swallowed so a
    single corrupt entry can't poison the whole endpoint. Returns ``[]`` when
    the file does not exist (callers used to special-case this themselves).
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    except OSError:
        # Permission errors etc. — behave as if empty so a transient FS hiccup
        # doesn't surface as a 500. Endpoints that need to know the difference
        # already wrap their own file ops in try/except above this layer.
        return []
    key = str(path)
    with _JSONL_CACHE_LOCK:
        cached = _JSONL_CACHE.get(key)
        if cached is not None and cached[0] == st.st_mtime_ns:
            return cached[1]
    # Read outside the lock — slow I/O must not block other cache readers.
    # Two concurrent first-callers will parse twice; both writes produce the
    # same list so the race is benign.
    # Tail-bound at 10k rows — older entries dropped on parse.
    rows_dq: deque[dict] = deque(maxlen=10000)
    # Per-line cap: a hostile or wedged producer that emits one giant line
    # would otherwise be ingested whole here and replicated across every
    # cached parse. 1 MiB per row is generous for legitimate JSONL events.
    max_line_bytes = 1 * 1024 * 1024
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Text mode yields str, so measure UTF-8 bytes to honour the
                # byte cap — len(line) would count code points, letting a line
                # of multi-byte UTF-8 reach ~4x the intended on-disk size.
                if len(line.encode("utf-8", errors="replace")) > max_line_bytes:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rows_dq.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    # Mirror the prior hand-rolled behaviour: skip malformed
                    # rows silently rather than failing the whole endpoint.
                    continue
    except OSError:
        # If the file vanished or became unreadable between ``stat()`` and
        # ``open()`` (a rare race during rotation), treat as empty. Don't
        # cache the empty result — the next call will retry the stat.
        return []
    rows = list(rows_dq)
    with _JSONL_CACHE_LOCK:
        _JSONL_CACHE[key] = (st.st_mtime_ns, rows)
    return rows
