// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, confirmModal, inputModal, openModal, } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { setCleanupAllInFlight, renderSummary } from "./hubRepoCards.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { getHubData, startHubJob, } from "./hubActions.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { refreshHub } from "./hubRefresh.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { loadUpdateTargetOptions, handleSystemUpdate } from "./hubRefresh.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
function splitCommaSeparated(value) {
    return value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
}
function currentDockerEnvPassthrough(destination) {
    const raw = destination?.env_passthrough;
    if (!Array.isArray(raw))
        return "";
    return raw
        .map((item) => String(item || "").trim())
        .filter(Boolean)
        .join(", ");
}
function currentDockerProfile(destination) {
    return typeof destination?.profile === "string"
        ? String(destination.profile).trim()
        : "";
}
function currentDockerWorkdir(destination) {
    return typeof destination?.workdir === "string"
        ? String(destination.workdir).trim()
        : "";
}
function currentDockerExplicitEnv(destination) {
    const raw = destination?.env;
    if (!raw || typeof raw !== "object" || Array.isArray(raw))
        return "";
    return Object.entries(raw)
        .map(([key, value]) => {
        const cleanKey = String(key || "").trim();
        if (!cleanKey)
            return "";
        if (value === null || value === undefined)
            return "";
        return `${cleanKey}=${String(value)}`;
    })
        .filter(Boolean)
        .join(", ");
}
function currentDockerMounts(destination) {
    const raw = destination?.mounts;
    if (!Array.isArray(raw))
        return "";
    const mounts = raw
        .map((item) => {
        if (!item || typeof item !== "object")
            return "";
        const source = String(item.source || "").trim();
        const target = String(item.target || "").trim();
        const rawReadOnly = item.read_only ??
            item.readOnly ??
            item.readonly;
        const readOnly = rawReadOnly === true;
        if (!source || !target)
            return "";
        return readOnly ? `${source}:${target}:ro` : `${source}:${target}`;
    })
        .filter(Boolean);
    return mounts.join(", ");
}
function parseDockerEnvMap(value) {
    const env = {};
    const entries = splitCommaSeparated(value);
    for (const entry of entries) {
        const splitAt = entry.indexOf("=");
        if (splitAt <= 0) {
            return {
                env: {},
                error: `Invalid env entry "${entry}". Use KEY=VALUE (comma-separated).`,
            };
        }
        const key = entry.slice(0, splitAt).trim();
        const mapValue = entry.slice(splitAt + 1);
        if (!key) {
            return {
                env: {},
                error: `Invalid env entry "${entry}". Use KEY=VALUE (comma-separated).`,
            };
        }
        env[key] = mapValue;
    }
    return { env, error: null };
}
function parseDockerMountList(value) {
    const mounts = [];
    const entries = splitCommaSeparated(value);
    for (const entry of entries) {
        let mountSpec = entry;
        let readOnly = null;
        const lowerEntry = entry.toLowerCase();
        if (lowerEntry.endsWith(":ro")) {
            mountSpec = entry.slice(0, -3);
            readOnly = true;
        }
        else if (lowerEntry.endsWith(":rw")) {
            mountSpec = entry.slice(0, -3);
            readOnly = false;
        }
        const splitAt = mountSpec.lastIndexOf(":");
        if (splitAt <= 0 || splitAt >= mountSpec.length - 1) {
            return {
                mounts: [],
                error: `Invalid mount "${entry}". Use source:target[:ro] (comma-separated).`,
            };
        }
        const source = mountSpec.slice(0, splitAt).trim();
        const target = mountSpec.slice(splitAt + 1).trim();
        if (!source || !target) {
            return {
                mounts: [],
                error: `Invalid mount "${entry}". Use source:target[:ro] (comma-separated).`,
            };
        }
        if (readOnly === true) {
            mounts.push({ source, target, read_only: true });
        }
        else {
            mounts.push({ source, target });
        }
    }
    return { mounts, error: null };
}
async function chooseDestinationKind(resourceLabel, currentKind) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.hidden = true;
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog repo-settings-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const header = document.createElement("div");
    header.className = "modal-header";
    const title = document.createElement("span");
    title.className = "label";
    title.textContent = `Set destination: ${resourceLabel}`;
    header.appendChild(title);
    const body = document.createElement("div");
    body.className = "modal-body";
    const hint = document.createElement("p");
    hint.className = "muted small";
    hint.textContent = "Choose execution destination kind.";
    body.appendChild(hint);
    const footer = document.createElement("div");
    footer.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "ghost";
    cancelBtn.textContent = "Cancel";
    const localBtn = document.createElement("button");
    localBtn.className = currentKind === "local" ? "primary" : "ghost";
    localBtn.textContent = "Local";
    const dockerBtn = document.createElement("button");
    dockerBtn.className = currentKind === "docker" ? "primary" : "ghost";
    dockerBtn.textContent = "Docker";
    footer.append(cancelBtn, localBtn, dockerBtn);
    dialog.append(header, body, footer);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    return new Promise((resolve) => {
        let closeModal = null;
        let settled = false;
        const returnFocusTo = document.activeElement;
        const finalize = (selected) => {
            if (settled)
                return;
            settled = true;
            if (closeModal) {
                const close = closeModal;
                closeModal = null;
                close();
            }
            overlay.remove();
            resolve(selected);
        };
        closeModal = openModal(overlay, {
            initialFocus: currentKind === "docker" ? dockerBtn : localBtn,
            returnFocusTo,
            onRequestClose: () => finalize(null),
        });
        cancelBtn.addEventListener("click", () => finalize(null));
        localBtn.addEventListener("click", () => finalize("local"));
        dockerBtn.addEventListener("click", () => finalize("docker"));
    });
}
function formatDestinationSummary(destination) {
    if (!destination || typeof destination !== "object")
        return "local";
    const kindRaw = destination.kind;
    const kind = typeof kindRaw === "string" ? kindRaw.trim().toLowerCase() : "local";
    if (kind === "docker") {
        const image = typeof destination.image === "string" ? destination.image.trim() : "";
        return image ? `docker:${image}` : "docker";
    }
    return "local";
}
async function promptForDestinationBody(resourceLabel, currentDestination) {
    const current = formatDestinationSummary(currentDestination);
    const currentKind = current.startsWith("docker:") || current === "docker" ? "docker" : "local";
    const kind = await chooseDestinationKind(resourceLabel, currentKind);
    if (!kind)
        return null;
    const body = { kind };
    if (kind === "docker") {
        const currentImage = typeof currentDestination?.image === "string"
            ? String(currentDestination.image)
            : "";
        const imageValue = await inputModal("Docker image:", {
            placeholder: "ghcr.io/acme/repo:tag",
            defaultValue: currentImage,
            confirmText: "Save",
        });
        if (!imageValue) {
            flash("Docker destination requires an image", "error");
            return null;
        }
        body.image = imageValue.trim();
        const configureAdvanced = await confirmModal("Configure optional docker fields (container name, profile, workdir, env passthrough, explicit env, mounts)?", {
            confirmText: "Configure",
            cancelText: "Skip",
            danger: false,
        });
        if (configureAdvanced) {
            const currentContainerName = typeof currentDestination?.container_name === "string"
                ? String(currentDestination.container_name)
                : "";
            const containerNameValue = await inputModal("Docker container name (optional):", {
                placeholder: "car-runner",
                defaultValue: currentContainerName,
                confirmText: "Next",
                allowEmpty: true,
            });
            if (containerNameValue === null)
                return null;
            const containerName = containerNameValue.trim();
            if (containerName) {
                body.container_name = containerName;
            }
            const profileValue = await inputModal("Docker profile (optional):", {
                placeholder: "full-dev",
                defaultValue: currentDockerProfile(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (profileValue === null)
                return null;
            const profile = profileValue.trim();
            if (profile) {
                body.profile = profile;
            }
            const workdirValue = await inputModal("Docker workdir (optional):", {
                placeholder: "/workspace",
                defaultValue: currentDockerWorkdir(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (workdirValue === null)
                return null;
            const workdir = workdirValue.trim();
            if (workdir) {
                body.workdir = workdir;
            }
            const envPassthroughValue = await inputModal("Docker env passthrough (optional, comma-separated):", {
                placeholder: "CAR_*, PATH",
                defaultValue: currentDockerEnvPassthrough(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (envPassthroughValue === null)
                return null;
            const envPassthrough = splitCommaSeparated(envPassthroughValue);
            if (envPassthrough.length) {
                body.env_passthrough = envPassthrough;
            }
            const envMapValue = await inputModal("Docker explicit env map (optional, KEY=VALUE pairs, comma-separated):", {
                placeholder: "OPENAI_API_KEY=sk-..., CODEX_HOME=/workspace/.codex",
                defaultValue: currentDockerExplicitEnv(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (envMapValue === null)
                return null;
            const parsedEnvMap = parseDockerEnvMap(envMapValue);
            if (parsedEnvMap.error) {
                flash(parsedEnvMap.error, "error");
                return null;
            }
            if (Object.keys(parsedEnvMap.env).length) {
                body.env = parsedEnvMap.env;
            }
            const mountsValue = await inputModal("Docker mounts (optional, source:target[:ro] pairs, comma-separated):", {
                placeholder: "/host/path:/workspace/path, /cache:/cache:ro",
                defaultValue: currentDockerMounts(currentDestination),
                confirmText: "Save",
                allowEmpty: true,
            });
            if (mountsValue === null)
                return null;
            const parsedMounts = parseDockerMountList(mountsValue);
            if (parsedMounts.error) {
                flash(parsedMounts.error, "error");
                return null;
            }
            if (parsedMounts.mounts.length) {
                body.mounts = parsedMounts.mounts;
            }
        }
    }
    return body;
}
async function promptAndSetRepoDestination(repo) {
    const body = await promptForDestinationBody(repo.display_name || repo.id, repo.effective_destination);
    if (!body)
        return false;
    const payload = (await api(`/hub/repos/${encodeURIComponent(repo.id)}/destination`, {
        method: "POST",
        body,
    }));
    const effective = formatDestinationSummary(payload.effective_destination);
    flash(`Updated destination for ${repo.id}: ${effective}`, "success");
    return true;
}
async function promptAndSetAgentWorkspaceDestination(workspace) {
    const body = await promptForDestinationBody(workspace.display_name || workspace.id, workspace.effective_destination);
    if (!body)
        return false;
    const payload = (await api(`/hub/agent-workspaces/${encodeURIComponent(workspace.id)}/destination`, {
        method: "POST",
        body,
    }));
    const effective = formatDestinationSummary(payload.effective_destination);
    flash(`Updated destination for ${workspace.id}: ${effective}`, "success");
    return true;
}
async function openRepoSettingsModal(repo) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.hidden = true;
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog repo-settings-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const header = document.createElement("div");
    header.className = "modal-header";
    const title = document.createElement("span");
    title.className = "label";
    title.textContent = `Settings: ${repo.display_name || repo.id}`;
    header.appendChild(title);
    const body = document.createElement("div");
    body.className = "modal-body";
    const worktreeSection = document.createElement("div");
    worktreeSection.className = "form-group";
    const worktreeLabel = document.createElement("label");
    worktreeLabel.textContent = "Worktree Setup Commands";
    const worktreeHint = document.createElement("p");
    worktreeHint.className = "muted small";
    worktreeHint.textContent =
        "Commands run with /bin/sh -lc after creating a new worktree. One per line, leave blank to disable.";
    const textarea = document.createElement("textarea");
    textarea.rows = 4;
    textarea.style.width = "100%";
    textarea.style.resize = "vertical";
    textarea.placeholder = "make setup\npnpm install\npre-commit install";
    textarea.value = (repo.worktree_setup_commands || []).join("\n");
    worktreeSection.append(worktreeLabel, worktreeHint, textarea);
    body.appendChild(worktreeSection);
    const destinationSection = document.createElement("div");
    destinationSection.className = "form-group";
    const destinationLabel = document.createElement("label");
    destinationLabel.textContent = "Execution Destination";
    const destinationHint = document.createElement("p");
    destinationHint.className = "muted small";
    destinationHint.textContent = "Set where runs execute for this repo.";
    const destinationRow = document.createElement("div");
    destinationRow.className = "settings-actions";
    const destinationPill = document.createElement("span");
    destinationPill.className = "pill pill-small hub-destination-settings-pill";
    destinationPill.textContent = formatDestinationSummary(repo.effective_destination);
    const destinationBtn = document.createElement("button");
    destinationBtn.className = "ghost";
    destinationBtn.textContent = "Change destination";
    destinationRow.append(destinationPill, destinationBtn);
    destinationSection.append(destinationLabel, destinationHint, destinationRow);
    body.appendChild(destinationSection);
    const dangerSection = document.createElement("div");
    dangerSection.className = "form-group settings-section-danger";
    const dangerLabel = document.createElement("label");
    dangerLabel.textContent = "Danger Zone";
    const dangerHint = document.createElement("p");
    dangerHint.className = "muted small";
    dangerHint.textContent =
        "Remove this repo from hub and delete its local directory.";
    const removeBtn = document.createElement("button");
    removeBtn.className = "danger sm";
    removeBtn.textContent = "Remove repo";
    dangerSection.append(dangerLabel, dangerHint, removeBtn);
    body.appendChild(dangerSection);
    const footer = document.createElement("div");
    footer.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "ghost";
    cancelBtn.textContent = "Cancel";
    const saveBtn = document.createElement("button");
    saveBtn.className = "primary";
    saveBtn.textContent = "Save";
    footer.append(cancelBtn, saveBtn);
    dialog.append(header, body, footer);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    return new Promise((resolve) => {
        let closeModal = null;
        let settled = false;
        const finalize = async (action) => {
            if (settled)
                return;
            settled = true;
            if (closeModal) {
                const close = closeModal;
                closeModal = null;
                close();
            }
            overlay.remove();
            if (action === "save") {
                const commands = textarea.value
                    .split("\n")
                    .map((line) => line.trim())
                    .filter(Boolean);
                try {
                    await api(`/hub/repos/${encodeURIComponent(repo.id)}/worktree-setup`, {
                        method: "POST",
                        body: { commands },
                    });
                    flash(commands.length
                        ? `Saved ${commands.length} setup command(s) for ${repo.id}`
                        : `Cleared setup commands for ${repo.id}`, "success");
                    await refreshHub();
                }
                catch (err) {
                    flash(err.message || "Failed to save settings", "error");
                }
            }
            if (action === "destination") {
                try {
                    const updated = await promptAndSetRepoDestination(repo);
                    if (updated) {
                        await refreshHub();
                    }
                }
                catch (err) {
                    flash(err.message || "Failed to update destination", "error");
                }
            }
            if (action === "remove") {
                try {
                    await removeRepoWithChecks(repo.id);
                }
                catch (err) {
                    flash(err.message || "Failed to remove repo", "error");
                }
            }
            resolve();
        };
        closeModal = openModal(overlay, {
            initialFocus: textarea,
            returnFocusTo: document.activeElement,
            onRequestClose: () => finalize("cancel"),
            onKeydown: (event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                    event.preventDefault();
                    finalize("save");
                }
            },
        });
        cancelBtn.addEventListener("click", () => finalize("cancel"));
        saveBtn.addEventListener("click", () => finalize("save"));
        destinationBtn.addEventListener("click", () => finalize("destination"));
        removeBtn.addEventListener("click", () => finalize("remove"));
    });
}
async function removeRepoWithChecks(repoId) {
    const check = await api(`/hub/repos/${repoId}/remove-check`, {
        method: "GET",
    });
    const warnings = [];
    const dirty = check.is_clean === false;
    if (dirty) {
        warnings.push("Working tree has uncommitted changes.");
    }
    const upstream = check.upstream;
    const hasUpstream = upstream?.has_upstream === false;
    if (hasUpstream) {
        warnings.push("No upstream tracking branch is configured.");
    }
    const ahead = Number(upstream?.ahead || 0);
    if (ahead > 0) {
        warnings.push(`Local branch is ahead of upstream by ${ahead} commit(s).`);
    }
    const behind = Number(upstream?.behind || 0);
    if (behind > 0) {
        warnings.push(`Local branch is behind upstream by ${behind} commit(s).`);
    }
    const worktrees = Array.isArray(check.worktrees)
        ? check.worktrees
        : [];
    if (worktrees.length) {
        warnings.push(`This repo has ${worktrees.length} worktree(s).`);
    }
    const messageParts = [`Remove repo "${repoId}" and delete its local directory?`];
    if (warnings.length) {
        messageParts.push("", "Warnings:", ...warnings.map((w) => `- ${w}`));
    }
    if (worktrees.length) {
        messageParts.push("", "Worktrees to delete:", ...worktrees.map((w) => `- ${w}`));
    }
    const ok = await confirmModal(messageParts.join("\n"), {
        confirmText: "Remove",
        danger: true,
    });
    if (!ok)
        return;
    const needsForce = dirty || ahead > 0;
    const requestBody = {
        force: needsForce,
        delete_dir: true,
        delete_worktrees: worktrees.length > 0,
    };
    if (needsForce) {
        const requiredAttestation = `REMOVE ${repoId}`;
        const forceAttestation = await inputModal(`This repo has uncommitted or unpushed changes.\n\nType this confirmation text to force removal:\n${requiredAttestation}`, { placeholder: requiredAttestation, confirmText: "Remove anyway" });
        if (!forceAttestation)
            return;
        if (forceAttestation !== requiredAttestation) {
            flash(`Confirmation text must exactly match: ${requiredAttestation}`, "error");
            return;
        }
        requestBody.force_attestation = forceAttestation;
    }
    await startHubJob(`/hub/jobs/repos/${repoId}/remove`, {
        body: requestBody,
        startedMessage: "Repo removal queued",
    });
    flash(`Removed repo: ${repoId}`, "success");
    await refreshHub();
}
export async function handleCleanupAll() {
    const btn = document.getElementById("hub-cleanup-all");
    if (!btn) {
        flash("Cleanup control is missing from the page.", "error");
        return;
    }
    let preview;
    try {
        preview = (await api("/hub/cleanup-all/preview", {
            method: "GET",
        }));
    }
    catch (err) {
        flash(err.message || "Failed to load cleanup preview", "error");
        return;
    }
    const threadCount = preview.threads?.archived_count || 0;
    const worktreeCount = preview.worktrees?.archived_count || 0;
    const flowRunCount = preview.flow_runs?.archived_count || 0;
    const totalCount = threadCount + worktreeCount + flowRunCount;
    if (totalCount <= 0) {
        flash("Nothing to clean up", "success");
        return;
    }
    const lines = [];
    if (threadCount > 0) {
        const repoSummaries = (preview.threads?.by_repo || [])
            .map((r) => `${r.repo_id} (${r.count})`)
            .join(", ");
        lines.push(`${threadCount} unbound thread${threadCount === 1 ? "" : "s"}: ${repoSummaries}`);
    }
    if (worktreeCount > 0) {
        const worktreeNames = (preview.worktrees?.items || [])
            .map((w) => w.branch || w.id)
            .join(", ");
        lines.push(`${worktreeCount} worktree${worktreeCount === 1 ? "" : "s"}: ${worktreeNames}`);
    }
    if (flowRunCount > 0) {
        const flowSummaries = (preview.flow_runs?.by_repo || [])
            .map((r) => `${r.repo_id} (${r.count})`)
            .join(", ");
        lines.push(`${flowRunCount} completed flow run${flowRunCount === 1 ? "" : "s"}: ${flowSummaries}`);
    }
    const message = `Clean slate?\n\nCAR will archive and clean up:\n\n${lines
        .map((l) => "• " + l)
        .join("\n")}`;
    const ok = await confirmModal(message, { confirmText: "Cleanup all" });
    if (!ok)
        return;
    const hubData = getHubData();
    setCleanupAllInFlight(true);
    renderSummary(hubData.repos || [], hubData);
    try {
        const job = await startHubJob("/hub/jobs/cleanup-all", {
            startedMessage: "Cleanup started",
        });
        const resultMessage = typeof job.result?.message === "string" && job.result.message.trim()
            ? job.result.message.trim()
            : "Cleanup complete";
        flash(resultMessage, "success");
        await refreshHub();
    }
    catch (err) {
        flash(err.message || "Cleanup failed", "error");
    }
    finally {
        setCleanupAllInFlight(false);
        renderSummary(hubData.repos || [], hubData);
    }
}
export let closeCreateRepoModal = null;
export let closeCreateAgentWorkspaceModal = null;
export function hideCreateRepoModal() {
    if (closeCreateRepoModal) {
        const close = closeCreateRepoModal;
        closeCreateRepoModal = null;
        close();
    }
}
export function hideCreateAgentWorkspaceModal() {
    if (closeCreateAgentWorkspaceModal) {
        const close = closeCreateAgentWorkspaceModal;
        closeCreateAgentWorkspaceModal = null;
        close();
    }
}
function resolvePath(path) {
    const base = window.__CAR_BASE_PREFIX || "";
    if (!base || path.startsWith(base))
        return path;
    return `${base}${path}`;
}
export function showCreateRepoModal() {
    const modal = document.getElementById("create-repo-modal");
    if (!modal)
        return;
    const triggerEl = document.activeElement;
    hideCreateRepoModal();
    const input = document.getElementById("create-repo-id");
    closeCreateRepoModal = openModal(modal, {
        initialFocus: input || modal,
        returnFocusTo: triggerEl,
        onRequestClose: hideCreateRepoModal,
    });
    if (input) {
        input.value = "";
        input.focus();
    }
    const pathInput = document.getElementById("create-repo-path");
    if (pathInput)
        pathInput.value = "";
    const urlInput = document.getElementById("create-repo-url");
    if (urlInput)
        urlInput.value = "";
    const gitCheck = document.getElementById("create-repo-git");
    if (gitCheck)
        gitCheck.checked = true;
}
export function showCreateAgentWorkspaceModal() {
    const modal = document.getElementById("create-agent-workspace-modal");
    if (!modal)
        return;
    const triggerEl = document.activeElement;
    hideCreateAgentWorkspaceModal();
    const input = document.getElementById("create-agent-workspace-id");
    closeCreateAgentWorkspaceModal = openModal(modal, {
        initialFocus: input || modal,
        returnFocusTo: triggerEl,
        onRequestClose: hideCreateAgentWorkspaceModal,
    });
    if (input) {
        input.value = "";
        input.focus();
    }
    const runtimeInput = document.getElementById("create-agent-workspace-runtime");
    if (runtimeInput)
        runtimeInput.value = "";
    const nameInput = document.getElementById("create-agent-workspace-name");
    if (nameInput)
        nameInput.value = "";
}
async function createRepo(repoId, repoPath, gitInit, gitUrl) {
    try {
        const payload = {};
        if (repoId)
            payload.id = repoId;
        if (repoPath)
            payload.path = repoPath;
        payload.git_init = gitInit;
        if (gitUrl)
            payload.git_url = gitUrl;
        const job = await startHubJob("/hub/jobs/repos", {
            body: payload,
            startedMessage: "Repo creation queued",
        });
        const label = repoId || repoPath || "repo";
        flash(`Created repo: ${label}`, "success");
        await refreshHub();
        if (job?.result?.mounted && job?.result?.id) {
            window.location.href = resolvePath(`/repos/${job.result.id}/`);
        }
        return true;
    }
    catch (err) {
        flash(err.message || "Failed to create repo", "error");
        return false;
    }
}
async function createAgentWorkspace(workspaceId, runtime, displayName) {
    try {
        const payload = {};
        if (workspaceId)
            payload.id = workspaceId;
        if (runtime)
            payload.runtime = runtime;
        if (displayName)
            payload.display_name = displayName;
        await startHubJob("/hub/jobs/agent-workspaces", {
            body: payload,
            startedMessage: "Agent workspace creation queued",
        });
        flash(`Created agent workspace: ${workspaceId || displayName || "workspace"}`, "success");
        await refreshHub();
        return true;
    }
    catch (err) {
        flash(err.message || "Failed to create agent workspace", "error");
        return false;
    }
}
export async function handleCreateRepoSubmit() {
    const idInput = document.getElementById("create-repo-id");
    const pathInput = document.getElementById("create-repo-path");
    const urlInput = document.getElementById("create-repo-url");
    const gitCheck = document.getElementById("create-repo-git");
    const repoId = idInput?.value?.trim() || null;
    const repoPath = pathInput?.value?.trim() || null;
    const gitUrl = urlInput?.value?.trim() || null;
    const gitInit = gitCheck?.checked ?? true;
    if (!repoId && !gitUrl) {
        flash("Repo ID or Git URL is required", "error");
        return;
    }
    const ok = await createRepo(repoId, repoPath, gitInit, gitUrl);
    if (ok) {
        hideCreateRepoModal();
    }
}
export async function handleCreateAgentWorkspaceSubmit() {
    const idInput = document.getElementById("create-agent-workspace-id");
    const runtimeInput = document.getElementById("create-agent-workspace-runtime");
    const nameInput = document.getElementById("create-agent-workspace-name");
    const workspaceId = idInput?.value?.trim() || null;
    const runtime = runtimeInput?.value?.trim() || null;
    const displayName = nameInput?.value?.trim() || null;
    if (!workspaceId || !runtime) {
        flash("Workspace ID and runtime are required", "error");
        return;
    }
    const ok = await createAgentWorkspace(workspaceId, runtime, displayName);
    if (ok) {
        hideCreateAgentWorkspaceModal();
    }
}
export function initHubSettings() {
    const settingsBtns = Array.from(document.querySelectorAll("#hub-settings, #pma-settings"));
    const modal = document.getElementById("hub-settings-modal");
    const closeBtn = document.getElementById("hub-settings-close");
    const updateBtn = document.getElementById("hub-update-btn");
    const updateTarget = document.getElementById("hub-update-target");
    void loadUpdateTargetOptions(updateTarget ? updateTarget.id : null);
    let closeModal = null;
    const hideModal = () => {
        if (closeModal) {
            const close = closeModal;
            closeModal = null;
            close();
        }
    };
    if (modal && settingsBtns.length > 0) {
        settingsBtns.forEach((settingsBtn) => {
            settingsBtn.addEventListener("click", () => {
                const triggerEl = document.activeElement;
                hideModal();
                closeModal = openModal(modal, {
                    initialFocus: closeBtn || updateBtn || modal,
                    returnFocusTo: triggerEl,
                    onRequestClose: hideModal,
                });
            });
        });
    }
    if (closeBtn && modal) {
        closeBtn.addEventListener("click", () => {
            hideModal();
        });
    }
    if (updateBtn) {
        updateBtn.addEventListener("click", () => handleSystemUpdate("hub-update-btn", updateTarget ? updateTarget.id : null));
    }
}
export { promptAndSetRepoDestination, promptAndSetAgentWorkspaceDestination, openRepoSettingsModal, removeRepoWithChecks, };
