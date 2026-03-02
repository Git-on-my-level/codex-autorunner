// GENERATED FILE - do not edit directly. Source: static_src/
import { api, resolvePath } from "./utils.js";
export async function fetchContextspace() {
    return (await api("/api/contextspace"));
}
export async function writeContextspace(kind, content) {
    return (await api(`/api/contextspace/${kind}`, {
        method: "PUT",
        body: { content },
    }));
}
export async function listContextspaceFiles() {
    const res = (await api("/api/contextspace/files"));
    if (Array.isArray(res))
        return res;
    return res.files ?? [];
}
export async function ingestSpecToTickets() {
    return (await api("/api/contextspace/spec/ingest", { method: "POST" }));
}
export async function listTickets() {
    return (await api("/api/flows/ticket_flow/tickets"));
}
export async function fetchContextspaceTree() {
    const res = (await api("/api/contextspace/tree"));
    return res.tree || [];
}
export async function uploadContextspaceFiles(files, subdir) {
    const fd = new FormData();
    Array.from(files).forEach((file) => fd.append("files", file));
    if (subdir)
        fd.append("subdir", subdir);
    return api("/api/contextspace/upload", { method: "POST", body: fd });
}
export function downloadContextspaceFile(path) {
    const url = resolvePath(`/api/contextspace/download?path=${encodeURIComponent(path)}`);
    window.location.href = url;
}
export function downloadContextspaceZip(path) {
    const url = path
        ? resolvePath(`/api/contextspace/download-zip?path=${encodeURIComponent(path)}`)
        : resolvePath("/api/contextspace/download-zip");
    window.location.href = url;
}
export async function createContextspaceFolder(path) {
    await api(`/api/contextspace/folder?path=${encodeURIComponent(path)}`, { method: "POST" });
}
export async function deleteContextspaceFile(path) {
    await api(`/api/contextspace/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}
export async function deleteContextspaceFolder(path) {
    await api(`/api/contextspace/folder?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}
