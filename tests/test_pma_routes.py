import asyncio
import json
from pathlib import Path
from typing import Optional

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import pma_active_context_content, seed_hub_files
from codex_autorunner.core import filebox
from codex_autorunner.core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_context import maybe_auto_prune_active_context
from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from codex_autorunner.integrations.pma_delivery import PmaDeliveryOutcome
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.cli import pma_cli
from codex_autorunner.surfaces.web.routes import pma as pma_routes
from tests.conftest import write_test_config


def _enable_pma(
    hub_root: Path,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    max_text_chars: Optional[int] = None,
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    if max_text_chars is not None:
        cfg["pma"]["max_text_chars"] = max_text_chars
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _disable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _targets_state_without_updated_at(state: dict[str, object]) -> dict[str, object]:
    return {
        "version": state.get("version"),
        "targets": state.get("targets"),
        "last_delivery_by_target": state.get("last_delivery_by_target"),
        "active_target_key": state.get("active_target_key"),
    }


def _install_fake_successful_chat_supervisor(
    app,
    *,
    turn_id: str,
    message: str = "assistant text",
) -> None:
    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = turn_id

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {"agent_messages": [message], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()


def _delivery_outcome_for_status(status: str) -> PmaDeliveryOutcome:
    if status == "success":
        return PmaDeliveryOutcome(
            status=status,
            configured_targets=1,
            delivered_targets=1,
            failed_targets=0,
        )
    if status == "partial_success":
        return PmaDeliveryOutcome(
            status=status,
            configured_targets=2,
            delivered_targets=1,
            failed_targets=1,
        )
    if status == "failed":
        return PmaDeliveryOutcome(
            status=status,
            configured_targets=1,
            delivered_targets=0,
            failed_targets=1,
        )
    if status == "duplicate_only":
        return PmaDeliveryOutcome(
            status=status,
            configured_targets=1,
            delivered_targets=0,
            failed_targets=0,
            skipped_duplicates=1,
            skipped_duplicate_keys=["web"],
        )
    return PmaDeliveryOutcome(
        status="no_targets",
        configured_targets=0,
        delivered_targets=0,
        failed_targets=0,
    )


def test_pma_agents_endpoint(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.get("/hub/pma/agents")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("agents"), list)
    assert payload.get("default") in {agent.get("id") for agent in payload["agents"]}


def test_pma_chat_requires_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={})
    assert resp.status_code == 400


def test_pma_chat_rejects_oversize_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root, max_text_chars=5)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "toolong"})
    assert resp.status_code == 400
    payload = resp.json()
    assert "max_text_chars" in (payload.get("detail") or "")


def test_pma_routes_enabled_by_default(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code == 200
    assert client.post("/hub/pma/chat", json={}).status_code == 400


def test_pma_routes_disabled_by_config(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code == 404
    assert client.post("/hub/pma/chat", json={"message": "hi"}).status_code == 404


def test_pma_targets_add_list_remove_clear(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    initial = client.get("/hub/pma/targets")
    assert initial.status_code == 200
    assert initial.json()["targets"] == []

    add_web = client.post("/hub/pma/targets/add", json={"ref": "web"})
    assert add_web.status_code == 200
    assert add_web.json()["key"] == "web"

    add_local = client.post(
        "/hub/pma/targets/add",
        json={"ref": "local:.codex-autorunner/pma/deliveries.jsonl"},
    )
    assert add_local.status_code == 200
    assert add_local.json()["key"] == "local:.codex-autorunner/pma/deliveries.jsonl"

    add_telegram = client.post(
        "/hub/pma/targets/add",
        json={"ref": "telegram:-100123:777"},
    )
    assert add_telegram.status_code == 200
    assert add_telegram.json()["key"] == "chat:telegram:-100123:777"

    add_discord = client.post(
        "/hub/pma/targets/add",
        json={"ref": "discord:987654321"},
    )
    assert add_discord.status_code == 200
    assert add_discord.json()["key"] == "chat:discord:987654321"

    listed = client.get("/hub/pma/targets")
    assert listed.status_code == 200
    listed_keys = {row["key"] for row in listed.json()["targets"]}
    assert listed_keys == {
        "web",
        "local:.codex-autorunner/pma/deliveries.jsonl",
        "chat:telegram:-100123:777",
        "chat:discord:987654321",
    }

    remove = client.post(
        "/hub/pma/targets/remove",
        json={"ref": "chat:telegram:-100123:777"},
    )
    assert remove.status_code == 200
    assert remove.json()["removed"] is True

    clear = client.post("/hub/pma/targets/clear", json={})
    assert clear.status_code == 200
    assert clear.json()["targets"] == []

    state = PmaDeliveryTargetsStore(hub_env.hub_root).load()
    assert state["targets"] == []


def test_pma_targets_reject_invalid_ref(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    invalid_add = client.post(
        "/hub/pma/targets/add",
        json={"ref": "telegram:not-a-number"},
    )
    assert invalid_add.status_code == 400
    detail = invalid_add.json().get("detail", "")
    assert "Invalid target ref" in detail
    assert "telegram:<chat_id>" in detail

    invalid_remove = client.post(
        "/hub/pma/targets/remove",
        json={"ref": "discord:"},
    )
    assert invalid_remove.status_code == 400
    assert "Invalid target ref" in invalid_remove.json().get("detail", "")


def test_pma_targets_active_get_and_set(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    assert client.post("/hub/pma/targets/add", json={"ref": "web"}).status_code == 200
    assert (
        client.post("/hub/pma/targets/add", json={"ref": "telegram:-100123:777"})
    ).status_code == 200

    initial = client.get("/hub/pma/targets/active")
    assert initial.status_code == 200
    initial_payload = initial.json()
    assert initial_payload["active_target_key"] == "web"
    assert (initial_payload.get("active_target") or {}).get("key") == "web"

    set_active = client.post(
        "/hub/pma/targets/active", json={"ref": "telegram:-100123:777"}
    )
    assert set_active.status_code == 200
    set_payload = set_active.json()
    assert set_payload["status"] == "ok"
    assert set_payload["changed"] is True
    assert set_payload["active_target_key"] == "chat:telegram:-100123:777"
    assert (set_payload.get("active_target") or {}).get(
        "key"
    ) == "chat:telegram:-100123:777"
    assert (
        next(
            row
            for row in set_payload["targets"]
            if row["key"] == "chat:telegram:-100123:777"
        )["active"]
        is True
    )

    set_same = client.post(
        "/hub/pma/targets/active", json={"key": "chat:telegram:-100123:777"}
    )
    assert set_same.status_code == 200
    assert set_same.json()["changed"] is False


def test_pma_targets_active_rejects_invalid_inputs(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.post("/hub/pma/targets/add", json={"ref": "web"}).status_code == 200

    missing = client.post("/hub/pma/targets/active", json={})
    assert missing.status_code == 400
    assert "ref or key is required" in (missing.json().get("detail") or "")

    invalid_ref = client.post("/hub/pma/targets/active", json={"ref": "discord:"})
    assert invalid_ref.status_code == 400
    assert "Invalid target ref" in (invalid_ref.json().get("detail") or "")

    not_found = client.post("/hub/pma/targets/active", json={"key": "chat:discord:42"})
    assert not_found.status_code == 404
    assert "Target not found" in (not_found.json().get("detail") or "")


def test_pma_targets_web_state_matches_cli_helper(tmp_path: Path) -> None:
    web_root = tmp_path / "web-hub"
    cli_root = tmp_path / "cli-hub"

    seed_hub_files(web_root, force=True)
    seed_hub_files(cli_root, force=True)
    _enable_pma(web_root)

    web_app = create_hub_app(web_root)
    client = TestClient(web_app)

    add_refs = [
        "web",
        "local:.codex-autorunner/pma/deliveries.jsonl",
        "telegram:-100123:777",
        "discord:987654321",
        "chat:telegram:-100123:777",
    ]
    remove_refs = ["telegram:-100123:777"]
    set_active_refs = ["discord:987654321"]
    set_active_keys = ["web"]

    for ref in add_refs:
        resp = client.post("/hub/pma/targets/add", json={"ref": ref})
        assert resp.status_code == 200, ref

    for ref in remove_refs:
        resp = client.post("/hub/pma/targets/remove", json={"ref": ref})
        assert resp.status_code == 200, ref

    for ref in set_active_refs:
        resp = client.post("/hub/pma/targets/active", json={"ref": ref})
        assert resp.status_code == 200, ref

    for key in set_active_keys:
        resp = client.post("/hub/pma/targets/active", json={"key": key})
        assert resp.status_code == 200, key

    web_state = PmaDeliveryTargetsStore(web_root).load()

    cli_store = PmaDeliveryTargetsStore(cli_root)
    for ref in add_refs:
        parsed = pma_cli._parse_pma_target_ref(ref)
        assert parsed is not None
        cli_store.add_target(parsed)
    for ref in remove_refs:
        parsed = pma_cli._parse_pma_target_ref(ref)
        assert parsed is not None
        cli_store.remove_target(parsed)

    for ref in set_active_refs:
        parsed = pma_cli._parse_pma_target_ref(ref)
        assert parsed is not None
        cli_store.set_active_target(parsed)
    for key in set_active_keys:
        cli_store.set_active_target(key)

    cli_state = cli_store.load()
    assert _targets_state_without_updated_at(
        web_state
    ) == _targets_state_without_updated_at(cli_state)
    assert web_state.get("active_target_key") == "web"


def test_pma_ui_target_management_controls_present() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    index_html = (
        repo_root / "src" / "codex_autorunner" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="pma-targets-list"' in index_html
    assert 'id="pma-target-ref-input"' in index_html
    assert 'id="pma-target-add"' in index_html
    assert 'id="pma-targets-clear"' in index_html

    pma_source = (
        repo_root / "src" / "codex_autorunner" / "static_src" / "pma.ts"
    ).read_text(encoding="utf-8")
    assert "/hub/pma/targets" in pma_source
    assert "/hub/pma/targets/active" in pma_source
    assert "/hub/pma/targets/add" in pma_source
    assert "/hub/pma/targets/remove" in pma_source
    assert "/hub/pma/targets/clear" in pma_source
    assert "Set active" in pma_source


def test_pma_chat_applies_model_reasoning_defaults(hub_env) -> None:
    _enable_pma(hub_env.hub_root, model="test-model", reasoning="high")

    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_kwargs = None

        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            self.turn_kwargs = turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert app.state.app_server_supervisor.client.turn_kwargs == {
        "model": "test-model",
        "effort": "high",
    }


@pytest.mark.parametrize(
    ("targets", "turn_id", "pre_marked_keys", "expected_delivery_status"),
    [
        (
            [{"kind": "web"}],
            "turn-delivery-success",
            (),
            "success",
        ),
        (
            [
                {"kind": "web"},
                {
                    "kind": "chat",
                    "platform": "telegram",
                    "chat_id": "123",
                    "thread_id": "invalid-thread",
                },
            ],
            "turn-delivery-partial",
            (),
            "partial_success",
        ),
        (
            [
                {
                    "kind": "chat",
                    "platform": "telegram",
                    "chat_id": "123",
                    "thread_id": "invalid-thread",
                }
            ],
            "turn-delivery-failed",
            (),
            "failed",
        ),
        (
            [{"kind": "web"}],
            "turn-delivery-duplicate",
            ("web",),
            "duplicate_only",
        ),
    ],
)
def test_pma_chat_exposes_top_level_delivery_status(
    hub_env,
    targets: list[dict[str, str]],
    turn_id: str,
    pre_marked_keys: tuple[str, ...],
    expected_delivery_status: str,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    targets_store = PmaDeliveryTargetsStore(hub_env.hub_root)
    targets_store.set_targets(targets)
    for key in pre_marked_keys:
        assert targets_store.mark_delivered(key, turn_id) is True

    _install_fake_successful_chat_supervisor(
        app,
        turn_id=turn_id,
        message=f"delivery status {expected_delivery_status}",
    )

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert payload.get("delivery_status") == expected_delivery_status

    delivery_outcome = payload.get("delivery_outcome")
    assert isinstance(delivery_outcome, dict)
    assert delivery_outcome.get("status") == expected_delivery_status


def test_pma_chat_does_not_implicitly_set_web_active_target(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    targets_store = PmaDeliveryTargetsStore(hub_env.hub_root)
    targets_store.set_targets(
        [
            {"kind": "web"},
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "-100123",
                "thread_id": "777",
            },
        ]
    )
    assert targets_store.set_active_target("chat:telegram:-100123:777") is True

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-keep-active"

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {"agent_messages": ["assistant text"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert (payload.get("delivery_outcome") or {}).get("status") == "success"
    assert payload.get("delivery_status") == "success"

    state = targets_store.load()
    assert state["active_target_key"] == "chat:telegram:-100123:777"


@pytest.mark.parametrize(
    ("delivery_outcome_status", "expected_delivery_status"),
    [
        ("success", "success"),
        ("partial_success", "partial_success"),
        ("failed", "failed"),
        ("duplicate_only", "duplicate_only"),
        ("no_targets", "skipped"),
    ],
)
def test_pma_chat_exposes_delivery_status_summary(
    hub_env,
    monkeypatch: pytest.MonkeyPatch,
    delivery_outcome_status: str,
    expected_delivery_status: str,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    async def _fake_deliver_output(**kwargs):
        _ = kwargs
        return _delivery_outcome_for_status(delivery_outcome_status)

    monkeypatch.setattr(
        pma_routes, "deliver_pma_output_to_active_sink", _fake_deliver_output
    )

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-delivery-summary"

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {"agent_messages": ["assistant text"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert (payload.get("delivery_outcome") or {}).get(
        "status"
    ) == delivery_outcome_status
    assert payload.get("delivery_status") == expected_delivery_status


def test_pma_chat_delivery_status_reflects_dispatch_failure(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    async def _fake_deliver_output(**kwargs):
        _ = kwargs
        return _delivery_outcome_for_status("success")

    async def _fake_deliver_dispatches(**kwargs):
        _ = kwargs
        return PmaDeliveryOutcome(
            status="failed",
            configured_targets=1,
            delivered_targets=0,
            failed_targets=1,
            dispatch_count=1,
        )

    monkeypatch.setattr(
        pma_routes, "deliver_pma_output_to_active_sink", _fake_deliver_output
    )
    monkeypatch.setattr(
        pma_routes,
        "list_pma_dispatches_for_turn",
        lambda *_args, **_kwargs: [
            {
                "dispatch_id": "dispatch-1",
                "title": "Dispatch title",
                "body": "Dispatch body",
                "priority": "high",
                "links": [],
            }
        ],
    )
    monkeypatch.setattr(
        pma_routes,
        "deliver_pma_dispatches_to_delivery_targets",
        _fake_deliver_dispatches,
    )

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-dispatch-failure"

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {"agent_messages": ["assistant text"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert (payload.get("delivery_outcome") or {}).get("status") == "success"
    assert (payload.get("dispatch_delivery_outcome") or {}).get("status") == "failed"
    assert payload.get("delivery_status") == "partial_success"


def test_pma_chat_github_injection_uses_raw_user_message(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    app = create_hub_app(hub_env.hub_root)
    observed: dict[str, str] = {}

    async def _fake_github_context_injection(**kwargs):
        observed["link_source_text"] = str(kwargs.get("link_source_text") or "")
        prompt_text = str(kwargs.get("prompt_text") or "")
        return f"{prompt_text}\n\n[injected-from-github]", True

    monkeypatch.setattr(
        pma_routes, "maybe_inject_github_context", _fake_github_context_injection
    )

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.prompt = None

        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, approval_policy, sandbox_policy, turn_kwargs
            self.prompt = prompt
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    message = "https://github.com/example/repo/issues/321"
    resp = client.post("/hub/pma/chat", json={"message": message})
    assert resp.status_code == 200
    assert observed["link_source_text"] == message
    assert "[injected-from-github]" in str(
        app.state.app_server_supervisor.client.prompt
    )


@pytest.mark.anyio
async def test_pma_chat_idempotency_key_uses_full_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        prefix = "a" * 100
        message_one = f"{prefix}one"
        message_two = f"{prefix}two"
        task_one = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": message_one})
        )
        await anyio.sleep(0.05)
        task_two = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": message_two})
        )
        await anyio.sleep(0.05)
        assert not task_two.done()
        blocker.set()
        with anyio.fail_after(5):
            resp_one = await task_one
            resp_two = await task_two

    assert resp_one.status_code == 200
    assert resp_two.status_code == 200
    assert resp_two.json().get("deduped") is not True


@pytest.mark.anyio
async def test_pma_active_updates_during_running_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": "hi"})
        )
        try:
            with anyio.fail_after(2):
                while True:
                    resp = await client.get("/hub/pma/active")
                    assert resp.status_code == 200
                    payload = resp.json()
                    current = payload.get("current") or {}
                    if (
                        payload.get("active")
                        and current.get("thread_id")
                        and current.get("turn_id")
                    ):
                        break
                    await anyio.sleep(0.05)
            assert payload["active"] is True
            assert payload["current"]["lane_id"] == "pma:default"
        finally:
            blocker.set()
        resp = await chat_task
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


@pytest.mark.anyio
async def test_pma_second_lane_item_does_not_clobber_active_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()
    turn_start_calls = 0

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            _ = timeout
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            nonlocal turn_start_calls
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            turn_start_calls += 1
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-concurrency"
    start_lane_worker = app.state.pma_lane_worker_start
    stop_lane_worker = app.state.pma_lane_worker_stop
    assert callable(start_lane_worker)
    assert callable(stop_lane_worker)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post(
                "/hub/pma/chat",
                json={"message": "first turn", "client_turn_id": "turn-1"},
            )
        )
        try:
            with anyio.fail_after(2):
                while True:
                    active_resp = await client.get("/hub/pma/active")
                    assert active_resp.status_code == 200
                    active_payload = active_resp.json()
                    current = active_payload.get("current") or {}
                    if (
                        active_payload.get("active")
                        and current.get("thread_id")
                        and current.get("turn_id")
                    ):
                        break
                    await anyio.sleep(0.05)

            first_current = dict(active_payload["current"])
            assert first_current.get("client_turn_id") == "turn-1"
            assert turn_start_calls == 1

            second_item, _ = await queue.enqueue(
                lane_id,
                "pma:test-concurrency:key-2",
                {
                    "message": "second turn",
                    "agent": "codex",
                    "client_turn_id": "turn-2",
                },
            )
            await start_lane_worker(app, lane_id)

            second_result = None
            with anyio.fail_after(2):
                while True:
                    items = await queue.list_items(lane_id)
                    match = next(
                        (
                            entry
                            for entry in items
                            if entry.item_id == second_item.item_id
                        ),
                        None,
                    )
                    assert match is not None
                    if match.state in (
                        QueueItemState.COMPLETED,
                        QueueItemState.FAILED,
                    ):
                        second_result = dict(match.result or {})
                        break
                    await anyio.sleep(0.05)

            assert second_result is not None
            assert second_result.get("status") == "error"
            assert "already active" in (second_result.get("detail") or "").lower()
            assert turn_start_calls == 1

            still_active = (await client.get("/hub/pma/active")).json()
            assert still_active["active"] is True
            assert still_active["current"]["client_turn_id"] == "turn-1"
            assert still_active["current"]["thread_id"] == first_current["thread_id"]
            assert still_active["current"]["turn_id"] == first_current["turn_id"]
        finally:
            await stop_lane_worker(app, lane_id)
            blocker.set()

        first_resp = await chat_task
        assert first_resp.status_code == 200
        assert first_resp.json().get("status") == "ok"


def test_pma_active_clears_on_prompt_build_error(hub_env, monkeypatch) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    async def _boom(*args, **kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(pma_routes, "build_hub_snapshot", _boom)

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "error"
    assert "snapshot failed" in (payload.get("detail") or "")

    active = client.get("/hub/pma/active").json()
    assert active["active"] is False
    assert active["current"] == {}


def test_pma_thread_reset_clears_registry(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id(PMA_KEY, "thread-codex")
    registry.set_thread_id(PMA_OPENCODE_KEY, "thread-opencode")

    client = TestClient(app)
    resp = client.post("/hub/pma/thread/reset", json={"agent": "opencode"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) == "thread-codex"
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None

    resp = client.post("/hub/pma/thread/reset", json={"agent": "all"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) is None


def test_pma_reset_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post("/hub/pma/reset", json={"agent": "all"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()


def test_pma_stop_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post("/hub/pma/stop", json={"lane_id": "pma:default"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert payload["details"]["lane_id"] == "pma:default"


def test_pma_new_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post(
        "/hub/pma/new", json={"agent": "codex", "lane_id": "pma:default"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()


def test_pma_files_list_empty(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []
    assert payload["outbox"] == []


def test_pma_files_upload_list_download_delete(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to inbox
    files = {"file.txt": ("file.txt", b"Hello, PMA!", "text/plain")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["inbox"]) == 1
    assert payload["inbox"][0]["name"] == "file.txt"
    assert payload["inbox"][0]["box"] == "inbox"
    assert payload["inbox"][0]["size"] == 11
    assert payload["inbox"][0]["source"] == "filebox"
    assert "/hub/pma/files/inbox/file.txt" in payload["inbox"][0]["url"]
    assert payload["outbox"] == []
    assert (
        filebox.inbox_dir(hub_env.hub_root) / "file.txt"
    ).read_bytes() == b"Hello, PMA!"

    # Download file
    resp = client.get("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200
    assert resp.content == b"Hello, PMA!"

    # Delete file
    resp = client.delete("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200

    # Verify file is gone
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []


def test_pma_files_invalid_box(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Try to upload to invalid box
    files = {"file.txt": ("file.txt", b"test", "text/plain")}
    resp = client.post("/hub/pma/files/invalid", files=files)
    assert resp.status_code == 400

    # Try to download from invalid box
    resp = client.get("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400

    # Try to delete from invalid box
    resp = client.delete("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400


def test_pma_files_list_includes_legacy_sources(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.inbox_dir(hub_env.hub_root) / "primary.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "legacy-pma.txt").write_bytes(b"legacy-pma")
    legacy_telegram = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "inbox"
    )
    legacy_telegram.mkdir(parents=True, exist_ok=True)
    (legacy_telegram / "legacy-telegram.txt").write_bytes(b"legacy-telegram")

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    entries = {item["name"]: item for item in payload["inbox"]}
    assert entries["primary.txt"]["source"] == "filebox"
    assert entries["legacy-pma.txt"]["source"] == "pma"
    assert entries["legacy-telegram.txt"]["source"] == "telegram"


def test_pma_files_download_resolves_legacy_path(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "legacy.txt").write_bytes(b"legacy")

    resp = client.get("/hub/pma/files/inbox/legacy.txt")
    assert resp.status_code == 200
    assert resp.content == b"legacy"


def test_pma_files_outbox(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to outbox
    files = {"output.txt": ("output.txt", b"Output content", "text/plain")}
    resp = client.post("/hub/pma/files/outbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["outbox"]) == 1
    assert payload["outbox"][0]["name"] == "output.txt"
    assert payload["outbox"][0]["box"] == "outbox"
    assert "/hub/pma/files/outbox/output.txt" in payload["outbox"][0]["url"]
    assert payload["inbox"] == []

    # Download from outbox
    resp = client.get("/hub/pma/files/outbox/output.txt")
    assert resp.status_code == 200
    assert resp.content == b"Output content"


def test_pma_files_delete_removes_only_resolved_file(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.inbox_dir(hub_env.hub_root) / "shared.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "shared.txt").write_bytes(b"legacy-pma")
    legacy_telegram = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-2"
        / "inbox"
    )
    legacy_telegram.mkdir(parents=True, exist_ok=True)
    (legacy_telegram / "shared.txt").write_bytes(b"legacy-telegram")

    resp = client.delete("/hub/pma/files/inbox/shared.txt")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.inbox_dir(hub_env.hub_root) / "shared.txt").exists()
    assert (legacy_pma / "shared.txt").exists()
    assert (legacy_telegram / "shared.txt").exists()


def test_pma_files_bulk_delete_removes_all_visible_entries(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.outbox_dir(hub_env.hub_root) / "a.txt").write_bytes(b"a")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "outbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "b.txt").write_bytes(b"b")
    legacy_telegram_pending = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-3"
        / "outbox"
        / "pending"
    )
    legacy_telegram_pending.mkdir(parents=True, exist_ok=True)
    (legacy_telegram_pending / "c.txt").write_bytes(b"c")

    resp = client.delete("/hub/pma/files/outbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (client.get("/hub/pma/files").json()["outbox"]) == []
    assert not (filebox.outbox_dir(hub_env.hub_root) / "a.txt").exists()
    assert not (legacy_pma / "b.txt").exists()
    assert not (legacy_telegram_pending / "c.txt").exists()


def test_pma_files_bulk_delete_preserves_hidden_legacy_duplicate(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.outbox_dir(hub_env.hub_root) / "shared.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "outbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "shared.txt").write_bytes(b"legacy")

    resp = client.delete("/hub/pma/files/outbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.outbox_dir(hub_env.hub_root) / "shared.txt").exists()
    assert (legacy_pma / "shared.txt").exists()
    payload = client.get("/hub/pma/files").json()
    assert payload["outbox"][0]["name"] == "shared.txt"
    assert payload["outbox"][0]["source"] == "pma"


def test_pma_files_rejects_invalid_filenames(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Test traversal attempts - upload rejects invalid filenames
    for filename in ["../x", "..", "a/b", "a\\b", ".", ""]:
        files = {"file": (filename, b"test", "text/plain")}
        resp = client.post("/hub/pma/files/inbox", files=files)
        assert resp.status_code == 400, f"Should reject filename: {filename}"
        assert "filename" in resp.json()["detail"].lower()


def test_pma_files_size_limit(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    max_upload_bytes = DEFAULT_HUB_CONFIG["pma"]["max_upload_bytes"]

    # Upload a file that exceeds the size limit
    large_content = b"x" * (max_upload_bytes + 1)
    files = {"large.bin": ("large.bin", large_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()

    # Upload a file that is exactly at the limit
    limit_content = b"y" * max_upload_bytes
    files = {"limit.bin": ("limit.bin", limit_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200
    assert "limit.bin" in resp.json()["saved"]


def test_pma_files_returns_404_for_nonexistent_files(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Download non-existent file
    resp = client.get("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]

    # Delete non-existent file
    resp = client.delete("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]


def test_pma_docs_list(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    assert "docs" in payload
    docs = payload["docs"]
    assert isinstance(docs, list)
    doc_names = [doc["name"] for doc in docs]
    assert doc_names == [
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    ]
    for doc in docs:
        assert "name" in doc
        assert "exists" in doc
        if doc["exists"]:
            assert "size" in doc
            assert "mtime" in doc
        if doc["name"] == "active_context.md":
            assert "line_count" in doc


def test_pma_docs_list_includes_auto_prune_metadata(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    active_context = (
        hub_env.hub_root / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    active_context.write_text(
        "\n".join(f"line {idx}" for idx in range(220)), encoding="utf-8"
    )
    state = maybe_auto_prune_active_context(hub_env.hub_root, max_lines=50)
    assert state is not None

    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    meta = payload.get("active_context_auto_prune")
    assert isinstance(meta, dict)
    assert meta.get("line_count_before") == 220
    assert meta.get("line_budget") == 50
    assert isinstance(meta.get("last_auto_pruned_at"), str)


def test_pma_docs_get(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert "content" in payload
    assert isinstance(payload["content"], str)


def test_pma_docs_get_nonexistent(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Delete the canonical doc, then try to get it
    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_agents_path = pma_dir / "docs" / "AGENTS.md"
    if docs_agents_path.exists():
        docs_agents_path.unlink()

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_pma_docs_list_migrates_legacy_doc_into_canonical(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_path = pma_dir / "docs" / "active_context.md"
    legacy_path = pma_dir / "active_context.md"

    docs_path.unlink(missing_ok=True)
    legacy_content = "# Legacy copy\n\n- migrated\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200

    assert docs_path.exists()
    assert docs_path.read_text(encoding="utf-8") == legacy_content
    assert not legacy_path.exists()

    resp = client.get("/hub/pma/docs/active_context.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == legacy_content


def test_pma_docs_put(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    new_content = "# AGENTS\n\nNew content"
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": new_content})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert payload["status"] == "ok"

    # Verify the content was saved
    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == new_content


def test_pma_docs_put_invalid_name(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/invalid.md", json={"content": "test"})
    assert resp.status_code == 400
    assert "Unknown doc name" in resp.json()["detail"]


def test_pma_docs_put_too_large(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    large_content = "x" * 500_001
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": large_content})
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_pma_docs_put_invalid_content_type(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": 123})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI returns a list of validation errors
    if isinstance(detail, list):
        assert any("content" in str(err) for err in detail)
    else:
        assert "content" in str(detail)


def test_pma_context_snapshot(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_path = docs_dir / "active_context.md"
    active_content = "# Active Context\n\n- alpha\n- beta\n"
    active_path.write_text(active_content, encoding="utf-8")

    resp = client.post("/hub/pma/context/snapshot", json={"reset": True})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["active_context_line_count"] == len(active_content.splitlines())
    assert payload["reset"] is True

    log_content = (docs_dir / "context_log.md").read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content
    assert active_content in log_content
    assert active_path.read_text(encoding="utf-8") == pma_active_context_content()


def test_pma_docs_disabled(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 404

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
