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
        # Write-timing fields for concurrency heuristics (Phase 2).
        self.last_mtime: float = 0.0        # last observed mtime of the .jsonl
        self.last_growth_ts: float = 0.0    # monotonic timestamp of last file growth
        self.subscribers: set = set()
        self.lock = threading.RLock()

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

    def submit_turn(self, sid: str, turn: dict, model: str) -> str:
        s = self._sessions[sid]
        with s.lock:
            if s.state == SessionState.MIRROR:
                s.last_rendered_offset = s.last_size      # seed BEFORE writing
                s.engine = self._engine_factory(sid, model)
                s.state = SessionState.ACQUIRING
                s._pending_first_turn = turn
                if s.engine.is_ready():
                    self._promote_to_engine(s)
                return "accepted"
            if s.state == SessionState.ENGINE:
                s.engine.submit(turn)
                s.turn_in_flight = True                   # re-arm the baton
                return "accepted"
            # ACQUIRING: store as first turn (one is already on its way)
            s._pending_first_turn = turn
            return "accepted"

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
