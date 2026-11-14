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

from slop_churner import __version__  # noqa: E402
from slop_churner.web import (  # noqa: E402
    Handler,
    ServerContext,
    _DualRequestHandler,
    _H2,
    _ThreadingTCPServer,
)

assert _H2, "slop-churner.web must see the h2 package when the test can"


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

    def _read_response(self, sid: int | None = None) -> tuple[int, dict, bytes]:
        """Read one response; frames of other (interleaved) streams are fed
        to the h2 state machine but ignored."""
        if sid is None:
            sid = self._stream - 2
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
                if getattr(ev, "stream_id", None) != sid:
                    continue
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
        for ev in events:
            if isinstance(ev, h2.events.DataReceived):
                cl.conn.acknowledge_received_data(len(ev.data), ev.stream_id)
                buf += ev.data
        # Flush AFTER acknowledging: the acks generate the WINDOW_UPDATE
        # frames the server needs before it can stream more data.
        s.sendall(cl.conn.data_to_send())
        # Require several distinct events: a single event would pass even if
        # the (mis)server set END_STREAM on the first frame and the stream
        # died — which is exactly the regression this guards.
        if buf.count(b"sse_h2") >= 3:
            s.close()
            ctx.store._conn.execute(
                "DELETE FROM runs WHERE id = ?", (run_id,))
            ctx.store._conn.commit()
            return
        # not enough events yet; keep waiting
    raise AssertionError(f"fewer than 3 SSE events over h2c; got: {buf!r}")


def _open_sse_stream(cl: H2Client, run_id: int) -> int:
    """Open a run's SSE stream on a fresh client stream; return the id."""
    sid = cl._stream
    cl._stream += 2
    peer = cl.sock.getpeername()
    cl.conn.send_headers(sid, [
        (":method", "GET"), (":scheme", "http"),
        (":path", f"/api/runs/{run_id}/events"),
        (":authority", f"{peer[0]}:{peer[1]}"),
    ], end_stream=True)
    cl.sock.sendall(cl.conn.data_to_send())
    return sid


def _pump_stream(cl: H2Client, sid: int, until: bytes,
                 seconds: float) -> bytes:
    """Read from the socket, collecting stream ``sid``'s DATA frames.

    Frames of other (interleaved) streams are still fed to the h2 state
    machine and acknowledged, but discarded. Returns sid's payload once it
    contains ``until``; raises on timeout.
    """
    buf = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data = cl.sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            raise AssertionError("server closed the connection mid-stream")
        events = cl.conn.receive_data(data)
        for ev in events:
            if isinstance(ev, h2.events.DataReceived):
                cl.conn.acknowledge_received_data(len(ev.data), ev.stream_id)
                if ev.stream_id == sid:
                    buf += ev.data
                    if until in buf:
                        return buf
        # Acknowledge first, then flush: the acks are what generate the
        # WINDOW_UPDATE frames the server needs to keep streaming.
        cl.sock.sendall(cl.conn.data_to_send())
    raise AssertionError(f"stream {sid} did not deliver {until!r} in {seconds}s; "
                         f"got {len(buf)} bytes: {buf[:120]!r}")


def multiplex_check(ctx: ServerContext, port: int) -> None:
    """A live SSE stream must not starve the other streams of the same h2
    connection.

    Regression test: _H2Session used to serve streams inline in its read
    loop, so the moment EventSource opened, the loop blocked forever in
    the SSE pump and every other request on the connection (browser UI
    polling, page reloads, approve/reject) hung — 'UI freezes during the
    planning phase'.
    """
    run_id = ctx.store.create_run("/tmp", "h2 multiplex", {})
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    cl = H2Client(s)
    try:
        sse_sid = _open_sse_stream(cl, run_id)

        # Regular request on a second stream while the SSE stream is live:
        # it must be answered, not queue behind the SSE pump.
        status, headers, body = cl.request("GET", "/api/health")
        assert status == 200, (status, headers, body)
        assert json.loads(body)["ok"] is True, body

        # ...and the SSE stream must still deliver events (publish in a
        # background loop: the server subscribes only once its stream thread
        # starts, so a one-shot publish can race ahead of it).
        stop = threading.Event()
        def publish_loop():
            while not stop.wait(0.1):
                ctx.hub.publish(run_id, {"name": "mx", "data": {"n": 1}})
        pt = threading.Thread(target=publish_loop, daemon=True)
        pt.start()
        try:
            buf = _pump_stream(cl, sse_sid, b'"mx"', seconds=10)
        finally:
            stop.set()
        assert b"mx" in buf, buf
    finally:
        s.close()
        with ctx.store._lock:
            ctx.store._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            ctx.store._conn.commit()


def big_sse_check(ctx: ServerContext, port: int) -> None:
    """A single SSE event far larger than the 64 KiB initial flow-control
    window must stream through intact.

    Regression test: send_data used to ignore the peer's window and h2
    raised FlowControlError past ~64 KiB, which the SSE handler swallowed
    and silently killed the live stream mid-planning.
    """
    run_id = ctx.store.create_run("/tmp", "h2 big sse", {})
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    cl = H2Client(s)
    try:
        sse_sid = _open_sse_stream(cl, run_id)
        event = {"name": "big", "data": {"blob": "x" * 300_000}}
        expected = ("data: " + json.dumps(event) + "\n\n").encode()
        stop = threading.Event()
        def publish_loop():
            # Publish repeatedly: the server subscribes only once its stream
            # thread starts, and the client consumes each copy as it arrives.
            while not stop.wait(0.1):
                ctx.hub.publish(run_id, event)
        pt = threading.Thread(target=publish_loop, daemon=True)
        pt.start()
        try:
            buf = _pump_stream(cl, sse_sid, expected, seconds=20)
            assert expected in buf
        finally:
            stop.set()
    finally:
        s.close()
        with ctx.store._lock:
            ctx.store._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            ctx.store._conn.commit()


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

    # -- a live SSE stream must not starve the rest of the connection ----------
    multiplex_check(ctx, port)

    # -- >64 KiB SSE events cross the flow-control window intact ----------------
    big_sse_check(ctx, port)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="slop-churner-h2-smoke-"))
    db = tmp / "slop-churner.db"
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
