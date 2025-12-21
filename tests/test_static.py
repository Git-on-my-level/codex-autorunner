from codex_autorunner.server import _static_dir


def test_static_dir_has_index():
    static_dir = _static_dir()
    assert static_dir.is_dir()
    assert (static_dir / "index.html").exists()


def test_static_mobile_terminal_compose_view_assets():
    static_dir = _static_dir()
    styles = (static_dir / "styles.css").read_text(encoding="utf-8")
    terminal_manager = (static_dir / "terminalManager.js").read_text(encoding="utf-8")
    assert "mobile-terminal-view" in styles
    assert "_setMobileViewActive" in terminal_manager
