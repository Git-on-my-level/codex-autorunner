import { api, confirmModal, escapeHtml, flash, resolvePath } from "./utils.js";
import {
  DEFAULT_FILEBOX_BOX,
  FILEBOX_BOXES,
  type FileBoxBox,
} from "./fileboxCatalog.js";

export type FileBoxEntry = {
  name: string;
  box: FileBoxBox;
  size?: number | null;
  modified_at?: string | null;
  source?: string | null;
  next_action?: string | null;
  attention_summary?: string | null;
  urgency?: string | null;
  likely_false_positive?: boolean | null;
  freshness?: {
    is_stale?: boolean | null;
    status?: string | null;
  } | null;
  url?: string;
};

export type FileBoxListing = {
  [K in FileBoxBox]: FileBoxEntry[];
};

export type FileBoxScope = "repo" | "hub" | "pma";

type FileBoxConfig = {
  scope: FileBoxScope;
  repoId?: string | null;
  basePath?: string;
};

type FileBoxWidgetOpts = FileBoxConfig & {
  inboxEl: HTMLElement | null;
  outboxEl: HTMLElement | null;
  uploadInput?: HTMLInputElement | null;
  uploadBtn?: HTMLButtonElement | null;
  refreshBtn?: HTMLButtonElement | null;
  uploadBox?: FileBoxBox;
  emptyMessage?: string;
  onChange?(listing: FileBoxListing): void;
  onUpload?(names: string[]): void;
  onError?(msg: string): void;
};

function formatBytes(size?: number | null): string {
  if (!size && size !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let val = size;
  let idx = 0;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  const formatted = idx === 0 ? String(val) : val.toFixed(1).replace(/\.0$/, "");
  return `${formatted}${units[idx]}`;
}

function createEmptyFileBoxListing(): FileBoxListing {
  return FILEBOX_BOXES.reduce((listing, box) => {
    listing[box] = [];
    return listing;
  }, {} as FileBoxListing);
}

function pathPrefix(config: FileBoxConfig): string {
  if (config.scope === "repo") {
    return config.basePath || "/api/filebox";
  }
  if (config.scope === "pma") {
    return config.basePath || "/hub/pma/files";
  }
  if (!config.repoId) {
    throw new Error("repoId is required for hub filebox");
  }
  const base = config.basePath || "/hub/filebox";
  return `${base}/${encodeURIComponent(config.repoId)}`;
}

async function listFileBox(config: FileBoxConfig): Promise<FileBoxListing> {
  const prefix = pathPrefix(config);
  const res = (await api(prefix)) as Partial<FileBoxListing> | null;
  const listing = createEmptyFileBoxListing();
  for (const box of FILEBOX_BOXES) {
    listing[box] = Array.isArray(res?.[box]) ? (res?.[box] as FileBoxEntry[]) : [];
  }
  return listing;
}

async function uploadFiles(
  config: FileBoxConfig,
  box: FileBoxBox,
  files: FileList | File[]
): Promise<string[]> {
  const prefix = pathPrefix(config);
  const form = new FormData();
  const names: string[] = [];
  Array.from(files).forEach((file) => {
    form.append(file.name, file);
    names.push(file.name);
  });
  await api(`${prefix}/${box}`, {
    method: "POST",
    body: form,
  });
  return names;
}

async function deleteFile(config: FileBoxConfig, box: FileBoxBox, name: string): Promise<void> {
  const prefix = pathPrefix(config);
  await api(`${prefix}/${box}/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export function createFileBoxWidget(opts: FileBoxWidgetOpts) {
  const uploadBox = opts.uploadBox || DEFAULT_FILEBOX_BOX;
  const [inboxBox, outboxBox] = FILEBOX_BOXES;
  let listing: FileBoxListing = createEmptyFileBoxListing();

  const renderList = (box: FileBoxBox, el: HTMLElement | null) => {
    if (!el) return;
    const files = listing[box] || [];
    if (!files.length) {
      el.innerHTML = opts.emptyMessage
        ? `<div class="filebox-empty muted small">${escapeHtml(opts.emptyMessage)}</div>`
        : "";
      return;
    }
    el.innerHTML = files
      .map((entry) => {
        const href = entry.url ? resolvePath(entry.url) : "#";
        const meta = entry.modified_at ? new Date(entry.modified_at).toLocaleString() : "";
        const size = formatBytes(entry.size);
        const source = entry.source && entry.source !== "filebox" ? ` • ${escapeHtml(entry.source || "")}` : "";
        const isLikelyStale =
          entry.freshness?.is_stale === true ||
          entry.next_action === "review_stale_uploaded_file" ||
          entry.likely_false_positive === true;
        const statusPill = isLikelyStale
          ? `<span class="pill pill-small pill-warn" title="${escapeHtml(
              entry.attention_summary || "Likely stale leftover upload"
            )}">likely stale</span>`
          : "";
        return `
        <div class="filebox-item">
          <div class="filebox-row">
            <a class="filebox-link" href="${escapeHtml(href)}" download>${escapeHtml(entry.name)}</a>
            ${statusPill}
            <button class="ghost sm icon-btn filebox-delete" data-box="${box}" data-file="${escapeHtml(
              entry.name
            )}" title="Delete">×</button>
          </div>
          <div class="filebox-meta muted small">${escapeHtml(size || "")}${source}${
          meta ? ` • ${escapeHtml(meta)}` : ""
        }</div>
        </div>
      `;
      })
      .join("");
    el.querySelectorAll(".filebox-delete").forEach((btn) => {
      btn.addEventListener("click", async (evt) => {
        const target = evt.currentTarget as HTMLElement;
        const boxName = (target.dataset.box || "") as FileBoxBox;
        const file = target.dataset.file || "";
        if (!boxName || !file) return;
        const confirmed = await confirmModal(`Delete ${file}?`);
        if (!confirmed) return;
        try {
          await deleteFile(opts, boxName, file);
          await refresh();
        } catch (err) {
          const msg = (err as Error).message || "Delete failed";
          flash(msg, "error");
          opts.onError?.(msg);
        }
      });
    });
  };

  const render = () => {
    renderList(inboxBox, opts.inboxEl);
    renderList(outboxBox, opts.outboxEl);
  };

  async function refresh(): Promise<FileBoxListing> {
    try {
      listing = await listFileBox(opts);
      render();
      opts.onChange?.(listing);
    } catch (err) {
      const msg = (err as Error).message || "Failed to load FileBox";
      flash(msg, "error");
      opts.onError?.(msg);
    }
    return listing;
  }

  const handleUpload = async (files: FileList | null) => {
    if (!files || !files.length) return;
    const names = Array.from(files).map((f) => f.name);
    try {
      await uploadFiles(opts, uploadBox, files);
      opts.onUpload?.(names);
      await refresh();
    } catch (err) {
      const msg = (err as Error).message || "Upload failed";
      flash(msg, "error");
      opts.onError?.(msg);
    } finally {
      if (opts.uploadInput) opts.uploadInput.value = "";
    }
  };

  if (opts.uploadBtn && opts.uploadInput) {
    opts.uploadBtn.addEventListener("click", () => opts.uploadInput?.click());
    opts.uploadInput.addEventListener("change", () => void handleUpload(opts.uploadInput?.files));
  }

  if (opts.refreshBtn) {
    opts.refreshBtn.addEventListener("click", () => void refresh());
  }

  return {
    refresh,
    snapshot(): FileBoxListing {
      return FILEBOX_BOXES.reduce((snapshot, box) => {
        snapshot[box] = [...listing[box]];
        return snapshot;
      }, createEmptyFileBoxListing());
    },
  };
}
