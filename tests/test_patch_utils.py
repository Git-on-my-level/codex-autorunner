from codex_autorunner.core.patch_utils import infer_patch_strip, normalize_patch_text


def test_normalize_patch_text_default_target_adds_strip_prefix():
    patch_text = "\n".join(
        [
            "@@ -1 +1 @@",
            "-old",
            "+new",
        ]
    )
    normalized, targets = normalize_patch_text(
        patch_text, default_target="docs/TODO.md"
    )

    assert normalized.startswith("--- a/docs/TODO.md\n+++ b/docs/TODO.md\n")
    assert infer_patch_strip(targets) == 1


def test_normalize_patch_text_apply_patch_format_infers_strip():
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: docs/PROGRESS.md",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "*** End Patch",
        ]
    )
    normalized, targets = normalize_patch_text(patch_text)

    assert normalized.startswith("--- a/docs/PROGRESS.md\n+++ b/docs/PROGRESS.md\n")
    assert infer_patch_strip(targets) == 1
