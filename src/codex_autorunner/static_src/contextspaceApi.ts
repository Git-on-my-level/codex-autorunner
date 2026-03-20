import { api } from "./utils.js";

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

export async function ingestSpecToTickets(): Promise<SpecIngestTicketsResponse> {
  return (await api("/api/contextspace/spec/ingest", { method: "POST" })) as SpecIngestTicketsResponse;
}

export async function listTickets(): Promise<{ tickets?: unknown[] }> {
  return (await api("/api/flows/ticket_flow/tickets")) as { tickets?: unknown[] };
}
