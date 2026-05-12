from __future__ import annotations

import json
from pathlib import Path

from scripts.web_ui_dogfood_report import build_report, main


def test_dogfood_report_groups_browser_findings(tmp_path: Path) -> None:
    fast = tmp_path / "fast.json"
    screens = tmp_path / "screens.json"
    smoke = tmp_path / "smoke.json"
    out = tmp_path / "latest.json"
    fast.write_text(
        json.dumps({"status": "passed", "failures": []}),
        encoding="utf-8",
    )
    screens.write_text(
        json.dumps(
            {
                "status": "failed",
                "out_dir": str(tmp_path / "screens"),
                "captures": [
                    {
                        "name": "tickets",
                        "path": "/tickets",
                        "viewport": {"width": 390, "height": 844},
                        "failure_subsystems": ["console"],
                        "console": {
                            "errors": [
                                {
                                    "text": "Not found: /car/tickets",
                                    "location": {"url": "app.js"},
                                }
                            ]
                        },
                        "screenshot": {"path": str(tmp_path / "tickets.png")},
                        "dom_summary": {"path": str(tmp_path / "tickets.dom.json")},
                        "accessibility": {},
                        "layout": {"path": str(tmp_path / "tickets.layout.json")},
                    }
                ],
                "diagnostics": {"failed_requests": [], "console_errors": []},
            }
        ),
        encoding="utf-8",
    )
    smoke.write_text(json.dumps({"status": "passed"}), encoding="utf-8")

    report = build_report(
        fast_summary_path=fast,
        screen_manifest_path=screens,
        smoke_manifest_path=smoke,
        out_path=out,
    )

    assert report["status"] == "failed"
    assert report["finding_count"] == 1
    assert report["high_or_medium_count"] == 1
    assert "console/network noise" in report["groups"]
    finding = report["findings"][0]
    assert finding["scenario_id"] == "tickets"
    assert finding["route"] == "/tickets"
    assert finding["viewport"] == "390x844"
    assert finding["rerun_command"].startswith("make web-ui-screens")


def test_dogfood_report_marks_optional_voice_config_as_non_actionable(
    tmp_path: Path,
) -> None:
    fast = tmp_path / "fast.json"
    screens = tmp_path / "screens.json"
    smoke = tmp_path / "smoke.json"
    out = tmp_path / "latest.json"
    fast.write_text(json.dumps({"status": "passed", "failures": []}))
    screens.write_text(
        json.dumps(
            {
                "status": "failed",
                "out_dir": str(tmp_path / "screens"),
                "captures": [
                    {
                        "name": "chat",
                        "path": "/chats",
                        "viewport": {"width": 1440, "height": 1000},
                        "failure_subsystems": ["console"],
                        "console": {
                            "errors": [
                                {
                                    "text": "Failed to load resource",
                                    "location": {"url": "/car/api/voice/config"},
                                }
                            ]
                        },
                    }
                ],
                "diagnostics": {
                    "failed_requests": [],
                    "console_errors": [{"location": {"url": "/car/api/voice/config"}}],
                },
            }
        )
    )
    smoke.write_text(json.dumps({"status": "passed"}))

    report = build_report(
        fast_summary_path=fast,
        screen_manifest_path=screens,
        smoke_manifest_path=smoke,
        out_path=out,
    )

    assert report["finding_count"] == 0
    assert report["non_actionable"][0]["id"] == "optional-voice-config-404"


def test_dogfood_report_cli_writes_latest(tmp_path: Path) -> None:
    fast = tmp_path / "fast.json"
    screens = tmp_path / "screens.json"
    smoke = tmp_path / "smoke.json"
    out = tmp_path / "latest.json"
    fast.write_text(json.dumps({"status": "passed", "failures": []}))
    screens.write_text(
        json.dumps({"status": "passed", "captures": [], "diagnostics": {}})
    )
    smoke.write_text(json.dumps({"status": "passed"}))

    exit_code = main(
        [
            "--fast-summary",
            str(fast),
            "--screen-manifest",
            str(screens),
            "--smoke-manifest",
            str(smoke),
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    report = json.loads(out.read_text())
    assert report["kind"] == "web_ui_dogfood_campaign"
    assert report["diagnostics_path"] == str(out)
