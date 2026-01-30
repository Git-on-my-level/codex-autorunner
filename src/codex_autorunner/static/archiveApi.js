// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js";
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
