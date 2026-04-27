from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

import yaml

from .paths import validate_app_glob, validate_app_path
from .refs import validate_app_id

_SUPPORTED_SCHEMA_VERSIONS = {1}
_SUPPORTED_HOOK_POINTS = {
    "after_ticket_done",
    "after_flow_terminal",
    "before_chat_wrapup",
}
_SUPPORTED_OUTPUT_KINDS = {"image", "markdown", "text", "json", "html"}
_SUPPORTED_HOOK_FAILURE_MODES = {"warn", "pause", "fail"}


class ManifestError(Exception):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


@dataclasses.dataclass(frozen=True)
class AppTemplate:
    path: str
    description: str = ""


@dataclasses.dataclass(frozen=True)
class AppOutput:
    kind: str
    path: str
    label: str = ""


@dataclasses.dataclass(frozen=True)
class AppTool:
    id: str
    description: str = ""
    argv: List[str] = dataclasses.field(default_factory=list)
    timeout_seconds: int = 60
    outputs: List[AppOutput] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class AppInput:
    required: bool = False
    description: str = ""


@dataclasses.dataclass(frozen=True)
class AppHookEntry:
    tool: Optional[str] = None
    when: Dict[str, Any] = dataclasses.field(default_factory=dict)
    failure: str = "warn"
    artifacts: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class AppHook:
    point: str
    entries: List[AppHookEntry] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class AppPermission:
    network: bool = False
    writes: List[str] = dataclasses.field(default_factory=list)
    reads: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class AppManifest:
    schema_version: int
    id: str
    name: str
    version: str
    description: str = ""
    entrypoint: Optional[AppTemplate] = None
    inputs: Dict[str, AppInput] = dataclasses.field(default_factory=dict)
    templates: Dict[str, AppTemplate] = dataclasses.field(default_factory=dict)
    tools: Dict[str, AppTool] = dataclasses.field(default_factory=dict)
    hooks: List[AppHook] = dataclasses.field(default_factory=list)
    permissions: AppPermission = dataclasses.field(default_factory=AppPermission)


def _require_dict(data: Any, field: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError(f"{field} must be a mapping", field=field)
    return data


def _require_str(data: Any, field: str) -> str:
    if not isinstance(data, str):
        raise ManifestError(f"{field} must be a string", field=field)
    return data


def _require_int(data: Any, field: str) -> int:
    if not isinstance(data, int):
        raise ManifestError(f"{field} must be an integer", field=field)
    return data


def _require_list(data: Any, field: str) -> List[Any]:
    if not isinstance(data, list):
        raise ManifestError(f"{field} must be a list", field=field)
    return data


def _parse_template(
    data: Dict[str, Any], context: str, *, path_key: str = "path"
) -> AppTemplate:
    path_raw = _require_str(data.get(path_key, ""), f"{context}.{path_key}")
    validate_app_path(path_raw)
    return AppTemplate(
        path=str(PurePosixPath(path_raw)),
        description=_require_str(data.get("description", ""), f"{context}.description"),
    )


def _parse_output(data: Dict[str, Any], context: str) -> AppOutput:
    kind = _require_str(data.get("kind", ""), f"{context}.kind")
    if kind not in _SUPPORTED_OUTPUT_KINDS:
        raise ManifestError(
            f"{context}.kind must be one of {sorted(_SUPPORTED_OUTPUT_KINDS)}",
            field=f"{context}.kind",
        )
    path_raw = _require_str(data.get("path", ""), f"{context}.path")
    validate_app_path(path_raw)
    return AppOutput(
        kind=kind,
        path=str(PurePosixPath(path_raw)),
        label=_require_str(data.get("label", ""), f"{context}.label"),
    )


def _parse_tool(tool_id: str, data: Dict[str, Any], context: str) -> AppTool:
    argv = _require_list(data.get("argv", []), f"{context}.argv")
    if not argv:
        raise ManifestError(
            f"{context}.argv must be a non-empty list", field=f"{context}.argv"
        )
    for i, arg in enumerate(argv):
        if not isinstance(arg, str):
            raise ManifestError(
                f"{context}.argv[{i}] must be a string",
                field=f"{context}.argv[{i}]",
            )

    outputs: List[AppOutput] = []
    for i, out_data in enumerate(
        _require_list(data.get("outputs", []), f"{context}.outputs")
    ):
        outputs.append(
            _parse_output(
                _require_dict(out_data, f"{context}.outputs[{i}]"),
                f"{context}.outputs[{i}]",
            )
        )

    return AppTool(
        id=tool_id,
        description=_require_str(data.get("description", ""), f"{context}.description"),
        argv=list(argv),
        timeout_seconds=data.get("timeout_seconds", 60),
        outputs=outputs,
    )


def _parse_hook_entry(
    data: Dict[str, Any], context: str, declared_tools: Dict[str, AppTool]
) -> AppHookEntry:
    tool = data.get("tool")
    if tool is not None:
        tool = _require_str(tool, f"{context}.tool")
        if tool not in declared_tools:
            raise ManifestError(
                f"{context}.tool references unknown tool {tool!r}",
                field=f"{context}.tool",
            )

    failure = _require_str(data.get("failure", "warn"), f"{context}.failure")
    if failure not in _SUPPORTED_HOOK_FAILURE_MODES:
        raise ManifestError(
            f"{context}.failure must be one of {sorted(_SUPPORTED_HOOK_FAILURE_MODES)}",
            field=f"{context}.failure",
        )

    artifacts: List[str] = []
    for i, art in enumerate(
        _require_list(data.get("artifacts", []), f"{context}.artifacts")
    ):
        art_str = _require_str(art, f"{context}.artifacts[{i}]")
        validate_app_path(art_str)
        artifacts.append(str(PurePosixPath(art_str)))

    return AppHookEntry(
        tool=tool,
        when=data.get("when", {}),
        failure=failure,
        artifacts=artifacts,
    )


def _parse_hook(
    point: str,
    entries_data: List[Any],
    context: str,
    declared_tools: Dict[str, AppTool],
) -> AppHook:
    if point not in _SUPPORTED_HOOK_POINTS:
        raise ManifestError(
            f"unknown hook point {point!r} "
            f"(supported: {sorted(_SUPPORTED_HOOK_POINTS)})",
            field=f"{context}.point",
        )

    parsed_entries: List[AppHookEntry] = []
    for i, entry in enumerate(entries_data):
        parsed_entries.append(
            _parse_hook_entry(
                _require_dict(entry, f"{context}[{i}]"),
                f"{context}[{i}]",
                declared_tools,
            )
        )
    return AppHook(point=point, entries=parsed_entries)


def _parse_permissions(data: Dict[str, Any], context: str) -> AppPermission:
    writes: List[str] = []
    for i, w in enumerate(_require_list(data.get("writes", []), f"{context}.writes")):
        w_str = _require_str(w, f"{context}.writes[{i}]")
        validate_app_glob(w_str)
        writes.append(w_str)

    reads: List[str] = []
    for i, r in enumerate(_require_list(data.get("reads", []), f"{context}.reads")):
        r_str = _require_str(r, f"{context}.reads[{i}]")
        validate_app_glob(r_str)
        reads.append(r_str)

    return AppPermission(
        network=bool(data.get("network", False)),
        writes=writes,
        reads=reads,
    )


def parse_app_manifest(raw: dict[str, Any]) -> AppManifest:
    sv = raw.get("schema_version")
    if sv not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError(
            f"unsupported schema_version: {sv!r} "
            f"(supported: {sorted(_SUPPORTED_SCHEMA_VERSIONS)})",
            field="schema_version",
        )

    app_id = _require_str(raw.get("id"), "id")
    try:
        validate_app_id(app_id)
    except ValueError as exc:
        raise ManifestError(str(exc), field="id") from exc

    name = _require_str(raw.get("name"), "name")
    if not name.strip():
        raise ManifestError("name must not be empty", field="name")

    version = _require_str(raw.get("version"), "version")
    if not version.strip():
        raise ManifestError("version must not be empty", field="version")

    description = _require_str(raw.get("description", ""), "description")

    entrypoint: Optional[AppTemplate] = None
    if "entrypoint" in raw and raw["entrypoint"] is not None:
        ep_data = _require_dict(raw["entrypoint"], "entrypoint")
        entrypoint = _parse_template(ep_data, "entrypoint", path_key="template")

    inputs: Dict[str, AppInput] = {}
    for key, val in _require_dict(raw.get("inputs", {}), "inputs").items():
        if not isinstance(val, dict):
            raise ManifestError(
                f"inputs.{key} must be a mapping", field=f"inputs.{key}"
            )
        inputs[key] = AppInput(
            required=bool(val.get("required", False)),
            description=_require_str(
                val.get("description", ""), f"inputs.{key}.description"
            ),
        )

    templates: Dict[str, AppTemplate] = {}
    for key, val in _require_dict(raw.get("templates", {}), "templates").items():
        if not isinstance(val, dict):
            raise ManifestError(
                f"templates.{key} must be a mapping", field=f"templates.{key}"
            )
        templates[key] = _parse_template(val, f"templates.{key}")

    tools: Dict[str, AppTool] = {}
    for tool_id, val in _require_dict(raw.get("tools", {}), "tools").items():
        if not isinstance(val, dict):
            raise ManifestError(
                f"tools.{tool_id} must be a mapping", field=f"tools.{tool_id}"
            )
        tools[tool_id] = _parse_tool(tool_id, val, f"tools.{tool_id}")

    hooks: List[AppHook] = []
    for point, entries_data in _require_dict(raw.get("hooks", {}), "hooks").items():
        hooks.append(
            _parse_hook(
                point,
                _require_list(entries_data, f"hooks.{point}"),
                f"hooks.{point}",
                tools,
            )
        )

    permissions = AppPermission()
    if "permissions" in raw and raw["permissions"] is not None:
        permissions = _parse_permissions(
            _require_dict(raw["permissions"], "permissions"),
            "permissions",
        )

    return AppManifest(
        schema_version=sv,
        id=app_id,
        name=name,
        version=version,
        description=description,
        entrypoint=entrypoint,
        inputs=inputs,
        templates=templates,
        tools=tools,
        hooks=hooks,
        permissions=permissions,
    )


def load_app_manifest(path: Path) -> AppManifest:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a YAML mapping")
    return parse_app_manifest(data)
