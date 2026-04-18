// GENERATED FILE - do not edit directly. Source: static_src/
import { api, confirmModal, flash } from "./utils.js";
import { checkTemplatesEnabled } from "./ticketTemplates.js";
function els() {
    return {
        list: document.getElementById("template-repos-list"),
        addBtn: document.getElementById("template-repos-add"),
        form: document.getElementById("template-repo-form"),
        idInput: document.getElementById("repo-id"),
        urlInput: document.getElementById("repo-url"),
        refInput: document.getElementById("repo-ref"),
        trustedInput: document.getElementById("repo-trusted"),
        saveBtn: document.getElementById("repo-save"),
        cancelBtn: document.getElementById("repo-cancel"),
    };
}
const state = {
    mode: "create",
    editId: null,
    enabled: false,
    repos: [],
    busy: false,
};
function setBusy(busy) {
    state.busy = busy;
    const { saveBtn, addBtn } = els();
    if (saveBtn)
        saveBtn.disabled = busy;
    if (addBtn)
        addBtn.disabled = busy;
}
function showForm(show) {
    const { form } = els();
    if (!form)
        return;
    if (show)
        form.classList.remove("hidden");
    else
        form.classList.add("hidden");
}
function resetForm() {
    const { idInput, urlInput, refInput, trustedInput } = els();
    if (idInput)
        idInput.value = "";
    if (urlInput)
        urlInput.value = "";
    if (refInput)
        refInput.value = "main";
    if (trustedInput)
        trustedInput.checked = false;
    state.mode = "create";
    state.editId = null;
    if (idInput)
        idInput.disabled = false;
}
function openCreateForm() {
    resetForm();
    showForm(true);
    const { idInput } = els();
    idInput?.focus();
}
function openEditForm(repo) {
    const { idInput, urlInput, refInput, trustedInput } = els();
    state.mode = "edit";
    state.editId = repo.id;
    if (idInput) {
        idInput.value = repo.id;
        idInput.disabled = true;
    }
    if (urlInput)
        urlInput.value = repo.url;
    if (refInput)
        refInput.value = repo.default_ref || "main";
    if (trustedInput)
        trustedInput.checked = Boolean(repo.trusted);
    showForm(true);
    urlInput?.focus();
}
function normalizeRequired(value, label) {
    const v = (value || "").trim();
    if (!v) {
        flash(`${label} is required`, "error");
        return null;
    }
    return v;
}
function renderRepos() {
    const { list } = els();
    if (!list)
        return;
    list.innerHTML = "";
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
            const action = btn.dataset.action;
            const id = btn.dataset.id;
            if (!action || !id)
                return;
            const repo = state.repos.find((r) => r.id === id);
            if (!repo)
                return;
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
export async function loadTemplateRepos() {
    const { list } = els();
    if (!list)
        return;
    try {
        const data = (await api("/api/templates/repos"));
        state.enabled = Boolean(data.enabled);
        state.repos = Array.isArray(data.repos) ? data.repos : [];
        renderRepos();
    }
    catch (err) {
        state.enabled = false;
        state.repos = [];
        renderRepos();
        flash(err.message || "Failed to load template repos", "error");
    }
}
async function saveRepo() {
    const { idInput, urlInput, refInput, trustedInput } = els();
    if (!idInput || !urlInput || !refInput || !trustedInput)
        return;
    const id = normalizeRequired(idInput.value, "ID");
    const url = normalizeRequired(urlInput.value, "Git URL");
    const ref = normalizeRequired(refInput.value, "Default ref");
    if (!id || !url || !ref)
        return;
    setBusy(true);
    try {
        if (state.mode === "create") {
            await api("/api/templates/repos", {
                method: "POST",
                body: { id, url, trusted: Boolean(trustedInput.checked), default_ref: ref },
            });
            flash("Template repo added", "success");
        }
        else if (state.mode === "edit" && state.editId) {
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
    }
    catch (err) {
        flash(err.message || "Failed to save template repo", "error");
    }
    finally {
        setBusy(false);
    }
}
async function deleteRepo(id) {
    const confirmed = await confirmModal(`Delete template repo "${id}"?`, {
        confirmText: "Delete",
        danger: true,
    });
    if (!confirmed)
        return;
    setBusy(true);
    try {
        await api(`/api/templates/repos/${encodeURIComponent(id)}`, { method: "DELETE" });
        flash("Template repo deleted", "success");
        await loadTemplateRepos();
        await checkTemplatesEnabled();
    }
    catch (err) {
        flash(err.message || "Failed to delete template repo", "error");
    }
    finally {
        setBusy(false);
    }
}
export function initTemplateReposSettings() {
    const { list, addBtn, saveBtn, cancelBtn } = els();
    if (!list || !addBtn || !saveBtn || !cancelBtn)
        return;
    addBtn.addEventListener("click", () => openCreateForm());
    saveBtn.addEventListener("click", () => void saveRepo());
    cancelBtn.addEventListener("click", () => {
        showForm(false);
        resetForm();
    });
}
