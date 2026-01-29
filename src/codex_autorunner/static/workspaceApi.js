// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js";
export async function fetchWorkspace() {
    return (await api("/api/workspace"));
}
export async function writeWorkspace(kind, content) {
    return (await api(`/api/workspace/${kind}`, {
        method: "PUT",
        body: { content },
    }));
}
export async function listWorkspaceFiles() {
    const res = (await api("/api/workspace/files"));
    if (Array.isArray(res))
        return res;
    return res.files ?? [];
}
export async function ingestSpecToTickets() {
    return (await api("/api/workspace/spec/ingest", { method: "POST" }));
}
export async function listTickets() {
    return (await api("/api/flows/ticket_flow/tickets"));
}
