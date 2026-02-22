from __future__ import annotations

import logging

import pytest

from codex_autorunner.integrations.chat.bootstrap import (
    ChatBootstrapStep,
    run_chat_bootstrap_steps,
)


@pytest.mark.anyio
async def test_run_chat_bootstrap_steps_continues_on_optional_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    async def _optional_fail() -> None:
        calls.append("optional")
        raise RuntimeError("optional failure")

    async def _required_ok() -> None:
        calls.append("required")

    logger = logging.getLogger("test.chat.bootstrap")
    with caplog.at_level(logging.INFO):
        await run_chat_bootstrap_steps(
            platform="chat",
            logger=logger,
            steps=(
                ChatBootstrapStep(
                    name="optional_step",
                    action=_optional_fail,
                    required=False,
                ),
                ChatBootstrapStep(
                    name="required_step",
                    action=_required_ok,
                    required=True,
                ),
            ),
        )

    assert calls == ["optional", "required"]
    assert "chat.bootstrap.step_failed" in caplog.text
    assert "chat.bootstrap.step_ok" in caplog.text


@pytest.mark.anyio
async def test_run_chat_bootstrap_steps_raises_on_required_failure() -> None:
    calls: list[str] = []

    async def _required_fail() -> None:
        calls.append("required_fail")
        raise RuntimeError("required failure")

    async def _never_runs() -> None:
        calls.append("never")

    with pytest.raises(RuntimeError, match="required failure"):
        await run_chat_bootstrap_steps(
            platform="chat",
            logger=logging.getLogger("test.chat.bootstrap"),
            steps=(
                ChatBootstrapStep(
                    name="required_fail",
                    action=_required_fail,
                    required=True,
                ),
                ChatBootstrapStep(
                    name="never_runs",
                    action=_never_runs,
                    required=True,
                ),
            ),
        )

    assert calls == ["required_fail"]
