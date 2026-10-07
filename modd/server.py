"""Tiny JSON HTTP API (standard library only).

Phase 3's ESP32 touchscreen polls ``/api/dashboard`` and taps
``/api/goals/<id>/done``; anything on the LAN can ``POST /api/ask``.
Set MODD_API_TOKEN to require ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .assistant import Assistant
from .db import Store
from .llm import LLMError


def make_handler(store: Store, assistant: Assistant | None, token: str = ""):
    lock = threading.Lock()  # one SQLite connection / one conversation at a time

    class Handler(BaseHTTPRequestHandler):
        server_version = "Modd/0.1"

        def log_message(self, fmt, *args):  # quieter default logging
            pass

        def _send(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not token:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {token}":
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if not self._authorized():
                return
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            with lock:
                if url.path == "/api/health":
                    return self._send(200, {"ok": True})
                if url.path == "/api/dashboard":
                    return self._send(200, store.dashboard())
                if url.path == "/api/revenue":
                    return self._send(200, store.revenue_for(qs.get("period", "month")))
                if url.path == "/api/goals":
                    return self._send(200, store.goals(qs.get("day")))
                if url.path == "/api/sessions":
                    return self._send(200, store.sessions(qs.get("start"), qs.get("end"), qs.get("status")))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            with lock:
                if m := re.fullmatch(r"/api/goals/(\d+)/done", path):
                    return self._send(200, {"updated": store.complete_goal(int(m.group(1)))})
                if path == "/api/goals" and body.get("text"):
                    return self._send(201, {"goal_id": store.add_goal(body["text"], body.get("area", "general"))})
                if path == "/api/ask" and body.get("text"):
                    if assistant is None:
                        return self._send(503, {"error": "assistant not configured"})
                    try:
                        return self._send(200, {"reply": assistant.ask(body["text"])})
                    except LLMError as e:
                        return self._send(502, {"error": str(e)})
            self._send(404, {"error": "not found"})

    return Handler


def serve(store: Store, assistant: Assistant | None, host: str, port: int, token: str = "") -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(store, assistant, token))
    print(f"Modd API on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
