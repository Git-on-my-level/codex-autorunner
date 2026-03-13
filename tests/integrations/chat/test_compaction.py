from __future__ import annotations

from codex_autorunner.integrations.chat.compaction import (
    COMPACT_SEED_PREFIX,
    COMPACT_SEED_SUFFIX,
    build_compact_seed_prompt,
    match_pending_compact_seed,
)


def test_build_compact_seed_prompt_uses_shared_format() -> None:
    prompt = build_compact_seed_prompt("goals and state")
    assert prompt.startswith(COMPACT_SEED_PREFIX)
    assert "goals and state" in prompt
    assert prompt.endswith(COMPACT_SEED_SUFFIX)


def test_match_pending_compact_seed_requires_matching_target_when_present() -> None:
    seed = match_pending_compact_seed(
        "seed",
        pending_target_id="thread-1",
        active_target_id="thread-1",
    )
    assert seed == "seed"
    assert (
        match_pending_compact_seed(
            "seed",
            pending_target_id="thread-1",
            active_target_id="thread-2",
        )
        is None
    )


def test_match_pending_compact_seed_allows_global_target_by_default() -> None:
    assert (
        match_pending_compact_seed(
            "seed",
            pending_target_id=None,
            active_target_id="thread-1",
        )
        == "seed"
    )
