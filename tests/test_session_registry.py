# tests/test_session_registry.py
from __future__ import annotations
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard" / "scripts"))

import session_registry as sr


def test_new_session_starts_in_mirror():
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("sid-1", jsonl_path="/tmp/sid-1.jsonl")
    assert s.state == sr.SessionState.MIRROR
    assert s.sid == "sid-1"


class _FakeEngine:
    def __init__(self): self.turns = []; self._ready = True; self.killed = False
    def submit(self, turn): self.turns.append(turn)
    def interrupt(self): pass
    def kill(self): self.killed = True
    def is_ready(self): return self._ready


def _mk(state, *, turn_in_flight=False, offset=0, size=0):
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = state
    s.turn_in_flight = turn_in_flight
    s.last_rendered_offset = offset
    s.last_size = size
    return reg, s

def test_writing_ours_true_during_acquiring():
    reg, s = _mk(sr.SessionState.ACQUIRING)
    assert reg.writing_ours(s) is True

def test_writing_ours_true_while_turn_in_flight():
    reg, s = _mk(sr.SessionState.ENGINE, turn_in_flight=True)
    assert reg.writing_ours(s) is True

def test_writing_ours_true_while_draining_own_reply():
    # turno acabou (não in-flight) mas ainda há bytes nossos por drenar
    reg, s = _mk(sr.SessionState.ENGINE, turn_in_flight=False, offset=10, size=42)
    assert reg.writing_ours(s) is True

def test_writing_ours_false_when_engine_idle_and_drained():
    reg, s = _mk(sr.SessionState.ENGINE, turn_in_flight=False, offset=42, size=42)
    assert reg.writing_ours(s) is False

def test_writing_ours_false_in_mirror():
    reg, s = _mk(sr.SessionState.MIRROR)
    assert reg.writing_ours(s) is False
