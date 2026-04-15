from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Iterable, Literal, Sequence

ValidationLane = Literal["core", "web-ui", "chat-apps", "aggregate"]
ScopedValidationLane = Literal["core", "web-ui", "chat-apps"]

_SCOPED_LANES: tuple[ScopedValidationLane, ...] = ("core", "web-ui", "chat-apps")

_SHARED_RISK_EXACT_PATHS = frozenset(
    {
        "Makefile",
        "codex-autorunner.yml",
        "codex-autorunner.override.yml",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "tests/conftest.py",
        "src/codex_autorunner/core/pytest_temp_cleanup.py",
        "src/codex_autorunner/core/validation_lanes.py",
    }
)
_SHARED_RISK_PREFIXES = (
    ".github/workflows",
    ".githooks",
    "scripts",
)

_LANE_PREFIXES: dict[ScopedValidationLane, tuple[str, ...]] = {
    "core": (
        "src/codex_autorunner/core",
        "src/codex_autorunner/flows",
        "src/codex_autorunner/surfaces/cli",
        "tests/core",
        "tests/flows",
        "tests/surfaces/cli",
        "tests/tickets",
    ),
    "web-ui": (
        "src/codex_autorunner/static",
        "src/codex_autorunner/static_src",
        "src/codex_autorunner/surfaces/web",
        "tests/js",
        "tests/routes",
        "tests/surfaces/web",
    ),
    "chat-apps": (
        "src/codex_autorunner/integrations/chat",
        "src/codex_autorunner/integrations/discord",
        "src/codex_autorunner/integrations/telegram",
        "tests/chat_surface_harness",
        "tests/chat_surface_integration",
        "tests/fixtures/discord",
        "tests/fixtures/telegram",
        "tests/integrations/chat",
        "tests/integrations/discord",
    ),
}

_LANE_GLOBS: dict[ScopedValidationLane, tuple[str, ...]] = {
    "core": (),
    "web-ui": (
        "tests/test_app_server*.py",
        "tests/test_auth_middleware.py",
        "tests/test_base_path*.py",
        "tests/test_hub_ui*.py",
        "tests/test_static*.py",
    ),
    "chat-apps": (
        "tests/test_chat*.py",
        "tests/test_discord*.py",
        "tests/test_telegram*.py",
        "tests/discord*.py",
        "tests/telegram*.py",
    ),
}


@dataclass(frozen=True)
class ValidationLaneSelection:
    lane: ValidationLane
    reason: str
    normalized_paths: tuple[str, ...]
    lanes_touched: tuple[ScopedValidationLane, ...]
    shared_risk_paths: tuple[str, ...]
    unknown_paths: tuple[str, ...]


def normalize_changed_path(raw_path: str) -> str:
    normalized = str(raw_path).strip().replace("\\", "/")
    if not normalized:
        return ""
    normalized = PurePosixPath(normalized).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return "" if normalized == "." else normalized


def classify_changed_files(paths: Iterable[str]) -> ValidationLaneSelection:
    normalized_paths = tuple(
        sorted({path for path in map(normalize_changed_path, paths) if path})
    )

    if not normalized_paths:
        return ValidationLaneSelection(
            lane="aggregate",
            reason="empty-change-set",
            normalized_paths=(),
            lanes_touched=(),
            shared_risk_paths=(),
            unknown_paths=(),
        )

    shared_risk_paths: list[str] = []
    unknown_paths: list[str] = []
    touched_lanes: set[ScopedValidationLane] = set()

    for path in normalized_paths:
        if _is_shared_risk_path(path):
            shared_risk_paths.append(path)
            continue

        scoped_lane = _classify_scoped_lane(path)
        if scoped_lane is None:
            unknown_paths.append(path)
            continue
        touched_lanes.add(scoped_lane)

    lanes_touched = tuple(lane for lane in _SCOPED_LANES if lane in touched_lanes)
    if shared_risk_paths:
        selected_lane: ValidationLane = "aggregate"
        reason = "shared-risk-path"
    elif unknown_paths:
        selected_lane = "aggregate"
        reason = "unknown-path"
    elif len(lanes_touched) > 1:
        selected_lane = "aggregate"
        reason = "multi-lane-diff"
    elif len(lanes_touched) == 1:
        selected_lane = lanes_touched[0]
        reason = "single-lane-diff"
    else:
        selected_lane = "aggregate"
        reason = "unknown-path"

    return ValidationLaneSelection(
        lane=selected_lane,
        reason=reason,
        normalized_paths=normalized_paths,
        lanes_touched=lanes_touched,
        shared_risk_paths=tuple(shared_risk_paths),
        unknown_paths=tuple(unknown_paths),
    )


def lane_selection_to_payload(selection: ValidationLaneSelection) -> dict[str, object]:
    return {
        "lane": selection.lane,
        "reason": selection.reason,
        "paths": list(selection.normalized_paths),
        "lanes_touched": list(selection.lanes_touched),
        "shared_risk_paths": list(selection.shared_risk_paths),
        "unknown_paths": list(selection.unknown_paths),
    }


def render_lane_selection(selection: ValidationLaneSelection) -> str:
    lines = [
        f"lane: {selection.lane}",
        f"reason: {selection.reason}",
        f"changed_files: {len(selection.normalized_paths)}",
    ]
    if selection.lanes_touched:
        lines.append(f"lanes_touched: {', '.join(selection.lanes_touched)}")
    if selection.shared_risk_paths:
        lines.append(f"shared_risk_paths: {', '.join(selection.shared_risk_paths)}")
    if selection.unknown_paths:
        lines.append(f"unknown_paths: {', '.join(selection.unknown_paths)}")
    return "\n".join(lines)


def _is_shared_risk_path(path: str) -> bool:
    if path in _SHARED_RISK_EXACT_PATHS:
        return True
    return any(_matches_prefix(path, prefix) for prefix in _SHARED_RISK_PREFIXES)


def _classify_scoped_lane(path: str) -> ScopedValidationLane | None:
    matches: list[ScopedValidationLane] = []
    for lane in _SCOPED_LANES:
        prefixes = _LANE_PREFIXES.get(lane, ())
        globs = _LANE_GLOBS.get(lane, ())
        if any(_matches_prefix(path, prefix) for prefix in prefixes) or any(
            fnmatch(path, pattern) for pattern in globs
        ):
            matches.append(lane)
    if len(matches) == 1:
        return matches[0]
    if not matches and fnmatch(path, "tests/test_*.py"):
        return "core"
    return None


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


__all__: Sequence[str] = (
    "ValidationLane",
    "ScopedValidationLane",
    "ValidationLaneSelection",
    "classify_changed_files",
    "lane_selection_to_payload",
    "normalize_changed_path",
    "render_lane_selection",
)
