"""
trusted_api.py — trusted-caller API server (port 6666).

Endpoints:
  GET /pending  — list all pending confirmation tokens
  GET /approve  — approve a pending send (actually sends the message)
  GET /deny     — cancel a pending send

This server is intentionally separate from the public API (port 6000) because
these endpoints can trigger outbound messages without further human interaction.
Only trusted callers (nanobot, local scripts) should be able to reach this port.
"""

import json
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .confirm import pending_count, list_pending, approve as confirm_approve, deny as confirm_deny

TRUSTED_PORT = 6666


class TrustedHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat()}] trusted: {' '.join(str(a) for a in args)}")

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        def first(key: str) -> str | None:
            return qs[key][0] if key in qs else None

        if parsed.path == "/pending":
            self.send_json({"count": pending_count(), "pending": list_pending()})

        elif parsed.path == "/approve":
            token = first("token")
            if not token:
                self.send_json({"error": "missing 'token' parameter"}, 400)
                return
            result = confirm_approve(token)
            self.send_json(result, 200 if result.get("ok") else 404)

        elif parsed.path == "/deny":
            token = first("token")
            if not token:
                self.send_json({"error": "missing 'token' parameter"}, 400)
                return
            result = confirm_deny(token)
            self.send_json(result, 200 if result.get("ok") else 404)

        else:
            self.send_json({"error": "Not found"}, 404)


def run_trusted_server() -> None:
    print(f"Trusted API server listening on http://localhost:{TRUSTED_PORT} (trusted callers only)")
    HTTPServer(("127.0.0.1", TRUSTED_PORT), TrustedHandler).serve_forever()
