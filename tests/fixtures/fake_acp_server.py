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
        self._next_official_turn = 1
        self._next_permission = 1
        self._sessions: dict[str, dict[str, Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._session_cancel_events: dict[str, threading.Event] = {}
        self._permission_waiters: dict[str, threading.Event] = {}
        self._permission_results: dict[str, dict[str, Any]] = {}
        self._last_official_prompt_params: dict[str, Any] | None = None

    def send(self, payload: dict[str, Any]) -> None:
        _write_line(self._lock, payload)

    def _send_result(self, request_id: Any, result: Any) -> None:
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
        if self._scenario == "session_status_idle_then_prompt_completed":
            self.send(
                {
                    "method": "session.status",
                    "params": {
                        "sessionId": session_id,
                        "status": {"type": "idle"},
                    },
                }
            )
            time.sleep(0.02)
            self.send(
                {
                    "method": "prompt/completed",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "status": "completed",
                        "finalOutput": "final canonical output",
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

    def _stream_official_prompt(
        self,
        *,
        request_id: Any,
        session_id: str,
        turn_id: str,
        prompt: str,
    ) -> None:
        cancel_event = self._session_cancel_events[session_id]
        thought_content: Any = {"type": "text", "text": "thinking"}
        reply_content: Any = {"type": "text", "text": "fixture reply"}
        if self._scenario == "official_content_parts":
            thought_content = [
                {"type": "text", "text": "thinking"},
                {"type": "output_text", "text": " more"},
            ]
            reply_content = [
                {"type": "text", "text": "fixture"},
                {"type": "output_text", "text": " reply"},
            ]
        self.send(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": thought_content,
                    },
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
                    cancel_event.clear()
                    self._send_result(request_id, {"stopReason": "cancelled"})
                    return
                if waiter.is_set():
                    break
            result = self._permission_results.pop(permission_id, {})
            outcome = result.get("outcome")
            if isinstance(outcome, dict):
                outcome_type = outcome.get("outcome")
                if outcome_type == "cancelled":
                    cancel_event.clear()
                    self._send_result(request_id, {"stopReason": "cancelled"})
                    return
                if outcome_type == "selected" and outcome.get("optionId") == "deny":
                    self._send_result(
                        request_id,
                        {
                            "stopReason": "refusal",
                            "message": "permission denied",
                        },
                    )
                    return
        if prompt == "crash":
            sys.stderr.write("fixture crash requested\n")
            sys.stderr.flush()
            os._exit(17)
        if prompt == "cancel me":
            while not cancel_event.wait(0.02):
                pass
            cancel_event.clear()
            self._send_result(
                request_id,
                {"stopReason": "cancelled", "userMessageId": turn_id},
            )
            return
        time.sleep(0.05)
        self.send(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": reply_content,
                    },
                },
            }
        )
        if self._scenario == "official_prompt_hang":
            return
        if self._scenario == "official_cancelled_before_return":
            self.send(
                {
                    "method": "prompt/cancelled",
                    "params": {
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "status": "cancelled",
                        "message": "request cancelled",
                    },
                }
            )
            return
        if self._scenario == "official_failed_before_return":
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
        if self._scenario == "official_terminal_before_return":
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
            time.sleep(0.05)
            cancel_event.clear()
            self._send_result(
                request_id,
                {"stopReason": "end_turn", "userMessageId": turn_id},
            )
            return
        if self._scenario == "official_terminal_without_turn_id":
            self.send(
                {
                    "method": "prompt/completed",
                    "params": {
                        "sessionId": session_id,
                        "status": "completed",
                        "finalOutput": "fixture reply",
                    },
                }
            )
            time.sleep(0.05)
            cancel_event.clear()
            self._send_result(
                request_id,
                {"stopReason": "end_turn", "userMessageId": turn_id},
            )
            return
        if self._scenario == "official_session_status_idle_before_return":
            self.send(
                {
                    "method": "session.status",
                    "params": {
                        "sessionId": session_id,
                        "status": {"type": "idle"},
                    },
                }
            )
            time.sleep(0.05)
            cancel_event.clear()
            self._send_result(
                request_id,
                {"stopReason": "end_turn", "userMessageId": turn_id},
            )
            return
        if self._scenario == "official_request_return_after_terminal":
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
            time.sleep(0.2)
            cancel_event.clear()
            self._send_result(
                request_id,
                {"stopReason": "end_turn", "userMessageId": turn_id},
            )
            return
        if self._scenario == "official_server_assigned_turn_id_before_return":
            server_turn_id = f"server-turn-{self._next_official_turn}"
            self._next_official_turn += 1
            self.send(
                {
                    "method": "prompt/completed",
                    "params": {
                        "sessionId": session_id,
                        "turnId": server_turn_id,
                        "status": "completed",
                        "finalOutput": "fixture reply",
                    },
                }
            )
            time.sleep(0.05)
            cancel_event.clear()
            self._send_result(
                request_id,
                {"stopReason": "end_turn", "userMessageId": server_turn_id},
            )
            return
        if self._scenario == "official_terminal_without_request_return":
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
            return
        cancel_event.clear()
        self._send_result(
            request_id,
            {"stopReason": "end_turn", "userMessageId": turn_id},
        )

    def _handle_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        is_official = self._scenario != "initialize_error"
        if method != "initialize" and not self._initialized:
            self._send_error(request_id, -32000, "not initialized")
            return
        if method == "initialize":
            if self._scenario == "initialize_error":
                self._send_error(request_id, -32001, "initialize failed")
                return
            if is_official and "protocolVersion" not in params:
                self._send_error(request_id, -32602, "Invalid params")
                return
            self._initialized = True
            if is_official:
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
                    "lastOfficialPromptParams": self._last_official_prompt_params,
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
            if self._scenario == "official_missing_load_result" and session is None:
                self._send_result(request_id, None)
                return
            if session is None:
                self._send_error(request_id, -32004, "session not found")
                return
            if self._scenario == "official_empty_load_result":
                self._send_result(request_id, {})
                return
            self._send_result(request_id, {"session": session})
            return
        if method == "session/list":
            self._send_result(
                request_id,
                {"sessions": list(self._sessions.values())},
            )
            return
        if method == "session/fork":
            source_id = str(params.get("sessionId") or "")
            if source_id not in self._sessions:
                self._send_error(request_id, -32004, "session not found")
                return
            fork_id = f"session-{self._next_session}"
            self._next_session += 1
            source = dict(self._sessions[source_id])
            fork_title = params.get("title") or f"fork of {source_id}"
            fork_session = {
                "sessionId": fork_id,
                "title": fork_title,
                "cwd": source.get("cwd"),
            }
            self._sessions[fork_id] = fork_session
            self._session_cancel_events[fork_id] = threading.Event()
            self._send_result(request_id, {"session": fork_session})
            return
        if method == "session/set_model":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self._sessions:
                self._send_error(request_id, -32004, "session not found")
                return
            self._sessions[session_id]["modelId"] = params.get("modelId", "")
            self._send_result(request_id, {"modelId": params.get("modelId", "")})
            return
        if method == "session/set_mode":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self._sessions:
                self._send_error(request_id, -32004, "session not found")
                return
            self._sessions[session_id]["mode"] = params.get("mode", "")
            self._send_result(request_id, {"mode": params.get("mode", "")})
            return
        if method == "session/new":
            session_id = f"session-{self._next_session}"
            self._next_session += 1
            session = {
                "sessionId": session_id,
                "cwd": params.get("cwd"),
                "title": params.get("title"),
            }
            self._sessions[session_id] = session
            self._session_cancel_events[session_id] = threading.Event()
            self._send_result(request_id, session)
            return
        if method == "session/prompt":
            session_id = str(params.get("sessionId") or "")
            if session_id not in self._sessions:
                self._send_error(request_id, -32004, "session not found")
                return
            self._last_official_prompt_params = dict(params)
            turn_id = str(params.get("messageId") or "").strip()
            if not turn_id:
                turn_id = f"turn-{self._next_official_turn}"
                self._next_official_turn += 1
            prompt_items = params.get("prompt")
            prompt_text = ""
            if isinstance(prompt_items, list):
                prompt_text = " ".join(
                    str(item.get("text") or "")
                    for item in prompt_items
                    if isinstance(item, dict)
                ).strip()
            worker = threading.Thread(
                target=self._stream_official_prompt,
                kwargs={
                    "request_id": request_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "prompt": prompt_text,
                },
                daemon=True,
            )
            worker.start()
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
            params = message.get("params") or {}
            session_id = str(params.get("sessionId") or "")
            cancel_event = self._session_cancel_events.get(session_id)
            if cancel_event is not None:
                cancel_event.set()
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
