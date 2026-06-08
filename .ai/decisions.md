# Architecture Decisions

Template:
- Date:
- Decision:
- Why:
- Consequence:
- Revisit conditions:

---
- Date: 2026-06-08
- Decision: The dashboard window hosts NO interactive terminal/conversation panes; the standalone canvas window (app/canvas.html) is the sole interactive surface. The dashboard Terminals tab is a read-only status list that routes keys to the canvas.
- Why: 5b-1 split the renderer into a self-sufficient PaneCore (pane-core.js) usable in a separate window; keeping a second inline renderer in terminals.js duplicated ~1.1k lines and two sources of truth. The user wants all terminals in the canvas.
- Consequence: terminals.js shrank ~1.9k lines; the cross-window PTY WS token now travels via a shared same-origin localStorage cache + the CanvasBus open message (fixed WS 403, no serve.py change). New conversations open the canvas window in the click gesture to avoid popup blocking.
- Revisit conditions: if a single-window inline mode is ever wanted again, or if serve.py stops omitting the PTY token from GET /api/ptys (then the shared token cache can be retired).

---
- Date: 2026-06-08
- Decision: Reviewer/executor convention — when source moves between modules, scoping the validation pytest to a hand-picked file list can mask regressions in sibling test files. Grep the whole tests/ tree for stale assertions on the moved symbols.
- Why: The convergence moved machinery from terminals.js to pane-core.js; three source-pinning tests in sibling files (test_terminals_fixes, _residual, _perf) kept asserting it in terminals.js and the narrow execute-phase gate missed them.
- Consequence: A broad targeted gate is run before declaring a structural-move task done.
- Revisit conditions: n/a (process note).
