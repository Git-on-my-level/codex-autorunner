type SaveStatus = "idle" | "saving" | "saved" | "error" | "dirty";

export interface DocEditorConfig {
  target: string;
  textarea: HTMLTextAreaElement;
  saveButton?: HTMLButtonElement | null;
  statusEl?: HTMLElement | null;
  onSave: (content: string, baseHash?: string) => Promise<string | void> | string | void;
  onLoad: () => Promise<string> | string;
  autoSaveDelay?: number;
  enableKeyboardSave?: boolean;
}

export class DocEditor {
  private config: Required<DocEditorConfig>;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private lastSavedContent = "";
  private status: SaveStatus = "idle";
  private destroyed = false;
  private baseHash: string | undefined;

  constructor(config: DocEditorConfig) {
    const {
      autoSaveDelay = 2000,
      enableKeyboardSave = true,
      saveButton = null,
      statusEl = null,
    } = config;

    this.config = {
      ...config,
      autoSaveDelay,
      enableKeyboardSave,
      saveButton,
      statusEl,
    };

    this.init();
  }

  private init(): void {
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

  destroy(): void {
    this.destroyed = true;
    if (this.saveTimer) clearTimeout(this.saveTimer);
    document.removeEventListener("keydown", this.handleKeydown);
    window.removeEventListener("beforeunload", this.handleBeforeUnload);
  }

  async load(): Promise<void> {
    const content = await this.config.onLoad();
    this.lastSavedContent = content ?? "";
    this.config.textarea.value = this.lastSavedContent;
    this.setStatus("saved");
  }

  private handleKeydown = (evt: KeyboardEvent): void => {
    const active = document.activeElement;
    const isTextarea = active === this.config.textarea;
    if (!isTextarea) return;

    if ((evt.metaKey || evt.ctrlKey) && evt.key.toLowerCase() === "s") {
      evt.preventDefault();
      void this.save(true);
    }
  };

  private handleBeforeUnload = (evt: BeforeUnloadEvent): void => {
    if (this.status === "dirty" || this.status === "saving") {
      evt.preventDefault();
      evt.returnValue = "Unsaved changes";
    }
  };

  private scheduleSave(): void {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => void this.save(), this.config.autoSaveDelay);
  }

  private markDirty(): void {
    if (this.status !== "dirty") {
      this.setStatus("dirty");
    }
  }

  private setStatus(status: SaveStatus): void {
    this.status = status;
    const { statusEl, saveButton } = this.config;
    if (statusEl) {
      statusEl.textContent = this.statusLabel(status);
      statusEl.classList.toggle("muted", status === "saved" || status === "idle");
      statusEl.classList.toggle("error", status === "error");
      statusEl.classList.toggle("dirty", status === "dirty");
    }
    if (saveButton) {
      if (status === "saving") saveButton.setAttribute("disabled", "true");
      else saveButton.removeAttribute("disabled");
    }
  }

  private statusLabel(status: SaveStatus): string {
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

  async save(force = false): Promise<void> {
    if (this.destroyed) return;
    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    const value = this.config.textarea.value;
    if (!force && value === this.lastSavedContent) return;

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
        if (this.status === "saved") this.setStatus("idle");
      }, 1200);
    } catch (err) {
      console.error("DocEditor save failed", err);
      this.setStatus("error");
    }
  }
}
