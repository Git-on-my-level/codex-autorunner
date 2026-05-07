from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.cli import app

runner = CliRunner()


def test_docs_list_includes_shipped_and_hub_docs(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    result = runner.invoke(app, ["docs", "list", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "car/path-model" in result.output
    assert "pma/about" in result.output
    assert str(tmp_path / ".codex-autorunner/pma/docs/ABOUT_CAR.md") in result.output


def test_docs_search_finds_shipped_path_model(tmp_path: Path) -> None:
    result = runner.invoke(app, ["docs", "search", "runtime cwd"])

    assert result.exit_code == 0
    assert "car/path-model" in result.output


def test_docs_show_and_path_for_hub_pma_doc(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    show = runner.invoke(app, ["docs", "show", "pma/about", "--path", str(tmp_path)])
    path = runner.invoke(app, ["docs", "path", "pma/about", "--path", str(tmp_path)])

    assert show.exit_code == 0
    assert "PMA Operations Guide" in show.output
    assert path.exit_code == 0
    assert path.output.strip() == str(
        tmp_path / ".codex-autorunner/pma/docs/ABOUT_CAR.md"
    )


def test_docs_show_repo_doc_when_repo_is_provided(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    hub.mkdir()
    repo.mkdir()
    seed_hub_files(hub, force=True)
    seed_repo_files(repo, force=True, git_required=False)

    result = runner.invoke(
        app,
        [
            "docs",
            "show",
            "repo/about",
            "--path",
            str(hub),
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert "ABOUT_CAR" in result.output
