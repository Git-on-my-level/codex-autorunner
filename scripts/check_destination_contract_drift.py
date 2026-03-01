#!/usr/bin/env python3

"""Detect drift between destination/profile code contract and docs contract."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Import after PYTHONPATH adjustment when running script directly from repo root.
from codex_autorunner.core.destinations import parse_destination_config  # noqa: E402
from codex_autorunner.integrations.docker.profile_contracts import (  # noqa: E402
    FULL_DEV_PROFILE_CONTRACT,
    SUPPORTED_DOCKER_PROFILES,
)

CONTRACT_BEGIN = "<!-- CAR_DESTINATION_CONTRACT:BEGIN -->"
CONTRACT_END = "<!-- CAR_DESTINATION_CONTRACT:END -->"

REQUIRED_DESTINATIONS_DOC_SNIPPETS = (
    "`profile`",
    "`workdir`",
    "`env`",
    "`read_only`",
    "`full-dev`",
    "hub-manifest-schema.md",
)


def _extract_contract_yaml(text: str) -> Dict[str, Any]:
    start = text.find(CONTRACT_BEGIN)
    end = text.find(CONTRACT_END)
    if start < 0 or end < 0 or end <= start:
        raise ValueError(
            f"missing contract block markers: {CONTRACT_BEGIN} ... {CONTRACT_END}"
        )
    raw = text[start + len(CONTRACT_BEGIN) : end].strip()
    match = re.search(r"```(?:yaml|yml)\s*(.*?)```", raw, flags=re.DOTALL)
    yaml_text = (match.group(1) if match else raw).strip()
    payload = yaml.safe_load(yaml_text)
    if not isinstance(payload, dict):
        raise ValueError("contract block must decode to a mapping")
    return payload


def _check_parser_contract() -> List[str]:
    issues: List[str] = []
    context = "destination contract drift check"

    valid_payload = {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "profile": "full-dev",
        "workdir": "/workspace",
        "env_passthrough": ["CAR_*"],
        "env": {"OPENAI_API_KEY": "sk-test"},
        "mounts": [
            {"source": "/tmp/src", "target": "/workspace/src", "read_only": True}
        ],
    }
    parsed_valid = parse_destination_config(valid_payload, context=context)
    if not parsed_valid.valid:
        issues.append(
            "docker destination parser rejected valid profile/workdir/env/mount fields"
        )

    bad_profile = parse_destination_config(
        {"kind": "docker", "image": "busybox:latest", "profile": "unknown"},
        context=context,
    )
    if bad_profile.valid:
        issues.append("docker destination parser unexpectedly accepted unknown profile")

    bad_mount_flag = parse_destination_config(
        {
            "kind": "docker",
            "image": "busybox:latest",
            "mounts": [
                {
                    "source": "/tmp/src",
                    "target": "/workspace/src",
                    "read_only": "yes",
                }
            ],
        },
        context=context,
    )
    if bad_mount_flag.valid:
        issues.append(
            "docker destination parser unexpectedly accepted non-boolean read_only"
        )

    bad_env_map = parse_destination_config(
        {"kind": "docker", "image": "busybox:latest", "env": {"OPENAI_API_KEY": 123}},
        context=context,
    )
    if bad_env_map.valid:
        issues.append(
            "docker destination parser unexpectedly accepted non-string env map values"
        )

    return issues


def _code_contract() -> Dict[str, Any]:
    return {
        "supported_destination_kinds": ["local", "docker"],
        "docker": {
            "required_fields": ["kind", "image"],
            "optional_fields": [
                "container_name",
                "mounts",
                "env_passthrough",
                "workdir",
                "profile",
                "env",
            ],
            "mount": {
                "required_fields": ["source", "target"],
                "optional_fields": ["read_only"],
                "read_only_type": "boolean",
            },
            "env": {
                "key_type": "string",
                "value_type": "string",
            },
            "profiles": {
                "supported": list(SUPPORTED_DOCKER_PROFILES),
            },
        },
        "profiles": {
            FULL_DEV_PROFILE_CONTRACT.name: {
                "required_binaries": list(FULL_DEV_PROFILE_CONTRACT.required_binaries),
                "required_auth_files": list(
                    FULL_DEV_PROFILE_CONTRACT.required_auth_files
                ),
                "default_env_passthrough": list(
                    FULL_DEV_PROFILE_CONTRACT.default_env_passthrough
                ),
                "default_mounts": [
                    {
                        "source": mount.source,
                        "target": mount.target,
                        "read_only": bool(mount.read_only),
                    }
                    for mount in FULL_DEV_PROFILE_CONTRACT.default_mounts
                ],
            }
        },
    }


def _canonicalize_string_list(values: Iterable[Any]) -> List[str]:
    normalized = [str(item).strip() for item in values if str(item).strip()]
    return sorted(set(normalized))


def _canonicalize_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(contract)
    out["supported_destination_kinds"] = _canonicalize_string_list(
        contract.get("supported_destination_kinds", [])
    )

    docker = dict(contract.get("docker") or {})
    docker["required_fields"] = _canonicalize_string_list(
        docker.get("required_fields", [])
    )
    docker["optional_fields"] = _canonicalize_string_list(
        docker.get("optional_fields", [])
    )

    mount = dict(docker.get("mount") or {})
    mount["required_fields"] = _canonicalize_string_list(
        mount.get("required_fields", [])
    )
    mount["optional_fields"] = _canonicalize_string_list(
        mount.get("optional_fields", [])
    )
    docker["mount"] = mount

    env = dict(docker.get("env") or {})
    env["key_type"] = str(env.get("key_type", "")).strip()
    env["value_type"] = str(env.get("value_type", "")).strip()
    docker["env"] = env

    profiles = dict(docker.get("profiles") or {})
    profiles["supported"] = _canonicalize_string_list(profiles.get("supported", []))
    docker["profiles"] = profiles
    out["docker"] = docker

    normalized_profiles: Dict[str, Any] = {}
    for profile_name, profile_payload in sorted(
        (contract.get("profiles") or {}).items()
    ):
        if not isinstance(profile_payload, dict):
            continue
        payload = dict(profile_payload)
        payload["required_binaries"] = _canonicalize_string_list(
            payload.get("required_binaries", [])
        )
        payload["required_auth_files"] = _canonicalize_string_list(
            payload.get("required_auth_files", [])
        )
        payload["default_env_passthrough"] = _canonicalize_string_list(
            payload.get("default_env_passthrough", [])
        )
        default_mounts = []
        for mount_item in payload.get("default_mounts", []):
            if not isinstance(mount_item, dict):
                continue
            source = str(mount_item.get("source", "")).strip()
            target = str(mount_item.get("target", "")).strip()
            if not source or not target:
                continue
            default_mounts.append(
                {
                    "source": source,
                    "target": target,
                    "read_only": bool(mount_item.get("read_only", False)),
                }
            )
        payload["default_mounts"] = sorted(
            default_mounts,
            key=lambda item: (item["source"], item["target"], item["read_only"]),
        )
        normalized_profiles[str(profile_name).strip()] = payload
    out["profiles"] = normalized_profiles
    return out


def _destinations_doc_issues(destinations_doc: Path) -> List[str]:
    text = destinations_doc.read_text(encoding="utf-8")
    missing = [
        snippet for snippet in REQUIRED_DESTINATIONS_DOC_SNIPPETS if snippet not in text
    ]
    if not missing:
        return []
    return [
        "destinations doc is missing required docker-contract snippets: "
        + ", ".join(sorted(missing))
    ]


def run_check(
    *,
    destinations_doc: Path,
    schema_doc: Path,
) -> List[str]:
    issues: List[str] = []

    parser_issues = _check_parser_contract()
    if parser_issues:
        issues.extend(parser_issues)
        return issues

    if not destinations_doc.exists():
        issues.append(f"missing destinations doc: {destinations_doc}")
        return issues
    if not schema_doc.exists():
        issues.append(f"missing schema doc: {schema_doc}")
        return issues

    issues.extend(_destinations_doc_issues(destinations_doc))

    try:
        docs_contract = _extract_contract_yaml(schema_doc.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(f"failed to parse docs contract block: {exc}")
        return issues

    expected = _canonicalize_contract(_code_contract())
    actual = _canonicalize_contract(docs_contract)
    if expected != actual:
        expected_yaml = yaml.safe_dump(expected, sort_keys=True).splitlines()
        actual_yaml = yaml.safe_dump(actual, sort_keys=True).splitlines()
        diff = "\n".join(
            difflib.unified_diff(
                expected_yaml,
                actual_yaml,
                fromfile="code_contract",
                tofile="docs_contract",
                lineterm="",
            )
        )
        issues.append("destination contract drift detected between code and docs")
        if diff:
            issues.append(diff)
    return issues


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check destination/profile docs contract drift."
    )
    parser.add_argument(
        "--destinations-doc",
        type=Path,
        default=REPO_ROOT / "docs" / "configuration" / "destinations.md",
    )
    parser.add_argument(
        "--schema-doc",
        type=Path,
        default=REPO_ROOT / "docs" / "reference" / "hub-manifest-schema.md",
    )
    args = parser.parse_args(argv)

    issues = run_check(
        destinations_doc=args.destinations_doc,
        schema_doc=args.schema_doc,
    )
    if issues:
        print("Destination contract drift check failed:")
        for item in issues:
            print(f"- {item}")
        return 1

    print("Destination contract drift check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
