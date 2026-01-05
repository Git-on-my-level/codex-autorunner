from codex_autorunner.telegram_bot import _extract_thread_path


def test_extract_thread_path_prefers_nested_cwd_over_root() -> None:
    entry = {
        "root": "/hub",
        "workspace": {
            "cwd": "/repo",
        },
    }
    assert _extract_thread_path(entry) == "/repo"


def test_extract_thread_path_ignores_root_only() -> None:
    entry = {"root": "/hub"}
    assert _extract_thread_path(entry) is None
