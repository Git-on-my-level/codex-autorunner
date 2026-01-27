import subprocess
from pathlib import Path

import pytest

from codex_autorunner.core.patch_utils import (
    PatchError,
    apply_patch_file,
    infer_patch_strip,
    normalize_patch_text,
    preview_patch,
)


def test_normalize_patch_text_default_target_adds_strip_prefix():
    patch_text = "\n".join(
        [
            "@@ -1 +1 @@",
            "-old",
            "+new",
        ]
    )
    normalized, targets = normalize_patch_text(
        patch_text, default_target=".codex-autorunner/workspace/active_context.md"
    )

    assert normalized.startswith(
        "--- a/.codex-autorunner/workspace/active_context.md\n+++ b/.codex-autorunner/workspace/active_context.md\n"
    )
    assert infer_patch_strip(targets) == 1


def test_normalize_patch_text_apply_patch_format_infers_strip():
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: .codex-autorunner/workspace/decisions.md",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "*** End Patch",
        ]
    )
    normalized, targets = normalize_patch_text(patch_text)

    assert normalized.startswith(
        "--- a/.codex-autorunner/workspace/decisions.md\n+++ b/.codex-autorunner/workspace/decisions.md\n"
    )
    assert infer_patch_strip(targets) == 1


def test_apply_patch_file_reports_missing_patch_binary(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("patch missing")

    monkeypatch.setattr(subprocess, "run", fake_run)
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text("", encoding="utf-8")

    with pytest.raises(PatchError, match="patch command not found"):
        apply_patch_file(
            tmp_path,
            patch_path,
            ["a/.codex-autorunner/workspace/active_context.md"],
        )


def test_preview_patch_passes_timeout(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*_args, **kwargs):
        assert kwargs.get("timeout") is not None
        return subprocess.CompletedProcess(args=["patch"], returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = preview_patch(
        tmp_path,
        "@@ -1 +1 @@\n-old\n+new",
        ["a/.codex-autorunner/workspace/active_context.md"],
        base_content={".codex-autorunner/workspace/active_context.md": "current"},
    )

    assert result[".codex-autorunner/workspace/active_context.md"] == "current"
