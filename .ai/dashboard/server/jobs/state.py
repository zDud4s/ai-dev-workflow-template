"""The shared in-memory job registry and its invariants.

This is the single owner of the mutable ``JOBS`` dict and its lock. Everything
that touches jobs — the lifecycle/streaming layer, persistence, the reaper, the
session-engine machinery, and the Handler methods still in serve.py — imports
these *by reference* so they all mutate one object. Nothing ever reassigns
``JOBS`` in production code (only `JOBS[id] = ...` / `.pop` / `.clear` /
`.values`), so the shim re-export in serve.py stays pointed at the same dict
forever.

Pure leaf: depends only on the stdlib, so the rest of the jobs package can build
on top of it.
"""
from __future__ import annotations

import threading

# Allowed job kinds.
#   orchestrate / plan : one-shot `claude -p <skill prompt>` runs.
#   chat               : long-lived interactive `claude` session driven by
#                        JSON messages on stdin and JSON events on stdout
#                        (--input-format stream-json / --output-format stream-json).
#   chat-codex         : one-turn `codex exec --json` run per user message.
#                        Resumed via `codex exec resume <session_id>`.
JOB_KINDS = {
    "orchestrate": "orchestrate",
    "plan": "planner",
    "chat": None,
    "chat-codex": None,
}

# In-memory job registry. State is lost on server restart by design;
# log files survive on disk for forensic reading.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBS_MAX = 50  # cap memory; oldest finished entries get evicted

# Fields that exist only at runtime inside the JOBS dict and must NOT be
# serialised to disk (they are either not JSON-encodable or meaningless
# after the subprocess dies).
_JOB_RUNTIME_FIELDS = frozenset({"proc", "subscribers", "stdin_lock"})

# Terminal job statuses — used to know when scanned log-file cost can be
# memoised back onto the job entry (cost can't change once the subprocess
# is dead).
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})
