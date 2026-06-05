"""Demo: start the dashboard server and inject 3 simulated agents.

Run from repo root:
    python .ai/dashboard/scripts/demo.py

Then open the printed URL, switch to the "Terminals" tab, and click
"Open all running" to see the three fake agents streaming side by side.
Each agent prints output every 1-3 seconds, pauses to ask questions, and
reads stdin from the dashboard input box.

This is meant to exercise the multi-terminal UI without burning real Claude
API credits. Real orchestrations go through Run -> Start.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import uuid
from pathlib import Path

PORT = int(os.environ.get("DEMO_PORT", "8770"))
REPO_ROOT = Path(__file__).resolve().parents[3]
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


# Each fake agent is a small Python program executed as a subprocess so the
# dashboard treats it like a real `claude -p` job: stdout streams, stdin reads.
# All three agents LOOP indefinitely so the user can interact at their own
# pace - they print activity, pause on a stdin question, react to the reply,
# then go around again.

PLANNER_SCRIPT = r"""
import sys, time
def out(s): sys.stdout.write(s + "\n"); sys.stdout.flush()
cycle = 0
while True:
    cycle += 1
    out(f"[planner] === cycle {cycle} ===")
    time.sleep(0.6); out("[planner] reading .ai/project.yaml")
    time.sleep(0.8); out("[planner] reading .ai/memory.md")
    time.sleep(0.8); out("[planner] classifying task: medium")
    time.sleep(0.5); out("[planner] drafting packet 01-orchestrate.md")
    for i in range(1, 4):
        time.sleep(0.9); out(f"[planner] step {i}/3: outlining sub-task")
    out("")
    out("[planner] ? which strategy this cycle?")
    out("[planner]   (a) refactor first, then add tests")
    out("[planner]   (b) write tests first (TDD), then refactor")
    out("[planner] type 'a' or 'b' and press enter (or anything else):")
    sys.stdout.flush()
    choice = (sys.stdin.readline() or "").strip().lower()
    if not choice:
        out("[planner] stdin closed; defaulting to 'b' and looping again")
        choice = "b"
    out(f"[planner] you chose: {choice}")
    time.sleep(0.4); out("[planner] finalising packet")
    out("[planner] handoff -> executor\n")
    time.sleep(2.0)
"""

EXECUTOR_SCRIPT = r"""
import sys, time
def out(s): sys.stdout.write(s + "\n"); sys.stdout.flush()
cycle = 0
while True:
    cycle += 1
    out(f"[executor] === cycle {cycle} ===")
    out("[executor] picking up packet")
    time.sleep(0.6); out("[executor] $ git status")
    time.sleep(0.5); out("[executor] working tree clean")
    time.sleep(0.5); out("[executor] $ python -m pytest -x --tb=short")
    for i in range(1, 6):
        time.sleep(0.6); out(f"[executor]   collecting test {i}/5 ...")
    out("[executor] 5 passed in 0.82s")
    time.sleep(0.4)
    out("")
    out("[executor] ! warning: file modified outside of plan: README.md")
    out("[executor] continue anyway? (y/n)")
    sys.stdout.flush()
    ans = (sys.stdin.readline() or "").strip().lower()
    if not ans:
        out("[executor] stdin closed; assuming 'y' and looping")
        ans = "y"
    out(f"[executor] reply: {ans}")
    if ans.startswith("y"):
        for i in range(1, 4):
            time.sleep(0.7); out(f"[executor] applying edit {i}/3")
        out("[executor] $ python -m pytest")
        time.sleep(0.8); out("[executor] all green")
        out("[executor] handoff -> reviewer\n")
    else:
        out("[executor] aborting per operator decision\n")
    time.sleep(2.0)
"""

REVIEWER_SCRIPT = r"""
import sys, time
def out(s): sys.stdout.write(s + "\n"); sys.stdout.flush()
cycle = 0
findings = [
    "no public API broken",
    "tests cover new endpoints",
    "stdin lock prevents interleaved writes",
    "SSE catch-up sends existing log on reconnect",
    "heartbeat keeps connection alive through proxies",
]
while True:
    cycle += 1
    out(f"[reviewer] === cycle {cycle} ===")
    out("[reviewer] waiting on executor handoff")
    for _ in range(2):
        time.sleep(1.0); out("[reviewer] .")
    out("[reviewer] handoff received, reading diff")
    for i, f in enumerate(findings, 1):
        time.sleep(1.0); out(f"[reviewer] [{i}/{len(findings)}] OK - {f}")
    time.sleep(0.4)
    out("")
    out("[reviewer] ? approve the change?")
    out("[reviewer] reply 'approve' or 'request-changes':")
    sys.stdout.flush()
    verdict = (sys.stdin.readline() or "").strip().lower()
    if not verdict:
        out("[reviewer] stdin closed; defaulting to 'approve' and looping")
        verdict = "approve"
    out(f"[reviewer] verdict: {verdict}")
    time.sleep(0.4)
    if verdict.startswith("approve"):
        out("[reviewer] OK - posting approval to .ai/decisions.md\n")
    else:
        out("[reviewer] requesting changes -> handing back to executor\n")
    time.sleep(2.0)
"""


def load_serve():
    spec = importlib.util.spec_from_file_location("dashboard_serve", SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    serve = load_serve()

    # Start the dashboard server on PORT, fall back across a wider window if
    # busy, and finally let the OS pick an ephemeral port (bind to 0).
    httpd = None
    bound: int | None = None
    last_err: OSError | None = None
    for candidate in [PORT + i for i in range(20)] + [0]:
        try:
            # Reuse serve._ThreadedServer (daemon_threads + Windows
            # SO_EXCLUSIVEADDRUSE) so Ctrl+C in the demo isn't held up
            # by in-flight requests and two demo instances can't silently
            # share a port on Windows.
            httpd = serve._ThreadedServer(("127.0.0.1", candidate), serve.Handler)
            bound = httpd.server_address[1]
            break
        except OSError as e:
            last_err = e
    if not httpd or bound is None:
        raise SystemExit(f"could not bind: {last_err}")

    threading.Thread(target=httpd.serve_forever, daemon=True, name="demo-http").start()
    url = f"http://localhost:{bound}/.ai/dashboard/"
    print(f"\nDemo dashboard running: {url}")
    print("Open it, switch to the 'Terminals' tab, click 'Open all running'.")
    print("Each agent will pause and wait for your stdin reply (Enter to send).\n")

    # Spawn 3 fake agents through the new helper. They appear as jobs in /api/jobs.
    for name, script in [
        ("planner", PLANNER_SCRIPT),
        ("executor", EXECUTOR_SCRIPT),
        ("reviewer", REVIEWER_SCRIPT),
    ]:
        job_id = str(uuid.uuid4())
        serve._start_subprocess_job(
            job_id=job_id,
            kind="orchestrate",
            task=f"demo: simulated {name}",
            argv=[sys.executable, "-u", "-c", script],
        )
        print(f"  spawned {name}: {job_id}")

    print("\nCtrl+C to stop the demo.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
