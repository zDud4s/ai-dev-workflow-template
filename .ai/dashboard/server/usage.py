from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from pathlib import Path

from server.paths import ROOT
from server.storage import _bound_path_cache
from server.transcript_paths import _codex_sessions_root, _transcripts_dir_for_cwd
from server.validation import _normalise_path_for_match, _parse_iso_ts

_CODEX_FILE_AGG_CACHE: dict[str, tuple[int, dict]] = {}
_CODEX_FILE_AGG_LOCK = threading.Lock()

# Cached aggregator result (re-read after _USAGE_TTL_SECONDS) so the overview
# can refresh without re-scanning ~/.codex/sessions on every reload.
_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_USAGE_TTL_SECONDS = 30.0


def _aggregate_claude_usage(repo_root: Path, now: _dt.datetime) -> dict:
    """Per-model + windowed token totals from this repo's Claude transcripts.

    Reads ``message.usage`` plus ``message.model`` and ``timestamp`` from each
    line in ``~/.claude/projects/<slug>/*.jsonl`` and accumulates totals by
    model for three windows: ``all``, last 5 hours, last 7 days. Returns
    zeros if there is no transcripts directory for this repo."""
    cutoff_5h = now - _dt.timedelta(hours=5)
    cutoff_7d = now - _dt.timedelta(days=7)

    def empty():
        return {
            "total": 0, "input": 0, "output": 0,
            "cache_creation": 0, "cache_read": 0,
            "messages": 0, "by_model": {},
        }

    out = {"all": empty(), "5h": empty(), "7d": empty(), "transcripts": 0}

    def add(win, model, usage):
        inp = int(usage.get("input_tokens") or 0)
        outp = int(usage.get("output_tokens") or 0)
        cc = int(usage.get("cache_creation_input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        tot = inp + outp + cc + cr
        win["total"] += tot
        win["input"] += inp
        win["output"] += outp
        win["cache_creation"] += cc
        win["cache_read"] += cr
        win["messages"] += 1
        bm = win["by_model"].setdefault(model, {"total": 0, "messages": 0})
        bm["total"] += tot
        bm["messages"] += 1

    tdir = _transcripts_dir_for_cwd(repo_root)
    if tdir is None:
        return out
    try:
        files = list(tdir.glob("*.jsonl"))
    except OSError:
        return out
    for p in files:
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                # Count only transcripts we actually opened â€” a locked/deleted
                # file (OSError below) contributes no usage data and shouldn't
                # inflate the scanned-transcript count.
                out["transcripts"] += 1
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    model = msg.get("model") or "unknown"
                    ts = _parse_iso_ts(obj.get("timestamp"))
                    try:
                        add(out["all"], model, usage)
                        if ts is not None and ts >= cutoff_5h:
                            add(out["5h"], model, usage)
                        if ts is not None and ts >= cutoff_7d:
                            add(out["7d"], model, usage)
                    except (TypeError, ValueError):
                        continue
        except OSError:
            continue
    _fill_percent_shares(out)
    return out


def _aggregate_codex_usage(repo_root: Path, now: _dt.datetime) -> dict:
    """Per-model + windowed token totals from Codex session rollouts for this
    repo, plus the most recent account-wide ``rate_limits`` block.

    Codex rollouts at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` carry
    ``turn_context`` events (which expose ``payload.model`` for each turn) and
    ``event_msg`` events with ``payload.type == "token_count"`` that bundle
    both ``info.last_token_usage`` (per-turn) and ``rate_limits`` (5h primary
    + weekly secondary, account-wide). Per-turn totals are filtered by
    matching ``session_meta.payload.cwd`` to ``repo_root`` so the overview
    only counts tokens spent on this project. Rate-limit percentages are
    account-wide (Codex does not expose per-project quotas)."""
    cutoff_5h = now - _dt.timedelta(hours=5)
    cutoff_7d = now - _dt.timedelta(days=7)

    def empty():
        return {
            "total": 0, "input": 0, "cached_input": 0,
            "output": 0, "reasoning_output": 0,
            "turns": 0, "by_model": {},
        }

    out = {
        "all": empty(), "5h": empty(), "7d": empty(),
        "sessions": 0, "rate_limits": None, "matched_sessions": 0,
    }

    root = _codex_sessions_root()
    if root is None:
        return out

    target = _normalise_path_for_match(str(repo_root))
    latest_rl: tuple[_dt.datetime, dict] | None = None  # account-wide

    try:
        files = list(root.rglob("rollout-*.jsonl"))
    except OSError:
        return out

    # Cap traversal at the N most recently modified rollouts. ``rglob`` walks
    # ALL Codex sessions on the machine (across every repo); the unfiltered
    # set can be ~150MB and hundreds of files. Older sessions don't contribute
    # meaningfully to "recent usage", and the 30s TTL cache means the first
    # call after expiry would otherwise stall the overview. Sort by mtime
    # descending and keep the most recent slice.
    _CODEX_SESSION_FILE_CAP = 100
    if len(files) > _CODEX_SESSION_FILE_CAP:
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            # If stat() fails on some entries, fall back to the unsorted
            # truncation; better than a 500 on the overview endpoint.
            pass
        files = files[:_CODEX_SESSION_FILE_CAP]

    def add(win, model, last):
        inp = int(last.get("input_tokens") or 0)
        cinp = int(last.get("cached_input_tokens") or 0)
        outp = int(last.get("output_tokens") or 0)
        rout = int(last.get("reasoning_output_tokens") or 0)
        tot = int(last.get("total_tokens") or 0) or (inp + outp + rout)
        win["total"] += tot
        win["input"] += inp
        win["cached_input"] += cinp
        win["output"] += outp
        win["reasoning_output"] += rout
        win["turns"] += 1
        bm = win["by_model"].setdefault(model, {"total": 0, "turns": 0})
        bm["total"] += tot
        bm["turns"] += 1

    def parse_rollout_file(path: Path) -> dict:
        file_agg = {
            "_target": target,
            "cwd_matches": False,
            "tokens": [],
            "latest_rl": None,
        }
        cwd_matches = False
        current_model = "unknown"
        latest_file_rl: tuple[_dt.datetime, dict] | None = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    t = obj.get("type")
                    payload = obj.get("payload") or {}
                    if t == "session_meta":
                        cwd = _normalise_path_for_match(payload.get("cwd") or "")
                        if cwd == target:
                            cwd_matches = True
                    elif t == "turn_context":
                        m = payload.get("model")
                        if isinstance(m, str) and m:
                            current_model = m
                        cwd = _normalise_path_for_match(payload.get("cwd") or "")
                        if cwd and cwd == target:
                            cwd_matches = True
                    elif t == "event_msg" and payload.get("type") == "token_count":
                        ts = _parse_iso_ts(obj.get("timestamp"))
                        rl = payload.get("rate_limits")
                        if isinstance(rl, dict) and ts is not None:
                            # Skip empty exhausted-quota snapshots so they do
                            # not clobber the last usable rate-limit payload.
                            has_payload = isinstance(rl.get("primary"), dict) or isinstance(rl.get("secondary"), dict)
                            if has_payload and (latest_file_rl is None or ts > latest_file_rl[0]):
                                latest_file_rl = (ts, rl)
                        if not cwd_matches:
                            continue
                        info = payload.get("info") or {}
                        last = info.get("last_token_usage")
                        if not isinstance(last, dict):
                            continue
                        file_agg["tokens"].append({
                            "ts": ts,
                            "model": current_model,
                            "last": dict(last),
                        })
        except OSError:
            return file_agg
        file_agg["cwd_matches"] = cwd_matches
        file_agg["latest_rl"] = latest_file_rl
        return file_agg

    def cached_rollout_file_agg(path: Path) -> dict | None:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return None
        with _CODEX_FILE_AGG_LOCK:
            cached = _CODEX_FILE_AGG_CACHE.get(str(path))
            if cached is not None and cached[0] == mtime_ns:
                agg = cached[1]
                if agg.get("_target") == target:
                    return agg
        agg = parse_rollout_file(path)
        with _CODEX_FILE_AGG_LOCK:
            _CODEX_FILE_AGG_CACHE[str(path)] = (mtime_ns, agg)
            _bound_path_cache(_CODEX_FILE_AGG_CACHE)
        return agg

    for p in files:
        out["sessions"] += 1
        file_agg = cached_rollout_file_agg(p)
        if file_agg is None:
            continue
        file_rl = file_agg.get("latest_rl")
        if file_rl is not None and (latest_rl is None or file_rl[0] > latest_rl[0]):
            latest_rl = file_rl
        if file_agg.get("cwd_matches"):
            out["matched_sessions"] += 1
        for token in file_agg.get("tokens") or []:
            ts = token.get("ts")
            model = token.get("model") or "unknown"
            last = token.get("last")
            if not isinstance(last, dict):
                continue
            try:
                add(out["all"], model, last)
                if ts is not None and ts >= cutoff_5h:
                    add(out["5h"], model, last)
                if ts is not None and ts >= cutoff_7d:
                    add(out["7d"], model, last)
            except (TypeError, ValueError):
                continue

    if latest_rl is not None:
        ts, rl = latest_rl
        # Each window carries its own resets_at (epoch seconds). If that
        # moment is in the past, the window has rolled over since the
        # snapshot was recorded and the percent is no longer current â€”
        # mark it stale so the UI can show that rather than pretending the
        # old number is live (Codex's IDE pulls live API data; we can't
        # from local rollouts).
        now_ts = now.timestamp()
        def _annotate_stale(win):
            if not isinstance(win, dict):
                return win
            ra = win.get("resets_at")
            if isinstance(ra, (int, float)) and ra < now_ts:
                w = dict(win)
                w["stale"] = True
                return w
            return win
        out["rate_limits"] = {
            "primary": _annotate_stale(rl.get("primary")),
            "secondary": _annotate_stale(rl.get("secondary")),
            "plan_type": rl.get("plan_type"),
            "last_event_at": ts.isoformat(),
        }
    _fill_percent_shares(out)
    return out


def _fill_percent_shares(out: dict) -> None:
    """Annotate each ``by_model[m]`` entry with a ``percent`` share of the
    window total (rounded to one decimal). Mutates in place."""
    for w in ("all", "5h", "7d"):
        win = out.get(w)
        if not isinstance(win, dict):
            continue
        total = win.get("total") or 0
        bm = win.get("by_model") or {}
        if total > 0:
            for info in bm.values():
                info["percent"] = round(100.0 * info["total"] / total, 1)
        else:
            for info in bm.values():
                info["percent"] = 0.0


# Claude Code stores its OAuth token here when the user is signed into a
# subscription plan. Tests override via ``_CLAUDE_CREDENTIALS_PATH_OVERRIDE``.
_CLAUDE_CREDENTIALS_PATH_OVERRIDE: Path | None = None
_CLAUDE_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_CLAUDE_USAGE_TTL_SECONDS = 60.0
# A *failed* or degraded (stale) usage result is cached only briefly so a
# transient blip (token mid-rewrite, a single upstream 429) clears on the next
# poll instead of pinning the overview card to "n/a" for the full success TTL.
_CLAUDE_USAGE_ERROR_TTL_SECONDS = 10.0
# Last genuinely-successful payload. Served (flagged ``stale``) when a later
# fetch fails, so one momentary error doesn't blank a previously-good reading.
_CLAUDE_USAGE_LAST_GOOD: dict = {"at": 0.0, "data": None}
_CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# Reading the credentials file can transiently fail when another Claude Code
# instance (a second project) is rewriting it to refresh the OAuth token:
# Windows raises a sharing violation (OSError/PermissionError) and a half-
# written file fails to parse (JSONDecodeError). Both are momentary, so retry a
# few times with a short backoff before treating a rewrite race as "signed out".
_CREDENTIALS_READ_RETRIES = 3
_CREDENTIALS_READ_RETRY_SLEEP_S = 0.05


def _read_credentials_json(path) -> dict | None:
    """Parse the Claude credentials JSON, retrying past the transient errors a
    concurrent rewrite produces. Returns the parsed dict, or ``None`` if the
    file stays unreadable/unparseable across every attempt."""
    for attempt in range(_CREDENTIALS_READ_RETRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            if attempt < _CREDENTIALS_READ_RETRIES - 1:
                time.sleep(_CREDENTIALS_READ_RETRY_SLEEP_S)
    return None


def _read_claude_oauth_token() -> tuple[str | None, str | None]:
    """Read the local Claude Code OAuth access token plus subscription tier.

    Returns ``(token, tier)`` or ``(None, None)`` when the credentials file
    is missing, malformed, or the token has expired. ``tier`` is the
    ``rateLimitTier`` (e.g. ``default_claude_max_5x``); useful for UI hints
    but not required for the API call itself."""
    path = _CLAUDE_CREDENTIALS_PATH_OVERRIDE or (Path.home() / ".claude" / ".credentials.json")
    creds = _read_credentials_json(path)
    if not isinstance(creds, dict):
        return None, None
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, None
    tok = oauth.get("accessToken")
    if not isinstance(tok, str) or not tok:
        return None, None
    exp = oauth.get("expiresAt")
    # expiresAt is milliseconds since epoch in this file.
    if isinstance(exp, (int, float)) and exp <= time.time() * 1000:
        return None, oauth.get("rateLimitTier")
    return tok, oauth.get("rateLimitTier")


def _usage_cache_ttl(cached: dict | None) -> float:
    """Cache lifetime for a usage result. A fresh success holds for the full
    TTL; a failure or a degraded (stale) reading holds only briefly so the next
    poll retries soon and can recover real data instead of pinning the error."""
    if cached and cached.get("available") and not cached.get("stale"):
        return _CLAUDE_USAGE_TTL_SECONDS
    return _CLAUDE_USAGE_ERROR_TTL_SECONDS


def _fetch_claude_oauth_usage() -> dict:
    """Fetch real Claude session/weekly utilization from the OAuth usage
    endpoint Claude Code itself uses for ``/usage``. Caches the response for
    ``_CLAUDE_USAGE_TTL_SECONDS`` so a busy overview reload doesn't hammer
    the API. The token never leaves this process.

    On a transient failure (token mid-rewrite, an upstream 429/5xx) the last
    genuinely-successful payload is served instead, flagged ``stale`` with the
    error attached, and cached only for ``_CLAUDE_USAGE_ERROR_TTL_SECONDS`` so
    the card degrades gracefully and recovers on the next poll rather than
    blanking to "n/a" for a full minute.

    Returns ``{"available": bool, "data"?: {...}, "error"?: str, "tier"?: str,
    "stale"?: bool}``. Network errors are swallowed and surfaced as
    ``available=False`` (or a stale reading when one is available)."""
    import urllib.error
    import urllib.request

    now_mono = time.monotonic()
    cached = _CLAUDE_USAGE_CACHE["data"]
    if cached is not None and (now_mono - _CLAUDE_USAGE_CACHE["at"]) < _usage_cache_ttl(cached):
        return cached

    tok, tier = _read_claude_oauth_token()
    if not tok:
        result = {
            "available": False,
            "error": "no claude oauth token (~/.claude/.credentials.json missing or expired)",
            "tier": tier,
        }
    else:
        req = urllib.request.Request(
            _CLAUDE_OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "User-Agent": "ai-workflow-dashboard/0.1 (claude-code-compatible)",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:  # nosec: B310 - constant trusted URL
                body = r.read().decode("utf-8")
                data = json.loads(body)
            result = {"available": True, "data": data, "tier": tier}
        except urllib.error.HTTPError as e:
            result = {"available": False, "error": f"http {e.code}", "tier": tier}
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
            result = {"available": False, "error": str(e)[:160], "tier": tier}

    if result.get("available"):
        # Remember the good reading so a later failure can degrade to it.
        _CLAUDE_USAGE_LAST_GOOD["data"] = result
        _CLAUDE_USAGE_LAST_GOOD["at"] = now_mono
    else:
        last = _CLAUDE_USAGE_LAST_GOOD["data"]
        if last is not None and last.get("data") is not None:
            # Degrade to the last-known-good reading rather than blanking the
            # card; flag it stale and keep the error for the "n/a" tooltip.
            result = {
                "available": True,
                "data": last["data"],
                "tier": last.get("tier", result.get("tier")),
                "stale": True,
                "error": result.get("error"),
            }

    _CLAUDE_USAGE_CACHE["data"] = result
    _CLAUDE_USAGE_CACHE["at"] = now_mono
    return result


def _aggregate_project_token_usage() -> dict:
    """Combined Claude + Codex token usage for this repo, with per-model and
    per-window breakdowns. Cached for ``_USAGE_TTL_SECONDS`` to keep the
    overview reload cheap (Codex scans can read ~150MB across all rollouts).
    Claude's real session-vs-quota utilization comes from the OAuth usage
    endpoint and is cached separately (see ``_fetch_claude_oauth_usage``)."""
    now_mono = time.monotonic()
    if _USAGE_CACHE["data"] is not None and (now_mono - _USAGE_CACHE["at"]) < _USAGE_TTL_SECONDS:
        cached = dict(_USAGE_CACHE["data"])
        # Always re-evaluate Claude OAuth usage on its own (shorter) TTL.
        cached["claude"] = dict(cached["claude"], rate_limits=_fetch_claude_oauth_usage())
        return cached
    now = _dt.datetime.now(_dt.timezone.utc)
    claude = _aggregate_claude_usage(ROOT, now)
    codex = _aggregate_codex_usage(ROOT, now)
    claude["rate_limits"] = _fetch_claude_oauth_usage()
    combined_all = (
        claude["all"]["total"] + codex["all"]["total"]
    )
    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "windows": {
            "5h": {"hours": 5},
            "7d": {"days": 7},
        },
        "claude": claude,
        "codex": codex,
        "combined": {"total": combined_all},
        # Backwards-compat with the first iteration of the API.
        "input": claude["all"]["input"],
        "output": claude["all"]["output"],
        "cache_creation": claude["all"]["cache_creation"],
        "cache_read": claude["all"]["cache_read"],
        "messages": claude["all"]["messages"],
        "transcripts": claude["transcripts"],
        "total": claude["all"]["total"],
    }
    _USAGE_CACHE["data"] = data
    _USAGE_CACHE["at"] = now_mono
    return data
