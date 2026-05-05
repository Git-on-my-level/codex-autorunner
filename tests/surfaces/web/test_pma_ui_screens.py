"""Contract tests for ``scripts/pma_ui_screens.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_module():
    path = _repo_root() / "scripts" / "pma_ui_screens.py"
    spec = importlib.util.spec_from_file_location("pma_ui_screens", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load pma_ui_screens.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_routes_cover_primary_pma_pages() -> None:
    mod = _load_module()
    routes = mod.parse_routes([])
    assert [(route.name, route.path) for route in routes] == list(mod.DEFAULT_ROUTES)
    assert ("repo-detail", "/repos/smoke-repo") in mod.DEFAULT_ROUTES
    assert ("worktree-detail", "/worktrees/smoke-repo--review") in mod.DEFAULT_ROUTES
    assert ("ticket-detail", "/tickets/350") in mod.DEFAULT_ROUTES
    assert ("contextspace", "/contextspace/local") in mod.DEFAULT_ROUTES


def test_custom_route_parsing() -> None:
    mod = _load_module()
    routes = mod.parse_routes(["chat=/pma", "ticket-detail=/tickets/example"])
    assert [(route.name, route.path) for route in routes] == [
        ("chat", "/pma"),
        ("ticket-detail", "/tickets/example"),
    ]


@pytest.mark.parametrize(
    "raw", ["missing-equals", "=/pma", "bad/name=/pma", "chat=pma"]
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
        mod.route_url("http://127.0.0.1:4173/", "/car", "/pma")
        == "http://127.0.0.1:4173/car/pma"
    )
