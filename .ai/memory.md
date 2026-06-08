# Project Memory

Operational facts for future tasks. Format: `- YYYY-MM-DD [topic] fact` — one line, dated, single topic tag (e.g. `[build]`, `[tests]`). See `.claude/skills/maintenance/SKILL.md` for what to store, what not to store, and consolidation rules.

## Entries

- 2026-06-08 [dashboard] Terminals tab is a STATUS LIST ONLY; the standalone canvas window (app/canvas.html via PaneCore in pane-core.js) is the sole interactive surface. terminals.js no longer builds inline panes — it routes keys to the canvas over CanvasBus (termSendToCanvas) and only keeps the status list + draft composer + canvas bridge.
- 2026-06-08 [dashboard] New conversations: the dashboard creates the server resource then routes the key to the canvas. Claude = mint sid + POST /api/sessions/<sid>/input (create-on-first-turn); codex = POST /api/jobs; shell/PTY = POST /api/ptys with launch steps sent as the bus open message's initialCommand. The canvas window must be opened in the click gesture (before any await) to dodge popup blockers.
- 2026-06-08 [dashboard] PTY WS auth token crosses windows via a shared same-origin localStorage cache dash.ptyTokens.v1 (window.PtyTokens get/set/remove) plus meta.token on the CanvasBus open message; canvas.js stores it into window._PTY_TOKENS before mount and persists it via saveCanvasState/restoreCanvasState. This fixed the canvas WS 403. No serve.py change. PtyTokens.remove is wired into the canvas terminal-close path so the cache does not grow unbounded.
- 2026-06-08 [dashboard] The chat/session/SSE/search/PTY render+stream engine lives in pane-core.js (canvas renderer), NOT terminals.js. Source-pinning tests that assert that machinery must target pane-core.js. dash.openPanes.v1 persistence + v1->v2 migration were removed.

