// GENERATED FILE - do not edit directly. Source: static_src/
export class DocEditor {
    constructor(config) {
        this.saveTimer = null;
        this.lastSavedContent = "";
        this.status = "idle";
        this.destroyed = false;
        this.handleKeydown = (evt) => {
            const active = document.activeElement;
            const isTextarea = active === this.config.textarea;
            if (!isTextarea)
                return;
            if ((evt.metaKey || evt.ctrlKey) && evt.key.toLowerCase() === "s") {
                evt.preventDefault();
                void this.save(true);
            }
        };
        this.handleBeforeUnload = (evt) => {
            if (this.status === "dirty" || this.status === "saving") {
                evt.preventDefault();
                evt.returnValue = "Unsaved changes";
            }
        };
        const { autoSaveDelay = 2000, enableKeyboardSave = true, saveButton = null, statusEl = null, } = config;
        this.config = {
            ...config,
            autoSaveDelay,
            enableKeyboardSave,
            saveButton,
            statusEl,
        };
        this.init();
    }
    init() {
        void this.load();
        this.config.textarea.addEventListener("input", () => {
            this.markDirty();
            this.scheduleSave();
        });
        this.config.textarea.addEventListener("blur", () => {
            void this.save();
        });
        if (this.config.saveButton) {
            this.config.saveButton.addEventListener("click", () => void this.save(true));
        }
        if (this.config.enableKeyboardSave) {
            document.addEventListener("keydown", this.handleKeydown);
            window.addEventListener("beforeunload", this.handleBeforeUnload);
        }
    }
    destroy() {
        this.destroyed = true;
        if (this.saveTimer)
            clearTimeout(this.saveTimer);
        document.removeEventListener("keydown", this.handleKeydown);
        window.removeEventListener("beforeunload", this.handleBeforeUnload);
    }
    async load() {
        const content = await this.config.onLoad();
        this.lastSavedContent = content ?? "";
        this.config.textarea.value = this.lastSavedContent;
        this.setStatus("saved");
    }
    scheduleSave() {
        if (this.saveTimer)
            clearTimeout(this.saveTimer);
        this.saveTimer = setTimeout(() => void this.save(), this.config.autoSaveDelay);
    }
    markDirty() {
        if (this.status !== "dirty") {
            this.setStatus("dirty");
        }
    }
    setStatus(status) {
        this.status = status;
        const { statusEl, saveButton } = this.config;
        if (statusEl) {
            statusEl.textContent = this.statusLabel(status);
            statusEl.classList.toggle("muted", status === "saved" || status === "idle");
            statusEl.classList.toggle("error", status === "error");
            statusEl.classList.toggle("dirty", status === "dirty");
        }
        if (saveButton) {
            if (status === "saving")
                saveButton.setAttribute("disabled", "true");
            else
                saveButton.removeAttribute("disabled");
        }
    }
    statusLabel(status) {
        switch (status) {
            case "saving":
                return "Saving…";
            case "saved":
                return "Saved";
            case "error":
                return "Save failed";
            case "dirty":
                return "Unsaved changes";
            default:
                return "";
        }
    }
    async save(force = false) {
        if (this.destroyed)
            return;
        if (this.saveTimer) {
            clearTimeout(this.saveTimer);
            this.saveTimer = null;
        }
        const value = this.config.textarea.value;
        if (!force && value === this.lastSavedContent)
            return;
        this.setStatus("saving");
        try {
            const maybeHash = await this.config.onSave(value, this.baseHash);
            if (typeof maybeHash === "string") {
                this.baseHash = maybeHash;
            }
            this.lastSavedContent = value;
            this.setStatus("saved");
            // Clear saved indicator after a short delay to keep UI calm
            setTimeout(() => {
                if (this.status === "saved")
                    this.setStatus("idle");
            }, 1200);
        }
        catch (err) {
            console.error("DocEditor save failed", err);
            this.setStatus("error");
        }
    }
}
