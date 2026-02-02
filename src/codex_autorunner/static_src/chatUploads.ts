import { api, flash, resolvePath } from "./utils.js";

const IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "gif", "webp", "heic", "heif"];
const IMAGE_MIME_EXT: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
  "image/heic": "heic",
  "image/heif": "heif",
};

type UploadBox = "inbox" | "outbox";
type InsertStyle = "markdown" | "path" | "both";

type PasteUploadOptions = {
  textarea: HTMLTextAreaElement | null;
  basePath: string;
  box?: UploadBox;
  insertStyle?: InsertStyle;
  pathPrefix?: string;
  onUploaded?: (entries: Array<{ name: string; url: string }>) => void;
};

function escapeMarkdownLinkText(text: string): string {
  return text.replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}

function toAbsoluteUrl(path: string): string {
  const resolved = resolvePath(path);
  try {
    return new URL(resolved, window.location.origin).toString();
  } catch {
    return resolved;
  }
}

function isImageFile(file: File): boolean {
  if (file.type && file.type.startsWith("image/")) return true;
  const lower = (file.name || "").toLowerCase();
  return IMAGE_EXTENSIONS.some((ext) => lower.endsWith(`.${ext}`));
}

function normalizeFilename(file: File, index: number, used: Set<string>): string {
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
    } else {
      candidate = `${name}-${suffix}`;
    }
    suffix += 1;
  }
  used.add(candidate);
  return candidate;
}

function extractImageFilesFromClipboard(event: ClipboardEvent): File[] {
  const items = event.clipboardData?.items;
  if (!items || !items.length) return [];
  const files: File[] = [];
  for (const item of Array.from(items)) {
    if (item.kind !== "file") continue;
    if (item.type && !item.type.startsWith("image/")) continue;
    const file = item.getAsFile();
    if (file && isImageFile(file)) files.push(file);
  }
  return files;
}

async function uploadImages(
  basePath: string,
  box: UploadBox,
  files: File[],
  pathPrefix?: string
): Promise<Array<{ name: string; url: string; path?: string }>> {
  const used = new Set<string>();
  const form = new FormData();
  const entries: Array<{ name: string; url: string; path?: string }> = [];
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

function insertTextAtCursor(
  textarea: HTMLTextAreaElement,
  text: string,
  options: { separator?: "newline" | "space" | "none" } = {}
): void {
  const value = textarea.value || "";
  const start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart! : value.length;
  const end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd! : value.length;
  const prefix = value.slice(0, start);
  const suffix = value.slice(end);
  const separator = options.separator || "newline";
  let insert = text;
  if (separator === "newline") {
    insert = `${prefix && !prefix.endsWith("\n") ? "\n" : ""}${insert}`;
  } else if (separator === "space") {
    insert = `${prefix && !/\s$/.test(prefix) ? " " : ""}${insert}`;
  }
  textarea.value = `${prefix}${insert}${suffix}`;
  const cursor = prefix.length + insert.length;
  textarea.setSelectionRange(cursor, cursor);
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

export function initChatPasteUpload(options: PasteUploadOptions): void {
  const { textarea } = options;
  if (!textarea) return;

  textarea.addEventListener("paste", async (event) => {
    const files = extractImageFilesFromClipboard(event);
    if (!files.length) return;
    event.preventDefault();

    const box = options.box || "inbox";
    const insertStyle = options.insertStyle || "markdown";
    try {
      const entries = await uploadImages(options.basePath, box, files, options.pathPrefix);
      const lines = entries.flatMap((entry) => {
        const label = escapeMarkdownLinkText(entry.name);
        const linkLine = `[${label}](${entry.url})`;
        if (insertStyle === "markdown") return [linkLine];
        const pathLine = entry.path || entry.name;
        if (insertStyle === "path") return [pathLine];
        return entry.path ? [linkLine, entry.path] : [linkLine];
      });
      if (lines.length) {
        insertTextAtCursor(textarea, lines.join("\n"), { separator: "newline" });
      }
      options.onUploaded?.(entries.map((entry) => ({ name: entry.name, url: entry.url })));
    } catch (err) {
      const message = (err as Error).message || "Image upload failed";
      flash(message, "error");
    }
  });
}
