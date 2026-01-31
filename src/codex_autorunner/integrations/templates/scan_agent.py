from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ...core.config import RepoConfig, load_hub_config
from ...core.ports.backend_orchestrator import BackendOrchestrator
from ...core.ports.run_event import Completed, Failed, OutputDelta, RunEvent
from ...core.prompts import TEMPLATE_SCAN_PROMPT
from ...core.templates import FetchedTemplate, TemplateScanRecord, write_scan_record

_FORMAT_ERROR_HINT = (
    "FORMAT ERROR: Output must be EXACTLY ONE LINE of JSON in the specified schema. "
    "Do not include markdown, code fences, or extra text."
)

_APPROVE_TOOL = "template_scan_approve"
_REJECT_TOOL = "template_scan_reject"
_ALLOWED_TOOLS = {_APPROVE_TOOL, _REJECT_TOOL}
_APPROVE_SEVERITIES = {"low", "medium"}
_REJECT_SEVERITIES = {"high"}


class TemplateScanError(RuntimeError):
    pass


class TemplateScanFormatError(TemplateScanError):
    pass


class TemplateScanRejectedError(TemplateScanError):
    def __init__(self, record: TemplateScanRecord, message: str) -> None:
        super().__init__(message)
        self.record = record


class TemplateScanBackendError(TemplateScanError):
    pass


@dataclasses.dataclass(frozen=True)
class TemplateScanDecision:
    tool: str
    blob_sha: str
    severity: str
    reason: str
    evidence: Optional[list[str]]


def build_template_scan_prompt(template: FetchedTemplate) -> str:
    prompt = TEMPLATE_SCAN_PROMPT
    replacements = {
        "repo_id": template.repo_id,
        "repo_url": template.url,
        "trusted_repo": str(template.trusted),
        "path": template.path,
        "ref": template.ref,
        "commit_sha": template.commit_sha,
        "blob_sha": template.blob_sha,
        "template_content": template.content,
    }
    for key, value in replacements.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", str(value))
    return prompt


def _extract_output_text(events: Iterable[RunEvent]) -> str:
    deltas: list[str] = []
    final_message: Optional[str] = None
    for event in events:
        if isinstance(event, Completed):
            final_message = event.final_message
            continue
        if isinstance(event, OutputDelta) and event.delta_type in {
            "assistant_message",
            "assistant_stream",
        }:
            if event.content:
                deltas.append(event.content)
    if final_message:
        return final_message
    if deltas:
        return "".join(deltas)
    return ""


def _normalize_line(text: str) -> str:
    line = text.strip()
    if "\n" in line or "\r" in line:
        raise TemplateScanFormatError("Output must be a single line")
    return line


def _parse_json_line(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplateScanFormatError(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TemplateScanFormatError("Output JSON must be an object")
    return payload


def _coerce_evidence(value: Any) -> Optional[list[str]]:
    if value is None or value == []:
        return None
    if isinstance(value, list):
        evidence = [str(item) for item in value][:3]
        return evidence or None
    return [str(value)]


def parse_template_scan_output(
    raw: str, *, expected_blob_sha: str
) -> TemplateScanDecision:
    line = _normalize_line(raw)
    if not line:
        raise TemplateScanFormatError("Empty output from scan agent")

    payload = _parse_json_line(line)
    tool = payload.get("tool")
    blob_sha = payload.get("blob_sha")
    severity = payload.get("severity")
    reason = payload.get("reason")
    evidence = _coerce_evidence(payload.get("evidence"))

    if tool not in _ALLOWED_TOOLS:
        raise TemplateScanFormatError("Missing or invalid tool field")
    if not isinstance(blob_sha, str) or not blob_sha:
        raise TemplateScanFormatError("Missing blob_sha field")
    if blob_sha != expected_blob_sha:
        raise TemplateScanFormatError("blob_sha mismatch")
    if not isinstance(severity, str) or not severity:
        raise TemplateScanFormatError("Missing severity field")
    if not isinstance(reason, str) or not reason:
        raise TemplateScanFormatError("Missing reason field")

    if tool == _APPROVE_TOOL and severity not in _APPROVE_SEVERITIES:
        raise TemplateScanFormatError("Invalid severity for approve decision")
    if tool == _REJECT_TOOL and severity not in _REJECT_SEVERITIES:
        raise TemplateScanFormatError("Invalid severity for reject decision")

    return TemplateScanDecision(
        tool=tool,
        blob_sha=blob_sha,
        severity=severity,
        reason=reason,
        evidence=evidence,
    )


def _decision_to_record(
    template: FetchedTemplate,
    decision: TemplateScanDecision,
    *,
    scanner: Optional[dict[str, str]] = None,
) -> TemplateScanRecord:
    scanned_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return TemplateScanRecord(
        blob_sha=template.blob_sha,
        repo_id=template.repo_id,
        path=template.path,
        ref=template.ref,
        commit_sha=template.commit_sha,
        trusted=template.trusted,
        decision="approve" if decision.tool == _APPROVE_TOOL else "reject",
        severity=decision.severity,
        reason=decision.reason,
        evidence=decision.evidence,
        scanned_at=scanned_at,
        scanner=scanner,
    )


def _scanner_metadata(config: Optional[RepoConfig]) -> Optional[dict[str, str]]:
    if config is None:
        return None
    model = config.codex_model or ""
    reasoning = config.codex_reasoning or ""
    metadata: dict[str, str] = {"agent": "codex"}
    if model:
        metadata["model"] = model
    if reasoning:
        metadata["reasoning"] = reasoning
    return metadata or None


def format_template_scan_rejection(record: TemplateScanRecord) -> str:
    lines = [
        "Template scan rejected this template.",
        f"repo_id={record.repo_id}",
        f"path={record.path}",
        f"ref={record.ref}",
        f"commit_sha={record.commit_sha}",
        f"blob_sha={record.blob_sha}",
        f"reason={record.reason}",
        "Pause and notify the user; do not continue with this template.",
    ]
    if record.evidence:
        lines.append("evidence:")
        lines.extend([f"- {item}" for item in record.evidence])
    return "\n".join(lines)


async def run_template_scan_with_orchestrator(
    *,
    config: RepoConfig,
    backend_orchestrator: BackendOrchestrator,
    template: FetchedTemplate,
    hub_root: Any,
    max_attempts: int = 2,
) -> TemplateScanRecord:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    prompt = build_template_scan_prompt(template)
    state = _build_scan_state()
    scanner = _scanner_metadata(config)
    last_error: Optional[str] = None

    for attempt in range(max_attempts):
        if attempt > 0:
            prompt = f"{_FORMAT_ERROR_HINT}\n\n{prompt}"
        events: list[RunEvent] = []
        try:
            async for event in backend_orchestrator.run_turn(
                agent_id="codex",
                state=state,
                prompt=prompt,
                model=config.codex_model,
                reasoning=config.codex_reasoning,
                session_key=f"template_scan:{template.blob_sha}:{attempt}",
            ):
                events.append(event)
        except Exception as exc:
            raise TemplateScanBackendError(str(exc)) from exc

        for event in events:
            if isinstance(event, Failed):
                raise TemplateScanBackendError(event.error_message)

        output = _extract_output_text(events)
        if not output:
            last_error = "Empty scan output"
            continue
        try:
            decision = parse_template_scan_output(
                output, expected_blob_sha=template.blob_sha
            )
        except TemplateScanFormatError as exc:
            last_error = str(exc)
            continue

        record = _decision_to_record(template, decision, scanner=scanner)
        write_scan_record(record, hub_root)
        if record.decision == "reject":
            raise TemplateScanRejectedError(
                record, format_template_scan_rejection(record)
            )
        return record

    raise TemplateScanFormatError(last_error or "Scan output failed validation")


async def run_template_scan(
    *,
    ctx: Any,
    template: FetchedTemplate,
    max_attempts: int = 2,
) -> TemplateScanRecord:
    backend_orchestrator = getattr(ctx, "_backend_orchestrator", None)
    if backend_orchestrator is None:
        raise TemplateScanBackendError(
            "Template scanning requires a backend orchestrator; configure Codex or OpenCode."
        )

    config = ctx.config
    try:
        hub_root = load_hub_config(config.root).root
    except Exception:
        hub_root = getattr(config, "root", None)
    if hub_root is None:
        raise TemplateScanBackendError("Missing hub root for scan cache writes")

    return await run_template_scan_with_orchestrator(
        config=config,
        backend_orchestrator=backend_orchestrator,
        template=template,
        hub_root=hub_root,
        max_attempts=max_attempts,
    )


def _build_scan_state() -> Any:
    from ...core.state import RunnerState

    return RunnerState(
        last_run_id=None,
        status="running",
        last_exit_code=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        autorunner_agent_override="codex",
        autorunner_model_override=None,
        autorunner_effort_override=None,
        autorunner_approval_policy="never",
        autorunner_sandbox_mode="readOnly",
        autorunner_workspace_write_network=False,
    )
