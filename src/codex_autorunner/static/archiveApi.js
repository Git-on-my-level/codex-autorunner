// GENERATED FILE - do not edit directly. Source: static_src/
import { api, resolvePath } from "./utils.js";
export async function listArchiveSnapshots() {
    const res = (await api("/api/archive/snapshots"));
    return res?.snapshots ?? [];
}
export async function fetchArchiveSnapshot(snapshotId, worktreeRepoId) {
    const params = new URLSearchParams();
    if (worktreeRepoId)
        params.set("worktree_repo_id", worktreeRepoId);
    const qs = params.toString();
    const url = `/api/archive/snapshots/${encodeURIComponent(snapshotId)}${qs ? `?${qs}` : ""}`;
    return (await api(url));
}
export async function listArchiveTree(snapshotId, worktreeRepoId, path = "") {
    const params = new URLSearchParams({ snapshot_id: snapshotId });
    if (worktreeRepoId)
        params.set("worktree_repo_id", worktreeRepoId);
    if (path)
        params.set("path", path);
    const url = `/api/archive/tree?${params.toString()}`;
    return (await api(url));
}
export async function readArchiveFile(snapshotId, worktreeRepoId, path) {
    const params = new URLSearchParams({ snapshot_id: snapshotId, path });
    if (worktreeRepoId)
        params.set("worktree_repo_id", worktreeRepoId);
    const url = `/api/archive/file?${params.toString()}`;
    return (await api(url));
}
export function downloadArchiveFile(snapshotId, worktreeRepoId, path) {
    const params = new URLSearchParams({ snapshot_id: snapshotId, path });
    if (worktreeRepoId)
        params.set("worktree_repo_id", worktreeRepoId);
    const url = resolvePath(`/api/archive/download?${params.toString()}`);
    window.location.href = url;
}
