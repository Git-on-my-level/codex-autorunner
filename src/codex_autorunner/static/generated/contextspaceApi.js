// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
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
