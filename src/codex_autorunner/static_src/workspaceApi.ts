import { api } from "./utils.js";

export type WorkspaceKind = "active_context" | "decisions" | "spec";

export interface WorkspaceResponse {
  active_context: string;
  decisions: string;
  spec: string;
}

export interface SpecIngestTicketsResponse {
  status: string;
  created: number;
  first_ticket_path?: string | null;
}

export async function fetchWorkspace(): Promise<WorkspaceResponse> {
  return (await api("/api/workspace")) as WorkspaceResponse;
}

export async function writeWorkspace(kind: WorkspaceKind, content: string): Promise<WorkspaceResponse> {
  return (await api(`/api/workspace/${kind}`, {
    method: "PUT",
    body: { content },
  })) as WorkspaceResponse;
}

export interface WorkspaceFileListItem {
  name: string;
  path: string;
  is_pinned: boolean;
  modified_at?: string | null;
}

export interface WorkspaceFileListResponse {
  files: WorkspaceFileListItem[];
}

export interface WorkspaceNode {
  name: string;
  path: string;
  type: "file" | "folder";
  is_pinned?: boolean;
  modified_at?: string | null;
  size?: number | null;
  children?: WorkspaceNode[];
}

export async function listWorkspaceFiles(): Promise<WorkspaceFileListItem[]> {
  const res = (await api("/api/workspace/files")) as WorkspaceFileListResponse | WorkspaceFileListItem[];
  if (Array.isArray(res)) return res;
  return res.files ?? [];
}

export async function ingestSpecToTickets(): Promise<SpecIngestTicketsResponse> {
  return (await api("/api/workspace/spec/ingest", { method: "POST" })) as SpecIngestTicketsResponse;
}

export async function listTickets(): Promise<{ tickets?: unknown[] }> {
  return (await api("/api/flows/ticket_flow/tickets")) as { tickets?: unknown[] };
}

export async function fetchWorkspaceTree(): Promise<WorkspaceNode[]> {
  const res = (await api("/api/workspace/tree")) as { tree: WorkspaceNode[] };
  return res.tree || [];
}

export async function uploadWorkspaceFiles(
  files: FileList | File[],
  subdir?: string
): Promise<{ uploaded: Array<{ filename: string; path: string; size: number }> }> {
  const fd = new FormData();
  Array.from(files as unknown as Iterable<File>).forEach((file) => fd.append("files", file));
  if (subdir) fd.append("subdir", subdir);
  return api("/api/workspace/upload", { method: "POST", body: fd }) as Promise<{
    uploaded: Array<{ filename: string; path: string; size: number }>;
  }>;
}

export function downloadWorkspaceFile(path: string): void {
  const url = `/api/workspace/download?path=${encodeURIComponent(path)}`;
  window.location.href = url;
}

export function downloadWorkspaceZip(path?: string): void {
  const url = path
    ? `/api/workspace/download-zip?path=${encodeURIComponent(path)}`
    : "/api/workspace/download-zip";
  window.location.href = url;
}

export async function createWorkspaceFolder(path: string): Promise<void> {
  await api(`/api/workspace/folder?path=${encodeURIComponent(path)}`, { method: "POST" });
}

export async function deleteWorkspaceFile(path: string): Promise<void> {
  await api(`/api/workspace/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}

export async function deleteWorkspaceFolder(path: string): Promise<void> {
  await api(`/api/workspace/folder?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}
