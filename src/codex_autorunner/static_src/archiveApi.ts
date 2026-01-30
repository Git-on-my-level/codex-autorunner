import { api } from "./utils.js";

export interface ArchiveSnapshotSummary {
  snapshot_id: string;
  worktree_repo_id: string;
  created_at?: string | null;
  status?: string | null;
  branch?: string | null;
  head_sha?: string | null;
  note?: string | null;
  summary?: Record<string, unknown> | null;
}

export interface ArchiveSnapshotsResponse {
  snapshots: ArchiveSnapshotSummary[];
}

export interface ArchiveSnapshotDetailResponse {
  snapshot: ArchiveSnapshotSummary;
  meta?: Record<string, unknown> | null;
}

export async function listArchiveSnapshots(): Promise<ArchiveSnapshotSummary[]> {
  const res = (await api("/api/archive/snapshots")) as ArchiveSnapshotsResponse;
  return res?.snapshots ?? [];
}

export async function fetchArchiveSnapshot(
  snapshotId: string,
  worktreeRepoId?: string | null
): Promise<ArchiveSnapshotDetailResponse> {
  const params = new URLSearchParams();
  if (worktreeRepoId) params.set("worktree_repo_id", worktreeRepoId);
  const qs = params.toString();
  const url = `/api/archive/snapshots/${encodeURIComponent(snapshotId)}${qs ? `?${qs}` : ""}`;
  return (await api(url)) as ArchiveSnapshotDetailResponse;
}
