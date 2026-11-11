"""Cleartext HTTP/2 (h2c) smoke test.

Speaks HTTP/2 prior-knowledge over a raw socket (preface + SETTINGS, then
HEADERS/DATA frames), and checks that:
  - GET /api/health answers 200 with the same JSON body as over HTTP/1.1,
  - request bodies (POST with DATA frames) work, e.g. 400 validation errors,
  - unknown paths 404 with the same JSON error body,
  - SSE events stream as DATA frames over h2c,
  - a plain urllib HTTP/1.1 client still works on the same port.

Also invoked by tests/web_smoke.py against that server (h2_checks) so the
same test command covers both protocols.

Run:  python3 tests/http2_smoke.py
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import h2.config
    import h2.connection
    import h2.events
    # h2 >= 4.2 emits ResponseReceived; older 4.x emits the legacy Headers.
    _H2_RES_EVENT = (getattr(h2.events, "ResponseReceived", None)
                     or h2.events.Headers)
except ImportError:
    print("h2 not installed; skipping HTTP/2 smoke test")
    sys.exit(0)

from ralph import __version__  # noqa: E402
from ralph.web import (  # noqa: E402
    Handler,
    ServerContext,
    _DualRequestHandler,
    _H2,
    _ThreadingTCPServer,
)

assert _H2, "ralph.web must see the h2 package when the test can"


class H2Client:
    """Minimal h2c client on a raw socket (prior knowledge)."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True,
                                      header_encoding="utf-8"))
        self.conn.initiate_connection()  # sends preface + SETTINGS
        sock.sendall(self.conn.data_to_send())
        sock.settimeout(10)
        self._stream = 1  # client stream ids are odd

    def request(self, method: str, path: str,
                body: bytes | None = None) -> tuple[int, dict, bytes]:
        sid = self._stream
        self._stream += 2
        peer = self.sock.getpeername()
        headers = [
            (":method", method), (":scheme", "http"),
            (":path", path), (":authority", f"{peer[0]}:{peer[1]}"),
        ]
        if body is not None:
            headers.append(("content-type", "application/json"))
        self.conn.send_headers(sid, headers, end_stream=body is None)
        if body is not None:
            self.conn.send_data(sid, body, end_stream=True)
        self.sock.sendall(self.conn.data_to_send())
        return self._read_response()

    def _read_response(self) -> tuple[int, dict, bytes]:
        status = None
        headers: dict[str, str] = {}
        chunks: list[bytes] = []
        while True:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("server closed the connection mid-response")
            events = self.conn.receive_data(data)
            self.sock.sendall(self.conn.data_to_send())
            ended = False
            for ev in events:
                if isinstance(ev, h2.events.DataReceived):
                    self.conn.acknowledge_received_data(len(ev.data), ev.stream_id)
                    chunks.append(ev.data)
                    ended = ev.stream_ended
                elif isinstance(ev, _H2_RES_EVENT):
                    fields = {k: v for k, v in ev.headers}
                    status = int(fields[":status"])
                    headers = {k: v for k, v in ev.headers
                               if not k.startswith(":")}
                    ended = ev.stream_ended
            if ended:
                return status, headers, b"".join(chunks)


def sse_check(ctx: ServerContext, port: int) -> None:
    """Subscribe to a run's SSE stream over h2c; events must stream as DATA."""
    # Throwaway run: _sse only requires the run to exist in the store, and
    # we remove the row afterwards so callers' run counts stay intact.
    run_id = ctx.store.create_run("/tmp", "sse over h2", {})
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    cl = H2Client(s)
    cl._stream += 2
    peer = s.getpeername()
    cl.conn.send_headers(cl._stream, [
        (":method", "GET"), (":scheme", "http"),
        (":path", f"/api/runs/{run_id}/events"),
        (":authority", f"{peer[0]}:{peer[1]}"),
    ], end_stream=True)
    s.sendall(cl.conn.data_to_send())

    deadline = time.time() + 15
    buf = b""
    attempts = 0
    while time.time() < deadline:
        if attempts < 40:
            # Publish until the server-side subscriber is in place (the hub
            # only fans out to live subscribers).
            ctx.hub.publish(run_id, {"name": "sse_h2", "data": {"n": attempts}})
            attempts += 1
        try:
            data = s.recv(65536)
        except socket.timeout:
            continue
        if not data:
            raise AssertionError("server closed the SSE stream")
        events = cl.conn.receive_data(data)
        s.sendall(cl.conn.data_to_send())
        for ev in events:
            if isinstance(ev, h2.events.DataReceived):
                cl.conn.acknowledge_received_data(len(ev.data), ev.stream_id)
                buf += ev.data
                if b"sse_h2" in buf:
                    s.close()
                    ctx.store._conn.execute(
                        "DELETE FROM runs WHERE id = ?", (run_id,))
                    ctx.store._conn.commit()
                    return
        # saw only pings so far; keep waiting for a real event
    raise AssertionError(f"no SSE event delivered over h2c; got: {buf!r}")


def h2_checks(ctx: ServerContext, port: int, db: Path) -> None:
    base = f"http://127.0.0.1:{port}"

    # -- health over h2c -------------------------------------------------------
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    cl = H2Client(s)
    status, headers, body = cl.request("GET", "/api/health")
    assert status == 200, (status, headers, body)
    assert headers.get("content-type") == "application/json", headers
    payload = json.loads(body)
    assert payload["ok"] is True, payload
    assert payload["version"] == __version__, payload
    assert str(db) in payload["db"], payload

    # Same port, same endpoint, over plain HTTP/1.1: identical body.
    with urllib.request.urlopen(f"{base}/api/health", timeout=10) as r:
        h1 = json.loads(r.read().decode())
    assert h1 == payload, (h1, payload)

    # -- 400 over h2c with a request body (client DATA frames) ----------------
    status, headers, body = cl.request("POST", "/api/runs", body=b'{"prompt": ""}')
    assert status == 400, (status, headers, body)
    assert json.loads(body) == {"error": "prompt is required"}, body
    assert headers.get("content-type") == "application/json", headers

    # -- 404 over h2c -----------------------------------------------------------
    status, _, body = cl.request("GET", "/api/nope")
    assert status == 404, (status, body)
    assert json.loads(body) == {"error": "not found"}, body

    # -- SSE events stream as DATA frames over h2c ------------------------------
    sse_check(ctx, port)
    s.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ralph-h2-smoke-"))
    db = tmp / "ralph.db"
    ctx = ServerContext(db)
    Handler.ctx = ctx
    httpd = _ThreadingTCPServer(("127.0.0.1", 0), _DualRequestHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        h2_checks(ctx, port, db)
    finally:
        httpd.shutdown()
    print("\nHTTP2 SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
