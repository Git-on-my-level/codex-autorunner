import { api, resolvePath } from "./utils.js";

export type ContextspaceKind = "active_context" | "decisions" | "spec";

export interface ContextspaceResponse {
  active_context: string;
  decisions: string;
  spec: string;
}

export interface SpecIngestTicketsResponse {
  status: string;
  created: number;
  first_ticket_path?: string | null;
}

export async function fetchContextspace(): Promise<ContextspaceResponse> {
  return (await api("/api/contextspace")) as ContextspaceResponse;
}

export async function writeContextspace(kind: ContextspaceKind, content: string): Promise<ContextspaceResponse> {
  return (await api(`/api/contextspace/${kind}`, {
    method: "PUT",
    body: { content },
  })) as ContextspaceResponse;
}

export interface ContextspaceFileListItem {
  name: string;
  path: string;
  is_pinned: boolean;
  modified_at?: string | null;
}

export interface ContextspaceFileListResponse {
  files: ContextspaceFileListItem[];
}

export interface ContextspaceNode {
  name: string;
  path: string;
  type: "file" | "folder";
  is_pinned?: boolean;
  modified_at?: string | null;
  size?: number | null;
  children?: ContextspaceNode[];
}

export async function listContextspaceFiles(): Promise<ContextspaceFileListItem[]> {
  const res = (await api("/api/contextspace/files")) as ContextspaceFileListResponse | ContextspaceFileListItem[];
  if (Array.isArray(res)) return res;
  return res.files ?? [];
}

export async function ingestSpecToTickets(): Promise<SpecIngestTicketsResponse> {
  return (await api("/api/contextspace/spec/ingest", { method: "POST" })) as SpecIngestTicketsResponse;
}

export async function listTickets(): Promise<{ tickets?: unknown[] }> {
  return (await api("/api/flows/ticket_flow/tickets")) as { tickets?: unknown[] };
}

export async function fetchContextspaceTree(): Promise<ContextspaceNode[]> {
  const res = (await api("/api/contextspace/tree")) as { tree: ContextspaceNode[] };
  return res.tree || [];
}

export async function uploadContextspaceFiles(
  files: FileList | File[],
  subdir?: string
): Promise<{ uploaded: Array<{ filename: string; path: string; size: number }> }> {
  const fd = new FormData();
  Array.from(files as unknown as Iterable<File>).forEach((file) => fd.append("files", file));
  if (subdir) fd.append("subdir", subdir);
  return api("/api/contextspace/upload", { method: "POST", body: fd }) as Promise<{
    uploaded: Array<{ filename: string; path: string; size: number }>;
  }>;
}

export function downloadContextspaceFile(path: string): void {
  const url = resolvePath(`/api/contextspace/download?path=${encodeURIComponent(path)}`);
  window.location.href = url;
}

export function downloadContextspaceZip(path?: string): void {
  const url = path
    ? resolvePath(`/api/contextspace/download-zip?path=${encodeURIComponent(path)}`)
    : resolvePath("/api/contextspace/download-zip");
  window.location.href = url;
}

export async function createContextspaceFolder(path: string): Promise<void> {
  await api(`/api/contextspace/folder?path=${encodeURIComponent(path)}`, { method: "POST" });
}

export async function deleteContextspaceFile(path: string): Promise<void> {
  await api(`/api/contextspace/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}

export async function deleteContextspaceFolder(path: string): Promise<void> {
  await api(`/api/contextspace/folder?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}
