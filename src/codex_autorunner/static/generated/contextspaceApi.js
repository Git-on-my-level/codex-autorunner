import { api } from "./utils.js";
export async function fetchContextspace() {
    return (await api("/api/contextspace"));
}
export async function writeContextspace(kind, content) {
    return (await api(`/api/contextspace/${kind}`, {
        method: "PUT",
        body: { content },
    }));
}
export async function ingestSpecToTickets() {
    return (await api("/api/contextspace/spec/ingest", { method: "POST" }));
}
export async function listTickets() {
    return (await api("/api/flows/ticket_flow/tickets"));
}
