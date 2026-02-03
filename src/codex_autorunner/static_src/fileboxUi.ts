import { api, confirmModal, escapeHtml, flash, resolvePath } from "./utils.js";

export type FileBoxEntry = {
  name: string;
  box: "inbox" | "outbox";
  size?: number | null;
  modified_at?: string | null;
  source?: string | null;
  url?: string;
};

export type FileBoxListing = {
  inbox: FileBoxEntry[];
  outbox: FileBoxEntry[];
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
  uploadBox?: "inbox" | "outbox";
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
  return {
    inbox: Array.isArray(res?.inbox) ? (res!.inbox as FileBoxEntry[]) : [],
    outbox: Array.isArray(res?.outbox) ? (res!.outbox as FileBoxEntry[]) : [],
  };
}

async function uploadFiles(
  config: FileBoxConfig,
  box: "inbox" | "outbox",
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

async function deleteFile(config: FileBoxConfig, box: "inbox" | "outbox", name: string): Promise<void> {
  const prefix = pathPrefix(config);
  await api(`${prefix}/${box}/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export function createFileBoxWidget(opts: FileBoxWidgetOpts) {
  const uploadBox = opts.uploadBox || "inbox";
  let listing: FileBoxListing = { inbox: [], outbox: [] };

  const renderList = (box: "inbox" | "outbox", el: HTMLElement | null) => {
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
        return `
        <div class="filebox-item">
          <div class="filebox-row">
            <a class="filebox-link" href="${escapeHtml(href)}" download>${escapeHtml(entry.name)}</a>
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
        const boxName = (target.dataset.box || "") as "inbox" | "outbox";
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
    renderList("inbox", opts.inboxEl);
    renderList("outbox", opts.outboxEl);
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
      return {
        inbox: [...listing.inbox],
        outbox: [...listing.outbox],
      };
    },
  };
}
