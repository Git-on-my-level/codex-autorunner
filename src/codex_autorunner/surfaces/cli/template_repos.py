"""Template repo config manager to centralize repo-config mutations."""

from pathlib import Path
from typing import Any, Optional

import typer
import yaml

from ...core.config import CONFIG_FILENAME, load_hub_config
from ...core.locks import file_lock


class TemplatesConfigError(Exception):
    """Error in templates configuration."""


class TemplateReposManager:
    """Manager for template repos in hub config."""

    def __init__(self, hub_config_path: Path) -> None:
        """Initialize the manager with a hub config path."""
        self.hub_config_path = hub_config_path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load the hub config YAML."""
        if not self.hub_config_path.exists():
            raise TemplatesConfigError(
                f"Hub config file not found: {self.hub_config_path}"
            )
        try:
            data = yaml.safe_load(self.hub_config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TemplatesConfigError(
                    f"Hub config must be a YAML mapping: {self.hub_config_path}"
                )
            self._data = data
        except yaml.YAMLError as exc:
            raise TemplatesConfigError(f"Invalid YAML in hub config: {exc}") from exc
        except OSError as exc:
            raise TemplatesConfigError(f"Failed to read hub config: {exc}") from exc

    def save(self) -> None:
        """Save the hub config YAML."""
        lock_path = self.hub_config_path.parent / (self.hub_config_path.name + ".lock")
        with file_lock(lock_path):
            self.hub_config_path.write_text(
                yaml.safe_dump(self._data, sort_keys=False), encoding="utf-8"
            )

    def list_repos(self) -> list[dict[str, Any]]:
        """List all configured template repos."""
        templates_config = self._data.get("templates", {})
        if not isinstance(templates_config, dict):
            templates_config = {}
        repos = templates_config.get("repos", [])
        if not isinstance(repos, list):
            repos = []
        return repos

    def add_repo(
        self,
        repo_id: str,
        url: str,
        trusted: Optional[bool] = None,
        default_ref: str = "main",
    ) -> None:
        """Add a template repo."""
        self._require_templates_enabled()

        templates_config = self._data.setdefault("templates", {})
        if not isinstance(templates_config, dict):
            raise TemplatesConfigError("Invalid templates config in hub config")
        templates_config.setdefault("enabled", True)

        repos = templates_config.setdefault("repos", [])
        if not isinstance(repos, list):
            raise TemplatesConfigError("Invalid repos config in hub config")

        existing_ids = {repo.get("id") for repo in repos if isinstance(repo, dict)}
        if repo_id in existing_ids:
            raise TemplatesConfigError(
                f"Repo ID '{repo_id}' already exists. Use a unique ID."
            )

        new_repo = {
            "id": repo_id,
            "url": url,
            "default_ref": default_ref,
        }
        if trusted is not None:
            new_repo["trusted"] = trusted

        repos.append(new_repo)

    def remove_repo(self, repo_id: str) -> None:
        """Remove a template repo."""
        templates_config = self._data.get("templates", {})
        if not isinstance(templates_config, dict):
            templates_config = {}
        repos = templates_config.get("repos", [])
        if not isinstance(repos, list):
            repos = []

        original_count = len(repos)
        filtered_repos = [
            repo
            for repo in repos
            if isinstance(repo, dict) and repo.get("id") != repo_id
        ]

        if len(filtered_repos) == original_count:
            raise TemplatesConfigError(f"Repo ID '{repo_id}' not found in config.")

        templates_config["repos"] = filtered_repos

    def set_trusted(self, repo_id: str, trusted: bool) -> None:
        """Set the trusted status of a template repo."""
        templates_config = self._data.get("templates", {})
        if not isinstance(templates_config, dict):
            templates_config = {}
        repos = templates_config.get("repos", [])
        if not isinstance(repos, list):
            repos = []

        found = False
        for repo in repos:
            if isinstance(repo, dict) and repo.get("id") == repo_id:
                repo["trusted"] = trusted
                found = True
                break

        if not found:
            raise TemplatesConfigError(f"Repo ID '{repo_id}' not found in config.")

    def _require_templates_enabled(self) -> None:
        """Ensure templates are enabled in config."""
        templates_config = self._data.get("templates", {})
        if not isinstance(templates_config, dict):
            templates_config = {}
        enabled = templates_config.get("enabled", True)
        if enabled is False:
            raise TemplatesConfigError(
                "Templates are disabled. Set templates.enabled=true in the hub config to enable."
            )


def load_template_repos_manager(hub: Optional[Path]) -> TemplateReposManager:
    """Load a TemplateReposManager for the given hub path."""
    try:
        config = load_hub_config(hub or Path.cwd())
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    hub_config_path = config.root / CONFIG_FILENAME
    return TemplateReposManager(hub_config_path)
