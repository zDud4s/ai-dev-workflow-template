# .ai/dashboard/scripts/session_registry.py
from __future__ import annotations
import enum, threading


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
