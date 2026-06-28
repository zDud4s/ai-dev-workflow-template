"""Request handlers for the analytics-family GET endpoints.

Covers /api/usage/total, /api/timeline, /api/analytics and /api/auto-select.
Extracted from serve.py as the ``AnalyticsRoutes`` mixin; ``Handler`` inherits
it so routing and ``serve.Handler._handle_analytics`` resolve via MRO. The
aggregation helpers each method calls are imported from their owning
``server.*`` modules (importing them from serve would be circular).
"""
from __future__ import annotations
from server.handlers._base import _RouteMixin

import datetime as _dt
import urllib.parse

from server.analytics import (
    _aggregate_analytics,
    _load_auto_select_ranking,
    _load_timeline_runs,
)
from server.usage import _aggregate_project_token_usage


class AnalyticsRoutes(_RouteMixin):
    """Usage / timeline / analytics / auto-select endpoints, mixed into ``Handler``."""

    def _handle_usage_total(self) -> None:
        """Aggregate token usage across every Claude transcript for this
        repo. Powers the overview's "Tokens used" card."""
        self._json(200, _aggregate_project_token_usage())

    def _handle_timeline(self) -> None:
        """Pipeline Gantt data — phase_dispatch events from .ai/local/ledgers/events.jsonl
        grouped per session_id. Powers the Timeline view."""
        self._json(200, {"runs": _load_timeline_runs()})

    def _handle_analytics(self, parsed) -> None:
        """Chart-ready aggregation of the six ledgers for the Analytics tab.
        Query param ``range`` is one of 7d/30d/90d/all (defaults to 30d)."""
        qs = urllib.parse.parse_qs(parsed.query)
        range_key = (qs.get("range", ["30d"])[0] or "30d")
        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            payload = _aggregate_analytics(now, range_key)
        except Exception as exc:  # never 500 the whole dashboard
            # Log server-side; return a generic message so an unexpected error
            # (e.g. an OSError carrying a filesystem path) can't leak internals
            # to the client — matching _read_json_body's convention.
            print(f"[serve] analytics aggregation failed: {exc}", flush=True)
            self._json(500, {"error": "analytics aggregation failed"})
            return
        self._json(200, payload)

    def _handle_auto_select(self, parsed) -> None:
        """Auto-select scorer ranking — aggregated from .ai/local/ledgers/metrics.jsonl.
        Powers the Auto-select view. Accepts `?min_samples=N` (clamp 1..50,
        default 5); invalid values fall back to the default."""
        raw = urllib.parse.parse_qs(parsed.query or "").get("min_samples", [None])[0]
        try:
            min_samples = max(1, min(50, int(raw)))
        except (TypeError, ValueError):
            min_samples = 5
        self._json(200, _load_auto_select_ranking(min_samples=min_samples))
