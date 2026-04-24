// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
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
