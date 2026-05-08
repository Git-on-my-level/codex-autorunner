// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
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
