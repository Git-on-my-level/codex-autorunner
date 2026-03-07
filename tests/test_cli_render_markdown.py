from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_render_markdown_generates_mermaid_and_doc_exports(monkeypatch, tmp_path: Path):
    source = tmp_path / "deck.md"
    source.write_text(
        "# Demo\n\n```mermaid\nflowchart TD\n  A-->B\n```\n\nDone.\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "outbox"

    from codex_autorunner.surfaces.cli.commands import render as render_cmd

    monkeypatch.setattr(
        render_cmd, "resolve_executable", lambda binary: f"/usr/bin/{binary}"
    )
    invoked: list[list[str]] = []

    def _fake_run(cmd: list[str], capture_output: bool, text: bool):
        _ = capture_output, text
        invoked.append(cmd)
        if "-o" in cmd:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("ok\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(render_cmd.subprocess, "run", _fake_run)

    result = runner.invoke(
        app,
        [
            "render",
            "markdown",
            str(source),
            "--out-dir",
            str(out_dir),
            "--keep-intermediate",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out_dir / "deck.diagram-01.png").exists()
    assert (out_dir / "deck.diagram-01.pdf").exists()
    assert (out_dir / "deck.html").exists()
    rendered = out_dir / "deck.rendered.md"
    assert rendered.exists()
    assert "![Mermaid Diagram 1](deck.diagram-01.png)" in rendered.read_text(
        encoding="utf-8"
    )
    assert any(cmd[0].endswith("mmdc") for cmd in invoked)
    assert any(cmd[0].endswith("pandoc") for cmd in invoked)


def test_render_markdown_requires_mermaid_binary(monkeypatch, tmp_path: Path):
    source = tmp_path / "doc.md"
    source.write_text("```mermaid\nflowchart LR\nA-->B\n```\n", encoding="utf-8")

    from codex_autorunner.surfaces.cli.commands import render as render_cmd

    def _fake_resolve(binary: str):
        if binary == "mmdc":
            return None
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(render_cmd, "resolve_executable", _fake_resolve)

    result = runner.invoke(app, ["render", "markdown", str(source)])

    assert result.exit_code == 1
    assert "Unable to find 'mmdc'" in result.output


def test_render_markdown_deduplicates_repeated_diagram_formats(
    monkeypatch, tmp_path: Path
):
    source = tmp_path / "deck.md"
    source.write_text(
        "# Demo\n\n```mermaid\nflowchart TD\n  A-->B\n```\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "outbox"

    from codex_autorunner.surfaces.cli.commands import render as render_cmd

    monkeypatch.setattr(
        render_cmd, "resolve_executable", lambda binary: f"/usr/bin/{binary}"
    )
    invoked: list[list[str]] = []

    def _fake_run(cmd: list[str], capture_output: bool, text: bool):
        _ = capture_output, text
        invoked.append(cmd)
        if "-o" in cmd:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("ok\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(render_cmd.subprocess, "run", _fake_run)

    result = runner.invoke(
        app,
        [
            "render",
            "markdown",
            str(source),
            "--out-dir",
            str(out_dir),
            "--diagram-format",
            "png",
            "--diagram-format",
            "png",
            "--diagram-format",
            "pdf",
            "--doc-format",
            "html",
            "--keep-intermediate",
        ],
    )

    assert result.exit_code == 0, result.output
    rendered = out_dir / "deck.rendered.md"
    assert rendered.exists()
    assert "![Mermaid Diagram 1](deck.diagram-01.png)" in rendered.read_text(
        encoding="utf-8"
    )
    png_runs = [cmd for cmd in invoked if cmd[-1].endswith("deck.diagram-01.png")]
    assert len(png_runs) == 1
