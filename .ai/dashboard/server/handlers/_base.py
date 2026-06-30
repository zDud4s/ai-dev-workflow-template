"""Type-checking-only base for the dashboard route-handler mixins.

The ``*Routes`` classes in this package are mixins with no runtime base: at
runtime ``serve.Handler`` composes them onto ``http.server.SimpleHTTPRequestHandler``
(see ``serve.Handler``'s bases), so ``self.send_header`` / ``self._json`` /
``self.wfile`` etc. all resolve through the MRO. Pyright, however, analyses each
mixin file in isolation and cannot see that future base, so it reports every such
access as ``reportAttributeAccessIssue``.

``_RouteMixin`` makes that implicit contract explicit *for the type-checker
only*: under ``TYPE_CHECKING`` it inherits ``SimpleHTTPRequestHandler`` (covering
send_response / send_header / end_headers / wfile / rfile / headers / send_error
/ ...) and declares the custom helpers + class constants that ``serve.Handler``
and its sibling mixins provide. Each handler uses it as its base, but at runtime
``_RouteMixin is object`` -- so the runtime MRO is byte-for-byte what it was when
the mixins had no explicit base. This module adds zero behaviour; it exists only
to give Pyright the type of ``self``.

Signatures are deliberately permissive (``*args/**kwargs -> Any``) so this never
introduces a *new* false positive; it only stops the access-on-unknown-base ones.
If a future helper is added to ``serve.Handler`` and called from a mixin, add one
line here (otherwise the red squiggle returns as a gentle reminder).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import SimpleHTTPRequestHandler
    from typing import Any, ClassVar

    class _RouteMixin(SimpleHTTPRequestHandler):
        # serve.Handler runs on socketserver.ThreadingTCPServer, so self.server
        # is always a server whose .server_address is the (host, port) tuple.
        # typeshed types BaseServer.server_address as a non-tuple-guaranteed
        # union, so subscripting it (self.server.server_address[1]) trips
        # reportIndexIssue. Loosen to Any here — self.server is only read for
        # that address — instead of scattering `# type: ignore[index]`.
        server: Any
        # -- helpers defined on serve.Handler / sibling route mixins --
        def _json(self, *args: Any, **kwargs: Any) -> Any: ...
        def _csrf_guard(self, *args: Any, **kwargs: Any) -> Any: ...
        def _run_subprocess(self, *args: Any, **kwargs: Any) -> Any: ...
        def _write_sse_frame(self, *args: Any, **kwargs: Any) -> Any: ...
        def _write_sse_event(self, *args: Any, **kwargs: Any) -> Any: ...
        def _sse_client_gone(self, *args: Any, **kwargs: Any) -> Any: ...
        def _job_summary(self, *args: Any, **kwargs: Any) -> Any: ...
        def _todos_latest(self, *args: Any, **kwargs: Any) -> Any: ...
        def _todos_banner(self, *args: Any, **kwargs: Any) -> Any: ...
        def _clean_todo_tags(self, *args: Any, **kwargs: Any) -> Any: ...
        def _read_workflow_version(self, *args: Any, **kwargs: Any) -> Any: ...
        def _find_bash(self, *args: Any, **kwargs: Any) -> Any: ...
        def _compose_multimodal_blocks(self, *args: Any, **kwargs: Any) -> Any: ...
        def _is_template_repo(self, *args: Any, **kwargs: Any) -> Any: ...
        def _is_blocked_path(self, *args: Any, **kwargs: Any) -> Any: ...
        def _clone_template(self, *args: Any, **kwargs: Any) -> Any: ...
        def _pipelines_origin_guard(self, *args: Any, **kwargs: Any) -> Any: ...
        def _agent_orchestrations_origin_guard(self, *args: Any, **kwargs: Any) -> Any: ...
        # -- class constants on serve.Handler / mixins --
        _UUID_RE: ClassVar[Any]
        _BLOCKED_NAMES: ClassVar[Any]
        _BLOCKED_NAME_PREFIXES: ClassVar[Any]
        _BLOCKED_NAME_SUFFIXES: ClassVar[Any]
        _BLOCKED_PATHS: ClassVar[Any]
        _TOOLS: ClassVar[Any]
        _REASONING: ClassVar[Any]
        _PHASES: ClassVar[Any]
        _PHASE_MODES: ClassVar[Any]
        _AUTO_SELECT_BUDGETS: ClassVar[Any]
        _IMPROVER_BOUNDS: ClassVar[Any]
        _IMPROVER_INT_FIELDS: ClassVar[Any]
else:
    _RouteMixin = object
