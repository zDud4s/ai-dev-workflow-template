"""Resilience tests for the dashboard's Claude OAuth /usage fetch.

These cover the failure modes that make the overview's usage card show "n/a"
when several dashboards run at once across projects (see serve._fetch_claude_oauth_usage):

  * the shared ~/.claude/.credentials.json being rewritten by another Claude
    Code instance while this process reads it -> a transient OSError (Windows
    sharing violation) or a half-written file (JSONDecodeError); and
  * a momentary fetch failure blanking a previously-good reading for a full
    minute, instead of degrading to the last-known value and retrying soon.
"""
from __future__ import annotations

import json
import time

import pytest

import serve
import server.usage as _usage


def _future_ms() -> int:
    """An expiresAt comfortably in the future (ms since epoch)."""
    return int((time.time() + 3600) * 1000)


def _creds_json(token: str = "tok-xyz", tier: str = "default_claude_max_5x") -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": _future_ms(), "rateLimitTier": tier}}
    )


class _FlakyPath:
    """Stand-in for the credentials Path whose read_text raises on the first
    ``fail_times`` calls (a concurrent rewrite) then returns ``payload``."""

    def __init__(self, payload: str, fail_times: int, exc: Exception):
        self.payload = payload
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    def read_text(self, encoding: str = "utf-8") -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.payload


class _FakeResp:
    """Minimal urlopen() context-manager stand-in."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def no_sleep(monkeypatch):
    """Make the read-retry backoff instant so tests don't actually wait."""
    monkeypatch.setattr(serve.time, "sleep", lambda *a, **k: None)


@pytest.fixture
def clear_usage_caches():
    """Reset the process-global usage caches around each test (they persist
    across calls by design, so leakage would otherwise couple tests)."""

    def reset():
        if hasattr(serve, "_CLAUDE_USAGE_CACHE"):
            serve._CLAUDE_USAGE_CACHE.update(at=0.0, data=None)
        _usage._CLAUDE_USAGE_CACHE.update(at=0.0, data=None)
        if hasattr(serve, "_CLAUDE_USAGE_LAST_GOOD"):
            serve._CLAUDE_USAGE_LAST_GOOD.update(at=0.0, data=None)
        _usage._CLAUDE_USAGE_LAST_GOOD.update(at=0.0, data=None)

    reset()
    yield
    reset()


def _patch_token(monkeypatch, value):
    monkeypatch.setattr(serve, "_read_claude_oauth_token", lambda: value)
    monkeypatch.setattr(_usage, "_read_claude_oauth_token", lambda: value)


def _patch_urlopen(monkeypatch, payload: dict):
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(body))


# --- Fix 2: credentials read survives a concurrent rewrite ------------------


def test_credentials_read_retries_past_transient_oserror(monkeypatch, no_sleep):
    flaky = _FlakyPath(_creds_json(), fail_times=2, exc=OSError("WinError 32: file in use"))
    monkeypatch.setattr(serve, "_CLAUDE_CREDENTIALS_PATH_OVERRIDE", flaky)
    monkeypatch.setattr(_usage, "_CLAUDE_CREDENTIALS_PATH_OVERRIDE", flaky)
    tok, tier = serve._read_claude_oauth_token()
    assert tok == "tok-xyz"
    assert tier == "default_claude_max_5x"
    assert flaky.calls == 3  # two transient failures, then success


def test_credentials_read_retries_past_partial_json(monkeypatch, no_sleep):
    flaky = _FlakyPath(_creds_json(), fail_times=1, exc=json.JSONDecodeError("Expecting value", "", 0))
    monkeypatch.setattr(serve, "_CLAUDE_CREDENTIALS_PATH_OVERRIDE", flaky)
    monkeypatch.setattr(_usage, "_CLAUDE_CREDENTIALS_PATH_OVERRIDE", flaky)
    tok, _ = serve._read_claude_oauth_token()
    assert tok == "tok-xyz"
    assert flaky.calls == 2


def test_credentials_read_gives_up_after_max_retries(monkeypatch, no_sleep):
    flaky = _FlakyPath("", fail_times=99, exc=OSError("permanently locked"))
    monkeypatch.setattr(serve, "_CLAUDE_CREDENTIALS_PATH_OVERRIDE", flaky)
    monkeypatch.setattr(_usage, "_CLAUDE_CREDENTIALS_PATH_OVERRIDE", flaky)
    tok, tier = serve._read_claude_oauth_token()
    assert tok is None and tier is None
    assert flaky.calls == serve._CREDENTIALS_READ_RETRIES


# --- Fix 1: degrade to last-known-good instead of blanking ------------------


def test_successful_fetch_records_last_good(monkeypatch, clear_usage_caches):
    _patch_token(monkeypatch, ("tok", "tier-5x"))
    _patch_urlopen(monkeypatch, {"five_hour": {"utilization": 12.0}})
    result = serve._fetch_claude_oauth_usage()
    assert result["available"] is True
    assert result["data"]["five_hour"]["utilization"] == 12.0
    assert serve._CLAUDE_USAGE_LAST_GOOD["data"] is not None


def test_fetch_serves_last_good_when_token_disappears(monkeypatch, clear_usage_caches):
    # A clean success first populates last-good.
    _patch_token(monkeypatch, ("tok", "tier-5x"))
    _patch_urlopen(monkeypatch, {"five_hour": {"utilization": 42.0}})
    first = serve._fetch_claude_oauth_usage()
    assert first["available"] is True

    # Expire the cache and make the next read fail (token vanished mid-rewrite).
    serve._CLAUDE_USAGE_CACHE["at"] = 0.0
    _usage._CLAUDE_USAGE_CACHE["at"] = 0.0
    _patch_token(monkeypatch, (None, "tier-5x"))

    degraded = serve._fetch_claude_oauth_usage()
    assert degraded["available"] is True  # not blanked
    assert degraded["stale"] is True  # but flagged stale
    assert degraded["data"]["five_hour"]["utilization"] == 42.0  # last-known value
    assert degraded.get("error")  # carries the reason for the staleness


def test_failure_without_last_good_reports_error(monkeypatch, clear_usage_caches):
    # No prior success to fall back to -> the hard failure is surfaced as-is.
    _patch_token(monkeypatch, (None, None))
    result = serve._fetch_claude_oauth_usage()
    assert result["available"] is False
    assert "oauth token" in result["error"].lower()


# --- Fix 1: failures and stale readings are cached only briefly -------------


def test_usage_cache_ttl_policy():
    full = serve._CLAUDE_USAGE_TTL_SECONDS
    short = serve._CLAUDE_USAGE_ERROR_TTL_SECONDS
    assert short < full  # a failure must not pin the card for the full window
    assert serve._usage_cache_ttl({"available": True}) == full
    assert serve._usage_cache_ttl({"available": True, "stale": True}) == short
    assert serve._usage_cache_ttl({"available": False}) == short
    assert serve._usage_cache_ttl(None) == short


# --- macOS Keychain credentials fallback ------------------------------------
# On macOS Claude Code stores its OAuth credentials in the login Keychain
# (generic-password item, service "Claude Code-credentials"), NOT in the
# ~/.claude/.credentials.json file used on Linux/Windows. Without a Keychain
# fallback the usage card's quota bars show "n/a" on every Mac.


class _FakeProc:
    """Minimal subprocess.run CompletedProcess stand-in."""

    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_keychain_reader_parses_security_output(monkeypatch):
    monkeypatch.setattr(_usage.sys, "platform", "darwin")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(returncode=0, stdout=_creds_json(token="tok-kc") + "\n")

    monkeypatch.setattr(_usage.subprocess, "run", fake_run)
    creds = _usage._read_keychain_credentials_json()
    assert isinstance(creds, dict)
    assert creds["claudeAiOauth"]["accessToken"] == "tok-kc"
    # queries the Claude Code keychain item via `security find-generic-password`
    assert "find-generic-password" in captured["cmd"]
    assert "Claude Code-credentials" in captured["cmd"]


def test_keychain_reader_returns_none_off_macos(monkeypatch):
    monkeypatch.setattr(_usage.sys, "platform", "win32")

    def boom(*a, **k):
        raise AssertionError("security must not be invoked off macOS")

    monkeypatch.setattr(_usage.subprocess, "run", boom)
    assert _usage._read_keychain_credentials_json() is None


def test_keychain_reader_returns_none_when_item_missing(monkeypatch):
    # `security` exits 44 ("could not be found") when the item is absent.
    monkeypatch.setattr(_usage.sys, "platform", "darwin")
    monkeypatch.setattr(
        _usage.subprocess, "run", lambda cmd, **k: _FakeProc(returncode=44, stdout="")
    )
    assert _usage._read_keychain_credentials_json() is None


def test_keychain_reader_survives_security_unavailable(monkeypatch):
    # `security` missing entirely (OSError) must not raise.
    monkeypatch.setattr(_usage.sys, "platform", "darwin")

    def raise_oserror(*a, **k):
        raise OSError("No such file or directory: 'security'")

    monkeypatch.setattr(_usage.subprocess, "run", raise_oserror)
    assert _usage._read_keychain_credentials_json() is None


def test_oauth_token_falls_back_to_keychain_when_file_absent(monkeypatch):
    # The bug: on macOS the JSON file does not exist, so the file read yields
    # None and the token must instead come from the Keychain.
    monkeypatch.setattr(_usage, "_read_credentials_json", lambda path: None)
    monkeypatch.setattr(
        _usage,
        "_read_keychain_credentials_json",
        lambda: json.loads(_creds_json(token="tok-kc", tier="tier-mac")),
        raising=False,
    )
    tok, tier = _usage._read_claude_oauth_token()
    assert tok == "tok-kc"
    assert tier == "tier-mac"


def test_oauth_token_prefers_file_over_keychain(monkeypatch):
    # When the file IS present (Linux/Windows), the Keychain is never consulted.
    monkeypatch.setattr(
        _usage, "_read_credentials_json",
        lambda path: json.loads(_creds_json(token="tok-file", tier="tier-file")),
    )

    def boom():
        raise AssertionError("Keychain must not be read when the file exists")

    monkeypatch.setattr(_usage, "_read_keychain_credentials_json", boom, raising=False)
    tok, tier = _usage._read_claude_oauth_token()
    assert tok == "tok-file"
    assert tier == "tier-file"
