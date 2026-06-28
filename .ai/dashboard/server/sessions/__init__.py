"""Unified-chat session machinery, keyed on the Claude transcript sid.

Groups the three flat session engines: ``events`` (was ``session_events`` --
parse a transcript JSONL line into chat events), ``registry`` (was
``session_registry`` -- per-session state machine), and ``lock`` (was
``session_lock`` -- cross-process file lock so two dashboards never run an
engine on the same session). No primary module. Distinct from ``server.pty``
(real shells) and ``server.handlers.sessions`` (the HTTP routes).
"""
