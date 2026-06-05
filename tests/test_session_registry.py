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


def test_submit_turn_from_mirror_acquires_engine_and_seeds_offset():
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.last_size = 128            # ficheiro já tinha histórico do IDE
    status = reg.submit_turn("s", {"text": "olá"}, model="claude-sonnet-4-6")
    assert status == "accepted"
    assert s.state == sr.SessionState.ENGINE        # _FakeEngine.is_ready() True → vai direto
    assert s.last_rendered_offset == 128            # semeado com o size atual
    assert s.turn_in_flight is True
    assert eng.turns == [{"text": "olá"}]

def test_submit_turn_when_engine_idle_rearms_in_flight():
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    reg.submit_turn("s", {"text": "a"}, model="m")
    reg.mark_turn_done("s")                          # motor ocioso, drenado
    assert reg.writing_ours(s) is False
    reg.submit_turn("s", {"text": "b"}, model="m")   # 2.º turno no motor vivo
    assert s.turn_in_flight is True
    assert reg.writing_ours(s) is True
    assert eng.turns == [{"text": "a"}, {"text": "b"}]

def test_acquiring_dwell_until_engine_ready():
    """Motor não-pronto fica em ACQUIRING; mark_engine_ready promove a ENGINE
    e submete o primeiro turno exatamente uma vez."""
    eng = _FakeEngine(); eng._ready = False
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    reg.submit_turn("s", {"text": "olá"}, model="m")
    assert s.state == sr.SessionState.ACQUIRING
    assert eng.turns == []
    eng._ready = True
    reg.mark_engine_ready("s")
    assert s.state == sr.SessionState.ENGINE
    assert eng.turns == [{"text": "olá"}]
    assert s.turn_in_flight is True


def test_release_kills_engine_and_returns_to_mirror():
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    reg.submit_turn("s", {"text": "x"}, model="m")
    reg.release("s")
    assert s.state == sr.SessionState.MIRROR
    assert eng.killed is True
    assert s.engine is None

def test_release_reconciles_offset_to_size():
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    reg.submit_turn("s", {"text": "x"}, model="m")
    s.last_size = 999
    reg.release("s")
    assert s.last_rendered_offset == 999   # tail recomeça daqui, sem repetir
