from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
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


def _spawn_child_process() -> subprocess.Popen[bytes]:
    child_code = """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)

while True:
    time.sleep(1)
"""
    return subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap_child_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return


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


@dataclass
class _PendingQuestion:
    request_id: str
    session_id: str
    assistant_message_id: str
    tool_part_id: str
    question: dict[str, Any]
    answered: threading.Event = field(default_factory=threading.Event)
    answers: Optional[list[list[str]]] = None
    rejected: bool = False


class _FakeOpenCodeState:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._pending_questions: dict[str, _PendingQuestion] = {}
        self._subscribers: list[queue.Queue[Optional[dict[str, Any]]]] = []
        self._next_id = 1

    def create_session(self, directory: Optional[str]) -> dict[str, Any]:
        with self._lock:
            session_id = f"session-{self._next_id}"
            self._next_id += 1
            payload = {
                "id": session_id,
                "title": "fixture session",
                "directory": directory,
            }
            self._sessions[session_id] = payload
            return dict(payload)

    def add_subscriber(self) -> queue.Queue[Optional[dict[str, Any]]]:
        subscriber: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        subscriber.put({"type": "server.connected", "properties": {}})
        return subscriber

    def remove_subscriber(
        self, subscriber: queue.Queue[Optional[dict[str, Any]]]
    ) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(dict(payload))

    def prompt_async(self, session_id: str, prompt_text: str) -> dict[str, Any]:
        user_message_id = f"user-{uuid.uuid4().hex[:8]}"
        assistant_message_id = f"assistant-{uuid.uuid4().hex[:8]}"
        self.emit(
            {
                "type": "message.updated",
                "properties": {
                    "sessionID": session_id,
                    "info": {
                        "id": user_message_id,
                        "role": "user",
                        "sessionID": session_id,
                    },
                },
            }
        )
        self.emit(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": session_id,
                    "part": {
                        "id": f"part-{uuid.uuid4().hex[:8]}",
                        "type": "text",
                        "text": prompt_text,
                        "messageID": user_message_id,
                        "sessionID": session_id,
                    },
                },
            }
        )
        self.emit(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": session_id,
                    "status": {"type": "busy"},
                },
            }
        )
        self.emit(
            {
                "type": "message.updated",
                "properties": {
                    "sessionID": session_id,
                    "info": {
                        "id": assistant_message_id,
                        "role": "assistant",
                        "sessionID": session_id,
                    },
                },
            }
        )
        if self.scenario != "question":
            self.emit(
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": session_id,
                        "part": {
                            "id": f"part-{uuid.uuid4().hex[:8]}",
                            "type": "text",
                            "text": "fixture reply",
                            "messageID": assistant_message_id,
                            "sessionID": session_id,
                        },
                    },
                }
            )
            self.emit(
                {
                    "type": "session.status",
                    "properties": {
                        "sessionID": session_id,
                        "status": {"type": "idle"},
                    },
                }
            )
            self.emit({"type": "session.idle", "properties": {"sessionID": session_id}})
            return {
                "info": {
                    "id": assistant_message_id,
                    "role": "assistant",
                    "sessionID": session_id,
                },
                "parts": [{"type": "text", "text": "fixture reply"}],
            }

        request_id = f"question-{uuid.uuid4().hex[:8]}"
        tool_part_id = f"tool-{uuid.uuid4().hex[:8]}"
        question = {
            "id": "framework",
            "header": "Testing Framework",
            "question": "Which testing framework would you like to use?",
            "options": [
                {
                    "label": "pytest",
                    "description": "Use pytest as the testing framework",
                },
                {
                    "label": "unittest",
                    "description": "Use unittest (standard library) as the testing framework",
                },
            ],
        }
        pending = _PendingQuestion(
            request_id=request_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            tool_part_id=tool_part_id,
            question=question,
        )
        with self._lock:
            self._pending_questions[request_id] = pending
        self.emit(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": session_id,
                    "part": {
                        "id": tool_part_id,
                        "messageID": assistant_message_id,
                        "sessionID": session_id,
                        "type": "tool",
                        "tool": "question",
                        "callID": "call-question-1",
                        "state": {"status": "pending", "input": {}, "raw": ""},
                    },
                },
            }
        )
        self.emit(
            {
                "type": "question.asked",
                "properties": {
                    "id": request_id,
                    "sessionID": session_id,
                    "questions": [question],
                    "tool": {
                        "messageID": assistant_message_id,
                        "callID": "call-question-1",
                    },
                },
            }
        )
        self.emit(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": session_id,
                    "part": {
                        "id": tool_part_id,
                        "messageID": assistant_message_id,
                        "sessionID": session_id,
                        "type": "tool",
                        "tool": "question",
                        "callID": "call-question-1",
                        "state": {
                            "status": "running",
                            "input": {"questions": [question]},
                            "raw": "",
                            "time": {"start": int(time.time() * 1000)},
                        },
                    },
                },
            }
        )
        pending.answered.wait()
        with self._lock:
            self._pending_questions.pop(request_id, None)
        answer_label = "unanswered"
        if pending.answers and pending.answers[0]:
            first = pending.answers[0][0]
            if isinstance(first, str) and first:
                answer_label = first
        elif pending.rejected:
            answer_label = "rejected"
        self.emit(
            {
                "type": "question.replied",
                "properties": {
                    "sessionID": session_id,
                    "requestID": request_id,
                    "answers": pending.answers or [],
                },
            }
        )
        self.emit(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": session_id,
                    "part": {
                        "id": tool_part_id,
                        "messageID": assistant_message_id,
                        "sessionID": session_id,
                        "type": "tool",
                        "tool": "question",
                        "callID": "call-question-1",
                        "state": {
                            "status": "completed",
                            "input": {"questions": [question]},
                            "output": (
                                "User has answered your questions: "
                                f"\"{question['question']}\"=\"{answer_label}\"."
                            ),
                            "metadata": {
                                "answers": pending.answers or [],
                                "truncated": False,
                            },
                            "title": "Asked 1 question",
                            "time": {
                                "start": int(time.time() * 1000) - 10,
                                "end": int(time.time() * 1000),
                            },
                        },
                    },
                },
            }
        )
        final_text = f"Got it - {answer_label} it is. What would you like me to do?"
        self.emit(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": session_id,
                    "part": {
                        "id": f"text-{uuid.uuid4().hex[:8]}",
                        "messageID": assistant_message_id,
                        "sessionID": session_id,
                        "type": "text",
                        "text": final_text,
                    },
                },
            }
        )
        self.emit(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": session_id,
                    "status": {"type": "idle"},
                },
            }
        )
        self.emit({"type": "session.idle", "properties": {"sessionID": session_id}})
        return {
            "info": {
                "id": assistant_message_id,
                "role": "assistant",
                "sessionID": session_id,
            },
            "parts": [{"type": "text", "text": final_text}],
        }

    def reply_question(
        self, request_id: str, answers: list[list[str]]
    ) -> dict[str, Any]:
        with self._lock:
            pending = self._pending_questions.get(request_id)
        if pending is None:
            return {"accepted": False, "status_code": 404}
        pending.answers = answers
        pending.answered.set()
        return {"accepted": True, "status_code": 200}

    def reject_question(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            pending = self._pending_questions.get(request_id)
        if pending is None:
            return {"accepted": False, "status_code": 404}
        pending.answers = []
        pending.rejected = True
        pending.answered.set()
        return {"accepted": True, "status_code": 200}


_REQUEST_STATE: Optional[_FakeOpenCodeState] = None


class _FakeRequestHandler(http.server.BaseHTTPRequestHandler):
    DOC_PATHS: dict[str, Any] = {
        "paths": {
            "/global/health": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/global/event": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/session": {
                "post": {"responses": {"200": {"description": "created"}}},
            },
            "/session/{session_id}/prompt_async": {
                "post": {"responses": {"200": {"description": "ok"}}},
            },
            "/question/{question_id}/reply": {
                "post": {"responses": {"200": {"description": "ok"}}},
            },
            "/question/{question_id}/reject": {
                "post": {"responses": {"200": {"description": "ok"}}},
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

    def _read_json_body(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            content_length = 0
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        if not _is_authorized(self):
            _write_unauthorized(self)
            return
        if self.path == "/doc":
            self._write_json(HTTPStatus.OK, self.DOC_PATHS)
            return
        if self.path in self.HEALTH_PATHS:
            self._write_json(HTTPStatus.OK, self.HEALTH_PATHS[self.path])
            return
        if self.path.startswith("/event") or self.path.startswith("/global/event"):
            self._handle_sse_stream()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        if not _is_authorized(self):
            _write_unauthorized(self)
            return
        state = _REQUEST_STATE
        if state is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "State unavailable")
            return
        payload = self._read_json_body()
        if self.path == "/session":
            directory = payload.get("directory")
            self._write_json(
                HTTPStatus.OK,
                state.create_session(directory if isinstance(directory, str) else None),
            )
            return
        prompt_match = re.fullmatch(r"/session/([^/]+)/prompt_async", self.path)
        if prompt_match:
            session_id = prompt_match.group(1)
            prompt_text = ""
            parts_raw = payload.get("parts")
            if isinstance(parts_raw, list):
                prompt_text = "".join(
                    part.get("text", "")
                    for part in parts_raw
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            result = state.prompt_async(session_id, prompt_text)
            self._write_json(HTTPStatus.OK, result)
            return
        reply_match = re.fullmatch(r"/question/([^/]+)/reply", self.path)
        if reply_match:
            request_id = reply_match.group(1)
            answers_raw = payload.get("answers")
            answers = (
                [
                    [str(answer) for answer in row if isinstance(answer, str)]
                    for row in answers_raw
                    if isinstance(row, list)
                ]
                if isinstance(answers_raw, list)
                else []
            )
            self._write_json(HTTPStatus.OK, state.reply_question(request_id, answers))
            return
        reject_match = re.fullmatch(r"/question/([^/]+)/reject", self.path)
        if reject_match:
            request_id = reject_match.group(1)
            self._write_json(HTTPStatus.OK, state.reject_question(request_id))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_sse_stream(self) -> None:
        state = _REQUEST_STATE
        if state is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "State unavailable")
            return
        subscriber = state.add_subscriber()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                event = subscriber.get(timeout=30)
                if event is None:
                    break
                event_type = event.get("type")
                payload = json.dumps(event)
                self.wfile.write(f"event: {event_type}\n".encode("utf-8"))
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, queue.Empty):
            return
        finally:
            state.remove_subscriber(subscriber)

    def log_message(self, *_args, **_kwargs) -> None:
        return


def _serve(scenario: str) -> None:
    global _REQUEST_STATE
    _REQUEST_STATE = _FakeOpenCodeState(scenario)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeRequestHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    child_proc = _spawn_child_process()
    _write_text_file(os.environ.get("OPENCODE_CHILD_PID_FILE"), f"{child_proc.pid}\n")
    _append_marker(
        _marker_path(),
        f"{base_url} {os.getpid()} child={child_proc.pid} scenario={scenario}",
    )
    print(f"listening on {base_url}", flush=True)

    try:
        server.serve_forever()
    finally:
        _reap_child_process(child_proc)
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="smoke")
    args = parser.parse_args()
    _serve(args.scenario)


if __name__ == "__main__":
    main()
