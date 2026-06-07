"""Self-sufficiency tests for ``app/pane-core.js`` as the ISOLATED canvas
renderer.

The canvas page (``app/canvas.html``) does NOT load ``terminals.js``. PaneCore
is therefore an isolated, self-contained renderer: every external symbol it
references must be satisfied by one of

  * a global provided by the scripts canvas.html DOES load
    (core.js / skills.js / pane-helpers.js + the marked / DOMPurify / xterm
    CDN libs),
  * a symbol defined inside pane-core.js itself, or
  * a method of the HOST object passed as the 3rd arg to
    ``PaneCore.mount(container, opts, host)`` — the registry / layout / open /
    persistence seam.

In particular, pane-core.js must NOT reference any of terminals.js's private
registry / layout / open / persistence globals (TERMS, termClose, termClosePty,
termOpen, termGetLayout, persistOpenPanes, termRenderEmptyState,
termFocusNewPane, termSetCollapsed). Those concerns are routed through
``host.*``. The runtime counterpart to these source-level checks is
``tests/test_pane_core_selfsuff_node`` semantics: a node load of pane-core.js
under a stub window that defines ONLY the canvas-provided globals drives every
mount path without a ReferenceError (run manually via the dev harness).

Duplication between pane-core.js and terminals.js is an ACCEPTED cost of the
isolated-renderer decision, so these tests assert nothing about terminals.js
(which is off-limits to the canvas work).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANE_CORE = ROOT / ".ai/dashboard/app/pane-core.js"
PANE_HELPERS = ROOT / ".ai/dashboard/app/pane-helpers.js"
CANVAS_HTML = ROOT / ".ai/dashboard/app/canvas.html"
CANVAS_JS = ROOT / ".ai/dashboard/app/canvas.js"


def _src() -> str:
    return PANE_CORE.read_text(encoding="utf-8")


# Verbatim, single-occurrence fragments of each pane template that PaneCore
# owns. Not layout affordances — they genuinely track that the pane-INTRINSIC
# template lives in pane-core.js.
CHAT_TEMPLATE_MARKER = "type, /skill, @file, paste/drop images, Enter sends"
PTY_TEMPLATE_MARKER = "xterm.js failed to load (CDN blocked?)"
TRANSCRIPT_TEMPLATE_MARKER = "type to fork this IDE session"
SESSION_TEMPLATE_MARKER = "type a message · Enter sends · Shift+Enter newline"


# ─── existence / structural contract ────────────────────────────────────────


def test_pane_core_file_exists():
    assert PANE_CORE.exists(), "app/pane-core.js must exist"


def test_pane_core_defines_mount_and_fetch_meta():
    src = _src()
    assert "mount" in src, "PaneCore must define mount"
    assert "fetchMeta" in src, "PaneCore must define fetchMeta"


def test_pane_core_ends_with_window_export():
    src = _src()
    assert "window.PaneCore =" in src, "pane-core.js must export window.PaneCore"
    assert "mount" in src and "fetchMeta" in src


def test_pane_core_is_not_an_es_module():
    src = _src()
    for kw in ("\nexport ", "\nimport ", "export default", "export {"):
        assert kw not in src, f"pane-core.js must not use ES-module syntax ({kw!r})"


def test_pane_core_mount_takes_a_host_param():
    """The public mount entry must thread a host (3rd param). We assert the
    signature carries a host argument and the implementation builds a host
    shim from it."""
    src = _src()
    assert re.search(r"function paneCoreMount\(container,\s*opts,\s*host\)", src), (
        "paneCoreMount must accept (container, opts, host)"
    )
    assert "paneCoreHost(" in src, "mount must wrap the host through paneCoreHost()"


# ─── pane templates live in pane-core.js (it owns the render DOM) ────────────


def test_chat_template_present_in_pane_core():
    assert CHAT_TEMPLATE_MARKER in _src()


def test_pty_template_present_in_pane_core():
    assert PTY_TEMPLATE_MARKER in _src()


def test_transcript_template_present_in_pane_core():
    assert TRANSCRIPT_TEMPLATE_MARKER in _src()


def test_session_template_present_in_pane_core():
    assert SESSION_TEMPLATE_MARKER in _src()


# ─── host-routing: no bare registry / layout / open / persist globals ────────

# These are terminals.js-private. On the canvas surface they are undefined, so
# any bare reference would throw at mount time. PaneCore must route every one
# of these concerns through host.* instead.
FORBIDDEN_CALL_PATTERNS = {
    # registry
    "TERMS": r"\bTERMS\b",
    "persistOpenPanes(": r"\bpersistOpenPanes\s*\(",
    # close / open / focus
    "termClose(": r"\btermClose\s*\(",
    "termClosePty(": r"\btermClosePty\s*\(",
    "termOpen(": r"\btermOpen\s*\(",
    "termOpenSession(": r"\btermOpenSession\s*\(",
    "termOpenPty(": r"\btermOpenPty\s*\(",
    "termFocusNewPane(": r"\btermFocusNewPane\s*\(",
    # layout
    "termGetLayout(": r"\btermGetLayout\s*\(",
    "termSetCollapsed(": r"\btermSetCollapsed\s*\(",
    "termRenderEmptyState(": r"\btermRenderEmptyState\s*\(",
    "termToggleCollapsed(": r"\btermToggleCollapsed\s*\(",
    # list-only auto-open machinery
    "suppressAutoOpen(": r"\bsuppressAutoOpen\s*\(",
    "termAutoOpenEnabled(": r"\btermAutoOpenEnabled\s*\(",
    "AUTO_OPENED_ONCE": r"\bAUTO_OPENED_ONCE\b",
    "DISPATCH_TRACKERS": r"\bDISPATCH_TRACKERS\b",
    "termOpenDispatchTracker(": r"\btermOpenDispatchTracker\s*\(",
}


def test_pane_core_has_no_bare_registry_layout_open_persist_globals():
    src = _src()
    offenders = []
    for label, pat in FORBIDDEN_CALL_PATTERNS.items():
        for m in re.finditer(pat, src):
            # Allow these tokens to appear inside line comments / prose (the
            # header + the dispatch-tracker excision note explain WHY they are
            # absent). Only a real code reference is a violation.
            line_start = src.rfind("\n", 0, m.start()) + 1
            line = src[line_start:src.find("\n", m.start())]
            stripped = line.lstrip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            offenders.append((label, line.strip()))
    assert not offenders, (
        "pane-core.js must route registry/layout/open/persist through host.*, "
        "not reference terminals.js-private globals in code: " + repr(offenders)
    )


# ─── self-sufficiency: every term*/render* identifier it references is local,
#     a known canvas-provided global, or a host method. ────────────────────────

# Globals the canvas page DOES provide (so pane-core.js may reference them):
#   * core.js / skills.js
ALLOWED_CORE_SKILLS = {
    "escape", "postJson", "setMsg", "MODELS_BY_TOOL",
    # scheduleTokenUsageRefresh / debounce are referenced through window.* with
    # a typeof guard, so they're optional — listed for documentation.
    "scheduleTokenUsageRefresh", "debounce",
}
#   * pane-helpers.js (the pure render leaves)
ALLOWED_PANE_HELPERS = {
    "termSetPillState", "termExportMarkdown", "termInitAutoFollow",
    "termCloseAutocomplete", "termClearThinkingPlaceholder", "termFormatCost",
    "termFormatCostCompact", "termRefreshCost", "termRenderRaw",
    "renderBashCommand", "termPtyMissingDeps", "termPtyWsUrl",
}
#   * jobs.js — loadJobs, referenced through a typeof-guarded local wrapper.
ALLOWED_JOBS = {"loadJobs"}
#   * HOST contract method names — these appear as shorthand method definitions
#     in the paneCoreHost shim (``renderEmptyState() { ... }``); they are
#     satisfied by the host param, not a terminals.js global.
ALLOWED_HOST_METHODS = {"renderEmptyState"}


def _pane_helpers_exports() -> set[str]:
    """The set of term*/render* names pane-helpers.js actually defines, so the
    whitelist can't drift silently from the file it claims to mirror."""
    src = PANE_HELPERS.read_text(encoding="utf-8")
    return set(re.findall(r"function\s+(term[A-Za-z0-9_]*|render[A-Za-z0-9_]*)\s*\(", src))


def test_allowed_pane_helpers_whitelist_matches_file():
    """Guard the whitelist: every name we permit as a pane-helpers global must
    actually be defined in pane-helpers.js (else it would be undefined on the
    canvas surface too)."""
    defined = _pane_helpers_exports()
    missing = ALLOWED_PANE_HELPERS - defined
    assert not missing, (
        "whitelist references pane-helpers names that file does not define: "
        + repr(missing)
    )


def test_pane_core_references_only_resolvable_term_and_render_identifiers():
    """Every ``term*`` / ``render*`` identifier referenced in pane-core.js code
    (not comments) must be either:
      * defined locally in pane-core.js, OR
      * one of the pane-helpers.js pure leaves the canvas loads.

    This is the heart of the isolated-renderer guarantee: no terminals.js-only
    render/stream/send/layout global leaks through. PaneCore's own copies use
    the ``paneCore*`` prefix, so the only legitimate bare ``term*`` references
    are the pane-helpers leaves.
    """
    src = _src()
    # Drop block + line comments so prose mentions (the header documents the
    # terminals.js lineage) don't count as references.
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code_lines = []
    for line in no_block.splitlines():
        # strip trailing line comments (best-effort: not inside strings, which
        # is fine here — our identifiers of interest aren't inside // strings).
        idx = line.find("//")
        if idx != -1:
            line = line[:idx]
        code_lines.append(line)
    code = "\n".join(code_lines)

    # Names defined locally in pane-core.js (functions + vars).
    local_defs = set(re.findall(r"function\s+([A-Za-z0-9_]+)\s*\(", code))
    local_defs |= set(re.findall(r"\bvar\s+([A-Za-z0-9_]+)", code))

    # CALL SITES of camelCase term*/render* helpers — ``termFoo(`` / ``renderFoo(``
    # — that are NOT property accesses (``.termFoo(`` / ``h.renderEmptyState(``
    # are host/object methods, fine). The leading-uppercase-after-prefix shape
    # (term[A-Z] / render[A-Z], OR the exact lowercase pane-helper names) avoids
    # matching the kind STRING "terminal" / the CSS class "term-pane", neither of
    # which is ever a call target. Property-method calls (host.renderEmptyState)
    # are excluded by the negative-lookbehind on ".".
    call_re = re.compile(r"(?<![.\w])(term[A-Za-z0-9_]*|render[A-Za-z0-9_]*)\s*\(")
    referenced = set(call_re.findall(code))

    allowed = ALLOWED_PANE_HELPERS | local_defs | ALLOWED_HOST_METHODS
    unresolved = {
        name for name in referenced
        if name not in allowed
    }
    assert not unresolved, (
        "pane-core.js references term*/render* identifiers that are neither "
        "defined locally nor provided by pane-helpers.js (would be undefined "
        "on the canvas): " + repr(sorted(unresolved))
    )


# ─── canvas wiring: host adapter exists + threaded into the mount call ───────


def test_canvas_defines_host_and_threads_it_into_mount():
    src = CANVAS_JS.read_text(encoding="utf-8")
    assert "CANVAS_PANE_HOST" in src, "canvas.js must define a PaneCore host adapter"
    # The host adapter must implement the full contract.
    for method in ("register", "unregister", "get", "each", "close", "persist",
                   "setCollapsed", "focusNewPane", "renderEmptyState", "openPane"):
        assert re.search(rf"\b{method}\s*:", src), (
            f"CANVAS_PANE_HOST must implement host method {method!r}"
        )
    # renderTree must pass the host as the 3rd arg to PaneCore.mount.
    assert re.search(r"PaneCore\.mount\([^)]*CANVAS_PANE_HOST\s*\)", src), (
        "renderTree must thread CANVAS_PANE_HOST into PaneCore.mount(...)"
    )


# ─── canvas.html provisions the render-engine dependencies ───────────────────


def test_canvas_html_loads_render_engine_cdn_deps_before_pane_core():
    """PaneCore's copied render engine needs marked + DOMPurify (assistant
    markdown) and xterm + fit addon (PTY panes). canvas.html must load them
    before pane-core.js so they resolve as globals."""
    html = CANVAS_HTML.read_text(encoding="utf-8")
    pc = html.find("./pane-core.js")
    assert pc != -1
    for needle in ("marked@", "dompurify@", "xterm@", "xterm-addon-fit@"):
        idx = html.find(needle)
        assert idx != -1, f"canvas.html must load {needle} (PaneCore render dep)"
        assert idx < pc, f"{needle} must load before pane-core.js"
