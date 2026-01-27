import { WorkspaceFileListItem } from "./workspaceApi.js";

type ChangeHandler = (file: WorkspaceFileListItem) => void;

interface BrowserOptions {
  container: HTMLElement;
  selectEl: HTMLSelectElement | null;
  onSelect: ChangeHandler;
}

export class WorkspaceFileBrowser {
  private files: WorkspaceFileListItem[] = [];
  private currentPath: string | null = null;
  private readonly container: HTMLElement;
  private readonly selectEl: HTMLSelectElement | null;
  private readonly onSelect: ChangeHandler;
  // Mobile file picker elements
  private readonly fileBtnEl: HTMLElement | null;
  private readonly fileBtnNameEl: HTMLElement | null;
  private readonly modalEl: HTMLElement | null;
  private readonly modalBodyEl: HTMLElement | null;
  private readonly modalCloseEl: HTMLElement | null;

  constructor(options: BrowserOptions) {
    this.container = options.container;
    this.selectEl = options.selectEl;
    this.onSelect = options.onSelect;
    // Mobile file picker modal elements
    this.fileBtnEl = document.getElementById("workspace-file-pill");
    this.fileBtnNameEl = document.getElementById("workspace-file-pill-name");
    this.modalEl = document.getElementById("file-picker-modal");
    this.modalBodyEl = document.getElementById("file-picker-body");
    this.modalCloseEl = document.getElementById("file-picker-close");
    this.initFilePicker();
  }

  private initFilePicker(): void {
    if (this.fileBtnEl) {
      this.fileBtnEl.addEventListener("click", (e) => {
        e.stopPropagation();
        this.openModal();
      });
    }
    if (this.modalCloseEl) {
      this.modalCloseEl.addEventListener("click", () => this.closeModal());
    }
    if (this.modalEl) {
      this.modalEl.addEventListener("click", (e) => {
        if (e.target === this.modalEl) this.closeModal();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !this.modalEl!.hidden) this.closeModal();
      });
    }
  }

  private openModal(): void {
    if (!this.modalEl) return;
    this.renderModal();
    this.modalEl.hidden = false;
    this.modalBodyEl?.querySelector<HTMLElement>(".file-picker-item")?.focus();
  }

  private closeModal(): void {
    if (this.modalEl) this.modalEl.hidden = true;
  }

  private renderModal(): void {
    if (!this.modalBodyEl) return;
    this.modalBodyEl.innerHTML = "";
    const pinned = this.files.filter((f) => f.is_pinned);
    const others = this.files.filter((f) => !f.is_pinned);

    const renderSection = (items: WorkspaceFileListItem[], title?: string): void => {
      if (!items.length) return;
      const section = document.createElement("div");
      section.className = "file-picker-section";
      if (title) {
        const header = document.createElement("div");
        header.className = "file-picker-section-title";
        header.textContent = title;
        section.appendChild(header);
      }
      items.forEach((f) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "file-picker-item" + (f.path === this.currentPath ? " active" : "");
        const nameSpan = document.createElement("span");
        nameSpan.className = "file-picker-item-name";
        nameSpan.textContent = f.name;
        item.appendChild(nameSpan);
        item.addEventListener("click", () => {
          this.select(f.path);
          this.closeModal();
        });
        section.appendChild(item);
      });
      this.modalBodyEl!.appendChild(section);
    };

    renderSection(pinned, "Pinned");
    renderSection(others, others.length && pinned.length ? "Files" : undefined);
  }

  private updateFileName(name: string): void {
    if (this.fileBtnNameEl) this.fileBtnNameEl.textContent = name || "Select file";
  }

  setFiles(files: WorkspaceFileListItem[], defaultPath?: string): void {
    this.files = files;
    this.render();
    if (files.length) {
      const initial = defaultPath || files[0].path;
      this.select(initial);
    }
  }

  select(path: string): void {
    const file = this.files.find((f) => f.path === path);
    if (!file) return;
    this.currentPath = path;
    this.highlight(path);
    this.updateFileName(file.name);
    if (this.selectEl) this.selectEl.value = path;
    this.onSelect(file);
  }

  private render(): void {
    this.container.innerHTML = "";
    const pinned = this.files.filter((f) => f.is_pinned);
    const others = this.files.filter((f) => !f.is_pinned);

    if (this.selectEl) {
      this.selectEl.innerHTML = "";
      this.files.forEach((f) => {
        const opt = document.createElement("option");
        opt.value = f.path;
        opt.textContent = f.name;
        this.selectEl!.appendChild(opt);
      });
      this.selectEl.onchange = () => {
        this.select(this.selectEl!.value);
      };
    }

    const renderList = (items: WorkspaceFileListItem[], title?: string): void => {
      if (!items.length) return;
      if (title) {
        const header = document.createElement("div");
        header.className = "workspace-file-header";
        header.textContent = title;
        this.container.appendChild(header);
      }
      items.forEach((f) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "workspace-file-row";
        row.dataset.path = f.path;
        row.textContent = f.name;
        if (f.is_pinned) row.classList.add("pinned");
        row.addEventListener("click", () => this.select(f.path));
        this.container.appendChild(row);
      });
    };

    renderList(pinned, "Pinned");
    if (pinned.length && others.length) {
      const divider = document.createElement("div");
      divider.className = "workspace-file-divider";
      this.container.appendChild(divider);
    }
    renderList(others, others.length ? "Files" : undefined);
  }

  private highlight(path: string): void {
    Array.from(this.container.querySelectorAll<HTMLElement>(".workspace-file-row")).forEach((row) => {
      row.classList.toggle("active", row.dataset.path === path);
    });
  }
}
