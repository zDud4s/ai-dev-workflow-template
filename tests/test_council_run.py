from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import council_run as cr  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

CATALOG = {
    "claude": ["claude-opus-4-8", "claude-sonnet-4-6"],
    "codex": ["gpt-5.5"],
}


def test_model_seat_claude_argv():
    seat = {"type": "model", "ref": "claude-opus-4-8"}
    argv, stdin = cr.build_seat_argv(seat, "Why is the sky blue?", CATALOG)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "Why is the sky blue?" in argv          # claude: question is an argv element
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert stdin is None


def test_agent_seat_uses_agent_flag():
    seat = {"type": "agent", "ref": "security-reviewer", "model": "claude-opus-4-8"}
    argv, stdin = cr.build_seat_argv(seat, "Audit this.", CATALOG)
    assert "--agent" in argv and "security-reviewer" in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert stdin is None


def test_model_seat_codex_mirrors_real_spawn():
    seat = {"type": "model", "ref": "gpt-5.5"}
    argv, stdin = cr.build_seat_argv(seat, "Hello", CATALOG)
    # MUST mirror serve._build_codex_chat_argv exactly (serve.py:4758-4779):
    assert argv == ["codex", "exec", "--json", "--skip-git-repo-check",
                    "-m", "gpt-5.5", "-"]           # trailing "-" => read prompt from stdin
    assert stdin == "Hello\n"                       # codex: question on stdin, not argv
    assert "Hello" not in argv


def test_validate_rejects_unknown_model():
    seat = {"type": "model", "ref": "gpt-9-imaginary"}
    err = cr.validate_seat(seat, CATALOG, agent_slugs=set())
    assert err and "unknown" in err.lower()


def test_validate_rejects_unknown_agent():
    seat = {"type": "agent", "ref": "ghost", "model": "claude-opus-4-8"}
    err = cr.validate_seat(seat, CATALOG, agent_slugs={"security-reviewer"})
    assert err and "ghost" in err


def test_validate_rejects_agent_on_codex_model():
    seat = {"type": "agent", "ref": "security-reviewer", "model": "gpt-5.5"}
    err = cr.validate_seat(seat, CATALOG, agent_slugs={"security-reviewer"})
    assert err and "codex" in err.lower()


def test_validate_accepts_good_seats():
    assert cr.validate_seat({"type": "model", "ref": "claude-opus-4-8"}, CATALOG, set()) is None
    assert cr.validate_seat({"type": "agent", "ref": "security-reviewer",
                             "model": "claude-opus-4-8"}, CATALOG,
                            {"security-reviewer"}) is None


def test_anonymize_is_deterministic_for_seed():
    responses = {0: "ans-0", 1: "ans-1", 2: "ans-2"}
    a = cr.anonymize(responses, seed="run-123")
    b = cr.anonymize(responses, seed="run-123")
    assert a["anon_map"] == b["anon_map"]            # reproducible
    assert set(a["anon_map"].keys()) == {"A", "B", "C"}
    assert set(a["anon_map"].values()) == {0, 1, 2}  # every seat mapped


def test_anonymize_excludes_self():
    responses = {0: "ans-0", 1: "ans-1", 2: "ans-2"}
    res = cr.anonymize(responses, seed="run-123")
    for seat_idx in responses:
        shown = res["for_seat"][seat_idx]            # {anon_label: text} shown to seat_idx
        assert all(res["anon_map"][lbl] != seat_idx for lbl in shown)


def test_average_rank_handles_partial_participation():
    # seat rankings: viewer -> [(anon_label, rank)]
    rankings = {
        0: [("A", 1), ("B", 2)],          # ranked 2 peers
        1: [("A", 1)],                     # ranked only 1 (others errored for it)
    }
    anon_map = {"A": 2, "B": 1}
    board = cr.aggregate_rankings(rankings, anon_map)
    # seat 2 (label A): ranks [1,1] -> avg 1.0 ; seat 1 (label B): ranks [2] -> avg 2.0
    by_seat = {row["seat_idx"]: row for row in board}
    assert by_seat[2]["avg_rank"] == 1.0
    assert by_seat[1]["avg_rank"] == 2.0
    assert board[0]["seat_idx"] == 2                # sorted best-first


def _fake_runner_factory(responses_by_argv):
    def runner(argv, stdin, timeout):
        for needle, out in responses_by_argv.items():
            if needle in argv:
                return {"status": "ok", "stdout": out, "ms": 1}
        return {"status": "error", "stdout": "", "error": "no match", "ms": 1}
    return runner


def test_run_full_council_happy_path(tmp_path):
    spec = {
        "id": "run-1", "question": "Q?", "timeout_seconds": 30,
        "runs_dir": str(tmp_path), "catalog": CATALOG,
        "seats": [
            {"type": "model", "ref": "claude-opus-4-8"},
            {"type": "model", "ref": "claude-sonnet-4-6"},
        ],
        "chairman": {"type": "model", "ref": "claude-opus-4-8"},
    }
    runner = _fake_runner_factory({
        "claude-opus-4-8": "A: 1\nB: 2",
        "claude-sonnet-4-6": "A: 1\nB: 2",
    })
    record = cr.run_council(spec, runner=runner, emit=lambda e: None)
    assert record["status"] == "done"
    assert len(record["stage1"]) == 2
    assert record["stage3"]["status"] == "ok"
    assert (tmp_path / "run-1.json").exists()       # persisted


def test_run_council_skips_peer_review_under_two_responses(tmp_path):
    spec = {
        "id": "run-2", "question": "Q?", "timeout_seconds": 30,
        "runs_dir": str(tmp_path), "catalog": CATALOG,
        "seats": [
            {"type": "model", "ref": "claude-opus-4-8"},
            {"type": "model", "ref": "claude-sonnet-4-6"},
        ],
        "chairman": {"type": "model", "ref": "claude-opus-4-8"},
    }
    # only opus answers; sonnet errors -> 1 valid response -> skip stage 2
    runner = _fake_runner_factory({"claude-opus-4-8": "only answer"})
    record = cr.run_council(spec, runner=runner, emit=lambda e: None)
    assert record["stage2"] == []                   # skipped
    assert record["stage3"]["status"] == "ok"       # chairman still synthesizes
    assert record["status"] == "done"


def test_run_council_only_valid_stage1_seats_rank_and_are_ranked(tmp_path):
    spec = {
        "id": "run-3", "question": "Q?", "timeout_seconds": 30,
        "runs_dir": str(tmp_path), "catalog": CATALOG,
        "seats": [
            {"type": "model", "ref": "claude-opus-4-8"},
            {"type": "model", "ref": "claude-sonnet-4-6"},
            {"type": "model", "ref": "gpt-5.5"},
        ],
        "chairman": {"type": "model", "ref": "claude-opus-4-8"},
    }
    runner = _fake_runner_factory({
        "claude-opus-4-8": "A: 1\nB: 2",
        "claude-sonnet-4-6": "A: 1\nB: 2",
    })
    record = cr.run_council(spec, runner=runner, emit=lambda e: None)
    assert [row["seat_idx"] for row in record["stage2"]] == [0, 1]
    assert {row["seat_idx"] for row in record["leaderboard"]} == {0, 1}
    assert all(row["n"] == 1 for row in record["leaderboard"])


def test_cli_entrypoint_fake_runner_writes_record_and_events(tmp_path):
    spec = {
        "id": "run-cli", "question": "Q?", "timeout_seconds": 30,
        "runs_dir": str(tmp_path), "catalog": CATALOG,
        "seats": [
            {"type": "model", "ref": "claude-opus-4-8"},
            {"type": "model", "ref": "claude-sonnet-4-6"},
        ],
        "chairman": {"type": "model", "ref": "claude-opus-4-8"},
    }
    env = os.environ.copy()
    env["COUNCIL_FAKE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "council_run.py")],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "run-cli.json").exists()
    assert '{"stage":"run","status":"done"}' in proc.stdout


def test_load_council_config_defaults_from_models_yaml():
    cfg = cr.load_council_config(ROOT / ".ai" / "models.yaml")
    assert cfg["timeout_seconds"] == 600
    assert cfg["chairman"] == {"type": "model", "ref": "claude-opus-4-8"}
    assert [member["ref"] for member in cfg["members"]] == [
        "claude-opus-4-8",
        "gpt-5.5",
        "claude-sonnet-4-6",
    ]
    assert all(member["type"] == "model" for member in cfg["members"])
    assert all("model" not in member for member in cfg["members"])


def test_example_agents_are_well_formed():
    agents_dir = ROOT / ".claude" / "agents"
    expected = {
        "security-reviewer",
        "marketing-strategist",
        "pr-strategist",
    }
    for slug in expected:
        path = agents_dir / f"{slug}.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        frontmatter = text.split("---", 2)[1]
        body = text.split("---", 2)[2].strip()
        data = yaml.safe_load(frontmatter)
        assert data["name"] == slug
        assert data["description"]
        assert data["tools"]
        assert data["model"] == "claude-opus-4-8"
        assert body


# --- Windows shim resolution + codex output distillation -------------------

def test_resolve_argv_falls_back_for_unknown_tool():
    # Nothing on PATH by that name -> argv returned unchanged (POSIX / abs path).
    assert cr._resolve_argv(["no-such-tool-xyz-123", "a", "b"]) == ["no-such-tool-xyz-123", "a", "b"]
    assert cr._resolve_argv([]) == []


def test_extract_codex_text_keeps_last_agent_message():
    # Codex emits a preamble agent_message, then tool calls, then the final
    # answer — we keep the last one (the synthesis), dropping preamble noise.
    stream = (
        '{"type":"thread.started","thread_id":"x"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"let me check the rules"}}\n'
        '{"type":"item.completed","item":{"type":"web_search","query":"gdpr 72h"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"the final answer"}}\n'
        '{"type":"turn.completed"}\n'
    )
    assert cr._extract_codex_text(stream) == "the final answer"


def test_extract_codex_text_falls_back_to_raw():
    # No agent_message event -> keep the raw text rather than dropping content.
    assert cr._extract_codex_text("plain non-json output") == "plain non-json output"


def test_run_seat_distills_codex_stdout():
    seat = {"type": "model", "ref": "gpt-5.5"}
    codex_stream = '{"type":"item.completed","item":{"type":"agent_message","text":"distilled"}}'
    def runner(argv, stdin, timeout):
        assert argv[0] == "codex"
        return {"status": "ok", "stdout": codex_stream, "ms": 1}
    _idx, result = cr._run_seat(0, seat, "q", CATALOG, 30, runner)
    assert result["stdout"] == "distilled"   # not the raw JSON event line
