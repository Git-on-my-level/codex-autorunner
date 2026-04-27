"""Doc-code sync tests.

Asserts that key documentation stays in sync with the actual codebase:
1. Integration directories appear in the constitution/glossary
2. Surface directories exist on disk
3. Architecture map references valid paths
4. Key doc files don't reference dead paths
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "codex_autorunner"
INTEGRATIONS_DIR = SRC_ROOT / "integrations"
SURFACES_DIR = SRC_ROOT / "surfaces"
AGENTS_DIR = SRC_ROOT / "agents"
CONSTITUTION = REPO_ROOT / "docs" / "car_constitution" / "10_CODEBASE_CONSTITUTION.md"
GLOSSARY = REPO_ROOT / "docs" / "car_constitution" / "95_GLOSSARY.md"
ARCH_MAP = REPO_ROOT / "docs" / "car_constitution" / "20_ARCHITECTURE_MAP.md"
ARCH_BOUNDARIES = REPO_ROOT / "docs" / "ARCHITECTURE_BOUNDARIES.md"
DESIGN_MD = REPO_ROOT / "DESIGN.md"


def _actual_dirs(parent: Path) -> set[str]:
    if not parent.is_dir():
        return set()
    return {
        d.name
        for d in parent.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    }


def _read_doc(path: Path) -> str:
    assert path.is_file(), f"Doc not found: {path}"
    return path.read_text(encoding="utf-8")


class TestIntegrationSync:
    def test_integration_dirs_exist_on_disk(self):
        expected = {
            "telegram",
            "discord",
            "github",
            "app_server",
            "docker",
            "chat",
            "templates",
            "agents",
        }
        actual = _actual_dirs(INTEGRATIONS_DIR)
        for name in expected:
            assert name in actual, f"Integration dir missing from disk: {name}"

    def test_constitution_lists_all_surfaces(self):
        text = _read_doc(CONSTITUTION)
        for surface in ("Discord", "Telegram", "Web", "CLI"):
            assert surface in text, f"Constitution missing surface: {surface}"

    def test_constitution_lists_all_backends(self):
        text = _read_doc(CONSTITUTION)
        for backend in ("Codex", "OpenCode", "Hermes", "ZeroClaw"):
            assert backend in text, f"Constitution missing backend: {backend}"


class TestSurfaceSync:
    def test_surface_dirs_exist_on_disk(self):
        # Chat platforms are under integrations/; surfaces/ is web + cli (+ telegram stubs if present).
        expected = {"web", "cli", "telegram"}
        actual = _actual_dirs(SURFACES_DIR)
        for name in expected:
            assert name in actual, f"Surface dir missing from disk: {name}"

    def test_architecture_boundaries_lists_all_surfaces(self):
        text = _read_doc(ARCH_BOUNDARIES)
        for surface in ("Web UI", "CLI", "Telegram", "Discord"):
            assert (
                surface in text
            ), f"ARCHITECTURE_BOUNDARIES missing surface: {surface}"


class TestAgentSync:
    def test_agent_dirs_exist_on_disk(self):
        expected = {"codex", "opencode", "hermes", "zeroclaw", "acp"}
        actual = _actual_dirs(AGENTS_DIR)
        for name in expected:
            assert name in actual, f"Agent dir missing from disk: {name}"


class TestArchitectureMapPaths:
    def test_pma_chat_delivery_runtime_path(self):
        text = _read_doc(ARCH_MAP)
        assert "pma_chat_delivery_runtime.py" in text
        expected_file = SRC_ROOT / "pma_chat_delivery_runtime.py"
        assert (
            expected_file.is_file()
        ), f"pma_chat_delivery_runtime.py not at package root: {expected_file}"

    def test_runner_submodules_under_tickets(self):
        text = _read_doc(ARCH_MAP)
        assert (
            "tickets/" in text or "tickets\\" in text
        ), "Runner submodules should reference tickets/ package path"
        for module in (
            "runner.py",
            "runner_selection.py",
            "runner_prompt.py",
            "runner_execution.py",
            "runner_post_turn.py",
            "runner_commit.py",
        ):
            assert (
                SRC_ROOT / "tickets" / module
            ).is_file(), f"Runner submodule missing: tickets/{module}"


class TestNoDeadPaths:
    DEAD_PATHS = [
        "src/codex_autorunner/routes/",
        "src/codex_autorunner/web/",
        "src/codex_autorunner/engine.py",
        "api_routes.py",
        "snapshot.py",
    ]

    def test_key_docs_no_dead_paths(self):
        docs_to_check = [
            REPO_ROOT / "README.md",
            ARCH_MAP,
            ARCH_BOUNDARIES,
            DESIGN_MD,
            CONSTITUTION,
            GLOSSARY,
            REPO_ROOT / "CONTRIBUTING.md",
            REPO_ROOT / "docs" / "AGENT_SETUP_GUIDE.md",
            SRC_ROOT / "surfaces" / "web" / "README.md",
        ]
        for doc_path in docs_to_check:
            if not doc_path.is_file():
                continue
            text = _read_doc(doc_path)
            for dead in self.DEAD_PATHS:
                assert (
                    dead not in text
                ), f"{doc_path.relative_to(REPO_ROOT)} references dead path: {dead}"


class TestContributingCommands:
    # Match "npm run" only when not part of "pnpm run" (substring overlap).
    _NPM_RUN_NOT_PNPM = re.compile(r"(?<!p)npm\s+run")

    def test_pnpm_not_npm(self):
        text = _read_doc(REPO_ROOT / "CONTRIBUTING.md")
        for line in text.splitlines():
            assert not self._NPM_RUN_NOT_PNPM.search(
                line
            ), f"CONTRIBUTING.md uses 'npm run' instead of 'pnpm run': {line}"


class TestDesignConfigSections:
    def test_design_lists_all_config_sections(self):
        text = _read_doc(DESIGN_MD)
        for section in (
            "hermes",
            "zeroclaw",
            "discord_bot",
            "telegram_bot",
            "agents",
            "notifications",
        ):
            assert section in text, f"DESIGN.md missing config section: {section}"
