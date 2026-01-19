from codex_autorunner.integrations.telegram.handlers import commands_runtime


def test_format_media_batch_failure_empty() -> None:
    message = commands_runtime._format_media_batch_failure(
        image_disabled=0,
        file_disabled=0,
        image_too_large=0,
        file_too_large=0,
        image_download_failed=0,
        file_download_failed=0,
        image_save_failed=0,
        file_save_failed=0,
        unsupported=0,
        max_image_bytes=10,
        max_file_bytes=20,
    )
    assert message == "Failed to process any media in the batch."


def test_format_media_batch_failure_details() -> None:
    message = commands_runtime._format_media_batch_failure(
        image_disabled=1,
        file_disabled=2,
        image_too_large=3,
        file_too_large=4,
        image_download_failed=1,
        file_download_failed=1,
        image_save_failed=1,
        file_save_failed=1,
        unsupported=2,
        max_image_bytes=10,
        max_file_bytes=20,
    )
    assert message.splitlines() == [
        "Failed to process any media in the batch.",
        "- 1 image(s) skipped (image handling disabled).",
        "- 2 file(s) skipped (file handling disabled).",
        "- 3 image(s) too large (max 10 bytes).",
        "- 4 file(s) too large (max 20 bytes).",
        "- 1 image(s) failed to download.",
        "- 1 file(s) failed to download.",
        "- 1 image(s) failed to save.",
        "- 1 file(s) failed to save.",
        "- 2 item(s) had unsupported media types.",
    ]
