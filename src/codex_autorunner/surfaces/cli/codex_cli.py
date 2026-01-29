import subprocess
from functools import lru_cache
from typing import Iterable, Optional

SUBCOMMAND_HINTS = ("exec", "resume")


def extract_flag_value(args: Iterable[str], flag: str) -> Optional[str]:
    if not args:
        return None
    for arg in args:
        if not isinstance(arg, str):
            continue
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1] or None
    args_list = [str(a) for a in args]
    for idx, arg in enumerate(args_list):
        if arg == flag and idx + 1 < len(args_list):
            return args_list[idx + 1]
    return None


def inject_flag(
    args: Iterable[str],
    flag: str,
    value: Optional[str],
    *,
    subcommands: Iterable[str] = SUBCOMMAND_HINTS,
) -> list[str]:
    if not value:
        return [str(a) for a in args]
    args_list = [str(a) for a in args]
    if extract_flag_value(args_list, flag):
        return args_list
    insert_at = None
    for cmd in subcommands:
        try:
            insert_at = args_list.index(cmd)
            break
        except ValueError:
            continue
    if insert_at is None:
        # `args` is sometimes a full argv that includes the binary at index 0,
        # e.g. ["codex", "--yolo", ...]. In that case, never prepend flags before
        # the binary or we'll turn argv[0] into e.g. "--model" and crash at spawn.
        if args_list and not args_list[0].startswith("-"):
            return [args_list[0], flag, value] + args_list[1:]
        return [flag, value] + args_list
    return args_list[:insert_at] + [flag, value] + args_list[insert_at:]


def apply_codex_options(
    args: Iterable[str],
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    supports_reasoning: Optional[bool] = None,
) -> list[str]:
    with_model = inject_flag(args, "--model", model)
    if reasoning and supports_reasoning is False:
        return with_model
    return inject_flag(with_model, "--reasoning", reasoning)


def _read_help_text(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    return "\n".join(filter(None, [result.stdout, result.stderr]))


@lru_cache(maxsize=8)
def supports_flag(binary: str, flag: str) -> bool:
    return flag in _read_help_text(binary)


def supports_reasoning(binary: str) -> bool:
    return supports_flag(binary, "--reasoning")
