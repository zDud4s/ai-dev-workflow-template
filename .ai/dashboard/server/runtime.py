"""Shared HTTP runtime state: the actually-bound port and the loopback
Origin allowlist.

The allowlist is consulted in two places — ``_origin_allowed`` for CSRF on
state-changing requests, and ``_browser_cross_origin_blocked`` for rejecting
cross-origin browser GETs against long-lived SSE endpoints. Both key on
``BOUND_PORT``.

This lives in its own module (rather than serve.py) so the WebSocket layer
(``server/ws.py``) can enforce the same Origin allowlist during its handshake
without importing serve. ``BOUND_PORT`` is mutable runtime state: it starts at
the configured port and ``main()`` republishes the real bound port via
``set_bound_port`` once the listening socket is open (the two diverge when the
dynamic-port fallback in ``main()`` picks another candidate).
"""
from __future__ import annotations

import os

# Initialised to the configured port (mirrors serve.PORT's default). main()
# overwrites it with the real bound port via set_bound_port() once the socket
# is open, so the allowlist below validates against the port the server is
# actually listening on rather than the configured one.
BOUND_PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))


def set_bound_port(port: int) -> None:
    """Publish the port the server actually bound to, so the Origin allowlist
    validates against it rather than the stale configured port. Critical when
    main()'s dynamic-port fallback picked a different candidate."""
    global BOUND_PORT
    BOUND_PORT = port


def _origin_allowed(headers) -> bool:
    """Origin allowlist for state-changing requests. Returns True iff:
      - the Origin header is present, AND
      - it exactly matches a loopback dashboard origin for the bound port.
    'null' Origin (sandboxed iframes / file://) is rejected. No trailing-
    slash tolerance -- Origin per RFC6454 has no path. Validates against
    BOUND_PORT (the port the server is actually listening on) rather than
    the configured PORT, so the dynamic-port-fallback in main() doesn't
    break CSRF for the second concurrent dashboard.
    """
    origin = headers.get("Origin")
    if origin is None:
        return False
    allowed = {
        f"http://127.0.0.1:{BOUND_PORT}",
        f"http://localhost:{BOUND_PORT}",
        f"http://[::1]:{BOUND_PORT}",
    }
    return origin in allowed


def _browser_cross_origin_blocked(headers) -> bool:
    """Return True when a long-lived GET (SSE) appears to be a cross-
    origin browser request and should be rejected.

    SSE endpoints can't go through ``_csrf_guard`` directly because we
    also want operator ``curl`` / ``wget`` to work — those send no
    Origin header at all. The actual threat is a cross-origin BROWSER
    page that issues ``new EventSource(...)``/``fetch(...)`` against
    a localhost SSE endpoint: the browser blocks reading the response,
    but the server already allocated a thread + queue slot. Repeated
    cross-origin requests exhaust the request-handling thread pool.

    Rule: reject only when Origin is set AND not in the loopback
    allowlist. Origin absent → not a browser context → allow.
    """
    origin = headers.get("Origin")
    if origin is None:
        return False
    allowed = {
        f"http://127.0.0.1:{BOUND_PORT}",
        f"http://localhost:{BOUND_PORT}",
        f"http://[::1]:{BOUND_PORT}",
    }
    return origin not in allowed
