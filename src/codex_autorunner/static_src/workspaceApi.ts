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
