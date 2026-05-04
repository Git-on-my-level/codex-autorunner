from __future__ import annotations

import json
from typing import Any, Callable, NoReturn

import typer


def echo_json(
    payload: Any,
    *,
    indent: int | None = 2,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Emit JSON using the CLI's stable pretty-print defaults."""
    typer.echo(json.dumps(payload, indent=indent, default=default))


def exit_with_error(
    message: str,
    *,
    code: int = 1,
    cause: BaseException | None = None,
) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=code) from cause
