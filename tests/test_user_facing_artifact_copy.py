from __future__ import annotations

from pathlib import Path

USER_FACING_SOURCES = (
    Path("src/codex_autorunner/core/context_awareness.py"),
    Path("src/codex_autorunner/core/pma_prompt_builder.py"),
    Path("src/codex_autorunner/core/about_car.py"),
    Path("src/codex_autorunner/core/car_context.py"),
    Path("src/codex_autorunner/bootstrap.py"),
    Path("src/codex_autorunner/adapters/telegram/handlers/commands/shared.py"),
    Path("src/codex_autorunner/adapters/telegram/handlers/commands/execution.py"),
    Path("src/codex_autorunner/adapters/telegram/handlers/commands/files.py"),
    Path("src/codex_autorunner/adapters/discord/message_turns.py"),
    Path("src/codex_autorunner/adapters/discord/service_normalization.py"),
)

FORBIDDEN_NORMAL_ARTIFACT_COPY = (
    "--to explicit",
    "import-legacy",
    "filebox/outbox",
    "outbox/pending",
    "Compatibility FileBox",
    "Legacy outbox",
    "Legacy pending",
    "write it to `<hub_root>/.codex-autorunner/filebox/outbox/`",
)


def test_user_facing_artifact_copy_omits_compatibility_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for rel_path in USER_FACING_SOURCES:
        text = (root / rel_path).read_text()
        for forbidden in FORBIDDEN_NORMAL_ARTIFACT_COPY:
            if forbidden in text:
                offenders.append(f"{rel_path}: {forbidden}")

    assert offenders == []
