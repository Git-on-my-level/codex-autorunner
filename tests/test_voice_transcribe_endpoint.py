from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import REPO_OVERRIDE_FILENAME, load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


@dataclass(frozen=True)
class _VoiceEnv:
    client: TestClient
    repo_id: str
    repo_root: Path


@pytest.fixture(scope="module")
def _voice_env(tmp_path_factory):
    hub_root = tmp_path_factory.mktemp("hub")
    seed_hub_files(hub_root, force=True)
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    write_test_config(
        repo_root / REPO_OVERRIDE_FILENAME,
        {"voice": {"enabled": True, "provider": "openai_whisper"}},
    )
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    app = create_hub_app(hub_root)
    yield _VoiceEnv(
        client=TestClient(app),
        repo_id=repo_id,
        repo_root=repo_root,
    )


def test_voice_transcribe_reads_uploaded_file_bytes(_voice_env, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    res = _voice_env.client.post(
        f"/repos/{_voice_env.repo_id}/api/voice/transcribe",
        files={"file": ("voice.webm", b"", "audio/webm")},
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "No audio received"
