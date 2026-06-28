"""Capture, resolve, and export the workflow TODO ledger.

The ledger is append-only JSONL: callers append a new snapshot whenever a TODO
changes, and readers fold by ``id`` with the latest row winning.
"""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".tox", ".mypy_cache", "tmp",
})

STOPWORDS = frozenset({
    "about", "after", "done", "fixme", "from",
    "have", "into", "need", "needs", "that",
    "their", "there", "this", "todo", "when",
    "where", "will", "with", "work", "your",
})

_MEMORY_FOLLOWUP_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} \[followup\] (.+)")
# Capture the comment opener so a block/html terminator (``*/``, ``-->``) is
# stripped ONLY when that comment style actually opened the line. A previous
# version stripped any of those terminators unconditionally, so a line comment
# like ``# TODO: see http://x/*/foo`` was truncated at ``*/``. The text is now
# captured to end-of-line and the matching terminator removed in code below.
_DIFF_MARKER_RE = re.compile(
    r"(?P<open>#|//|/\*|<!--|^\s*\*\s)\s*\b(?P<kw>TODO|FIXME|XXX)\b(?![.\w])[:\s]+(?P<text>.+?)\s*$"
)
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

DIFF_SKIP_PREFIXES = (
    ".ai/packets/",
    ".ai/specs/",
    ".ai/plans/",
    ".ai/memory.md",
    ".ai/memory-archive.md",
    ".ai/decisions.md",
    ".ai/TODO.md",
    ".ai/ledgers/todos.jsonl",
    ".ai/ledgers/todos-archive.jsonl",
    ".gitignore",
    ".ai/scripts/todos_parser.py",
    "tests/test_todos_",
    "tests/test_references.py",
)


def _utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _todos_path(repo_root) -> Path:
    return Path(repo_root) / ".ai" / "ledgers" / "todos.jsonl"


def _todo_md_path(repo_root) -> Path:
    return Path(repo_root) / ".ai" / "TODO.md"


def _lock_path(repo_root) -> Path:
    return Path(repo_root) / ".ai" / ".todos.lock"


def _config_path(repo_root) -> Path:
    return Path(repo_root) / ".ai" / "dashboard" / "todos-config.json"


def load_config(repo_root) -> dict:
    path = _config_path(repo_root)
    if not path.is_file():
        return {"auto_enabled": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"auto_enabled": True}
        return {"auto_enabled": bool(data.get("auto_enabled", True))}
    except (OSError, ValueError):
        return {"auto_enabled": True}


def save_config(repo_root, config: dict) -> dict:
    path = _config_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {"auto_enabled": bool(config.get("auto_enabled", True))}
    path.write_text(json.dumps(clean) + "\n", encoding="utf-8", newline="\n")
    return clean


def auto_enabled(repo_root) -> bool:
    return load_config(repo_root).get("auto_enabled", True)


def _log_path(repo_root) -> Path:
    return Path(repo_root) / ".ai" / "dashboard" / ".todos-parser.log"


def _load_jsonl(path) -> list[dict]:
    """Load JSONL rows from ``path``.

    ``path`` may be either the ledger path itself or a repo root containing
    ``.ai/ledgers/todos.jsonl``. Malformed lines are skipped so one bad append does not
    poison dashboard reads.
    """
    p = Path(path)
    if p.is_dir():
        p = _todos_path(p)
    rows = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _normalize(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", text).strip()


def _dedup_hash(source: str, title: str) -> str:
    payload = (source + "\n" + _normalize(title)).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _dedup_source_key(source: str) -> str:
    key = re.sub(r"#L\d+\b", "", source)
    return re.sub(r":\d+$", "", key)


def _allocate_id(existing, now: str | None = None) -> str:
    date = (now or _utc_now())[:10]
    prefix = f"td_{date}_"
    rows = existing.values() if isinstance(existing, dict) else existing
    max_seq = 0
    for row in rows:
        todo_id = row.get("id") if isinstance(row, dict) else ""
        if not isinstance(todo_id, str) or not todo_id.startswith(prefix):
            continue
        try:
            max_seq = max(max_seq, int(todo_id.rsplit("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{prefix}{max_seq + 1:03d}"


def _acquire_lock(path, timeout: float = 5.0):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    while True:
        try:
            return os.open(str(lock_path), flags)
        except FileExistsError:
            if time.time() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID; defensive about platform gaps."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not signal it.
        return True
    except (OSError, AttributeError):
        # os.kill missing/unreliable (e.g. some Windows cases): treat as alive
        # so the caller falls back to the mtime threshold instead.
        return True
    return True


def _lock_is_orphaned(path, stale_after: float = 30.0) -> bool:
    """A lock is orphaned when its mtime is older than ``stale_after`` seconds
    OR its recorded PID is dead. An unparseable/missing PID counts as orphaned;
    a missing lock file does not (nothing to reclaim)."""
    lock_path = Path(path)
    try:
        st_mtime = lock_path.stat().st_mtime
    except OSError:
        return False
    if time.time() - st_mtime > stale_after:
        return True
    try:
        raw = lock_path.read_text(encoding="ascii", errors="replace").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return True
    return not _pid_is_alive(pid)


def _fold_latest(rows: list[dict]) -> dict:
    latest = {}
    for row in rows:
        todo_id = row.get("id")
        if isinstance(todo_id, str) and todo_id:
            latest[todo_id] = row
    return latest


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
        f.write("\n")


def _write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _log_error(repo_root, source: str, exc: Exception) -> None:
    try:
        path = _log_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{_utc_now()} {source}: {exc.__class__.__name__}: {exc}\n"
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    except OSError:
        pass


def _infer_tags(title: str) -> list[str]:
    tags = []
    bracket = re.match(r"^\[([A-Za-z0-9_, -]+)\]\s+", title)
    if bracket:
        tags.extend(bracket.group(1).replace(",", " ").split())
    tags.extend(re.findall(r"(?:^|\s)#([A-Za-z][A-Za-z0-9_-]{1,30})", title))
    seen = set()
    out = []
    for tag in tags:
        normalized = re.sub(r"[^a-z0-9_-]+", "", tag.lower())
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _capture_memory_followups(repo_root) -> list[dict]:
    path = Path(repo_root) / ".ai" / "memory.md"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    captures = []
    for line_no, line in enumerate(lines, 1):
        match = _MEMORY_FOLLOWUP_RE.match(line)
        if not match:
            continue
        title = match.group(1).strip()
        if not title:
            continue
        captures.append({
            "title": title,
            "tags": _infer_tags(title),
            "source": f"memory.md:{line_no}",
            "source_ref": f".ai/memory.md#L{line_no}",
        })
    return captures


def _capture_handoff_followups(repo_root) -> list[dict]:
    plans_dir = Path(repo_root) / ".ai" / "plans"
    candidates = sorted(plans_dir.glob("*.md"), key=lambda p: p.name)
    if not candidates:
        return []
    path = candidates[-1]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        # Plan file vanished between the glob and the read (TOCTOU).
        return []
    handoff_idx = None
    for idx, line in enumerate(lines):
        if re.match(r"^## Handoff\b", line):
            handoff_idx = idx
    if handoff_idx is None:
        return []
    followups_idx = None
    for idx in range(handoff_idx + 1, len(lines)):
        if re.match(r"^## Handoff\b", lines[idx]):
            break
        if re.match(r"^## Follow-ups\b", lines[idx]):
            followups_idx = idx
            break
    if followups_idx is None:
        return []
    captures = []
    for idx in range(followups_idx + 1, len(lines)):
        line = lines[idx]
        if re.match(r"^## \S", line):
            break
        match = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
        if not match:
            continue
        title = re.sub(r"^\[[ xX]\]\s+", "", match.group(1).strip())
        if title.lower() in {"none", "n/a", "na"}:
            continue
        line_no = idx + 1
        captures.append({
            "title": title,
            "tags": _infer_tags(title),
            "source": f"plans/{path.name}:{line_no}",
            "source_ref": f".ai/plans/{path.name}#L{line_no}",
        })
    return captures


def _run_git(repo_root, args: list[str]) -> subprocess.CompletedProcess:
    # ``-c core.quotePath=false`` disables git's default C-style quoting of
    # non-ASCII paths, so diff headers stay ``+++ b/.ai/packets/wîrd.md``
    # instead of ``+++ "b/.ai/packets/w\303\256rd.md"``. Without it the
    # quote/escape defeats the a//b/ prefix strip below, leaking TODOs from
    # skip-listed dirs and corrupting source_ref. See also _unquote_git_path.
    return subprocess.run(
        ["git", "-c", "core.quotePath=false"] + args,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _first_path_segment(path: str) -> str:
    return path.replace("\\", "/").split("/", 1)[0]


def _unquote_git_path(raw: str) -> str:
    """Decode a git C-style-quoted path (``"...\\303\\256..."``).

    Defense-in-depth for the case core.quotePath is forced on elsewhere or
    git quotes for another reason (control chars). A non-quoted path is
    returned unchanged.
    """
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    inner = raw[1:-1]
    # Reverse git's octal/C escaping by encoding the literal escapes to
    # latin-1 bytes, then decoding those bytes as UTF-8.
    try:
        decoded = inner.encode("latin-1", "backslashreplace").decode(
            "unicode_escape"
        )
        return decoded.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return inner


def _path_from_diff_header(line: str) -> str | None:
    raw = _unquote_git_path(line[4:].strip())
    if raw == "/dev/null":
        return None
    if raw.startswith("b/") or raw.startswith("a/"):
        return raw[2:]
    return raw


def _capture_diff_markers(repo_root, last_sha=None) -> list[dict]:
    args = ["diff", "--unified=0"]
    if last_sha:
        args.append(f"{last_sha}..HEAD")
    else:
        args.append("HEAD")
    proc = _run_git(repo_root, args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")

    captures = []
    current_path = None
    skipped = False
    new_line_no = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ "):
            current_path = _path_from_diff_header(line)
            skipped = (
                current_path is None
                or _first_path_segment(current_path) in SKIP_DIRS
                or any(current_path.startswith(p) for p in DIFF_SKIP_PREFIXES)
            )
            new_line_no = None
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_line_no = int(hunk.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            line_no = new_line_no
            if new_line_no is not None:
                new_line_no += 1
            if skipped or current_path is None:
                continue
            marker = _DIFF_MARKER_RE.search(line[1:])
            if not marker:
                continue
            title = marker.group("text").strip()
            # Strip the block/html terminator only for the comment style that
            # actually opened this line. ``/*`` and a `` * `` continuation line
            # both live inside a /* */ block → ``*/``; ``<!--`` → ``-->``.
            # Line comments (#, //) keep their text verbatim.
            opener = marker.group("open").strip()
            if opener in ("/*", "*") and title.endswith("*/"):
                title = title[:-2].strip()
            elif opener == "<!--" and title.endswith("-->"):
                title = title[:-3].strip()
            if not title:
                continue
            source = f"{current_path}:{line_no}" if line_no is not None else current_path
            captures.append({
                "title": title,
                "tags": _infer_tags(title),
                "source": source,
                "source_ref": source,
            })
            continue
        if line.startswith(" ") and new_line_no is not None:
            new_line_no += 1
    return captures


def _new_todo(capture: dict, existing: list[dict], now: str, captured_by: str) -> dict:
    source = capture["source"]
    title = capture["title"]
    return {
        "id": _allocate_id(existing, now),
        "title": title,
        "tags": capture.get("tags") or [],
        "source": source,
        "source_ref": capture.get("source_ref", source),
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "captured_by": captured_by,
        "dedup_hash": _dedup_hash(_dedup_source_key(source), title),
        "resolution": None,
        "rejected_hashes": [],
    }


def scan_and_append(repo_root, last_sha=None, captured_by: str = "maintenance") -> dict:
    root = Path(repo_root)
    ledger = _todos_path(root)
    rows = _load_jsonl(ledger)
    latest = _fold_latest(rows)
    by_hash = {
        row.get("dedup_hash"): row
        for row in latest.values()
        if isinstance(row.get("dedup_hash"), str)
    }
    captures = []
    errors = []
    for name, func in (
        ("memory", lambda: _capture_memory_followups(root)),
        ("handoff", lambda: _capture_handoff_followups(root)),
        ("diff", lambda: _capture_diff_markers(root, last_sha)),
    ):
        try:
            captures.extend(func())
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)})
            _log_error(root, name, exc)

    now = _utc_now()
    existing_for_ids = list(rows)
    to_append = []
    added = 0
    updated = 0
    for capture in captures:
        title = capture.get("title", "").strip()
        source = capture.get("source", "").strip()
        if not title or not source:
            continue
        dedup = _dedup_hash(_dedup_source_key(source), title)
        current = by_hash.get(dedup)
        if current:
            row = dict(current)
            # Refresh the title too: dedup is keyed on the NORMALIZED title, so
            # captures that normalize equal but differ in casing/punctuation
            # land here — the exported TODO.md should track the latest wording.
            row["title"] = title
            row["source"] = source
            row["source_ref"] = capture.get("source_ref", source)
            row["updated_at"] = now
            row["dedup_hash"] = dedup
            to_append.append(row)
            by_hash[dedup] = row
            updated += 1
            continue
        row = _new_todo(capture, existing_for_ids, now, captured_by)
        to_append.append(row)
        existing_for_ids.append(row)
        by_hash[dedup] = row
        added += 1

    for row in to_append:
        _append_jsonl(ledger, row)

    result = {
        "ok": not errors,
        "added": added,
        "updated": updated,
        "errors": errors,
    }
    if errors:
        result["banner"] = "TODO scan partial failure"
    if to_append:
        regen = regen_markdown(root)
        if not regen.get("ok"):
            result["ok"] = False
            result["banner"] = regen.get("banner", "TODO.md export stale")
    return result


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z]{4,}", text.lower())
        if token not in STOPWORDS
    }


def _commit_candidates(repo_root, last_sha=None) -> list[tuple[str, str]]:
    args = ["log"]
    if last_sha:
        args.append(f"{last_sha}..HEAD")
    else:
        args.extend(["-n", "50"])
    args.append("--format=%H%x00%s%n%b%x00")
    proc = _run_git(repo_root, args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git log failed")
    parts = proc.stdout.split("\x00")
    candidates = []
    for idx in range(0, len(parts) - 1, 2):
        sha = parts[idx].strip()
        text = parts[idx + 1].strip()
        if sha and text:
            candidates.append((sha, text))
    return candidates


def _decision_candidates(repo_root, last_sha=None) -> list[tuple[str, str]]:
    path = Path(repo_root) / ".ai" / "decisions.md"
    if not path.exists():
        return []
    args = ["diff", "--unified=0"]
    if last_sha:
        args.append(f"{last_sha}..HEAD")
    else:
        args.append("HEAD")
    args.extend(["--", ".ai/decisions.md"])
    proc = _run_git(repo_root, args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff decisions failed")
    out = []
    new_line_no = None
    for line in proc.stdout.splitlines():
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_line_no = int(hunk.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            line_no = new_line_no
            if new_line_no is not None:
                new_line_no += 1
            text = line[1:].strip()
            if text:
                evidence = f"decisions.md:{line_no}" if line_no is not None else "decisions.md"
                out.append((evidence, text))
            continue
        if line.startswith(" ") and new_line_no is not None:
            new_line_no += 1
    return out


def _match_strong_enough(title_tokens: set, evidence_tokens: set) -> bool:
    common = title_tokens & evidence_tokens
    if len(common) < 3:
        return False
    return any(len(tok) >= 6 for tok in common)


def _suggestion_for(todo: dict, commits: list[tuple[str, str]], decisions: list[tuple[str, str]]):
    title_tokens = _tokens(todo.get("title", ""))
    if len(title_tokens) < 3:
        return None
    rejected = set(todo.get("rejected_hashes") or [])
    for sha, message in commits:
        if sha in rejected:
            continue
        if _match_strong_enough(title_tokens, _tokens(message)):
            return ("commit-match", sha)
    for evidence, text in decisions:
        if evidence in rejected:
            continue
        if _match_strong_enough(title_tokens, _tokens(text)):
            return ("decision-match", evidence)
    return None


def auto_resolve(repo_root, last_sha=None) -> dict:
    root = Path(repo_root)
    ledger = _todos_path(root)
    latest = _fold_latest(_load_jsonl(ledger))
    errors = []
    try:
        commits = _commit_candidates(root, last_sha)
    except Exception as exc:
        commits = []
        errors.append({"source": "commits", "error": str(exc)})
        _log_error(root, "commits", exc)
    try:
        decisions = _decision_candidates(root, last_sha)
    except Exception as exc:
        decisions = []
        errors.append({"source": "decisions", "error": str(exc)})
        _log_error(root, "decisions", exc)

    now = _utc_now()
    appended = []
    for todo in sorted(latest.values(), key=lambda row: row.get("id", "")):
        if todo.get("status") != "open":
            continue
        suggestion = _suggestion_for(todo, commits, decisions)
        if suggestion is None:
            continue
        by, evidence = suggestion
        row = dict(todo)
        row["status"] = "resolved-suggested"
        row["updated_at"] = now
        row["resolution"] = {"by": by, "evidence": evidence, "at": now}
        appended.append(row)

    for row in appended:
        _append_jsonl(ledger, row)

    result = {"ok": not errors, "suggested": len(appended), "errors": errors}
    if errors:
        result["banner"] = "TODO resolve partial failure"
    if appended:
        regen = regen_markdown(root)
        if not regen.get("ok"):
            result["ok"] = False
            result["banner"] = regen.get("banner", "TODO.md export stale")
    return result


def _parse_iso(value: str):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title)).strip()


def _source_suffix(todo: dict) -> str:
    source = todo.get("source") or todo.get("source_ref")
    return f" (`{source}`)" if source else ""


def _suggested_suffix(todo: dict) -> str:
    resolution = todo.get("resolution") or {}
    by = resolution.get("by")
    evidence = resolution.get("evidence", "")
    if by == "commit-match" and evidence:
        return f" (commit `{evidence[:7]}`)"
    if evidence:
        return f" ({evidence})"
    return ""


def _resolved_suffix(todo: dict) -> str:
    resolution = todo.get("resolution") or {}
    at = resolution.get("at") or todo.get("updated_at") or ""
    date = at[:10] if at else "unknown"
    return f" (resolved {date})"


def _markdown_from_rows(rows: list[dict], now: str) -> str:
    latest = _fold_latest(rows)
    open_by_tag = {}
    suggested = []
    resolved = []
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=30)
    )
    for todo in latest.values():
        status = todo.get("status")
        if status == "open":
            tags = todo.get("tags") or ["untagged"]
            tag = tags[0] if isinstance(tags, list) and tags else "untagged"
            open_by_tag.setdefault(str(tag), []).append(todo)
        elif status == "resolved-suggested":
            suggested.append(todo)
        elif status == "resolved":
            resolved_at = _parse_iso((todo.get("resolution") or {}).get("at", ""))
            if resolved_at is None:
                resolved_at = _parse_iso(todo.get("updated_at", ""))
            if resolved_at is not None and resolved_at >= cutoff:
                resolved.append(todo)

    lines = [
        "<!-- AUTO-GENERATED from .ai/ledgers/todos.jsonl - do not edit by hand -->",
        f"<!-- last regen: {now} -->",
        "",
        "## Open",
        "",
    ]
    if open_by_tag:
        for tag in sorted(open_by_tag):
            lines.append(f"### [{tag}]")
            for todo in sorted(open_by_tag[tag], key=lambda row: row.get("id", "")):
                title = _clean_title(todo.get("title", ""))
                lines.append(f"- [ ] {todo.get('id')} - {title}{_source_suffix(todo)}")
            lines.append("")
    else:
        lines.extend(["_No open TODOs._", ""])

    lines.extend(["## Suggested resolve", ""])
    if suggested:
        for todo in sorted(suggested, key=lambda row: row.get("id", "")):
            title = _clean_title(todo.get("title", ""))
            lines.append(f"- [?] {todo.get('id')} - {title}{_suggested_suffix(todo)}")
        lines.append("")
    else:
        lines.extend(["_No suggested resolutions._", ""])

    lines.extend(["## Resolved (last 30 days)", ""])
    if resolved:
        for todo in sorted(resolved, key=lambda row: row.get("id", "")):
            title = _clean_title(todo.get("title", ""))
            lines.append(f"- [x] {todo.get('id')} - {title}{_resolved_suffix(todo)}")
        lines.append("")
    else:
        lines.extend(["_No recently resolved TODOs._", ""])
    return "\n".join(lines).rstrip() + "\n"


def _ensure_skip_worktree(repo_root) -> None:
    """Mark .ai/TODO.md as skip-worktree so locally regenerated content never
    shows up as a git change.

    The repo ships .ai/TODO.md as an empty template (committed once); this keeps
    each developer's live export from polluting `git status` or getting committed
    accidentally. skip-worktree is a per-clone flag and doesn't travel with the
    repo, so we (re)apply it here on every export — the first regen after a fresh
    clone or install sets it automatically. No-op when the file is untracked
    (e.g. a target project that .gitignores it) or git isn't available.
    """
    try:
        tracked = _run_git(repo_root, ["ls-files", "--error-unmatch", ".ai/TODO.md"])
        if tracked.returncode != 0:
            return  # untracked, or not a git repo — nothing to pin
        _run_git(repo_root, ["update-index", "--skip-worktree", ".ai/TODO.md"])
    except OSError:
        pass  # git not installed — silent no-op


def regen_markdown(repo_root) -> dict:
    root = Path(repo_root)
    lock = _lock_path(root)
    fd = _acquire_lock(lock)
    if fd is None:
        # Lock held: if it looks orphaned (dead PID or stale mtime), reclaim it
        # once and retry. Otherwise leave the live holder alone.
        if _lock_is_orphaned(lock):
            try:
                os.unlink(str(lock))
            except OSError:
                pass
            fd = _acquire_lock(lock)
    if fd is None:
        return {"ok": False, "banner": "TODO.md export stale"}
    try:
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        now = _utc_now()
        markdown = _markdown_from_rows(_load_jsonl(_todos_path(root)), now)
        _write_text_lf(_todo_md_path(root), markdown)
        _ensure_skip_worktree(root)
        return {"ok": True, "banner": None}
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass
