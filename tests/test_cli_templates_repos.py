from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.config import CONFIG_FILENAME


def test_templates_repos_list_empty(hub_env) -> None:
    """List template repos when none are configured."""
    config_path = hub_env.hub_root / CONFIG_FILENAME
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["repos"] = []
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["templates", "repos", "list", "--hub", str(hub_env.hub_root)],
    )

    assert result.exit_code == 0
    assert "No template repos configured." in result.stdout


def test_templates_repos_list_json(hub_env) -> None:
    """List template repos in JSON format."""
    config_path = hub_env.hub_root / CONFIG_FILENAME
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["repos"] = [
        {
            "id": "test1",
            "url": "https://github.com/test1/repo",
            "trusted": True,
            "default_ref": "main",
        },
        {
            "id": "test2",
            "url": "/local/path/repo",
            "trusted": False,
            "default_ref": "develop",
        },
    ]
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["templates", "repos", "list", "--hub", str(hub_env.hub_root), "--json"],
    )

    assert result.exit_code == 0
    import json

    payload = json.loads(result.stdout)
    assert payload["repos"] == data["templates"]["repos"]


def test_templates_repos_add(hub_env) -> None:
    """Add a new template repo."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "add",
            "newrepo",
            "https://github.com/new/repo",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 0
    assert "Added template repo 'newrepo' to hub config." in result.stdout

    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repos = data["templates"]["repos"]
    assert any(repo.get("id") == "newrepo" for repo in repos)


def test_templates_repos_add_with_trusted(hub_env) -> None:
    """Add a trusted template repo."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "add",
            "trustedrepo",
            "https://github.com/trusted/repo",
            "--trusted",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 0
    assert "Added template repo 'trustedrepo' to hub config." in result.stdout

    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repos = data["templates"]["repos"]
    repo = next((r for r in repos if r.get("id") == "trustedrepo"), None)
    assert repo is not None
    assert repo.get("trusted") is True


def test_templates_repos_add_duplicate_id(hub_env) -> None:
    """Adding a repo with duplicate ID should fail."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["repos"] = [
        {
            "id": "existing",
            "url": "https://github.com/existing/repo",
            "default_ref": "main",
        }
    ]
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "add",
            "existing",
            "https://github.com/new/repo",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stdout or "already exists" in str(
        result.exception
    )


def test_templates_repos_remove(hub_env) -> None:
    """Remove an existing template repo."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["repos"] = [
        {
            "id": "toremove",
            "url": "https://github.com/remove/repo",
            "default_ref": "main",
        }
    ]
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["templates", "repos", "remove", "toremove", "--hub", str(hub_env.hub_root)],
    )

    assert result.exit_code == 0
    assert "Removed template repo 'toremove' from hub config." in result.stdout

    data_after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert not any(
        repo.get("id") == "toremove" for repo in data_after["templates"]["repos"]
    )


def test_templates_repos_remove_not_found(hub_env) -> None:
    """Removing a non-existent repo should fail."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "remove",
            "nonexistent",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 1
    assert "not found" in result.stdout or "not found" in str(result.exception)


def test_templates_repos_trust(hub_env) -> None:
    """Mark a repo as trusted."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["repos"] = [
        {
            "id": "totrust",
            "url": "https://github.com/trust/repo",
            "trusted": False,
            "default_ref": "main",
        }
    ]
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["templates", "repos", "trust", "totrust", "--hub", str(hub_env.hub_root)],
    )

    assert result.exit_code == 0
    assert "Marked repo 'totrust' as trusted." in result.stdout

    data_after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repo = next(
        (r for r in data_after["templates"]["repos"] if r.get("id") == "totrust"), None
    )
    assert repo is not None
    assert repo.get("trusted") is True


def test_templates_repos_trust_not_found(hub_env) -> None:
    """Trusting a non-existent repo should fail."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "trust",
            "nonexistent",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 1
    assert "not found" in result.stdout or "not found" in str(result.exception)


def test_templates_repos_untrust(hub_env) -> None:
    """Mark a repo as untrusted."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["repos"] = [
        {
            "id": "tountrust",
            "url": "https://github.com/untrust/repo",
            "trusted": True,
            "default_ref": "main",
        }
    ]
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "untrust",
            "tountrust",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 0
    assert "Marked repo 'tountrust' as untrusted." in result.stdout

    data_after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repo = next(
        (r for r in data_after["templates"]["repos"] if r.get("id") == "tountrust"),
        None,
    )
    assert repo is not None
    assert repo.get("trusted") is False


def test_templates_repos_add_when_disabled(hub_env) -> None:
    """Adding a repo when templates are disabled should fail."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["templates"]["enabled"] = False
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "repos",
            "add",
            "newrepo",
            "https://github.com/new/repo",
            "--hub",
            str(hub_env.hub_root),
        ],
    )

    assert result.exit_code == 1
    assert "disabled" in result.stdout or "disabled" in str(result.exception)
