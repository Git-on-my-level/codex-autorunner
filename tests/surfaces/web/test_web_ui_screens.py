"""Contract tests for ``scripts/web_ui_screens.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_module():
    path = _repo_root() / "scripts" / "web_ui_screens.py"
    spec = importlib.util.spec_from_file_location("web_ui_screens", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load web_ui_screens.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_routes_cover_primary_web_pages() -> None:
    mod = _load_module()
    routes = mod.parse_routes([])
    assert [(route.name, route.path) for route in routes] == list(mod.DEFAULT_ROUTES)
    assert ("hub", "/hub") in mod.DEFAULT_ROUTES
    assert ("repo-detail", "/repos/smoke-repo") in mod.DEFAULT_ROUTES
    assert (
        "worktree-detail",
        "/repos/smoke-repo/worktrees/smoke-repo--review",
    ) in mod.DEFAULT_ROUTES
    assert (
        "ticket-detail",
        "/tickets/TICKET-350-smoke-fixture",
    ) in mod.DEFAULT_ROUTES
    assert ("contextspace", "/contextspace/local") in mod.DEFAULT_ROUTES
    assert ("dashboard", "/dashboard") not in mod.DEFAULT_ROUTES


def test_default_routes_match_web_ui_qa_docs() -> None:
    mod = _load_module()
    docs = (_repo_root() / "docs" / "ops" / "web-ui-qa.md").read_text(encoding="utf-8")
    documented_routes = [
        line.strip()[3:-1]
        for line in docs.splitlines()
        if line.strip().startswith("- `/") and line.strip().endswith("`")
    ]

    assert documented_routes == [path for _name, path in mod.DEFAULT_ROUTES]


def test_custom_route_parsing() -> None:
    mod = _load_module()
    routes = mod.parse_routes(["chat=/chats", "ticket-detail=/tickets/example"])
    assert [(route.name, route.path) for route in routes] == [
        ("chat", "/chats"),
        ("ticket-detail", "/tickets/example"),
    ]


@pytest.mark.parametrize(
    "raw", ["missing-equals", "=/chats", "bad/name=/chats", "chat=chats"]
)
def test_custom_route_validation(raw: str) -> None:
    mod = _load_module()
    with pytest.raises(ValueError):
        mod.parse_routes([raw])


def test_viewport_parsing() -> None:
    mod = _load_module()
    assert mod.parse_viewport("1440x1000") == (1440, 1000)
    assert mod.parse_viewport("390X844") == (390, 844)
    assert mod.parse_viewports([]) == [(1440, 1000), (390, 844)]
    assert mod.parse_viewports(["1280x720"]) == [(1280, 720)]


@pytest.mark.parametrize("raw", ["1440", "widextall", "100x100"])
def test_viewport_validation(raw: str) -> None:
    mod = _load_module()
    with pytest.raises(ValueError):
        mod.parse_viewport(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("car", "/car"),
        ("/car/", "/car"),
    ],
)
def test_base_path_normalization(raw: str, expected: str) -> None:
    mod = _load_module()
    assert mod.normalize_base_path(raw) == expected


def test_route_url_joins_origin_base_path_and_route() -> None:
    mod = _load_module()
    assert (
        mod.route_url("http://127.0.0.1:4173/", "/car", "/chats")
        == "http://127.0.0.1:4173/car/chats"
    )


def test_manifest_shape_has_agent_readable_capture_fields(tmp_path: Path) -> None:
    mod = _load_module()
    manifest = mod.build_manifest(
        mode="url",
        base_url="http://127.0.0.1:4173",
        base_path="/car",
        hub_root=None,
        out_dir=tmp_path,
        viewports=[(1440, 1000)],
        full_page=True,
    )
    manifest["captures"].append(
        {
            "name": "chat",
            "path": "/chats",
            "url": "http://127.0.0.1:4173/car/chats",
            "final_url": "http://127.0.0.1:4173/car/chats",
            "viewport": {"width": 1440, "height": 1000},
            "duration_ms": 123,
            "navigation": {"ok": True, "status": 200, "error": None},
            "loading_marker": {
                "state": "cleared",
                "remaining_markers": [],
                "rendered_error_state": False,
            },
            "screenshot": {
                "path": str(tmp_path / "1440x1000" / "chat.png"),
                "relative_path": "1440x1000/chat.png",
                "size_bytes": 42,
                "error": None,
            },
            "accessibility": {
                "path": str(tmp_path / "1440x1000" / "chat.a11y_snapshot.json"),
                "relative_path": "1440x1000/chat.a11y_snapshot.json",
                "error": None,
            },
            "dom_summary": {
                "path": str(tmp_path / "1440x1000" / "chat.dom_summary.json"),
                "relative_path": "1440x1000/chat.dom_summary.json",
            },
            "console": {"errors": [], "warnings": []},
            "network": {"failed_requests": []},
            "layout": {
                "path": str(tmp_path / "1440x1000" / "chat.layout_diagnostics.json"),
                "relative_path": "1440x1000/chat.layout_diagnostics.json",
                "has_horizontal_overflow": False,
                "overflowing_elements": [],
                "clipped_elements": [],
            },
            "failure_subsystems": [],
        }
    )

    assert manifest["schema_version"] == 2
    assert manifest["captures"][0]["screenshot"]["relative_path"] == (
        "1440x1000/chat.png"
    )
    assert manifest["captures"][0]["accessibility"]["relative_path"] == (
        "1440x1000/chat.a11y_snapshot.json"
    )
    assert manifest["captures"][0]["dom_summary"]["relative_path"] == (
        "1440x1000/chat.dom_summary.json"
    )
    assert manifest["captures"][0]["layout"]["relative_path"] == (
        "1440x1000/chat.layout_diagnostics.json"
    )
    assert set(manifest["diagnostics"]) == {
        "console_errors",
        "console_warnings",
        "page_errors",
        "failed_requests",
    }


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            {
                "navigation": {"ok": False},
                "screenshot": {"path": "x.png", "size_bytes": 1},
            },
            ["navigation"],
        ),
        (
            {
                "console": {"errors": [{"text": "boom"}]},
                "screenshot": {"path": "x.png", "size_bytes": 1},
            },
            ["console"],
        ),
        (
            {
                "network": {"failed_requests": [{"url": "/api"}]},
                "screenshot": {"path": "x.png", "size_bytes": 1},
            },
            ["network"],
        ),
        (
            {
                "loading_marker": {
                    "state": "timeout",
                    "rendered_error_state": False,
                },
                "screenshot": {"path": "x.png", "size_bytes": 1},
            },
            ["loading"],
        ),
        (
            {
                "layout": {
                    "has_horizontal_overflow": True,
                    "clipped_elements": [],
                },
                "screenshot": {"path": "x.png", "size_bytes": 1},
            },
            ["layout"],
        ),
        ({"screenshot": {"size_bytes": 0}}, ["screenshot"]),
        (
            {
                "accessibility": {"error": "cdp unavailable"},
                "screenshot": {"path": "x.png", "size_bytes": 1},
            },
            ["accessibility capture"],
        ),
    ],
)
def test_failure_classification_names_subsystem(record, expected: list[str]) -> None:
    mod = _load_module()
    assert mod.classify_capture_failures(record) == expected


def test_loading_marker_status_classifies_timeout_present_and_cleared() -> None:
    mod = _load_module()
    assert mod.build_loading_marker_status(
        timed_out=True, body_text="Loading tickets"
    ) == {
        "state": "timeout",
        "remaining_markers": ["Loading tickets"],
        "rendered_error_state": False,
    }
    assert (
        mod.build_loading_marker_status(timed_out=False, body_text="Loading settings")[
            "state"
        ]
        == "present"
    )
    assert mod.build_loading_marker_status(
        timed_out=False, body_text="Tickets loaded"
    ) == {
        "state": "cleared",
        "remaining_markers": [],
        "rendered_error_state": False,
    }
