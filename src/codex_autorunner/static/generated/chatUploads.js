// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, resolvePath } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { DEFAULT_FILEBOX_BOX } from "./fileboxCatalog.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
const IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "gif", "webp", "heic", "heif"];
const IMAGE_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
};
function escapeMarkdownLinkText(text) {
    return text.replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}
function toAbsoluteUrl(path) {
    const resolved = resolvePath(path);
    try {
        return new URL(resolved, window.location.origin).toString();
    }
    catch {
        return resolved;
    }
}
function isImageFile(file) {
    if (file.type && file.type.startsWith("image/"))
        return true;
    const lower = (file.name || "").toLowerCase();
    return IMAGE_EXTENSIONS.some((ext) => lower.endsWith(`.${ext}`));
}
function normalizeFilename(file, index, used) {
    let name = (file.name || "").trim();
    if (!name) {
        const ext = IMAGE_MIME_EXT[file.type] || "png";
        name = `pasted-image-${Date.now()}-${index + 1}.${ext}`;
    }
    let candidate = name;
    let suffix = 1;
    while (used.has(candidate)) {
        const dot = name.lastIndexOf(".");
        if (dot > 0) {
            const base = name.slice(0, dot);
            const ext = name.slice(dot + 1);
            candidate = `${base}-${suffix}.${ext}`;
        }
        else {
            candidate = `${name}-${suffix}`;
        }
        suffix += 1;
    }
    used.add(candidate);
    return candidate;
}
function extractImageFilesFromClipboard(event) {
    const items = event.clipboardData?.items;
    if (!items || !items.length)
        return [];
    const files = [];
    for (const item of Array.from(items)) {
        if (item.kind !== "file")
            continue;
        if (item.type && !item.type.startsWith("image/"))
            continue;
        const file = item.getAsFile();
        if (file && isImageFile(file))
            files.push(file);
    }
    return files;
}
async function uploadImages(basePath, box, files, pathPrefix) {
    const used = new Set();
    const form = new FormData();
    const entries = [];
    const prefix = basePath.replace(/\/$/, "");
    const normalizedPathPrefix = pathPrefix ? pathPrefix.replace(/\/$/, "") : "";
    files.forEach((file, index) => {
        const name = normalizeFilename(file, index, used);
        form.append(name, file, name);
        const path = normalizedPathPrefix ? `${normalizedPathPrefix}/${box}/${name}` : undefined;
        const relativeUrl = `${prefix}/${box}/${encodeURIComponent(name)}`;
        entries.push({ name, url: toAbsoluteUrl(relativeUrl), path });
    });
    await api(`${prefix}/${box}`, { method: "POST", body: form });
    return entries;
}
function insertTextAtCursor(textarea, text, options = {}) {
    const value = textarea.value || "";
    const start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : value.length;
    const end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : value.length;
    const prefix = value.slice(0, start);
    const suffix = value.slice(end);
    const separator = options.separator || "newline";
    let insert = text;
    if (separator === "newline") {
        insert = `${prefix && !prefix.endsWith("\n") ? "\n" : ""}${insert}`;
    }
    else if (separator === "space") {
        insert = `${prefix && !/\s$/.test(prefix) ? " " : ""}${insert}`;
    }
    textarea.value = `${prefix}${insert}${suffix}`;
    const cursor = prefix.length + insert.length;
    textarea.setSelectionRange(cursor, cursor);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
}
export function createAttachmentTracker() {
    const attachments = [];
    return {
        getAttachments() {
            return [...attachments];
        },
        getPendingCount() {
            return attachments.filter((a) => a.status === "uploading").length;
        },
        getSummaryText() {
            if (!attachments.length)
                return "";
            const uploaded = attachments.filter((a) => a.status === "uploaded");
            const pending = attachments.filter((a) => a.status === "uploading");
            const failed = attachments.filter((a) => a.status === "failed");
            const parts = [];
            if (uploaded.length)
                parts.push(`${uploaded.length} uploaded`);
            if (pending.length)
                parts.push(`${pending.length} uploading`);
            if (failed.length)
                parts.push(`${failed.length} failed`);
            const names = uploaded.map((a) => a.name).join(", ");
            return `Attachments: ${parts.join(", ")}${names ? ` (${names})` : ""}`;
        },
        _add(entry) {
            attachments.push(entry);
        },
        _update(name, status, error) {
            const entry = attachments.find((a) => a.name === name);
            if (entry) {
                entry.status = status;
                if (error)
                    entry.error = error;
            }
        },
    };
}
export function initChatPasteUpload(options) {
    const tracker = createAttachmentTracker();
    const { textarea } = options;
    if (!textarea)
        return tracker;
    textarea.addEventListener("paste", async (event) => {
        const files = extractImageFilesFromClipboard(event);
        if (!files.length)
            return;
        event.preventDefault();
        const box = options.box || DEFAULT_FILEBOX_BOX;
        const insertStyle = options.insertStyle || "markdown";
        const used = new Set();
        files.forEach((file, index) => {
            const name = normalizeFilename(file, index, used);
            tracker._add({ name, url: "", status: "uploading" });
        });
        options.onAttachmentStateChange?.(tracker);
        try {
            const entries = await uploadImages(options.basePath, box, files, options.pathPrefix);
            for (const entry of entries) {
                tracker._update(entry.name, "uploaded");
                const tracked = tracker.getAttachments().find((a) => a.name === entry.name);
                if (tracked)
                    tracked.url = entry.url;
            }
            options.onAttachmentStateChange?.(tracker);
            const lines = entries.flatMap((entry) => {
                const label = escapeMarkdownLinkText(entry.name);
                const linkLine = `[${label}](${entry.url})`;
                if (insertStyle === "markdown")
                    return [linkLine];
                const pathLine = entry.path || entry.name;
                if (insertStyle === "path")
                    return [pathLine];
                return entry.path ? [linkLine, entry.path] : [linkLine];
            });
            if (lines.length) {
                insertTextAtCursor(textarea, lines.join("\n"), { separator: "newline" });
            }
            options.onUploaded?.(entries.map((entry) => ({ name: entry.name, url: entry.url })));
        }
        catch (err) {
            const message = err.message || "Image upload failed";
            for (const a of tracker.getAttachments()) {
                if (a.status === "uploading")
                    tracker._update(a.name, "failed", message);
            }
            options.onAttachmentStateChange?.(tracker);
            flash(message, "error");
        }
    });
    return tracker;
}
