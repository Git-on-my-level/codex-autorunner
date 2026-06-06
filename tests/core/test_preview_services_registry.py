from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from codex_autorunner.core.preview_services import (
    PreviewServiceRegistry,
    PreviewServiceRegistryError,
    PreviewServiceStatus,
    services_read_model,
)
from codex_autorunner.core.preview_services.ids import validate_service_id
from codex_autorunner.core.preview_services.models import (
    PreviewServiceRecord,
    service_record_from_dict,
)


def static_service_payload(service_id: str = "svc_static123") -> dict:
    return {
        "service_id": service_id,
        "name": "Static preview",
        "kind": "static_file",
        "status": "registered",
        "created_by": "test",
        "created_at": "2026-06-05T00:00:00Z",
        "updated_at": "2026-06-05T00:00:00Z",
        "scope_links": [{"kind": "repo", "id": "car"}],
        "target": {"path": "/tmp/index.html"},
        "exposure": {
            "car_url": f"/preview/services/{service_id}/",
            "proxy_enabled": True,
        },
        "restart_policy": {
            "auto_start_on_hub_start": False,
            "restart_on_exit": "never",
        },
        "metadata": {"label": "demo"},
    }


def loopback_service_payload(service_id: str = "svc_loopback123") -> dict:
    return {
        "service_id": service_id,
        "name": "Loopback preview",
        "kind": "loopback_url",
        "status": "healthy",
        "created_at": "2026-06-05T00:00:00Z",
        "updated_at": "2026-06-05T00:00:01Z",
        "target": {
            "host": "127.0.0.1",
            "port": 39001,
            "scheme": "http",
            "direct_url": "http://127.0.0.1:39001/",
        },
        "exposure": {"car_url": f"/preview/services/{service_id}/"},
        "health_check": {"type": "http", "path": "/", "expected_status": [200]},
    }


def managed_service_payload(service_id: str = "svc_managed123") -> dict:
    return {
        "service_id": service_id,
        "name": "Managed preview",
        "kind": "managed_command",
        "status": "running",
        "created_at": "2026-06-05T00:00:00Z",
        "updated_at": "2026-06-05T00:00:01Z",
        "target": {
            "host": "127.0.0.1",
            "port": 39002,
            "scheme": "http",
            "direct_url": "http://127.0.0.1:39002/",
        },
        "exposure": {"car_url": f"/preview/services/{service_id}/"},
        "port_policy": {"mode": "auto", "allocated_port": 39002},
        "command": {
            "argv": ["npm", "run", "dev", "--", "--port", "$PORT"],
            "cwd": "/tmp/repo",
            "env": {"PORT": "$PORT"},
        },
        "process": {
            "pid": 1234,
            "pgid": 1234,
            "started_at": "2026-06-05T00:00:01Z",
        },
        "logs": {
            "path": ".codex-autorunner/services/logs/svc_managed123.log",
            "tail_url": "/hub/services/svc_managed123/logs",
        },
    }


def test_service_record_round_trips_without_losing_fields(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)
    created = registry.create(static_service_payload())

    loaded = registry.require(created.service_id)

    assert loaded.to_dict() == created.to_dict()
    raw = json.loads(
        (
            tmp_path
            / ".codex-autorunner"
            / "services"
            / "records"
            / f"{created.service_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert raw == created.to_dict()


def test_static_and_loopback_services_do_not_require_process_metadata(
    tmp_path: Path,
) -> None:
    registry = PreviewServiceRegistry(tmp_path)

    static = registry.create(static_service_payload())
    loopback = registry.create(loopback_service_payload())

    assert static.process is None
    assert static.command is None
    assert loopback.process is None
    assert loopback.command is None


def test_old_records_load_with_safe_taxonomy_defaults() -> None:
    static_payload = static_service_payload()
    loopback_payload = loopback_service_payload()
    managed_payload = managed_service_payload()
    for payload in (static_payload, loopback_payload, managed_payload):
        payload.pop("service_class", None)
        payload.pop("trust_level", None)
        payload.pop("ownership", None)
        payload.pop("network_policy", None)

    static = service_record_from_dict(static_payload)
    loopback = service_record_from_dict(loopback_payload)
    managed = service_record_from_dict(managed_payload)

    assert static.service_class == "preview"
    assert static.trust_level == "generated"
    assert static.ownership == "static"
    assert static.network_policy == "loopback_only"
    assert loopback.service_class == "application"
    assert loopback.trust_level == "external"
    assert loopback.ownership == "external"
    assert managed.service_class == "preview"
    assert managed.trust_level == "generated"
    assert managed.ownership == "car_managed"


def test_infrastructure_metadata_default_is_trusted() -> None:
    payload = managed_service_payload()
    payload["created_by"] = "opencode"
    payload["metadata"] = {"service_class": "infrastructure"}

    record = service_record_from_dict(payload)

    assert record.service_class == "infrastructure"
    assert record.trust_level == "trusted"
    assert record.ownership == "car_managed"


def test_managed_service_can_include_process_metadata(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)

    managed = registry.create(managed_service_payload())

    assert managed.command is not None
    assert managed.process is not None
    assert managed.process.pid == 1234


def test_invalid_enum_values_fail_with_useful_error() -> None:
    payload = static_service_payload()
    payload["kind"] = "unknown"

    with pytest.raises(ValidationError, match="kind"):
        PreviewServiceRecord.model_validate(payload)


def test_invalid_service_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="service_id"):
        validate_service_id("../svc_bad")

    registry = PreviewServiceRegistry(tmp_path)
    payload = static_service_payload("../svc_bad")
    payload["exposure"]["car_url"] = "/preview/services/../svc_bad/"
    with pytest.raises(ValueError, match="service_id"):
        registry.create(payload)


def test_record_service_id_must_match_car_url() -> None:
    payload = static_service_payload("svc_static123")
    payload["exposure"]["car_url"] = "/preview/services/svc_other123/"

    with pytest.raises(ValueError, match="exposure.car_url"):
        service_record_from_dict(payload)


def test_registry_update_and_delete_do_not_require_subprocess(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)
    created = registry.create(static_service_payload())

    updated = registry.update(
        created.service_id, {"status": PreviewServiceStatus.STOPPED}
    )
    assert updated.status == PreviewServiceStatus.STOPPED.value
    assert registry.delete(created.service_id) is True
    assert registry.get(created.service_id) is None


def test_corrupt_json_handling_reports_record_path(tmp_path: Path) -> None:
    records_dir = tmp_path / ".codex-autorunner" / "services" / "records"
    records_dir.mkdir(parents=True)
    corrupt_path = records_dir / "svc_corrupt123.json"
    corrupt_path.write_text("{ nope", encoding="utf-8")

    registry = PreviewServiceRegistry(tmp_path)

    with pytest.raises(
        PreviewServiceRegistryError, match="Invalid preview service JSON"
    ):
        registry.list()
    with pytest.raises(PreviewServiceRegistryError, match=str(corrupt_path)):
        registry.require("svc_corrupt123")


def test_invalid_registry_filename_is_reported(tmp_path: Path) -> None:
    records_dir = tmp_path / ".codex-autorunner" / "services" / "records"
    records_dir.mkdir(parents=True)
    (records_dir / "../bad.json").resolve().write_text("{}", encoding="utf-8")
    bad_path = records_dir / "bad.json"
    bad_path.write_text("{}", encoding="utf-8")

    registry = PreviewServiceRegistry(tmp_path)

    with pytest.raises(PreviewServiceRegistryError, match="service_id"):
        registry.list()


def test_read_model_shape_is_stable(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)
    static = registry.create(static_service_payload())
    loopback = registry.create(loopback_service_payload())
    managed = registry.create(managed_service_payload())

    model = registry.read_model()
    standalone_model = services_read_model([static, loopback, managed])

    assert model == standalone_model
    assert model["counts"] == {
        "total": 3,
        "running": 2,
        "attention": 0,
        "managed": 1,
        "static": 1,
        "loopback": 1,
        "preview": 2,
        "application": 1,
        "infrastructure": 0,
    }
    assert model["services"][0]["service_id"] == "svc_loopback123"
    assert {
        "service_id",
        "name",
        "kind",
        "status",
        "car_url",
        "scope_links",
        "proxy_enabled",
        "service_class",
        "trust_level",
        "ownership",
        "network_policy",
        "capabilities",
        "desired_state",
        "observed_state",
    } <= set(model["services"][0])


def test_read_model_redacts_command_env_values(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)
    payload = managed_service_payload()
    payload["command"]["env"] = {
        "PORT": "$PORT",
        "OPENAI_API_KEY": "secret",
    }
    managed = registry.create(payload)

    model = services_read_model([managed])
    command = model["services"][0]["desired_state"]["command"]

    assert command["env"] == {
        "PORT": "<redacted>",
        "OPENAI_API_KEY": "<redacted>",
    }
    assert "secret" not in json.dumps(model)


def test_read_model_capabilities_cover_service_lifecycle_states(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)
    static = registry.create(static_service_payload("svc_static124"))
    loopback = registry.create(loopback_service_payload("svc_loopback124"))
    running = registry.create(managed_service_payload("svc_running124"))
    stopped_payload = managed_service_payload("svc_stopped124")
    stopped_payload["status"] = "stopped"
    stopped_payload.pop("process")
    stopped = registry.create(stopped_payload)
    failed_payload = managed_service_payload("svc_failed124")
    failed_payload["status"] = "failed"
    failed_payload.pop("process")
    failed = registry.create(failed_payload)
    orphaned_payload = managed_service_payload("svc_orphaned124")
    orphaned_payload["status"] = "orphaned"
    orphaned = registry.create(orphaned_payload)
    exited_payload = managed_service_payload("svc_exited124")
    exited_payload["status"] = "exited"
    exited_payload["process"]["exit_code"] = 1
    exited = registry.create(exited_payload)

    by_id = {
        item["service_id"]: item
        for item in services_read_model(
            [static, loopback, running, stopped, failed, orphaned, exited]
        )["services"]
    }

    assert by_id[static.service_id]["capabilities"]["can_open"] is True
    assert by_id[static.service_id]["capabilities"]["can_start"] is False
    assert by_id[loopback.service_id]["capabilities"]["can_unlink"] is True
    assert by_id[running.service_id]["capabilities"]["can_stop"] is True
    assert by_id[running.service_id]["capabilities"]["can_kill"] is True
    assert (
        by_id[running.service_id]["capabilities"]["requires_force_for_unlink"] is True
    )
    assert by_id[stopped.service_id]["capabilities"]["can_start"] is True
    assert by_id[failed.service_id]["capabilities"]["can_start"] is True
    assert by_id[orphaned.service_id]["capabilities"]["can_start"] is False
    assert by_id[exited.service_id]["capabilities"]["can_start"] is True
    assert by_id[exited.service_id]["observed_state"]["status"] == "exited"


def test_path_payload_mismatch_is_rejected_on_list(tmp_path: Path) -> None:
    registry = PreviewServiceRegistry(tmp_path)
    payload = static_service_payload("svc_payload123")
    registry.create(payload)
    src = registry.records_dir / "svc_payload123.json"
    dst = registry.records_dir / "svc_path123.json"
    src.rename(dst)

    with pytest.raises(
        PreviewServiceRegistryError, match="does not match registry path"
    ):
        registry.list()
