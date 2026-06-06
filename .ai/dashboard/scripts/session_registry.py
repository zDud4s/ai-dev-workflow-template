# .ai/dashboard/scripts/session_registry.py
"""Per-session state machine for the dashboard unified-chat feature.

SessionRegistry tracks active sessions by sid (the Claude .jsonl stem). Each
Session moves through four states:
  mirror    – server tails the .jsonl; no dashboard engine running.
  acquiring – engine is starting; the first turn is buffered.
  engine    – engine is live; turns are submitted directly.
  foreign   – session is owned by another agent; local engine must not run.

Thread-safety: a registry-level RLock guards the sessions dict; a per-Session
RLock guards individual state transitions. The engine is injected via
engine_factory so the registry stays free of HTTP / CLI concerns.
"""
from __future__ import annotations
import enum
import threading
import time

# Minimum seconds without file growth before MIRROR → ACQUIRING is allowed.
QUIESCENT_S = 2.5


class SessionState(enum.Enum):
    MIRROR = "mirror"
    ACQUIRING = "acquiring"
    ENGINE = "engine"
    FOREIGN = "foreign"


class Session:
    def __init__(self, sid: str, jsonl_path: str):
        self.sid = sid
        self.jsonl_path = jsonl_path
        self.state = SessionState.MIRROR
        self.engine = None
        self.owner = None
        self.turn_in_flight = False
        self.last_size = 0
        self.last_rendered_offset = 0
        # Single-slot queue: the turn buffered while in ACQUIRING state.
        # Exposed via the _pending_first_turn property for backward compatibility.
        self.pending_turn: dict | None = None
        # Model stored alongside pending_turn so auto-acquire knows which model to use.
        self.pending_model: str | None = None
        # Write-timing fields for concurrency heuristics.
        self.last_mtime: float = 0.0        # last observed mtime of the .jsonl
        self.last_growth_ts: float = 0.0    # monotonic timestamp of last file growth
        self.subscribers: set = set()
        self.lock = threading.RLock()
        # Callable () -> bool set by serve.py; returns True when the engine recently
        # produced stdout.  Tests set this per-session.  None means "unknown".
        self.engine_active_probe = None
        # Warnings accumulated during anomalous transitions (foreign writes, aborts).
        self.warnings: list = []
        # Set True when the .jsonl shrinks or disappears; signals log rotation / reset.
        self.terminated: bool = False

    # ------------------------------------------------------------------
    # Backward-compatibility shim: existing code reads/writes
    # _pending_first_turn; this property forwards to pending_turn so
    # _promote_to_engine and submit_turn require no changes.
    # (Will be removed when callers are updated.)
    # ------------------------------------------------------------------

    @property
    def _pending_first_turn(self) -> dict | None:
        """Read-through alias for pending_turn."""
        return self.pending_turn

    @_pending_first_turn.setter
    def _pending_first_turn(self, value: dict | None) -> None:
        self.pending_turn = value


class SessionRegistry:
    def __init__(self, engine_factory, clock=time.monotonic):
        """Initialize the registry.

        Args:
            engine_factory: Callable(sid, model) -> engine object.
            clock: Zero-argument callable returning a float timestamp.
                   Defaults to time.monotonic. Override in tests to avoid
                   real sleeps when exercising time-based transitions.
        """
        self._engine_factory = engine_factory
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def get_or_create(self, sid: str, jsonl_path: str) -> Session:
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                s = Session(sid, jsonl_path)
                self._sessions[sid] = s
            return s

    def writing_ours(self, s: Session) -> bool:
        """True iff state==ACQUIRING, OR state==ENGINE and (turn in-flight
        OR still draining our own reply: last_rendered_offset < last_size).
        False in all other cases (MIRROR, or ENGINE idle and drained)."""
        if s.state == SessionState.ACQUIRING:
            return True
        if s.state == SessionState.ENGINE:
            return s.turn_in_flight or s.last_rendered_offset < s.last_size
        return False

    def submit_turn(self, sid: str, turn: dict, model: str, owner=None) -> str:
        """Route an incoming turn to the session's state machine.

        Returns:
            "accepted"  – turn will be processed immediately or on next ready event.
            "queued"    – turn stored in the single pending slot; caller may retry later.
            "rejected"  – slot already occupied; discard this turn.
        """
        s = self._sessions[sid]
        with s.lock:
            # Single-slot guard: if a turn is already waiting, reject immediately.
            if s.pending_turn is not None:
                return "rejected"

            if s.state == SessionState.MIRROR:
                elapsed = self._clock() - s.last_growth_ts
                if elapsed >= QUIESCENT_S:
                    # File has been quiet long enough; safe to acquire.
                    s.last_rendered_offset = s.last_size  # seed offset BEFORE writing
                    s.engine = self._engine_factory(sid, model)
                    s.state = SessionState.ACQUIRING
                    s.pending_turn = turn
                    s.pending_model = model
                    s.owner = owner
                    if s.engine.is_ready():
                        self._promote_to_engine(s)
                    return "accepted"
                else:
                    # File is still being written; buffer and wait.
                    s.pending_turn = turn
                    s.pending_model = model
                    return "queued"

            if s.state == SessionState.ENGINE:
                # Different tab is the registered owner; buffer, do not steal.
                if s.owner is not None and owner is not None and owner != s.owner:
                    s.pending_turn = turn
                    s.pending_model = model
                    return "queued"
                # Our engine: submit if idle, buffer if busy.
                if not s.turn_in_flight:
                    s.engine.submit(turn)
                    s.turn_in_flight = True
                    return "accepted"
                else:
                    s.pending_turn = turn
                    s.pending_model = model
                    return "queued"

            if s.state == SessionState.FOREIGN:
                # A foreign agent owns this session; buffer for later.
                s.pending_turn = turn
                s.pending_model = model
                return "queued"

            # ACQUIRING: pending slot is occupied by the buffered first turn,
            # so the guard above already catches a second attempt. Unreachable.
            return "rejected"  # pragma: no cover

    def _promote_to_engine(self, s: Session) -> None:
        s.state = SessionState.ENGINE
        first = s._pending_first_turn
        if first is not None:
            s.engine.submit(first)
            s.turn_in_flight = True
            s._pending_first_turn = None

    def mark_engine_ready(self, sid: str) -> None:
        s = self._sessions[sid]
        with s.lock:
            if s.state == SessionState.ACQUIRING:
                self._promote_to_engine(s)

    def mark_turn_done(self, sid: str) -> None:
        s = self._sessions[sid]
        with s.lock:
            s.turn_in_flight = False
            s.last_rendered_offset = s.last_size

    def release(self, sid: str) -> None:
        s = self._sessions[sid]
        with s.lock:
            if s.engine is not None:
                try: s.engine.kill()
                except Exception: pass
            s.engine = None
            s.turn_in_flight = False
            s.last_rendered_offset = s.last_size   # de-dup on any engine exit
            s.state = SessionState.MIRROR

    # ------------------------------------------------------------------
    # Engine-activity helper
    # ------------------------------------------------------------------

    def _engine_recently_active(self, s: Session) -> bool:
        """Return True when the session's engine probe reports recent stdout activity."""
        return bool(s.engine_active_probe and s.engine_active_probe())

    # ------------------------------------------------------------------
    # File-watcher entry points
    # ------------------------------------------------------------------

    def _reconcile_to_mirror(self, s: Session) -> None:
        """Kill engine if running, set state to MIRROR and mark as terminated."""
        if s.engine is not None:
            try:
                s.engine.kill()
            except Exception:
                # Swallow kill errors — the engine may already be gone.
                pass
        s.engine = None
        s.state = SessionState.MIRROR
        s.terminated = True

    def note_jsonl_growth(self, sid: str, size: int, mtime: float) -> None:
        """Called by the background watcher every tick with the session .jsonl's
        current byte size and mtime.  Detects whether growth belongs to our engine
        or to the IDE and drives state transitions accordingly.

        Safety rule: when growth cannot be confidently attributed to our engine we
        always cede — kill the engine, re-queue the operator's turn, record a
        warning — rather than risk overwriting the IDE's output.
        """
        s = self._sessions[sid]
        with s.lock:
            now = self._clock()

            # --- Shrink / rotation: file got smaller → treat as a reset. ---
            if size < s.last_size:
                s.last_size = size
                s.last_mtime = mtime
                self._reconcile_to_mirror(s)
                return

            # --- Growth: new bytes arrived. ---
            if size > s.last_size:
                s.last_growth_ts = now
                s.last_size = size
                s.last_mtime = mtime

                if s.state == SessionState.MIRROR:
                    # No engine running; any write must be the IDE.
                    s.state = SessionState.FOREIGN

                elif s.state == SessionState.ACQUIRING:
                    # Growth during startup: ours only if the engine is producing stdout.
                    if self._engine_recently_active(s):
                        # Engine is initialising and writing — stay ACQUIRING.
                        pass
                    else:
                        # Cannot confirm the write is ours; abort and yield.
                        if s.engine is not None:
                            try:
                                s.engine.kill()
                            except Exception:
                                # Engine may already have exited.
                                pass
                        s.engine = None
                        s.state = SessionState.FOREIGN
                        s.warnings.append("acquire aborted: foreign write")
                        # pending_turn is intentionally kept so auto-acquire can
                        # resubmit it once the session goes quiet again.

                elif s.state == SessionState.ENGINE:
                    # Growth is ours if a turn is in-flight OR the probe corroborates it.
                    ours = s.turn_in_flight or self._engine_recently_active(s)
                    if ours:
                        # Account for our own output so we don't re-render it.
                        s.last_rendered_offset = s.last_size
                    else:
                        # Idle engine, no recent stdout: the IDE wrote this — cede.
                        if s.engine is not None:
                            try:
                                s.engine.kill()
                            except Exception:
                                # Engine may already have exited.
                                pass
                        s.engine = None
                        s.state = SessionState.FOREIGN
                        s.warnings.append("ceded: foreign write during idle engine")

                # FOREIGN: IDE is still writing; fields already updated above.

                return

            # --- Quiet tick: size unchanged. ---
            s.last_mtime = mtime

            if s.state == SessionState.FOREIGN:
                elapsed = now - s.last_growth_ts
                if elapsed >= QUIESCENT_S:
                    if s.pending_turn is not None:
                        # Session has gone quiet and there is a buffered turn: acquire.
                        model = s.pending_model or "claude-sonnet-4-6"
                        s.last_rendered_offset = s.last_size
                        s.engine = self._engine_factory(sid, model)
                        s.state = SessionState.ACQUIRING
                        if s.engine.is_ready():
                            self._promote_to_engine(s)
                    else:
                        # No pending turn; relax back to mirroring.
                        s.state = SessionState.MIRROR

    def note_jsonl_gone(self, sid: str) -> None:
        """Called when the watcher finds the .jsonl absent (deleted or rotated away).
        Reconciles to MIRROR + terminated, killing the engine if one is running.
        """
        s = self._sessions[sid]
        with s.lock:
            self._reconcile_to_mirror(s)
