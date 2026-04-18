from __future__ import annotations

import json
from pathlib import Path

from tests.chat_surface_lab.incident_replay import (
    build_replay_scenario_payload,
    convert_incident_trace_to_scenario,
    sanitize_incident_payload,
)
from tests.chat_surface_lab.scenario_runner import load_scenario


def test_convert_incident_trace_to_scenario_sanitizes_sensitive_data(
    tmp_path: Path,
) -> None:
    incident = {
        "incident_id": "incident-2026-04-19",
        "title": "Discord timeout regression",
        "token": "Bearer abcdefghijklmnopqrstuvwxyz123456",
        "notes": "Observed in /Users/dazheng/private-repo and user 123456789",
        "scenario": {
            "schema_version": 1,
            "scenario_id": "raw-incident",
            "description": "raw payload",
            "surfaces": ["discord"],
            "runtime_fixture": {"kind": "hermes", "scenario": "official"},
            "actions": [
                {
                    "kind": "send_message",
                    "actor": "user",
                    "payload": {
                        "text": "reproduce with key abcdefghijklmnopqrstuvwxyz123456"
                    },
                }
            ],
        },
    }
    incident_path = tmp_path / "incident.json"
    incident_path.write_text(json.dumps(incident, indent=2), encoding="utf-8")
    output_path = tmp_path / "scenario.json"

    convert_incident_trace_to_scenario(
        incident_path=incident_path,
        output_path=output_path,
        scenario_id="incident_timeout_replay",
    )

    assert output_path.exists()
    rendered = output_path.read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in rendered
    assert "/Users/dazheng" not in rendered
    assert "123456789" not in rendered

    scenario = load_scenario(output_path)
    assert scenario.scenario_id == "incident_timeout_replay"
    assert scenario.tags and "incident_replay" in scenario.tags


def test_build_replay_scenario_from_symptoms_supports_restart_and_duplicates() -> None:
    payload = build_replay_scenario_payload(
        incident={
            "incident_id": "incident-restart-dup",
            "surface": "telegram",
            "symptoms": ["restart_window", "duplicate_delivery"],
            "assert_contains": "Workspace:",
        },
    )

    loadable = sanitize_incident_payload(payload)
    assert isinstance(loadable, dict)
    scenario_actions = loadable.get("actions")
    assert isinstance(scenario_actions, list)
    assert any(
        item.get("kind") == "restart_surface_harness" for item in scenario_actions
    )
    assert any(item.get("kind") == "run_status_update" for item in scenario_actions)
    assert payload["scenario_id"] == "incident-restart-dup"
