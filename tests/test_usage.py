import json
from pathlib import Path
from typing import Optional

from codex_autorunner.core.usage import (
    get_hub_usage_series_cached,
    get_hub_usage_summary_cached,
    get_repo_usage_series_cached,
    get_repo_usage_summary_cached,
    get_usage_series_cache,
    summarize_hub_usage,
    summarize_repo_usage,
)


def _write_session(
    tmp_path: Path, cwd: Path, events: list[dict], model: Optional[str] = None
) -> None:
    target = tmp_path / "sessions" / "2025" / "12" / "01"
    target.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": "2025-12-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": str(cwd), "model": model},
        }
    ]
    lines.extend(events)
    existing = list(target.glob("*.jsonl"))
    session_path = target / f"session-{len(existing)}.jsonl"
    session_path.write_text("\n".join(json.dumps(entry) for entry in lines) + "\n")


def _refresh_usage_cache(codex_home: Path) -> None:
    cache = get_usage_series_cache(codex_home)
    payload = cache._load_cache()
    cache._update_cache(payload)


def test_summarize_repo_usage_reads_token_deltas(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        repo_root,
        [
            {
                "timestamp": "2025-12-01T00:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 15,
                        },
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 15,
                        },
                    },
                },
            },
            {
                "timestamp": "2025-12-01T00:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 18,
                            "cached_input_tokens": 2,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 25,
                        }
                    },
                },
            },
        ],
    )

    summary = summarize_repo_usage(repo_root, codex_home=codex_home)

    assert summary.events == 2
    assert summary.totals.input_tokens == 18
    assert summary.totals.cached_input_tokens == 2
    assert summary.totals.output_tokens == 5
    assert summary.totals.total_tokens == 25


def test_hub_usage_assigns_unmatched(tmp_path):
    hub_repo_one = tmp_path / "hub_repo_one"
    hub_repo_two = tmp_path / "hub_repo_two"
    hub_repo_one.mkdir()
    hub_repo_two.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        hub_repo_one / "subdir",
        [
            {
                "timestamp": "2025-12-01T01:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                        "last_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                    },
                },
            }
        ],
    )

    # Event that should not match either repo
    _write_session(
        codex_home,
        tmp_path / "other",
        [
            {
                "timestamp": "2025-12-01T01:05:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 3,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 4,
                        },
                        "last_token_usage": {
                            "input_tokens": 3,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 4,
                        },
                    },
                },
            }
        ],
    )

    per_repo, unmatched = summarize_hub_usage(
        [("repo-one", hub_repo_one), ("repo-two", hub_repo_two)],
        codex_home=codex_home,
    )

    assert per_repo["repo-one"].totals.total_tokens == 7
    assert per_repo["repo-two"].totals.total_tokens == 0
    assert unmatched.totals.total_tokens == 4
    assert unmatched.events == 1


def test_hub_usage_heuristic_rolls_worktree_into_base(tmp_path):
    base_repo = tmp_path / "codex-autorunner"
    stray_worktree = tmp_path / "codex-autorunner--feature-x"
    base_repo.mkdir()
    stray_worktree.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        stray_worktree,
        [
            {
                "timestamp": "2025-12-01T01:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 15,
                        },
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 15,
                        },
                    },
                },
            }
        ],
    )

    per_repo, unmatched = summarize_hub_usage(
        [("codex-autorunner", base_repo)],
        codex_home=codex_home,
    )

    assert per_repo["codex-autorunner"].totals.total_tokens == 15
    assert per_repo["codex-autorunner"].events == 1
    assert unmatched.events == 0


def test_hub_usage_heuristic_skips_unrelated_double_dash(tmp_path):
    base_repo = tmp_path / "codex-autorunner"
    unrelated = tmp_path / "another--repo"
    base_repo.mkdir()
    unrelated.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        unrelated,
        [
            {
                "timestamp": "2025-12-01T02:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 6,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 8,
                        },
                        "last_token_usage": {
                            "input_tokens": 6,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 8,
                        },
                    },
                },
            }
        ],
    )

    per_repo, unmatched = summarize_hub_usage(
        [("codex-autorunner", base_repo)],
        codex_home=codex_home,
    )

    assert per_repo["codex-autorunner"].events == 0
    assert unmatched.events == 1
    assert unmatched.totals.total_tokens == 8


def test_usage_series_groups_by_model(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        repo_root,
        [
            {
                "timestamp": "2025-12-01T00:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 6,
                            "cached_input_tokens": 0,
                            "output_tokens": 4,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 10,
                        },
                        "last_token_usage": {
                            "input_tokens": 6,
                            "cached_input_tokens": 0,
                            "output_tokens": 4,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 10,
                        },
                    },
                },
            }
        ],
        model="gpt-5",
    )

    _write_session(
        codex_home,
        repo_root,
        [
            {
                "timestamp": "2025-12-02T00:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 2,
                            "cached_input_tokens": 0,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 5,
                        },
                        "last_token_usage": {
                            "input_tokens": 2,
                            "cached_input_tokens": 0,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 5,
                        },
                    },
                },
            }
        ],
        model="gpt-4o",
    )

    _refresh_usage_cache(codex_home)
    series, status = get_repo_usage_series_cached(
        repo_root, codex_home=codex_home, bucket="day", segment="model"
    )

    assert status == "ready"
    assert series["buckets"] == ["2025-12-01", "2025-12-02"]
    values = {item["key"]: item["values"] for item in series["series"]}
    assert values["gpt-5"] == [10, 0]
    assert values["gpt-4o"] == [0, 5]


def test_hub_usage_series_includes_unmatched(tmp_path):
    hub_repo_one = tmp_path / "hub_repo_one"
    hub_repo_one.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        hub_repo_one,
        [
            {
                "timestamp": "2025-12-01T01:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                        "last_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                    },
                },
            }
        ],
        model="gpt-5",
    )

    _write_session(
        codex_home,
        tmp_path / "other",
        [
            {
                "timestamp": "2025-12-02T01:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 2,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 3,
                        },
                        "last_token_usage": {
                            "input_tokens": 2,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 3,
                        },
                    },
                },
            }
        ],
    )

    _refresh_usage_cache(codex_home)
    series, status = get_hub_usage_series_cached(
        [("repo-one", hub_repo_one)],
        codex_home=codex_home,
        bucket="day",
        segment="repo",
    )

    assert status == "ready"
    values = {item["key"]: item["values"] for item in series["series"]}
    assert series["buckets"] == ["2025-12-01", "2025-12-02"]
    assert values["repo-one"] == [7, 0]
    assert values["other"] == [0, 3]


def test_repo_usage_summary_cache_matches_baseline(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    codex_home = tmp_path / "codex"

    rate_limits = {"primary": {"used_percent": 80, "window_minutes": 5}}
    _write_session(
        codex_home,
        repo_root,
        [
            {
                "timestamp": "2025-12-01T00:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": rate_limits,
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 1,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 13,
                        },
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 1,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 13,
                        },
                    },
                },
            }
        ],
    )

    baseline = summarize_repo_usage(repo_root, codex_home=codex_home)
    _refresh_usage_cache(codex_home)
    cached, status = get_repo_usage_summary_cached(repo_root, codex_home=codex_home)

    assert status == "ready"
    assert cached.events == baseline.events
    assert cached.totals.total_tokens == baseline.totals.total_tokens
    assert cached.latest_rate_limits == baseline.latest_rate_limits


def test_hub_usage_summary_cache_matches_baseline(tmp_path):
    hub_repo_one = tmp_path / "hub_repo_one"
    hub_repo_two = tmp_path / "hub_repo_two"
    hub_repo_one.mkdir()
    hub_repo_two.mkdir()
    codex_home = tmp_path / "codex"

    _write_session(
        codex_home,
        hub_repo_one,
        [
            {
                "timestamp": "2025-12-01T01:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                        "last_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 0,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                    },
                },
            }
        ],
    )

    _write_session(
        codex_home,
        tmp_path / "other",
        [
            {
                "timestamp": "2025-12-01T02:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 3,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 4,
                        },
                        "last_token_usage": {
                            "input_tokens": 3,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 4,
                        },
                    },
                },
            }
        ],
    )

    baseline_per_repo, baseline_unmatched = summarize_hub_usage(
        [("repo-one", hub_repo_one), ("repo-two", hub_repo_two)],
        codex_home=codex_home,
    )
    _refresh_usage_cache(codex_home)
    cached_per_repo, cached_unmatched, status = get_hub_usage_summary_cached(
        [("repo-one", hub_repo_one), ("repo-two", hub_repo_two)],
        codex_home=codex_home,
    )

    assert status == "ready"
    assert (
        cached_per_repo["repo-one"].totals.total_tokens
        == baseline_per_repo["repo-one"].totals.total_tokens
    )
    assert (
        cached_per_repo["repo-two"].totals.total_tokens
        == baseline_per_repo["repo-two"].totals.total_tokens
    )
    assert cached_unmatched.events == baseline_unmatched.events
