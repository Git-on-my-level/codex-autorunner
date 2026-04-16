from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_PROFILES_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "idle_cpu_profiles.yml"
)

_REQUIRED_TOP_KEYS = {"version", "defaults", "profiles", "artifact_contract"}

_REQUIRED_PROFILE_KEYS = {
    "description",
    "services",
    "hard_budget",
    "expected_owned_process_categories",
}

_REQUIRED_SERVICE_KEYS = {"command", "alias"}

_REQUIRED_HEALTH_PROBE_KEYS = {"method", "path", "timeout_seconds", "retries"}

_REQUIRED_ARTIFACT_CONTRACT_KEYS = {"directory", "filename_pattern", "schema"}

_FIRST_CAMPAIGN_PROFILES_SET = {
    "hub_only",
    "hub_plus_discord",
    "hub_plus_telegram",
    "hub_with_idle_runtime",
}

_FIRST_CAMPAIGN_PROFILES = tuple(sorted(_FIRST_CAMPAIGN_PROFILES_SET))

_HARD_BUDGET_KEYS = {"enabled"}

_COMPARISON_BUDGET_KEYS = {"max_aggregate_cpu_percent"}


def _load_profiles() -> dict:
    return yaml.safe_load(_PROFILES_PATH.read_text(encoding="utf-8"))


class TestIdleCpuProfilesFormat:
    def test_file_exists(self):
        assert _PROFILES_PATH.is_file(), f"Missing profile file: {_PROFILES_PATH}"

    def test_valid_yaml(self):
        data = _load_profiles()
        assert isinstance(data, dict)

    def test_top_level_keys(self):
        data = _load_profiles()
        missing = _REQUIRED_TOP_KEYS - set(data.keys())
        assert not missing, f"Missing top-level keys: {missing}"

    def test_version_is_integer(self):
        data = _load_profiles()
        assert isinstance(data["version"], int)

    def test_defaults_has_required_keys(self):
        data = _load_profiles()
        defaults = data.get("defaults", {})
        for key in ("warmup_seconds", "duration_seconds", "sample_interval_seconds"):
            assert key in defaults, f"defaults missing key: {key}"
            assert isinstance(
                defaults[key], (int, float)
            ), f"defaults.{key} must be numeric"

    def test_defaults_health_probe(self):
        data = _load_profiles()
        probe = data.get("defaults", {}).get("health_probe", {})
        missing = _REQUIRED_HEALTH_PROBE_KEYS - set(probe.keys())
        assert not missing, f"health_probe missing keys: {missing}"

    def test_defaults_owned_process_categories(self):
        data = _load_profiles()
        cats = data.get("defaults", {}).get("owned_process_categories", [])
        assert isinstance(cats, list) and len(cats) > 0
        for cat in cats:
            assert isinstance(cat, str)


class TestIdleCpuProfileDefinitions:
    def test_all_first_campaign_profiles_present(self):
        data = _load_profiles()
        profiles = set(data.get("profiles", {}).keys())
        missing = _FIRST_CAMPAIGN_PROFILES_SET - profiles
        assert not missing, f"Missing profiles: {missing}"

    @pytest.mark.parametrize("profile_name", _FIRST_CAMPAIGN_PROFILES)
    def test_profile_has_required_keys(self, profile_name: str):
        data = _load_profiles()
        profile = data["profiles"][profile_name]
        missing = _REQUIRED_PROFILE_KEYS - set(profile.keys())
        assert not missing, f"{profile_name} missing keys: {missing}"

    @pytest.mark.parametrize("profile_name", _FIRST_CAMPAIGN_PROFILES)
    def test_profile_services_structure(self, profile_name: str):
        data = _load_profiles()
        services = data["profiles"][profile_name]["services"]
        assert isinstance(services, list) and len(services) > 0
        for svc in services:
            missing = _REQUIRED_SERVICE_KEYS - set(svc.keys())
            assert not missing, f"{profile_name} service missing keys: {missing}"

    @pytest.mark.parametrize("profile_name", _FIRST_CAMPAIGN_PROFILES)
    def test_profile_expected_categories_are_strings(self, profile_name: str):
        data = _load_profiles()
        cats = data["profiles"][profile_name]["expected_owned_process_categories"]
        assert isinstance(cats, list)
        for cat in cats:
            assert isinstance(cat, str)

    def test_hub_only_is_hard_budget(self):
        data = _load_profiles()
        hb = data["profiles"]["hub_only"]["hard_budget"]
        assert hb["enabled"] is True
        assert isinstance(hb["max_aggregate_cpu_percent"], (int, float))
        assert hb["max_aggregate_cpu_percent"] <= 5.0

    def test_comparison_profiles_are_not_hard_budget(self):
        data = _load_profiles()
        comparison_profiles = _FIRST_CAMPAIGN_PROFILES_SET - {"hub_only"}
        for name in comparison_profiles:
            hb = data["profiles"][name]["hard_budget"]
            assert hb["enabled"] is False, f"{name} should not be a hard budget"


class TestArtifactContract:
    def test_artifact_directory_is_diagnostics_idle_cpu(self):
        data = _load_profiles()
        directory = data["artifact_contract"]["directory"]
        assert directory == ".codex-autorunner/diagnostics/idle-cpu/"

    def test_artifact_contract_has_required_keys(self):
        data = _load_profiles()
        contract = data["artifact_contract"]
        missing = _REQUIRED_ARTIFACT_CONTRACT_KEYS - set(contract.keys())
        assert not missing, f"artifact_contract missing keys: {missing}"

    def test_artifact_schema_has_signoff(self):
        data = _load_profiles()
        schema = data["artifact_contract"]["schema"]
        assert "signoff" in schema, "artifact schema must include signoff"
        signoff = schema["signoff"]
        for key in ("passed", "budget_type", "actual_aggregate_cpu_percent", "message"):
            assert key in signoff, f"signoff missing key: {key}"

    def test_artifact_schema_has_aggregate_metrics(self):
        data = _load_profiles()
        schema = data["artifact_contract"]["schema"]
        assert "aggregate_metrics" in schema
        metrics = schema["aggregate_metrics"]
        for key in (
            "car_owned_cpu_mean_percent",
            "car_owned_cpu_max_percent",
            "car_owned_cpu_p95_percent",
            "car_owned_cpu_samples",
        ):
            assert key in metrics, f"aggregate_metrics missing key: {key}"

    def test_artifact_schema_has_per_process_metrics(self):
        data = _load_profiles()
        schema = data["artifact_contract"]["schema"]
        assert "per_process_metrics" in schema

    def test_artifact_schema_has_health_probe_summary(self):
        data = _load_profiles()
        schema = data["artifact_contract"]["schema"]
        assert "health_probe_summary" in schema

    def test_artifact_schema_has_environment(self):
        data = _load_profiles()
        schema = data["artifact_contract"]["schema"]
        assert "environment" in schema


class TestMalformedProfileRejection:
    def test_missing_profile_key_rejected(self, tmp_path: Path):
        malformed = {
            "version": 1,
            "defaults": {},
            "profiles": {
                "bad_profile": {
                    "services": [{"command": "car hub serve", "alias": "hub"}],
                },
            },
            "artifact_contract": {},
        }
        path = tmp_path / "bad.yml"
        path.write_text(yaml.safe_dump(malformed), encoding="utf-8")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile = data["profiles"]["bad_profile"]
        missing = _REQUIRED_PROFILE_KEYS - set(profile.keys())
        assert missing, "Malformed profile should be missing required keys"

    def test_empty_profiles_rejected(self):
        data = _load_profiles()
        profiles = data.get("profiles", {})
        assert len(profiles) >= len(_FIRST_CAMPAIGN_PROFILES_SET)

    def test_service_without_command_rejected(self, tmp_path: Path):
        malformed = {
            "version": 1,
            "defaults": {},
            "profiles": {
                "no_cmd": {
                    "description": "test",
                    "services": [{"alias": "hub"}],
                    "hard_budget": {"enabled": True},
                    "expected_owned_process_categories": ["car_service"],
                },
            },
            "artifact_contract": {},
        }
        path = tmp_path / "no_cmd.yml"
        path.write_text(yaml.safe_dump(malformed), encoding="utf-8")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in data["profiles"]["no_cmd"]["services"]:
            missing = _REQUIRED_SERVICE_KEYS - set(svc.keys())
            assert missing, "Service without command should be rejected"
