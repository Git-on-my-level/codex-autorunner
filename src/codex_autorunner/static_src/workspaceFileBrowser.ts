import { WorkspaceNode, deleteWorkspaceFile, deleteWorkspaceFolder, downloadWorkspaceFile, downloadWorkspaceZip } from "./workspaceApi.js";
import { flash } from "./utils.js";

type ChangeHandler = (file: WorkspaceNode) => void;

interface BrowserOptions {
  container: HTMLElement;
  selectEl?: HTMLSelectElement | null;
  breadcrumbsEl?: HTMLElement | null;
  onSelect: ChangeHandler;
  onPathChange?: (path: string) => void;
  onRefresh?: () => Promise<void> | void;
  onConfirm?: (message: string) => Promise<boolean>;
}

export class WorkspaceFileBrowser {
  private tree: WorkspaceNode[] = [];
  private currentPath = "";
  private selectedPath: string | null = null;
  private readonly container: HTMLElement;
  private readonly selectEl: HTMLSelectElement | null;
  private readonly breadcrumbsEl: HTMLElement | null;
  private readonly onSelect: ChangeHandler;
  private readonly onPathChange?: (path: string) => void;
  private readonly onRefresh: () => Promise<void> | void;
  private readonly onConfirm?: (message: string) => Promise<boolean>;
  private readonly fileBtnEl: HTMLElement | null;
  private readonly fileBtnNameEl: HTMLElement | null;
  private readonly modalEl: HTMLElement | null;
  private readonly modalBodyEl: HTMLElement | null;
  private readonly modalCloseEl: HTMLElement | null;

  constructor(options: BrowserOptions) {
    this.container = options.container;
    this.selectEl = options.selectEl ?? null;
    this.breadcrumbsEl = options.breadcrumbsEl ?? null;
    this.onSelect = options.onSelect;
    this.onPathChange = options.onPathChange;
    this.onRefresh = options.onRefresh ?? (() => {});
    this.onConfirm = options.onConfirm;

    this.fileBtnEl = document.getElementById("workspace-file-pill");
    this.fileBtnNameEl = document.getElementById("workspace-file-pill-name");
    this.modalEl = document.getElementById("file-picker-modal");
    this.modalBodyEl = document.getElementById("file-picker-body");
    this.modalCloseEl = document.getElementById("file-picker-close");
    this.initFilePicker();
  }

  setTree(tree: WorkspaceNode[], defaultPath?: string): void {
    this.tree = tree || [];
    const next = this.pickInitialSelection(defaultPath);
    const shouldTrigger = !!next && next !== this.selectedPath;
    const didSelect = next ? this.select(next, shouldTrigger) : false;
    if (!didSelect) this.render();
  }

  getCurrentPath(): string {
    return this.currentPath;
  }

  navigateTo(path: string): void {
    this.currentPath = path;
    if (this.onPathChange) this.onPathChange(this.currentPath);
    this.render();
    this.renderModal();
  }

  select(path: string, trigger = true): boolean {
    const node = this.findNode(path);
    if (!node || node.type !== "file") {
      this.render();
      return false;
    }
    this.selectedPath = path;
    this.currentPath = this.parentPath(path);
    if (this.onPathChange) this.onPathChange(this.currentPath);
    this.updateFileName(node.name);
    this.updateSelect(path);
    this.render();
    if (trigger) this.onSelect(node);
    return true;
  }

  refresh(): void {
    this.render();
    this.renderModal();
  }

  private pickInitialSelection(defaultPath?: string): string | null {
    if (defaultPath && this.findNode(defaultPath)) return defaultPath;
    if (this.selectedPath && this.findNode(this.selectedPath)) {
      return this.selectedPath;
    }
    const firstFile = this.flattenFiles(this.tree).find((n) => n.type === "file");
    return firstFile ? firstFile.path : null;
  }
  private parentPath(path: string): string {
    const parts = path.split("/").filter(Boolean);
    if (parts.length <= 1) return "";
    parts.pop();
    return parts.join("/");
  }

  private flattenFiles(nodes: WorkspaceNode[]): WorkspaceNode[] {
    const acc: WorkspaceNode[] = [];
    const walk = (list: WorkspaceNode[]) => {
      list.forEach((n) => {
        if (n.type === "file") acc.push(n);
        if (n.children?.length) walk(n.children);
      });
    };
    walk(nodes);
    return acc;
  }

  private findNode(path: string, nodes?: WorkspaceNode[]): WorkspaceNode | null {
    const list = nodes || this.tree;
    for (const node of list) {
      if (node.path === path) return node;
      if (node.children?.length) {
        const found = this.findNode(path, node.children);
        if (found) return found;
      }
    }
    return null;
  }

  private getChildren(path: string): WorkspaceNode[] {
    if (!path) return this.tree;
    const node = this.findNode(path);
    return node?.children || [];
  }

  private updateFileName(name: string): void {
    if (this.fileBtnNameEl) this.fileBtnNameEl.textContent = name || "Select file";
  }

  private updateSelect(path: string): void {
    if (!this.selectEl) return;
    const options = this.flattenFiles(this.tree);
    this.selectEl.innerHTML = "";
    options.forEach((node) => {
      const opt = document.createElement("option");
      opt.value = node.path;
      opt.textContent = node.name;
      this.selectEl!.appendChild(opt);
    });
    this.selectEl.value = path;
    this.selectEl.onchange = () => this.select(this.selectEl!.value);
  }

  private renderBreadcrumbs(): void {
    if (!this.breadcrumbsEl) return;
    this.breadcrumbsEl.innerHTML = "";
    const nav = document.createElement("div");
    nav.className = "workspace-breadcrumbs-inner";

    const rootBtn = document.createElement("button");
    rootBtn.type = "button";
    rootBtn.textContent = "Workspace";
    rootBtn.addEventListener("click", () => this.navigateTo(""));
    nav.appendChild(rootBtn);

    const parts = this.currentPath ? this.currentPath.split("/") : [];
    let accum = "";
    parts.forEach((part) => {
      const sep = document.createElement("span");
      sep.textContent = " / ";
      nav.appendChild(sep);

      accum = accum ? `${accum}/${part}` : part;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = part;
      const target = accum;
      btn.addEventListener("click", () => this.navigateTo(target));
      nav.appendChild(btn);
    });

    this.breadcrumbsEl.appendChild(nav);
  }

  private render(): void {
    this.container.innerHTML = "";
    this.renderBreadcrumbs();

    const nodes = this.getChildren(this.currentPath);

    const renderNodes = (list: WorkspaceNode[]): void => {
      if (this.currentPath) {
        const upRow = document.createElement("div");
        upRow.className = "workspace-tree-row workspace-folder-row";
        const label = document.createElement("div");
        label.className = "workspace-tree-label";
        const main = document.createElement("div");
        main.className = "workspace-tree-main";
        const caret = document.createElement("span");
        caret.className = "workspace-tree-caret";
        caret.textContent = "◂";
        main.appendChild(caret);
        const name = document.createElement("button");
        name.type = "button";
        name.className = "workspace-tree-name";
        name.textContent = "Up one level";
        const navigateUp = () => this.navigateTo(this.parentPath(this.currentPath));
        name.addEventListener("click", navigateUp);
        main.appendChild(name);
        label.appendChild(main);
        upRow.appendChild(label);
        upRow.addEventListener("click", (evt) => {
          const target = evt.target as HTMLElement | null;
          if (target?.closest("button")) return;
          navigateUp();
        });
        this.container.appendChild(upRow);
      }

      list.forEach((node) => {
        const row = document.createElement("div");
        row.className = `workspace-tree-row ${node.type === "folder" ? "workspace-folder-row" : "workspace-file-row"}`;
        if (node.path === this.selectedPath) row.classList.add("active");
        row.dataset.path = node.path;
        row.tabIndex = 0;

        const label = document.createElement("div");
        label.className = "workspace-tree-label";

        const main = document.createElement("div");
        main.className = "workspace-tree-main";

        if (node.type === "folder") {
          const caret = document.createElement("span");
          caret.className = "workspace-tree-caret";
          caret.textContent = "▸";
          main.appendChild(caret);
        }

        const name = document.createElement("button");
        name.type = "button";
        name.className = "workspace-tree-name";
        name.textContent = node.name;
        if (node.is_pinned) name.classList.add("pinned");
        const activateNode = () => {
          if (node.type === "folder") {
            this.currentPath = node.path;
            this.render();
            this.renderModal();
          } else {
            this.select(node.path);
          }
        };
        if (node.type === "folder") {
          name.addEventListener("click", activateNode);
        } else {
          name.addEventListener("click", activateNode);
        }
        main.appendChild(name);
        label.appendChild(main);

        const meta = document.createElement("span");
        meta.className = "workspace-tree-meta";
        if (node.type === "file" && node.size != null) {
          meta.textContent = this.prettySize(node.size);
        } else if (node.type === "folder" && node.children) {
          const count = node.children.filter((c) => c.type === "file").length;
          meta.textContent = count ? `${count} file${count === 1 ? "" : "s"}` : "";
        }
        if (meta.textContent) label.appendChild(meta);

        const actions = document.createElement("div");
        actions.className = "workspace-item-actions";

        // Download button for files
        if (node.type === "file") {
          const dlBtn = document.createElement("button");
          dlBtn.type = "button";
          dlBtn.className = "ghost sm workspace-download-btn";
          dlBtn.textContent = "⬇";
          dlBtn.title = `Download ${node.name}`;
          dlBtn.addEventListener("click", (evt) => {
            evt.stopPropagation();
            downloadWorkspaceFile(node.path);
          });
          actions.appendChild(dlBtn);
        }

        // Download as ZIP button for folders
        if (node.type === "folder") {
          const dlBtn = document.createElement("button");
          dlBtn.type = "button";
          dlBtn.className = "ghost sm workspace-download-btn";
          dlBtn.textContent = "⬇";
          dlBtn.title = `Download ${node.name} as ZIP`;
          dlBtn.addEventListener("click", (evt) => {
            evt.stopPropagation();
            downloadWorkspaceZip(node.path);
          });
          actions.appendChild(dlBtn);
        }

        // Delete button for files (non-pinned)
        if (node.type === "file" && !node.is_pinned) {
          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "ghost sm danger";
          delBtn.textContent = "✕";
          delBtn.title = "Delete file";
          delBtn.addEventListener("click", async (evt) => {
            evt.stopPropagation();
            const ok = this.onConfirm ? await this.onConfirm(`Delete ${node.name}?`) : confirm(`Delete ${node.name}?`);
            if (!ok) return;
            try {
              await deleteWorkspaceFile(node.path);
              if (this.selectedPath === node.path) {
                this.selectedPath = null;
              }
              await this.onRefresh();
            } catch (err) {
              flash((err as Error).message || "Failed to delete file", "error");
            }
          });
          actions.appendChild(delBtn);
        }

        // Delete button for folders
        if (node.type === "folder") {
          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "ghost sm danger";
          delBtn.textContent = "✕";
          delBtn.title = "Delete folder";
          delBtn.addEventListener("click", async (evt) => {
            evt.stopPropagation();
            const ok = this.onConfirm
              ? await this.onConfirm(`Delete folder ${node.name}? (must be empty)`)
              : confirm(`Delete folder ${node.name}? (must be empty)`);
            if (!ok) return;
            try {
              await deleteWorkspaceFolder(node.path);
              await this.onRefresh();
            } catch (err) {
              flash((err as Error).message || "Failed to delete folder", "error");
            }
          });
          actions.appendChild(delBtn);
        }

        row.appendChild(label);
        if (actions.childElementCount) row.appendChild(actions);
        row.addEventListener("click", (evt) => {
          const target = evt.target as HTMLElement | null;
          if (target?.closest(".workspace-item-actions")) return;
          if (target?.closest("button")) return;
          activateNode();
        });
        row.addEventListener("keydown", (evt) => {
          if (evt.target !== row) return;
          if (evt.key === "Enter" || evt.key === " ") {
            evt.preventDefault();
            activateNode();
          }
        });
        this.container.appendChild(row);
      });
    };

    renderNodes(nodes);
  }

  private renderModal(): void {
    if (!this.modalBodyEl) return;
    this.modalBodyEl.innerHTML = "";

    const crumbs = document.createElement("div");
    crumbs.className = "file-picker-crumbs";
    const root = document.createElement("button");
    root.type = "button";
    root.textContent = "Workspace";
    root.addEventListener("click", () => {
      this.currentPath = "";
      this.render();
      this.renderModal();
    });
    crumbs.appendChild(root);

    const parts = this.currentPath ? this.currentPath.split("/") : [];
    let accum = "";
    parts.forEach((part) => {
      const sep = document.createElement("span");
      sep.textContent = " / ";
      crumbs.appendChild(sep);
      accum = accum ? `${accum}/${part}` : part;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = part;
      const target = accum;
      btn.addEventListener("click", () => {
        this.currentPath = target;
        this.render();
        this.renderModal();
      });
      crumbs.appendChild(btn);
    });
    this.modalBodyEl.appendChild(crumbs);

    const nodes = this.getChildren(this.currentPath);
    if (!nodes.length) {
      const empty = document.createElement("div");
      empty.className = "file-picker-empty";
      empty.textContent = "Empty folder";
      this.modalBodyEl.appendChild(empty);
      return;
    }

    nodes.forEach((node) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "file-picker-item";
      item.dataset.path = node.path;
      const label = document.createElement("span");
      label.className = "file-picker-name";
      label.textContent = node.name;
      item.appendChild(label);

      const actions = document.createElement("span");
      actions.className = "file-picker-actions";

      // Download button
      const dlBtn = document.createElement("button");
      dlBtn.type = "button";
      dlBtn.className = "ghost sm workspace-download-btn";
      dlBtn.textContent = "⬇";
      dlBtn.title = node.type === "folder" ? `Download ${node.name} as ZIP` : `Download ${node.name}`;
      dlBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (node.type === "folder") {
          downloadWorkspaceZip(node.path);
        } else {
          downloadWorkspaceFile(node.path);
        }
      });
      actions.appendChild(dlBtn);

      // Delete button (for non-pinned items)
      if (!(node.type === "file" && node.is_pinned)) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "ghost sm danger";
        delBtn.textContent = "✕";
        delBtn.title = `Delete ${node.type}`;
        delBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const ok = this.onConfirm
            ? await this.onConfirm(`Delete ${node.name}${node.type === "folder" ? " (must be empty)" : ""}?`)
            : confirm(`Delete ${node.name}${node.type === "folder" ? " (must be empty)" : ""}?`);
          if (!ok) return;
          try {
            if (node.type === "folder") {
              await deleteWorkspaceFolder(node.path);
            } else {
              await deleteWorkspaceFile(node.path);
              if (this.selectedPath === node.path) this.selectedPath = null;
            }
            await this.onRefresh();
            this.render();
            this.renderModal();
          } catch (err) {
            flash((err as Error).message || "Failed to delete", "error");
          }
        });
        actions.appendChild(delBtn);
      }
      item.appendChild(actions);

      if (node.type === "folder") {
        item.classList.add("folder");
        item.addEventListener("click", () => {
          this.currentPath = node.path;
          this.render();
          this.renderModal();
        });
      } else {
        item.classList.add("file");
        item.classList.toggle("active", node.path === this.selectedPath);
        item.addEventListener("click", () => {
          this.select(node.path);
          this.closeModal();
        });
      }
      this.modalBodyEl!.appendChild(item);
    });
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

  private prettySize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    return `${mb.toFixed(1)} MB`;
  }
}
