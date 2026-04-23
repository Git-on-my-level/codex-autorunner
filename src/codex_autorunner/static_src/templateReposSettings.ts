import { api, confirmModal, flash } from "./utils.js";
import { checkTemplatesEnabled } from "./ticketTemplates.js";

type TemplateRepo = {
  id: string;
  url: string;
  trusted: boolean;
  default_ref: string;
};

type TemplateReposResponse = {
  enabled: boolean;
  repos: TemplateRepo[];
};

type FormMode = "create" | "edit";

type Ui = {
  list: HTMLElement | null;
  addBtn: HTMLButtonElement | null;
  form: HTMLElement | null;
  idInput: HTMLInputElement | null;
  urlInput: HTMLInputElement | null;
  refInput: HTMLInputElement | null;
  trustedInput: HTMLInputElement | null;
  saveBtn: HTMLButtonElement | null;
  cancelBtn: HTMLButtonElement | null;
};

function els(): Ui {
  return {
    list: document.getElementById("template-repos-list"),
    addBtn: document.getElementById("template-repos-add") as HTMLButtonElement | null,
    form: document.getElementById("template-repo-form"),
    idInput: document.getElementById("repo-id") as HTMLInputElement | null,
    urlInput: document.getElementById("repo-url") as HTMLInputElement | null,
    refInput: document.getElementById("repo-ref") as HTMLInputElement | null,
    trustedInput: document.getElementById("repo-trusted") as HTMLInputElement | null,
    saveBtn: document.getElementById("repo-save") as HTMLButtonElement | null,
    cancelBtn: document.getElementById("repo-cancel") as HTMLButtonElement | null,
  };
}

const state: {
  mode: FormMode;
  editId: string | null;
  enabled: boolean;
  repos: TemplateRepo[];
  busy: boolean;
  loadFailed: boolean;
} = {
  mode: "create",
  editId: null,
  enabled: false,
  repos: [],
  busy: false,
  loadFailed: false,
};

function setBusy(busy: boolean): void {
  state.busy = busy;
  const { saveBtn, addBtn } = els();
  if (saveBtn) saveBtn.disabled = busy;
  if (addBtn) addBtn.disabled = busy;
}

function showForm(show: boolean): void {
  const { form } = els();
  if (!form) return;
  if (show) form.classList.remove("hidden");
  else form.classList.add("hidden");
}

function resetForm(): void {
  const { idInput, urlInput, refInput, trustedInput } = els();
  if (idInput) idInput.value = "";
  if (urlInput) urlInput.value = "";
  if (refInput) refInput.value = "main";
  if (trustedInput) trustedInput.checked = false;
  state.mode = "create";
  state.editId = null;
  if (idInput) idInput.disabled = false;
}

function openCreateForm(): void {
  resetForm();
  showForm(true);
  const { idInput } = els();
  idInput?.focus();
}

function openEditForm(repo: TemplateRepo): void {
  const { idInput, urlInput, refInput, trustedInput } = els();
  state.mode = "edit";
  state.editId = repo.id;
  if (idInput) {
    idInput.value = repo.id;
    idInput.disabled = true;
  }
  if (urlInput) urlInput.value = repo.url;
  if (refInput) refInput.value = repo.default_ref || "main";
  if (trustedInput) trustedInput.checked = Boolean(repo.trusted);
  showForm(true);
  urlInput?.focus();
}

function normalizeRequired(value: string, label: string): string | null {
  const v = (value || "").trim();
  if (!v) {
    flash(`${label} is required`, "error");
    return null;
  }
  return v;
}

function renderRepos(): void {
  const { list } = els();
  if (!list) return;
  list.innerHTML = "";
  if (state.loadFailed) {
    const hint = document.createElement("div");
    hint.className = "muted small";
    hint.textContent = "Failed to load template repos \u2014 refresh to retry.";
    list.appendChild(hint);
    return;
  }
  if (!state.enabled) {
    const hint = document.createElement("div");
    hint.className = "muted small";
    hint.textContent = "Templates are disabled (templates.enabled=false).";
    list.appendChild(hint);
  }
  if (!state.repos.length) {
    const empty = document.createElement("div");
    empty.className = "muted small";
    empty.textContent = "No template repos configured.";
    list.appendChild(empty);
    return;
  }
  for (const repo of state.repos) {
    const row = document.createElement("div");
    row.className = "template-repo-item";
    row.innerHTML = `
      <div class="template-repo-meta">
        <span class="template-repo-id">${repo.id}</span>
        <span class="template-repo-url">${repo.url}</span>
        <span class="muted small">ref: ${repo.default_ref}${repo.trusted ? " · trusted" : ""}</span>
      </div>
      <div class="template-repo-actions">
        <button class="ghost sm" data-action="edit" data-id="${repo.id}">Edit</button>
        <button class="danger sm" data-action="delete" data-id="${repo.id}">Delete</button>
      </div>
    `;
    list.appendChild(row);
  }

  list.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = (btn as HTMLButtonElement).dataset.action;
      const id = (btn as HTMLButtonElement).dataset.id;
      if (!action || !id) return;
      const repo = state.repos.find((r) => r.id === id);
      if (!repo) return;
      if (action === "edit") {
        openEditForm(repo);
        return;
      }
      if (action === "delete") {
        await deleteRepo(id);
      }
    });
  });
}

export async function loadTemplateRepos(): Promise<void> {
  const { list } = els();
  if (!list) return;
  try {
    const data = (await api("/api/templates/repos")) as TemplateReposResponse;
    if (!data || typeof data !== "object") {
      state.loadFailed = true;
      state.enabled = false;
      state.repos = [];
      renderRepos();
      flash("Template repos response was unreadable", "error");
      return;
    }
    state.loadFailed = false;
    state.enabled = typeof data.enabled === "boolean" ? data.enabled : false;
    state.repos = Array.isArray(data.repos)
      ? data.repos.filter(
          (r): r is TemplateRepo =>
            r != null &&
            typeof (r as TemplateRepo).id === "string" &&
            typeof (r as TemplateRepo).url === "string"
        )
      : [];
    renderRepos();
  } catch (err) {
    state.loadFailed = true;
    state.enabled = false;
    state.repos = [];
    renderRepos();
    flash((err as Error).message || "Failed to load template repos", "error");
  }
}

async function saveRepo(): Promise<void> {
  const { idInput, urlInput, refInput, trustedInput } = els();
  if (!idInput || !urlInput || !refInput || !trustedInput) return;

  const id = normalizeRequired(idInput.value, "ID");
  const url = normalizeRequired(urlInput.value, "Git URL");
  const ref = normalizeRequired(refInput.value, "Default ref");
  if (!id || !url || !ref) return;

  setBusy(true);
  try {
    if (state.mode === "create") {
      await api("/api/templates/repos", {
        method: "POST",
        body: { id, url, trusted: Boolean(trustedInput.checked), default_ref: ref },
      });
      flash("Template repo added", "success");
    } else if (state.mode === "edit" && state.editId) {
      await api(`/api/templates/repos/${encodeURIComponent(state.editId)}`, {
        method: "PUT",
        body: { url, trusted: Boolean(trustedInput.checked), default_ref: ref },
      });
      flash("Template repo updated", "success");
    }
    await loadTemplateRepos();
    await checkTemplatesEnabled();
    showForm(false);
    resetForm();
  } catch (err) {
    flash((err as Error).message || "Failed to save template repo", "error");
  } finally {
    setBusy(false);
  }
}

async function deleteRepo(id: string): Promise<void> {
  const confirmed = await confirmModal(`Delete template repo "${id}"?`, {
    confirmText: "Delete",
    danger: true,
  });
  if (!confirmed) return;
  setBusy(true);
  try {
    await api(`/api/templates/repos/${encodeURIComponent(id)}`, { method: "DELETE" });
    flash("Template repo deleted", "success");
    await loadTemplateRepos();
    await checkTemplatesEnabled();
  } catch (err) {
    flash((err as Error).message || "Failed to delete template repo", "error");
  } finally {
    setBusy(false);
  }
}

export function initTemplateReposSettings(): void {
  const { list, addBtn, saveBtn, cancelBtn } = els();
  if (!list || !addBtn || !saveBtn || !cancelBtn) return;

  addBtn.addEventListener("click", () => openCreateForm());
  saveBtn.addEventListener("click", () => void saveRepo());
  cancelBtn.addEventListener("click", () => {
    showForm(false);
    resetForm();
  });
}

