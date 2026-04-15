from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FileBudget:
    path: str
    max_lines: int
    reason: str


@dataclass(frozen=True)
class FunctionBudget:
    path: str
    qualname: str
    max_lines: int
    reason: str


@dataclass(frozen=True)
class HelperOwnershipRule:
    owner_path: str
    helper_names: tuple[str, ...]
    scan_roots: tuple[str, ...]
    reason: str


STANDARD_FILE_BUDGETS = (
    FileBudget(
        path="src/codex_autorunner/surfaces/web/routes/flow_routes/status_history_routes.py",
        max_lines=360,
        reason="Keep the extracted flow status/history slice small enough to stay injectable.",
    ),
    FileBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat_routes/execution.py",
        max_lines=460,
        reason="Keep the file-chat execution seam extracted from the legacy route builder.",
    ),
    FileBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat_routes/runtime.py",
        max_lines=110,
        reason="File-chat runtime state helpers should remain a thin route-support seam.",
    ),
    FileBudget(
        path="src/codex_autorunner/surfaces/web/routes/pma.py",
        max_lines=90,
        reason="The PMA composition entrypoint should remain a small router assembler.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/chat/command_diagnostics.py",
        max_lines=180,
        reason="Shared diagnostics commands should stay concentrated in the extracted adapter seam.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/discord/pma_commands.py",
        max_lines=150,
        reason="The Discord PMA command slice should stay narrow and readable.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/chat/progress_primitives.py",
        max_lines=550,
        reason="Cross-surface progress rendering should live in one bounded helper module.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/telegram/progress_stream.py",
        max_lines=20,
        reason="Telegram progress_stream is now a compatibility re-export and should stay thin.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/telegram/handlers/utils.py",
        max_lines=150,
        reason="Telegram OpenCode usage helpers should remain centralized in one small module.",
    ),
    FileBudget(
        path="src/codex_autorunner/tickets/runner_types.py",
        max_lines=285,
        reason="Typed runner state belongs in a compact seam, not another catch-all module.",
    ),
    FileBudget(
        path="src/codex_autorunner/tickets/runner_selection.py",
        max_lines=640,
        reason="Ticket selection should stay extracted from TicketRunner.step().",
    ),
    FileBudget(
        path="src/codex_autorunner/tickets/runner_execution.py",
        max_lines=230,
        reason="Turn execution should stay extracted from TicketRunner.step().",
    ),
    FileBudget(
        path="src/codex_autorunner/tickets/runner_post_turn.py",
        max_lines=466,
        reason="Post-turn reconciliation should stay extracted from TicketRunner.step().",
    ),
    FileBudget(
        path="src/codex_autorunner/tickets/runner_prompt.py",
        max_lines=360,
        reason="Prompt assembly should stay extracted from TicketRunner.step().",
    ),
)

STANDARD_FUNCTION_BUDGETS = (
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/flow_routes/status_history_routes.py",
        qualname="build_status_history_routes",
        max_lines=300,
        reason="The extracted flow router builder should remain a composition layer.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat_routes/execution.py",
        qualname="execute_file_chat",
        max_lines=160,
        reason="File-chat execution should stay outside the legacy route monolith.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat_routes/execution.py",
        qualname="execute_app_server",
        max_lines=105,
        reason="App-server execution handling should stay split out of file_chat.py.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat_routes/execution.py",
        qualname="execute_opencode",
        max_lines=160,
        reason="OpenCode execution handling should stay split out of file_chat.py.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/pma.py",
        qualname="build_pma_routes",
        max_lines=40,
        reason="The PMA router entrypoint should remain a thin composition function.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/chat/command_diagnostics.py",
        qualname="build_status_text",
        max_lines=60,
        reason="Shared status text assembly should stay extracted and focused.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/chat/command_diagnostics.py",
        qualname="build_debug_text",
        max_lines=55,
        reason="Shared debug text assembly should stay extracted and focused.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/chat/command_diagnostics.py",
        qualname="build_ids_text",
        max_lines=40,
        reason="Shared ids text assembly should stay extracted and focused.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/discord/pma_commands.py",
        qualname="handle_pma_on",
        max_lines=50,
        reason="PMA subcommands should stay small after extraction.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/discord/pma_commands.py",
        qualname="handle_pma_off",
        max_lines=50,
        reason="PMA subcommands should stay small after extraction.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/discord/pma_commands.py",
        qualname="handle_pma_status",
        max_lines=45,
        reason="PMA subcommands should stay small after extraction.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/chat/progress_primitives.py",
        qualname="render_progress_text",
        max_lines=210,
        reason="Cross-surface progress rendering should stay centralized without regrowing another monolith.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/chat/progress_primitives.py",
        qualname="TurnProgressTracker.add_action",
        max_lines=60,
        reason="Progress tracker mutation should stay compact and auditable.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/telegram/handlers/utils.py",
        qualname="_build_opencode_token_usage",
        max_lines=60,
        reason="Telegram OpenCode usage normalization should stay centralized in one helper.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_selection.py",
        qualname="select_ticket",
        max_lines=115,
        reason="Ticket selection should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_selection.py",
        qualname="validate_ticket_for_execution",
        max_lines=60,
        reason="Ticket validation should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_execution.py",
        qualname="execute_turn",
        max_lines=60,
        reason="Turn execution should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_execution.py",
        qualname="compute_loop_guard",
        max_lines=55,
        reason="Loop-guard logic should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_post_turn.py",
        qualname="archive_dispatch_and_create_summary",
        max_lines=85,
        reason="Dispatch archival should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_post_turn.py",
        qualname="process_commit_required",
        max_lines=65,
        reason="Commit gating should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_post_turn.py",
        qualname="build_pause_result",
        max_lines=50,
        reason="Pause-result assembly should remain extracted from TicketRunner.step().",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner_prompt.py",
        qualname="build_prompt",
        max_lines=230,
        reason="Prompt assembly should remain extracted from TicketRunner.step().",
    ),
)

TEST_FILE_CAPS = (
    FileBudget(
        path="tests/discord_message_turns_support.py",
        max_lines=9815,
        reason="The extracted Discord message-turn support module is still large, but obvious regrowth should fail while the split layout settles.",
    ),
    FileBudget(
        path="tests/telegram_pma_routing_support.py",
        max_lines=7305,
        reason="The extracted Telegram PMA routing support module is still large, but obvious regrowth should fail while follow-on splits land.",
    ),
    FileBudget(
        path="tests/fixtures/telegram_command_helpers.py",
        max_lines=60,
        reason="Telegram command test helpers should stay as a tiny shared fixture seam.",
    ),
    FileBudget(
        path="tests/integrations/discord/test_message_turns.py",
        max_lines=60,
        reason="The Discord message-turn wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/integrations/discord/test_message_turns_routing.py",
        max_lines=60,
        reason="The Discord routing wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/integrations/discord/test_message_turns_message_flow.py",
        max_lines=60,
        reason="The Discord message-flow wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/integrations/discord/test_message_turns_streaming.py",
        max_lines=60,
        reason="The Discord streaming wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/integrations/discord/test_message_turns_managed_threads.py",
        max_lines=50,
        reason="The Discord managed-thread wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/test_telegram_pma_routing.py",
        max_lines=40,
        reason="The Telegram PMA routing wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/test_telegram_pma_managed_threads.py",
        max_lines=40,
        reason="The Telegram PMA managed-thread wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
    FileBudget(
        path="tests/test_telegram_pma_workspace_commands.py",
        max_lines=60,
        reason="The Telegram PMA workspace wrapper should stay a thin import-only entrypoint over the shared support module.",
    ),
)

LEGACY_FILE_CAPS = (
    FileBudget(
        path="src/codex_autorunner/surfaces/web/routes/flows.py",
        max_lines=1825,
        reason="Legacy flow composition owner until more /api/flows CRUD, lifecycle, and ticket-diff aggregation routes are extracted; keep the merge-ref shape under a narrow ceiling.",
    ),
    FileBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat.py",
        max_lines=1200,
        reason="Legacy file-chat composition owner until the remaining route wrappers migrate fully to file_chat_routes/.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/discord/commands.py",
        max_lines=560,
        reason="Discord slash-command payload still lives in one registry builder pending a later breakup.",
    ),
    FileBudget(
        path="src/codex_autorunner/integrations/discord/car_command_dispatch.py",
        max_lines=475,
        reason="Discord car command routing is still centralized while migrated command slices accumulate.",
    ),
    FileBudget(
        path="src/codex_autorunner/tickets/runner.py",
        max_lines=1185,
        reason="TicketRunner still owns remaining orchestration glue while extracted runner seams stabilize.",
    ),
)

LEGACY_FUNCTION_CAPS = (
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/flows.py",
        qualname="build_flow_routes",
        max_lines=1150,
        reason="Legacy flow route builder should not grow while extracted flow_routes slices exist.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/file_chat.py",
        qualname="build_file_chat_routes",
        max_lines=980,
        reason="Legacy file-chat route builder should not grow while file_chat_routes slices exist.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/pma_routes/chat_runtime.py",
        qualname="build_chat_runtime_router",
        max_lines=350,
        reason="PMA chat runtime still has a large legacy builder, but it should not get materially larger before extraction.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/surfaces/web/routes/pma_routes/managed_thread_runtime.py",
        qualname="build_managed_thread_runtime_routes",
        max_lines=850,
        reason="Managed-thread PMA routing is still a legacy hotspot pending a dedicated extraction pass.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/discord/commands.py",
        qualname="build_application_commands",
        max_lines=560,
        reason="The slash-command schema builder is still centralized, but obvious regrowth should fail.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/integrations/discord/car_command_dispatch.py",
        qualname="handle_car_command",
        max_lines=475,
        reason="The Discord car command dispatcher is still centralized, but obvious regrowth should fail.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner.py",
        qualname="TicketRunner.step",
        max_lines=625,
        reason="TicketRunner.step() is still a legacy orchestration hotspot, but new growth should fail.",
    ),
    FunctionBudget(
        path="src/codex_autorunner/tickets/runner.py",
        qualname="TicketRunner._build_prompt",
        max_lines=240,
        reason="The remaining prompt glue in runner.py should keep shrinking toward runner_prompt.py, not regrow.",
    ),
)

HELPER_OWNERSHIP_RULES = (
    HelperOwnershipRule(
        owner_path="src/codex_autorunner/integrations/chat/progress_primitives.py",
        helper_names=(
            "format_elapsed",
            "_normalize_inline_text",
            "_normalize_output_text",
            "_truncate_tail",
            "_merge_output_text",
            "_output_matches_final_message",
        ),
        scan_roots=(
            "src/codex_autorunner/integrations/chat",
            "src/codex_autorunner/integrations/telegram",
        ),
        reason="Progress rendering helpers should have one cross-surface owner.",
    ),
    HelperOwnershipRule(
        owner_path="src/codex_autorunner/integrations/telegram/handlers/utils.py",
        helper_names=(
            "_flatten_opencode_tokens",
            "_extract_opencode_usage_payload",
            "_extract_opencode_usage_value",
            "_build_opencode_token_usage",
        ),
        scan_roots=("src/codex_autorunner/integrations/telegram/handlers",),
        reason="Telegram OpenCode usage helpers should stay centralized in handlers/utils.py.",
    ),
    HelperOwnershipRule(
        owner_path="tests/fixtures/telegram_command_helpers.py",
        helper_names=(
            "README_REVISIT_GUIDANCE_MIN_MODULE_THRESHOLD",
            "noop_handler",
            "bot_command_entity",
            "make_command_spec",
        ),
        scan_roots=("tests",),
        reason="Shared Telegram command-test helpers should stay owned by tests/fixtures/telegram_command_helpers.py.",
    ),
)


def _repo_path(path: str) -> Path:
    return REPO_ROOT / path


def _line_count(path: str) -> int:
    return len(_repo_path(path).read_text(encoding="utf-8").splitlines())


@lru_cache(maxsize=None)
def _module_ast(path: str) -> ast.Module:
    return ast.parse(_repo_path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _function_spans(path: str) -> dict[str, int]:
    module = _module_ast(path)
    spans: dict[str, int] = {}

    def visit(node: ast.AST, prefix: tuple[str, ...] = ()) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, (*prefix, child.name))
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, child.name))
                if child.end_lineno is not None:
                    spans[qualname] = child.end_lineno - child.lineno + 1
                visit(child, (*prefix, child.name))
                continue
            visit(child, prefix)

    visit(module)
    return spans


def _assignment_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_assignment_names(element))
        return names
    return set()


@lru_cache(maxsize=None)
def _top_level_symbols(path: str) -> set[str]:
    module = _module_ast(path)
    symbols: set[str] = set()
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                symbols.update(_assignment_names(target))
            continue
        if isinstance(node, ast.AnnAssign):
            symbols.update(_assignment_names(node.target))
    return symbols


def _check_file_budgets(budgets: tuple[FileBudget, ...], *, label: str) -> list[str]:
    failures: list[str] = []
    for budget in budgets:
        current = _line_count(budget.path)
        if current > budget.max_lines:
            failures.append(
                f"{label} file cap exceeded: {budget.path} has {current} lines "
                f"(cap {budget.max_lines}). {budget.reason}"
            )
    return failures


def _check_function_budgets(
    budgets: tuple[FunctionBudget, ...], *, label: str
) -> list[str]:
    failures: list[str] = []
    for budget in budgets:
        spans = _function_spans(budget.path)
        current = spans.get(budget.qualname)
        if current is None:
            failures.append(
                f"{label} function missing: {budget.path} does not define "
                f"{budget.qualname}. {budget.reason}"
            )
            continue
        if current > budget.max_lines:
            failures.append(
                f"{label} function cap exceeded: {budget.path}:{budget.qualname} has "
                f"{current} lines (cap {budget.max_lines}). {budget.reason}"
            )
    return failures


def _check_helper_ownership() -> list[str]:
    failures: list[str] = []
    for rule in HELPER_OWNERSHIP_RULES:
        owner = rule.owner_path
        owner_symbols = _top_level_symbols(owner)
        for helper_name in rule.helper_names:
            locations: list[str] = []
            if helper_name in owner_symbols:
                locations.append(owner)
            for root in rule.scan_roots:
                for path in sorted(_repo_path(root).rglob("*.py")):
                    relative = str(path.relative_to(REPO_ROOT))
                    if relative == owner:
                        continue
                    if helper_name in _top_level_symbols(relative):
                        locations.append(relative)
            if not locations:
                failures.append(
                    f"Helper owner missing: {owner} does not define {helper_name}. "
                    f"{rule.reason}"
                )
                continue
            if locations != [owner]:
                failures.append(
                    f"Helper ownership violated for {helper_name}: found in "
                    f"{', '.join(locations)}. Owner must be {owner}. {rule.reason}"
                )
    return failures


def _fail_if_any(failures: list[str]) -> None:
    if failures:
        pytest.fail("\n".join(f"- {failure}" for failure in failures))


def test_hotspot_file_budgets() -> None:
    failures = []
    failures.extend(_check_file_budgets(STANDARD_FILE_BUDGETS, label="Budget"))
    failures.extend(_check_file_budgets(TEST_FILE_CAPS, label="Test cap"))
    failures.extend(_check_file_budgets(LEGACY_FILE_CAPS, label="Legacy cap"))
    _fail_if_any(failures)


def test_hotspot_function_budgets() -> None:
    failures = []
    failures.extend(_check_function_budgets(STANDARD_FUNCTION_BUDGETS, label="Budget"))
    failures.extend(_check_function_budgets(LEGACY_FUNCTION_CAPS, label="Legacy cap"))
    _fail_if_any(failures)


def test_hotspot_helper_ownership() -> None:
    _fail_if_any(_check_helper_ownership())
