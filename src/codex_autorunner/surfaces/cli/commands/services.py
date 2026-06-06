from __future__ import annotations

import json
import os
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin

import httpx
import typer


def register_services_commands(
    services_app: typer.Typer,
    *,
    require_hub_config: Callable[[Optional[Path]], Any],
    build_server_url: Callable[..., str],
    request_json: Callable[..., dict[str, Any]],
    raise_exit: Callable[..., None],
) -> None:
    def _url(config: Any, route: str, *, base_path: Optional[str] = None) -> str:
        return build_server_url(config, route, base_path_override=base_path)

    def _append_query(url: str, params: dict[str, Any]) -> str:
        filtered = {
            key: value
            for key, value in params.items()
            if value is not None and value != ""
        }
        if not filtered:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode(filtered)}"

    def _request(
        method: str,
        url: str,
        config: Any,
        *,
        payload: Optional[dict[str, Any]] = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        try:
            return request_json(
                method,
                url,
                payload,
                token_env=getattr(config, "server_auth_token_env", None),
                timeout_seconds=timeout_seconds,
            )
        except (RuntimeError, OSError, ValueError, TypeError, httpx.HTTPError) as exc:
            from .utils import format_hub_request_error

            raise_exit(
                format_hub_request_error(
                    action="Failed to request preview services from the hub.",
                    url=url,
                    exc=exc,
                    base_path_cli_hint="--base-path /car",
                ),
                cause=exc,
            )
            raise typer.Exit(code=1) from exc

    def _load_config(path: Optional[Path]) -> Any:
        return require_hub_config(path)

    def _json(data: dict[str, Any]) -> None:
        typer.echo(json.dumps(data, indent=2, sort_keys=True))

    def _preview_public_base_url(config: Any) -> Optional[str]:
        env_value = os.environ.get("CAR_PREVIEW_PUBLIC_BASE_URL")
        if env_value and env_value.strip():
            return env_value.strip().rstrip("/")
        preview_cfg = getattr(config, "preview_services", None)
        if not isinstance(preview_cfg, dict):
            raw_cfg = getattr(config, "raw", None)
            preview_cfg = (
                raw_cfg.get("preview_services")
                if isinstance(raw_cfg, dict)
                else preview_cfg
            )
        if isinstance(preview_cfg, dict):
            for key in ("public_base_url", "publicBaseUrl", "base_url", "baseUrl"):
                value = preview_cfg.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().rstrip("/")
        for attr in ("public_base_url", "server_public_url", "external_url"):
            value = getattr(config, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip().rstrip("/")
        return None

    def _absolute_or_relative_preview_url(config: Any, preview_url: str) -> str:
        if preview_url.startswith(("http://", "https://")):
            return preview_url
        base = _preview_public_base_url(config)
        if not base:
            return preview_url
        return urljoin(f"{base}/", preview_url.lstrip("/"))

    def _with_absolute_preview_urls(config: Any, data: Any) -> Any:
        if isinstance(data, list):
            return [_with_absolute_preview_urls(config, item) for item in data]
        if not isinstance(data, dict):
            return data
        normalized: dict[str, Any] = {
            key: _with_absolute_preview_urls(config, value)
            for key, value in data.items()
        }
        for key in ("preview_url", "car_url"):
            value = normalized.get(key)
            if isinstance(value, str) and value.startswith("/"):
                normalized[key] = _absolute_or_relative_preview_url(config, value)
        return normalized

    def _parse_ttl_seconds(value: str) -> int:
        raw = value.strip().lower()
        if not raw:
            raise_exit("--ttl must be non-empty.")
        multiplier = 1
        if raw[-1:] in {"s", "m", "h", "d"}:
            suffix = raw[-1]
            raw = raw[:-1]
            multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[suffix]
        try:
            amount = int(raw)
        except ValueError:
            raise_exit("--ttl must be an integer duration such as 3600, 10m, or 24h.")
        ttl_seconds = amount * multiplier
        if ttl_seconds <= 0:
            raise_exit("--ttl must be greater than zero.")
        return ttl_seconds

    def _scope_links(scopes: list[str]) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        for raw in scopes:
            value = raw.strip()
            if not value:
                continue
            if ":" not in value:
                links.append({"kind": value})
                continue
            kind, ident = value.split(":", 1)
            if kind == "workspace":
                links.append({"kind": kind, "path": ident})
            else:
                links.append({"kind": kind, "id": ident})
        return links

    def _env_pairs(pairs: list[str]) -> dict[str, str]:
        env: dict[str, str] = {}
        for pair in pairs:
            if "=" not in pair:
                raise_exit(f"Invalid --env value {pair!r}; expected KEY=VALUE.")
            key, value = pair.split("=", 1)
            key = key.strip()
            if not key:
                raise_exit(f"Invalid --env value {pair!r}; key is empty.")
            env[key] = value
        return env

    def _client_resolved_path(path_value: Path) -> str:
        return str(path_value.expanduser().resolve())

    def _service(data: dict[str, Any]) -> dict[str, Any]:
        service = data.get("service")
        return service if isinstance(service, dict) else data

    def _service_models(data: dict[str, Any]) -> list[dict[str, Any]]:
        read_model = data.get("read_model")
        if isinstance(read_model, dict) and isinstance(
            read_model.get("services"), list
        ):
            return [item for item in read_model["services"] if isinstance(item, dict)]
        services = data.get("services")
        return (
            [item for item in services if isinstance(item, dict)]
            if isinstance(services, list)
            else []
        )

    def _print_service_line(service: dict[str, Any]) -> None:
        service_id = service.get("service_id", "")
        name = service.get("name", "")
        kind = service.get("kind", "")
        service_class = service.get("service_class", "")
        trust_level = service.get("trust_level", "")
        ownership = service.get("ownership", "")
        status = service.get("status", "")
        scope = service.get("scope") or _first_scope(service.get("scope_links"))
        service_url = _service_preview_or_car_url(service)
        port = service.get("port") or (service.get("target") or {}).get("port")
        port_text = f" port={port}" if port else ""
        scope_text = f" scope={scope}" if scope else ""
        taxonomy = "/".join(
            str(item) for item in (service_class, trust_level, ownership) if item
        )
        taxonomy_text = f" {taxonomy}" if taxonomy else ""
        typer.echo(
            f"{service_id}\t{status}\t{kind}{taxonomy_text}\t{name}{scope_text}{port_text}\t{service_url}"
        )

    def _service_preview_or_car_url(service: dict[str, Any]) -> str:
        raw_exposure = service.get("exposure")
        exposure: dict[str, Any] = (
            raw_exposure if isinstance(raw_exposure, dict) else {}
        )
        return str(
            service.get("preview_url")
            or service.get("previewUrl")
            or exposure.get("preview_url")
            or exposure.get("previewUrl")
            or service.get("car_url")
            or exposure.get("car_url")
            or ""
        )

    def _first_scope(scope_links: Any) -> Optional[str]:
        if not isinstance(scope_links, list) or not scope_links:
            return None
        first = scope_links[0]
        if not isinstance(first, dict):
            return None
        kind = first.get("kind")
        if not kind:
            return None
        if first.get("id"):
            return f"{kind}:{first['id']}"
        if first.get("path"):
            return f"{kind}:{first['path']}"
        return str(kind)

    def _print_service_detail(service: dict[str, Any]) -> None:
        fields = [
            ("id", service.get("service_id")),
            ("name", service.get("name")),
            ("kind", service.get("kind")),
            ("service_class", service.get("service_class")),
            ("trust_level", service.get("trust_level")),
            ("ownership", service.get("ownership")),
            ("network_policy", service.get("network_policy")),
            ("status", service.get("status")),
            ("scope", service.get("scope") or _first_scope(service.get("scope_links"))),
            (
                "preview_url",
                service.get("preview_url")
                or service.get("previewUrl")
                or (service.get("exposure") or {}).get("preview_url")
                or (service.get("exposure") or {}).get("previewUrl"),
            ),
            (
                "car_url",
                service.get("car_url")
                or (service.get("exposure") or {}).get("car_url"),
            ),
            (
                "direct_url",
                service.get("direct_url")
                or (service.get("target") or {}).get("direct_url"),
            ),
            ("port", service.get("port") or (service.get("target") or {}).get("port")),
            (
                "owner_pid",
                service.get("owner_pid") or (service.get("process") or {}).get("pid"),
            ),
        ]
        for key, value in fields:
            if value is not None and value != "":
                typer.echo(f"{key}: {value}")
        process = (
            service.get("process") if isinstance(service.get("process"), dict) else {}
        )
        for key in ("exit_code", "exited_at", "last_exit_reason"):
            value = process.get(key) if isinstance(process, dict) else None
            if value is not None and value != "":
                typer.echo(f"{key}: {value}")

    def _print_action(data: dict[str, Any], action: str) -> None:
        service = _service(data)
        deleted = data.get("deleted")
        suffix = " deleted=true" if deleted is True else ""
        typer.echo(
            f"{action}: {service.get('service_id', '')} {service.get('status', '')} "
            f"{_service_preview_or_car_url(service)}{suffix}".strip()
        )

    def _danger_payload(
        *,
        force: bool,
        force_attestation: Optional[str],
        command_name: str,
    ) -> dict[str, Any]:
        if force and not force_attestation:
            raise_exit(
                f"{command_name} with --force requires --force-attestation TEXT."
            )
        if force_attestation and not force:
            raise_exit(f"{command_name} --force-attestation requires --force.")
        return {"force": force, "force_attestation": force_attestation}

    @services_app.command("list")
    def list_services(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        scope: Optional[str] = typer.Option(
            None, "--scope", help="Filter by scope, such as repo:my-repo."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """List preview services registered with the hub."""
        config = _load_config(path)
        url = _append_query(
            _url(config, "/hub/services", base_path=base_path),
            {"scope": scope},
        )
        data = _request("GET", url, config)
        if json_output:
            _json(_with_absolute_preview_urls(config, data))
            return
        services = _service_models(data)
        if not services:
            typer.echo("No preview services.")
            return
        for service in services:
            _print_service_line(service)

    @services_app.command("get")
    def get_service(
        service_id: str = typer.Argument(..., help="Preview service ID."),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Show one preview service by ID."""
        config = _load_config(path)
        data = _request(
            "GET",
            _url(config, f"/hub/services/{service_id}", base_path=base_path),
            config,
        )
        if json_output:
            _json(_with_absolute_preview_urls(config, data))
            return
        _print_service_detail(_service(data))

    @services_app.command("register-static")
    def register_static(
        static_path: Path = typer.Argument(..., help="Static file or directory path."),
        name: Optional[str] = typer.Option(
            None, "--name", help="Service display name."
        ),
        scope: list[str] = typer.Option(
            [], "--scope", help="Scope link, repeatable; e.g. repo:car."
        ),
        kind: Optional[str] = typer.Option(
            None, "--kind", help="static-file or static-dir."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Register a static file or directory as a preview service."""
        kind_value = kind.replace("-", "_") if kind else None
        payload = {
            "path": _client_resolved_path(static_path),
            "name": name,
            "kind": kind_value,
            "scope_links": _scope_links(scope),
            "created_by": "cli",
        }
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, "/hub/services/static", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, "registered")

    @services_app.command("register-url")
    def register_url(
        url_arg: str = typer.Argument(..., help="Loopback URL to proxy."),
        name: Optional[str] = typer.Option(
            None, "--name", help="Service display name."
        ),
        scope: list[str] = typer.Option(
            [], "--scope", help="Scope link, repeatable; e.g. repo:car."
        ),
        health_path: Optional[str] = typer.Option(
            "/", "--health-path", help="HTTP health path; empty disables HTTP health."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Register a loopback URL as a proxied preview service."""
        payload = {
            "url": url_arg,
            "name": name,
            "health_path": health_path,
            "scope_links": _scope_links(scope),
            "created_by": "cli",
        }
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, "/hub/services/loopback-url", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, "registered")

    @services_app.command(
        "start-managed",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def start_managed(
        ctx: typer.Context,
        name: str = typer.Option(..., "--name", help="Service display name."),
        cwd: Path = typer.Option(..., "--cwd", help="Command working directory."),
        scope: list[str] = typer.Option(
            [], "--scope", help="Scope link, repeatable; e.g. repo:car."
        ),
        port: Optional[int] = typer.Option(
            None, "--port", help="Exact preferred port."
        ),
        auto_port: bool = typer.Option(
            False, "--auto-port", help="Allocate a port from the preview range."
        ),
        env: list[str] = typer.Option(
            [], "--env", help="Environment pair, repeatable; KEY=VALUE."
        ),
        env_policy: str = typer.Option(
            "minimal",
            "--env-policy",
            help="Managed env policy: minimal, allowlist, or inherit_all.",
        ),
        health_path: Optional[str] = typer.Option(
            "/", "--health-path", help="HTTP health path."
        ),
        autostart: bool = typer.Option(
            False, "--autostart", help="Opt into hub-start autostart."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Register a managed command and start it immediately."""
        command = [str(item) for item in ctx.args]
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise_exit("start-managed requires a command after --.")
        if port is not None and auto_port:
            raise_exit("Use either --port or --auto-port, not both.")
        port_policy: dict[str, Any] = (
            {"mode": "auto"}
            if auto_port or port is None
            else {"mode": "preferred", "port": port}
        )
        payload = {
            "name": name,
            "argv": command,
            "cwd": _client_resolved_path(cwd),
            "env": _env_pairs(env),
            "env_policy": env_policy,
            "port_policy": port_policy,
            "health_check": (
                {"type": "http", "path": health_path}
                if health_path
                else {"type": "tcp"}
            ),
            "scope_links": _scope_links(scope),
            "created_by": "cli",
            "auto_start_on_hub_start": autostart,
            "start": True,
        }
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, "/hub/services/managed", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, "started")

    @services_app.command(
        "register-managed",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def register_managed(
        ctx: typer.Context,
        name: str = typer.Option(..., "--name", help="Service display name."),
        cwd: Path = typer.Option(..., "--cwd", help="Command working directory."),
        scope: list[str] = typer.Option(
            [], "--scope", help="Scope link, repeatable; e.g. repo:car."
        ),
        port: Optional[int] = typer.Option(
            None, "--port", help="Exact preferred port."
        ),
        auto_port: bool = typer.Option(
            False, "--auto-port", help="Allocate a port from the preview range."
        ),
        env: list[str] = typer.Option(
            [], "--env", help="Environment pair, repeatable; KEY=VALUE."
        ),
        env_policy: str = typer.Option(
            "minimal",
            "--env-policy",
            help="Managed env policy: minimal, allowlist, or inherit_all.",
        ),
        health_path: Optional[str] = typer.Option(
            "/", "--health-path", help="HTTP health path."
        ),
        autostart: bool = typer.Option(
            False, "--autostart", help="Opt into hub-start autostart."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Register a managed command without starting it."""
        command = [str(item) for item in ctx.args]
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise_exit("register-managed requires a command after --.")
        if port is not None and auto_port:
            raise_exit("Use either --port or --auto-port, not both.")
        port_policy: dict[str, Any] = (
            {"mode": "auto"}
            if auto_port or port is None
            else {"mode": "preferred", "port": port}
        )
        payload = {
            "name": name,
            "argv": command,
            "cwd": _client_resolved_path(cwd),
            "env": _env_pairs(env),
            "env_policy": env_policy,
            "port_policy": port_policy,
            "health_check": (
                {"type": "http", "path": health_path}
                if health_path
                else {"type": "tcp"}
            ),
            "scope_links": _scope_links(scope),
            "created_by": "cli",
            "auto_start_on_hub_start": autostart,
            "start": False,
        }
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, "/hub/services/managed", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, "registered")

    def _simple_lifecycle(
        service_id: str,
        action: str,
        *,
        path: Optional[Path],
        base_path: Optional[str],
        json_output: bool,
    ) -> None:
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, f"/hub/services/{service_id}/{action}", base_path=base_path),
            config,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, action)

    @services_app.command("start")
    def start(
        service_id: str,
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Start a registered preview service."""
        _simple_lifecycle(
            service_id, "start", path=path, base_path=base_path, json_output=json_output
        )

    @services_app.command("stop")
    def stop(
        service_id: str,
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Stop a running preview service."""
        _simple_lifecycle(
            service_id, "stop", path=path, base_path=base_path, json_output=json_output
        )

    @services_app.command("restart")
    def restart(
        service_id: str,
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Restart a preview service."""
        _simple_lifecycle(
            service_id,
            "restart",
            path=path,
            base_path=base_path,
            json_output=json_output,
        )

    @services_app.command("kill")
    def kill(
        service_id: str,
        force: bool = typer.Option(False, "--force", help="Confirm force termination."),
        force_attestation: Optional[str] = typer.Option(
            None, "--force-attestation", help="Reason for force termination."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Force-terminate a managed preview service process."""
        payload = _danger_payload(
            force=force, force_attestation=force_attestation, command_name="kill"
        )
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, f"/hub/services/{service_id}/kill", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, "killed")

    def _danger_lifecycle(
        service_id: str,
        action: str,
        *,
        force: bool,
        force_attestation: Optional[str],
        path: Optional[Path],
        base_path: Optional[str],
        json_output: bool,
    ) -> None:
        payload = _danger_payload(
            force=force, force_attestation=force_attestation, command_name=action
        )
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, f"/hub/services/{service_id}/{action}", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, action)

    @services_app.command("teardown")
    def teardown(
        service_id: str,
        force: bool = typer.Option(
            False, "--force", help="Allow forceful teardown when needed."
        ),
        force_attestation: Optional[str] = typer.Option(
            None, "--force-attestation", help="Reason for forceful teardown."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Tear down a preview service and remove its registration."""
        _danger_lifecycle(
            service_id,
            "teardown",
            force=force,
            force_attestation=force_attestation,
            path=path,
            base_path=base_path,
            json_output=json_output,
        )

    @services_app.command("unlink")
    def unlink(
        service_id: str,
        force: bool = typer.Option(
            False, "--force", help="Allow unlinking a running managed service."
        ),
        force_attestation: Optional[str] = typer.Option(
            None, "--force-attestation", help="Reason for force unlink."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Unlink a preview service from the hub without deleting its files."""
        _danger_lifecycle(
            service_id,
            "unlink",
            force=force,
            force_attestation=force_attestation,
            path=path,
            base_path=base_path,
            json_output=json_output,
        )

    @services_app.command("logs")
    def logs(
        service_id: str,
        tail: int = typer.Option(
            200, "--tail", min=0, max=5000, help="Number of log lines to fetch."
        ),
        follow: bool = typer.Option(
            False, "--follow", help="Poll and print appended logs."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Fetch or follow logs for a preview service."""
        config = _load_config(path)
        url = _append_query(
            _url(config, f"/hub/services/{service_id}/logs", base_path=base_path),
            {"tail": tail},
        )
        if not follow:
            data = _request("GET", url, config)
            if json_output:
                _json(data)
                return
            typer.echo(
                data.get("text", ""), nl=not str(data.get("text", "")).endswith("\n")
            )
            return
        if json_output:
            raise_exit("logs --follow cannot be combined with --json.")
        previous = ""
        while True:
            data = _request("GET", url, config)
            text = str(data.get("text", ""))
            if text.startswith(previous):
                typer.echo(text[len(previous) :], nl=False)
            elif text != previous:
                typer.echo(text, nl=False)
            previous = text
            time.sleep(1.0)

    @services_app.command("open")
    def open_service(
        service_id: str,
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        direct: bool = typer.Option(
            False,
            "--direct",
            help="Open the diagnostic /preview/services route instead of issuing a capability link.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Open a preview service URL in the default browser."""
        config = _load_config(path)
        if direct:
            data = _request(
                "GET",
                _url(config, f"/hub/services/{service_id}", base_path=base_path),
                config,
            )
            raw_service = data.get("read_model")
            service: dict[str, Any] = (
                raw_service if isinstance(raw_service, dict) else _service(data)
            )
            exposure = service.get("exposure")
            exposure_map = exposure if isinstance(exposure, dict) else {}
            preview_url = str(
                exposure_map.get("car_url") or service.get("car_url") or ""
            )
        else:
            data = _request(
                "POST",
                _url(
                    config,
                    f"/hub/services/{service_id}/preview-token",
                    base_path=base_path,
                ),
                config,
            )
            preview_url = str(data.get("preview_url") or "")
        resolved = _absolute_or_relative_preview_url(config, preview_url)
        if json_output:
            _json({"service_id": service_id, "preview_url": resolved})
            return
        if resolved.startswith(("http://", "https://")):
            webbrowser.open(resolved)
        typer.echo(resolved)

    @services_app.command("issue-link")
    def issue_link(
        service_id: str,
        ttl: str = typer.Option("24h", "--ttl", help="Capability TTL, e.g. 24h."),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Issue a time-limited capability link for a preview service."""
        ttl_seconds = _parse_ttl_seconds(ttl)
        config = _load_config(path)
        url = _append_query(
            _url(
                config,
                f"/hub/services/{service_id}/preview-token",
                base_path=base_path,
            ),
            {"ttl": ttl_seconds},
        )
        data = _request("POST", url, config)
        preview_url = str(data.get("preview_url") or "")
        data["preview_url"] = _absolute_or_relative_preview_url(config, preview_url)
        if json_output:
            _json(data)
            return
        typer.echo(data["preview_url"])

    @services_app.command("revoke-link")
    def revoke_link(
        service_id: str,
        all_links: bool = typer.Option(
            False, "--all", help="Revoke all active links for the service."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Revoke active capability links for a preview service."""
        if not all_links:
            raise_exit("revoke-link currently requires --all.")
        config = _load_config(path)
        data = _request(
            "POST",
            _url(
                config,
                f"/hub/services/{service_id}/preview-token/revoke",
                base_path=base_path,
            ),
            config,
        )
        if json_output:
            _json(data)
            return
        typer.echo(f"revoked: {data.get('revoked', 0)}")

    @services_app.command("health")
    def health(
        service_id: str,
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Run a health check against a preview service."""
        config = _load_config(path)
        data = _request(
            "POST",
            _url(config, f"/hub/services/{service_id}/health", base_path=base_path),
            config,
        )
        if json_output:
            _json(data)
            return
        health_data = data.get("health")
        result = health_data if isinstance(health_data, dict) else {}
        service = _service(data)
        fields = [
            str(service.get("service_id", service_id)),
            str(service.get("status", "")),
            f"ok={result.get('ok')}",
            f"type={result.get('type')}",
        ]
        if result.get("status_code") is not None:
            fields.append(f"status_code={result.get('status_code')}")
        if result.get("error"):
            fields.append(f"error={result.get('error')}")
        typer.echo(" ".join(field for field in fields if field).strip())

    @services_app.command("set-autostart")
    def set_autostart(
        service_id: str,
        enabled: Optional[bool] = typer.Option(
            None, "--enabled/--disabled", help="Enable or disable hub-start autostart."
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", help="Hub root or config path."
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override configured hub base path."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print JSON response."),
    ) -> None:
        """Enable or disable hub-start autostart for a managed service."""
        if enabled is None:
            raise_exit("set-autostart requires --enabled or --disabled.")
        payload = {
            "restart_policy": {
                "auto_start_on_hub_start": enabled,
                "restart_on_exit": "never",
            }
        }
        config = _load_config(path)
        data = _request(
            "PATCH",
            _url(config, f"/hub/services/{service_id}", base_path=base_path),
            config,
            payload=payload,
        )
        if json_output:
            _json(data)
            return
        _print_action(data, "updated")
