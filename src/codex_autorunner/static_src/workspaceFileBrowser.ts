import { WorkspaceFileListItem } from "./workspaceApi.js";

type ChangeHandler = (file: WorkspaceFileListItem) => void;

interface BrowserOptions {
  container: HTMLElement;
  selectEl: HTMLSelectElement | null;
  onSelect: ChangeHandler;
}

export class WorkspaceFileBrowser {
  private files: WorkspaceFileListItem[] = [];
  private readonly container: HTMLElement;
  private readonly selectEl: HTMLSelectElement | null;
  private readonly onSelect: ChangeHandler;

  constructor(options: BrowserOptions) {
    this.container = options.container;
    this.selectEl = options.selectEl;
    this.onSelect = options.onSelect;
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
    this.highlight(path);
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
