// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
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
