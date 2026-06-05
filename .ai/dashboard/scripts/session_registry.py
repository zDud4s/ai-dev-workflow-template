# .ai/dashboard/scripts/session_registry.py
"""Per-session state machine for the dashboard unified-chat feature.

SessionRegistry tracks active sessions by sid (the Claude .jsonl stem). Each
Session moves through three states:
  mirror    – server tails the .jsonl; no dashboard engine running.
  acquiring – engine is starting; the first turn is buffered.
  engine    – engine is live; turns are submitted directly.
FOREIGN is reserved for Phase 2.

Thread-safety: a registry-level RLock guards the sessions dict; a per-Session
RLock guards individual state transitions. The engine is injected via
engine_factory so the registry stays free of HTTP / CLI concerns.
"""
from __future__ import annotations
import enum
import threading


class SessionState(enum.Enum):
    MIRROR = "mirror"
    ACQUIRING = "acquiring"
    ENGINE = "engine"
    # FOREIGN entra na Fase 2.


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
        self._pending_first_turn = None   # turno guardado enquanto em ACQUIRING
        self.subscribers: set = set()
        self.lock = threading.RLock()


class SessionRegistry:
    def __init__(self, engine_factory):
        self._engine_factory = engine_factory
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
        """Verdadeiro sse state==ACQUIRING, OU state==ENGINE e (turno em voo
        OU ainda a drenar a própria resposta: last_rendered_offset < last_size).
        Falso em qualquer outro caso (MIRROR, ou ENGINE ocioso e drenado)."""
        if s.state == SessionState.ACQUIRING:
            return True
        if s.state == SessionState.ENGINE:
            return s.turn_in_flight or s.last_rendered_offset < s.last_size
        return False

    def submit_turn(self, sid: str, turn: dict, model: str) -> str:
        s = self._sessions[sid]
        with s.lock:
            if s.state == SessionState.MIRROR:
                s.last_rendered_offset = s.last_size      # semear ANTES de escrever
                s.engine = self._engine_factory(sid, model)
                s.state = SessionState.ACQUIRING
                s._pending_first_turn = turn
                if s.engine.is_ready():
                    self._promote_to_engine(s)
                return "accepted"
            if s.state == SessionState.ENGINE:
                s.engine.submit(turn)
                s.turn_in_flight = True                   # re-arma a posse
                return "accepted"
            # ACQUIRING: guarda como primeiro turno (já há um a caminho)
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
            s.last_rendered_offset = s.last_size   # de-dup em qualquer saída de engine
            s.state = SessionState.MIRROR
