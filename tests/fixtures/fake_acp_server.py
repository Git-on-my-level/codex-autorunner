from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any


def _write_line(lock: threading.Lock, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, separators=(",", ":"))
    with lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class FakeACPServer:
    def __init__(self, scenario: str) -> None:
        self._scenario = scenario
        self._lock = threading.Lock()
        self._initialized = False
        self._initialized_notification = False
        self._running = True
        self._next_session = 1
        self._next_turn = 1
        self._sessions: dict[str, dict[str, Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def send(self, payload: dict[str, Any]) -> None:
        _write_line(self._lock, payload)

    def _send_result(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"id": request_id, "result": result})

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        self.send({"id": request_id, "error": {"code": code, "message": message}})

    def _stream_prompt(self, *, session_id: str, turn_id: str, prompt: str) -> None:
        self.send(
            {
                "method": "prompt/started",
                "params": {"sessionId": session_id, "turnId": turn_id},
            }
        )
        self.send(
            {
                "method": "prompt/progress",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "delta": "fixture ",
                },
            }
        )
        cancel_event = self._cancel_events[turn_id]
        if prompt == "needs permission":
            self.send(
                {
                    "method": "permission/requested",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "requestId": "perm-1",
                        "description": "Need approval",
                        "context": {"tool": "shell", "command": "ls"},
                    },
                }
            )
        if prompt == "crash":
            self.send(
                {
                    "method": "prompt/progress",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "message": "crashing",
                    },
                }
            )
            sys.stderr.write("fixture crash requested\n")
            sys.stderr.flush()
            os._exit(17)
        if prompt == "cancel me":
            while not cancel_event.is_set():
                time.sleep(0.02)
            self.send(
                {
                    "method": "prompt/cancelled",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "status": "cancelled",
                    },
                }
            )
            return
        time.sleep(0.05)
        self.send(
            {
                "method": "prompt/progress",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "delta": "reply",
                },
            }
        )
        self.send(
            {
                "method": "prompt/completed",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "status": "completed",
                    "finalOutput": "fixture reply",
                },
            }
        )

    def _handle_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if method != "initialize" and not self._initialized:
            self._send_error(request_id, -32000, "not initialized")
            return
        if method == "initialize":
            if self._scenario == "initialize_error":
                self._send_error(request_id, -32001, "initialize failed")
                return
            self._initialized = True
            self._send_result(
                request_id,
                {
                    "protocolVersion": "1.0",
                    "serverInfo": {"name": "fake-acp", "version": "0.1.0"},
                    "capabilities": {
                        "sessions": True,
                        "streaming": True,
                        "interrupt": True,
                    },
                },
            )
            return
        if method == "fixture/status":
            self._send_result(
                request_id,
                {
                    "initialized": self._initialized,
                    "initializedNotification": self._initialized_notification,
                },
            )
            return
        if method == "session/create":
            session_id = f"session-{self._next_session}"
            self._next_session += 1
            session = {
                "sessionId": session_id,
                "title": params.get("title"),
                "cwd": params.get("cwd"),
            }
            self._sessions[session_id] = session
            self._send_result(request_id, {"session": session})
            return
        if method == "session/load":
            session_id = str(params.get("sessionId") or "")
            session = self._sessions.get(session_id)
            if session is None:
                self._send_error(request_id, -32004, "session not found")
                return
            self._send_result(request_id, {"session": session})
            return
        if method == "session/list":
            self._send_result(
                request_id,
                {"sessions": list(self._sessions.values())},
            )
            return
        if method == "prompt/start":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self._sessions:
                self._send_error(request_id, -32004, "session not found")
                return
            turn_id = f"turn-{self._next_turn}"
            self._next_turn += 1
            self._cancel_events[turn_id] = threading.Event()
            self._send_result(
                request_id,
                {"sessionId": session_id, "turnId": turn_id, "status": "started"},
            )
            worker = threading.Thread(
                target=self._stream_prompt,
                kwargs={
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "prompt": str(params.get("prompt") or ""),
                },
                daemon=True,
            )
            worker.start()
            return
        if method == "prompt/cancel":
            turn_id = str(params.get("turnId") or "")
            cancel_event = self._cancel_events.get(turn_id)
            if cancel_event is None:
                self._send_error(request_id, -32004, "turn not found")
                return
            cancel_event.set()
            self._send_result(request_id, {"status": "cancelling"})
            return
        if method == "custom/echo":
            self._send_result(request_id, {"echo": params})
            return
        if method == "shutdown":
            self._send_result(request_id, {"status": "ok"})
            return
        self._send_error(request_id, -32601, f"Method not found: {method}")

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "initialized":
            self._initialized_notification = True
            return
        if method == "exit":
            self._running = False

    def serve(self) -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            message = json.loads(line)
            if "id" in message:
                self._handle_request(message)
            else:
                self._handle_notification(message)
            if not self._running:
                break


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="basic")
    args = parser.parse_args()
    FakeACPServer(args.scenario).serve()


if __name__ == "__main__":
    main()
