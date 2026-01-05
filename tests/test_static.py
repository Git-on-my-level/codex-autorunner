from contextlib import contextmanager
from pathlib import Path

from codex_autorunner import server
from codex_autorunner.server import _static_dir


def test_static_dir_has_index():
    static_dir, stack = _static_dir()
    try:
        assert static_dir.is_dir()
        assert (static_dir / "index.html").exists()
    finally:
        if stack is not None:
            stack.close()


def test_static_mobile_terminal_compose_view_assets():
    static_dir, stack = _static_dir()
    try:
        styles = (static_dir / "styles.css").read_text(encoding="utf-8")
        terminal_manager = (static_dir / "terminalManager.js").read_text(
            encoding="utf-8"
        )
        assert "mobile-terminal-view" in styles
        assert "_setMobileViewActive" in terminal_manager
    finally:
        if stack is not None:
            stack.close()


def test_static_dir_fallback_when_as_file_fails(monkeypatch):
    def raising_as_file(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(server.resources, "as_file", raising_as_file)
    static_dir, stack = _static_dir()
    try:
        expected = Path(server.__file__).resolve().parent / "static"
        assert static_dir == expected
        assert static_dir.is_dir()
    finally:
        if stack is not None:
            stack.close()


def test_static_dir_fallback_when_as_file_missing(monkeypatch, tmp_path: Path):
    missing = tmp_path / "missing"

    @contextmanager
    def fake_as_file(_):
        yield missing

    monkeypatch.setattr(server.resources, "as_file", fake_as_file)
    static_dir, stack = _static_dir()
    try:
        expected = Path(server.__file__).resolve().parent / "static"
        assert static_dir == expected
        assert static_dir.is_dir()
    finally:
        if stack is not None:
            stack.close()
