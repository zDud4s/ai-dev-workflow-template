# tests/test_session_registry.py
from __future__ import annotations
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard" / "scripts"))

from server import session_registry as sr


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


# --- note_jsonl_gone during brand-new create-on-first-turn startup ---
#
# Regression guard: a freshly-spawned engine (ACQUIRING, transcript not yet
# written) must survive the watcher's absent-file poll. The watcher stats
# ~/.claude/projects/<slug>/<sid>.jsonl every second; for a create-on-first-turn
# session that file does not exist until claude has received the first turn and
# emitted SessionStart (a few seconds later). Reconciling on that expected gap
# killed the engine before it could write the transcript, leaving /stream stuck
# at 404 forever.

def test_note_jsonl_gone_preserves_acquiring_engine_before_first_write():
    """ACQUIRING + engine present + last_size==0 (never observed): a missing
    transcript is the expected pre-creation state, so note_jsonl_gone must NOT
    kill the engine or leave MIRROR/terminated."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ACQUIRING,
        last_size=0,          # transcript has never been observed yet
        engine=eng,
    )
    reg.note_jsonl_gone("s")
    assert s.state == sr.SessionState.ACQUIRING, "must stay ACQUIRING while engine starts up"
    assert eng.killed is False, "freshly-spawned engine must not be killed"
    assert s.engine is eng
    assert s.terminated is False


def test_note_jsonl_gone_preserves_engine_before_first_write():
    """ENGINE (just promoted, first turn delivered) + last_size==0: claude is
    about to create the transcript, so an absent file must not reconcile/kill."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=0,
        engine=eng,
    )
    reg.note_jsonl_gone("s")
    assert s.state == sr.SessionState.ENGINE
    assert eng.killed is False
    assert s.engine is eng
    assert s.terminated is False


def test_note_jsonl_gone_still_reconciles_after_transcript_existed():
    """Once the transcript has been observed (last_size>0), a later disappearance
    is a real rotation/deletion and MUST still reconcile to MIRROR + terminated."""
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=200,        # transcript existed at least once
        engine=eng,
    )
    reg.note_jsonl_gone("s")
    assert s.state == sr.SessionState.MIRROR
    assert s.terminated is True
    assert eng.killed is True
    assert s.engine is None


def test_note_jsonl_gone_reconciles_mirror_with_no_engine():
    """MIRROR with no engine and last_size==0 still reconciles (no engine to
    preserve); the guard only protects sessions whose own engine is starting."""
    reg, s, clock = _mk_growth(sr.SessionState.MIRROR, last_size=0, engine=None)
    reg.note_jsonl_gone("s")
    assert s.state == sr.SessionState.MIRROR
    assert s.terminated is True


def test_first_turn_delivered_on_promotion_despite_gone_poll():
    """End-to-end of the create-on-first-turn fix with a fake engine:

    1. submit_turn on a brand-new MIRROR session spawns a not-yet-ready engine,
       buffering the first turn (ACQUIRING).
    2. The watcher polls before claude has written the transcript -> note_jsonl_gone
       fires while last_size==0; the engine must SURVIVE (this was the bug).
    3. The engine becomes ready -> mark_engine_ready promotes ACQUIRING -> ENGINE
       and the buffered first turn is delivered to the engine exactly once.
    """
    eng = _FakeEngine()
    eng._ready = False
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    # Brand-new create-on-first-turn: file never seen (last_size==0) and quiet
    # (last_growth_ts==0 -> elapsed huge), so submit_turn acquires immediately.
    status = reg.submit_turn("s", {"text": "hi"}, model="claude-haiku-4-5", owner="diag")
    assert status == "accepted"
    assert s.state == sr.SessionState.ACQUIRING
    assert eng.turns == []                       # buffered, not yet delivered

    # Watcher poll lands during the transcript-creation gap.
    reg.note_jsonl_gone("s")
    assert s.state == sr.SessionState.ACQUIRING  # engine preserved (regression guard)
    assert eng.killed is False

    # Engine comes up; promotion flushes the buffered first turn.
    eng._ready = True
    reg.mark_engine_ready("s")
    assert s.state == sr.SessionState.ENGINE
    assert eng.turns == [{"text": "hi"}]         # delivered exactly once
    assert s.turn_in_flight is True


# ---------------------------------------------------------------------------
# Fix 1 (RED): offset-drain term in ENGINE growth attribution
# ---------------------------------------------------------------------------

def test_engine_offset_not_drained_growth_stays_engine():
    """Fix 1 RED: ENGINE, turn_in_flight=False, probe=False, but last_rendered_offset < last_size
    (reply bytes not yet drained from the previous turn) — growth must be treated as ours
    and the session must STAY ENGINE with no warning and no engine kill.

    Before Fix 1 this test FAILS because the code omits the offset-drain term and cedes.
    """
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=200,
        last_rendered_offset=100,   # 100 bytes still pending drain
        turn_in_flight=False,
        engine=eng,
        engine_active_probe=lambda: False,
    )
    # File grows by another 100 bytes while the trailing reply bytes are still outstanding.
    reg.note_jsonl_growth("s", size=300, mtime=2.0)
    assert s.state == sr.SessionState.ENGINE, "must stay ENGINE — these are our own trailing bytes"
    assert eng.killed is False, "engine must NOT be killed"
    assert s.warnings == [], "no spurious cede warning"
    # Offset should advance to the new size now that we confirmed ownership.
    assert s.last_rendered_offset == 300


def test_engine_idle_foreign_growth_cedes_with_drained_offset():
    """Fix 1 companion: ENGINE, turn_in_flight=False, probe=False, AND offset==last_size
    (fully drained) — growth IS foreign and must cede.  This mirrors the existing
    test_engine_idle_foreign_growth_cedes but spells out the offset==size condition
    explicitly so that Fix 1 does not accidentally suppress real cedes.
    """
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=200,
        last_rendered_offset=200,   # fully drained: no outstanding bytes
        turn_in_flight=False,
        engine=eng,
        engine_active_probe=lambda: False,
    )
    reg.note_jsonl_growth("s", size=300, mtime=2.0)
    assert s.state == sr.SessionState.FOREIGN, "must cede — truly foreign write"
    assert eng.killed is True
    assert len(s.warnings) >= 1
    assert any("ceded" in w for w in s.warnings)


# ---------------------------------------------------------------------------
# Fix 2 (RED): in-flight turn preserved when ENGINE cedes to FOREIGN
# ---------------------------------------------------------------------------

def test_cede_moves_in_flight_turn_to_pending():
    """Fix 2 RED: when ENGINE cedes to FOREIGN while in_flight_turn is set and
    pending_turn is None, the in-flight turn must be moved to pending_turn so it
    is not lost.

    Scenario: a turn was submitted and is in the engine (in_flight_turn set,
    turn_in_flight was True), the turn finishes (turn_in_flight goes False,
    offset advances to last_size so drain is complete), then a foreign write
    arrives.  At cede time in_flight_turn is still set (mark_turn_done clears it
    only when the next pending turn is promoted or there is nothing pending).
    The cede must rescue it into pending_turn.

    Before Fix 2 this test FAILS because in_flight_turn does not exist and no
    rescue happens.
    """
    eng = _FakeEngine()
    reg, s, clock = _mk_growth(
        sr.SessionState.ENGINE,
        last_size=200,
        last_rendered_offset=200,   # fully drained — cede path will be taken
        turn_in_flight=False,
        engine=eng,
        engine_active_probe=lambda: False,
    )
    # Simulate: a turn was handed to the engine and is recorded as in-flight.
    s.in_flight_turn = {"text": "x"}
    s.pending_turn = None

    # Foreign growth arrives — cede is triggered.
    reg.note_jsonl_growth("s", size=300, mtime=2.0)

    assert s.state == sr.SessionState.FOREIGN
    # The in-flight turn must have been rescued into pending_turn.
    assert s.pending_turn == {"text": "x"}, (
        "in_flight_turn must be moved to pending_turn on cede so the turn is not lost"
    )
    # in_flight_turn must be cleared after rescue.
    assert s.in_flight_turn is None


def test_session_has_in_flight_turn_attribute():
    """Fix 2 RED: Session must have an in_flight_turn attribute initialized to None."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("attr-check", jsonl_path="/tmp/attr.jsonl")
    assert hasattr(s, "in_flight_turn"), "Session must define in_flight_turn"
    assert s.in_flight_turn is None


# ---------------------------------------------------------------------------
# ENGINE_IDLE_S constant, idle_since attribute, tick(), and interrupt()
# ---------------------------------------------------------------------------

def test_engine_idle_s_constant():
    """MODULE must expose ENGINE_IDLE_S = 300."""
    assert sr.ENGINE_IDLE_S == 300


def test_session_idle_since_initialises_to_zero():
    """A freshly created Session must have idle_since == 0.0."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("idle-init", jsonl_path="/tmp/idle.jsonl")
    assert s.idle_since == 0.0


# --- tick: releases idle ENGINE after ENGINE_IDLE_S ---

def _mk_tick(state, *, turn_in_flight=False, pending_turn=None,
             idle_since=0.0, clock_val=100.0):
    """Build registry+session for tick() tests with a mutable clock cell."""
    clock_cell = [clock_val]
    eng = _FakeEngine()
    reg = sr.SessionRegistry(
        engine_factory=lambda sid, model: eng,
        clock=lambda: clock_cell[0],
    )
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = state
    s.turn_in_flight = turn_in_flight
    s.pending_turn = pending_turn
    s.idle_since = idle_since
    if state == sr.SessionState.ENGINE:
        s.engine = eng
    return reg, s, clock_cell, eng


def test_tick_releases_idle_engine():
    """tick() on an ENGINE that has been idle for >= ENGINE_IDLE_S must call release()
    and leave the session in MIRROR state."""
    reg, s, clock_cell, eng = _mk_tick(
        sr.SessionState.ENGINE,
        turn_in_flight=False,
        pending_turn=None,
        idle_since=50.0,
        clock_val=50.0 + sr.ENGINE_IDLE_S + 1,  # just past the threshold
    )
    reg.tick("s")
    assert s.state == sr.SessionState.MIRROR, "idle ENGINE must be released to MIRROR"
    assert eng.killed is True


def test_tick_no_release_when_pending_turn():
    """tick() must NOT release when pending_turn is set (a turn is about to go in-flight)."""
    reg, s, clock_cell, eng = _mk_tick(
        sr.SessionState.ENGINE,
        turn_in_flight=False,
        pending_turn={"text": "queued"},
        idle_since=50.0,
        clock_val=50.0 + sr.ENGINE_IDLE_S + 1,
    )
    reg.tick("s")
    assert s.state == sr.SessionState.ENGINE, "must stay ENGINE when pending_turn is set"


def test_tick_no_release_when_turn_in_flight():
    """tick() must NOT release when turn_in_flight is True."""
    reg, s, clock_cell, eng = _mk_tick(
        sr.SessionState.ENGINE,
        turn_in_flight=True,
        pending_turn=None,
        idle_since=50.0,
        clock_val=50.0 + sr.ENGINE_IDLE_S + 1,
    )
    reg.tick("s")
    assert s.state == sr.SessionState.ENGINE, "must stay ENGINE while a turn is in-flight"


def test_tick_noop_for_mirror():
    """tick() on a MIRROR session must be a no-op (no exception, state unchanged)."""
    reg, s, clock_cell, eng = _mk_tick(sr.SessionState.MIRROR)
    reg.tick("s")
    assert s.state == sr.SessionState.MIRROR


def test_tick_noop_for_foreign():
    """tick() on a FOREIGN session must be a no-op."""
    reg, s, clock_cell, eng = _mk_tick(sr.SessionState.FOREIGN)
    reg.tick("s")
    assert s.state == sr.SessionState.FOREIGN


def test_tick_noop_for_acquiring():
    """tick() on an ACQUIRING session must be a no-op."""
    reg, s, clock_cell, eng = _mk_tick(sr.SessionState.ACQUIRING)
    reg.tick("s")
    assert s.state == sr.SessionState.ACQUIRING


def test_tick_no_release_when_idle_since_not_set():
    """tick() must NOT release when idle_since == 0.0 (timer not started)."""
    reg, s, clock_cell, eng = _mk_tick(
        sr.SessionState.ENGINE,
        turn_in_flight=False,
        pending_turn=None,
        idle_since=0.0,
        clock_val=50.0 + sr.ENGINE_IDLE_S + 1,
    )
    reg.tick("s")
    assert s.state == sr.SessionState.ENGINE, "must stay ENGINE when idle_since is 0.0"


# --- interrupt: turn-boundary reconcile, stays ENGINE ---

def test_interrupt_clears_in_flight_and_reconciles_offset():
    """interrupt() on ENGINE with turn_in_flight=True must clear turn_in_flight,
    clear in_flight_turn, reconcile last_rendered_offset to last_size, and keep
    state as ENGINE."""
    clock_cell = [200.0]
    eng = _FakeEngine()
    reg = sr.SessionRegistry(
        engine_factory=lambda sid, model: eng,
        clock=lambda: clock_cell[0],
    )
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = sr.SessionState.ENGINE
    s.engine = eng
    s.turn_in_flight = True
    s.in_flight_turn = {"text": "interrupted"}
    s.last_rendered_offset = 50
    s.last_size = 200

    reg.interrupt("s")

    assert s.turn_in_flight is False, "turn_in_flight must be cleared"
    assert s.in_flight_turn is None, "in_flight_turn must be cleared"
    assert s.last_rendered_offset == s.last_size, "offset must be reconciled to size"
    assert s.state == sr.SessionState.ENGINE, "state must remain ENGINE (not a transition)"
    assert s.idle_since == 200.0, "idle_since must be set to clock() on interrupt"


def test_interrupt_closes_writing_ours_race():
    """After interrupt(), writing_ours() must return False so a post-interrupt
    self-cede race cannot occur (spec §5.4)."""
    clock_cell = [200.0]
    eng = _FakeEngine()
    reg = sr.SessionRegistry(
        engine_factory=lambda sid, model: eng,
        clock=lambda: clock_cell[0],
    )
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = sr.SessionState.ENGINE
    s.engine = eng
    s.turn_in_flight = True
    s.in_flight_turn = {"text": "interrupted"}
    s.last_rendered_offset = 50
    s.last_size = 200

    reg.interrupt("s")

    assert reg.writing_ours(s) is False, (
        "writing_ours must be False after interrupt so the self-cede race is closed"
    )


# ---------------------------------------------------------------------------
# Review follow-ups: late-result-after-cede guard (C1), failed-submit no wedge
# (I2), and removal of the dead `subscribers` attribute (M2).
# ---------------------------------------------------------------------------

class _FailingEngine(_FakeEngine):
    """Engine whose submit() reports a write failure (dead/closed stdin)."""
    def submit(self, turn):
        return False


def test_mark_turn_done_is_noop_after_cede():
    """A late type=result from a killed subprocess must not touch a None engine
    nor advance the rendered offset on the now foreign-owned file."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    # Post-cede shape: FOREIGN, engine killed, the rescued turn left pending.
    s.state = sr.SessionState.FOREIGN
    s.engine = None
    s.pending_turn = {"text": "rescued"}
    s.last_size = 500
    s.last_rendered_offset = 100

    reg.mark_turn_done("s")  # must NOT raise

    assert s.state == sr.SessionState.FOREIGN
    assert s.pending_turn == {"text": "rescued"}, "pending turn must not be consumed by a stray result"
    assert s.last_rendered_offset == 100, "offset must NOT advance on a foreign-owned file"


def test_mark_turn_done_is_noop_after_release():
    """A late result after release() (MIRROR, engine None) must be a safe no-op."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FakeEngine())
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = sr.SessionState.MIRROR
    s.engine = None
    s.last_size = 300
    s.last_rendered_offset = 80

    reg.mark_turn_done("s")  # must NOT raise

    assert s.state == sr.SessionState.MIRROR
    assert s.last_rendered_offset == 80


def test_failed_submit_does_not_wedge_turn_in_flight():
    """When the engine reports the write failed, the session must reconcile to
    MIRROR with a warning instead of leaving turn_in_flight stuck True forever."""
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: _FailingEngine())
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.last_growth_ts = -10_000.0  # quiet long enough to acquire immediately

    reg.submit_turn("s", {"text": "hi"}, model="m")

    assert s.turn_in_flight is False, "a failed submit must not set turn_in_flight"
    assert s.state == sr.SessionState.MIRROR, "an unusable engine must be reconciled to mirror"
    assert any("write failed" in w for w in s.warnings), "a warning must record the failed delivery"


def test_failed_drain_after_turn_done_does_not_wedge():
    """If draining a queued turn into the engine fails, do not wedge turn_in_flight."""
    eng = _FailingEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = sr.SessionState.ENGINE
    s.engine = eng
    s.turn_in_flight = True
    s.pending_turn = {"text": "queued"}

    reg.mark_turn_done("s")

    assert s.turn_in_flight is False
    assert s.state == sr.SessionState.MIRROR
    assert any("write failed" in w for w in s.warnings)


def test_session_has_no_dead_subscribers_attribute():
    """The unused `subscribers` field was removed (M2); Session must not declare it."""
    s = sr.Session("s", "/tmp/s.jsonl")
    assert not hasattr(s, "subscribers"), "Session.subscribers was dead code and should be gone"


def test_repeated_lock_held_warning_is_deduped():
    """Under sustained cross-process lock contention the quiet-tick path must not
    append the same warning every poll — that would grow s.warnings unboundedly
    (the SSE loop no longer clears it). Consecutive identical warnings de-dup."""
    clock_cell = [1000.0]
    reg = sr.SessionRegistry(
        engine_factory=lambda sid, model: _FakeEngine(),
        clock=lambda: clock_cell[0],
        lock_acquire=lambda sid, owner: False,  # another process always holds the lock
    )
    s = reg.get_or_create("s", jsonl_path="/tmp/s.jsonl")
    s.state = sr.SessionState.FOREIGN
    s.pending_turn = {"text": "queued"}
    s.pending_owner = "me"
    s.last_size = 100
    s.last_growth_ts = 1000.0

    # Several quiet ticks (size unchanged) well past QUIESCENT_S, lock still held.
    for i in range(5):
        clock_cell[0] = 1000.0 + 3.0 + i
        reg.note_jsonl_growth("s", 100, mtime=clock_cell[0])

    held = [w for w in s.warnings if "lock held" in w]
    assert len(held) == 1, f"lock-held warning must be de-duped under sustained contention, got {len(held)}"
    assert s.state == sr.SessionState.FOREIGN  # still waiting for the lock


def test_new_session_does_not_self_cede_on_creation_writes():
    """A brand-new session (no prior transcript) acquires a CREATING engine
    whose first writes to the .jsonl are its own. Those writes must NOT be
    mistaken for the IDE and trigger a cede to FOREIGN."""
    eng = _FakeEngine()
    reg = sr.SessionRegistry(engine_factory=lambda sid, model: eng)
    s = reg.get_or_create("new-sid", jsonl_path="/tmp/new-sid.jsonl")
    # New file: last_growth_ts stays 0.0 so the first turn acquires immediately.
    status = reg.submit_turn("new-sid", {"text": "hello"}, model="m")
    assert status == "accepted"
    assert s.state == sr.SessionState.ENGINE
    assert s.turn_in_flight is True

    # The creating engine now writes the user echo + reply to a fresh .jsonl:
    # growth from offset 0. writing_ours must hold (turn in-flight), so the
    # watcher attributes it to us and does NOT cede.
    reg.note_jsonl_growth("new-sid", size=240, mtime=1.0)
    assert s.state == sr.SessionState.ENGINE, "creation writes must not self-cede to FOREIGN"
    assert s.last_rendered_offset == 240  # accounted as ours
