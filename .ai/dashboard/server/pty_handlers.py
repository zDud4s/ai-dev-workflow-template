"""Request handlers for PTY (real shell) sessions over WebSocket.

Covers /api/ptys (list), /api/ptys/<id> (get), POST /api/ptys (create),
/api/ptys/<id>/kill and the /api/ptys/<id>/io WebSocket upgrade. Extracted from
serve.py as the ``PtyRoutes`` mixin; ``Handler`` inherits it so routing and
``serve.Handler._handle_pty_ws`` resolve via MRO. The PTY registry + spawn/kill
helpers and the WebSocket framing are imported from their owning ``server.*``
modules (importing them from serve would be circular).
"""
from __future__ import annotations

import json
import queue as _stdqueue
import secrets
import threading

from server.paths import ROOT
from server.pty import PTYS, PTYS_LOCK, _pty_kill, _pty_spawn, _pty_summary
from server.ws import WebSocket, _WsClosed


class PtyRoutes:
    """PTY (real shell) WebSocket endpoints, mixed into ``Handler``."""

    def _handle_ptys_list(self) -> None:
        with PTYS_LOCK:
            out = [_pty_summary(e) for e in PTYS.values()]
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, {"ptys": out})

    def _handle_pty_get(self, pty_id: str) -> None:
        with PTYS_LOCK:
            entry = PTYS.get(pty_id)
            summary = _pty_summary(entry) if entry else None
        if not summary:
            self._json(404, {"error": "pty not found"})
            return
        self._json(200, summary)

    def _handle_pty_create(self, body: dict) -> None:
        shell = body.get("shell")
        if shell is not None and not isinstance(shell, str):
            self._json(400, {"error": "shell must be a string"})
            return
        cwd_raw = body.get("cwd")
        if cwd_raw is not None and not isinstance(cwd_raw, str):
            self._json(400, {"error": "cwd must be a string"})
            return
        # Constrain the INITIAL ``cwd`` to inside the repo. Note: this
        # only pins where the shell *starts*; once running, the shell
        # can `cd ..` freely — we don't chroot or pivot_root. Treat this
        # as a UX guardrail against accidents (paste a wrong path),
        # not as a sandbox boundary. Empty/None falls back to the repo
        # root.
        cwd = None
        if cwd_raw:
            try:
                resolved = (ROOT / cwd_raw).resolve()
                resolved.relative_to(ROOT.resolve())
                if not resolved.is_dir():
                    self._json(404, {"error": f"cwd not found or not a directory: {cwd_raw}"})
                    return
                cwd = str(resolved)
            except (ValueError, OSError):
                self._json(403, {"error": "cwd must be inside the repo"})
                return
        try:
            cols = int(body.get("cols") or 80)
            rows = int(body.get("rows") or 24)
        except (TypeError, ValueError):
            self._json(400, {"error": "cols/rows must be integers"})
            return
        cols = max(20, min(500, cols))
        rows = max(5,  min(200, rows))
        try:
            entry = _pty_spawn(shell, cwd, cols, rows)
        except ImportError as e:
            # Windows without pywinpty installed.
            self._json(503, {"error": str(e)})
            return
        except FileNotFoundError as e:
            print(f"[serve] pty spawn missing binary: {e}", flush=True)
            self._json(503, {"error": "shell not found"})
            return
        except Exception as e:
            print(f"[serve] pty spawn failed: {e}", flush=True)
            self._json(500, {"error": "failed to spawn PTY"})
            return
        self._json(201, _pty_summary(entry, include_token=True))

    def _handle_pty_kill(self, pty_id: str) -> None:
        if _pty_kill(pty_id):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "pty not found"})

    def _handle_pty_ws(self, pty_id: str, qs: dict[str, list[str]] | None = None) -> None:
        """WebSocket endpoint: bidirectional byte stream + JSON control
        messages. Frames:
          * binary  -> bytes written to the PTY master (keystrokes)
          * text    -> JSON control: {"type":"resize","cols":N,"rows":M}
        Server -> client:
          * binary  -> bytes read off the PTY master
          * text    -> JSON: {"type":"exit"} on EOF

        Requires the per-PTY token issued by /api/ptys (POST). The token
        is passed either via the ``token`` query string parameter or the
        ``Sec-WebSocket-Protocol`` header value. Without a valid token
        the upgrade is refused with 403 so a malicious script on the
        dashboard origin can't enumerate PTYs and slurp their scrollback.
        """
        with PTYS_LOCK:
            entry = PTYS.get(pty_id)
        if not entry:
            self.send_error(404, "pty not found")
            return
        expected = entry.get("_token")
        provided = ""
        if qs:
            provided = (qs.get("token") or [""])[0] or ""
        if not provided:
            # Allow token via subprotocol header too — useful when the
            # JS client wants to keep the URL clean of secrets.
            provided = (self.headers.get("Sec-WebSocket-Protocol") or "").strip()
        if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
            self.send_error(403, "pty token required")
            return
        ws = WebSocket.accept(self)
        if ws is None:
            return  # handshake already sent its own error
        q: _stdqueue.Queue = _stdqueue.Queue(maxsize=4096)
        # Catch-up: dump the ring buffer first so the client sees existing
        # output even if it attached after the session started.
        with entry["_lock"]:
            if entry["_ring"]:
                try:
                    ws.send_binary(bytes(entry["_ring"]))
                except _WsClosed:
                    return
            entry["_subscribers"].append(q)
            ended = entry.get("status") == "ended"
        if ended:
            try:
                ws.send_text(json.dumps({"type": "exit"}))
            except _WsClosed:
                pass
            ws.close()
            with entry["_lock"]:
                try: entry["_subscribers"].remove(q)
                except ValueError: pass
            return

        # Stop event so the writer thread can clean up when the reader
        # bails (or vice versa).
        stop = threading.Event()

        def pump_outbound():
            try:
                while not stop.is_set():
                    try:
                        chunk = q.get(timeout=15)
                    except _stdqueue.Empty:
                        # Heartbeat ping keeps proxies / browsers happy.
                        try:
                            ws._send_frame(WebSocket.OPCODE_PING, b"")
                        except _WsClosed:
                            return
                        continue
                    if chunk is None:
                        try:
                            ws.send_text(json.dumps({"type": "exit"}))
                        except _WsClosed:
                            pass
                        return
                    try:
                        ws.send_binary(chunk)
                    except _WsClosed:
                        return
            finally:
                stop.set()

        writer = threading.Thread(target=pump_outbound, daemon=True)
        writer.start()

        try:
            while not stop.is_set():
                try:
                    opcode, data = ws.recv()
                except _WsClosed:
                    break
                if opcode == WebSocket.OPCODE_BIN:
                    pty = entry.get("_pty")
                    if pty is not None:
                        try:
                            pty.write(data)
                        except (OSError, ValueError) as e:
                            # OSError covers EBADF / EPIPE on a dead PTY;
                            # ValueError covers writes against a closed fd.
                            # Either way we can't recover — break the loop
                            # but record the cause so a flood of "WS closed
                            # unexpectedly" reports has context.
                            print(f"[serve] pty_ws write({pty_id}) failed: {e}", flush=True)
                            break
                elif opcode == WebSocket.OPCODE_TEXT:
                    # Control message (resize, etc.) JSON-encoded.
                    try:
                        msg = json.loads(data.decode("utf-8", errors="replace"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if msg.get("type") == "resize":
                        try:
                            cols = max(20, min(500, int(msg.get("cols") or 80)))
                            rows = max(5,  min(200, int(msg.get("rows") or 24)))
                        except (TypeError, ValueError):
                            continue
                        pty = entry.get("_pty")
                        if pty is not None:
                            try:
                                pty.resize(cols, rows)
                            except (OSError, ValueError) as e:
                                # resize is best-effort; some backends throw
                                # on a stale handle. Don't break the loop —
                                # the user can still type / read — but log.
                                print(f"[serve] pty_ws resize({pty_id}) failed: {e}", flush=True)
                        with entry["_lock"]:
                            entry["cols"] = cols
                            entry["rows"] = rows
        finally:
            stop.set()
            with entry["_lock"]:
                try: entry["_subscribers"].remove(q)
                except ValueError: pass
            try:
                ws.close()
            except (OSError, _WsClosed):
                # Already closed by the peer — common path, not worth logging.
                pass
