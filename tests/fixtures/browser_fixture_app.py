#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class BrowserFixtureHandler(http.server.BaseHTTPRequestHandler):
    server_version = "BrowserFixture/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_text(200, "ok")
            return

        if parsed.path == "/":
            self._send_html(
                200,
                """
                <html>
                  <head><title>Fixture Landing</title></head>
                  <body>
                    <h1 data-testid=\"landing-title\">Fixture Landing</h1>
                    <p>Deterministic local browser fixture.</p>
                    <a href=\"/form\">Open form</a>
                  </body>
                </html>
                """,
            )
            return

        if parsed.path == "/form":
            self._send_html(
                200,
                """
                <html>
                  <head><title>Fixture Form</title></head>
                  <body>
                    <h1 aria-label=\"Form Heading\">Sign In</h1>
                    <form method=\"post\" action=\"/form\">
                      <label>Email <input name=\"email\" data-testid=\"email-input\" /></label>
                      <label>Password <input type=\"password\" name=\"password\" /></label>
                      <button type=\"submit\">Submit</button>
                    </form>
                  </body>
                </html>
                """,
            )
            return

        if parsed.path == "/dashboard":
            query = parse_qs(parsed.query)
            email = (query.get("email") or ["unknown"])[0]
            self._send_html(
                200,
                f"""
                <html>
                  <head><title>Fixture Dashboard</title></head>
                  <body>
                    <h1 data-testid=\"dashboard-heading\">Dashboard</h1>
                    <p>Signed in as <strong>{email}</strong></p>
                  </body>
                </html>
                """,
            )
            return

        self._send_text(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/form":
            self._send_text(404, "not found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            _ = self.rfile.read(length)

        # Keep redirects deterministic and header-safe in this fixture.
        location = "/dashboard?email=demo%40example.com"

        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args, **_kwargs) -> None:
        return

    def _send_text(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, status: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic browser fixture app")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pid-file", required=True)
    args = parser.parse_args()

    Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), BrowserFixtureHandler
    )
    print(f"READY http://127.0.0.1:{args.port}/health", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
