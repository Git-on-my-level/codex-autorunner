from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.config import CONFIG_FILENAME, GENERATED_CONFIG_HEADER


def _load_raw_hub_config(config_path):
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data.setdefault("templates", {})
    return data


def test_templates_repos_list_empty(hub_env) -> None:
    """List template repos when none are configured."""
    config_path = hub_env.hub_root / CONFIG_FILENAME
    import yaml

    data = _load_raw_hub_config(config_path)
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

    data = _load_raw_hub_config(config_path)
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
    assert "Added repo newrepo" in result.stdout

    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    raw_text = config_path.read_text(encoding="utf-8")
    assert raw_text.startswith(GENERATED_CONFIG_HEADER)
    data = yaml.safe_load(raw_text)
    repos = data["templates"]["repos"]
    assert any(repo.get("id") == "newrepo" for repo in repos)
    assert any(repo.get("id") == "blessed" for repo in repos)


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
    assert "Added repo trustedrepo" in result.stdout

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = _load_raw_hub_config(config_path)
    repos = data["templates"]["repos"]
    repo = next((r for r in repos if r.get("id") == "trustedrepo"), None)
    assert repo is not None
    assert repo.get("trusted") is True


def test_templates_repos_add_keeps_non_generated_sparse_config_sparse(hub_env) -> None:
    """Adding a repo to a hand-authored sparse config should not expand defaults."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    config_path.write_text("version: 2\nmode: hub\n", encoding="utf-8")

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

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "mode", "templates"}
    repos = data["templates"]["repos"]
    assert any(repo.get("id") == "blessed" for repo in repos)
    assert any(repo.get("id") == "newrepo" for repo in repos)
    assert "pma" not in data
    assert "server" not in data


def test_templates_repos_add_duplicate_id(hub_env) -> None:
    """Adding a repo with duplicate ID should fail."""
    import yaml

    config_path = hub_env.hub_root / CONFIG_FILENAME
    data = _load_raw_hub_config(config_path)
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
    data = _load_raw_hub_config(config_path)
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
    assert "Removed repo toremove" in result.stdout

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
    data = _load_raw_hub_config(config_path)
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
    assert "Trusted totrust" in result.stdout

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
    data = _load_raw_hub_config(config_path)
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
    assert "Untrusted tountrust" in result.stdout

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
    data = _load_raw_hub_config(config_path)
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
