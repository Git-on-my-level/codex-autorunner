from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .primitives import act_step

SUPPORTED_V1_ACTIONS = {
    "goto",
    "click",
    "fill",
    "press",
    "wait_for_url",
    "wait_for_text",
    "wait_ms",
    "screenshot",
    "snapshot_a11y",
}


@dataclass(frozen=True)
class DemoStep:
    action: str
    data: Dict[str, Any]


@dataclass(frozen=True)
class DemoManifest:
    version: int
    steps: list[DemoStep]


@dataclass(frozen=True)
class DemoStepReport:
    index: int
    action: str
    ok: bool
    error: Optional[str] = None
    artifacts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DemoExecutionResult:
    ok: bool
    steps: list[DemoStepReport]
    artifacts: Dict[str, Path]
    error_message: Optional[str] = None
    failed_step_index: Optional[int] = None


def load_demo_manifest(path: Path) -> DemoManifest:
    if not path.exists():
        raise ValueError(f"Demo manifest not found: {path}")
    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        raise ValueError("Demo manifest is empty.")

    payload: Any
    if path.suffix.lower() == ".json":
        payload = json.loads(raw_text)
    else:
        payload = yaml.safe_load(raw_text)

    if not isinstance(payload, dict):
        raise ValueError("Demo manifest root must be a mapping.")

    version = payload.get("version")
    if version != 1:
        raise ValueError(
            f"Unsupported demo manifest version: {version!r}. Expected version: 1."
        )

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("Demo manifest must include a 'steps' list.")

    steps: list[DemoStep] = []
    for idx, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"Step {idx} must be a mapping.")
        action_raw = raw_step.get("action")
        if not isinstance(action_raw, str) or not action_raw.strip():
            raise ValueError(f"Step {idx} is missing a valid 'action'.")
        action = action_raw.strip()
        if action not in SUPPORTED_V1_ACTIONS:
            supported = ", ".join(sorted(SUPPORTED_V1_ACTIONS))
            raise ValueError(
                f"Unsupported step action '{action}' at step {idx}. "
                f"Supported actions: {supported}."
            )
        _validate_step(idx, action, raw_step)
        steps.append(DemoStep(action=action, data=dict(raw_step)))

    return DemoManifest(version=1, steps=steps)


def execute_demo_manifest(
    *,
    page: Any,
    manifest: DemoManifest,
    base_url: str,
    initial_path: str,
    out_dir: Path,
    timeout_ms: int = 30000,
) -> DemoExecutionResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, Path] = {}
    reports: list[DemoStepReport] = []

    for idx, step in enumerate(manifest.steps, start=1):
        step_artifacts: list[str] = []
        try:
            produced = act_step(
                page=page,
                action=step.action,
                step_data=step.data,
                step_index=idx,
                base_url=base_url,
                initial_path=initial_path,
                out_dir=out_dir,
                timeout_ms=timeout_ms,
            )
            for key, path in produced.items():
                artifact_key = f"step_{idx}.{key}"
                artifacts[artifact_key] = path
                step_artifacts.append(str(path))
            reports.append(
                DemoStepReport(
                    index=idx,
                    action=step.action,
                    ok=True,
                    artifacts=step_artifacts,
                )
            )
        except Exception as exc:
            message = str(exc).strip() or repr(exc)
            reports.append(
                DemoStepReport(
                    index=idx,
                    action=step.action,
                    ok=False,
                    error=message,
                    artifacts=step_artifacts,
                )
            )
            return DemoExecutionResult(
                ok=False,
                steps=reports,
                artifacts=artifacts,
                error_message=f"Step {idx} ({step.action}) failed: {message}",
                failed_step_index=idx,
            )

    return DemoExecutionResult(ok=True, steps=reports, artifacts=artifacts)


def _validate_step(index: int, action: str, step: dict[str, Any]) -> None:
    if action == "goto":
        _require_non_empty_str(step, "url", index=index, action=action)
        return
    if action == "click":
        _require_locator(step, index=index, action=action)
        return
    if action == "fill":
        _require_locator(step, index=index, action=action)
        _require_non_empty_str(step, "value", index=index, action=action)
        return
    if action == "press":
        _require_non_empty_str(step, "key", index=index, action=action)
        return
    if action == "wait_for_url":
        _require_non_empty_str(step, "url", index=index, action=action)
        return
    if action == "wait_for_text":
        _require_locator(step, index=index, action=action)
        return
    if action == "wait_ms":
        ms = step.get("ms")
        if not isinstance(ms, int) or ms < 0:
            raise ValueError(
                f"Step {index} ({action}) requires non-negative integer 'ms'."
            )
        return
    if action in {"screenshot", "snapshot_a11y"}:
        return


def _require_non_empty_str(
    step: dict[str, Any],
    key: str,
    *,
    index: int,
    action: str,
) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Step {index} ({action}) requires non-empty '{key}'.")
    return value


def _require_locator(step: dict[str, Any], *, index: int, action: str) -> None:
    locator_keys = ("role", "name", "label", "text", "test_id", "selector")
    for key in locator_keys:
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return

    keys = ", ".join(locator_keys)
    raise ValueError(
        f"Step {index} ({action}) requires a locator. Provide one of: {keys}."
    )
