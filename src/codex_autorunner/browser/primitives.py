from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from ..core.utils import atomic_write
from .artifacts import deterministic_artifact_name, reserve_artifact_path
from .models import Viewport

# CAR browser substrate v1 is deterministic and accessibility-first.
# These primitives stay transport-neutral (no Typer/MCP/Stagehand coupling)
# so future adapters can reuse the same observe/act/capture contracts.

_TEST_ID_RE = re.compile(r"data-testid=[\"']([^\"']+)[\"']", re.IGNORECASE)
_ARIA_LABEL_RE = re.compile(r"aria-label=[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class ObservePageResult:
    artifacts: dict[str, Path]
    skipped: dict[str, str]
    metadata: dict[str, Any]
    locator_refs: list[dict[str, Any]]


def _snapshot_unavailable_payload(reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "unavailable",
        "reason": reason,
    }


def _capture_accessibility_snapshot(page: Any) -> tuple[Any, bool]:
    accessibility = getattr(page, "accessibility", None)
    if accessibility is None or not hasattr(accessibility, "snapshot"):
        return (
            _snapshot_unavailable_payload(
                "page.accessibility.snapshot is not available for this page/runtime"
            ),
            False,
        )
    payload = accessibility.snapshot()
    if payload is None:
        return (
            _snapshot_unavailable_payload(
                "page.accessibility.snapshot() returned null"
            ),
            False,
        )
    return payload, True


def capture_artifact(
    *,
    out_dir: Path,
    kind: str,
    extension: str,
    writer: Callable[[Path], None],
    url: Optional[str] = None,
    path_hint: Optional[str] = None,
    output_name: Optional[str] = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = deterministic_artifact_name(
        kind=kind,
        extension=extension,
        url=url,
        path_hint=path_hint,
        output_name=output_name,
    )
    artifact_path, _collision = reserve_artifact_path(out_dir, filename)
    writer(artifact_path)
    return artifact_path


def observe_page(
    *,
    page: Any,
    out_dir: Path,
    target_url: str,
    path_hint: str,
    viewport: Viewport,
    output_name: Optional[str] = None,
    include_html: bool = True,
    max_html_bytes: int = 250_000,
) -> ObservePageResult:
    artifacts: dict[str, Path] = {}
    skipped: dict[str, str] = {}

    snapshot_payload, snapshot_available = _capture_accessibility_snapshot(page)

    current_url = str(getattr(page, "url", "") or target_url)
    title_text = ""
    if hasattr(page, "title"):
        title_text = str(page.title() or "")

    html_content = ""
    if include_html and hasattr(page, "content"):
        html_content = str(page.content() or "")

    locator_refs = extract_locator_refs(snapshot=snapshot_payload, html=html_content)

    snapshot_path = capture_artifact(
        out_dir=out_dir,
        kind="observe-a11y",
        extension="json",
        url=target_url,
        path_hint=path_hint,
        output_name=output_name,
        writer=lambda p: _write_json_payload(p, snapshot_payload),
    )
    artifacts["snapshot"] = snapshot_path

    metadata_payload = {
        "schema_version": 1,
        "target_url": target_url,
        "captured_url": current_url,
        "title": title_text,
        "viewport": {"width": viewport.width, "height": viewport.height},
        "snapshot_file": snapshot_path.name,
        "snapshot_status": "ok" if snapshot_available else "unavailable",
    }
    metadata_path = capture_artifact(
        out_dir=out_dir,
        kind="observe-meta",
        extension="json",
        url=target_url,
        path_hint=path_hint,
        writer=lambda p: _write_json_payload(p, metadata_payload),
    )
    artifacts["metadata"] = metadata_path

    locator_payload = {
        "schema_version": 1,
        "target_url": current_url,
        "count": len(locator_refs),
        "refs": locator_refs,
    }
    locator_refs_path = capture_artifact(
        out_dir=out_dir,
        kind="observe-locators",
        extension="json",
        url=target_url,
        path_hint=path_hint,
        writer=lambda p: _write_json_payload(p, locator_payload),
    )
    artifacts["locator_refs"] = locator_refs_path

    if include_html:
        html_size = len(html_content.encode("utf-8"))
        if html_size <= max_html_bytes:
            html_path = capture_artifact(
                out_dir=out_dir,
                kind="observe-dom",
                extension="html",
                url=target_url,
                path_hint=path_hint,
                writer=lambda p: atomic_write(p, html_content),
            )
            artifacts["html"] = html_path
        else:
            skipped["html"] = (
                f"Skipped HTML snapshot ({html_size} bytes > {max_html_bytes} bytes)."
            )

    run_manifest_payload = {
        "schema_version": 1,
        "mode": "observe",
        "runtime_profile": {
            "deterministic": True,
            "accessibility_first": True,
            "prompt_driven": False,
            "mcp_shape_compatible": True,
        },
        "generated_at_epoch_ms": int(time.time() * 1000),
        "target_url": target_url,
        "captured_url": current_url,
        "viewport": {"width": viewport.width, "height": viewport.height},
        "snapshot_status": "ok" if snapshot_available else "unavailable",
        "artifacts": {key: path.name for key, path in artifacts.items()},
        "skipped": skipped,
    }
    run_manifest_path = capture_artifact(
        out_dir=out_dir,
        kind="observe-run-manifest",
        extension="json",
        url=target_url,
        path_hint=path_hint,
        writer=lambda p: _write_json_payload(p, run_manifest_payload),
    )
    artifacts["run_manifest"] = run_manifest_path

    return ObservePageResult(
        artifacts=artifacts,
        skipped=skipped,
        metadata=metadata_payload,
        locator_refs=locator_refs,
    )


def act_step(
    *,
    page: Any,
    action: str,
    step_data: Mapping[str, Any],
    step_index: int,
    base_url: str,
    initial_path: str,
    out_dir: Path,
    timeout_ms: int,
) -> dict[str, Path]:
    step_timeout = _step_timeout_ms(step_data, timeout_ms)

    if action == "goto":
        target = _resolve_step_url(
            base_url,
            _require_non_empty_str(
                step_data, "url", step_index=step_index, action=action
            ),
            initial_path,
        )
        wait_until = step_data.get("wait_until")
        wait_value = (
            wait_until.strip()
            if isinstance(wait_until, str) and wait_until.strip()
            else "networkidle"
        )
        page.goto(target, timeout=step_timeout, wait_until=wait_value)
        return {}

    if action == "click":
        locator = _resolve_locator(page, step_data)
        locator.click(timeout=step_timeout)
        return {}

    if action == "fill":
        locator = _resolve_locator(page, step_data)
        value = _require_non_empty_str(
            step_data,
            "value",
            step_index=step_index,
            action=action,
        )
        locator.fill(value, timeout=step_timeout)
        return {}

    if action == "press":
        key = _require_non_empty_str(
            step_data,
            "key",
            step_index=step_index,
            action=action,
        )
        has_locator = any(
            isinstance(step_data.get(field), str) and step_data.get(field, "").strip()
            for field in ("role", "name", "label", "text", "test_id", "selector")
        )
        if has_locator:
            locator = _resolve_locator(page, step_data)
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
            _require_non_empty_str(
                step_data, "url", step_index=step_index, action=action
            ),
            initial_path,
        )
        page.wait_for_url(target, timeout=step_timeout)
        return {}

    if action == "wait_for_text":
        locator = _resolve_locator(page, step_data)
        locator.wait_for(state="visible", timeout=step_timeout)
        return {}

    if action == "wait_ms":
        ms_raw = step_data.get("ms", 0)
        ms = int(ms_raw) if isinstance(ms_raw, int) else 0
        if ms > step_timeout:
            raise ValueError(
                f"wait_ms step requested {ms}ms but timeout is {step_timeout}ms."
            )
        time.sleep(ms / 1000.0)
        return {}

    if action == "screenshot":
        output_raw = step_data.get("output")
        output_name = output_raw if isinstance(output_raw, str) else None
        full_page = bool(step_data.get("full_page", True))
        path = capture_artifact(
            out_dir=out_dir,
            kind=f"demo-step-{step_index:02d}-screenshot",
            extension="png",
            url=str(getattr(page, "url", "") or base_url),
            output_name=output_name,
            writer=lambda p: page.screenshot(path=str(p), full_page=full_page),
        )
        return {"screenshot": path}

    if action == "snapshot_a11y":
        output_raw = step_data.get("output")
        output_name = output_raw if isinstance(output_raw, str) else None
        payload, _snapshot_available = _capture_accessibility_snapshot(page)
        path = capture_artifact(
            out_dir=out_dir,
            kind=f"demo-step-{step_index:02d}-a11y",
            extension="json",
            url=str(getattr(page, "url", "") or base_url),
            output_name=output_name,
            writer=lambda p: _write_json_payload(p, payload),
        )
        return {"a11y_snapshot": path}

    raise ValueError(f"Unsupported action {action!r}")


def extract_locator_refs(
    *,
    snapshot: Any,
    html: str = "",
    max_refs: int = 128,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_ref(payload: dict[str, Any]) -> None:
        if len(refs) >= max_refs:
            return
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if key in seen:
            return
        seen.add(key)
        refs.append(payload)

    def walk_snapshot(node: Any) -> None:
        if not isinstance(node, dict) or len(refs) >= max_refs:
            return
        role = node.get("role")
        name = node.get("name")
        if isinstance(role, str) and role.strip():
            role_payload: dict[str, Any] = {
                "strategy": "role",
                "role": role.strip(),
                "source": "a11y",
            }
            if isinstance(name, str) and name.strip():
                role_payload["name"] = name.strip()
            add_ref(role_payload)

        if isinstance(name, str) and name.strip():
            add_ref({"strategy": "text", "text": name.strip(), "source": "a11y"})

        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                walk_snapshot(child)

    walk_snapshot(snapshot)

    if html:
        for match in _TEST_ID_RE.finditer(html):
            value = match.group(1).strip()
            if value:
                add_ref({"strategy": "test_id", "test_id": value, "source": "dom"})
        for match in _ARIA_LABEL_RE.finditer(html):
            value = match.group(1).strip()
            if value:
                add_ref({"strategy": "label", "label": value, "source": "dom"})

    return refs


def _write_json_payload(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _step_timeout_ms(step: Mapping[str, Any], default_timeout_ms: int) -> int:
    raw = step.get("timeout_ms")
    if raw is None:
        return default_timeout_ms
    if not isinstance(raw, int) or raw <= 0:
        raise ValueError("timeout_ms must be a positive integer.")
    return raw


def _require_non_empty_str(
    step: Mapping[str, Any],
    key: str,
    *,
    step_index: int,
    action: str,
) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Step {step_index} ({action}) requires non-empty '{key}'.")
    return value.strip()


def _resolve_locator(page: Any, step: Mapping[str, Any]) -> Any:
    exact = bool(step.get("exact", False))

    role = step.get("role")
    if isinstance(role, str) and role.strip():
        kwargs: dict[str, Any] = {}
        name = step.get("name")
        if isinstance(name, str) and name.strip():
            kwargs["name"] = name.strip()
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

    raise ValueError(
        "Step is missing locator fields. Provide role/name, label, text, test_id, or selector."
    )


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
