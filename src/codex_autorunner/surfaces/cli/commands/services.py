from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode

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
        status = service.get("status", "")
        scope = service.get("scope") or _first_scope(service.get("scope_links"))
        car_url = service.get("car_url") or (service.get("exposure") or {}).get(
            "car_url", ""
        )
        port = service.get("port") or (service.get("target") or {}).get("port")
        port_text = f" port={port}" if port else ""
        scope_text = f" scope={scope}" if scope else ""
        typer.echo(
            f"{service_id}\t{status}\t{kind}\t{name}{scope_text}{port_text}\t{car_url}"
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
            ("status", service.get("status")),
            ("scope", service.get("scope") or _first_scope(service.get("scope_links"))),
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

    def _print_action(data: dict[str, Any], action: str) -> None:
        service = _service(data)
        deleted = data.get("deleted")
        suffix = " deleted=true" if deleted is True else ""
        typer.echo(
            f"{action}: {service.get('service_id', '')} {service.get('status', '')} "
            f"{(service.get('exposure') or {}).get('car_url') or service.get('car_url') or ''}{suffix}".strip()
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
        config = _load_config(path)
        url = _append_query(
            _url(config, "/hub/services", base_path=base_path),
            {"scope": scope},
        )
        data = _request("GET", url, config)
        if json_output:
            _json(data)
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
        config = _load_config(path)
        data = _request(
            "GET",
            _url(config, f"/hub/services/{service_id}", base_path=base_path),
            config,
        )
        if json_output:
            _json(data)
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
        kind_value = kind.replace("-", "_") if kind else None
        payload = {
            "path": str(static_path),
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
            "cwd": str(cwd),
            "env": _env_pairs(env),
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
