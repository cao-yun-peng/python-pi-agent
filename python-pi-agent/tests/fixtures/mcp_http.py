"""Minimal local wire peer for transport tests; deliberately independent of SDK server."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def local_peer(mode="json", token="fixture-only", started=None, cancelled=None):
    calls, methods, auth = [], [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, status, body=b"", headers=None):
            self.send_response(status)
            self.send_header("content-length", str(len(body)))
            if "content-type" not in (headers or {}):
                self.send_header("content-type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            methods.append("GET")
            self.respond(405)

        def do_DELETE(self):
            methods.append("DELETE")
            self.respond(200)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length", "0")))
            frame = json.loads(raw)
            method = frame["method"]
            methods.append(method)
            auth.append(self.headers.get("authorization"))
            if self.headers.get("authorization") != token or mode == "unauthorized":
                self.respond(401, b"private authentication response")
                return
            if "id" not in frame:
                self.respond(202)
                return
            headers = {}
            if method == "server/discover":
                result = {
                    "cacheScope": "public",
                    "ttlMs": 0,
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                    "resultType": "complete",
                }
            elif method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
                headers["mcp-session-id"] = "fixture-session"
            elif method == "tools/list":
                result = {
                    "cacheScope": "public",
                    "ttlMs": 0,
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Local echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        }
                    ],
                    "resultType": "complete",
                }
            elif method == "tools/call":
                calls.append(frame["params"]["name"])
                if mode == "wait":
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    self.wfile.flush()
                    self.connection.settimeout(3)
                    if started is not None:
                        started.set()
                    try:
                        if self.connection.recv(1) == b"" and cancelled is not None:
                            cancelled.set()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                result = {
                    "content": [
                        {"type": "text", "text": frame["params"]["arguments"]["value"]}
                    ],
                    "resultType": "complete",
                }
                if mode in {"large-json", "large-sse"}:
                    result["content"][0]["text"] = "x" * 16384
                if mode == "redirect":
                    self.respond(307, headers={"location": "/elsewhere"})
                    return
                if mode == "disconnect":
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
            else:
                self.respond(400)
                return
            body = json.dumps(
                {"jsonrpc": "2.0", "id": frame["id"], "result": result}
            ).encode()
            if method == "tools/call" and mode in {"sse", "large-sse", "broken-sse"}:
                body = b"event: message\ndata: " + body + b"\n\n"
                if mode == "broken-sse":
                    body = b'id: previous\nretry: 0\nevent: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":"ignored","progress":1}}\n\n'
                headers["content-type"] = "text/event-stream"
            self.respond(200, body, headers)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", calls, methods, auth
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
