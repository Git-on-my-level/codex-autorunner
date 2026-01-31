import sys

import pytest

from codex_autorunner.codex_runner import (
    CodexTimeoutError,
    build_codex_command,
    run_codex_capture_async,
)
from codex_autorunner.core.config import ConfigError
from codex_autorunner.core.runtime import RuntimeContext


def test_build_codex_command_missing_binary(repo):
    engine = RuntimeContext(repo)
    engine.config.codex_binary = "codex-missing-binary"
    with pytest.raises(ConfigError):
        build_codex_command(engine.config, "hello")


@pytest.mark.anyio
async def test_run_codex_capture_async_times_out(repo):
    engine = RuntimeContext(repo)
    engine.config.codex_binary = sys.executable
    engine.config.codex_args = ["-c", "import time; time.sleep(1)"]
    with pytest.raises(CodexTimeoutError):
        await run_codex_capture_async(
            engine.config,
            engine.repo_root,
            "hello",
            timeout_seconds=0.01,
        )
