// GENERATED FILE - do not edit directly. Source: static_src/
import { api, resolvePath } from "./utils.js";
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
export async function fetchWorkspaceTree() {
    const res = (await api("/api/workspace/tree"));
    return res.tree || [];
}
export async function uploadWorkspaceFiles(files, subdir) {
    const fd = new FormData();
    Array.from(files).forEach((file) => fd.append("files", file));
    if (subdir)
        fd.append("subdir", subdir);
    return api("/api/workspace/upload", { method: "POST", body: fd });
}
export function downloadWorkspaceFile(path) {
    const url = resolvePath(`/api/workspace/download?path=${encodeURIComponent(path)}`);
    window.location.href = url;
}
export function downloadWorkspaceZip(path) {
    const url = path
        ? resolvePath(`/api/workspace/download-zip?path=${encodeURIComponent(path)}`)
        : resolvePath("/api/workspace/download-zip");
    window.location.href = url;
}
export async function createWorkspaceFolder(path) {
    await api(`/api/workspace/folder?path=${encodeURIComponent(path)}`, { method: "POST" });
}
export async function deleteWorkspaceFile(path) {
    await api(`/api/workspace/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}
export async function deleteWorkspaceFolder(path) {
    await api(`/api/workspace/folder?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}
