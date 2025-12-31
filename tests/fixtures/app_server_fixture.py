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
                        "serverInfo": {"name": "fixture", "instance": self._instance_id},
                        "capabilities": {},
                    },
                }
            )
            return
        if method == "thread/list":
            self.send(
                {
                    "id": req_id,
                    "result": [
                        {
                            "id": "thread-seed",
                            "preview": "fixture preview",
                            "cwd": params.get("cwd", "/tmp"),
                        }
                    ],
                }
            )
            return
        if method == "thread/start":
            thread_id = f"thread-{self._next_thread}"
            self._next_thread += 1
            self.send(
                {
                    "id": req_id,
                    "result": {"id": thread_id, "cwd": params.get("cwd")},
                }
            )
            return
        if method == "thread/resume":
            thread_id = params.get("threadId")
            self.send({"id": req_id, "result": {"id": thread_id}})
            return
        if method == "turn/start":
            turn_id = f"turn-{self._next_turn}"
            self._next_turn += 1
            self.send({"id": req_id, "result": {"id": turn_id}})
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
            self._send_turn_completed(turn_id)
            return
        if method == "turn/interrupt":
            turn_id = params.get("turnId")
            self.send({"id": req_id, "result": {"id": turn_id, "status": "interrupted"}})
            if self._scenario == "interrupt" and turn_id in self._pending_interrupts:
                self._pending_interrupts.remove(turn_id)
                self._send_turn_completed(turn_id, status="interrupted")
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
