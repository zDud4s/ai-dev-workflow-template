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
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Minimum seconds without file growth before MIRROR → ACQUIRING is allowed.
QUIESCENT_S = 2.5

# Seconds the ENGINE may sit fully idle (no turn in-flight, nothing pending)
# before the background watcher reclaims it via tick().
ENGINE_IDLE_S = 300


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
        # Owner stored alongside pending_turn so auto-acquire restores the correct owner.
        self.pending_owner: str | None = None
        # The turn currently running inside the engine (set when handed off, cleared on cede
        # or when mark_turn_done finds nothing pending).  Used to rescue lost turns on cede.
        self.in_flight_turn: dict | None = None
        # Write-timing fields for concurrency heuristics.
        self.last_mtime: float = 0.0        # last observed mtime of the .jsonl
        self.last_growth_ts: float = 0.0    # monotonic timestamp of last file growth
        self.lock = threading.RLock()
        # Callable () -> bool set by serve.py; returns True when the engine recently
        # produced stdout.  Tests set this per-session.  None means "unknown".
        self.engine_active_probe = None
        # Warnings accumulated during anomalous transitions (foreign writes, aborts).
        self.warnings: list = []
        # Set True when the .jsonl shrinks or disappears; signals log rotation / reset.
        self.terminated: bool = False
        # Monotonic timestamp (from the registry clock) at which the engine
        # became fully idle (no turn in-flight, nothing pending).  Reset to 0.0
        # whenever a turn goes in-flight.  Used by tick() to enforce ENGINE_IDLE_S.
        self.idle_since: float = 0.0

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
    def __init__(
        self,
        engine_factory,
        clock=time.monotonic,
        lock_acquire=None,
        lock_release=None,
        lock_heartbeat=None,
    ):
        """Initialize the registry.

        Args:
            engine_factory: Callable(sid, model) -> engine object.
            clock: Zero-argument callable returning a float timestamp.
                   Defaults to time.monotonic. Override in tests to avoid
                   real sleeps when exercising time-based transitions.
            lock_acquire: Optional callable(sid, owner) -> bool. When provided,
                called before spawning an engine. If it returns False the spawn
                is skipped and a warning is recorded on the session.
            lock_release: Optional callable(sid). Called after killing the engine
                in release() to free the cross-process file lock.
            lock_heartbeat: Optional callable(sid). Called by the background
                watcher for sessions in ENGINE or ACQUIRING state so the file
                lock stays alive between heartbeat intervals.
        """
        self._engine_factory = engine_factory
        self._clock = clock
        self._lock_acquire = lock_acquire
        self._lock_release = lock_release
        self._lock_heartbeat = lock_heartbeat
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
                    # File has been quiet long enough; safe to acquire — but only
                    # if the cross-process lock is available (or no lock hook set).
                    if self._lock_acquire is not None and not self._lock_acquire(sid, owner):
                        # Another process holds the lock; buffer the turn and wait.
                        s.pending_turn = turn
                        s.pending_model = model
                        s.pending_owner = owner
                        self._warn(s, "engine lock held by another process")
                        return "queued"
                    s.last_rendered_offset = s.last_size  # seed offset BEFORE writing
                    s.engine = self._engine_factory(sid, model)
                    s.engine_active_probe = getattr(s.engine, "recently_active", None)
                    s.state = SessionState.ACQUIRING
                    s.pending_turn = turn
                    s.pending_model = model
                    s.pending_owner = owner
                    s.owner = owner
                    if s.engine.is_ready():
                        self._promote_to_engine(s)
                    return "accepted"
                else:
                    # File is still being written; buffer and wait.
                    s.pending_turn = turn
                    s.pending_model = model
                    s.pending_owner = owner
                    return "queued"

            if s.state == SessionState.ENGINE:
                # Different tab is the registered owner; buffer, do not steal.
                if s.owner is not None and owner is not None and owner != s.owner:
                    s.pending_turn = turn
                    s.pending_model = model
                    s.pending_owner = owner
                    return "queued"
                # Our engine: submit if idle, buffer if busy.
                if not s.turn_in_flight:
                    if self._deliver(s, turn):
                        s.turn_in_flight = True
                        s.in_flight_turn = turn  # record for rescue if we later cede
                        s.idle_since = 0.0       # timer reset: engine is now busy
                        return "accepted"
                    # Delivery failed: engine reconciled to MIRROR. Report
                    # rejected so the frontend keeps the text for a retry.
                    return "rejected"
                else:
                    s.pending_turn = turn
                    s.pending_model = model
                    s.pending_owner = owner
                    return "queued"

            if s.state == SessionState.FOREIGN:
                # A foreign agent owns this session; buffer for later.
                s.pending_turn = turn
                s.pending_model = model
                s.pending_owner = owner
                return "queued"

            # ACQUIRING: pending slot is occupied by the buffered first turn,
            # so the guard above already catches a second attempt. Unreachable.
            return "rejected"  # pragma: no cover

    def _warn(self, s: Session, msg: str) -> None:
        """Record a warning, de-duplicating an immediately-repeated identical
        message. The SSE loop delivers warnings via a per-stream cursor and no
        longer clears the shared list, so a per-tick path (e.g. the quiet-tick
        'lock held by another process' branch) must not append the same line
        every second — that would grow the list without bound and spam panes."""
        if not s.warnings or s.warnings[-1] != msg:
            s.warnings.append(msg)

    def _deliver(self, s: Session, turn: dict) -> bool:
        """Submit a turn to the engine, failing safe.

        If the engine reports the write failed — submit() returns False or
        raises (dead process / closed stdin) — reconcile the session to MIRROR
        and record a warning rather than leaving a turn wedged in-flight
        forever (which tick()'s idle reclaim, gated on ``not turn_in_flight``,
        would never recover).  A submit() returning None keeps the historical
        "success" contract.  Returns True on success, False on a reported failure.
        Must be called with s.lock held.
        """
        try:
            ok = s.engine.submit(turn)
        except Exception:
            logger.warning("engine submit raised for %s; reconciling to mirror", s.sid, exc_info=True)
            ok = False
        if ok is False:
            self._warn(s, "engine write failed; turn not delivered")
            self._reconcile_to_mirror(s)
            if self._lock_release is not None:
                self._lock_release(s.sid)
            s.turn_in_flight = False
            s.in_flight_turn = None
            return False
        return True

    def _promote_to_engine(self, s: Session) -> None:
        s.state = SessionState.ENGINE
        first = s._pending_first_turn
        if first is not None:
            s._pending_first_turn = None
            if self._deliver(s, first):
                s.turn_in_flight = True
                s.in_flight_turn = first  # record for rescue if we later cede
                s.idle_since = 0.0        # timer reset: turn is now in-flight

    def mark_engine_ready(self, sid: str) -> None:
        s = self._sessions[sid]
        with s.lock:
            if s.state == SessionState.ACQUIRING:
                self._promote_to_engine(s)

    def mark_turn_done(self, sid: str) -> None:
        s = self._sessions[sid]
        with s.lock:
            # A terminal 'result' can arrive late — e.g. buffered in a
            # subprocess we already killed during a cede or release. Act only
            # while we still own a live engine; otherwise this would crash on a
            # None engine and, worse, advance last_rendered_offset on a file
            # the IDE now owns (letting the next foreign write slip through).
            if s.state != SessionState.ENGINE or s.engine is None:
                return
            s.turn_in_flight = False
            s.last_rendered_offset = s.last_size
            if s.pending_turn is not None:
                # Drain the queued turn into the engine; record it as the new in-flight turn.
                next_turn = s.pending_turn
                s.pending_turn = None
                if self._deliver(s, next_turn):
                    s.turn_in_flight = True
                    s.in_flight_turn = next_turn
                    s.idle_since = 0.0  # re-armed: idle timer resets while a turn is running
                # else: _deliver reconciled to MIRROR; nothing left in-flight.
            else:
                # Nothing pending; the engine is now fully idle — start the idle timer.
                s.in_flight_turn = None
                s.idle_since = self._clock()

    def release(self, sid: str) -> None:
        s = self._sessions[sid]
        with s.lock:
            if s.engine is not None:
                try:
                    s.engine.kill()
                except Exception:
                    # Kill of an already-dead engine is expected; log for diagnostics.
                    logger.debug("engine kill failed for %s", sid, exc_info=True)
            s.engine = None
            s.turn_in_flight = False
            s.last_rendered_offset = s.last_size   # de-dup on any engine exit
            s.state = SessionState.MIRROR
            # Free the cross-process file lock so another dashboard process may acquire.
            if self._lock_release is not None:
                self._lock_release(sid)

    def tick(self, sid: str) -> None:
        """Check whether the ENGINE for *sid* has been idle long enough to reclaim.

        Called by the background watcher on each poll cycle (or directly by tests
        with an injected clock).  If the session is in ENGINE state, has no turn
        in-flight, has nothing pending, and the idle timer has expired, the engine
        is released back to MIRROR.  All other states are a no-op.
        """
        s = self._sessions[sid]
        with s.lock:
            if (
                s.state == SessionState.ENGINE
                and not s.turn_in_flight
                and s.pending_turn is None
                and s.idle_since  # 0.0 means timer not started; skip
                and self._clock() - s.idle_since >= ENGINE_IDLE_S
            ):
                self.release(sid)

    def interrupt(self, sid: str) -> None:
        """Perform a turn-boundary reconcile for an ENGINE session whose running
        turn was interrupted (i.e. no terminal 'result' event was delivered).

        Per spec §5.4 this is NOT a state transition — the session stays in
        ENGINE.  It clears the in-flight bookkeeping and reconciles
        last_rendered_offset so that writing_ours() returns False immediately,
        closing the post-interrupt self-cede race.
        """
        s = self._sessions[sid]
        with s.lock:
            if s.state == SessionState.ENGINE:
                s.turn_in_flight = False
                s.in_flight_turn = None
                s.last_rendered_offset = s.last_size  # reconcile: no pending drain
                s.idle_since = self._clock()          # idle timer starts now

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
                # Kill of an already-dead engine is expected; log for diagnostics.
                logger.debug("engine kill failed for %s", s.sid, exc_info=True)
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
                if s.state == SessionState.MIRROR:
                    # No engine running; any write must be the IDE.
                    s.last_growth_ts = now
                    s.last_size = size
                    s.last_mtime = mtime
                    s.state = SessionState.FOREIGN

                elif s.state == SessionState.ACQUIRING:
                    # Growth during startup: ours only if the engine is producing stdout.
                    s.last_growth_ts = now
                    s.last_size = size
                    s.last_mtime = mtime
                    if self._engine_recently_active(s):
                        # Engine is initialising and writing — stay ACQUIRING.
                        pass
                    else:
                        # Cannot confirm the write is ours; abort and yield.
                        if s.engine is not None:
                            try:
                                s.engine.kill()
                            except Exception:
                                # Kill of an already-dead engine is expected; log for diagnostics.
                                logger.debug("engine kill failed for %s", sid, exc_info=True)
                        s.engine = None
                        s.turn_in_flight = False  # clear stale flag on abort
                        s.state = SessionState.FOREIGN
                        self._warn(s, "acquire aborted: foreign write")
                        # pending_turn is intentionally kept so auto-acquire can
                        # resubmit it once the session goes quiet again.

                elif s.state == SessionState.ENGINE:
                    # Determine ownership BEFORE updating last_size.
                    # Growth is ours when a turn is in-flight, the engine's stdout
                    # probe corroborates activity, OR we still have undrained bytes
                    # from a reply that finished a moment ago (offset-drain term).
                    ours = (
                        s.turn_in_flight
                        or self._engine_recently_active(s)
                        or (s.last_rendered_offset < s.last_size)
                    )
                    # Now update the size fields.
                    s.last_growth_ts = now
                    s.last_size = size
                    s.last_mtime = mtime
                    if ours:
                        # Account for our own output so we don't re-render it.
                        s.last_rendered_offset = s.last_size
                    else:
                        # Idle engine, fully drained, no recent stdout: the IDE wrote
                        # this — cede rather than risk overwriting the IDE's output.
                        if s.pending_turn is None and s.in_flight_turn is not None:
                            # Rescue the turn that was running in the engine so it is
                            # not lost; it will be resubmitted once the session quiets.
                            s.pending_turn = s.in_flight_turn
                        s.in_flight_turn = None
                        s.turn_in_flight = False  # clear stale flag on cede
                        if s.engine is not None:
                            try:
                                s.engine.kill()
                            except Exception:
                                # Kill of an already-dead engine is expected; log for diagnostics.
                                logger.debug("engine kill failed for %s", sid, exc_info=True)
                        s.engine = None
                        s.state = SessionState.FOREIGN
                        self._warn(s, "ceded: foreign write during idle engine")

                else:
                    # FOREIGN: IDE is still writing; just update the size fields.
                    s.last_growth_ts = now
                    s.last_size = size
                    s.last_mtime = mtime

                return

            # --- Quiet tick: size unchanged. ---
            s.last_mtime = mtime

            if s.state == SessionState.FOREIGN:
                elapsed = now - s.last_growth_ts
                if elapsed >= QUIESCENT_S:
                    if s.pending_turn is not None:
                        # Session has gone quiet and there is a buffered turn: acquire —
                        # unless the cross-process lock is held by another process.
                        if self._lock_acquire is not None and not self._lock_acquire(sid, s.pending_owner):
                            self._warn(s, "engine lock held by another process")
                            # Leave pending_turn in place; stay in FOREIGN and wait.
                        else:
                            # Restore the owner that submitted the pending turn.
                            model = s.pending_model or "claude-sonnet-4-6"
                            s.owner = s.pending_owner
                            s.last_rendered_offset = s.last_size
                            s.engine = self._engine_factory(sid, model)
                            s.engine_active_probe = getattr(s.engine, "recently_active", None)
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
