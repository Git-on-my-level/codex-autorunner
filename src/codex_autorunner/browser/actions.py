from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import yaml

from .artifacts import (
    deterministic_artifact_name,
    reserve_artifact_path,
    write_json_artifact,
)

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
            produced = _execute_step(
                page=page,
                step=step,
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
        _require_str(step, "url", index=index, action=action)
        return
    if action == "click":
        _require_locator(step, index=index, action=action)
        return
    if action == "fill":
        _require_locator(step, index=index, action=action)
        _require_str(step, "value", index=index, action=action)
        return
    if action == "press":
        _require_str(step, "key", index=index, action=action)
        return
    if action == "wait_for_url":
        _require_str(step, "url", index=index, action=action)
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


def _require_str(step: dict[str, Any], key: str, *, index: int, action: str) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Step {index} ({action}) requires non-empty '{key}'.")
    return value


def _require_locator(step: dict[str, Any], *, index: int, action: str) -> None:
    locator_keys = ("role", "name", "label", "text", "test_id", "selector")
    has_locator = False
    for key in locator_keys:
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            has_locator = True
            break

    if not has_locator:
        keys = ", ".join(locator_keys)
        raise ValueError(
            f"Step {index} ({action}) requires a locator. " f"Provide one of: {keys}."
        )


def _step_timeout_ms(step: dict[str, Any], default_timeout_ms: int) -> int:
    raw = step.get("timeout_ms")
    if raw is None:
        return default_timeout_ms
    if not isinstance(raw, int) or raw <= 0:
        raise ValueError("timeout_ms must be a positive integer.")
    return raw


def _resolve_locator(page: Any, step: dict[str, Any]) -> Any:
    exact = bool(step.get("exact", False))

    role = step.get("role")
    if isinstance(role, str) and role.strip():
        kwargs: dict[str, Any] = {}
        name = step.get("name")
        if isinstance(name, str) and name.strip():
            kwargs["name"] = name
            kwargs["exact"] = exact
        return page.get_by_role(role.strip(), **kwargs)

    label = step.get("label")
    if isinstance(label, str) and label.strip():
        return page.get_by_label(label.strip(), exact=exact)

    text = step.get("text")
    if isinstance(text, str) and text.strip():
        return page.get_by_text(text.strip(), exact=exact)

    test_id = step.get("test_id")
    if isinstance(test_id, str) and test_id.strip():
        return page.get_by_test_id(test_id.strip())

    selector = step.get("selector")
    if isinstance(selector, str) and selector.strip():
        return page.locator(selector.strip())

    raise ValueError("Step is missing locator fields.")


def _resolve_step_url(base_url: str, raw_url: str, initial_path: str) -> str:
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    if raw_url.startswith("/"):
        return _build_navigation_url(base_url, raw_url)
    if raw_url.strip() == "":
        return _build_navigation_url(base_url, initial_path)
    return _build_navigation_url(base_url, f"/{raw_url}")


def _build_navigation_url(base_url: str, path: str = "/") -> str:
    normalized_base = base_url.strip()
    normalized_path = (path or "/").strip() or "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    base = urlsplit(normalized_base)
    override = urlsplit(normalized_path)
    return urlunsplit(
        (
            base.scheme,
            base.netloc,
            override.path or "/",
            override.query,
            override.fragment,
        )
    )


def _execute_step(
    *,
    page: Any,
    step: DemoStep,
    step_index: int,
    base_url: str,
    initial_path: str,
    out_dir: Path,
    timeout_ms: int,
) -> dict[str, Path]:
    action = step.action
    data = step.data
    step_timeout = _step_timeout_ms(data, timeout_ms)

    if action == "goto":
        target = _resolve_step_url(
            base_url,
            _require_str(data, "url", index=step_index, action=action),
            initial_path,
        )
        wait_until = data.get("wait_until")
        wait_value = wait_until if isinstance(wait_until, str) and wait_until else None
        page.goto(target, timeout=step_timeout, wait_until=wait_value or "networkidle")
        return {}

    if action == "click":
        locator = _resolve_locator(page, data)
        locator.click(timeout=step_timeout)
        return {}

    if action == "fill":
        locator = _resolve_locator(page, data)
        locator.fill(
            _require_str(data, "value", index=step_index, action=action),
            timeout=step_timeout,
        )
        return {}

    if action == "press":
        key = _require_str(data, "key", index=step_index, action=action)
        has_locator = False
        for field in ("role", "name", "label", "text", "test_id", "selector"):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                has_locator = True
                break
        if has_locator:
            locator = _resolve_locator(page, data)
            locator.press(key, timeout=step_timeout)
        else:
            keyboard = getattr(page, "keyboard", None)
            if keyboard is None or not hasattr(keyboard, "press"):
                raise ValueError(
                    "Page keyboard interface is unavailable for press step."
                )
            keyboard.press(key)
        return {}

    if action == "wait_for_url":
        target = _resolve_step_url(
            base_url,
            _require_str(data, "url", index=step_index, action=action),
            initial_path,
        )
        page.wait_for_url(target, timeout=step_timeout)
        return {}

    if action == "wait_for_text":
        locator = _resolve_locator(page, data)
        locator.wait_for(state="visible", timeout=step_timeout)
        return {}

    if action == "wait_ms":
        ms = int(data.get("ms", 0))
        time.sleep(ms / 1000.0)
        return {}

    if action == "screenshot":
        output_name = data.get("output")
        explicit_name = output_name if isinstance(output_name, str) else None
        filename = deterministic_artifact_name(
            kind=f"demo-step-{step_index:02d}-screenshot",
            extension="png",
            url=getattr(page, "url", None),
            output_name=explicit_name,
        )
        target_path, _collision = reserve_artifact_path(out_dir, filename)
        full_page = data.get("full_page")
        full_page_flag = bool(full_page) if full_page is not None else True
        page.screenshot(path=str(target_path), full_page=full_page_flag)
        return {"screenshot": target_path}

    if action == "snapshot_a11y":
        output_name = data.get("output")
        explicit_name = output_name if isinstance(output_name, str) else None
        filename = deterministic_artifact_name(
            kind=f"demo-step-{step_index:02d}-a11y",
            extension="json",
            url=getattr(page, "url", None),
            output_name=explicit_name,
        )
        accessibility = getattr(page, "accessibility", None)
        payload = None
        if accessibility is not None and hasattr(accessibility, "snapshot"):
            payload = accessibility.snapshot()
        result = write_json_artifact(
            out_dir=out_dir, filename=filename, payload=payload
        )
        return {"a11y_snapshot": result.path}

    raise ValueError(f"Unsupported action {action!r}")
