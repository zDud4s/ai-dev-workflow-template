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
    # turn finished (not in-flight) but our bytes are not yet drained
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
    s.last_size = 128            # file already had history from the IDE
    status = reg.submit_turn("s", {"text": "olá"}, model="claude-sonnet-4-6")
    assert status == "accepted"
    assert s.state == sr.SessionState.ENGINE        # _FakeEngine.is_ready() True → vai direto
    assert s.last_rendered_offset == 128            # seeded with the current size
    assert s.turn_in_flight is True
    assert eng.turns == [{"text": "olá"}]

def test_submit_turn_when_engine_idle_rearms_in_flight():
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    reg.submit_turn("s", {"text": "a"}, model="m")
    reg.mark_turn_done("s")                          # engine idle, drained
    assert reg.writing_ours(s) is False
    reg.submit_turn("s", {"text": "b"}, model="m")   # 2nd turn on the live engine
    assert s.turn_in_flight is True
    assert reg.writing_ours(s) is True
    assert eng.turns == [{"text": "a"}, {"text": "b"}]

def test_acquiring_dwell_until_engine_ready():
    """A not-ready engine stays in ACQUIRING; mark_engine_ready promotes to ENGINE
    and submits the first turn exactly once."""
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


# ---------------------------------------------------------------------------
# New tests for Phase 1 schema groundwork: FOREIGN state + pending slot + clock
# ---------------------------------------------------------------------------

def test_foreign_state_value():
    """SessionState.FOREIGN must have value 'foreign'."""
    assert sr.SessionState.FOREIGN.value == "foreign"


def test_new_session_pending_turn_is_none():
    """A freshly created Session has pending_turn=None."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("fresh", jsonl_path="/tmp/fresh.jsonl")
    assert s.pending_turn is None


def test_new_session_timing_fields_are_zero():
    """A freshly created Session has last_mtime==0.0 and last_growth_ts==0.0."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("fresh2", jsonl_path="/tmp/fresh2.jsonl")
    assert s.last_mtime == 0.0
    assert s.last_growth_ts == 0.0


def test_registry_injectable_clock():
    """SessionRegistry(engine_factory, clock=...) stores and exposes the clock."""
    fixed_clock = lambda: 100.0
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine(), clock=fixed_clock)
    assert reg._clock() == 100.0


def test_registry_default_clock_is_monotonic():
    """The default registry clock is time.monotonic."""
    import time
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    assert reg._clock is time.monotonic


# ---------------------------------------------------------------------------
# Tests for submit_turn accepted/queued/rejected routing
# ---------------------------------------------------------------------------

# Constant must be defined at module level.
def test_quiescent_s_constant():
    assert sr.QUIESCENT_S == 2.5


def _mk_timed(state, *, turn_in_flight=False, owner=None, pending_turn=None,
              last_growth_ts=0.0, clock=None):
    """Build a registry+session for submit_turn tests with clock control."""
    if clock is None:
        clock = lambda: 100.0  # default: 100 seconds elapsed, well beyond QUIESCENT_S
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng, clock=clock)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = state
    s.turn_in_flight = turn_in_flight
    s.owner = owner
    s.pending_turn = pending_turn
    s.last_growth_ts = last_growth_ts
    return reg, s, eng


# --- pending_turn already set → always rejected ---

def test_pending_already_set_returns_rejected_from_mirror():
    """If pending_turn is already set, submit_turn returns 'rejected' regardless of state."""
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, pending_turn={"text": "old"})
    result = reg.submit_turn("s", {"text": "new"}, model="m")
    assert result == "rejected"


def test_pending_already_set_returns_rejected_from_acquiring():
    """ACQUIRING with pending_turn set → rejected (slot occupied)."""
    reg, s, eng = _mk_timed(sr.SessionState.ACQUIRING, pending_turn={"text": "buffered"})
    result = reg.submit_turn("s", {"text": "second"}, model="m")
    assert result == "rejected"


def test_pending_already_set_returns_rejected_from_engine():
    """ENGINE with pending_turn set → rejected."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=True,
                             pending_turn={"text": "queued"})
    result = reg.submit_turn("s", {"text": "extra"}, model="m")
    assert result == "rejected"


def test_pending_already_set_returns_rejected_from_foreign():
    """FOREIGN with pending_turn set → rejected."""
    reg, s, eng = _mk_timed(sr.SessionState.FOREIGN, pending_turn={"text": "q"})
    result = reg.submit_turn("s", {"text": "new"}, model="m")
    assert result == "rejected"


# --- MIRROR branch ---

def test_mirror_quiet_returns_accepted_and_transitions_to_acquiring():
    """MIRROR + quiescent → accepted, state becomes ACQUIRING, owner set, pending_turn set."""
    # clock returns 100.0, last_growth_ts=0.0 → elapsed=100 >= QUIESCENT_S
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=0.0, clock=lambda: 100.0)
    eng._ready = False  # keep in ACQUIRING so we can inspect it
    result = reg.submit_turn("s", {"text": "hello"}, model="m", owner="tab-A")
    assert result == "accepted"
    assert s.state == sr.SessionState.ACQUIRING
    assert s.owner == "tab-A"
    assert s.pending_turn == {"text": "hello"}


def test_mirror_quiet_spawns_engine():
    """MIRROR + quiescent → engine factory is called (engine object assigned)."""
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=0.0, clock=lambda: 100.0)
    eng._ready = False
    reg.submit_turn("s", {"text": "hi"}, model="m")
    assert s.engine is not None


def test_mirror_quiet_engine_ready_promotes_to_engine():
    """MIRROR + quiescent + engine already ready → state promoted to ENGINE."""
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=0.0, clock=lambda: 100.0)
    # eng._ready is True by default
    result = reg.submit_turn("s", {"text": "hi"}, model="m", owner="tab-A")
    assert result == "accepted"
    assert s.state == sr.SessionState.ENGINE
    # pending_turn flushed by _promote_to_engine
    assert s.pending_turn is None
    assert eng.turns == [{"text": "hi"}]
    assert s.turn_in_flight is True


def test_mirror_quiet_seeds_last_rendered_offset():
    """MIRROR + quiescent → last_rendered_offset seeded from last_size."""
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=0.0, clock=lambda: 100.0)
    eng._ready = False
    s.last_size = 256
    reg.submit_turn("s", {"text": "x"}, model="m")
    assert s.last_rendered_offset == 256


def test_mirror_not_quiet_returns_queued_stays_mirror():
    """MIRROR + not quiescent → queued, state stays MIRROR."""
    # clock=99.0, last_growth_ts=97.0 → elapsed=2.0 < QUIESCENT_S=2.5
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=97.0, clock=lambda: 99.0)
    result = reg.submit_turn("s", {"text": "soon"}, model="m")
    assert result == "queued"
    assert s.state == sr.SessionState.MIRROR
    assert s.pending_turn == {"text": "soon"}


def test_mirror_not_quiet_does_not_spawn_engine():
    """MIRROR + not quiescent → no engine spawned."""
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=97.0, clock=lambda: 99.0)
    reg.submit_turn("s", {"text": "soon"}, model="m")
    assert s.engine is None


# --- ENGINE branch ---

def test_engine_idle_no_owner_conflict_returns_accepted():
    """ENGINE + idle (no pending, no in-flight) → accepted, engine.submit called."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=False)
    s.engine = eng
    result = reg.submit_turn("s", {"text": "go"}, model="m", owner="tab-A")
    assert result == "accepted"
    assert eng.turns == [{"text": "go"}]
    assert s.turn_in_flight is True


def test_engine_busy_returns_queued():
    """ENGINE + turn_in_flight=True → queued, pending_turn set."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=True)
    s.engine = eng
    result = reg.submit_turn("s", {"text": "wait"}, model="m", owner="tab-A")
    assert result == "queued"
    assert s.pending_turn == {"text": "wait"}
    assert eng.turns == []  # submit not called


def test_engine_different_owner_returns_queued():
    """ENGINE + different owner → queued, owner NOT changed."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=False, owner="tab-A")
    s.engine = eng
    result = reg.submit_turn("s", {"text": "mine"}, model="m", owner="tab-B")
    assert result == "queued"
    assert s.owner == "tab-A"   # unchanged
    assert s.pending_turn == {"text": "mine"}
    assert eng.turns == []


def test_engine_different_owner_with_pending_returns_rejected():
    """ENGINE + different owner + pending_turn already set → rejected."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=False, owner="tab-A",
                             pending_turn={"text": "already-queued"})
    s.engine = eng
    result = reg.submit_turn("s", {"text": "also-mine"}, model="m", owner="tab-B")
    assert result == "rejected"


def test_engine_same_owner_idle_returns_accepted():
    """ENGINE + same owner, idle → accepted (same as no-owner case)."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=False, owner="tab-A")
    s.engine = eng
    result = reg.submit_turn("s", {"text": "cont"}, model="m", owner="tab-A")
    assert result == "accepted"
    assert eng.turns == [{"text": "cont"}]


def test_engine_no_owner_session_idle_returns_accepted():
    """ENGINE + s.owner is None, any submitter → accepted (owner not set in session)."""
    reg, s, eng = _mk_timed(sr.SessionState.ENGINE, turn_in_flight=False, owner=None)
    s.engine = eng
    result = reg.submit_turn("s", {"text": "anon"}, model="m", owner=None)
    assert result == "accepted"


# --- FOREIGN branch ---

def test_foreign_returns_queued_and_sets_pending():
    """FOREIGN → queued, pending_turn set."""
    reg, s, eng = _mk_timed(sr.SessionState.FOREIGN)
    result = reg.submit_turn("s", {"text": "pend"}, model="m")
    assert result == "queued"
    assert s.pending_turn == {"text": "pend"}


# --- signature accepts owner keyword ---

def test_submit_turn_accepts_owner_kwarg():
    """submit_turn(sid, turn, model, owner=...) must not raise."""
    reg, s, eng = _mk_timed(sr.SessionState.MIRROR, last_growth_ts=0.0, clock=lambda: 100.0)
    eng._ready = False
    result = reg.submit_turn("s", {"text": "x"}, model="m", owner="tab-Z")
    assert result in {"accepted", "queued", "rejected"}


# ---------------------------------------------------------------------------
# note_jsonl_growth tests — written first (TDD: all fail before implementation)
# ---------------------------------------------------------------------------

def _mk_growth(state, *, clock_val=100.0, last_size=100, last_mtime=1.0,
               last_growth_ts=0.0, last_rendered_offset=0,
               turn_in_flight=False, pending_turn=None, pending_model=None,
               engine_active_probe=None, engine=None):
    """Build a registry+session configured for note_jsonl_growth tests."""
    t = clock_val
    reg = sr.SessionRegistry(
        engine_factory=lambda sid, model: _FakeEngine(),
        clock=lambda: t,
    )
    # Use a mutable cell so we can advance the clock inside a test.
    clock_cell = [clock_val]
    reg._clock = lambda: clock_cell[0]

    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = state
    s.last_size = last_size
    s.last_mtime = last_mtime
    s.last_growth_ts = last_growth_ts
    s.last_rendered_offset = last_rendered_offset
    s.turn_in_flight = turn_in_flight
    s.pending_turn = pending_turn
    s.pending_model = pending_model if pending_model is not None else "m"
    s.engine_active_probe = engine_active_probe
    if engine is not None:
        s.engine = engine
    return reg, s, clock_cell


# --- MIRROR + growth → FOREIGN ---

def test_mirror_growth_transitions_to_foreign():
    """MIRROR: when the .jsonl grows it must be the IDE; transition to FOREIGN."""
    reg, s, clock = _mk_growth(sr.SessionState.MIRROR, last_size=100)
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.FOREIGN
    assert s.last_size == 200
    assert s.last_mtime == 2.0


# --- FOREIGN + quiet ≥ QUIESCENT_S + no pending → MIRROR ---

def test_foreign_quiet_no_pending_transitions_to_mirror():
    """FOREIGN + quiet tick after QUIESCENT_S with no pending turn → MIRROR."""
    reg, s, clock = _mk_growth(
        sr.SessionState.FOREIGN,
        last_size=100, last_growth_ts=90.0,  # growth happened at t=90
        clock_val=90.0,
    )
    # First tick: growth at t=90 sets last_growth_ts
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.FOREIGN

    # Advance clock past quiescence threshold; send a quiet tick (same size)
    clock[0] = 90.0 + sr.QUIESCENT_S + 0.1
    reg.note_jsonl_growth("s", size=200, mtime=3.0)
    assert s.state == sr.SessionState.MIRROR


# --- FOREIGN + quiet ≥ QUIESCENT_S + pending → ACQUIRING + engine spawned ---

def test_foreign_quiet_with_pending_auto_acquires():
    """FOREIGN + quiet tick after QUIESCENT_S + pending turn → ACQUIRING, engine set."""
    spawned = []

    def factory(sid, model):
        e = _FakeEngine()
        e._ready = False   # stay in ACQUIRING so we can check
        spawned.append(e)
        return e

    reg = sr.SessionRegistry(engine_factory=factory, clock=lambda: 90.0)
    clock_cell = [90.0]
    reg._clock = lambda: clock_cell[0]

    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = sr.SessionState.FOREIGN
    s.last_size = 100
    s.last_mtime = 1.0
    s.last_growth_ts = 90.0
    s.pending_turn = {"text": "waiting"}
    s.pending_model = "claude-sonnet-4-6"

    # Growth tick establishes last_growth_ts = 90
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.FOREIGN

    # Quiet tick after quiescence
    clock_cell[0] = 90.0 + sr.QUIESCENT_S + 0.1
    reg.note_jsonl_growth("s", size=200, mtime=3.0)

    assert s.state == sr.SessionState.ACQUIRING
    assert s.engine is not None
    assert len(spawned) == 1


# --- ENGINE idle + growth → FOREIGN, engine killed, warning recorded ---

def test_engine_idle_foreign_growth_cedes():
    """ENGINE idle (turn_in_flight=False, probe=False) + growth → FOREIGN, kill, warning."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=100, last_rendered_offset=100,
        turn_in_flight=False,
        engine=eng,
        engine_active_probe=lambda: False,
    )
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.FOREIGN
    assert eng.killed is True
    assert s.engine is None
    assert len(s.warnings) >= 1
    assert any("ceded" in w for w in s.warnings)


# --- ENGINE, turn_in_flight=True + growth → stays ENGINE, offset advances ---

def test_engine_turn_in_flight_growth_stays_engine():
    """ENGINE with turn_in_flight=True + growth → stays ENGINE, last_rendered_offset updated."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=100, last_rendered_offset=100,
        turn_in_flight=True,
        engine=eng,
        engine_active_probe=lambda: False,
    )
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.ENGINE
    assert eng.killed is False
    assert s.last_rendered_offset == 200
    assert s.warnings == []


# --- ENGINE idle but probe=True + growth → stays ENGINE ---

def test_engine_idle_probe_true_growth_stays_engine():
    """ENGINE idle but engine_active_probe returns True → corroborated ours, stay ENGINE."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=100, last_rendered_offset=100,
        turn_in_flight=False,
        engine=eng,
        engine_active_probe=lambda: True,
    )
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.ENGINE
    assert eng.killed is False
    assert s.last_rendered_offset == 200
    assert s.warnings == []


# --- ACQUIRING + growth, probe False → FOREIGN (abort) + warning ---

def test_acquiring_growth_probe_false_aborts_to_foreign():
    """ACQUIRING + growth + probe False → engine killed, FOREIGN, warning, pending kept."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ACQUIRING,
        last_size=100,
        engine=eng,
        engine_active_probe=lambda: False,
        pending_turn={"text": "my turn"},
    )
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.FOREIGN
    assert eng.killed is True
    assert s.engine is None
    assert len(s.warnings) >= 1
    assert any("acquire aborted" in w for w in s.warnings)
    # Turn must be preserved for future auto-acquire
    assert s.pending_turn == {"text": "my turn"}


# --- ACQUIRING + growth, probe True → stays ACQUIRING ---

def test_acquiring_growth_probe_true_stays_acquiring():
    """ACQUIRING + growth + probe True (our engine is producing stdout) → stays ACQUIRING."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ACQUIRING,
        last_size=100,
        engine=eng,
        engine_active_probe=lambda: True,
        pending_turn={"text": "my turn"},
    )
    reg.note_jsonl_growth("s", size=200, mtime=2.0)
    assert s.state == sr.SessionState.ACQUIRING
    assert eng.killed is False
    assert s.warnings == []


# --- Shrink → MIRROR + terminated + engine killed ---

def test_shrink_transitions_to_mirror_terminated():
    """Shrink (size < last_size) → MIRROR, terminated=True, engine killed."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=500,
        engine=eng,
    )
    reg.note_jsonl_growth("s", size=100, mtime=5.0)
    assert s.state == sr.SessionState.MIRROR
    assert s.terminated is True
    assert eng.killed is True
    assert s.engine is None
    assert s.last_size == 100
    assert s.last_mtime == 5.0


# --- note_jsonl_gone → MIRROR + terminated ---

def test_note_jsonl_gone_mirror_terminated():
    """note_jsonl_gone: file disappeared → MIRROR + terminated=True, engine killed if any."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=200,
        engine=eng,
    )
    reg.note_jsonl_gone("s")
    assert s.state == sr.SessionState.MIRROR
    assert s.terminated is True
    assert eng.killed is True
    assert s.engine is None
