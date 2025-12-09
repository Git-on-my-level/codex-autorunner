from codex_autorunner.server import _static_dir


def test_static_dir_has_index():
    static_dir = _static_dir()
    assert static_dir.is_dir()
    assert (static_dir / "index.html").exists()
