from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from ..schemas import HubDestinationSetRequest
    from .mount_manager import HubMountManager


class HubDestinationService:
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager

    def _resolve_manifest_repo(self, repo_id: str):
        from .....manifest import load_manifest

        manifest = load_manifest(
            self._context.config.manifest_path, self._context.config.root
        )
        repos_by_id = {entry.id: entry for entry in manifest.repos}
        repo = repos_by_id.get(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")
        return manifest, repos_by_id, repo

    def _destination_payload(
        self, manifest, repo, repos_by_id: dict[str, Any]
    ) -> dict[str, Any]:
        from .....core.destinations import resolve_effective_repo_destination

        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        merged_issues = [*manifest.issues_for_repo(repo.id), *resolution.issues]
        deduped_issues: list[str] = []
        for message in merged_issues:
            if message in deduped_issues:
                continue
            deduped_issues.append(message)
        return {
            "repo_id": repo.id,
            "kind": repo.kind,
            "worktree_of": repo.worktree_of,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": deduped_issues,
        }

    async def get_repo_destination(self, repo_id: str) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger, logging.INFO, "Hub destination show repo=%s" % repo_id
        )
        manifest, repos_by_id, repo = await asyncio.to_thread(
            self._resolve_manifest_repo, repo_id
        )
        return self._destination_payload(manifest, repo, repos_by_id)

    async def set_repo_destination(
        self, repo_id: str, payload: HubDestinationSetRequest
    ) -> dict:
        from .....core.destinations import validate_destination_write_payload
        from .....manifest import save_manifest

        normalized_kind = payload.kind.strip().lower()
        destination: dict[str, Any]
        if normalized_kind == "local":
            destination = {"kind": "local"}
        elif normalized_kind == "docker":
            destination = {"kind": "docker", "image": (payload.image or "").strip()}
            container_name = (payload.container_name or "").strip()
            if container_name:
                destination["container_name"] = container_name
            profile = (payload.profile or "").strip()
            if profile:
                destination["profile"] = profile
            workdir = (payload.workdir or "").strip()
            if workdir:
                destination["workdir"] = workdir
            env_passthrough = [
                str(item).strip()
                for item in (payload.env_passthrough or [])
                if str(item).strip()
            ]
            if env_passthrough:
                destination["env_passthrough"] = env_passthrough
            if payload.env:
                destination["env"] = dict(payload.env)
            mounts: list[dict[str, Any]] = []
            for item in payload.mounts or []:
                source = str((item or {}).get("source") or "")
                target = str((item or {}).get("target") or "")
                mount_payload: dict[str, Any] = {"source": source, "target": target}
                read_only = (item or {}).get("read_only")
                if read_only is None and "readOnly" in (item or {}):
                    read_only = (item or {}).get("readOnly")
                if read_only is None and "readonly" in (item or {}):
                    read_only = (item or {}).get("readonly")
                if read_only is not None:
                    mount_payload["read_only"] = read_only
                mounts.append(mount_payload)
            if mounts:
                destination["mounts"] = mounts
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported destination kind: {payload.kind!r}. "
                    "Use 'local' or 'docker'."
                ),
            )

        validated = validate_destination_write_payload(
            destination, context="destination"
        )
        if not validated.valid or validated.normalized_destination is None:
            detail = "; ".join(validated.errors) or "Invalid destination payload"
            raise HTTPException(status_code=400, detail=detail)
        normalized_destination = validated.normalized_destination

        manifest, repos_by_id, repo = await asyncio.to_thread(
            self._resolve_manifest_repo, repo_id
        )
        repo.destination = normalized_destination
        await asyncio.to_thread(
            save_manifest,
            self._context.config.manifest_path,
            manifest,
            self._context.config.root,
        )
        manifest, repos_by_id, repo = await asyncio.to_thread(
            self._resolve_manifest_repo, repo_id
        )

        snapshots = await asyncio.to_thread(
            self._context.supervisor.list_repos, use_cache=False
        )
        await self._mount_manager.refresh_mounts(snapshots)
        return self._destination_payload(manifest, repo, repos_by_id)
