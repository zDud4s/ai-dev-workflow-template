"""Local dashboard server for the AI workflow.

Run from repo root:
    python .ai/dashboard/serve.py

Then open http://localhost:8765/.ai/dashboard/

Serves the whole repo as static files (read-only) plus a small JSON API:

    GET  /api/list?path=.ai/plans              ->  {"entries": [...]}
    POST /api/memory       {topic, fact}        ->  appends to .ai/memory.md
    POST /api/decisions    {date, decision,
                            why, consequence,
                            revisit}            ->  appends to .ai/decisions.md
    POST /api/events/clear                      ->  truncates .ai/events.jsonl
    POST /api/models/dispatch_mode {mode}       ->  flips dispatch_mode in
                                                    .ai/models.yaml

    POST /api/jobs            {kind, task}      ->  spawn an orchestrate /
                                                    planner subprocess
    GET  /api/jobs                              ->  list recent jobs
    GET  /api/jobs/<id>?tail=N                  ->  job details + last N log lines
    POST /api/jobs/<id>/cancel                  ->  terminate the subprocess
"""
from __future__ import annotations

import datetime as _dt
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import uuid
from collections import deque
from pathlib import Path

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
ROOT = Path(__file__).resolve().parents[2]  # repo root
JOBS_DIR = ROOT / ".ai" / "dashboard" / "jobs"

# Allowed job kinds and the skill name each invokes inside `claude -p`.
JOB_KINDS = {
    "orchestrate": "orchestrate",
    "plan": "planner",
}

# In-memory job registry. State is lost on server restart by design;
# log files survive on disk for forensic reading.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBS_MAX = 50  # cap memory; oldest finished entries get evicted


def _read_yaml_field(path: Path, field: str) -> dict:
    """Minimal YAML helper to pull a top-level mapping like `session: {...}`.

    Avoids a PyYAML dependency. Only handles the simple two-line shape used in
    .ai/models.yaml. Returns an empty dict on any failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if not in_block:
            if stripped.startswith(field + ":"):
                in_block = True
            continue
        if not stripped:
            break
        if not stripped.startswith((" ", "\t")):
            break
        m = re.match(r"^\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(\S.*)?$", stripped)
        if not m:
            continue
        val = (m.group(2) or "").strip()
        val = val.split("#", 1)[0].strip()  # strip inline comment
        out[m.group(1)] = val.strip('"\'')
    return out


def _spawn_job(job_id: str, kind: str, task: str) -> None:
    """Spawn `claude -p` in a worker thread and capture stdout+stderr to a log file."""
    skill = JOB_KINDS[kind]
    session = _read_yaml_field(ROOT / ".ai" / "models.yaml", "session")
    model = session.get("model") or "claude-sonnet-4-6"
    claude_bin = shutil.which("claude") or "claude"

    prompt = (
        f"Use the {skill} skill.\n"
        f"Task: {task}\n\n"
        "Run non-interactively: never ask the user a clarifying question. "
        "If you genuinely cannot proceed, emit the Escalation block defined "
        "in .ai/workflow/dispatch.md and exit non-zero."
    )

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = JOBS_DIR / f"{job_id}.log"

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        JOBS[job_id]["log_path"] = str(log_path)
        JOBS[job_id]["command"] = f"{claude_bin} -p ... --model {model}"

    def runner() -> None:
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as logf:
                logf.write(f"# job {job_id} kind={kind} model={model}\n")
                logf.write(f"# task: {task}\n\n")
                logf.flush()
                proc = subprocess.Popen(
                    [claude_bin, "-p", prompt, "--model", model],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                with JOBS_LOCK:
                    JOBS[job_id]["pid"] = proc.pid
                exit_code = proc.wait()
            with JOBS_LOCK:
                j = JOBS[job_id]
                if j["status"] == "cancelling":
                    j["status"] = "cancelled"
                else:
                    j["status"] = "done" if exit_code == 0 else "failed"
                j["exit_code"] = exit_code
                j["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        except Exception as e:  # noqa: BLE001 (don't crash the server)
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(e)
                JOBS[job_id]["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()


def _cancel_job(job_id: str) -> bool:
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j["status"] not in {"running", "queued"}:
            return False
        j["status"] = "cancelling"
        pid = j.get("pid")
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)
            else:
                os.kill(pid, 15)
        except (ProcessLookupError, OSError):
            pass
    return True


def _evict_old_jobs() -> None:
    """Keep JOBS dict bounded; remove oldest finished entries when over cap."""
    with JOBS_LOCK:
        if len(JOBS) <= JOBS_MAX:
            return
        finished = [
            (jid, j) for jid, j in JOBS.items()
            if j["status"] in {"done", "failed", "cancelled"}
        ]
        finished.sort(key=lambda x: x[1].get("ended_at") or "")
        for jid, _j in finished[: len(JOBS) - JOBS_MAX]:
            JOBS.pop(jid, None)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # ----- routing -----
    def do_GET(self):  # noqa: N802 (stdlib signature)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/list":
            self._handle_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/jobs":
            self._handle_jobs_list()
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)", parsed.path)
        if m:
            self._handle_job_get(m.group(1), urllib.parse.parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_json_body()
        if body is None:
            return  # already responded with 400
        if parsed.path == "/api/memory":
            self._handle_memory(body)
        elif parsed.path == "/api/decisions":
            self._handle_decisions(body)
        elif parsed.path == "/api/events/clear":
            self._handle_events_clear()
        elif parsed.path == "/api/models/dispatch_mode":
            self._handle_dispatch_mode(body)
        elif parsed.path == "/api/models/phase":
            self._handle_phase_update(body)
        elif parsed.path == "/api/jobs":
            self._handle_jobs_create(body)
        else:
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/cancel", parsed.path)
            if m:
                self._handle_job_cancel(m.group(1))
            else:
                self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    # ----- helpers -----
    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > 64 * 1024:
            self._json(413, {"error": "payload too large"})
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json(400, {"error": "invalid JSON", "detail": str(e)})
            return None

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write(f"[dashboard] {fmt % args}\n")

    # ----- GET handlers -----
    def _handle_list(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path", [""])[0] or "").lstrip("/").replace("\\", "/")
        target = (ROOT / rel).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            self._json(403, {"error": "path outside repo root"})
            return
        if not target.is_dir():
            self._json(404, {"error": "not a directory", "path": rel})
            return
        entries = sorted(p.name for p in target.iterdir() if not p.name.startswith("."))
        self._json(200, {"path": rel, "entries": entries})

    # ----- POST handlers -----
    def _handle_memory(self, body: dict) -> None:
        topic = (body.get("topic") or "").strip()
        fact = (body.get("fact") or "").strip()
        if not topic or not fact:
            self._json(400, {"error": "topic and fact are required"})
            return
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", topic):
            self._json(400, {"error": "topic must be lowercase letters, digits, '-' or '_' (max 32)"})
            return
        if len(fact) > 500:
            self._json(400, {"error": "fact must be 500 chars or fewer"})
            return
        # Single-line fact: collapse whitespace
        fact_single = " ".join(fact.split())
        date = _dt.date.today().strftime("%Y-%m-%d")
        line = f"- {date} [{topic}] {fact_single}\n"
        path = ROOT / ".ai" / "memory.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + line, encoding="utf-8")
        self._json(200, {"ok": True, "line": line.rstrip()})

    def _handle_decisions(self, body: dict) -> None:
        date = (body.get("date") or _dt.date.today().strftime("%Y-%m-%d")).strip()
        decision = (body.get("decision") or "").strip()
        why = (body.get("why") or "").strip()
        consequence = (body.get("consequence") or "").strip()
        revisit = (body.get("revisit") or "").strip()
        if not decision or not why:
            self._json(400, {"error": "decision and why are required"})
            return
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            self._json(400, {"error": "date must be YYYY-MM-DD"})
            return
        for label, val in [("decision", decision), ("why", why), ("consequence", consequence), ("revisit", revisit)]:
            if len(val) > 1000:
                self._json(400, {"error": f"{label} must be 1000 chars or fewer"})
                return
        entry = (
            f"\n## {date} — {decision.splitlines()[0]}\n"
            f"- Date: {date}\n"
            f"- Decision: {decision}\n"
            f"- Why: {why}\n"
            f"- Consequence: {consequence or '—'}\n"
            f"- Revisit conditions: {revisit or '—'}\n"
        )
        path = ROOT / ".ai" / "decisions.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + entry, encoding="utf-8")
        self._json(200, {"ok": True, "entry": entry})

    def _handle_events_clear(self) -> None:
        path = ROOT / ".ai" / "events.jsonl"
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            self._json(500, {"error": "could not clear events", "detail": str(e)})
            return
        self._json(200, {"ok": True})

    # ----- jobs -----
    def _job_summary(self, j: dict) -> dict:
        return {
            "id": j["id"],
            "kind": j["kind"],
            "task": j["task"][:120],
            "status": j["status"],
            "created_at": j.get("created_at"),
            "started_at": j.get("started_at"),
            "ended_at": j.get("ended_at"),
            "exit_code": j.get("exit_code"),
            "command": j.get("command"),
        }

    def _handle_jobs_create(self, body: dict) -> None:
        kind = (body.get("kind") or "").strip()
        task = (body.get("task") or "").strip()
        if kind not in JOB_KINDS:
            self._json(400, {"error": f"kind must be one of {sorted(JOB_KINDS)}"})
            return
        if not task:
            self._json(400, {"error": "task is required"})
            return
        if len(task) > 4000:
            self._json(400, {"error": "task must be 4000 chars or fewer"})
            return
        if not shutil.which("claude"):
            self._json(503, {"error": "`claude` CLI not found on PATH"})
            return
        job_id = str(uuid.uuid4())
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "kind": kind,
                "task": task,
                "status": "queued",
                "created_at": now,
                "pid": None,
                "log_path": None,
                "exit_code": None,
                "started_at": None,
                "ended_at": None,
            }
        _spawn_job(job_id, kind, task)
        _evict_old_jobs()
        with JOBS_LOCK:
            self._json(201, self._job_summary(JOBS[job_id]))

    def _handle_jobs_list(self) -> None:
        with JOBS_LOCK:
            items = [self._job_summary(j) for j in JOBS.values()]
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, {"jobs": items})

    def _handle_job_get(self, job_id: str, qs: dict[str, list[str]]) -> None:
        try:
            tail = max(1, min(2000, int((qs.get("tail", ["200"])[0]))))
        except ValueError:
            tail = 200
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            summary = self._job_summary(j) if j else None
            log_path = j.get("log_path") if j else None
        if not summary:
            self._json(404, {"error": "job not found"})
            return
        log_lines: list[str] = []
        if log_path and Path(log_path).exists():
            try:
                # Tail the last N lines without loading the whole file.
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    log_lines = list(deque(f, maxlen=tail))
            except OSError:
                log_lines = []
        summary["log_tail"] = "".join(log_lines)
        self._json(200, summary)

    def _handle_job_cancel(self, job_id: str) -> None:
        if _cancel_job(job_id):
            self._json(200, {"ok": True})
        else:
            self._json(409, {"error": "job not running or not found"})

    def _handle_dispatch_mode(self, body: dict) -> None:
        mode = (body.get("mode") or "").strip()
        if mode not in {"auto", "manual"}:
            self._json(400, {"error": "mode must be 'auto' or 'manual'"})
            return
        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        text = path.read_text(encoding="utf-8")
        # Replace existing `dispatch_mode: <value>` line (with optional inline comment), or insert near top.
        line_re = re.compile(r"^(dispatch_mode:\s*)\S+(\s*(?:#.*)?)$", re.M)
        if line_re.search(text):
            new_text = line_re.sub(rf"\g<1>{mode}\g<2>", text, count=1)
        else:
            # Insert after the first non-comment, non-blank line — keep it simple.
            new_text = f"dispatch_mode: {mode}    # auto | manual\n\n" + text
        path.write_text(new_text, encoding="utf-8")
        self._json(200, {"ok": True, "mode": mode})

    # ----- phase config edit -----
    _PHASES = {"session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
    _TOOLS = {"claude", "codex"}
    _PHASE_MODES = {"inline", "agent", "dispatcher"}
    _REASONING = {"xhigh", "high", "medium", "low"}

    def _handle_phase_update(self, body: dict) -> None:
        phase = (body.get("phase") or "").strip()
        if phase not in self._PHASES:
            self._json(400, {"error": f"phase must be one of {sorted(self._PHASES)}"})
            return
        # All fields optional; only those present are updated.
        updates: dict[str, str | None] = {}
        if "tool" in body:
            tool = (body.get("tool") or "").strip()
            if tool not in self._TOOLS:
                self._json(400, {"error": f"tool must be one of {sorted(self._TOOLS)}"})
                return
            updates["tool"] = tool
        if "model" in body:
            model = (body.get("model") or "").strip()
            if not model or len(model) > 80 or not re.fullmatch(r"[A-Za-z0-9._\-]+", model):
                self._json(400, {"error": "model must be 1-80 chars [A-Za-z0-9._-]"})
                return
            updates["model"] = model
        if "mode" in body:
            mode = (body.get("mode") or "").strip()
            if mode and mode not in self._PHASE_MODES:
                self._json(400, {"error": f"mode must be one of {sorted(self._PHASE_MODES)} or empty"})
                return
            updates["mode"] = mode or None  # empty => remove the line
        if "reasoning_effort" in body:
            re_eff = (body.get("reasoning_effort") or "").strip()
            if re_eff and re_eff not in self._REASONING:
                self._json(400, {"error": f"reasoning_effort must be one of {sorted(self._REASONING)} or empty"})
                return
            updates["reasoning_effort"] = re_eff or None

        if not updates:
            self._json(400, {"error": "no updatable fields provided (tool, model, mode, reasoning_effort)"})
            return

        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        try:
            new_text = _patch_phase_block(path.read_text(encoding="utf-8"), phase, updates)
        except ValueError as e:
            self._json(404, {"error": str(e)})
            return
        path.write_text(new_text, encoding="utf-8")
        self._json(200, {"ok": True, "phase": phase, "updated": updates})


def _patch_phase_block(text: str, phase: str, updates: dict[str, str | None]) -> str:
    """Update fields under a top-level YAML mapping like ``plan:\\n  tool: ...``.

    For each key in updates:
      - value is a string -> replace existing `  <key>: <old>` line, or insert
        as the first child line after the header
      - value is None     -> remove the `  <key>: ...` line if present
    """
    lines = text.splitlines(keepends=False)
    n = len(lines)
    header_idx = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(phase)}\s*:\s*(#.*)?$", ln):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"phase block `{phase}:` not found in models.yaml")
    # Find end of this block (next non-indented, non-blank line)
    end_idx = n
    for j in range(header_idx + 1, n):
        ln = lines[j]
        if ln.strip() == "":
            continue
        if not ln.startswith((" ", "\t")):
            end_idx = j
            break
    block = lines[header_idx + 1 : end_idx]
    # Track existing keys
    key_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*)?$")
    indent = "  "
    for ln in block:
        m = key_re.match(ln)
        if m:
            indent = m.group(1)
            break

    def render(key: str, val: str) -> str:
        return f"{indent}{key}: {val}"

    new_block: list[str] = list(block)
    for key, val in updates.items():
        existing_idx = None
        for k, ln in enumerate(new_block):
            m = key_re.match(ln)
            if m and m.group(2) == key:
                existing_idx = k
                break
        if val is None:
            if existing_idx is not None:
                new_block.pop(existing_idx)
            continue
        if existing_idx is not None:
            # Preserve inline comment if any
            ln = new_block[existing_idx]
            m = re.match(r"^(\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*)\S+(\s*(?:#.*)?)$", ln)
            if m:
                new_block[existing_idx] = f"{m.group(1)}{val}{m.group(2)}"
            else:
                new_block[existing_idx] = render(key, val)
        else:
            # Insert as the last non-empty child line
            insert_at = len(new_block)
            while insert_at > 0 and new_block[insert_at - 1].strip() == "":
                insert_at -= 1
            new_block.insert(insert_at, render(key, val))

    new_lines = lines[: header_idx + 1] + new_block + lines[end_idx:]
    out = "\n".join(new_lines)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def main() -> None:
    last_err: OSError | None = None
    for candidate in (PORT, PORT + 1, PORT + 2, PORT + 3):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", candidate), Handler)
        except OSError as e:
            last_err = e
            continue
        with httpd:
            url = f"http://localhost:{candidate}/.ai/dashboard/"
            print(f"AI workflow dashboard: {url}")
            print("Press Ctrl+C to stop.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped.")
        return
    raise SystemExit(f"could not bind to any port starting at {PORT}: {last_err}")


if __name__ == "__main__":
    main()
