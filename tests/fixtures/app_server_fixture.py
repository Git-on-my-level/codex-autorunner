import argparse
import json
import sys
import threading
import time
import uuid
from typing import Optional


def _write_line(lock: threading.Lock, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":"))
    with lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


class FixtureServer:
    def __init__(self, scenario: str) -> None:
        self._scenario = scenario
        self._lock = threading.Lock()
        self._initialized = False
        self._initialized_notification = False
        self._next_thread = 1
        self._next_turn = 1
        self._next_approval = 900
        self._pending_approvals: dict[int, str] = {}
        self._pending_interrupts: set[str] = set()
        self._instance_id = uuid.uuid4().hex[:8]
        self._missing_turns: dict[str, str] = {}

    def send(self, payload: dict) -> None:
        _write_line(self._lock, payload)

    def _send_error(self, req_id: int, message: str) -> None:
        self.send(
            {
                "id": req_id,
                "error": {"code": -32601, "message": message},
            }
        )

    def _send_turn_completed(
        self,
        turn_id: str,
        *,
        status: str = "completed",
        approval_decision: Optional[str] = None,
    ) -> None:
        self.send(
            {
                "method": "item/completed",
                "params": {
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "text": "fixture reply",
                    },
                },
            }
        )
        params = {"turnId": turn_id, "status": status}
        if approval_decision is not None:
            params["approvalDecision"] = approval_decision
        self.send({"method": "turn/completed", "params": params})

    def _send_review_completed(
        self, turn_id: str, *, status: str = "completed"
    ) -> None:
        self.send(
            {
                "method": "item/completed",
                "params": {
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "text": "fixture reply",
                        "review": "fixture reply",
                    },
                },
            }
        )
        self.send(
            {
                "method": "turn/completed",
                "params": {"turnId": turn_id, "status": status},
            }
        )

    def _handle_request(self, message: dict) -> None:
        req_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if method != "initialize" and not self._initialized:
            self._send_error(req_id, "not initialized")
            return
        if method == "initialize":
            self._initialized = True
            self.send(
                {
                    "id": req_id,
                    "result": {
                        "serverInfo": {
                            "name": "fixture",
                            "instance": self._instance_id,
                        },
                        "capabilities": {},
                    },
                }
            )
            return
        if method == "thread/list":
            if (
                self._scenario == "thread_list_requires_params"
                and "params" not in message
            ):
                self._send_error(req_id, "Invalid request: missing field `params`")
                return
            if self._scenario in ("thread_list_empty", "thread_list_empty_refresh"):
                self.send(
                    {
                        "id": req_id,
                        "result": {"threads": [], "nextCursor": None},
                    }
                )
                return
            if self._scenario == "thread_list_paged":
                cursor = params.get("cursor")
                if cursor:
                    entries = [
                        {"id": "thread-2", "preview": "fixture preview 2"},
                        {"id": "thread-3", "preview": "fixture preview 3"},
                    ]
                    next_cursor = None
                else:
                    entries = [{"id": "thread-1", "preview": "fixture preview 1"}]
                    next_cursor = "cursor-2"
                self.send(
                    {
                        "id": req_id,
                        "result": {"threads": entries, "nextCursor": next_cursor},
                    }
                )
                return
            if self._scenario == "thread_list_compact_seed":
                entry = {
                    "id": "thread-compact",
                    "first_user_message": (
                        "Context from previous thread:\n\n"
                        "Goals: Fix resume labels\n\n"
                        "Continue from this context. Ask for missing info if needed."
                    ),
                    "last_user_message": "Latest user request",
                    "last_assistant_message": "Latest assistant reply",
                }
                self.send(
                    {
                        "id": req_id,
                        "result": {"threads": [entry], "nextCursor": None},
                    }
                )
                return
            cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None
            entry = {
                "id": "thread-seed",
                "preview": "fixture preview",
            }
            if cwd:
                entry["cwd"] = cwd
            if self._scenario == "thread_list_data_shape":
                self.send(
                    {
                        "id": req_id,
                        "result": {
                            "data": [entry],
                            "nextCursor": None,
                        },
                    }
                )
            else:
                self.send(
                    {
                        "id": req_id,
                        "result": [entry],
                    }
                )
            return
        if method == "thread/start":
            thread_id = f"thread-{self._next_thread}"
            self._next_thread += 1
            if self._scenario == "thread_id_key":
                result = {"threadId": thread_id, "cwd": params.get("cwd")}
            elif self._scenario == "thread_id_snake":
                result = {"thread_id": thread_id, "cwd": params.get("cwd")}
            elif self._scenario == "thread_start_missing_cwd":
                result = {"id": thread_id}
            elif self._scenario == "thread_start_mismatch":
                raw_cwd = params.get("cwd")
                if isinstance(raw_cwd, str):
                    mismatched = f"{raw_cwd.rstrip('/')}-other"
                else:
                    mismatched = "/mismatch"
                result = {"id": thread_id, "cwd": mismatched}
            else:
                result = {"id": thread_id, "cwd": params.get("cwd")}
            self.send(
                {
                    "id": req_id,
                    "result": result,
                }
            )
            return
        if method == "thread/resume":
            thread_id = params.get("threadId")
            if self._scenario == "thread_list_empty_refresh":
                self.send(
                    {
                        "id": req_id,
                        "result": {"id": thread_id, "preview": "refreshed preview"},
                    }
                )
            elif self._scenario == "missing_turn_completed":
                turn_id = self._missing_turns.pop(thread_id, None)
                result = {"id": thread_id}
                if turn_id:
                    result["turns"] = [{"id": turn_id, "status": "completed"}]
                    result["items"] = [
                        {
                            "turnId": turn_id,
                            "itemId": "item-1",
                            "type": "agentMessage",
                            "content": [
                                {"type": "output_text", "text": "recovered reply"}
                            ],
                        }
                    ]
                self.send({"id": req_id, "result": result})
            else:
                self.send({"id": req_id, "result": {"id": thread_id}})
            return
        if method == "turn/start":
            turn_id = f"turn-{self._next_turn}"
            self._next_turn += 1
            self.send({"id": req_id, "result": {"id": turn_id}})
            if self._scenario == "sandbox_policy_check":
                sandbox_policy = params.get("sandboxPolicy")
                if not (
                    isinstance(sandbox_policy, dict)
                    and sandbox_policy.get("type") == "dangerFullAccess"
                ):
                    self._send_error(req_id, "invalid sandboxPolicy")
                    return
            if self._scenario == "turn_error_no_agent":
                self.send(
                    {
                        "method": "error",
                        "params": {
                            "turnId": turn_id,
                            "threadId": params.get("threadId"),
                            "willRetry": False,
                            "error": {"message": "Auth required"},
                        },
                    }
                )
                self.send(
                    {
                        "method": "turn/completed",
                        "params": {"turnId": turn_id, "status": "failed"},
                    }
                )
                return
            if self._scenario == "approval":
                approval_id = self._next_approval
                self._next_approval += 1
                self._pending_approvals[approval_id] = turn_id
                self.send(
                    {
                        "id": approval_id,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "turnId": turn_id,
                            "command": "echo hello",
                            "reason": "fixture approval",
                        },
                    }
                )
                return
            if self._scenario == "interrupt":
                self._pending_interrupts.add(turn_id)
                return
            if self._scenario == "missing_turn_completed":
                thread_id = params.get("threadId")
                if isinstance(thread_id, str):
                    self._missing_turns[thread_id] = turn_id
                self.send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "turnId": turn_id,
                            "threadId": thread_id,
                            "itemId": "item-1",
                            "delta": "recovered ",
                        },
                    }
                )
                return
            self._send_turn_completed(turn_id)
            return
        if method == "review/start":
            turn_id = f"turn-{self._next_turn}"
            self._next_turn += 1
            self.send({"id": req_id, "result": {"id": turn_id}})
            if self._scenario == "review_duplicate":
                self._send_review_completed(turn_id)
            else:
                self._send_turn_completed(turn_id)
            return
        if method == "turn/interrupt":
            turn_id = params.get("turnId")
            thread_id = params.get("threadId")
            if not turn_id or not thread_id:
                self.send(
                    {
                        "id": req_id,
                        "error": {
                            "code": -32602,
                            "message": "turn/interrupt missing threadId or turnId",
                        },
                    }
                )
                return
            self.send({"id": req_id, "result": {}})
            if self._scenario == "interrupt" and turn_id in self._pending_interrupts:
                self._pending_interrupts.remove(turn_id)
                self._send_turn_completed(turn_id, status="interrupted")
            return
        if method == "command/exec":
            if self._scenario == "command_exec_hang":
                return
            self.send(
                {
                    "id": req_id,
                    "result": {
                        "stdout": "fixture output\n",
                        "stderr": "",
                        "exitCode": 0,
                    },
                }
            )
            return
        if method == "fixture/status":
            self.send(
                {
                    "id": req_id,
                    "result": {
                        "initialized": self._initialized,
                        "initializedNotification": self._initialized_notification,
                        "instance": self._instance_id,
                    },
                }
            )
            return
        if method == "fixture/slow":

            def _send_late() -> None:
                time.sleep(0.05)
                self.send({"id": req_id, "result": {"value": params.get("value")}})

            threading.Thread(target=_send_late, daemon=True).start()
            return
        if method == "fixture/fast":
            self.send({"id": req_id, "result": {"value": params.get("value")}})
            return
        if method == "fixture/echo":
            self.send(
                {
                    "id": req_id,
                    "result": {
                        "value": params.get("value"),
                        "instance": self._instance_id,
                    },
                }
            )
            return
        if method == "fixture/oversize_drop":
            oversized_payload = {
                "method": "fixture/oversized",
                "params": {
                    "threadId": "thread-oversize",
                    "turnId": "turn-oversize",
                    "blob": "x" * 2048,
                },
            }
            response_payload = {
                "id": req_id,
                "result": {"value": params.get("value")},
            }
            oversized_line = json.dumps(oversized_payload, separators=(",", ":"))
            response_line = json.dumps(response_payload, separators=(",", ":"))
            with self._lock:
                sys.stdout.write(f"{oversized_line}\n{response_line}\n")
                sys.stdout.flush()
            return
        if method == "fixture/echo_no_newline":
            payload = {
                "id": req_id,
                "result": {
                    "value": params.get("value"),
                    "instance": self._instance_id,
                },
            }
            data = json.dumps(payload, separators=(",", ":"))
            with self._lock:
                sys.stdout.write(data)
                sys.stdout.flush()
            sys.exit(0)
        if method == "fixture/crash":
            self.send({"id": req_id, "result": {"ok": True}})
            sys.stdout.flush()
            sys.exit(0)
        self._send_error(req_id, f"unsupported method: {method}")

    def _handle_response(self, message: dict) -> None:
        req_id = message.get("id")
        if req_id in self._pending_approvals:
            turn_id = self._pending_approvals.pop(req_id)
            decision = None
            result = message.get("result") or {}
            if isinstance(result, dict):
                decision = result.get("decision")
            self._send_turn_completed(turn_id, approval_decision=decision or "unknown")

    def _handle_notification(self, message: dict) -> None:
        if message.get("method") == "initialized":
            self._initialized_notification = True

    def run(self) -> None:
        for line in sys.stdin:
            payload = line.strip()
            if not payload:
                continue
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "method" in message and "id" in message:
                self._handle_request(message)
                continue
            if "method" in message and "id" not in message:
                self._handle_notification(message)
                continue
            if "id" in message:
                self._handle_response(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="basic")
    args = parser.parse_args()
    server = FixtureServer(args.scenario)
    server.run()


if __name__ == "__main__":
    main()
