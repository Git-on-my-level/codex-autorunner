from __future__ import annotations

import dataclasses
from typing import Any, Optional

import pytest

from codex_autorunner.core.config import load_repo_config
from codex_autorunner.core.ports.run_event import Completed
from codex_autorunner.core.templates import FetchedTemplate, get_scan_record
from codex_autorunner.integrations.templates.scan_agent import (
    TemplateScanFormatError,
    TemplateScanRejectedError,
    parse_template_scan_output,
    run_template_scan_with_orchestrator,
)


class FakeOrchestrator:
    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def run_turn(
        self,
        *,
        agent_id: str,
        state: Any,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        session_key: str,
    ):
        _ = agent_id, state, model, reasoning, session_key
        self.prompts.append(prompt)
        if not self._outputs:
            raise RuntimeError("No outputs configured")
        output = self._outputs.pop(0)
        yield Completed(timestamp="2026-01-31T00:00:00Z", final_message=output)


def _template(blob_sha: str) -> FetchedTemplate:
    return FetchedTemplate(
        repo_id="blessed",
        url="https://example.com/templates",
        trusted=False,
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="deadbeef",
        blob_sha=blob_sha,
        content="Do the thing.",
    )


def test_parse_template_scan_output_accepts_valid_json() -> None:
    raw = '{"tool":"template_scan_approve","blob_sha":"abc123","severity":"low","reason":"ok"}'
    decision = parse_template_scan_output(raw, expected_blob_sha="abc123")
    assert decision.tool == "template_scan_approve"
    assert decision.severity == "low"
    assert decision.reason == "ok"
    assert decision.evidence is None


def test_parse_template_scan_output_rejects_multiline() -> None:
    raw = (
        '{"tool":"template_scan_approve","blob_sha":"abc123","severity":"low","reason":"ok"}\n'
        "extra"
    )
    with pytest.raises(TemplateScanFormatError):
        parse_template_scan_output(raw, expected_blob_sha="abc123")


@pytest.mark.asyncio
async def test_scan_retries_on_format_error(hub_env) -> None:
    config = load_repo_config(hub_env.repo_root)
    template = _template("abc123")
    outputs = [
        "not json",
        '{"tool":"template_scan_approve","blob_sha":"abc123","severity":"low","reason":"safe"}',
    ]
    backend = FakeOrchestrator(outputs)

    record = await run_template_scan_with_orchestrator(
        config=config,
        backend_orchestrator=backend,
        template=template,
        hub_root=hub_env.hub_root,
        max_attempts=2,
    )

    assert record.decision == "approve"
    assert len(backend.prompts) == 2
    assert "FORMAT ERROR" in backend.prompts[1]
    cached = get_scan_record(hub_env.hub_root, template.blob_sha)
    assert cached == record


@pytest.mark.asyncio
async def test_scan_rejection_is_cached(hub_env) -> None:
    config = load_repo_config(hub_env.repo_root)
    template = _template("beef123")
    outputs = [
        '{"tool":"template_scan_reject","blob_sha":"beef123","severity":"high","reason":"bad","evidence":["x"]}'
    ]
    backend = FakeOrchestrator(outputs)

    with pytest.raises(TemplateScanRejectedError) as exc:
        await run_template_scan_with_orchestrator(
            config=config,
            backend_orchestrator=backend,
            template=template,
            hub_root=hub_env.hub_root,
        )

    cached = get_scan_record(hub_env.hub_root, template.blob_sha)
    assert cached is not None
    assert cached.decision == "reject"
    assert cached.evidence is None
    assert cached == dataclasses.replace(exc.value.record, evidence=None)
