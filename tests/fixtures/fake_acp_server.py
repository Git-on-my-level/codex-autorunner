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


def _write_raw_stdout(lock: threading.Lock, line: str) -> None:
    with lock:
        sys.stdout.write(line)
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
        self._next_permission = 1
        self._sessions: dict[str, dict[str, Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._permission_waiters: dict[str, threading.Event] = {}
        self._permission_results: dict[str, dict[str, Any]] = {}

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
        if prompt == "stdout noise":
            _write_raw_stdout(self._lock, "\n")
            _write_raw_stdout(
                self._lock,
                "  ┊ 💻 $ curl -fsS http://127.0.0.1:4517/car/hub... 0.3s\n",
            )
            _write_raw_stdout(
                self._lock,
                "\x1b[33m  [tool] (｡•́︿•̀｡) deliberating...\x1b[0m\n",
            )
        if prompt == "stdout invalid":
            _write_raw_stdout(self._lock, "ACP dependencies not installed.\n")
            return
        if prompt == "stdout invalid bracketed":
            _write_raw_stdout(
                self._lock,
                '[tool] {"id":"1","method":"prompt/completed"}\n',
            )
            return
        cancel_event = self._cancel_events[turn_id]
        if prompt == "needs permission":
            permission_id = f"perm-{self._next_permission}"
            self._next_permission += 1
            waiter = threading.Event()
            self._permission_waiters[permission_id] = waiter
            self.send(
                {
                    "id": permission_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "requestId": permission_id,
                        "description": "Need approval",
                        "toolCall": {
                            "kind": "shell",
                            "rawInput": {"command": ["ls"]},
                        },
                        "options": [
                            {"optionId": "allow", "label": "Allow once"},
                            {"optionId": "deny", "label": "Deny"},
                        ],
                        "context": {"tool": "shell", "command": ["ls"]},
                    },
                }
            )
            while True:
                if cancel_event.wait(0.02):
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
                if waiter.is_set():
                    break
            result = self._permission_results.pop(permission_id, {})
            outcome = result.get("outcome")
            if isinstance(outcome, dict):
                outcome_type = outcome.get("outcome")
                if outcome_type == "cancelled":
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
                if outcome_type == "selected" and outcome.get("optionId") == "deny":
                    self.send(
                        {
                            "method": "prompt/failed",
                            "params": {
                                "sessionId": session_id,
                                "turnId": turn_id,
                                "status": "failed",
                                "message": "permission denied",
                            },
                        }
                    )
                    return
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
        if self._scenario == "session_status_idle_completion_gap":
            self.send(
                {
                    "method": "session.status",
                    "params": {
                        "sessionId": session_id,
                        "status": {"type": "idle"},
                    },
                }
            )
            return
        completed_params = {
            "sessionId": session_id,
            "status": "completed",
            "finalOutput": "fixture reply",
        }
        if self._scenario != "terminal_missing_turn_id":
            completed_params["turnId"] = turn_id
        self.send(
            {
                "method": "prompt/completed",
                "params": completed_params,
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
            if self._scenario == "official" and "protocolVersion" not in params:
                self._send_error(request_id, -32602, "Invalid params")
                return
            self._initialized = True
            if self._scenario == "official":
                self._send_result(
                    request_id,
                    {
                        "protocolVersion": 1,
                        "agentInfo": {"name": "fake-hermes", "version": "0.4.0"},
                        "agentCapabilities": {
                            "sessionCapabilities": {"fork": {}, "list": {}}
                        },
                    },
                )
                return
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
            if self._scenario == "official":
                self._send_error(request_id, -32601, "Method not found: session/list")
                return
            self._send_result(
                request_id,
                {"sessions": list(self._sessions.values())},
            )
            return
        if method == "session/new":
            session_id = f"session-{self._next_session}"
            self._next_session += 1
            session = {
                "sessionId": session_id,
                "cwd": params.get("cwd"),
            }
            self._sessions[session_id] = session
            self._send_result(request_id, {"sessionId": session_id})
            return
        if method == "session/prompt":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self._sessions:
                self._send_error(request_id, -32004, "session not found")
                return
            self.send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "thinking"},
                        },
                    },
                }
            )
            self.send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "OK"},
                        },
                    },
                }
            )
            self._send_result(request_id, {"stopReason": "end_turn"})
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

    def _handle_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            return
        permission_id = str(request_id)
        waiter = self._permission_waiters.get(permission_id)
        if waiter is None:
            return
        result = message.get("result")
        self._permission_results[permission_id] = (
            result if isinstance(result, dict) else {}
        )
        waiter.set()

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "initialized":
            self._initialized_notification = True
            return
        if method == "session/cancel":
            return
        if method == "exit":
            self._running = False

    def serve(self) -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            message = json.loads(line)
            if "id" in message and "method" not in message:
                self._handle_response(message)
            elif "id" in message:
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
