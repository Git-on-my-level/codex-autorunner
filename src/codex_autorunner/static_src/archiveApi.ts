import { api, resolvePath } from "./utils.js";

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

export interface ArchiveTreeNode {
  path: string;
  name: string;
  type: "file" | "folder";
  size_bytes?: number | null;
  mtime?: number | null;
}

export interface ArchiveTreeResponse {
  path: string;
  nodes: ArchiveTreeNode[];
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

export async function listArchiveTree(
  snapshotId: string,
  worktreeRepoId?: string | null,
  path: string = ""
): Promise<ArchiveTreeResponse> {
  const params = new URLSearchParams({ snapshot_id: snapshotId });
  if (worktreeRepoId) params.set("worktree_repo_id", worktreeRepoId);
  if (path) params.set("path", path);
  const url = `/api/archive/tree?${params.toString()}`;
  return (await api(url)) as ArchiveTreeResponse;
}

export async function readArchiveFile(
  snapshotId: string,
  worktreeRepoId: string | null,
  path: string
): Promise<string> {
  const params = new URLSearchParams({ snapshot_id: snapshotId, path });
  if (worktreeRepoId) params.set("worktree_repo_id", worktreeRepoId);
  const url = `/api/archive/file?${params.toString()}`;
  return (await api(url)) as string;
}

export function downloadArchiveFile(snapshotId: string, worktreeRepoId: string | null, path: string): void {
  const params = new URLSearchParams({ snapshot_id: snapshotId, path });
  if (worktreeRepoId) params.set("worktree_repo_id", worktreeRepoId);
  const url = resolvePath(`/api/archive/download?${params.toString()}`);
  window.location.href = url;
}
