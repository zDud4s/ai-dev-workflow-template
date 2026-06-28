"""Shared HTTP-layer limits for the dashboard request handler.

Extracted from serve.py so the per-domain handler mixins (``server/*_handlers.py``)
can import these caps without importing serve (which would be circular — serve
imports the mixins to assemble ``Handler``). serve.py re-exports every name via a
shim, so ``serve.MAX_JSON_BODY`` etc. keep resolving for existing callers/tests.
"""
from __future__ import annotations

import threading

# Caps concurrent /api/suggestions/<id>/draft + /api/agents/suggest requests.
# Both endpoints spawn long-running `claude -p` / `codex` subprocesses
# (timeout_seconds, default 120s) on the request thread; without a cap a
# handful of concurrent clients can exhaust the server thread pool. Shared
# between both endpoints because they consume the same LLM CLI binary.
_SUGGESTION_SEMAPHORE = threading.Semaphore(2)
# Hard cap on the request-thread wall-clock for /api/suggestions/<id>/draft
# and /api/agents/suggest. ``cfg["timeout_seconds"]`` can be set as high as
# 3600s (see _IMPROVER_TIMEOUT_BOUNDS); even with the semaphore cap above,
# a 1-hour subprocess pinning a request thread + browser tab connection is
# a trivial DoS vector. 60s is well above any healthy LLM response time
# yet bounded so a misbehaving CLI can't park the dashboard.
_SUGGESTION_HTTP_TIMEOUT_MAX = 60

# Maximum size of a JSON request body. Anything larger gets a 413 before we
# even allocate a buffer — a single multi-MB POST against an endpoint that
# expects ``{"mode": "..."}`` is a trivial DoS otherwise. 1 MiB is well above
# any legitimate payload the dashboard sends (the largest is the chat
# composer with inlined files, which is capped client-side at ~256 KB).
MAX_JSON_BODY = 1024 * 1024  # 1 MiB

# Per-PUT cap for /api/pipelines/<slug>. Pipeline YAMLs are tiny —
# a few nodes, kilobytes at most. Capping at 256 KB keeps the
# generic 1 MiB ceiling for other endpoints while making it cheap
# to reject obviously-malformed PUTs to this specific route.
MAX_PIPELINE_PUT_BYTES = 256 * 1024  # 256KB hard cap on PUT body

# Hard upper bound on a single Server-Sent Events session, regardless of
# whether the subscriber is idle or not. ``_handle_job_stream`` already
# bails on a 4-minute idle window, but a chatty job could keep a single
# connection open indefinitely otherwise — and the SSE response holds a
# request thread, a queue subscriber slot, and a TCP connection for the
# whole lifetime. Clients reconnect transparently, so a forced rotation
# is observationally invisible.
MAX_SSE_SESSION_S = 1800  # 30 minutes

# Upper bound on the initial catch-up flush in ``_handle_transcript_stream``.
# Transcript JSONLs grow into the tens of MB over long IDE sessions and the
# old code did one unbounded ``fh.read()`` per SSE subscriber, so N parallel
# streams scaled memory pressure linearly with file size. We cap the catch-up
# at 4 MiB and tail from the last line boundary inside that window — live tail
# then picks up from EOF so new records still arrive.
MAX_TRANSCRIPT_CATCHUP_BYTES = 4 * 1024 * 1024  # 4 MiB

# Directories the fallback ``ROOT.rglob("*")`` walk in ``_handle_files_list``
# must not descend into. Without this, the autocomplete endpoint walks the
# entire repo on every keystroke when ``git ls-files`` is unavailable —
# slow on large repos and leaks dotfile paths (``.git/objects/*``,
# ``node_modules/**``, ``.venv/**``) into the suggestion list.
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".tox", ".mypy_cache", "tmp",
})
