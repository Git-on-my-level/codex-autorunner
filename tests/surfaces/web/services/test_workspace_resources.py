from pathlib import Path

from codex_autorunner.core.artifact_filebox_storage import ArtifactFileBoxStorage
from codex_autorunner.surfaces.web.services.workspace_resources import (
    FileBoxResourceService,
    FileBoxUrlScope,
)


def test_artifact_delivery_listing_includes_download_url(tmp_path: Path) -> None:
    source = tmp_path / "spec.md"
    source.write_text("# Spec\n", encoding="utf-8")
    intent = ArtifactFileBoxStorage(tmp_path).enqueue_delivery_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:1",
    )

    payload = FileBoxResourceService().list_artifact_deliveries(
        tmp_path,
        url_scope=FileBoxUrlScope(root_path="/base", repo_id="repo-1"),
    )

    delivery = payload["deliveries"][0]
    assert delivery["delivery_id"] == intent.delivery_id
    assert delivery["download_url"] == (
        f"/base/hub/filebox/repo-1/artifacts/deliveries/"
        f"{intent.delivery_id.replace(':', '%3A')}/download"
    )
    assert delivery["artifact"]["url"] == delivery["download_url"]


def test_open_delivery_artifact_returns_downloadable_file(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_text("payload\n", encoding="utf-8")
    intent = ArtifactFileBoxStorage(tmp_path).enqueue_delivery_file(
        source,
        target_surface="discord",
        target_conversation_key="channel:1",
    )

    resource = FileBoxResourceService().open_delivery_artifact(
        tmp_path, intent.delivery_id
    )

    try:
        assert resource.entry.name == "report.txt"
        assert resource.entry.source == "artifact_delivery"
        assert resource.handle.read() == b"payload\n"
    finally:
        resource.handle.close()
