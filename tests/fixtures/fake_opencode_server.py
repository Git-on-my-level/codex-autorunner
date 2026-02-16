from __future__ import annotations

import base64
import http.server
import json
import os
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional


def _write_text_file(path: Optional[str], content: str) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)


def _append_marker(path: Optional[str], marker: str) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file:
        file.write(marker + "\n")


def _spawn_child_process() -> int:
    child_code = """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)

while True:
    time.sleep(1)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def _auth_config() -> tuple[Optional[str], Optional[str]]:
    return (
        os.environ.get("OPENCODE_SERVER_USERNAME"),
        os.environ.get("OPENCODE_SERVER_PASSWORD"),
    )


def _parse_basic_auth_header(header: str) -> Optional[tuple[str, str]]:
    if not header.startswith("Basic "):
        return None
    token = header[6:]
    try:
        decoded = base64.b64decode(token).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def _is_authorized(handler: http.server.BaseHTTPRequestHandler) -> bool:
    username_required, password_required = _auth_config()
    if not password_required:
        return True
    if not username_required:
        username_required = "opencode"

    header = handler.headers.get("Authorization", "")
    credentials = _parse_basic_auth_header(header)
    if credentials is None:
        return False
    return credentials == (username_required, password_required)


def _write_unauthorized(handler: http.server.BaseHTTPRequestHandler) -> None:
    response = json.dumps({"error": "unauthorized"})
    encoded = response.encode("utf-8")
    handler.send_response(HTTPStatus.UNAUTHORIZED)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("WWW-Authenticate", 'Basic realm="OpenCode"')
    handler.end_headers()
    handler.wfile.write(encoded)


def _marker_path() -> Optional[str]:
    return os.environ.get("OPENCODE_START_MARKER_PATH") or os.environ.get(
        "OPENCODE_START_MARKER_FILE"
    )


class _FakeRequestHandler(http.server.BaseHTTPRequestHandler):
    DOC_PATHS: dict[str, Any] = {
        "paths": {
            "/global/health": {
                "get": {"responses": {"200": {"description": "ok"}}},
            },
            "/global/event": {
                "get": {"responses": {"200": {"description": "ok"}}},
            },
        }
    }
    HEALTH_PATHS = {
        "/global/health": {"status": "ok"},
        "/health": {"status": "ok"},
    }

    def _write_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (HTTP handler API).
        if not _is_authorized(self):
            _write_unauthorized(self)
            return

        if self.path == "/doc":
            self._write_json(HTTPStatus.OK, self.DOC_PATHS)
            return
        if self.path in self.HEALTH_PATHS:
            self._write_json(HTTPStatus.OK, self.HEALTH_PATHS[self.path])
            return

        self.send_error(
            HTTPStatus.NOT_FOUND,
            "Not Found",
        )

    def log_message(self, *_args, **_kwargs) -> None:
        return


def _serve() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeRequestHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    print(f"listening on {base_url}", flush=True)

    child_pid = _spawn_child_process()
    _write_text_file(os.environ.get("OPENCODE_CHILD_PID_FILE"), f"{child_pid}\n")
    _append_marker(
        _marker_path(),
        f"{base_url} {os.getpid()} child={child_pid}",
    )

    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    _serve()


if __name__ == "__main__":
    main()
