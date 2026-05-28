import inspect
import sys, pathlib, json, threading, os

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
import serve  # the module under test


def _bare_handler():
    h = serve.Handler.__new__(serve.Handler)
    h.directory = str(serve.ROOT)
    return h


def _is_under(path, root):
    resolved = os.path.normcase(os.path.realpath(str(path)))
    base = os.path.normcase(os.path.realpath(str(root)))
    return resolved == base or resolved.startswith(base + os.sep)


def _read_jsonl(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_static_serving_allows_dashboard_subtree():
    h = _bare_handler()

    translated = h.translate_path("/.ai/dashboard/index.html")

    assert _is_under(translated, serve.ROOT / ".ai" / "dashboard")


def test_static_serving_blocks_repo_root_files():
    # The static handler is a blocklist (NOT a dashboard-only allowlist) — the
    # dashboard frontend fetches .ai/memory.md, .ai/decisions.md, .ai/project.yaml,
    # .ai/models.yaml, .ai/plans/*, .ai/specs/*, etc. via this handler, so those
    # must stay reachable. Only known-sensitive paths are blocked.
    h = _bare_handler()

    for path in ["/.git/config", "/.git/HEAD", "/.claude/settings.json"]:
        translated = h.translate_path(path)
        assert translated.endswith("__blocked_sensitive_path__"), \
            f"{path} should be blocked, got {translated}"


def test_static_serving_allows_project_state_files():
    # Regression guard for the 2026-05-22 hotfix: dashboard frontend depends on
    # these paths reaching the static handler. If a future change adds a stricter
    # guard that breaks any of them, the overview page goes blank.
    h = _bare_handler()

    needed = [
        "/.ai/memory.md",
        "/.ai/decisions.md",
        "/.ai/project.yaml",
        "/.ai/models.yaml",
        "/.ai/ledgers/events.jsonl",
        "/.ai/ledgers/jobs.jsonl",
    ]
    for path in needed:
        translated = h.translate_path(path)
        assert not translated.endswith("__blocked_sensitive_path__"), \
            f"{path} should be allowed, got {translated}"
        assert _is_under(translated, serve.ROOT), \
            f"{path} should resolve under ROOT, got {translated}"


def test_static_serving_blocks_parent_escape():
    h = _bare_handler()

    translated = h.translate_path("/.ai/dashboard/../../.git/config")

    assert translated.endswith("__blocked_sensitive_path__")


def test_api_paths_not_blocked_by_static_guard(monkeypatch):
    h = _bare_handler()
    h.path = "/api/jobs"
    called = []

    def fake_jobs_list(self):
        called.append(self)

    monkeypatch.setattr(serve.Handler, "_handle_jobs_list", fake_jobs_list)

    h.do_GET()

    assert called == [h]


def test_jobs_persist_lock_serializes_writes(tmp_path, monkeypatch):
    persist_path = tmp_path / "jobs.jsonl"
    jobs = {
        f"job-{i}": {
            "id": f"job-{i}",
            "status": "done",
            "payload": "x" * 1000,
        }
        for i in range(20)
    }
    monkeypatch.setattr(serve, "JOBS_PERSIST_FILE", persist_path)
    monkeypatch.setattr(serve, "JOBS", jobs)

    threads = [
        threading.Thread(target=serve._persist_job, args=(job_id,))
        for job_id in jobs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = _read_jsonl(persist_path)
    assert {row["id"] for row in rows} == set(jobs)
    assert len(rows) == len(jobs)


def test_improvements_ledger_lock_serializes_writes(tmp_path, monkeypatch):
    ledger_path = tmp_path / "improvements.jsonl"
    monkeypatch.setattr(serve, "IMPROVEMENTS_LEDGER", ledger_path)

    threads = [
        threading.Thread(
            target=serve._audit_improvement,
            args=(f"skill-{i}", "applied", "reason", None, None, i),
        )
        for i in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = _read_jsonl(ledger_path)
    assert {row["skill"] for row in rows} == {f"skill-{i}" for i in range(20)}
    assert len(rows) == 20


def test_skill_metrics_lock_serializes_writes(tmp_path, monkeypatch):
    metrics_path = tmp_path / "skill_metrics.jsonl"
    jobs = {
        f"job-{i}": {
            "id": f"job-{i}",
            "kind": "plan",
            "status": "done",
            "exit_code": 0,
            "started_at": "2026-05-22T00:00:00+00:00",
            "ended_at": "2026-05-22T00:00:01+00:00",
            "log_path": None,
            "cost": {"duration_ms": i, "cost_usd": 0.01, "turns": 1},
            "session_id": f"session-{i}",
            "model": "test-model",
        }
        for i in range(20)
    }
    monkeypatch.setattr(serve, "SKILL_METRICS_FILE", metrics_path)
    monkeypatch.setattr(serve, "JOBS", jobs)
    monkeypatch.setattr(serve, "_post_job_skill_actions", lambda job_id, skill_ids: None)

    threads = [
        threading.Thread(target=serve._record_skill_metrics, args=(job_id,))
        for job_id in jobs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = _read_jsonl(metrics_path)
    assert {row["job_id"] for row in rows} == set(jobs)
    assert {row["skill"] for row in rows} == {"planner"}
    assert len(rows) == len(jobs)


def test_apply_improvement_atomic_replace_success(tmp_path, monkeypatch):
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("ORIGINAL", encoding="utf-8")
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(serve, "SKILL_BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(serve, "IMPROVEMENTS_LEDGER", tmp_path / "improvements.jsonl")

    ok = serve._apply_improvement(
        skill_path,
        "NEW",
        source="test",
        reason="t",
        proposal_id=None,
        skill_id="s",
        diff_lines=1,
    )

    backups = list(backups_dir.glob("*.bak"))
    assert ok is True
    assert skill_path.read_text(encoding="utf-8") == "NEW"
    assert not skill_path.with_name(skill_path.name + ".tmp").exists()
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "ORIGINAL"


def test_apply_improvement_atomic_replace_failure_preserves_original(tmp_path, monkeypatch):
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.setattr(serve, "SKILL_BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(serve, "IMPROVEMENTS_LEDGER", tmp_path / "improvements.jsonl")

    def fail_replace(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(serve.os, "replace", fail_replace)

    ok = serve._apply_improvement(
        skill_path,
        "NEW",
        source="test",
        reason="t",
        proposal_id=None,
        skill_id="s",
        diff_lines=1,
    )

    assert ok is False
    assert skill_path.read_text(encoding="utf-8") == "ORIGINAL"


def test_validate_template_url_narrow_except():
    src = inspect.getsource(serve._validate_template_url)
    assert "except (ValueError, TypeError):" in src
    assert "except Exception:" not in src


def test_security_headers_present():
    src = inspect.getsource(serve.Handler.end_headers)
    for expected in [
        "X-Frame-Options",
        "DENY",
        "Referrer-Policy",
        "no-referrer",
        "Content-Security-Policy",
        "frame-ancestors",
    ]:
        assert expected in src


def test_blocked_names_expanded():
    src = inspect.getsource(serve.Handler)
    for expected in [
        ".git-credentials",
        ".pfx",
        "id_rsa",
        "auth.json",
    ]:
        assert expected in src


def test_template_url_rejects_file_scheme():
    assert serve._validate_template_url("file:///etc/passwd") == serve._DEFAULT_WORKFLOW_TEMPLATE_URL
