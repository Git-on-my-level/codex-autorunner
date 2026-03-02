// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Ticket Templates - Template picker for creating tickets from templates
 */
import { api, flash } from "./utils.js";
import { openTicketEditor } from "./ticketEditor.js";
const TEMPLATE_HISTORY_KEY = "car:ticket-template-history";
const MAX_HISTORY_ITEMS = 10;
const FETCH_DEBOUNCE_MS = 500;
const state = {
    isOpen: false,
    enabled: false,
    loading: false,
    previewContent: null,
    lastFetchedRef: null,
    repos: [],
    fetchDebounceTimer: null,
};
function els() {
    return {
        modal: document.getElementById("ticket-template-modal"),
        refInput: document.getElementById("ticket-template-ref"),
        clearBtn: document.getElementById("ticket-template-clear"),
        agentSelect: document.getElementById("ticket-template-agent"),
        preview: document.getElementById("ticket-template-preview"),
        previewStatus: document.getElementById("ticket-template-preview-status"),
        error: document.getElementById("ticket-template-error"),
        cancelBtn: document.getElementById("ticket-template-cancel"),
        applyBtn: document.getElementById("ticket-template-apply"),
        closeBtn: document.getElementById("ticket-template-close"),
        reposContainer: document.getElementById("ticket-template-repos"),
        recentContainer: document.getElementById("ticket-template-recent"),
        inputHint: document.getElementById("ticket-template-hint"),
        // Split button
        dropdownToggle: document.getElementById("ticket-new-dropdown-toggle"),
        dropdown: document.getElementById("ticket-new-dropdown"),
        fromTemplateBtn: document.getElementById("ticket-new-from-template"),
        // Mobile
        overflowTemplate: document.getElementById("ticket-overflow-template"),
    };
}
/**
 * Parse a GitHub URL into template reference format
 * Supports:
 * - https://github.com/owner/repo/blob/branch/path/to/file.md
 * - https://raw.githubusercontent.com/owner/repo/branch/path/to/file.md
 * Returns null if not a valid GitHub URL
 */
function parseGitHubUrl(input) {
    const trimmed = input.trim();
    // GitHub blob URL: https://github.com/owner/repo/blob/branch/path/to/file.md
    const blobMatch = trimmed.match(/^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/blob\/([^/]+)\/(.+)$/);
    if (blobMatch) {
        const [, owner, repo, ref, path] = blobMatch;
        const ownerRepo = `${owner}/${repo}`;
        // Try to find matching repo by URL pattern
        const configuredRepoId = findRepoIdByUrl(`github.com/${ownerRepo}`);
        return {
            repoId: configuredRepoId || ownerRepo,
            path,
            ref,
            isConfigured: configuredRepoId !== null,
            originalOwnerRepo: ownerRepo,
        };
    }
    // Raw GitHub URL: https://raw.githubusercontent.com/owner/repo/branch/path/to/file.md
    const rawMatch = trimmed.match(/^https?:\/\/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.+)$/);
    if (rawMatch) {
        const [, owner, repo, ref, path] = rawMatch;
        const ownerRepo = `${owner}/${repo}`;
        const configuredRepoId = findRepoIdByUrl(`github.com/${ownerRepo}`);
        return {
            repoId: configuredRepoId || ownerRepo,
            path,
            ref,
            isConfigured: configuredRepoId !== null,
            originalOwnerRepo: ownerRepo,
        };
    }
    return null;
}
/**
 * Find a configured repo ID by matching URL pattern
 */
function findRepoIdByUrl(urlFragment) {
    for (const repo of state.repos) {
        if (repo.url.includes(urlFragment)) {
            return repo.id;
        }
    }
    return null;
}
/**
 * Convert input to template reference format
 * Handles both direct refs and GitHub URLs
 */
function normalizeTemplateRef(input) {
    const trimmed = input.trim();
    if (!trimmed)
        return { ref: "", isGitHubUrl: false, isConfigured: true };
    // Check if it's a GitHub URL
    const parsed = parseGitHubUrl(trimmed);
    if (parsed) {
        return {
            ref: `${parsed.repoId}:${parsed.path}@${parsed.ref}`,
            isGitHubUrl: true,
            isConfigured: parsed.isConfigured,
            originalOwnerRepo: parsed.originalOwnerRepo,
        };
    }
    // Already in correct format - check if repo part is configured
    const colonIdx = trimmed.indexOf(":");
    if (colonIdx > 0) {
        const repoId = trimmed.slice(0, colonIdx);
        const isConfigured = state.repos.some((r) => r.id === repoId);
        return { ref: trimmed, isGitHubUrl: false, isConfigured };
    }
    return { ref: trimmed, isGitHubUrl: false, isConfigured: true };
}
/**
 * Load template history from localStorage
 */
function loadHistory() {
    try {
        const raw = localStorage.getItem(TEMPLATE_HISTORY_KEY);
        if (!raw)
            return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
    }
    catch {
        return [];
    }
}
/**
 * Save template ref to history
 */
function saveToHistory(ref) {
    const history = loadHistory();
    const filtered = history.filter((h) => h !== ref);
    filtered.unshift(ref);
    const trimmed = filtered.slice(0, MAX_HISTORY_ITEMS);
    try {
        localStorage.setItem(TEMPLATE_HISTORY_KEY, JSON.stringify(trimmed));
    }
    catch {
        // Ignore storage errors
    }
}
/**
 * Get the current repo prefix from the input
 */
function getCurrentRepoPrefix() {
    const { refInput } = els();
    if (!refInput)
        return null;
    const value = refInput.value.trim();
    const colonIdx = value.indexOf(":");
    if (colonIdx > 0) {
        return value.slice(0, colonIdx);
    }
    return null;
}
/**
 * Render available repos as chips (toggleable)
 */
function renderRepos() {
    const { reposContainer } = els();
    if (!reposContainer)
        return;
    if (state.repos.length === 0) {
        reposContainer.innerHTML = '<span class="muted small">No template repos configured</span>';
        return;
    }
    const currentPrefix = getCurrentRepoPrefix();
    reposContainer.innerHTML = "";
    for (const repo of state.repos) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "ticket-template-chip";
        chip.dataset.repoId = repo.id;
        // Highlight if this repo is currently selected
        if (currentPrefix === repo.id) {
            chip.classList.add("active");
        }
        chip.textContent = repo.id;
        chip.title = `${repo.url}${repo.trusted ? " (trusted)" : ""}`;
        chip.addEventListener("click", () => {
            const { refInput } = els();
            if (!refInput)
                return;
            const isCurrentlyActive = chip.classList.contains("active");
            if (isCurrentlyActive) {
                // Deselect - clear the repo prefix but keep the path if any
                const colonIdx = refInput.value.indexOf(":");
                if (colonIdx > 0) {
                    refInput.value = refInput.value.slice(colonIdx + 1);
                }
            }
            else {
                // Select - prepend repo prefix
                const colonIdx = refInput.value.indexOf(":");
                const pathPart = colonIdx > 0 ? refInput.value.slice(colonIdx + 1) : refInput.value;
                refInput.value = `${repo.id}:${pathPart}`;
            }
            refInput.focus();
            refInput.setSelectionRange(refInput.value.length, refInput.value.length);
            // Update chip states
            updateRepoChipStates();
            // Clear preview since ref changed
            clearPreview();
            hideError();
            updateInputHint();
        });
        reposContainer.appendChild(chip);
    }
}
/**
 * Update repo chip active states based on current input
 */
function updateRepoChipStates() {
    const { reposContainer } = els();
    if (!reposContainer)
        return;
    const currentPrefix = getCurrentRepoPrefix();
    const chips = reposContainer.querySelectorAll(".ticket-template-chip");
    chips.forEach((chip) => {
        const repoId = chip.dataset.repoId;
        if (repoId === currentPrefix) {
            chip.classList.add("active");
        }
        else {
            chip.classList.remove("active");
        }
    });
}
/**
 * Clear preview state
 */
function clearPreview() {
    const { preview, applyBtn, previewStatus } = els();
    if (preview) {
        preview.textContent = "Template content will appear here after you enter a reference above.";
        preview.classList.remove("has-content", "loading");
    }
    if (applyBtn)
        applyBtn.disabled = true;
    if (previewStatus)
        previewStatus.textContent = "";
    state.previewContent = null;
    state.lastFetchedRef = null;
}
/**
 * Update the input hint based on current state
 */
function updateInputHint() {
    const { refInput, inputHint } = els();
    if (!inputHint || !refInput)
        return;
    const value = refInput.value.trim();
    if (!value) {
        inputHint.textContent = "or paste GitHub URL";
        inputHint.classList.remove("hidden");
    }
    else if (value.includes(":") && value.includes("/")) {
        // Looks like a complete reference
        inputHint.classList.add("hidden");
    }
    else if (value.includes(":")) {
        // Has repo prefix, needs path
        inputHint.textContent = "add path to template file";
        inputHint.classList.remove("hidden");
    }
    else {
        inputHint.textContent = "or paste GitHub URL";
        inputHint.classList.remove("hidden");
    }
}
/**
 * Render recent templates as clickable chips
 */
function renderRecentTemplates() {
    const { recentContainer } = els();
    if (!recentContainer)
        return;
    const history = loadHistory();
    if (history.length === 0) {
        recentContainer.classList.add("hidden");
        return;
    }
    recentContainer.classList.remove("hidden");
    recentContainer.innerHTML = "";
    const label = document.createElement("span");
    label.className = "ticket-template-label";
    label.textContent = "Recent";
    recentContainer.appendChild(label);
    const chipsDiv = document.createElement("div");
    chipsDiv.className = "ticket-template-chips";
    // Show up to 5 recent templates
    for (const ref of history.slice(0, 5)) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "ticket-template-chip ticket-template-chip-recent";
        // Show shortened version: just the path part
        const parts = ref.split(":");
        const displayText = parts.length > 1 ? parts.slice(1).join(":").split("@")[0] : ref;
        chip.textContent = displayText.length > 30 ? "..." + displayText.slice(-27) : displayText;
        chip.title = ref;
        chip.addEventListener("click", () => {
            const { refInput } = els();
            if (refInput) {
                refInput.value = ref;
                void fetchTemplatePreview();
            }
        });
        chipsDiv.appendChild(chip);
    }
    recentContainer.appendChild(chipsDiv);
}
/**
 * Format a user-friendly error message
 */
function formatErrorMessage(rawMessage) {
    // Try to parse JSON error format from backend
    try {
        // Check if it looks like JSON
        if (rawMessage.startsWith("{") || rawMessage.includes('"code"')) {
            const parsed = JSON.parse(rawMessage);
            if (parsed.message) {
                return parsed.message;
            }
        }
    }
    catch {
        // Not JSON, use as-is
    }
    // Clean up common error patterns
    let msg = rawMessage;
    // Remove JSON-like prefixes
    msg = msg.replace(/^\{"code":"[^"]+","message":"?/, "").replace(/"?\}$/, "");
    // Make repo errors more helpful
    if (msg.includes("Template repo not configured")) {
        const match = msg.match(/not configured:\s*(.+)/);
        if (match) {
            const repoId = match[1].trim();
            const availableRepos = state.repos.map((r) => r.id);
            if (availableRepos.length > 0) {
                return `Repository "${repoId}" is not configured. Available repos: ${availableRepos.join(", ")}`;
            }
            return `Repository "${repoId}" is not configured. Add it to your templates config first.`;
        }
    }
    return msg;
}
/**
 * Show error message
 */
function showError(message) {
    const { error } = els();
    if (!error)
        return;
    error.textContent = formatErrorMessage(message);
    error.classList.remove("hidden");
}
/**
 * Hide error message
 */
function hideError() {
    const { error } = els();
    if (!error)
        return;
    error.textContent = "";
    error.classList.add("hidden");
    error.classList.remove("warning");
}
/**
 * Update loading state
 */
function setLoading(loading) {
    state.loading = loading;
    const { applyBtn, previewStatus, preview } = els();
    if (applyBtn)
        applyBtn.disabled = loading || !state.previewContent;
    if (previewStatus) {
        previewStatus.textContent = loading ? "Loading..." : "";
    }
    if (preview && loading) {
        preview.classList.add("loading");
    }
    else if (preview) {
        preview.classList.remove("loading");
    }
}
/**
 * Check if templates are enabled and update UI accordingly
 */
export async function checkTemplatesEnabled() {
    try {
        const data = (await api("/api/templates/repos"));
        state.enabled = data.enabled;
        state.repos = data.repos || [];
        const { dropdownToggle, overflowTemplate } = els();
        if (state.enabled) {
            dropdownToggle?.classList.remove("hidden");
            overflowTemplate?.classList.remove("hidden");
        }
        else {
            dropdownToggle?.classList.add("hidden");
            overflowTemplate?.classList.add("hidden");
        }
        return state.enabled;
    }
    catch {
        state.enabled = false;
        state.repos = [];
        return false;
    }
}
/**
 * Fetch template content for preview (debounced version)
 */
function debouncedFetchPreview() {
    if (state.fetchDebounceTimer) {
        clearTimeout(state.fetchDebounceTimer);
    }
    state.fetchDebounceTimer = setTimeout(() => {
        void fetchTemplatePreview();
    }, FETCH_DEBOUNCE_MS);
}
/**
 * Fetch template content for preview
 */
async function fetchTemplatePreview() {
    const { refInput, preview, applyBtn, previewStatus } = els();
    if (!refInput || !preview)
        return;
    const rawInput = refInput.value.trim();
    if (!rawInput) {
        preview.textContent = "Enter a template reference to see preview.";
        preview.classList.remove("has-content");
        state.previewContent = null;
        state.lastFetchedRef = null;
        if (applyBtn)
            applyBtn.disabled = true;
        if (previewStatus)
            previewStatus.textContent = "";
        return;
    }
    // Normalize the input (handle GitHub URLs)
    const normalized = normalizeTemplateRef(rawInput);
    const templateRef = normalized.ref;
    // Handle non-configured repos before making API call
    if (!normalized.isConfigured) {
        const availableRepos = state.repos.map((r) => r.id);
        if (normalized.isGitHubUrl) {
            // GitHub URL to non-configured repo
            let msg = `Repository "${normalized.originalOwnerRepo}" is not configured. `;
            msg += "To use templates from this repo, add it to templates.repos in your config (can be trusted or untrusted).";
            if (availableRepos.length > 0) {
                msg += ` Currently available: ${availableRepos.join(", ")}`;
            }
            showError(msg);
        }
        else {
            // Direct reference to non-configured repo
            const colonIdx = templateRef.indexOf(":");
            const repoId = colonIdx > 0 ? templateRef.slice(0, colonIdx) : templateRef;
            let msg = `Repository "${repoId}" is not configured. Add it to templates.repos in your config first.`;
            if (availableRepos.length > 0) {
                msg += ` Available: ${availableRepos.join(", ")}`;
            }
            showError(msg);
        }
        preview.textContent = "";
        preview.classList.remove("has-content");
        state.previewContent = null;
        state.lastFetchedRef = null;
        if (applyBtn)
            applyBtn.disabled = true;
        if (previewStatus)
            previewStatus.textContent = "";
        return;
    }
    // Update input if we normalized a GitHub URL
    if (normalized.isGitHubUrl && templateRef !== rawInput) {
        refInput.value = templateRef;
        if (previewStatus)
            previewStatus.textContent = "Converted from GitHub URL";
        // Update chip states since input changed
        updateRepoChipStates();
    }
    // Don't refetch if same ref
    if (templateRef === state.lastFetchedRef && state.previewContent) {
        return;
    }
    setLoading(true);
    hideError();
    try {
        const data = (await api("/api/templates/fetch", {
            method: "POST",
            body: { template: templateRef },
        }));
        state.previewContent = data.content;
        state.lastFetchedRef = templateRef;
        // Show preview with truncation for very long templates
        const maxPreviewLines = 30;
        const lines = data.content.split("\n");
        if (lines.length > maxPreviewLines) {
            preview.textContent = lines.slice(0, maxPreviewLines).join("\n") + "\n... (truncated)";
        }
        else {
            preview.textContent = data.content;
        }
        preview.classList.add("has-content");
        // Show scan info if untrusted
        if (!data.trusted && data.scan_decision) {
            if (previewStatus) {
                previewStatus.textContent = `Scanned: ${data.scan_decision.decision} (${data.scan_decision.severity})`;
            }
        }
        else if (previewStatus && !previewStatus.textContent?.includes("Converted")) {
            previewStatus.textContent = data.trusted ? "Trusted" : "Scanned";
        }
        if (applyBtn)
            applyBtn.disabled = false;
    }
    catch (err) {
        const error = err;
        const message = error.detail?.message || error.message || "Failed to fetch template";
        showError(message);
        preview.textContent = "";
        preview.classList.remove("has-content");
        state.previewContent = null;
        state.lastFetchedRef = null;
        if (applyBtn)
            applyBtn.disabled = true;
        if (previewStatus)
            previewStatus.textContent = "";
    }
    finally {
        setLoading(false);
    }
}
/**
 * Apply template to create a new ticket
 */
async function applyTemplate() {
    const { refInput, agentSelect } = els();
    if (!refInput)
        return;
    const rawInput = refInput.value.trim();
    if (!rawInput) {
        showError("Please enter a template reference.");
        return;
    }
    const normalized = normalizeTemplateRef(rawInput);
    const templateRef = normalized.ref;
    if (!normalized.isConfigured) {
        showError("This repository is not configured. Please use a configured template repo.");
        return;
    }
    setLoading(true);
    hideError();
    try {
        const body = { template: templateRef };
        const agentOverride = agentSelect?.value;
        if (agentOverride) {
            body.set_agent = agentOverride;
        }
        const result = (await api("/api/templates/apply", {
            method: "POST",
            body,
        }));
        // Save to history on success
        saveToHistory(templateRef);
        // Close template modal
        closeTemplateModal();
        // Fetch the created ticket and open in editor
        try {
            const ticketData = await api(`/api/flows/ticket_flow/tickets/${result.index}`);
            openTicketEditor(ticketData);
        }
        catch {
            flash(`Created ${result.filename}`, "success");
        }
    }
    catch (err) {
        const error = err;
        const message = error.detail?.message || error.message || "Failed to create ticket from template";
        showError(message);
    }
    finally {
        setLoading(false);
    }
}
/**
 * Open the template picker modal
 */
export function openTemplateModal() {
    const { modal, refInput, preview, applyBtn, previewStatus, clearBtn } = els();
    if (!modal)
        return;
    // Reset state
    state.isOpen = true;
    state.previewContent = null;
    state.lastFetchedRef = null;
    // Clear form and set dynamic placeholder
    if (refInput) {
        refInput.value = "";
        // Set placeholder with actual repo name if available
        const repoId = state.repos.length > 0 ? state.repos[0].id : "repo";
        refInput.placeholder = `${repoId}:path/to/template.md`;
    }
    if (preview) {
        preview.textContent = "Template content will appear here after you enter a reference above.";
        preview.classList.remove("has-content", "loading");
    }
    if (applyBtn)
        applyBtn.disabled = true;
    if (previewStatus)
        previewStatus.textContent = "";
    // Hide clear button initially
    if (clearBtn)
        clearBtn.classList.add("hidden");
    hideError();
    renderRepos();
    renderRecentTemplates();
    updateInputHint();
    // Show modal
    modal.classList.remove("hidden");
    // Focus input
    refInput?.focus();
    // Close any open dropdowns
    const { dropdown } = els();
    dropdown?.classList.add("hidden");
}
/**
 * Close the template picker modal
 */
export function closeTemplateModal() {
    const { modal } = els();
    if (!modal)
        return;
    // Clear any pending debounce
    if (state.fetchDebounceTimer) {
        clearTimeout(state.fetchDebounceTimer);
        state.fetchDebounceTimer = null;
    }
    state.isOpen = false;
    modal.classList.add("hidden");
}
/**
 * Toggle the split button dropdown
 */
function toggleDropdown() {
    const { dropdown } = els();
    if (!dropdown)
        return;
    dropdown.classList.toggle("hidden");
}
/**
 * Close dropdown when clicking outside
 */
function handleDocumentClick(e) {
    const { dropdown, dropdownToggle } = els();
    if (!dropdown || !dropdownToggle)
        return;
    if (!dropdown.contains(e.target) && !dropdownToggle.contains(e.target)) {
        dropdown.classList.add("hidden");
    }
}
/**
 * Update clear button visibility
 */
function updateClearButton() {
    const { refInput, clearBtn } = els();
    if (!clearBtn || !refInput)
        return;
    if (refInput.value.trim()) {
        clearBtn.classList.remove("hidden");
    }
    else {
        clearBtn.classList.add("hidden");
    }
}
/**
 * Clear input and reset state
 */
function clearInput() {
    const { refInput } = els();
    if (!refInput)
        return;
    refInput.value = "";
    clearPreview();
    hideError();
    updateRepoChipStates();
    updateInputHint();
    updateClearButton();
    refInput.focus();
}
/**
 * Initialize the template picker
 */
export function initTicketTemplates() {
    const { modal, refInput, clearBtn, cancelBtn, applyBtn, closeBtn, dropdownToggle, fromTemplateBtn, overflowTemplate, } = els();
    if (!modal)
        return;
    // Prevent double initialization
    if (modal.dataset.templateInitialized === "1")
        return;
    modal.dataset.templateInitialized = "1";
    // Check if templates are enabled and load repos
    void checkTemplatesEnabled();
    // Split button dropdown toggle
    dropdownToggle?.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleDropdown();
    });
    // "From Template" button in dropdown
    fromTemplateBtn?.addEventListener("click", () => {
        openTemplateModal();
    });
    // Mobile overflow template button
    overflowTemplate?.addEventListener("click", () => {
        const overflowDropdown = document.getElementById("ticket-overflow-dropdown");
        overflowDropdown?.classList.add("hidden");
        openTemplateModal();
    });
    // Modal close button
    closeBtn?.addEventListener("click", closeTemplateModal);
    cancelBtn?.addEventListener("click", closeTemplateModal);
    // Clear button
    clearBtn?.addEventListener("click", clearInput);
    // Apply button
    applyBtn?.addEventListener("click", () => void applyTemplate());
    // Auto-fetch on input change (debounced)
    refInput?.addEventListener("input", () => {
        const value = refInput.value.trim();
        updateClearButton();
        updateRepoChipStates();
        updateInputHint();
        if (value) {
            debouncedFetchPreview();
        }
        else {
            clearPreview();
            hideError();
        }
    });
    // Handle paste - immediately try to fetch
    refInput?.addEventListener("paste", () => {
        // Wait for paste to complete
        setTimeout(() => {
            updateClearButton();
            updateRepoChipStates();
            updateInputHint();
            const value = refInput.value.trim();
            if (value) {
                void fetchTemplatePreview();
            }
        }, 0);
    });
    // Enter key: if preview loaded, apply; otherwise fetch preview
    refInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            if (state.previewContent && !state.loading) {
                void applyTemplate();
            }
            else {
                void fetchTemplatePreview();
            }
        }
    });
    // Cmd/Ctrl+Enter always applies if preview is ready
    document.addEventListener("keydown", (e) => {
        if (state.isOpen && (e.metaKey || e.ctrlKey) && e.key === "Enter") {
            if (state.previewContent && !state.loading) {
                e.preventDefault();
                void applyTemplate();
            }
        }
    });
    // Close modal on backdrop click
    modal.addEventListener("click", (e) => {
        if (e.target === modal) {
            closeTemplateModal();
        }
    });
    // Close modal on Escape
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && state.isOpen) {
            closeTemplateModal();
        }
    });
    // Close dropdown when clicking elsewhere
    document.addEventListener("click", handleDocumentClick);
}
